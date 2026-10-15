"""Plug-in settings: ANALYTICS_* variables in the same .env. Langfuse keys and URL come from the app's own settings
(LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL, LANGFUSE_ENVIRONMENT)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class AnalyticsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ANALYTICS_",
        env_file=Path(__file__).resolve().parents[3] / ".env",
        extra="ignore",
        case_sensitive=False,
    )

    sync_lookback_days: int = 90  # first sync / full re-sync: traces this far back
    sync_overlap_hours: int = 6  # incremental sync re-reads this window: costs and late spans arrive after the trace
    sync_page_size: int = 100  # Langfuse public API maximum
    sync_max_pages: int = 500  # per endpoint and sync, so one sync can't run forever on a very large project
    sync_tag: str = "graphbase"  # only traces with this tag (the app tags every trace it writes); empty = all
    request_timeout_seconds: float = 30.0
    request_retries: int = 4  # on HTTP 429 / 5xx / network errors, with backoff (Retry-After is honoured)
    # USD per 1M tokens for models Langfuse has no price for (e.g. Ollama), as JSON:
    # {"qwen2.5:7b-instruct": {"input": 0.0, "output": 0.0}}. Used only when Langfuse reports no cost.
    model_prices: dict[str, dict[str, float]] = {}
    recent_traces: int = 25  # rows in the "Recent traces" table
    # Keep each chat question's text (from the traced prompt) for "Questions by user". false = never store it.
    store_questions: bool = True


@lru_cache
def get_analytics_settings() -> AnalyticsSettings:
    return AnalyticsSettings()
