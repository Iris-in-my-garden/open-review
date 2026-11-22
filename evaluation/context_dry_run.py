"""Build a real retrieval/prompt estimate without making any LLM requests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from openreview.repo.local import LocalReviewInput
from openreview.retriever.context_planner import plan_contexts
from openreview.review.redaction import redact_text
from openreview.review.single_shot import SINGLE_SHOT_INSTRUCTION
from openreview.review.token_budget import estimate_tokens


def build_context_dry_run(
    instance: Dict[str, Any],
    *,
    repo_path,
    pipeline,
) -> Dict[str, Any]:
    """Run the review preparation/retrieval path and report prompt inputs only."""
    owner, repo_name = instance["repo"].split("/", 1)
    local_input = LocalReviewInput(
        repo_path=repo_path,
        base_sha=instance["base_commit"],
        head_sha=instance["head_commit"],
        owner=owner,
        repo=repo_name,
        pr_number=int(instance.get("pr_number") or 0),
        title=f"AACR {instance['instance_id']}",
        pr_url=instance.get("github_pr_url"),
    )
    session = pipeline._new_session()
    state = session.new_state(
        pr_url=instance.get("github_pr_url") or "local://aacr-dry-run",
        output_dir=Path("."),
        local_mode=True,
        local_input=local_input,
    )
    executor = session.executor
    try:
        executor._node_load_local(state)
        executor._node_prepare_local(state)
        executor._node_parse_diff(state)
        executor._node_triage(state)

        batches = executor.batch_planner.plan(state, session.retriever)
        impact_contexts, static_contexts = executor.evidence_pipeline.collect(state)
        prompt_builder = session.reviewer.prompt_builder
        system_prompt = prompt_builder.build_system_prompt()
        batch_reports = []

        for index, batch in enumerate(batches, start=1):
            prompt_builder.reset_context_stats()
            contexts = session.retriever.retrieve(
                state.local_repo_path, batch, base_path=state.base_path
            )
            contexts.extend(
                executor.evidence_pipeline.extra_contexts(
                    batch,
                    impact_contexts,
                    static_contexts,
                    include_global_impact=index == 1,
                )
            )
            contexts = plan_contexts(
                contexts,
                pipeline.config.max_context_tokens,
                stats=state.retrieval_stats,
            )
            user_prompt = prompt_builder.build_user_prompt(
                state.pr_info,
                batch,
                contexts,
                task_instruction=(
                    SINGLE_SHOT_INSTRUCTION
                    + session.reviewer._focus_instruction(state.selected_aspects)
                ),
            )
            included_ids = {
                context.context_id
                for context in contexts
                if context.context_id and f"[{context.context_id}]" in user_prompt
            }
            context_reports = []
            for context in contexts:
                context.ensure_context_id()
                safe_snippet, redaction_count = redact_text(context.snippet)
                safe_context = context.model_copy(update={"snippet": safe_snippet})
                rendered = prompt_builder._render_context(safe_context)
                context_reports.append(
                    {
                        "context_id": context.context_id,
                        "file": context.file_path,
                        "type": context.kind,
                        "priority": f"P{int(context.priority)}",
                        "priority_order": int(context.priority),
                        "symbol": context.symbol,
                        "revision": context.revision,
                        "start_line": context.start_line,
                        "end_line": context.end_line,
                        "snippet_tokens_estimate": estimate_tokens(safe_snippet),
                        "prompt_segment_tokens_estimate": estimate_tokens(rendered) + 20,
                        "included_in_prompt": context.context_id in included_ids,
                        "redactions": redaction_count,
                        "text_is_redacted": True,
                        "text": safe_snippet,
                    }
                )

            system_tokens = estimate_tokens(system_prompt)
            user_tokens = estimate_tokens(user_prompt)
            batch_reports.append(
                {
                    "batch": index,
                    "files": [file.filename for file in batch.files],
                    "contexts": context_reports,
                    "patch_tokens_estimate": sum(
                        estimate_tokens(file.patch or "") for file in batch.files
                    ),
                    "system_prompt_tokens_estimate": system_tokens,
                    "user_prompt_tokens_estimate": user_tokens,
                    "final_prompt_tokens_estimate": system_tokens + user_tokens,
                    "prompt_estimate_scope": (
                        "primary review system + user text; excludes conditional "
                        "verifier prompts and provider-side schema/request encoding"
                    ),
                    "completion_tokens_limit": pipeline.config.max_completion_tokens,
                    "context_stats": dict(prompt_builder.context_stats),
                }
            )

        return {
            "instance_id": instance["instance_id"],
            "repo": instance["repo"],
            "base_commit": instance["base_commit"],
            "head_commit": instance["head_commit"],
            "llm_called": False,
            "token_estimator": "openreview.review.token_budget.estimate_tokens (heuristic, not provider tokenizer)",
            "batches": batch_reports,
            "final_prompt_tokens_estimate": sum(
                batch["final_prompt_tokens_estimate"] for batch in batch_reports
            ),
        }
    finally:
        if state.local_workspace is not None:
            state.local_workspace.cleanup()
