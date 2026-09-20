"""ARGUS Phase 4 — Causal Analysis Schemas (§6–§8, §34, §37).

Every response carries its own epistemic status: ``confidence`` buckets, the
evidence counts behind them, and an explicit ``limitations`` block. The API
never returns a bare probability or a single confident "root cause" — the
primary candidate is ``UNKNOWN`` unless the evidence justified selection, and
``CORRELATES_WITH`` edges are labelled as correlation all the way to the UI.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import Field

from app.models.causal import (
    AnalysisStatus,
    CandidateStatus,
    CandidateType,
    CausalEvidenceCategory,
    CausalRelationshipType,
    ConfidenceLevel,
    EvidencePolarity,
)
from app.schemas.base import BaseSchema, IDMixin, TimestampMixin

ANALYSIS_ENGINE_VERSION = "phase4-causal-v1"


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------
class CausalEvidenceResponse(IDMixin, TimestampMixin, BaseSchema):
    """One stored fact bound to a candidate or a specific edge."""

    analysis_id: uuid.UUID
    candidate_id: Optional[uuid.UUID] = None
    relationship_id: Optional[uuid.UUID] = None
    category: CausalEvidenceCategory
    polarity: EvidencePolarity
    source_table: str
    source_id: Optional[uuid.UUID] = None
    incident_evidence_id: Optional[uuid.UUID] = None
    quote: str
    explanation: str
    component_id: Optional[uuid.UUID] = None
    observed_at: Optional[datetime] = None
    strength: float


class CausalEvidenceList(BaseSchema):
    items: list[CausalEvidenceResponse]
    total: int


# ---------------------------------------------------------------------------
# Relationships (causal-graph edges)
# ---------------------------------------------------------------------------
class CausalRelationshipResponse(IDMixin, TimestampMixin, BaseSchema):
    """A directed hypothesis edge. ``CORRELATES_WITH`` is never a cause."""

    analysis_id: uuid.UUID
    source_candidate_id: uuid.UUID
    target_candidate_id: uuid.UUID
    relationship_type: CausalRelationshipType
    confidence: ConfidenceLevel
    supporting_evidence_count: int
    contradicting_evidence_count: int
    temporal_alignment_seconds: Optional[int] = None
    #: 0/1/2 — none / indirect / direct structural support from the Phase 2 graph.
    structural_support: int
    #: 0/1 — stored traces show a call between the components.
    observational_support: int
    contradiction_notes: Optional[list] = None
    explanation: str


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------
class RootCauseCandidateResponse(IDMixin, TimestampMixin, BaseSchema):
    """A hypothesis — never a conclusion. Score and confidence are separate."""

    analysis_id: uuid.UUID
    component_id: Optional[uuid.UUID] = None
    component_name: Optional[str] = None
    event_id: Optional[uuid.UUID] = None
    event_kind: Optional[str] = None
    candidate_type: CandidateType
    status: CandidateStatus
    score: float
    confidence: ConfidenceLevel
    is_external: bool
    first_observed_at: Optional[datetime] = None
    supporting_evidence_count: int
    contradicting_evidence_count: int
    neutral_evidence_count: int
    explanation: Optional[str] = None
    #: Score components, each documented — no black-box number (§25).
    score_breakdown: Optional[dict] = None
    reasons: Optional[list] = None
    uncertainty: Optional[dict] = None


class RootCauseCandidateList(BaseSchema):
    items: list[RootCauseCandidateResponse]
    total: int


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
class CausalAnalysisResponse(IDMixin, TimestampMixin, BaseSchema):
    """One versioned analysis run (§6, §35)."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    incident_id: uuid.UUID
    analysis_version: int
    status: AnalysisStatus
    trigger: Optional[str] = None
    requested_by: Optional[str] = None
    analysis_version_tag: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    overall_confidence: ConfidenceLevel
    primary_candidate_id: Optional[uuid.UUID] = None
    summary: Optional[str] = None
    #: Evidence the analysis looked for and did not find (§30 honesty).
    missing_evidence: Optional[list] = None
    analysis_metadata: Optional[dict] = None


class CausalAnalysisDetailResponse(CausalAnalysisResponse):
    """Full analysis payload: candidates, edges and per-candidate evidence."""

    candidates: list[RootCauseCandidateResponse] = Field(default_factory=list)
    relationships: list[CausalRelationshipResponse] = Field(default_factory=list)
    evidence: list[CausalEvidenceResponse] = Field(default_factory=list)


class CausalGraphResponse(BaseSchema):
    """The incident's causal graph in a renderable shape (§9, §40).

    Edge semantics are explicit so the UI can render correlation differently
    from causal hypotheses without re-deriving anything.
    """

    analysis_id: uuid.UUID
    analysis_version: int
    overall_confidence: ConfidenceLevel
    primary_candidate_id: Optional[uuid.UUID] = None
    nodes: list[RootCauseCandidateResponse]
    edges: list[CausalRelationshipResponse]
    #: Static disclaimer rendered with every graph (§2, §40).
    disclaimer: str = (
        "Edges are evidence-supported hypotheses, not proven causation. "
        "CORRELATES_WITH edges indicate co-occurrence only."
    )


class HypothesisResponse(BaseSchema):
    """One alternative hypothesis with its full evidence story (§28)."""

    candidate: RootCauseCandidateResponse
    supporting: list[CausalEvidenceResponse] = Field(default_factory=list)
    contradicting: list[CausalEvidenceResponse] = Field(default_factory=list)
    neutral: list[CausalEvidenceResponse] = Field(default_factory=list)
    why_confidence_differs: Optional[str] = None


class HypothesesResponse(BaseSchema):
    """All plausible hypotheses for the incident, primary first."""

    analysis_id: uuid.UUID
    analysis_version: int
    primary_candidate_id: Optional[uuid.UUID] = None
    overall_confidence: ConfidenceLevel
    items: list[HypothesisResponse]


class CausalChainLinkResponse(BaseSchema):
    """One arrow in a causal chain — every arrow is auditable (§32)."""

    source_candidate_id: uuid.UUID
    target_candidate_id: uuid.UUID
    relationship_type: CausalRelationshipType
    confidence: ConfidenceLevel
    temporal_alignment_seconds: Optional[int] = None
    evidence_count: int
    explanation: str


class CausalChainResponse(BaseSchema):
    """An ordered causal chain (§32, §33) or the reason none was built."""

    analysis_id: uuid.UUID
    #: Ordered candidate ids from suspected origin to observed symptoms.
    chain: list[CausalChainLinkResponse] = Field(default_factory=list)
    candidate_ids: list[uuid.UUID] = Field(default_factory=list)
    valid: bool
    validation_notes: list[str] = Field(default_factory=list)


class EvidenceAnalysisResponse(BaseSchema):
    """Per-candidate evidence split, the heart of explainability (§24, §37)."""

    analysis_id: uuid.UUID
    candidates: list[HypothesisResponse]
    missing_evidence: Optional[list] = None


class AnalyzeRequest(BaseSchema):
    """Request a (re-)analysis. Triggers are audited (§35, §44)."""

    requested_by: Optional[str] = Field(None, max_length=255)
    trigger: str = Field("manual", max_length=255)


class AnalyzeResponse(BaseSchema):
    """Result of triggering an analysis (idempotent semantics, §34)."""

    analysis_id: uuid.UUID
    incident_id: uuid.UUID
    analysis_version: int
    status: AnalysisStatus
    reused: bool = Field(
        False,
        description=(
            "True when an up-to-date completed analysis existed and was "
            "returned instead of running a new pass"
        ),
    )


class AnalysisHistoryItem(BaseSchema):
    """A previous analysis version, for the history view (§43)."""

    analysis_id: uuid.UUID
    analysis_version: int
    status: AnalysisStatus
    overall_confidence: ConfidenceLevel
    primary_candidate_id: Optional[uuid.UUID] = None
    primary_candidate_summary: Optional[str] = None
    candidate_count: int
    supporting_evidence_count: int
    contradicting_evidence_count: int
    started_at: datetime
    completed_at: Optional[datetime] = None
    trigger: Optional[str] = None
    requested_by: Optional[str] = None
    #: What changed vs the previous version (§43): new evidence, confidence
    #: changes, new contradictions.
    diff: Optional[dict] = None


class AnalysisHistoryResponse(BaseSchema):
    items: list[AnalysisHistoryItem]
    total: int
