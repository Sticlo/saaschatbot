#!/bin/sh
# Respaldo diario de las bases de Omitel y Evolution (formato custom de pg_dump, ya comprimido).
# Restaurar: pg_restore --clean --if-exists -d <base> <archivo>.dump
set -eu

DIR="${BACKUP_DIR:-/backups}"
KEEP="${BACKUP_KEEP_DAYS:-14}"
DATABASES="${BACKUP_DATABASES:-${POSTGRES_DB:-saaschatbot} evolution}"

mkdir -p "$DIR"
while true; do
  stamp=$(date -u +%Y-%m-%d_%H%M)
  for db in $DATABASES; do
    tmp="$DIR/.$db-$stamp.dump"
    if pg_dump -Fc -d "$db" -f "$tmp"; then
      mv "$tmp" "$DIR/$db-$stamp.dump"
      echo "Respaldo listo: $db-$stamp.dump"
    else
      rm -f "$tmp"
      echo "Falló el respaldo de $db" >&2
    fi
  done
  find "$DIR" -name '*.dump' -mtime +"$KEEP" -delete
  [ -n "${BACKUP_ONCE:-}" ] && exit 0
  sleep 86400
done
