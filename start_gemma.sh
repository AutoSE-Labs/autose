#!/usr/bin/env bash
# Start the vllm server for nvidia/Gemma-4-26B-A4B-NVFP4 on dgx-spark.
# Usage: bash start_gemma.sh [--foreground]
#   --foreground   keep the process attached (default: run via nohup in background)

set -euo pipefail

VENV="/home/sa4s/.venv"
MODEL="nvidia/Gemma-4-26B-A4B-NVFP4"
PORT=9000
LOG="$HOME/vllm-gemma.log"

export PATH="$VENV/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Kill ALL processes holding GPU memory (catches crashed/zombie vllm processes)
echo "Checking for GPU memory users..."
GPU_PIDS=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | tr -d ' ' || true)
VLLM_PIDS=$(pgrep -f "vllm" 2>/dev/null || true)
ALL_PIDS=$(printf '%s\n' $GPU_PIDS $VLLM_PIDS | sort -u | tr '\n' ' ')
if [ -n "$(echo $ALL_PIDS | tr -d ' ')" ]; then
    echo "Killing processes: $ALL_PIDS"
    kill -9 $ALL_PIDS 2>/dev/null || true
    sleep 3
fi

CMD="$VENV/bin/vllm serve $MODEL \
    --port $PORT \
    --trust-remote-code \
    --enable-auto-tool-choice \
    --tool-call-parser pythonic"

if [ "${1:-}" = "--foreground" ]; then
    echo "Starting vllm in foreground..."
    exec $CMD
else
    echo "Starting vllm in background (log: $LOG)..."
    nohup $CMD > "$LOG" 2>&1 &
    SERVER_PID=$!
    echo "PID $SERVER_PID — waiting for server to become ready..."
    for i in $(seq 1 120); do
        # Bail early if the server process has already died
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            echo "ERROR: vllm process died (PID $SERVER_PID)."
            echo "--- last 30 lines of $LOG ---"
            tail -30 "$LOG"
            exit 1
        fi
        # Check for a fatal error line in the log
        if grep -q "RuntimeError\|CUDA error\|FileNotFoundError\|EngineCore failed" "$LOG" 2>/dev/null; then
            echo "ERROR: vllm reported a fatal error."
            echo "--- last 30 lines of $LOG ---"
            tail -30 "$LOG"
            kill -9 "$SERVER_PID" 2>/dev/null || true
            exit 1
        fi
        if curl -sf "http://localhost:$PORT/v1/models" > /dev/null 2>&1; then
            echo "Server is ready at http://localhost:$PORT/v1"
            exit 0
        fi
        sleep 3
    done
    echo "ERROR: Timed out after 6 minutes waiting for server."
    echo "--- last 30 lines of $LOG ---"
    tail -30 "$LOG"
    exit 1
fi
