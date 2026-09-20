"""ARGUS Causal Candidate Generator (Phase 4 §20–§22, §28).

Turns analyzer output into a *bounded* set of root-cause candidates. Every
candidate must name the concrete stored rows that produced it — the generator
never invents a candidate type or a component:

* change candidates come from ``ChangeAnalyzer`` assessments;
* component candidates come from observed anomaly components and trace
  propagation origins;
* dependency-failure candidates come from trace failure edges (a child that
  repeatedly fails inside parents);
* resource candidates come from RESOURCE-class anomalies;
* external-dependency candidates come from components flagged external.

Candidate types are derived from the producing evidence, never assumed. The
count is bounded by ``CAUSAL_MAX_CANDIDATES``; beyond the budget, lower-ranked
candidates are merged into an "other observations" note instead of silently
dropped. A candidate with zero evidence is never emitted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from app.models.anomaly import AnomalyType
from app.models.causal import CandidateType
from app.services.change_analyzer import ChangeAssessment, ChangeAnalysisResult
from app.services.trace_analyzer import TraceAnalysisResult


@dataclass
class CandidateSeed:
    """A hypothesis before persistence — carries its producing evidence refs."""

    candidate_type: CandidateType
    component_id: Optional[uuid.UUID]
    event_id: Optional[uuid.UUID]
    event_kind: Optional[str]
    is_external: bool = False
    #: Human label, used in explanations ("Deployment deploy-123 ...").
    label: str = ""
    #: Why this candidate exists — one sentence, evidence-quoted.
    explanation: str = ""
    #: (source_table, source_id, quote, observed_at) provenance rows.
    origin_refs: list[tuple[str, Optional[uuid.UUID], str, Optional[datetime]]] = field(
        default_factory=list
    )
    #: Score bonus inputs computed at generation time (e.g. touched provider).
    touches_affected: bool = False
    is_temporal_contradiction: bool = False

    @property
    def dedup_key(self) -> tuple:
        """Identity of the hypothesis — *what* is accused, not how we found it.

        A component reached through a failing span and the same component
        reached through its own anomaly are **one** hypothesis, not two
        competitors: splitting them would dilute each one's evidence and let a
        weaker reading outrank a better-evidenced one. Change events stay
        separate because a deployment and the component it touched are
        genuinely different explanations.
        """
        if self.event_id is not None:
            return ("EVENT", self.event_id)
        if self.component_id is not None:
            return ("COMPONENT", self.component_id)
        return ("UNANCHORED", self.label)


#: When two discovery paths merge, the more specific characterisation wins
#: (a datastore stays a ``DATABASE`` even if a trace found it first).
_TYPE_SPECIFICITY: dict[CandidateType, int] = {
    CandidateType.DATABASE: 0,
    CandidateType.EXTERNAL_DEPENDENCY: 1,
    CandidateType.RESOURCE_EXHAUSTION: 2,
    CandidateType.INFRASTRUCTURE: 3,
    CandidateType.DEPENDENCY_FAILURE: 4,
    CandidateType.APPLICATION_COMPONENT: 5,
    CandidateType.DATA_ISSUE: 6,
    CandidateType.UNKNOWN: 7,
}


#: Anomaly types that describe a component's own degradation (vs a signal).
_COMPONENT_ANOMALY_TYPES = {
    AnomalyType.LATENCY_SPIKE,
    AnomalyType.ERROR_RATE_SPIKE,
    AnomalyType.HEALTH_DEGRADATION,
    AnomalyType.TRACE_FAILURE_SPIKE,
    AnomalyType.LOG_PATTERN_SPIKE,
    AnomalyType.METRIC_BASELINE_DEVIATION,
    AnomalyType.METRIC_THRESHOLD,
    AnomalyType.THROUGHPUT_DROP,
    AnomalyType.RESOURCE_USAGE_SPIKE,
}


class CausalCandidateGenerator:
    """Builds the bounded candidate set from analyzer outputs (§22)."""

    def __init__(self, *, max_candidates: int = 8) -> None:
        self._max_candidates = max(3, int(max_candidates))

    def generate(
        self,
        *,
        project_id: uuid.UUID,
        onset: datetime,
        anomaly_components: Sequence[
            tuple[Optional[uuid.UUID], str, AnomalyType, Optional[datetime]]
        ],
        change_result: ChangeAnalysisResult,
        trace_result: TraceAnalysisResult,
        external_component_ids: set[uuid.UUID] | None = None,
        database_component_ids: set[uuid.UUID] | None = None,
    ) -> list[CandidateSeed]:
        """Assemble candidates.

        ``anomaly_components`` — (component_id, label, anomaly_type, observed_at)
        for each incident anomaly; labels come from the stored description.
        """
        externals = external_component_ids or set()
        databases = database_component_ids or set()
        seeds: list[CandidateSeed] = []
        seen: set[tuple] = set()

        by_key: dict[tuple, CandidateSeed] = {}

        def add(seed: CandidateSeed) -> None:
            if seed.component_id is None and seed.event_id is None:
                return  # a candidate must anchor to something stored
            key = seed.dedup_key
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = seed
                seen.add(key)
                seeds.append(seed)
                return
            # Merge: pool the evidence, keep the strongest characterisation,
            # and never drop a contradiction someone else noticed.
            existing.origin_refs.extend(seed.origin_refs)
            if _TYPE_SPECIFICITY.get(seed.candidate_type, 9) < _TYPE_SPECIFICITY.get(
                existing.candidate_type, 9
            ):
                existing.candidate_type = seed.candidate_type
            existing.is_external = existing.is_external or seed.is_external
            existing.touches_affected = (
                existing.touches_affected or seed.touches_affected
            )
            existing.is_temporal_contradiction = (
                existing.is_temporal_contradiction or seed.is_temporal_contradiction
            )
            if seed.explanation and seed.explanation not in existing.explanation:
                existing.explanation = (
                    f"{existing.explanation}; {seed.explanation}"
                    if existing.explanation
                    else seed.explanation
                )

        # -- 1. Change candidates (§18, §19) ---------------------------------
        for assessment in change_result.assessments:
            add(self._seed_from_change(assessment))

        # -- 2. First-failing component(s) from trace propagation (§16) ------
        for propagation in trace_result.propagations:
            if not propagation.failure_order:
                continue
            origin_component, origin_at = propagation.failure_order[0]
            if origin_component is None:
                continue
            add(
                CandidateSeed(
                    candidate_type=CandidateType.APPLICATION_COMPONENT,
                    component_id=origin_component,
                    event_id=None,
                    event_kind="TRACE_PROPAGATION_ORIGIN",
                    label=self._component_label(origin_component),
                    explanation=(
                        f"In trace {propagation.trace_id}, this component failed "
                        f"{propagation.delays[0] if propagation.delays else 0}s before the "
                        "next component failed — first in the propagation order"
                    ),
                    origin_refs=[
                        (
                            "trace_records",
                            None,
                            f"Trace {propagation.trace_id}: first failing component",
                            origin_at,
                        )
                    ],
                )
            )

        # -- 3. Dependency-failure candidates from trace edges (§15) ---------
        child_failures: dict[Optional[uuid.UUID], list] = {}
        for edge in trace_result.edges:
            child_failures.setdefault(edge.child_component_id, []).append(edge)
        for child_component, edges in child_failures.items():
            if child_component is None or not edges:
                continue
            sample = edges[0]
            candidate_type = (
                CandidateType.DATABASE
                if child_component in databases
                else CandidateType.DEPENDENCY_FAILURE
            )
            add(
                CandidateSeed(
                    candidate_type=candidate_type,
                    component_id=child_component,
                    event_id=None,
                    event_kind="TRACE_FAILURE_EDGE",
                    label=self._component_label(child_component),
                    is_external=child_component in externals,
                    explanation=(
                        f"{len(edges)} failing child span(s) recorded inside parent "
                        f"spans — e.g. {sample.observation}"
                    ),
                    origin_refs=[
                        (
                            "span_records",
                            None,
                            edge.observation,
                            edge.observed_at,
                        )
                        for edge in edges[:5]
                    ],
                )
            )

        # -- 4. Anomaly components (§22: first failing components) -----------
        for component_id, label, anomaly_type, observed_at in anomaly_components:
            if component_id is None:
                continue
            if anomaly_type is AnomalyType.RESOURCE_USAGE_SPIKE:
                candidate_type = CandidateType.RESOURCE_EXHAUSTION
            elif component_id in databases:
                candidate_type = CandidateType.DATABASE
            elif component_id in externals:
                candidate_type = CandidateType.EXTERNAL_DEPENDENCY
            else:
                candidate_type = CandidateType.APPLICATION_COMPONENT
            add(
                CandidateSeed(
                    candidate_type=candidate_type,
                    component_id=component_id,
                    event_id=None,
                    event_kind="OBSERVED_ANOMALY",
                    label=label,
                    is_external=component_id in externals,
                    explanation=f"Observed {anomaly_type.value} anomaly on this component",
                    origin_refs=[
                        (
                            "anomalies",
                            None,
                            f"{anomaly_type.value} observed on {label}",
                            observed_at,
                        )
                    ],
                )
            )

        return self._bound(seeds)

    # -- Seed builders -------------------------------------------------------
    @staticmethod
    def _seed_from_change(assessment: ChangeAssessment) -> CandidateSeed:
        event = assessment.event
        candidate_type = (
            CandidateType.DEPLOYMENT
            if event.kind == "DEPLOYMENT"
            else CandidateType.CONFIGURATION_CHANGE
        )
        return CandidateSeed(
            candidate_type=candidate_type,
            component_id=event.component_id,
            event_id=event.event_id,
            event_kind=event.kind,
            label=event.label,
            explanation=assessment.reason,
            touches_affected=assessment.touches_affected_components,
            is_temporal_contradiction=assessment.is_temporal_contradiction,
            origin_refs=[
                (
                    "deployment_events"
                    if event.kind == "DEPLOYMENT"
                    else "configuration_change_events",
                    event.event_id,
                    f"{event.label}: {assessment.reason}",
                    event.at,
                )
            ],
        )

    def _bound(self, seeds: list[CandidateSeed]) -> list[CandidateSeed]:
        """Rank by generation-time priority and cap (§22 bounded count).

        Contradicting changes **always survive the cut**: they are the evidence
        that a tempting explanation is wrong, and hiding them would be lying by
        omission. The remaining budget goes to candidates touching affected
        components first, then to those with richer evidence.
        """
        contradictions = [seed for seed in seeds if seed.is_temporal_contradiction]
        ranked = sorted(
            (seed for seed in seeds if not seed.is_temporal_contradiction),
            key=lambda s: (
                0 if s.touches_affected else 1,
                -len(s.origin_refs),
                s.label,
            ),
        )
        budget = max(0, self._max_candidates - len(contradictions))
        kept = contradictions + ranked[:budget]
        overflow = len(seeds) - len(kept)
        if overflow > 0:
            note = CandidateSeed(
                candidate_type=CandidateType.UNKNOWN,
                component_id=None,
                event_id=None,
                event_kind="OVERFLOW",
                label=f"{overflow} additional observations",
                explanation=(
                    f"{overflow} further evidence-backed observations existed but "
                    "were below the candidate budget; nothing was hidden — they "
                    "remain in the analysis evidence"
                ),
            )
            kept.append(note)
        return kept

    @staticmethod
    def _component_label(component_id: uuid.UUID) -> str:
        return f"component {str(component_id)[:8]}"


__all__ = [
    "CandidateSeed",
    "CausalCandidateGenerator",
]
