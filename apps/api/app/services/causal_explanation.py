"""ARGUS Root Cause Explanation Service (Phase 4 §30, §32, §33, §37, §38, §41).

Turns stored causal rows into explanations an engineer can act on. Two rules
shape everything here:

* **The deterministic engine is authoritative** (§38). Explanations are
  *derived* from the persisted candidates, evidence and relationships. Every
  sentence is traceable to a row, and no provider may invent a fact, change a
  score, or soften a contradiction.
* **Explanations are structured first, prose second.** The API returns
  ``reasons``, ``supporting``/``contradicting`` splits and explicit
  ``limitations``; the narrative is a rendering of those, never a substitute
  for them (§37: no hidden chain-of-thought).

``RootCauseExplanationProvider`` is the seam for an optional interpretation
layer. ``DeterministicExplanationProvider`` is the default and the only
implementation that may produce the authoritative fields.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.causal import (
    CausalAnalysis,
    CausalEvidence,
    CausalRelationship,
    CausalRelationshipType,
    EvidencePolarity,
    RootCauseCandidate,
)

logger = logging.getLogger(__name__)

#: Rendered with every explanation — the phase's central honesty statement.
CAUSAL_DISCLAIMER = (
    "Causal analysis produces evidence-supported hypotheses; it does not "
    "constitute mathematical proof of causation."
)

#: Relationship types that assert a direction (vs. plain co-occurrence).
DIRECTIONAL_TYPES = {
    CausalRelationshipType.POSSIBLE_CAUSE,
    CausalRelationshipType.LIKELY_CAUSE,
    CausalRelationshipType.CONTRIBUTES_TO,
    CausalRelationshipType.TRIGGERS,
    CausalRelationshipType.BLOCKS,
    CausalRelationshipType.AMPLIFIES,
    CausalRelationshipType.DOWNSTREAM_EFFECT,
}


@dataclass
class LoadedAnalysis:
    """An analysis plus everything needed to explain it, already loaded."""

    analysis: CausalAnalysis
    candidates: list[RootCauseCandidate]
    relationships: list[CausalRelationship]
    evidence: list[CausalEvidence]

    def evidence_for(self, candidate_id: uuid.UUID) -> list[CausalEvidence]:
        """The candidate's *own* facts, excluding rows bound to an edge.

        Edge-bound rows carry a ``candidate_id`` only because the schema
        requires one; they justify a relationship, and counting them here would
        make a candidate report more evidence than the scorer counted when it
        chose that candidate's confidence. The split returned to a client must
        equal the stored counts exactly, so edge rows are reachable only
        through :meth:`evidence_for_relationship` (§24, §41).
        """
        return [
            item
            for item in self.evidence
            if item.candidate_id == candidate_id and item.relationship_id is None
        ]

    def evidence_for_relationship(
        self, relationship_id: uuid.UUID
    ) -> list[CausalEvidence]:
        return [
            item for item in self.evidence if item.relationship_id == relationship_id
        ]


@dataclass
class CandidateExplanation:
    """A candidate's structured explanation (§37)."""

    candidate_id: uuid.UUID
    candidate_type: str
    label: Optional[str]
    confidence: str
    score: float
    supporting_evidence: int
    contradicting_evidence: int
    neutral_evidence: int
    reasons: list[str] = field(default_factory=list)
    supporting_quotes: list[str] = field(default_factory=list)
    contradicting_quotes: list[str] = field(default_factory=list)
    score_breakdown: dict = field(default_factory=dict)
    uncertainty: dict = field(default_factory=dict)
    why_this_confidence: str = ""
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "candidate_id": str(self.candidate_id),
            "candidate_type": self.candidate_type,
            "label": self.label,
            "confidence": self.confidence,
            "score": round(self.score, 4),
            "supporting_evidence": self.supporting_evidence,
            "contradicting_evidence": self.contradicting_evidence,
            "neutral_evidence": self.neutral_evidence,
            "reasons": self.reasons,
            "supporting_quotes": self.supporting_quotes,
            "contradicting_quotes": self.contradicting_quotes,
            "score_breakdown": self.score_breakdown,
            "uncertainty": self.uncertainty,
            "why_this_confidence": self.why_this_confidence,
            "limitations": self.limitations,
        }


@dataclass
class RelationshipExplanation:
    """Why an edge is believed to exist (§41)."""

    relationship_id: uuid.UUID
    source_candidate_id: uuid.UUID
    target_candidate_id: uuid.UUID
    relationship_type: str
    directional: bool
    confidence: str
    temporal_alignment_seconds: Optional[int]
    structural_support: int
    observational_support: int
    evidence_quotes: list[str] = field(default_factory=list)
    explanation: str = ""
    caveats: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "relationship_id": str(self.relationship_id),
            "source_candidate_id": str(self.source_candidate_id),
            "target_candidate_id": str(self.target_candidate_id),
            "relationship_type": self.relationship_type,
            "directional": self.directional,
            "confidence": self.confidence,
            "temporal_alignment_seconds": self.temporal_alignment_seconds,
            "structural_support": self.structural_support,
            "observational_support": self.observational_support,
            "evidence_quotes": self.evidence_quotes,
            "explanation": self.explanation,
            "caveats": self.caveats,
        }


@dataclass
class AnalysisExplanation:
    """The whole analysis, narrated from its stored rows (§30)."""

    analysis_id: uuid.UUID
    analysis_version: int
    overall_confidence: str
    primary_candidate_id: Optional[uuid.UUID] = None
    headline: str = ""
    narrative: list[str] = field(default_factory=list)
    alternatives: list[CandidateExplanation] = field(default_factory=list)
    primary: Optional[CandidateExplanation] = None
    limitations: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    disclaimer: str = CAUSAL_DISCLAIMER

    def as_dict(self) -> dict:
        return {
            "analysis_id": str(self.analysis_id),
            "analysis_version": self.analysis_version,
            "overall_confidence": self.overall_confidence,
            "primary_candidate_id": (
                str(self.primary_candidate_id) if self.primary_candidate_id else None
            ),
            "headline": self.headline,
            "narrative": self.narrative,
            "primary": self.primary.as_dict() if self.primary else None,
            "alternatives": [alt.as_dict() for alt in self.alternatives],
            "limitations": self.limitations,
            "missing_evidence": self.missing_evidence,
            "disclaimer": self.disclaimer,
        }


class RootCauseExplanationProvider(Protocol):
    """Optional interpretation layer (§38).

    An implementation may only *re-word* what the deterministic engine already
    established: it receives the structured explanation and returns prose.
    """

    name: str

    def render(self, explanation: AnalysisExplanation) -> list[str]:
        """Return narrative lines for an already-derived explanation."""
        ...


class DeterministicExplanationProvider:
    """Default provider: prose assembled from stored facts, no model involved."""

    name = "deterministic"

    def render(self, explanation: AnalysisExplanation) -> list[str]:
        lines: list[str] = [explanation.headline]
        if explanation.primary is not None:
            lines.append(
                f"Primary hypothesis: {explanation.primary.label or 'unnamed candidate'} "
                f"({explanation.primary.candidate_type}) at "
                f"{explanation.primary.confidence} confidence, score "
                f"{explanation.primary.score:.2f}."
            )
            for reason in explanation.primary.reasons[:8]:
                lines.append(f"Evidence: {reason}")
            if explanation.primary.contradicting_quotes:
                for quote in explanation.primary.contradicting_quotes[:4]:
                    lines.append(f"Contradicting evidence: {quote}")
            else:
                lines.append("Contradicting evidence: none was observed.")
        for alternative in explanation.alternatives:
            lines.append(
                f"Alternative considered: {alternative.label or 'unnamed'} "
                f"({alternative.candidate_type}) — {alternative.confidence} "
                f"confidence because {alternative.why_this_confidence}"
            )
        if explanation.missing_evidence:
            lines.append("Missing evidence: " + "; ".join(explanation.missing_evidence))
        for limitation in explanation.limitations:
            lines.append(f"Limitation: {limitation}")
        lines.append(CAUSAL_DISCLAIMER)
        return lines


class NullExplanationProvider:
    """Explicitly-absent AI provider — the API stays deterministic by default.

    It exists so a deployment can *say* no interpretation layer is configured
    instead of silently implying every sentence came from one.
    """

    name = "none"

    def render(self, explanation: AnalysisExplanation) -> list[str]:
        return []


class CausalExplanationService:
    """Builds structured explanations for stored analyses (§30, §37, §41)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        provider: Optional[RootCauseExplanationProvider] = None,
    ) -> None:
        self._session = session
        self._provider: RootCauseExplanationProvider = (
            provider or DeterministicExplanationProvider()
        )

    async def load_analysis(
        self, *, project_id: uuid.UUID, incident_id: uuid.UUID, analysis_id: uuid.UUID
    ) -> Optional[LoadedAnalysis]:
        """Load a full analysis with candidates, edges and evidence.

        Scope is enforced in SQL (``project_id`` *and* ``incident_id``), so a
        route cannot accidentally serve another project's causal graph (§45).
        """
        stmt = (
            select(CausalAnalysis)
            .where(
                CausalAnalysis.id == analysis_id,
                CausalAnalysis.project_id == project_id,
                CausalAnalysis.incident_id == incident_id,
            )
            .options(selectinload(CausalAnalysis.candidates))
        )
        analysis = (await self._session.execute(stmt)).scalars().first()
        if analysis is None:
            return None
        relationships = list(
            (
                await self._session.execute(
                    select(CausalRelationship)
                    .where(CausalRelationship.analysis_id == analysis.id)
                    .order_by(
                        CausalRelationship.supporting_evidence_count.desc(),
                        CausalRelationship.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        evidence = list(
            (
                await self._session.execute(
                    select(CausalEvidence)
                    .where(CausalEvidence.analysis_id == analysis.id)
                    .order_by(CausalEvidence.strength.desc())
                )
            )
            .scalars()
            .all()
        )
        return LoadedAnalysis(
            analysis=analysis,
            candidates=list(analysis.candidates),
            relationships=relationships,
            evidence=evidence,
        )

    def explain_loaded(
        self,
        loaded: LoadedAnalysis,
        *,
        incident_label: str = "",
        labels: Optional[dict[uuid.UUID, str]] = None,
    ) -> AnalysisExplanation:
        """Explain an already-loaded analysis (§30)."""
        return self.explain(
            analysis=loaded.analysis,
            candidates=loaded.candidates,
            relationships=loaded.relationships,
            evidence=loaded.evidence,
            incident_label=incident_label,
            labels=labels,
        )

    @staticmethod
    def label_for(
        candidate: RootCauseCandidate, labels: dict[uuid.UUID, str]
    ) -> Optional[str]:
        """Human label for a candidate: component name, then its explanation.

        The component name is what an engineer recognises, so it wins; the
        discovery sentence is kept as the explanation field rather than being
        pressed into service as a label.
        """
        if candidate.component_id is not None:
            name = labels.get(candidate.component_id)
            if name:
                return name
        return None

    # -- structured explanations ---------------------------------------------
    def explain(
        self,
        *,
        analysis: CausalAnalysis,
        candidates: Sequence[RootCauseCandidate],
        relationships: Sequence[CausalRelationship],
        evidence: Sequence[CausalEvidence],
        incident_label: str = "",
        labels: Optional[dict[uuid.UUID, str]] = None,
    ) -> AnalysisExplanation:
        """Build the structured explanation for one analysis.

        ``labels`` maps component ids to names so a hypothesis reads as
        "Inventory Database" rather than as a UUID or an algorithm sentence.
        """
        names = labels or {}
        by_candidate: dict[uuid.UUID, list[CausalEvidence]] = {}
        for item in evidence:
            # Edge-bound facts are excluded for the same reason as in
            # ``evidence_for``: the explanation's evidence counts must match the
            # candidate's recorded counts rather than the edge's.
            if item.relationship_id is not None:
                continue
            by_candidate.setdefault(item.candidate_id, []).append(item)

        ordered = sorted(candidates, key=lambda c: (-c.score, str(c.id)))
        explained = [
            self.explain_candidate(
                candidate,
                by_candidate.get(candidate.id, []),
                label=self.label_for(candidate, names),
            )
            for candidate in ordered
        ]
        primary = next(
            (
                item
                for item, candidate in zip(explained, ordered)
                if candidate.id == analysis.primary_candidate_id
            ),
            None,
        )
        alternatives = [
            item
            for item, candidate in zip(explained, ordered)
            if candidate.id != analysis.primary_candidate_id
        ][:4]

        limitations = self._limitations(
            primary=primary, relationships=relationships, evidence=evidence
        )
        if analysis.primary_candidate_id is None:
            headline = (
                f"{incident_label or 'Incident'}: insufficient evidence to name a "
                "root cause. The candidates below are ranked but none reached the "
                "documented threshold for a primary hypothesis."
            )
        else:
            headline = (
                f"{incident_label or 'Incident'}: the most evidence-supported "
                f"explanation is {primary.label if primary else 'an unnamed candidate'}."
            )

        explanation = AnalysisExplanation(
            analysis_id=analysis.id,
            analysis_version=analysis.analysis_version,
            overall_confidence=(
                analysis.overall_confidence.value
                if hasattr(analysis.overall_confidence, "value")
                else str(analysis.overall_confidence)
            ),
            primary_candidate_id=analysis.primary_candidate_id,
            headline=headline,
            alternatives=alternatives,
            primary=primary,
            limitations=limitations,
            missing_evidence=list(analysis.missing_evidence or []),
        )
        explanation.narrative = self._provider.render(explanation)
        return explanation

    @staticmethod
    def explain_candidate(
        candidate: RootCauseCandidate,
        evidence: Sequence[CausalEvidence],
        *,
        label: Optional[str] = None,
    ) -> CandidateExplanation:
        supporting = [
            item for item in evidence if item.polarity is EvidencePolarity.SUPPORTING
        ]
        contradicting = [
            item for item in evidence if item.polarity is EvidencePolarity.CONTRADICTING
        ]
        neutral = [
            item for item in evidence if item.polarity is EvidencePolarity.NEUTRAL
        ]
        reasons = [f"[{item.category.value}] {item.quote}" for item in supporting] + [
            f"[CONTRADICTING] {item.quote}" for item in contradicting
        ]
        uncertainty = candidate.uncertainty or {}
        limitations = list(uncertainty.get("caveats", []))
        if not supporting:
            limitations.append(
                "No supporting evidence could be bound to this candidate."
            )
        return CandidateExplanation(
            candidate_id=candidate.id,
            candidate_type=(
                candidate.candidate_type.value
                if hasattr(candidate.candidate_type, "value")
                else str(candidate.candidate_type)
            ),
            label=label or candidate.explanation,
            confidence=(
                candidate.confidence.value
                if hasattr(candidate.confidence, "value")
                else str(candidate.confidence)
            ),
            score=candidate.score,
            supporting_evidence=len(supporting),
            contradicting_evidence=len(contradicting),
            neutral_evidence=len(neutral),
            reasons=reasons,
            supporting_quotes=[item.quote for item in supporting],
            contradicting_quotes=[item.quote for item in contradicting],
            score_breakdown=candidate.score_breakdown or {},
            uncertainty=uncertainty,
            why_this_confidence=CausalExplanationService.confidence_reason(candidate),
            limitations=limitations,
        )

    @staticmethod
    def confidence_reason(candidate: RootCauseCandidate) -> str:
        """One-line, evidence-counted reason for a candidate's confidence (§37)."""
        categories = sorted(
            {
                item.split("]")[0].lstrip("[")
                for item in (candidate.reasons or [])
                if isinstance(item, str) and item.startswith("[")
            }
        )
        if candidate.contradicting_evidence_count:
            return (
                f"{candidate.contradicting_evidence_count} contradicting fact(s) "
                f"weaken {candidate.supporting_evidence_count} supporting fact(s)"
            )
        if categories:
            return (
                f"{candidate.supporting_evidence_count} supporting fact(s) across "
                f"{len(categories)} evidence categor"
                f"{'y' if len(categories) == 1 else 'ies'}"
            )
        return "no structured evidence was recorded"

    @staticmethod
    def _limitations(
        *,
        primary: Optional[CandidateExplanation],
        relationships: Sequence[CausalRelationship],
        evidence: Sequence[CausalEvidence],
    ) -> list[str]:
        limitations: list[str] = []
        if primary is not None and primary.contradicting_evidence:
            limitations.append(
                "Contradicting evidence exists for the primary hypothesis and is "
                "shown rather than suppressed."
            )
        trace_facts = [e for e in evidence if e.category.value == "TRACE"]
        if not trace_facts:
            limitations.append(
                "No span-level or trace evidence was available: direction rests on "
                "temporal and structural evidence only."
            )
        correlation_only = [
            r
            for r in relationships
            if r.relationship_type is CausalRelationshipType.CORRELATES_WITH
        ]
        if correlation_only:
            limitations.append(
                f"{len(correlation_only)} relationship(s) are co-occurrence only "
                "(CORRELATES_WITH) and carry no direction."
            )
        limitations.append(
            "ARGUS has no direct causal instrumentation: conclusions remain "
            "evidence-supported, not instrumented end to end."
        )
        return limitations

    # -- edge explanations (§41) ---------------------------------------------
    def explain_relationship(
        self,
        relationship: CausalRelationship,
        evidence: Sequence[CausalEvidence],
    ) -> RelationshipExplanation:
        quotes = [item.quote for item in evidence if item.quote]
        caveats = list(relationship.contradiction_notes or [])
        if relationship.relationship_type is CausalRelationshipType.CORRELATES_WITH:
            caveats.append(
                "Recorded as co-occurrence: no evidence of direction was found, so "
                "no causal type is claimed."
            )
        if (
            relationship.temporal_alignment_seconds is not None
            and relationship.temporal_alignment_seconds < 0
        ):
            caveats.append(
                f"The target appears to have degraded "
                f"{-relationship.temporal_alignment_seconds}s before the source — "
                "a temporal contradiction."
            )
        return RelationshipExplanation(
            relationship_id=relationship.id,
            source_candidate_id=relationship.source_candidate_id,
            target_candidate_id=relationship.target_candidate_id,
            relationship_type=relationship.relationship_type.value,
            directional=relationship.relationship_type in DIRECTIONAL_TYPES,
            confidence=relationship.confidence.value,
            temporal_alignment_seconds=relationship.temporal_alignment_seconds,
            structural_support=relationship.structural_support,
            observational_support=relationship.observational_support,
            evidence_quotes=quotes,
            explanation=relationship.explanation,
            caveats=caveats,
        )

    # -- chain extraction (§32, §33) ------------------------------------------
    def extract_chain(
        self,
        *,
        candidates: Sequence[RootCauseCandidate],
        relationships: Sequence[CausalRelationship],
        primary_candidate_id: Optional[uuid.UUID],
        max_length: int = 6,
    ) -> list[CausalRelationship]:
        """Walk evidence-backed directed edges from the suspected origin.

        Only directional edges participate — a chain threaded through
        ``CORRELATES_WITH`` would imply direction nobody observed. Greedy by
        edge confidence then evidence count, and bounded in length.
        """
        by_id = {candidate.id: candidate for candidate in candidates}
        if primary_candidate_id is None or primary_candidate_id not in by_id:
            return []
        outgoing: dict[uuid.UUID, list[CausalRelationship]] = {}
        for relationship in relationships:
            if relationship.relationship_type not in DIRECTIONAL_TYPES:
                continue
            if relationship.source_candidate_id == relationship.target_candidate_id:
                continue
            outgoing.setdefault(relationship.source_candidate_id, []).append(
                relationship
            )
        for edges in outgoing.values():
            edges.sort(
                key=lambda r: (
                    0 if r.confidence.value == "HIGH" else 1,
                    -r.supporting_evidence_count,
                    str(r.target_candidate_id),
                )
            )

        chain: list[CausalRelationship] = []
        visited: set[uuid.UUID] = set()
        node = primary_candidate_id
        while len(chain) < max_length:
            visited.add(node)
            next_edges = [
                edge
                for edge in outgoing.get(node, [])
                if edge.target_candidate_id not in visited
            ]
            if not next_edges:
                break
            edge = next_edges[0]
            chain.append(edge)
            node = edge.target_candidate_id
        return chain


__all__ = [
    "CAUSAL_DISCLAIMER",
    "DIRECTIONAL_TYPES",
    "AnalysisExplanation",
    "CandidateExplanation",
    "CausalExplanationService",
    "LoadedAnalysis",
    "DeterministicExplanationProvider",
    "NullExplanationProvider",
    "RelationshipExplanation",
    "RootCauseExplanationProvider",
]
