"""ARGUS Debug Context Builder (Phase 6 §21, §22).

Assembles the structured investigation the AI debugger reasons over, and — just
as importantly — records *what it left out*.

Three properties, each of which is a requirement rather than a nicety:

**Deterministic.** The same incident and snapshot produce byte-identical context.
Selection is by explicit priority order and stable tie-breaking, never by set or
dict iteration order, so an analysis is reproducible and a diff between two runs
means something.

**Bounded.** Every section has a limit and the whole thing has a byte budget
(``DEBUG_MAX_CONTEXT_BYTES``). When the budget runs out, lower-priority sections
are dropped *and reported* in ``budget.dropped``. Silently truncating would let an
analysis claim to have considered evidence it never saw.

**Evidence-indexed.** Every fact gets an id (``E1``, ``E2``, …) and a canonical
reference (``TRACE:abc``, ``COMMIT:4e50a182``, ``FILE:app/x.py:10-20``). The
model cites ids; the validator resolves them back to the stored row (§29, §30).
An id that cannot be resolved is a rejected claim, not a displayed one.

Source code and log text are **untrusted data** (§58). They are redacted (§57)
and placed inside explicitly delimited blocks that the prompt tells the model to
treat as data — never as instructions.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.anomaly import Anomaly
from app.models.causal import (
    CausalAnalysis,
    CausalEvidence,
    RootCauseCandidate,
)
from app.models.code import (
    CodeFile,
    CodeRelationshipType,
    CodeRiskSignal,
    CodeSymbol,
    RepositorySnapshot,
    TraceCodeMapping,
)
from app.models.deployment import CodeRepository, DeploymentEvent
from app.models.incident import Incident, IncidentEvidence, IncidentTimelineEvent
from app.models.observability import LogRecord, MetricRecord, SpanRecord, TraceStatus
from app.models.reproduction import (
    ReproductionComparison,
    ReproductionExperiment,
    ReproductionValidation,
)
from app.services.change_relevance import ChangeRelevanceAnalyzer
from app.services.code_query_service import CodeKnowledgeService
from app.services.source_redaction import RedactionReport, default_redactor
from app.services.trace_code_mapper import (
    StackFrame,
    StackTraceAnalyzer,
    match_frame_to_snapshot,
)

logger = logging.getLogger(__name__)
settings = get_settings()

#: Bumped when the *selection rules* change, not when limits change. Recorded on
#: every analysis run so an old analysis can be identified as produced by an older
#: policy (§59).
CONTEXT_VERSION = "1"

#: Deterministic section order, most decisive first. This ordering IS the priority
#: policy: when the byte budget binds, the tail of this list is what is dropped,
#: and it is dropped in a fixed order rather than by accident.
SECTION_ORDER = (
    "incident",
    "causal_analysis",
    "reproduction",
    "trace_path",
    "stack_traces",
    "code_locations",
    "recent_changes",
    "risk_signals",
    "telemetry_excerpts",
    "recurrence",
)


@dataclass
class ContextEvidence:
    """One indexed fact, with the reference string a claim must cite."""

    id: str
    kind: str
    reference: str
    label: str
    detail: str = ""
    source_table: Optional[str] = None
    source_id: Optional[str] = None
    component_id: Optional[str] = None
    observed_at: Optional[str] = None
    #: Raw code/telemetry kept for the prompt once redaction has run.
    excerpt: Optional[str] = None

    def as_dict(self) -> dict:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "reference": self.reference,
            "label": self.label,
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.observed_at:
            payload["observed_at"] = self.observed_at
        if self.excerpt:
            payload["excerpt"] = self.excerpt
        return payload


@dataclass
class ContextBudget:
    """What the budget allowed, and what it refused."""

    max_bytes: int
    bytes_used: int = 0
    dropped: list[str] = field(default_factory=list)
    truncated_sections: list[str] = field(default_factory=list)
    limits: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "max_bytes": self.max_bytes,
            "bytes_used": self.bytes_used,
            "dropped": list(self.dropped),
            "truncated_sections": list(self.truncated_sections),
            "limits": dict(self.limits),
        }


@dataclass
class DebugContext:
    """The complete, redacted investigation handed to a model (or a human)."""

    incident_id: str
    project_id: str
    snapshot_id: Optional[str]
    version_status: str
    version_note: str
    sections: dict = field(default_factory=dict)
    evidence: list[ContextEvidence] = field(default_factory=list)
    budget: Optional[ContextBudget] = None
    redaction: Optional[dict] = None
    context_version: str = CONTEXT_VERSION
    built_at: Optional[str] = None
    #: Deterministic notes that must reach the reader regardless of the model:
    #: missing evidence, version uncertainty, ambiguous mappings.
    caveats: list[str] = field(default_factory=list)

    def evidence_index(self) -> dict[str, ContextEvidence]:
        return {item.id: item for item in self.evidence}

    def for_prompt(self) -> dict:
        """The JSON payload handed to the model (redacted, budgeted)."""
        return {
            "context_version": self.context_version,
            "incident_id": self.incident_id,
            "code_version": {
                "snapshot_id": self.snapshot_id,
                "status": self.version_status,
                "note": self.version_note,
            },
            "sections": self.sections,
            "evidence": [item.as_dict() for item in self.evidence],
            "caveats": list(self.caveats),
            "budget": self.budget.as_dict() if self.budget else None,
        }

    def summarise(self) -> dict:
        """The deterministic view shown to a human when no model is available."""
        return {
            "context_version": self.context_version,
            "snapshot_id": self.snapshot_id,
            "version_status": self.version_status,
            "version_note": self.version_note,
            "evidence": [item.as_dict() for item in self.evidence],
            "caveats": list(self.caveats),
            "budget": self.budget.as_dict() if self.budget else None,
            "redaction": self.redaction,
        }


class DebugContextBuilder:
    """Builds a :class:`DebugContext` for one incident and snapshot."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.knowledge = CodeKnowledgeService(session)
        self.stack_analyzer = StackTraceAnalyzer()
        self.redactor = default_redactor
        #: Set during ``build`` so frame paths can be matched to snapshot paths
        #: with exactly the mapper's suffix rule instead of by eye.
        self.snapshot_id_for_frames: Optional[uuid.UUID] = None
        self._path_cache: dict[object, set[str]] = {}

    async def build(
        self,
        incident: Incident,
        snapshot: Optional[RepositorySnapshot],
        *,
        repository_id=None,
        max_bytes: Optional[int] = None,
    ) -> DebugContext:
        budget = ContextBudget(
            max_bytes=max_bytes or settings.DEBUG_MAX_CONTEXT_BYTES,
            limits={
                "max_bytes": max_bytes or settings.DEBUG_MAX_CONTEXT_BYTES,
                "max_traces": settings.DEBUG_MAX_FILES_READ,
                "max_lines_read": settings.DEBUG_MAX_LINES_READ,
                "max_search_results": settings.DEBUG_MAX_SEARCH_RESULTS,
                "max_tool_calls": settings.DEBUG_MAX_TOOL_CALLS,
            },
        )
        context = DebugContext(
            incident_id=str(incident.id),
            project_id=str(incident.project_id),
            snapshot_id=str(snapshot.id) if snapshot else None,
            version_status=(snapshot.version_status.value if snapshot else "UNKNOWN"),
            version_note=(
                snapshot.version_evidence
                if snapshot and snapshot.version_evidence
                else "no code snapshot is available for this incident"
            ),
            budget=budget,
            built_at=datetime.now(timezone.utc).isoformat(),
        )
        if snapshot is None:
            context.caveats.append(
                "no repository snapshot is linked to this incident, so no code "
                "location can be claimed"
            )
        self.snapshot_id_for_frames = snapshot.id if snapshot else None

        redaction = RedactionReport()
        counters = {"evidence": 0}

        # ---- 1. incident --------------------------------------------------
        await self._section_incident(incident, context, counters, redaction)
        # ---- 2. causal analysis -------------------------------------------
        await self._section_causal(incident, context, counters, redaction)
        # ---- 3. reproduction ----------------------------------------------
        await self._section_reproduction(incident, context, counters, redaction)
        # ---- 4. trace path ------------------------------------------------
        await self._section_traces(incident, snapshot, context, counters, redaction)
        # ---- 5. stack traces ----------------------------------------------
        await self._section_stack_traces(incident, context, counters, redaction)
        # ---- 6. mapped code locations -------------------------------------
        await self._section_code_locations(snapshot, context, counters, redaction)
        # ---- 7. recent changes --------------------------------------------
        await self._section_recent_changes(
            incident, repository_id, context, counters, redaction
        )
        # ---- 8. risk signals ----------------------------------------------
        await self._section_risk_signals(snapshot, context, counters, redaction)
        # ---- 9. telemetry excerpts ----------------------------------------
        await self._section_telemetry(incident, context, counters, redaction)
        # ---- 10. recurrence -----------------------------------------------
        await self._section_recurrence(incident, context, counters, redaction)

        context.redaction = redaction.as_dict()
        self._apply_budget(context, budget)
        if budget.dropped:
            context.caveats.append(
                "context budget was reached; these sections were omitted: "
                + ", ".join(budget.dropped)
            )
        return context

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------
    def _next_id(self, counters: dict) -> str:
        counters["evidence"] += 1
        return f"E{counters['evidence']}"

    def _add(
        self,
        context: DebugContext,
        counters: dict,
        *,
        kind: str,
        reference: str,
        label: str,
        detail: str = "",
        source_table: Optional[str] = None,
        source_id: Optional[uuid.UUID] = None,
        component_id: Optional[uuid.UUID] = None,
        observed_at: Optional[datetime] = None,
        excerpt: Optional[str] = None,
        redaction: Optional[RedactionReport] = None,
    ) -> ContextEvidence:
        cleaned_excerpt = None
        if excerpt:
            cleaned_excerpt, report = self.redactor.redact(excerpt)
            if redaction is not None:
                for category, count in report.counts.items():
                    for _ in range(count):
                        redaction.record(category, 0)
                redaction.characters_removed += report.characters_removed
        item = ContextEvidence(
            id=self._next_id(counters),
            kind=kind,
            reference=reference,
            label=label[:500],
            detail=detail[:2000],
            source_table=source_table,
            source_id=str(source_id) if source_id else None,
            component_id=str(component_id) if component_id else None,
            observed_at=observed_at.isoformat() if observed_at else None,
            excerpt=cleaned_excerpt,
        )
        context.evidence.append(item)
        return item

    async def _section_incident(
        self, incident: Incident, context: DebugContext, counters: dict, redaction
    ) -> None:
        section: dict = {
            "title": incident.title,
            "severity": incident.severity.value,
            "status": incident.status.value,
            "detected_at": incident.detected_at.isoformat()
            if incident.detected_at
            else None,
            "started_at": incident.started_at.isoformat()
            if incident.started_at
            else None,
            "summary": incident.summary,
            "correlation_rationale": incident.correlation_rationale,
            "evidence": [],
            "timeline": [],
        }
        self._add(
            context,
            counters,
            kind="INCIDENT",
            reference=f"INCIDENT:{incident.id}",
            label=incident.title,
            detail=incident.summary or "",
            source_table="incidents",
            source_id=incident.id,
            observed_at=incident.detected_at,
        )
        rows = (
            (
                await self.session.execute(
                    select(IncidentEvidence)
                    .where(IncidentEvidence.incident_id == incident.id)
                    .order_by(IncidentEvidence.timestamp)
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            item = self._add(
                context,
                counters,
                kind="INCIDENT_EVIDENCE",
                reference=f"INCIDENT_EVIDENCE:{row.id}",
                label=f"{row.evidence_type.value} from {row.source_id}",
                detail=(row.description or "") + " " + (row.relevance_reason or ""),
                source_table="incident_evidence",
                source_id=row.id,
                component_id=row.component_id,
                observed_at=row.timestamp,
                excerpt=row.observed_value,
                redaction=redaction,
            )
            section["evidence"].append(
                {
                    "id": item.id,
                    "type": row.evidence_type.value,
                    "observed": row.observed_value,
                    "expected": row.expected_value,
                    "severity": row.severity.value if row.severity else None,
                    "relevance": row.relevance_reason,
                }
            )

        anomalies = (
            (
                await self.session.execute(
                    select(Anomaly)
                    .where(Anomaly.incident_id == incident.id)
                    .order_by(Anomaly.detected_at)
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )
        section["anomalies"] = []
        for anomaly in anomalies:
            item = self._add(
                context,
                counters,
                kind="ANOMALY",
                reference=f"ANOMALY:{anomaly.id}",
                label=(
                    f"{anomaly.anomaly_type.value} on "
                    f"{anomaly.metric_name or anomaly.component_id}"
                ),
                detail=(anomaly.description or ""),
                source_table="anomalies",
                source_id=anomaly.id,
                component_id=anomaly.component_id,
                observed_at=anomaly.detected_at,
            )
            section["anomalies"].append(
                {
                    "id": item.id,
                    "type": anomaly.anomaly_type.value,
                    "severity": anomaly.severity.value if anomaly.severity else None,
                    "metric": anomaly.metric_name,
                    "observed": anomaly.observed_value,
                    "expected": anomaly.expected_value,
                    "detected_at": anomaly.detected_at.isoformat()
                    if anomaly.detected_at
                    else None,
                }
            )

        timeline = (
            (
                await self.session.execute(
                    select(IncidentTimelineEvent)
                    .where(IncidentTimelineEvent.incident_id == incident.id)
                    .order_by(IncidentTimelineEvent.occurred_at)
                    .limit(40)
                )
            )
            .scalars()
            .all()
        )
        for event in timeline:
            section["timeline"].append(
                {
                    "type": event.event_type.value,
                    "at": event.occurred_at.isoformat() if event.occurred_at else None,
                    "title": event.title,
                    "context_only": event.is_context_only,
                }
            )
        context.sections["incident"] = section

    async def _section_causal(
        self, incident: Incident, context: DebugContext, counters: dict, redaction
    ) -> None:
        analysis = (
            (
                await self.session.execute(
                    select(CausalAnalysis)
                    .where(CausalAnalysis.incident_id == incident.id)
                    .order_by(CausalAnalysis.analysis_version.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if analysis is None:
            context.sections["causal_analysis"] = None
            context.caveats.append(
                "no causal analysis exists for this incident; hypotheses will rest "
                "on telemetry alone"
            )
            return
        section: dict = {
            "analysis_id": str(analysis.id),
            "analysis_version": analysis.analysis_version,
            "status": analysis.status.value,
            "overall_confidence": analysis.overall_confidence.value,
            "primary_candidate_id": (
                str(analysis.primary_candidate_id)
                if analysis.primary_candidate_id
                else None
            ),
            "summary": analysis.summary,
            "missing_evidence": analysis.missing_evidence,
            "candidates": [],
        }
        self._add(
            context,
            counters,
            kind="CAUSAL_ANALYSIS",
            reference=f"CAUSAL_ANALYSIS:{analysis.id}",
            label=f"causal analysis v{analysis.analysis_version}",
            detail=analysis.summary or "",
            source_table="causal_analyses",
            source_id=analysis.id,
            observed_at=analysis.started_at,
        )
        candidates = (
            (
                await self.session.execute(
                    select(RootCauseCandidate)
                    .where(RootCauseCandidate.analysis_id == analysis.id)
                    .order_by(RootCauseCandidate.score.desc())
                    .limit(8)
                )
            )
            .scalars()
            .all()
        )
        for candidate in candidates:
            item = self._add(
                context,
                counters,
                kind="CAUSAL_CANDIDATE",
                reference=f"CAUSAL_CANDIDATE:{candidate.id}",
                label=f"{candidate.candidate_type.value} ({candidate.confidence.value})",
                detail=(candidate.explanation or ""),
                source_table="root_cause_candidates",
                source_id=candidate.id,
                component_id=candidate.component_id,
                observed_at=candidate.first_observed_at,
            )
            evidence_rows = (
                (
                    await self.session.execute(
                        select(CausalEvidence)
                        .where(CausalEvidence.candidate_id == candidate.id)
                        .order_by(CausalEvidence.strength.desc())
                        .limit(6)
                    )
                )
                .scalars()
                .all()
            )
            section["candidates"].append(
                {
                    "id": item.id,
                    "candidate_id": str(candidate.id),
                    "type": candidate.candidate_type.value,
                    "confidence": candidate.confidence.value,
                    "score": candidate.score,
                    "explanation": candidate.explanation,
                    "uncertainty": candidate.uncertainty,
                    "supporting": candidate.supporting_evidence_count,
                    "contradicting": candidate.contradicting_evidence_count,
                    "evidence": [
                        {
                            "category": row.category.value,
                            "polarity": row.polarity.value,
                            "quote": row.quote,
                            "explanation": row.explanation,
                            "source": f"{row.source_table}:{row.source_id}"
                            if row.source_id
                            else row.source_table,
                        }
                        for row in evidence_rows
                    ],
                }
            )
        context.sections["causal_analysis"] = section

    async def _section_reproduction(
        self, incident: Incident, context: DebugContext, counters: dict, redaction
    ) -> None:
        experiments = (
            (
                await self.session.execute(
                    select(ReproductionExperiment)
                    .where(ReproductionExperiment.incident_id == incident.id)
                    .order_by(ReproductionExperiment.created_at.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        if not experiments:
            context.sections["reproduction"] = None
            return
        section: dict = {"experiments": []}
        for experiment in experiments:
            item = self._add(
                context,
                counters,
                kind="REPRODUCTION",
                reference=f"REPRODUCTION:{experiment.id}",
                label=f"experiment {experiment.status.value}",
                detail=experiment.summary or "",
                source_table="reproduction_experiments",
                source_id=experiment.id,
                observed_at=experiment.created_at,
            )
            validation = (
                (
                    await self.session.execute(
                        select(ReproductionValidation)
                        .where(ReproductionValidation.experiment_id == experiment.id)
                        .order_by(ReproductionValidation.created_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            comparison = (
                (
                    await self.session.execute(
                        select(ReproductionComparison)
                        .where(ReproductionComparison.experiment_id == experiment.id)
                        .order_by(ReproductionComparison.created_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            section["experiments"].append(
                {
                    "id": item.id,
                    "experiment_id": str(experiment.id),
                    "status": experiment.status.value,
                    "result": experiment.result.value if experiment.result else None,
                    "confidence": experiment.confidence.value
                    if experiment.confidence
                    else None,
                    "summary": experiment.summary,
                    "validation": (
                        {
                            "outcome": validation.outcome.value,
                            "summary": validation.summary,
                            "confidence": (
                                validation.confidence.value
                                if validation.confidence
                                else None
                            ),
                            "missing_inputs": validation.missing_inputs,
                            "environment_differences": validation.environment_differences,
                            "determinism": validation.determinism,
                            "limitations": validation.limitations,
                        }
                        if validation
                        else None
                    ),
                    "comparison": (
                        {
                            "overall_similarity": comparison.overall_similarity.value,
                            "similarity_score": comparison.similarity_score,
                            "explanation": comparison.explanation,
                        }
                        if comparison
                        else None
                    ),
                }
            )
        context.sections["reproduction"] = section

    async def _section_traces(
        self,
        incident: Incident,
        snapshot: Optional[RepositorySnapshot],
        context: DebugContext,
        counters: dict,
        redaction,
    ) -> None:
        spans = (
            (
                await self.session.execute(
                    select(SpanRecord)
                    .where(SpanRecord.project_id == incident.project_id)
                    .order_by(SpanRecord.start_time.desc())
                    .limit(settings.DEBUG_MAX_FILES_READ * 2)
                )
            )
            .scalars()
            .all()
        )
        failing = [span for span in spans if span.status == TraceStatus.ERROR]
        section: dict = {
            "examined": len(spans),
            "failing_count": len(failing),
            "failing_spans": [],
            "trace_mappings": [],
        }
        for span in failing[:15]:
            item = self._add(
                context,
                counters,
                kind="SPAN",
                reference=f"SPAN:{span.span_id}",
                label=f"{span.operation or 'span'} failed",
                detail=(
                    f"duration {span.duration_ms}ms"
                    if span.duration_ms is not None
                    else "duration unknown"
                ),
                source_table="spans",
                source_id=None,
                component_id=span.component_id,
                observed_at=span.start_time,
            )
            section["failing_spans"].append(
                {
                    "id": item.id,
                    "span_id": span.span_id,
                    "trace_id": span.trace_id,
                    "parent_span_id": span.parent_span_id,
                    "operation": span.operation,
                    "duration_ms": span.duration_ms,
                    "start_time": span.start_time.isoformat()
                    if span.start_time
                    else None,
                    "metadata": span.metadata_,
                }
            )
            self._add(
                context,
                counters,
                kind="TRACE",
                reference=f"TRACE:{span.trace_id}",
                label=f"trace containing {span.operation or 'failing span'}",
                source_table="traces",
            )
        if snapshot is not None:
            mappings = (
                (
                    await self.session.execute(
                        select(TraceCodeMapping)
                        .where(TraceCodeMapping.snapshot_id == snapshot.id)
                        .order_by(TraceCodeMapping.confidence.desc())
                        .limit(20)
                    )
                )
                .scalars()
                .all()
            )
            for mapping in mappings:
                section["trace_mappings"].append(
                    {
                        "kind": mapping.mapping_kind.value,
                        "confidence": mapping.confidence,
                        "symbol_id": str(mapping.symbol_id)
                        if mapping.symbol_id
                        else None,
                        "file_path": mapping.file_path,
                        "lines": [mapping.start_line, mapping.end_line],
                        "operation": mapping.operation,
                        "endpoint": mapping.endpoint,
                        "evidence": mapping.evidence,
                        "unmapped_reason": mapping.unmapped_reason,
                    }
                )
        if not failing:
            context.caveats.append(
                "no failing spans were found for this project; trace-to-code mapping "
                "has nothing to work from"
            )
        context.sections["trace_path"] = section

    async def _snapshot_paths(self, snapshot_id) -> set[str]:
        if snapshot_id in self._path_cache:
            return self._path_cache[snapshot_id]
        rows = (
            await self.session.execute(
                select(CodeFile.path).where(CodeFile.snapshot_id == snapshot_id)
            )
        ).all()
        paths = {row[0] for row in rows}
        self._path_cache[snapshot_id] = paths
        return paths

    async def _section_stack_traces(
        self, incident: Incident, context: DebugContext, counters: dict, redaction
    ) -> None:
        logs = (
            (
                await self.session.execute(
                    select(LogRecord)
                    .where(LogRecord.project_id == incident.project_id)
                    .order_by(LogRecord.timestamp.desc())
                    .limit(settings.DEBUG_MAX_FILES_READ * 2)
                )
            )
            .scalars()
            .all()
        )
        snapshot_files: set[str] = set()
        if self.snapshot_id_for_frames is not None:
            snapshot_files = await self._snapshot_paths(self.snapshot_id_for_frames)
        section: dict = {"examined": len(logs), "traces": []}
        for log in logs:
            parsed = self.stack_analyzer.parse(log.message)
            if parsed is None or not parsed.frames:
                continue
            item = self._add(
                context,
                counters,
                kind="LOG",
                reference=f"LOG:{log.id}",
                label=f"{parsed.exception_type or 'traceback'} in {log.service or 'unknown service'}",
                detail=(parsed.message or "")[:300],
                source_table="log_records",
                source_id=log.id,
                component_id=log.component_id,
                observed_at=log.timestamp,
                excerpt=log.message,
                redaction=redaction,
            )
            section["traces"].append(
                {
                    "id": item.id,
                    "log_id": str(log.id),
                    "format": parsed.format,
                    "exception_type": parsed.exception_type,
                    "message": parsed.message,
                    "frames": [
                        {
                            "file": frame.file_path,
                            #: The snapshot-relative path, or None when the frame
                            #: cannot be confined to the repository. Stated
                            #: explicitly so a claim built on it is not silently
                            #: attributed to a file that is not in the snapshot.
                            "snapshot_path": match_frame_to_snapshot(
                                snapshot_files, frame.file_path or ""
                            ),
                            "line": frame.line,
                            "column": frame.column,
                            "function": frame.function,
                        }
                        for frame in parsed.frames[:15]
                    ],
                }
            )
            if len(section["traces"]) >= 8:
                break
        if not section["traces"]:
            context.caveats.append(
                "no stack trace could be parsed from this project's logs; the top "
                "frame is therefore unknown and no code location can be derived from one"
            )
        context.sections["stack_traces"] = section

    async def _section_code_locations(
        self,
        snapshot: Optional[RepositorySnapshot],
        context: DebugContext,
        counters: dict,
        redaction,
    ) -> None:
        if snapshot is None:
            context.sections["code_locations"] = None
            return
        symbols: list[CodeSymbol] = []
        seen: set[object] = set()
        mappings = (
            (
                await self.session.execute(
                    select(TraceCodeMapping)
                    .where(
                        TraceCodeMapping.snapshot_id == snapshot.id,
                        TraceCodeMapping.symbol_id.is_not(None),
                    )
                    .order_by(TraceCodeMapping.confidence.desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        for mapping in mappings:
            if mapping.symbol_id in seen:
                continue
            seen.add(mapping.symbol_id)
            symbol = await self.knowledge.symbol_by_id(mapping.symbol_id)
            if symbol is not None:
                symbols.append(symbol)
        section: dict = {"mapped": [], "candidates": []}
        for symbol in symbols:
            item = self._add(
                context,
                counters,
                kind="SYMBOL",
                reference=f"FILE:{symbol.file_path}:{symbol.start_line}-{symbol.end_line}",
                label=symbol.qualified_name,
                detail=f"{symbol.symbol_type.value} ({symbol.language or 'unknown'})",
                source_table="code_symbols",
                source_id=symbol.id,
                component_id=symbol.component_id,
            )
            section["mapped"].append(
                {
                    "id": item.id,
                    "symbol_id": str(symbol.id),
                    "qualified_name": symbol.qualified_name,
                    "file_path": symbol.file_path,
                    "start_line": symbol.start_line,
                    "end_line": symbol.end_line,
                    "symbol_type": symbol.symbol_type.value,
                    "signature": symbol.signature,
                    "route": symbol.route,
                    "has_source": bool(symbol.source),
                }
            )
        #: File-level candidates for sections that only name a file (a failing
        #: test, a changed file): offered so the model can ask about them without
        #: pretending they are confirmed locations.
        for path in sorted(
            {item for item in [m.file_path for m in mappings if m.file_path] if item}
        )[:10]:
            symbols_in_file = await self.knowledge.symbols_in_file(
                snapshot.id, path, limit=5
            )
            for symbol in symbols_in_file:
                if symbol.id in seen:
                    continue
                seen.add(symbol.id)
                item = self._add(
                    context,
                    counters,
                    kind="SYMBOL",
                    reference=f"FILE:{symbol.file_path}:{symbol.start_line}-{symbol.end_line}",
                    label=symbol.qualified_name,
                    detail=f"{symbol.symbol_type.value} in a mapped file",
                    source_table="code_symbols",
                    source_id=symbol.id,
                )
                section["candidates"].append(
                    {
                        "id": item.id,
                        "symbol_id": str(symbol.id),
                        "qualified_name": symbol.qualified_name,
                        "file_path": symbol.file_path,
                        "start_line": symbol.start_line,
                        "end_line": symbol.end_line,
                    }
                )
        context.sections["code_locations"] = section

    async def _section_recent_changes(
        self,
        incident: Incident,
        repository_id,
        context: DebugContext,
        counters: dict,
        redaction,
    ) -> None:
        onset = _as_aware(incident.started_at or incident.detected_at)
        if onset is None:
            context.sections["recent_changes"] = None
            return
        window_start = onset - timedelta(days=settings.CODE_RECENT_CHANGE_DAYS)
        deployment_rows: list[tuple[DeploymentEvent, Optional[datetime]]] = []
        deployments = (
            (
                await self.session.execute(
                    select(DeploymentEvent)
                    .where(
                        DeploymentEvent.project_id == incident.project_id,
                        DeploymentEvent.deployed_at >= window_start,
                        DeploymentEvent.deployed_at <= onset,
                    )
                    .order_by(DeploymentEvent.deployed_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        section: dict = {"window_start": window_start.isoformat(), "deployments": []}
        for deployment in deployments:
            #: SQLite hands back naive datetimes where PostgreSQL returns aware
            #: ones. Normalising here keeps the same incident producing the same
            #: context on both backends instead of raising on one of them.
            deployed_at = _as_aware(deployment.deployed_at)
            deployment_rows.append((deployment, deployed_at))
            item = self._add(
                context,
                counters,
                kind="DEPLOYMENT",
                reference=(
                    f"COMMIT:{deployment.commit_sha}"
                    if deployment.commit_sha
                    else f"DEPLOYMENT:{deployment.deployment_id}"
                ),
                label=(
                    f"deployment {deployment.deployment_id} "
                    f"({deployment.version or 'no version'})"
                ),
                detail=deployment.description or "",
                source_table="deployment_events",
                source_id=deployment.id,
                component_id=deployment.component_id,
                observed_at=deployment.deployed_at,
            )
            section["deployments"].append(
                {
                    "id": item.id,
                    "deployment_id": deployment.deployment_id,
                    "version": deployment.version,
                    "commit_sha": deployment.commit_sha,
                    "deployed_at": deployed_at.isoformat() if deployed_at else None,
                    "status": deployment.status.value,
                    "component_id": str(deployment.component_id)
                    if deployment.component_id
                    else None,
                    "seconds_before_onset": (
                        int((onset - deployed_at).total_seconds())
                        if deployed_at is not None
                        else None
                    ),
                }
            )
        if not deployments:
            context.caveats.append(
                f"no deployment was recorded in the {settings.CODE_RECENT_CHANGE_DAYS} days "
                "before this incident, so no change can be temporally linked to it"
            )
        #: Which of those changes is actually connected to the code the incident
        #: exercised (§19, §20). Deliberately separate from the deployment list:
        #: "was deployed recently" and "touched the failing path" are different
        #: facts, and conflating them is how a recent commit gets blamed.
        section["relevance"] = await self._assess_changes(
            incident, repository_id, context, counters, section
        )
        context.sections["recent_changes"] = section

    async def _assess_changes(
        self,
        incident: Incident,
        repository_id,
        context: DebugContext,
        counters: dict,
        section: dict,
    ) -> Optional[dict]:
        repository = None
        if repository_id is not None:
            repository = await self.session.get(CodeRepository, repository_id)
        snapshot = None
        if self.snapshot_id_for_frames is not None:
            snapshot = await self.session.get(
                RepositorySnapshot, self.snapshot_id_for_frames
            )
        named = sorted(
            {
                item.reference.split("FILE:", 1)[-1].split(":", 1)[0]
                for item in context.evidence
                if item.reference.startswith("FILE:")
            }
        )
        try:
            report = await ChangeRelevanceAnalyzer(self.session).analyze(
                incident, snapshot, repository, named_files=named
            )
        except Exception as error:  # noqa: BLE001 - relevance is an aid, not a gate
            logger.warning("change relevance analysis failed: %s", error)
            context.caveats.append(
                "the relevance of recent changes could not be determined, so no change "
                "is reported as relevant"
            )
            return None
        for note in report.notes:
            if note not in context.caveats:
                context.caveats.append(note)
        for assessment in report.assessments:
            if assessment.classification.value == "UNKNOWN":
                continue
            self._add(
                context,
                counters,
                kind="DEPLOYMENT",
                reference=(
                    f"COMMIT:{assessment.commit_sha}"
                    if assessment.commit_sha
                    else f"DEPLOYMENT:{assessment.deployment_id}"
                ),
                label=(
                    f"{assessment.classification.value}: "
                    f"{(assessment.commit_sha or assessment.deployment_id or '')[:12]}"
                ),
                detail=assessment.reason,
                source_table="deployment_events",
                observed_at=assessment.deployed_at,
            )
        section["relevance"] = report.as_dict()
        return report.as_dict()

    async def _section_risk_signals(
        self,
        snapshot: Optional[RepositorySnapshot],
        context: DebugContext,
        counters: dict,
        redaction,
    ) -> None:
        if snapshot is None:
            context.sections["risk_signals"] = None
            return
        rows = (
            (
                await self.session.execute(
                    select(CodeRiskSignal)
                    .where(CodeRiskSignal.snapshot_id == snapshot.id)
                    .order_by(CodeRiskSignal.value.desc())
                    .limit(25)
                )
            )
            .scalars()
            .all()
        )
        section: dict = {"signals": []}
        for row in rows:
            section["signals"].append(
                {
                    "type": row.signal_type.value,
                    "value": row.value,
                    "unit": row.unit,
                    "file_path": row.file_path,
                    "symbol_id": str(row.symbol_id) if row.symbol_id else None,
                    "detail": row.detail,
                }
            )
        context.sections["risk_signals"] = section

    async def _section_telemetry(
        self, incident: Incident, context: DebugContext, counters: dict, redaction
    ) -> None:
        logs = (
            (
                await self.session.execute(
                    select(LogRecord)
                    .where(LogRecord.project_id == incident.project_id)
                    .order_by(LogRecord.timestamp.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        metrics = (
            (
                await self.session.execute(
                    select(MetricRecord)
                    .where(MetricRecord.project_id == incident.project_id)
                    .order_by(MetricRecord.timestamp.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        section: dict = {"logs": [], "metrics": []}
        for log in logs:
            item = self._add(
                context,
                counters,
                kind="LOG",
                reference=f"LOG:{log.id}",
                label=f"{log.level.value} {log.service or ''}".strip(),
                excerpt=log.message,
                source_table="log_records",
                source_id=log.id,
                component_id=log.component_id,
                observed_at=log.timestamp,
                redaction=redaction,
            )
            section["logs"].append(
                {"id": item.id, "level": log.level.value, "service": log.service}
            )
        for metric in metrics:
            item = self._add(
                context,
                counters,
                kind="METRIC",
                reference=f"METRIC:{metric.id}",
                label=f"{metric.metric_name} = {metric.value}",
                source_table="metric_records",
                source_id=metric.id,
                component_id=metric.component_id,
                observed_at=metric.timestamp,
            )
            section["metrics"].append(
                {
                    "id": item.id,
                    "name": metric.metric_name,
                    "value": metric.value,
                    "unit": metric.unit,
                    "labels": metric.labels,
                }
            )
        context.sections["telemetry_excerpts"] = section

    async def _section_recurrence(
        self, incident: Incident, context: DebugContext, counters: dict, redaction
    ) -> None:
        """Earlier incidents touching the same files (§45, §46).

        Deliberately framed as *recurrence*, not defectiveness: a file that has
        appeared in four incidents is worth looking at first, and that is all the
        data supports.
        """
        current_paths = {
            item.reference.split("FILE:", 1)[-1].split(":", 1)[0]
            for item in context.evidence
            if item.reference.startswith("FILE:")
        }
        if not current_paths:
            context.sections["recurrence"] = None
            return
        from app.models.code import DebugCodeLocation, DebugSession

        rows = (
            await self.session.execute(
                select(DebugCodeLocation.file_path, DebugSession.incident_id)
                .join(DebugSession, DebugSession.id == DebugCodeLocation.session_id)
                .where(
                    DebugSession.project_id == incident.project_id,
                    DebugSession.incident_id != incident.id,
                    DebugCodeLocation.file_path.in_(sorted(current_paths)[:20]),
                )
                .order_by(DebugCodeLocation.file_path)
                .limit(100)
            )
        ).all()
        counts: dict[str, set] = {}
        for path, incident_id in rows:
            counts.setdefault(path, set()).add(incident_id)
        section: dict = {"recurrence": []}
        for path, incident_ids in sorted(
            counts.items(), key=lambda item: (-len(item[1]), item[0])
        ):
            item = self._add(
                context,
                counters,
                kind="RECURRENCE",
                reference=f"FILE:{path}",
                label=f"{path} appeared in {len(incident_ids)} other investigation(s)",
                detail="previous incidents: "
                + ", ".join(sorted(str(value) for value in incident_ids)),
                source_table="debug_code_locations",
            )
            section["recurrence"].append(
                {
                    "id": item.id,
                    "file_path": path,
                    "previous_incidents": len(incident_ids),
                }
            )
        context.sections["recurrence"] = section if section["recurrence"] else None

    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------
    def _apply_budget(self, context: DebugContext, budget: ContextBudget) -> None:
        """Trim the context to the byte budget, dropping sections in fixed order.

        Sizes are measured on the serialised payload, because that is what has to
        fit in the request. Trimming proceeds in two clearly separated passes:

        1. Drop whole sections from the tail of :data:`SECTION_ORDER`, along with
           any evidence that only existed to feed them. A section is never left
           half-populated, because a partial section still reads as complete.
        2. If whole sections are not enough, drop evidence from the tail of the
           evidence list (lowest priority collected last) and record that the
           index itself was truncated.

        Whatever happens is reported in ``budget.dropped`` /
        ``budget.truncated_sections`` and surfaced as a caveat, so no analysis can
        imply it saw evidence that was actually omitted.
        """

        def size() -> int:
            return len(json.dumps(context.for_prompt(), default=str).encode("utf-8"))

        def live_ids() -> set[str]:
            found: set[str] = set()
            for value in context.sections.values():
                found |= _evidence_ids(value)
            return found

        budget.bytes_used = size()
        if budget.bytes_used <= budget.max_bytes:
            return
        for name in reversed(SECTION_ORDER):
            if context.sections.get(name) is None:
                continue
            context.sections[name] = None
            budget.dropped.append(name)
            keep = live_ids()
            context.evidence = [item for item in context.evidence if item.id in keep]
            budget.bytes_used = size()
            if budget.bytes_used <= budget.max_bytes:
                return
        while context.evidence and budget.bytes_used > budget.max_bytes:
            context.evidence.pop()
            if "evidence" not in budget.truncated_sections:
                budget.truncated_sections.append("evidence")
            budget.bytes_used = size()


def _as_aware(value: Optional[datetime]) -> Optional[datetime]:
    """Normalise a stored timestamp to an aware UTC value.

    SQLite drops the timezone where PostgreSQL keeps it, so every timestamp that
    takes part in arithmetic is passed through here. Without this, the same
    incident builds a context on PostgreSQL and raises on SQLite.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _evidence_ids(value: Any) -> set[str]:
    """Collect every ``"id": "E…"`` mentioned anywhere inside ``value``."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "id" and isinstance(item, str) and item.startswith("E"):
                found.add(item)
            else:
                found |= _evidence_ids(item)
    elif isinstance(value, list):
        for item in value:
            found |= _evidence_ids(item)
    return found


#: Re-exported so the session service can describe a frame without importing the
#: mapper directly.
__all__ = [
    "CONTEXT_VERSION",
    "ContextBudget",
    "ContextEvidence",
    "DebugContext",
    "DebugContextBuilder",
    "StackFrame",
    "CodeRelationshipType",
]
