from __future__ import annotations

from fastapi import APIRouter

from app.api import audit_logs, auth, conversations, plans, subscriptions, tenants, users, whatsapp

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(plans.router)
api_router.include_router(tenants.router)
api_router.include_router(subscriptions.router)
api_router.include_router(whatsapp.router)
api_router.include_router(conversations.router)
api_router.include_router(users.router)
api_router.include_router(audit_logs.router)
