from __future__ import annotations

from unittest.mock import patch

from app.services.contact_identity_service import (
    build_contacts_index,
    resolve_contact_identity,
)
from app.services.chat_sync_service import _consolidate_lid_duplicates, _merge_items


def test_resolve_contact_identity_prefers_agenda_name_over_push_name():
    item = {"remoteJid": "573001234567@s.whatsapp.net", "pushName": "Perfil WA"}
    contacts_index = build_contacts_index(
        [{"remoteJid": "573001234567@s.whatsapp.net", "name": "Mi Novia"}]
    )
    with patch("app.services.contact_identity_service.fetch_stored_chat_name", return_value=None):
        phone, contact_jid, name, archived = resolve_contact_identity(
            "573001234567@s.whatsapp.net",
            item,
            contacts_index=contacts_index,
            instance_name="t_test",
            dsn="",
        )
    assert phone == "+573001234567"
    assert name == "Mi Novia"
    assert contact_jid == ""
    assert archived is None


def test_resolve_contact_identity_does_not_default_archived_false():
    identity = resolve_contact_identity(
        "573001234567@s.whatsapp.net",
        {"remoteJid": "573001234567@s.whatsapp.net", "name": "Ana"},
        contacts_index={},
        instance_name="t_test",
        dsn="",
    )
    assert identity is not None
    assert identity[3] is None


def test_merge_items_combines_name_and_timestamp():
    merged = _merge_items(
        chats=[{"remoteJid": "573001234567@s.whatsapp.net", "pushName": "Perfil"}],
        contacts=[],
        stored_chats=[{"remoteJid": "573001234567@s.whatsapp.net", "name": "Agenda"}],
        stored_contacts=[],
        message_index=[
            {
                "remoteJid": "573001234567@s.whatsapp.net",
                "lastMessageTimestamp": 1700000000,
            }
        ],
    )
    assert len(merged) == 1
    jid, item = merged[0]
    assert jid == "573001234567@s.whatsapp.net"
    assert item["name"] == "Agenda"
    assert item["pushName"] == "Perfil"
    assert item["lastMessageTimestamp"] == 1700000000


def test_consolidate_lid_duplicates_merges_into_phone_jid():
    items = [
        ("123456789@lid", {"remoteJid": "123456789@lid", "name": "Mi Novia"}),
        (
            "573001234567@s.whatsapp.net",
            {"remoteJid": "573001234567@s.whatsapp.net", "pushName": "Perfil"},
        ),
    ]
    result = _consolidate_lid_duplicates(
        items,
        {"123456789@lid": "+573001234567"},
        {"123456789@lid": "Mi Novia"},
    )
    jids = {jid for jid, _ in result}
    assert "123456789@lid" not in jids
    assert "573001234567@s.whatsapp.net" in jids
    phone_item = next(item for jid, item in result if jid.endswith("@s.whatsapp.net"))
    assert phone_item.get("name") == "Mi Novia"
    assert phone_item.get("_lid_jid") == "123456789@lid"
