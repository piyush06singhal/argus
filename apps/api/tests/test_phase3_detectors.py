"""Phase 3 — deterministic detector unit tests (§11–§16).

Every detector is asserted on three paths: fires, does not fire, and refuses to
guess when data is missing. The refusal cases matter most — a detector that
treats absent data as anomalous generates incidents out of silence.
"""

from __future__ import annotations

import pytest

from app.models.anomaly import AnomalySource, AnomalyType
from app.services.baseline import StaticBaseline
from app.services.detectors import (
    PLACEHOLDER_EMAIL,
    PLACEHOLDER_HEX,
    PLACEHOLDER_IP,
    PLACEHOLDER_NUM,
    PLACEHOLDER_UUID,
    detect_baseline_deviation,
    detect_error_rate,
    detect_health_transition,
    detect_latency_ratio,
    detect_pattern_spike,
    detect_rate_change,
    detect_threshold,
    detect_trace_failure_rate,
    detect_z_score,
    direction_for,
    error_rate,
    latency_summary,
    normalize_log_pattern,
    rate_change,
)


class TestHelpers:
    def test_error_rate_guards_zero_total(self) -> None:
        assert error_rate(5, 0) is None
        assert error_rate(5, None) is None
        assert error_rate(None, 10) is None

    def test_error_rate_normal(self) -> None:
        assert error_rate(2, 10) == 0.2

    def test_rate_change_guards_zero_baseline(self) -> None:
        assert rate_change(10, 0) is None
        assert rate_change(None, 5) is None

    def test_rate_change_normal(self) -> None:
        assert rate_change(15, 10) == pytest.approx(0.5)

    def test_direction_for_anomaly_type(self) -> None:
        assert direction_for(AnomalyType.LATENCY_SPIKE) == "ABOVE"
        assert direction_for(AnomalyType.THROUGHPUT_DROP) == "BELOW"
        assert direction_for(AnomalyType.HEALTH_DEGRADATION) == "BELOW"
        assert direction_for("NOT_A_TYPE") == "ABOVE"

    def test_latency_summary_empty_is_none_not_zero(self) -> None:
        summary = latency_summary([])
        assert summary["sample_count"] == 0
        assert summary["average"] is None
        assert summary["p95"] is None

    def test_latency_summary_values(self) -> None:
        summary = latency_summary([100.0, 200.0, 300.0, 400.0])
        assert summary["sample_count"] == 4
        assert summary["average"] == 250.0
        assert summary["p95"] == pytest.approx(385.0)


class TestLogPatternNormalization:
    def test_numbers_become_placeholder(self) -> None:
        assert (
            normalize_log_pattern("ERROR request failed user_id=123")
            == f"ERROR request failed user_id={PLACEHOLDER_NUM}"
        )

    def test_different_ids_share_one_template(self) -> None:
        a = normalize_log_pattern("ERROR request failed user_id=123")
        b = normalize_log_pattern("ERROR request failed user_id=456")
        assert a == b

    def test_uuid_email_ip_hex(self) -> None:
        msg = (
            "trace 550e8400-e29b-41d4-a716-446655440000 user a@b.com "
            "from 10.0.0.1 ref deadbeefdeadbeef00"
        )
        out = normalize_log_pattern(msg)
        assert PLACEHOLDER_UUID in out
        assert PLACEHOLDER_EMAIL in out
        assert PLACEHOLDER_IP in out
        assert PLACEHOLDER_HEX in out

    def test_floats_replaced(self) -> None:
        assert normalize_log_pattern("latency 12.5ms") == f"latency {PLACEHOLDER_NUM}ms"

    def test_static_messages_unchanged(self) -> None:
        assert normalize_log_pattern("database timeout") == "database timeout"

    def test_none_and_empty(self) -> None:
        assert normalize_log_pattern(None) is None
        assert normalize_log_pattern("") is None

    def test_length_capped(self) -> None:
        out = normalize_log_pattern("x" * 2000)
        assert out is not None
        assert len(out) <= 512


class TestThresholdDetector:
    def test_fires_above(self) -> None:
        out = detect_threshold(
            observed=890.0,
            threshold=500.0,
            anomaly_type=AnomalyType.LATENCY_SPIKE,
        )
        assert out.fired is True
        assert out.source is AnomalySource.METRIC
        assert out.confidence is not None and 0 < out.confidence <= 1

    def test_does_not_fire_within(self) -> None:
        out = detect_threshold(
            observed=300.0,
            threshold=500.0,
            anomaly_type=AnomalyType.LATENCY_SPIKE,
        )
        assert out.fired is False
        assert "did not cross" in out.reason

    def test_below_direction_fires(self) -> None:
        out = detect_threshold(
            observed=10.0,
            threshold=100.0,
            anomaly_type=AnomalyType.THROUGHPUT_DROP,
        )
        assert out.fired is True

    def test_missing_values_do_not_fire(self) -> None:
        assert (
            detect_threshold(
                observed=None,
                threshold=500.0,
                anomaly_type=AnomalyType.LATENCY_SPIKE,
            ).fired
            is False
        )
        assert (
            detect_threshold(
                observed=900.0,
                threshold=None,
                anomaly_type=AnomalyType.LATENCY_SPIKE,
            ).fired
            is False
        )


class TestBaselineDeviationDetector:
    def test_fires_when_multiplier_exceeded(self) -> None:
        out = detect_baseline_deviation(
            deviation_relative=3.0,
            observed=880.0,
            expected=220.0,
            multiplier=2.5,
            anomaly_type=AnomalyType.LATENCY_SPIKE,
        )
        assert out.fired is True
        assert out.deviation == 3.0

    def test_does_not_fire_within_multiplier(self) -> None:
        out = detect_baseline_deviation(
            deviation_relative=0.5,
            observed=330.0,
            expected=220.0,
            multiplier=2.5,
            anomaly_type=AnomalyType.LATENCY_SPIKE,
        )
        assert out.fired is False

    def test_insufficient_baseline_never_fires(self) -> None:
        out = detect_baseline_deviation(
            deviation_relative=9.0,
            observed=880.0,
            expected=None,
            multiplier=2.5,
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            sufficient=False,
        )
        assert out.fired is False
        assert "INSUFFICIENT_DATA" in out.reason

    def test_drop_requires_negative_deviation(self) -> None:
        """A *drop* is anomalous below expectation, not above it."""
        above = detect_baseline_deviation(
            deviation_relative=0.8,
            observed=180.0,
            expected=100.0,
            multiplier=0.5,
            anomaly_type=AnomalyType.THROUGHPUT_DROP,
        )
        assert above.fired is False
        below = detect_baseline_deviation(
            deviation_relative=-0.8,
            observed=20.0,
            expected=100.0,
            multiplier=0.5,
            anomaly_type=AnomalyType.THROUGHPUT_DROP,
        )
        assert below.fired is True


class TestZScoreDetector:
    def test_fires_above_threshold(self) -> None:
        out = detect_z_score(
            z_score=4.2,
            observed=400.0,
            expected=100.0,
            z_threshold=3.0,
            anomaly_type=AnomalyType.LATENCY_SPIKE,
        )
        assert out.fired is True

    def test_negative_z_uses_magnitude(self) -> None:
        out = detect_z_score(
            z_score=-4.2,
            observed=10.0,
            expected=100.0,
            z_threshold=3.0,
            anomaly_type=AnomalyType.LATENCY_SPIKE,
        )
        assert out.fired is True

    def test_no_variance_does_not_fire(self) -> None:
        out = detect_z_score(
            z_score=None,
            observed=10.0,
            expected=10.0,
            z_threshold=3.0,
            anomaly_type=AnomalyType.LATENCY_SPIKE,
        )
        assert out.fired is False
        assert "variance" in out.reason


class TestErrorRateDetector:
    def test_fires_above_threshold(self) -> None:
        out = detect_error_rate(failed=9, total=100, threshold=0.05)
        assert out.fired is True
        assert out.observed_value == pytest.approx(0.09)

    def test_no_requests_does_not_fire(self) -> None:
        out = detect_error_rate(failed=0, total=0, threshold=0.05)
        assert out.fired is False

    def test_within_threshold(self) -> None:
        assert detect_error_rate(failed=1, total=100, threshold=0.05).fired is False


class TestLatencyRatioDetector:
    def _baseline(self, values):
        return StaticBaseline(sum(values) / len(values)).calculate(values)

    def test_fires_when_ratio_exceeded(self) -> None:
        baseline = self._baseline([240.0, 240.0, 240.0])
        out = detect_latency_ratio(observed=890.0, baseline=baseline, multiplier=2.0)
        assert out.fired is True
        assert out.metadata["ratio"] == pytest.approx(890.0 / 240.0)

    def test_within_ratio(self) -> None:
        baseline = self._baseline([240.0, 240.0, 240.0])
        out = detect_latency_ratio(observed=300.0, baseline=baseline, multiplier=2.0)
        assert out.fired is False

    def test_insufficient_baseline(self) -> None:
        from app.services.baseline import RollingBaseline

        baseline = RollingBaseline(min_samples=10).calculate([1.0])
        out = detect_latency_ratio(observed=890.0, baseline=baseline, multiplier=2.0)
        assert out.fired is False


class TestRateChangeDetector:
    def test_relative_multiplier(self) -> None:
        out = detect_rate_change(
            current=150.0, baseline=100.0, multiplier=0.2, sufficient=True
        )
        assert out.fired is True

    def test_absolute_threshold(self) -> None:
        out = detect_rate_change(
            current=150.0, baseline=100.0, threshold=40.0, sufficient=True
        )
        assert out.fired is True
        out2 = detect_rate_change(
            current=110.0, baseline=100.0, threshold=40.0, sufficient=True
        )
        assert out2.fired is False

    def test_no_configured_limit_is_skip(self) -> None:
        out = detect_rate_change(current=150.0, baseline=100.0, sufficient=True)
        assert out.fired is False
        assert "neither threshold nor multiplier" in out.reason

    def test_zero_baseline_uses_absolute(self) -> None:
        out = detect_rate_change(
            current=50.0, baseline=0.0, threshold=10.0, sufficient=True
        )
        assert out.fired is True


class TestPatternSpikeDetector:
    def test_fires_on_spike(self) -> None:
        from app.services.baseline import RollingBaseline

        baseline = RollingBaseline(min_samples=3).calculate([1.0, 2.0, 1.0])
        out = detect_pattern_spike(
            current_count=30.0,
            baseline=baseline,
            multiplier=3.0,
            pattern_template="ERROR database timeout",
        )
        assert out.fired is True
        assert out.pattern_template == "ERROR database timeout"

    def test_zero_baseline_uses_floor(self) -> None:
        """A never-seen pattern is not multiplied by zero to become invisible."""
        from app.services.baseline import RollingBaseline

        baseline = RollingBaseline(min_samples=2).calculate([0.0, 0.0])
        out = detect_pattern_spike(current_count=5.0, baseline=baseline, multiplier=3.0)
        assert out.fired is True

    def test_insufficient_baseline(self) -> None:
        from app.services.baseline import RollingBaseline

        baseline = RollingBaseline(min_samples=10).calculate([1.0])
        out = detect_pattern_spike(current_count=5.0, baseline=baseline, multiplier=3.0)
        assert out.fired is False


class TestTraceFailureDetector:
    def test_fires(self) -> None:
        out = detect_trace_failure_rate(failed=18, total=100, threshold=0.05)
        assert out.fired is True
        assert out.source is AnomalySource.TRACE

    def test_no_traces(self) -> None:
        assert (
            detect_trace_failure_rate(failed=0, total=0, threshold=0.05).fired is False
        )


class TestHealthTransitionDetector:
    def test_worsening_fires(self) -> None:
        out = detect_health_transition(
            previous_status="HEALTHY", current_status="DEGRADED"
        )
        assert out.fired is True
        assert out.source is AnomalySource.HEALTH_CHECK

    def test_improvement_does_not_fire(self) -> None:
        out = detect_health_transition(
            previous_status="UNHEALTHY", current_status="HEALTHY"
        )
        assert out.fired is False

    def test_repeated_state_does_not_fire(self) -> None:
        """Repeating DEGRADED must not emit an anomaly every poll."""
        out = detect_health_transition(
            previous_status="DEGRADED", current_status="DEGRADED"
        )
        assert out.fired is False
        assert "not a worsening" in out.reason

    def test_missing_state_does_not_fire(self) -> None:
        assert (
            detect_health_transition(
                previous_status=None, current_status="UNHEALTHY"
            ).fired
            is False
        )

    def test_healthy_to_unhealthy_is_worse(self) -> None:
        out = detect_health_transition(
            previous_status="HEALTHY", current_status="UNHEALTHY"
        )
        assert out.fired is True
        assert out.deviation == 2.0
