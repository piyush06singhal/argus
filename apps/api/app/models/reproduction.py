"""ARGUS Failure Reproduction Models (Phase 5).

Phase 5 lets ARGUS run a **controlled experiment**: take a Phase 4 root-cause
hypothesis, build an isolated sandbox that represents the relevant system state,
replay sanitized inputs, inject controlled faults, capture real telemetry, and
compare what happened against the original incident.

The epistemic boundary this module enforces in structure (not just prose):

* an experiment is an **observation**, never a verdict — ``result`` and
  ``outcome`` are different columns on different tables because "the failure
  reproduced" and "the hypothesis is therefore true" are different claims;
* a failed reproduction may be ``INCONCLUSIVE``: ``failure_classification``
  records *why* the sandbox did not fail (environment error, timeout, missing
  dependency, non-determinism) so a null result can never be read as a
  refutation;
* every artifact is content-hashed and immutable after the experiment, and every
  fault is recorded so "it reproduced naturally" and "we forced it" can never be
  confused (§22);
* reproduction telemetry carries its own namespace, so it can never be mistaken
  for production telemetry (§24).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, Guid as UUID, JSONType

#: Bounded confidence buckets are reused from Phase 4 (same PG enum type) so a
#: reproduction verdict and a causal confidence are directly comparable and
#: neither can drift into a different scale.
from app.models.causal import ConfidenceLevel

if TYPE_CHECKING:
    from app.models.incident import Incident


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class ExperimentStatus(str, enum.Enum):
    """Lifecycle of one reproduction experiment (§6, §37).

    The order is the *happy path*: planned → validating → provisioning → ready
    → replaying → running → collecting → comparing → completed. Failure exits
    are terminal.
    """

    PLANNED = "PLANNED"
    VALIDATING = "VALIDATING"
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    REPLAYING = "REPLAYING"
    RUNNING = "RUNNING"
    COLLECTING = "COLLECTING"
    COMPARING = "COMPARING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class ReproductionResult(str, enum.Enum):
    """What the experiment observed (§30).

    Deliberately *not* "true"/"false": ``FAILED`` means the expected failure did
    not appear in the sandbox, which is a statement about the sandbox, not about
    the hypothesis.
    """

    SUCCESSFUL = "SUCCESSFUL"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_RUN = "NOT_RUN"


class ReproductionStrategy(str, enum.Enum):
    """The controlled mechanism a plan uses (§9)."""

    SYNTHETIC_INPUT_REPLAY = "SYNTHETIC_INPUT_REPLAY"
    EVENT_REPLAY = "EVENT_REPLAY"
    DEPENDENCY_FAULT = "DEPENDENCY_FAULT"
    CONFIGURATION_REPLAY = "CONFIGURATION_REPLAY"
    STATE_SNAPSHOT = "STATE_SNAPSHOT"


class SandboxBackendKind(str, enum.Enum):
    """How a sandbox is isolated (§10, §11)."""

    LOCAL_PROCESS = "LOCAL_PROCESS"
    DOCKER = "DOCKER"


class SandboxStatus(str, enum.Enum):
    """Sandbox lifecycle. ``DESTROYED`` is the only acceptable end state."""

    CREATING = "CREATING"
    READY = "READY"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    DESTROYED = "DESTROYED"
    FAILED = "FAILED"


class SandboxNetworkPolicy(str, enum.Enum):
    """Sandbox egress policy (§13). Default deny."""

    ISOLATED = "ISOLATED"
    MOCK_DEPENDENCIES = "MOCK_DEPENDENCIES"
    CONTROLLED_EGRESS = "CONTROLLED_EGRESS"


class RunStatus(str, enum.Enum):
    """Lifecycle of one repetition within an experiment (§34, §35)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class FailureClass(str, enum.Enum):
    """Why an experiment did not produce a usable observation (§36)."""

    ENVIRONMENT_ERROR = "ENVIRONMENT_ERROR"
    INPUT_ERROR = "INPUT_ERROR"
    TIMEOUT = "TIMEOUT"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    SANDBOX_ERROR = "SANDBOX_ERROR"
    APPLICATION_FAILURE = "APPLICATION_FAILURE"
    NO_FAILURE_OBSERVED = "NO_FAILURE_OBSERVED"
    INSUFFICIENT_TELEMETRY = "INSUFFICIENT_TELEMETRY"
    UNKNOWN = "UNKNOWN"


class ReplayInputSource(str, enum.Enum):
    """Where a replayed input came from (§17)."""

    HTTP_REQUEST = "HTTP_REQUEST"
    EVENT = "EVENT"
    MESSAGE = "MESSAGE"
    TRACE_INPUT = "TRACE_INPUT"
    SYNTHETIC = "SYNTHETIC"


class ReplayStatus(str, enum.Enum):
    """Outcome of one replay item. ``REJECTED`` means safety validation won."""

    PENDING = "PENDING"
    SENT = "SENT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    SKIPPED = "SKIPPED"


class ReplayMode(str, enum.Enum):
    """How replay items are dispatched (§19)."""

    SEQUENTIAL = "SEQUENTIAL"
    PARALLEL = "PARALLEL"
    TIMED = "TIMED"
    BURST = "BURST"
    RATE_LIMITED = "RATE_LIMITED"


class FaultType(str, enum.Enum):
    """Controlled faults available inside a sandbox (§21)."""

    LATENCY = "LATENCY"
    TIMEOUT = "TIMEOUT"
    HTTP_4XX = "HTTP_4XX"
    HTTP_5XX = "HTTP_5XX"
    CONNECTION_FAILURE = "CONNECTION_FAILURE"
    RESPONSE_CORRUPTION = "RESPONSE_CORRUPTION"
    RESOURCE_PRESSURE = "RESOURCE_PRESSURE"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"


class FaultTrigger(str, enum.Enum):
    """When an injected fault becomes active."""

    IMMEDIATE = "IMMEDIATE"
    AFTER_REPLAY_INDEX = "AFTER_REPLAY_INDEX"
    AT_OFFSET = "AT_OFFSET"
    ON_REQUEST_COUNT = "ON_REQUEST_COUNT"
    MANUAL = "MANUAL"


class FaultStatus(str, enum.Enum):
    """Fault lifecycle — every fault is audited (§22)."""

    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ObservationSignal(str, enum.Enum):
    """Kind of captured reproduction telemetry (§25)."""

    SPAN = "SPAN"
    TRACE = "TRACE"
    LOG = "LOG"
    METRIC = "METRIC"
    HEALTH = "HEALTH"
    EVENT = "EVENT"
    DEPLOYMENT = "DEPLOYMENT"
    CONFIGURATION = "CONFIGURATION"


class ObservationStatus(str, enum.Enum):
    """Whether a captured signal matched what the plan expected."""

    EXPECTED = "EXPECTED"
    UNEXPECTED = "UNEXPECTED"
    NEUTRAL = "NEUTRAL"
    MISSING = "MISSING"


class ValidationOutcome(str, enum.Enum):
    """What the experiment says about the hypothesis (§31).

    ``INCONCLUSIVE`` is a first-class success of the process: it means ARGUS
    learned that it cannot yet decide, and knows why.
    """

    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class ArtifactType(str, enum.Enum):
    """Auditable outputs of an experiment (§41)."""

    ENVIRONMENT_SNAPSHOT = "ENVIRONMENT_SNAPSHOT"
    REPRODUCTION_PLAN = "REPRODUCTION_PLAN"
    REPRODUCTION_MANIFEST = "REPRODUCTION_MANIFEST"
    REPLAY_MANIFEST = "REPLAY_MANIFEST"
    TELEMETRY_SNAPSHOT = "TELEMETRY_SNAPSHOT"
    LOGS = "LOGS"
    TRACE_SUMMARY = "TRACE_SUMMARY"
    COMPARISON_RESULT = "COMPARISON_RESULT"
    SANDBOX_METADATA = "SANDBOX_METADATA"
    FAULT_RECORD = "FAULT_RECORD"
    VALIDATION_REPORT = "VALIDATION_REPORT"
    PROCESS_OUTPUT = "PROCESS_OUTPUT"


class SnapshotSource(str, enum.Enum):
    """Which side of the comparison a snapshot describes (§33)."""

    ORIGINAL = "ORIGINAL"
    SANDBOX = "SANDBOX"


class ComparisonDimension(str, enum.Enum):
    """The independent lenses the comparator scores (§27, §28).

    Kept as an enum (not free strings) so the UI and the docs can never drift
    from the dimensions the comparator actually computes.
    """

    TEMPORAL = "TEMPORAL"
    COMPONENT = "COMPONENT"
    ERROR = "ERROR"
    LATENCY = "LATENCY"
    TRACE_TOPOLOGY = "TRACE_TOPOLOGY"
    LOG_PATTERN = "LOG_PATTERN"
    FAILURE_SEQUENCE = "FAILURE_SEQUENCE"
    RECOVERY = "RECOVERY"


# ---------------------------------------------------------------------------
# ReproductionExperiment — the experiment envelope (§6)
# ---------------------------------------------------------------------------
class ReproductionExperiment(BaseModel):
    """One controlled attempt to reproduce one incident's hypothesised failure.

    Experiments are versioned per ``(incident, hypothesis)`` so a re-run is a
    new row and the history stays auditable (§51). ``candidate_id`` is the
    Phase 4 hypothesis under test; when it is null the experiment tests the
    analysis' primary candidate and records that in ``metadata_``.
    """

    __tablename__ = "reproduction_experiments"
    __table_args__ = (
        Index(
            "ix_repro_experiments_incident_version",
            "incident_id",
            "experiment_version",
        ),
        Index("ix_repro_experiments_project_status", "project_id", "status"),
        Index("ix_repro_experiments_candidate", "candidate_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("environments.id", ondelete="CASCADE"), nullable=True
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    causal_analysis_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("causal_analyses.id", ondelete="SET NULL"), nullable=True
    )
    candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("root_cause_candidates.id", ondelete="SET NULL"),
        nullable=True,
    )

    experiment_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[ExperimentStatus] = mapped_column(
        SAEnum(ExperimentStatus, name="experimentstatus"),
        nullable=False,
        default=ExperimentStatus.PLANNED,
    )
    result: Mapped[ReproductionResult] = mapped_column(
        SAEnum(ReproductionResult, name="reproductionresult"),
        nullable=False,
        default=ReproductionResult.NOT_RUN,
    )
    #: Coarse confidence in the *result*, never a probability (§29).
    confidence: Mapped[ConfidenceLevel] = mapped_column(
        SAEnum(ConfidenceLevel, name="confidencelevel"),
        nullable=False,
        default=ConfidenceLevel.INSUFFICIENT,
    )
    trigger: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    requested_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    #: The engine version that produced this row (audit, §6).
    engine_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    repetitions: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    completed_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Telemetry namespace — reproduction rows can never be read as production
    #: telemetry while this is set (§24).
    telemetry_namespace: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Hard deadline. A reaper sweeps experiments past it even if the worker
    #: that owned them died (§39).
    timeout_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_classification: Mapped[Optional[FailureClass]] = mapped_column(
        SAEnum(FailureClass, name="failureclass"), nullable=True
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    incident: Mapped["Incident"] = relationship("Incident")
    runs: Mapped[List["ReproductionRun"]] = relationship(
        back_populates="experiment",
        cascade="all, delete-orphan",
        order_by="ReproductionRun.run_index",
    )
    faults: Mapped[List["ReproductionFault"]] = relationship(
        back_populates="experiment",
        cascade="all, delete-orphan",
        order_by="ReproductionFault.created_at",
    )


# ---------------------------------------------------------------------------
# ReproductionPlan — what will be run, and why (§7)
# ---------------------------------------------------------------------------
class ReproductionPlan(BaseModel):
    """An explicit, inspectable plan — nothing about an experiment is hidden.

    ``expected_behavior`` is **derived** from the incident's own telemetry by
    the planner (§26); it is never hand-written, because a fabricated
    expectation would make every comparison meaningless.
    """

    __tablename__ = "reproduction_plans"
    __table_args__ = (Index("ix_repro_plans_experiment", "experiment_id", unique=True),)

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("reproduction_experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    strategy: Mapped[ReproductionStrategy] = mapped_column(
        SAEnum(ReproductionStrategy, name="reproductionstrategy"),
        nullable=False,
        default=ReproductionStrategy.DEPENDENCY_FAULT,
    )
    target_component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("system_components.id", ondelete="SET NULL"), nullable=True
    )
    #: Denormalised label so a plan stays readable after component deletion.
    target_component_name: Mapped[str] = mapped_column(String(255), nullable=False)
    target_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    #: Why we are doing this: hypothesis statement, evidence ids, objective.
    objectives: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    required_services: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    required_dependencies: Mapped[Optional[list]] = mapped_column(
        JSONType, nullable=True
    )
    input_sources: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    #: Derived from the incident: components, sequence, signals, thresholds.
    expected_behavior: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    #: What the plan promises *not* to do (§12): no production contact, etc.
    safety_constraints: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    resource_limits: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    network_policy: Mapped[SandboxNetworkPolicy] = mapped_column(
        SAEnum(SandboxNetworkPolicy, name="sandboxnetworkpolicy"),
        nullable=False,
        default=SandboxNetworkPolicy.ISOLATED,
    )
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    repetitions: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: Provenance: which incident rows the plan was derived from.
    derived_from: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    experiment: Mapped["ReproductionExperiment"] = relationship(
        "ReproductionExperiment"
    )


# ---------------------------------------------------------------------------
# ReproductionHypothesis — the claim under test (§8)
# ---------------------------------------------------------------------------
class ReproductionHypothesis(BaseModel):
    """One Phase 4 candidate restated as a falsifiable experiment (§8).

    The expected sequence/signals are copied from the causal analysis and its
    incident evidence at plan time, so the experiment is compared against the
    hypothesis *as it was stated*, not a rewritten version of it.
    """

    __tablename__ = "reproduction_hypotheses"
    __table_args__ = (
        Index("ix_repro_hypotheses_experiment", "experiment_id", unique=True),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("reproduction_experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    source_analysis_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("causal_analyses.id", ondelete="SET NULL"), nullable=True
    )
    candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("root_cause_candidates.id", ondelete="SET NULL"),
        nullable=True,
    )
    candidate_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("system_components.id", ondelete="SET NULL"), nullable=True
    )
    component_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    #: Human-readable claim, e.g. "PostgreSQL latency caused Inventory timeouts".
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    expected_failure: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expected_components: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    expected_sequence: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    expected_signals: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    expected_time_window_seconds: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    supporting_evidence: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    contradicted_by: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)

    experiment: Mapped["ReproductionExperiment"] = relationship(
        "ReproductionExperiment"
    )


# ---------------------------------------------------------------------------
# ReproductionSandbox — the disposable environment (§11, §55)
# ---------------------------------------------------------------------------
class ReproductionSandbox(BaseModel):
    """A provisioned, disposable sandbox and its cleanup state.

    ``cleanup_attempts``/``cleanup_error`` exist because §55 is mandatory: a
    sandbox that fails to clean up must be *visible* as a failure (and retried
    by the reaper), never silently leaked.
    """

    __tablename__ = "reproduction_sandboxes"
    __table_args__ = (
        Index("ix_repro_sandboxes_experiment", "experiment_id"),
        Index("ix_repro_sandboxes_status", "status"),
        Index("ix_repro_sandboxes_key", "sandbox_key", unique=True),
    )

    experiment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("reproduction_experiments.id", ondelete="CASCADE"),
        nullable=True,
    )
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )

    sandbox_key: Mapped[str] = mapped_column(String(128), nullable=False)
    backend: Mapped[SandboxBackendKind] = mapped_column(
        SAEnum(SandboxBackendKind, name="sandboxbackendkind"),
        nullable=False,
        default=SandboxBackendKind.LOCAL_PROCESS,
    )
    status: Mapped[SandboxStatus] = mapped_column(
        SAEnum(SandboxStatus, name="sandboxstatus"),
        nullable=False,
        default=SandboxStatus.CREATING,
    )
    network_policy: Mapped[SandboxNetworkPolicy] = mapped_column(
        SAEnum(SandboxNetworkPolicy, name="sandboxnetworkpolicy"),
        nullable=False,
        default=SandboxNetworkPolicy.ISOLATED,
    )

    root_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    resource_limits: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    #: name → {port, pid|container_id, health, ...}
    services: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    process_ids: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    container_ids: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)

    created_at_sandbox: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stopped_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    destroyed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    cleanup_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cleanup_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Set when the sandbox could not be torn down — drives the orphan metric.
    orphaned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


# ---------------------------------------------------------------------------
# ReproductionRun — one repetition (§34, §35)
# ---------------------------------------------------------------------------
class ReproductionRun(BaseModel):
    """A single execution of the plan. Repeatability lives here.

    Repetitions are how ARGUS observes non-determinism (§34): three failures out
    of four runs is an **experimental observation** (``reproduction_rate``), and
    the model refuses to promote it into a causal probability.
    """

    __tablename__ = "reproduction_runs"
    __table_args__ = (
        Index("ix_repro_runs_experiment_index", "experiment_id", "run_index"),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("reproduction_experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    sandbox_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("reproduction_sandboxes.id", ondelete="SET NULL"),
        nullable=True,
    )

    run_index: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[RunStatus] = mapped_column(
        SAEnum(RunStatus, name="runstatus"), nullable=False, default=RunStatus.PENDING
    )
    result: Mapped[ReproductionResult] = mapped_column(
        SAEnum(ReproductionResult, name="reproductionresult"),
        nullable=False,
        default=ReproductionResult.NOT_RUN,
    )
    failure_classification: Mapped[Optional[FailureClass]] = mapped_column(
        SAEnum(FailureClass, name="failureclass"), nullable=True
    )

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    replay_request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    replay_success_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    replay_failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    replay_rejected_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    telemetry_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    faults_applied: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    experiment: Mapped["ReproductionExperiment"] = relationship(
        "ReproductionExperiment", back_populates="runs"
    )
    comparisons: Mapped[List["ReproductionComparison"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# ReproductionInput — a sanitized replay item and its outcome (§17, §20)
# ---------------------------------------------------------------------------
class ReproductionInput(BaseModel):
    """One replayed request/event, sanitized before it can ever be sent.

    ``payload`` holds the **sanitized** body only; ``redactions`` records what
    was replaced so the transformation is auditable. ``relative_offset_ms``
    exists because original wall-clock timestamps can never be reused directly
    in a replay (§20).
    """

    __tablename__ = "reproduction_inputs"
    __table_args__ = (
        Index("ix_repro_inputs_experiment_order", "experiment_id", "plan_order"),
        Index("ix_repro_inputs_run", "run_id"),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("reproduction_experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("reproduction_runs.id", ondelete="CASCADE"), nullable=True
    )

    input_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    plan_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[ReplayInputSource] = mapped_column(
        SAEnum(ReplayInputSource, name="replayinputsource"),
        nullable=False,
        default=ReplayInputSource.SYNTHETIC,
    )
    replay_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[ReplayStatus] = mapped_column(
        SAEnum(ReplayStatus, name="replaystatus"),
        nullable=False,
        default=ReplayStatus.PENDING,
    )

    method: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    #: Logical target inside the sandbox (never an external URL).
    target_service: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    target_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    target_component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("system_components.id", ondelete="SET NULL"), nullable=True
    )

    payload: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    payload_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    redactions: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)

    relative_offset_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    original_timestamp: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    replay_timestamp: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_summary: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    reject_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# ReproductionFault — injected fault + audit trail (§21, §22)
# ---------------------------------------------------------------------------
class ReproductionFault(BaseModel):
    """One controlled fault, with the audit record that makes it honest.

    ``injected`` is the column that stops "the sandbox failed" from being
    confused with "we made the sandbox fail": a natural reproduction and an
    injected one are different observations.
    """

    __tablename__ = "reproduction_faults"
    __table_args__ = (
        Index("ix_repro_faults_experiment", "experiment_id"),
        Index("ix_repro_faults_run", "run_id"),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("reproduction_experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("reproduction_runs.id", ondelete="SET NULL"), nullable=True
    )

    fault_type: Mapped[FaultType] = mapped_column(
        SAEnum(FaultType, name="faulttype"), nullable=False
    )
    #: Logical sandbox target, e.g. "datastore" — never a production address.
    target: Mapped[str] = mapped_column(String(128), nullable=False)
    target_component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("system_components.id", ondelete="SET NULL"), nullable=True
    )
    #: Always "sandbox": fault injection outside the sandbox is impossible.
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="sandbox")
    trigger: Mapped[FaultTrigger] = mapped_column(
        SAEnum(FaultTrigger, name="faulttrigger"),
        nullable=False,
        default=FaultTrigger.IMMEDIATE,
    )
    parameters: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    intensity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    status: Mapped[FaultStatus] = mapped_column(
        SAEnum(FaultStatus, name="faultstatus"),
        nullable=False,
        default=FaultStatus.PLANNED,
    )
    injected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    requests_affected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    experiment: Mapped["ReproductionExperiment"] = relationship(
        "ReproductionExperiment", back_populates="faults"
    )


# ---------------------------------------------------------------------------
# ReproductionObservation — captured telemetry (§23, §25, §26)
# ---------------------------------------------------------------------------
class ReproductionObservation(BaseModel):
    """One captured reproduction signal, already judged against expectation.

    A single table carries spans, logs, metrics, health, and events because the
    phase's own requirement is one comparable shape (§25); splitting them would
    force the comparator to re-implement the same join four times.
    ``status``/``matched_expected`` are set at capture time so a *missing*
    expected signal is recorded explicitly rather than being absent (§26 —
    silent absence is how a failed reproduction looks like a clean one).
    """

    __tablename__ = "reproduction_observations"
    __table_args__ = (
        Index("ix_repro_observations_experiment", "experiment_id"),
        Index("ix_repro_observations_run_signal", "run_id", "signal_type"),
        Index("ix_repro_observations_component", "component_name"),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("reproduction_experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("reproduction_runs.id", ondelete="CASCADE"), nullable=False
    )
    sandbox_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("reproduction_sandboxes.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: ``repro:<experiment_id>`` — the namespace that keeps this row separable
    #: from incident telemetry forever (§24).
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)

    signal_type: Mapped[ObservationSignal] = mapped_column(
        SAEnum(ObservationSignal, name="observationsignal"), nullable=False
    )
    status: Mapped[ObservationStatus] = mapped_column(
        SAEnum(ObservationStatus, name="observationstatus"),
        nullable=False,
        default=ObservationStatus.NEUTRAL,
    )
    matched_expected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    relative_offset_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    component_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("system_components.id", ondelete="SET NULL"), nullable=True
    )
    #: Which runner emitted it (sandbox-internal provenance).
    source: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    metric_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unit: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    expected_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    severity: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    operation: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    duration_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    span_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    parent_span_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    attributes: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)


# ---------------------------------------------------------------------------
# ReproductionComparison — original vs reproduced (§27, §28, §49)
# ---------------------------------------------------------------------------
class ReproductionComparison(BaseModel):
    """The explainable comparison of one run against the original incident.

    ``dimensions`` stores, per dimension, the computed score **and the formula
    and inputs that produced it**. §29 forbids fake precision, so this row
    always carries a coarse bucket alongside any number, and the API exposes the
    formulas so nobody has to trust an unexplained decimal.
    """

    __tablename__ = "reproduction_comparisons"
    __table_args__ = (
        Index("ix_repro_comparisons_experiment", "experiment_id"),
        Index("ix_repro_comparisons_run", "run_id"),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("reproduction_experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("reproduction_runs.id", ondelete="CASCADE"), nullable=False
    )

    #: Coarse bucket (INSUFFICIENT/LOW/MEDIUM/HIGH) — the headline number's
    #: honest companion.
    overall_similarity: Mapped[ConfidenceLevel] = mapped_column(
        SAEnum(ConfidenceLevel, name="confidencelevel"),
        nullable=False,
        default=ConfidenceLevel.INSUFFICIENT,
    )
    similarity_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    result: Mapped[ReproductionResult] = mapped_column(
        SAEnum(ReproductionResult, name="reproductionresult"),
        nullable=False,
        default=ReproductionResult.NOT_RUN,
    )

    #: Per-dimension: score, bucket, formula, inputs, notes.
    dimensions: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    formula_reference: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    component_overlap: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    matched_components: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    missing_components: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    extra_components: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    sequence_original: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    sequence_reproduced: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    sequence_match: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    metric_deltas: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    error_comparison: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    trace_topology: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    log_pattern: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    recovery: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    temporal: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    original_summary: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    reproduced_summary: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    explanation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    run: Mapped["ReproductionRun"] = relationship(
        "ReproductionRun", back_populates="comparisons"
    )


# ---------------------------------------------------------------------------
# ReproductionValidation — what the experiment says about the hypothesis (§31)
# ---------------------------------------------------------------------------
class ReproductionValidation(BaseModel):
    """The experiment's verdict on the hypothesis — with its own uncertainties.

    The result and the verdict are separate columns across separate tables on
    purpose: a ``FAILED`` reproduction with an ``INCONCLUSIVE`` verdict is the
    normal, expected combination when the sandbox differs from production, and
    the schema makes that the default reading rather than an exception.
    """

    __tablename__ = "reproduction_validations"
    __table_args__ = (
        Index("ix_repro_validations_experiment", "experiment_id", unique=True),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("reproduction_experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("root_cause_candidates.id", ondelete="SET NULL"),
        nullable=True,
    )

    outcome: Mapped[ValidationOutcome] = mapped_column(
        SAEnum(ValidationOutcome, name="validationoutcome"),
        nullable=False,
        default=ValidationOutcome.INCONCLUSIVE,
    )
    confidence: Mapped[ConfidenceLevel] = mapped_column(
        SAEnum(ConfidenceLevel, name="confidencelevel"),
        nullable=False,
        default=ConfidenceLevel.INSUFFICIENT,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    supporting_observations: Mapped[Optional[list]] = mapped_column(
        JSONType, nullable=True
    )
    contradicting_observations: Mapped[Optional[list]] = mapped_column(
        JSONType, nullable=True
    )
    environment_differences: Mapped[Optional[list]] = mapped_column(
        JSONType, nullable=True
    )
    missing_inputs: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    #: Determinism observation: reproduction_rate over N runs (§34).
    determinism: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    artifact_ids: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    #: What this experiment still cannot tell you (§65).
    limitations: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)


# ---------------------------------------------------------------------------
# ReproductionArtifact — content-addressed, immutable evidence (§41, §42)
# ---------------------------------------------------------------------------
class ReproductionArtifact(BaseModel):
    """A stored experiment artifact with a hash that proves its content.

    Artifacts are written once and never rewritten; ``immutable`` is stored so a
    later mutation attempt is a detectable anomaly rather than silent history
    rewriting (§42).
    """

    __tablename__ = "reproduction_artifacts"
    __table_args__ = (
        Index("ix_repro_artifacts_experiment", "experiment_id"),
        Index("ix_repro_artifacts_type", "artifact_type"),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("reproduction_experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("reproduction_runs.id", ondelete="SET NULL"), nullable=True
    )

    artifact_type: Mapped[ArtifactType] = mapped_column(
        SAEnum(ArtifactType, name="artifacttype"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(128), nullable=False, default="application/json"
    )
    #: Path relative to the artifact root — never an absolute host path.
    storage_location: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


# ---------------------------------------------------------------------------
# EnvironmentSnapshot — original vs sandbox environment (§14, §33)
# ---------------------------------------------------------------------------
class EnvironmentSnapshot(BaseModel):
    """A sanitized picture of an environment, for difference analysis.

    Secrets are removed before the row is written, and the redaction count is
    stored alongside so a comparison can say "these two environments were
    captured under the same sanitization" instead of assuming it (§15).
    """

    __tablename__ = "environment_snapshots"
    __table_args__ = (
        Index("ix_env_snapshots_experiment_source", "experiment_id", "source"),
    )

    experiment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("reproduction_experiments.id", ondelete="CASCADE"),
        nullable=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    source: Mapped[SnapshotSource] = mapped_column(
        SAEnum(SnapshotSource, name="snapshotsource"), nullable=False
    )
    label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    application_version: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    schema_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    runtime_versions: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    dependency_versions: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    configuration: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    environment_variables: Mapped[Optional[dict]] = mapped_column(
        JSONType, nullable=True
    )
    feature_flags: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    service_topology: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    resource_limits: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    #: Proof of sanitization: which keys/values were replaced, and how many.
    sanitization: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    snapshot_metadata: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
