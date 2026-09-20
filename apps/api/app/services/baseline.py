"""ARGUS Baseline Engine (Phase 3 §9–§10).

Provider-neutral, deterministic baselines. Pure functions and small strategy
classes only — no database, no I/O — so the statistics can be unit-tested in
isolation and reused by any detector.

Key honesty rules baked in here:

* **Missing data is not failure.** ``None`` samples are dropped, and a baseline
  without enough history reports ``sufficient=False`` rather than inventing a
  value. Nothing downstream may treat ``INSUFFICIENT_DATA`` as an anomaly.
* **Zero variance is not a division.** When ``stddev`` is ~0 the z-score is
  ``None``, never ``inf``.
* **No seasonal forecasting.** Only STATIC and ROLLING strategies exist; the
  interface leaves room for more without pretending to have them.

Anomaly severity is *not* computed here — this module answers only
"what was expected, and how far is the observation from it?".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

#: Sentinel explaining an absent baseline. Present as a literal so it can be
#: surfaced in explanations and stored in metadata without translation.
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

#: Below this magnitude a standard deviation is treated as zero.
_EPSILON = 1e-12


def percentile(sorted_values: Sequence[float], p: float) -> Optional[float]:
    """Deterministic percentile via linear interpolation.

    Matches the common "linear" definition (the default in numpy), so p50 of an
    even-sized sample is the midpoint rather than an arbitrary element. Returns
    ``None`` for an empty sample.
    """
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    if p <= 0:
        return float(sorted_values[0])
    if p >= 100:
        return float(sorted_values[-1])
    rank = (len(sorted_values) - 1) * (p / 100.0)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(sorted_values[int(rank)])
    weight = rank - lower
    return float(
        sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * weight
    )


@dataclass(frozen=True)
class MetricStats:
    """Deterministic summary statistics over a sample of observations."""

    sample_count: int
    mean: Optional[float] = None
    median: Optional[float] = None
    stddev: Optional[float] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    p50: Optional[float] = None
    p95: Optional[float] = None
    p99: Optional[float] = None

    @property
    def has_variance(self) -> bool:
        """True only when a meaningful standard deviation exists."""
        return self.stddev is not None and self.stddev > _EPSILON


def compute_stats(values: Sequence[Optional[float]]) -> MetricStats:
    """Compute summary statistics, ignoring ``None`` (missing) samples."""
    clean = [float(v) for v in values if v is not None]
    count = len(clean)
    if count == 0:
        return MetricStats(sample_count=0)

    ordered = sorted(clean)
    mean = sum(ordered) / count
    if count > 1:
        # Population standard deviation — deterministic, no sample correction.
        variance = sum((v - mean) ** 2 for v in ordered) / count
        stddev = math.sqrt(variance)
    else:
        stddev = 0.0

    return MetricStats(
        sample_count=count,
        mean=mean,
        median=percentile(ordered, 50),
        stddev=stddev,
        min_value=ordered[0],
        max_value=ordered[-1],
        p50=percentile(ordered, 50),
        p95=percentile(ordered, 95),
        p99=percentile(ordered, 99),
    )


@dataclass(frozen=True)
class Deviation:
    """The measured distance between an observation and its expectation.

    This is evidence strength, never causal probability.
    """

    observed: float
    expected: Optional[float]
    absolute: Optional[float] = None
    relative: Optional[float] = None
    z_score: Optional[float] = None
    direction: str = "UNKNOWN"

    @property
    def above(self) -> bool:
        return self.direction == "ABOVE"

    @property
    def below(self) -> bool:
        return self.direction == "BELOW"


@dataclass(frozen=True)
class BaselineResult:
    """A baseline plus the stats it was derived from."""

    strategy: str
    window_seconds: int
    expected_value: Optional[float]
    stats: MetricStats
    #: False when there was not enough history / no configured expectation.
    sufficient: bool
    reason: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    @property
    def sample_count(self) -> int:
        return self.stats.sample_count


class BaselineStrategy:
    """Interface for deterministic baseline strategies (§9).

    Subclasses answer two questions only: *what did we expect* and *how far off
    is the observation*. Advanced models can be added later behind the same
    interface without touching detectors.
    """

    name: str = "BASE"

    def __init__(self, window_seconds: int = 300) -> None:
        self.window_seconds = window_seconds

    def calculate(
        self, values: Sequence[Optional[float]], *, metric_name: str = ""
    ) -> BaselineResult:
        raise NotImplementedError

    def expected_value(
        self,
        values: Sequence[Optional[float]],
        *,
        metric_name: str = "",
        percentile_key: Optional[str] = None,
    ) -> BaselineResult:
        """Select the expected value a detector should compare against.

        ``percentile_key`` lets a latency rule compare like with like (p95
        against baseline p95) instead of mixing percentiles.
        """
        base = self.calculate(values, metric_name=metric_name)
        if not base.sufficient:
            return base
        if percentile_key is None:
            return base
        chosen = {
            "p50": base.stats.p50,
            "median": base.stats.median,
            "p95": base.stats.p95,
            "p99": base.stats.p99,
            "mean": base.stats.mean,
            "max": base.stats.max_value,
            "min": base.stats.min_value,
        }.get(percentile_key)
        if chosen is None:
            return base
        return BaselineResult(
            strategy=base.strategy,
            window_seconds=base.window_seconds,
            expected_value=chosen,
            stats=base.stats,
            sufficient=True,
            reason=base.reason,
            metadata={**base.metadata, "percentile_key": percentile_key},
        )

    def detect_deviation(
        self, observed: Optional[float], baseline: BaselineResult
    ) -> Optional[Deviation]:
        """Compare an observation against a baseline.

        Returns ``None`` when the baseline is insufficient or the observation is
        missing — an absence of data is not a deviation.
        """
        if observed is None or not baseline.sufficient:
            return None
        expected = baseline.expected_value
        if expected is None:
            return None

        absolute = observed - expected
        if expected != 0:
            relative = absolute / abs(expected)
        else:
            # A zero expectation makes relative change undefined, not infinite.
            relative = None

        z_score: Optional[float] = None
        if baseline.stats.has_variance and baseline.stats.stddev is not None:
            z_score = absolute / baseline.stats.stddev

        if absolute > _EPSILON:
            direction = "ABOVE"
        elif absolute < -_EPSILON:
            direction = "BELOW"
        else:
            direction = "EQUAL"

        return Deviation(
            observed=float(observed),
            expected=expected,
            absolute=absolute,
            relative=relative,
            z_score=z_score,
            direction=direction,
        )


class StaticBaseline(BaselineStrategy):
    """A configured expectation (§9).

    ``expected_value`` is explicit configuration, so the baseline is always
    ``sufficient`` — *unless* no value was configured, in which case it is
    explicitly insufficient rather than silently zero.
    """

    name = "STATIC"

    def __init__(
        self, expected_value: Optional[float], window_seconds: int = 300
    ) -> None:
        super().__init__(window_seconds)
        self._expected = expected_value

    def calculate(
        self, values: Sequence[Optional[float]], *, metric_name: str = ""
    ) -> BaselineResult:
        stats = compute_stats(values)
        if self._expected is None:
            return BaselineResult(
                strategy=self.name,
                window_seconds=self.window_seconds,
                expected_value=None,
                stats=stats,
                sufficient=False,
                reason="no static expected_value configured",
            )
        return BaselineResult(
            strategy=self.name,
            window_seconds=self.window_seconds,
            expected_value=float(self._expected),
            stats=stats,
            sufficient=True,
        )


class RollingBaseline(BaselineStrategy):
    """Statistics over a rolling historical window (§9).

    Insufficient history yields ``sufficient=False`` with an explicit reason —
    the engine never lowers a threshold to manufacture a detection.
    """

    name = "ROLLING"

    def __init__(
        self,
        min_samples: int = 5,
        window_seconds: int = 300,
        expected_stat: str = "mean",
    ) -> None:
        super().__init__(window_seconds)
        self.min_samples = max(1, int(min_samples))
        self.expected_stat = expected_stat

    def calculate(
        self, values: Sequence[Optional[float]], *, metric_name: str = ""
    ) -> BaselineResult:
        stats = compute_stats(values)
        if stats.sample_count < self.min_samples:
            return BaselineResult(
                strategy=self.name,
                window_seconds=self.window_seconds,
                expected_value=None,
                stats=stats,
                sufficient=False,
                reason=(
                    f"{INSUFFICIENT_DATA}: {stats.sample_count} sample(s) < "
                    f"min_samples={self.min_samples}"
                ),
            )
        expected = {
            "mean": stats.mean,
            "median": stats.median,
            "min": stats.min_value,
            "max": stats.max_value,
            "p50": stats.p50,
            "p95": stats.p95,
            "p99": stats.p99,
        }.get(self.expected_stat, stats.mean)
        return BaselineResult(
            strategy=self.name,
            window_seconds=self.window_seconds,
            expected_value=expected,
            stats=stats,
            sufficient=expected is not None,
        )


def build_baseline(
    strategy: str,
    *,
    window_seconds: int = 300,
    min_samples: int = 5,
    expected_value: Optional[float] = None,
    expected_stat: str = "mean",
) -> BaselineStrategy:
    """Construct a baseline strategy from a persisted rule's fields."""
    normalized = (strategy or "").upper()
    if normalized == "STATIC":
        return StaticBaseline(expected_value, window_seconds=window_seconds)
    return RollingBaseline(
        min_samples=min_samples,
        window_seconds=window_seconds,
        expected_stat=expected_stat,
    )


__all__ = [
    "INSUFFICIENT_DATA",
    "MetricStats",
    "Deviation",
    "BaselineResult",
    "BaselineStrategy",
    "StaticBaseline",
    "RollingBaseline",
    "build_baseline",
    "compute_stats",
    "percentile",
]
