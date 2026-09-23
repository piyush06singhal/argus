"""ARGUS Reliability Case (Phase 11 §14–§18).

The unified operational object. A case is what a person actually works on: it
holds references to everything ARGUS concluded and did about one situation, and
a single ordered timeline of what happened.

What a case deliberately is **not**:

* **not a second incident.** The incident subsystem stays authoritative about
  incidents: a case points at one (`incident_id`) and never edits it. Two tables
  disagreeing about whether something is resolved is exactly the §5 problem this
  phase exists to remove, and it is not solved by giving the new table a second
  opinion.
* **not a new evidence store.** §16 wants all evidence *linked*, not copied. The
  case's evidence view is assembled by querying the phases that own the rows, so
  a metric, trace, commit or patch is stored once and cited from here.
* **not a workflow.** §11's workflow engine drives the orchestration; a case is
  the durable record of the situation the workflow is about.

Status changes go through one legal-transition table, so the API and the UI
cannot offer a move the backend will refuse — the same discipline Phase 3 applied
to incidents and Phase 9 to remediation actions.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.platform import (
    CaseStatus,
    CaseTrigger,
    PlatformEventType,
    ReliabilityCase,
    ReliabilityCaseTimeline,
    TimelineEntryKind,
)

logger = logging.getLogger(__name__)

#: The legal case lifecycle (§11, §14). Terminal states have no outgoing edges.
CASE_STATUS_TRANSITIONS: dict[CaseStatus, tuple[CaseStatus, ...]] = {
    CaseStatus.OPEN: (
        CaseStatus.TRIAGED,
        CaseStatus.ANALYZING,
        CaseStatus.RESOLVED,
        CaseStatus.CANCELLED,
    ),
    CaseStatus.TRIAGED: (
        CaseStatus.ANALYZING,
        CaseStatus.CANCELLED,
        CaseStatus.RESOLVED,
    ),
    CaseStatus.ANALYZING: (
        CaseStatus.DIAGNOSED,
        CaseStatus.RESOLVED,
        CaseStatus.CANCELLED,
    ),
    CaseStatus.DIAGNOSED: (
        CaseStatus.REMEDIATION_READY,
        CaseStatus.RESOLVED,
        CaseStatus.CANCELLED,
    ),
    CaseStatus.REMEDIATION_READY: (
        CaseStatus.AUTHORIZED,
        CaseStatus.CANCELLED,
        CaseStatus.RESOLVED,
    ),
    CaseStatus.AUTHORIZED: (
        CaseStatus.EXECUTING,
        CaseStatus.CANCELLED,
        CaseStatus.RESOLVED,
    ),
    CaseStatus.EXECUTING: (
        CaseStatus.VERIFYING,
        CaseStatus.RESOLVED,
        CaseStatus.CANCELLED,
    ),
    CaseStatus.VERIFYING: (
        CaseStatus.RESOLVED,
        CaseStatus.EXECUTING,
        CaseStatus.CANCELLED,
    ),
    CaseStatus.RESOLVED: (CaseStatus.LEARNED, CaseStatus.CLOSED, CaseStatus.ANALYZING),
    CaseStatus.LEARNED: (CaseStatus.CLOSED,),
    CaseStatus.CLOSED: (),
    CaseStatus.CANCELLED: (),
}

#: Statuses a case can never leave.
TERMINAL_CASE_STATUSES = (CaseStatus.CLOSED, CaseStatus.CANCELLED)
#: Statuses that count as still being worked.
LIVE_CASE_STATUSES = tuple(
    status
    for status in CaseStatus
    if status not in TERMINAL_CASE_STATUSES and status != CaseStatus.LEARNED
)

#: The stage each case status maps to in the workflow engine (§11).
CASE_STATUS_TO_STAGE = {
    CaseStatus.OPEN: "DETECTED",
    CaseStatus.TRIAGED: "TRIAGED",
    CaseStatus.ANALYZING: "ANALYZING",
    CaseStatus.DIAGNOSED: "DIAGNOSED",
    CaseStatus.REMEDIATION_READY: "REMEDIATION_READY",
    CaseStatus.AUTHORIZED: "AUTHORIZED",
    CaseStatus.EXECUTING: "EXECUTING",
    CaseStatus.VERIFYING: "VERIFYING",
    CaseStatus.RESOLVED: "RESOLVED",
    CaseStatus.LEARNED: "LEARNED",
}


class CaseStateError(ValueError):
    """An illegal case transition or a malformed case operation."""


def _aware(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def can_transition(current: CaseStatus, target: CaseStatus) -> bool:
    """Whether a case may move from ``current`` to ``target`` (§14)."""
    if current == target:
        return False
    return target in CASE_STATUS_TRANSITIONS.get(current, ())


#: How many times ``open_case`` re-derives a reference after losing the
#: ``(project_id, reference)`` race. Two concurrent openers (the API sweep and the
#: scheduled sweep, or two replicas) are normal, so a single loss is expected;
#: a run of losses means something is wrong and the caller should see it.
REFERENCE_ATTEMPTS = 5


#: How many existing references are scanned when deriving the next one. A
#: project's case list is a long-lived operational record, so the scan is bounded
#: rather than unbounded; the bound is far above any realistic case count.
REFERENCE_SCAN_LIMIT = 5000

#: ``CASE-<n>`` — the reference format, and the only shape a number is read from.
_REFERENCE_PATTERN = re.compile(r"^CASE-(\d+)$")


def reference_number(reference: Optional[str]) -> int:
    """The numeric part of a ``CASE-<n>`` reference, or ``0`` if it has none.

    Parsing is the point: ``max(reference)`` is a *string* comparison, and
    ``'CASE-9' > 'CASE-10'``. Ordering by text therefore stops growing at nine,
    and every later open re-derives a reference that already exists — a bug the
    live platform gate caught as a duplicate-key violation on case opening
    (``CASE-10`` requested while ``CASE-10`` was already stored).
    """
    if not reference:
        return 0
    match = _REFERENCE_PATTERN.match(str(reference))
    return int(match.group(1)) if match else 0


async def next_reference(session: AsyncSession, *, project_id: uuid.UUID) -> str:
    """Allocate the next ``CASE-<n>`` reference for a project.

    Derived from the highest existing *number* rather than a sequence, because a
    reference that skips is confusing and a reference that repeats is a bug. The
    unique constraint on ``(project_id, reference)`` remains the real guarantee:
    two concurrent opens cannot both win, and losing that race is *handled*, not
    fatal — see :func:`open_case`.
    """
    stmt = (
        select(ReliabilityCase.reference)
        .where(ReliabilityCase.project_id == project_id)
        .limit(REFERENCE_SCAN_LIMIT)
    )
    existing = (await session.scalars(stmt)).all()
    highest = max((reference_number(ref) for ref in existing), default=0)
    return f"CASE-{highest + 1}"


async def open_case(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    trigger: CaseTrigger,
    title: str,
    environment_id: Optional[uuid.UUID] = None,
    summary: Optional[str] = None,
    severity: Optional[str] = None,
    incident_id: Optional[uuid.UUID] = None,
    primary_component_id: Optional[uuid.UUID] = None,
    component_ids: Optional[Sequence[uuid.UUID]] = None,
    source_type: Optional[str] = None,
    source_id: Optional[uuid.UUID] = None,
    opening_snapshot_id: Optional[uuid.UUID] = None,
    opened_by: Optional[str] = None,
    opened_at: Optional[datetime] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> ReliabilityCase:
    """Open a case for a situation, and record that it opened.

    The reference allocation and the insert are one attempt: a concurrent opener
    that claims the same ``CASE-<n>`` makes this insert raise, and the attempt is
    retried under a savepoint with a freshly derived reference. Nothing is lost —
    the rival's row is committed or not, and either way this call still returns a
    real case. Letting the collision escape instead would surface as a 500 and
    poison the caller's transaction; the *only* correct behaviours here are
    "opened a case" or a genuine, reported failure.
    """
    moment = _aware(opened_at)
    case: Optional[ReliabilityCase] = None
    last_error: Optional[Exception] = None
    for attempt in range(REFERENCE_ATTEMPTS):
        reference = await next_reference(session, project_id=project_id)
        candidate = ReliabilityCase(
            project_id=project_id,
            environment_id=environment_id,
            reference=reference,
            title=title[:500],
            summary=summary,
            status=CaseStatus.OPEN,
            trigger=trigger,
            severity=severity,
            primary_component_id=primary_component_id,
            component_ids=[str(c) for c in (component_ids or [])],
            incident_id=incident_id,
            source_type=source_type,
            source_id=source_id,
            opened_at=moment,
            opening_snapshot_id=opening_snapshot_id,
            opened_by=opened_by,
            status_changed_at=moment,
            status_changed_by=opened_by,
            metadata_=metadata,
        )
        try:
            #: A savepoint, so losing the race rolls back only this attempt: the
            #: caller's transaction keeps whatever the earlier steps of its pass
            #: already did.
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
        except IntegrityError as exc:
            last_error = exc
            logger.info(
                "case reference %s was taken for project %s; retrying (attempt %d)",
                reference,
                project_id,
                attempt + 1,
            )
            #: Give the winner a moment to commit, so the retry derives a
            #: reference past it rather than the same one again.
            await asyncio.sleep(min(1.0, 0.05 * (2**attempt)))
            continue
        case = candidate
        break
    if case is None:
        raise RuntimeError(
            f"could not allocate a case reference for project {project_id} "
            f"after {REFERENCE_ATTEMPTS} attempts"
        ) from last_error

    await append_timeline(
        session,
        case=case,
        kind=TimelineEntryKind.EVIDENCE,
        event_type="CASE_OPENED",
        title=f"Case {case.reference} opened",
        detail=title,
        source=trigger.value.lower(),
        evidence={
            "trigger": trigger.value,
            "incident_id": str(incident_id) if incident_id else None,
            "source_type": source_type,
            "source_id": str(source_id) if source_id else None,
        },
        component_id=primary_component_id,
        actor=opened_by,
        system_action=opened_by is None,
        occurred_at=moment,
        dedup_key=f"opened:{case.id}",
    )

    from app.services.platform_events import correlation_id_for, safely_publish_event

    await safely_publish_event(
        session,
        project_id=project_id,
        environment_id=environment_id,
        event_type=PlatformEventType.CASE_OPENED,
        source="reliability_case",
        subject_type="case",
        subject_id=case.id,
        component_id=primary_component_id,
        case_id=case.id,
        correlation_id=correlation_id_for(kind="case", subject_id=case.id),
        occurred_at=moment,
        payload={
            "reference": case.reference,
            "trigger": trigger.value,
            "incident_id": str(incident_id) if incident_id else None,
            "severity": severity,
        },
    )
    return case


async def append_timeline(
    session: AsyncSession,
    *,
    case: ReliabilityCase,
    kind: TimelineEntryKind,
    event_type: str,
    title: str,
    source: str,
    detail: Optional[str] = None,
    evidence: Optional[dict[str, Any]] = None,
    component_id: Optional[uuid.UUID] = None,
    actor: Optional[str] = None,
    system_action: bool = False,
    result: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
    dedup_key: Optional[str] = None,
) -> Optional[ReliabilityCaseTimeline]:
    """Append one entry to the unified timeline (§15).

    Idempotent on ``(case_id, dedup_key)``: the control plane is event-driven and
    events are replayed, so an entry that is written twice is a duplicated fact,
    not a second occurrence. Callers that have no natural key get one derived
    from the entry itself.
    """
    moment = _aware(occurred_at)
    key = dedup_key or _derive_dedup_key(
        case_id=case.id,
        event_type=event_type,
        title=title,
        source=source,
        occurred_at=moment,
    )
    existing = (
        await session.execute(
            select(ReliabilityCaseTimeline).where(
                ReliabilityCaseTimeline.case_id == case.id,
                ReliabilityCaseTimeline.dedup_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None

    next_sequence = (
        await session.scalar(
            select(func.max(ReliabilityCaseTimeline.sequence)).where(
                ReliabilityCaseTimeline.case_id == case.id
            )
        )
        or 0
    ) + 1
    entry = ReliabilityCaseTimeline(
        case_id=case.id,
        project_id=case.project_id,
        sequence=next_sequence,
        occurred_at=moment,
        kind=kind,
        event_type=event_type,
        title=title[:500],
        detail=detail,
        component_id=component_id,
        source=source,
        evidence=evidence,
        actor=actor,
        system_action=system_action,
        result=result,
        dedup_key=key,
    )
    session.add(entry)
    await session.flush()
    return entry


def _derive_dedup_key(
    *,
    case_id: uuid.UUID,
    event_type: str,
    title: str,
    source: str,
    occurred_at: datetime,
) -> str:
    import hashlib

    raw = f"{case_id}|{event_type}|{source}|{title}|{occurred_at.isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


async def transition_case(
    session: AsyncSession,
    *,
    case: ReliabilityCase,
    target: CaseStatus,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
    evidence: Optional[dict[str, Any]] = None,
    occurred_at: Optional[datetime] = None,
    enforce: bool = True,
) -> ReliabilityCase:
    """Move a case to a new status, or refuse (§14).

    ``enforce=False`` is used by the control plane when it mirrors an *external*
    fact that has already happened — an incident resolving on its own, for
    instance. Even then the timeline records the transition, so a mirrored move
    is visible rather than silent.
    """
    moment = _aware(occurred_at)
    if case.status == target:
        return case
    if enforce and not can_transition(case.status, target):
        raise CaseStateError(
            f"illegal case transition: {case.status.value} -> {target.value}"
        )
    previous = case.status
    case.status = target
    case.status_changed_at = moment
    case.status_changed_by = actor
    if target in (CaseStatus.RESOLVED, CaseStatus.CLOSED, CaseStatus.CANCELLED):
        case.closed_at = moment
    await session.flush()

    await append_timeline(
        session,
        case=case,
        kind=TimelineEntryKind.STATE_CHANGE,
        event_type="CASE_STATUS_CHANGED",
        title=f"Case status changed to {target.value}",
        detail=reason,
        source="reliability_case",
        evidence={"previous_status": previous.value, **(evidence or {})},
        actor=actor,
        system_action=actor is None,
        result=target.value,
        occurred_at=moment,
        dedup_key=f"status:{previous.value}->{target.value}:{moment.isoformat()}",
    )

    from app.services.platform_events import correlation_id_for, safely_publish_event

    event_type = (
        PlatformEventType.CASE_CLOSED
        if target in TERMINAL_CASE_STATUSES
        else PlatformEventType.CASE_STATUS_CHANGED
    )
    await safely_publish_event(
        session,
        project_id=case.project_id,
        environment_id=case.environment_id,
        event_type=event_type,
        source="reliability_case",
        subject_type="case",
        subject_id=case.id,
        component_id=case.primary_component_id,
        case_id=case.id,
        correlation_id=correlation_id_for(kind="case", subject_id=case.id),
        occurred_at=moment,
        payload={
            "reference": case.reference,
            "previous_status": previous.value,
            "status": target.value,
            "reason": reason,
        },
    )
    return case


async def find_open_case_for_incident(
    session: AsyncSession, *, incident_id: uuid.UUID
) -> Optional[ReliabilityCase]:
    """The live case about this incident, when one exists (§14 dedup)."""
    stmt = (
        select(ReliabilityCase)
        .where(
            ReliabilityCase.incident_id == incident_id,
            ReliabilityCase.status.in_(list(LIVE_CASE_STATUSES)),
        )
        .order_by(ReliabilityCase.opened_at.desc())
        .limit(1)
    )
    return (await session.scalars(stmt)).first()


async def get_case(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
) -> Optional[ReliabilityCase]:
    """Load a case, enforcing project scope when one is supplied (§42)."""
    case = await session.get(ReliabilityCase, case_id)
    if case is None:
        return None
    if project_id is not None and case.project_id != project_id:
        return None
    return case


async def list_cases(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    statuses: Optional[Sequence[CaseStatus]] = None,
    environment_id: Optional[uuid.UUID] = None,
    component_id: Optional[uuid.UUID] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ReliabilityCase]:
    """Cases for a project, newest first."""
    stmt = (
        select(ReliabilityCase)
        .where(ReliabilityCase.project_id == project_id)
        .order_by(ReliabilityCase.opened_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if statuses:
        stmt = stmt.where(ReliabilityCase.status.in_(list(statuses)))
    if environment_id is not None:
        stmt = stmt.where(ReliabilityCase.environment_id == environment_id)
    return list((await session.scalars(stmt)).all())


async def case_timeline(
    session: AsyncSession, *, case_id: uuid.UUID, limit: int = 500
) -> list[ReliabilityCaseTimeline]:
    """The unified timeline, in order."""
    stmt = (
        select(ReliabilityCaseTimeline)
        .where(ReliabilityCaseTimeline.case_id == case_id)
        .order_by(ReliabilityCaseTimeline.sequence)
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


@dataclass
class CaseEvidence:
    """Everything linked to a case, grouped by the phase that owns it (§16).

    Assembled by reference, never copied: each entry carries the owning row's id
    and the little that makes it readable, so the case view can link back to the
    subsystem that is authoritative about it.
    """

    metrics: list[dict[str, Any]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    traces: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    incidents: list[dict[str, Any]] = field(default_factory=list)
    deployments: list[dict[str, Any]] = field(default_factory=list)
    commits: list[dict[str, Any]] = field(default_factory=list)
    analyses: list[dict[str, Any]] = field(default_factory=list)
    reproductions: list[dict[str, Any]] = field(default_factory=list)
    predictions: list[dict[str, Any]] = field(default_factory=list)
    remediations: list[dict[str, Any]] = field(default_factory=list)
    patches: list[dict[str, Any]] = field(default_factory=list)
    knowledge: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics,
            "logs": self.logs,
            "traces": self.traces,
            "anomalies": self.anomalies,
            "incidents": self.incidents,
            "deployments": self.deployments,
            "commits": self.commits,
            "analyses": self.analyses,
            "reproductions": self.reproductions,
            "predictions": self.predictions,
            "remediations": self.remediations,
            "patches": self.patches,
            "knowledge": self.knowledge,
            "gaps": self.gaps,
        }


async def collect_case_evidence(
    session: AsyncSession, *, case: ReliabilityCase
) -> CaseEvidence:
    """Gather the case's linked evidence by querying the owning phases (§16).

    Every lookup is best-effort and bounded. A phase that cannot answer adds a
    line to ``gaps`` rather than failing the case view — §59's graceful
    degradation applies to the read path too, and a missing section is information
    a reader needs rather than an error to hide.
    """
    from app.models.anomaly import Anomaly
    from app.models.causal import CausalAnalysis, RootCauseCandidate
    from app.models.deployment import DeploymentEvent
    from app.models.fix import FixHypothesis, Patch
    from app.models.incident import Incident
    from app.models.reliability import ReliabilityForecast
    from app.models.remediation import RemediationAction
    from app.models.reproduction import ReproductionExperiment

    evidence = CaseEvidence()
    component_ids = [uuid.UUID(str(c)) for c in (case.component_ids or []) if c] or (
        [case.primary_component_id] if case.primary_component_id else []
    )
    #: The window reaches *backwards* from the open time (see the config note on
    #: ``PLATFORM_CASE_EVIDENCE_LOOKBACK_SECONDS``). A case is opened to
    #: investigate something that has already happened, so a window starting at
    #: ``opened_at`` would omit the anomaly and deployment that caused it.
    settings = get_settings()
    window_start = _aware(case.opened_at) - timedelta(
        seconds=settings.PLATFORM_CASE_EVIDENCE_LOOKBACK_SECONDS
    )
    window_end = _aware(case.closed_at) or datetime.now(timezone.utc)

    # -- the incident itself
    if case.incident_id:
        incident = await session.get(Incident, case.incident_id)
        if incident is not None:
            evidence.incidents.append(
                {
                    "id": str(incident.id),
                    "title": incident.title,
                    "severity": getattr(
                        incident.severity, "value", str(incident.severity)
                    ),
                    "status": getattr(incident.status, "value", str(incident.status)),
                    "detected_at": _aware(incident.detected_at).isoformat(),
                    "resolved_at": _aware(incident.resolved_at).isoformat()
                    if incident.resolved_at
                    else None,
                }
            )
        else:
            evidence.gaps.append(
                "the incident this case was opened for no longer exists"
            )
    else:
        evidence.incidents = [
            {
                "id": str(row.id),
                "title": row.title,
                "severity": getattr(row.severity, "value", str(row.severity)),
                "status": getattr(row.status, "value", str(row.status)),
                "detected_at": _aware(row.detected_at).isoformat(),
                "resolved_at": _aware(row.resolved_at).isoformat()
                if row.resolved_at
                else None,
            }
            for row in (
                await session.scalars(
                    select(Incident)
                    .where(
                        Incident.project_id == case.project_id,
                        Incident.primary_component_id.in_(component_ids),
                        Incident.detected_at >= window_start,
                        Incident.detected_at <= window_end,
                    )
                    .order_by(Incident.detected_at)
                    .limit(50)
                )
            ).all()
        ]

    # -- anomalies on the case's components inside its window
    anomaly_rows = (
        await session.scalars(
            select(Anomaly)
            .where(
                Anomaly.project_id == case.project_id,
                Anomaly.component_id.in_(component_ids),
                Anomaly.detected_at >= window_start,
                Anomaly.detected_at <= window_end,
            )
            .order_by(Anomaly.detected_at)
            .limit(100)
        )
    ).all()
    for anomaly in anomaly_rows:
        evidence.anomalies.append(
            {
                "id": str(anomaly.id),
                "anomaly_type": getattr(
                    anomaly.anomaly_type, "value", str(anomaly.anomaly_type)
                ),
                "severity": getattr(anomaly.severity, "value", str(anomaly.severity)),
                "status": getattr(anomaly.status, "value", str(anomaly.status)),
                "metric_name": anomaly.metric_name,
                "detected_at": _aware(anomaly.detected_at).isoformat(),
            }
        )

    # -- deployments in the window (change evidence)
    deployment_rows = (
        await session.scalars(
            select(DeploymentEvent)
            .where(
                DeploymentEvent.project_id == case.project_id,
                DeploymentEvent.deployed_at >= window_start,
                DeploymentEvent.deployed_at <= window_end,
            )
            .order_by(DeploymentEvent.deployed_at)
            .limit(50)
        )
    ).all()
    for deployment in deployment_rows:
        evidence.deployments.append(
            {
                "id": str(deployment.id),
                "version": getattr(deployment, "version", None),
                "commit_sha": getattr(deployment, "commit_sha", None),
                "status": getattr(getattr(deployment, "status", None), "value", None),
                "deployed_at": _aware(deployment.deployed_at).isoformat(),
            }
        )

    # -- causal analyses for the case's incident
    if case.incident_id:
        analysis_rows = (
            await session.scalars(
                select(CausalAnalysis)
                .where(
                    CausalAnalysis.project_id == case.project_id,
                    CausalAnalysis.incident_id == case.incident_id,
                )
                .order_by(CausalAnalysis.created_at.desc())
                .limit(10)
            )
        ).all()
        for analysis in analysis_rows:
            candidates = (
                await session.scalars(
                    select(RootCauseCandidate)
                    .where(RootCauseCandidate.analysis_id == analysis.id)
                    .order_by(RootCauseCandidate.score.desc())
                    .limit(10)
                )
            ).all()
            evidence.analyses.append(
                {
                    "id": str(analysis.id),
                    "status": getattr(analysis.status, "value", str(analysis.status)),
                    "created_at": _aware(analysis.created_at).isoformat(),
                    "candidates": [
                        {
                            "id": str(candidate.id),
                            "candidate_type": getattr(
                                candidate.candidate_type,
                                "value",
                                str(candidate.candidate_type),
                            ),
                            "status": getattr(
                                candidate.status, "value", str(candidate.status)
                            ),
                            "score": candidate.score,
                            "confidence": getattr(candidate.confidence, "value", None)
                            if candidate.confidence
                            else None,
                            "component_id": str(candidate.component_id)
                            if getattr(candidate, "component_id", None)
                            else None,
                        }
                        for candidate in candidates
                    ],
                }
            )

        # -- reproduction experiments for the same incident
        experiment_rows = (
            await session.scalars(
                select(ReproductionExperiment)
                .where(
                    ReproductionExperiment.project_id == case.project_id,
                    ReproductionExperiment.incident_id == case.incident_id,
                )
                .order_by(ReproductionExperiment.created_at.desc())
                .limit(10)
            )
        ).all()
        for experiment in experiment_rows:
            evidence.reproductions.append(
                {
                    "id": str(experiment.id),
                    "status": getattr(
                        experiment.status, "value", str(experiment.status)
                    ),
                    "result": getattr(experiment.result, "value", None)
                    if experiment.result
                    else None,
                    "confidence": experiment.confidence,
                    "created_at": _aware(experiment.created_at).isoformat(),
                }
            )

        # -- hypotheses and their patches (Phase 7)
        hypothesis_rows = (
            await session.scalars(
                select(FixHypothesis)
                .where(FixHypothesis.project_id == case.project_id)
                .order_by(FixHypothesis.created_at.desc())
                .limit(20)
            )
        ).all()
        for hypothesis in hypothesis_rows:
            patch_rows = (
                await session.scalars(
                    select(Patch)
                    .where(Patch.fix_hypothesis_id == hypothesis.id)
                    .order_by(Patch.created_at.desc())
                    .limit(10)
                )
            ).all()
            evidence.patches.append(
                {
                    "hypothesis_id": str(hypothesis.id),
                    "statement": hypothesis.description,
                    "proposed_change": hypothesis.proposed_change,
                    "status": getattr(
                        hypothesis.status, "value", str(hypothesis.status)
                    ),
                    "patches": [
                        {
                            "id": str(patch.id),
                            "status": getattr(patch.status, "value", str(patch.status)),
                            "changed_files": patch.changed_files,
                            "lines_added": patch.lines_added,
                            "lines_removed": patch.lines_removed,
                        }
                        for patch in patch_rows
                    ],
                }
            )

    # -- forecasts for the case's components
    forecast_rows = (
        await session.scalars(
            select(ReliabilityForecast)
            .where(
                ReliabilityForecast.project_id == case.project_id,
                ReliabilityForecast.component_id.in_(component_ids),
            )
            .order_by(ReliabilityForecast.generated_at.desc())
            .limit(25)
        )
    ).all()
    for forecast in forecast_rows:
        evidence.predictions.append(
            {
                "id": str(forecast.id),
                "prediction_type": getattr(
                    forecast.prediction_type, "value", str(forecast.prediction_type)
                ),
                "risk_level": getattr(
                    forecast.risk_level, "value", str(forecast.risk_level)
                ),
                "risk_score": forecast.risk_score,
                "confidence": forecast.confidence,
                "generated_at": _aware(forecast.generated_at).isoformat(),
            }
        )

    # -- remediation actions touching the case
    remediation_stmt = select(RemediationAction).where(
        RemediationAction.project_id == case.project_id
    )
    if case.incident_id:
        remediation_stmt = remediation_stmt.where(
            RemediationAction.incident_id == case.incident_id
        )
    else:
        remediation_stmt = remediation_stmt.where(
            RemediationAction.component_id.in_(component_ids),
            RemediationAction.created_at >= window_start,
            RemediationAction.created_at <= window_end,
        )
    for action in (
        await session.scalars(
            remediation_stmt.order_by(RemediationAction.created_at.desc()).limit(25)
        )
    ).all():
        evidence.remediations.append(
            {
                "id": str(action.id),
                "action_type": getattr(
                    action.action_type, "value", str(action.action_type)
                ),
                "status": getattr(action.status, "value", str(action.status)),
                "outcome": getattr(action.outcome, "value", None)
                if action.outcome
                else None,
                "execution_mode": getattr(
                    action.execution_mode, "value", str(action.execution_mode)
                ),
                "created_at": _aware(action.created_at).isoformat(),
            }
        )

    # -- historical knowledge retrieved for this situation (Phase 10, read-only)
    try:
        from app.services.experience_retrieval import ExperienceRetrievalService

        if case.incident_id:
            result = await ExperienceRetrievalService().retrieve_for_incident(
                session,
                incident_id=case.incident_id,
                project_id=case.project_id,
                limit=5,
            )
            evidence.knowledge = [match.as_dict() for match in result.matches]
        else:
            evidence.knowledge = []
            evidence.gaps.append(
                "no incident is linked to this case, so historical retrieval has "
                "no situation to match against"
            )
    except Exception:  # pragma: no cover - learning is optional (§59)
        evidence.gaps.append(
            "historical intelligence unavailable: the learning layer did not answer"
        )
    return evidence


async def case_summary(
    session: AsyncSession, *, case: ReliabilityCase
) -> dict[str, Any]:
    """The compact view of a case used in lists and in the assistant's context."""
    timeline = await case_timeline(session, case_id=case.id, limit=500)
    return {
        "id": str(case.id),
        "reference": case.reference,
        "title": case.title,
        "summary": case.summary,
        "status": case.status.value,
        "trigger": case.trigger.value,
        "severity": case.severity,
        "project_id": str(case.project_id),
        "environment_id": str(case.environment_id) if case.environment_id else None,
        "primary_component_id": str(case.primary_component_id)
        if case.primary_component_id
        else None,
        "component_ids": list(case.component_ids or []),
        "incident_id": str(case.incident_id) if case.incident_id else None,
        "opened_at": _aware(case.opened_at).isoformat(),
        "closed_at": _aware(case.closed_at).isoformat() if case.closed_at else None,
        "opened_by": case.opened_by,
        "duration_seconds": (
            (_aware(case.closed_at) - _aware(case.opened_at)).total_seconds()
            if case.closed_at
            else (datetime.now(timezone.utc) - _aware(case.opened_at)).total_seconds()
        ),
        "timeline_entries": len(timeline),
        "last_event_at": _aware(timeline[-1].occurred_at).isoformat()
        if timeline
        else None,
        "allowed_transitions": [
            status.value for status in CASE_STATUS_TRANSITIONS.get(case.status, ())
        ],
    }


__all__ = [
    "CASE_STATUS_TO_STAGE",
    "CASE_STATUS_TRANSITIONS",
    "LIVE_CASE_STATUSES",
    "TERMINAL_CASE_STATUSES",
    "CaseEvidence",
    "CaseStateError",
    "append_timeline",
    "can_transition",
    "case_summary",
    "case_timeline",
    "collect_case_evidence",
    "find_open_case_for_incident",
    "get_case",
    "list_cases",
    "next_reference",
    "open_case",
    "reference_number",
    "transition_case",
]
