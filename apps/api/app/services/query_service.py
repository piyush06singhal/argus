"""ARGUS Observability Query Service.

Centralizes richer read paths used by the observability API (§22, §32):
cross-source search over logs/metrics/traces/events with time-bucketed
aggregates, and trace-heatmap helpers for the trace browser.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import Text, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.observability import (
    EventType,
    LogRecord,
    MetricRecord,
    ObservabilityEvent,
    Severity,
    TraceRecord,
)


class ObservabilityQueryService:
    """Read-model accessors for the observability explorer."""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def search_events(
        self,
        *,
        project_id: Optional[uuid.UUID] = None,
        query: Optional[str] = None,
        event_type: Optional[EventType] = None,
        source: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[List[ObservabilityEvent], int]:
        """Full-text-ish search over normalized events with filters."""
        stmt = select(ObservabilityEvent)
        count = select(func.count(ObservabilityEvent.id))

        if project_id:
            stmt = stmt.where(ObservabilityEvent.project_id == project_id)
            count = count.where(ObservabilityEvent.project_id == project_id)
        if query:
            like = f"%{query}%"
            stmt = stmt.where(
                cast(ObservabilityEvent.payload, Text).ilike(like)
            )
            count = count.where(
                cast(ObservabilityEvent.payload, Text).ilike(like)
            )
        if event_type:
            stmt = stmt.where(ObservabilityEvent.event_type == event_type)
            count = count.where(ObservabilityEvent.event_type == event_type)
        if source:
            stmt = stmt.where(ObservabilityEvent.source == source)
            count = count.where(ObservabilityEvent.source == source)
        if start_time:
            stmt = stmt.where(ObservabilityEvent.timestamp >= start_time)
            count = count.where(ObservabilityEvent.timestamp >= start_time)
        if end_time:
            stmt = stmt.where(ObservabilityEvent.timestamp <= end_time)
            count = count.where(ObservabilityEvent.timestamp <= end_time)

        total = (await self._db.execute(count)).scalar() or 0
        stmt = (
            stmt.order_by(ObservabilityEvent.timestamp.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = (await self._db.execute(stmt)).scalars().all()
        return list(items), total

    async def aggregate_counts(
        self,
        *,
        project_id: uuid.UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> dict[str, int]:
        """Counts of normalized telemetry by type for a time range."""
        buckets: dict[str, int] = {}
        for model, name in [
            (LogRecord, "logs"),
            (MetricRecord, "metrics"),
            (TraceRecord, "traces"),
            (ObservabilityEvent, "events"),
        ]:
            count = (
                await self._db.execute(
                    select(func.count(model.id)).where(
                        model.project_id == project_id,
                        model.timestamp >= start_time,
                        model.timestamp <= end_time,
                    )
                )
            ).scalar()
            buckets[name] = count or 0
        return buckets

    async def severity_distribution(
        self,
        *,
        project_id: Optional[uuid.UUID] = None,
        start_time: datetime,
        end_time: datetime,
    ) -> dict[str, int]:
        """Count of events per severity in a time range."""
        stmt = (
            select(
                ObservabilityEvent.severity,
                func.count(ObservabilityEvent.id),
            )
            .where(
                ObservabilityEvent.timestamp >= start_time,
                ObservabilityEvent.timestamp <= end_time,
            )
            .group_by(ObservabilityEvent.severity)
        )
        if project_id:
            stmt = stmt.where(ObservabilityEvent.project_id == project_id)

        distribution: dict[str, int] = {}
        for severity, count in (await self._db.execute(stmt)).all():
            key = severity.value if hasattr(severity, "value") else str(severity or "UNKNOWN")
            distribution[key] = count
        # Normalize logs too.
        log_stmt = (
            select(LogRecord.level, func.count(LogRecord.id))
            .where(
                LogRecord.timestamp >= start_time,
                LogRecord.timestamp <= end_time,
            )
            .group_by(LogRecord.level)
        )
        if project_id:
            log_stmt = log_stmt.where(LogRecord.project_id == project_id)
        for level, count in (await self._db.execute(log_stmt)).all():
            key = level.value if hasattr(level, "value") else str(level or "UNKNOWN")
            distribution[key] = distribution.get(key, 0) + count

        for severity in Severity:
            distribution.setdefault(severity.value, 0)
        return distribution

    async def recent_traces(
        self,
        *,
        project_id: Optional[uuid.UUID] = None,
        limit: int = 50,
    ) -> List[TraceRecord]:
        """Most recent traces (optionally project-scoped)."""
        stmt = select(TraceRecord).order_by(TraceRecord.start_time.desc()).limit(limit)
        if project_id:
            stmt = stmt.where(TraceRecord.project_id == project_id)
        return list((await self._db.execute(stmt)).scalars().all())