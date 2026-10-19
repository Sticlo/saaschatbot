#!/bin/sh
# Respaldo diario de Omitel: bases (pg_dump custom, ya comprimido), fotos/catálogos/archivos de chats,
# prueba de restauración y copia cifrada fuera del servidor (si hay almacenamiento S3 configurado).
# Restaurar una base: pg_restore --clean --if-exists -d <base> <archivo>.dump
# Restaurar archivos: tar xzf files-<fecha>.tar.gz -C <destino>
# Bajar la copia externa: ver deploy/RESTAURAR.md
set -u

DIR="${BACKUP_DIR:-/backups}"
KEEP="${BACKUP_KEEP_DAYS:-14}"
OFFSITE_KEEP="${BACKUP_OFFSITE_KEEP_DAYS:-30}"
MAIN_DB="${POSTGRES_DB:-saaschatbot}"
DATABASES="${BACKUP_DATABASES:-$MAIN_DB evolution}"
FILES_ROOT="${BACKUP_FILES_ROOT:-/data}"
STATUS="$DIR/status.env"
CHECK_DB="omitel_restore_check"

mkdir -p "$DIR"

# Lo que lee el vigilante de la API: segundos Unix del último paso que salió bien.
set_status() {
  touch "$STATUS"
  grep -v "^$1=" "$STATUS" > "$STATUS.tmp" || true
  echo "$1=$2" >> "$STATUS.tmp"
  mv "$STATUS.tmp" "$STATUS"
}

offsite_enabled() {
  [ -n "${BACKUP_S3_BUCKET:-}" ] && [ -n "${BACKUP_S3_ACCESS_KEY_ID:-}" ] \
    && [ -n "${BACKUP_S3_SECRET_ACCESS_KEY:-}" ] && [ -n "${BACKUP_ENCRYPTION_KEY:-}" ]
}

setup_offsite() {
  command -v rclone >/dev/null 2>&1 || apk add --no-cache rclone >/dev/null 2>&1 || return 1
  export RCLONE_CONFIG_STORE_TYPE=s3
  export RCLONE_CONFIG_STORE_PROVIDER="${BACKUP_S3_PROVIDER:-Other}"
  export RCLONE_CONFIG_STORE_ENDPOINT="${BACKUP_S3_ENDPOINT:-}"
  export RCLONE_CONFIG_STORE_REGION="${BACKUP_S3_REGION:-auto}"
  export RCLONE_CONFIG_STORE_ACCESS_KEY_ID="$BACKUP_S3_ACCESS_KEY_ID"
  export RCLONE_CONFIG_STORE_SECRET_ACCESS_KEY="$BACKUP_S3_SECRET_ACCESS_KEY"
  export RCLONE_CONFIG_STORE_NO_CHECK_BUCKET=true
  # Cifrado del lado de Omitel: el proveedor solo guarda bytes ilegibles.
  export RCLONE_CONFIG_OFFSITE_TYPE=crypt
  export RCLONE_CONFIG_OFFSITE_REMOTE="store:$BACKUP_S3_BUCKET/omitel"
  export RCLONE_CONFIG_OFFSITE_FILENAME_ENCRYPTION=off
  export RCLONE_CONFIG_OFFSITE_DIRECTORY_NAME_ENCRYPTION=false
  RCLONE_CONFIG_OFFSITE_PASSWORD=$(rclone obscure "$BACKUP_ENCRYPTION_KEY") || return 1
  export RCLONE_CONFIG_OFFSITE_PASSWORD
}

dump_databases() {
  ok=1
  for db in $DATABASES; do
    tmp="$DIR/.$db-$stamp.dump"
    if pg_dump -Fc -d "$db" -f "$tmp"; then
      mv "$tmp" "$DIR/$db-$stamp.dump"
      echo "Respaldo listo: $db-$stamp.dump"
    else
      rm -f "$tmp"
      echo "Falló el respaldo de $db" >&2
      ok=0
    fi
  done
  [ "$ok" = 1 ]
}

archive_files() {
  [ -d "$FILES_ROOT" ] || return 0
  tmp="$DIR/.files-$stamp.tar.gz"
  if tar czf "$tmp" -C "$FILES_ROOT" . 2>/dev/null; then
    mv "$tmp" "$DIR/files-$stamp.tar.gz"
    echo "Respaldo listo: files-$stamp.tar.gz"
  else
    rm -f "$tmp"
    echo "Falló el respaldo de fotos y archivos" >&2
    return 1
  fi
}

# Una copia que nunca se restauró no es una copia: se carga en una base temporal y se revisa.
check_restore() {
  dump="$DIR/$MAIN_DB-$stamp.dump"
  [ -f "$dump" ] || return 1
  dropdb --if-exists "$CHECK_DB" >/dev/null 2>&1
  createdb "$CHECK_DB" || return 1
  result=1
  if pg_restore --no-owner --no-privileges -d "$CHECK_DB" "$dump" >/dev/null 2>&1; then
    tables=$(psql -d "$CHECK_DB" -tAc "select count(*) from information_schema.tables where table_schema='public'")
    tenants=$(psql -d "$CHECK_DB" -tAc "select count(*) from tenants" 2>/dev/null || echo 0)
    if [ "${tables:-0}" -ge 10 ] && [ "${tenants:-0}" -ge 1 ]; then
      echo "Restauración probada: $tables tablas, $tenants negocios"
      result=0
    else
      echo "La copia restaurada está incompleta ($tables tablas, $tenants negocios)" >&2
    fi
  else
    echo "pg_restore falló con $dump" >&2
  fi
  dropdb --if-exists "$CHECK_DB" >/dev/null 2>&1
  return $result
}

upload_offsite() {
  rclone copy "$DIR" offsite: --include "*-$stamp.*" --retries 3 --low-level-retries 5 || return 1
  rclone delete offsite: --min-age "${OFFSITE_KEEP}d" --include "*.dump" --include "*.tar.gz" >/dev/null 2>&1 || true
  echo "Copia externa subida (cifrada)"
}

while true; do
  stamp=$(date -u +%Y-%m-%d_%H%M)
  dump_databases && set_status LAST_DUMP_OK "$(date +%s)"
  archive_files && set_status LAST_FILES_OK "$(date +%s)"
  check_restore && set_status LAST_RESTORE_OK "$(date +%s)"
  if offsite_enabled; then
    set_status OFFSITE_ENABLED 1
    if setup_offsite && upload_offsite; then
      set_status LAST_OFFSITE_OK "$(date +%s)"
    else
      echo "Falló la copia externa" >&2
    fi
  else
    set_status OFFSITE_ENABLED 0
  fi
  find "$DIR" \( -name '*.dump' -o -name '*.tar.gz' \) -mtime +"$KEEP" -delete
  [ -n "${BACKUP_ONCE:-}" ] && exit 0
  sleep 86400
done
