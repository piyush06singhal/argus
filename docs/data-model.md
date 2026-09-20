# ARGUS Data Model

Phase 0 establishes the ARGUS data model: the entities that represent software systems, their behavior, and their changes. Everything in later phases — knowledge graph, incident intelligence, root-cause analysis — consumes and extends this foundation.

## 1. Conventions

- **UUID primary keys** on every entity (`BaseModel`)
- **Timestamps** everywhere: `created_at` / `updated_at` via `TimestampMixin`, plus domain timestamps (`timestamp`, `start_time`, `deployed_at`, `detected_at`, …)
- **Enum-driven domains** for status, severity, type, and category fields (implemented as PostgreSQL `ENUM`)
- **`metadata`** — a reserved name in SQLAlchemy — is stored via a `metadata_` Pydantic field aliased to `"metadata"` on the wire. Stored as **JSONB** on PostgreSQL, **JSON** on SQLite (tests) via `JSONType`
- **Foreign keys** enforce referential integrity: components belong to a project, incidents belong to a project and optionally an environment, etc.

## 2. Entity relationship overview

```text
projects
 ├── environments
 ├── system_components ──┐
 │   └── component_dependencies (self-referential)
 ├── deployment_events
 ├── observability_events
 ├── incidents
 │    └── incident_evidence
 ├── log_records
 ├── metric_records
 ├── traces
 │    └── spans
 ├── code_repositories
 ├── graph_nodes              (Phase 2 overlay — mirrors canonical entities)
 ├── graph_edges              (typed, provenance-carrying relationships)
 ├── graph_node_aliases
 ├── graph_snapshots
 ├── graph_discovery_records
 ├── graph_reconciliation_runs
 ├── graph_data_quality_records
 ├── service_endpoints
 └── component_owners
```

### 2.1 Graph overlay linkage (Phase 2)

`graph_nodes` mirrors canonical entities without duplicating them: the pair
(`entity_kind`, `entity_id`) — e.g. `("system_component", <uuid>)` — is unique
per project and points back at the canonical row, which remains the source of
truth. `graph_edges` reference node IDs and carry `edge_type`, `source`
(provenance), `confidence` (evidence strength), `status` (ACTIVE/STALE/…),
`first_seen_at`/`last_seen_at`, and redacted `metadata` (evidence log in
`metadata.sources[]`). Indexes cover `project_id`, `environment_id`,
`node_type`, `edge_type`, `source_node_id`, `target_node_id`,
`external_identifier`, and alias lookups.

## 3. Entities

### 3.1 `projects`

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | PK |
| `name` | String(255) | |
| `slug` | String(255) | unique, indexed |
| `description` | Text | optional |
| `status` | Enum `ACTIVE`/`PAUSED`/`ARCHIVED` | default ACTIVE |
| `repository_url` | String(512) | optional |
| `repository_provider` | String(50) | optional |
| `default_branch` | String(100) | optional |
| `metadata` | JSONB | optional |
| `created_at`/`updated_at` | datetime | via mixin |

A project is the container for everything ARGUS analyzes about one software system. Deleting a project cascades to its environments, components, incidents, deployments, and observability data.

### 3.2 `environments`

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | PK |
| `project_id` | UUID → `projects.id` | indexed |
| `name` | String(255) | |
| `environment_type` | Enum `DEVELOPMENT`/`TEST`/`STAGING`/`PRODUCTION`/`CUSTOM` | |
| `description` | Text | optional |
| `is_active` | bool | default true |
| `metadata` | JSONB | optional |

### 3.3 `system_components`

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | PK |
| `project_id` | UUID → `projects.id` | indexed |
| `environment_id` | UUID → `environments.id` | optional, indexed |
| `name` | String(255) | |
| `component_type` | Enum e.g. `SERVICE`, `DATABASE`, `CACHE` | |
| `category` | Enum | e.g. application/infrastructure |
| `status` | Enum | e.g. ACTIVE/DEGRADED/DOWN |
| `description` | Text | optional |
| `owner_team` | String | optional |
| `metadata` | JSONB | |

The `component_dependencies` table models the relationship **graph**. It is self-referential: each row links a `source_component_id` to a `target_component_id`, with a `dependency_type` (e.g. `HTTP`, `DATABASE`, `MESSAGE_QUEUE`) and optional `call_rate`/`timeout_ms` hints. This graph is what every future system-map and blast-radius analysis consumes.

### 3.4 Observability

All observability entities carry `project_id`, optional `environment_id`/`component_id`, and a structured `metadata`/`payload` document. See [docs/observability-model.md](docs/observability-model.md).

| Entity | Table | Key fields |
|--------|-------|------------|
| `ObservabilityEvent` | `observability_events` | `timestamp`, `source`, `event_type`, `severity`, `payload`, `request_id`, `trace_id`, `span_id`, `deployment_id`, `incident_id` |
| `LogRecord` | `log_records` | `timestamp`, `level`, `message`, `service`, `trace_id`, `span_id` |
| `MetricRecord` | `metric_records` | `timestamp`, `metric_name`, `metric_type`, `value`, `unit`, `labels` |
| `TraceRecord` | `traces` | `trace_id` (string), `name`, `start_time`, `end_time`, `duration_ms`, `status` |
| `SpanRecord` | `spans` | `trace_id`, `span_id`, `parent_span_id`, `operation`, `duration_ms`, `status` |

Traces and spans use string IDs (the industry-standard W3C-compatible IDs) rather than UUIDs; `GET /observability/traces/{trace_id}` returns a trace with all its spans, preserving parent/child structure.

### 3.5 Incidents & evidence

| Entity | Table | Key fields |
|--------|-------|------------|
| `Incident` | `incidents` | `title`, `description`, `severity` (`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`), `status` (`OPEN`/`ACKNOWLEDGED`/`INVESTIGATING`/`MITIGATED`/`RESOLVED`/`CLOSED`), `detected_at`, `started_at`, `resolved_at`, `acknowledged_at`, `fingerprint` (deterministic dedup key), `summary` (deterministic, regenerated from evidence), `primary_component_id`, `correlation_rationale` (why the anomalies were grouped), `status_changed_by` |
| `IncidentEvidence` | `incident_evidence` | `evidence_type` (`LOG`/`METRIC`/`TRACE`/`SPAN`/`DEPLOYMENT`/`CONFIGURATION_CHANGE`/`HEALTH_CHECK`/`GRAPH`/`ANOMALY`/`CODE_CHANGE`/`DEPENDENCY_CHANGE`/`CUSTOM`), `source_id`, `timestamp`, `relevance_score` (0.0–1.0), `description`, `component_id`, `anomaly_id`, `observed_value`, `expected_value`, `severity`, `confidence`, `provenance`, `relevance_reason` |
| `IncidentTimelineEvent` | `incident_timeline_events` | `event_type`, `occurred_at` (UTC), `title`, `description`, `component_id`, `anomaly_id`, `evidence_id`, `is_context_only`, `provenance` |

An incident connects to evidence via `incident_evidence` rows; each row references a specific log/metric/trace/deployment by `source_id` and records a `relevance_reason` — **why it is relevant, never why it is the cause**. `IncidentTimelineEvent.is_context_only` distinguishes temporal context (a nearby deployment) from an observed fact, so the UI can render the distinction instead of blurring it.

`incident_evidence.incident_id` cascades with its incident (evidence is *of* the incident); component-scoped references are `SET NULL` so deleting a component never erases incident history. Anomaly grouping is `SET NULL` on `anomalies.incident_id` — being correlated is not ownership.

### 3.5.1 Phase 3 anomaly domain

| Entity | Table | Key fields |
|--------|-------|------------|
| `AnomalyRule` | `anomaly_rules` | `name` (unique per project), `anomaly_type`, `condition`, `metric_name`, `baseline_strategy`, `expected_value`, `threshold`, `multiplier`, `z_threshold`, `min_samples`, `window_seconds`, `cooldown_seconds`, `persistence_cycles`, `severity`, `enabled`, `component_id` / `environment_id` scope |
| `AnomalyBaseline` | `anomaly_baselines` | `metric_name`, `strategy`, `window_seconds`, `sample_count`, `mean`, `median`, `stddev`, `min_value`, `max_value`, `p50`, `p95`, `p99`, `expected_value`, `computed_at` (append-only, auditable) |
| `Anomaly` | `anomalies` | `anomaly_type`, `severity`, `status`, `source`, `metric_name`, `pattern_template`, `observed_value`, `expected_value`, `deviation`, `threshold`, `z_score`, `confidence`, `fingerprint`, `description`, `source_event_id`, `observation_count`, `detected_at`, `started_at`, `ended_at`, `last_seen_at`, `suppressed`, `suppression_reason`, `rule_id`, `incident_id` |
| `AnomalyObservation` | `anomaly_observations` | `observed_at`, `observed_value`, `expected_value`, `deviation`, `z_score`, `sample_count`, `source_event_id`, `payload_summary` (redacted) |
| `AnomalyFingerprint` | `anomaly_fingerprints` | `fingerprint` (unique per project), `anomaly_type`, `anomaly_id`, `occurrence_count`, `first_seen_at`, `last_seen_at` |
| `AnomalySuppression` | `anomaly_suppressions` | `reason`, `starts_at`, `ends_at`, `enabled`, `anomaly_type` / `metric_name` / `component_id` / `environment_id` scope |
| `MaintenanceWindow` | `maintenance_windows` | `name`, `starts_at`, `ends_at`, `suppress_anomalies`, `downgrade_severity`, `reason`, `enabled` |

Suppression is **recorded, never silent**: a suppressed anomaly keeps its row, its rule reference, the reason and the timestamp. Anomaly and incident fingerprints are deterministic hashes of stable context (project, environment, component, type, discriminator) — never of a timestamp or random id.

### 3.5.2 Phase 4 causal domain

| Entity | Table | Key fields |
|--------|-------|------------|
| `CausalAnalysis` | `causal_analyses` | `incident_id`, `project_id`, `environment_id`, `analysis_version` (unique per incident), `status`, `trigger`, `requested_by`, `analysis_version_tag`, `started_at`, `completed_at`, `overall_confidence`, `primary_candidate_id`, `summary`, `missing_evidence`, `analysis_metadata` (reproducibility record: input fingerprint, engine version, window, budgets) |
| `RootCauseCandidate` | `root_cause_candidates` | `candidate_type`, `component_id` / `event_id`, `status`, `score`, `confidence`, `is_external`, `first_observed_at`, `supporting_evidence_count`, `contradicting_evidence_count`, `neutral_evidence_count`, `explanation`, `score_breakdown`, `reasons`, `uncertainty` |
| `CausalEvidence` | `causal_evidence` | `candidate_id` (required owner), `relationship_id` (set when the row justifies an edge), `category`, `polarity`, `source_table` + `source_id` (provenance), `quote`, `explanation`, `component_id`, `observed_at`, `strength` |
| `CausalRelationship` | `causal_relationships` | `source_candidate_id`, `target_candidate_id`, `relationship_type`, `confidence`, `supporting_evidence_count`, `contradicting_evidence_count`, `temporal_alignment_seconds`, `structural_support`, `observational_support`, `contradiction_notes`, `explanation` |

Three invariants shape this domain:

* **Score and confidence are separate columns.** `score` is a deterministic,
  documented combination of evidence components; `confidence` is a bucket
  (`HIGH`/`MEDIUM`/`LOW`/`INSUFFICIENT`). No probability is ever stored.
* **`primary_candidate_id` is nullable on purpose.** A null primary means ARGUS
  declined to name a root cause — a stored, first-class outcome, not a failure.
* **A candidate's counts must match the evidence a reader can see.**
  `causal_evidence.candidate_id` is non-null (the schema requires an owner), so
  candidate-level reads exclude rows that also carry a `relationship_id`: those
  justify an edge, and counting them twice made a candidate display more support
  than its confidence was computed on.

### 3.6 Deployments & code repositories

| Entity | Table | Key fields |
|--------|-------|------------|
| `DeploymentEvent` | `deployment_events` | `component_id`, `environment_id`, `version`, `commit_sha`, `deployed_by`, `status` (`SUCCESS`/`FAILED`/`ROLLED_BACK`/…), `deployed_at`, `change_summary` |
| `CodeRepository` | `code_repositories` | `url`, `provider`, `default_branch` |

Deployments are the anchor for future incident ↔ change correlation ("a deployment occurred shortly before the incident").

## 4. Enum domains

| Entity field | Enum values |
|--------------|-------------|
| `ProjectStatus` | ACTIVE, PAUSED, ARCHIVED |
| `EnvironmentType` | DEVELOPMENT, TEST, STAGING, PRODUCTION, CUSTOM |
| `ComponentCategory` / `ComponentStatus` / `DependencyType` | per-domain (see `models/system.py`) |
| `IncidentSeverity` | LOW, MEDIUM, HIGH, CRITICAL |
| `IncidentStatus` | OPEN, ACKNOWLEDGED, INVESTIGATING, MITIGATED, RESOLVED, CLOSED |
| `EvidenceType` | LOG, METRIC, TRACE, SPAN, DEPLOYMENT, CONFIGURATION_CHANGE, HEALTH_CHECK, GRAPH, ANOMALY, CODE_CHANGE, DEPENDENCY_CHANGE, CUSTOM |
| `TimelineEventType` | INCIDENT_CREATED, ANOMALY_DETECTED, ANOMALY_UPDATED, COMPONENT_AFFECTED, DEPLOYMENT_OCCURRED, CONFIGURATION_CHANGED, HEALTH_CHANGED, TRACE_FAILURE, LOG_PATTERN_SPIKE, EVIDENCE_ADDED, NOTE, INCIDENT_STATUS_CHANGED, INCIDENT_ACKNOWLEDGED, INCIDENT_MITIGATED, INCIDENT_RESOLVED |
| `AnomalyType` | METRIC_THRESHOLD, METRIC_BASELINE_DEVIATION, ERROR_RATE_SPIKE, LATENCY_SPIKE, THROUGHPUT_DROP, LOG_PATTERN_SPIKE, TRACE_FAILURE_SPIKE, HEALTH_DEGRADATION, REQUEST_RATE_CHANGE, RESOURCE_USAGE_SPIKE, DEPLOYMENT_RELATED_CHANGE, CONFIGURATION_RELATED_CHANGE |
| `AnomalySeverity` | LOW, MEDIUM, HIGH, CRITICAL |
| `AnomalyStatus` | DETECTED, ACKNOWLEDGED, INVESTIGATING, RESOLVED, EXPIRED |
| `AnomalySource` | METRIC, LOG, TRACE, SPAN, HEALTH_CHECK, DEPLOYMENT, CONFIGURATION, GRAPH, COMPOSITE, UNKNOWN |
| `BaselineStrategy` | STATIC, ROLLING |
| `RuleCondition` | THRESHOLD, BASELINE_DEVIATION, Z_SCORE, RATE_CHANGE, ERROR_RATE, LATENCY_RATIO, PATTERN_SPIKE, HEALTH_TRANSITION, TRACE_FAILURE_RATE |
| `EventType` / `Severity` | per-domain (see `models/observability.py`) |
| `MetricType` | COUNTER, GAUGE, HISTOGRAM, … |
| `TraceStatus` | OK, ERROR, UNKNOWN, … |
| `DeploymentStatus` | SUCCESS, FAILED, ROLLED_BACK, … |
| `CandidateType` | DEPLOYMENT, CONFIGURATION_CHANGE, APPLICATION_COMPONENT, DATABASE, EXTERNAL_DEPENDENCY, INFRASTRUCTURE, RESOURCE_EXHAUSTION, DEPENDENCY_FAILURE, DATA_ISSUE, UNKNOWN |
| `CandidateStatus` | SUPPORTED, WEAKENED, WITHDRAWN, UNDER_EVALUATION |
| `CausalRelationshipType` | POSSIBLE_CAUSE, LIKELY_CAUSE, DOWNSTREAM_EFFECT, CONTRIBUTES_TO, BLOCKS, TRIGGERS, AMPLIFIES, CORRELATES_WITH |
| `CausalEvidenceCategory` | TEMPORAL, TRACE, DEPENDENCY, CHANGE, METRIC, LOG, HEALTH, RESOURCE, CONFIGURATION, DEPLOYMENT, RECOVERY, CONTRADICTING |
| `EvidencePolarity` | SUPPORTING, CONTRADICTING, NEUTRAL |
| `ConfidenceLevel` | HIGH, MEDIUM, LOW, INSUFFICIENT |
| `AnalysisStatus` | PENDING, RUNNING, COMPLETED, FAILED |

Pydantic validates these enums at the API boundary, so malformed values are rejected with 422.

## 5. Indexes & performance

- `slug` unique on projects; indexed FKs on `project_id`, `environment_id`, `component_id`
- Timestamp columns are eligible for range queries (`start_time`, `timestamp`, `detected_at`) with pagination on every list endpoint
- No endpoint returns unbounded observability data (`page_size` ≤ 100)
- Causal analysis is bounded on every axis: evidence window, candidate budget,
  edge budget, trace cap, dependency hops and evidence cap are all settings, so
  no query ever scans the whole telemetry database for one incident

## 6. Migrations

Schema is managed by Alembic (`apps/api/alembic/`). See [docs/development.md](docs/development.md).

## 7. Testing with SQLite

`JSONType` maps to `JSONB` on PostgreSQL and `JSON` on SQLite, so the full model runs against a file-backed SQLite database in tests (fast, isolated). Enum columns behave identically on both backends via SQLAlchemy `Enum` with `native_enum` semantics compatible in both.
