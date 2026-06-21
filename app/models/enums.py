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
