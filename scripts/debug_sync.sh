#!/usr/bin/env bash
# Diagnóstico rápido de sync WhatsApp (local dev)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"
export PYTHONPATH=.

echo "=== Health API ==="
curl -sf http://127.0.0.1:8000/health | python3 -m json.tool

echo ""
echo "=== Webhook Evolution ==="
EVO_KEY="${EVOLUTION_API_KEY:-dev-evolution-key-local}"
INSTANCE="${1:-t_c728cc56241e4c3a}"
curl -sf "http://127.0.0.1:8080/webhook/find/${INSTANCE}" -H "apikey: ${EVO_KEY}" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('url:', d.get('url')); print('enabled:', d.get('enabled'))"

echo ""
echo "=== URLs esperadas (.env) ==="
../.venv/bin/python -c "
from app.config import settings
print('APP_PUBLIC_URL:', settings.app_public_url)
print('WEBHOOK_BASE:', settings.evolution_webhook_base_url())
"

echo ""
echo "=== Últimos mensajes en BD (2h) ==="
../.venv/bin/python <<'PY'
from datetime import datetime, timezone, timedelta
from app.infrastructure.persistence.database import SessionLocal
from app.domain.entities import Tenant, Message, Conversation
from sqlalchemy import desc

with SessionLocal() as db:
    tenant = db.query(Tenant).filter(Tenant.slug=='string').first()
    if not tenant:
        tenant = db.query(Tenant).order_by(Tenant.created_at.desc()).first()
    since = datetime.now(timezone.utc) - timedelta(hours=2)
    rows = db.query(Message).filter(Message.tenant_id==tenant.id, Message.created_at>=since).order_by(desc(Message.created_at)).limit(8).all()
    for m in rows:
        c = db.query(Conversation).filter(Conversation.id==m.conversation_id).first()
        print(f'{m.created_at} {m.direction} [{c.contact_name if c else "?"}] {m.body[:50]!r}')
PY

echo ""
echo "=== Webhook trace (Redis) ==="
../.venv/bin/python <<'PY'
import uuid
from app.infrastructure.persistence.database import SessionLocal
from app.domain.entities import Tenant
from app.application.sync.webhook_trace_service import list_webhook_trace, last_webhook_at

with SessionLocal() as db:
    tenant = db.query(Tenant).filter(Tenant.slug=='string').first()
    if not tenant:
        tenant = db.query(Tenant).order_by(Tenant.created_at.desc()).first()
    print('last_webhook_at:', last_webhook_at(tenant.id))
    for row in list_webhook_trace(tenant.id, limit=5):
        print(row)
PY
