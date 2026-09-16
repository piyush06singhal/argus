/**
 * Typed fetch wrapper for the ARGUS Intelligence backend.
 *
 * Paths include the backend's `/api/v1` prefix (e.g. `/api/v1/projects`).
 * The web app proxies `/api/*` to the backend via `next.config.mjs` rewrites.
 * Server components build an absolute URL against `API_PROXY_TARGET` because
 * Node's `fetch` rejects relative URLs; the browser uses the same relative
 * path, which the rewrite maps to the backend. See `resolveBackendUrl`.
 */

// ---------------------------------------------------------------------------
// Common envelope
// ---------------------------------------------------------------------------

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

interface ApiErrorBody {
  detail?: string;
  message?: string;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------

export type ProjectStatus = 'active' | 'archived' | 'pending';

export interface Project {
  id: string;
  name: string;
  description?: string | null;
  slug: string;
  status: ProjectStatus;
  created_at: string;
  updated_at: string;
}

export interface Environment {
  id: string;
  name: string;
  environment_type?: string | null;
  tags?: Record<string, string> | null;
  created_at: string;
  project_id: string;
}

export type ComponentHealth = 'healthy' | 'degraded' | 'unhealthy' | 'unknown';

export interface Component {
  id: string;
  name: string;
  type?: string | null;
  version?: string | null;
  endpoint?: string | null;
  health?: ComponentHealth;
  tags?: Record<string, string> | null;
  created_at: string;
  project_id: string;
}

export type DependencyStatus = 'operational' | 'partial' | 'outage' | 'unknown';

export interface Dependency {
  id: string;
  source_component_id: string;
  target_component_id: string;
  dependency_type: string;
  status: DependencyStatus;
  latency_ms?: number | null;
  error_rate?: number | null;
  discovered_at: string;
  project_id: string;
}

// ---------------------------------------------------------------------------
// Observability
// ---------------------------------------------------------------------------

export type LogLevel = 'INFO' | 'WARN' | 'ERROR' | 'DEBUG' | 'TRACE';

export interface LogRecord {
  id: string;
  timestamp: string;
  level: LogLevel;
  message: string;
  service: string;
  component_id?: string | null;
  environment_id?: string | null;
  project_id?: string | null;
  trace_id?: string | null;
  attributes?: Record<string, unknown> | null;
}

export type MetricType =
  | 'counter'
  | 'gauge'
  | 'histogram'
  | 'summary'
  | 'up';

export interface Metric {
  id: string;
  timestamp: string;
  metric_name: string;
  metric_type: MetricType;
  value: number;
  unit?: string | null;
  labels?: Record<string, string> | null;
  component_id?: string | null;
  environment_id?: string | null;
  project_id?: string | null;
}

export interface TraceSpan {
  id: string;
  trace_id: string;
  parent_span_id?: string | null;
  name: string;
  service: string;
  start_time: string;
  duration_ms: number;
  component_id?: string | null;
  environment_id?: string | null;
  project_id?: string | null;
  status?: string | null;
  attributes?: Record<string, unknown> | null;
}

export interface Trace {
  id: string;
  trace_id: string;
  name?: string | null;
  service: string;
  started_at: string;
  duration_ms: number;
  status?: string | null;
  span_count?: number | null;
  error_count?: number | null;
  has_errors?: boolean;
  component_id?: string | null;
  environment_id?: string | null;
  project_id?: string | null;
  spans?: TraceSpan[];
}

export interface TraceDetail extends Trace {
  spans: TraceSpan[];
}

export interface ObservabilityEvent {
  id: string;
  timestamp: string;
  event_type: string;
  source: string;
  severity?: string | null;
  payload?: Record<string, unknown> | null;
  metadata?: Record<string, unknown> | null;
  component_id?: string | null;
  environment_id?: string | null;
  project_id?: string | null;
  trace_id?: string | null;
  request_id?: string | null;
  deployment_id?: string | null;
  incident_id?: string | null;
}

// ---------------------------------------------------------------------------
// Ingestion (Phase 1 §39, §45)
// ---------------------------------------------------------------------------

export type IngestionSourceStatus =
  | 'UNKNOWN'
  | 'HEALTHY'
  | 'DEGRADED'
  | 'FAILING';

export type IngestionSourceCategory =
  | 'APPLICATION'
  | 'OTEL'
  | 'PROMETHEUS'
  | 'CLOUD'
  | 'CUSTOM'
  | 'WEBHOOK'
  | 'FILE'
  | 'MOCK';

export interface ObservabilitySource {
  id: string;
  project_id: string;
  environment_id?: string | null;
  name: string;
  source_type: IngestionSourceCategory;
  description?: string | null;
  configuration?: Record<string, unknown> | null;
  status: IngestionSourceStatus;
  last_event_at?: string | null;
  last_success_at?: string | null;
  last_error?: string | null;
  error_count: number;
  consecutive_errors: number;
  event_count: number;
  metadata?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface IngestionSourceHealth {
  id: string;
  name: string;
  source_type: string;
  status: string;
  events_7d: number;
  error_count: number;
  consecutive_errors: number;
  last_success_at?: string | null;
}

export interface IngestionSummary {
  source_count: number;
  status_counts: Record<string, number>;
  dead_letter_count: number;
  events_ingested_7d: number;
  healthy_sources: number;
  failing_sources: number;
}

export interface IngestionFailure {
  id: string;
  fingerprint: string;
  source_id?: string | null;
  source?: string | null;
  project_id?: string | null;
  event_type?: string | null;
  error_type: string;
  error_message: string;
  retry_count: number;
  received_at?: string | null;
  failed_at: string;
  payload_summary?: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------
// Incidents
// ---------------------------------------------------------------------------

export type IncidentSeverity = 'critical' | 'major' | 'minor' | 'low';
export type IncidentStatus =
  | 'detected'
  | 'acknowledged'
  | 'in_progress'
  | 'resolved'
  | 'closed';

export interface Incident {
  id: string;
  title: string;
  severity: IncidentSeverity;
  status: IncidentStatus;
  description?: string | null;
  detected_at: string;
  started_at?: string | null;
  resolved_at?: string | null;
  project_id?: string | null;
  component_id?: string | null;
  assigned_team?: string | null;
  created_at: string;
  updated_at: string;
}

export interface Evidence {
  id: string;
  incident_id: string;
  evidence_type: string;
  data?: Record<string, unknown> | null;
  collection_method?: string | null;
  collected_at: string;
}

// ---------------------------------------------------------------------------
// Deployments
// ---------------------------------------------------------------------------

export type DeploymentStatus =
  | 'in_progress'
  | 'successful'
  | 'failed'
  | 'canceled';

export interface Deployment {
  id: string;
  project_id: string;
  project_name?: string | null;
  component_id: string;
  component_name?: string | null;
  environment_id: string;
  environment_name?: string | null;
  version: string;
  commit?: string | null;
  status: DeploymentStatus;
  deployed_at: string;
  triggered_by?: string | null;
  metadata?: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export interface ServiceHealth {
  status: string;
  timestamp: string;
  version?: string;
  environment?: string;
  [key: string]: unknown;
}

export interface DependencyHealth {
  name: string;
  status: string;
  latency_ms?: number | null;
  error?: string | null;
}

export interface DependenciesHealth {
  status: string;
  timestamp: string;
  dependencies: DependencyHealth[];
}

// ---------------------------------------------------------------------------
// Fetch wrapper
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/**
 * Resolve a backend path to the URL that the current runtime should fetch.
 *
 * - Browser (client components): return the path as-is — the Next.js rewrite
 *   proxies `/api/*` to the backend, so a same-origin relative URL works.
 * - Server (server components): build an absolute URL. Node's global `fetch`
 *   (undici) rejects relative URLs, so we target the API directly using
 *   `API_PROXY_TARGET` (the same host the proxy rewrite uses). Defaults to the
 *   local-dev API.
 */
function resolveBackendUrl(path: string): string {
  if (typeof window === 'undefined') {
    const base =
      process.env.API_PROXY_TARGET ?? process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';
    return `${base}${path}`;
  }
  return path;
}

/**
 * Fetch a path from the backend through the Next.js proxy.
 *
 * The `path` should include the `/api/v1` prefix (e.g. `/api/v1/projects`).
 */
export async function apiFetch<T>(
  path: string,
  options?: RequestInit
): Promise<T> {
  const headers = new Headers(options?.headers);
  headers.set('Accept', 'application/json');
  headers.set('Content-Type', 'application/json');

  const res = await fetch(resolveBackendUrl(path), {
    ...options,
    headers,
    cache: options?.cache ?? 'no-store',
  });

  if (!res.ok) {
    let message = `Request failed with status ${res.status}`;
    try {
      const body: ApiErrorBody = await res.json();
      if (typeof body.detail === 'string') {
        message = body.detail;
      } else if (typeof body.message === 'string') {
        message = body.message;
      }
    } catch {
      // Response body was not JSON; keep the default message.
    }
    throw new ApiError(message, res.status);
  }

  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// Convenience API methods
// ---------------------------------------------------------------------------

export const api = {
  listProjects: (page = 1, pageSize = 20) =>
    apiFetch<PaginatedResponse<Project>>(
      `/api/v1/projects?page=${page}&page_size=${pageSize}`
    ),

  getProject: (id: string) =>
    apiFetch<Project>(`/api/v1/projects/${encodeURIComponent(id)}`),

  listEnvironments: (projectId: string) =>
    apiFetch<PaginatedResponse<Environment>>(
      `/api/v1/projects/${encodeURIComponent(projectId)}/environments`
    ),

  listComponents: (projectId: string) =>
    apiFetch<PaginatedResponse<Component>>(
      `/api/v1/projects/${encodeURIComponent(projectId)}/components`
    ),

  listDependencies: (projectId: string) =>
    apiFetch<PaginatedResponse<Dependency>>(
      `/api/v1/projects/${encodeURIComponent(projectId)}/dependencies`
    ),

  listIncidents: ({
    page = 1,
    pageSize = 20,
    severity,
    status,
  }: {
    page?: number;
    pageSize?: number;
    severity?: IncidentSeverity | string;
    status?: IncidentStatus | string;
  } = {}) => {
    const params = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (severity && severity !== '') {
      params.set('severity', String(severity));
    }
    if (status && status !== '') {
      params.set('status', String(status));
    }
    return apiFetch<PaginatedResponse<Incident>>(
      `/api/v1/incidents?${params.toString()}`
    );
  },

  getIncident: (id: string) =>
    apiFetch<Incident>(`/api/v1/incidents/${encodeURIComponent(id)}`),

  getIncidentEvidence: (id: string) =>
    apiFetch<PaginatedResponse<Evidence>>(
      `/api/v1/incidents/${encodeURIComponent(id)}/evidence`
    ),

  listDeployments: (page = 1, pageSize = 20) =>
    apiFetch<PaginatedResponse<Deployment>>(
      `/api/v1/deployments?page=${page}&page_size=${pageSize}`
    ),

  listEvents: ({ page = 1, pageSize = 20 }: { page?: number; pageSize?: number } = {}) =>
    apiFetch<PaginatedResponse<ObservabilityEvent>>(
      `/api/v1/observability/events?page=${page}&page_size=${pageSize}`
    ),

  listLogs: ({
    page = 1,
    pageSize = 20,
    level,
  }: {
    page?: number;
    pageSize?: number;
    level?: LogLevel | string;
  } = {}) => {
    const params = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (level && level !== '') {
      params.set('level', String(level));
    }
    return apiFetch<PaginatedResponse<LogRecord>>(
      `/api/v1/observability/logs?${params.toString()}`
    );
  },

  listMetrics: ({
    page = 1,
    pageSize = 20,
    name,
  }: {
    page?: number;
    pageSize?: number;
    name?: string;
  } = {}) => {
    const params = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (name && name !== '') {
      params.set('metric_name', String(name));
    }
    return apiFetch<PaginatedResponse<Metric>>(
      `/api/v1/observability/metrics?${params.toString()}`
    );
  },

  listTraces: ({
    page = 1,
    pageSize = 20,
    traceId,
  }: {
    page?: number;
    pageSize?: number;
    traceId?: string;
  } = {}) => {
    const params = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (traceId && traceId !== '') {
      params.set('trace_id', String(traceId));
    }
    return apiFetch<PaginatedResponse<Trace>>(
      `/api/v1/observability/traces?${params.toString()}`
    );
  },

  getTrace: (traceId: string) =>
    apiFetch<TraceDetail>(
      `/api/v1/observability/traces/${encodeURIComponent(traceId)}`
    ),

  listSources: () =>
    apiFetch<{ items: ObservabilitySource[]; total: number }>(
      `/api/v1/ingestion/sources`
    ),

  sourcesHealth: (projectId?: string) => {
    const params = new URLSearchParams();
    if (projectId) {
      params.set('project_id', projectId);
    }
    const qs = params.toString();
    return apiFetch<IngestionSourceHealth[]>(
      `/api/v1/ingestion/sources-health${qs ? `?${qs}` : ''}`
    );
  },

  ingestionStats: () =>
    apiFetch<IngestionSummary>(`/api/v1/ingestion/stats?days=7`),

  deadLetter: (limit = 50) =>
    apiFetch<IngestionFailure[]>(
      `/api/v1/ingestion/dead-letter?limit=${limit}`
    ),

  liveness: () => apiFetch<ServiceHealth>('/health/live'),
  dependencies: () =>
    apiFetch<DependenciesHealth>('/health/dependencies'),
};

// ---------------------------------------------------------------------------
// Formatting helpers (used by several server components)
// ---------------------------------------------------------------------------

export function formatDate(value?: string | null): string {
  if (!value) {
    return '—';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

export function formatDuration(ms: number): string {
  if (ms < 1000) {
    return `${ms.toFixed(1)} ms`;
  }
  if (ms < 60_000) {
    return `${(ms / 1000).toFixed(2)} s`;
  }
  return `${(ms / 60_000).toFixed(2)} min`;
}