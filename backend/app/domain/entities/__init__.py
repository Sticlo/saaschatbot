from __future__ import annotations

from app.domain.entities.ai_reply_event import AiReplyEvent
from app.domain.entities.appointment import Appointment
from app.domain.entities.audit_log import AuditLog
from app.domain.entities.conversation import Conversation, Message
from app.domain.entities.enums import (
    CampaignStatus,
    ConversationMode,
    ConversationStatus,
    LeadStatus,
    MessageDirection,
    MessageSource,
    MessageStatus,
    SendQueueStatus,
    SubscriptionStatus,
    TenantPlan,
    UserRole,
    WhatsAppStatus,
)
from app.domain.entities.bait_template import BaitTemplate
from app.domain.entities.legal_consent import LegalConsent
from app.domain.entities.outbound import Campaign, Exclusion, Lead, SendQueueItem
from app.domain.entities.plan import DEFAULT_PLAN_ID, Plan
from app.domain.entities.staff import StaffMember
from app.domain.entities.subscription import Subscription
from app.domain.entities.tenant import Tenant, TenantProfile
from app.domain.entities.user import User
from app.domain.entities.whatsapp_contact_link import WhatsAppContactLink
from app.domain.entities.whatsapp_session import WhatsAppSession

__all__ = [
    "AiReplyEvent",
    "Appointment",
    "AuditLog",
    "BaitTemplate",
    "Campaign",
    "Exclusion",
    "Lead",
    "SendQueueItem",
    "Conversation",
    "CampaignStatus",
    "ConversationMode",
    "ConversationStatus",
    "DEFAULT_PLAN_ID",
    "LeadStatus",
    "LegalConsent",
    "Message",
    "MessageDirection",
    "MessageSource",
    "MessageStatus",
    "Plan",
    "SendQueueStatus",
    "StaffMember",
    "Subscription",
    "SubscriptionStatus",
    "Tenant",
    "TenantPlan",
    "TenantProfile",
    "User",
    "UserRole",
    "WhatsAppSession",
    "WhatsAppContactLink",
    "WhatsAppStatus",
]
