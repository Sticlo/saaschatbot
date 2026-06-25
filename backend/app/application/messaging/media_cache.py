"""
Caché local de media de WhatsApp.

Cuando el webhook de Evolution llega con base64: True, guarda la media en
static/media/<tenant_id>/<evolution_message_id>.<ext> para servirla después
sin depender de que Evolution DB tenga el mensaje disponible.
"""
from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Directorio raíz donde se guardan los archivos de media
_MEDIA_ROOT = Path(__file__).parent.parent.parent / "static" / "media"

_MIME_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "audio/ogg": "ogg",
    "audio/ogg; codecs=opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
}

_TYPE_MIME: dict[str, str] = {
    "image": "image/jpeg",
    "sticker": "image/webp",
    "video": "video/mp4",
    "audio": "audio/ogg",
    "ptt": "audio/ogg",
    "document": "application/octet-stream",
}


def _ext_for_mime(mimetype: str) -> str:
    base = (mimetype or "").split(";")[0].strip().lower()
    return _MIME_EXT.get(base, "bin")


def _cache_dir(tenant_id: str) -> Path:
    d = _MEDIA_ROOT / str(tenant_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_media_from_webhook(
    tenant_id: str,
    evolution_message_id: str,
    b64_data: str,
    media_type: str = "document",
    mimetype: str = "",
) -> Optional[str]:
    """
    Guarda la media recibida en el webhook (base64) a disco.
    Devuelve la ruta relativa al archivo guardado (para servir), o None si falla.
    """
    if not b64_data or not evolution_message_id:
        return None
    try:
        # Quitar el prefijo "data:...;base64," si viene así
        if "," in b64_data:
            b64_data = b64_data.split(",", 1)[1]

        raw = base64.b64decode(b64_data)

        # Determinar extensión
        mime = mimetype or _TYPE_MIME.get(media_type, "application/octet-stream")
        ext = _ext_for_mime(mime)

        safe_id = re.sub(r"[^A-Za-z0-9_\-]", "_", evolution_message_id)
        filename = f"{safe_id}.{ext}"
        path = _cache_dir(str(tenant_id)) / filename
        path.write_bytes(raw)

        return f"media/{tenant_id}/{filename}"
    except Exception as exc:
        log.warning("save_media_from_webhook id=%s: %s", evolution_message_id, exc)
        return None


def get_media_from_cache(
    tenant_id: str,
    evolution_message_id: str,
) -> Optional[dict]:
    """
    Busca la media guardada en disco para el evolution_message_id dado.
    Devuelve {"base64": str, "mimetype": str} o None.
    """
    if not evolution_message_id:
        return None
    try:
        safe_id = re.sub(r"[^A-Za-z0-9_\-]", "_", evolution_message_id)
        cache_dir = _MEDIA_ROOT / str(tenant_id)
        if not cache_dir.exists():
            return None
        # Buscar cualquier archivo que empiece con safe_id
        for f in cache_dir.iterdir():
            if f.stem == safe_id:
                raw = f.read_bytes()
                ext = f.suffix.lstrip(".")
                # Determinar mimetype a partir de extensión
                mime_map = {v: k for k, v in _MIME_EXT.items()}
                mimetype = mime_map.get(ext, f"application/{ext}")
                b64 = base64.b64encode(raw).decode()
                return {"base64": b64, "mimetype": mimetype}
    except Exception as exc:
        log.warning("get_media_from_cache id=%s: %s", evolution_message_id, exc)
    return None
