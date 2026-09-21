"""Phase 8 — feature engineering, statistics and time-series behaviour.

Two layers are pinned here:

* the **pure math** (§64) — every trend shape the phase names, including the
  awkward ones: noisy, missing, duplicate and sparse data;
* the **engine** (§7–§18) — that it reads only what lies before the forecast
  time, that missing metrics stay missing rather than becoming zero, and that
  the data-quality verdict degrades honestly.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.reliability import FeatureTrend, ForecastDataQuality
from app.services import reliability_stats as stats
from app.services.reliability_features import (
    CANONICAL_METRICS,
    FEATURE_SCHEMA_VERSION,
    ReliabilityFeatureEngine,
    canonical_metric_key,
)
from tests.phase8_helpers import (
    METRIC_CHECKOUT_ERROR_RATE,
    METRIC_CHECKOUT_P95,
    METRIC_CPU,
    emit_error_logs,
    emit_metric_series,
    emit_spans,
    utcnow,
)


# ---------------------------------------------------------------------------
# §64 — time-series shapes
# ---------------------------------------------------------------------------


class TestTimeSeriesShapes:
    """Every trend shape the spec names, with deterministic inputs."""

    def test_increasing_trend_is_rising(self):
        assert stats.classify_trend([400, 450, 520, 610, 720]) is FeatureTrend.RISING

    def test_decreasing_trend_is_falling(self):
        assert stats.classify_trend([720, 610, 520, 450, 400]) is FeatureTrend.FALLING

    def test_flat_trend_is_flat(self):
        assert stats.classify_trend([500, 500, 500, 500, 500]) is FeatureTrend.FLAT

    def test_noisy_trend_without_direction_is_volatile(self):
        """Oscillation with no net slope is VOLATILE, not RISING (§64)."""
        assert stats.classify_trend([10, 30, 12, 34, 11, 33]) is FeatureTrend.VOLATILE

    def test_strong_slope_inside_noise_is_still_rising(self):
        """A steep trend is directional even when the series is noisy."""
        values = [10, 26, 44, 58, 76, 92, 110]
        assert stats.classify_trend(values) is FeatureTrend.RISING

    def test_empty_series_is_unknown_not_flat(self):
        """No data and no movement are different claims (§18, §64)."""
        assert stats.classify_trend([]) is FeatureTrend.UNKNOWN
        assert stats.classify_trend([500]) is FeatureTrend.UNKNOWN

    def test_missing_samples_are_skipped_not_zeroed(self):
        """A gap must not read as a drop to zero (§8, §64)."""
        with_gap = [500.0, None, 510.0, None, 520.0]
        #: The mean is over the three real observations, not over three values
        #: and two zeros.
        assert stats.mean(with_gap) == pytest.approx(510.0)
        profile = stats.profile_series("m", with_gap)
        assert profile.sample_count == 3
        #: A strictly increasing series is RISING once the move clears the
        #: declared flat band; assert on a series that does.
        assert stats.classify_trend([500.0, None, 560.0, None, 640.0]) is (
            FeatureTrend.RISING
        )
        #: Below the flat band the trend is deliberately reported as flat, and
        #: the gaps do not change that.
        assert stats.classify_trend(with_gap) is FeatureTrend.FLAT

    def test_sparse_series_still_reports_insufficient_samples(self):
        profile = stats.profile_series("m", [500.0, 520.0], min_samples=5)
        assert profile.sample_count == 2
        assert profile.sample_sufficient is False
        assert profile.trend is FeatureTrend.RISING

    def test_duplicate_samples_are_counted_as_observations(self):
        """Duplicate rows are real observations, not a data-quality coin flip."""
        profile = stats.profile_series("m", [500.0, 500.0, 500.0, 500.0, 500.0])
        assert profile.sample_count == 5
        assert profile.trend is FeatureTrend.FLAT
        assert profile.volatility == pytest.approx(0.0)

    def test_slope_direction_and_magnitude(self):
        assert stats.slope([100, 200, 300]) == pytest.approx(100.0)
        assert stats.normalized_slope([100, 200, 300]) == pytest.approx(0.5)
        assert stats.slope([1.0]) is None

    def test_ewma_smooths_and_skips_gaps(self):
        series = stats.ewma([10.0, None, 20.0], 0.5)
        assert series[0] == pytest.approx(10.0)
        #: A missing sample repeats the previous state rather than resetting.
        assert series[1] == pytest.approx(10.0)
        assert series[2] == pytest.approx(15.0)

    def test_ewma_rejects_an_invalid_alpha(self):
        with pytest.raises(ValueError):
            stats.ewma([1.0, 2.0], 0.0)

    def test_change_rate_is_none_against_a_zero_baseline(self):
        """A ratio against zero is not a measurement."""
        assert stats.change_rate(5.0, 0.0) is None
        assert stats.change_rate(15.0, 10.0) == pytest.approx(0.5)
        assert stats.change_rate(None, 10.0) is None

    def test_projection_is_bounded(self):
        """§20: a projection may not run away from the observed data."""
        projected = stats.bounded_linear_projection(
            [100, 200, 300], steps_ahead=50, max_growth_ratio=3.0
        )
        assert projected is not None
        assert projected <= max(300.0, 200.0 * 3.0)

    def test_saturation_requires_a_configured_ceiling(self):
        assert stats.is_saturated(95.0, ceiling=100.0, saturation_ratio=0.85)
        assert not stats.is_saturated(50.0, ceiling=100.0, saturation_ratio=0.85)
        #: No ceiling configured means no saturation claim may be made.
        assert not stats.is_saturated(95.0, ceiling=None, saturation_ratio=0.85)
        assert not stats.is_saturated(None, ceiling=100.0, saturation_ratio=0.85)


# ---------------------------------------------------------------------------
# §8 — canonical metric mapping
# ---------------------------------------------------------------------------


class TestCanonicalMetricMapping:
    def test_latency_percentiles_map_to_distinct_families(self):
        assert canonical_metric_key("http.checkout.latency.p95") == "latency_p95"
        assert canonical_metric_key("http.checkout.latency.p99") == "latency_p99"
        assert canonical_metric_key("http.checkout.latency.p50") == "latency_p50"

    def test_unmapped_metric_returns_none_rather_than_guessing(self):
        assert canonical_metric_key("some.custom.counter") is None
        assert canonical_metric_key("") is None

    def test_known_families_map(self):
        assert canonical_metric_key("http.checkout.error_rate") == "error_rate"
        assert canonical_metric_key("system.cpu.utilization") == "cpu_utilization"
        assert canonical_metric_key("system.queue.depth") == "queue_depth"

    def test_every_canonical_key_is_reachable_by_some_name(self):
        """A canonical family nobody can produce would be dead weight."""
        probes = {
            "request_rate": "http.requests.rate",
            "error_rate": "http.error_rate",
            "latency_p50": "a.latency.p50",
            "latency_p95": "a.latency.p95",
            "latency_p99": "a.latency.p99",
            "cpu_utilization": "system.cpu.utilization",
            "memory_utilization": "system.memory.utilization",
            "disk_utilization": "system.disk.utilization",
            "queue_depth": "system.queue.depth",
            "connection_pool_usage": "db.connection.pool.usage",
        }
        for family, probe in probes.items():
            assert canonical_metric_key(probe) == family
            assert family in CANONICAL_METRICS


# ---------------------------------------------------------------------------
# §7, §17 — the feature engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestReliabilityFeatureEngine:
    async def _scope(self, db_session):
        from tests.phase6_helpers import build_project

        return await build_project(db_session, name="Phase8 Features")

    async def test_rising_latency_produces_a_rising_profile(self, db_session):
        project, environment, component = await self._scope(db_session)
        now = utcnow()
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CHECKOUT_P95,
            values=[400, 450, 520, 610, 720],
            end=now,
        )
        bundle = await ReliabilityFeatureEngine(db_session).build(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            component_name=component.name,
            forecast_time=now,
        )
        assert bundle.trend("latency_p95") is FeatureTrend.RISING
        assert bundle.value("latency_p95_current") == pytest.approx(720.0)
        assert bundle.value("latency_p95_slope") is not None
        assert bundle.value("latency_p95_slope") > 0

    async def test_metrics_after_the_forecast_time_are_invisible(self, db_session):
        """The leakage boundary, at the feature layer (§30, §65)."""
        project, environment, component = await self._scope(db_session)
        now = utcnow()
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CHECKOUT_P95,
            values=[400, 410, 420, 430, 440],
            end=now,
        )
        #: A future spike that must not influence the forecast at ``now``.
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CHECKOUT_P95,
            values=[9000.0, 9500.0],
            end=now + timedelta(hours=2),
        )
        bundle = await ReliabilityFeatureEngine(db_session).build(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            forecast_time=now,
        )
        assert bundle.value("latency_p95_current") == pytest.approx(440.0)
        assert bundle.value("latency_p95_max") == pytest.approx(440.0)

    async def test_absent_metric_stays_none_not_zero(self, db_session):
        project, environment, component = await self._scope(db_session)
        now = utcnow()
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CHECKOUT_P95,
            values=[400, 410, 420, 430, 440],
            end=now,
        )
        bundle = await ReliabilityFeatureEngine(db_session).build(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            forecast_time=now,
        )
        #: CPU was never reported; "unknown" is the honest value.
        assert bundle.value("cpu_utilization_current") is None
        assert bundle.value("cpu_utilization_mean") is None

    async def test_no_telemetry_at_all_is_insufficient(self, db_session):
        """§69: a component with no history must not read as low risk."""
        project, environment, component = await self._scope(db_session)
        bundle = await ReliabilityFeatureEngine(db_session).build(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            forecast_time=utcnow(),
        )
        assert bundle.quality is ForecastDataQuality.INSUFFICIENT
        assert bundle.coverage == 0.0
        assert any("no telemetry" in note for note in bundle.quality_notes)

    async def test_telemetry_that_stopped_arriving_is_not_good_quality(
        self, db_session
    ):
        """§42: a broken pipeline must not look like stability."""
        project, environment, component = await self._scope(db_session)
        now = utcnow()
        stale_at = now - timedelta(hours=6)
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CHECKOUT_P95,
            values=[400, 410, 420, 430, 440],
            end=stale_at,
        )
        bundle = await ReliabilityFeatureEngine(db_session).build(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            forecast_time=now,
        )
        #: The samples fall outside the one-hour analysis window, and their age
        #: is measured explicitly rather than inferred from an empty window.
        assert bundle.value("telemetry_is_stale") == pytest.approx(1.0)
        assert bundle.value("telemetry_staleness_seconds") == pytest.approx(
            6 * 3600, abs=60
        )
        assert bundle.quality is ForecastDataQuality.POOR
        assert any("stale" in note for note in bundle.quality_notes)

    async def test_fresh_telemetry_is_not_flagged_stale(self, db_session):
        project, environment, component = await self._scope(db_session)
        now = utcnow()
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CHECKOUT_P95,
            values=[400, 410, 420, 430, 440],
            end=now,
        )
        bundle = await ReliabilityFeatureEngine(db_session).build(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            forecast_time=now,
        )
        assert bundle.value("telemetry_is_stale") == pytest.approx(0.0)

    async def test_all_telemetry_families_give_good_quality(self, db_session):
        project, environment, component = await self._scope(db_session)
        now = utcnow()
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CHECKOUT_P95,
            values=[400, 410, 420, 430, 440],
            end=now,
        )
        await emit_error_logs(
            db_session, project, environment, component, count=3, end=now
        )
        await emit_spans(
            db_session,
            project,
            component,
            durations_ms=[100, 110, 120, 130],
            end=now,
        )
        bundle = await ReliabilityFeatureEngine(db_session).build(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            forecast_time=now,
        )
        assert bundle.quality is ForecastDataQuality.GOOD
        assert bundle.coverage == pytest.approx(1.0)
        assert bundle.sources["telemetry_families_present"] == 3

    async def test_error_features_count_only_before_the_forecast_time(self, db_session):
        project, environment, component = await self._scope(db_session)
        now = utcnow()
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CHECKOUT_ERROR_RATE,
            values=[0.01, 0.02, 0.03, 0.04, 0.05],
            end=now,
        )
        await emit_error_logs(
            db_session, project, environment, component, count=4, end=now
        )
        await emit_error_logs(
            db_session,
            project,
            environment,
            component,
            count=50,
            end=now + timedelta(hours=3),
        )
        bundle = await ReliabilityFeatureEngine(db_session).build(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            forecast_time=now,
        )
        assert bundle.value("error_log_count") == pytest.approx(4.0)
        assert bundle.value("errors_last_1h") == pytest.approx(4.0)

    async def test_snapshot_payload_is_serialisable_and_versioned(self, db_session):
        """§17: the stored snapshot is what makes a forecast reproducible."""
        project, environment, component = await self._scope(db_session)
        now = utcnow()
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CPU,
            values=[10, 20, 30, 40, 50],
            end=now,
            unit="percent",
        )
        bundle = await ReliabilityFeatureEngine(db_session).build(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            forecast_time=now,
        )
        payload = bundle.as_snapshot_payload()
        assert payload["schema_version"] == FEATURE_SCHEMA_VERSION
        assert payload["numeric"]["cpu_utilization_current"] == pytest.approx(50.0)
        assert payload["trends"]["cpu_utilization"] == FeatureTrend.RISING.value
        import json

        #: Round-tripping proves it can be stored in the JSONB column as-is.
        assert json.loads(json.dumps(payload))["schema_version"] == (
            FEATURE_SCHEMA_VERSION
        )

    async def test_resource_saturation_uses_configured_ceilings(self, db_session):
        project, environment, component = await self._scope(db_session)
        now = utcnow()
        #: 95% CPU with a 100% ceiling and 0.85 saturation ratio.
        await emit_metric_series(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_CPU,
            values=[80, 85, 90, 93, 95],
            end=now,
            unit="percent",
        )
        bundle = await ReliabilityFeatureEngine(db_session).build(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            forecast_time=now,
        )
        assert bundle.value("cpu_utilization_saturated") == pytest.approx(1.0)
        assert bundle.value("resource_saturation_rate") == pytest.approx(1.0)

    async def test_scope_is_required(self, db_session):
        with pytest.raises(ValueError):
            await ReliabilityFeatureEngine(db_session).build(
                project_id=None,
                environment_id=None,
                component_id=None,
                forecast_time=utcnow(),
            )
