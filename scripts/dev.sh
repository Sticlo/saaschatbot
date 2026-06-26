#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
cd "$BACKEND"

if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "→ Creado .env desde .env.example (edita POSTGRES_PASSWORD antes de producción)"
fi

if command -v docker >/dev/null 2>&1; then
  docker compose -f "$ROOT/deploy/docker-compose.yml" up -d
  echo "→ Postgres + Redis + Chatwoot + WAHA levantados (Evolution opcional: --profile evolution)"
else
  echo "⚠ Docker no encontrado. Asegúrate de tener Postgres y Redis corriendo."
  echo "  brew services start postgresql@16 redis"
  echo "  Evolution (Mac sin Docker): ./scripts/evolution-mac.sh start"
fi

if [ ! -d "$ROOT/.venv" ]; then
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -r "$BACKEND/requirements.txt"
fi

"$ROOT/.venv/bin/alembic" upgrade head
echo "→ Migraciones aplicadas"

echo "→ Landing  http://localhost:4200  (./scripts/dev-site.sh)"
echo "→ Panel    http://localhost:8000/panel"
echo "→ API docs http://localhost:8000/docs"
echo "→ Workers embebidos (dev). Producción: docker compose -f deploy/docker-compose.prod.yml up"
export EMBED_WORKERS_IN_API=true
export PYTHONPATH="$BACKEND"
exec "$ROOT/.venv/bin/uvicorn" app.presentation.main:app --reload \
  --host 0.0.0.0 --port 8000 \
  --timeout-graceful-shutdown 2 \
  --reload-exclude 'alembic/*' \
  --reload-exclude '.venv/*'
