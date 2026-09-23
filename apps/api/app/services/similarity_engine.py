"""ARGUS Reliability Similarity Engine (Phase 10 §11, §12).

Compares a current situation with a stored :class:`FailureSignature` and returns
a **score together with its reasons**. The phase is explicit that returning
``similarity = 0.91`` with no explanation is not acceptable, so this module has
no API that produces a bare number: :meth:`ReliabilitySimilarityEngine.compare`
always returns a :class:`SimilarityExplanation` carrying the matched features,
the features that disagreed, and the same-context facts.

How the score is built:

* Each feature class (anomaly, metric behaviour, dependency, deployment,
  resource, log, trace, component) has a weight. A shared latency spike is not
  the same evidence as a shared component id, and the weights make that
  difference explicit rather than incidental.
* Within a class the score is the Jaccard overlap of the two token sets, so
  having *extra* features on one side lowers the score instead of being free.
* Classes present on neither side are excluded from the denominator. An incident
  with no deployment context is not penalised for lacking one — otherwise every
  old incident would resemble every other old incident.
* Context (same component, same environment, same incident kind) contributes as
  named bonuses, and each is reported as a separate matched fact.

Nothing here claims causality. Two signatures matching means two situations
looked alike; §12 of the phase says so in the module the recommendation engine
reads from, so the wording travels with the number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from app.services.learning_signatures import (
    FailureSignature,
    ResolutionSignature,
    SignaturePair,
)

#: Feature class → weight. Components and metric behaviour dominate because they
#: are what an engineer would use to decide "is this the same problem?".
DEFAULT_WEIGHTS: dict[str, float] = {
    "component": 1.6,
    "metric": 1.4,
    "anomaly": 1.2,
    "dependency": 1.0,
    "incident_kind": 0.9,
    "deployment": 0.6,
    "resource": 0.6,
    "log": 0.5,
    "trace": 0.5,
}

#: Bonus applied when the two situations share the named context. Kept smaller
#: than any feature class: context refines a match, it does not create one.
CONTEXT_BONUSES: dict[str, float] = {
    "same_component": 0.15,
    "same_environment": 0.05,
    "same_incident_kind": 0.10,
}

#: Classes whose values are identity-like rather than behavioural. Disagreeing
#: on identity is not evidence *against* similarity, so it is reported but has
#: no negative weight beyond the shared/union ratio.
_IDENTITY_CLASSES = frozenset({"component"})


@dataclass(frozen=True)
class FeatureMatch:
    """One feature that matched (or did not) between two signatures."""

    feature_class: str
    value: str
    weight: float

    def as_dict(self) -> dict[str, object]:
        return {
            "feature_class": self.feature_class,
            "value": self.value,
            "weight": self.weight,
        }


@dataclass(frozen=True)
class SimilarityExplanation:
    """A similarity score with the reasoning that produced it (§12)."""

    score: float
    matched: tuple[FeatureMatch, ...]
    left_only: tuple[FeatureMatch, ...]
    right_only: tuple[FeatureMatch, ...]
    context: tuple[str, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "score": round(self.score, 4),
            "matched": [m.as_dict() for m in self.matched],
            "left_only": [m.as_dict() for m in self.left_only],
            "right_only": [m.as_dict() for m in self.right_only],
            "context": list(self.context),
            "reasons": list(self.reasons),
        }

    def summary(self) -> str:
        """A one-line explanation for lists and logs."""
        if not self.matched:
            return "no shared features were found"
        return "; ".join(self.reasons) if self.reasons else "shared features only"


@dataclass(frozen=True)
class RankedExperience:
    """A historical experience and why it matched (§13)."""

    experience: SignaturePair
    explanation: SimilarityExplanation
    similarity: float

    @property
    def experience_id(self) -> Optional[str]:
        return self.experience.experience_id

    @property
    def resolution(self) -> Optional[ResolutionSignature]:
        return self.experience.resolution


def _features(signature: FailureSignature) -> dict[str, set[str]]:
    """Split a signature into per-class token sets."""
    return {
        "component": set(signature.affected_components),
        "anomaly": set(signature.anomaly_types),
        "metric": set(signature.metric_behaviors),
        "dependency": set(signature.dependency_conditions),
        "deployment": set(signature.deployment_context),
        "resource": set(signature.resource_pressure),
        "log": set(signature.log_patterns),
        "trace": set(signature.trace_patterns),
        "incident_kind": (
            {signature.incident_kind} if signature.incident_kind != "unknown" else set()
        ),
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


class ReliabilitySimilarityEngine:
    """Explainable, weight-driven comparison of failure signatures."""

    def __init__(self, *, weights: Optional[dict[str, float]] = None) -> None:
        self.weights = dict(DEFAULT_WEIGHTS)
        if weights:
            self.weights.update(weights)

    def compare(
        self,
        current: FailureSignature,
        historical: FailureSignature,
        *,
        same_component: bool = False,
        same_environment: bool = False,
        same_incident_kind: Optional[bool] = None,
    ) -> SimilarityExplanation:
        """Score one pair, returning every feature that decided the score.

        ``same_component``/``same_environment`` are passed by the caller because
        the signature deliberately does not carry environment identity (it would
        leak scope into a comparison that may span environments, §36).
        """
        left = _features(current)
        right = _features(historical)

        matched: list[FeatureMatch] = []
        left_only: list[FeatureMatch] = []
        right_only: list[FeatureMatch] = []
        weighted_sum = 0.0
        weight_total = 0.0

        for feature_class in self.weights:
            left_values = left.get(feature_class, set())
            right_values = right.get(feature_class, set())
            union = left_values | right_values
            if not union:
                #: Absent on both sides: not evidence either way (§11).
                continue
            weight = self.weights[feature_class]
            weight_total += weight
            weighted_sum += weight * _jaccard(left_values, right_values)

            for value in sorted(left_values & right_values):
                matched.append(FeatureMatch(feature_class, value, weight))
            for value in sorted(left_values - right_values):
                left_only.append(FeatureMatch(feature_class, value, weight))
            for value in sorted(right_values - left_values):
                right_only.append(FeatureMatch(feature_class, value, weight))

        base = weighted_sum / weight_total if weight_total else 0.0

        context: list[str] = []
        bonus = 0.0
        if same_component:
            context.append("same_component")
            bonus += CONTEXT_BONUSES["same_component"]
        if same_environment:
            context.append("same_environment")
            bonus += CONTEXT_BONUSES["same_environment"]
        share_kind = (
            same_incident_kind
            if same_incident_kind is not None
            else (
                current.incident_kind != "unknown"
                and current.incident_kind == historical.incident_kind
            )
        )
        if share_kind:
            context.append("same_incident_kind")
            bonus += CONTEXT_BONUSES["same_incident_kind"]

        score = min(1.0, base + bonus)
        return SimilarityExplanation(
            score=score,
            matched=tuple(matched),
            left_only=tuple(left_only),
            right_only=tuple(right_only),
            context=tuple(context),
            reasons=self._reasons(
                current, historical, matched, left_only, right_only, context
            ),
        )

    def rank(
        self,
        current: FailureSignature,
        candidates: Iterable[SignaturePair],
        *,
        threshold: float = 0.0,
        limit: Optional[int] = None,
        component_id: Optional[str] = None,
        environment_id: Optional[str] = None,
    ) -> list[RankedExperience]:
        """Score and order historical experiences against a current situation.

        Ordering is by score, then by recency, then by experience id: the last
        key makes the order total, so two identical scores cannot produce a
        result that differs between runs (which would make retrieval
        unreproducible in tests and in the UI).
        """
        ranked: list[RankedExperience] = []
        for candidate in candidates:
            explanation = self.compare(
                current,
                candidate.failure,
                same_component=(
                    component_id is not None and candidate.component_id == component_id
                ),
                same_environment=(
                    environment_id is not None
                    and candidate.metadata.get("environment_id") == environment_id
                ),
            )
            if explanation.score < threshold:
                continue
            ranked.append(
                RankedExperience(
                    experience=candidate,
                    explanation=explanation,
                    similarity=explanation.score,
                )
            )

        ranked.sort(
            key=lambda item: (
                -item.similarity,
                -(
                    item.experience.occurred_at.timestamp()
                    if item.experience.occurred_at
                    else 0.0
                ),
                item.experience.experience_id or "",
            )
        )
        return ranked[:limit] if limit is not None else ranked

    # -- explanation ------------------------------------------------------
    def _reasons(
        self,
        current: FailureSignature,
        historical: FailureSignature,
        matched: Sequence[FeatureMatch],
        left_only: Sequence[FeatureMatch],
        right_only: Sequence[FeatureMatch],
        context: Sequence[str],
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        by_class: dict[str, list[str]] = {}
        for match in matched:
            by_class.setdefault(match.feature_class, []).append(match.value)
        for feature_class, values in by_class.items():
            reasons.append(f"shared {feature_class}: {', '.join(values)}")

        for text in context:
            reasons.append(text.replace("_", " "))

        for feature_class in ("metric", "anomaly", "dependency"):
            differing = [
                f"{m.value} (only in the current situation)"
                for m in left_only
                if m.feature_class == feature_class
            ] + [
                f"{m.value} (only in the historical case)"
                for m in right_only
                if m.feature_class == feature_class
            ]
            if differing:
                reasons.append(f"{feature_class} differs: {', '.join(differing)}")

        if not reasons:
            reasons.append("no shared features were found")
        return tuple(reasons)


__all__ = [
    "CONTEXT_BONUSES",
    "DEFAULT_WEIGHTS",
    "FeatureMatch",
    "RankedExperience",
    "ReliabilitySimilarityEngine",
    "SimilarityExplanation",
]
