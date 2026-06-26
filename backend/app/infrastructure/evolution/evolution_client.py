from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)

HISTORY_WEBHOOK_EVENTS = [
    "CONNECTION_UPDATE",
    "QRCODE_UPDATED",
    "MESSAGES_UPSERT",
    "MESSAGES_UPDATE",
    "MESSAGES_SET",
    "CHATS_SET",
    "CHATS_UPSERT",
    "CHATS_UPDATE",
    "CONTACTS_SET",
    "CONTACTS_UPSERT",
]


class EvolutionAPIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class EvolutionClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 5.0,
    ):
        self.base_url = (base_url or settings.evolution_api_url).rstrip("/")
        self.api_key = api_key or settings.evolution_api_key
        self.timeout = timeout

    def _headers(self) -> dict:
        return {
            "apikey": self.api_key,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        timeout: Optional[float] = None,
    ):
        url = f"{self.base_url}{path}"
        request_timeout = timeout if timeout is not None else self.timeout
        try:
            with httpx.Client(timeout=request_timeout) as client:
                response = client.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json,
                    params=params,
                )
        except httpx.RequestError as exc:
            raise EvolutionAPIError(f"No se pudo conectar con Evolution API: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:500]
            raise EvolutionAPIError(
                f"Evolution API error {response.status_code}: {detail}",
                status_code=response.status_code,
            )

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    def create_instance(
        self,
        instance_name: str,
        webhook_url: str,
        webhook_secret: str,
    ) -> dict:
        payload = {
            "instanceName": instance_name,
            "integration": "WHATSAPP-BAILEYS",
            "qrcode": True,
            "syncFullHistory": False,
            "webhook": {
                "url": webhook_url,
                "byEvents": False,
                "base64": True,
                "headers": {
                    "X-Webhook-Secret": webhook_secret,
                },
                "events": HISTORY_WEBHOOK_EVENTS,
            },
        }
        return self._request("POST", "/instance/create", json=payload, timeout=60.0)

    def ensure_webhook(self, instance_name: str, webhook_url: str, webhook_secret: str) -> dict:
        """Actualiza webhook con eventos de historial (instancias creadas antes no los tenían)."""
        payload = {
            "webhook": {
                "enabled": True,
                "url": webhook_url,
                "byEvents": False,
                "base64": True,
                "headers": {"X-Webhook-Secret": webhook_secret},
                "events": HISTORY_WEBHOOK_EVENTS,
            }
        }
        return self._request(
            "POST",
            f"/webhook/set/{instance_name}",
            json=payload,
            timeout=15.0,
        )

    def set_chatwoot(self, instance_name: str, config: dict) -> dict:
        return self._request(
            "POST",
            f"/chatwoot/set/{instance_name}",
            json=config,
            timeout=30.0,
        )

    def find_chatwoot(self, instance_name: str) -> dict:
        result = self._request(
            "GET",
            f"/chatwoot/find/{instance_name}",
            timeout=15.0,
        )
        return result if isinstance(result, dict) else {}

    def restart_instance(self, instance_name: str) -> dict:
        return self._request(
            "POST",
            f"/instance/restart/{instance_name}",
            json={},
            timeout=90.0,
        )

    def connect_instance(self, instance_name: str) -> dict:
        return self._request("GET", f"/instance/connect/{instance_name}", timeout=60.0)

    def connection_state(self, instance_name: str) -> dict:
        return self._request("GET", f"/instance/connectionState/{instance_name}")

    def instance_exists(self, instance_name: str) -> bool:
        try:
            result = self._request(
                "GET",
                "/instance/fetchInstances",
                params={"instanceName": instance_name},
            )
        except EvolutionAPIError as exc:
            if exc.status_code == 404:
                return False
            raise
        if isinstance(result, list):
            return len(result) > 0
        return bool(result)

    def fetch_instance(self, instance_name: str) -> Optional[dict]:
        try:
            result = self._request(
                "GET",
                "/instance/fetchInstances",
                params={"instanceName": instance_name},
            )
        except EvolutionAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        if isinstance(result, list) and result:
            return result[0]
        return None

    def logout_instance(self, instance_name: str) -> dict:
        return self._request("DELETE", f"/instance/logout/{instance_name}")

    def delete_instance(self, instance_name: str) -> dict:
        return self._request("DELETE", f"/instance/delete/{instance_name}")

    def send_text(self, instance_name: str, number: str, text: str) -> dict:
        payload = {"number": number, "text": text}
        return self._request(
            "POST", f"/message/sendText/{instance_name}", json=payload, timeout=30.0
        )

    def send_media(
        self,
        instance_name: str,
        number: str,
        *,
        media_b64: str,
        mimetype: str,
        caption: str = "",
        filename: str = "image.jpg",
    ) -> dict:
        payload = {
            "number": number,
            "mediatype": "image",
            "mimetype": mimetype,
            "caption": caption,
            "media": media_b64,
            "fileName": filename,
        }
        return self._request(
            "POST", f"/message/sendMedia/{instance_name}", json=payload, timeout=45.0
        )

    def send_buttons(
        self,
        instance_name: str,
        number: str,
        *,
        title: str,
        description: str,
        footer: str,
        buttons: list[dict],
    ) -> dict:
        payload = {
            "number": number,
            "title": title[:60],
            "description": description[:1024],
            "footer": footer[:60],
            "buttons": buttons,
        }
        return self._request(
            "POST", f"/message/sendButtons/{instance_name}", json=payload, timeout=45.0
        )

    def ensure_realtime_settings(self, instance_name: str) -> None:
        """Evita descarga masiva de historial al conectar (modo WhatsApp Web)."""
        try:
            self.set_settings(
                instance_name,
                {
                    "rejectCall": False,
                    "groupsIgnore": True,
                    "alwaysOnline": False,
                    "readMessages": False,
                    "readStatus": False,
                    "syncFullHistory": False,
                },
                timeout=8.0,
            )
        except EvolutionAPIError as exc:
            log.warning("ensure_realtime_settings %s: %s", instance_name, exc)

    def set_settings(self, instance_name: str, settings: dict, *, timeout: Optional[float] = 15.0) -> dict:
        return self._request(
            "POST",
            f"/settings/set/{instance_name}",
            json=settings,
            timeout=timeout,
        )

    def find_chats(self, instance_name: str, body: Optional[dict] = None) -> list:
        result = self._request(
            "POST",
            f"/chat/findChats/{instance_name}",
            json=body or {},
            timeout=60.0,
        )
        return result if isinstance(result, list) else []

    def find_contacts(self, instance_name: str, body: Optional[dict] = None) -> list:
        result = self._request(
            "POST",
            f"/chat/findContacts/{instance_name}",
            json=body or {},
            timeout=60.0,
        )
        return result if isinstance(result, list) else []

    def find_messages(
        self,
        instance_name: str,
        remote_jid: str,
        *,
        limit: int = 30,
    ) -> dict:
        payload = {
            "where": {"key": {"remoteJid": remote_jid}},
            "page": 1,
            "offset": limit,
        }
        result = self._request(
            "POST",
            f"/chat/findMessages/{instance_name}",
            json=payload,
            timeout=60.0,
        )
        return result if isinstance(result, dict) else {}

    def find_recent_messages(
        self,
        instance_name: str,
        *,
        limit: int = 200,
        page: int = 1,
    ) -> dict:
        """Todos los mensajes recientes de la instancia (paginado)."""
        payload = {"where": {}, "page": page, "offset": limit}
        result = self._request(
            "POST",
            f"/chat/findMessages/{instance_name}",
            json=payload,
            timeout=60.0,
        )
        return result if isinstance(result, dict) else {}

    def fetch_chat_states(self, instance_name: str) -> list:
        result = self._request(
            "GET",
            f"/chat/chatStates/{instance_name}",
            timeout=15.0,
        )
        return result if isinstance(result, list) else []

    def fetch_profile(self, instance_name: str, number: str) -> dict:
        """Nombre de perfil WhatsApp para un número (pushName / notify del perfil)."""
        digits = "".join(ch for ch in str(number) if ch.isdigit())
        result = self._request(
            "POST",
            f"/chat/fetchProfile/{instance_name}",
            json={"number": digits},
            timeout=20.0,
        )
        return result if isinstance(result, dict) else {}

    def fetch_business_profile(self, instance_name: str, number: str) -> dict:
        digits = "".join(ch for ch in str(number) if ch.isdigit())
        result = self._request(
            "POST",
            f"/chat/fetchBusinessProfile/{instance_name}",
            json={"number": digits},
            timeout=20.0,
        )
        return result if isinstance(result, dict) else {}

    def find_message_by_key(
        self,
        instance_name: str,
        *,
        message_id: str,
        remote_jid: str,
    ) -> Optional[dict]:
        """Busca un mensaje en Evolution por su ID y remoteJid."""
        payload = {
            "where": {
                "key": {
                    "id": message_id,
                    "remoteJid": remote_jid,
                }
            },
            "page": 1,
            "offset": 1,
        }
        try:
            result = self._request(
                "POST",
                f"/chat/findMessages/{instance_name}",
                json=payload,
                timeout=15.0,
            )
            messages = result.get("messages", {}).get("records") or result.get("records") or []
            if not messages:
                messages = result.get("messages") or []
            if isinstance(messages, list) and messages:
                return messages[0]
        except EvolutionAPIError:
            pass
        return None

    def get_media_base64(self, instance_name: str, message_obj: dict, *, convert_to_mp4: bool = False) -> dict:
        """Descarga y desencripta media de WhatsApp. Retorna {base64, mediaType, mimetype}."""
        payload = {
            "message": message_obj,
            "convertToMp4": convert_to_mp4,
        }
        result = self._request(
            "POST",
            f"/chat/getBase64FromMediaMessage/{instance_name}",
            json=payload,
            timeout=60.0,
        )
        return result if isinstance(result, dict) else {}


evolution_client = EvolutionClient()
