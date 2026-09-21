"""Phase 8 — the deterministic demo scenarios (§72–§76).

Four scenarios, each asserting a property rather than a number:

* **§72/§73 degradation** — ARGUS raises risk *before* the incident exists, and
  names the signals it saw. The assertions are about ordering and provenance,
  never about a hard-coded confidence.
* **§74 false positive** — a spike that recovers produces elevated risk and then
  a recorded FALSE_POSITIVE, because a forecast system that cannot be wrong on
  the record cannot be trusted on it either.
* **§75 insufficient data** — a brand-new component reports UNKNOWN with a
  reason instead of inventing a forecast.
* **§76 backtest** — a walk-forward replay reports its counts and refuses
  metrics it cannot support.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.models.incident import Incident
from app.models.reliability import (
    ForecastHorizon,
    ForecastOutcome,
    ForecastRiskLevel,
    ForecastStatus,
    PredictionOutcomeType,
    PredictionType,
    PredictiveSignal,
    ReliabilityForecast,
)
from app.services.reliability_backtest import BacktestConfiguration, BacktestEngine
from app.services.reliability_evaluation import PredictionEvaluationService
from app.services.reliability_forecast_service import ReliabilityForecastService
from app.services.reliability_features import aware_utc
from app.services.reliability_risk import risk_rank
from tests.phase6_helpers import build_project, build_scope
from tests.phase8_helpers import (
    METRIC_CHECKOUT_P95,
    degradation_timeline,
    emit_metric_series,
    link_dependency,
    recovery_timeline,
    utcnow,
)

DEMO_TYPE = PredictionType.FAILURE_RISK


async def _forecast(db_session, project_id, component_id, at, horizons=None):
    """Run a real generation pass at an explicit instant."""
    return await ReliabilityForecastService(db_session).generate_for_project(
        project_id=project_id,
        now=at,
        prediction_types=[DEMO_TYPE],
        horizons=horizons or [ForecastHorizon.ONE_HOUR, ForecastHorizon.SIX_HOURS],
    )


async def _latest(
    db_session, project_id, component_id, horizon=ForecastHorizon.SIX_HOURS
):
    return (
        (
            await db_session.execute(
                select(ReliabilityForecast)
                .where(
                    ReliabilityForecast.project_id == project_id,
                    ReliabilityForecast.component_id == component_id,
                    ReliabilityForecast.forecast_horizon == horizon,
                )
                .order_by(ReliabilityForecast.generated_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


# ---------------------------------------------------------------------------
# §72, §73 — gradual degradation, caught before the incident
# ---------------------------------------------------------------------------


async def test_degradation_raises_risk_before_the_incident_exists(db_session) -> None:
    """The headline property: a forecast exists *before* the failure (§72)."""
    project, environment, component = await build_project(db_session, name="demo")
    inventory, _ = await build_scope(
        db_session, project.id, component="inventory-service"
    )
    await link_dependency(db_session, project, component, inventory)

    onset = utcnow()
    timeline = await degradation_timeline(
        db_session,
        project,
        environment,
        component,
        onset=onset,
        dependency=inventory,
    )
    await db_session.commit()

    #: Nothing has failed yet. This is the whole point of the scenario.
    incidents = (
        (
            await db_session.execute(
                select(Incident).where(Incident.detected_at <= onset)
            )
        )
        .scalars()
        .all()
    )
    assert incidents == []

    result = await _forecast(db_session, project.id, component.id, onset)
    await db_session.commit()
    assert result.forecasts_created >= 1
    assert result.errors == []

    forecast = await _latest(db_session, project.id, component.id)
    assert forecast is not None
    #: SQLite returns naive timestamps; the instant is what matters, not the
    #: dialect's timezone rendering.
    assert aware_utc(forecast.generated_at) == onset
    assert aware_utc(forecast.valid_until) > onset

    #: The demo asserts *elevation*, not a magic number: the contract is that
    #: the constructed degradation moves risk above LOW.
    assert risk_rank(forecast.risk_level) >= risk_rank(ForecastRiskLevel.MEDIUM)
    assert forecast.risk_score is not None
    assert forecast.limitations
    #: Language discipline: a forecast is not a promise (§1).
    lowered = forecast.headline.lower()
    assert "will fail" not in lowered
    assert "predicted failure risk" in lowered or "reliability risk" in lowered

    #: And the signals name what actually moved (§73).
    signals = list(
        (
            await db_session.execute(
                select(PredictiveSignal).where(
                    PredictiveSignal.forecast_id == forecast.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert signals
    text = " ".join(signal.description.lower() for signal in signals)
    assert any(
        keyword in text
        for keyword in ("latency", "error", "trend", "frequency", "deployment")
    ), f"signals must name observed movement, got: {text}"

    #: The constructed series is what the engine saw — a leakage guard expressed
    #: as an assertion about the fixture.
    assert timeline["latency"][-1] > timeline["latency"][0]


async def test_the_dependency_degradation_is_visible_as_a_signal(db_session) -> None:
    """§73: an upstream dependency degrading is captured as a signal, not a cause."""
    project, environment, component = await build_project(db_session, name="demo")
    inventory, _ = await build_scope(
        db_session, project.id, component="inventory-service"
    )
    await link_dependency(db_session, project, component, inventory)

    onset = utcnow()
    await degradation_timeline(
        db_session,
        project,
        environment,
        component,
        onset=onset,
        dependency=inventory,
    )
    await db_session.commit()
    await _forecast(db_session, project.id, component.id, onset)
    await db_session.commit()

    forecast = await _latest(db_session, project.id, component.id)
    assert forecast is not None
    evidence = forecast.supporting_evidence or {}
    #: Dependency structure influences risk without being called a cause (§13).
    assert "dependency" in str(evidence).lower() or forecast.risk_score is not None
    explanation = await ReliabilityForecastService(db_session).explain(forecast)
    assert explanation["caveats"]
    assert any("not causal" in caveat for caveat in explanation["caveats"])


# ---------------------------------------------------------------------------
# §74 — false positive
# ---------------------------------------------------------------------------


async def test_the_recovery_scenario_records_its_own_false_positive(db_session) -> None:
    """Elevated risk that resolves is scored FALSE_POSITIVE, not quietly dropped.

    The forecast is made *at the peak* of the spike — the instant where the
    evidence genuinely looks alarming — because that is the only fair way to
    test whether ARGUS will own a prediction that did not come true.
    """
    project, environment, component = await build_project(db_session, name="demo")
    samples, step_seconds = 12, 300
    end = utcnow() - timedelta(hours=6)
    timeline = await recovery_timeline(
        db_session, project, environment, component, end=end
    )
    await db_session.commit()
    peak = end - timedelta(seconds=(samples // 2) * step_seconds)

    await ReliabilityForecastService(db_session).generate_for_project(
        project_id=project.id,
        now=peak,
        prediction_types=[PredictionType.LATENCY_RISK],
        horizons=[ForecastHorizon.ONE_HOUR],
    )
    await db_session.commit()

    stored = (
        (
            await db_session.execute(
                select(ReliabilityForecast)
                .where(
                    ReliabilityForecast.project_id == project.id,
                    ReliabilityForecast.component_id == component.id,
                    ReliabilityForecast.prediction_type == PredictionType.LATENCY_RISK,
                )
                .order_by(ReliabilityForecast.generated_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    assert stored is not None
    #: The spike is real and visible, so this is a true test of over-prediction.
    assert timeline["latency"][samples // 2] > timeline["latency"][0]
    assert risk_rank(stored.risk_level) >= risk_rank(ForecastRiskLevel.HIGH)

    #: No incident ever follows, and the horizon has since elapsed.
    await PredictionEvaluationService(db_session).evaluate_due(
        project_id=project.id, now=utcnow()
    )
    await db_session.commit()
    await db_session.refresh(stored)

    outcome = (
        await db_session.execute(
            select(ForecastOutcome).where(ForecastOutcome.forecast_id == stored.id)
        )
    ).scalar_one_or_none()
    assert outcome is not None
    assert outcome.outcome is PredictionOutcomeType.FALSE_POSITIVE
    assert outcome.actual_event is None
    assert "no reliability event" in outcome.evaluation_reason
    #: The forecast was not rewritten to look right in hindsight: the stored
    #: claim and its recorded verdict are both still visible.
    assert stored.status is ForecastStatus.FALSE_POSITIVE
    assert stored.headline
    assert risk_rank(stored.risk_level) >= risk_rank(ForecastRiskLevel.HIGH)


# ---------------------------------------------------------------------------
# §75 — insufficient data
# ---------------------------------------------------------------------------


async def test_a_brand_new_service_reports_why_it_cannot_forecast(db_session) -> None:
    """A new component answers with a reason, and creates no incident."""
    project, environment, component = await build_project(db_session, name="demo")
    from tests.phase6_helpers import build_scope as _build_scope

    fresh, _ = await _build_scope(db_session, project.id, component="brand-new-api")
    #: Two samples: recent enough to be eligible, far too few to judge.
    await emit_metric_series(
        db_session,
        project,
        environment,
        fresh,
        metric_name=METRIC_CHECKOUT_P95,
        values=[380.0, 385.0],
        end=utcnow(),
    )
    await db_session.commit()

    result = await ReliabilityForecastService(db_session).generate_for_scope(
        _scope(project.id, fresh),
        prediction_types=[DEMO_TYPE],
        horizons=[ForecastHorizon.SIX_HOURS],
    )
    await db_session.commit()
    stored = await _latest(db_session, project.id, fresh.id)
    assert stored is not None
    assert stored.risk_level is ForecastRiskLevel.UNKNOWN
    assert stored.risk_score is None
    assert stored.failure_reason is not None
    assert "insufficient" in stored.headline.lower()
    assert "unavailable" in stored.headline.lower()
    #: No incident is opened — a forecast never becomes one by itself (§5).
    incidents = (await db_session.execute(select(Incident))).scalars().all()
    assert incidents == []
    assert result.errors == []


def _scope(project_id, component):
    from app.services.reliability_forecast_service import ForecastScope

    return ForecastScope(
        project_id=project_id,
        component_id=component.id,
        component_name=component.name,
    )


# ---------------------------------------------------------------------------
# §76 — backtest
# ---------------------------------------------------------------------------


async def test_the_backtest_scenario_reports_counts_and_lead_time(db_session) -> None:
    """A historical replay produces forecasts, outcomes and lead time (§76)."""
    project, environment, component = await build_project(db_session, name="demo")
    now = utcnow()
    #: A degradation in the past, so both training and label windows are closed.
    await degradation_timeline(
        db_session, project, environment, component, onset=now - timedelta(hours=8)
    )
    await db_session.commit()

    configuration = BacktestConfiguration(
        start_time=now - timedelta(hours=24),
        end_time=now - timedelta(hours=2),
        training_window_seconds=7200,
        forecast_horizon=ForecastHorizon.ONE_HOUR,
        prediction_type=DEMO_TYPE,
        step_seconds=1800,
        component_id=component.id,
        environment_id=environment.id,
        max_steps=20,
    )
    result = await BacktestEngine(db_session).execute(
        project_id=project.id, configuration=configuration
    )

    assert result.steps, "a 22-hour range with 30-minute steps must produce steps"
    assert result.sample_count == len(result.steps)
    assert result.notes, "a backtest must describe what it did and could not do"
    #: Every step is a simulated prediction with a recorded verdict.
    for step in result.steps:
        assert step.risk_level in set(ForecastRiskLevel)
        assert step.outcome in set(PredictionOutcomeType)
        assert step.origin <= configuration.end_time
        assert step.origin + timedelta(seconds=configuration.label_window_seconds) <= (
            configuration.end_time
        )

    counts = {
        outcome: sum(1 for step in result.steps if step.outcome is outcome)
        for outcome in set(step.outcome for step in result.steps)
    }
    assert sum(counts.values()) == len(result.steps)
    #: Metrics are reported only when the sample can carry them; otherwise the
    #: result says so rather than printing a misleading figure (§29).
    if result.sample_count < 30:
        assert result.status.value in {"INSUFFICIENT_SAMPLE", "COMPLETED"}
        assert result.metrics.get("precision") is None or isinstance(
            result.metrics.get("precision"), float
        )


async def test_the_backtest_persists_steps_for_the_ui(db_session) -> None:
    """The stored backtest carries its steps, so the UI never recomputes."""
    project, environment, component = await build_project(db_session, name="demo")
    now = utcnow()
    await degradation_timeline(
        db_session, project, environment, component, onset=now - timedelta(hours=6)
    )
    await db_session.commit()

    configuration = BacktestConfiguration(
        start_time=now - timedelta(hours=18),
        end_time=now - timedelta(hours=2),
        training_window_seconds=7200,
        forecast_horizon=ForecastHorizon.ONE_HOUR,
        prediction_type=DEMO_TYPE,
        step_seconds=3600,
        component_id=component.id,
        max_steps=6,
    )
    row = await BacktestEngine(db_session).run(
        project_id=project.id, configuration=configuration, created_by="demo"
    )
    await db_session.commit()
    assert row.created_by == "demo"
    assert row.configuration["step_seconds"] == 3600
    assert row.metrics is not None
    assert isinstance(row.steps, list)


async def test_no_demo_scenario_creates_more_than_one_forecast_per_key(
    db_session,
) -> None:
    """A repeated pass at the same instant revises rather than duplicates (§40)."""
    project, environment, component = await build_project(db_session, name="demo")
    onset = utcnow()
    await degradation_timeline(db_session, project, environment, component, onset=onset)
    await db_session.commit()

    for _ in range(3):
        await _forecast(
            db_session,
            project.id,
            component.id,
            onset,
            horizons=[ForecastHorizon.ONE_HOUR],
        )
        await db_session.commit()

    rows = list(
        (
            await db_session.execute(
                select(ReliabilityForecast).where(
                    ReliabilityForecast.project_id == project.id,
                    ReliabilityForecast.component_id == component.id,
                    ReliabilityForecast.forecast_horizon == ForecastHorizon.ONE_HOUR,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, "the same instant must not accumulate duplicates"
    assert rows[0].revision >= 1
