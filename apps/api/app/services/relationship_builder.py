"""ARGUS Learned Relationship Builder (Phase 10 §23, §24, §25).

Derives component-to-component relationships from completed episodes and stores
them in ``intelligence_relationships`` — the learning layer's contribution to the
knowledge graph.

The distinction this module exists to preserve:

```text
graph_edges ("A calls B")               → how the system is wired
intelligence_relationships ("A and B    → what history observed
  failed together in 7 of 7 episodes")
```

§24 forbids confusing the two. Every edge produced here therefore carries the
sample count, the coverage window, the ids of the episodes behind it, and a
``limitations`` list that starts with
:data:`app.services.knowledge_validation.HISTORICAL_RELATIONSHIP_NOTE`. Direction is
claimed only when the *stored* analysis justifies it:

* :data:`RelationshipKind.FAILURE_PROPAGATION` needs a Phase 4 root-cause
  candidate that is ``SUPPORTED`` with MEDIUM+ confidence. The direction comes
  from that hypothesis, and the edge says so, including when a human later
  confirmed it via ``ROOT_CAUSE_CONFIRMED``.
* :data:`RelationshipKind.SHARED_FAILURE` is the honest fallback: the same
  episodes touched both components and nothing established which end came
  first, so ``directed`` is ``False`` and the API will not draw an arrow.
* :data:`RelationshipKind.DEPENDENCY_DEGRADATION` takes its direction from the
  *declared* dependency (``component_dependencies``): the dependency degraded
  while the component that depends on it was affected.
* :data:`RelationshipKind.REMEDIATION_INFLUENCE` records that remediating one
  component coincided with the other recovering. That is an association with an
  outcome, never a mechanism.

Determinism and idempotency, both load-bearing:

* Every count is **recomputed from the corpus window**, not incremented. Running
  the pipeline twice over unchanged history produces byte-identical rows, so a
  re-run can never inflate support — the failure mode that would turn two
  episodes into "14 observations".
* Iteration order is sorted at every level (components, pairs), so the rows and
  their evidence lists are stable across runs and across processes.
* Nothing below :data:`app.models.intelligence.RELATIONSHIP_MIN_SAMPLES` is
  stored at all. One co-occurrence is an anecdote and the table does not take
  anecdotes.

Decay marks edges ``STALE`` rather than deleting them (§25): an edge that stops
being reconfirmed is evidence that the topology changed, which is worth keeping.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.causal import CandidateStatus, ConfidenceLevel, RootCauseCandidate
from app.models.intelligence import (
    RELATIONSHIP_MIN_SAMPLES,
    DataProvenance,
    KnowledgeConfidence,
    LearnedRelationship,
    RelationshipKind,
    RelationshipStatus,
    ReliabilityExperience,
)
from app.models.remediation import RemediationAction
from app.models.system import ComponentDependency
from app.services.learning_signatures import (
    FEATURE_SCHEMA_VERSION,
    FailureSignature,
    ResolutionSignature,
)

logger = logging.getLogger(__name__)

#: Algorithm identity recorded on every edge (§26, §68).
ALGORITHM = "relationship-miner"
ALGORITHM_VERSION = "1.0"

#: How many episode citations one edge keeps. Bounded: the point is that a
#: reviewer can open a few of them, not that the row mirrors the corpus.
EVIDENCE_LIMIT = 25

#: Printed with every learned relationship (§24). The API returns it verbatim so
#: a client cannot render these as dependencies without contradicting the data.
HISTORICAL_RELATIONSHIP_NOTE = (
    "HISTORICAL RELATIONSHIP — learned from past episodes, not a dependency "
    "declared in configuration"
)

#: Direction of a FAILURE_PROPAGATION edge rests on a Phase 4 *hypothesis*, and
#: the edge must say so rather than reading as an established call path.
_HYPOTHESIS_DIRECTION_NOTE = (
    "Direction comes from a supported root-cause hypothesis (Phase 4), not from "
    "a confirmed call path"
)
_CONFIRMED_DIRECTION_NOTE = (
    "Direction comes from a root cause confirmed by a human reviewer"
)
_NO_DIRECTION_NOTE = (
    "No direction is claimed: the two components failed in the same episodes"
)
_DEPENDENCY_DIRECTION_NOTE = (
    "Direction follows the declared dependency, not the observed symptom order"
)
_REMEDIATION_NOTE = (
    "Remediating one component coincided with the other recovering in the same "
    "episode; this is an association with an outcome, not a mechanism"
)

#: Confidence ladder reused from §34 buckets.
_HIGH_SAMPLES = 10
_MEDIUM_SAMPLES = 5
_HIGH_SUPPORT = 0.8
_MEDIUM_SUPPORT = 0.6

#: Scores assigned to the direction sources, so an edge whose direction came
#: from a confirmed root cause is distinguishable from one that did not.
_DIRECTION_PRIORITY = {
    "human_confirmed_root_cause": 3,
    "supported_root_cause_hypothesis": 2,
    "declared_dependency": 1,
    "none": 0,
}


@dataclass
class EdgeAccumulator:
    """One (source, target, kind) pair being counted within a window."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID]
    source_component_id: uuid.UUID
    target_component_id: uuid.UUID
    kind: RelationshipKind
    directed: bool
    sample_count: int = 0
    supporting_count: int = 0
    coverage_start: Optional[datetime] = None
    coverage_end: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    evidence: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    direction_source: str = "none"

    @property
    def key(self) -> tuple:
        return (
            str(self.environment_id) if self.environment_id else "-",
            str(self.source_component_id),
            str(self.target_component_id),
            self.kind.value,
        )

    def observe(
        self,
        *,
        experience_id: uuid.UUID,
        incident_id: Optional[uuid.UUID],
        start: datetime,
        end: datetime,
        supporting: bool,
        note: Optional[str] = None,
        direction_source: Optional[str] = None,
    ) -> None:
        #: Every boundary is normalised to aware UTC on the way in. Rows read
        #: back from SQLite carry naive timestamps while the ones passed in are
        #: aware, and comparing the two raises — a bug that only shows up on the
        #: second run, which is precisely when the counts must stay stable.
        start = _aware(start)
        end = _aware(end)
        self.sample_count += 1
        if supporting:
            self.supporting_count += 1
        self.coverage_start = _min_dt(self.coverage_start, start)
        self.coverage_end = _max_dt(self.coverage_end, end)
        self.last_seen_at = _max_dt(self.last_seen_at, end)
        if note and note not in self.notes:
            self.notes.append(note)
        if direction_source and _DIRECTION_PRIORITY.get(
            direction_source, 0
        ) > _DIRECTION_PRIORITY.get(self.direction_source, 0):
            self.direction_source = direction_source
        if len(self.evidence) < EVIDENCE_LIMIT:
            self.evidence.append(
                {
                    "type": "experience",
                    "id": str(experience_id),
                    "incident_id": str(incident_id) if incident_id else None,
                    "occurred_at": _iso(end),
                }
            )

    def support_strength(self) -> Optional[float]:
        if self.sample_count <= 0:
            return None
        return self.supporting_count / self.sample_count

    def confidence(self) -> KnowledgeConfidence:
        strength = self.support_strength()
        if strength is None:
            return KnowledgeConfidence.UNKNOWN
        if self.sample_count >= _HIGH_SAMPLES and strength >= _HIGH_SUPPORT:
            return KnowledgeConfidence.HIGH
        if self.sample_count >= _MEDIUM_SAMPLES and strength >= _MEDIUM_SUPPORT:
            return KnowledgeConfidence.MEDIUM
        return KnowledgeConfidence.LOW

    def limitations(self) -> list[str]:
        return [
            HISTORICAL_RELATIONSHIP_NOTE,
            *self.notes,
            f"Derived from {self.sample_count} episode(s) in the mined window",
        ]


@dataclass
class RelationshipBuildResult:
    """What one build did (§80). Counts are reported, never hard-coded."""

    project_id: Optional[str] = None
    edges_considered: int = 0
    edges_created: int = 0
    edges_updated: int = 0
    edges_low_support: int = 0
    edges_stale: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "edges_considered": self.edges_considered,
            "edges_created": self.edges_created,
            "edges_updated": self.edges_updated,
            "edges_low_support": self.edges_low_support,
            "edges_stale": self.edges_stale,
            "errors": list(self.errors),
        }


async def build_relationships(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    cutoff: datetime,
    learning_run_id: Optional[uuid.UUID] = None,
    lookback_days: Optional[int] = None,
    settings: Optional[Settings] = None,
) -> RelationshipBuildResult:
    """Recompute the project's learned relationships as of ``cutoff`` (§23, §31).

    ``cutoff`` bounds the corpus in SQL on ``end_time``: an episode that ended
    after the boundary is future knowledge and cannot shape a relationship *as
    of* the boundary — the same temporal-leakage guard every other stage uses.
    """
    settings = settings or get_settings()
    boundary = _aware(cutoff)
    result = RelationshipBuildResult(project_id=str(project_id))

    experiences = await _load_experiences(
        session,
        project_id=project_id,
        cutoff=boundary,
        lookback_days=lookback_days,
        limit=settings.INTELLIGENCE_LEARNING_BATCH * 4,
    )
    if not experiences:
        stale = await mark_stale(
            session, project_id=project_id, now=boundary, settings=settings
        )
        result.edges_stale = len(stale)
        return result

    confirmed = await _load_confirmed_root_cause_incidents(
        session, experiences=experiences
    )
    root_causes = await _load_root_cause_components(
        session, experiences=experiences, confirmed=confirmed
    )
    remediation_components = await _load_remediation_components(
        session, experiences=experiences
    )
    component_ids = _involved_components(experiences)
    declared = await _load_declared_dependencies(session, component_ids=component_ids)

    accumulators: dict[tuple, EdgeAccumulator] = {}
    for experience in experiences:
        try:
            _observe_episode(
                accumulators,
                experience=experience,
                root_causes=root_causes,
                confirmed=confirmed,
                remediation_components=remediation_components,
                declared=declared,
            )
        except Exception as exc:  # pragma: no cover - defensive per-episode guard
            #: One unreadable episode must not cost the whole project its
            #: relationships; it is reported instead (§79).
            logger.warning(
                "relationship derivation failed for experience %s",
                experience.id,
                exc_info=True,
            )
            result.errors.append(f"{experience.id}: {type(exc).__name__}: {exc}")

    result.edges_considered = len(accumulators)

    refreshed: list[tuple[EdgeAccumulator, LearnedRelationship]] = []
    for key in sorted(accumulators.keys()):
        accumulator = accumulators[key]
        if accumulator.sample_count < RELATIONSHIP_MIN_SAMPLES:
            result.edges_low_support += 1
            continue
        row, created = await _upsert_edge(
            session,
            accumulator=accumulator,
            learning_run_id=learning_run_id,
            now=boundary,
        )
        refreshed.append((accumulator, row))
        if created:
            result.edges_created += 1
        else:
            result.edges_updated += 1

    touched = {str(row.id) for _acc, row in refreshed}
    stale = await mark_stale(
        session,
        project_id=project_id,
        now=boundary,
        settings=settings,
        keep=touched,
    )
    result.edges_stale = len(stale)
    await session.flush()
    return result


# --------------------------------------------------------------- observations


def _observe_episode(
    accumulators: dict[tuple, EdgeAccumulator],
    *,
    experience: ReliabilityExperience,
    root_causes: dict[str, tuple[uuid.UUID, str]],
    confirmed: set[str],
    remediation_components: dict[str, uuid.UUID],
    declared: set[tuple[uuid.UUID, uuid.UUID]],
) -> None:
    """Turn one episode into observations on every relationship it supports."""
    signature = FailureSignature.from_dict(experience.failure_signature)
    resolution = (
        ResolutionSignature.from_dict(experience.resolution_signature)
        if experience.resolution_signature
        else None
    )

    components = _episode_components(experience)
    if len(components) < 2:
        #: A single-component episode says nothing about how components relate.
        return

    environment_id = experience.environment_id
    outcome = (experience.outcome or "").lower()
    succeeded = bool(
        (resolution and resolution.succeeded)
        or outcome in {"effective", "partially_effective", "patch_verified"}
    )

    direction = root_causes.get(str(experience.id))
    # -- FAILURE_PROPAGATION: directional, needs a supported root-cause component
    if direction is not None and direction[0] in components:
        origin, direction_source = direction
        note = (
            _CONFIRMED_DIRECTION_NOTE
            if direction_source == "human_confirmed_root_cause"
            else _HYPOTHESIS_DIRECTION_NOTE
        )
        for target in sorted(
            (component for component in components if component != origin),
            key=str,
        ):
            _accumulate(
                accumulators,
                experience=experience,
                environment_id=environment_id,
                source=origin,
                target=target,
                kind=RelationshipKind.FAILURE_PROPAGATION,
                directed=True,
                supporting=True,
                note=note,
                direction_source=direction_source,
            )
    else:
        # -- SHARED_FAILURE: undirected, canonical pair ordering
        ordered = sorted(components, key=str)
        for index, source in enumerate(ordered):
            for target in ordered[index + 1 :]:
                _accumulate(
                    accumulators,
                    experience=experience,
                    environment_id=environment_id,
                    source=source,
                    target=target,
                    kind=RelationshipKind.SHARED_FAILURE,
                    #: Undirected, and the flag says so — a client cannot draw an
                    #: arrow for a co-occurrence (§24).
                    directed=False,
                    supporting=True,
                    note=_NO_DIRECTION_NOTE,
                    direction_source="none",
                )

    # -- DEPENDENCY_DEGRADATION: a declared dependency of an affected component
    conditions = set(signature.dependency_conditions)
    if conditions & {"dependency_degraded", "dependency_unhealthy"}:
        for dependent, dependency in sorted(declared, key=str):
            if dependent not in components or dependency not in components:
                continue
            _accumulate(
                accumulators,
                experience=experience,
                environment_id=environment_id,
                source=dependency,
                target=dependent,
                kind=RelationshipKind.DEPENDENCY_DEGRADATION,
                directed=True,
                supporting=True,
                note=_DEPENDENCY_DIRECTION_NOTE,
                direction_source="declared_dependency",
            )

    # -- REMEDIATION_INFLUENCE: acting on one component, another recovered
    action_component = remediation_components.get(str(experience.id))
    if action_component is not None and action_component in components:
        for other in sorted(
            (component for component in components if component != action_component),
            key=str,
        ):
            _accumulate(
                accumulators,
                experience=experience,
                environment_id=environment_id,
                source=action_component,
                target=other,
                kind=RelationshipKind.REMEDIATION_INFLUENCE,
                directed=True,
                supporting=succeeded,
                note=_REMEDIATION_NOTE,
                direction_source="none",
            )


def _accumulate(
    accumulators: dict[tuple, EdgeAccumulator],
    *,
    experience: ReliabilityExperience,
    environment_id: Optional[uuid.UUID],
    source: uuid.UUID,
    target: uuid.UUID,
    kind: RelationshipKind,
    directed: bool,
    supporting: bool,
    note: str,
    direction_source: str,
) -> None:
    if source == target:
        return
    seed = EdgeAccumulator(
        project_id=experience.project_id,
        environment_id=environment_id,
        source_component_id=source,
        target_component_id=target,
        kind=kind,
        directed=directed,
    )
    accumulator = accumulators.setdefault(seed.key, seed)
    #: Direction only ever arrives from an explicit source, never from sorting:
    #: a kind is uniformly directed or uniformly not, and the flag records which.
    accumulator.directed = directed or accumulator.directed
    accumulator.observe(
        experience_id=experience.id,
        incident_id=experience.incident_id,
        start=experience.start_time,
        end=experience.end_time,
        supporting=supporting,
        note=note,
        direction_source=direction_source,
    )


# ----------------------------------------------------------------- persistence


async def _upsert_edge(
    session: AsyncSession,
    *,
    accumulator: EdgeAccumulator,
    learning_run_id: Optional[uuid.UUID],
    now: datetime,
) -> tuple[LearnedRelationship, bool]:
    """Write one edge, replacing the window-derived counts (§28 idempotency)."""
    existing = await _find_edge(session, accumulator)
    if existing is None:
        row = LearnedRelationship(
            project_id=accumulator.project_id,
            environment_id=accumulator.environment_id,
            source_component_id=accumulator.source_component_id,
            target_component_id=accumulator.target_component_id,
            kind=accumulator.kind,
            directed=accumulator.directed,
            status=RelationshipStatus.ACTIVE,
            sample_count=accumulator.sample_count,
            supporting_count=accumulator.supporting_count,
            confidence=accumulator.confidence(),
            evidence=list(accumulator.evidence),
            limitations=accumulator.limitations(),
            provenance=DataProvenance.OBSERVABILITY,
            algorithm=ALGORITHM,
            algorithm_version=ALGORITHM_VERSION,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            coverage_start=accumulator.coverage_start,
            coverage_end=accumulator.coverage_end,
            first_seen_at=accumulator.coverage_start,
            last_seen_at=accumulator.last_seen_at,
            learning_run_id=learning_run_id,
        )
        session.add(row)
        await session.flush()
        return row, True

    #: Counts are *recomputed*, never added: a second run over unchanged history
    #: must produce the same row, not double the support.
    existing.sample_count = accumulator.sample_count
    existing.supporting_count = accumulator.supporting_count
    existing.confidence = accumulator.confidence()
    existing.directed = accumulator.directed
    existing.evidence = list(accumulator.evidence)
    existing.limitations = accumulator.limitations()
    existing.algorithm = ALGORITHM
    existing.algorithm_version = ALGORITHM_VERSION
    existing.feature_schema_version = FEATURE_SCHEMA_VERSION
    existing.coverage_start = _min_dt(
        existing.coverage_start, accumulator.coverage_start
    )
    existing.coverage_end = _max_dt(existing.coverage_end, accumulator.coverage_end)
    existing.first_seen_at = existing.first_seen_at or accumulator.coverage_start
    existing.last_seen_at = _max_dt(existing.last_seen_at, accumulator.last_seen_at)
    existing.learning_run_id = learning_run_id
    #: An edge that history confirms again is not stale any more — otherwise a
    #: single quiet run would permanently mark a live relationship dead.
    if existing.status == RelationshipStatus.STALE:
        existing.status = RelationshipStatus.ACTIVE
    await session.flush()
    return existing, False


async def mark_stale(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    now: datetime,
    settings: Optional[Settings] = None,
    keep: Optional[set[str]] = None,
    limit: int = 2000,
) -> list[LearnedRelationship]:
    """Mark edges not reconfirmed within the staleness horizon (§25, §42).

    Never deletes: the edge, its evidence and its coverage survive, and it is
    marked ``STALE`` again on every run until it either reappears or knowledge
    decay retires the pattern behind it.
    """
    settings = settings or get_settings()
    moment = _aware(now)
    horizon = moment - timedelta(days=settings.INTELLIGENCE_KNOWLEDGE_STALE_AFTER_DAYS)

    stmt = (
        select(LearnedRelationship)
        .where(LearnedRelationship.project_id == project_id)
        .where(LearnedRelationship.status == RelationshipStatus.ACTIVE)
        .where(
            (LearnedRelationship.last_seen_at.is_(None))
            | (LearnedRelationship.last_seen_at < horizon)
        )
        .limit(limit)
    )
    rows = list((await session.scalars(stmt)).all())
    refreshed = keep or set()
    retired = [row for row in rows if str(row.id) not in refreshed]
    for row in retired:
        row.status = RelationshipStatus.STALE
    if retired:
        await session.flush()
    return retired


# --------------------------------------------------------------------- loading


async def _load_experiences(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    cutoff: datetime,
    lookback_days: Optional[int],
    limit: int,
) -> list[ReliabilityExperience]:
    """The window of episodes a build reasons over (§28, §31).

    Ordered oldest-first so the evidence list a reviewer opens is chronological;
    ``POOR`` rows are excluded because the pre-learning quality gate (§30) already
    decided they are not trustworthy enough to learn from.
    """
    stmt = (
        select(ReliabilityExperience)
        .where(ReliabilityExperience.project_id == project_id)
        .where(ReliabilityExperience.end_time <= cutoff)
        .where(ReliabilityExperience.data_quality != "POOR")
        .order_by(ReliabilityExperience.end_time.asc())
        .limit(limit)
    )
    if lookback_days is not None:
        stmt = stmt.where(
            ReliabilityExperience.end_time >= cutoff - timedelta(days=lookback_days)
        )
    return list((await session.scalars(stmt)).all())


async def _load_root_cause_components(
    session: AsyncSession,
    *,
    experiences: Sequence[ReliabilityExperience],
    confirmed: set[str],
) -> dict[str, tuple[uuid.UUID, str]]:
    """Episode id → (root-cause component, direction source), where supported.

    ``SUPPORTED`` with MEDIUM+ confidence is the bar. A ``WEAKENED`` or
    ``INSUFFICIENT`` candidate would give a direction nobody stands behind, and
    a direction is exactly what a reader of an arrow acts on.

    The direction *source* is recorded alongside it so an edge whose direction a
    human confirmed is distinguishable from one resting on a hypothesis (§78).
    """
    candidate_ids = [
        experience.root_cause_candidate_id
        for experience in experiences
        if experience.root_cause_candidate_id is not None
    ]
    if not candidate_ids:
        return {}
    rows = list(
        (
            await session.scalars(
                select(RootCauseCandidate).where(
                    RootCauseCandidate.id.in_(candidate_ids)
                )
            )
        ).all()
    )
    by_id = {str(row.id): row for row in rows}
    resolved: dict[str, tuple[uuid.UUID, str]] = {}
    for experience in experiences:
        if experience.root_cause_candidate_id is None:
            continue
        candidate = by_id.get(str(experience.root_cause_candidate_id))
        if candidate is None or candidate.component_id is None:
            continue
        if candidate.status != CandidateStatus.SUPPORTED:
            continue
        if candidate.confidence not in (ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH):
            continue
        source = (
            "human_confirmed_root_cause"
            if str(experience.incident_id) in confirmed
            else "supported_root_cause_hypothesis"
        )
        resolved[str(experience.id)] = (candidate.component_id, source)
    return resolved


async def _load_confirmed_root_cause_incidents(
    session: AsyncSession,
    *,
    experiences: Sequence[ReliabilityExperience],
) -> set[str]:
    """Incident ids with a recorded ``ROOT_CAUSE_CONFIRMED`` event (§78).

    Human confirmation upgrades an edge's *direction* to something a reviewer
    signed off on. It does not upgrade the edge to a dependency, and it does not
    touch the sample count — §78 is explicit that confirmation improves evidence
    quality without guaranteeing correctness.
    """
    from app.models.intelligence import LearningEvent, LearningEventType

    incident_ids = [
        experience.incident_id
        for experience in experiences
        if experience.incident_id is not None
    ]
    if not incident_ids:
        return set()
    rows = await session.scalars(
        select(LearningEvent.subject_id)
        .where(LearningEvent.event_type == LearningEventType.ROOT_CAUSE_CONFIRMED)
        .where(LearningEvent.subject_id.in_(incident_ids))
    )
    return {str(value) for value in rows.all()}


async def _load_remediation_components(
    session: AsyncSession,
    *,
    experiences: Sequence[ReliabilityExperience],
) -> dict[str, uuid.UUID]:
    """Episode id → the component the episode's remediation acted on."""
    action_ids = [
        experience.remediation_action_id
        for experience in experiences
        if experience.remediation_action_id is not None
    ]
    if not action_ids:
        return {}
    rows = list(
        (
            await session.scalars(
                select(RemediationAction).where(RemediationAction.id.in_(action_ids))
            )
        ).all()
    )
    by_id = {
        str(row.id): row.component_id for row in rows if row.component_id is not None
    }
    resolved: dict[str, uuid.UUID] = {}
    for experience in experiences:
        if experience.remediation_action_id is None:
            continue
        component_id = by_id.get(str(experience.remediation_action_id))
        if component_id is not None:
            resolved[str(experience.id)] = component_id
    return resolved


async def _load_declared_dependencies(
    session: AsyncSession, *, component_ids: set[uuid.UUID]
) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """Declared (dependent → dependency) pairs inside the involved component set."""
    if not component_ids:
        return set()
    #: ``session.execute``, not ``session.scalars``: scalars keeps only the first
    #: column of a multi-column select, which would turn every pair into a bare
    #: UUID and lose the direction the dependency states.
    result = await session.execute(
        select(
            ComponentDependency.source_component_id,
            ComponentDependency.target_component_id,
        )
        .where(ComponentDependency.source_component_id.in_(component_ids))
        .where(ComponentDependency.target_component_id.in_(component_ids))
    )
    return {(source, target) for source, target in result.all()}


def _involved_components(
    experiences: Iterable[ReliabilityExperience],
) -> set[uuid.UUID]:
    involved: set[uuid.UUID] = set()
    for experience in experiences:
        involved |= _episode_components(experience)
    return involved


def _episode_components(experience: ReliabilityExperience) -> set[uuid.UUID]:
    """Every component the episode touched, as UUIDs.

    ``component_ids`` is the authoritative set (the builder stores UUIDs there);
    ``primary_component_id`` is added in case an older row predates it.
    """
    components: set[uuid.UUID] = set()
    for value in experience.component_ids or []:
        parsed = _as_uuid(value)
        if parsed is not None:
            components.add(parsed)
    if experience.primary_component_id is not None:
        components.add(experience.primary_component_id)
    return components


async def _find_edge(
    session: AsyncSession, accumulator: EdgeAccumulator
) -> Optional[LearnedRelationship]:
    """The existing row for this key, environment NULL handled explicitly.

    ``== None`` is written as ``is_(None)``: a plain ``==`` against a NULL
    environment would generate ``environment_id = NULL``, which matches nothing
    in SQL — every run would insert a duplicate edge.
    """
    stmt = (
        select(LearnedRelationship)
        .where(LearnedRelationship.project_id == accumulator.project_id)
        .where(
            LearnedRelationship.source_component_id == accumulator.source_component_id
        )
        .where(
            LearnedRelationship.target_component_id == accumulator.target_component_id
        )
        .where(LearnedRelationship.kind == accumulator.kind)
    )
    if accumulator.environment_id is None:
        stmt = stmt.where(LearnedRelationship.environment_id.is_(None))
    else:
        stmt = stmt.where(
            LearnedRelationship.environment_id == accumulator.environment_id
        )
    return await session.scalar(stmt.limit(1))


def _as_uuid(value: Any) -> Optional[uuid.UUID]:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _aware(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _min_dt(left: Optional[datetime], right: Optional[datetime]) -> Optional[datetime]:
    candidates = [_aware(value) for value in (left, right) if value is not None]
    return min(candidates) if candidates else None


def _max_dt(left: Optional[datetime], right: Optional[datetime]) -> Optional[datetime]:
    candidates = [_aware(value) for value in (left, right) if value is not None]
    return max(candidates) if candidates else None


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


__all__ = [
    "ALGORITHM",
    "ALGORITHM_VERSION",
    "EVIDENCE_LIMIT",
    "HISTORICAL_RELATIONSHIP_NOTE",
    "RelationshipBuildResult",
    "build_relationships",
    "mark_stale",
]
