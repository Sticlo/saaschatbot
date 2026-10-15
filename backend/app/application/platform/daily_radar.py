"""Radar diario para el dueño de la plataforma: lo que salió mal ayer en cada negocio antes de que
el negocio se vaya sin decir nada. Clientes que se frustraron, promesas de la IA que no se
cumplieron, fallas, negocios que no arrancan y configuraciones que hacen perder ventas."""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.application.appointments.appointment_service import BOGOTA
from app.config import settings
from app.domain.entities import AuditLog, Conversation, Message, Product, Tenant, User
from app.domain.entities.enums import MessageDirection, MessageSource, UserRole, WhatsAppStatus
from app.infrastructure.cache.redis_client import get_redis

log = logging.getLogger(__name__)

SEND_AT = time(7, 0)
WINDOW = timedelta(hours=24)
# La IA dijo «Mira 👇» o «te lo envío» y no salió nada más en este tiempo.
PROMISE_GRACE = timedelta(minutes=3)
NO_CHATS_DAYS = 3
TRIAL_ENDING = timedelta(days=2)
MAX_SAMPLES = 3
AI_FAILURES = ("system.ai_send_failed", "system.ai_outage_handoff", "system.ai_quota_reached")

_FRUSTRATED_RE = re.compile(
    r"^[\s?¿!.]*\?{2,}[\s?¿!.]*$"
    r"|no\s+(?:me\s+)?(?:ha[ns]?\s+)?(?:lleg|respond|contest|aparec|carg)"
    r"|no\s+(?:te\s+|le\s+)?entiend|no\s+sirve"
    r"|\bhola\s*\?{2,}|\balguien\b.*\?"
    r"|me\s+(?:pueden|puede|van\s+a)\s+(?:atender|responder|contestar)"
    r"|(?:eres|es)\s+(?:un\s+)?(?:robot|bot|maquina)"
    r"|hablar\s+con\s+(?:una\s+|un\s+)?(?:persona|humano|asesor)"
    r"|de\s+nuevo|otra\s+vez|todavia\s+no|aun\s+no"
)
_PROMISE_RE = re.compile(
    r"\bmira\b|\bahi\s+te\s+va|\baqui\s+(?:te\s+va|tienes|estan)"
    r"|te\s+(?:lo|la|los|las)\s+(?:envio|mando|paso|comparto)"
    r"|te\s+(?:envio|mando|paso|comparto)\s+(?:el|la|los|las|nuestr)"
)


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).lower().split())


def _short(text: str, n: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _who(conversation: Optional[Conversation]) -> str:
    if conversation is None:
        return "cliente"
    tail = re.sub(r"\D", "", conversation.contact_phone or "")[-4:]
    name = (conversation.contact_name or "").strip() or "cliente"
    return f"{name} (…{tail})" if tail else name


@dataclass
class TenantFindings:
    name: str
    email: str = ""
    frustrated: list[str] = field(default_factory=list)
    broken_promises: list[str] = field(default_factory=list)
    ai_failures: int = 0
    setup: list[str] = field(default_factory=list)
    status: list[str] = field(default_factory=list)

    @property
    def has_any(self) -> bool:
        return bool(self.frustrated or self.broken_promises or self.ai_failures or self.setup or self.status)


@dataclass
class Radar:
    since: datetime
    inbound: int = 0
    ai_replies: int = 0
    signups: list[str] = field(default_factory=list)
    tenants: dict = field(default_factory=dict)

    def tenant(self, t: Tenant, email: str) -> TenantFindings:
        if t.id not in self.tenants:
            self.tenants[t.id] = TenantFindings(name=t.business_name, email=email)
        return self.tenants[t.id]


def _owner_emails(db: Session) -> dict:
    rows = (
        db.query(User.tenant_id, User.email)
        .filter(User.role == UserRole.OWNER.value, User.is_active.is_(True))
        .order_by(User.created_at.desc())
        .all()
    )
    return {tenant_id: email for tenant_id, email in rows}


def build_radar(db: Session, now: datetime, tenant_ids: Optional[list] = None) -> Radar:
    from app.application.conversations.interest_alert_service import alert_phones, own_number_recipients

    since = now - WINDOW
    radar = Radar(since=since)
    emails = _owner_emails(db)
    query = db.query(Tenant).filter(Tenant.is_active.is_(True))
    if tenant_ids is not None:
        query = query.filter(Tenant.id.in_(tenant_ids))
    tenants = {t.id: t for t in query.all()}

    recent = (
        db.query(Message)
        .filter(Message.created_at >= since, Message.tenant_id.in_(list(tenants)))
        .order_by(Message.conversation_id, Message.created_at)
        .all()
    )
    by_conv: dict = defaultdict(list)
    for m in recent:
        by_conv[m.conversation_id].append(m)
    conversations = {
        c.id: c for c in db.query(Conversation).filter(Conversation.id.in_(list(by_conv))).all()
    } if by_conv else {}

    for conv_id, msgs in by_conv.items():
        conv = conversations.get(conv_id)
        tenant = tenants.get(msgs[0].tenant_id)
        if tenant is None:
            continue
        for i, m in enumerate(msgs):
            body = m.body or ""
            if m.direction == MessageDirection.IN.value:
                radar.inbound += 1
                if _FRUSTRATED_RE.search(_plain(body)):
                    radar.tenant(tenant, emails.get(tenant.id, "")).frustrated.append(
                        f"{_who(conv)}: «{_short(body)}»"
                    )
                continue
            if m.source != MessageSource.BOT.value:
                continue
            radar.ai_replies += 1
            if body.startswith("[Imagen]") or body.startswith("📄") or not _PROMISE_RE.search(_plain(body)):
                continue
            nxt = msgs[i + 1] if i + 1 < len(msgs) else None
            followed = (
                nxt is not None
                and nxt.direction == MessageDirection.OUT.value
                and nxt.created_at - m.created_at <= PROMISE_GRACE
            )
            if not followed and now - m.created_at > PROMISE_GRACE:
                radar.tenant(tenant, emails.get(tenant.id, "")).broken_promises.append(
                    f"{_who(conv)}: la IA dijo «{_short(body, 70)}» y no se envió nada"
                )

    failures = (
        db.query(AuditLog.tenant_id, AuditLog.action)
        .filter(AuditLog.created_at >= since, AuditLog.action.in_(AI_FAILURES))
        .all()
    )
    for tenant_id, _action in failures:
        tenant = tenants.get(tenant_id)
        if tenant is not None:
            radar.tenant(tenant, emails.get(tenant.id, "")).ai_failures += 1

    quiet_since = now - timedelta(days=NO_CHATS_DAYS)
    for tenant in tenants.values():
        f_email = emails.get(tenant.id, "")
        if tenant.created_at >= since:
            radar.signups.append(f"{tenant.business_name} ({f_email or 'sin correo'})")
        session = tenant.whatsapp_session
        profile = tenant.profile
        findings: list[str] = []
        status: list[str] = []
        ever_connected = bool(session and session.last_connected_at)
        if not ever_connected:
            if tenant.created_at < since:
                days = (now - tenant.created_at).days
                status.append(f"Se registró hace {days} día{'s' if days != 1 else ''} y nunca conectó WhatsApp")
        elif tenant.whatsapp_status != WhatsAppStatus.CONNECTED.value:
            off_since = session.last_disconnected_at
            if off_since is None or now - off_since > timedelta(hours=1):
                status.append("WhatsApp desconectado: la IA no está atendiendo")
        elif not any(m.tenant_id == tenant.id for m in recent) and (
            db.query(Message.id)
            .filter(
                Message.tenant_id == tenant.id,
                Message.direction == MessageDirection.IN.value,
                Message.created_at >= quiet_since,
            )
            .first()
            is None
        ):
            status.append(f"Conectado pero sin chats de clientes en {NO_CHATS_DAYS} días")

        sub = tenant.subscription
        trial_end = getattr(sub, "trial_ends_at", None) if sub is not None else None
        if sub is not None and sub.status == "trial" and trial_end and now < trial_end <= now + TRIAL_ENDING:
            hours = int((trial_end - now).total_seconds() // 3600)
            status.append(f"Su prueba vence en {hours} h y no ha pagado")

        if ever_connected and profile is not None:
            recipients = profile.alert_recipients or []
            if own_number_recipients([r for r in recipients if isinstance(r, dict)], session):
                findings.append("Las alertas van al mismo número del negocio: no le suenan")
            elif not alert_phones(profile, sales=True):
                findings.append("Nadie recibe las alertas de venta")
            if (profile.payment_methods or "").strip():
                products = db.query(Product).filter(Product.tenant_id == tenant.id).all()
                if not products:
                    findings.append("Vende con pago en línea pero no tiene productos cargados")
                elif not any(p.photos for p in products):
                    findings.append("Ningún producto tiene fotos")

        if findings or status:
            row = radar.tenant(tenant, f_email)
            row.setup.extend(findings)
            row.status.extend(status)
    return radar


def _section(rows: list[TenantFindings], title: str, attr: str) -> list[str]:
    picked = [r for r in rows if getattr(r, attr)]
    if not picked:
        return []
    lines = ["", title]
    for r in picked:
        items = getattr(r, attr)
        lines.append(f"• {r.name} ({r.email or 'sin correo'}) — {len(items)}")
        lines.extend(f"    - {item}" for item in items[:MAX_SAMPLES])
        if len(items) > MAX_SAMPLES:
            lines.append(f"    - y {len(items) - MAX_SAMPLES} más")
    return lines


def render_radar(radar: Radar, now: datetime) -> tuple[str, str]:
    rows = sorted(radar.tenants.values(), key=lambda r: r.name.lower())
    rows = [r for r in rows if r.has_any]
    frustrated = sum(len(r.frustrated) for r in rows)
    promises = sum(len(r.broken_promises) for r in rows)
    failures = sum(r.ai_failures for r in rows)
    urgent = frustrated + promises + failures
    day = now.astimezone(BOGOTA).strftime("%d/%m")
    subject = (
        f"Radar Omitel {day}: {urgent} cosa{'s' if urgent != 1 else ''} por revisar"
        if urgent
        else f"Radar Omitel {day}: todo en orden"
    )
    lines = [
        f"Últimas 24 horas: {radar.inbound} mensajes de clientes, {radar.ai_replies} respuestas de la IA.",
    ]
    if radar.signups:
        lines.append(f"Registros nuevos: {', '.join(radar.signups)}. Escríbeles hoy para que conecten.")
    lines += _section(rows, "😤 CLIENTES FRUSTRADOS (escribieron «??», «no me llegó», pidieron una persona…)", "frustrated")
    lines += _section(rows, "🤥 LA IA PROMETIÓ ALGO QUE NO SE ENVIÓ", "broken_promises")
    failing = [r for r in rows if r.ai_failures]
    if failing:
        lines += ["", "⚠️ FALLAS DE LA IA (no pudo responder o enviar)"]
        lines += [f"• {r.name} ({r.email or 'sin correo'}) — {r.ai_failures}" for r in failing]
    lines += _section(rows, "🚦 NEGOCIOS QUE NECESITAN UNA MANO", "status")
    lines += _section(rows, "🔧 CONFIGURACIÓN QUE LES HACE PERDER VENTAS", "setup")
    if not rows:
        lines += ["", "No hubo clientes frustrados, promesas incumplidas ni fallas. 👌"]
    lines += ["", f"Consola: {settings.app_public_url.rstrip('/')}/panel/admin"]
    return subject, "\n".join(lines)


def _recipients() -> list[str]:
    emails = [settings.ops_alert_email.strip().lower()] if settings.ops_alert_email.strip() else []
    emails += sorted(settings.platform_admins())
    return list(dict.fromkeys(e for e in emails if e))


def send_daily_radar(db: Session, now: Optional[datetime] = None, *, force: bool = False) -> bool:
    """Una vez al día desde las 7:00 en Colombia. Con varios workers, solo uno lo envía."""
    from app.infrastructure.email.email_service import send_ops_alert_email

    now = now or datetime.now(timezone.utc)
    local = now.astimezone(BOGOTA)
    to = _recipients()
    if not to or (not force and local.time() < SEND_AT):
        return False
    if not force:
        try:
            if not get_redis().set(f"platform:daily_radar:{local.date().isoformat()}", "1", nx=True, ex=3 * 86400):
                return False
        except Exception:
            return False
    subject, text = render_radar(build_radar(db, now), now)
    sent = False
    for email in to:
        try:
            send_ops_alert_email(to_email=email, subject=subject, text=text)
            sent = True
        except Exception:
            log.exception("No se pudo enviar el radar diario a %s", email)
    return sent
