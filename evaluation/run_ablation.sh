#!/usr/bin/env bash
#
# 一条命令跑完四档消融：review -> metrics -> gate -> 可选 semantic -> report。
#
# 用法示例：
#   ./evaluation/run_ablation.sh --dry-run
#   ./evaluation/run_ablation.sh --split dev --model gpt-4o-mini \
#       --preflight-tokens 200000 --run-budget-tokens 20000
#   ./evaluation/run_ablation.sh --split dev --semantic \
#       --aacr-eval-dir /tmp/aacr-bench/evaluation
#
# 注意：
#   - 四档必须使用同一个 --model、同一份 manifest、同一个 --split。
#   - --preflight-tokens 是整批预检；--run-budget-tokens 是单个 PR 的运行时上限。
#   - --dry-run 会实际检索上下文并估算 prompt，必要时 clone/fetch；不会调用 LLM。

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PYTHON_BIN="${PYTHON_BIN:-uv run python}"
read -r -a PYTHON <<< "$PYTHON_BIN"
MANIFEST="evaluation/manifests/aacr_pygo_v3.json"
SPLIT="dev"
MODEL=""
VERIFIER_MODEL=""
THINKING=1
VERIFIER_THINKING=0
REASONING_EFFORT=""
LEVELS="diff ast graph verifier"
LIMIT=""
PREFLIGHT_TOKENS=""
RUN_BUDGET_TOKENS=""
BUDGET_CALLS=""
REPORT_OUT=""
DRY_RUN=0
RESUME=0
SEMANTIC=0
AACR_EVAL_DIR=""
NO_VERIFY=0

usage() {
  cat <<'EOF'
用法: evaluation/run_ablation.sh [options]

  --split SPLIT              dev | holdout | all（默认 dev；test 仅用于旧 manifest）
  --model MODEL              Review 主模型；不传则默认 deepseek-v4-pro
  --verifier-model MODEL     Verifier 模型；不传则 DeepSeek 下默认 deepseek-flash
  --no-thinking              关闭 Review thinking（默认开启，仅 DeepSeek 生效）
  --verifier-thinking        开启 Verifier thinking（默认关闭）
  --reasoning-effort LEVEL   none | low | high | max（默认 high）
  --levels "diff ast ..."    要跑的档位（默认全部四档）
  --limit N                  每个 split 只跑前 N 条（smoke）
  --preflight-tokens N       split 级 token 预检；超限则在调用 LLM 前退出
  --run-budget-tokens N      单个 PR run 的运行时输入 token 上限
  --budget-calls N           单个 PR run 的 LLM 调用上限
  --dry-run                  实际检索上下文并估算 prompt；不调用 LLM
  --resume                   跳过已有成功且配置匹配的结果
  --semantic                 复用配置匹配的完整结果；缺失时补跑 review，再跑 AACR judge
  --aacr-eval-dir DIR        AACR 的 evaluation/ 目录（--semantic 必需）
  --no-verify-source         跳过数据集 SHA256 校验（仅本地 fixture / 镜像）
  --report-out PATH          汇总报告输出路径（默认 reports/eval_<split>.md）
  -h, --help                 显示本帮助

示例:
  ./evaluation/run_ablation.sh --dry-run
  ./evaluation/run_ablation.sh --split dev --model gpt-4o-mini \
    --preflight-tokens 200000 --run-budget-tokens 20000
EOF
}

require_value() {
  if [[ $# -lt 2 || -z "$2" ]]; then
    echo "error: $1 需要一个值" >&2
    exit 2
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --split) require_value "$@"; SPLIT="$2"; shift 2 ;;
    --model) require_value "$@"; MODEL="$2"; shift 2 ;;
    --verifier-model) require_value "$@"; VERIFIER_MODEL="$2"; shift 2 ;;
    --thinking) THINKING=1; shift ;;
    --no-thinking) THINKING=0; shift ;;
    --verifier-thinking) VERIFIER_THINKING=1; shift ;;
    --no-verifier-thinking) VERIFIER_THINKING=0; shift ;;
    --reasoning-effort) require_value "$@"; REASONING_EFFORT="$2"; shift 2 ;;
    --levels) require_value "$@"; LEVELS="$2"; shift 2 ;;
    --limit) require_value "$@"; LIMIT="$2"; shift 2 ;;
    --preflight-tokens) require_value "$@"; PREFLIGHT_TOKENS="$2"; shift 2 ;;
    --run-budget-tokens) require_value "$@"; RUN_BUDGET_TOKENS="$2"; shift 2 ;;
    --budget-calls) require_value "$@"; BUDGET_CALLS="$2"; shift 2 ;;
    --report-out) require_value "$@"; REPORT_OUT="$2"; shift 2 ;;
    --aacr-eval-dir) require_value "$@"; AACR_EVAL_DIR="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --resume) RESUME=1; shift ;;
    --semantic) SEMANTIC=1; shift ;;
    --no-verify-source) NO_VERIFY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: 未知参数 $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$SEMANTIC" -eq 1 && -z "$AACR_EVAL_DIR" ]]; then
  echo "error: --semantic 需要 --aacr-eval-dir" >&2
  exit 2
fi

if [[ ! -f "$MANIFEST" ]]; then
  echo "error: 找不到 manifest: $MANIFEST" >&2
  exit 2
fi

VERSION="$("${PYTHON[@]}" -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$MANIFEST")"
if [[ -z "$REPORT_OUT" ]]; then
  REPORT_OUT="reports/eval_${SPLIT}.md"
fi

read -r -a LEVEL_ARRAY <<< "$LEVELS"
if [[ ${#LEVEL_ARRAY[@]} -eq 0 ]]; then
  echo "error: --levels 不能为空" >&2
  exit 2
fi

run_review() {
  local level="$1"
  local run_id="${SPLIT}-${level}"
  if [[ "$SEMANTIC" -eq 1 ]] && is_reusable_level "$level"; then
    echo "==> [$level] 复用已存在且配置匹配的 review 结果"
    return 0
  fi

  local -a cmd=(
    "${PYTHON[@]}" -m evaluation.cli review
    --manifest "$MANIFEST"
    --split "$SPLIT"
    --run-id "$run_id"
    --ablation "$level"
  )
  if [[ -n "$MODEL" ]]; then cmd+=(--model "$MODEL"); fi
  if [[ -n "$VERIFIER_MODEL" ]]; then cmd+=(--verifier-model "$VERIFIER_MODEL"); fi
  if [[ "$THINKING" -eq 1 ]]; then cmd+=(--thinking); else cmd+=(--no-thinking); fi
  if [[ "$VERIFIER_THINKING" -eq 1 ]]; then cmd+=(--verifier-thinking); fi
  if [[ -n "$REASONING_EFFORT" ]]; then cmd+=(--reasoning-effort "$REASONING_EFFORT"); fi
  if [[ -n "$LIMIT" ]]; then cmd+=(--limit "$LIMIT"); fi
  if [[ -n "$PREFLIGHT_TOKENS" ]]; then cmd+=(--preflight-tokens "$PREFLIGHT_TOKENS"); fi
  if [[ -n "$RUN_BUDGET_TOKENS" ]]; then cmd+=(--run-budget-tokens "$RUN_BUDGET_TOKENS"); fi
  if [[ -n "$BUDGET_CALLS" ]]; then cmd+=(--budget-calls "$BUDGET_CALLS"); fi
  if [[ "$RESUME" -eq 1 || "$SEMANTIC" -eq 1 ]]; then cmd+=(--resume); fi
  if [[ "$DRY_RUN" -eq 1 ]]; then cmd+=(--dry-run); fi
  if [[ "$NO_VERIFY" -eq 1 ]]; then cmd+=(--no-verify-source); fi

  echo ""
  echo "==> [$level] review ($run_id)"
  "${cmd[@]}"
}

is_reusable_level() {
  local level="$1"
  local -a cmd=(
    "${PYTHON[@]}" -m evaluation.cli check-run
    --manifest "$MANIFEST"
    --split "$SPLIT"
    --run-id "${SPLIT}-${level}"
    --ablation "$level"
  )
  if [[ -n "$LIMIT" ]]; then cmd+=(--limit "$LIMIT"); fi
  if [[ -n "$MODEL" ]]; then cmd+=(--model "$MODEL"); fi
  if [[ -n "$VERIFIER_MODEL" ]]; then cmd+=(--verifier-model "$VERIFIER_MODEL"); fi
  if [[ "$THINKING" -eq 1 ]]; then cmd+=(--thinking); else cmd+=(--no-thinking); fi
  if [[ "$VERIFIER_THINKING" -eq 1 ]]; then cmd+=(--verifier-thinking); fi
  if [[ -n "$REASONING_EFFORT" ]]; then cmd+=(--reasoning-effort "$REASONING_EFFORT"); fi
  "${cmd[@]}"
}

run_metrics_and_gate() {
  local level="$1"
  local run_id="${SPLIT}-${level}"
  local metrics_dir="evaluation/metrics/${VERSION}/${run_id}"
  local deterministic="${metrics_dir}/deterministic_${run_id}.json"

  local -a extra=()
  if [[ "$NO_VERIFY" -eq 1 ]]; then extra+=(--no-verify-source); fi

  echo ""
  echo "==> [$level] metrics"
  local -a metrics_cmd=(
    "${PYTHON[@]}" -m evaluation.cli metrics
    --manifest "$MANIFEST"
    --split "$SPLIT"
    --run-id "$run_id"
  )
  if [[ ${#extra[@]} -gt 0 ]]; then metrics_cmd+=("${extra[@]}"); fi
  "${metrics_cmd[@]}"

  echo ""
  echo "==> [$level] gate"
  if ! "${PYTHON[@]}" -m evaluation.cli gate --metrics "$deterministic"; then
    echo "warning: [$level] gate 未通过；继续跑其余档位，最终汇总仍会生成。" >&2
    GATE_FAILED=1
  fi

  if [[ "$SEMANTIC" -eq 1 ]]; then
    echo ""
    echo "==> [$level] semantic"
    local -a semantic_cmd=(
      "${PYTHON[@]}" -m evaluation.cli semantic
      --manifest "$MANIFEST"
      --split "$SPLIT"
      --run-id "$run_id"
      --aacr-eval-dir "$AACR_EVAL_DIR"
    )
    if [[ ${#extra[@]} -gt 0 ]]; then semantic_cmd+=("${extra[@]}"); fi
    "${semantic_cmd[@]}"
  fi
}

GATE_FAILED=0

NEEDS_REVIEW=0
if [[ "$DRY_RUN" -eq 0 ]]; then
  if [[ "$SEMANTIC" -eq 1 ]]; then
    for level in "${LEVEL_ARRAY[@]}"; do
      if ! is_reusable_level "$level"; then
        NEEDS_REVIEW=1
      fi
    done
  else
    NEEDS_REVIEW=1
  fi
  if [[ "$NEEDS_REVIEW" -eq 1 ]]; then
    echo ""
    echo "==> 预热仓库缓存（按唯一仓库准备，并确保所需 commits 可用）"
    prepare_cmd=(
      "${PYTHON[@]}" -m evaluation.cli prepare-repos
      --manifest "$MANIFEST"
      --split "$SPLIT"
    )
    if [[ -n "$LIMIT" ]]; then prepare_cmd+=(--limit "$LIMIT"); fi
    "${prepare_cmd[@]}"
  fi
fi

for level in "${LEVEL_ARRAY[@]}"; do
  run_review "$level"
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo ""
  echo "dry-run 完成：已实际检索上下文并估算 prompt，没有调用 LLM。"
  exit 0
fi

for level in "${LEVEL_ARRAY[@]}"; do
  run_metrics_and_gate "$level"
done

echo ""
echo "==> report"
report_cmd=("${PYTHON[@]}" -m evaluation.cli report)
for level in "${LEVEL_ARRAY[@]}"; do
  report_cmd+=(--run "${level}=evaluation/metrics/${VERSION}/${SPLIT}-${level}")
done
report_cmd+=(--out "$REPORT_OUT")
"${report_cmd[@]}"

echo ""
echo "完成。汇总报告: $REPORT_OUT"
if [[ "$GATE_FAILED" -ne 0 ]]; then
  echo "注意：至少一个档位的 gate 未通过，请查看上方输出。" >&2
  exit 1
fi
