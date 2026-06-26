#!/usr/bin/env bash
# Backend + landing Angular en paralelo (desarrollo local)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

PIDS=()

if command -v docker >/dev/null 2>&1; then
  docker compose -f "$ROOT/deploy/docker-compose.yml" up -d
else
  echo "→ Docker no encontrado — Postgres/Redis: brew services start postgresql@16 redis"
  if ! curl -sf http://127.0.0.1:8080/ >/dev/null 2>&1; then
    if [ -x "$ROOT/scripts/evolution-mac.sh" ]; then
      echo "→ Evolution no responde en :8080 — inicia en otra terminal:"
      echo "    ./scripts/evolution-mac.sh start"
    fi
  else
    echo "→ Evolution ya responde en http://localhost:8080"
  fi
fi

export EMBED_WORKERS_IN_API=true
export PYTHONPATH="$BACKEND"
(
  cd "$BACKEND"
  "$ROOT/.venv/bin/uvicorn" app.presentation.main:app --reload \
    --host 0.0.0.0 --port 8000 \
    --timeout-graceful-shutdown 2 \
    --reload-exclude 'alembic/*' \
    --reload-exclude '.venv/*'
) &
PIDS+=($!)

echo "→ Esperando API en :8000…"
if lsof -ti:8000 >/dev/null 2>&1; then
  echo "⚠ Puerto 8000 ocupado. Deteniendo uvicorn anterior…"
  lsof -ti:8000 | xargs kill -9 2>/dev/null || true
  sleep 1
fi
if lsof -ti:8000 >/dev/null 2>&1; then
  echo "❌ No se pudo liberar :8000. Ejecuta manualmente:"
  echo "    kill -9 \$(lsof -ti:8000)"
  exit 1
fi
for _ in $(seq 1 40); do
  if curl -sf "http://127.0.0.1:8000/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

"$ROOT/scripts/dev-site.sh" &
PIDS+=($!)

echo "→ Landing: http://localhost:4200"
echo "→ Panel:   http://localhost:4200/panel (proxy al backend)"
wait
