// Formatting and series definitions for the Analytics tab (frontend/src/analytics).

// Cost categories, in fixed order: a category keeps its colour whatever the filter (colours in analytics.css).
export const COST_SERIES = [
  { key: "creation_cost", category: "creation", label: "KB creation & ingestion", color: "var(--an-creation)" },
  { key: "rag_cost", category: "rag", label: "RAG / chat", color: "var(--an-rag)" },
  { key: "evaluation_cost", category: "evaluation", label: "Evaluation", color: "var(--an-evaluation)" },
  { key: "other_cost", category: "other", label: "Other", color: "var(--an-other)" },
];

export const SINGLE_COLOR = "var(--an-single)";

const compactFmt = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

export function compact(v) {
  return compactFmt.format(Number(v) || 0);
}

export function integer(v) {
  return Math.round(Number(v) || 0).toLocaleString("en-US");
}

// USD with enough decimals for LLM-sized amounts ($0.0042) without noise on large ones ($1.2K).
export function money(v) {
  const n = Number(v) || 0;
  const abs = Math.abs(n);
  if (abs === 0) return "$0.00";
  if (abs >= 1000) return `$${compact(n)}`;
  if (abs >= 1) return `$${n.toFixed(2)}`;
  if (abs >= 0.01) return `$${n.toFixed(3)}`;
  if (abs >= 0.0001) return `$${n.toFixed(4)}`;
  return "<$0.0001";
}

// Decimals needed to write the tick step exactly (0.25 -> 2, 0.005 -> 3, 5 -> 0).
function stepDecimals(step) {
  return Math.min(4, (String(Number((step || 1).toFixed(6))).split(".")[1] || "").length);
}

// Axis ticks: decimals follow the tick step so $0 / $0.25 / $0.50 line up.
export function moneyTick(v, step) {
  if (!v) return "$0";
  if (v >= 1000) return `$${compact(v)}`;
  return `$${v.toFixed(stepDecimals(step))}`;
}

export function countTick(v) {
  return v >= 1000 ? compact(v) : integer(v);
}

export function duration(msValue) {
  if (msValue == null) return "—";
  const v = Number(msValue);
  if (v < 1000) return `${Math.round(v)} ms`;
  if (v < 60000) return `${(v / 1000).toFixed(1)} s`;
  return `${(v / 60000).toFixed(1)} min`;
}

// "nice" axis: max rounded up to 1/2/2.5/5 x 10^n and the tick step; whole-number steps for counts.
export function niceScale(max, ticks = 4, wholeNumbers = false) {
  if (!(max > 0)) return wholeNumbers ? { max: 1, step: 1 } : { max: 1, step: 0.25 };
  const raw = max / ticks;
  const mag = 10 ** Math.floor(Math.log10(raw));
  let step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  if (wholeNumbers) step = Math.max(1, Math.ceil(step));
  return { max: Math.ceil(max / step - 1e-9) * step, step };
}

export function plural(n, word) {
  return `${integer(n)} ${word}${Math.round(Number(n) || 0) === 1 ? "" : "s"}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

// Buckets arrive as "YYYY-MM-DD" (the server's calendar day); parse without a time zone shift.
export function bucketLabels(iso, bucket) {
  const [y, m, d] = String(iso).slice(0, 10).split("-").map(Number);
  const day = new Date(Date.UTC(y, m - 1, d));
  const short = `${d} ${MONTHS[m - 1]}`;
  if (bucket === "week") return { short, long: `Week of ${short} ${y}` };
  return { short, long: `${DAYS[day.getUTCDay()]} ${short} ${y}` };
}

// "5 min ago", "3 h ago", "yesterday", "4 days ago", then the date.
export function relativeTime(iso, now = Date.now()) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const minutes = Math.round((now - t) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days === 1) return "yesterday";
  if (days < 7) return `${days} days ago`;
  const d = new Date(t);
  return `${d.getDate()} ${MONTHS[d.getMonth()]}${d.getFullYear() === new Date(now).getFullYear() ? "" : ` ${d.getFullYear()}`}`;
}

export function initials(name) {
  return (name || "?").split(/[\s._-]+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
}

// Change against the previous period for a stat tile; null when there is nothing to compare with.
export function change(current, previous) {
  const cur = Number(current) || 0;
  const prev = Number(previous) || 0;
  if (!prev) return cur ? { dir: "up", text: "New" } : null;
  const pct = ((cur - prev) / prev) * 100;
  if (Math.abs(pct) < 0.5) return { dir: "flat", text: "0%" };
  const shown = Math.abs(pct) >= 100 ? Math.round(Math.abs(pct)) : Math.abs(pct).toFixed(Math.abs(pct) < 10 ? 1 : 0);
  return { dir: pct > 0 ? "up" : "down", text: `${pct > 0 ? "▲" : "▼"} ${shown}%` };
}

// YYYY-MM-DD in the browser's calendar (what <input type="date"> uses).
export function toDateInput(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function daysAgo(n, from = new Date()) {
  const d = new Date(from);
  d.setDate(d.getDate() - n);
  return d;
}

// Smooth line through points without overshoot (monotone cubic), so a curve never dips below zero.
export function monotonePath(points) {
  const n = points.length;
  if (!n) return "";
  if (n === 1) return `M${points[0][0]},${points[0][1]}`;
  const dx = [], slope = [];
  for (let i = 0; i < n - 1; i += 1) {
    dx.push(points[i + 1][0] - points[i][0]);
    slope.push(dx[i] ? (points[i + 1][1] - points[i][1]) / dx[i] : 0);
  }
  const m = [slope[0]];
  for (let i = 1; i < n - 1; i += 1) {
    m.push(slope[i - 1] * slope[i] <= 0 ? 0 : (3 * (dx[i - 1] + dx[i])) /
      ((2 * dx[i] + dx[i - 1]) / slope[i - 1] + (dx[i] + 2 * dx[i - 1]) / slope[i]));
  }
  m.push(slope[n - 2]);
  let d = `M${points[0][0]},${points[0][1]}`;
  for (let i = 0; i < n - 1; i += 1) {
    const [x0, y0] = points[i];
    const [x1, y1] = points[i + 1];
    const h = dx[i] / 3;
    d += `C${x0 + h},${y0 + m[i] * h} ${x1 - h},${y1 - m[i + 1] * h} ${x1},${y1}`;
  }
  return d;
}
