from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.router import api_router
from app.config import settings
from app.database import SessionLocal
from app.redis_client import get_redis, redis_ping
from app.services.plan_service import ensure_default_plan


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        with SessionLocal() as db:
            ensure_default_plan(db)
    except Exception:
        pass
    yield
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
    status_label = "ok" if db_ok and redis_ok else "degraded"
    return {
        "status": status_label,
        "postgres": db_ok,
        "redis": redis_ok,
        "env": settings.app_env,
    }
