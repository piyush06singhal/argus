"""ARGUS Causal Analysis Models (Phase 4).

Phase 4 turns Phase 3's *correlation* into **evidence-supported hypotheses**.
The boundary that defines the whole phase:

* Causal analysis produces **hypotheses with evidence**, never conclusions.
* ``CORRELATES_WITH`` is a distinct relationship type from the causal ones —
  the model makes it impossible to blur "things that happened together" with
  "things one of which did to the other".
* Every relationship and every candidate must be explainable from stored rows.
* ``UNKNOWN`` is a first-class candidate: the system is allowed to say
  "insufficient evidence" instead of inventing an answer.

Concepts are deliberately kept apart (§2 of the phase spec):

======================  =====================================================
Concept                 Where it lives here
======================  =====================================================
Observed fact           the evidence rows Phase 1–3 already store
Correlation             ``CausalRelationshipType.CORRELATES_WITH``
Temporal association    ``CausalEvidenceCategory.TEMPORAL`` (support *or* contra)
Structural relationship ``structural_support`` / ``DEPENDENCY`` evidence
Causal evidence         ``CausalRelationshipType.LIKELY_CAUSE`` etc. + strength
Hypothesis              ``RootCauseCandidate``
Conclusion              never — ARGUS does not conclude
======================  =====================================================
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from app.models.base import BaseModel, Guid as UUID, JSONType
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.incident import Incident
    from app.models.system import SystemComponent


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class AnalysisStatus(str, enum.Enum):
    """Lifecycle of one causal-analysis run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CandidateType(str, enum.Enum):
    """What kind of thing a root-cause candidate is.

    Never assumed before analysis: the candidate generator assigns the type
    from the evidence that produced the candidate, and a candidate that no
    evidence pins down stays ``UNKNOWN``.
    """

    DEPLOYMENT = "DEPLOYMENT"
    CONFIGURATION_CHANGE = "CONFIGURATION_CHANGE"
    APPLICATION_COMPONENT = "APPLICATION_COMPONENT"
    DATABASE = "DATABASE"
    EXTERNAL_DEPENDENCY = "EXTERNAL_DEPENDENCY"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    RESOURCE_EXHAUSTION = "RESOURCE_EXHAUSTION"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    DATA_ISSUE = "DATA_ISSUE"
    UNKNOWN = "UNKNOWN"


class CandidateStatus(str, enum.Enum):
    """Lifecycle of a candidate within one analysis."""

    HYPOTHESIS = "HYPOTHESIS"
    UNDER_EVALUATION = "UNDER_EVALUATION"
    SUPPORTED = "SUPPORTED"
    WEAKENED = "WEAKENED"
    REFUTED = "REFUTED"
    WITHDRAWN = "WITHDRAWN"


class CausalRelationshipType(str, enum.Enum):
    """Relationship kinds in the per-incident causal graph.

    ``CORRELATES_WITH`` exists so that "observed together" can be stated
    without implying direction — it must never be used when any evidence of
    direction exists, and causal types must never be used when only
    co-occurrence is known.
    """

    POSSIBLE_CAUSE = "POSSIBLE_CAUSE"
    LIKELY_CAUSE = "LIKELY_CAUSE"
    DOWNSTREAM_EFFECT = "DOWNSTREAM_EFFECT"
    CONTRIBUTES_TO = "CONTRIBUTES_TO"
    BLOCKS = "BLOCKS"
    TRIGGERS = "TRIGGERS"
    AMPLIFIES = "AMPLIFIES"
    CORRELATES_WITH = "CORRELATES_WITH"


class CausalEvidenceCategory(str, enum.Enum):
    """Which analysis lens produced a piece of causal evidence (§23)."""

    TEMPORAL = "TEMPORAL"
    TRACE = "TRACE"
    DEPENDENCY = "DEPENDENCY"
    CHANGE = "CHANGE"
    METRIC = "METRIC"
    LOG = "LOG"
    HEALTH = "HEALTH"
    RESOURCE = "RESOURCE"
    CONFIGURATION = "CONFIGURATION"
    DEPLOYMENT = "DEPLOYMENT"
    RECOVERY = "RECOVERY"
    CONTRADICTING = "CONTRADICTING"


class EvidencePolarity(str, enum.Enum):
    """Whether an evidence item supports, contradicts, or is neutral."""

    SUPPORTING = "SUPPORTING"
    CONTRADICTING = "CONTRADICTING"
    NEUTRAL = "NEUTRAL"


class ConfidenceLevel(str, enum.Enum):
    """Coarse, honest confidence buckets (§27).

    Criteria (enforced by the scorer, not just prose):

    * ``HIGH`` — multiple *independent* evidence categories align on one chain.
    * ``MEDIUM`` — several categories align but direct causal evidence
      (traces) is incomplete or the chain has gaps.
    * ``LOW`` — only temporal/structural evidence exists.
    * ``INSUFFICIENT`` — evidence is contradictory or too sparse to rank.
    """

    INSUFFICIENT = "INSUFFICIENT"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# ---------------------------------------------------------------------------
# CausalAnalysis — one versioned run of the engine over one incident
# ---------------------------------------------------------------------------
class CausalAnalysis(BaseModel):
    """One versioned causal-analysis run for an incident (§6, §35, §44).

    Re-analysis creates a **new** row with ``analysis_version + 1``; previous
    versions are preserved so confidence changes are auditable over time.
    """

    __tablename__ = "causal_analyses"
    __table_args__ = (
        Index("ix_causal_analyses_incident_version", "incident_id", "analysis_version"),
        Index("ix_causal_analyses_project", "project_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("environments.id", ondelete="CASCADE"), nullable=True
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )

    analysis_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[AnalysisStatus] = mapped_column(
        SAEnum(AnalysisStatus, name="analysisstatus"),
        nullable=False,
        default=AnalysisStatus.PENDING,
    )
    #: What triggered this run: "manual", "api", "worker", "new_evidence", ...
    trigger: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    #: Who/what requested it (actor string, as in Phase 3 lifecycle routes).
    requested_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    #: The analysis-engine version that produced this row (audit §44).
    analysis_version_tag: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    overall_confidence: Mapped[ConfidenceLevel] = mapped_column(
        SAEnum(ConfidenceLevel, name="confidencelevel"),
        nullable=False,
        default=ConfidenceLevel.INSUFFICIENT,
    )
    primary_candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), nullable=True
    )
    #: Why the primary candidate (or UNKNOWN) was selected — always stored.
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Evidence the analysis *wanted* but did not find (§ "missing evidence").
    missing_evidence: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    #: Reproducibility: bounded inputs used (windows, limits, candidate counts).
    analysis_metadata: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    incident: Mapped["Incident"] = relationship("Incident")
    candidates: Mapped[List["RootCauseCandidate"]] = relationship(
        back_populates="analysis",
        cascade="all, delete-orphan",
        order_by="RootCauseCandidate.score.desc(), RootCauseCandidate.created_at",
    )


# ---------------------------------------------------------------------------
# RootCauseCandidate — one hypothesis
# ---------------------------------------------------------------------------
class RootCauseCandidate(BaseModel):
    """A hypothesised contributor to the incident (§7, §28).

    A candidate is a *hypothesis*, never a conclusion. It carries both a
    relative ``score`` (among this analysis' candidates) and a coarse
    ``confidence`` — a candidate can rank first among weak candidates while
    overall confidence stays low, and the model keeps those facts separate.
    """

    __tablename__ = "root_cause_candidates"
    __table_args__ = (
        Index("ix_rcc_analysis_score", "analysis_id", "score"),
        Index("ix_rcc_component", "component_id"),
    )

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("causal_analyses.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("system_components.id", ondelete="SET NULL"), nullable=True
    )
    #: For change candidates: the deployment/configuration row itself.
    event_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(), nullable=True)
    event_kind: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    candidate_type: Mapped[CandidateType] = mapped_column(
        SAEnum(CandidateType, name="candidatetype"),
        nullable=False,
        default=CandidateType.UNKNOWN,
    )
    status: Mapped[CandidateStatus] = mapped_column(
        SAEnum(CandidateStatus, name="candidatestatus"),
        nullable=False,
        default=CandidateStatus.HYPOTHESIS,
    )

    #: Relative ranking among this analysis' candidates (0..1, deterministic).
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence: Mapped[ConfidenceLevel] = mapped_column(
        SAEnum(ConfidenceLevel, name="confidencelevel"),
        nullable=False,
        default=ConfidenceLevel.INSUFFICIENT,
    )

    #: Component-level attributes
    is_external: Mapped[bool] = mapped_column(nullable=False, default=False)
    first_observed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    supporting_evidence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    contradicting_evidence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    neutral_evidence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    #: Why this candidate exists at all — always evidence-quoted, never vibes.
    explanation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Score breakdown: every component and its value, for the UI (§25, §37).
    score_breakdown: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    #: Deterministic reasons list for the explanation API (§37).
    reasons: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    #: What would strengthen or weaken this hypothesis (honest uncertainty).
    uncertainty: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    analysis: Mapped["CausalAnalysis"] = relationship(back_populates="candidates")
    component: Mapped[Optional["SystemComponent"]] = relationship("SystemComponent")
    evidence: Mapped[List["CausalEvidence"]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
        order_by="CausalEvidence.strength.desc()",
    )
    outgoing_relationships: Mapped[List["CausalRelationship"]] = relationship(
        foreign_keys="CausalRelationship.source_candidate_id",
        back_populates="source_candidate",
        cascade="all, delete-orphan",
    )
    incoming_relationships: Mapped[List["CausalRelationship"]] = relationship(
        foreign_keys="CausalRelationship.target_candidate_id",
        back_populates="target_candidate",
        cascade="all, delete-orphan",
    )


# ---------------------------------------------------------------------------
# CausalEvidence — a stored observation bound to a candidate (§23, §24)
# ---------------------------------------------------------------------------
class CausalEvidence(BaseModel):
    """One piece of evidence for/against a candidate.

    Every row points at a **real stored record** (anomaly, trace, span,
    deployment, configuration change, graph edge, incident evidence) —
    ``source_table``/``source_id`` make fabrication structurally impossible,
    and ``quote`` stores the human-readable fact as the engine found it.
    """

    __tablename__ = "causal_evidence"
    __table_args__ = (
        Index("ix_causal_evidence_candidate_polarity", "candidate_id"),
        Index("ix_causal_evidence_source", "source_table", "source_id"),
    )

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("causal_analyses.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("root_cause_candidates.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    category: Mapped[CausalEvidenceCategory] = mapped_column(
        SAEnum(CausalEvidenceCategory, name="causalevidencecategory"), nullable=False
    )
    polarity: Mapped[EvidencePolarity] = mapped_column(
        SAEnum(EvidencePolarity, name="evidencepolarity"), nullable=False, index=True
    )

    #: Provenance — where this fact physically lives.
    source_table: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(), nullable=True)
    #: Optional anchor to the incident's own evidence rows (Phase 3).
    incident_evidence_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("incident_evidence.id", ondelete="SET NULL"), nullable=True
    )
    #: Set when this evidence justifies a specific relationship *edge* rather
    #: than only its candidate — this is what makes every graph edge auditable
    #: back to stored facts.
    relationship_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("causal_relationships.id", ondelete="CASCADE"), nullable=True
    )

    #: What the record says (short, quoted/factual, no interpretation).
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    #: Why this fact matters *for or against* this candidate.
    explanation: Mapped[str] = mapped_column(Text, nullable=False)

    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("system_components.id", ondelete="SET NULL"), nullable=True
    )
    observed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: 0..1 evidence strength within its category (deterministic, documented).
    strength: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    candidate: Mapped["RootCauseCandidate"] = relationship(back_populates="evidence")


# ---------------------------------------------------------------------------
# CausalRelationship — a hypothesis about how two candidates relate (§8, §32)
# ---------------------------------------------------------------------------
class CausalRelationship(BaseModel):
    """One directed edge in the incident's causal graph.

    Every edge must be explainable: ``evidence_ids`` reference the candidate-
    bound evidence rows that justify it, and an edge with no supporting
    evidence must not exist. ``CORRELATES_WITH`` marks co-occurrence with no
    direction evidence — visually and semantically distinct from cause edges.
    """

    __tablename__ = "causal_relationships"
    __table_args__ = (
        Index("ix_causal_rel_analysis", "analysis_id"),
        Index(
            "ix_causal_rel_pair",
            "source_candidate_id",
            "target_candidate_id",
            "relationship_type",
            unique=True,
        ),
    )

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("causal_analyses.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    source_candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("root_cause_candidates.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("root_cause_candidates.id", ondelete="CASCADE"),
        nullable=False,
    )

    relationship_type: Mapped[CausalRelationshipType] = mapped_column(
        SAEnum(CausalRelationshipType, name="causalrelationshiptype"),
        nullable=False,
        index=True,
    )
    confidence: Mapped[ConfidenceLevel] = mapped_column(
        SAEnum(ConfidenceLevel, name="confidencelevel"),
        nullable=False,
        default=ConfidenceLevel.LOW,
    )

    #: Aggregates of the bound evidence, denormalised for the graph UI.
    supporting_evidence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    contradicting_evidence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    #: How well the two candidates line up in time (seconds; negative = effect
    #: apparently *before* cause, which is a contradiction, not a delay).
    temporal_alignment_seconds: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    #: Does the Phase 2 graph/dependency model connect these two? (0/1/2 =
    #: no/indirect/direct structural support.)
    structural_support: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Do stored traces show a parent→child (call) between them? (0/1)
    observational_support: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    #: Contradictions known for this edge (e.g. effect precedes cause).
    contradiction_notes: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)

    #: Why this edge exists — evidence-quoted, always.
    explanation: Mapped[str] = mapped_column(Text, nullable=False)

    source_candidate: Mapped["RootCauseCandidate"] = relationship(
        foreign_keys=[source_candidate_id], back_populates="outgoing_relationships"
    )
    target_candidate: Mapped["RootCauseCandidate"] = relationship(
        foreign_keys=[target_candidate_id], back_populates="incoming_relationships"
    )
    edge_evidence: Mapped[List["CausalEvidence"]] = relationship(
        foreign_keys="CausalEvidence.relationship_id",
        cascade="all, delete-orphan",
        order_by="CausalEvidence.strength.desc()",
    )
