from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.presentation.websockets.panel_ws import router as panel_ws_router
from app.presentation.api.router import api_router
from app.presentation.api.webhooks import router as webhooks_router
from app.config import settings
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        with SessionLocal() as db:
            ensure_default_plan(db)
    except Exception:
        pass

    if settings.embed_workers_in_api:
        from app.application.ai.ai_queue_service import (
            recover_pending_ai_replies,
            start_ai_worker,
            stop_ai_worker,
        )
        from app.application.outbound.bait_scheduler import start_outbound_worker, stop_outbound_worker
        from app.application.workers.queue_service import start_webhook_worker, stop_webhook_worker
        from app.application.sync.sync_queue_service import start_sync_worker, stop_sync_worker

        start_webhook_worker()
        start_outbound_worker()
        start_ai_worker()
        start_sync_worker()
        recover_pending_ai_replies()
        yield
        stop_ai_worker()
        stop_outbound_worker()
        stop_webhook_worker()
        stop_sync_worker()
    else:
        yield

    try:
        get_redis().close()
    except Exception:
        pass


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=(
        [
            "http://localhost:4200",
            "http://127.0.0.1:4200",
            "http://localhost:4000",
            "http://127.0.0.1:4000",
        ]
        if settings.debug
        else []
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(webhooks_router)
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


@app.get("/panel")
def panel():
    index = PANEL_DIR / "index.html"
    if not index.is_file():
        return {"detail": "Panel no disponible"}
    return FileResponse(index)


@app.get("/health")
def health():
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
    workers_outbound = 0
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
        workers_outbound = count_active_workers("outbound")
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
            "outbound": workers_outbound,
            "ai": workers_ai,
        },
        "webhook_queue_depth": queue_depth,
        "ai_queue_depth": ai_queue_depth,
        "ai_slots_active": ai_slots_active,
        "ai_max_parallel": settings.ai_max_parallel_jobs,
        "env": settings.app_env,
    }
