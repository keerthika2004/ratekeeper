"""Settings, read from environment variables prefixed RATEKEEPER_ (or a .env file).

Example: RATEKEEPER_REDIS_URL=redis://redis:6379/0

Defaults are for local development against `docker compose up`. The password
in the default database URL is a throwaway local value, not a secret.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RATEKEEPER_", env_file=".env", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "postgresql+asyncpg://ratekeeper:ratekeeper-local-dev@localhost:5432/ratekeeper"

    redis_timeout_seconds: float = 0.1
    """Upper bound for one check's Redis call. A check normally takes well under 1 ms;
    if Redis hasn't answered in 100 ms, the policy's failure mode is a better answer
    than making the caller wait."""
    redis_connect_timeout_seconds: float = 1.0

    policy_cache_max_size: int = 10_000
    policy_cache_refresh_after_seconds: float = 30
    policy_cache_expire_after_seconds: float = 3600

    breaker_failure_rate_threshold: float = 0.5
    breaker_window_size: int = 20
    breaker_minimum_calls: int = 10
    breaker_open_seconds: float = 5
    breaker_half_open_calls: int = 3
