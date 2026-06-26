from __future__ import annotations

import enum


class UserRole(str, enum.Enum):
    OWNER = "owner"
    AGENT = "agent"
    VIEWER = "viewer"


class TenantPlan(str, enum.Enum):
    TRIAL = "trial"
    PAID = "paid"
    SUSPENDED = "suspended"


class WhatsAppStatus(str, enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RESTRICTED = "restricted"
    BANNED = "banned"


class SubscriptionStatus(str, enum.Enum):
    TRIAL = "trial"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"


class ConversationMode(str, enum.Enum):
    AUTO = "auto"
    MANUAL = "manual"


class ConversationStatus(str, enum.Enum):
    ACTIVE = "active"
    EXCLUDED = "excluded"
    CLOSED = "closed"


class ConversationInterest(str, enum.Enum):
    INTERESTED = "interested"
    NOT_INTERESTED = "not_interested"


class MessageDirection(str, enum.Enum):
    IN = "in"
    OUT = "out"


class MessageSource(str, enum.Enum):
    CONTACT = "contact"
    AGENT = "agent"
    BOT = "bot"
    SYSTEM = "system"
    BAIT = "bait"


class MessageStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    RECEIVED = "received"
    FAILED = "failed"


class LeadStatus(str, enum.Enum):
    PENDING = "pending"
    QUEUED = "queued"
    CONTACTED = "contacted"
    EXCLUDED = "excluded"
    FAILED = "failed"


class CampaignStatus(str, enum.Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"


class SendQueueStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"
