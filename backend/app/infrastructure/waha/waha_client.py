from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)


class WahaAPIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class WahaClient:
    """Cliente WAHA (WhatsApp HTTP API) — motor WEBJS = Chrome real."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.base_url = (base_url or settings.waha_api_url).rstrip("/")
        self.api_key = api_key or settings.waha_api_key
        self.timeout = timeout

    def _headers(self, *, json_accept: bool = False) -> dict:
        headers = {"X-Api-Key": self.api_key}
        if json_accept:
            headers["Accept"] = "application/json"
            headers["Content-Type"] = "application/json"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        timeout: Optional[float] = None,
        json_accept: bool = True,
    ) -> Any:
        if not self.api_key:
            raise WahaAPIError("WAHA_API_KEY no configurado")
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=timeout or self.timeout) as client:
                response = client.request(
                    method,
                    url,
                    headers=self._headers(json_accept=json_accept),
                    json=json,
                    params=params,
                )
        except httpx.RequestError as exc:
            raise WahaAPIError(
                f"WAHA no responde en {self.base_url} — "
                f"docker compose -f deploy/docker-compose.yml up -d waha ({exc})"
            ) from exc

        if response.status_code >= 400:
            raise WahaAPIError(
                f"WAHA API error {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    def session_exists(self, name: str) -> bool:
        try:
            self.get_session(name)
            return True
        except WahaAPIError as exc:
            if exc.status_code == 404:
                return False
            if "No se pudo conectar" in str(exc):
                raise WahaAPIError(
                    "WAHA no responde en "
                    f"{self.base_url} — levanta: docker compose -f deploy/docker-compose.yml up -d waha"
                ) from exc
            raise

    def get_session(self, name: str) -> dict:
        result = self._request("GET", f"/api/sessions/{name}")
        return result if isinstance(result, dict) else {}

    def create_session(self, name: str, *, engine: str = "WEBJS") -> dict:
        payload = {
            "name": name,
            "start": True,
            "config": {
                "engine": engine,
            },
        }
        return self._request("POST", "/api/sessions", json=payload, timeout=90.0)

    def start_session(self, name: str) -> dict:
        return self._request("POST", f"/api/sessions/{name}/start", timeout=90.0)

    def stop_session(self, name: str) -> dict:
        return self._request("POST", f"/api/sessions/{name}/stop", timeout=60.0)

    def logout_session(self, name: str) -> dict:
        return self._request("POST", "/api/sessions/logout", json={"name": name}, timeout=60.0)

    def delete_session(self, name: str) -> dict:
        return self._request("DELETE", f"/api/sessions/{name}", timeout=60.0)

    def get_me(self, name: str) -> dict:
        result = self._request("GET", f"/api/sessions/{name}/me")
        return result if isinstance(result, dict) else {}

    def get_qr_base64(self, name: str) -> Optional[str]:
        result = self._request("GET", f"/api/{name}/auth/qr", json_accept=True)
        if not isinstance(result, dict):
            return None
        for key in ("base64", "data", "qrcode", "qr"):
            raw = result.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw if raw.startswith("data:") else f"data:image/png;base64,{raw}"
        return None

    def send_text(self, session: str, chat_id: str, text: str) -> dict:
        payload = {"session": session, "chatId": chat_id, "text": text}
        return self._request("POST", "/api/sendText", json=payload, timeout=45.0)

    def list_apps(self, session: str) -> list[dict]:
        result = self._request("GET", "/api/apps", params={"session": session})
        if isinstance(result, list):
            return [r for r in result if isinstance(r, dict)]
        if isinstance(result, dict):
            payload = result.get("payload") or result.get("data")
            if isinstance(payload, list):
                return [r for r in payload if isinstance(r, dict)]
        return []

    def ensure_chatwoot_app(
        self,
        *,
        session: str,
        chatwoot_url: str,
        account_id: int,
        account_token: str,
        inbox_id: int,
    ) -> dict:
        """Configura app Chatwoot nativa en WAHA (Chrome → Chatwoot, sin Evolution)."""
        config = {
            "linkPreview": "OFF",
            "locale": "es-ES",
            "url": chatwoot_url.rstrip("/"),
            "accountId": account_id,
            "accountToken": account_token,
            "inboxId": inbox_id,
            "commands": {"server": True, "queue": True},
            "conversations": {
                "sort": "created_newest",
                "status": ["open", "pending", "snoozed"],
            },
        }
        for app in self.list_apps(session):
            if str(app.get("app") or "").lower() == "chatwoot":
                app_id = app.get("id")
                if app_id:
                    return self._request(
                        "PUT",
                        f"/api/apps/{app_id}",
                        json={"session": session, "app": "chatwoot", "config": config, "enabled": True},
                        timeout=30.0,
                    )
        return self._request(
            "POST",
            "/api/apps",
            json={"session": session, "app": "chatwoot", "config": config, "enabled": True},
            timeout=30.0,
        )


waha_client = WahaClient()
