from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.shared.core.phone import is_valid_whatsapp_phone, normalize_phone
from app.domain.entities import (
    Campaign,
    Conversation,
    Lead,
    SendQueueItem,
    Tenant,
    TenantProfile,
    WhatsAppSession,
)
from app.domain.entities.enums import (
    CampaignStatus,
    LeadStatus,
    MessageSource,
    SendQueueStatus,
)
from app.application.outbound.bait_limit_service import check_bait_quota, record_bait_sent
from app.infrastructure.evolution.evolution_client import EvolutionAPIError
from app.application.messaging.message_service import get_or_create_conversation
from app.application.outbound.outbound_dedup_service import (
    mark_phone_contacted,
    register_phone,
    should_skip_outbound,
)
from app.application.realtime.realtime_service import publish_conversation_updated, publish_panel_event
from app.application.whatsapp.whatsapp_service import send_bait_message
from app.application.whatsapp.whatsapp_status import can_send_whatsapp

log = logging.getLogger(__name__)

DEFAULT_TEMPLATE = settings.default_bait_template


def render_bait_message(template: str, *, contact_name: str = "", business_name: str = "") -> str:
    name_part = ""
    if contact_name and not contact_name.startswith("+"):
        name_part = f" {contact_name.split()[0]}"
    text = template.replace("{name_part}", name_part).replace("{name}", contact_name or "")
    text = text.replace("{business}", business_name or "")
    return text.strip()


def get_bait_template(db: Session, tenant: Tenant, campaign: Optional[Campaign] = None) -> str:
    if campaign and campaign.message_template:
        return campaign.message_template.strip()
    profile = (
        db.query(TenantProfile)
        .filter(TenantProfile.tenant_id == tenant.id)
        .first()
    )
    if profile and profile.bait_message_template:
        return profile.bait_message_template.strip()
    return DEFAULT_TEMPLATE


def import_leads(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    items: list[dict],
    source: str = "manual",
) -> dict:
    """Importa leads con dedup por teléfono. items: [{phone, name?}]"""
    from app.application.outbound.outbound_dedup_service import ensure_dedup_sets_loaded, is_phone_known

    ensure_dedup_sets_loaded(db, tenant_id)
    added = skipped = invalid = 0

    for item in items:
        raw_phone = str(item.get("phone") or item.get("phone_e164") or "").strip()
        phone = normalize_phone(raw_phone)
        if not phone or not is_valid_whatsapp_phone(phone):
            invalid += 1
            continue

        if is_phone_known(tenant_id, phone):
            skipped += 1
            continue

        existing = (
            db.query(Lead)
            .filter(Lead.tenant_id == tenant_id, Lead.phone_e164 == phone)
            .first()
        )
        if existing:
            register_phone(tenant_id, phone)
            skipped += 1
            continue

        lead = Lead(
            tenant_id=tenant_id,
            phone_e164=phone,
            name=str(item.get("name") or "").strip(),
            source=source,
            status=LeadStatus.PENDING.value,
        )
        db.add(lead)
        register_phone(tenant_id, phone)
        added += 1

    db.flush()
    return {"added": added, "skipped": skipped, "invalid": invalid}


def _compute_scheduled_at(base: datetime, index: int) -> datetime:
    """Espacia envíos con delay aleatorio entre min y max segundos."""
    delay = random.randint(settings.bait_delay_min_seconds, settings.bait_delay_max_seconds)
    # Cada item adicional suma otro delay para no disparar en ráfaga
    total_seconds = delay * (index + 1)
    return base + timedelta(seconds=total_seconds)


def enqueue_campaign(
    db: Session,
    *,
    tenant: Tenant,
    lead_ids: Optional[list[uuid.UUID]] = None,
    limit: Optional[int] = None,
    campaign_name: str = "Campaña",
    message_template: Optional[str] = None,
    bait_template_id: Optional[uuid.UUID] = None,
) -> dict:
    """Encola carnadas para leads pending. Retorna stats o error."""
    ok, reason, _ = check_bait_quota(db, tenant, count=1)
    if not ok:
        return {"ok": False, "error": reason}

    query = db.query(Lead).filter(
        Lead.tenant_id == tenant.id,
        Lead.status == LeadStatus.PENDING.value,
    )
    if lead_ids:
        query = query.filter(Lead.id.in_(lead_ids))

    leads = query.order_by(Lead.created_at.asc()).all()
    if limit is not None:
        leads = leads[:limit]

    if not leads:
        return {"ok": False, "error": "No hay leads pendientes para encolar"}

    # Respetar cuota disponible
    ok, reason, meta = check_bait_quota(db, tenant, count=len(leads))
    if not ok:
        # Encolar solo lo que cabe en la cuota
        if meta.get("is_trial"):
            cap = meta.get("trial_remaining", 0)
        elif meta.get("daily_remaining") is not None:
            cap = meta.get("daily_remaining", 0)
        else:
            return {"ok": False, "error": reason}
        if cap <= 0:
            return {"ok": False, "error": reason}
        leads = leads[:cap]

    from app.application.outbound.bait_template_service import get_template, template_to_extras

    bait_template = None
    message_extras: dict = {}
    if bait_template_id:
        bait_template = get_template(db, tenant.id, bait_template_id)
        if bait_template is None:
            return {"ok": False, "error": "Plantilla de carnada no encontrada"}
        message_extras = template_to_extras(bait_template)

    campaign = Campaign(
        tenant_id=tenant.id,
        name=campaign_name,
        status=CampaignStatus.RUNNING.value,
        message_template=message_template,
        bait_template_id=bait_template.id if bait_template else None,
        total_queued=len(leads),
        started_at=datetime.now(timezone.utc),
    )
    db.add(campaign)
    db.flush()

    if bait_template and not message_template:
        template = bait_template.text.strip()
    else:
        template = get_bait_template(db, tenant, campaign)
    now = datetime.now(timezone.utc)
    queued = 0

    for idx, lead in enumerate(leads):
        skip = should_skip_outbound(db, tenant.id, lead.phone_e164)
        if skip:
            lead.status = LeadStatus.EXCLUDED.value if skip == "excluded" else LeadStatus.CONTACTED.value
            continue

        body = render_bait_message(
            template,
            contact_name=lead.name,
            business_name=tenant.business_name,
        )
        item = SendQueueItem(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            lead_id=lead.id,
            phone_e164=lead.phone_e164,
            contact_name=lead.name,
            message_body=body,
            message_extras=message_extras or None,
            status=SendQueueStatus.PENDING.value,
            scheduled_at=_compute_scheduled_at(now, idx),
        )
        db.add(item)
        lead.status = LeadStatus.QUEUED.value
        queued += 1

    campaign.total_queued = queued
    if queued == 0:
        campaign.status = CampaignStatus.COMPLETED.value
        campaign.completed_at = datetime.now(timezone.utc)

    db.flush()
    publish_panel_event(
        tenant.id,
        {"type": "outbound.queued", "campaign_id": str(campaign.id), "queued": queued},
    )
    return {
        "ok": True,
        "campaign_id": str(campaign.id),
        "queued": queued,
        "skipped": len(leads) - queued,
    }


def process_send_queue_item(db: Session, item: SendQueueItem) -> bool:
    """
    Procesa un item de la cola. Retorna True si terminó (sent/skipped/failed).
    """
    tenant = db.query(Tenant).filter(Tenant.id == item.tenant_id).first()
    session = (
        db.query(WhatsAppSession)
        .filter(WhatsAppSession.tenant_id == item.tenant_id)
        .first()
    )
    if tenant is None or session is None:
        _fail_item(item, "Tenant o sesión WhatsApp no encontrada")
        return True

    wa_ok, wa_reason = can_send_whatsapp(tenant.whatsapp_status)
    if not wa_ok:
        _fail_item(item, wa_reason)
        return True

    if session.active_connection_id is None:
        _fail_item(item, "WhatsApp no vinculado")
        return True

    # Pausa por tenant
    from app.application.outbound.bait_scheduler import is_outbound_paused

    if is_outbound_paused(item.tenant_id):
        return False  # reintentar después

    ok, reason, _ = check_bait_quota(db, tenant, count=1)
    if not ok:
        _skip_item(item, reason)
        _update_campaign(db, item.campaign_id, skipped=True)
        return True

    skip = should_skip_outbound(db, tenant.id, item.phone_e164)
    if skip:
        _skip_item(item, skip)
        _update_lead(db, item.lead_id, LeadStatus.CONTACTED.value if skip != "excluded" else LeadStatus.EXCLUDED.value)
        _update_campaign(db, item.campaign_id, skipped=True)
        return True

    item.status = SendQueueStatus.PROCESSING.value
    db.flush()

    conversation = get_or_create_conversation(
        db,
        tenant_id=tenant.id,
        contact_phone=item.phone_e164,
        contact_name=item.contact_name or item.phone_e164,
        whatsapp_connection_id=session.active_connection_id,
    )

    try:
        message = send_bait_message(
            db,
            tenant=tenant,
            session=session,
            conversation=conversation,
            text=item.message_body,
            source=MessageSource.BAIT.value,
            extras=item.message_extras,
        )
        conversation.bait_sent = True
        conversation.ai_active = False
        record_bait_sent(db, tenant)
        mark_phone_contacted(tenant.id, item.phone_e164)
        _update_lead(db, item.lead_id, LeadStatus.CONTACTED.value)

        item.status = SendQueueStatus.SENT.value
        item.sent_at = datetime.now(timezone.utc)
        _update_campaign(db, item.campaign_id, sent=True)
        publish_conversation_updated(tenant.id, conversation)
        publish_panel_event(
            tenant.id,
            {
                "type": "outbound.sent",
                "conversation_id": str(conversation.id),
                "message_id": str(message.id),
                "phone": item.phone_e164,
            },
        )
        db.flush()
        return True
    except EvolutionAPIError as exc:
        _fail_item(item, str(exc))
        _update_lead(db, item.lead_id, LeadStatus.FAILED.value)
        _update_campaign(db, item.campaign_id, failed=True)
        db.flush()
        return True


def _skip_item(item: SendQueueItem, reason: str) -> None:
    item.status = SendQueueStatus.SKIPPED.value
    item.skip_reason = reason[:255]
    item.sent_at = datetime.now(timezone.utc)


def _fail_item(item: SendQueueItem, error: str) -> None:
    item.status = SendQueueStatus.FAILED.value
    item.error_message = error[:2000]
    item.sent_at = datetime.now(timezone.utc)


def _update_lead(db: Session, lead_id: Optional[uuid.UUID], status: str) -> None:
    if not lead_id:
        return
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if lead:
        lead.status = status


def _update_campaign(
    db: Session,
    campaign_id: Optional[uuid.UUID],
    *,
    sent: bool = False,
    skipped: bool = False,
    failed: bool = False,
) -> None:
    if not campaign_id:
        return
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        return
    if sent:
        campaign.total_sent += 1
    if skipped:
        campaign.total_skipped += 1
    if failed:
        campaign.total_failed += 1

    pending = (
        db.query(SendQueueItem)
        .filter(
            SendQueueItem.campaign_id == campaign_id,
            SendQueueItem.status.in_(
                [SendQueueStatus.PENDING.value, SendQueueStatus.PROCESSING.value]
            ),
        )
        .count()
    )
    if pending == 0 and campaign.status == CampaignStatus.RUNNING.value:
        campaign.status = CampaignStatus.COMPLETED.value
        campaign.completed_at = datetime.now(timezone.utc)


def get_queue_stats(db: Session, tenant_id: uuid.UUID) -> dict:
    pending = (
        db.query(SendQueueItem)
        .filter(
            SendQueueItem.tenant_id == tenant_id,
            SendQueueItem.status == SendQueueStatus.PENDING.value,
        )
        .count()
    )
    processing = (
        db.query(SendQueueItem)
        .filter(
            SendQueueItem.tenant_id == tenant_id,
            SendQueueItem.status == SendQueueStatus.PROCESSING.value,
        )
        .count()
    )
    leads_pending = (
        db.query(Lead)
        .filter(Lead.tenant_id == tenant_id, Lead.status == LeadStatus.PENDING.value)
        .count()
    )
    return {
        "queue_pending": pending,
        "queue_processing": processing,
        "leads_pending": leads_pending,
    }
