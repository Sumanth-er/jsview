"""The ServiceNow CSDM ontology template (servicenow_csdm.json) and the knowledge base's manual mappings on top.

The template is a versioned JSON file in the repo: it changes rarely, is reviewed like code and can be handed to
the LLM as is. What people decide at runtime (manual mappings, ignored items) lives in Postgres
(table ontology_unmapped, see app/ontology/store.py) and is layered over the template by `Ontology.with_mappings`.
"""

import copy
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

TEMPLATE_PATH = Path(__file__).with_name("servicenow_csdm.json")

LABEL_RE = re.compile(r"^[A-Z][A-Za-z0-9]{0,30}$")  # also a sheet name, so at most 31 characters
REL_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
PROP_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
FIELD_RE = re.compile(r"^[A-Za-z0-9_.]{1,120}$")
TABLE_RE = re.compile(r"^[a-z0-9_]{1,80}$")
TYPES = ("string", "integer", "float", "date", "boolean")
ALL_CI = "@ci"


class OntologyError(ValueError):
    pass


@dataclass
class OntClass:
    label: str
    domain: str
    tables: list[str]
    description: str = ""
    fetch: list[str] = field(default_factory=list)
    where: dict = field(default_factory=dict)  # {field: [values]} or {table: {field: [values]}}
    is_ci: bool = False
    properties: list[dict] = field(default_factory=list)  # {"field", "name", "type"}
    references: list[dict] = field(default_factory=list)  # {"field", "to": [labels], "type"}
    manual: bool = False  # added by a manual mapping

    def where_for(self, table: str) -> dict:
        if table in self.where and isinstance(self.where[table], dict):
            return self.where[table]
        return {k: v for k, v in self.where.items() if not isinstance(v, dict)}

    def matches(self, table: str, row: dict) -> bool:
        for fld, allowed in self.where_for(table).items():
            value = str(row.get(fld) or "").strip().lower()
            if value not in {str(a).strip().lower() for a in allowed}:
                return False
        return True

    def mapped_fields(self) -> set[str]:
        out = {p["field"] for p in self.properties}
        for r in self.references:
            out |= {r["field"], f"{r['field']}_name"}
        return out


@dataclass
class Ontology:
    name: str
    version: str
    domains: list[dict]
    classes: list[OntClass]
    link_tables: list[dict]
    rel_table: str
    rel_rules: list[dict]  # expanded: {"servicenow_type", "parent", "child", "type", "manual"}
    ignore_fields: set[str]
    raw: dict

    # ------------------------------------------------------------------ lookups
    def cls(self, label: str) -> OntClass | None:
        return next((c for c in self.classes if c.label == label), None)

    def labels(self) -> list[str]:
        return [c.label for c in self.classes]

    def ci_labels(self) -> list[str]:
        return [c.label for c in self.classes if c.is_ci]

    def expand(self, targets) -> list[str]:
        out = []
        for t in [targets] if isinstance(targets, str) else targets:
            out += self.ci_labels() if t == ALL_CI else [t]
        return list(dict.fromkeys(out))

    def link_table(self, table: str) -> dict | None:
        return next((lt for lt in self.link_tables if lt["table"] == table), None)

    def rel_types(self) -> set[str]:
        return {r["servicenow_type"].lower() for r in self.rel_rules}

    def rules_for(self, servicenow_type: str) -> list[dict]:
        t = (servicenow_type or "").strip().lower()
        return [r for r in self.rel_rules if r["servicenow_type"].lower() == t]

    def classify(self, table: str, row: dict) -> OntClass | None:
        """The class of a record: its sys_class_name first (most specific), else the table it was read from
        (a subclass the template doesn't list, e.g. cmdb_ci_esx_server read from cmdb_ci_server)."""
        own = str(row.get("sys_class_name") or "").strip()
        for name in dict.fromkeys(x for x in (own, table) if x):
            candidates = [c for c in self.classes if name in c.tables]
            if not candidates:
                continue
            # classes with a condition first; a class without one is the fallback for that table
            for c in sorted(candidates, key=lambda c: not c.where_for(name)):
                if c.matches(name, row):
                    return c
            return None  # the table is known but no class accepts this record (e.g. another classification)
        return None

    def graph_relationship_types(self) -> list[str]:
        types = {r["type"] for c in self.classes for r in c.references}
        types |= {r["type"] for r in self.rel_rules} | {lt["type"] for lt in self.link_tables}
        return sorted(types)

    # ------------------------------------------------------------------ manual mappings
    def with_mappings(self, items: list[dict]) -> "Ontology":
        """A copy with the knowledge base's mapped items applied. items: rows of ontology_unmapped with
        status 'mapped' (item_kind, item_key, mapping, detail)."""
        ont = copy.deepcopy(self)
        # classes first: fields and relationships may refer to a class created by a mapping
        order = {"class": 0, "field": 1, "relationship": 2}
        for item in sorted(items, key=lambda i: order.get(i["item_kind"], 9)):
            try:
                ont._apply(item)
            except OntologyError:
                continue  # the template changed under an old mapping; it shows up as not mapped again
        return ont

    def _apply(self, item: dict) -> None:
        m, kind, key = item["mapping"] or {}, item["item_kind"], item["item_key"]
        action = m.get("action")
        if kind == "class":
            if action == "map_to_class":
                c = self._need(m.get("class"))
                if key not in c.tables:
                    c.tables.append(key)
            elif action == "new_class":
                label = m.get("label")
                if self.cls(label):
                    raise OntologyError(f"{label} already exists")
                is_ci = key.startswith("cmdb_ci") or key == "service_offering"
                props = [] if is_ci else [{"field": f, "name": f, "type": "string"} for f in ("name", "number")]
                c = OntClass(
                    label=label,
                    domain=m.get("domain") or "manage_technical_services",
                    tables=[key],
                    description=m.get("description") or f"Mapped manually from ServiceNow table {key}",
                    is_ci=is_ci,
                    manual=True,
                    properties=props,
                )
                if is_ci:
                    _include(c, self.raw["mixins"]["ci"])
                self.classes.append(c)
        elif kind == "field":
            label, _, fld = key.partition(".")
            c = self._need(label)
            if action == "property" and fld not in c.mapped_fields():
                c.properties.append({"field": fld, "name": m["name"], "type": m.get("type") or "string"})
            elif action == "reference" and fld not in c.mapped_fields():
                c.references.append({"field": fld, "to": self.expand(m["to"]), "type": m["type"]})
        elif kind == "relationship":
            if action != "relationship":
                return
            pairs = [(p["parent"], p["child"]) for p in (item.get("detail") or {}).get("pairs", [])]
            if m.get("parent") and m.get("child"):
                pairs = [(m["parent"], m["child"])]
            servicenow_type = (item.get("detail") or {}).get("servicenow_type") or key.split(" | ")[0]
            for parent, child in pairs:
                if self.cls(parent) and self.cls(child):
                    self.rel_rules.append(
                        {
                            "servicenow_type": servicenow_type,
                            "parent": parent,
                            "child": child,
                            "type": m["type"],
                            "reverse": bool(m.get("reverse")),
                            "manual": True,
                        }
                    )

    def _need(self, label) -> OntClass:
        c = self.cls(label) if label else None
        if not c:
            raise OntologyError(f"Unknown class {label!r}")
        return c

    # ------------------------------------------------------------------ for the UI and the LLM
    def summary(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "domains": self.domains,
            "classes": [
                {
                    "label": c.label,
                    "domain": c.domain,
                    "description": c.description,
                    "tables": c.tables,
                    "is_ci": c.is_ci,
                    "manual": c.manual,
                    "properties": [{"field": p["field"], "name": p["name"], "type": p["type"]} for p in c.properties],
                    "references": [{"field": r["field"], "to": r["to"], "type": r["type"]} for r in c.references],
                }
                for c in self.classes
            ],
            "link_tables": self.link_tables,
            "ci_relationships": [
                {k: r[k] for k in ("servicenow_type", "parent", "child", "type", "reverse") if k in r}
                for r in self.rel_rules
            ],
            "relationship_types": self.graph_relationship_types(),
        }

    def describe_for_llm(self) -> str:
        lines = [f"Ontology {self.name} v{self.version} (ServiceNow CSDM). Domains:"]
        lines += [f"- {d['id']}: {d['label']}" for d in self.domains]
        lines.append("Classes (label [domain] tables: description):")
        for c in self.classes:
            lines.append(f"- {c.label} [{c.domain}] {', '.join(c.tables)}: {c.description}")
        lines.append("Graph relationship types already used: " + ", ".join(self.graph_relationship_types()))
        lines.append("cmdb_rel_ci rules (ServiceNow type: parent -> child = graph type):")
        for r in self.rel_rules:
            lines.append(f"- {r['servicenow_type']}: {r['parent']} -> {r['child']} = {r['type']}")
        return "\n".join(lines)

    def preset_datasets(self) -> list[dict]:
        """Everything the template knows how to map, as connector datasets (one per table to read)."""
        per_table: dict[str, list[str]] = {}
        for c in self.classes:
            for table in c.fetch or c.tables[:1]:
                fields = per_table.setdefault(table, ["sys_id", "sys_class_name"])
                wanted = [p["field"] for p in c.properties] + [r["field"] for r in c.references]
                wanted += [f for cond in [c.where_for(table)] for f in cond]
                fields += [f for f in dict.fromkeys(wanted) if f not in fields]
        datasets = [{"name": table, "source": table, "fields": fields} for table, fields in per_table.items()]
        for lt in self.link_tables:
            datasets.append(
                {
                    "name": lt["table"],
                    "source": lt["table"],
                    "fields": ["sys_id", lt["from"]["field"], lt["to"]["field"]],
                }
            )
        datasets.append(
            {"name": self.rel_table, "source": self.rel_table, "fields": ["sys_id", "parent", "child", "type"]}
        )
        return datasets


def _include(c: OntClass, mixin: dict) -> None:
    have = {p["field"] for p in c.properties}
    c.properties = [copy.deepcopy(p) for p in mixin.get("properties", []) if p["field"] not in have] + c.properties
    have_refs = {r["field"] for r in c.references}
    c.references = [copy.deepcopy(r) for r in mixin.get("references", []) if r["field"] not in have_refs] + c.references


def _as_list(v) -> list:
    return [v] if isinstance(v, str) else list(v)


def build(raw: dict) -> Ontology:
    classes = []
    for spec in raw["classes"]:
        c = OntClass(
            label=spec["label"],
            domain=spec["domain"],
            tables=list(spec["tables"]),
            description=spec.get("description", ""),
            fetch=list(spec.get("fetch", [])),
            where=copy.deepcopy(spec.get("where", {})),
            is_ci="ci" in spec.get("include", []),
            properties=copy.deepcopy(spec.get("properties", [])),
            references=copy.deepcopy(spec.get("references", [])),
        )
        for mixin in spec.get("include", []):
            _include(c, raw["mixins"][mixin])
        classes.append(c)
    ont = Ontology(
        name=raw["name"],
        version=raw["version"],
        domains=raw["domains"],
        classes=classes,
        link_tables=copy.deepcopy(raw.get("link_tables", [])),
        rel_table=raw["ci_relationships"]["table"],
        rel_rules=[],
        ignore_fields=set(raw.get("ignore_fields", [])),
        raw=raw,
    )
    for c in ont.classes:
        for r in c.references:
            r["to"] = ont.expand(r["to"])
    for lt in ont.link_tables:
        for end in ("from", "to"):
            lt[end]["class"] = ont.expand(lt[end]["class"])
    for rule in raw["ci_relationships"]["rules"]:
        for parent in ont.expand(_as_list(rule["parent"])):
            for child in ont.expand(_as_list(rule["child"])):
                ont.rel_rules.append(
                    {
                        "servicenow_type": rule["servicenow_type"],
                        "parent": parent,
                        "child": child,
                        "type": rule["type"],
                        "reverse": bool(rule.get("reverse")),
                    }
                )
    errors = validate(ont)
    if errors:
        raise OntologyError("; ".join(errors))
    return ont


def validate(ont: Ontology) -> list[str]:
    errors = []
    labels = ont.labels()
    domains = {d["id"] for d in ont.domains}
    for dup in {x for x in labels if labels.count(x) > 1}:
        errors.append(f"class {dup} is defined twice")
    for c in ont.classes:
        if not LABEL_RE.match(c.label):
            errors.append(f"class label {c.label!r} must be PascalCase, at most 31 characters")
        if c.domain not in domains:
            errors.append(f"{c.label}: unknown domain {c.domain}")
        for t in c.tables:
            if not TABLE_RE.match(t):
                errors.append(f"{c.label}: bad table name {t!r}")
        names = [p["name"] for p in c.properties]
        for dup in {x for x in names if names.count(x) > 1}:
            errors.append(f"{c.label}: property {dup} twice")
        for p in c.properties:
            if not PROP_RE.match(p["name"]) or p["name"] == "sys_id" or p.get("type") not in TYPES:
                errors.append(f"{c.label}: bad property {p}")
        for r in c.references:
            if not REL_RE.match(r["type"]):
                errors.append(f"{c.label}.{r['field']}: bad relationship type {r['type']!r}")
            for t in r["to"]:
                if t not in labels:
                    errors.append(f"{c.label}.{r['field']}: unknown target class {t}")
    for rule in ont.rel_rules:
        for end in ("parent", "child"):
            if rule[end] not in labels:
                errors.append(f"cmdb_rel_ci rule {rule['servicenow_type']}: unknown class {rule[end]}")
        if not REL_RE.match(rule["type"]):
            errors.append(f"cmdb_rel_ci rule {rule['servicenow_type']}: bad type {rule['type']!r}")
    for lt in ont.link_tables:
        for end in ("from", "to"):
            for t in lt[end]["class"]:
                if t not in labels:
                    errors.append(f"link table {lt['table']}: unknown class {t}")
    return errors


@lru_cache(maxsize=1)
def _raw() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def load() -> Ontology:
    """The template as shipped (a fresh copy each call, so callers can layer mappings on it)."""
    return build(json.loads(_raw()))
