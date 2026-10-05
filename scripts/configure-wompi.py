#!/usr/bin/env python3
"""Guarda las llaves de Wompi en el .env de la raíz sin mostrarlas en pantalla.

Uso:  python3 scripts/configure-wompi.py
Las llaves salen de comercios.wompi.co → Desarrolladores (sandbox o producción).
"""
from __future__ import annotations

import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
RELOAD_TRIGGER = ROOT / "backend" / "app" / "config.py"

KEYS = [
    ("WOMPI_PUBLIC_KEY", "Llave pública", {"test": "pub_test_", "prod": "pub_prod_"}),
    ("WOMPI_PRIVATE_KEY", "Llave privada", {"test": "prv_test_", "prod": "prv_prod_"}),
    ("WOMPI_INTEGRITY_SECRET", "Secreto de integridad", {"test": "test_integrity_", "prod": "prod_integrity_"}),
    ("WOMPI_EVENTS_SECRET", "Secreto de eventos", {"test": "test_events_", "prod": "prod_events_"}),
]
API_BASE = {"test": "https://sandbox.wompi.co/v1", "prod": "https://production.wompi.co/v1"}


def ask(label: str, prefixes: dict[str, str]) -> tuple[str, str]:
    hint = " o ".join(f"{p}…" for p in prefixes.values())
    while True:
        value = getpass.getpass(f"{label} ({hint}): ").strip()
        for env, prefix in prefixes.items():
            if value.startswith(prefix) and len(value) > len(prefix) + 8:
                return value, env
        got = f"{len(value)} caracteres que empiezan por «{value[:4]}»" if value else "nada (¿se pegó?)"
        print(f"  ✗ No parece una {label.lower()} de Wompi. Debe empezar por {hint}. Recibí {got}.")


def get_json(url: str, headers: dict[str, str]) -> tuple[int, dict]:
    request = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (urllib.error.URLError, TimeoutError):
        return 0, {}


def check_with_wompi(env: str, public_key: str, private_key: str) -> bool:
    base = API_BASE[env]
    status, body = get_json(f"{base}/merchants/{public_key}", {})
    merchant = (body.get("data") or {}) if status == 200 else {}
    if not merchant:
        print(f"  ✗ Wompi no reconoce la llave pública (HTTP {status or 'sin conexión'}).")
        return False
    name = merchant.get("legal_name") or merchant.get("name") or "tu comercio"
    print(f"  ✓ Llave pública válida: {name}")

    status, _ = get_json(f"{base}/transactions?reference=omitel-config-check", {"Authorization": f"Bearer {private_key}"})
    if status != 200:
        print(f"  ✗ Wompi rechazó la llave privada (HTTP {status or 'sin conexión'}).")
        return False
    print("  ✓ Llave privada válida")
    return True


def write_env(values: dict[str, str], path: Path = ENV_PATH) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values)
    out: list[str] = []
    for line in lines:
        match = re.match(r"\s*(?:export\s+)?([A-Z0-9_]+)\s*=", line)
        key = match.group(1) if match else None
        if key in pending:
            out.append(f"{key}={pending.pop(key)}")
        elif key in values or key == "WOMPI_API_BASE_URL":
            # La URL de la API sale del tipo de llave; una fija de otro ambiente rompería los cobros.
            continue
        else:
            out.append(line)
    if pending:
        if out and out[-1].strip():
            out.append("")
        out.append("# Wompi — pagos COP")
        out.extend(f"{key}={value}" for key, value in pending.items())
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def read_env(path: Path = ENV_PATH) -> dict[str, str]:
    found: dict[str, str] = {}
    if not path.exists():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*)$", line)
        if match:
            found[match.group(1)] = match.group(2).strip().strip("'\"")
    return found


def check_saved() -> int:
    """Revisa las llaves que ya están en .env (pegadas a mano) sin mostrarlas."""
    saved = read_env()
    envs: set[str] = set()
    ok = True
    for key, label, prefixes in KEYS:
        value = saved.get(key, "")
        env = next((e for e, p in prefixes.items() if value.startswith(p) and len(value) > len(p) + 8), None)
        if env is None:
            got = f"empieza por «{value[:4]}»" if value else "no está"
            print(f"  ✗ {key}: {got} (debe empezar por {' o '.join(prefixes.values())})")
            ok = False
        else:
            print(f"  ✓ {key}: formato correcto")
            envs.add(env)
    if not ok:
        return 1
    if len(envs) > 1:
        print("  ✗ Hay llaves de sandbox mezcladas con llaves de producción.")
        return 1
    env = envs.pop()
    print("\nProbando las llaves con Wompi…")
    if not check_with_wompi(env, saved["WOMPI_PUBLIC_KEY"], saved["WOMPI_PRIVATE_KEY"]):
        return 1
    RELOAD_TRIGGER.touch()
    print(f"\n✓ Todo bien — modo {'SANDBOX' if env == 'test' else 'PRODUCCIÓN'}. El backend local se recarga solo.")
    return 0


def main() -> int:
    if "--check" in sys.argv:
        return check_saved()
    print("Configurar Wompi (las llaves no se muestran mientras las escribes)\n")
    values: dict[str, str] = {}
    envs: set[str] = set()
    for key, label, prefixes in KEYS:
        value, env = ask(label, prefixes)
        values[key] = value
        envs.add(env)
    if len(envs) > 1:
        print("\n✗ Mezclaste llaves de sandbox y de producción. Usa las cuatro del mismo ambiente.")
        return 1
    env = envs.pop()

    print("\nProbando las llaves con Wompi…")
    if not check_with_wompi(env, values["WOMPI_PUBLIC_KEY"], values["WOMPI_PRIVATE_KEY"]):
        print("\nNo se guardó nada. Revisa las llaves en comercios.wompi.co → Desarrolladores.")
        return 1

    write_env(values)
    RELOAD_TRIGGER.touch()
    label = "SANDBOX (pruebas, no cobra dinero real)" if env == "test" else "PRODUCCIÓN (cobros reales)"
    print(f"\n✓ Guardado en .env — modo {label}.")
    print("  El backend local se recarga solo en unos segundos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
