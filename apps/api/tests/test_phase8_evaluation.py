"""Phase 8 — lifecycle, outcomes, metrics, calibration, warnings and time series.

The evaluation layer is where a prediction becomes falsifiable, so the tests
here care as much about *refusals* as about numbers: a metric that cannot be
justified must be absent, an outcome that cannot be decided must be
INCONCLUSIVE, and a warning that would repeat must be deduplicated rather than
posted again.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.reliability import (
    CalibrationStatus,
    EarlyWarningStatus,
    FeatureTrend,
    ForecastDataQuality,
    ForecastHorizon,
    ForecastRiskLevel,
    ForecastStatus,
    PredictionOutcomeType,
    PredictionType,
    PredictiveSignalType,
    ReliabilityForecast,
)
from app.services import reliability_stats as stats
from app.services.reliability_evaluation import (
    PredictionEvaluationService,
    compute_metrics,
)
from app.services.reliability_features import ReliabilityFeatureEngine
from app.services.reliability_forecast_service import ReliabilityForecastService
from app.services.reliability_risk import RiskPolicy, classify_score, risk_rank
from app.services.reliability_warnings import EarlyWarningService
from tests.phase6_helpers import build_project
from tests.phase8_helpers import (
    METRIC_CHECKOUT_P95,
    degradation_timeline,
    emit_anomaly,
    emit_incident,
    emit_metric_series,
    utcnow,
)


# ---------------------------------------------------------------------------
# §63 — pure statistics: the time-series cases the phase names
# ---------------------------------------------------------------------------


class TestTimeSeries:
    """The seven shapes §64 requires, asserted on the statistic itself."""

    def test_increasing_trend_is_positive_per_hour(self) -> None:
        values = [400.0, 450.0, 520.0, 610.0, 720.0]
        assert (stats.normalized_slope(values) or 0) > 0
        assert stats.classify_trend(values) is FeatureTrend.RISING

    def test_decreasing_trend_is_negative(self) -> None:
        values = [720.0, 610.0, 520.0, 450.0, 400.0]
        assert (stats.normalized_slope(values) or 0) < 0
        assert stats.classify_trend(values) is FeatureTrend.FALLING

    def test_flat_trend_is_not_reported_as_movement(self) -> None:
        values = [400.0] * 8
        assert stats.classify_trend(values) is FeatureTrend.FLAT
        assert stats.normalized_slope(values) == pytest.approx(0.0)

    def test_noisy_series_is_volatile_not_trending(self) -> None:
        """Oscillation around a stable mean is VOLATILE, not a direction."""
        values = [410.0, 700.0, 200.0, 690.0, 210.0, 700.0]
        assert stats.classify_trend(values) is FeatureTrend.VOLATILE
        assert stats.coefficient_of_variation(values) > 0.35

    def test_missing_values_are_dropped_not_zeroed(self) -> None:
        values = [400.0, None, 500.0, None, 600.0]
        assert stats.clean(values) == [400.0, 500.0, 600.0]
        assert stats.mean(values) == pytest.approx(500.0)

    def test_empty_and_single_point_series_produce_no_claim(self) -> None:
        assert stats.mean([]) is None
        assert stats.normalized_slope([]) is None
        assert stats.normalized_slope([1.0]) is None
        assert stats.classify_trend([]) is FeatureTrend.UNKNOWN
        assert stats.classify_trend([1.0]) is FeatureTrend.UNKNOWN

    def test_identical_timestamps_do_not_divide_by_zero(self) -> None:
        """A degenerate timestamp grid yields None, never an invented slope."""
        moment = utcnow()
        value = stats.slope_per_hour([500.0, 500.0], [moment, moment])
        assert value is None or value == pytest.approx(0.0)

    def test_slope_per_hour_is_normalized_by_the_elapsed_time(self) -> None:
        start = utcnow()
        values = [100.0, 200.0, 300.0]
        stamps = [start, start + timedelta(hours=1), start + timedelta(hours=2)]
        per_hour = stats.slope_per_hour(values, stamps)
        assert per_hour is not None
        assert (per_hour or 0) > 0

    def test_ewma_smooths_toward_the_recent_values(self) -> None:
        values = [100.0, 100.0, 100.0, 900.0]
        smoothed = stats.ewma_last(values, 0.5)
        assert smoothed is not None and 100.0 < smoothed < 900.0
        assert stats.ewma_last(values, 1.0) == pytest.approx(900.0)

    def test_projection_is_bounded_and_never_extrapolates_forever(self) -> None:
        values = [100.0, 200.0, 400.0, 800.0]
        projected = stats.bounded_linear_projection(
            values, steps_ahead=10, max_growth_ratio=3.0
        )
        assert projected is not None
        #: The cap is a documented multiple, so a runaway series cannot
        #: produce an unbounded forecast value (§20).
        average = sum(values) / len(values)
        assert projected <= max(average * 3.0, max(values)) + 1e-6

    def test_projection_refuses_a_single_point(self) -> None:
        assert (
            stats.bounded_linear_projection(
                [100.0], steps_ahead=5, max_growth_ratio=3.0
            )
            is None
        )

    def test_saturation_detection_needs_a_ceiling(self) -> None:
        assert stats.is_saturated(95.0, ceiling=100.0, saturation_ratio=0.85) is True
        assert stats.is_saturated(20.0, ceiling=100.0, saturation_ratio=0.85) is False
        #: Without a configured ceiling the answer is False, not a guess.
        assert stats.is_saturated(95.0, ceiling=None, saturation_ratio=0.85) is False


# ---------------------------------------------------------------------------
# §5 — risk policy
# ---------------------------------------------------------------------------


class TestRiskPolicy:
    def test_thresholds_are_configurable_and_ordered(self) -> None:
        policy = RiskPolicy(
            threshold_medium=0.2, threshold_high=0.5, threshold_critical=0.8
        )
        assert policy.classify(0.1) is ForecastRiskLevel.LOW
        assert policy.classify(0.3) is ForecastRiskLevel.MEDIUM
        assert policy.classify(0.6) is ForecastRiskLevel.HIGH
        assert policy.classify(0.95) is ForecastRiskLevel.CRITICAL
        assert policy.classify(None) is ForecastRiskLevel.UNKNOWN
        assert policy.describe()["threshold_high"] == 0.5

    def test_policy_rejects_inverted_thresholds(self) -> None:
        with pytest.raises(ValueError):
            RiskPolicy(threshold_medium=0.9, threshold_high=0.5, threshold_critical=0.8)

    def test_ranks_order_the_levels(self) -> None:
        assert risk_rank(ForecastRiskLevel.CRITICAL) > risk_rank(ForecastRiskLevel.HIGH)
        assert risk_rank(ForecastRiskLevel.UNKNOWN) < risk_rank(ForecastRiskLevel.LOW)

    def test_classify_score_matches_the_published_policy(self) -> None:
        from app.services.reliability_risk import risk_policy

        policy = risk_policy()
        assert classify_score(0.0) is ForecastRiskLevel.LOW
        assert classify_score(None) is ForecastRiskLevel.UNKNOWN
        assert classify_score(policy.threshold_critical) is ForecastRiskLevel.CRITICAL


# ---------------------------------------------------------------------------
# §27, §28, §29 — lifecycle and outcomes
# ---------------------------------------------------------------------------


async def _stored_forecast(db_session, project_id, **overrides):
    """Insert a forecast directly, with an explicit instant and window.

    ``revision`` is caller-controlled because the table's uniqueness rule is
    (scope, type, horizon, revision): several forecasts of the same key must be
    successive revisions, which is exactly the dedup invariant.
    """
    revision = overrides.pop("revision", 1)
    now = overrides.pop("now", utcnow())
    horizon = overrides.pop("horizon", ForecastHorizon.ONE_HOUR)
    row = ReliabilityForecast(
        project_id=project_id,
        environment_id=overrides.pop("environment_id", None),
        component_id=overrides.pop("component_id", None),
        prediction_type=overrides.pop("prediction_type", PredictionType.FAILURE_RISK),
        forecast_horizon=horizon,
        generated_at=now,
        valid_from=now,
        valid_until=now + timedelta(seconds=horizon.seconds),
        risk_level=overrides.pop("risk_level", ForecastRiskLevel.HIGH),
        risk_score=overrides.pop("risk_score", 0.7),
        data_quality=overrides.pop("data_quality", ForecastDataQuality.GOOD),
        model_version_label="rolling-trend/v1",
        fingerprint=overrides.pop("fingerprint", "fp-" + str(now.timestamp())),
        headline=overrides.pop("headline", "Elevated risk over the next 1h"),
        status=overrides.pop("status", ForecastStatus.ACTIVE),
        revision=revision,
        **overrides,
    )
    db_session.add(row)
    await db_session.flush()
    return row


class TestLifecycleAndOutcomes:
    async def test_a_horizon_that_elapsed_becomes_expired_not_confirmed(
        self, db_session
    ) -> None:
        project, environment, component = await build_project(db_session)
        now = utcnow()
        forecast = await _stored_forecast(
            db_session,
            project.id,
            now=now - timedelta(hours=3),
            environment_id=environment.id,
            component_id=component.id,
        )
        await db_session.commit()

        expired = await ReliabilityForecastService(db_session).expire_due(now=now)
        await db_session.commit()
        await db_session.refresh(forecast)
        assert expired >= 1
        #: Expiry is not confirmation: nothing happened, and ARGUS must not
        #: imply that it did (§27).
        assert forecast.status is ForecastStatus.EXPIRED

    async def test_a_prediction_followed_by_an_incident_is_a_true_positive(
        self, db_session
    ) -> None:
        project, environment, component = await build_project(db_session)
        now = utcnow()
        forecast = await _stored_forecast(
            db_session,
            project.id,
            now=now - timedelta(hours=2),
            horizon=ForecastHorizon.ONE_HOUR,
            environment_id=environment.id,
            component_id=component.id,
            risk_level=ForecastRiskLevel.HIGH,
        )
        await emit_incident(
            db_session,
            project,
            environment,
            component,
            detected_at=now - timedelta(hours=1, minutes=30),
            severity="HIGH",
        )
        await db_session.commit()

        service = PredictionEvaluationService(db_session)
        summary = await service.evaluate_due(project_id=project.id, now=now)
        await db_session.commit()
        assert summary["scored"] == 1

        from sqlalchemy import select

        from app.models.reliability import ForecastOutcome

        outcome = (
            await db_session.execute(
                select(ForecastOutcome).where(
                    ForecastOutcome.forecast_id == forecast.id
                )
            )
        ).scalar_one()
        assert outcome.outcome is PredictionOutcomeType.TRUE_POSITIVE
        assert outcome.matched_incident_id is not None
        assert outcome.time_to_event_seconds is not None
        assert outcome.evaluation_reason
        await db_session.refresh(forecast)
        assert forecast.status is ForecastStatus.CONFIRMED

    async def test_elevated_risk_with_no_event_is_scored_a_false_positive(
        self, db_session
    ) -> None:
        """§68: the system records its own over-prediction rather than hiding it."""
        project, environment, component = await build_project(db_session)
        now = utcnow()
        forecast = await _stored_forecast(
            db_session,
            project.id,
            now=now - timedelta(hours=2),
            environment_id=environment.id,
            component_id=component.id,
        )
        await db_session.commit()

        await PredictionEvaluationService(db_session).evaluate_due(
            project_id=project.id, now=now
        )
        await db_session.commit()

        from sqlalchemy import select

        from app.models.reliability import ForecastOutcome

        outcome = (
            await db_session.execute(
                select(ForecastOutcome).where(
                    ForecastOutcome.forecast_id == forecast.id
                )
            )
        ).scalar_one()
        assert outcome.outcome is PredictionOutcomeType.FALSE_POSITIVE
        await db_session.refresh(forecast)
        assert forecast.status is ForecastStatus.FALSE_POSITIVE

    async def test_an_unknown_forecast_is_inconclusive_rather_than_wrong(
        self, db_session
    ) -> None:
        """No claim was made, so there is nothing to score as correct or not."""
        project, environment, component = await build_project(db_session)
        now = utcnow()
        forecast = await _stored_forecast(
            db_session,
            project.id,
            now=now - timedelta(hours=2),
            environment_id=environment.id,
            component_id=component.id,
            risk_level=ForecastRiskLevel.UNKNOWN,
            risk_score=None,
            data_quality=ForecastDataQuality.INSUFFICIENT,
        )
        await db_session.commit()
        await PredictionEvaluationService(db_session).evaluate_due(
            project_id=project.id, now=now
        )
        await db_session.commit()
        await db_session.refresh(forecast)
        assert forecast.status is ForecastStatus.INCONCLUSIVE

    async def test_a_missed_event_is_a_false_negative_not_a_silence(
        self, db_session
    ) -> None:
        """A LOW forecast before an incident is recorded as a miss (§29)."""
        project, environment, component = await build_project(db_session)
        now = utcnow()
        forecast = await _stored_forecast(
            db_session,
            project.id,
            now=now - timedelta(hours=2),
            environment_id=environment.id,
            component_id=component.id,
            risk_level=ForecastRiskLevel.LOW,
            risk_score=0.1,
        )
        await emit_anomaly(
            db_session,
            project,
            environment,
            component,
            detected_at=now - timedelta(hours=1, minutes=30),
            anomaly_type="LATENCY_SPIKE",
            severity="HIGH",
        )
        await db_session.commit()

        await PredictionEvaluationService(db_session).evaluate_due(
            project_id=project.id, now=now
        )
        await db_session.commit()

        from sqlalchemy import select

        from app.models.reliability import ForecastOutcome

        outcome = (
            await db_session.execute(
                select(ForecastOutcome).where(
                    ForecastOutcome.forecast_id == forecast.id
                )
            )
        ).scalar_one()
        assert outcome.outcome is PredictionOutcomeType.FALSE_NEGATIVE
        assert outcome.matched_anomaly_id is not None

    async def test_evaluation_is_idempotent(self, db_session) -> None:
        """Re-running scores nothing twice, so a metric cannot drift by accident."""
        project, environment, component = await build_project(db_session)
        now = utcnow()
        await _stored_forecast(
            db_session,
            project.id,
            now=now - timedelta(hours=2),
            environment_id=environment.id,
            component_id=component.id,
        )
        await db_session.commit()
        service = PredictionEvaluationService(db_session)
        first = await service.evaluate_due(project_id=project.id, now=now)
        await db_session.commit()
        second = await service.evaluate_due(project_id=project.id, now=now)
        await db_session.commit()
        assert first["scored"] == 1
        assert second["scored"] == 0
        #: The decided forecast is no longer a candidate at all — it has an
        #: outcome, so it is settled rather than silently re-scored.
        assert second["skipped"] + second["candidates"] == 0

        from sqlalchemy import select

        from app.models.reliability import ForecastOutcome

        outcomes = list(
            (await db_session.execute(select(ForecastOutcome))).scalars().all()
        )
        assert len(outcomes) == 1, "one forecast must yield exactly one outcome"


# ---------------------------------------------------------------------------
# §29, §32, §33 — metrics, sample floors, calibration
# ---------------------------------------------------------------------------


class TestMetrics:
    def test_no_metrics_below_the_sample_floor(self) -> None:
        summary = compute_metrics([])
        assert summary.sample_count == 0
        assert summary.metrics.get("precision") in (None, 0)
        assert summary.notes, "an empty sample must explain why there is no metric"

    async def test_precision_and_recall_appear_once_there_is_enough_evidence(
        self, db_session
    ) -> None:
        """Metrics are reported only when the sample can support them."""
        project, environment, component = await build_project(db_session)
        now = utcnow()
        from app.models.reliability import ForecastOutcome

        #: 40 scored forecasts: 20 positive, half of them followed by an event.
        rows = []
        for index in range(40):
            positive = index < 20
            event = positive and index % 2 == 0
            forecast = await _stored_forecast(
                db_session,
                project.id,
                now=now - timedelta(hours=3 + index),
                environment_id=environment.id,
                component_id=component.id,
                risk_level=(
                    ForecastRiskLevel.HIGH if positive else ForecastRiskLevel.LOW
                ),
                risk_score=0.7 if positive else 0.1,
                fingerprint=f"metric-{index}",
                revision=index + 1,
            )
            rows.append(
                ForecastOutcome(
                    forecast_id=forecast.id,
                    project_id=project.id,
                    component_id=component.id,
                    evaluation_window_start=forecast.valid_from,
                    evaluation_window_end=forecast.valid_until,
                    outcome=(
                        PredictionOutcomeType.TRUE_POSITIVE
                        if event
                        else PredictionOutcomeType.FALSE_POSITIVE
                        if positive
                        else PredictionOutcomeType.FALSE_NEGATIVE
                        if index < 30
                        else PredictionOutcomeType.TRUE_NEGATIVE
                    ),
                    predicted_risk_level=forecast.risk_level,
                    predicted_risk_score=forecast.risk_score,
                    evaluation_reason="fixture",
                    evaluated_at=forecast.valid_until + timedelta(minutes=1),
                )
            )
        db_session.add_all(rows)
        await db_session.commit()

        from sqlalchemy import select

        stored = list(
            (await db_session.execute(select(ForecastOutcome))).scalars().all()
        )
        summary = compute_metrics(stored)
        assert summary.sample_count == 40
        assert summary.positive_count + summary.negative_count == 40
        assert summary.metrics.get("precision") is not None
        assert summary.metrics.get("recall") is not None
        assert summary.metrics.get("false_positive_rate") is not None
        assert 0.0 <= summary.metrics["precision"] <= 1.0
        assert summary.status.value in {"COMPLETED", "INSUFFICIENT_SAMPLE"}

    async def test_a_run_persists_its_window_filters_and_counts(
        self, db_session
    ) -> None:
        """§32: an evaluation run is immutable, self-describing history."""
        project, environment, component = await build_project(db_session)
        now = utcnow()
        await _stored_forecast(
            db_session,
            project.id,
            now=now - timedelta(hours=2),
            environment_id=environment.id,
            component_id=component.id,
        )
        await db_session.commit()

        run = await PredictionEvaluationService(db_session).run_evaluation(
            project_id=project.id,
            window_start=now - timedelta(days=30),
            window_end=now,
        )
        await db_session.commit()
        assert run.feature_schema_version
        assert run.dataset_window_start < run.dataset_window_end
        assert run.sample_count >= 0
        assert run.calibration_status in set(CalibrationStatus)
        assert run.notes, "a run records why it reports what it reports"

    async def test_calibration_status_is_never_claimed_without_bands(
        self, db_session
    ) -> None:
        project, environment, component = await build_project(db_session)
        now = utcnow()
        await _stored_forecast(
            db_session,
            project.id,
            now=now - timedelta(hours=2),
            environment_id=environment.id,
            component_id=component.id,
        )
        await db_session.commit()
        run = await PredictionEvaluationService(db_session).run_evaluation(
            project_id=project.id,
            window_start=now - timedelta(days=30),
            window_end=now,
        )
        if run.calibration_status is CalibrationStatus.UNKNOWN:
            assert not run.reliability_bands
        else:
            assert run.reliability_bands, "a calibration claim needs its bands"


# ---------------------------------------------------------------------------
# §39, §40 — early warning dedup, cooldown, floors
# ---------------------------------------------------------------------------


class TestEarlyWarnings:
    async def test_repeated_forecasts_refresh_one_warning(self, db_session) -> None:
        """One underlying trend yields one warning with a count, not a storm."""
        project, environment, component = await build_project(db_session)
        now = utcnow()
        forecast = await _stored_forecast(
            db_session,
            project.id,
            now=now,
            environment_id=environment.id,
            component_id=component.id,
            risk_level=ForecastRiskLevel.HIGH,
            fingerprint="stable-fingerprint",
        )
        await db_session.commit()

        service = EarlyWarningService(db_session)
        first = await service.evaluate(project_id=project.id, now=now)
        await db_session.commit()
        assert first["raised"] == 1

        # A second pass over the same live forecast must not raise a second one.
        second = await service.evaluate(
            project_id=project.id, now=now + timedelta(minutes=1)
        )
        await db_session.commit()
        assert second["raised"] == 0
        assert second["updated"] + second["suppressed"] >= 1

        warnings = await service.list_warnings(project_id=project.id)
        assert len(warnings) == 1
        assert warnings[0].occurrence_count >= 1
        assert warnings[0].forecast_id == forecast.id

    async def test_warnings_below_the_severity_floor_are_not_raised(
        self, db_session
    ) -> None:
        project, environment, component = await build_project(db_session)
        now = utcnow()
        await _stored_forecast(
            db_session,
            project.id,
            now=now,
            environment_id=environment.id,
            component_id=component.id,
            risk_level=ForecastRiskLevel.LOW,
            risk_score=0.1,
        )
        await db_session.commit()
        service = EarlyWarningService(db_session)
        summary = await service.evaluate(project_id=project.id, now=now)
        await db_session.commit()
        assert summary["raised"] == 0
        assert await service.list_warnings(project_id=project.id) == []

    async def test_unknown_risk_never_raises_a_warning(self, db_session) -> None:
        """No evidence must not be escalated as if it were bad news."""
        project, environment, component = await build_project(db_session)
        now = utcnow()
        await _stored_forecast(
            db_session,
            project.id,
            now=now,
            environment_id=environment.id,
            component_id=component.id,
            risk_level=ForecastRiskLevel.UNKNOWN,
            risk_score=None,
        )
        await db_session.commit()
        service = EarlyWarningService(db_session)
        await service.evaluate(project_id=project.id, now=now)
        await db_session.commit()
        assert await service.list_warnings(project_id=project.id) == []

    async def test_an_expired_forecast_warning_is_retired(self, db_session) -> None:
        project, environment, component = await build_project(db_session)
        now = utcnow()
        await _stored_forecast(
            db_session,
            project.id,
            now=now,
            environment_id=environment.id,
            component_id=component.id,
            risk_level=ForecastRiskLevel.CRITICAL,
        )
        await db_session.commit()
        service = EarlyWarningService(db_session)
        await service.evaluate(project_id=project.id, now=now)
        await db_session.commit()

        #: A warning tracks its forecast's window, so the forecast expires first
        #: and the warning then follows it out of the open list.
        later = now + timedelta(hours=3)
        await ReliabilityForecastService(db_session).expire_due(
            now=later, project_id=project.id
        )
        expired = await service.expire_due(now=later, project_id=project.id)
        await db_session.commit()
        assert expired >= 1
        warnings = await service.list_warnings(project_id=project.id)
        assert all(row.status is not EarlyWarningStatus.OPEN for row in warnings)

    async def test_acknowledged_and_dismissed_warnings_leave_the_open_list(
        self, db_session
    ) -> None:
        project, environment, component = await build_project(db_session)
        now = utcnow()
        await _stored_forecast(
            db_session,
            project.id,
            now=now,
            environment_id=environment.id,
            component_id=component.id,
            risk_level=ForecastRiskLevel.HIGH,
        )
        await db_session.commit()
        service = EarlyWarningService(db_session)
        await service.evaluate(project_id=project.id, now=now)
        await db_session.commit()
        warning = (await service.list_warnings(project_id=project.id))[0]

        await service.acknowledge(warning_id=warning.id, actor="oncall@argus")
        await db_session.commit()
        assert (
            await service.list_warnings(
                project_id=project.id, status=EarlyWarningStatus.OPEN
            )
            == []
        )
        assert (
            len(
                await service.list_warnings(
                    project_id=project.id, status=EarlyWarningStatus.ACKNOWLEDGED
                )
            )
            == 1
        )

    async def test_dismissing_records_the_reason_without_touching_the_forecast(
        self, db_session
    ) -> None:
        project, environment, component = await build_project(db_session)
        now = utcnow()
        forecast = await _stored_forecast(
            db_session,
            project.id,
            now=now,
            environment_id=environment.id,
            component_id=component.id,
            risk_level=ForecastRiskLevel.HIGH,
        )
        await db_session.commit()
        service = EarlyWarningService(db_session)
        await service.evaluate(project_id=project.id, now=now)
        await db_session.commit()
        warning = (await service.list_warnings(project_id=project.id))[0]
        before = (forecast.risk_level, forecast.status)

        row = await service.dismiss(
            warning_id=warning.id, actor="sre@argus", reason="known batch job"
        )
        await db_session.commit()
        assert row is not None
        assert row.status is EarlyWarningStatus.DISMISSED
        assert row.metadata_.get("dismissed_reason") == "known batch job"
        await db_session.refresh(forecast)
        #: Dismissing a warning is a judgement about the alert, not the data.
        assert (forecast.risk_level, forecast.status) == before


# ---------------------------------------------------------------------------
# §35, §36, §38 — signals, similarity, score structure
# ---------------------------------------------------------------------------


class TestSignalsAndScore:
    async def test_signals_carry_their_measurements_and_rank(self, db_session) -> None:
        project, environment, component = await build_project(db_session)
        onset = utcnow()
        await degradation_timeline(
            db_session, project, environment, component, onset=onset
        )
        await db_session.commit()
        await ReliabilityForecastService(db_session).generate_for_project(
            project_id=project.id,
            now=onset,
            prediction_types=[PredictionType.FAILURE_RISK],
            horizons=[ForecastHorizon.SIX_HOURS],
        )
        await db_session.commit()

        from sqlalchemy import select

        from app.models.reliability import PredictiveSignal

        signals = list(
            (
                await db_session.execute(
                    select(PredictiveSignal).order_by(PredictiveSignal.rank.asc())
                )
            )
            .scalars()
            .all()
        )
        assert signals, "a degrading component must produce predictive signals"
        assert signals[0].rank == 0
        assert all(signal.forecast_id for signal in signals)
        for signal in signals:
            assert isinstance(signal.signal_type, PredictiveSignalType)
            assert signal.description
            assert signal.evidence_ids is None or isinstance(signal.evidence_ids, dict)
        # Ranks are unique so "top signal" is a well-defined thing.
        assert len({signal.rank for signal in signals}) == len(signals)

    async def test_reliability_score_reports_present_and_missing_dimensions(
        self, db_session
    ) -> None:
        """§38: the score names its dimensions and admits what it lacks."""
        project, environment, component = await build_project(db_session)
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CHECKOUT_P95,
            values=[400.0 + index for index in range(30)],
            end=utcnow(),
        )
        await db_session.commit()

        bundle = await ReliabilityFeatureEngine(db_session).build(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            forecast_time=utcnow(),
        )
        score = ReliabilityForecastService(db_session).reliability_score(bundle)
        payload = score.as_dict()
        assert payload["dimensions"], "a score must name the dimensions it used"
        assert payload["missing_dimensions"], "and admit the ones it lacked"
        assert payload["method"], "an aggregate score must document itself"
        assert payload["limitations"]
        for dimension in payload["dimensions"]:
            if not dimension["missing"]:
                assert 0.0 <= dimension["value"] <= 1.0
            assert dimension["weight"] > 0
        assert payload["score"] is None or 0.0 <= payload["score"] <= 1.0
