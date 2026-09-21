"""ARGUS Forecasting Statistics (Phase 8 §8, §19–§21, §64).

Pure, deterministic math. No database, no I/O, no randomness — so every rule
that decides whether a metric is *rising*, *flat*, *volatile* or *saturated*
can be unit-tested with literal numbers and reproduced exactly.

The honesty rules this module enforces:

* **A trend needs evidence.** Fewer than two usable samples yields
  ``UNKNOWN``, never ``FLAT`` — "no data" and "no movement" are different
  claims, and conflating them is how a broken pipeline looks like stability.
* **Direction beats noise only when it is strong enough.** High volatility
  with negligible slope reports ``VOLATILE``; a steep slope inside noisy data
  still reports ``RISING``. The two cases are distinguished by explicit,
  documented thresholds, not by whichever branch happens to run first.
* **Trends are never extrapolated past the observed window.** A slope is a
  description of what happened; turning it into a forecast is the predictors'
  job, and they bound it (§20).
* **Nothing here is a probability.** Slopes, ratios and z-scores are evidence
  strength; only a predictor may attach a calibrated number, and only when the
  data supports one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from app.models.reliability import FeatureTrend
from app.services.baseline import compute_stats, percentile

#: Below this magnitude a relative slope is indistinguishable from zero.
DEFAULT_FLAT_EPSILON = 0.02

#: Coefficient of variation above which a series is considered noisy.
DEFAULT_VOLATILE_RATIO = 0.35

#: A relative slope this large is directional even inside a noisy series.
DEFAULT_STRONG_SLOPE = 0.15

_EPSILON = 1e-12


def clean(values: Sequence[Optional[float]]) -> list[float]:
    """Drop missing samples, keeping order. Missing data is not zero."""
    return [float(v) for v in values if v is not None]


def mean(values: Sequence[Optional[float]]) -> Optional[float]:
    data = clean(values)
    if not data:
        return None
    return sum(data) / len(data)


def stddev(values: Sequence[Optional[float]]) -> Optional[float]:
    data = clean(values)
    if not data:
        return None
    if len(data) == 1:
        return 0.0
    avg = sum(data) / len(data)
    return math.sqrt(sum((v - avg) ** 2 for v in data) / len(data))


def coefficient_of_variation(values: Sequence[Optional[float]]) -> Optional[float]:
    """Relative dispersion (``stddev / |mean|``), or ``None`` without a mean.

    Scale-free, so a latency series in milliseconds and an error rate in
    percent can be compared with the same threshold.
    """
    data = clean(values)
    if not data:
        return None
    avg = sum(data) / len(data)
    if abs(avg) <= _EPSILON:
        # A series hovering at zero has no meaningful relative dispersion.
        return 0.0
    sd = stddev(data)
    if sd is None:
        return None
    return abs(sd / avg)


def slope(values: Sequence[Optional[float]]) -> Optional[float]:
    """Least-squares slope per sample step over the *observed* window.

    Deterministic closed form; ``None`` for fewer than two points, because a
    single observation has no direction.
    """
    data = clean(values)
    n = len(data)
    if n < 2:
        return None
    x_mean = (n - 1) / 2.0
    y_mean = sum(data) / n
    numerator = 0.0
    denominator = 0.0
    for index, value in enumerate(data):
        dx = index - x_mean
        numerator += dx * (value - y_mean)
        denominator += dx * dx
    if denominator <= _EPSILON:
        return None
    return numerator / denominator


def normalized_slope(values: Sequence[Optional[float]]) -> Optional[float]:
    """Slope expressed as a fraction of the mean, per step.

    Scale-free and comparable across metrics: ``0.05`` means "about 5% of the
    mean per sample step", regardless of the unit.
    """
    data = clean(values)
    if len(data) < 2:
        return None
    raw = slope(data)
    if raw is None:
        return None
    avg = sum(data) / len(data)
    if abs(avg) <= _EPSILON:
        # Growing from (near) zero: report the absolute step as the direction.
        return raw
    return raw / abs(avg)


def slope_per_hour(
    values: Sequence[Optional[float]],
    timestamps: Sequence[Optional[datetime]],
) -> Optional[float]:
    """Least-squares slope expressed per hour and normalized by the mean.

    This is the trend feature the predictors score on, and the *unit matters*:
    a slope per sample step silently depends on the scrape interval, so the same
    metric sampled every 5 minutes would look five times calmer than one
    sampled every minute. Normalizing per hour makes a forecast a property of
    the system rather than of the ingestion cadence.

    ``0.5`` means "moving 50% of its mean per hour". Returns ``None`` when
    there are fewer than two usable pairs or no time elapsed between them.
    """
    pairs: list[tuple[float, float]] = []
    origin: Optional[datetime] = None
    for value, stamp in zip(values, timestamps):
        if value is None or stamp is None:
            continue
        moment = (
            stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)
        )
        if origin is None:
            origin = moment
        pairs.append((moment.timestamp() - origin.timestamp(), float(value)))
    if len(pairs) < 2:
        return None

    n = len(pairs)
    mean_x = sum(pair[0] for pair in pairs) / n
    mean_y = sum(pair[1] for pair in pairs) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    denominator = sum((x - mean_x) ** 2 for x, _y in pairs)
    if denominator <= _EPSILON:
        #: All observations share one instant: no elapsed time, no trend.
        return None
    per_second = numerator / denominator
    hourly = per_second * 3600.0
    if abs(mean_y) <= _EPSILON:
        return hourly
    return hourly / abs(mean_y)


def ewma(values: Sequence[Optional[float]], alpha: float) -> list[Optional[float]]:
    """Exponentially weighted moving average (§21).

    ``alpha`` is the weight of the newest observation. Missing samples are
    skipped without resetting the state, so a gap does not look like a drop to
    zero. The returned series is aligned with the input.
    """
    if not 0.0 < alpha <= 1.0:
        raise ValueError("alpha must be in (0, 1]")
    out: list[Optional[float]] = []
    state: Optional[float] = None
    for value in values:
        if value is None:
            out.append(state)
            continue
        state = (
            float(value)
            if state is None
            else alpha * float(value) + (1.0 - alpha) * state
        )
        out.append(state)
    return out


def ewma_last(values: Sequence[Optional[float]], alpha: float) -> Optional[float]:
    """Final EWMA value, or ``None`` when no sample was usable."""
    series = ewma(values, alpha)
    for value in reversed(series):
        if value is not None:
            return value
    return None


def change_rate(current: Optional[float], baseline: Optional[float]) -> Optional[float]:
    """Signed relative change ``(current - baseline) / |baseline|``.

    ``None`` when either side is missing or the baseline is ~0 — a ratio
    against zero is not a measurement.
    """
    if current is None or baseline is None:
        return None
    if abs(baseline) <= _EPSILON:
        return None
    return (current - baseline) / abs(baseline)


def classify_trend(
    values: Sequence[Optional[float]],
    *,
    flat_epsilon: float = DEFAULT_FLAT_EPSILON,
    volatile_ratio: float = DEFAULT_VOLATILE_RATIO,
    strong_slope: float = DEFAULT_STRONG_SLOPE,
) -> FeatureTrend:
    """Name the direction of a series (§64).

    Order of decisions, each with an explicit threshold:

    1. fewer than two usable samples → ``UNKNOWN`` (not ``FLAT``);
    2. |relative slope| ≤ ``flat_epsilon`` → ``FLAT``;
    3. dispersion ≥ ``volatile_ratio`` **and** |relative slope| <
       ``strong_slope`` → ``VOLATILE`` (movement exists but is not directional);
    4. otherwise the sign of the slope → ``RISING`` / ``FALLING``.
    """
    data = clean(values)
    if len(data) < 2:
        return FeatureTrend.UNKNOWN

    rel = normalized_slope(data)
    if rel is None or abs(rel) <= flat_epsilon:
        return FeatureTrend.FLAT

    cv = coefficient_of_variation(data)
    if cv is not None and cv >= volatile_ratio and abs(rel) < strong_slope:
        return FeatureTrend.VOLATILE

    return FeatureTrend.RISING if rel > 0 else FeatureTrend.FALLING


def is_saturated(
    value: Optional[float],
    *,
    ceiling: Optional[float],
    saturation_ratio: float,
) -> bool:
    """Whether a value has reached ``saturation_ratio`` of a known ceiling.

    Only answers when a ceiling is actually configured — an unbounded metric
    is never "saturated", which keeps the signal from firing on a guess.
    """
    if value is None or ceiling is None or ceiling <= _EPSILON:
        return False
    return value >= ceiling * saturation_ratio


@dataclass(frozen=True)
class SeriesProfile:
    """Deterministic summary of one metric series inside a feature window."""

    metric_name: str
    sample_count: int
    current: Optional[float] = None
    mean: Optional[float] = None
    median: Optional[float] = None
    stddev: Optional[float] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    p50: Optional[float] = None
    p95: Optional[float] = None
    p99: Optional[float] = None
    slope: Optional[float] = None
    normalized_slope: Optional[float] = None
    volatility: Optional[float] = None
    trend: FeatureTrend = FeatureTrend.UNKNOWN
    #: Confidence in the *profile*, driven by how much data it saw.
    sample_sufficient: bool = False
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "metric_name": self.metric_name,
            "sample_count": self.sample_count,
            "current": self.current,
            "mean": self.mean,
            "median": self.median,
            "stddev": self.stddev,
            "min": self.minimum,
            "max": self.maximum,
            "p50": self.p50,
            "p95": self.p95,
            "p99": self.p99,
            "slope": self.slope,
            "normalized_slope": self.normalized_slope,
            "volatility": self.volatility,
            "trend": self.trend.value,
            "sample_sufficient": self.sample_sufficient,
        }


def profile_series(
    metric_name: str,
    values: Sequence[Optional[float]],
    *,
    min_samples: int = 3,
    flat_epsilon: float = DEFAULT_FLAT_EPSILON,
    volatile_ratio: float = DEFAULT_VOLATILE_RATIO,
    strong_slope: float = DEFAULT_STRONG_SLOPE,
) -> SeriesProfile:
    """Build a :class:`SeriesProfile` from one ordered series.

    ``min_samples`` is the count below which the profile is reported as
    insufficient — the value is still computed and stored (so a reviewer can
    see it), but ``sample_sufficient`` is ``False`` and no predictor is allowed
    to treat it as a basis for a confident forecast (§18).
    """
    data = clean(values)
    stats = compute_stats(data)
    rel = normalized_slope(data)
    return SeriesProfile(
        metric_name=metric_name,
        sample_count=stats.sample_count,
        current=data[-1] if data else None,
        mean=stats.mean,
        median=stats.median,
        stddev=stats.stddev,
        minimum=stats.min_value,
        maximum=stats.max_value,
        p50=stats.p50,
        p95=stats.p95,
        p99=stats.p99,
        slope=slope(data),
        normalized_slope=rel,
        volatility=coefficient_of_variation(data),
        trend=classify_trend(
            data,
            flat_epsilon=flat_epsilon,
            volatile_ratio=volatile_ratio,
            strong_slope=strong_slope,
        ),
        sample_sufficient=stats.sample_count >= min_samples,
    )


def bounded_linear_projection(
    values: Sequence[Optional[float]],
    *,
    steps_ahead: int,
    max_growth_ratio: float,
) -> Optional[float]:
    """Project a series forward with a hard bound (§20).

    Linear extrapolation of the observed slope, clamped so the projection can
    never exceed ``max_growth_ratio`` times the observed mean (or fall below the
    observed minimum). Bounded on purpose: an unbounded extrapolation is how a
    forecast turns into a confident fiction.
    """
    data = clean(values)
    if len(data) < 2 or steps_ahead <= 0:
        return None
    raw = slope(data)
    if raw is None:
        return None
    current = data[-1]
    projected = current + raw * steps_ahead
    avg = sum(data) / len(data)
    upper = max(abs(avg) * max_growth_ratio, abs(current))
    lower_value = min(data)
    return max(lower_value, min(projected, upper))


def safe_ratio(
    numerator: Optional[float], denominator: Optional[float]
) -> Optional[float]:
    """Division that returns ``None`` instead of raising or yielding ``inf``."""
    if numerator is None or denominator is None:
        return None
    if abs(denominator) <= _EPSILON:
        return None
    return numerator / denominator


__all__ = [
    "DEFAULT_FLAT_EPSILON",
    "DEFAULT_STRONG_SLOPE",
    "DEFAULT_VOLATILE_RATIO",
    "SeriesProfile",
    "bounded_linear_projection",
    "change_rate",
    "classify_trend",
    "clean",
    "coefficient_of_variation",
    "ewma",
    "ewma_last",
    "is_saturated",
    "mean",
    "normalized_slope",
    "percentile",
    "profile_series",
    "safe_ratio",
    "slope",
    "slope_per_hour",
    "stddev",
]
