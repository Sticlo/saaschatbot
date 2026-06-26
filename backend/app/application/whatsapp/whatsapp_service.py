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
    normalize_phone,
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
from app.application.whatsapp.whatsapp_gateway import (
    WhatsAppGatewayError,
    connect_instance as gateway_connect,
    connection_state as gateway_connection_state,
    create_instance as gateway_create_instance,
    delete_instance as gateway_delete_instance,
    ensure_realtime_settings as gateway_ensure_realtime,
    ensure_webhook as gateway_ensure_webhook,
    fetch_instance as gateway_fetch_instance,
    gateway_request,
    instance_exists as gateway_instance_exists,
    logout_instance as gateway_logout_instance,
    refresh_qr as gateway_refresh_qr,
    send_text as gateway_send_text,
    uses_waha,
)
from app.application.whatsapp.whatsapp_status import apply_session_status, can_send_whatsapp, resolve_whatsapp_status

# Alias para compatibilidad con handlers que capturan EvolutionAPIError
EvolutionAPIError = WhatsAppGatewayError

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
    return f"{settings.evolution_webhook_base_url()}/webhooks/evolution/{tenant_id}"


def ensure_evolution_webhook(
    session: WhatsAppSession,
    tenant_id: uuid.UUID,
    *,
    force: bool = False,
) -> None:
    """Re-registra webhook con URL alcanzable desde Evolution (omitido con WAHA)."""
    if uses_waha():
        return
    from app.infrastructure.cache.redis_client import get_redis

    webhook_url = _webhook_url(tenant_id)
    if not force:
        try:
            found = gateway_request(
                "GET",
                f"/webhook/find/{session.instance_name}",
                timeout=8.0,
            )
            if isinstance(found, dict):
                stored = str(found.get("url") or "").rstrip("/")
                expected = webhook_url.rstrip("/")
                if stored == expected and found.get("enabled"):
                    return
        except WhatsAppGatewayError:
            pass

    key = f"webhook:ensure:{tenant_id}"
    if not force and not get_redis().set(key, "1", nx=True, ex=120):
        return
    try:
        gateway_ensure_webhook(
            session.instance_name,
            webhook_url,
            settings.evolution_webhook_secret,
        )
        log.info(
            "Webhook Evolution actualizado instancia=%s url=%s force=%s",
            session.instance_name,
            webhook_url,
            force,
        )
    except EvolutionAPIError as exc:
        log.warning("ensure_webhook %s: %s", session.instance_name, exc)


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


def _instance_needs_clean_qr(meta: Optional[dict]) -> bool:
    """Sesión vieja o logout — el QR falla en el celular si no se limpia."""
    if not meta:
        return False
    status = str(meta.get("connectionStatus") or meta.get("status") or "").lower()
    if status in {"close", "closed", "disconnected", "failed", "stopped"}:
        return True
    if meta.get("disconnectionReasonCode") in (401, 403, 428):
        return True
    if status == "connecting" and meta.get("ownerJid"):
        return True
    return False


def _prepare_instance_for_new_qr(
    session: WhatsAppSession,
    tenant: Tenant,
    *,
    webhook_url: str,
    webhook_secret: str,
) -> None:
    """Logout/borrado en Evolution para vincular otro celular sin error en el QR."""
    from app.config import settings
    from app.infrastructure.evolution.evolution_store import purge_instance_stored_data

    meta = None
    try:
        meta = gateway_fetch_instance(session.instance_name)
    except WhatsAppGatewayError:
        pass

    if not _instance_needs_clean_qr(meta):
        return

    log.info(
        "Limpiando sesión WhatsApp instancia=%s (vincular celular nuevo)",
        session.instance_name,
    )
    try:
        gateway_logout_instance(session.instance_name)
    except WhatsAppGatewayError as exc:
        log.warning("logout antes de QR %s: %s", session.instance_name, exc)

    try:
        meta_after = gateway_fetch_instance(session.instance_name)
    except WhatsAppGatewayError:
        meta_after = None

    if _instance_needs_clean_qr(meta_after):
        try:
            gateway_delete_instance(session.instance_name)
            if settings.evolution_database_url and not uses_waha():
                from app.infrastructure.evolution.evolution_store import purge_instance_stored_data

                purge_instance_stored_data(
                    settings.evolution_database_url, session.instance_name
                )
            gateway_create_instance(
                session.instance_name,
                webhook_url,
                webhook_secret,
            )
            log.info("Instancia %s recreada para QR limpio", session.instance_name)
        except WhatsAppGatewayError as exc:
            log.warning("delete/recreate %s: %s", session.instance_name, exc)

    session.phone_number = None
    session.bound_owner_jid = None
    session.qr_base64 = None


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

    try:
        if not gateway_instance_exists(session.instance_name):
            try:
                gateway_create_instance(
                    session.instance_name,
                    _webhook_url(tenant.id),
                    settings.evolution_webhook_secret,
                )
            except WhatsAppGatewayError as exc:
                if exc.status_code not in (400, 403, 409):
                    raise
                log.info("Instancia %s ya existe, reconectando", session.instance_name)
        else:
            try:
                gateway_ensure_webhook(
                    session.instance_name,
                    _webhook_url(tenant.id),
                    settings.evolution_webhook_secret,
                )
            except WhatsAppGatewayError as exc:
                log.warning("No se pudo actualizar webhook: %s", exc)
        try:
            gateway_ensure_realtime(session.instance_name)
        except Exception:
            pass
        _prepare_instance_for_new_qr(
            session,
            tenant,
            webhook_url=_webhook_url(tenant.id),
            webhook_secret=settings.evolution_webhook_secret,
        )
    except WhatsAppGatewayError:
        raise

    connect_data = gateway_connect(session.instance_name)
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
        from app.application.sync.sync_scheduler import ensure_whatsapp_sync_after_connect

        ensure_whatsapp_sync_after_connect(tenant.id, force=True)
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
        state_payload = gateway_connection_state(session.instance_name)
    except WhatsAppGatewayError:
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
        instance_meta = gateway_fetch_instance(session.instance_name)
    except WhatsAppGatewayError:
        instance_meta = None

    if instance_meta:
        meta_status = str(
            instance_meta.get("connectionStatus") or instance_meta.get("status") or ""
        ).lower()
        if meta_status in {"open", "working"}:
            state = "open"
        elif meta_status in {"close", "closed", "disconnected", "failed", "stopped"}:
            state = "close"
        elif meta_status == "scan_qr_code":
            state = "connecting"
        owner = owner or instance_meta.get("ownerJid") or instance_meta.get("owner")
        if uses_waha() and state == "connecting":
            qr = gateway_refresh_qr(session.instance_name)
            if qr:
                session.qr_base64 = qr
                session.qr_updated_at = datetime.now(timezone.utc)

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
        if settings.evolution_database_url and not uses_waha():
            from app.infrastructure.evolution.evolution_store import purge_instance_stored_data

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
    from app.application.conversations.whatsapp_conversation_service import (
        notify_conversations_cleared,
        purge_all_tenant_whatsapp_conversations,
    )
    from app.application.sync.tenant_sync_state import clear_tenant_sync_state

    clear_tenant_sync_state(tenant.id)

    purge_all_tenant_whatsapp_conversations(db, tenant_id=tenant.id)
    if settings.evolution_database_url and not uses_waha():
        from app.infrastructure.evolution.evolution_store import purge_instance_stored_data

        purge_instance_stored_data(settings.evolution_database_url, session.instance_name)
    session.active_connection_id = None
    session.connection_started_at = None
    session.bound_owner_jid = None
    notify_conversations_cleared(tenant.id)

    try:
        gateway_logout_instance(session.instance_name)
    except WhatsAppGatewayError as exc:
        log.warning("WhatsApp logout failed for %s: %s", session.instance_name, exc)

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
    if not uses_waha():
        from app.infrastructure.evolution.evolution_store import fetch_lid_alt_phone

        alt_phone = ""
        if conversation.contact_jid and conversation.contact_jid.endswith("@lid"):
            alt_phone = fetch_lid_alt_phone(
                settings.evolution_database_url,
                session.instance_name,
                conversation.contact_jid,
            )
            if not alt_phone and session.active_connection_id:
                from app.application.conversations.contact_resolver_service import _load_link_maps

                _lid_map, _ = _load_link_maps(
                    db,
                    tenant_id=tenant.id,
                    whatsapp_connection_id=session.active_connection_id,
                )
                alt_phone = _lid_map.get(conversation.contact_jid) or ""
            if alt_phone and is_valid_whatsapp_phone(alt_phone):
                recipient = phone_to_evolution_number(alt_phone)
                from app.application.sync.contact_identity_service import apply_identity_to_conversation
                from app.application.conversations.contact_resolver_service import (
                    record_contact_link,
                    repair_duplicates_for_contact,
                )

                record_contact_link(
                    db,
                    tenant_id=tenant.id,
                    whatsapp_connection_id=session.active_connection_id,
                    lid_jid=conversation.contact_jid,
                    phone_e164=normalize_phone(alt_phone),
                    verified=True,
                )
                apply_identity_to_conversation(
                    conversation,
                    contact_phone=normalize_phone(alt_phone),
                    contact_name=conversation.contact_name,
                    contact_jid=conversation.contact_jid,
                )
                if session.active_connection_id:
                    canonical = repair_duplicates_for_contact(
                        db,
                        tenant_id=tenant.id,
                        whatsapp_connection_id=session.active_connection_id,
                        phone=normalize_phone(alt_phone),
                        lid_jid=conversation.contact_jid,
                        instance_name=session.instance_name,
                    )
                    if canonical is not None and canonical.id != conversation.id:
                        conversation = canonical

    result = gateway_send_text(session.instance_name, recipient, text)

    evolution_id = None
    if isinstance(result, dict):
        evolution_id = (
            result.get("key", {}).get("id")
            if isinstance(result.get("key"), dict)
            else result.get("messageId") or result.get("id")
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
