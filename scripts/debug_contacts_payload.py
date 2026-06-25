#!/usr/bin/env python3
"""
Llama directamente a Evolution API para obtener los contactos de la instancia
y ver qué campos incluye para los @s.whatsapp.net sin nombre.
Corre: .venv/bin/python scripts/debug_contacts_payload.py 2>&1
"""
import sys, os, uuid
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend"))

from app.config import settings
from app.infrastructure.evolution.evolution_client import evolution_client

INSTANCE = "t_c728cc56241e4c3a"

print("Llamando a Evolution API fetchContacts...")
try:
    # Intentar fetchContacts
    result = evolution_client._request("GET", f"/contact/fetchContacts/{INSTANCE}", timeout=15)
    contacts = result if isinstance(result, list) else (result.get("contacts") or result.get("data") or [])
    print(f"Total contactos retornados por API: {len(contacts)}")

    # Mostrar primeros 5 sin nombre
    no_name = [c for c in contacts if not c.get("name") and not c.get("pushName")]
    with_name = [c for c in contacts if c.get("name") or c.get("pushName")]
    print(f"Con nombre: {len(with_name)} | Sin nombre: {len(no_name)}")

    print("\nEjemplo SIN nombre (primeros 3):")
    for c in no_name[:3]:
        print(f"  {c}")

    print("\nEjemplo CON nombre (primeros 3):")
    for c in with_name[:3]:
        print(f"  id={c.get('id') or c.get('remoteJid')} | name={c.get('name')} | push={c.get('pushName')} | notify={c.get('notify')}")

except Exception as e:
    print(f"Error fetchContacts: {e}")

# También buscar un contacto específico
print("\n--- Buscando contacto específico 573219469201 ---")
try:
    result = evolution_client._request(
        "GET",
        f"/contact/fetchContacts/{INSTANCE}",
        params={"remoteJid": "573219469201@s.whatsapp.net"},
        timeout=10
    )
    print(f"Resultado: {result}")
except Exception as e:
    print(f"Error: {e}")
