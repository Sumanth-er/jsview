"""Postgres side of the plug-in: its two tables (schema.sql), the sync upserts and the chart queries."""

import datetime as dt
import threading
from decimal import Decimal
from pathlib import Path

from psycopg.types.json import Jsonb

from app.db import get_conn

SCHEMA_SQL = Path(__file__).with_name("schema.sql")
SCHEMA_LOCK = 72_011_090  # advisory lock while creating the tables (app migrations use 72011001)

_ready = False
_ready_lock = threading.Lock()

TRACE_COLUMNS = (
    "id", "name", "category", "kb_name", "user_id", "session_id", "environment", "tags", "metadata",
    "trace_timestamp", "latency_ms", "langfuse_cost", "total_cost", "html_path", "source", "question", "request_kind",
)  # fmt: skip
# kept when a re-sync brings no value (web chat questions come from prompts after the trace upsert)
_KEEP_IF_NULL = {"question", "request_kind"}
OBSERVATION_COLUMNS = (
    "id", "trace_id", "parent_observation_id", "type", "name", "model", "level", "status_message", "start_time",
    "end_time", "latency_ms", "input_tokens", "output_tokens", "total_tokens", "input_cost", "output_cost",
    "total_cost", "cost_estimated",
)  # fmt: skip
_JSON_COLUMNS = {"tags", "metadata"}

# Traces in the selected window: days = 0 means all time, otherwise whole days up to today.
_WINDOW = (
    "(%(days)s::int = 0 OR t.trace_timestamp >= date_trunc('day', now()) - make_interval(days => %(days)s::int - 1))"
)
_SCOPE = f"t.kb_name = ANY(%(kbs)s) AND {_WINDOW}"
_BY_CATEGORY = """coalesce(sum(t.total_cost) FILTER (WHERE t.category = 'creation'), 0) AS creation_cost,
                  coalesce(sum(t.total_cost) FILTER (WHERE t.category = 'rag'), 0) AS rag_cost,
                  coalesce(sum(t.total_cost) FILTER (WHERE t.category = 'evaluation'), 0) AS evaluation_cost,
                  coalesce(sum(t.total_cost) FILTER (WHERE t.category = 'other'), 0) AS other_cost"""


def ensure_schema() -> None:
    """Create the plug-in's tables once per process (idempotent, safe with several workers)."""
    global _ready
    if _ready:
        return
    with _ready_lock:
        if _ready:
            return
        with get_conn() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
            conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        _ready = True


def _plain(row: dict) -> dict:
    return {k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()}


def _upsert_sql(table: str, columns: tuple) -> str:
    updates = ", ".join(
        f"{c} = coalesce(EXCLUDED.{c}, {table}.{c})" if c in _KEEP_IF_NULL else f"{c} = EXCLUDED.{c}"
        for c in columns
        if c != "id"
    )
    return f"""INSERT INTO {table} ({", ".join(columns)}, modified_by, synced_at)
               VALUES ({", ".join(["%s"] * len(columns))}, %s, now())
               ON CONFLICT (id) DO UPDATE SET {updates}, modified_by = EXCLUDED.modified_by, synced_at = now()"""


def _values(row: dict, columns: tuple, actor: str) -> tuple:
    return tuple(Jsonb(row[c]) if c in _JSON_COLUMNS else row[c] for c in columns) + (actor,)


# ------------------------------------------------------------------ sync writes
def latest_trace_time() -> dt.datetime | None:
    with get_conn() as conn:
        return conn.execute("SELECT max(trace_timestamp) AS latest FROM langfuse_traces").fetchone()["latest"]


def upsert_traces(rows: list[dict], actor: str) -> list[str]:
    if not rows:
        return []
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(_upsert_sql("langfuse_traces", TRACE_COLUMNS), [_values(r, TRACE_COLUMNS, actor) for r in rows])
    return [r["id"] for r in rows]


def upsert_observations(rows: list[dict], actor: str) -> list[dict]:
    """Store observations whose trace is stored (others belong to another app or are outside the window)."""
    if not rows:
        return []
    with get_conn() as conn:
        known = {
            r["id"]
            for r in conn.execute(
                "SELECT id FROM langfuse_traces WHERE id = ANY(%s)", (list({r["trace_id"] for r in rows}),)
            ).fetchall()
        }
        kept = [r for r in rows if r["trace_id"] in known]
        if kept:
            with conn.cursor() as cur:
                cur.executemany(
                    _upsert_sql("langfuse_observations", OBSERVATION_COLUMNS),
                    [_values(r, OBSERVATION_COLUMNS, actor) for r in kept],
                )
    return kept


def refresh_trace_totals(trace_ids: set[str] | list[str], actor: str) -> None:
    """Roll the stored observations up into their traces: tokens, call / error counts, cost (incl. estimates)."""
    if not trace_ids:
        return
    with get_conn() as conn:
        conn.execute(
            """UPDATE langfuse_traces t SET
                   input_tokens = a.input_tokens, output_tokens = a.output_tokens, total_tokens = a.total_tokens,
                   observation_count = a.observations, generation_count = a.generations, error_count = a.errors,
                   total_cost = GREATEST(t.langfuse_cost, a.cost),
                   latency_ms = coalesce(t.latency_ms, a.latency_ms),
                   modified_by = %(actor)s
               FROM (SELECT trace_id,
                            sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
                            sum(total_tokens) AS total_tokens, count(*) AS observations,
                            count(*) FILTER (WHERE type IN ('GENERATION', 'EMBEDDING')) AS generations,
                            count(*) FILTER (WHERE upper(level) = 'ERROR') AS errors,
                            sum(total_cost) AS cost,
                            extract(epoch FROM max(end_time) - min(start_time)) * 1000 AS latency_ms
                     FROM langfuse_observations WHERE trace_id = ANY(%(ids)s) GROUP BY trace_id) a
               WHERE t.id = a.trace_id""",
            {"ids": list(trace_ids), "actor": actor},
        )


def set_questions(questions: dict[str, str], actor: str) -> None:
    """Store the question of chat traces whose input didn't carry one (web chat), from their first LLM prompt."""
    if not questions:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            """UPDATE langfuse_traces SET question = %s, request_kind = 'question', modified_by = %s
               WHERE id = %s AND category IN ('rag', 'evaluation') AND question IS NULL""",
            [(q, actor, trace_id) for trace_id, q in questions.items()],
        )


# ------------------------------------------------------------------ reads
def sync_overview() -> dict:
    with get_conn() as conn:
        traces = conn.execute(
            """SELECT count(*) AS stored_traces, max(synced_at) AS last_synced_at,
                      min(trace_timestamp) AS first_trace_at, max(trace_timestamp) AS last_trace_at
               FROM langfuse_traces"""
        ).fetchone()
        observations = conn.execute("SELECT count(*) AS stored_observations FROM langfuse_observations").fetchone()
    return {**traces, **observations}


def _buckets(conn, params: dict) -> tuple[str, dt.datetime]:
    """Day buckets for up to 120 days, week buckets beyond (all time over a long history)."""
    if params["days"]:
        start = conn.execute(
            "SELECT date_trunc('day', now()) - make_interval(days => %(days)s::int - 1) AS start", params
        ).fetchone()["start"]
        return ("day" if params["days"] <= 120 else "week"), start
    row = conn.execute(
        "SELECT min(t.trace_timestamp) AS first, now() AS now FROM langfuse_traces t WHERE t.kb_name = ANY(%(kbs)s)",
        params,
    ).fetchone()
    start = row["first"] or row["now"]
    return ("day" if (row["now"] - start).days <= 120 else "week"), start


def summary(kb_names: list[str], days: int, viewer: str, owned: list[str], recent_limit: int) -> dict:
    """Everything the Analytics tab shows, for traces of `kb_names` in the window (days = 0: all time)."""
    empty = {
        "totals": {}, "previous": None, "series": [], "bucket": "day",
        "by_kb": [], "by_operation": [], "by_model": [], "by_source": [], "recent": [],
    }  # fmt: skip
    if not kb_names:
        return empty
    params = {"kbs": kb_names, "days": days, "viewer": viewer, "owned": owned, "limit": recent_limit}
    with get_conn() as conn:
        # the same number of days just before the window, for the tiles' "vs previous period"
        previous = (
            conn.execute(
                f"""SELECT count(*) AS traces, coalesce(sum(t.total_cost), 0) AS cost, {_BY_CATEGORY},
                           count(*) FILTER (WHERE t.category = 'rag') AS rag_traces,
                           coalesce(sum(t.total_tokens), 0)::bigint AS total_tokens,
                           avg(t.latency_ms) FILTER (WHERE t.category = 'rag') AS avg_rag_latency_ms
                    FROM langfuse_traces t WHERE t.kb_name = ANY(%(kbs)s)
                      AND t.trace_timestamp >= date_trunc('day', now()) - make_interval(days => 2 * %(days)s::int - 1)
                      AND t.trace_timestamp < date_trunc('day', now()) - make_interval(days => %(days)s::int - 1)""",
                params,
            ).fetchone()
            if days
            else None
        )
        totals = conn.execute(
            f"""SELECT count(*) AS traces, coalesce(sum(t.total_cost), 0) AS cost, {_BY_CATEGORY},
                       count(*) FILTER (WHERE t.category = 'creation') AS creation_traces,
                       count(*) FILTER (WHERE t.category = 'rag') AS rag_traces,
                       count(*) FILTER (WHERE t.category = 'evaluation') AS evaluation_traces,
                       count(*) FILTER (WHERE t.category = 'other') AS other_traces,
                       coalesce(sum(t.total_tokens) FILTER (WHERE t.category = 'creation'), 0)::bigint
                           AS creation_tokens,
                       coalesce(sum(t.total_tokens) FILTER (WHERE t.category = 'rag'), 0)::bigint AS rag_tokens,
                       coalesce(sum(t.total_tokens) FILTER (WHERE t.category = 'evaluation'), 0)::bigint
                           AS evaluation_tokens,
                       coalesce(sum(t.total_tokens) FILTER (WHERE t.category = 'other'), 0)::bigint AS other_tokens,
                       count(DISTINCT t.kb_name) AS active_kbs,
                       count(DISTINCT t.kb_name) FILTER (WHERE t.category = 'creation') AS kbs_built,
                       coalesce(sum(t.input_tokens), 0)::bigint AS input_tokens,
                       coalesce(sum(t.output_tokens), 0)::bigint AS output_tokens,
                       coalesce(sum(t.total_tokens), 0)::bigint AS total_tokens,
                       coalesce(sum(t.generation_count), 0)::bigint AS model_calls,
                       avg(t.latency_ms) FILTER (WHERE t.category = 'rag') AS avg_rag_latency_ms,
                       count(*) FILTER (WHERE t.error_count > 0) AS error_traces,
                       count(DISTINCT t.user_id) AS users
                FROM langfuse_traces t WHERE {_SCOPE}""",
            params,
        ).fetchone()
        unit, start = _buckets(conn, params)
        series = conn.execute(
            f"""WITH buckets AS (
                    SELECT generate_series(date_trunc(%(unit)s, %(start)s::timestamptz), date_trunc(%(unit)s, now()),
                                           %(step)s::interval) AS bucket)
                SELECT b.bucket::date AS bucket, {_BY_CATEGORY}, count(t.id) AS traces,
                       coalesce(sum(t.input_tokens), 0)::bigint AS input_tokens,
                       coalesce(sum(t.output_tokens), 0)::bigint AS output_tokens,
                       coalesce(sum(t.total_tokens), 0)::bigint AS total_tokens
                FROM buckets b
                LEFT JOIN langfuse_traces t ON date_trunc(%(unit)s, t.trace_timestamp) = b.bucket AND {_SCOPE}
                GROUP BY b.bucket ORDER BY b.bucket""",
            {**params, "unit": unit, "start": start, "step": f"1 {unit}"},
        ).fetchall()
        by_kb = conn.execute(
            f"""SELECT t.kb_name, count(*) AS traces, coalesce(sum(t.total_cost), 0) AS cost, {_BY_CATEGORY},
                       count(*) FILTER (WHERE t.category = 'rag') AS rag_traces,
                       coalesce(sum(t.total_tokens), 0)::bigint AS total_tokens, max(t.trace_timestamp) AS last_activity
                FROM langfuse_traces t WHERE {_SCOPE} GROUP BY t.kb_name ORDER BY cost DESC, traces DESC""",
            params,
        ).fetchall()
        by_operation = conn.execute(
            f"""SELECT coalesce(t.name, '(unnamed)') AS operation, t.category, count(*) AS traces,
                       coalesce(sum(t.total_cost), 0) AS cost, coalesce(sum(t.total_tokens), 0)::bigint AS total_tokens,
                       avg(t.latency_ms) AS avg_latency_ms
                FROM langfuse_traces t WHERE {_SCOPE}
                GROUP BY 1, 2 ORDER BY cost DESC, traces DESC""",
            params,
        ).fetchall()
        by_source = conn.execute(
            f"""SELECT coalesce(t.source, 'web') AS source, count(*) AS traces,
                       coalesce(sum(t.total_cost), 0) AS cost, coalesce(sum(t.total_tokens), 0)::bigint AS total_tokens
                FROM langfuse_traces t WHERE {_SCOPE} GROUP BY 1 ORDER BY cost DESC, traces DESC""",
            params,
        ).fetchall()
        by_model = conn.execute(
            f"""SELECT o.model, o.type, count(*) AS calls,
                       coalesce(sum(o.input_tokens), 0)::bigint AS input_tokens,
                       coalesce(sum(o.output_tokens), 0)::bigint AS output_tokens,
                       coalesce(sum(o.total_tokens), 0)::bigint AS total_tokens,
                       coalesce(sum(o.total_cost), 0) AS cost, bool_or(o.cost_estimated) AS estimated
                FROM langfuse_observations o JOIN langfuse_traces t ON t.id = o.trace_id
                WHERE o.model IS NOT NULL AND o.model <> '' AND {_SCOPE}
                GROUP BY o.model, o.type ORDER BY cost DESC, calls DESC""",
            params,
        ).fetchall()
        recent = conn.execute(
            f"""SELECT t.id, t.name, t.category, t.kb_name, t.trace_timestamp, t.total_cost, t.total_tokens,
                       t.latency_ms, t.error_count, t.html_path,
                       CASE WHEN t.user_id = %(viewer)s OR t.kb_name = ANY(%(owned)s) THEN t.user_id END AS user_id
                FROM langfuse_traces t WHERE {_SCOPE}
                ORDER BY t.trace_timestamp DESC LIMIT %(limit)s""",
            params,
        ).fetchall()
    return {
        "totals": _plain(totals),
        "previous": _plain(previous) if previous else None,
        "series": [_plain(r) for r in series],
        "bucket": unit,
        "by_kb": [_plain(r) for r in by_kb],
        "by_operation": [_plain(r) for r in by_operation],
        "by_model": [_plain(r) for r in by_model],
        "by_source": [_plain(r) for r in by_source],
        "recent": [_plain(r) for r in recent],
    }


# Questions by user: chat / RAG / MCP question traces the viewer may see per person: their own anywhere, and
# everyone's on the knowledge bases they own (the same rule as user names in "Recent traces").
_QUESTION_SCOPE = """t.kb_name = ANY(%(kbs)s) AND t.category = 'rag' AND t.user_id IS NOT NULL
                     AND (t.user_id = %(viewer)s OR t.kb_name = ANY(%(owned)s))"""
# custom range: whole days from `start` to `end` inclusive (the database's calendar days)
_CUSTOM_RANGE = "t.trace_timestamp >= %(start)s::date AND t.trace_timestamp < %(end)s::date + 1"


def queries(
    kb_names: list[str],
    viewer: str,
    owned: list[str],
    days: int,
    start: dt.date | None,
    end: dt.date | None,
    user: str | None,
    limit: int,
    source: str | None = None,
) -> dict:
    """Per-user totals plus the newest `limit` questions (with text, cost, tokens) in the window or custom range."""
    totals_empty = {"queries": 0, "cost": 0.0, "total_tokens": 0, "input_tokens": 0, "output_tokens": 0, "users": 0}
    if not kb_names:
        return {"users": [], "sources": [], "totals": totals_empty, "queries": [], "truncated": False}
    params = {
        "kbs": kb_names, "viewer": viewer, "owned": owned, "days": days, "start": start, "end": end,
        "user": user, "limit": limit, "source": source,
    }  # fmt: skip
    in_range = f"{_QUESTION_SCOPE} AND {_CUSTOM_RANGE if start else _WINDOW}"
    scope = f"{in_range} AND coalesce(t.source, 'web') = %(source)s" if source else in_range
    picked = f"{scope} AND t.user_id = %(user)s" if user else scope
    with get_conn() as conn:
        sources = conn.execute(
            f"""SELECT coalesce(t.source, 'web') AS source, count(*) AS queries, coalesce(sum(t.total_cost), 0) AS cost
                FROM langfuse_traces t WHERE {in_range}{" AND t.user_id = %(user)s" if user else ""}
                GROUP BY 1 ORDER BY queries DESC""",
            params,
        ).fetchall()
        users = conn.execute(
            f"""SELECT t.user_id, count(*) AS queries, coalesce(sum(t.total_cost), 0) AS cost,
                       coalesce(sum(t.total_tokens), 0)::bigint AS total_tokens,
                       avg(t.latency_ms) AS avg_latency_ms, max(t.trace_timestamp) AS last_at
                FROM langfuse_traces t WHERE {scope}
                GROUP BY t.user_id ORDER BY cost DESC, queries DESC, t.user_id""",
            params,
        ).fetchall()
        totals = conn.execute(
            f"""SELECT count(*) AS queries, coalesce(sum(t.total_cost), 0) AS cost,
                       coalesce(sum(t.total_tokens), 0)::bigint AS total_tokens,
                       coalesce(sum(t.input_tokens), 0)::bigint AS input_tokens,
                       coalesce(sum(t.output_tokens), 0)::bigint AS output_tokens,
                       avg(t.latency_ms) AS avg_latency_ms, count(DISTINCT t.user_id) AS users,
                       min(t.trace_timestamp) AS first_at, max(t.trace_timestamp) AS last_at
                FROM langfuse_traces t WHERE {picked}""",
            params,
        ).fetchone()
        rows = conn.execute(
            f"""SELECT t.id, t.user_id, t.question, t.request_kind, coalesce(t.source, 'web') AS source, t.kb_name,
                       t.name, t.trace_timestamp, t.total_cost,
                       t.input_tokens, t.output_tokens, t.total_tokens, t.latency_ms, t.error_count, t.html_path
                FROM langfuse_traces t WHERE {picked}
                ORDER BY t.trace_timestamp DESC LIMIT %(limit)s""",
            params,
        ).fetchall()
    totals = _plain(totals)
    return {
        "users": [_plain(r) for r in users],
        "sources": [_plain(r) for r in sources],
        "totals": totals,
        "queries": [_plain(r) for r in rows],
        "truncated": totals["queries"] > len(rows),
    }
