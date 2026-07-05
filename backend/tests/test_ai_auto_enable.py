from __future__ import annotations

from app.application.ai.ai_auto_enable_service import maybe_auto_enable_ai_for_inbound
from app.domain.entities import Conversation, Tenant


def _tenant(**kwargs) -> Tenant:
    t = Tenant(business_name="Barbería", slug="x")
    for k, v in kwargs.items():
        setattr(t, k, v)
    return t


def _conv(**kwargs) -> Conversation:
    c = Conversation(
        tenant_id=None,
        contact_phone="+573001112233",
        whatsapp_connection_id=None,
    )
    for k, v in kwargs.items():
        setattr(c, k, v)
    return c


def test_legacy_friend_never_auto_enables():
    tenant = _tenant(ai_global_enabled=True)
    conv = _conv(imported_legacy=True, bait_sent=False, ai_active=False)
    assert maybe_auto_enable_ai_for_inbound(tenant=tenant, conversation=conv, body="hola parce") is False
    assert conv.ai_active is False


def test_bait_prospect_auto_enables():
    tenant = _tenant(ai_global_enabled=True)
    conv = _conv(imported_legacy=True, bait_sent=True, ai_active=False)
    assert maybe_auto_enable_ai_for_inbound(tenant=tenant, conversation=conv, body="hola") is True
    assert conv.ai_active is True


def test_new_number_business_question_auto_enables():
    tenant = _tenant(ai_global_enabled=True)
    conv = _conv(imported_legacy=False, bait_sent=False, ai_active=False)
    assert (
        maybe_auto_enable_ai_for_inbound(
            tenant=tenant,
            conversation=conv,
            body="¿Cuánto cuesta el corte?",
        )
        is True
    )
    assert conv.ai_active is True


def test_new_number_casual_greeting_stays_off():
    tenant = _tenant(ai_global_enabled=True)
    conv = _conv(imported_legacy=False, bait_sent=False, ai_active=False)
    assert maybe_auto_enable_ai_for_inbound(tenant=tenant, conversation=conv, body="ok") is False
    assert conv.ai_active is False
