"""ARGUS Remediation Orchestration (Phase 9 §11, §22, §26, §36, §37, §39).

The service is where the gates become a pipeline. Each step is a method that
persists its own row, moves the action through the state machine and appends to
the audit chain — so any step can be called on its own (by the API, by a worker or
by the sweeper) and the action's history is complete either way.

    propose → assess (safety) → evaluate_policy → approve/authorize
            → execute → verify → roll back / post-analyse

Five rules that shape the code:

* **Every gate writes a row.** A refusal is stored, not returned and forgotten:
  ``RemediationAssessment``, ``RemediationPolicyDecision``, ``RemediationApproval``,
  ``RemediationExecution``, ``RemediationVerification`` and ``RemediationRollback``
  are all append-only, so "why did ARGUS do that?" is answerable months later.
* **Authorization is re-checked, never assumed.** Approving an action does not
  skip the policy engine: if the budget has been spent, the breaker has opened or
  an emergency stop has been engaged since, authorization fails then.
* **Autonomous authorization is a written policy, not a model.** The only path to
  an unapproved execution is ``PolicyDecision.ALLOW`` from the policy engine with
  an ``AUTONOMOUS_POLICY`` approval record. An AI-sourced proposal cannot produce
  one (§1).
* **Verification decides, not the execution.** ``VERIFIED`` is only reachable
  through a verification verdict, and a failed verdict rolls back when the action
  can be reversed.
* **Nothing here retries forever.** Attempts, retries, breaker state and the
  action's expiry are all bounded, and exhaustion escalates instead of looping.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.incident import Incident, IncidentStatus
from app.models.remediation import (
    CanaryStage,
    CircuitState,
    ExecutionStatus,
    PostAnalysisStatus,
    RemediationAction,
    RemediationActorType,
    RemediationApproval,
    RemediationApprovalStatus,
    RemediationAssessment,
    RemediationAuditEventType,
    RemediationExecution,
    RemediationExecutionMode,
    RemediationFailureReason,
    RemediationOutcome,
    RemediationPolicy,
    RemediationPolicyDecision,
    RemediationProposal,
    RemediationStatus,
    RollbackStatus,
    RollbackTrigger,
    SafetyStatus,
    VerificationVerdict,
)
from app.services.remediation_audit import AuditTrail
from app.services.remediation_clock import aware, is_expired, utcnow
from app.services.remediation_executor import (
    ExecutionOutcome,
    RemediationExecutor,
)
from app.services.remediation_evidence import (
    count_incidents_since,
    error_rate,
    health_snapshot,
)
from app.services.remediation_policy import (
    PolicyEngine,
    PolicyEvaluation,
    environment_name,
    get_breaker,
    is_non_production,
    resolve_policy,
)
from app.services.remediation_registry import (
    get_definition,
    validate_parameters,
)
from app.services.remediation_rollback import RollbackEngine, RollbackResult
from app.services.remediation_safety import (
    SafetyEngine,
    SafetyReport,
    build_rollback_plan,
    build_verification_plan,
)
from app.services.remediation_state import (
    IN_FLIGHT_STATUSES,
    TERMINAL_STATUSES,
    apply_transition,
    is_terminal,
    may_apply_effect,
)
from app.services.remediation_verification import (
    VerificationEngine,
    VerificationResult,
)

logger = logging.getLogger(__name__)
settings = get_settings()


def _created_at(action: RemediationAction) -> datetime:
    """An action's creation time, tolerating a not-yet-refreshed server default."""
    return (
        aware(action.created_at) if action.created_at is not None else aware(utcnow())
    )


@dataclass
class StepResult:
    """A compact record of what one orchestration step did."""

    action_id: uuid.UUID
    status: RemediationStatus
    step: str
    detail: str
    failure_reason: Optional[RemediationFailureReason] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_id": str(self.action_id),
            "status": self.status.value,
            "step": self.step,
            "detail": self.detail,
            "failure_reason": (
                self.failure_reason.value if self.failure_reason else None
            ),
        }


class RemediationService:
    """The gate pipeline for one database session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditTrail(session)

    # -- proposals ----------------------------------------------------------

    async def propose(
        self,
        drafts: Sequence[Any],
        *,
        created_by: str = "planner",
        now: Optional[datetime] = None,
    ) -> list[RemediationAction]:
        """Persist proposal drafts as proposals *and* proposals-as-actions.

        The action is created in ``PROPOSED`` with all three gate verdicts unset.
        Nothing has been checked, and the row says so: an action cannot be
        authorized until every gate has run and recorded a result.
        """
        now = aware(now or utcnow())
        created: list[RemediationAction] = []
        for draft in drafts:
            if await self._is_duplicate(draft, now=now):
                continue
            columns = draft.as_columns()
            proposal = RemediationProposal(**columns)
            self._session.add(proposal)
            await self._session.flush()

            definition = get_definition(draft.action_type)
            action = RemediationAction(
                project_id=draft.project_id,
                environment_id=draft.environment_id,
                component_id=draft.component_id,
                proposal_id=proposal.id,
                action_type=draft.action_type,
                description=draft.recommended_action,
                reason=draft.rationale,
                risk_level=draft.risk_level,
                blast_radius=draft.blast_radius,
                blast_radius_percent=draft.blast_radius_percent,
                affected_resource_count=1,
                source_type=draft.source_type,
                source_id=draft.source_id,
                incident_id=draft.incident_id,
                forecast_id=draft.forecast_id,
                causal_analysis_id=draft.causal_analysis_id,
                root_cause_analysis_id=draft.root_cause_candidate_id,
                fix_id=draft.fix_hypothesis_id,
                patch_id=draft.patch_id,
                parameters=draft.parameters,
                status=RemediationStatus.PROPOSED,
                execution_mode=RemediationExecutionMode.OBSERVE_ONLY,
                adapter_kind=definition.adapter_kind,
                rollback_strategy=definition.rollback_strategy,
                rollback_available=definition.reversible,
                rollback_plan=build_rollback_plan(draft.action_type, draft.parameters),
                verification_plan=build_verification_plan(draft.action_type),
                preconditions=draft.preconditions,
                fingerprint=draft.fingerprint,
                headline=f"{draft.action_type.value}: {draft.recommended_action}"[:400],
                expires_at=now
                + timedelta(seconds=settings.REMEDIATION_ACTION_EXPIRY_SECONDS),
                created_by=created_by,
            )
            self._session.add(action)
            await self._session.flush()
            await self._audit.record(
                action,
                RemediationAuditEventType.ACTION_PROPOSED,
                actor=created_by,
                summary=f"proposed {draft.action_type.value} ({draft.strategy})",
                detail={
                    "strategy": draft.strategy,
                    "risk_level": draft.risk_level.value,
                    "confidence": draft.confidence,
                    "confidence_reason": draft.confidence_reason,
                    "limitations": draft.limitations,
                    "supporting_evidence": draft.supporting_evidence,
                    "blast_radius": draft.blast_radius.value,
                    "source_type": draft.source_type.value,
                },
                to_status=RemediationStatus.PROPOSED,
                now=now,
            )
            created.append(action)
        return created

    async def _is_duplicate(self, draft: Any, *, now: datetime) -> bool:
        """Whether this exact remediation is already in flight or recently proposed."""
        fingerprint = draft.fingerprint
        active = (
            await self._session.execute(
                select(RemediationAction.id)
                .where(RemediationAction.project_id == draft.project_id)
                .where(RemediationAction.fingerprint == fingerprint)
                .where(
                    RemediationAction.status.notin_(
                        [s.value for s in TERMINAL_STATUSES]
                    )
                )
                .limit(1)
            )
        ).first()
        if active is not None:
            return True
        window_start = now - timedelta(
            seconds=settings.REMEDIATION_PROPOSAL_DEDUP_WINDOW_SECONDS
        )
        recent = (
            await self._session.execute(
                select(RemediationProposal.id)
                .where(RemediationProposal.project_id == draft.project_id)
                .where(RemediationProposal.fingerprint == fingerprint)
                .where(RemediationProposal.created_at >= window_start)
                .limit(1)
            )
        ).first()
        return recent is not None

    # -- gate 1: validation + safety ----------------------------------------

    async def assess(
        self,
        action: RemediationAction,
        *,
        now: Optional[datetime] = None,
        actor: str = "safety-engine",
    ) -> SafetyReport:
        """Validate parameters and run the safety assessment."""
        now = aware(now or utcnow())
        if action.status == RemediationStatus.PROPOSED:
            apply_transition(action, RemediationStatus.VALIDATING)
            await self._session.flush()

        definition = get_definition(action.action_type)
        clean, errors = validate_parameters(action.action_type, action.parameters)
        if errors:
            action.failure_reason = RemediationFailureReason.PARAMETER_INVALID
            action.failure_detail = "; ".join(errors)[:2000]
            #: The verdict is recorded on the action even on this early exit:
            #: a blocked action with ``safety_status = None`` would read as
            #: "not assessed yet" rather than "refused", which is exactly the
            #: conflation the API is supposed to prevent.
            action.safety_status = SafetyStatus.FAILED
            apply_transition(action, RemediationStatus.BLOCKED)
            await self._audit.record(
                action,
                RemediationAuditEventType.VALIDATION_FAILED,
                actor=actor,
                summary="parameter validation failed",
                detail={"errors": errors},
                to_status=RemediationStatus.BLOCKED,
                now=now,
            )
            await self._session.flush()
            checks = [
                {"name": "parameters_valid", "result": "FAIL", "detail": e}
                for e in errors
            ]
            report = SafetyReport(
                status=SafetyStatus.FAILED,
                blocking=["parameters_valid"],
                reason="; ".join(errors),
                failure_reason=RemediationFailureReason.PARAMETER_INVALID,
            )
            report.checks = checks
            #: The refusal is persisted like every other gate result. Without
            #: this row the policy engine would later read "never assessed" and
            #: could authorize a remediation whose parameters failed validation.
            self._session.add(
                RemediationAssessment(
                    action_id=action.id,
                    project_id=action.project_id,
                    status=report.status,
                    checks=checks,
                    blocking=report.blocking,
                    warnings=report.warnings,
                    reversible=report.reversible,
                    rollback_plan=report.rollback_plan,
                    blast_radius=report.blast_radius,
                    blast_radius_percent=report.blast_radius_percent,
                    affected_resource_count=report.affected_resource_count,
                    requires_human_approval=report.requires_human_approval,
                    reason=report.reason,
                    assessed_by=actor,
                )
            )
            return report
        # Persist the registry-validated parameters: a handler only ever sees
        # values that passed the schema.
        action.parameters = clean

        env_name = await environment_name(self._session, action.environment_id)
        report = await SafetyEngine(self._session).assess(
            action, environment_name=env_name, now=now
        )
        assessment = RemediationAssessment(
            action_id=action.id,
            project_id=action.project_id,
            status=report.status,
            checks=report.checks,
            blocking=report.blocking,
            warnings=report.warnings,
            reversible=report.reversible,
            rollback_plan=report.rollback_plan,
            blast_radius=report.blast_radius,
            blast_radius_percent=report.blast_radius_percent,
            affected_resource_count=report.affected_resource_count,
            requires_human_approval=report.requires_human_approval,
            reason=report.reason,
            assessed_by=actor,
        )
        self._session.add(assessment)

        action.safety_status = report.status
        action.rollback_available = report.reversible
        action.rollback_plan = report.rollback_plan
        action.verification_plan = report.verification_plan
        action.blast_radius_percent = report.blast_radius_percent
        action.affected_resource_count = report.affected_resource_count

        await self._audit.record(
            action,
            RemediationAuditEventType.SAFETY_ASSESSED,
            actor=actor,
            summary=report.reason[:500],
            detail={
                "status": report.status.value,
                "checks": report.checks,
                "blocking": report.blocking,
                "warnings": report.warnings,
                "reversible": report.reversible,
            },
            now=now,
        )

        if not report.passed:
            action.failure_reason = (
                report.failure_reason or RemediationFailureReason.PRECONDITION_FAILED
            )
            action.failure_detail = report.reason[:2000]
            apply_transition(action, RemediationStatus.BLOCKED)
            await self._audit.record(
                action,
                RemediationAuditEventType.ACTION_BLOCKED,
                actor=actor,
                summary="blocked by the safety assessment",
                detail={"blocking": report.blocking},
                to_status=RemediationStatus.BLOCKED,
                now=now,
            )
        else:
            apply_transition(action, RemediationStatus.POLICY_REVIEW)
        await self._session.flush()
        del definition
        return report

    # -- gate 2: policy ------------------------------------------------------

    async def evaluate_policy(
        self,
        action: RemediationAction,
        *,
        report: Optional[SafetyReport] = None,
        now: Optional[datetime] = None,
        actor: str = "policy-engine",
        human_approved: bool = False,
    ) -> PolicyEvaluation:
        """Resolve the policy, evaluate it, and record the verdict.

        ``human_approved`` is passed only by :meth:`decide` once a named human has
        approved *this* action. It answers the authority question and nothing
        else — see :meth:`PolicyEngine.evaluate`.
        """
        now = aware(now or utcnow())
        if action.status == RemediationStatus.PROPOSED:
            # The gates must be run in order; running policy without safety would
            # let an unassessed action through.
            report = await self.assess(action, now=now)
        if action.status not in (
            RemediationStatus.VALIDATING,
            RemediationStatus.POLICY_REVIEW,
            RemediationStatus.AWAITING_APPROVAL,
            RemediationStatus.BLOCKED,
        ):
            raise ValueError(
                f"cannot evaluate policy for an action in {action.status.value}"
            )

        assessment = await self._latest_assessment(action.id)
        env_name = await environment_name(self._session, action.environment_id)
        non_production = await is_non_production(self._session, action.environment_id)

        evaluation = await PolicyEngine(self._session).evaluate(
            action,
            assessment=assessment,
            environment_name=env_name,
            non_production=non_production,
            human_approved=human_approved,
            now=now,
        )

        decision_row = RemediationPolicyDecision(
            action_id=action.id,
            policy_id=evaluation.policy.policy_id if evaluation.policy else None,
            project_id=action.project_id,
            environment_id=action.environment_id,
            decision=evaluation.decision,
            execution_mode=evaluation.execution_mode,
            policy_revision=evaluation.policy.revision if evaluation.policy else None,
            matched_rules=evaluation.matched_rules,
            reasons=evaluation.reasons,
            failure_reason=evaluation.failure_reason,
            requires_canary=evaluation.requires_canary,
            budget_state=evaluation.budget.as_dict() if evaluation.budget else None,
            circuit_state=(
                {
                    "state": evaluation.breaker.state.value,
                    "consecutive_failures": evaluation.breaker.consecutive_failures,
                    "threshold": evaluation.breaker.threshold,
                }
                if evaluation.breaker is not None
                else None
            ),
            evaluated_by=actor,
        )
        self._session.add(decision_row)

        action.policy_status = evaluation.decision
        action.execution_mode = evaluation.execution_mode
        if evaluation.policy is not None and evaluation.policy.policy_id is not None:
            action.metadata_ = {
                **(action.metadata_ or {}),
                "policy_id": str(evaluation.policy.policy_id),
                "policy_revision": evaluation.policy.revision,
                "policy_source": evaluation.policy.source,
            }

        await self._audit.record(
            action,
            RemediationAuditEventType.POLICY_EVALUATED,
            actor=actor,
            summary=f"policy decision: {evaluation.decision.value}",
            detail={
                "decision": evaluation.decision.value,
                "execution_mode": evaluation.execution_mode.value,
                "matched_rules": evaluation.matched_rules,
                "reasons": evaluation.reasons,
                "budget": evaluation.budget.as_dict() if evaluation.budget else None,
                "requires_canary": evaluation.requires_canary,
            },
            now=now,
        )

        if evaluation.allowed:
            action.canary_required = evaluation.requires_canary
            if evaluation.requires_canary:
                action.canary_stage = CanaryStage.CANARY
                action.canary_percent = (
                    evaluation.policy.canary_percent if evaluation.policy else None
                )
            # A policy ALLOW is an authorization from the policy, recorded as one.
            self._session.add(
                RemediationApproval(
                    action_id=action.id,
                    project_id=action.project_id,
                    status=RemediationApprovalStatus.APPROVED,
                    actor_type=RemediationActorType.AUTONOMOUS_POLICY,
                    actor=actor,
                    decided_at=now,
                    reason=(
                        "autonomous authorization by policy: "
                        + "; ".join(evaluation.reasons)
                    )[:2000],
                    scope_snapshot=self._scope_snapshot(action, report),
                )
            )
            action.authorization_status = RemediationApprovalStatus.APPROVED
            action.approved_at = now
            action.approved_by = actor
            action.authorized_at = now
            action.authorized_by = actor
            apply_transition(action, RemediationStatus.AUTHORIZED)
            await self._audit.record(
                action,
                RemediationAuditEventType.AUTHORIZED,
                actor_type=RemediationActorType.AUTONOMOUS_POLICY,
                actor=actor,
                summary="authorized autonomously by policy",
                detail={"decision": evaluation.decision.value},
                to_status=RemediationStatus.AUTHORIZED,
                now=now,
            )
        elif evaluation.requires_human:
            self._session.add(
                RemediationApproval(
                    action_id=action.id,
                    project_id=action.project_id,
                    status=RemediationApprovalStatus.PENDING,
                    actor_type=RemediationActorType.HUMAN,
                    expires_at=now
                    + timedelta(
                        seconds=(
                            evaluation.policy.approval_ttl_seconds
                            if evaluation.policy
                            else settings.REMEDIATION_APPROVAL_TTL_SECONDS
                        )
                    ),
                    reason="awaiting human approval",
                    scope_snapshot=self._scope_snapshot(action, report),
                )
            )
            action.failure_reason = RemediationFailureReason.APPROVAL_REQUIRED
            action.failure_detail = "; ".join(evaluation.reasons)[:2000]
            apply_transition(action, RemediationStatus.AWAITING_APPROVAL)
            await self._audit.record(
                action,
                RemediationAuditEventType.APPROVAL_REQUESTED,
                actor=actor,
                summary="approval requested",
                detail={"reasons": evaluation.reasons},
                to_status=RemediationStatus.AWAITING_APPROVAL,
                now=now,
            )
        else:
            action.failure_reason = evaluation.failure_reason
            action.failure_detail = "; ".join(evaluation.reasons)[:2000]
            # Transient blockers stay recoverable; a real denial is terminal.
            transient = {
                RemediationFailureReason.CIRCUIT_OPEN,
                RemediationFailureReason.BUDGET_EXHAUSTED,
                RemediationFailureReason.CONCURRENCY_LIMIT,
                RemediationFailureReason.EMERGENCY_STOP,
                RemediationFailureReason.ADAPTER_UNAVAILABLE,
            }
            target = (
                RemediationStatus.BLOCKED
                if evaluation.failure_reason in transient
                else RemediationStatus.REJECTED
            )
            apply_transition(action, target)
            await self._audit.record(
                action,
                (
                    RemediationAuditEventType.POLICY_DENIED
                    if target == RemediationStatus.REJECTED
                    else RemediationAuditEventType.ACTION_BLOCKED
                ),
                actor=actor,
                summary="policy refused the action: "
                + "; ".join(evaluation.reasons)[:500],
                detail={"decision": evaluation.decision.value},
                to_status=target,
                now=now,
            )
        await self._session.flush()
        return evaluation

    async def _latest_assessment(
        self, action_id: uuid.UUID
    ) -> Optional[RemediationAssessment]:
        stmt = (
            select(RemediationAssessment)
            .where(RemediationAssessment.action_id == action_id)
            .order_by(RemediationAssessment.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    def _scope_snapshot(
        self, action: RemediationAction, report: Optional[SafetyReport]
    ) -> dict[str, Any]:
        """What the approver is being asked to approve, frozen at request time."""
        return {
            "action_id": str(action.id),
            "action_type": action.action_type.value,
            "risk_level": action.risk_level.value,
            "blast_radius": action.blast_radius.value,
            "blast_radius_percent": action.blast_radius_percent,
            "affected_resource_count": action.affected_resource_count,
            "parameters": action.parameters,
            "rollback_plan": action.rollback_plan,
            "verification_plan": action.verification_plan,
            "safety_status": action.safety_status.value
            if action.safety_status
            else None,
            "reversible": bool(report.reversible) if report is not None else None,
        }

    # -- gate 3: approval ----------------------------------------------------

    async def pending_approval(
        self, action_id: uuid.UUID
    ) -> Optional[RemediationApproval]:
        stmt = (
            select(RemediationApproval)
            .where(RemediationApproval.action_id == action_id)
            .where(RemediationApproval.status == RemediationApprovalStatus.PENDING)
            .order_by(RemediationApproval.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def decide(
        self,
        action: RemediationAction,
        *,
        approve: bool,
        actor: str,
        reason: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> tuple[RemediationAction, Optional[PolicyEvaluation]]:
        """Record a human decision and, if approving, re-run every gate.

        Re-running the gates on approval is the point: the policy may no longer
        allow the action, the breaker may have opened, or an emergency stop may
        have been engaged while the approver was deciding.
        """
        now = aware(now or utcnow())
        if action.status != RemediationStatus.AWAITING_APPROVAL:
            raise ValueError(
                f"the action is in {action.status.value}, not awaiting approval"
            )
        if not actor or not actor.strip():
            raise ValueError("an approval requires a named human actor")

        approval = await self.pending_approval(action.id)
        if approval is not None:
            if is_expired(approval.expires_at, now=now):
                approval.status = RemediationApprovalStatus.EXPIRED
                action.failure_reason = RemediationFailureReason.APPROVAL_EXPIRED
                action.failure_detail = "the approval request expired"
                apply_transition(action, RemediationStatus.EXPIRED)
                await self._audit.record(
                    action,
                    RemediationAuditEventType.ACTION_EXPIRED,
                    actor="system",
                    summary="the approval request expired before a decision",
                    to_status=RemediationStatus.EXPIRED,
                    now=now,
                )
                await self._session.flush()
                return action, None
            approval.status = (
                RemediationApprovalStatus.APPROVED
                if approve
                else RemediationApprovalStatus.REJECTED
            )
            approval.actor = actor
            approval.actor_type = RemediationActorType.HUMAN
            approval.decided_at = now
            approval.reason = (reason or "")[:2000] or approval.reason

        if not approve:
            action.authorization_status = RemediationApprovalStatus.REJECTED
            action.approved_by = actor
            apply_transition(action, RemediationStatus.REJECTED)
            await self._audit.record(
                action,
                RemediationAuditEventType.REJECTED,
                actor_type=RemediationActorType.HUMAN,
                actor=actor,
                summary=reason or "rejected by a human operator",
                to_status=RemediationStatus.REJECTED,
                now=now,
            )
            await self._session.flush()
            return action, None

        action.approved_at = now
        action.approved_by = actor
        #: The approval is recorded *before* the gates are re-run, and the
        #: re-run is told a human has decided. Everything else about the
        #: evaluation is unchanged: the safety verdict, the breaker, the budget
        #: and the scope rules are re-checked against the world as it is now.
        evaluation = await self.evaluate_policy(
            action, now=now, actor=actor, human_approved=True
        )
        if evaluation.allowed:
            action.authorized_at = now
            action.authorized_by = actor
            action.failure_reason = None
            action.failure_detail = None
            await self._audit.record(
                action,
                RemediationAuditEventType.APPROVED,
                actor_type=RemediationActorType.HUMAN,
                actor=actor,
                summary="approved and authorized by a human operator",
                detail={
                    "reason": reason,
                    "execution_mode": evaluation.execution_mode.value,
                },
                now=now,
            )
        await self._session.flush()
        return action, evaluation

    # -- execution -----------------------------------------------------------

    async def execute(
        self,
        action: RemediationAction,
        *,
        actor: str = "executor",
        now: Optional[datetime] = None,
        force_dry_run: bool = False,
    ) -> ExecutionOutcome:
        """Apply the action's effect (or refuse, with a recorded reason)."""
        now = aware(now or utcnow())
        if not may_apply_effect(action.status):
            raise ValueError(
                f"the action is in {action.status.value}; only an authorized or "
                "scheduled action may be executed"
            )
        await self._audit.record(
            action,
            RemediationAuditEventType.EXECUTION_STARTED,
            actor=actor,
            summary=f"execution attempt {action.attempt} started",
            detail={
                "mode": action.execution_mode.value,
                "adapter_kind": action.adapter_kind.value,
                "parameters": action.parameters,
            },
            now=now,
        )
        if action.status != RemediationStatus.EXECUTING:
            apply_transition(action, RemediationStatus.EXECUTING)
        await self._session.flush()

        policy = await resolve_policy(
            self._session, action.project_id, action.environment_id
        )
        executor = RemediationExecutor(
            self._session,
            execution_timeout_seconds=policy.execution_timeout_seconds,
        )
        outcome = await executor.execute(
            action,
            actor=actor,
            now=now,
            force_dry_run=force_dry_run,
        )

        action.execution_status = outcome.status
        if outcome.execution is not None:
            action.executed_by = actor

        if outcome.status == ExecutionStatus.REFUSED:
            action.failure_reason = (
                outcome.failure_reason or RemediationFailureReason.PRECONDITION_FAILED
            )
            action.failure_detail = outcome.detail[:2000]
            apply_transition(action, RemediationStatus.BLOCKED)
            await self._audit.record(
                action,
                RemediationAuditEventType.EXECUTION_REFUSED,
                actor=actor,
                summary=f"execution refused: {outcome.detail}"[:500],
                detail={
                    "failure_reason": (
                        action.failure_reason.value if action.failure_reason else None
                    )
                },
                to_status=RemediationStatus.BLOCKED,
                now=now,
            )
        elif outcome.status == ExecutionStatus.NOT_PERFORMED:
            # A dry run or a shadow pass: validated, deliberately unapplied.
            apply_transition(action, RemediationStatus.AWAITING_APPROVAL)
            await self._audit.record(
                action,
                RemediationAuditEventType.EXECUTION_REFUSED,
                actor=actor,
                summary=(
                    f"{action.execution_mode.value}: validated without applying an "
                    "effect"
                ),
                detail={"mode": action.execution_mode.value},
                to_status=RemediationStatus.AWAITING_APPROVAL,
                now=now,
            )
        elif outcome.succeeded:
            await self._record_breaker_outcome(action, success=True, reason=None)
            apply_transition(action, RemediationStatus.VERIFYING)
            await self._audit.record(
                action,
                RemediationAuditEventType.EXECUTION_SUCCEEDED,
                actor=actor,
                summary=outcome.detail[:500] or "the effect was applied",
                detail={
                    "steps": outcome.execution.steps if outcome.execution else None
                },
                to_status=RemediationStatus.VERIFYING,
                now=now,
            )
        else:
            await self._record_breaker_outcome(
                action, success=False, reason=outcome.failure_reason
            )
            action.failure_reason = (
                outcome.failure_reason or RemediationFailureReason.HANDLER_ERROR
            )
            action.failure_detail = outcome.detail[:2000]
            await self._audit.record(
                action,
                RemediationAuditEventType.EXECUTION_FAILED,
                actor=actor,
                summary=f"execution failed: {outcome.detail}"[:500],
                to_status=None,
                now=now,
            )
            if outcome.effect_applied:
                # A partial effect is in place; undo it rather than retry onto it.
                apply_transition(action, RemediationStatus.ROLLING_BACK)
            elif action.retry_count < action.max_retries:
                action.retry_count += 1
                action.attempt += 1
                apply_transition(action, RemediationStatus.SCHEDULED)
            else:
                apply_transition(action, RemediationStatus.FAILED)
                action.outcome = RemediationOutcome.INCONCLUSIVE
        await self._session.flush()
        return outcome

    async def _record_breaker_outcome(
        self,
        action: RemediationAction,
        *,
        success: bool,
        reason: Optional[RemediationFailureReason],
    ) -> None:
        """Feed the outcome into the per-scope breaker (§25, §27)."""
        policy = await resolve_policy(
            self._session, action.project_id, action.environment_id
        )
        breaker = await get_breaker(
            self._session,
            action.project_id,
            action.environment_id,
            action.action_type,
            threshold=policy.circuit_failure_threshold,
        )
        now = aware(utcnow())
        breaker.total_attempts += 1
        if success:
            breaker.consecutive_failures = 0
            breaker.total_successes += 1
            breaker.last_success_at = now
            if breaker.state != CircuitState.CLOSED:
                previous = breaker.state
                breaker.state = CircuitState.CLOSED
                breaker.opened_at = None
                breaker.opened_until = None
                await self._audit.record(
                    action,
                    RemediationAuditEventType.CIRCUIT_CLOSED,
                    summary=(
                        f"the {action.action_type.value} breaker closed after a "
                        f"successful {previous.value} probe"
                    ),
                    now=now,
                )
            return

        breaker.consecutive_failures += 1
        breaker.total_failures += 1
        breaker.last_failure_at = now
        if breaker.consecutive_failures >= breaker.threshold and (
            breaker.state == CircuitState.CLOSED
        ):
            breaker.state = CircuitState.OPEN
            breaker.opened_at = now
            breaker.opened_until = now + timedelta(seconds=policy.circuit_reset_seconds)
            breaker.last_trip_reason = reason.value if reason else "repeated failure"
            await self._audit.record(
                action,
                RemediationAuditEventType.CIRCUIT_OPENED,
                summary=(
                    f"the {action.action_type.value} breaker opened after "
                    f"{breaker.consecutive_failures} consecutive failures"
                ),
                detail={"until": breaker.opened_until.isoformat()},
                now=now,
            )

    # -- verification --------------------------------------------------------

    async def verify(
        self,
        action: RemediationAction,
        *,
        execution: Optional[RemediationExecution] = None,
        now: Optional[datetime] = None,
        actor: str = "verifier",
    ) -> VerificationResult:
        """Verify the applied effect and act on the verdict."""
        now = aware(now or utcnow())
        if action.status not in (
            RemediationStatus.VERIFYING,
            RemediationStatus.EXECUTING,
        ):
            raise ValueError(f"cannot verify an action in {action.status.value}")
        policy = await resolve_policy(
            self._session, action.project_id, action.environment_id
        )
        if execution is None:
            execution = await self._latest_successful_execution(action.id)

        await self._audit.record(
            action,
            RemediationAuditEventType.VERIFICATION_STARTED,
            actor=actor,
            summary="verification started",
            detail={"execution_id": str(execution.id) if execution else None},
            now=now,
        )
        engine = VerificationEngine(self._session)
        result = await engine.verify(
            action,
            execution=execution,
            window_seconds=policy.verification_window_seconds,
            grace_seconds=policy.verification_grace_seconds,
            now=now,
        )
        action.execution_status = (
            ExecutionStatus.SUCCEEDED
            if result.verdict != VerificationVerdict.NOT_EXECUTED
            else action.execution_status
        )

        if result.verdict in (
            VerificationVerdict.VERIFIED,
            VerificationVerdict.PARTIALLY_VERIFIED,
        ):
            if action.status != RemediationStatus.VERIFYING:
                apply_transition(action, RemediationStatus.VERIFYING)
            apply_transition(action, RemediationStatus.VERIFIED)
            action.outcome = (
                RemediationOutcome.EFFECTIVE
                if result.verdict == VerificationVerdict.VERIFIED
                else RemediationOutcome.PARTIALLY_EFFECTIVE
            )
            action.post_analysis_status = PostAnalysisStatus.COMPLETED
            action.failure_reason = None
            action.failure_detail = (
                None
                if result.verdict == VerificationVerdict.VERIFIED
                else "; ".join(result.limitations)[:2000]
            )
        elif result.verdict == VerificationVerdict.FAILED:
            action.outcome = RemediationOutcome.HARMFUL
            if action.rollback_available:
                if action.status != RemediationStatus.VERIFYING:
                    apply_transition(action, RemediationStatus.VERIFYING)
                apply_transition(action, RemediationStatus.ROLLING_BACK)
            else:
                if action.status != RemediationStatus.VERIFYING:
                    apply_transition(action, RemediationStatus.VERIFYING)
                apply_transition(action, RemediationStatus.FAILED)
        else:  # INCONCLUSIVE or NOT_EXECUTED
            attempts = await engine.attempts(action.id)
            if attempts >= policy.max_verification_attempts:
                # Bounded: an action that cannot be confirmed does not sit in
                # VERIFYING forever. It escalates to a human, unverified.
                if action.status != RemediationStatus.VERIFYING:
                    apply_transition(action, RemediationStatus.VERIFYING)
                apply_transition(action, RemediationStatus.FAILED)
                action.outcome = RemediationOutcome.INCONCLUSIVE
                action.post_analysis_status = PostAnalysisStatus.INSUFFICIENT_EVIDENCE
                action.failure_detail = (
                    f"verification remained inconclusive after {attempts} attempts: "
                    + "; ".join(result.limitations)[:1500]
                )
            else:
                action.post_analysis_status = PostAnalysisStatus.INSUFFICIENT_EVIDENCE
                action.failure_detail = (
                    "verification was inconclusive; it will be retried: "
                    + "; ".join(result.limitations)[:1500]
                )

        await self._audit.record(
            action,
            RemediationAuditEventType.VERIFICATION_COMPLETED,
            actor=actor,
            summary=f"verification verdict: {result.verdict.value}",
            detail={
                "verdict": result.verdict.value,
                "checks": result.checks,
                "limitations": result.limitations,
                "observation_seconds": result.observation_seconds,
            },
            to_status=action.status,
            now=now,
        )
        await self._session.flush()
        return result

    async def _latest_successful_execution(
        self, action_id: uuid.UUID
    ) -> Optional[RemediationExecution]:
        stmt = (
            select(RemediationExecution)
            .where(RemediationExecution.action_id == action_id)
            .where(RemediationExecution.effect_applied.is_(True))
            .order_by(RemediationExecution.attempt.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    # -- rollback ------------------------------------------------------------

    async def rollback(
        self,
        action: RemediationAction,
        *,
        trigger: RollbackTrigger,
        requested_by: str = "system",
        reason: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> RollbackResult:
        """Reverse the action's effect and record the outcome."""
        now = aware(now or utcnow())
        if action.status not in (
            RemediationStatus.VERIFIED,
            RemediationStatus.FAILED,
            RemediationStatus.ROLLING_BACK,
            RemediationStatus.VERIFYING,
        ):
            raise ValueError(f"cannot roll back an action in {action.status.value}")

        await self._audit.record(
            action,
            RemediationAuditEventType.ROLLBACK_STARTED,
            actor=requested_by,
            summary=f"rollback started ({trigger.value})",
            detail={"reason": reason, "strategy": action.rollback_strategy.value},
            now=now,
        )
        if action.status != RemediationStatus.ROLLING_BACK:
            apply_transition(action, RemediationStatus.ROLLING_BACK)
        await self._session.flush()

        engine = RollbackEngine(self._session)
        result = await engine.rollback(
            action,
            trigger=trigger,
            requested_by=requested_by,
            reason=reason,
            now=now,
        )
        if result.status == RollbackStatus.SUCCEEDED:
            apply_transition(action, RemediationStatus.ROLLED_BACK)
            if action.outcome in (None, RemediationOutcome.PARTIALLY_EFFECTIVE):
                action.outcome = RemediationOutcome.INCONCLUSIVE
            await self._record_breaker_outcome(
                action, success=False, reason=RemediationFailureReason.HANDLER_ERROR
            )
            await self._audit.record(
                action,
                RemediationAuditEventType.ROLLBACK_COMPLETED,
                actor=requested_by,
                summary="the effect was reversed and the reversal was verified",
                detail={
                    "verification_verdict": (
                        result.verification_verdict.value
                        if result.verification_verdict
                        else None
                    ),
                    "steps": result.rollback.steps,
                },
                to_status=RemediationStatus.ROLLED_BACK,
                now=now,
            )
        else:
            if action.status != RemediationStatus.FAILED:
                apply_transition(action, RemediationStatus.FAILED)
            action.failure_detail = result.detail[:2000]
            await self._audit.record(
                action,
                RemediationAuditEventType.ROLLBACK_FAILED,
                actor=requested_by,
                summary=f"rollback did not complete: {result.detail}"[:500],
                detail={"status": result.status.value},
                to_status=action.status,
                now=now,
            )
        await self._session.flush()
        return result

    # -- post-remediation analysis -------------------------------------------

    async def post_analyse(
        self, action: RemediationAction, *, now: Optional[datetime] = None
    ) -> dict[str, Any]:
        """Summarise what actually happened, after the fact (§36, §37).

        Answers four questions, all from stored data: did the observed signals
        improve, did the originating incident close, has this exact remediation
        been tried before, and what remains unproven.
        """
        now = aware(now or utcnow())
        if action.completed_at is None:
            return {}
        completed = aware(action.completed_at)
        window = timedelta(seconds=settings.REMEDIATION_VERIFICATION_WINDOW_SECONDS)
        health_after = await health_snapshot(
            self._session,
            action.project_id,
            component_id=action.component_id,
            environment_id=action.environment_id,
            start=completed,
            end=now,
        )
        health_before = await health_snapshot(
            self._session,
            action.project_id,
            component_id=action.component_id,
            environment_id=action.environment_id,
            start=completed - window,
            end=completed,
        )
        errors_after = await error_rate(
            self._session,
            action.project_id,
            component_id=action.component_id,
            environment_id=action.environment_id,
            start=completed,
            end=now,
        )
        incidents_after = await count_incidents_since(
            self._session,
            action.project_id,
            completed,
            environment_id=action.environment_id,
        )
        prior_successes = int(
            (
                await self._session.execute(
                    select(func.count(RemediationAction.id))
                    .where(RemediationAction.project_id == action.project_id)
                    .where(RemediationAction.fingerprint == action.fingerprint)
                    .where(RemediationAction.id != action.id)
                    .where(RemediationAction.status == RemediationStatus.VERIFIED)
                )
            ).scalar()
            or 0
        )
        prior_failures = int(
            (
                await self._session.execute(
                    select(func.count(RemediationAction.id))
                    .where(RemediationAction.project_id == action.project_id)
                    .where(RemediationAction.fingerprint == action.fingerprint)
                    .where(RemediationAction.id != action.id)
                    .where(
                        RemediationAction.status.in_(
                            [
                                RemediationStatus.FAILED,
                                RemediationStatus.ROLLED_BACK,
                            ]
                        )
                    )
                )
            ).scalar()
            or 0
        )

        incident_closed: Optional[bool] = None
        if action.incident_id is not None:
            incident = await self._session.get(Incident, action.incident_id)
            if incident is not None:
                incident_closed = incident.status in (
                    IncidentStatus.RESOLVED,
                    IncidentStatus.CLOSED,
                )

        limitations: list[str] = []
        if health_after.samples == 0:
            limitations.append(
                "no health checks arrived after the action, so its effect on "
                "component health is unconfirmed"
            )
        if errors_after.samples == 0:
            limitations.append(
                "no error-rate telemetry arrived after the action, so its effect on "
                "errors is unconfirmed"
            )
        if incident_closed is False:
            limitations.append(
                "the originating incident is still open; the action has not (yet) "
                "resolved it"
            )
        if prior_failures and not prior_successes:
            limitations.append(
                "this exact remediation has failed before for this target, which is "
                "evidence against it being the right one"
            )

        analysis = {
            "analysed_at": now.isoformat(),
            "health_before": health_before.as_dict(),
            "health_after": health_after.as_dict(),
            "error_rate_after": errors_after.as_dict(),
            "incidents_after": incidents_after,
            "incident_closed": incident_closed,
            "prior_verified_attempts": prior_successes,
            "prior_failed_attempts": prior_failures,
            "verdict": action.outcome.value if action.outcome else None,
            "limitations": limitations,
            "note": (
                "post-remediation analysis attributes the change to the action only "
                "in the weak sense of temporal proximity; it is not proof that the "
                "action caused the improvement"
            ),
        }
        action.post_analysis = analysis
        action.post_analysis_status = PostAnalysisStatus.COMPLETED
        await self._audit.record(
            action,
            RemediationAuditEventType.POST_ANALYSIS_COMPLETED,
            summary="post-remediation analysis recorded",
            detail={
                "verdict": analysis["verdict"],
                "incident_closed": incident_closed,
                "prior_attempts": prior_successes + prior_failures,
            },
            now=now,
        )
        await self._session.flush()
        return analysis

    # -- orchestration -------------------------------------------------------

    async def run(
        self,
        action: RemediationAction,
        *,
        actor: str = "scheduler",
        now: Optional[datetime] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Drive one action through execution, verification, rollback and analysis.

        This is the single entry point the worker and the API both use, so the
        pipeline cannot diverge between the two paths.
        """
        now = aware(now or utcnow())
        steps: list[dict[str, Any]] = []
        if action.status in (
            RemediationStatus.PROPOSED,
            RemediationStatus.VALIDATING,
        ):
            await self.assess(action, now=now)
            steps.append(
                StepResult(
                    action.id,
                    action.status,
                    "assess",
                    action.failure_detail or "safety assessed",
                    action.failure_reason,
                ).as_dict()
            )
        if action.status == RemediationStatus.POLICY_REVIEW:
            await self.evaluate_policy(action, now=now)
            steps.append(
                StepResult(
                    action.id,
                    action.status,
                    "policy",
                    "policy evaluated",
                    action.failure_reason,
                ).as_dict()
            )
        if action.status in (
            RemediationStatus.AWAITING_APPROVAL,
            RemediationStatus.REJECTED,
            RemediationStatus.BLOCKED,
        ):
            await self._session.flush()
            return {
                "action_id": str(action.id),
                "status": action.status.value,
                "steps": steps,
                "detail": "the action is waiting on a gate it cannot clear on its own",
            }

        outcome = await self.execute(
            action, actor=actor, now=now, force_dry_run=dry_run
        )
        steps.append(
            StepResult(
                action.id,
                action.status,
                "execute",
                outcome.detail or outcome.status.value,
                outcome.failure_reason,
            ).as_dict()
        )

        if action.status == RemediationStatus.SCHEDULED:
            # A bounded retry remains: run it once more, then stop.
            outcome = await self.execute(action, actor=actor, now=now)
            steps.append(
                StepResult(
                    action.id,
                    action.status,
                    "execute_retry",
                    outcome.detail or outcome.status.value,
                    outcome.failure_reason,
                ).as_dict()
            )

        if action.status == RemediationStatus.VERIFYING:
            result = await self.verify(action, now=now)
            steps.append(
                StepResult(
                    action.id,
                    action.status,
                    "verify",
                    f"verdict {result.verdict.value}",
                ).as_dict()
            )

        if action.status == RemediationStatus.ROLLING_BACK:
            rollback = await self.rollback(
                action,
                trigger=(
                    RollbackTrigger.VERIFICATION_FAILED
                    if action.outcome == RemediationOutcome.HARMFUL
                    else RollbackTrigger.EXECUTION_FAILED
                ),
                requested_by=actor,
                reason="automatic rollback after a failed or partial outcome",
                now=now,
            )
            steps.append(
                StepResult(
                    action.id,
                    action.status,
                    "rollback",
                    rollback.detail[:500],
                ).as_dict()
            )

        if action.status in (
            RemediationStatus.VERIFIED,
            RemediationStatus.ROLLED_BACK,
            RemediationStatus.FAILED,
        ):
            await self.post_analyse(action, now=now)

        await self._session.flush()
        return {
            "action_id": str(action.id),
            "status": action.status.value,
            "outcome": action.outcome.value if action.outcome else None,
            "steps": steps,
        }

    # -- lifecycle helpers ---------------------------------------------------

    async def cancel(
        self,
        action: RemediationAction,
        *,
        actor: str,
        reason: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> RemediationAction:
        """Cancel an action that has not started executing."""
        now = aware(now or utcnow())
        if action.status in IN_FLIGHT_STATUSES:
            raise ValueError(
                "an action that is executing or verifying cannot be cancelled here; "
                "roll it back instead"
            )
        if is_terminal(action.status):
            return action
        apply_transition(action, RemediationStatus.CANCELLED)
        await self._audit.record(
            action,
            RemediationAuditEventType.ACTION_CANCELLED,
            actor_type=RemediationActorType.HUMAN,
            actor=actor,
            summary=reason or "cancelled by a human operator",
            to_status=RemediationStatus.CANCELLED,
            now=now,
        )
        await self._session.flush()
        return action

    async def engage_emergency_stop(
        self,
        project_id: uuid.UUID,
        *,
        engage: bool,
        actor: str,
        reason: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> RemediationPolicy:
        """Engage or release the per-project kill switch (§34).

        Engaging also cancels every action that has not started executing, because
        an emergency stop that leaves authorized actions queued is not a stop.
        Releasing never authorizes anything by itself: it merely removes the block,
        and each action must pass its gates again.
        """
        from app.services.remediation_policy import upsert_policy

        now = aware(now or utcnow())
        values = {
            "emergency_stop_active": engage,
            "emergency_stop_reason": reason or ("engaged" if engage else None),
            "emergency_stop_at": now if engage else None,
            "emergency_stop_by": actor if engage else None,
        }
        policy = await upsert_policy(
            self._session, project_id, None, values, updated_by=actor
        )
        event = (
            RemediationAuditEventType.EMERGENCY_STOP_ENGAGED
            if engage
            else RemediationAuditEventType.EMERGENCY_STOP_RELEASED
        )
        await self._audit.record(
            None,
            event,
            project_id=project_id,
            actor_type=RemediationActorType.HUMAN,
            actor=actor,
            summary=(
                f"emergency stop {'engaged' if engage else 'released'}"
                + (f": {reason}" if reason else "")
            ),
            detail={"reason": reason},
            now=now,
        )

        if engage:
            open_actions = list(
                (
                    await self._session.execute(
                        select(RemediationAction)
                        .where(RemediationAction.project_id == project_id)
                        .where(
                            RemediationAction.status.in_(
                                [
                                    RemediationStatus.AUTHORIZED,
                                    RemediationStatus.SCHEDULED,
                                    RemediationStatus.AWAITING_APPROVAL,
                                    RemediationStatus.POLICY_REVIEW,
                                    RemediationStatus.VALIDATING,
                                ]
                            )
                        )
                        .limit(200)
                    )
                )
                .scalars()
                .all()
            )
            for action in open_actions:
                action.failure_reason = RemediationFailureReason.EMERGENCY_STOP
                action.failure_detail = reason or "emergency stop engaged"
                apply_transition(action, RemediationStatus.BLOCKED)
                await self._audit.record(
                    action,
                    RemediationAuditEventType.ACTION_BLOCKED,
                    actor_type=RemediationActorType.HUMAN,
                    actor=actor,
                    summary="blocked by the emergency stop",
                    to_status=RemediationStatus.BLOCKED,
                    now=now,
                )
        await self._session.flush()
        return policy


__all__ = [
    "RemediationService",
    "StepResult",
]
