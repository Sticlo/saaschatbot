#!/usr/bin/env bash
# Arranca workers en procesos separados (modo producción local).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
cd "$BACKEND"

if [ ! -d "$ROOT/.venv" ]; then
  echo "Ejecuta primero: ./scripts/dev.sh (crea .venv)"
  exit 1
fi

export EMBED_WORKERS_IN_API=false
export PYTHONPATH="$BACKEND"
PY="$ROOT/.venv/bin/python"

WEBHOOK_N="${WORKER_WEBHOOK_N:-2}"
OUTBOUND_N="${WORKER_OUTBOUND_N:-3}"
AI_N="${WORKER_AI_N:-2}"

pids=()
cleanup() {
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

for i in $(seq 1 "$WEBHOOK_N"); do
  WORKER_ID="webhook-$i" $PY -m app.worker webhook &
  pids+=($!)
done

for i in $(seq 1 "$OUTBOUND_N"); do
  WORKER_ID="outbound-$i" $PY -m app.worker outbound &
  pids+=($!)
done

for i in $(seq 1 "$AI_N"); do
  extra=""
  if [ "$i" = "1" ]; then
    extra="--recover-ai"
  fi
  WORKER_ID="ai-$i" $PY -m app.worker ai $extra &
  pids+=($!)
done

echo "→ Workers: webhook×$WEBHOOK_N outbound×$OUTBOUND_N ai×$AI_N (EMBED_WORKERS_IN_API=false)"
echo "→ Arranca la API aparte: cd backend && EMBED_WORKERS_IN_API=false ../.venv/bin/uvicorn app.presentation.main:app --host 0.0.0.0 --port 8000"
wait
