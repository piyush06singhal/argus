"""ARGUS Remediation Rollback Engine (Phase 9 §32, §33).

Rollback reverses what an action applied — and then **verifies the reversal**,
because "we issued the undo" is exactly the same weak claim as "we issued the
change". A rollback that reports success without observing the post-rollback state
would reintroduce the failure mode this phase exists to eliminate.

What each declared strategy actually means here:

``INVERSE_ACTION`` / ``RESTORE_PREVIOUS_STATE``
    The applied control rows are restored to their recorded previous state. For a
    control the platform itself obeys, that is a real, checkable reversal.
``REVERT_WORKSPACE``
    The workspace was destroyed at the end of the attempt, so the reversal is
    already a fact rather than an operation. Recorded as such.
``MANUAL``
    The platform cannot undo it. Recorded as ``NOT_AVAILABLE`` with instructions,
    and the action is left in a failed state for a human — never marked
    ``ROLLED_BACK``, which would be a lie.
``NONE``
    Irreversible by declaration. Same treatment, stated plainly.

Rollback is idempotent: an active rollback for the action is returned rather than
started twice, so a retried request or a sweeper race cannot reverse the same
effect twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.remediation import (
    AdapterKind,
    CheckResult,
    RemediationAction,
    RemediationActionType,
    RemediationControlKind,
    RemediationControlState,
    RemediationExecution,
    RemediationRollback,
    RollbackStatus,
    RollbackStrategy,
    RollbackTrigger,
    VerificationVerdict,
)
from app.services.remediation_clock import aware, utcnow
from app.services.remediation_controls import _current_row
from app.services.remediation_evidence import error_rate
from app.services.remediation_executor import RemediationExecutor
from app.services.remediation_registry import get_definition
from app.services.remediation_safety import build_rollback_plan

logger = logging.getLogger(__name__)
settings = get_settings()


#: The control state an action leaves in force, keyed for the reversal check.
_APPLIED_CONTROL_STATE: dict[RemediationActionType, RemediationControlState] = {
    RemediationActionType.PAUSE_BACKGROUND_JOB: RemediationControlState.PAUSED,
    RemediationActionType.RESUME_BACKGROUND_JOB: RemediationControlState.RESUMED,
    RemediationActionType.DISABLE_FEATURE_FLAG: RemediationControlState.DISABLED,
    RemediationActionType.ENABLE_FEATURE_FLAG: RemediationControlState.ENABLED,
    RemediationActionType.DISABLE_DEGRADED_DEPENDENCY: RemediationControlState.SUPPRESSED,
}

_CONTROL_KIND: dict[RemediationActionType, RemediationControlKind] = {
    RemediationActionType.PAUSE_BACKGROUND_JOB: RemediationControlKind.BACKGROUND_JOB,
    RemediationActionType.RESUME_BACKGROUND_JOB: RemediationControlKind.BACKGROUND_JOB,
    RemediationActionType.DISABLE_FEATURE_FLAG: RemediationControlKind.FEATURE_FLAG,
    RemediationActionType.ENABLE_FEATURE_FLAG: RemediationControlKind.FEATURE_FLAG,
    RemediationActionType.DISABLE_DEGRADED_DEPENDENCY: RemediationControlKind.DEPENDENCY_SUPPRESSION,
}

_CONTROL_SCOPE_PARAMETER: dict[RemediationActionType, str] = {
    RemediationActionType.PAUSE_BACKGROUND_JOB: "job",
    RemediationActionType.RESUME_BACKGROUND_JOB: "job",
    RemediationActionType.DISABLE_FEATURE_FLAG: "flag",
    RemediationActionType.ENABLE_FEATURE_FLAG: "flag",
    RemediationActionType.DISABLE_DEGRADED_DEPENDENCY: "dependency_component_id",
}

#: Rollback statuses that mean "an attempt is under way".
_ACTIVE_ROLLBACK_STATUSES = (RollbackStatus.PENDING, RollbackStatus.RUNNING)


@dataclass
class RollbackResult:
    """The outcome of one rollback attempt."""

    rollback: RemediationRollback
    status: RollbackStatus
    verification_verdict: Optional[VerificationVerdict] = None
    detail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == RollbackStatus.SUCCEEDED

    @property
    def attempted(self) -> bool:
        """Whether a real reversal operation was performed."""
        return self.status not in (RollbackStatus.NOT_AVAILABLE,)


class RollbackEngine:
    """Reverses an applied remediation and checks that it is reversed."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active_rollback(self, action_id: Any) -> Optional[RemediationRollback]:
        """An in-flight rollback for this action, if there is one."""
        stmt = (
            select(RemediationRollback)
            .where(RemediationRollback.action_id == action_id)
            .where(RemediationRollback.status.in_(_ACTIVE_ROLLBACK_STATUSES))
            .order_by(RemediationRollback.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def rollback(
        self,
        action: RemediationAction,
        *,
        trigger: RollbackTrigger,
        requested_by: str = "system",
        reason: Optional[str] = None,
        execution: Optional[RemediationExecution] = None,
        now: Optional[datetime] = None,
        verify: bool = True,
    ) -> RollbackResult:
        """Reverse the action's effect and verify the reversal."""
        now = aware(now or utcnow())
        existing = await self.active_rollback(action.id)
        if existing is not None:
            return RollbackResult(
                rollback=existing,
                status=existing.status,
                verification_verdict=existing.verification_verdict,
                detail="a rollback for this action is already in progress",
            )

        definition = get_definition(action.action_type)
        plan = build_rollback_plan(action.action_type, action.parameters)
        row = RemediationRollback(
            action_id=action.id,
            project_id=action.project_id,
            trigger=trigger,
            strategy=definition.rollback_strategy,
            status=RollbackStatus.PENDING,
            plan=plan,
            requested_by=requested_by,
            started_at=now,
        )
        self._session.add(row)
        await self._session.flush()

        if execution is None:
            execution = await self._latest_applied_execution(action.id)

        if definition.rollback_strategy in (
            RollbackStrategy.NONE,
            RollbackStrategy.MANUAL,
        ):
            row.status = RollbackStatus.NOT_AVAILABLE
            row.completed_at = aware(utcnow())
            row.steps = [
                {
                    "step": "not_available",
                    "detail": plan.get("description", "this action cannot be reversed"),
                }
            ]
            row.error = (
                "the platform cannot reverse this action; a human must do it and "
                "record the outcome"
                if definition.rollback_strategy == RollbackStrategy.MANUAL
                else "this action is irreversible by declaration"
            )
            return RollbackResult(
                rollback=row,
                status=RollbackStatus.NOT_AVAILABLE,
                detail=row.error,
            )

        row.status = RollbackStatus.RUNNING
        await self._session.flush()

        adapter_kind = definition.adapter_kind
        if adapter_kind == AdapterKind.WORKSPACE:
            # The workspace was destroyed with the attempt, so the reversal is
            # already a fact; saying so is more honest than "reverting" nothing.
            row.status = RollbackStatus.SUCCEEDED
            row.steps = [
                {
                    "step": "workspace_destroyed",
                    "detail": (
                        "the isolated workspace was destroyed at the end of the "
                        "attempt, so no live tree holds the change"
                    ),
                }
            ]
            row.completed_at = aware(utcnow())
            if verify:
                row.verification_verdict = VerificationVerdict.VERIFIED
            return RollbackResult(
                rollback=row,
                status=RollbackStatus.SUCCEEDED,
                verification_verdict=row.verification_verdict,
                detail="the workspace holding the change no longer exists",
            )

        executor = RemediationExecutor(self._session)
        try:
            reverted = await executor.reverts_for(action, execution)
        except Exception as error:  # noqa: BLE001 - reported, never raised
            row.status = RollbackStatus.FAILED
            row.failure_reason = None
            row.error = f"{type(error).__name__}: {error}"[:2000]
            row.completed_at = aware(utcnow())
            row.steps = [
                {"step": "revert_failed", "detail": row.error},
            ]
            return RollbackResult(
                rollback=row,
                status=RollbackStatus.FAILED,
                detail=row.error,
            )

        row.steps = reverted.steps
        row.controls_reverted = reverted.control_ids
        if reverted.status.value != "SUCCEEDED" or not reverted.effect_applied:
            row.status = RollbackStatus.FAILED
            row.failure_reason = reverted.failure_reason
            row.error = (reverted.error or reverted.output_summary)[:2000]
            row.completed_at = aware(utcnow())
            return RollbackResult(
                rollback=row,
                status=RollbackStatus.FAILED,
                detail=row.error or "the reversal could not be applied",
            )

        row.status = RollbackStatus.SUCCEEDED
        row.completed_at = aware(utcnow())
        if verify:
            verdict, checks = await self._verify_reversal(
                action, applied_at=now, strategy=definition.rollback_strategy
            )
            row.verification_verdict = verdict
            row.metadata_ = {"reversal_checks": checks}
        return RollbackResult(
            rollback=row,
            status=RollbackStatus.SUCCEEDED,
            verification_verdict=row.verification_verdict,
            detail=reverted.output_summary or "the effect was reversed",
        )

    # -- reversal verification ----------------------------------------------

    async def _verify_reversal(
        self,
        action: RemediationAction,
        *,
        applied_at: datetime,
        strategy: RollbackStrategy,
    ) -> tuple[VerificationVerdict, list[dict[str, Any]]]:
        """Confirm the reversal from the platform's own state and telemetry.

        Two checks, and the first is decisive: the control the action applied must
        no longer be in force. The second is corroboration — the error rate after
        the reversal must not be worse than before the action ran.
        """
        checks: list[dict[str, Any]] = []
        kind = _CONTROL_KIND.get(action.action_type)
        parameter = _CONTROL_SCOPE_PARAMETER.get(action.action_type)
        applied_state = _APPLIED_CONTROL_STATE.get(action.action_type)

        if kind is None or parameter is None or applied_state is None:
            return (
                VerificationVerdict.NOT_EXECUTED,
                [
                    {
                        "check": "control_reverted",
                        "result": CheckResult.SKIPPED.value,
                        "detail": "this action did not apply a control",
                    }
                ],
            )

        scope_key = (action.parameters or {}).get(parameter)
        row = None
        if scope_key:
            row = await _current_row(
                self._session,
                kind,
                str(scope_key),
                project_id=action.project_id,
                environment_id=action.environment_id,
            )
        still_applied = row is not None and row.state == applied_state

        if still_applied:
            checks.append(
                {
                    "check": "control_reverted",
                    "result": CheckResult.FAIL.value,
                    "detail": (
                        f"'{scope_key}' is still {applied_state.value} after the "
                        "rollback"
                    ),
                }
            )
            return VerificationVerdict.FAILED, checks

        checks.append(
            {
                "check": "control_reverted",
                "result": CheckResult.PASS.value,
                "detail": (
                    f"'{scope_key}' no longer holds {applied_state.value}"
                    + (
                        f" (now {row.state.value})"
                        if row is not None
                        else " (no control is applied)"
                    )
                ),
            }
        )

        window = max(60, settings.REMEDIATION_VERIFICATION_WINDOW_SECONDS)
        before = await error_rate(
            self._session,
            action.project_id,
            component_id=action.component_id,
            environment_id=action.environment_id,
            start=applied_at - timedelta(seconds=window),
            end=applied_at,
        )
        after = await error_rate(
            self._session,
            action.project_id,
            component_id=action.component_id,
            environment_id=action.environment_id,
            start=applied_at
            + timedelta(seconds=settings.REMEDIATION_VERIFICATION_GRACE_SECONDS),
            end=aware(utcnow()),
        )
        if after.samples == 0:
            checks.append(
                {
                    "check": "error_rate_after_reversal",
                    "result": CheckResult.NOT_OBSERVABLE.value,
                    "detail": "no error-rate telemetry was recorded after the rollback",
                }
            )
            return VerificationVerdict.PARTIALLY_VERIFIED, checks

        observed = float(after.observed or 0.0)
        baseline = before.observed
        if baseline is not None and observed > float(baseline) * (
            1.0 + settings.REMEDIATION_ERROR_RATE_TOLERANCE
        ):
            checks.append(
                {
                    "check": "error_rate_after_reversal",
                    "result": CheckResult.FAIL.value,
                    "detail": (
                        f"the error rate is {observed:.4f} after the rollback, worse "
                        f"than the {float(baseline):.4f} before the action"
                    ),
                }
            )
            return VerificationVerdict.FAILED, checks

        checks.append(
            {
                "check": "error_rate_after_reversal",
                "result": CheckResult.PASS.value,
                "detail": (
                    f"the error rate is {observed:.4f} after the rollback"
                    + (
                        f" against {float(baseline):.4f} before the action"
                        if baseline is not None
                        else "; no baseline existed"
                    )
                ),
            }
        )
        return VerificationVerdict.VERIFIED, checks

    async def _latest_applied_execution(
        self, action_id: Any
    ) -> Optional[RemediationExecution]:
        """The newest attempt that actually applied an effect."""
        stmt = (
            select(RemediationExecution)
            .where(RemediationExecution.action_id == action_id)
            .where(RemediationExecution.effect_applied.is_(True))
            .order_by(RemediationExecution.attempt.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def history(self, action_id: Any) -> list[RemediationRollback]:
        """Every rollback attempt for an action, oldest first."""
        stmt = (
            select(RemediationRollback)
            .where(RemediationRollback.action_id == action_id)
            .order_by(RemediationRollback.created_at)
        )
        return list((await self._session.execute(stmt)).scalars().all())


__all__ = [
    "RollbackEngine",
    "RollbackResult",
]
