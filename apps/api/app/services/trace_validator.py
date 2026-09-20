"""ARGUS Trace Cross-Reference Validation.

Validates trace integrity by detecting orphan spans (spans whose parent
references a non-existent span or whose trace has no matching TraceRecord),
broken parent-child chains, and duration anomalies.

Phase 1 §20: orphan span handling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Set
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.observability import SpanRecord, TraceRecord

logger = logging.getLogger(__name__)


@dataclass
class OrphanSpan:
    """A span whose parent_span_id references a missing span."""

    span_id: str
    trace_id: str
    parent_span_id: str
    operation: Optional[str] = None


@dataclass
class TraceValidationResult:
    """Outcome of validating a single trace."""

    trace_id: str
    total_spans: int = 0
    orphan_spans: List[OrphanSpan] = field(default_factory=list)
    missing_trace_record: bool = False
    duration_anomalies: List[str] = field(default_factory=list)
    is_valid: bool = True

    def revalidate(self) -> None:
        """Recompute ``is_valid`` based on current field values.

        Call after mutating orphan_spans / missing_trace_record /
        duration_anomalies, since ``__post_init__`` only fires once at
        construction time (when those fields are still empty).
        """
        if self.orphan_spans or self.missing_trace_record or self.duration_anomalies:
            self.is_valid = False
        else:
            self.is_valid = True


@dataclass
class ValidationSummary:
    """Aggregated results across multiple traces."""

    traces_checked: int = 0
    valid_traces: int = 0
    traces_with_orphans: int = 0
    total_orphan_spans: int = 0
    missing_trace_records: int = 0
    duration_anomalies: int = 0


class TraceValidator:
    """Validates trace/span cross-references in the database.

    Usage::

        validator = TraceValidator(db)
        result = await validator.validate_trace("abc-123")
        summary = await validator.validate_project(project_id)
    """

    def __init__(self, db: AsyncSession):
        self._db = db

    async def validate_trace(self, trace_id: str) -> TraceValidationResult:
        """Validate all spans within a single trace."""
        result = TraceValidationResult(trace_id=trace_id)

        # Check if the trace record exists
        trace_q = await self._db.execute(
            select(TraceRecord).where(TraceRecord.trace_id == trace_id)
        )
        trace = trace_q.scalar_one_or_none()
        if trace is None:
            result.missing_trace_record = True

        # Fetch all spans for this trace
        spans_q = await self._db.execute(
            select(SpanRecord).where(SpanRecord.trace_id == trace_id)
        )
        spans = spans_q.scalars().all()
        result.total_spans = len(spans)

        if not spans:
            return result

        # Build lookup: span_id → SpanRecord
        span_ids: Set[str] = {s.span_id for s in spans}

        # Detect orphan spans
        for span in spans:
            if span.parent_span_id and span.parent_span_id not in span_ids:
                result.orphan_spans.append(
                    OrphanSpan(
                        span_id=span.span_id,
                        trace_id=span.trace_id,
                        parent_span_id=span.parent_span_id,
                        operation=span.operation,
                    )
                )

        # Duration anomaly: a span whose duration exceeds the trace duration
        if trace and trace.duration_ms is not None:
            for span in spans:
                if (
                    span.duration_ms is not None
                    and span.duration_ms > trace.duration_ms * 1.1
                ):
                    result.duration_anomalies.append(
                        f"Span {span.span_id} duration {span.duration_ms:.1f}ms "
                        f"exceeds trace duration {trace.duration_ms:.1f}ms"
                    )

        # Duration anomaly: span end before start
        for span in spans:
            if span.start_time and span.end_time and span.end_time < span.start_time:
                result.duration_anomalies.append(
                    f"Span {span.span_id} ends before it starts"
                )

        # Recompute is_valid now that all mutations are complete.
        result.revalidate()
        return result

    async def validate_project(
        self, project_id: UUID, *, limit: int = 500
    ) -> ValidationSummary:
        """Validate all traces for a project (bounded by limit)."""
        summary = ValidationSummary()

        traces_q = await self._db.execute(
            select(TraceRecord)
            .where(TraceRecord.project_id == project_id)
            .order_by(TraceRecord.start_time.desc())
            .limit(limit)
        )
        traces = traces_q.scalars().all()

        for trace in traces:
            summary.traces_checked += 1
            result = await self.validate_trace(trace.trace_id)
            if result.is_valid:
                summary.valid_traces += 1
            if result.orphan_spans:
                summary.traces_with_orphans += 1
                summary.total_orphan_spans += len(result.orphan_spans)
            if result.missing_trace_record:
                summary.missing_trace_records += 1
            summary.duration_anomalies += len(result.duration_anomalies)

        return summary

    async def find_orphan_spans(
        self, project_id: UUID, *, limit: int = 100
    ) -> List[OrphanSpan]:
        """Find all orphan spans in a project (span whose parent doesn't exist)."""
        # Fetch all span_ids in the project
        spans_q = await self._db.execute(
            select(SpanRecord).where(SpanRecord.project_id == project_id).limit(5000)
        )
        spans = spans_q.scalars().all()

        all_span_ids: Set[str] = {s.span_id for s in spans}
        orphans: List[OrphanSpan] = []

        for span in spans:
            if span.parent_span_id and span.parent_span_id not in all_span_ids:
                orphans.append(
                    OrphanSpan(
                        span_id=span.span_id,
                        trace_id=span.trace_id,
                        parent_span_id=span.parent_span_id,
                        operation=span.operation,
                    )
                )
                if len(orphans) >= limit:
                    break

        return orphans
