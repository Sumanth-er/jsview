// Analytics tab: Langfuse cost and usage per knowledge base. Plug-in page (frontend/src/analytics); its only links to
// the rest of the app are one route in App.jsx and one tab in components/common.jsx. Data comes from client.js
// (/api/analytics, backend/app/langfuse_analytics), filled by the "Sync from Langfuse" button.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { formatDate } from "../api.js";
import { ErrorBox, TypeBadge, usePoll } from "../components/common.jsx";
import {
  AreaChart, ChartCard, ColumnChart, DataTable, HBarList, Legend, Segmented, Sparkline, StatTile,
} from "./charts.jsx";
import { getSummary, getSyncStatus, startSync as postSync } from "./client.js";
import CostBreakdown from "./CostBreakdown.jsx";
import {
  COST_SERIES, SINGLE_COLOR, bucketLabels, change, compact, countTick, duration, integer, money, moneyTick, plural,
} from "./format.js";
import UserQueries from "./UserQueries.jsx";
import "./analytics.css";

const RANGES = [
  { value: 7, label: "7 days" },
  { value: 30, label: "30 days" },
  { value: 90, label: "90 days" },
  { value: 0, label: "All time" },
];
const CATEGORY_SERIES = Object.fromEntries(COST_SERIES.map((s) => [s.category, s]));
const tokensText = (v) => compact(v);

function SyncBar({ sync, error, onSync }) {
  const running = Boolean(sync?.running);
  const configured = sync?.configured !== false;
  const state = !sync ? "idle" : !configured ? "off" : running ? "busy" : sync.ok === false ? "bad" : "good";
  return (
    <div className="card an-sync">
      <span className={`an-sync-dot an-sync-${state}`} aria-hidden="true" />
      <div className="an-sync-text">
        <div className="an-sync-title">Langfuse sync</div>
        {!sync ? (
          <div className="muted small">Checking…</div>
        ) : !configured ? (
          <div className="muted small">
            Langfuse is not configured. Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in .env and restart the backend.
          </div>
        ) : (
          <div className="muted small">
            {sync.last_synced_at ? `Last synced ${formatDate(sync.last_synced_at, true)}` : "Never synced"}
            {` · ${plural(sync.stored_traces, "trace")} and ${plural(sync.stored_observations, "observation")} stored`}
            {sync.langfuse_url && <> · <a className="an-ext" href={sync.langfuse_url} target="_blank" rel="noopener noreferrer">Open Langfuse ↗</a></>}
          </div>
        )}
        {running && (
          <div className="small" role="status">
            Syncing… {integer(sync.traces)} traces, {integer(sync.observations)} observations so far
          </div>
        )}
        {!running && sync?.message && (
          <div className={`small ${sync.ok === false ? "an-bad" : "an-good"}`} role="status">{sync.message}</div>
        )}
        <ErrorBox error={error} />
      </div>
      <div className="an-sync-actions">
        <button className="btn sec sm" disabled={running || !configured} onClick={() => onSync(true)}
                title={`Re-read the last ${sync?.lookback_days ?? 90} days from Langfuse`}>
          Full re-sync
        </button>
        <button className="btn" disabled={running || !configured} onClick={() => onSync(false)}>
          {running && <span className="an-spin" aria-hidden="true" />}
          {running ? "Syncing…" : "Sync from Langfuse"}
        </button>
      </div>
    </div>
  );
}

export default function Analytics() {
  const [params, setParams] = useSearchParams();
  const kb = params.get("kb") || "";
  const daysParam = Number(params.get("days"));
  const days = RANGES.some((r) => r.value === daysParam) && params.get("days") !== null ? daysParam : 30;
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [sync, setSync] = useState(null);
  const [syncError, setSyncError] = useState(null);
  const [reloadKey, setReloadKey] = useState(0);

  const setFilter = (key, value) => {
    const next = new URLSearchParams(params);
    if (value === "" || value == null) next.delete(key);
    else next.set(key, String(value));
    setParams(next, { replace: true });
  };

  const load = useCallback(() => {
    setLoading(true);
    return getSummary({ kb, days })
      .then((d) => { setData(d); setError(null); })
      .catch(setError)
      .finally(() => setLoading(false));
  }, [kb, days]);
  useEffect(() => { load(); }, [load]);

  const loadStatus = useCallback(() => getSyncStatus().then(setSync).catch(setSyncError), []);
  useEffect(() => { loadStatus(); }, [loadStatus]);
  const running = Boolean(sync?.running);
  usePoll(loadStatus, 1500, running);

  // Reload the charts once a sync finishes.
  const wasRunning = useRef(false);
  useEffect(() => {
    if (running) wasRunning.current = true;
    else if (wasRunning.current) {
      wasRunning.current = false;
      load();
      setReloadKey((k) => k + 1);
    }
  }, [running, load]);

  const onSync = async (full) => {
    setSyncError(null);
    try {
      setSync(await postSync(full));
    } catch (e) {
      setSyncError(e);
    }
  };

  const view = useMemo(() => {
    if (!data) return null;
    const bucket = data.bucket;
    const series = data.series.map((r) => {
      const { short, long } = bucketLabels(r.bucket, bucket);
      const cost = COST_SERIES.reduce((sum, s) => sum + (Number(r[s.key]) || 0), 0);
      return { key: String(r.bucket), label: short, title: long, raw: r, values: { ...r, cost } };
    });
    const costSeries = COST_SERIES.filter((s) => series.some((r) => Number(r.values[s.key]) > 0));
    const kbTypes = Object.fromEntries(data.knowledge_bases.map((k) => [k.kb_name, k.kb_type]));
    return {
      bucket,
      series,
      costSeries: costSeries.length ? costSeries : COST_SERIES.slice(0, 2),
      spark: (key) => series.map((r) => Number(r.values[key]) || 0),
      tokens: series.map((r) => ({
        ...r,
        details: [
          { label: "input", value: integer(r.raw.input_tokens) },
          { label: "output", value: integer(r.raw.output_tokens) },
        ],
      })),
      byKb: data.by_kb.map((r) => {
        const type = r.kb_type || kbTypes[r.kb_name];
        return {
          key: r.kb_name,
          label: r.kb_name,
          badge: type && <span className="an-badge"><TypeBadge type={type} /></span>,
          sub: `${plural(r.rag_traces, "question")} · ${compact(r.total_tokens)} tokens`,
          values: r,
          raw: r,
          type,
        };
      }),
      byOperation: data.by_operation.map((r) => {
        const s = CATEGORY_SERIES[r.category] || CATEGORY_SERIES.other;
        return {
          key: `${r.operation}|${r.category}`,
          label: r.operation,
          sub: `${plural(r.traces, "trace")} · avg ${duration(r.avg_latency_ms)}`,
          values: { [s.key]: r.cost },
          raw: r,
          details: [{ label: "tokens", value: integer(r.total_tokens) }],
        };
      }),
    };
  }, [data]);

  const t = data?.totals || {};
  const prev = data?.previous;
  const rangeLabel = RANGES.find((r) => r.value === days)?.label.toLowerCase();
  const scope = kb ? kb : "all your knowledge bases";
  const noData = data && !Number(t.traces);
  const perQuestion = Number(t.rag_traces) ? Number(t.rag_cost) / Number(t.rag_traces) : null;
  const periodLabel = view?.bucket === "week" ? "Week of" : "Day";
  const delta = (key, fmt) => {
    const c = prev ? change(t[key], prev[key]) : null;
    return c && { ...c, title: `Previous ${days} days: ${fmt(prev[key])}` };
  };
  // Total also includes work no other tile names (RAGAS evaluation, unclassified traces); say so.
  const totalNotes = [
    Number(t.error_traces) ? `${integer(t.error_traces)} with errors` : null,
    Number(t.evaluation_cost) ? `incl. ${money(t.evaluation_cost)} evaluation` : null,
    Number(t.other_cost) ? `incl. ${money(t.other_cost)} other` : null,
  ].filter(Boolean);

  return (
    <div className="page an-root">
      <div className="an-head">
        <div>
          <div className="an-eyebrow">Langfuse observability</div>
          <h1>Analytics</h1>
          <div className="sub">Cost and usage of your knowledge bases: what building them cost, and what answering costs.</div>
        </div>
      </div>

      <SyncBar sync={sync} error={syncError} onSync={onSync} />

      <div className="an-filters" role="group" aria-label="Filters">
        <label className="an-field an-field-kb">
          <span>Knowledge base</span>
          <select className="inp" value={kb} onChange={(e) => setFilter("kb", e.target.value)}>
            <option value="">All my knowledge bases{data ? ` (${data.knowledge_bases.length})` : ""}</option>
            {(data?.knowledge_bases || []).map((k) => (
              <option key={k.kb_name} value={k.kb_name}>{k.kb_name} · {k.kb_type}</option>
            ))}
            {kb && data && !data.knowledge_bases.some((k) => k.kb_name === kb) && <option value={kb}>{kb}</option>}
          </select>
        </label>
        <div className="an-field">
          <span>Period</span>
          <Segmented label="Period" value={days} options={RANGES}
                     onChange={(v) => setFilter("days", v === 30 ? "" : v)} />
        </div>
      </div>

      <ErrorBox error={error} />

      {data && noData && (
        <div className="notice" role="status">
          No Langfuse traces for {scope} in the {rangeLabel === "all time" ? "stored history" : `last ${rangeLabel}`}.
          {!Number(sync?.stored_traces) && sync?.configured !== false && " Press Sync from Langfuse to load them."}
        </div>
      )}

      {view && !noData && (
        <div className={`an-body${loading ? " an-busy" : ""}`} aria-busy={loading}>
          <div className="an-tiles">
            <StatTile label="Total cost" value={t.cost} format={money} delta={delta("cost", money)}
                      spark={<Sparkline values={view.spark("cost")} color={SINGLE_COLOR} />}
                      sub={[plural(t.traces, "trace"), ...totalNotes].join(" · ")} />
            <StatTile label="KB creation cost" dot="var(--an-creation)" value={t.creation_cost} format={money}
                      delta={delta("creation_cost", money)}
                      spark={<Sparkline values={view.spark("creation_cost")} color="var(--an-creation)" />}
                      sub={plural(t.creation_traces, "extraction, build or ingest run")} />
            <StatTile label="RAG / chat cost" dot="var(--an-rag)" value={t.rag_cost} format={money}
                      delta={delta("rag_cost", money)}
                      spark={<Sparkline values={view.spark("rag_cost")} color="var(--an-rag)" />}
                      sub={`${plural(t.rag_traces, "question")}${perQuestion != null ? ` · ${money(perQuestion)} each` : ""}`} />
            <StatTile label="Tokens" value={t.total_tokens} format={tokensText} delta={delta("total_tokens", compact)}
                      spark={<Sparkline values={view.spark("total_tokens")} color={SINGLE_COLOR} />}
                      sub={`${compact(t.input_tokens)} input · ${compact(t.output_tokens)} output`} />
            <StatTile label="Average answer time" value={duration(t.avg_rag_latency_ms)}
                      delta={delta("avg_rag_latency_ms", duration)}
                      sub={`${plural(t.model_calls, "model call")} · ${plural(t.users, "user")}`} />
          </div>

          <div className="an-grid">
            <ChartCard
              title="Cost over time"
              sub={`USD per ${view.bucket}, by kind of work · ${scope}`}
              legend={<Legend items={view.costSeries} />}
              table={
                <DataTable rows={view.series} columns={[
                  { key: "d", label: periodLabel, render: (r) => r.title },
                  ...view.costSeries.map((s) => ({ key: s.key, label: s.label, num: true, render: (r) => money(r.values[s.key]) })),
                  { key: "c", label: "Total", num: true, render: (r) => money(r.values.cost) },
                  { key: "n", label: "Traces", num: true, render: (r) => integer(r.raw.traces) },
                ]} />
              }
            >
              <ColumnChart rows={view.series} series={view.costSeries} format={money} tickFormat={moneyTick}
                           ariaLabel={`Cost per ${view.bucket}, ${money(t.cost)} in total. Use the arrow keys to read each ${view.bucket}.`} />
            </ChartCard>

            <ChartCard title="Cost breakdown"
                       sub={`Where ${money(t.cost)} went: by kind of work, by channel, and what drives it · ${scope}`}>
              <CostBreakdown totals={t} bySource={data.by_source} byKb={data.by_kb} byOperation={data.by_operation}
                             sourceLabels={data.sources} />
            </ChartCard>

            {!kb && (
              <ChartCard
                title="Cost by knowledge base"
                sub="KB creation & ingestion versus RAG / chat cost, with each base's share of the total"
                legend={<Legend items={COST_SERIES.filter((s) => view.byKb.some((r) => Number(r.values[s.key]) > 0))} />}
                table={
                  <DataTable rows={view.byKb} columns={[
                    { key: "kb", label: "Knowledge base", render: (r) => <span className="an-op">{r.label} {r.type && <TypeBadge type={r.type} />}</span> },
                    { key: "c", label: "KB creation", num: true, render: (r) => money(r.raw.creation_cost) },
                    { key: "r", label: "RAG / chat", num: true, render: (r) => money(r.raw.rag_cost) },
                    { key: "e", label: "Evaluation", num: true, render: (r) => money(r.raw.evaluation_cost) },
                    { key: "o", label: "Other", num: true, render: (r) => money(r.raw.other_cost) },
                    { key: "t", label: "Total", num: true, render: (r) => <strong>{money(r.raw.cost)}</strong> },
                    { key: "q", label: "Questions", num: true, render: (r) => integer(r.raw.rag_traces) },
                  ]} />
                }
              >
                <HBarList rows={view.byKb} series={COST_SERIES} format={money}
                          emptyText="No knowledge base has traces in this period." />
              </ChartCard>
            )}

            <UserQueries kb={kb} reloadKey={reloadKey} />

            <ChartCard
              span={3}
              title="Traces over time"
              sub={`Operations per ${view.bucket}: questions, extractions, builds, ingests`}
              table={
                <DataTable rows={view.series} columns={[
                  { key: "d", label: periodLabel, render: (r) => r.title },
                  { key: "n", label: "Traces", num: true, render: (r) => integer(r.raw.traces) },
                ]} />
              }
            >
              <AreaChart rows={view.series} valueKey="traces" label="Traces" color={SINGLE_COLOR} format={integer}
                         tickFormat={countTick} wholeNumbers
                         ariaLabel={`Traces per ${view.bucket}, ${integer(t.traces)} in total. Use the arrow keys to read each ${view.bucket}.`} />
            </ChartCard>

            <ChartCard
              span={3}
              title="Tokens over time"
              sub={`Input + output tokens per ${view.bucket}`}
              table={
                <DataTable rows={view.tokens} columns={[
                  { key: "d", label: periodLabel, render: (r) => r.title },
                  { key: "i", label: "Input", num: true, render: (r) => integer(r.raw.input_tokens) },
                  { key: "o", label: "Output", num: true, render: (r) => integer(r.raw.output_tokens) },
                  { key: "t", label: "Total", num: true, render: (r) => integer(r.raw.total_tokens) },
                ]} />
              }
            >
              <AreaChart rows={view.tokens} valueKey="total_tokens" label="Tokens" color={SINGLE_COLOR}
                         format={integer} tickFormat={countTick} wholeNumbers
                         ariaLabel={`Tokens per ${view.bucket}, ${integer(t.total_tokens)} in total. Use the arrow keys to read each ${view.bucket}.`} />
            </ChartCard>

            <ChartCard
              title="Cost by operation"
              sub="Each Langfuse trace is one operation; colour shows its kind of work"
              legend={<Legend items={COST_SERIES.filter((s) => view.byOperation.some((r) => Number(r.values[s.key]) > 0))} />}
              table={
                <DataTable rows={view.byOperation} columns={[
                  { key: "op", label: "Operation", render: (r) => r.label },
                  { key: "k", label: "Kind", render: (r) => (CATEGORY_SERIES[r.raw.category] || CATEGORY_SERIES.other).label },
                  { key: "n", label: "Traces", num: true, render: (r) => integer(r.raw.traces) },
                  { key: "t", label: "Tokens", num: true, render: (r) => integer(r.raw.total_tokens) },
                  { key: "l", label: "Avg duration", num: true, render: (r) => duration(r.raw.avg_latency_ms) },
                  { key: "c", label: "Cost", num: true, render: (r) => <strong>{money(r.raw.cost)}</strong> },
                ]} />
              }
            >
              <HBarList rows={view.byOperation} series={COST_SERIES} format={money}
                        emptyText="No operations in this period." />
            </ChartCard>

            <ChartCard title="Usage by model" sub="LLM calls and embedding requests inside the traces">
              {data.by_model.length ? (
                <div className="an-models">
                  {data.by_model.map((m) => {
                    const share = Number(t.cost) ? (Number(m.cost) / Number(t.cost)) * 100 : 0;
                    return (
                      <div key={`${m.model}|${m.type}`} className="an-model">
                        <div className="an-model-head">
                          <span className="mono">{m.model}</span>
                          <span className={`an-chip${m.type === "EMBEDDING" ? " an-chip-ghost" : ""}`}>
                            {m.type === "EMBEDDING" ? "Embedding" : "LLM"}
                          </span>
                        </div>
                        <div className="an-model-cost">
                          {money(m.cost)}
                          {m.estimated && <span className="muted small" title="Estimated from ANALYTICS_MODEL_PRICES"> est.</span>}
                        </div>
                        <div className="an-meter" aria-hidden="true"><span style={{ width: `${Math.min(100, share)}%` }} /></div>
                        <dl className="an-model-stats">
                          <div><dt>Calls</dt><dd>{integer(m.calls)}</dd></div>
                          <div><dt>Input</dt><dd>{compact(m.input_tokens)}</dd></div>
                          <div><dt>Output</dt><dd>{compact(m.output_tokens)}</dd></div>
                          <div><dt>Share</dt><dd>{share ? `${share < 1 ? "<1" : Math.round(share)}%` : "—"}</dd></div>
                        </dl>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="an-empty">No model calls in this period.</div>
              )}
            </ChartCard>

            <ChartCard title="Recent traces" sub="Every operation, newest first; open one in Langfuse for the full trace">
              {data.recent.length ? (
                <DataTable rows={data.recent.map((r) => ({ ...r, key: r.id }))} columns={[
                  { key: "w", label: "When", render: (r) => formatDate(r.trace_timestamp, true) },
                  {
                    key: "op", label: "Operation", render: (r) => (
                      <span className="an-op">
                        <span className="an-dot" style={{ background: (CATEGORY_SERIES[r.category] || CATEGORY_SERIES.other).color }} aria-hidden="true" />
                        {r.name || "(unnamed)"}
                        {r.error_count > 0 && <span className="an-chip an-chip-bad">Error</span>}
                      </span>
                    ),
                  },
                  { key: "kb", label: "Knowledge base", render: (r) => r.kb_name },
                  { key: "u", label: "User", render: (r) => r.user_id || <span className="muted">—</span> },
                  { key: "t", label: "Tokens", num: true, render: (r) => integer(r.total_tokens) },
                  { key: "c", label: "Cost", num: true, render: (r) => money(r.total_cost) },
                  { key: "l", label: "Duration", num: true, render: (r) => duration(r.latency_ms) },
                  { key: "x", label: "", render: (r) => r.url && <a className="an-ext" href={r.url} target="_blank" rel="noopener noreferrer">Langfuse ↗</a> },
                ]} />
              ) : (
                <div className="an-empty">No traces in this period.</div>
              )}
            </ChartCard>
          </div>
        </div>
      )}
      {!data && loading && <div className="muted">Loading…</div>}
    </div>
  );
}
