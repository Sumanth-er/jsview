-- Langfuse analytics plug-in (app/langfuse_analytics). Idempotent: run by store.ensure_schema() on first use, so the
-- plug-in needs no entry in app/migrations. Copies of Langfuse traces and observations for the Analytics tab.
-- No prompts, answers or other input/output content is stored; only names, timings, models, tokens and cost, plus the
-- user's question for chat traces (langfuse_traces.question; off with ANALYTICS_STORE_QUESTIONS=false).

CREATE TABLE IF NOT EXISTS langfuse_traces (
    id                text PRIMARY KEY,                 -- Langfuse trace id
    name              text,                             -- operation: KG Extraction, Embedding, Chat, RAG Chat, ...
    category          text NOT NULL DEFAULT 'other'
                      CHECK (category IN ('creation', 'rag', 'evaluation', 'other')),
    kb_name           text,                             -- trace metadata knowledge_base; no FK, traces outlive KBs
    user_id           text,
    session_id        text,                             -- the Langfuse session label, never the web session key
    environment       text,
    tags              jsonb NOT NULL DEFAULT '[]',
    metadata          jsonb NOT NULL DEFAULT '{}',      -- knowledge_base / operation / job_id / source only
    trace_timestamp   timestamptz NOT NULL,
    latency_ms        double precision,
    langfuse_cost     numeric(20, 10) NOT NULL DEFAULT 0,  -- totalCost as Langfuse reports it (USD)
    total_cost        numeric(20, 10) NOT NULL DEFAULT 0,  -- greatest(langfuse_cost, sum of observation costs)
    input_tokens      bigint NOT NULL DEFAULT 0,
    output_tokens     bigint NOT NULL DEFAULT 0,
    total_tokens      bigint NOT NULL DEFAULT 0,
    observation_count integer NOT NULL DEFAULT 0,
    generation_count  integer NOT NULL DEFAULT 0,       -- LLM calls + embedding requests
    error_count       integer NOT NULL DEFAULT 0,       -- observations with level ERROR
    html_path         text,                             -- link into the Langfuse UI, relative to LANGFUSE_BASE_URL
    synced_at         timestamptz NOT NULL DEFAULT now(),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    modified_by       text NOT NULL DEFAULT 'system'
);
CREATE INDEX IF NOT EXISTS langfuse_traces_kb_time ON langfuse_traces (kb_name, trace_timestamp);
CREATE INDEX IF NOT EXISTS langfuse_traces_time ON langfuse_traces (trace_timestamp);
-- What was asked (chat / RAG / MCP / evaluation traces only): from the trace input (MCP tools, RAGAS) or else the
-- first LLM prompt of the trace (web chat). request_kind: question | search (MCP search_documents) | cypher (query_graph)
ALTER TABLE langfuse_traces ADD COLUMN IF NOT EXISTS question text;
ALTER TABLE langfuse_traces ADD COLUMN IF NOT EXISTS request_kind text;
-- Where the operation came from: web | mcp | job | evaluation
ALTER TABLE langfuse_traces ADD COLUMN IF NOT EXISTS source text;
UPDATE langfuse_traces SET source = CASE
        WHEN name LIKE 'MCP %' THEN 'mcp' WHEN category = 'evaluation' THEN 'evaluation'
        WHEN category = 'creation' THEN 'job' ELSE 'web' END
    WHERE source IS NULL;
CREATE INDEX IF NOT EXISTS langfuse_traces_user_time ON langfuse_traces (user_id, trace_timestamp);

CREATE TABLE IF NOT EXISTS langfuse_observations (
    id                    text PRIMARY KEY,             -- Langfuse observation id
    trace_id              text NOT NULL REFERENCES langfuse_traces (id) ON DELETE CASCADE,
    parent_observation_id text,
    type                  text,                         -- GENERATION | EMBEDDING | SPAN | CHAIN | ...
    name                  text,                         -- llm_json, llm_text, embedding_documents, Neo4j, ...
    model                 text,
    level                 text,
    status_message        text,
    start_time            timestamptz,
    end_time              timestamptz,
    latency_ms            double precision,
    input_tokens          bigint NOT NULL DEFAULT 0,
    output_tokens         bigint NOT NULL DEFAULT 0,
    total_tokens          bigint NOT NULL DEFAULT 0,
    input_cost            numeric(20, 10) NOT NULL DEFAULT 0,
    output_cost           numeric(20, 10) NOT NULL DEFAULT 0,
    total_cost            numeric(20, 10) NOT NULL DEFAULT 0,
    cost_estimated        boolean NOT NULL DEFAULT false,  -- true: priced from ANALYTICS_MODEL_PRICES, not Langfuse
    synced_at             timestamptz NOT NULL DEFAULT now(),
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    modified_by           text NOT NULL DEFAULT 'system'
);
CREATE INDEX IF NOT EXISTS langfuse_observations_trace ON langfuse_observations (trace_id);
CREATE INDEX IF NOT EXISTS langfuse_observations_model ON langfuse_observations (model) WHERE model IS NOT NULL;

-- updated_at maintained by a trigger, like every other table (own function, so the plug-in depends on nothing)
CREATE OR REPLACE FUNCTION langfuse_analytics_touch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS langfuse_traces_touch ON langfuse_traces;
CREATE TRIGGER langfuse_traces_touch BEFORE UPDATE ON langfuse_traces
    FOR EACH ROW EXECUTE FUNCTION langfuse_analytics_touch();
DROP TRIGGER IF EXISTS langfuse_observations_touch ON langfuse_observations;
CREATE TRIGGER langfuse_observations_touch BEFORE UPDATE ON langfuse_observations
    FOR EACH ROW EXECUTE FUNCTION langfuse_analytics_touch();
