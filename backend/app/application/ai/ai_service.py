from __future__ import annotations

import logging
import random
import time
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.application.ai.ai_classifier_service import classify_inbound_message
from app.application.ai.ai_conversation_service import generate_qualify_reply, generate_reply
from app.application.ai.ai_mode_service import is_classify_only, is_qualify_mode, resolve_ai_mode
from app.application.ai.ai_qualify_service import detect_purchase_intent, handoff_reply
from app.application.ai.ai_shortcut_service import send_reply_with_shortcut
from app.application.ai.ai_usage_service import (
    check_daily_classify_quota,
    check_daily_reply_quota,
    increment_daily_classify_count,
    increment_daily_reply_count,
)
from app.application.billing.tenant_profile_service import get_or_create_tenant_profile
from app.application.messaging.message_service import _detect_media_type_from_body
from app.application.outbound.outbound_dedup_service import mark_phone_excluded
from app.application.outbound.quick_shortcut_service import get_shortcuts
from app.application.realtime.realtime_service import publish_conversation_updated, publish_panel_event
from app.application.whatsapp.whatsapp_service import send_text_message
from app.config import settings
from app.domain.entities import Conversation, Exclusion, Message, Tenant, TenantProfile, WhatsAppSession
from app.domain.entities.enums import (
    ConversationInterest,
    ConversationMode,
    ConversationStatus,
    MessageDirection,
    MessageSource,
)
from app.infrastructure.ai.deepseek_client import DeepSeekError

log = logging.getLogger(__name__)

OPT_OUT_REPLY = (
    "Entendido, no te escribiremos más. Disculpa la molestia. "
    "Si en algún momento cambias de opinión, aquí estaremos."
)
NO_INTEREST_REPLY = (
    "Perfecto, gracias por avisar. Que tengas un excelente día. "
    "Si más adelante te interesa, con gusto te ayudamos."
)
FALLBACK_REPLY = (
    "¡Hola! Gracias por escribir. Cuéntame qué te gustaría saber y con gusto te ayudo."
)


def ai_block_reason(tenant: Tenant, conversation: Conversation) -> Optional[str]:
    if not tenant.ai_global_enabled:
        return "IA global apagada en el panel"
    if not conversation.ai_active:
        return "IA desactivada en este chat"
    if conversation.mode != ConversationMode.AUTO.value:
        return "Modo manual activo en este chat"
    if conversation.status == ConversationStatus.EXCLUDED.value:
        return "Contacto excluido"
    return None


def should_ai_respond(tenant: Tenant, conversation: Conversation) -> bool:
    return ai_block_reason(tenant, conversation) is None


def is_respondable_text(body: str) -> bool:
    text = (body or "").strip()
    if not text:
        return False
    if _detect_media_type_from_body(text):
        return False
    return True


def _interest_from_category(category: str) -> Optional[str]:
    if category in ("interesado", "duda"):
        return ConversationInterest.INTERESTED.value
    if category == "no_interesado":
        return ConversationInterest.NOT_INTERESTED.value
    return None


def maybe_schedule_ai_for_conversation(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> bool:
    """Encola IA si el último mensaje es entrante y aplica procesamiento."""
    from app.application.ai.ai_queue_service import enqueue_ai_reply_ids

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    conversation = (
        db.query(Conversation)
        .filter(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id)
        .first()
    )
    if not tenant or not conversation or not should_ai_respond(tenant, conversation):
        return False

    profile = get_or_create_tenant_profile(db, tenant_id)
    classify_mode = is_classify_only(profile)

    latest_in = (
        db.query(Message)
        .filter(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.IN.value,
        )
        .order_by(Message.created_at.desc())
        .first()
    )
    if latest_in is None or not is_respondable_text(latest_in.body):
        return False

    if not classify_mode:
        replied = (
            db.query(Message.id)
            .filter(
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.OUT.value,
                Message.source == MessageSource.BOT.value,
                Message.created_at >= latest_in.created_at,
            )
            .first()
        )
        if replied is not None:
            return False

    enqueue_ai_reply_ids(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        message_id=latest_in.id,
        db=db,
    )
    return True


def register_opt_out(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    reason: str = "opt_out",
) -> None:
    phone = conversation.contact_phone
    if not phone or phone.startswith("lid:"):
        conversation.status = ConversationStatus.EXCLUDED.value
        conversation.interest_status = ConversationInterest.NOT_INTERESTED.value
        conversation.ai_active = False
        return

    existing = (
        db.query(Exclusion)
        .filter(Exclusion.tenant_id == tenant.id, Exclusion.phone_e164 == phone)
        .first()
    )
    if existing is None:
        db.add(
            Exclusion(
                tenant_id=tenant.id,
                phone_e164=phone,
                reason=reason[:255],
            )
        )

    mark_phone_excluded(tenant.id, phone)
    conversation.status = ConversationStatus.EXCLUDED.value
    conversation.interest_status = ConversationInterest.NOT_INTERESTED.value
    conversation.ai_active = False
    publish_conversation_updated(tenant.id, conversation)


def _is_latest_inbound(db: Session, conversation_id: uuid.UUID, message_id: uuid.UUID) -> bool:
    latest = (
        db.query(Message)
        .filter(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.IN.value,
        )
        .order_by(Message.created_at.desc())
        .first()
    )
    return latest is not None and latest.id == message_id


def _load_history(db: Session, conversation_id: uuid.UUID) -> list[Message]:
    return (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
        .all()
    )


def _publish_quota_error(tenant: Tenant, conversation_id: uuid.UUID, err: str) -> None:
    publish_panel_event(
        tenant.id,
        {
            "type": "ai.error",
            "conversation_id": str(conversation_id),
            "error": err,
        },
    )


def _publish_ai_send_error(
    tenant: Tenant, conversation_id: uuid.UUID, err: str
) -> None:
    publish_panel_event(
        tenant.id,
        {
            "type": "ai.error",
            "conversation_id": str(conversation_id),
            "error": err[:300],
        },
    )


def _publish_ai_error_if_actionable(
    tenant: Tenant, conversation_id: uuid.UUID, err: str
) -> None:
    """Solo alerta en panel si el usuario debe actuar (cuota, key, saldo)."""
    text = (err or "").lower()
    actionable = any(
        k in text
        for k in (
            "límite",
            "limite",
            "quota",
            "saldo",
            "402",
            "401",
            "inválida",
            "invalida",
            "no configurada",
            "deepseek_api_key",
        )
    )
    if actionable:
        _publish_quota_error(tenant, conversation_id, err[:300])


def _run_classification(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    message: Message,
    profile: Optional[TenantProfile],
) -> Optional[dict]:
    try:
        return classify_inbound_message(
            message.body,
            business_name=tenant.business_name,
        )
    except Exception:
        log.exception("Error clasificando mensaje conv=%s", conversation.id)
        return None


def _process_classify_only(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    message: Message,
    session: WhatsAppSession,
    profile: Optional[TenantProfile],
) -> bool:
    """Solo etiqueta interesado / no interesado — sin cerrar venta ni charlar."""
    quota_ok, _, _, quota_err = check_daily_classify_quota(db, tenant)
    if not quota_ok:
        log.warning("Clasificación quota tenant=%s: %s", tenant.id, quota_err)
        _publish_quota_error(tenant, conversation.id, quota_err or "Límite diario")
        return True

    classification = _run_classification(
        db, tenant=tenant, conversation=conversation, message=message, profile=profile
    )
    if classification is None:
        return True

    category = classification.get("category", "duda")
    log.info(
        "IA classify-only tenant=%s conv=%s category=%s",
        tenant.id,
        conversation.id,
        category,
    )

    if category == "ruido":
        return True

    if category == "opt_out":
        register_opt_out(db, tenant=tenant, conversation=conversation)
        try:
            send_text_message(
                db,
                tenant=tenant,
                session=session,
                conversation=conversation,
                text=OPT_OUT_REPLY,
                source=MessageSource.BOT.value,
            )
        except Exception:
            log.exception("Error enviando opt_out conv=%s", conversation.id)
        increment_daily_classify_count(tenant.id)
        return True

    interest = _interest_from_category(category)
    if interest:
        conversation.interest_status = interest
        if interest == ConversationInterest.NOT_INTERESTED.value:
            conversation.ai_active = False

    increment_daily_classify_count(tenant.id)
    publish_conversation_updated(tenant.id, conversation)
    return True


def _handoff_to_human(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    session: WhatsAppSession,
) -> bool:
    """Clasifica como interesado y avisa que un humano cierra — sin agendar ni vender."""
    conversation.interest_status = ConversationInterest.INTERESTED.value
    conversation.mode = ConversationMode.MANUAL.value
    publish_conversation_updated(tenant.id, conversation)

    quota_ok, _, _, quota_err = check_daily_reply_quota(db, tenant)
    if not quota_ok:
        log.warning("IA quota tenant=%s handoff: %s", tenant.id, quota_err)
        _publish_quota_error(tenant, conversation.id, quota_err or "Límite diario IA")
        return True

    try:
        send_text_message(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            text=handoff_reply(tenant.business_name),
            source=MessageSource.BOT.value,
        )
        increment_daily_reply_count(tenant.id)
    except Exception:
        log.exception("Error enviando handoff conv=%s", conversation.id)
        return False
    return True


def _process_qualify(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    message: Message,
    session: WhatsAppSession,
    profile: Optional[TenantProfile],
) -> bool:
    """Saluda, responde dudas y clasifica interés — el humano cierra la venta."""
    classification = _run_classification(
        db, tenant=tenant, conversation=conversation, message=message, profile=profile
    )
    if classification is None:
        return True

    category = classification.get("category", "duda")
    log.info(
        "IA qualify tenant=%s conv=%s category=%s",
        tenant.id,
        conversation.id,
        category,
    )

    if category == "ruido":
        return True

    if category == "opt_out":
        register_opt_out(db, tenant=tenant, conversation=conversation)
        try:
            send_text_message(
                db,
                tenant=tenant,
                session=session,
                conversation=conversation,
                text=OPT_OUT_REPLY,
                source=MessageSource.BOT.value,
            )
        except Exception:
            log.exception("Error enviando opt_out conv=%s", conversation.id)
        return True

    if category == "no_interesado":
        conversation.interest_status = ConversationInterest.NOT_INTERESTED.value
        conversation.ai_active = False
        publish_conversation_updated(tenant.id, conversation)
        try:
            send_text_message(
                db,
                tenant=tenant,
                session=session,
                conversation=conversation,
                text=NO_INTEREST_REPLY,
                source=MessageSource.BOT.value,
            )
        except Exception:
            log.exception("Error enviando no_interesado conv=%s", conversation.id)
        return True

    if detect_purchase_intent(message.body):
        return _handoff_to_human(
            db, tenant=tenant, conversation=conversation, session=session
        )

    quota_ok, _, _, quota_err = check_daily_reply_quota(db, tenant)
    if not quota_ok:
        log.warning("IA quota tenant=%s: %s", tenant.id, quota_err)
        _publish_quota_error(tenant, conversation.id, quota_err or "Límite diario IA")
        return True

    history = _load_history(db, conversation.id)
    inbound_count = sum(1 for m in history if m.direction == MessageDirection.IN.value)
    is_first_contact = inbound_count <= 1
    shortcuts = get_shortcuts(db, tenant.id)

    reply = FALLBACK_REPLY
    generated_reply = None
    used_fallback = False
    try:
        generated_reply = generate_qualify_reply(
            tenant=tenant,
            profile=profile,
            contact_name=conversation.contact_name,
            history=history,
            is_first_contact=is_first_contact,
            shortcuts=shortcuts,
        )
        if generated_reply.message.strip():
            reply = generated_reply.message.strip()
            increment_daily_reply_count(tenant.id)
    except DeepSeekError as exc:
        used_fallback = True
        log.warning("IA qualify fallback conv=%s: %s", conversation.id, exc)
        _publish_ai_error_if_actionable(tenant, conversation.id, str(exc)[:300])

    if not reply.strip():
        return True

    try:
        if generated_reply is not None and not used_fallback:
            send_reply_with_shortcut(
                db,
                tenant=tenant,
                session=session,
                conversation=conversation,
                reply=generated_reply,
            )
        else:
            send_text_message(
                db,
                tenant=tenant,
                session=session,
                conversation=conversation,
                text=reply.strip(),
                source=MessageSource.BOT.value,
            )
        if used_fallback:
            log.info("IA qualify respondió con fallback conv=%s", conversation.id)
    except Exception as exc:
        log.exception("Error enviando respuesta qualify conv=%s", conversation.id)
        _publish_ai_send_error(
            tenant,
            conversation.id,
            f"No se pudo enviar la respuesta por WhatsApp: {exc}"[:300],
        )
        return False

    return True


def _process_full_reply(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    message: Message,
    session: WhatsAppSession,
    profile: Optional[TenantProfile],
    classification: dict,
) -> bool:
    category = classification.get("category", "duda")

    if category == "ruido":
        return True

    if category == "opt_out":
        register_opt_out(db, tenant=tenant, conversation=conversation)
        try:
            send_text_message(
                db,
                tenant=tenant,
                session=session,
                conversation=conversation,
                text=OPT_OUT_REPLY,
                source=MessageSource.BOT.value,
            )
        except Exception:
            log.exception("Error enviando opt_out conv=%s", conversation.id)
        return True

    if category == "no_interesado":
        conversation.interest_status = ConversationInterest.NOT_INTERESTED.value
        conversation.ai_active = False
        publish_conversation_updated(tenant.id, conversation)
        try:
            send_text_message(
                db,
                tenant=tenant,
                session=session,
                conversation=conversation,
                text=NO_INTEREST_REPLY,
                source=MessageSource.BOT.value,
            )
        except Exception:
            log.exception("Error enviando no_interesado conv=%s", conversation.id)
        return True

    interest = _interest_from_category(category)
    if interest == ConversationInterest.INTERESTED.value:
        conversation.interest_status = interest
        publish_conversation_updated(tenant.id, conversation)

    quota_ok, _, _, quota_err = check_daily_reply_quota(db, tenant)
    if not quota_ok:
        log.warning("IA quota tenant=%s: %s", tenant.id, quota_err)
        _publish_quota_error(tenant, conversation.id, quota_err or "Límite diario IA")
        return True

    history = _load_history(db, conversation.id)
    shortcuts = get_shortcuts(db, tenant.id)
    reply = FALLBACK_REPLY
    generated_reply = None
    used_fallback = False
    try:
        generated_reply = generate_reply(
            tenant=tenant,
            profile=profile,
            contact_name=conversation.contact_name,
            history=history,
            shortcuts=shortcuts,
        )
        if generated_reply.message.strip():
            reply = generated_reply.message.strip()
            increment_daily_reply_count(tenant.id)
    except DeepSeekError as exc:
        used_fallback = True
        log.warning("IA fallback conv=%s: %s", conversation.id, exc)
        _publish_ai_error_if_actionable(tenant, conversation.id, str(exc)[:300])

    if not reply.strip():
        return True

    try:
        if generated_reply is not None and not used_fallback:
            send_reply_with_shortcut(
                db,
                tenant=tenant,
                session=session,
                conversation=conversation,
                reply=generated_reply,
            )
        else:
            send_text_message(
                db,
                tenant=tenant,
                session=session,
                conversation=conversation,
                text=reply.strip(),
                source=MessageSource.BOT.value,
            )
        if used_fallback:
            log.info("IA respondió con fallback conv=%s", conversation.id)
    except Exception as exc:
        log.exception("Error enviando respuesta IA conv=%s", conversation.id)
        _publish_ai_send_error(
            tenant,
            conversation.id,
            f"No se pudo enviar la respuesta por WhatsApp: {exc}"[:300],
        )
        return False

    return True


def process_ai_reply(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
) -> bool:
    """Procesa un job de IA: clasificar y/o responder según ai_mode del negocio."""
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    conversation = (
        db.query(Conversation)
        .filter(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id)
        .first()
    )
    message = (
        db.query(Message)
        .filter(
            Message.id == message_id,
            Message.conversation_id == conversation_id,
            Message.tenant_id == tenant_id,
        )
        .first()
    )
    session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant_id).first()

    if not tenant or not conversation or not message or not session:
        if not message:
            log.warning(
                "IA job sin mensaje conv=%s msg=%s (¿race o mensaje borrado?)",
                conversation_id,
                message_id,
            )
        return True

    if message.direction != MessageDirection.IN.value:
        return True

    if not _is_latest_inbound(db, conversation_id, message_id):
        log.debug("IA skip: hay mensaje más reciente conv=%s", conversation_id)
        return True

    if not should_ai_respond(tenant, conversation):
        reason = ai_block_reason(tenant, conversation) or "desconocido"
        log.info("IA skip conv=%s: %s", conversation_id, reason)
        return True

    if not is_respondable_text(message.body):
        log.info("IA skip conv=%s: mensaje no respondible (%r)", conversation_id, message.body[:80])
        return True

    profile = get_or_create_tenant_profile(db, tenant_id)
    classify_mode = is_classify_only(profile)

    delay = (
        settings.ai_classify_delay_seconds
        if classify_mode
        else random.uniform(
            settings.ai_reply_delay_min_seconds,
            settings.ai_reply_delay_max_seconds,
        )
    )
    time.sleep(delay)

    db.refresh(conversation)
    db.refresh(tenant)
    if not should_ai_respond(tenant, conversation):
        return True
    if not _is_latest_inbound(db, conversation_id, message_id):
        return True

    if classify_mode:
        return _process_classify_only(
            db,
            tenant=tenant,
            conversation=conversation,
            message=message,
            session=session,
            profile=profile,
        )

    if is_qualify_mode(profile):
        return _process_qualify(
            db,
            tenant=tenant,
            conversation=conversation,
            message=message,
            session=session,
            profile=profile,
        )

    classification = _run_classification(
        db,
        tenant=tenant,
        conversation=conversation,
        message=message,
        profile=profile,
    )
    if classification is None:
        return True

    log.info(
        "IA classify tenant=%s conv=%s category=%s mode=%s",
        tenant_id,
        conversation_id,
        classification.get("category"),
        resolve_ai_mode(profile),
    )

    return _process_full_reply(
        db,
        tenant=tenant,
        conversation=conversation,
        message=message,
        session=session,
        profile=profile,
        classification=classification,
    )
