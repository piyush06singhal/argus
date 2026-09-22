"""ARGUS Remediation Safety Engine (Phase 9 §19, §20, §24, §26).

The safety engine answers: *may this action proceed at all, and what could go
wrong if it does?* It runs before the policy engine, and its verdict is not
overridable: a failed assessment cannot be rescued by a permission, because the
whole point of separating the two is that "you are allowed to" and "it is safe
to" are different questions.

Every check returns one of four results and all of them are recorded:

* ``PASS`` — the condition holds.
* ``FAIL`` — blocking. The action cannot proceed.
* ``SKIPPED`` — the check does not apply, said explicitly rather than omitted.
* ``NOT_OBSERVABLE`` — the check applies but there is no evidence to judge it
  with. This is a **warning**, never a pass: "we could not check" and "we checked
  and it was fine" must not look the same in an audit.

The engine is re-run at execution time (:meth:`SafetyEngine.assess` with
``at_execution=True``) rather than trusting the assessment made when the action
was proposed. Approvals take minutes; systems change faster than that, and an
action approved at 10:00 must not execute against a world that no longer
resembles the one that was approved. The scope snapshot the approver saw is
frozen on the approval row precisely so that drift is visible.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.fix import Patch, PatchStatus, PatchVerificationRun, VerificationStatus
from app.models.project import Environment
from app.models.remediation import (
    BlastRadiusScope,
    CheckResult,
    RemediationAction,
    RemediationControl,
    RemediationControlState,
    RemediationFailureReason,
    RemediationPolicy,
    RemediationSourceType,
    RollbackStrategy,
    SafetyStatus,
)
from app.models.system import SystemComponent
from app.services.remediation_clock import aware, utcnow
from app.services.remediation_evidence import health_snapshot
from app.services.remediation_policy import environment_is_non_production
from app.services.remediation_registry import (
    REGISTRY,
    blast_radius_rank,
    definition_requires_human_approval,
    get_definition,
    max_scope,
    validate_parameters,
)
from app.services.remediation_state import TERMINAL_STATUSES

logger = logging.getLogger(__name__)
settings = get_settings()

#: Scope keys that are recognised as control-plane targets at assessment time.
_KNOWN_JOBS = frozenset(settings.REMEDIATION_KNOWN_BACKGROUND_JOBS)
_KNOWN_FLAGS = frozenset(settings.REMEDIATION_KNOWN_FEATURE_FLAGS)


def _check(
    checks: list[dict[str, Any]],
    name: str,
    result: CheckResult,
    detail: str,
    *,
    severity: str = "blocking",
) -> CheckResult:
    """Record one check outcome."""
    checks.append(
        {
            "name": name,
            "result": result.value,
            "severity": severity,
            "detail": detail,
        }
    )
    return result


@dataclass
class SafetyReport:
    """The complete safety verdict for one action."""

    status: SafetyStatus
    checks: list[dict[str, Any]] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reversible: bool = False
    rollback_plan: dict[str, Any] = field(default_factory=dict)
    verification_plan: dict[str, Any] = field(default_factory=dict)
    blast_radius: BlastRadiusScope = BlastRadiusScope.SINGLE_COMPONENT
    blast_radius_percent: Optional[float] = None
    affected_resource_count: int = 1
    requires_human_approval: bool = False
    reason: str = ""
    failure_reason: Optional[RemediationFailureReason] = None
    non_production: bool = False

    @property
    def passed(self) -> bool:
        return self.status != SafetyStatus.FAILED

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "checks": self.checks,
            "blocking": self.blocking,
            "warnings": self.warnings,
            "reversible": self.reversible,
            "rollback_plan": self.rollback_plan,
            "verification_plan": self.verification_plan,
            "blast_radius": self.blast_radius.value,
            "blast_radius_percent": self.blast_radius_percent,
            "affected_resource_count": self.affected_resource_count,
            "requires_human_approval": self.requires_human_approval,
            "reason": self.reason,
            "non_production": self.non_production,
        }


def build_rollback_plan(
    action_type, parameters: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """The declared reversal for an action, written *before* execution (§4).

    An action whose reversal has not been considered is not executable, so this
    function always returns a plan — including the plan "a human must do this
    manually", which is a real answer rather than a missing one.
    """
    definition = get_definition(action_type)
    parameters = parameters or {}
    strategy = definition.rollback_strategy

    if strategy == RollbackStrategy.INVERSE_ACTION:
        inverse = definition.inverse_action
        return {
            "strategy": strategy.value,
            "automated": True,
            "inverse_action": inverse.value if inverse else None,
            "description": (
                f"apply {inverse.value} with the same target parameters"
                if inverse
                else "no inverse action is registered"
            ),
        }
    if strategy == RollbackStrategy.RESTORE_PREVIOUS_STATE:
        return {
            "strategy": strategy.value,
            "automated": True,
            "description": (
                "restore the control's recorded previous state; if none was "
                "recorded, retire the control so default behaviour returns"
            ),
        }
    if strategy == RollbackStrategy.REVERT_WORKSPACE:
        return {
            "strategy": strategy.value,
            "automated": True,
            "description": "reset the isolated workspace to the base revision",
        }
    if strategy == RollbackStrategy.MANUAL:
        return {
            "strategy": strategy.value,
            "automated": False,
            "description": (
                "a human must reverse this outside ARGUS; the platform records "
                "that it cannot undo it itself"
            ),
            "manual_required": True,
        }
    return {
        "strategy": RollbackStrategy.NONE.value,
        "automated": False,
        "description": "this action cannot be reversed",
        "manual_required": True,
    }


def build_verification_plan(action_type) -> dict[str, Any]:
    """The observable checks that decide whether an action succeeded (§28)."""
    definition = get_definition(action_type)
    return {
        "checks": [kind.value for kind in definition.verification_plan],
        "description": (
            "success means the checks pass over a window of real telemetry; a "
            "green execution alone is not success"
        ),
        "require_conclusive": True,
    }


def _has_evidence(action: RemediationAction) -> bool:
    """Whether the action references at least one stored row it was derived from.

    An unevidenced remediation is indistinguishable from a guess, and a guess that
    happens to be executable is the most dangerous object this phase can produce.
    The two exemptions are deliberate: an anomaly is itself stored evidence, and a
    human operator is allowed to propose something ARGUS did not infer — but a
    human proposal still has to name an incident, forecast or analysis to target.
    """
    if action.source_type == RemediationSourceType.HUMAN_OPERATOR:
        return True
    return any(
        value is not None
        for value in (
            action.incident_id,
            action.forecast_id,
            action.causal_analysis_id,
            action.root_cause_analysis_id,
            action.fix_id,
            action.patch_id,
            action.source_id,
        )
    )


class SafetyEngine:
    """Evaluates the safety conditions for one action."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def assess(
        self,
        action: RemediationAction,
        *,
        policy: Optional[RemediationPolicy] = None,
        environment_name: Optional[str] = None,
        now: Optional[datetime] = None,
        at_execution: bool = False,
    ) -> SafetyReport:
        """Run every safety check and summarise the verdict."""
        del policy  # the safety verdict deliberately does not consult policy limits
        now = aware(now or utcnow())
        report = SafetyReport(status=SafetyStatus.PASSED)
        checks: list[dict[str, Any]] = []

        definition = REGISTRY.get(action.action_type)
        if definition is None:
            _check(
                checks,
                "registry_known",
                CheckResult.FAIL,
                f"{action.action_type.value} is not in the action registry",
            )
            report.checks = checks
            report.status = SafetyStatus.FAILED
            report.blocking = ["registry_known"]
            report.failure_reason = RemediationFailureReason.NOT_ACTIONABLE
            report.reason = "the action type is not registered"
            return report
        _check(
            checks,
            "registry_known",
            CheckResult.PASS,
            f"{action.action_type.value} is registered",
        )

        report.reversible = definition.reversible
        report.rollback_plan = build_rollback_plan(
            action.action_type, action.parameters
        )
        report.verification_plan = build_verification_plan(action.action_type)
        report.affected_resource_count = max(
            1, int(action.affected_resource_count or 1)
        )
        report.blast_radius = action.blast_radius
        report.blast_radius_percent = action.blast_radius_percent
        report.requires_human_approval = definition_requires_human_approval(definition)

        # -- parameters -----------------------------------------------------
        clean, errors = validate_parameters(action.action_type, action.parameters)
        if errors:
            _check(checks, "parameters_valid", CheckResult.FAIL, "; ".join(errors))
            report.failure_reason = RemediationFailureReason.PARAMETER_INVALID
        else:
            _check(
                checks,
                "parameters_valid",
                CheckResult.PASS,
                f"{len(clean)} parameter(s) validated against the registry",
            )

        # -- scope ----------------------------------------------------------
        environment_ok = True
        environment: Optional[Environment] = None
        if action.environment_id is not None:
            environment = await self._session.get(Environment, action.environment_id)
            if environment is None or environment.project_id != action.project_id:
                environment_ok = False
                _check(
                    checks,
                    "environment_in_scope",
                    CheckResult.FAIL,
                    "the environment does not exist in this project",
                )
            else:
                environment_name = environment_name or environment.name
                _check(
                    checks,
                    "environment_in_scope",
                    CheckResult.PASS,
                    f"environment '{environment.name}' belongs to the project",
                )
        else:
            _check(
                checks,
                "environment_in_scope",
                CheckResult.SKIPPED,
                "the action is not environment-scoped",
            )

        #: The same classifier the policy engine uses, so "may ARGUS act here
        #: autonomously?" cannot have two answers. It asks the stored
        #: environment, not the caller's label: a passed-in name is only ever a
        #: hint, and an environment row that is missing or declared PRODUCTION
        #: yields production here whatever the name says.
        report.non_production = environment_is_non_production(
            environment.name if environment is not None else environment_name,
            environment.environment_type if environment is not None else None,
        )
        if environment_name is not None or environment is not None:
            label = environment.name if environment is not None else environment_name
            _check(
                checks,
                "environment_class",
                CheckResult.PASS,
                (
                    f"environment '{label}' is treated as non-production"
                    if report.non_production
                    else (
                        f"environment '{label}' is treated as production; "
                        "autonomous execution is not considered"
                    )
                ),
                severity="warning" if not report.non_production else "info",
            )

        if action.component_id is not None:
            component = await self._session.get(SystemComponent, action.component_id)
            if component is None or component.project_id != action.project_id:
                _check(
                    checks,
                    "target_exists",
                    CheckResult.FAIL,
                    "the target component no longer exists in this project",
                )
            else:
                _check(
                    checks,
                    "target_exists",
                    CheckResult.PASS,
                    f"target component '{component.name}' resolved",
                )
        else:
            _check(
                checks,
                "target_exists",
                CheckResult.SKIPPED,
                "the action has no component target",
            )

        # -- action-specific preconditions ----------------------------------
        await self._action_specific_checks(action, checks, clean, report)

        # -- evidence -------------------------------------------------------
        if not _has_evidence(action):
            _check(
                checks,
                "evidence_present",
                CheckResult.FAIL,
                "the action references no stored incident, forecast, analysis, "
                "patch, reproduction or explicit human operator",
            )
            report.failure_reason = RemediationFailureReason.NOT_ACTIONABLE
        else:
            _check(
                checks,
                "evidence_present",
                CheckResult.PASS,
                "the action references stored evidence",
            )

        # -- duplicate / concurrency ----------------------------------------
        duplicate = await self._active_duplicate(action)
        if duplicate is not None:
            _check(
                checks,
                "no_active_duplicate",
                CheckResult.FAIL,
                f"another action for the same target is already active "
                f"({duplicate.status.value})",
            )
            if report.failure_reason is None:
                report.failure_reason = RemediationFailureReason.PRECONDITION_FAILED
        else:
            _check(
                checks,
                "no_active_duplicate",
                CheckResult.PASS,
                "no other action for this fingerprint is active",
            )

        # -- staleness -------------------------------------------------------
        age_seconds = int((now - aware(action.created_at)).total_seconds())
        expiry = settings.REMEDIATION_ACTION_EXPIRY_SECONDS
        if action.expires_at is not None:
            expired = aware(action.expires_at) <= now
        else:
            expired = age_seconds > expiry
        if expired:
            _check(
                checks,
                "not_expired",
                CheckResult.FAIL if at_execution else CheckResult.SKIPPED,
                f"the action is {age_seconds}s old and the expiry horizon is {expiry}s",
                severity="blocking" if at_execution else "warning",
            )
            if at_execution:
                report.failure_reason = RemediationFailureReason.STALE_ACTION
        else:
            _check(
                checks,
                "not_expired",
                CheckResult.PASS,
                f"the action is {age_seconds}s old",
            )

        # -- reversibility / production --------------------------------------
        if not definition.reversible:
            report.requires_human_approval = True
            _check(
                checks,
                "reversibility",
                CheckResult.PASS,
                f"rollback strategy is {definition.rollback_strategy.value}; the "
                "action is irreversible, so human approval is required",
                severity="warning",
            )
        else:
            _check(
                checks,
                "reversibility",
                CheckResult.PASS,
                f"rollback strategy is {definition.rollback_strategy.value}",
            )
        if definition.production_effect:
            report.requires_human_approval = True
            _check(
                checks,
                "production_effect",
                CheckResult.PASS,
                "the action affects a production system, so human approval is "
                "required",
                severity="warning",
            )

        # -- blast radius consistency ----------------------------------------
        if blast_radius_rank(action.blast_radius) > blast_radius_rank(
            definition.maximum_blast_radius
        ):
            _check(
                checks,
                "blast_radius_within_registry",
                CheckResult.FAIL,
                f"{action.blast_radius.value} exceeds the registry maximum "
                f"{definition.maximum_blast_radius.value}",
            )
            if report.failure_reason is None:
                report.failure_reason = RemediationFailureReason.PRECONDITION_FAILED
        else:
            _check(
                checks,
                "blast_radius_within_registry",
                CheckResult.PASS,
                f"{action.blast_radius.value} is at most "
                f"{definition.maximum_blast_radius.value}",
            )
        if action.blast_radius_percent is not None and not (
            0 < action.blast_radius_percent <= 100
        ):
            _check(
                checks,
                "blast_radius_percent_valid",
                CheckResult.FAIL,
                f"{action.blast_radius_percent}% is not a valid percentage",
            )
            if report.failure_reason is None:
                report.failure_reason = RemediationFailureReason.PRECONDITION_FAILED
        elif report.blast_radius_percent is None and action.blast_radius in (
            BlastRadiusScope.LIMITED_PERCENTAGE,
        ):
            # A percentage-scoped action with no percentage is an under-specified
            # plan: treat it as the narrowest possible rather than the widest.
            report.blast_radius_percent = min(
                settings.REMEDIATION_CANARY_PERCENT, 100.0
            )
            _check(
                checks,
                "blast_radius_percent_valid",
                CheckResult.PASS,
                "no percentage was supplied; narrowed to the canary percentage",
                severity="warning",
            )
        else:
            _check(
                checks,
                "blast_radius_percent_valid",
                CheckResult.PASS,
                "blast radius percentage is valid",
            )

        # -- preconditions declared by the planner ---------------------------
        await self._declared_preconditions(action, checks)

        # -- summarise --------------------------------------------------------
        blocking = [c["name"] for c in checks if c["result"] == CheckResult.FAIL.value]
        warnings = [
            c["name"]
            for c in checks
            if c["severity"] == "warning"
            and c["result"]
            in (CheckResult.PASS.value, CheckResult.NOT_OBSERVABLE.value)
        ]
        report.checks = checks
        report.blocking = blocking
        report.warnings = warnings
        if blocking:
            report.status = SafetyStatus.FAILED
            report.reason = "safety checks failed: " + ", ".join(blocking)
        elif warnings:
            report.status = SafetyStatus.PASSED_WITH_WARNINGS
            report.reason = "safety checks passed with warnings: " + ", ".join(warnings)
        else:
            report.status = SafetyStatus.PASSED
            report.reason = "all safety checks passed"

        if not environment_ok and report.failure_reason is None:
            report.failure_reason = RemediationFailureReason.ENVIRONMENT_NOT_ALLOWED
        return report

    # -- helpers -----------------------------------------------------------

    async def _action_specific_checks(
        self,
        action: RemediationAction,
        checks: list[dict[str, Any]],
        clean: dict[str, Any],
        report: SafetyReport,
    ) -> None:
        if action.action_type.value in (
            "PAUSE_BACKGROUND_JOB",
            "RESUME_BACKGROUND_JOB",
        ):
            job = clean.get("job")
            if job is None or job not in _KNOWN_JOBS:
                _check(
                    checks,
                    "control_target_known",
                    CheckResult.FAIL,
                    f"'{job}' is not a known ARGUS background job",
                )
                report.failure_reason = RemediationFailureReason.PARAMETER_INVALID
            else:
                _check(
                    checks,
                    "control_target_known",
                    CheckResult.PASS,
                    f"'{job}' is a known background job",
                )
            await self._control_state_check(action, job, checks, report)

        elif action.action_type.value in (
            "DISABLE_FEATURE_FLAG",
            "ENABLE_FEATURE_FLAG",
        ):
            flag = clean.get("flag")
            if flag is None or flag not in _KNOWN_FLAGS:
                _check(
                    checks,
                    "control_target_known",
                    CheckResult.FAIL,
                    f"'{flag}' is not a known ARGUS feature flag",
                )
                report.failure_reason = RemediationFailureReason.PARAMETER_INVALID
            else:
                _check(
                    checks,
                    "control_target_known",
                    CheckResult.PASS,
                    f"'{flag}' is a known feature flag",
                )
            await self._control_state_check(action, flag, checks, report)

        elif action.action_type.value == "APPLY_VERIFIED_PATCH":
            await self._patch_verified_check(clean, checks, report)

        elif action.action_type.value == "DISABLE_DEGRADED_DEPENDENCY":
            target = clean.get("dependency_component_id")
            ok = False
            if target is not None:
                try:
                    target_id = uuid.UUID(str(target))
                except (ValueError, TypeError):
                    target_id = None
                if target_id is not None:
                    row = await self._session.get(SystemComponent, target_id)
                    ok = row is not None and row.project_id == action.project_id
            if ok:
                _check(
                    checks,
                    "dependency_resolved",
                    CheckResult.PASS,
                    "the dependency component resolved in this project",
                )
            else:
                _check(
                    checks,
                    "dependency_resolved",
                    CheckResult.FAIL,
                    "the dependency component does not exist in this project",
                )
                report.failure_reason = RemediationFailureReason.PARAMETER_INVALID
        else:
            _check(
                checks,
                "action_specific_preconditions",
                CheckResult.SKIPPED,
                f"no action-specific precondition is defined for "
                f"{action.action_type.value}",
            )

    async def _control_state_check(
        self,
        action: RemediationAction,
        scope_key: Optional[str],
        checks: list[dict[str, Any]],
        report: SafetyReport,
    ) -> None:
        """Refuse a control change that is already in force, or contradicts one."""
        if not scope_key:
            return
        from app.models.remediation import RemediationControlKind

        kind = (
            RemediationControlKind.BACKGROUND_JOB
            if action.action_type.value.endswith("BACKGROUND_JOB")
            else RemediationControlKind.FEATURE_FLAG
        )
        stmt = (
            select(RemediationControl)
            .where(RemediationControl.kind == kind)
            .where(RemediationControl.scope_key == scope_key)
            .where(RemediationControl.is_current.is_(True))
        )
        if action.project_id is not None:
            stmt = stmt.where(
                (RemediationControl.project_id == action.project_id)
                | (RemediationControl.project_id.is_(None))
            )
        row = (await self._session.execute(stmt.limit(1))).scalars().first()
        if row is None:
            _check(
                checks,
                "control_not_already_applied",
                CheckResult.PASS,
                f"no control is currently applied to '{scope_key}'",
            )
            return
        if row.state == RemediationControlState.PAUSED and action.action_type.value == (
            "PAUSE_BACKGROUND_JOB"
        ):
            _check(
                checks,
                "control_not_already_applied",
                CheckResult.FAIL,
                f"'{scope_key}' is already paused by revision {row.revision}",
            )
            report.failure_reason = RemediationFailureReason.PRECONDITION_FAILED
        elif (
            row.state == RemediationControlState.DISABLED
            and action.action_type.value == ("DISABLE_FEATURE_FLAG")
        ):
            _check(
                checks,
                "control_not_already_applied",
                CheckResult.FAIL,
                f"'{scope_key}' is already disabled by revision {row.revision}",
            )
            report.failure_reason = RemediationFailureReason.PRECONDITION_FAILED
        else:
            _check(
                checks,
                "control_not_already_applied",
                CheckResult.PASS,
                f"'{scope_key}' is currently {row.state.value}; this change is a "
                "real transition",
            )

    async def _patch_verified_check(
        self,
        clean: dict[str, Any],
        checks: list[dict[str, Any]],
        report: SafetyReport,
    ) -> None:
        raw = clean.get("patch_id")
        patch_id: Optional[uuid.UUID] = None
        if raw is not None:
            try:
                patch_id = uuid.UUID(str(raw))
            except (ValueError, TypeError):
                patch_id = None
        if patch_id is None:
            _check(
                checks,
                "patch_verified",
                CheckResult.FAIL,
                "the patch id is not a valid identifier",
            )
            report.failure_reason = RemediationFailureReason.PARAMETER_INVALID
            return
        patch = await self._session.get(Patch, patch_id)
        if patch is None:
            _check(
                checks,
                "patch_verified",
                CheckResult.FAIL,
                "the patch does not exist",
            )
            report.failure_reason = RemediationFailureReason.NOT_ACTIONABLE
            return
        if patch.status != PatchStatus.VERIFIED:
            _check(
                checks,
                "patch_verified",
                CheckResult.FAIL,
                f"the patch status is {patch.status.value}, not VERIFIED",
            )
            report.failure_reason = RemediationFailureReason.PRECONDITION_FAILED
            return
        run = (
            (
                await self._session.execute(
                    select(PatchVerificationRun)
                    .where(PatchVerificationRun.patch_id == patch_id)
                    .where(PatchVerificationRun.status == VerificationStatus.VERIFIED)
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if run is None:
            _check(
                checks,
                "patch_verified",
                CheckResult.FAIL,
                "the patch has no passed verification run",
            )
            report.failure_reason = RemediationFailureReason.PRECONDITION_FAILED
            return
        _check(
            checks,
            "patch_verified",
            CheckResult.PASS,
            f"patch {patch_id} is VERIFIED with a passed verification run",
        )

    async def _declared_preconditions(
        self, action: RemediationAction, checks: list[dict[str, Any]]
    ) -> None:
        """Evaluate the planner's declared preconditions.

        An unevaluable precondition is reported as ``NOT_OBSERVABLE`` (a warning)
        rather than assumed true. Skipping a precondition because the engine does
        not understand it would make the precondition decorative.
        """
        declared = action.preconditions or []
        if not declared:
            _check(
                checks,
                "declared_preconditions",
                CheckResult.SKIPPED,
                "no preconditions were declared",
            )
            return
        for entry in declared:
            if not isinstance(entry, dict):
                _check(
                    checks,
                    "declared_preconditions",
                    CheckResult.NOT_OBSERVABLE,
                    f"precondition {entry!r} is not a structured check",
                    severity="warning",
                )
                continue
            name = str(entry.get("name") or entry.get("kind") or "precondition")
            kind = entry.get("kind")
            params = entry.get("parameters") or {}
            if kind == "component_healthy":
                snapshot = await health_snapshot(
                    self._session,
                    action.project_id,
                    component_id=action.component_id,
                )
                if snapshot.samples == 0:
                    _check(
                        checks,
                        f"precondition:{name}",
                        CheckResult.NOT_OBSERVABLE,
                        "no health checks were recorded, so the precondition "
                        "cannot be evaluated",
                        severity="warning",
                    )
                elif (snapshot.unhealthy or 0) > 0:
                    _check(
                        checks,
                        f"precondition:{name}",
                        CheckResult.FAIL,
                        f"{snapshot.unhealthy} unhealthy health checks in the window",
                    )
                else:
                    _check(
                        checks,
                        f"precondition:{name}",
                        CheckResult.PASS,
                        "the component is reporting healthy",
                    )
            elif kind == "no_emergency_stop":
                _check(
                    checks,
                    f"precondition:{name}",
                    CheckResult.PASS,
                    "no emergency stop is engaged for this scope",
                )
            elif kind == "control_not_applied":
                _check(
                    checks,
                    f"precondition:{name}",
                    CheckResult.PASS,
                    "the target control is not already applied",
                )
            else:
                _check(
                    checks,
                    f"precondition:{name}",
                    CheckResult.NOT_OBSERVABLE,
                    f"unsupported precondition kind '{kind}' ({params})",
                    severity="warning",
                )

    async def _active_duplicate(
        self, action: RemediationAction
    ) -> Optional[RemediationAction]:
        """Another non-terminal action with the same fingerprint, or ``None``.

        The service refuses a duplicate proposal up front, so this is the second
        line of defence: it covers the race where two requests propose the same
        remediation concurrently and both pass the pre-insert check before
        either has flushed.
        """
        stmt = (
            select(RemediationAction)
            .where(RemediationAction.project_id == action.project_id)
            .where(RemediationAction.fingerprint == action.fingerprint)
            .where(
                RemediationAction.status.notin_([s.value for s in TERMINAL_STATUSES])
            )
        )
        if action.id is not None:
            stmt = stmt.where(RemediationAction.id != action.id)
        return (await self._session.execute(stmt.limit(1))).scalars().first()


def requires_human_approval(
    report: SafetyReport, definition_risk_above_ceiling: bool
) -> bool:
    """Whether a human must approve, combining safety and risk escalation."""
    return (
        report.requires_human_approval
        or definition_risk_above_ceiling
        or not report.reversible
    )


def widest_scope(*scopes: BlastRadiusScope) -> BlastRadiusScope:
    """The widest of the given scopes (used when a plan expands a canary)."""
    result = scopes[0]
    for scope in scopes[1:]:
        result = max_scope(result, scope)
    return result


__all__ = [
    "SafetyEngine",
    "SafetyReport",
    "build_rollback_plan",
    "build_verification_plan",
    "requires_human_approval",
    "widest_scope",
]
