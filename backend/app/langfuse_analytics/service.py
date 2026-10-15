"""What the Analytics tab can ask for, independent of HTTP: access scope, assembling results, Langfuse links.

Layers of the plug-in (each replaceable on its own):
    api.py      HTTP routes: parameters in, schemas out (the only FastAPI code)
    schemas.py  the response format (the API contract)
    service.py  this file: who may see what, and what each view contains
    store.py    SQL against the plug-in's two tables
    sync.py     Langfuse -> tables (client.py reads Langfuse, transform.py maps records to rows)
"""

import datetime as dt

from app import kb
from app.auth import CurrentUser
from app.config import get_settings
from app.langfuse_analytics import store, sync
from app.langfuse_analytics.settings import get_analytics_settings
from app.langfuse_analytics.transform import CATEGORIES, SOURCES


class NotConfigured(RuntimeError):
    pass


def langfuse_url() -> str:
    return get_settings().langfuse_base_url.rstrip("/")


def scope(user: CurrentUser, kb_name: str = "") -> tuple[list[str], list[dict], list[str]]:
    """(knowledge bases to report on, every KB the user can access, the ones they own). Raises 404 / 403 for a KB
    the user can't open, exactly like the app's other KB routes (kb.require_access)."""
    accessible = kb.list_for_user(user.user_id)
    if kb_name:
        kb.require_access(user, kb_name)
        names = [kb_name]
    else:
        names = [r["kb_name"] for r in accessible]
    return names, accessible, [r["kb_name"] for r in accessible if r["role"] == "owner"]


def _link(rows: list[dict]) -> None:
    base = langfuse_url()
    for row in rows:
        path = row.pop("html_path", None)
        row["url"] = f"{base}{path}" if path and path.startswith("/") else None


def sync_status() -> dict:
    store.ensure_schema()
    return {
        **sync.status(),
        **store.sync_overview(),
        "configured": sync.configured(),
        "langfuse_url": langfuse_url(),
        "lookback_days": get_analytics_settings().sync_lookback_days,
    }


def start_sync(actor: str, full: bool = False) -> dict:
    if not sync.configured():
        raise NotConfigured("Langfuse is not configured: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")
    store.ensure_schema()
    return {**sync_status(), **sync.start(actor, full=full)}


def summary(user: CurrentUser, kb_name: str = "", days: int = 30) -> dict:
    store.ensure_schema()
    names, accessible, owned = scope(user, kb_name)
    data = store.summary(names, days, user.user_id, owned, get_analytics_settings().recent_traces)
    types = {r["kb_name"]: r["kb_type"] for r in accessible}
    for row in data["by_kb"]:
        row["kb_type"] = types.get(row["kb_name"])
    _link(data["recent"])
    return {
        "knowledge_bases": [
            {"kb_name": r["kb_name"], "kb_type": r["kb_type"], "role": r["role"], "status": r["status"]}
            for r in accessible
        ],
        "selected": kb_name or None,
        "days": days,
        "categories": CATEGORIES,
        "sources": SOURCES,
        **data,
    }


def questions(
    user: CurrentUser,
    kb_name: str = "",
    person: str | None = None,
    source: str | None = None,
    days: int = 30,
    start: dt.date | None = None,
    end: dt.date | None = None,
    limit: int = 300,
) -> dict:
    """Questions asked (web chat, MCP agents), per person. The viewer sees their own questions everywhere and
    everyone's on the knowledge bases they own."""
    if (start is None) != (end is None):
        raise ValueError("A custom range needs both 'from' and 'to'")
    if start and end and start > end:
        raise ValueError("'from' must be on or before 'to'")
    if source and source not in SOURCES:
        raise ValueError(f"source must be one of: {', '.join(SOURCES)}")
    store.ensure_schema()
    names, _, owned = scope(user, kb_name)
    data = store.queries(names, user.user_id, owned, days, start, end, person, limit, source)
    _link(data["queries"])
    return {
        "selected": kb_name or None,
        "user": person,
        "source": source,
        "days": days,
        "from": start,
        "to": end,
        "questions_stored": get_analytics_settings().store_questions,
        **data,
    }
