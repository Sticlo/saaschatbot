#!/usr/bin/env python3
"""
Script de diagnóstico para nombres de contactos.
Correr con: .venv/bin/python scripts/debug_names.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend"))

from app.config import settings
from app.infrastructure.persistence.database import SessionLocal
from app.domain.entities import Tenant, WhatsAppSession, Conversation

TENANT_ID = None  # Se auto-detecta

db = SessionLocal()

# Mostrar todos los tenants disponibles
all_tenants = db.query(Tenant).all()
print("\nTenants disponibles:")
for t in all_tenants:
    s = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == t.id).first()
    phone = s.phone_number if s else "sin sesión"
    print(f"  [{t.id}] {t.business_name} | status={t.whatsapp_status} | phone={phone}")

# Usar el tenant con conversaciones reales (el que tiene más conversaciones)
import uuid as _uuid
TARGET_ID = _uuid.UUID("c728cc56-241e-4c3a-9cb0-bad181948cdb")
tenant = next((t for t in all_tenants if t.id == TARGET_ID), None)
if tenant is None:
    tenant = next((t for t in all_tenants if t.whatsapp_status == "connected"), all_tenants[0])
print(f"\nUsando tenant: {tenant.business_name} ({tenant.id})\n")

session = db.query(WhatsAppSession).filter(WhatsAppSession.tenant_id == tenant.id).first()
if session is None:
    print("ERROR: No hay sesión WhatsApp para este tenant.")
    db.close()
    sys.exit(1)

print(f"\n{'='*60}")
print(f"TENANT: {tenant.business_name} | STATUS: {tenant.whatsapp_status}")
print(f"PHONE: {session.phone_number} | JID: {session.bound_owner_jid}")
print(f"INSTANCE: {session.instance_name}")
print(f"{'='*60}\n")

# ── 1. Contactos en Evolution DB ───────────────────────────────
if settings.evolution_database_url:
    import psycopg
    dsn = settings.evolution_database_url
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                # Verificar columnas de la tabla Contact
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'Contact' ORDER BY ordinal_position
                """)
                cols = [r[0] for r in cur.fetchall()]
                print(f"Columnas de Contact: {cols}\n")

                # Muestra de contactos con nombre
                has_name = 'name' in cols
                if has_name:
                    cur.execute("""
                        SELECT c."remoteJid", c.name, c."pushName"
                        FROM "Contact" c
                        JOIN "Instance" i ON i.id = c."instanceId"
                        WHERE i.name = %s
                          AND (c.name IS NOT NULL AND c.name != '' OR c."pushName" IS NOT NULL)
                        ORDER BY c.name NULLS LAST
                        LIMIT 20
                    """, (session.instance_name,))
                    rows = cur.fetchall()
                    print(f"Contactos en Evolution (primeros 20, con name/pushName):")
                    for jid, name, push in rows:
                        print(f"  JID={jid:<30} name={str(name):<25} push={push}")
                else:
                    cur.execute("""
                        SELECT c."remoteJid", c."pushName"
                        FROM "Contact" c
                        JOIN "Instance" i ON i.id = c."instanceId"
                        WHERE i.name = %s AND c."pushName" IS NOT NULL
                        LIMIT 20
                    """, (session.instance_name,))
                    rows = cur.fetchall()
                    print(f"Contactos en Evolution (solo pushName):")
                    for jid, push in rows:
                        print(f"  JID={jid:<30} push={push}")

                cur.execute("""
                    SELECT COUNT(*) FROM "Contact" c
                    JOIN "Instance" i ON i.id = c."instanceId"
                    WHERE i.name = %s
                """, (session.instance_name,))
                total = cur.fetchone()[0]
                print(f"\nTotal contactos en Evolution DB: {total}")
    except Exception as e:
        print(f"ERROR conectando a Evolution DB: {e}")
else:
    print("evolution_database_url no configurado\n")

# ── 2. Conversations en nuestra DB ────────────────────────────
print(f"\n{'─'*60}")
convs = (
    db.query(Conversation)
    .filter(
        Conversation.tenant_id == tenant.id,
        Conversation.whatsapp_connection_id == session.active_connection_id,
    )
    .order_by(Conversation.last_message_at.desc().nullslast())
    .limit(30)
    .all()
)
print(f"Conversaciones activas (30 más recientes):")
placeholder_count = 0
for c in convs:
    from app.shared.core.phone import is_placeholder_contact_name
    is_ph = is_placeholder_contact_name(c.contact_name or "", c.contact_phone or "")
    flag = "⚠ PLACEHOLDER" if is_ph else "✓"
    if is_ph:
        placeholder_count += 1
    print(f"  {flag:<15} phone={str(c.contact_phone):<20} name={str(c.contact_name):<25} jid={c.contact_jid or ''}")

total_convs = db.query(Conversation).filter(
    Conversation.tenant_id == tenant.id,
    Conversation.whatsapp_connection_id == session.active_connection_id,
).count()
print(f"\nTotal conversaciones: {total_convs} | Con placeholder: {placeholder_count}")

# ── 3. Test de enriquecimiento en seco ────────────────────────
print(f"\n{'─'*60}")
print("Ejecutando enrich_tenant_conversations (dry-run, sin commit)...")
try:
    from app.application.sync.contact_identity_service import enrich_tenant_conversations
    stats = enrich_tenant_conversations(db, tenant=tenant, session=session)
    print(f"Stats: {stats}")
    # Ver cuántos cambiarían
    changed = 0
    for c in db.query(Conversation).filter(
        Conversation.tenant_id == tenant.id,
        Conversation.whatsapp_connection_id == session.active_connection_id,
    ).all():
        from app.shared.core.phone import is_placeholder_contact_name
        is_ph = is_placeholder_contact_name(c.contact_name or "", c.contact_phone or "")
        if not is_ph:
            changed += 1
    print(f"Con nombre real después de enrich: {changed}/{total_convs}")
except Exception as e:
    import traceback
    print(f"ERROR en enrich: {e}")
    traceback.print_exc()
finally:
    db.rollback()  # No persistir

db.close()
print(f"\n{'='*60}\n")
