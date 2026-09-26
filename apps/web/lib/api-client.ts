/**
 * ARGUS API client — browser-safe (hardening W7).
 *
 * This is the module every *client* component imports. It resolves the caller's
 * token from the browser cookie and attaches it to every request, so no call
 * site has to remember to.
 *
 * It must stay free of server-only imports (``next/headers`` in particular):
 * a page boundary that imports the whole client would otherwise drag the
 * server's request plumbing into the browser bundle and fail the build. The
 * *server* half — resolving the incoming request's cookie so a server-rendered
 * page shows the caller exactly what their own token may see — lives in
 * ``lib/api.ts``, which registers a resolver into the seam below.
 */

export * from './api-types';
import type {
  PaginatedResponse,
  Project,
  Environment,
  Component,
  Dependency,
  LogLevel,
  LogRecord,
  Metric,
  Trace,
  TraceDetail,
  ObservabilityEvent,
  ObservabilitySource,
  IngestionSourceHealth,
  IngestionSummary,
  IngestionFailure,
  IncidentSeverity,
  IncidentStatus,
  Incident,
  Evidence,
  TimelineEvent,
  AnomalySeverity,
  AnomalyStatus,
  AnomalyType,
  AnomalySource,
  Anomaly,
  AnomalyDetail,
  AnomalyRule,
  AnomalyRuleCreate,
  AffectedComponent,
  IncidentGraphContext,
  DeploymentContextItem,
  ConfigurationContextItem,
  IncidentSummary,
  ReliabilityMetrics,
  IncidentDashboard,
  ConfidenceLevel,
  RootCauseCandidate,
  CausalAnalysisDetail,
  CausalGraph,
  CausalChain,
  Hypotheses,
  EvidenceAnalysis,
  AnalyzeResult,
  AnalysisHistory,
  AnalysisExplanation,
  RelationshipExplanation,
  ExperimentStatus,
  ReproductionExperiment,
  ReproductionPlan,
  ReproductionInput,
  ReproductionFault,
  ReproductionValidation,
  ReproductionArtifact,
  ReproductionEnvironmentSnapshot,
  ReproductionExperimentDetail,
  ReproductionHistory,
  ReproductionStatus,
  ReproductionSafetyPreview,
  ReproductionTelemetry,
  ReproductionComparisonList,
  ReproductionManifest,
  ReproductionMetrics,
  CreateReproductionPayload,
  Deployment,
  ServiceHealth,
  DependenciesHealth,
  WhoAmI,
  TokenList,
  TokenCreate,
  TokenCreateResponse,
  TokenResponse,
} from './api-types';
import { readTokenCookie } from './argus-auth';
interface ApiErrorBody {
  detail?: string | { message?: string; error_code?: string };
  message?: string;
  error_code?: string;
  [key: string]: unknown;
}


//: Defined in ``api-error.ts`` and re-exported here so importing this client
//: from the browser never has to reach the server-only token resolution below.
//: Client components that only need to *interpret* failures import
//: ``@/lib/api-error`` directly.
export { ApiError } from './api-error';
import { ApiError } from './api-error';

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
/**
 * Build a `?project_id=` scope query string.
 *
 * The backend validates ownership when a scope is supplied, which is how the
 * single-tenant Phase 3 API enforces project isolation (§46).
 */
export function scopeQuery(projectId?: string): string {
  return projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
}

/**
 * Build a query string from a param object, skipping `undefined` values.
 *
 * List endpoints in the reliability API take many optional filters; this keeps
 * the call sites readable without producing `key=undefined` noise in URLs.
 */
export function toQuery(
  params: Record<string, string | number | boolean | undefined>
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) {
      search.set(key, String(value));
    }
  }
  return search.toString();
}

export function resolveBackendUrl(path: string): string {
  if (typeof window === 'undefined') {
    const base =
      process.env.API_PROXY_TARGET ?? process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';
    return `${base}${path}`;
  }
  return path;
}

/**
 * Resolve the bearer token for the *current* runtime.
 *
 * Browser: the token the user entered (see `lib/argus-auth.ts`).
 * Server: the same cookie, read off the incoming page request, so a
 * server-rendered page is scoped to exactly the same credential as the
 * client that asked for it. `ARGUS_API_TOKEN` is a documented fallback for
 * automation (server-side smoke tests, scripts) and is never preferred over
 * the caller's own cookie.
 *
 * The server-only branch is imported dynamically: `next/headers` can only be
 * evaluated in a server context, and `api.ts` is also part of the client
 * bundle. The branch is never taken in the browser.
 */
/**
 * Request-scoped credential resolution (hardening W7).
 *
 * ``lib/api.ts`` (server-only) registers a resolver here. What is stored in
 * this module is a *function*, never a credential, so nothing request-specific
 * is held in module state — the resolver reads Next's own per-request cookie
 * store each time it is called. That distinction is the whole reason this is
 * safe in a single module instance shared by concurrent requests.
 */
type TokenResolver = () => Promise<string | null>;

let requestTokenResolver: TokenResolver | null = null;

export function registerRequestTokenResolver(resolver: TokenResolver): void {
  requestTokenResolver = resolver;
}

async function resolveAuthToken(): Promise<string | null> {
  if (typeof window !== 'undefined') {
    return readTokenCookie();
  }
  //: On the server the credential must be *the caller's*, resolved per request
  //: (``cookies()`` in ``lib/api.ts``, which registers the resolver below).
  //: This module deliberately cannot reach that code itself: it is part of the
  //: browser bundle, and importing ``next/headers`` from it fails the build.
  if (requestTokenResolver) {
    const scoped = await requestTokenResolver();
    if (scoped) {
      return scoped;
    }
  }
  // No request scope (build, static generation, a CLI) — fall back to env.
  return process.env.ARGUS_API_TOKEN ?? null;
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

  // Every backend route requires a bearer token (hardening W1). The token is
  // attached here rather than at ~350 call sites, so no request can forget it.
  if (!headers.has('Authorization')) {
    const token = await resolveAuthToken();
    if (token) {
      headers.set('Authorization', `Bearer ${token}`);
    }
  }

  const res = await fetch(resolveBackendUrl(path), {
    ...options,
    headers,
    cache: options?.cache ?? 'no-store',
  });

  if (!res.ok) {
    let message = `Request failed with status ${res.status}`;
    let code: string | undefined;
    try {
      const body: ApiErrorBody = await res.json();
      if (typeof body.detail === 'string') {
        message = body.detail;
      } else if (body.detail && typeof body.detail === 'object') {
        //: A route that attaches facts to its refusal: take the human sentence
        //: *and* the machine code, so a caller can explain the specific case
        //: instead of matching on prose.
        if (typeof body.detail.message === 'string') message = body.detail.message;
        if (typeof body.detail.error_code === 'string') code = body.detail.error_code;
      } else if (typeof body.message === 'string') {
        message = body.message;
      }
      if (typeof body.error_code === 'string') code = body.error_code;
    } catch {
      // Response body was not JSON; keep the default message.
    }
    throw new ApiError(message, res.status, code);
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
    projectId,
    environmentId,
    componentId,
    fingerprint,
  }: {
    page?: number;
    pageSize?: number;
    severity?: IncidentSeverity | string;
    status?: IncidentStatus | string;
    projectId?: string;
    environmentId?: string;
    componentId?: string;
    fingerprint?: string;
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
    if (projectId) {
      params.set('project_id', projectId);
    }
    if (environmentId) {
      params.set('environment_id', environmentId);
    }
    if (componentId) {
      params.set('component_id', componentId);
    }
    if (fingerprint) {
      params.set('fingerprint', fingerprint);
    }
    return apiFetch<PaginatedResponse<Incident>>(
      `/api/v1/incidents?${params.toString()}`
    );
  },

  getIncident: (id: string, projectId?: string) =>
    apiFetch<Incident>(
      `/api/v1/incidents/${encodeURIComponent(id)}${scopeQuery(projectId)}`
    ),

  getIncidentEvidence: (id: string, projectId?: string) =>
    apiFetch<PaginatedResponse<Evidence>>(
      `/api/v1/incidents/${encodeURIComponent(id)}/evidence${scopeQuery(projectId)}`
    ),

  getIncidentTimeline: (id: string, pageSize = 100) =>
    apiFetch<PaginatedResponse<TimelineEvent>>(
      `/api/v1/incidents/${encodeURIComponent(id)}/timeline?page_size=${pageSize}`
    ),

  getIncidentAnomalies: (id: string) =>
    apiFetch<PaginatedResponse<Anomaly>>(
      `/api/v1/incidents/${encodeURIComponent(id)}/anomalies?page_size=100`
    ),

  getIncidentComponents: (id: string) =>
    apiFetch<PaginatedResponse<AffectedComponent>>(
      `/api/v1/incidents/${encodeURIComponent(id)}/components?page_size=100`
    ),

  getIncidentGraph: (id: string) =>
    apiFetch<IncidentGraphContext>(
      `/api/v1/incidents/${encodeURIComponent(id)}/graph`
    ),

  getIncidentDeployments: (id: string) =>
    apiFetch<DeploymentContextItem[]>(
      `/api/v1/incidents/${encodeURIComponent(id)}/deployments`
    ),

  getIncidentConfigurationChanges: (id: string) =>
    apiFetch<ConfigurationContextItem[]>(
      `/api/v1/incidents/${encodeURIComponent(id)}/configuration-changes`
    ),

  getIncidentSummary: (id: string) =>
    apiFetch<IncidentSummary>(
      `/api/v1/incidents/${encodeURIComponent(id)}/summary`
    ),

  /** Lifecycle actions — the backend rejects illegal transitions with 409. */
  acknowledgeIncident: (id: string, actor?: string, note?: string) =>
    apiFetch<Incident>(`/api/v1/incidents/${encodeURIComponent(id)}/acknowledge`, {
      method: 'POST',
      body: JSON.stringify({ actor: actor ?? null, note: note ?? null }),
    }),

  investigateIncident: (id: string, actor?: string, note?: string) =>
    apiFetch<Incident>(`/api/v1/incidents/${encodeURIComponent(id)}/investigate`, {
      method: 'POST',
      body: JSON.stringify({ actor: actor ?? null, note: note ?? null }),
    }),

  mitigateIncident: (id: string, actor?: string, note?: string) =>
    apiFetch<Incident>(`/api/v1/incidents/${encodeURIComponent(id)}/mitigate`, {
      method: 'POST',
      body: JSON.stringify({ actor: actor ?? null, note: note ?? null }),
    }),

  resolveIncident: (id: string, actor?: string, note?: string) =>
    apiFetch<Incident>(`/api/v1/incidents/${encodeURIComponent(id)}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ actor: actor ?? null, note: note ?? null }),
    }),

  reopenIncident: (id: string, actor?: string, note?: string) =>
    apiFetch<Incident>(`/api/v1/incidents/${encodeURIComponent(id)}/reopen`, {
      method: 'POST',
      body: JSON.stringify({ actor: actor ?? null, note: note ?? null }),
    }),

  addIncidentNote: (id: string, title: string, actor?: string, description?: string) =>
    apiFetch<TimelineEvent>(`/api/v1/incidents/${encodeURIComponent(id)}/timeline`, {
      method: 'POST',
      body: JSON.stringify({
        event_type: 'NOTE',
        occurred_at: new Date().toISOString(),
        title,
        description: description ?? null,
        actor: actor ?? null,
      }),
    }),

  // --- Anomalies ---------------------------------------------------------
  listAnomalies: ({
    page = 1,
    pageSize = 25,
    projectId,
    environmentId,
    componentId,
    incidentId,
    severity,
    status,
    anomalyType,
    source,
    metricName,
    includeSuppressed = true,
  }: {
    page?: number;
    pageSize?: number;
    projectId?: string;
    environmentId?: string;
    componentId?: string;
    incidentId?: string;
    severity?: AnomalySeverity | string;
    status?: AnomalyStatus | string;
    anomalyType?: AnomalyType | string;
    source?: AnomalySource | string;
    metricName?: string;
    includeSuppressed?: boolean;
  } = {}) => {
    const params = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
      include_suppressed: String(includeSuppressed),
    });
    if (projectId) params.set('project_id', projectId);
    if (environmentId) params.set('environment_id', environmentId);
    if (componentId) params.set('component_id', componentId);
    if (incidentId) params.set('incident_id', incidentId);
    if (severity && severity !== '') params.set('severity', String(severity));
    if (status && status !== '') params.set('status', String(status));
    if (anomalyType && anomalyType !== '') {
      params.set('anomaly_type', String(anomalyType));
    }
    if (source && source !== '') params.set('source', String(source));
    if (metricName) params.set('metric_name', metricName);
    return apiFetch<PaginatedResponse<Anomaly>>(
      `/api/v1/anomalies?${params.toString()}`
    );
  },

  getAnomaly: (id: string) =>
    apiFetch<AnomalyDetail>(`/api/v1/anomalies/${encodeURIComponent(id)}`),

  acknowledgeAnomaly: (id: string, actor?: string) =>
    apiFetch<Anomaly>(`/api/v1/anomalies/${encodeURIComponent(id)}/acknowledge`, {
      method: 'POST',
      body: JSON.stringify({ actor: actor ?? null }),
    }),

  resolveAnomaly: (id: string, actor?: string) =>
    apiFetch<Anomaly>(`/api/v1/anomalies/${encodeURIComponent(id)}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ actor: actor ?? null }),
    }),

  // --- Anomaly rules & policies -----------------------------------------
  listAnomalyRules: (projectId?: string) => {
    const params = new URLSearchParams({ page_size: '100' });
    if (projectId) params.set('project_id', projectId);
    return apiFetch<PaginatedResponse<AnomalyRule>>(
      `/api/v1/anomaly-rules?${params.toString()}`
    );
  },

  createAnomalyRule: (payload: AnomalyRuleCreate) =>
    apiFetch<AnomalyRule>('/api/v1/anomaly-rules', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  updateAnomalyRule: (id: string, payload: Partial<AnomalyRuleCreate>) =>
    apiFetch<AnomalyRule>(`/api/v1/anomaly-rules/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  // --- Metrics & dashboard ---------------------------------------------
  reliabilityMetrics: (projectId: string, windowSeconds = 86_400) =>
    apiFetch<ReliabilityMetrics>(
      `/api/v1/projects/${encodeURIComponent(projectId)}/reliability-metrics` +
        `?window_seconds=${windowSeconds}`
    ),

  incidentDashboard: (
    projectId: string,
    windowSeconds = 86_400,
    bucketSeconds = 3600
  ) =>
    apiFetch<IncidentDashboard>(
      `/api/v1/projects/${encodeURIComponent(projectId)}/incident-dashboard` +
        `?window_seconds=${windowSeconds}&bucket_seconds=${bucketSeconds}`
    ),

  runDetection: (projectId: string, environmentId?: string) => {
    const params = new URLSearchParams();
    if (environmentId) params.set('environment_id', environmentId);
    const qs = params.toString();
    return apiFetch<Record<string, unknown>>(
      `/api/v1/projects/${encodeURIComponent(projectId)}/anomalies/detect${
        qs ? `?${qs}` : ''
      }`,
      { method: 'POST' }
    );
  },

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

  // --- Causal analysis (Phase 4) -----------------------------------------
  /**
   * Run (or reuse) the causal analysis for an incident.
   *
   * The backend is idempotent: without `force` an unchanged evidence set
   * returns the stored version instead of appending a duplicate.
   */
  analyzeIncidentCausally: (
    id: string,
    { projectId, force = false, trigger = 'manual', requestedBy }: {
      projectId?: string;
      force?: boolean;
      trigger?: string;
      requestedBy?: string;
    } = {}
  ) => {
    const params = new URLSearchParams();
    if (projectId) params.set('project_id', projectId);
    if (force) params.set('force', 'true');
    const qs = params.toString();
    return apiFetch<AnalyzeResult>(
      `/api/v1/incidents/${encodeURIComponent(id)}/analyze${qs ? `?${qs}` : ''}`,
      {
        method: 'POST',
        body: JSON.stringify({ trigger, requested_by: requestedBy ?? null }),
      }
    );
  },

  getCausalAnalysis: (id: string, projectId?: string, analysisId?: string) => {
    const params = new URLSearchParams();
    if (projectId) params.set('project_id', projectId);
    if (analysisId) params.set('analysis_id', analysisId);
    const qs = params.toString();
    return apiFetch<CausalAnalysisDetail>(
      `/api/v1/incidents/${encodeURIComponent(id)}/causal-analysis${qs ? `?${qs}` : ''}`
    );
  },

  getAnalysisHistory: (id: string, projectId?: string, limit = 20) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (projectId) params.set('project_id', projectId);
    return apiFetch<AnalysisHistory>(
      `/api/v1/incidents/${encodeURIComponent(id)}/causal-analysis/history?${params.toString()}`
    );
  },

  getRootCauses: (id: string, projectId?: string) =>
    apiFetch<{ items: RootCauseCandidate[]; total: number }>(
      `/api/v1/incidents/${encodeURIComponent(id)}/root-causes${scopeQuery(projectId)}`
    ),

  getCausalGraph: (id: string, projectId?: string) =>
    apiFetch<CausalGraph>(
      `/api/v1/incidents/${encodeURIComponent(id)}/causal-graph${scopeQuery(projectId)}`
    ),

  getCausalChain: (id: string, projectId?: string) =>
    apiFetch<CausalChain>(
      `/api/v1/incidents/${encodeURIComponent(id)}/causal-chain${scopeQuery(projectId)}`
    ),

  getHypotheses: (id: string, projectId?: string) =>
    apiFetch<Hypotheses>(
      `/api/v1/incidents/${encodeURIComponent(id)}/hypotheses${scopeQuery(projectId)}`
    ),

  getEvidenceAnalysis: (id: string, projectId?: string) =>
    apiFetch<EvidenceAnalysis>(
      `/api/v1/incidents/${encodeURIComponent(id)}/evidence-analysis${scopeQuery(projectId)}`
    ),

  getAnalysisExplanation: (id: string, analysisId: string, projectId?: string) =>
    apiFetch<AnalysisExplanation>(
      `/api/v1/incidents/${encodeURIComponent(id)}/causal-analysis/` +
        `${encodeURIComponent(analysisId)}/explanation${scopeQuery(projectId)}`
    ),

  explainRelationship: (id: string, relationshipId: string, projectId?: string) =>
    apiFetch<RelationshipExplanation>(
      `/api/v1/incidents/${encodeURIComponent(id)}/relationships/` +
        `${encodeURIComponent(relationshipId)}/explanation${scopeQuery(projectId)}`
    ),

  // --- Failure reproduction (Phase 5) ------------------------------------
  /**
   * Plan an experiment for an incident's hypothesis. **Never executes it.**
   *
   * Planning and execution are separate calls because a plan is something an
   * engineer reviews, and §47 requires explicit confirmation before anything runs.
   */
  createReproduction: (
    incidentId: string,
    {
      payload = {},
      projectId,
      environmentId,
    }: {
      payload?: CreateReproductionPayload;
      projectId?: string;
      environmentId?: string;
    } = {}
  ) => {
    const params = new URLSearchParams();
    if (projectId) params.set('project_id', projectId);
    if (environmentId) params.set('environment_id', environmentId);
    const qs = params.toString();
    return apiFetch<ReproductionExperimentDetail>(
      `/api/v1/incidents/${encodeURIComponent(incidentId)}/reproductions${
        qs ? `?${qs}` : ''
      }`,
      { method: 'POST', body: JSON.stringify(payload) }
    );
  },

  listIncidentReproductions: (incidentId: string, projectId?: string) =>
    apiFetch<ReproductionHistory>(
      `/api/v1/incidents/${encodeURIComponent(incidentId)}/reproductions` +
        scopeQuery(projectId)
    ),

  listReproductions: ({
    projectId,
    incidentId,
    status,
    page = 1,
    pageSize = 20,
  }: {
    projectId: string;
    incidentId?: string;
    status?: ExperimentStatus | string;
    page?: number;
    pageSize?: number;
  }) => {
    const params = new URLSearchParams({
      project_id: projectId,
      page: String(page),
      page_size: String(pageSize),
    });
    if (incidentId) params.set('incident_id', incidentId);
    if (status) params.set('status', String(status));
    return apiFetch<PaginatedResponse<ReproductionExperiment>>(
      `/api/v1/reproductions?${params.toString()}`
    );
  },

  getReproduction: (id: string, projectId?: string) =>
    apiFetch<ReproductionExperimentDetail>(
      `/api/v1/reproductions/${encodeURIComponent(id)}${scopeQuery(projectId)}`
    ),

  getReproductionPlan: (id: string, projectId?: string) =>
    apiFetch<ReproductionPlan>(
      `/api/v1/reproductions/${encodeURIComponent(id)}/plan${scopeQuery(projectId)}`
    ),

  /** Everything that must be shown before an experiment may run (§47). */
  getReproductionSafety: (id: string, projectId?: string) =>
    apiFetch<ReproductionSafetyPreview>(
      `/api/v1/reproductions/${encodeURIComponent(id)}/safety${scopeQuery(projectId)}`
    ),

  getReproductionStatus: (id: string, projectId?: string) =>
    apiFetch<ReproductionStatus>(
      `/api/v1/reproductions/${encodeURIComponent(id)}/status${scopeQuery(projectId)}`
    ),

  getReproductionInputs: (id: string, projectId?: string) =>
    apiFetch<ReproductionInput[]>(
      `/api/v1/reproductions/${encodeURIComponent(id)}/inputs${scopeQuery(projectId)}`
    ),

  getReproductionTelemetry: (id: string, projectId?: string) =>
    apiFetch<ReproductionTelemetry>(
      `/api/v1/reproductions/${encodeURIComponent(id)}/telemetry${scopeQuery(
        projectId
      )}`
    ),

  getReproductionArtifacts: (
    id: string,
    projectId?: string
  ): Promise<{ experiment_id: string; items: ReproductionArtifact[]; total: number }> =>
    apiFetch<{
      experiment_id: string;
      items: ReproductionArtifact[];
      total: number;
    }>(
      `/api/v1/reproductions/${encodeURIComponent(id)}/artifacts${scopeQuery(
        projectId
      )}`
    ),

  getReproductionComparison: (id: string, projectId?: string) =>
    apiFetch<ReproductionComparisonList>(
      `/api/v1/reproductions/${encodeURIComponent(id)}/comparison${scopeQuery(
        projectId
      )}`
    ),

  getReproductionValidation: (id: string, projectId?: string) =>
    apiFetch<ReproductionValidation>(
      `/api/v1/reproductions/${encodeURIComponent(id)}/validation${scopeQuery(
        projectId
      )}`
    ),

  getReproductionEnvironment: (id: string, projectId?: string) =>
    apiFetch<ReproductionEnvironmentSnapshot[]>(
      `/api/v1/reproductions/${encodeURIComponent(id)}/environment${scopeQuery(
        projectId
      )}`
    ),

  getReproductionFaults: (
    id: string,
    projectId?: string
  ): Promise<{
    experiment_id: string;
    items: ReproductionFault[];
    total: number;
    injected_total: number;
  }> =>
    apiFetch<{
      experiment_id: string;
      items: ReproductionFault[];
      total: number;
      injected_total: number;
    }>(
      `/api/v1/reproductions/${encodeURIComponent(id)}/faults${scopeQuery(projectId)}`
    ),

  getReproductionManifest: (id: string, projectId?: string) =>
    apiFetch<ReproductionManifest>(
      `/api/v1/reproductions/${encodeURIComponent(id)}/manifest${scopeQuery(
        projectId
      )}`
    ),

  /**
   * Execute a planned experiment. The backend *requires* a project scope here:
   * knowing an id is not authority to run it.
   */
  startReproduction: (id: string, projectId: string, requestedBy?: string) =>
    apiFetch<ReproductionExperimentDetail>(
      `/api/v1/reproductions/${encodeURIComponent(id)}/start` +
        `?project_id=${encodeURIComponent(projectId)}`,
      {
        method: 'POST',
        body: JSON.stringify({
          confirm_sandbox: true,
          requested_by: requestedBy ?? null,
        }),
      }
    ),

  cancelReproduction: (id: string, projectId: string, reason?: string) =>
    apiFetch<ReproductionExperimentDetail>(
      `/api/v1/reproductions/${encodeURIComponent(id)}/cancel` +
        `?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify({ reason: reason ?? null }) }
    ),

  /** Plan a *fresh* version of the same hypothesis; the first attempt is kept. */
  retryReproduction: (
    id: string,
    projectId: string,
    payload: CreateReproductionPayload = {}
  ) =>
    apiFetch<ReproductionExperimentDetail>(
      `/api/v1/reproductions/${encodeURIComponent(id)}/retry` +
        `?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  reproductionMetrics: (projectId: string) =>
    apiFetch<ReproductionMetrics>(
      `/api/v1/reproductions/metrics?project_id=${encodeURIComponent(projectId)}`
    ),

  // -----------------------------------------------------------------------
  // Phase 6 — Code Intelligence & AI Debugger
  // -----------------------------------------------------------------------

  listRepositories: (projectId: string) =>
    apiFetch<RepositoryList>(
      `/api/v1/projects/${encodeURIComponent(projectId)}/repositories`
    ),

  registerRepository: (
    projectId: string,
    payload: RegisterRepositoryPayload
  ) =>
    apiFetch<Repository>(
      `/api/v1/projects/${encodeURIComponent(projectId)}/repositories`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  indexRepository: (
    projectId: string,
    repositoryId: string,
    payload: IndexRepositoryPayload = {}
  ) =>
    apiFetch<IndexResult>(
      `/api/v1/projects/${encodeURIComponent(
        projectId
      )}/repositories/${encodeURIComponent(repositoryId)}/index`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  repositoryHistory: (
    projectId: string,
    repositoryId: string,
    options: { reference?: string; path?: string; limit?: number } = {}
  ) => {
    const params = new URLSearchParams({ project_id: projectId });
    if (options.reference) {
      params.set('reference', options.reference);
    }
    if (options.path) {
      params.set('path', options.path);
    }
    if (options.limit) {
      params.set('limit', String(options.limit));
    }
    return apiFetch<HistoryResult>(
      `/api/v1/projects/${encodeURIComponent(
        projectId
      )}/repositories/${encodeURIComponent(repositoryId)}/history?${params}`
    );
  },

  listSnapshots: (projectId: string, repositoryId: string) =>
    apiFetch<SnapshotList>(
      `/api/v1/projects/${encodeURIComponent(
        projectId
      )}/repositories/${encodeURIComponent(repositoryId)}/snapshots`
    ),

  getSnapshotSummary: (snapshotId: string, projectId?: string) =>
    apiFetch<SnapshotSummary>(
      `/api/v1/snapshots/${encodeURIComponent(snapshotId)}${scopeQuery(projectId)}`
    ),

  getSnapshotFiles: (snapshotId: string, projectId?: string) =>
    apiFetch<CodeFileList>(
      `/api/v1/snapshots/${encodeURIComponent(
        snapshotId
      )}/files${scopeQuery(projectId)}`
    ),

  searchSnapshotCode: (
    snapshotId: string,
    query: string,
    projectId?: string
  ) =>
    apiFetch<CodeSearchResult>(
      `/api/v1/snapshots/${encodeURIComponent(snapshotId)}/search${
        scopeQuery(projectId) || '?'
      }query=${encodeURIComponent(query)}`
    ),

  getSymbolDetail: (snapshotId: string, symbolId: string, projectId?: string) =>
    apiFetch<SymbolDetail>(
      `/api/v1/snapshots/${encodeURIComponent(
        snapshotId
      )}/symbols/${encodeURIComponent(symbolId)}${scopeQuery(projectId)}`
    ),

  getIncidentCodeMappings: (incidentId: string, projectId?: string) =>
    apiFetch<TraceMappingList>(
      `/api/v1/incidents/${encodeURIComponent(
        incidentId
      )}/code-mappings${scopeQuery(projectId)}`
    ),

  createDebugSession: (
    incidentId: string,
    projectId: string,
    payload: CreateDebugSessionPayload = {}
  ) =>
    apiFetch<DebugSessionDetail>(
      `/api/v1/incidents/${encodeURIComponent(
        incidentId
      )}/debug-sessions?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  listDebugSessions: (incidentId: string, projectId?: string) =>
    apiFetch<DebugSessionList>(
      `/api/v1/incidents/${encodeURIComponent(
        incidentId
      )}/debug-sessions${scopeQuery(projectId)}`
    ),

  getDebugSession: (sessionId: string, projectId?: string) =>
    apiFetch<DebugSessionDetail>(
      `/api/v1/debug-sessions/${encodeURIComponent(sessionId)}${scopeQuery(
        projectId
      )}`
    ),

  analyzeDebugSession: (sessionId: string, projectId?: string) =>
    apiFetch<DebugAnalysis>(
      `/api/v1/debug-sessions/${encodeURIComponent(
        sessionId
      )}/analyze${scopeQuery(projectId)}`,
      { method: 'POST' }
    ),

  getDebugSessionMessages: (sessionId: string, projectId?: string) =>
    apiFetch<DebugMessage[]>(
      `/api/v1/debug-sessions/${encodeURIComponent(
        sessionId
      )}/messages${scopeQuery(projectId)}`
    ),

  askDebugSession: (
    sessionId: string,
    payload: AskDebugPayload,
    projectId?: string
  ) =>
    apiFetch<DebugAssistantAnswer>(
      `/api/v1/debug-sessions/${encodeURIComponent(
        sessionId
      )}/messages${scopeQuery(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  getDebugSessionTools: (sessionId: string, projectId?: string) =>
    apiFetch<DebugToolCall[]>(
      `/api/v1/debug-sessions/${encodeURIComponent(
        sessionId
      )}/tools${scopeQuery(projectId)}`
    ),

  getDebugTimeline: (sessionId: string, projectId?: string) =>
    apiFetch<DebugTimeline>(
      `/api/v1/debug-sessions/${encodeURIComponent(
        sessionId
      )}/timeline${scopeQuery(projectId)}`
    ),

  debuggerMetrics: (projectId?: string) =>
    apiFetch<DebuggerMetrics>(
      `/api/v1/debugger/metrics${scopeQuery(projectId)}`
    ),

  // -- Phase 7 — fix generation & verification (§65) ----------------------

  createFixHypothesis: (
    incidentId: string,
    projectId: string,
    payload: CreateFixHypothesisPayload
  ) =>
    apiFetch<FixHypothesis>(
      `/api/v1/incidents/${encodeURIComponent(
        incidentId
      )}/fixes?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  listIncidentFixes: (incidentId: string, projectId?: string) =>
    apiFetch<FixHypothesisList>(
      `/api/v1/incidents/${encodeURIComponent(
        incidentId
      )}/fixes${scopeQuery(projectId)}`
    ),

  listFixes: (options: {
    projectId: string;
    incidentId?: string;
    status?: string;
  }) => {
    const params = new URLSearchParams({ project_id: options.projectId });
    if (options.incidentId) {
      params.set('incident_id', options.incidentId);
    }
    if (options.status) {
      params.set('status', options.status);
    }
    return apiFetch<FixHypothesisList>(`/api/v1/fixes?${params}`);
  },

  getFixHypothesis: (hypothesisId: string, projectId?: string) =>
    apiFetch<FixHypothesis>(
      `/api/v1/fixes/${encodeURIComponent(
        hypothesisId
      )}${scopeQuery(projectId)}`
    ),

  generatePatch: (
    hypothesisId: string,
    projectId: string,
    payload: GeneratePatchPayload = {}
  ) =>
    apiFetch<Patch>(
      `/api/v1/fixes/${encodeURIComponent(
        hypothesisId
      )}/generate?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  listHypothesisPatches: (hypothesisId: string, projectId?: string) =>
    apiFetch<PatchList>(
      `/api/v1/fixes/${encodeURIComponent(
        hypothesisId
      )}/patches${scopeQuery(projectId)}`
    ),

  listPatches: (options: { projectId: string; status?: string }) => {
    const params = new URLSearchParams({ project_id: options.projectId });
    if (options.status) {
      params.set('status', options.status);
    }
    return apiFetch<PatchList>(`/api/v1/patches?${params}`);
  },

  getPatch: (patchId: string, projectId?: string) =>
    apiFetch<PatchDetail>(
      `/api/v1/patches/${encodeURIComponent(patchId)}${scopeQuery(projectId)}`
    ),

  getPatchDiff: async (patchId: string, projectId?: string) => {
    const detail = await apiFetch<PatchDetail>(
      `/api/v1/patches/${encodeURIComponent(patchId)}${scopeQuery(projectId)}`
    );
    return detail.patch_content;
  },

  verifyPatch: (
    patchId: string,
    projectId: string,
    payload: VerifyPatchPayload
  ) =>
    apiFetch<PatchVerificationRun>(
      `/api/v1/patches/${encodeURIComponent(
        patchId
      )}/verify?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  getLatestVerification: (patchId: string, projectId?: string) =>
    apiFetch<PatchVerificationRun>(
      `/api/v1/patches/${encodeURIComponent(
        patchId
      )}/verification${scopeQuery(projectId)}`
    ),

  getPatchComparison: (patchId: string, projectId?: string) =>
    apiFetch<Record<string, unknown>>(
      `/api/v1/patches/${encodeURIComponent(
        patchId
      )}/comparison${scopeQuery(projectId)}`
    ),

  getPatchArtifacts: (patchId: string, projectId?: string) =>
    apiFetch<Record<string, unknown>>(
      `/api/v1/patches/${encodeURIComponent(
        patchId
      )}/artifacts${scopeQuery(projectId)}`
    ),

  /** §41, §71 — records a decision. Nothing merges or deploys. */
  approvePatch: (
    patchId: string,
    projectId: string,
    payload: ReviewPatchPayload = {}
  ) =>
    apiFetch<PatchReviewAction>(
      `/api/v1/patches/${encodeURIComponent(
        patchId
      )}/approve?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify({ ...payload, action: 'APPROVE' }) }
    ),

  rejectPatch: (patchId: string, projectId: string, payload: ReviewPatchPayload = {}) =>
    apiFetch<PatchReviewAction>(
      `/api/v1/patches/${encodeURIComponent(
        patchId
      )}/reject?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify({ ...payload, action: 'REJECT' }) }
    ),

  regeneratePatch: (
    patchId: string,
    projectId: string,
    payload: ReviewPatchPayload = {}
  ) =>
    apiFetch<Patch>(
      `/api/v1/patches/${encodeURIComponent(
        patchId
      )}/regenerate?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify({ ...payload, action: 'REGENERATE' }) }
    ),

  getVerificationReport: (patchId: string, projectId?: string) =>
    apiFetch<Record<string, unknown>>(
      `/api/v1/patches/${encodeURIComponent(
        patchId
      )}/report${scopeQuery(projectId)}`
    ),

  fixMetrics: (projectId: string) =>
    apiFetch<FixMetrics>(
      `/api/v1/fixes/metrics?project_id=${encodeURIComponent(projectId)}`
    ),

  // -- Phase 8 — predictive reliability (§44) ------------------------------

  generateForecasts: (
    projectId: string,
    payload: GenerateForecastsPayload = {}
  ) =>
    apiFetch<ForecastGenerateResult>(
      `/api/v1/reliability/forecasts/generate?project_id=${encodeURIComponent(
        projectId
      )}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  listForecasts: (params: Record<string, string | number | boolean | undefined>) =>
    apiFetch<ForecastList>(`/api/v1/reliability/forecasts?${toQuery(params)}`),

  getForecast: (forecastId: string, projectId?: string) =>
    apiFetch<Forecast>(
      `/api/v1/reliability/forecasts/${encodeURIComponent(
        forecastId
      )}${scopeQuery(projectId)}`
    ),

  getForecastExplanation: (forecastId: string, projectId?: string) =>
    apiFetch<ForecastExplanation>(
      `/api/v1/reliability/forecasts/${encodeURIComponent(
        forecastId
      )}/explanation${scopeQuery(projectId)}`
    ),

  getForecastSignals: (forecastId: string, projectId?: string) =>
    apiFetch<ForecastSignal[]>(
      `/api/v1/reliability/forecasts/${encodeURIComponent(
        forecastId
      )}/signals${scopeQuery(projectId)}`
    ),

  getForecastSnapshot: (forecastId: string, projectId?: string) =>
    apiFetch<FeatureSnapshot>(
      `/api/v1/reliability/forecasts/${encodeURIComponent(
        forecastId
      )}/snapshot${scopeQuery(projectId)}`
    ),

  getForecastOutcome: (forecastId: string, projectId?: string) =>
    apiFetch<ForecastOutcome | null>(
      `/api/v1/reliability/forecasts/${encodeURIComponent(
        forecastId
      )}/outcome${scopeQuery(projectId)}`
    ),

  getRiskHeatmap: (params: Record<string, string | boolean | undefined>) =>
    apiFetch<RiskHeatmap>(`/api/v1/reliability/heatmap?${toQuery(params)}`),

  getComponentProfile: (componentId: string, projectId: string) =>
    apiFetch<ComponentProfile>(
      `/api/v1/reliability/components/${encodeURIComponent(
        componentId
      )}/profile?project_id=${encodeURIComponent(projectId)}`
    ),

  listReliabilitySignals: (
    params: Record<string, string | number | undefined>
  ) => apiFetch<ForecastSignal[]>(`/api/v1/reliability/signals?${toQuery(params)}`),

  listModelVersions: (limit = 50) =>
    apiFetch<ModelVersionList>(`/api/v1/reliability/models?limit=${limit}`),

  getModelVersion: (modelId: string) =>
    apiFetch<ModelVersion>(
      `/api/v1/reliability/models/${encodeURIComponent(modelId)}`
    ),

  listEvaluationRuns: (params: Record<string, string | number | undefined>) =>
    apiFetch<EvaluationRunList>(
      `/api/v1/reliability/evaluations?${toQuery(params)}`
    ),

  runEvaluation: (projectId: string, dispatch = false) =>
    apiFetch<EvaluationRun>(
      `/api/v1/reliability/evaluate?project_id=${encodeURIComponent(
        projectId
      )}&dispatch=${dispatch}`,
      { method: 'POST' }
    ),

  runBacktest: (projectId: string, payload: BacktestPayload) =>
    apiFetch<BacktestRunResult>(
      `/api/v1/reliability/backtests?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  listBacktests: (projectId: string, limit = 50) =>
    apiFetch<BacktestList>(
      `/api/v1/reliability/backtests?project_id=${encodeURIComponent(
        projectId
      )}&limit=${limit}`
    ),

  getBacktest: (backtestId: string, projectId: string) =>
    apiFetch<Backtest>(
      `/api/v1/reliability/backtests/${encodeURIComponent(
        backtestId
      )}?project_id=${encodeURIComponent(projectId)}`
    ),

  reliabilityHealth: (projectId?: string) =>
    apiFetch<PlatformHealth>(
      `/api/v1/reliability/health${scopeQuery(projectId)}`
    ),

  listDriftFindings: (projectId: string, limit = 100) =>
    apiFetch<DriftHistory>(
      `/api/v1/reliability/drift?project_id=${encodeURIComponent(
        projectId
      )}&limit=${limit}`
    ),

  assessDrift: (projectId: string, persist = true) =>
    apiFetch<DriftReport>(
      `/api/v1/reliability/drift/assess?project_id=${encodeURIComponent(
        projectId
      )}&persist=${persist}`,
      { method: 'POST' }
    ),

  listEarlyWarnings: (params: Record<string, string | number | undefined>) =>
    apiFetch<EarlyWarningList>(
      `/api/v1/reliability/warnings?${toQuery(params)}`
    ),

  acknowledgeWarning: (
    warningId: string,
    projectId: string,
    payload: WarningActionPayload = {}
  ) =>
    apiFetch<EarlyWarning>(
      `/api/v1/reliability/warnings/${encodeURIComponent(
        warningId
      )}/acknowledge?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  dismissWarning: (
    warningId: string,
    projectId: string,
    payload: WarningActionPayload = {}
  ) =>
    apiFetch<EarlyWarning>(
      `/api/v1/reliability/warnings/${encodeURIComponent(
        warningId
      )}/dismiss?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  // -- Phase 9 — safe autonomous remediation (§44, §47, §48) ---------------

  listRemediationActionTypes: () =>
    apiFetch<RemediationActionTypeList>('/api/v1/remediation/action-types'),

  getRemediationPolicy: (projectId: string, environmentId?: string) =>
    apiFetch<RemediationPolicy>(
      `/api/v1/remediation/policy?project_id=${encodeURIComponent(
        projectId
      )}${environmentId ? `&environment_id=${encodeURIComponent(environmentId)}` : ''}`
    ),

  updateRemediationPolicy: (
    projectId: string,
    payload: RemediationPolicyUpdate
  ) =>
    apiFetch<RemediationPolicy>(
      `/api/v1/remediation/policy?project_id=${encodeURIComponent(projectId)}`,
      { method: 'PUT', body: JSON.stringify(payload) }
    ),

  setEmergencyStop: (
    projectId: string,
    payload: { engage: boolean; actor: string; reason?: string }
  ) =>
    apiFetch<RemediationPolicy>(
      `/api/v1/remediation/emergency-stop?project_id=${encodeURIComponent(
        projectId
      )}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  listRemediationControls: (projectId: string, environmentId?: string) =>
    apiFetch<RemediationControlList>(
      `/api/v1/remediation/controls?project_id=${encodeURIComponent(
        projectId
      )}${environmentId ? `&environment_id=${encodeURIComponent(environmentId)}` : ''}`
    ),

  listRemediationBreakers: (projectId: string) =>
    apiFetch<RemediationBreakerList>(
      `/api/v1/remediation/breakers?project_id=${encodeURIComponent(projectId)}`
    ),

  planRemediations: (payload: RemediationPlanRequest) =>
    apiFetch<RemediationPlanResult>('/api/v1/remediation/actions/plan', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  proposeRemediation: (payload: RemediationProposeRequest) =>
    apiFetch<RemediationAction>('/api/v1/remediation/actions/propose', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  listRemediationActions: (params: Record<string, string | number | undefined>) =>
    apiFetch<RemediationActionList>(
      `/api/v1/remediation/actions?${toQuery(params)}`
    ),

  getRemediationAction: (actionId: string, projectId?: string) =>
    apiFetch<RemediationActionDetail>(
      `/api/v1/remediation/actions/${encodeURIComponent(
        actionId
      )}${scopeQuery(projectId)}`
    ),

  incidentRemediationActions: (incidentId: string, projectId?: string) =>
    apiFetch<RemediationActionList>(
      `/api/v1/remediation/incidents/${encodeURIComponent(
        incidentId
      )}/actions${scopeQuery(projectId)}`
    ),

  getRemediationAudit: (actionId: string, projectId?: string) =>
    apiFetch<RemediationAuditEvent[]>(
      `/api/v1/remediation/actions/${encodeURIComponent(
        actionId
      )}/audit${scopeQuery(projectId)}`
    ),

  verifyRemediationAudit: (actionId: string, projectId?: string) =>
    apiFetch<RemediationAuditChain>(
      `/api/v1/remediation/actions/${encodeURIComponent(
        actionId
      )}/audit/verify${scopeQuery(projectId)}`
    ),

  assessRemediationAction: (actionId: string, projectId: string) =>
    apiFetch<RemediationAction>(
      `/api/v1/remediation/actions/${encodeURIComponent(
        actionId
      )}/assess?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST' }
    ),

  evaluateRemediationPolicy: (actionId: string, projectId: string) =>
    apiFetch<RemediationAction>(
      `/api/v1/remediation/actions/${encodeURIComponent(
        actionId
      )}/evaluate?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST' }
    ),

  verifyRemediationAction: (actionId: string, projectId: string) =>
    apiFetch<RemediationAction>(
      `/api/v1/remediation/actions/${encodeURIComponent(
        actionId
      )}/verify?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST' }
    ),

  approveRemediationAction: (
    actionId: string,
    payload: RemediationDecisionRequest,
    projectId: string
  ) =>
    apiFetch<RemediationAction>(
      `/api/v1/remediation/actions/${encodeURIComponent(
        actionId
      )}/approve?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  rejectRemediationAction: (
    actionId: string,
    payload: RemediationDecisionRequest,
    projectId: string
  ) =>
    apiFetch<RemediationAction>(
      `/api/v1/remediation/actions/${encodeURIComponent(
        actionId
      )}/reject?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  executeRemediationAction: (
    actionId: string,
    projectId: string,
    payload: { actor?: string; dry_run?: boolean; async_execution?: boolean } = {}
  ) =>
    apiFetch<RemediationRunResult>(
      `/api/v1/remediation/actions/${encodeURIComponent(
        actionId
      )}/execute?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  runRemediationAction: (actionId: string, projectId: string) =>
    apiFetch<RemediationRunResult>(
      `/api/v1/remediation/actions/${encodeURIComponent(
        actionId
      )}/run?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST' }
    ),

  rollbackRemediationAction: (
    actionId: string,
    projectId: string,
    payload: { actor: string; reason?: string }
  ) =>
    apiFetch<RemediationRunResult>(
      `/api/v1/remediation/actions/${encodeURIComponent(
        actionId
      )}/rollback?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  cancelRemediationAction: (
    actionId: string,
    projectId: string,
    payload: { actor: string; reason?: string }
  ) =>
    apiFetch<RemediationAction>(
      `/api/v1/remediation/actions/${encodeURIComponent(
        actionId
      )}/cancel?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  recordRemediationExecution: (
    actionId: string,
    projectId: string,
    payload: { actor: string; note: string; outcome_expected?: string }
  ) =>
    apiFetch<RemediationAction>(
      `/api/v1/remediation/actions/${encodeURIComponent(
        actionId
      )}/record-execution?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  runRemediationSweep: (payload: { project_id?: string; plan?: boolean } = {}) =>
    apiFetch<Record<string, unknown>>('/api/v1/remediation/sweep', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  remediationMetrics: (projectId?: string) =>
    apiFetch<RemediationMetrics>(
      `/api/v1/remediation/metrics${scopeQuery(projectId)}`
    ),

  // -- Phase 10 — reliability intelligence (§51–§61) ------------------------

  intelligenceHealth: (projectId?: string) =>
    apiFetch<IntelligenceHealth>(
      `/api/v1/intelligence/health${scopeQuery(projectId)}`
    ),

  intelligenceDashboard: (projectId: string) =>
    apiFetch<IntelligenceDashboard>(
      `/api/v1/intelligence/dashboard?project_id=${encodeURIComponent(projectId)}`
    ),

  intelligenceMetrics: (projectId?: string) =>
    apiFetch<IntelligenceMetrics>(
      `/api/v1/intelligence/metrics${scopeQuery(projectId)}`
    ),

  listKnowledge: (params: Record<string, string | number | undefined>) =>
    apiFetch<KnowledgeList>(
      `/api/v1/intelligence/knowledge?${toQuery(params)}`
    ),

  getKnowledge: (knowledgeId: string, projectId: string) =>
    apiFetch<KnowledgeDetail>(
      `/api/v1/intelligence/knowledge/${encodeURIComponent(
        knowledgeId
      )}?project_id=${encodeURIComponent(projectId)}`
    ),

  getKnowledgeVersions: (knowledgeId: string, projectId: string) =>
    apiFetch<KnowledgeVersionList>(
      `/api/v1/intelligence/knowledge/${encodeURIComponent(
        knowledgeId
      )}/versions?project_id=${encodeURIComponent(projectId)}`
    ),

  reviewKnowledge: (
    knowledgeId: string,
    projectId: string,
    payload: KnowledgeReviewPayload
  ) =>
    apiFetch<KnowledgeDetail>(
      `/api/v1/intelligence/knowledge/${encodeURIComponent(
        knowledgeId
      )}/review?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  listPatterns: (params: Record<string, string | number | boolean | undefined>) =>
    apiFetch<KnowledgeList>(`/api/v1/intelligence/patterns?${toQuery(params)}`),

  listExperiences: (params: Record<string, string | number | undefined>) =>
    apiFetch<ExperienceList>(
      `/api/v1/intelligence/experiences?${toQuery(params)}`
    ),

  getExperience: (experienceId: string, projectId: string) =>
    apiFetch<ExperienceDetail>(
      `/api/v1/intelligence/experiences/${encodeURIComponent(
        experienceId
      )}?project_id=${encodeURIComponent(projectId)}`
    ),

  listRecommendations: (
    params: Record<string, string | number | undefined>
  ) =>
    apiFetch<RecommendationList>(
      `/api/v1/intelligence/recommendations?${toQuery(params)}`
    ),

  getRecommendation: (recommendationId: string, projectId: string) =>
    apiFetch<RecommendationDetail>(
      `/api/v1/intelligence/recommendations/${encodeURIComponent(
        recommendationId
      )}?project_id=${encodeURIComponent(projectId)}`
    ),

  incidentRecommendations: (
    incidentId: string,
    projectId: string,
    generate = false
  ) =>
    apiFetch<RecommendationList>(
      `/api/v1/intelligence/incidents/${encodeURIComponent(
        incidentId
      )}/recommendations?project_id=${encodeURIComponent(
        projectId
      )}&generate=${generate ? 'true' : 'false'}`
    ),

  decideRecommendation: (
    recommendationId: string,
    projectId: string,
    payload: RecommendationDecisionPayload
  ) =>
    apiFetch<RecommendationDetail>(
      `/api/v1/intelligence/recommendations/${encodeURIComponent(
        recommendationId
      )}/decide?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  recordRecommendationOutcome: (
    recommendationId: string,
    projectId: string,
    payload: RecommendationOutcomePayload
  ) =>
    apiFetch<RecommendationDetail>(
      `/api/v1/intelligence/recommendations/${encodeURIComponent(
        recommendationId
      )}/outcome?project_id=${encodeURIComponent(projectId)}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  componentLearningProfile: (componentId: string, projectId: string) =>
    apiFetch<ComponentLearningProfile>(
      `/api/v1/intelligence/components/${encodeURIComponent(
        componentId
      )}/profile?project_id=${encodeURIComponent(projectId)}`
    ),

  intelligenceRelationships: (
    params: Record<string, string | number | undefined>
  ) =>
    apiFetch<LearnedRelationshipList>(
      `/api/v1/intelligence/relationships?${toQuery(params)}`
    ),

  remediationEffectiveness: (
    params: Record<string, string | number | undefined>
  ) =>
    apiFetch<RemediationEffectiveness>(
      `/api/v1/intelligence/remediation-effectiveness?${toQuery(params)}`
    ),

  compareRemediationActions: (params: Record<string, string | undefined>) =>
    apiFetch<ActionComparison>(
      `/api/v1/intelligence/remediation-effectiveness/compare?${toQuery(params)}`
    ),

  searchKnowledge: (params: Record<string, string | number | undefined>) =>
    apiFetch<KnowledgeSearchAnswer>(
      `/api/v1/intelligence/search?${toQuery(params)}`
    ),

  listLearningRuns: (params: Record<string, string | number | undefined>) =>
    apiFetch<LearningRunList>(
      `/api/v1/intelligence/learning-runs?${toQuery(params)}`
    ),

  getLearningRun: (runId: string, projectId?: string) =>
    apiFetch<LearningRunDetail>(
      `/api/v1/intelligence/learning-runs/${encodeURIComponent(runId)}${scopeQuery(
        projectId
      )}`
    ),

  triggerLearningRun: (payload: LearningRunRequestPayload) =>
    apiFetch<LearningRunSummary>('/api/v1/intelligence/learning-runs', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  getEventHooks: (projectId?: string) =>
    apiFetch<EventHook>(
      `/api/v1/intelligence/event-hooks${scopeQuery(projectId)}`
    ),

  updateEventHooks: (payload: EventHookUpdatePayload) =>
    apiFetch<EventHook>('/api/v1/intelligence/event-hooks', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  runIntelligenceSweep: (projectId?: string, force = false) =>
    apiFetch<IntelligenceSweep>(
      `/api/v1/intelligence/sweep${scopeQuery(projectId)}${
        projectId ? '&' : '?'
      }force=${force ? 'true' : 'false'}`,
      { method: 'POST' }
    ),

  // -------------------------------------------------------------------------
  // Phase 11 — unified reliability platform
  // -------------------------------------------------------------------------

  platformProjects: () => apiFetch<PlatformProjectCard[]>('/api/v1/platform/projects'),

  platformOverview: (projectId: string, environmentId?: string) =>
    apiFetch<OverviewResponse>(
      `/api/v1/platform/overview?${toQuery({
        project_id: projectId,
        environment_id: environmentId,
      })}`
    ),

  platformEngineering: (projectId: string) =>
    apiFetch<Record<string, unknown>>(
      `/api/v1/platform/engineering?${toQuery({ project_id: projectId })}`
    ),

  platformActivity: (
    projectId: string,
    options: { eventType?: string; limit?: number; offset?: number } = {}
  ) =>
    apiFetch<ActivityResponse>(
      `/api/v1/platform/activity?${toQuery({
        project_id: projectId,
        event_type: options.eventType,
        limit: options.limit,
        offset: options.offset,
      })}`
    ),

  platformStory: (correlationId: string) =>
    apiFetch<StoryResponse>(
      `/api/v1/platform/story/${encodeURIComponent(correlationId)}`
    ),

  platformState: (
    projectId: string,
    options: { environmentId?: string; include?: string } = {}
  ) =>
    apiFetch<SystemStateResponse>(
      `/api/v1/platform/state?${toQuery({
        project_id: projectId,
        environment_id: options.environmentId,
        include: options.include,
      })}`
    ),

  platformRecomputeState: (projectId: string, environmentId?: string) =>
    apiFetch<SystemStateResponse>(
      `/api/v1/platform/state/recompute?${toQuery({
        project_id: projectId,
        environment_id: environmentId,
      })}`,
      { method: 'POST' }
    ),

  platformComponentStateHistory: (
    componentId: string,
    projectId: string,
    days = 30
  ) =>
    apiFetch<StateHistoryResponse>(
      `/api/v1/platform/state/components/${encodeURIComponent(
        componentId
      )}/history?${toQuery({ project_id: projectId, days })}`
    ),

  platformContext: (
    projectId: string,
    options: { componentId?: string; incidentId?: string } = {}
  ) =>
    apiFetch<ContextResponse>(
      `/api/v1/platform/context?${toQuery({
        project_id: projectId,
        component_id: options.componentId,
        incident_id: options.incidentId,
      })}`
    ),

  platformSnapshotContext: (params: {
    projectId: string;
    incidentId?: string;
    componentId?: string;
    caseId?: string;
    includeState?: boolean;
    actor?: string;
  }) =>
    apiFetch<ContextSnapshotResponse>(
      `/api/v1/platform/context/snapshots?${toQuery({
        project_id: params.projectId,
        incident_id: params.incidentId,
        component_id: params.componentId,
        case_id: params.caseId,
        include_state: params.includeState,
        actor: params.actor,
      })}`,
      { method: 'POST' }
    ),

  platformCases: (
    projectId: string,
    options: {
      status?: string;
      environmentId?: string;
      limit?: number;
      offset?: number;
    } = {}
  ) =>
    apiFetch<CaseListResponse>(
      `/api/v1/platform/cases?${toQuery({
        project_id: projectId,
        status: options.status,
        environment_id: options.environmentId,
        limit: options.limit,
        offset: options.offset,
      })}`
    ),

  platformOpenCase: (params: {
    projectId: string;
    title: string;
    trigger?: string;
    componentId?: string;
    environmentId?: string;
    summary?: string;
    actor?: string;
  }) =>
    apiFetch<CaseSummary>(
      `/api/v1/platform/cases?${toQuery({
        project_id: params.projectId,
        title: params.title,
        trigger: params.trigger,
        component_id: params.componentId,
        environment_id: params.environmentId,
        summary: params.summary,
        actor: params.actor,
      })}`,
      { method: 'POST' }
    ),

  platformCase: (
    caseId: string,
    projectId: string,
    includeEvidence = false
  ) =>
    apiFetch<CaseDetailResponse>(
      `/api/v1/platform/cases/${encodeURIComponent(caseId)}?${toQuery({
        project_id: projectId,
        include_evidence: includeEvidence,
      })}`
    ),

  platformCaseStatus: (params: {
    caseId: string;
    projectId: string;
    status: string;
    reason?: string;
    actor?: string;
  }) =>
    apiFetch<CaseSummary>(
      `/api/v1/platform/cases/${encodeURIComponent(
        params.caseId
      )}/status?${toQuery({ project_id: params.projectId })}`,
      {
        method: 'POST',
        body: JSON.stringify({
          status: params.status,
          reason: params.reason,
          actor: params.actor,
        }),
      }
    ),

  platformCaseAsk: (params: {
    caseId: string;
    projectId: string;
    question: string;
    includeEvidence?: boolean;
  }) =>
    apiFetch<AssistantAnswerResponse>(
      `/api/v1/platform/cases/${encodeURIComponent(
        params.caseId
      )}/ask?${toQuery({ project_id: params.projectId })}`,
      {
        method: 'POST',
        body: JSON.stringify({
          question: params.question,
          include_evidence: params.includeEvidence ?? false,
        }),
      }
    ),

  platformCaseStory: (caseId: string, projectId: string) =>
    apiFetch<StoryResponse>(
      `/api/v1/platform/cases/${encodeURIComponent(caseId)}/story?${toQuery({
        project_id: projectId,
      })}`
    ),

  platformSearch: (params: {
    projectId: string;
    query: string;
    kind?: string;
    environmentId?: string;
    limit?: number;
  }) =>
    apiFetch<SearchResponse>(
      `/api/v1/platform/search?${toQuery({
        project_id: params.projectId,
        q: params.query,
        kind: params.kind,
        environment_id: params.environmentId,
        limit: params.limit,
      })}`
    ),

  platformSearchHelp: () =>
    apiFetch<SearchHelpResponse>('/api/v1/platform/search/help'),

  platformServices: (projectId: string, environmentId?: string) =>
    apiFetch<CatalogListResponse>(
      `/api/v1/platform/services?${toQuery({
        project_id: projectId,
        environment_id: environmentId,
      })}`
    ),

  platformService: (componentId: string, projectId: string, windowDays = 30) =>
    apiFetch<CatalogEntryResponse>(
      `/api/v1/platform/services/${encodeURIComponent(
        componentId
      )}?${toQuery({ project_id: projectId, window_days: windowDays })}`
    ),

  platformSetOwnership: (params: {
    componentId: string;
    projectId: string;
    team: string;
    ownerName?: string;
    contactEmail?: string;
    repositoryOwner?: string;
    onCall?: string;
    documentationUrl?: string;
    actor?: string;
  }) =>
    apiFetch<Record<string, unknown>>(
      `/api/v1/platform/services/${encodeURIComponent(
        params.componentId
      )}/ownership?${toQuery({ project_id: params.projectId })}`,
      {
        method: 'PUT',
        body: JSON.stringify({
          team: params.team,
          owner_name: params.ownerName,
          contact_email: params.contactEmail,
          repository_owner: params.repositoryOwner,
          on_call: params.onCall,
          documentation_url: params.documentationUrl,
          actor: params.actor,
        }),
      }
    ),

  platformBlastRadius: (componentId: string, projectId: string, maxHops = 2) =>
    apiFetch<Record<string, unknown>>(
      `/api/v1/platform/services/${encodeURIComponent(
        componentId
      )}/blast-radius?${toQuery({ project_id: projectId, max_hops: maxHops })}`
    ),

  platformSlo: (projectId: string) =>
    apiFetch<SloOverviewResponse>(
      `/api/v1/platform/slo?${toQuery({ project_id: projectId })}`
    ),

  platformCreateSlo: (projectId: string, payload: SloCreateRequest) =>
    apiFetch<Record<string, unknown>>(
      `/api/v1/platform/slo?${toQuery({ project_id: projectId })}`,
      {
        method: 'POST',
        body: JSON.stringify(payload),
      }
    ),

  platformEvaluateSlo: (projectId: string, sloId?: string) =>
    apiFetch<SloEvaluationResponse | SloEvaluationResponse[]>(
      `/api/v1/platform/slo/evaluate?${toQuery({
        project_id: projectId,
        slo_id: sloId,
      })}`,
      { method: 'POST' }
    ),

  platformSloDetail: (sloId: string, projectId: string) =>
    apiFetch<SloEvaluationResponse>(
      `/api/v1/platform/slo/${encodeURIComponent(sloId)}?${toQuery({
        project_id: projectId,
      })}`
    ),

  platformErrorBudget: (sloId: string, projectId: string, limit = 50) =>
    apiFetch<ErrorBudgetResponse>(
      `/api/v1/platform/slo/${encodeURIComponent(
        sloId
      )}/error-budget?${toQuery({ project_id: projectId, limit })}`
    ),

  platformChanges: (
    projectId: string,
    options: { environmentId?: string; days?: number; limit?: number } = {}
  ) =>
    apiFetch<ChangeListResponse>(
      `/api/v1/platform/changes?${toQuery({
        project_id: projectId,
        environment_id: options.environmentId,
        days: options.days,
        limit: options.limit,
      })}`
    ),

  platformChangeRisk: (projectId: string, deploymentId: string) =>
    apiFetch<Record<string, unknown>>(
      `/api/v1/platform/changes/risk?${toQuery({
        project_id: projectId,
        deployment_id: deploymentId,
      })}`
    ),

  platformChangeFailureRate: (projectId: string, days?: number) =>
    apiFetch<ChangeFailureRateResponse>(
      `/api/v1/platform/changes/failure-rate?${toQuery({
        project_id: projectId,
        days,
      })}`
    ),

  platformCompareEnvironments: (
    projectId: string,
    left: string,
    right: string
  ) =>
    apiFetch<EnvironmentComparisonResponse>(
      `/api/v1/platform/environments/compare?${toQuery({
        project_id: projectId,
        left,
        right,
      })}`
    ),

  platformHealth: () =>
    apiFetch<PlatformHealthResponse>('/api/v1/platform/health'),

  platformReadiness: () =>
    apiFetch<ReadinessResponse>('/api/v1/platform/readiness'),

  platformDependencies: () =>
    apiFetch<DependencyHealthResponse>('/api/v1/platform/dependencies'),

  platformLive: () => apiFetch<Record<string, unknown>>('/api/v1/platform/live'),

  platformDataQuality: (
    projectId: string,
    options: { status?: string; limit?: number } = {}
  ) =>
    apiFetch<DataQualityResponse>(
      `/api/v1/platform/data-quality?${toQuery({
        project_id: projectId,
        status: options.status,
        limit: options.limit,
      })}`
    ),

  platformDataQualityCheck: (projectId: string, persist = true) =>
    apiFetch<DataQualityCheckResponse>(
      `/api/v1/platform/data-quality/check?${toQuery({
        project_id: projectId,
        persist,
      })}`,
      { method: 'POST' }
    ),

  platformDataQualityStatus: (params: {
    issueId: string;
    projectId: string;
    status: string;
    actor?: string;
  }) =>
    apiFetch<Record<string, unknown>>(
      `/api/v1/platform/data-quality/${encodeURIComponent(
        params.issueId
      )}/status?${toQuery({ project_id: params.projectId })}`,
      {
        method: 'POST',
        body: JSON.stringify({ status: params.status, actor: params.actor }),
      }
    ),

  platformConfiguration: (projectId: string) =>
    apiFetch<ConfigurationResponse>(
      `/api/v1/platform/configuration?${toQuery({ project_id: projectId })}`
    ),

  platformWriteConfiguration: (
    projectId: string,
    payload: ConfigurationUpdateRequest
  ) =>
    apiFetch<Record<string, unknown>>(
      `/api/v1/platform/configuration?${toQuery({ project_id: projectId })}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  platformRollbackConfiguration: (
    projectId: string,
    payload: ConfigurationRollbackRequest
  ) =>
    apiFetch<Record<string, unknown>>(
      `/api/v1/platform/configuration/rollback?${toQuery({
        project_id: projectId,
      })}`,
      { method: 'POST', body: JSON.stringify(payload) }
    ),

  platformFeatureFlags: (projectId: string) =>
    apiFetch<FeatureFlagsResponse>(
      `/api/v1/platform/feature-flags?${toQuery({ project_id: projectId })}`
    ),

  platformNotifications: (
    projectId: string,
    options: { status?: string; limit?: number } = {}
  ) =>
    apiFetch<NotificationListResponse>(
      `/api/v1/platform/notifications?${toQuery({
        project_id: projectId,
        status: options.status,
        limit: options.limit,
      })}`
    ),

  platformReadNotification: (params: {
    notificationId: string;
    projectId: string;
    actor?: string;
    acknowledge?: boolean;
  }) =>
    apiFetch<Record<string, unknown>>(
      `/api/v1/platform/notifications/${encodeURIComponent(
        params.notificationId
      )}/read?${toQuery({ project_id: params.projectId })}`,
      {
        method: 'POST',
        body: JSON.stringify({
          actor: params.actor,
          acknowledge: params.acknowledge ?? false,
        }),
      }
    ),

  platformReport: (
    projectId: string,
    options: { kind?: string; days?: number; format?: string } = {}
  ) =>
    apiFetch<ReportResponse>(
      `/api/v1/platform/reports?${toQuery({
        project_id: projectId,
        kind: options.kind,
        days: options.days,
        format: options.format,
      })}`
    ),

  platformPostmortem: (
    incidentId: string,
    projectId: string,
    draftNarrative = false
  ) =>
    apiFetch<PostmortemResponse>(
      `/api/v1/platform/incidents/${encodeURIComponent(
        incidentId
      )}/postmortem?${toQuery({
        project_id: projectId,
        draft_narrative: draftNarrative,
      })}`
    ),

  platformImprovementPlan: (projectId: string, days = 30) =>
    apiFetch<ImprovementPlanResponse>(
      `/api/v1/platform/improvement-plan?${toQuery({
        project_id: projectId,
        days,
      })}`
    ),

  platformMetrics: (projectId: string, days = 30) =>
    apiFetch<PlatformMetricsResponse>(
      `/api/v1/platform/metrics?${toQuery({ project_id: projectId, days })}`
    ),

  platformIntegrations: () =>
    apiFetch<IntegrationRegistryResponse>('/api/v1/platform/integrations'),

  platformWebhookRequirements: () =>
    apiFetch<WebhookRequirementsResponse>(
      '/api/v1/platform/webhooks/requirements'
    ),

  platformSweep: (projectId?: string) =>
    apiFetch<Record<string, unknown>>(
      `/api/v1/platform/sweep${scopeQuery(projectId)}`,
      { method: 'POST' }
    ),

  // -------------------------------------------------------------------------
  // Authentication (hardening W1)
  // -------------------------------------------------------------------------

  /** Validate a token and learn the identity/role it carries. */
  whoami: (token?: string) =>
    apiFetch<WhoAmI>(
      '/api/v1/auth/whoami',
      token ? { headers: { Authorization: `Bearer ${token}` } } : undefined
    ),

  listTokens: () => apiFetch<TokenList>('/api/v1/auth/tokens'),

  createToken: (body: TokenCreate) =>
    apiFetch<TokenCreateResponse>('/api/v1/auth/tokens', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  revokeToken: (tokenId: string) =>
    apiFetch<TokenResponse>(`/api/v1/auth/tokens/${tokenId}`, {
      method: 'DELETE',
    }),

  // -------------------------------------------------------------------------
  // Single sign-on (hardening W2)
  // -------------------------------------------------------------------------

  /**
   * Is SSO configured here, and what is it called?
   *
   * Public on the backend — the sign-in page has to ask before anyone holds a
   * credential. It reveals the provider label, the callback the operator
   * registered and the policy switches, and no secret of any kind.
   */
  oidcConfig: () => apiFetch<OidcPublicConfig>('/api/v1/auth/oidc/config'),

  /**
   * Exchange the code the provider sent the browser for an ARGUS session.
   *
   * Called by the callback page, which is where the provider lands. The code
   * and state travel in the request body, and the session secret comes back in
   * the response body — so neither ever appears in a URL, a Referer header or
   * an access log.
   */
  oidcCallback: (body: OidcCallbackRequest) =>
    apiFetch<OidcLoginResponse>('/api/v1/auth/oidc/callback', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** People provisioned by the identity provider (ADMIN). */
  listOidcIdentities: (includeDisabled = true) =>
    apiFetch<OidcIdentityList>(
      `/api/v1/auth/oidc/identities?include_disabled=${includeDisabled}`
    ),

  /**
   * Disable a person and revoke every session they hold, in one step.
   *
   * Deactivating somebody at the provider is not sufficient: their ARGUS
   * session is a bearer token that never consults the provider again.
   */
  disableOidcIdentity: (identityId: string) =>
    apiFetch<OidcIdentityStateChange>(
      `/api/v1/auth/oidc/identities/${identityId}/disable`,
      { method: 'POST' }
    ),

  /** Allow a person to sign in again (their old sessions stay revoked). */
  enableOidcIdentity: (identityId: string) =>
    apiFetch<OidcIdentityStateChange>(
      `/api/v1/auth/oidc/identities/${identityId}/enable`,
      { method: 'POST' }
    ),
};

// ---------------------------------------------------------------------------
// Single sign-on types (hardening W2)
// ---------------------------------------------------------------------------

/** What the sign-in page needs about SSO, and nothing an attacker can use. */
export interface OidcPublicConfig {
  enabled: boolean;
  provider_name: string;
  login_path: string;
  redirect_uri: string;
  requires_verified_email: boolean;
  allowed_email_domains: string[];
}

export interface OidcCallbackRequest {
  code: string;
  state: string;
}

/** A minted SSO session. The token appears here and never again. */
export interface OidcLoginResponse {
  token: string;
  expires_at: string;
  token_id: string;
  role: string;
  display_name?: string | null;
  email?: string | null;
  provider: string;
  project_ids: string[];
  unrestricted: boolean;
  redirect_to: string;
  warning: string;
}

/** A person as ARGUS last saw them at login. */
export interface OidcIdentity {
  id: string;
  provider: string;
  subject: string;
  email?: string | null;
  display_name?: string | null;
  role: string;
  project_ids: string[];
  email_verified: boolean;
  first_login_at: string;
  last_login_at: string;
  login_count: number;
  last_login_ip?: string | null;
  disabled: boolean;
  disabled_at?: string | null;
  disabled_reason?: string | null;
  active_sessions: number;
}

export interface OidcIdentityList {
  items: OidcIdentity[];
  total: number;
}

export interface OidcIdentityStateChange {
  id: string;
  disabled: boolean;
  revoked_sessions: number;
}

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

// ---------------------------------------------------------------------------
// Phase 6 — Code Intelligence & AI Debugger (§6–§65)
// ---------------------------------------------------------------------------

export type RepositoryIndexStatus =
  | 'PENDING'
  | 'INDEXING'
  | 'INDEXED'
  | 'PARTIAL'
  | 'FAILED';

export type CodeVersionStatus = 'RESOLVED' | 'UNRESOLVED' | 'UNKNOWN';

export type SnapshotStatus = 'READY' | 'PARTIAL' | 'FAILED' | 'INDEXING';

export interface Repository {
  id: string;
  project_id: string;
  provider: string;
  repository_url: string;
  default_branch: string;
  connection_status: string;
  language?: string | null;
  framework?: string | null;
  index_status: RepositoryIndexStatus;
  last_indexed_at?: string | null;
  last_indexed_commit?: string | null;
  created_at: string;
  updated_at: string;
  latest_snapshot_id?: string | null;
  latest_commit_sha?: string | null;
  snapshot_count: number;
  capabilities: string[];
}

export interface RepositoryList {
  items: Repository[];
  total: number;
}

export interface CodeSnapshot {
  id: string;
  project_id: string;
  repository_id: string;
  commit_sha?: string | null;
  branch?: string | null;
  commit_at?: string | null;
  commit_message?: string | null;
  commit_author?: string | null;
  provider_name: string;
  version_status: CodeVersionStatus;
  version_evidence?: string | null;
  status: SnapshotStatus;
  indexed_at?: string | null;
  file_count: number;
  symbol_count: number;
  languages: string[];
  error?: string | null;
  created_at: string;
}

export interface SnapshotList {
  items: CodeSnapshot[];
  total: number;
}

export interface IndexRun {
  id: string;
  snapshot_id: string;
  repository_id: string;
  status: string;
  trigger?: string | null;
  incremental: boolean;
  base_commit_sha?: string | null;
  started_at: string;
  completed_at?: string | null;
  duration_ms?: number | null;
  files_seen: number;
  files_indexed: number;
  files_reused: number;
  files_added: number;
  files_modified: number;
  files_deleted: number;
  files_failed: number;
  symbols_indexed: number;
  references_indexed: number;
  relationships_indexed: number;
  files_heuristic: number;
  files_partial: number;
  errors: unknown[];
  snapshot?: CodeSnapshot | null;
}

export interface IndexResult {
  run: IndexRun;
  snapshot: CodeSnapshot;
  notes: string[];
}

export interface RegisterRepositoryPayload {
  provider?: 'local' | 'git';
  repository_url: string;
  default_branch?: string | null;
  language?: string | null;
  last_indexed_commit?: string | null;
}

export interface IndexRepositoryPayload {
  reference?: string | null;
  incremental?: boolean;
  max_files?: number | null;
}

export type CodeSymbolType =
  | 'FUNCTION'
  | 'CLASS'
  | 'METHOD'
  | 'MODULE'
  | 'VARIABLE'
  | 'CONSTANT'
  | 'INTERFACE'
  | 'ROUTE';

export interface CodeSymbol {
  id: string;
  snapshot_id: string;
  file_path: string;
  symbol_name: string;
  qualified_name: string;
  symbol_type: CodeSymbolType | string;
  language?: string | null;
  start_line: number;
  end_line: number;
  signature?: string | null;
  documentation?: string | null;
  is_async: boolean;
  complexity?: number | null;
  route?: string | null;
  http_method?: string | null;
  component_id?: string | null;
  reference: string;
  caller_count: number;
  callee_count: number;
}

export interface SymbolList {
  items: CodeSymbol[];
  total: number;
  truncated: boolean;
}

export interface SymbolDetail extends CodeSymbol {
  source?: string | null;
  callers: SymbolEdge[];
  callees: SymbolEdge[];
  related_files: string[];
}

export interface SymbolEdge {
  qualified_name: string;
  file_path: string;
  start_line: number;
  end_line: number;
  relationship: string;
  confidence: number;
  line: number;
  reference: string;
}

export interface CodeFile {
  id: string;
  path: string;
  language?: string | null;
  module_name?: string | null;
  size_bytes: number;
  line_count: number;
  is_test: boolean;
  parse_status: string;
  parse_error?: string | null;
  last_commit_sha?: string | null;
  last_modified_at?: string | null;
  last_author?: string | null;
  symbol_count: number;
}

export interface CodeFileList {
  items: CodeFile[];
  total: number;
  truncated: boolean;
}

export interface CodeSearchResult {
  snapshot_id: string;
  query: string;
  symbols: CodeSymbol[];
  source_matches: CodeSymbol[];
  references: Array<{
    name: string;
    file_path: string;
    line: number;
    kind: string;
    resolved: boolean;
    symbol_id?: string | null;
  }>;
  truncated: boolean;
}

export interface SnapshotSummary {
  snapshot: CodeSnapshot;
  files: number;
  symbols: number;
  relationships: number;
  references: number;
  tests: number;
  languages: Record<string, number>;
  framework?: string | null;
  signals_by_type: Record<string, number>;
  limitations: string[];
}

export interface TraceCodeMapping {
  id: string;
  snapshot_id?: string | null;
  component_id?: string | null;
  trace_id?: string | null;
  span_id?: string | null;
  operation?: string | null;
  service_name?: string | null;
  endpoint?: string | null;
  http_method?: string | null;
  mapping_kind: string;
  symbol_id?: string | null;
  file_path?: string | null;
  start_line?: number | null;
  end_line?: number | null;
  confidence: number;
  evidence?: string | null;
  unmapped_reason?: string | null;
  reference?: string | null;
}

export interface TraceMappingList {
  incident_id: string;
  snapshot_id?: string | null;
  items: TraceCodeMapping[];
  total: number;
  mapped: number;
  unmapped: number;
  unmapped_reasons: Record<string, number>;
}

export type LocationValidation =
  | 'VALID'
  | 'NOT_FOUND'
  | 'OUT_OF_SNAPSHOT'
  | 'LINE_OUT_OF_RANGE'
  | 'AMBIGUOUS'
  | 'STALE';

export interface DebugCodeLocation {
  id: string;
  file_path: string;
  symbol_name?: string | null;
  symbol_id?: string | null;
  start_line?: number | null;
  end_line?: number | null;
  label: string;
  reason: string;
  confidence: ConfidenceLevel;
  validation: LocationValidation;
  validation_detail?: string | null;
  evidence_refs: string[];
  reference: string;
  displayable: boolean;
}

export interface DebugEvidence {
  id: string;
  kind: string;
  polarity: string;
  reference: string;
  label?: string | null;
  source_table?: string | null;
  source_id?: string | null;
  quote?: string | null;
  start_line?: number | null;
  end_line?: number | null;
  component_id?: string | null;
  valid: boolean;
  validation_error?: string | null;
  strength: number;
  observed_at?: string | null;
}

export type HypothesisValidationStatus =
  | 'UNVERIFIED'
  | 'SUPPORTED'
  | 'PARTIALLY_SUPPORTED'
  | 'WEAKENED'
  | 'REFUTED'
  | 'INVALID_REFERENCE';

export interface DebugHypothesis {
  id: string;
  description: string;
  category: string;
  confidence: ConfidenceLevel;
  validation_status: HypothesisValidationStatus;
  rationale?: string | null;
  testable: boolean;
  test_approach?: string | null;
  recurrence_count: number;
  locations: DebugCodeLocation[];
  supporting_evidence: DebugEvidence[];
  contradicting_evidence: DebugEvidence[];
}

export type DebugSessionStatus =
  | 'CREATED'
  | 'CONTEXT_BUILDING'
  | 'ANALYZING'
  | 'WAITING_FOR_VALIDATION'
  | 'COMPLETED'
  | 'FAILED'
  | 'CANCELLED';

export type DebugAnalysisStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'COMPLETED'
  | 'DEGRADED'
  | 'FAILED'
  | 'CANCELLED'
  | 'LIMIT_REACHED';

export interface DebugSession {
  id: string;
  project_id: string;
  incident_id: string;
  repository_id?: string | null;
  snapshot_id?: string | null;
  title?: string | null;
  status: DebugSessionStatus;
  created_by?: string | null;
  version_status: CodeVersionStatus;
  version_note?: string | null;
  context_version: string;
  summary?: string | null;
  created_at: string;
  updated_at: string;
}

export interface DebugMessage {
  id: string;
  role: 'ENGINEER' | 'ARGUS' | 'SYSTEM' | string;
  content: string;
  created_by?: string | null;
  evidence_refs: string[];
  metadata?: Record<string, unknown> | null;
  created_at: string;
}

export interface DebugToolCall {
  id: string;
  tool_name: string;
  arguments?: Record<string, unknown> | null;
  status: string;
  result_summary?: string | null;
  result_count?: number | null;
  result_bytes?: number | null;
  truncated: boolean;
  error?: string | null;
  started_at: string;
  duration_ms?: number | null;
}

export interface DebugAnalysis {
  id: string;
  session_id: string;
  snapshot_id?: string | null;
  status: DebugAnalysisStatus;
  kind: string;
  provider_name?: string | null;
  model_name?: string | null;
  prompt_version: string;
  context_version: string;
  started_at: string;
  completed_at?: string | null;
  duration_ms?: number | null;
  tool_call_count: number;
  files_accessed: number;
  context_bytes?: number | null;
  confidence: ConfidenceLevel;
  summary?: string | null;
  invalid_references: Array<Record<string, unknown>>;
  missing_evidence: string[];
  recommended_inspections: string[];
  degraded: boolean;
  degraded_reason?: string | null;
  locations: DebugCodeLocation[];
  hypotheses: DebugHypothesis[];
  evidence: DebugEvidence[];
  counts: Record<string, number>;
}

export interface DebugSessionDetail extends DebugSession {
  snapshot?: CodeSnapshot | null;
  repository?: Repository | null;
  latest_analysis?: DebugAnalysis | null;
  locations: DebugCodeLocation[];
  hypotheses: DebugHypothesis[];
  messages: DebugMessage[];
  counts: Record<string, number>;
}

export interface DebugSessionList {
  items: DebugSession[];
  total: number;
}

export interface CreateDebugSessionPayload {
  title?: string | null;
  repository_id?: string | null;
  snapshot_id?: string | null;
  created_by?: string | null;
  run_analysis?: boolean;
  index_snapshot?: boolean;
}

export interface AskDebugPayload {
  question: string;
  asked_by?: string | null;
}

export interface DebugAssistantAnswer {
  message_id: string;
  answer: string;
  evidence: string[];
  invalid_references: Array<Record<string, unknown>>;
  missing_evidence: string[];
  confidence: ConfidenceLevel;
  tool_calls: Array<Record<string, unknown>>;
  degraded_reason?: string | null;
  budget: Record<string, unknown>;
}

export interface DebugTimelineEvent {
  at: string;
  kind: string;
  title: string;
  detail?: string | null;
  reference?: string | null;
}

export interface DebugTimeline {
  session_id: string;
  incident_id: string;
  items: DebugTimelineEvent[];
  notes: string[];
}

export interface DebuggerMetrics {
  sessions: number;
  sessions_completed: number;
  analyses: number;
  analyses_degraded: number;
  hypotheses: number;
  by_validation_status: Record<string, number>;
  locations_claimed: number;
  locations_valid: number;
  locations_rejected: number;
  invalid_references: number;
  tool_calls: number;
  tool_calls_refused: number;
  repositories: number;
  snapshots: number;
  index_status: Record<string, number>;
  engine_version: string;
  limitations: string[];
}

export interface CommitInfo {
  sha: string;
  short_sha: string;
  author?: string | null;
  committed_at?: string | null;
  message?: string | null;
  files_changed: number;
  parents: string[];
}

export interface HistoryResult {
  repository_id: string;
  revision?: string | null;
  path?: string | null;
  items: CommitInfo[];
  truncated: boolean;
  reason?: string | null;
}

// ---------------------------------------------------------------------------
// Phase 7 — Fix Generation & Verification (§5–§72)
// ---------------------------------------------------------------------------

export type FixCategory =
  | 'BUG_FIX'
  | 'ERROR_HANDLING'
  | 'TIMEOUT_FIX'
  | 'RETRY_FIX'
  | 'VALIDATION_FIX'
  | 'RESOURCE_HANDLING'
  | 'CONCURRENCY_FIX'
  | 'DATABASE_QUERY_FIX'
  | 'API_CONTRACT_FIX'
  | 'CONFIGURATION_FIX'
  | 'DEPENDENCY_HANDLING'
  | 'PERFORMANCE_FIX'
  | 'UNKNOWN';

export type FixStatus =
  | 'DRAFT'
  | 'HYPOTHESIZED'
  | 'PATCHING'
  | 'PATCH_GENERATED'
  | 'GENERATION_FAILED'
  | 'REJECTED'
  | 'SUPERSEDED';

export type PatchStatus =
  | 'GENERATED'
  | 'PARSE_FAILED'
  | 'VALIDATION_FAILED'
  | 'APPLIED'
  | 'BUILD_FAILED'
  | 'TEST_FAILED'
  | 'REPRODUCTION_FAILED'
  | 'VERIFIED'
  | 'REJECTED'
  | 'SUPERSEDED'
  | 'GENERATION_FAILED';

export type PatchFormat = 'UNIFIED_DIFF';

export type RiskLevel = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';

export type VerificationLevel =
  | 'NONE'
  | 'STATIC_VALIDATED'
  | 'TEST_VALIDATED'
  | 'REPRODUCTION_VALIDATED'
  | 'REGRESSION_VALIDATED'
  | 'FULLY_VERIFIED';

export type VerificationStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'VERIFIED'
  | 'NOT_VERIFIED'
  | 'FAILED'
  | 'CANCELLED';

export type WorkspaceStatus =
  | 'CREATING'
  | 'READY'
  | 'PATCH_APPLIED'
  | 'BUSY'
  | 'DESTROYED'
  | 'FAILED';

export type ReviewAction =
  | 'APPROVE'
  | 'REJECT'
  | 'REQUEST_CHANGES'
  | 'REGENERATE'
  | 'EXPORT';

export type ReviewState =
  | 'AWAITING_REVIEW'
  | 'CHANGES_REQUESTED'
  | 'APPROVED'
  | 'REJECTED';

export type TamperingFlag =
  | 'NONE'
  | 'TEST_DELETED'
  | 'ASSERTION_WEAKENED'
  | 'TEST_SKIPPED'
  | 'EXPECTATION_CHANGED'
  | 'LINT_DISABLED'
  | 'TYPECHECK_DISABLED'
  | 'CI_MODIFIED'
  | 'VERIFICATION_MODIFIED';

export interface FixHypothesis {
  id: string;
  project_id: string;
  incident_id: string;
  debug_session_id?: string | null;
  analysis_run_id?: string | null;
  root_cause_candidate_id?: string | null;
  reproduction_experiment_id?: string | null;
  repository_id?: string | null;
  snapshot_id?: string | null;
  title: string;
  description: string;
  proposed_change: string;
  expected_behavior?: string | null;
  category: FixCategory;
  scope_files: string[];
  excluded_paths: string[];
  supporting_evidence: Array<Record<string, unknown>>;
  target_symbols: string[];
  risk_level: RiskLevel;
  confidence: string;
  status: FixStatus;
  created_by?: string | null;
  created_at: string;
}

export interface FixHypothesisList {
  items: FixHypothesis[];
  total: number;
  truncated: boolean;
}

export interface Patch {
  id: string;
  project_id: string;
  fix_hypothesis_id: string;
  patch_experiment_id?: string | null;
  base_commit_sha?: string | null;
  patch_format: PatchFormat;
  changed_files: number;
  lines_added: number;
  lines_removed: number;
  symbols_modified: string[];
  affected_paths: string[];
  generated_by: string;
  generation_model?: string | null;
  status: PatchStatus;
  explanation: Record<string, unknown>;
  failure_reason?: string | null;
  created_at: string;
  review_state?: ReviewState | null;
}

export interface PatchList {
  items: Patch[];
  total: number;
  truncated: boolean;
}

export interface PatchTestRun {
  id: string;
  kind: string;
  command_key: string;
  command_resolved?: string | null;
  unknown_configuration: boolean;
  exit_code?: number | null;
  timed_out: boolean;
  duration_ms?: number | null;
  tests_total?: number | null;
  tests_passed?: number | null;
  tests_failed?: number | null;
  output_tail?: string | null;
  selected_tests: string[];
  selection_reason?: string | null;
}

export interface PatchRegressionTest {
  id: string;
  name: string;
  file_path: string;
  origin: string;
  ran_on_base: boolean;
  failed_on_base: boolean;
  ran_on_patched: boolean;
  passed_on_patched: boolean;
  valid: boolean;
  invalid_reason?: string | null;
  content_hash?: string | null;
}

export interface PatchComparison {
  id: string;
  metrics: Record<string, unknown>;
  regressions: unknown[];
  thresholds: Record<string, unknown>;
  causal_chain_resolved?: boolean | null;
  causal_chain_note?: string | null;
  summary?: string | null;
}

export interface PatchWorkspace {
  id: string;
  patch_id: string;
  branch_name: string;
  base_commit_sha?: string | null;
  patched_commit_sha?: string | null;
  status: WorkspaceStatus;
  created_at_workspace?: string | null;
  destroyed_at?: string | null;
  workspace_metadata: Record<string, unknown>;
}

export interface PatchVerificationRun {
  id: string;
  patch_id: string;
  workspace_id?: string | null;
  status: VerificationStatus;
  level: VerificationLevel;
  confidence: string;
  confidence_reason?: string | null;
  tampering_flag: TamperingFlag;
  verification_env_intact: boolean;
  baseline_failure_reproduced: boolean;
  patched_failure_reproduced?: boolean | null;
  regression_detected: boolean;
  started_at: string;
  completed_at?: string | null;
  duration_ms?: number | null;
  verdict_reason?: string | null;
  evidence: Record<string, unknown>;
  test_runs: PatchTestRun[];
  regression_tests: PatchRegressionTest[];
  comparisons: PatchComparison[];
}

export interface PatchReviewAction {
  id: string;
  patch_id: string;
  action: ReviewAction;
  actor: string;
  reason?: string | null;
  new_patch_id?: string | null;
  audit_metadata: Record<string, unknown>;
  created_at: string;
}

export interface PatchDetail extends Patch {
  patch_content: string;
  review_actions: PatchReviewAction[];
  verification_runs: PatchVerificationRun[];
  workspaces: PatchWorkspace[];
}

export interface FixMetrics {
  hypotheses_total: number;
  patches_total: number;
  patches_verified: number;
  patches_awaiting_review: number;
  patches_rejected: number;
  verification_runs_total: number;
  tampering_flags_total: number;
  regressions_detected_total: number;
}

export interface CreateFixHypothesisPayload {
  debug_session_id: string;
  title?: string | null;
  scope_override?: string[] | null;
  root_cause_candidate_id?: string | null;
  reproduction_experiment_id?: string | null;
}

export interface GeneratePatchPayload {
  generated_by?: string;
  patch_experiment_id?: string | null;
}

export interface VerifyPatchPayload {
  baseline_reproduced: boolean;
  baseline_metrics?: Record<string, number> | null;
  patched_metrics?: Record<string, number> | null;
  baseline_failure_signature?: string;
  patched_still_reproduces?: boolean;
}

export interface ReviewPatchPayload {
  actor?: string;
  reason?: string | null;
  new_patch_id?: string | null;
}

export interface FixArtifact {
  name: string;
  kind: string;
  content_hash: string;
  size_bytes?: number | null;
  immutable: boolean;
  created_at: string;
  reference?: Record<string, unknown>;
}
// ---------------------------------------------------------------------------
// Phase 8 — predictive reliability (§44)
// ---------------------------------------------------------------------------

export type ForecastHorizon =
  | 'ONE_HOUR'
  | 'SIX_HOURS'
  | 'TWENTY_FOUR_HOURS'
  | 'SEVEN_DAYS';

export type PredictionType =
  | 'FAILURE_RISK'
  | 'ERROR_RATE_RISK'
  | 'LATENCY_RISK'
  | 'AVAILABILITY_RISK'
  | 'RESOURCE_EXHAUSTION_RISK'
  | 'DEPENDENCY_FAILURE_RISK'
  | 'REGRESSION_RISK'
  | 'INCIDENT_RISK'
  | 'RELIABILITY_DEGRADATION';

export type ForecastRiskLevel = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL' | 'UNKNOWN';

export type ForecastStatus =
  | 'GENERATED'
  | 'ACTIVE'
  | 'EXPIRED'
  | 'CONFIRMED'
  | 'FALSE_POSITIVE'
  | 'INCONCLUSIVE';

export type DataQuality = 'GOOD' | 'PARTIAL' | 'POOR' | 'INSUFFICIENT';

export type CalibrationStatus = 'GOOD' | 'ACCEPTABLE' | 'POOR' | 'UNKNOWN';

export type PredictionOutcomeType =
  | 'TRUE_POSITIVE'
  | 'FALSE_POSITIVE'
  | 'TRUE_NEGATIVE'
  | 'FALSE_NEGATIVE'
  | 'INCONCLUSIVE';

export interface ForecastSignal {
  id: string;
  forecast_id: string;
  project_id: string;
  environment_id?: string | null;
  component_id?: string | null;
  signal_type: string;
  severity: 'INFO' | 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
  contribution?: number | null;
  rank: number;
  description: string;
  metric_name?: string | null;
  observed_value?: number | null;
  baseline_value?: number | null;
  change_rate?: number | null;
  trend: 'RISING' | 'FALLING' | 'FLAT' | 'VOLATILE' | 'UNKNOWN';
  evidence_ids: Record<string, unknown>;
  similar_incident_count: number;
  created_at: string;
}

export interface Forecast {
  id: string;
  project_id: string;
  environment_id?: string | null;
  component_id?: string | null;
  prediction_type: PredictionType;
  forecast_horizon: ForecastHorizon;
  generated_at: string;
  valid_from: string;
  valid_until: string;
  risk_score?: number | null;
  risk_level: ForecastRiskLevel;
  confidence?: number | null;
  confidence_reason?: string | null;
  calibration_status: CalibrationStatus;
  data_quality: DataQuality;
  data_coverage?: number | null;
  model_version_id?: string | null;
  model_version_label: string;
  feature_snapshot_id?: string | null;
  status: ForecastStatus;
  fingerprint: string;
  dominant_signal?: string | null;
  headline: string;
  summary?: string | null;
  limitations: string[];
  supporting_evidence: Record<string, unknown>;
  failure_reason?: string | null;
  failure_detail?: string | null;
  previous_forecast_id?: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
  signals: ForecastSignal[];
}

export interface ForecastList {
  items: Forecast[];
  total: number;
  truncated: boolean;
}

/** §34 — the four questions: what changed, why, what supports it, what is uncertain. */
export interface ForecastExplanation extends Record<string, unknown> {
  forecast_id: string;
  headline: string;
  risk_level: ForecastRiskLevel;
  what_changed: string[];
  why_risk_increased: string[];
  what_supports_this: Record<string, unknown>;
  what_is_uncertain: string[];
  why_risk_changed: Record<string, unknown>;
  historical_evidence: Array<Record<string, unknown>>;
  caveats: string[];
  data_quality: DataQuality;
  data_coverage?: number | null;
  model_version: string;
  horizon_label: string;
}

export interface FeatureSnapshot {
  id: string;
  project_id: string;
  component_id?: string | null;
  forecast_time: string;
  feature_window_start: string;
  feature_window_end: string;
  feature_schema_version: string;
  feature_values: Record<string, unknown>;
  data_sources: Record<string, unknown>;
  data_quality: DataQuality;
  data_quality_notes: string[];
  data_coverage?: number | null;
  sample_count: number;
  created_at: string;
}

export interface ForecastOutcome {
  id: string;
  forecast_id: string;
  evaluation_window_start: string;
  evaluation_window_end: string;
  outcome: PredictionOutcomeType;
  actual_event?: string | null;
  actual_severity?: string | null;
  time_to_event_seconds?: number | null;
  matched_incident_id?: string | null;
  matched_anomaly_id?: string | null;
  predicted_risk_level: ForecastRiskLevel;
  predicted_risk_score?: number | null;
  evaluation_reason: string;
  evaluated_at: string;
}

export interface RiskHeatmapCell {
  component_id?: string | null;
  component_name?: string | null;
  environment_id?: string | null;
  prediction_type: PredictionType;
  by_horizon: Partial<Record<ForecastHorizon, ForecastRiskLevel>>;
  worst_level: ForecastRiskLevel;
  evidence_count: number;
}

export interface RiskHeatmap {
  cells: RiskHeatmapCell[];
  horizons: ForecastHorizon[];
  generated_at: string;
  empty_reason?: string | null;
}

export interface ComponentProfile {
  project_id: string;
  component_id: string;
  component_name?: string | null;
  component_type?: string | null;
  generated_at: string;
  current_risk: Record<string, unknown>;
  worst_risk: ForecastRiskLevel;
  signals: Record<string, unknown>;
  reliability_score: Record<string, unknown>;
  data_quality: DataQuality;
  data_coverage?: number | null;
  data_quality_notes: string[];
  recent_incidents: Array<Record<string, unknown>>;
  forecasts: Forecast[];
  limitations: string[];
}

export interface ModelVersion {
  id: string;
  model_name: string;
  model_type: string;
  version: string;
  algorithm?: string | null;
  training_window_seconds?: number | null;
  feature_schema_version: string;
  parameters: Record<string, unknown>;
  metrics: Record<string, unknown>;
  calibration_metrics: Record<string, unknown>;
  calibration_status: CalibrationStatus;
  sample_count: number;
  status: 'DEVELOPMENT' | 'VALIDATED' | 'ACTIVE' | 'RETIRED';
  description?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ModelVersionList {
  items: ModelVersion[];
  total: number;
  truncated: boolean;
}

export interface EvaluationRun {
  id: string;
  project_id?: string | null;
  model_version_label?: string | null;
  prediction_type?: PredictionType | null;
  forecast_horizon?: ForecastHorizon | null;
  status: 'COMPLETED' | 'INSUFFICIENT_SAMPLE' | 'FAILED';
  dataset_window_start: string;
  dataset_window_end: string;
  feature_schema_version: string;
  sample_count: number;
  positive_count: number;
  negative_count: number;
  inconclusive_count: number;
  metrics: Record<string, unknown>;
  calibration: Record<string, unknown>;
  calibration_status: CalibrationStatus;
  reliability_bands: Array<Record<string, unknown>>;
  notes: string[];
  created_at: string;
}

export interface EvaluationRunList {
  items: EvaluationRun[];
  total: number;
  truncated: boolean;
}

export interface BacktestStep {
  origin: string;
  risk_level: ForecastRiskLevel;
  risk_score?: number | null;
  outcome: PredictionOutcomeType;
  reason?: string | null;
  time_to_event_seconds?: number | null;
}

export interface Backtest {
  id: string;
  project_id: string;
  status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED';
  configuration: Record<string, unknown>;
  start_time: string;
  end_time: string;
  training_window_seconds: number;
  forecast_horizon: ForecastHorizon;
  prediction_type: PredictionType;
  evaluation_run_id?: string | null;
  metrics: Record<string, unknown>;
  sample_count: number;
  error?: string | null;
  created_by?: string | null;
  created_at: string;
  steps: BacktestStep[];
}

export interface BacktestList {
  items: Backtest[];
  total: number;
  truncated: boolean;
}

export interface BacktestRunResult {
  items: Backtest[];
  total: number;
  note: string;
}

export interface DriftFinding {
  id: string;
  project_id: string;
  kind: 'FEATURE_DRIFT' | 'PREDICTION_DRIFT' | 'OUTCOME_DRIFT' | 'CALIBRATION_DRIFT' | 'DATA_DRIFT';
  status: 'STABLE' | 'WATCH' | 'FLAGGED';
  feature_name?: string | null;
  drift_score?: number | null;
  threshold?: number | null;
  description: string;
  requires_review: boolean;
  created_at: string;
}

export interface DriftHistory {
  summary: Record<string, unknown>;
  items: DriftFinding[];
  total: number;
  truncated: boolean;
}

export interface DriftReport {
  project_id: string;
  reference_window: string[];
  current_window: string[];
  worst_status: 'STABLE' | 'WATCH' | 'FLAGGED';
  flagged_count: number;
  findings: Array<Record<string, unknown>>;
  notes: string[];
  review_policy: string;
  retrain_performed: boolean;
  model_activated: boolean;
}

export interface EarlyWarning {
  id: string;
  project_id: string;
  environment_id?: string | null;
  component_id?: string | null;
  forecast_id?: string | null;
  fingerprint: string;
  title: string;
  description?: string | null;
  severity: ForecastRiskLevel;
  status: 'OPEN' | 'ACKNOWLEDGED' | 'DISMISSED' | 'EXPIRED';
  occurrence_count: number;
  first_raised_at: string;
  last_raised_at: string;
  last_suppressed_at?: string | null;
  acknowledged_at?: string | null;
  acknowledged_by?: string | null;
}

export interface EarlyWarningList {
  items: EarlyWarning[];
  total: number;
  truncated: boolean;
}

export interface PlatformHealth {
  generated_at: string;
  forecast_count: number;
  active_forecasts: number;
  high_risk_forecasts: number;
  unknown_forecasts: number;
  data_quality_distribution: Record<string, number>;
  model_version_count: number;
  thresholds: Record<string, number>;
  limits: Record<string, unknown>;
  accuracy: Record<string, unknown>;
  calibration: Record<string, unknown>;
  coverage: Record<string, unknown>;
  drift: Record<string, unknown>;
  warnings: Record<string, unknown>;
  models: Array<Record<string, unknown>>;
  notes: string[];
  limitations: string[];
}

export interface ForecastGenerateResult {
  dispatched: boolean;
  project_id: string;
  job_id?: string | null;
  scopes: number;
  forecasts_created: number;
  forecasts_updated: number;
  signals_created: number;
  skipped: string[];
  errors: string[];
  duration_ms: number;
  message: string;
}

export interface GenerateForecastsPayload {
  environment_id?: string | null;
  prediction_types?: PredictionType[];
  horizons?: ForecastHorizon[];
  limit?: number;
  dispatch?: boolean;
}

export interface BacktestPayload {
  start_time: string;
  end_time: string;
  training_window_seconds?: number;
  forecast_horizon?: ForecastHorizon;
  prediction_type?: PredictionType;
  component_ids?: string[];
  environment_id?: string | null;
  step_seconds?: number;
  max_steps?: number;
  max_components?: number;
  created_by?: string;
}

export interface WarningActionPayload {
  actor?: string;
  reason?: string | null;
}

// ---------------------------------------------------------------------------
// Phase 9 — safe autonomous remediation (§2–§5, §12, §21, §44)
// ---------------------------------------------------------------------------

export type RemediationActionTypeValue =
  | 'RESTART_SERVICE'
  | 'RESTART_INSTANCE'
  | 'SCALE_SERVICE_WITHIN_LIMIT'
  | 'DISABLE_FEATURE_FLAG'
  | 'ENABLE_FEATURE_FLAG'
  | 'PAUSE_BACKGROUND_JOB'
  | 'RESUME_BACKGROUND_JOB'
  | 'DISABLE_DEGRADED_DEPENDENCY'
  | 'ROUTE_TRAFFIC_TO_HEALTHY_INSTANCE'
  | 'ROLLBACK_DEPLOYMENT'
  | 'ROLLBACK_CONFIGURATION'
  | 'APPLY_VERIFIED_PATCH';

export type RemediationStatusValue =
  | 'PROPOSED'
  | 'VALIDATING'
  | 'POLICY_REVIEW'
  | 'AWAITING_APPROVAL'
  | 'AUTHORIZED'
  | 'SCHEDULED'
  | 'EXECUTING'
  | 'VERIFYING'
  | 'VERIFIED'
  | 'FAILED'
  | 'ROLLING_BACK'
  | 'ROLLED_BACK'
  | 'REJECTED'
  | 'CANCELLED'
  | 'EXPIRED'
  | 'BLOCKED';

export type RemediationExecutionModeValue =
  | 'OBSERVE_ONLY'
  | 'DRY_RUN'
  | 'SHADOW'
  | 'HUMAN_APPROVAL'
  | 'AUTONOMOUS'
  | 'EMERGENCY_STOP';

export type RemediationRiskLevelValue = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';

export type PolicyDecisionValue =
  | 'ALLOW'
  | 'ALLOW_WITH_CANARY'
  | 'REQUIRE_APPROVAL'
  | 'DENY';

export type SafetyStatusValue = 'PASSED' | 'PASSED_WITH_WARNINGS' | 'FAILED';

export type BlastRadiusScopeValue =
  | 'SINGLE_INSTANCE'
  | 'SINGLE_COMPONENT'
  | 'SINGLE_ENVIRONMENT'
  | 'LIMITED_PERCENT'
  | 'PROJECT_WIDE';

export type RemediationOutcomeValue =
  | 'EFFECTIVE'
  | 'PARTIALLY_EFFECTIVE'
  | 'INEFFECTIVE'
  | 'HARMFUL'
  | 'UNKNOWN';

export interface RemediationActionParameter {
  name: string;
  kind: string;
  required: boolean;
  choices?: string[] | null;
  choices_from?: string | null;
  minimum?: number | null;
  maximum?: number | null;
  default?: unknown;
  description: string;
}

export interface RemediationActionTypeInfo {
  action_type: RemediationActionTypeValue;
  description: string;
  risk_level: RemediationRiskLevelValue;
  adapter_kind: string;
  parameters: RemediationActionParameter[];
  verification_plan: string[];
  rollback_strategy: string;
  inverse_action?: RemediationActionTypeValue | null;
  maximum_blast_radius: BlastRadiusScopeValue;
  production_effect: boolean;
  supports_canary: boolean;
  supports_autonomous_execution: boolean;
  requires_human_approval: boolean;
  reversible: boolean;
  executable_in_build: boolean;
  unavailable_reason?: string | null;
  notes: string[];
}

export interface RemediationActionTypeList {
  actions: RemediationActionTypeInfo[];
  count: number;
  execution_enabled: boolean;
}

export interface RemediationPolicy {
  id?: string | null;
  project_id?: string | null;
  environment_id?: string | null;
  source: string;
  revision?: number | null;
  enabled: boolean;
  execution_mode: RemediationExecutionModeValue;
  autonomous_max_risk: RemediationRiskLevelValue;
  allowed_action_types?: string[] | null;
  allowed_environment_names?: string[] | null;
  max_actions_per_window: number;
  action_window_seconds: number;
  cooldown_seconds: number;
  max_concurrent_actions: number;
  max_blast_radius_percent: number;
  max_blast_radius_scope: BlastRadiusScopeValue;
  canary_enabled: boolean;
  canary_percent: number;
  approval_ttl_seconds: number;
  verification_window_seconds: number;
  execution_timeout_seconds: number;
  action_expiry_seconds: number;
  emergency_stop_active: boolean;
  emergency_stop_reason?: string | null;
  emergency_stop_at?: string | null;
  emergency_stop_by?: string | null;
  updated_by?: string | null;
  clamped: string[];
  notes?: string | null;
}

export interface RemediationPolicyUpdate {
  enabled?: boolean;
  execution_mode?: RemediationExecutionModeValue;
  autonomous_max_risk?: RemediationRiskLevelValue;
  allowed_action_types?: string[] | null;
  allowed_environment_names?: string[] | null;
  max_actions_per_window?: number;
  action_window_seconds?: number;
  cooldown_seconds?: number;
  max_concurrent_actions?: number;
  max_blast_radius_percent?: number;
  canary_enabled?: boolean;
  canary_percent?: number;
  approval_ttl_seconds?: number;
  verification_window_seconds?: number;
  execution_timeout_seconds?: number;
  action_expiry_seconds?: number;
  environment_id?: string | null;
  notes?: string | null;
  updated_by?: string | null;
}

export interface RemediationControl {
  id: string;
  kind: string;
  scope_key: string;
  state: string;
  previous_state?: string | null;
  is_current: boolean;
  revision: number;
  applied_at?: string | null;
  expires_at?: string | null;
  reverted_at?: string | null;
  applied_by?: string | null;
  reason?: string | null;
  applied_by_action_id?: string | null;
  effective: boolean;
}

export interface RemediationControlList {
  controls: RemediationControl[];
  count: number;
}

export interface RemediationBreaker {
  action_type: RemediationActionTypeValue;
  state: string;
  consecutive_failures: number;
  total_attempts: number;
  total_failures: number;
  total_successes: number;
  threshold: number;
  opened_at?: string | null;
  opened_until?: string | null;
  last_failure_at?: string | null;
  last_success_at?: string | null;
  last_trip_reason?: string | null;
}

export interface RemediationBreakerList {
  breakers: RemediationBreaker[];
  count: number;
}

export interface RemediationProposal {
  id: string;
  project_id: string;
  environment_id?: string | null;
  component_id?: string | null;
  action_type: RemediationActionTypeValue;
  source_type: string;
  source_id?: string | null;
  strategy?: string | null;
  problem: string;
  recommended_action: string;
  expected_effect: string;
  supporting_evidence?: Array<Record<string, unknown>> | null;
  parameters?: Record<string, unknown> | null;
  risk_level: RemediationRiskLevelValue;
  blast_radius: BlastRadiusScopeValue;
  blast_radius_percent?: number | null;
  preconditions?: Array<Record<string, unknown>> | null;
  verification_plan?: Record<string, unknown> | null;
  rollback_plan?: Record<string, unknown> | null;
  confidence?: number | null;
  confidence_reason?: string | null;
  limitations?: string[] | null;
  rationale?: string | null;
  generated_by: string;
  model_version?: string | null;
  incident_id?: string | null;
  forecast_id?: string | null;
  causal_analysis_id?: string | null;
  root_cause_candidate_id?: string | null;
  patch_id?: string | null;
  fingerprint: string;
  created_at?: string | null;
}

export interface RemediationAction {
  id: string;
  project_id: string;
  environment_id?: string | null;
  component_id?: string | null;
  proposal_id?: string | null;
  action_type: RemediationActionTypeValue;
  status: RemediationStatusValue;
  description: string;
  reason?: string | null;
  headline: string;
  risk_level: RemediationRiskLevelValue;
  blast_radius: BlastRadiusScopeValue;
  blast_radius_percent?: number | null;
  affected_resource_count: number;
  source_type: string;
  source_id?: string | null;
  parameters?: Record<string, unknown> | null;
  safety_status?: SafetyStatusValue | null;
  policy_status?: PolicyDecisionValue | null;
  authorization_status?: string | null;
  execution_status?: string | null;
  execution_mode: RemediationExecutionModeValue;
  adapter_kind: string;
  rollback_strategy: string;
  rollback_available: boolean;
  rollback_plan?: Record<string, unknown> | null;
  verification_plan?: Record<string, unknown> | null;
  preconditions?: Array<Record<string, unknown>> | null;
  canary_required: boolean;
  canary_stage: string;
  canary_percent?: number | null;
  attempt: number;
  retry_count: number;
  max_retries: number;
  failure_reason?: string | null;
  failure_detail?: string | null;
  outcome?: RemediationOutcomeValue | null;
  post_analysis_status: string;
  fingerprint: string;
  incident_id?: string | null;
  forecast_id?: string | null;
  patch_id?: string | null;
  created_by: string;
  approved_by?: string | null;
  authorized_by?: string | null;
  executed_by?: string | null;
  created_at?: string | null;
  approved_at?: string | null;
  authorized_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  expires_at?: string | null;
  rollback_performed_at?: string | null;
}

export interface RemediationActionList {
  actions: RemediationAction[];
  count: number;
  total: number;
}

export interface RemediationAssessment {
  id: string;
  status: SafetyStatusValue;
  checks?: Array<Record<string, unknown>> | null;
  blocking?: string[] | null;
  warnings?: string[] | null;
  reversible: boolean;
  rollback_plan?: Record<string, unknown> | null;
  blast_radius: BlastRadiusScopeValue;
  blast_radius_percent?: number | null;
  affected_resource_count: number;
  requires_human_approval: boolean;
  reason?: string | null;
  assessed_by: string;
  created_at?: string | null;
}

export interface RemediationPolicyDecision {
  id: string;
  policy_id?: string | null;
  decision: PolicyDecisionValue;
  execution_mode: RemediationExecutionModeValue;
  policy_revision?: number | null;
  matched_rules?: Array<Record<string, unknown>> | null;
  reasons?: string[] | null;
  failure_reason?: string | null;
  requires_canary: boolean;
  budget_state?: Record<string, unknown> | null;
  circuit_state?: Record<string, unknown> | null;
  evaluated_by: string;
  created_at?: string | null;
}

export interface RemediationApproval {
  id: string;
  status: string;
  actor_type: string;
  actor?: string | null;
  decided_at?: string | null;
  expires_at?: string | null;
  reason?: string | null;
  scope_snapshot?: Record<string, unknown> | null;
  created_at?: string | null;
}

export interface RemediationExecutionRecord {
  id: string;
  attempt: number;
  mode: RemediationExecutionModeValue;
  adapter_kind: string;
  adapter_name?: string | null;
  status: string;
  effect_applied: boolean;
  steps?: Array<Record<string, unknown>> | null;
  control_ids?: string[] | null;
  output_summary?: string | null;
  failure_reason?: string | null;
  error?: string | null;
  idempotency_key: string;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms?: number | null;
  executed_by: string;
}

export interface RemediationVerificationRecord {
  id: string;
  execution_id?: string | null;
  verdict: string;
  checks?: Array<Record<string, unknown>> | null;
  passed_count: number;
  failed_count: number;
  not_observable_count: number;
  window_start?: string | null;
  window_end?: string | null;
  observation_seconds: number;
  summary?: string | null;
  limitations?: string[] | null;
  verified_by: string;
  created_at?: string | null;
}

export interface RemediationRollbackRecord {
  id: string;
  trigger: string;
  strategy: string;
  status: string;
  plan?: Record<string, unknown> | null;
  steps?: Array<Record<string, unknown>> | null;
  controls_reverted?: string[] | null;
  verification_verdict?: string | null;
  failure_reason?: string | null;
  error?: string | null;
  requested_by?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  created_at?: string | null;
}

export interface RemediationAuditEvent {
  id: string;
  sequence: number;
  event_type: string;
  actor_type: string;
  actor?: string | null;
  from_status?: RemediationStatusValue | null;
  to_status?: RemediationStatusValue | null;
  summary: string;
  detail?: Record<string, unknown> | null;
  occurred_at?: string | null;
  entry_hash: string;
  prev_hash?: string | null;
}

export interface RemediationAuditChain {
  action_id: string;
  intact: boolean;
  events: number;
  broken_at?: number | null;
  reason?: string | null;
}

export interface RemediationActionDetail {
  action: RemediationAction;
  proposal?: RemediationProposal | null;
  assessments: RemediationAssessment[];
  policy_decisions: RemediationPolicyDecision[];
  approvals: RemediationApproval[];
  executions: RemediationExecutionRecord[];
  verifications: RemediationVerificationRecord[];
  rollbacks: RemediationRollbackRecord[];
  audit: RemediationAuditEvent[];
  audit_chain?: RemediationAuditChain | null;
  post_analysis?: Record<string, unknown> | null;
  allowed_transitions: RemediationStatusValue[];
}

export interface RemediationPlanRequest {
  project_id: string;
  environment_id?: string | null;
  incident_id?: string | null;
  forecast_id?: string | null;
  auto_assess?: boolean;
}

export interface RemediationPlanResult {
  project_id: string;
  proposals_created: number;
  actions_created: number;
  actions: RemediationAction[];
  skipped_duplicates: number;
  detail?: string | null;
}

export interface RemediationProposeRequest {
  project_id: string;
  action_type: RemediationActionTypeValue;
  description: string;
  reason?: string | null;
  environment_id?: string | null;
  component_id?: string | null;
  incident_id?: string | null;
  forecast_id?: string | null;
  patch_id?: string | null;
  parameters?: Record<string, unknown>;
  blast_radius?: BlastRadiusScopeValue | null;
  blast_radius_percent?: number | null;
  created_by?: string;
  auto_assess?: boolean;
}

export interface RemediationDecisionRequest {
  actor: string;
  reason?: string | null;
}

export interface RemediationRunStep {
  action_id: string;
  status: RemediationStatusValue;
  step: string;
  detail: string;
  failure_reason?: string | null;
}

export interface RemediationRunResult {
  action_id: string;
  status: RemediationStatusValue;
  outcome?: RemediationOutcomeValue | null;
  detail?: string | null;
  steps: RemediationRunStep[];
}

export interface RemediationMetrics {
  project_id?: string | null;
  total_actions: number;
  by_status: Record<string, number>;
  by_action_type: Record<string, number>;
  by_failure_reason: Record<string, number>;
  outcomes: Record<string, number>;
  executions_attempted: number;
  executions_with_effect: number;
  verifications_passed: number;
  verifications_failed: number;
  verifications_inconclusive: number;
  rollbacks_succeeded: number;
  rollbacks_failed: number;
  controls_in_force: number;
  open_breakers: number;
  awaiting_approval: number;
  autonomous_authorizations: number;
  human_authorizations: number;
  emergency_stop_active: boolean;
}

// ---------------------------------------------------------------------------
// Phase 10 — reliability intelligence (§51–§61)
//
// Every learned artifact carries its own qualification: a knowledge item has a
// sample count, a coverage window, a confidence and its limitations; a
// recommendation has the evidence it used, the uncertainty, and what Phase 9
// would require. The types carry those fields because dropping one downstream is
// how "restart works 81% of the time" gets shown with no sample size (§15, §40).
// ---------------------------------------------------------------------------

export type KnowledgeStatusValue =
  | 'CANDIDATE'
  | 'VALIDATING'
  | 'VALIDATED'
  | 'ACTIVE'
  | 'DEPRECATED'
  | 'REJECTED'
  | 'SUPERSEDED';

/** §4. Only VALIDATED and ACTIVE may influence a recommendation. */
export const LIVE_KNOWLEDGE_STATUSES = ['VALIDATED', 'ACTIVE'] as const;

/** §4. Retired statuses are kept for history and shown as retired. */
export const RETIRED_KNOWLEDGE_STATUSES = [
  'DEPRECATED',
  'REJECTED',
  'SUPERSEDED',
] as const;

export type KnowledgeTypeValue =
  | 'INCIDENT_PATTERN'
  | 'FAILURE_PATTERN'
  | 'ANOMALY_PATTERN'
  | 'REMEDIATION_PATTERN'
  | 'REGRESSION_PATTERN'
  | 'DEPENDENCY_PATTERN'
  | 'DEPLOYMENT_PATTERN'
  | 'RESOURCE_PATTERN'
  | 'PREDICTIVE_PATTERN'
  | 'RECOVERY_PATTERN'
  | 'COMPONENT_RELIABILITY_PATTERN';

export type KnowledgeConfidenceValue = 'UNKNOWN' | 'LOW' | 'MEDIUM' | 'HIGH';

export type KnowledgeScopeValue =
  | 'COMPONENT_SPECIFIC'
  | 'SERVICE_CLASS'
  | 'PROJECT_LEVEL'
  | 'CROSS_PROJECT';

export interface KnowledgeItem {
  id: string;
  knowledge_type: KnowledgeTypeValue;
  status: KnowledgeStatusValue;
  scope: KnowledgeScopeValue;
  component_id?: string | null;
  environment_id?: string | null;
  title: string;
  description: string;
  feature_signature: string;
  sample_count: number;
  success_count?: number | null;
  support_strength?: number | null;
  coverage_start?: string | null;
  coverage_end?: string | null;
  confidence: KnowledgeConfidenceValue;
  algorithm: string;
  algorithm_version: string;
  feature_schema_version: string;
  validation?: Record<string, unknown> | null;
  limitations: string[];
  version: number;
  supersedes_knowledge_id?: string | null;
  reviewed_at?: string | null;
  reviewed_by?: string | null;
  review_reason?: string | null;
  last_confirmed_at?: string | null;
  sources: Array<Record<string, unknown>>;
  experience_ids: string[];
  created_at?: string | null;
  updated_at?: string | null;
}

export interface KnowledgeVersionItem {
  id: string;
  version: number;
  status: KnowledgeStatusValue;
  confidence: KnowledgeConfidenceValue;
  sample_count: number;
  snapshot: Record<string, unknown>;
  note?: string | null;
  learning_run_id?: string | null;
  activated_at?: string | null;
  deactivated_at?: string | null;
  created_at?: string | null;
}

export interface KnowledgeReviewItem {
  id: string;
  decision: string;
  reviewer: string;
  reason?: string | null;
  knowledge_version: number;
  created_at?: string | null;
}

export interface KnowledgeList {
  items: KnowledgeItem[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface KnowledgeVersionList {
  items: KnowledgeVersionItem[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface KnowledgeDetail {
  knowledge: KnowledgeItem;
  versions: KnowledgeVersionItem[];
  reviews: KnowledgeReviewItem[];
  related: KnowledgeItem[];
  experiences: ExperienceItem[];
}

export interface KnowledgeReviewPayload {
  decision: 'APPROVE' | 'REJECT' | 'REQUEST_MORE_EVIDENCE' | 'DEPRECATE';
  reviewer: string;
  reason?: string | null;
}

export interface ExperienceItem {
  id: string;
  project_id: string;
  incident_id?: string | null;
  environment_id?: string | null;
  primary_component_id?: string | null;
  remediation_action_id?: string | null;
  start_time: string;
  end_time: string;
  recovery_seconds?: number | null;
  outcome: string;
  data_quality: string;
  provenance: string;
  component_ids: string[];
  failure_signature: Record<string, unknown>;
  failure_label: string;
  failure_fingerprint: string;
  resolution_signature?: Record<string, unknown> | null;
  resolution_label?: string | null;
  learning_run_id?: string | null;
}

export interface ExperienceList {
  items: ExperienceItem[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface ExperienceTimelineEntry {
  stage: string;
  at?: string | null;
  detail?: string | null;
}

export interface ExperienceDetail {
  experience: ExperienceItem;
  failure_signature: Record<string, unknown>;
  resolution_signature?: Record<string, unknown> | null;
  incident?: { id: string; title: string; status: string; severity: string } | null;
  component?: { id: string; name: string; category?: string | null } | null;
  remediation?: {
    id: string;
    action_type: string;
    status: string;
    outcome?: string | null;
  } | null;
  timeline: ExperienceTimelineEntry[];
}

export type RecommendationTypeValue =
  | 'INVESTIGATE_COMPONENT'
  | 'INVESTIGATE_DEPENDENCY'
  | 'REVIEW_RECENT_CHANGE'
  | 'REVIEW_REMEDIATION'
  | 'RUN_REPRODUCTION'
  | 'CONSIDER_ROLLBACK'
  | 'CONSIDER_RESTART'
  | 'CONSIDER_TRAFFIC_SHIFT'
  | 'REVIEW_CAPACITY'
  | 'REVIEW_CONFIGURATION';

export interface RecommendationItem {
  id: string;
  recommendation_type: RecommendationTypeValue;
  status: string;
  title: string;
  rationale: string;
  confidence: KnowledgeConfidenceValue;
  component_id?: string | null;
  environment_id?: string | null;
  incident_id?: string | null;
  forecast_id?: string | null;
  knowledge_ids: string[];
  experience_ids: string[];
  current_evidence: Record<string, unknown>;
  limitations: string[];
  ranking?: Record<string, unknown> | null;
  historical?: Record<string, unknown> | null;
  policy_note?: string | null;
  decision?: Record<string, unknown> | null;
  outcome?: Record<string, unknown> | null;
  decided_by?: string | null;
  decided_at?: string | null;
  expires_at?: string | null;
  created_at?: string | null;
}

export interface RecommendationOutcomeItem {
  id: string;
  verdict: string;
  detail?: Record<string, unknown> | null;
  recorded_at: string;
  recorded_by?: string | null;
}

export interface RecommendationList {
  items: RecommendationItem[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface RecommendationDetail {
  recommendation: RecommendationItem;
  outcomes: RecommendationOutcomeItem[];
  knowledge: KnowledgeItem[];
  experiences: ExperienceItem[];
}

export interface RecommendationDecisionPayload {
  decision: 'ACCEPTED' | 'DISMISSED';
  actor: string;
  reason?: string | null;
}

export interface RecommendationOutcomePayload {
  verdict: 'EFFECTIVE' | 'INEFFECTIVE' | 'REGRESSION_CAUSING' | 'INCONCLUSIVE';
  recorded_by: string;
  detail?: Record<string, unknown> | null;
  remediation_action_id?: string | null;
}

/**
 * §23/§24. A relationship learned from history — never a declared dependency.
 *
 * `directed` is the field that matters most: an undirected observation must not
 * be drawn as an arrow, and `is_dependency` is present (and always false) so a
 * viewer cannot render structural meaning the payload does not carry.
 */
export type RelationshipKindValue =
  | 'FAILURE_PROPAGATION'
  | 'SHARED_FAILURE'
  | 'DEPENDENCY_DEGRADATION'
  | 'REMEDIATION_INFLUENCE';

export interface LearnedRelationshipItem {
  id: string;
  project_id: string;
  environment_id?: string | null;
  source_component_id: string;
  source_component_name: string;
  target_component_id: string;
  target_component_name: string;
  kind: RelationshipKindValue;
  directed: boolean;
  status: string;
  sample_count: number;
  supporting_count?: number | null;
  support_strength?: number | null;
  confidence: KnowledgeConfidenceValue;
  evidence: Array<Record<string, unknown>>;
  limitations: string[];
  provenance: string;
  algorithm: string;
  algorithm_version: string;
  feature_schema_version: string;
  coverage_start?: string | null;
  coverage_end?: string | null;
  first_seen_at?: string | null;
  last_seen_at?: string | null;
  learning_run_id?: string | null;
  is_dependency: false;
  disclaimer: string;
}

export interface LearnedRelationshipList {
  items: LearnedRelationshipItem[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  relationship_note: string;
  limitations: string[];
}

export interface ComponentLearningProfile {
  component: { id: string; name: string; category?: string | null };
  /** §23/§24. Learned history, kept visually separate from the structural graph. */
  relationships?: LearnedRelationshipItem[];
  profiles: Array<{
    id: string;
    component_id: string;
    window_days: number;
    computed_at: string;
    incident_count: number;
    anomaly_count: number;
    remediation_count: number;
    rollback_count: number;
    regression_count: number;
    mean_recovery_seconds?: number | null;
    forecast_outcome_count: number;
    forecast_true_positive_count: number;
    chronic_signal: boolean;
    chronic_reasons: string[];
    breakdown?: Record<string, unknown> | null;
  }>;
  knowledge: KnowledgeItem[];
}

export interface EffectivenessBucket {
  action_type: string;
  dimension: string;
  dimension_value?: string | null;
  comparable: number;
  successful: number;
  partially_successful: number;
  failed: number;
  rolled_back: number;
  unresolved: number;
  mean_recovery_seconds?: number | null;
  regression_count: number;
  success_ratio?: number | null;
  insufficient: boolean;
  minimum_samples: number;
  experience_ids: string[];
  limitations: string[];
}

export interface RemediationEffectiveness {
  buckets: EffectivenessBucket[];
  headline: string;
  observational_label: string;
  limitations: string[];
}

export interface ActionComparison {
  label: string;
  failure_pattern?: string | null;
  actions: Record<string, Record<string, unknown> & { headline?: string }>;
  verdict: string;
  summary: string;
  limitations: string[];
}

export interface SearchCitation {
  type: string;
  id: string;
  label?: string | null;
}

export interface KnowledgeSearchAnswer {
  question: string;
  intent: string;
  answer: string;
  evidence_available: boolean;
  citations: SearchCitation[];
  knowledge: Array<Record<string, unknown>>;
  experiences: Array<Record<string, unknown>>;
  effectiveness: Array<Record<string, unknown>>;
  limitations: string[];
  warnings: string[];
}

export interface LearningRunItem {
  id: string;
  project_id?: string | null;
  status: string;
  trigger: string;
  data_cutoff: string;
  last_processed_at?: string | null;
  started_at: string;
  completed_at?: string | null;
  events_processed: number;
  experiences_created: number;
  experiences_updated: number;
  patterns_discovered: number;
  patterns_validated: number;
  patterns_rejected: number;
  knowledge_activated: number;
  records_flagged: number;
  /** §23. Learned-relationship counts for this run. */
  relationships_created?: number;
  relationships_updated?: number;
  algorithm_versions: Record<string, unknown>;
  error_summary?: string | null;
}

export interface LearningRunList {
  items: LearningRunItem[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface LearningRunDetail {
  run: LearningRunItem;
  events: Array<{
    id: string;
    event_type: string;
    subject_id: string;
    occurred_at: string;
    processed_at?: string | null;
    unprocessable_reason?: string | null;
    provenance: string;
  }>;
}

export interface LearningRunSummary {
  run_id?: string | null;
  status: string;
  projects: string[];
  events_processed: number;
  experiences_created: number;
  experiences_updated: number;
  experiences_flagged: number;
  patterns_discovered: number;
  patterns_validated: number;
  patterns_rejected: number;
  knowledge_created: number;
  knowledge_updated: number;
  knowledge_activated: number;
  knowledge_deprecated: number;
  recommendations_created: number;
  recommendations_expired: number;
  /** §23. Reported so a run that learned nothing about relationships is visible. */
  relationships_created?: number;
  relationships_updated?: number;
  relationships_stale?: number;
  skipped_reasons: string[];
  unprocessable: Record<string, string>;
  errors: string[];
}

export interface LearningRunRequestPayload {
  project_id: string;
  trigger?: string;
  cutoff?: string | null;
  lookback_days?: number | null;
  generate_recommendations?: boolean;
}

export interface EventHook {
  project_id?: string | null;
  enabled_event_types: string[];
  trusted_provenance: string[];
  updated_by?: string | null;
}

export interface EventHookUpdatePayload {
  project_id?: string | null;
  enabled_event_types?: string[] | null;
  trusted_provenance?: string[] | null;
  updated_by?: string | null;
}

export interface IntelligenceSweep {
  projects_considered: number;
  projects_run: number;
  events_consumed: number;
  knowledge_deprecated: number;
  recommendations_expired: number;
  runs: Array<Record<string, unknown>>;
  paused: boolean;
  disabled: boolean;
  errors: string[];
}

export interface IntelligenceHealth {
  learning_enabled: boolean;
  sweep_enabled: boolean;
  auto_activation_enabled: boolean;
  include_ai_generated: boolean;
  pending_events: number;
  last_run_status?: string | null;
  last_run_at?: string | null;
  knowledge_stale_after_days: number;
  minimum_samples: Record<string, number>;
}

export interface IntelligenceDashboard {
  knowledge_by_status: Record<string, number>;
  knowledge_by_type: Record<string, number>;
  active_knowledge: number;
  validated_knowledge: number;
  candidate_patterns: number;
  stale_knowledge: number;
  rejected_patterns: number;
  recently_learned: KnowledgeItem[];
  experiences: number;
  open_recommendations: number;
  pending_events: number;
  chronic_components: number;
  last_run?: LearningRunItem | null;
}

export interface IntelligenceMetrics {
  learning_runs: number;
  learning_failures: number;
  events_total: number;
  events_pending: number;
  experiences: number;
  experiences_poor_quality: number;
  knowledge_validated_or_active: number;
  knowledge_candidates: number;
  knowledge_rejected: number;
  knowledge_stale: number;
  /** §80. The relationship stage is observable too. */
  relationships_active?: number;
  relationships_stale?: number;
  relationships_undirected?: number;
  pattern_validation_rate?: number | null;
  recommendations_by_status: Record<string, number>;
  recommendations_decided: number;
  recommendation_success_rate?: number | null;
}

// ---------------------------------------------------------------------------
// Phase 11 — unified reliability platform
//
// The response shapes mirror the backend §68–§71 schemas: a derived number
// always arrives with its provenance, and every list of facts carries the
// limitations that produced it.
// ---------------------------------------------------------------------------

export interface PlatformProjectCard {
  project_id: string;
  name: string;
  status: string;
  open_cases: number;
  open_data_quality_issues: number;
}

export interface PlatformErrorResponse {
  code: string;
  message: string;
  details?: Record<string, unknown> | null;
  request_id?: string | null;
  timestamp: string;
  trace_exposed: boolean;
}

// -- §2–§5 system state ------------------------------------------------------

export interface ComponentStateItem {
  id: string;
  name: string;
  component_type: string;
  environment_id?: string | null;
  state: string;
  state_reason?: string | null;
  state_evidence: Record<string, unknown>;
}

export interface DependencyItem {
  id: string;
  source_component_id: string;
  source_name?: string | null;
  target_component_id: string;
  target_name?: string | null;
  dependency_type: string;
}

export interface StateCounts {
  INCIDENT: number;
  RECOVERING: number;
  DEGRADED: number;
  AT_RISK: number;
  HEALTHY: number;
  UNKNOWN: number;
}

export interface SystemHealthSummary {
  components_total: number;
  components_with_evidence: number;
  components_unknown: number;
  state_counts: Record<string, number>;
  coverage_percent: number;
}

export interface SystemStateResponse {
  project_id: string;
  environment_id?: string | null;
  as_of: string;
  components: ComponentStateItem[];
  dependencies: DependencyItem[];
  health: SystemHealthSummary;
  state_counts: Record<string, number>;
  active_anomalies: Array<Record<string, unknown>>;
  active_incidents: Array<Record<string, unknown>>;
  predicted_risks: Array<Record<string, unknown>>;
  recent_changes: Array<Record<string, unknown>>;
  active_remediations: Array<Record<string, unknown>>;
  reliability_patterns: Array<Record<string, unknown>>;
  data_quality: Record<string, unknown>;
  limitations: string[];
}

export interface StateTransitionItem {
  id: string;
  component_id: string;
  previous_state?: string | null;
  new_state: string;
  trigger: string;
  reason?: string | null;
  evidence?: Record<string, unknown> | null;
  source: string;
  occurred_at: string;
}

export interface StateHistoryResponse {
  component_id: string;
  transitions: StateTransitionItem[];
  as_of_state?: string | null;
  note: string;
}

// -- §7, §8 context ----------------------------------------------------------

export interface ContextSnapshotResponse {
  id: string;
  project_id: string;
  scope: string;
  fingerprint: string;
  as_of: string;
  snapshot: Record<string, unknown>;
  created_by?: string | null;
  created_at: string;
}

export interface ContextResponse {
  references: Record<string, unknown>;
  description?: string | null;
  attributes: Record<string, unknown>;
  unavailable: Record<string, string>;
  fingerprint: string;
}

// -- §14–§16 cases -----------------------------------------------------------

export interface CaseSummary {
  id: string;
  reference: string;
  title: string;
  summary?: string | null;
  status: string;
  trigger: string;
  severity?: string | null;
  project_id: string;
  environment_id?: string | null;
  primary_component_id?: string | null;
  component_ids: string[];
  incident_id?: string | null;
  opened_at: string;
  closed_at?: string | null;
  opened_by?: string | null;
  duration_seconds?: number | null;
  timeline_entries: number;
  last_event_at?: string | null;
  allowed_transitions: string[];
}

export interface TimelineEntryItem {
  sequence: number;
  occurred_at: string;
  kind: string;
  event_type: string;
  title: string;
  detail?: string | null;
  component_id?: string | null;
  source: string;
  evidence?: Record<string, unknown> | null;
  actor?: string | null;
  system_action: boolean;
  result?: string | null;
}

export interface CaseListResponse {
  cases: CaseSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface CaseDetailResponse {
  case: CaseSummary;
  timeline: TimelineEntryItem[];
  evidence: Record<string, unknown>;
  workflows: Array<Record<string, unknown>>;
  state: Record<string, unknown>;
  notes: string[];
}

export interface CaseStatusChangeRequest {
  status: string;
  reason?: string;
  actor?: string;
}

// -- §27, §28 case assistant -------------------------------------------------

export interface AssistantQuestionRequest {
  question: string;
  include_evidence?: boolean;
}

export interface CitationItem {
  kind: string;
  source: string;
  row_id: string;
  label: string;
  detail?: string | null;
}

export interface AssistantAnswerResponse {
  question: string;
  intent: string;
  answer: string;
  confidence: number;
  confidence_reason: string;
  citations: CitationItem[];
  unknowns: string[];
  narrator?: string | null;
  grounding: Record<string, unknown>;
  evidence?: Record<string, unknown> | null;
}

// -- §19–§25 dashboard -------------------------------------------------------

export interface OverviewResponse {
  project_id: string;
  as_of: string;
  executive_summary: Record<string, unknown>;
  health: Record<string, unknown>;
  state_counts: Record<string, number>;
  active_incidents: Array<Record<string, unknown>>;
  predicted_risks: Array<Record<string, unknown>>;
  active_remediations: Array<Record<string, unknown>>;
  recent_changes: Array<Record<string, unknown>>;
  top_risky_components: Array<Record<string, unknown>>;
  recent_recoveries: Array<Record<string, unknown>>;
  learning_insights: Record<string, unknown>;
  argus_health: Record<string, unknown>;
  data_quality: Record<string, unknown>;
  open_cases: Array<Record<string, unknown>>;
  slo: Record<string, unknown>;
  limitations: string[];
}

export interface ActivityItem {
  id: string;
  event_type: string;
  title: string;
  source: string;
  occurred_at: string;
  subject_type?: string | null;
  subject_id?: string | null;
  component_id?: string | null;
  case_id?: string | null;
  correlation_id?: string | null;
  link?: string | null;
  payload?: Record<string, unknown> | null;
  processed: boolean;
}

export interface ActivityResponse {
  items: ActivityItem[];
  limit: number;
  offset: number;
}

export interface StoryResponse {
  correlation_id: string;
  events: ActivityItem[];
  stage_count: number;
  note: string;
}

// -- §30, §31 service catalog ------------------------------------------------

export interface CatalogEntryResponse {
  component_id: string;
  name: string;
  component_type: string;
  environment_id?: string | null;
  environment_name?: string | null;
  owner: Record<string, unknown>;
  dependencies: Array<Record<string, unknown>>;
  dependents: Array<Record<string, unknown>>;
  endpoints: Array<Record<string, unknown>>;
  state: string;
  state_reason?: string | null;
  available?: boolean | null;
  metrics: Record<string, unknown>;
  risk: Record<string, unknown>;
  incident_history: Array<Record<string, unknown>>;
  deployment_history: Array<Record<string, unknown>>;
  remediation_history: Array<Record<string, unknown>>;
  reliability_profile?: Record<string, unknown> | null;
  scorecard?: Record<string, unknown> | null;
  unavailable: Record<string, string>;
  limitations: string[];
}

export interface CatalogListResponse {
  services: CatalogEntryResponse[];
  total: number;
}

export interface OwnershipRequest {
  team: string;
  owner_name?: string;
  contact_email?: string;
  repository_owner?: string;
  on_call?: string;
  documentation_url?: string;
  actor?: string;
}

// -- §32–§35 SLOs ------------------------------------------------------------

export interface SloItem {
  slo_id: string;
  name: string;
  indicator: string;
  target: number;
  comparison: string;
  unit?: string | null;
  component_id?: string | null;
  enabled: boolean;
  status: string;
  reading?: number | null;
  burn_rate?: number | null;
  burn_state?: string | null;
  remaining_percent?: number | null;
  computed_at?: string | null;
  never_evaluated: boolean;
}

export interface SloOverviewResponse {
  as_of: string;
  objectives_total: number;
  by_status: Record<string, number>;
  objectives: SloItem[];
  limitations: string[];
}

export interface SloEvaluationResponse {
  slo_id: string;
  name: string;
  indicator: string;
  window_start: string;
  window_end: string;
  status: string;
  reading?: number | null;
  target: number;
  comparison: string;
  sample_count: number;
  allowed_failure?: number | null;
  observed_failure?: number | null;
  remaining?: number | null;
  remaining_percent?: number | null;
  burn_rate?: number | null;
  burn_state: string;
  compliance_percent?: number | null;
  data_quality: string;
  evidence: Record<string, unknown>;
  limitations: string[];
}

export interface SloCreateRequest {
  name: string;
  indicator: string;
  target: number;
  comparison?: string;
  metric_name?: string;
  component_id?: string;
  environment_id?: string;
  window_seconds?: number;
  unit?: string;
  description?: string;
  actor?: string;
}

export interface ErrorBudgetResponse {
  slo_id: string;
  name: string;
  latest?: Record<string, unknown> | null;
  history: Array<Record<string, unknown>>;
  definition: string;
}

// -- §38–§41, §84 change intelligence ----------------------------------------

export interface ChangeListResponse {
  changes: Array<Record<string, unknown>>;
  note: string;
}

export interface EnvironmentComparisonResponse {
  project_id: string;
  left: Record<string, unknown>;
  right: Record<string, unknown>;
  differences: Array<Record<string, unknown>>;
  notes: string[];
}

export interface ChangeFailureRateResponse {
  window_days: number;
  deployments_total: number;
  deployments_succeeded: number;
  deployments_failed: number;
  deployments_rolled_back: number;
  deployments_with_incident: number;
  failure_rate?: number | null;
  methodology: string;
  limitations: string[];
}

// -- §57–§60, §105–§107 platform health --------------------------------------

export interface SubsystemHealthItem {
  name: string;
  status: string;
  required: boolean;
  optional: boolean;
  latency_ms?: number | null;
  detail?: string | null;
  last_success_at?: string | null;
  last_failure_at?: string | null;
  queue_depth?: number | null;
  error_rate?: number | null;
  metrics: Record<string, unknown>;
}

export interface PlatformHealthResponse {
  as_of: string;
  status: string;
  ready: boolean;
  subsystems: SubsystemHealthItem[];
  degraded_capabilities: string[];
  notes: string[];
  summary: Record<string, number>;
}

export interface ReadinessResponse {
  ready: boolean;
  required_subsystems: SubsystemHealthItem[];
  optional_subsystems: SubsystemHealthItem[];
  reason: string;
}

export interface DependencyHealthResponse {
  as_of: string;
  dependencies: SubsystemHealthItem[];
  required_count: number;
  optional_count: number;
  graceful_degradation: Record<string, string>;
}

// -- §87–§90 data quality ----------------------------------------------------

export interface DataQualityIssueItem {
  id: string;
  kind: string;
  severity: string;
  status: string;
  subject_type: string;
  subject_id: string;
  component_id?: string | null;
  title: string;
  detail?: string | null;
  evidence?: Record<string, unknown> | null;
  suggestion?: string | null;
  detected_at: string;
  last_seen_at: string;
  occurrence_count: number;
}

export interface DataQualityResponse {
  summary: Record<string, unknown>;
  issues: DataQualityIssueItem[];
  descriptions: Record<string, string>;
}

export interface DataQualityStatusRequest {
  status: string;
  actor?: string;
}

/**
 * The result of running the §87/§88 consistency checks. It reports *findings*
 * (what the checks saw) separately from *opened/updated/resolved* (what the
 * persistence step did with them), because a read-only run has findings and no
 * persistence counts.
 */
export interface DataQualityCheckResponse {
  checked: number;
  findings: number;
  by_kind: Record<string, number>;
  opened: number;
  updated: number;
  resolved: number;
  errors: string[];
}

// -- §91–§94 configuration ---------------------------------------------------

export interface ConfigurationVersionItem {
  id: string;
  scope: string;
  scope_id?: string | null;
  version: number;
  settings: Record<string, unknown>;
  redacted_fields: string[];
  previous_version?: number | null;
  change_summary?: string | null;
  changed_by?: string | null;
  reason?: string | null;
  rolled_back_from?: number | null;
  created_at: string;
}

export interface ConfigurationResponse {
  project_id: string;
  sections: Record<string, unknown>;
  overrides: Record<string, unknown>;
  redacted_fields: string[];
  notes: string[];
  versions: ConfigurationVersionItem[];
}

export interface ConfigurationUpdateRequest {
  scope: string;
  settings: Record<string, unknown>;
  scope_id?: string;
  change_summary?: string;
  reason?: string;
  actor?: string;
}

export interface ConfigurationRollbackRequest {
  scope: string;
  target_version: number;
  scope_id?: string;
  actor?: string;
  reason?: string;
}

export interface FeatureFlagsResponse {
  flags: Record<string, boolean>;
  reasons: Record<string, string>;
  defaults: string;
}

// -- §53–§56 notifications ---------------------------------------------------

export interface NotificationItem {
  id: string;
  kind: string;
  severity: string;
  status: string;
  title: string;
  body?: string | null;
  source: string;
  subject_type?: string | null;
  subject_id?: string | null;
  case_id?: string | null;
  link?: string | null;
  evidence?: Record<string, unknown> | null;
  occurrence_count: number;
  channels_attempted?: string[] | null;
  delivery?: Record<string, unknown> | null;
  delivered_at?: string | null;
  read_at?: string | null;
  acknowledged_by?: string | null;
  created_at: string;
}

export interface NotificationListResponse {
  notifications: NotificationItem[];
  summary: Record<string, unknown>;
}

export interface NotificationAckRequest {
  actor?: string;
  acknowledge?: boolean;
}

// -- §17, §18 search ---------------------------------------------------------

export interface SearchHitItem {
  kind: string;
  id: string;
  title: string;
  subtitle?: string | null;
  status?: string | null;
  occurred_at?: string | null;
  component_id?: string | null;
  route?: string | null;
  matched_field?: string | null;
  metadata: Record<string, unknown>;
}

export interface SearchResponse {
  query: string;
  total: number;
  by_kind: Record<string, number>;
  results: Record<string, SearchHitItem[]>;
  filters: Record<string, unknown>;
  notes: string[];
}

export interface SearchHelpResponse {
  kinds: string[];
  examples: string[];
  filters: Record<string, string>;
  notes: string[];
}

// -- §37, §77–§85 reports ----------------------------------------------------

export interface ReportResponse {
  kind: string;
  window_days: number;
  project_id: string;
  environment_id?: string | null;
  generated_at: string;
  sections: Record<string, unknown>;
  limitations: string[];
}

export interface PostmortemResponse {
  incident_id: string;
  title: string;
  generated_at: string;
  sections: Record<string, unknown>;
  narrative?: string | null;
  narrative_provider?: string | null;
  narrative_unavailable_reason?: string | null;
  unknowns: string[];
  follow_up_actions: Array<Record<string, unknown>>;
  note: string;
}

export interface ImprovementPlanResponse {
  window_days: number;
  items: Array<Record<string, unknown>>;
  criteria: string;
  note: string;
  truncated: boolean;
}

// -- §51–§53 integrations ----------------------------------------------------

export interface IntegrationRegistryResponse {
  providers: Record<string, unknown>;
  note: string;
}

export interface WebhookRequirementsResponse {
  headers: Record<string, string>;
  requirements: string[];
  rejections: string[];
}

export interface WebhookReceiptResponse {
  accepted: boolean;
  event_type?: string | null;
  delivery_id?: string | null;
  fingerprint?: string | null;
  idempotent_replay: boolean;
  reason?: string | null;
  code?: string | null;
  payload_summary: Record<string, unknown>;
}

// §73–§76, §85 engineering metrics return a free-form dict on the backend; the
// fields the UI reads are typed here rather than the whole shape being invented.
export interface PlatformMetricsResponse {
  mttd?: Record<string, unknown>;
  mttr?: Record<string, unknown>;
  workflows?: Record<string, unknown>;
  trends?: Record<string, unknown>;
  change_failure?: Record<string, unknown>;
  scorecards?: Array<Record<string, unknown>>;
  limitations?: string[];
  [key: string]: unknown;
}
