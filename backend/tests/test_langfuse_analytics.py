"""Langfuse analytics plug-in (app/langfuse_analytics): record mapping, the Langfuse API client, the sync + chart
queries against Postgres, and the HTTP API.

Self-contained (no conftest fixtures). Langfuse is replaced by an httpx MockTransport serving records shaped like
the public API's. The Postgres test runs only when LANGFUSE_ANALYTICS_TEST_DSN points at a scratch database, e.g.
    LANGFUSE_ANALYTICS_TEST_DSN=postgresql://postgres:postgres@localhost:5433/analytics_test pytest -k analytics
"""

import base64
import datetime as dt
import math
import os
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.auth import CurrentUser, current_user
from app.langfuse_analytics import api, schemas, service, store, sync, transform
from app.langfuse_analytics.client import LangfuseAPIError, LangfuseReader
from app.langfuse_analytics.settings import AnalyticsSettings

NOW = dt.datetime.now(dt.UTC).replace(microsecond=0)


def _iso(delta: dt.timedelta) -> str:
    return (NOW - delta).isoformat().replace("+00:00", "Z")


# Records shaped like GET /api/public/traces and /api/public/observations, for the traces the app writes.
TRACES = [
    {  # background job: trace_context(..., "kg_extraction", kb, job_id=..., source=...)
        "id": "t-extract",
        "name": "KG Extraction",
        "timestamp": _iso(dt.timedelta(days=2)),
        "userId": "priya.nair",
        "sessionId": "job:5",
        "tags": ["graphbase", "KG Extraction"],
        "metadata": {"job_id": "5", "source": "orders.xlsx", "knowledge_base": "retail_kg", "resourceAttributes": {}},
        "input": {"operation": "kg_extraction", "knowledge_base": "retail_kg", "job_id": 5},
        "latency": 42.5,
        "totalCost": 0.012,
        "htmlPath": "/project/p1/traces/t-extract",
    },
    {  # RAG chat on a KB priya can read but doesn't own, asked by someone else
        "id": "t-rag",
        "name": "RAG Chat",
        "timestamp": _iso(dt.timedelta(days=1)),
        "userId": "arjun.mehta",
        "sessionId": "chat-9",
        "tags": ["graphbase", "RAG Chat"],
        "metadata": {"knowledge_base": '"policies_rag"'},
        "latency": 2.1,
        "totalCost": 0.003,
        "htmlPath": "/project/p1/traces/t-rag",
    },
    {  # graph chat on a local model: Langfuse has no price; KB only in the root input
        "id": "t-chat",
        "name": "Chat",
        "timestamp": _iso(dt.timedelta(hours=3)),
        "userId": "priya.nair",
        "tags": ["graphbase", "Chat"],
        "metadata": None,
        "input": {"operation": "chat", "knowledge_base": "retail_kg"},
        "latency": None,
        "totalCost": 0,
    },
    {  # multi-KB synthesis call made outside any trace_context: no knowledge base
        "id": "t-orphan",
        "name": "llm_text",
        "timestamp": _iso(dt.timedelta(hours=1)),
        "tags": ["graphbase"],
        "totalCost": 0.001,
    },
]
OBSERVATIONS = [
    {
        "id": "o1",
        "traceId": "t-extract",
        "type": "GENERATION",
        "name": "llm_json",
        "model": "gpt-4.1",
        "startTime": _iso(dt.timedelta(days=2)),
        "endTime": _iso(dt.timedelta(days=2, seconds=-20)),
        "usageDetails": {"input": 1000, "output": 200, "total": 1200},
        "costDetails": {"input": 0.002, "output": 0.0016, "total": 0.0036},
        "input": [["system", "Extract the schema."], ["human", "Question: not a chat question"]],
    },
    {
        "id": "o2",
        "traceId": "t-extract",
        "type": "GENERATION",
        "name": "llm_json",
        "model": "gpt-4.1",
        "startTime": _iso(dt.timedelta(days=2)),
        "endTime": _iso(dt.timedelta(days=2, seconds=-40)),
        "usage": {"input": 2000, "output": 500, "total": 2500},
        "calculatedTotalCost": 0.0084,
    },
    {
        "id": "o3",
        "traceId": "t-rag",
        "type": "EMBEDDING",
        "name": "embedding_query",
        "model": "text-embedding-3-large",
        "startTime": _iso(dt.timedelta(days=1)),
    },
    {
        "id": "o4",
        "traceId": "t-rag",
        "type": "GENERATION",
        "name": "llm_text",
        "model": "gpt-4.1",
        "startTime": _iso(dt.timedelta(days=1)),
        "promptTokens": 300,
        "completionTokens": 50,
        "totalTokens": 350,
        "costDetails": {"total": 0.003},
        "input": [
            ["system", "You answer questions using ONLY the numbered passages."],
            ["human", "Context:\n[1] (faq.pdf)\nQuestion: from the FAQ\n\nQuestion: What is the return window?"],
        ],
    },
    {
        "id": "o5",
        "traceId": "t-chat",
        "type": "GENERATION",
        "name": "llm_text",
        "model": "qwen2.5:7b-instruct",
        "startTime": _iso(dt.timedelta(hours=3)),
        "endTime": _iso(dt.timedelta(hours=3, seconds=-4)),
        "usageDetails": {"input": 500, "output": 100},
        "input": '[{"role": "system", "content": "You are the query planner."}, {"role": "user", "content": '
        '"Earlier in this conversation:\\nQ: list suppliers\\n\\nQuestion: Which suppliers deliver to Chennai?"}]',
    },
    {
        "id": "o6",
        "traceId": "t-chat",
        "type": "SPAN",
        "name": "Neo4j",
        "level": "ERROR",
        "statusMessage": "boom",
        "startTime": _iso(dt.timedelta(hours=3)),
    },
    {
        "id": "o7",
        "traceId": "someone-elses-trace",
        "type": "GENERATION",
        "name": "x",
        "model": "gpt-4.1",
        "startTime": _iso(dt.timedelta(hours=2)),
        "costDetails": {"total": 9.0},
    },
]
PRICES = {"qwen2.5:7b-instruct": {"input": 1.0, "output": 2.0}}  # USD per 1M tokens


def fake_langfuse(traces=TRACES, observations=OBSERVATIONS, fail_first: int = 0):
    """MockTransport for Langfuse's public API: pages by page/limit, checks basic auth, can answer 429 first."""
    calls: list[httpx.Request] = []
    expected = "Basic " + base64.b64encode(b"pk-test:sk-test").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, json={"message": "Invalid credentials"})
        if len(calls) <= fail_first:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "rate limited"})
        items = {"/api/public/traces": traces, "/api/public/observations": observations}.get(request.url.path)
        if items is None:
            return httpx.Response(404)
        page, limit = int(request.url.params["page"]), int(request.url.params["limit"])
        return httpx.Response(
            200,
            json={
                "data": items[(page - 1) * limit : page * limit],
                "meta": {
                    "page": page,
                    "limit": limit,
                    "totalItems": len(items),
                    "totalPages": max(1, math.ceil(len(items) / limit)),
                },
            },
        )

    return httpx.MockTransport(handler), calls


def reader(transport, **kwargs) -> LangfuseReader:
    return LangfuseReader("https://lf.test/", "pk-test", "sk-test", transport=transport, sleep=lambda s: None, **kwargs)


# ------------------------------------------------------------------ record mapping
@pytest.mark.parametrize(
    "name,category",
    [
        ("KG Extraction", "creation"),
        ("KG Build", "creation"),
        ("Embedding", "creation"),
        ("Hybrid Ingestion", "creation"),
        ("Add Data", "creation"),
        ("Chat", "rag"),
        ("RAG Chat", "rag"),
        ("Hybrid Chat", "rag"),
        ("multi_kb_rag_chat", "rag"),
        ("MCP ask_knowledge_base", "rag"),
        ("RAGAS Evaluation", "evaluation"),
        ("MCP whoami", "other"),
        ("llm_text", "other"),
        (None, "other"),
    ],
)
def test_category_of_trace_names(name, category):
    assert transform.category_of(name) == category


def test_trace_row_keeps_kb_and_ids_but_no_content():
    row = transform.trace_row(TRACES[0])
    assert row["kb_name"] == "retail_kg" and row["category"] == "creation"
    assert row["metadata"] == {"knowledge_base": "retail_kg", "job_id": "5", "source": "orders.xlsx"}
    assert row["latency_ms"] == 42500 and row["langfuse_cost"] == row["total_cost"] == 0.012
    assert row["trace_timestamp"].tzinfo is not None and "input" not in row and "output" not in row
    assert transform.trace_row(TRACES[1])["kb_name"] == "policies_rag"  # JSON-quoted metadata value
    assert transform.trace_row(TRACES[2])["kb_name"] == "retail_kg"  # from the root input
    assert transform.trace_row(TRACES[3])["kb_name"] is None


MCP_TRACES = [  # what app/mcp_server.py now puts in the trace input (_trace_details)
    {
        "id": "t-mcp-ask",
        "name": "MCP ask_knowledge_base",
        "timestamp": _iso(dt.timedelta(hours=2)),
        "userId": "arjun.mehta",
        "metadata": {"knowledge_base": "retail_kg"},
        "totalCost": 0.002,
        "input": {"operation": "MCP ask_knowledge_base", "knowledge_base": "retail_kg", "question": "Top supplier?"},
    },
    {
        "id": "t-mcp-cypher",
        "name": "MCP query_graph",
        "timestamp": _iso(dt.timedelta(hours=2)),
        "userId": "arjun.mehta",
        "metadata": {"knowledge_base": "retail_kg"},
        "input": {
            "operation": "MCP query_graph",
            "knowledge_base": "retail_kg",
            "cypher": "MATCH (s) RETURN s",
            "limit": 5,
        },
    },
    {
        "id": "t-mcp-search",
        "name": "MCP search_documents",
        "timestamp": _iso(dt.timedelta(hours=2)),
        "userId": "arjun.mehta",
        "metadata": {"knowledge_base": "policies_rag"},
        "input": '{"operation": "MCP search_documents", "knowledge_base": "policies_rag", "k": 3}',
    },  # content off
]


def test_trace_row_reads_what_mcp_tools_were_asked_and_the_source():
    rows = {t["id"]: transform.trace_row(t) for t in MCP_TRACES}
    assert (rows["t-mcp-ask"]["question"], rows["t-mcp-ask"]["request_kind"]) == ("Top supplier?", "question")
    assert (rows["t-mcp-cypher"]["question"], rows["t-mcp-cypher"]["request_kind"]) == ("MATCH (s) RETURN s", "cypher")
    assert rows["t-mcp-search"]["question"] is None and rows["t-mcp-search"]["request_kind"] is None
    assert {r["source"] for r in rows.values()} == {"mcp"}
    assert transform.trace_row(MCP_TRACES[0], keep_text=False)["question"] is None  # ANALYTICS_STORE_QUESTIONS=false
    assert [transform.trace_row(t)["source"] for t in TRACES] == ["job", "web", "web", "web"]
    ragas = {
        "id": "r",
        "name": "RAGAS Evaluation",
        "timestamp": _iso(dt.timedelta(hours=1)),
        "input": {"question": "Return window?", "knowledge_base": "policies_rag"},
    }
    assert (
        transform.trace_row(ragas)["source"] == "evaluation"
        and transform.trace_row(ragas)["question"] == "Return window?"
    )
    assert transform.trace_row(TRACES[0])["question"] is None  # never for KB creation, whatever the input holds


def test_observation_usage_and_cost_from_every_api_shape():
    rows = {o["id"]: transform.observation_row(o, PRICES) for o in OBSERVATIONS}
    assert (rows["o1"]["input_tokens"], rows["o1"]["output_tokens"], rows["o1"]["total_tokens"]) == (1000, 200, 1200)
    assert rows["o1"]["total_cost"] == pytest.approx(0.0036) and rows["o1"]["latency_ms"] == pytest.approx(20000)
    assert rows["o2"]["total_tokens"] == 2500 and rows["o2"]["total_cost"] == pytest.approx(0.0084)
    assert rows["o4"]["total_tokens"] == 350 and rows["o4"]["total_cost"] == pytest.approx(0.003)
    assert rows["o3"]["total_cost"] == 0 and not rows["o3"]["cost_estimated"]
    # no Langfuse price: estimated from ANALYTICS_MODEL_PRICES, total tokens derived from input + output
    assert rows["o5"]["total_tokens"] == 600 and rows["o5"]["cost_estimated"]
    assert rows["o5"]["total_cost"] == pytest.approx(500 * 1.0 / 1e6 + 100 * 2.0 / 1e6)
    assert rows["o6"]["level"] == "ERROR" and rows["o6"]["model"] is None


@pytest.mark.parametrize(
    "prompt_input,question",
    [
        ([["system", "Question: system text is ignored"], ["human", "Question: How many orders?"]], "How many orders?"),
        ([{"role": "user", "content": "Question: Late orders?\n\nYour previous query:\nMATCH (n)"}], "Late orders?"),
        (
            [["human", "Question: Top supplier?\n\nQuery used (only to understand the columns): MATCH (s)"]],
            "Top supplier?",
        ),
        ('[["human", "Context: ...\\nQuestion: Refund policy?"]]', "Refund policy?"),
        ([["human", "Question: " + "x" * 30 + "... [9,000 more characters]"]], "x" * 30 + "…"),
        ("[not captured: LANGFUSE_CAPTURE_CONTENT=false]", None),
        ([["system", "Question: only a system prompt"]], None),
        ([["human", "Extract nodes and relationships from these rows"]], None),
    ],
)
def test_question_of_reads_the_question_line_of_chat_prompts(prompt_input, question):
    assert transform.question_of({"type": "GENERATION", "input": prompt_input}) == question


def test_question_of_ignores_spans_and_embeddings():
    assert transform.question_of({"type": "SPAN", "input": "Question: no"}) is None
    assert transform.question_of({"type": "EMBEDDING", "input": [["human", "Question: no"]]}) is None


# ------------------------------------------------------------------ client
def test_reader_pages_through_results_with_basic_auth():
    transport, calls = fake_langfuse()
    with reader(transport, page_size=2) as r:
        got = list(r.traces(NOW - dt.timedelta(days=30), NOW, tag="graphbase", environment="dev"))
    assert [t["id"] for t in got] == [t["id"] for t in TRACES]
    assert len(calls) == 2 and not r.truncated
    params = calls[0].url.params
    assert params["tags"] == "graphbase" and params["environment"] == "dev" and params["orderBy"] == "timestamp.asc"
    assert params["fromTimestamp"].endswith("Z") and params["toTimestamp"].endswith("Z")


def test_reader_retries_rate_limits_and_stops_at_max_pages():
    transport, calls = fake_langfuse(fail_first=2)
    with reader(transport, page_size=2, max_pages=1) as r:
        got = list(r.observations(NOW - dt.timedelta(days=30), NOW))
    assert len(got) == 2 and r.truncated and len(calls) == 3  # two 429s, then page 1; page 2 is never fetched


def test_reader_reports_bad_keys_and_unreachable_server():
    transport, _ = fake_langfuse()
    bad = LangfuseReader("https://lf.test", "pk-x", "sk-x", transport=transport, sleep=lambda s: None)
    with pytest.raises(LangfuseAPIError, match="rejected the API keys"):
        list(bad.traces(NOW, NOW))

    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(LangfuseAPIError, match="unreachable"):
        list(reader(httpx.MockTransport(down), retries=1).traces(NOW, NOW))


# ------------------------------------------------------------------ background sync state
def test_start_runs_one_sync_at_a_time_and_reports_the_result(monkeypatch):
    release = threading.Event()

    def fake_run(actor, full):
        release.wait(5)
        return {"traces": 4, "observations": 6, "window_start": NOW, "truncated": False}

    monkeypatch.setattr(sync, "run_sync", fake_run)
    first = sync.start("priya.nair")
    assert first["running"] and first["triggered_by"] == "priya.nair"
    assert sync.start("arjun.mehta")["triggered_by"] == "priya.nair"  # second click joins the running sync
    release.set()
    for _ in range(100):
        if not sync.status()["running"]:
            break
        time.sleep(0.05)
    state = sync.status()
    assert state["ok"] and state["traces"] == 4 and "Synced 4 traces and 6 observations" in state["message"]


# ------------------------------------------------------------------ HTTP API (api.py -> service.py -> schemas.py)
WHEN = "2026-10-14T09:30:00Z"


@pytest.fixture
def client(monkeypatch):
    accessible = [
        {"kb_name": "retail_kg", "kb_type": "graph", "role": "owner", "status": "ready"},
        {"kb_name": "policies_rag", "kb_type": "rag", "role": "user", "status": "ready"},
    ]
    seen = {}

    def fake_summary(names, days, viewer, owned, limit):
        seen.update(names=names, days=days, viewer=viewer, owned=owned)
        return {
            "totals": {"traces": 1, "cost": 0.1},
            "previous": None,
            "series": [{"bucket": "2026-10-14", "rag_cost": 0.1, "traces": 1}],
            "bucket": "day",
            "by_model": [],
            "by_operation": [],
            "by_source": [{"source": "mcp", "traces": 1, "cost": 0.1, "total_tokens": 10}],
            "by_kb": [{"kb_name": "retail_kg", "cost": 0.1}],
            "recent": [
                {"id": "t1", "category": "rag", "trace_timestamp": WHEN, "html_path": "/project/p1/traces/t1"},
                {"id": "t2", "category": "rag", "trace_timestamp": WHEN, "html_path": None},
            ],
        }

    def fake_require_access(user, kb_name):
        if kb_name not in {k["kb_name"] for k in accessible}:
            raise HTTPException(403, "You do not have access to this knowledge base")

    monkeypatch.setattr(service.kb, "list_for_user", lambda user_id: accessible)
    monkeypatch.setattr(service.kb, "require_access", fake_require_access)
    monkeypatch.setattr(service.store, "ensure_schema", lambda: None)
    monkeypatch.setattr(service.store, "summary", fake_summary)
    monkeypatch.setattr(service.store, "sync_overview", lambda: {"stored_traces": 0, "stored_observations": 0})
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[current_user] = lambda: CurrentUser("priya.nair", "Priya Nair", None)
    with TestClient(app) as c:
        c.seen = seen
        yield c


def test_summary_covers_only_accessible_knowledge_bases(client):
    body = client.get("/api/analytics/summary").json()
    assert client.seen == {
        "names": ["retail_kg", "policies_rag"],
        "days": 30,
        "viewer": "priya.nair",
        "owned": ["retail_kg"],
    }
    assert body["by_kb"][0]["kb_type"] == "graph" and body["selected"] is None
    assert body["recent"][0]["url"].endswith("/project/p1/traces/t1") and body["recent"][1]["url"] is None
    assert "html_path" not in body["recent"][0]

    client.get("/api/analytics/summary", params={"kb": "policies_rag", "days": 0})
    assert client.seen["names"] == ["policies_rag"] and client.seen["days"] == 0
    assert client.get("/api/analytics/summary", params={"kb": "secret_kg"}).status_code == 403
    assert client.get("/api/analytics/summary", params={"days": -1}).status_code == 422


def test_summary_follows_the_schema_contract(client):
    body = client.get("/api/analytics/summary").json()
    assert body["api_version"] == schemas.API_VERSION
    # every field of the contract is present, with defaults where the store had nothing
    assert set(schemas.Summary.model_fields) <= set(body)
    assert set(schemas.Totals.model_fields) == set(body["totals"]) and body["totals"]["kbs_built"] == 0
    assert body["series"][0] == {
        **{k: 0 for k in schemas.SeriesPoint.model_fields},
        "bucket": "2026-10-14",
        "rag_cost": 0.1,
        "traces": 1,
    }
    assert body["sources"]["mcp"] == "MCP agents" and body["by_source"][0]["source"] == "mcp"


def test_queries_route_checks_the_custom_range_and_source(client, monkeypatch):
    seen = {}

    def fake_queries(names, viewer, owned, days, start, end, user, limit, source):
        seen.update(names=names, owned=owned, start=start, end=end, user=user, limit=limit, source=source)
        return {
            "users": [{"user_id": "arjun.mehta", "queries": 1, "cost": 0.01}],
            "sources": [{"source": "mcp", "queries": 1}],
            "totals": {"queries": 1},
            "truncated": False,
            "queries": [
                {
                    "id": "t1",
                    "user_id": "arjun.mehta",
                    "question": "MATCH (s) RETURN s",
                    "request_kind": "cypher",
                    "source": "mcp",
                    "trace_timestamp": WHEN,
                    "html_path": "/project/p1/traces/t1",
                }
            ],
        }

    monkeypatch.setattr(service.store, "queries", fake_queries)
    params = {"kb": "retail_kg", "user": "arjun.mehta", "source": "mcp", "from": "2026-10-01", "to": "2026-10-15"}
    body = client.get("/api/analytics/queries", params=params).json()
    assert seen["names"] == ["retail_kg"] and seen["owned"] == ["retail_kg"] and seen["user"] == "arjun.mehta"
    assert (seen["start"], seen["end"], seen["source"]) == (dt.date(2026, 10, 1), dt.date(2026, 10, 15), "mcp")
    assert body["queries"][0]["url"].endswith("/traces/t1") and body["queries"][0]["request_kind"] == "cypher"
    assert body["from"] == "2026-10-01" and body["to"] == "2026-10-15" and body["api_version"] == 1
    assert client.get("/api/analytics/queries", params={"from": "2026-10-01"}).status_code == 422
    assert client.get("/api/analytics/queries", params={"from": "2026-10-09", "to": "2026-10-01"}).status_code == 422
    assert client.get("/api/analytics/queries", params={"from": "10/01/2026", "to": "2026-10-02"}).status_code == 422
    assert client.get("/api/analytics/queries", params={"source": "email"}).status_code == 422
    assert client.get("/api/analytics/queries", params={"kb": "secret_kg"}).status_code == 403


def test_sync_endpoint_needs_langfuse_keys(client, monkeypatch):
    monkeypatch.setattr(service.sync, "configured", lambda: False)
    response = client.post("/api/analytics/langfuse/sync", json={"full": True})
    assert response.status_code == 409 and "LANGFUSE_PUBLIC_KEY" in response.json()["detail"]

    started = {}

    def fake_start(actor, full):
        started.update(actor=actor, full=full)
        return {"running": True, "triggered_by": actor, "full": full}

    monkeypatch.setattr(service.sync, "configured", lambda: True)
    monkeypatch.setattr(service.sync, "start", fake_start)
    response = client.post("/api/analytics/langfuse/sync", json={"full": True})
    assert response.status_code == 202 and response.json()["running"] and response.json()["configured"]
    assert started == {"actor": "priya.nair", "full": True}


# ------------------------------------------------------------------ Postgres: sync + chart queries
DSN = os.environ.get("LANGFUSE_ANALYTICS_TEST_DSN")


@pytest.fixture
def scratch_db(monkeypatch):
    if not DSN:
        pytest.skip("set LANGFUSE_ANALYTICS_TEST_DSN to a scratch Postgres database")
    import psycopg
    from psycopg.rows import dict_row

    @contextmanager
    def get_conn():
        with psycopg.connect(DSN, row_factory=dict_row) as conn:
            yield conn

    monkeypatch.setattr(store, "get_conn", get_conn)
    monkeypatch.setattr(store, "_ready", False)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS langfuse_observations, langfuse_traces")
    yield get_conn
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS langfuse_observations, langfuse_traces")
        conn.execute("DROP FUNCTION IF EXISTS langfuse_analytics_touch()")


def test_sync_stores_traces_and_charts_add_up(scratch_db, monkeypatch):
    monkeypatch.setattr(sync, "get_analytics_settings", lambda: AnalyticsSettings(model_prices=PRICES))
    transport, _ = fake_langfuse()
    result = sync.run_sync("priya.nair", reader=reader(transport, page_size=3), now=NOW)
    assert result["traces"] == 4 and result["observations"] == 6  # o7 belongs to a trace that isn't ours
    assert result["window_start"] == NOW - dt.timedelta(days=90)

    with scratch_db() as conn:
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM langfuse_traces").fetchall()}
        assert conn.execute("SELECT count(*) AS n FROM langfuse_observations").fetchone()["n"] == 6
    assert rows["t-extract"]["total_tokens"] == 3700 and rows["t-extract"]["generation_count"] == 2
    assert float(rows["t-chat"]["total_cost"]) == pytest.approx(0.0007)  # estimate beats Langfuse's 0
    assert rows["t-chat"]["error_count"] == 1 and rows["t-chat"]["latency_ms"] == pytest.approx(4000)
    assert rows["t-extract"]["modified_by"] == "priya.nair" and rows["t-orphan"]["kb_name"] is None
    assert rows["t-rag"]["question"] == "What is the return window?"
    assert rows["t-chat"]["question"] == "Which suppliers deliver to Chennai?"
    assert rows["t-extract"]["question"] is None  # KB creation prompts are never stored as questions

    # incremental re-sync: window starts at the newest trace minus the overlap; nothing is duplicated
    again = sync.run_sync("system", reader=reader(fake_langfuse()[0]), now=NOW)
    assert again["window_start"] == NOW - dt.timedelta(hours=1) - dt.timedelta(hours=6)
    with scratch_db() as conn:
        assert conn.execute("SELECT count(*) AS n FROM langfuse_traces").fetchone()["n"] == 4
        updated = conn.execute("SELECT updated_at > created_at AS touched FROM langfuse_traces WHERE id = 't-extract'")
        assert updated.fetchone()["touched"]  # trigger maintains updated_at

    data = store.summary(["retail_kg", "policies_rag"], 30, "priya.nair", ["retail_kg"], 10)
    t = data["totals"]
    assert t["traces"] == 3 and t["error_traces"] == 1 and t["rag_traces"] == 2
    assert t["creation_cost"] == pytest.approx(0.012) and t["rag_cost"] == pytest.approx(0.0037)
    assert t["cost"] == pytest.approx(0.0157) and t["total_tokens"] == 3700 + 350 + 600
    assert len(data["series"]) == 30 and data["bucket"] == "day"
    assert sum(r["creation_cost"] + r["rag_cost"] for r in data["series"]) == pytest.approx(0.0157)
    assert {r["kb_name"] for r in data["by_kb"]} == {"retail_kg", "policies_rag"}
    assert data["by_operation"][0]["operation"] == "KG Extraction"
    models = {(m["model"], m["type"]): m for m in data["by_model"]}
    assert models[("qwen2.5:7b-instruct", "GENERATION")]["estimated"]
    assert models[("gpt-4.1", "GENERATION")]["calls"] == 3
    users = {r["id"]: r["user_id"] for r in data["recent"]}
    assert users == {"t-chat": "priya.nair", "t-rag": None, "t-extract": "priya.nair"}  # others' only on owned KBs

    assert {r["source"]: r["traces"] for r in data["by_source"]} == {"job": 1, "web": 2}
    assert t["kbs_built"] == 1 and t["active_kbs"] == 2 and t["creation_tokens"] == 3700 and t["rag_tokens"] == 950
    only_rag = store.summary(["policies_rag"], 0, "arjun.mehta", [], 10)
    assert only_rag["totals"]["traces"] == 1 and only_rag["recent"][0]["user_id"] == "arjun.mehta"
    assert store.summary([], 30, "priya.nair", [], 10)["totals"] == {}
    assert data["previous"]["traces"] == 0 and only_rag["previous"] is None  # days 31-60 ago: nothing; all time: n/a

    # Questions by user: own questions everywhere, everyone's only on owned knowledge bases
    kbs = ["retail_kg", "policies_rag"]
    mine = store.queries(kbs, "priya.nair", ["retail_kg"], 30, None, None, None, 50)
    assert [u["user_id"] for u in mine["users"]] == ["priya.nair"]  # arjun's question is on a KB priya doesn't own
    assert [q["question"] for q in mine["queries"]] == ["Which suppliers deliver to Chennai?"]
    assert mine["totals"]["queries"] == 1 and mine["totals"]["total_tokens"] == 600 and not mine["truncated"]
    owner = store.queries(kbs, "lead", ["retail_kg", "policies_rag"], 30, None, None, None, 1)
    assert {u["user_id"] for u in owner["users"]} == {"priya.nair", "arjun.mehta"}
    assert owner["totals"]["queries"] == 2 and len(owner["queries"]) == 1 and owner["truncated"]
    one = store.queries(kbs, "lead", kbs, 30, None, None, "arjun.mehta", 50)
    assert [q["id"] for q in one["queries"]] == ["t-rag"] and len(one["users"]) == 2  # list of people stays whole
    assert one["queries"][0]["total_cost"] == pytest.approx(0.003)
    today = NOW.date()
    custom = store.queries(kbs, "lead", kbs, 30, today - dt.timedelta(days=5), today + dt.timedelta(days=1), None, 50)
    assert custom["totals"]["queries"] == 2
    old = today - dt.timedelta(days=20)
    assert store.queries(kbs, "lead", kbs, 30, old, old, None, 50)["totals"]["queries"] == 0
    assert store.queries([], "lead", [], 30, None, None, None, 50)["queries"] == []
    assert {s["source"]: s["queries"] for s in owner["sources"]} == {"web": 2}

    # MCP agents: question / Cypher from the trace input; web questions survive a re-sync (kept, not overwritten)
    sync.run_sync("system", reader=reader(fake_langfuse(TRACES + MCP_TRACES, OBSERVATIONS)[0]), now=NOW, full=True)
    mcp = store.queries(kbs, "lead", kbs, 30, None, None, None, 50, "mcp")
    by_id = {q["id"]: q for q in mcp["queries"]}
    assert set(by_id) == {"t-mcp-ask", "t-mcp-cypher", "t-mcp-search"} and mcp["users"][0]["user_id"] == "arjun.mehta"
    assert (by_id["t-mcp-cypher"]["question"], by_id["t-mcp-cypher"]["request_kind"]) == (
        "MATCH (s) RETURN s",
        "cypher",
    )
    assert by_id["t-mcp-search"]["question"] is None and by_id["t-mcp-ask"]["source"] == "mcp"
    both = store.queries(kbs, "lead", kbs, 30, None, None, None, 50)
    assert {s["source"]: s["queries"] for s in both["sources"]} == {"web": 2, "mcp": 3}
    assert {q["id"]: q["question"] for q in both["queries"]}["t-chat"] == "Which suppliers deliver to Chennai?"
    web = store.queries(kbs, "lead", kbs, 30, None, None, None, 50, "web")
    assert {q["source"] for q in web["queries"]} == {"web"} and web["totals"]["queries"] == 2
