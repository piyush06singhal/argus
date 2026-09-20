"""ARGUS Reproduction Context (Phase 5 §26, §27).

Reads the **original incident's own telemetry** into a comparable shape.

Everything Phase 5 does downstream — the expected behaviour a plan promises, the
eight similarity dimensions, the verdict — is measured against what this module
returns. That makes it the single place where the phase could accidentally cheat:
if the context were fuzzy, synthesized, or loosely derived, every comparison
would inherit the fuzziness while still *looking* rigorous.

So the loader is strict about provenance:

* every component, timestamp, latency and log pattern comes from a stored row;
* the query windows are the Phase 4 ones (``CAUSAL_EVIDENCE_WINDOW_SECONDS`` /
  ``CAUSAL_RECOVERY_WINDOW_SECONDS``), so an incident is read the same way the
  causal engine read it;
* missing telemetry is reported as missing (empty collections and a populated
  ``provenance``), never filled in with a plausible default.

A source with no telemetry at all is a legitimate, reportable outcome — it is
what makes a reproduction ``INCONCLUSIVE`` rather than falsely ``FAILED``.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.anomaly import Anomaly, AnomalyObservation
from app.models.incident import Incident, IncidentEvidence
from app.models.ingestion import HealthCheckEvent, HealthStatus
from app.models.observability import LogRecord, Severity, SpanRecord, TraceStatus
from app.models.system import SystemComponent

logger = logging.getLogger(__name__)
settings = get_settings()

#: Numbers, hex ids and quoted strings are stripped when comparing log text, so
#: two occurrences of the same failure message collapse to one pattern instead
#: of looking like different messages.
_NUMBERS_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_QUOTED_RE = re.compile(r"('[^']*'|\"[^\"]*\")")


def normalize_log_message(message: str) -> str:
    """Collapse a concrete log line into a comparable pattern."""
    text = _UUID_RE.sub("<uuid>", message)
    text = _QUOTED_RE.sub("<value>", text)
    text = _HEX_RE.sub("<hex>", text)
    text = _NUMBERS_RE.sub("<n>", text)
    return " ".join(text.split())[:240]


def ensure_utc(
    value: Optional[datetime], fallback: Optional[datetime] = None
) -> datetime:
    """Coerce a possibly-naive timestamp to aware UTC (SQLite returns naive)."""
    if value is None:
        return fallback or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass
class SourceBehavior:
    """The original incident's observable behaviour, in comparable form."""

    incident_id: uuid.UUID
    onset: datetime
    window_start: datetime
    window_end: datetime

    #: Affected components, in the order their telemetry first degraded.
    sequence: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    component_ids: dict[str, uuid.UUID] = field(default_factory=dict)

    error_components: list[str] = field(default_factory=list)
    degraded_components: list[str] = field(default_factory=list)
    #: Observed latency per component (ms), from stored metric evidence.
    latency_ms: dict[str, float] = field(default_factory=dict)
    #: Share of in-window spans that failed (0..1).
    error_rate: float = 0.0
    span_count: int = 0
    error_span_count: int = 0
    #: ``{"parent", "child", "operation", "error"}`` call relationships.
    trace_edges: list[dict[str, Any]] = field(default_factory=list)
    #: component → normalized log patterns seen at error level.
    log_patterns: dict[str, list[str]] = field(default_factory=dict)
    recovery_order: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """True when the incident carries nothing to compare against."""
        return not self.sequence and not self.error_components and self.span_count == 0

    def relabeled(self, mapping: Mapping[str, str]) -> "SourceBehavior":
        """Return a copy whose component names are the *sandbox's* names.

        The incident names components as ARGUS knows them ("Inventory Database");
        a sandbox calls the same thing ``datastore``. Comparing the two without
        translating one side would report zero component overlap for every
        experiment — a comparison that always says "nothing matched" is worse
        than no comparison, because it looks like a real finding.

        Names the template does not know are kept verbatim rather than dropped:
        they then show up as *missing* components, which is the truthful report
        ("the sandbox has no such service"). The mapping itself is recorded in
        ``provenance`` so the translation stays auditable.
        """

        def translate(name: str) -> str:
            return mapping.get(name, name)

        relabeled = replace(
            self,
            sequence=list(dict.fromkeys(translate(item) for item in self.sequence)),
            components=list(dict.fromkeys(translate(item) for item in self.components)),
            component_ids={
                translate(name): value for name, value in self.component_ids.items()
            },
            error_components=list(
                dict.fromkeys(translate(item) for item in self.error_components)
            ),
            degraded_components=list(
                dict.fromkeys(translate(item) for item in self.degraded_components)
            ),
            latency_ms={translate(k): v for k, v in self.latency_ms.items()},
            trace_edges=[
                {
                    **edge,
                    "parent": translate(str(edge.get("parent"))),
                    "child": translate(str(edge.get("child"))),
                }
                for edge in self.trace_edges
            ],
            log_patterns={translate(k): list(v) for k, v in self.log_patterns.items()},
            recovery_order=list(
                dict.fromkeys(translate(item) for item in self.recovery_order)
            ),
            provenance={**self.provenance, "alias_map": dict(mapping)},
        )
        return relabeled

    def as_dict(self) -> dict[str, Any]:
        return {
            "incident_id": str(self.incident_id),
            "onset": self.onset.isoformat(),
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "sequence": list(self.sequence),
            "components": list(self.components),
            "error_components": list(self.error_components),
            "degraded_components": list(self.degraded_components),
            "latency_ms": dict(self.latency_ms),
            "error_rate": round(self.error_rate, 4),
            "span_count": self.span_count,
            "error_span_count": self.error_span_count,
            "trace_edge_count": len(self.trace_edges),
            "log_pattern_count": sum(len(v) for v in self.log_patterns.values()),
            "recovery_order": list(self.recovery_order),
            "provenance": dict(self.provenance),
        }


def _component_name(component: Optional[SystemComponent], fallback: str) -> str:
    if component is None:
        return fallback
    return component.name


class SourceBehaviorLoader:
    """Loads an incident's behaviour from stored Phase 1–3 telemetry."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(self, incident: Incident) -> SourceBehavior:
        """Load the incident's comparable behaviour, bounded by Phase 4 windows."""
        onset = ensure_utc(incident.started_at or incident.detected_at)
        window_start = onset - timedelta(
            seconds=settings.CAUSAL_EVIDENCE_WINDOW_SECONDS
        )
        window_end = onset + timedelta(seconds=settings.CAUSAL_RECOVERY_WINDOW_SECONDS)
        behavior = SourceBehavior(
            incident_id=incident.id,
            onset=onset,
            window_start=window_start,
            window_end=window_end,
        )

        anomalies = await self._load_anomalies(incident)
        onset_by_anomaly = await self._load_observation_times(anomalies)
        component_ids: set[uuid.UUID] = set()
        for anomaly in anomalies:
            if anomaly.component_id:
                component_ids.add(anomaly.component_id)
        if incident.primary_component_id:
            component_ids.add(incident.primary_component_id)
        evidence = await self._load_evidence(incident)
        for row in evidence:
            if row.component_id:
                component_ids.add(row.component_id)

        components = await self._load_components(incident.project_id, component_ids)
        behavior.component_ids = {
            _component_name(components.get(cid), str(cid)): cid for cid in component_ids
        }

        # Ordering: earliest stored observation per anomaly, falling back to the
        # anomaly's own detected_at only when no observation exists. Using the
        # sweep instant for everything would make the incident look simultaneous.
        ordered: list[tuple[datetime, str]] = []
        for anomaly in anomalies:
            if not anomaly.component_id:
                continue
            at = onset_by_anomaly.get(anomaly.id) or ensure_utc(anomaly.detected_at)
            name = _component_name(
                components.get(anomaly.component_id), str(anomaly.component_id)
            )
            ordered.append((at, name))
        ordered.sort(key=lambda item: item[0])
        behavior.sequence = list(dict.fromkeys(name for _, name in ordered))
        behavior.components = list(behavior.sequence)

        await self._load_spans(incident, behavior)
        await self._load_logs(incident, behavior)
        await self._load_metric_evidence(incident, evidence, behavior)
        await self._load_recovery(incident, behavior, onset)

        # Deduplicate while preserving discovery order: anomalies first, then
        # spans, then logs, so the most authoritative source wins the ordering.
        behavior.error_components = list(dict.fromkeys(behavior.error_components))

        behavior.provenance = {
            "anomalies": len(anomalies),
            "components": len(behavior.components),
            "evidence_rows": len(evidence),
            "spans": behavior.span_count,
            "log_patterns": sum(len(v) for v in behavior.log_patterns.values()),
            "health_recoveries": len(behavior.recovery_order),
            "windows": {
                "evidence_seconds": settings.CAUSAL_EVIDENCE_WINDOW_SECONDS,
                "recovery_seconds": settings.CAUSAL_RECOVERY_WINDOW_SECONDS,
            },
        }
        return behavior

    # -- loaders ---------------------------------------------------------
    async def _load_anomalies(self, incident: Incident) -> list[Anomaly]:
        stmt = (
            select(Anomaly)
            .where(
                Anomaly.project_id == incident.project_id,
                Anomaly.incident_id == incident.id,
                Anomaly.suppressed.is_(False),
            )
            .order_by(Anomaly.detected_at)
        )
        if incident.environment_id is None:
            stmt = stmt.where(Anomaly.environment_id.is_(None))
        else:
            stmt = stmt.where(Anomaly.environment_id == incident.environment_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def _load_observation_times(
        self, anomalies: Sequence[Anomaly]
    ) -> dict[uuid.UUID, datetime]:
        if not anomalies:
            return {}
        ids = [anomaly.id for anomaly in anomalies]
        stmt = select(AnomalyObservation).where(AnomalyObservation.anomaly_id.in_(ids))
        times: dict[uuid.UUID, datetime] = {}
        for row in (await self._session.execute(stmt)).scalars().all():
            if row.observed_at is None:
                continue
            at = ensure_utc(row.observed_at)
            existing = times.get(row.anomaly_id)
            if existing is None or at < existing:
                times[row.anomaly_id] = at
        return times

    async def _load_evidence(self, incident: Incident) -> list[IncidentEvidence]:
        stmt = (
            select(IncidentEvidence)
            .where(IncidentEvidence.incident_id == incident.id)
            .order_by(IncidentEvidence.timestamp)
            .limit(settings.CAUSAL_MAX_EVIDENCE)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def _load_components(
        self, project_id: uuid.UUID, component_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, SystemComponent]:
        if not component_ids:
            return {}
        stmt = select(SystemComponent).where(
            SystemComponent.project_id == project_id,
            SystemComponent.id.in_(list(component_ids)),
        )
        return {
            row.id: row for row in (await self._session.execute(stmt)).scalars().all()
        }

    async def _load_spans(self, incident: Incident, behavior: SourceBehavior) -> None:
        """Build the incident's call topology and error profile from spans."""
        stmt = (
            select(SpanRecord)
            .where(
                SpanRecord.project_id == incident.project_id,
                SpanRecord.start_time >= behavior.window_start,
                SpanRecord.start_time <= behavior.window_end,
            )
            .order_by(SpanRecord.start_time)
            .limit(settings.CAUSAL_MAX_TRACES * 20)
        )
        spans = list((await self._session.execute(stmt)).scalars().all())
        if not spans:
            return
        by_span_id = {span.span_id: span for span in spans}
        component_ids = {span.component_id for span in spans if span.component_id}
        components = await self._load_components(incident.project_id, component_ids)
        names = {
            cid: _component_name(components.get(cid), str(cid)) for cid in component_ids
        }

        errors = 0
        for span in spans:
            if span.status is TraceStatus.ERROR:
                errors += 1
        behavior.span_count = len(spans)
        behavior.error_span_count = errors
        behavior.error_rate = errors / len(spans) if spans else 0.0

        # Error profile: a component is an error component when one of its own
        # spans failed. Derived from the span rows directly rather than through
        # the parent/child edges, so a failing root span (which has no parent to
        # attribute it to) still counts.
        for span in spans:
            if span.status is TraceStatus.ERROR and span.component_id:
                name = names.get(span.component_id)
                if name:
                    behavior.error_components.append(name)
            if span.component_id and span.duration_ms is not None:
                name = names.get(span.component_id)
                if name:
                    behavior.latency_ms[name] = max(
                        behavior.latency_ms.get(name, 0.0), float(span.duration_ms)
                    )

        edges: list[dict[str, Any]] = []
        for span in spans:
            child_name = names.get(span.component_id) if span.component_id else None
            if not span.parent_span_id or not child_name:
                continue
            parent = by_span_id.get(span.parent_span_id)
            parent_name = (
                names.get(parent.component_id)
                if parent is not None and parent.component_id
                else None
            )
            if not parent_name or parent_name == child_name:
                continue
            edges.append(
                {
                    "parent": parent_name,
                    "child": child_name,
                    "operation": span.operation,
                    "error": span.status is TraceStatus.ERROR,
                }
            )
        behavior.trace_edges = edges

    async def _load_logs(self, incident: Incident, behavior: SourceBehavior) -> None:
        stmt = (
            select(LogRecord)
            .where(
                LogRecord.project_id == incident.project_id,
                LogRecord.timestamp >= behavior.window_start,
                LogRecord.timestamp <= behavior.window_end,
                LogRecord.level.in_([Severity.ERROR, Severity.FATAL]),
            )
            .order_by(LogRecord.timestamp)
            .limit(500)
        )
        logs = list((await self._session.execute(stmt)).scalars().all())
        if not logs:
            return
        component_ids = {row.component_id for row in logs if row.component_id}
        components = await self._load_components(incident.project_id, component_ids)
        for row in logs:
            if not row.component_id:
                continue
            name = _component_name(
                components.get(row.component_id), str(row.component_id)
            )
            patterns = behavior.log_patterns.setdefault(name, [])
            pattern = normalize_log_message(row.message or "")
            if pattern and pattern not in patterns:
                patterns.append(pattern)
            if name not in behavior.error_components:
                behavior.error_components.append(name)

    async def _load_metric_evidence(
        self,
        incident: Incident,
        evidence: Sequence[IncidentEvidence],
        behavior: SourceBehavior,
    ) -> None:
        """Latency thresholds come from stored metric evidence, not from guesses.

        The *largest* observed value per component is kept: a reproduction has to
        approach the worst degradation the incident showed, not its quietest
        sample. Names are resolved from the components already discovered, with
        the evidence ``source_id`` as an explicit fallback so an unresolved
        metric is still attributable rather than silently dropped.
        """
        by_id = {cid: name for name, cid in behavior.component_ids.items()}
        unresolved_ids = {
            row.component_id
            for row in evidence
            if row.component_id and row.component_id not in by_id
        }
        if unresolved_ids:
            components = await self._load_components(
                incident.project_id, unresolved_ids
            )
            for cid, component in components.items():
                by_id[cid] = component.name
                behavior.component_ids[component.name] = cid

        for row in evidence:
            if getattr(row.evidence_type, "value", row.evidence_type) != "METRIC":
                continue
            if row.observed_value is None:
                continue
            try:
                value = float(row.observed_value)
            except (TypeError, ValueError):
                continue
            name = by_id.get(row.component_id) if row.component_id is not None else None
            if name is None:
                name = row.source_id or "unknown"
            if value >= behavior.latency_ms.get(name, 0.0):
                behavior.latency_ms[name] = value

    async def _load_recovery(
        self,
        incident: Incident,
        behavior: SourceBehavior,
        onset: datetime,
    ) -> None:
        """Order the components that returned to a healthy state after onset."""
        stmt = (
            select(HealthCheckEvent)
            .where(
                HealthCheckEvent.project_id == incident.project_id,
                HealthCheckEvent.timestamp >= onset,
                HealthCheckEvent.timestamp <= behavior.window_end,
            )
            .order_by(HealthCheckEvent.timestamp)
            .limit(500)
        )
        try:
            rows = list((await self._session.execute(stmt)).scalars().all())
        except Exception as exc:  # noqa: BLE001 - health telemetry is optional
            logger.info("Health recovery query unavailable: %s", exc)
            return
        if not rows:
            return
        recovered: list[str] = []
        component_ids = {row.component_id for row in rows if row.component_id}
        components = await self._load_components(incident.project_id, component_ids)
        for row in rows:
            if row.status is not HealthStatus.HEALTHY or row.component_id is None:
                continue
            name = _component_name(
                components.get(row.component_id), str(row.component_id)
            )
            if name not in recovered:
                recovered.append(name)
        behavior.recovery_order = recovered
        behavior.provenance["health_recovery_source"] = "health_check_events"


__all__ = [
    "SourceBehavior",
    "SourceBehaviorLoader",
    "ensure_utc",
    "normalize_log_message",
]
