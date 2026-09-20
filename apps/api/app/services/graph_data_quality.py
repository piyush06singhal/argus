"""ARGUS Graph Data-Quality Service.

Runs the graph validator, persists every finding as a
``GraphDataQualityRecord``, and aggregates a project health summary
(``GraphHealthResponse``). ``ok`` is True unless an ``ERROR``-severity record
exists for the project.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import (
    DataQualitySeverity,
    GraphDataQualityRecord,
    GraphEdge,
    GraphNode,
    GraphReconciliationRun,
    ReconciliationStatus,
)
from app.schemas.graph import (
    GraphHealthResponse,
    GraphHealthRow,
)
from app.services.graph_validator import GraphValidator


class GraphDataQualityService:
    """Validator orchestration + health aggregation for a project graph."""

    def __init__(self, db: AsyncSession, validator: GraphValidator) -> None:
        self._db = db
        self._validator = validator

    # ------------------------------------------------------------------
    # Write path: run checks and persist findings
    # ------------------------------------------------------------------
    async def run_and_get_health(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
    ) -> GraphHealthResponse:
        issues = await self._validator.run_checks(
            project_id=project_id, environment_id=environment_id
        )
        for issue in issues:
            self._db.add(
                GraphDataQualityRecord(
                    project_id=project_id,
                    environment_id=environment_id,
                    check_type=issue.check_type,
                    severity=issue.severity,
                    detail=issue.detail,
                )
            )
        await self._db.flush()
        return await self.current_health(project_id=project_id)

    # ------------------------------------------------------------------
    # Read path: no checks run
    # ------------------------------------------------------------------
    async def current_health(self, *, project_id: uuid.UUID) -> GraphHealthResponse:
        """Aggregate existing records into a health summary (read-only)."""
        node_count = (
            await self._db.execute(
                select(func.count())
                .select_from(GraphNode)
                .where(GraphNode.project_id == project_id)
            )
        ).scalar() or 0
        edge_count = (
            await self._db.execute(
                select(func.count())
                .select_from(GraphEdge)
                .where(GraphEdge.project_id == project_id)
            )
        ).scalar() or 0

        run = (
            await self._db.execute(
                select(GraphReconciliationRun)
                .where(GraphReconciliationRun.project_id == project_id)
                .order_by(GraphReconciliationRun.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        last_reconciled_at = (
            run.finished_at
            if (run and run.status == ReconciliationStatus.SUCCESS)
            else None
        )

        agg_rows = (
            await self._db.execute(
                select(
                    GraphDataQualityRecord.check_type,
                    GraphDataQualityRecord.severity,
                    func.count(GraphDataQualityRecord.id).label("count"),
                    func.max(GraphDataQualityRecord.detected_at).label("latest"),
                )
                .where(GraphDataQualityRecord.project_id == project_id)
                .group_by(
                    GraphDataQualityRecord.check_type,
                    GraphDataQualityRecord.severity,
                )
                .order_by(GraphDataQualityRecord.severity)
            )
        ).all()

        data_quality: List[GraphHealthRow] = [
            GraphHealthRow(
                check_type=check_type,
                severity=severity,
                count=count,
                latest_detected_at=latest,
            )
            for check_type, severity, count, latest in agg_rows
        ]
        has_error = any(
            row.severity == DataQualitySeverity.ERROR for row in data_quality
        )
        return GraphHealthResponse(
            project_id=project_id,
            node_count=node_count,
            edge_count=edge_count,
            last_reconciled_at=last_reconciled_at,
            data_quality=data_quality,
            ok=not has_error,
        )
