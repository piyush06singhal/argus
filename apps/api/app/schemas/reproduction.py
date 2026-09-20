"""ARGUS Phase 5 — Failure Reproduction Schemas (§44–§50).

Two rules shape these schemas:

1. **The result and the verdict are separate fields.** ``result`` says what the
   sandbox did; ``outcome`` says what that means for the hypothesis. Collapsing
   them into one "confidence" is exactly the fake certainty §29 forbids.
2. **Nothing executable crosses the boundary.** A client may name a *logical*
   sandbox service (``datastore``) and a *typed* fault; it can never supply a
   URL, an image, a shell command, or an arbitrary parameter blob that reaches
   a process. The allowlists are Pydantic-validated here, and re-validated in
   the replay engine before anything runs (§56, §57).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import Field, field_validator, model_validator

from app.models.causal import ConfidenceLevel
from app.models.reproduction import (
    ArtifactType,
    ExperimentStatus,
    FailureClass,
    FaultStatus,
    FaultTrigger,
    FaultType,
    ObservationSignal,
    ObservationStatus,
    ReplayInputSource,
    ReplayMode,
    ReplayStatus,
    ReproductionResult,
    ReproductionStrategy,
    RunStatus,
    SandboxBackendKind,
    SandboxNetworkPolicy,
    SandboxStatus,
    SnapshotSource,
    ValidationOutcome,
)
from app.schemas.base import BaseSchema, IDMixin, TimestampMixin

REPRODUCTION_ENGINE_VERSION = "phase5-reproduction-v1"

#: A sandbox target is a *logical service name* — never a host, URL, path, or
#: shell fragment. Anchored and narrow on purpose (§57).
_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

#: Parameter keys that could smuggle execution into a fault spec.
_FORBIDDEN_PARAM_KEYS = {
    "command",
    "cmd",
    "shell",
    "script",
    "exec",
    "argv",
    "path",
    "mount",
    "volume",
    "image",
    "env",
    "credential",
    "credentials",
    "token",
    "secret",
    "password",
    "url",
    "host",
}


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
#: Triggers ARGUS can actually drive inside a sandbox. A sandbox fault is
#: installed by the orchestrator, so a "manual" or "on request count" trigger has
#: no actor to fire it — allowing those would create a fault that is recorded as
#: injected but never activates.
_DRIVABLE_TRIGGERS = {
    FaultTrigger.IMMEDIATE,
    FaultTrigger.AFTER_REPLAY_INDEX,
    FaultTrigger.AT_OFFSET,
}


def _trigger_value(trigger: Any) -> str:
    """The plain name of a trigger, whatever form it arrives in.

    ``BaseSchema`` sets ``use_enum_values=True``, so by the time a model
    validator runs the field is already the plain string (``"AT_OFFSET"``).
    Comparing it with ``is`` against an enum member would silently never match —
    which is how a required-field rule becomes decorative, and how a
    ``.value`` access turns a 422 into a 500.
    """
    return str(getattr(trigger, "value", trigger))


class FaultSpecRequest(BaseSchema):
    """One requested fault injection — typed, bounded, sandbox-scoped (§21).

    There is no field for "where to run it": the target is a service name in the
    experiment's own sandbox and the scope is fixed to ``sandbox``.
    """

    fault_type: FaultType
    target: str = Field(..., max_length=64)
    trigger: FaultTrigger = FaultTrigger.IMMEDIATE
    duration_ms: Optional[int] = Field(None, ge=0, le=600_000)
    intensity: Optional[float] = Field(None, ge=0.0, le=1.0)
    parameters: Optional[dict[str, Any]] = None
    #: Required when ``trigger`` is ``AFTER_REPLAY_INDEX``.
    after_replay_index: Optional[int] = Field(None, ge=0, le=10_000)
    #: Required when ``trigger`` is ``AT_OFFSET``.
    at_offset_ms: Optional[int] = Field(None, ge=0, le=600_000)

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        if not _SERVICE_NAME_RE.match(value):
            raise ValueError(
                "Fault target must be a sandbox service name "
                "(lowercase letters, digits, '_' or '-'), not a host or command"
            )
        return value

    @model_validator(mode="after")
    def _validate_trigger(self) -> "FaultSpecRequest":
        trigger = _trigger_value(self.trigger)
        drivable = {_trigger_value(item) for item in _DRIVABLE_TRIGGERS}
        if trigger not in drivable:
            allowed = ", ".join(sorted(drivable))
            raise ValueError(
                f"The {trigger} trigger cannot be driven inside an experiment; "
                f"use one of: {allowed}"
            )
        if trigger == FaultTrigger.AFTER_REPLAY_INDEX.value:
            if self.after_replay_index is None:
                raise ValueError(
                    "An AFTER_REPLAY_INDEX fault requires after_replay_index"
                )
        elif self.after_replay_index is not None:
            raise ValueError(
                "after_replay_index is only meaningful for an AFTER_REPLAY_INDEX fault"
            )
        if trigger == FaultTrigger.AT_OFFSET.value:
            if self.at_offset_ms is None:
                raise ValueError("An AT_OFFSET fault requires at_offset_ms")
        elif self.at_offset_ms is not None:
            raise ValueError("at_offset_ms is only meaningful for an AT_OFFSET fault")
        return self

    @field_validator("parameters")
    @classmethod
    def _validate_parameters(
        cls, value: Optional[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        if value is None:
            return None
        if len(value) > 16:
            raise ValueError("Fault parameters are limited to 16 keys")
        for key, item in value.items():
            if key.lower() in _FORBIDDEN_PARAM_KEYS:
                raise ValueError(f"Fault parameter {key!r} is not permitted")
            if isinstance(item, (dict, list)):
                raise ValueError(f"Fault parameter {key!r} must be a scalar")
            if isinstance(item, str) and len(item) > 256:
                raise ValueError(f"Fault parameter {key!r} is too long")
        return value


class ReplayInputRequest(BaseSchema):
    """An engineer-supplied synthetic replay item.

    Only a logical sandbox service, an HTTP method and a JSON body are
    accepted; the body is sanitized before it is ever stored or sent (§16).
    """

    method: str = Field("POST", max_length=16)
    target_service: str = Field(..., max_length=64)
    target_path: str = Field(..., max_length=512)
    payload: Optional[dict[str, Any]] = None
    relative_offset_ms: int = Field(0, ge=0, le=600_000)
    source: ReplayInputSource = ReplayInputSource.SYNTHETIC

    @field_validator("method")
    @classmethod
    def _validate_method(cls, value: str) -> str:
        upper = value.upper()
        if upper not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            raise ValueError(f"Unsupported HTTP method: {value!r}")
        return upper

    @field_validator("target_service")
    @classmethod
    def _validate_target_service(cls, value: str) -> str:
        if not _SERVICE_NAME_RE.match(value):
            raise ValueError("target_service must be a sandbox service name")
        return value

    @field_validator("target_path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("target_path must be an absolute path inside the sandbox")
        if "//" in value or ".." in value or ":" in value:
            raise ValueError("target_path must not contain '..', '//' or ':'")
        return value


class CreateReproductionRequest(BaseSchema):
    """Plan (and optionally start) an experiment for an incident (§44, §46).

    Only safe knobs are exposed: the hypothesis to test, how many repetitions,
    how the replay is dispatched, and — for advanced use — explicit fault and
    input specifications. Everything else is derived server-side from the
    incident and its causal analysis.
    """

    # ``default=`` is spelled out on purpose: this model is constructed with no
    # arguments when a client posts an empty body ("plan it your way"), and the
    # explicit keyword is what keeps that call type-checkable.
    candidate_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Phase 4 candidate under test; defaults to the primary",
    )
    causal_analysis_id: Optional[uuid.UUID] = Field(
        default=None, description="Analysis version to test; defaults to the latest"
    )
    strategy: Optional[ReproductionStrategy] = None
    repetitions: Optional[int] = Field(default=None, ge=1, le=10)
    replay_mode: Optional[ReplayMode] = None
    network_policy: Optional[SandboxNetworkPolicy] = None
    timeout_seconds: Optional[int] = Field(default=None, ge=10, le=1800)
    faults: Optional[list[FaultSpecRequest]] = Field(default=None, max_length=16)
    inputs: Optional[list[ReplayInputRequest]] = Field(default=None, max_length=200)
    requested_by: Optional[str] = Field(default=None, max_length=255)


class StartReproductionRequest(BaseSchema):
    """Explicit confirmation to execute a planned experiment (§47).

    ``confirm_sandbox`` must be true: the API refuses to start an experiment
    merely because a plan exists, which is what makes the safety confirmation
    page real rather than decorative.
    """

    confirm_sandbox: bool = Field(
        ..., description="Must be true — execution is never implicit"
    )
    requested_by: Optional[str] = Field(None, max_length=255)

    @field_validator("confirm_sandbox")
    @classmethod
    def _must_confirm(cls, value: bool) -> bool:
        if not value:
            raise ValueError(
                "Experiments only start on explicit confirmation "
                "(confirm_sandbox=true)"
            )
        return value


class CancelReproductionRequest(BaseSchema):
    """Cancel a running experiment (§38)."""

    reason: Optional[str] = Field(None, max_length=500)


# ---------------------------------------------------------------------------
# Plan / hypothesis
# ---------------------------------------------------------------------------
class ReproductionPlanResponse(IDMixin, TimestampMixin, BaseSchema):
    experiment_id: uuid.UUID
    strategy: ReproductionStrategy
    target_component_id: Optional[uuid.UUID] = None
    target_component_name: str
    target_version: Optional[str] = None
    objectives: Optional[dict] = None
    required_services: Optional[list] = None
    required_dependencies: Optional[list] = None
    input_sources: Optional[list] = None
    expected_behavior: Optional[dict] = None
    safety_constraints: Optional[dict] = None
    resource_limits: Optional[dict] = None
    network_policy: SandboxNetworkPolicy
    timeout_seconds: int
    repetitions: int
    derived_from: Optional[dict] = None


class ReproductionHypothesisResponse(IDMixin, TimestampMixin, BaseSchema):
    source_analysis_id: Optional[uuid.UUID] = None
    candidate_id: Optional[uuid.UUID] = None
    candidate_type: Optional[str] = None
    component_id: Optional[uuid.UUID] = None
    component_name: Optional[str] = None
    statement: str
    expected_failure: Optional[str] = None
    expected_components: Optional[list] = None
    expected_sequence: Optional[list] = None
    expected_signals: Optional[list] = None
    expected_time_window_seconds: Optional[int] = None
    supporting_evidence: Optional[list] = None
    contradicted_by: Optional[list] = None


# ---------------------------------------------------------------------------
# Sandbox / run / replay / fault
# ---------------------------------------------------------------------------
class ReproductionSandboxResponse(IDMixin, TimestampMixin, BaseSchema):
    sandbox_key: str
    backend: SandboxBackendKind
    status: SandboxStatus
    network_policy: SandboxNetworkPolicy
    root_path: Optional[str] = None
    resource_limits: Optional[dict] = None
    services: Optional[dict] = None
    created_at_sandbox: Optional[datetime] = None
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    destroyed_at: Optional[datetime] = None
    cleanup_attempts: int
    cleanup_error: Optional[str] = None
    orphaned: bool


class ReproductionRunResponse(IDMixin, TimestampMixin, BaseSchema):
    run_index: int
    status: RunStatus
    result: ReproductionResult
    failure_classification: Optional[FailureClass] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    replay_request_count: int
    replay_success_count: int
    replay_failure_count: int
    replay_rejected_count: int
    observation_count: int
    telemetry_bytes: int
    error: Optional[str] = None
    faults_applied: Optional[list] = None
    notes: Optional[str] = None


class ReproductionInputResponse(IDMixin, TimestampMixin, BaseSchema):
    run_id: Optional[uuid.UUID] = None
    input_index: int
    plan_order: int
    source: ReplayInputSource
    replay_id: Optional[str] = None
    status: ReplayStatus
    method: Optional[str] = None
    target_service: Optional[str] = None
    target_path: Optional[str] = None
    target_component_id: Optional[uuid.UUID] = None
    payload_hash: Optional[str] = None
    redactions: Optional[list] = None
    relative_offset_ms: int
    original_timestamp: Optional[datetime] = None
    replay_timestamp: Optional[datetime] = None
    status_code: Optional[int] = None
    duration_ms: Optional[int] = None
    response_summary: Optional[dict] = None
    reject_reason: Optional[str] = None
    error: Optional[str] = None


class ReproductionFaultResponse(IDMixin, TimestampMixin, BaseSchema):
    fault_type: FaultType
    target: str
    scope: str
    trigger: FaultTrigger
    parameters: Optional[dict] = None
    duration_ms: Optional[int] = None
    intensity: Optional[float] = None
    status: FaultStatus
    injected: bool
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    requests_affected: int
    result: Optional[str] = None


class ReproductionObservationResponse(IDMixin, TimestampMixin, BaseSchema):
    namespace: str
    signal_type: ObservationSignal
    status: ObservationStatus
    matched_expected: bool
    observed_at: datetime
    relative_offset_ms: int
    component_name: Optional[str] = None
    source: Optional[str] = None
    metric_name: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    expected_value: Optional[float] = None
    severity: Optional[str] = None
    message: Optional[str] = None
    operation: Optional[str] = None
    duration_ms: Optional[float] = None
    error: bool
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    parent_span_id: Optional[str] = None
    attributes: Optional[dict] = None


# ---------------------------------------------------------------------------
# Comparison / validation / artifacts
# ---------------------------------------------------------------------------
class ReproductionComparisonResponse(IDMixin, TimestampMixin, BaseSchema):
    run_id: uuid.UUID
    overall_similarity: ConfidenceLevel
    similarity_score: Optional[float] = None
    result: ReproductionResult
    dimensions: Optional[dict] = None
    formula_reference: Optional[str] = None
    component_overlap: Optional[dict] = None
    matched_components: Optional[list] = None
    missing_components: Optional[list] = None
    extra_components: Optional[list] = None
    sequence_original: Optional[list] = None
    sequence_reproduced: Optional[list] = None
    sequence_match: Optional[bool] = None
    metric_deltas: Optional[dict] = None
    error_comparison: Optional[dict] = None
    trace_topology: Optional[dict] = None
    log_pattern: Optional[dict] = None
    recovery: Optional[dict] = None
    temporal: Optional[dict] = None
    original_summary: Optional[dict] = None
    reproduced_summary: Optional[dict] = None
    explanation: Optional[str] = None


class ReproductionValidationResponse(IDMixin, TimestampMixin, BaseSchema):
    candidate_id: Optional[uuid.UUID] = None
    outcome: ValidationOutcome
    confidence: ConfidenceLevel
    summary: str
    supporting_observations: Optional[list] = None
    contradicting_observations: Optional[list] = None
    environment_differences: Optional[list] = None
    missing_inputs: Optional[list] = None
    determinism: Optional[dict] = None
    artifact_ids: Optional[list] = None
    limitations: Optional[list] = None


class ReproductionArtifactResponse(IDMixin, TimestampMixin, BaseSchema):
    run_id: Optional[uuid.UUID] = None
    artifact_type: ArtifactType
    name: str
    content_type: str
    storage_location: str
    size_bytes: int
    content_hash: str
    immutable: bool
    metadata_: Optional[dict] = Field(None, alias="metadata_")


class EnvironmentSnapshotResponse(IDMixin, TimestampMixin, BaseSchema):
    source: SnapshotSource
    label: Optional[str] = None
    captured_at: datetime
    application_version: Optional[str] = None
    schema_version: Optional[str] = None
    runtime_versions: Optional[dict] = None
    dependency_versions: Optional[dict] = None
    configuration: Optional[dict] = None
    feature_flags: Optional[dict] = None
    service_topology: Optional[dict] = None
    resource_limits: Optional[dict] = None
    sanitization: Optional[dict] = None
    content_hash: Optional[str] = None


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------
class ReproductionExperimentResponse(IDMixin, TimestampMixin, BaseSchema):
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    incident_id: uuid.UUID
    causal_analysis_id: Optional[uuid.UUID] = None
    candidate_id: Optional[uuid.UUID] = None
    experiment_version: int
    status: ExperimentStatus
    result: ReproductionResult
    confidence: ConfidenceLevel
    trigger: Optional[str] = None
    requested_by: Optional[str] = None
    engine_version: Optional[str] = None
    repetitions: int
    completed_runs: int
    telemetry_namespace: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    timeout_at: Optional[datetime] = None
    cancel_requested_at: Optional[datetime] = None
    summary: Optional[str] = None
    failure_classification: Optional[FailureClass] = None


class ReproductionExperimentDetailResponse(BaseSchema):
    """Everything the investigation UI needs for one experiment (§45–§51)."""

    experiment: ReproductionExperimentResponse
    plan: Optional[ReproductionPlanResponse] = None
    hypothesis: Optional[ReproductionHypothesisResponse] = None
    runs: list[ReproductionRunResponse] = []
    validation: Optional[ReproductionValidationResponse] = None
    sandbox: Optional[ReproductionSandboxResponse] = None
    faults: list[ReproductionFaultResponse] = []
    input_count: int = 0
    available_transitions: list[str] = []
    #: Always present so a client never has to invent the caveat itself.
    disclaimer: str = (
        "A reproduction is an experiment, not a proof. A failed reproduction "
        "does not by itself refute the hypothesis."
    )


class ReproductionExperimentListResponse(BaseSchema):
    items: list[ReproductionExperimentResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class ReproductionHistoryEntry(BaseSchema):
    """One row of an incident's experiment history (§51)."""

    experiment_id: uuid.UUID
    experiment_version: int
    status: ExperimentStatus
    result: ReproductionResult
    outcome: Optional[ValidationOutcome] = None
    confidence: ConfidenceLevel
    duration_ms: Optional[int] = None
    repetitions: int
    created_at: datetime
    completed_at: Optional[datetime] = None
    hypothesis: Optional[str] = None
    summary: Optional[str] = None


class ReproductionHistoryResponse(BaseSchema):
    incident_id: uuid.UUID
    items: list[ReproductionHistoryEntry]
    total: int


class ExperimentProgress(BaseSchema):
    """Progress along the §37 lifecycle, for the live experiment page (§48)."""

    status: ExperimentStatus
    step: int
    total_steps: int
    percent: int
    is_terminal: bool
    elapsed_seconds: Optional[float] = None


class ResourceUsage(BaseSchema):
    """Reported resource limits/usage, so nothing about an experiment is hidden."""

    limits: Optional[dict] = None
    observed: Optional[dict] = None


class ReproductionMetricsResponse(BaseSchema):
    """Observability of ARGUS's *own* reproduction work (§54).

    Counts and durations, derived from stored rows at read time. A counter kept
    in memory would report a healthy zero while sandboxes accumulated, so nothing
    here is a cached tally.
    """

    project_id: uuid.UUID
    experiments: dict[str, int] = Field(default_factory=dict)
    results: dict[str, int] = Field(default_factory=dict)
    runs_completed: int = 0
    runs_failed: int = 0
    sandboxes_total: int = 0
    sandboxes_destroyed: int = 0
    live_sandboxes: int = 0
    orphaned_sandboxes: int = 0
    cleanup_failures: int = 0
    durations_ms: dict[str, Optional[int]] = Field(default_factory=dict)
    failures_by_class: dict[str, int] = Field(default_factory=dict)
    backend: str = "local"
    network_policy: str = "ISOLATED"
    sandbox_disk: dict[str, Any] = Field(default_factory=dict)


class ReproductionStatusResponse(BaseSchema):
    """Live status of one experiment (§44, §48)."""

    experiment_id: uuid.UUID
    status: ExperimentStatus
    result: ReproductionResult
    progress: ExperimentProgress
    sandbox: Optional[ReproductionSandboxResponse] = None
    runs_completed: int
    repetitions: int
    replay_total: int
    replay_completed: int
    faults_active: int
    faults_total: int
    resources: ResourceUsage
    latest_run: Optional[ReproductionRunResponse] = None
    cancel_requested: bool
    timeout_at: Optional[datetime] = None


class ReproductionTelemetryResponse(BaseSchema):
    """Captured reproduction telemetry, always namespaced (§23, §24)."""

    experiment_id: uuid.UUID
    namespace: str
    isolation_note: str = (
        "This telemetry was captured from an isolated sandbox in its own "
        "namespace. It is never mixed with production telemetry."
    )
    items: list[ReproductionObservationResponse]
    total: int
    expected_count: int
    matched_count: int
    missing_count: int


class ReproductionArtifactListResponse(BaseSchema):
    experiment_id: uuid.UUID
    items: list[ReproductionArtifactResponse]
    total: int


class ReproductionFaultListResponse(BaseSchema):
    experiment_id: uuid.UUID
    items: list[ReproductionFaultResponse]
    total: int
    injected_total: int


class ReproductionComparisonListResponse(BaseSchema):
    experiment_id: uuid.UUID
    items: list[ReproductionComparisonResponse]
    total: int
    aggregate: Optional[dict] = None


class ReproductionManifestResponse(BaseSchema):
    """The manifest of an experiment (§43) — no environment is opaque."""

    experiment_id: uuid.UUID
    experiment_version: int
    status: ExperimentStatus
    application_version: Optional[str] = None
    strategy: Optional[ReproductionStrategy] = None
    services: list[str] = []
    dependencies: list[str] = []
    inputs: list[dict] = []
    faults: list[dict] = []
    repetitions: int = 1
    network_policy: Optional[SandboxNetworkPolicy] = None
    resource_limits: Optional[dict] = None
    timeout_seconds: Optional[int] = None
    artifact_hashes: list[dict] = []


class ReproductionSafetyPreviewResponse(BaseSchema):
    """The §47 confirmation payload — shown *before* anything is executed."""

    experiment_id: uuid.UUID
    sandbox: str
    backend: SandboxBackendKind
    network_policy: SandboxNetworkPolicy
    production_access: str
    credentials: str
    resource_limits: Optional[dict] = None
    timeout_seconds: int
    repetitions: int
    services: list[str] = []
    faults: list[dict] = []
    warnings: list[str] = []
    can_start: bool
    blocked_reasons: list[str] = []


__all__ = [
    "REPRODUCTION_ENGINE_VERSION",
    "CancelReproductionRequest",
    "CreateReproductionRequest",
    "EnvironmentSnapshotResponse",
    "ExperimentProgress",
    "FaultSpecRequest",
    "ReplayInputRequest",
    "ReproductionArtifactListResponse",
    "ReproductionArtifactResponse",
    "ReproductionComparisonListResponse",
    "ReproductionComparisonResponse",
    "ReproductionExperimentDetailResponse",
    "ReproductionExperimentListResponse",
    "ReproductionExperimentResponse",
    "ReproductionFaultListResponse",
    "ReproductionFaultResponse",
    "ReproductionHistoryEntry",
    "ReproductionHistoryResponse",
    "ReproductionHypothesisResponse",
    "ReproductionInputResponse",
    "ReproductionManifestResponse",
    "ReproductionObservationResponse",
    "ReproductionPlanResponse",
    "ReproductionRunResponse",
    "ReproductionSafetyPreviewResponse",
    "ReproductionSandboxResponse",
    "ReproductionStatusResponse",
    "ReproductionTelemetryResponse",
    "ReproductionValidationResponse",
    "ResourceUsage",
    "StartReproductionRequest",
]
