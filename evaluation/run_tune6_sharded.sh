#!/usr/bin/env bash
# Run the 6-PR recall experiment in two repo-aware shards.
#
# The current worktree is expected to contain the prompt variant under test.
# Shards are split by repository to avoid concurrent git-worktree/index
# contention on the same repository.
#
# Usage:
#   bash evaluation/run_tune6_sharded.sh
#   RUN_SEMANTIC=1 JUDGE_API_KEY=... bash evaluation/run_tune6_sharded.sh
#   RUN_ID=tune6-recall-b FORCE=1 bash evaluation/run_tune6_sharded.sh
#
# Useful overrides:
#   REPO_CACHE=/path/to/OpenReview/evaluation/.cache/repos
#   RESULTS_ROOT=/path/to/OpenReview/evaluation/results/aacr-pygo-v3
#   METRICS_ROOT=/path/to/OpenReview/evaluation/metrics/aacr-pygo-v3
#   MAX_CONCURRENT_BATCHES=1   # lower this if the API returns 429/timeouts
#   RESUME=1                   # reuse already-ok shard results
#   RUN_SEMANTIC=1             # also run the AACR semantic judge
#   BASELINE_METRICS=/path/to/evaluation/metrics/aacr-pygo-v3/dev-production-v2
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MANIFEST="${MANIFEST:-$ROOT/evaluation/manifests/aacr_pygo_v3.json}"
RUN_ID="${RUN_ID:-tune6-recall-a}"
SPLIT="${SPLIT:-dev}"
REPO_CACHE="${REPO_CACHE:-$ROOT/evaluation/.cache/repos}"
RESULTS_ROOT="${RESULTS_ROOT:-$ROOT/evaluation/results/aacr-pygo-v3}"
METRICS_ROOT="${METRICS_ROOT:-$ROOT/evaluation/metrics/aacr-pygo-v3}"
WORK_DIR="${WORK_DIR:-$ROOT/reports/tune6/$RUN_ID}"

PREFLIGHT_TOKENS="${PREFLIGHT_TOKENS:-800000}"
RUN_BUDGET_TOKENS="${RUN_BUDGET_TOKENS:-600000}"
MAX_CONCURRENT_BATCHES="${MAX_CONCURRENT_BATCHES:-2}"
LLM_TIMEOUT="${LLM_TIMEOUT:-300}"
LLM_MAX_RETRIES="${LLM_MAX_RETRIES:-2}"
REASONING_EFFORT="${REASONING_EFFORT:-high}"
RESUME="${RESUME:-0}"
FORCE="${FORCE:-0}"
MOCK_LLM="${MOCK_LLM:-0}"
RUN_SEMANTIC="${RUN_SEMANTIC:-0}"
AACR_EVAL_DIR="${AACR_EVAL_DIR:-$HOME/PycharmProjects/aacr-bench/evaluation}"
BASELINE_METRICS="${BASELINE_METRICS:-$METRICS_ROOT/dev-production-v2}"

SHARD0_IDS="${SHARD0_IDS:-mudler__LocalAI@e466450,ollama__ollama@2ed14f2,ollama__ollama@b6002f6}"
SHARD1_IDS="${SHARD1_IDS:-wavetermdev__waveterm@61b0fb4,comfyanonymous__ComfyUI@4dcd698,langflow-ai__langflow@b1cbdba}"

LOG_DIR="$WORK_DIR/logs"
TUNE_MANIFEST="$WORK_DIR/tune6.json"
SHARD0_RESULTS="$RESULTS_ROOT/$RUN_ID-s0"
SHARD1_RESULTS="$RESULTS_ROOT/$RUN_ID-s1"
MERGED_RESULTS="$RESULTS_ROOT/$RUN_ID"
MERGED_METRICS="$METRICS_ROOT/$RUN_ID"

if [[ ! -d "$REPO_CACHE" ]]; then
  echo "error: repo cache not found: $REPO_CACHE" >&2
  echo "hint: set REPO_CACHE to the main worktree's evaluation/.cache/repos" >&2
  exit 2
fi
if [[ "$MOCK_LLM" != "1" && -z "${DEEPSEEK_API_KEY:-}${OPENAI_API_KEY:-}" ]]; then
  echo "error: set DEEPSEEK_API_KEY (or OPENAI_API_KEY) first" >&2
  exit 2
fi
if [[ ! -f "$MANIFEST" ]]; then
  echo "error: manifest not found: $MANIFEST" >&2
  exit 2
fi

mkdir -p "$WORK_DIR" "$LOG_DIR"

echo "[build] shard manifests -> $WORK_DIR"
uv run python -m evaluation.sharding build \
  --manifest "$MANIFEST" \
  --output-dir "$WORK_DIR" \
  --shard "s0=$SHARD0_IDS" \
  --shard "s1=$SHARD1_IDS" \
  --tune-manifest "$TUNE_MANIFEST"

REVIEW_ARGS=(
  --split "$SPLIT"
  --repo-cache "$REPO_CACHE"
  --preflight-tokens "$PREFLIGHT_TOKENS"
  --run-budget-tokens "$RUN_BUDGET_TOKENS"
  --max-concurrent-batches "$MAX_CONCURRENT_BATCHES"
  --llm-timeout "$LLM_TIMEOUT"
  --llm-max-retries "$LLM_MAX_RETRIES"
  --reasoning-effort "$REASONING_EFFORT"
)
if [[ "$RESUME" == "1" ]]; then
  REVIEW_ARGS+=(--resume)
fi
if [[ "$MOCK_LLM" == "1" ]]; then
  REVIEW_ARGS+=(--mock-llm)
fi

echo "[run] shard s0 -> $SHARD0_RESULTS"
uv run python -m evaluation.cli review \
  --manifest "$WORK_DIR/s0.json" \
  --run-id "$RUN_ID-s0" \
  --results-dir "$SHARD0_RESULTS" \
  "${REVIEW_ARGS[@]}" \
  > "$LOG_DIR/s0.log" 2>&1 &
PID0=$!

echo "[run] shard s1 -> $SHARD1_RESULTS"
uv run python -m evaluation.cli review \
  --manifest "$WORK_DIR/s1.json" \
  --run-id "$RUN_ID-s1" \
  --results-dir "$SHARD1_RESULTS" \
  "${REVIEW_ARGS[@]}" \
  > "$LOG_DIR/s1.log" 2>&1 &
PID1=$!

STATUS=0
wait "$PID0" || STATUS=1
wait "$PID1" || STATUS=1
if [[ "$STATUS" -ne 0 ]]; then
  echo "error: at least one shard failed" >&2
  echo "--- s0.log (tail) ---" >&2
  tail -n 40 "$LOG_DIR/s0.log" >&2 || true
  echo "--- s1.log (tail) ---" >&2
  tail -n 40 "$LOG_DIR/s1.log" >&2 || true
  exit 1
fi

echo "[merge] $SHARD0_RESULTS + $SHARD1_RESULTS -> $MERGED_RESULTS"
MERGE_ARGS=(
  --output-dir "$MERGED_RESULTS"
  --run "$SHARD0_RESULTS"
  --run "$SHARD1_RESULTS"
  --instance-order-manifest "$TUNE_MANIFEST"
)
if [[ "$FORCE" == "1" ]]; then
  MERGE_ARGS+=(--force)
fi
uv run python -m evaluation.sharding merge "${MERGE_ARGS[@]}"

echo "[metrics] -> $MERGED_METRICS"
uv run python -m evaluation.cli metrics \
  --manifest "$TUNE_MANIFEST" \
  --split "$SPLIT" \
  --run-id "$RUN_ID" \
  --results-dir "$MERGED_RESULTS" \
  --metrics-dir "$MERGED_METRICS"

DETERMINISTIC="$MERGED_METRICS/deterministic_$RUN_ID.json"
if [[ -f "$DETERMINISTIC" ]]; then
  echo "[gate] $DETERMINISTIC"
  uv run python -m evaluation.cli gate --metrics "$DETERMINISTIC" || true
fi

if [[ "$RUN_SEMANTIC" == "1" ]]; then
  : "${JUDGE_API_KEY:?set JUDGE_API_KEY to run semantic evaluation}"
  export JUDGE_BASE_URL="${JUDGE_BASE_URL:-https://api.deepseek.com}"
  export JUDGE_MODEL="${JUDGE_MODEL:-deepseek-v4-pro}"
  export JUDGE_USE_MOCK="${JUDGE_USE_MOCK:-false}"
  echo "[semantic] $MERGED_RESULTS -> $MERGED_METRICS"
  uv run python -m evaluation.cli semantic \
    --manifest "$TUNE_MANIFEST" \
    --split "$SPLIT" \
    --run-id "$RUN_ID" \
    --results-dir "$MERGED_RESULTS" \
    --metrics-dir "$MERGED_METRICS" \
    --aacr-eval-dir "$AACR_EVAL_DIR"
fi

if [[ -d "$BASELINE_METRICS" ]]; then
  echo "[compare] baseline=$BASELINE_METRICS candidate=$MERGED_METRICS"
  uv run python -m evaluation.sharding compare \
    --baseline-metrics "$BASELINE_METRICS" \
    --candidate-metrics "$MERGED_METRICS" \
    --manifest "$TUNE_MANIFEST" || true
else
  echo "[compare] baseline metrics not found: $BASELINE_METRICS (skipped)"
fi

echo "[done] merged results: $MERGED_RESULTS"
echo "[done] metrics:        $MERGED_METRICS"
echo "[done] logs:           $LOG_DIR"
