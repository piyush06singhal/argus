"""ARGUS Reliability Risk Policy (Phase 8 §5, §38, §83).

One place decides what "HIGH" means, and one place decides how an interpretable
reliability score is composed. Scattering either across predictors is how a
platform ends up with three different definitions of "critical" and a score
nobody can explain.

Two things this module refuses to do:

* **It never invents a level for missing evidence.** :func:`classify_score`
  returns :attr:`ForecastRiskLevel.UNKNOWN` when the score is ``None``, so a
  component with no history is *unknown*, not safe (§18, §69).
* **It never presents the reliability score as a verdict on software quality.**
  :class:`ReliabilityScore` is a weighted reading of named, normalized health
  dimensions, with its missing-data behaviour stated rather than hidden (§38).
  It is a summary of *observed reliability signals*, nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from app.core.config import get_settings
from app.models.reliability import ForecastRiskLevel

settings = get_settings()

#: The declared method string stored alongside every score, so a reader can
#: find the exact rule that produced it without reading this file (§83).
RELIABILITY_SCORE_METHOD = (
    "weighted mean of normalized health dimensions in [0,1] (1 = healthy); "
    "missing dimensions have their weight redistributed proportionally across "
    "the dimensions that do have data; if every dimension is missing the score "
    "is None. Not a universal measure of software quality."
)

#: Dimension weights for the reliability score (§38). They sum to 1.0 over a
#: complete set; see :class:`ReliabilityScore` for what happens when a
#: dimension has no data.
DIMENSION_WEIGHTS: dict[str, float] = {
    "availability_health": 0.20,
    "error_health": 0.20,
    "latency_health": 0.15,
    "resource_health": 0.15,
    "dependency_health": 0.10,
    "incident_stability": 0.10,
    "change_stability": 0.10,
}

#: Order used by the API and UI so the composition is always presented the
#: same way.
DIMENSION_ORDER: tuple[str, ...] = tuple(DIMENSION_WEIGHTS)


@dataclass(frozen=True)
class RiskPolicy:
    """Configurable risk-score bands (§5).

    Bands are half-open on the lower edge: ``[medium, high)`` is MEDIUM. The
    bands are read from settings so a deployment can retune them without a code
    change, and every classifier in the phase goes through here.
    """

    threshold_medium: float
    threshold_high: float
    threshold_critical: float

    def __post_init__(self) -> None:
        ordered = (
            self.threshold_medium,
            self.threshold_high,
            self.threshold_critical,
        )
        if list(ordered) != sorted(ordered):
            raise ValueError(
                "risk thresholds must be ascending: "
                f"MEDIUM={self.threshold_medium} HIGH={self.threshold_high} "
                f"CRITICAL={self.threshold_critical}"
            )

    def classify(self, score: Optional[float]) -> ForecastRiskLevel:
        """Map a 0–1 risk score to a level, or ``UNKNOWN`` without a score."""
        if score is None:
            return ForecastRiskLevel.UNKNOWN
        clamped = max(0.0, min(1.0, score))
        if clamped >= self.threshold_critical:
            return ForecastRiskLevel.CRITICAL
        if clamped >= self.threshold_high:
            return ForecastRiskLevel.HIGH
        if clamped >= self.threshold_medium:
            return ForecastRiskLevel.MEDIUM
        return ForecastRiskLevel.LOW

    def describe(self) -> dict:
        return {
            "threshold_medium": self.threshold_medium,
            "threshold_high": self.threshold_high,
            "threshold_critical": self.threshold_critical,
        }


def risk_policy() -> RiskPolicy:
    """The active policy, built from settings."""
    return RiskPolicy(
        threshold_medium=settings.RELIABILITY_THRESHOLD_MEDIUM,
        threshold_high=settings.RELIABILITY_THRESHOLD_HIGH,
        threshold_critical=settings.RELIABILITY_THRESHOLD_CRITICAL,
    )


def classify_score(score: Optional[float]) -> ForecastRiskLevel:
    """Convenience wrapper over the active policy."""
    return risk_policy().classify(score)


#: Ordering used wherever risk levels are compared or sorted.
RISK_ORDER: dict[ForecastRiskLevel, int] = {
    ForecastRiskLevel.UNKNOWN: -1,
    ForecastRiskLevel.LOW: 0,
    ForecastRiskLevel.MEDIUM: 1,
    ForecastRiskLevel.HIGH: 2,
    ForecastRiskLevel.CRITICAL: 3,
}


def risk_rank(level: ForecastRiskLevel) -> int:
    """Comparable rank; ``UNKNOWN`` ranks below ``LOW`` on purpose.

    Unknown is not "better than low" as a reliability statement — it ranks
    lowest only so that a deterministic sort has a total order. Callers that
    need a different reading must check for ``UNKNOWN`` explicitly, and the
    storey strings below never describe unknown as safe.
    """
    return RISK_ORDER[level]


def risk_statement(level: ForecastRiskLevel, subject: str) -> str:
    """Phrase a level the way §1 requires: risk, never a promise of failure."""
    if level is ForecastRiskLevel.UNKNOWN:
        return f"{subject} cannot be forecast — insufficient historical evidence"
    if level is ForecastRiskLevel.CRITICAL:
        return f"{subject} shows critical predicted reliability risk"
    if level is ForecastRiskLevel.HIGH:
        return f"{subject} shows elevated predicted reliability risk"
    if level is ForecastRiskLevel.MEDIUM:
        return f"{subject} shows moderate predicted reliability risk"
    return f"{subject} shows no elevated predicted reliability risk"


def level_from_name(name: str) -> ForecastRiskLevel:
    """Parse a configured level name, defaulting to ``UNKNOWN``."""
    try:
        return ForecastRiskLevel(name)
    except ValueError:
        return ForecastRiskLevel.UNKNOWN


def at_least(level: ForecastRiskLevel, minimum: ForecastRiskLevel) -> bool:
    """Whether ``level`` meets or exceeds ``minimum`` (§39).

    ``UNKNOWN`` never satisfies a threshold — a warning must not be raised on
    an absence of evidence.
    """
    if level is ForecastRiskLevel.UNKNOWN:
        return False
    return risk_rank(level) >= risk_rank(minimum)


@dataclass(frozen=True)
class ReliabilityScore:
    """A structured, explainable reliability summary (§38).

    Every dimension is a normalized ``[0, 1]`` health value where ``1`` is
    healthy, produced by a named rule elsewhere in the phase. ``None`` means the
    dimension had no usable data — which is *not* the same as healthy, and is
    reported as missing rather than defaulted.

    The composition is deliberately boring: a weighted mean over the dimensions
    that have data, with the missing weight redistributed proportionally. There
    is no curve fitting, no learned weights and no hidden constant.
    """

    dimensions: dict[str, Optional[float]]
    weights: dict[str, float] = field(default_factory=lambda: dict(DIMENSION_WEIGHTS))

    @property
    def present(self) -> dict[str, float]:
        return {
            name: value for name, value in self.dimensions.items() if value is not None
        }

    @property
    def missing(self) -> list[str]:
        return [
            name
            for name in DIMENSION_ORDER
            if name in self.dimensions and self.dimensions[name] is None
        ]

    @property
    def score(self) -> Optional[float]:
        """Weighted mean over present dimensions, or ``None`` if none present."""
        present = self.present
        if not present:
            return None
        total_weight = sum(self.weights.get(name, 0.0) for name in present)
        if total_weight <= 0:
            return None
        weighted = sum(
            max(0.0, min(1.0, value)) * self.weights.get(name, 0.0)
            for name, value in present.items()
        )
        return weighted / total_weight

    @property
    def risk_score(self) -> Optional[float]:
        """The same number expressed as risk (``1 - health``)."""
        score = self.score
        return None if score is None else 1.0 - score

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "risk_score": self.risk_score,
            "method": RELIABILITY_SCORE_METHOD,
            "dimensions": [
                {
                    "name": name,
                    "value": self.dimensions.get(name),
                    "weight": self.weights.get(name),
                    "missing": self.dimensions.get(name) is None,
                }
                for name in DIMENSION_ORDER
                if name in self.dimensions
            ],
            "missing_dimensions": self.missing,
            "limitations": (
                "A dimension with no data is reported as missing, not healthy. "
                "Weights are fixed and documented; the score summarizes observed "
                "reliability signals and is not a measure of software quality."
            ),
        }


def health_from_change(
    change_rate: Optional[float], *, tolerance: float = 0.10
) -> Optional[float]:
    """Map a relative change to a health value in ``[0, 1]``.

    ``change_rate = 0`` is perfectly healthy (``1.0``); a change at or beyond
    ``tolerance`` (in either direction) is fully unhealthy (``0.0``). Written
    once, used by every dimension, so the dimensions are comparable.
    """
    if change_rate is None:
        return None
    magnitude = abs(change_rate)
    if magnitude >= tolerance:
        return 0.0
    return 1.0 - (magnitude / tolerance)


def health_from_count(count: Optional[float], *, ceiling: float) -> Optional[float]:
    """Map a non-negative count to health, saturating at ``ceiling``."""
    if count is None:
        return None
    if ceiling <= 0:
        return None
    if count <= 0:
        return 1.0
    return max(0.0, 1.0 - min(count / ceiling, 1.0))


def health_from_ratio(ratio: Optional[float]) -> Optional[float]:
    """Map a 0–1 *unhealthy share* to health (``1 - ratio``), None-safe.

    Used by dimensions that are already proportional (share of saturated
    resources, share of degraded dependencies), so those dimensions are
    normalized the same way as the change-based ones.
    """
    if ratio is None:
        return None
    return max(0.0, min(1.0, 1.0 - ratio))


def first_non_missing(values: Sequence[Optional[float]]) -> Optional[float]:
    """First non-``None`` value, so a dimension can prefer one source."""
    for value in values:
        if value is not None:
            return value
    return None


__all__ = [
    "DIMENSION_ORDER",
    "DIMENSION_WEIGHTS",
    "RELIABILITY_SCORE_METHOD",
    "RISK_ORDER",
    "ReliabilityScore",
    "RiskPolicy",
    "at_least",
    "classify_score",
    "first_non_missing",
    "health_from_change",
    "health_from_count",
    "health_from_ratio",
    "level_from_name",
    "risk_policy",
    "risk_rank",
    "risk_statement",
]
