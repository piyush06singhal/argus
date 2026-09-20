"""ARGUS Trace Analyzer (Phase 4 §15, §16).

Extracts *directional* causal evidence from stored span trees — the strongest
evidence source Phase 1 provides, because a span carries an explicit parent:
a failing child span inside a parent span is a stored record of "the call this
parent made failed", which is far better than temporal proximity.

What this module reports (and never claims):

* ``TraceFailureEdge`` — parent component → child component with the failing
  child span and the error latency. This is evidence that the *call* failed,
  which supports (never proves) "child's failure contributed to parent's".
* ``PropagationObservation`` — ordered failure onset across components within
  single traces, giving per-request propagation delays.

Every produced row names its ``trace_id``/``span_id``/``parent_span_id`` so the
explanation API can quote the exact stored record. Components with no span
records contribute nothing — missing evidence is reported as missing, not
invented (§ constraint 8/9).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc_or_now
from app.models.observability import SpanRecord, TraceStatus

_FAILURE_STATUSES = {TraceStatus.ERROR, TraceStatus.TIMEOUT}


@dataclass(frozen=True)
class SpanView:
    """Normalized span for analysis (no ORM object escapes this module)."""

    span_id: str
    parent_span_id: Optional[str]
    trace_id: str
    component_id: Optional[uuid.UUID]
    operation: Optional[str]
    start_time: datetime
    end_time: Optional[datetime]
    duration_ms: Optional[float]
    failed: bool

    @property
    def start(self) -> datetime:
        return ensure_utc_or_now(self.start_time)

    @property
    def failed_at(self) -> datetime:
        """When this span's failure *concluded* — the causally sound instant.

        A span's start time says when the call began, which for nested calls is
        the opposite of the causal order (a caller starts before the dependency
        it waits on). A failure can only conclude after the child work it was
        waiting for finished, so ordering by ``end_time`` puts the innermost
        failure first — which is exactly the propagation direction. Falls back
        to the start when a span carries no end time.
        """
        return ensure_utc_or_now(self.end_time or self.start_time)


@dataclass(frozen=True)
class TraceFailureEdge:
    """A failing child call observed inside a parent span (§15)."""

    trace_id: str
    parent_span_id: str
    child_span_id: str
    parent_component_id: Optional[uuid.UUID]
    child_component_id: Optional[uuid.UUID]
    child_operation: Optional[str]
    child_status: str
    child_duration_ms: Optional[float]
    observed_at: datetime

    @property
    def observation(self) -> str:
        status = self.child_status or "FAILED"
        duration = (
            f" after {int(self.child_duration_ms)}ms" if self.child_duration_ms else ""
        )
        return (
            f"Trace {self.trace_id}: {status} span {self.child_span_id}"
            f"{duration} inside parent span {self.parent_span_id}"
        )


@dataclass(frozen=True)
class PropagationObservation:
    """Failure onset ordering across components within one trace (§16)."""

    trace_id: str
    #: (component_id, first-failure instant) ordered by time.
    failure_order: list[tuple[Optional[uuid.UUID], datetime]] = field(
        default_factory=list
    )

    @property
    def delays(self) -> list[int]:
        """Seconds between consecutive failure onsets in this trace."""
        stamps = [t for _c, t in self.failure_order]
        return [int((b - a).total_seconds()) for a, b in zip(stamps, stamps[1:])]

    @property
    def is_propagation_shaped(self) -> bool:
        """More than one distinct component failing in sequence in one request."""
        components = {c for c, _t in self.failure_order if c is not None}
        return len(components) > 1


@dataclass
class TraceAnalysisResult:
    edges: list[TraceFailureEdge] = field(default_factory=list)
    propagations: list[PropagationObservation] = field(default_factory=list)
    #: True when the window contained traces but none had usable spans —
    #: reported as missing evidence, never silently ignored.
    traces_without_spans: int = 0

    @property
    def has_directional_evidence(self) -> bool:
        return bool(self.edges or self.propagations)


class TraceAnalyzer:
    """Reads bounded trace windows and derives directional evidence."""

    def __init__(self, session: AsyncSession, *, max_traces: int = 100) -> None:
        self._session = session
        self._max_traces = max(1, int(max_traces))

    async def analyze_window(
        self,
        project_id: uuid.UUID,
        *,
        environment_id: uuid.UUID | None,
        start: datetime,
        end: datetime,
        component_ids: set[uuid.UUID] | None = None,
    ) -> TraceAnalysisResult:
        """Analyze failing traces in ``[start, end]`` (exact scope, like Phase 3).

        Scope discipline: ``environment_id=None`` reads environment-less traces
        only — the Phase 4 rule matches Phase 3's exact-scope decision.
        """
        result = TraceAnalysisResult()
        lo, hi = ensure_utc_or_now(start), ensure_utc_or_now(end)

        failing_traces = await self._failing_trace_ids(
            project_id, environment_id=environment_id, start=lo, end=hi
        )
        if not failing_traces:
            return result

        spans = await self._load_spans(
            project_id, environment_id=environment_id, trace_ids=failing_traces
        )
        if not spans:
            result.traces_without_spans = len(failing_traces)
            return result

        by_trace: dict[str, list[SpanView]] = defaultdict(list)
        for span in spans:
            if component_ids is not None and span.component_id not in component_ids:
                # Spans of unrelated components stay out of the candidate story.
                continue
            by_trace[span.trace_id].append(span)

        for trace_id, trace_spans in by_trace.items():
            result.edges.extend(self._failure_edges(trace_spans))
            propagation = self._propagation(trace_id, trace_spans)
            if propagation is not None:
                result.propagations.append(propagation)
        return result

    # -- Loading ------------------------------------------------------------
    async def _failing_trace_ids(
        self,
        project_id: uuid.UUID,
        *,
        environment_id: uuid.UUID | None,
        start: datetime,
        end: datetime,
    ) -> list[str]:
        from app.models.observability import TraceRecord

        stmt = select(TraceRecord.trace_id).where(
            TraceRecord.project_id == project_id,
            TraceRecord.status.in_(_FAILURE_STATUSES),
            TraceRecord.start_time >= start,
            TraceRecord.start_time <= end,
        )
        # Exact environment scope (§46 / Phase 3 parity).
        stmt = stmt.where(
            TraceRecord.environment_id == environment_id
            if environment_id is not None
            else TraceRecord.environment_id.is_(None)
        )
        stmt = stmt.order_by(TraceRecord.start_time.desc()).limit(self._max_traces)
        return list((await self._session.execute(stmt)).scalars().all())

    async def _load_spans(
        self,
        project_id: uuid.UUID,
        *,
        environment_id: uuid.UUID | None,
        trace_ids: Sequence[str],
    ) -> list[SpanView]:
        if not trace_ids:
            return []
        stmt = select(SpanRecord).where(
            SpanRecord.project_id == project_id,
            SpanRecord.trace_id.in_(list(trace_ids)),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        views: list[SpanView] = []
        for row in rows:
            views.append(
                SpanView(
                    span_id=row.span_id,
                    parent_span_id=row.parent_span_id,
                    trace_id=row.trace_id,
                    component_id=row.component_id,
                    operation=row.operation,
                    start_time=row.start_time,
                    end_time=row.end_time,
                    duration_ms=row.duration_ms,
                    failed=row.status in _FAILURE_STATUSES,
                )
            )
        return views

    # -- Derivation ---------------------------------------------------------
    @staticmethod
    def _failure_edges(spans: Sequence[SpanView]) -> list[TraceFailureEdge]:
        by_id = {s.span_id: s for s in spans}
        edges: list[TraceFailureEdge] = []
        for span in spans:
            if not span.failed:
                continue
            parent = by_id.get(span.parent_span_id) if span.parent_span_id else None
            if parent is None:
                continue
            edges.append(
                TraceFailureEdge(
                    trace_id=span.trace_id,
                    parent_span_id=parent.span_id,
                    child_span_id=span.span_id,
                    parent_component_id=parent.component_id,
                    child_component_id=span.component_id,
                    child_operation=span.operation,
                    child_status="FAILED",
                    child_duration_ms=span.duration_ms,
                    observed_at=span.start,
                )
            )
        edges.sort(key=lambda e: (e.observed_at, e.child_span_id))
        return edges

    @staticmethod
    def _propagation(
        trace_id: str, spans: Sequence[SpanView]
    ) -> Optional[PropagationObservation]:
        failing = sorted((s for s in spans if s.failed), key=lambda s: s.failed_at)
        if not failing:
            return None
        onset: dict[Optional[uuid.UUID], datetime] = {}
        for span in failing:
            current = onset.get(span.component_id)
            if current is None or span.failed_at < current:
                onset[span.component_id] = span.failed_at
        ordered = sorted(onset.items(), key=lambda kv: (kv[1], str(kv[0])))
        return PropagationObservation(
            trace_id=trace_id,
            failure_order=ordered,
        )


__all__ = [
    "PropagationObservation",
    "SpanView",
    "TraceAnalysisResult",
    "TraceAnalyzer",
    "TraceFailureEdge",
]
