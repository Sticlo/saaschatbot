from __future__ import annotations

from fastapi import APIRouter

from app.presentation.api import (
    ai,
    ai_flows,
    ai_questions,
    appointments,
    audit_logs,
    auth,
    availability,
    bait_templates,
    billing,
    conversations,
    orders,
    plans,
    platform,
    products,
    quick_shortcuts,
    results,
    site_connection,
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
api_router.include_router(bait_templates.router)
api_router.include_router(quick_shortcuts.router)
api_router.include_router(ai_flows.router)
api_router.include_router(availability.router)
api_router.include_router(site_connection.router)
api_router.include_router(products.router)
api_router.include_router(orders.router)
api_router.include_router(appointments.router)
api_router.include_router(results.router)
api_router.include_router(ai.router)
api_router.include_router(ai_questions.router)
api_router.include_router(users.router)
api_router.include_router(audit_logs.router)
api_router.include_router(platform.router)
