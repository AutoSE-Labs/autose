#!/usr/bin/env bash
# Run AutoSE on a seeded DeepSWE subset with Pier's local Docker environment.
#
# Usage: run.sh <job-name> [extra pier args...]
# Env:   MODEL (default openai/qwen3.8-27b), N_TASKS (20), SEED (42),
#        N_CONCURRENT (4), AUTOSE_REF (git ref installed in the task image),
#        DEEPSWE_TASKS (path to deep-swe/tasks), VLLM_API_KEY_FILE.
# Needs tunnel.sh running so the model is reachable at http://172.17.0.1/v1.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
JOB="${1:?job name}"; shift
MODEL="${MODEL:-openai/qwen3.8-27b}"
TASKS="${DEEPSWE_TASKS:-$HOME/autose-bench/deep-swe/tasks}"
BASE_URL="${BASE_URL:-http://172.17.0.1/v1}"
KEY="$(cat "${VLLM_API_KEY_FILE:-$HOME/.autose-vllm-key}")"
REF="${AUTOSE_REF:-main}"

include=()
while read -r name; do include+=(--include-task-name "$name"); done \
    < <(python3 "$HERE/sample_tasks.py" "$TASKS" --n "${N_TASKS:-20}" --seed "${SEED:-42}")

# Qwen3.8 thinking-mode sampling from the model card.
EXTRAS='{"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0}'

PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" pier run -p "$TASKS" "${include[@]}" \
    --agent-import-path autose_agent:AutoSEAgent \
    --model "$MODEL" \
    --ak "base_url=$BASE_URL" --ak "ref=$REF" --ae "OPENAI_API_KEY=$KEY" \
    --ak "request_extras=$EXTRAS" \
    --env docker -n "${N_CONCURRENT:-4}" \
    --jobs-dir "$HOME/autose-bench/jobs" --job-name "$JOB" \
    "$@"
