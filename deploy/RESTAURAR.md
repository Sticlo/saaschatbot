# Restaurar Omitel desde un respaldo

El servicio `backup` deja cada día en `deploy/backups/`:

- `saaschatbot-<fecha>.dump`: base principal (negocios, chats, productos, citas…).
- `evolution-<fecha>.dump`: sesiones de WhatsApp.
- `files-<fecha>.tar.gz`: fotos de productos, catálogos y archivos de los chats.
- `status.env`: cuándo salió bien cada paso (lo vigila la API y avisa si falla).

Cada copia de la base principal se restaura sola en una base temporal para comprobar que sirve.
Si `.env` trae `BACKUP_S3_*` y `BACKUP_ENCRYPTION_KEY`, también se sube cifrada a `<bucket>/omitel/`
y allá se conservan 30 días.

## 1. Bajar la copia externa (si el servidor ya no existe)

En el servidor nuevo, con el repo clonado y el `.env` restaurado (incluida `BACKUP_ENCRYPTION_KEY`):

```sh
cd /opt/omitel/deploy && mkdir -p backups
docker compose --env-file ../.env -f docker-compose.prod.yml run --rm --entrypoint sh backup -c '
  apk add --no-cache rclone >/dev/null &&
  export RCLONE_CONFIG_STORE_TYPE=s3 RCLONE_CONFIG_STORE_PROVIDER="$BACKUP_S3_PROVIDER" \
    RCLONE_CONFIG_STORE_ENDPOINT="$BACKUP_S3_ENDPOINT" RCLONE_CONFIG_STORE_REGION="$BACKUP_S3_REGION" \
    RCLONE_CONFIG_STORE_ACCESS_KEY_ID="$BACKUP_S3_ACCESS_KEY_ID" \
    RCLONE_CONFIG_STORE_SECRET_ACCESS_KEY="$BACKUP_S3_SECRET_ACCESS_KEY" \
    RCLONE_CONFIG_OFFSITE_TYPE=crypt RCLONE_CONFIG_OFFSITE_REMOTE="store:$BACKUP_S3_BUCKET/omitel" \
    RCLONE_CONFIG_OFFSITE_FILENAME_ENCRYPTION=off RCLONE_CONFIG_OFFSITE_DIRECTORY_NAME_ENCRYPTION=false \
    RCLONE_CONFIG_OFFSITE_PASSWORD="$(rclone obscure "$BACKUP_ENCRYPTION_KEY")" &&
  rclone ls offsite: && rclone copy offsite: /backups --max-age 2d'
```

## 2. Restaurar las bases

Con `postgres` arriba y la API y los workers apagados:

```sh
cd /opt/omitel
docker compose --env-file .env -f deploy/docker-compose.prod.yml stop api worker-webhook worker-ai worker-ai-primary worker-sync evolution
docker compose --env-file .env -f deploy/docker-compose.prod.yml run --rm --entrypoint sh backup -c '
  pg_restore --clean --if-exists --no-owner -d "$POSTGRES_DB" /backups/saaschatbot-<fecha>.dump;
  pg_restore --clean --if-exists --no-owner -d evolution /backups/evolution-<fecha>.dump'
```

## 3. Restaurar fotos y archivos

```sh
docker compose --env-file .env -f deploy/docker-compose.prod.yml run --rm --entrypoint sh \
  -v deploy_tenant_assets:/restore/assets -v deploy_chat_media:/restore/media backup -c '
  tar xzf /backups/files-<fecha>.tar.gz -C /restore'
```

## 4. Levantar todo

```sh
docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d
curl -s https://app.omitel.net/health
```
