"""Conexión directa con la web del negocio (MCP + OAuth): el dueño autoriza y la IA consulta en vivo."""
from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app.application.availability import site_connector as sc
from app.infrastructure.ai import ai_text_provider, deepseek_client
from app.infrastructure.ai.deepseek_client import DeepSeekError
from app.shared.core import secret_box
from tests.conftest import requires_db

TOOLS = [
    {"name": "check_availability", "title": "Disponibilidad", "description": "Fechas libres por alojamiento",
     "inputSchema": {"type": "object", "properties": {"check_in": {"type": "string"}}}},
    {"name": "get_reservation", "description": "Detalle de una reserva (datos del huésped)", "inputSchema": {"type": "object"}},
]


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def getdel(self, key):
        return self.store.pop(key, None)

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(sc, "get_redis", lambda: redis)
    return redis


def _meta():
    return {
        "server_url": "https://glamping.example.com/mcp",
        "resource": "https://glamping.example.com/mcp",
        "resource_name": "Glamping",
        "authorization_endpoint": "https://auth.example.com/oauth/authorize",
        "token_endpoint": "https://auth.example.com/oauth/token",
        "registration_endpoint": "https://auth.example.com/oauth/register",
        "scopes": [],
    }


def test_secret_box_roundtrip():
    sealed = secret_box.encrypt("token-secreto")
    assert "token-secreto" not in sealed
    assert secret_box.decrypt(sealed) == "token-secreto"
    assert secret_box.decrypt("basura") == ""


def test_start_builds_pkce_link_and_remembers_state(monkeypatch, fake_redis):
    monkeypatch.setattr(sc, "discover", lambda url: _meta())
    monkeypatch.setattr(sc, "_register_client", lambda meta: ("cliente-1", ""))
    tenant, user = uuid.uuid4(), uuid.uuid4()
    url = sc.start_connection(tenant, user, "https://glamping.example.com/mcp")
    query = parse_qs(urlsplit(url).query)
    assert url.startswith("https://auth.example.com/oauth/authorize?")
    assert query["code_challenge_method"] == ["S256"]
    assert query["client_id"] == ["cliente-1"]
    assert query["resource"] == ["https://glamping.example.com/mcp"]
    assert query["redirect_uri"][0].endswith("/api/v1/site-connection/callback")
    saved = json.loads(fake_redis.store[f"siteconn:state:{query['state'][0]}"])
    assert saved["tenant_id"] == str(tenant) and saved["verifier"]


def test_parse_rpc_reads_event_stream():
    resp = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text='event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n\n',
    )
    assert sc._parse_rpc(resp, 2) == {"tools": []}
    with pytest.raises(sc._Unauthorized):
        sc._parse_rpc(httpx.Response(401), 2)


def _conn(**extra):
    return {
        "server_url": "https://glamping.example.com/mcp",
        "resource": "https://glamping.example.com/mcp",
        "resource_name": "Glamping",
        "token_endpoint": "https://auth.example.com/oauth/token",
        "client_id": "cliente-1",
        "client_secret": "",
        "access_token": secret_box.encrypt("viejo"),
        "refresh_token": secret_box.encrypt("refresco"),
        "expires_at": time.time() + 3600,
        "status": "ok",
        "tools": [
            {"name": t["name"], "title": t.get("title", ""), "description": t["description"], "input_schema": t["inputSchema"]}
            for t in TOOLS
        ],
        "allowed_tools": ["check_availability"],
        **extra,
    }


def test_ai_only_sees_allowed_tools():
    tools = sc.ai_tools(SimpleNamespace(site_connection=_conn()))
    assert [t["function"]["name"] for t in tools] == ["check_availability"]
    assert sc.ai_tools(SimpleNamespace(site_connection=_conn(status="expired"))) == []
    assert "Nunca compartas datos de otras reservas" in sc.site_tools_block(tools)


def test_run_tool_refreshes_expired_token_and_blocks_others(monkeypatch, fake_redis):
    conn = _conn()
    saved = {}
    monkeypatch.setattr(sc, "_load", lambda tenant_id: dict(conn))
    monkeypatch.setattr(sc, "_save", lambda tenant_id, c: saved.update(c))
    calls = []

    def fake_rpc(c, method, params):
        calls.append(secret_box.decrypt(c["access_token"]))
        if secret_box.decrypt(c["access_token"]) == "viejo":
            raise sc._Unauthorized()
        return {"content": [{"type": "text", "text": "Xue libre el 25 y 26"}]}

    monkeypatch.setattr(sc, "_rpc", fake_rpc)
    monkeypatch.setattr(sc, "_token_request", lambda c, form: {"access_token": "nuevo", "expires_in": 3600})
    tenant = uuid.uuid4()
    assert sc.run_ai_tool(tenant, "check_availability", {"check_in": "2026-10-25"}) == "Xue libre el 25 y 26"
    assert calls == ["viejo", "nuevo"]
    assert secret_box.decrypt(saved["access_token"]) == "nuevo"
    assert sc.run_ai_tool(tenant, "get_reservation", {}) == "Esa consulta no está permitida."


def test_run_tool_failure_tells_ai_not_to_promise(monkeypatch, fake_redis):
    monkeypatch.setattr(sc, "_load", lambda tenant_id: _conn())

    def boom(*_a):
        raise sc.SiteConnectionError("caído")

    monkeypatch.setattr(sc, "_rpc", boom)
    assert sc.run_ai_tool(uuid.uuid4(), "check_availability", {}) == sc.UNAVAILABLE_TEXT


def test_deepseek_runs_tools_then_answers(monkeypatch):
    monkeypatch.setattr(deepseek_client, "is_configured", lambda: True)
    replies = iter([
        {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                        "function": {"name": "check_availability", "arguments": '{"check_in":"2026-10-25"}'}}]},
        {"content": '{"reply":"¡Sí! La Xue está libre."}'},
    ])
    payloads = []

    def fake_message(payload, timeout):
        payloads.append(json.loads(json.dumps(payload)))
        return next(replies)

    monkeypatch.setattr(deepseek_client, "_completion_message", fake_message)
    ran = []
    out = deepseek_client.chat_completion_with_tools(
        [{"role": "user", "content": "¿Xue el 25?"}],
        tools=[{"type": "function", "function": {"name": "check_availability", "parameters": {"type": "object"}}}],
        run_tool=lambda name, args: ran.append((name, args)) or "Libre",
    )
    assert out == '{"reply":"¡Sí! La Xue está libre."}'
    assert ran == [("check_availability", {"check_in": "2026-10-25"})]
    assert payloads[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "Libre"}


def test_tools_fallback_answers_without_promising(monkeypatch):
    monkeypatch.setattr(deepseek_client, "is_configured", lambda: True)

    def fail(*_a, **_k):
        raise DeepSeekError("caído", status_code=500)

    monkeypatch.setattr(deepseek_client, "chat_completion_with_tools", fail)
    seen = {}
    monkeypatch.setattr(ai_text_provider, "chat_completion", lambda messages, **k: seen.setdefault("m", messages) and "ok")
    assert ai_text_provider.chat_completion_tools(
        [{"role": "user", "content": "hola"}], tools=[{}], run_tool=lambda *_: "", unavailable_note="NO PROMETAS"
    ) == "ok"
    assert seen["m"][-1] == {"role": "system", "content": "NO PROMETAS"}


def _register(client: TestClient) -> dict:
    tag = uuid.uuid4().hex[:8]
    res = client.post(
        "/api/v1/auth/register",
        json={
            "business_name": f"Glamping {tag}",
            "owner_name": "Dueña",
            "email": f"sitio-{tag}@test.com",
            "password": "password123",
            "accept_legal": True,
        },
    )
    assert res.status_code == 201, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@requires_db
def test_owner_connects_site_end_to_end(client: TestClient, monkeypatch, fake_redis):
    monkeypatch.setattr(sc, "discover", lambda url: _meta())
    monkeypatch.setattr(sc, "_register_client", lambda meta: ("cliente-1", ""))
    monkeypatch.setattr(sc, "_token_request", lambda c, form: {"access_token": "acceso", "refresh_token": "refresco", "expires_in": 3600})
    monkeypatch.setattr(sc, "_rpc", lambda c, method, params: {"tools": TOOLS})
    headers = _register(client)

    started = client.post("/api/v1/site-connection/start", headers=headers, json={"url": "https://glamping.example.com/mcp"})
    assert started.status_code == 200, started.text
    state = parse_qs(urlsplit(started.json()["authorize_url"]).query)["state"][0]

    back = client.get(f"/api/v1/site-connection/callback?state={state}&code=abc", follow_redirects=False)
    assert back.status_code == 303
    assert back.headers["location"].endswith("/panel?site=ok")

    reused = client.get(f"/api/v1/site-connection/callback?state={state}&code=abc", follow_redirects=False)
    assert "site=error" in reused.headers["location"]

    view = client.get("/api/v1/site-connection", headers=headers).json()
    assert view["connected"] and view["name"] == "Glamping"
    assert {t["name"]: t["allowed"] for t in view["tools"]} == {"check_availability": True, "get_reservation": False}
    assert "acceso" not in json.dumps(view)

    updated = client.put("/api/v1/site-connection/tools", headers=headers, json={"allowed": ["get_reservation", "inventada"]})
    assert [t["name"] for t in updated.json()["tools"] if t["allowed"]] == ["get_reservation"]

    assert client.delete("/api/v1/site-connection", headers=headers).json() == {"connected": False}
    assert client.get("/api/v1/site-connection", headers=headers).json() == {"connected": False}
