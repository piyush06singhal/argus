"""ARGUS Incident Routes (Phase 0 CRUD + Phase 3 incident intelligence).

Phase 3 adds the investigation surface — timeline, correlated anomalies,
affected components, graph context, deployment/configuration context, the
deterministic summary, and lifecycle actions — and hardens every route so that
ownership is proven server-side (§46): an out-of-scope ``project_id`` or a
nested resource belonging to another project is a 404, never data.

Legacy behaviour is preserved for callers that pass no scope; adding a
``project_id`` query parameter *narrows* the result and never widens it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    require_environment,
    require_incident,
    require_project,
)
from app.core.database import get_db
from app.models.anomaly import Anomaly, AnomalySeverity
from app.models.incident import (
    Incident,
    IncidentEvidence,
    IncidentStatus,
    IncidentTimelineEvent,
    TimelineEventType,
)
from app.models.system import SystemComponent
from app.schemas.anomaly import (
    AffectedComponentList,
    AffectedComponentResponse,
    AnomalyList,
    AnomalyResponse,
    DeploymentContextItem,
    IncidentGraphContext,
    IncidentGraphEdge,
    IncidentGraphNode,
    IncidentSummaryResponse,
    TimelineEventList,
)
from app.schemas.incident import (
    EvidenceCreate,
    EvidenceList,
    EvidenceResponse,
    IncidentCreate,
    IncidentList,
    IncidentResponse,
    IncidentUpdate,
    LifecycleActionRequest,
    TimelineEventCreate,
    TimelineEventResponse,
)
from app.services import incident_state
from app.services.anomaly_severity import max_severity
from app.services.incident_context import (
    build_configuration_context,
    build_deployment_context,
    build_graph_context,
    component_names,
)
from app.services.incident_manager import IncidentManager
from app.services.incident_summary import (
    SummaryAnomaly,
    SummaryComponent,
    SummaryTimelineItem,
    build_incident_summary,
)

router = APIRouter(prefix="/incidents", tags=["Incidents"])

_MAX_NESTED = 100


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _enum(value: object) -> Optional[str]:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


async def _scoped_incident(
    db: AsyncSession,
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID],
    environment_id: Optional[uuid.UUID] = None,
) -> Incident:
    """Fetch an incident under the caller's scope (404 when out of scope)."""
    if project_id is not None:
        await require_project(db, project_id)
        await require_environment(db, project_id, environment_id)
    return await require_incident(
        db,
        incident_id,
        project_id=project_id,
        environment_id=environment_id,
    )


# ---------------------------------------------------------------------------
# Incident CRUD (hardened)
# ---------------------------------------------------------------------------
@router.post("", response_model=IncidentResponse, status_code=201)
async def create_incident(
    incident_data: IncidentCreate,
    db: AsyncSession = Depends(get_db),
) -> Incident:
    """Create a new incident (project/environment ownership validated)."""
    await require_project(db, incident_data.project_id)
    await require_environment(
        db, incident_data.project_id, incident_data.environment_id
    )
    data = incident_data.model_dump()
    if data.get("primary_component_id") is not None:
        component = await db.get(SystemComponent, data["primary_component_id"])
        if component is None or component.project_id != incident_data.project_id:
            raise HTTPException(status_code=404, detail="Component not found")
    incident = Incident(**data)
    db.add(incident)
    await db.flush()
    await db.refresh(incident)
    return incident


@router.get("", response_model=IncidentList)
async def list_incidents(
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    severity: Optional[str] = None,
    status: Optional[str] = None,
    component_id: Optional[uuid.UUID] = None,
    fingerprint: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> IncidentList:
    """List incidents with indexed filters and pagination."""
    if project_id is not None:
        await require_project(db, project_id)
        await require_environment(db, project_id, environment_id)

    conditions = []
    if project_id is not None:
        conditions.append(Incident.project_id == project_id)
    if environment_id is not None:
        conditions.append(Incident.environment_id == environment_id)
    if severity:
        conditions.append(Incident.severity == severity)
    if status:
        conditions.append(Incident.status == status)
    if component_id is not None:
        conditions.append(Incident.primary_component_id == component_id)
    if fingerprint:
        conditions.append(Incident.fingerprint == fingerprint)
    if start_time:
        conditions.append(Incident.detected_at >= start_time)
    if end_time:
        conditions.append(Incident.detected_at <= end_time)

    query = select(Incident)
    count_query = select(func.count(Incident.id))
    for condition in conditions:
        query = query.where(condition)
        count_query = count_query.where(condition)

    total = (await db.execute(count_query)).scalar() or 0
    query = (
        query.order_by(Incident.detected_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    incidents = (await db.execute(query)).scalars().all()
    return IncidentList(
        items=[IncidentResponse.model_validate(i) for i in incidents],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.get("/{incident_id}", response_model=IncidentResponse)
async def get_incident(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> Incident:
    """Get an incident by ID (scoped when a project is supplied)."""
    return await _scoped_incident(db, incident_id, project_id, environment_id)


@router.put("/{incident_id}", response_model=IncidentResponse)
async def update_incident(
    incident_id: uuid.UUID,
    incident_data: IncidentUpdate,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> Incident:
    """Update incident metadata.

    A direct ``status`` write is validated against the lifecycle state machine,
    so an illegal transition cannot be persisted through the legacy route.
    """
    incident = await _scoped_incident(db, incident_id, project_id)
    update_data = incident_data.model_dump(exclude_unset=True)

    requested_status = update_data.pop("status", None)
    if requested_status is not None:
        try:
            target = incident_state.assert_transition(
                IncidentStatus(incident.status), IncidentStatus(requested_status)
            )
        except incident_state.InvalidIncidentTransition as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        if target is not IncidentStatus(incident.status):
            incident.status = target
            timestamp_field = incident_state.timestamp_field_for(target)
            if timestamp_field is not None:
                setattr(incident, timestamp_field, _now())
            incident.status_changed_by = (
                update_data.get("status_changed_by") or "api:update"
            )

    for field, value in update_data.items():
        setattr(incident, field, value)

    await db.flush()
    await db.refresh(incident)
    return incident


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------
@router.post(
    "/{incident_id}/evidence", response_model=EvidenceResponse, status_code=201
)
async def create_evidence(
    incident_id: uuid.UUID,
    evidence_data: EvidenceCreate,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> IncidentEvidence:
    """Attach evidence to an incident.

    Any referenced component or anomaly must belong to the incident's project —
    otherwise evidence could bridge two tenants.
    """
    incident = await _scoped_incident(db, incident_id, project_id)
    data = evidence_data.model_dump(exclude={"incident_id"})
    if data.get("component_id") is not None:
        component = await db.get(SystemComponent, data["component_id"])
        if component is None or component.project_id != incident.project_id:
            raise HTTPException(status_code=404, detail="Component not found")
    if data.get("anomaly_id") is not None:
        anomaly = await db.get(Anomaly, data["anomaly_id"])
        if anomaly is None or anomaly.project_id != incident.project_id:
            raise HTTPException(status_code=404, detail="Anomaly not found")

    evidence = IncidentEvidence(incident_id=incident_id, **data)
    db.add(evidence)
    await db.flush()
    await db.refresh(evidence)
    return evidence


@router.get("/{incident_id}/evidence", response_model=EvidenceList)
async def list_evidence(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> EvidenceList:
    """List evidence for an incident (paginated, newest first)."""
    await _scoped_incident(db, incident_id, project_id)
    count_query = select(func.count(IncidentEvidence.id)).where(
        IncidentEvidence.incident_id == incident_id
    )
    total = (await db.execute(count_query)).scalar() or 0
    query = (
        select(IncidentEvidence)
        .where(IncidentEvidence.incident_id == incident_id)
        .order_by(IncidentEvidence.timestamp.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    evidence = (await db.execute(query)).scalars().all()
    return EvidenceList(
        items=[EvidenceResponse.model_validate(e) for e in evidence],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


# ---------------------------------------------------------------------------
# Phase 3: timeline, anomalies, components, graph, context, summary
# ---------------------------------------------------------------------------
@router.get("/{incident_id}/timeline", response_model=TimelineEventList)
async def get_timeline(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> TimelineEventList:
    """Chronological timeline of stored facts and marked context (§27)."""
    await _scoped_incident(db, incident_id, project_id)
    count_query = select(func.count(IncidentTimelineEvent.id)).where(
        IncidentTimelineEvent.incident_id == incident_id
    )
    total = (await db.execute(count_query)).scalar() or 0
    query = (
        select(IncidentTimelineEvent)
        .where(IncidentTimelineEvent.incident_id == incident_id)
        .order_by(IncidentTimelineEvent.occurred_at.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    events = (await db.execute(query)).scalars().all()
    return TimelineEventList(
        items=[TimelineEventResponse.model_validate(e) for e in events],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.post(
    "/{incident_id}/timeline", response_model=TimelineEventResponse, status_code=201
)
async def add_timeline_note(
    incident_id: uuid.UUID,
    payload: TimelineEventCreate,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> IncidentTimelineEvent:
    """Add a hand-authored note to the timeline (the only writable kind)."""
    incident = await _scoped_incident(db, incident_id, project_id)
    if payload.event_type is not TimelineEventType.NOTE:
        raise HTTPException(
            status_code=422,
            detail="Only NOTE timeline events may be added manually; "
            "all other entries are derived from stored evidence",
        )
    event = IncidentTimelineEvent(
        incident_id=incident.id,
        project_id=incident.project_id,
        environment_id=incident.environment_id,
        event_type=TimelineEventType.NOTE,
        occurred_at=payload.occurred_at,
        title=payload.title,
        description=payload.description,
        provenance="manual",
        metadata_={"actor": payload.actor},
    )
    db.add(event)
    await db.flush()
    await db.refresh(event)
    return event


@router.get("/{incident_id}/anomalies", response_model=AnomalyList)
async def get_incident_anomalies(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> AnomalyList:
    """The anomalies correlated into this incident (grouping, not causation)."""
    await _scoped_incident(db, incident_id, project_id)
    count_query = select(func.count(Anomaly.id)).where(
        Anomaly.incident_id == incident_id
    )
    total = (await db.execute(count_query)).scalar() or 0
    query = (
        select(Anomaly)
        .where(Anomaly.incident_id == incident_id)
        .order_by(Anomaly.detected_at.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(query)).scalars().all()
    return AnomalyList(
        items=[AnomalyResponse.model_validate(a) for a in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.get("/{incident_id}/components", response_model=AffectedComponentList)
async def get_incident_components(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> AffectedComponentList:
    """Observed blast radius: directly observed vs upstream/downstream context."""
    await _scoped_incident(db, incident_id, project_id)
    from app.services.incident_context import build_dependency_components

    observed = [
        c
        for c in (
            (
                await db.execute(
                    select(Anomaly.component_id).where(
                        Anomaly.incident_id == incident_id,
                        Anomaly.component_id.isnot(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        if c is not None
    ]
    views = await build_dependency_components(db, observed)

    # Per-component anomaly counts and worst severity, so "affected" is
    # quantified from stored evidence rather than implied by presence.
    count_rows = (
        await db.execute(
            select(Anomaly.component_id, func.count(Anomaly.id))
            .where(
                Anomaly.incident_id == incident_id,
                Anomaly.component_id.isnot(None),
            )
            .group_by(Anomaly.component_id)
        )
    ).all()
    counts = {cid: count for cid, count in count_rows}
    severity_rows = (
        await db.execute(
            select(Anomaly.component_id, Anomaly.severity).where(
                Anomaly.incident_id == incident_id,
                Anomaly.component_id.isnot(None),
            )
        )
    ).all()
    worst: dict[uuid.UUID, object] = {}
    for cid, severity in severity_rows:
        current = worst.get(cid)
        worst[cid] = (
            severity
            if current is None
            else max_severity(AnomalySeverity(current), AnomalySeverity(severity))
        )

    items = [
        AffectedComponentResponse(
            component_id=v.component_id,
            name=v.name,
            classification=v.classification,
            reason=v.reason,
            anomaly_count=counts.get(v.component_id, 0),
            severity=worst.get(v.component_id),  # type: ignore[arg-type]
        )
        for v in views
    ]
    total = len(items)
    start = (page - 1) * page_size
    return AffectedComponentList(
        items=items[start : start + page_size],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.get("/{incident_id}/graph", response_model=IncidentGraphContext)
async def get_incident_graph(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> IncidentGraphContext:
    """Structural graph slice around the incident (never a causality claim)."""
    incident = await _scoped_incident(db, incident_id, project_id)
    from app.services.incident_context import build_dependency_components

    observed = [
        c
        for c in (
            (
                await db.execute(
                    select(Anomaly.component_id).where(
                        Anomaly.incident_id == incident_id,
                        Anomaly.component_id.isnot(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        if c is not None
    ]
    views = await build_dependency_components(db, observed)
    classifications = {v.component_id: v.classification for v in views}
    context = await build_graph_context(
        db,
        project_id=incident.project_id,
        component_ids=list(classifications.keys()),
        classifications=classifications,
    )
    return IncidentGraphContext(
        nodes=[IncidentGraphNode(**n) for n in context.nodes],
        edges=[IncidentGraphEdge(**e) for e in context.edges],
        disclaimer=context.disclaimer,
    )


@router.get("/{incident_id}/deployments", response_model=list[DeploymentContextItem])
async def get_incident_deployments(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> list[DeploymentContextItem]:
    """Nearby deployments — temporal context only (§31)."""
    incident = await _scoped_incident(db, incident_id, project_id)
    rows = await build_deployment_context(db, incident)
    return [DeploymentContextItem(**row) for row in rows]


@router.get("/{incident_id}/configuration-changes")
async def get_incident_configuration_changes(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Nearby configuration changes — temporal context only (§32)."""
    incident = await _scoped_incident(db, incident_id, project_id)
    return await build_configuration_context(db, incident)


@router.get("/{incident_id}/summary", response_model=IncidentSummaryResponse)
async def get_incident_summary(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> IncidentSummaryResponse:
    """Deterministic summary regenerated from stored evidence (§33)."""
    incident = await _scoped_incident(db, incident_id, project_id)
    anomalies = list(
        (
            await db.execute(
                select(Anomaly)
                .where(Anomaly.incident_id == incident_id)
                .order_by(Anomaly.detected_at)
                .limit(_MAX_NESTED)
            )
        )
        .scalars()
        .all()
    )
    names = await component_names(
        db, [a.component_id for a in anomalies if a.component_id is not None]
    )
    summary_anomalies = [
        SummaryAnomaly(
            anomaly_type=_enum(a.anomaly_type) or "",
            severity=_enum(a.severity) or "",
            component_name=(
                names.get(a.component_id) if a.component_id is not None else None
            ),
            metric_name=a.metric_name,
            pattern_template=a.pattern_template,
            observed_value=a.observed_value,
            expected_value=a.expected_value,
            deviation=a.deviation,
            detected_at=a.detected_at,
            suppressed=bool(a.suppressed),
        )
        for a in anomalies
    ]
    deployments = await build_deployment_context(db, incident)
    config_changes = await build_configuration_context(db, incident)
    # The summary's blast-radius section must match `/components`: observed
    # components *and* their classified upstream/downstream context, not just
    # the primary component.
    from app.services.incident_context import build_dependency_components

    observed_ids: list[uuid.UUID] = [
        a.component_id for a in anomalies if a.component_id is not None
    ]
    if incident.primary_component_id is not None:
        observed_ids.append(incident.primary_component_id)
    affected_views = await build_dependency_components(db, observed_ids)
    summary_components: list[SummaryComponent] = [
        SummaryComponent(name=v.name, classification=v.classification)
        for v in affected_views
    ]
    primary_name: Optional[str] = None
    if incident.primary_component_id is not None:
        primary_names = await component_names(db, [incident.primary_component_id])
        primary_name = primary_names.get(incident.primary_component_id)
    if not summary_components and primary_name:
        summary_components.append(
            SummaryComponent(name=primary_name, classification="DIRECTLY_OBSERVED")
        )
    text, generated_from = build_incident_summary(
        title=incident.title,
        severity=_enum(incident.severity) or "",
        status=_enum(incident.status) or "",
        detected_at=incident.detected_at,
        resolved_at=incident.resolved_at,
        primary_component_name=primary_name,
        anomalies=summary_anomalies,
        components=summary_components,
        deployments=[
            SummaryTimelineItem(
                label=f"Deployment {d['deployment_id']}",
                occurred_at=d["deployed_at"],
                seconds_from_first_anomaly=d["seconds_before_first_anomaly"],
            )
            for d in deployments
        ],
        config_changes=[
            SummaryTimelineItem(
                label=f"Configuration change {c['summary']}",
                occurred_at=c["changed_at"],
                seconds_from_first_anomaly=(
                    (incident.detected_at - c["changed_at"]).total_seconds()
                    if c["changed_at"]
                    else None
                ),
            )
            for c in config_changes
        ],
    )
    return IncidentSummaryResponse(
        incident_id=incident.id,
        title=incident.title,
        severity=incident.severity,
        status=incident.status,
        started_at=incident.started_at,
        detected_at=incident.detected_at,
        resolved_at=incident.resolved_at,
        text=text,
        generated_from=generated_from,
    )


# ---------------------------------------------------------------------------
# Lifecycle actions (§26)
# ---------------------------------------------------------------------------
async def _apply_transition(
    db: AsyncSession,
    incident: Incident,
    target: IncidentStatus,
    payload: LifecycleActionRequest,
) -> Incident:
    manager = IncidentManager(db, now=_now())
    try:
        await manager.transition(
            incident, target, actor=payload.actor, note=payload.note
        )
    except incident_state.InvalidIncidentTransition as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await db.flush()
    await db.refresh(incident)
    return incident


@router.post("/{incident_id}/acknowledge", response_model=IncidentResponse)
async def acknowledge_incident(
    incident_id: uuid.UUID,
    payload: LifecycleActionRequest,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> Incident:
    """Acknowledge an incident (auditable actor)."""
    incident = await _scoped_incident(db, incident_id, project_id)
    return await _apply_transition(db, incident, IncidentStatus.ACKNOWLEDGED, payload)


@router.post("/{incident_id}/investigate", response_model=IncidentResponse)
async def investigate_incident(
    incident_id: uuid.UUID,
    payload: LifecycleActionRequest,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> Incident:
    """Move an incident into INVESTIGATING."""
    incident = await _scoped_incident(db, incident_id, project_id)
    return await _apply_transition(db, incident, IncidentStatus.INVESTIGATING, payload)


@router.post("/{incident_id}/mitigate", response_model=IncidentResponse)
async def mitigate_incident(
    incident_id: uuid.UUID,
    payload: LifecycleActionRequest,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> Incident:
    """Mark an incident MITIGATED (impact stopped; investigation may continue)."""
    incident = await _scoped_incident(db, incident_id, project_id)
    return await _apply_transition(db, incident, IncidentStatus.MITIGATED, payload)


@router.post("/{incident_id}/resolve", response_model=IncidentResponse)
async def resolve_incident(
    incident_id: uuid.UUID,
    payload: LifecycleActionRequest,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> Incident:
    """Resolve an incident (auditable actor)."""
    incident = await _scoped_incident(db, incident_id, project_id)
    return await _apply_transition(db, incident, IncidentStatus.RESOLVED, payload)


@router.post("/{incident_id}/reopen", response_model=IncidentResponse)
async def reopen_incident(
    incident_id: uuid.UUID,
    payload: LifecycleActionRequest,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> Incident:
    """Reopen a resolved/closed incident (a regression is a real event)."""
    incident = await _scoped_incident(db, incident_id, project_id)
    return await _apply_transition(db, incident, IncidentStatus.OPEN, payload)
