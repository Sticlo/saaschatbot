#!/usr/bin/env bash
# Despliegue en el servidor. Lo llama GitHub Actions por SSH solo después de que pasan las pruebas,
# con una llave que no puede ejecutar nada más (command= en authorized_keys).
set -euo pipefail

cd /opt/omitel
exec 9>/tmp/omitel-deploy.lock
flock -n 9 || { echo "Ya hay un despliegue en curso"; exit 1; }

COMPOSE=(docker compose --env-file .env -f deploy/docker-compose.prod.yml)
before=$(git rev-parse HEAD)

git fetch -q origin main
git merge -q --ff-only origin/main
echo "Desplegando $(git log -1 --oneline) (antes: ${before:0:7})"

"${COMPOSE[@]}" up -d --build

for _ in $(seq 1 30); do
  if curl -fsS -m 5 https://app.omitel.net/health >/dev/null; then
    echo "API sana"
    exit 0
  fi
  sleep 5
done
echo "La API no respondió /health en 150 s"
"${COMPOSE[@]}" ps
"${COMPOSE[@]}" logs --tail 80 api
exit 1
