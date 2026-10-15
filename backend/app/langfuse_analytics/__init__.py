"""Langfuse analytics plug-in: copy Langfuse traces into Postgres and serve per-knowledge-base cost / usage charts.

Self-contained, nothing else in the app depends on it:
- owns two tables, `langfuse_traces` and `langfuse_observations` (schema.sql, created on first use);
- reads Langfuse's public REST API with the keys the app already uses for tracing (LANGFUSE_*);
- adds one router under /api/analytics (the UI's Analytics tab).

Plug in (backend/app/main.py):

    from app.langfuse_analytics import router as langfuse_analytics_router
    app.include_router(langfuse_analytics_router)

Remove: delete those two lines and this package (optionally DROP the two tables, see README.md). It never changes how
traces are written (app/observability.py) and stores no prompts or answers.
"""

from app.langfuse_analytics.api import router

__all__ = ["router"]
