"""ARGUS Learning Run (Phase 10 §7, §27–§29, §63, §79).

The pipeline, in the order the phase specifies:

```text
LearningEvent            (already recorded by Phases 3–9)
      ↓  validation + normalization
ReliabilityExperience    (experience_builder, §8)
      ↓  feature extraction
pattern mining           (pattern_miners, §17–§22)
      ↓  evaluation
knowledge validation     (knowledge_validation, §32–§37)
      ↓  candidate
knowledge lifecycle      (knowledge_lifecycle, §4, §26, §71–§74)
      ↓
profiles, recommendations, decay, expiry
```

Guarantees this module is responsible for:

* **Incremental by default** (§28). Events are consumed once, marked with the run
  that consumed them, and a checkpoint (``last_processed_at`` on the run row)
  records how far the pipeline has read. Re-running a completed run finds nothing
  new and changes nothing.
* **Bounded work** (§29, §33). Every query is limited by
  ``INTELLIGENCE_LEARNING_BATCH``/``INTELLIGENCE_MAX_PATTERNS_PER_RUN``, so a
  project with a million rows pages through instead of timing out.
* **Auditable** (§79). The run row records the cutoff, the trigger, what was
  consumed, how many patterns were found, validated and rejected, and the
  algorithm versions used. A failed run leaves a FAILED row with its error rather
  than disappearing.
* **Never fatal to the platform.** :func:`safely_execute_learning_run` swallows
  nothing silently, but it also never propagates into a caller's request path —
  learning is a consumer of history, not a dependency of it.

Raw events never become active knowledge: they become experiences, which become
candidates, which are validated, which may be activated. Each step is a separate
write with its own record.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.intelligence import (
    LearningEvent,
    LearningEventType,
    LearningRun,
    LearningRunStatus,
    ReliabilityExperience,
    ReliabilityKnowledge,
)
from app.models.project import SoftwareProject
from app.services.component_profiles import recompute_profiles
from app.services.experience_builder import build_experiences
from app.services.intelligence_state import assert_run_transition
from app.services.knowledge_lifecycle import record_candidate, refresh_staleness
from app.services.knowledge_validation import KnowledgeValidationService
from app.services.learning_events import (
    claim_unprocessed_events,
    enabled_event_types,
    mark_event_processed,
    trusted_provenance,
)
from app.services.learning_signatures import FEATURE_SCHEMA_VERSION
from app.services.pattern_miners import (
    ALGORITHM_VERSION,
    ExperienceCorpus,
    load_corpus,
)

logger = logging.getLogger(__name__)


@dataclass
class LearningRunSummary:
    """The ledger of one run (§27, §98)."""

    run_id: Optional[str] = None
    status: LearningRunStatus = LearningRunStatus.QUEUED
    projects: list[str] = field(default_factory=list)
    events_processed: int = 0
    experiences_created: int = 0
    experiences_updated: int = 0
    experiences_flagged: int = 0
    patterns_discovered: int = 0
    patterns_validated: int = 0
    patterns_rejected: int = 0
    knowledge_created: int = 0
    knowledge_updated: int = 0
    knowledge_activated: int = 0
    knowledge_deprecated: int = 0
    recommendations_created: int = 0
    recommendations_expired: int = 0
    relationships_created: int = 0
    relationships_updated: int = 0
    relationships_stale: int = 0
    skipped_reasons: list[str] = field(default_factory=list)
    unprocessable: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "projects": list(self.projects),
            "events_processed": self.events_processed,
            "experiences_created": self.experiences_created,
            "experiences_updated": self.experiences_updated,
            "experiences_flagged": self.experiences_flagged,
            "patterns_discovered": self.patterns_discovered,
            "patterns_validated": self.patterns_validated,
            "patterns_rejected": self.patterns_rejected,
            "knowledge_created": self.knowledge_created,
            "knowledge_updated": self.knowledge_updated,
            "knowledge_activated": self.knowledge_activated,
            "knowledge_deprecated": self.knowledge_deprecated,
            "recommendations_created": self.recommendations_created,
            "recommendations_expired": self.recommendations_expired,
            "relationships_created": self.relationships_created,
            "relationships_updated": self.relationships_updated,
            "relationships_stale": self.relationships_stale,
            "skipped_reasons": list(self.skipped_reasons),
            "unprocessable": dict(self.unprocessable),
            "errors": list(self.errors),
        }


async def create_run(
    session: AsyncSession,
    *,
    project_id: Optional[uuid.UUID],
    trigger: str,
    cutoff: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> LearningRun:
    """Create a QUEUED run row with its cutoff (§27, §31)."""
    moment = now or datetime.now(timezone.utc)
    run = LearningRun(
        project_id=project_id,
        status=LearningRunStatus.QUEUED,
        trigger=trigger,
        data_cutoff=_aware(cutoff) or moment,
        started_at=moment,
        algorithm_versions={
            "pattern_miners": ALGORITHM_VERSION,
            "feature_schema": FEATURE_SCHEMA_VERSION,
        },
    )
    session.add(run)
    await session.flush()
    return run


async def execute_learning_run(
    session: AsyncSession,
    *,
    project_id: Optional[uuid.UUID] = None,
    trigger: str = "manual",
    cutoff: Optional[datetime] = None,
    run_id: Optional[uuid.UUID] = None,
    lookback_days: Optional[int] = None,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
    generate_recommendations: bool = True,
) -> LearningRunSummary:
    """Run the whole pipeline once, incrementally and auditably."""
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    boundary = _aware(cutoff) or moment
    summary = LearningRunSummary()

    run = (
        await session.get(LearningRun, run_id)
        if run_id is not None
        else await create_run(
            session, project_id=project_id, trigger=trigger, cutoff=boundary, now=moment
        )
    )
    assert run is not None
    if run.status == LearningRunStatus.QUEUED:
        assert_run_transition(run.status, LearningRunStatus.RUNNING)
        run.status = LearningRunStatus.RUNNING
    run.started_at = run.started_at or moment
    summary.run_id = str(run.id)
    await session.flush()

    try:
        projects = await _target_projects(
            session,
            project_id=run.project_id,
            cutoff=boundary,
            limit=settings.INTELLIGENCE_LEARNING_BATCH,
        )
        summary.projects = [str(item) for item in projects]
        if not projects:
            summary.skipped_reasons.append("no_projects_with_history")

        for target in projects:
            await _consume_events(
                session,
                project_id=target,
                run=run,
                boundary=boundary,
                summary=summary,
                settings=settings,
            )

            #: Profiles are computed *before* mining, because the §21/§22
            #: chronic-component miner reads them. Computing them afterwards
            #: still works — one run later — which is the kind of ordering bug
            #: nobody notices: the signal and the knowledge about it would never
            #: appear in the same run.
            await recompute_profiles(
                session, project_id=target, now=boundary, settings=settings
            )

            corpus = await load_corpus(
                session,
                project_id=target,
                cutoff=boundary,
                lookback_days=lookback_days,
                limit=settings.INTELLIGENCE_LEARNING_BATCH * 4,
            )
            await _mine_and_record(
                session,
                corpus=corpus,
                run=run,
                boundary=boundary,
                summary=summary,
                settings=settings,
            )
            #: §23. Learned relationships come after mining, because the edges
            #: describe the same corpus the patterns were mined from: one window
            #: of history, two kinds of derived artifact. They are written to
            #: ``intelligence_relationships``, never to ``graph_edges`` (§24).
            await _derive_relationships(
                session,
                project_id=target,
                run=run,
                boundary=boundary,
                lookback_days=lookback_days,
                summary=summary,
                settings=settings,
            )
            if generate_recommendations:
                await _generate_recommendations(
                    session,
                    project_id=target,
                    boundary=boundary,
                    summary=summary,
                    settings=settings,
                )
            await _reconcile_experience_signatures(
                session, project_id=target, corpus=corpus
            )

        retired = await refresh_staleness(
            session, project_id=run.project_id, settings=settings, now=boundary
        )
        summary.knowledge_deprecated = len(retired)

        run.status = LearningRunStatus.COMPLETED
        run.completed_at = datetime.now(timezone.utc)
        run.last_processed_at = boundary
        _apply_summary(run, summary)
        await session.flush()
        summary.status = LearningRunStatus.COMPLETED
        return summary

    except Exception as exc:  # pragma: no cover - exercised by failure tests
        logger.exception("learning run failed")
        run.status = LearningRunStatus.FAILED
        run.completed_at = datetime.now(timezone.utc)
        run.error_summary = f"{type(exc).__name__}: {exc}"[:2000]
        _apply_summary(run, summary)
        summary.status = LearningRunStatus.FAILED
        summary.errors.append(str(exc))
        await session.flush()
        raise


async def safely_execute_learning_run(
    session: AsyncSession,
    **kwargs: Any,
) -> LearningRunSummary:
    """Run the pipeline, converting a failure into a reported summary.

    Used by the sweep: a broken run must be recorded and retried later, not
    allowed to kill the scheduler that runs it.
    """
    try:
        return await execute_learning_run(session, **kwargs)
    except Exception as exc:
        logger.warning("learning run failed (recorded on the run row)", exc_info=True)
        #: §79. The failed run *was* recorded before the exception propagated;
        #: reporting a failure with no id would leave the audit chain broken
        #: exactly when it matters most.
        failed = await _latest_failed_run(session, project_id=kwargs.get("project_id"))
        return LearningRunSummary(
            run_id=str(failed.id) if failed is not None else None,
            status=LearningRunStatus.FAILED,
            errors=[str(exc)],
        )


async def _latest_failed_run(
    session: AsyncSession, *, project_id: Optional[uuid.UUID]
) -> Optional[LearningRun]:
    """The most recent run recorded as failed, for the failure summary."""
    stmt = (
        select(LearningRun)
        .where(LearningRun.status == LearningRunStatus.FAILED)
        .order_by(LearningRun.started_at.desc())
        .limit(1)
    )
    if project_id is not None:
        stmt = stmt.where(LearningRun.project_id == project_id)
    return await session.scalar(stmt)


async def _target_projects(
    session: AsyncSession,
    *,
    project_id: Optional[uuid.UUID],
    cutoff: datetime,
    limit: int,
) -> list[uuid.UUID]:
    """Which projects this run should process (§28).

    A single-project run is explicit. A global run processes the projects that
    have *unconsumed* events, plus any project that already has knowledge to
    re-confirm — so periodic runs keep patterns fresh without a full scan.
    """
    if project_id is not None:
        return [project_id]
    event_projects = set(
        (
            await session.scalars(
                select(LearningEvent.project_id)
                .where(LearningEvent.processed_at.is_(None))
                .where(LearningEvent.occurred_at <= cutoff)
                .limit(limit)
            )
        ).all()
    )
    experience_projects = set(
        (
            await session.scalars(select(ReliabilityExperience.project_id).limit(limit))
        ).all()
    )
    return sorted(event_projects | experience_projects)


async def _consume_events(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run: LearningRun,
    boundary: datetime,
    summary: LearningRunSummary,
    settings: Settings,
) -> None:
    """Turn unprocessed events into experiences, once each (§6, §64)."""
    enabled = await enabled_event_types(session, project_id=project_id)
    trusted = await trusted_provenance(session, project_id=project_id)
    events = await claim_unprocessed_events(
        session,
        project_id=project_id,
        cutoff=boundary,
        limit=settings.INTELLIGENCE_LEARNING_BATCH,
        trusted=trusted,
    )
    if not events:
        return

    #: An event whose type the operator disabled is *recorded as skipped*, not
    #: silently dropped: coverage is part of what the run reports (§79).
    events = list(events)
    processed_by_type: dict[str, list[LearningEvent]] = {}
    for event in events:
        event_type = getattr(event.event_type, "value", str(event.event_type))
        if event.event_type not in enabled:
            await mark_event_processed(
                session, event, run_id=run.id, reason="event_type_disabled"
            )
            summary.skipped_reasons.append("event_type_disabled")
            continue
        processed_by_type.setdefault(event_type, []).append(event)

    #: Every event is mapped to the incident whose episode it refreshes. The
    #: subject of a learning event is the row the event is *about* — an incident
    #: resolution names the incident, a remediation names the action — so this is
    #: the one place that knows how to walk from one to the other. Without it a
    #: verified remediation or a rollback would be recorded as an event about an
    #: incident that does not exist and then discarded, which is exactly how
    #: "learn from remediation" turns into "never learns from remediation".
    targets = await _resolve_incident_targets(
        session, events=events, project_id=project_id
    )

    #: Distinct incidents only: two events (a resolution and a rollback) about one
    #: incident is one experience.
    unique_targets: list[uuid.UUID] = []
    seen: set[str] = set()
    for target in targets.values():
        if target.incident_id is None:
            continue
        key = str(target.incident_id)
        if key in seen:
            continue
        seen.add(key)
        unique_targets.append(target.incident_id)

    result = await build_experiences(
        session,
        incident_ids=unique_targets,
        cutoff=boundary,
        learning_run_id=run.id,
    )
    summary.experiences_created += len(result.created)
    summary.experiences_updated += len(result.updated)
    summary.experiences_flagged += result.flagged
    summary.unprocessable.update(result.unprocessable)

    for event in [item for group in processed_by_type.values() for item in group]:
        event_target: Optional[_EventTarget] = targets.get(str(event.id))
        if event_target is None:
            #: Hypotheses and predictions are read by the miners from their own
            #: rows; they refresh no episode, and the ledger says so.
            reason: Optional[str] = "not_incident_scoped"
        elif target.incident_id is None:
            #: It *should* name an episode but does not: the incident was
            #: deleted, belongs to another project, or the row it hangs off is
            #: gone. Visible with its own reason rather than counted as work.
            reason = event_target.reason or "unresolvable_subject"
            summary.unprocessable.setdefault(str(event.subject_id), reason)
        else:
            incident_id = event_target.incident_id
            reason = result.unprocessable.get(str(incident_id))
            if reason is None and not any(
                str(row.incident_id) == str(incident_id)
                for row in [*result.created, *result.updated]
            ):
                reason = "experience_unchanged"
        await mark_event_processed(session, event, run_id=run.id, reason=reason)
        summary.events_processed += 1


#: Events whose subject *is* the incident.
_INCIDENT_SUBJECT_EVENTS = frozenset({LearningEventType.INCIDENT_RESOLVED})

#: Events about a row that belongs to an incident, and the walk from that row to
#: the incident: each step is ``(model, foreign key)``. A remediation names
#: itself; a patch names its hypothesis; a root-cause decision names its
#: analysis. Encoded as data so a new event type is one line, not one more
#: branch.
_INCIDENT_PATHS: dict[Any, tuple[tuple[Any, str], ...]] = {}


def _incident_paths() -> dict[Any, tuple[tuple[Any, str], ...]]:
    """The walk table, built lazily so imports stay at the top of the module."""
    if _INCIDENT_PATHS:
        return _INCIDENT_PATHS
    from app.models.causal import CausalAnalysis, RootCauseCandidate
    from app.models.fix import FixHypothesis, Patch
    from app.models.remediation import RemediationAction
    from app.models.reproduction import ReproductionExperiment

    for event_type in (
        LearningEventType.REMEDIATION_COMPLETED,
        LearningEventType.ROLLBACK_COMPLETED,
    ):
        _INCIDENT_PATHS[event_type] = ((RemediationAction, "incident_id"),)
    for event_type in (
        LearningEventType.REPRODUCTION_CONFIRMED,
        LearningEventType.REPRODUCTION_FAILED,
    ):
        _INCIDENT_PATHS[event_type] = ((ReproductionExperiment, "incident_id"),)
    for event_type in (
        LearningEventType.PATCH_VERIFIED,
        LearningEventType.PATCH_REGRESSION_DETECTED,
    ):
        _INCIDENT_PATHS[event_type] = (
            (Patch, "fix_hypothesis_id"),
            (FixHypothesis, "incident_id"),
        )
    for event_type in (
        LearningEventType.ROOT_CAUSE_CONFIRMED,
        LearningEventType.ROOT_CAUSE_REJECTED,
    ):
        _INCIDENT_PATHS[event_type] = (
            (RootCauseCandidate, "analysis_id"),
            (CausalAnalysis, "incident_id"),
        )
    return _INCIDENT_PATHS


@dataclass(frozen=True)
class _EventTarget:
    """The episode an event refreshes, or why none could be named."""

    incident_id: Optional[uuid.UUID] = None
    reason: Optional[str] = None


async def _resolve_incident_targets(
    session: AsyncSession,
    *,
    events: list[LearningEvent],
    project_id: uuid.UUID,
) -> dict[str, _EventTarget]:
    """Event id → the incident whose episode the event refreshes.

    The ``payload`` shortcut is tried first (the emitting phase already knew the
    incident and recorded it), then the walk table is followed through the rows.

    Both paths are project-scoped. A payload is data written by a caller, so an
    id in it is only believed once the incident it names is confirmed to belong
    to this project: otherwise one project's event could refresh — and thereby
    teach ARGUS about — another project's episode.

    Events that name no episode at all are simply absent from the result; events
    that *should* name one but do not carry the reason (``incident_not_found``
    for a deleted or foreign incident, ``unresolvable_subject`` for a subject
    row that is gone).
    """
    from app.models.fix import Patch
    from app.models.incident import Incident
    from app.models.remediation import RemediationAction
    from app.models.reproduction import ReproductionExperiment

    paths = _incident_paths()
    resolved: dict[str, _EventTarget] = {}
    candidate_ids: set[uuid.UUID] = set()

    scoped_models: dict[Any, str] = {
        RemediationAction: "project_id",
        ReproductionExperiment: "project_id",
        Patch: "project_id",
    }

    for event in events:
        event_type = event.event_type
        if event_type in _INCIDENT_SUBJECT_EVENTS:
            candidate_ids.add(event.subject_id)
            resolved[str(event.id)] = _EventTarget(incident_id=event.subject_id)
            continue
        steps = paths.get(event_type)
        if steps is None:
            continue
        payload_incident = _payload_uuid(event.payload, "incident_id")
        if payload_incident is not None:
            candidate_ids.add(payload_incident)
            resolved[str(event.id)] = _EventTarget(incident_id=payload_incident)
            continue
        current_id: Optional[uuid.UUID] = event.subject_id
        for model, column in steps:
            if current_id is None:
                break
            stmt = select(getattr(model, column)).where(model.id == current_id)
            scope_column = scoped_models.get(model)
            if scope_column is not None:
                stmt = stmt.where(getattr(model, scope_column) == project_id)
            value = await session.scalar(stmt.limit(1))
            current_id = _as_uuid(value)
        if current_id is None:
            resolved[str(event.id)] = _EventTarget(reason="unresolvable_subject")
            continue
        candidate_ids.add(current_id)
        resolved[str(event.id)] = _EventTarget(incident_id=current_id)

    #: One query decides which of the named incidents this project may learn
    #: from; anything else is dropped here rather than at episode assembly.
    if candidate_ids:
        rows = await session.scalars(
            select(Incident.id)
            .where(Incident.id.in_(candidate_ids))
            .where(Incident.project_id == project_id)
        )
        allowed = {str(value) for value in rows.all()}
        for key, target in list(resolved.items()):
            if target.incident_id is None:
                continue
            if str(target.incident_id) not in allowed:
                resolved[key] = _EventTarget(reason="incident_not_found")

    return resolved


def _payload_uuid(payload: Any, key: str) -> Optional[uuid.UUID]:
    """A UUID out of an event payload, or ``None`` when it is absent or junk."""
    if not isinstance(payload, dict):
        return None
    return _as_uuid(payload.get(key))


def _as_uuid(value: Any) -> Optional[uuid.UUID]:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


async def _mine_and_record(
    session: AsyncSession,
    *,
    corpus: ExperienceCorpus,
    run: LearningRun,
    boundary: datetime,
    summary: LearningRunSummary,
    settings: Settings,
) -> None:
    """Mine, validate and record patterns for one project (§7)."""
    from app.services.pattern_miners import default_miners

    validator = KnowledgeValidationService(settings)
    remaining = settings.INTELLIGENCE_MAX_PATTERNS_PER_RUN

    for miner in default_miners():
        if remaining <= 0:
            summary.skipped_reasons.append("pattern_budget_exhausted")
            break
        patterns = await miner.mine(session, corpus, min_samples=2)
        for pattern in patterns[:remaining]:
            remaining -= 1
            summary.patterns_discovered += 1
            verdict = validator.validate(
                pattern,
                corpus=corpus,
                cutoff=boundary,
            )
            result = await record_candidate(
                session,
                pattern,
                verdict,
                learning_run_id=run.id,
                settings=settings,
                now=boundary,
            )
            if result.skipped_reason:
                summary.skipped_reasons.append(result.skipped_reason)
                continue
            if result.created:
                summary.knowledge_created += 1
            elif result.updated:
                summary.knowledge_updated += 1
            if result.activated:
                summary.knowledge_activated += 1
            if verdict.promotable:
                summary.patterns_validated += 1
            else:
                summary.patterns_rejected += 1


async def _generate_recommendations(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    boundary: datetime,
    summary: LearningRunSummary,
    settings: Settings,
) -> None:
    """Produce and persist recommendations for current open incidents (§38)."""
    from app.models.incident import Incident, IncidentStatus
    from app.services.recommendation_engine import ReliabilityRecommendationEngine

    engine = ReliabilityRecommendationEngine(settings)
    incidents = list(
        (
            await session.scalars(
                select(Incident)
                .where(Incident.project_id == project_id)
                .where(
                    Incident.status.in_(
                        [
                            IncidentStatus.OPEN,
                            IncidentStatus.ACKNOWLEDGED,
                            IncidentStatus.INVESTIGATING,
                            IncidentStatus.MITIGATED,
                        ]
                    )
                )
                .order_by(Incident.detected_at.desc())
                .limit(25)
            )
        ).all()
    )
    for incident in incidents:
        ranked = await engine.recommend_for_incident(
            session, project_id=project_id, incident_id=incident.id, cutoff=boundary
        )
        created = await engine.persist_many(session, ranked, now=boundary)
        summary.recommendations_created += len(created)

    expired = await engine.expire_stale(session, now=boundary)
    summary.recommendations_expired += len(expired)


async def _reconcile_experience_signatures(
    session: AsyncSession, *, project_id: uuid.UUID, corpus: ExperienceCorpus
) -> None:
    """Touch experiences whose signature vocabulary changed (§26).

    A feature-schema change makes old groupings incomparable; re-stamping the
    rows with the current version keeps the corpus homogeneous instead of mixing
    two definitions in one bucket. The re-stamp itself is done by the builder on
    the next observation, so nothing is rewritten here — this only counts what is
    pending, for the run's report.
    """
    stale = [
        entry
        for entry in corpus.entries
        if entry.failure.schema_version != FEATURE_SCHEMA_VERSION
    ]
    if stale:
        logger.info(
            "project %s has %d experience(s) written under an older feature schema",
            project_id,
            len(stale),
        )


async def _derive_relationships(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run: LearningRun,
    boundary: datetime,
    lookback_days: Optional[int],
    summary: LearningRunSummary,
    settings: Settings,
) -> None:
    """Learn component relationships from the episode window (§23).

    A failure here must not fail the run: relationships are a derived view, and
    the knowledge, recommendations and experiences the run already produced stay
    valid without them. The error is recorded on the summary instead.
    """
    from app.services.relationship_builder import build_relationships

    try:
        result = await build_relationships(
            session,
            project_id=project_id,
            cutoff=boundary,
            learning_run_id=run.id,
            lookback_days=lookback_days,
            settings=settings,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("relationship derivation failed", exc_info=True)
        summary.errors.append(f"relationships: {type(exc).__name__}: {exc}")
        return

    summary.relationships_created += result.edges_created
    summary.relationships_updated += result.edges_updated
    summary.relationships_stale += result.edges_stale
    summary.errors.extend(result.errors)


def _apply_summary(run: LearningRun, summary: LearningRunSummary) -> None:
    run.events_processed = summary.events_processed
    run.experiences_created = summary.experiences_created
    run.experiences_updated = summary.experiences_updated
    run.patterns_discovered = summary.patterns_discovered
    run.patterns_validated = summary.patterns_validated
    run.patterns_rejected = summary.patterns_rejected
    run.knowledge_activated = summary.knowledge_activated
    run.records_flagged = summary.experiences_flagged
    run.relationships_created = summary.relationships_created
    run.relationships_updated = summary.relationships_updated


async def pending_event_count(
    session: AsyncSession, *, project_id: Optional[uuid.UUID] = None
) -> int:
    """How much work is waiting, for health and the dashboard (§80)."""
    stmt = select(func.count(LearningEvent.id)).where(
        LearningEvent.processed_at.is_(None)
    )
    if project_id is not None:
        stmt = stmt.where(LearningEvent.project_id == project_id)
    return int(await session.scalar(stmt) or 0)


async def latest_run(
    session: AsyncSession, *, project_id: Optional[uuid.UUID] = None
) -> Optional[LearningRun]:
    stmt = select(LearningRun).order_by(LearningRun.started_at.desc()).limit(1)
    if project_id is not None:
        stmt = stmt.where(LearningRun.project_id == project_id)
    return await session.scalar(stmt)


async def run_is_due(
    session: AsyncSession,
    *,
    project_id: Optional[uuid.UUID],
    interval_seconds: int,
    now: Optional[datetime] = None,
) -> bool:
    """Whether enough time has passed since the last run (§29)."""
    moment = _aware(now) or datetime.now(timezone.utc)
    run = await latest_run(session, project_id=project_id)
    if run is None:
        return True
    if run.status == LearningRunStatus.RUNNING:
        started = _aware(run.started_at) or moment
        #: A run that has been RUNNING for more than three intervals is assumed
        #: abandoned (a process died mid-run) and may be superseded.
        return (moment - started) > timedelta(seconds=interval_seconds * 3)
    reference = _aware(run.completed_at) or _aware(run.started_at) or moment
    return (moment - reference) >= timedelta(seconds=interval_seconds)


async def eligible_projects(
    session: AsyncSession,
    *,
    cutoff: Optional[datetime] = None,
    limit: int = 100,
) -> list[SoftwareProject]:
    """Projects the scheduler may run for: the ones with actual history (§29).

    A project with no experiences and no knowledge has nothing to learn from, so
    a batch run over it can only produce an empty run row. Including it would
    make a nightly job report activity that did not happen.

    ``cutoff`` bounds the history considered, for the same reason every other
    query in this module is bounded: a project whose only history is in the
    future relative to the run is not eligible *as of* the run.
    """
    boundary = _aware(cutoff) or datetime.now(timezone.utc)
    experience_projects = select(ReliabilityExperience.project_id).where(
        ReliabilityExperience.end_time <= boundary
    )
    knowledge_projects = select(ReliabilityKnowledge.project_id)
    stmt = (
        select(SoftwareProject)
        .where(
            SoftwareProject.id.in_(experience_projects)
            | SoftwareProject.id.in_(knowledge_projects)
        )
        .order_by(SoftwareProject.created_at.asc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


__all__ = [
    "LearningRunSummary",
    "create_run",
    "eligible_projects",
    "execute_learning_run",
    "latest_run",
    "pending_event_count",
    "run_is_due",
    "safely_execute_learning_run",
]
