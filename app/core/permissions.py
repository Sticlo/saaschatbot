from __future__ import annotations

from app.models.enums import UserRole

ROLE_HIERARCHY = {
    UserRole.VIEWER: 1,
    UserRole.AGENT: 2,
    UserRole.OWNER: 3,
}


def role_at_least(user_role: str, minimum: UserRole) -> bool:
    try:
        current = UserRole(user_role)
    except ValueError:
        return False
    return ROLE_HIERARCHY[current] >= ROLE_HIERARCHY[minimum]


def can_manage_users(user_role: str) -> bool:
    return role_at_least(user_role, UserRole.OWNER)


def can_edit_tenant_settings(user_role: str) -> bool:
    return role_at_least(user_role, UserRole.OWNER)


def can_respond_messages(user_role: str) -> bool:
    return role_at_least(user_role, UserRole.AGENT)


def can_view_dashboard(user_role: str) -> bool:
    return role_at_least(user_role, UserRole.VIEWER)
