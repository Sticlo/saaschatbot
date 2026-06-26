from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)


class ChatwootAPIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class ChatwootClient:
    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_token: Optional[str] = None,
        account_id: Optional[str] = None,
        timeout: float = 15.0,
    ):
        self.base_url = (base_url or settings.chatwoot_base_url()).rstrip("/")
        self.api_token = api_token or settings.chatwoot_api_token
        self.account_id = str(account_id or settings.chatwoot_account_id)
        self.timeout = timeout

    def _headers(self) -> dict:
        return {
            "api_access_token": self.api_token,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        if not self.api_token:
            raise ChatwootAPIError("CHATWOOT_API_TOKEN no configurado")
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=timeout or self.timeout) as client:
                response = client.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json,
                )
        except httpx.RequestError as exc:
            raise ChatwootAPIError(f"No se pudo conectar con Chatwoot: {exc}") from exc

        if response.status_code >= 400:
            raise ChatwootAPIError(
                f"Chatwoot API error {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    def list_inboxes(self) -> list[dict]:
        result = self._request("GET", f"/api/v1/accounts/{self.account_id}/inboxes")
        if isinstance(result, dict):
            payload = result.get("payload")
            if isinstance(payload, list):
                return payload
        return result if isinstance(result, list) else []

    def find_inbox_by_name(self, name: str) -> Optional[dict]:
        target = (name or "").strip().lower()
        for inbox in self.list_inboxes():
            if str(inbox.get("name") or "").strip().lower() == target:
                return inbox
        return None

    def ensure_account_webhook(self, target_url: str) -> None:
        subscriptions = [
            "conversation_created",
            "conversation_updated",
            "message_created",
            "message_updated",
            "contact_created",
        ]
        existing = self._request("GET", f"/api/v1/accounts/{self.account_id}/webhooks")
        hooks = existing.get("payload") if isinstance(existing, dict) else []
        if isinstance(hooks, list):
            for hook in hooks:
                if str(hook.get("url") or "").rstrip("/") == target_url.rstrip("/"):
                    return
        self._request(
            "POST",
            f"/api/v1/accounts/{self.account_id}/webhooks",
            json={"url": target_url, "subscriptions": subscriptions},
        )
        log.info("Webhook Chatwoot registrado url=%s", target_url)


chatwoot_client = ChatwootClient()
