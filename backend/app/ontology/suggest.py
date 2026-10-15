"""Ask the LLM where ServiceNow data the template doesn't cover could fit. The answer is only a suggestion shown
next to the item; a person maps it (or ignores it). Nothing is applied automatically."""

import json
import logging

from app.llm import ask_json
from app.ontology.store import validate_mapping
from app.ontology.template import Ontology, OntologyError

log = logging.getLogger(__name__)

SYSTEM = """You help map ServiceNow data onto the ServiceNow Common Service Data Model (CSDM).
You are given the ontology (classes, their ServiceNow tables, relationship types) and ONE item that the
ontology does not cover yet. Suggest how it should be mapped. A person reviews your suggestion, so say how
sure you are and why. Prefer existing classes and relationship types; only propose a new class when nothing
in the ontology fits. Answer with one JSON object only."""

FORMATS = {
    "class": """The item is a ServiceNow table (or sys_class_name) whose records match no ontology class.
Answer one of:
{"action": "map_to_class", "class": "<existing class label>", "confidence": "high|medium|low", "reason": "..."}
{"action": "new_class", "label": "<PascalCase, max 31 chars>", "domain": "<domain id>", "description": "...",
 "confidence": "...", "reason": "..."}
{"action": "ignore", "confidence": "...", "reason": "<why this data doesn't belong in the graph>"}""",
    "field": """The item is a field of an ontology class that has data but no mapping.
Answer one of:
{"action": "property", "name": "<lower_snake_case>", "type": "string|integer|float|date|boolean",
 "confidence": "...", "reason": "..."}
{"action": "reference", "to": ["<class label>"], "type": "<UPPER_SNAKE_CASE relationship type>",
 "confidence": "...", "reason": "..."}   (only when the values are sys_ids of records of that class)
{"action": "ignore", "confidence": "...", "reason": "..."}   (system or noise fields)""",
    "relationship": """The item is a cmdb_rel_ci relationship type (or a pair of classes for a known type) that has
no rule. Answer one of:
{"action": "relationship", "type": "<UPPER_SNAKE_CASE graph relationship type>", "reverse": false,
 "confidence": "...", "reason": "..."}   (reverse=true when the graph edge should point child -> parent)
{"action": "ignore", "confidence": "...", "reason": "..."}""",
}


def suggest(ont: Ontology, item: dict) -> dict:
    prompt = (
        f"{ont.describe_for_llm()}\n\nItem kind: {item['item_kind']}\nItem: {item['item_key']}\n"
        f"Details: {json.dumps(item['detail'], default=str)[:3000]}\n\n{FORMATS[item['item_kind']]}"
    )
    try:
        raw = ask_json(SYSTEM, prompt)
    except Exception as exc:  # the queue must not depend on the model being available
        log.warning("ontology suggestion failed for %s: %s", item["item_key"], exc)
        return {"error": "The LLM could not suggest a mapping; map this item manually."}
    out = {
        "confidence": str(raw.get("confidence") or "low").lower()[:10],
        "reason": str(raw.get("reason") or "")[:600],
    }
    try:
        out["mapping"] = validate_mapping(ont, item, raw)
    except OntologyError as exc:
        out["mapping"] = None
        out["note"] = f"The LLM's answer didn't fit the ontology ({exc}); map this item manually."
        out["raw"] = {k: v for k, v in raw.items() if k not in ("confidence", "reason")}
    return out
