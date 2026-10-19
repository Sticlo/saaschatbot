"""Consola de plataforma: cómo va cada empresa y qué se le cambió a mano."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.application.ai.ai_usage_service import (
    build_ai_usage_summary,
    daily_classify_limit,
    daily_reply_limit,
    get_daily_reply_count,
)
from app.application.billing.subscription_service import (
    activate_paid_subscription,
    build_subscription_summary,
    subscription_is_paid,
    trial_has_expired,
    trial_is_running,
)
from app.application.billing.tenant_service import log_audit
from app.application.conversations.interest_alert_service import alert_recipients
from app.application.platform.content_review import content_findings, pending_terms
from app.application.platform.incidents import PROBLEM_ACTIONS
from app.application.platform.tenant_overrides import (
    FEATURES,
    LIMITS,
    feature_allowed,
    limit_override,
    normalize_overrides,
    tenant_overrides,
)
from app.domain.entities import (
    AiQuestion,
    AuditLog,
    Conversation,
    Plan,
    Subscription,
    Tenant,
    User,
    UserRole,
    WhatsAppSession,
)
from app.domain.entities.enums import ConversationInterest, SubscriptionStatus, TenantPlan
from app.infrastructure.cache.redis_client import cache_set, get_redis, tenant_cache_key

PROBLEM_WINDOW_DAYS = 7
MAX_TENANTS_LISTED = 500
MAX_GRANT_DAYS = 365
MAX_TRIAL_EXTENSION_DAYS = 60


class PlatformError(ValueError):
    pass


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _subscription_view(tenant: Tenant, sub: Optional[Subscription]) -> dict[str, Any]:
    if sub is None or sub.plan is None:
        return {"status": "none", "plan_id": None, "plan_name": None}
    summary = build_subscription_summary(tenant, sub, sub.plan)
    return {
        "status": summary["status"],
        "plan_id": str(sub.plan.id),
        "plan_name": sub.plan.name,
        "is_paid": summary["is_paid"],
        "needs_payment": summary["needs_payment"],
        "current_period_end": _iso(summary["current_period_end"]),
        "auto_renew": summary["auto_renew"],
        "payment_method_label": summary["payment_method_label"],
        "cancel_at_period_end": summary["cancel_at_period_end"],
        "trial_ends_at": _iso(summary["trial_ends_at"]),
        "trial_days_left": summary["trial_days_left"],
        "trial_expired": summary["trial_expired"],
    }


def login_method(user: User) -> str:
    if user.oauth_provider:
        return user.oauth_provider
    return "password" if user.hashed_password else "magic_link"


def activation_status(
    tenant: Tenant, sub: Optional[Subscription], ever_connected: bool
) -> str:
    """Embudo de la empresa: se registró → conectó WhatsApp → paga."""
    if not tenant.is_active:
        return "suspended"
    if sub is not None and subscription_is_paid(sub):
        return "paying"
    return "active" if ever_connected else "pending"


def _ever_connected(db: Session, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    rows = db.query(WhatsAppSession.tenant_id).filter(
        WhatsAppSession.tenant_id.in_(ids),
        or_(WhatsAppSession.last_connected_at.isnot(None), WhatsAppSession.status == "connected"),
    )
    return {row[0] for row in rows}


def list_accounts(db: Session, *, query: str = "") -> list[dict[str, Any]]:
    """Cada correo registrado, con cómo entra y en qué punto va su empresa."""
    users_q = db.query(User, Tenant).join(Tenant, Tenant.id == User.tenant_id)
    q = query.strip()
    if q:
        like = f"%{q}%"
        users_q = users_q.filter(or_(User.email.ilike(like), User.full_name.ilike(like), Tenant.business_name.ilike(like)))
    rows = users_q.order_by(User.created_at.desc()).limit(MAX_TENANTS_LISTED).all()
    ids = list({tenant.id for _, tenant in rows})
    subs = {
        s.tenant_id: s
        for s in db.query(Subscription).filter(Subscription.tenant_id.in_(ids))
    } if ids else {}
    connected = _ever_connected(db, ids) if ids else set()
    return [
        {
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "is_active": user.is_active,
            "login_method": login_method(user),
            "last_login_at": _iso(user.last_login_at),
            "created_at": _iso(user.created_at),
            "tenant_id": str(tenant.id),
            "business_name": tenant.business_name,
            "activation": activation_status(tenant, subs.get(tenant.id), tenant.id in connected),
        }
        for user, tenant in rows
    ]


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _wa_disconnects(db: Session, ids: list[uuid.UUID], since: datetime) -> dict[uuid.UUID, list[tuple[str, datetime]]]:
    """(motivo, cuándo) de cada desconexión registrada; las viejas sin motivo quedan como «unknown»."""
    out: dict[uuid.UUID, list[tuple[str, datetime]]] = {}
    rows = db.query(AuditLog.tenant_id, AuditLog.details, AuditLog.created_at).filter(
        AuditLog.action == "system.whatsapp_disconnected",
        AuditLog.created_at >= since,
    )
    if ids:
        rows = rows.filter(AuditLog.tenant_id.in_(ids))
    for tenant_id, details, created_at in rows:
        reason = (details or {}).get("reason") if isinstance(details, dict) else None
        out.setdefault(tenant_id, []).append((reason or "unknown", _aware(created_at)))
    return out


ATTENTION_LABELS = {
    "content_risk": "Posible venta de algo ilegal",
    "wa_banned": "WhatsApp bloqueado o restringido",
    "wa_rejected": "WhatsApp rechazó el vínculo",
    "wa_down": "WhatsApp caído",
    "payment_due": "Pago pendiente",
    "trial_ending": "La prueba termina pronto",
    "not_connected": "No ha conectado WhatsApp",
    "ai_not_ready": "Le falta configurar la IA",
    "ai_failures": "Fallas de la IA",
    "ai_unanswered": "La IA no supo responder",
    "ai_off": "Apagó la IA",
    "quiet": "Sin chats hace días",
    "trial_lapsed": "Prueba vencida sin pagar",
}
TRIAL_ENDING_HOURS = 48
QUIET_DAYS = 3
TRIAL_LAPSED_FOLLOWUP_DAYS = 10
# Preguntas que la IA no supo y el dueño lleva un día sin enseñarle: el cliente final se queda sin respuesta.
UNANSWERED_MIN = 3
UNANSWERED_AGE_HOURS = 24


def _unanswered_questions(db: Session, ids: list[uuid.UUID], now: datetime) -> dict[uuid.UUID, int]:
    rows = db.query(AiQuestion.tenant_id, func.count(AiQuestion.id)).filter(
        AiQuestion.status == "open",
        AiQuestion.created_at <= now - timedelta(hours=UNANSWERED_AGE_HOURS),
    )
    if ids:
        rows = rows.filter(AiQuestion.tenant_id.in_(ids))
    return dict(rows.group_by(AiQuestion.tenant_id).all())


def attention_reasons(
    tenant: Tenant,
    sub: Optional[Subscription],
    *,
    session: Optional[WhatsAppSession],
    ever_connected: bool,
    last_activity_at: Optional[datetime],
    disconnects_7d: list[tuple[str, datetime]],
    ai_problems_7d: int,
    readiness: Optional[dict],
    now: datetime,
    unanswered: int = 0,
    risky_terms: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Por qué hay que mirar (o llamar) a esta empresa hoy, de lo más urgente a lo menos."""
    if not tenant.is_active:
        return []
    paid = sub is not None and subscription_is_paid(sub, now)
    trial = trial_is_running(sub, now)
    live = paid or trial
    status = tenant.whatsapp_status
    connected = status == "connected"
    reasons: list[tuple[int, str, Optional[datetime], Optional[str]]] = []

    if risky_terms:
        reasons.append((1, "content_risk", None, ", ".join(risky_terms[:3])))
    if status in ("banned", "restricted"):
        reasons.append((1, "wa_banned", None, None))
    elif not connected and any(r == "device_removed" for r, _ in disconnects_7d):
        reasons.append((1, "wa_rejected", max(at for r, at in disconnects_7d if r == "device_removed"), None))
    elif live and ever_connected and not connected:
        reasons.append((1, "wa_down", _aware(session.last_disconnected_at) if session else None, None))
    if sub is not None and sub.current_period_end is not None and sub.status == SubscriptionStatus.PAST_DUE.value:
        reasons.append((1, "payment_due", _aware(sub.current_period_end), None))
    if trial and not paid and sub.trial_ends_at is not None:
        ends = _aware(sub.trial_ends_at)
        if ends - now <= timedelta(hours=TRIAL_ENDING_HOURS):
            reasons.append((2, "trial_ending", ends, None))
    if live and not ever_connected and now - _aware(tenant.created_at) >= timedelta(hours=12):
        reasons.append((2, "not_connected", _aware(tenant.created_at), None))
    if live and readiness is not None and not readiness.get("ready"):
        reasons.append((2, "ai_not_ready", None, f"{readiness.get('done', 0)}/{readiness.get('total', 0)}"))
    if ai_problems_7d:
        reasons.append((2, "ai_failures", None, str(ai_problems_7d)))
    if live and unanswered >= UNANSWERED_MIN:
        reasons.append((2, "ai_unanswered", None, str(unanswered)))
    if connected and not tenant.ai_global_enabled:
        reasons.append((3, "ai_off", None, None))
    if connected:
        reference = _aware(last_activity_at) or (_aware(session.last_connected_at) if session else None)
        if reference is not None and now - reference >= timedelta(days=QUIET_DAYS):
            reasons.append((3, "quiet", reference, None))
    if trial_has_expired(sub, now) and now - _aware(sub.trial_ends_at) <= timedelta(days=TRIAL_LAPSED_FOLLOWUP_DAYS):
        reasons.append((3, "trial_lapsed", _aware(sub.trial_ends_at), None))

    reasons.sort(key=lambda r: r[0])
    return [
        {"key": key, "label": ATTENTION_LABELS[key], "priority": priority, "at": _iso(at), "detail": detail}
        for priority, key, at, detail in reasons
    ]


def list_tenants(db: Session, *, query: str = "") -> list[dict[str, Any]]:
    tenants_q = db.query(Tenant)
    q = query.strip()
    if q:
        like = f"%{q}%"
        owner_match = db.query(User.tenant_id).filter(User.email.ilike(like))
        tenants_q = tenants_q.filter(
            or_(Tenant.business_name.ilike(like), Tenant.slug.ilike(like), Tenant.id.in_(owner_match))
        )
    tenants = tenants_q.order_by(Tenant.created_at.desc()).limit(MAX_TENANTS_LISTED).all()
    ids = [t.id for t in tenants]
    if not ids:
        return []

    subs = {
        s.tenant_id: s
        for s in db.query(Subscription)
        .options(joinedload(Subscription.plan))
        .filter(Subscription.tenant_id.in_(ids))
    }
    owners: dict[uuid.UUID, str] = {}
    for tenant_id, email in (
        db.query(User.tenant_id, User.email)
        .filter(User.tenant_id.in_(ids), User.role == UserRole.OWNER.value)
        .order_by(User.created_at.asc())
    ):
        owners.setdefault(tenant_id, email)
    conv_stats = {
        row[0]: row
        for row in db.query(
            Conversation.tenant_id,
            func.count(Conversation.id),
            func.count(Conversation.id).filter(
                Conversation.interest_status == ConversationInterest.INTERESTED.value
            ),
            func.max(Conversation.last_message_at),
        )
        .filter(Conversation.tenant_id.in_(ids))
        .group_by(Conversation.tenant_id)
    }
    since = datetime.now(timezone.utc) - timedelta(days=PROBLEM_WINDOW_DAYS)
    problems = {
        row[0]: row
        for row in db.query(AuditLog.tenant_id, func.count(AuditLog.id), func.max(AuditLog.created_at))
        .filter(
            AuditLog.tenant_id.in_(ids),
            AuditLog.action.in_(PROBLEM_ACTIONS),
            AuditLog.created_at >= since,
        )
        .group_by(AuditLog.tenant_id)
    }
    ai_problems = dict(
        db.query(AuditLog.tenant_id, func.count(AuditLog.id))
        .filter(
            AuditLog.tenant_id.in_(ids),
            AuditLog.action.in_(PROBLEM_ACTIONS - {"system.whatsapp_disconnected"}),
            AuditLog.created_at >= since,
        )
        .group_by(AuditLog.tenant_id)
        .all()
    )
    connected = _ever_connected(db, ids)
    sessions = {s.tenant_id: s for s in db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id.in_(ids))}
    disconnects = _wa_disconnects(db, ids, since)
    now = datetime.now(timezone.utc)
    unanswered = _unanswered_questions(db, ids, now)
    findings = content_findings(db, ids)

    from app.application.ai.ai_readiness_service import tenant_readiness

    out = []
    for t in tenants:
        conv = conv_stats.get(t.id)
        prob = problems.get(t.id)
        sub = subs.get(t.id)
        live = t.is_active and (subscription_is_paid(sub, now) or trial_is_running(sub, now))
        attention = attention_reasons(
            t,
            sub,
            session=sessions.get(t.id),
            ever_connected=t.id in connected,
            last_activity_at=conv[3] if conv else None,
            disconnects_7d=disconnects.get(t.id, []),
            ai_problems_7d=ai_problems.get(t.id, 0),
            readiness=tenant_readiness(db, t.id) if live else None,
            now=now,
            unanswered=unanswered.get(t.id, 0),
            risky_terms=pending_terms(t, findings.get(t.id, [])),
        )
        out.append(
            {
                "id": str(t.id),
                "business_name": t.business_name,
                "slug": t.slug,
                "owner_email": owners.get(t.id),
                "is_active": t.is_active,
                "activation": activation_status(t, subs.get(t.id), t.id in connected),
                "whatsapp_status": t.whatsapp_status,
                "ai_enabled": t.ai_global_enabled and feature_allowed(t, "ai_replies"),
                "has_overrides": bool(set(tenant_overrides(t)) - {"content_reviewed"}),
                "subscription": _subscription_view(t, subs.get(t.id)),
                "ai_replies_today": get_daily_reply_count(t.id),
                "conversations": conv[1] if conv else 0,
                "interested": conv[2] if conv else 0,
                "last_activity_at": _iso(conv[3]) if conv else None,
                "problems_7d": prob[1] if prob else 0,
                "last_problem_at": _iso(prob[2]) if prob else None,
                "attention": attention,
                "created_at": _iso(t.created_at),
            }
        )
    return out


def platform_overview(db: Session) -> dict[str, Any]:
    """Números del negocio completo: embudo, ingresos del mes y salud de WhatsApp."""
    now = datetime.now(timezone.utc)
    month_ago = now - timedelta(days=30)
    tenants = db.query(Tenant).all()
    ids = [t.id for t in tenants]
    subs = {
        s.tenant_id: s
        for s in db.query(Subscription).options(joinedload(Subscription.plan))
    }
    connected = _ever_connected(db, ids) if ids else set()

    def funnel(group: list[Tenant]) -> dict[str, int]:
        return {
            "registered": len(group),
            "connected": sum(1 for t in group if t.id in connected),
            "paying": sum(1 for t in group if subscription_is_paid(subs.get(t.id), now)),
        }

    mrr = courtesy = 0
    for t in tenants:
        sub = subs.get(t.id)
        if not t.is_active or not subscription_is_paid(sub, now):
            continue
        if sub.current_period_end is None:
            courtesy += 1
        else:
            mrr += sub.plan.price_cop if sub.plan else 0

    trials = [subs.get(t.id) for t in tenants if t.is_active and trial_is_running(subs.get(t.id), now)]
    ending = [
        s for s in trials
        if s.trial_ends_at is not None and _aware(s.trial_ends_at) - now <= timedelta(hours=TRIAL_ENDING_HOURS)
    ]

    by_reason: dict[str, int] = {}
    tenants_by_reason: dict[str, set[uuid.UUID]] = {}
    for tenant_id, events in _wa_disconnects(db, [], month_ago).items():
        for reason, _ in events:
            by_reason[reason] = by_reason.get(reason, 0) + 1
            tenants_by_reason.setdefault(reason, set()).add(tenant_id)
    banned_now = sum(1 for t in tenants if t.whatsapp_status in ("banned", "restricted"))
    banned_tenants = len(tenants_by_reason.get("banned", set()) | {
        t.id for t in tenants if t.whatsapp_status in ("banned", "restricted")
    })

    return {
        "funnel": funnel(tenants),
        "funnel_30d": funnel([t for t in tenants if _aware(t.created_at) >= month_ago]),
        "signups_7d": sum(1 for t in tenants if _aware(t.created_at) >= now - timedelta(days=7)),
        "signups_prev_7d": sum(
            1 for t in tenants if now - timedelta(days=14) <= _aware(t.created_at) < now - timedelta(days=7)
        ),
        "mrr_cop": mrr,
        "courtesy_accounts": courtesy,
        "trials_active": len(trials),
        "trials_ending_48h": len(ending),
        "whatsapp": {
            "connected_now": sum(1 for t in tenants if t.whatsapp_status == "connected"),
            "ever_connected": len(connected),
            "banned_now": banned_now,
            "disconnects_30d": by_reason,
            "tenants_affected_30d": {reason: len(v) for reason, v in tenants_by_reason.items()},
            "ban_rate": round(banned_tenants / len(connected), 4) if connected else 0.0,
        },
    }


def _first_at(db: Session, *criteria) -> Optional[datetime]:
    return db.query(func.min(AuditLog.created_at)).filter(*criteria).scalar()


def activation_steps(db: Session, tenant: Tenant, sub: Optional[Subscription], readiness: dict) -> list[dict[str, Any]]:
    """El camino de una empresa nueva hasta pagar, con la fecha en que cumplió cada paso."""
    from app.domain.entities import Message

    first_connect = _first_at(
        db, AuditLog.tenant_id == tenant.id, AuditLog.action == "system.whatsapp_connected"
    )
    session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant.id).first()
    ever_connected = bool(_ever_connected(db, [tenant.id]))
    first_ai = (
        db.query(func.min(Message.created_at))
        .filter(Message.tenant_id == tenant.id, Message.source == "bot")
        .scalar()
    )
    has_interested = (
        db.query(Conversation.id)
        .filter(
            Conversation.tenant_id == tenant.id,
            Conversation.interest_status == ConversationInterest.INTERESTED.value,
        )
        .first()
        is not None
    )
    first_paid = _first_at(db, AuditLog.tenant_id == tenant.id, AuditLog.action == "subscription.activated")
    paid = sub is not None and subscription_is_paid(sub)
    return [
        {"key": "registered", "label": "Se registró", "done": True, "at": _iso(tenant.created_at)},
        {
            "key": "ai_ready",
            "label": f"Configuró la IA ({readiness.get('done', 0)}/{readiness.get('total', 0)})",
            "done": bool(readiness.get("ready")),
            "at": None,
        },
        {
            "key": "connected",
            "label": "Conectó WhatsApp",
            "done": ever_connected,
            "at": _iso(first_connect or (session.last_connected_at if session else None)),
        },
        {"key": "first_ai_reply", "label": "La IA respondió su primer chat", "done": first_ai is not None, "at": _iso(first_ai)},
        {"key": "first_interested", "label": "Tuvo su primer interesado", "done": has_interested, "at": None},
        {"key": "paid", "label": "Pagó", "done": paid or first_paid is not None, "at": _iso(first_paid)},
    ]


def _effective_limits(db: Session, tenant: Tenant, sub: Optional[Subscription]) -> dict[str, Any]:
    plan_members = sub.plan.max_team_members if sub and sub.plan else None
    members_override = limit_override(tenant, "max_team_members")
    return {
        "ai_daily_replies": daily_reply_limit(db, tenant),
        "ai_daily_classifications": daily_classify_limit(db, tenant),
        "max_team_members": members_override if members_override is not None else plan_members,
    }


def tenant_detail(db: Session, tenant: Tenant) -> dict[str, Any]:
    sub = (
        db.query(Subscription)
        .options(joinedload(Subscription.plan))
        .filter(Subscription.tenant_id == tenant.id)
        .first()
    )
    session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant.id).first()
    users = db.query(User).filter(User.tenant_id == tenant.id).order_by(User.created_at.asc()).all()
    profile = tenant.profile
    total, interested, last_at = (
        db.query(
            func.count(Conversation.id),
            func.count(Conversation.id).filter(
                Conversation.interest_status == ConversationInterest.INTERESTED.value
            ),
            func.max(Conversation.last_message_at),
        )
        .filter(Conversation.tenant_id == tenant.id)
        .one()
    )
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    new_week = (
        db.query(func.count(Conversation.id))
        .filter(Conversation.tenant_id == tenant.id, Conversation.created_at >= week_ago)
        .scalar()
    )
    overrides = tenant_overrides(tenant)
    from app.application.ai.ai_readiness_service import tenant_readiness
    from app.application.whatsapp.whatsapp_status import DISCONNECT_REASONS

    now = datetime.now(timezone.utc)
    readiness = tenant_readiness(db, tenant.id)
    disconnects = _wa_disconnects(db, [tenant.id], now - timedelta(days=30)).get(tenant.id, [])
    ai_problems_7d = (
        db.query(func.count(AuditLog.id))
        .filter(
            AuditLog.tenant_id == tenant.id,
            AuditLog.action.in_(PROBLEM_ACTIONS - {"system.whatsapp_disconnected"}),
            AuditLog.created_at >= week_ago,
        )
        .scalar()
    )
    ever_connected = bool(_ever_connected(db, [tenant.id]))
    owner = next((u for u in users if u.role == UserRole.OWNER.value), users[0] if users else None)
    alert_phones = [r["phone"] for r in alert_recipients(profile) if r.get("phone")]
    findings = content_findings(db, [tenant.id]).get(tenant.id, [])
    pending = pending_terms(tenant, findings)
    return {
        "content_review": {"findings": findings, "pending": pending},
        "attention": attention_reasons(
            tenant,
            sub,
            session=session,
            ever_connected=ever_connected,
            last_activity_at=last_at,
            disconnects_7d=[(r, at) for r, at in disconnects if at >= week_ago],
            ai_problems_7d=ai_problems_7d,
            readiness=readiness,
            now=now,
            unanswered=_unanswered_questions(db, [tenant.id], now).get(tenant.id, 0),
            risky_terms=pending,
        ),
        "activation_steps": activation_steps(db, tenant, sub, readiness),
        "readiness": readiness,
        "contact": {
            "name": owner.full_name if owner else None,
            "email": owner.email if owner else None,
            "phone": alert_phones[0] if alert_phones else (session.phone_number if session else None),
            "phone_source": "alert" if alert_phones else ("whatsapp" if session and session.phone_number else None),
        },
        "whatsapp_disconnects_30d": [
            {"reason": reason, "label": DISCONNECT_REASONS.get(reason, reason), "at": _iso(at)}
            for reason, at in sorted(disconnects, key=lambda e: e[1], reverse=True)
        ],
        "id": str(tenant.id),
        "business_name": tenant.business_name,
        "slug": tenant.slug,
        "is_active": tenant.is_active,
        "created_at": _iso(tenant.created_at),
        "ai_global_enabled": tenant.ai_global_enabled,
        "industry": profile.industry if profile else None,
        "ai_mode": profile.ai_mode if profile else None,
        "ai_booking_enabled": bool(profile.ai_booking_enabled) if profile else False,
        "alert_phone_set": bool(alert_recipients(profile)),
        "whatsapp": {
            "status": tenant.whatsapp_status,
            "phone_number": session.phone_number if session else None,
            "last_connected_at": _iso(session.last_connected_at) if session else None,
            "last_disconnected_at": _iso(session.last_disconnected_at) if session else None,
        },
        "activation": activation_status(tenant, sub, bool(_ever_connected(db, [tenant.id]))),
        "users": [
            {
                "email": u.email,
                "full_name": u.full_name,
                "role": u.role,
                "is_active": u.is_active,
                "login_method": login_method(u),
                "last_login_at": _iso(u.last_login_at),
            }
            for u in users
        ],
        "subscription": _subscription_view(tenant, sub),
        "usage": build_ai_usage_summary(db, tenant),
        "stats": {
            "conversations": total,
            "interested": interested,
            "new_conversations_7d": new_week,
            "last_activity_at": _iso(last_at),
        },
        "limits": _effective_limits(db, tenant, sub),
        "plan_limits": {
            "max_team_members": sub.plan.max_team_members if sub and sub.plan else None,
            "features": sub.plan.features if sub and sub.plan else {},
        },
        "overrides": overrides,
        "features": {key: feature_allowed(tenant, key) for key in FEATURES},
        "catalog": {"features": FEATURES, "limits": LIMITS},
    }


HISTORY_KINDS = {"all", "problems", "platform", "system"}


def tenant_history(
    db: Session, tenant_id: uuid.UUID, *, kind: str = "all", limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    q = db.query(AuditLog, User.email).outerjoin(User, User.id == AuditLog.user_id).filter(
        AuditLog.tenant_id == tenant_id
    )
    if kind == "problems":
        q = q.filter(AuditLog.action.in_(PROBLEM_ACTIONS))
    elif kind == "platform":
        q = q.filter(AuditLog.action.like("platform.%"))
    elif kind == "system":
        q = q.filter(AuditLog.action.like("system.%"))
    rows = q.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit).all()
    return [
        {
            "id": str(log.id),
            "action": log.action,
            "message": log.message,
            "details": log.details,
            "user_email": email,
            "is_problem": log.action in PROBLEM_ACTIONS,
            "created_at": _iso(log.created_at),
        }
        for log, email in rows
    ]


def _grant_paid_days(db: Session, tenant: Tenant, sub: Subscription, days: int) -> datetime:
    """Días de cortesía: se suman al periodo pagado vigente o arrancan hoy."""
    now = datetime.now(timezone.utc)
    end = sub.current_period_end
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if subscription_is_paid(sub, now) and end is not None and end > now:
        start, new_end = sub.current_period_start or now, end + timedelta(days=days)
    else:
        start, new_end = now, now + timedelta(days=days)
    activate_paid_subscription(db, tenant, sub, sub.plan, period_start=start, period_end=new_end)
    return new_end


def _grant_unlimited(db: Session, tenant: Tenant, sub: Subscription) -> None:
    """Plan pagado sin fecha de fin: los sweepers ignoran periodos sin final y nunca se cobra."""
    activate_paid_subscription(
        db, tenant, sub, sub.plan, period_start=sub.current_period_start or datetime.now(timezone.utc)
    )
    sub.current_period_end = None
    sub.auto_renew = False
    sub.cancel_at_period_end = False
    sub.next_renewal_attempt_at = None
    sub.renewal_attempts = 0


def _extend_trial(tenant: Tenant, sub: Subscription, days: int) -> datetime:
    """Más días de prueba: se suman a los que le quedan o arrancan hoy si ya se le acabó."""
    now = datetime.now(timezone.utc)
    end = sub.trial_ends_at
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    new_end = (end if end is not None and end > now else now) + timedelta(days=days)
    sub.status = SubscriptionStatus.TRIAL.value
    sub.trial_ends_at = new_end
    tenant.plan = TenantPlan.TRIAL.value
    tenant.daily_bait_limit = sub.plan.trial_bait_limit
    cache_set(tenant_cache_key(str(tenant.id), "plan"), tenant.plan, ttl_seconds=900)
    return new_end


def update_tenant(
    db: Session,
    tenant: Tenant,
    *,
    admin_email: str,
    admin_user_id: uuid.UUID,
    admin_tenant_id: uuid.UUID,
    ip_address: Optional[str],
    plan_id: Optional[uuid.UUID] = None,
    is_active: Optional[bool] = None,
    grant_paid_days: Optional[int] = None,
    grant_unlimited: bool = False,
    extend_trial_days: Optional[int] = None,
    overrides: Optional[dict[str, Any]] = None,
    reset_ai_today: bool = False,
    content_reviewed: bool = False,
) -> list[str]:
    """Aplica los cambios y deja cada uno en el historial de la empresa con el correo de quien lo hizo."""
    changes: list[tuple[str, str, dict[str, Any]]] = []
    sub = (
        db.query(Subscription)
        .options(joinedload(Subscription.plan))
        .filter(Subscription.tenant_id == tenant.id)
        .first()
    )

    if plan_id is not None:
        plan = db.get(Plan, plan_id)
        if plan is None or not plan.is_active:
            raise PlatformError("Plan no encontrado")
        if sub is None:
            raise PlatformError("La empresa no tiene suscripción")
        if sub.plan_id != plan.id:
            previous = sub.plan.name if sub.plan else None
            sub.plan_id = plan.id
            sub.plan = plan
            if tenant.plan == TenantPlan.PAID.value:
                tenant.daily_bait_limit = plan.daily_bait_limit
            changes.append(
                ("platform.plan_changed", f"Plan cambiado de {previous} a {plan.name}", {"from": previous, "to": plan.name})
            )

    if grant_paid_days:
        if not 1 <= grant_paid_days <= MAX_GRANT_DAYS:
            raise PlatformError(f"Los días de cortesía van de 1 a {MAX_GRANT_DAYS}")
        if sub is None or sub.plan is None:
            raise PlatformError("La empresa no tiene suscripción")
        new_end = _grant_paid_days(db, tenant, sub, grant_paid_days)
        changes.append(
            (
                "platform.paid_days_granted",
                f"{grant_paid_days} días de plan regalados (vence {new_end:%Y-%m-%d})",
                {"days": grant_paid_days, "period_end": new_end.isoformat()},
            )
        )

    if grant_unlimited:
        if sub is None or sub.plan is None:
            raise PlatformError("La empresa no tiene suscripción")
        _grant_unlimited(db, tenant, sub)
        changes.append(
            ("platform.unlimited_granted", f"{sub.plan.name} sin vencimiento (cortesía, sin cobros)", {"plan": sub.plan.name})
        )

    if extend_trial_days:
        if not 1 <= extend_trial_days <= MAX_TRIAL_EXTENSION_DAYS:
            raise PlatformError(f"La prueba se extiende de 1 a {MAX_TRIAL_EXTENSION_DAYS} días")
        if sub is None or sub.plan is None:
            raise PlatformError("La empresa no tiene suscripción")
        if not (trial_is_running(sub) or trial_has_expired(sub)):
            raise PlatformError("Esta empresa ya no está en prueba: usa «Regalar días de plan»")
        new_end = _extend_trial(tenant, sub, extend_trial_days)
        changes.append(
            (
                "platform.trial_extended",
                f"Prueba extendida {extend_trial_days} días (termina {new_end:%Y-%m-%d})",
                {"days": extend_trial_days, "trial_ends_at": new_end.isoformat()},
            )
        )

    if overrides is not None:
        try:
            clean = normalize_overrides(overrides)
        except ValueError as exc:
            raise PlatformError(str(exc)) from exc
        before = tenant_overrides(tenant)
        if "content_reviewed" in before:
            clean["content_reviewed"] = before["content_reviewed"]
        if clean != before:
            tenant.platform_overrides = clean or None
            changes.append(
                ("platform.overrides_changed", "Ajustes de límites y funciones actualizados", {"before": before, "after": clean})
            )

    if is_active is not None and is_active != tenant.is_active:
        if not is_active and tenant.id == admin_tenant_id:
            raise PlatformError("No puedes suspender tu propia empresa: perderías el acceso a la consola")
        tenant.is_active = is_active
        changes.append(
            (
                "platform.reactivated" if is_active else "platform.suspended",
                "Empresa reactivada" if is_active else "Empresa suspendida: sin acceso al panel y la IA no responde",
                {},
            )
        )

    if content_reviewed:
        terms = pending_terms(tenant, content_findings(db, [tenant.id]).get(tenant.id, []))
        if terms:
            current = dict(tenant_overrides(tenant))
            current["content_reviewed"] = sorted(set(current.get("content_reviewed") or []) | set(terms))
            tenant.platform_overrides = current
            changes.append(
                ("platform.content_reviewed", f"Contenido revisado y aprobado: {', '.join(terms)}", {"terms": terms})
            )

    if reset_ai_today:
        from app.application.ai.ai_usage_service import _classify_day_key, _reply_day_key

        try:
            get_redis().delete(_reply_day_key(tenant.id), _classify_day_key(tenant.id))
        except Exception:
            raise PlatformError("No se pudo reiniciar el contador (Redis)") from None
        changes.append(("platform.ai_counter_reset", "Contador de IA de hoy reiniciado", {}))

    for action, message, details in changes:
        log_audit(
            db,
            tenant_id=tenant.id,
            user_id=None,
            action=action,
            details={**details, "admin_email": admin_email, "admin_user_id": str(admin_user_id)},
            message=f"{message} — por {admin_email}",
            ip_address=ip_address,
        )
    db.commit()
    return [action for action, _, _ in changes]
