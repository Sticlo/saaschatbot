# Omitel

Asistente de ventas con IA para WhatsApp, pensado para negocios en Colombia (barberías, salones, tiendas…).
El negocio vincula su WhatsApp escaneando un QR; la IA responde a los clientes, clasifica a los interesados
y agenda citas. El dueño lo ve todo en un panel web y paga una suscripción mensual con Wompi.

## Cómo está armado

| Pieza | Tecnología | Carpeta | Puerto local |
|---|---|---|---|
| API + workers (webhooks, IA, sincronización, cobros) | Python, FastAPI, SQLAlchemy, Alembic | `backend/` | 8000 |
| Panel del negocio (chats, citas, configuración) y consola de plataforma | HTML + JavaScript sin framework, lo sirve la API | `web/panel/` | 8000 (`/panel`) |
| Sitio público (landing, precios, registro, login, Mi plan) | Angular 19 | `web/site/` | 4200 |
| Conexión con WhatsApp | [Evolution API](https://github.com/EvolutionAPI/evolution-api) v2.2.3 (no oficial, basada en WhatsApp Web) | `whatsapp/evolution-api/` (no se versiona) | 8080 |
| Base de datos | PostgreSQL 16 (bases `saaschatbot` y `evolution`) | — | 5432 |
| Colas, caché y tiempo real | Redis 7 | — | 6379 |
| IA | DeepSeek (texto) y Gemini (audios, imágenes y respaldo) | — | — |

Flujo de un mensaje: WhatsApp → Evolution → webhook a la API → cola en Redis → worker de IA → respuesta por
Evolution. El panel recibe los cambios en vivo por WebSocket (`/ws/panel`).

En desarrollo los workers corren dentro del mismo proceso de la API (`EMBED_WORKERS_IN_API=true`).
En producción son procesos separados (ver [Despliegue](#ramas-y-despliegue)).

## Requisitos

- **Python 3.12** (es la versión de producción; 3.10 o superior funciona).
- **Node.js 20 o superior** (Angular 19 y Evolution lo necesitan).
- **PostgreSQL 16 y Redis 7**, con Docker o instalados con Homebrew.
- Llaves de **DeepSeek** y **Gemini** para que la IA responda. Sin ellas el panel funciona, pero la IA no contesta.
- Un celular con WhatsApp para vincular un número de pruebas. **No uses un número personal importante:**
  Evolution no es la API oficial y WhatsApp puede bloquear números con comportamiento de bot.

## Instalación (primera vez)

### 1. Clonar y crear el `.env`

```bash
git clone git@github.com:Sticlo/saaschatbot.git
cd saaschatbot
git checkout dev
cp .env.example .env
```

En `.env` llena como mínimo:

- `SECRET_KEY`: cualquier texto aleatorio largo. Genéralo con
  `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
- `POSTGRES_PASSWORD`, y la misma contraseña dentro de `DATABASE_URL` y `EVOLUTION_DATABASE_URL`.
- `DEEPSEEK_API_KEY` y `GEMINI_API_KEY`. Pídeselas al equipo o crea las tuyas.

Los secretos nunca se suben al repositorio: `.env` está en `.gitignore`.

### 2. Postgres y Redis

**Opción A, con Docker:**

```bash
docker compose -f deploy/docker-compose.yml up -d postgres redis
```

La base `evolution` se crea sola la primera vez (`deploy/postgres-init/`).

**Opción B, con Homebrew (Mac sin Docker):**

```bash
brew install postgresql@16 redis
brew services start postgresql@16
brew services start redis

# Usuario y bases (usa la misma contraseña que pusiste en .env).
# CREATEDB hace falta para que las pruebas creen su propia base saaschatbot_test.
psql postgres -c "CREATE ROLE saaschatbot LOGIN PASSWORD 'tu-password-local' CREATEDB;"
createdb -O saaschatbot saaschatbot
createdb -O saaschatbot evolution
```

### 3. Backend

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cd backend && ../.venv/bin/alembic upgrade head && cd ..
```

### 4. Evolution (WhatsApp)

**Mac o Linux sin Docker (lo que usa el equipo hoy):**

```bash
./scripts/evolution-mac.sh setup
```

La primera vez clona Evolution en `whatsapp/evolution-api/`, crea su `.env` desde
`deploy/evolution.env.example` y se detiene para que completes tres valores en
`whatsapp/evolution-api/.env`:

- `DATABASE_CONNECTION_URI`: tu contraseña de Postgres.
- `AUTHENTICATION_API_KEY`: **el mismo valor** que `EVOLUTION_API_KEY` del `.env` de la raíz.
- `CONFIG_SESSION_PHONE_VERSION`: la versión actual de WhatsApp Web. Abre
  https://web.whatsapp.com/sw.js, busca `client_revision` y escribe `2.3000.<ese número>`.

Vuelve a correr `./scripts/evolution-mac.sh setup`. Instala dependencias, crea las tablas y compila;
tarda unos minutos.

**Con Docker:** agrega `CONFIG_SESSION_PHONE_VERSION` al `.env` de la raíz, cambia
`EVOLUTION_IN_DOCKER=true` y corre:

```bash
docker compose -f deploy/docker-compose.yml --profile evolution up -d evolution
```

### 5. Sitio Angular

```bash
npm install
```

Se corre en la raíz: el `package.json` usa *workspaces* e instala también `web/site`.

## Correr el proyecto

Con todo instalado, abre dos terminales:

```bash
# Terminal 1 — WhatsApp
./scripts/evolution-mac.sh start

# Terminal 2 — API (con workers) + sitio Angular
./scripts/dev-all.sh
```

O, si prefieres ver cada log por separado, una terminal para cada uno:

```bash
./scripts/evolution-mac.sh start   # Evolution en :8080
./scripts/dev.sh                   # API en :8000 (aplica migraciones al arrancar)
./scripts/dev-site.sh              # Sitio en :4200
```

| Qué | URL |
|---|---|
| Sitio (landing, registro, login, Mi plan) | http://localhost:4200 |
| Panel del negocio | http://localhost:4200/panel (o http://localhost:8000/panel) |
| Consola de plataforma | http://localhost:8000/panel/admin |
| Documentación de la API (solo en desarrollo) | http://localhost:8000/docs |
| Estado de los servicios | http://localhost:8000/health |

Si `/health` responde `"postgres": true, "redis": true` y muestra workers de `webhook` y `ai`, el backend
está bien.

### Primer uso

1. Entra a http://localhost:4200/registro y crea una cuenta. Arranca con una prueba gratis de `TRIAL_DAYS` días.
2. Sin `RESEND_API_KEY` no se envían correos: los enlaces de acceso y de recuperación de contraseña
   se imprimen en la terminal de la API.
3. En el panel, pulsa **Vincular WhatsApp** y escanea el QR desde WhatsApp → Dispositivos vinculados.
4. Escríbele a ese número desde otro celular para ver llegar el chat y responder a la IA.
5. Para entrar a la consola de plataforma, agrega tu correo a `PLATFORM_ADMIN_EMAILS` en `.env`.

Los pagos usan las llaves **sandbox** de Wompi (`pub_test_…`); con ellas no se cobra dinero real.
Para validarlas: `.venv/bin/python scripts/configure-wompi.py --check`. Las tarjetas de prueba están en la
[documentación de Wompi](https://docs.wompi.co/docs/colombia/datos-de-prueba-en-sandbox/).

## Pruebas

```bash
cd backend
PYTHONPATH=. ../.venv/bin/pytest -q
```

- Las pruebas usan su propia base (`saaschatbot_test`, se crea y migra sola) y la base 15 de Redis.
  No tocan tus datos de desarrollo, pero Postgres y Redis tienen que estar corriendo.
- La suite completa tarda unos 12 minutos. Para una sola área:
  `PYTHONPATH=. ../.venv/bin/pytest -q tests/test_business_results.py`.
- No corras dos sesiones de pytest al mismo tiempo: comparten la base de pruebas.

Para revisar que el sitio compila: `npm run build:site`.

## Estructura del repositorio

```
backend/
  app/
    domain/entities/     Modelos SQLAlchemy (tenant, conversación, mensaje, cita, suscripción…)
    application/         Lógica de negocio por área: ai, appointments, billing, whatsapp, results…
    infrastructure/      Integraciones: Redis, correo (Resend), Evolution, IA
    presentation/        API REST (api/), WebSockets, middlewares de seguridad
    worker.py            Entrada de los workers en producción (webhook, ai, sync)
  alembic/versions/      Migraciones de la base de datos (numeradas: 001, 002…)
  tests/                 Pruebas (pytest)
web/
  panel/                 Panel del negocio y consola de plataforma (app.js, admin.js)
  site/                  Sitio público en Angular
deploy/                  Docker Compose de desarrollo y producción, Caddy y plantillas de .env
scripts/                 Scripts de desarrollo y configuración
```

### Convenciones

- **Cambios en la base de datos:** crea una migración nueva en `backend/alembic/versions/` con el siguiente
  número y aplícala con `alembic upgrade head`. Las pruebas migran su base solas.
- **Multi-negocio:** todo dato pertenece a un `tenant`. Toda consulta debe filtrar por `tenant_id`; hay pruebas
  que verifican ese aislamiento.
- **Panel:** cuando cambies `web/panel/app.js` o `styles.css`, sube el número de versión (`?v=`) en
  `web/panel/index.html` para que los navegadores no usen la copia vieja.
- **Mensajes salientes:** nada de envíos masivos ni en frío. Cuidar el número del negocio es parte del producto.

## Problemas comunes

| Síntoma | Causa y solución |
|---|---|
| "Origen no permitido" al vincular WhatsApp o guardar | El sitio corre en un puerto que el backend no acepta. En desarrollo se aceptan 4200, 4300 y 4000. Usa `SITE_PORT=4300 ./scripts/dev-site.sh` si el 4200 está ocupado. |
| El QR no aparece o la vinculación falla | Actualiza `CONFIG_SESSION_PHONE_VERSION` (ver paso 4) y reinicia Evolution. Revisa `./scripts/evolution-mac.sh status`. |
| Llegan mensajes a WhatsApp pero no al panel | Evolution no alcanza el webhook. Con Evolution nativo, `EVOLUTION_IN_DOCKER=false`; con Docker, `true`. |
| La IA no responde | Faltan `DEEPSEEK_API_KEY`/`GEMINI_API_KEY`, o la IA está apagada en la configuración del negocio dentro del panel. |
| `Puerto 8080 ocupado` | Ya hay un Evolution corriendo: `./scripts/evolution-mac.sh stop`. |
| No llegan los correos | Es normal sin `RESEND_API_KEY`: el enlace sale en la terminal de la API. |

## Ramas y despliegue

- Se trabaja en **`dev`**. **`main`** guarda versiones estables y se actualiza desde `dev`.
- **Sitio (`www.omitel.net`):** Cloudflare Workers lo compila y publica automáticamente con cada push
  (`npm run build:site` + `wrangler deploy`, configurado en `wrangler.toml`). Un push a `dev` o a `main`
  publica la landing en producción.
- **API, panel y Evolution (`app.omitel.net`):** servidor propio con Docker Compose.
  1. Copia `deploy/env.production.example` a `.env` en el servidor y llena todos los valores.
  2. Corre `docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d --build`.

  Las migraciones corren solas antes de arrancar la API, y Caddy saca el certificado HTTPS.
  Con `APP_ENV=production` la API se niega a arrancar si faltan secretos obligatorios
  (`SECRET_KEY`, `RESEND_API_KEY`, secretos de webhooks).

### Respaldos

El servicio `backup` guarda cada 24 horas una copia de las bases `saaschatbot` y `evolution` en
`deploy/backups/` del servidor y borra las de más de 14 días (`BACKUP_KEEP_DAYS`). Las fotos de los chats y
los logos viven en los volúmenes `chat_media` y `tenant_assets`.

Esas copias están en el mismo servidor: si se pierde el servidor, se pierden con él. Bájalas de vez en cuando
a otro lugar:

```bash
scp -r usuario@servidor:saaschatbot/deploy/backups ./respaldos-omitel
```

Para restaurar una copia:

```bash
docker compose --env-file .env -f deploy/docker-compose.prod.yml exec -T postgres \
  sh -c 'pg_restore --clean --if-exists -U "$POSTGRES_USER" -d saaschatbot' \
  < deploy/backups/saaschatbot-AAAA-MM-DD_HHMM.dump
```
