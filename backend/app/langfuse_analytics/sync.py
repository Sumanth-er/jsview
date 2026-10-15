"""Langfuse -> Postgres sync behind the Analytics tab's "Sync from Langfuse" button.

One sync at a time, in a background thread (the button returns at once; the UI polls the status). Window:
- first sync, or a full re-sync: the last ANALYTICS_SYNC_LOOKBACK_DAYS;
- otherwise from the newest stored trace minus ANALYTICS_SYNC_OVERLAP_HOURS, because Langfuse fills in costs and late
  observations after a trace starts. Rows are upserted, so re-reading a window is harmless.
"""

import datetime as dt
import logging
import threading

from app.config import get_settings
from app.langfuse_analytics import store, transform
from app.langfuse_analytics.client import LangfuseAPIError, LangfuseReader
from app.langfuse_analytics.settings import get_analytics_settings

log = logging.getLogger(__name__)

BATCH_SIZE = 200
_lock = threading.Lock()
_state: dict = {
    "running": False,
    "ok": None,
    "message": None,
    "started_at": None,
    "finished_at": None,
    "triggered_by": None,
    "full": False,
    "window_start": None,
    "traces": 0,
    "observations": 0,
    "truncated": False,
}


def configured() -> bool:
    s = get_settings()
    return bool(s.langfuse_public_key and s.langfuse_secret_key)


def status() -> dict:
    with _lock:
        return dict(_state)


def _update(**fields) -> None:
    with _lock:
        _state.update(fields)


def start(actor: str, full: bool = False) -> dict:
    """Start a background sync unless one is running; returns the current state either way."""
    with _lock:
        if _state["running"]:
            return dict(_state)
        _state.update(
            running=True, ok=None, message="Syncing from Langfuse…", started_at=_now(), finished_at=None,
            triggered_by=actor, full=full, window_start=None, traces=0, observations=0, truncated=False,
        )  # fmt: skip
        snapshot = dict(_state)
    threading.Thread(target=_run, args=(actor, full), name="langfuse-analytics-sync", daemon=True).start()
    return snapshot


def _run(actor: str, full: bool) -> None:
    try:
        result = run_sync(actor, full)
    except LangfuseAPIError as exc:
        _update(running=False, ok=False, message=str(exc), finished_at=_now())
        return
    except Exception as exc:  # noqa: BLE001 - report it in the UI instead of losing it in a thread
        log.exception("Langfuse analytics sync failed")
        _update(running=False, ok=False, message=f"Sync failed ({type(exc).__name__}: {exc})"[:500], finished_at=_now())
        return
    note = " (stopped at ANALYTICS_SYNC_MAX_PAGES; sync again for the rest)" if result["truncated"] else ""
    _update(
        running=False, ok=True, finished_at=_now(), **result,
        message=f"Synced {result['traces']:,} traces and {result['observations']:,} observations{note}",
    )  # fmt: skip


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def run_sync(actor: str, full: bool = False, reader: LangfuseReader | None = None, now: dt.datetime | None = None):
    """Fetch the window's traces and observations and upsert them. Synchronous (the thread and tests call it)."""
    s, a = get_settings(), get_analytics_settings()
    store.ensure_schema()
    until = now or _now()
    latest = None if full else store.latest_trace_time()
    if latest is None:
        since = until - dt.timedelta(days=a.sync_lookback_days)
    else:
        since = min(latest, until) - dt.timedelta(hours=a.sync_overlap_hours)
    _update(window_start=since)
    own_reader = reader is None
    reader = reader or LangfuseReader(
        s.langfuse_base_url,
        s.langfuse_public_key,
        s.langfuse_secret_key,
        timeout=a.request_timeout_seconds,
        retries=a.request_retries,
        page_size=a.sync_page_size,
        max_pages=a.sync_max_pages,
    )
    environment = s.langfuse_environment or None
    touched: set[str] = set()
    questions: dict[str, tuple[str, str]] = {}  # trace id -> (start time, question) of its earliest LLM prompt
    n_traces = n_observations = 0
    try:
        batch: list[dict] = []
        for trace in reader.traces(since, until, tag=a.sync_tag or None, environment=environment):
            if trace.get("id") and transform.parse_time(trace.get("timestamp")):
                batch.append(transform.trace_row(trace, keep_text=a.store_questions))
            if len(batch) >= BATCH_SIZE:
                touched.update(store.upsert_traces(batch, actor))
                n_traces, batch = n_traces + len(batch), []
                _update(traces=n_traces)
        touched.update(store.upsert_traces(batch, actor))
        n_traces += len(batch)
        _update(traces=n_traces)

        batch = []
        for observation in reader.observations(since, until, environment=environment):
            if observation.get("id") and observation.get("traceId"):
                batch.append(transform.observation_row(observation, a.model_prices))
                question = transform.question_of(observation) if a.store_questions else None
                if question:
                    started = str(observation.get("startTime") or "")
                    earliest = questions.get(observation["traceId"])
                    if earliest is None or started < earliest[0]:
                        questions[observation["traceId"]] = (started, question)
            if len(batch) >= BATCH_SIZE:
                kept = store.upsert_observations(batch, actor)
                touched.update(r["trace_id"] for r in kept)
                n_observations, batch = n_observations + len(kept), []
                _update(observations=n_observations)
        kept = store.upsert_observations(batch, actor)
        touched.update(r["trace_id"] for r in kept)
        n_observations += len(kept)
        _update(observations=n_observations)

        store.refresh_trace_totals(touched, actor)
        store.set_questions({trace_id: q for trace_id, (_, q) in questions.items()}, actor)
    finally:
        if own_reader:
            reader.close()
    log.info(
        "Langfuse analytics sync by %s: %d traces, %d observations since %s", actor, n_traces, n_observations, since
    )
    return {"traces": n_traces, "observations": n_observations, "window_start": since, "truncated": reader.truncated}
