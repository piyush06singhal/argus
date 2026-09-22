"""ARGUS Remediation Routes (Phase 9 §42–§44).

```text
GET    /remediation/action-types                     the registry (§4)
GET    /remediation/policy                           the effective policy (§21)
PUT    /remediation/policy                           change a policy (§21)
POST   /remediation/emergency-stop                   engage/release the stop (§34)
GET    /remediation/controls                         controls currently in force
GET    /remediation/breakers                         breaker states (§25)

POST   /remediation/actions/plan                     run the planner (§8)
POST   /remediation/actions/propose                  a human proposal (§7)
GET    /remediation/actions                          actions (project-scoped)
GET    /remediation/actions/{id}                     the full evidence chain (§44)
GET    /remediation/actions/{id}/audit               the audit trail (§12)
GET    /remediation/actions/{id}/audit/verify        does the chain verify? (§12)
POST   /remediation/actions/{id}/assess              re-run safety (§19)
POST   /remediation/actions/{id}/evaluate            re-run policy (§21)
POST   /remediation/actions/{id}/approve             approve (§22)
POST   /remediation/actions/{id}/reject              reject (§22)
POST   /remediation/actions/{id}/execute             execute (§26)
POST   /remediation/actions/{id}/run                 drive the whole pipeline
POST   /remediation/actions/{id}/verify              verify (§28)
POST   /remediation/actions/{id}/rollback            roll back (§32)
POST   /remediation/actions/{id}/cancel              cancel (§5)
POST   /remediation/actions/{id}/record-execution    record a manual execution (§26)
GET    /remediation/incidents/{id}/actions           that incident's actions
GET    /remediation/metrics                          aggregate counts (§44)
POST   /remediation/sweep                            run a sweep now (§39)
```

Scope rules follow the Phase 3–8 convention: mutating requests **require** a
project and prove ownership, reads accept an optional project and enforce it when
supplied, and an out-of-scope id answers 404 rather than confirming existence.

There is no endpoint that accepts a command, a script or a URL, and none that
executes anything the gates have not already approved. The most powerful thing
this file can do is ask the platform to reconsider an action it has already
assessed.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_environment, require_incident, require_project
from app.core.config import get_settings
from app.core.database import get_db
from app.models.remediation import (
    BlastRadiusScope,
    CircuitState,
    ExecutionStatus,
    RemediationAction,
    RemediationActionType,
    RemediationActorType,
    RemediationRiskLevel,
    RemediationApproval,
    RemediationApprovalStatus,
    RemediationAssessment,
    RemediationAuditEvent,
    RemediationAuditEventType,
    RemediationCircuitBreaker,
    RemediationControl,
    RemediationExecution,
    RemediationExecutionMode,
    RemediationPolicy,
    RemediationPolicyDecision,
    RemediationProposal,
    RemediationRollback,
    RemediationSourceType,
    RemediationStatus,
    RemediationVerification,
    RollbackStatus,
    RollbackTrigger,
    VerificationVerdict,
)
from app.schemas.remediation import (
    ActionDetailResponse,
    ActionListResponse,
    ActionParameterResponse,
    ActionResponse,
    ActionStepResponse,
    ActionTypeListResponse,
    ActionTypeResponse,
    ApprovalDecisionRequest,
    ApprovalResponse,
    AssessmentResponse,
    AuditChainResponse,
    AuditEventResponse,
    CancelRequest,
    CircuitBreakerListResponse,
    CircuitBreakerResponse,
    ControlListResponse,
    ControlResponse,
    EmergencyStopRequest,
    ExecuteRequest,
    ExecutionResponse,
    ManualProposalRequest,
    PlanRequest,
    PlanResponse,
    PolicyDecisionResponse,
    PolicyResponse,
    PolicyUpdateRequest,
    ProposalResponse,
    RecordExecutionRequest,
    RemediationMetricsResponse,
    RollbackRequest,
    RollbackResponse,
    RunResponse,
    SweepRequest,
    VerificationResponse,
)
from app.services.queue import enqueue_remediation_action
from app.services.remediation_clock import aware, is_expired, utcnow
from app.services.remediation_controls import (
    current_controls,
    describe as describe_control,
)
from app.services.remediation_planner import ProposalDraft, RemediationPlanner
from app.services.remediation_policy import resolve_policy, upsert_policy
from app.services.remediation_registry import (
    all_definitions,
    definition_summary,
    get_definition,
    validate_parameters,
)
from app.services.remediation_service import RemediationService
from app.services.remediation_state import allowed_targets, apply_transition
from app.services.remediation_sweep import sweep_remediations_once

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()

_LIST_LIMIT = 200


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _coerce(enum_type: Any, value: Any) -> Any:
    """Turn a request-body enum value back into a real enum member.

    ``BaseSchema`` sets ``use_enum_values``, so every enum in a request body
    arrives as a plain string. The services and the registry work with enum
    members (``.value``, ``is`` comparisons, rank lookups), so the boundary is
    where the conversion belongs — the same convention the earlier phases use.
    ``None`` passes through, because "not supplied" is a distinct instruction
    from "supplied and empty".
    """
    if value is None:
        return None
    return value if isinstance(value, enum_type) else enum_type(value)


async def _require_action(
    db: AsyncSession, action_id: uuid.UUID, project_id: Optional[uuid.UUID]
) -> RemediationAction:
    """Load an action, enforcing project scope when one is supplied."""
    action = await db.get(RemediationAction, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Remediation action not found")
    if project_id is not None and action.project_id != project_id:
        raise HTTPException(status_code=404, detail="Remediation action not found")
    return action


def _action_response(action: RemediationAction) -> ActionResponse:
    return ActionResponse(
        id=action.id,
        project_id=action.project_id,
        environment_id=action.environment_id,
        component_id=action.component_id,
        proposal_id=action.proposal_id,
        action_type=action.action_type,
        status=action.status,
        description=action.description,
        reason=action.reason,
        headline=action.headline,
        risk_level=action.risk_level,
        blast_radius=action.blast_radius,
        blast_radius_percent=action.blast_radius_percent,
        affected_resource_count=action.affected_resource_count,
        source_type=action.source_type,
        source_id=action.source_id,
        parameters=action.parameters,
        safety_status=action.safety_status,
        policy_status=action.policy_status,
        authorization_status=action.authorization_status,
        execution_status=action.execution_status,
        execution_mode=action.execution_mode,
        adapter_kind=action.adapter_kind,
        rollback_strategy=action.rollback_strategy,
        rollback_available=action.rollback_available,
        rollback_plan=action.rollback_plan,
        verification_plan=action.verification_plan,
        preconditions=action.preconditions,
        canary_required=action.canary_required,
        canary_stage=action.canary_stage,
        canary_percent=action.canary_percent,
        attempt=action.attempt,
        retry_count=action.retry_count,
        max_retries=action.max_retries,
        failure_reason=action.failure_reason,
        failure_detail=action.failure_detail,
        outcome=action.outcome,
        post_analysis_status=action.post_analysis_status,
        fingerprint=action.fingerprint,
        incident_id=action.incident_id,
        forecast_id=action.forecast_id,
        patch_id=action.patch_id,
        created_by=action.created_by,
        approved_by=action.approved_by,
        authorized_by=action.authorized_by,
        executed_by=action.executed_by,
        created_at=action.created_at,
        approved_at=action.approved_at,
        authorized_at=action.authorized_at,
        started_at=action.started_at,
        completed_at=action.completed_at,
        expires_at=action.expires_at,
        rollback_performed_at=action.rollback_performed_at,
    )


def _proposal_response(row: RemediationProposal) -> ProposalResponse:
    return ProposalResponse(
        id=row.id,
        project_id=row.project_id,
        environment_id=row.environment_id,
        component_id=row.component_id,
        action_type=row.action_type,
        source_type=row.source_type,
        source_id=row.source_id,
        strategy=row.strategy,
        problem=row.problem,
        recommended_action=row.recommended_action,
        expected_effect=row.expected_effect,
        supporting_evidence=row.supporting_evidence,
        parameters=row.parameters,
        risk_level=row.risk_level,
        blast_radius=row.blast_radius,
        blast_radius_percent=row.blast_radius_percent,
        preconditions=row.preconditions,
        verification_plan=row.verification_plan,
        rollback_plan=row.rollback_plan,
        confidence=row.confidence,
        confidence_reason=row.confidence_reason,
        limitations=row.limitations,
        rationale=row.rationale,
        generated_by=row.generated_by,
        model_version=row.model_version,
        incident_id=row.incident_id,
        forecast_id=row.forecast_id,
        causal_analysis_id=row.causal_analysis_id,
        root_cause_candidate_id=row.root_cause_candidate_id,
        patch_id=row.patch_id,
        fingerprint=row.fingerprint,
        created_at=row.created_at,
    )


def _assessment_response(row: RemediationAssessment) -> AssessmentResponse:
    return AssessmentResponse(
        id=row.id,
        status=row.status,
        checks=row.checks,
        blocking=row.blocking,
        warnings=row.warnings,
        reversible=row.reversible,
        rollback_plan=row.rollback_plan,
        blast_radius=row.blast_radius,
        blast_radius_percent=row.blast_radius_percent,
        affected_resource_count=row.affected_resource_count,
        requires_human_approval=row.requires_human_approval,
        reason=row.reason,
        assessed_by=row.assessed_by,
        created_at=row.created_at,
    )


def _policy_decision_response(
    row: RemediationPolicyDecision,
) -> PolicyDecisionResponse:
    return PolicyDecisionResponse(
        id=row.id,
        policy_id=row.policy_id,
        decision=row.decision,
        execution_mode=row.execution_mode,
        policy_revision=row.policy_revision,
        matched_rules=row.matched_rules,
        reasons=row.reasons,
        failure_reason=row.failure_reason,
        requires_canary=row.requires_canary,
        budget_state=row.budget_state,
        circuit_state=row.circuit_state,
        evaluated_by=row.evaluated_by,
        created_at=row.created_at,
    )


def _approval_response(row: RemediationApproval) -> ApprovalResponse:
    return ApprovalResponse(
        id=row.id,
        status=row.status,
        actor_type=row.actor_type,
        actor=row.actor,
        decided_at=row.decided_at,
        expires_at=row.expires_at,
        reason=row.reason,
        scope_snapshot=row.scope_snapshot,
        created_at=row.created_at,
    )


def _execution_response(row: RemediationExecution) -> ExecutionResponse:
    return ExecutionResponse(
        id=row.id,
        attempt=row.attempt,
        mode=row.mode,
        adapter_kind=row.adapter_kind,
        adapter_name=row.adapter_name,
        status=row.status,
        effect_applied=row.effect_applied,
        steps=row.steps,
        control_ids=row.control_ids,
        output_summary=row.output_summary,
        failure_reason=row.failure_reason,
        error=row.error,
        idempotency_key=row.idempotency_key,
        started_at=row.started_at,
        completed_at=row.completed_at,
        duration_ms=row.duration_ms,
        executed_by=row.executed_by,
    )


def _verification_response(row: RemediationVerification) -> VerificationResponse:
    return VerificationResponse(
        id=row.id,
        execution_id=row.execution_id,
        verdict=row.verdict,
        checks=row.checks,
        passed_count=row.passed_count,
        failed_count=row.failed_count,
        not_observable_count=row.not_observable_count,
        window_start=row.window_start,
        window_end=row.window_end,
        observation_seconds=row.observation_seconds,
        summary=row.summary,
        limitations=row.limitations,
        verified_by=row.verified_by,
        created_at=row.created_at,
    )


def _rollback_response(row: RemediationRollback) -> RollbackResponse:
    return RollbackResponse(
        id=row.id,
        trigger=row.trigger.value,
        strategy=row.strategy,
        status=row.status,
        plan=row.plan,
        steps=row.steps,
        controls_reverted=row.controls_reverted,
        verification_verdict=row.verification_verdict,
        failure_reason=row.failure_reason,
        error=row.error,
        requested_by=row.requested_by,
        started_at=row.started_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
    )


def _audit_response(row: RemediationAuditEvent) -> AuditEventResponse:
    return AuditEventResponse(
        id=row.id,
        sequence=row.sequence,
        event_type=row.event_type.value,
        actor_type=row.actor_type,
        actor=row.actor,
        from_status=row.from_status,
        to_status=row.to_status,
        summary=row.summary,
        detail=row.detail,
        occurred_at=row.occurred_at,
        entry_hash=row.entry_hash,
        prev_hash=row.prev_hash,
    )


def _policy_response(policy: Any, row: Any = None) -> PolicyResponse:
    """Render an effective policy (with its ceiling clamps) as a response."""
    return PolicyResponse(
        id=policy.policy_id,
        project_id=row.project_id if row is not None else None,
        environment_id=row.environment_id if row is not None else None,
        source=policy.source,
        revision=policy.revision,
        enabled=row.enabled if row is not None else True,
        execution_mode=policy.execution_mode,
        autonomous_max_risk=policy.autonomous_max_risk,
        allowed_action_types=(
            list(policy.allowed_action_types) if policy.allowed_action_types else None
        ),
        allowed_environment_names=(
            list(policy.allowed_environment_names)
            if policy.allowed_environment_names
            else None
        ),
        max_actions_per_window=policy.max_actions_per_window,
        action_window_seconds=policy.action_window_seconds,
        cooldown_seconds=policy.cooldown_seconds,
        max_concurrent_actions=policy.max_concurrent_actions,
        max_blast_radius_percent=policy.max_blast_radius_percent,
        max_blast_radius_scope=policy.max_blast_radius_scope,
        canary_enabled=policy.canary_enabled,
        canary_percent=policy.canary_percent,
        approval_ttl_seconds=policy.approval_ttl_seconds,
        verification_window_seconds=policy.verification_window_seconds,
        execution_timeout_seconds=policy.execution_timeout_seconds,
        action_expiry_seconds=policy.action_expiry_seconds,
        emergency_stop_active=policy.emergency_stop_active,
        emergency_stop_reason=policy.emergency_stop_reason,
        emergency_stop_at=row.emergency_stop_at if row is not None else None,
        emergency_stop_by=row.emergency_stop_by if row is not None else None,
        updated_by=row.updated_by if row is not None else None,
        clamped=list(policy.clamped),
        notes=row.notes if row is not None else None,
    )


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


@router.get("/remediation/action-types", response_model=ActionTypeListResponse)
async def list_action_types() -> ActionTypeListResponse:
    """Every action ARGUS is willing to perform, with its safety properties (§4)."""
    actions = []
    for definition in all_definitions():
        summary = definition_summary(definition)
        actions.append(
            ActionTypeResponse(
                action_type=definition.action_type,
                description=definition.description,
                risk_level=definition.risk_level,
                adapter_kind=definition.adapter_kind,
                parameters=[
                    ActionParameterResponse(**parameter)
                    for parameter in summary["parameters"]
                ],
                verification_plan=summary["verification_plan"],
                rollback_strategy=definition.rollback_strategy,
                inverse_action=definition.inverse_action,
                maximum_blast_radius=definition.maximum_blast_radius,
                production_effect=definition.production_effect,
                supports_canary=definition.supports_canary,
                supports_autonomous_execution=definition.supports_autonomous_execution,
                requires_human_approval=summary["requires_human_approval"],
                reversible=definition.reversible,
                executable_in_build=summary["executable_in_build"],
                unavailable_reason=definition.unavailable_reason,
                notes=list(definition.notes),
            )
        )
    return ActionTypeListResponse(
        actions=actions,
        count=len(actions),
        execution_enabled=settings.REMEDIATION_EXECUTION_ENABLED,
    )


# ---------------------------------------------------------------------------
# policy + stop + controls
# ---------------------------------------------------------------------------


@router.get("/remediation/policy", response_model=PolicyResponse)
async def get_policy(
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    environment_id: Optional[uuid.UUID] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> PolicyResponse:
    """The policy that would govern an action in this scope.

    Shown even when no row exists: a project on the restrictive default needs to
    see that it is on the restrictive default.
    """
    await require_project(db, project_id)
    await require_environment(db, project_id, environment_id)
    effective = await resolve_policy(db, project_id, environment_id)
    row = None
    if effective.policy_id is not None:
        row = await db.get(RemediationPolicy, effective.policy_id)
    return _policy_response(effective, row)


@router.put("/remediation/policy", response_model=PolicyResponse)
async def put_policy(
    request: PolicyUpdateRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> PolicyResponse:
    """Create or update a scope's policy.

    Turning on ``AUTONOMOUS`` is an explicit, audited configuration change, and
    the process's hard ceilings still apply on top: this endpoint can narrow what
    ARGUS may do, never widen it beyond what the operator started the process with.
    """
    await require_project(db, project_id)
    environment_id = request.environment_id
    await require_environment(db, project_id, environment_id)
    values = request.model_dump(exclude_unset=True, exclude_none=True)
    values.pop("environment_id", None)
    actor = values.pop("updated_by", None) or "api"
    #: Restore the real enum members the stored row is typed with.
    for field, enum_type in (
        ("execution_mode", RemediationExecutionMode),
        ("autonomous_max_risk", RemediationRiskLevel),
    ):
        if values.get(field) is not None:
            values[field] = _coerce(enum_type, values[field])

    if request.allowed_action_types is not None:
        unknown = [
            name
            for name in request.allowed_action_types
            if name not in {t.value for t in RemediationActionType}
        ]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"unknown action types in the allow-list: {', '.join(unknown)}",
            )

    row = await upsert_policy(db, project_id, environment_id, values, updated_by=actor)
    from app.services.remediation_audit import AuditTrail

    await AuditTrail(db).record(
        None,
        RemediationAuditEventType.POLICY_UPDATED,
        project_id=project_id,
        environment_id=environment_id,
        actor_type=RemediationActorType.HUMAN,
        actor=actor,
        summary=f"policy revision {row.revision} saved",
        detail={
            "changed": sorted(values.keys()),
            "execution_mode": row.execution_mode.value,
        },
    )
    await db.commit()
    effective = await resolve_policy(db, project_id, environment_id)
    return _policy_response(effective, row)


@router.post("/remediation/emergency-stop", response_model=PolicyResponse)
async def emergency_stop(
    request: EmergencyStopRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> PolicyResponse:
    """Engage or release the project's kill switch (§34).

    Engaging blocks every open action immediately. Releasing authorizes nothing:
    each action must clear its gates again.
    """
    await require_project(db, project_id)
    service = RemediationService(db)
    row = await service.engage_emergency_stop(
        project_id,
        engage=request.engage,
        actor=request.actor,
        reason=request.reason,
    )
    await db.commit()
    effective = await resolve_policy(db, project_id, None)
    return _policy_response(effective, row)


@router.get("/remediation/controls", response_model=ControlListResponse)
async def list_controls(
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    environment_id: Optional[uuid.UUID] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> ControlListResponse:
    """Every ARGUS control currently holding something down (§10)."""
    await require_project(db, project_id)
    await require_environment(db, project_id, environment_id)
    #: Without an environment the console wants everything in force for the
    #: project, not just the environment-less rows — otherwise it would report
    #: "nothing is paused" while a sweep is paused in one environment.
    rows = await current_controls(
        db,
        project_id=project_id,
        environment_id=environment_id,
        any_environment=environment_id is None,
    )
    now = aware(utcnow())
    controls = []
    for row in rows:
        rendered = describe_control(row)
        controls.append(
            ControlResponse(
                **rendered,
                applied_by_action_id=row.applied_by_action_id,
                effective=not is_expired(row.expires_at, now=now),
            )
        )
    return ControlListResponse(controls=controls, count=len(controls))


@router.get("/remediation/breakers", response_model=CircuitBreakerListResponse)
async def list_breakers(
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> CircuitBreakerListResponse:
    """Breaker state per action type, so a refusal can be explained (§25)."""
    await require_project(db, project_id)
    rows = (
        (
            await db.execute(
                select(RemediationCircuitBreaker)
                .where(RemediationCircuitBreaker.project_id == project_id)
                .order_by(RemediationCircuitBreaker.action_type)
            )
        )
        .scalars()
        .all()
    )
    return CircuitBreakerListResponse(
        breakers=[
            CircuitBreakerResponse(
                action_type=row.action_type,
                state=row.state,
                consecutive_failures=row.consecutive_failures,
                total_attempts=row.total_attempts,
                total_failures=row.total_failures,
                total_successes=row.total_successes,
                threshold=row.threshold,
                opened_at=row.opened_at,
                opened_until=row.opened_until,
                last_failure_at=row.last_failure_at,
                last_success_at=row.last_success_at,
                last_trip_reason=row.last_trip_reason,
            )
            for row in rows
        ],
        count=len(rows),
    )


# ---------------------------------------------------------------------------
# planning + proposals
# ---------------------------------------------------------------------------


@router.post("/remediation/actions/plan", response_model=PlanResponse)
async def plan_actions(
    request: PlanRequest, db: AsyncSession = Depends(get_db)
) -> PlanResponse:
    """Ask the planner for evidence-backed remediation proposals (§8)."""
    await require_project(db, request.project_id)
    await require_environment(db, request.project_id, request.environment_id)

    planner = RemediationPlanner(db)
    drafts: list[ProposalDraft] = []
    if request.incident_id is not None:
        incident = await require_incident(
            db, request.incident_id, project_id=request.project_id
        )
        drafts = await planner.plan_for_incident(incident)
    elif request.forecast_id is not None:
        from app.models.reliability import ReliabilityForecast

        forecast = await db.get(ReliabilityForecast, request.forecast_id)
        if forecast is None or forecast.project_id != request.project_id:
            raise HTTPException(status_code=404, detail="Forecast not found")
        drafts = await planner.plan_for_forecast(forecast)
    else:
        raise HTTPException(
            status_code=422,
            detail=(
                "a plan needs a source: pass incident_id or forecast_id. Planning "
                "never invents a target."
            ),
        )

    service = RemediationService(db)
    before = await _count_actions(db, request.project_id)
    actions = await service.propose(drafts, created_by="remediation-planner")
    created = len(actions)
    skipped = max(0, len(drafts) - created)

    if request.auto_assess:
        for action in actions:
            await service.assess(action)
            if action.status == RemediationStatus.POLICY_REVIEW:
                await service.evaluate_policy(action)
    await db.commit()
    del before
    return PlanResponse(
        project_id=request.project_id,
        proposals_created=created,
        actions_created=created,
        actions=[_action_response(action) for action in actions],
        skipped_duplicates=skipped,
        detail=(
            "no strategy produced a proposal for this source; the evidence does "
            "not support a specific remediation yet"
            if created == 0
            else None
        ),
    )


@router.post("/remediation/actions/propose", response_model=ActionResponse)
async def propose_action(
    request: ManualProposalRequest, db: AsyncSession = Depends(get_db)
) -> ActionResponse:
    """Record a human-proposed remediation, then run it through the gates (§7).

    The proposal is stored with ``HUMAN_OPERATOR`` provenance. It is still
    assessed and still subject to policy: proposing is not authorizing.
    """
    await require_project(db, request.project_id)
    await require_environment(db, request.project_id, request.environment_id)
    if request.incident_id is not None:
        await require_incident(db, request.incident_id, project_id=request.project_id)

    action_type = _coerce(RemediationActionType, request.action_type)
    clean, errors = validate_parameters(action_type, request.parameters)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    definition = get_definition(action_type)

    draft = ProposalDraft(
        project_id=request.project_id,
        environment_id=request.environment_id,
        component_id=request.component_id,
        action_type=action_type,
        source_type=RemediationSourceType.HUMAN_OPERATOR,
        source_id=request.incident_id,
        strategy="OPERATOR",
        problem=request.description,
        recommended_action=request.description,
        expected_effect=request.reason or "the operator expects the described effect",
        rationale=request.reason or "proposed directly by an operator",
        risk_level=definition.risk_level,
        blast_radius=(
            _coerce(BlastRadiusScope, request.blast_radius)
            or definition.maximum_blast_radius
        ),
        blast_radius_percent=request.blast_radius_percent,
        confidence=0.5,
        confidence_reason=(
            "a human operator proposed this; it carries no inferred evidence beyond "
            "the identifiers supplied"
        ),
        limitations=[
            "this remediation was proposed by a person rather than inferred from "
            "evidence, so its confidence reflects only the operator's judgement",
            "the safety and policy gates still apply and may refuse it",
        ],
        supporting_evidence=[
            {
                "kind": "operator_proposal",
                "actor": request.created_by,
                "reason": request.reason,
            }
        ],
        parameters=clean,
        incident_id=request.incident_id,
        forecast_id=request.forecast_id,
        patch_id=request.patch_id,
    )
    service = RemediationService(db)
    actions = await service.propose(
        [draft], created_by=request.created_by or "operator"
    )
    if not actions:
        raise HTTPException(
            status_code=409,
            detail=(
                "an equivalent remediation already exists and is not finished; "
                "duplicates are refused rather than queued"
            ),
        )
    action = actions[0]
    if request.auto_assess:
        await service.assess(action)
        if action.status == RemediationStatus.POLICY_REVIEW:
            await service.evaluate_policy(action)
    await db.commit()
    return _action_response(action)


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


@router.get("/remediation/actions", response_model=ActionListResponse)
async def list_actions(
    project_id: Optional[uuid.UUID] = Query(default=None),
    environment_id: Optional[uuid.UUID] = Query(default=None),
    status: Optional[str] = Query(default=None),
    action_type: Optional[str] = Query(default=None),
    incident_id: Optional[uuid.UUID] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> ActionListResponse:
    """Actions, newest first, with optional filters."""
    stmt = select(RemediationAction)
    count_stmt = select(func.count(RemediationAction.id))
    conditions = []
    if project_id is not None:
        await require_project(db, project_id)
        conditions.append(RemediationAction.project_id == project_id)
    if environment_id is not None:
        conditions.append(RemediationAction.environment_id == environment_id)
    if status is not None:
        try:
            status_value = RemediationStatus(status)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="unknown status") from error
        conditions.append(RemediationAction.status == status_value)
    if action_type is not None:
        try:
            type_value = RemediationActionType(action_type)
        except ValueError as error:
            raise HTTPException(
                status_code=422, detail="unknown action type"
            ) from error
        conditions.append(RemediationAction.action_type == type_value)
    if incident_id is not None:
        conditions.append(RemediationAction.incident_id == incident_id)

    for condition in conditions:
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    total = int((await db.execute(count_stmt)).scalar() or 0)
    rows = (
        (
            await db.execute(
                stmt.order_by(RemediationAction.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return ActionListResponse(
        actions=[_action_response(row) for row in rows],
        count=len(rows),
        total=total,
    )


@router.get("/remediation/actions/{action_id}", response_model=ActionDetailResponse)
async def get_action(
    action_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> ActionDetailResponse:
    """One action with every gate result, execution, verification and audit event."""
    action = await _require_action(db, action_id, project_id)
    assessments = (
        (
            await db.execute(
                select(RemediationAssessment)
                .where(RemediationAssessment.action_id == action_id)
                .order_by(RemediationAssessment.created_at)
            )
        )
        .scalars()
        .all()
    )
    decisions = (
        (
            await db.execute(
                select(RemediationPolicyDecision)
                .where(RemediationPolicyDecision.action_id == action_id)
                .order_by(RemediationPolicyDecision.created_at)
            )
        )
        .scalars()
        .all()
    )
    approvals = (
        (
            await db.execute(
                select(RemediationApproval)
                .where(RemediationApproval.action_id == action_id)
                .order_by(RemediationApproval.created_at)
            )
        )
        .scalars()
        .all()
    )
    executions = (
        (
            await db.execute(
                select(RemediationExecution)
                .where(RemediationExecution.action_id == action_id)
                .order_by(RemediationExecution.attempt)
            )
        )
        .scalars()
        .all()
    )
    verifications = (
        (
            await db.execute(
                select(RemediationVerification)
                .where(RemediationVerification.action_id == action_id)
                .order_by(RemediationVerification.created_at)
            )
        )
        .scalars()
        .all()
    )
    rollbacks = (
        (
            await db.execute(
                select(RemediationRollback)
                .where(RemediationRollback.action_id == action_id)
                .order_by(RemediationRollback.created_at)
            )
        )
        .scalars()
        .all()
    )
    audit = (
        (
            await db.execute(
                select(RemediationAuditEvent)
                .where(RemediationAuditEvent.action_id == action_id)
                .order_by(RemediationAuditEvent.sequence)
            )
        )
        .scalars()
        .all()
    )
    proposal = None
    if action.proposal_id is not None:
        proposal_row = await db.get(RemediationProposal, action.proposal_id)
        if proposal_row is not None:
            proposal = _proposal_response(proposal_row)

    from app.services.remediation_audit import AuditTrail

    chain = await AuditTrail(db).verify_chain(action_id)
    return ActionDetailResponse(
        action=_action_response(action),
        proposal=proposal,
        assessments=[_assessment_response(row) for row in assessments],
        policy_decisions=[_policy_decision_response(row) for row in decisions],
        approvals=[_approval_response(row) for row in approvals],
        executions=[_execution_response(row) for row in executions],
        verifications=[_verification_response(row) for row in verifications],
        rollbacks=[_rollback_response(row) for row in rollbacks],
        audit=[_audit_response(row) for row in audit],
        audit_chain={
            "intact": chain["intact"],
            "events": chain["events"],
            "broken_at": chain["broken_at"],
            "reason": chain["reason"],
        },
        post_analysis=action.post_analysis,
        allowed_transitions=sorted(
            allowed_targets(action.status), key=lambda s: s.value
        ),
    )


@router.get(
    "/remediation/actions/{action_id}/audit", response_model=list[AuditEventResponse]
)
async def get_audit(
    action_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[AuditEventResponse]:
    """The hash-chained audit trail for one action (§12)."""
    await _require_action(db, action_id, project_id)
    from app.services.remediation_audit import AuditTrail

    rows = await AuditTrail(db).history(action_id)
    return [_audit_response(row) for row in rows]


@router.get(
    "/remediation/actions/{action_id}/audit/verify", response_model=AuditChainResponse
)
async def verify_audit(
    action_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> AuditChainResponse:
    """Recompute the audit chain and report the first break, if any."""
    await _require_action(db, action_id, project_id)
    from app.services.remediation_audit import AuditTrail

    result = await AuditTrail(db).verify_chain(action_id)
    return AuditChainResponse(action_id=action_id, **result)


async def _gate_action(
    db: AsyncSession,
    action_id: uuid.UUID,
    project_id: Optional[uuid.UUID],
    *,
    gate: str,
) -> RemediationAction:
    action = await _require_action(db, action_id, project_id)
    service = RemediationService(db)
    try:
        if gate == "assess":
            await service.assess(action)
        elif gate == "evaluate":
            await service.evaluate_policy(action)
        elif gate == "verify":
            await service.verify(action)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await db.commit()
    return action


@router.post("/remediation/actions/{action_id}/assess", response_model=ActionResponse)
async def assess_action(
    action_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """(Re-)run the safety assessment on a proposed action (§19)."""
    return _action_response(
        await _gate_action(db, action_id, project_id, gate="assess")
    )


@router.post("/remediation/actions/{action_id}/evaluate", response_model=ActionResponse)
async def evaluate_action(
    action_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """(Re-)run the policy engine on an assessed action (§21)."""
    return _action_response(
        await _gate_action(db, action_id, project_id, gate="evaluate")
    )


@router.post("/remediation/actions/{action_id}/verify", response_model=ActionResponse)
async def verify_action(
    action_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """Run verification against an action that has applied its effect (§28)."""
    return _action_response(
        await _gate_action(db, action_id, project_id, gate="verify")
    )


@router.post("/remediation/actions/{action_id}/approve", response_model=ActionResponse)
async def approve_action(
    action_id: uuid.UUID,
    request: ApprovalDecisionRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """Approve a pending action and authorize it (§22).

    Every gate is re-evaluated on approval: the policy may have changed, the
    budget may be spent, or an emergency stop may have been engaged meanwhile.
    """
    action = await _require_action(db, action_id, project_id)
    service = RemediationService(db)
    try:
        action, evaluation = await service.decide(
            action, approve=True, actor=request.actor, reason=request.reason
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await db.commit()
    if evaluation is not None and not evaluation.allowed:
        logger.info(
            "approval did not authorize action %s: %s",
            action_id,
            action.failure_reason.value if action.failure_reason else "denied",
        )
    return _action_response(action)


@router.post("/remediation/actions/{action_id}/reject", response_model=ActionResponse)
async def reject_action(
    action_id: uuid.UUID,
    request: ApprovalDecisionRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """Reject a pending action. Rejection is terminal by design (§5)."""
    action = await _require_action(db, action_id, project_id)
    service = RemediationService(db)
    try:
        action, _ = await service.decide(
            action, approve=False, actor=request.actor, reason=request.reason
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await db.commit()
    return _action_response(action)


@router.post("/remediation/actions/{action_id}/execute", response_model=RunResponse)
async def execute_action(
    action_id: uuid.UUID,
    request: ExecuteRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> RunResponse:
    """Execute an authorized action, then verify it (§26–§30).

    ``async_execution`` hands the work to the remediation queue; if the broker is
    unreachable the request falls back to executing inline, because an approved
    remediation should not be lost to a Redis outage.
    """
    action = await _require_action(db, action_id, project_id)
    if request.async_execution and not request.dry_run:
        queued = await enqueue_remediation_action(
            action_id=action.id, project_id=action.project_id
        )
        if queued:
            if action.status == RemediationStatus.AUTHORIZED:
                apply_transition(action, RemediationStatus.SCHEDULED)
                await db.commit()
            return RunResponse(
                action_id=action.id,
                status=action.status,
                detail="queued for the remediation worker",
            )

    service = RemediationService(db)
    try:
        result = await service.run(action, actor=request.actor, dry_run=request.dry_run)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await db.commit()
    return RunResponse(
        action_id=action.id,
        status=action.status,
        outcome=action.outcome,
        detail=result.get("detail"),
        steps=[ActionStepResponse(**step) for step in result.get("steps", [])],
    )


@router.post("/remediation/actions/{action_id}/run", response_model=RunResponse)
async def run_action(
    action_id: uuid.UUID,
    request: ExecuteRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> RunResponse:
    """Drive an action through every step it can currently clear.

    Safe to call repeatedly: each step is idempotent, so a run over an action that
    is already waiting on a gate simply reports where it stopped.
    """
    action = await _require_action(db, action_id, project_id)
    service = RemediationService(db)
    result = await service.run(action, actor=request.actor, dry_run=request.dry_run)
    await db.commit()
    return RunResponse(
        action_id=action.id,
        status=action.status,
        outcome=action.outcome,
        detail=result.get("detail"),
        steps=[ActionStepResponse(**step) for step in result.get("steps", [])],
    )


@router.post("/remediation/actions/{action_id}/rollback", response_model=RunResponse)
async def rollback_action(
    action_id: uuid.UUID,
    request: RollbackRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> RunResponse:
    """Reverse an applied action and verify the reversal (§32, §33)."""
    action = await _require_action(db, action_id, project_id)
    service = RemediationService(db)
    try:
        result = await service.rollback(
            action,
            trigger=RollbackTrigger.HUMAN_REQUEST,
            requested_by=request.actor,
            reason=request.reason,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await service.post_analyse(action)
    await db.commit()
    return RunResponse(
        action_id=action.id,
        status=action.status,
        outcome=action.outcome,
        detail=result.detail,
        steps=[
            ActionStepResponse(
                action_id=action.id,
                status=action.status,
                step="rollback",
                detail=result.detail,
            )
        ],
    )


@router.post("/remediation/actions/{action_id}/cancel", response_model=ActionResponse)
async def cancel_action(
    action_id: uuid.UUID,
    request: CancelRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """Cancel an action that has not started executing."""
    action = await _require_action(db, action_id, project_id)
    service = RemediationService(db)
    try:
        action = await service.cancel(
            action, actor=request.actor, reason=request.reason
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await db.commit()
    return _action_response(action)


@router.post(
    "/remediation/actions/{action_id}/record-execution", response_model=ActionResponse
)
async def record_execution(
    action_id: uuid.UUID,
    request: RecordExecutionRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> ActionResponse:
    """Record that a human performed an action ARGUS refused to automate (§26).

    The execution records the asserting person as its operator
    (``adapter_name="human_operator"``) and ``effect_applied=True`` only because a
    named person said so — and the platform then verifies that claim from
    telemetry exactly as it would its own attempt. An asserted effect that no
    telemetry supports does not verify.
    """
    action = await _require_action(db, action_id, project_id)
    if action.status != RemediationStatus.BLOCKED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"a manual execution can only be recorded for a blocked action; "
                f"this one is {action.status.value}"
            ),
        )
    from app.services.remediation_audit import AuditTrail
    from app.services.remediation_executor import idempotency_key
    from app.services.remediation_clock import utcnow as _utcnow

    now = _utcnow()
    attempt = (
        int(
            (
                await db.execute(
                    select(func.count(RemediationExecution.id)).where(
                        RemediationExecution.action_id == action.id
                    )
                )
            ).scalar()
            or 0
        )
        + 1
    )
    action.attempt = attempt
    key = idempotency_key(action)
    #: The regime recorded is ``HUMAN_APPROVAL`` — a person is the operator — and
    #: ``adapter_name`` plus the step text say plainly that ARGUS did not perform
    #: it. Adding a ``MANUAL`` enum member would need a native Postgres enum
    #: migration to say the same thing, so the honest labelling is done in the
    #: record's own fields instead.
    execution = RemediationExecution(
        action_id=action.id,
        project_id=action.project_id,
        environment_id=action.environment_id,
        component_id=action.component_id,
        action_type=action.action_type,
        mode=RemediationExecutionMode.HUMAN_APPROVAL,
        adapter_kind=action.adapter_kind,
        adapter_name="human_operator",
        attempt=attempt,
        status=ExecutionStatus.SUCCEEDED,
        effect_applied=True,
        steps=[
            {
                "step": "manual_execution",
                "detail": f"performed outside ARGUS by {request.actor}: {request.note}",
            }
        ],
        output_summary=request.note[:4000],
        idempotency_key=key,
        started_at=now,
        completed_at=now,
        executed_by=request.actor,
    )
    db.add(execution)
    action.executed_by = request.actor
    action.execution_status = ExecutionStatus.SUCCEEDED
    action.failure_reason = None
    action.failure_detail = None
    apply_transition(action, RemediationStatus.VERIFYING)
    await AuditTrail(db).record(
        action,
        RemediationAuditEventType.EXECUTION_RECORDED_MANUALLY,
        actor_type=RemediationActorType.HUMAN,
        actor=request.actor,
        summary=f"manual execution recorded: {request.note}"[:500],
        detail={"expected": request.outcome_expected},
        to_status=RemediationStatus.VERIFYING,
        now=now,
    )
    await db.flush()

    # A recorded claim is still only a claim: verify it like any other attempt.
    service = RemediationService(db)
    await service.verify(action, execution=execution, actor="manual-verification")
    await db.commit()
    return _action_response(action)


# ---------------------------------------------------------------------------
# project-level views
# ---------------------------------------------------------------------------


@router.get(
    "/remediation/incidents/{incident_id}/actions", response_model=ActionListResponse
)
async def incident_actions(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> ActionListResponse:
    """Every remediation attempt recorded against one incident."""
    await require_incident(db, incident_id, project_id=project_id)
    rows = (
        (
            await db.execute(
                select(RemediationAction)
                .where(RemediationAction.incident_id == incident_id)
                .order_by(RemediationAction.created_at.desc())
                .limit(_LIST_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    return ActionListResponse(
        actions=[_action_response(row) for row in rows],
        count=len(rows),
        total=len(rows),
    )


@router.get("/remediation/metrics", response_model=RemediationMetricsResponse)
async def remediation_metrics(
    project_id: Optional[uuid.UUID] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> RemediationMetricsResponse:
    """Aggregate remediation activity for a scope (§44).

    Refusals are counted as prominently as successes: a platform whose policy is
    working looks mostly like a list of things it declined to do.
    """
    if project_id is not None:
        await require_project(db, project_id)

    def scoped(stmt, column):
        return stmt.where(column == project_id) if project_id is not None else stmt

    status_rows = (
        await db.execute(
            scoped(
                select(RemediationAction.status, func.count(RemediationAction.id)),
                RemediationAction.project_id,
            ).group_by(RemediationAction.status)
        )
    ).all()
    type_rows = (
        await db.execute(
            scoped(
                select(RemediationAction.action_type, func.count(RemediationAction.id)),
                RemediationAction.project_id,
            ).group_by(RemediationAction.action_type)
        )
    ).all()
    reason_rows = (
        await db.execute(
            scoped(
                select(
                    RemediationAction.failure_reason, func.count(RemediationAction.id)
                ),
                RemediationAction.project_id,
            )
            .where(RemediationAction.failure_reason.is_not(None))
            .group_by(RemediationAction.failure_reason)
        )
    ).all()
    outcome_rows = (
        await db.execute(
            scoped(
                select(RemediationAction.outcome, func.count(RemediationAction.id)),
                RemediationAction.project_id,
            )
            .where(RemediationAction.outcome.is_not(None))
            .group_by(RemediationAction.outcome)
        )
    ).all()
    execution_rows = (
        await db.execute(
            scoped(
                select(
                    RemediationExecution.effect_applied,
                    func.count(RemediationExecution.id),
                ),
                RemediationExecution.project_id,
            ).group_by(RemediationExecution.effect_applied)
        )
    ).all()
    verdict_rows = (
        await db.execute(
            scoped(
                select(
                    RemediationVerification.verdict,
                    func.count(RemediationVerification.id),
                ),
                RemediationVerification.project_id,
            ).group_by(RemediationVerification.verdict)
        )
    ).all()
    rollback_rows = (
        await db.execute(
            scoped(
                select(RemediationRollback.status, func.count(RemediationRollback.id)),
                RemediationRollback.project_id,
            ).group_by(RemediationRollback.status)
        )
    ).all()
    approval_rows = (
        await db.execute(
            scoped(
                select(
                    RemediationApproval.actor_type, func.count(RemediationApproval.id)
                ),
                RemediationApproval.project_id,
            )
            .where(RemediationApproval.status == RemediationApprovalStatus.APPROVED)
            .group_by(RemediationApproval.actor_type)
        )
    ).all()

    controls_query = select(func.count(RemediationControl.id)).where(
        RemediationControl.is_current.is_(True)
    )
    if project_id is not None:
        controls_query = controls_query.where(
            RemediationControl.project_id == project_id
        )
    controls_in_force = int((await db.execute(controls_query)).scalar() or 0)

    breakers_query = select(func.count(RemediationCircuitBreaker.id)).where(
        RemediationCircuitBreaker.state == CircuitState.OPEN
    )
    if project_id is not None:
        breakers_query = breakers_query.where(
            RemediationCircuitBreaker.project_id == project_id
        )
    open_breakers = int((await db.execute(breakers_query)).scalar() or 0)

    emergency_stop_active = False
    if project_id is not None:
        effective = await resolve_policy(db, project_id, None)
        emergency_stop_active = effective.emergency_stop_active

    by_status = {row[0].value: int(row[1]) for row in status_rows}
    by_verdict = {row[0].value: int(row[1]) for row in verdict_rows}
    by_rollback = {row[0].value: int(row[1]) for row in rollback_rows}
    by_actor = {row[0].value: int(row[1]) for row in approval_rows}

    return RemediationMetricsResponse(
        project_id=project_id,
        total_actions=sum(by_status.values()),
        by_status=by_status,
        by_action_type={row[0].value: int(row[1]) for row in type_rows},
        by_failure_reason={row[0].value: int(row[1]) for row in reason_rows},
        outcomes={row[0].value: int(row[1]) for row in outcome_rows},
        executions_attempted=sum(int(row[1]) for row in execution_rows),
        executions_with_effect=sum(
            int(row[1]) for row in execution_rows if row[0] is True
        ),
        verifications_passed=by_verdict.get(VerificationVerdict.VERIFIED.value, 0),
        verifications_failed=by_verdict.get(VerificationVerdict.FAILED.value, 0),
        verifications_inconclusive=by_verdict.get(
            VerificationVerdict.INCONCLUSIVE.value, 0
        ),
        rollbacks_succeeded=by_rollback.get(RollbackStatus.SUCCEEDED.value, 0),
        rollbacks_failed=by_rollback.get(RollbackStatus.FAILED.value, 0),
        controls_in_force=controls_in_force,
        open_breakers=open_breakers,
        awaiting_approval=by_status.get(RemediationStatus.AWAITING_APPROVAL.value, 0),
        autonomous_authorizations=by_actor.get(
            RemediationActorType.AUTONOMOUS_POLICY.value, 0
        ),
        human_authorizations=by_actor.get(RemediationActorType.HUMAN.value, 0),
        emergency_stop_active=emergency_stop_active,
    )


@router.post("/remediation/sweep", response_model=dict)
async def run_sweep(request: SweepRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """Run one remediation sweep pass now (§39).

    Operator-triggered for the same reason the Phase 8 sweep exposes itself: a
    scheduled pass that only runs on a timer is impossible to reason about during
    an incident.
    """
    if request.project_id is not None:
        await require_project(db, request.project_id)
    await db.commit()
    from app.core.database import async_session_factory

    summary = await sweep_remediations_once(
        async_session_factory, project_id=request.project_id, plan=request.plan
    )
    return summary


async def _count_actions(db: AsyncSession, project_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count(RemediationAction.id)).where(
                    RemediationAction.project_id == project_id
                )
            )
        ).scalar()
        or 0
    )


__all__ = ["router"]
