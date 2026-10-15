"""HTTP routes of the plug-in, under /api/analytics: parameters in, schemas out. The response format lives in
schemas.py, the rules in service.py; this file only translates HTTP. Every number is limited to knowledge bases the
signed-in user owns or was granted (service.scope -> kb.list_for_user / kb.require_access)."""

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth import CurrentUser, current_user
from app.langfuse_analytics import schemas, service

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


class SyncBody(BaseModel):
    full: bool = False  # re-read the whole ANALYTICS_SYNC_LOOKBACK_DAYS window instead of only what's new


@router.get("/langfuse/status", response_model=schemas.SyncStatus)
def sync_status(user: CurrentUser = Depends(current_user)):
    return service.sync_status()


@router.post("/langfuse/sync", status_code=202, response_model=schemas.SyncStatus)
def start_sync(body: SyncBody | None = None, user: CurrentUser = Depends(current_user)):
    """Fetch new traces from Langfuse into langfuse_traces / langfuse_observations (runs in the background)."""
    try:
        return service.start_sync(user.user_id, full=bool(body and body.full))
    except service.NotConfigured as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/summary", response_model=schemas.Summary)
def analytics_summary(
    kb_name: str = Query("", alias="kb", description="One knowledge base; empty = all the user can access"),
    days: int = Query(30, ge=0, le=3650, description="Whole days up to today; 0 = all time"),
    user: CurrentUser = Depends(current_user),
):
    return service.summary(user, kb_name, days)


@router.get("/queries", response_model=schemas.Questions)
def questions_by_user(
    kb_name: str = Query("", alias="kb", description="One knowledge base; empty = all the user can access"),
    person: str = Query("", alias="user", description="One person; empty = everyone the viewer may see"),
    source: str = Query("", description="web | mcp; empty = both"),
    days: int = Query(30, ge=0, le=3650, description="Whole days up to today; 0 = all time (ignored with from/to)"),
    start: dt.date | None = Query(None, alias="from", description="Custom range start (YYYY-MM-DD, inclusive)"),
    end: dt.date | None = Query(None, alias="to", description="Custom range end (YYYY-MM-DD, inclusive)"),
    limit: int = Query(300, ge=1, le=1000),
    user: CurrentUser = Depends(current_user),
):
    """Questions asked (web chat, MCP agents), per person: who asked what, when, and its cost and tokens."""
    try:
        return service.questions(user, kb_name, person.strip() or None, source.strip() or None, days, start, end, limit)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
