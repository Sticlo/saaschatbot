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
    is_lid_derived_phone,
    is_valid_whatsapp_phone,
    lid_digits_from_jid,
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
    restart_instance as gateway_restart_instance,
    refresh_qr as gateway_refresh_qr,
    send_text as gateway_send_text,
    send_image as gateway_send_image,
    send_buttons as gateway_send_buttons,
    uses_waha,
)
from app.application.whatsapp.whatsapp_status import apply_session_status, can_send_whatsapp, resolve_whatsapp_status

# Alias para compatibilidad con handlers que capturan EvolutionAPIError
EvolutionAPIError = WhatsAppGatewayError


def _trusted_lid_alt_phone(
    alt_phone: str,
    *,
    lid_jid: str,
) -> str:
    phone = (alt_phone or "").strip()
    if not phone or not is_valid_whatsapp_phone(phone):
        return ""
    if is_lid_derived_phone(phone, lid_jid):
        return ""
    return normalize_phone(phone)


def _resolve_lid_send_recipient(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    conversation: Conversation,
) -> tuple[Conversation, str]:
    """Destino Evolution para chats @lid; solo sustituye por teléfono si es real."""
    if conversation.contact_jid and conversation.contact_jid.endswith("@lid"):
        if is_lid_derived_phone(conversation.contact_phone, conversation.contact_jid):
            lid_digits = lid_digits_from_jid(conversation.contact_jid)
            if lid_digits:
                conversation.contact_phone = f"lid:{lid_digits}"
                db.flush()

    recipient = evolution_send_target(conversation)
    if uses_waha() or not conversation.contact_jid or not conversation.contact_jid.endswith("@lid"):
        return conversation, recipient

    from app.infrastructure.evolution.evolution_store import fetch_lid_alt_phone

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

    trusted = _trusted_lid_alt_phone(alt_phone, lid_jid=conversation.contact_jid)
    if not trusted:
        return conversation, recipient

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
        phone_e164=trusted,
        verified=True,
    )
    apply_identity_to_conversation(
        conversation,
        contact_phone=trusted,
        contact_name=conversation.contact_name,
        contact_jid=conversation.contact_jid,
    )
    if session.active_connection_id:
        canonical = repair_duplicates_for_contact(
            db,
            tenant_id=tenant.id,
            whatsapp_connection_id=session.active_connection_id,
            phone=trusted,
            lid_jid=conversation.contact_jid,
            instance_name=session.instance_name,
        )
        if canonical is not None and canonical.id != conversation.id:
            conversation = canonical
    return conversation, phone_to_evolution_number(trusted)

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
    disc = str(meta.get("disconnectionObject") or "")
    if "device_removed" in disc or '"conflict"' in disc:
        return True
    if status == "connecting" and meta.get("ownerJid"):
        return True
    return False


def _reset_gateway_instance_credentials(
    session: WhatsAppSession,
    *,
    webhook_url: str,
    webhook_secret: str,
) -> None:
    """Borra credenciales atascadas. Logout falla si Evolution está en 'connecting'."""
    name = session.instance_name

    try:
        gateway_logout_instance(name)
    except WhatsAppGatewayError as exc:
        log.info("logout %s omitido (%s) — se recreará instancia", name, exc)

    try:
        gateway_delete_instance(name)
    except WhatsAppGatewayError as exc:
        if exc.status_code != 404:
            log.warning("delete %s: %s", name, exc)

    if settings.evolution_database_url and not uses_waha():
        from app.infrastructure.evolution.evolution_store import purge_instance_stored_data

        purge_instance_stored_data(settings.evolution_database_url, name)

    try:
        gateway_create_instance(name, webhook_url, webhook_secret)
        log.info("Instancia %s recreada para QR limpio", name)
    except WhatsAppGatewayError as exc:
        if exc.status_code in (400, 403, 409) and gateway_instance_exists(name):
            try:
                gateway_restart_instance(name)
                log.info("Instancia %s reiniciada tras recreate fallido", name)
            except WhatsAppGatewayError as restart_exc:
                log.warning("restart %s: %s", name, restart_exc)
        elif exc.status_code not in (400, 403, 409):
            raise


def _prepare_instance_for_new_qr(
    session: WhatsAppSession,
    tenant: Tenant,
    *,
    webhook_url: str,
    webhook_secret: str,
    force: bool = False,
) -> None:
    """Logout/borrado en Evolution para vincular otro celular sin error en el QR."""
    meta = None
    try:
        meta = gateway_fetch_instance(session.instance_name)
    except WhatsAppGatewayError:
        pass

    if not force and not _instance_needs_clean_qr(meta):
        return

    log.info(
        "Limpiando sesión WhatsApp instancia=%s (vincular celular nuevo, force=%s)",
        session.instance_name,
        force,
    )

    if uses_waha():
        try:
            gateway_logout_instance(session.instance_name)
        except WhatsAppGatewayError:
            pass
        try:
            gateway_delete_instance(session.instance_name)
            gateway_create_instance(session.instance_name, webhook_url, webhook_secret)
        except WhatsAppGatewayError as exc:
            log.warning("waha reset %s: %s", session.instance_name, exc)
    else:
        _reset_gateway_instance_credentials(
            session,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
        )

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
        refresh_session_status(db, tenant, session)
    except EvolutionAPIError:
        pass
    if tenant.whatsapp_status == WhatsAppStatus.CONNECTED.value:
        return session

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
        from app.application.conversations.whatsapp_conversation_service import (
            purge_ephemeral_whatsapp_data,
        )

        if previous_status == WhatsAppStatus.CONNECTED.value:
            purge_ephemeral_whatsapp_data(db, tenant=tenant, session=session, notify=True)
        session.status = WhatsAppStatus.DISCONNECTED.value
        tenant.whatsapp_status = WhatsAppStatus.DISCONNECTED.value
        session.phone_number = None
        session.qr_base64 = None
        session.bound_owner_jid = None
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

    # Historial del panel = sesión viva. Si se cae el celular, no dejar basura en BD.
    if mapped in {
        WhatsAppStatus.DISCONNECTED.value,
        WhatsAppStatus.BANNED.value,
        WhatsAppStatus.RESTRICTED.value,
    } and previous_status == WhatsAppStatus.CONNECTED.value:
        from app.application.conversations.whatsapp_conversation_service import (
            purge_ephemeral_whatsapp_data,
        )

        purge_ephemeral_whatsapp_data(db, tenant=tenant, session=session, notify=True)
    elif mapped != WhatsAppStatus.CONNECTED.value:
        # Ya estaba desconectado pero quedaron chats (caída sin purge).
        leftover = (
            db.query(Conversation.id)
            .filter(Conversation.tenant_id == tenant.id)
            .limit(1)
            .first()
        )
        if leftover is not None:
            from app.application.conversations.whatsapp_conversation_service import (
                purge_ephemeral_whatsapp_data,
            )

            purge_ephemeral_whatsapp_data(db, tenant=tenant, session=session, notify=True)

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
        purge_ephemeral_whatsapp_data,
    )

    purge_ephemeral_whatsapp_data(db, tenant=tenant, session=session, notify=True)

    try:
        gateway_logout_instance(session.instance_name)
    except WhatsAppGatewayError as exc:
        log.warning("WhatsApp logout failed for %s: %s", session.instance_name, exc)

    # Borrar la instancia para que Evolution deje de emitir webhooks aunque el
    # logout haya fallado — si no, la IA sigue contestando "desvinculada".
    try:
        gateway_delete_instance(session.instance_name)
    except WhatsAppGatewayError as exc:
        if exc.status_code != 404:
            log.warning("WhatsApp delete instance failed for %s: %s", session.instance_name, exc)

    session.status = WhatsAppStatus.DISCONNECTED.value
    session.qr_base64 = None
    session.bound_owner_jid = None
    session.phone_number = None
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

    conversation, recipient = _resolve_lid_send_recipient(
        db,
        tenant=tenant,
        session=session,
        conversation=conversation,
    )

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


def send_image_message(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    conversation: Conversation,
    image_path: str,
    caption: str = "",
    source: str,
) -> Message:
    ok, reason = can_send_whatsapp(tenant.whatsapp_status)
    if not ok:
        raise EvolutionAPIError(reason)

    conversation, recipient = _prepare_outbound_recipient(
        db, tenant=tenant, session=session, conversation=conversation
    )
    from app.application.outbound.tenant_asset_service import read_asset_base64

    b64, mime = read_asset_base64(image_path, tenant_id=tenant.id)
    filename = image_path.rsplit("/", 1)[-1]
    result = gateway_send_image(
        session.instance_name,
        recipient,
        data_b64=b64,
        mimetype=mime,
        filename=filename,
        caption=caption[:1024],
    )
    evolution_id = _extract_evolution_id(result)
    display_body = f"[Imagen]\n{caption}".strip() if caption else "[Imagen]"
    return _record_outbound_message(
        db,
        tenant=tenant,
        conversation=conversation,
        body=display_body[:4096],
        source=source,
        evolution_id=evolution_id,
    )


def _extract_evolution_id(result: dict) -> Optional[str]:
    if not isinstance(result, dict):
        return None
    key = result.get("key")
    if isinstance(key, dict) and key.get("id"):
        return str(key["id"])
    for field in ("messageId", "id"):
        if result.get(field):
            return str(result[field])
    return None


def _prepare_outbound_recipient(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    conversation: Conversation,
) -> tuple[Conversation, str]:
    if not conversation.contact_jid and not is_valid_whatsapp_phone(conversation.contact_phone):
        raise EvolutionAPIError(
            "No se puede enviar a este contacto — número inválido. "
            "Pide que te escriba de nuevo por WhatsApp."
        )

    return _resolve_lid_send_recipient(
        db,
        tenant=tenant,
        session=session,
        conversation=conversation,
    )


def _record_outbound_message(
    db: Session,
    *,
    tenant: Tenant,
    conversation: Conversation,
    body: str,
    source: str,
    evolution_id: Optional[str],
) -> Message:
    message = Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        direction=MessageDirection.OUT.value,
        source=source,
        body=body,
        status=MessageStatus.SENT.value,
        evolution_message_id=evolution_id,
    )
    conversation.last_message_at = datetime.now(timezone.utc)
    db.add(message)
    db.flush()
    publish_message_event(tenant, conversation, message, event_type="message.out")
    return message


def _buttons_to_provider(buttons: list[dict]) -> list[dict]:
    out: list[dict] = []
    for idx, btn in enumerate(buttons[:3]):
        label = str(btn.get("label") or "").strip()
        if not label:
            continue
        value = str(btn.get("value") or label).strip()
        out.append(
            {
                "type": "reply",
                "displayText": label[:25],
                "id": value[:120],
            }
        )
    return out


def send_bait_message(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    conversation: Conversation,
    text: str,
    source: str,
    extras: Optional[dict] = None,
) -> Message:
    ok, reason = can_send_whatsapp(tenant.whatsapp_status)
    if not ok:
        raise EvolutionAPIError(reason)

    conversation, recipient = _prepare_outbound_recipient(
        db, tenant=tenant, session=session, conversation=conversation
    )
    extras = extras or {}
    image_path = extras.get("image_path")
    buttons = extras.get("buttons") or []
    button_title = str(extras.get("button_title") or tenant.business_name or "Opciones")[:60]
    button_footer = str(extras.get("button_footer") or "")[:60]
    provider_buttons = _buttons_to_provider(buttons) if buttons else []
    last_id: Optional[str] = None
    display_body = text

    if image_path:
        from app.application.outbound.tenant_asset_service import read_asset_base64

        b64, mime = read_asset_base64(image_path, tenant_id=tenant.id)
        filename = image_path.rsplit("/", 1)[-1]
        caption = "" if provider_buttons else text
        result = gateway_send_image(
            session.instance_name,
            recipient,
            data_b64=b64,
            mimetype=mime,
            filename=filename,
            caption=caption[:1024],
        )
        last_id = _extract_evolution_id(result)
        if provider_buttons:
            display_body = f"[Imagen]\n{text}"

    if provider_buttons:
        try:
            result = gateway_send_buttons(
                session.instance_name,
                recipient,
                title=button_title,
                description=text[:1024],
                footer=button_footer,
                buttons=provider_buttons,
            )
            last_id = _extract_evolution_id(result) or last_id
            if image_path:
                display_body = f"[Imagen + botones]\n{text}"
        except WhatsAppGatewayError:
            log.warning("Botones fallaron — enviando solo texto tenant=%s", tenant.id)
            if not image_path or provider_buttons:
                result = gateway_send_text(session.instance_name, recipient, text)
                last_id = _extract_evolution_id(result) or last_id
    elif not image_path:
        result = gateway_send_text(session.instance_name, recipient, text)
        last_id = _extract_evolution_id(result)

    return _record_outbound_message(
        db,
        tenant=tenant,
        conversation=conversation,
        body=display_body[:4096],
        source=source,
        evolution_id=last_id,
    )
