#!/usr/bin/env python3
"""
Debug específico para los contactos sin nombre.
Corre: .venv/bin/python scripts/debug_missing.py 2>&1
"""
import sys, os, uuid
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend"))

from app.config import settings
from app.infrastructure.persistence.database import SessionLocal
from app.domain.entities import Tenant, WhatsAppSession, Conversation
from app.shared.core.phone import is_placeholder_contact_name

TENANT_ID = uuid.UUID("c728cc56-241e-4c3a-9cb0-bad181948cdb")

db = SessionLocal()
tenant = db.query(Tenant).filter(Tenant.id == TENANT_ID).first()
session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == TENANT_ID).first()

print(f"Instance: {session.instance_name}\n")

# Buscar todos los placeholders
placeholders = [
    c for c in db.query(Conversation).filter(
        Conversation.tenant_id == TENANT_ID,
        Conversation.whatsapp_connection_id == session.active_connection_id,
    ).all()
    if is_placeholder_contact_name(c.contact_name or "", c.contact_phone or "")
]

print(f"Total conversaciones sin nombre: {len(placeholders)}\n")

if not settings.evolution_database_url:
    print("No hay evolution_database_url configurado")
    sys.exit(1)

import psycopg
from app.shared.core.phone import normalize_phone

dsn = settings.evolution_database_url

with psycopg.connect(dsn, connect_timeout=5) as conn:
    with conn.cursor() as cur:

        print("="*60)
        print("VERIFICANDO EN EVOLUTION para cada placeholder:")
        print("="*60)

        for c in placeholders[:20]:
            phone = c.contact_phone or ""
            # JID formato s.whatsapp.net
            import re
            digits = re.sub(r"\D", "", phone)
            jid_phone = f"{digits}@s.whatsapp.net"

            # 1. ¿Está en Contact table?
            cur.execute("""
                SELECT c."remoteJid", c."pushName"
                FROM "Contact" c
                JOIN "Instance" i ON i.id = c."instanceId"
                WHERE i.name = %s AND (
                    c."remoteJid" = %s OR c."remoteJid" LIKE %s
                )
                LIMIT 3
            """, (session.instance_name, jid_phone, f"%{digits}%"))
            contacts = cur.fetchall()

            # 2. ¿Tiene mensajes en Message table?
            cur.execute("""
                SELECT m.key->>'remoteJid', m."pushName", m.key->>'fromMe'
                FROM "Message" m
                JOIN "Instance" i ON i.id = m."instanceId"
                WHERE i.name = %s AND (
                    m.key->>'remoteJid' = %s
                    OR m.key->>'remoteJid' LIKE %s
                    OR m.key->>'remoteJidAlt' = %s
                )
                AND m."pushName" IS NOT NULL
                LIMIT 3
            """, (session.instance_name, jid_phone, f"%{digits}%", jid_phone))
            msgs = cur.fetchall()

            print(f"\nphone={phone}")
            print(f"  JID buscado: {jid_phone}")
            print(f"  En Contact:  {contacts if contacts else 'NO ENCONTRADO'}")
            print(f"  En Messages: {msgs if msgs else 'NO ENCONTRADO'}")

        print("\n" + "="*60)
        print("MUESTRA de contactos @lid en Contact table (primeros 5):")
        cur.execute("""
            SELECT c."remoteJid", c."pushName"
            FROM "Contact" c
            JOIN "Instance" i ON i.id = c."instanceId"
            WHERE i.name = %s AND c."remoteJid" LIKE '%@lid'
            LIMIT 5
        """, (session.instance_name,))
        for row in cur.fetchall():
            print(f"  {row}")

        print("\n¿Hay contacts con @s.whatsapp.net?")
        cur.execute("""
            SELECT COUNT(*) FROM "Contact" c
            JOIN "Instance" i ON i.id = c."instanceId"
            WHERE i.name = %s AND c."remoteJid" LIKE '%@s.whatsapp.net'
        """, (session.instance_name,))
        count = cur.fetchone()[0]
        print(f"  Contacts con @s.whatsapp.net: {count}")

        print("\n¿Hay mensajes en Message table?")
        cur.execute("""
            SELECT COUNT(*) FROM "Message" m
            JOIN "Instance" i ON i.id = m."instanceId"
            WHERE i.name = %s
        """, (session.instance_name,))
        count = cur.fetchone()[0]
        print(f"  Total mensajes en Evolution DB: {count}")

db.close()
