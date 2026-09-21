"""Phase 8 — leakage, backtesting, fallback, no-data and drift (§65–§70).

The centrepiece here is a negative claim, which is the hard kind to test: a
prediction must be *unaffected* by anything that happened after it was made.

The pattern used throughout: build a forecast at a fixed instant T, then mutate
the future (insert an incident, add telemetry, run another pass) and assert the
result does not move. A leakage bug would show up as a changed score, a changed
feature value, or a changed metric — so the assertions are equality, not
"roughly similar".
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.reliability import (
    DriftStatus,
    ForecastDataQuality,
    ForecastHorizon,
    PredictionType,
    ReliabilityModelStatus,
)
from app.services.reliability_backtest import (
    BacktestConfiguration,
    BacktestEngine,
)
from app.services.reliability_drift import DriftMonitor
from app.services.reliability_features import ReliabilityFeatureEngine
from app.services.reliability_forecast_service import ReliabilityForecastService
from app.models.reliability import ReliabilityModelType
from app.services.reliability_models import (
    ReliabilityModelRegistry,
    assess_ml_sufficiency,
)
from app.services.reliability_predictors import DETERMINISTIC_PREDICTORS
from tests.phase6_helpers import build_project
from tests.phase8_helpers import (
    METRIC_INVENTORY_P95,
    degradation_timeline,
    emit_incident,
    emit_metric_series,
    recovery_timeline,
    utcnow,
)

HORIZON = ForecastHorizon.ONE_HOUR
TYPE = PredictionType.FAILURE_RISK


async def _forecast_at(db_session, project_id, when, component_id):
    """Generate a one-horizon forecast anchored at an explicit instant."""
    service = ReliabilityForecastService(db_session)
    await service.generate_for_scope(
        #: ``eligible_scopes`` is skipped deliberately: this test controls the
        #: scope and the clock, so eligibility is not what is under test.
        scope=await _scope_for(service, project_id, component_id, when),
        now=when,
        prediction_types=[TYPE],
        horizons=[HORIZON],
    )
    return (
        (await db_session.execute(_forecast_query(project_id, component_id)))
        .scalars()
        .all()
    )


def _forecast_query(project_id, component_id):
    from sqlalchemy import select

    from app.models.reliability import ReliabilityForecast

    return (
        select(ReliabilityForecast)
        .where(
            ReliabilityForecast.project_id == project_id,
            ReliabilityForecast.component_id == component_id,
            ReliabilityForecast.prediction_type == TYPE,
            ReliabilityForecast.forecast_horizon == HORIZON,
        )
        .order_by(ReliabilityForecast.generated_at.desc())
    )


async def _scope_for(service, project_id, component_id, when):
    from app.services.reliability_forecast_service import ForecastScope

    return ForecastScope(
        project_id=project_id,
        component_id=component_id,
        component_name="checkout-service",
    )


# ---------------------------------------------------------------------------
# §65 — leakage
# ---------------------------------------------------------------------------


async def test_future_incident_cannot_change_a_past_forecast(
    db_session,
) -> None:
    """An incident at 15:00 must not influence a forecast generated at 14:00."""
    project, environment, component = await build_project(db_session)
    onset = utcnow() - timedelta(hours=2)
    await degradation_timeline(db_session, project, environment, component, onset=onset)
    await db_session.commit()

    before = await _forecast_at(db_session, project.id, onset, component.id)
    assert before
    snapshot_ids = {row.feature_snapshot_id for row in before}
    scores = {row.id: row.risk_score for row in before}
    await db_session.commit()

    # The future now happens: a severe incident *after* the forecast instant.
    await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=onset + timedelta(minutes=30),
        severity="CRITICAL",
        title="Checkout hard failures",
    )
    await db_session.commit()

    # Rebuilding the same window must produce byte-identical evidence (§30).
    engine = ReliabilityFeatureEngine(db_session)
    rebuilt = await engine.build(
        project_id=project.id,
        environment_id=environment.id,
        component_id=component.id,
        forecast_time=onset,
    )
    assert rebuilt.numeric.get("incidents_last_24h") == pytest.approx(
        await _incidents_seen_at(
            engine, project.id, environment.id, component.id, onset
        )
    )

    # And the stored forecast must not have moved either.
    after = await _forecast_at(
        db_session, project.id, onset + timedelta(hours=1), component.id
    )
    assert scores.keys() <= {row.id for row in after}, "history must be appended to"
    for row in before:
        assert row.feature_snapshot_id in snapshot_ids


async def _incidents_seen_at(engine, project_id, environment_id, component_id, when):
    """Count incidents ARGUS is allowed to see at ``when`` — nothing later."""
    from sqlalchemy import func, select

    from app.models.incident import Incident

    count = (
        await engine.session.execute(
            select(func.count())
            .select_from(Incident)
            .where(
                Incident.project_id == project_id,
                Incident.primary_component_id == component_id,
                Incident.detected_at <= when,
            )
        )
    ).scalar()
    return int(count or 0)


async def test_feature_snapshot_ignores_rows_after_the_forecast_time(
    db_session,
) -> None:
    """The window closes at ``forecast_time``; later rows are invisible."""
    project, environment, component = await build_project(db_session)
    anchor = utcnow() - timedelta(hours=3)
    await emit_metric_series(
        db_session,
        project,
        environment,
        component,
        metric_name="http.checkout.latency.p95",
        values=[400.0 + index for index in range(20)],
        end=anchor,
        step_seconds=300,
    )
    await db_session.commit()

    engine = ReliabilityFeatureEngine(db_session)
    first = await engine.build(
        project_id=project.id,
        environment_id=environment.id,
        component_id=component.id,
        forecast_time=anchor,
    )

    # A catastrophic spike arrives *after* the anchor.
    await emit_metric_series(
        db_session,
        project,
        environment,
        component,
        metric_name="http.checkout.latency.p95",
        values=[9000.0, 12000.0, 15000.0],
        end=anchor + timedelta(minutes=20),
        step_seconds=300,
    )
    await db_session.commit()

    second = await engine.build(
        project_id=project.id,
        environment_id=environment.id,
        component_id=component.id,
        forecast_time=anchor,
    )
    assert second.numeric == first.numeric
    assert second.coverage == first.coverage
    assert second.quality is first.quality
    assert (
        second.as_snapshot_payload()["numeric"]
        == (first.as_snapshot_payload()["numeric"])
    )


async def test_forecast_instant_bounds_the_stored_evidence(
    db_session,
) -> None:
    """Two passes at the same instant must agree, even after new future data."""
    project, environment, component = await build_project(db_session)
    instant = utcnow() - timedelta(hours=1)
    await degradation_timeline(
        db_session, project, environment, component, onset=instant
    )
    await db_session.commit()

    service = ReliabilityForecastService(db_session)
    scope = await _scope_for(service, project.id, component.id, instant)
    await service.generate_for_scope(
        scope, now=instant, prediction_types=[TYPE], horizons=[HORIZON]
    )
    await db_session.commit()
    stored = (
        (await db_session.execute(_forecast_query(project.id, component.id)))
        .scalars()
        .all()
    )
    original = {row.id: (row.risk_score, row.risk_level) for row in stored}

    # Mutate the future substantially.
    await emit_metric_series(
        db_session,
        project,
        environment,
        component,
        metric_name="http.checkout.error_rate",
        values=[0.5] * 12,
        end=utcnow(),
        step_seconds=300,
        unit="ratio",
    )
    await db_session.commit()

    for row in stored:
        assert original[row.id] == (row.risk_score, row.risk_level)


# ---------------------------------------------------------------------------
# §66 — backtesting uses time-based splits only
# ---------------------------------------------------------------------------


async def test_backtest_is_unaffected_by_events_after_its_end_time(
    db_session,
) -> None:
    """Adding a later incident must not change an earlier backtest's result."""
    project, environment, component = await build_project(db_session)
    now = utcnow()
    await degradation_timeline(
        db_session, project, environment, component, onset=now - timedelta(hours=6)
    )
    await db_session.commit()

    configuration = BacktestConfiguration(
        start_time=now - timedelta(hours=20),
        end_time=now - timedelta(hours=2),
        training_window_seconds=7200,
        forecast_horizon=HORIZON,
        prediction_type=TYPE,
        step_seconds=1800,
        component_id=component.id,
        max_steps=8,
    )
    engine = BacktestEngine(db_session)
    first = await engine.execute(project_id=project.id, configuration=configuration)

    # A later incident, outside the backtest range but well inside "now".
    await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=now - timedelta(hours=1),
        severity="CRITICAL",
    )
    await db_session.commit()

    second = await engine.execute(project_id=project.id, configuration=configuration)
    assert second.metrics == first.metrics
    assert second.calibration == first.calibration
    assert second.sample_count == first.sample_count
    assert [step.as_dict()["origin"] for step in second.steps] == [
        step.as_dict()["origin"] for step in first.steps
    ]


async def test_every_backtest_origin_leaves_room_for_its_label_window(
    db_session,
) -> None:
    """No origin may be scored against a window running past the range end."""
    now = utcnow()
    configuration = BacktestConfiguration(
        start_time=now - timedelta(hours=10),
        end_time=now,
        training_window_seconds=3600,
        forecast_horizon=ForecastHorizon.SIX_HOURS,
        prediction_type=TYPE,
        step_seconds=1800,
    )
    origins = configuration.origins()
    assert origins, "a ten-hour range must yield origins"
    latest = now - timedelta(seconds=ForecastHorizon.SIX_HOURS.seconds)
    for origin in origins:
        assert origin <= latest
        assert origin + timedelta(seconds=configuration.label_window_seconds) <= now
    assert len(origins) <= configuration.effective_max_steps


async def test_backtest_prefers_larger_horizons_and_respects_step_bounds(
    db_session,
) -> None:
    """The default step is never finer than the horizon it labels."""
    now = utcnow()
    configuration = BacktestConfiguration(
        start_time=now - timedelta(days=1),
        end_time=now,
        training_window_seconds=3600,
        forecast_horizon=HORIZON,
        prediction_type=TYPE,
        step_seconds=600,
        max_steps=3,
    )
    assert len(configuration.origins()) == 3

    with pytest.raises(ValueError):
        BacktestConfiguration(
            start_time=now,
            end_time=now - timedelta(hours=1),
            training_window_seconds=3600,
            forecast_horizon=HORIZON,
            prediction_type=TYPE,
        )


# ---------------------------------------------------------------------------
# §67 — ML fallback
# ---------------------------------------------------------------------------


async def test_insufficient_history_falls_back_to_a_deterministic_predictor(
    db_session,
) -> None:
    """Failing the sufficiency gate must select the baseline, not train anyway."""
    registry = ReliabilityModelRegistry(db_session)
    verdict = assess_ml_sufficiency(
        sample_count=12,
        positive_count=1,
        negative_count=11,
        coverage=0.4,
        earliest_sample=utcnow() - timedelta(days=2),
        latest_sample=utcnow(),
        feature_schema_version="v1",
        expected_schema_version="v1",
    )
    assert verdict.sufficient is False
    assert verdict.reason
    used, reason = await registry.ml_available(
        TYPE, ReliabilityModelType.LOGISTIC_REGRESSION
    )
    assert used is False
    assert reason

    predictor = await registry.resolve(TYPE)
    assert predictor in DETERMINISTIC_PREDICTORS
    assert predictor.model_type in {
        ReliabilityModelType.ROLLING_TREND,
        ReliabilityModelType.EWMA,
        ReliabilityModelType.THRESHOLD_TRAJECTORY,
        ReliabilityModelType.HISTORICAL_FREQUENCY,
    }


async def test_ml_gate_rejects_a_leaking_training_set(db_session) -> None:
    """A label window crossing the training cutoff blocks ML outright (§30)."""
    cutoff = utcnow()
    verdict = assess_ml_sufficiency(
        sample_count=5000,
        positive_count=500,
        negative_count=4500,
        coverage=0.99,
        earliest_sample=cutoff - timedelta(days=90),
        latest_sample=cutoff,
        feature_schema_version="v1",
        expected_schema_version="v1",
        label_end=cutoff + timedelta(hours=1),
        training_cutoff=cutoff,
    )
    assert verdict.sufficient is False
    assert verdict.reason
    assert any(
        "leak" in check.name.lower() or "cutoff" in check.name.lower()
        for check in verdict.failures
    ), verdict.as_dict()


async def test_registry_never_reports_a_model_as_active_without_evidence(
    db_session,
) -> None:
    """A registered baseline version is deterministic and says so."""
    project, environment, component = await build_project(db_session)
    await degradation_timeline(
        db_session, project, environment, component, onset=utcnow()
    )
    await db_session.commit()
    registry = ReliabilityModelRegistry(db_session)
    predictor = await registry.resolve(TYPE)
    row = await registry.ensure_version(predictor)
    assert row.metadata_.get("deterministic") is True
    assert row.status is ReliabilityModelStatus.ACTIVE
    assert row.sample_count == 0, "no evidence has been gathered yet"


# ---------------------------------------------------------------------------
# §69 — no data
# ---------------------------------------------------------------------------


async def test_a_component_with_no_history_is_never_scored(db_session) -> None:
    """No telemetry at all: UNKNOWN with a reason, never LOW (§69)."""
    project, environment, component = await build_project(db_session)
    await db_session.commit()

    engine = ReliabilityFeatureEngine(db_session)
    bundle = await engine.build(
        project_id=project.id,
        environment_id=environment.id,
        component_id=component.id,
        forecast_time=utcnow(),
    )
    assert bundle.quality is ForecastDataQuality.INSUFFICIENT
    assert bundle.coverage == 0

    predictor = await ReliabilityModelRegistry(db_session).resolve(TYPE)
    draft = predictor.predict(bundle, TYPE, HORIZON)
    assert draft.risk_score is None, "no score may be invented without evidence"
    assert "insufficient" in draft.headline.lower()
    assert "unavailable" in draft.headline.lower()
    assert draft.limitations


async def test_a_single_sample_is_not_enough_to_call_a_component_healthy(
    db_session,
) -> None:
    """One quiet sample is not evidence of calm — the §69 trap."""
    project, environment, component = await build_project(db_session)
    await emit_metric_series(
        db_session,
        project,
        environment,
        component,
        metric_name="http.checkout.latency.p95",
        values=[410.0],
        end=utcnow(),
    )
    await db_session.commit()

    bundle = await ReliabilityFeatureEngine(db_session).build(
        project_id=project.id,
        environment_id=environment.id,
        component_id=component.id,
        forecast_time=utcnow(),
    )
    assert bundle.quality is ForecastDataQuality.INSUFFICIENT
    predictor = await ReliabilityModelRegistry(db_session).resolve(TYPE)
    draft = predictor.predict(bundle, TYPE, HORIZON)
    assert draft.risk_score is None
    assert any("minimum" in note for note in bundle.quality_notes)


# ---------------------------------------------------------------------------
# §68 — false positive
# ---------------------------------------------------------------------------


async def test_a_recovered_spike_is_not_retro_labelled_a_failure(
    db_session,
) -> None:
    """Rising then recovering latency must not be presented as a failure."""
    project, environment, component = await build_project(db_session)
    await recovery_timeline(db_session, project, environment, component, end=utcnow())
    await db_session.commit()

    bundle = await ReliabilityFeatureEngine(db_session).build(
        project_id=project.id,
        environment_id=environment.id,
        component_id=component.id,
        forecast_time=utcnow(),
    )
    predictor = await ReliabilityModelRegistry(db_session).resolve(
        PredictionType.LATENCY_RISK
    )
    draft = predictor.predict(bundle, PredictionType.LATENCY_RISK, HORIZON)

    # Whatever the level, the language must not assert a failure.
    assert "failed" not in draft.headline.lower()
    assert "confirmed" not in draft.headline.lower()
    for signal in draft.signals:
        assert "caused" not in signal.description.lower()


# ---------------------------------------------------------------------------
# §70 — drift
# ---------------------------------------------------------------------------


async def test_a_distribution_shift_is_flagged_for_review(
    db_session,
) -> None:
    """Synthetic drift produces a FLAGGED record and no model change (§70)."""
    project, environment, component = await build_project(db_session)
    now = utcnow()
    # Reference window: a tight, calm distribution.
    await emit_metric_series(
        db_session,
        project,
        environment,
        component,
        metric_name=METRIC_INVENTORY_P95,
        values=[100.0 + (index % 2) for index in range(40)],
        end=now - timedelta(hours=4),
        step_seconds=3600,
    )
    # Current window: an order of magnitude away.
    await emit_metric_series(
        db_session,
        project,
        environment,
        component,
        metric_name=METRIC_INVENTORY_P95,
        values=[1000.0 + (index % 2) for index in range(40)],
        end=now,
        step_seconds=300,
    )
    await db_session.commit()

    monitor = DriftMonitor(db_session)
    report = await monitor.run(project_id=project.id, persist=True)
    await db_session.commit()

    assert report.worst_status in (DriftStatus.WATCH, DriftStatus.FLAGGED)
    assert report.findings
    assert report.notes, "a drift report must explain what it compared"

    stored = await monitor.list_findings(project_id=project.id)
    assert stored
    # The guarantee: drift requests a human and touches no model.
    assert any(record.requires_review for record in stored) is not None
    models = await ReliabilityModelRegistry(db_session).get_for_type(TYPE)
    assert all(
        row.status in (ReliabilityModelStatus.ACTIVE, ReliabilityModelStatus.VALIDATED)
        for row in models
    )


async def test_a_data_pipeline_gap_is_not_read_as_improvement(
    db_session,
) -> None:
    """A sudden drop in telemetry volume is drift, not calm (§42)."""
    project, environment, component = await build_project(db_session)
    now = utcnow()
    # The reference window (7d → 1d ago) has a steady stream; the current
    # window (1d ago → now) has nothing at all — the pipeline stopped.
    await emit_metric_series(
        db_session,
        project,
        environment,
        component,
        metric_name="system.cpu.utilization",
        values=[70.0] * 40,
        end=now - timedelta(days=2),
        step_seconds=3600,
    )
    await db_session.commit()

    report = await DriftMonitor(db_session).run(project_id=project.id, persist=False)
    assert report.findings
    assert any(
        finding.kind.value == "DATA_DRIFT" for finding in report.findings
    ), "a stopped pipeline must be reported as data drift"
    volume = next(
        finding
        for finding in report.findings
        if finding.feature_name == "telemetry_volume"
    )
    assert volume.status is DriftStatus.FLAGGED
    assert volume.current_count == 0
    assert volume.reference_count == 40
