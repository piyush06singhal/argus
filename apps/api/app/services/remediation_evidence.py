"""ARGUS Remediation Evidence Reader (Phase 9 §19, §28–§31, §46).

One place that answers *"what does the stored telemetry actually say about this
component, right now?"* — used by the safety engine to evaluate preconditions and
by the verification engine to decide whether an action worked.

Everything here reads Phase 0–3 rows: health checks, metrics and spans. Nothing
writes, and nothing calls out to a system. That matters for two reasons: the
safety assessment must be cheap enough to run before *every* attempt, and a
verification that could itself perturb the system would be worthless as evidence.

The most important property in this module is the distinction between **a real
measurement and no measurement**. Every snapshot carries ``samples``, and a caller
that treats ``samples == 0`` as "fine" is a bug — which is why
:class:`ObservationResult` has an explicit ``NOT_OBSERVABLE`` result and why
:func:`error_rate` returns a rate only when it actually has samples to compute
one from.

Error rate and latency are derived from **two** independent sources on purpose
(metrics first, spans as a fallback). If a system ingests spans but not error-rate
metrics, ARGUS can still verify a remediation there; if neither exists, it says so
rather than guessing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.anomaly import Anomaly
from app.models.incident import Incident
from app.models.ingestion import HealthCheckEvent, HealthStatus
from app.models.observability import MetricRecord, SpanRecord, TraceStatus
from app.models.remediation import CheckResult, RemediationControlKind
from app.services.remediation_clock import aware, utcnow

#: Metric names that represent an error ratio, matched case-insensitively by
#: substring so `error_rate`, `http_error_rate` and `errors_ratio` all qualify.
_ERROR_RATE_HINTS = ("error_rate", "error_ratio", "errors_ratio", "failure_rate")
#: Metric names that represent latency.
_LATENCY_HINTS = ("latency", "duration", "response_time", "request_time")


@dataclass
class ObservationResult:
    """A measurement, or an honest statement that there is not one."""

    result: CheckResult
    observed: Optional[float] = None
    baseline: Optional[float] = None
    samples: int = 0
    detail: str = ""
    source: Optional[str] = None

    @property
    def observable(self) -> bool:
        return self.result != CheckResult.NOT_OBSERVABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "result": self.result.value,
            "observed": self.observed,
            "baseline": self.baseline,
            "samples": self.samples,
            "detail": self.detail,
            "source": self.source,
        }


@dataclass
class HealthSnapshot:
    """Component health over a window (Phase 1 §15)."""

    total: int = 0
    healthy: int = 0
    degraded: int = 0
    unhealthy: int = 0
    unknown: int = 0
    latest_status: Optional[HealthStatus] = None
    latest_at: Optional[datetime] = None
    latency_mean_ms: Optional[float] = None

    @property
    def samples(self) -> int:
        return self.total

    @property
    def unhealthy_ratio(self) -> Optional[float]:
        if self.total == 0:
            return None
        return (self.degraded + self.unhealthy) / self.total

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "healthy": self.healthy,
            "degraded": self.degraded,
            "unhealthy": self.unhealthy,
            "unknown": self.unknown,
            "latest_status": self.latest_status.value if self.latest_status else None,
            "latest_at": self.latest_at.isoformat() if self.latest_at else None,
            "latency_mean_ms": self.latency_mean_ms,
            "unhealthy_ratio": self.unhealthy_ratio,
        }


@dataclass
class MetricSnapshot:
    """Aggregate of named metric series over a window."""

    samples: int = 0
    mean: Optional[float] = None
    maximum: Optional[float] = None
    minimum: Optional[float] = None
    names: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "mean": self.mean,
            "maximum": self.maximum,
            "minimum": self.minimum,
            "names": list(self.names),
        }


def _scope_conditions(
    model,
    project_id: uuid.UUID,
    *,
    component_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
):
    conditions = [model.project_id == project_id]
    if component_id is not None:
        conditions.append(model.component_id == component_id)
    if environment_id is not None and hasattr(model, "environment_id"):
        conditions.append(model.environment_id == environment_id)
    return conditions


async def health_snapshot(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    component_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> HealthSnapshot:
    """Health-check history for a component (or the whole project) over a window."""
    conditions = _scope_conditions(
        HealthCheckEvent,
        project_id,
        component_id=component_id,
        environment_id=environment_id,
    )
    if start is not None:
        conditions.append(HealthCheckEvent.timestamp >= aware(start))
    if end is not None:
        conditions.append(HealthCheckEvent.timestamp <= aware(end))

    rows = (
        await session.execute(
            select(
                HealthCheckEvent.status,
                HealthCheckEvent.timestamp,
                HealthCheckEvent.latency_ms,
            )
            .where(and_(*conditions))
            .order_by(HealthCheckEvent.timestamp.desc())
            .limit(2000)
        )
    ).all()

    snapshot = HealthSnapshot()
    latencies: list[float] = []
    for status, timestamp, latency in rows:
        snapshot.total += 1
        if snapshot.latest_status is None:
            snapshot.latest_status = status
            snapshot.latest_at = timestamp
        if status == HealthStatus.HEALTHY:
            snapshot.healthy += 1
        elif status == HealthStatus.DEGRADED:
            snapshot.degraded += 1
        elif status == HealthStatus.UNHEALTHY:
            snapshot.unhealthy += 1
        else:
            snapshot.unknown += 1
        if latency is not None:
            latencies.append(float(latency))
    if latencies:
        snapshot.latency_mean_ms = sum(latencies) / len(latencies)
    return snapshot


async def metric_snapshot(
    session: AsyncSession,
    project_id: uuid.UUID,
    names: Sequence[str],
    *,
    component_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> MetricSnapshot:
    """Aggregate the named metric series (exact names) over a window."""
    if not names:
        return MetricSnapshot()
    conditions = _scope_conditions(
        MetricRecord,
        project_id,
        component_id=component_id,
        environment_id=environment_id,
    )
    conditions.append(MetricRecord.metric_name.in_(list(names)))
    if start is not None:
        conditions.append(MetricRecord.timestamp >= aware(start))
    if end is not None:
        conditions.append(MetricRecord.timestamp <= aware(end))

    rows = (
        await session.execute(
            select(
                MetricRecord.metric_name,
                func.count(MetricRecord.id),
                func.avg(MetricRecord.value),
                func.max(MetricRecord.value),
                func.min(MetricRecord.value),
            )
            .where(and_(*conditions))
            .group_by(MetricRecord.metric_name)
        )
    ).all()

    total = 0
    weighted_mean_sum = 0.0
    maximum: Optional[float] = None
    minimum: Optional[float] = None
    seen: list[str] = []
    for name, count, mean, max_value, min_value in rows:
        count = int(count or 0)
        if count == 0:
            continue
        seen.append(name)
        total += count
        if mean is not None:
            weighted_mean_sum += float(mean) * count
        if max_value is not None:
            maximum = (
                float(max_value) if maximum is None else max(maximum, float(max_value))
            )
        if min_value is not None:
            minimum = (
                float(min_value) if minimum is None else min(minimum, float(min_value))
            )

    if total == 0:
        return MetricSnapshot()
    return MetricSnapshot(
        samples=total,
        mean=weighted_mean_sum / total if total else None,
        maximum=maximum,
        minimum=minimum,
        names=tuple(sorted(seen)),
    )


async def _matching_metric_names(
    session: AsyncSession,
    project_id: uuid.UUID,
    hints: Sequence[str],
    *,
    component_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
) -> tuple[str, ...]:
    """Discover metric names by intent, bounded to this scope's own series."""
    conditions = _scope_conditions(
        MetricRecord,
        project_id,
        component_id=component_id,
        environment_id=environment_id,
    )
    rows = (
        (
            await session.execute(
                select(MetricRecord.metric_name)
                .where(and_(*conditions))
                .distinct()
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    matched = [
        name for name in rows if any(hint in (name or "").lower() for hint in hints)
    ]
    return tuple(sorted(matched))


async def error_rate(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    component_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> ObservationResult:
    """The observed error ratio, from metrics if present, else from spans.

    Returns ``NOT_OBSERVABLE`` when there is genuinely nothing to compute from.
    That is the important case: a system with no error telemetry cannot be
    *verified*, and reporting 0.0 there would be the single most dangerous lie
    this engine could tell.
    """
    names = await _matching_metric_names(
        session,
        project_id,
        _ERROR_RATE_HINTS,
        component_id=component_id,
        environment_id=environment_id,
    )
    snapshot = await metric_snapshot(
        session,
        project_id,
        names,
        component_id=component_id,
        environment_id=environment_id,
        start=start,
        end=end,
    )
    if snapshot.samples > 0 and snapshot.mean is not None:
        # A ratio metric reported as a percentage is normalised so callers
        # compare like with like.
        value = snapshot.mean
        if value > 1.0:
            value = value / 100.0
        return ObservationResult(
            result=CheckResult.PASS,
            observed=round(value, 6),
            samples=snapshot.samples,
            detail=f"mean of {len(snapshot.names)} error metric series",
            source="metrics",
        )

    span_stats = await span_error_ratio(
        session,
        project_id,
        component_id=component_id,
        environment_id=environment_id,
        start=start,
        end=end,
    )
    return span_stats


async def span_error_ratio(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    component_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> ObservationResult:
    """Error ratio computed from stored spans (Phase 1 §12)."""
    conditions = [SpanRecord.project_id == project_id]
    if component_id is not None:
        conditions.append(SpanRecord.component_id == component_id)
    if start is not None:
        conditions.append(SpanRecord.start_time >= aware(start))
    if end is not None:
        conditions.append(SpanRecord.start_time <= aware(end))

    rows = (
        await session.execute(
            select(SpanRecord.status, func.count(SpanRecord.id))
            .where(and_(*conditions))
            .group_by(SpanRecord.status)
        )
    ).all()
    total = sum(int(count or 0) for _status, count in rows)
    if total == 0:
        return ObservationResult(
            result=CheckResult.NOT_OBSERVABLE,
            samples=0,
            detail="no spans and no error metrics were recorded in the window",
        )
    failing = sum(
        int(count or 0)
        for status, count in rows
        if status in (TraceStatus.ERROR, TraceStatus.TIMEOUT)
    )
    return ObservationResult(
        result=CheckResult.PASS,
        observed=round(failing / total, 6),
        samples=total,
        detail=f"{failing} of {total} spans failed",
        source="spans",
    )


async def latency_snapshot(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    component_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> MetricSnapshot:
    """Latency series for a scope, discovered by intent then aggregated."""
    names = await _matching_metric_names(
        session,
        project_id,
        _LATENCY_HINTS,
        component_id=component_id,
        environment_id=environment_id,
    )
    snapshot = await metric_snapshot(
        session,
        project_id,
        names,
        component_id=component_id,
        environment_id=environment_id,
        start=start,
        end=end,
    )
    if snapshot.samples > 0:
        return snapshot

    # Fall back to span durations: a system that records traces but no latency
    # metric still has observable latency.
    conditions = [
        SpanRecord.project_id == project_id,
        SpanRecord.duration_ms.is_not(None),
    ]
    if component_id is not None:
        conditions.append(SpanRecord.component_id == component_id)
    if start is not None:
        conditions.append(SpanRecord.start_time >= aware(start))
    if end is not None:
        conditions.append(SpanRecord.start_time <= aware(end))
    row = (
        await session.execute(
            select(
                func.count(SpanRecord.id),
                func.avg(SpanRecord.duration_ms),
                func.max(SpanRecord.duration_ms),
                func.min(SpanRecord.duration_ms),
            ).where(and_(*conditions))
        )
    ).first()
    if row is None or not row[0]:
        return MetricSnapshot()
    return MetricSnapshot(
        samples=int(row[0]),
        mean=float(row[1]) if row[1] is not None else None,
        maximum=float(row[2]) if row[2] is not None else None,
        minimum=float(row[3]) if row[3] is not None else None,
        names=("span.duration_ms",),
    )


async def count_anomalies_since(
    session: AsyncSession,
    project_id: uuid.UUID,
    since: datetime,
    *,
    component_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
) -> int:
    """Anomalies detected (by ``detected_at``) at or after ``since``."""
    conditions = [
        Anomaly.project_id == project_id,
        Anomaly.detected_at >= aware(since),
    ]
    if component_id is not None:
        conditions.append(Anomaly.component_id == component_id)
    if environment_id is not None:
        conditions.append(Anomaly.environment_id == environment_id)
    return int(
        (
            await session.execute(
                select(func.count(Anomaly.id)).where(and_(*conditions))
            )
        ).scalar()
        or 0
    )


async def count_incidents_since(
    session: AsyncSession,
    project_id: uuid.UUID,
    since: datetime,
    *,
    environment_id: Optional[uuid.UUID] = None,
) -> int:
    """Incidents detected at or after ``since``."""
    conditions = [
        Incident.project_id == project_id,
        Incident.detected_at >= aware(since),
    ]
    if environment_id is not None:
        conditions.append(Incident.environment_id == environment_id)
    return int(
        (
            await session.execute(
                select(func.count(Incident.id)).where(and_(*conditions))
            )
        ).scalar()
        or 0
    )


# ---------------------------------------------------------------------------
# Effect probes: did the control actually change the platform's behaviour?
# ---------------------------------------------------------------------------

#: Maps a gated thing (feature flag or background job) to the observable
#: artefact it produces. Verification uses this to prove that *stopping* the
#: thing stopped its output, instead of merely reading back its own setting —
#: the difference between "the switch is off" and "the machine is quiet".
_EFFECT_PROBES: dict[str, tuple[str, str]] = {
    # feature flags
    "reliability_forecasting": ("reliability_forecasts", "forecasts written"),
    "anomaly_detection": ("anomalies", "anomalies detected"),
    "code_indexing": ("code_index_runs", "index runs started"),
    "fix_verification": ("patch_verification_runs", "patch verifications started"),
    "reproduction_execution": ("reproduction_runs", "reproduction runs started"),
    "graph_extraction": ("graph_snapshots", "graph snapshots built"),
    # background jobs (same artefacts, different switch)
    "reliability_sweep": ("reliability_forecasts", "forecasts written"),
    "anomaly_sweep": ("anomalies", "anomalies detected"),
    "code_sweep": ("code_index_runs", "index runs started"),
    "fix_sweep": ("patch_verification_runs", "patch verifications started"),
    "reproduction_sweep": ("reproduction_runs", "reproduction runs started"),
    "ingestion_worker": ("observability_events", "events ingested"),
    "remediation_sweep": ("remediation_executions", "remediation executions recorded"),
}


def probe_for(scope_key: str) -> Optional[tuple[str, str]]:
    """The observable artefact a gated control produces, if one is known."""
    return _EFFECT_PROBES.get(scope_key)


async def count_artifacts_since(
    session: AsyncSession,
    table: str,
    project_id: Optional[uuid.UUID],
    since: datetime,
) -> Optional[int]:
    """Count rows created in ``table`` at or after ``since``, project-scoped.

    Only tables named in :data:`_EFFECT_PROBES` are addressable, and the table
    name is never interpolated from user input — a probe is a fixed, reviewed
    mapping, so this cannot become a query-injection or table-enumeration
    primitive.
    """
    from sqlalchemy import text as sa_text

    allowed = {table_name for table_name, _ in _EFFECT_PROBES.values()}
    if table not in allowed:  # pragma: no cover - guarded by callers
        return None

    conditions = ["created_at >= :since"]
    params: dict[str, Any] = {"since": aware(since)}
    if project_id is not None:
        conditions.append("project_id = :project_id")
        params["project_id"] = str(project_id)
    # Table names come from the reviewed allow-list above, never from input.
    sql = sa_text(
        f"SELECT COUNT(*) FROM {table} WHERE " + " AND ".join(conditions)  # noqa: S608
    )
    value = (await session.execute(sql, params)).scalar()
    return int(value or 0)


async def control_effect_observation(
    session: AsyncSession,
    *,
    kind: RemediationControlKind,
    scope_key: str,
    project_id: Optional[uuid.UUID],
    since: datetime,
) -> ObservationResult:
    """Observe whether the gated thing's output stopped (or resumed)."""
    del kind  # the probe is keyed by the gated thing, not by how it was gated
    probe = probe_for(scope_key)
    if probe is None:
        return ObservationResult(
            result=CheckResult.NOT_OBSERVABLE,
            samples=0,
            detail=(
                f"no observable artefact is mapped for '{scope_key}', so the effect "
                "of this control cannot be confirmed from telemetry"
            ),
        )
    table, label = probe
    count = await count_artifacts_since(session, table, project_id, since)
    if count is None:  # pragma: no cover - guarded by the allow-list
        return ObservationResult(
            result=CheckResult.NOT_OBSERVABLE,
            samples=0,
            detail=f"no probe available for {scope_key}",
        )
    return ObservationResult(
        result=CheckResult.PASS,
        observed=float(count),
        samples=count,
        detail=f"{count} {label} since the control was applied",
        source=table,
    )


def now_window(seconds: int) -> tuple[datetime, datetime]:
    """A ``(start, end)`` window ending now."""
    end = aware(utcnow())
    from datetime import timedelta

    return end - timedelta(seconds=max(0, seconds)), end


__all__ = [
    "HealthSnapshot",
    "MetricSnapshot",
    "ObservationResult",
    "control_effect_observation",
    "count_anomalies_since",
    "count_artifacts_since",
    "count_incidents_since",
    "error_rate",
    "health_snapshot",
    "latency_snapshot",
    "metric_snapshot",
    "now_window",
    "probe_for",
    "span_error_ratio",
]
