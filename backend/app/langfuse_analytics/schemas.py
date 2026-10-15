"""The plug-in's API contract: the exact JSON every /api/analytics route returns.

This file is the only place the response format is defined. Routes declare these models as response_model, so
FastAPI validates every response against them and documents them in /docs (OpenAPI). To change the format later,
change it here and in frontend/src/analytics/client.js (the only frontend file that reads it); store.py (SQL) and
service.py (access rules) stay as they are. Bump API_VERSION when a change is not backwards compatible.
"""

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field

API_VERSION = 1


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


# ------------------------------------------------------------------ sync
class SyncStatus(_Model):
    api_version: int = API_VERSION
    configured: bool
    running: bool = False
    ok: bool | None = None
    message: str | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    triggered_by: str | None = None
    full: bool = False
    window_start: dt.datetime | None = None
    traces: int = 0  # synced by the current / last run
    observations: int = 0
    truncated: bool = False
    stored_traces: int = 0  # in the tables
    stored_observations: int = 0
    last_synced_at: dt.datetime | None = None
    first_trace_at: dt.datetime | None = None
    last_trace_at: dt.datetime | None = None
    langfuse_url: str
    lookback_days: int


# ------------------------------------------------------------------ summary (the charts)
class KnowledgeBaseRef(_Model):
    kb_name: str
    kb_type: str
    role: str
    status: str | None = None


class Totals(_Model):
    traces: int = 0
    cost: float = 0.0
    creation_cost: float = 0.0
    rag_cost: float = 0.0
    evaluation_cost: float = 0.0
    other_cost: float = 0.0
    creation_traces: int = 0
    rag_traces: int = 0
    evaluation_traces: int = 0
    other_traces: int = 0
    creation_tokens: int = 0
    rag_tokens: int = 0
    evaluation_tokens: int = 0
    other_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0
    avg_rag_latency_ms: float | None = None
    error_traces: int = 0
    users: int = 0
    active_kbs: int = 0  # knowledge bases with any activity in the period
    kbs_built: int = 0  # knowledge bases with creation / ingestion runs in the period


class PreviousTotals(_Model):
    """The same number of days just before the period (for "vs previous period")."""

    traces: int = 0
    cost: float = 0.0
    creation_cost: float = 0.0
    rag_cost: float = 0.0
    evaluation_cost: float = 0.0
    other_cost: float = 0.0
    rag_traces: int = 0
    total_tokens: int = 0
    avg_rag_latency_ms: float | None = None


class SeriesPoint(_Model):
    bucket: dt.date  # first day of the day / week
    creation_cost: float = 0.0
    rag_cost: float = 0.0
    evaluation_cost: float = 0.0
    other_cost: float = 0.0
    traces: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class KbCost(_Model):
    kb_name: str
    kb_type: str | None = None
    traces: int = 0
    cost: float = 0.0
    creation_cost: float = 0.0
    rag_cost: float = 0.0
    evaluation_cost: float = 0.0
    other_cost: float = 0.0
    rag_traces: int = 0
    total_tokens: int = 0
    last_activity: dt.datetime | None = None


class OperationCost(_Model):
    operation: str
    category: str
    traces: int = 0
    cost: float = 0.0
    total_tokens: int = 0
    avg_latency_ms: float | None = None


class ModelUsage(_Model):
    model: str
    type: str | None = None  # GENERATION (LLM) | EMBEDDING
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    estimated: bool = False  # priced from ANALYTICS_MODEL_PRICES


class SourceUsage(_Model):
    source: str  # web | mcp | job | evaluation
    traces: int = 0
    cost: float = 0.0
    total_tokens: int = 0


class TraceRow(_Model):
    id: str
    name: str | None = None
    category: str
    kb_name: str | None = None
    user_id: str | None = None  # None: someone else's, on a knowledge base the viewer doesn't own
    trace_timestamp: dt.datetime
    total_cost: float = 0.0
    total_tokens: int = 0
    latency_ms: float | None = None
    error_count: int = 0
    url: str | None = None  # the trace in the Langfuse UI


class Summary(_Model):
    api_version: int = API_VERSION
    knowledge_bases: list[KnowledgeBaseRef]
    selected: str | None = None
    days: int
    categories: dict[str, str]  # category -> label
    sources: dict[str, str]  # source -> label
    bucket: str = "day"  # day | week
    totals: Totals
    previous: PreviousTotals | None = None
    series: list[SeriesPoint] = []
    by_kb: list[KbCost] = []
    by_operation: list[OperationCost] = []
    by_model: list[ModelUsage] = []
    by_source: list[SourceUsage] = []
    recent: list[TraceRow] = []


# ------------------------------------------------------------------ questions by user
class PersonUsage(_Model):
    user_id: str
    queries: int = 0
    cost: float = 0.0
    total_tokens: int = 0
    avg_latency_ms: float | None = None
    last_at: dt.datetime | None = None


class SourceCount(_Model):
    source: str
    queries: int = 0
    cost: float = 0.0


class QuestionTotals(_Model):
    queries: int = 0
    cost: float = 0.0
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    avg_latency_ms: float | None = None
    users: int = 0
    first_at: dt.datetime | None = None
    last_at: dt.datetime | None = None


class QuestionRow(_Model):
    id: str
    user_id: str
    question: str | None = None  # None: text not captured
    request_kind: str | None = None  # question | search | cypher
    source: str = "web"
    kb_name: str | None = None
    name: str | None = None  # the operation
    trace_timestamp: dt.datetime
    total_cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float | None = None
    error_count: int = 0
    url: str | None = None


class Questions(_Model):
    api_version: int = API_VERSION
    selected: str | None = None
    user: str | None = None
    source: str | None = None
    days: int
    from_date: dt.date | None = Field(None, alias="from")
    to_date: dt.date | None = Field(None, alias="to")
    questions_stored: bool
    users: list[PersonUsage] = []
    sources: list[SourceCount] = []
    totals: QuestionTotals
    queries: list[QuestionRow] = []
    truncated: bool = False
