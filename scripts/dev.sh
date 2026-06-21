#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "→ Creado .env desde .env.example (edita POSTGRES_PASSWORD antes de producción)"
fi

if command -v docker >/dev/null 2>&1; then
  docker compose up -d
  echo "→ Postgres + Redis levantados"
else
  echo "⚠ Docker no encontrado. Asegúrate de tener Postgres y Redis corriendo."
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi

.venv/bin/alembic upgrade head
echo "→ Migraciones aplicadas"

echo "→ API en http://localhost:8000/docs"
exec .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
