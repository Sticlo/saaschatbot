"""Lanzamiento: suscripciones que vencen, sitio y panel en dominios distintos
(www.omitel.net / app.omitel.net) y Evolution hablando con la API por la red interna."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from app.domain.entities.enums import SubscriptionStatus
from tests.conftest import make_wa_tenant, requires_db


def _sub(status: str, end: datetime | None):
    return SimpleNamespace(status=status, current_period_end=end)


# ── Suscripciones ───────────────────────────────────────────────────────────


def test_paid_subscription_lasts_its_period_plus_grace(monkeypatch):
    from app.application.billing.subscription_service import subscription_is_paid
    from app.config import settings

    monkeypatch.setattr(settings, "subscription_grace_days", 3)
    now = datetime.now(timezone.utc)
    active = SubscriptionStatus.ACTIVE.value

    assert subscription_is_paid(_sub(active, now + timedelta(days=10)))
    assert subscription_is_paid(_sub(active, now - timedelta(days=2))), "dentro de la gracia"
    assert not subscription_is_paid(_sub(active, now - timedelta(days=4)))
    assert not subscription_is_paid(_sub(active, (now - timedelta(days=30)).replace(tzinfo=None)))
    assert not subscription_is_paid(_sub(SubscriptionStatus.TRIAL.value, now + timedelta(days=10)))
    assert not subscription_is_paid(None)


def test_renewing_early_keeps_the_days_already_paid():
    from app.application.billing.subscription_service import renewal_period

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=5)
    start, new_end = renewal_period(_sub(SubscriptionStatus.ACTIVE.value, end), now)
    assert start == end
    assert new_end == end + timedelta(days=30)

    start, new_end = renewal_period(_sub(SubscriptionStatus.PAST_DUE.value, now - timedelta(days=9)), now)
    assert start == now, "si ya venció, los 30 días cuentan desde hoy"
    assert new_end == now + timedelta(days=30)


@requires_db
def test_lapsed_subscriptions_become_past_due_and_lose_paid_limits(monkeypatch):
    from app.application.ai.ai_usage_service import PLAN_REQUIRED, daily_reply_limit
    from app.application.billing.plan_service import ensure_default_plan
    from app.application.billing.subscription_service import (
        build_subscription_summary,
        expire_lapsed_subscriptions,
    )
    from app.config import settings
    from app.domain.entities import Subscription, Tenant
    from app.domain.entities.enums import TenantPlan
    from app.infrastructure.persistence.database import SessionLocal

    monkeypatch.setattr(settings, "subscription_grace_days", 3)
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        plan = ensure_default_plan(db)
        lapsed_tenant, _ = make_wa_tenant(db, label="Vencido")
        current_tenant, _ = make_wa_tenant(db, label="AlDia")
        db.add_all(
            [
                Subscription(
                    tenant_id=lapsed_tenant.id,
                    plan_id=plan.id,
                    status=SubscriptionStatus.ACTIVE.value,
                    current_period_start=now - timedelta(days=40),
                    current_period_end=now - timedelta(days=10),
                ),
                Subscription(
                    tenant_id=current_tenant.id,
                    plan_id=plan.id,
                    status=SubscriptionStatus.ACTIVE.value,
                    current_period_start=now - timedelta(days=5),
                    current_period_end=now + timedelta(days=25),
                ),
            ]
        )
        db.query(Tenant).filter(Tenant.id.in_([lapsed_tenant.id, current_tenant.id])).update(
            {"plan": TenantPlan.PAID.value}, synchronize_session=False
        )
        db.commit()

        lapsed_tenant_row = db.get(Tenant, lapsed_tenant.id)
        lapsed_sub = db.query(Subscription).filter(Subscription.tenant_id == lapsed_tenant.id).one()
        summary = build_subscription_summary(lapsed_tenant_row, lapsed_sub, plan)
        assert summary["is_paid"] is False
        assert summary["needs_payment"] is True, "el panel pide renovar aunque el barrido no haya corrido"
        assert summary["status"] == SubscriptionStatus.PAST_DUE.value
        assert daily_reply_limit(db, lapsed_tenant_row) == PLAN_REQUIRED, "sin plan vigente la IA se detiene"

        assert expire_lapsed_subscriptions(db, now=now) >= 1

    with SessionLocal() as db:
        lapsed_sub = db.query(Subscription).filter(Subscription.tenant_id == lapsed_tenant.id).one()
        current_sub = db.query(Subscription).filter(Subscription.tenant_id == current_tenant.id).one()
        assert lapsed_sub.status == SubscriptionStatus.PAST_DUE.value
        assert db.get(Tenant, lapsed_tenant.id).plan == TenantPlan.SUSPENDED.value
        assert current_sub.status == SubscriptionStatus.ACTIVE.value
        assert db.get(Tenant, current_tenant.id).plan == TenantPlan.PAID.value


# ── Sitio y panel en dominios distintos ─────────────────────────────────────


def _prod_settings(**overrides):
    from app.config import Settings

    values = dict(
        app_env="production",
        secret_key="s" * 48,
        evolution_webhook_secret="prod-webhook-secret",
        site_public_url="https://www.omitel.net",
        panel_public_url="https://app.omitel.net",
        app_public_url="https://app.omitel.net",
        cors_origins="https://omitel.net, https://omitel.net/",
        evolution_webhook_internal_url="",
        evolution_in_docker=True,
        resend_api_key="re_test_not_used",
        chatwoot_enabled=False,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_production_cors_allows_only_the_public_site():
    origins = _prod_settings().cors_allowed_origins()
    assert origins == ["https://omitel.net", "https://www.omitel.net"]


def test_evolution_posts_webhooks_through_the_internal_network():
    assert _prod_settings(evolution_webhook_internal_url="http://api:8000/").evolution_webhook_base_url() == (
        "http://api:8000"
    )
    assert _prod_settings().evolution_webhook_base_url() == "https://app.omitel.net"


def test_oauth_session_cookie_is_set_on_the_panel_domain(monkeypatch):
    from app.config import settings
    from app.presentation.api import auth

    monkeypatch.setattr(settings, "site_public_url", "https://www.omitel.net")
    monkeypatch.setattr(settings, "panel_public_url", "https://app.omitel.net")
    monkeypatch.setattr(settings, "app_public_url", "https://app.omitel.net")
    monkeypatch.setattr(auth, "create_oauth_finish_token", lambda _token: "finish-token")

    assert auth._site_url("/panel?welcome=1") == "https://app.omitel.net/panel?welcome=1"
    assert auth._site_url("/precios") == "https://www.omitel.net/precios"

    url = urlparse(auth._oauth_finish_url("jwt", "https://app.omitel.net/panel?welcome=1"))
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://app.omitel.net/api/v1/auth/oauth/finish"
    assert parse_qs(url.query)["next"] == ["/panel?welcome=1"]


@requires_db
def test_panel_page_knows_where_the_public_site_lives(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "site_public_url", "https://www.omitel.net/")
    response = client.get("/panel")
    assert response.status_code == 200
    assert 'name="omitel-site-url" content="https://www.omitel.net"' in response.text
    assert "</head>" in response.text
