"""ARGUS Reproduction Comparator (Phase 5 §27–§30, §49).

Compares what the incident showed against what the sandbox showed, across eight
independent dimensions, and reports each one **with its formula and its inputs**.

Two rules drive the whole module:

**No unexplainable number (§29).** Every dimension returns a coarse bucket
alongside its score, a written formula, and the concrete inputs that produced it.
A reader who distrusts the score can recompute it by hand from the payload —
which is the only thing that makes a similarity figure worth showing an engineer.

**Absence is not failure.** A dimension whose inputs do not exist (no recovery
telemetry in the incident, no shared latencies) reports ``available: false`` and
is excluded from the overall score instead of contributing a zero. Scoring a
missing measurement as "no similarity" is how a comparison silently concludes
something it does not know.

The overall bucket is a weighted mean over *available* dimensions only, and the
``result`` (§30) is decided by the expectation match and the sequence — not by
the score alone, because a high similarity to a wrong sequence is still a
different failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from app.models.reproduction import ComparisonDimension, ReproductionResult
from app.services.reproduction_context import SourceBehavior, normalize_log_message
from app.services.reproduction_expectations import (
    ExpectationMatcher,
    ExpectedBehavior,
)
from app.services.telemetry_capture import CaptureResult, CapturedSignal

#: Weights for the overall mean. The failure *sequence* and the affected
#: *components* carry the most weight because they are the claim a reproduction is
#: actually making; recovery is weighted lowest because it is the most often
#: unobservable.
DIMENSION_WEIGHTS: dict[ComparisonDimension, float] = {
    ComparisonDimension.FAILURE_SEQUENCE: 2.0,
    ComparisonDimension.COMPONENT: 1.5,
    ComparisonDimension.ERROR: 1.5,
    ComparisonDimension.LATENCY: 1.0,
    ComparisonDimension.TRACE_TOPOLOGY: 1.0,
    ComparisonDimension.LOG_PATTERN: 1.0,
    ComparisonDimension.TEMPORAL: 1.0,
    ComparisonDimension.RECOVERY: 0.5,
}

#: Bucket thresholds for the weighted mean.
HIGH_THRESHOLD = 0.85
MEDIUM_THRESHOLD = 0.65
LOW_THRESHOLD = 0.40

FORMULA_REFERENCE = (
    "Component = |shared components| / |incident components|. "
    "Error = 1 - |original error rate - reproduced error rate| / max(both, 0.05). "
    "Latency = mean over shared components of min(original, reproduced) / "
    "max(original, reproduced). "
    "Trace topology = Jaccard of (parent, child) call pairs. "
    "Log pattern = Jaccard of normalized error-message templates. "
    "Failure sequence = longest common subsequence of the two component orders / "
    "longer length. "
    "Temporal = concordant pairs / all comparable pairs (Kendall-style) over the "
    "shared components' relative order. "
    "Recovery = |recovered in both| / |recovered in the incident|. "
    "Overall = weighted mean over dimensions with available inputs only "
    f"(weights: {', '.join(f'{k.value}={v}' for k, v in DIMENSION_WEIGHTS.items())}). "
    "A dimension with missing inputs reports available=false and is excluded — "
    "absence of evidence is never scored as dissimilarity."
)


@dataclass
class DimensionResult:
    """One dimension's score, bucket, formula and inputs."""

    dimension: ComparisonDimension
    available: bool
    score: Optional[float]
    formula: str
    inputs: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension.value,
            "available": self.available,
            "score": None if self.score is None else round(self.score, 4),
            "bucket": bucket_for(self.score) if self.available else None,
            "weight": DIMENSION_WEIGHTS.get(self.dimension, 1.0),
            "formula": self.formula,
            "inputs": self.inputs,
            "notes": self.notes,
        }


def bucket_for(score: Optional[float]) -> str:
    """Coarse bucket for a 0..1 similarity score (§29 — never a bare decimal)."""
    if score is None:
        return "INSUFFICIENT"
    if score >= HIGH_THRESHOLD:
        return "HIGH"
    if score >= MEDIUM_THRESHOLD:
        return "MEDIUM"
    if score >= LOW_THRESHOLD:
        return "LOW"
    return "INSUFFICIENT"


def _p95(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return float(ordered[index])


def _jaccard(left: set[Any], right: set[Any]) -> tuple[float, set[Any]]:
    """Jaccard similarity plus the shared elements (empty/empty counts as 1.0)."""
    union = left | right
    if not union:
        return 1.0, set()
    return len(left & right) / len(union), left & right


def _lcs_length(left: Sequence[str], right: Sequence[str]) -> int:
    """Longest common subsequence length — ordering-sensitive, tolerant of extra."""
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    for item_left in left:
        current = [0]
        for index, item_right in enumerate(right, start=1):
            if item_left == item_right:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[index - 1]))
        previous = current
    return previous[-1]


@dataclass
class ComparisonResult:
    """The complete, explainable comparison of one run against the incident."""

    result: ReproductionResult
    overall_similarity: str
    similarity_score: Optional[float]
    dimensions: dict[str, dict[str, Any]] = field(default_factory=dict)
    component_overlap: dict[str, Any] = field(default_factory=dict)
    matched_components: list[str] = field(default_factory=list)
    missing_components: list[str] = field(default_factory=list)
    extra_components: list[str] = field(default_factory=list)
    sequence_original: list[str] = field(default_factory=list)
    sequence_reproduced: list[str] = field(default_factory=list)
    sequence_match: bool = False
    metric_deltas: dict[str, Any] = field(default_factory=dict)
    error_comparison: dict[str, Any] = field(default_factory=dict)
    trace_topology: dict[str, Any] = field(default_factory=dict)
    log_pattern: dict[str, Any] = field(default_factory=dict)
    recovery: dict[str, Any] = field(default_factory=dict)
    temporal: dict[str, Any] = field(default_factory=dict)
    original_summary: dict[str, Any] = field(default_factory=dict)
    reproduced_summary: dict[str, Any] = field(default_factory=dict)
    explanation: str = ""
    #: Expectation accounting, consumed by the hypothesis validator (§31).
    expectations_total: int = 0
    expectations_matched: int = 0
    expectations_missing: list[dict[str, Any]] = field(default_factory=list)
    formula_reference: str = FORMULA_REFERENCE


class ReproductionComparator:
    """Computes the eight-dimension comparison."""

    def compare(
        self,
        *,
        source: SourceBehavior,
        capture: CaptureResult,
        expected: Optional[ExpectedBehavior] = None,
        faults: Sequence[dict[str, Any]] = (),
        alias_map: Optional[Mapping[str, str]] = None,
    ) -> ComparisonResult:
        # The incident names components as ARGUS knows them; the sandbox names its
        # own services. One side must be translated before anything can match, and
        # it is the source side because every expectation is already stated in
        # sandbox service names.
        if alias_map:
            source = source.relabeled(alias_map)
        behavior = expected or ExpectedBehavior()
        matcher = ExpectationMatcher(behavior)
        produced = _ReproducedView(capture)

        dimensions = {
            ComparisonDimension.COMPONENT: self._component_dimension(source, produced),
            ComparisonDimension.ERROR: self._error_dimension(source, produced),
            ComparisonDimension.LATENCY: self._latency_dimension(source, produced),
            ComparisonDimension.TRACE_TOPOLOGY: self._topology_dimension(
                source, produced
            ),
            ComparisonDimension.LOG_PATTERN: self._log_dimension(source, produced),
            ComparisonDimension.FAILURE_SEQUENCE: self._sequence_dimension(
                source, produced, behavior
            ),
            ComparisonDimension.RECOVERY: self._recovery_dimension(source, produced),
            ComparisonDimension.TEMPORAL: self._temporal_dimension(source, produced),
        }

        score, bucket = self._aggregate(dimensions)
        missing = [
            expectation.as_dict() for expectation in matcher.missing(capture.signals)
        ]
        total_expectations = len(behavior.signals)
        matched_expectations = max(0, total_expectations - len(missing))

        result = self._decide_result(
            source=source,
            produced=produced,
            behavior=behavior,
            bucket=bucket,
            matched_expectations=matched_expectations,
            total_expectations=total_expectations,
        )

        comparison = ComparisonResult(
            result=result,
            overall_similarity=bucket,
            similarity_score=score,
            dimensions={
                key.value: value.as_dict() for key, value in dimensions.items()
            },
            expectations_total=total_expectations,
            expectations_matched=matched_expectations,
            expectations_missing=missing,
            matched_components=dimensions[ComparisonDimension.COMPONENT].inputs.get(
                "matched", []
            ),
            missing_components=dimensions[ComparisonDimension.COMPONENT].inputs.get(
                "missing", []
            ),
            extra_components=dimensions[ComparisonDimension.COMPONENT].inputs.get(
                "extra", []
            ),
            sequence_original=list(source.sequence),
            sequence_reproduced=list(produced.onset_order),
            sequence_match=bool(
                dimensions[ComparisonDimension.FAILURE_SEQUENCE].inputs.get(
                    "exact_match"
                )
            ),
            metric_deltas=dimensions[ComparisonDimension.LATENCY].inputs.get(
                "deltas", {}
            ),
            error_comparison=dimensions[ComparisonDimension.ERROR].inputs,
            trace_topology=dimensions[ComparisonDimension.TRACE_TOPOLOGY].inputs,
            log_pattern=dimensions[ComparisonDimension.LOG_PATTERN].inputs,
            recovery=dimensions[ComparisonDimension.RECOVERY].inputs,
            temporal=dimensions[ComparisonDimension.TEMPORAL].inputs,
            component_overlap=dimensions[ComparisonDimension.COMPONENT].as_dict(),
            original_summary=source.as_dict(),
            reproduced_summary=produced.as_dict(),
        )
        comparison.explanation = self._explain(
            comparison, dimensions, produced=produced, faults=faults
        )
        return comparison

    # -- dimensions ------------------------------------------------------
    def _component_dimension(
        self, source: SourceBehavior, produced: "_ReproducedView"
    ) -> DimensionResult:
        expected_set = set(source.components)
        produced_set = set(produced.components)
        matched = sorted(expected_set & produced_set)
        missing = sorted(expected_set - produced_set)
        extra = sorted(produced_set - expected_set)
        formula = "|shared| / |incident components|"
        if not expected_set:
            return DimensionResult(
                dimension=ComparisonDimension.COMPONENT,
                available=False,
                score=None,
                formula=formula,
                inputs={"matched": matched, "missing": missing, "extra": extra},
                notes=[
                    "The incident identifies no affected components, so component "
                    "overlap cannot be measured"
                ],
            )
        score = len(matched) / len(expected_set)
        return DimensionResult(
            dimension=ComparisonDimension.COMPONENT,
            available=True,
            score=score,
            formula=formula,
            inputs={
                "incident_components": sorted(expected_set),
                "reproduced_components": sorted(produced_set),
                "matched": matched,
                "missing": missing,
                "extra": extra,
                "matched_count": len(matched),
                "expected_count": len(expected_set),
            },
        )

    def _error_dimension(
        self, source: SourceBehavior, produced: "_ReproducedView"
    ) -> DimensionResult:
        formula = "1 - |original_rate - reproduced_rate| / max(original_rate, reproduced_rate, 0.05)"
        original_rate = float(source.error_rate)
        reproduced_rate = produced.error_rate
        if source.span_count == 0 and produced.error_signal_count == 0:
            return DimensionResult(
                dimension=ComparisonDimension.ERROR,
                available=False,
                score=None,
                formula=formula,
                inputs={
                    "original_error_rate": original_rate,
                    "reproduced_error_rate": reproduced_rate,
                },
                notes=["Neither the incident nor the sandbox recorded failing spans"],
            )
        denominator = max(original_rate, reproduced_rate, 0.05)
        score = max(0.0, 1.0 - abs(original_rate - reproduced_rate) / denominator)
        return DimensionResult(
            dimension=ComparisonDimension.ERROR,
            available=True,
            score=score,
            formula=formula,
            inputs={
                "original_error_rate": round(original_rate, 4),
                "reproduced_error_rate": round(reproduced_rate, 4),
                "original_error_spans": source.error_span_count,
                "original_spans": source.span_count,
                "reproduced_error_signals": produced.error_signal_count,
                "reproduced_signals": produced.signal_count,
            },
        )

    def _latency_dimension(
        self, source: SourceBehavior, produced: "_ReproducedView"
    ) -> DimensionResult:
        formula = "mean over shared components of min(original, reproduced) / max(original, reproduced)"
        shared = sorted(set(source.latency_ms) & set(produced.latency_ms))
        if not shared:
            return DimensionResult(
                dimension=ComparisonDimension.LATENCY,
                available=False,
                score=None,
                formula=formula,
                inputs={
                    "original_latency_ms": dict(source.latency_ms),
                    "reproduced_latency_ms": dict(produced.latency_ms),
                },
                notes=[
                    "No component has a latency measurement on both sides, so the "
                    "latency profiles cannot be compared"
                ],
            )
        ratios: list[float] = []
        deltas: dict[str, Any] = {}
        for component in shared:
            original = float(source.latency_ms[component])
            reproduced = float(produced.latency_ms[component])
            highest = max(original, reproduced)
            if highest <= 0:
                continue
            ratios.append(min(original, reproduced) / highest)
            deltas[component] = {
                "original_ms": round(original, 2),
                "reproduced_ms": round(reproduced, 2),
                "delta_pct": round((reproduced - original) / original * 100, 2)
                if original
                else None,
            }
        if not ratios:
            return DimensionResult(
                dimension=ComparisonDimension.LATENCY,
                available=False,
                score=None,
                formula=formula,
                inputs={"deltas": deltas},
                notes=["Shared components had no positive latency to compare"],
            )
        return DimensionResult(
            dimension=ComparisonDimension.LATENCY,
            available=True,
            score=sum(ratios) / len(ratios),
            formula=formula,
            inputs={
                "shared_components": shared,
                "ratios": [round(ratio, 4) for ratio in ratios],
                "deltas": deltas,
                "original_latency_ms": dict(source.latency_ms),
                "reproduced_latency_ms": dict(produced.latency_ms),
            },
        )

    def _topology_dimension(
        self, source: SourceBehavior, produced: "_ReproducedView"
    ) -> DimensionResult:
        formula = "Jaccard of (parent, child) call pairs"
        original = {
            (str(edge.get("parent")), str(edge.get("child")))
            for edge in source.trace_edges
            if edge.get("parent") and edge.get("child")
        }
        reproduced = set(produced.trace_edges)
        if not original and not reproduced:
            return DimensionResult(
                dimension=ComparisonDimension.TRACE_TOPOLOGY,
                available=False,
                score=None,
                formula=formula,
                inputs={},
                notes=["Neither side produced a parent/child call relationship"],
            )
        score, shared = _jaccard(original, reproduced)
        return DimensionResult(
            dimension=ComparisonDimension.TRACE_TOPOLOGY,
            available=True,
            score=score,
            formula=formula,
            inputs={
                "original_edges": sorted(f"{a}->{b}" for a, b in original),
                "reproduced_edges": sorted(f"{a}->{b}" for a, b in reproduced),
                "shared_edges": sorted(f"{a}->{b}" for a, b in shared),
                "shared_count": len(shared),
                "union_count": len(original | reproduced),
            },
        )

    def _log_dimension(
        self, source: SourceBehavior, produced: "_ReproducedView"
    ) -> DimensionResult:
        formula = "Jaccard of normalized error-message templates"
        original = {
            pattern for patterns in source.log_patterns.values() for pattern in patterns
        }
        reproduced = set(produced.log_patterns)
        if not original and not reproduced:
            return DimensionResult(
                dimension=ComparisonDimension.LOG_PATTERN,
                available=False,
                score=None,
                formula=formula,
                inputs={},
                notes=["Neither side recorded an error-level log message"],
            )
        score, shared = _jaccard(original, reproduced)
        return DimensionResult(
            dimension=ComparisonDimension.LOG_PATTERN,
            available=True,
            score=score,
            formula=formula,
            inputs={
                "original_patterns": sorted(original),
                "reproduced_patterns": sorted(reproduced),
                "shared_patterns": sorted(shared),
                "shared_count": len(shared),
            },
        )

    def _sequence_dimension(
        self,
        source: SourceBehavior,
        produced: "_ReproducedView",
        behavior: ExpectedBehavior,
    ) -> DimensionResult:
        formula = "longest common subsequence / longer length"
        original = list(source.sequence)
        reproduced = list(produced.onset_order)
        expected_sequence = list(behavior.sequence)
        if not original and not reproduced:
            return DimensionResult(
                dimension=ComparisonDimension.FAILURE_SEQUENCE,
                available=False,
                score=None,
                formula=formula,
                inputs={},
                notes=["No component ordering could be derived from either side"],
            )
        longest = max(len(original), len(reproduced), 1)
        score = _lcs_length(original, reproduced) / longest
        exact = bool(original) and original == reproduced
        return DimensionResult(
            dimension=ComparisonDimension.FAILURE_SEQUENCE,
            available=True,
            score=score,
            formula=formula,
            inputs={
                "original_sequence": original,
                "reproduced_sequence": reproduced,
                "planned_sequence": expected_sequence,
                "lcs_length": _lcs_length(original, reproduced),
                "exact_match": exact,
            },
            notes=[] if exact else ["The reproduced order differs from the incident's"],
        )

    def _recovery_dimension(
        self, source: SourceBehavior, produced: "_ReproducedView"
    ) -> DimensionResult:
        formula = "|recovered in both| / |recovered in the incident|"
        if not source.recovery_order:
            return DimensionResult(
                dimension=ComparisonDimension.RECOVERY,
                available=False,
                score=None,
                formula=formula,
                inputs={
                    "incident_recovered": [],
                    "reproduced_recovered": sorted(produced.recovered),
                },
                notes=[
                    "The incident has no health-recovery telemetry, so recovery "
                    "alignment cannot be scored"
                ],
            )
        incident_recovered = set(source.recovery_order)
        reproduced_recovered = set(produced.recovered)
        shared = sorted(incident_recovered & reproduced_recovered)
        return DimensionResult(
            dimension=ComparisonDimension.RECOVERY,
            available=True,
            score=len(shared) / len(incident_recovered),
            formula=formula,
            inputs={
                "incident_recovered": sorted(incident_recovered),
                "reproduced_recovered": sorted(reproduced_recovered),
                "shared": shared,
            },
        )

    def _temporal_dimension(
        self, source: SourceBehavior, produced: "_ReproducedView"
    ) -> DimensionResult:
        formula = "concordant pairs / comparable pairs over shared components"
        original_order = [c for c in source.sequence if c in set(produced.onset_order)]
        reproduced_order = [
            c for c in produced.onset_order if c in set(source.sequence)
        ]
        shared = set(original_order) & set(reproduced_order)
        if len(shared) < 2:
            return DimensionResult(
                dimension=ComparisonDimension.TEMPORAL,
                available=False,
                score=None,
                formula=formula,
                inputs={
                    "original_order": original_order,
                    "reproduced_order": reproduced_order,
                },
                notes=[
                    "Fewer than two shared components, so no relative timing can be "
                    "compared"
                ],
            )
        index_original = {name: i for i, name in enumerate(original_order)}
        index_reproduced = {name: i for i, name in enumerate(reproduced_order)}
        concordant = 0
        discordant = 0
        names = sorted(shared)
        for i, first in enumerate(names):
            for second in names[i + 1 :]:
                left = index_original[first] - index_original[second]
                right = index_reproduced[first] - index_reproduced[second]
                if left * right >= 0:
                    concordant += 1
                else:
                    discordant += 1
        total = concordant + discordant
        score = concordant / total if total else None
        return DimensionResult(
            dimension=ComparisonDimension.TEMPORAL,
            available=score is not None,
            score=score,
            formula=formula,
            inputs={
                "original_order": original_order,
                "reproduced_order": reproduced_order,
                "concordant_pairs": concordant,
                "discordant_pairs": discordant,
            },
        )

    # -- aggregation -----------------------------------------------------
    @staticmethod
    def _aggregate(
        dimensions: dict[ComparisonDimension, DimensionResult],
    ) -> tuple[Optional[float], str]:
        weighted = 0.0
        total_weight = 0.0
        for dimension, result in dimensions.items():
            if not result.available or result.score is None:
                continue
            weight = DIMENSION_WEIGHTS.get(dimension, 1.0)
            weighted += result.score * weight
            total_weight += weight
        if total_weight == 0:
            return None, "INSUFFICIENT"
        score = weighted / total_weight
        return score, bucket_for(score)

    @staticmethod
    def _decide_result(
        *,
        source: SourceBehavior,
        produced: "_ReproducedView",
        behavior: ExpectedBehavior,
        bucket: str,
        matched_expectations: int,
        total_expectations: int,
    ) -> ReproductionResult:
        """Decide what the sandbox demonstrated (§30).

        Order matters: "nothing was observed" outranks every other reading,
        because a sandbox that produced no telemetry cannot have failed to
        reproduce anything — it failed to run a comparable experiment.
        """
        if produced.signal_count == 0 and not produced.transport_failures:
            return ReproductionResult.INCONCLUSIVE
        if total_expectations == 0 and not produced.error_signal_count:
            # Nothing was expected and nothing failed: there is no experiment
            # here, only a sandbox that ran cleanly.
            return ReproductionResult.INCONCLUSIVE
        if produced.error_signal_count == 0 and matched_expectations == 0:
            return ReproductionResult.FAILED
        sequence_ok = bool(produced.onset_order) and (
            not source.sequence or produced.onset_order == source.sequence
        )
        if (
            matched_expectations == total_expectations
            and total_expectations > 0
            and sequence_ok
            and bucket in {"MEDIUM", "HIGH"}
        ):
            return ReproductionResult.SUCCESSFUL
        if matched_expectations > 0:
            return ReproductionResult.PARTIAL
        # Failures were observed but they are not the ones the incident showed.
        return (
            ReproductionResult.PARTIAL
            if produced.error_signal_count
            else ReproductionResult.FAILED
        )

    # -- narrative -------------------------------------------------------
    @staticmethod
    def _explain(
        comparison: ComparisonResult,
        dimensions: dict[ComparisonDimension, DimensionResult],
        *,
        produced: "_ReproducedView",
        faults: Sequence[dict[str, Any]],
    ) -> str:
        parts: list[str] = []
        component = dimensions[ComparisonDimension.COMPONENT]
        if component.available:
            parts.append(
                f"{component.inputs.get('matched_count')} of "
                f"{component.inputs.get('expected_count')} incident components also "
                f"failed in the sandbox"
                + (
                    f" (missing: {', '.join(component.inputs.get('missing', []))})"
                    if component.inputs.get("missing")
                    else ""
                )
            )
        sequence = dimensions[ComparisonDimension.FAILURE_SEQUENCE]
        if sequence.available:
            parts.append(
                "the reproduced order matched the incident's"
                if sequence.inputs.get("exact_match")
                else "the reproduced order differed from the incident's"
            )
        error = dimensions[ComparisonDimension.ERROR]
        if error.available:
            parts.append(
                f"the error rate was {error.inputs.get('reproduced_error_rate')} "
                f"against {error.inputs.get('original_error_rate')} in the incident"
            )
        latency = dimensions[ComparisonDimension.LATENCY]
        if latency.available:
            parts.append(
                f"latency similarity was "
                f"{bucket_for(latency.score).lower()} across "
                f"{len(latency.inputs.get('shared_components', []))} shared components"
            )
        if comparison.expectations_missing:
            parts.append(
                f"{len(comparison.expectations_missing)} expected signal(s) never "
                "appeared in the sandbox"
            )
        if produced.transport_failures:
            parts.append(
                f"{produced.transport_failures} replay(s) failed at the transport "
                "layer, which may be the sandbox rather than the system failing"
            )
        if faults:
            injected = [item for item in faults if item.get("injected")]
            if injected:
                parts.append(
                    f"{len(injected)} fault(s) were injected, so the observed failure "
                    "was induced rather than emergent"
                )
        summary = (
            f"Reproduction {comparison.result.value}: "
            + "; ".join(parts)
            + f". Overall similarity is {comparison.overall_similarity}."
        )
        return summary


class _ReproducedView:
    """Convenience view over captured signals, so dimensions read cleanly."""

    def __init__(self, capture: CaptureResult) -> None:
        self._capture = capture
        self.signals: list[CapturedSignal] = list(capture.signals)
        self.components = sorted(
            {item.component_name for item in self.signals if item.component_name}
        )
        self.signal_count = len(self.signals)
        self.error_signals = [item for item in self.signals if item.error]
        self.error_signal_count = len(self.error_signals)
        self.transport_failures = sum(
            1 for item in self.signals if (item.attributes or {}).get("transport_error")
        )

        self.error_rate = (
            self.error_signal_count / self.signal_count if self.signal_count else 0.0
        )

        per_component: dict[str, list[float]] = {}
        for item in self.signals:
            if item.duration_ms is None or not item.component_name:
                continue
            per_component.setdefault(item.component_name, []).append(
                float(item.duration_ms)
            )
        self.latency_ms: dict[str, float] = {}
        for component, values in per_component.items():
            p95 = _p95(values)
            if p95 is not None:
                self.latency_ms[component] = p95

        self.trace_edges: set[tuple[str, str]] = set()
        span_by_id: dict[str, CapturedSignal] = {}
        for item in self.signals:
            if item.signal_type.value == "SPAN" and item.span_id:
                span_by_id[item.span_id] = item
        for item in self.signals:
            if not item.parent_span_id or not item.component_name:
                continue
            parent = span_by_id.get(item.parent_span_id)
            parent_name = parent.component_name if parent is not None else None
            if parent_name and parent_name != item.component_name:
                self.trace_edges.add((parent_name, item.component_name))

        self.log_patterns = sorted(
            {
                normalize_log_message(item.message or "")
                for item in self.signals
                if item.error and item.message
            }
            - {""}
        )

        # Onset order: the first moment each component showed an error.
        first_error: dict[str, int] = {}
        for item in self.error_signals:
            if not item.component_name:
                continue
            if item.component_name not in first_error:
                first_error[item.component_name] = item.relative_offset_ms
        self.onset_order = [
            name for name, _ in sorted(first_error.items(), key=lambda pair: pair[1])
        ]

        # Recovery: components whose last health observation is a healthy state
        # after having failed earlier in the run.
        last_state: dict[str, str] = {}
        for item in self.signals:
            if item.signal_type.value != "HEALTH" or not item.component_name:
                continue
            last_state[item.component_name] = str(
                (item.attributes or {}).get("state") or "UP"
            ).upper()
        self.recovered = sorted(
            name
            for name, state in last_state.items()
            if state == "UP" and name in first_error
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "components": self.components,
            "signal_count": self.signal_count,
            "error_signal_count": self.error_signal_count,
            "error_rate": round(self.error_rate, 4),
            "latency_ms": {k: round(v, 2) for k, v in self.latency_ms.items()},
            "onset_order": self.onset_order,
            "recovered": self.recovered,
            "trace_edges": sorted(f"{a}->{b}" for a, b in self.trace_edges),
            "log_patterns": self.log_patterns,
            "transport_failures": self.transport_failures,
            "namespace": self._capture.namespace,
            "missing_expectations": self._capture.missing_count,
        }


__all__ = [
    "DIMENSION_WEIGHTS",
    "FORMULA_REFERENCE",
    "HIGH_THRESHOLD",
    "LOW_THRESHOLD",
    "MEDIUM_THRESHOLD",
    "ComparisonResult",
    "DimensionResult",
    "ReproductionComparator",
    "bucket_for",
]
