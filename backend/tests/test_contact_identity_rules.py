from app.shared.core.phone import pair_lid_phone_from_message_key


def test_pair_lid_phone_requires_both_in_same_key():
    key = {
        "remoteJid": "131735270514761@lid",
        "remoteJidAlt": "573208725610@s.whatsapp.net",
    }
    lid, phone = pair_lid_phone_from_message_key(key)
    assert lid == "131735270514761@lid"
    assert phone == "+573208725610"


def test_pair_lid_phone_rejects_phone_only_key():
    key = {"remoteJid": "573004678377@s.whatsapp.net"}
    assert pair_lid_phone_from_message_key(key) == ("", "")


def test_resolve_identity_prefers_phone_agenda_over_lid_push_name():
    from unittest.mock import patch

    from app.application.sync.contact_identity_service import (
        build_contacts_index,
        resolve_contact_identity,
    )

    item = {
        "remoteJid": "131735270514761@lid",
        "pushName": "Diana 🐶🐱",
        "lastMessage": {
            "key": {
                "remoteJid": "131735270514761@lid",
                "remoteJidAlt": "573208725610@s.whatsapp.net",
            },
            "pushName": "Diana 🐶🐱",
        },
    }
    contacts_index = build_contacts_index(
        [
            {
                "remoteJid": "573208725610@s.whatsapp.net",
                "name": "Ana",
            }
        ]
    )
    with patch("app.application.sync.contact_identity_service.fetch_stored_chat_name", return_value=None):
        phone, contact_jid, name, _ = resolve_contact_identity(
            "131735270514761@lid",
            item,
            contacts_index=contacts_index,
            instance_name="t_test",
            dsn="",
            key=item["lastMessage"]["key"],
            lid_jid="131735270514761@lid",
        )
    assert phone == "+573208725610"
    assert contact_jid == "131735270514761@lid"
    assert name == "Ana"
