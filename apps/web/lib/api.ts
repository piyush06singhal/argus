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
// Failure reproduction (Phase 5)
// ---------------------------------------------------------------------------

/**
 * Lifecycle of one experiment (§6, §37). The happy path is ordered; the last
 * three are terminal exits.
 */
export type ExperimentStatus =
  | 'PLANNED'
  | 'VALIDATING'
  | 'PROVISIONING'
  | 'READY'
  | 'REPLAYING'
  | 'RUNNING'
  | 'COLLECTING'
  | 'COMPARING'
  | 'COMPLETED'
  | 'FAILED'
  | 'CANCELLED'
  | 'TIMED_OUT';

/**
 * What the sandbox *observed*. `FAILED` means the expected failure did not
 * appear in the sandbox — a statement about the sandbox, not the hypothesis.
 */
export type ReproductionResult =
  | 'SUCCESSFUL'
  | 'PARTIAL'
  | 'FAILED'
  | 'INCONCLUSIVE'
  | 'NOT_RUN';

export type ReproductionStrategy =
  | 'SYNTHETIC_INPUT_REPLAY'
  | 'EVENT_REPLAY'
  | 'DEPENDENCY_FAULT'
  | 'CONFIGURATION_REPLAY'
  | 'STATE_SNAPSHOT';

export type SandboxBackendKind = 'LOCAL_PROCESS' | 'DOCKER';
export type SandboxStatus =
  | 'CREATING'
  | 'READY'
  | 'STOPPING'
  | 'STOPPED'
  | 'DESTROYED'
  | 'FAILED';
export type SandboxNetworkPolicy =
  | 'ISOLATED'
  | 'MOCK_DEPENDENCIES'
  | 'CONTROLLED_EGRESS';
export type RunStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'COMPLETED'
  | 'FAILED'
  | 'TIMED_OUT'
  | 'CANCELLED';
export type FailureClass =
  | 'ENVIRONMENT_ERROR'
  | 'INPUT_ERROR'
  | 'TIMEOUT'
  | 'RESOURCE_LIMIT'
  | 'DEPENDENCY_UNAVAILABLE'
  | 'SANDBOX_ERROR'
  | 'APPLICATION_FAILURE'
  | 'NO_FAILURE_OBSERVED'
  | 'INSUFFICIENT_TELEMETRY'
  | 'UNKNOWN';
export type ReplayInputSource =
  | 'HTTP_REQUEST'
  | 'EVENT'
  | 'MESSAGE'
  | 'TRACE_INPUT'
  | 'SYNTHETIC';
export type ReplayStatus =
  | 'PENDING'
  | 'SENT'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'REJECTED'
  | 'SKIPPED';
export type ReplayMode =
  | 'SEQUENTIAL'
  | 'PARALLEL'
  | 'TIMED'
  | 'BURST'
  | 'RATE_LIMITED';
export type FaultType =
  | 'LATENCY'
  | 'TIMEOUT'
  | 'HTTP_4XX'
  | 'HTTP_5XX'
  | 'CONNECTION_FAILURE'
  | 'RESPONSE_CORRUPTION'
  | 'RESOURCE_PRESSURE'
  | 'DEPENDENCY_UNAVAILABLE';
export type FaultTrigger =
  | 'IMMEDIATE'
  | 'AFTER_REPLAY_INDEX'
  | 'AT_OFFSET';
export type FaultStatus =
  | 'PLANNED'
  | 'ACTIVE'
  | 'COMPLETED'
  | 'FAILED'
  | 'SKIPPED';
export type ObservationSignal =
  | 'SPAN'
  | 'TRACE'
  | 'LOG'
  | 'METRIC'
  | 'HEALTH'
  | 'EVENT'
  | 'DEPLOYMENT'
  | 'CONFIGURATION';
export type ObservationStatus =
  | 'EXPECTED'
  | 'UNEXPECTED'
  | 'NEUTRAL'
  | 'MISSING';
export type ValidationOutcome =
  | 'SUPPORTED'
  | 'PARTIALLY_SUPPORTED'
  | 'NOT_SUPPORTED'
  | 'INCONCLUSIVE';
export type ArtifactType =
  | 'ENVIRONMENT_SNAPSHOT'
  | 'REPRODUCTION_PLAN'
  | 'REPRODUCTION_MANIFEST'
  | 'REPLAY_MANIFEST'
  | 'TELEMETRY_SNAPSHOT'
  | 'LOGS'
  | 'TRACE_SUMMARY'
  | 'COMPARISON_RESULT'
  | 'SANDBOX_METADATA'
  | 'FAULT_RECORD'
  | 'VALIDATION_REPORT'
  | 'PROCESS_OUTPUT';
export type SnapshotSource = 'ORIGINAL' | 'SANDBOX';

export interface ReproductionExperiment {
  id: string;
  project_id: string;
  environment_id?: string | null;
  incident_id: string;
  causal_analysis_id?: string | null;
  candidate_id?: string | null;
  experiment_version: number;
  status: ExperimentStatus;
  result: ReproductionResult;
  confidence: ConfidenceLevel;
  trigger?: string | null;
  requested_by?: string | null;
  engine_version?: string | null;
  repetitions: number;
  completed_runs: number;
  telemetry_namespace?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  timeout_at?: string | null;
  cancel_requested_at?: string | null;
  summary?: string | null;
  failure_classification?: FailureClass | null;
  created_at: string;
  updated_at: string;
}

export interface ReproductionPlan {
  id: string;
  experiment_id: string;
  strategy: ReproductionStrategy;
  target_component_id?: string | null;
  target_component_name: string;
  target_version?: string | null;
  objectives?: Record<string, unknown> | null;
  required_services?: string[] | null;
  required_dependencies?: string[] | null;
  input_sources?: unknown[] | null;
  expected_behavior?: Record<string, unknown> | null;
  safety_constraints?: Record<string, unknown> | null;
  resource_limits?: Record<string, unknown> | null;
  network_policy: SandboxNetworkPolicy;
  timeout_seconds: number;
  repetitions: number;
  derived_from?: Record<string, unknown> | null;
  created_at: string;
}

export interface ReproductionHypothesis {
  id: string;
  source_analysis_id?: string | null;
  candidate_id?: string | null;
  candidate_type?: string | null;
  component_id?: string | null;
  component_name?: string | null;
  statement: string;
  expected_failure?: string | null;
  expected_components?: string[] | null;
  expected_sequence?: string[] | null;
  expected_signals?: unknown[] | null;
  expected_time_window_seconds?: number | null;
  supporting_evidence?: unknown[] | null;
  contradicted_by?: string[] | null;
}

export interface ReproductionSandbox {
  id: string;
  sandbox_key: string;
  backend: SandboxBackendKind;
  status: SandboxStatus;
  network_policy: SandboxNetworkPolicy;
  root_path?: string | null;
  resource_limits?: Record<string, unknown> | null;
  services?: Record<string, unknown> | null;
  created_at_sandbox?: string | null;
  started_at?: string | null;
  stopped_at?: string | null;
  destroyed_at?: string | null;
  cleanup_attempts: number;
  cleanup_error?: string | null;
  orphaned: boolean;
}

export interface ReproductionRun {
  id: string;
  run_index: number;
  status: RunStatus;
  result: ReproductionResult;
  failure_classification?: FailureClass | null;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms?: number | null;
  replay_request_count: number;
  replay_success_count: number;
  replay_failure_count: number;
  replay_rejected_count: number;
  observation_count: number;
  telemetry_bytes: number;
  error?: string | null;
  faults_applied?: unknown[] | null;
  notes?: string | null;
}

export interface ReproductionInput {
  id: string;
  run_id?: string | null;
  input_index: number;
  plan_order: number;
  source: ReplayInputSource;
  replay_id?: string | null;
  status: ReplayStatus;
  method?: string | null;
  target_service?: string | null;
  target_path?: string | null;
  payload_hash?: string | null;
  redactions?: string[] | null;
  relative_offset_ms: number;
  original_timestamp?: string | null;
  replay_timestamp?: string | null;
  status_code?: number | null;
  duration_ms?: number | null;
  response_summary?: Record<string, unknown> | null;
  reject_reason?: string | null;
  error?: string | null;
}

export interface ReproductionFault {
  id: string;
  fault_type: FaultType;
  target: string;
  scope: string;
  trigger: FaultTrigger;
  parameters?: Record<string, unknown> | null;
  duration_ms?: number | null;
  intensity?: number | null;
  status: FaultStatus;
  injected: boolean;
  started_at?: string | null;
  ended_at?: string | null;
  requests_affected: number;
  result?: string | null;
}

export interface ReproductionObservation {
  id: string;
  namespace: string;
  signal_type: ObservationSignal;
  status: ObservationStatus;
  matched_expected: boolean;
  observed_at: string;
  relative_offset_ms: number;
  component_name?: string | null;
  source?: string | null;
  metric_name?: string | null;
  value?: number | null;
  unit?: string | null;
  expected_value?: number | null;
  severity?: string | null;
  message?: string | null;
  operation?: string | null;
  duration_ms?: number | null;
  error: boolean;
  trace_id?: string | null;
  span_id?: string | null;
  parent_span_id?: string | null;
  attributes?: Record<string, unknown> | null;
}

export interface ReproductionComparison {
  id: string;
  run_id: string;
  overall_similarity: ConfidenceLevel;
  similarity_score?: number | null;
  result: ReproductionResult;
  dimensions?: Record<string, number | null> | null;
  formula_reference?: string | null;
  component_overlap?: Record<string, unknown> | null;
  matched_components?: string[] | null;
  missing_components?: string[] | null;
  extra_components?: string[] | null;
  sequence_original?: string[] | null;
  sequence_reproduced?: string[] | null;
  sequence_match?: boolean | null;
  metric_deltas?: Record<string, unknown> | null;
  error_comparison?: Record<string, unknown> | null;
  trace_topology?: Record<string, unknown> | null;
  log_pattern?: Record<string, unknown> | null;
  recovery?: Record<string, unknown> | null;
  temporal?: Record<string, unknown> | null;
  original_summary?: Record<string, unknown> | null;
  reproduced_summary?: Record<string, unknown> | null;
  explanation?: string | null;
}

export interface ReproductionValidation {
  id: string;
  candidate_id?: string | null;
  outcome: ValidationOutcome;
  confidence: ConfidenceLevel;
  summary: string;
  supporting_observations?: unknown[] | null;
  contradicting_observations?: unknown[] | null;
  environment_differences?: unknown[] | null;
  missing_inputs?: string[] | null;
  determinism?: ReproductionDeterminism | null;
  artifact_ids?: string[] | null;
  limitations?: string[] | null;
}

/**
 * Repeatability over the repetitions that actually ran (§34). A rate here is an
 * observation about the sandbox, never a probability that the hypothesis holds.
 */
export interface ReproductionDeterminism {
  runs: number;
  successful_runs?: number;
  partial_runs?: number;
  reproduction_rate?: number | null;
  classification?:
    | 'DETERMINISTIC'
    | 'INTERMITTENT'
    | 'NOT_REPRODUCED'
    | 'REPRODUCED'
    | 'NOT_RUN';
  note?: string | null;
}

export interface ReproductionArtifact {
  id: string;
  run_id?: string | null;
  artifact_type: ArtifactType;
  name: string;
  content_type: string;
  storage_location: string;
  size_bytes: number;
  content_hash: string;
  immutable: boolean;
  metadata_?: Record<string, unknown> | null;
}

export interface ReproductionEnvironmentSnapshot {
  id: string;
  source: SnapshotSource;
  label?: string | null;
  captured_at: string;
  application_version?: string | null;
  schema_version?: string | null;
  runtime_versions?: Record<string, unknown> | null;
  dependency_versions?: Record<string, unknown> | null;
  configuration?: Record<string, unknown> | null;
  feature_flags?: Record<string, unknown> | null;
  service_topology?: Record<string, unknown> | null;
  resource_limits?: Record<string, unknown> | null;
  sanitization?: Record<string, unknown> | null;
  content_hash?: string | null;
}

/** Everything the reproduction workspace renders for one experiment (§45–§51). */
export interface ReproductionExperimentDetail {
  experiment: ReproductionExperiment;
  plan?: ReproductionPlan | null;
  hypothesis?: ReproductionHypothesis | null;
  runs: ReproductionRun[];
  validation?: ReproductionValidation | null;
  sandbox?: ReproductionSandbox | null;
  faults: ReproductionFault[];
  input_count: number;
  available_transitions: string[];
  disclaimer: string;
}

export interface ReproductionHistoryEntry {
  experiment_id: string;
  experiment_version: number;
  status: ExperimentStatus;
  result: ReproductionResult;
  outcome?: ValidationOutcome | null;
  confidence: ConfidenceLevel;
  duration_ms?: number | null;
  repetitions: number;
  created_at: string;
  completed_at?: string | null;
  hypothesis?: string | null;
  summary?: string | null;
}

export interface ReproductionHistory {
  incident_id: string;
  items: ReproductionHistoryEntry[];
  total: number;
}

export interface ReproductionProgress {
  status: ExperimentStatus;
  step: number;
  total_steps: number;
  percent: number;
  is_terminal: boolean;
  elapsed_seconds?: number | null;
}

export interface ReproductionStatus {
  experiment_id: string;
  status: ExperimentStatus;
  result: ReproductionResult;
  progress: ReproductionProgress;
  sandbox?: ReproductionSandbox | null;
  runs_completed: number;
  repetitions: number;
  replay_total: number;
  replay_completed: number;
  faults_active: number;
  faults_total: number;
  resources: {
    limits: Record<string, number | string | null>;
    observed?: Record<string, number | null> | null;
  };
  latest_run?: ReproductionRun | null;
  cancel_requested: boolean;
  timeout_at?: string | null;
}

/** The §47 confirmation payload — what must be shown before execution. */
export interface ReproductionSafetyPreview {
  experiment_id: string;
  sandbox: string;
  backend: SandboxBackendKind;
  network_policy: SandboxNetworkPolicy;
  production_access: string;
  credentials: string;
  resource_limits: Record<string, number | string | null>;
  timeout_seconds: number;
  repetitions: number;
  services: string[];
  faults: {
    fault_type: FaultType;
    target: string;
    status: FaultStatus;
    injected: boolean;
  }[];
  warnings: string[];
  can_start: boolean;
  blocked_reasons: string[];
}

export interface ReproductionTelemetry {
  experiment_id: string;
  namespace: string;
  items: ReproductionObservation[];
  total: number;
  expected_count: number;
  matched_count: number;
  missing_count: number;
}

export interface ReproductionComparisonList {
  experiment_id: string;
  items: ReproductionComparison[];
  total: number;
  aggregate?: {
    runs: number;
    mean_similarity?: number | null;
    buckets: string[];
    results: string[];
    sequence_matched: boolean;
    note: string;
  } | null;
}

export interface ReproductionManifest {
  experiment_id: string;
  experiment_version: number;
  status: ExperimentStatus;
  application_version?: string | null;
  strategy: ReproductionStrategy;
  services: string[];
  dependencies: string[];
  inputs: {
    method?: string | null;
    target: string;
    payload_hash?: string | null;
    offset_ms: number;
    source: ReplayInputSource;
    status: ReplayStatus;
  }[];
  faults: {
    fault_type: FaultType;
    target: string;
    status: FaultStatus;
    injected: boolean;
    requests_affected: number;
  }[];
  repetitions: number;
  network_policy: SandboxNetworkPolicy;
  resource_limits: Record<string, number | string | null>;
  timeout_seconds: number;
  artifact_hashes: {
    name: string;
    artifact_type: ArtifactType;
    content_hash: string;
    size_bytes: number;
  }[];
}

/** Observability of ARGUS's own experiments (§54). */
export interface ReproductionMetrics {
  project_id: string;
  experiments: Record<string, number>;
  results: Record<string, number>;
  runs_completed: number;
  runs_failed: number;
  sandboxes_total: number;
  sandboxes_destroyed: number;
  live_sandboxes: number;
  orphaned_sandboxes: number;
  cleanup_failures: number;
  durations_ms: Record<string, number | null>;
  failures_by_class: Record<string, number>;
  backend: string;
  network_policy: string;
  sandbox_disk: Record<string, unknown>;
}

/**
 * A requested fault. The shape itself is the security boundary: a logical
 * sandbox service and a *typed* fault, never a URL, command or image.
 */
export interface ReproductionFaultSpec {
  fault_type: FaultType;
  target: string;
  trigger?: FaultTrigger;
  duration_ms?: number | null;
  intensity?: number | null;
  parameters?: Record<string, unknown> | null;
  after_replay_index?: number | null;
  at_offset_ms?: number | null;
}

export interface ReproductionInputSpec {
  method?: string;
  target_service: string;
  target_path: string;
  payload?: Record<string, unknown> | null;
  relative_offset_ms?: number;
  source?: ReplayInputSource;
}

export interface CreateReproductionPayload {
  candidate_id?: string | null;
  causal_analysis_id?: string | null;
  strategy?: ReproductionStrategy | null;
  repetitions?: number | null;
  replay_mode?: ReplayMode | null;
  network_policy?: SandboxNetworkPolicy | null;
  timeout_seconds?: number | null;
  faults?: ReproductionFaultSpec[] | null;
  inputs?: ReproductionInputSpec[] | null;
  requested_by?: string | null;
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