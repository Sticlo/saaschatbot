from __future__ import annotations

import mimetypes
import re
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

_ASSETS_ROOT = Path(__file__).resolve().parents[2] / "static" / "assets"
_MAX_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 10 * 1024 * 1024
PDF_MIME = "application/pdf"
_ALLOWED_MIME = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    PDF_MIME: ".pdf",
}


def assets_dir(tenant_id: uuid.UUID) -> Path:
    path = _ASSETS_ROOT / str(tenant_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _detect_mime(upload: UploadFile) -> str:
    mime = (upload.content_type or "").split(";")[0].strip().lower()
    if mime in _EXTENSIONS:
        return mime
    guessed, _ = mimetypes.guess_type(upload.filename or "")
    return (guessed or "").lower()


def _store(tenant_id: uuid.UUID, raw: bytes, mime: str) -> str:
    filename = f"{uuid.uuid4().hex}{_EXTENSIONS[mime]}"
    (assets_dir(tenant_id) / filename).write_bytes(raw)
    return f"assets/{tenant_id}/{filename}"


def save_tenant_image(tenant_id: uuid.UUID, upload: UploadFile) -> str:
    raw = upload.file.read()
    if len(raw) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="La imagen no puede superar 5 MB")
    mime = _detect_mime(upload)
    if mime not in _ALLOWED_MIME:
        raise HTTPException(status_code=400, detail="Solo se permiten imágenes JPG, PNG, WEBP o GIF")
    return _store(tenant_id, raw, mime)


_CHAT_IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


def read_chat_image(upload: UploadFile) -> tuple[bytes, str]:
    """Foto que el dueño pega o adjunta en un chat: se manda tal cual, no se guarda."""
    raw = upload.file.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="La foto no puede superar 5 MB")
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return raw, "image/webp"
    for signature, mime in _CHAT_IMAGE_SIGNATURES:
        if raw.startswith(signature):
            return raw, mime
    raise HTTPException(status_code=400, detail="Solo se pueden enviar fotos JPG, PNG o WEBP")


def display_file_name(raw_name: str, *, default: str = "catalogo.pdf") -> str:
    """Nombre que verá el cliente en WhatsApp, sin rutas ni caracteres raros."""
    name = Path(raw_name or "").name
    name = re.sub(r"[^\w .()\-áéíóúÁÉÍÓÚñÑ]", "", name).strip(" .")[:80]
    if not name:
        return default
    return name if name.lower().endswith(".pdf") else f"{name}.pdf"


def save_tenant_catalog_file(tenant_id: uuid.UUID, upload: UploadFile) -> tuple[str, str]:
    """Imagen o PDF para un atajo (menú, carta, catálogo). Devuelve (ruta, mime)."""
    raw = upload.file.read()
    mime = _detect_mime(upload)
    if mime == PDF_MIME:
        if len(raw) > MAX_PDF_BYTES:
            raise HTTPException(status_code=413, detail="El PDF no puede superar 10 MB")
        if not raw.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="El archivo no parece un PDF válido")
    elif mime in _ALLOWED_MIME:
        if len(raw) > _MAX_BYTES:
            raise HTTPException(status_code=413, detail="La imagen no puede superar 5 MB")
    else:
        raise HTTPException(status_code=400, detail="Sube una imagen (JPG, PNG, WEBP) o un PDF")
    return _store(tenant_id, raw, mime), mime


def resolve_asset_path(relative_path: str, *, tenant_id: uuid.UUID) -> Path:
    prefix = f"assets/{tenant_id}/"
    if not relative_path or not relative_path.startswith(prefix):
        raise ValueError("Ruta de imagen inválida")
    full = _ASSETS_ROOT / str(tenant_id) / Path(relative_path).name
    if not full.is_file():
        raise FileNotFoundError("Imagen no encontrada")
    return full


def read_asset_base64(relative_path: str, *, tenant_id: uuid.UUID) -> tuple[str, str]:
    path = resolve_asset_path(relative_path, tenant_id=tenant_id)
    mime, _ = mimetypes.guess_type(str(path))
    import base64

    return base64.b64encode(path.read_bytes()).decode("ascii"), mime or "image/jpeg"
