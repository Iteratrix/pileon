#!/usr/bin/env bash
# Labeler tournament: run every (model, prompt variant) over a judge packet
# directory, then score them all against judge consensus (and, when the study
# database is given, against the human gold).
#
#   lab/scripts/tournament.sh <packet dir> [study db] [extra gold json]
#
# Variants: a codebook prompt (v2, contrast, reason), the same with `+json`
# for structured output, or `systemone` for a decision model. Each model is
# pulled first if missing. Configs whose output already covers the packet are
# skipped, so adding a candidate and rerunning costs only the new runs.
# CONFIGS="model variant,model variant" replaces the candidate list. Chat
# variants also get the bulk labeler's direction pass (`-directed` copies).
#
# Uses its own SSH tunnel (TUNNEL_PORT, default 11436) to the Ollama host and
# unloads each model when it is done so large models do not pile up in memory.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
DIR="$1"
STUDY_DB="${2:-}"
EXTRA_GOLD="${3:-}"
PORT="${TUNNEL_PORT:-11436}"
HOST="${OLLAMA_HOST_ALIAS:-muse-mini}"
CANDIDATES=(
  "qwen3.5:27b-mlx v2" "qwen3.5:27b-mlx contrast" "qwen3.5:27b-mlx reason"
  "qwen3.6:35b-a3b v2" "qwen3.6:35b-a3b contrast" "qwen3.6:35b-a3b reason"
  "gemma4:31b v2" "gemma4:31b contrast"
  "mistral-small3.1:24b v2" "qwen3:32b v2" "llama3.3:70b v2"
  "qwen3.8:27b-mlx v2" "nemotron-3.5-lightning v2" "nemotron-3.5-bolt v2"
  "llama3.3:70b v2+json" "qwen3.8:27b-mlx v2+json"
  "clef-flash systemone" "clef systemone" "nimble systemone" "tev1 systemone"
)
if [[ -n "${CONFIGS:-}" ]]; then
  IFS=, read -ra CANDIDATES <<< "$CONFIGS"
fi
ITEMS="$(grep -c . "$DIR/packet.jsonl")"

ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -N -L "$PORT:localhost:11434" "$HOST" &
TUNNEL=$!
trap 'kill $TUNNEL 2>/dev/null || true' EXIT
for _ in $(seq 30); do curl -sf "localhost:$PORT/api/tags" >/dev/null && break; sleep 2; done

# The bulk labeler's direction pass, for chat variants whose run is complete
# and whose -directed copy is not.
direct() {
  local model="$1" variant="$2" stem
  [[ "$variant" == systemone || "$variant" == *+json ]] && return 0
  stem="$DIR/${model//:/_}+$variant"
  [[ -f "$stem.jsonl" ]] && (( $(grep -c . "$stem.jsonl") >= ITEMS )) || return 0
  [[ -f "$stem-directed.jsonl" ]] && (( $(grep -c . "$stem-directed.jsonl") >= ITEMS )) && return 0
  echo "[tournament $(date +%H:%M)] $model $variant direction pass"
  uv run python -m pileon_lab.evalset direct --dir "$DIR" \
    --ollama "http://localhost:$PORT" --model "$model" --variant "$variant" || \
    echo "[tournament] $model $variant direction pass failed; continuing"
  curl -s "localhost:$PORT/api/generate" -d "{\"model\": \"$model\", \"keep_alive\": 0}" >/dev/null || true
}

for spec in "${CANDIDATES[@]}"; do
  model="${spec% *}"
  variant="${spec#* }"
  out="$DIR/${model//:/_}+$variant.jsonl"
  if [[ -f "$out" ]] && (( $(grep -c . "$out") >= ITEMS )); then
    echo "[tournament $(date +%H:%M)] $model $variant already complete"
    direct "$model" "$variant"
    continue
  fi
  echo "[tournament $(date +%H:%M)] $model $variant"
  curl -sf "localhost:$PORT/api/pull" -d "{\"model\": \"$model\", \"stream\": false}" >/dev/null || \
    echo "[tournament] pull of $model failed; trying anyway"
  uv run python -m pileon_lab.evalset run --dir "$DIR" \
    --ollama "http://localhost:$PORT" --model "$model" --variant "$variant" || \
    echo "[tournament] $model $variant failed; continuing"
  direct "$model" "$variant"
  curl -s "localhost:$PORT/api/generate" -d "{\"model\": \"$model\", \"keep_alive\": 0}" >/dev/null || true
done

{
  echo "== judge consensus, dev split"
  uv run python -m pileon_lab.evalset score --dir "$DIR" --split dev
  echo "== judge consensus, test split"
  uv run python -m pileon_lab.evalset score --dir "$DIR" --split test
  if [[ -n "$STUDY_DB" ]]; then
    echo "== human gold (persons-only, mapping B)"
    for file in "$DIR"/*.jsonl; do
      name="$(basename "$file" .jsonl)"
      [[ "$name" == packet ]] && continue
      uv run python -m pileon_lab.judges score --study-db "$STUDY_DB" \
        ${EXTRA_GOLD:+--extra-gold "$EXTRA_GOLD"} --dir "$DIR" --judge "$file" | tail -1
    done
  fi
  echo "== AUC and F1 for hostile or worse (decision models also as probabilities)"
  uv run python -m pileon_lab.baselines --dir "$DIR" \
    ${STUDY_DB:+--study-db "$STUDY_DB"} ${EXTRA_GOLD:+--extra-gold "$EXTRA_GOLD"}
} > "$DIR/tournament.txt" 2>&1
echo "[tournament $(date +%H:%M)] done: $DIR/tournament.txt"
