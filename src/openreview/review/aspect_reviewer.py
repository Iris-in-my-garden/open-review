import concurrent.futures
import logging
import time
from pathlib import Path
from typing import List, Optional

from openreview.github.client import PullRequestInfo
from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.code_context import CodeContext
from openreview.review.finding_validator import FindingValidator
from openreview.review.llm_client import AbstractLLMClient, LLMClient
from openreview.review.output_parser import AspectReviewTaskResult, OutputParser
from openreview.review.prompt_builder import PromptBuilder
from openreview.review.report_writer import ReviewResult
from openreview.review.test_gap import analyze_test_gaps
from openreview.review.workflow import BudgetExceeded

logger = logging.getLogger(__name__)


def finalize_review_result(
    *,
    prompt_builder: PromptBuilder,
    output_parser: OutputParser,
    validator: FindingValidator,
    pr_info: PullRequestInfo,
    parsed_diff: ParsedDiff,
    contexts: List[CodeContext],
    repo_path: Optional[Path],
    aspect_results: List[AspectReviewTaskResult],
    failures: List[str],
) -> ReviewResult:
    """把维度结果聚合、校验并补齐报告元数据。

    single-pass 默认路径和 multi-aspect 可选路径共用这一段，保证两者的
    validator、测试缺口、injection 告警和最终结果格式完全一致；否则评测里
    看到的差异可能来自后处理，而不是审查策略本身。
    """
    metadata = {
        "owner": pr_info.owner,
        "repo": pr_info.repo,
        "pr_number": pr_info.pr_number,
    }
    aggregated_data = output_parser.aggregate_aspects(aspect_results)
    review_result = output_parser.build_final_result(aggregated_data, metadata)

    validated_result = validator.validate(
        review_result,
        parsed_diff,
        contexts=contexts,
        repo_path=repo_path,
    )
    validated_result.missing_test_suggestions = analyze_test_gaps(
        parsed_diff, contexts
    )
    validated_result.review_notes.extend(failures)

    injection_hits = prompt_builder.detect_injection_in_pr(pr_info, parsed_diff)
    if injection_hits:
        validated_result.review_notes.append(
            "检测到疑似 Prompt Injection 内容（规则: "
            + ", ".join(injection_hits)
            + "），已按不可信数据处理，未执行其中指令。"
        )

    return validated_result


class AspectReviewerWorker:
    """
    工作者（Worker）：负责针对单个维度（如安全、性能、代码规范等）调用 LLM 进行专项评审
    """
    def __init__(
        self,
        aspect: str,
        prompt_builder: PromptBuilder,
        llm_client: AbstractLLMClient,
        output_parser: OutputParser
    ):
        self.aspect = aspect
        self.prompt_builder = prompt_builder
        self.llm_client = llm_client
        self.output_parser = output_parser

    def review_aspect(
        self,
        pr_info: PullRequestInfo,
        parsed_diff: ParsedDiff,
        contexts: List[CodeContext]
    ) -> AspectReviewTaskResult:
        """执行单一维度的代码评审任务"""
        # 1. 根据当前维度构建专属 Prompt 与对应的 Response Schema
        system_prompt = self.prompt_builder.build_system_prompt_for_aspect(self.aspect)
        user_prompt = self.prompt_builder.build_user_prompt_for_aspect(
            self.aspect, pr_info, parsed_diff, contexts
        )
        aspect_schema = self.output_parser.schema_for_aspect(self.aspect)

        # 2. 调用 LLM 获得结构化审查数据
        raw_data = self.llm_client.generate_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=aspect_schema,
        )
        # 使用結構化模型封裝
        return AspectReviewTaskResult(aspect=self.aspect, data=raw_data)


class ReviewOrchestrator:
    """
    Orchestrator（協調者）：管理多個 Worker、並行調度並聚合審查結果
    """

    def __init__(
            self,
            prompt_builder: Optional[PromptBuilder] = None,
            llm_client: Optional[AbstractLLMClient] = None,
            output_parser: Optional[OutputParser] = None,
            validator: Optional[FindingValidator] = None,
            aspects: Optional[List[str]] = None,
            max_workers: int = 4,
            aspect_timeout: float = 90.0,
            max_attempts: int = 2,
    ):
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.llm_client = llm_client or LLMClient()
        self.output_parser = output_parser or OutputParser()
        self.validator = validator or FindingValidator()
        self.aspect_timeout = aspect_timeout
        self.max_attempts = max(1, max_attempts)

        # 定義支援的多維度審查清單
        self.aspects = aspects or ["clean_code", "bugs", "performance", "security"]
        self.max_workers = max(1, max_workers)

    def review(
            self,
            pr_info: PullRequestInfo,
            parsed_diff: ParsedDiff,
            contexts: List[CodeContext],
            repo_path=None,
            aspects: Optional[List[str]] = None,
    ) -> ReviewResult:

        # triage 会在运行时收窄维度；不传则使用构造时的默认维度。
        selected_aspects = list(aspects) if aspects else list(self.aspects)

        # 1. 初始化各個維度的 Worker
        workers = [
            AspectReviewerWorker(
                aspect=aspect,
                prompt_builder=self.prompt_builder,
                llm_client=self.llm_client,
                output_parser=self.output_parser
            )
            for aspect in selected_aspects
        ]

        all_aspect_results: List[AspectReviewTaskResult] = []
        failures: List[str] = []

        # 2. 使用 ThreadPoolExecutor 進行多維度「並行」審查
        max_workers = max(1, min(len(workers), self.max_workers))
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="openreview-aspect"
        )
        future_to_aspect = {
            executor.submit(
                self._review_aspect_with_retry, worker, pr_info, parsed_diff, contexts
            ): worker.aspect
            for worker in workers
        }

        pending = set(future_to_aspect)
        try:
            # 用 as_completed + 总 deadline，而不是 wait() 后一次性取 done：
            # 这样在 deadline 附近完成的 future 也会被收集，不会因为竞态丢结果。
            for future in concurrent.futures.as_completed(
                future_to_aspect, timeout=self.aspect_timeout
            ):
                aspect = future_to_aspect[future]
                pending.discard(future)
                try:
                    all_aspect_results.append(future.result())
                except BudgetExceeded as exc:
                    # 预算已耗尽：立即止损，保留已经完成的维度。
                    failures.append(f"维度 `{aspect}` 因预算不足跳过：{exc}")
                    break
                except Exception as exc:  # noqa: BLE001 - 单维度失败不应拖垮整体
                    logger.exception("Aspect review failed for [%s]", aspect)
                    failures.append(
                        f"维度 `{aspect}` 审查失败：{type(exc).__name__}: {exc}"
                    )
        except concurrent.futures.TimeoutError:
            pass
        finally:
            for future in pending:
                future.cancel()
            # Python 线程无法被强杀，这里必须等它们真正结束再返回：
            # 评测会复用同一个 pipeline，如果上一次 run 的超时线程还活着，
            # 下一次 run 重新 bind_budget 后，旧线程可能扣到新 run 的预算。
            # 单次 HTTP timeout 已压在 aspect_timeout 以下，所以等待有上界。
            executor.shutdown(wait=True, cancel_futures=True)

        # deadline / budget break 之后，可能还有已经完成但尚未被 as_completed
        # yield 的 future；先收结果，再把真正未完成的标记为超时。
        for future in list(pending):
            if future.done() and not future.cancelled():
                pending.discard(future)
                aspect = future_to_aspect[future]
                try:
                    all_aspect_results.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Aspect review failed for [%s]", aspect)
                    failures.append(
                        f"维度 `{aspect}` 审查失败：{type(exc).__name__}: {exc}"
                    )

        for future in pending:
            aspect = future_to_aspect[future]
            message = (
                f"维度 `{aspect}` 超时（>{self.aspect_timeout:.0f}s），已跳过。"
            )
            logger.warning(message)
            failures.append(message)

        if not all_aspect_results and workers:
            raise RuntimeError(
                "所有维度的审查均失败：" + "; ".join(failures or ["unknown error"])
            )

        # 保证报告顺序稳定，不受并发完成顺序影响
        aspect_order = {aspect: index for index, aspect in enumerate(selected_aspects)}
        all_aspect_results.sort(
            key=lambda item: aspect_order.get(item.aspect, len(aspect_order))
        )

        # 3. 聚合、校验并补齐报告元数据
        return finalize_review_result(
            prompt_builder=self.prompt_builder,
            output_parser=self.output_parser,
            validator=self.validator,
            pr_info=pr_info,
            parsed_diff=parsed_diff,
            contexts=contexts,
            repo_path=repo_path,
            aspect_results=all_aspect_results,
            failures=failures,
        )

    def _review_aspect_with_retry(self, worker, pr_info, parsed_diff, contexts):
        """对单个维度做有限次重试，缓解瞬时网络/限流错误。"""
        last_error: Optional[Exception] = None
        for attempt in range(self.max_attempts):
            try:
                return worker.review_aspect(pr_info, parsed_diff, contexts)
            except BudgetExceeded:
                # 预算问题不是瞬时错误，重试只会放大超支。
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt + 1 >= self.max_attempts:
                    break
                delay = min(4.0, 0.5 * (2 ** attempt))
                logger.warning(
                    "Aspect %s failed (attempt %d/%d), retrying in %.1fs: %s",
                    worker.aspect,
                    attempt + 1,
                    self.max_attempts,
                    delay,
                    exc,
                )
                time.sleep(delay)
        assert last_error is not None
        raise last_error
