-- ServiceNow data the CSDM ontology template doesn't cover, per knowledge base.
-- The template itself is a versioned JSON file in the repo (app/ontology/servicenow_csdm.json). What changes at
-- runtime lives here: what wasn't mapped, the LLM's suggestion, and the person's decision (mapped / ignored).
CREATE TABLE ontology_unmapped (
    id             bigserial PRIMARY KEY,
    kb_name        text NOT NULL REFERENCES kb_catalog (kb_name) ON DELETE CASCADE ON UPDATE CASCADE,
    item_kind      text NOT NULL CHECK (item_kind IN ('class', 'field', 'relationship')),
    item_key       text NOT NULL,          -- table | Class.field | "Type::Type" or "Type | Parent -> Child"
    detail         jsonb NOT NULL DEFAULT '{}',
    suggestion     jsonb,                  -- the LLM's proposal; never applied without a person
    suggested_at   timestamptz,
    status         text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'mapped', 'ignored')),
    mapping        jsonb,                  -- what the person decided
    decided_by     text,
    decided_at     timestamptz,
    applied_at     timestamptz,            -- when the mapping was last used to rebuild the graph
    occurrences    integer NOT NULL DEFAULT 0,
    first_seen     timestamptz NOT NULL DEFAULT now(),
    last_seen      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kb_name, item_kind, item_key)
);
CREATE INDEX ontology_unmapped_kb_status ON ontology_unmapped (kb_name, status);
