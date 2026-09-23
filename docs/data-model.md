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
 ├── component_owners
 ├── causal_analyses                     (Phase 4 causal domain)
 │    ├── root_cause_candidates
 │    ├── causal_evidence
 │    └── causal_relationships
 └── reproduction_experiments             (Phase 5 reproduction domain)
      ├── reproduction_plans
      ├── reproduction_hypotheses
      ├── reproduction_sandboxes
      ├── reproduction_runs ──► reproduction_inputs / reproduction_observations
      ├── reproduction_faults
      ├── reproduction_comparisons
      ├── reproduction_validations
      ├── reproduction_artifacts
      └── environment_snapshots
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

### 3.5.3 Phase 5 reproduction domain

| Entity | Table | Key fields |
|--------|-------|------------|
| `ReproductionExperiment` | `reproduction_experiments` | `incident_id`, `project_id`, `environment_id`, `causal_analysis_id`, `candidate_id`, `experiment_version`, `status`, `result`, `confidence`, `trigger`, `requested_by`, `engine_version`, `repetitions`, `completed_runs`, `telemetry_namespace`, `started_at`, `completed_at`, `timeout_at`, `cancel_requested_at`, `summary`, `failure_classification` |
| `ReproductionPlan` | `reproduction_plans` | `strategy`, `target_component_id`, `target_version`, `objectives`, `required_services`, `required_dependencies`, `input_sources`, `expected_behavior`, `safety_constraints`, `resource_limits`, `network_policy`, `timeout_seconds`, `repetitions`, `derived_from` |
| `ReproductionHypothesis` | `reproduction_hypotheses` | `source_analysis_id`, `candidate_id`, `candidate_type`, `component_id`, `statement`, `expected_failure`, `expected_components`, `expected_sequence`, `expected_signals`, `expected_time_window_seconds`, `supporting_evidence`, `contradicted_by` |
| `ReproductionSandbox` | `reproduction_sandboxes` | `sandbox_key`, `backend`, `status`, `network_policy`, `root_path`, `resource_limits`, `services`, `created_at_sandbox`, `started_at`, `stopped_at`, `destroyed_at`, `cleanup_attempts`, `cleanup_error`, `orphaned` |
| `ReproductionRun` | `reproduction_runs` | `run_index`, `status`, `result`, `failure_classification`, `started_at`, `completed_at`, `duration_ms`, `replay_request_count`, `replay_success_count`, `replay_failure_count`, `replay_rejected_count`, `observation_count`, `telemetry_bytes`, `error`, `faults_applied`, `notes` |
| `ReproductionInput` | `reproduction_inputs` | `plan_order`, `source`, `replay_id`, `status`, `method`, `target_service`, `target_path`, `payload_hash`, `redactions`, `relative_offset_ms`, `original_timestamp`, `replay_timestamp`, `status_code`, `duration_ms`, `response_summary`, `reject_reason` |
| `ReproductionFault` | `reproduction_faults` | `fault_type`, `target`, `scope`, `trigger`, `parameters`, `duration_ms`, `intensity`, `status`, `injected`, `started_at`, `ended_at`, `requests_affected`, `result` |
| `ReproductionObservation` | `reproduction_observations` | `namespace`, `signal_type`, `status`, `matched_expected`, `observed_at`, `relative_offset_ms`, `component_name`, `metric_name`, `value`, `unit`, `expected_value`, `severity`, `message`, `operation`, `duration_ms`, `error`, `trace_id`/`span_id`/`parent_span_id`, `attributes` |
| `ReproductionComparison` | `reproduction_comparisons` | `run_id`, `overall_similarity`, `similarity_score`, `result`, `dimensions`, `formula_reference`, `component_overlap`, `matched_components`, `missing_components`, `extra_components`, `sequence_original`, `sequence_reproduced`, `sequence_match`, `metric_deltas`, `error_comparison`, `trace_topology`, `log_pattern`, `recovery`, `temporal`, `original_summary`, `reproduced_summary`, `explanation` |
| `ReproductionValidation` | `reproduction_validations` | `candidate_id`, `outcome`, `confidence`, `summary`, `supporting_observations`, `contradicting_observations`, `environment_differences`, `missing_inputs`, `determinism`, `artifact_ids`, `limitations` |
| `ReproductionArtifact` | `reproduction_artifacts` | `run_id`, `artifact_type`, `name`, `content_type`, `storage_location`, `size_bytes`, `content_hash`, `immutable`, `metadata` |
| `EnvironmentSnapshot` | `environment_snapshots` | `source` (`ORIGINAL`/`SANDBOX`), `label`, `captured_at`, `application_version`, `schema_version`, `runtime_versions`, `dependency_versions`, `configuration`, `feature_flags`, `service_topology`, `resource_limits`, `sanitization`, `content_hash` |

Three invariants shape this domain:

* **`result` and `outcome` are never the same column.** `result` (on the
  experiment and the run) says what the sandbox observed; `outcome` (on the
  validation) says what that means for the hypothesis. Collapsing them would make
  "it behaved like the incident" and "the hypothesis is therefore true"
  indistinguishable at the storage layer.
* **A fault is recorded whether or not it fired.** `injected` plus
  `requests_affected` (derived from captured telemetry, not from ARGUS's own
  counter) is what keeps "it failed naturally" separable from "we caused it".
* **Reproduction telemetry carries its own namespace.**
  `reproduction_observations.namespace` is `repro:<experiment_id>`, so a
  reproduction can never be mistaken for the production signal it reproduces.

### 3.6 Deployments & code repositories

| Entity | Table | Key fields |
|--------|-------|------------|
| `DeploymentEvent` | `deployment_events` | `component_id`, `environment_id`, `version`, `commit_sha`, `deployed_by`, `status` (`SUCCESS`/`FAILED`/`ROLLED_BACK`/…), `deployed_at`, `change_summary` |
| `CodeRepository` | `code_repositories` | `project_id`, `provider` (`local`/`git`), `repository_url`, `local_path`, `default_branch`, `connection_status`, `capabilities`, `index_status`, `last_indexed_at`, `last_indexed_commit` |

Deployments are the anchor for incident ↔ change correlation ("a deployment occurred shortly before the incident") and for deriving which revision a snapshot should pin.

### 3.7 Phase 6 code-intelligence & debugger domain

| Entity | Table | Key fields |
|--------|-------|------------|
| `RepositorySnapshot` | `repository_snapshots` | `repository_id`, `project_id`, `revision`, `resolved_revision`, `revision_type`, `version_status` (`RESOLVED`/`UNRESOLVED`/`UNKNOWN`), `version_evidence`, `status` (`CREATED`/`INDEXING`/`READY`/`PARTIAL`/`FAILED`), `provider_name`, `parser_version`, `content_hash`, `file_count`, `symbol_count` |
| `CodeIndexRun` | `code_index_runs` | `snapshot_id`, `repository_id`, `status`, `trigger`, `incremental`, `base_commit_sha`, `files_seen/indexed/reused/added/modified/deleted/failed`, `symbols_indexed`, `references_indexed`, `relationships_indexed`, `duration_ms`, `run_metadata` |
| `CodeFile` | `code_files` | `snapshot_id`, `path`, `language`, `module_name`, `size_bytes`, `line_count`, `is_test`, `parse_status`, `parse_error`, `last_commit_sha`, `last_author`, `symbol_count` |
| `CodeSymbol` | `code_symbols` | `snapshot_id`, `file_id`, `file_path`, `symbol_name`, `qualified_name`, `symbol_type`, `start_line`, `end_line`, `signature`, `documentation`, `is_async`, `complexity`, `route`, `http_method`, `component_id`, `caller_count`, `callee_count` |
| `CodeReference` | `code_references` | `snapshot_id`, `file_id`, `name`, `line`, `kind`, `resolved`, `symbol_id` |
| `CodeRelationship` | `code_relationships` | `snapshot_id`, `source_symbol_id`, `target_symbol_id`, `relationship` (`CALLS`/…), `confidence`, `line` |
| `CodeRiskSignal` | `code_risk_signals` | `snapshot_id`, `file_id`, `signal_type`, `severity`, `reason`, `metadata` |
| `TraceCodeMapping` | `trace_code_mappings` | `incident_id`, `project_id`, `snapshot_id`, `span_id`, `trace_id`, `operation`, `service_name`, `endpoint`, `http_method`, `mapping_kind`, `symbol_id`, `file_path`, `start_line`, `end_line`, `confidence`, `evidence`, `unmapped_reason` |
| `DebugSession` | `debug_sessions` | `project_id`, `incident_id`, `repository_id`, `snapshot_id`, `title`, `status`, `created_by`, `version_status`, `version_note`, `context_version`, `summary` |
| `DebugAnalysisRun` | `debug_analysis_runs` | `session_id`, `status` (`PENDING`/`RUNNING`/`COMPLETED`/`DEGRADED`/`FAILED`/`CANCELLED`/`LIMIT_REACHED`), `kind`, `provider_name`, `model_name`, `prompt_version`, `context_version`, `tool_call_count`, `files_accessed`, `context_bytes`, `confidence`, `summary`, `invalid_references`, `missing_evidence`, `recommended_inspections`, `degraded_reason`, `context_snapshot`, `redaction_report`, `error`, `run_metadata` |
| `DebugCodeLocation` | `debug_code_locations` | `analysis_run_id`, `file_path`, `symbol_id`, `symbol_name`, `start_line`, `end_line`, `label`, `reason`, `confidence`, `validation` (`VALID`/`NOT_FOUND`/`OUT_OF_SNAPSHOT`/`LINE_OUT_OF_RANGE`/`AMBIGUOUS`/`STALE`), `validation_detail`, `evidence_refs`, `displayable` |
| `DebugHypothesis` | `debug_hypotheses` | `analysis_run_id`, `description`, `category`, `confidence`, `validation_status` (`UNVERIFIED`/`SUPPORTED`/`PARTIALLY_SUPPORTED`/`WEAKENED`/`REFUTED`/`INVALID_REFERENCE`), `rationale`, `testable`, `test_approach`, `recurrence_count` |
| `DebugEvidence` | `debug_evidence` | `hypothesis_id`, `kind`, `polarity` (`SUPPORTING`/`CONTRADICTING`/`NEUTRAL`), `reference`, `source_table`, `source_id`, `quote`, `start_line`, `end_line`, `valid`, `validation_error`, `strength`, `observed_at` |
| `DebugMessage` | `debug_messages` | `session_id`, `role` (`ENGINEER`/`ARGUS`/`SYSTEM`), `content`, `created_by`, `evidence_refs`, `metadata` |
| `DebugToolCall` | `debug_tool_calls` | `analysis_run_id`, `tool_name`, `arguments`, `status`, `result_summary`, `result_count`, `result_bytes`, `truncated`, `error`, `duration_ms` |

Three invariants shape this domain:

* **A claim and its validation live on the same row but are separate fields.**
  `file_path`/`symbol`/`start_line` is the *claim*; `validation`,
  `validation_detail` and `displayable` are the *verdict*. A non-`VALID` claim is
  stored (audit matters) with `displayable: false` — the API and the UI both
  treat that as final.
* **Snapshot ids are the only source of line-level truth.** Locations resolve
  against the pinned snapshot; a newer commit makes them `STALE` until
  re-indexed, and stable symbol ids across incremental re-index keep stored
  references resolving.
* **Every external tool call is recorded.** `debug_tool_calls` stores arguments,
  status, result size and truncation for each model tool call — the analysis is
  reproducible as an audit even when the model is not.

### 3.8 Phase 7 fix-generation & verification domain

| Entity | Table | Key fields |
|--------|-------|------------|
| `FixHypothesis` | `fix_hypotheses` | `incident_id`, `debug_session_id`, `analysis_run_id`, `root_cause_candidate_id`, `reproduction_experiment_id`, `repository_id`, `snapshot_id`, `title`, `description`, `proposed_change`, `expected_behavior`, `category` (`BUG_FIX`/`ERROR_HANDLING`/`TIMEOUT_FIX`/`RETRY_FIX`/`VALIDATION_FIX`/`RESOURCE_HANDLING`/`CONCURRENCY_FIX`/`DATABASE_QUERY_FIX`/`API_CONTRACT_FIX`/`CONFIGURATION_FIX`/`DEPENDENCY_HANDLING`/`PERFORMANCE_FIX`/`UNKNOWN`), `scope_files`, `excluded_paths`, `supporting_evidence`, `target_symbols`, `risk_level`, `confidence`, `status` (`DRAFT`/`HYPOTHESIZED`/`PATCHING`/`PATCH_GENERATED`/`GENERATION_FAILED`/`REJECTED`/`SUPERSEDED`), `created_by` |
| `Patch` | `patches` | `fix_hypothesis_id`, `patch_experiment_id`, `repository_id`, `snapshot_id`, `base_commit_sha`, `patch_format`, `patch_content`, `changed_files`, `lines_added/removed`, `symbols_modified`, `affected_paths`, `generated_by`, `generation_model`, `generation_version`, `status` (`GENERATED`/`PARSE_FAILED`/`VALIDATION_FAILED`/`APPLIED`/`BUILD_FAILED`/`TEST_FAILED`/`REPRODUCTION_FAILED`/`VERIFIED`/`REJECTED`/`SUPERSEDED`/`GENERATION_FAILED`), `explanation`, `failure_reason` |
| `PatchWorkspace` | `patch_workspaces` | `patch_id`, `patch_experiment_id`, `repository_id`, `branch_name`, `base_commit_sha`, `patched_commit_sha`, `root_path`, `status` (`CREATING`/`READY`/`PATCH_APPLIED`/`BUSY`/`DESTROYED`/`FAILED`), `created_at_workspace`, `destroyed_at`, `workspace_metadata` |
| `PatchVerificationRun` | `patch_verification_runs` | `patch_id`, `workspace_id`, `status` (`PENDING`/`RUNNING`/`VERIFIED`/`NOT_VERIFIED`/`FAILED`/`CANCELLED`), `level` (`NONE`/`STATIC_VALIDATED`/`TEST_VALIDATED`/`REPRODUCTION_VALIDATED`/`REGRESSION_VALIDATED`/`FULLY_VERIFIED`), `confidence`, `confidence_reason`, `tampering_flag`, `verification_env_intact`, `baseline_failure_reproduced`, `patched_failure_reproduced`, `regression_detected`, `duration_ms`, `verdict_reason`, `evidence` |
| `PatchTestRun` | `patch_test_runs` | `verification_run_id`, `workspace_id`, `kind` (`STATIC`/`BUILD`/`UNIT`/`INTEGRATION`/`REGRESSION`/`FULL_SUITE`), `command_key`, `command_resolved`, `unknown_configuration`, `exit_code`, `timed_out`, `duration_ms`, `tests_total/passed/failed`, `output_tail`, `selected_tests`, `selection_reason` |
| `PatchRegressionTest` | `patch_regression_tests` | `verification_run_id`, `name`, `file_path`, `origin`, `content_hash`, `ran_on_base`, `failed_on_base`, `ran_on_patched`, `passed_on_patched`, `valid`, `invalid_reason` |
| `PatchComparison` | `patch_comparisons` | `verification_run_id`, `metrics`, `regressions`, `thresholds`, `summary`, `causal_chain_resolved`, `causal_chain_note` |
| `PatchRiskAssessment` | `patch_risk_assessments` | `patch_id`, `risk_level` (`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`), `signals`, `explanation` |
| `PatchReviewAction` | `patch_review_actions` | `patch_id`, `action` (`APPROVE`/`REJECT`/`REQUEST_CHANGES`/`REGENERATE`/`EXPORT`), `actor`, `reason`, `new_patch_id`, `audit_metadata` |
| `PatchArtifact` | `patch_artifacts` | `patch_id`, `verification_run_id`, `artifact_type`, `name`, `storage_path`, `size_bytes`, `content_hash` (SHA-256), `immutable`, `artifact_metadata` |

Four invariants shape this domain:

* **A suggestion is not a patch, and a patch is not a fix.** `status` moves
  `GENERATED` → `APPLIED` → `VERIFIED` only through the verification engine's own
  run; nothing sets `VERIFIED` as a side effect of generation or review.
* **Every verdict cites stored rows.** A `VERIFIED` run points at the test runs,
  the two-sided regression test and the comparison that produced it; the API
  returns them with the verdict, and the export report derives its checklist from
  them rather than restating them.
* **Review is human and final.** `patch_review_actions` is the only path to an
  `APPROVED` review state; approval additionally requires a stored `VERIFIED`
  run, and a recorded decision refuses further actions on that candidate.
* **Artifacts outlive the experiment.** Files are written outside the workspace
  (which is destroyed unconditionally), hashed from the exact bytes on disk, and
  marked immutable once the run is terminal.

### Phase 8 — Predictive reliability

| Entity | Table | Key fields |
| :--- | :--- | :--- |
| `ReliabilityForecast` | `reliability_forecasts` | `project_id`, `environment_id`, `component_id`, `prediction_type`, `forecast_horizon`, `generated_at`, `valid_from`, `valid_until`, `forecast_time`, `risk_score`, `risk_level`, `confidence`, `calibration_status`, `data_coverage`, `coverage_verdict`, `model_version_id`, `feature_snapshot_id`, `previous_forecast_id`, `revision`, `status`, `headline`, `supporting_evidence`, `limitations` |
| `PredictiveSignal` | `predictive_signals` | `forecast_id`, `signal_type`, `component_id`, `severity`, `description`, `observed_value`, `baseline_value`, `trend`, `change_rate`, `evidence_ids` |
| `ForecastFeatureSnapshot` | `forecast_feature_snapshots` | `project_id`, `environment_id`, `component_id`, `forecast_time`, `window_seconds`, `feature_schema_version`, `features` (JSON), `feature_sources`, `data_coverage`, `sample_count`, `staleness_seconds` |
| `ForecastOutcome` | `forecast_outcomes` | `forecast_id`, `horizon_end`, `outcome_type` (`TRUE_POSITIVE`/`FALSE_POSITIVE`/`TRUE_NEGATIVE`/`FALSE_NEGATIVE`/`INCONCLUSIVE`), `observed_risk_level`, `predicted_risk_level`, `incident_id`, `anomaly_id`, `observed_evidence`, `evaluated_at`, `evaluation_run_id` |
| `ForecastFingerprint` | `forecast_fingerprints` | `project_id`, `environment_id`, `component_id`, `prediction_type`, `forecast_horizon`, `dedup_key`, `current_forecast_id`, `revision`, `last_generated_at` |
| `ReliabilityModelVersion` | `reliability_model_versions` | `name`, `model_type`, `version`, `status` (`DEVELOPMENT`/`VALIDATED`/`ACTIVE`/`RETIRED`), `parameters`, `trained_on_samples`, `description` |
| `ReliabilityEvaluationRun` | `reliability_evaluation_runs` | `project_id`, `window_start`, `window_end`, `forecasts_evaluated`, `sample_count`, `outcomes` (counts by type), `precision`, `recall`, `calibration_status`, `feature_schema_version`, `notes` |
| `ReliabilityBacktest` | `reliability_backtests` | `project_id`, `status`, `start_time`, `end_time`, `training_window_seconds`, `component_ids`, `horizons`, `prediction_types`, `step_seconds`, `steps` (JSON), `summary`, `leakage_contract` |
| `ReliabilityDriftRecord` | `reliability_drift_records` | `project_id`, `environment_id`, `component_id`, `drift_kind`, `status` (`STABLE`/`WATCH`/`FLAGGED`), `metric_name`, `reference_value`, `current_value`, `change_ratio`, `threshold`, `detected_at`, `review_policy`, `retrain_performed`, `model_activated` |
| `ReliabilityEarlyWarning` | `reliability_early_warnings` | `project_id`, `environment_id`, `component_id`, `forecast_id`, `prediction_type`, `risk_level`, `severity`, `dedup_key`, `first_raised_at`, `last_raised_at`, `cooldown_seconds`, `status`, `acknowledged_at`, `dismissed_at`, `actor`, `message` |

Four invariants shape this domain:

* **A prediction is not a fact, an anomaly or an incident.** A forecast stores its
  own validity window and never creates an incident; an incident created after a
  forecast was generated cannot rewrite that forecast.
* **Every forecast is reproducible.** The feature snapshot (exact features,
  window and schema version) is written *before* the forecast row, so a stored
  forecast can always be re-derived from stored inputs.
* **Scope is proven, not trusted.** Every row carries `project_id`; component
  references use `ON DELETE SET NULL` so deleting a component never erases
  forecast history, and readers that need attribution (such as the heatmap) omit
  unattributable rows rather than rendering a nameless one.
* **Drift and evaluation are append-only.** A drift record flags for review and
  can neither retrain nor activate a model; an evaluation run scores each elapsed
  horizon exactly once.

### Phase 9 — Safe autonomous remediation

Twelve tables, one per object the remediation pipeline reasons about: the
operator's intent (policy), what was suggested (proposal), what will be done
(action), what each gate concluded (assessment, decision, approval), what
happened (execution), whether it helped (verification, rollback) and who did what
(audit event) — plus the two books the safety guarantees live in (circuit
breaker, control).

| Entity | Table | Key fields |
| :--- | :--- | :--- |
| `RemediationPolicy` | `remediation_policies` | `project_id`, `environment_id`, `execution_mode`, `autonomous_max_risk`, `allowed_action_types`, `allowed_environment_names`, `max_actions_per_window`, `cooldown_seconds`, `max_concurrent_actions`, `canary_enabled`, `canary_percent`, `max_blast_radius_percent`, `circuit_*`, `approval_ttl_seconds`, `verification_*`, `max_execution_attempts`, `action_expiry_seconds`, `emergency_stop_*`, `revision`, `updated_by` |
| `RemediationProposal` | `remediation_proposals` | `action_type`, `source_type`/`source_id` (incident, causal analysis, candidate, patch, forecast, operator), `strategy`, `problem`, `expected_effect`, `supporting_evidence`, `parameters`, `risk_level`, `blast_radius`, `preconditions`, `verification_plan`, `rollback_plan`, `confidence`, `limitations`, `fingerprint` |
| `RemediationAction` | `remediation_actions` | `proposal_id`, `action_type`, `status` (the §5 lifecycle), `parameters`, `risk_level`, `blast_radius`, `blast_radius_percent`, `affected_resource_count`, `safety_status`, `policy_status`, `authorization_status`, `execution_status`, `execution_mode`, `rollback_plan`, `verification_plan`, `canary_*`, `attempt`, `max_retries`, `failure_reason`, `outcome`, `post_analysis_status`, `expires_at`, `approved_by`, `authorized_by`, `executed_by`, `fingerprint` |
| `RemediationAssessment` | `remediation_assessments` | `action_id`, `status` (`PASSED`/`PASSED_WITH_WARNINGS`/`FAILED`), `checks` (JSON, one entry per check with name/result/severity), `blocking`, `warnings`, `reversible`, `rollback_plan`, `blast_radius`, `requires_human_approval`, `reason`, `assessed_by` |
| `RemediationPolicyDecision` | `remediation_policy_decisions` | `action_id`, `decision` (`ALLOW`/`ALLOW_WITH_CANARY`/`REQUIRE_APPROVAL`/`DENY`), `execution_mode`, `matched_rules`, `reasons`, `failure_reason`, `requires_canary`, `budget_state`, `circuit_state`, `policy_revision`, `evaluated_by` |
| `RemediationApproval` | `remediation_approvals` | `action_id`, `status` (`PENDING`/`APPROVED`/`REJECTED`/`EXPIRED`), `actor_type` (`HUMAN`/`AUTONOMOUS_POLICY`), `actor`, `decided_at`, `expires_at`, `reason`, `scope_snapshot` (JSON — what the approver saw) |
| `RemediationExecution` | `remediation_executions` | `action_id`, `attempt` (unique with the action), `status`, `mode`, `dry_run`, `effect_applied`, `adapter_kind`, `adapter_name`, `steps`, `output_summary`, `error`, `failure_reason`, `control_ids`, `idempotency_key`, `duration_ms`, `executed_by` |
| `RemediationVerification` | `remediation_verifications` | `action_id`, `execution_id`, `verdict` (`VERIFIED`/`PARTIALLY_VERIFIED`/`FAILED`/`INCONCLUSIVE`/`NOT_EXECUTED`), `checks` (JSON), `attempt`, `window_start`/`window_end`/`observation_seconds`, `passed_count`, `failed_count`, `not_observable_count`, `summary`, `limitations`, `verified_by` |
| `RemediationRollback` | `remediation_rollbacks` | `action_id`, `status` (`PENDING`/`RUNNING`/`SUCCEEDED`/`FAILED`/`NOT_AVAILABLE`), `strategy`, `plan`, `steps`, `trigger`, `controls_reverted`, `inverse_action_id`, `verification_verdict` (the reversal's own verification), `requested_by`, `error`, `failure_reason` |
| `RemediationAuditEvent` | `remediation_audit_events` | `action_id`, `sequence`, `event_type`, `actor_type`, `actor`, `from_status`, `to_status`, `summary`, `detail`, `occurred_at`, `entry_hash`, `prev_hash` |
| `RemediationCircuitBreaker` | `remediation_circuit_breakers` | `project_id`, `environment_id`, `action_type`, `state` (`CLOSED`/`OPEN`/`HALF_OPEN`), `consecutive_failures`, `total_*`, `threshold`, `opened_at`, `opened_until`, `last_trip_reason` — unique on the scope, `NULLS NOT DISTINCT` |
| `RemediationControl` | `remediation_controls` | `kind` (`BACKGROUND_JOB`/`FEATURE_FLAG`/`DEPENDENCY_SUPPRESSION`), `scope_key`, `state`, `previous_state`, `is_current`, `revision`, `project_id`, `environment_id`, `component_id`, `applied_by_action_id`, `applied_by`, `reason`, `effective_from`, `expires_at`, `reverted_at` |

Four invariants shape this domain:

* **The action row is the ledger.** Every gate writes its own table and the
  action carries the resulting status; nothing is inferred from a missing row.
* **Identity is content.** A proposal and its action share a `fingerprint`
  (action type + scope + source + parameters), which is what makes a duplicate a
  refusal rather than a queue of identical actions.
* **Approval is scoped evidence.** `scope_snapshot` freezes what the approver saw;
  an autonomous approval is `actor_type=AUTONOMOUS_POLICY`, so "who decided" is
  never ambiguous.
* **The audit chain is tamper-evident.** `entry_hash` binds the entry's content
  and `prev_hash` binds its predecessor, so the chain can be recomputed and a
  break located.

### Phase 10 — Reliability intelligence & autonomous learning

Twelve tables, one per object the learning layer reasons about: the episode
(`ReliabilityExperience`), the outcome that produced it (`LearningEvent`), the
belief (`ReliabilityKnowledge`) with its revision ledger (`KnowledgeVersion`) and
its human decisions (`KnowledgeReview`), the work (`LearningRun`,
`LearningExperiment`), the per-component read model (`ComponentReliabilityProfile`),
the advice (`ReliabilityRecommendation`) and its separate outcome
(`RecommendationOutcome`), the learned graph overlay
(`LearnedRelationship`) and the producer registry (`LearningEventHook`).

| Entity | Table | Key fields |
| :--- | :--- | :--- |
| `ReliabilityExperience` | `reliability_experiences` | `project_id`, `environment_id`, `incident_id`, `component_ids`, `failure_signature` (`failure_fingerprint`), `resolution_signature`, `outcome` (a string from the outcome enum of the phase that produced it), `recovery_bucket`, `duration_seconds`, `evidence_ids`, `as_of`, `provenance` |
| `LearningEvent` | `learning_events` | `project_id`, `event_type`, `subject_id`, `subject_type`, `incident_id`, `provenance`, `payload`, `dedup_key` (unique), `occurred_at`, `processed_at`, `process_reason`, `run_id` |
| `ReliabilityKnowledge` | `reliability_knowledge` | `knowledge_type`, `status`, `scope`, `environment_id`, `component_id`, `title`, `description`, `fingerprint`, `feature_signature`, `sources`, `experience_ids`, `sample_count`, `success_count`, `coverage_start`/`coverage_end`, `confidence`, `support_strength`, `algorithm`, `algorithm_version`, `feature_schema_version`, `validation` (JSON, per-check), `limitations`, `version`, `supersedes_knowledge_id`, `reviewed_at`/`reviewed_by`/`review_reason`, `last_confirmed_at` |
| `KnowledgeVersion` | `intelligence_knowledge_versions` | `knowledge_id`, `version`, `status`, `snapshot` (JSON), `change_note`, `run_id`, `recorded_by` |
| `KnowledgeReview` | `intelligence_knowledge_reviews` | `knowledge_id`, `decision` (`APPROVE`/`REJECT`/`REQUEST_MORE_EVIDENCE`/`DEPRECATE`), `actor`, `reason`, `from_status`, `to_status` |
| `LearningRun` | `intelligence_learning_runs` | `project_ids`, `status`, `started_at`/`completed_at`, `trigger`, `cutoff`, `summary` (JSON — counts, skips, unprocessable, errors), `error` |
| `LearningExperiment` | `intelligence_learning_experiments` | `knowledge_id`, `baseline_fingerprint`, `variant_fingerprint`, `status`, `comparison` (JSON), `limitations`, `sample_counts`, `conclusion` |
| `ComponentReliabilityProfile` | `intelligence_component_profiles` | `component_id`, `window_days`, `incidents`, `anomalies`, `remediations`, `successful_remediations`, `chronic` flags, `recommendation`, `computed_at` — unique per `(component, window)` |
| `ReliabilityRecommendation` | `intelligence_recommendations` | `recommendation_type`, `status`, `incident_id`, `component_id`, `knowledge_id`, `title`, `rationale`, `evidence`, `confidence`, `priority`, `expires_at`, `decided_at`/`decided_by`, `decision_reason` |
| `RecommendationOutcome` | `intelligence_recommendation_outcomes` | `recommendation_id`, `outcome` (`EFFECTIVE`/`INEFFECTIVE`/`REGRESSION_CAUSING`/`UNKNOWN`), `observed_at`, `evidence`, `recorded_by` |
| `LearnedRelationship` | `intelligence_relationships` | `project_id`, `environment_id`, `source_component_id`, `target_component_id`, `kind`, `directed`, `status`, `support_count`, `sample_count`, `confidence`, `support_strength`, `coverage_start`/`coverage_end`, `evidence`, `limitations` — unique per `(project, source, target, kind)` |
| `LearningEventHook` | `intelligence_event_hooks` | `producer`, `event_type`, `enabled`, `last_published_at`, `published_count`, `last_error` |

Four invariants shape this domain:

* **Derived, never authoritative.** Every row is computed from Phases 0–9 rows and
  cascades with the project (and with the incident for experiences), so the
  learning layer can never outlive the evidence it describes.
* **Provenance or nothing.** A learning record that cannot name where it came
  from is not written, and `AI_GENERATED`/`MOCK` are excluded from learning by
  default.
* **Knowledge is additive.** A revision writes a version, and a materially
  different conclusion supersedes the old row instead of editing it, so an old
  belief stays readable as it was.
* **A learned relationship is not a dependency.** It is stored in its own table
  with its own support and an explicit `directed` flag; it never writes to
  `component_dependencies` or `graph_edges`.

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
| `ExperimentStatus` | PLANNED, VALIDATING, PROVISIONING, READY, REPLAYING, RUNNING, COLLECTING, COMPARING, COMPLETED, FAILED, CANCELLED, TIMED_OUT |
| `ReproductionResult` | SUCCESSFUL, PARTIAL, FAILED, INCONCLUSIVE, NOT_RUN |
| `ReproductionStrategy` | SYNTHETIC_INPUT_REPLAY, EVENT_REPLAY, DEPENDENCY_FAULT, CONFIGURATION_REPLAY, STATE_SNAPSHOT |
| `SandboxBackendKind` / `SandboxStatus` | LOCAL_PROCESS, DOCKER / CREATING, READY, STOPPING, STOPPED, DESTROYED, FAILED |
| `SandboxNetworkPolicy` | ISOLATED, MOCK_DEPENDENCIES, CONTROLLED_EGRESS |
| `RunStatus` | PENDING, RUNNING, COMPLETED, FAILED, TIMED_OUT, CANCELLED |
| `FailureClass` | ENVIRONMENT_ERROR, INPUT_ERROR, TIMEOUT, RESOURCE_LIMIT, DEPENDENCY_UNAVAILABLE, SANDBOX_ERROR, APPLICATION_FAILURE, NO_FAILURE_OBSERVED, INSUFFICIENT_TELEMETRY, UNKNOWN |
| `ReplayInputSource` / `ReplayStatus` | HTTP_REQUEST, EVENT, MESSAGE, TRACE_INPUT, SYNTHETIC / PENDING, SENT, SUCCEEDED, FAILED, REJECTED, SKIPPED |
| `KnowledgeType` | INCIDENT_PATTERN, FAILURE_PATTERN, ANOMALY_PATTERN, REMEDIATION_PATTERN, REGRESSION_PATTERN, DEPENDENCY_PATTERN, DEPLOYMENT_PATTERN, RESOURCE_PATTERN, PREDICTIVE_PATTERN, RECOVERY_PATTERN, COMPONENT_RELIABILITY_PATTERN |
| `KnowledgeStatus` | CANDIDATE, VALIDATING, VALIDATED, ACTIVE, DEPRECATED, REJECTED, SUPERSEDED |
| `KnowledgeScope` | COMPONENT_SPECIFIC, SERVICE_CLASS, PROJECT_LEVEL, CROSS_PROJECT |
| `KnowledgeConfidence` | UNKNOWN, LOW, MEDIUM, HIGH |
| `DataProvenance` | OBSERVABILITY, SYSTEM_GENERATED, HUMAN_ENTERED, AI_GENERATED, IMPORTED, MOCK |
| `LearningEventType` | INCIDENT_RESOLVED, REMEDIATION_COMPLETED, PATCH_VERIFIED, PATCH_REGRESSION_DETECTED, FORECAST_CONFIRMED, FORECAST_FALSE_POSITIVE, FORECAST_MISSED, ROOT_CAUSE_CONFIRMED, ROOT_CAUSE_REJECTED, REPRODUCTION_CONFIRMED, REPRODUCTION_FAILED, ROLLBACK_COMPLETED |
| `KnowledgeReviewDecision` | APPROVE, REJECT, REQUEST_MORE_EVIDENCE, DEPRECATE |
| `RelationshipKind` | FAILURE_PROPAGATION, SHARED_FAILURE (always undirected), DEPENDENCY_DEGRADATION, REMEDIATION_INFLUENCE |
| `RelationshipStatus` | ACTIVE, STALE, SUPERSEDED |
| `RecommendationType` | INVESTIGATE_COMPONENT, INVESTIGATE_DEPENDENCY, REVIEW_RECENT_CHANGE, REVIEW_REMEDIATION, RUN_REPRODUCTION, CONSIDER_ROLLBACK, CONSIDER_RESTART, CONSIDER_TRAFFIC_SHIFT, REVIEW_CAPACITY, REVIEW_CONFIGURATION |
| `RecommendationStatus` | OPEN, ACCEPTED, DISMISSED, EXPIRED, EFFECTIVE, INEFFECTIVE, REGRESSION_CAUSING |
| `LearningRunStatus` / `LearningExperimentStatus` | QUEUED, RUNNING, COMPLETED, FAILED, CANCELLED / RUNNING, PASSED, FAILED, REJECTED |
| `ReplayMode` | SEQUENTIAL, PARALLEL, TIMED, BURST, RATE_LIMITED |
| `FaultType` | LATENCY, TIMEOUT, HTTP_4XX, HTTP_5XX, CONNECTION_FAILURE, RESPONSE_CORRUPTION, RESOURCE_PRESSURE, DEPENDENCY_UNAVAILABLE |
| `FaultTrigger` / `FaultStatus` | IMMEDIATE, AFTER_REPLAY_INDEX, AT_OFFSET, ON_REQUEST_COUNT, MANUAL / PLANNED, ACTIVE, COMPLETED, FAILED, SKIPPED |
| `ObservationSignal` / `ObservationStatus` | SPAN, TRACE, LOG, METRIC, HEALTH, EVENT, DEPLOYMENT, CONFIGURATION / EXPECTED, UNEXPECTED, NEUTRAL, MISSING |
| `ValidationOutcome` | SUPPORTED, PARTIALLY_SUPPORTED, NOT_SUPPORTED, INCONCLUSIVE |
| `ArtifactType` | ENVIRONMENT_SNAPSHOT, REPRODUCTION_PLAN, REPRODUCTION_MANIFEST, REPLAY_MANIFEST, TELEMETRY_SNAPSHOT, LOGS, TRACE_SUMMARY, COMPARISON_RESULT, SANDBOX_METADATA, FAULT_RECORD, VALIDATION_REPORT, PROCESS_OUTPUT |
| `SnapshotSource` | ORIGINAL, SANDBOX |
| `AnomalySource` | METRIC, LOG, TRACE, SPAN, HEALTH_CHECK, DEPLOYMENT, CONFIGURATION, GRAPH, COMPOSITE, UNKNOWN |
| `BaselineStrategy` | STATIC, ROLLING |
| `CodeVersionStatus` | RESOLVED, UNRESOLVED, UNKNOWN |
| `SnapshotStatus` | CREATED, INDEXING, READY, PARTIAL, FAILED |
| `RepositoryIndexStatus` | PENDING, INDEXING, INDEXED, PARTIAL, FAILED |
| `LocationValidation` | VALID, NOT_FOUND, OUT_OF_SNAPSHOT, LINE_OUT_OF_RANGE, AMBIGUOUS, STALE |
| `DebugSessionStatus` | CREATED, CONTEXT_BUILDING, ANALYZING, WAITING_FOR_VALIDATION, COMPLETED, FAILED, CANCELLED |
| `DebugAnalysisStatus` | PENDING, RUNNING, COMPLETED, DEGRADED, FAILED, CANCELLED, LIMIT_REACHED |
| `HypothesisValidationStatus` | UNVERIFIED, SUPPORTED, PARTIALLY_SUPPORTED, WEAKENED, REFUTED, INVALID_REFERENCE |
| `EvidencePolarity` | SUPPORTING, CONTRADICTING, NEUTRAL |
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
| `ForecastHorizon` | ONE_HOUR, SIX_HOURS, TWENTY_FOUR_HOURS, SEVEN_DAYS |
| `PredictionType` | FAILURE_RISK, ERROR_RATE_RISK, LATENCY_RISK, AVAILABILITY_RISK, RESOURCE_EXHAUSTION_RISK, DEPENDENCY_FAILURE_RISK, REGRESSION_RISK, INCIDENT_RISK, RELIABILITY_DEGRADATION |
| `ForecastRiskLevel` | LOW, MEDIUM, HIGH, CRITICAL, UNKNOWN |
| `ForecastStatus` | GENERATED, ACTIVE, EVALUATED, EXPIRED, SUPPRESSED |
| `DataCoverage` | GOOD, PARTIAL, POOR, INSUFFICIENT |
| `CalibrationStatus` | GOOD, ACCEPTABLE, POOR, UNKNOWN |
| `PredictionOutcomeType` | TRUE_POSITIVE, FALSE_POSITIVE, TRUE_NEGATIVE, FALSE_NEGATIVE, INCONCLUSIVE |
| `ReliabilityModelType` | ROLLING_TREND, EWMA, THRESHOLD_TRAJECTORY, HISTORICAL_FREQUENCY, LOGISTIC_REGRESSION, GRADIENT_BOOSTED_TREES, TIME_SERIES, SURVIVAL |
| `ReliabilityModelStatus` | DEVELOPMENT, VALIDATED, ACTIVE, RETIRED |
| `PredictiveSignalType` | ERROR_RATE_INCREASING, LATENCY_INCREASING, RESOURCE_SATURATION, DEPENDENCY_DEGRADATION, FAILURE_FREQUENCY_INCREASING, DEPLOYMENT_INSTABILITY, RECURRENT_INCIDENT_PATTERN, CODE_CHURN_RISK, RECENT_REGRESSION_SIGNAL, ANOMALY_CLUSTER |
| `SignalSeverity` | LOW, MEDIUM, HIGH, CRITICAL |
| `DriftKind` / `DriftStatus` | per-domain (see `models/reliability.py`) — status is STABLE, WATCH, FLAGGED |
| `EarlyWarningStatus` | OPEN, ACKNOWLEDGED, DISMISSED, EXPIRED |

Pydantic validates these enums at the API boundary, so malformed values are rejected with 422.

## 5. Indexes & performance

- `slug` unique on projects; indexed FKs on `project_id`, `environment_id`, `component_id`
- Timestamp columns are eligible for range queries (`start_time`, `timestamp`, `detected_at`) with pagination on every list endpoint
- No endpoint returns unbounded observability data (`page_size` ≤ 100)
- Causal analysis is bounded on every axis: evidence window, candidate budget,
  edge budget, trace cap, dependency hops and evidence cap are all settings, so
  no query ever scans the whole telemetry database for one incident
- Reproduction is bounded too: replay requests, inputs per plan, repetitions,
  telemetry signals and telemetry bytes all have ceilings, and one repetition's
  sandbox never shares a directory or a socket with another's
- Code intelligence has the same discipline: files per snapshot, bytes per file,
  list sizes and search results are all capped, the model tool surface is
  read-only and budgeted (`DEBUG_MAX_TOOL_CALLS`, `DEBUG_MAX_ANALYSIS_SECONDS`),
  and every index/search/query runs inside one snapshot's rows
- Predictive reliability is bounded as well: eligible components per run, metric
  series per scope, samples per series and signals per forecast all have ceilings,
  forecasts are indexed on their scope and generation time, and a heatmap query is
  limited rather than unbounded

## 6. Migrations

Schema is managed by Alembic (`apps/api/alembic/`). See [docs/development.md](docs/development.md).

## 7. Testing with SQLite

`JSONType` maps to `JSONB` on PostgreSQL and `JSON` on SQLite, so the full model runs against a file-backed SQLite database in tests (fast, isolated). Enum columns behave identically on both backends via SQLAlchemy `Enum` with `native_enum` semantics compatible in both.
