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
// Incidents (Phase 0 CRUD + Phase 3 incident intelligence)
// ---------------------------------------------------------------------------

// These mirror the backend enums exactly (`app/models/incident.py`). They were
// previously a divergent invented set (critical/major/minor/low, detected/
// in_progress); reconciling them is what lets the UI render real incidents and
// offer only transitions the API accepts.
export type IncidentSeverity = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
export type IncidentStatus =
  | 'OPEN'
  | 'ACKNOWLEDGED'
  | 'INVESTIGATING'
  | 'MITIGATED'
  | 'RESOLVED'
  | 'CLOSED';

export interface Incident {
  id: string;
  project_id: string;
  environment_id?: string | null;
  title: string;
  description?: string | null;
  severity: IncidentSeverity;
  status: IncidentStatus;
  detected_at: string;
  started_at?: string | null;
  resolved_at?: string | null;
  fingerprint?: string | null;
  summary?: string | null;
  primary_component_id?: string | null;
  correlation_rationale?: Record<string, unknown> | null;
  acknowledged_at?: string | null;
  status_changed_by?: string | null;
  metadata?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export type EvidenceType =
  | 'LOG'
  | 'METRIC'
  | 'TRACE'
  | 'SPAN'
  | 'DEPLOYMENT'
  | 'CONFIGURATION_CHANGE'
  | 'HEALTH_CHECK'
  | 'GRAPH'
  | 'ANOMALY'
  | 'CODE_CHANGE'
  | 'DEPENDENCY_CHANGE'
  | 'CUSTOM';

export interface Evidence {
  id: string;
  incident_id: string;
  evidence_type: EvidenceType | string;
  source_id: string;
  timestamp: string;
  relevance_score?: number | null;
  description?: string | null;
  component_id?: string | null;
  observed_value?: string | null;
  expected_value?: string | null;
  severity?: IncidentSeverity | null;
  confidence?: number | null;
  provenance?: string | null;
  // Why this evidence is *relevant* — never a causal claim.
  relevance_reason?: string | null;
  anomaly_id?: string | null;
  metadata?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export type TimelineEventType =
  | 'INCIDENT_CREATED'
  | 'ANOMALY_DETECTED'
  | 'ANOMALY_UPDATED'
  | 'COMPONENT_AFFECTED'
  | 'DEPLOYMENT_OCCURRED'
  | 'CONFIGURATION_CHANGED'
  | 'HEALTH_CHANGED'
  | 'TRACE_FAILURE'
  | 'LOG_PATTERN_SPIKE'
  | 'EVIDENCE_ADDED'
  | 'NOTE'
  | 'INCIDENT_STATUS_CHANGED'
  | 'INCIDENT_ACKNOWLEDGED'
  | 'INCIDENT_MITIGATED'
  | 'INCIDENT_RESOLVED';

export interface TimelineEvent {
  id: string;
  incident_id: string;
  project_id: string;
  environment_id?: string | null;
  event_type: TimelineEventType;
  occurred_at: string;
  title: string;
  description?: string | null;
  component_id?: string | null;
  anomaly_id?: string | null;
  // True when the entry is temporal context, not an observed fact (§31).
  is_context_only: boolean;
  provenance?: string | null;
  created_at: string;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// Anomalies (Phase 3)
// ---------------------------------------------------------------------------

export type AnomalySeverity = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
export type AnomalyStatus =
  | 'DETECTED'
  | 'ACKNOWLEDGED'
  | 'INVESTIGATING'
  | 'RESOLVED'
  | 'EXPIRED';
export type AnomalyType =
  | 'METRIC_THRESHOLD'
  | 'METRIC_BASELINE_DEVIATION'
  | 'ERROR_RATE_SPIKE'
  | 'LATENCY_SPIKE'
  | 'THROUGHPUT_DROP'
  | 'LOG_PATTERN_SPIKE'
  | 'TRACE_FAILURE_SPIKE'
  | 'HEALTH_DEGRADATION'
  | 'REQUEST_RATE_CHANGE'
  | 'RESOURCE_USAGE_SPIKE'
  | 'DEPLOYMENT_RELATED_CHANGE'
  | 'CONFIGURATION_RELATED_CHANGE';
export type AnomalySource =
  | 'METRIC'
  | 'LOG'
  | 'TRACE'
  | 'SPAN'
  | 'HEALTH_CHECK'
  | 'DEPLOYMENT'
  | 'CONFIGURATION'
  | 'GRAPH'
  | 'COMPOSITE'
  | 'UNKNOWN';
export type BaselineStrategy = 'STATIC' | 'ROLLING';
export type RuleCondition =
  | 'THRESHOLD'
  | 'BASELINE_DEVIATION'
  | 'Z_SCORE'
  | 'RATE_CHANGE'
  | 'ERROR_RATE'
  | 'LATENCY_RATIO'
  | 'PATTERN_SPIKE'
  | 'HEALTH_TRANSITION'
  | 'TRACE_FAILURE_RATE';

export interface Anomaly {
  id: string;
  project_id: string;
  environment_id?: string | null;
  component_id?: string | null;
  rule_id?: string | null;
  incident_id?: string | null;
  anomaly_type: AnomalyType;
  severity: AnomalySeverity;
  status: AnomalyStatus;
  source: AnomalySource;
  metric_name?: string | null;
  pattern_template?: string | null;
  observed_value?: number | null;
  expected_value?: number | null;
  deviation?: number | null;
  threshold?: number | null;
  z_score?: number | null;
  confidence?: number | null;
  fingerprint: string;
  description?: string | null;
  source_event_id?: string | null;
  observation_count: number;
  detected_at: string;
  started_at?: string | null;
  ended_at?: string | null;
  last_seen_at?: string | null;
  acknowledged_at?: string | null;
  resolved_at?: string | null;
  status_changed_by?: string | null;
  suppressed: boolean;
  suppression_reason?: string | null;
  suppressed_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AnomalyObservation {
  id: string;
  anomaly_id: string;
  project_id: string;
  environment_id?: string | null;
  component_id?: string | null;
  observed_at: string;
  observed_value?: number | null;
  expected_value?: number | null;
  deviation?: number | null;
  z_score?: number | null;
  sample_count?: number | null;
  source_event_id?: string | null;
  payload_summary?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

/** Deterministic "why was this detected" block (§52). */
export interface AnomalyExplanation {
  anomaly_id: string;
  why_detected: string;
  detector: string;
  condition?: string | null;
  baseline_strategy?: string | null;
  baseline_sample_count?: number | null;
  baseline_window_seconds?: number | null;
  observed_value?: number | null;
  expected_value?: number | null;
  deviation?: number | null;
  z_score?: number | null;
  threshold_exceeded?: string | null;
  metric_name?: string | null;
  pattern_template?: string | null;
  component_id?: string | null;
  environment_id?: string | null;
  telemetry_source?: string | null;
  source_event_id?: string | null;
  fingerprint_material: string;
  confidence_meaning: string;
}

export interface AnomalyDetail extends Anomaly {
  observations: AnomalyObservation[];
  explanation?: AnomalyExplanation | null;
}

export interface AnomalyRule {
  id: string;
  project_id: string;
  environment_id?: string | null;
  component_id?: string | null;
  name: string;
  description?: string | null;
  anomaly_type: AnomalyType;
  condition: RuleCondition;
  metric_name?: string | null;
  baseline_strategy: BaselineStrategy;
  expected_value?: number | null;
  threshold?: number | null;
  multiplier?: number | null;
  z_threshold?: number | null;
  min_samples: number;
  window_seconds: number;
  cooldown_seconds: number;
  persistence_cycles: number;
  severity: AnomalySeverity;
  enabled: boolean;
  created_by?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AnomalyRuleCreate {
  project_id: string;
  environment_id?: string | null;
  component_id?: string | null;
  name: string;
  description?: string | null;
  anomaly_type: AnomalyType;
  condition: RuleCondition;
  metric_name?: string | null;
  baseline_strategy?: BaselineStrategy;
  expected_value?: number | null;
  threshold?: number | null;
  multiplier?: number | null;
  z_threshold?: number | null;
  min_samples?: number;
  window_seconds?: number;
  cooldown_seconds?: number;
  persistence_cycles?: number;
  severity: AnomalySeverity;
  enabled?: boolean;
  created_by?: string | null;
}

export interface AffectedComponent {
  component_id: string;
  name?: string | null;
  classification:
    | 'DIRECTLY_OBSERVED'
    | 'UPSTREAM_CONTEXT'
    | 'DOWNSTREAM_CONTEXT'
    | 'DEPENDENCY_CONTEXT';
  reason?: string | null;
  anomaly_count?: number;
  severity?: AnomalySeverity | null;
}

export interface IncidentGraphNode {
  node_id: string;
  name: string;
  node_type: string;
  classification: string;
}

export interface IncidentGraphEdge {
  source_node_id: string;
  target_node_id: string;
  edge_type: string;
  source?: string | null;
}

export interface IncidentGraphContext {
  nodes: IncidentGraphNode[];
  edges: IncidentGraphEdge[];
  disclaimer: string;
}

export interface DeploymentContextItem {
  deployment_event_id: string;
  deployment_id: string;
  component_id?: string | null;
  version?: string | null;
  deployed_at: string;
  status?: string | null;
  seconds_before_first_anomaly?: number | null;
  is_context_only: boolean;
}

export interface ConfigurationContextItem {
  configuration_event_id: string;
  component_id?: string | null;
  changed_at: string;
  summary?: string | null;
  actor?: string | null;
  is_context_only: boolean;
}

export interface IncidentSummary {
  incident_id: string;
  title: string;
  severity: IncidentSeverity;
  status: IncidentStatus;
  started_at?: string | null;
  detected_at: string;
  resolved_at?: string | null;
  text: string;
  generated_from: string[];
}

export interface ReliabilityMetrics {
  project_id?: string | null;
  environment_id?: string | null;
  window_seconds: number;
  generated_at: string;
  anomalies_detected: number;
  anomalies_open: number;
  anomalies_resolved: number;
  anomalies_suppressed: number;
  anomalies_deduplicated: number;
  anomalies_by_severity: Record<string, number>;
  anomalies_by_type: Record<string, number>;
  incidents_created: number;
  incidents_open: number;
  incidents_resolved: number;
  incidents_by_severity: Record<string, number>;
  mtta_seconds?: number | null;
  mttr_seconds?: number | null;
  mttr_definition: string;
  mtta_definition: string;
}

export interface TimeBucket {
  bucket_start: string;
  count: number;
}

export interface IncidentDashboard {
  metrics: ReliabilityMetrics;
  anomalies_over_time: TimeBucket[];
  incidents_over_time: TimeBucket[];
  severity_distribution: Record<string, number>;
  anomaly_categories: Record<string, number>;
  top_affected_components: {
    component_id: string;
    name?: string | null;
    anomaly_count: number;
    max_severity: string;
  }[];
  recent_incidents: {
    id: string;
    title: string;
    severity: string;
    status: string;
    detected_at: string;
    primary_component_id?: string | null;
  }[];
  recent_anomalies: {
    id: string;
    anomaly_type: string;
    severity: string;
    status: string;
    component_id?: string | null;
    detected_at: string;
    suppressed: boolean;
  }[];
  window_seconds: number;
  bucket_seconds: number;
  generated_at: string;
}

// ---------------------------------------------------------------------------
// Causal analysis (Phase 4)
// ---------------------------------------------------------------------------

/**
 * Confidence buckets, not probabilities. The backend never returns a
 * percentage, so neither does the UI — `INSUFFICIENT` is a real answer.
 */
export type ConfidenceLevel = 'HIGH' | 'MEDIUM' | 'LOW' | 'INSUFFICIENT';

export type CandidateType =
  | 'DEPLOYMENT'
  | 'CONFIGURATION_CHANGE'
  | 'APPLICATION_COMPONENT'
  | 'DATABASE'
  | 'EXTERNAL_DEPENDENCY'
  | 'INFRASTRUCTURE'
  | 'RESOURCE_EXHAUSTION'
  | 'DEPENDENCY_FAILURE'
  | 'DATA_ISSUE'
  | 'UNKNOWN';

export type CandidateStatus =
  | 'SUPPORTED'
  | 'WEAKENED'
  | 'WITHDRAWN'
  | 'UNDER_EVALUATION';

export type CausalRelationshipType =
  | 'POSSIBLE_CAUSE'
  | 'LIKELY_CAUSE'
  | 'DOWNSTREAM_EFFECT'
  | 'CONTRIBUTES_TO'
  | 'BLOCKS'
  | 'TRIGGERS'
  | 'AMPLIFIES'
  | 'CORRELATES_WITH';

export type CausalEvidenceCategory =
  | 'TEMPORAL'
  | 'TRACE'
  | 'DEPENDENCY'
  | 'CHANGE'
  | 'METRIC'
  | 'LOG'
  | 'HEALTH'
  | 'RESOURCE'
  | 'CONFIGURATION'
  | 'DEPLOYMENT'
  | 'RECOVERY'
  | 'CONTRADICTING';

export type EvidencePolarity = 'SUPPORTING' | 'CONTRADICTING' | 'NEUTRAL';

export type AnalysisStatus = 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED';

export interface CausalEvidence {
  id: string;
  analysis_id: string;
  candidate_id?: string | null;
  relationship_id?: string | null;
  category: CausalEvidenceCategory;
  polarity: EvidencePolarity;
  source_table: string;
  source_id?: string | null;
  incident_evidence_id?: string | null;
  quote: string;
  explanation: string;
  component_id?: string | null;
  observed_at?: string | null;
  strength: number;
  created_at: string;
  updated_at: string;
}

/** Documented score components — the UI never shows a bare number (§25). */
export interface ScoreBreakdown {
  total: number;
  trace: number;
  change: number;
  recovery: number;
  resource: number;
  temporal: number;
  dependency: number;
  propagation: number;
  contradiction_penalty: number;
  outgoing_edge_support: number;
}

export interface RootCauseCandidate {
  id: string;
  analysis_id: string;
  component_id?: string | null;
  component_name?: string | null;
  event_id?: string | null;
  event_kind?: string | null;
  candidate_type: CandidateType;
  status: CandidateStatus;
  score: number;
  confidence: ConfidenceLevel;
  is_external: boolean;
  first_observed_at?: string | null;
  supporting_evidence_count: number;
  contradicting_evidence_count: number;
  neutral_evidence_count: number;
  explanation?: string | null;
  score_breakdown?: ScoreBreakdown | null;
  reasons?: string[] | null;
  uncertainty?: { missing?: string[]; caveats?: string[] } | null;
  created_at: string;
  updated_at: string;
}

export interface CausalRelationship {
  id: string;
  analysis_id: string;
  source_candidate_id: string;
  target_candidate_id: string;
  relationship_type: CausalRelationshipType;
  confidence: ConfidenceLevel;
  supporting_evidence_count: number;
  contradicting_evidence_count: number;
  temporal_alignment_seconds?: number | null;
  structural_support: number;
  observational_support: number;
  contradiction_notes?: string[] | null;
  explanation: string;
  created_at: string;
  updated_at: string;
}

export interface CausalAnalysis {
  id: string;
  project_id: string;
  environment_id?: string | null;
  incident_id: string;
  analysis_version: number;
  status: AnalysisStatus;
  trigger?: string | null;
  requested_by?: string | null;
  analysis_version_tag?: string | null;
  started_at: string;
  completed_at?: string | null;
  overall_confidence: ConfidenceLevel;
  primary_candidate_id?: string | null;
  summary?: string | null;
  missing_evidence?: string[] | null;
  analysis_metadata?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface CausalAnalysisDetail extends CausalAnalysis {
  candidates: RootCauseCandidate[];
  relationships: CausalRelationship[];
  evidence: CausalEvidence[];
}

export interface CausalGraph {
  analysis_id: string;
  analysis_version: number;
  overall_confidence: ConfidenceLevel;
  primary_candidate_id?: string | null;
  nodes: RootCauseCandidate[];
  edges: CausalRelationship[];
  disclaimer: string;
}

export interface CausalChainLink {
  source_candidate_id: string;
  target_candidate_id: string;
  relationship_type: CausalRelationshipType;
  confidence: ConfidenceLevel;
  temporal_alignment_seconds?: number | null;
  evidence_count: number;
  explanation: string;
}

export interface CausalChain {
  analysis_id: string;
  chain: CausalChainLink[];
  candidate_ids: string[];
  valid: boolean;
  validation_notes: string[];
}

export interface Hypothesis {
  candidate: RootCauseCandidate;
  supporting: CausalEvidence[];
  contradicting: CausalEvidence[];
  neutral: CausalEvidence[];
  why_confidence_differs?: string | null;
}

export interface Hypotheses {
  analysis_id: string;
  analysis_version: number;
  primary_candidate_id?: string | null;
  overall_confidence: ConfidenceLevel;
  items: Hypothesis[];
}

export interface EvidenceAnalysis {
  analysis_id: string;
  candidates: Hypothesis[];
  missing_evidence?: string[] | null;
}

export interface AnalyzeResult {
  analysis_id: string;
  incident_id: string;
  analysis_version: number;
  status: AnalysisStatus;
  reused: boolean;
}

export interface AnalysisHistoryItem {
  analysis_id: string;
  analysis_version: number;
  status: AnalysisStatus;
  overall_confidence: ConfidenceLevel;
  primary_candidate_id?: string | null;
  primary_candidate_summary?: string | null;
  candidate_count: number;
  supporting_evidence_count: number;
  contradicting_evidence_count: number;
  started_at: string;
  completed_at?: string | null;
  trigger?: string | null;
  requested_by?: string | null;
  diff?: {
    primary_changed: boolean;
    previous_primary_candidate_id?: string | null;
    confidence_changed: boolean;
    previous_confidence: string;
    candidate_count_delta: number;
    evidence_count: number;
    new_contradictions: number;
    relationship_count: number;
  } | null;
}

export interface AnalysisHistory {
  items: AnalysisHistoryItem[];
  total: number;
}

/** Structured explanation (§37) — reasons, not a hidden reasoning trace. */
export interface CandidateExplanation {
  candidate_id: string;
  candidate_type: string;
  label?: string | null;
  confidence: string;
  score: number;
  supporting_evidence: number;
  contradicting_evidence: number;
  neutral_evidence: number;
  reasons: string[];
  supporting_quotes: string[];
  contradicting_quotes: string[];
  score_breakdown: Partial<ScoreBreakdown>;
  uncertainty: { missing?: string[]; caveats?: string[] };
  why_this_confidence: string;
  limitations: string[];
}

export interface AnalysisExplanation {
  analysis_id: string;
  analysis_version: number;
  overall_confidence: string;
  primary_candidate_id?: string | null;
  headline: string;
  narrative: string[];
  primary?: CandidateExplanation | null;
  alternatives: CandidateExplanation[];
  limitations: string[];
  missing_evidence: string[];
  disclaimer: string;
  status?: string;
}

/** Why ARGUS believes one specific edge exists (§41). */
export interface RelationshipExplanation {
  relationship_id: string;
  source_candidate_id: string;
  target_candidate_id: string;
  relationship_type: CausalRelationshipType;
  directional: boolean;
  confidence: string;
  temporal_alignment_seconds?: number | null;
  structural_support: number;
  observational_support: number;
  evidence_quotes: string[];
  explanation: string;
  caveats: string[];
  analysis_id?: string;
  analysis_version?: number;
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
/**
 * Build a `?project_id=` scope query string.
 *
 * The backend validates ownership when a scope is supplied, which is how the
 * single-tenant Phase 3 API enforces project isolation (§46).
 */
export function scopeQuery(projectId?: string): string {
  return projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
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