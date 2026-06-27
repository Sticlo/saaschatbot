from __future__ import annotations

from fastapi import APIRouter

from app.presentation.api import (
    ai,
    audit_logs,
    auth,
    bait_templates,
    billing,
    conversations,
    outbound,
    plans,
    quick_shortcuts,
    subscriptions,
    tenants,
    users,
    whatsapp,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(billing.router)
api_router.include_router(plans.router)
api_router.include_router(tenants.router)
api_router.include_router(subscriptions.router)
api_router.include_router(whatsapp.router)
api_router.include_router(conversations.router)
api_router.include_router(outbound.router)
api_router.include_router(bait_templates.router)
api_router.include_router(quick_shortcuts.router)
api_router.include_router(ai.router)
api_router.include_router(users.router)
api_router.include_router(audit_logs.router)
