"""Map records pulled from ServiceNow onto the CSDM ontology.

Input: the raw tables the connector read (source table + rows, already flattened: a reference field `x` holds the
sys_id and `x_name` the display value). Output:
  * one sheet per ontology class (rows grouped by class, de-duplicated by sys_id), one sheet per cmdb_rel_ci
    relationship type and one per link table: written to a workbook, so the normal loader builds the graph;
  * the graph schema in the app's usual format, generated from the template (no LLM involved);
  * everything the template doesn't cover: unknown classes (tables / sys_class_name), fields with data that no
    property or reference maps, and cmdb_rel_ci relationship types or class pairs without a rule.
    These go to the "not mapped" queue (store.py), get an LLM suggestion and must be mapped by a person.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app.connectors.base import Table
from app.ontology.template import Ontology

SYS_ID = re.compile(r"^[0-9a-fA-F]{32}$")
REL_SHEET = "CI rel - "
MAX_SAMPLES = 5


@dataclass
class MapResult:
    tables: list[Table]  # sheet name -> rows, written to the workbook
    nodes: list[dict]  # schema nodes (without counts)
    relationships: list[dict]
    unmapped: list[dict]  # {"item_kind", "item_key", "detail"}
    stats: dict = field(default_factory=dict)


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _samples(values) -> list[str]:
    out = []
    for v in values:
        s = str(v)[:80]
        if not _blank(v) and s not in out:
            out.append(s)
        if len(out) == MAX_SAMPLES:
            break
    return out


def combine(raw_tables: list[tuple[str, Table]]) -> list[tuple[str, Table]]:
    """Several pulls of the same table (initial load + refreshes): one table, the latest row per sys_id wins."""
    merged: dict[str, tuple[list[str], dict]] = {}
    for source, t in raw_tables:
        cols, rows = merged.setdefault(source, ([], {}))
        cols += [c for c in t.columns if c not in cols]
        for i, r in enumerate(t.rows):
            rows[r.get("sys_id") or f"_{len(rows)}_{i}"] = r
    return [(s, Table(s, cols, list(rows.values()))) for s, (cols, rows) in merged.items()]


def map_tables(
    ont: Ontology,
    raw_tables: list[tuple[str, Table]],
    ignored: set[tuple[str, str]] = frozenset(),
    known_labels: set[str] = frozenset(),
) -> MapResult:
    """known_labels: classes already in the graph (a refresh may pull incidents only; their users exist)."""
    by_class: dict[str, dict[str, dict]] = defaultdict(dict)  # label -> sys_id -> row
    class_of: dict[str, str] = {}  # sys_id -> label
    unknown: dict[str, dict] = {}  # table / sys_class_name -> detail
    rel_rows: list[dict] = []
    link_rows: dict[str, list[dict]] = defaultdict(list)
    source_of: dict[str, set[str]] = defaultdict(set)

    for source, t in combine(raw_tables):
        if source == ont.rel_table:
            rel_rows += t.rows
            continue
        if ont.link_table(source):
            link_rows[source] += t.rows
            continue
        for row in t.rows:
            c = ont.classify(source, row)
            if c is None:
                key = str(row.get("sys_class_name") or source).strip()
                if ("class", key) in ignored:
                    continue
                d = unknown.setdefault(
                    key, {"table": key, "read_from": source, "rows": 0, "columns": Counter(), "examples": []}
                )
                d["rows"] += 1
                for k, v in row.items():
                    if not _blank(v):
                        d["columns"][k] += 1
                name = row.get("name") or row.get("number") or row.get("short_description")
                if name and len(d["examples"]) < MAX_SAMPLES and str(name) not in d["examples"]:
                    d["examples"].append(str(name)[:80])
                continue
            sid = row.get("sys_id")
            if _blank(sid):
                continue
            by_class[c.label][sid] = row
            class_of[str(sid).lower()] = c.label
            source_of[c.label].add(source)

    present = set(by_class) | set(known_labels)
    tables, nodes, rels, unmapped = [], [], [], []
    stats = {"classes": {}, "relationships": {}, "unresolved_relationships": 0}

    # ------------------------------------------------------------------ one sheet per class
    for c in ont.classes:
        rows = list(by_class.get(c.label, {}).values())
        if not rows:
            continue
        stats["classes"][c.label] = len(rows)
        filled = Counter(k for r in rows for k, v in r.items() if not _blank(v))
        columns = ["sys_id"]
        props = []
        for p in c.properties:
            if any(p["field"] in r for r in rows):
                columns.append(p["field"])
                props.append({"name": p["name"], "column": p["field"], "type": p["type"]})
        for ref in c.references:
            f = ref["field"]
            if not any(f in r for r in rows):
                continue
            columns.append(f)
            name_col = f"{f}_name" if any(f"{f}_name" in r for r in rows) else None
            if name_col:
                columns.append(name_col)
            prop_name = f"{f}_name" if not f.endswith("_name") else f
            taken = {p["name"] for p in props}
            if prop_name not in taken and prop_name != "sys_id":
                # the readable name stays on the node even when the target record isn't in the graph
                props.append({"name": prop_name, "column": name_col or f, "type": "string"})
            for target in ref["to"]:
                if target in present:
                    linked = sum(1 for r in rows if r.get(f) and class_of.get(str(r[f]).lower()) == target)
                    rels.append(
                        {
                            "count": linked,
                            "type": ref["type"],
                            "sheet": c.label,
                            "from": {"label": c.label, "column": "sys_id"},
                            "to": {"label": target, "column": f},
                            "properties": [],
                            "required": False,
                            "on_unknown": "skip",
                            "group": f"{c.label}.{f}",
                        }
                    )
        nodes.append(
            {
                "label": c.label,
                "sheet": c.label,
                "role": "row",
                "key": {"name": "sys_id", "column": "sys_id"},
                "properties": props,
                "domain": c.domain,
                "description": c.description,
            }
        )
        tables.append(Table(c.label, list(dict.fromkeys(columns)), [{k: r.get(k) for k in columns} for r in rows]))

        # fields with data that nothing maps
        mapped = c.mapped_fields() | ont.ignore_fields
        refs_in_data = {k[: -len("_name")] for k in filled if k.endswith("_name") and k[: -len("_name")] in filled}
        for col in sorted(filled):
            if col in mapped or (col.endswith("_name") and col[: -len("_name")] in refs_in_data):
                continue
            key = f"{c.label}.{col}"
            if ("field", key) in ignored:
                continue
            is_ref = col in refs_in_data or all(SYS_ID.match(str(r.get(col) or "")) for r in rows if r.get(col))
            shown = f"{col}_name" if col in refs_in_data else col
            target_classes = Counter(class_of.get(str(r.get(col)).lower()) for r in rows if r.get(col) and is_ref)
            target_classes.pop(None, None)
            unmapped.append(
                {
                    "item_kind": "field",
                    "item_key": key,
                    "detail": {
                        "class": c.label,
                        "field": col,
                        "tables": sorted(source_of[c.label]),
                        "rows_with_value": filled[col],
                        "rows": len(rows),
                        "is_reference": is_ref,
                        "points_to": [lab for lab, _ in target_classes.most_common()],
                        "samples": _samples(r.get(shown) for r in rows),
                    },
                }
            )

    # ------------------------------------------------------------------ unknown classes
    for key, d in unknown.items():
        unmapped.append(
            {
                "item_kind": "class",
                "item_key": key,
                "detail": {
                    "table": key,
                    "read_from": d["read_from"],
                    "rows": d["rows"],
                    "columns": [c for c, _ in d["columns"].most_common(25) if c not in ont.ignore_fields],
                    "examples": d["examples"],
                },
            }
        )

    # ------------------------------------------------------------------ cmdb_rel_ci
    by_type: dict[str, list[dict]] = defaultdict(list)
    unknown_types: dict[str, dict] = {}
    odd_pairs: dict[tuple, dict] = {}
    for r in rel_rows:
        rtype = str(r.get("type_name") or r.get("type") or "").strip()
        if not rtype or _blank(r.get("parent")) or _blank(r.get("child")):
            continue
        parent_cls = class_of.get(str(r["parent"]).lower())
        child_cls = class_of.get(str(r["child"]).lower())
        rules = ont.rules_for(rtype)
        if not rules:
            if ("relationship", rtype) in ignored:
                continue
            d = unknown_types.setdefault(rtype, {"servicenow_type": rtype, "rows": 0, "pairs": Counter()})
            d["rows"] += 1
            if parent_cls and child_cls:
                d["pairs"][(parent_cls, child_cls)] += 1
            continue
        if parent_cls and child_cls:
            allowed = {(x["parent"], x["child"]) for x in rules}
            if (parent_cls, child_cls) not in allowed:
                key = f"{rtype} | {parent_cls} -> {child_cls}"
                if ("relationship", key) not in ignored:
                    d = odd_pairs.setdefault(
                        key, {"servicenow_type": rtype, "rows": 0, "pairs": Counter({(parent_cls, child_cls): 0})}
                    )
                    d["rows"] += 1
                    d["pairs"][(parent_cls, child_cls)] += 1
                continue
        else:
            # an end isn't in this pull: the loader links it if that record is already in the graph
            stats["unresolved_relationships"] += 1
        by_type[rules[0]["servicenow_type"]].append(r)

    for rtype, rows in by_type.items():
        sheet = f"{REL_SHEET}{rtype.split('::')[0]}"[:31]
        tables.append(
            Table(
                sheet,
                ["sys_id", "parent", "child"],
                [{k: r.get(k) for k in ("sys_id", "parent", "child")} for r in rows],
            )
        )
        stats["relationships"][rtype] = len(rows)
        for rule in ont.rules_for(rtype):
            if rule["parent"] not in present or rule["child"] not in present:
                continue
            a, b = ("child", "parent") if rule.get("reverse") else ("parent", "child")
            linked = sum(
                1
                for r in rows
                if class_of.get(str(r["parent"]).lower()) == rule["parent"]
                and class_of.get(str(r["child"]).lower()) == rule["child"]
            )
            rels.append(
                {
                    "count": linked,
                    "type": rule["type"],
                    "sheet": sheet,
                    "from": {"label": rule[a], "column": a},
                    "to": {"label": rule[b], "column": b},
                    "properties": [],
                    "required": False,
                    "on_unknown": "skip",
                    "group": sheet,
                }
            )
    for key, d in list(unknown_types.items()) + list(odd_pairs.items()):
        unmapped.append(
            {
                "item_kind": "relationship",
                "item_key": key,
                "detail": {
                    "servicenow_type": d["servicenow_type"],
                    "rows": d["rows"],
                    "pairs": [{"parent": p, "child": c, "rows": n} for (p, c), n in d["pairs"].most_common()],
                },
            }
        )

    # ------------------------------------------------------------------ link tables
    for source, rows in link_rows.items():
        lt = ont.link_table(source)
        f_from, f_to = lt["from"]["field"], lt["to"]["field"]
        sheet = lt.get("sheet") or source
        tables.append(
            Table(sheet, ["sys_id", f_from, f_to], [{k: r.get(k) for k in ("sys_id", f_from, f_to)} for r in rows])
        )
        stats["relationships"][lt["type"]] = len(rows)
        for a in lt["from"]["class"]:
            for b in lt["to"]["class"]:
                if a in present and b in present:
                    linked = sum(
                        1
                        for r in rows
                        if class_of.get(str(r.get(f_from)).lower()) == a and class_of.get(str(r.get(f_to)).lower()) == b
                    )
                    rels.append(
                        {
                            "count": linked,
                            "type": lt["type"],
                            "sheet": sheet,
                            "from": {"label": a, "column": f_from},
                            "to": {"label": b, "column": f_to},
                            "properties": [],
                            "required": False,
                            "on_unknown": "skip",
                            "group": sheet,
                        }
                    )

    for i, n in enumerate(nodes, 1):
        n["id"] = f"n{i}"
    for i, r in enumerate(rels, 1):
        r["id"] = f"r{i}"
    stats["unmapped"] = Counter(u["item_kind"] for u in unmapped)
    return MapResult(tables, nodes, rels, unmapped, stats)
