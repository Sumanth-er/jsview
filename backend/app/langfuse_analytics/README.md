# Langfuse analytics plug-in

Adds the **Analytics** tab: cost, token and usage charts per knowledge base, built from the Langfuse traces the app
already writes (`app/observability.py`). A **Sync from Langfuse** button copies traces from Langfuse's public API into
two Postgres tables; the charts read only those tables.

Nothing in the existing code depends on this package. The only app change it relies on is optional and tracing-only:
`app/mcp_server.py` puts the agent's question / search query / Cypher into its Langfuse trace (`_trace_details`).

## Layers

Each layer has one job and can be changed or replaced without touching the others:

```
frontend/src/analytics/client.js   the only frontend code that knows URLs and parameters
        │  HTTP (JSON format = schemas.py)
api.py        routes: parameters in, schemas out (the only FastAPI code)
schemas.py    THE API CONTRACT: Pydantic response models, validated on every response, listed in /docs
service.py    who may see what (kb.list_for_user / kb.require_access) and what each view contains
store.py      SQL against the two tables
sync.py       Langfuse -> tables, using client.py (Langfuse REST) and transform.py (record -> row, pure functions)
schema.sql    the two tables (idempotent DDL, run on first use)
```

**Changing the API format later:** edit the model in `schemas.py` (and the matching keys in `service.py` /
`store.py` if the data itself changes), then `frontend/src/analytics/client.js` and the component that shows it.
Every response carries `api_version`; bump `schemas.API_VERSION` for a change that isn't backwards compatible. The
sync, the tables and the access rules are not affected by a format change.

| File | What it does |
|---|---|
| `backend/app/langfuse_analytics/schema.sql` | The two tables, `langfuse_traces` and `langfuse_observations`, created on first use (idempotent) |
| `backend/app/langfuse_analytics/client.py` | Read-only client for `GET /api/public/traces` and `/api/public/observations` (paging, 429/5xx retry) |
| `backend/app/langfuse_analytics/transform.py` | Maps a Langfuse trace/observation to a row: knowledge base, kind of work, source, tokens, cost, what was asked |
| `backend/app/langfuse_analytics/sync.py` | The sync: one at a time, in a background thread; incremental with an overlap window |
| `backend/app/langfuse_analytics/store.py` | Upserts and the chart queries |
| `backend/app/langfuse_analytics/service.py` | Access scope and view assembly (no HTTP) |
| `backend/app/langfuse_analytics/schemas.py` | Response models (the API contract) |
| `backend/app/langfuse_analytics/api.py` | `/api/analytics/*` routes |
| `backend/app/langfuse_analytics/settings.py` | Optional `ANALYTICS_*` settings |
| `frontend/src/analytics/` | `client.js` (API calls), the page, `CostBreakdown.jsx`, `UserQueries.jsx`, dependency-free charts, scoped CSS (every class prefixed `an-`) |
| `backend/tests/test_langfuse_analytics.py` | Tests (Langfuse mocked; Postgres part runs with `LANGFUSE_ANALYTICS_TEST_DSN`) |

## API

| Route | |
|---|---|
| `GET /api/analytics/langfuse/status` | Sync state, rows stored, last sync time, whether Langfuse is configured |
| `POST /api/analytics/langfuse/sync` | Start a sync (`{"full": true}` re-reads the whole look-back window). 409 without Langfuse keys |
| `GET /api/analytics/summary?kb=<name>&days=<7\|30\|90\|0>` | Everything the tab shows. `kb` empty = every KB the user can access; `days=0` = all time |
| `GET /api/analytics/queries?kb=&user=&source=&days=30` or `&from=YYYY-MM-DD&to=YYYY-MM-DD` | Questions by user: per-person totals, per-source counts and the newest questions (text, kind, source, time, cost, tokens). `source` = `web` / `mcp` / empty. Custom range is inclusive; `from` after `to` is a 422 |

Response models: `SyncStatus`, `Summary`, `Questions` in `schemas.py` (also at `/docs`).

## How traces become "KB creation cost" and "RAG cost"

The app opens one Langfuse trace per operation, named after it, with the knowledge base in the trace metadata.

| Trace name (from the app) | Category in the charts |
|---|---|
| KG Extraction, KG Build, Embedding (RAG ingest), Hybrid Ingestion, Add Data | **KB creation & ingestion** |
| Chat, RAG Chat, Hybrid Chat, `multi_kb_*_chat`, MCP ask_knowledge_base / search_documents / query_graph | **RAG / chat** |
| RAGAS Evaluation | Evaluation |
| anything else (e.g. MCP whoami) | Other |

**What was asked** (Questions by user):
- MCP agents: from the trace input that `app/mcp_server.py` records: `question` (ask_knowledge_base), `query`
  (search_documents, shown as **Search**), `cypher` (query_graph, shown as **Cypher**). RAGAS evaluations: `question`.
- Web app chat: the root trace has no question, so it is read from the first LLM prompt (`app/chat.py` ends every
  graph / RAG / hybrid prompt with `Question: <question>`).
- Both need `LANGFUSE_CAPTURE_CONTENT=true` (the default); otherwise it shows as "not captured".

**Source** of every trace: `web` (web app), `mcp` (an MCP agent: trace name `MCP <tool>`), `job` (KB creation and
ingestion jobs), `evaluation` (RAGAS). The Cost breakdown shows cost by source; Questions by user filters by it.
Access granted to a person covers both the web app and MCP (same `kb_access` grants), so the same rules apply.

Cost is Langfuse's own calculated cost (it prices known models such as gpt-4.1 from the token usage the app reports).
For models Langfuse has no price for (Ollama), set `ANALYTICS_MODEL_PRICES` to get an estimate (marked "est.").

## Settings (all optional, in the same `.env`)

Langfuse access reuses `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` and `LANGFUSE_ENVIRONMENT`.

```dotenv
ANALYTICS_SYNC_LOOKBACK_DAYS=90      # first sync / full re-sync window
ANALYTICS_SYNC_OVERLAP_HOURS=6       # incremental sync re-reads this much before the newest stored trace
ANALYTICS_SYNC_PAGE_SIZE=100
ANALYTICS_SYNC_MAX_PAGES=500         # per endpoint and sync
ANALYTICS_SYNC_TAG=graphbase         # only traces with this tag (the app tags all of its traces); empty = all
ANALYTICS_REQUEST_TIMEOUT_SECONDS=30
ANALYTICS_REQUEST_RETRIES=4
ANALYTICS_MODEL_PRICES={"qwen2.5:7b-instruct": {"input": 0.0, "output": 0.0}}   # USD per 1M tokens
ANALYTICS_RECENT_TRACES=25
ANALYTICS_STORE_QUESTIONS=true      # false = never store the question text (Questions by user then shows "not captured")
```

## Privacy and access

- No prompts or answers are stored; only names, timings, models, tokens, cost, the knowledge base / job id / source
  file name from the trace metadata, and the user's question on chat traces (`ANALYTICS_STORE_QUESTIONS`).
- Every number is limited to knowledge bases the signed-in user owns or was granted. Another user's ID (in "Recent
  traces") and their questions (in "Questions by user") are shown only on knowledge bases the viewer owns; the viewer
  always sees their own.

## Known limits

- Embedding requests are recorded by the app without token counts (`app/llm.py` sends only the number of texts), so
  Langfuse cannot price them and they show $0.
- The final synthesis call of a multi-KB chat runs outside any knowledge base's trace, so it is stored but not
  attributed to a knowledge base (it doesn't appear in per-KB numbers).

## Remove

1. Delete the two lines marked `plug-in` in `backend/app/main.py`, the two in `frontend/src/App.jsx` and the one in
   `frontend/src/components/common.jsx`.
2. Delete `backend/app/langfuse_analytics/`, `frontend/src/analytics/` and `backend/tests/test_langfuse_analytics.py`.
3. Optionally drop the data:

```sql
DROP TABLE IF EXISTS langfuse_observations, langfuse_traces;
DROP FUNCTION IF EXISTS langfuse_analytics_touch();
```
