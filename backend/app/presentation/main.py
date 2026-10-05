from __future__ import annotations

import hmac
import html
import ipaddress
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.application.monitoring.dev_alerts import alert_dev, install_log_alert_handler
from app.application.monitoring.health_watchdog import start_health_watchdog, stop_health_watchdog

from app.presentation.security_middleware import (
    BodySizeLimitMiddleware,
    OriginGuardMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    SessionRefreshMiddleware,
)
from app.shared.core.rate_limit import client_ip

from app.presentation.websockets.panel_ws import router as panel_ws_router
from app.presentation.api.router import api_router
from app.presentation.api.webhooks import router as webhooks_router
from app.presentation.api.chatwoot_webhooks import router as chatwoot_webhooks_router
from app.config import settings, ALLOWED_DEEPSEEK_MODEL
from app.infrastructure.persistence.database import SessionLocal
from app.infrastructure.cache.redis_client import get_redis, redis_ping
from app.application.billing.plan_service import ensure_default_plan


def _resolve_panel_dir() -> Path:
    """Encuentra web/panel subiendo desde este archivo (robusto tras mover carpetas)."""
    here = Path(__file__).resolve()
    for base in (here.parent, *here.parents):
        panel_dir = base / "web" / "panel"
        if panel_dir.is_dir():
            return panel_dir
    return here.parents[3] / "web" / "panel"


PANEL_DIR = _resolve_panel_dir()


def _ensure_webhooks_for_connected_sessions() -> None:
    """Re-registra webhook Evolution al arrancar (omitido con WAHA)."""
    from app.application.whatsapp.whatsapp_gateway import uses_waha

    if uses_waha():
        return
    try:
        from app.domain.entities import Tenant, WhatsAppSession
        from app.domain.entities.enums import WhatsAppStatus
        from app.application.whatsapp.whatsapp_service import ensure_evolution_webhook

        with SessionLocal() as db:
            rows = (
                db.query(Tenant, WhatsAppSession)
                .join(WhatsAppSession, WhatsAppSession.tenant_id == Tenant.id)
                .filter(Tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value)
                .all()
            )
            for tenant, session in rows:
                ensure_evolution_webhook(session, tenant.id)
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        with SessionLocal() as db:
            ensure_default_plan(db)
    except Exception:
        pass

    start_health_watchdog()
    if settings.embed_workers_in_api:
        from app.application.ai.ai_queue_service import (
            recover_pending_ai_replies,
            start_ai_worker,
            stop_ai_worker,
        )
        from app.application.workers.queue_service import start_webhook_worker, stop_webhook_worker
        from app.application.sync.sync_queue_service import start_sync_worker, stop_sync_worker
        from app.application.sync.live_pull_scheduler import (
            start_live_pull_scheduler,
            stop_live_pull_scheduler,
        )
        from app.application.chatwoot.chatwoot_service import chatwoot_sync_mode
        from app.application.billing.subscription_sweeper import (
            start_subscription_sweeper,
            stop_subscription_sweeper,
        )

        start_webhook_worker()
        start_ai_worker()
        start_sync_worker()
        start_subscription_sweeper()
        if not chatwoot_sync_mode():
            start_live_pull_scheduler()
        recover_pending_ai_replies()
        _ensure_webhooks_for_connected_sessions()
        yield
        if not chatwoot_sync_mode():
            stop_live_pull_scheduler()
        stop_subscription_sweeper()
        stop_ai_worker()
        stop_webhook_worker()
        stop_sync_worker()
    else:
        from app.application.sync.live_pull_scheduler import (
            start_live_pull_scheduler,
            stop_live_pull_scheduler,
        )

        from app.application.chatwoot.chatwoot_service import chatwoot_sync_mode

        if settings.app_env.lower() in ("development", "dev", "local"):
            if not chatwoot_sync_mode():
                start_live_pull_scheduler()
            _ensure_webhooks_for_connected_sessions()
        else:
            # Tras un deploy, cada instancia queda con su clave de webhook vigente (lock por negocio).
            threading.Thread(
                target=_ensure_webhooks_for_connected_sessions, name="webhook-ensure", daemon=True
            ).start()
        yield
        if settings.app_env.lower() in ("development", "dev", "local"):
            if not chatwoot_sync_mode():
                stop_live_pull_scheduler()

    stop_health_watchdog()
    try:
        get_redis().close()
    except Exception:
        pass


def _init_sentry() -> None:
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.app_env,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            # Nunca enviar cuerpos, cookies ni cabeceras con tokens a un tercero.
            send_default_pii=False,
            max_request_body_size="never",
        )
    except Exception:
        logging.getLogger(__name__).exception("No se pudo iniciar Sentry")


_init_sentry()
install_log_alert_handler()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
)

# El último en agregarse es el más externo: CORS envuelve todo para que hasta un 429 sea legible.
if settings.is_production():
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts())
app.add_middleware(SessionRefreshMiddleware)
app.add_middleware(OriginGuardMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Requested-With"],
    max_age=600,
)

@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """El usuario ve un mensaje tranquilo; el dev recibe el detalle técnico."""
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    await run_in_threadpool(
        alert_dev,
        f"http500:{request.method}:{path}:{type(exc).__name__}",
        f"Error 500 en {request.method} {path}",
        f"{type(exc).__name__}: {str(exc)[:400]}",
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Tuvimos un inconveniente momentáneo. Intenta de nuevo en unos segundos."},
    )


app.include_router(api_router)
app.include_router(webhooks_router)
app.include_router(chatwoot_webhooks_router)
app.include_router(panel_ws_router)

# Monorepo: panel estático; la landing principal vive en web/site (Angular SSR)
if PANEL_DIR.is_dir():
    app.mount("/static/panel", StaticFiles(directory=str(PANEL_DIR)), name="panel-static")


@app.get("/")
def api_root():
    return {
        "app": settings.app_name,
        "panel": "/panel",
        "health": "/health",
        "api": "/api/v1",
        "site": "Angular SSR en web/site (puerto 4200 dev / 4000 prod)",
    }


def _panel_csp(request: Request) -> str:
    host = request.headers.get("host") or ""
    ws = f"wss://{host}" if settings.is_production() else f"ws://{host} wss://{host}"
    return (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; media-src 'self' data: blob:; font-src 'self'; "
        f"connect-src 'self' {ws}; object-src 'none'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    )


@app.get("/panel")
def panel(request: Request):
    index = PANEL_DIR / "index.html"
    if not index.is_file():
        return {"detail": "Panel no disponible"}
    # El panel puede vivir en otro dominio que la landing: le decimos dónde están login y precios.
    # Va en un <meta> (no en <script> en línea) para no abrir la CSP.
    site = html.escape(settings.site_public_url.rstrip("/"), quote=True)
    body = index.read_text(encoding="utf-8").replace(
        "</head>", f'<meta name="omitel-site-url" content="{site}" />\n</head>', 1
    )
    return HTMLResponse(
        body,
        headers={"Content-Security-Policy": _panel_csp(request), "Cache-Control": "no-cache"},
    )


@app.get("/panel/admin")
def panel_admin(request: Request):
    # La página es pública pero vacía: todos los datos salen de /api/v1/platform, que sí exige superadmin.
    page = PANEL_DIR / "admin.html"
    if not page.is_file():
        return {"detail": "Consola no disponible"}
    return HTMLResponse(
        page.read_text(encoding="utf-8"),
        headers={
            "Content-Security-Policy": _panel_csp(request),
            "Cache-Control": "no-cache",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


def _is_internal_caller(request: Request) -> bool:
    """Healthcheck de Docker/monitor interno, o quien presente HEALTH_TOKEN."""
    token = settings.health_token.strip()
    provided = request.headers.get("x-health-token") or ""
    if token and provided and hmac.compare_digest(token, provided):
        return True
    try:
        ip = ipaddress.ip_address(client_ip(request))
    except ValueError:
        return False
    # Solo loopback: detrás de Caddy una IP privada podría ser el propio proxy reenviando a un externo.
    return ip.is_loopback


_PUBLIC_HEALTH_TTL_SECONDS = 5.0
_public_health_cache: dict[str, float | bool] = {"at": 0.0, "ok": True}


def _core_services_ok() -> bool:
    """Postgres + Redis, cacheado unos segundos: /health no tiene rate limit."""
    now = time.monotonic()
    if now - float(_public_health_cache["at"]) < _PUBLIC_HEALTH_TTL_SECONDS:
        return bool(_public_health_cache["ok"])
    ok = redis_ping()
    if ok:
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
        except Exception:
            ok = False
    _public_health_cache.update(at=now, ok=ok)
    return ok


@app.get("/health")
def health(request: Request):
    if not _is_internal_caller(request):
        # Al público solo «vivo o no»: sin colas, versiones ni entorno (reconocimiento).
        # 503 si algo esencial cayó, para que un monitor externo (UptimeRobot) avise.
        if _core_services_ok():
            return {"status": "ok"}
        return JSONResponse(status_code=503, content={"status": "degraded"})
    return _health_details()


def _health_details():
    db_ok = False
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            db_ok = True
    except Exception:
        db_ok = False

    redis_ok = redis_ping()
    queue_depth = 0
    ai_queue_depth = 0
    workers_webhook = 0
    workers_ai = 0
    ai_slots_active = 0
    try:
        from app.application.workers.queue_service import INBOUND_WEBHOOK_QUEUE
        from app.application.ai.ai_queue_service import AI_REPLY_QUEUE, ai_worker_is_alive
        from app.application.workers.worker_runtime import count_active_ai_slots, count_active_workers
        from app.infrastructure.cache.redis_client import get_redis

        r = get_redis()
        queue_depth = r.llen(INBOUND_WEBHOOK_QUEUE)
        ai_queue_depth = r.llen(AI_REPLY_QUEUE)
        workers_webhook = count_active_workers("webhook")
        workers_ai = count_active_workers("ai")
        if workers_webhook == 0 and settings.embed_workers_in_api:
            from app.application.workers.queue_service import webhook_worker_is_alive

            workers_webhook = 1 if webhook_worker_is_alive() else 0
        if workers_ai == 0 and settings.embed_workers_in_api:
            workers_ai = 1 if ai_worker_is_alive() else 0
        ai_slots_active = count_active_ai_slots()
    except Exception:
        pass

    status_label = "ok" if db_ok and redis_ok else "degraded"
    return {
        "status": status_label,
        "postgres": db_ok,
        "redis": redis_ok,
        "workers_embedded": settings.embed_workers_in_api,
        "workers": {
            "webhook": workers_webhook,
            "ai": workers_ai,
        },
        "webhook_queue_depth": queue_depth,
        "ai_queue_depth": ai_queue_depth,
        "ai_slots_active": ai_slots_active,
        "ai_max_parallel": settings.ai_max_parallel_jobs,
        "env": settings.app_env,
    }


@app.get("/health/deepseek")
def health_deepseek(request: Request):
    """Diagnóstico de DeepSeek. Cada llamada gasta una petición real: solo red interna o token."""
    if not settings.debug and not _is_internal_caller(request):
        raise HTTPException(status_code=404, detail="Not Found")
    from app.infrastructure.ai.deepseek_client import check_provider_health, is_configured

    if not is_configured():
        return {
            "ok": False,
            "configured": False,
            "error": "DEEPSEEK_API_KEY no configurada en .env",
            "model": ALLOWED_DEEPSEEK_MODEL,
        }
    ok, err = check_provider_health()
    return {
        "ok": ok,
        "configured": True,
        "error": err,
        "model": ALLOWED_DEEPSEEK_MODEL,
        "api_base": settings.deepseek_api_base,
    }
