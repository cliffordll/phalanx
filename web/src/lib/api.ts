/**
 * Typed API client for the phalanx dashboard backend.
 *
 * Pares the upstream hermes-agent api.ts (784 lines) down to just the
 * endpoints phalanx ships in §2.7 waves 1-3.  Everything goes through
 * fetchJSON which auto-injects the session token from
 * window.__HERMES_SESSION_TOKEN__ (set by hermes_cli/web_server.py at
 * mount_spa render time).
 */

const SESSION_HEADER = "X-Hermes-Session-Token";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

export async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  const token = window.__HERMES_SESSION_TOKEN__;
  if (token && !headers.has(SESSION_HEADER)) {
    headers.set(SESSION_HEADER, token);
  }
  const res = await fetch(url, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.detail ?? body?.error ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, `${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

// ── Status ────────────────────────────────────────────────────────────

export interface StatusResponse {
  version: string;
  release_date: string;
  phalanx_home: string;
  config_path: string;
  env_path: string;
  model: string | null;
  base_url: string;
  provider: string | null;
  session_count: number;
  active_sessions: number;
  tools: string[];
}

// ── Sessions ──────────────────────────────────────────────────────────

export interface SessionRow {
  id: string;
  source: string;
  model: string | null;
  started_at: number;
  ended_at: number | null;
  end_reason: string | null;
  message_count: number;
  tool_call_count: number;
  input_tokens: number;
  output_tokens: number;
  estimated_cost_usd: number | null;
  title: string | null;
  preview: string;
  last_active: number;
  is_active: boolean;
}

export interface PaginatedSessions {
  sessions: SessionRow[];
  total: number;
  limit: number;
  offset: number;
}

export interface SessionMessage {
  id: number;
  session_id: string;
  role: string;
  content: unknown;
  tool_call_id?: string | null;
  tool_calls?: unknown;
  tool_name?: string | null;
  timestamp: number;
  token_count?: number | null;
  finish_reason?: string | null;
}

export interface SessionMessagesResponse {
  session_id: string;
  messages: SessionMessage[];
}

// ── Logs ──────────────────────────────────────────────────────────────

export interface LogsResponse {
  file: string;
  lines: string[];
}

// ── Analytics ─────────────────────────────────────────────────────────

export interface AnalyticsDailyRow {
  day: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens: number;
  estimated_cost: number;
  actual_cost: number;
  sessions: number;
  api_calls: number;
}

export interface AnalyticsByModelRow {
  model: string;
  input_tokens: number;
  output_tokens: number;
  estimated_cost: number;
  sessions: number;
  api_calls: number;
}

export interface AnalyticsResponse {
  daily: AnalyticsDailyRow[];
  by_model: AnalyticsByModelRow[];
  totals: {
    total_input: number;
    total_output: number;
    total_cache_read: number;
    total_reasoning: number;
    total_estimated_cost: number;
    total_actual_cost: number;
    total_sessions: number;
    total_api_calls: number;
  };
  period_days: number;
}

// ── Env ───────────────────────────────────────────────────────────────

export interface EnvVarInfo {
  is_set: boolean;
  redacted_value: string | null;
  description: string;
  url: string | null;
  category: string;
  is_password: boolean;
  advanced: boolean;
}

// ── Config ────────────────────────────────────────────────────────────

export interface SchemaField {
  type: "string" | "number" | "boolean" | "select";
  default: unknown;
  description?: string;
  options?: string[];
  category: string;
}

export interface SchemaResponse {
  fields: Record<string, SchemaField>;
  category_order: string[];
}

// ── Client surface ────────────────────────────────────────────────────

export const api = {
  // status
  getStatus: () => fetchJSON<StatusResponse>("/api/status"),

  // sessions
  getSessions: (limit = 20, offset = 0) =>
    fetchJSON<PaginatedSessions>(
      `/api/sessions?limit=${limit}&offset=${offset}`,
    ),
  getSessionMessages: (id: string) =>
    fetchJSON<SessionMessagesResponse>(
      `/api/sessions/${encodeURIComponent(id)}/messages`,
    ),
  deleteSession: (id: string) =>
    fetchJSON<{ ok: boolean }>(`/api/sessions/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  setSessionTitle: (id: string, title: string | null) =>
    fetchJSON<{ ok: boolean }>(
      `/api/sessions/${encodeURIComponent(id)}/title`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      },
    ),

  // logs
  getLogs: (params: {
    file?: string;
    lines?: number;
    level?: string;
    component?: string;
    search?: string;
  }) => {
    const qs = new URLSearchParams();
    if (params.file) qs.set("file", params.file);
    if (params.lines) qs.set("lines", String(params.lines));
    if (params.level && params.level !== "ALL") qs.set("level", params.level);
    if (params.component && params.component !== "all")
      qs.set("component", params.component);
    if (params.search) qs.set("search", params.search);
    return fetchJSON<LogsResponse>(`/api/logs?${qs.toString()}`);
  },

  // analytics
  getAnalytics: (days: number) =>
    fetchJSON<AnalyticsResponse>(`/api/analytics/usage?days=${days}`),

  // config
  getConfig: () => fetchJSON<Record<string, unknown>>("/api/config"),
  saveConfig: (config: Record<string, unknown>) =>
    fetchJSON<{ ok: boolean }>("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config }),
    }),
  getConfigRaw: () => fetchJSON<{ yaml: string }>("/api/config/raw"),
  saveConfigRaw: (yaml_text: string) =>
    fetchJSON<{ ok: boolean }>("/api/config/raw", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ yaml_text }),
    }),
  getSchema: () => fetchJSON<SchemaResponse>("/api/config/schema"),

  // env
  getEnvVars: () => fetchJSON<Record<string, EnvVarInfo>>("/api/env"),
  setEnvVar: (key: string, value: string) =>
    fetchJSON<{ ok: boolean; key: string }>("/api/env", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value }),
    }),
  deleteEnvVar: (key: string) =>
    fetchJSON<{ ok: boolean; key: string }>("/api/env", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    }),
  revealEnvVar: (key: string) =>
    fetchJSON<{ key: string; value: string }>("/api/env/reveal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    }),

  // references (§2.8.b wave 3)
  resolveReferences: (text: string) =>
    fetchJSON<ResolveReferencesResponse>("/api/references/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }),
};

// ── References (§2.8.b wave 3) ────────────────────────────────────────

export interface ResolvedReference {
  type: "file" | "diff" | "url" | "session" | string;
  key: string;
  content: string;
  error: string | null;
  content_chars: number;
}

export interface ResolveReferencesResponse {
  rewritten_text: string;
  resolved: ResolvedReference[];
}
