from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.application.ai.ai_appointment_service import (
    BOOKING_HANDOFF_MESSAGE,
    BookingContext,
    already_booked,
    assigned_staff_note,
    available_options_message,
    claims_booking,
    confirm_slot_message,
    denies_own_booking,
    detect_scheduling_interest,
    detect_slot_confirmation,
    infer_claimed_booking,
    nearest_slots,
    own_booking_message,
    pending_promise_note,
    promises_follow_up,
    requested_dates,
    requested_slot_note,
    requested_staff,
    slot_taken_message,
    stall_correction,
    stalls_on_agenda,
    try_create_booking,
)
from app.application.ai.ai_classifier_service import classify_inbound_message
from app.application.ai.ai_conversation_service import generate_qualify_reply, generate_reply
from app.application.ai.ai_mode_service import is_classify_only, is_qualify_mode, resolve_ai_mode
from app.application.ai.ai_qualify_service import (
    detect_closing_intent,
    detect_interest_signal,
    detect_purchase_intent,
    handoff_reply,
)
from app.application.ai.ai_shortcut_service import (
    STAGE_CHATTING,
    STAGE_CLOSING,
    STAGE_INTERESTED,
    STAGE_RANK,
    AiGeneratedReply,
    annotate_sent_shortcuts,
    asks_to_resend,
    infer_promised_shortcut,
    send_reply_with_shortcut,
)
from app.application.appointments.appointment_service import (
    BOGOTA,
    collect_upcoming_free_slots,
    describe_appointment,
    relink_orphan_appointments,
    staff_label,
    upcoming_appointments_for_conversation,
)
from app.application.ai.ai_usage_service import (
    check_daily_classify_quota,
    check_daily_reply_quota,
    increment_daily_classify_count,
    increment_daily_reply_count,
)
from app.application.billing.tenant_profile_service import get_or_create_tenant_profile
from app.application.platform.incidents import record_incident
from app.application.platform.tenant_overrides import feature_allowed
from app.application.ai.ai_transcription_service import (
    can_transcribe,
    ensure_transcript,
    is_image,
    is_voice_note,
)
from app.application.messaging.message_service import (
    _detect_media_type_from_body,
    is_reaction_only,
    text_for_ai,
)
from app.application.outbound.outbound_dedup_service import mark_phone_excluded
from app.application.outbound.quick_shortcut_service import get_shortcuts
from app.application.realtime.realtime_service import publish_conversation_updated, publish_panel_event
from app.application.whatsapp.whatsapp_service import send_text_message
from app.config import settings
from app.infrastructure.cache.redis_client import get_redis
from app.domain.entities import Conversation, Exclusion, Message, Tenant, TenantProfile, WhatsAppSession
from app.domain.entities.enums import (
    ConversationInterest,
    ConversationMode,
    ConversationStatus,
    MessageDirection,
    MessageSource,
)
from app.infrastructure.ai import gemini_client
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

# Si la IA no responde, el cliente no debe notarlo: primero reintentos en silencio, al segundo
# fallo un «ya te confirmo» como lo diría una persona, y si sigue caída el chat pasa al dueño
# como cualquier traspaso a humano.
OUTAGE_RETRY_DELAYS_SECONDS = (20, 60, 180)
OUTAGE_STATE_TTL_SECONDS = 3600
HOLDING_REPLY_COOLDOWN_SECONDS = 15 * 60
OWNER_HANDOFF_ALERT_COOLDOWN_SECONDS = 30 * 60
SEND_RETRY_DELAY_SECONDS = 30
HANDOFF_MARKER_TTL_SECONDS = 30 * 24 * 3600
# Chat pasado a una persona: si nadie le contesta al cliente en este tiempo, la IA lo retoma.
HANDOFF_RESCUE_SECONDS = 5 * 60
HANDOFF_WAIT_TTL_SECONDS = 24 * 3600
CLOSING_ALERT_COOLDOWN_SECONDS = 6 * 3600
BOOKING_DAYS_AHEAD = 14
BOOKING_DATE_LOOKBACK = 16  # últimos mensajes (cliente e IA) donde buscamos fechas de las que se habló
HOLDING_REPLIES_FIRST_CONTACT = (
    "¡Hola! 😊 Gracias por escribirnos. Dame un momentico y ya te ayudo.",
    "¡Hola! Qué gusto saludarte 😊 Dame un momentico y te cuento.",
)
HOLDING_REPLIES = (
    "Dame un momentico y te confirmo 🙌",
    "Ya te confirmo, dame un momentico 😊",
    "Un momentico y te cuento 🙌",
)
OUTAGE_HANDOFF_REPLY = "Le paso tu mensaje a alguien del equipo para que te responda personalmente 🙌"
UNREADABLE_AUDIO_REPLY = "No logré escuchar bien tu audio 🙈 ¿Me lo escribes porfa?"
UNREADABLE_IMAGE_REPLY = "No me cargó bien la imagen 🙈 ¿Me cuentas qué estás buscando?"


def ai_block_reason(tenant: Tenant, conversation: Conversation) -> Optional[str]:
    if not tenant.is_active:
        return "Empresa suspendida"
    if not feature_allowed(tenant, "ai_replies"):
        return "IA pausada por soporte de Omitel"
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
    if _detect_media_type_from_body(text) or is_reaction_only(text):
        return False
    return True


def _detect_interest(category: str, body: str) -> Optional[str]:
    """La categoría «interesado» del clasificador es el default de cualquier charla;
    solo la intención de compra cuenta como interés real."""
    if category == "no_interesado":
        return ConversationInterest.NOT_INTERESTED.value
    if detect_purchase_intent(body):
        return ConversationInterest.INTERESTED.value
    return None


def _handoff_key(conversation_id: uuid.UUID) -> str:
    return f"ai:handed_off:{conversation_id}"


def _was_handed_off(conversation_id: uuid.UUID) -> bool:
    """Si la IA ya entregó el chat y el agente la volvió a activar, no se despide otra vez."""
    try:
        return bool(get_redis().exists(_handoff_key(conversation_id)))
    except Exception:
        return False


def _mark_handed_off(conversation_id: uuid.UUID) -> None:
    try:
        get_redis().set(_handoff_key(conversation_id), "1", ex=HANDOFF_MARKER_TTL_SECONDS)
    except Exception:
        log.warning("No se pudo marcar traspaso conv=%s", conversation_id)


def _awaiting_key(conversation_id: uuid.UUID) -> str:
    return f"ai:awaiting_human:{conversation_id}"


def _rescued_key(conversation_id: uuid.UUID) -> str:
    return f"ai:rescued:{conversation_id}"


def _schedule_rescue(tenant_id: uuid.UUID, conversation_id: uuid.UUID, message_id: uuid.UUID) -> None:
    from app.application.ai.ai_queue_service import JOB_RESCUE, schedule_ai_retry

    try:
        schedule_ai_retry(
            tenant_id, conversation_id, message_id, delay_seconds=HANDOFF_RESCUE_SECONDS, kind=JOB_RESCUE
        )
    except Exception:
        log.warning("No se pudo programar el rescate del chat conv=%s", conversation_id)


def _await_human(tenant_id: uuid.UUID, conversation_id: uuid.UUID, message_id: uuid.UUID) -> None:
    """El chat pasó a una persona. Si nadie le responde al cliente, la IA lo retoma."""
    try:
        get_redis().set(_awaiting_key(conversation_id), str(time.time()), ex=HANDOFF_WAIT_TTL_SECONDS)
    except Exception:
        log.warning("No se pudo marcar el chat como esperando a una persona conv=%s", conversation_id)
        return
    _schedule_rescue(tenant_id, conversation_id, message_id)


def _awaiting_human_since(conversation_id: uuid.UUID) -> Optional[datetime]:
    try:
        raw = get_redis().get(_awaiting_key(conversation_id))
    except Exception:
        return None
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def clear_awaiting_human(conversation_id: uuid.UUID) -> None:
    """El dueño tomó una decisión sobre el chat: la IA no lo retoma por su cuenta."""
    try:
        get_redis().delete(_awaiting_key(conversation_id))
    except Exception:
        pass


def schedule_rescue_if_awaiting(
    tenant_id: uuid.UUID, conversation_id: uuid.UUID, message_id: uuid.UUID
) -> bool:
    """El cliente volvió a escribir en un chat que la IA pasó a una persona."""
    if _awaiting_human_since(conversation_id) is None:
        return False
    _schedule_rescue(tenant_id, conversation_id, message_id)
    return True


def _was_rescued(conversation_id: uuid.UUID) -> bool:
    try:
        return bool(get_redis().exists(_rescued_key(conversation_id)))
    except Exception:
        return False


def _remind_owner_of_waiting_client(tenant: Tenant, conversation: Conversation, *, resumed: bool) -> None:
    from app.application.conversations.interest_alert_service import alert_phones, send_alerts

    phones = alert_phones(tenant.profile)
    session = tenant.whatsapp_session
    if not phones or session is None:
        return
    try:
        key = f"ai:rescue_alert:{conversation.id}:{'resumed' if resumed else 'waiting'}"
        if not get_redis().set(key, "1", nx=True, ex=HANDOFF_WAIT_TTL_SECONDS):
            return
    except Exception:
        return
    minutes = HANDOFF_RESCUE_SECONDS // 60
    if resumed:
        text = (
            f"⏰ *{tenant.business_name}*: nadie le respondió a {_contact_label(conversation)} en "
            f"{minutes} min, así que la IA retomó el chat para que no se enfríe.\n\n"
            f"Si prefieres atenderlo tú, apaga la IA en ese chat: {_panel_link()}"
        )
    else:
        text = (
            f"⏰ *{tenant.business_name}*: {_contact_label(conversation)} lleva {minutes} min "
            f"esperando respuesta.\n\nRespóndele aquí: {_panel_link()}"
        )
    if not send_alerts(session, phones, text):
        log.warning("No se pudo recordar al dueño el chat en espera conv=%s", conversation.id)


def rescue_unanswered_handoff(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
) -> bool:
    """Pasaron unos minutos desde que el chat quedó en manos de una persona. Si nadie le respondió
    al cliente y él volvió a escribir, la IA retoma; si no escribió, solo se le recuerda al dueño."""
    since = _awaiting_human_since(conversation_id)
    if since is None:
        return True
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    conversation = (
        db.query(Conversation)
        .filter(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id)
        .first()
    )
    if tenant is None or conversation is None:
        return True
    if should_ai_respond(tenant, conversation):
        clear_awaiting_human(conversation_id)  # alguien ya reactivó la IA
        return True
    human_replied = (
        db.query(Message.id)
        .filter(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.OUT.value,
            Message.source == MessageSource.AGENT.value,
            Message.created_at >= since,
        )
        .first()
    )
    if human_replied is not None:
        clear_awaiting_human(conversation_id)
        return True

    latest_in = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id, Message.direction == MessageDirection.IN.value)
        .order_by(Message.created_at.desc())
        .first()
    )
    pending = latest_in is not None and not (
        db.query(Message.id)
        .filter(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.OUT.value,
            Message.created_at > latest_in.created_at,
        )
        .first()
    )
    blocked_by_owner = (
        not tenant.is_active
        or not tenant.ai_global_enabled
        or not feature_allowed(tenant, "ai_replies")
        or conversation.status == ConversationStatus.EXCLUDED.value
    )
    if not pending or blocked_by_owner:
        _remind_owner_of_waiting_client(tenant, conversation, resumed=False)
        return True

    conversation.ai_active = True
    conversation.mode = ConversationMode.AUTO.value
    clear_awaiting_human(conversation_id)
    try:
        get_redis().set(_rescued_key(conversation_id), "1", ex=HANDOFF_WAIT_TTL_SECONDS)
    except Exception:
        pass
    db.commit()
    publish_conversation_updated(tenant.id, conversation)
    log.info("Nadie respondió al cliente: la IA retoma el chat conv=%s", conversation_id)
    record_incident(
        tenant.id,
        "system.ai_rescued_handoff",
        f"Nadie le respondió a {_contact_label(conversation)}: la IA retomó el chat",
        details={"conversation_id": str(conversation_id)},
        throttle_scope=str(conversation_id),
    )
    _remind_owner_of_waiting_client(tenant, conversation, resumed=True)
    return process_ai_reply(db, tenant_id=tenant_id, conversation_id=conversation_id, message_id=latest_in.id)


def _close_on_interest(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    session: WhatsAppSession,
    message: Message,
    category: str,
    send_farewell: bool = True,
) -> bool:
    """La primera vez que se detecta interés o desinterés la IA se apaga y el chat pasa a manual.
    Si el agente reactivó la IA después, solo se actualiza la etiqueta. True = no seguir respondiendo."""
    interest = _detect_interest(category, text_for_ai(message))
    if interest is None:
        return False

    if _was_handed_off(conversation.id):
        conversation.interest_status = interest
        publish_conversation_updated(tenant.id, conversation)
        return False

    conversation.interest_status = interest
    conversation.ai_active = False
    conversation.mode = ConversationMode.MANUAL.value
    _mark_handed_off(conversation.id)
    if interest == ConversationInterest.INTERESTED.value:
        _await_human(tenant.id, conversation.id, message.id)
    publish_conversation_updated(tenant.id, conversation)

    if not send_farewell:
        return True
    text = (
        handoff_reply(tenant.business_name)
        if interest == ConversationInterest.INTERESTED.value
        else NO_INTEREST_REPLY
    )
    try:
        send_text_message(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            text=text,
            source=MessageSource.BOT.value,
        )
    except Exception:
        log.exception("Error enviando cierre %s conv=%s", interest, conversation.id)
    return True


def maybe_schedule_ai_for_conversation(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    allow_stale: bool = False,
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

    latest_in = _latest_meaningful_inbound(db, conversation_id)
    if latest_in is None:
        recent = _recent_inbound(db, conversation_id)
        if classify_mode or not recent or not _is_textless_opening(db, conversation_id, recent[0]):
            return False
        latest_in = recent[0]
    elif not is_respondable_text(text_for_ai(latest_in)) and not can_transcribe(latest_in):
        return False

    if not classify_mode and _bot_already_replied(db, conversation_id, latest_in):
        return False

    # El panel abierto refresca cada pocos segundos: sin esto, un envío que falla siempre
    # (contacto inalcanzable) gasta una respuesta de la IA en cada refresco.
    if not allow_stale and _send_failed_recently(latest_in.id):
        return False

    return enqueue_ai_reply_ids(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        message_id=latest_in.id,
        db=db,
        allow_stale=allow_stale,
    )


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


def _is_ai_noise(message: Message) -> bool:
    """Reacciones, stickers y adjuntos sin texto: solos no piden respuesta, y no deben tapar la
    pregunta que el cliente mandó justo antes («hola» + sticker)."""
    if is_reaction_only(message.body):
        return True
    if is_voice_note(message) or is_image(message):
        # Se transcriben / describen; sin Gemini no hay forma de entenderlos.
        return message.transcript is None and not gemini_client.is_configured()
    return not is_respondable_text(text_for_ai(message))


def _recent_inbound(db: Session, conversation_id: uuid.UUID) -> list[Message]:
    return (
        db.query(Message)
        .filter(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.IN.value,
        )
        .order_by(Message.created_at.desc())
        .limit(20)
        .all()
    )


def _latest_meaningful_inbound(db: Session, conversation_id: uuid.UUID) -> Optional[Message]:
    """Último entrante con algo que responder (texto, audio o imagen)."""
    return next((m for m in _recent_inbound(db, conversation_id) if not _is_ai_noise(m)), None)


def _is_latest_inbound(db: Session, conversation_id: uuid.UUID, message_id: uuid.UUID) -> bool:
    latest = _latest_meaningful_inbound(db, conversation_id)
    return latest is not None and latest.id == message_id


def _is_textless_opening(db: Session, conversation_id: uuid.UUID, message: Message) -> bool:
    """El cliente abrió el chat solo con un sticker o un adjunto: una persona saludaría de vuelta."""
    if is_reaction_only(message.body) or not _is_ai_noise(message):
        return False
    if is_voice_note(message) or is_image(message):
        return False
    has_outbound = (
        db.query(Message.id)
        .filter(Message.conversation_id == conversation_id, Message.direction == MessageDirection.OUT.value)
        .first()
        is not None
    )
    if has_outbound:
        return False
    recent = _recent_inbound(db, conversation_id)
    return bool(recent) and recent[0].id == message.id and all(_is_ai_noise(m) for m in recent)


def _inbound_count(db: Session, conversation_id: uuid.UUID) -> int:
    return (
        db.query(Message.id)
        .filter(Message.conversation_id == conversation_id, Message.direction == MessageDirection.IN.value)
        .count()
    )


def _bot_already_replied(
    db: Session,
    conversation_id: uuid.UUID,
    message: Message,
    *,
    ignore_message_id: Optional[str] = None,
) -> bool:
    """`ignore_message_id`: el «dame un momentico» no cuenta como respuesta real."""
    query = db.query(Message.id).filter(
        Message.conversation_id == conversation_id,
        Message.direction == MessageDirection.OUT.value,
        Message.source == MessageSource.BOT.value,
        Message.created_at >= message.created_at,
    )
    if ignore_message_id:
        try:
            query = query.filter(Message.id != uuid.UUID(ignore_message_id))
        except ValueError:
            pass
    return query.first() is not None


def _redis_text(value) -> Optional[str]:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


def _outage_key(message_id: uuid.UUID) -> str:
    return f"ai:outage:{message_id}"


def _holding_key(message_id: uuid.UUID) -> str:
    return f"ai:holding:{message_id}"


def _holding_message_id(message_id: uuid.UUID) -> Optional[str]:
    try:
        return _redis_text(get_redis().get(_holding_key(message_id)))
    except Exception:
        return None


def _bump_counter(key: str) -> Optional[int]:
    try:
        client = get_redis()
        count = int(client.incr(key))
        client.expire(key, OUTAGE_STATE_TTL_SECONDS)
        return count
    except Exception:
        return None


def _send_failed_recently(message_id: uuid.UUID) -> bool:
    try:
        return bool(get_redis().exists(f"ai:sendfail:{message_id}"))
    except Exception:
        return False


def _clear_outage_state(message_id: uuid.UUID) -> None:
    try:
        get_redis().delete(_outage_key(message_id), f"ai:sendfail:{message_id}")
    except Exception:
        pass


def _send_holding_reply(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    message: Message,
    session: WhatsAppSession,
    is_first_contact: bool,
) -> None:
    try:
        if not get_redis().set(
            f"ai:holding_conv:{conversation.id}", "1", nx=True, ex=HOLDING_REPLY_COOLDOWN_SECONDS
        ):
            return
    except Exception:
        return
    text = random.choice(HOLDING_REPLIES_FIRST_CONTACT if is_first_contact else HOLDING_REPLIES)
    try:
        sent = send_text_message(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            text=text,
            source=MessageSource.BOT.value,
        )
        get_redis().set(_holding_key(message.id), str(sent.id), ex=OUTAGE_STATE_TTL_SECONDS)
    except Exception:
        log.exception("No se pudo enviar mensaje de espera conv=%s", conversation.id)


def _notify_owner_of_handoff(tenant: Tenant, conversation: Conversation) -> None:
    from app.application.conversations.interest_alert_service import alert_phones, send_alerts
    from app.shared.core.phone import resolve_display_name

    phones = alert_phones(tenant.profile)
    session = tenant.whatsapp_session
    if not phones or session is None:
        return
    try:
        if not get_redis().set(
            f"ai:handoff_owner_alert:{tenant.id}", "1", nx=True, ex=OWNER_HANDOFF_ALERT_COOLDOWN_SECONDS
        ):
            return
    except Exception:
        return
    name = resolve_display_name(
        conversation.contact_name, conversation.contact_phone, contact_jid=conversation.contact_jid or ""
    )
    text = (
        f"🔔 *{tenant.business_name}*: {name} está esperando respuesta. "
        "Te pasamos el chat para que le respondas tú.\n\n"
        f"Respóndele aquí: {settings.app_public_url.rstrip('/')}/panel"
    )
    if not send_alerts(session, phones, text):
        log.warning("No se pudo avisar al dueño del traspaso tenant=%s", tenant.id)


def _hand_off_after_outage(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    message: Message,
    session: WhatsAppSession,
) -> None:
    from app.application.monitoring.dev_alerts import alert_dev

    conversation.mode = ConversationMode.MANUAL.value
    _await_human(tenant.id, conversation.id, message.id)
    publish_conversation_updated(tenant.id, conversation)
    try:
        send_text_message(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            text=OUTAGE_HANDOFF_REPLY,
            source=MessageSource.BOT.value,
        )
    except Exception:
        log.exception("No se pudo enviar traspaso a humano conv=%s", conversation.id)
    publish_panel_event(
        tenant.id,
        {
            "type": "ai.handoff",
            "conversation_id": str(conversation.id),
            "message": "Este cliente está esperando respuesta: el chat pasó a ti para que le respondas.",
        },
    )
    _notify_owner_of_handoff(tenant, conversation)
    record_incident(
        tenant.id,
        "system.ai_outage_handoff",
        f"La IA no respondió y el chat de {_contact_label(conversation)} pasó al dueño",
        details={"conversation_id": str(conversation.id)},
        throttle_scope=str(conversation.id),
    )
    alert_dev(
        f"ai:handoff:{tenant.id}",
        "Chats pasados al dueño porque la IA no respondió",
        f"Negocio: {tenant.business_name} ({tenant.id})\nConversación: {conversation.id}",
        severity="critical",
        throttle_seconds=1800,
    )


def _handle_ai_unavailable(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    message: Message,
    session: WhatsAppSession,
    is_first_contact: bool,
) -> bool:
    from app.application.ai.ai_queue_service import schedule_ai_retry

    attempt = _bump_counter(_outage_key(message.id))
    if attempt is not None and attempt <= len(OUTAGE_RETRY_DELAYS_SECONDS):
        if attempt >= 2:
            _send_holding_reply(
                db,
                tenant=tenant,
                conversation=conversation,
                message=message,
                session=session,
                is_first_contact=is_first_contact,
            )
        try:
            schedule_ai_retry(
                tenant.id,
                conversation.id,
                message.id,
                delay_seconds=OUTAGE_RETRY_DELAYS_SECONDS[attempt - 1],
            )
            return True
        except Exception:
            log.exception("No se pudo programar reintento IA conv=%s", conversation.id)

    _hand_off_after_outage(db, tenant=tenant, conversation=conversation, message=message, session=session)
    return True


def _handle_unreadable_media(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    message: Message,
    session: WhatsAppSession,
) -> bool:
    """Audio o imagen sin texto que no logramos entender: nunca dejar al cliente en visto."""
    if message.transcript is None:
        # No se pudo descargar o Gemini falló: se reintenta igual que una caída de la IA.
        return _handle_ai_unavailable(
            db,
            tenant=tenant,
            conversation=conversation,
            message=message,
            session=session,
            is_first_contact=_inbound_count(db, conversation.id) <= 1,
        )
    # Se procesó pero no hay nada entendible (audio sin voz, archivo enorme): pedirlo como lo haría una persona.
    text = UNREADABLE_AUDIO_REPLY if is_voice_note(message) else UNREADABLE_IMAGE_REPLY
    try:
        send_text_message(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            text=text,
            source=MessageSource.BOT.value,
        )
    except Exception:
        log.exception("No se pudo pedir que reenvíen el adjunto conv=%s", conversation.id)
        return False
    return True


def _send_generated_reply(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    message: Message,
    session: WhatsAppSession,
    reply,
) -> bool:
    try:
        send_reply_with_shortcut(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            reply=reply,
            allow_resend=asks_to_resend(text_for_ai(message)),
        )
    except Exception as exc:
        log.exception("Error enviando respuesta IA conv=%s", conversation.id)
        # Instancia borrada o sin permiso: reintentar solo gastaría otra respuesta de la IA.
        permanent = getattr(exc, "status_code", None) in (401, 403, 404)
        if not permanent and _bump_counter(f"ai:sendfail:{message.id}") == 1:
            # Un tropiezo de Evolution suele pasar solo: reintentar antes de molestar al dueño.
            from app.application.ai.ai_queue_service import schedule_ai_retry

            try:
                schedule_ai_retry(
                    tenant.id, conversation.id, message.id, delay_seconds=SEND_RETRY_DELAY_SECONDS
                )
                return False
            except Exception:
                pass
        record_incident(
            tenant.id,
            "system.ai_send_failed",
            f"No se pudo enviar la respuesta de la IA a {_contact_label(conversation)}",
            details={
                "conversation_id": str(conversation.id),
                "error": str(exc)[:300],
                "status_code": getattr(exc, "status_code", None),
            },
        )
        _publish_ai_send_error(tenant, conversation.id)
        return False
    _clear_outage_state(message.id)
    return True


def _booking_context(
    db: Session,
    *,
    tenant: Tenant,
    profile: Optional[TenantProfile],
    conversation: Conversation,
    history: list[Message],
) -> Optional[BookingContext]:
    if profile is None or not profile.ai_booking_enabled or not feature_allowed(tenant, "ai_booking"):
        return None
    today = datetime.now(BOGOTA).date()
    covered_until = today + timedelta(days=BOOKING_DAYS_AHEAD - 1)
    relink_orphan_appointments(db, conversation=conversation)
    upcoming = upcoming_appointments_for_conversation(
        db, tenant_id=tenant.id, conversation_id=conversation.id
    )
    # La fecha puede haberla dicho el cliente o la IA hace varios mensajes («el 21 con Juan»).
    talked = {d for m in history[-BOOKING_DATE_LOOKBACK:] for d in requested_dates(text_for_ai(m), today)}
    talked |= {row.starts_at.astimezone(BOGOTA).date() for row in upcoming}
    asked = {d for d in talked if d > covered_until}
    free_slots = collect_upcoming_free_slots(
        db, tenant_id=tenant.id, days=BOOKING_DAYS_AHEAD, limit=None, extra_days=asked
    )
    return BookingContext(
        free_slots=free_slots,
        existing_appointment="; ".join(
            describe_appointment(row) + (f" ({row.notes})" if row.notes else "") for row in upcoming
        ),
        staff_label=staff_label(profile),
        covered_until=covered_until.isoformat(),
        extra_days=tuple(d.isoformat() for d in sorted(asked)),
        booked=tuple(
            (
                row.starts_at.astimezone(BOGOTA).date().isoformat(),
                row.starts_at.astimezone(BOGOTA).strftime("%H:%M"),
                row.staff.name if row.staff is not None else "",
            )
            for row in upcoming
        ),
    )


def _day_in_talk(history: list[Message]) -> Optional[str]:
    """El último día concreto del que se habló, para ofrecer horas de ese día."""
    today = datetime.now(BOGOTA).date()
    for m in reversed(history[-BOOKING_DATE_LOOKBACK:]):
        days = requested_dates(text_for_ai(m), today)
        if days:
            return max(days).isoformat()
    return None


def _generate_without_stalling(generate, *, booking: Optional[BookingContext], history: list[Message], **kwargs):
    """Con la agenda delante, «déjame revisar y te confirmo» deja al cliente esperando a nadie."""
    if booking is None:
        return generate(history=history, booking=booking, **kwargs)
    inbound = [text_for_ai(m) for m in reversed(history) if m.direction == MessageDirection.IN.value][:3]
    previous_bot = next(
        (m.body or "" for m in reversed(history) if m.source == MessageSource.BOT.value), ""
    )
    day = _day_in_talk(history)
    # Tras un «ok», la hora que pidió está unos mensajes atrás.
    slot_note = next(
        (n for n in (requested_slot_note(t, booking.free_slots, day, booking.booked) for t in inbound) if n), ""
    )
    note = "\n".join(p for p in (slot_note, pending_promise_note(previous_bot)) if p)
    # «Te escribo apenas tenga la confirmación» no nombra la agenda; el mensaje anterior sí.
    context = " ".join([*(inbound[:1]), previous_bot])
    reply = generate(history=history, booking=booking, correction=note, **kwargs)
    if reply is None or not reply.message.strip():
        return reply
    if not stalls_on_agenda(reply.message, context, booking.free_slots):
        return reply
    log.info("IA dijo «ya te confirmo» teniendo la agenda; se le pide otra vez")
    try:
        retry = generate(
            history=history,
            booking=booking,
            correction="\n".join(p for p in (note, stall_correction(reply.message)) if p),
            **kwargs,
        )
    except DeepSeekError:
        retry = None
    if retry is not None and retry.message.strip() and not stalls_on_agenda(
        retry.message, context, booking.free_slots
    ):
        return retry
    return replace(
        reply,
        message=available_options_message(booking.free_slots, day=_day_in_talk(history)),
        book_slot=None,
        shortcut_id=None,
        stage=STAGE_INTERESTED,
    )


def _contact_label(conversation: Conversation) -> str:
    from app.shared.core.phone import resolve_display_name

    return resolve_display_name(
        conversation.contact_name, conversation.contact_phone, contact_jid=conversation.contact_jid or ""
    )


def _contact_line(conversation: Conversation) -> str:
    from app.shared.core.phone import format_display_phone, is_valid_whatsapp_phone

    label = _contact_label(conversation)
    phone = conversation.contact_phone or ""
    if is_valid_whatsapp_phone(phone) and label != format_display_phone(phone):
        return f"Cliente: {label} · {format_display_phone(phone)}"
    return f"Cliente: {label}"


def _send_sales_alert(
    tenant: Tenant, *, key: str, ttl_seconds: int, text: str, skip_phone: Optional[str] = None
) -> None:
    """Ventas listas y citas: al dueño y a quienes despachan."""
    from app.application.conversations.interest_alert_service import alert_phones, send_alerts
    from app.shared.core.phone import phone_match_tail

    phones = [
        p for p in alert_phones(tenant.profile, sales=True) if not (skip_phone and phone_match_tail(p, skip_phone))
    ]
    session = tenant.whatsapp_session
    if not phones or session is None:
        return
    try:
        if not get_redis().set(key, "1", nx=True, ex=ttl_seconds):
            return
    except Exception:
        return
    if not send_alerts(session, phones, text):
        log.warning("No se pudo enviar aviso al equipo tenant=%s key=%s", tenant.id, key)


def _notify_assigned_staff(tenant: Tenant, appointment) -> Optional[str]:
    """Avisa a quien atiende la cita. Devuelve su número si se le avisó."""
    from app.application.appointments.staff_notify import build_staff_notice, send_staff_notice

    notice = build_staff_notice(tenant, appointment)
    if notice is None:
        return None
    try:
        if not get_redis().set(f"ai:staff_notice:{appointment.id}", "1", nx=True, ex=24 * 3600):
            return notice.phone
    except Exception:
        return None
    return notice.phone if send_staff_notice(notice) else None


def _panel_link() -> str:
    return f"{settings.app_public_url.rstrip('/')}/panel"


def _resolve_stage(
    reply: AiGeneratedReply,
    *,
    message: Message,
    shortcuts: list[dict],
    booking: Optional[BookingContext],
) -> str:
    """La IA lee toda la conversación; las palabras clave solo suben la etapa, nunca la bajan."""
    text = text_for_ai(message)
    stage = reply.stage
    # «Sí, confirmo» justo después de que la IA agendó es sobre esa cita, no una venta por cerrar.
    scheduling = booking is not None and (
        reply.book_slot is not None
        or detect_scheduling_interest(text)
        or (bool(booking.booked) and detect_slot_confirmation(text))
    )
    if detect_closing_intent(text) and not scheduling:
        stage = _max_stage(stage, STAGE_CLOSING)
    elif detect_interest_signal(text):
        stage = _max_stage(stage, STAGE_INTERESTED)
    if reply.shortcut_id and any(
        str(s.get("id")) == reply.shortcut_id and s.get("type") in ("image", "document") for s in shortcuts
    ):
        stage = _max_stage(stage, STAGE_INTERESTED)
    if scheduling and stage == STAGE_CLOSING:
        # Con agenda activa, quien pide cita la agenda la IA: no hace falta pasar el chat.
        stage = STAGE_INTERESTED
    if promises_follow_up(reply.message):
        # Prometió escribir después y la IA no escribe sola: alguien tiene que cumplirlo.
        stage = STAGE_CLOSING
    return stage


def _max_stage(a: str, b: str) -> str:
    return a if STAGE_RANK.get(a, 0) >= STAGE_RANK.get(b, 0) else b


def _apply_stage(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    message: Message,
    stage: str,
) -> None:
    if stage == STAGE_CHATTING:
        return
    changed = conversation.interest_status != ConversationInterest.INTERESTED.value
    conversation.interest_status = ConversationInterest.INTERESTED.value
    if stage == STAGE_CLOSING:
        # Si la IA ya retomó este chat porque nadie respondía, no lo vuelve a soltar: el aviso basta.
        if not _was_rescued(conversation.id):
            conversation.ai_active = False
            conversation.mode = ConversationMode.MANUAL.value
            _mark_handed_off(conversation.id)
            _await_human(tenant.id, conversation.id, message.id)
        changed = True
        snippet = " ".join(text_for_ai(message).split())[:200]
        _send_sales_alert(
            tenant,
            key=f"ai:closing_alert:{conversation.id}",
            ttl_seconds=CLOSING_ALERT_COOLDOWN_SECONDS,
            text=(
                f"🔥 *{tenant.business_name}*: {_contact_label(conversation)} ya quiere comprar. "
                "Te dejamos el chat para que cierres la venta.\n\n"
                f"{_contact_line(conversation)}\n"
                f"Último mensaje: «{snippet}»\n\nRespóndele aquí: {_panel_link()}"
            ),
        )
        log.info("IA entrega chat listo para cerrar conv=%s", conversation.id)
        record_incident(
            tenant.id,
            "system.ai_handoff_closing",
            f"{_contact_label(conversation)} quiere comprar: la IA pasó el chat al dueño",
            details={"conversation_id": str(conversation.id)},
            throttle_seconds=CLOSING_ALERT_COOLDOWN_SECONDS,
            throttle_scope=str(conversation.id),
        )
    if changed:
        publish_conversation_updated(tenant.id, conversation)


def _deliver_reply(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    message: Message,
    session: WhatsAppSession,
    reply: AiGeneratedReply,
    shortcuts: list[dict],
    booking: Optional[BookingContext],
) -> bool:
    appointment = None
    repeated = None
    inferred = None
    if booking is not None and reply.book_slot is None:
        inferred = infer_claimed_booking(
            reply.message, booking.free_slots, today=datetime.now(BOGOTA).date()
        )
        if inferred is not None:
            log.info("IA confirmó cita sin book_slot; se agenda lo que dijo conv=%s", conversation.id)
            reply = replace(reply, book_slot=inferred)
    if reply.book_slot is not None and booking is not None:
        repeated = already_booked(
            db,
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            slot=reply.book_slot,
            client_text=text_for_ai(message),
            inferred=inferred is not None,
            reply_text=reply.message,
        )
    if repeated is not None:
        log.info("IA repitió una cita que ya existe; no se agenda otra conv=%s", conversation.id)
        reply = replace(reply, book_slot=None)
    elif reply.book_slot is not None and booking is not None:
        appointment = try_create_booking(
            db,
            tenant=tenant,
            conversation=conversation,
            slot=reply.book_slot,
            client_name=_contact_label(conversation),
        )
        if appointment is None:
            slot = reply.book_slot
            preferred = requested_staff(db, tenant.id, slot.staff)
            fresh = collect_upcoming_free_slots(
                db,
                tenant_id=tenant.id,
                days=BOOKING_DAYS_AHEAD,
                limit=None,
                staff_id=preferred.id if preferred else None,
                extra_days={date.fromisoformat(slot.date)},
            )
            options = nearest_slots(fresh, day=slot.date, start=slot.start)
            reply = replace(reply, message=slot_taken_message(options), shortcut_id=None, book_slot=None)
        else:
            reply = replace(reply, message=assigned_staff_note(reply.message, appointment))

    own = denies_own_booking(reply.message, booking.booked) if booking is not None and appointment is None else None
    if own is not None:
        log.warning("IA dijo que no había cupo en la hora de la cita del propio cliente conv=%s", conversation.id)
        reply = replace(reply, message=own_booking_message(own), book_slot=None, shortcut_id=None)

    if (
        appointment is None
        and repeated is None
        and claims_booking(reply.message)
        and not (booking is not None and booking.existing_appointment)
    ):
        # Nunca dejar al cliente creyendo que tiene cita si no quedó en la agenda.
        if booking is not None:
            log.warning("IA dijo que agendó sin horario válido; se pide el horario conv=%s", conversation.id)
            reply = replace(
                reply, message=confirm_slot_message(booking.free_slots), book_slot=None, shortcut_id=None
            )
        else:
            log.warning("IA dijo que agendó con la agenda apagada; se pasa al equipo conv=%s", conversation.id)
            reply = replace(reply, message=BOOKING_HANDOFF_MESSAGE, book_slot=None, stage=STAGE_CLOSING)

    if reply.shortcut_id is None:
        promised = infer_promised_shortcut(reply.message, text_for_ai(message), shortcuts)
        if promised:
            log.info("IA prometió un atajo sin adjuntarlo; se adjunta conv=%s", conversation.id)
            reply = replace(reply, shortcut_id=promised)

    if not _send_generated_reply(
        db, tenant=tenant, conversation=conversation, message=message, session=session, reply=reply
    ):
        return False

    if appointment is not None:
        log.info("IA agendó cita conv=%s appointment=%s", conversation.id, appointment.id)
        record_incident(
            tenant.id,
            "system.ai_booked_appointment",
            f"La IA agendó una cita para {_contact_label(conversation)} — {describe_appointment(appointment)}",
            details={"conversation_id": str(conversation.id), "appointment_id": str(appointment.id)},
            throttle_scope=str(appointment.id),
        )
        staff_phone = _notify_assigned_staff(tenant, appointment)
        _send_sales_alert(
            tenant,
            key=f"ai:booking_alert:{appointment.id}",
            ttl_seconds=24 * 3600,
            text=(
                f"📅 *{tenant.business_name}*: la IA agendó una cita para "
                f"{_contact_label(conversation)} — {describe_appointment(appointment)}.\n\n"
                f"{_contact_line(conversation)}\n"
                f"Ver agenda: {_panel_link()}"
            ),
            skip_phone=staff_phone,
        )
    _apply_stage(
        db,
        tenant=tenant,
        conversation=conversation,
        message=message,
        stage=_resolve_stage(reply, message=message, shortcuts=shortcuts, booking=booking),
    )
    return True


def _client_is_waiting_on_us(db: Session, conversation_id: uuid.UUID, message: Message) -> bool:
    """Un «ok» suelto no pide respuesta, salvo que conteste a una pregunta nuestra o a un
    «ya te confirmo»: ahí el cliente se queda esperando."""
    last_bot = (
        db.query(Message.body)
        .filter(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.OUT.value,
            Message.source == MessageSource.BOT.value,
            Message.created_at <= message.created_at,
            Message.id != message.id,
        )
        .order_by(Message.created_at.desc())
        .first()
    )
    if last_bot is None:
        return False
    body = last_bot[0] or ""
    return "?" in body or promises_follow_up(body)


def _load_history(db: Session, conversation_id: uuid.UUID) -> list[Message]:
    return (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
        .all()
    )


def _publish_quota_error(tenant: Tenant, conversation_id: uuid.UUID, err: str) -> None:
    record_incident(
        tenant.id,
        "system.ai_quota_reached",
        err,
        throttle_seconds=6 * 3600,
    )
    publish_panel_event(
        tenant.id,
        {
            "type": "ai.error",
            "conversation_id": str(conversation_id),
            "error": err,
        },
    )


def _publish_ai_send_error(tenant: Tenant, conversation_id: uuid.UUID) -> None:
    """Al dueño solo lo que él puede resolver; el detalle técnico va al dev por alerta."""
    publish_panel_event(
        tenant.id,
        {
            "type": "ai.error",
            "conversation_id": str(conversation_id),
            "error": "No pudimos enviar la respuesta automática. Revisa que tu WhatsApp siga vinculado.",
        },
    )


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
            text_for_ai(message),
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

    _close_on_interest(
        db,
        tenant=tenant,
        conversation=conversation,
        session=session,
        message=message,
        category=category,
        send_farewell=False,
    )

    increment_daily_classify_count(tenant.id)
    publish_conversation_updated(tenant.id, conversation)
    return True


def _process_qualify(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    message: Message,
    session: WhatsAppSession,
    profile: Optional[TenantProfile],
    classification: Optional[dict] = None,
) -> bool:
    """Saluda, responde dudas y clasifica interés — el humano cierra la venta."""
    classification = classification or _run_classification(
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

    if category == "ruido" and not _client_is_waiting_on_us(db, conversation.id, message):
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

    # El interés de compra lo decide la respuesta de la IA (etapa); aquí solo el «no, gracias».
    if category == "no_interesado" and _close_on_interest(
        db,
        tenant=tenant,
        conversation=conversation,
        session=session,
        message=message,
        category=category,
    ):
        return True

    quota_ok, _, _, quota_err = check_daily_reply_quota(db, tenant)
    if not quota_ok:
        log.warning("IA quota tenant=%s: %s", tenant.id, quota_err)
        _publish_quota_error(tenant, conversation.id, quota_err or "Límite diario IA")
        return True

    history = _load_history(db, conversation.id)
    inbound_count = sum(1 for m in history if m.direction == MessageDirection.IN.value)
    is_first_contact = inbound_count <= 1
    shortcuts = annotate_sent_shortcuts(get_shortcuts(db, tenant.id), conversation.id)
    booking = _booking_context(db, tenant=tenant, profile=profile, conversation=conversation, history=history)

    try:
        generated_reply = _generate_without_stalling(
            generate_qualify_reply,
            tenant=tenant,
            profile=profile,
            contact_name=conversation.contact_name,
            history=history,
            is_first_contact=is_first_contact,
            shortcuts=shortcuts,
            booking=booking,
        )
    except DeepSeekError as exc:
        log.warning("IA qualify sin respuesta conv=%s: %s", conversation.id, exc)
        generated_reply = None
    if generated_reply is None or not generated_reply.message.strip():
        return _handle_ai_unavailable(
            db,
            tenant=tenant,
            conversation=conversation,
            message=message,
            session=session,
            is_first_contact=is_first_contact,
        )

    increment_daily_reply_count(tenant.id)
    return _deliver_reply(
        db,
        tenant=tenant,
        conversation=conversation,
        message=message,
        session=session,
        reply=generated_reply,
        shortcuts=shortcuts,
        booking=booking,
    )


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

    if category == "ruido" and not _client_is_waiting_on_us(db, conversation.id, message):
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

    if category == "no_interesado" and _close_on_interest(
        db,
        tenant=tenant,
        conversation=conversation,
        session=session,
        message=message,
        category=category,
    ):
        return True

    quota_ok, _, _, quota_err = check_daily_reply_quota(db, tenant)
    if not quota_ok:
        log.warning("IA quota tenant=%s: %s", tenant.id, quota_err)
        _publish_quota_error(tenant, conversation.id, quota_err or "Límite diario IA")
        return True

    history = _load_history(db, conversation.id)
    shortcuts = annotate_sent_shortcuts(get_shortcuts(db, tenant.id), conversation.id)
    booking = _booking_context(db, tenant=tenant, profile=profile, conversation=conversation, history=history)
    try:
        generated_reply = _generate_without_stalling(
            generate_reply,
            tenant=tenant,
            profile=profile,
            contact_name=conversation.contact_name,
            history=history,
            shortcuts=shortcuts,
            booking=booking,
        )
    except DeepSeekError as exc:
        log.warning("IA sin respuesta conv=%s: %s", conversation.id, exc)
        generated_reply = None
    if generated_reply is None or not generated_reply.message.strip():
        inbound_count = sum(1 for m in history if m.direction == MessageDirection.IN.value)
        return _handle_ai_unavailable(
            db,
            tenant=tenant,
            conversation=conversation,
            message=message,
            session=session,
            is_first_contact=inbound_count <= 1,
        )

    increment_daily_reply_count(tenant.id)
    return _deliver_reply(
        db,
        tenant=tenant,
        conversation=conversation,
        message=message,
        session=session,
        reply=generated_reply,
        shortcuts=shortcuts,
        booking=booking,
    )


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

    def is_target() -> bool:
        return _is_latest_inbound(db, conversation_id, message_id) or _is_textless_opening(
            db, conversation_id, message
        )

    if not is_target():
        log.debug("IA skip: hay mensaje más reciente conv=%s", conversation_id)
        return True
    opening = _is_textless_opening(db, conversation_id, message)

    if not should_ai_respond(tenant, conversation):
        reason = ai_block_reason(tenant, conversation) or "desconocido"
        log.info("IA skip conv=%s: %s", conversation_id, reason)
        return True

    if can_transcribe(message):
        ensure_transcript(db, tenant=tenant, conversation=conversation, message=message)

    profile = get_or_create_tenant_profile(db, tenant_id)
    classify_mode = is_classify_only(profile)
    if classify_mode and opening:
        return True

    if not opening and not is_respondable_text(text_for_ai(message)):
        if not classify_mode and (is_voice_note(message) or is_image(message)):
            return _handle_unreadable_media(
                db, tenant=tenant, conversation=conversation, message=message, session=session
            )
        log.info("IA skip conv=%s: mensaje no respondible (%r)", conversation_id, message.body[:80])
        return True

    holding_id = _holding_message_id(message_id)

    if not classify_mode and _bot_already_replied(
        db, conversation_id, message, ignore_message_id=holding_id
    ):
        log.info("IA skip conv=%s: ya respondida msg=%s", conversation_id, message_id)
        return True

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
    if not is_target():
        return True
    if not classify_mode and _bot_already_replied(
        db, conversation_id, message, ignore_message_id=holding_id
    ):
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

    # Un sticker de saludo no se clasifica (saldría «ruido»): se contesta como un «hola».
    greeting = {"category": "duda", "reason": "saludo sin texto"} if opening else None

    if is_qualify_mode(profile):
        return _process_qualify(
            db,
            tenant=tenant,
            conversation=conversation,
            message=message,
            session=session,
            profile=profile,
            classification=greeting,
        )

    classification = greeting or _run_classification(
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
