#!/usr/bin/env bash
# Landing Angular SSR (desarrollo con proxy al backend en :8000)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SITE="$ROOT/web/site"

if [ ! -d "$SITE/node_modules" ]; then
  echo "→ Instalando dependencias de web/site…"
  npm --prefix "$SITE" install
fi

echo "→ Site Angular: http://localhost:4200"
echo "→ Backend API esperado en http://127.0.0.1:8000 (./scripts/dev.sh en otra terminal)"
exec npm --prefix "$SITE" run start -- --host 0.0.0.0 --port 4200
