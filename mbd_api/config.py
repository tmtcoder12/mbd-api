"""Validated runtime configuration."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed application settings with production safeguards."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_version: str = "1.0.0"
    log_level: str = "INFO"
    json_logs: bool = True

    openai_api_key: SecretStr
    openai_timeout_seconds: float = Field(default=45.0, ge=1.0, le=120.0)
    openai_max_retries: int = Field(default=2, ge=0, le=5)
    supabase_url: str
    supabase_service_role_key: SecretStr
    supabase_timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    readiness_timeout_seconds: float = Field(default=3.0, ge=0.25, le=15.0)

    widget_signing_keys: SecretStr
    widget_active_kid: str = "v1"
    widget_token_max_age_seconds: int = Field(default=900, ge=60, le=7200)

    chat_persistence: bool = True
    min_score_default: float = Field(default=0.0, ge=-1.0, le=1.0)
    top_k: int = Field(default=8, ge=1, le=50)
    query_classifier_model: str = "gpt-5-mini"

    allow_localhost_origins: bool = True
    trust_proxy_headers: bool = False
    max_request_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    max_message_chars: int = Field(default=4000, ge=100, le=20_000)

    rate_limit_requests_per_minute: int = Field(default=30, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)
    session_rate_limit_requests_per_minute: int = Field(default=45, ge=1)
    session_rate_limit_window_seconds: int = Field(default=60, ge=1)
    token_issue_rate_limit_requests_per_minute: int = Field(default=30, ge=1)
    token_issue_rate_limit_window_seconds: int = Field(default=60, ge=1)

    rate_limit_redis_url: str | None = None
    query_cache_enabled: bool = False
    query_cache_redis_url: str | None = None
    query_cache_ttl_seconds: int = Field(default=900, ge=1)
    query_cache_namespace: str = "qcache:v1"
    query_cache_semantic_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    query_cache_semantic_max_candidates: int = Field(default=200, ge=1, le=5000)
    query_cache_require_restaurant_relevance: bool = True
    query_cache_classifier_model: str = "gpt-5-nano"
    query_cache_classifier_timeout_ms: int = Field(default=250, ge=0, le=10_000)

    stripe_webhooks_enabled: bool = False
    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None

    @field_validator("openai_api_key", "supabase_service_role_key", "widget_signing_keys")
    @classmethod
    def required_secret_must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("required secrets must not be blank")
        return value

    @model_validator(mode="after")
    def validate_runtime_contract(self) -> Settings:
        if not self.supabase_url.startswith(("http://", "https://")):
            raise ValueError("SUPABASE_URL must be an http(s) URL")
        if self.query_cache_enabled and not (self.query_cache_redis_url or self.rate_limit_redis_url):
            raise ValueError("QUERY_CACHE_ENABLED requires QUERY_CACHE_REDIS_URL or RATE_LIMIT_REDIS_URL")
        if self.stripe_webhooks_enabled:
            stripe_key = self.secret(self.stripe_secret_key).strip()
            webhook_key = self.secret(self.stripe_webhook_secret).strip()
            if not stripe_key:
                raise ValueError("STRIPE_WEBHOOKS_ENABLED requires STRIPE_SECRET_KEY")
            if not webhook_key:
                raise ValueError("STRIPE_WEBHOOKS_ENABLED requires STRIPE_WEBHOOK_SECRET")
            if not stripe_key.startswith(("sk_test_", "sk_live_")):
                raise ValueError("STRIPE_SECRET_KEY must be a Stripe test or live secret key")
            if not webhook_key.startswith("whsec_"):
                raise ValueError("STRIPE_WEBHOOK_SECRET must start with whsec_")
        if self.app_env == "production":
            raw_keys = self.widget_signing_keys.get_secret_value().strip()
            secrets = [part.split(":", 1)[-1].strip() for part in raw_keys.split(",") if part.strip()]
            if not secrets or any(len(secret) < 32 for secret in secrets):
                raise ValueError("Each widget signing secret must contain at least 32 characters in production")
            if self.allow_localhost_origins:
                raise ValueError("ALLOW_LOCALHOST_ORIGINS must be false in production")
        return self

    def secret(self, value: SecretStr | None) -> str:
        return value.get_secret_value() if value else ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
