"""Cobro automático con fuentes de pago Wompi (tarjeta o Nequi) y cancelación.

Reglas (Estatuto del Consumidor, Ley 1480 de 2011):
- El cobro automático solo se activa con autorización expresa del dueño, que queda auditada.
- Se avisa por correo antes de cada cobro, con monto, fecha y enlace para cancelar.
- Cancelar es tan fácil como suscribirse y el plan sigue activo hasta el fin del periodo pagado.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.application.billing.checkout_service import (
    fulfill_checkout,
    new_reference,
    transaction_matches_checkout,
)
from app.application.billing.plan_service import get_plan_by_slug
from app.application.billing.subscription_service import CHARGE_LEAD, subscription_is_paid, trial_has_expired
from app.application.billing.tenant_service import log_audit
from app.application.billing.wompi_service import (
    WompiError,
    cop_to_wompi_cents,
    create_payment_source,
    create_source_transaction,
    fetch_transaction,
    find_transaction_by_reference,
    void_payment_source,
    wompi_sync_enabled,
)
from app.config import settings
from app.shared.core.rate_limit import consume_once
from app.domain.entities import Plan, Subscription, SubscriptionStatus, Tenant, TenantPlan, User
from app.domain.entities.enums import UserRole
from app.domain.entities.payment_checkout import PaymentCheckout, PaymentCheckoutKind, PaymentCheckoutStatus
from app.infrastructure.cache.redis_client import cache_set, tenant_cache_key
from app.infrastructure.email.email_service import send_billing_email

log = logging.getLogger(__name__)

REMINDER_LEAD = timedelta(days=3)
MAX_RENEWAL_ATTEMPTS = 3
RETRY_DELAY = timedelta(days=1)
PENDING_SYNC_AFTER = timedelta(minutes=2)
LOST_CHARGE_AFTER = timedelta(minutes=10)

SOURCE_TYPES = ("CARD", "NEQUI")
CANCEL_REASONS = {
    "precio": "Es muy caro",
    "resultados": "No vi resultados",
    "tecnico": "Problemas técnicos",
    "no_necesito": "Ya no lo necesito",
    "otra_herramienta": "Me cambio a otra herramienta",
    "otro": "Otro motivo",
}

_BOGOTA = ZoneInfo("America/Bogota")
_MONTHS = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


class AutoRenewError(ValueError):
    pass


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def format_date(value: datetime) -> str:
    local = _aware(value).astimezone(_BOGOTA)
    return f"{local.day} de {_MONTHS[local.month - 1]} de {local.year}"


def format_cop(amount: int) -> str:
    return "$" + f"{int(amount):,}".replace(",", ".")


def _manage_url() -> str:
    return f"{settings.site_public_url.rstrip('/')}/mi-plan"


def _owner(db: Session, tenant_id: Any) -> Optional[User]:
    return (
        db.query(User)
        .filter(User.tenant_id == tenant_id, User.role == UserRole.OWNER.value, User.is_active.is_(True))
        .order_by(User.created_at.asc())
        .first()
    )


def _notify(db: Session, tenant_id: Any, *, subject: str, title: str, paragraphs: list[str], cta: str) -> None:
    owner = _owner(db, tenant_id)
    if owner is None:
        return
    try:
        send_billing_email(
            to_email=owner.email,
            subject=subject,
            title=title,
            paragraphs=paragraphs,
            cta_label=cta,
            cta_url=_manage_url(),
        )
    except Exception:
        log.exception("No se pudo enviar el aviso de facturación «%s» tenant=%s", subject, tenant_id)


def _payment_label(source_type: str, source: dict[str, Any], hint: dict[str, str]) -> str:
    public = source.get("public_data") or {}
    if source_type == "NEQUI":
        phone = re.sub(r"\D", "", str(public.get("phone_number") or hint.get("phone_last_four") or ""))
        return f"Nequi •••• {phone[-4:]}" if phone else "Nequi"
    brand = re.sub(r"[^A-Za-z ]", "", str(public.get("brand") or hint.get("brand") or "Tarjeta"))[:20]
    last_four = re.sub(r"\D", "", str(public.get("last_four") or hint.get("last_four") or ""))[-4:]
    return f"{brand.upper() or 'TARJETA'} •••• {last_four}" if last_four else brand.upper() or "Tarjeta"


def save_payment_method(
    db: Session,
    *,
    tenant: Tenant,
    user: User,
    subscription: Subscription,
    source_type: str,
    token: str,
    label_hint: dict[str, str],
    plan_slug: Optional[str],
    ip_address: Optional[str],
) -> Optional[PaymentCheckout]:
    """Guarda la fuente de pago y activa el cobro automático.

    Si no hay un periodo pagado vigente (o se elige otro plan) cobra de inmediato;
    devuelve ese cobro para que el cliente consulte su estado.
    """
    if not wompi_sync_enabled():
        raise AutoRenewError("Los pagos automáticos aún no están configurados en el servidor.")
    source_type = (source_type or "").upper()
    if source_type not in SOURCE_TYPES:
        raise AutoRenewError("Medio de pago no soportado.")
    token = (token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{8,200}", token):
        raise AutoRenewError("El token de Wompi no es válido.")
    token_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not consume_once(f"wompi-token:{token_key}", ttl_seconds=86_400):
        raise AutoRenewError("Ese medio de pago ya se usó. Vuelve a tokenizar la tarjeta o Nequi.")

    plan: Plan = subscription.plan
    if plan_slug:
        chosen = get_plan_by_slug(db, plan_slug)
        if chosen is None or not chosen.is_public:
            raise AutoRenewError("Plan no disponible")
        plan = chosen

    try:
        source = create_payment_source(source_type=source_type, token=token, customer_email=user.email.lower())
    except WompiError as exc:
        raise AutoRenewError(f"Wompi rechazó el medio de pago: {exc}") from exc
    source_id = str(source.get("id") or "").strip()
    if not source_id or str(source.get("status") or "").upper() != "AVAILABLE":
        raise AutoRenewError("Wompi no aprobó el medio de pago. Revisa los datos e intenta de nuevo.")

    previous_source = subscription.payment_source_id
    now = datetime.now(timezone.utc)
    label = _payment_label(source_type, source, label_hint)
    subscription.payment_source_id = source_id
    subscription.payment_method_type = source_type
    subscription.payment_method_label = label
    subscription.auto_renew = True
    subscription.auto_renew_accepted_at = now
    subscription.cancel_at_period_end = False
    subscription.cancelled_at = None
    subscription.renewal_attempts = 0
    subscription.next_renewal_attempt_at = None

    log_audit(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        action="billing.auto_renew_authorized",
        details={
            "payment_method": label,
            "plan_slug": plan.slug,
            "amount_cop": plan.price_cop,
            "interval_days": 30,
            "wompi_payment_source_id": source_id,
        },
        ip_address=ip_address,
    )
    db.flush()

    if previous_source and previous_source != source_id:
        try:
            void_payment_source(previous_source)
        except WompiError:
            log.warning("No se pudo anular la fuente anterior %s tenant=%s", previous_source, tenant.id)

    is_paid = subscription_is_paid(subscription, now)
    if not is_paid or plan.id != subscription.plan_id:
        return _charge(db, subscription=subscription, plan=plan, kind=PaymentCheckoutKind.AUTO, now=now)
    period_end = subscription.current_period_end
    if period_end is not None and _aware(period_end) - CHARGE_LEAD <= now:
        return _charge(db, subscription=subscription, plan=plan, kind=PaymentCheckoutKind.RENEWAL, now=now)
    return None


def _charge(
    db: Session,
    *,
    subscription: Subscription,
    plan: Plan,
    kind: str,
    now: datetime,
) -> PaymentCheckout:
    owner = _owner(db, subscription.tenant_id)
    email = owner.email.lower() if owner else ""
    amount_in_cents = cop_to_wompi_cents(plan.price_cop)
    checkout = PaymentCheckout(
        tenant_id=subscription.tenant_id,
        plan_id=plan.id,
        reference=new_reference(subscription.tenant_id),
        amount_in_cents=amount_in_cents,
        currency="COP",
        status=PaymentCheckoutStatus.PENDING,
        customer_email=email,
        kind=kind,
    )
    db.add(checkout)
    # Queda registrado antes de llamar a Wompi: si el proceso muere a mitad, el barrido
    # lo encuentra pendiente y lo concilia en vez de cobrar dos veces.
    db.commit()

    try:
        transaction = create_source_transaction(
            reference=checkout.reference,
            amount_in_cents=amount_in_cents,
            customer_email=email,
            payment_source_id=str(subscription.payment_source_id),
            source_type=str(subscription.payment_method_type or "CARD"),
        )
    except WompiError as exc:
        fulfill_checkout(db, checkout=checkout, wompi_transaction_id=None, wompi_status="ERROR", failure_reason=str(exc))
        db.commit()
        return checkout

    transaction_id = str(transaction.get("id") or "") or None
    checkout.wompi_transaction_id = transaction_id
    fulfill_checkout(
        db,
        checkout=checkout,
        wompi_transaction_id=transaction_id,
        wompi_status=str(transaction.get("status") or "PENDING"),
        failure_reason=transaction.get("status_message"),
    )
    db.commit()
    return checkout


def on_auto_charge_approved(db: Session, *, checkout: PaymentCheckout, subscription: Subscription, plan: Plan) -> None:
    subscription.renewal_attempts = 0
    subscription.next_renewal_attempt_at = None
    end = subscription.current_period_end
    _notify(
        db,
        subscription.tenant_id,
        subject="Recibimos tu pago de Omitel",
        title="¡Pago recibido!",
        paragraphs=[
            f"Cobramos {format_cop(checkout.amount_in_cents // 100)} a {subscription.payment_method_label or 'tu medio de pago'} "
            f"por tu {plan.name}.",
            f"Tu plan queda activo hasta el {format_date(end)}." if end else "Tu plan quedó activo.",
            f"Referencia: {checkout.reference}",
        ],
        cta="Ver mi plan",
    )


def register_renewal_failure(db: Session, *, checkout: PaymentCheckout) -> None:
    subscription = db.query(Subscription).filter(Subscription.tenant_id == checkout.tenant_id).first()
    if subscription is None:
        return
    now = datetime.now(timezone.utc)
    subscription.renewal_attempts = (subscription.renewal_attempts or 0) + 1
    final = subscription.renewal_attempts >= MAX_RENEWAL_ATTEMPTS
    subscription.next_renewal_attempt_at = None if final else now + RETRY_DELAY
    log.warning(
        "Cobro automático rechazado tenant=%s intento=%s motivo=%s",
        checkout.tenant_id,
        subscription.renewal_attempts,
        checkout.failure_reason,
    )
    if subscription.renewal_attempts != 1 and not final:
        return
    end = subscription.current_period_end
    grace_end = _aware(end) + timedelta(days=settings.subscription_grace_days) if end else None
    if final:
        paragraphs = [
            f"No pudimos cobrar tu plan a {subscription.payment_method_label or 'tu medio de pago'} después de "
            f"{MAX_RENEWAL_ATTEMPTS} intentos.",
            (f"Tu plan se pausará el {format_date(grace_end)} si no actualizas el medio de pago o pagas manualmente."
             if grace_end else "Actualiza tu medio de pago para no perder el servicio."),
        ]
    else:
        paragraphs = [
            f"El banco rechazó el cobro a {subscription.payment_method_label or 'tu medio de pago'}"
            + (f" ({checkout.failure_reason})." if checkout.failure_reason else "."),
            "Lo intentaremos de nuevo mañana. Si cambiaste de tarjeta, actualízala para no perder el servicio.",
        ]
    _notify(
        db,
        subscription.tenant_id,
        subject="No pudimos cobrar tu plan de Omitel",
        title="Problema con tu pago",
        paragraphs=paragraphs,
        cta="Actualizar medio de pago",
    )


def cancel_subscription(
    db: Session,
    *,
    subscription: Subscription,
    user: User,
    reason: str,
    feedback: str,
    ip_address: Optional[str],
) -> None:
    if subscription.status == SubscriptionStatus.TRIAL.value or trial_has_expired(subscription):
        raise AutoRenewError("Estás en prueba gratis: no hay ningún cobro que cancelar.")
    if subscription.status == SubscriptionStatus.CANCELLED.value or subscription.cancel_at_period_end:
        raise AutoRenewError("Tu suscripción ya está cancelada.")

    now = datetime.now(timezone.utc)
    reason = reason if reason in CANCEL_REASONS else "otro"
    subscription.auto_renew = False
    subscription.next_renewal_attempt_at = None
    subscription.cancelled_at = now
    subscription.cancel_reason = reason
    subscription.cancel_feedback = (feedback or "").strip()[:500] or None

    paid = subscription_is_paid(subscription, now)
    end = subscription.current_period_end
    if paid and end is not None and _aware(end) > now:
        subscription.cancel_at_period_end = True
        access_text = f"Tu plan sigue activo hasta el {format_date(end)}. Después no se hará ningún cobro."
    else:
        _end_subscription(db, subscription)
        access_text = "Tu suscripción terminó y no se harán más cobros."

    log_audit(
        db,
        tenant_id=subscription.tenant_id,
        user_id=user.id,
        action="subscription.cancelled",
        details={"reason": reason, "at_period_end": subscription.cancel_at_period_end},
        ip_address=ip_address,
    )
    _notify(
        db,
        subscription.tenant_id,
        subject="Cancelaste tu suscripción a Omitel",
        title="Suscripción cancelada",
        paragraphs=[
            access_text,
            "Si cambias de opinión puedes reactivarla desde Mi plan antes de esa fecha, sin perder nada.",
        ],
        cta="Ver mi plan",
    )


def resume_subscription(db: Session, *, subscription: Subscription, user: User, ip_address: Optional[str]) -> None:
    now = datetime.now(timezone.utc)
    if not subscription.cancel_at_period_end or not subscription_is_paid(subscription, now):
        raise AutoRenewError("No hay una cancelación pendiente que deshacer.")
    subscription.cancel_at_period_end = False
    subscription.cancelled_at = None
    subscription.cancel_reason = None
    subscription.cancel_feedback = None
    subscription.auto_renew = bool(subscription.payment_source_id)
    log_audit(
        db,
        tenant_id=subscription.tenant_id,
        user_id=user.id,
        action="subscription.resumed",
        details={"auto_renew": subscription.auto_renew},
        ip_address=ip_address,
    )


def remove_payment_method(db: Session, *, subscription: Subscription, user: User, ip_address: Optional[str]) -> None:
    source_id = subscription.payment_source_id
    if not source_id:
        raise AutoRenewError("No tienes un medio de pago guardado.")
    try:
        void_payment_source(source_id)
    except WompiError:
        log.warning("No se pudo anular la fuente %s en Wompi; se desvincula igual", source_id)
    label = subscription.payment_method_label
    subscription.payment_source_id = None
    subscription.payment_method_type = None
    subscription.payment_method_label = None
    subscription.auto_renew = False
    subscription.next_renewal_attempt_at = None
    log_audit(
        db,
        tenant_id=subscription.tenant_id,
        user_id=user.id,
        action="billing.payment_method_removed",
        details={"payment_method": label},
        ip_address=ip_address,
    )


def _end_subscription(db: Session, subscription: Subscription) -> None:
    subscription.status = SubscriptionStatus.CANCELLED.value
    subscription.cancel_at_period_end = False
    subscription.auto_renew = False
    tenant = db.get(Tenant, subscription.tenant_id)
    if tenant is not None:
        tenant.plan = TenantPlan.SUSPENDED.value
        tenant.daily_bait_limit = 0
        cache_set(tenant_cache_key(str(tenant.id), "plan"), tenant.plan, ttl_seconds=900)


# —— Barrido periódico ——


def _send_reminders(db: Session, now: datetime) -> int:
    due = (
        db.query(Subscription)
        .filter(
            Subscription.auto_renew.is_(True),
            Subscription.cancel_at_period_end.is_(False),
            Subscription.status == SubscriptionStatus.ACTIVE.value,
            Subscription.payment_source_id.isnot(None),
            Subscription.current_period_end.isnot(None),
            Subscription.current_period_end > now,
            Subscription.current_period_end <= now + REMINDER_LEAD,
        )
        .all()
    )
    sent = 0
    for subscription in due:
        end = _aware(subscription.current_period_end)
        if subscription.renewal_reminder_for is not None and _aware(subscription.renewal_reminder_for) == end:
            continue
        plan = subscription.plan
        _notify(
            db,
            subscription.tenant_id,
            subject="Pronto renovamos tu plan de Omitel",
            title="Aviso de cobro automático",
            paragraphs=[
                f"El {format_date(end - CHARGE_LEAD)} cobraremos {format_cop(plan.price_cop)} a "
                f"{subscription.payment_method_label or 'tu medio de pago'} para renovar tu {plan.name} por 30 días más.",
                "No tienes que hacer nada. Si prefieres no renovar, puedes cancelar desde Mi plan antes de esa fecha.",
            ],
            cta="Gestionar mi plan",
        )
        subscription.renewal_reminder_for = end
        sent += 1
    if due:
        db.commit()
    return sent


def _charge_due_renewals(db: Session, now: datetime) -> int:
    due_ids = [
        row.id
        for row in db.query(Subscription.id)
        .filter(
            Subscription.auto_renew.is_(True),
            Subscription.cancel_at_period_end.is_(False),
            Subscription.payment_source_id.isnot(None),
            Subscription.status.in_([SubscriptionStatus.ACTIVE.value, SubscriptionStatus.PAST_DUE.value]),
            Subscription.current_period_end.isnot(None),
            Subscription.current_period_end <= now + CHARGE_LEAD,
            Subscription.renewal_attempts < MAX_RENEWAL_ATTEMPTS,
        )
        .all()
    ]
    charged = 0
    for subscription_id in due_ids:
        # Bloqueo por fila: con varios workers barriendo a la vez, solo uno cobra.
        subscription = (
            db.query(Subscription)
            .filter(Subscription.id == subscription_id)
            .with_for_update(skip_locked=True)
            .first()
        )
        if subscription is None:
            db.rollback()
            continue
        retry_at = subscription.next_renewal_attempt_at
        in_flight = (
            db.query(PaymentCheckout.id)
            .filter(
                PaymentCheckout.tenant_id == subscription.tenant_id,
                PaymentCheckout.kind.in_([PaymentCheckoutKind.AUTO, PaymentCheckoutKind.RENEWAL]),
                PaymentCheckout.status == PaymentCheckoutStatus.PENDING,
            )
            .first()
        )
        still_due = (
            subscription.auto_renew
            and not subscription.cancel_at_period_end
            and subscription.current_period_end is not None
            and _aware(subscription.current_period_end) <= now + CHARGE_LEAD
            and (retry_at is None or _aware(retry_at) <= now)
        )
        if in_flight or not still_due:
            db.rollback()
            continue
        _charge(db, subscription=subscription, plan=subscription.plan, kind=PaymentCheckoutKind.RENEWAL, now=now)
        charged += 1
    return charged


def _reconcile_pending(db: Session, now: datetime) -> int:
    pending = (
        db.query(PaymentCheckout)
        .filter(
            or_(
                PaymentCheckout.kind.in_([PaymentCheckoutKind.AUTO, PaymentCheckoutKind.RENEWAL]),
                # Pagos del widget que Wompi avisó pero no se pudieron confirmar en su API.
                and_(
                    PaymentCheckout.kind == PaymentCheckoutKind.CHECKOUT,
                    PaymentCheckout.wompi_transaction_id.isnot(None),
                ),
            ),
            PaymentCheckout.status == PaymentCheckoutStatus.PENDING,
            PaymentCheckout.created_at <= now - PENDING_SYNC_AFTER,
        )
        .all()
    )
    resolved = 0
    for checkout in pending:
        try:
            if checkout.wompi_transaction_id:
                transaction = fetch_transaction(checkout.wompi_transaction_id)
            elif _aware(checkout.created_at) <= now - LOST_CHARGE_AFTER:
                transaction = find_transaction_by_reference(checkout.reference) or {"status": "ERROR"}
            else:
                continue
        except WompiError:
            continue
        if not transaction:
            continue
        status = str(transaction.get("status") or "").upper()
        if status == "PENDING":
            continue
        if status == "APPROVED" and not transaction_matches_checkout(checkout, transaction):
            continue
        fulfill_checkout(
            db,
            checkout=checkout,
            wompi_transaction_id=str(transaction.get("id") or "") or checkout.wompi_transaction_id,
            wompi_status=status,
            failure_reason=transaction.get("status_message") or ("Sin respuesta de Wompi" if status == "ERROR" else None),
        )
        db.commit()
        resolved += 1
    return resolved


def _end_cancelled_periods(db: Session, now: datetime) -> int:
    ending = (
        db.query(Subscription)
        .filter(
            Subscription.cancel_at_period_end.is_(True),
            Subscription.status == SubscriptionStatus.ACTIVE.value,
            Subscription.current_period_end.isnot(None),
            Subscription.current_period_end <= now,
        )
        .all()
    )
    for subscription in ending:
        _end_subscription(db, subscription)
        log.info("Suscripción cancelada al fin de periodo tenant=%s", subscription.tenant_id)
    if ending:
        db.commit()
    return len(ending)


def _end_expired_trials(db: Session, now: datetime) -> int:
    expired_ids = [
        row.id
        for row in db.query(Subscription.id).filter(
            Subscription.status == SubscriptionStatus.TRIAL.value,
            Subscription.trial_ends_at.isnot(None),
            Subscription.trial_ends_at <= now,
        )
    ]
    ended = 0
    for subscription_id in expired_ids:
        # Con varios workers barriendo, solo el que logra el cambio de estado avisa al dueño.
        claimed = (
            db.query(Subscription)
            .filter(Subscription.id == subscription_id, Subscription.status == SubscriptionStatus.TRIAL.value)
            .update({Subscription.status: SubscriptionStatus.PAST_DUE.value}, synchronize_session=False)
        )
        if not claimed:
            db.rollback()
            continue
        subscription = db.get(Subscription, subscription_id)
        tenant = db.get(Tenant, subscription.tenant_id)
        if tenant is not None:
            tenant.plan = TenantPlan.SUSPENDED.value
            tenant.daily_bait_limit = 0
            cache_set(tenant_cache_key(str(tenant.id), "plan"), tenant.plan, ttl_seconds=900)
        log_audit(
            db,
            tenant_id=subscription.tenant_id,
            user_id=None,
            action="billing.trial_expired",
            details={"trial_ends_at": _aware(subscription.trial_ends_at).isoformat()},
            message="Terminó la prueba gratis: la IA dejó de responder hasta que active el plan",
        )
        db.commit()
        plan = subscription.plan
        _notify(
            db,
            subscription.tenant_id,
            subject="Terminó tu prueba gratis de Omitel",
            title="Tu prueba gratis terminó",
            paragraphs=[
                "Tu asistente dejó de responder los chats de WhatsApp porque terminaron tus días de prueba.",
                f"Activa el {plan.name} por {format_cop(plan.price_cop)} al mes y sigue vendiendo donde lo dejaste: "
                "tus chats, atajos y configuración siguen guardados.",
            ],
            cta="Activar mi plan",
        )
        ended += 1
    return ended


def process_auto_renewals(db: Session, now: Optional[datetime] = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    steps: list[tuple[str, Callable[[Session, datetime], int]]] = [
        ("trials_ended", _end_expired_trials),
        ("ended", _end_cancelled_periods),
        ("reconciled", _reconcile_pending),
        ("reminded", _send_reminders),
    ]
    if wompi_sync_enabled():
        steps.append(("charged", _charge_due_renewals))
    result: dict[str, int] = {}
    for name, step in steps:
        try:
            result[name] = step(db, now)
        except Exception:
            db.rollback()
            log.exception("Error en el barrido de cobro automático (%s)", name)
            result[name] = 0
    return result
