#!/usr/bin/env bash
# Prepara la base de datos de Chatwoot (solo la primera vez).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "❌ Docker requerido para Chatwoot"
  exit 1
fi

echo "→ Levantando Postgres + Redis…"
docker compose -f deploy/docker-compose.yml up -d postgres redis

echo "→ Preparando schema Chatwoot (puede tardar ~1 min)…"
docker compose -f deploy/docker-compose.yml run --rm chatwoot bundle exec rails db:chatwoot_prepare

echo "✓ Chatwoot listo. Arranca todo con:"
echo "    docker compose -f deploy/docker-compose.yml up -d"
echo "→ UI: http://localhost:3000 (crea admin y copia el API token a CHATWOOT_API_TOKEN en .env)"
