"""ARGUS Safe Autonomous Remediation Models (Phase 9).

Phase 9 is the first phase that can *change* something. The module is built
around one refusal, stated the same way in every table:

    **a proposal is not an authorization.**

Everything else follows from it:

* **An action is never a command.** ``RemediationAction.action_type`` is a key
  into a code-defined registry (§3, §4); parameters are validated against that
  definition's schema before anything runs. There is no column anywhere in this
  module that holds a shell string.
* **Every gate leaves a row.** Safety assessment, policy decision, approval,
  execution attempt, verification and rollback are all separate tables. An
  action is therefore auditable end to end *even when it is refused* — the
  refusal is the interesting part, so it is recorded rather than returned.
* **Status is a state machine, not a label.** ``RemediationStatus`` is the §5
  lifecycle and the transitions between its members are validated by
  :mod:`app.services.remediation_state`; nothing here writes a status the state
  machine rejects.
* **Reversibility is declared before execution, never discovered after.** An
  action whose ``rollback_strategy`` is ``NONE`` is irreversible by definition,
  and the safety engine escalates it to human approval unconditionally (§4).
* **Audit events form a hash chain.** Each ``RemediationAuditEvent`` carries the
  digest of the event before it, so a deleted or edited row is detectable
  rather than merely absent (§12).

The runtime side of the control plane is :class:`RemediationControl`. It is what
makes ARGUS-native actions *real* rather than simulated: when the executor
pauses a background job or disables an ARGUS feature flag, the row written here
is the same row the worker and the sweeps read before they do any work.

All enum type names are namespaced ``remediation_*`` because PostgreSQL enum
types are database-global and earlier phases already created ``risklevel`` and
``risksignaltype`` with different members; reusing those names would silently
coerce Phase 9 values (§4, and the same hazard Phase 8 documented).

Ownership follows the Phase 2–8 pattern: project/environment-scoped rows cascade
with their owner; component, incident and patch references use ``ON DELETE SET
NULL`` so deleting a component never erases the record of what was done to it.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, Guid as UUID, JSONType


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RemediationActionType(str, enum.Enum):
    """The controlled action registry's members (§3).

    Deliberately a closed set. "Run this command" is not a member, and no
    string from a request body, an AI provider or a proposal can ever become
    one: the registry in :mod:`app.services.remediation_registry` is the only
    place this enum is mapped to behaviour.
    """

    #: Service-level recovery.
    RESTART_SERVICE = "RESTART_SERVICE"
    RESTART_INSTANCE = "RESTART_INSTANCE"
    #: ARGUS-owned flags. Native, reversible, autonomously eligible.
    DISABLE_FEATURE_FLAG = "DISABLE_FEATURE_FLAG"
    ENABLE_FEATURE_FLAG = "ENABLE_FEATURE_FLAG"
    #: Change rollback (external systems; adapter required).
    ROLLBACK_DEPLOYMENT = "ROLLBACK_DEPLOYMENT"
    ROLLBACK_CONFIGURATION = "ROLLBACK_CONFIGURATION"
    #: Bounded capacity change.
    SCALE_SERVICE_WITHIN_LIMIT = "SCALE_SERVICE_WITHIN_LIMIT"
    #: Blast-radius reduction while a dependency is unhealthy.
    DISABLE_DEGRADED_DEPENDENCY = "DISABLE_DEGRADED_DEPENDENCY"
    ROUTE_TRAFFIC_TO_HEALTHY_INSTANCE = "ROUTE_TRAFFIC_TO_HEALTHY_INSTANCE"
    #: Apply a Phase 7-verified patch to an isolated workspace copy only.
    APPLY_VERIFIED_PATCH = "APPLY_VERIFIED_PATCH"
    #: Pause/resume ARGUS's own background work.
    PAUSE_BACKGROUND_JOB = "PAUSE_BACKGROUND_JOB"
    RESUME_BACKGROUND_JOB = "RESUME_BACKGROUND_JOB"


class RemediationSourceType(str, enum.Enum):
    """Where a remediation originated (§7).

    Provenance is stored, not inferred: an action proposed from a *forecast*
    carries different evidence than one proposed from a *reproduced failure*,
    and the reviewer must be told which.
    """

    INCIDENT = "INCIDENT"
    ANOMALY = "ANOMALY"
    ROOT_CAUSE_ANALYSIS = "ROOT_CAUSE_ANALYSIS"
    FAILURE_REPRODUCTION = "FAILURE_REPRODUCTION"
    DEBUG_ANALYSIS = "DEBUG_ANALYSIS"
    VERIFIED_PATCH = "VERIFIED_PATCH"
    RELIABILITY_FORECAST = "RELIABILITY_FORECAST"
    KNOWN_RECOVERY_PATTERN = "KNOWN_RECOVERY_PATTERN"
    HUMAN_OPERATOR = "HUMAN_OPERATOR"


class RemediationStatus(str, enum.Enum):
    """The §5 lifecycle. Terminal states are terminal.

    Note that ``VERIFIED`` means *the action ran and the system behaved as
    expected* — never merely *the command exited zero* (§5 of the safety
    principles, "verification is mandatory").
    """

    PROPOSED = "PROPOSED"
    VALIDATING = "VALIDATING"
    POLICY_REVIEW = "POLICY_REVIEW"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AUTHORIZED = "AUTHORIZED"
    SCHEDULED = "SCHEDULED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"


class RemediationRiskLevel(str, enum.Enum):
    """Action risk (§4, §15). Drives the authorization ceiling.

    Separate from the Phase 8 forecasting risk enum on purpose: this one is a
    *property of an action*, agreed in advance by whoever wrote the registry
    entry, and must not be movable by a prediction.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RemediationExecutionMode(str, enum.Enum):
    """How far an action is allowed to go (§40, §107).

    ``OBSERVE_ONLY`` never authorizes anything; ``DRY_RUN`` validates and
    simulates without side effects; ``SHADOW`` performs the read-only half and
    observes; ``HUMAN_APPROVAL`` and ``AUTONOMOUS`` are the two live regimes;
    ``EMERGENCY_STOP`` denies everything and is checked before every other
    rule.
    """

    OBSERVE_ONLY = "OBSERVE_ONLY"
    DRY_RUN = "DRY_RUN"
    SHADOW = "SHADOW"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"
    AUTONOMOUS = "AUTONOMOUS"
    EMERGENCY_STOP = "EMERGENCY_STOP"

    @property
    def allows_live_effect(self) -> bool:
        """Whether a regime permits a real side effect to be applied."""
        return self in (
            RemediationExecutionMode.HUMAN_APPROVAL,
            RemediationExecutionMode.AUTONOMOUS,
        )


class PolicyDecision(str, enum.Enum):
    """The policy engine's verdict (§4 of the principles, §21)."""

    ALLOW = "ALLOW"
    #: Allowed, but only with a canary step first (5, 8).
    ALLOW_WITH_CANARY = "ALLOW_WITH_CANARY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class RemediationApprovalStatus(str, enum.Enum):
    """Approval records are append-only evidence (§22)."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    WITHDRAWN = "WITHDRAWN"


class RemediationActorType(str, enum.Enum):
    """Who authorized. The distinction is the whole point of §1.

    ``AUTONOMOUS_POLICY`` is *not* an AI: it means a written policy authorized
    a bounded, registered, low-risk action. An AI proposal always arrives as
    ``AI_PROPOSAL`` and can never approve anything.
    """

    HUMAN = "HUMAN"
    AUTONOMOUS_POLICY = "AUTONOMOUS_POLICY"
    AI_PROPOSAL = "AI_PROPOSAL"
    SYSTEM = "SYSTEM"


class SafetyStatus(str, enum.Enum):
    """Safety gate verdict (§6 of the principles, §19, §20)."""

    PASSED = "PASSED"
    PASSED_WITH_WARNINGS = "PASSED_WITH_WARNINGS"
    FAILED = "FAILED"


class ExecutionStatus(str, enum.Enum):
    """One execution attempt (§26)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    #: The attempt ran and the effect could not be applied.
    FAILED = "FAILED"
    #: The engine refused before any effect was attempted.
    REFUSED = "REFUSED"
    #: A dry run or shadow attempt: validated, deliberately no effect.
    NOT_PERFORMED = "NOT_PERFORMED"


class AdapterKind(str, enum.Enum):
    """Where an action's effect would land (§4, §26).

    ``CONTROL_PLANE`` actions change ARGUS's own runtime and are therefore
    genuinely executable today. ``WORKSPACE`` actions operate on an isolated
    copy. ``EXTERNAL`` actions reach a system ARGUS does not own, and are
    refused with ``ADAPTER_UNAVAILABLE`` until an operator configures an
    adapter for the environment — the honest default (§2, §3).
    """

    CONTROL_PLANE = "CONTROL_PLANE"
    WORKSPACE = "WORKSPACE"
    EXTERNAL = "EXTERNAL"


class VerificationVerdict(str, enum.Enum):
    """§28–§31. ``INCONCLUSIVE`` is a real outcome and is never rounded to
    success: an action whose effect cannot be observed has not been verified.
    """

    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_EXECUTED = "NOT_EXECUTED"


class VerificationCheckKind(str, enum.Enum):
    """The observable checks a verification plan can name (§29, §30)."""

    CONTROL_STATE = "CONTROL_STATE"
    #: Did the gated thing actually stop producing output? The difference
    #: between "the switch is off" and "the machine is quiet".
    CONTROL_EFFECT = "CONTROL_EFFECT"
    HEALTH_STATUS = "HEALTH_STATUS"
    ERROR_RATE = "ERROR_RATE"
    LATENCY = "LATENCY"
    NEW_ANOMALIES = "NEW_ANOMALIES"
    NEW_INCIDENTS = "NEW_INCIDENTS"
    INSTANCE_COUNT = "INSTANCE_COUNT"
    TEST_RESULT = "TEST_RESULT"
    WORKSPACE_DIFF = "WORKSPACE_DIFF"


class CheckResult(str, enum.Enum):
    """One check's outcome.

    ``NOT_OBSERVABLE`` exists so that "we have no data" cannot be silently
    treated as a pass — the most dangerous possible bug in a verification
    engine (§30).
    """

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_OBSERVABLE = "NOT_OBSERVABLE"
    SKIPPED = "SKIPPED"


class RollbackStrategy(str, enum.Enum):
    """Declared *before* execution, never discovered after (§4 of principles)."""

    #: Irreversible: requires human approval unconditionally.
    NONE = "NONE"
    #: The inverse registered action (pause↔resume, disable↔enable).
    INVERSE_ACTION = "INVERSE_ACTION"
    #: Restore the captured previous value of a control or setting.
    RESTORE_PREVIOUS_STATE = "RESTORE_PREVIOUS_STATE"
    #: Reset the isolated workspace to the base revision.
    REVERT_WORKSPACE = "REVERT_WORKSPACE"
    #: Only a human can undo it, outside ARGUS; recorded, not attempted.
    MANUAL = "MANUAL"


class RollbackTrigger(str, enum.Enum):
    """Why a rollback started (§32)."""

    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    VERIFICATION_INCONCLUSIVE = "VERIFICATION_INCONCLUSIVE"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    HUMAN_REQUEST = "HUMAN_REQUEST"
    POLICY_REQUIRED = "POLICY_REQUIRED"
    EMERGENCY_STOP = "EMERGENCY_STOP"


class RollbackStatus(str, enum.Enum):
    """Rollback outcome (§33)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class BlastRadiusScope(str, enum.Enum):
    """How much of the system an action may touch (§7 of principles, §24)."""

    SINGLE_INSTANCE = "SINGLE_INSTANCE"
    SINGLE_COMPONENT = "SINGLE_COMPONENT"
    LIMITED_PERCENTAGE = "LIMITED_PERCENTAGE"
    ENVIRONMENT = "ENVIRONMENT"


class RemediationOutcome(str, enum.Enum):
    """Post-remediation analysis verdict (§36, §37).

    ``EFFECTIVE`` requires *verified* recovery, not a green execution.
    """

    EFFECTIVE = "EFFECTIVE"
    PARTIALLY_EFFECTIVE = "PARTIALLY_EFFECTIVE"
    INEFFECTIVE = "INEFFECTIVE"
    HARMFUL = "HARMFUL"
    INCONCLUSIVE = "INCONCLUSIVE"


class RemediationFailureReason(str, enum.Enum):
    """Why an action did not proceed (§17, §26, §27).

    Recorded explicitly so a refusal can be explained, counted and alerted on,
    rather than looking like an action that simply never ran.
    """

    EXECUTION_DISABLED = "EXECUTION_DISABLED"
    ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
    ENVIRONMENT_NOT_ALLOWED = "ENVIRONMENT_NOT_ALLOWED"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    IRREVERSIBLE_RESTRICTED = "IRREVERSIBLE_RESTRICTED"
    POLICY_DENIED = "POLICY_DENIED"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    CONCURRENCY_LIMIT = "CONCURRENCY_LIMIT"
    STALE_ACTION = "STALE_ACTION"
    NOT_ACTIONABLE = "NOT_ACTIONABLE"
    HANDLER_ERROR = "HANDLER_ERROR"
    TIMEOUT = "TIMEOUT"
    PARAMETER_INVALID = "PARAMETER_INVALID"


class CircuitState(str, enum.Enum):
    """Per-scope breaker state (§27 of principles, §25)."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CanaryStage(str, enum.Enum):
    """Staged rollout (§8, §23)."""

    NONE = "NONE"
    CANARY = "CANARY"
    EXPANDING = "EXPANDING"
    COMPLETE = "COMPLETE"


class RemediationControlKind(str, enum.Enum):
    """The ARGUS-native control plane (§10, §26).

    These are the only effects Phase 9 can apply without an external adapter,
    and that is a deliberate scope decision rather than a missing feature: the
    control plane is ARGUS's own runtime, so acting on it is genuinely safe,
    genuinely reversible and genuinely verifiable.
    """

    #: An ARGUS-owned behaviour flag (detection, forecasting, indexing, …).
    FEATURE_FLAG = "FEATURE_FLAG"
    #: A named background sweep or the ingestion worker.
    BACKGROUND_JOB = "BACKGROUND_JOB"
    #: A knowledge-graph dependency temporarily excluded from correlation.
    DEPENDENCY_SUPPRESSION = "DEPENDENCY_SUPPRESSION"


class RemediationControlState(str, enum.Enum):
    """The applied state of a control."""

    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    PAUSED = "PAUSED"
    RESUMED = "RESUMED"
    SUPPRESSED = "SUPPRESSED"


class RemediationAuditEventType(str, enum.Enum):
    """Audit vocabulary (§35). One event per state change or gate decision."""

    ACTION_PROPOSED = "ACTION_PROPOSED"
    ACTION_VALIDATED = "ACTION_VALIDATED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    SAFETY_ASSESSED = "SAFETY_ASSESSED"
    POLICY_EVALUATED = "POLICY_EVALUATED"
    POLICY_DENIED = "POLICY_DENIED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    AUTHORIZED = "AUTHORIZED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_REFUSED = "EXECUTION_REFUSED"
    EXECUTION_SUCCEEDED = "EXECUTION_SUCCEEDED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    EXECUTION_RECORDED_MANUALLY = "EXECUTION_RECORDED_MANUALLY"
    VERIFICATION_STARTED = "VERIFICATION_STARTED"
    VERIFICATION_COMPLETED = "VERIFICATION_COMPLETED"
    ROLLBACK_STARTED = "ROLLBACK_STARTED"
    ROLLBACK_COMPLETED = "ROLLBACK_COMPLETED"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    POST_ANALYSIS_COMPLETED = "POST_ANALYSIS_COMPLETED"
    ACTION_CANCELLED = "ACTION_CANCELLED"
    ACTION_EXPIRED = "ACTION_EXPIRED"
    ACTION_BLOCKED = "ACTION_BLOCKED"
    CIRCUIT_OPENED = "CIRCUIT_OPENED"
    CIRCUIT_CLOSED = "CIRCUIT_CLOSED"
    EMERGENCY_STOP_ENGAGED = "EMERGENCY_STOP_ENGAGED"
    EMERGENCY_STOP_RELEASED = "EMERGENCY_STOP_RELEASED"
    POLICY_UPDATED = "POLICY_UPDATED"
    CONTROL_APPLIED = "CONTROL_APPLIED"
    CONTROL_REVERTED = "CONTROL_REVERTED"


class PostAnalysisStatus(str, enum.Enum):
    """Whether the §37 post-remediation pass ran and what it concluded."""

    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class RemediationPolicy(BaseModel):
    """The per-scope contract that decides what may run (§21).

    One row per project/environment. A missing row is **not** a permissive
    default: the policy engine materialises a restrictive fallback
    (``OBSERVE_ONLY``) and says so in the decision record (§2, "default DENY").
    """

    __tablename__ = "remediation_policies"
    __table_args__ = (
        Index(
            "ix_remediation_policies_scope",
            "project_id",
            "environment_id",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="default")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: The governing regime (§40). ``HUMAN_APPROVAL`` is the default; autonomous
    #: execution has to be turned on explicitly and is itself audited.
    execution_mode: Mapped[RemediationExecutionMode] = mapped_column(
        SAEnum(RemediationExecutionMode, name="remediation_execution_mode"),
        default=RemediationExecutionMode.HUMAN_APPROVAL,
        nullable=False,
        index=True,
    )
    #: Absolute ceiling for autonomous execution: anything at or above this
    #: risk level needs a human, whatever else the policy says (§7).
    autonomous_max_risk: Mapped[RemediationRiskLevel] = mapped_column(
        SAEnum(RemediationRiskLevel, name="remediation_risk_level"),
        default=RemediationRiskLevel.LOW,
        nullable=False,
    )
    #: Explicit action allow-list. ``None`` means "every registered action",
    #: which is still filtered by every other rule here.
    allowed_action_types: Mapped[Optional[list]] = mapped_column(
        JSONType, nullable=True
    )
    #: Environments (by name) where a live effect may ever be applied.
    allowed_environment_names: Mapped[Optional[list]] = mapped_column(
        JSONType, nullable=True
    )
    #: Guard rails (§24, §25, §27).
    max_actions_per_window: Mapped[int] = mapped_column(
        Integer, default=5, nullable=False
    )
    action_window_seconds: Mapped[int] = mapped_column(
        Integer, default=3600, nullable=False
    )
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    max_concurrent_actions: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )
    max_blast_radius_percent: Mapped[float] = mapped_column(
        Float, default=10.0, nullable=False
    )
    circuit_failure_threshold: Mapped[int] = mapped_column(
        Integer, default=3, nullable=False
    )
    circuit_reset_seconds: Mapped[int] = mapped_column(
        Integer, default=900, nullable=False
    )
    #: Canary (§23).
    canary_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    canary_percent: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    #: Approval (§22).
    approval_ttl_seconds: Mapped[int] = mapped_column(
        Integer, default=1800, nullable=False
    )
    #: Verification §28–§30.
    verification_window_seconds: Mapped[int] = mapped_column(
        Integer, default=300, nullable=False
    )
    verification_grace_seconds: Mapped[int] = mapped_column(
        Integer, default=30, nullable=False
    )
    max_verification_attempts: Mapped[int] = mapped_column(
        Integer, default=2, nullable=False
    )
    #: Execution §26, §27.
    execution_timeout_seconds: Mapped[int] = mapped_column(
        Integer, default=120, nullable=False
    )
    max_execution_attempts: Mapped[int] = mapped_column(
        Integer, default=2, nullable=False
    )
    action_expiry_seconds: Mapped[int] = mapped_column(
        Integer, default=86_400, nullable=False
    )
    #: Kill switch (§34). Checked before every other rule.
    emergency_stop_active: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    emergency_stop_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    emergency_stop_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    emergency_stop_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


class RemediationProposal(BaseModel):
    """A candidate remediation, before any gate has run (§6, §8).

    The proposal is where honesty is enforced about *why* something is being
    suggested: ``supporting_evidence`` must name real stored rows (incident,
    candidate, forecast, patch ids), and ``limitations`` must be populated. A
    proposal with no evidence is refused by the planner rather than stored
    (§9), because an unevidenced proposal is indistinguishable from a guess.
    """

    __tablename__ = "remediation_proposals"
    __table_args__ = (
        Index(
            "ix_remediation_proposals_scope_created",
            "project_id",
            "created_at",
        ),
        Index(
            "ix_remediation_proposals_source",
            "source_type",
            "source_id",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action_type: Mapped[RemediationActionType] = mapped_column(
        SAEnum(RemediationActionType, name="remediation_action_type"),
        nullable=False,
        index=True,
    )
    #: Provenance (§7).
    source_type: Mapped[RemediationSourceType] = mapped_column(
        SAEnum(RemediationSourceType, name="remediation_source_type"),
        nullable=False,
        index=True,
    )
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    incident_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("incidents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    forecast_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reliability_forecasts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    causal_analysis_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("causal_analyses.id", ondelete="SET NULL"),
        nullable=True,
    )
    root_cause_candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("root_cause_candidates.id", ondelete="SET NULL"),
        nullable=True,
    )
    reproduction_experiment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reproduction_experiments.id", ondelete="SET NULL"),
        nullable=True,
    )
    fix_hypothesis_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fix_hypotheses.id", ondelete="SET NULL"),
        nullable=True,
    )
    patch_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: §6 content.
    problem: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False)
    expected_effect: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_evidence: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    #: ``{action_type: {...}}`` — validated against the registry before use.
    parameters: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    risk_level: Mapped[RemediationRiskLevel] = mapped_column(
        SAEnum(RemediationRiskLevel, name="remediation_risk_level"),
        nullable=False,
        index=True,
    )
    blast_radius: Mapped[BlastRadiusScope] = mapped_column(
        SAEnum(BlastRadiusScope, name="remediation_blast_radius"),
        nullable=False,
    )
    blast_radius_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    #: Plain-language conditions that must hold before execution (§19).
    preconditions: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    verification_plan: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    rollback_plan: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    #: 0–1 confidence in the *recommendation*, and its basis. Never a
    #: probability of success (§6).
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    limitations: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Strategy label from the planner's catalogue (§9–§18).
    strategy: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    #: Set when the proposal came from an AI provider rather than a rule.
    generated_by: Mapped[RemediationActorType] = mapped_column(
        SAEnum(RemediationActorType, name="remediation_actor_type"),
        default=RemediationActorType.SYSTEM,
        nullable=False,
    )
    model_version: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    # Relationships
    actions: Mapped[List["RemediationAction"]] = relationship(
        "RemediationAction", back_populates="proposal"
    )


class RemediationAction(BaseModel):
    """The core §2 domain object: one bounded, gated, auditable change.

    ``status`` only ever moves through :mod:`app.services.remediation_state`'s
    legal transitions. ``authorization_status`` is intentionally separate from
    ``execution_status``: *authorized to run* and *actually ran* are different
    facts and conflating them is how a system ends up reporting success for
    work it never did.
    """

    __tablename__ = "remediation_actions"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "fingerprint",
            "attempt",
            name="uq_remediation_actions_project_fingerprint_attempt",
        ),
        Index(
            "ix_remediation_actions_scope_status",
            "project_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_remediation_actions_scope_type",
            "project_id",
            "action_type",
            "created_at",
        ),
        Index(
            "ix_remediation_actions_proposal",
            "proposal_id",
        ),
        Index(
            "ix_remediation_actions_expiry",
            "expires_at",
            "status",
        ),
        Index(
            "ix_remediation_actions_active",
            "project_id",
            "environment_id",
            "status",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    proposal_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("remediation_proposals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action_type: Mapped[RemediationActionType] = mapped_column(
        SAEnum(RemediationActionType, name="remediation_action_type"),
        nullable=False,
        index=True,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The registry entry's risk level, copied at creation so a later registry
    #: change cannot retroactively lower the risk of an executed action (§15).
    risk_level: Mapped[RemediationRiskLevel] = mapped_column(
        SAEnum(RemediationRiskLevel, name="remediation_risk_level"),
        nullable=False,
        index=True,
    )
    blast_radius: Mapped[BlastRadiusScope] = mapped_column(
        SAEnum(BlastRadiusScope, name="remediation_blast_radius"),
        nullable=False,
    )
    blast_radius_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    affected_resource_count: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )
    #: Provenance, mirrored onto the action so it is readable without a join.
    source_type: Mapped[RemediationSourceType] = mapped_column(
        SAEnum(RemediationSourceType, name="remediation_source_type"),
        nullable=False,
    )
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    incident_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("incidents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    forecast_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reliability_forecasts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    causal_analysis_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("causal_analyses.id", ondelete="SET NULL"),
        nullable=True,
    )
    root_cause_analysis_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("root_cause_candidates.id", ondelete="SET NULL"),
        nullable=True,
    )
    fix_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fix_hypotheses.id", ondelete="SET NULL"),
        nullable=True,
    )
    patch_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("patches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: Validated parameters (the only input to a handler).
    parameters: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    #: Gate verdicts, kept as three separate columns (§5).
    safety_status: Mapped[Optional[SafetyStatus]] = mapped_column(
        SAEnum(SafetyStatus, name="remediation_safety_status"), nullable=True
    )
    policy_status: Mapped[Optional[PolicyDecision]] = mapped_column(
        SAEnum(PolicyDecision, name="remediation_policy_decision"), nullable=True
    )
    authorization_status: Mapped[Optional[RemediationApprovalStatus]] = mapped_column(
        SAEnum(RemediationApprovalStatus, name="remediation_approval_status"),
        nullable=True,
    )
    execution_status: Mapped[Optional[ExecutionStatus]] = mapped_column(
        SAEnum(ExecutionStatus, name="remediation_execution_status"), nullable=True
    )
    status: Mapped[RemediationStatus] = mapped_column(
        SAEnum(RemediationStatus, name="remediation_status"),
        default=RemediationStatus.PROPOSED,
        nullable=False,
        index=True,
    )
    #: The regime the action was evaluated under (§40).
    execution_mode: Mapped[RemediationExecutionMode] = mapped_column(
        SAEnum(RemediationExecutionMode, name="remediation_execution_mode"),
        default=RemediationExecutionMode.HUMAN_APPROVAL,
        nullable=False,
    )
    adapter_kind: Mapped[AdapterKind] = mapped_column(
        SAEnum(AdapterKind, name="remediation_adapter_kind"),
        nullable=False,
    )
    rollback_strategy: Mapped[RollbackStrategy] = mapped_column(
        SAEnum(RollbackStrategy, name="remediation_rollback_strategy"),
        nullable=False,
    )
    rollback_available: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    #: The action that undoes this one, when one exists (§33).
    rollback_action_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("remediation_actions.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: What the rollback would be, recorded before execution (§4).
    rollback_plan: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    verification_plan: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    preconditions: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    #: Canary (§23).
    canary_required: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    canary_stage: Mapped[CanaryStage] = mapped_column(
        SAEnum(CanaryStage, name="remediation_canary_stage"),
        default=CanaryStage.NONE,
        nullable=False,
    )
    canary_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    #: Bounded retry bookkeeping (§6 of principles, §27).
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: Dedup/monotonic key over scope+action+target, so a sweep cannot propose
    #: the same remediation twice in the same window (§25).
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Explicit refusal record when an action could not proceed.
    failure_reason: Mapped[Optional[RemediationFailureReason]] = mapped_column(
        SAEnum(RemediationFailureReason, name="remediation_failure_reason"),
        nullable=True,
        index=True,
    )
    failure_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Post-remediation analysis (§36, §37).
    outcome: Mapped[Optional[RemediationOutcome]] = mapped_column(
        SAEnum(RemediationOutcome, name="remediation_outcome"), nullable=True
    )
    post_analysis_status: Mapped[PostAnalysisStatus] = mapped_column(
        SAEnum(PostAnalysisStatus, name="remediation_post_analysis_status"),
        default=PostAnalysisStatus.PENDING,
        nullable=False,
    )
    post_analysis: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    headline: Mapped[str] = mapped_column(String(400), nullable=False)
    #: §2 timestamps.
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    authorized_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    rollback_performed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: §2 actors.
    created_by: Mapped[str] = mapped_column(
        String(255), nullable=False, default="system"
    )
    approved_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    authorized_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    executed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    # Relationships
    proposal: Mapped[Optional["RemediationProposal"]] = relationship(
        "RemediationProposal", back_populates="actions"
    )
    assessments: Mapped[List["RemediationAssessment"]] = relationship(
        "RemediationAssessment",
        back_populates="action",
        cascade="all, delete-orphan",
    )
    policy_decisions: Mapped[List["RemediationPolicyDecision"]] = relationship(
        "RemediationPolicyDecision",
        back_populates="action",
        cascade="all, delete-orphan",
    )
    approvals: Mapped[List["RemediationApproval"]] = relationship(
        "RemediationApproval",
        back_populates="action",
        cascade="all, delete-orphan",
    )
    executions: Mapped[List["RemediationExecution"]] = relationship(
        "RemediationExecution",
        back_populates="action",
        cascade="all, delete-orphan",
        order_by="RemediationExecution.attempt",
    )
    verifications: Mapped[List["RemediationVerification"]] = relationship(
        "RemediationVerification",
        back_populates="action",
        cascade="all, delete-orphan",
    )
    #: ``remediation_rollbacks`` has *two* foreign keys into this table
    #: (``action_id`` and ``inverse_action_id``), so the join column has to be
    #: named explicitly; without it SQLAlchemy cannot resolve the relationship
    #: at all, and the mapper fails before any query runs.
    rollbacks: Mapped[List["RemediationRollback"]] = relationship(
        "RemediationRollback",
        back_populates="action",
        cascade="all, delete-orphan",
        foreign_keys="RemediationRollback.action_id",
    )
    audit_events: Mapped[List["RemediationAuditEvent"]] = relationship(
        "RemediationAuditEvent",
        back_populates="action",
        cascade="all, delete-orphan",
    )


class RemediationAssessment(BaseModel):
    """One safety assessment pass over an action (§19, §20).

    Append-only: re-assessing an action writes a new row, so a decision made
    under one set of facts can be compared with the decision made under
    another. ``checks`` is a list of ``{name, result, detail}`` objects and
    ``blocking`` names the checks that refused the action.
    """

    __tablename__ = "remediation_assessments"
    __table_args__ = (
        Index(
            "ix_remediation_assessments_action_created",
            "action_id",
            "created_at",
        ),
    )

    action_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("remediation_actions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[SafetyStatus] = mapped_column(
        SAEnum(SafetyStatus, name="remediation_safety_status"),
        nullable=False,
        index=True,
    )
    checks: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    blocking: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    warnings: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    #: Whether the action can be undone, and how (§4 of the principles).
    reversible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rollback_plan: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    blast_radius: Mapped[BlastRadiusScope] = mapped_column(
        SAEnum(BlastRadiusScope, name="remediation_blast_radius"),
        nullable=False,
    )
    blast_radius_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    affected_resource_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    #: True when the assessment itself demands a human, independent of policy.
    requires_human_approval: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    assessed_by: Mapped[str] = mapped_column(
        String(255), nullable=False, default="engine"
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    action: Mapped["RemediationAction"] = relationship(
        "RemediationAction", back_populates="assessments"
    )


class RemediationPolicyDecision(BaseModel):
    """One policy evaluation (§21).

    Stores the resolved policy revision, the matched rules and the verdict, so
    "why was this allowed?" is answerable after the policy has been edited.
    """

    __tablename__ = "remediation_policy_decisions"
    __table_args__ = (
        Index(
            "ix_remediation_policy_decisions_action_created",
            "action_id",
            "created_at",
        ),
        Index(
            "ix_remediation_policy_decisions_project_decision",
            "project_id",
            "decision",
        ),
    )

    action_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("remediation_actions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    policy_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("remediation_policies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
    )
    decision: Mapped[PolicyDecision] = mapped_column(
        SAEnum(PolicyDecision, name="remediation_policy_decision"),
        nullable=False,
        index=True,
    )
    execution_mode: Mapped[RemediationExecutionMode] = mapped_column(
        SAEnum(RemediationExecutionMode, name="remediation_execution_mode"),
        nullable=False,
    )
    policy_revision: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: Human-readable rules that fired, in evaluation order.
    matched_rules: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    reasons: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    #: Non-empty exactly when the decision is ``DENY`` (§17).
    failure_reason: Mapped[Optional[RemediationFailureReason]] = mapped_column(
        SAEnum(RemediationFailureReason, name="remediation_failure_reason"),
        nullable=True,
    )
    requires_canary: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    budget_state: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    circuit_state: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    evaluated_by: Mapped[str] = mapped_column(
        String(255), nullable=False, default="policy-engine"
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    action: Mapped["RemediationAction"] = relationship(
        "RemediationAction", back_populates="policy_decisions"
    )


class RemediationApproval(BaseModel):
    """An approval decision (§22).

    ``actor_type`` is the field that makes §1 enforceable: an ``AI_PROPOSAL``
    row can never satisfy the authorization gate, and the authorization engine
    checks that rather than trusting ``status`` alone.
    """

    __tablename__ = "remediation_approvals"
    __table_args__ = (
        Index(
            "ix_remediation_approvals_action_status",
            "action_id",
            "status",
        ),
        Index(
            "ix_remediation_approvals_project_created",
            "project_id",
            "created_at",
        ),
    )

    action_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("remediation_actions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[RemediationApprovalStatus] = mapped_column(
        SAEnum(RemediationApprovalStatus, name="remediation_approval_status"),
        default=RemediationApprovalStatus.PENDING,
        nullable=False,
        index=True,
    )
    actor_type: Mapped[RemediationActorType] = mapped_column(
        SAEnum(RemediationActorType, name="remediation_actor_type"),
        nullable=False,
    )
    actor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Frozen summary of what the approver saw (risk, blast radius, evidence).
    scope_snapshot: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    action: Mapped["RemediationAction"] = relationship(
        "RemediationAction", back_populates="approvals"
    )


class RemediationExecution(BaseModel):
    """One execution attempt (§26, §27).

    ``effect_applied`` distinguishes *the handler ran* from *something actually
    changed*. Only ``effect_applied`` may lead to verification, so a dry run can
    never be mistaken for a live remediation.
    """

    __tablename__ = "remediation_executions"
    __table_args__ = (
        UniqueConstraint(
            "action_id", "attempt", name="uq_remediation_executions_action_attempt"
        ),
        Index(
            "ix_remediation_executions_project_status",
            "project_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_remediation_executions_scope_type",
            "project_id",
            "action_type",
            "created_at",
        ),
    )

    action_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("remediation_actions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action_type: Mapped[RemediationActionType] = mapped_column(
        SAEnum(RemediationActionType, name="remediation_action_type"),
        nullable=False,
        index=True,
    )
    mode: Mapped[RemediationExecutionMode] = mapped_column(
        SAEnum(RemediationExecutionMode, name="remediation_execution_mode"),
        nullable=False,
        index=True,
    )
    adapter_kind: Mapped[AdapterKind] = mapped_column(
        SAEnum(AdapterKind, name="remediation_adapter_kind"),
        nullable=False,
    )
    adapter_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[ExecutionStatus] = mapped_column(
        SAEnum(ExecutionStatus, name="remediation_execution_status"),
        default=ExecutionStatus.PENDING,
        nullable=False,
        index=True,
    )
    #: Whether a real side effect was applied. Dry runs are always ``False``.
    effect_applied: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    #: What the handler did, in structured form — never a raw command line.
    steps: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    #: The control plane rows this attempt created (for rollback and audit).
    control_ids: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    #: Redacted handler output summary, bounded in length.
    output_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[Optional[RemediationFailureReason]] = mapped_column(
        SAEnum(RemediationFailureReason, name="remediation_failure_reason"),
        nullable=True,
        index=True,
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Deterministic key so a retried job applies at most one effect.
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    executed_by: Mapped[str] = mapped_column(
        String(255), nullable=False, default="executor"
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    action: Mapped["RemediationAction"] = relationship(
        "RemediationAction", back_populates="executions"
    )


class RemediationVerification(BaseModel):
    """One verification pass (§28–§31).

    The verdict is computed from checks, and ``NOT_OBSERVABLE`` checks are
    counted separately so ``INCONCLUSIVE`` can be reported honestly instead of
    being averaged into a pass.
    """

    __tablename__ = "remediation_verifications"
    __table_args__ = (
        Index(
            "ix_remediation_verifications_action_created",
            "action_id",
            "created_at",
        ),
        Index(
            "ix_remediation_verifications_project_verdict",
            "project_id",
            "verdict",
        ),
    )

    action_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("remediation_actions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("remediation_executions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    verdict: Mapped[VerificationVerdict] = mapped_column(
        SAEnum(VerificationVerdict, name="remediation_verification_verdict"),
        nullable=False,
        index=True,
    )
    #: ``{check_kind: {result, observed, baseline, detail}}``.
    checks: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    passed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    not_observable_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    observation_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Explicit statement of what could not be verified (§31, §46).
    limitations: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    verified_by: Mapped[str] = mapped_column(
        String(255), nullable=False, default="verifier"
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    action: Mapped["RemediationAction"] = relationship(
        "RemediationAction", back_populates="verifications"
    )


class RemediationRollback(BaseModel):
    """A rollback attempt and its own verification (§32, §33).

    A rollback is not assumed to have worked: it is verified with the same
    machinery as the action it reverses, and ``verification_verdict`` records
    that result.
    """

    __tablename__ = "remediation_rollbacks"
    __table_args__ = (
        Index(
            "ix_remediation_rollbacks_action_created",
            "action_id",
            "created_at",
        ),
    )

    action_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("remediation_actions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trigger: Mapped[RollbackTrigger] = mapped_column(
        SAEnum(RollbackTrigger, name="remediation_rollback_trigger"),
        nullable=False,
        index=True,
    )
    strategy: Mapped[RollbackStrategy] = mapped_column(
        SAEnum(RollbackStrategy, name="remediation_rollback_strategy"),
        nullable=False,
    )
    status: Mapped[RollbackStatus] = mapped_column(
        SAEnum(RollbackStatus, name="remediation_rollback_status"),
        default=RollbackStatus.PENDING,
        nullable=False,
        index=True,
    )
    plan: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    steps: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    #: Set when the rollback was executed as an inverse registered action.
    inverse_action_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("remediation_actions.id", ondelete="SET NULL"),
        nullable=True,
    )
    controls_reverted: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    verification_verdict: Mapped[Optional[VerificationVerdict]] = mapped_column(
        SAEnum(VerificationVerdict, name="remediation_verification_verdict"),
        nullable=True,
    )
    failure_reason: Mapped[Optional[RemediationFailureReason]] = mapped_column(
        SAEnum(RemediationFailureReason, name="remediation_failure_reason"),
        nullable=True,
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requested_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    #: Named because this table also references ``remediation_actions`` through
    #: ``inverse_action_id``; an unqualified join is ambiguous.
    action: Mapped["RemediationAction"] = relationship(
        "RemediationAction",
        back_populates="rollbacks",
        foreign_keys=[action_id],
    )


class RemediationAuditEvent(BaseModel):
    """Append-only audit trail (§12).

    ``prev_hash``/``entry_hash`` chain the events for one action. The chain is
    what makes the log *evidence*: removing a row breaks the chain at a
    detectable point instead of leaving a tidy-looking history.
    """

    __tablename__ = "remediation_audit_events"
    __table_args__ = (
        Index(
            "ix_remediation_audit_events_action_created",
            "action_id",
            "created_at",
        ),
        Index(
            "ix_remediation_audit_events_project_type",
            "project_id",
            "event_type",
            "created_at",
        ),
        UniqueConstraint(
            "action_id", "sequence", name="uq_remediation_audit_events_action_sequence"
        ),
    )

    action_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("remediation_actions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    event_type: Mapped[RemediationAuditEventType] = mapped_column(
        SAEnum(RemediationAuditEventType, name="remediation_audit_event_type"),
        nullable=False,
        index=True,
    )
    #: Who or what caused this event.
    actor_type: Mapped[RemediationActorType] = mapped_column(
        SAEnum(RemediationActorType, name="remediation_actor_type"),
        nullable=False,
    )
    actor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    from_status: Mapped[Optional[RemediationStatus]] = mapped_column(
        SAEnum(RemediationStatus, name="remediation_status"), nullable=True
    )
    to_status: Mapped[Optional[RemediationStatus]] = mapped_column(
        SAEnum(RemediationStatus, name="remediation_status"), nullable=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    prev_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    action: Mapped[Optional["RemediationAction"]] = relationship(
        "RemediationAction", back_populates="audit_events"
    )


class RemediationCircuitBreaker(BaseModel):
    """Per-scope failure breaker (§27 of the principles, §25).

    Deliberately keyed on ``(project, environment, action_type)``: a single
    action type that keeps failing must stop being attempted without freezing
    every other remediation the project might legitimately need.
    """

    __tablename__ = "remediation_circuit_breakers"
    __table_args__ = (
        Index(
            "ix_remediation_circuit_breakers_scope_type",
            "project_id",
            "environment_id",
            "action_type",
        ),
        #: One breaker per scope, enforced by the database rather than by
        #: convention. ``get_breaker`` reads a single row, so a duplicate
        #: ``CLOSED`` row sitting beside an ``OPEN`` one would silently mask the
        #: open breaker and let a failing action type keep being attempted.
        #: ``NULLS NOT DISTINCT`` matters because a project-wide breaker has a
        #: NULL ``environment_id``: under the default SQL semantics two such
        #: rows are "different", which is exactly the duplicate this prevents.
        Index(
            "uq_remediation_circuit_breakers_scope",
            "project_id",
            "environment_id",
            "action_type",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    action_type: Mapped[RemediationActionType] = mapped_column(
        SAEnum(RemediationActionType, name="remediation_action_type"),
        nullable=False,
        index=True,
    )
    state: Mapped[CircuitState] = mapped_column(
        SAEnum(CircuitState, name="remediation_circuit_state"),
        default=CircuitState.CLOSED,
        nullable=False,
        index=True,
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    total_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_successes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    threshold: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    opened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    opened_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_failure_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_trip_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


class RemediationControl(BaseModel):
    """A live ARGUS control-plane setting (§10, §26).

    This is the table that makes Phase 9's native actions *real*: the worker and
    every sweep read it before doing work, so a ``PAUSE_BACKGROUND_JOB`` action
    genuinely stops that job and ``RESUME_BACKGROUND_JOB`` genuinely restarts it.

    A control is never deleted. Reverting writes a new revision with the
    previous state, so the history of what was disabled, by which action, and
    for how long, survives — which is exactly what an audit needs.
    """

    __tablename__ = "remediation_controls"
    __table_args__ = (
        Index(
            "ix_remediation_controls_scope_key_kind",
            "project_id",
            "kind",
            "scope_key",
        ),
        Index(
            "ix_remediation_controls_lookup",
            "kind",
            "scope_key",
            "is_current",
        ),
    )

    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    kind: Mapped[RemediationControlKind] = mapped_column(
        SAEnum(RemediationControlKind, name="remediation_control_kind"),
        nullable=False,
        index=True,
    )
    #: The control's identity, e.g. ``reliability_sweep`` or ``anomaly_detection``.
    scope_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    state: Mapped[RemediationControlState] = mapped_column(
        SAEnum(RemediationControlState, name="remediation_control_state"),
        nullable=False,
        index=True,
    )
    #: The state this control had before the change, for exact reversal.
    previous_state: Mapped[Optional[RemediationControlState]] = mapped_column(
        SAEnum(RemediationControlState, name="remediation_control_state"),
        nullable=True,
    )
    #: Only one current row per (kind, scope_key, scope) is authoritative.
    is_current: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: The action that applied this control; SET NULL keeps the row if a
    #: project is torn down in read-only mode.
    applied_by_action_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("remediation_actions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    reverted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    applied_by: Mapped[str] = mapped_column(
        String(255), nullable=False, default="system"
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


__all__ = [
    "RemediationActionType",
    "RemediationSourceType",
    "RemediationStatus",
    "RemediationRiskLevel",
    "RemediationExecutionMode",
    "PolicyDecision",
    "RemediationApprovalStatus",
    "RemediationActorType",
    "SafetyStatus",
    "ExecutionStatus",
    "AdapterKind",
    "VerificationVerdict",
    "VerificationCheckKind",
    "CheckResult",
    "RollbackStrategy",
    "RollbackTrigger",
    "RollbackStatus",
    "BlastRadiusScope",
    "RemediationOutcome",
    "RemediationFailureReason",
    "CircuitState",
    "CanaryStage",
    "RemediationControlKind",
    "RemediationControlState",
    "RemediationAuditEventType",
    "PostAnalysisStatus",
    "RemediationPolicy",
    "RemediationProposal",
    "RemediationAction",
    "RemediationAssessment",
    "RemediationPolicyDecision",
    "RemediationApproval",
    "RemediationExecution",
    "RemediationVerification",
    "RemediationRollback",
    "RemediationAuditEvent",
    "RemediationCircuitBreaker",
    "RemediationControl",
]
