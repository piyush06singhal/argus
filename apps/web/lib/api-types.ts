/**
 * ARGUS API types — the wire contract, in one place (hardening W7).
 *
 * Split out of ``api.ts``, which had grown to ~6.9k lines carrying both the
 * contract and the client. The contract is what a reader needs first when
 * they are asking "what does this endpoint return?"; a separate module makes
 * that answerable without scrolling past 1.8k lines of request plumbing.
 *
 * Nothing here changes the wire format. ``api.ts`` re-exports all of it, so
 * existing imports keep working unchanged.
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

// ---------------------------------------------------------------------------
// Authentication (hardening W1)
// ---------------------------------------------------------------------------

export type TokenRole = 'ADMIN' | 'OPERATOR' | 'VIEWER';
export type TokenState = 'ACTIVE' | 'REVOKED' | 'EXPIRED';

/** The caller's effective identity — drives role-aware UI. */
export interface WhoAmI {
  token_id: string;
  name: string;
  role: TokenRole;
  /** Empty list = every project (ADMIN). Otherwise the granted projects. */
  project_ids: string[];
  unrestricted: boolean;
  auth_enforced: boolean;
}

/** Token metadata — never the secret (it is shown once, at creation). */
export interface TokenSummary {
  id: string;
  name: string;
  role: TokenRole;
  status: TokenState;
  expires_at?: string | null;
  revoked_at?: string | null;
  last_used_at?: string | null;
  created_by?: string | null;
  description?: string | null;
  created_at: string;
  project_ids: string[];
}

export interface TokenList {
  items: TokenSummary[];
  total: number;
}

export interface TokenCreate {
  name: string;
  role: TokenRole;
  expires_in_days?: number;
  project_ids?: string[];
  description?: string;
}

export interface TokenCreateResponse {
  id: string;
  name: string;
  role: TokenRole;
  expires_at?: string | null;
  project_ids: string[];
  token: string;
  warning: string;
}

export type TokenResponse = TokenSummary;

