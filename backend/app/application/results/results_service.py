"""Lo que Omitel le generó a cada negocio: citas que agendó la IA, cuántas fuera de horario,
mensajes respondidos y su valor en plata. Solo cuenta lo que hizo la IA, sin inflar nada:
si el dueño no reconoce el número en su agenda, deja de creer en todos."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import Time, cast, func, or_
from sqlalchemy.orm import Session

from app.application.appointments.appointment_service import BOGOTA, active_staff, get_schedule
from app.domain.entities import AiReplyEvent, Appointment, Tenant, TenantProfile

AI_SOURCE = "ai"
# Lo que tarda una persona en leer y contestar un mensaje de WhatsApp. Conservador a propósito.
MINUTES_PER_REPLY = 1
MAX_TICKET_COP = 50_000_000

MONTHS_ES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


@dataclass
class ResultsSummary:
    start: datetime
    end: datetime
    ai_appointments: int
    after_hours_appointments: int
    ai_replies: int
    after_hours_replies: int
    clients_attended: int
    avg_ticket_cop: Optional[int]
    revenue_cop: Optional[int]
    plan_price_cop: Optional[int]

    @property
    def hours_saved(self) -> int:
        return round(self.ai_replies * MINUTES_PER_REPLY / 60)

    @property
    def roi_multiple(self) -> Optional[float]:
        if not self.revenue_cop or not self.plan_price_cop:
            return None
        return round(self.revenue_cop / self.plan_price_cop, 1)

    @property
    def has_activity(self) -> bool:
        return bool(self.ai_appointments or self.ai_replies)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["start"] = self.start.isoformat()
        data["end"] = self.end.isoformat()
        data["hours_saved"] = self.hours_saved
        data["roi_multiple"] = self.roi_multiple
        return data


# —— Periodos (hora de Colombia) ——


def _local_midnight(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=BOGOTA)


def month_period(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(BOGOTA)
    return _local_midnight(local.date().replace(day=1)), now


def previous_month_period(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    start_this, _ = month_period(now)
    last_day_prev = start_this.date() - timedelta(days=1)
    return _local_midnight(last_day_prev.replace(day=1)), start_this


def month_key(moment: datetime) -> str:
    local = moment.astimezone(BOGOTA)
    return f"{local.year:04d}-{local.month:02d}"


def month_name(moment: datetime) -> str:
    return MONTHS_ES[moment.astimezone(BOGOTA).month - 1]


# —— Cálculo ——


def normalize_ticket(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    value = int(value)
    if value <= 0:
        return None
    if value > MAX_TICKET_COP:
        raise ValueError("Ese valor es demasiado alto para un servicio promedio")
    return value


def _working_days(db: Session, tenant_id: uuid.UUID) -> Optional[set[int]]:
    """Días (0 = lunes) en que atiende alguien del equipo. None = sin equipo: no se sabe."""
    staff = active_staff(db, tenant_id)
    if not staff:
        return None
    return {int(d) for s in staff for d in (s.work_days or [])}


def is_after_hours(
    moment: datetime, *, open_time: time, close_time: time, working_days: Optional[set[int]]
) -> bool:
    local = moment.astimezone(BOGOTA)
    if working_days is not None and local.weekday() not in working_days:
        return True
    return not (open_time <= local.time() < close_time)


def _plan_price(db: Session, tenant: Tenant) -> Optional[int]:
    from app.application.billing.plan_service import get_default_plan
    from app.application.billing.subscription_service import get_tenant_subscription

    subscription = get_tenant_subscription(db, tenant.id)
    if subscription is not None and subscription.plan is not None:
        return subscription.plan.price_cop or None
    try:
        return get_default_plan(db).price_cop or None
    except RuntimeError:
        return None


def compute_results(
    db: Session,
    tenant: Tenant,
    *,
    start: datetime,
    end: datetime,
    profile: Optional[TenantProfile] = None,
    plan_price_cop: Optional[int] = None,
) -> ResultsSummary:
    profile = profile or tenant.profile
    schedule = get_schedule(profile) if profile else None
    open_time = time.fromisoformat(schedule.open_time) if schedule else time(8, 0)
    close_time = time.fromisoformat(schedule.close_time) if schedule else time(18, 0)
    working_days = _working_days(db, tenant.id)

    booked_at = [
        row.created_at
        for row in db.query(Appointment.created_at).filter(
            Appointment.tenant_id == tenant.id,
            Appointment.source == AI_SOURCE,
            Appointment.created_at >= start,
            Appointment.created_at < end,
        )
    ]
    after_hours_appointments = sum(
        1
        for moment in booked_at
        if is_after_hours(moment, open_time=open_time, close_time=close_time, working_days=working_days)
    )

    replies = db.query(AiReplyEvent).filter(
        AiReplyEvent.tenant_id == tenant.id,
        AiReplyEvent.created_at >= start,
        AiReplyEvent.created_at < end,
    )
    ai_replies = replies.count()
    clients_attended = replies.with_entities(func.count(func.distinct(AiReplyEvent.contact_key))).scalar() or 0
    local_ts = func.timezone("America/Bogota", AiReplyEvent.created_at)
    local_clock = cast(local_ts, Time)
    outside = [local_clock < open_time, local_clock >= close_time]
    if working_days is not None:
        # isodow: 1 = lunes … 7 = domingo.
        outside.append(~func.extract("isodow", local_ts).in_([d + 1 for d in working_days]))
    after_hours_replies = replies.filter(or_(*outside)).count()

    ticket = profile.avg_ticket_cop if profile else None
    return ResultsSummary(
        start=start,
        end=end,
        ai_appointments=len(booked_at),
        after_hours_appointments=after_hours_appointments,
        ai_replies=ai_replies,
        after_hours_replies=after_hours_replies,
        clients_attended=int(clients_attended),
        avg_ticket_cop=ticket,
        revenue_cop=len(booked_at) * ticket if ticket else None,
        plan_price_cop=plan_price_cop if plan_price_cop is not None else _plan_price(db, tenant),
    )


def tenant_results(db: Session, tenant: Tenant, now: Optional[datetime] = None) -> dict[str, Any]:
    """Todo lo que muestran el panel y Mi plan: este mes, últimos 30 días y desde el inicio."""
    now = now or datetime.now(timezone.utc)
    profile = tenant.profile
    price = _plan_price(db, tenant)
    month_start, _ = month_period(now)
    since = tenant.created_at or month_start

    def summary(start: datetime) -> ResultsSummary:
        return compute_results(db, tenant, start=start, end=now, profile=profile, plan_price_cop=price)

    all_time = summary(since)
    return {
        "month": summary(month_start).to_dict(),
        "month_name": month_name(now),
        "last_30_days": summary(now - timedelta(days=30)).to_dict(),
        "all_time": all_time.to_dict(),
        "months_active": max(1, round((now - since).days / 30)),
        "avg_ticket_cop": profile.avg_ticket_cop if profile else None,
        "plan_price_cop": price,
    }


# —— Textos ——


def format_cop(amount: int) -> str:
    return "$" + f"{int(amount):,}".replace(",", ".")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _multiple(value: float) -> str:
    return f"{value:.1f}".replace(".", ",").replace(",0", "")


def results_paragraphs(summary: ResultsSummary, *, period: str) -> list[str]:
    """Párrafos del resumen. `period`: "En septiembre", "En los últimos 30 días"…"""
    lines: list[str] = []
    if summary.ai_appointments:
        booked = f"{period} la IA de Omitel agendó {_plural(summary.ai_appointments, 'cita', 'citas')} por WhatsApp"
        if summary.after_hours_appointments:
            booked += (
                f", {summary.after_hours_appointments} de ellas fuera de tu horario, cuando nadie habría contestado"
            )
        lines.append(booked + ".")
        if summary.revenue_cop:
            lines.append(
                f"Con un servicio promedio de {format_cop(summary.avg_ticket_cop)}, eso son unos "
                f"<strong>{format_cop(summary.revenue_cop)}</strong> en ventas."
            )
    if summary.ai_replies:
        replied = (
            f"{'Además respondió' if lines else period + ' la IA respondió'} "
            f"{_plural(summary.ai_replies, 'mensaje', 'mensajes')} a "
            f"{_plural(summary.clients_attended, 'cliente', 'clientes')}"
        )
        if summary.hours_saved >= 1:
            replied += f" (≈ {_plural(summary.hours_saved, 'hora', 'horas')} de trabajo que no tuviste que hacer)"
        lines.append(replied + ".")
    if summary.roi_multiple and summary.roi_multiple >= 1:
        lines.append(
            f"Tu plan cuesta {format_cop(summary.plan_price_cop)}: te devolvió "
            f"<strong>{_multiple(summary.roi_multiple)} veces</strong> lo que pagas."
        )
    return lines


def results_whatsapp_text(business_name: str, summary: ResultsSummary, *, period: str, panel_url: str) -> str:
    lines = [f"📊 *{business_name}* — {period.lower()} con Omitel:", ""]
    if summary.ai_appointments:
        booked = f"📅 {_plural(summary.ai_appointments, 'cita agendada', 'citas agendadas')} por la IA"
        if summary.after_hours_appointments:
            booked += f" ({summary.after_hours_appointments} fuera de horario)"
        lines.append(booked)
    if summary.revenue_cop:
        lines.append(f"💰 ≈ {format_cop(summary.revenue_cop)} en ventas")
    if summary.ai_replies:
        lines.append(
            f"💬 {_plural(summary.ai_replies, 'mensaje respondido', 'mensajes respondidos')} a "
            f"{_plural(summary.clients_attended, 'cliente', 'clientes')}"
        )
    if summary.roi_multiple and summary.roi_multiple >= 1:
        lines.append(f"🚀 {_multiple(summary.roi_multiple)} veces lo que cuesta tu plan")
    lines += ["", f"Mira el detalle en tu panel: {panel_url}"]
    return "\n".join(lines)


def value_paragraph(
    db: Session,
    tenant: Tenant,
    now: Optional[datetime] = None,
    *,
    days: int = 30,
    period: str = "",
) -> Optional[str]:
    """Una frase para los correos de cobro. None si la IA no agendó nada en ese periodo."""
    now = now or datetime.now(timezone.utc)
    summary = compute_results(db, tenant, start=now - timedelta(days=days), end=now)
    if not summary.ai_appointments:
        return None
    text = (
        f"{period or f'En los últimos {days} días'} la IA agendó "
        f"{_plural(summary.ai_appointments, 'cita', 'citas')} por WhatsApp"
    )
    if summary.revenue_cop:
        text += f", unos <strong>{format_cop(summary.revenue_cop)}</strong> en ventas"
        if summary.roi_multiple and summary.roi_multiple >= 1:
            text += f": {_multiple(summary.roi_multiple)} veces lo que cuesta tu plan"
    return text + "."
