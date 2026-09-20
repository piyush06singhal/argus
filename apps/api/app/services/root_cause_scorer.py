"""ARGUS Root Cause Scorer (Phase 4 §25–§29, §31).

A **deterministic, documented** scoring framework. Design rules:

* ``score`` and ``confidence`` are separate concepts (§26) — a candidate can
  rank first among weak candidates while the honest confidence bucket stays
  ``LOW`` or ``INSUFFICIENT``. The scorer computes both and never fuses them.
* No fake precision: scores are relative within one analysis, and the
  breakdown of every component is stored next to the score (§37) so the UI
  can show *why* a number is what it is.
* ``INSUFFICIENT`` is a first-class outcome (§29): contradictory or too-sparse
  evidence yields UNKNOWN selection, never a forced answer.
* No clock, no randomness, no network: identical inputs → identical outputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.models.causal import ConfidenceLevel
from app.services.causal_graph import CausalGraphSpec, HypothesisNode

# ---------------------------------------------------------------------------
# Documented scoring weights (§25). Every value here is deliberate:
# trace evidence is the strongest directional source Phase 1 provides;
# temporal alignment is supporting context, never proof (§11: before ≠ caused);
# recovery ordering adds directional support (§31); contradictions subtract.
# ---------------------------------------------------------------------------
W_TRACE = 0.30
W_TEMPORAL = 0.20
W_DEPENDENCY = 0.12
W_CHANGE = 0.12
W_PROPAGATION = 0.16
W_RECOVERY = 0.10
W_RESOURCE = 0.08
#: Each contradicting item subtracts; the penalty is bounded so one bad fact
#: cannot drag a well-evidenced candidate to zero (honesty, not erasure).
W_CONTRADICTION_PENALTY = 0.15
MAX_CONTRADICTION_PENALTY = 0.45

#: Multi-source requirement for HIGH (§27): at least this many *independent
#: categories* must support the candidate.
HIGH_MIN_CATEGORIES = 3
#: Trace (directional) evidence required for HIGH.
HIGH_REQUIRES_TRACE = True
MEDIUM_MIN_CATEGORIES = 2
#: Below this total support the candidate cannot even be ranked as LOW.
#: Calibrated to the weights below: one strong temporal fact (0.9 × W_TEMPORAL)
#: contributes ≈0.13, and §27 defines "only temporal/structural evidence" as
#: LOW — so the floor must sit *below* that, not above it.
LOW_MIN_SUPPORT = 0.10
#: Aggregate support below which the candidate is INSUFFICIENT to rank. A weak
#: single fact (strength 0.4) lands under this and is reported as too sparse.
INSUFFICIENT_SUPPORT = 0.08

#: Contradictions are weighed against the candidate's own support. A candidate
#: more than half contradicted cannot outrank an uncontradicted one, and no
#: amount of corroboration makes a half-refuted hypothesis "HIGH" (§12, §27).
CONTRADICTION_DOMINANT_RATIO = 0.5
CONTRADICTION_MATERIAL_RATIO = 0.25

#: Ordering used to apply the contradiction cap (higher = stronger claim).
_CONFIDENCE_ORDER: dict[ConfidenceLevel, int] = {
    ConfidenceLevel.INSUFFICIENT: 0,
    ConfidenceLevel.LOW: 1,
    ConfidenceLevel.MEDIUM: 2,
    ConfidenceLevel.HIGH: 3,
}


@dataclass
class ScoreBreakdown:
    """Every score component and its value — stored on the candidate (§25)."""

    trace: float = 0.0
    temporal: float = 0.0
    dependency: float = 0.0
    change: float = 0.0
    propagation: float = 0.0
    recovery: float = 0.0
    resource: float = 0.0
    contradiction_penalty: float = 0.0
    outgoing_edge_support: float = 0.0

    @property
    def total(self) -> float:
        raw = (
            self.trace
            + self.temporal
            + self.dependency
            + self.change
            + self.propagation
            + self.recovery
            + self.resource
            + self.outgoing_edge_support
            - self.contradiction_penalty
        )
        return max(0.0, min(1.0, raw))

    def as_dict(self) -> dict[str, float]:
        return {
            "trace": round(self.trace, 4),
            "temporal": round(self.temporal, 4),
            "dependency": round(self.dependency, 4),
            "change": round(self.change, 4),
            "propagation": round(self.propagation, 4),
            "recovery": round(self.recovery, 4),
            "resource": round(self.resource, 4),
            "outgoing_edge_support": round(self.outgoing_edge_support, 4),
            "contradiction_penalty": round(self.contradiction_penalty, 4),
            "total": round(self.total, 4),
        }


@dataclass
class ScoredCandidate:
    """A candidate with its score, breakdown, and honest confidence bucket."""

    key: tuple
    label: str
    score: float
    breakdown: ScoreBreakdown
    confidence: ConfidenceLevel
    confidence_reason: str
    supporting_categories: set[str] = field(default_factory=set)
    supporting_count: int = 0
    contradicting_count: int = 0
    reasons: list[str] = field(default_factory=list)
    uncertainty: dict = field(default_factory=dict)


class RootCauseScorer:
    """Scores candidates and assigns confidence per the documented criteria."""

    def score(
        self,
        *,
        graph: CausalGraphSpec,
        recovery_order: Optional[list[tuple[tuple, datetime]]] = None,
    ) -> list[ScoredCandidate]:
        """Score every node in the graph.

        ``recovery_order`` — (node_key, recovered_at) in recovery sequence
        (§31). Causes that recover *before* their effects gain support;
        a supposed cause recovering last is recorded as uncertainty.
        """
        recovery_order = recovery_order or []
        recovery_rank: dict[tuple, int] = {
            key: idx for idx, (key, _t) in enumerate(recovery_order)
        }
        scored: list[ScoredCandidate] = []
        for node in graph.nodes.values():
            scored.append(self._score_node(node, graph, recovery_rank))
        # Relative normalization inside one analysis: the top candidate maps
        # to 1.0 *only if* its own support is real (≥ LOW_MIN_SUPPORT);
        # otherwise scores stay small and honest.
        scored.sort(key=lambda s: (-s.score, s.label))
        return scored

    # -- internals -------------------------------------------------------------
    def _score_node(
        self,
        node: HypothesisNode,
        graph: CausalGraphSpec,
        recovery_rank: dict[tuple, int],
    ) -> ScoredCandidate:
        breakdown = ScoreBreakdown()
        supporting_categories: set[str] = set()
        supporting_count = 0
        contradicting_count = 0
        reasons: list[str] = []

        for evidence in node.supporting():
            supporting_categories.add(evidence.category.value)
            supporting_count += 1
        for evidence in node.contradicting():
            contradicting_count += 1

        # Category contributions: share of that category's evidence mass.
        def category_mass(category_values: set[str]) -> float:
            total = sum(
                e.weight
                for e in node.supporting()
                if e.category.value in category_values
            )
            return min(1.0, total)

        breakdown.trace = W_TRACE * category_mass({"TRACE"})
        breakdown.temporal = W_TEMPORAL * category_mass({"TEMPORAL"})
        breakdown.dependency = W_DEPENDENCY * category_mass({"DEPENDENCY"})
        breakdown.change = W_CHANGE * category_mass(
            {"CHANGE", "DEPLOYMENT", "CONFIGURATION"}
        )
        breakdown.resource = W_RESOURCE * category_mass(
            {"RESOURCE", "METRIC", "LOG", "HEALTH"}
        )
        breakdown.recovery = W_RECOVERY * category_mass({"RECOVERY"})

        # Propagation/outgoing-edge support: this node is the source of
        # evidence-backed edges (it explains other nodes' failures).
        out_edges = graph.edges_from(node.key)
        if out_edges:
            edge_mass = sum(len(e.evidence) for e in out_edges)
            breakdown.outgoing_edge_support = W_PROPAGATION * min(1.0, edge_mass / 3.0)

        breakdown.contradiction_penalty = min(
            MAX_CONTRADICTION_PENALTY,
            W_CONTRADICTION_PENALTY * contradicting_count,
        )

        score = breakdown.total

        # -- Confidence (§27) — separate from score ----------------------------
        if contradicting_count > 0 and len(supporting_categories) <= 1:
            confidence = ConfidenceLevel.INSUFFICIENT
            confidence_reason = (
                "Evidence is contradictory with no independent corroboration"
            )
        elif score < INSUFFICIENT_SUPPORT:
            confidence = ConfidenceLevel.INSUFFICIENT
            confidence_reason = "Too little evidence to rank this candidate"
        elif len(supporting_categories) >= HIGH_MIN_CATEGORIES and (
            breakdown.trace > 0 or not HIGH_REQUIRES_TRACE
        ):
            confidence = ConfidenceLevel.HIGH
            confidence_reason = (
                f"{len(supporting_categories)} independent evidence categories align"
                + (" including trace direction" if breakdown.trace > 0 else "")
            )
        elif len(supporting_categories) >= MEDIUM_MIN_CATEGORIES:
            confidence = ConfidenceLevel.MEDIUM
            confidence_reason = (
                f"{len(supporting_categories)} evidence categories align, but direct "
                "causal evidence is incomplete"
            )
        elif score >= LOW_MIN_SUPPORT:
            confidence = ConfidenceLevel.LOW
            confidence_reason = (
                "Only temporal/structural evidence — a hypothesis, not a conclusion"
            )
        else:
            confidence = ConfidenceLevel.INSUFFICIENT
            confidence_reason = "Evidence too sparse to support this hypothesis"

        # -- Contradictions cap the bucket (§12, §27) ---------------------------
        # Corroboration on some axes does not license ignoring refutation on
        # another: a candidate whose contradicting weight rivals its supporting
        # weight is capped, and the cap is stated rather than hidden.
        support_before_penalty = score + breakdown.contradiction_penalty
        if contradicting_count and support_before_penalty > 0:
            ratio = breakdown.contradiction_penalty / support_before_penalty
            cap: Optional[ConfidenceLevel] = None
            if ratio >= CONTRADICTION_DOMINANT_RATIO:
                cap = ConfidenceLevel.LOW
            elif ratio >= CONTRADICTION_MATERIAL_RATIO:
                cap = ConfidenceLevel.MEDIUM
            if (
                cap is not None
                and _CONFIDENCE_ORDER[confidence] > _CONFIDENCE_ORDER[cap]
            ):
                confidence = cap
                confidence_reason += (
                    f"; capped at {cap.value} because contradicting evidence is "
                    f"material ({ratio:.0%} of this candidate's evidence weight)"
                )

        # -- Reasons (§37): one line per evidence fact, quoted -----------------
        for evidence in sorted(node.supporting(), key=lambda e: -e.weight):
            reasons.append(f"[{evidence.category.value}] {evidence.quote}")
        for evidence in node.contradicting():
            reasons.append(f"[CONTRADICTING] {evidence.quote}")

        # -- Recovery-order sanity (§31) ---------------------------------------
        uncertainty: dict = {"missing": [], "caveats": []}
        if node.key in recovery_rank and out_edges:
            my_rank = recovery_rank[node.key]
            later_recoveries = [
                graph.nodes[e.target_key].label
                for e in out_edges
                if e.target_key in recovery_rank
                and recovery_rank[e.target_key] < my_rank
            ]
            if later_recoveries:
                uncertainty["caveats"].append(
                    "Supposed cause recovered after its effects "
                    f"({', '.join(later_recoveries)}) — recovery ordering weakens "
                    "this hypothesis"
                )
        if breakdown.trace == 0:
            uncertainty["missing"].append("trace evidence showing the call path")
        if breakdown.temporal == 0:
            uncertainty["missing"].append("temporal alignment with the incident onset")

        return ScoredCandidate(
            key=node.key,
            label=node.label,
            score=score,
            breakdown=breakdown,
            confidence=confidence,
            confidence_reason=confidence_reason,
            supporting_categories=supporting_categories,
            supporting_count=supporting_count,
            contradicting_count=contradicting_count,
            reasons=reasons,
            uncertainty=uncertainty,
        )
