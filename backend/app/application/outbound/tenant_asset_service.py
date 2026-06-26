from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

_ASSETS_ROOT = Path(__file__).resolve().parents[2] / "static" / "assets"
_MAX_BYTES = 5 * 1024 * 1024
_ALLOWED_MIME = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})


def assets_dir(tenant_id: uuid.UUID) -> Path:
    path = _ASSETS_ROOT / str(tenant_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_tenant_image(tenant_id: uuid.UUID, upload: UploadFile) -> str:
    raw = upload.file.read()
    if len(raw) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="La imagen no puede superar 5 MB")

    mime = (upload.content_type or "").split(";")[0].strip().lower()
    if mime not in _ALLOWED_MIME:
        guessed, _ = mimetypes.guess_type(upload.filename or "")
        mime = (guessed or "").lower()
    if mime not in _ALLOWED_MIME:
        raise HTTPException(status_code=400, detail="Solo se permiten imágenes JPG, PNG, WEBP o GIF")

    ext = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }[mime]
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = assets_dir(tenant_id) / filename
    dest.write_bytes(raw)
    return f"assets/{tenant_id}/{filename}"


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
