"""The "not mapped" queue (table ontology_unmapped): what the CSDM template doesn't cover, the LLM's suggestion,
and the person's decision. Decisions are per knowledge base and are applied the next time it is mapped."""

import json

from app.db import get_conn
from app.ontology.template import (
    FIELD_RE,
    LABEL_RE,
    PROP_RE,
    REL_RE,
    TYPES,
    Ontology,
    OntologyError,
)

ACTIONS = {
    "class": {"map_to_class", "new_class", "ignore"},
    "field": {"property", "reference", "ignore"},
    "relationship": {"relationship", "ignore"},
}


def _json(v):
    return json.dumps(v, default=str)


def record(kb_name: str, items: list[dict]) -> list[dict]:
    """Add or refresh items seen in a mapping run. Decided items keep their decision; a pending item whose
    data changed loses its stale suggestion. Returns the pending rows that still need a suggestion."""
    if not items:
        return []
    with get_conn() as conn:
        for it in items:
            conn.execute(
                """INSERT INTO ontology_unmapped (kb_name, item_kind, item_key, detail, occurrences)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (kb_name, item_kind, item_key) DO UPDATE
                   SET detail = EXCLUDED.detail, last_seen = now(),
                       occurrences = ontology_unmapped.occurrences + EXCLUDED.occurrences,
                       suggestion = CASE
                           WHEN ontology_unmapped.status = 'pending'
                                AND ontology_unmapped.detail->'pairs' IS DISTINCT FROM EXCLUDED.detail->'pairs'
                           THEN NULL ELSE ontology_unmapped.suggestion END""",
                (
                    kb_name,
                    it["item_kind"],
                    it["item_key"],
                    _json(it["detail"]),
                    int(it["detail"].get("rows_with_value") or it["detail"].get("rows") or 1),
                ),
            )
        return conn.execute(
            """SELECT * FROM ontology_unmapped
               WHERE kb_name = %s AND status = 'pending' AND suggestion IS NULL ORDER BY id""",
            (kb_name,),
        ).fetchall()


def items(kb_name: str, status: str | None = None) -> list[dict]:
    q = "SELECT * FROM ontology_unmapped WHERE kb_name = %s"
    args = [kb_name]
    if status:
        q += " AND status = %s"
        args.append(status)
    with get_conn() as conn:
        return conn.execute(q + " ORDER BY item_kind, item_key", args).fetchall()


def get(kb_name: str, item_id: int) -> dict | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM ontology_unmapped WHERE kb_name = %s AND id = %s", (kb_name, item_id)
        ).fetchone()


def counts(kb_name: str) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT status, count(*) AS n,
                      count(*) FILTER (WHERE status = 'mapped' AND (applied_at IS NULL OR applied_at < decided_at))
                          AS not_applied
               FROM ontology_unmapped WHERE kb_name = %s GROUP BY status""",
            (kb_name,),
        ).fetchall()
    out = {"pending": 0, "mapped": 0, "ignored": 0, "not_applied": 0}
    for r in rows:
        out[r["status"]] = r["n"]
        out["not_applied"] += r["not_applied"]
    return out


def save_suggestion(item_id: int, suggestion: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE ontology_unmapped SET suggestion = %s, suggested_at = now() WHERE id = %s",
            (_json(suggestion), item_id),
        )


def decided(kb_name: str) -> tuple[list[dict], set[tuple[str, str]]]:
    """(mapped items to layer over the template, {(kind, key)} the person chose to ignore)."""
    rows = items(kb_name)
    return (
        [r for r in rows if r["status"] == "mapped"],
        {(r["item_kind"], r["item_key"]) for r in rows if r["status"] == "ignored"},
    )


def mark_applied(kb_name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE ontology_unmapped SET applied_at = now() WHERE kb_name = %s AND status = 'mapped'", (kb_name,)
        )


def resolve_mapped(kb_name: str, ont: Ontology, still_unmapped: list[dict]) -> None:
    """Items that are mapped (or now covered by the template) and no longer show up as unmapped: nothing to do.
    Pending items the template now covers (e.g. after a template update) are closed as mapped by the template."""
    seen = {(u["item_kind"], u["item_key"]) for u in still_unmapped}
    with get_conn() as conn:
        for r in conn.execute(
            "SELECT id, item_kind, item_key FROM ontology_unmapped WHERE kb_name = %s AND status = 'pending'",
            (kb_name,),
        ).fetchall():
            if (r["item_kind"], r["item_key"]) not in seen and _covered(ont, r["item_kind"], r["item_key"]):
                conn.execute(
                    """UPDATE ontology_unmapped SET status = 'mapped', mapping = %s, decided_by = 'template',
                              decided_at = now(), applied_at = now() WHERE id = %s""",
                    (_json({"action": "template"}), r["id"]),
                )


def _covered(ont: Ontology, kind: str, key: str) -> bool:
    if kind == "class":
        return any(key in c.tables for c in ont.classes)
    if kind == "field":
        label, _, fld = key.partition(".")
        c = ont.cls(label)
        return bool(c and fld in c.mapped_fields())
    rtype, _, pair = key.partition(" | ")
    rules = ont.rules_for(rtype)
    if not pair:
        return bool(rules)
    parent, _, child = pair.partition(" -> ")
    return any(r["parent"] == parent and r["child"] == child for r in rules)


def validate_mapping(ont: Ontology, item: dict, body: dict) -> dict:
    """The person's decision, checked against the ontology. Returns the cleaned mapping."""
    kind, action = item["item_kind"], body.get("action")
    if action not in ACTIONS[kind]:
        raise OntologyError(f"For a {kind}, action must be one of: {', '.join(sorted(ACTIONS[kind]))}")
    if action == "ignore":
        return {"action": "ignore"}
    labels = set(ont.labels())
    if kind == "class":
        if action == "map_to_class":
            if body.get("class") not in labels:
                raise OntologyError("Choose one of the ontology's classes")
            return {"action": action, "class": body["class"]}
        label = str(body.get("label") or "").strip()
        if not LABEL_RE.match(label):
            raise OntologyError("A new class needs a PascalCase name of at most 31 characters, e.g. LinuxCluster")
        if label in labels:
            raise OntologyError(f"{label} already exists; map to it instead")
        domains = {d["id"] for d in ont.domains}
        domain = body.get("domain") or "manage_technical_services"
        if domain not in domains:
            raise OntologyError(f"domain must be one of {', '.join(sorted(domains))}")
        return {
            "action": action,
            "label": label,
            "domain": domain,
            "description": str(body.get("description") or "")[:300],
        }
    if kind == "field":
        label, _, fld = item["item_key"].partition(".")
        if not FIELD_RE.match(fld):
            raise OntologyError("Bad field name")
        if action == "property":
            name = str(body.get("name") or "").strip()
            c = ont.cls(label)
            if not PROP_RE.match(name) or name == "sys_id":
                raise OntologyError("Property names are lower_snake_case, e.g. patch_window")
            if c and name in {p["name"] for p in c.properties}:
                raise OntologyError(f"{label} already has a property {name}")
            typ = body.get("type") or "string"
            if typ not in TYPES:
                raise OntologyError(f"type must be one of {', '.join(TYPES)}")
            return {"action": action, "name": name, "type": typ}
        targets = body.get("to") or []
        targets = [targets] if isinstance(targets, str) else targets
        if not targets or any(t not in labels and t != "@ci" for t in targets):
            raise OntologyError("Choose the class(es) the reference points to")
        rtype = str(body.get("type") or "").strip().upper()
        if not REL_RE.match(rtype):
            raise OntologyError("Relationship types are UPPER_SNAKE_CASE, e.g. PATCHED_BY")
        return {"action": action, "to": targets, "type": rtype}
    rtype = str(body.get("type") or "").strip().upper()
    if not REL_RE.match(rtype):
        raise OntologyError("Relationship types are UPPER_SNAKE_CASE, e.g. HOSTED_ON")
    out = {"action": action, "type": rtype, "reverse": bool(body.get("reverse"))}
    pairs = (item.get("detail") or {}).get("pairs") or []
    if body.get("parent") or body.get("child"):
        if body.get("parent") not in labels or body.get("child") not in labels:
            raise OntologyError("parent and child must be ontology classes")
        out.update(parent=body["parent"], child=body["child"])
    elif not pairs:
        raise OntologyError(
            "Neither end of these relationships is in the pulled data, so say which classes it links (parent, child)"
        )
    return out


def decide(kb_name: str, item_id: int, mapping: dict, user_id: str) -> dict:
    status = "ignored" if mapping["action"] == "ignore" else "mapped"
    with get_conn() as conn:
        return conn.execute(
            """UPDATE ontology_unmapped SET status = %s, mapping = %s, decided_by = %s, decided_at = now()
               WHERE kb_name = %s AND id = %s RETURNING *""",
            (status, _json(mapping), user_id, kb_name, item_id),
        ).fetchone()


def reopen(kb_name: str, item_id: int) -> dict | None:
    with get_conn() as conn:
        return conn.execute(
            """UPDATE ontology_unmapped SET status = 'pending', mapping = NULL, decided_by = NULL, decided_at = NULL
               WHERE kb_name = %s AND id = %s RETURNING *""",
            (kb_name, item_id),
        ).fetchone()
