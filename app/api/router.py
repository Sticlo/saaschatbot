from __future__ import annotations

from fastapi import APIRouter

from app.api import auth, tenants, users

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(tenants.router)
api_router.include_router(users.router)
