// "Questions by user": who asked what, when, and what it cost, from the web app and MCP agents. Filters: person,
// source (web / MCP), and a period (presets or a custom From/To range). Scope follows the page's knowledge base
// filter. Data: client.getQuestions (GET /api/analytics/queries).
import { useEffect, useMemo, useState } from "react";
import { formatDate } from "../api.js";
import { ErrorBox } from "../components/common.jsx";
import { ChartCard, DataTable, ScatterChart, Segmented } from "./charts.jsx";
import { getQuestions } from "./client.js";
import {
  SINGLE_COLOR, compact, daysAgo, duration, initials, integer, money, moneyTick, plural, relativeTime, toDateInput,
} from "./format.js";

const PERIODS = [
  { value: 7, label: "7 days" },
  { value: 30, label: "30 days" },
  { value: 90, label: "90 days" },
  { value: "custom", label: "Custom" },
];
const KINDS = { search: "Search", cypher: "Cypher" };
const PAGE = 20;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function xLabel(t, span) {
  const d = new Date(t);
  const day = `${d.getDate()} ${MONTHS[d.getMonth()]}`;
  return span <= 2 * 86400e3 ? `${day} ${String(d.getHours()).padStart(2, "0")}:00` : day;
}

// "MCP query_graph" -> "query_graph"; web operations keep their name
const operation = (name) => (name || "").replace(/^MCP /, "");

function Avatar({ name, small }) {
  return <span className={`an-avatar${small ? " an-avatar-sm" : ""}`} aria-hidden="true">{initials(name)}</span>;
}

function SourcePill({ source }) {
  return source === "mcp"
    ? <span className="an-chip an-chip-agent">MCP agent</span>
    : <span className="an-chip an-chip-ghost">Web app</span>;
}

function RequestText({ q, open, onToggle }) {
  if (!q.question) {
    return <p className="an-q-text an-q-missing">{KINDS[q.request_kind] || "Question"} text not captured</p>;
  }
  const kind = KINDS[q.request_kind];
  const toggle = { role: "button", tabIndex: 0, "aria-expanded": open, onClick: onToggle,
                   onKeyDown: (e) => (e.key === "Enter" || e.key === " ") && onToggle() };
  return (
    <div className="an-q-request">
      {kind && <span className={`an-kind-tag an-kind-${q.request_kind}`}>{kind}</span>}
      {q.request_kind === "cypher" ? (
        <code className={`an-q-code${open ? " an-is-open" : ""}`} title={open ? undefined : q.question} {...toggle}>{q.question}</code>
      ) : (
        <p className={`an-q-text${open ? " an-is-open" : ""}`} title={open ? undefined : q.question} {...toggle}>{q.question}</p>
      )}
    </div>
  );
}

export default function UserQueries({ kb, reloadKey }) {
  const [user, setUser] = useState("");
  const [source, setSource] = useState("");
  const [period, setPeriod] = useState(30);
  const [from, setFrom] = useState(() => toDateInput(daysAgo(29)));
  const [to, setTo] = useState(() => toDateInput(new Date()));
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [shown, setShown] = useState(PAGE);
  const [open, setOpen] = useState(null);

  const custom = period === "custom";
  const today = toDateInput(new Date());
  const rangeError = !custom ? null : !from || !to ? "Pick both dates." : from > to ? "“From” must be on or before “To”." : null;
  const request = useMemo(
    () => (custom ? { kb, user, source, from, to } : { kb, user, source, days: period }),
    [kb, user, source, custom, from, to, period],
  );

  useEffect(() => {
    if (rangeError) return undefined;
    let cancelled = false;
    setLoading(true);
    getQuestions(request)
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setError(null);
        setShown(PAGE);
      })
      .catch((e) => !cancelled && setError(e))
      .finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [request, rangeError, reloadKey]);

  // The time axis follows the range the shown data was loaded for (not half-typed dates).
  const domain = useMemo(() => {
    if (!data) return [Date.now() - 86400e3, Date.now()];
    if (data.from && data.to) {
      return [new Date(`${data.from}T00:00:00`).getTime(), new Date(`${data.to}T23:59:59`).getTime()];
    }
    const now = Date.now();
    if (!data.days) {
      const times = (data.queries || []).map((q) => Date.parse(q.trace_timestamp));
      return times.length ? [Math.min(...times), now] : [now - 86400e3, now];
    }
    const start = daysAgo(data.days - 1);
    start.setHours(0, 0, 0, 0);
    return [start.getTime(), now];
  }, [data]);

  const rows = useMemo(() => data?.queries || [], [data]);
  const points = useMemo(() => rows.map((q) => ({
    key: q.id, t: Date.parse(q.trace_timestamp), y: Number(q.total_cost) || 0, size: Number(q.total_tokens) || 0, q,
  })), [rows]);
  const people = data?.users || [];
  const totals = data?.totals || {};
  const sourceCounts = Object.fromEntries((data?.sources || []).map((s) => [s.source, s.queries]));
  const allCount = Object.values(sourceCounts).reduce((a, b) => a + b, 0);
  const maxCost = Math.max(0, ...people.map((p) => Number(p.cost) || 0));
  const pick = (name) => setUser((current) => (current === name ? "" : name));
  const rangeText = data?.from ? `${data.from} to ${data.to}` : `last ${data?.days ?? period} days`;

  const toolbar = (
    <div className="an-toolbar" role="group" aria-label="Questions filters">
      <label className="an-field an-field-person">
        <span>Person</span>
        <select className="inp" value={user} onChange={(e) => setUser(e.target.value)}>
          <option value="">Everyone{people.length ? ` (${people.length})` : ""}</option>
          {people.map((p) => (
            <option key={p.user_id} value={p.user_id}>{p.user_id} · {plural(p.queries, "question")}</option>
          ))}
          {user && !people.some((p) => p.user_id === user) && <option value={user}>{user}</option>}
        </select>
      </label>
      <div className="an-field">
        <span>Source</span>
        <Segmented label="Questions source" value={source} onChange={setSource} options={[
          { value: "", label: "All", count: data ? allCount : null },
          { value: "web", label: "Web app", count: data ? sourceCounts.web || 0 : null },
          { value: "mcp", label: "MCP agents", count: data ? sourceCounts.mcp || 0 : null },
        ]} />
      </div>
      <div className="an-field">
        <span>Period</span>
        <Segmented label="Questions period" value={period} onChange={setPeriod} options={PERIODS} />
      </div>
      {custom && (
        <div className="an-field an-dates">
          <label>
            <span>From</span>
            <input type="date" className="inp" value={from} max={to || today} onChange={(e) => setFrom(e.target.value)} />
          </label>
          <span className="an-dash" aria-hidden="true">–</span>
          <label>
            <span>To</span>
            <input type="date" className="inp" value={to} min={from} max={today} onChange={(e) => setTo(e.target.value)} />
          </label>
        </div>
      )}
      {rangeError && <div className="an-field-error" role="alert">{rangeError}</div>}
    </div>
  );

  const table = (
    <DataTable rows={rows.map((q) => ({ ...q, key: q.id }))} columns={[
      { key: "p", label: "Person", render: (q) => q.user_id },
      { key: "s", label: "Source", render: (q) => (q.source === "mcp" ? "MCP agent" : "Web app") },
      { key: "q", label: "Asked", render: (q) => (
        q.question
          ? <span className="an-cell-text">{KINDS[q.request_kind] ? `[${KINDS[q.request_kind]}] ` : ""}{q.question}</span>
          : <span className="muted">Not captured</span>
      ) },
      { key: "kb", label: "Knowledge base", render: (q) => q.kb_name },
      { key: "w", label: "When", render: (q) => formatDate(q.trace_timestamp, true) },
      { key: "t", label: "Tokens", num: true, render: (q) => integer(q.total_tokens) },
      { key: "c", label: "Cost", num: true, render: (q) => money(q.total_cost) },
      { key: "d", label: "Duration", num: true, render: (q) => duration(q.latency_ms) },
    ]} />
  );

  return (
    <ChartCard
      title="Questions by user"
      sub="Who asked what, when, and what it cost, from the web app and MCP agents. You see your own questions, plus everyone's on knowledge bases you own."
      toolbar={toolbar}
      table={rows.length ? table : null}
    >
      <ErrorBox error={error} />
      {data && (
        <div className={`an-qbody${loading ? " an-busy" : ""}`} aria-busy={loading}>
          <div className="an-qstats">
            <div><strong>{integer(totals.queries)}</strong><span>{totals.queries === 1 ? "question" : "questions"}</span></div>
            <div><strong>{money(totals.cost)}</strong><span>cost</span></div>
            <div><strong>{compact(totals.total_tokens)}</strong><span>tokens</span></div>
            <div><strong>{duration(totals.avg_latency_ms)}</strong><span>avg answer time</span></div>
            <div><strong>{integer(totals.users)}</strong><span>{totals.users === 1 ? "person" : "people"}</span></div>
            <div className="an-qrange">
              {user ? `${user} · ` : ""}{source === "mcp" ? "MCP agents · " : source === "web" ? "web app · " : ""}
              {rangeText}{kb ? ` · ${kb}` : ""}
            </div>
          </div>

          {!rows.length ? (
            <div className="an-empty">No questions {user ? `from ${user} ` : ""}in this period.</div>
          ) : (
            <>
              <div className="an-qgrid">
                <div className="an-people" aria-label="People">
                  <div className="an-people-head"><span>Person</span><span>Cost</span></div>
                  {people.map((p) => (
                    <button key={p.user_id} type="button" className={`an-person${user === p.user_id ? " an-is-on" : ""}`}
                            aria-pressed={user === p.user_id} onClick={() => pick(p.user_id)}>
                      <Avatar name={p.user_id} />
                      <span className="an-person-who">
                        <span className="an-person-name">{p.user_id}</span>
                        <span className="an-person-meta">{plural(p.queries, "question")} · {compact(p.total_tokens)} tokens</span>
                        <span className="an-person-bar"><span style={{ width: `${maxCost ? (p.cost / maxCost) * 100 : 0}%` }} /></span>
                      </span>
                      <span className="an-person-cost">{money(p.cost)}</span>
                    </button>
                  ))}
                </div>
                <div className="an-scatter">
                  <div className="an-scatter-cap">Each dot is one question: when it was asked and what it cost. Bigger = more tokens.</div>
                  <ScatterChart
                    points={points}
                    domain={domain}
                    format={money}
                    tickFormat={moneyTick}
                    xLabel={xLabel}
                    color={SINGLE_COLOR}
                    onPick={(p) => setUser(p.q.user_id)}
                    ariaLabel={`${plural(points.length, "question")}, cost against time. Use the arrow keys to read each one.`}
                    renderTip={({ q }) => (
                      <>
                        <div className="an-tip-user"><Avatar name={q.user_id} small /><strong>{q.user_id}</strong>
                          <span className="an-tip-label">{formatDate(q.trace_timestamp, true)}</span></div>
                        <div className={q.request_kind === "cypher" ? "an-tip-q an-tip-code" : "an-tip-q"}>
                          {KINDS[q.request_kind] ? `${KINDS[q.request_kind]}: ` : ""}{q.question || "Text not captured"}
                        </div>
                        <div className="an-tip-row"><strong>{money(q.total_cost)}</strong><span className="an-tip-label">cost</span>
                          <strong>{integer(q.total_tokens)}</strong><span className="an-tip-label">tokens</span>
                          <strong>{duration(q.latency_ms)}</strong></div>
                        <div className="an-tip-row">
                          <span className="an-tip-label">{q.kb_name} · {q.source === "mcp" ? "MCP agent" : "web app"} · {operation(q.name)}</span>
                        </div>
                      </>
                    )}
                  />
                </div>
              </div>

              <ol className="an-qlist" aria-label="Questions, newest first">
                {rows.slice(0, shown).map((q) => (
                  <li key={q.id} className="an-q">
                    <Avatar name={q.user_id} />
                    <div className="an-q-body">
                      <div className="an-q-head">
                        <button type="button" className="an-q-user" onClick={() => pick(q.user_id)}
                                title={user === q.user_id ? "Show everyone" : `Only ${q.user_id}`}>{q.user_id}</button>
                        <time dateTime={q.trace_timestamp} title={formatDate(q.trace_timestamp, true)}>
                          {relativeTime(q.trace_timestamp)}
                        </time>
                      </div>
                      <RequestText q={q} open={open === q.id} onToggle={() => setOpen(open === q.id ? null : q.id)} />
                      <div className="an-q-meta">
                        <SourcePill source={q.source} />
                        <span className="an-chip">{q.kb_name}</span>
                        <span className="an-chip an-chip-ghost">{operation(q.name)}</span>
                        {q.error_count > 0 && <span className="an-chip an-chip-bad">Error</span>}
                      </div>
                    </div>
                    <dl className="an-q-stats">
                      <div><dt>Cost</dt><dd>{money(q.total_cost)}</dd></div>
                      <div>
                        <dt>Tokens</dt>
                        <dd title={`${integer(q.input_tokens)} input · ${integer(q.output_tokens)} output`}>{compact(q.total_tokens)}</dd>
                      </div>
                      <div><dt>Time</dt><dd>{duration(q.latency_ms)}</dd></div>
                    </dl>
                    {q.url ? (
                      <a className="an-q-link" href={q.url} target="_blank" rel="noopener noreferrer"
                         aria-label={`Open ${q.user_id}'s question in Langfuse`} title="Open in Langfuse">↗</a>
                    ) : <span />}
                  </li>
                ))}
              </ol>
              <div className="an-qfoot">
                {rows.length > shown && (
                  <button type="button" className="btn sec sm" onClick={() => setShown(shown + PAGE)}>
                    Show {Math.min(PAGE, rows.length - shown)} more
                  </button>
                )}
                <span className="muted small">
                  Showing {integer(Math.min(shown, rows.length))} of {integer(totals.queries)}
                  {data.truncated ? " (newest 500 loaded; narrow the period for older ones)" : ""}
                </span>
              </div>
            </>
          )}
          {!data.questions_stored && (
            <div className="muted small">Question text is switched off (ANALYTICS_STORE_QUESTIONS=false).</div>
          )}
        </div>
      )}
      {!data && loading && <div className="an-empty">Loading questions…</div>}
    </ChartCard>
  );
}
