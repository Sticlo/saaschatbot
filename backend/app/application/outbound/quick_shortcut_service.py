from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.domain.entities import TenantProfile

_MAX_SHORTCUTS = 12


def _normalize(raw: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in raw[:_MAX_SHORTCUTS]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        kind = str(item.get("type") or "text").strip().lower()
        if kind not in ("text", "image"):
            kind = "text"
        sid = str(item.get("id") or uuid.uuid4())
        if kind == "image":
            path = str(item.get("image_path") or "").strip() or None
            if not path:
                continue
            out.append({"id": sid, "label": label[:20], "type": "image", "image_path": path})
        else:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            out.append({"id": sid, "label": label[:20], "type": "text", "text": text[:2000]})
    return out


def get_shortcuts(db: Session, tenant_id: uuid.UUID) -> list[dict]:
    profile = db.query(TenantProfile).filter(TenantProfile.tenant_id == tenant_id).first()
    if profile is None or not profile.quick_shortcuts:
        return []
    if isinstance(profile.quick_shortcuts, list):
        return profile.quick_shortcuts
    return []


def save_shortcuts(db: Session, tenant_id: uuid.UUID, shortcuts: list[dict]) -> list[dict]:
    profile = db.query(TenantProfile).filter(TenantProfile.tenant_id == tenant_id).first()
    if profile is None:
        raise ValueError("Perfil no encontrado")
    cleaned = _normalize(shortcuts)
    profile.quick_shortcuts = cleaned
    db.flush()
    return cleaned


def find_shortcut(db: Session, tenant_id: uuid.UUID, shortcut_id: str) -> Optional[dict]:
    for item in get_shortcuts(db, tenant_id):
        if str(item.get("id")) == str(shortcut_id):
            return item
    return None
