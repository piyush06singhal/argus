"""ARGUS Deterministic Detectors (Phase 3 §11–§16).

Each detector is a pure function from an *aggregate* (already-computed values)
to a :class:`DetectionOutcome`. They never read the database, never call an LLM,
and never invent values: an outcome either fires with the numbers that caused
it, or does not fire.

Everything here is explainable by construction — the outcome carries the
observed value, the expectation, the threshold that was crossed, and a plain
sentence describing the comparison. Severity is *not* decided here (see
``app.services.anomaly_severity``); this module answers only "did this cross a
line, and by how much?".

Direction matters and is derived from the anomaly type: a throughput *drop* is
anomalous when it goes **below** expectation, while a latency *spike* is
anomalous **above** it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from app.models.anomaly import AnomalySource, AnomalyType
from app.services.baseline import BaselineResult, compute_stats

#: Anomaly types whose deviation is interesting *below* the expectation.
_BELOW_TYPES = {
    AnomalyType.THROUGHPUT_DROP,
    AnomalyType.HEALTH_DEGRADATION,
}


def direction_for(anomaly_type: AnomalyType | str) -> str:
    """Return ``"ABOVE"`` or ``"BELOW"`` for an anomaly type."""
    try:
        kind = AnomalyType(anomaly_type)
    except ValueError:
        return "ABOVE"
    return "BELOW" if kind in _BELOW_TYPES else "ABOVE"


@dataclass(frozen=True)
class DetectionOutcome:
    """The result of evaluating one deterministic condition."""

    fired: bool
    anomaly_type: AnomalyType
    source: AnomalySource
    #: Empty for a non-firing outcome — a skip is not a finding.
    description: str = ""
    observed_value: Optional[float] = None
    expected_value: Optional[float] = None
    deviation: Optional[float] = None
    threshold: Optional[float] = None
    z_score: Optional[float] = None
    #: Evidence strength in [0, 1] — how far past the line the observation is.
    #: Never a probability of causality.
    confidence: Optional[float] = None
    metric_name: Optional[str] = None
    pattern_template: Optional[str] = None
    #: Plain-language explanation for the UI / API (§52).
    reason: str = ""
    metadata: dict = field(default_factory=dict)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def error_rate(failed: Optional[float], total: Optional[float]) -> Optional[float]:
    """Compute an error rate, guarding ``total <= 0`` (§11).

    A zero total means *no requests observed*, which is not a 0% or 100% error
    rate — it is nothing to measure.
    """
    if failed is None or total is None or total <= 0:
        return None
    return float(failed) / float(total)


def rate_change(current: Optional[float], baseline: Optional[float]) -> Optional[float]:
    """Relative change of a rate, guarding a zero baseline (§11)."""
    if current is None or baseline is None or baseline == 0:
        return None
    return (float(current) - float(baseline)) / abs(float(baseline))


# ---------------------------------------------------------------------------
# Log pattern normalization (§13) — deterministic, bounded, no universal parser
# ---------------------------------------------------------------------------
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
# Leading guard only: a number immediately preceded by a word character or dot
# is part of an identifier (``user123``, ``v1.2.3``) and must be left alone,
# while a trailing unit (``12.5ms``) is still normalized.
_FLOAT_RE = re.compile(r"(?<![\w.])\d+\.\d+")
_INT_RE = re.compile(r"(?<![\w.])\d+")

#: Placeholders substituted for dynamic values. Documented so templates are
#: stable across releases (changing these would re-key every log fingerprint).
PLACEHOLDER_UUID = "<uuid>"
PLACEHOLDER_EMAIL = "<email>"
PLACEHOLDER_IP = "<ip>"
PLACEHOLDER_HEX = "<hex>"
PLACEHOLDER_NUM = "<num>"

PATTERN_MAX_LENGTH = 512


def normalize_log_pattern(message: Optional[str]) -> Optional[str]:
    """Normalize a log message into a stable comparison template (§13).

    ``ERROR request failed user_id=123`` and ``... user_id=456`` both become
    ``ERROR request failed user_id=<num>``. Replaces only well-known dynamic
    shapes (UUIDs, emails, IPs, long hex, numbers) — it is deliberately *not* a
    universal path/ID parser, so unusual messages are left as-is rather than
    wrongly collapsed.
    """
    if not message:
        return None
    text = str(message)
    text = _UUID_RE.sub(PLACEHOLDER_UUID, text)
    text = _EMAIL_RE.sub(PLACEHOLDER_EMAIL, text)
    text = _IP_RE.sub(PLACEHOLDER_IP, text)
    text = _HEX_RE.sub(PLACEHOLDER_HEX, text)
    text = _FLOAT_RE.sub(PLACEHOLDER_NUM, text)
    text = _INT_RE.sub(PLACEHOLDER_NUM, text)
    text = " ".join(text.split())
    return text[:PATTERN_MAX_LENGTH]


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------
def detect_threshold(
    *,
    observed: Optional[float],
    threshold: Optional[float],
    anomaly_type: AnomalyType,
    source: AnomalySource = AnomalySource.METRIC,
    metric_name: Optional[str] = None,
    direction: Optional[str] = None,
) -> DetectionOutcome:
    """Fire when an absolute threshold is crossed (§11)."""
    metric_ref = f" {metric_name}" if metric_name else ""
    if observed is None or threshold is None:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            reason=f"threshold evaluation skipped: missing observed value or "
            f"threshold for{metric_ref}".strip(),
        )

    direction = direction or direction_for(anomaly_type)
    crossed = observed > threshold if direction == "ABOVE" else observed < threshold
    if not crossed:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            observed_value=observed,
            expected_value=threshold,
            threshold=threshold,
            reason=(
                f"observed {observed} did not cross the {direction} threshold "
                f"{threshold}"
            ),
        )

    # Confidence grows with distance past the threshold, saturating at 1.0.
    reference = abs(threshold) or 1.0
    margin = abs(observed - threshold) / reference
    return DetectionOutcome(
        fired=True,
        anomaly_type=anomaly_type,
        source=source,
        metric_name=metric_name,
        observed_value=observed,
        expected_value=threshold,
        deviation=observed - threshold,
        threshold=threshold,
        confidence=_clamp(margin),
        description=(
            f"Observed {observed} crossed the {direction} threshold {threshold}"
        ),
        reason=f"{observed} vs threshold {threshold} (+{margin:.4f} past)",
    )


def detect_baseline_deviation(
    *,
    deviation_relative: Optional[float],
    observed: Optional[float],
    expected: Optional[float],
    multiplier: Optional[float],
    anomaly_type: AnomalyType,
    source: AnomalySource = AnomalySource.METRIC,
    metric_name: Optional[str] = None,
    sufficient: bool = True,
    direction: Optional[str] = None,
) -> DetectionOutcome:
    """Fire when a relative deviation exceeds a configured multiplier (§11).

    Uses the *relative* deviation (``(observed - expected) / |expected|``) so a
    rule means the same thing regardless of the metric's magnitude.
    """
    if not sufficient:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            reason="baseline insufficient (INSUFFICIENT_DATA)",
        )
    if deviation_relative is None or multiplier is None or observed is None:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            reason="deviation evaluation skipped: missing value or multiplier",
        )

    direction = direction or direction_for(anomaly_type)
    signed = deviation_relative if direction == "ABOVE" else -deviation_relative
    crossed = signed > multiplier
    if not crossed:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            observed_value=observed,
            expected_value=expected,
            deviation=deviation_relative,
            threshold=multiplier,
            reason=(
                f"relative deviation {deviation_relative:.4f} within "
                f"multiplier {multiplier}"
            ),
        )

    return DetectionOutcome(
        fired=True,
        anomaly_type=anomaly_type,
        source=source,
        metric_name=metric_name,
        observed_value=observed,
        expected_value=expected,
        deviation=deviation_relative,
        threshold=multiplier,
        confidence=_clamp(signed / multiplier if multiplier else 0.0),
        description=(
            f"Observed {observed} deviated {deviation_relative:+.2%} from expected "
            f"{expected} (multiplier {multiplier})"
        ),
        reason=(f"relative deviation {signed:+.4f} exceeds multiplier {multiplier}"),
    )


def detect_z_score(
    *,
    z_score: Optional[float],
    observed: Optional[float],
    expected: Optional[float],
    z_threshold: Optional[float],
    anomaly_type: AnomalyType,
    source: AnomalySource = AnomalySource.METRIC,
    metric_name: Optional[str] = None,
    sufficient: bool = True,
) -> DetectionOutcome:
    """Fire when ``|z| > threshold``, requiring real variance (§11)."""
    if not sufficient or z_score is None or z_threshold is None:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            reason=("z-score evaluation skipped: insufficient baseline or no variance"),
        )
    magnitude = abs(z_score)
    if magnitude <= z_threshold:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            observed_value=observed,
            expected_value=expected,
            z_score=z_score,
            threshold=z_threshold,
            reason=f"|z| {magnitude:.4f} within threshold {z_threshold}",
        )
    return DetectionOutcome(
        fired=True,
        anomaly_type=anomaly_type,
        source=source,
        metric_name=metric_name,
        observed_value=observed,
        expected_value=expected,
        z_score=z_score,
        threshold=z_threshold,
        confidence=_clamp(magnitude / z_threshold if z_threshold else 0.0),
        description=f"Observed {observed} is {magnitude:.2f}σ from expected {expected}",
        reason=f"|z| {magnitude:.4f} exceeds threshold {z_threshold}",
    )


def detect_error_rate(
    *,
    failed: Optional[float],
    total: Optional[float],
    threshold: Optional[float],
    anomaly_type: AnomalyType = AnomalyType.ERROR_RATE_SPIKE,
    source: AnomalySource = AnomalySource.METRIC,
    metric_name: Optional[str] = None,
) -> DetectionOutcome:
    """Fire when the error rate exceeds a threshold (§11)."""
    rate = error_rate(failed, total)
    if rate is None or threshold is None:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            reason="error rate undefined (no requests observed) or no threshold",
        )
    if rate <= threshold:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            observed_value=rate,
            expected_value=threshold,
            threshold=threshold,
            reason=f"error rate {rate:.4f} within threshold {threshold}",
        )
    return DetectionOutcome(
        fired=True,
        anomaly_type=anomaly_type,
        source=source,
        metric_name=metric_name,
        observed_value=rate,
        expected_value=threshold,
        deviation=rate - threshold,
        threshold=threshold,
        confidence=_clamp((rate - threshold) / (threshold or 1.0)),
        description=(
            f"Error rate {rate:.2%} exceeds threshold {threshold:.2%} "
            f"({int(failed or 0)}/{int(total or 0)})"
        ),
        reason=f"error rate {rate:.4f} > threshold {threshold}",
        metadata={"failed": failed, "total": total},
    )


def detect_latency_ratio(
    *,
    observed: Optional[float],
    baseline: BaselineResult,
    multiplier: Optional[float],
    anomaly_type: AnomalyType = AnomalyType.LATENCY_SPIKE,
    source: AnomalySource = AnomalySource.METRIC,
    metric_name: Optional[str] = None,
) -> DetectionOutcome:
    """Fire when ``observed > baseline * multiplier`` (§12).

    Persistence is handled by the rule engine, not here — this only measures
    one window so a single fluctuation cannot open an incident on its own.
    """
    if multiplier is None or observed is None or not baseline.sufficient:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            reason="latency ratio skipped: insufficient baseline or missing value",
        )
    expected = baseline.expected_value
    if expected is None:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            reason="latency ratio skipped: no expected value",
        )
    limit = expected * multiplier
    if observed <= limit:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            observed_value=observed,
            expected_value=expected,
            threshold=limit,
            reason=f"observed {observed} within {multiplier}x baseline {expected}",
        )
    ratio = observed / expected if expected else None
    return DetectionOutcome(
        fired=True,
        anomaly_type=anomaly_type,
        source=source,
        metric_name=metric_name,
        observed_value=observed,
        expected_value=expected,
        deviation=observed - expected,
        threshold=limit,
        confidence=_clamp((observed - limit) / (limit or 1.0)),
        description=(f"Latency {observed} exceeds {multiplier}x baseline {expected}"),
        reason=f"observed {observed} > baseline*multiplier {limit}",
        metadata={"ratio": ratio, "baseline_samples": baseline.sample_count},
    )


def detect_rate_change(
    *,
    current: Optional[float],
    baseline: Optional[float],
    threshold: Optional[float] = None,
    multiplier: Optional[float] = None,
    anomaly_type: AnomalyType = AnomalyType.REQUEST_RATE_CHANGE,
    source: AnomalySource = AnomalySource.METRIC,
    metric_name: Optional[str] = None,
    sufficient: bool = True,
) -> DetectionOutcome:
    """Fire on a significant change in a rate (§11).

    Either an absolute ``threshold`` (on the raw difference) or a relative
    ``multiplier`` (on the change ratio) may be used — but at least one is
    required, otherwise nothing can be evaluated.
    """
    if not sufficient or current is None or baseline is None:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            reason="rate change skipped: insufficient baseline or missing value",
        )
    if threshold is None and multiplier is None:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            reason="rate change skipped: neither threshold nor multiplier configured",
        )

    relative = rate_change(current, baseline)
    absolute = current - baseline
    if multiplier is not None and relative is not None:
        magnitude = abs(relative)
        limit: Optional[float] = multiplier
    else:
        magnitude = abs(absolute)
        limit = threshold
    if limit is None or magnitude <= limit:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            observed_value=current,
            expected_value=baseline,
            threshold=limit,
            reason=f"change {magnitude:.4f} within limit {limit}",
        )
    return DetectionOutcome(
        fired=True,
        anomaly_type=anomaly_type,
        source=source,
        metric_name=metric_name,
        observed_value=current,
        expected_value=baseline,
        deviation=relative if relative is not None else absolute,
        threshold=limit,
        confidence=_clamp(magnitude / limit if limit else 0.0),
        description=f"Rate changed from {baseline} to {current}",
        reason=f"rate change {magnitude:.4f} exceeds limit {limit}",
        metadata={"relative": relative, "absolute": absolute},
    )


def detect_pattern_spike(
    *,
    current_count: Optional[float],
    baseline: Optional[BaselineResult],
    multiplier: Optional[float],
    pattern_template: Optional[str] = None,
    source: AnomalySource = AnomalySource.LOG,
    metric_name: Optional[str] = None,
    anomaly_type: AnomalyType = AnomalyType.LOG_PATTERN_SPIKE,
) -> DetectionOutcome:
    """Fire when a normalized log pattern occurs far more often than usual (§13)."""
    if (
        multiplier is None
        or current_count is None
        or baseline is None
        or not baseline.sufficient
    ):
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            pattern_template=pattern_template,
            reason="pattern spike skipped: insufficient baseline or missing count",
        )
    expected = baseline.expected_value
    if expected is None:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            pattern_template=pattern_template,
            reason="pattern spike skipped: no baseline frequency",
        )
    # A baseline of zero occurrences cannot be multiplied meaningfully — treat
    # any single occurrence as the floor instead of dividing by zero.
    limit = expected * multiplier if expected > 0 else 1.0
    if current_count <= limit:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            pattern_template=pattern_template,
            observed_value=current_count,
            expected_value=expected,
            threshold=limit,
            reason=f"pattern count {current_count} within {multiplier}x baseline",
        )
    return DetectionOutcome(
        fired=True,
        anomaly_type=anomaly_type,
        source=source,
        metric_name=metric_name,
        pattern_template=pattern_template,
        observed_value=current_count,
        expected_value=expected,
        deviation=current_count - expected,
        threshold=limit,
        confidence=_clamp((current_count - limit) / (limit or 1.0)),
        description=(
            f"Log pattern '{pattern_template}' occurred {current_count} times "
            f"(baseline {expected})"
        ),
        reason=f"pattern count {current_count} > baseline*multiplier {limit}",
        metadata={"baseline_samples": baseline.sample_count},
    )


def detect_trace_failure_rate(
    *,
    failed: Optional[float],
    total: Optional[float],
    threshold: Optional[float],
    source: AnomalySource = AnomalySource.TRACE,
    metric_name: Optional[str] = None,
    anomaly_type: AnomalyType = AnomalyType.TRACE_FAILURE_SPIKE,
) -> DetectionOutcome:
    """Fire when the share of failed traces/spans exceeds a threshold (§14)."""
    rate = error_rate(failed, total)
    if rate is None or threshold is None:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            reason="trace failure rate undefined (no traces) or no threshold",
        )
    if rate <= threshold:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            observed_value=rate,
            expected_value=threshold,
            threshold=threshold,
            reason=f"trace failure rate {rate:.4f} within threshold {threshold}",
        )
    return DetectionOutcome(
        fired=True,
        anomaly_type=anomaly_type,
        source=source,
        metric_name=metric_name,
        observed_value=rate,
        expected_value=threshold,
        deviation=rate - threshold,
        threshold=threshold,
        confidence=_clamp((rate - threshold) / (threshold or 1.0)),
        description=(
            f"{rate:.2%} of traces failed ({int(failed or 0)}/{int(total or 0)})"
        ),
        reason=f"trace failure rate {rate:.4f} > threshold {threshold}",
        metadata={"failed": failed, "total": total},
    )


#: Health states ordered by worsening severity — used to detect transitions.
_HEALTH_ORDER = {"UNKNOWN": 0, "HEALTHY": 0, "DEGRADED": 1, "UNHEALTHY": 2}


def detect_health_transition(
    *,
    previous_status: Optional[str],
    current_status: Optional[str],
    source: AnomalySource = AnomalySource.HEALTH_CHECK,
    anomaly_type: AnomalyType = AnomalyType.HEALTH_DEGRADATION,
    metric_name: Optional[str] = None,
) -> DetectionOutcome:
    """Fire only on a *worsening* transition (§15).

    Repeating the same unhealthy state does **not** create a new anomaly — the
    rule engine's fingerprint/cooldown would otherwise emit one per poll.
    """
    if not previous_status or not current_status:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            reason="health transition skipped: missing previous or current state",
        )
    prev_rank = _HEALTH_ORDER.get(str(previous_status).upper(), 0)
    curr_rank = _HEALTH_ORDER.get(str(current_status).upper(), 0)
    if curr_rank <= prev_rank:
        return DetectionOutcome(
            fired=False,
            anomaly_type=anomaly_type,
            source=source,
            metric_name=metric_name,
            reason=f"health {previous_status} -> {current_status} is not a worsening",
        )
    return DetectionOutcome(
        fired=True,
        anomaly_type=anomaly_type,
        source=source,
        metric_name=metric_name,
        # Health states are ordinal, so the "value" is the severity rank.
        observed_value=float(curr_rank),
        expected_value=float(prev_rank),
        deviation=float(curr_rank - prev_rank),
        confidence=_clamp((curr_rank - prev_rank) / 2.0),
        description=f"Health degraded from {previous_status} to {current_status}",
        reason=f"health {previous_status} -> {current_status}",
        metadata={"previous_status": previous_status, "current_status": current_status},
    )


def latency_summary(durations_ms: Sequence[Optional[float]]) -> dict:
    """Deterministic latency aggregates for a window (§12).

    Returns ``None`` values (not zeros) when there is nothing to summarize, so
    an empty window cannot masquerade as healthy or unhealthy.
    """
    stats = compute_stats(durations_ms)
    return {
        "sample_count": stats.sample_count,
        "average": stats.mean,
        "median": stats.median,
        "p95": stats.p95,
        "p99": stats.p99,
        "max": stats.max_value,
    }


__all__ = [
    "DetectionOutcome",
    "direction_for",
    "error_rate",
    "rate_change",
    "normalize_log_pattern",
    "detect_threshold",
    "detect_baseline_deviation",
    "detect_z_score",
    "detect_error_rate",
    "detect_latency_ratio",
    "detect_rate_change",
    "detect_pattern_spike",
    "detect_trace_failure_rate",
    "detect_health_transition",
    "latency_summary",
]
