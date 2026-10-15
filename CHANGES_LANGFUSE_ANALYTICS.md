# Langfuse analytics tab: complete change log

Everything changed in this work session, checked against git (`git status`, `git diff` against commit `bce20de`).

**What was added:** a separate plug-in that gives the app an **Analytics** tab. You pick a knowledge base (or all of
them) and a period and see Langfuse cost and usage graphs: **KB creation cost**, **RAG / chat cost**, a **Cost
breakdown**, cost by knowledge base and by operation, model usage, recent traces, and **Questions by user** (who
asked what, from the web app or an MCP agent, when, at what cost and token count, with person / source / custom date
range filters). A **Sync from Langfuse** button copies traces from Langfuse into **two new tables**,
`langfuse_traces` and `langfuse_observations`, and the charts read only those tables.

**What was touched in the existing project:** 4 existing files, 23 lines added and 1 line changed in total:

| File | Change | Lines | Why |
|---|---|---|---|
| `backend/app/main.py` | 2 lines added | 20, 97 | Plug the analytics API in |
| `frontend/src/App.jsx` | 2 lines added | 3, 56 | Plug the Analytics page in |
| `frontend/src/components/common.jsx` | 1 line added | 25 | The "Analytics" tab in the top bar |
| `backend/app/mcp_server.py` | 18 lines added, 1 changed | 81-95, 105-107 | MCP traces record the agent's question / search / Cypher (you asked for this in round 3) |

Nothing else in your project was modified: no existing function's logic, SQL, migration, setting, stylesheet,
dependency, Docker file or document. Sections 1-3 describe each touch in detail.

---

## 1. Changes to existing files, in detail

### 1.1 `backend/app/main.py`: 2 lines added

```diff
@@ -17,6 +17,7 @@ from app.api import ontology as ontology_api
 from app.config import get_settings
 from app.db import close_pool, get_conn, open_pool, run_migrations
 from app.graphstore import close_driver
+from app.langfuse_analytics import router as langfuse_analytics_router  # plug-in: Analytics tab (Langfuse)
 from app.observability import flush_langfuse
@@ -93,6 +94,7 @@ app.include_router(connectors_api.router)
 app.include_router(conversations_api.router)
 app.include_router(kbs_api.router)
 app.include_router(ontology_api.router)
+app.include_router(langfuse_analytics_router)  # plug-in: /api/analytics (remove this and the import to unplug)
```

- **Line 20** imports the plug-in's router. Importing has no side effects: no database call, no thread, no Langfuse
  call. The plug-in's tables are created later, on the first `/api/analytics` request.
- **Line 97** registers the routes under `/api/analytics/*` (status, sync, summary, queries). No existing route,
  middleware or startup step changes. Your CSRF middleware applies to these routes like to all others, so the Sync
  POST needs the `X-Requested-With` header (the UI sends it).
- Startup (`lifespan`: migrations, pool, job recovery) is unchanged; the plug-in adds nothing to it.
- **Undo:** delete both lines.

### 1.2 `frontend/src/App.jsx`: 2 lines added

```diff
@@ -1,5 +1,6 @@
 import { useEffect, useState } from "react";
 import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
+import Analytics from "./analytics/AnalyticsPage.jsx"; // plug-in: Analytics tab (Langfuse)
 import { sessionState } from "./auth.js";
@@ -52,6 +53,7 @@ export default function App() {
           <Route path="/chat" element={<Chat />} />
           <Route path="/sessions" element={<Sessions />} />
+          <Route path="/analytics" element={<Analytics />} />
         </Route>
```

- **Line 3** imports the page; **line 56** adds the `/analytics` route **inside** your existing `Protected` route,
  so it gets the same sign-in check, top bar and session-timeout watcher as every other page.
- Side effect to know: Vite bundles CSS that a module imports, so the plug-in's `analytics.css` is part of the app's
  stylesheet from the start. Every one of its rules starts with `.an-root` and every animation name with `an-`
  (checked), so it only applies inside the Analytics page and cannot restyle other screens.
- **Undo:** delete both lines.

### 1.3 `frontend/src/components/common.jsx`: 1 line added

```diff
@@ -22,6 +22,7 @@ export function TopBar() {
         <NavLink className={tab} to="/access">Access</NavLink>
         <NavLink className={tab} to="/chat">Chat</NavLink>
+        <NavLink className={tab} to="/analytics">Analytics</NavLink>
       </nav>
```

- **Line 25** adds the "Analytics" tab after "Chat", using your existing `tab` style. Nothing else in `TopBar` or
  the other components changed.
- **Undo:** delete the line.

### 1.4 `backend/app/mcp_server.py`: MCP traces record what the agent asked (round 3)

You asked for this so that `search_documents` queries and `query_graph` Cypher show up in Analytics; until then the
MCP trace held only the tool name and knowledge base.

```diff
@@ -78,6 +78,21 @@ def _kb(user: CurrentUser, kb_name: str, kind: str | None = None, ready: bool =
     return cat
 
 
+# What a tool call's Langfuse trace records besides the KB: the tool's arguments after kb_name, as (name, is text).
+# Text (question, search query, Cypher) only when LANGFUSE_CAPTURE_CONTENT allows content in traces; limits always.
+# Tracing only: the tool itself is called exactly as before.
+_TRACED_ARGS = {
+    "ask_knowledge_base": (("question", True),),
+    "search_documents": (("query", True), ("k", False)),
+    "query_graph": (("cypher", True), ("limit", False)),
+}
+
+
+def _trace_details(tool: str, args: tuple) -> dict:
+    content = get_settings().langfuse_capture_content
+    return {name: value for (name, text), value in zip(_TRACED_ARGS.get(tool, ()), args[1:]) if content or not text}
+
+
 async def _call(tool: str, fn, *args) -> Any:
@@ -87,7 +102,9 @@ async def _call(tool: str, fn, *args) -> Any:
         kb_name = args[0] if args and isinstance(args[0], str) and tool != "whoami" else None
         # one Langfuse trace per tool call, as the Keycloak user; the Keycloak session groups an agent's calls
-        with trace_context(user.user_id, claims.get("sid") or claims.get("jti"), f"MCP {tool}", kb_name):
+        with trace_context(
+            user.user_id, claims.get("sid") or claims.get("jti"), f"MCP {tool}", kb_name, **_trace_details(tool, args)
+        ):
             return fn(user, *args)
```

**What it does**

- **Lines 81-95 (added):** `_TRACED_ARGS` lists, per tool, the arguments after `kb_name` and whether each one is
  text. `_trace_details(tool, args)` turns the call's arguments into a small dict, e.g. `{"query": "refund policy",
  "k": 5}`. Text is only included when `LANGFUSE_CAPTURE_CONTENT=true` (the setting that already decides whether
  prompts go to Langfuse). `k` and `limit` are always included because they aren't content.
- **Lines 105-107 (changed from line 90):** the existing `trace_context(...)` call gets those values as extra keyword
  arguments. `trace_context` (in `app/observability.py`, unchanged) already accepts extra keyword arguments and puts
  them in the trace's input, next to `operation` and `knowledge_base`.

| Tool | Langfuse trace input before | After |
|---|---|---|
| `ask_knowledge_base` | `operation`, `knowledge_base` | + `question` |
| `search_documents` | `operation`, `knowledge_base` | + `query`, `k` |
| `query_graph` | `operation`, `knowledge_base` | + `cypher`, `limit` |
| `whoami`, `list_knowledge_bases`, `describe_knowledge_base` | unchanged | unchanged |

**What it does not change**

- Every tool is still called as `fn(user, *args)` with the same arguments and returns the same result. Access checks,
  guardrails, Cypher safety, the thread hand-off and logging are untouched.
- Trace name, user and session are unchanged, so your existing test `test_mcp_tool_calls_are_traced_as_the_signed_in_user`
  (which checks name / user / session) is unaffected.

**What to be aware of**

- With `LANGFUSE_CAPTURE_CONTENT=true`, the agent's search text and Cypher now appear in Langfuse: in the trace's
  input, and also in its metadata, because `trace_context` copies extra values there. The question of
  `ask_knowledge_base` already reached Langfuse before this change, inside the LLM prompt. With
  `LANGFUSE_CAPTURE_CONTENT=false` no text is added.
- MCP traces recorded before this change don't carry the text, so they show "not captured" in Questions by user.

**How it was verified:** the real `_call` and Langfuse SDK, exporting to your `tests/langfuse_mock.py`. Each tool
received exactly its arguments and its result came back unchanged; the traces carried question / query / Cypher; with
content capture off, only `k` / `limit`.

**Undo:** delete lines 81-95 and put line 90 back to
`with trace_context(user.user_id, claims.get("sid") or claims.get("jti"), f"MCP {tool}", kb_name):`. The plug-in keeps
working; MCP searches and Cypher then show as "not captured".

---

## 2. Did the analytics UI or backend disturb existing code?

**No existing code was modified** apart from the lines in section 1 (git diff shows nothing else). The plug-in only
**uses** existing code, read-only:

| Plug-in uses | From | For |
|---|---|---|
| `current_user`, `CurrentUser` | `app/auth.py` | Signed-in user on every analytics route |
| `kb.list_for_user`, `kb.require_access` | `app/kb.py` | Which knowledge bases a user may see (same rules as the rest of the app) |
| `get_conn` | `app/db.py` | The app's Postgres connection pool |
| `get_settings` (`LANGFUSE_*` values) | `app/config.py` | Langfuse URL / keys / environment, content-capture setting |
| `WORKFLOW_NAMES` | `app/observability.py` | Trace names → KB creation / RAG categories |
| `api`, `formatDate` | `frontend/src/api.js` | API calls (session cookie, CSRF header), dates |
| `ErrorBox`, `TypeBadge`, `usePoll` | `frontend/src/components/common.jsx` | Error box, KB type badge, polling during a sync |

If those signatures change in the future, the plug-in needs the matching update; nothing in your code depends on
the plug-in.

**Runtime effects on the running app (nothing changes until someone opens Analytics or presses Sync):**

- **Database:** two new tables, their indexes, and one trigger function (`langfuse_analytics_touch`), created with
  `CREATE ... IF NOT EXISTS` on the first analytics request (round 2 and 3 columns added with `ADD COLUMN IF NOT
  EXISTS`). Your existing tables are only **read** (through `kb.list_for_user` / `kb.require_access`: `knowledge_bases`,
  `kb_access`, `kb_catalog`, `users`), never written. Table creation takes advisory lock 72011090, which differs from
  your migration lock (72011001).
- **Backend process:** a sync runs in one background thread in the backend process and uses one pooled connection at
  a time (your pool allows 10). The analytics pages run ordinary queries on the plug-in's own tables.
- **Langfuse:** the sync only **reads** from Langfuse's public API. Your tracing (`app/observability.py`) is unchanged.
- **Frontend:** a new tab and route. Styles are scoped (section 1.2).

**One interaction that went the other way, now fixed:** your global stylesheet has generic classes, `.num`
(`styles.css:849`, the job step bubbles, `display: flex`) and `.bar` (`styles.css:818`). The plug-in's tables used
the same class names, so your styles squeezed every number into one column. The fix was inside the plug-in: every
plug-in class now starts with `an-`. `styles.css` was not changed.

**Files I created outside the plug-in folders:** only this document. Running the tests left no `__pycache__`,
`.pytest_cache`, `node_modules` or `dist` changes in the project (checked). All test tooling lived outside the
project in a temporary folder: a Python virtual environment, an embedded PostgreSQL, a copy of the frontend, and a
stand-in Langfuse server. Your local PostgreSQL on port 5432 only saw one failed sign-in attempt with the documented
dev password; nothing was created or changed there.

---

## 3. All changes in this session, round by round

### Round 1: the plug-in
- Backend package `backend/app/langfuse_analytics/`: Langfuse REST client, record → row mapping, background sync,
  Postgres store, API routes, settings, table DDL.
- Tables `langfuse_traces` and `langfuse_observations`.
- Frontend `frontend/src/analytics/`: Analytics page, dependency-free SVG charts, formatting, scoped CSS.
- The 5 hook lines (sections 1.1-1.3), tests, README, this document.

### Round 2: modern charts and Questions by user
- **Questions by user**: person filter, period presets plus a **custom From / To range**, people ranked by cost, a
  scatter of every question (time vs cost, size = tokens), and the question list (person, question, KB, time, cost,
  tokens, duration, Langfuse link).
- The question of a web chat is read from the first LLM prompt of its trace (`app/chat.py` ends every prompt with
  `Question: ...`); new column `langfuse_traces.question`; setting `ANALYTICS_STORE_QUESTIONS`.
- Tiles with sparklines and change against the previous period; area charts; donut; animated columns.
- Fixed: "NaN" axis labels while a custom range was half-typed; repeated day labels; dots outside the axis when the
  database's calendar day differs from the browser's.

### Round 3: MCP text, table fix, layout, API contract, cost breakdown, animations
- **`app/mcp_server.py`** (section 1.4). The plug-in reads `question` / `query` / `cypher` from the trace input and
  stores them with `request_kind` = question / search / cypher; new column `source` (web / mcp / job / evaluation).
- **Source filter** in Questions by user (All / Web app / MCP agents, with counts); MCP rows show a Search or Cypher tag,
  Cypher as code.
- **Table bug fixed** (`an-` prefix, section 2); tables got sticky headers and a scroll limit.
- **Layout:** Cost over time is full width; the donut moved into a full-width **Cost breakdown** card: donut linked to
  a table (cost, share, operations, average per operation, tokens), cost by channel, and highlights (per question, per
  KB built, most expensive KB, costliest operation, output-token share). Three / two / one columns by screen width;
  table columns drop instead of scrolling.
- **Modular API:** `schemas.py` (the JSON contract, validated on every response, `api_version`), `service.py`
  (access rules), thin `api.py`; frontend `client.js` is the only file that knows URLs.
- **Animations:** only `transform` / `opacity`; heavy chart layers cached so hover redraws a small overlay; pointer
  handling once per frame; count-up numbers; all motion off with reduced motion. Measured at 60 fps while hovering
  30 columns and 272 dots, with no long tasks.

### Round 4: this document
- Full change log, git-checked, and the sync explanation below.

---

## 4. How the sync works

```
 [Sync from Langfuse] / [Full re-sync]          (any signed-in user; Analytics tab)
          │ POST /api/analytics/langfuse/sync {"full": false | true}      → 202 at once
          ▼
 background thread in the backend (one sync at a time; a second click just shows the running one)
   1. ensure the two tables exist
   2. choose the window  ──  Sync:          newest stored trace − 6 h  →  now   (first time: last 90 days)
                         └─  Full re-sync:  now − 90 days              →  now
   3. GET /api/public/traces        tag "graphbase", LANGFUSE_ENVIRONMENT, oldest first, 100 per page
        → map each trace (KB, kind of work, source, question for MCP)   → upsert in batches of 200
   4. GET /api/public/observations  same window, 100 per page
        → tokens, cost (+ estimate from ANALYTICS_MODEL_PRICES), web-chat question from the prompt
        → upsert in batches, skipping observations of traces not stored (other apps / older traces)
   5. roll observations up into their traces: tokens, model calls, errors, cost = max(Langfuse, sum incl. estimates)
   6. store web-chat questions (only where none is stored yet)
   7. status: "Synced N traces and M observations"  (or the error, e.g. wrong keys)
          ▲
 the page polls GET /api/analytics/langfuse/status every 1.5 s while it runs, then reloads every chart
```

Details:

- **Only reads Langfuse**, with your existing `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL`.
  Without the keys both buttons are disabled and the card says so.
- **What is fetched:** traces your app wrote (tag `graphbase`, which `app/observability.py` puts on every trace), in
  `LANGFUSE_ENVIRONMENT` if set, for **all users and all knowledge bases**. Who sees what is decided when the charts
  are read (section 6), not at sync time.
- **Upserts, never deletes:** rows are matched by Langfuse id and inserted or updated, so re-reading a window never
  creates duplicates. Nothing is ever deleted, so rows older than the window stay, and traces deleted in Langfuse
  stay in the tables.
- **Safe to interrupt:** each batch is committed on its own. If the backend restarts mid-sync, the rows already
  written stay, the status is lost, and the next Sync continues from the newest stored trace.
- **Rate limits and errors:** HTTP 429 / 5xx / network errors are retried 4 times with backoff (Langfuse's
  `Retry-After` is honoured); wrong keys give a clear message in the sync card.
- **Size cap:** at most 500 pages (50,000 records) per endpoint per sync (`ANALYTICS_SYNC_MAX_PAGES`). If it stops
  there the message says so; press Sync again for the rest.
- **The card shows:** "Last synced" (the newest `synced_at` in the table), how many traces and observations are
  stored, the live count while syncing, and the result or error.
- One sync per backend process. If you run several backend workers, two clicks on different workers could sync in
  parallel; that's harmless because writes are upserts.

### The two buttons

| | **Sync from Langfuse** | **Full re-sync** |
|---|---|---|
| Window read | From the newest trace already stored, minus 6 hours (`ANALYTICS_SYNC_OVERLAP_HOURS`), up to now. The very first time (empty tables): the last 90 days (`ANALYTICS_SYNC_LOOKBACK_DAYS`) | Always the last 90 days (`ANALYTICS_SYNC_LOOKBACK_DAYS`), whatever is stored |
| Speed | Fast: usually only what's new since last time | Slower, with more Langfuse API calls: re-reads everything in the window |
| Why the overlap | Langfuse fills in costs and late observations after a trace starts; re-reading the last 6 hours picks those up | n/a |
| Deletes anything? | No | No (it does not empty the tables) |
| Use it | Routinely, whenever you want fresh numbers | After changing `ANALYTICS_MODEL_PRICES`, `ANALYTICS_STORE_QUESTIONS` (false → true), `LANGFUSE_ENVIRONMENT` or `ANALYTICS_SYNC_TAG`; after raising `ANALYTICS_SYNC_LOOKBACK_DAYS` to load older history; after a sync stopped at the page cap (observations of that window are re-read); or if numbers look stale |

What neither button does:
- **Remove data.** To start over, empty the tables and press Sync:
  `TRUNCATE langfuse_observations, langfuse_traces;`.
- **Wipe a stored question.** A re-sync never blanks one. If you switch `ANALYTICS_STORE_QUESTIONS` to false, existing
  text stays until you run `UPDATE langfuse_traces SET question = NULL;`.
- **Add question text to old MCP traces.** Traces recorded before the `mcp_server.py` change don't contain it.

---

## 5. Files

### New files (all lines new)

| File | Lines | Purpose |
|---|---|---|
| `backend/app/langfuse_analytics/__init__.py` | 1-19 | Package entry: exports `router` |
| `backend/app/langfuse_analytics/api.py` | 1-58 | HTTP routes only: parameters in, schemas out |
| `backend/app/langfuse_analytics/schemas.py` | 1-236 | The API contract: Pydantic response models for every route |
| `backend/app/langfuse_analytics/service.py` | 1-118 | Access scope and what each view contains (no HTTP) |
| `backend/app/langfuse_analytics/store.py` | 1-360 | SQL: upserts, chart queries, previous period, by source, Questions by user |
| `backend/app/langfuse_analytics/sync.py` | 1-153 | The sync (section 4) |
| `backend/app/langfuse_analytics/client.py` | 1-128 | Read-only Langfuse public API client (paging, retry) |
| `backend/app/langfuse_analytics/transform.py` | 1-281 | Langfuse records → rows: KB, kind of work, source, tokens, cost, what was asked |
| `backend/app/langfuse_analytics/settings.py` | 1-35 | Optional `ANALYTICS_*` settings |
| `backend/app/langfuse_analytics/schema.sql` | 1-86 | The two tables, indexes, trigger, added columns |
| `backend/app/langfuse_analytics/README.md` | 1-122 | Plug-in documentation (layers, API, settings, removal) |
| `backend/tests/test_langfuse_analytics.py` | 1-620 | 35 tests |
| `frontend/src/analytics/client.js` | 1-30 | The frontend's only link to the API |
| `frontend/src/analytics/AnalyticsPage.jsx` | 1-418 | The page |
| `frontend/src/analytics/CostBreakdown.jsx` | 1-146 | Cost breakdown card |
| `frontend/src/analytics/UserQueries.jsx` | 1-311 | Questions by user card |
| `frontend/src/analytics/charts.jsx` | 1-627 | Charts, tiles, tables (no npm dependency) |
| `frontend/src/analytics/format.js` | 1-152 | Formatting, axis scales, smooth curves, series colours |
| `frontend/src/analytics/analytics.css` | 1-610 | Styles, all scoped under `.an-root` with `an-` classes |
| `CHANGES_LANGFUSE_ANALYTICS.md` | all | This document |

No new npm or Python dependency: the backend uses `httpx`, `psycopg`, `pydantic-settings` and FastAPI, which your
project already has; the charts are plain SVG.

### Backend layers (for future changes to the API format)

```
frontend/src/analytics/client.js   the only frontend code that knows URLs and parameters
        │  HTTP, JSON format defined in schemas.py
api.py        routes: parameters in, schemas out (the only FastAPI code)
schemas.py    THE API CONTRACT: response models, validated on every response, listed in /docs, api_version
service.py    who may see what, and what each view contains
store.py      SQL on the two tables
sync.py       Langfuse → tables, using client.py (Langfuse REST) and transform.py (pure mapping)
```

To change the API format later: edit `schemas.py` (plus `service.py` / `store.py` if the data itself changes), then
`client.js` and the component that shows it. Bump `API_VERSION` for an incompatible change. The sync, the tables and
the access rules are unaffected.

---

## 6. Data, access and API

**Tables**
- `langfuse_traces`: one row per Langfuse trace (= one operation): name, category (`creation` / `rag` / `evaluation` /
  `other`), source (`web` / `mcp` / `job` / `evaluation`), `kb_name`, user, session label, timestamp, latency, Langfuse
  cost and total cost, input / output / total tokens, observation / model-call / error counts, Langfuse link path,
  `question` and `request_kind` (chat, MCP and evaluation traces only), plus `created_at`, `updated_at` (trigger),
  `modified_by` (the user who pressed Sync).
- `langfuse_observations`: one row per LLM call / embedding / span of a stored trace: type, name, model, level,
  timings, tokens, input / output / total cost, `cost_estimated`, plus the audit columns.
- No prompts or answers are stored. The only text kept is what was asked (question / search query / Cypher);
  `ANALYTICS_STORE_QUESTIONS=false` stops that.

**Who sees what** (the same grants as the web app; MCP uses the same `kb_access` grants)
- Only knowledge bases you own or were granted. A knowledge base created by someone else without access to you never
  appears.
- Totals and charts include everyone's activity on those knowledge bases.
- Other people's names (Recent traces) and their questions (Questions by user) are shown only on knowledge bases you
  own; you always see your own.

**Routes** (all need a signed-in user; response models in `schemas.py`, also at `/docs`)

| Route | Returns |
|---|---|
| `GET /api/analytics/langfuse/status` | `SyncStatus`: running / result, counts, last synced, whether Langfuse is configured |
| `POST /api/analytics/langfuse/sync` `{"full": bool}` | `SyncStatus`; 202 (starts in background), 409 without Langfuse keys |
| `GET /api/analytics/summary?kb=&days=` | `Summary`: everything the charts show; `days` 7 / 30 / 90 / 0 (all time) |
| `GET /api/analytics/queries?kb=&user=&source=&days=` or `&from=&to=` | `Questions`: people, sources, totals, newest questions; custom range inclusive |

**Settings** (optional, in `.env`; not added to `.env.example` to keep that file untouched; see the plug-in README):
`ANALYTICS_SYNC_LOOKBACK_DAYS` (90), `ANALYTICS_SYNC_OVERLAP_HOURS` (6), `ANALYTICS_SYNC_PAGE_SIZE` (100),
`ANALYTICS_SYNC_MAX_PAGES` (500), `ANALYTICS_SYNC_TAG` (graphbase), `ANALYTICS_REQUEST_TIMEOUT_SECONDS` (30),
`ANALYTICS_REQUEST_RETRIES` (4), `ANALYTICS_MODEL_PRICES` ({}), `ANALYTICS_RECENT_TRACES` (25),
`ANALYTICS_STORE_QUESTIONS` (true).

**Known limits**
- Embedding requests carry no token counts in your traces (`app/llm.py` sends only the number of texts), so they cost
  $0 in Analytics.
- Ollama models have no Langfuse price: $0 unless `ANALYTICS_MODEL_PRICES` is set (shown as "est.").
- The final combining call of a multi-KB chat runs outside any knowledge base's trace, so it isn't counted per KB.
- The MCP agent's own LLM (in `agent/`, on the user's machine) doesn't report to Langfuse, so its cost isn't included.

---

## 7. Verification

- `pytest tests/test_langfuse_analytics.py`: **35 passed**, including PostgreSQL tests against a throwaway database
  (`LANGFUSE_ANALYTICS_TEST_DSN`; skipped without it). They cover mapping, question extraction, the Langfuse client,
  the sync, access scoping, the response contract, the source filter, custom ranges, and questions kept across re-sync.
- `mcp_server.py` checked end to end (section 1.4).
- `ruff check` (E, F, W, I, UP; line length 120) clean on the plug-in, its test, `main.py` and `mcp_server.py`; new
  Python files ruff-formatted. ESLint clean on `src/analytics`, `App.jsx`, `components/common.jsx`; `vite build` OK.
  (ESLint reports two existing, unrelated issues in `pages/Chat.jsx:219` and `pages/Review.jsx:291`, left as they are.)
- Browser, against the real routes with a stand-in Langfuse (351 traces, including MCP) and PostgreSQL: sync, filters,
  tables, breakdown, MCP rows, hover performance, and 375 / 1008 / 1366 / 1920 px layouts; no horizontal overflow.
- **Not tested:** a real Langfuse project, a real MCP agent session, Docker Compose. Your repository's own
  `tests/test_mcp_server.py` and `tests/test_observability.py` could not be run because `tests/conftest.py` and
  `tests/fixtures.py` are not in this checkout.

## 8. Removing the plug-in

1. Delete the 5 hook lines (sections 1.1-1.3). Optionally revert `mcp_server.py` (section 1.4).
2. Delete `backend/app/langfuse_analytics/`, `frontend/src/analytics/`, `backend/tests/test_langfuse_analytics.py`.
3. Optionally drop the data:

```sql
DROP TABLE IF EXISTS langfuse_observations, langfuse_traces;
DROP FUNCTION IF EXISTS langfuse_analytics_touch();
```
