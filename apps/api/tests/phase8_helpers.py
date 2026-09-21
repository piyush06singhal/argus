"""Shared builders for the Phase 8 test suites.

Deliberately explicit about *when* everything happened. Predictive reliability
is a statement about time, so a fixture that does not control timestamps cannot
prove anything about leakage, lead time or backtesting — the tests would pass
for the wrong reason.

Two helpers carry most of the weight:

* :func:`emit_metric_series` writes a deterministic series with an exact
  timestamp grid, so a trend test can assert a *direction* it constructed.
* :func:`degradation_timeline` reproduces the §72 demo shape — checkout p95
  climbing, then dependency latency, then errors, then an incident — with a
  caller-chosen ``onset`` so a test can place the future relative to a forecast.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from tests.phase6_helpers import build_project, build_scope  # noqa: F401  (re-export)

#: The metric names the demo application and the fixtures both use. Kept here so
#: a test names the same signal the predictors map onto.
METRIC_CHECKOUT_P95 = "http.checkout.latency.p95"
METRIC_CHECKOUT_ERROR_RATE = "http.checkout.error_rate"
METRIC_INVENTORY_P95 = "http.inventory.latency.p95"
METRIC_CPU = "system.cpu.utilization"
METRIC_MEMORY = "system.memory.utilization"
METRIC_QUEUE = "system.queue.depth"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def emit_metric_series(
    session: AsyncSession,
    project: Any,
    environment: Any,
    component: Any,
    *,
    metric_name: str,
    values: Sequence[float],
    end: datetime,
    step_seconds: int = 60,
    unit: str = "ms",
) -> list[Any]:
    """Write a metric series ending at ``end`` with an exact timestamp grid.

    Values are written oldest-first, so ``values[-1]`` is the value at ``end``.
    Deterministic: no jitter, no randomness — a trend assertion can therefore
    assert the exact slope it constructed.
    """
    from app.models.observability import MetricRecord, MetricType

    rows = []
    count = len(values)
    for index, value in enumerate(values):
        offset = (count - 1 - index) * step_seconds
        row = MetricRecord(
            project_id=project.id,
            environment_id=environment.id if environment else None,
            component_id=component.id if component else None,
            timestamp=end - timedelta(seconds=offset),
            metric_name=metric_name,
            metric_type=MetricType.GAUGE,
            value=float(value),
            unit=unit,
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


async def emit_error_logs(
    session: AsyncSession,
    project: Any,
    environment: Any,
    component: Any,
    *,
    count: int,
    end: datetime,
    span_seconds: int = 600,
    message: str = "TimeoutError: inventory database query timed out",
) -> list[Any]:
    """Write ``count`` ERROR logs spread evenly across ``span_seconds``."""
    from app.models.observability import LogRecord, Severity

    rows = []
    for index in range(count):
        offset = (count - 1 - index) * max(span_seconds // max(count, 1), 1)
        row = LogRecord(
            project_id=project.id,
            environment_id=environment.id if environment else None,
            component_id=component.id if component else None,
            timestamp=end - timedelta(seconds=offset),
            level=Severity.ERROR,
            service=getattr(component, "name", "service"),
            message=message,
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


async def emit_spans(
    session: AsyncSession,
    project: Any,
    component: Any,
    *,
    durations_ms: Sequence[float],
    end: datetime,
    step_seconds: int = 30,
    error_every: Optional[int] = None,
) -> list[Any]:
    """Write spans with a deterministic duration series.

    ``error_every`` marks every Nth span as ERROR, which is how a test builds a
    *sustained* failure rate rather than a single blip.
    """
    from app.models.observability import SpanRecord, TraceStatus

    rows = []
    count = len(durations_ms)
    for index, duration in enumerate(durations_ms):
        offset = (count - 1 - index) * step_seconds
        failing = error_every is not None and index % error_every == 0
        row = SpanRecord(
            trace_id=f"trace-{uuid.uuid4().hex[:10]}",
            span_id=f"span-{uuid.uuid4().hex[:8]}",
            project_id=project.id,
            component_id=component.id if component else None,
            operation="POST /checkout",
            start_time=end - timedelta(seconds=offset),
            duration_ms=float(duration),
            status=TraceStatus.ERROR if failing else TraceStatus.OK,
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


async def emit_anomaly(
    session: AsyncSession,
    project: Any,
    environment: Any,
    component: Any,
    *,
    detected_at: datetime,
    anomaly_type: str = "LATENCY_SPIKE",
    severity: str = "HIGH",
    metric_name: str = METRIC_CHECKOUT_P95,
    observation_count: int = 1,
) -> Any:
    """Write one anomaly, with an explicit type so similarity can be tested."""
    from app.models.anomaly import (
        Anomaly,
        AnomalySeverity,
        AnomalySource,
        AnomalyStatus,
        AnomalyType,
    )

    row = Anomaly(
        project_id=project.id,
        environment_id=environment.id if environment else None,
        component_id=component.id if component else None,
        anomaly_type=AnomalyType(anomaly_type),
        severity=AnomalySeverity(severity),
        status=AnomalyStatus.DETECTED,
        source=AnomalySource.METRIC,
        metric_name=metric_name,
        fingerprint=uuid.uuid4().hex,
        description=f"{anomaly_type} on {metric_name}",
        detected_at=detected_at,
        started_at=detected_at,
        observation_count=observation_count,
    )
    session.add(row)
    await session.flush()
    return row


async def emit_incident(
    session: AsyncSession,
    project: Any,
    environment: Any,
    component: Any,
    *,
    detected_at: datetime,
    resolved_at: Optional[datetime] = None,
    severity: str = "HIGH",
    status: str = "RESOLVED",
    title: str = "Checkout failures",
    fingerprint: Optional[str] = None,
) -> Any:
    """Write one incident with an explicit fingerprint, for recurrence tests."""
    from app.models.incident import Incident, IncidentSeverity, IncidentStatus

    row = Incident(
        project_id=project.id,
        environment_id=environment.id if environment else None,
        primary_component_id=component.id if component else None,
        title=title,
        severity=IncidentSeverity(severity),
        status=IncidentStatus(status),
        detected_at=detected_at,
        started_at=detected_at,
        resolved_at=resolved_at,
        fingerprint=fingerprint,
        summary=title,
    )
    session.add(row)
    await session.flush()
    return row


async def emit_deployment(
    session: AsyncSession,
    project: Any,
    environment: Any,
    component: Any,
    *,
    deployed_at: datetime,
    status: str = "SUCCESS",
    metadata: Optional[dict] = None,
) -> Any:
    """Write one deployment, optionally with size metadata."""
    from app.models.deployment import DeploymentEvent, DeploymentStatus

    row = DeploymentEvent(
        project_id=project.id,
        environment_id=environment.id if environment else None,
        component_id=component.id if component else None,
        deployment_id=f"deploy-{uuid.uuid4().hex[:8]}",
        version="1.0.0",
        status=DeploymentStatus(status),
        deployed_at=deployed_at,
        description="fixture deployment",
        metadata_=metadata,
    )
    session.add(row)
    await session.flush()
    return row


async def link_dependency(
    session: AsyncSession,
    project: Any,
    source: Any,
    target: Any,
    *,
    dependency_type: str = "HTTP",
) -> Any:
    """Create a structural dependency edge (Phase 0 table)."""
    from app.models.system import ComponentDependency, DependencyType

    row = ComponentDependency(
        source_component_id=source.id,
        target_component_id=target.id,
        dependency_type=DependencyType(dependency_type),
        description="fixture dependency",
    )
    session.add(row)
    await session.flush()
    return row


async def degradation_timeline(
    session: AsyncSession,
    project: Any,
    environment: Any,
    component: Any,
    *,
    onset: datetime,
    dependency: Optional[Any] = None,
    samples: int = 12,
    step_seconds: int = 300,
    start_p95: float = 420.0,
    end_p95: float = 730.0,
    error_rate_start: float = 0.005,
    error_rate_end: float = 0.08,
    log_errors: int = 8,
) -> dict:
    """Reproduce the §72 gradual-degradation shape ending at ``onset``.

    Builds an increasing latency series, an increasing error-rate series, error
    logs, a rising dependency latency series and one successful deployment —
    everything a forecast made at ``onset`` is allowed to see, and nothing
    after it. Returns the values it wrote so a test can assert the direction it
    constructed rather than a magic number.

    The incident that follows is *not* created here: the caller places it after
    ``onset``, which is exactly how the leakage tests are written.
    """
    latency = [
        start_p95 + (end_p95 - start_p95) * (index / (samples - 1))
        for index in range(samples)
    ]
    error_rate = [
        error_rate_start + (error_rate_end - error_rate_start) * (index / (samples - 1))
        for index in range(samples)
    ]
    await emit_metric_series(
        session,
        project,
        environment,
        component,
        metric_name=METRIC_CHECKOUT_P95,
        values=latency,
        end=onset,
        step_seconds=step_seconds,
    )
    await emit_metric_series(
        session,
        project,
        environment,
        component,
        metric_name=METRIC_CHECKOUT_ERROR_RATE,
        values=error_rate,
        end=onset,
        step_seconds=step_seconds,
        unit="ratio",
    )
    if dependency is not None:
        await emit_metric_series(
            session,
            project,
            environment,
            dependency,
            metric_name=METRIC_INVENTORY_P95,
            values=[
                120.0 + 380.0 * (index / (samples - 1)) for index in range(samples)
            ],
            end=onset,
            step_seconds=step_seconds,
        )
    await emit_error_logs(
        session, project, environment, component, count=log_errors, end=onset
    )
    await emit_deployment(
        session,
        project,
        environment,
        component,
        deployed_at=onset - timedelta(minutes=20),
        metadata={"files_changed": 3, "lines_added": 90, "lines_removed": 12},
    )
    return {
        "latency": latency,
        "error_rate": error_rate,
        "onset": onset,
        "step_seconds": step_seconds,
    }


async def recovery_timeline(
    session: AsyncSession,
    project: Any,
    environment: Any,
    component: Any,
    *,
    end: datetime,
    samples: int = 12,
    step_seconds: int = 300,
) -> dict:
    """The §74 false-positive shape: latency rises then recovers, errors flat.

    A predictable pattern that does **not** end in an incident — the fixture
    that lets a test prove ARGUS never presents elevated risk as a confirmed
    failure.
    """
    peak = samples // 2
    latency = [
        #: Up to the midpoint, then back down to where it started.
        420.0 + (300.0 * (index / peak))
        if index <= peak
        else 720.0 - (300.0 * ((index - peak) / max(samples - 1 - peak, 1)))
        for index in range(samples)
    ]
    await emit_metric_series(
        session,
        project,
        environment,
        component,
        metric_name=METRIC_CHECKOUT_P95,
        values=latency,
        end=end,
        step_seconds=step_seconds,
    )
    await emit_metric_series(
        session,
        project,
        environment,
        component,
        metric_name=METRIC_CHECKOUT_ERROR_RATE,
        values=[0.004] * samples,
        end=end,
        step_seconds=step_seconds,
        unit="ratio",
    )
    return {"latency": latency, "end": end, "step_seconds": step_seconds}


def hours_before(moment: datetime, hours: float) -> datetime:
    return moment - timedelta(hours=hours)


def days_before(moment: datetime, days: float) -> datetime:
    return moment - timedelta(days=days)


def all_present(values: Iterable[Optional[float]]) -> bool:
    return all(value is not None for value in values)
