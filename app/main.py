from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.panel_ws import router as panel_ws_router
from app.api.router import api_router
from app.api.webhooks import router as webhooks_router
from app.config import settings
from app.database import SessionLocal
from app.redis_client import get_redis, redis_ping
from app.services.plan_service import ensure_default_plan
from app.services.queue_service import start_webhook_worker, stop_webhook_worker


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        with SessionLocal() as db:
            ensure_default_plan(db)
    except Exception:
        pass
    start_webhook_worker()
    yield
    stop_webhook_worker()
    try:
        get_redis().close()
    except Exception:
        pass


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.debug else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(webhooks_router)
app.include_router(panel_ws_router)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def root():
    return {
        "app": settings.app_name,
        "docs": "/docs",
        "health": "/health",
        "panel": "/panel",
        "api": "/api/v1",
    }


@app.get("/panel")
def panel():
    index = STATIC_DIR / "panel" / "index.html"
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
    try:
        from app.services.queue_service import INBOUND_WEBHOOK_QUEUE
        from app.redis_client import get_redis

        queue_depth = get_redis().llen(INBOUND_WEBHOOK_QUEUE)
    except Exception:
        pass

    status_label = "ok" if db_ok and redis_ok else "degraded"
    return {
        "status": status_label,
        "postgres": db_ok,
        "redis": redis_ok,
        "webhook_queue_depth": queue_depth,
        "env": settings.app_env,
    }
