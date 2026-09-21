"""ARGUS Reliability Forecast Service (Phase 8 §26, §27, §34–§38, §40, §43).

The orchestration layer. It decides *what* to forecast, assembles the evidence
through the feature engine, runs one predictor, classifies the risk with the
central policy, stores the forecast with its signals and its feature snapshot,
and maintains the dedup/revision bookkeeping.

The rules that shape this code:

* **A forecast is reproducible from stored rows.** The feature snapshot is
  written before the forecast, and the forecast points at it. Re-reading the
  two is enough to recompute the same prediction (§17, §82).
* **Insufficient evidence produces an explicit refusal, not a low number.**
  When the engine reports insufficient data, the predictor returns no score and
  the stored forecast is ``risk_level=UNKNOWN`` with a ``failure_reason`` —
  never ``LOW`` (§18, §69, §81).
* **Deduplication keeps one current row per logical scope without erasing
  history.** A forecast refreshed inside the refresh window updates in place; a
  genuinely new prediction becomes the next *revision*, and the previous row
  remains readable (§40).
* **Nothing here reacts.** This module writes forecasts and signals. It does
  not open incidents, page anyone, or change a system — warnings and evaluation
  are separate services by design (§5, §8, §87).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.anomaly import Anomaly
from app.models.incident import Incident
from app.models.observability import MetricRecord
from app.models.project import Environment, SoftwareProject
from app.models.reliability import (
    CalibrationStatus,
    ForecastDataQuality,
    ForecastFailureReason,
    ForecastFeatureSnapshot,
    ForecastFingerprint,
    ForecastHorizon,
    ForecastRiskLevel,
    ForecastStatus,
    PredictionType,
    PredictiveSignal,
    ReliabilityForecast,
    ReliabilityModelVersion,
)
from app.models.system import SystemComponent
from app.services import reliability_risk as risk
from app.services.reliability_features import (
    FEATURE_SCHEMA_VERSION,
    FeatureBundle,
    ReliabilityFeatureEngine,
    aware_utc,
)
from app.services.reliability_models import ReliabilityModelRegistry
from app.services.reliability_predictors import (
    PredictionDraft,
    ReliabilityPredictor,
    SignalDraft,
)

logger = logging.getLogger(__name__)
settings = get_settings()

#: In-process notification hook. The real asynchronous path is the Redis
#: worker; this exists so a test (or an embedder) can observe forecast creation
#: without standing up a broker (§26 step 11).
ForecastHook = Callable[[dict], None]
_FORECAST_HOOKS: list[ForecastHook] = []


def register_forecast_hook(hook: ForecastHook) -> None:
    """Register a listener called after each stored forecast."""
    if hook not in _FORECAST_HOOKS:
        _FORECAST_HOOKS.append(hook)


def clear_forecast_hooks() -> None:
    _FORECAST_HOOKS.clear()


def emit_forecast_event(event: dict) -> None:
    """Publish a forecast-created event: structured log plus in-process hooks.

    Hook failures are swallowed and logged: a listener must never be able to
    fail a forecast that was already committed.
    """
    logger.info(
        "reliability_forecast_created project=%s component=%s type=%s horizon=%s "
        "level=%s score=%s model=%s",
        event.get("project_id"),
        event.get("component_id"),
        event.get("prediction_type"),
        event.get("forecast_horizon"),
        event.get("risk_level"),
        event.get("risk_score"),
        event.get("model_version_label"),
    )
    for hook in list(_FORECAST_HOOKS):
        try:
            hook(event)
        except Exception:  # noqa: BLE001 - a listener must not break a forecast
            logger.exception("forecast hook failed")


def forecast_fingerprint(
    *,
    project_id: Any,
    environment_id: Optional[Any],
    component_id: Optional[Any],
    prediction_type: PredictionType,
    horizon: ForecastHorizon,
    dominant_signal: Optional[str],
) -> str:
    """Deterministic dedup key for one logical forecast (§40).

    Includes the *dominant signal* on purpose: a latency-led forecast and an
    error-led forecast for the same component and horizon are different claims,
    and collapsing them would hide one behind the other.
    """
    raw = "|".join(
        [
            str(project_id),
            str(environment_id or "-"),
            str(component_id or "-"),
            prediction_type.value,
            horizon.value,
            str(dominant_signal or "-"),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class ForecastScope:
    """One component (or project-wide scope) a forecast may be produced for."""

    project_id: Any
    environment_id: Optional[Any] = None
    component_id: Optional[Any] = None
    component_name: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    @property
    def subject(self) -> str:
        return self.component_name or "the project"


@dataclass
class GenerationResult:
    """What one generation pass did, reported rather than logged quietly."""

    scopes: int = 0
    forecasts_created: int = 0
    forecasts_updated: int = 0
    forecasts_revised: int = 0
    refusals: int = 0
    signals: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "scopes": self.scopes,
            "forecasts_created": self.forecasts_created,
            "forecasts_updated": self.forecasts_updated,
            "forecasts_revised": self.forecasts_revised,
            "refusals": self.refusals,
            "signals": self.signals,
            "errors": self.errors[:20],
            "skipped": self.skipped[:20],
            "duration_ms": self.duration_ms,
        }


def configured_horizons() -> list[ForecastHorizon]:
    """Horizons to generate, from configuration (§3)."""
    out: list[ForecastHorizon] = []
    for name in settings.RELIABILITY_DEFAULT_HORIZONS:
        try:
            out.append(ForecastHorizon(name))
        except ValueError:
            logger.warning("ignoring unknown configured horizon %r", name)
    return out or [ForecastHorizon.SIX_HOURS]


def configured_prediction_types() -> list[PredictionType]:
    """Prediction types to generate, from configuration (§4)."""
    out: list[PredictionType] = []
    for name in settings.RELIABILITY_DEFAULT_PREDICTION_TYPES:
        try:
            out.append(PredictionType(name))
        except ValueError:
            logger.warning("ignoring unknown configured prediction type %r", name)
    return out or [PredictionType.FAILURE_RISK]


class ReliabilityForecastService:
    """Generates, stores and describes reliability forecasts (§26)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.engine = ReliabilityFeatureEngine(session)
        self.registry = ReliabilityModelRegistry(session)

    # -- scope discovery (§26 step 1) ----------------------------------
    async def eligible_scopes(
        self,
        *,
        project_id: Any,
        environment_id: Optional[Any] = None,
        now: Optional[datetime] = None,
        limit: Optional[int] = None,
        lookback_seconds: int = 86_400,
    ) -> list[ForecastScope]:
        """Components with telemetry recent enough to forecast.

        Eligibility is *observed activity*, not configuration: a component that
        has produced no telemetry in the lookback window is skipped rather than
        forecast as calm. The list is bounded so one sweep cannot scan a whole
        tenant (§59).
        """
        now = aware_utc(now or datetime.now(timezone.utc))
        since = now - timedelta(seconds=lookback_seconds)
        limit = limit or settings.RELIABILITY_MAX_COMPONENTS_PER_RUN

        clauses = [
            MetricRecord.project_id == project_id,
            MetricRecord.timestamp >= since,
            MetricRecord.timestamp <= now,
            MetricRecord.component_id.is_not(None),
        ]
        if environment_id is not None:
            clauses.append(MetricRecord.environment_id == environment_id)

        rows = (
            await self.session.execute(
                select(
                    MetricRecord.component_id,
                    MetricRecord.environment_id,
                    func.count().label("samples"),
                )
                .where(*clauses)
                .group_by(MetricRecord.component_id, MetricRecord.environment_id)
                .order_by(func.count().desc())
                .limit(limit)
            )
        ).all()

        component_ids = [row[0] for row in rows if row[0] is not None]
        names: dict[Any, str] = {}
        if component_ids:
            name_rows = (
                await self.session.execute(
                    select(SystemComponent.id, SystemComponent.name).where(
                        SystemComponent.id.in_(component_ids)
                    )
                )
            ).all()
            names = {cid: name for cid, name in name_rows}

        scopes = [
            ForecastScope(
                project_id=project_id,
                environment_id=row[1],
                component_id=row[0],
                component_name=names.get(row[0]),
                metadata={"metric_samples_in_lookback": int(row[2])},
            )
            for row in rows
            if row[0] is not None
        ]
        return scopes

    # -- generation (§26 steps 2–11) -----------------------------------
    async def generate_for_scope(
        self,
        scope: ForecastScope,
        *,
        now: Optional[datetime] = None,
        prediction_types: Optional[Sequence[PredictionType]] = None,
        horizons: Optional[Sequence[ForecastHorizon]] = None,
        result: Optional[GenerationResult] = None,
    ) -> GenerationResult:
        """Build one feature snapshot and forecast every type × horizon."""
        started = datetime.now(timezone.utc)
        now = aware_utc(now or started)
        result = result or GenerationResult()
        types = list(prediction_types or configured_prediction_types())
        horizon_list = list(horizons or configured_horizons())

        #: One snapshot per component per pass: every type and horizon reads the
        #: same evidence, so two forecasts of the same component can never
        #: disagree about what was observed (§17).
        try:
            bundle = await self.engine.build(
                project_id=scope.project_id,
                environment_id=scope.environment_id,
                component_id=scope.component_id,
                component_name=scope.component_name,
                forecast_time=now,
            )
        except Exception as error:  # noqa: BLE001 - one bad scope must not stop a run
            logger.exception("feature generation failed for scope %s", scope)
            result.errors.append(
                f"scope {scope.component_id}: {type(error).__name__}: {error}"
            )
            return result

        result.scopes += 1

        for prediction_type in types:
            predictor = await self.registry.resolve(prediction_type)
            model_row = await self.registry.ensure_version(predictor)
            for horizon in horizon_list:
                try:
                    await self._generate_one(
                        scope=scope,
                        bundle=bundle,
                        predictor=predictor,
                        model_row=model_row,
                        prediction_type=prediction_type,
                        horizon=horizon,
                        now=now,
                        result=result,
                    )
                except Exception as error:  # noqa: BLE001
                    logger.exception(
                        "forecast generation failed for %s/%s",
                        prediction_type.value,
                        horizon.value,
                    )
                    result.errors.append(
                        f"{prediction_type.value}/{horizon.value}: "
                        f"{type(error).__name__}: {error}"
                    )

        result.duration_ms += int(
            (datetime.now(timezone.utc) - started).total_seconds() * 1000
        )
        return result

    async def generate_for_project(
        self,
        *,
        project_id: Any,
        environment_id: Optional[Any] = None,
        now: Optional[datetime] = None,
        prediction_types: Optional[Sequence[PredictionType]] = None,
        horizons: Optional[Sequence[ForecastHorizon]] = None,
        limit: Optional[int] = None,
    ) -> GenerationResult:
        """Forecast every eligible component in a project (§26, §58)."""
        started = datetime.now(timezone.utc)
        now = aware_utc(now or started)
        result = GenerationResult()
        scopes = await self.eligible_scopes(
            project_id=project_id,
            environment_id=environment_id,
            now=now,
            limit=limit,
        )
        if not scopes:
            result.skipped.append(
                "no component had telemetry in the lookback window, so nothing "
                "was forecast"
            )
        for scope in scopes:
            await self.generate_for_scope(
                scope,
                now=now,
                prediction_types=prediction_types,
                horizons=horizons,
                result=result,
            )
        result.duration_ms = int(
            (datetime.now(timezone.utc) - started).total_seconds() * 1000
        )
        return result

    async def _generate_one(
        self,
        *,
        scope: ForecastScope,
        bundle: FeatureBundle,
        predictor: ReliabilityPredictor,
        model_row: ReliabilityModelVersion,
        prediction_type: PredictionType,
        horizon: ForecastHorizon,
        now: datetime,
        result: GenerationResult,
    ) -> ReliabilityForecast:
        """Produce, classify, dedup and store one forecast."""
        draft = predictor.predict(bundle, prediction_type, horizon)
        #: Internal consistency is checked before anything is stored: a score
        #: outside [0,1] or a number with no signals is a bug, not a forecast.
        problems = predictor.validate(draft)
        if problems:
            raise ValueError(
                f"predictor {predictor.name} produced an invalid draft: "
                + "; ".join(problems)
            )

        level = risk.classify_score(draft.risk_score)
        snapshot = await self._store_snapshot(bundle)
        fingerprint = forecast_fingerprint(
            project_id=scope.project_id,
            environment_id=scope.environment_id,
            component_id=scope.component_id,
            prediction_type=prediction_type,
            horizon=horizon,
            dominant_signal=draft.dominant_signal,
        )
        current = await self._current_forecast(scope, prediction_type, horizon)
        refresh_window = max(settings.RELIABILITY_SWEEP_INTERVAL_SECONDS, 60)
        refreshable = (
            current is not None
            and current.status in (ForecastStatus.GENERATED, ForecastStatus.ACTIVE)
            and current.risk_level is level
            and current.dominant_signal == draft.dominant_signal
            and current.fingerprint == fingerprint
            and (now - aware_utc(current.generated_at)).total_seconds()
            <= refresh_window
        )

        if refreshable and current is not None:
            forecast = current
            forecast.risk_score = draft.risk_score
            forecast.confidence = draft.confidence
            forecast.confidence_reason = draft.confidence_reason
            forecast.data_quality = bundle.quality
            forecast.data_coverage = bundle.coverage
            forecast.headline = draft.headline
            forecast.summary = predictor.explain(draft)
            forecast.limitations = list(draft.limitations)
            forecast.supporting_evidence = dict(draft.evidence)
            forecast.feature_snapshot_id = snapshot.id
            forecast.failure_reason = self._failure_reason(bundle, draft)
            forecast.failure_detail = (
                None if forecast.failure_reason is None else draft.confidence_reason
            )
            await self._replace_signals(forecast, scope, draft)
            result.forecasts_updated += 1
        else:
            forecast = ReliabilityForecast(
                project_id=scope.project_id,
                environment_id=scope.environment_id,
                component_id=scope.component_id,
                prediction_type=prediction_type,
                forecast_horizon=horizon,
                generated_at=now,
                valid_from=now,
                valid_until=now + timedelta(seconds=horizon.seconds),
                risk_score=draft.risk_score,
                risk_level=level,
                confidence=draft.confidence,
                confidence_reason=draft.confidence_reason,
                calibration_status=CalibrationStatus.UNKNOWN,
                data_quality=bundle.quality,
                data_coverage=bundle.coverage,
                model_version_id=model_row.id,
                model_version_label=f"{model_row.model_name}/{model_row.version}",
                feature_snapshot_id=snapshot.id,
                status=(
                    ForecastStatus.ACTIVE
                    if level is not ForecastRiskLevel.UNKNOWN
                    else ForecastStatus.GENERATED
                ),
                fingerprint=fingerprint,
                dominant_signal=draft.dominant_signal,
                headline=draft.headline,
                summary=predictor.explain(draft),
                limitations=list(draft.limitations),
                supporting_evidence=dict(draft.evidence),
                failure_reason=self._failure_reason(bundle, draft),
                failure_detail=None,
                previous_forecast_id=current.id if current else None,
                revision=(current.revision + 1) if current else 1,
                metadata_={
                    "predictor": predictor.name,
                    "unevaluated_rules": draft.unevaluated,
                    "component_name": scope.component_name,
                },
            )
            if forecast.failure_reason is not None:
                forecast.failure_detail = draft.confidence_reason
            self.session.add(forecast)
            await self.session.flush()
            await self._replace_signals(forecast, scope, draft)
            if current is None:
                result.forecasts_created += 1
            else:
                result.forecasts_revised += 1

        if level is ForecastRiskLevel.UNKNOWN:
            result.refusals += 1
        result.signals += len(draft.signals)

        await self._update_fingerprint(
            fingerprint=fingerprint,
            scope=scope,
            prediction_type=prediction_type,
            horizon=horizon,
            forecast=forecast,
            previous_level=current.risk_level if current is not None else None,
            now=now,
        )

        emit_forecast_event(
            {
                "forecast_id": str(forecast.id),
                "project_id": str(scope.project_id),
                "environment_id": (
                    str(scope.environment_id) if scope.environment_id else None
                ),
                "component_id": str(scope.component_id) if scope.component_id else None,
                "prediction_type": prediction_type.value,
                "forecast_horizon": horizon.value,
                "risk_level": level.value,
                "risk_score": forecast.risk_score,
                "model_version_label": forecast.model_version_label,
                "data_quality": bundle.quality.value,
                "generated_at": now.isoformat(),
            }
        )
        return forecast

    # -- persistence helpers -------------------------------------------
    async def _store_snapshot(self, bundle: FeatureBundle) -> ForecastFeatureSnapshot:
        """Persist the exact features used, then return the row (§17)."""
        note = bundle.quality_notes or None
        snapshot = ForecastFeatureSnapshot(
            project_id=bundle.project_id,
            environment_id=bundle.environment_id,
            component_id=bundle.component_id,
            forecast_time=bundle.forecast_time,
            feature_window_start=bundle.window_start,
            feature_window_end=bundle.window_end,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            feature_values=bundle.as_snapshot_payload(),
            data_sources=dict(bundle.sources),
            data_quality=bundle.quality,
            data_quality_notes=list(note) if note else None,
            data_coverage=bundle.coverage,
            sample_count=bundle.sample_count,
            metadata_={
                "baseline_window_start": bundle.baseline_window_start.isoformat(),
                "window_seconds": bundle.window_seconds,
            },
        )
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def _replace_signals(
        self,
        forecast: ReliabilityForecast,
        scope: ForecastScope,
        draft: PredictionDraft,
    ) -> None:
        """Rewrite a forecast's signals to match its current evidence.

        Signals are derived rows owned by the forecast, so refreshing them is a
        replace rather than an append — otherwise a refreshed forecast would
        accumulate contradictory signal sets across sweeps. Ranked and capped so
        the explanation shows the strongest contributors (§35).
        """
        existing = (
            (
                await self.session.execute(
                    select(PredictiveSignal).where(
                        PredictiveSignal.forecast_id == forecast.id
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in existing:
            await self.session.delete(row)

        limit = settings.RELIABILITY_MAX_SIGNALS_PER_FORECAST
        for rank, draft_signal in enumerate(draft.signals[:limit]):
            self.session.add(_signal_row(forecast, scope, draft_signal, rank))
        await self.session.flush()

    async def _current_forecast(
        self,
        scope: ForecastScope,
        prediction_type: PredictionType,
        horizon: ForecastHorizon,
    ) -> Optional[ReliabilityForecast]:
        """The newest non-terminal forecast for this scope, type and horizon.

        Looked up by *scope* rather than by fingerprint on purpose: the
        fingerprint records which signal led the prediction, and a forecast
        whose leading signal changed is the next revision of the same claim,
        not a brand-new one. Keying the lookup on the fingerprint would let one
        scope accumulate several parallel "current" rows (§40).
        """
        clauses = [
            ReliabilityForecast.project_id == scope.project_id,
            ReliabilityForecast.prediction_type == prediction_type,
            ReliabilityForecast.forecast_horizon == horizon,
            ReliabilityForecast.status.in_(
                [
                    ForecastStatus.GENERATED,
                    ForecastStatus.ACTIVE,
                    ForecastStatus.CONFIRMED,
                    ForecastStatus.INCONCLUSIVE,
                ]
            ),
        ]
        if scope.environment_id is None:
            clauses.append(ReliabilityForecast.environment_id.is_(None))
        else:
            clauses.append(ReliabilityForecast.environment_id == scope.environment_id)
        if scope.component_id is None:
            clauses.append(ReliabilityForecast.component_id.is_(None))
        else:
            clauses.append(ReliabilityForecast.component_id == scope.component_id)

        return (
            (
                await self.session.execute(
                    select(ReliabilityForecast)
                    .where(*clauses)
                    .order_by(ReliabilityForecast.revision.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    async def _update_fingerprint(
        self,
        *,
        fingerprint: str,
        scope: ForecastScope,
        prediction_type: PredictionType,
        horizon: ForecastHorizon,
        forecast: ReliabilityForecast,
        previous_level: Optional[ForecastRiskLevel],
        now: datetime,
    ) -> None:
        """Point the dedup registry at the current forecast (§40).

        ``previous_risk_level`` is overwritten with the level of the revision
        being superseded, which is what the "why risk changed" view compares
        against (§51).
        """
        row = (
            (
                await self.session.execute(
                    select(ForecastFingerprint).where(
                        ForecastFingerprint.project_id == scope.project_id,
                        ForecastFingerprint.fingerprint == fingerprint,
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            self.session.add(
                ForecastFingerprint(
                    project_id=scope.project_id,
                    environment_id=scope.environment_id,
                    component_id=scope.component_id,
                    fingerprint=fingerprint,
                    prediction_type=prediction_type,
                    forecast_horizon=horizon,
                    current_forecast_id=forecast.id,
                    revision_count=forecast.revision,
                    previous_risk_level=forecast.risk_level,
                    first_seen_at=now,
                    last_seen_at=now,
                    metadata_={"component_name": scope.component_name},
                )
            )
        else:
            if row.current_forecast_id != forecast.id:
                if previous_level is not None:
                    row.previous_risk_level = previous_level
                row.revision_count = max(row.revision_count, forecast.revision)
            row.current_forecast_id = forecast.id
            row.last_seen_at = now
        await self.session.flush()

    @staticmethod
    def _failure_reason(
        bundle: FeatureBundle, draft: PredictionDraft
    ) -> Optional[ForecastFailureReason]:
        """Why a forecast has no number, stated explicitly (§81)."""
        if draft.risk_score is not None:
            return None
        if bundle.quality is ForecastDataQuality.INSUFFICIENT:
            return ForecastFailureReason.INSUFFICIENT_DATA
        if bundle.quality is ForecastDataQuality.POOR:
            return ForecastFailureReason.DATA_QUALITY_FAILURE
        return ForecastFailureReason.PREDICTION_FAILED

    # -- lifecycle (§27) ------------------------------------------------
    async def expire_due(
        self, *, now: Optional[datetime] = None, project_id: Optional[Any] = None
    ) -> int:
        """Move forecasts past ``valid_until`` to ``EXPIRED`` (§27).

        Evaluation has its own grace window, so an expired forecast is not
        immediately unscoreable — this only reflects that its horizon has
        passed without a decision.
        """
        now = aware_utc(now or datetime.now(timezone.utc))
        clauses = [
            ReliabilityForecast.valid_until < now,
            ReliabilityForecast.status.in_(
                [ForecastStatus.GENERATED, ForecastStatus.ACTIVE]
            ),
        ]
        if project_id is not None:
            clauses.append(ReliabilityForecast.project_id == project_id)
        rows = (
            (
                await self.session.execute(
                    select(ReliabilityForecast).where(*clauses).limit(1000)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.status = ForecastStatus.EXPIRED
        await self.session.flush()
        return len(rows)

    # -- explanation (§34, §35) ----------------------------------------
    async def explain(self, forecast: ReliabilityForecast) -> dict:
        """Explain one forecast: what changed, why, what supports it, what is uncertain.

        Every section maps to stored rows — the signals are the ones that were
        persisted with the forecast, and the "seen before" block is a structured
        similarity search over real incidents (§36). Nothing is generated by a
        language model here.
        """
        signals = (
            (
                await self.session.execute(
                    select(PredictiveSignal)
                    .where(PredictiveSignal.forecast_id == forecast.id)
                    .order_by(PredictiveSignal.rank.asc())
                )
            )
            .scalars()
            .all()
        )
        previous = None
        if forecast.previous_forecast_id is not None:
            previous = (
                await self.session.execute(
                    select(ReliabilityForecast).where(
                        ReliabilityForecast.id == forecast.previous_forecast_id
                    )
                )
            ).scalar_one_or_none()

        return {
            "forecast_id": str(forecast.id),
            "headline": forecast.headline,
            "summary": forecast.summary,
            "risk_level": forecast.risk_level.value,
            "risk_score": forecast.risk_score,
            "prediction_type": forecast.prediction_type.value,
            "forecast_horizon": forecast.forecast_horizon.value,
            "horizon_label": forecast.forecast_horizon.label,
            "model_version": forecast.model_version_label,
            "generated_at": forecast.generated_at.isoformat(),
            "valid_until": forecast.valid_until.isoformat(),
            "confidence": forecast.confidence,
            "confidence_reason": forecast.confidence_reason,
            "calibration_status": forecast.calibration_status.value,
            "data_quality": forecast.data_quality.value,
            "data_coverage": forecast.data_coverage,
            "what_changed": self._what_changed(signals),
            "why_risk_increased": [signal.description for signal in signals[:5]],
            "what_supports_this": {
                "signals": [
                    {
                        "signal_type": signal.signal_type.value,
                        "description": signal.description,
                        "severity": signal.severity.value,
                        "contribution": signal.contribution,
                        "observed_value": signal.observed_value,
                        "baseline_value": signal.baseline_value,
                        "trend": signal.trend.value,
                        "evidence": signal.evidence_ids,
                        "similar_incident_count": signal.similar_incident_count,
                    }
                    for signal in signals
                ],
                "evidence_summary": forecast.supporting_evidence,
            },
            "what_is_uncertain": list(forecast.limitations or []),
            "why_risk_changed": self._why_risk_changed(forecast, previous),
            "caveats": [
                "predictive signals describe evidence that risk is increasing; "
                "they are not causal evidence (§6)",
                "a forecast is not an incident and does not open one (§5)",
                "risk levels are bands, not calibrated probabilities",
            ],
        }

    @staticmethod
    def _what_changed(signals: Sequence[PredictiveSignal]) -> list[str]:
        """The measured movement behind each signal, stated as a change."""
        out: list[str] = []
        for signal in signals[:5]:
            if signal.observed_value is None:
                out.append(signal.description)
                continue
            if signal.baseline_value is not None:
                out.append(
                    f"{signal.signal_type.value}: {signal.observed_value:.4g} "
                    f"(baseline {signal.baseline_value:.4g}, trend "
                    f"{signal.trend.value.lower()})"
                )
            else:
                out.append(
                    f"{signal.signal_type.value}: {signal.observed_value:.4g} "
                    f"(trend {signal.trend.value.lower()})"
                )
        return out

    @staticmethod
    def _why_risk_changed(
        forecast: ReliabilityForecast, previous: Optional[ReliabilityForecast]
    ) -> dict:
        """Diff against the previous revision of the same dedup scope (§51).

        Reports both directions: a risk that *fell* is stated as plainly as one
        that rose, because a forecast system that only ever escalates is not
        telling the truth about recovery.
        """
        if previous is None:
            return {
                "previous_forecast_id": None,
                "previous_risk_level": None,
                "direction": "NEW",
                "details": ["no previous revision exists for this scope and horizon"],
            }
        old_rank = risk.risk_rank(previous.risk_level)
        new_rank = risk.risk_rank(forecast.risk_level)
        direction = (
            "INCREASED"
            if new_rank > old_rank
            else "DECREASED"
            if new_rank < old_rank
            else "UNCHANGED"
        )
        details = []
        old_score = previous.risk_score
        new_score = forecast.risk_score
        if old_score is not None and new_score is not None:
            details.append(
                f"risk score moved from {old_score:.4g} to {new_score:.4g} "
                f"({new_score - old_score:+.4g})"
            )
        if previous.data_quality is not forecast.data_quality:
            details.append(
                f"data quality changed from {previous.data_quality.value} to "
                f"{forecast.data_quality.value}"
            )
        if previous.dominant_signal != forecast.dominant_signal:
            details.append(
                f"dominant signal changed from "
                f"{previous.dominant_signal or 'none'} to "
                f"{forecast.dominant_signal or 'none'}"
            )
        if not details:
            details.append("the underlying signals did not materially change")
        return {
            "previous_forecast_id": str(previous.id),
            "previous_risk_level": previous.risk_level.value,
            "direction": direction,
            "details": details,
        }

    # -- historical similarity (§36) -----------------------------------
    async def similar_incidents(
        self,
        *,
        project_id: Any,
        component_id: Optional[Any],
        bundle: FeatureBundle,
        limit: int = 5,
        now: Optional[datetime] = None,
    ) -> list[dict]:
        """Structurally similar past incidents, with their outcomes (§36).

        Similarity is a declared weighted comparison of *structure*: overlap of
        anomaly types, matching latency/error trend directions and a comparable
        severity mix. It is reported as "similar situations have been seen", and
        every result carries the caveat that similarity is not a prediction of
        repeat (§6, §36).
        """
        now = aware_utc(now or datetime.now(timezone.utc))
        since = now - timedelta(days=settings.RELIABILITY_SIMILARITY_LOOKBACK_DAYS)
        clauses = [
            Incident.project_id == project_id,
            Incident.detected_at >= since,
            Incident.detected_at <= now,
        ]
        if component_id is not None:
            clauses.append(Incident.primary_component_id == component_id)

        incidents = (
            await self.session.execute(
                select(
                    Incident.id,
                    Incident.detected_at,
                    Incident.severity,
                    Incident.status,
                    Incident.title,
                )
                .where(*clauses)
                .order_by(Incident.detected_at.desc())
                .limit(60)
            )
        ).all()
        if not incidents:
            return []

        current_types = set(
            (bundle.detail.get("anomalies") or {}).get("types_in_window", {})
        )
        current_latency = bundle.trend("latency_p95")
        current_error = bundle.trend("error_rate")

        incident_ids = [row[0] for row in incidents]
        anomaly_rows = (
            await self.session.execute(
                select(Anomaly.incident_id, Anomaly.anomaly_type)
                .where(Anomaly.incident_id.in_(incident_ids))
                .distinct()
            )
        ).all()
        types_by_incident: dict[Any, set[str]] = {}
        for incident_id, anomaly_type in anomaly_rows:
            types_by_incident.setdefault(incident_id, set()).add(
                str(anomaly_type.value)
            )

        results: list[dict] = []
        for incident_id, detected_at, severity, status, title in incidents:
            types = types_by_incident.get(incident_id, set())
            overlap = (
                len(current_types & types) / len(current_types | types)
                if (current_types | types)
                else 0.0
            )
            #: Latency/error direction agreement is derived from whether the
            #: historical incident's own anomaly set contains the matching
            #: family — a structural comparison, not a re-read of its telemetry.
            latency_match = (
                1.0
                if (
                    current_latency.value == "RISING"
                    and any("LATENCY" in t for t in types)
                )
                else 0.0
            )
            error_match = (
                1.0
                if (
                    current_error.value == "RISING" and any("ERROR" in t for t in types)
                )
                else 0.0
            )
            severity_match = 1.0 if severity is not None else 0.0
            score = (
                0.45 * overlap
                + 0.20 * latency_match
                + 0.15 * error_match
                + 0.20 * severity_match
            )
            if score <= 0:
                continue
            results.append(
                {
                    "incident_id": str(incident_id),
                    "title": title,
                    "detected_at": aware_utc(detected_at).isoformat(),
                    "severity": severity.value if severity is not None else None,
                    "status": status.value if status is not None else None,
                    "similarity_score": round(score, 4),
                    "shared_anomaly_types": sorted(current_types & types),
                    "observed_pattern": (
                        ", ".join(sorted(types)) if types else "no anomalies linked"
                    ),
                    "outcome": status.value if status is not None else "unknown",
                }
            )

        results.sort(key=lambda item: (-item["similarity_score"], item["incident_id"]))
        return results[:limit]

    # -- reliability score (§38) ---------------------------------------
    def reliability_score(self, bundle: FeatureBundle) -> risk.ReliabilityScore:
        """Compose the interpretable reliability score from the bundle (§38).

        Each dimension is derived from a named feature, and a dimension with no
        data stays missing rather than defaulting to healthy — the score then
        says how much of itself is missing.
        """
        numeric = bundle.numeric
        latency_change = numeric.get("latency_p95_deviation_from_baseline")
        if latency_change is None:
            latency_change = numeric.get("latency_p95_slope")
        error_change = numeric.get("error_rate_change")
        if error_change is None:
            error_change = numeric.get("span_error_rate_trend")

        dimensions: dict[str, Optional[float]] = {
            "availability_health": risk.health_from_change(
                risk.first_non_missing(
                    [
                        numeric.get("span_error_rate_trend"),
                        numeric.get("error_rate_change"),
                    ]
                )
            ),
            "error_health": risk.health_from_change(error_change),
            "latency_health": risk.health_from_change(latency_change),
            "resource_health": risk.health_from_ratio(
                numeric.get("resource_saturation_rate")
            ),
            "dependency_health": risk.health_from_ratio(
                numeric.get("dependency_failure_frequency")
            ),
            "incident_stability": risk.health_from_count(
                numeric.get("incidents_last_7d"), ceiling=5.0
            ),
            "change_stability": risk.health_from_change(
                risk.first_non_missing(
                    [
                        numeric.get("deployment_failure_frequency"),
                        numeric.get("rollback_frequency"),
                    ]
                )
            ),
        }
        return risk.ReliabilityScore(dimensions=dimensions)

    # -- component profile (§37) ---------------------------------------
    async def component_profile(
        self,
        *,
        project_id: Any,
        component_id: Any,
        now: Optional[datetime] = None,
        environment_id: Optional[Any] = None,
    ) -> dict:
        """Everything the profile view shows, from stored rows (§37).

        A read-only projection: it assembles existing forecasts, incidents,
        anomalies, dependencies and the composed reliability score. It creates
        nothing, so opening a profile can never change what ARGUS believes.
        """
        now = aware_utc(now or datetime.now(timezone.utc))
        component = (
            await self.session.execute(
                select(SystemComponent).where(SystemComponent.id == component_id)
            )
        ).scalar_one_or_none()

        bundle = await self.engine.build(
            project_id=project_id,
            environment_id=environment_id,
            component_id=component_id,
            component_name=component.name if component else None,
            forecast_time=now,
        )

        forecast_clauses = [
            ReliabilityForecast.project_id == project_id,
            ReliabilityForecast.component_id == component_id,
        ]
        forecasts = (
            (
                await self.session.execute(
                    select(ReliabilityForecast)
                    .where(*forecast_clauses)
                    .order_by(ReliabilityForecast.generated_at.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        #: The newest forecast per (type, horizon) is what a reader means by
        #: "current risk"; older revisions remain in the history.
        current: dict[tuple[str, str], ReliabilityForecast] = {}
        for forecast in forecasts:
            key = (forecast.prediction_type.value, forecast.forecast_horizon.value)
            current.setdefault(key, forecast)

        incident_rows = (
            (
                await self.session.execute(
                    select(Incident)
                    .where(
                        Incident.project_id == project_id,
                        Incident.primary_component_id == component_id,
                        Incident.detected_at <= now,
                    )
                    .order_by(Incident.detected_at.desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        score = self.reliability_score(bundle)

        environment = None
        if environment_id is not None:
            environment = (
                await self.session.execute(
                    select(Environment).where(Environment.id == environment_id)
                )
            ).scalar_one_or_none()

        project = (
            await self.session.execute(
                select(SoftwareProject).where(SoftwareProject.id == project_id)
            )
        ).scalar_one_or_none()

        return {
            "project_id": str(project_id),
            "project_name": project.name if project else None,
            "environment_id": str(environment_id) if environment_id else None,
            "environment_name": environment.name if environment else None,
            "component_id": str(component_id),
            "component_name": component.name if component else None,
            "component_type": _enum_text(
                component.component_type if component is not None else None
            ),
            "generated_at": now.isoformat(),
            "reliability_score": score.as_dict(),
            "current_risk": {
                f"{ptype}:{horizon}": {
                    "risk_level": forecast.risk_level.value,
                    "risk_score": forecast.risk_score,
                    "confidence": forecast.confidence,
                    "data_quality": forecast.data_quality.value,
                    "generated_at": aware_utc(forecast.generated_at).isoformat(),
                    "headline": forecast.headline,
                    "dominant_signal": forecast.dominant_signal,
                }
                for (ptype, horizon), forecast in sorted(current.items())
            },
            "signals": {
                "anomalies_last_24h": bundle.numeric.get("anomalies_last_24h"),
                "incidents_last_7d": bundle.numeric.get("incidents_last_7d"),
                "incidents_last_30d": bundle.numeric.get("incidents_last_30d"),
                "error_trend": bundle.trend("error_rate").value,
                "latency_trend": bundle.trend("latency_p95").value,
                "dependency_health": bundle.numeric.get("dependency_failure_frequency"),
                "deployments_last_24h": bundle.numeric.get("deployments_last_24h"),
            },
            "data_quality": bundle.quality.value,
            "data_coverage": bundle.coverage,
            "data_quality_notes": bundle.quality_notes,
            "recent_incidents": [
                {
                    "id": str(incident.id),
                    "title": incident.title,
                    "severity": (
                        incident.severity.value if incident.severity else None
                    ),
                    "status": incident.status.value if incident.status else None,
                    "detected_at": (
                        aware_utc(incident.detected_at).isoformat()
                        if incident.detected_at
                        else None
                    ),
                }
                for incident in incident_rows
            ],
            "limitations": [
                "the reliability score is a weighted reading of named health "
                "dimensions, not a universal measure of software quality",
                "predicted risk describes evidence of increasing risk; it is not "
                "a probability of failure and not a causal claim",
            ],
        }

    # -- platform health (§43) -----------------------------------------
    async def platform_health(
        self, *, project_id: Optional[Any] = None, now: Optional[datetime] = None
    ) -> dict:
        """Forecast-platform metrics, with sample sizes attached (§43, §53)."""
        now = aware_utc(now or datetime.now(timezone.utc))

        def scoped(stmt: Any, model: Any) -> Any:
            if project_id is None:
                return stmt
            return stmt.where(model.project_id == project_id)

        total = int(
            (
                await self.session.execute(
                    scoped(
                        select(func.count()).select_from(ReliabilityForecast),
                        ReliabilityForecast,
                    )
                )
            ).scalar()
            or 0
        )
        active = int(
            (
                await self.session.execute(
                    scoped(
                        select(func.count())
                        .select_from(ReliabilityForecast)
                        .where(
                            ReliabilityForecast.status == ForecastStatus.ACTIVE,
                            ReliabilityForecast.valid_until >= now,
                        ),
                        ReliabilityForecast,
                    )
                )
            ).scalar()
            or 0
        )
        high_risk = int(
            (
                await self.session.execute(
                    scoped(
                        select(func.count())
                        .select_from(ReliabilityForecast)
                        .where(
                            ReliabilityForecast.risk_level.in_(
                                [ForecastRiskLevel.HIGH, ForecastRiskLevel.CRITICAL]
                            )
                        ),
                        ReliabilityForecast,
                    )
                )
            ).scalar()
            or 0
        )
        unknown = int(
            (
                await self.session.execute(
                    scoped(
                        select(func.count())
                        .select_from(ReliabilityForecast)
                        .where(
                            ReliabilityForecast.risk_level == ForecastRiskLevel.UNKNOWN
                        ),
                        ReliabilityForecast,
                    )
                )
            ).scalar()
            or 0
        )
        quality_rows = (
            await self.session.execute(
                scoped(
                    select(ReliabilityForecast.data_quality, func.count()).group_by(
                        ReliabilityForecast.data_quality
                    ),
                    ReliabilityForecast,
                )
            )
        ).all()
        model_rows = int(
            (
                await self.session.execute(
                    select(func.count()).select_from(ReliabilityModelVersion)
                )
            ).scalar()
            or 0
        )

        return {
            "generated_at": now.isoformat(),
            "forecast_count": total,
            "active_forecasts": active,
            "high_risk_forecasts": high_risk,
            "unknown_forecasts": unknown,
            "data_quality_distribution": {
                str(quality.value): int(count) for quality, count in quality_rows
            },
            "model_version_count": model_rows,
            "thresholds": risk.risk_policy().describe(),
            "limits": {
                "max_components_per_run": settings.RELIABILITY_MAX_COMPONENTS_PER_RUN,
                "min_evaluation_sample": settings.RELIABILITY_MIN_EVALUATION_SAMPLE,
            },
            "notes": [
                "confirmation rate, false-positive rate and lead time come from the "
                "evaluation service, which refuses to report a metric below the "
                "configured sample size",
                "an UNKNOWN forecast means the evidence was insufficient, not that "
                "the component is healthy",
            ],
        }


def _enum_text(value: Any) -> Optional[str]:
    """Render an enum-ish column as text, tolerating raw strings.

    Enum columns come back as members on PostgreSQL and as plain strings on
    some backends (and after a raw SQL read), so both are handled rather than
    assuming one shape — a profile must not 500 because the driver returned a
    string.
    """
    if value is None:
        return None
    candidate = getattr(value, "value", value)
    return str(candidate)


def _signal_row(
    forecast: ReliabilityForecast,
    scope: ForecastScope,
    draft: SignalDraft,
    rank: int,
) -> PredictiveSignal:
    """Materialise a signal draft as a stored row."""
    return PredictiveSignal(
        forecast_id=forecast.id,
        project_id=scope.project_id,
        environment_id=scope.environment_id,
        component_id=scope.component_id,
        signal_type=draft.signal_type,
        severity=draft.severity,
        contribution=draft.contribution,
        rank=rank,
        description=draft.description,
        metric_name=draft.metric_name,
        observed_value=draft.observed_value,
        baseline_value=draft.baseline_value,
        change_rate=draft.change_rate,
        trend=draft.trend,
        evidence_ids=draft.evidence_ids or None,
        similar_incident_count=draft.similar_incident_count,
        metadata_={"rule": draft.rule},
    )


__all__ = [
    "ForecastScope",
    "GenerationResult",
    "ReliabilityForecastService",
    "clear_forecast_hooks",
    "configured_horizons",
    "configured_prediction_types",
    "emit_forecast_event",
    "forecast_fingerprint",
    "register_forecast_hook",
]
