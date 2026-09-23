"""ARGUS Reliability Intelligence Routes (Phase 10 §61, §62).

```text
GET    /intelligence/health                       is the learning layer working? (§80)
GET    /intelligence/dashboard                    the §52 knowledge dashboard
GET    /intelligence/metrics                      the §80/§82 quality metrics

GET    /intelligence/knowledge                    learned patterns, filtered (§53)
GET    /intelligence/knowledge/{id}               pattern + versions + reviews + evidence
POST   /intelligence/knowledge/{id}/review        a human decision (§72)
GET    /intelligence/knowledge/{id}/versions      the version ledger (§26)

GET    /intelligence/experiences                  historical episodes (§54)
GET    /intelligence/experiences/{id}             one episode with its timeline
GET    /intelligence/patterns                     knowledge narrowed to patterns (§56)
GET    /intelligence/patterns/{id}                same as the knowledge detail

GET    /intelligence/recommendations              evidence-backed advice (§57)
GET    /intelligence/recommendations/{id}         one recommendation + its outcomes
POST   /intelligence/recommendations/{id}/decide      accept or dismiss (§43)
POST   /intelligence/recommendations/{id}/outcome     record what happened (§43, §81)
GET    /intelligence/incidents/{id}/recommendations   advice for one incident

GET    /intelligence/components/{id}/profile      component learning profile (§58)
GET    /intelligence/relationships                learned component relationships (§23, §24)
GET    /intelligence/remediation-effectiveness    contextual effectiveness (§15, §16)
GET    /intelligence/remediation-effectiveness/compare  two actions, side by side (§45)
GET    /intelligence/search                       grounded question answering (§46–§49)

GET    /intelligence/learning-runs                run history (§59)
GET    /intelligence/learning-runs/{id}           one run and the events it consumed
POST   /intelligence/learning-runs                trigger a run by hand (§63)

GET    /intelligence/event-hooks                  what is learned from (§63, §76)
PUT    /intelligence/event-hooks                  narrow that set
POST   /intelligence/sweep                        run the scheduled work now (§29)
```

Scope rules follow every previous phase: mutating requests **require** a project
and prove ownership, reads accept an optional project and enforce it when
supplied, and an out-of-scope id answers 404 rather than confirming existence
(§62). There is no endpoint here that executes anything: the most a
recommendation endpoint can do is record what a person decided.

Knowledge a human has *not* approved is returned with its status — ``CANDIDATE``
and ``VALIDATING`` items are visible on purpose, because §53 requires the review
surface to show what is waiting rather than only what is believed.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_component, require_incident, require_project
from app.core.config import get_settings
from app.core.database import get_db
from app.models.intelligence import (
    KnowledgeStatus,
    KnowledgeType,
    LearningEventType,
    LearningExperiment,
    RecommendationStatus,
    RecommendationType,
    ReliabilityRecommendation,
)
from app.schemas.intelligence import (
    ActionComparisonResponse,
    ComponentProfileResponse,
    DashboardResponse,
    EffectivenessBucketItem,
    EffectivenessResponse,
    EventHookResponse,
    EventHookUpdateRequest,
    ExperienceDetailResponse,
    ExperienceListResponse,
    IntelligenceHealthResponse,
    KnowledgeDetailResponse,
    KnowledgeListResponse,
    KnowledgeReviewRequest,
    KnowledgeVersionItem,
    KnowledgeVersionListResponse,
    LearningMetricsResponse,
    LearningRunDetailResponse,
    LearningRunListResponse,
    LearningRunRequest,
    LearningRunSummaryResponse,
    RecommendationDecisionRequest,
    RecommendationDetailResponse,
    RecommendationListResponse,
    RecommendationOutcomeRequest,
    RelationshipListResponse,
    SearchResponse,
    SweepResponse,
)
from app.services import intelligence_service as service
from app.services.intelligence_sweep import run_learning_sweep
from app.services.knowledge_lifecycle import (
    activate_knowledge,
    deprecate_knowledge,
    list_knowledge_versions,
    reject_knowledge,
    request_more_evidence,
)
from app.services.knowledge_search import KnowledgeSearchService
from app.services.learning_events import (
    enabled_event_types,
    set_event_hook,
    trusted_provenance,
)
from app.services.learning_run import (
    execute_learning_run,
    latest_run,
    pending_event_count,
)
from app.services.remediation_effectiveness import (
    OBSERVATIONAL_LABEL,
    action_effectiveness,
    compare_actions,
)
from app.services.recommendation_engine import ReliabilityRecommendationEngine
from app.services.relationship_builder import HISTORICAL_RELATIONSHIP_NOTE

logger = logging.getLogger(__name__)

router = APIRouter(tags=["intelligence"])


def _pagination(
    items: list[Any], total: int, page: int, page_size: int
) -> dict[str, Any]:
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if page_size else 0,
    }


async def _require_recommendation(
    db: AsyncSession, recommendation_id: uuid.UUID, project_id: uuid.UUID
) -> ReliabilityRecommendation:
    row = await db.scalar(
        select(ReliabilityRecommendation)
        .where(ReliabilityRecommendation.id == recommendation_id)
        .where(ReliabilityRecommendation.project_id == project_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return row


# ---------------------------------------------------------------------------
# Health, dashboard, metrics
# ---------------------------------------------------------------------------


@router.get(
    "/intelligence/health",
    response_model=IntelligenceHealthResponse,
    summary="Learning layer health",
)
async def intelligence_health(
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> IntelligenceHealthResponse:
    settings = get_settings()
    if project_id is not None:
        await require_project(db, project_id)
    run = await latest_run(db, project_id=project_id)
    return IntelligenceHealthResponse(
        learning_enabled=settings.INTELLIGENCE_LEARNING_ENABLED,
        sweep_enabled=settings.INTELLIGENCE_SWEEP_ENABLED,
        auto_activation_enabled=settings.INTELLIGENCE_AUTO_ACTIVATE_ENABLED,
        include_ai_generated=settings.INTELLIGENCE_INCLUDE_AI_GENERATED,
        pending_events=await pending_event_count(db, project_id=project_id),
        last_run_status=run.status.value if run is not None else None,
        last_run_at=run.started_at if run is not None else None,
        knowledge_stale_after_days=settings.INTELLIGENCE_KNOWLEDGE_STALE_AFTER_DAYS,
        minimum_samples={
            "candidate": settings.INTELLIGENCE_MIN_SAMPLES_CANDIDATE,
            "validation": settings.INTELLIGENCE_MIN_SAMPLES_VALIDATION,
            "high_confidence": settings.INTELLIGENCE_MIN_SAMPLES_HIGH_CONFIDENCE,
            "effectiveness": settings.INTELLIGENCE_MIN_EFFECTIVENESS_SAMPLES,
        },
    )


@router.get(
    "/intelligence/dashboard",
    response_model=DashboardResponse,
    summary="Knowledge dashboard",
)
async def intelligence_dashboard(
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> DashboardResponse:
    await require_project(db, project_id)
    payload = await service.dashboard(db, project_id=project_id)
    return DashboardResponse(**payload)


@router.get(
    "/intelligence/metrics",
    response_model=LearningMetricsResponse,
    summary="Learning quality metrics",
)
async def intelligence_metrics(
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> LearningMetricsResponse:
    if project_id is not None:
        await require_project(db, project_id)
    payload = await service.learning_metrics(db, project_id=project_id)
    return LearningMetricsResponse(**payload)


# ---------------------------------------------------------------------------
# Knowledge
# ---------------------------------------------------------------------------


@router.get(
    "/intelligence/knowledge",
    response_model=KnowledgeListResponse,
    summary="List learned knowledge",
)
async def list_knowledge(
    project_id: uuid.UUID = Query(...),
    status: Optional[str] = Query(None, description="KnowledgeStatus value"),
    knowledge_type: Optional[str] = Query(None, description="KnowledgeType value"),
    component_id: Optional[uuid.UUID] = Query(None),
    search: Optional[str] = Query(None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeListResponse:
    await require_project(db, project_id)
    _validate_enum(KnowledgeStatus, status)
    _validate_enum(KnowledgeType, knowledge_type)
    rows, total = await service.list_knowledge(
        db,
        project_id=project_id,
        status=status,
        knowledge_type=knowledge_type,
        component_id=component_id,
        search=search,
        page=page,
        page_size=page_size,
    )
    items = [service.knowledge_row(row) for row in rows]
    return KnowledgeListResponse(**_pagination(items, total, page, page_size))


@router.get(
    "/intelligence/knowledge/{knowledge_id}",
    response_model=KnowledgeDetailResponse,
    summary="Knowledge detail with its evidence",
)
async def get_knowledge(
    knowledge_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeDetailResponse:
    await require_project(db, project_id)
    payload = await service.knowledge_detail(
        db, project_id=project_id, knowledge_id=knowledge_id
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="Knowledge not found")
    return KnowledgeDetailResponse(**payload)


@router.post(
    "/intelligence/knowledge/{knowledge_id}/review",
    response_model=KnowledgeDetailResponse,
    summary="Review a learned pattern",
)
async def review_knowledge(
    knowledge_id: uuid.UUID,
    payload: KnowledgeReviewRequest,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeDetailResponse:
    """§72. A person approves, rejects, asks for evidence, or deprecates.

    ``APPROVE`` requires the row to be at least VALIDATED: activation is a step
    in the lifecycle, not a way to skip validation, and the refusal is a 409 with
    the reason rather than a silent no-op.
    """
    await require_project(db, project_id)
    knowledge = await service.get_knowledge(
        db, project_id=project_id, knowledge_id=knowledge_id
    )
    if knowledge is None:
        raise HTTPException(status_code=404, detail="Knowledge not found")

    decision = payload.decision.upper()
    try:
        if decision == "APPROVE":
            await activate_knowledge(
                db,
                knowledge,
                reviewer=payload.reviewer,
                reason=payload.reason,
            )
        elif decision == "REJECT":
            await reject_knowledge(
                db, knowledge, reviewer=payload.reviewer, reason=payload.reason
            )
        elif decision == "REQUEST_MORE_EVIDENCE":
            await request_more_evidence(
                db,
                knowledge,
                reviewer=payload.reviewer,
                reason=payload.reason,
            )
        elif decision == "DEPRECATE":
            await deprecate_knowledge(
                db,
                knowledge,
                actor=payload.reviewer,
                reason=payload.reason,
            )
        else:
            raise HTTPException(
                status_code=422,
                detail="decision must be APPROVE, REJECT, REQUEST_MORE_EVIDENCE or DEPRECATE",
            )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await db.commit()
    detail = await service.knowledge_detail(
        db, project_id=project_id, knowledge_id=knowledge_id
    )
    assert detail is not None
    return KnowledgeDetailResponse(**detail)


@router.get(
    "/intelligence/knowledge/{knowledge_id}/versions",
    response_model=KnowledgeVersionListResponse,
    summary="The version ledger for one pattern",
)
async def knowledge_versions(
    knowledge_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeVersionListResponse:
    """§26. What ARGUS believed at each revision, and when it changed its mind."""
    await require_project(db, project_id)
    knowledge = await service.get_knowledge(
        db, project_id=project_id, knowledge_id=knowledge_id
    )
    if knowledge is None:
        raise HTTPException(status_code=404, detail="Knowledge not found")
    rows = await list_knowledge_versions(db, knowledge_id=knowledge_id)
    items = [KnowledgeVersionItem(**service.version_row(row)) for row in rows]
    return KnowledgeVersionListResponse(
        items=items,
        total=len(items),
        page=1,
        page_size=max(len(items), 1),
        total_pages=1 if items else 0,
    )


# ---------------------------------------------------------------------------
# Experiences and patterns
# ---------------------------------------------------------------------------


@router.get(
    "/intelligence/experiences",
    response_model=ExperienceListResponse,
    summary="Historical reliability experiences",
)
async def list_experiences(
    project_id: uuid.UUID = Query(...),
    component_id: Optional[uuid.UUID] = Query(None),
    outcome: Optional[str] = Query(None, max_length=40),
    data_quality: Optional[str] = Query(None, max_length=20),
    since: Optional[datetime] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> ExperienceListResponse:
    await require_project(db, project_id)
    rows, total = await service.list_experiences(
        db,
        project_id=project_id,
        component_id=component_id,
        outcome=outcome,
        data_quality=data_quality,
        since=since,
        page=page,
        page_size=page_size,
    )
    items = [service.experience_row(row) for row in rows]
    return ExperienceListResponse(**_pagination(items, total, page, page_size))


@router.get(
    "/intelligence/experiences/{experience_id}",
    response_model=ExperienceDetailResponse,
    summary="One experience with its pipeline timeline",
)
async def get_experience(
    experience_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> ExperienceDetailResponse:
    await require_project(db, project_id)
    payload = await service.experience_detail(
        db, project_id=project_id, experience_id=experience_id
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="Experience not found")
    return ExperienceDetailResponse(**payload)


@router.get(
    "/intelligence/patterns",
    response_model=KnowledgeListResponse,
    summary="Pattern explorer",
)
async def list_patterns(
    project_id: uuid.UUID = Query(...),
    knowledge_type: Optional[str] = Query(None),
    component_id: Optional[uuid.UUID] = Query(None),
    search: Optional[str] = Query(None, max_length=200),
    include_retired: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeListResponse:
    """§56. Knowledge narrowed to pattern types, live ones by default.

    Retired patterns are excluded unless asked for: a deprecated pattern shown
    among live ones is how stale advice survives its own expiry.
    """
    await require_project(db, project_id)
    _validate_enum(KnowledgeType, knowledge_type)
    rows, total = await service.list_knowledge(
        db,
        project_id=project_id,
        status=None,
        knowledge_type=knowledge_type,
        component_id=component_id,
        search=search,
        page=1 if include_retired else page,
        page_size=page_size if include_retired else 100,
    )
    if not include_retired:
        rows = [row for row in rows if row.status.value not in service.RETIRED_STATUSES]
        total = len(rows)
        start = (page - 1) * page_size
        rows = rows[start : start + page_size]
    items = [service.knowledge_row(row) for row in rows]
    return KnowledgeListResponse(**_pagination(items, total, page, page_size))


@router.get(
    "/intelligence/patterns/{knowledge_id}",
    response_model=KnowledgeDetailResponse,
    summary="One pattern, same detail as knowledge",
)
async def get_pattern(
    knowledge_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeDetailResponse:
    return await get_knowledge(knowledge_id, project_id, db)


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


@router.get(
    "/intelligence/recommendations",
    response_model=RecommendationListResponse,
    summary="List recommendations",
)
async def list_recommendations(
    project_id: uuid.UUID = Query(...),
    status: Optional[str] = Query(None),
    recommendation_type: Optional[str] = Query(None),
    component_id: Optional[uuid.UUID] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> RecommendationListResponse:
    await require_project(db, project_id)
    _validate_enum(RecommendationStatus, status)
    _validate_enum(RecommendationType, recommendation_type)
    rows, total = await service.list_recommendations(
        db,
        project_id=project_id,
        status=status,
        recommendation_type=recommendation_type,
        component_id=component_id,
        page=page,
        page_size=page_size,
    )
    items = [service.recommendation_row(row) for row in rows]
    return RecommendationListResponse(**_pagination(items, total, page, page_size))


@router.get(
    "/intelligence/incidents/{incident_id}/recommendations",
    response_model=RecommendationListResponse,
    summary="Recommendations for one incident",
)
async def incident_recommendations(
    incident_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    generate: bool = Query(
        False,
        description="Generate fresh recommendations instead of listing stored ones",
    ),
    db: AsyncSession = Depends(get_db),
) -> RecommendationListResponse:
    """Listing is the default; ``generate=true`` asks for a fresh evaluation.

    Generation is explicit because it writes rows. A plain GET that silently
    created records would make the audit trail depend on who happened to open a
    page.
    """
    await require_project(db, project_id)
    await require_incident(db, incident_id, project_id=project_id)

    if generate:
        engine = ReliabilityRecommendationEngine()
        ranked = await engine.recommend_for_incident(
            db, project_id=project_id, incident_id=incident_id
        )
        await engine.persist_many(db, ranked)
        await db.commit()

    rows, total = await service.list_recommendations(
        db, project_id=project_id, page=1, page_size=100
    )
    filtered = [row for row in rows if row.incident_id == incident_id]
    items = [service.recommendation_row(row) for row in filtered]
    return RecommendationListResponse(**_pagination(items, len(filtered), 1, 100))


@router.get(
    "/intelligence/recommendations/{recommendation_id}",
    response_model=RecommendationDetailResponse,
    summary="One recommendation with its evidence",
)
async def get_recommendation(
    recommendation_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> RecommendationDetailResponse:
    await require_project(db, project_id)
    payload = await service.recommendation_detail(
        db, project_id=project_id, recommendation_id=recommendation_id
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return RecommendationDetailResponse(**payload)


@router.post(
    "/intelligence/recommendations/{recommendation_id}/decide",
    response_model=RecommendationDetailResponse,
    summary="Accept or dismiss a recommendation",
)
async def decide_recommendation(
    recommendation_id: uuid.UUID,
    payload: RecommendationDecisionRequest,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> RecommendationDetailResponse:
    """§43. This records a decision. It does not execute anything."""
    await require_project(db, project_id)
    row = await _require_recommendation(db, recommendation_id, project_id)
    engine = ReliabilityRecommendationEngine()
    try:
        await engine.decide(
            db,
            row,
            decision=payload.decision,
            actor=payload.actor,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    detail = await service.recommendation_detail(
        db, project_id=project_id, recommendation_id=recommendation_id
    )
    assert detail is not None
    return RecommendationDetailResponse(**detail)


@router.post(
    "/intelligence/recommendations/{recommendation_id}/outcome",
    response_model=RecommendationDetailResponse,
    summary="Record what actually happened",
)
async def record_recommendation_outcome(
    recommendation_id: uuid.UUID,
    payload: RecommendationOutcomeRequest,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> RecommendationDetailResponse:
    """§43, §81. Acceptance is not correctness; this is where correctness lands."""
    await require_project(db, project_id)
    row = await _require_recommendation(db, recommendation_id, project_id)
    engine = ReliabilityRecommendationEngine()
    try:
        await engine.record_outcome(
            db,
            row,
            verdict=payload.verdict,
            recorded_by=payload.recorded_by,
            detail=payload.detail,
            remediation_action_id=payload.remediation_action_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    detail = await service.recommendation_detail(
        db, project_id=project_id, recommendation_id=recommendation_id
    )
    assert detail is not None
    return RecommendationDetailResponse(**detail)


# ---------------------------------------------------------------------------
# Components and effectiveness
# ---------------------------------------------------------------------------


@router.get(
    "/intelligence/components/{component_id}/profile",
    response_model=ComponentProfileResponse,
    summary="Component learning profile",
)
async def component_profile(
    component_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    window_days: Optional[int] = Query(None, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> ComponentProfileResponse:
    await require_project(db, project_id)
    await require_component(db, component_id, project_id=project_id)
    payload = await service.component_profile_detail(
        db, project_id=project_id, component_id=component_id, window_days=window_days
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="Component not found")
    return ComponentProfileResponse(**payload)


@router.get(
    "/intelligence/relationships",
    response_model=RelationshipListResponse,
    summary="Learned component relationships",
)
async def list_relationships(
    project_id: uuid.UUID = Query(...),
    component_id: Optional[uuid.UUID] = Query(None),
    kind: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    min_samples: Optional[int] = Query(None, ge=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> RelationshipListResponse:
    """§23/§24. Relationships learned from episodes, never declared dependencies.

    These live in ``intelligence_relationships``, apart from ``graph_edges``, and
    every row carries ``is_dependency: false`` plus the historical-relationship
    disclaimer. A client that renders them as architecture is contradicting the
    payload it was sent, not reading it.

    ``UNKNOWN``/``STALE`` edges are excluded from the default view and reachable
    by asking for them: what history no longer confirms is not current belief.
    """
    await require_project(db, project_id)
    try:
        items, total = await service.list_relationships(
            db,
            project_id=project_id,
            component_id=component_id,
            kind=kind,
            status=status,
            min_samples=min_samples,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        #: An unknown filter value is a 422, not an empty page: answering "no
        #: results" to a typo would look exactly like answering the real question.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return RelationshipListResponse(
        **_pagination(items, total, page, page_size),
        relationship_note=HISTORICAL_RELATIONSHIP_NOTE,
        limitations=[
            "Relationships are derived from completed episodes and are not a statement of architectural dependency",
            "A direction is shown only when a supported or human-confirmed root cause established it",
        ],
    )


@router.get(
    "/intelligence/remediation-effectiveness",
    response_model=EffectivenessResponse,
    summary="Contextual remediation effectiveness",
)
async def remediation_effectiveness(
    project_id: uuid.UUID = Query(...),
    action_type: Optional[str] = Query(None, max_length=60),
    breakdown: Optional[str] = Query(
        None,
        description="Comma-separated: action,component,environment,failure_pattern,severity",
    ),
    component_id: Optional[uuid.UUID] = Query(None),
    environment_id: Optional[uuid.UUID] = Query(None),
    lookback_days: Optional[int] = Query(None, ge=1, le=3650),
    db: AsyncSession = Depends(get_db),
) -> EffectivenessResponse:
    """§15/§16. Counts over comparable cases, never a bare percentage."""
    await require_project(db, project_id)
    dimensions = (
        [item.strip() for item in breakdown.split(",") if item.strip()]
        if breakdown
        else ["action", "component", "failure_pattern"]
    )
    buckets = await action_effectiveness(
        db,
        project_id=project_id,
        action_type=action_type,
        breakdown=dimensions,
        component_id=component_id,
        environment_id=environment_id,
        lookback_days=lookback_days,
    )
    payload = [EffectivenessBucketItem(**bucket.as_dict()) for bucket in buckets]
    headline = (
        buckets[0].headline()
        if buckets
        else "No comparable historical cases were found for this scope."
    )
    return EffectivenessResponse(
        buckets=payload,
        headline=headline,
        observational_label=OBSERVATIONAL_LABEL,
        limitations=sorted({note for bucket in buckets for note in bucket.limitations}),
    )


@router.get(
    "/intelligence/remediation-effectiveness/compare",
    response_model=ActionComparisonResponse,
    summary="Compare two actions in comparable situations",
)
async def compare_remediation_actions(
    project_id: uuid.UUID = Query(...),
    action_a: str = Query(..., max_length=60),
    action_b: str = Query(..., max_length=60),
    failure_pattern: Optional[str] = Query(None, max_length=120),
    db: AsyncSession = Depends(get_db),
) -> ActionComparisonResponse:
    """§45. An observational comparison, labelled as one."""
    await require_project(db, project_id)
    payload = await compare_actions(
        db,
        project_id=project_id,
        action_a=action_a,
        action_b=action_b,
        failure_label=failure_pattern,
    )
    return ActionComparisonResponse(**payload)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@router.get(
    "/intelligence/search",
    response_model=SearchResponse,
    summary="Grounded knowledge search",
)
async def search_knowledge(
    project_id: uuid.UUID = Query(...),
    q: str = Query(..., min_length=1, max_length=500),
    incident_id: Optional[uuid.UUID] = Query(None),
    component_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> SearchResponse:
    """§46–§49. Answers from stored rows, with verified citations."""
    await require_project(db, project_id)
    if incident_id is not None:
        await require_incident(db, incident_id, project_id=project_id)
    if component_id is not None:
        await require_component(db, component_id, project_id=project_id)
    service_instance = KnowledgeSearchService()
    answer = await service_instance.search(
        db,
        project_id=project_id,
        question=q,
        incident_id=incident_id,
        component_id=component_id,
        limit=limit,
    )
    return SearchResponse(**answer.as_dict())


# ---------------------------------------------------------------------------
# Learning runs
# ---------------------------------------------------------------------------


@router.get(
    "/intelligence/learning-runs",
    response_model=LearningRunListResponse,
    summary="Learning run history",
)
async def list_learning_runs(
    project_id: Optional[uuid.UUID] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> LearningRunListResponse:
    if project_id is not None:
        await require_project(db, project_id)
    rows, total = await service.list_runs(
        db, project_id=project_id, page=page, page_size=page_size
    )
    items = [service.run_row(row) for row in rows]
    return LearningRunListResponse(**_pagination(items, total, page, page_size))


@router.get(
    "/intelligence/learning-runs/{run_id}",
    response_model=LearningRunDetailResponse,
    summary="One learning run and the events it consumed",
)
async def get_learning_run(
    run_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> LearningRunDetailResponse:
    if project_id is not None:
        await require_project(db, project_id)
    payload = await service.run_detail(db, run_id=run_id, project_id=project_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Learning run not found")
    return LearningRunDetailResponse(**payload)


@router.post(
    "/intelligence/learning-runs",
    response_model=LearningRunSummaryResponse,
    summary="Trigger a learning run",
)
async def trigger_learning_run(
    payload: LearningRunRequest,
    db: AsyncSession = Depends(get_db),
) -> LearningRunSummaryResponse:
    """§63. A manual run over one project, bounded by the same settings."""
    await require_project(db, payload.project_id)
    settings = get_settings()
    if not settings.INTELLIGENCE_LEARNING_ENABLED:
        raise HTTPException(
            status_code=409,
            detail="Learning is disabled (INTELLIGENCE_LEARNING_ENABLED=false)",
        )
    summary = await execute_learning_run(
        db,
        project_id=payload.project_id,
        trigger=payload.trigger,
        cutoff=payload.cutoff,
        lookback_days=payload.lookback_days,
        generate_recommendations=payload.generate_recommendations,
    )
    await db.commit()
    return LearningRunSummaryResponse(**summary.as_dict())


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------


@router.get(
    "/intelligence/event-hooks",
    response_model=EventHookResponse,
    summary="What the learning pipeline consumes",
)
async def get_event_hooks(
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> EventHookResponse:
    if project_id is not None:
        await require_project(db, project_id)
    enabled = await enabled_event_types(db, project_id=project_id)
    trusted = await trusted_provenance(db, project_id=project_id)
    return EventHookResponse(
        project_id=project_id,
        enabled_event_types=sorted(item.value for item in enabled),
        trusted_provenance=sorted(item.value for item in trusted),
    )


@router.put(
    "/intelligence/event-hooks",
    response_model=EventHookResponse,
    summary="Narrow what the pipeline consumes",
)
async def update_event_hooks(
    payload: EventHookUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> EventHookResponse:
    """§63, §76. A hook may only *narrow* trust: the settings remain the ceiling."""
    if payload.project_id is not None:
        await require_project(db, payload.project_id)
    if payload.enabled_event_types is not None:
        _validate_enum_list(
            [item.upper() for item in payload.enabled_event_types],
            [member.value for member in LearningEventType],
            field="enabled_event_types",
        )
    await set_event_hook(
        db,
        project_id=payload.project_id,
        enabled_event_types=payload.enabled_event_types,
        trusted_provenance_classes=payload.trusted_provenance,
        updated_by=payload.updated_by,
    )
    await db.commit()
    enabled = await enabled_event_types(db, project_id=payload.project_id)
    trusted = await trusted_provenance(db, project_id=payload.project_id)
    return EventHookResponse(
        project_id=payload.project_id,
        enabled_event_types=sorted(item.value for item in enabled),
        trusted_provenance=sorted(item.value for item in trusted),
    )


@router.post(
    "/intelligence/sweep",
    response_model=SweepResponse,
    summary="Run the scheduled learning work now",
)
async def run_sweep(
    project_id: Optional[uuid.UUID] = Query(None),
    force: bool = Query(False),
    db: AsyncSession = Depends(get_db),
) -> SweepResponse:
    if project_id is not None:
        await require_project(db, project_id)
    result = await run_learning_sweep(db, project_id=project_id, force=force)
    await db.commit()
    return SweepResponse(**result.as_dict())


@router.get(
    "/intelligence/experiments",
    response_model=LearningRunListResponse,
    summary="Learning experiments (inert by design)",
)
async def list_experiments(
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> LearningRunListResponse:
    """§69/§74. Experiments score algorithms. They cannot activate anything."""
    await require_project(db, project_id)
    rows = list(
        (
            await db.scalars(
                select(LearningExperiment)
                .where(LearningExperiment.project_id == project_id)
                .order_by(LearningExperiment.created_at.desc())
                .limit(100)
            )
        ).all()
    )
    items = [
        {
            "id": str(row.id),
            "project_id": str(row.project_id),
            "status": row.status.value,
            "trigger": row.algorithm,
            "data_cutoff": row.as_of.isoformat(),
            "last_processed_at": None,
            "started_at": row.created_at.isoformat(),
            "completed_at": None,
            "events_processed": 0,
            "experiences_created": 0,
            "experiences_updated": 0,
            "patterns_discovered": 0,
            "patterns_validated": 0,
            "patterns_rejected": 0,
            "knowledge_activated": 0,
            "records_flagged": 0,
            "algorithm_versions": {
                "algorithm": row.algorithm,
                "version": row.algorithm_version,
            },
            "error_summary": None,
        }
        for row in rows
    ]
    return LearningRunListResponse(**_pagination(items, len(items), 1, 100))


def _validate_enum(enum_cls: Any, value: Optional[str]) -> None:
    """Reject an unknown filter value with 422 instead of silently ignoring it."""
    if value is None:
        return
    allowed = {member.value for member in enum_cls}
    if value not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"{value!r} is not a valid {enum_cls.__name__} (allowed: {sorted(allowed)})",
        )


def _validate_enum_list(values: list[str], allowed: list[str], *, field: str) -> None:
    unknown = [value for value in values if value not in allowed]
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"unknown {field} value(s): {sorted(unknown)}"
        )


__all__ = ["router"]
