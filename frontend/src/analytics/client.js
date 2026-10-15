// The Analytics tab's only link to its backend (/api/analytics, backend/app/langfuse_analytics). Every URL and
// parameter lives here, and the response shapes are the ones in backend/app/langfuse_analytics/schemas.py; if the
// API format changes, this file (and the schema) is what changes.
import { api } from "../api.js";

export const API_VERSION = 1;
const BASE = "/analytics";

function query(params) {
  const p = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") p.set(key, String(value));
  });
  return p.toString();
}

// -> SyncStatus
export const getSyncStatus = () => api(`${BASE}/langfuse/status`);

// -> SyncStatus (the sync runs in the background; poll getSyncStatus while `running`)
export const startSync = (full = false) => api(`${BASE}/langfuse/sync`, { method: "POST", json: { full } });

// -> Summary. kb: "" = all accessible; days: 0 = all time
export const getSummary = ({ kb = "", days = 30 } = {}) => api(`${BASE}/summary?${query({ kb, days })}`);

// -> Questions. Either days, or a custom range from/to (YYYY-MM-DD, inclusive). source: "web" | "mcp" | ""
export function getQuestions({ kb = "", user = "", source = "", days = 30, from = "", to = "", limit = 500 } = {}) {
  const range = from && to ? { from, to } : { days };
  return api(`${BASE}/queries?${query({ kb, user, source, limit, ...range })}`);
}
