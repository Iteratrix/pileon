#!/usr/bin/env bash
# Run a command with an SSH tunnel to the Ollama host open, then close it.
#   TUNNEL_PORT=11437 lab/scripts/with-tunnel.sh uv run python -m pileon_lab.label ...
set -euo pipefail
PORT="${TUNNEL_PORT:-11437}"
HOST="${OLLAMA_HOST_ALIAS:-muse-mini}"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -N -L "$PORT:localhost:11434" "$HOST" &
TUNNEL=$!
trap 'kill $TUNNEL 2>/dev/null || true' EXIT
for _ in $(seq 30); do curl -sf "localhost:$PORT/api/tags" >/dev/null && break; sleep 2; done
"$@"
