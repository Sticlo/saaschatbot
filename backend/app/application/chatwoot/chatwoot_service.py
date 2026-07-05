from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.domain.entities import Tenant, WhatsAppSession
from app.infrastructure.chatwoot.chatwoot_client import ChatwootAPIError, chatwoot_client
from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client

log = logging.getLogger(__name__)


def chatwoot_token_configured() -> bool:
    token = (settings.chatwoot_api_token or "").strip()
    if not token:
        return False
    if token.startswith("<") or "replace" in token.lower():
        return False
    return True


def chatwoot_only_mode() -> bool:
    """True = prohibir sync Evolution custom; solo Chatwoot importa chats/mensajes."""
    return bool(settings.chatwoot_enabled)


def chatwoot_sync_mode() -> bool:
    """True = Chatwoot configurado y listo para importar vía API."""
    return bool(chatwoot_only_mode() and chatwoot_token_configured())


def chatwoot_sync_ready(session: Optional[WhatsAppSession]) -> bool:
    """True si Chatwoot está configurado y la sesión tiene inbox listo para importar."""
    if not chatwoot_sync_mode():
        return False
    if session is None:
        return False
    meta = _chatwoot_meta(session)
    return bool(meta.get("enabled") and meta.get("inbox_id"))


def _chatwoot_meta(session: WhatsAppSession) -> dict:
    meta = session.metadata_json if isinstance(session.metadata_json, dict) else {}
    cw = meta.get("chatwoot")
    return cw if isinstance(cw, dict) else {}


def _save_chatwoot_meta(session: WhatsAppSession, patch: dict) -> None:
    meta = dict(session.metadata_json or {})
    current = _chatwoot_meta(session)
    current.update(patch)
    meta["chatwoot"] = current
    session.metadata_json = meta


def ensure_chatwoot_integration(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    force: bool = False,
) -> bool:
    """Conecta instancia Evolution ↔ Chatwoot (importa chats/mensajes sin sync custom)."""
    if not settings.chatwoot_enabled:
        return False
    if not chatwoot_token_configured():
        log.warning("Chatwoot habilitado pero falta CHATWOOT_API_TOKEN tenant=%s", tenant.id)
        return False

    existing = _chatwoot_meta(session)

    if uses_waha():
        return _ensure_waha_chatwoot_app(db, tenant=tenant, session=session, existing=existing)

    import_history = settings.whatsapp_import_history_on_connect
    payload = {
        "enabled": True,
        "accountId": settings.chatwoot_account_id,
        "token": settings.chatwoot_api_token,
        "url": settings.chatwoot_base_url(),
        "signMsg": True,
        "signDelimiter": "\n",
        "reopenConversation": True,
        "conversationPending": False,
        "nameInbox": session.instance_name,
        "mergeBrazilContacts": True,
        "importContacts": import_history,
        "importMessages": import_history,
        "daysLimitImportMessages": (
            settings.chatwoot_days_limit_import_messages if import_history else 1
        ),
        "autoCreate": True,
    }
    try:
        evolution_client.set_chatwoot(session.instance_name, payload)
        cw_info = evolution_client.find_chatwoot(session.instance_name)
    except EvolutionAPIError as exc:
        log.warning("set_chatwoot falló instancia=%s: %s", session.instance_name, exc)
        return False

    if existing.get("enabled") and existing.get("inbox_id") and not force:
        log.debug(
            "Chatwoot config actualizada tenant=%s days_limit=%s",
            tenant.id,
            settings.chatwoot_days_limit_import_messages,
        )
        return True

    inbox_id: Optional[int] = None
    if isinstance(cw_info, dict):
        raw_inbox = cw_info.get("inboxId") or cw_info.get("inbox_id")
        if raw_inbox is not None:
            try:
                inbox_id = int(raw_inbox)
            except (TypeError, ValueError):
                inbox_id = None

    if inbox_id is None:
        try:
            inbox = chatwoot_client.find_inbox_by_name(session.instance_name)
            if inbox:
                inbox_id = int(inbox.get("id"))
        except ChatwootAPIError as exc:
            log.debug("find_inbox_by_name: %s", exc)

    _save_chatwoot_meta(
        session,
        {
            "enabled": True,
            "inbox_id": inbox_id,
            "inbox_name": session.instance_name,
            "account_id": settings.chatwoot_account_id,
        },
    )
    db.flush()

    try:
        chatwoot_client.ensure_account_webhook(settings.chatwoot_webhook_url())
    except ChatwootAPIError as exc:
        log.warning("ensure_account_webhook: %s", exc)

    log.info(
        "Chatwoot conectado tenant=%s instancia=%s inbox_id=%s",
        tenant.id,
        session.instance_name,
        inbox_id,
    )
    return True


def resolve_tenant_by_inbox_id(db: Session, inbox_id: int) -> Optional[tuple[Tenant, WhatsAppSession]]:
    rows = (
        db.query(Tenant, WhatsAppSession)
        .join(WhatsAppSession, WhatsAppSession.tenant_id == Tenant.id)
        .all()
    )
    for tenant, session in rows:
        meta = _chatwoot_meta(session)
        try:
            if int(meta.get("inbox_id") or 0) == int(inbox_id):
                return tenant, session
        except (TypeError, ValueError):
            continue
    return None


def chatwoot_panel_url(session: WhatsAppSession) -> Optional[str]:
    meta = _chatwoot_meta(session)
    inbox_id = meta.get("inbox_id")
    if not inbox_id:
        return None
    base = settings.chatwoot_url.rstrip("/")
    return f"{base}/app/accounts/{settings.chatwoot_account_id}/inbox/{inbox_id}"


def _ensure_waha_chatwoot_app(
    db: Session,
    *,
    tenant: Tenant,
    session: WhatsAppSession,
    existing: dict,
) -> bool:
    """WAHA app Chatwoot: Chrome sincroniza historial nativo → Chatwoot."""
    from app.infrastructure.waha.waha_client import WahaAPIError, waha_client
    from app.infrastructure.chatwoot.chatwoot_client import ChatwootAPIError, chatwoot_client

    inbox_id: Optional[int] = None
    try:
        raw = existing.get("inbox_id")
        if raw is not None:
            inbox_id = int(raw)
    except (TypeError, ValueError):
        inbox_id = None

    if inbox_id is None:
        try:
            inbox = chatwoot_client.find_inbox_by_name(session.instance_name)
            if inbox:
                inbox_id = int(inbox.get("id"))
        except ChatwootAPIError as exc:
            log.debug("find_inbox_by_name waha: %s", exc)

    try:
        waha_client.ensure_chatwoot_app(
            session=session.instance_name,
            chatwoot_url=settings.chatwoot_internal_url(),
            account_id=int(settings.chatwoot_account_id),
            account_token=settings.chatwoot_api_token,
            inbox_id=inbox_id or 1,
        )
    except WahaAPIError as exc:
        log.warning("WAHA chatwoot app falló instancia=%s: %s", session.instance_name, exc)
        return False

    if inbox_id is None:
        try:
            inbox = chatwoot_client.find_inbox_by_name(session.instance_name)
            if inbox:
                inbox_id = int(inbox.get("id"))
        except ChatwootAPIError:
            pass

    _save_chatwoot_meta(
        session,
        {
            "enabled": True,
            "inbox_id": inbox_id,
            "inbox_name": session.instance_name,
            "account_id": settings.chatwoot_account_id,
            "provider": "waha",
        },
    )
    db.flush()

    try:
        chatwoot_client.ensure_account_webhook(settings.chatwoot_webhook_url())
    except ChatwootAPIError as exc:
        log.warning("ensure_account_webhook: %s", exc)

    log.info(
        "WAHA↔Chatwoot conectado tenant=%s instancia=%s inbox_id=%s",
        tenant.id,
        session.instance_name,
        inbox_id,
    )
    return True
