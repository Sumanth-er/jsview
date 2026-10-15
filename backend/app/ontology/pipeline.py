"""ServiceNow -> CSDM ontology -> knowledge graph.

Every ServiceNow pull for a graph knowledge base goes through here instead of LLM schema extraction:
  1. Fetch the tables (connector) and keep the raw pull as a snapshot next to the KB's uploads, so mappings
     decided later can be applied without fetching again.
  2. Map the records onto the template + this KB's manual mappings (mapper.py): one sheet per class, written
     to a workbook, and the graph schema generated from the template.
  3. Record what isn't mapped (store.py) and ask the LLM for a suggestion for each new item (suggest.py).
  4. New KB: PII scan, then Review as usual. Existing KB: the schema grows by what's newly mapped and the
     normal add-data loader writes the rows.
"""

import datetime as dt
import gzip
import json
import logging
import uuid
from pathlib import Path

from app import extraction, jobs, kb, nist_pii, pipelines
from app import graph_schema as gs
from app.config import get_settings
from app.connectors import ConnectorError, Dataset
from app.connectors import store as connection_store
from app.connectors.base import Table
from app.connectors.pipeline import write_workbook
from app.db import get_conn
from app.ontology import mapper, store, suggest, template
from app.tabular import read_table_file

log = logging.getLogger(__name__)

MAP_STEP = "Map to the CSDM ontology"
SUGGEST_STEP = "LLM suggests mappings for data not in the ontology"
PII_STEP = "LLM scans for PII"
REMAP_STEP = "Map the saved ServiceNow data again"

CREATE_STEPS = [MAP_STEP, SUGGEST_STEP, PII_STEP]
ADD_STEPS = [MAP_STEP, SUGGEST_STEP, *pipelines.ADD_GRAPH_STEPS]


def is_ontology_schema(schema: dict | None) -> bool:
    return bool(schema and (schema.get("ontology") or {}).get("name") == "servicenow-csdm")


def _folder(kb_name: str) -> Path:
    return Path(get_settings().upload_dir) / kb_name


# ------------------------------------------------------------------ raw snapshots
def save_snapshot(kb_name: str, raw: list[tuple[str, Table]]) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = _folder(kb_name) / f"{uuid.uuid4().hex[:8]}_servicenow_raw_{stamp}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "fetched_at": dt.datetime.now(dt.UTC).isoformat(),
        "tables": [{"source": s, "name": t.name, "columns": t.columns, "rows": t.rows} for s, t in raw],
    }
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(body, f, default=str)
    return str(path)


def load_snapshots(paths: list[str]) -> list[tuple[str, Table]]:
    raw = []
    for p in paths:
        if not Path(p).exists():
            log.warning("ServiceNow snapshot %s is missing; skipped", p)
            continue
        with gzip.open(p, "rt", encoding="utf-8") as f:
            body = json.load(f)
        raw += [(t["source"], Table(t["name"], t["columns"], t["rows"])) for t in body["tables"]]
    return raw


def fetch(connection_row: dict, datasets: list[Dataset], since: str | None, say) -> tuple[list, list[str]]:
    """Read every dataset. A table this instance doesn't have (plugin not installed: HTTP 404) or that the
    account may not read (HTTP 403) is skipped with a note instead of failing the whole pull."""
    raw, skipped, first_error = [], [], None
    with connection_store.open_connector(connection_row) as conn:
        for ds in datasets:
            say(f"Reading {ds.source}")
            try:
                raw.append((ds.source, conn.fetch_table(ds, since=since, progress=say)))
            except ConnectorError as exc:
                if "HTTP 404" not in str(exc) and "HTTP 403" not in str(exc):
                    raise
                first_error = first_error or exc
                skipped.append(ds.source)
    if not any(t.rows for _, t in raw):
        if first_error and not raw:
            raise first_error
        raise ConnectorError("The source returned no rows" + (" changed since then" if since else ""))
    return raw, skipped


def _fetched(raw, skipped) -> str:
    text = ", ".join(f"{s}: {len(t.rows):,} rows" for s, t in raw if t.rows)
    if skipped:
        text += f". Not on this instance or not readable: {', '.join(skipped)}"
    return text


# ------------------------------------------------------------------ mapping
def effective(kb_name: str) -> tuple[template.Ontology, set]:
    mapped, ignored = store.decided(kb_name)
    return template.load().with_mappings(mapped), ignored


def map_to_schema(
    kb_name: str, raw: list[tuple[str, Table]], known_labels: set[str] = frozenset()
) -> tuple[dict, mapper.MapResult, template.Ontology]:
    """Map, write the workbook and return the schema for it (source_path set)."""
    ont, ignored = effective(kb_name)
    result = mapper.map_tables(ont, raw, ignored, known_labels)
    if not result.tables:
        raise ConnectorError(
            "None of the pulled ServiceNow data maps to the CSDM ontology yet. Map the items listed under "
            "'Not mapped' and apply them."
        )
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    name = f"servicenow_csdm_{stamp}.xlsx"
    path = _folder(kb_name) / f"{uuid.uuid4().hex[:8]}_{name}"
    write_workbook(path, result.tables)
    sheets = read_table_file(path, name)
    summary = extraction.sheets_summary(sheets)
    rows = {s.name: len(s.rows) for s in sheets}
    nodes = [n for n in result.nodes if n["sheet"] in summary]
    for n in nodes:
        n["count"] = rows.get(n["sheet"], 0)
    labels = {n["label"] for n in nodes} | set(known_labels)
    rels = [
        r
        for r in result.relationships
        if r["sheet"] in summary and r["from"]["label"] in labels and r["to"]["label"] in labels
    ]
    schema = {
        "source_file": name,
        "source_path": str(path),
        "sheets": summary,
        "nodes": nodes,
        "relationships": rels,
        "ontology": {
            "name": ont.name,
            "version": ont.version,
            "mapped_at": dt.datetime.now(dt.UTC).isoformat(),
            "stats": {k: dict(v) if isinstance(v, dict) else v for k, v in result.stats.items()},
        },
    }
    return schema, result, ont


def record_unmapped(job_id: int | None, step_index: int | None, kb_name: str, ont, result: mapper.MapResult) -> str:
    """Queue what isn't mapped and ask the LLM about the new items. Returns a summary line."""
    pending = store.record(kb_name, result.unmapped)
    store.resolve_mapped(kb_name, ont, result.unmapped)
    limit = get_settings().ontology_max_llm_suggestions
    asked = 0
    for item in pending[:limit]:
        store.save_suggestion(item["id"], suggest.suggest(ont, item))
        asked += 1
        if job_id is not None and step_index is not None:
            jobs.step(job_id, step_index, "running", f"{asked} of {min(len(pending), limit)} items")
    c = store.counts(kb_name)
    text = f"{c['pending']} items not mapped" + (f", {asked} new suggestions" if asked else "")
    if len(pending) > limit:
        text += f" ({len(pending) - limit} more without a suggestion yet)"
    return text


def _map_summary(result: mapper.MapResult) -> str:
    classes = result.stats["classes"]
    text = f"{sum(classes.values()):,} records in {len(classes)} CSDM classes"
    rels = sum(result.stats["relationships"].values())
    if rels:
        text += f", {rels:,} relationship rows"
    if result.unmapped:
        text += f"; {len(result.unmapped)} items not in the ontology"
    return text


def scan_pii(schema: dict, sheets_wanted: set[str] | None = None) -> list[dict]:
    sheets = read_table_file(schema["source_path"], schema["source_file"])
    found = []
    for s in sheets:
        if sheets_wanted is not None and s.name not in sheets_wanted:
            continue
        for item in extraction.detect_pii(s, extraction.stored_columns(schema, s.name)):
            found.append({"sheet": s.name, **item})
    return nist_pii.assess(found, schema["sheets"])


def merge_schema(base: dict, new: dict) -> dict:
    """Grow an approved schema by what a new mapping adds. Existing node types, properties and relationships
    (including the reviewer's edits) are kept; new ones are appended."""
    out = json.loads(json.dumps(base, default=str))
    sheets = out.setdefault("sheets", {})
    for name, prof in new["sheets"].items():
        old_cols = (sheets.get(name) or {}).get("columns", {})
        # a refresh may pull fewer fields: keep the columns the approved schema already uses
        sheets[name] = {**prof, "columns": {**old_cols, **prof.get("columns", {})}}
    for k in ("source_file", "source_path"):
        out[k] = new[k]
    out["ontology"] = {**(out.get("ontology") or {}), **new["ontology"]}
    by_label = {n["label"]: n for n in out["nodes"]}
    for n in new["nodes"]:
        have = by_label.get(n["label"])
        if not have:
            out["nodes"].append({**n, "id": f"n{len(out['nodes']) + 1}"})
            by_label[n["label"]] = out["nodes"][-1]
            continue
        names = {have["key"]["name"]} | {p["name"] for p in have.get("properties", [])}
        have.setdefault("properties", []).extend(p for p in n["properties"] if p["name"] not in names)
        have["sheet"] = n["sheet"]

    def sig(r):
        return (r["sheet"], r["type"], r["from"]["label"], r["from"]["column"], r["to"]["label"], r["to"]["column"])

    seen = {sig(r) for r in out["relationships"]}
    ids = {r["id"] for r in out["relationships"]}
    n = len(out["relationships"])
    for r in new["relationships"]:
        if sig(r) in seen:
            continue
        n += 1
        while f"r{n}" in ids:
            n += 1
        out["relationships"].append({**r, "id": f"r{n}"})
        ids.add(f"r{n}")
        seen.add(sig(r))
    return gs.clean(out)


def _snapshots(schema: dict | None) -> list[str]:
    return list(((schema or {}).get("ontology") or {}).get("snapshots") or [])


def _save_schema(kb_name: str, column: str, schema: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            f"UPDATE kb_catalog SET {column} = %s WHERE kb_name = %s", (json.dumps(schema, default=str), kb_name)
        )


# ------------------------------------------------------------------ jobs (job_id first, see jobs.submit)
def graph_from_connection(job_id: int, kb_name: str, connection_row: dict, datasets: list[dict]) -> None:
    """New knowledge base: fetch, map, suggest, scan PII -> awaiting review."""
    say = lambda msg: jobs.step(job_id, 0, "running", msg)  # noqa: E731
    say("Connecting")
    raw, skipped = fetch(connection_row, [Dataset.from_dict(d) for d in datasets], None, say)
    snapshot = save_snapshot(kb_name, raw)
    jobs.step(job_id, 0, "done", _fetched(raw, skipped))
    _create_from_raw(job_id, 1, kb_name, raw, [snapshot])


def _create_from_raw(job_id: int, first: int, kb_name: str, raw, snapshots: list[str]) -> None:
    jobs.step(job_id, first, "running")
    schema, result, ont = map_to_schema(kb_name, raw)
    schema["ontology"]["snapshots"] = snapshots
    jobs.step(job_id, first, "done", _map_summary(result))
    jobs.step(job_id, first + 1, "running")
    jobs.step(job_id, first + 1, "done", record_unmapped(job_id, first + 1, kb_name, ont, result))
    jobs.step(job_id, first + 2, "running")
    schema["pii"] = scan_pii(schema)
    jobs.step(job_id, first + 2, "done", f"{len(schema['pii'])} PII columns")
    store.mark_applied(kb_name)
    kb.set_status(kb_name, "awaiting_review", None, draft_schema=json.dumps(schema, default=str))
    pipelines.sync_graph_pii(kb_name, schema)


def _grow_and_load(
    job_id: int, first: int, kb_name: str, raw, user_id: str, merge_existing: bool, skip_invalid: bool
) -> None:
    approved = kb.get_catalog(kb_name)["approved_schema"]
    jobs.step(job_id, first, "running")
    known = {n["label"] for n in approved["nodes"]}
    new, result, ont = map_to_schema(kb_name, raw, known_labels=known)
    jobs.step(job_id, first, "done", _map_summary(result))
    jobs.step(job_id, first + 1, "running")
    jobs.step(job_id, first + 1, "done", record_unmapped(job_id, first + 1, kb_name, ont, result))
    merged = merge_schema(approved, new)
    added_sheets = {n["sheet"] for n in merged["nodes"] if n["label"] not in known}
    if added_sheets:
        merged["pii"] = list(approved.get("pii", [])) + scan_pii(new, added_sheets)
    _save_schema(kb_name, "approved_schema", merged)
    _save_schema(kb_name, "draft_schema", merged)
    if added_sheets:
        pipelines.sync_graph_pii(kb_name, merged)
    with jobs.steps_after(job_id, first + 2):
        pipelines.graph_add_data(
            job_id, kb_name, merged["source_path"], merged["source_file"], user_id, merge_existing, skip_invalid
        )
    store.mark_applied(kb_name)


def add_from_connection(
    job_id: int,
    kb_name: str,
    connection_row: dict,
    datasets: list[dict],
    user_id: str,
    since: str | None,
    merge_existing: bool,
    skip_invalid: bool,
) -> None:
    """Refresh an ontology-mapped KB from ServiceNow."""
    say = lambda msg: jobs.step(job_id, 0, "running", msg)  # noqa: E731
    say("Connecting")
    raw, skipped = fetch(connection_row, [Dataset.from_dict(d) for d in datasets], since, say)
    snapshot = save_snapshot(kb_name, raw)
    jobs.step(job_id, 0, "done", _fetched(raw, skipped))
    cat = kb.get_catalog(kb_name)
    schema = cat["approved_schema"]
    schema.setdefault("ontology", {})["snapshots"] = _snapshots(schema) + [snapshot]
    _save_schema(kb_name, "approved_schema", schema)
    _grow_and_load(job_id, 1, kb_name, raw, user_id, merge_existing, skip_invalid)


def apply_steps(status: str) -> list[str]:
    return (
        [REMAP_STEP, SUGGEST_STEP, *pipelines.ADD_GRAPH_STEPS]
        if status == "ready"
        else [REMAP_STEP, SUGGEST_STEP, PII_STEP]
    )


def apply_mappings(job_id: int, kb_name: str, user_id: str) -> None:
    """Map everything pulled so far again with the current manual mappings. A built graph grows by what is now
    mapped; a KB still awaiting review gets a new draft."""
    cat = kb.get_catalog(kb_name)
    schema = cat["approved_schema"] if cat["status"] == "ready" else cat["draft_schema"]
    snapshots = _snapshots(schema)
    raw = load_snapshots(snapshots)
    if not raw:
        raise ConnectorError("The saved ServiceNow data is gone; refresh the knowledge base from ServiceNow instead")
    if cat["status"] == "ready":
        _grow_and_load(job_id, 0, kb_name, raw, user_id, True, True)
        kb.set_status(kb_name, "ready", None)
    else:
        _create_from_raw(job_id, 0, kb_name, raw, snapshots)
