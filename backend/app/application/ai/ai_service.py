from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import replace
from typing import Optional

from sqlalchemy.orm import Session

from app.application.ai.ai_appointment_service import (
    BookingContext,
    detect_scheduling_interest,
    slot_taken_message,
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
    send_reply_with_shortcut,
)
from app.application.appointments.appointment_service import (
    collect_upcoming_free_slots,
    describe_appointment,
    upcoming_appointment_for_conversation,
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
CLOSING_ALERT_COOLDOWN_SECONDS = 6 * 3600
BOOKING_DAYS_AHEAD = 7
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
    from app.application.conversations.interest_alert_service import send_alert
    from app.shared.core.phone import resolve_display_name

    profile = tenant.profile
    session = tenant.whatsapp_session
    if profile is None or not profile.alert_phone or session is None:
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
    try:
        send_alert(session, profile.alert_phone, text)
    except Exception:
        log.warning("No se pudo avisar al dueño del traspaso tenant=%s", tenant.id)


def _hand_off_after_outage(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    session: WhatsAppSession,
) -> None:
    from app.application.monitoring.dev_alerts import alert_dev

    conversation.mode = ConversationMode.MANUAL.value
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

    _hand_off_after_outage(db, tenant=tenant, conversation=conversation, session=session)
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
) -> Optional[BookingContext]:
    if profile is None or not profile.ai_booking_enabled or not feature_allowed(tenant, "ai_booking"):
        return None
    free_slots = collect_upcoming_free_slots(
        db, tenant_id=tenant.id, days=BOOKING_DAYS_AHEAD, limit=200
    )
    existing = upcoming_appointment_for_conversation(
        db, tenant_id=tenant.id, conversation_id=conversation.id
    )
    return BookingContext(
        free_slots=free_slots,
        existing_appointment=describe_appointment(existing) if existing else "",
    )


def _contact_label(conversation: Conversation) -> str:
    from app.shared.core.phone import resolve_display_name

    return resolve_display_name(
        conversation.contact_name, conversation.contact_phone, contact_jid=conversation.contact_jid or ""
    )


def _send_owner_alert(tenant: Tenant, *, key: str, ttl_seconds: int, text: str) -> None:
    from app.application.conversations.interest_alert_service import send_alert

    profile = tenant.profile
    session = tenant.whatsapp_session
    if profile is None or not profile.alert_phone or session is None:
        return
    try:
        if not get_redis().set(key, "1", nx=True, ex=ttl_seconds):
            return
    except Exception:
        return
    try:
        send_alert(session, profile.alert_phone, text)
    except Exception:
        log.warning("No se pudo enviar aviso al dueño tenant=%s key=%s", tenant.id, key)


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
    scheduling = booking is not None and (reply.book_slot is not None or detect_scheduling_interest(text))
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
        conversation.ai_active = False
        conversation.mode = ConversationMode.MANUAL.value
        _mark_handed_off(conversation.id)
        changed = True
        snippet = " ".join(text_for_ai(message).split())[:200]
        _send_owner_alert(
            tenant,
            key=f"ai:closing_alert:{conversation.id}",
            ttl_seconds=CLOSING_ALERT_COOLDOWN_SECONDS,
            text=(
                f"🔥 *{tenant.business_name}*: {_contact_label(conversation)} ya quiere comprar. "
                "Te dejamos el chat para que cierres la venta.\n\n"
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
    if reply.book_slot is not None and booking is not None:
        appointment = try_create_booking(
            db,
            tenant=tenant,
            conversation=conversation,
            slot=reply.book_slot,
            client_name=_contact_label(conversation),
        )
        if appointment is None:
            fresh = collect_upcoming_free_slots(db, tenant_id=tenant.id, days=BOOKING_DAYS_AHEAD, limit=3)
            reply = replace(reply, message=slot_taken_message(fresh), shortcut_id=None, book_slot=None)

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
        _send_owner_alert(
            tenant,
            key=f"ai:booking_alert:{appointment.id}",
            ttl_seconds=24 * 3600,
            text=(
                f"📅 *{tenant.business_name}*: la IA agendó una cita para "
                f"{_contact_label(conversation)} — {describe_appointment(appointment)}.\n\n"
                f"Ver agenda: {_panel_link()}"
            ),
        )
    _apply_stage(
        db,
        tenant=tenant,
        conversation=conversation,
        message=message,
        stage=_resolve_stage(reply, message=message, shortcuts=shortcuts, booking=booking),
    )
    return True


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
    booking = _booking_context(db, tenant=tenant, profile=profile, conversation=conversation)

    try:
        generated_reply = generate_qualify_reply(
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
    booking = _booking_context(db, tenant=tenant, profile=profile, conversation=conversation)
    try:
        generated_reply = generate_reply(
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
