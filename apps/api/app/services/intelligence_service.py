"""ARGUS Reliability Intelligence Queries (Phase 10 §51–§60, §80–§82).

The read model the API and the Reliability Intelligence Center consume. It owns
two things and nothing else:

* **Scope enforcement.** Every function takes a ``project_id`` and filters on it.
  An out-of-scope id returns ``None`` rather than a row, so the route can answer
  404 without confirming that the row exists — the same rule Phases 3–9 use.
* **Derivation, not decoration.** Counts, staleness flags and coverage windows
  are computed from the stored columns. Nothing is displayed that is not either
  stored or counted, which is what lets the UI claim "34 of 42" honestly.

The dashboard is assembled here rather than in the route so the same numbers can
be produced for a text report or a test without going through HTTP.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Optional, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident
from app.models.intelligence import (
    ComponentReliabilityProfile,
    KnowledgeReview,
    KnowledgeStatus,
    KnowledgeType,
    KnowledgeVersion,
    LearnedRelationship,
    LearningEvent,
    LearningRun,
    RecommendationOutcome,
    RecommendationStatus,
    RelationshipKind,
    RelationshipStatus,
    ReliabilityExperience,
    ReliabilityKnowledge,
    ReliabilityRecommendation,
)
from app.models.remediation import RemediationAction
from app.models.system import SystemComponent
from app.services.learning_signatures import (
    FailureSignature,
    ResolutionSignature,
)
from app.services.relationship_builder import HISTORICAL_RELATIONSHIP_NOTE

logger = logging.getLogger(__name__)

#: Statuses that mean "this pattern is currently believed" (§4).
LIVE_STATUSES = (KnowledgeStatus.VALIDATED, KnowledgeStatus.ACTIVE)

#: Statuses that mean "retired, kept for history".
RETIRED_STATUSES = (
    KnowledgeStatus.DEPRECATED,
    KnowledgeStatus.REJECTED,
    KnowledgeStatus.SUPERSEDED,
)


async def list_knowledge(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    status: Optional[str] = None,
    knowledge_type: Optional[str] = None,
    component_id: Optional[uuid.UUID] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[ReliabilityKnowledge], int]:
    """Knowledge rows for one project, filtered and paginated."""
    conditions = [ReliabilityKnowledge.project_id == project_id]
    if status:
        conditions.append(ReliabilityKnowledge.status == _status(status))
    if knowledge_type:
        conditions.append(
            ReliabilityKnowledge.knowledge_type == _knowledge_type(knowledge_type)
        )
    if component_id is not None:
        conditions.append(ReliabilityKnowledge.component_id == component_id)
    if search:
        like = f"%{search.lower()}%"
        conditions.append(
            or_(
                func.lower(ReliabilityKnowledge.title).like(like),
                func.lower(ReliabilityKnowledge.description).like(like),
                func.lower(ReliabilityKnowledge.feature_signature).like(like),
            )
        )

    total = int(
        await session.scalar(
            select(func.count(ReliabilityKnowledge.id)).where(*conditions)
        )
        or 0
    )
    stmt = (
        select(ReliabilityKnowledge)
        .where(*conditions)
        .order_by(
            ReliabilityKnowledge.sample_count.desc(),
            ReliabilityKnowledge.updated_at.desc(),
        )
        .offset(max(0, (page - 1) * page_size))
        .limit(page_size)
    )
    return list((await session.scalars(stmt)).all()), total


async def get_knowledge(
    session: AsyncSession, *, project_id: uuid.UUID, knowledge_id: uuid.UUID
) -> Optional[ReliabilityKnowledge]:
    """One knowledge row, scoped to its project."""
    return await session.scalar(
        select(ReliabilityKnowledge)
        .where(ReliabilityKnowledge.id == knowledge_id)
        .where(ReliabilityKnowledge.project_id == project_id)
    )


async def knowledge_detail(
    session: AsyncSession, *, project_id: uuid.UUID, knowledge_id: uuid.UUID
) -> Optional[dict[str, Any]]:
    """Knowledge with its version ledger, reviews and conflicts (§53)."""
    row = await get_knowledge(session, project_id=project_id, knowledge_id=knowledge_id)
    if row is None:
        return None

    versions = list(
        (
            await session.scalars(
                select(KnowledgeVersion)
                .where(KnowledgeVersion.knowledge_id == row.id)
                .order_by(
                    KnowledgeVersion.version.desc(), KnowledgeVersion.created_at.desc()
                )
            )
        ).all()
    )
    reviews = list(
        (
            await session.scalars(
                select(KnowledgeReview)
                .where(KnowledgeReview.knowledge_id == row.id)
                .order_by(KnowledgeReview.created_at.desc())
            )
        ).all()
    )
    related = list(
        (
            await session.scalars(
                select(ReliabilityKnowledge)
                .where(ReliabilityKnowledge.project_id == project_id)
                .where(ReliabilityKnowledge.knowledge_type == row.knowledge_type)
                .where(ReliabilityKnowledge.id != row.id)
                .where(ReliabilityKnowledge.scope == row.scope)
                .limit(10)
            )
        ).all()
    )
    experiences = list(
        (
            await session.scalars(
                select(ReliabilityExperience)
                .where(ReliabilityExperience.id.in_(_uuid_list(row.experience_ids)))
                .limit(50)
            )
        ).all()
    )

    return {
        "knowledge": _knowledge_row(row),
        "versions": [_version_row(item) for item in versions],
        "reviews": [_review_row(item) for item in reviews],
        "related": [_knowledge_row(item) for item in related],
        "experiences": [_experience_row(item) for item in experiences],
    }


async def list_experiences(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    component_id: Optional[uuid.UUID] = None,
    outcome: Optional[str] = None,
    data_quality: Optional[str] = None,
    since: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[ReliabilityExperience], int]:
    """Historical episodes for one project (§54)."""
    conditions = [ReliabilityExperience.project_id == project_id]
    if component_id is not None:
        conditions.append(ReliabilityExperience.primary_component_id == component_id)
    if outcome:
        conditions.append(ReliabilityExperience.outcome == outcome)
    if data_quality:
        conditions.append(ReliabilityExperience.data_quality == data_quality)
    if since is not None:
        conditions.append(ReliabilityExperience.start_time >= since)

    total = int(
        await session.scalar(
            select(func.count(ReliabilityExperience.id)).where(*conditions)
        )
        or 0
    )
    stmt = (
        select(ReliabilityExperience)
        .where(*conditions)
        .order_by(ReliabilityExperience.start_time.desc())
        .offset(max(0, (page - 1) * page_size))
        .limit(page_size)
    )
    return list((await session.scalars(stmt)).all()), total


async def experience_detail(
    session: AsyncSession, *, project_id: uuid.UUID, experience_id: uuid.UUID
) -> Optional[dict[str, Any]]:
    """One episode with the pipeline timeline it was assembled from (§54)."""
    row = await session.scalar(
        select(ReliabilityExperience)
        .where(ReliabilityExperience.id == experience_id)
        .where(ReliabilityExperience.project_id == project_id)
    )
    if row is None:
        return None

    incident = await session.get(Incident, row.incident_id) if row.incident_id else None
    component = (
        await session.get(SystemComponent, row.primary_component_id)
        if row.primary_component_id
        else None
    )
    action = (
        await session.get(RemediationAction, row.remediation_action_id)
        if row.remediation_action_id
        else None
    )

    timeline: list[dict[str, Any]] = [
        {
            "stage": "detection",
            "at": row.start_time.isoformat(),
            "detail": "incident started",
        },
    ]
    if row.causal_analysis_id:
        timeline.append(
            {
                "stage": "root_cause_analysis",
                "at": None,
                "detail": str(row.causal_analysis_id),
            }
        )
    if row.reproduction_id:
        timeline.append(
            {"stage": "reproduction", "at": None, "detail": str(row.reproduction_id)}
        )
    if row.patch_id:
        timeline.append({"stage": "fix", "at": None, "detail": str(row.patch_id)})
    if action is not None:
        timeline.append(
            {
                "stage": "remediation",
                "at": action.completed_at.isoformat() if action.completed_at else None,
                "detail": f"{getattr(action.action_type, 'value', action.action_type)} "
                f"({getattr(action.status, 'value', action.status)})",
            }
        )
    timeline.append(
        {
            "stage": "outcome",
            "at": row.end_time.isoformat(),
            "detail": row.outcome,
        }
    )

    return {
        "experience": _experience_row(row),
        "failure_signature": row.failure_signature,
        "resolution_signature": row.resolution_signature,
        "incident": (
            {
                "id": str(incident.id),
                "title": incident.title,
                "status": getattr(incident.status, "value", str(incident.status)),
                "severity": getattr(incident.severity, "value", str(incident.severity)),
            }
            if incident is not None
            else None
        ),
        "component": (
            {"id": str(component.id), "name": component.name}
            if component is not None
            else None
        ),
        "remediation": (
            {
                "id": str(action.id),
                "action_type": getattr(action.action_type, "value", action.action_type),
                "status": getattr(action.status, "value", action.status),
                "outcome": getattr(action.outcome, "value", action.outcome)
                if action.outcome
                else None,
            }
            if action is not None
            else None
        ),
        "timeline": timeline,
    }


async def list_recommendations(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    status: Optional[str] = None,
    recommendation_type: Optional[str] = None,
    component_id: Optional[uuid.UUID] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[ReliabilityRecommendation], int]:
    conditions = [ReliabilityRecommendation.project_id == project_id]
    if status:
        conditions.append(
            ReliabilityRecommendation.status == RecommendationStatus(str(status))
        )
    if recommendation_type:
        conditions.append(
            ReliabilityRecommendation.recommendation_type
            == _recommendation_type(recommendation_type)
        )
    if component_id is not None:
        conditions.append(ReliabilityRecommendation.component_id == component_id)

    total = int(
        await session.scalar(
            select(func.count(ReliabilityRecommendation.id)).where(*conditions)
        )
        or 0
    )
    stmt = (
        select(ReliabilityRecommendation)
        .where(*conditions)
        .order_by(ReliabilityRecommendation.created_at.desc())
        .offset(max(0, (page - 1) * page_size))
        .limit(page_size)
    )
    return list((await session.scalars(stmt)).all()), total


async def recommendation_detail(
    session: AsyncSession, *, project_id: uuid.UUID, recommendation_id: uuid.UUID
) -> Optional[dict[str, Any]]:
    row = await session.scalar(
        select(ReliabilityRecommendation)
        .where(ReliabilityRecommendation.id == recommendation_id)
        .where(ReliabilityRecommendation.project_id == project_id)
    )
    if row is None:
        return None
    outcomes = list(
        (
            await session.scalars(
                select(RecommendationOutcome)
                .where(RecommendationOutcome.recommendation_id == row.id)
                .order_by(RecommendationOutcome.recorded_at.desc())
            )
        ).all()
    )
    knowledge = list(
        (
            await session.scalars(
                select(ReliabilityKnowledge).where(
                    ReliabilityKnowledge.id.in_(_uuid_list(row.knowledge_ids))
                )
            )
        ).all()
    )
    experiences = list(
        (
            await session.scalars(
                select(ReliabilityExperience)
                .where(ReliabilityExperience.id.in_(_uuid_list(row.experience_ids)))
                .limit(50)
            )
        ).all()
    )
    return {
        "recommendation": _recommendation_row(row),
        "outcomes": [
            {
                "id": str(item.id),
                "verdict": item.verdict,
                "detail": item.detail,
                "recorded_at": item.recorded_at.isoformat(),
                "recorded_by": item.recorded_by,
            }
            for item in outcomes
        ],
        "knowledge": [_knowledge_row(item) for item in knowledge],
        "experiences": [_experience_row(item) for item in experiences],
    }


async def component_profile_detail(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    component_id: uuid.UUID,
    window_days: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """A component's reliability profile, plus the knowledge about it (§58)."""
    component = await session.get(SystemComponent, component_id)
    if component is None or component.project_id != project_id:
        return None

    stmt = (
        select(ComponentReliabilityProfile)
        .where(ComponentReliabilityProfile.project_id == project_id)
        .where(ComponentReliabilityProfile.component_id == component_id)
        .order_by(ComponentReliabilityProfile.window_days.asc())
    )
    if window_days is not None:
        stmt = stmt.where(ComponentReliabilityProfile.window_days == window_days)
    profiles = list((await session.scalars(stmt)).all())

    knowledge = list(
        (
            await session.scalars(
                select(ReliabilityKnowledge)
                .where(ReliabilityKnowledge.project_id == project_id)
                .where(ReliabilityKnowledge.component_id == component_id)
                .order_by(ReliabilityKnowledge.sample_count.desc())
                .limit(50)
            )
        ).all()
    )
    #: §23/§58. What history has observed *around* this component, kept separate
    #: from the structural graph the rest of the platform renders (§24).
    relationships, _total = await list_relationships(
        session, project_id=project_id, component_id=component_id, page_size=25
    )
    return {
        "component": {
            "id": str(component.id),
            "name": component.name,
            "category": getattr(component.component_type, "value", None),
        },
        "profiles": [_profile_row(item) for item in profiles],
        "knowledge": [_knowledge_row(item) for item in knowledge],
        "relationships": relationships,
    }


async def list_relationships(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    component_id: Optional[uuid.UUID] = None,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    min_samples: Optional[int] = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    """Learned component relationships for a project (§23, §24).

    Rows are returned already shaped, with both component names resolved and the
    ``HISTORICAL RELATIONSHIP`` label attached, because the one thing a client
    must not be able to do is show these as declared dependencies. Filtering by
    ``component_id`` matches either end — "what has history seen around this
    component" is the question the component view asks.
    """
    conditions = [LearnedRelationship.project_id == project_id]
    if component_id is not None:
        conditions.append(
            or_(
                LearnedRelationship.source_component_id == component_id,
                LearnedRelationship.target_component_id == component_id,
            )
        )
    if kind:
        conditions.append(LearnedRelationship.kind == _relationship_kind(kind))
    if status:
        conditions.append(LearnedRelationship.status == _relationship_status(status))
    else:
        #: Stale edges are history, not current belief; they are reachable by
        #: asking for them explicitly and are excluded from the default view.
        conditions.append(LearnedRelationship.status == RelationshipStatus.ACTIVE)
    if min_samples is not None:
        conditions.append(LearnedRelationship.sample_count >= min_samples)

    total = int(
        await session.scalar(
            select(func.count(LearnedRelationship.id)).where(*conditions)
        )
        or 0
    )
    stmt = (
        select(LearnedRelationship)
        .where(*conditions)
        .order_by(
            LearnedRelationship.sample_count.desc(),
            LearnedRelationship.last_seen_at.desc(),
            LearnedRelationship.source_component_id.asc(),
            LearnedRelationship.target_component_id.asc(),
        )
        .offset(max(page - 1, 0) * page_size)
        .limit(page_size)
    )
    rows = list((await session.scalars(stmt)).all())
    names = await _component_names(
        session,
        component_ids={
            value
            for row in rows
            for value in (row.source_component_id, row.target_component_id)
        },
    )
    return [_relationship_row(row, names) for row in rows], total


async def list_runs(
    session: AsyncSession,
    *,
    project_id: Optional[uuid.UUID] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[LearningRun], int]:
    conditions = []
    if project_id is not None:
        conditions.append(LearningRun.project_id == project_id)
    total = int(
        await session.scalar(select(func.count(LearningRun.id)).where(*conditions)) or 0
    )
    stmt = (
        select(LearningRun)
        .where(*conditions)
        .order_by(LearningRun.started_at.desc())
        .offset(max(0, (page - 1) * page_size))
        .limit(page_size)
    )
    return list((await session.scalars(stmt)).all()), total


async def run_detail(
    session: AsyncSession, *, run_id: uuid.UUID, project_id: Optional[uuid.UUID] = None
) -> Optional[dict[str, Any]]:
    stmt = select(LearningRun).where(LearningRun.id == run_id)
    if project_id is not None:
        stmt = stmt.where(LearningRun.project_id == project_id)
    run = await session.scalar(stmt)
    if run is None:
        return None
    events = list(
        (
            await session.scalars(
                select(LearningEvent)
                .where(LearningEvent.processed_by_run_id == run.id)
                .order_by(LearningEvent.occurred_at.asc())
                .limit(200)
            )
        ).all()
    )
    return {
        "run": _run_row(run),
        "events": [
            {
                "id": str(event.id),
                "event_type": event.event_type.value,
                "subject_id": str(event.subject_id),
                "occurred_at": event.occurred_at.isoformat(),
                "processed_at": event.processed_at.isoformat()
                if event.processed_at
                else None,
                "unprocessable_reason": event.unprocessable_reason,
                "provenance": event.provenance.value,
            }
            for event in events
        ],
    }


async def dashboard(session: AsyncSession, *, project_id: uuid.UUID) -> dict[str, Any]:
    """The §52 knowledge dashboard, counted from stored rows."""
    status_counts: dict[str, int] = {}
    for status in KnowledgeStatus:
        status_counts[status.value] = int(
            await session.scalar(
                select(func.count(ReliabilityKnowledge.id))
                .where(ReliabilityKnowledge.project_id == project_id)
                .where(ReliabilityKnowledge.status == status)
            )
            or 0
        )

    type_counts: dict[str, int] = {}
    rows = (
        await session.execute(
            select(
                ReliabilityKnowledge.knowledge_type, func.count(ReliabilityKnowledge.id)
            )
            .where(ReliabilityKnowledge.project_id == project_id)
            .group_by(ReliabilityKnowledge.knowledge_type)
        )
    ).all()
    for knowledge_type, count in rows:
        type_counts[getattr(knowledge_type, "value", str(knowledge_type))] = int(count)

    recent = list(
        (
            await session.scalars(
                select(ReliabilityKnowledge)
                .where(ReliabilityKnowledge.project_id == project_id)
                .order_by(ReliabilityKnowledge.updated_at.desc())
                .limit(5)
            )
        ).all()
    )
    experience_count = int(
        await session.scalar(
            select(func.count(ReliabilityExperience.id)).where(
                ReliabilityExperience.project_id == project_id
            )
        )
        or 0
    )
    open_recommendations = int(
        await session.scalar(
            select(func.count(ReliabilityRecommendation.id))
            .where(ReliabilityRecommendation.project_id == project_id)
            .where(ReliabilityRecommendation.status == RecommendationStatus.OPEN)
        )
        or 0
    )
    pending_events = int(
        await session.scalar(
            select(func.count(LearningEvent.id))
            .where(LearningEvent.project_id == project_id)
            .where(LearningEvent.processed_at.is_(None))
        )
        or 0
    )
    last_run = await session.scalar(
        select(LearningRun)
        .where(LearningRun.project_id == project_id)
        .order_by(LearningRun.started_at.desc())
        .limit(1)
    )
    chronic_components = int(
        await session.scalar(
            select(func.count(ComponentReliabilityProfile.id))
            .where(ComponentReliabilityProfile.project_id == project_id)
            .where(ComponentReliabilityProfile.chronic_signal.is_(True))
        )
        or 0
    )
    return {
        "knowledge_by_status": status_counts,
        "knowledge_by_type": type_counts,
        "active_knowledge": status_counts.get(KnowledgeStatus.ACTIVE.value, 0),
        "validated_knowledge": status_counts.get(KnowledgeStatus.VALIDATED.value, 0),
        "candidate_patterns": status_counts.get(KnowledgeStatus.CANDIDATE.value, 0),
        "stale_knowledge": status_counts.get(KnowledgeStatus.DEPRECATED.value, 0),
        "rejected_patterns": status_counts.get(KnowledgeStatus.REJECTED.value, 0),
        "recently_learned": [_knowledge_row(item) for item in recent],
        "experiences": experience_count,
        "open_recommendations": open_recommendations,
        "pending_events": pending_events,
        "chronic_components": chronic_components,
        "last_run": _run_row(last_run) if last_run is not None else None,
    }


async def learning_metrics(
    session: AsyncSession, *, project_id: Optional[uuid.UUID] = None
) -> dict[str, Any]:
    """The §80/§82 learning-quality metrics."""
    knowledge_conditions = (
        [ReliabilityKnowledge.project_id == project_id] if project_id else []
    )
    validated = int(
        await session.scalar(
            select(func.count(ReliabilityKnowledge.id))
            .where(*knowledge_conditions)
            .where(ReliabilityKnowledge.status.in_(LIVE_STATUSES))
        )
        or 0
    )
    rejected = int(
        await session.scalar(
            select(func.count(ReliabilityKnowledge.id))
            .where(*knowledge_conditions)
            .where(ReliabilityKnowledge.status == KnowledgeStatus.REJECTED)
        )
        or 0
    )
    candidates = int(
        await session.scalar(
            select(func.count(ReliabilityKnowledge.id))
            .where(*knowledge_conditions)
            .where(ReliabilityKnowledge.status == KnowledgeStatus.CANDIDATE)
        )
        or 0
    )
    stale = int(
        await session.scalar(
            select(func.count(ReliabilityKnowledge.id))
            .where(*knowledge_conditions)
            .where(ReliabilityKnowledge.status == KnowledgeStatus.DEPRECATED)
        )
        or 0
    )

    run_conditions = [LearningRun.project_id == project_id] if project_id else []
    runs = int(
        await session.scalar(select(func.count(LearningRun.id)).where(*run_conditions))
        or 0
    )
    failures = int(
        await session.scalar(
            select(func.count(LearningRun.id))
            .where(*run_conditions)
            .where(LearningRun.error_summary.isnot(None))
        )
        or 0
    )
    events_total = int(
        await session.scalar(
            select(func.count(LearningEvent.id)).where(
                *([LearningEvent.project_id == project_id] if project_id else [])
            )
        )
        or 0
    )
    events_pending = int(
        await session.scalar(
            select(func.count(LearningEvent.id))
            .where(*([LearningEvent.project_id == project_id] if project_id else []))
            .where(LearningEvent.processed_at.is_(None))
        )
        or 0
    )
    experiences = int(
        await session.scalar(
            select(func.count(ReliabilityExperience.id)).where(
                *(
                    [ReliabilityExperience.project_id == project_id]
                    if project_id
                    else []
                )
            )
        )
        or 0
    )
    poor_experiences = int(
        await session.scalar(
            select(func.count(ReliabilityExperience.id))
            .where(
                *(
                    [ReliabilityExperience.project_id == project_id]
                    if project_id
                    else []
                )
            )
            .where(ReliabilityExperience.data_quality == "POOR")
        )
        or 0
    )

    #: §80. The relationship stage is observable too: a project whose learning
    #: runs keep completing while its graph stays empty is a signal, not silence.
    relationship_conditions = (
        [LearnedRelationship.project_id == project_id] if project_id else []
    )
    relationships_active = int(
        await session.scalar(
            select(func.count(LearnedRelationship.id))
            .where(*relationship_conditions)
            .where(LearnedRelationship.status == RelationshipStatus.ACTIVE)
        )
        or 0
    )
    relationships_stale = int(
        await session.scalar(
            select(func.count(LearnedRelationship.id))
            .where(*relationship_conditions)
            .where(LearnedRelationship.status == RelationshipStatus.STALE)
        )
        or 0
    )
    #: Edges with no direction: reported separately so "history saw them fail
    #: together" is never counted as "failures travel from one to the other".
    relationships_undirected = int(
        await session.scalar(
            select(func.count(LearnedRelationship.id))
            .where(*relationship_conditions)
            .where(LearnedRelationship.directed.is_(False))
        )
        or 0
    )

    rec_conditions = (
        [ReliabilityRecommendation.project_id == project_id] if project_id else []
    )
    rec_by_status: dict[str, int] = {}
    for status in RecommendationStatus:
        rec_by_status[status.value] = int(
            await session.scalar(
                select(func.count(ReliabilityRecommendation.id))
                .where(*rec_conditions)
                .where(ReliabilityRecommendation.status == status)
            )
            or 0
        )

    total_decided = sum(
        rec_by_status.get(status, 0)
        for status in ("EFFECTIVE", "INEFFECTIVE", "REGRESSION_CAUSING")
    )
    return {
        "learning_runs": runs,
        "learning_failures": failures,
        "events_total": events_total,
        "events_pending": events_pending,
        "experiences": experiences,
        "experiences_poor_quality": poor_experiences,
        "knowledge_validated_or_active": validated,
        "knowledge_candidates": candidates,
        "knowledge_rejected": rejected,
        "knowledge_stale": stale,
        "relationships_active": relationships_active,
        "relationships_stale": relationships_stale,
        "relationships_undirected": relationships_undirected,
        "pattern_validation_rate": (
            validated / (validated + rejected) if (validated + rejected) else None
        ),
        "recommendations_by_status": rec_by_status,
        "recommendations_decided": total_decided,
        "recommendation_success_rate": (
            rec_by_status.get("EFFECTIVE", 0) / total_decided if total_decided else None
        ),
    }


# --------------------------------------------------------------------------
# Row serialization
# --------------------------------------------------------------------------


def _knowledge_row(row: ReliabilityKnowledge) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "knowledge_type": row.knowledge_type.value,
        "status": row.status.value,
        "scope": row.scope.value,
        "component_id": str(row.component_id) if row.component_id else None,
        "environment_id": str(row.environment_id) if row.environment_id else None,
        "title": row.title,
        "description": row.description,
        "feature_signature": row.feature_signature,
        "sample_count": row.sample_count,
        "success_count": row.success_count,
        "support_strength": row.support_strength,
        "coverage_start": row.coverage_start.isoformat()
        if row.coverage_start
        else None,
        "coverage_end": row.coverage_end.isoformat() if row.coverage_end else None,
        "confidence": row.confidence.value,
        "algorithm": row.algorithm,
        "algorithm_version": row.algorithm_version,
        "feature_schema_version": row.feature_schema_version,
        "validation": row.validation,
        "limitations": list(row.limitations or []),
        "version": row.version,
        "supersedes_knowledge_id": (
            str(row.supersedes_knowledge_id) if row.supersedes_knowledge_id else None
        ),
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "reviewed_by": row.reviewed_by,
        "review_reason": row.review_reason,
        "last_confirmed_at": (
            row.last_confirmed_at.isoformat() if row.last_confirmed_at else None
        ),
        "sources": list(row.sources or []),
        "experience_ids": list(row.experience_ids or []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _experience_row(row: ReliabilityExperience) -> dict[str, Any]:
    failure = FailureSignature.from_dict(row.failure_signature)
    resolution = (
        ResolutionSignature.from_dict(row.resolution_signature)
        if row.resolution_signature
        else None
    )
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "incident_id": str(row.incident_id) if row.incident_id else None,
        "environment_id": str(row.environment_id) if row.environment_id else None,
        "primary_component_id": (
            str(row.primary_component_id) if row.primary_component_id else None
        ),
        "remediation_action_id": (
            str(row.remediation_action_id) if row.remediation_action_id else None
        ),
        "start_time": row.start_time.isoformat(),
        "end_time": row.end_time.isoformat(),
        "recovery_seconds": row.recovery_seconds,
        "outcome": row.outcome,
        "data_quality": row.data_quality,
        "provenance": row.provenance.value,
        "component_ids": list(row.component_ids or []),
        "failure_signature": row.failure_signature,
        "failure_label": failure.label(),
        "failure_fingerprint": row.failure_fingerprint,
        "resolution_signature": row.resolution_signature,
        "resolution_label": resolution.label() if resolution is not None else None,
        "learning_run_id": str(row.learning_run_id) if row.learning_run_id else None,
    }


def _recommendation_row(row: ReliabilityRecommendation) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "recommendation_type": row.recommendation_type.value,
        "status": row.status.value,
        "title": row.title,
        "rationale": row.rationale,
        "confidence": row.confidence.value,
        "component_id": str(row.component_id) if row.component_id else None,
        "environment_id": str(row.environment_id) if row.environment_id else None,
        "incident_id": str(row.incident_id) if row.incident_id else None,
        "forecast_id": str(row.forecast_id) if row.forecast_id else None,
        "knowledge_ids": list(row.knowledge_ids or []),
        "experience_ids": list(row.experience_ids or []),
        "current_evidence": row.current_evidence,
        "limitations": list(row.limitations or []),
        "ranking": row.ranking,
        "historical": row.historical,
        "policy_note": row.policy_note,
        "decision": row.decision,
        "outcome": row.outcome,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _profile_row(row: ComponentReliabilityProfile) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "component_id": str(row.component_id),
        "window_days": row.window_days,
        "computed_at": row.computed_at.isoformat(),
        "incident_count": row.incident_count,
        "anomaly_count": row.anomaly_count,
        "remediation_count": row.remediation_count,
        "rollback_count": row.rollback_count,
        "regression_count": row.regression_count,
        "mean_recovery_seconds": row.mean_recovery_seconds,
        "forecast_outcome_count": row.forecast_outcome_count,
        "forecast_true_positive_count": row.forecast_true_positive_count,
        "chronic_signal": row.chronic_signal,
        "chronic_reasons": list(row.chronic_reasons or []),
        "breakdown": row.breakdown,
    }


def _relationship_row(
    row: LearnedRelationship, names: dict[str, str]
) -> dict[str, Any]:
    """One learned relationship, labelled so it cannot pass for a dependency.

    ``is_dependency`` is always ``False`` and the disclaimer is included in the
    payload rather than left to the client — a view that renders an arrow for a
    co-occurrence has to ignore a field that says not to (§24).
    """
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "environment_id": str(row.environment_id) if row.environment_id else None,
        "source_component_id": str(row.source_component_id),
        "source_component_name": names.get(
            str(row.source_component_id), str(row.source_component_id)
        ),
        "target_component_id": str(row.target_component_id),
        "target_component_name": names.get(
            str(row.target_component_id), str(row.target_component_id)
        ),
        "kind": row.kind.value,
        "directed": row.directed,
        "status": row.status.value,
        "sample_count": row.sample_count,
        "supporting_count": row.supporting_count,
        "support_strength": (
            row.supporting_count / row.sample_count
            if row.supporting_count is not None and row.sample_count
            else None
        ),
        "confidence": row.confidence.value,
        "evidence": list(row.evidence or []),
        "limitations": list(row.limitations or []),
        "provenance": row.provenance.value,
        "algorithm": row.algorithm,
        "algorithm_version": row.algorithm_version,
        "feature_schema_version": row.feature_schema_version,
        "coverage_start": _iso(row.coverage_start),
        "coverage_end": _iso(row.coverage_end),
        "first_seen_at": _iso(row.first_seen_at),
        "last_seen_at": _iso(row.last_seen_at),
        "learning_run_id": str(row.learning_run_id) if row.learning_run_id else None,
        #: §24. Never true: these are learned from episodes, not declared.
        "is_dependency": False,
        "disclaimer": HISTORICAL_RELATIONSHIP_NOTE,
    }


async def _component_names(
    session: AsyncSession, *, component_ids: set[uuid.UUID]
) -> dict[str, str]:
    """Names for the components an edge connects, in one query."""
    if not component_ids:
        return {}
    #: ``execute``, not ``scalars``: a two-column select through ``scalars``
    #: yields only the first column, which would silently produce "names" made of
    #: component ids.
    result = await session.execute(
        select(SystemComponent.id, SystemComponent.name).where(
            SystemComponent.id.in_(component_ids)
        )
    )
    return {str(component_id): name for component_id, name in result.all()}


def _run_row(row: LearningRun) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id) if row.project_id else None,
        "status": row.status.value,
        "trigger": row.trigger,
        "data_cutoff": row.data_cutoff.isoformat(),
        "last_processed_at": (
            row.last_processed_at.isoformat() if row.last_processed_at else None
        ),
        "started_at": row.started_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "events_processed": row.events_processed,
        "experiences_created": row.experiences_created,
        "experiences_updated": row.experiences_updated,
        "patterns_discovered": row.patterns_discovered,
        "patterns_validated": row.patterns_validated,
        "patterns_rejected": row.patterns_rejected,
        "knowledge_activated": row.knowledge_activated,
        "records_flagged": row.records_flagged,
        "relationships_created": row.relationships_created,
        "relationships_updated": row.relationships_updated,
        "algorithm_versions": row.algorithm_versions,
        "error_summary": row.error_summary,
    }


def _version_row(row: KnowledgeVersion) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "version": row.version,
        "status": row.status.value,
        "confidence": row.confidence.value,
        "sample_count": row.sample_count,
        "snapshot": row.snapshot,
        "note": row.note,
        "learning_run_id": str(row.learning_run_id) if row.learning_run_id else None,
        "activated_at": row.activated_at.isoformat() if row.activated_at else None,
        "deactivated_at": row.deactivated_at.isoformat()
        if row.deactivated_at
        else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _review_row(row: KnowledgeReview) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "decision": row.decision,
        "reviewer": row.reviewer,
        "reason": row.reason,
        "knowledge_version": row.knowledge_version,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


#: Public aliases for the row serializers. The routes use these, and naming them
#: here keeps one definition of what a knowledge/experience/recommendation
#: payload looks like instead of a copy per endpoint.
knowledge_row = _knowledge_row
experience_row = _experience_row
recommendation_row = _recommendation_row
profile_row = _profile_row
run_row = _run_row
relationship_row = _relationship_row
version_row = _version_row


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _relationship_kind(value: str) -> RelationshipKind:
    """An unknown kind is refused, never defaulted.

    ``?kind=<typo>`` answering with every relationship would silently answer a
    different question than the one asked, and a caller cannot tell the
    difference from a filter that genuinely matched everything.
    """
    try:
        return RelationshipKind(value.strip().upper())
    except ValueError as exc:
        raise ValueError(f"unknown relationship kind: {value}") from exc


def _relationship_status(value: str) -> RelationshipStatus:
    try:
        return RelationshipStatus(value.strip().upper())
    except ValueError as exc:
        raise ValueError(f"unknown relationship status: {value}") from exc


def _uuid_list(values: Optional[Sequence[Any]]) -> list[uuid.UUID]:
    resolved: list[uuid.UUID] = []
    for value in values or []:
        try:
            resolved.append(uuid.UUID(str(value)))
        except (ValueError, AttributeError, TypeError):
            continue
    return resolved


def _status(value: str) -> KnowledgeStatus:
    return KnowledgeStatus(str(value))


def _knowledge_type(value: str) -> KnowledgeType:
    return KnowledgeType(str(value))


def _recommendation_type(value: str):
    from app.models.intelligence import RecommendationType

    return RecommendationType(str(value))


__all__ = [
    "LIVE_STATUSES",
    "RETIRED_STATUSES",
    "component_profile_detail",
    "dashboard",
    "experience_detail",
    "get_knowledge",
    "knowledge_detail",
    "learning_metrics",
    "list_experiences",
    "list_knowledge",
    "list_recommendations",
    "list_runs",
    "experience_row",
    "knowledge_row",
    "profile_row",
    "recommendation_detail",
    "recommendation_row",
    "run_detail",
    "run_row",
    "version_row",
]
