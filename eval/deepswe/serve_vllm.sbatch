#!/bin/bash
# Serve a model with vLLM on the turing cluster (L40S, sm89).
# Usage: sbatch --export=ALL,MODEL=Qwen/Qwen3.8-27B-FP8,SERVED=qwen3.8-27b serve_vllm.sbatch
# Weights and the venv live on the node-local /scratch, so pin the node that
# has them (-w) when submitting.
#SBATCH -J autose-vllm
#SBATCH -A karthik.vaidhyanathan
#SBATCH -p u22
#SBATCH -w node08
#SBATCH --gres=gpu:1
#SBATCH -c 8
#SBATCH --mem=64G
#SBATCH -t 1-00:00:00
#SBATCH -o %x-%j.log

set -euo pipefail

MODEL="${MODEL:-Qwen/Qwen3.8-27B-FP8}"
SERVED="${SERVED:-qwen3.8-27b}"
TP="${TP:-1}"
PORT="${PORT:-8000}"
MAX_LEN="${MAX_LEN:-131072}"
SCRATCH="/scratch/$USER"

export HF_HOME="$SCRATCH/hf"
export HF_HUB_OFFLINE=1
source "$SCRATCH/vllm-env/bin/activate"

# The key keeps the endpoint closed to anyone else who can reach the node.
API_KEY_FILE="$HOME/.autose-vllm-key"
[ -f "$API_KEY_FILE" ] || (umask 077 && openssl rand -hex 24 > "$API_KEY_FILE")

nvidia-smi --query-gpu=name,memory.total --format=csv
echo "serving $MODEL as $SERVED on $(hostname):$PORT (tp=$TP, max_len=$MAX_LEN)"

exec vllm serve "$MODEL" \
    --served-model-name "$SERVED" \
    --host 0.0.0.0 --port "$PORT" \
    --api-key "$(cat "$API_KEY_FILE")" \
    --tensor-parallel-size "$TP" \
    --max-model-len "$MAX_LEN" \
    --kv-cache-dtype fp8 \
    --gpu-memory-utilization 0.92 \
    --enable-prefix-caching \
    --language-model-only \
    --reasoning-parser qwen3 \
    --enable-auto-tool-choice --tool-call-parser qwen3_xml
