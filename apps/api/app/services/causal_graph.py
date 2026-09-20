"""ARGUS Causal Graph Builder & Validator (Phase 4 §9, §32, §33).

Turns analyzer output into the incident's *causal graph*: a small set of
directed edges between candidates, where **every edge names the stored facts
that justify it**. Two rules govern everything here:

* no edge without evidence — a relationship nobody can point at must not
  exist, even if the structure suggests it (§32);
* direction must be earned — structural adjacency ("A calls B") only
  licenses a hypothesis, never a causal edge (§14).

``CORRELATES_WITH`` is kept strictly apart from the causal types: it is used
when two candidates co-occur but no direction evidence exists, and it is
*refused* when direction evidence does exist. The validator (§33) rejects
impossible edges (effect precedes cause) instead of emitting them silently,
so downstream consumers never need to re-check timestamps.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Sequence

from app.core.time import ensure_utc_or_now
from app.models.causal import (
    CausalEvidenceCategory,
    CausalRelationshipType,
    EvidencePolarity,
)
from app.services.change_analyzer import ChangeAnalysisResult
from app.services.dependency_analyzer import DependencyContext, StructuralRelation
from app.services.trace_analyzer import TraceAnalysisResult

#: Effect precedes cause by more than this → the edge is rejected/annotated.
TEMPORAL_CONTRADICTION_TOLERANCE_SECONDS = 5

#: A causal (directional) edge needs at least this many distinct supporting
#: facts; with fewer, the pair is downgraded to CORRELATES_WITH.
MIN_DIRECTION_FACTS = 1

_CATEGORY_WEIGHTS: dict[CausalEvidenceCategory, float] = {
    CausalEvidenceCategory.TRACE: 1.0,
    CausalEvidenceCategory.TEMPORAL: 0.7,
    CausalEvidenceCategory.DEPENDENCY: 0.6,
    CausalEvidenceCategory.CHANGE: 0.6,
    CausalEvidenceCategory.DEPLOYMENT: 0.6,
    CausalEvidenceCategory.CONFIGURATION: 0.6,
    CausalEvidenceCategory.METRIC: 0.4,
    CausalEvidenceCategory.LOG: 0.4,
    CausalEvidenceCategory.HEALTH: 0.4,
    CausalEvidenceCategory.RESOURCE: 0.5,
    CausalEvidenceCategory.RECOVERY: 0.5,
    CausalEvidenceCategory.CONTRADICTING: 0.0,
}


class EdgeKind(str, Enum):
    """How an edge between two candidates came to exist (provenance label)."""

    TRACE_FAILURE = "TRACE_FAILURE"  # failing child span inside parent span
    PROPAGATION = "PROPAGATION"  # ordered failure onsets within one trace
    DEPENDENCY_PRECEDENCE = "DEPENDENCY_PRECEDENCE"  # provider failed first
    CHANGE_TRIGGERED = "CHANGE_TRIGGERED"  # temporally-relevant change


@dataclass(frozen=True)
class EvidenceSpec:
    """A piece of evidence before persistence — always points at a stored row."""

    category: CausalEvidenceCategory
    polarity: EvidencePolarity
    source_table: str
    source_id: Optional[uuid.UUID]
    quote: str
    explanation: str
    component_id: Optional[uuid.UUID] = None
    observed_at: Optional[datetime] = None
    strength: float = 0.0
    #: Set when this fact justifies a specific edge rather than a candidate.
    edge_kind: Optional[EdgeKind] = None

    @property
    def weight(self) -> float:
        return _CATEGORY_WEIGHTS.get(self.category, 0.0) * max(
            0.0, min(1.0, self.strength)
        )


@dataclass
class HypothesisNode:
    """A candidate in the in-memory graph (mirrors CandidateSeed + identity)."""

    key: tuple  # the CandidateSeed.dedup_key
    candidate_type: str
    component_id: Optional[uuid.UUID]
    event_id: Optional[uuid.UUID]
    label: str
    explanation: str
    first_observed_at: Optional[datetime] = None
    is_external: bool = False
    evidence: list[EvidenceSpec] = field(default_factory=list)

    @property
    def at(self) -> Optional[datetime]:
        return (
            ensure_utc_or_now(self.first_observed_at)
            if self.first_observed_at
            else None
        )

    def supporting(self) -> list[EvidenceSpec]:
        return [e for e in self.evidence if e.polarity is EvidencePolarity.SUPPORTING]

    def contradicting(self) -> list[EvidenceSpec]:
        return [
            e for e in self.evidence if e.polarity is EvidencePolarity.CONTRADICTING
        ]


@dataclass
class HypothesisEdge:
    """A directed candidate→candidate relationship before persistence."""

    source_key: tuple
    target_key: tuple
    relationship_type: CausalRelationshipType
    edge_kind: EdgeKind
    evidence: list[EvidenceSpec] = field(default_factory=list)
    temporal_alignment_seconds: Optional[int] = None
    #: 0/1/2 = none/indirect/direct structural support (DependencyContext).
    structural_support: int = 0
    #: 0/1 — stored traces show a parent→child call between the components.
    observational_support: int = 0
    contradiction_notes: list[str] = field(default_factory=list)
    explanation: str = ""

    @property
    def directional(self) -> bool:
        return self.relationship_type is not CausalRelationshipType.CORRELATES_WITH


@dataclass
class CausalGraphSpec:
    """The per-incident causal graph, ready for persistence."""

    nodes: dict[tuple, HypothesisNode] = field(default_factory=dict)
    edges: list[HypothesisEdge] = field(default_factory=list)
    #: Pairs that had *some* co-occurrence but failed validation — reported,
    #: never silently dropped (the explanation API surfaces these).
    rejected_pairs: list[tuple[str, str, str]] = field(default_factory=list)

    def edges_from(self, key: tuple) -> list[HypothesisEdge]:
        return [e for e in self.edges if e.source_key == key]

    def edges_to(self, key: tuple) -> list[HypothesisEdge]:
        return [e for e in self.edges if e.target_key == key]


class CausalValidator:
    """Chain/edge validation (§33). Pure functions over timestamps + evidence."""

    def __init__(
        self, *, tolerance_seconds: int = TEMPORAL_CONTRADICTION_TOLERANCE_SECONDS
    ) -> None:
        self._tolerance = int(tolerance_seconds)

    def validate_edge(self, edge: HypothesisEdge) -> Optional[str]:
        """Return a rejection reason, or ``None`` when the edge is acceptable."""
        if not edge.evidence:
            return "no supporting evidence"
        cause = edge.edge_kind in (
            EdgeKind.TRACE_FAILURE,
            EdgeKind.PROPAGATION,
            EdgeKind.DEPENDENCY_PRECEDENCE,
            EdgeKind.CHANGE_TRIGGERED,
        )
        if cause and edge.relationship_type is CausalRelationshipType.CORRELATES_WITH:
            return "direction evidence exists but type is CORRELATES_WITH"
        if (
            not cause
            and edge.relationship_type is not CausalRelationshipType.CORRELATES_WITH
        ):
            return "causal type without direction evidence"
        if (
            edge.temporal_alignment_seconds is not None
            and edge.temporal_alignment_seconds < -self._tolerance
        ):
            return (
                f"effect precedes cause by {-edge.temporal_alignment_seconds}s"
                " (temporal contradiction)"
            )
        if edge.directional and not edge.evidence:
            return "directional edge without evidence"
        return None

    def validate_chain(self, chain: Sequence[HypothesisEdge]) -> list[str]:
        """Validate a source→…→sink path; returns all violations found."""
        problems: list[str] = []
        for idx, edge in enumerate(chain):
            reason = self.validate_edge(edge)
            if reason is not None:
                problems.append(f"edge {idx}: {reason}")
        return problems

    def temporal_alignment(
        self, cause: HypothesisNode, effect: HypothesisNode
    ) -> Optional[int]:
        """Seconds from cause onset to effect onset (negative = effect first)."""
        cause_at, effect_at = cause.at, effect.at
        if cause_at is None or effect_at is None:
            return None
        return int((effect_at - cause_at).total_seconds())


class CausalGraphBuilder:
    """Assembles the causal graph from candidate seeds + analyzer outputs (§9)."""

    def __init__(
        self,
        *,
        validator: Optional[CausalValidator] = None,
        max_edges: int = 16,
    ) -> None:
        self._validator = validator or CausalValidator()
        self._max_edges = max(4, int(max_edges))

    # -- public API ----------------------------------------------------------
    def build(
        self,
        *,
        nodes: dict[tuple, HypothesisNode],
        trace_result: TraceAnalysisResult,
        dependency_context: DependencyContext,
        change_result: ChangeAnalysisResult,
    ) -> CausalGraphSpec:
        graph = CausalGraphSpec(nodes=nodes)
        candidate_edges: list[HypothesisEdge] = []

        candidate_edges.extend(
            self._edges_from_traces(nodes, trace_result, dependency_context)
        )
        candidate_edges.extend(
            self._edges_from_dependency_precedence(nodes, dependency_context)
        )
        candidate_edges.extend(
            self._edges_from_changes(nodes, change_result, dependency_context)
        )

        # Merge first, then budget: the cap bounds the *stored* graph, so
        # counting duplicate candidate edges against it would silently drop
        # real relationships that a different lens discovered.
        merged = self._dedupe_edges(candidate_edges)
        accepted: list[HypothesisEdge] = []
        for edge in merged:
            if len(accepted) >= self._max_edges:
                graph.rejected_pairs.append(
                    (
                        str(edge.source_key),
                        str(edge.target_key),
                        "edge budget exhausted",
                    )
                )
                continue
            reason = self._validator.validate_edge(edge)
            if reason is None:
                edge.explanation = self._explain_edge(edge)
                accepted.append(edge)
            else:
                graph.rejected_pairs.append(
                    (str(edge.source_key), str(edge.target_key), reason)
                )
        graph.edges = accepted
        return graph

    # -- trace-derived edges (§15, §16) --------------------------------------
    def _edges_from_traces(
        self,
        nodes: dict[tuple, HypothesisNode],
        trace_result: TraceAnalysisResult,
        dependency_context: DependencyContext,
    ) -> list[HypothesisEdge]:
        edges: list[HypothesisEdge] = []
        # Group failing-call edges by (parent component, child component).
        grouped: dict[tuple[Optional[uuid.UUID], Optional[uuid.UUID]], list] = {}
        for failure_edge in trace_result.edges:
            key = (failure_edge.parent_component_id, failure_edge.child_component_id)
            grouped.setdefault(key, []).append(failure_edge)

        for (parent_id, child_id), observations in grouped.items():
            parent_key = self._component_key(nodes, parent_id)
            child_key = self._component_key(nodes, child_id)
            if parent_key is None or child_key is None or parent_key == child_key:
                continue
            evidence = [
                EvidenceSpec(
                    category=CausalEvidenceCategory.TRACE,
                    polarity=EvidencePolarity.SUPPORTING,
                    source_table="span_records",
                    source_id=None,
                    quote=obs.observation,
                    explanation=(
                        f"A failing {obs.child_status or 'FAILED'} span on the child ran "
                        "inside the parent's span — the parent's call to this component "
                        "failed, which is directional evidence stronger than timing"
                    ),
                    component_id=child_id,
                    observed_at=obs.observed_at,
                    strength=0.9,
                    edge_kind=EdgeKind.TRACE_FAILURE,
                )
                for obs in observations
            ]
            relation = (
                CausalRelationshipType.LIKELY_CAUSE
                if len(observations) >= 3
                else CausalRelationshipType.POSSIBLE_CAUSE
            )
            edges.append(
                HypothesisEdge(
                    source_key=child_key,  # the failing child is the cause side
                    target_key=parent_key,  # the calling parent suffers the effect
                    relationship_type=relation,
                    edge_kind=EdgeKind.TRACE_FAILURE,
                    evidence=evidence,
                    structural_support=self._structural_score(
                        dependency_context, child_id, parent_id
                    ),
                    observational_support=1,
                    temporal_alignment_seconds=None,
                )
            )

        # Propagation order edges: consecutive failure onsets in one trace.
        for propagation in trace_result.propagations:
            ordered = [
                (component, moment)
                for component, moment in propagation.failure_order
                if component is not None
            ]
            for (cause_component, cause_at), (effect_component, effect_at) in zip(
                ordered, ordered[1:]
            ):
                cause_key = self._component_key(nodes, cause_component)
                effect_key = self._component_key(nodes, effect_component)
                if cause_key is None or effect_key is None or cause_key == effect_key:
                    continue
                delay = int((effect_at - cause_at).total_seconds())
                if delay < 0:
                    continue  # impossible within one trace; skip, don't invent
                edges.append(
                    HypothesisEdge(
                        source_key=cause_key,
                        target_key=effect_key,
                        relationship_type=CausalRelationshipType.DOWNSTREAM_EFFECT,
                        edge_kind=EdgeKind.PROPAGATION,
                        evidence=[
                            EvidenceSpec(
                                category=CausalEvidenceCategory.TEMPORAL,
                                polarity=EvidencePolarity.SUPPORTING,
                                source_table="traces",
                                source_id=None,
                                quote=(
                                    f"Trace {propagation.trace_id}: this component failed "
                                    f"first; the next component failed {delay}s later"
                                ),
                                explanation=(
                                    "Failure onsets inside one request are ordered — the "
                                    "later failure follows the earlier one within the "
                                    "same call path"
                                ),
                                component_id=effect_component,
                                observed_at=effect_at,
                                strength=0.8,
                                edge_kind=EdgeKind.PROPAGATION,
                            )
                        ],
                        structural_support=self._structural_score(
                            dependency_context, cause_component, effect_component
                        ),
                        observational_support=1,
                        temporal_alignment_seconds=delay,
                    )
                )
        return edges

    # -- dependency-precedence edges (§13, §14) -------------------------------
    def _edges_from_dependency_precedence(
        self,
        nodes: dict[tuple, HypothesisNode],
        dependency_context: DependencyContext,
    ) -> list[HypothesisEdge]:
        """Provider failed *before* its caller → POSSIBLE_CAUSE hypothesis.

        Structural adjacency alone never produces an edge: the pair must also
        show temporal precedence between the two candidates' first failures.
        """
        edges: list[HypothesisEdge] = []
        for key, node in nodes.items():
            if node.component_id is None:
                continue
            for provider_id in dependency_context.providers_of(node.component_id):
                provider_key = self._component_key(nodes, provider_id)
                if provider_key is None or provider_key == key:
                    continue
                provider_node = nodes[provider_key]
                caller_node = nodes[key]
                provider_at, caller_at = provider_node.at, caller_node.at
                if provider_at is None or caller_at is None:
                    continue
                delay = int((caller_at - provider_at).total_seconds())
                if delay <= 0:
                    continue  # no precedence → no hypothesis from structure
                edges.append(
                    HypothesisEdge(
                        source_key=provider_key,
                        target_key=key,
                        relationship_type=CausalRelationshipType.POSSIBLE_CAUSE,
                        edge_kind=EdgeKind.DEPENDENCY_PRECEDENCE,
                        evidence=[
                            EvidenceSpec(
                                category=CausalEvidenceCategory.DEPENDENCY,
                                polarity=EvidencePolarity.SUPPORTING,
                                source_table="component_dependencies",
                                source_id=None,
                                quote=(
                                    f"Dependency model: this component calls "
                                    f"{caller_node.label}; its failure preceded the "
                                    f"caller's by {delay}s"
                                ),
                                explanation=(
                                    "The caller depends on this component and degraded "
                                    "after it — precedence plus structure licenses a "
                                    "hypothesis, not a conclusion"
                                ),
                                component_id=provider_id,
                                observed_at=provider_at,
                                strength=0.6,
                                edge_kind=EdgeKind.DEPENDENCY_PRECEDENCE,
                            )
                        ],
                        structural_support=2,
                        observational_support=0,
                        temporal_alignment_seconds=delay,
                    )
                )
        return edges

    # -- change-triggered edges (§18, §19) ------------------------------------
    def _edges_from_changes(
        self,
        nodes: dict[tuple, HypothesisNode],
        change_result: ChangeAnalysisResult,
        dependency_context: DependencyContext,
    ) -> list[HypothesisEdge]:
        """Connect a relevant change only to the components it could reach.

        A deployment of the checkout service is a hypothesis about *checkout*
        and about the components that call it — not about every component in
        the incident. Fanning the edge out to the whole candidate set would
        manufacture relationships nobody observed (§14, §32).
        """
        edges: list[HypothesisEdge] = []
        for assessment in change_result.assessments:
            if assessment.relevance.value != "TEMPORALLY_RELEVANT":
                continue  # contradictions become candidate evidence, not edges
            change_key = self._event_key(nodes, assessment.event.event_id)
            if change_key is None:
                continue
            change_node = nodes[change_key]
            touched = assessment.event.component_id
            for key, node in nodes.items():
                if key == change_key or node.component_id is None:
                    continue
                if not assessment.touches_affected_components:
                    continue
                if touched is not None and node.component_id != touched:
                    # Only dependents of the changed component can be affected
                    # by it (the change travels *down* the call graph).
                    if touched not in dependency_context.providers_of(
                        node.component_id
                    ):
                        continue
                component_at = node.at
                if component_at is None:
                    continue
                delay = int((component_at - assessment.event.at).total_seconds())
                if delay < 0:
                    continue
                category = (
                    CausalEvidenceCategory.DEPLOYMENT
                    if assessment.event.kind == "DEPLOYMENT"
                    else CausalEvidenceCategory.CONFIGURATION
                )
                edges.append(
                    HypothesisEdge(
                        source_key=change_key,
                        target_key=key,
                        relationship_type=CausalRelationshipType.POSSIBLE_CAUSE,
                        edge_kind=EdgeKind.CHANGE_TRIGGERED,
                        evidence=[
                            EvidenceSpec(
                                category=category,
                                polarity=EvidencePolarity.SUPPORTING,
                                source_table=assessment.event.kind.lower(),
                                source_id=assessment.event.event_id,
                                quote=(
                                    f"{assessment.event.label} occurred "
                                    f"{delay}s before this component's degradation"
                                ),
                                explanation=(
                                    "The change is temporally relevant and touches the "
                                    "affected component — supporting context, never proof"
                                ),
                                component_id=node.component_id,
                                observed_at=assessment.event.occurred_at,
                                strength=0.5,
                                edge_kind=EdgeKind.CHANGE_TRIGGERED,
                            )
                        ],
                        structural_support=1 if change_node.component_id else 0,
                        observational_support=0,
                        temporal_alignment_seconds=delay,
                    )
                )
        return edges

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _component_key(
        nodes: dict[tuple, HypothesisNode], component_id: Optional[uuid.UUID]
    ) -> Optional[tuple]:
        if component_id is None:
            return None
        for key, node in nodes.items():
            if node.component_id == component_id:
                return key
        return None

    @staticmethod
    def _event_key(
        nodes: dict[tuple, HypothesisNode], event_id: uuid.UUID
    ) -> Optional[tuple]:
        for key, node in nodes.items():
            if node.event_id == event_id:
                return key
        return None

    @staticmethod
    def _structural_score(
        dependency_context: DependencyContext,
        source_id: Optional[uuid.UUID],
        target_id: Optional[uuid.UUID],
    ) -> int:
        if source_id is None or target_id is None:
            return 0
        relation = dependency_context.relation(source_id, target_id)
        if relation is StructuralRelation.DIRECT:
            return 2
        if relation is StructuralRelation.INDIRECT:
            return 1
        return 0

    @staticmethod
    def _dedupe_edges(edges: list[HypothesisEdge]) -> list[HypothesisEdge]:
        """One edge per (source, target) — strongest type wins, evidence merges."""
        best: dict[tuple, HypothesisEdge] = {}
        order: list[tuple] = []
        causal_rank = {
            CausalRelationshipType.LIKELY_CAUSE: 0,
            CausalRelationshipType.TRIGGERS: 1,
            CausalRelationshipType.POSSIBLE_CAUSE: 2,
            CausalRelationshipType.DOWNSTREAM_EFFECT: 3,
            CausalRelationshipType.CONTRIBUTES_TO: 4,
            CausalRelationshipType.AMPLIFIES: 5,
            CausalRelationshipType.BLOCKS: 6,
            CausalRelationshipType.CORRELATES_WITH: 7,
        }
        for edge in edges:
            pair = (edge.source_key, edge.target_key)
            if pair not in best:
                best[pair] = edge
                order.append(pair)
                continue
            existing = best[pair]
            if (
                causal_rank[edge.relationship_type]
                < causal_rank[existing.relationship_type]
            ):
                merged = edge
                merged.evidence = existing.evidence + edge.evidence
                merged.observational_support = max(
                    existing.observational_support, edge.observational_support
                )
                merged.structural_support = max(
                    existing.structural_support, edge.structural_support
                )
                best[pair] = merged
            else:
                existing.evidence.extend(edge.evidence)
                existing.observational_support = max(
                    existing.observational_support, edge.observational_support
                )
                existing.structural_support = max(
                    existing.structural_support, edge.structural_support
                )
        return [best[pair] for pair in order]

    @staticmethod
    def _explain_edge(edge: HypothesisEdge) -> str:
        kinds = sorted({e.edge_kind.value for e in edge.evidence if e.edge_kind})
        parts = [
            f"{len(edge.evidence)} stored fact(s): {', '.join(kinds) or 'evidence'}"
        ]
        if edge.temporal_alignment_seconds is not None:
            parts.append(f"effect onset {edge.temporal_alignment_seconds}s after cause")
        if edge.structural_support == 2:
            parts.append("direct dependency in the graph model")
        elif edge.structural_support == 1:
            parts.append("indirect dependency in the graph model")
        if edge.observational_support:
            parts.append("trace spans show the call path")
        return "; ".join(parts)
