#!/usr/bin/env python3
"""One-shot repo restructure: monorepo layout + clean architecture layers."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# --- Physical moves: (src relative to ROOT, dst relative to ROOT) ---
MOVES: list[tuple[str, str]] = [
    ("static/panel", "web/panel"),
    ("static/media", "web/media"),
    ("services/evolution-api", "whatsapp/evolution-api"),
    ("app", "backend/app"),
    ("alembic", "backend/alembic"),
    ("tests", "backend/tests"),
    ("requirements.txt", "backend/requirements.txt"),
    ("pytest.ini", "backend/pytest.ini"),
    ("alembic.ini", "backend/alembic.ini"),
    ("Dockerfile", "backend/Dockerfile"),
    ("docker-compose.yml", "deploy/docker-compose.yml"),
    ("docker-compose.prod.yml", "deploy/docker-compose.prod.yml"),
    ("security-audit.js", "tools/security-audit.js"),
]

# --- Internal backend restructure (relative to backend/app) ---
INTERNAL_MOVES: list[tuple[str, str]] = [
    ("main.py", "presentation/main.py"),
    ("api", "presentation/api"),
    ("schemas", "presentation/schemas"),
    ("api/panel_ws.py", "presentation/websockets/panel_ws.py"),
    ("core", "shared/core"),
    ("database.py", "infrastructure/persistence/database.py"),
    ("redis_client.py", "infrastructure/cache/redis_client.py"),
    ("models", "domain/entities"),
    ("services/whatsapp_service.py", "application/whatsapp/whatsapp_service.py"),
    ("services/whatsapp_status.py", "application/whatsapp/whatsapp_status.py"),
    ("services/whatsapp_conversation_service.py", "application/conversations/whatsapp_conversation_service.py"),
    ("services/message_service.py", "application/messaging/message_service.py"),
    ("services/webhook_processor.py", "application/messaging/webhook_processor.py"),
    ("services/media_cache.py", "application/messaging/media_cache.py"),
    ("services/chat_sync_service.py", "application/sync/chat_sync_service.py"),
    ("services/sync_scheduler.py", "application/sync/sync_scheduler.py"),
    ("services/contact_identity_service.py", "application/sync/contact_identity_service.py"),
    ("services/outbound_service.py", "application/outbound/outbound_service.py"),
    ("services/outbound_dedup_service.py", "application/outbound/outbound_dedup_service.py"),
    ("services/bait_scheduler.py", "application/outbound/bait_scheduler.py"),
    ("services/bait_limit_service.py", "application/outbound/bait_limit_service.py"),
    ("services/ai_service.py", "application/ai/ai_service.py"),
    ("services/ai_classifier_service.py", "application/ai/ai_classifier_service.py"),
    ("services/ai_conversation_service.py", "application/ai/ai_conversation_service.py"),
    ("services/ai_queue_service.py", "application/ai/ai_queue_service.py"),
    ("services/tenant_service.py", "application/billing/tenant_service.py"),
    ("services/plan_service.py", "application/billing/plan_service.py"),
    ("services/subscription_service.py", "application/billing/subscription_service.py"),
    ("services/realtime_service.py", "application/realtime/realtime_service.py"),
    ("services/queue_service.py", "application/workers/queue_service.py"),
    ("services/worker_runtime.py", "application/workers/worker_runtime.py"),
    ("services/evolution_client.py", "infrastructure/evolution/evolution_client.py"),
    ("services/evolution_store.py", "infrastructure/evolution/evolution_store.py"),
    ("services/deepseek_client.py", "infrastructure/ai/deepseek_client.py"),
]

SERVICE_REPLACEMENTS = [
    ("app.application.whatsapp.whatsapp_service", "app.application.whatsapp.whatsapp_service"),
    ("app.application.whatsapp.whatsapp_status", "app.application.whatsapp.whatsapp_status"),
    ("app.application.conversations.whatsapp_conversation_service", "app.application.conversations.whatsapp_conversation_service"),
    ("app.application.messaging.message_service", "app.application.messaging.message_service"),
    ("app.application.messaging.webhook_processor", "app.application.messaging.webhook_processor"),
    ("app.application.messaging.media_cache", "app.application.messaging.media_cache"),
    ("app.application.sync.chat_sync_service", "app.application.sync.chat_sync_service"),
    ("app.application.sync.sync_scheduler", "app.application.sync.sync_scheduler"),
    ("app.application.sync.contact_identity_service", "app.application.sync.contact_identity_service"),
    ("app.application.outbound.outbound_service", "app.application.outbound.outbound_service"),
    ("app.application.outbound.outbound_dedup_service", "app.application.outbound.outbound_dedup_service"),
    ("app.application.outbound.bait_scheduler", "app.application.outbound.bait_scheduler"),
    ("app.application.outbound.bait_limit_service", "app.application.outbound.bait_limit_service"),
    ("app.application.ai.ai_service", "app.application.ai.ai_service"),
    ("app.application.ai.ai_classifier_service", "app.application.ai.ai_classifier_service"),
    ("app.application.ai.ai_conversation_service", "app.application.ai.ai_conversation_service"),
    ("app.application.ai.ai_queue_service", "app.application.ai.ai_queue_service"),
    ("app.application.billing.tenant_service", "app.application.billing.tenant_service"),
    ("app.application.billing.plan_service", "app.application.billing.plan_service"),
    ("app.application.billing.subscription_service", "app.application.billing.subscription_service"),
    ("app.application.realtime.realtime_service", "app.application.realtime.realtime_service"),
    ("app.application.workers.queue_service", "app.application.workers.queue_service"),
    ("app.application.workers.worker_runtime", "app.application.workers.worker_runtime"),
    ("app.infrastructure.evolution.evolution_client", "app.infrastructure.evolution.evolution_client"),
    ("app.infrastructure.evolution.evolution_store", "app.infrastructure.evolution.evolution_store"),
    ("app.infrastructure.ai.deepseek_client", "app.infrastructure.ai.deepseek_client"),
]

IMPORT_REPLACEMENTS = [
    *SERVICE_REPLACEMENTS,
    ("app.presentation.websockets.panel_ws", "app.presentation.websockets.panel_ws"),
    ("app.presentation.api.", "app.presentation.api."),
    ("app.presentation.main", "app.presentation.main"),
    ("app.presentation.schemas.", "app.presentation.schemas."),
    ("app.shared.core.", "app.shared.core."),
    ("app.infrastructure.persistence.database", "app.infrastructure.persistence.database"),
    ("app.infrastructure.cache.redis_client", "app.infrastructure.cache.redis_client"),
    ("app.domain.entities.enums", "app.domain.entities.enums"),
    ("app.domain.entities.", "app.domain.entities."),
    ("from app.domain.entities import", "from app.domain.entities import"),
    ("import app.domain.entities", "import app.domain.entities"),
]


def safe_move(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise SystemExit(f"Destination exists: {dst}")
    shutil.move(str(src), str(dst))


def apply_import_replacements(content: str) -> str:
    for old, new in IMPORT_REPLACEMENTS:
        content = content.replace(old, new)
    return content


def patch_py_files(base: Path) -> None:
    for path in base.rglob("*.py"):
        if ".venv" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        updated = apply_import_replacements(text)
        if updated != text:
            path.write_text(updated, encoding="utf-8")


def create_init_files(app_dir: Path) -> None:
    packages = [
        "presentation",
        "presentation/api",
        "presentation/schemas",
        "presentation/websockets",
        "application",
        "application/whatsapp",
        "application/conversations",
        "application/messaging",
        "application/sync",
        "application/outbound",
        "application/ai",
        "application/billing",
        "application/realtime",
        "application/workers",
        "domain",
        "domain/entities",
        "infrastructure",
        "infrastructure/persistence",
        "infrastructure/cache",
        "infrastructure/evolution",
        "infrastructure/ai",
        "shared",
        "shared/core",
    ]
    for pkg in packages:
        init = app_dir / pkg / "__init__.py"
        init.parent.mkdir(parents=True, exist_ok=True)
        if not init.exists():
            init.write_text('"""Package."""\n', encoding="utf-8")

    # Remove empty services folder remnants
    services_dir = app_dir / "services"
    if services_dir.is_dir() and not any(services_dir.iterdir()):
        services_dir.rmdir()


def main() -> None:
    # Phase 1: top-level moves
    for src_rel, dst_rel in MOVES:
        safe_move(ROOT / src_rel, ROOT / dst_rel)

    app_dir = ROOT / "backend" / "app"
    if not app_dir.is_dir():
        raise SystemExit("backend/app not found after move")

    # Phase 2: move panel_ws before moving api folder wholesale
    panel_ws_src = app_dir / "api" / "panel_ws.py"
    if panel_ws_src.is_file():
        ws_dst = app_dir / "presentation" / "websockets" / "panel_ws.py"
        ws_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(panel_ws_src), str(ws_dst))

    # Phase 3: internal moves (skip panel_ws — already moved)
    for src_rel, dst_rel in INTERNAL_MOVES:
        if src_rel == "api/panel_ws.py":
            continue
        src = app_dir / src_rel
        dst = app_dir / dst_rel
        if src.is_file():
            safe_move(src, dst)
        elif src.is_dir():
            safe_move(src, dst)

    # Clean empty dirs
    for name in ("api", "schemas", "core", "models", "services"):
        d = app_dir / name
        if d.is_dir() and not any(d.rglob("*")):
            shutil.rmtree(d, ignore_errors=True)

    create_init_files(app_dir)

    # Phase 4: update imports across repo
    for base in (ROOT / "backend", ROOT / "scripts"):
        if base.is_dir():
            patch_py_files(base)

    print("Restructure complete.")


if __name__ == "__main__":
    main()
