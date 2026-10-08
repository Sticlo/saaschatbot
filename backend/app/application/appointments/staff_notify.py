from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app.application.appointments.appointment_service import describe_appointment
from app.domain.entities import Appointment, Tenant
from app.shared.core.phone import format_display_phone, is_valid_whatsapp_phone, phone_to_evolution_number

log = logging.getLogger(__name__)

_HEADLINES = {
    "new": "📅 Nueva cita para ti",
    "moved": "🔁 Te movieron una cita",
    "cancelled": "❌ Se canceló tu cita",
}


@dataclass(frozen=True)
class StaffNotice:
    instance_name: str
    phone: str
    text: str


def build_staff_notice(
    tenant: Tenant,
    row: Appointment,
    *,
    kind: str = "new",
) -> Optional[StaffNotice]:
    """Aviso por WhatsApp a quien atiende la cita. None si no tiene número o no hay WhatsApp."""
    staff = row.staff
    session = tenant.whatsapp_session
    if staff is None or not staff.phone or session is None or not session.instance_name:
        return None
    client = (row.client_name or "").strip() or "Cliente"
    phone = row.client_phone or ""
    if is_valid_whatsapp_phone(phone):
        client += f" · {format_display_phone(phone)}"
    lines = [
        f"{_HEADLINES.get(kind, _HEADLINES['new'])} en *{tenant.business_name}*",
        f"{describe_appointment(row, with_staff=False)}",
        f"Cliente: {client}",
    ]
    if row.notes and kind != "cancelled":
        lines.append(f"Notas: {row.notes[:300]}")
    return StaffNotice(instance_name=session.instance_name, phone=staff.phone, text="\n".join(lines))


def send_staff_notice(notice: Optional[StaffNotice]) -> bool:
    if notice is None:
        return False
    from app.application.whatsapp.whatsapp_gateway import send_text

    try:
        send_text(notice.instance_name, phone_to_evolution_number(notice.phone), notice.text)
        return True
    except Exception:
        log.warning("No se pudo avisar al empleado %s", notice.phone[-4:], exc_info=True)
        return False
