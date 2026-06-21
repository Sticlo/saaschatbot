from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
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


settings = Settings()
