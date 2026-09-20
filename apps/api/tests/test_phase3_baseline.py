"""Phase 3 — baseline engine unit tests (§9–§11).

Deterministic statistics only: these tests pin the exact numbers so a future
refactor cannot silently change what "expected" means.
"""

from __future__ import annotations

import pytest

from app.services.baseline import (
    INSUFFICIENT_DATA,
    RollingBaseline,
    StaticBaseline,
    build_baseline,
    compute_stats,
    percentile,
)


class TestPercentile:
    def test_empty_is_none(self) -> None:
        assert percentile([], 50) is None

    def test_single_value(self) -> None:
        assert percentile([7.0], 95) == 7.0

    def test_median_even_sample_interpolates(self) -> None:
        assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5

    def test_bounds(self) -> None:
        vals = [1.0, 2.0, 3.0]
        assert percentile(vals, 0) == 1.0
        assert percentile(vals, 100) == 3.0

    def test_p95_of_ten_values(self) -> None:
        vals = [float(i) for i in range(1, 11)]  # 1..10
        assert percentile(vals, 95) == pytest.approx(9.55)


class TestComputeStats:
    def test_empty_sample(self) -> None:
        stats = compute_stats([])
        assert stats.sample_count == 0
        assert stats.mean is None
        assert stats.stddev is None
        assert stats.has_variance is False

    def test_none_values_are_missing_not_zero(self) -> None:
        stats = compute_stats([10.0, None, 20.0, None])
        assert stats.sample_count == 2
        assert stats.mean == 15.0

    def test_known_statistics(self) -> None:
        stats = compute_stats([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
        assert stats.sample_count == 8
        assert stats.mean == 5.0
        assert stats.median == 4.5
        assert stats.min_value == 2.0
        assert stats.max_value == 9.0
        # Population stddev of this sample.
        assert stats.stddev == pytest.approx(2.0, abs=1e-9)

    def test_single_value_has_zero_variance(self) -> None:
        stats = compute_stats([42.0])
        assert stats.stddev == 0.0
        assert stats.has_variance is False

    def test_zero_values_are_real_samples(self) -> None:
        """A metric that is legitimately zero is data, not missing."""
        stats = compute_stats([0.0, 0.0, 0.0])
        assert stats.sample_count == 3
        assert stats.mean == 0.0

    def test_all_identical_values_no_variance(self) -> None:
        stats = compute_stats([5.0, 5.0, 5.0])
        assert stats.stddev == 0.0
        assert stats.has_variance is False


class TestRollingBaseline:
    def test_insufficient_history_is_explicit(self) -> None:
        baseline = RollingBaseline(min_samples=5, window_seconds=300)
        result = baseline.calculate([1.0, 2.0])
        assert result.sufficient is False
        assert result.expected_value is None
        assert INSUFFICIENT_DATA in (result.reason or "")
        assert result.sample_count == 2

    def test_sufficient_history_uses_mean(self) -> None:
        baseline = RollingBaseline(min_samples=3)
        result = baseline.calculate([10.0, 20.0, 30.0])
        assert result.sufficient is True
        assert result.expected_value == 20.0

    def test_expected_stat_selection(self) -> None:
        baseline = RollingBaseline(min_samples=2, expected_stat="p95")
        result = baseline.calculate([1.0, 2.0, 3.0, 4.0, 5.0])
        assert result.expected_value == pytest.approx(4.8)

    def test_missing_values_do_not_inflate_sample_count(self) -> None:
        baseline = RollingBaseline(min_samples=3)
        result = baseline.calculate([1.0, None, 2.0, None])
        assert result.sufficient is False
        assert result.sample_count == 2


class TestStaticBaseline:
    def test_configured_value_is_sufficient(self) -> None:
        baseline = StaticBaseline(200.0)
        result = baseline.calculate([])
        assert result.sufficient is True
        assert result.expected_value == 200.0

    def test_missing_configuration_is_insufficient(self) -> None:
        baseline = StaticBaseline(None)
        result = baseline.calculate([1.0, 2.0])
        assert result.sufficient is False
        assert result.expected_value is None
        assert result.reason is not None


class TestDeviation:
    def test_relative_and_absolute(self) -> None:
        baseline = StaticBaseline(200.0)
        result = baseline.calculate([])
        dev = baseline.detect_deviation(300.0, result)
        assert dev is not None
        assert dev.absolute == 100.0
        assert dev.relative == pytest.approx(0.5)
        assert dev.direction == "ABOVE"
        assert dev.above is True

    def test_below_direction(self) -> None:
        baseline = StaticBaseline(100.0)
        dev = baseline.detect_deviation(40.0, baseline.calculate([]))
        assert dev is not None
        assert dev.direction == "BELOW"
        assert dev.below is True

    def test_equal_within_epsilon(self) -> None:
        baseline = StaticBaseline(100.0)
        dev = baseline.detect_deviation(100.0, baseline.calculate([]))
        assert dev is not None
        assert dev.direction == "EQUAL"
        assert dev.absolute == 0.0

    def test_zero_expectation_relative_is_none(self) -> None:
        """Zero baseline must not produce an infinite relative change."""
        baseline = StaticBaseline(0.0)
        dev = baseline.detect_deviation(5.0, baseline.calculate([]))
        assert dev is not None
        assert dev.relative is None
        assert dev.absolute == 5.0

    def test_z_score_absent_without_variance(self) -> None:
        baseline = RollingBaseline(min_samples=3)
        result = baseline.calculate([5.0, 5.0, 5.0])
        dev = baseline.detect_deviation(9.0, result)
        assert dev is not None
        assert dev.z_score is None

    def test_z_score_present_with_variance(self) -> None:
        baseline = RollingBaseline(min_samples=3)
        result = baseline.calculate([10.0, 20.0, 30.0])
        dev = baseline.detect_deviation(40.0, result)
        assert dev is not None
        assert dev.z_score is not None
        assert dev.z_score > 0

    def test_missing_observation_is_not_a_deviation(self) -> None:
        baseline = StaticBaseline(100.0)
        assert baseline.detect_deviation(None, baseline.calculate([])) is None

    def test_insufficient_baseline_yields_no_deviation(self) -> None:
        baseline = RollingBaseline(min_samples=10)
        result = baseline.calculate([1.0])
        assert baseline.detect_deviation(999.0, result) is None


class TestExpectedValueSelection:
    def test_percentile_key_compares_like_with_like(self) -> None:
        baseline = RollingBaseline(min_samples=2, window_seconds=300)
        result = baseline.expected_value(
            [100.0, 200.0, 300.0, 400.0], percentile_key="p95"
        )
        assert result.sufficient is True
        assert result.expected_value == pytest.approx(385.0)
        assert result.metadata["percentile_key"] == "p95"

    def test_unknown_percentile_key_falls_back(self) -> None:
        baseline = RollingBaseline(min_samples=2)
        result = baseline.expected_value([10.0, 20.0], percentile_key="nope")
        assert result.expected_value == 15.0


class TestBuildBaseline:
    def test_static(self) -> None:
        strategy = build_baseline("STATIC", expected_value=123.0)
        assert isinstance(strategy, StaticBaseline)
        assert strategy.calculate([]).expected_value == 123.0

    def test_rolling_default(self) -> None:
        strategy = build_baseline("ROLLING", min_samples=2)
        assert isinstance(strategy, RollingBaseline)

    def test_unknown_strategy_defaults_to_rolling(self) -> None:
        strategy = build_baseline("SOMETHING_FUTURE")
        assert isinstance(strategy, RollingBaseline)
