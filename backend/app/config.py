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
    app_public_url: str = "http://host.docker.internal:8000"
    evolution_webhook_secret: str = "change-me-webhook-secret"

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
