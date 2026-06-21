#!/usr/bin/env bash
# Evolution API nativo en Mac (sin Docker) — Node + Redis + Postgres (Homebrew)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EVO_DIR="$ROOT/services/evolution-api"
CMD="${1:-start}"

require_evo_dir() {
  if [ ! -d "$EVO_DIR" ]; then
    echo "→ Clonando Evolution API v2.2.3…"
    mkdir -p "$ROOT/services"
    git clone --depth 1 --branch 2.2.3 https://github.com/EvolutionAPI/evolution-api.git "$EVO_DIR"
  fi
}

setup() {
  require_evo_dir
  cd "$EVO_DIR"

  if [ ! -f .env ]; then
    echo "❌ Falta $EVO_DIR/.env — ejecuta setup desde el repo o copia la config del README."
    exit 1
  fi

  if [ ! -d node_modules ]; then
    echo "→ npm install (puede tardar ~2 min)…"
    npm install
  fi

  if [ ! -d dist ]; then
    echo "→ Generando DB y build…"
    npm run db:generate
    npm run db:deploy
    npm run build
  fi

  echo "✓ Evolution listo en $EVO_DIR"
}

start() {
  setup
  if lsof -ti:8080 >/dev/null 2>&1; then
    echo "⚠ Puerto 8080 ocupado. Detén el proceso o usa: $0 stop"
    lsof -i:8080
    exit 1
  fi
  echo "→ Evolution API en http://localhost:8080"
  cd "$EVO_DIR"
  exec npm run start:prod
}

stop() {
  pkill -f "node dist/main" 2>/dev/null || true
  echo "→ Evolution detenido"
}

status() {
  if curl -sf http://localhost:8080/ >/dev/null; then
    echo "✓ Evolution respondiendo en :8080"
    curl -s http://localhost:8080/ | head -c 120
    echo ""
  else
    echo "✗ Evolution no responde en :8080"
    exit 1
  fi
}

case "$CMD" in
  setup) setup ;;
  start) start ;;
  stop) stop ;;
  status) status ;;
  *)
    echo "Uso: $0 {setup|start|stop|status}"
    exit 1
    ;;
esac
