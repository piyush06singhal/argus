"""ARGUS Phase 3 Metrics & Dashboard Aggregation (§36, §44, §45).

Bounded, indexed aggregate queries — never a full-history scan — that power the
incident dashboard, the reliability metrics endpoint, and the Prometheus
exporter.

Terminology and arithmetic are explicit because "MTTR" means different things in
different tools:

* **MTTA** (mean time to acknowledge) = mean(``acknowledged_at - detected_at``)
  over incidents acknowledged in the window.
* **MTTR** (mean time to resolve) = mean(``resolved_at - detected_at``) over
  incidents resolved in the window — measured from *detection*, not from
  acknowledgement, and stated as such wherever it is reported.

Counts are counts of stored rows. Nothing here is estimated, modelled, or
AI-generated.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc, utcnow
from app.models.anomaly import (
    Anomaly,
    AnomalyFingerprint,
    AnomalyStatus,
)
from app.models.incident import Incident, IncidentStatus
from app.models.system import SystemComponent

#: Terminal anomaly statuses (evidence window closed or handled).
_ANOMALY_RESOLVED_STATUSES = (AnomalyStatus.RESOLVED, AnomalyStatus.EXPIRED)
#: Incident statuses that still need attention.
_INCIDENT_OPEN_STATUSES = (
    IncidentStatus.OPEN,
    IncidentStatus.ACKNOWLEDGED,
    IncidentStatus.INVESTIGATING,
)
#: Incident statuses considered closed for throughput/MTTR purposes.
_INCIDENT_DONE_STATUSES = (IncidentStatus.RESOLVED, IncidentStatus.CLOSED)

_DEFAULT_WINDOW_SECONDS = 86_400


def _scoped(query, column, project_id: Optional[uuid.UUID]):
    if project_id is not None:
        return query.where(column == project_id)
    return query


@dataclass
class ReliabilityMetrics:
    """Counts and durations for one scope and window (§44)."""

    project_id: Optional[str] = None
    environment_id: Optional[str] = None
    window_seconds: int = _DEFAULT_WINDOW_SECONDS
    generated_at: datetime = field(default_factory=utcnow)
    anomalies_detected: int = 0
    anomalies_open: int = 0
    anomalies_resolved: int = 0
    anomalies_suppressed: int = 0
    anomalies_deduplicated: int = 0
    anomalies_by_severity: dict = field(default_factory=dict)
    anomalies_by_type: dict = field(default_factory=dict)
    incidents_created: int = 0
    incidents_open: int = 0
    incidents_resolved: int = 0
    incidents_by_severity: dict = field(default_factory=dict)
    mtta_seconds: Optional[float] = None
    mttr_seconds: Optional[float] = None
    mttr_definition: str = (
        "mean(resolved_at - detected_at) over incidents resolved in the window"
    )
    mtta_definition: str = (
        "mean(acknowledged_at - detected_at) over incidents acknowledged in the window"
    )

    def as_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "environment_id": self.environment_id,
            "window_seconds": self.window_seconds,
            "generated_at": self.generated_at.isoformat(),
            "anomalies_detected": self.anomalies_detected,
            "anomalies_open": self.anomalies_open,
            "anomalies_resolved": self.anomalies_resolved,
            "anomalies_suppressed": self.anomalies_suppressed,
            "anomalies_deduplicated": self.anomalies_deduplicated,
            "anomalies_by_severity": self.anomalies_by_severity,
            "anomalies_by_type": self.anomalies_by_type,
            "incidents_created": self.incidents_created,
            "incidents_open": self.incidents_open,
            "incidents_resolved": self.incidents_resolved,
            "incidents_by_severity": self.incidents_by_severity,
            "mtta_seconds": self.mtta_seconds,
            "mttr_seconds": self.mttr_seconds,
            "mttr_definition": self.mttr_definition,
            "mtta_definition": self.mtta_definition,
        }


async def _group_counts(db: AsyncSession, query) -> dict[str, int]:
    return {
        (key.value if hasattr(key, "value") else str(key)): count
        for key, count in (await db.execute(query)).all()
    }


async def reliability_metrics(
    db: AsyncSession,
    *,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    window_seconds: int = _DEFAULT_WINDOW_SECONDS,
    now: Optional[datetime] = None,
) -> ReliabilityMetrics:
    """Compute the Phase 3 reliability metrics for a scope and window."""
    reference = now or utcnow()
    since = reference - timedelta(seconds=max(1, window_seconds))
    result = ReliabilityMetrics(
        project_id=str(project_id) if project_id else None,
        environment_id=str(environment_id) if environment_id else None,
        window_seconds=window_seconds,
        generated_at=reference,
    )

    anomaly_scope = [Anomaly.project_id == project_id] if project_id else []
    if environment_id is not None:
        anomaly_scope.append(Anomaly.environment_id == environment_id)
    incident_scope = [Incident.project_id == project_id] if project_id else []
    if environment_id is not None:
        incident_scope.append(Incident.environment_id == environment_id)

    # --- Anomalies ---------------------------------------------------------
    result.anomalies_detected = int(
        (
            await db.execute(
                select(func.count(Anomaly.id)).where(
                    *anomaly_scope, Anomaly.detected_at >= since
                )
            )
        ).scalar()
        or 0
    )
    result.anomalies_open = int(
        (
            await db.execute(
                select(func.count(Anomaly.id)).where(
                    *anomaly_scope, Anomaly.status.notin_(_ANOMALY_RESOLVED_STATUSES)
                )
            )
        ).scalar()
        or 0
    )
    result.anomalies_resolved = int(
        (
            await db.execute(
                select(func.count(Anomaly.id)).where(
                    *anomaly_scope,
                    Anomaly.status.in_(_ANOMALY_RESOLVED_STATUSES),
                    Anomaly.detected_at >= since,
                )
            )
        ).scalar()
        or 0
    )
    result.anomalies_suppressed = int(
        (
            await db.execute(
                select(func.count(Anomaly.id)).where(
                    *anomaly_scope,
                    Anomaly.suppressed.is_(True),
                    Anomaly.detected_at >= since,
                )
            )
        ).scalar()
        or 0
    )

    severity_query = select(Anomaly.severity, func.count(Anomaly.id)).where(
        *anomaly_scope, Anomaly.detected_at >= since
    )
    result.anomalies_by_severity = await _group_counts(
        db, severity_query.group_by(Anomaly.severity)
    )
    type_query = select(Anomaly.anomaly_type, func.count(Anomaly.id)).where(
        *anomaly_scope, Anomaly.detected_at >= since
    )
    result.anomalies_by_type = await _group_counts(
        db, type_query.group_by(Anomaly.anomaly_type)
    )

    # Deduplicated = detections beyond the first for each fingerprint.
    fingerprint_scope = (
        [AnomalyFingerprint.project_id == project_id] if project_id else []
    )
    if environment_id is not None:
        fingerprint_scope.append(AnomalyFingerprint.environment_id == environment_id)
    total_occurrences = int(
        (
            await db.execute(
                select(func.sum(AnomalyFingerprint.occurrence_count)).where(
                    *fingerprint_scope
                )
            )
        ).scalar()
        or 0
    )
    distinct_fingerprints = int(
        (
            await db.execute(
                select(func.count(AnomalyFingerprint.id)).where(*fingerprint_scope)
            )
        ).scalar()
        or 0
    )
    result.anomalies_deduplicated = max(0, total_occurrences - distinct_fingerprints)

    # --- Incidents ---------------------------------------------------------
    result.incidents_created = int(
        (
            await db.execute(
                select(func.count(Incident.id)).where(
                    *incident_scope, Incident.detected_at >= since
                )
            )
        ).scalar()
        or 0
    )
    result.incidents_open = int(
        (
            await db.execute(
                select(func.count(Incident.id)).where(
                    *incident_scope, Incident.status.in_(_INCIDENT_OPEN_STATUSES)
                )
            )
        ).scalar()
        or 0
    )
    result.incidents_resolved = int(
        (
            await db.execute(
                select(func.count(Incident.id)).where(
                    *incident_scope,
                    Incident.status.in_(_INCIDENT_DONE_STATUSES),
                    Incident.detected_at >= since,
                )
            )
        ).scalar()
        or 0
    )
    incident_severity_query = select(Incident.severity, func.count(Incident.id)).where(
        *incident_scope, Incident.detected_at >= since
    )
    result.incidents_by_severity = await _group_counts(
        db, incident_severity_query.group_by(Incident.severity)
    )

    # MTTA / MTTR — computed in Python over a bounded row set so the definition
    # stays inspectable rather than buried in dialect-specific timestamp maths.
    ack_rows = (
        await db.execute(
            select(Incident.detected_at, Incident.acknowledged_at).where(
                *incident_scope,
                Incident.acknowledged_at.isnot(None),
                Incident.acknowledged_at >= since,
            )
        )
    ).all()
    result.mtta_seconds = _mean_delta(ack_rows)

    resolve_rows = (
        await db.execute(
            select(Incident.detected_at, Incident.resolved_at).where(
                *incident_scope,
                Incident.resolved_at.isnot(None),
                Incident.resolved_at >= since,
            )
        )
    ).all()
    result.mttr_seconds = _mean_delta(resolve_rows)
    return result


def _mean_delta(rows: Sequence[Any]) -> Optional[float]:
    deltas: list[float] = []
    for row in rows:
        start, end = row[0], row[1]
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            continue
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        if start_utc is None or end_utc is None:
            continue
        seconds = (end_utc - start_utc).total_seconds()
        if seconds >= 0:
            deltas.append(seconds)
    if not deltas:
        return None
    return round(sum(deltas) / len(deltas), 3)


async def incident_dashboard(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = None,
    window_seconds: int = _DEFAULT_WINDOW_SECONDS,
    bucket_seconds: int = 3600,
    top_components: int = 10,
    now: Optional[datetime] = None,
) -> dict:
    """Aggregate the incident dashboard payload (§36).

    All series are bucketed server-side and bounded; the frontend never fetches
    a raw unbounded incident list to build a chart.
    """
    reference = now or utcnow()
    since = reference - timedelta(seconds=max(1, window_seconds))
    bucket = max(60, bucket_seconds)

    anomaly_scope = [Anomaly.project_id == project_id]
    incident_scope = [Incident.project_id == project_id]
    if environment_id is not None:
        anomaly_scope.append(Anomaly.environment_id == environment_id)
        incident_scope.append(Incident.environment_id == environment_id)

    metrics = await reliability_metrics(
        db,
        project_id=project_id,
        environment_id=environment_id,
        window_seconds=window_seconds,
        now=reference,
    )

    anomalies_over_time = await _bucket_series(
        db,
        column=Anomaly.detected_at,
        model_id=Anomaly.id,
        where=anomaly_scope + [Anomaly.detected_at >= since],
        since=since,
        until=reference,
        bucket_seconds=bucket,
    )
    incidents_over_time = await _bucket_series(
        db,
        column=Incident.detected_at,
        model_id=Incident.id,
        where=incident_scope + [Incident.detected_at >= since],
        since=since,
        until=reference,
        bucket_seconds=bucket,
    )

    component_rows = (
        await db.execute(
            select(
                Anomaly.component_id,
                func.count(Anomaly.id).label("count"),
                func.max(Anomaly.severity).label("max_severity"),
            )
            .where(
                *anomaly_scope,
                Anomaly.detected_at >= since,
                Anomaly.component_id.isnot(None),
            )
            .group_by(Anomaly.component_id)
            .order_by(func.count(Anomaly.id).desc())
            .limit(top_components)
        )
    ).all()
    component_ids = [row[0] for row in component_rows if row[0] is not None]
    names: dict[uuid.UUID, str] = {}
    if component_ids:
        names = {
            cid: name
            for cid, name in (
                await db.execute(
                    select(SystemComponent.id, SystemComponent.name).where(
                        SystemComponent.id.in_(component_ids)
                    )
                )
            ).all()
        }
    top = [
        {
            "component_id": row[0],
            "name": names.get(row[0]),
            "anomaly_count": row[1],
            "max_severity": (row[2].value if hasattr(row[2], "value") else str(row[2])),
        }
        for row in component_rows
    ]

    recent_incidents = list(
        (
            await db.execute(
                select(Incident)
                .where(*incident_scope)
                .order_by(Incident.detected_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    recent_anomalies = list(
        (
            await db.execute(
                select(Anomaly)
                .where(*anomaly_scope)
                .order_by(Anomaly.detected_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )

    return {
        "metrics": metrics.as_dict(),
        "anomalies_over_time": anomalies_over_time,
        "incidents_over_time": incidents_over_time,
        "severity_distribution": metrics.anomalies_by_severity,
        "anomaly_categories": metrics.anomalies_by_type,
        "top_affected_components": top,
        "recent_incidents": [
            {
                "id": i.id,
                "title": i.title,
                "severity": i.severity.value
                if hasattr(i.severity, "value")
                else str(i.severity),
                "status": i.status.value
                if hasattr(i.status, "value")
                else str(i.status),
                "detected_at": i.detected_at,
                "primary_component_id": i.primary_component_id,
            }
            for i in recent_incidents
        ],
        "recent_anomalies": [
            {
                "id": a.id,
                "anomaly_type": a.anomaly_type.value
                if hasattr(a.anomaly_type, "value")
                else str(a.anomaly_type),
                "severity": a.severity.value
                if hasattr(a.severity, "value")
                else str(a.severity),
                "status": a.status.value
                if hasattr(a.status, "value")
                else str(a.status),
                "component_id": a.component_id,
                "detected_at": a.detected_at,
                "suppressed": a.suppressed,
            }
            for a in recent_anomalies
        ],
        "window_seconds": window_seconds,
        "bucket_seconds": bucket,
        "generated_at": reference.isoformat(),
    }


async def _bucket_series(
    db: AsyncSession,
    *,
    column,
    model_id,
    where: Sequence,
    since: datetime,
    until: datetime,
    bucket_seconds: int,
) -> list[dict]:
    """Bucket counts by time in Python over a bounded row set.

    Dialect-neutral on purpose (SQLite in tests, PostgreSQL in production), and
    the window is always bounded so this can never be an unbounded scan.
    """
    rows = (
        await db.execute(select(column, model_id).where(*where).limit(50_000))
    ).all()
    buckets: dict[int, int] = {}
    span = int((until - since).total_seconds())
    steps = span // bucket_seconds + 1
    for i in range(steps):
        buckets[int(since.timestamp()) + i * bucket_seconds] = 0
    for timestamp, _ in rows:
        if timestamp is None:
            continue
        value = ensure_utc(timestamp)
        if value is None:
            continue
        key = int(value.timestamp()) - (int(value.timestamp()) % bucket_seconds)
        if key in buckets:
            buckets[key] += 1
    return [
        {
            "bucket_start": datetime.fromtimestamp(key, tz=UTC),
            "count": count,
        }
        for key, count in sorted(buckets.items())
    ]


__all__ = [
    "ReliabilityMetrics",
    "reliability_metrics",
    "incident_dashboard",
]
