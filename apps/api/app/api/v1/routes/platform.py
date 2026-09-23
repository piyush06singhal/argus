"""ARGUS Unified Platform Routes (Phase 11 §68–§71, §106, §115).

The composed surface. Every endpoint here either aggregates what a phase service
already computes or exposes the control plane's own objects — none of them
duplicates domain logic, which is §115's "use composition where appropriate".

```text
GET    /platform/overview                      the §19 unified dashboard
GET    /platform/state                         the §2 system state
GET    /platform/state/components/{id}/history §5 state history
POST   /platform/state/recompute               recompute + record transitions

GET    /platform/context                       the §7 context for a scope
POST   /platform/context/snapshots             take a §8 snapshot

GET    /platform/cases                         list (§14)
GET    /platform/cases/{id}                    the §26 workspace payload
POST   /platform/cases/{id}/status             a person moves the case
POST   /platform/cases/{id}/ask                the §27 assistant (§28 guards)
GET    /platform/cases/{id}/story              the §10 event chain
POST   /platform/cases                         open one by hand

GET    /platform/activity                      the §25 feed
GET    /platform/search                        global search (§18)
GET    /platform/search/help                   the syntax

GET    /platform/services                      the §30 catalog
GET    /platform/services/{id}                 one service
PUT    /platform/services/{id}/ownership       record ownership (§31)
GET    /platform/services/{id}/blast-radius    structural dependents

GET    /platform/slo                           objectives + readings (§33)
POST   /platform/slo                           define one
POST   /platform/slo/evaluate                  evaluate now
GET    /platform/slo/{id}/error-budget         budget history (§34)

GET    /platform/changes                       the §38 change view
GET    /platform/changes/risk                  the §39 advisory view
GET    /platform/changes/failure-rate          the §84 metric
GET    /platform/environments/compare          the §41 comparison

GET    /platform/health                        ARGUS self-monitoring (§58)
GET    /platform/readiness                     readiness (§107)
GET    /platform/dependencies                  dependency report (§106)

GET    /platform/data-quality                  the §89 center
POST   /platform/data-quality/{id}/status      an operator's decision
POST   /platform/data-quality/check            run the checks now

GET    /platform/configuration                 effective config + versions
POST   /platform/configuration                 write a new version
POST   /platform/configuration/rollback        restore a version
GET    /platform/feature-flags                 §61/§62

GET    /platform/notifications                 the inbox (§54)
POST   /platform/notifications/{id}/read       mark read/ack
GET    /platform/notifications/summary

GET    /platform/reports                       generate a §77 report
GET    /platform/incidents/{id}/postmortem     the §79 postmortem (§80)
GET    /platform/improvement-plan              the §82 plan

GET    /platform/integrations                  the §52 provider registry
GET    /platform/webhooks/requirements         the §53 contract
POST   /platform/webhooks/{source}             receive one (signed)

GET    /platform/metrics                       §73–§76, §85
POST   /platform/sweep                         run the control plane now
```

Scope rules follow every previous phase: mutating requests **require** a project
and prove ownership, reads accept an optional project and enforce it when
supplied, and an out-of-scope id answers 404 rather than confirming existence
(§42).

The two surfaces that can change something are deliberately small. Configuration
writes are versioned and validated; case status changes are checked against the
one legal-transition table. **Nothing on this router executes a remediation**: the
routes for that stay in Phase 9's router, behind Phase 9's gates, because a second
path into execution is precisely the loophole the platform must not grow.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_component, require_incident, require_project
from app.core.config import get_settings
from app.core.database import get_db
from app.models.platform import (
    CaseStatus,
    CaseTrigger,
    DataQualityStatus,
    NotificationStatus,
    PlatformEventType,
    SloComparison,
    SloIndicator,
)
from app.schemas.platform import (
    ActivityResponse,
    AssistantAnswerResponse,
    AssistantQuestionRequest,
    CaseAssistantCapabilityResponse,
    CaseDetailResponse,
    CaseListResponse,
    CaseStatusChangeRequest,
    CaseSummary,
    CatalogEntryResponse,
    CatalogListResponse,
    ChangeFailureRateResponse,
    ChangeListResponse,
    ConfigurationResponse,
    ConfigurationRollbackRequest,
    ConfigurationUpdateRequest,
    ConfigurationVersionItem,
    ContextResponse,
    ContextSnapshotResponse,
    DataQualityIssueItem,
    DataQualityResponse,
    DataQualityStatusRequest,
    DependencyHealthResponse,
    EnvironmentComparisonResponse,
    ErrorBudgetResponse,
    FeatureFlagsResponse,
    ImprovementPlanResponse,
    IntegrationRegistryResponse,
    NotificationAckRequest,
    NotificationItem,
    NotificationListResponse,
    OverviewResponse,
    OwnershipRequest,
    PlatformHealthResponse,
    PostmortemResponse,
    ReadinessResponse,
    ReportResponse,
    SearchHelpResponse,
    SearchResponse,
    SloCreateRequest,
    SloEvaluationResponse,
    SloOverviewResponse,
    StateHistoryResponse,
    StateTransitionItem,
    StoryResponse,
    SystemStateResponse,
    TimelineEntryItem,
    WebhookReceiptResponse,
    WebhookRequirementsResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["platform"])
settings = get_settings()


def _request_id(request: Request) -> Optional[str]:
    """§71: the request id, when the middleware supplied one."""
    for header in ("x-request-id", "x-correlation-id"):
        value = request.headers.get(header)
        if value:
            return value
    return None


# ---------------------------------------------------------------------------
# §19–§25 — overview and activity
# ---------------------------------------------------------------------------
@router.get(
    "/platform/overview",
    response_model=OverviewResponse,
    summary="Unified reliability dashboard",
)
async def platform_overview(
    project_id: uuid.UUID = Query(...),
    environment_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> OverviewResponse:
    await require_project(db, project_id)
    from app.api.v1.deps import require_environment

    await require_environment(db, project_id, environment_id)
    from app.services.control_plane import build_overview

    overview = await build_overview(
        db, project_id=project_id, environment_id=environment_id
    )
    return OverviewResponse(**overview.as_dict())


@router.get(
    "/platform/engineering",
    response_model=dict,
    summary="Engineering-depth summary (§24)",
)
async def platform_engineering(
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_project(db, project_id)
    from app.services.control_plane import engineering_summary

    return await engineering_summary(db, project_id=project_id)


@router.get(
    "/platform/activity",
    response_model=ActivityResponse,
    summary="ARGUS activity feed (§25)",
)
async def platform_activity(
    project_id: uuid.UUID = Query(...),
    event_type: Optional[str] = Query(None, description="PlatformEventType value"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> ActivityResponse:
    await require_project(db, project_id)
    from app.services.control_plane import activity_feed

    types = None
    if event_type:
        try:
            types = [PlatformEventType(event_type)]
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"unknown event type '{event_type}'"
            ) from exc
    payload = await activity_feed(
        db, project_id=project_id, limit=limit, offset=offset, event_types=types
    )
    return ActivityResponse(**payload)


@router.get(
    "/platform/story/{correlation_id}",
    response_model=StoryResponse,
    summary="The correlated story for one situation (§10)",
)
async def platform_story(
    correlation_id: str,
    db: AsyncSession = Depends(get_db),
) -> StoryResponse:
    """The chain of events that share a correlation id.

    Read-only and unscoped on purpose: a correlation id is an opaque value the
    caller already holds, and it is returned exactly as it was stored — a reader
    cannot use this to enumerate another project, because the ids are not
    guessable and no project data is listed.
    """
    from app.services.control_plane import system_story

    return StoryResponse(**await system_story(db, correlation_id=correlation_id))


@router.get(
    "/platform/projects",
    response_model=list[dict],
    summary="Multi-project overview cards (§42)",
)
async def platform_projects(db: AsyncSession = Depends(get_db)) -> list[dict]:
    from app.services.control_plane import project_overview_cards

    return await project_overview_cards(db)


# ---------------------------------------------------------------------------
# §2–§5 — system state
# ---------------------------------------------------------------------------
@router.get(
    "/platform/state",
    response_model=SystemStateResponse,
    summary="Unified system state",
)
async def platform_state(
    project_id: uuid.UUID = Query(...),
    environment_id: Optional[uuid.UUID] = Query(None),
    include: Optional[str] = Query(
        None, description="comma-separated sections to include"
    ),
    db: AsyncSession = Depends(get_db),
) -> SystemStateResponse:
    await require_project(db, project_id)
    from app.api.v1.deps import require_environment

    await require_environment(db, project_id, environment_id)
    from app.services.system_state import build_system_state

    sections = [item.strip() for item in include.split(",")] if include else None
    state = await build_system_state(
        db,
        project_id=project_id,
        environment_id=environment_id,
        include=sections,
    )
    return SystemStateResponse(**state.as_dict())


@router.post(
    "/platform/state/recompute",
    response_model=dict,
    summary="Recompute state and record transitions",
)
async def platform_state_recompute(
    project_id: uuid.UUID = Query(...),
    environment_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Recompute component states and append transition rows.

    Mutating by design — it is the endpoint that writes the §5 history — so it
    requires a project and returns only what changed.
    """
    await require_project(db, project_id)
    from app.services.system_state import (
        build_system_state,
        derive_component_states,
        record_transitions,
    )

    state = await build_system_state(
        db,
        project_id=project_id,
        environment_id=environment_id,
        include=("components",),
    )
    component_ids = [uuid.UUID(component["id"]) for component in state.components]
    results = await derive_component_states(
        db, project_id=project_id, component_ids=component_ids
    )
    transitions = await record_transitions(
        db,
        project_id=project_id,
        results=results,
        environment_by_component={
            uuid.UUID(component["id"]): (
                uuid.UUID(component["environment_id"])
                if component.get("environment_id")
                else None
            )
            for component in state.components
        },
    )
    await db.commit()
    return {
        "components_evaluated": len(results),
        "transitions_recorded": len(transitions),
        "health": state.health,
        "limitations": state.limitations,
    }


@router.get(
    "/platform/state/components/{component_id}/history",
    response_model=StateHistoryResponse,
    summary="Component state history (§5)",
)
async def platform_component_state_history(
    component_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> StateHistoryResponse:
    await require_project(db, project_id)
    await require_component(db, component_id, project_id=project_id)
    from app.services.system_state import state_history

    since = datetime.now(timezone.utc) - __import__("datetime").timedelta(days=days)
    transitions = await state_history(db, component_id=component_id, since=since)
    return StateHistoryResponse(
        component_id=component_id,
        transitions=[
            StateTransitionItem(
                id=row.id,
                component_id=row.component_id,
                previous_state=(
                    row.previous_state.value if row.previous_state else None
                ),
                new_state=row.new_state.value,
                trigger=row.trigger.value,
                reason=row.reason,
                evidence=row.evidence,
                source=row.source,
                occurred_at=row.occurred_at,
            )
            for row in transitions
        ],
        as_of_state=transitions[-1].new_state.value if transitions else None,
    )


# ---------------------------------------------------------------------------
# §7, §8 — context
# ---------------------------------------------------------------------------
@router.get(
    "/platform/context",
    response_model=ContextResponse,
    summary="Reliability context for a scope (§7)",
)
async def platform_context(
    project_id: uuid.UUID = Query(...),
    component_id: Optional[uuid.UUID] = Query(None),
    incident_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> ContextResponse:
    await require_project(db, project_id)
    if component_id is not None:
        await require_component(db, component_id, project_id=project_id)
    if incident_id is not None:
        await require_incident(db, incident_id, project_id=project_id)
    from app.services.reliability_context import build_context

    context = await build_context(
        db, project_id=project_id, component_id=component_id, incident_id=incident_id
    )
    return ContextResponse(
        references=context.references(),
        description=context.describe(),
        attributes=context.attributes,
        unavailable=context.unavailable,
        fingerprint=context.fingerprint(),
    )


@router.post(
    "/platform/context/snapshots",
    response_model=ContextSnapshotResponse,
    summary="Take a context snapshot (§8)",
)
async def platform_snapshot(
    project_id: uuid.UUID = Query(...),
    incident_id: Optional[uuid.UUID] = Query(None),
    component_id: Optional[uuid.UUID] = Query(None),
    case_id: Optional[uuid.UUID] = Query(None),
    include_state: bool = Query(True),
    actor: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> ContextSnapshotResponse:
    await require_project(db, project_id)
    from app.services.reliability_context import build_context, snapshot_context

    context = await build_context(
        db,
        project_id=project_id,
        incident_id=incident_id,
        component_id=component_id,
        case_id=case_id,
    )
    row = await snapshot_context(
        db,
        context=context,
        scope="CASE" if case_id else ("COMPONENT" if component_id else "PROJECT"),
        created_by=actor,
        include_system_state=include_state,
    )
    await db.commit()
    return ContextSnapshotResponse(
        id=row.id,
        project_id=row.project_id,
        scope=row.scope,
        fingerprint=row.fingerprint,
        as_of=row.as_of,
        snapshot=row.snapshot,
        created_by=row.created_by,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# §14–§16, §26, §27 — cases
# ---------------------------------------------------------------------------
@router.get(
    "/platform/cases",
    response_model=CaseListResponse,
    summary="List reliability cases",
)
async def platform_cases(
    project_id: uuid.UUID = Query(...),
    status: Optional[str] = Query(None, description="CaseStatus value"),
    environment_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> CaseListResponse:
    await require_project(db, project_id)
    from app.services.reliability_case import case_summary, list_cases

    statuses = None
    if status:
        try:
            statuses = [CaseStatus(status)]
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"unknown case status '{status}'"
            ) from exc
    cases = await list_cases(
        db,
        project_id=project_id,
        statuses=statuses,
        environment_id=environment_id,
        limit=limit,
        offset=offset,
    )
    summaries = [await case_summary(db, case=case) for case in cases]
    return CaseListResponse(
        cases=[CaseSummary(**summary) for summary in summaries],
        total=len(summaries),
        limit=limit,
        offset=offset,
    )


@router.post(
    "/platform/cases",
    response_model=CaseSummary,
    summary="Open a reliability case by hand",
)
async def platform_open_case(
    project_id: uuid.UUID = Query(...),
    title: str = Query(..., min_length=1, max_length=500),
    trigger: str = Query("OPERATOR"),
    component_id: Optional[uuid.UUID] = Query(None),
    environment_id: Optional[uuid.UUID] = Query(None),
    summary: Optional[str] = Query(None),
    actor: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> CaseSummary:
    await require_project(db, project_id)
    if component_id is not None:
        await require_component(db, component_id, project_id=project_id)
    from app.services.reliability_case import case_summary, open_case

    try:
        case_trigger = CaseTrigger(trigger)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"unknown case trigger '{trigger}'"
        ) from exc
    case = await open_case(
        db,
        project_id=project_id,
        trigger=case_trigger,
        title=title,
        environment_id=environment_id,
        summary=summary,
        primary_component_id=component_id,
        component_ids=[component_id] if component_id else [],
        opened_by=actor,
    )
    await db.commit()
    return CaseSummary(**await case_summary(db, case=case))


@router.get(
    "/platform/cases/{case_id}",
    response_model=CaseDetailResponse,
    summary="The case workspace payload (§26)",
)
async def platform_case_detail(
    case_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    include_evidence: bool = Query(True),
    db: AsyncSession = Depends(get_db),
) -> CaseDetailResponse:
    await require_project(db, project_id)
    from app.services.reliability_case import (
        case_summary,
        case_timeline,
        collect_case_evidence,
        get_case,
    )
    from app.services.system_state import build_system_state

    case = await get_case(db, case_id=case_id, project_id=project_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    from app.services.reliability_workflow import workflow_history

    entries = await case_timeline(db, case_id=case.id)
    evidence = (
        (await collect_case_evidence(db, case=case)).as_dict()
        if include_evidence
        else {}
    )
    workflows = await workflow_history(db, case_id=case.id)
    state = await build_system_state(
        db,
        project_id=project_id,
        environment_id=case.environment_id,
        include=("health", "active_incidents", "predicted_risks"),
    )
    notes = [
        "the incident subsystem remains authoritative about its own incident; "
        "this case references it",
        "evidence is linked by reference, not copied",
    ]
    return CaseDetailResponse(
        case=CaseSummary(**await case_summary(db, case=case)),
        timeline=[
            TimelineEntryItem(
                sequence=entry.sequence,
                occurred_at=entry.occurred_at,
                kind=entry.kind.value,
                event_type=entry.event_type,
                title=entry.title,
                detail=entry.detail,
                component_id=entry.component_id,
                source=entry.source,
                evidence=entry.evidence,
                actor=entry.actor,
                system_action=entry.system_action,
                result=entry.result,
            )
            for entry in entries
        ],
        evidence=evidence,
        workflows=[
            {
                "id": str(workflow.id),
                "stage": workflow.stage.value,
                "status": workflow.status.value,
                "completed_stages": workflow.completed_stages or [],
                "attempt": workflow.attempt,
                "stop_reason": workflow.stop_reason.value
                if workflow.stop_reason
                else None,
                "stop_detail": workflow.stop_detail,
                "deadline_at": workflow.deadline_at,
                "started_at": workflow.started_at,
                "completed_at": workflow.completed_at,
            }
            for workflow in workflows
        ],
        state={
            "health": state.health,
            "state_counts": state.state_counts(),
            "active_incidents": state.active_incidents,
        },
        notes=notes,
    )


@router.post(
    "/platform/cases/{case_id}/status",
    response_model=CaseSummary,
    summary="Move a case (a person's decision)",
)
async def platform_case_status(
    case_id: uuid.UUID,
    request: CaseStatusChangeRequest,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> CaseSummary:
    """Apply a legal case transition, or refuse with the reason (§14).

    Not an execution path: a case moving to ``AUTHORIZED`` records that Phase 9
    authorized an action; it does not authorize anything.
    """
    await require_project(db, project_id)
    from app.services.reliability_case import (
        CASE_STATUS_TRANSITIONS,
        CaseStateError,
        case_summary,
        get_case,
        transition_case,
    )

    case = await get_case(db, case_id=case_id, project_id=project_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    try:
        target = CaseStatus(request.status)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"unknown case status '{request.status}'"
        ) from exc
    try:
        await transition_case(
            db,
            case=case,
            target=target,
            actor=request.actor,
            reason=request.reason,
        )
    except CaseStateError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "illegal_transition",
                "message": str(exc),
                "allowed": [
                    status.value
                    for status in CASE_STATUS_TRANSITIONS.get(case.status, ())
                ],
            },
        ) from exc
    await db.commit()
    return CaseSummary(**await case_summary(db, case=case))


@router.get(
    "/platform/case-assistant",
    response_model=CaseAssistantCapabilityResponse,
    summary="What the case assistant does and refuses (§28)",
)
async def platform_case_assistant_capability() -> CaseAssistantCapabilityResponse:
    """The §28 capability sheet, served whether or not the assistant is on.

    A capability an operator cannot introspect is a capability they have to trust.
    This returns the guarantees and the refusals verbatim, with the flag that says
    whether answers are available on this deployment at all.
    """
    from app.services.case_assistant import assistant_capability

    return CaseAssistantCapabilityResponse(**assistant_capability())


@router.post(
    "/platform/cases/{case_id}/ask",
    response_model=AssistantAnswerResponse,
    summary="Ask about a case (§27, §28)",
)
async def platform_case_ask(
    case_id: uuid.UUID,
    request: AssistantQuestionRequest,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> AssistantAnswerResponse:
    await require_project(db, project_id)
    if not settings.PLATFORM_CASE_ASSISTANT_ENABLED:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "assistant_disabled",
                "message": (
                    "the case assistant is switched off on this deployment; the "
                    "case workspace and every deterministic capability work "
                    "without it"
                ),
            },
        )
    from app.services.case_assistant import ask

    answer = await ask(
        db,
        case_id=case_id,
        question=request.question,
        project_id=project_id,
        include_evidence=request.include_evidence,
    )
    if answer is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return AssistantAnswerResponse(
        **answer.as_dict(include_pack=request.include_evidence)
    )


@router.get(
    "/platform/cases/{case_id}/story",
    response_model=StoryResponse,
    summary="The event chain for a case's situation (§10)",
)
async def platform_case_story(
    case_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> StoryResponse:
    await require_project(db, project_id)
    from app.services.platform_events import correlation_id_for
    from app.services.control_plane import system_story
    from app.services.reliability_case import get_case

    case = await get_case(db, case_id=case_id, project_id=project_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    correlation_id = correlation_id_for(kind="case", subject_id=case.id)
    story = await system_story(db, correlation_id=correlation_id)
    if not story["events"] and case.incident_id:
        story = await system_story(
            db,
            correlation_id=correlation_id_for(
                kind="incident", subject_id=case.incident_id
            ),
        )
    return StoryResponse(**story)


# ---------------------------------------------------------------------------
# §18 — search
# ---------------------------------------------------------------------------
@router.get("/platform/search", response_model=SearchResponse, summary="Global search")
async def platform_search(
    project_id: uuid.UUID = Query(...),
    q: str = Query(..., min_length=1, max_length=500),
    kind: Optional[str] = Query(None, description="comma-separated categories"),
    environment_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(10, ge=1, le=25),
    db: AsyncSession = Depends(get_db),
) -> SearchResponse:
    await require_project(db, project_id)
    from app.services.global_search import search
    from app.services.integrations import WebhookError, limiter

    try:
        limiter("search").check(str(project_id))
    except WebhookError as exc:
        raise HTTPException(status_code=429, detail=exc.message) from exc
    kinds = [item.strip() for item in kind.split(",")] if kind else None
    results = await search(
        db,
        raw_query=q,
        project_id=project_id,
        limit_per_kind=limit,
        kinds=kinds,
        environment_id=environment_id,
    )
    return SearchResponse(**results.as_dict())


@router.get(
    "/platform/search/help",
    response_model=SearchHelpResponse,
    summary="Search syntax",
)
async def platform_search_help() -> SearchHelpResponse:
    from app.services.global_search import search_help

    return SearchHelpResponse(**search_help())


# ---------------------------------------------------------------------------
# §30, §31 — the service catalog
# ---------------------------------------------------------------------------
@router.get(
    "/platform/services",
    response_model=CatalogListResponse,
    summary="Service catalog",
)
async def platform_services(
    project_id: uuid.UUID = Query(...),
    environment_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> CatalogListResponse:
    await require_project(db, project_id)
    from app.services.service_catalog import list_catalog

    entries = await list_catalog(
        db, project_id=project_id, environment_id=environment_id
    )
    return CatalogListResponse(
        services=[CatalogEntryResponse(**entry.as_dict()) for entry in entries],
        total=len(entries),
    )


@router.get(
    "/platform/services/{component_id}",
    response_model=CatalogEntryResponse,
    summary="One service's catalog record",
)
async def platform_service(
    component_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    window_days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> CatalogEntryResponse:
    await require_project(db, project_id)
    await require_component(db, component_id, project_id=project_id)
    from app.services.service_catalog import catalog_entry

    entry = await catalog_entry(
        db, component_id=component_id, project_id=project_id, window_days=window_days
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="Component not found")
    return CatalogEntryResponse(**entry.as_dict())


@router.put(
    "/platform/services/{component_id}/ownership",
    response_model=dict,
    summary="Record ownership (§31)",
)
async def platform_set_ownership(
    component_id: uuid.UUID,
    request: OwnershipRequest,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_project(db, project_id)
    await require_component(db, component_id, project_id=project_id)
    from app.services.service_catalog import set_ownership

    owner = await set_ownership(
        db,
        component_id=component_id,
        team=request.team,
        owner_name=request.owner_name,
        contact_email=request.contact_email,
        repository_owner=request.repository_owner,
        on_call=request.on_call,
        documentation_url=request.documentation_url,
        actor=request.actor,
    )
    await db.commit()
    return {
        "component_id": str(component_id),
        "team": owner.team,
        "owner": owner.owner_name,
        "on_call": owner.on_call,
        "note": "ownership is recorded, never inferred from repository or commit data",
    }


@router.get(
    "/platform/services/{component_id}/blast-radius",
    response_model=dict,
    summary="Structural dependents of a service",
)
async def platform_blast_radius(
    component_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    max_hops: int = Query(3, ge=1, le=5),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_project(db, project_id)
    await require_component(db, component_id, project_id=project_id)
    from app.services.service_catalog import dependency_blast_radius

    return await dependency_blast_radius(
        db, project_id=project_id, component_id=component_id, max_hops=max_hops
    )


# ---------------------------------------------------------------------------
# §32–§35 — SLOs and error budgets
# ---------------------------------------------------------------------------
@router.get("/platform/slo", response_model=SloOverviewResponse, summary="Objectives")
async def platform_slo(
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> SloOverviewResponse:
    await require_project(db, project_id)
    from app.services.slo_service import slo_overview

    return SloOverviewResponse(**await slo_overview(db, project_id=project_id))


@router.post(
    "/platform/slo",
    response_model=dict,
    summary="Define an objective",
)
async def platform_create_slo(
    request: SloCreateRequest,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_project(db, project_id)
    if request.component_id is not None:
        await require_component(db, request.component_id, project_id=project_id)
    from app.services.platform_config import ConfigurationError
    from app.services.slo_service import create_slo

    try:
        indicator = SloIndicator(request.indicator)
        comparison = SloComparison(request.comparison)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown indicator or comparison; indicators are "
                f"{[item.value for item in SloIndicator]}"
            ),
        ) from exc
    try:
        slo = await create_slo(
            db,
            project_id=project_id,
            name=request.name,
            indicator=indicator,
            target=request.target,
            comparison=comparison,
            metric_name=request.metric_name,
            component_id=request.component_id,
            environment_id=request.environment_id,
            window_seconds=request.window_seconds,
            unit=request.unit,
            description=request.description,
            actor=request.actor,
        )
    except ConfigurationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_objective", "message": str(exc)},
        ) from exc
    await db.commit()
    return {"slo_id": str(slo.id), "name": slo.name, "versioned": True}


@router.post(
    "/platform/slo/evaluate",
    response_model=dict,
    summary="Evaluate every objective now",
)
async def platform_evaluate_slo(
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_project(db, project_id)
    from app.services.slo_service import evaluate_project

    summary = await evaluate_project(db, project_id=project_id)
    await db.commit()
    return summary


@router.get(
    "/platform/slo/{slo_id}",
    response_model=SloEvaluationResponse,
    summary="Evaluate one objective",
)
async def platform_slo_detail(
    slo_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> SloEvaluationResponse:
    await require_project(db, project_id)
    from app.models.platform import ServiceLevelObjective
    from app.services.slo_service import evaluate_slo

    slo = await db.get(ServiceLevelObjective, slo_id)
    if slo is None or slo.project_id != project_id:
        raise HTTPException(status_code=404, detail="Objective not found")
    evaluation = await evaluate_slo(db, slo=slo)
    return SloEvaluationResponse(**evaluation.as_dict())


@router.get(
    "/platform/slo/{slo_id}/error-budget",
    response_model=ErrorBudgetResponse,
    summary="Error budget history (§34)",
)
async def platform_error_budget(
    slo_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> ErrorBudgetResponse:
    await require_project(db, project_id)
    from app.models.platform import ServiceLevelObjective
    from app.services.slo_service import budget_history, evaluate_and_record

    slo = await db.get(ServiceLevelObjective, slo_id)
    if slo is None or slo.project_id != project_id:
        raise HTTPException(status_code=404, detail="Objective not found")
    rows = await budget_history(db, slo_id=slo_id, limit=limit)
    if not rows:
        evaluation, snapshot = await evaluate_and_record(db, slo=slo)
        await db.commit()
        rows = [snapshot]
    return ErrorBudgetResponse(
        slo_id=str(slo_id),
        name=slo.name,
        latest={
            "status": rows[0].status.value,
            "window_start": rows[0].window_start,
            "window_end": rows[0].window_end,
            "allowed_failure": rows[0].allowed_failure,
            "observed_failure": rows[0].observed_failure,
            "remaining": rows[0].remaining,
            "remaining_percent": rows[0].remaining_percent,
            "burn_rate": rows[0].burn_rate,
            "burn_state": rows[0].burn_state.value,
            "sample_count": rows[0].sample_count,
            "computed_at": rows[0].computed_at,
        },
        history=[
            {
                "computed_at": row.computed_at,
                "status": row.status.value,
                "burn_rate": row.burn_rate,
                "burn_state": row.burn_state.value,
                "remaining_percent": row.remaining_percent,
                "sample_count": row.sample_count,
            }
            for row in rows
        ],
        definition=(
            "allowed_failure is the fraction of the window the objective permits; "
            "observed_failure is the share of stored samples outside the target; "
            "burn_rate = observed / allowed, where 1.0 consumes the window's budget "
            "exactly. Thresholds are configuration."
        ),
    )


# ---------------------------------------------------------------------------
# §38–§41, §84 — change intelligence
# ---------------------------------------------------------------------------
@router.get(
    "/platform/changes",
    response_model=ChangeListResponse,
    summary="Unified change view (§38)",
)
async def platform_changes(
    project_id: uuid.UUID = Query(...),
    environment_id: Optional[uuid.UUID] = Query(None),
    days: int = Query(7, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> ChangeListResponse:
    await require_project(db, project_id)
    from app.services.change_intelligence import recent_changes

    since = datetime.now(timezone.utc) - __import__("datetime").timedelta(days=days)
    changes = await recent_changes(
        db,
        project_id=project_id,
        environment_id=environment_id,
        since=since,
        limit=limit,
    )
    return ChangeListResponse(changes=changes)


@router.get(
    "/platform/changes/risk",
    response_model=dict,
    summary="Deployment risk view — advisory (§39)",
)
async def platform_change_risk(
    project_id: uuid.UUID = Query(...),
    deployment_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_project(db, project_id)
    from app.models.deployment import DeploymentEvent
    from app.services.change_intelligence import change_risk_view

    deployment = await db.get(DeploymentEvent, deployment_id)
    if deployment is None or deployment.project_id != project_id:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return await change_risk_view(db, project_id=project_id, deployment=deployment)


@router.get(
    "/platform/changes/failure-rate",
    response_model=ChangeFailureRateResponse,
    summary="Change failure rate (§84)",
)
async def platform_change_failure_rate(
    project_id: uuid.UUID = Query(...),
    days: Optional[int] = Query(None, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> ChangeFailureRateResponse:
    await require_project(db, project_id)
    from app.services.change_intelligence import change_failure_rate

    result = await change_failure_rate(db, project_id=project_id, window_days=days)
    return ChangeFailureRateResponse(**result.as_dict())


@router.get(
    "/platform/environments/compare",
    response_model=EnvironmentComparisonResponse,
    summary="Environment comparison (§41)",
)
async def platform_compare_environments(
    project_id: uuid.UUID = Query(...),
    left: uuid.UUID = Query(...),
    right: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> EnvironmentComparisonResponse:
    await require_project(db, project_id)
    from app.services.change_intelligence import compare_environments

    try:
        payload = await compare_environments(
            db,
            project_id=project_id,
            left_environment_id=left,
            right_environment_id=right,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return EnvironmentComparisonResponse(**payload)


# ---------------------------------------------------------------------------
# §57–§60, §105–§107 — platform health
# ---------------------------------------------------------------------------
@router.get(
    "/platform/health",
    response_model=PlatformHealthResponse,
    summary="ARGUS self-monitoring (§58)",
)
async def platform_health_endpoint(
    db: AsyncSession = Depends(get_db),
) -> PlatformHealthResponse:
    from app.services.platform_health import platform_health

    report = await platform_health(db)
    return PlatformHealthResponse(**report.as_dict())


@router.get(
    "/platform/readiness",
    response_model=ReadinessResponse,
    summary="Readiness (§107)",
)
async def platform_readiness(db: AsyncSession = Depends(get_db)) -> ReadinessResponse:
    from app.services.platform_health import readiness

    return ReadinessResponse(**await readiness(db))


@router.get(
    "/platform/dependencies",
    response_model=DependencyHealthResponse,
    summary="Dependency report (§106)",
)
async def platform_dependencies(
    db: AsyncSession = Depends(get_db),
) -> DependencyHealthResponse:
    from app.services.platform_health import dependencies

    return DependencyHealthResponse(**await dependencies(db))


@router.get("/platform/live", summary="Liveness")
async def platform_live() -> dict:
    """Liveness: the process is up.

    Deliberately does not touch the database. A liveness probe that fails when the
    database is slow would restart a healthy process, which is the classic way a
    database blip becomes an outage.
    """
    return {"status": "alive", "as_of": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# §87–§90 — data quality
# ---------------------------------------------------------------------------
@router.get(
    "/platform/data-quality",
    response_model=DataQualityResponse,
    summary="Data quality center (§89)",
)
async def platform_data_quality(
    project_id: uuid.UUID = Query(...),
    status: Optional[str] = Query(None, description="DataQualityStatus value"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> DataQualityResponse:
    await require_project(db, project_id)
    from app.services.data_quality_center import (
        ISSUE_DESCRIPTIONS,
        list_issues,
        quality_summary,
    )

    statuses = None
    if status:
        try:
            statuses = [DataQualityStatus(status)]
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"unknown issue status '{status}'"
            ) from exc
    issues = await list_issues(
        db, project_id=project_id, statuses=statuses, limit=limit
    )
    return DataQualityResponse(
        summary=await quality_summary(db, project_id=project_id),
        issues=[
            DataQualityIssueItem(
                id=issue.id,
                kind=issue.kind.value,
                severity=issue.severity.value,
                status=issue.status.value,
                subject_type=issue.subject_type,
                subject_id=issue.subject_id,
                component_id=issue.component_id,
                title=issue.title,
                detail=issue.detail,
                evidence=issue.evidence,
                suggestion=issue.suggestion,
                detected_at=issue.detected_at,
                last_seen_at=issue.last_seen_at,
                occurrence_count=issue.occurrence_count,
            )
            for issue in issues
        ],
        descriptions={kind.value: text for kind, text in ISSUE_DESCRIPTIONS.items()},
    )


@router.post(
    "/platform/data-quality/check",
    response_model=dict,
    summary="Run the consistency checks now",
)
async def platform_data_quality_check(
    project_id: uuid.UUID = Query(...),
    persist: bool = Query(True),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_project(db, project_id)
    from app.services.data_quality_center import run_consistency_checks

    result = await run_consistency_checks(db, project_id=project_id, persist=persist)
    if persist:
        await db.commit()
    return result.as_dict()


@router.post(
    "/platform/data-quality/{issue_id}/status",
    response_model=dict,
    summary="Record a decision about an issue",
)
async def platform_data_quality_status(
    issue_id: uuid.UUID,
    request: DataQualityStatusRequest,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_project(db, project_id)
    from app.models.platform import DataQualityIssue
    from app.services.data_quality_center import set_issue_status

    issue = await db.get(DataQualityIssue, issue_id)
    if issue is None or issue.project_id != project_id:
        raise HTTPException(status_code=404, detail="Issue not found")
    try:
        status = DataQualityStatus(request.status)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"unknown issue status '{request.status}'"
        ) from exc
    await set_issue_status(db, issue=issue, status=status, actor=request.actor)
    await db.commit()
    return {
        "issue_id": str(issue_id),
        "status": issue.status.value,
        "resolved_by": issue.resolved_by,
        "note": (
            "ARGUS never repairs historical evidence automatically; a decision "
            "about history is a person's"
        ),
    }


# ---------------------------------------------------------------------------
# §44, §91–§94 — configuration
# ---------------------------------------------------------------------------
@router.get(
    "/platform/configuration",
    response_model=ConfigurationResponse,
    summary="Effective configuration and versions",
)
async def platform_configuration(
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> ConfigurationResponse:
    await require_project(db, project_id)
    from app.services.platform_config import (
        configuration_history,
        effective_configuration,
    )

    effective = await effective_configuration(db, project_id=project_id)
    versions = await configuration_history(db, project_id=project_id, limit=25)
    return ConfigurationResponse(
        project_id=str(project_id),
        sections=effective.sections,
        overrides=effective.overrides,
        redacted_fields=effective.redacted_fields,
        notes=effective.notes,
        versions=[
            ConfigurationVersionItem(
                id=row.id,
                scope=row.scope.value,
                scope_id=row.scope_id,
                version=row.version,
                settings=row.settings,
                redacted_fields=row.redacted_fields or [],
                previous_version=row.previous_version,
                change_summary=row.change_summary,
                changed_by=row.changed_by,
                reason=row.reason,
                rolled_back_from=row.rolled_back_from,
                created_at=row.created_at,
            )
            for row in versions
        ],
    )


@router.post(
    "/platform/configuration",
    response_model=dict,
    summary="Write a configuration version",
)
async def platform_write_configuration(
    request: ConfigurationUpdateRequest,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_project(db, project_id)
    from app.services.platform_config import (
        ConfigurationError,
        ConfigurationScope,
        record_configuration,
        validate_configuration,
    )

    try:
        scope = ConfigurationScope(request.scope)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"unknown configuration scope '{request.scope}'"
        ) from exc
    try:
        validate_configuration(scope=scope.value, settings=request.settings)
    except ConfigurationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_configuration", "message": str(exc)},
        ) from exc
    version = await record_configuration(
        db,
        project_id=project_id,
        scope=scope,
        scope_id=request.scope_id,
        settings=request.settings,
        change_summary=request.change_summary,
        changed_by=request.actor,
        reason=request.reason,
    )
    await db.commit()
    return {
        "scope": scope.value,
        "version": version.version,
        "previous_version": version.previous_version,
        "redacted_fields": version.redacted_fields or [],
        "note": "configuration is append-only: this wrote a new version",
    }


@router.post(
    "/platform/configuration/rollback",
    response_model=dict,
    summary="Restore a configuration version (§94)",
)
async def platform_rollback_configuration(
    request: ConfigurationRollbackRequest,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_project(db, project_id)
    from app.services.platform_config import (
        ConfigurationError,
        ConfigurationScope,
        rollback_configuration,
    )

    try:
        scope = ConfigurationScope(request.scope)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"unknown configuration scope '{request.scope}'"
        ) from exc
    try:
        version = await rollback_configuration(
            db,
            project_id=project_id,
            scope=scope,
            target_version=request.target_version,
            scope_id=request.scope_id,
            actor=request.actor,
            reason=request.reason,
        )
    except ConfigurationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await db.commit()
    return {
        "scope": scope.value,
        "restored_from": version.rolled_back_from,
        "new_version": version.version,
    }


@router.get(
    "/platform/feature-flags",
    response_model=FeatureFlagsResponse,
    summary="Feature flags (§61, §62)",
)
async def platform_feature_flags(
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> FeatureFlagsResponse:
    await require_project(db, project_id)
    from app.services.platform_config import feature_flags

    return FeatureFlagsResponse(**await feature_flags(db, project_id=project_id))


# ---------------------------------------------------------------------------
# §53–§56 — notifications
# ---------------------------------------------------------------------------
@router.get(
    "/platform/notifications",
    response_model=NotificationListResponse,
    summary="Notification inbox (§54)",
)
async def platform_notifications(
    project_id: uuid.UUID = Query(...),
    status: Optional[str] = Query(None, description="NotificationStatus value"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> NotificationListResponse:
    await require_project(db, project_id)
    from app.services.platform_notifications import (
        list_notifications,
        notification_summary,
    )

    statuses = None
    if status:
        try:
            statuses = [NotificationStatus(status)]
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"unknown notification status '{status}'"
            ) from exc
    rows = await list_notifications(
        db, project_id=project_id, statuses=statuses, limit=limit
    )
    return NotificationListResponse(
        notifications=[
            NotificationItem(
                id=row.id,
                kind=row.kind.value,
                severity=row.severity.value,
                status=row.status.value,
                title=row.title,
                body=row.body,
                source=row.source,
                subject_type=row.subject_type,
                subject_id=row.subject_id,
                case_id=row.case_id,
                link=row.link,
                evidence=row.evidence,
                occurrence_count=row.occurrence_count,
                channels_attempted=row.channels_attempted,
                delivery=row.delivery,
                delivered_at=row.delivered_at,
                read_at=row.read_at,
                acknowledged_by=row.acknowledged_by,
                created_at=row.created_at,
            )
            for row in rows
        ],
        summary=await notification_summary(db, project_id=project_id),
    )


@router.post(
    "/platform/notifications/{notification_id}/read",
    response_model=dict,
    summary="Mark a notification read or acknowledged",
)
async def platform_notification_read(
    notification_id: uuid.UUID,
    request: NotificationAckRequest,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_project(db, project_id)
    from app.models.platform import PlatformNotification
    from app.services.platform_notifications import mark_read

    row = await db.get(PlatformNotification, notification_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=404, detail="Notification not found")
    await mark_read(
        db, notification=row, actor=request.actor, acknowledge=request.acknowledge
    )
    await db.commit()
    return {"id": str(notification_id), "status": row.status.value}


# ---------------------------------------------------------------------------
# §37, §77–§85 — reports
# ---------------------------------------------------------------------------
@router.get(
    "/platform/reports",
    response_model=ReportResponse,
    summary="Generate a reliability report (§77)",
)
async def platform_reports(
    project_id: uuid.UUID = Query(...),
    kind: str = Query("weekly", description="daily | weekly | monthly"),
    days: Optional[int] = Query(None, ge=1, le=365),
    format: str = Query("json", description="json | csv | markdown"),
    db: AsyncSession = Depends(get_db),
):
    await require_project(db, project_id)
    from app.services.platform_reports import (
        build_report,
        render_report_csv,
        render_report_markdown,
    )

    report = await build_report(db, project_id=project_id, kind=kind, window_days=days)
    if format == "csv":
        return PlainTextResponse(render_report_csv(report), media_type="text/csv")
    if format in ("markdown", "md", "text"):
        return PlainTextResponse(
            render_report_markdown(report), media_type="text/markdown"
        )
    return ReportResponse(**report.as_dict())


@router.get(
    "/platform/incidents/{incident_id}/postmortem",
    response_model=PostmortemResponse,
    summary="Structured postmortem (§79, §80)",
)
async def platform_postmortem(
    incident_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    draft_narrative: bool = Query(False, description="draft narrative text"),
    db: AsyncSession = Depends(get_db),
) -> PostmortemResponse:
    await require_project(db, project_id)
    await require_incident(db, incident_id, project_id=project_id)
    from app.services.platform_reports import build_postmortem

    postmortem = await build_postmortem(
        db,
        incident_id=incident_id,
        project_id=project_id,
        draft_narrative=draft_narrative,
    )
    if postmortem is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return PostmortemResponse(**postmortem.as_dict())


@router.get(
    "/platform/improvement-plan",
    response_model=ImprovementPlanResponse,
    summary="Reliability improvement plan (§82, §83)",
)
async def platform_improvement_plan(
    project_id: uuid.UUID = Query(...),
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> ImprovementPlanResponse:
    await require_project(db, project_id)
    from app.services.platform_reports import build_improvement_plan

    return ImprovementPlanResponse(
        **await build_improvement_plan(db, project_id=project_id, window_days=days)
    )


@router.get(
    "/platform/metrics",
    response_model=dict,
    summary="Engineering reliability metrics (§73–§76, §85)",
)
async def platform_metrics(
    project_id: uuid.UUID = Query(...),
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_project(db, project_id)
    from app.services.change_intelligence import change_failure_rate
    from app.services.platform_metrics import (
        mean_time_to_detect,
        time_to_recover_breakdown,
    )
    from app.services.slo_service import slo_overview

    return {
        "time_to_recover": await time_to_recover_breakdown(
            db, project_id=project_id, window_days=days
        ),
        "mttd": await mean_time_to_detect(db, project_id=project_id, window_days=days),
        "change_failure_rate": (
            await change_failure_rate(db, project_id=project_id, window_days=days)
        ).as_dict(),
        "slo": await slo_overview(db, project_id=project_id),
        "definitions": (
            "every metric carries its own methodology and sample size. A stage "
            "without stored evidence is reported as null, never as zero."
        ),
    }


# ---------------------------------------------------------------------------
# §51–§53 — integrations and webhooks
# ---------------------------------------------------------------------------
@router.get(
    "/platform/integrations",
    response_model=IntegrationRegistryResponse,
    summary="Provider registry (§52)",
)
async def platform_integrations() -> IntegrationRegistryResponse:
    from app.services.integrations import provider_registry

    return IntegrationRegistryResponse(**provider_registry())


@router.get(
    "/platform/webhooks/requirements",
    response_model=WebhookRequirementsResponse,
    summary="The webhook contract (§53)",
)
async def platform_webhook_requirements() -> WebhookRequirementsResponse:
    from app.services.integrations import webhook_requirements

    return WebhookRequirementsResponse(**webhook_requirements())


@router.post(
    "/platform/webhooks/{source}",
    response_model=WebhookReceiptResponse,
    summary="Receive a signed webhook",
)
async def platform_webhook(
    source: str,
    request: Request,
    project_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> WebhookReceiptResponse:
    """Accept a signed delivery, or reject it with its reason (§53).

    The body is read **raw** and never parsed before verification: validating
    parsed JSON against a signature of the re-serialized object is a classic
    bypass. Rejections are 4xx with a machine-readable code, and the reason is
    recorded — a rejected delivery is evidence too.
    """
    await require_project(db, project_id)
    from app.services.integrations import WebhookError, handle_webhook

    body = await request.body()
    headers = {key: value for key, value in request.headers.items()}
    try:
        outcome = await handle_webhook(
            db, project_id=project_id, body=body, headers=headers, source=source
        )
    except WebhookError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message, "source": source},
        ) from exc
    await db.commit()
    return WebhookReceiptResponse(**outcome.as_dict())


# ---------------------------------------------------------------------------
# §10, §12 — the control plane's own clock
# ---------------------------------------------------------------------------
@router.post(
    "/platform/sweep", response_model=dict, summary="Run the control plane now"
)
async def platform_sweep(db: AsyncSession = Depends(get_db)) -> dict:
    """Run one control-plane pass: correlate, route, advance, recompute, check.

    Unscoped on purpose — it is the platform's own scheduled work, and running it
    by hand is the operator's way of not waiting for the interval.
    """
    from app.services.platform_sweep import run_platform_sweep

    result = await run_platform_sweep(db)
    await db.commit()
    return result.as_dict()


__all__ = ["router"]
