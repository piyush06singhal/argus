"""ARGUS Predictive Reliability Routes (Phase 8 §44).

```text
POST   /reliability/forecasts/generate            request a forecast pass (§57)
GET    /reliability/forecasts                     forecasts (project-scoped)
GET    /reliability/forecasts/{id}                one forecast + its signals
GET    /reliability/forecasts/{id}/explanation    the §34/§51 explanation
GET    /reliability/forecasts/{id}/signals        the predictive signals (§35)
GET    /reliability/forecasts/{id}/snapshot       the exact features used (§17)
GET    /reliability/forecasts/{id}/outcome        what actually happened (§28)

GET    /reliability/heatmap                       component × horizon risk (§47)
GET    /reliability/components/{id}/profile       reliability profile (§37)
GET    /reliability/components/{id}/forecasts     that component's forecasts
GET    /reliability/signals                       signal stream, project-scoped
GET    /reliability/models                        model registry (§54)
GET    /reliability/models/{id}                   one model version
GET    /reliability/evaluations                   accuracy runs (§53)
POST   /reliability/evaluate                      score due forecasts (§28)
POST   /reliability/backtests                     run a walk-forward backtest (§55)
GET    /reliability/backtests                     backtest history
GET    /reliability/backtests/{id}                one backtest + its steps
GET    /reliability/health                        platform health (§43)
GET    /reliability/drift                         stored drift findings (§41, §42)
POST   /reliability/drift/assess                  run a drift assessment
GET    /reliability/warnings                      early warnings (§39)
POST   /reliability/warnings/{id}/acknowledge     human acknowledges (§39)
POST   /reliability/warnings/{id}/dismiss         human dismisses (§39)
```

Scope rules, following the Phase 3–7 convention: a mutating request **requires**
``project_id`` and proves ownership; reads accept an optional ``project_id`` and
enforce it when supplied, answering 404 rather than confirming existence.

Nothing here remediates anything. There is no endpoint that rolls back, scales,
deploys or patches — that boundary is deliberate and is the whole point of the
phase's scope (§8, §87).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_component, require_environment, require_project
from app.core.config import get_settings
from app.core.database import get_db
from app.services.reliability_narrative import resolve_narrative_provider
from app.models.reliability import (
    DriftKind,
    EarlyWarningStatus,
    ForecastDataQuality,
    ForecastHorizon,
    ForecastOutcome,
    ForecastRiskLevel,
    ForecastStatus,
    PredictionType,
    PredictiveSignal,
    ReliabilityBacktest,
    ReliabilityDriftRecord,
    ReliabilityEarlyWarning,
    ReliabilityEvaluationRun,
    ReliabilityForecast,
    ReliabilityModelVersion,
)
from app.schemas.reliability import (
    BacktestListResponse,
    BacktestRequest,
    BacktestResponse,
    BacktestRunResponse,
    ComponentProfileResponse,
    DriftHistoryResponse,
    DriftReportResponse,
    EarlyWarningListResponse,
    EarlyWarningResponse,
    EvaluationRunListResponse,
    EvaluationRunResponse,
    FeatureSnapshotResponse,
    ForecastExplanationResponse,
    ForecastGenerateRequest,
    ForecastGenerateResponse,
    ModelVersionListResponse,
    ModelVersionResponse,
    PlatformHealthResponse,
    PredictionOutcomeResponse,
    PredictiveSignalResponse,
    ReliabilityForecastListResponse,
    ReliabilityForecastResponse,
    RiskHeatmapCellResponse,
    RiskHeatmapResponse,
    WarningActionRequest,
)
from app.services.queue import (
    enqueue_reliability_evaluate,
    enqueue_reliability_forecast,
)
from app.services.reliability_backtest import BacktestConfiguration, BacktestEngine
from app.services.reliability_drift import DriftMonitor, drift_summary
from app.services.reliability_evaluation import PredictionEvaluationService
from app.services.reliability_features import ReliabilityFeatureEngine, aware_utc
from app.services.reliability_forecast_service import ReliabilityForecastService
from app.services.reliability_risk import risk_rank
from app.services.reliability_warnings import EarlyWarningService

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()

_LIST_LIMIT = 200


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _signal_response(row: PredictiveSignal) -> PredictiveSignalResponse:
    return PredictiveSignalResponse(
        id=row.id,
        forecast_id=row.forecast_id,
        project_id=row.project_id,
        environment_id=row.environment_id,
        component_id=row.component_id,
        signal_type=str(getattr(row.signal_type, "value", row.signal_type)),
        severity=row.severity,
        contribution=row.contribution,
        rank=row.rank,
        description=row.description,
        metric_name=row.metric_name,
        observed_value=row.observed_value,
        baseline_value=row.baseline_value,
        change_rate=row.change_rate,
        trend=row.trend,
        evidence_ids=dict(row.evidence_ids or {}),
        similar_incident_count=row.similar_incident_count,
        created_at=row.created_at,
    )


def _forecast_response(
    row: ReliabilityForecast, signals: Optional[list[PredictiveSignal]] = None
) -> ReliabilityForecastResponse:
    return ReliabilityForecastResponse(
        id=row.id,
        project_id=row.project_id,
        environment_id=row.environment_id,
        component_id=row.component_id,
        prediction_type=row.prediction_type,
        forecast_horizon=row.forecast_horizon,
        generated_at=row.generated_at,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        risk_score=row.risk_score,
        risk_level=row.risk_level,
        confidence=row.confidence,
        confidence_reason=row.confidence_reason,
        calibration_status=row.calibration_status,
        data_quality=row.data_quality,
        data_coverage=row.data_coverage,
        model_version_id=row.model_version_id,
        model_version_label=row.model_version_label,
        feature_snapshot_id=row.feature_snapshot_id,
        status=row.status,
        fingerprint=row.fingerprint,
        dominant_signal=(
            str(getattr(row.dominant_signal, "value", row.dominant_signal))
            if row.dominant_signal
            else None
        ),
        headline=row.headline,
        summary=row.summary,
        limitations=list(row.limitations or []),
        supporting_evidence=dict(row.supporting_evidence or {}),
        failure_reason=row.failure_reason,
        failure_detail=row.failure_detail,
        previous_forecast_id=row.previous_forecast_id,
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
        signals=[_signal_response(s) for s in (signals or [])],
    )


async def _require_forecast(
    db: AsyncSession, forecast_id: uuid.UUID, project_id: Optional[uuid.UUID]
) -> ReliabilityForecast:
    """Fetch a forecast, enforcing project scope. 404 on unknown or foreign."""
    forecast = await db.get(ReliabilityForecast, forecast_id)
    if forecast is None or (
        project_id is not None and forecast.project_id != project_id
    ):
        raise HTTPException(status_code=404, detail="Forecast not found")
    return forecast


async def _require_backtest(
    db: AsyncSession, backtest_id: uuid.UUID, project_id: uuid.UUID
) -> ReliabilityBacktest:
    backtest = await db.get(ReliabilityBacktest, backtest_id)
    if backtest is None or backtest.project_id != project_id:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return backtest


async def _require_warning(
    db: AsyncSession, warning_id: uuid.UUID, project_id: Optional[uuid.UUID]
) -> ReliabilityEarlyWarning:
    warning = await db.get(ReliabilityEarlyWarning, warning_id)
    if warning is None or (project_id is not None and warning.project_id != project_id):
        raise HTTPException(status_code=404, detail="Warning not found")
    return warning


def _health_scope(project_id: Optional[uuid.UUID]) -> dict:
    """The reliability dashboard is project-scoped, or unscoped for operators."""
    return {"project_id": project_id}


def _as_enum(value: Any, enum_cls: Any) -> Any:
    """Coerce a request field to a real enum member.

    Request schemas inherit ``use_enum_values``, so a body carrying
    ``"ONE_HOUR"`` arrives as a plain string. The service layer is typed on the
    enum (it reads ``.seconds``), and a string silently satisfies neither
    branch of a comparison — so the boundary normalizes, once, here.
    """
    if value is None:
        return None
    return value if isinstance(value, enum_cls) else enum_cls(value)


def _as_enums(values: Optional[list[Any]], enum_cls: Any) -> Optional[list[Any]]:
    if not values:
        return None
    return [_as_enum(value, enum_cls) for value in values]


# ---------------------------------------------------------------------------
# Generation (§26, §57, §58)
# ---------------------------------------------------------------------------


@router.post(
    "/reliability/forecasts/generate",
    response_model=ForecastGenerateResponse,
    summary="Request a forecast pass",
)
async def generate_forecasts(
    request: ForecastGenerateRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> ForecastGenerateResponse:
    """Schedule (or run) a forecast pass for one project.

    ``dispatch=true`` queues the work and returns immediately — a forecast pass
    scans history and must never block a request thread (§57). ``dispatch=false``
    runs it inline and is intended for operators and tests; it is still bounded
    by ``RELIABILITY_MAX_COMPONENTS_PER_RUN``.
    """
    await require_project(db, project_id)
    await require_environment(db, project_id, request.environment_id)

    if request.dispatch:
        queued = await enqueue_reliability_forecast(
            project_id=project_id, environment_id=request.environment_id
        )
        return ForecastGenerateResponse(
            dispatched=queued,
            project_id=project_id,
            job_id="reliability_forecast" if queued else None,
            message=(
                "forecast pass queued on the reliability worker"
                if queued
                else (
                    "async reliability is disabled or the queue is unreachable; "
                    "run the pass inline with dispatch=false, or rely on the "
                    "scheduled sweep"
                )
            ),
        )

    if not settings.RELIABILITY_FORECASTING_ENABLED:
        raise HTTPException(
            status_code=503,
            detail=(
                "forecasting is disabled (RELIABILITY_FORECASTING_ENABLED=false); "
                "no forecast was produced"
            ),
        )

    service = ReliabilityForecastService(db)
    result = await service.generate_for_project(
        project_id=project_id,
        environment_id=request.environment_id,
        prediction_types=_as_enums(request.prediction_types, PredictionType),
        horizons=_as_enums(request.horizons, ForecastHorizon),
        limit=request.limit,
    )
    await db.commit()
    return ForecastGenerateResponse(
        dispatched=False,
        project_id=project_id,
        scopes=result.scopes,
        forecasts_created=result.forecasts_created,
        forecasts_updated=result.forecasts_updated,
        signals_created=result.signals,
        skipped=list(result.skipped),
        errors=list(result.errors),
        duration_ms=result.duration_ms,
        message=(
            f"generated {result.forecasts_created} new forecast(s), updated "
            f"{result.forecasts_updated}, refused {result.refusals} for "
            f"insufficient evidence"
        ),
    )


# ---------------------------------------------------------------------------
# Forecasts (§2, §17, §28, §34, §35)
# ---------------------------------------------------------------------------


@router.get(
    "/reliability/forecasts",
    response_model=ReliabilityForecastListResponse,
    summary="List forecasts",
)
async def list_forecasts(
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    environment_id: Optional[uuid.UUID] = Query(None),
    component_id: Optional[uuid.UUID] = Query(None),
    prediction_type: Optional[PredictionType] = Query(None),
    forecast_horizon: Optional[ForecastHorizon] = Query(None),
    risk_level: Optional[ForecastRiskLevel] = Query(None),
    status: Optional[ForecastStatus] = Query(None),
    active_only: bool = Query(False, description="Only unexpired forecasts"),
    limit: int = Query(default=_LIST_LIMIT, ge=1, le=_LIST_LIMIT),
    db: AsyncSession = Depends(get_db),
) -> ReliabilityForecastListResponse:
    """Newest-first forecasts, filtered by any dimension the heatmap offers."""
    await require_project(db, project_id)
    clauses: list[Any] = [ReliabilityForecast.project_id == project_id]
    if environment_id is not None:
        clauses.append(ReliabilityForecast.environment_id == environment_id)
    if component_id is not None:
        clauses.append(ReliabilityForecast.component_id == component_id)
    if prediction_type is not None:
        clauses.append(ReliabilityForecast.prediction_type == prediction_type)
    if forecast_horizon is not None:
        clauses.append(ReliabilityForecast.forecast_horizon == forecast_horizon)
    if risk_level is not None:
        clauses.append(ReliabilityForecast.risk_level == risk_level)
    if status is not None:
        clauses.append(ReliabilityForecast.status == status)
    if active_only:
        clauses.append(ReliabilityForecast.valid_until >= datetime.now(timezone.utc))

    rows = list(
        (
            await db.execute(
                select(ReliabilityForecast)
                .where(*clauses)
                .order_by(ReliabilityForecast.generated_at.desc())
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    truncated = len(rows) > limit
    items = [_forecast_response(row) for row in rows[:limit]]
    return ReliabilityForecastListResponse(
        items=items, total=len(items), truncated=truncated
    )


@router.get(
    "/reliability/forecasts/{forecast_id}",
    response_model=ReliabilityForecastResponse,
    summary="Get one forecast",
)
async def get_forecast(
    forecast_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> ReliabilityForecastResponse:
    forecast = await _require_forecast(db, forecast_id, project_id)
    signals = list(
        (
            await db.execute(
                select(PredictiveSignal)
                .where(PredictiveSignal.forecast_id == forecast.id)
                .order_by(PredictiveSignal.rank.asc())
            )
        )
        .scalars()
        .all()
    )
    return _forecast_response(forecast, signals)


@router.get(
    "/reliability/forecasts/{forecast_id}/explanation",
    response_model=ForecastExplanationResponse,
    summary="Explain a forecast",
)
async def explain_forecast(
    forecast_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> ForecastExplanationResponse:
    """What changed, why risk moved, what supports it, what is uncertain (§34).

    The historical block rebuilds the *original* feature window (anchored at the
    forecast's own generation time) so the similarity search uses the same
    evidence the forecast was built from, not today's data.
    """
    forecast = await _require_forecast(db, forecast_id, project_id)
    service = ReliabilityForecastService(db)
    payload = await service.explain(forecast)

    historical: list[dict] = []
    try:
        bundle = await ReliabilityFeatureEngine(db).build(
            project_id=forecast.project_id,
            environment_id=forecast.environment_id,
            component_id=forecast.component_id,
            forecast_time=forecast.generated_at,
        )
        historical = await service.similar_incidents(
            project_id=forecast.project_id,
            component_id=forecast.component_id,
            bundle=bundle,
            limit=5,
            now=forecast.generated_at,
        )
    except Exception:  # noqa: BLE001 - explanation must still render without it
        logger.exception("similar-incident search failed for %s", forecast.id)

    #: The optional narrative layer (§34) re-words the explanation above; it is
    #: additive and cannot reach a field that carries a claim. With no provider
    #: configured this returns nothing and the response stays deterministic.
    narrative = await resolve_narrative_provider().render(
        {**payload, "historical_evidence": historical}
    )

    return ForecastExplanationResponse(
        forecast_id=forecast.id,
        headline=payload["headline"],
        summary=payload.get("summary"),
        risk_level=forecast.risk_level,
        risk_score=forecast.risk_score,
        prediction_type=forecast.prediction_type,
        forecast_horizon=forecast.forecast_horizon,
        horizon_label=payload["horizon_label"],
        model_version=payload["model_version"],
        generated_at=forecast.generated_at,
        valid_until=forecast.valid_until,
        confidence=forecast.confidence,
        confidence_reason=forecast.confidence_reason,
        calibration_status=forecast.calibration_status,
        data_quality=forecast.data_quality,
        data_coverage=forecast.data_coverage,
        what_changed=payload["what_changed"],
        why_risk_increased=payload["why_risk_increased"],
        what_supports_this=payload["what_supports_this"],
        what_is_uncertain=payload["what_is_uncertain"],
        why_risk_changed=payload["why_risk_changed"],
        historical_evidence=historical,
        caveats=payload["caveats"],
        ai_narrative=narrative.text,
        ai_narrative_provider=narrative.provider,
        ai_narrative_degraded=narrative.degraded,
    )


@router.get(
    "/reliability/forecasts/{forecast_id}/signals",
    response_model=list[PredictiveSignalResponse],
    summary="Signals behind a forecast",
)
async def forecast_signals(
    forecast_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> list[PredictiveSignalResponse]:
    forecast = await _require_forecast(db, forecast_id, project_id)
    rows = list(
        (
            await db.execute(
                select(PredictiveSignal)
                .where(PredictiveSignal.forecast_id == forecast.id)
                .order_by(PredictiveSignal.rank.asc())
            )
        )
        .scalars()
        .all()
    )
    return [_signal_response(row) for row in rows]


@router.get(
    "/reliability/forecasts/{forecast_id}/snapshot",
    response_model=FeatureSnapshotResponse,
    summary="The features behind a forecast",
)
async def forecast_snapshot(
    forecast_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> FeatureSnapshotResponse:
    """The exact inputs, so the prediction is auditable and reproducible (§17)."""
    forecast = await _require_forecast(db, forecast_id, project_id)
    if forecast.feature_snapshot_id is None:
        raise HTTPException(
            status_code=404,
            detail="this forecast has no feature snapshot (it failed before one was stored)",
        )
    from app.models.reliability import ForecastFeatureSnapshot

    snapshot = await db.get(ForecastFeatureSnapshot, forecast.feature_snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Feature snapshot not found")
    return FeatureSnapshotResponse(
        id=snapshot.id,
        project_id=snapshot.project_id,
        environment_id=snapshot.environment_id,
        component_id=snapshot.component_id,
        forecast_time=snapshot.forecast_time,
        feature_window_start=snapshot.feature_window_start,
        feature_window_end=snapshot.feature_window_end,
        feature_schema_version=snapshot.feature_schema_version,
        feature_values=dict(snapshot.feature_values or {}),
        data_sources=dict(snapshot.data_sources or {}),
        data_quality=snapshot.data_quality,
        data_quality_notes=list(snapshot.data_quality_notes or []),
        data_coverage=snapshot.data_coverage,
        sample_count=snapshot.sample_count,
        created_at=snapshot.created_at,
    )


@router.get(
    "/reliability/forecasts/{forecast_id}/outcome",
    response_model=Optional[PredictionOutcomeResponse],
    summary="What actually happened",
)
async def forecast_outcome(
    forecast_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> Optional[PredictionOutcomeResponse]:
    """The evaluation, if the horizon has elapsed and it has been scored."""
    forecast = await _require_forecast(db, forecast_id, project_id)
    row = (
        await db.execute(
            select(ForecastOutcome)
            .where(ForecastOutcome.forecast_id == forecast.id)
            .order_by(ForecastOutcome.evaluated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return PredictionOutcomeResponse(
        id=row.id,
        forecast_id=row.forecast_id,
        project_id=row.project_id,
        environment_id=row.environment_id,
        component_id=row.component_id,
        evaluation_window_start=row.evaluation_window_start,
        evaluation_window_end=row.evaluation_window_end,
        outcome=row.outcome,
        actual_event=row.actual_event,
        actual_severity=row.actual_severity,
        time_to_event_seconds=row.time_to_event_seconds,
        matched_incident_id=row.matched_incident_id,
        matched_anomaly_id=row.matched_anomaly_id,
        predicted_risk_level=row.predicted_risk_level,
        predicted_risk_score=row.predicted_risk_score,
        evaluation_reason=row.evaluation_reason,
        evaluated_at=row.evaluated_at,
    )


# ---------------------------------------------------------------------------
# Heatmap and component views (§37, §47, §52)
# ---------------------------------------------------------------------------


@router.get(
    "/reliability/heatmap",
    response_model=RiskHeatmapResponse,
    summary="Component × horizon risk heatmap",
)
async def risk_heatmap(
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    environment_id: Optional[uuid.UUID] = Query(None),
    prediction_type: Optional[PredictionType] = Query(None),
    active_only: bool = Query(True),
    db: AsyncSession = Depends(get_db),
) -> RiskHeatmapResponse:
    """The newest forecast per component × type × horizon, as a grid (§47).

    A cell only exists where a forecast exists. Absent evidence is *absent*, not
    green — the UI is expected to render the gap rather than invent a LOW.
    """
    await require_project(db, project_id)
    await require_environment(db, project_id, environment_id)

    now = datetime.now(timezone.utc)
    clauses: list[Any] = [ReliabilityForecast.project_id == project_id]
    if environment_id is not None:
        clauses.append(ReliabilityForecast.environment_id == environment_id)
    if prediction_type is not None:
        clauses.append(ReliabilityForecast.prediction_type == prediction_type)
    if active_only:
        clauses.append(ReliabilityForecast.valid_until >= now)

    rows = list(
        (
            await db.execute(
                select(ReliabilityForecast)
                .where(*clauses)
                .order_by(ReliabilityForecast.generated_at.desc())
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )

    #: Newest wins: the query is newest-first, so the first row seen for a cell
    #: is the current belief about that component, type and horizon.
    cells: dict[tuple[Any, Any, Any], dict] = {}
    for row in rows:
        #: Generation only ever produces component-scoped forecasts, so a row
        #: whose component reference is NULL is an *orphan*: its component was
        #: deleted, and ``ON DELETE SET NULL`` deliberately kept the forecast as
        #: history rather than erasing it. It can no longer be attributed to a
        #: component, so this component grid omits it instead of inventing a
        #: nameless column that reads like a project-wide claim. The row stays
        #: visible through the forecast endpoints; it is simply not a cell.
        if row.component_id is None:
            continue
        key = (row.component_id, row.environment_id, row.prediction_type)
        cell = cells.setdefault(
            key,
            {
                "component_id": row.component_id,
                "environment_id": row.environment_id,
                "prediction_type": row.prediction_type,
                "by_horizon": {},
                "worst": ForecastRiskLevel.UNKNOWN,
                "evidence": 0,
            },
        )
        if row.forecast_horizon in cell["by_horizon"]:
            continue
        cell["by_horizon"][row.forecast_horizon] = row.risk_level
        if risk_rank(row.risk_level) > risk_rank(cell["worst"]):
            cell["worst"] = row.risk_level
        cell["evidence"] += 1

    component_ids = [key[0] for key in cells if key[0] is not None]
    names: dict[Any, str] = {}
    if component_ids:
        from app.models.system import SystemComponent

        names = {
            row[0]: row[1]
            for row in (
                await db.execute(
                    select(SystemComponent.id, SystemComponent.name).where(
                        SystemComponent.id.in_(component_ids)
                    )
                )
            ).all()
        }

    out: list[RiskHeatmapCellResponse] = []
    for (_, _, _), cell in sorted(
        cells.items(), key=lambda item: (-risk_rank(item[1]["worst"]), str(item[0]))
    ):
        out.append(
            RiskHeatmapCellResponse(
                component_id=cell["component_id"],
                component_name=names.get(cell["component_id"]),
                environment_id=cell["environment_id"],
                prediction_type=cell["prediction_type"],
                by_horizon=cell["by_horizon"],
                worst_level=cell["worst"],
                evidence_count=cell["evidence"],
            )
        )

    return RiskHeatmapResponse(
        cells=out,
        horizons=sorted(
            {h for cell in cells.values() for h in cell["by_horizon"]},
            key=lambda h: h.seconds,
        ),
        generated_at=now,
        empty_reason=None if out else "no current forecast exists in this scope",
    )


@router.get(
    "/reliability/components/{component_id}/profile",
    response_model=ComponentProfileResponse,
    summary="Component reliability profile",
)
async def component_profile(
    component_id: uuid.UUID,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    environment_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> ComponentProfileResponse:
    """Everything the profile view shows — incidents, trends, forecasts, score."""
    await require_project(db, project_id)
    await require_component(
        db, component_id, project_id=project_id, environment_id=environment_id
    )
    service = ReliabilityForecastService(db)
    payload = await service.component_profile(
        project_id=project_id,
        component_id=component_id,
        environment_id=environment_id,
    )
    forecasts = list(
        (
            await db.execute(
                select(ReliabilityForecast)
                .where(
                    ReliabilityForecast.project_id == project_id,
                    ReliabilityForecast.component_id == component_id,
                )
                .order_by(ReliabilityForecast.generated_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    worst = ForecastRiskLevel.UNKNOWN
    for row in forecasts:
        if risk_rank(row.risk_level) > risk_rank(worst):
            worst = row.risk_level

    return ComponentProfileResponse(
        project_id=project_id,
        project_name=payload.get("project_name"),
        environment_id=environment_id,
        environment_name=payload.get("environment_name"),
        component_id=component_id,
        component_name=payload.get("component_name"),
        component_type=payload.get("component_type"),
        generated_at=aware_utc(datetime.fromisoformat(payload["generated_at"])),
        current_risk=payload.get("current_risk", {}),
        worst_risk=worst,
        signals=payload.get("signals", {}),
        reliability_score=payload.get("reliability_score", {}),
        data_quality=payload.get("data_quality") or ForecastDataQuality.INSUFFICIENT,
        data_coverage=payload.get("data_coverage"),
        data_quality_notes=payload.get("data_quality_notes", []),
        recent_incidents=payload.get("recent_incidents", []),
        forecasts=[_forecast_response(row) for row in forecasts],
        limitations=payload.get("limitations", []),
    )


@router.get(
    "/reliability/components/{component_id}/forecasts",
    response_model=ReliabilityForecastListResponse,
    summary="Forecasts for one component",
)
async def component_forecasts(
    component_id: uuid.UUID,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    prediction_type: Optional[PredictionType] = Query(None),
    forecast_horizon: Optional[ForecastHorizon] = Query(None),
    active_only: bool = Query(False),
    limit: int = Query(default=_LIST_LIMIT, ge=1, le=_LIST_LIMIT),
    db: AsyncSession = Depends(get_db),
) -> ReliabilityForecastListResponse:
    await require_project(db, project_id)
    await require_component(db, component_id, project_id=project_id)
    clauses: list[Any] = [
        ReliabilityForecast.project_id == project_id,
        ReliabilityForecast.component_id == component_id,
    ]
    if prediction_type is not None:
        clauses.append(ReliabilityForecast.prediction_type == prediction_type)
    if forecast_horizon is not None:
        clauses.append(ReliabilityForecast.forecast_horizon == forecast_horizon)
    if active_only:
        clauses.append(ReliabilityForecast.valid_until >= datetime.now(timezone.utc))
    rows = list(
        (
            await db.execute(
                select(ReliabilityForecast)
                .where(*clauses)
                .order_by(ReliabilityForecast.generated_at.desc())
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    truncated = len(rows) > limit
    items = [_forecast_response(row) for row in rows[:limit]]
    return ReliabilityForecastListResponse(
        items=items, total=len(items), truncated=truncated
    )


@router.get(
    "/reliability/signals",
    response_model=list[PredictiveSignalResponse],
    summary="Predictive signal stream",
)
async def list_signals(
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    component_id: Optional[uuid.UUID] = Query(None),
    severity: Optional[str] = Query(None),
    signal_type: Optional[str] = Query(None),
    limit: int = Query(default=_LIST_LIMIT, ge=1, le=_LIST_LIMIT),
    db: AsyncSession = Depends(get_db),
) -> list[PredictiveSignalResponse]:
    """Signals newest-first. Signals are predictive evidence, never causes (§6)."""
    await require_project(db, project_id)
    clauses: list[Any] = [PredictiveSignal.project_id == project_id]
    if component_id is not None:
        clauses.append(PredictiveSignal.component_id == component_id)
    if severity is not None:
        clauses.append(PredictiveSignal.severity == severity)
    if signal_type is not None:
        clauses.append(PredictiveSignal.signal_type == signal_type)
    rows = list(
        (
            await db.execute(
                select(PredictiveSignal)
                .where(*clauses)
                .order_by(PredictiveSignal.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [_signal_response(row) for row in rows]


# ---------------------------------------------------------------------------
# Models, evaluations, backtests (§25, §29, §32, §54, §55)
# ---------------------------------------------------------------------------


@router.get(
    "/reliability/models",
    response_model=ModelVersionListResponse,
    summary="Model registry",
)
async def list_models(
    status: Optional[str] = Query(None),
    model_type: Optional[str] = Query(None),
    limit: int = Query(default=_LIST_LIMIT, ge=1, le=_LIST_LIMIT),
    db: AsyncSession = Depends(get_db),
) -> ModelVersionListResponse:
    """Every registered model version, newest first.

    The registry is global (a model is not tenant data), but it exposes no
    customer evidence — only algorithm, parameters and evaluation metrics (§54).
    """
    clauses: list[Any] = []
    if status is not None:
        clauses.append(ReliabilityModelVersion.status == status)
    if model_type is not None:
        clauses.append(ReliabilityModelVersion.model_type == model_type)
    rows = list(
        (
            await db.execute(
                select(ReliabilityModelVersion)
                .where(*clauses)
                .order_by(ReliabilityModelVersion.created_at.desc())
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    truncated = len(rows) > limit
    return ModelVersionListResponse(
        items=[_model_response(row) for row in rows[:limit]],
        total=min(len(rows), limit),
        truncated=truncated,
    )


def _model_response(row: ReliabilityModelVersion) -> ModelVersionResponse:
    return ModelVersionResponse(
        id=row.id,
        model_name=row.model_name,
        model_type=row.model_type,
        version=row.version,
        algorithm=row.algorithm,
        training_window_seconds=row.training_window_seconds,
        feature_schema_version=row.feature_schema_version,
        parameters=dict(row.parameters or {}),
        metrics=dict(row.metrics or {}),
        calibration_metrics=dict(row.calibration_metrics or {}),
        calibration_status=row.calibration_status,
        sample_count=row.sample_count,
        status=row.status,
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get(
    "/reliability/models/{model_id}",
    response_model=ModelVersionResponse,
    summary="One model version",
)
async def get_model(
    model_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ModelVersionResponse:
    row = await db.get(ReliabilityModelVersion, model_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Model version not found")
    return _model_response(row)


@router.get(
    "/reliability/evaluations",
    response_model=EvaluationRunListResponse,
    summary="Evaluation runs",
)
async def list_evaluations(
    project_id: Optional[uuid.UUID] = Query(None),
    prediction_type: Optional[PredictionType] = Query(None),
    forecast_horizon: Optional[ForecastHorizon] = Query(None),
    limit: int = Query(default=_LIST_LIMIT, ge=1, le=_LIST_LIMIT),
    db: AsyncSession = Depends(get_db),
) -> EvaluationRunListResponse:
    """Historical evaluation runs, never overwritten (§32).

    Each run carries its sample count: a precision figure without one is not a
    measurement, it is a guess (§29, §53).
    """
    if project_id is not None:
        await require_project(db, project_id)
    clauses: list[Any] = []
    if project_id is not None:
        clauses.append(ReliabilityEvaluationRun.project_id == project_id)
    if prediction_type is not None:
        clauses.append(ReliabilityEvaluationRun.prediction_type == prediction_type)
    if forecast_horizon is not None:
        clauses.append(ReliabilityEvaluationRun.forecast_horizon == forecast_horizon)
    rows = list(
        (
            await db.execute(
                select(ReliabilityEvaluationRun)
                .where(*clauses)
                .order_by(ReliabilityEvaluationRun.created_at.desc())
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    truncated = len(rows) > limit
    return EvaluationRunListResponse(
        items=[_evaluation_response(row) for row in rows[:limit]],
        total=min(len(rows), limit),
        truncated=truncated,
    )


def _evaluation_response(row: ReliabilityEvaluationRun) -> EvaluationRunResponse:
    return EvaluationRunResponse(
        id=row.id,
        project_id=row.project_id,
        model_version_id=row.model_version_id,
        model_version_label=row.model_version_label,
        prediction_type=row.prediction_type,
        forecast_horizon=row.forecast_horizon,
        status=row.status,
        dataset_window_start=row.dataset_window_start,
        dataset_window_end=row.dataset_window_end,
        feature_schema_version=row.feature_schema_version,
        sample_count=row.sample_count,
        positive_count=row.positive_count,
        negative_count=row.negative_count,
        inconclusive_count=row.inconclusive_count,
        metrics=dict(row.metrics or {}),
        calibration=dict(row.calibration or {}),
        calibration_status=row.calibration_status,
        reliability_bands=list(row.reliability_bands or []),
        notes=list(row.notes or []),
        created_at=row.created_at,
    )


@router.post(
    "/reliability/evaluate",
    response_model=EvaluationRunResponse,
    summary="Score due forecasts",
)
async def evaluate_due(
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    prediction_type: Optional[PredictionType] = Query(None),
    forecast_horizon: Optional[ForecastHorizon] = Query(None),
    window_days: int = Query(30, ge=1, le=365),
    dispatch: bool = Query(False),
    db: AsyncSession = Depends(get_db),
) -> EvaluationRunResponse:
    """Score elapsed forecasts and aggregate them into one immutable run (§29).

    Scoring is time-driven: an outcome exists only once the horizon has passed,
    which is why this is a job and not an ingestion hook.
    """
    await require_project(db, project_id)
    if dispatch:
        queued = await enqueue_reliability_evaluate(project_id=project_id)
        if not queued:
            raise HTTPException(
                status_code=503,
                detail=(
                    "async reliability is disabled or the queue is unreachable; "
                    "call again with dispatch=false"
                ),
            )
        raise HTTPException(
            status_code=202,
            detail="evaluation queued; poll /reliability/evaluations for the run",
        )

    now = datetime.now(timezone.utc)
    service = PredictionEvaluationService(db)
    run = await service.run_evaluation(
        project_id=project_id,
        window_start=now - timedelta(days=window_days),
        window_end=now,
        prediction_type=prediction_type,
        horizon=forecast_horizon,
    )
    await db.commit()
    return _evaluation_response(run)


@router.post(
    "/reliability/backtests",
    response_model=BacktestRunResponse,
    summary="Run a walk-forward backtest",
)
async def run_backtest(
    request: BacktestRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> BacktestRunResponse:
    """Replay history with no future information, and score the forecasts (§30).

    One backtest per requested component. Every component is validated against
    the project first, so a backtest cannot be pointed at another tenant's
    component to measure it (§60).
    """
    await require_project(db, project_id)
    await require_environment(db, project_id, request.environment_id)
    if request.end_time <= request.start_time:
        raise HTTPException(status_code=422, detail="end_time must be after start_time")

    component_ids = list(request.component_ids or [])
    if component_ids:
        cap = request.max_components or min(len(component_ids), 10)
        if len(component_ids) > cap:
            raise HTTPException(
                status_code=422,
                detail=f"at most {cap} components may be backtested in one request",
            )
    else:
        component_ids = [None]  # type: ignore[list-item]

    horizon = _as_enum(request.forecast_horizon, ForecastHorizon)
    prediction_type = _as_enum(request.prediction_type, PredictionType)
    step = request.step_seconds or max(horizon.seconds, 3600)

    engine = BacktestEngine(db)
    out: list[BacktestResponse] = []
    for component_id in component_ids:
        if component_id is not None:
            await require_component(db, component_id, project_id=project_id)
        try:
            configuration = BacktestConfiguration(
                start_time=request.start_time,
                end_time=request.end_time,
                training_window_seconds=request.training_window_seconds,
                forecast_horizon=horizon,
                prediction_type=prediction_type,
                step_seconds=step,
                component_id=component_id,
                environment_id=request.environment_id,
                max_steps=request.max_steps,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        row = await engine.run(
            project_id=project_id,
            configuration=configuration,
            created_by=request.created_by,
        )
        out.append(_backtest_response(row))

    await db.commit()
    return BacktestRunResponse(
        items=out,
        total=len(out),
        note=(
            "a backtest replays stored history with time-based splits only; "
            "no future event can influence a historical forecast, and the "
            "result is a measurement of this baseline predictor, not a promise "
            "about production"
        ),
    )


def _backtest_response(row: ReliabilityBacktest) -> BacktestResponse:
    return BacktestResponse(
        id=row.id,
        project_id=row.project_id,
        status=row.status,
        configuration=dict(row.configuration or {}),
        start_time=row.start_time,
        end_time=row.end_time,
        training_window_seconds=row.training_window_seconds,
        forecast_horizon=row.forecast_horizon,
        prediction_type=row.prediction_type,
        evaluation_run_id=row.evaluation_run_id,
        metrics=dict(row.metrics or {}),
        sample_count=row.sample_count,
        error=row.error,
        created_by=row.created_by,
        created_at=row.created_at,
        steps=list(row.steps or []),
    )


@router.get(
    "/reliability/backtests",
    response_model=BacktestListResponse,
    summary="Backtest history",
)
async def list_backtests(
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    prediction_type: Optional[PredictionType] = Query(None),
    limit: int = Query(default=_LIST_LIMIT, ge=1, le=_LIST_LIMIT),
    db: AsyncSession = Depends(get_db),
) -> BacktestListResponse:
    await require_project(db, project_id)
    clauses: list[Any] = [ReliabilityBacktest.project_id == project_id]
    if prediction_type is not None:
        clauses.append(ReliabilityBacktest.prediction_type == prediction_type)
    rows = list(
        (
            await db.execute(
                select(ReliabilityBacktest)
                .where(*clauses)
                .order_by(ReliabilityBacktest.created_at.desc())
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    truncated = len(rows) > limit
    return BacktestListResponse(
        items=[_backtest_response(row) for row in rows[:limit]],
        total=min(len(rows), limit),
        truncated=truncated,
    )


@router.get(
    "/reliability/backtests/{backtest_id}",
    response_model=BacktestResponse,
    summary="One backtest",
)
async def get_backtest(
    backtest_id: uuid.UUID,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> BacktestResponse:
    await require_project(db, project_id)
    row = await _require_backtest(db, backtest_id, project_id)
    return _backtest_response(row)


# ---------------------------------------------------------------------------
# Health, drift, warnings (§39, §41–§43)
# ---------------------------------------------------------------------------


@router.get(
    "/reliability/health",
    response_model=PlatformHealthResponse,
    summary="Predictive reliability platform health",
)
async def reliability_health(
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> PlatformHealthResponse:
    """Forecast counts, accuracy, calibration, drift and warning pressure (§43)."""
    if project_id is not None:
        await require_project(db, project_id)

    service = ReliabilityForecastService(db)
    payload = await service.platform_health(**_health_scope(project_id))

    accuracy: dict = {}
    calibration: dict = {}
    latest = await PredictionEvaluationService(db).latest_evaluation(
        project_id=project_id
    )
    if latest is not None:
        accuracy = {
            "run_id": str(latest.id),
            "evaluated_at": latest.created_at.isoformat(),
            "window_start": latest.dataset_window_start.isoformat(),
            "window_end": latest.dataset_window_end.isoformat(),
            "sample_count": latest.sample_count,
            "positive_count": latest.positive_count,
            "negative_count": latest.negative_count,
            "inconclusive_count": latest.inconclusive_count,
            "status": latest.status.value,
            "metrics": dict(latest.metrics or {}),
            "notes": list(latest.notes or []),
        }
        calibration = {
            "status": latest.calibration_status.value,
            "detail": dict(latest.calibration or {}),
            "bands": list(latest.reliability_bands or []),
        }
    else:
        accuracy = {
            "sample_count": 0,
            "status": "NO_EVALUATION_RUN",
            "notes": [
                "no evaluation run exists yet; run POST /reliability/evaluate "
                "once forecast horizons have elapsed"
            ],
        }
        calibration = {
            "status": "UNKNOWN",
            "detail": {},
            "bands": [],
        }

    drift_records = await DriftMonitor(db).list_findings(
        project_id=project_id, limit=200
    )
    warning_rows = await EarlyWarningService(db).list_warnings(
        project_id=project_id, status=EarlyWarningStatus.OPEN, limit=200
    )
    models = list(
        (
            await db.execute(
                select(ReliabilityModelVersion).order_by(
                    ReliabilityModelVersion.created_at.desc()
                )
            )
        )
        .scalars()
        .all()
    )

    return PlatformHealthResponse(
        generated_at=datetime.now(timezone.utc),
        forecast_count=payload["forecast_count"],
        active_forecasts=payload["active_forecasts"],
        high_risk_forecasts=payload["high_risk_forecasts"],
        unknown_forecasts=payload["unknown_forecasts"],
        data_quality_distribution=payload["data_quality_distribution"],
        model_version_count=payload["model_version_count"],
        thresholds=payload["thresholds"],
        limits=payload["limits"],
        accuracy=accuracy,
        calibration=calibration,
        coverage={
            "forecasts_by_status": payload["data_quality_distribution"],
            "note": (
                "coverage counts change with telemetry volume; a drop in "
                "forecast count may be a data gap rather than an improvement"
            ),
        },
        drift=drift_summary(drift_records),
        warnings={
            "open": len(warning_rows),
            "highest_severity": (
                max(
                    (row.severity.value for row in warning_rows),
                    key=lambda name: risk_rank(ForecastRiskLevel(name)),
                )
                if warning_rows
                else None
            ),
            "note": (
                "warnings are deduplicated per component, type and horizon and "
                "are rate-limited by a cooldown (§39, §40)"
            ),
        },
        models=[
            {
                "id": str(row.id),
                "model_name": row.model_name,
                "version": row.version,
                "model_type": row.model_type.value,
                "status": row.status.value,
                "calibration_status": row.calibration_status.value,
                "sample_count": row.sample_count,
                "updated_at": row.updated_at.isoformat(),
            }
            for row in models[:50]
        ],
        notes=list(payload["notes"]),
        limitations=[
            "Phase 8 predicts, explains, evaluates and warns. It does not "
            "remediate, roll back, scale or deploy anything (§8, §87).",
            "risk levels are calibrated bands, not failure probabilities; a "
            "HIGH forecast is evidence of increasing risk, not a promise",
        ],
    )


@router.get(
    "/reliability/drift",
    response_model=DriftHistoryResponse,
    summary="Stored drift findings",
)
async def list_drift(
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    kind: Optional[DriftKind] = Query(None),
    requires_review: Optional[bool] = Query(None),
    limit: int = Query(default=_LIST_LIMIT, ge=1, le=_LIST_LIMIT),
    db: AsyncSession = Depends(get_db),
) -> DriftHistoryResponse:
    """Drift records are evidence requests for human review (§41, §42).

    This endpoint is read-only on purpose: a GET must never retrain, activate or
    retire a model. Assessment is an explicit POST.
    """
    await require_project(db, project_id)
    monitor = DriftMonitor(db)
    rows = await monitor.list_findings(
        project_id=project_id,
        kind=kind,
        requires_review=requires_review,
        limit=limit + 1,
    )
    truncated = len(rows) > limit
    rows = rows[:limit]
    return DriftHistoryResponse(
        summary=drift_summary(rows),
        items=[_drift_response(row) for row in rows],
        total=len(rows),
        truncated=truncated,
    )


def _drift_response(row: ReliabilityDriftRecord) -> Any:
    from app.schemas.reliability import DriftFindingResponse

    return DriftFindingResponse(
        id=row.id,
        project_id=row.project_id,
        environment_id=row.environment_id,
        component_id=row.component_id,
        model_version_id=row.model_version_id,
        kind=row.kind,
        status=row.status,
        feature_name=row.feature_name,
        drift_score=row.drift_score,
        threshold=row.threshold,
        description=row.description,
        requires_review=row.requires_review,
        created_at=row.created_at,
    )


@router.post(
    "/reliability/drift/assess",
    response_model=DriftReportResponse,
    summary="Run a drift assessment",
)
async def assess_drift(
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    environment_id: Optional[uuid.UUID] = Query(None),
    persist: bool = Query(True, description="Store the findings as records"),
    db: AsyncSession = Depends(get_db),
) -> DriftReportResponse:
    """Compare the current window against the reference window (§41, §42).

    The response states the review policy explicitly because the interesting
    guarantee is a negative one: a flagged finding requests human review and
    changes no model.
    """
    await require_project(db, project_id)
    await require_environment(db, project_id, environment_id)
    report = await DriftMonitor(db).run(
        project_id=project_id,
        environment_id=environment_id,
        persist=persist,
    )
    if persist:
        await db.commit()
    payload = report.as_dict()
    return DriftReportResponse(
        project_id=project_id,
        reference_window=[
            aware_utc(datetime.fromisoformat(moment))
            for moment in payload["reference_window"]
        ],
        current_window=[
            aware_utc(datetime.fromisoformat(moment))
            for moment in payload["current_window"]
        ],
        worst_status=payload["worst_status"],
        flagged_count=payload["flagged_count"],
        findings=payload["findings"],
        notes=list(payload["notes"]),
        review_policy=payload["review_policy"],
    )


@router.get(
    "/reliability/warnings",
    response_model=EarlyWarningListResponse,
    summary="Early warnings",
)
async def list_warnings(
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    status: Optional[EarlyWarningStatus] = Query(None),
    minimum_severity: Optional[ForecastRiskLevel] = Query(None),
    limit: int = Query(default=_LIST_LIMIT, ge=1, le=_LIST_LIMIT),
    db: AsyncSession = Depends(get_db),
) -> EarlyWarningListResponse:
    await require_project(db, project_id)
    rows = await EarlyWarningService(db).list_warnings(
        project_id=project_id,
        status=status,
        minimum_severity=minimum_severity,
        limit=limit,
    )
    return EarlyWarningListResponse(
        items=[_warning_response(row) for row in rows],
        total=len(rows),
        truncated=False,
    )


def _warning_response(row: ReliabilityEarlyWarning) -> EarlyWarningResponse:
    return EarlyWarningResponse(
        id=row.id,
        project_id=row.project_id,
        environment_id=row.environment_id,
        component_id=row.component_id,
        forecast_id=row.forecast_id,
        fingerprint=row.fingerprint,
        title=row.title,
        description=row.description,
        severity=row.severity,
        status=row.status,
        occurrence_count=row.occurrence_count,
        first_raised_at=row.first_raised_at,
        last_raised_at=row.last_raised_at,
        last_suppressed_at=row.last_suppressed_at,
        acknowledged_at=row.acknowledged_at,
        acknowledged_by=row.acknowledged_by,
    )


@router.post(
    "/reliability/warnings/{warning_id}/acknowledge",
    response_model=EarlyWarningResponse,
    summary="Acknowledge a warning",
)
async def acknowledge_warning(
    warning_id: uuid.UUID,
    request: WarningActionRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> EarlyWarningResponse:
    """A human takes responsibility for a warning. Nothing else happens (§8)."""
    await require_project(db, project_id)
    await _require_warning(db, warning_id, project_id)
    service = EarlyWarningService(db)
    row = await service.acknowledge(warning_id=warning_id, actor=request.actor)
    if row is None:
        raise HTTPException(status_code=404, detail="Warning not found")
    await db.commit()
    return _warning_response(row)


@router.post(
    "/reliability/warnings/{warning_id}/dismiss",
    response_model=EarlyWarningResponse,
    summary="Dismiss a warning",
)
async def dismiss_warning(
    warning_id: uuid.UUID,
    request: WarningActionRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> EarlyWarningResponse:
    """A human judges the warning not actionable. The forecast is untouched."""
    await require_project(db, project_id)
    await _require_warning(db, warning_id, project_id)
    service = EarlyWarningService(db)
    row = await service.dismiss(
        warning_id=warning_id, actor=request.actor, reason=request.reason
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Warning not found")
    await db.commit()
    return _warning_response(row)
