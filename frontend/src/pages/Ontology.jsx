import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, formatDate } from "../api.js";
import { ErrorBox } from "../components/common.jsx";

// ServiceNow data the CSDM ontology template doesn't cover: the LLM suggests, the knowledge base owner maps.
const KIND = { class: "Table / class", field: "Field", relationship: "Relationship" };
const ACTIONS = {
  class: [["map_to_class", "Map to an existing class"], ["new_class", "Create a new class"], ["ignore", "Ignore"]],
  field: [["property", "Store as a property"], ["reference", "Link to another class"], ["ignore", "Ignore"]],
  relationship: [["relationship", "Map to a graph relationship"], ["ignore", "Ignore"]],
};
const TYPES = ["string", "integer", "float", "date", "boolean"];

const snake = (s) => String(s || "").replace(/^u_/, "").replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").toLowerCase();
const upper = (s) => String(s || "").split("::")[0].trim().replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").toUpperCase();
const pascal = (s) => String(s || "").replace(/^cmdb_ci_/, "").split(/[^A-Za-z0-9]+/).filter(Boolean)
  .map((w) => w[0].toUpperCase() + w.slice(1)).join("").slice(0, 31);

export function describeMapping(m, domains = {}) {
  if (!m) return "";
  switch (m.action) {
    case "map_to_class": return `Map to class ${m.class}`;
    case "new_class": return `New class ${m.label} (${domains[m.domain] || m.domain})`;
    case "property": return `Property ${m.name} (${m.type})`;
    case "reference": return `Link ${m.type} → ${(m.to || []).map((t) => (t === "@ci" ? "any CI" : t)).join(", ")}`;
    case "relationship":
      return `Relationship ${m.type}${m.parent ? ` (${m.parent} → ${m.child})` : ""}${m.reverse ? ", child → parent" : ""}`;
    case "ignore": return "Ignore";
    case "template": return "Covered by the template";
    default: return JSON.stringify(m);
  }
}

function defaultDraft(item) {
  const d = item.detail || {};
  const s = item.suggestion?.mapping;
  if (s) return { ...s };
  if (item.item_kind === "class") return { action: "new_class", label: pascal(item.item_key), domain: "manage_technical_services" };
  if (item.item_kind === "field") {
    return d.is_reference && d.points_to?.length
      ? { action: "reference", to: [d.points_to[0]], type: upper(d.field) }
      : { action: "property", name: snake(d.field), type: "string" };
  }
  return { action: "relationship", type: upper(d.servicenow_type), reverse: false };
}

function Detail({ item }) {
  const d = item.detail || {};
  if (item.item_kind === "class") {
    return (
      <div className="small">
        <div>{d.rows?.toLocaleString()} {d.rows === 1 ? "record" : "records"} of <span className="mono">{d.table}</span>
          {d.read_from && d.read_from !== d.table && <> (read from <span className="mono">{d.read_from}</span>)</>}
          {" "}match no class in the ontology.</div>
        {d.examples?.length > 0 && <div className="muted">Examples: {d.examples.join(", ")}</div>}
        {d.columns?.length > 0 && <div className="muted">Fields with data: <span className="mono">{d.columns.join(", ")}</span></div>}
      </div>
    );
  }
  if (item.item_kind === "field") {
    return (
      <div className="small">
        <div><span className="mono">{d.field}</span> on {d.class} has values in {d.rows_with_value?.toLocaleString()} of{" "}
          {d.rows?.toLocaleString()} records but no mapping
          {d.is_reference && <> — it holds references{d.points_to?.length ? <> to {d.points_to.join(", ")}</> : ""}</>}.</div>
        {d.samples?.length > 0 && <div className="muted">Values: {d.samples.join(" · ")}</div>}
        {d.tables?.length > 0 && <div className="muted">Read from <span className="mono">{d.tables.join(", ")}</span></div>}
      </div>
    );
  }
  return (
    <div className="small">
      <div>{d.rows?.toLocaleString()} <span className="mono">cmdb_rel_ci</span> rows of type{" "}
        <span className="mono">{d.servicenow_type}</span> have no rule in the ontology.</div>
      {d.pairs?.length > 0
        ? <div className="muted">Between: {d.pairs.map((p) => `${p.parent} → ${p.child} (${p.rows})`).join(", ")}</div>
        : <div className="muted">Neither end is a mapped class yet, so say which classes it links.</div>}
    </div>
  );
}

function MappingForm({ item, template, onSave, onSuggest, busy }) {
  const [draft, setDraft] = useState(() => defaultDraft(item));
  const set = (patch) => setDraft((x) => ({ ...x, ...patch }));
  const classes = template.classes.map((c) => c.label);
  const needsPair = item.item_kind === "relationship" && !(item.detail?.pairs || []).length;
  const id = (name) => `m-${item.id}-${name}`;
  return (
    <form className="ont-form" onSubmit={(e) => { e.preventDefault(); onSave(draft); }}>
      <div className="ont-fields">
        <label className="lbl" htmlFor={id("action")}>Decision</label>
        <select id={id("action")} className="inp sm" value={draft.action}
                onChange={(e) => setDraft({ ...defaultDraft({ ...item, suggestion: null }), action: e.target.value })}>
          {ACTIONS[item.item_kind].map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        {draft.action === "map_to_class" && (<>
          <label className="lbl" htmlFor={id("class")}>Class</label>
          <select id={id("class")} className="inp sm" value={draft.class || ""} onChange={(e) => set({ class: e.target.value })}>
            <option value="" disabled>Choose…</option>
            {classes.map((c) => <option key={c}>{c}</option>)}
          </select>
        </>)}
        {draft.action === "new_class" && (<>
          <label className="lbl" htmlFor={id("label")}>Class name</label>
          <input id={id("label")} className="inp sm" value={draft.label || ""} maxLength={31} placeholder="e.g. LinuxCluster"
                 onChange={(e) => set({ label: e.target.value })} />
          <label className="lbl" htmlFor={id("domain")}>CSDM domain</label>
          <select id={id("domain")} className="inp sm" value={draft.domain || ""} onChange={(e) => set({ domain: e.target.value })}>
            {template.domains.map((d) => <option key={d.id} value={d.id}>{d.label}</option>)}
          </select>
        </>)}
        {draft.action === "property" && (<>
          <label className="lbl" htmlFor={id("name")}>Property name</label>
          <input id={id("name")} className="inp sm mono" value={draft.name || ""} onChange={(e) => set({ name: e.target.value })} />
          <label className="lbl" htmlFor={id("type")}>Type</label>
          <select id={id("type")} className="inp sm" value={draft.type || "string"} onChange={(e) => set({ type: e.target.value })}>
            {TYPES.map((t) => <option key={t}>{t}</option>)}
          </select>
        </>)}
        {draft.action === "reference" && (<>
          <label className="lbl" htmlFor={id("to")}>Points to</label>
          <select id={id("to")} className="inp sm" value={(draft.to || [])[0] || ""} onChange={(e) => set({ to: [e.target.value] })}>
            <option value="" disabled>Choose…</option>
            <option value="@ci">Any configuration item</option>
            {classes.map((c) => <option key={c}>{c}</option>)}
          </select>
          <label className="lbl" htmlFor={id("rtype")}>Relationship type</label>
          <input id={id("rtype")} className="inp sm mono" value={draft.type || ""} placeholder="e.g. PATCHED_BY"
                 onChange={(e) => set({ type: e.target.value.toUpperCase() })} />
        </>)}
        {draft.action === "relationship" && (<>
          <label className="lbl" htmlFor={id("gtype")}>Graph relationship type</label>
          <input id={id("gtype")} className="inp sm mono" value={draft.type || ""} placeholder="e.g. HOSTED_ON"
                 list={id("types")} onChange={(e) => set({ type: e.target.value.toUpperCase() })} />
          <datalist id={id("types")}>{template.relationship_types.map((t) => <option key={t} value={t} />)}</datalist>
          {needsPair && (<>
            <label className="lbl" htmlFor={id("parent")}>Parent class</label>
            <select id={id("parent")} className="inp sm" value={draft.parent || ""} onChange={(e) => set({ parent: e.target.value })}>
              <option value="" disabled>Choose…</option>
              {classes.map((c) => <option key={c}>{c}</option>)}
            </select>
            <label className="lbl" htmlFor={id("child")}>Child class</label>
            <select id={id("child")} className="inp sm" value={draft.child || ""} onChange={(e) => set({ child: e.target.value })}>
              <option value="" disabled>Choose…</option>
              {classes.map((c) => <option key={c}>{c}</option>)}
            </select>
          </>)}
          <label className="small ont-check">
            <input type="checkbox" checked={!!draft.reverse} onChange={(e) => set({ reverse: e.target.checked })} />
            Edge points child → parent
          </label>
        </>)}
      </div>
      <div className="acts">
        <button className="btn sm" type="submit" disabled={busy}>{draft.action === "ignore" ? "Ignore" : "Save mapping"}</button>
        {onSuggest && (
          <button className="btn sec sm" type="button" onClick={onSuggest} disabled={busy}>Ask the LLM again</button>
        )}
      </div>
    </form>
  );
}

function Suggestion({ s, domains }) {
  if (!s) return <div className="ont-suggest muted small">No LLM suggestion yet.</div>;
  if (s.error) return <div className="ont-suggest muted small">{s.error}</div>;
  return (
    <div className="ont-suggest small">
      <span className="ont-suggest-label">LLM suggests</span>{" "}
      {s.mapping ? <strong>{describeMapping(s.mapping, domains)}</strong> : <em>{s.note}</em>}
      {s.confidence && <span className={`badge ${s.confidence === "high" ? "g" : s.confidence === "medium" ? "b" : "w"}`}>{s.confidence}</span>}
      {s.reason && <div className="muted">{s.reason}</div>}
    </div>
  );
}

function Item({ item, template, domains, canEdit, onSave, onReopen, onSuggest, busy }) {
  const decided = item.status !== "pending";
  const notApplied = item.status === "mapped" && (!item.applied_at || item.applied_at < item.decided_at);
  return (
    <div className="card ont-item">
      <div className="ont-item-head">
        <span className="chip">{KIND[item.item_kind]}</span>
        <span className="mono ont-key">{item.item_key}</span>
        {item.status === "mapped" && <span className={`badge ${notApplied ? "w" : "g"}`}>{notApplied ? "mapped, not applied yet" : "mapped"}</span>}
        {item.status === "ignored" && <span className="badge">ignored</span>}
        <span className="muted small ont-seen">seen {formatDate(item.last_seen, true)}</span>
      </div>
      <Detail item={item} />
      {decided ? (
        <div className="ont-decision small">
          <strong>{describeMapping(item.mapping, domains)}</strong>
          <span className="muted"> — {item.decided_by === "template" ? "the template now covers it" : `by ${item.decided_by}`}
            {item.decided_at && `, ${formatDate(item.decided_at, true)}`}</span>
          {canEdit && item.decided_by !== "template" && (
            <button className="btn link" type="button" onClick={() => onReopen(item)} disabled={busy}>Change</button>
          )}
        </div>
      ) : (
        <>
          <Suggestion s={item.suggestion} domains={domains} />
          {canEdit ? (
            <>
              <MappingForm key={JSON.stringify(item.suggestion)} item={item} template={template} busy={busy}
                           onSave={(m) => onSave(item, m)} onSuggest={() => onSuggest(item)} />
            </>
          ) : <div className="muted small">Only the knowledge base owner can map items.</div>}
        </>
      )}
    </div>
  );
}

function TemplateView({ template }) {
  const byDomain = template.domains.map((d) => ({ ...d, classes: template.classes.filter((c) => c.domain === d.id) }));
  return (
    <div className="ont-template">
      {byDomain.map((d) => (
        <div key={d.id} className="card panel">
          <div className="panel-head"><div><div className="section">{d.label}</div><div className="help">{d.description}</div></div></div>
          <div className="panel-body">
            {d.classes.map((c) => (
              <div key={c.label} className="ont-class">
                <div><strong>{c.label}</strong> {c.manual && <span className="badge w">mapped manually</span>}
                  <span className="mono small muted"> {c.tables.join(", ")}</span></div>
                <div className="small muted">{c.description}</div>
                <div className="small">{c.properties.map((p) => p.name).join(", ")}
                  {c.references.length > 0 && <> · links {c.references.map((r) => `${r.type} → ${r.to.length > 3 ? "CIs" : r.to.join("/")}`).join(", ")}</>}</div>
              </div>
            ))}
          </div>
        </div>
      ))}
      <div className="card panel">
        <div className="panel-head"><div><div className="section">CI relationships (cmdb_rel_ci)</div></div></div>
        <div className="panel-body small">
          {template.ci_relationships.map((r, i) => (
            <div key={i}><span className="mono">{r.servicenow_type}</span>: {r.parent} → {r.child} = <strong>{r.type}</strong></div>
          ))}
        </div>
      </div>
    </div>
  );
}

export default function Ontology() {
  const { kb } = useParams();
  const navigate = useNavigate();
  const [data, setData] = useState(null);
  const [tab, setTab] = useState("pending");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => api(`/kbs/${kb}/ontology`).then(setData).catch(setError), [kb]);
  useEffect(() => { load(); }, [load]);

  const domains = useMemo(() => Object.fromEntries((data?.template.domains || []).map((d) => [d.id, d.label])), [data]);
  const act = async (fn) => {
    setBusy(true);
    setError(null);
    try { await fn(); await load(); } catch (e) { setError(e); } finally { setBusy(false); }
  };
  const save = (item, mapping) => act(() => api(`/kbs/${kb}/ontology/items/${item.id}`, { method: "PUT", json: mapping }));
  const reopen = (item) => act(() => api(`/kbs/${kb}/ontology/items/${item.id}/reopen`, { method: "POST" }));
  const suggest = (item) => act(() => api(`/kbs/${kb}/ontology/items/${item.id}/suggest`, { method: "POST" }));
  const apply = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api(`/kbs/${kb}/ontology/apply`, { method: "POST" });
      navigate(`/kbs/${kb}/jobs/${r.job_id}`);
    } catch (e) { setError(e); setBusy(false); }
  };

  if (error && !data) return <div className="page"><ErrorBox error={error} /><Link to="/workspace">Back to workspace</Link></div>;
  if (!data) return <div className="page muted">Loading…</div>;

  const c = data.counts;
  const canEdit = data.role === "owner";
  const shown = data.items.filter((i) => i.status === tab);
  const canApply = canEdit && c.not_applied > 0 && ["ready", "awaiting_review"].includes(data.status);
  return (
    <div className="page">
      <div className="ont-head">
        <div>
          <div className="muted small"><Link to="/workspace">Workspace</Link> / {kb} / ServiceNow ontology</div>
          <h1 style={{ marginTop: 4 }}>ServiceNow data not in the ontology</h1>
          <div className="help">
            Every record pulled from ServiceNow is mapped onto the <strong>CSDM ontology template</strong>{" "}
            ({data.template.name} v{data.template.version}). Tables, fields and relationship types the template
            does not cover are listed here and are <strong>left out of the graph</strong> until they are mapped.
            The LLM suggests a mapping; nothing changes until the owner saves a decision and applies it.
          </div>
        </div>
        {canEdit && (
          <div className="ont-apply">
            <button className="btn" type="button" onClick={apply} disabled={!canApply || busy}>
              Apply mappings{c.not_applied ? ` (${c.not_applied})` : ""}
            </button>
            <div className="muted small">
              {data.status === "awaiting_review"
                ? "Maps the saved ServiceNow data again and replaces the draft on the Review screen."
                : "Maps the saved ServiceNow data again and adds what is now mapped to the graph."}
            </div>
          </div>
        )}
      </div>
      <ErrorBox error={error} />
      {data.status === "awaiting_review" && (
        <div className="notice">This knowledge base is waiting for review. <Link to={`/kbs/${kb}/review`}>Open the Review screen</Link></div>
      )}
      <div className="ont-tabs" role="tablist">
        {[["pending", `Not mapped (${c.pending})`], ["mapped", `Mapped (${c.mapped})`], ["ignored", `Ignored (${c.ignored})`],
          ["template", "Ontology template"]].map(([k, l]) => (
          <button key={k} type="button" role="tab" aria-selected={tab === k} className={`tab${tab === k ? " on" : ""}`}
                  onClick={() => setTab(k)}>{l}</button>
        ))}
      </div>
      {tab === "template" ? <TemplateView template={data.template} /> : (
        shown.length ? shown.map((item) => (
          <Item key={item.id} item={item} template={data.template} domains={domains} canEdit={canEdit} busy={busy}
                onSave={save} onReopen={reopen} onSuggest={suggest} />
        )) : <div className="card muted ont-item">{tab === "pending" ? "Everything pulled from ServiceNow is mapped." : "Nothing here."}</div>
      )}
    </div>
  );
}
