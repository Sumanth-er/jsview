// Dependency-free SVG/HTML charts for the Analytics tab: columns (stacked or single), smooth area, donut, horizontal
// bars, scatter (one dot per question), sparklines, count-up numbers, stat tiles and the card that pairs every chart
// with a table view. Labels come from API data and are rendered as React text (never HTML).
//
// Performance: the heavy layers (bars, dots, paths, grid) are memoised, so hovering re-renders only a small overlay;
// pointer handlers run at most once per animation frame; animations use only transform and opacity (GPU-friendly)
// and are skipped when the user prefers reduced motion. Every class is prefixed "an-" so the app's own CSS never
// reaches inside (the app has e.g. .num and .bar).
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { monotonePath, niceScale } from "./format.js";

const GAP = 2;        // surface gap between stacked segments
const BAR_MAX = 24;   // column thickness cap
const RADIUS = 4;     // rounded data end

export const prefersReducedMotion = () =>
  typeof window !== "undefined" && Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);

export function useWidth() {
  const ref = useRef(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    setWidth(Math.floor(el.getBoundingClientRect().width));
    let frame = 0;
    const observer = new ResizeObserver(([entry]) => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => setWidth(Math.floor(entry.contentRect.width)));
    });
    observer.observe(el);
    return () => { cancelAnimationFrame(frame); observer.disconnect(); };
  }, []);
  return [ref, width];
}

// Pointer position relative to the element, delivered at most once per animation frame.
function useFramePointer(onPoint) {
  const frame = useRef(0);
  const latest = useRef(null);
  const handler = useRef(onPoint);
  handler.current = onPoint;
  useEffect(() => () => cancelAnimationFrame(frame.current), []);
  return useCallback((e) => {
    const box = e.currentTarget.getBoundingClientRect();
    latest.current = { x: e.clientX - box.left, y: e.clientY - box.top };
    if (frame.current) return;
    frame.current = requestAnimationFrame(() => {
      frame.current = 0;
      handler.current(latest.current);
    });
  }, []);
}

const svgId = (id) => id.replace(/[^a-zA-Z0-9_-]/g, "");

function roundedTop(x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + h}Z`;
}

function axisTicks(scale) {
  const ticks = [];
  for (let v = 0; v <= scale.max + scale.step / 2; v += scale.step) ticks.push(v);
  return ticks;
}

function Grid({ scale, y, left, right, format }) {
  return axisTicks(scale).map((v) => (
    <g key={v}>
      <line x1={left} x2={right} y1={y(v)} y2={y(v)} stroke={v === 0 ? "var(--an-axis)" : "var(--an-grid)"}
            strokeWidth="1" shapeRendering="crispEdges" />
      <text className="an-axis-text" x={left - 10} y={y(v)} dy="0.32em" textAnchor="end">{format(v, scale.step)}</text>
    </g>
  ));
}

function XLabels({ rows, x, y, every, n }) {
  return rows.map((r, i) => (i % every === (n - 1) % every ? (
    <text key={r.key} className="an-axis-text" x={x(i)} y={y}
          textAnchor={i === 0 && n > 1 ? "start" : i === n - 1 && n > 1 ? "end" : "middle"}>
      {r.label}
    </text>
  ) : null));
}

function arrowKeys(n, setActive) {
  return (e) => {
    const moves = { ArrowLeft: -1, ArrowRight: 1 };
    if (e.key in moves) {
      e.preventDefault();
      setActive((i) => Math.max(0, Math.min(n - 1, (i ?? n - 1) + moves[e.key])));
    } else if (e.key === "Home") {
      e.preventDefault();
      setActive(0);
    } else if (e.key === "End") {
      e.preventDefault();
      setActive(n - 1);
    } else if (e.key === "Escape") setActive(null);
  };
}

const tipSide = (x, width) => (x > width / 2 ? { right: width - x + 12 } : { left: x + 12 });

// ------------------------------------------------------------------ small pieces
export function Segmented({ options, value, onChange, label, size }) {
  return (
    <div className={`an-seg${size === "sm" ? " an-seg-sm" : ""}`} role="group" aria-label={label}>
      {options.map((o) => (
        <button key={String(o.value)} type="button" aria-pressed={o.value === value} onClick={() => onChange(o.value)}>
          {o.label}
          {o.count != null && <span className="an-seg-count">{o.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Legend({ items }) {
  if (!items || items.length < 2) return null;  // one series: the title names it
  return (
    <ul className="an-legend" aria-label="Legend">
      {items.map((s) => (
        <li key={s.key}><span className="an-swatch" style={{ background: s.color }} aria-hidden="true" />{s.label}</li>
      ))}
    </ul>
  );
}

// A number that eases from its previous value to the new one (about 0.7 s, one small component re-rendering).
export function CountUp({ value, format, duration = 700 }) {
  const target = Number(value) || 0;
  const [shown, setShown] = useState(() => (prefersReducedMotion() ? target : 0));
  const current = useRef(shown);
  useEffect(() => {
    if (prefersReducedMotion() || current.current === target) {
      current.current = target;
      setShown(target);
      return undefined;
    }
    const from = current.current;
    const start = performance.now();
    let frame = requestAnimationFrame(function tick(now) {
      const p = Math.min(1, (now - start) / duration);
      current.current = from + (target - from) * (1 - (1 - p) ** 3);
      setShown(current.current);
      if (p < 1) frame = requestAnimationFrame(tick);
    });
    return () => cancelAnimationFrame(frame);
  }, [target, duration]);
  return <>{format(shown)}</>;
}

export function Sparkline({ values, color, width = 96, height = 30 }) {
  const gid = svgId(useId());
  if (!values || values.length < 2 || !values.some((v) => v > 0)) return null;
  const max = Math.max(...values);
  const pts = values.map((v, i) => [2 + (i / (values.length - 1)) * (width - 6), height - 3 - (v / max) * (height - 8)]);
  const line = monotonePath(pts);
  const last = pts[pts.length - 1];
  return (
    <svg className="an-spark" width={width} height={height} aria-hidden="true">
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={color} stopOpacity="0.22" />
          <stop offset="1" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={`${line}L${last[0]},${height}L${pts[0][0]},${height}Z`} fill={`url(#${gid})`} />
      <path d={line} fill="none" stroke={color} strokeWidth="1.75" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={last[0]} cy={last[1]} r="3" fill={color} stroke="#fff" strokeWidth="1.5" />
    </svg>
  );
}

export function StatTile({ label, value, format, sub, delta, spark, dot }) {
  return (
    <div className="card an-tile">
      <div className="an-tile-top">
        <span className="an-tile-label">
          {dot && <span className="an-dot" style={{ background: dot }} aria-hidden="true" />}
          {label}
        </span>
        {delta && <span className={`an-delta an-delta-${delta.dir}`} title={delta.title}>{delta.text}</span>}
      </div>
      <div className="an-tile-main">
        <div className="an-tile-value">{format ? <CountUp value={value} format={format} /> : value}</div>
        {spark}
      </div>
      {sub && <div className="an-tile-sub">{sub}</div>}
    </div>
  );
}

// A chart card. `toolbar` (filters) stays visible; "Chart | Table" swaps the chart for its table twin.
export function ChartCard({ title, sub, legend, table, toolbar, span = 6, children }) {
  const [view, setView] = useState("chart");
  const showTable = table && view === "table";
  return (
    <section className={`card an-card an-s${span}`} aria-label={title}>
      <header className="an-card-head">
        <div className="an-card-titles">
          <h2 className="an-card-title">{title}</h2>
          {sub && <p className="an-card-sub">{sub}</p>}
        </div>
        {table && (
          <Segmented size="sm" label={`${title}: view`} value={view} onChange={setView}
                     options={[{ value: "chart", label: "Chart" }, { value: "table", label: "Table" }]} />
        )}
      </header>
      {toolbar}
      {!showTable && legend}
      {showTable ? <div className="an-fade-in">{table}</div> : children}
    </section>
  );
}

// Plain data table with right-aligned numeric columns. columns: [{key, label, num?, render(row)}]
export function DataTable({ rows, columns }) {
  return (
    <div className="an-table-wrap">
      <table className="an-table">
        <thead>
          <tr>{columns.map((c) => <th key={c.key} className={c.num ? "an-num" : undefined}>{c.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              {columns.map((c) => <td key={c.key} className={c.num ? "an-num" : undefined}>{c.render(r)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Tooltip({ title, series, values, format, details, style, children }) {
  const list = series || [];
  const shown = list.filter((s) => list.length === 1 || values[s.key]);
  const total = list.reduce((sum, s) => sum + (Number(values?.[s.key]) || 0), 0);
  return (
    <div className="an-tip" style={style} role="status">
      {title && <div className="an-tip-title">{title}</div>}
      {(shown.length ? shown : list.slice(0, 1)).map((s) => (
        <div className="an-tip-row" key={s.key}>
          <span className="an-tip-key" style={{ background: s.color }} aria-hidden="true" />
          <strong>{format(values[s.key])}</strong>
          {list.length > 1 && <span className="an-tip-label">{s.label}</span>}
        </div>
      ))}
      {list.length > 1 && shown.length > 1 && (
        <div className="an-tip-row an-tip-total"><strong>{format(total)}</strong><span className="an-tip-label">Total</span></div>
      )}
      {(details || []).map((d) => (
        <div className="an-tip-row" key={d.label}><strong>{d.value}</strong><span className="an-tip-label">{d.label}</span></div>
      ))}
      {children}
    </div>
  );
}

// ------------------------------------------------------------------ columns over time
function columnSegments(row, series) {
  const segments = [];
  let base = 0;
  series.forEach((s) => {
    const v = Number(row.values[s.key]) || 0;
    if (v > 0) {
      segments.push({ s, from: base, to: base + v });
      base += v;
    }
  });
  return segments;
}

function ColumnShapes({ segments, x, barW, y }) {
  return segments.map(({ s, from, to }, j) => {
    const top = y(to);
    const h = y(from) - (j > 0 ? GAP : 0) - top;
    if (h < 0.5) return null;
    return j === segments.length - 1
      ? <path key={s.key} d={roundedTop(x, top, barW, h, RADIUS)} fill={s.color} />
      : <rect key={s.key} x={x} y={top} width={barW} height={h} fill={s.color} />;
  });
}

// rows: [{key, label, title, values: {seriesKey: n}, details?}], series: [{key, label, color}]
export function ColumnChart({ rows, series, format, tickFormat, wholeNumbers = false, height = 260, ariaLabel }) {
  const [ref, width] = useWidth();
  const [active, setActive] = useState(null);
  const geo = useMemo(() => {
    const margin = { top: 24, right: 6, bottom: 26, left: 56 };
    const plotW = Math.max(0, width - margin.left - margin.right);
    const plotH = height - margin.top - margin.bottom;
    const totals = rows.map((r) => series.reduce((sum, s) => sum + (Number(r.values[s.key]) || 0), 0));
    const scale = niceScale(Math.max(0, ...totals), 4, wholeNumbers);
    const n = rows.length;
    const band = n ? plotW / n : 0;
    const barW = Math.max(1, Math.min(BAR_MAX, band * 0.62));
    const y = (v) => margin.top + plotH - (v / scale.max) * plotH;
    const colX = (i) => margin.left + band * i + (band - barW) / 2;
    const peak = totals.reduce((best, v, i) => (v > (totals[best] ?? 0) ? i : best), 0);
    const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(plotW / 64))));
    return { margin, plotW, plotH, totals, scale, n, band, barW, y, colX, peak, every };
  }, [rows, series, width, height, wholeNumbers]);
  const { margin, plotH, totals, scale, n, band, barW, y, colX, peak, every } = geo;

  // the expensive layer: built once per data / size change, never on hover
  const bars = useMemo(() => (
    <g className="an-bars">
      {rows.map((r, i) => (
        <g key={r.key} className="an-col" style={{ animationDelay: `${Math.min(i * 10, 300)}ms` }}>
          <ColumnShapes segments={columnSegments(r, series)} x={colX(i)} barW={barW} y={y} />
        </g>
      ))}
    </g>
  ), [rows, series, colX, barW, y]);
  const frame = useMemo(() => (
    <>
      <Grid scale={scale} y={y} left={margin.left} right={width - margin.right} format={tickFormat || format} />
      <XLabels rows={rows} n={n} every={every} y={height - 6} x={(i) => margin.left + band * (i + 0.5)} />
    </>
  ), [scale, y, margin, width, tickFormat, format, rows, n, every, height, band]);

  const onPoint = useFramePointer(({ x }) => {
    const i = Math.floor((x - margin.left) / (band || 1));
    setActive(i >= 0 && i < n ? i : null);
  });
  const tipX = active != null ? margin.left + band * (active + 0.5) : 0;

  return (
    <div className="an-chart" ref={ref} style={{ height }}>
      {width > 0 && (
        <svg width={width} height={height} role="img" tabIndex={0} aria-label={ariaLabel}
             className={active != null ? "an-has-active" : undefined}
             onKeyDown={arrowKeys(n, setActive)} onFocus={() => setActive((i) => i ?? n - 1)}
             onBlur={() => setActive(null)} onMouseMove={onPoint} onMouseLeave={() => setActive(null)}>
          {frame}
          {active != null && (
            <rect x={margin.left + band * active} y={margin.top - 10} width={band} height={plotH + 10} rx="6"
                  fill="var(--an-hover)" />
          )}
          {bars}
          {active != null && rows[active] && (
            <g className="an-col-focus">
              <ColumnShapes segments={columnSegments(rows[active], series)} x={colX(active)} barW={barW} y={y} />
            </g>
          )}
          {active == null && totals[peak] > 0 && (
            <text className="an-peak" x={margin.left + band * (peak + 0.5)} y={y(totals[peak]) - 8}
                  textAnchor={peak > n - 3 ? "end" : peak < 2 ? "start" : "middle"}>
              {format(totals[peak])}
            </text>
          )}
        </svg>
      )}
      {active != null && rows[active] && (
        <Tooltip title={rows[active].title} series={series} values={rows[active].values} format={format}
                 details={rows[active].details} style={tipSide(tipX, width)} />
      )}
    </div>
  );
}

// ------------------------------------------------------------------ one series as a smooth area
export function AreaChart({ rows, valueKey, label, color, format, tickFormat, wholeNumbers = false, height = 220,
  ariaLabel }) {
  const [ref, width] = useWidth();
  const [active, setActive] = useState(null);
  const gid = svgId(useId());
  const geo = useMemo(() => {
    const margin = { top: 24, right: 18, bottom: 26, left: 52 };
    const plotW = Math.max(0, width - margin.left - margin.right);
    const plotH = height - margin.top - margin.bottom;
    const values = rows.map((r) => Number(r.values[valueKey]) || 0);
    const scale = niceScale(Math.max(0, ...values), 4, wholeNumbers);
    const n = rows.length;
    const x = (i) => margin.left + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
    const y = (v) => margin.top + plotH - (v / scale.max) * plotH;
    const line = monotonePath(values.map((v, i) => [x(i), y(v)]));
    const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(plotW / 64))));
    return { margin, plotW, values, scale, n, x, y, line, every };
  }, [rows, valueKey, width, height, wholeNumbers]);
  const { margin, plotW, values, scale, n, x, y, line, every } = geo;
  const last = n - 1;

  const body = useMemo(() => n > 0 && (
    <>
      <Grid scale={scale} y={y} left={margin.left} right={width - margin.right} format={tickFormat || format} />
      <g className="an-area-group">
        <path d={`${line}L${x(last)},${y(0)}L${x(0)},${y(0)}Z`} fill={`url(#${gid})`} />
        <path d={line} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
      </g>
      <XLabels rows={rows} n={n} every={every} y={height - 6} x={x} />
    </>
  ), [n, scale, y, margin, width, tickFormat, format, line, x, last, gid, color, rows, every, height]);

  const onPoint = useFramePointer(({ x: px }) => {
    const i = Math.round(((px - margin.left) / (plotW || 1)) * (n - 1));
    setActive(Math.max(0, Math.min(n - 1, i)));
  });

  return (
    <div className="an-chart" ref={ref} style={{ height }}>
      {width > 0 && n > 0 && (
        <svg width={width} height={height} role="img" tabIndex={0} aria-label={ariaLabel}
             onKeyDown={arrowKeys(n, setActive)} onFocus={() => setActive((i) => i ?? last)}
             onBlur={() => setActive(null)} onMouseMove={onPoint} onMouseLeave={() => setActive(null)}>
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor={color} stopOpacity="0.2" />
              <stop offset="1" stopColor={color} stopOpacity="0.01" />
            </linearGradient>
          </defs>
          {body}
          {active != null ? (
            <>
              <line x1={x(active)} x2={x(active)} y1={margin.top - 6} y2={y(0)} stroke="var(--an-cross)" strokeWidth="1" />
              <circle cx={x(active)} cy={y(values[active])} r="5" fill={color} stroke="#fff" strokeWidth="2" />
            </>
          ) : (
            <>
              <circle cx={x(last)} cy={y(values[last])} r="4.5" fill={color} stroke="#fff" strokeWidth="2" />
              <text className="an-peak" x={x(last)} y={y(values[last]) - 10} textAnchor="end">{format(values[last])}</text>
            </>
          )}
        </svg>
      )}
      {active != null && rows[active] && (
        <Tooltip title={rows[active].title} series={[{ key: valueKey, label, color }]} values={rows[active].values}
                 format={format} details={rows[active].details} style={tipSide(x(active), width)} />
      )}
    </div>
  );
}

// ------------------------------------------------------------------ donut
function arcPath(cx, cy, r0, r1, a0, a1) {
  const end = Math.min(a1, a0 + Math.PI * 2 - 1e-4);
  const large = end - a0 > Math.PI ? 1 : 0;
  const at = (r, a) => `${cx + r * Math.cos(a)},${cy + r * Math.sin(a)}`;
  return `M${at(r1, a0)}A${r1},${r1} 0 ${large} 1 ${at(r1, end)}L${at(r0, end)}A${r0},${r0} 0 ${large} 0 ${at(r0, a0)}Z`;
}

// Part-to-whole ring with the total (or the hovered / focused segment) in the middle. items: [{key,label,color,value}]
// `active` / `onActive` let a neighbouring legend or table drive the highlight.
export function Donut({ items, format, centerLabel = "Total", size = 190, active, onActive }) {
  const c = size / 2;
  const outer = c - 6;
  const inner = outer * 0.66;
  const { arcs, total } = useMemo(() => {
    const shown = items.filter((i) => Number(i.value) > 0);
    const sum = shown.reduce((acc, i) => acc + Number(i.value), 0);
    let angle = -Math.PI / 2;
    return {
      total: sum,
      arcs: shown.map((item) => {
        const a0 = angle;
        angle += (Number(item.value) / sum) * Math.PI * 2;
        return { ...item, d: arcPath(c, c, inner, outer, a0, angle) };
      }),
    };
  }, [items, c, inner, outer]);
  const focus = arcs.find((a) => a.key === active);
  return (
    <svg className="an-donut-svg" width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img"
         aria-label={`${centerLabel} ${format(total)}`} onMouseLeave={() => onActive?.(null)}>
      {!total && <circle cx={c} cy={c} r={(outer + inner) / 2} fill="none" stroke="var(--an-track)" strokeWidth={outer - inner} />}
      <g className={active ? "an-arcs an-has-active" : "an-arcs"}>
        {arcs.map((a) => (
          <path key={a.key} d={a.d} fill={a.color} stroke="#fff" strokeWidth="2" strokeLinejoin="round"
                className={`an-arc${active === a.key ? " an-arc-on" : ""}`} style={{ transformOrigin: `${c}px ${c}px` }}
                onMouseEnter={() => onActive?.(a.key)} />
        ))}
      </g>
      <text x={c} y={c - 2} textAnchor="middle" className="an-donut-value">
        {focus ? format(focus.value) : <CountUp value={total} format={format} />}
      </text>
      <text x={c} y={c + 18} textAnchor="middle" className="an-donut-label">
        {focus ? `${Math.round((focus.value / total) * 100)}% · ${focus.label}` : centerLabel}
      </text>
    </svg>
  );
}

// ------------------------------------------------------------------ horizontal bars
// rows: [{key, label, sub, badge?, values: {seriesKey: n}, details?}]
export function HBarList({ rows, series, format, emptyText = "Nothing to show." }) {
  const [active, setActive] = useState(null);
  const totals = rows.map((r) => series.reduce((sum, s) => sum + (Number(r.values[s.key]) || 0), 0));
  const max = Math.max(0, ...totals);
  const grand = totals.reduce((a, b) => a + b, 0);
  if (!rows.length) return <div className="an-empty">{emptyText}</div>;
  return (
    <div className="an-hbars" onMouseLeave={() => setActive(null)}>
      {rows.map((r, i) => {
        const parts = series.filter((s) => Number(r.values[s.key]) > 0);
        const share = grand ? (totals[i] / grand) * 100 : 0;
        return (
          <div key={r.key} className={`an-hrow${active === i ? " an-is-on" : ""}`} tabIndex={0}
               aria-label={`${r.label}: ${format(totals[i])}`}
               onMouseEnter={() => setActive(i)} onFocus={() => setActive(i)} onBlur={() => setActive(null)}>
            <div className="an-hlabel">
              <div className="an-hlabel-name" title={r.label}>{r.label}{r.badge}</div>
              {r.sub && <div className="an-hlabel-sub" title={r.sub}>{r.sub}</div>}
            </div>
            <div className="an-htrack">
              {max > 0 && totals[i] > 0 && (
                <div className="an-hbar" style={{ width: `${(totals[i] / max) * 100}%`, animationDelay: `${Math.min(i * 40, 320)}ms` }}>
                  {parts.map((s) => (
                    <div key={s.key} className="an-hseg" style={{ flexGrow: Number(r.values[s.key]), background: s.color }} />
                  ))}
                </div>
              )}
            </div>
            <div className="an-hval">
              <strong>{format(totals[i])}</strong>
              <span>{share ? `${share < 1 ? "<1" : Math.round(share)}%` : "—"}</span>
            </div>
            {active === i && (
              <Tooltip title={r.label} series={series} values={r.values} format={format} details={r.details} />
            )}
          </div>
        );
      })}
    </div>
  );
}

// ------------------------------------------------------------------ scatter over time
// Time-axis ticks on calendar boundaries (local midnights, or whole hours for spans under two days).
function timeTicks(t0, t1, maxTicks) {
  const hour = 3600e3;
  const day = 24 * hour;
  const ticks = [];
  const first = new Date(t0);
  if (t1 - t0 >= 2 * day) {
    const step = Math.max(1, Math.ceil((t1 - t0) / day / maxTicks));
    first.setHours(0, 0, 0, 0);
    if (first.getTime() < t0) first.setDate(first.getDate() + 1);
    for (const d = first; d.getTime() <= t1 && ticks.length < 60; d.setDate(d.getDate() + step)) ticks.push(d.getTime());
  } else {
    const step = Math.max(1, Math.ceil((t1 - t0) / hour / maxTicks));
    first.setMinutes(0, 0, 0);
    if (first.getTime() < t0) first.setHours(first.getHours() + 1);
    for (const d = first; d.getTime() <= t1 && ticks.length < 60; d.setHours(d.getHours() + step)) ticks.push(d.getTime());
  }
  return ticks;
}

// One dot per item over time: x = when, y = value, area = size. Hover / arrow keys pick the nearest dot.
// points: [{key, t (ms), y, size}], domain: [t0, t1] (ms), renderTip(point) -> tooltip content.
export function ScatterChart({ points, domain, format, tickFormat, xLabel, color, height = 270, ariaLabel, renderTip,
  onPick }) {
  const [ref, width] = useWidth();
  const [active, setActive] = useState(null);
  const geo = useMemo(() => {
    const margin = { top: 16, right: 18, bottom: 28, left: 56 };
    const plotW = Math.max(0, width - margin.left - margin.right);
    const plotH = height - margin.top - margin.bottom;
    const scale = niceScale(Math.max(0, ...points.map((p) => p.y)));
    // the requested range, widened to every point (the server's calendar day can differ from the browser's)
    const times = points.map((p) => p.t).filter(Number.isFinite);
    const lo = Math.min(...(Number.isFinite(domain[0]) ? [domain[0]] : []), ...times);
    const hi = Math.max(...(Number.isFinite(domain[1]) ? [domain[1]] : []), ...times);
    const mid = Number.isFinite(lo) ? lo : Date.now();
    const [t0, t1] = Number.isFinite(lo) && Number.isFinite(hi) && hi > lo ? [lo, hi] : [mid - 3600e3, mid + 3600e3];
    const x = (t) => margin.left + ((t - t0) / (t1 - t0)) * plotW;
    const y = (v) => margin.top + plotH - (v / scale.max) * plotH;
    const maxSize = Math.max(1, ...points.map((p) => p.size || 0));
    const r = (s) => 4 + 7 * Math.sqrt((s || 0) / maxSize);
    const ordered = [...points].sort((a, b) => a.t - b.t).map((p) => ({ ...p, cx: x(p.t), cy: y(p.y), r: r(p.size) }));
    const xTicks = timeTicks(t0, t1, Math.max(2, Math.min(7, Math.floor(plotW / 100))));
    return { margin, scale, t0, t1, x, y, ordered, xTicks };
  }, [points, domain, width, height]);
  const { margin, scale, t0, t1, x, y, ordered, xTicks } = geo;

  const dots = useMemo(() => (
    <g className="an-dots">
      {ordered.map((p) => (
        <circle key={p.key} cx={p.cx} cy={p.cy} r={p.r} fill={color} fillOpacity="0.55" stroke="#fff" strokeWidth="2" />
      ))}
    </g>
  ), [ordered, color]);
  const frame = useMemo(() => (
    <>
      <Grid scale={scale} y={y} left={margin.left} right={width - margin.right} format={tickFormat || format} />
      {xTicks.map((t, i) => (
        <text key={i} className="an-axis-text" x={x(t)} y={height - 6}
              textAnchor={x(t) - margin.left < 24 ? "start" : width - margin.right - x(t) < 24 ? "end" : "middle"}>
          {xLabel(t, t1 - t0)}
        </text>
      ))}
    </>
  ), [scale, y, margin, width, tickFormat, format, xTicks, x, height, xLabel, t0, t1]);

  const onPoint = useFramePointer(({ x: px, y: py }) => {
    let best = null;
    let bestD = 40 * 40;  // nearest dot within 40 px: no need to land on the pixel
    ordered.forEach((p, i) => {
      const d = (p.cx - px) ** 2 + (p.cy - py) ** 2;
      if (d < bestD) { bestD = d; best = i; }
    });
    setActive(best);
  });
  const hit = active != null ? ordered[active] : null;

  return (
    <div className="an-chart" ref={ref} style={{ height }}>
      {width > 0 && (
        <svg width={width} height={height} role="img" tabIndex={0} aria-label={ariaLabel}
             className={hit ? "an-has-active" : undefined}
             onMouseMove={onPoint} onMouseLeave={() => setActive(null)} onKeyDown={arrowKeys(ordered.length, setActive)}
             onFocus={() => setActive((i) => i ?? (ordered.length ? ordered.length - 1 : null))}
             onBlur={() => setActive(null)} onClick={() => hit && onPick && onPick(hit)}
             style={{ cursor: hit && onPick ? "pointer" : "default" }}>
          {frame}
          {hit && <line x1={hit.cx} x2={hit.cx} y1={margin.top} y2={y(0)} stroke="var(--an-cross)" strokeWidth="1" />}
          {dots}
          {hit && <circle cx={hit.cx} cy={hit.cy} r={hit.r + 2} fill={color} stroke="#fff" strokeWidth="2.5" />}
        </svg>
      )}
      {hit && <div className="an-tip an-tip-wide" style={tipSide(hit.cx, width)} role="status">{renderTip(hit)}</div>}
    </div>
  );
}
