#!/usr/bin/env bash
# Levanta WAHA + Chatwoot (requiere Docker Desktop)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v docker >/dev/null 2>&1; then
  echo "❌ Docker no encontrado."
  echo "   Instala Docker Desktop: https://www.docker.com/products/docker-desktop/"
  echo "   O usa Evolution temporalmente: WHATSAPP_PROVIDER=evolution en .env"
  exit 1
fi

docker compose -f "$ROOT/deploy/docker-compose.yml" up -d postgres redis chatwoot chatwoot-sidekiq waha

echo "→ Esperando WAHA en :3001…"
for _ in $(seq 1 30); do
  if curl -sf -H "X-Api-Key: ${WAHA_API_KEY:-dev-waha-key-local}" http://127.0.0.1:3001/api/sessions >/dev/null 2>&1; then
    echo "✓ WAHA listo     http://localhost:3001/dashboard"
    echo "✓ Chatwoot       http://localhost:3000"
    exit 0
  fi
  sleep 2
done

echo "⚠ WAHA aún arrancando — revisa: docker logs saaschatbot_waha"
exit 1
