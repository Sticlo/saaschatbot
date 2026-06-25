from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.shared.core.phone import (
    evolution_send_target,
    instance_name_for_tenant,
    is_valid_whatsapp_phone,
    phone_to_evolution_number,
)
from app.domain.entities import (
    Conversation,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageSource,
    MessageStatus,
    Tenant,
    WhatsAppSession,
    WhatsAppStatus,
)
from app.application.realtime.realtime_service import publish_message_event
from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client
from app.infrastructure.evolution.evolution_store import fetch_lid_alt_phone
from app.application.whatsapp.whatsapp_status import apply_session_status, can_send_whatsapp, resolve_whatsapp_status

log = logging.getLogger(__name__)


@dataclass
class SessionRefreshResult:
    session: WhatsAppSession
    binding_reset: bool = False
    phone_changed: bool = False
    status_changed: bool = False

    @property
    def should_notify(self) -> bool:
        return self.status_changed or self.binding_reset or self.phone_changed

    @property
    def should_sync(self) -> bool:
        return (
            self.session.status == WhatsAppStatus.CONNECTED.value
            and (self.status_changed or self.binding_reset or self.phone_changed)
        )


def _webhook_url(tenant_id: uuid.UUID) -> str:
    return f"{settings.app_public_url.rstrip('/')}/webhooks/evolution/{tenant_id}"


def get_or_create_session(db: Session, tenant: Tenant) -> WhatsAppSession:
    session = (
        db.query(WhatsAppSession)
        .filter(WhatsAppSession.tenant_id == tenant.id)
        .first()
    )
    if session:
        return session

    instance_name = instance_name_for_tenant(tenant.id)
    session = WhatsAppSession(
        tenant_id=tenant.id,
        instance_name=instance_name,
        status=WhatsAppStatus.DISCONNECTED.value,
    )
    db.add(session)
    db.flush()
    return session


def _extract_qr(payload: dict) -> Optional[str]:
    for key in ("base64", "qrcode", "code"):
        if isinstance(payload.get(key), str):
            return payload[key]
    qrcode = payload.get("qrcode")
    if isinstance(qrcode, dict):
        return qrcode.get("base64") or qrcode.get("code")
    return None


def reconnect_session(db: Session, tenant: Tenant) -> WhatsAppSession:
    """Reconexión: regenera QR si Evolution no está vinculado."""
    session = get_or_create_session(db, tenant)
    try:
        refresh_session_status(db, tenant, session)
    except EvolutionAPIError:
        pass
    if tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value:
        return session
    return start_connection(db, tenant)


def start_connection(db: Session, tenant: Tenant) -> WhatsAppSession:
    session = get_or_create_session(db, tenant)
    client = evolution_client

    try:
        if not client.instance_exists(session.instance_name):
            try:
                client.create_instance(
                    session.instance_name,
                    _webhook_url(tenant.id),
                    settings.evolution_webhook_secret,
                )
            except EvolutionAPIError as exc:
                if exc.status_code not in (400, 403, 409):
                    raise
                log.info("Instancia %s ya existe en Evolution, reconectando", session.instance_name)
        else:
            try:
                client.ensure_webhook(
                    session.instance_name,
                    _webhook_url(tenant.id),
                    settings.evolution_webhook_secret,
                )
            except EvolutionAPIError as exc:
                log.warning("No se pudo actualizar webhook: %s", exc)
    except EvolutionAPIError:
        raise

    connect_data = client.connect_instance(session.instance_name)
    qr = _extract_qr(connect_data if isinstance(connect_data, dict) else {})
    if not qr and isinstance(connect_data, dict):
        qr = _extract_qr(connect_data.get("qrcode", {}))

    # Evolution puede restaurar sesión sin QR (celular ya vinculado).
    try:
        refresh_session_status(db, tenant, session)
    except EvolutionAPIError:
        pass

    if tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value:
        from app.application.realtime.realtime_service import publish_whatsapp_status

        publish_whatsapp_status(
            tenant.id,
            status=WhatsAppStatus.CONNECTED.value,
            qr_base64=None,
            phone_number=session.phone_number,
        )
        return session

    session.status = WhatsAppStatus.CONNECTING.value
    session.qr_base64 = qr
    session.qr_updated_at = datetime.now(timezone.utc) if qr else None
    tenant.whatsapp_status = WhatsAppStatus.CONNECTING.value
    db.flush()
    from app.application.realtime.realtime_service import publish_whatsapp_status

    publish_whatsapp_status(
        tenant.id,
        status=WhatsAppStatus.CONNECTING.value,
        qr_base64=qr,
        phone_number=session.phone_number,
    )
    return session


def refresh_session_status(
    db: Session, tenant: Tenant, session: WhatsAppSession
) -> SessionRefreshResult:
    previous_status = session.status
    phone_before = session.phone_number
    bound_before = session.bound_owner_jid
    binding_reset = False
    try:
        state_payload = evolution_client.connection_state(session.instance_name)
    except EvolutionAPIError:
        session.status = WhatsAppStatus.DISCONNECTED.value
        tenant.whatsapp_status = WhatsAppStatus.DISCONNECTED.value
        db.flush()
        return SessionRefreshResult(
            session=session,
            status_changed=previous_status != WhatsAppStatus.DISCONNECTED.value,
        )

    state = ""
    if isinstance(state_payload, dict):
        state = (
            state_payload.get("instance", {}).get("state")
            or state_payload.get("state")
            or state_payload.get("connectionStatus")
            or ""
        )

    owner = None
    if isinstance(state_payload, dict):
        instance_data = state_payload.get("instance") or state_payload
        if isinstance(instance_data, dict):
            owner = instance_data.get("owner") or instance_data.get("wuid")
        owner = owner or state_payload.get("owner") or state_payload.get("wuid")

    try:
        instance_meta = evolution_client.fetch_instance(session.instance_name)
    except EvolutionAPIError:
        instance_meta = None

    if instance_meta:
        meta_status = str(instance_meta.get("connectionStatus") or "").lower()
        if meta_status == "open":
            state = "open"
        elif meta_status in {"close", "closed", "disconnected"}:
            state = "close"
        owner = owner or instance_meta.get("ownerJid") or instance_meta.get("owner")

    mapped = resolve_whatsapp_status(str(state), state_payload if isinstance(state_payload, dict) else {})
    from app.application.conversations.whatsapp_conversation_service import (
        needs_new_whatsapp_binding,
        start_new_whatsapp_binding,
        ensure_whatsapp_binding_ready,
    )

    if needs_new_whatsapp_binding(
        session,
        previous_status=previous_status,
        mapped_status=mapped,
        owner_jid=str(owner) if owner else None,
    ):
        start_new_whatsapp_binding(
            db,
            tenant=tenant,
            session=session,
            instance_name=session.instance_name,
            owner_jid=str(owner) if owner else None,
        )
        binding_reset = True
    else:
        ensure_whatsapp_binding_ready(
            db,
            tenant=tenant,
            session=session,
            owner_jid=str(owner) if owner else None,
        )

    apply_session_status(
        session,
        tenant,
        mapped,
        owner_jid=str(owner) if owner else None,
    )

    if (
        mapped in {
            WhatsAppStatus.DISCONNECTED.value,
            WhatsAppStatus.BANNED.value,
            WhatsAppStatus.RESTRICTED.value,
        }
        and previous_status == WhatsAppStatus.CONNECTED.value
    ):
        from app.config import settings
        from app.infrastructure.evolution.evolution_store import purge_instance_stored_data
        from app.application.conversations.whatsapp_conversation_service import (
            notify_conversations_cleared,
            purge_all_tenant_whatsapp_conversations,
        )

        purge_all_tenant_whatsapp_conversations(db, tenant_id=tenant.id)
        if settings.evolution_database_url:
            purge_instance_stored_data(settings.evolution_database_url, session.instance_name)
        session.active_connection_id = None
        session.connection_started_at = None
        session.bound_owner_jid = None
        notify_conversations_cleared(tenant.id)

    phone_changed = phone_before != session.phone_number or (
        bound_before != session.bound_owner_jid and session.bound_owner_jid is not None
    )
    status_changed = mapped != previous_status
    result = SessionRefreshResult(
        session=session,
        binding_reset=binding_reset,
        phone_changed=phone_changed,
        status_changed=status_changed,
    )

    if result.should_notify:
        from app.application.realtime.realtime_service import publish_whatsapp_status

        publish_whatsapp_status(
            tenant.id,
            status=mapped,
            qr_base64=session.qr_base64,
            phone_number=session.phone_number,
        )
    db.flush()
    return result


def disconnect_session(db: Session, tenant: Tenant, session: WhatsAppSession) -> None:
    from app.config import settings
    from app.infrastructure.evolution.evolution_store import purge_instance_stored_data
    from app.application.conversations.whatsapp_conversation_service import (
        notify_conversations_cleared,
        purge_all_tenant_whatsapp_conversations,
    )

    purge_all_tenant_whatsapp_conversations(db, tenant_id=tenant.id)
    if settings.evolution_database_url:
        purge_instance_stored_data(settings.evolution_database_url, session.instance_name)
    session.active_connection_id = None
    session.connection_started_at = None
    session.bound_owner_jid = None
    notify_conversations_cleared(tenant.id)

    try:
        evolution_client.logout_instance(session.instance_name)
    except EvolutionAPIError as exc:
        log.warning("Evolution logout failed for %s: %s", session.instance_name, exc)

    session.status = WhatsAppStatus.DISCONNECTED.value
    session.qr_base64 = None
    session.last_disconnected_at = datetime.now(timezone.utc)
    tenant.whatsapp_status = WhatsAppStatus.DISCONNECTED.value
    db.flush()


def send_text_message(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    conversation: Conversation,
    text: str,
    source: str,
) -> Message:
    ok, reason = can_send_whatsapp(tenant.whatsapp_status)
    if not ok:
        raise EvolutionAPIError(reason)

    if not conversation.contact_jid and not is_valid_whatsapp_phone(conversation.contact_phone):
        raise EvolutionAPIError(
            "No se puede enviar a este contacto — número inválido. "
            "Pide que te escriba de nuevo por WhatsApp."
        )

    recipient = evolution_send_target(conversation)
    if conversation.contact_jid and conversation.contact_jid.endswith("@lid"):
        alt_phone = fetch_lid_alt_phone(
            settings.evolution_database_url,
            session.instance_name,
            conversation.contact_jid,
        )
        if alt_phone and is_valid_whatsapp_phone(alt_phone):
            recipient = phone_to_evolution_number(alt_phone)

    result = evolution_client.send_text(session.instance_name, recipient, text)

    evolution_id = None
    if isinstance(result, dict):
        evolution_id = (
            result.get("key", {}).get("id")
            if isinstance(result.get("key"), dict)
            else result.get("messageId")
        )

    message = Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        direction=MessageDirection.OUT.value,
        source=source,
        body=text,
        status=MessageStatus.SENT.value,
        evolution_message_id=str(evolution_id) if evolution_id else None,
    )
    conversation.last_message_at = datetime.now(timezone.utc)
    db.add(message)
    db.flush()

    publish_message_event(tenant, conversation, message, event_type="message.out")
    return message
