"""ARGUS Remediation Schemas (Phase 9 §42–§44).

Three shape decisions, each of which encodes a rule from the phase rather than a
preference about JSON:

* **The three gate verdicts are separate fields.** ``safety_status``,
  ``policy_status``, ``authorization_status`` and ``execution_status`` travel
  independently, because "permitted", "approved" and "actually ran" are different
  facts and a client that conflates them will render a refusal as a success.
* **A refusal is a first-class payload.** ``failure_reason`` and
  ``failure_detail`` are present on every action response, so a blocked action
  explains itself instead of looking like one that simply has not run yet.
* **The registry is exposed as data.** ``ActionTypeResponse`` mirrors the code
  definition — risk, blast radius, rollback strategy, whether it is executable in
  this build — so an operator can see what the platform is willing to do without
  reading the source.

Requests are deliberately narrow: there is no field anywhere that accepts a
command, a script, a URL or an arbitrary parameter bag. Parameters for a human
proposal are validated against the registry exactly like a planner's.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import Field

from app.models.remediation import (
    AdapterKind,
    BlastRadiusScope,
    CanaryStage,
    CircuitState,
    ExecutionStatus,
    PolicyDecision,
    PostAnalysisStatus,
    RemediationActionType,
    RemediationActorType,
    RemediationApprovalStatus,
    RemediationExecutionMode,
    RemediationFailureReason,
    RemediationOutcome,
    RemediationRiskLevel,
    RemediationSourceType,
    RemediationStatus,
    RollbackStatus,
    RollbackStrategy,
    SafetyStatus,
    VerificationVerdict,
)
from app.schemas.base import BaseSchema


# ---------------------------------------------------------------------------
# Registry (§3, §4)
# ---------------------------------------------------------------------------


class ActionParameterResponse(BaseSchema):
    """One accepted parameter of a registered action."""

    name: str
    kind: str
    required: bool = False
    choices: Optional[list[str]] = None
    choices_from: Optional[str] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    default: Optional[Any] = None
    description: str = ""


class ActionTypeResponse(BaseSchema):
    """A registry entry, as data (§4)."""

    action_type: RemediationActionType
    description: str
    risk_level: RemediationRiskLevel
    adapter_kind: AdapterKind
    parameters: list[ActionParameterResponse] = Field(default_factory=list)
    verification_plan: list[str] = Field(default_factory=list)
    rollback_strategy: RollbackStrategy
    inverse_action: Optional[RemediationActionType] = None
    maximum_blast_radius: BlastRadiusScope
    production_effect: bool
    supports_canary: bool
    supports_autonomous_execution: bool
    requires_human_approval: bool
    reversible: bool
    executable_in_build: bool
    unavailable_reason: Optional[str] = None
    notes: list[str] = Field(default_factory=list)


class ActionTypeListResponse(BaseSchema):
    """Every registered action."""

    actions: list[ActionTypeResponse] = Field(default_factory=list)
    count: int = 0
    execution_enabled: bool = True


# ---------------------------------------------------------------------------
# Policy (§21, §40)
# ---------------------------------------------------------------------------


class PolicyResponse(BaseSchema):
    """The effective policy for a scope, including the operator's ceilings."""

    id: Optional[uuid.UUID] = None
    project_id: Optional[uuid.UUID] = None
    environment_id: Optional[uuid.UUID] = None
    source: str = "fallback"
    revision: Optional[int] = None
    enabled: bool = True
    execution_mode: RemediationExecutionMode
    autonomous_max_risk: RemediationRiskLevel
    allowed_action_types: Optional[list[str]] = None
    allowed_environment_names: Optional[list[str]] = None
    max_actions_per_window: int = 0
    action_window_seconds: int = 0
    cooldown_seconds: int = 0
    max_concurrent_actions: int = 0
    max_blast_radius_percent: float = 0.0
    max_blast_radius_scope: BlastRadiusScope
    canary_enabled: bool = True
    canary_percent: float = 0.0
    approval_ttl_seconds: int = 0
    verification_window_seconds: int = 0
    execution_timeout_seconds: int = 0
    action_expiry_seconds: int = 0
    emergency_stop_active: bool = False
    emergency_stop_reason: Optional[str] = None
    emergency_stop_at: Optional[datetime] = None
    emergency_stop_by: Optional[str] = None
    updated_by: Optional[str] = None
    clamped: list[str] = Field(default_factory=list)
    notes: Optional[str] = None


class PolicyUpdateRequest(BaseSchema):
    """Change a scope's policy.

    Every field is optional: a partial update keeps the previous values. Hard
    ceilings configured on the process are applied on top and cannot be raised
    through this endpoint (§21, §45).
    """

    enabled: Optional[bool] = None
    execution_mode: Optional[RemediationExecutionMode] = None
    autonomous_max_risk: Optional[RemediationRiskLevel] = None
    allowed_action_types: Optional[list[str]] = None
    allowed_environment_names: Optional[list[str]] = None
    max_actions_per_window: Optional[int] = Field(default=None, ge=1, le=100)
    action_window_seconds: Optional[int] = Field(default=None, ge=60, le=604_800)
    cooldown_seconds: Optional[int] = Field(default=None, ge=0, le=86_400)
    max_concurrent_actions: Optional[int] = Field(default=None, ge=1, le=10)
    max_blast_radius_percent: Optional[float] = Field(default=None, ge=0, le=100)
    canary_enabled: Optional[bool] = None
    canary_percent: Optional[float] = Field(default=None, ge=1, le=100)
    approval_ttl_seconds: Optional[int] = Field(default=None, ge=60, le=86_400)
    verification_window_seconds: Optional[int] = Field(default=None, ge=30, le=86_400)
    execution_timeout_seconds: Optional[int] = Field(default=None, ge=5, le=3600)
    action_expiry_seconds: Optional[int] = Field(default=None, ge=300, le=2_592_000)
    environment_id: Optional[uuid.UUID] = None
    notes: Optional[str] = Field(default=None, max_length=2000)
    updated_by: Optional[str] = Field(default=None, max_length=255)


class EmergencyStopRequest(BaseSchema):
    """Engage or release a project's kill switch (§34)."""

    engage: bool
    actor: str = Field(min_length=1, max_length=255)
    reason: Optional[str] = Field(default=None, max_length=2000)


# ---------------------------------------------------------------------------
# Control plane (§10)
# ---------------------------------------------------------------------------


class ControlResponse(BaseSchema):
    """One ARGUS-owned control and its current state."""

    id: uuid.UUID
    kind: str
    scope_key: str
    state: str
    previous_state: Optional[str] = None
    is_current: bool = True
    revision: int = 1
    applied_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    reverted_at: Optional[datetime] = None
    applied_by: Optional[str] = None
    reason: Optional[str] = None
    applied_by_action_id: Optional[uuid.UUID] = None
    effective: bool = True


class ControlListResponse(BaseSchema):
    """Every control currently in force for a scope."""

    controls: list[ControlResponse] = Field(default_factory=list)
    count: int = 0


# ---------------------------------------------------------------------------
# Proposals and actions (§2, §6)
# ---------------------------------------------------------------------------


class ProposalResponse(BaseSchema):
    """A candidate remediation with its provenance and its own uncertainty."""

    id: uuid.UUID
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    action_type: RemediationActionType
    source_type: RemediationSourceType
    source_id: Optional[uuid.UUID] = None
    strategy: Optional[str] = None
    problem: str
    recommended_action: str
    expected_effect: str
    supporting_evidence: Optional[list] = None
    parameters: Optional[dict] = None
    risk_level: RemediationRiskLevel
    blast_radius: BlastRadiusScope
    blast_radius_percent: Optional[float] = None
    preconditions: Optional[list] = None
    verification_plan: Optional[dict] = None
    rollback_plan: Optional[dict] = None
    confidence: Optional[float] = None
    confidence_reason: Optional[str] = None
    limitations: Optional[list] = None
    rationale: Optional[str] = None
    generated_by: RemediationActorType
    model_version: Optional[str] = None
    incident_id: Optional[uuid.UUID] = None
    forecast_id: Optional[uuid.UUID] = None
    causal_analysis_id: Optional[uuid.UUID] = None
    root_cause_candidate_id: Optional[uuid.UUID] = None
    patch_id: Optional[uuid.UUID] = None
    fingerprint: str
    created_at: Optional[datetime] = None


class ActionResponse(BaseSchema):
    """One remediation action — the §2 domain object, with all four verdicts."""

    id: uuid.UUID
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    proposal_id: Optional[uuid.UUID] = None
    action_type: RemediationActionType
    status: RemediationStatus
    description: str
    reason: Optional[str] = None
    headline: str
    risk_level: RemediationRiskLevel
    blast_radius: BlastRadiusScope
    blast_radius_percent: Optional[float] = None
    affected_resource_count: int = 1
    source_type: RemediationSourceType
    source_id: Optional[uuid.UUID] = None
    parameters: Optional[dict] = None
    safety_status: Optional[SafetyStatus] = None
    policy_status: Optional[PolicyDecision] = None
    authorization_status: Optional[RemediationApprovalStatus] = None
    execution_status: Optional[ExecutionStatus] = None
    execution_mode: RemediationExecutionMode
    adapter_kind: AdapterKind
    rollback_strategy: RollbackStrategy
    rollback_available: bool = False
    rollback_plan: Optional[dict] = None
    verification_plan: Optional[dict] = None
    preconditions: Optional[list] = None
    canary_required: bool = False
    canary_stage: CanaryStage = CanaryStage.NONE
    canary_percent: Optional[float] = None
    attempt: int = 1
    retry_count: int = 0
    max_retries: int = 0
    failure_reason: Optional[RemediationFailureReason] = None
    failure_detail: Optional[str] = None
    outcome: Optional[RemediationOutcome] = None
    post_analysis_status: PostAnalysisStatus
    fingerprint: str
    incident_id: Optional[uuid.UUID] = None
    forecast_id: Optional[uuid.UUID] = None
    patch_id: Optional[uuid.UUID] = None
    created_by: str = "system"
    approved_by: Optional[str] = None
    authorized_by: Optional[str] = None
    executed_by: Optional[str] = None
    #: §2 timestamps.
    created_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    authorized_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    rollback_performed_at: Optional[datetime] = None


class ActionListResponse(BaseSchema):
    """A page of actions, newest first."""

    actions: list[ActionResponse] = Field(default_factory=list)
    count: int = 0
    total: int = 0


class AssessmentResponse(BaseSchema):
    """One safety assessment, with every check it ran."""

    id: uuid.UUID
    status: SafetyStatus
    checks: Optional[list] = None
    blocking: Optional[list] = None
    warnings: Optional[list] = None
    reversible: bool = False
    rollback_plan: Optional[dict] = None
    blast_radius: BlastRadiusScope
    blast_radius_percent: Optional[float] = None
    affected_resource_count: int = 0
    requires_human_approval: bool = False
    reason: Optional[str] = None
    assessed_by: str = "engine"
    created_at: Optional[datetime] = None


class PolicyDecisionResponse(BaseSchema):
    """One policy evaluation, with the rules that fired."""

    id: uuid.UUID
    policy_id: Optional[uuid.UUID] = None
    decision: PolicyDecision
    execution_mode: RemediationExecutionMode
    policy_revision: Optional[int] = None
    matched_rules: Optional[list] = None
    reasons: Optional[list] = None
    failure_reason: Optional[RemediationFailureReason] = None
    requires_canary: bool = False
    budget_state: Optional[dict] = None
    circuit_state: Optional[dict] = None
    evaluated_by: str = "policy-engine"
    created_at: Optional[datetime] = None


class ApprovalResponse(BaseSchema):
    """One authorization record."""

    id: uuid.UUID
    status: RemediationApprovalStatus
    actor_type: RemediationActorType
    actor: Optional[str] = None
    decided_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    reason: Optional[str] = None
    scope_snapshot: Optional[dict] = None
    created_at: Optional[datetime] = None


class ExecutionResponse(BaseSchema):
    """One execution attempt, including whether an effect was applied."""

    id: uuid.UUID
    attempt: int
    mode: RemediationExecutionMode
    adapter_kind: AdapterKind
    adapter_name: Optional[str] = None
    status: ExecutionStatus
    effect_applied: bool = False
    steps: Optional[list] = None
    control_ids: Optional[list] = None
    output_summary: Optional[str] = None
    failure_reason: Optional[RemediationFailureReason] = None
    error: Optional[str] = None
    idempotency_key: str
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    executed_by: str = "executor"


class VerificationResponse(BaseSchema):
    """One verification pass and its verdict."""

    id: uuid.UUID
    execution_id: Optional[uuid.UUID] = None
    verdict: VerificationVerdict
    checks: Optional[list] = None
    passed_count: int = 0
    failed_count: int = 0
    not_observable_count: int = 0
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    observation_seconds: int = 0
    summary: Optional[str] = None
    limitations: Optional[list] = None
    verified_by: str = "verifier"
    created_at: Optional[datetime] = None


class RollbackResponse(BaseSchema):
    """One rollback attempt, itself verified."""

    id: uuid.UUID
    trigger: str
    strategy: RollbackStrategy
    status: RollbackStatus
    plan: Optional[dict] = None
    steps: Optional[list] = None
    controls_reverted: Optional[list] = None
    verification_verdict: Optional[VerificationVerdict] = None
    failure_reason: Optional[RemediationFailureReason] = None
    error: Optional[str] = None
    requested_by: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class AuditEventResponse(BaseSchema):
    """One hash-chained audit event."""

    id: uuid.UUID
    sequence: int
    event_type: str
    actor_type: RemediationActorType
    actor: Optional[str] = None
    from_status: Optional[RemediationStatus] = None
    to_status: Optional[RemediationStatus] = None
    summary: str
    detail: Optional[dict] = None
    occurred_at: Optional[datetime] = None
    entry_hash: str
    prev_hash: Optional[str] = None


class ActionDetailResponse(BaseSchema):
    """An action with its whole evidence chain attached (§12, §44)."""

    action: ActionResponse
    proposal: Optional[ProposalResponse] = None
    assessments: list[AssessmentResponse] = Field(default_factory=list)
    policy_decisions: list[PolicyDecisionResponse] = Field(default_factory=list)
    approvals: list[ApprovalResponse] = Field(default_factory=list)
    executions: list[ExecutionResponse] = Field(default_factory=list)
    verifications: list[VerificationResponse] = Field(default_factory=list)
    rollbacks: list[RollbackResponse] = Field(default_factory=list)
    audit: list[AuditEventResponse] = Field(default_factory=list)
    audit_chain: Optional[dict] = None
    post_analysis: Optional[dict] = None
    #: Which transitions the state machine will accept next (§5).
    allowed_transitions: list[RemediationStatus] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class PlanRequest(BaseSchema):
    """Ask the planner to propose remediations for a scope."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    incident_id: Optional[uuid.UUID] = None
    forecast_id: Optional[uuid.UUID] = None
    auto_assess: bool = Field(
        default=True,
        description=(
            "Run the safety and policy gates on each new proposal. The proposal "
            "is always recorded; this only decides how far the pipeline advances."
        ),
    )


class PlanResponse(BaseSchema):
    """What the planner produced."""

    project_id: uuid.UUID
    proposals_created: int = 0
    actions_created: int = 0
    actions: list[ActionResponse] = Field(default_factory=list)
    skipped_duplicates: int = 0
    detail: Optional[str] = None


class ManualProposalRequest(BaseSchema):
    """A human operator proposing a remediation (§7).

    Still gated: the parameters are validated against the registry, the safety
    engine evaluates them and the policy engine decides whether it may run. A
    human proposing an action does not authorize it.
    """

    project_id: uuid.UUID
    action_type: RemediationActionType
    description: str = Field(min_length=1, max_length=2000)
    reason: Optional[str] = Field(default=None, max_length=2000)
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    incident_id: Optional[uuid.UUID] = None
    forecast_id: Optional[uuid.UUID] = None
    patch_id: Optional[uuid.UUID] = None
    parameters: dict = Field(default_factory=dict)
    blast_radius: Optional[BlastRadiusScope] = None
    blast_radius_percent: Optional[float] = Field(default=None, ge=0, le=100)
    #: A proposal with no named proposer is an unattributable action, and an
    #: unattributable action is one nobody can be asked about later (§6, §12).
    created_by: str = Field(default="operator", min_length=1, max_length=255)
    auto_assess: bool = True


class ApprovalDecisionRequest(BaseSchema):
    """Approve or reject a pending action (§22)."""

    actor: str = Field(min_length=1, max_length=255)
    reason: Optional[str] = Field(default=None, max_length=2000)


class ExecuteRequest(BaseSchema):
    """Execute an authorized action.

    ``async_execution`` hands the work to the remediation queue instead of doing
    it inline; a broker that cannot be reached falls back to inline execution, so
    an approved remediation is never lost to a Redis outage.
    """

    actor: str = Field(default="operator", max_length=255)
    dry_run: bool = False
    async_execution: bool = False


class RollbackRequest(BaseSchema):
    """Reverse an applied action (§32)."""

    actor: str = Field(min_length=1, max_length=255)
    reason: Optional[str] = Field(default=None, max_length=2000)


class CancelRequest(BaseSchema):
    """Cancel an action that has not started."""

    actor: str = Field(min_length=1, max_length=255)
    reason: Optional[str] = Field(default=None, max_length=2000)


class RecordExecutionRequest(BaseSchema):
    """Record a remediation a human performed outside ARGUS (§26).

    This exists because ARGUS refuses to pretend it can restart a service it holds
    no credentials for: the operator does it, and the platform records it, audits
    it and then *verifies it from telemetry* like any other action.
    """

    actor: str = Field(min_length=1, max_length=255)
    note: str = Field(min_length=1, max_length=2000)
    outcome_expected: Optional[str] = Field(default=None, max_length=500)


class SweepRequest(BaseSchema):
    """Run a remediation sweep pass now (operator-triggered)."""

    project_id: Optional[uuid.UUID] = None
    plan: bool = True


# ---------------------------------------------------------------------------
# Compound views (§44, §47)
# ---------------------------------------------------------------------------


class ActionStepResponse(BaseSchema):
    """One orchestration step, for the run view."""

    action_id: uuid.UUID
    status: RemediationStatus
    step: str
    detail: str
    failure_reason: Optional[RemediationFailureReason] = None


class RunResponse(BaseSchema):
    """The result of driving an action through the pipeline."""

    action_id: uuid.UUID
    status: RemediationStatus
    outcome: Optional[RemediationOutcome] = None
    detail: Optional[str] = None
    steps: list[ActionStepResponse] = Field(default_factory=list)


class RemediationMetricsResponse(BaseSchema):
    """Aggregate counts for the console and for alerting (§44).

    Deliberately counts *refusals* as prominently as successes: a platform whose
    policy is doing its job looks mostly like a list of things it declined to do.
    """

    project_id: Optional[uuid.UUID] = None
    total_actions: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    by_action_type: dict[str, int] = Field(default_factory=dict)
    by_failure_reason: dict[str, int] = Field(default_factory=dict)
    outcomes: dict[str, int] = Field(default_factory=dict)
    executions_attempted: int = 0
    executions_with_effect: int = 0
    verifications_passed: int = 0
    verifications_failed: int = 0
    verifications_inconclusive: int = 0
    rollbacks_succeeded: int = 0
    rollbacks_failed: int = 0
    controls_in_force: int = 0
    open_breakers: int = 0
    awaiting_approval: int = 0
    autonomous_authorizations: int = 0
    human_authorizations: int = 0
    emergency_stop_active: bool = False


class AuditChainResponse(BaseSchema):
    """Whether an action's audit chain still verifies (§12)."""

    action_id: uuid.UUID
    intact: bool
    events: int = 0
    broken_at: Optional[int] = None
    reason: Optional[str] = None


class CircuitBreakerResponse(BaseSchema):
    """One breaker and its counters (§25)."""

    action_type: RemediationActionType
    state: CircuitState
    consecutive_failures: int = 0
    total_attempts: int = 0
    total_failures: int = 0
    total_successes: int = 0
    threshold: int = 0
    opened_at: Optional[datetime] = None
    opened_until: Optional[datetime] = None
    last_failure_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_trip_reason: Optional[str] = None


class CircuitBreakerListResponse(BaseSchema):
    """Every breaker for a project."""

    breakers: list[CircuitBreakerResponse] = Field(default_factory=list)
    count: int = 0


__all__ = [
    "ActionDetailResponse",
    "ActionListResponse",
    "ActionParameterResponse",
    "ActionResponse",
    "ActionStepResponse",
    "ActionTypeListResponse",
    "ActionTypeResponse",
    "ApprovalDecisionRequest",
    "ApprovalResponse",
    "AssessmentResponse",
    "AuditChainResponse",
    "AuditEventResponse",
    "CancelRequest",
    "CircuitBreakerListResponse",
    "CircuitBreakerResponse",
    "ControlListResponse",
    "ControlResponse",
    "EmergencyStopRequest",
    "ExecuteRequest",
    "ExecutionResponse",
    "ManualProposalRequest",
    "PlanRequest",
    "PlanResponse",
    "PolicyDecisionResponse",
    "PolicyResponse",
    "PolicyUpdateRequest",
    "ProposalResponse",
    "RecordExecutionRequest",
    "RemediationMetricsResponse",
    "RollbackRequest",
    "RollbackResponse",
    "RunResponse",
    "SweepRequest",
    "VerificationResponse",
]
