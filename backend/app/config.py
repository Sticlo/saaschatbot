from __future__ import annotations

from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWED_DEEPSEEK_MODEL = "deepseek-chat"
_INSECURE_SECRET_KEYS = frozenset(
    {"change-me-in-env", "change-me", "secret", "changeme", ""}
)
_INSECURE_WEBHOOK_SECRETS = frozenset(
    {"change-me-webhook-secret", "dev-webhook-secret-local", ""}
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[2] / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "SaasChatbot"
    app_env: str = "development"
    debug: bool = True
    secret_key: str = "change-me-in-env"

    api_host: str = "0.0.0.0"
    api_port: int = 8000

    database_url: str = "postgresql+psycopg://saaschatbot:password@localhost:5432/saaschatbot"
    redis_url: str = "redis://localhost:6379/0"

    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 1440

    trial_bait_limit: int = 10
    paid_daily_bait_limit: int = 100
    default_plan_slug: str = "pro"

    # Outbound / carnadas
    bait_delay_min_seconds: int = 45
    bait_delay_max_seconds: int = 120
    outbound_worker_poll_seconds: float = 5.0
    default_bait_template: str = (
        "Hola{name_part}! Vi tu negocio y me gustaría contarte cómo podemos ayudarte. "
        "¿Tienes un momentico para charlar?"
    )

    # DeepSeek IA — solo deepseek-chat (económico). No usar reasoner ni v4-pro.
    deepseek_api_key: str = ""
    deepseek_api_base: str = "https://api.deepseek.com"
    deepseek_chat_model: str = ALLOWED_DEEPSEEK_MODEL
    deepseek_classifier_model: str = ALLOWED_DEEPSEEK_MODEL
    ai_reply_delay_min_seconds: float = 2.0
    ai_reply_delay_max_seconds: float = 6.0
    # Costo IA — deepseek-chat + heurísticas (sin clasificador LLM por defecto)
    ai_classifier_use_llm: bool = False
    ai_history_messages: int = 10
    ai_reply_max_tokens: int = 350
    ai_trial_daily_reply_limit: int = 80
    ai_paid_daily_reply_limit: int = 400
    ai_default_mode: str = "qualify"
    ai_trial_daily_classify_limit: int = 500
    ai_paid_daily_classify_limit: int = 0  # 0 = ilimitado (clasificar no cuesta casi nada)
    ai_classify_delay_seconds: float = 0.3
    ai_maps_plans_per_day: int = 5

    # Workers — en producción la API no arranca workers (procesos separados).
    embed_workers_in_api: bool = False
    ai_max_parallel_jobs: int = 20
    ai_slot_wait_seconds: float = 120.0

    # Réplicas sugeridas en docker-compose.prod (documentación operativa).
    worker_webhook_replicas: int = 5
    worker_outbound_replicas: int = 15
    worker_ai_replicas: int = 4

    evolution_api_url: str = "http://localhost:8080"
    evolution_api_key: str = "change-me-evolution-key"
    evolution_database_url: str = ""
    app_public_url: str = "http://localhost:8000"
    evolution_webhook_secret: str = "change-me-webhook-secret"
    # True solo si Evolution corre dentro de Docker y la API en el host (localhost).
    evolution_in_docker: bool = False

    # WhatsApp provider: waha (Chrome real, recomendado) | evolution
    whatsapp_provider: str = "evolution"
    waha_api_url: str = "http://localhost:3001"
    waha_api_key: str = "dev-waha-key-local"
    waha_engine: str = "WEBJS"

    # Chatwoot — sincronización WhatsApp vía WAHA/Evolution ↔ Chatwoot ↔ panel
    chatwoot_enabled: bool = True
    chatwoot_url: str = "http://localhost:3000"
    chatwoot_account_id: str = "1"
    chatwoot_api_token: str = ""
    chatwoot_days_limit_import_messages: int = 3
    chatwoot_inbox_messages_per_chat: int = 15
    chatwoot_inbox_max_chats_per_sync: int = 12
    chatwoot_webhook_secret: str = "dev-chatwoot-webhook-local"

    # OAuth — Google / GitHub (login social)
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/v1/auth/google/callback"
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = "http://localhost:8000/api/v1/auth/github/callback"
    oauth_success_redirect: str = "http://localhost:4200/precios"
    oauth_error_redirect: str = "http://localhost:4200/login"
    resend_api_key: str = ""
    email_from: str = "Omitel <onboarding@resend.dev>"
    site_public_url: str = "http://localhost:4200"
    magic_link_expire_minutes: int = 15
    password_reset_expire_minutes: int = 30
    auth_login_max_attempts: int = 10
    auth_login_window_seconds: int = 900
    auth_forgot_max_attempts: int = 5
    auth_forgot_window_seconds: int = 3600

    # Wompi — pagos COP (Colombia)
    wompi_public_key: str = ""
    wompi_integrity_secret: str = ""
    wompi_events_secret: str = ""
    wompi_checkout_redirect_url: str = "http://localhost:4200/precios"

    def chatwoot_base_url(self) -> str:
        """URL que Evolution usa para hablar con Chatwoot."""
        url = self.chatwoot_url.rstrip("/")
        if self.evolution_in_docker:
            return url.replace("://localhost", "://chatwoot").replace("://127.0.0.1", "://chatwoot")
        return url

    def chatwoot_internal_url(self) -> str:
        """URL que WAHA en Docker usa para llegar a Chatwoot."""
        return self.chatwoot_url.rstrip("/").replace("://localhost", "://chatwoot").replace(
            "://127.0.0.1", "://chatwoot"
        )

    def chatwoot_webhook_url(self) -> str:
        return f"{self.evolution_webhook_base_url()}/webhooks/chatwoot"

    def evolution_webhook_base_url(self) -> str:
        """URL que Evolution usa para POST de webhooks."""
        base = self.app_public_url.rstrip("/")
        if not self.evolution_in_docker:
            # Evolution nativo (npm) resuelve 127.0.0.1 de forma fiable; host.docker.internal falla.
            return (
                base.replace("host.docker.internal", "127.0.0.1")
                .replace("://localhost", "://127.0.0.1")
            )
        if "localhost" in base or "127.0.0.1" in base:
            return (
                base.replace("://localhost", "://host.docker.internal")
                .replace("://127.0.0.1", "://host.docker.internal")
            )
        return base

    @field_validator("chatwoot_days_limit_import_messages")
    @classmethod
    def _cap_chatwoot_history_days(cls, value: int) -> int:
        """Evolution importa historial en bloque — >7 días satura Node/Postgres."""
        try:
            n = int(value)
        except (TypeError, ValueError):
            return 3
        return max(1, min(n, 7))

    @field_validator("chatwoot_inbox_messages_per_chat", "chatwoot_inbox_max_chats_per_sync")
    @classmethod
    def _cap_chatwoot_inbox_batch(cls, value: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return 15
        return max(5, min(n, 50))

    @field_validator(
        "ai_history_messages",
        "ai_reply_max_tokens",
        "ai_trial_daily_reply_limit",
        "ai_paid_daily_reply_limit",
        "ai_trial_daily_classify_limit",
        "ai_paid_daily_classify_limit",
        "ai_maps_plans_per_day",
    )
    @classmethod
    def _cap_ai_limits(cls, value: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return 200
        return max(1, min(n, 5000))

    @field_validator("deepseek_chat_model", "deepseek_classifier_model")
    @classmethod
    def _force_cheap_deepseek_model(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()
        if cleaned != ALLOWED_DEEPSEEK_MODEL:
            return ALLOWED_DEEPSEEK_MODEL
        return cleaned

    @field_validator("debug", mode="before")
    @classmethod
    def _default_debug_off_in_prod(cls, value, info):
        env = (info.data.get("app_env") or "").lower()
        if env in ("production", "prod"):
            return False
        return value

    @model_validator(mode="after")
    def _apply_development_defaults(self) -> "Settings":
        env = (self.app_env or "").lower()
        if env in ("development", "dev", "local") and not self.embed_workers_in_api:
            object.__setattr__(self, "embed_workers_in_api", True)
        return self

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> "Settings":
        env = (self.app_env or "").lower()
        if env in ("production", "prod"):
            if self.secret_key.strip().lower() in _INSECURE_SECRET_KEYS or len(self.secret_key) < 32:
                raise ValueError(
                    "SECRET_KEY inseguro o ausente — define uno de ≥32 caracteres en producción"
                )
            if self.evolution_webhook_secret.strip().lower() in _INSECURE_WEBHOOK_SECRETS:
                raise ValueError(
                    "EVOLUTION_WEBHOOK_SECRET debe configurarse en producción"
                )
            if self.debug:
                object.__setattr__(self, "debug", False)
        return self


settings = Settings()
