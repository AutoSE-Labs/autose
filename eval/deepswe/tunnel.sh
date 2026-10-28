#!/usr/bin/env bash
# Expose a vLLM server on a turing GPU node to DeepSWE task containers on this host.
#
#   task container -> Pier egress proxy (squid, ports 80/443 only)
#     -> http://172.17.0.1:80 (socat on docker0) -> 127.0.0.1:18000
#     -> ssh -L through turing -> <node>:8000 (vLLM)
#
# Usage: tunnel.sh [node] (default node08). Runs in the foreground; put it in tmux.
# The SSH key is forwarding-only on turing (restrict,permitopen=...).

set -euo pipefail

NODE="${1:-node08}"
LOCAL_PORT=18000
BRIDGE_IP="$(ip -4 -o addr show docker0 | awk '{print $4}' | cut -d/ -f1)"
KEY="$HOME/.ssh/turing_vllm_tunnel"

docker rm -f autose-vllm-bridge >/dev/null 2>&1 || true
docker run -d --name autose-vllm-bridge --restart unless-stopped --network host \
    alpine/socat:1.8.0.3 \
    "TCP-LISTEN:80,bind=$BRIDGE_IP,fork,reuseaddr" "TCP:127.0.0.1:$LOCAL_PORT" >/dev/null
echo "socat: $BRIDGE_IP:80 -> 127.0.0.1:$LOCAL_PORT"

while true; do
    echo "$(date '+%F %T') ssh -L $LOCAL_PORT -> $NODE:8000"
    ssh -i "$KEY" -o BatchMode=yes -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
        -N -L "127.0.0.1:$LOCAL_PORT:$NODE:8000" arihant.tripathy@turing.iiit.ac.in || true
    sleep 5
done
