"""ARGUS Baseline Predictors (Phase 8 §19–§22, §35, §83).

Deterministic, statistically transparent predictors. Every one of them is a
documented rule over numbers that already exist in a feature snapshot: no
randomness, no learned weights, no black box.

The shared contract — :class:`ReliabilityPredictor` — is what makes Phase 8
extendable without a rewrite (§23): ``predict`` produces a draft,
``explain`` renders it, ``validate`` checks the draft against its own
invariants. An ML provider implements the same three methods and nothing else
in the phase has to change.

Four predictors ship:

* :class:`RollingTrendPredictor` — is a metric's *slope* heading toward a
  known-bad region (§20)?
* :class:`EWMAReliabilityPredictor` — is a noisy metric *sustained* at an
  elevated level rather than spiking once (§21)?
* :class:`ThresholdTrajectoryPredictor` — is a resource on a bounded path to a
  configured ceiling (§16, §20)?
* :class:`HistoricalFrequencyPredictor` — is a recurring event happening more
  often than its own history says it should (§22)?

Two rules are structural:

* **Nothing is extrapolated past its bound.** Trajectories are projected with
  :func:`reliability_stats.bounded_linear_projection`; a denominator is never
  zero. A forecast may say "heading toward", never "will reach".
* **A rule without evidence is not a zero.** Every rule whose inputs are
  missing is excluded from the score's denominator *and* recorded, so "we could
  not evaluate this" never reads as "this looked fine" (§83).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from app.core.config import get_settings
from app.models.reliability import (
    FeatureTrend,
    ForecastDataQuality,
    ForecastHorizon,
    PredictiveSignalType,
    PredictionType,
    ReliabilityModelType,
    SignalSeverity,
)
from app.services import reliability_stats as stats
from app.services.reliability_features import FeatureBundle

settings = get_settings()

#: Below this many usable samples a *series* cannot support a trend claim.
MIN_TREND_SAMPLES = 3

#: Intensity→severity bands. Documented rather than tuned: an intensity at or
#: above 0.6 is treated as a strong signal, 0.3 as moderate, below as weak.
SEVERITY_HIGH_INTENSITY = 0.6
SEVERITY_MEDIUM_INTENSITY = 0.3

#: Scale at which each rule's driver is considered "fully fired". The units are
#: stated in each rule's docstring so a reader can disagree with the number
#: rather than having to reverse-engineer it.
DEFAULT_SCALES: dict[str, float] = {
    #: Relative slope expressed per HOUR: 0.4 means "moving 40% of its mean per
    #: hour" is a fully-fired trend. Per hour rather than per sample so the
    #: score does not change when a scrape interval changes (§8).
    "trend_slope": 0.40,
    #: Relative change versus the window mean.
    "change_rate": 0.5,
    #: Absolute error-rate level (0.10 = 10% of requests failing).
    "error_rate_level": 0.10,
    #: Share of spans above twice the median.
    "tail_frequency": 0.10,
    #: Share of outgoing dependencies currently degraded.
    "degraded_dependency_share": 0.34,
    #: Share of resource metrics at or above saturation.
    "saturation_share": 0.50,
    #: Anomalies per hour in the window.
    "anomaly_density": 4.0,
    #: Deployments in the last day.
    "deployment_count_24h": 6.0,
    #: Share of deployments that failed or were rolled back.
    "deployment_instability": 0.25,
    #: Recent regression signals (Phase 6 ``ERROR_PRONE_PATH`` count).
    "regression_signals": 5.0,
    #: Code churn count (``FREQUENTLY_CHANGED`` signals).
    "code_churn": 10.0,
    #: Recurring incidents (incidents sharing a fingerprint).
    "recurring_incidents": 2.0,
    #: Observed incidents in the last 7 days.
    "incidents_7d": 4.0,
    #: Relative growth of incident frequency versus the previous week.
    "frequency_trend": 0.5,
}

#: Quality → confidence multiplier (§7). A forecast from partial telemetry is
#: never as confident as one from complete telemetry.
_QUALITY_FACTOR: dict[ForecastDataQuality, float] = {
    ForecastDataQuality.GOOD: 1.0,
    ForecastDataQuality.PARTIAL: 0.7,
    ForecastDataQuality.POOR: 0.4,
    ForecastDataQuality.INSUFFICIENT: 0.0,
}


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def intensity(value: Optional[float], scale: float) -> Optional[float]:
    """Map a driver value onto ``[0, 1]`` against a declared scale.

    ``None`` in → ``None`` out: missing evidence is never scored as calm.
    """
    if value is None:
        return None
    if scale <= 0:
        return None
    return clamp01(abs(value) / scale)


def severity_for(value: Optional[float]) -> SignalSeverity:
    if value is None:
        return SignalSeverity.LOW
    if value >= SEVERITY_HIGH_INTENSITY:
        return SignalSeverity.HIGH
    if value >= SEVERITY_MEDIUM_INTENSITY:
        return SignalSeverity.MEDIUM
    return SignalSeverity.LOW


@dataclass
class SignalDraft:
    """A contributing predictive signal before it is persisted (§35)."""

    signal_type: PredictiveSignalType
    description: str
    contribution: Optional[float] = None
    severity: SignalSeverity = SignalSeverity.LOW
    metric_name: Optional[str] = None
    observed_value: Optional[float] = None
    baseline_value: Optional[float] = None
    change_rate: Optional[float] = None
    trend: FeatureTrend = FeatureTrend.UNKNOWN
    evidence_ids: dict = field(default_factory=dict)
    similar_incident_count: int = 0
    #: Name of the rule that produced it — the audit trail for §82/§83.
    rule: str = ""

    @property
    def rankable(self) -> float:
        return self.contribution if self.contribution is not None else 0.0


@dataclass
class PredictionDraft:
    """One prediction, with everything needed to explain and audit it."""

    prediction_type: PredictionType
    horizon: ForecastHorizon
    #: ``None`` means "not enough evidence to put a number on it" (§7).
    risk_score: Optional[float]
    confidence: Optional[float]
    confidence_reason: str
    signals: list[SignalDraft] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    #: Rules that could not be evaluated, with the reason. Never silent.
    unevaluated: list[dict] = field(default_factory=list)
    model_type: ReliabilityModelType = ReliabilityModelType.ROLLING_TREND
    #: Free-text, human-readable summary of the trajectory.
    headline: str = ""

    @property
    def dominant_signal(self) -> Optional[str]:
        if not self.signals:
            return None
        best = max(self.signals, key=lambda s: (s.rankable, s.signal_type.value))
        return best.signal_type.value


class ReliabilityPredictor(ABC):
    """Provider-neutral predictor interface (§19, §23).

    ``predict`` is the only method that must produce numbers; ``explain``
    renders them and ``validate`` asserts the draft's own invariants. Keeping
    ``validate`` on the interface means a future ML provider is held to the
    same internal consistency the deterministic ones are, without the caller
    knowing which kind it is holding.
    """

    #: Stable identifier written into ``model_version_label`` (§82).
    name: str = "predictor"
    model_type: ReliabilityModelType = ReliabilityModelType.ROLLING_TREND
    version: str = "1"
    #: Deterministic hyper-parameters, persisted with the model version.
    parameters: dict[str, Any] = {}

    @abstractmethod
    def predict(
        self,
        bundle: FeatureBundle,
        prediction_type: PredictionType,
        horizon: ForecastHorizon,
    ) -> PredictionDraft:
        """Produce one draft. Must never raise for thin data — return
        ``risk_score=None`` and a limitation instead."""

    def explain(self, draft: PredictionDraft) -> str:
        """Render a draft as a short, honest sentence."""
        subject = self.name
        if draft.risk_score is None:
            return (
                f"{subject}: no forecast could be produced for "
                f"{draft.prediction_type.value} over {draft.horizon.label} — "
                "insufficient evaluable evidence"
            )
        top = draft.signals[:3]
        drivers = ", ".join(s.description for s in top) if top else "no signal fired"
        return (
            f"{subject} predicts {draft.prediction_type.value} over "
            f"{draft.horizon.label} from: {drivers}"
        )

    def validate(self, draft: PredictionDraft) -> list[str]:
        """Check the draft against its own invariants; returns problems found.

        These are *internal consistency* checks, not accuracy checks: a
        forecast can be wrong and still be valid. What must never happen is a
        score outside ``[0, 1]``, a numeric score with no signals behind it, or
        a confident number resting on insufficient data.
        """
        problems: list[str] = []
        if draft.risk_score is not None and not 0.0 <= draft.risk_score <= 1.0:
            problems.append(f"risk_score {draft.risk_score} is outside [0, 1]")
        if draft.confidence is not None and not 0.0 <= draft.confidence <= 1.0:
            problems.append(f"confidence {draft.confidence} is outside [0, 1]")
        if draft.risk_score is not None and not draft.signals:
            problems.append("a numeric risk score was produced with no signals")
        if draft.risk_score is None and not draft.limitations:
            problems.append("an unavailable forecast must state a limitation")
        for signal in draft.signals:
            if signal.contribution is not None and not (
                0.0 <= signal.contribution <= 1.0
            ):
                problems.append(
                    f"signal {signal.signal_type.value} contribution "
                    f"{signal.contribution} is outside [0, 1]"
                )
        return problems


# ---------------------------------------------------------------------------
# Shared scoring machinery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One evaluable predictive rule.

    ``weight`` is the rule's share of the risk score. Weights are declared per
    predictor and documented; they are *not* fitted, which is the point — a
    reviewer can read the number and disagree with it (§83).
    """

    key: str
    signal_type: PredictiveSignalType
    weight: float
    description: str
    #: Feature names this rule reads, in priority order. The first present wins.
    drivers: tuple[str, ...]
    scale_key: str
    metric_name: Optional[str] = None


def _evaluate_rule(
    rule: Rule,
    bundle: FeatureBundle,
) -> tuple[Optional[SignalDraft], Optional[dict]]:
    """Evaluate one rule, returning either a draft or an unevaluated record."""
    for driver in rule.drivers:
        value = bundle.numeric.get(driver)
        if value is None:
            continue
        scale = DEFAULT_SCALES[rule.scale_key]
        score = intensity(value, scale)
        if score is None:
            continue
        baseline_value = None
        if driver.endswith("_current"):
            baseline_value = bundle.numeric.get(driver.rsplit("_", 1)[0] + "_mean")
        elif driver.endswith("_slope"):
            baseline_value = 0.0
        return (
            SignalDraft(
                signal_type=rule.signal_type,
                description=rule.description.format(
                    value=value,
                    metric=bundle.detail.get("component_name") or "the component",
                ),
                contribution=score,
                severity=severity_for(score),
                metric_name=rule.metric_name or driver,
                observed_value=value,
                baseline_value=baseline_value,
                change_rate=bundle.numeric.get(
                    driver.replace("_slope", "_change_rate")
                ),
                trend=bundle.trend(driver.replace("_slope", "")),
                rule=rule.key,
            ),
            None,
        )
    return None, {
        "rule": rule.key,
        "signals": rule.signal_type.value,
        "reason": (
            "none of the features this rule needs were present in the snapshot: "
            + ", ".join(rule.drivers)
        ),
    }


def _draft_from_rules(
    *,
    predictor: ReliabilityPredictor,
    bundle: FeatureBundle,
    prediction_type: PredictionType,
    horizon: ForecastHorizon,
    rules: Sequence[Rule],
    headline_template: str,
    extra_limitations: Sequence[str] = (),
) -> PredictionDraft:
    """Score a rule set into a draft, excluding unevaluable rules transparently.

    The score is a weighted mean over the rules that *could* be evaluated, so a
    rule with no data neither inflates nor deflates risk. That denominator
    choice is the difference between "we saw little" and "we could not look",
    and the ``unevaluated`` list records the second case.
    """
    drafts: list[SignalDraft] = []
    unevaluated: list[dict] = []
    for rule in rules:
        draft, missing = _evaluate_rule(rule, bundle)
        if draft is not None:
            drafts.append(draft)
        elif missing is not None:
            unevaluated.append(missing)

    limitations = list(extra_limitations)
    if bundle.quality is ForecastDataQuality.INSUFFICIENT:
        limitations.append(
            "the evidence available before the forecast time was insufficient "
            "to form a baseline, so no forecast was produced"
        )
    elif bundle.quality is ForecastDataQuality.POOR:
        limitations.append(
            "telemetry coverage was poor; the forecast rests on a partial view"
        )
    for record in unevaluated:
        limitations.append(
            f"rule '{record['rule']}' could not be evaluated: {record['reason']}"
        )

    weighted = [(d, rule) for d, rule in zip(drafts, _matched_rules(rules, drafts))]
    evaluable_weight = sum(rule.weight for _d, rule in weighted)
    if bundle.quality is ForecastDataQuality.INSUFFICIENT or evaluable_weight <= 0:
        return PredictionDraft(
            prediction_type=prediction_type,
            horizon=horizon,
            risk_score=None,
            confidence=None,
            confidence_reason=(
                "no evaluable rule had data, so no risk score was produced"
            ),
            signals=[],
            limitations=limitations,
            unevaluated=unevaluated,
            evidence={"evaluable_weight": 0.0},
            model_type=predictor.model_type,
            headline=(
                f"Prediction unavailable for {prediction_type.value} over "
                f"{horizon.label} — insufficient historical evidence"
            ),
        )

    score = sum(d.rankable * r.weight for d, r in weighted) / evaluable_weight
    signals = sorted(drafts, key=lambda s: (-s.rankable, s.signal_type.value))
    confidence, confidence_reason = _confidence(bundle, len(drafts), len(rules))

    headline = headline_template.format(
        component=bundle.detail.get("component_name") or "the component",
        radar=horizon.label,
        level=_band_word(score),
    )
    return PredictionDraft(
        prediction_type=prediction_type,
        horizon=horizon,
        risk_score=clamp01(score),
        confidence=confidence,
        confidence_reason=confidence_reason,
        signals=signals,
        limitations=limitations,
        evidence={
            "evaluable_weight": evaluable_weight,
            "evaluable_rules": len(drafts),
            "declared_rules": len(rules),
            "data_coverage": bundle.coverage,
            "data_quality": bundle.quality.value,
        },
        unevaluated=unevaluated,
        model_type=predictor.model_type,
        headline=headline,
    )


def _matched_rules(rules: Sequence[Rule], drafts: Sequence[SignalDraft]) -> list[Rule]:
    """Pair each evaluated draft back to its rule, preserving declaration order.

    ``_evaluate_rule`` returns drafts in the order the rules were declared, so
    a positional pairing is exact; the ``rule`` key is asserted to keep the two
    lists from silently desynchronizing if that ever changes.
    """
    by_key = {rule.key: rule for rule in rules}
    matched: list[Rule] = []
    for draft in drafts:
        rule = by_key.get(draft.rule)
        if rule is None:
            raise ValueError(f"draft {draft.rule} has no matching rule")
        matched.append(rule)
    return matched


def _band_word(score: float) -> str:
    if score >= 0.6:
        return "elevated"
    if score >= 0.3:
        return "moderate"
    return "low"


def _confidence(
    bundle: FeatureBundle, evaluated: int, declared: int
) -> tuple[float, str]:
    """Confidence in the *level*, from data quality, coverage and rule coverage.

    Three declared components, each bounded, so the number is explainable:

    * 50% — data-quality verdict (GOOD/PARTIAL/POOR/INSUFFICIENT);
    * 30% — share of telemetry families that produced data;
    * 20% — share of declared rules that could be evaluated.

    This is confidence in the forecast, not a probability of failure, and the
    API says so wherever it is shown (§7).
    """
    quality = _QUALITY_FACTOR.get(bundle.quality, 0.0)
    rule_share = (evaluated / declared) if declared else 0.0
    value = clamp01(0.5 * quality + 0.3 * bundle.coverage + 0.2 * rule_share)
    reason = (
        "confidence in the risk level (not a probability of failure): "
        f"data quality {bundle.quality.value}, telemetry coverage "
        f"{bundle.coverage:.0%}, evaluable rules {evaluated}/{declared}"
    )
    return value, reason


# ---------------------------------------------------------------------------
# Rules, shared across predictors
# ---------------------------------------------------------------------------

LATENCY_TREND_RULES: tuple[Rule, ...] = (
    Rule(
        key="latency_p95_trend",
        signal_type=PredictiveSignalType.LATENCY_INCREASING,
        weight=0.45,
        description="p95 latency is trending {value:+.2%} of its mean per hour",
        drivers=("latency_p95_slope", "latency_p99_slope", "span_latency_slope"),
        scale_key="trend_slope",
        metric_name="latency_p95",
    ),
    Rule(
        key="latency_level_vs_baseline",
        signal_type=PredictiveSignalType.LATENCY_INCREASING,
        weight=0.30,
        description="p95 latency sits {value:+.2%} away from its day baseline",
        drivers=("latency_p95_deviation_from_baseline",),
        scale_key="change_rate",
        metric_name="latency_p95",
    ),
    Rule(
        key="tail_latency_frequency",
        signal_type=PredictiveSignalType.LATENCY_INCREASING,
        weight=0.25,
        description="tail latency (above 2x the median) affects {value:.2%} of spans",
        drivers=("tail_latency_frequency",),
        scale_key="tail_frequency",
        metric_name="span.duration_ms",
    ),
)

ERROR_RATE_RULES: tuple[Rule, ...] = (
    Rule(
        key="error_rate_level",
        signal_type=PredictiveSignalType.ERROR_RATE_INCREASING,
        weight=0.40,
        description="error rate is at {value:.4f} (0.10 = 10% of requests)",
        drivers=("error_rate_current", "span_error_rate"),
        scale_key="error_rate_level",
        metric_name="error_rate",
    ),
    Rule(
        key="error_rate_trend",
        signal_type=PredictiveSignalType.ERROR_RATE_INCREASING,
        weight=0.35,
        description="error rate is changing {value:+.2%} of its mean per hour",
        drivers=("error_rate_slope", "span_error_rate_trend"),
        scale_key="trend_slope",
        metric_name="error_rate",
    ),
    Rule(
        key="error_volume_change",
        signal_type=PredictiveSignalType.ERROR_RATE_INCREASING,
        weight=0.25,
        description="error log volume changed {value:+.2%} versus the start of the window",
        drivers=("error_rate_change",),
        scale_key="change_rate",
        metric_name="log.error_rate",
    ),
)

RESOURCE_RULES: tuple[Rule, ...] = (
    Rule(
        key="resource_saturation",
        signal_type=PredictiveSignalType.RESOURCE_SATURATION,
        weight=0.50,
        description="saturation affects {value:.2%} of measurable resource metrics",
        drivers=("resource_saturation_rate",),
        scale_key="saturation_share",
    ),
    Rule(
        key="cpu_trend",
        signal_type=PredictiveSignalType.RESOURCE_SATURATION,
        weight=0.25,
        description="CPU utilisation is changing {value:+.2%} of its mean per hour",
        drivers=("cpu_utilization_slope",),
        scale_key="trend_slope",
        metric_name="cpu_utilization",
    ),
    Rule(
        key="memory_trend",
        signal_type=PredictiveSignalType.RESOURCE_SATURATION,
        weight=0.15,
        description="memory utilisation is changing {value:+.2%} of its mean per hour",
        drivers=("memory_utilization_slope",),
        scale_key="trend_slope",
        metric_name="memory_utilization",
    ),
    Rule(
        key="queue_trend",
        signal_type=PredictiveSignalType.RESOURCE_SATURATION,
        weight=0.10,
        description="queue depth is changing {value:+.2%} of its mean per hour",
        drivers=("queue_depth_slope", "connection_pool_usage_slope"),
        scale_key="trend_slope",
        metric_name="queue_depth",
    ),
)

DEPENDENCY_RULES: tuple[Rule, ...] = (
    Rule(
        key="degraded_dependencies",
        signal_type=PredictiveSignalType.DEPENDENCY_DEGRADATION,
        weight=0.40,
        description=(
            "{value:.2%} of this component's dependencies are currently degraded"
        ),
        drivers=("dependency_failure_frequency",),
        scale_key="degraded_dependency_share",
    ),
    Rule(
        key="dependency_latency_trend",
        signal_type=PredictiveSignalType.DEPENDENCY_DEGRADATION,
        weight=0.35,
        description=(
            "dependency latency is trending {value:+.2%} of its mean per hour"
        ),
        drivers=("dependency_latency_trend",),
        scale_key="trend_slope",
    ),
    Rule(
        key="dependency_anomaly_density",
        signal_type=PredictiveSignalType.ANOMALY_CLUSTER,
        weight=0.25,
        description="anomalies are occurring at {value:.2f} per hour in this scope",
        drivers=("component_anomaly_density",),
        scale_key="anomaly_density",
    ),
)

ANOMALY_RULES: tuple[Rule, ...] = (
    Rule(
        key="anomaly_density",
        signal_type=PredictiveSignalType.ANOMALY_CLUSTER,
        weight=0.55,
        description="anomalies are occurring at {value:.2f} per hour in this scope",
        drivers=("component_anomaly_density",),
        scale_key="anomaly_density",
    ),
    Rule(
        key="repeated_anomalies",
        signal_type=PredictiveSignalType.ANOMALY_CLUSTER,
        weight=0.45,
        description="repeated anomaly patterns: {value:.0f}",
        drivers=("repeated_anomaly_patterns",),
        scale_key="recurring_incidents",
    ),
)

DEPLOYMENT_RULES: tuple[Rule, ...] = (
    Rule(
        key="deployment_frequency",
        signal_type=PredictiveSignalType.DEPLOYMENT_INSTABILITY,
        weight=0.35,
        description="deployments in the last 24h: {value:.0f}",
        drivers=("deployments_last_24h",),
        scale_key="deployment_count_24h",
    ),
    Rule(
        key="deployment_failures",
        signal_type=PredictiveSignalType.DEPLOYMENT_INSTABILITY,
        weight=0.40,
        description=("{value:.2%} of observed deployments failed or were rolled back"),
        drivers=("deployment_failure_frequency",),
        scale_key="deployment_instability",
    ),
    Rule(
        key="rollback_frequency",
        signal_type=PredictiveSignalType.DEPLOYMENT_INSTABILITY,
        weight=0.25,
        description="rollbacks account for {value:.2%} of observed deployments",
        drivers=("rollback_frequency",),
        scale_key="deployment_instability",
    ),
)

REGRESSION_RULES: tuple[Rule, ...] = (
    Rule(
        key="regression_signals",
        signal_type=PredictiveSignalType.RECENT_REGRESSION_SIGNAL,
        weight=0.45,
        description="code locations flagged as likely regression sites: {value:.0f}",
        drivers=("recent_regression_signals",),
        scale_key="regression_signals",
    ),
    Rule(
        key="code_churn",
        signal_type=PredictiveSignalType.CODE_CHURN_RISK,
        weight=0.30,
        description="recently churned code signals: {value:.0f}",
        drivers=("recent_code_churn", "recent_code_changes"),
        scale_key="code_churn",
    ),
    Rule(
        key="failed_patches",
        signal_type=PredictiveSignalType.CODE_CHURN_RISK,
        weight=0.25,
        description="failed fix attempts recorded: {value:.0f}",
        drivers=("failed_patch_count",),
        scale_key="regression_signals",
    ),
)

INCIDENT_RULES: tuple[Rule, ...] = (
    Rule(
        key="recurring_incidents",
        signal_type=PredictiveSignalType.RECURRENT_INCIDENT_PATTERN,
        weight=0.35,
        description="incidents repeating an earlier pattern: {value:.0f}",
        drivers=("recurring_incident_count",),
        scale_key="recurring_incidents",
    ),
    Rule(
        key="incident_frequency",
        signal_type=PredictiveSignalType.FAILURE_FREQUENCY_INCREASING,
        weight=0.35,
        description="incidents in the last 7 days: {value:.0f}",
        drivers=("incidents_last_7d",),
        scale_key="incidents_7d",
    ),
    Rule(
        key="incident_frequency_trend",
        signal_type=PredictiveSignalType.FAILURE_FREQUENCY_INCREASING,
        weight=0.30,
        description="incident frequency changed {value:+.2%} versus the prior week",
        drivers=("incident_frequency_trend",),
        scale_key="frequency_trend",
    ),
)

FAILURE_RULES: tuple[Rule, ...] = (
    *ERROR_RATE_RULES,
    Rule(
        key="span_failure_rate",
        signal_type=PredictiveSignalType.FAILURE_FREQUENCY_INCREASING,
        weight=0.30,
        description="span failure rate is at {value:.4f}",
        drivers=("span_error_rate",),
        scale_key="error_rate_level",
        metric_name="trace.failure_rate",
    ),
    Rule(
        key="open_incidents",
        signal_type=PredictiveSignalType.RECURRENT_INCIDENT_PATTERN,
        weight=0.20,
        description="open or investigating incidents: {value:.0f}",
        drivers=("open_incident_count",),
        scale_key="recurring_incidents",
    ),
)

DEGRADATION_RULES: tuple[Rule, ...] = (
    *LATENCY_TREND_RULES,
    *ERROR_RATE_RULES,
    Rule(
        key="degradation_anomaly_density",
        signal_type=PredictiveSignalType.ANOMALY_CLUSTER,
        weight=0.25,
        description="anomalies are occurring at {value:.2f} per hour",
        drivers=("component_anomaly_density",),
        scale_key="anomaly_density",
    ),
)


#: Which rule set each prediction type scores with (§4). Declared as data so
#: the mapping is inspectable and a new type is one entry, not a branch.
RULE_SETS: dict[PredictionType, tuple[Rule, ...]] = {
    PredictionType.LATENCY_RISK: LATENCY_TREND_RULES,
    PredictionType.ERROR_RATE_RISK: ERROR_RATE_RULES,
    PredictionType.RESOURCE_EXHAUSTION_RISK: RESOURCE_RULES,
    PredictionType.DEPENDENCY_FAILURE_RISK: DEPENDENCY_RULES,
    PredictionType.REGRESSION_RISK: REGRESSION_RULES,
    PredictionType.INCIDENT_RISK: INCIDENT_RULES,
    PredictionType.FAILURE_RISK: FAILURE_RULES,
    PredictionType.AVAILABILITY_RISK: (
        *FAILURE_RULES,
        Rule(
            key="availability_latency",
            signal_type=PredictiveSignalType.LATENCY_INCREASING,
            weight=0.20,
            description="latency is trending {value:+.2%} of its mean per hour",
            drivers=("latency_p95_slope", "span_latency_slope"),
            scale_key="trend_slope",
            metric_name="latency_p95",
        ),
    ),
    PredictionType.RELIABILITY_DEGRADATION: DEGRADATION_RULES,
}

#: Headlines per type. ``{component}``/``{radar}``/``{level}`` are filled in by
#: the scoring helper, and every one is phrased as risk, never as a prediction
#: of certain failure (§1).
HEADLINES: dict[PredictionType, str] = {
    PredictionType.FAILURE_RISK: (
        "{component} shows {level} predicted failure risk over the next {radar}"
    ),
    PredictionType.ERROR_RATE_RISK: (
        "{component} shows {level} predicted error-rate risk over the next {radar}"
    ),
    PredictionType.LATENCY_RISK: (
        "{component} shows {level} predicted latency risk over the next {radar}"
    ),
    PredictionType.AVAILABILITY_RISK: (
        "{component} shows {level} predicted availability risk over the next {radar}"
    ),
    PredictionType.RESOURCE_EXHAUSTION_RISK: (
        "{component} shows {level} predicted resource-exhaustion risk over the "
        "next {radar}"
    ),
    PredictionType.DEPENDENCY_FAILURE_RISK: (
        "{component} shows {level} predicted dependency-failure risk over the "
        "next {radar}"
    ),
    PredictionType.REGRESSION_RISK: (
        "{component} shows {level} predicted regression risk over the next {radar}"
    ),
    PredictionType.INCIDENT_RISK: (
        "{component} shows {level} predicted incident risk over the next {radar}"
    ),
    PredictionType.RELIABILITY_DEGRADATION: (
        "{component} shows {level} predicted reliability degradation over the "
        "next {radar}"
    ),
}


class _RuleSetPredictor(ReliabilityPredictor):
    """Shared implementation: score a declared rule set for a prediction type.

    Kept as the base for every deterministic predictor so the *only* difference
    between them is the rule set, the model type it reports and its parameters.
    A second implementation of the scoring loop is exactly how two predictors
    end up disagreeing about what a score means.
    """

    model_type: ReliabilityModelType = ReliabilityModelType.ROLLING_TREND
    #: Predictors may restrict themselves to a subset of prediction types;
    #: ``None`` means "any type with a rule set".
    supported_types: Optional[frozenset[PredictionType]] = None

    def supports(self, prediction_type: PredictionType) -> bool:
        if prediction_type not in RULE_SETS:
            return False
        if self.supported_types is None:
            return True
        return prediction_type in self.supported_types

    def predict(
        self,
        bundle: FeatureBundle,
        prediction_type: PredictionType,
        horizon: ForecastHorizon,
    ) -> PredictionDraft:
        rules = RULE_SETS.get(prediction_type)
        if not rules:
            return PredictionDraft(
                prediction_type=prediction_type,
                horizon=horizon,
                risk_score=None,
                confidence=None,
                confidence_reason="no rule set is declared for this prediction type",
                limitations=["no rule set is declared for this prediction type"],
                model_type=self.model_type,
                headline=(
                    f"Prediction unavailable for {prediction_type.value} — no "
                    "model is registered for it"
                ),
            )
        draft = _draft_from_rules(
            predictor=self,
            bundle=bundle,
            prediction_type=prediction_type,
            horizon=horizon,
            rules=rules,
            headline_template=HEADLINES.get(
                prediction_type, "{component} shows {level} predicted risk"
            ),
            extra_limitations=self.limitations(bundle, horizon),
        )
        draft.model_type = self.model_type
        return draft

    def limitations(self, bundle: FeatureBundle, horizon: ForecastHorizon) -> list[str]:
        """Predictor-specific limitations, always stated (§34, §89)."""
        return []


class RollingTrendPredictor(_RuleSetPredictor):
    """Is a metric's slope heading toward a known-bad region (§20)?

    Uses the normalized least-squares slope over the feature window and the
    deviation from the previous day's baseline. The slope is *not*
    extrapolated: the predictor reports that a metric is moving, and the
    threshold-trajectory predictor is the one that talks about reaching a
    ceiling.
    """

    name = "rolling_trend"
    model_type = ReliabilityModelType.ROLLING_TREND
    version = "1"
    parameters = {
        "slope_window_seconds": settings.RELIABILITY_FEATURE_WINDOW_SECONDS,
        "flat_epsilon": settings.RELIABILITY_FLAT_EPSILON,
        "volatile_ratio": settings.RELIABILITY_VOLATILE_RATIO,
        "scales": {
            "trend_slope": DEFAULT_SCALES["trend_slope"],
            "change_rate": DEFAULT_SCALES["change_rate"],
        },
    }

    def limitations(self, bundle: FeatureBundle, horizon: ForecastHorizon) -> list[str]:
        return [
            "a rising trend is evidence that risk is increasing, not that a "
            "failure will occur",
            f"the trend is measured over the last {bundle.window_seconds // 60} "
            "minutes of telemetry and is not extrapolated",
        ]


class EWMAReliabilityPredictor(_RuleSetPredictor):
    """Is a noisy metric sustained at an elevated level (§21)?

    The EWMA smooths the series so a single spike cannot carry a forecast, and
    the alpha is exposed as a parameter with the reason it exists: a smaller
    alpha weights history more, which is the right trade for a metric that is
    individually noisy but meaningful in aggregate.
    """

    name = "ewma"
    model_type = ReliabilityModelType.EWMA
    version = "1"
    parameters = {
        "alpha": settings.RELIABILITY_EWMA_ALPHA,
        "alpha_rationale": (
            "weight of the newest observation; lower values weight history more, "
            "which is appropriate for individually noisy metrics"
        ),
        "window_seconds": settings.RELIABILITY_FEATURE_WINDOW_SECONDS,
    }

    def limitations(self, bundle: FeatureBundle, horizon: ForecastHorizon) -> list[str]:
        return [
            "the EWMA reflects the level a metric is sustained at; it does not "
            "model seasonality or a step change in workload",
        ]


class ThresholdTrajectoryPredictor(_RuleSetPredictor):
    """Is a resource on a bounded path to a configured ceiling (§16, §20)?

    The projection is bounded by
    :data:`~app.services.reliability_stats.bounded_linear_projection` and the
    ceilings come from settings, so "approaching saturation" is always a
    statement against a number an operator set — not a guess about what
    "high" means.
    """

    name = "threshold_trajectory"
    model_type = ReliabilityModelType.THRESHOLD_TRAJECTORY
    version = "1"
    parameters = {
        "saturation_ratio": settings.RELIABILITY_SATURATION_RATIO,
        "max_growth_ratio": settings.RELIABILITY_MAX_PROJECTION_GROWTH,
        "ceilings": {
            "latency_ms": settings.RELIABILITY_LATENCY_CEILING_MS,
            "error_rate": settings.RELIABILITY_ERROR_RATE_CEILING,
            "cpu_percent": settings.RELIABILITY_CPU_CEILING_PERCENT,
            "memory_percent": settings.RELIABILITY_MEMORY_CEILING_PERCENT,
            "disk_percent": settings.RELIABILITY_DISK_CEILING_PERCENT,
            "queue_depth": settings.RELIABILITY_QUEUE_CEILING,
            "connection_pool": settings.RELIABILITY_CONNECTION_CEILING,
        },
    }
    supported_types = frozenset(
        {
            PredictionType.RESOURCE_EXHAUSTION_RISK,
            PredictionType.LATENCY_RISK,
            PredictionType.AVAILABILITY_RISK,
        }
    )

    def limitations(self, bundle: FeatureBundle, horizon: ForecastHorizon) -> list[str]:
        ceilings = self.parameters["ceilings"]
        return [
            "saturation is measured against the configured ceilings: "
            + ", ".join(
                f"{key}={value}" for key, value in ceilings.items() if value is not None
            ),
            "the trajectory is bounded and assumes the workload does not change",
        ]


class HistoricalFrequencyPredictor(_RuleSetPredictor):
    """Is a recurring event happening more often than its history says (§22)?

    Compares two equal-length windows (the last 7 days against the 7 before
    them) plus the recurring-pattern count. Two equal windows rather than a
    fitted rate, so the comparison is reproducible from stored rows and does
    not overfit a handful of events.
    """

    name = "historical_frequency"
    model_type = ReliabilityModelType.HISTORICAL_FREQUENCY
    version = "1"
    parameters = {
        "window_days": 7,
        "min_events_for_trend": 2,
        "min_ratio_change": settings.RELIABILITY_FREQUENCY_TREND_RATIO,
        "scales": {
            "incidents_7d": DEFAULT_SCALES["incidents_7d"],
            "frequency_trend": DEFAULT_SCALES["frequency_trend"],
            "recurring_incidents": DEFAULT_SCALES["recurring_incidents"],
        },
    }
    supported_types = frozenset(
        {
            PredictionType.INCIDENT_RISK,
            PredictionType.FAILURE_RISK,
            PredictionType.AVAILABILITY_RISK,
            PredictionType.RELIABILITY_DEGRADATION,
        }
    )

    def limitations(self, bundle: FeatureBundle, horizon: ForecastHorizon) -> list[str]:
        return [
            "historical frequency assumes past behaviour is a useful guide; a "
            "deliberate architecture or workload change invalidates it",
            "small counts produce unstable frequency estimates, so these "
            "signals are weighted lower than measured telemetry trends",
        ]


#: The deterministic predictors, in the order the registry prefers them (§19).
DETERMINISTIC_PREDICTORS: tuple[_RuleSetPredictor, ...] = (
    RollingTrendPredictor(),
    EWMAReliabilityPredictor(),
    ThresholdTrajectoryPredictor(),
    HistoricalFrequencyPredictor(),
)


def predictors_for(
    prediction_type: PredictionType,
) -> list["_RuleSetPredictor"]:
    """Every deterministic predictor that can score this prediction type."""
    return [p for p in DETERMINISTIC_PREDICTORS if p.supports(prediction_type)]


def projection_check(
    values: Sequence[Optional[float]],
    *,
    steps_ahead: int,
    ceiling: Optional[float],
) -> Optional[dict]:
    """Bounded projection helper used by explanations and tests.

    Returns the projected value, the ceiling it was compared against and
    whether it crossed — or ``None`` when there is not enough data to project.
    Exposed so a caller can show the same trajectory the predictor reasoned
    over instead of a hand-drawn arrow.
    """
    projected = stats.bounded_linear_projection(
        values,
        steps_ahead=steps_ahead,
        max_growth_ratio=settings.RELIABILITY_MAX_PROJECTION_GROWTH,
    )
    if projected is None:
        return None
    return {
        "projected": projected,
        "ceiling": ceiling,
        "crosses_ceiling": (ceiling is not None and projected >= ceiling),
        "steps_ahead": steps_ahead,
        "bounded_growth_ratio": settings.RELIABILITY_MAX_PROJECTION_GROWTH,
    }


__all__ = [
    "DEFAULT_SCALES",
    "DETERMINISTIC_PREDICTORS",
    "HEADLINES",
    "MIN_TREND_SAMPLES",
    "RULE_SETS",
    "EWMAReliabilityPredictor",
    "HistoricalFrequencyPredictor",
    "PredictionDraft",
    "ReliabilityPredictor",
    "RollingTrendPredictor",
    "Rule",
    "SignalDraft",
    "ThresholdTrajectoryPredictor",
    "clamp01",
    "intensity",
    "predictors_for",
    "projection_check",
    "severity_for",
]
