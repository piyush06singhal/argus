"""ARGUS Anomaly Explainability (§52).

Every anomaly must be able to answer *why was this detected?* without any model
in the loop. This module turns the stored detection context into a structured,
deterministic explanation:

* which rule and condition fired,
* which baseline strategy produced the expected value,
* the observed vs expected values and the deviation,
* the threshold (or z-threshold) that was exceeded,
* the component and the telemetry source,
* the fingerprint material, so the dedup key is inspectable.

It never states a cause and never invents a value — every field comes from the
anomaly row, its rule, or its persisted baseline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.models.anomaly import Anomaly, AnomalyBaseline, AnomalyRule, RuleCondition
from app.services.fingerprints import anomaly_fingerprint_material


def _enum_value(value: object) -> Optional[str]:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _threshold_exceeded(
    rule: Optional[AnomalyRule],
    anomaly: Anomaly,
) -> Optional[str]:
    """Describe the threshold that was exceeded, in plain language."""
    if rule is None:
        return None
    condition = _enum_value(rule.condition)
    if condition == RuleCondition.THRESHOLD.value and rule.threshold is not None:
        return (
            f"THRESHOLD: observed {anomaly.observed_value} exceeded the "
            f"configured threshold {rule.threshold}"
        )
    if (
        condition == RuleCondition.BASELINE_DEVIATION.value
        and rule.multiplier is not None
    ):
        return (
            f"BASELINE_DEVIATION: deviation exceeded {rule.multiplier}x the "
            "rolling baseline"
        )
    if condition == RuleCondition.Z_SCORE.value and rule.z_threshold is not None:
        return (
            f"Z_SCORE: |z| exceeded {rule.z_threshold} "
            f"(observed z={anomaly.z_score})"
        )
    if condition == RuleCondition.LATENCY_RATIO.value and rule.multiplier is not None:
        return (
            f"LATENCY_RATIO: latency exceeded {rule.multiplier}x the expected " "value"
        )
    if condition == RuleCondition.ERROR_RATE.value and rule.threshold is not None:
        return (
            f"ERROR_RATE: error rate exceeded {rule.threshold} "
            f"(observed {anomaly.observed_value})"
        )
    if condition == RuleCondition.RATE_CHANGE.value:
        return "RATE_CHANGE: request/error rate changed beyond the configured bound"
    if condition == RuleCondition.PATTERN_SPIKE.value and rule.multiplier is not None:
        return (
            f"PATTERN_SPIKE: log pattern frequency exceeded {rule.multiplier}x its "
            "baseline"
        )
    if (
        condition == RuleCondition.TRACE_FAILURE_RATE.value
        and rule.threshold is not None
    ):
        return (
            f"TRACE_FAILURE_RATE: failed trace ratio exceeded {rule.threshold} "
            f"(observed {anomaly.observed_value})"
        )
    return None


@dataclass(frozen=True)
class AnomalyExplanation:
    """The explainability block attached to an anomaly detail response."""

    anomaly_id: str
    why_detected: str
    detector: str
    condition: Optional[str]
    baseline_strategy: Optional[str]
    baseline_sample_count: Optional[int]
    baseline_window_seconds: Optional[int]
    observed_value: Optional[float]
    expected_value: Optional[float]
    deviation: Optional[float]
    z_score: Optional[float]
    threshold_exceeded: Optional[str]
    metric_name: Optional[str]
    pattern_template: Optional[str]
    component_id: Optional[str]
    environment_id: Optional[str]
    telemetry_source: Optional[str]
    source_event_id: Optional[str]
    fingerprint_material: str
    confidence_meaning: str = (
        "confidence is evidence strength from the detecting rule, not the "
        "probability that anything caused anything"
    )

    def as_dict(self) -> dict:
        return {
            "anomaly_id": self.anomaly_id,
            "why_detected": self.why_detected,
            "detector": self.detector,
            "condition": self.condition,
            "baseline_strategy": self.baseline_strategy,
            "baseline_sample_count": self.baseline_sample_count,
            "baseline_window_seconds": self.baseline_window_seconds,
            "observed_value": self.observed_value,
            "expected_value": self.expected_value,
            "deviation": self.deviation,
            "z_score": self.z_score,
            "threshold_exceeded": self.threshold_exceeded,
            "metric_name": self.metric_name,
            "pattern_template": self.pattern_template,
            "component_id": self.component_id,
            "environment_id": self.environment_id,
            "telemetry_source": self.telemetry_source,
            "source_event_id": self.source_event_id,
            "fingerprint_material": self.fingerprint_material,
            "confidence_meaning": self.confidence_meaning,
        }


def build_anomaly_explanation(
    anomaly: Anomaly,
    *,
    rule: Optional[AnomalyRule] = None,
    baseline: Optional[AnomalyBaseline] = None,
) -> AnomalyExplanation:
    """Assemble the deterministic explanation for one anomaly."""
    discriminator = anomaly.metric_name or anomaly.pattern_template
    material = anomaly_fingerprint_material(
        project_id=anomaly.project_id,
        anomaly_type=anomaly.anomaly_type,
        discriminator=discriminator,
        environment_id=anomaly.environment_id,
        component_id=anomaly.component_id,
    )

    condition = _enum_value(rule.condition) if rule else None
    detector = "rule_engine"
    if condition is None:
        detector = "correlation_context" if anomaly.incident_id else "rule_engine"

    threshold = _threshold_exceeded(rule, anomaly)
    observed = anomaly.observed_value
    expected = anomaly.expected_value
    if threshold:
        why = threshold
    elif observed is not None and expected is not None:
        why = (
            f"observed value {observed} deviated from the expected value " f"{expected}"
        )
    elif anomaly.description:
        why = anomaly.description
    else:
        why = (
            f"{_enum_value(anomaly.anomaly_type)} was detected from "
            f"{_enum_value(anomaly.source)} evidence"
        )

    return AnomalyExplanation(
        anomaly_id=str(anomaly.id),
        why_detected=why,
        detector=detector,
        condition=condition,
        baseline_strategy=(
            _enum_value(rule.baseline_strategy)
            if rule
            else (_enum_value(baseline.strategy) if baseline else None)
        ),
        baseline_sample_count=baseline.sample_count if baseline else None,
        baseline_window_seconds=baseline.window_seconds if baseline else None,
        observed_value=observed,
        expected_value=expected,
        deviation=anomaly.deviation,
        z_score=anomaly.z_score,
        threshold_exceeded=threshold,
        metric_name=anomaly.metric_name,
        pattern_template=anomaly.pattern_template,
        component_id=str(anomaly.component_id) if anomaly.component_id else None,
        environment_id=(
            str(anomaly.environment_id) if anomaly.environment_id else None
        ),
        telemetry_source=_enum_value(anomaly.source),
        source_event_id=anomaly.source_event_id,
        fingerprint_material=material,
    )


__all__ = ["AnomalyExplanation", "build_anomaly_explanation"]
