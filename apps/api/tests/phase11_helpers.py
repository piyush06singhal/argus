"""Shared builders for the Phase 11 test suites.

Phase 11's guarantees are about *agreement*: one state, one case, one timeline.
A fixture that hand-wrote those rows would prove nothing, because the whole phase
is the derivation. So the fixtures here build the upstream evidence — incidents,
anomalies, deployments, metric samples — and let the real services derive
everything the tests then assert on.

Three deliberate properties:

* :func:`episode` writes a complete, realistic situation (deployment → anomaly →
  incident) and returns the rows, so a test can hand them to any Phase 11 service
  and get whatever the pipeline would actually produce.
* :func:`metric_samples` writes real ``metric_records`` rows, because SLO
  evaluation reads that table and a stubbed reading would skip the whole
  window/comparison/coverage path.
* Time is always explicit. Nothing here calls ``now()`` to decide when something
  happened: the state machine's recovery window and the error budget's burn rate
  are both *about* elapsed time, so a fixture that could not place history on
  both sides of a boundary could not test them at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from tests.phase6_helpers import build_project, build_scope  # noqa: F401  (re-export)
from tests.phase8_helpers import (  # noqa: F401  (re-export)
    emit_anomaly,
    emit_deployment,
    emit_incident,
    link_dependency,
)

#: Real-looking metric names matter: the objective's indicator is validated
#: against a naming convention, so ``metric1`` would silently test nothing.
METRIC_ERROR_RATE = "http.checkout.error_rate"
METRIC_LATENCY_P95 = "http.checkout.latency.p95"
METRIC_AVAILABILITY = "http.checkout.availability"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def minutes_ago(moment: datetime, minutes: float) -> datetime:
    return moment - timedelta(minutes=minutes)


def hours_ago(moment: datetime, hours: float) -> datetime:
    return moment - timedelta(hours=hours)


def days_ago(moment: datetime, days: float) -> datetime:
    return moment - timedelta(days=days)


@dataclass
class Episode:
    """One realistic situation: a deployment, degrading telemetry, an incident."""

    project: Any
    environment: Any
    component: Any
    deployment: Any
    anomaly: Any
    incident: Any
    onset: datetime


async def metric_samples(
    session: AsyncSession,
    project: Any,
    environment: Any,
    component: Any,
    *,
    metric_name: str,
    values: list[float],
    starting_at: datetime,
    step_seconds: int = 60,
    unit: Optional[str] = None,
) -> list[Any]:
    """Write real metric rows, one per value, spaced ``step_seconds`` apart.

    ``metric_records`` is the table Phase 1 ingests into and Phase 11's SLO
    evaluator reads. Writing genuine rows is what makes a window boundary, a
    sample count and a coverage figure testable.
    """
    from app.models.observability import MetricRecord, MetricType

    created = []
    for index, value in enumerate(values):
        row = MetricRecord(
            project_id=project.id,
            environment_id=environment.id if environment else None,
            component_id=component.id if component else None,
            timestamp=starting_at + timedelta(seconds=index * step_seconds),
            metric_name=metric_name,
            metric_type=MetricType.GAUGE,
            value=float(value),
            unit=unit,
        )
        session.add(row)
        created.append(row)
    await session.flush()
    return created


async def episode(
    session: AsyncSession,
    *,
    name: str = "Phase11 Fixture",
    onset: Optional[datetime] = None,
    severity: str = "HIGH",
    incident_status: str = "OPEN",
    resolved_at: Optional[datetime] = None,
    with_deployment: bool = True,
    title: str = "Checkout failing",
    component_name: str = "checkout-service",
) -> Episode:
    """A delivery that degraded a service, the telemetry that showed it, and the
    incident that followed — in that order, so the change is genuinely *before*
    the onset and not merely present."""
    project, environment, component = await build_project(session, name=name)
    moment = onset or utcnow()
    deployed_at = moment - timedelta(minutes=30)

    deployment = None
    if with_deployment:
        deployment = await emit_deployment(
            session,
            project,
            environment,
            component,
            deployed_at=deployed_at,
            metadata={"files_changed": 12},
        )
    anomaly = await emit_anomaly(
        session,
        project,
        environment,
        component,
        detected_at=moment,
        metric_name=METRIC_LATENCY_P95,
    )
    incident = await emit_incident(
        session,
        project,
        environment,
        component,
        detected_at=moment,
        status=incident_status,
        resolved_at=resolved_at,
        severity=severity,
        title=title,
        fingerprint=f"phase11-{uuid.uuid4().hex[:8]}",
    )
    #: The incident manager sets this when it groups anomalies into an incident
    #: (``incident_manager`` does ``anomaly.incident_id = incident.id``), so the
    #: fixture links them too — otherwise postmortem/evidence tests would be
    #: exercising a state production never writes.
    anomaly.incident_id = incident.id
    await session.flush()
    return Episode(
        project=project,
        environment=environment,
        component=component,
        deployment=deployment,
        anomaly=anomaly,
        incident=incident,
        onset=moment,
    )


async def make_case(
    session: AsyncSession,
    *,
    project: Any,
    environment: Any = None,
    component: Any = None,
    incident: Any = None,
    title: str = "Checkout incident case",
    trigger: str = "INCIDENT",
) -> Any:
    """Open a case through the real service, so its timeline is real too."""
    from app.models.platform import CaseTrigger
    from app.services.reliability_case import open_case

    return await open_case(
        session,
        project_id=project.id,
        trigger=CaseTrigger(trigger),
        title=title,
        environment_id=environment.id if environment else None,
        incident_id=incident.id if incident else None,
        primary_component_id=component.id if component else None,
        component_ids=[component.id] if component else [],
        summary="fixture case",
    )


async def make_slo(
    session: AsyncSession,
    *,
    project: Any,
    component: Any = None,
    environment: Any = None,
    name: str = "Checkout availability",
    indicator: str = "AVAILABILITY",
    target: float = 0.99,
    comparison: str = "AT_LEAST",
    metric_name: str = METRIC_AVAILABILITY,
    window_seconds: int = 86_400,
) -> Any:
    """Define an objective through the real service (§33)."""
    from app.models.platform import SloComparison, SloIndicator
    from app.services.slo_service import create_slo

    return await create_slo(
        session,
        project_id=project.id,
        name=name,
        indicator=SloIndicator(indicator),
        target=target,
        comparison=SloComparison(comparison),
        metric_name=metric_name,
        component_id=component.id if component else None,
        environment_id=environment.id if environment else None,
        window_seconds=window_seconds,
    )


async def make_notification(
    session: AsyncSession,
    *,
    project: Any,
    subject_id: Optional[uuid.UUID] = None,
    kind: str = "CRITICAL_INCIDENT",
    title: str = "Critical incident opened",
    at: Optional[datetime] = None,
) -> Any:
    """Raise a notification through the real dedup path (§54–§56)."""
    from app.models.platform import NotificationKind
    from app.services.platform_notifications import notify

    return await notify(
        session,
        project_id=project.id,
        kind=NotificationKind(kind),
        title=title,
        subject_type="incident",
        subject_id=subject_id or uuid.uuid4(),
        evidence={"source": "fixture"},
        now=at,
        deliver=False,
    )


__all__ = [
    "Episode",
    "METRIC_AVAILABILITY",
    "METRIC_ERROR_RATE",
    "METRIC_LATENCY_P95",
    "build_project",
    "build_scope",
    "days_ago",
    "emit_anomaly",
    "emit_deployment",
    "emit_incident",
    "episode",
    "hours_ago",
    "link_dependency",
    "make_case",
    "make_notification",
    "make_slo",
    "metric_samples",
    "minutes_ago",
    "utcnow",
]
