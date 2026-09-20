"""ARGUS Ingestion Stats & Health Service (§45).

Aggregates live health of registered observability sources plus dead-letter
and throughput counters for the ingestion health surface.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ingestion import (
    IngestionFailure,
    ObservabilitySource,
    ObservabilitySourceStatus,
)
from app.models.observability import ObservabilityEvent


class IngestionStatsService:
    """Compute ingestion health/statistics from persisted state."""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def list_sources(
        self, *, project_id: Optional[uuid.UUID] = None
    ) -> list[ObservabilitySource]:
        stmt = select(ObservabilitySource).order_by(ObservabilitySource.name)
        if project_id:
            stmt = stmt.where(ObservabilitySource.project_id == project_id)
        return list((await self._db.execute(stmt)).scalars().all())

    async def source_summary(
        self,
        *,
        project_id: Optional[uuid.UUID] = None,
        days: int = 7,
    ) -> dict[str, object]:
        """Overall ingestion health summary."""
        sources = await self.list_sources(project_id=project_id)
        since = datetime.utcnow() - timedelta(days=days)

        status_counts: dict[str, int] = {}
        for source in sources:
            key = (
                source.status.value
                if hasattr(source.status, "value")
                else str(source.status)
            )
            status_counts[key] = status_counts.get(key, 0) + 1

        # Dead-letter and throughput counts, optionally project-scoped.
        dl_stmt = select(func.count(IngestionFailure.id))
        if project_id:
            dl_stmt = dl_stmt.where(IngestionFailure.project_id == project_id)
        dead_letter_count = (await self._db.execute(dl_stmt)).scalar() or 0

        ev_stmt = select(func.count(ObservabilityEvent.id)).where(
            ObservabilityEvent.ingested_at >= since
        )
        if project_id:
            ev_stmt = ev_stmt.where(ObservabilityEvent.project_id == project_id)
        events_ingested = (await self._db.execute(ev_stmt)).scalar() or 0

        return {
            "source_count": len(sources),
            "status_counts": status_counts,
            "dead_letter_count": dead_letter_count,
            "events_ingested_7d": events_ingested,
            "healthy_sources": status_counts.get(
                ObservabilitySourceStatus.HEALTHY.value, 0
            ),
            "failing_sources": status_counts.get(
                ObservabilitySourceStatus.FAILING.value, 0
            ),
        }

    async def dead_letter_list(
        self,
        *,
        project_id: Optional[uuid.UUID] = None,
        limit: int = 50,
    ) -> list[IngestionFailure]:
        stmt = (
            select(IngestionFailure)
            .order_by(IngestionFailure.failed_at.desc())
            .limit(limit)
        )
        if project_id:
            stmt = stmt.where(IngestionFailure.project_id == project_id)
        return list((await self._db.execute(stmt)).scalars().all())

    async def top_sources(
        self,
        *,
        project_id: uuid.UUID,
        days: int = 7,
    ) -> list[dict[str, object]]:
        """Per-source event volume and health, for the ingestion center."""
        since = datetime.utcnow() - timedelta(days=days)
        sources = await self.list_sources(project_id=project_id)

        rows: list[dict[str, object]] = []
        for source in sources:
            events = (
                await self._db.execute(
                    select(func.count(ObservabilityEvent.id)).where(
                        ObservabilityEvent.project_id == project_id,
                        ObservabilityEvent.source_id == str(source.id),
                        ObservabilityEvent.ingested_at >= since,
                    )
                )
            ).scalar() or 0
            rows.append(
                {
                    "id": str(source.id),
                    "name": source.name,
                    "source_type": source.source_type.value
                    if hasattr(source.source_type, "value")
                    else str(source.source_type),
                    "status": source.status.value
                    if hasattr(source.status, "value")
                    else str(source.status),
                    "events_7d": events,
                    "error_count": source.error_count,
                    "consecutive_errors": source.consecutive_errors,
                    "last_success_at": source.last_success_at,
                }
            )
        return rows
