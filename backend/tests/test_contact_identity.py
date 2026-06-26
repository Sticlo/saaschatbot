from __future__ import annotations

from unittest.mock import patch

from app.application.sync.contact_identity_service import (
    build_contacts_index,
    resolve_contact_identity,
)
from app.application.sync.chat_sync_service import _consolidate_lid_duplicates, _merge_items


def test_extract_name_prefers_notify_from_whatsapp():
    from app.application.sync.contact_name_cache_service import extract_name_from_record

    assert extract_name_from_record({"id": "573208177650@s.whatsapp.net", "notify": "Katherin"}) == "Katherin"
    assert extract_name_from_record({"remoteJid": "573004583560@s.whatsapp.net", "pushName": "Julian"}) == "Julian"
    assert extract_name_from_record({"name": "Agenda", "pushName": "Perfil"}) == "Agenda"


def test_is_placeholder_contact_name_treats_contacto_as_placeholder():
    from app.shared.core.phone import is_placeholder_contact_name, resolve_display_name

    assert is_placeholder_contact_name("Contacto", "lid:123")
    assert is_placeholder_contact_name("contacto", "+573001234567")
    assert not is_placeholder_contact_name("Katherin", "+573208177650")
    assert resolve_display_name("", "lid:50985707815107", contact_jid="50985707815107@lid").startswith("···")


def test_resolve_contact_identity_prefers_agenda_name_over_push_name():
    item = {"remoteJid": "573001234567@s.whatsapp.net", "pushName": "Perfil WA"}
    contacts_index = build_contacts_index(
        [{"remoteJid": "573001234567@s.whatsapp.net", "name": "Mi Novia"}]
    )
    with patch("app.application.sync.contact_identity_service.fetch_stored_chat_name", return_value=None):
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


def test_merge_items_includes_agenda_contacts():
    merged = _merge_items(
        chats=[],
        contacts=[{"remoteJid": "573001234567@s.whatsapp.net", "name": "Ana"}],
        stored_chats=[],
        stored_contacts=[],
        message_index=[],
    )
    assert len(merged) == 1
    jid, item = merged[0]
    assert jid == "573001234567@s.whatsapp.net"
    assert item["name"] == "Ana"
    assert item.get("_from_agenda") is True


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


def test_contact_names_lookup_resolves_phone_from_lid_contact():
    from app.application.sync.contact_identity_service import ContactNamesLookup
    from app.domain.entities.conversation import Conversation

    lookup = ContactNamesLookup(
        jid_names={"123456789@lid": "Mi Novia"},
        phone_names={},
        lid_to_phone={"123456789@lid": "+573001234567"},
        phone_to_lid={"+573001234567": "123456789@lid", "573001234567": "123456789@lid"},
    )
    conv = Conversation(
        contact_phone="+573001234567",
        contact_name="+573001234567",
        contact_jid="",
    )
    assert lookup.resolve_for_conversation(conv) == "Mi Novia"


def test_contact_names_lookup_resolves_push_name_by_phone_jid():
    from app.application.sync.contact_identity_service import ContactNamesLookup
    from app.domain.entities.conversation import Conversation

    lookup = ContactNamesLookup(
        jid_names={"573001234567@s.whatsapp.net": "Juan Perfil"},
        phone_names={"+573001234567": "Juan Perfil"},
        lid_to_phone={},
        phone_to_lid={},
    )
    conv = Conversation(
        contact_phone="+573001234567",
        contact_name="+573001234567",
        contact_jid="",
    )
    assert lookup.resolve_for_conversation(conv) == "Juan Perfil"


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
