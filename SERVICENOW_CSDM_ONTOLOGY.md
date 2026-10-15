# ServiceNow → CSDM ontology → knowledge graph

Knowledge graphs built from ServiceNow are mapped onto the **ServiceNow Common Service Data Model (CSDM)**. The
mapping comes from a fixed ontology template, so the LLM no longer guesses the schema. Anything the template
doesn't cover is queued as **"not mapped"**. The LLM suggests a mapping for each queued item, and the knowledge
base owner maps it (or ignores it) by hand.

This applies to ServiceNow graph knowledge bases only. SAP, uploaded files and ServiceNow knowledge articles (RAG)
work as before.

## Where things are stored

| What | Where | Why |
|---|---|---|
| The ontology template | `backend/app/ontology/servicenow_csdm.json` (JSON in the repo) | It is nested (class → fields → relationships), changes rarely, is reviewed and versioned like code, and can be handed to the LLM as it is. |
| What isn't mapped, the LLM's suggestions, the owner's decisions | Postgres table `ontology_unmapped` (migration `013_servicenow_ontology.sql`) | It changes at runtime, has several users and syncs writing at once, and needs an audit trail (who decided what, and when). |

Storing the template in SQL would spread one document over several linked tables. Changes would then bypass code
review and need migrations. Storing the runtime decisions in a JSON file would lose concurrent writes and the
audit trail.

## The template

`servicenow_csdm.json` holds the following:

- **Domains**: Foundation, Design, Build, Manage technical services, Sell / consume, plus Service operations
  for ITSM records.
- **Classes**, 28 of them. Each class lists:
  - its ServiceNow tables. Subclasses resolve through `sys_class_name` first, then through the table the
    record was read from, so `cmdb_ci_esx_server` read from `cmdb_ci_server` becomes a `Server`;
  - an optional `where` condition. For example, `service_offering` splits into `TechnicalServiceOffering` and
    `BusinessServiceOffering` by `service_classification`;
  - the fields that become properties;
  - the reference fields that become relationships;
  - mixins: `ci` gives every configuration item its common fields (owner, support group, location…), and
    `ticket` does the same for incidents, problems and changes.
- **Link tables**: `sys_user_grmember` becomes `(User)-[:MEMBER_OF]->(Group)`, and `svc_ci_assoc` becomes
  `(ApplicationService)-[:CONTAINS_CI]->(CI)`.
- **`cmdb_rel_ci` rules**: each ServiceNow relationship type between two classes becomes a graph relationship.
  For example:
  - `Consumes::Consumed by`: BusinessApplication → ApplicationService = `CONSUMES`
  - `Depends on::Used by`: ApplicationService → Server = `DEPENDS_ON`
  - `Contains::Contained by`: TechnicalServiceOffering → DynamicCIGroup = `CONTAINS`

To change the template, edit the JSON and bump `version`. `template.load()` validates it: labels, domains,
relationship types and target classes must all exist.

**Check the relationship rules against your instance.** The `cmdb_rel_ci` type names and directions follow
CSDM 4/5 guidance. Before relying on them, check them against the instance's `cmdb_rel_type` table and the CSDM
"CI relationships" page. A rule that doesn't match the instance is not silently wrong: those rows show up as
"not mapped".

## How a sync works

1. **Fetch from ServiceNow.** The connector reads the datasets. The **CSDM service model** preset lists
   every table the template maps (about 30). A table the instance doesn't have (HTTP 404) or may not read
   (HTTP 403) is skipped with a note. The raw pull is saved as a snapshot
   (`<uploads>/<kb>/*_servicenow_raw_*.json.gz`), so mappings decided later can be applied without fetching
   again.
2. **Map to the CSDM ontology** (`app/ontology/mapper.py`):
   - each record is classified, and rows are de-duplicated by `sys_id`;
   - one sheet per class, one per `cmdb_rel_ci` type and one per link table are written to a workbook;
   - the graph schema is generated from the template. Every node is keyed on `sys_id`, and a reference also
     keeps a readable `<field>_name` property.
3. **Record what isn't mapped**, in three kinds:
   - *class*: a table or `sys_class_name` that matches no class;
   - *field*: a field with data on a mapped class that no property or reference covers;
   - *relationship*: a `cmdb_rel_ci` type with no rule, or a known type between two classes that no rule
     allows.

   The LLM is asked for a suggestion for each new item (`app/ontology/suggest.py`), at most
   `ONTOLOGY_MAX_LLM_SUGGESTIONS` per sync. Suggestions are validated against the ontology and **never
   applied automatically**.
4. **What happens next depends on the knowledge base:**
   - *New knowledge base*: PII scan, then the Review screen as usual (with a banner naming the template and the
     number of unmapped items), then Submit and build.
   - *Refresh*: the approved schema grows by whatever is newly mapped, and the usual add-data loader writes the
     rows.

## Mapping by hand

To find the unmapped items, use Workspace → **Not mapped (N)**, or the link on the Review screen. The page is
`/kbs/<kb>/ontology`, and only the knowledge base owner can make decisions there.

| Item | Decisions |
|---|---|
| Table / class | Map to an existing class · Create a new class (name + CSDM domain) · Ignore |
| Field | Store as a property (name, type) · Link to another class (target, relationship type) · Ignore |
| Relationship | Map to a graph relationship type (optionally child → parent) · Ignore |

Decisions apply to that knowledge base and are layered over the template (`Ontology.with_mappings`). The
**Apply mappings** button maps the saved snapshots again:

- a knowledge base still in review gets a new draft;
- a built graph grows by what is now mapped.

A new class's own fields then appear as new items, so the ontology grows one step at a time. Ignored items stay
ignored on later syncs.

## API

| Method | Path | Who |
|---|---|---|
| GET | `/api/ontology/servicenow` | any signed-in user: the template (classes, domains, rules) |
| GET | `/api/kbs/{kb}/ontology` | anyone with access: counts, items with suggestions, the template for this KB |
| PUT | `/api/kbs/{kb}/ontology/items/{id}` | owner: `{"action": ...}` (map or ignore) |
| POST | `/api/kbs/{kb}/ontology/items/{id}/reopen` | owner: undo a decision |
| POST | `/api/kbs/{kb}/ontology/items/{id}/suggest` | owner: ask the LLM again |
| POST | `/api/kbs/{kb}/ontology/apply` | owner: map the saved data again with the decisions (job) |

## Files

- **New:**
  - `backend/app/ontology/` (`servicenow_csdm.json`, `template.py`, `mapper.py`, `store.py`, `suggest.py`,
    `pipeline.py`)
  - `backend/app/api/ontology.py`
  - `backend/app/migrations/013_servicenow_ontology.sql`
  - `frontend/src/pages/Ontology.jsx`
  - `backend/tests/test_servicenow_ontology.py`
- **Changed:**
  - `connectors/servicenow.py`: CSDM preset; `sys_class_name` keeps the table name; connection test reads one
    incident;
  - `api/connectors.py`: ServiceNow graph create and refresh go through the ontology; up to 60 datasets;
  - `loader.py`:
    - a sheet whose name matches is preferred;
    - optional connector links don't make a sheet unmatchable;
    - a reference with several candidate classes counts as "left out" only when none matched;
  - `api/kbs.py`: Review payload carries `ontology`;
  - `kb.py`: workspace list carries `ontology` and `ontology_pending`;
  - `main.py`, `config.py` (`ONTOLOGY_MAX_LLM_SUGGESTIONS`), `.env.example`;
  - `App.jsx`, `Review.jsx`, `Workspace.jsx`, `styles.css`;
  - `tests/source_mocks.py`: a CSDM landscape in the ServiceNow stand-in;
  - `tests/test_connectors.py`.
