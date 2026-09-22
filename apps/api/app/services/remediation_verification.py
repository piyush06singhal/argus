"""ARGUS Remediation Verification Engine (Phase 9 §28–§31).

Verification is where a remediation stops being a claim and becomes a fact — or
fails to. The rule the whole module exists to enforce:

    an action is not successful because the command completed.

Success means the action ran **and** the system's observed behaviour afterwards
matches what was expected, over a window of real telemetry. So every check here
compares a *post* observation against a *pre* observation of the same signal, and
the verdict is computed from check results rather than from the execution's status.

Check semantics, and why each one is the shape it is:

* **CONTROL_STATE** — read back the control the action applied. This is the
  decisive check for a control-plane action: it is deterministic, so it can
  actually be conclusive.
* **HEALTH_STATUS / ERROR_RATE / LATENCY** — post-versus-pre comparison with
  explicit tolerances. "Not worse" is the claim ARGUS makes, so "not worse" is
  what it tests.
* **NEW_ANOMALIES / NEW_INCIDENTS** — a remediation that makes things worse shows
  up here first.
* **TEST_RESULT / WORKSPACE_DIFF** — for a patch validated in a workspace; taken
  from the execution's own recorded evidence.
* **INSTANCE_COUNT** — genuinely not observable from ARGUS's telemetry, so it
  reports ``NOT_OBSERVABLE`` instead of inventing a pass.

Verdicts (§30):

* any ``FAIL`` → ``FAILED``;
* the action's **primary** check passing with every other check conclusive →
  ``VERIFIED``;
* the primary check passing while secondary checks are unobservable →
  ``PARTIALLY_VERIFIED``, with the gaps listed as limitations;
* the primary check unobservable → ``INCONCLUSIVE``, never a pass.

And ``NOT_EXECUTED`` when no effect was applied at all, so a dry run can never be
scored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.remediation import (
    CheckResult,
    RemediationAction,
    RemediationActionType,
    RemediationControlKind,
    RemediationControlState,
    RemediationExecution,
    RemediationVerification,
    VerificationCheckKind,
    VerificationVerdict,
)
from app.services.remediation_clock import aware, utcnow
from app.services.remediation_controls import _current_row
from app.services.remediation_evidence import (
    count_anomalies_since,
    count_incidents_since,
    control_effect_observation,
    error_rate,
    health_snapshot,
    latency_snapshot,
)
from app.services.remediation_registry import get_definition

logger = logging.getLogger(__name__)
settings = get_settings()


#: The state each control-plane action should leave in force.
_EXPECTED_CONTROL_STATE: dict[RemediationActionType, RemediationControlState] = {
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

#: The check whose result carries the action's actual claim. Everything else is
#: corroboration; this is the one that must be conclusive for VERIFIED.
_PRIMARY_CHECK: dict[RemediationActionType, VerificationCheckKind] = {
    RemediationActionType.PAUSE_BACKGROUND_JOB: VerificationCheckKind.CONTROL_STATE,
    RemediationActionType.RESUME_BACKGROUND_JOB: VerificationCheckKind.CONTROL_STATE,
    RemediationActionType.DISABLE_FEATURE_FLAG: VerificationCheckKind.CONTROL_STATE,
    RemediationActionType.ENABLE_FEATURE_FLAG: VerificationCheckKind.CONTROL_STATE,
    RemediationActionType.DISABLE_DEGRADED_DEPENDENCY: VerificationCheckKind.CONTROL_STATE,
    RemediationActionType.APPLY_VERIFIED_PATCH: VerificationCheckKind.WORKSPACE_DIFF,
}


def primary_check(action_type: RemediationActionType) -> VerificationCheckKind:
    """The decisive check for an action type (first in the registry by default)."""
    if action_type in _PRIMARY_CHECK:
        return _PRIMARY_CHECK[action_type]
    definition = get_definition(action_type)
    return definition.verification_plan[0]


@dataclass
class VerificationResult:
    """The complete outcome of one verification pass."""

    verdict: VerificationVerdict
    checks: list[dict[str, Any]] = field(default_factory=list)
    passed_count: int = 0
    failed_count: int = 0
    not_observable_count: int = 0
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    observation_seconds: int = 0
    summary: str = ""
    limitations: list[str] = field(default_factory=list)

    @property
    def conclusive(self) -> bool:
        return self.verdict in (
            VerificationVerdict.VERIFIED,
            VerificationVerdict.PARTIALLY_VERIFIED,
            VerificationVerdict.FAILED,
        )

    @property
    def confirms_success(self) -> bool:
        """Whether the action's claim is confirmed (fully or in part)."""
        return self.verdict in (
            VerificationVerdict.VERIFIED,
            VerificationVerdict.PARTIALLY_VERIFIED,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "checks": self.checks,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "not_observable_count": self.not_observable_count,
            "window_start": (
                self.window_start.isoformat() if self.window_start else None
            ),
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "observation_seconds": self.observation_seconds,
            "summary": self.summary,
            "limitations": self.limitations,
        }


class VerificationEngine:
    """Computes the §28 verdict from observable evidence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def verify(
        self,
        action: RemediationAction,
        *,
        execution: Optional[RemediationExecution],
        window_seconds: Optional[int] = None,
        grace_seconds: Optional[int] = None,
        now: Optional[datetime] = None,
        persist: bool = True,
    ) -> VerificationResult:
        """Verify the action's effect over a window of stored telemetry."""
        now = aware(now or utcnow())
        window_seconds = (
            window_seconds or settings.REMEDIATION_VERIFICATION_WINDOW_SECONDS
        )
        grace_seconds = (
            grace_seconds
            if grace_seconds is not None
            else settings.REMEDIATION_VERIFICATION_GRACE_SECONDS
        )

        if execution is None or not execution.effect_applied:
            result = VerificationResult(
                verdict=VerificationVerdict.NOT_EXECUTED,
                window_start=now,
                window_end=now,
                summary=(
                    "no effect was applied, so there is nothing to verify; a dry "
                    "run is never scored as a success"
                ),
                limitations=["no live effect was applied"],
            )
            if persist:
                await self._persist(action, execution, result)
            return result

        applied_at = aware(execution.completed_at or execution.started_at or now)
        # The observation window starts after a grace period so the system has a
        # chance to react; measuring the instant after the change measures noise.
        obs_start = applied_at + timedelta(seconds=max(0, grace_seconds))
        obs_end = now if now > obs_start else obs_start
        baseline_start = applied_at - timedelta(seconds=window_seconds)

        definition = get_definition(action.action_type)
        checks: list[dict[str, Any]] = []
        for kind in definition.verification_plan:
            try:
                checks.append(
                    await self._run_check(
                        action,
                        execution,
                        kind,
                        baseline_start=baseline_start,
                        baseline_end=applied_at,
                        obs_start=obs_start,
                        obs_end=obs_end,
                    )
                )
            except Exception as error:  # noqa: BLE001 - a failing check is data
                checks.append(
                    {
                        "check": kind.value,
                        "result": CheckResult.NOT_OBSERVABLE.value,
                        "detail": f"the check could not be evaluated: {error}"[:500],
                        "observed": None,
                        "baseline": None,
                    }
                )

        result = self._verdict(action, checks, applied_at=applied_at, obs_end=obs_end)
        result.window_start = obs_start
        result.window_end = obs_end
        result.observation_seconds = max(0, int((obs_end - obs_start).total_seconds()))
        if persist:
            await self._persist(action, execution, result)
        return result

    # -- individual checks ---------------------------------------------------

    async def _run_check(
        self,
        action: RemediationAction,
        execution: RemediationExecution,
        kind: VerificationCheckKind,
        *,
        baseline_start: datetime,
        baseline_end: datetime,
        obs_start: datetime,
        obs_end: datetime,
    ) -> dict[str, Any]:
        if kind == VerificationCheckKind.CONTROL_STATE:
            return await self._check_control_state(action)
        if kind == VerificationCheckKind.CONTROL_EFFECT:
            return await self._check_control_effect(
                action, execution, obs_start=obs_start, obs_end=obs_end
            )
        if kind == VerificationCheckKind.HEALTH_STATUS:
            return await self._check_health(
                action, baseline_start, baseline_end, obs_start, obs_end
            )
        if kind == VerificationCheckKind.ERROR_RATE:
            return await self._check_error_rate(
                action, baseline_start, baseline_end, obs_start, obs_end
            )
        if kind == VerificationCheckKind.LATENCY:
            return await self._check_latency(
                action, baseline_start, baseline_end, obs_start, obs_end
            )
        if kind == VerificationCheckKind.NEW_ANOMALIES:
            return await self._check_new_anomalies(action, baseline_start, obs_start)
        if kind == VerificationCheckKind.NEW_INCIDENTS:
            return await self._check_new_incidents(action, baseline_start, obs_start)
        if kind == VerificationCheckKind.INSTANCE_COUNT:
            return {
                "check": kind.value,
                "result": CheckResult.NOT_OBSERVABLE.value,
                "detail": (
                    "ARGUS stores no instance inventory, so an instance count "
                    "cannot be observed from its telemetry"
                ),
                "observed": None,
                "baseline": None,
            }
        if kind == VerificationCheckKind.TEST_RESULT:
            return self._check_test_result(execution)
        if kind == VerificationCheckKind.WORKSPACE_DIFF:
            return self._check_workspace_diff(execution)
        return {
            "check": kind.value,
            "result": CheckResult.NOT_OBSERVABLE.value,
            "detail": f"no implementation exists for the {kind.value} check",
            "observed": None,
            "baseline": None,
        }

    async def _check_control_state(self, action: RemediationAction) -> dict[str, Any]:
        """Read back the applied control — the decisive control-plane check."""
        kind = _CONTROL_KIND.get(action.action_type)
        parameter = _CONTROL_SCOPE_PARAMETER.get(action.action_type)
        if kind is None or parameter is None:
            return {
                "check": VerificationCheckKind.CONTROL_STATE.value,
                "result": CheckResult.NOT_OBSERVABLE.value,
                "detail": "this action does not change a control",
                "observed": None,
                "baseline": None,
            }
        scope_key = (action.parameters or {}).get(parameter)
        if not scope_key:
            return {
                "check": VerificationCheckKind.CONTROL_STATE.value,
                "result": CheckResult.FAIL.value,
                "detail": f"the action carries no '{parameter}' to read back",
                "observed": None,
                "baseline": None,
            }
        row = await _current_row(
            self._session,
            kind,
            str(scope_key),
            project_id=action.project_id,
            environment_id=action.environment_id,
        )
        expected = _EXPECTED_CONTROL_STATE[action.action_type]
        if row is None:
            # For an inverse action (resume/enable) the absence of a control row
            # *is* the desired end state: the platform's default behaviour.
            if action.action_type in (
                RemediationActionType.RESUME_BACKGROUND_JOB,
                RemediationActionType.ENABLE_FEATURE_FLAG,
            ):
                return {
                    "check": VerificationCheckKind.CONTROL_STATE.value,
                    "result": CheckResult.PASS.value,
                    "detail": (
                        f"'{scope_key}' has no control applied, which is the "
                        "platform's default behaviour"
                    ),
                    "observed": RemediationControlState.RESUMED.value,
                    "baseline": None,
                }
            return {
                "check": VerificationCheckKind.CONTROL_STATE.value,
                "result": CheckResult.FAIL.value,
                "detail": f"no current control row exists for '{scope_key}'",
                "observed": None,
                "baseline": expected.value,
            }
        if row.state == expected or (
            action.action_type == RemediationActionType.RESUME_BACKGROUND_JOB
            and row.state
            in (RemediationControlState.RESUMED, RemediationControlState.ENABLED)
        ):
            return {
                "check": VerificationCheckKind.CONTROL_STATE.value,
                "result": CheckResult.PASS.value,
                "detail": f"'{scope_key}' is {row.state.value} as intended",
                "observed": row.state.value,
                "baseline": expected.value,
            }
        return {
            "check": VerificationCheckKind.CONTROL_STATE.value,
            "result": CheckResult.FAIL.value,
            "detail": (
                f"'{scope_key}' is {row.state.value} but {expected.value} was "
                "intended"
            ),
            "observed": row.state.value,
            "baseline": expected.value,
        }

    async def _check_control_effect(
        self,
        action: RemediationAction,
        execution: RemediationExecution,
        *,
        obs_start: datetime,
        obs_end: datetime,
    ) -> dict[str, Any]:
        """Did the gated thing actually stop (or restart) producing output?

        This is the check that distinguishes *the switch is off* from *the machine
        is quiet*. For a suppression it demands silence; for a resumption it
        demands output, and admits when none was due rather than inventing
        confirmation.
        """
        kind = _CONTROL_KIND.get(action.action_type)
        parameter = _CONTROL_SCOPE_PARAMETER.get(action.action_type)
        if kind is None or parameter is None:
            return {
                "check": VerificationCheckKind.CONTROL_EFFECT.value,
                "result": CheckResult.SKIPPED.value,
                "detail": "this action does not change a control",
                "observed": None,
                "baseline": None,
            }
        scope_key = (action.parameters or {}).get(parameter)
        if not scope_key:
            return {
                "check": VerificationCheckKind.CONTROL_EFFECT.value,
                "result": CheckResult.NOT_OBSERVABLE.value,
                "detail": "the action carries no control key to probe",
                "observed": None,
                "baseline": None,
            }
        applied_at = aware(execution.completed_at or execution.started_at or obs_start)
        observation = await control_effect_observation(
            self._session,
            kind=kind,
            scope_key=str(scope_key),
            project_id=action.project_id,
            since=applied_at,
        )
        if observation.result == CheckResult.NOT_OBSERVABLE:
            return {
                "check": VerificationCheckKind.CONTROL_EFFECT.value,
                "result": CheckResult.NOT_OBSERVABLE.value,
                "detail": observation.detail,
                "observed": None,
                "baseline": None,
            }
        observed = float(observation.observed or 0.0)
        suppressive = action.action_type in (
            RemediationActionType.PAUSE_BACKGROUND_JOB,
            RemediationActionType.DISABLE_FEATURE_FLAG,
            RemediationActionType.DISABLE_DEGRADED_DEPENDENCY,
        )
        if suppressive:
            if observed == 0:
                return {
                    "check": VerificationCheckKind.CONTROL_EFFECT.value,
                    "result": CheckResult.PASS.value,
                    "detail": (
                        "the gated work produced no further output after the "
                        "action; " + observation.detail
                    ),
                    "observed": 0.0,
                    "baseline": None,
                }
            grace_elapsed = (
                aware(utcnow()) - applied_at
            ).total_seconds() >= settings.REMEDIATION_VERIFICATION_GRACE_SECONDS
            if not grace_elapsed:
                return {
                    "check": VerificationCheckKind.CONTROL_EFFECT.value,
                    "result": CheckResult.NOT_OBSERVABLE.value,
                    "detail": (
                        "output is still arriving inside the grace period, which is "
                        "expected while in-flight work drains"
                    ),
                    "observed": observed,
                    "baseline": None,
                }
            return {
                "check": VerificationCheckKind.CONTROL_EFFECT.value,
                "result": CheckResult.FAIL.value,
                "detail": (
                    "the gated work kept producing output after the action: "
                    + observation.detail
                ),
                "observed": observed,
                "baseline": None,
            }
        # Resumption: output is expected, but none being due is not a failure.
        if observed > 0:
            return {
                "check": VerificationCheckKind.CONTROL_EFFECT.value,
                "result": CheckResult.PASS.value,
                "detail": "the resumed work produced output: " + observation.detail,
                "observed": observed,
                "baseline": None,
            }
        return {
            "check": VerificationCheckKind.CONTROL_EFFECT.value,
            "result": CheckResult.NOT_OBSERVABLE.value,
            "detail": (
                "no output was due in the window, so resumption cannot be "
                "confirmed from artefacts"
            ),
            "observed": 0.0,
            "baseline": None,
        }

    async def _check_health(
        self,
        action: RemediationAction,
        baseline_start: datetime,
        baseline_end: datetime,
        obs_start: datetime,
        obs_end: datetime,
    ) -> dict[str, Any]:
        before = await health_snapshot(
            self._session,
            action.project_id,
            component_id=action.component_id,
            environment_id=action.environment_id,
            start=baseline_start,
            end=baseline_end,
        )
        after = await health_snapshot(
            self._session,
            action.project_id,
            component_id=action.component_id,
            environment_id=action.environment_id,
            start=obs_start,
            end=obs_end,
        )
        if after.samples == 0:
            return {
                "check": VerificationCheckKind.HEALTH_STATUS.value,
                "result": CheckResult.NOT_OBSERVABLE.value,
                "detail": "no health checks arrived in the observation window",
                "observed": None,
                "baseline": before.unhealthy_ratio,
            }
        if after.unhealthy > 0:
            return {
                "check": VerificationCheckKind.HEALTH_STATUS.value,
                "result": CheckResult.FAIL.value,
                "detail": (
                    f"{after.unhealthy} unhealthy health checks after the action "
                    f"(latest {after.latest_status.value if after.latest_status else 'unknown'})"
                ),
                "observed": after.unhealthy_ratio,
                "baseline": before.unhealthy_ratio,
            }
        if after.degraded > 0:
            return {
                "check": VerificationCheckKind.HEALTH_STATUS.value,
                "result": CheckResult.PASS.value,
                "detail": f"{after.degraded} degraded but no unhealthy checks",
                "observed": after.unhealthy_ratio,
                "baseline": before.unhealthy_ratio,
            }
        return {
            "check": VerificationCheckKind.HEALTH_STATUS.value,
            "result": CheckResult.PASS.value,
            "detail": f"{after.total} healthy health checks after the action",
            "observed": 0.0,
            "baseline": before.unhealthy_ratio,
        }

    async def _check_error_rate(
        self,
        action: RemediationAction,
        baseline_start: datetime,
        baseline_end: datetime,
        obs_start: datetime,
        obs_end: datetime,
    ) -> dict[str, Any]:
        before = await error_rate(
            self._session,
            action.project_id,
            component_id=action.component_id,
            environment_id=action.environment_id,
            start=baseline_start,
            end=baseline_end,
        )
        after = await error_rate(
            self._session,
            action.project_id,
            component_id=action.component_id,
            environment_id=action.environment_id,
            start=obs_start,
            end=obs_end,
        )
        if after.result == CheckResult.NOT_OBSERVABLE or after.samples == 0:
            return {
                "check": VerificationCheckKind.ERROR_RATE.value,
                "result": CheckResult.NOT_OBSERVABLE.value,
                "detail": after.detail or "no error-rate telemetry in the window",
                "observed": None,
                "baseline": before.observed,
            }
        tolerance = settings.REMEDIATION_ERROR_RATE_TOLERANCE
        observed = float(after.observed or 0.0)
        baseline = before.observed
        if baseline is None:
            # Nothing to compare against: a low absolute rate is reassuring, a
            # high one is not, and neither is a pass we can justify.
            if observed <= 0.01:
                return {
                    "check": VerificationCheckKind.ERROR_RATE.value,
                    "result": CheckResult.PASS.value,
                    "detail": (
                        f"error rate is {observed:.4f} after the action "
                        f"({after.samples} samples); no baseline existed"
                    ),
                    "observed": observed,
                    "baseline": None,
                }
            return {
                "check": VerificationCheckKind.ERROR_RATE.value,
                "result": CheckResult.NOT_OBSERVABLE.value,
                "detail": (
                    "no baseline error rate exists, and the post-action rate "
                    f"({observed:.4f}) is not low enough to be conclusive"
                ),
                "observed": observed,
                "baseline": None,
            }
        if observed <= float(baseline) * (1.0 + tolerance):
            return {
                "check": VerificationCheckKind.ERROR_RATE.value,
                "result": CheckResult.PASS.value,
                "detail": (
                    f"error rate {observed:.4f} is not worse than the baseline "
                    f"{float(baseline):.4f} (tolerance {tolerance:.0%})"
                ),
                "observed": observed,
                "baseline": float(baseline),
            }
        return {
            "check": VerificationCheckKind.ERROR_RATE.value,
            "result": CheckResult.FAIL.value,
            "detail": (
                f"error rate rose from {float(baseline):.4f} to {observed:.4f}, "
                f"beyond the {tolerance:.0%} tolerance"
            ),
            "observed": observed,
            "baseline": float(baseline),
        }

    async def _check_latency(
        self,
        action: RemediationAction,
        baseline_start: datetime,
        baseline_end: datetime,
        obs_start: datetime,
        obs_end: datetime,
    ) -> dict[str, Any]:
        before = await latency_snapshot(
            self._session,
            action.project_id,
            component_id=action.component_id,
            environment_id=action.environment_id,
            start=baseline_start,
            end=baseline_end,
        )
        after = await latency_snapshot(
            self._session,
            action.project_id,
            component_id=action.component_id,
            environment_id=action.environment_id,
            start=obs_start,
            end=obs_end,
        )
        if not after.samples or after.mean is None:
            return {
                "check": VerificationCheckKind.LATENCY.value,
                "result": CheckResult.NOT_OBSERVABLE.value,
                "detail": "no latency telemetry in the observation window",
                "observed": None,
                "baseline": before.mean,
            }
        tolerance = settings.REMEDIATION_LATENCY_TOLERANCE
        if not before.samples or before.mean is None:
            return {
                "check": VerificationCheckKind.LATENCY.value,
                "result": CheckResult.NOT_OBSERVABLE.value,
                "detail": (
                    "no baseline latency exists, so the post-action mean "
                    f"({after.mean:.1f}ms) cannot be compared"
                ),
                "observed": after.mean,
                "baseline": None,
            }
        if after.mean <= before.mean * (1.0 + tolerance):
            return {
                "check": VerificationCheckKind.LATENCY.value,
                "result": CheckResult.PASS.value,
                "detail": (
                    f"latency mean {after.mean:.1f}ms is not worse than the "
                    f"baseline {before.mean:.1f}ms (tolerance {tolerance:.0%})"
                ),
                "observed": after.mean,
                "baseline": before.mean,
            }
        return {
            "check": VerificationCheckKind.LATENCY.value,
            "result": CheckResult.FAIL.value,
            "detail": (
                f"latency rose from {before.mean:.1f}ms to {after.mean:.1f}ms, "
                f"beyond the {tolerance:.0%} tolerance"
            ),
            "observed": after.mean,
            "baseline": before.mean,
        }

    async def _check_new_anomalies(
        self,
        action: RemediationAction,
        baseline_start: datetime,
        obs_start: datetime,
    ) -> dict[str, Any]:
        before = await count_anomalies_since(
            self._session,
            action.project_id,
            baseline_start,
            component_id=action.component_id,
            environment_id=action.environment_id,
        )
        after = await count_anomalies_since(
            self._session,
            action.project_id,
            obs_start,
            component_id=action.component_id,
            environment_id=action.environment_id,
        )
        if after == 0:
            return {
                "check": VerificationCheckKind.NEW_ANOMALIES.value,
                "result": CheckResult.PASS.value,
                "detail": f"no new anomalies (pre-action window had {before})",
                "observed": 0.0,
                "baseline": float(before),
            }
        if after <= before:
            return {
                "check": VerificationCheckKind.NEW_ANOMALIES.value,
                "result": CheckResult.PASS.value,
                "detail": (
                    f"{after} anomalies after the action against {before} in the "
                    "pre-action window: the rate did not increase"
                ),
                "observed": float(after),
                "baseline": float(before),
            }
        return {
            "check": VerificationCheckKind.NEW_ANOMALIES.value,
            "result": CheckResult.FAIL.value,
            "detail": (
                f"anomaly rate increased: {after} after the action against "
                f"{before} before it"
            ),
            "observed": float(after),
            "baseline": float(before),
        }

    async def _check_new_incidents(
        self,
        action: RemediationAction,
        baseline_start: datetime,
        obs_start: datetime,
    ) -> dict[str, Any]:
        after = await count_incidents_since(
            self._session,
            action.project_id,
            obs_start,
            environment_id=action.environment_id,
        )
        before = await count_incidents_since(
            self._session,
            action.project_id,
            baseline_start,
            environment_id=action.environment_id,
        )
        if after > 0:
            return {
                "check": VerificationCheckKind.NEW_INCIDENTS.value,
                "result": CheckResult.FAIL.value,
                "detail": (
                    f"{after} incident(s) were detected after the action; the "
                    "remediation did not prevent escalation"
                ),
                "observed": float(after),
                "baseline": float(before),
            }
        return {
            "check": VerificationCheckKind.NEW_INCIDENTS.value,
            "result": CheckResult.PASS.value,
            "detail": f"no new incidents detected (pre-action window had {before})",
            "observed": 0.0,
            "baseline": float(before),
        }

    def _check_test_result(self, execution: RemediationExecution) -> dict[str, Any]:
        metadata = execution.metadata_ or {}
        test_result = metadata.get("test_result") or {}
        result = str(test_result.get("result", "NOT_OBSERVABLE"))
        if result == "PASS":
            return {
                "check": VerificationCheckKind.TEST_RESULT.value,
                "result": CheckResult.PASS.value,
                "detail": (
                    f"the detected test command '{test_result.get('command')}' "
                    "passed in the isolated workspace"
                ),
                "observed": 0.0,
                "baseline": None,
            }
        if result == "FAIL":
            return {
                "check": VerificationCheckKind.TEST_RESULT.value,
                "result": CheckResult.FAIL.value,
                "detail": (
                    f"the detected test command '{test_result.get('command')}' "
                    "failed in the isolated workspace"
                ),
                "observed": 1.0,
                "baseline": None,
            }
        return {
            "check": VerificationCheckKind.TEST_RESULT.value,
            "result": CheckResult.NOT_OBSERVABLE.value,
            "detail": str(test_result.get("reason") or "no test result was recorded"),
            "observed": None,
            "baseline": None,
        }

    def _check_workspace_diff(self, execution: RemediationExecution) -> dict[str, Any]:
        metadata = execution.metadata_ or {}
        diff_bytes = metadata.get("diff_bytes")
        if diff_bytes is None:
            return {
                "check": VerificationCheckKind.WORKSPACE_DIFF.value,
                "result": CheckResult.NOT_OBSERVABLE.value,
                "detail": "the execution recorded no workspace diff",
                "observed": None,
                "baseline": None,
            }
        if int(diff_bytes) > 0:
            return {
                "check": VerificationCheckKind.WORKSPACE_DIFF.value,
                "result": CheckResult.PASS.value,
                "detail": (
                    f"the patch produced a {int(diff_bytes)}-byte diff in the "
                    "isolated workspace"
                ),
                "observed": float(diff_bytes),
                "baseline": None,
            }
        return {
            "check": VerificationCheckKind.WORKSPACE_DIFF.value,
            "result": CheckResult.FAIL.value,
            "detail": "the patch applied but produced no change to the workspace",
            "observed": 0.0,
            "baseline": None,
        }

    # -- verdict ------------------------------------------------------------

    def _verdict(
        self,
        action: RemediationAction,
        checks: Sequence[dict[str, Any]],
        *,
        applied_at: datetime,
        obs_end: datetime,
    ) -> VerificationResult:
        passed = [c for c in checks if c["result"] == CheckResult.PASS.value]
        failed = [c for c in checks if c["result"] == CheckResult.FAIL.value]
        unobservable = [
            c for c in checks if c["result"] == CheckResult.NOT_OBSERVABLE.value
        ]
        result = VerificationResult(
            verdict=VerificationVerdict.VERIFIED,
            checks=list(checks),
            passed_count=len(passed),
            failed_count=len(failed),
            not_observable_count=len(unobservable),
        )

        primary = primary_check(action.action_type)
        primary_row = next((c for c in checks if c["check"] == primary.value), None)

        if failed:
            result.verdict = VerificationVerdict.FAILED
            result.summary = (
                "verification failed: " + "; ".join(c["detail"] for c in failed)
            )[:4000]
            result.limitations = [
                "the action was applied, so a failed verification also means a "
                "partially-applied change may need rolling back",
            ]
            return result

        if (
            primary_row is None
            or primary_row["result"] == CheckResult.NOT_OBSERVABLE.value
        ):
            result.verdict = VerificationVerdict.INCONCLUSIVE
            result.summary = (
                f"the decisive '{primary.value}' check could not be evaluated: "
                f"{(primary_row or {}).get('detail', 'the check did not run')}"
            )[:4000]
            result.limitations = [c["detail"] for c in unobservable] or [
                "no evidence was available to confirm the effect"
            ]
            return result

        if not passed:
            result.verdict = VerificationVerdict.INCONCLUSIVE
            result.summary = "no check produced a conclusive result"
            result.limitations = [c["detail"] for c in unobservable]
            return result

        if unobservable:
            result.verdict = VerificationVerdict.PARTIALLY_VERIFIED
            result.summary = (
                f"the decisive '{primary.value}' check passed; "
                f"{len(unobservable)} corroborating check(s) could not be observed"
            )[:4000]
            result.limitations = [c["detail"] for c in unobservable]
            return result

        result.summary = (
            f"all {len(passed)} checks passed, including the decisive "
            f"'{primary.value}' check"
        )
        return result

    async def _persist(
        self,
        action: RemediationAction,
        execution: Optional[RemediationExecution],
        result: VerificationResult,
    ) -> RemediationVerification:
        """Append the verification row (never overwrite a previous one)."""
        now = aware(utcnow())
        row = RemediationVerification(
            action_id=action.id,
            execution_id=execution.id if execution is not None else None,
            project_id=action.project_id,
            environment_id=action.environment_id,
            component_id=action.component_id,
            verdict=result.verdict,
            checks=result.checks,
            passed_count=result.passed_count,
            failed_count=result.failed_count,
            not_observable_count=result.not_observable_count,
            window_start=result.window_start or now,
            window_end=result.window_end or now,
            observation_seconds=result.observation_seconds,
            summary=result.summary,
            limitations=result.limitations,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def attempts(self, action_id: Any) -> int:
        """How many verification passes this action has had."""
        from app.models.remediation import RemediationVerification as Row

        return int(
            (
                await self._session.execute(
                    select(func.count(Row.id)).where(Row.action_id == action_id)
                )
            ).scalar()
            or 0
        )


def verdict_reason(verdict: VerificationVerdict) -> str:
    """A one-line, human-facing reading of a verdict."""
    return {
        VerificationVerdict.VERIFIED: "the change was applied and the system behaved as expected",
        VerificationVerdict.PARTIALLY_VERIFIED: (
            "the decisive check passed but some confirmations were unavailable"
        ),
        VerificationVerdict.FAILED: "the system did not behave as expected",
        VerificationVerdict.INCONCLUSIVE: "the effect could not be confirmed from telemetry",
        VerificationVerdict.NOT_EXECUTED: "nothing was applied, so nothing was verified",
    }[verdict]


__all__ = [
    "VerificationEngine",
    "VerificationResult",
    "primary_check",
    "verdict_reason",
]
