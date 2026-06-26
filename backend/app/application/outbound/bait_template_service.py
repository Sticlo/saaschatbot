from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.application.outbound.outbound_service import render_bait_message
from app.domain.entities import BaitTemplate, Tenant


def list_templates(db: Session, tenant_id: uuid.UUID) -> list[BaitTemplate]:
    return (
        db.query(BaitTemplate)
        .filter(BaitTemplate.tenant_id == tenant_id)
        .order_by(BaitTemplate.is_default.desc(), BaitTemplate.updated_at.desc())
        .all()
    )


def get_template(db: Session, tenant_id: uuid.UUID, template_id: uuid.UUID) -> Optional[BaitTemplate]:
    return (
        db.query(BaitTemplate)
        .filter(BaitTemplate.tenant_id == tenant_id, BaitTemplate.id == template_id)
        .first()
    )


def _clear_default(db: Session, tenant_id: uuid.UUID) -> None:
    db.query(BaitTemplate).filter(
        BaitTemplate.tenant_id == tenant_id,
        BaitTemplate.is_default.is_(True),
    ).update({BaitTemplate.is_default: False})


def _normalize_buttons(buttons: Optional[list[dict]]) -> Optional[list[dict]]:
    if not buttons:
        return None
    cleaned: list[dict] = []
    for idx, btn in enumerate(buttons[:3]):
        label = str(btn.get("label") or btn.get("displayText") or "").strip()
        if not label:
            continue
        value = str(btn.get("value") or btn.get("id") or label).strip()
        cleaned.append({"type": "reply", "label": label[:25], "value": value[:120]})
    return cleaned or None


def create_template(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    name: str,
    text: str,
    image_path: Optional[str] = None,
    buttons: Optional[list[dict]] = None,
    button_title: Optional[str] = None,
    button_footer: Optional[str] = None,
    is_default: bool = False,
) -> BaitTemplate:
    if is_default:
        _clear_default(db, tenant_id)
    row = BaitTemplate(
        tenant_id=tenant_id,
        name=name.strip()[:120],
        text=text.strip(),
        image_path=image_path,
        buttons=_normalize_buttons(buttons),
        button_title=(button_title or "").strip()[:120] or None,
        button_footer=(button_footer or "").strip()[:255] or None,
        is_default=is_default,
    )
    db.add(row)
    db.flush()
    return row


def update_template(
    db: Session,
    row: BaitTemplate,
    *,
    name: Optional[str] = None,
    text: Optional[str] = None,
    image_path: Optional[str] = None,
    clear_image: bool = False,
    buttons: Optional[list[dict]] = None,
    button_title: Optional[str] = None,
    button_footer: Optional[str] = None,
    is_default: Optional[bool] = None,
) -> BaitTemplate:
    if name is not None:
        row.name = name.strip()[:120]
    if text is not None:
        row.text = text.strip()
    if clear_image:
        row.image_path = None
    elif image_path is not None:
        row.image_path = image_path
    if buttons is not None:
        row.buttons = _normalize_buttons(buttons)
    if button_title is not None:
        row.button_title = button_title.strip()[:120] or None
    if button_footer is not None:
        row.button_footer = button_footer.strip()[:255] or None
    if is_default is True:
        _clear_default(db, row.tenant_id)
        row.is_default = True
    elif is_default is False:
        row.is_default = False
    db.flush()
    return row


def delete_template(db: Session, row: BaitTemplate) -> None:
    db.delete(row)
    db.flush()


def template_to_extras(row: BaitTemplate) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    if row.image_path:
        extras["image_path"] = row.image_path
    if row.buttons:
        extras["buttons"] = row.buttons
    if row.button_title:
        extras["button_title"] = row.button_title
    if row.button_footer:
        extras["button_footer"] = row.button_footer
    return extras


def preview_template(
    row: BaitTemplate,
    *,
    tenant: Tenant,
    contact_name: str = "María",
) -> dict[str, Any]:
    return {
        "text": render_bait_message(
            row.text,
            contact_name=contact_name,
            business_name=tenant.business_name,
        ),
        "image_path": row.image_path,
        "buttons": row.buttons or [],
        "button_title": row.button_title,
        "button_footer": row.button_footer,
    }
