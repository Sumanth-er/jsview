"""The ServiceNow CSDM ontology: the template, and per knowledge base the data it doesn't cover ("not mapped"),
the LLM's suggestions and the owner's manual mappings."""

import anyio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app import jobs, kb
from app.auth import CurrentUser, current_user
from app.ontology import pipeline as op
from app.ontology import store, suggest, template

router = APIRouter(prefix="/api", tags=["ontology"])


class MappingBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    action: str


def _schema(cat: dict) -> dict | None:
    return cat["approved_schema"] if cat["status"] == "ready" else cat["draft_schema"] or cat["approved_schema"]


def _ontology_kb(user: CurrentUser, kb_name: str, roles=("owner", "user")) -> dict:
    cat = kb.require_access(user, kb_name, roles=roles)
    if not op.is_ontology_schema(_schema(cat)):
        raise HTTPException(404, "This knowledge base wasn't built from ServiceNow with the CSDM ontology")
    return cat


def _item(row: dict) -> dict:
    return {
        k: row[k]
        for k in (
            "id",
            "item_kind",
            "item_key",
            "detail",
            "suggestion",
            "suggested_at",
            "status",
            "mapping",
            "decided_by",
            "decided_at",
            "applied_at",
            "occurrences",
            "first_seen",
            "last_seen",
        )
    }


@router.get("/ontology/servicenow")
def get_template(user: CurrentUser = Depends(current_user)):
    return template.load().summary()


@router.get("/kbs/{kb_name}/ontology")
def get_kb_ontology(kb_name: str, user: CurrentUser = Depends(current_user)):
    cat = _ontology_kb(user, kb_name)
    info = dict(_schema(cat).get("ontology") or {})
    info.pop("snapshots", None)  # file paths stay on the server
    ont, _ = op.effective(kb_name)
    return {
        "kb_name": kb_name,
        "status": cat["status"],
        "role": cat["role"],
        "ontology": info,
        "counts": store.counts(kb_name),
        "items": [_item(r) for r in store.items(kb_name)],
        "template": ont.summary(),
    }


@router.put("/kbs/{kb_name}/ontology/items/{item_id}")
def map_item(kb_name: str, item_id: int, body: MappingBody, user: CurrentUser = Depends(current_user)):
    _ontology_kb(user, kb_name, roles=("owner",))
    item = store.get(kb_name, item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    ont, _ = op.effective(kb_name)
    try:
        mapping = store.validate_mapping(ont, item, body.model_dump())
    except template.OntologyError as exc:
        raise HTTPException(422, str(exc)) from None
    return _item(store.decide(kb_name, item_id, mapping, user.user_id))


@router.post("/kbs/{kb_name}/ontology/items/{item_id}/reopen")
def reopen_item(kb_name: str, item_id: int, user: CurrentUser = Depends(current_user)):
    _ontology_kb(user, kb_name, roles=("owner",))
    row = store.reopen(kb_name, item_id)
    if not row:
        raise HTTPException(404, "Item not found")
    return _item(row)


@router.post("/kbs/{kb_name}/ontology/items/{item_id}/suggest")
async def suggest_again(kb_name: str, item_id: int, user: CurrentUser = Depends(current_user)):
    _ontology_kb(user, kb_name, roles=("owner",))
    item = store.get(kb_name, item_id)
    if not item:
        raise HTTPException(404, "Item not found")

    def run():
        ont, _ = op.effective(kb_name)
        store.save_suggestion(item_id, suggest.suggest(ont, item))
        return _item(store.get(kb_name, item_id))

    return await anyio.to_thread.run_sync(run)


@router.post("/kbs/{kb_name}/ontology/apply", status_code=202)
def apply_mappings(kb_name: str, user: CurrentUser = Depends(current_user)):
    cat = _ontology_kb(user, kb_name, roles=("owner",))
    if cat["status"] not in ("ready", "awaiting_review"):
        raise HTTPException(409, f"The knowledge base is {cat['status']}; apply mappings once it is ready")
    if cat["status"] == "awaiting_review":
        kb.set_status(kb_name, "extracting", "Mapping the ServiceNow data again", actor=user.user_id)
    job_id = jobs.create(
        kb_name,
        "add_data" if cat["status"] == "ready" else "graph_extract",
        op.apply_steps(cat["status"]),
        user.user_id,
        "ServiceNow: manual CSDM mappings",
    )
    # a failure while still in review leaves the KB failed (like extraction); a built graph stays as it was
    on_error = (lambda m: kb.set_status(kb_name, "failed", m[:500])) if cat["status"] != "ready" else None
    jobs.submit(
        job_id,
        op.apply_mappings,
        kb_name,
        user.user_id,
        on_error=on_error,
        user_id=user.user_id,
        session_id=user.session_id,
        operation="add_data",
    )
    return {"job_id": job_id}
