"""ARGUS Anomaly & Detection Routes (Phase 3 §35–§43).

Route groups:

* ``/anomalies`` — list/detail/observations/explanation + lifecycle (ack/resolve)
* ``/anomaly-rules`` — validated rule CRUD
* ``/anomaly-suppressions`` — auditable suppression rules
* ``/maintenance-windows`` — scheduled, auditable suppression/downgrade windows
* ``/projects/{id}/anomalies/detect`` — run one bounded detection + correlation pass

Every read/write validates project/environment ownership server-side (§46):
resources are addressed by UUID, but an out-of-scope UUID is a 404, never data.
All list endpoints are paginated and filters are indexed columns (§47).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    require_anomaly,
    require_environment,
    require_project,
)
from app.core.database import get_db
from app.core.time import ensure_utc, utcnow
from app.models.anomaly import (
    Anomaly,
    AnomalyBaseline,
    AnomalyObservation,
    AnomalyRule,
    AnomalySeverity,
    AnomalySource,
    AnomalyStatus,
    AnomalySuppression,
    AnomalyType,
    MaintenanceWindow,
)
from app.schemas.anomaly import (
    AnomalyDetailResponse,
    AnomalyList,
    AnomalyObservationResponse,
    AnomalyResponse,
    AnomalyRuleCreate,
    AnomalyRuleList,
    AnomalyRuleResponse,
    AnomalyRuleUpdate,
    AnomalyStatusUpdate,
    AnomalySuppressionCreate,
    AnomalySuppressionResponse,
    AnomalySuppressionUpdate,
    MaintenanceWindowCreate,
    MaintenanceWindowList,
    MaintenanceWindowResponse,
    MaintenanceWindowUpdate,
    SuppressionList,
)
from app.services import anomaly_state
from app.services.anomaly_explanation import build_anomaly_explanation

router = APIRouter(tags=["Anomalies"])

_OBSERVATION_PAGE_LIMIT = 200


def _now() -> datetime:
    """Aware UTC now, via the shared helper (naive DB values must not leak in)."""
    return utcnow()


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------
@router.get("/anomalies", response_model=AnomalyList)
async def list_anomalies(
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    component_id: Optional[uuid.UUID] = None,
    incident_id: Optional[uuid.UUID] = None,
    anomaly_type: Optional[AnomalyType] = None,
    severity: Optional[AnomalySeverity] = None,
    status: Optional[AnomalyStatus] = None,
    source: Optional[AnomalySource] = None,
    metric_name: Optional[str] = None,
    fingerprint: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    include_suppressed: bool = Query(True),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> AnomalyList:
    """List anomalies with indexed filters and pagination."""
    if project_id is not None:
        await require_project(db, project_id)
        await require_environment(db, project_id, environment_id)

    query = select(Anomaly)
    count_query = select(func.count(Anomaly.id))

    conditions = []
    if project_id is not None:
        conditions.append(Anomaly.project_id == project_id)
    if environment_id is not None:
        conditions.append(Anomaly.environment_id == environment_id)
    if component_id is not None:
        conditions.append(Anomaly.component_id == component_id)
    if incident_id is not None:
        conditions.append(Anomaly.incident_id == incident_id)
    if anomaly_type is not None:
        conditions.append(Anomaly.anomaly_type == anomaly_type)
    if severity is not None:
        conditions.append(Anomaly.severity == severity)
    if status is not None:
        conditions.append(Anomaly.status == status)
    if source is not None:
        conditions.append(Anomaly.source == source)
    if metric_name is not None:
        conditions.append(Anomaly.metric_name == metric_name)
    if fingerprint is not None:
        conditions.append(Anomaly.fingerprint == fingerprint)
    if start_time is not None:
        conditions.append(Anomaly.detected_at >= start_time)
    if end_time is not None:
        conditions.append(Anomaly.detected_at <= end_time)
    if not include_suppressed:
        conditions.append(Anomaly.suppressed.is_(False))

    for condition in conditions:
        query = query.where(condition)
        count_query = count_query.where(condition)

    total = (await db.execute(count_query)).scalar() or 0
    query = (
        query.order_by(Anomaly.detected_at.desc())
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


@router.get("/anomalies/{anomaly_id}", response_model=AnomalyDetailResponse)
async def get_anomaly(
    anomaly_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    observation_limit: int = Query(50, ge=1, le=_OBSERVATION_PAGE_LIMIT),
    db: AsyncSession = Depends(get_db),
) -> AnomalyDetailResponse:
    """Anomaly detail: observations plus the deterministic explanation (§52)."""
    anomaly = await require_anomaly(
        db, anomaly_id, project_id=project_id, environment_id=environment_id
    )
    rule = await db.get(AnomalyRule, anomaly.rule_id) if anomaly.rule_id else None
    baseline = None
    if anomaly.rule_id is not None:
        baseline = (
            await db.execute(
                select(AnomalyBaseline)
                .where(
                    AnomalyBaseline.project_id == anomaly.project_id,
                    AnomalyBaseline.component_id == anomaly.component_id,
                    AnomalyBaseline.metric_name == (anomaly.metric_name or ""),
                )
                .order_by(AnomalyBaseline.computed_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    observations = list(
        (
            await db.execute(
                select(AnomalyObservation)
                .where(AnomalyObservation.anomaly_id == anomaly.id)
                .order_by(AnomalyObservation.observed_at.desc())
                .limit(observation_limit)
            )
        )
        .scalars()
        .all()
    )
    explanation = build_anomaly_explanation(anomaly, rule=rule, baseline=baseline)
    # Validate through the flat response first: validating the ORM object
    # directly against the detail model would make Pydantic read the
    # ``observations`` relationship, which lazy-loads and explodes inside an
    # async session (MissingGreenlet). Observations are attached explicitly.
    base = AnomalyResponse.model_validate(anomaly)
    return AnomalyDetailResponse(
        **base.model_dump(),
        observations=[
            AnomalyObservationResponse.model_validate(o) for o in observations
        ],
        explanation=explanation.as_dict(),
    )


async def _transition_anomaly(
    db: AsyncSession,
    anomaly: Anomaly,
    target: AnomalyStatus,
    payload: AnomalyStatusUpdate,
) -> Anomaly:
    current = AnomalyStatus(anomaly.status)
    try:
        target_status = anomaly_state.assert_transition(current, target)
    except anomaly_state.InvalidAnomalyTransition as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    if target_status is not current:
        anomaly.status = target_status
        timestamp_field = anomaly_state.timestamp_field_for(target_status)
        if timestamp_field is not None:
            setattr(anomaly, timestamp_field, _now())
        anomaly.status_changed_by = payload.actor
    await db.flush()
    await db.refresh(anomaly)
    return anomaly


@router.post("/anomalies/{anomaly_id}/acknowledge", response_model=AnomalyResponse)
async def acknowledge_anomaly(
    anomaly_id: uuid.UUID,
    payload: AnomalyStatusUpdate,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> Anomaly:
    """Acknowledge an anomaly (auditable actor)."""
    anomaly = await require_anomaly(db, anomaly_id, project_id=project_id)
    return await _transition_anomaly(db, anomaly, AnomalyStatus.ACKNOWLEDGED, payload)


@router.post("/anomalies/{anomaly_id}/resolve", response_model=AnomalyResponse)
async def resolve_anomaly(
    anomaly_id: uuid.UUID,
    payload: AnomalyStatusUpdate,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> Anomaly:
    """Resolve an anomaly (auditable actor)."""
    anomaly = await require_anomaly(db, anomaly_id, project_id=project_id)
    return await _transition_anomaly(db, anomaly, AnomalyStatus.RESOLVED, payload)


# ---------------------------------------------------------------------------
# Anomaly rules
# ---------------------------------------------------------------------------
@router.get("/anomaly-rules", response_model=AnomalyRuleList)
async def list_anomaly_rules(
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    component_id: Optional[uuid.UUID] = None,
    anomaly_type: Optional[AnomalyType] = None,
    enabled: Optional[bool] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> AnomalyRuleList:
    """List anomaly rules."""
    if project_id is not None:
        await require_project(db, project_id)
        await require_environment(db, project_id, environment_id)

    conditions = []
    if project_id is not None:
        conditions.append(AnomalyRule.project_id == project_id)
    if environment_id is not None:
        conditions.append(AnomalyRule.environment_id == environment_id)
    if component_id is not None:
        conditions.append(AnomalyRule.component_id == component_id)
    if anomaly_type is not None:
        conditions.append(AnomalyRule.anomaly_type == anomaly_type)
    if enabled is not None:
        conditions.append(AnomalyRule.enabled.is_(enabled))

    query = select(AnomalyRule)
    count_query = select(func.count(AnomalyRule.id))
    for condition in conditions:
        query = query.where(condition)
        count_query = count_query.where(condition)

    total = (await db.execute(count_query)).scalar() or 0
    query = (
        query.order_by(AnomalyRule.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(query)).scalars().all()
    return AnomalyRuleList(
        items=[AnomalyRuleResponse.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.post("/anomaly-rules", response_model=AnomalyRuleResponse, status_code=201)
async def create_anomaly_rule(
    payload: AnomalyRuleCreate,
    db: AsyncSession = Depends(get_db),
) -> AnomalyRule:
    """Create a validated anomaly rule."""
    await require_project(db, payload.project_id)
    await require_environment(db, payload.project_id, payload.environment_id)
    rule = AnomalyRule(**payload.model_dump())
    db.add(rule)
    await db.flush()
    await db.refresh(rule)
    return rule


@router.get("/anomaly-rules/{rule_id}", response_model=AnomalyRuleResponse)
async def get_anomaly_rule(
    rule_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> AnomalyRule:
    """Fetch one rule."""
    rule = await db.get(AnomalyRule, rule_id)
    if rule is None or (project_id is not None and rule.project_id != project_id):
        raise HTTPException(status_code=404, detail="Anomaly rule not found")
    return rule


@router.patch("/anomaly-rules/{rule_id}", response_model=AnomalyRuleResponse)
async def update_anomaly_rule(
    rule_id: uuid.UUID,
    payload: AnomalyRuleUpdate,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> AnomalyRule:
    """Patch a rule (only supplied fields change)."""
    rule = await db.get(AnomalyRule, rule_id)
    if rule is None or (project_id is not None and rule.project_id != project_id):
        raise HTTPException(status_code=404, detail="Anomaly rule not found")
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(rule, field, value)
    await db.flush()
    await db.refresh(rule)
    return rule


# ---------------------------------------------------------------------------
# Suppressions (§42)
# ---------------------------------------------------------------------------
@router.get("/anomaly-suppressions", response_model=SuppressionList)
async def list_suppressions(
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    active_only: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> SuppressionList:
    """List suppression rules."""
    if project_id is not None:
        await require_project(db, project_id)

    conditions = []
    if project_id is not None:
        conditions.append(AnomalySuppression.project_id == project_id)
    if environment_id is not None:
        conditions.append(AnomalySuppression.environment_id == environment_id)
    if active_only:
        now = _now()
        conditions.append(AnomalySuppression.enabled.is_(True))
        conditions.append(AnomalySuppression.starts_at <= now)
        conditions.append(
            (AnomalySuppression.ends_at.is_(None)) | (AnomalySuppression.ends_at >= now)
        )

    query = select(AnomalySuppression)
    count_query = select(func.count(AnomalySuppression.id))
    for condition in conditions:
        query = query.where(condition)
        count_query = count_query.where(condition)

    total = (await db.execute(count_query)).scalar() or 0
    query = (
        query.order_by(AnomalySuppression.starts_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(query)).scalars().all()
    return SuppressionList(
        items=[AnomalySuppressionResponse.model_validate(s) for s in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.post(
    "/anomaly-suppressions", response_model=AnomalySuppressionResponse, status_code=201
)
async def create_suppression(
    payload: AnomalySuppressionCreate,
    db: AsyncSession = Depends(get_db),
) -> AnomalySuppression:
    """Create an auditable suppression rule (anomalies are recorded, not lost)."""
    await require_project(db, payload.project_id)
    await require_environment(db, payload.project_id, payload.environment_id)
    suppression = AnomalySuppression(**payload.model_dump())
    db.add(suppression)
    await db.flush()
    await db.refresh(suppression)
    return suppression


@router.patch(
    "/anomaly-suppressions/{suppression_id}",
    response_model=AnomalySuppressionResponse,
)
async def update_suppression(
    suppression_id: uuid.UUID,
    payload: AnomalySuppressionUpdate,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> AnomalySuppression:
    """Patch a suppression (only supplied fields change).

    Deactivate (``enabled=false``) or close early (``ends_at``); never delete —
    the fact that detection was muted for a window must remain visible.
    """
    suppression = await db.get(AnomalySuppression, suppression_id)
    if suppression is None or (
        project_id is not None and suppression.project_id != project_id
    ):
        raise HTTPException(status_code=404, detail="Anomaly suppression not found")
    data = payload.model_dump(exclude_unset=True)
    if "ends_at" in data and data["ends_at"] is not None:
        # ``.starts_at`` comes from storage and may be naive (SQLite) while the
        # payload is aware — normalizing both sides avoids a 500 on comparison.
        starts_at = ensure_utc(suppression.starts_at)
        if starts_at is not None and data["ends_at"] < starts_at:
            raise HTTPException(
                status_code=422, detail="'ends_at' must be at or after 'starts_at'"
            )
    for field, value in data.items():
        setattr(suppression, field, value)
    await db.flush()
    await db.refresh(suppression)
    return suppression


# ---------------------------------------------------------------------------
# Maintenance windows (§43)
# ---------------------------------------------------------------------------
@router.get("/maintenance-windows", response_model=MaintenanceWindowList)
async def list_maintenance_windows(
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    active_only: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> MaintenanceWindowList:
    """List maintenance windows."""
    if project_id is not None:
        await require_project(db, project_id)

    conditions = []
    if project_id is not None:
        conditions.append(MaintenanceWindow.project_id == project_id)
    if environment_id is not None:
        conditions.append(MaintenanceWindow.environment_id == environment_id)
    if active_only:
        now = _now()
        conditions.append(MaintenanceWindow.enabled.is_(True))
        conditions.append(MaintenanceWindow.starts_at <= now)
        conditions.append(MaintenanceWindow.ends_at >= now)

    query = select(MaintenanceWindow)
    count_query = select(func.count(MaintenanceWindow.id))
    for condition in conditions:
        query = query.where(condition)
        count_query = count_query.where(condition)

    total = (await db.execute(count_query)).scalar() or 0
    query = (
        query.order_by(MaintenanceWindow.starts_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(query)).scalars().all()
    return MaintenanceWindowList(
        items=[MaintenanceWindowResponse.model_validate(w) for w in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.post(
    "/maintenance-windows", response_model=MaintenanceWindowResponse, status_code=201
)
async def create_maintenance_window(
    payload: MaintenanceWindowCreate,
    db: AsyncSession = Depends(get_db),
) -> MaintenanceWindow:
    """Create a maintenance window."""
    await require_project(db, payload.project_id)
    await require_environment(db, payload.project_id, payload.environment_id)
    window = MaintenanceWindow(**payload.model_dump())
    db.add(window)
    await db.flush()
    await db.refresh(window)
    return window


@router.patch(
    "/maintenance-windows/{window_id}", response_model=MaintenanceWindowResponse
)
async def update_maintenance_window(
    window_id: uuid.UUID,
    payload: MaintenanceWindowUpdate,
    project_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> MaintenanceWindow:
    """Patch a maintenance window (only supplied fields change).

    A window that neither suppresses nor downgrades would be a no-op row that
    claims to do something, so the create-time invariant is re-checked here.
    """
    window = await db.get(MaintenanceWindow, window_id)
    if window is None or (project_id is not None and window.project_id != project_id):
        raise HTTPException(status_code=404, detail="Maintenance window not found")
    data = payload.model_dump(exclude_unset=True)
    suppress = data.get("suppress_anomalies", window.suppress_anomalies)
    downgrade = data.get("downgrade_severity", window.downgrade_severity)
    if not suppress and not downgrade:
        raise HTTPException(
            status_code=422,
            detail="a window must either suppress anomalies or downgrade severity",
        )
    ends_at = data.get("ends_at", window.ends_at)
    starts_at = ensure_utc(window.starts_at)
    if ends_at is not None and starts_at is not None and ends_at <= starts_at:
        raise HTTPException(
            status_code=422, detail="'ends_at' must be after 'starts_at'"
        )
    for field, value in data.items():
        setattr(window, field, value)
    await db.flush()
    await db.refresh(window)
    return window


# ---------------------------------------------------------------------------
# Detection trigger (§19–§20) — one bounded, idempotent pass
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/anomalies/detect")
async def run_detection(
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = Query(None),
    correlate: bool = Query(True),
    lookback_seconds: int = Query(900, ge=60, le=86_400),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Run detection (and correlation) once for a scope.

    Synchronous and bounded so an operator or the demo can trigger a pass
    deterministically; the same logic runs asynchronously in the worker.
    """
    await require_project(db, project_id)
    await require_environment(db, project_id, environment_id)

    from app.services.anomaly_detection import AnomalyDetectionService
    from app.services.incident_manager import IncidentManager

    now = _now()
    detection = await AnomalyDetectionService(db, now=now, max_rules=None).run(
        project_id=project_id, environment_id=environment_id
    )
    payload: dict = {"detection": detection.as_dict(), "correlation": None}
    if correlate:
        correlation = await IncidentManager(db, now=now).process_scope(
            project_id=project_id, environment_id=environment_id
        )
        payload["correlation"] = correlation.as_dict()
    await db.commit()
    payload["lookback_seconds"] = lookback_seconds
    return payload


# ---------------------------------------------------------------------------
# Reliability metrics & dashboard (§36, §44)
# ---------------------------------------------------------------------------
@router.get("/projects/{project_id}/reliability-metrics")
async def project_reliability_metrics(
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = Query(None),
    window_seconds: int = Query(86_400, ge=60, le=2_592_000),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Deterministic reliability metrics for a project scope (§44)."""
    await require_project(db, project_id)
    await require_environment(db, project_id, environment_id)
    from app.services.anomaly_metrics import reliability_metrics

    metrics = await reliability_metrics(
        db,
        project_id=project_id,
        environment_id=environment_id,
        window_seconds=window_seconds,
    )
    return metrics.as_dict()


@router.get("/projects/{project_id}/incident-dashboard")
async def project_incident_dashboard(
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = Query(None),
    window_seconds: int = Query(86_400, ge=60, le=2_592_000),
    bucket_seconds: int = Query(3600, ge=60, le=86_400),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Aggregated dashboard payload — counts, series, top components (§36)."""
    await require_project(db, project_id)
    await require_environment(db, project_id, environment_id)
    from app.services.anomaly_metrics import incident_dashboard

    return await incident_dashboard(
        db,
        project_id=project_id,
        environment_id=environment_id,
        window_seconds=window_seconds,
        bucket_seconds=bucket_seconds,
    )


__all__ = ["router"]
