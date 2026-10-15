"""ServiceNow -> CSDM ontology template -> knowledge graph, and the "not mapped" queue.

Runs against the ServiceNow stand-in (tests/source_mocks.py), whose CSDM landscape includes things the template
doesn't cover: a cmdb_ci_cluster class, custom fields (u_patch_window, u_cost_center), an unknown relationship
type ("Cluster of::Cluster"), a known type between classes no rule allows, and tables the instance lacks (404).
"""

import json
from collections import Counter

import pytest

from app import jobs, kb, loader
from app.connectors import Dataset
from app.connectors import pipeline as cp
from app.connectors.servicenow import PRESETS as SN_PRESETS
from app.ontology import mapper, suggest, template
from app.tabular import read_table_file

from . import test_connectors
from .conftest import login
from .test_connectors import MemoryGraphStore, _connect, sn

# the connector tests' fixtures (ServiceNow stand-in, signed-in users with an in-memory graph)
app_env = test_connectors.app_env
servicenow = test_connectors.servicenow

CSDM = SN_PRESETS["csdm"]["datasets"]


def _datasets(extra_fields: bool = True) -> list[dict]:
    out = []
    for d in CSDM:
        d = json.loads(json.dumps(d))
        if extra_fields and d["source"] == "cmdb_ci_server":
            d["fields"].append("u_patch_window")  # a custom field the template doesn't know
        if extra_fields and d["source"] == "cmdb_ci_business_app":
            d["fields"].append("u_cost_center")
        out.append(d)
    out.append({"name": "cmdb_ci_cluster", "source": "cmdb_ci_cluster"})  # a class the template doesn't know
    return out


def _pull(mock) -> list[tuple[str, object]]:
    raw = []
    with sn(mock) as c:
        for d in _datasets():
            try:
                raw.append((d["source"], c.fetch_table(Dataset.from_dict(d))))
            except Exception as exc:  # tables this instance doesn't have
                assert "HTTP 404" in str(exc), exc
    return raw


def _keys(items, kind):
    return {i["item_key"] for i in items if i["item_kind"] == kind}


# ------------------------------------------------------------------ the template
def test_template_is_valid_and_covers_the_csdm_domains():
    ont = template.load()
    assert template.validate(ont) == []
    assert {d["id"] for d in ont.domains} >= {
        "foundation",
        "design",
        "build",
        "manage_technical_services",
        "sell_consume",
    }
    by_domain = Counter(c.domain for c in ont.classes)
    assert all(by_domain[d] for d in ("foundation", "design", "build", "manage_technical_services", "sell_consume"))
    for label in (
        "BusinessCapability",
        "BusinessApplication",
        "ApplicationService",
        "TechnicalService",
        "TechnicalServiceOffering",
        "BusinessService",
        "BusinessServiceOffering",
        "DynamicCIGroup",
    ):
        assert ont.cls(label), label
    assert ont.cls("ApplicationService").tables[0] == "cmdb_ci_service_auto"
    # mixins expand: every CI has an owner and a support group; "@ci" expands to every CI class
    assert {"owned_by", "support_group"} <= {r["field"] for r in ont.cls("Server").references}
    affects = next(r for r in ont.cls("Incident").references if r["field"] == "cmdb_ci")
    assert set(affects["to"]) == set(ont.ci_labels()) and "Server" in affects["to"]
    sources = [d["source"] for d in CSDM]
    assert "cmdb_rel_ci" in sources and sources.count("service_offering") == 1  # both offering classes, one pull


def test_bad_templates_are_refused():
    raw = json.loads(template._raw())
    raw["classes"][0]["label"] = "not pascal"
    raw["classes"][1]["references"][0]["to"] = ["NoSuchClass"]
    with pytest.raises(template.OntologyError, match="PascalCase.*|unknown target class"):
        template.build(raw)


# ------------------------------------------------------------------ mapping
def test_records_map_onto_classes_and_relationships(servicenow, tmp_path):
    raw = _pull(servicenow)
    sources = {s for s, _ in raw}
    assert "business_unit" not in sources  # 404 on this instance: skipped, not fatal
    ont = template.load()
    result = mapper.map_tables(ont, raw)
    classes = result.stats["classes"]
    assert classes["BusinessApplication"] == 2 and classes["BusinessCapability"] == 1
    assert classes["ApplicationService"] == 2  # discovered + calculated subclasses
    assert classes["DynamicCIGroup"] == 1  # read twice (service_auto and its own table), kept once
    assert classes["Server"] == 2  # cmdb_ci_esx_server isn't listed: mapped through the table it came from
    assert classes["TechnicalServiceOffering"] == 1 and classes["BusinessServiceOffering"] == 1  # by classification
    assert classes["Incident"] == 260 and classes["User"] == 40
    # sys_class_name keeps the table name even though ServiceNow displays "Linux Server"
    server = next(t for t in result.tables if t.name == "Server")
    assert {r["sys_id"] for r in server.rows} and "u_patch_window" not in server.columns

    unmapped = result.unmapped
    assert _keys(unmapped, "class") == {"cmdb_ci_cluster"}
    assert {"Server.u_patch_window", "BusinessApplication.u_cost_center"} <= _keys(unmapped, "field")
    assert _keys(unmapped, "relationship") == {
        "Cluster of::Cluster",
        "Depends on::Used by | BusinessApplication -> Server",
    }
    cluster_of = next(u for u in unmapped if u["item_key"] == "Cluster of::Cluster")
    assert cluster_of["detail"]["pairs"] == []  # the cluster isn't a mapped class, so the pair is unknown
    assert result.stats["unresolved_relationships"] >= 1  # a CI that wasn't pulled

    # the generated schema loads with the normal loader
    path = tmp_path / "csdm.xlsx"
    cp.write_workbook(path, result.tables)
    sheets = read_table_file(path)
    from app import extraction
    from app import graph_schema as gs

    schema = gs.clean(
        {
            "source_file": "csdm.xlsx",
            "sheets": extraction.sheets_summary(sheets),
            "nodes": result.nodes,
            "relationships": result.relationships,
        }
    )
    plan = loader.plan_load(schema, sheets, {})
    assert not plan.rejected
    links = Counter()
    for r in schema["relationships"]:
        links[(r["from"]["label"], r["type"], r["to"]["label"])] += len(plan.rel_rows.get(r["id"], {}))
    assert links[("BusinessApplication", "CONSUMES", "ApplicationService")] == 2
    assert links[("BusinessCapability", "PROVIDED_BY", "BusinessApplication")] == 1
    assert links[("ApplicationService", "DEPENDS_ON", "Server")] == 1
    assert links[("ApplicationService", "DEPENDS_ON", "TechnicalServiceOffering")] == 1
    assert links[("TechnicalServiceOffering", "CONTAINS", "DynamicCIGroup")] == 1
    assert links[("DatabaseInstance", "RUNS_ON", "Server")] == 1
    assert links[("Server", "HOSTED_ON", "Server")] == 1
    assert links[("TechnicalServiceOffering", "OFFERING_OF", "TechnicalService")] == 1
    assert links[("BusinessServiceOffering", "OFFERING_OF", "BusinessService")] == 1
    assert links[("ApplicationService", "CONTAINS_CI", "Server")] == 1  # svc_ci_assoc
    assert sum(n for (a, t, b), n in links.items() if t == "MEMBER_OF") == 6
    assert sum(n for (a, t, b), n in links.items() if a == "Incident" and t == "AFFECTS_CI") == 0  # old mock CIs
    assert links[("Incident", "RAISED_BY", "User")] == 260
    # a reference with several candidate classes counts as skipped only when none matched
    assert plan.links_skipped["AFFECTS_CI"] == 260 + 20 + 30  # tickets point at CIs from a generic cmdb_ci pull


def test_manual_mappings_extend_the_template(servicenow):
    raw = _pull(servicenow)
    ont = template.load()
    first = mapper.map_tables(ont, raw)
    items = [
        {
            "item_kind": "class",
            "item_key": "cmdb_ci_cluster",
            "mapping": {"action": "new_class", "label": "Cluster", "domain": "manage_technical_services"},
        },
        {
            "item_kind": "field",
            "item_key": "Server.u_patch_window",
            "mapping": {"action": "property", "name": "patch_window", "type": "string"},
        },
        {
            "item_kind": "relationship",
            "item_key": "Cluster of::Cluster",
            "detail": {"servicenow_type": "Cluster of::Cluster", "pairs": []},
            "mapping": {"action": "relationship", "type": "MEMBER_OF_CLUSTER", "parent": "Server", "child": "Cluster"},
        },
    ]
    mapped = ont.with_mappings(items)
    assert ont.cls("Cluster") is None and mapped.cls("Cluster").manual  # the template itself is untouched
    second = mapper.map_tables(
        mapped, raw, ignored={("relationship", "Depends on::Used by | BusinessApplication -> Server")}
    )
    assert second.stats["classes"]["Cluster"] == 1
    assert "patch_window" in {p["name"] for n in second.nodes if n["label"] == "Server" for p in n["properties"]}
    assert any(r["type"] == "MEMBER_OF_CLUSTER" for r in second.relationships)
    keys = {u["item_key"] for u in second.unmapped}
    assert "cmdb_ci_cluster" not in keys and "Server.u_patch_window" not in keys
    assert "Cluster of::Cluster" not in keys and not any("BusinessApplication -> Server" in k for k in keys)
    assert "Cluster.cluster_status" in keys  # the new class's own fields now show up to be mapped
    assert len(second.unmapped) < len(first.unmapped) + 1


# ------------------------------------------------------------------ API and jobs
@pytest.fixture
def llm(monkeypatch):
    """Stand-in for the LLM's mapping suggestions."""
    calls = []

    def answer(system, prompt):
        calls.append(prompt)
        if "Item: cmdb_ci_cluster" in prompt:
            return {
                "action": "new_class",
                "label": "Cluster",
                "domain": "manage_technical_services",
                "confidence": "medium",
                "reason": "Clusters group servers; CSDM has no cluster class.",
            }
        if "Item: Server.u_patch_window" in prompt:
            return {
                "action": "property",
                "name": "patch_window",
                "type": "string",
                "confidence": "high",
                "reason": "A maintenance window text.",
            }
        if "Item: Cluster of::Cluster" in prompt:
            return {"action": "relationship", "type": "member of cluster", "confidence": "low", "reason": "?"}
        raise ValueError("model unavailable")

    monkeypatch.setattr(suggest, "ask_json", answer)
    return calls


def _wait_ok(job_id):
    job = jobs.wait(job_id, timeout=120)
    assert job["status"] == "succeeded", job["error"]
    return job


def test_servicenow_graph_kb_maps_to_csdm_and_queues_what_is_not_mapped(client, app_env, servicenow, llm):
    priya, arjun = app_env["priya"], app_env["arjun"]
    c = _connect(client, priya, servicenow)
    r = client.post(
        "/api/kbs/from-connection",
        headers=priya,
        json={
            "kb_name": "t_csdm",
            "kb_type": "graph",
            "domain": "IT",
            "sub_domain": "CMDB",
            "connection_id": c["id"],
            "datasets": _datasets(),
        },
    )
    assert r.status_code == 201, r.text
    job = _wait_ok(r.json()["job_id"])
    names = [s["name"] for s in job["steps"]]
    assert names == [
        "Fetch from ServiceNow",
        "Map to the CSDM ontology",
        "LLM suggests mappings for data not in the ontology",
        "LLM scans for PII",
    ]
    assert "Not on this instance or not readable: " in job["steps"][0]["detail"]  # 404 tables noted
    assert "CSDM classes" in job["steps"][1]["detail"]
    cat = kb.get_catalog("t_csdm")
    assert cat["status"] == "awaiting_review"
    schema = cat["draft_schema"]
    assert schema["ontology"]["name"] == "servicenow-csdm"
    assert {n["label"] for n in schema["nodes"]} >= {"BusinessApplication", "ApplicationService", "Server", "User"}
    assert all(n["key"]["name"] == "sys_id" for n in schema["nodes"])

    review = client.get("/api/kbs/t_csdm/review", headers=priya).json()
    assert review["ontology"]["counts"]["pending"] >= 4

    view = client.get("/api/kbs/t_csdm/ontology", headers=priya).json()
    items = {i["item_key"]: i for i in view["items"]}
    assert "snapshots" not in view["ontology"]
    cluster = items["cmdb_ci_cluster"]
    assert cluster["status"] == "pending" and cluster["suggestion"]["mapping"]["label"] == "Cluster"
    assert items["Server.u_patch_window"]["suggestion"]["mapping"] == {
        "action": "property",
        "name": "patch_window",
        "type": "string",
    }
    # an answer that doesn't fit the ontology is kept as a note, never as a mapping
    assert items["Cluster of::Cluster"]["suggestion"]["mapping"] is None
    assert "map this item manually" in items["Cluster of::Cluster"]["suggestion"]["note"]
    # the model being down doesn't block the queue
    assert "could not suggest" in items["BusinessApplication.u_cost_center"]["suggestion"]["error"]

    # only the owner maps; someone with access may look
    assert client.post("/api/kbs/t_csdm/access", headers=priya, json={"user_id": "arjun.mehta"}).status_code == 201
    assert client.get("/api/kbs/t_csdm/ontology", headers=arjun).status_code == 200
    put = lambda key, body, who=priya: client.put(  # noqa: E731
        f"/api/kbs/t_csdm/ontology/items/{items[key]['id']}", headers=who, json=body
    )
    assert put("cmdb_ci_cluster", {"action": "ignore"}, arjun).status_code == 403
    assert put("cmdb_ci_cluster", {"action": "new_class", "label": "bad label"}).status_code == 422
    assert put("cmdb_ci_cluster", {"action": "new_class", "label": "Server"}).status_code == 422  # exists
    assert put("cmdb_ci_cluster", {"action": "new_class", "label": "Cluster"}).json()["status"] == "mapped"
    assert put("Server.u_patch_window", {"action": "property", "name": "patch_window"}).status_code == 200
    assert put("BusinessApplication.u_cost_center", {"action": "ignore"}).json()["status"] == "ignored"
    odd = "Depends on::Used by | BusinessApplication -> Server"
    assert put(odd, {"action": "relationship", "type": "RUNS_ON"}).status_code == 200
    # the relationship to the (new) cluster class: no pair was seen, so the classes must be named
    r = put("Cluster of::Cluster", {"action": "relationship", "type": "MEMBER_OF_CLUSTER"})
    assert r.status_code == 422 and "parent, child" in r.json()["detail"]
    assert client.get("/api/kbs/t_csdm/ontology", headers=priya).json()["counts"]["not_applied"] == 3

    # applying while still in review regenerates the draft
    r = client.post("/api/kbs/t_csdm/ontology/apply", headers=priya)
    assert r.status_code == 202, r.text
    _wait_ok(r.json()["job_id"])
    cat = kb.get_catalog("t_csdm")
    assert cat["status"] == "awaiting_review"
    labels = {n["label"]: n for n in cat["draft_schema"]["nodes"]}
    assert "Cluster" in labels and "patch_window" in {p["name"] for p in labels["Server"]["properties"]}
    assert any(
        r["type"] == "RUNS_ON" and r["from"]["label"] == "BusinessApplication"
        for r in cat["draft_schema"]["relationships"]
    )
    view = client.get("/api/kbs/t_csdm/ontology", headers=priya).json()
    items = {i["item_key"]: i for i in view["items"]}
    assert view["counts"]["not_applied"] == 0
    assert items["Cluster.cluster_status"]["status"] == "pending"  # the new class's own fields come up next
    assert items["Cluster of::Cluster"]["detail"]["pairs"] == [{"parent": "Server", "child": "Cluster", "rows": 1}]
    assert put("Cluster of::Cluster", {"action": "relationship", "type": "MEMBER_OF_CLUSTER"}).status_code == 200

    # review + build as usual
    review = client.get("/api/kbs/t_csdm/review", headers=priya).json()
    r = client.post("/api/kbs/t_csdm/submit", headers=priya, json={"schema": review["schema"]})
    _wait_ok(r.json()["job_id"])
    assert kb.get_catalog("t_csdm")["status"] == "ready"
    graph = MemoryGraphStore.nodes["t_csdm"]
    assert len(graph["Server"]) == 2 and len(graph["Cluster"]) == 1 and len(graph["Incident"]) == 260

    # applying on a built graph grows it
    r = client.post("/api/kbs/t_csdm/ontology/apply", headers=priya)
    job = _wait_ok(r.json()["job_id"])
    assert [s["name"] for s in job["steps"]][:2] == [
        "Map the saved ServiceNow data again",
        "LLM suggests mappings for data not in the ontology",
    ]
    approved = kb.get_catalog("t_csdm")["approved_schema"]
    assert any(r["type"] == "MEMBER_OF_CLUSTER" for r in approved["relationships"])

    # a refresh from ServiceNow goes through the ontology too
    servicenow.data["cmdb_ci_server"].append(
        {
            **servicenow.data["cmdb_ci_server"][0],
            "sys_id": "e1" * 16,
            "name": "srv-lin-02",
            "sys_updated_on": "2026-10-05 09:00:00",
        }
    )
    r = client.post(
        "/api/kbs/t_csdm/add-data/connection",
        headers=priya,
        json={
            "connection_id": c["id"],
            "since": "2026-10-01",
            "datasets": [d for d in _datasets() if d["source"] in ("cmdb_ci_server", "incident")],
        },
    )
    assert r.status_code == 202, r.text
    job = _wait_ok(r.json()["job_id"])
    assert [s["name"] for s in job["steps"]][1] == "Map to the CSDM ontology"
    assert len(MemoryGraphStore.nodes["t_csdm"]["Server"]) == 3
    assert MemoryGraphStore.nodes["t_csdm"]["Server"]["E1" * 16]["patch_window"] == "Sun 02:00"


def test_ontology_endpoints_need_an_ontology_kb(client, app_env):
    priya = app_env["priya"]
    assert client.get("/api/ontology/servicenow", headers=priya).json()["name"] == "servicenow-csdm"
    assert client.get("/api/kbs/nope/ontology", headers=priya).status_code in (403, 404)
    other = login(client, "karthik.r")
    assert client.get("/api/ontology/servicenow", headers=other).status_code == 200
