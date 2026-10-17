#!/usr/bin/env bash
# Stage C, unattended: wait for the archive replay to finish, then detect
# surges over replay + live, label a sample on the Ollama host, build network
# features, and write the judge packet. Judging, scoring and publishing are
# left for a person (or session) to do afterwards.
#
# Run as a transient user unit so it outlives the shell that started it:
#   systemd-run --user --unit pileon-stage-c --same-dir \
#     [--setenv LABEL_MODEL=... --setenv LABEL_VARIANT=...] bash deploy/stage-c.sh
# Follow with: journalctl --user -u pileon-stage-c -f
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
OLLAMA_HOST_ALIAS="${OLLAMA_HOST_ALIAS:-muse-mini}"
TUNNEL_PORT="${TUNNEL_PORT:-11435}"
REPLAY_END_SEQ="${REPLAY_END_SEQ:-27069071412}"
LABEL_MODEL="${LABEL_MODEL:-qwen3.5:27b-mlx}"
LABEL_VARIANT="${LABEL_VARIANT:-v2}"
STAMP="$(date +%Y-%m-%d)"
SURGES="$ROOT/data/surges/network-$STAMP.jsonl.zst"
LAB="$ROOT/lab"
DATA="$ROOT/data/lab"

log() { printf '[stage-c %s] %s\n' "$(date +%H:%M:%S)" "$*"; }

replay_done() {
  local state result last
  state="$(systemctl --user show pileon-replay -p ActiveState --value)"
  result="$(systemctl --user show pileon-replay -p Result --value)"
  last="$(python3 -c "import json;print(json.load(open('$ROOT/data/jetstream/replay/state.json')).get('last_seq') or 0)")"
  [[ "$state" == "inactive" && "$result" == "success" && "$last" -ge "$REPLAY_END_SEQ" ]]
}

log "waiting for the replay to finish (last_seq >= $REPLAY_END_SEQ)"
until replay_done; do sleep 600; done
log "replay finished"

log "detecting surges -> $SURGES"
"$ROOT/target/release/pileon-detect" \
  --root "$ROOT/data/jetstream/replay" --root "$ROOT/data/jetstream/live" \
  --out "$SURGES"

log "opening tunnel to $OLLAMA_HOST_ALIAS"
ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -N -L "$TUNNEL_PORT:localhost:11434" "$OLLAMA_HOST_ALIAS" &
TUNNEL=$!
trap 'kill $TUNNEL 2>/dev/null || true' EXIT
for _ in $(seq 30); do
  curl -sf "localhost:$TUNNEL_PORT/api/tags" >/dev/null && break
  sleep 2
done

cd "$LAB"
log "labeling a 500-surge sample"
uv run python -m pileon_lab.label --surges "$SURGES" --cache "$DATA/cache.db" \
  --labels "$DATA/labels.db" --sample-size 500 --per-surge 30 \
  --ollama "http://localhost:$TUNNEL_PORT" --model "$LABEL_MODEL" --variant "$LABEL_VARIANT"

log "building network features"
uv run python -m pileon_lab.network --surges "$SURGES" --cache "$DATA/cache.db" \
  --labels "$DATA/labels.db" --labeler "$LABEL_MODEL+$LABEL_VARIANT" --enrich \
  --out "$DATA/network-features.jsonl"

log "writing the judge packet"
uv run python -m pileon_lab.evalset packet --surges "$SURGES" --cache "$DATA/cache.db" \
  --labels "$DATA/labels.db" --labeler "$LABEL_MODEL+$LABEL_VARIANT" --size 300 \
  --dir "$DATA/eval-net"

log "done: $SURGES, $DATA/network-features.jsonl, $DATA/eval-net/packet.jsonl"
