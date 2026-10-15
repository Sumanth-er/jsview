// "Cost breakdown": where the money goes. Donut by kind of work linked to a detail table (hover either one), cost
// by channel (web app, MCP agents, background jobs, evaluation) and the highlights that explain the total.
// Layout: three columns on wide screens, donut + table over channels/highlights on medium, one column on phones.
import { useState } from "react";
import { Donut, useWidth } from "./charts.jsx";
import { COST_SERIES, compact, duration, integer, money, plural } from "./format.js";

const pct = (part, whole) => (whole ? (part / whole) * 100 : 0);
const pctText = (p) => (p ? `${p < 1 ? "<1" : Math.round(p)}%` : "—");

export default function CostBreakdown({ totals: t, bySource, byKb, byOperation, sourceLabels }) {
  const [active, setActive] = useState(null);
  const [ref, width] = useWidth();
  const size = width && width < 560 ? Math.max(150, Math.min(200, width - 40)) : 200;
  const total = Number(t.cost) || 0;
  const kinds = COST_SERIES.map((s) => ({
    ...s,
    value: Number(t[s.key]) || 0,
    traces: Number(t[`${s.category}_traces`]) || 0,
    tokens: Number(t[`${s.category}_tokens`]) || 0,
  })).filter((k) => k.value > 0 || k.traces > 0 || k.category === "creation" || k.category === "rag");
  const channels = (bySource || []).filter((s) => s.traces > 0);
  const maxChannel = Math.max(0, ...channels.map((s) => Number(s.cost) || 0));
  const topKb = (byKb || [])[0];
  const topOp = (byOperation || [])[0];
  const highlights = [
    Number(t.rag_traces) > 0 && {
      key: "q", label: "Per question", value: money(t.rag_cost / t.rag_traces),
      sub: `${plural(t.rag_traces, "question")} · avg ${duration(t.avg_rag_latency_ms)}`,
    },
    Number(t.kbs_built) > 0 && {
      key: "kb", label: "Per knowledge base built", value: money(t.creation_cost / t.kbs_built),
      sub: `${plural(t.kbs_built, "base")} built or extended`,
    },
    topKb && Number(topKb.cost) > 0 && {
      key: "top", label: "Most expensive knowledge base", value: topKb.kb_name,
      sub: `${money(topKb.cost)} · ${pctText(pct(topKb.cost, total))} of the total`,
    },
    topOp && Number(topOp.cost) > 0 && {
      key: "op", label: "Costliest operation", value: topOp.operation,
      sub: `${money(topOp.cost)} over ${plural(topOp.traces, "run")} · ${money(topOp.cost / Math.max(1, topOp.traces))} each`,
    },
    Number(t.total_tokens) > 0 && {
      key: "tok", label: "Output tokens", value: pctText(pct(t.output_tokens, t.total_tokens)),
      sub: `${compact(t.output_tokens)} of ${compact(t.total_tokens)} tokens (priced higher than input)`,
    },
  ].filter(Boolean);

  return (
    <div className="an-breakdown" ref={ref}>
      <div className="an-breakdown-chart">
        <Donut items={kinds} format={money} centerLabel="Total cost" size={size} active={active} onActive={setActive} />
      </div>

      <div className="an-breakdown-table">
        <table className="an-table an-table-plain">
          <thead>
            <tr>
              <th>Kind of work</th>
              <th className="an-num">Cost</th>
              <th className="an-share-col">Share</th>
              <th className="an-num an-hide-sm">Operations</th>
              <th className="an-num an-hide-sm">Avg / operation</th>
              <th className="an-num an-hide-md">Tokens</th>
            </tr>
          </thead>
          <tbody onMouseLeave={() => setActive(null)}>
            {kinds.map((k) => {
              const share = pct(k.value, total);
              return (
                <tr key={k.key} className={active === k.key ? "an-is-on" : undefined} tabIndex={0}
                    onMouseEnter={() => setActive(k.key)} onFocus={() => setActive(k.key)} onBlur={() => setActive(null)}>
                  <td>
                    <span className="an-kind">
                      <span className="an-swatch" style={{ background: k.color }} aria-hidden="true" />{k.label}
                    </span>
                  </td>
                  <td className="an-num"><strong>{money(k.value)}</strong></td>
                  <td className="an-share-col">
                    <span className="an-share">
                      <span className="an-share-track" aria-hidden="true">
                        <span style={{ width: `${Math.min(100, share)}%`, background: k.color }} />
                      </span>
                      <span className="an-share-pct">{pctText(share)}</span>
                    </span>
                  </td>
                  <td className="an-num an-hide-sm">{integer(k.traces)}</td>
                  <td className="an-num an-hide-sm">{k.traces ? money(k.value / k.traces) : "—"}</td>
                  <td className="an-num an-hide-md">{compact(k.tokens)}</td>
                </tr>
              );
            })}
          </tbody>
          <tfoot>
            <tr>
              <td>Total</td>
              <td className="an-num"><strong>{money(total)}</strong></td>
              <td className="an-share-col"><span className="an-share-pct">100%</span></td>
              <td className="an-num an-hide-sm">{integer(t.traces)}</td>
              <td className="an-num an-hide-sm">{t.traces ? money(total / t.traces) : "—"}</td>
              <td className="an-num an-hide-md">{compact(t.total_tokens)}</td>
            </tr>
          </tfoot>
        </table>
      </div>

      <div className="an-breakdown-side">
        <div>
          <h3 className="an-mini-title">By channel</h3>
          {channels.length ? (
            <ul className="an-channels">
              {channels.map((s) => (
                <li key={s.source}>
                  <div className="an-channel-head">
                    <span>{sourceLabels?.[s.source] || s.source}</span>
                    <strong>{money(s.cost)}</strong>
                  </div>
                  <div className="an-channel-track" aria-hidden="true">
                    <span style={{ width: `${maxChannel ? (s.cost / maxChannel) * 100 : 0}%` }} />
                  </div>
                  <div className="an-channel-sub">
                    {plural(s.traces, "operation")} · {pctText(pct(s.cost, total))} of cost
                  </div>
                </li>
              ))}
            </ul>
          ) : <div className="an-empty">No activity.</div>}
        </div>
        {highlights.length > 0 && (
          <div>
            <h3 className="an-mini-title">Highlights</h3>
            <dl className="an-highlights">
              {highlights.map((h) => (
                <div key={h.key}>
                  <dt>{h.label}</dt>
                  <dd title={h.value}>{h.value}</dd>
                  <dd className="an-highlight-sub">{h.sub}</dd>
                </div>
              ))}
            </dl>
          </div>
        )}
      </div>
    </div>
  );
}
