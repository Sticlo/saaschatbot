from __future__ import annotations

import logging
import time
from typing import Any, Optional

from app.config import settings

log = logging.getLogger(__name__)

_waha_probe_at: float = 0.0
_waha_probe_ok: Optional[bool] = None
_WAHA_PROBE_TTL = 45.0


class WhatsAppGatewayError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def waha_requested() -> bool:
    return (settings.whatsapp_provider or "waha").strip().lower() == "waha"


def _waha_reachable() -> bool:
    global _waha_probe_at, _waha_probe_ok
    now = time.monotonic()
    if _waha_probe_ok is not None and (now - _waha_probe_at) < _WAHA_PROBE_TTL:
        return _waha_probe_ok

    ok = False
    try:
        import httpx

        url = f"{settings.waha_api_url.rstrip('/')}/api/sessions"
        response = httpx.get(
            url,
            headers={"X-Api-Key": settings.waha_api_key or ""},
            timeout=2.5,
        )
        ok = response.status_code < 500
    except Exception:
        ok = False

    if _waha_probe_ok is not False and not ok and waha_requested():
        log.warning(
            "WAHA no responde en %s — usando Evolution (%s). "
            "Para WAHA: instala Docker y ejecuta ./scripts/waha-docker.sh",
            settings.waha_api_url,
            settings.evolution_api_url,
        )

    _waha_probe_at = now
    _waha_probe_ok = ok
    return ok


def uses_waha() -> bool:
    """True solo si WHATSAPP_PROVIDER=waha y el servicio responde."""
    return waha_requested() and _waha_reachable()


def active_provider_label() -> str:
    return "waha" if uses_waha() else "evolution"


def instance_exists(name: str) -> bool:
    if uses_waha():
        from app.infrastructure.waha.waha_client import WahaAPIError, waha_client

        try:
            return waha_client.session_exists(name)
        except WahaAPIError as exc:
            raise WhatsAppGatewayError(str(exc), exc.status_code) from exc
    from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client

    try:
        return evolution_client.instance_exists(name)
    except EvolutionAPIError as exc:
        raise WhatsAppGatewayError(str(exc), exc.status_code) from exc


def create_instance(name: str, webhook_url: str, webhook_secret: str) -> dict:
    if uses_waha():
        from app.infrastructure.waha.waha_client import WahaAPIError, waha_client

        try:
            return waha_client.create_session(name, engine=settings.waha_engine)
        except WahaAPIError as exc:
            raise WhatsAppGatewayError(str(exc), exc.status_code) from exc
    from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client

    try:
        return evolution_client.create_instance(name, webhook_url, webhook_secret)
    except EvolutionAPIError as exc:
        raise WhatsAppGatewayError(str(exc), exc.status_code) from exc


def ensure_webhook(name: str, webhook_url: str, webhook_secret: str) -> dict:
    if uses_waha():
        return {}
    from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client

    try:
        return evolution_client.ensure_webhook(name, webhook_url, webhook_secret)
    except EvolutionAPIError as exc:
        raise WhatsAppGatewayError(str(exc), exc.status_code) from exc


def ensure_realtime_settings(name: str) -> None:
    if uses_waha():
        return
    from app.infrastructure.evolution.evolution_client import evolution_client

    try:
        evolution_client.ensure_realtime_settings(name)
    except Exception as exc:
        log.debug("ensure_realtime_settings %s: %s", name, exc)


def fetch_instance(name: str) -> Optional[dict]:
    if uses_waha():
        from app.infrastructure.waha.waha_client import WahaAPIError, waha_client

        try:
            return waha_client.get_session(name)
        except WahaAPIError:
            return None
    from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client

    try:
        return evolution_client.fetch_instance(name)
    except EvolutionAPIError:
        return None


def logout_instance(name: str) -> dict:
    if uses_waha():
        from app.infrastructure.waha.waha_client import WahaAPIError, waha_client

        try:
            return waha_client.logout_session(name)
        except WahaAPIError as exc:
            raise WhatsAppGatewayError(str(exc), exc.status_code) from exc
    from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client

    try:
        return evolution_client.logout_instance(name)
    except EvolutionAPIError as exc:
        raise WhatsAppGatewayError(str(exc), exc.status_code) from exc


def delete_instance(name: str) -> dict:
    if uses_waha():
        from app.infrastructure.waha.waha_client import WahaAPIError, waha_client

        try:
            return waha_client.delete_session(name)
        except WahaAPIError as exc:
            raise WhatsAppGatewayError(str(exc), exc.status_code) from exc
    from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client

    try:
        return evolution_client.delete_instance(name)
    except EvolutionAPIError as exc:
        raise WhatsAppGatewayError(str(exc), exc.status_code) from exc


def connect_instance(name: str) -> dict:
    if uses_waha():
        from app.infrastructure.waha.waha_client import WahaAPIError, waha_client

        try:
            if not waha_client.session_exists(name):
                waha_client.create_session(name, engine=settings.waha_engine)
            else:
                waha_client.start_session(name)
            qr = waha_client.get_qr_base64(name)
            session = waha_client.get_session(name)
            return {"status": session.get("status"), "qrcode": {"base64": qr} if qr else {}}
        except WahaAPIError as exc:
            raise WhatsAppGatewayError(str(exc), exc.status_code) from exc
    from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client

    try:
        return evolution_client.connect_instance(name)
    except EvolutionAPIError as exc:
        raise WhatsAppGatewayError(str(exc), exc.status_code) from exc


def connection_state(name: str) -> dict:
    if uses_waha():
        from app.infrastructure.waha.waha_client import WahaAPIError, waha_client

        try:
            session = waha_client.get_session(name)
            status = str(session.get("status") or "").upper()
            me = session.get("me") if isinstance(session.get("me"), dict) else {}
            if not me and status == "WORKING":
                try:
                    me = waha_client.get_me(name)
                except WahaAPIError:
                    me = {}
            state = "open" if status == "WORKING" else "close"
            if status == "SCAN_QR_CODE":
                state = "connecting"
            owner = me.get("id") or me.get("jid") or ""
            return {
                "instance": {"state": state, "ownerJid": owner},
                "state": state,
                "connectionStatus": status,
            }
        except WahaAPIError as exc:
            raise WhatsAppGatewayError(str(exc), exc.status_code) from exc
    from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client

    try:
        return evolution_client.connection_state(name)
    except EvolutionAPIError as exc:
        raise WhatsAppGatewayError(str(exc), exc.status_code) from exc


def send_text(session_name: str, recipient: str, text: str) -> dict:
    if uses_waha():
        from app.infrastructure.waha.waha_client import WahaAPIError, waha_client
        from app.shared.core.phone import phone_to_evolution_number

        chat_id = recipient
        if "@" not in chat_id:
            chat_id = f"{phone_to_evolution_number(recipient)}@c.us"
        try:
            return waha_client.send_text(session_name, chat_id, text)
        except WahaAPIError as exc:
            raise WhatsAppGatewayError(str(exc), exc.status_code) from exc
    from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client

    try:
        return evolution_client.send_text(session_name, recipient, text)
    except EvolutionAPIError as exc:
        raise WhatsAppGatewayError(str(exc), exc.status_code) from exc


def _resolve_chat_id(recipient: str) -> tuple[str, str]:
    from app.shared.core.phone import phone_to_evolution_number

    if uses_waha():
        chat_id = recipient if "@" in recipient else f"{phone_to_evolution_number(recipient)}@c.us"
        return chat_id, recipient
    return recipient, recipient


def send_image(
    session_name: str,
    recipient: str,
    *,
    data_b64: str,
    mimetype: str,
    filename: str,
    caption: str = "",
) -> dict:
    if uses_waha():
        from app.infrastructure.waha.waha_client import WahaAPIError, waha_client

        chat_id, _ = _resolve_chat_id(recipient)
        try:
            return waha_client.send_image(
                session_name,
                chat_id,
                data_b64=data_b64,
                mimetype=mimetype,
                filename=filename,
                caption=caption,
            )
        except WahaAPIError as exc:
            raise WhatsAppGatewayError(str(exc), exc.status_code) from exc
    from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client

    try:
        return evolution_client.send_media(
            session_name,
            recipient,
            media_b64=data_b64,
            mimetype=mimetype,
            caption=caption,
            filename=filename,
        )
    except EvolutionAPIError as exc:
        raise WhatsAppGatewayError(str(exc), exc.status_code) from exc


def send_buttons(
    session_name: str,
    recipient: str,
    *,
    title: str,
    description: str,
    footer: str,
    buttons: list[dict],
) -> dict:
    if uses_waha():
        from app.infrastructure.waha.waha_client import WahaAPIError, waha_client

        chat_id, _ = _resolve_chat_id(recipient)
        try:
            return waha_client.send_buttons(
                session_name,
                chat_id,
                title=title,
                body=description,
                footer=footer,
                buttons=buttons,
            )
        except WahaAPIError as exc:
            raise WhatsAppGatewayError(str(exc), exc.status_code) from exc
    from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client

    try:
        return evolution_client.send_buttons(
            session_name,
            recipient,
            title=title,
            description=description,
            footer=footer,
            buttons=buttons,
        )
    except EvolutionAPIError as exc:
        raise WhatsAppGatewayError(str(exc), exc.status_code) from exc


def refresh_qr(name: str) -> Optional[str]:
    if not uses_waha():
        return None
    from app.infrastructure.waha.waha_client import waha_client

    return waha_client.get_qr_base64(name)


def gateway_request(method: str, path: str, **kwargs: Any) -> Any:
    """Proxy para ensure_evolution_webhook (solo Evolution)."""
    if uses_waha():
        return {}
    from app.infrastructure.evolution.evolution_client import EvolutionAPIError, evolution_client

    try:
        return evolution_client._request(method, path, **kwargs)
    except EvolutionAPIError as exc:
        raise WhatsAppGatewayError(str(exc), exc.status_code) from exc
