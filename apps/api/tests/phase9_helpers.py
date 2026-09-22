"""Shared builders for the Phase 9 test suites.

Phase 9's guarantees are all about *refusal*, and a refusal is only meaningful
against a target that would otherwise be allow-able. So the fixtures here are
deliberately permissive where they can be:

* :func:`build_scope` gives every test a project, an environment and a component
  with no policy row at all — the most restrictive default — and
  :func:`set_policy` is how a test opts into a less restrictive regime, one
  recorded field at a time.
* :func:`make_draft` builds a proposal for a real registered action with real
  parameters, so an assessment exercises the parameter schema rather than a
  hand-written row that happens to be valid.
* :func:`healthy_then_recovered` writes telemetry on both sides of an execution,
  which is what a verification window actually reads. A verification test that
  does not control that telemetry would pass for the wrong reason.

Nothing here monkeypatches a gate. If a test needs an action to reach
``AUTHORIZED``, it must configure a policy that allows it — the same thing an
operator would have to do.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import EnvironmentType
from tests.phase6_helpers import build_project, build_scope  # noqa: F401  (re-export)
from tests.phase8_helpers import emit_metric_series, emit_error_logs  # noqa: F401

#: The metric names the control-plane checks look for. Kept here so a test names
#: the same signal the verification engine maps onto.
METRIC_ERROR_RATE = "http.checkout.error_rate"
METRIC_P95 = "http.checkout.latency.p95"
METRIC_CPU = "system.cpu.utilization"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def set_policy(
    session: AsyncSession,
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = None,
    **values: Any,
):
    """Create or update a scope's policy row.

    A thin wrapper over the real :func:`upsert_policy` so a test cannot
    accidentally bypass the clamping the engine applies.
    """
    from app.services.remediation_policy import upsert_policy

    return await upsert_policy(
        session,
        project_id,
        environment_id,
        values,
        updated_by=values.pop("updated_by", "test"),
    )


async def set_environment_class(session: AsyncSession, environment, name: str):
    """Name an environment *and* declare the type that goes with that name.

    A scope counts as non-production only when **both** signals agree (see
    :func:`app.services.remediation_policy.environment_is_non_production`): the
    name has to be in the configured allow-list, and the environment must not be
    declared ``PRODUCTION``. ``build_project`` creates a production-typed
    environment, so a test that only renamed it would be describing a production
    environment with a misleading name — and would no longer be testing the
    thing it claims to test.
    """
    environment.name = name
    environment.environment_type = (
        EnvironmentType.PRODUCTION
        if name.strip().lower() == "production"
        else EnvironmentType.STAGING
    )
    await session.flush()
    return environment


def make_draft(
    project,
    environment,
    component,
    *,
    action_type=None,
    parameters: Optional[dict] = None,
    source_type=None,
    source_id: Optional[uuid.UUID] = None,
    incident_id: Optional[uuid.UUID] = None,
    forecast_id: Optional[uuid.UUID] = None,
    patch_id: Optional[uuid.UUID] = None,
    risk_level=None,
    blast_radius=None,
    confidence: float = 0.7,
    strategy: str = "TEST",
    problem: str = "the component is unhealthy",
    expected_effect: str = "the component recovers",
):
    """A planner-shaped proposal draft for a registered action.

    Defaults to ``PAUSE_BACKGROUND_JOB`` because it is the one action that is
    genuinely executable in this build (the control plane is ARGUS's own
    runtime), which is what lets the execution tests assert a *real* effect.
    """
    from app.models.remediation import (
        BlastRadiusScope,
        RemediationActionType,
        RemediationRiskLevel,
        RemediationSourceType,
    )
    from app.services.remediation_planner import ProposalDraft

    action_type = action_type or RemediationActionType.PAUSE_BACKGROUND_JOB
    source_type = source_type or RemediationSourceType.INCIDENT
    if parameters is None:
        #: Names must match the registry's own parameter names exactly; an
        #: accepted-looking synonym would make the fixture fail validation.
        parameters = {
            RemediationActionType.PAUSE_BACKGROUND_JOB: {"job": "anomaly_sweep"},
            RemediationActionType.RESUME_BACKGROUND_JOB: {"job": "anomaly_sweep"},
            RemediationActionType.DISABLE_FEATURE_FLAG: {"flag": "graph_extraction"},
            RemediationActionType.ENABLE_FEATURE_FLAG: {"flag": "graph_extraction"},
        }.get(action_type, {})
    return ProposalDraft(
        project_id=project.id,
        environment_id=environment.id if environment else None,
        component_id=component.id if component else None,
        action_type=action_type,
        source_type=source_type,
        source_id=source_id,
        strategy=strategy,
        problem=problem,
        recommended_action=f"{action_type.value} on the affected scope",
        expected_effect=expected_effect,
        rationale="a test fixture proposed this from stored evidence",
        risk_level=risk_level or RemediationRiskLevel.LOW,
        blast_radius=blast_radius or BlastRadiusScope.SINGLE_COMPONENT,
        confidence=confidence,
        confidence_reason="conservative fixture confidence",
        limitations=["fixture: the evidence is synthetic"],
        supporting_evidence=[{"kind": "fixture", "id": str(source_id)}],
        parameters=parameters,
        incident_id=incident_id,
        forecast_id=forecast_id,
        patch_id=patch_id,
    )


async def clone_action(session: AsyncSession, action, *, attempt: int = 2):
    """A second action row carrying the *same* fingerprint as ``action``.

    Three layers normally make a duplicate impossible: the service refuses a
    duplicate proposal, the safety engine refuses a duplicate active action, and
    the table has a unique constraint on ``(project_id, fingerprint, attempt)``.
    Only the third is absolute, so reaching the safety guard at all requires
    bypassing that constraint — which is what ``attempt`` does here. The result
    reproduces the concurrent-proposal race the guard exists for: two requests
    that both pass the pre-insert "is this a duplicate?" check before either has
    flushed, arriving with different attempt counters.
    """
    from app.models.remediation import RemediationAction, RemediationStatus

    columns = {
        column.name: getattr(action, column.name)
        for column in RemediationAction.__mapper__.columns
        if column.name not in ("id", "created_at", "updated_at")
    }
    columns["status"] = RemediationStatus.PROPOSED
    columns["attempt"] = attempt
    clone = RemediationAction(**columns)
    session.add(clone)
    await session.flush()
    return clone


async def build_incident(
    session: AsyncSession,
    project,
    environment,
    component,
    *,
    severity: str = "HIGH",
    status: str = "OPEN",
):
    """A minimal open incident, so a fixture action has real evidence.

    The safety engine refuses an action that references no stored row ("an
    unevidenced remediation is indistinguishable from a guess"), so a fixture
    without one would be testing the refusal path by accident.
    """
    from app.models.incident import Incident, IncidentSeverity, IncidentStatus

    onset = utcnow() - timedelta(minutes=20)
    row = Incident(
        project_id=project.id,
        environment_id=environment.id if environment else None,
        primary_component_id=component.id if component else None,
        title="Checkout failures",
        severity=IncidentSeverity(severity),
        status=IncidentStatus(status),
        detected_at=onset,
        started_at=onset,
        fingerprint=uuid.uuid4().hex,
        summary="error rate elevated on checkout",
    )
    session.add(row)
    await session.flush()
    return row


async def make_action(session: AsyncSession, project, environment, component, **kwargs):
    """Propose one action, with an incident attached as its evidence."""
    from app.services.remediation_service import RemediationService

    created_by = kwargs.pop("created_by", "test")
    if not kwargs.get("incident_id"):
        incident = await build_incident(session, project, environment, component)
        kwargs["incident_id"] = incident.id
    #: A planner draft names the row it came from as well as the incident, and
    #: the fingerprint is derived from that source id — omitting it would make a
    #: fixture action look like a different remediation from the draft that
    #: describes it.
    if not kwargs.get("source_id") and not any(
        kwargs.get(key) for key in ("forecast_id", "patch_id")
    ):
        kwargs["source_id"] = kwargs["incident_id"]
    drafts = [make_draft(project, environment, component, **kwargs)]
    created = await RemediationService(session).propose(drafts, created_by=created_by)
    assert created, "the fixture draft was deduplicated unexpectedly"
    return created[0]


async def healthy_then_recovered(
    session: AsyncSession,
    project,
    environment,
    component,
    *,
    applied_at: datetime,
    good_errors: float = 0.0,
    bad_errors: float = 0.25,
    samples: int = 6,
    step_seconds: int = 30,
    p95_good: float = 180.0,
    p95_bad: float = 900.0,
) -> dict:
    """Telemetry on both sides of an execution.

    Before ``applied_at`` the component is unhealthy (the incident), after it the
    component is healthy (the remediation worked). A verification window opened
    after the execution therefore sees improvement — and the same fixture with
    ``recovered=False`` behavior is what the failure/rollback tests invert.
    """
    #: The name has to contain one of the evidence layer's own hints, or the
    #: series is invisible to the check being tested.
    error_metric = METRIC_ERROR_RATE

    await emit_metric_series(
        session,
        project,
        environment,
        component,
        metric_name=error_metric,
        values=[bad_errors] * samples,
        end=applied_at - timedelta(seconds=step_seconds),
        step_seconds=step_seconds,
        unit="ratio",
    )
    await emit_metric_series(
        session,
        project,
        environment,
        component,
        metric_name=error_metric,
        values=[good_errors] * samples,
        end=applied_at + timedelta(seconds=samples * step_seconds),
        step_seconds=step_seconds,
        unit="ratio",
    )
    await emit_metric_series(
        session,
        project,
        environment,
        component,
        metric_name=METRIC_P95,
        values=[p95_bad] * samples,
        end=applied_at - timedelta(seconds=step_seconds),
        step_seconds=step_seconds,
    )
    await emit_metric_series(
        session,
        project,
        environment,
        component,
        metric_name=METRIC_P95,
        values=[p95_good] * samples,
        end=applied_at + timedelta(seconds=samples * step_seconds),
        step_seconds=step_seconds,
    )
    await emit_error_logs(
        session,
        project,
        environment,
        component,
        count=6,
        end=applied_at - timedelta(seconds=step_seconds),
    )
    return {
        "applied_at": applied_at,
        "error_metric": error_metric,
        "window_end": applied_at + timedelta(seconds=samples * step_seconds),
    }


async def degraded_after_apply(
    session: AsyncSession,
    project,
    environment,
    component,
    *,
    applied_at: datetime,
    samples: int = 6,
    step_seconds: int = 30,
) -> dict:
    """The inverse of :func:`healthy_then_recovered` — the remediation harmed.

    Errors go *up* after the action, which is the only way a verification test
    can prove that ``FAILED`` is reachable and that a harmful change triggers a
    rollback rather than being reported as a success.
    """
    error_metric = METRIC_ERROR_RATE
    await emit_metric_series(
        session,
        project,
        environment,
        component,
        metric_name=error_metric,
        values=[0.01] * samples,
        end=applied_at - timedelta(seconds=step_seconds),
        step_seconds=step_seconds,
        unit="ratio",
    )
    await emit_metric_series(
        session,
        project,
        environment,
        component,
        metric_name=error_metric,
        values=[0.65] * samples,
        end=applied_at + timedelta(seconds=samples * step_seconds),
        step_seconds=step_seconds,
        unit="ratio",
    )
    await emit_error_logs(
        session,
        project,
        environment,
        component,
        count=25,
        end=applied_at + timedelta(seconds=samples * step_seconds),
    )
    return {"applied_at": applied_at, "error_metric": error_metric}


def in_seconds(seconds: float) -> datetime:
    return utcnow() + timedelta(seconds=seconds)


def minutes_ago(minutes: float) -> datetime:
    return utcnow() - timedelta(minutes=minutes)


def assert_single(values: Sequence[Any]) -> Any:
    assert len(values) == 1, f"expected exactly one row, found {len(values)}"
    return values[0]
