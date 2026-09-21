"""Phase 8 — forecast generation, dedup, lifecycle and leakage.

The leakage tests here are mandatory (§65): they construct an incident *after*
the forecast instant and assert it changes nothing about what was predicted.
They are written against real rows rather than mocked features, because the
boundary being tested is a SQL filter and only a real query can prove it holds.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.reliability import (
    ForecastDataQuality,
    ForecastFailureReason,
    ForecastFingerprint,
    ForecastHorizon,
    ForecastRiskLevel,
    ForecastStatus,
    PredictionType,
    PredictiveSignal,
    ReliabilityForecast,
    ReliabilityModelVersion,
)
from app.services.reliability_forecast_service import (
    ReliabilityForecastService,
    clear_forecast_hooks,
    forecast_fingerprint,
    register_forecast_hook,
)
from app.services.reliability_risk import risk_policy
from tests.phase8_helpers import (
    METRIC_CHECKOUT_ERROR_RATE,
    METRIC_CHECKOUT_P95,
    degradation_timeline,
    emit_incident,
    emit_metric_series,
    utcnow,
)

TYPES = [PredictionType.LATENCY_RISK, PredictionType.ERROR_RATE_RISK]
HORIZONS = [ForecastHorizon.ONE_HOUR, ForecastHorizon.SIX_HOURS]


@pytest.mark.asyncio
class TestForecastGeneration:
    async def _scope(self, db_session):
        from tests.phase6_helpers import build_project

        return await build_project(db_session, name="Phase8 Forecast")

    async def test_degradation_produces_elevated_risk(self, db_session):
        """The §72 shape must predict elevated risk *before* the incident."""
        project, environment, component = await self._scope(db_session)
        onset = utcnow()
        await degradation_timeline(
            db_session, project, environment, component, onset=onset
        )
        service = ReliabilityForecastService(db_session)
        result = await service.generate_for_scope(
            scope=await self._single_scope(service, project, environment, component),
            now=onset,
            prediction_types=TYPES,
            horizons=HORIZONS,
        )
        assert result.forecasts_created == len(TYPES) * len(HORIZONS)
        assert not result.errors
        forecasts = (
            (
                await db_session.execute(
                    select(ReliabilityForecast).where(
                        ReliabilityForecast.prediction_type
                        == PredictionType.LATENCY_RISK,
                        ReliabilityForecast.forecast_horizon
                        == ForecastHorizon.SIX_HOURS,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(forecasts) == 1
        forecast = forecasts[0]
        assert forecast.risk_level in (
            ForecastRiskLevel.MEDIUM,
            ForecastRiskLevel.HIGH,
            ForecastRiskLevel.CRITICAL,
        )
        assert forecast.risk_score is not None
        assert 0.0 <= forecast.risk_score <= 1.0
        assert forecast.failure_reason is None

    async def test_forecast_is_reproducible_from_its_snapshot(self, db_session):
        """§17/§82: stored features + model must explain the stored score."""
        project, environment, component = await self._scope(db_session)
        onset = utcnow()
        await degradation_timeline(
            db_session, project, environment, component, onset=onset
        )
        service = ReliabilityForecastService(db_session)
        scope = await self._single_scope(service, project, environment, component)
        await service.generate_for_scope(
            scope=scope,
            now=onset,
            prediction_types=[PredictionType.LATENCY_RISK],
            horizons=[ForecastHorizon.SIX_HOURS],
        )
        forecast = (
            (await db_session.execute(select(ReliabilityForecast).limit(1)))
            .scalars()
            .one()
        )
        from app.models.reliability import ForecastFeatureSnapshot

        snapshot = (
            await db_session.execute(
                select(ForecastFeatureSnapshot).where(
                    ForecastFeatureSnapshot.id == forecast.feature_snapshot_id
                )
            )
        ).scalar_one()
        #: The snapshot is bounded by the forecast instant, which is what makes
        #: a stored prediction replayable. SQLite hands back naive datetimes, so
        #: the comparison normalizes — the same convention the sweeps use.
        from app.services.reliability_features import aware_utc

        assert aware_utc(snapshot.forecast_time) == onset
        assert aware_utc(snapshot.feature_window_end) == onset
        assert snapshot.feature_values["schema_version"] == "v1"
        assert snapshot.data_quality == forecast.data_quality
        assert forecast.model_version_label.startswith("rolling_trend/")

    async def test_signals_are_persisted_ranked_and_predictive(self, db_session):
        project, environment, component = await self._scope(db_session)
        onset = utcnow()
        await degradation_timeline(
            db_session, project, environment, component, onset=onset
        )
        service = ReliabilityForecastService(db_session)
        scope = await self._single_scope(service, project, environment, component)
        await service.generate_for_scope(
            scope=scope,
            now=onset,
            prediction_types=[PredictionType.LATENCY_RISK],
            horizons=[ForecastHorizon.SIX_HOURS],
        )
        forecast = (
            (await db_session.execute(select(ReliabilityForecast))).scalars().one()
        )
        signals = (
            (
                await db_session.execute(
                    select(PredictiveSignal)
                    .where(PredictiveSignal.forecast_id == forecast.id)
                    .order_by(PredictiveSignal.rank.asc())
                )
            )
            .scalars()
            .all()
        )
        assert signals
        assert [signal.rank for signal in signals] == list(range(len(signals)))
        assert forecast.dominant_signal is not None
        #: §6: signals describe evidence of increasing risk, never causation.
        for signal in signals:
            assert signal.contribution is None or 0.0 <= signal.contribution <= 1.0
            assert "cause" not in signal.description.lower()

    async def test_no_telemetry_produces_an_explicit_refusal(self, db_session):
        """§69/§81: no history means UNKNOWN with a stated reason, not LOW."""
        project, environment, component = await self._scope(db_session)
        service = ReliabilityForecastService(db_session)
        scope = await self._single_scope(service, project, environment, component)
        result = await service.generate_for_scope(
            scope=scope,
            now=utcnow(),
            prediction_types=[PredictionType.FAILURE_RISK],
            horizons=[ForecastHorizon.ONE_HOUR],
        )
        assert result.refusals == 1
        forecast = (
            (await db_session.execute(select(ReliabilityForecast))).scalars().one()
        )
        assert forecast.risk_level is ForecastRiskLevel.UNKNOWN
        assert forecast.risk_score is None
        assert forecast.failure_reason is ForecastFailureReason.INSUFFICIENT_DATA
        assert "insufficient" in forecast.headline.lower()
        assert forecast.limitations

    async def test_risk_level_follows_the_central_policy(self, db_session):
        """§5: classification comes from the configured thresholds only."""
        project, environment, component = await self._scope(db_session)
        onset = utcnow()
        await degradation_timeline(
            db_session, project, environment, component, onset=onset
        )
        service = ReliabilityForecastService(db_session)
        scope = await self._single_scope(service, project, environment, component)
        await service.generate_for_scope(
            scope=scope,
            now=onset,
            prediction_types=[PredictionType.LATENCY_RISK],
            horizons=[ForecastHorizon.SIX_HOURS],
        )
        forecast = (
            (await db_session.execute(select(ReliabilityForecast))).scalars().one()
        )
        assert forecast.risk_level is risk_policy().classify(forecast.risk_score)

    async def test_model_version_is_registered_and_referenced(self, db_session):
        """§25/§82: every forecast names a stored model version."""
        project, environment, component = await self._scope(db_session)
        onset = utcnow()
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CHECKOUT_P95,
            values=[400, 450, 520, 610, 720],
            end=onset,
        )
        service = ReliabilityForecastService(db_session)
        scope = await self._single_scope(service, project, environment, component)
        await service.generate_for_scope(
            scope=scope,
            now=onset,
            prediction_types=[PredictionType.LATENCY_RISK],
            horizons=[ForecastHorizon.SIX_HOURS],
        )
        forecast = (
            (await db_session.execute(select(ReliabilityForecast))).scalars().one()
        )
        models = (
            (await db_session.execute(select(ReliabilityModelVersion))).scalars().all()
        )
        assert models
        assert forecast.model_version_id in {model.id for model in models}
        assert forecast.model_version_label == (
            f"{forecast.model_version_id and 'rolling_trend'}/1"
        )

    async def test_no_incident_is_created_by_a_forecast(self, db_session):
        """§5/§87: prediction must never open an incident or remediate."""
        project, environment, component = await self._scope(db_session)
        onset = utcnow()
        await degradation_timeline(
            db_session, project, environment, component, onset=onset
        )
        service = ReliabilityForecastService(db_session)
        scope = await self._single_scope(service, project, environment, component)
        await service.generate_for_scope(scope=scope, now=onset)
        from app.models.incident import Incident

        incidents = (await db_session.execute(select(Incident))).scalars().all()
        assert incidents == []

    async def test_forecast_event_is_emitted(self, db_session):
        """§26 step 11: creation is observable."""
        project, environment, component = await self._scope(db_session)
        onset = utcnow()
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CHECKOUT_P95,
            values=[400, 450, 520, 610, 720],
            end=onset,
        )
        events: list[dict] = []
        clear_forecast_hooks()
        register_forecast_hook(events.append)
        try:
            service = ReliabilityForecastService(db_session)
            scope = await self._single_scope(service, project, environment, component)
            await service.generate_for_scope(
                scope=scope,
                now=onset,
                prediction_types=[PredictionType.LATENCY_RISK],
                horizons=[ForecastHorizon.ONE_HOUR],
            )
        finally:
            clear_forecast_hooks()
        assert len(events) == 1
        assert events[0]["prediction_type"] == "LATENCY_RISK"
        assert events[0]["forecast_horizon"] == "ONE_HOUR"

    async def _single_scope(self, service, project, environment, component):
        from app.services.reliability_forecast_service import ForecastScope

        return ForecastScope(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            component_name=component.name,
        )


@pytest.mark.asyncio
class TestLeakagePrevention:
    """§65 — a future incident must not influence an earlier forecast."""

    async def test_future_incident_does_not_change_the_prediction(self, db_session):
        from tests.phase6_helpers import build_project

        project, environment, component = await build_project(
            db_session, name="Phase8 Leakage"
        )
        onset = utcnow()
        await degradation_timeline(
            db_session, project, environment, component, onset=onset
        )
        service = ReliabilityForecastService(db_session)
        scope = await service.eligible_scopes(
            project_id=project.id, now=onset, lookback_seconds=7_200
        )
        assert scope

        await service.generate_for_scope(
            scope=scope[0],
            now=onset,
            prediction_types=[PredictionType.FAILURE_RISK],
            horizons=[ForecastHorizon.SIX_HOURS],
        )
        baseline = (
            (await db_session.execute(select(ReliabilityForecast))).scalars().one()
        )
        baseline_score = baseline.risk_score
        baseline_snapshot = baseline.feature_snapshot_id
        baseline_evidence = dict(baseline.supporting_evidence or {})

        #: Everything below happens *after* the forecast instant.
        await emit_incident(
            db_session,
            project,
            environment,
            component,
            detected_at=onset + timedelta(hours=1),
            severity="CRITICAL",
        )
        from app.models.observability import MetricRecord, MetricType

        db_session.add(
            MetricRecord(
                project_id=project.id,
                environment_id=environment.id,
                component_id=component.id,
                timestamp=onset + timedelta(minutes=20),
                metric_name=METRIC_CHECKOUT_ERROR_RATE,
                metric_type=MetricType.GAUGE,
                value=0.99,
            )
        )
        await db_session.flush()

        #: Re-forecasting at the *same* instant must see the identical evidence.
        from app.services.reliability_features import ReliabilityFeatureEngine

        bundle = await ReliabilityFeatureEngine(db_session).build(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            component_name=component.name,
            forecast_time=onset,
        )
        assert bundle.value("error_rate_current") is not None
        #: The spike at +20 minutes is not the current value.
        assert bundle.value("error_rate_current") < 0.5
        assert bundle.value("incidents_last_24h") == 0.0
        assert bundle.value("incidents_last_7d") == 0.0
        assert bundle.value("open_incident_count") == 0.0
        assert bundle.value("time_since_last_incident_seconds") is None

        await service.generate_for_scope(
            scope=scope[0],
            now=onset,
            prediction_types=[PredictionType.FAILURE_RISK],
            horizons=[ForecastHorizon.SIX_HOURS],
        )
        #: The refresh path updated the same logical forecast; the score and the
        #: evidence it rests on are unchanged by the future incident.
        refreshed = (
            (await db_session.execute(select(ReliabilityForecast))).scalars().one()
        )
        assert refreshed.risk_score == baseline_score
        assert refreshed.supporting_evidence == baseline_evidence
        assert refreshed.feature_snapshot_id != baseline_snapshot

    async def test_incident_before_the_instant_is_visible(self, db_session):
        """The boundary must exclude the future, not all history."""
        from tests.phase6_helpers import build_project

        project, environment, component = await build_project(
            db_session, name="Phase8 Leakage Negative"
        )
        onset = utcnow()
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CHECKOUT_P95,
            values=[400, 410, 420, 430, 440],
            end=onset,
        )
        await emit_incident(
            db_session,
            project,
            environment,
            component,
            detected_at=onset - timedelta(hours=3),
        )
        from app.services.reliability_features import ReliabilityFeatureEngine

        bundle = await ReliabilityFeatureEngine(db_session).build(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            forecast_time=onset,
        )
        assert bundle.value("incidents_last_24h") == pytest.approx(1.0)
        assert bundle.value("time_since_last_incident_seconds") == pytest.approx(
            3 * 3600, abs=5
        )


@pytest.mark.asyncio
class TestDeduplicationAndLifecycle:
    async def _scope(self, db_session):
        from tests.phase6_helpers import build_project

        return await build_project(db_session, name="Phase8 Dedup")

    async def _seed(self, db_session, project, environment, component, onset):
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CHECKOUT_P95,
            values=[400, 450, 520, 610, 720],
            end=onset,
        )

    async def test_repeated_generation_inside_the_window_updates_in_place(
        self, db_session
    ):
        """§40: a sweep must not emit a new row for the same trend."""
        project, environment, component = await self._scope(db_session)
        onset = utcnow()
        await self._seed(db_session, project, environment, component, onset)
        service = ReliabilityForecastService(db_session)
        scope = await service.eligible_scopes(project_id=project.id, now=onset)
        assert scope

        first = await service.generate_for_scope(
            scope=scope[0],
            now=onset,
            prediction_types=[PredictionType.LATENCY_RISK],
            horizons=[ForecastHorizon.SIX_HOURS],
        )
        second = await service.generate_for_scope(
            scope=scope[0],
            now=onset + timedelta(seconds=30),
            prediction_types=[PredictionType.LATENCY_RISK],
            horizons=[ForecastHorizon.SIX_HOURS],
        )
        assert first.forecasts_created == 1
        assert second.forecasts_updated == 1
        assert second.forecasts_created == 0
        rows = (await db_session.execute(select(ReliabilityForecast))).scalars().all()
        assert len(rows) == 1
        assert rows[0].revision == 1

    async def test_a_materially_new_prediction_becomes_a_revision(self, db_session):
        """History is preserved when the prediction genuinely changes."""
        project, environment, component = await self._scope(db_session)
        onset = utcnow()
        await self._seed(db_session, project, environment, component, onset)
        service = ReliabilityForecastService(db_session)
        scope = await service.eligible_scopes(project_id=project.id, now=onset)
        await service.generate_for_scope(
            scope=scope[0],
            now=onset,
            prediction_types=[PredictionType.LATENCY_RISK],
            horizons=[ForecastHorizon.SIX_HOURS],
        )
        #: Far outside the refresh window: a new revision, both rows retained.
        later = onset + timedelta(hours=2)
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CHECKOUT_P95,
            values=[1200, 1500, 1800, 2100, 2400],
            end=later,
        )
        result = await service.generate_for_scope(
            scope=scope[0],
            now=later,
            prediction_types=[PredictionType.LATENCY_RISK],
            horizons=[ForecastHorizon.SIX_HOURS],
        )
        assert result.forecasts_revised == 1
        rows = (
            (
                await db_session.execute(
                    select(ReliabilityForecast).order_by(
                        ReliabilityForecast.revision.asc()
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert [row.revision for row in rows] == [1, 2]
        assert rows[1].previous_forecast_id == rows[0].id

    async def test_fingerprint_registry_tracks_the_current_revision(self, db_session):
        project, environment, component = await self._scope(db_session)
        onset = utcnow()
        await self._seed(db_session, project, environment, component, onset)
        service = ReliabilityForecastService(db_session)
        scope = await service.eligible_scopes(project_id=project.id, now=onset)
        await service.generate_for_scope(
            scope=scope[0],
            now=onset,
            prediction_types=[PredictionType.LATENCY_RISK],
            horizons=[ForecastHorizon.SIX_HOURS],
        )
        prints = (await db_session.execute(select(ForecastFingerprint))).scalars().all()
        forecast = (
            (await db_session.execute(select(ReliabilityForecast))).scalars().one()
        )
        assert len(prints) == 1
        assert prints[0].current_forecast_id == forecast.id
        assert prints[0].fingerprint == forecast.fingerprint

    async def test_different_signals_are_different_fingerprints(self):
        """§40: a latency-led and an error-led forecast are different claims."""
        latency = forecast_fingerprint(
            project_id="p",
            environment_id="e",
            component_id="c",
            prediction_type=PredictionType.FAILURE_RISK,
            horizon=ForecastHorizon.SIX_HOURS,
            dominant_signal="LATENCY_INCREASING",
        )
        errors = forecast_fingerprint(
            project_id="p",
            environment_id="e",
            component_id="c",
            prediction_type=PredictionType.FAILURE_RISK,
            horizon=ForecastHorizon.SIX_HOURS,
            dominant_signal="ERROR_RATE_INCREASING",
        )
        assert latency != errors

    async def test_expiry_moves_past_windows_to_expired(self, db_session):
        """§27: a passed horizon is EXPIRED, not silently still active."""
        project, environment, component = await self._scope(db_session)
        onset = utcnow()
        await self._seed(db_session, project, environment, component, onset)
        service = ReliabilityForecastService(db_session)
        scope = await service.eligible_scopes(project_id=project.id, now=onset)
        await service.generate_for_scope(
            scope=scope[0],
            now=onset,
            prediction_types=[PredictionType.LATENCY_RISK],
            horizons=[ForecastHorizon.ONE_HOUR],
        )
        expired = await service.expire_due(now=onset + timedelta(hours=2))
        assert expired == 1
        forecast = (
            (await db_session.execute(select(ReliabilityForecast))).scalars().one()
        )
        assert forecast.status is ForecastStatus.EXPIRED

    async def test_expiry_does_not_touch_live_forecasts(self, db_session):
        project, environment, component = await self._scope(db_session)
        onset = utcnow()
        await self._seed(db_session, project, environment, component, onset)
        service = ReliabilityForecastService(db_session)
        scope = await service.eligible_scopes(project_id=project.id, now=onset)
        await service.generate_for_scope(
            scope=scope[0],
            now=onset,
            prediction_types=[PredictionType.LATENCY_RISK],
            horizons=[ForecastHorizon.SEVEN_DAYS],
        )
        assert await service.expire_due(now=onset + timedelta(hours=2)) == 0


@pytest.mark.asyncio
class TestProjectIsolation:
    async def test_forecasts_never_cross_projects(self, db_session):
        """§60: one project's evidence cannot produce another's forecast."""
        from tests.phase6_helpers import build_project

        project_a, env_a, component_a = await build_project(db_session, name="A")
        project_b, env_b, component_b = await build_project(db_session, name="B")
        onset = utcnow()
        await emit_metric_series(
            db_session,
            project_a,
            env_a,
            component_a,
            metric_name=METRIC_CHECKOUT_P95,
            values=[400, 500, 700, 900, 1200],
            end=onset,
        )
        #: B has its own component with no telemetry at all.
        service = ReliabilityForecastService(db_session)
        scopes_b = await service.eligible_scopes(project_id=project_b.id, now=onset)
        assert scopes_b == []

    async def test_component_without_telemetry_is_not_forecast(self, db_session):
        from tests.phase6_helpers import build_project, build_scope

        project, environment, component = await build_project(db_session, name="C")
        _env2, idle_component = await build_scope(
            db_session, project.id, component="idle-service"
        )
        onset = utcnow()
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CHECKOUT_P95,
            values=[400, 450, 520, 610, 720],
            end=onset,
        )
        service = ReliabilityForecastService(db_session)
        scopes = await service.eligible_scopes(project_id=project.id, now=onset)
        assert {scope.component_id for scope in scopes} == {component.id}
        assert idle_component.id not in {scope.component_id for scope in scopes}


@pytest.mark.asyncio
class TestExplanationAndProfile:
    async def test_explanation_reports_all_four_sections(self, db_session):
        """§34: what changed, why, what supports it, what is uncertain."""
        from tests.phase6_helpers import build_project

        project, environment, component = await build_project(
            db_session, name="Phase8 Explain"
        )
        onset = utcnow()
        await degradation_timeline(
            db_session, project, environment, component, onset=onset
        )
        service = ReliabilityForecastService(db_session)
        scope = await service.eligible_scopes(project_id=project.id, now=onset)
        await service.generate_for_scope(
            scope=scope[0],
            now=onset,
            prediction_types=[PredictionType.LATENCY_RISK],
            horizons=[ForecastHorizon.SIX_HOURS],
        )
        forecast = (
            (await db_session.execute(select(ReliabilityForecast))).scalars().one()
        )
        explanation = await service.explain(forecast)
        assert explanation["what_changed"]
        assert explanation["why_risk_increased"]
        assert explanation["what_supports_this"]["signals"]
        assert explanation["what_is_uncertain"]
        assert explanation["model_version"]
        assert explanation["calibration_status"] == "UNKNOWN"
        #: The caveats must say what the numbers are not.
        joined = " ".join(explanation["caveats"]).lower()
        assert "not causal" in joined or "not a causal" in joined

    async def test_refused_forecast_explains_itself(self, db_session):
        from tests.phase6_helpers import build_project

        project, environment, component = await build_project(
            db_session, name="Phase8 Refusal Explain"
        )
        service = ReliabilityForecastService(db_session)
        scope = await service.eligible_scopes(project_id=project.id, now=utcnow())
        from app.services.reliability_forecast_service import ForecastScope

        await service.generate_for_scope(
            scope=ForecastScope(
                project_id=project.id,
                environment_id=environment.id,
                component_id=component.id,
                component_name=component.name,
            ),
            now=utcnow(),
            prediction_types=[PredictionType.FAILURE_RISK],
            horizons=[ForecastHorizon.ONE_HOUR],
        )
        del scope
        forecast = (
            (await db_session.execute(select(ReliabilityForecast))).scalars().one()
        )
        explanation = await service.explain(forecast)
        assert explanation["risk_level"] == "UNKNOWN"
        assert explanation["what_is_uncertain"]
        assert explanation["data_quality"] == ForecastDataQuality.INSUFFICIENT.value

    async def test_component_profile_does_not_create_forecasts(self, db_session):
        """§37: a profile is a read-only projection."""
        from tests.phase6_helpers import build_project

        project, environment, component = await build_project(
            db_session, name="Phase8 Profile"
        )
        onset = utcnow()
        await degradation_timeline(
            db_session, project, environment, component, onset=onset
        )
        service = ReliabilityForecastService(db_session)
        profile = await service.component_profile(
            project_id=project.id,
            component_id=component.id,
            environment_id=environment.id,
            now=onset,
        )
        assert profile["component_name"] == component.name
        assert profile["reliability_score"]["method"]
        assert (
            await db_session.execute(select(ReliabilityForecast))
        ).scalars().all() == []
        assert profile["limitations"]

    async def test_reliability_score_marks_missing_dimensions(self, db_session):
        from tests.phase6_helpers import build_project
        from app.services.reliability_features import ReliabilityFeatureEngine

        project, environment, component = await build_project(
            db_session, name="Phase8 Score"
        )
        onset = utcnow()
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CHECKOUT_P95,
            values=[400, 450, 520, 610, 720],
            end=onset,
        )
        bundle = await ReliabilityFeatureEngine(db_session).build(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            forecast_time=onset,
        )
        score = ReliabilityForecastService(db_session).reliability_score(bundle)
        payload = score.as_dict()
        assert payload["method"]
        #: Dimensions with no data are reported missing, never defaulted healthy.
        assert payload["missing_dimensions"]
        assert all(
            dimension["missing"] == (dimension["value"] is None)
            for dimension in payload["dimensions"]
        )
