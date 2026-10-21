"""Conexión directa con la web del negocio por MCP: el dueño autoriza a Omitel una vez (OAuth con
PKCE y registro automático) y la IA consulta en vivo las herramientas que él permita, p. ej. la
disponibilidad de cada alojamiento."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from sqlalchemy.orm import Session

from app.application.availability.availability_service import CalendarError, _require_public_host, normalize_url
from app.application.billing.tenant_profile_service import get_or_create_tenant_profile
from app.config import settings
from app.infrastructure.cache.redis_client import get_redis
from app.shared.core import secret_box

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
CALLBACK_PATH = "/api/v1/site-connection/callback"
STATE_TTL_SECONDS = 15 * 60
HTTP_TIMEOUT_SECONDS = 12.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TOOL_RESULT_CHARS = 4000
MAX_TOOLS = 40
# Solo las de disponibilidad quedan activas solas: las de reservas muestran datos de otros huéspedes.
_DEFAULT_ALLOWED = re.compile(r"availab|disponib", re.I)

UNAVAILABLE_TEXT = (
    "No se pudo consultar la web del negocio ahora. No confirmes disponibilidad: dile al cliente que "
    "se la confirmas en un momento."
)


class SiteConnectionError(CalendarError):
    """Mensaje listo para mostrarle al dueño."""


class _Unauthorized(Exception):
    pass


def redirect_uri() -> str:
    return f"{settings.app_public_url.rstrip('/')}{CALLBACK_PATH}"


def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Petición a un servidor público (nunca a la red interna), siguiendo redirecciones revisadas."""
    for _ in range(4):
        url = normalize_url(url)
        _require_public_host(urlsplit(url).hostname or "")
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False) as client:
                resp = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise SiteConnectionError("No pude comunicarme con tu sitio web. Intenta de nuevo en un rato.") from exc
        if resp.is_redirect and method == "GET":
            url = urljoin(url, resp.headers.get("location", ""))
            continue
        if len(resp.content) > MAX_RESPONSE_BYTES:
            raise SiteConnectionError("Tu sitio respondió algo demasiado grande.")
        return resp
    raise SiteConnectionError("Tu sitio redirige demasiadas veces.")


def _get_json(url: str) -> Optional[dict]:
    try:
        resp = _request("GET", url, headers={"Accept": "application/json"})
        data = resp.json() if resp.status_code == 200 else None
    except (SiteConnectionError, CalendarError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _initialize_payload() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "Omitel", "version": "1.0"},
        },
    }


def discover(server_url: str) -> dict:
    """Cómo autoriza ese servidor MCP (RFC 9728 + RFC 8414)."""
    server_url = normalize_url(server_url)
    parts = urlsplit(server_url)
    origin = f"https://{parts.netloc}"
    probe = _request(
        "POST",
        server_url,
        json=_initialize_payload(),
        headers={"Accept": "application/json, text/event-stream"},
    )
    if probe.status_code not in (401, 403):
        raise SiteConnectionError(
            "Esa dirección no pide autorización o no es un servidor MCP. Revisa que sea la dirección para "
            "asistentes de IA de tu sitio."
        )
    candidates = []
    hinted = re.search(r'resource_metadata="([^"]+)"', probe.headers.get("www-authenticate", ""))
    if hinted:
        candidates.append(hinted.group(1))
    candidates += [
        f"{origin}/.well-known/oauth-protected-resource{parts.path.rstrip('/')}",
        f"{origin}/.well-known/oauth-protected-resource",
    ]
    resource = next((m for m in map(_get_json, candidates) if m and m.get("authorization_servers")), None)
    if not resource:
        raise SiteConnectionError("Tu sitio no publica cómo autorizar asistentes de IA.")
    issuer = str(resource["authorization_servers"][0]).rstrip("/")
    issuer_parts = urlsplit(issuer)
    issuer_origin = f"{issuer_parts.scheme}://{issuer_parts.netloc}"
    auth = next(
        (
            m
            for m in map(
                _get_json,
                [
                    f"{issuer_origin}/.well-known/oauth-authorization-server{issuer_parts.path}",
                    f"{issuer}/.well-known/oauth-authorization-server",
                    f"{issuer_origin}/.well-known/openid-configuration{issuer_parts.path}",
                    f"{issuer}/.well-known/openid-configuration",
                ],
            )
            if m and m.get("authorization_endpoint") and m.get("token_endpoint")
        ),
        None,
    )
    if not auth:
        raise SiteConnectionError("No encontré el servidor de autorización de tu sitio.")
    methods = auth.get("code_challenge_methods_supported")
    if methods and "S256" not in methods:
        raise SiteConnectionError("Tu sitio no usa una autorización compatible (falta PKCE S256).")
    if not auth.get("registration_endpoint"):
        raise SiteConnectionError("Tu sitio no permite registrar asistentes nuevos automáticamente.")
    return {
        "server_url": server_url,
        "resource": str(resource.get("resource") or server_url),
        "resource_name": str(resource.get("resource_name") or parts.netloc)[:80],
        "authorization_endpoint": normalize_url(auth["authorization_endpoint"]),
        "token_endpoint": normalize_url(auth["token_endpoint"]),
        "registration_endpoint": normalize_url(auth["registration_endpoint"]),
        "scopes": resource.get("scopes_supported") or [],
    }


def _register_client(meta: dict) -> tuple[str, str]:
    resp = _request(
        "POST",
        meta["registration_endpoint"],
        json={
            "client_name": "Omitel",
            "client_uri": settings.site_public_url.rstrip("/"),
            "redirect_uris": [redirect_uri()],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code not in (200, 201) or not data.get("client_id"):
        log.warning("Registro MCP rechazado %s: %s", resp.status_code, resp.text[:300])
        raise SiteConnectionError("Tu sitio no aceptó registrar a Omitel. Intenta de nuevo en un rato.")
    return str(data["client_id"]), str(data.get("client_secret") or "")


def start_connection(tenant_id: uuid.UUID, user_id: uuid.UUID, server_url: str) -> str:
    """Devuelve el enlace donde el dueño inicia sesión en su web y autoriza a Omitel."""
    meta = discover(server_url)
    client_id, client_secret = _register_client(meta)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    get_redis().set(
        f"siteconn:state:{state}",
        json.dumps({
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "verifier": verifier,
            "meta": meta,
            "client_id": client_id,
            "client_secret": secret_box.encrypt(client_secret),
        }),
        ex=STATE_TTL_SECONDS,
    )
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": meta["resource"],
    }
    if meta["scopes"]:
        params["scope"] = " ".join(meta["scopes"])
    separator = "&" if "?" in meta["authorization_endpoint"] else "?"
    return f"{meta['authorization_endpoint']}{separator}{urlencode(params)}"


def _token_request(conn: dict, form: dict) -> dict:
    form = {**form, "client_id": conn["client_id"], "resource": conn["resource"]}
    client_secret = secret_box.decrypt(conn.get("client_secret") or "")
    if client_secret:
        form["client_secret"] = client_secret
    resp = _request("POST", conn["token_endpoint"], data=form, headers={"Accept": "application/json"})
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200 or not data.get("access_token"):
        log.warning("Token MCP rechazado %s: %s", resp.status_code, str(data.get("error") or "")[:100])
        raise SiteConnectionError("Tu sitio no aceptó la autorización. Vuelve a conectarlo.")
    return data


def _store_tokens(conn: dict, data: dict) -> dict:
    conn = dict(conn)
    conn["access_token"] = secret_box.encrypt(str(data["access_token"]))
    if data.get("refresh_token"):
        conn["refresh_token"] = secret_box.encrypt(str(data["refresh_token"]))
    conn["expires_at"] = time.time() + int(data.get("expires_in") or 3600)
    return conn


def finish_connection(db: Session, state: str, code: str) -> uuid.UUID:
    raw = get_redis().getdel(f"siteconn:state:{state}") if state else None
    if not raw:
        raise SiteConnectionError("La autorización venció. Vuelve a darle a «Conectar».")
    pending = json.loads(raw)
    meta = pending["meta"]
    conn = {
        "server_url": meta["server_url"],
        "resource": meta["resource"],
        "resource_name": meta["resource_name"],
        "token_endpoint": meta["token_endpoint"],
        "client_id": pending["client_id"],
        "client_secret": pending["client_secret"],
    }
    data = _token_request(
        conn,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(),
            "code_verifier": pending["verifier"],
        },
    )
    conn = _store_tokens(conn, data)
    conn.update(status="ok", last_error="", connected_at=time.time(), tools=[], allowed_tools=[])
    tenant_id = uuid.UUID(pending["tenant_id"])
    try:
        tools = _list_tools(tenant_id, conn)
    except (_Unauthorized, SiteConnectionError, CalendarError) as exc:
        log.warning("Conectado pero sin lista de herramientas tenant=%s: %s", tenant_id, exc)
        tools = []
        conn["last_error"] = "Conectado, pero no pude leer qué consultas ofrece tu sitio. Dale a «Actualizar»."
    conn["tools"] = tools
    conn["allowed_tools"] = [t["name"] for t in tools if _DEFAULT_ALLOWED.search(t["name"])]
    profile = get_or_create_tenant_profile(db, tenant_id)
    profile.site_connection = conn
    db.flush()
    return tenant_id


def _load(tenant_id: uuid.UUID) -> Optional[dict]:
    from app.domain.entities import TenantProfile
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        profile = db.query(TenantProfile).filter(TenantProfile.tenant_id == tenant_id).first()
        conn = profile.site_connection if profile else None
        return dict(conn) if isinstance(conn, dict) else None


def _save(tenant_id: uuid.UUID, conn: dict) -> None:
    from app.domain.entities import TenantProfile
    from app.infrastructure.persistence.database import SessionLocal

    with SessionLocal() as db:
        profile = db.query(TenantProfile).filter(TenantProfile.tenant_id == tenant_id).first()
        current = profile.site_connection if profile else None
        # Si el dueño desconectó o reconectó mientras tanto, no se pisa.
        if not isinstance(current, dict) or current.get("client_id") != conn.get("client_id"):
            return
        profile.site_connection = conn
        db.commit()


def _refresh(tenant_id: uuid.UUID, conn: dict) -> dict:
    refresh_token = secret_box.decrypt(conn.get("refresh_token") or "")
    if not refresh_token:
        raise _Unauthorized()
    client = get_redis()
    lock = f"siteconn:refresh:{tenant_id}"
    if not client.set(lock, "1", nx=True, ex=30):
        time.sleep(1.5)
        latest = _load(tenant_id)
        if latest and latest.get("expires_at", 0) > time.time() + 30:
            return latest
        raise _Unauthorized()
    try:
        data = _token_request(conn, {"grant_type": "refresh_token", "refresh_token": refresh_token})
    except SiteConnectionError:
        conn = {**conn, "status": "expired", "last_error": "La autorización venció: vuelve a conectar tu sitio."}
        _save(tenant_id, conn)
        raise _Unauthorized()
    finally:
        client.delete(lock)
    conn = _store_tokens(conn, data)
    _save(tenant_id, conn)
    return conn


def _parse_rpc(resp: httpx.Response, rpc_id: int) -> dict:
    if resp.status_code in (401, 403):
        raise _Unauthorized()
    if resp.status_code >= 400:
        raise SiteConnectionError(f"Tu sitio respondió con error ({resp.status_code}).")
    messages: list[Any] = []
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    messages.append(json.loads(line[5:].strip()))
                except ValueError:
                    continue
    else:
        try:
            body = resp.json()
        except ValueError as exc:
            raise SiteConnectionError("Tu sitio respondió algo que no entiendo.") from exc
        messages = body if isinstance(body, list) else [body]
    for msg in messages:
        if isinstance(msg, dict) and msg.get("id") == rpc_id:
            if msg.get("error"):
                raise SiteConnectionError(str(msg["error"].get("message") or "Error del sitio")[:200])
            return msg.get("result") or {}
    raise SiteConnectionError("Tu sitio no respondió la consulta.")


def _rpc(conn: dict, method: str, params: dict) -> dict:
    url = normalize_url(conn["server_url"])
    _require_public_host(urlsplit(url).hostname or "")
    headers = {
        "Authorization": f"Bearer {secret_box.decrypt(conn.get('access_token') or '')}",
        "Accept": "application/json, text/event-stream",
    }
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False) as client:
            init = client.post(url, json=_initialize_payload(), headers=headers)
            version = _parse_rpc(init, 1).get("protocolVersion") or PROTOCOL_VERSION
            headers["MCP-Protocol-Version"] = str(version)
            if init.headers.get("mcp-session-id"):
                headers["Mcp-Session-Id"] = init.headers["mcp-session-id"]
            client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
            resp = client.post(url, json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params}, headers=headers)
            return _parse_rpc(resp, 2)
    except httpx.HTTPError as exc:
        raise SiteConnectionError("No pude comunicarme con tu sitio web.") from exc


def _call(tenant_id: uuid.UUID, conn: dict, method: str, params: dict) -> dict:
    if conn.get("expires_at", 0) < time.time() + 60:
        conn = _refresh(tenant_id, conn)
    try:
        return _rpc(conn, method, params)
    except _Unauthorized:
        return _rpc(_refresh(tenant_id, conn), method, params)


def _list_tools(tenant_id: uuid.UUID, conn: dict) -> list[dict]:
    result = _call(tenant_id, conn, "tools/list", {})
    tools = []
    for tool in (result.get("tools") or [])[:MAX_TOOLS]:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
        tools.append({
            "name": str(tool["name"])[:120],
            "title": str(tool.get("title") or "")[:120],
            "description": str(tool.get("description") or "")[:1000],
            "input_schema": schema,
        })
    return tools


def refresh_tools(tenant_id: uuid.UUID) -> dict:
    conn = _load(tenant_id)
    if not conn:
        raise SiteConnectionError("Tu sitio no está conectado.")
    try:
        tools = _list_tools(tenant_id, conn)
    except _Unauthorized as exc:
        raise SiteConnectionError("La autorización venció: vuelve a conectar tu sitio.") from exc
    conn = _load(tenant_id) or conn
    names = {t["name"] for t in tools}
    conn.update(tools=tools, allowed_tools=[n for n in conn.get("allowed_tools") or [] if n in names], status="ok", last_error="")
    _save(tenant_id, conn)
    return conn


def set_allowed_tools(db: Session, tenant_id: uuid.UUID, names: list[str]) -> dict:
    profile = get_or_create_tenant_profile(db, tenant_id)
    conn = dict(profile.site_connection or {})
    if not conn:
        raise SiteConnectionError("Tu sitio no está conectado.")
    known = {t["name"] for t in conn.get("tools") or []}
    conn["allowed_tools"] = [n for n in dict.fromkeys(names) if n in known]
    profile.site_connection = conn
    db.flush()
    return conn


def disconnect(db: Session, tenant_id: uuid.UUID) -> None:
    profile = get_or_create_tenant_profile(db, tenant_id)
    profile.site_connection = None
    db.flush()


def public_view(conn: Optional[dict]) -> dict:
    if not conn:
        return {"connected": False}
    allowed = set(conn.get("allowed_tools") or [])
    return {
        "connected": True,
        "server_url": conn.get("server_url"),
        "name": conn.get("resource_name"),
        "status": conn.get("status") or "ok",
        "last_error": conn.get("last_error") or "",
        "tools": [
            {"name": t["name"], "title": t.get("title") or "", "description": t.get("description") or "", "allowed": t["name"] in allowed}
            for t in conn.get("tools") or []
        ],
    }


def _ai_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:64]


def ai_tools(profile: Any) -> list[dict]:
    """Herramientas de la web que la IA puede usar en este chat (formato OpenAI)."""
    conn = getattr(profile, "site_connection", None) if profile is not None else None
    if not isinstance(conn, dict) or conn.get("status") != "ok":
        return []
    allowed = set(conn.get("allowed_tools") or [])
    out = []
    for tool in conn.get("tools") or []:
        if tool["name"] not in allowed:
            continue
        schema = tool.get("input_schema") or {}
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        out.append({
            "type": "function",
            "function": {
                "name": _ai_name(tool["name"]),
                "description": (tool.get("description") or tool.get("title") or tool["name"])[:1000],
                "parameters": schema,
            },
        })
    return out


def _result_text(result: dict) -> str:
    parts = [str(c.get("text") or "") for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
    text = "\n".join(p for p in parts if p) or (json.dumps(result["structuredContent"], ensure_ascii=False) if result.get("structuredContent") else "")
    if result.get("isError"):
        text = f"La consulta falló: {text}"
    return (text or "Sin resultados.")[:TOOL_RESULT_CHARS]


def run_ai_tool(tenant_id: uuid.UUID, ai_name: str, arguments: dict) -> str:
    conn = _load(tenant_id)
    if not conn or conn.get("status") != "ok":
        return UNAVAILABLE_TEXT
    allowed = set(conn.get("allowed_tools") or [])
    real = next((t["name"] for t in conn.get("tools") or [] if _ai_name(t["name"]) == ai_name and t["name"] in allowed), None)
    if not real:
        return "Esa consulta no está permitida."
    try:
        return _result_text(_call(tenant_id, conn, "tools/call", {"name": real, "arguments": arguments or {}}))
    except (_Unauthorized, SiteConnectionError, CalendarError) as exc:
        log.warning("Consulta a la web del negocio falló tenant=%s tool=%s: %s", tenant_id, real, exc)
        return UNAVAILABLE_TEXT


def site_tools_block(tools: list[dict]) -> str:
    if not tools:
        return ""
    return (
        "\n\nPuedes consultar en vivo la web del negocio con herramientas (disponibilidad y lo que el dueño permitió).\n"
        "- Antes de decir que hay cupo o que una fecha está libre, consulta con las fechas exactas que pidió el "
        "cliente (formato AAAA-MM-DD). Si aún no dijo fechas, pregúntaselas primero.\n"
        "- Responde con lo que diga la consulta. Si falla o no es clara, di que lo confirmas en un momento.\n"
        "- Nunca compartas datos de otras reservas ni de otros huéspedes (nombres, teléfonos, correos, pagos)."
    )
