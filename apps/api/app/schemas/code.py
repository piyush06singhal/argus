"""ARGUS Phase 6 — Code Intelligence & AI Debugger Schemas (§25–§31, §47–§52).

Three rules shape this surface:

1. **A claim and its validation are separate fields.** ``label`` says what ARGUS
   believes a location may be (and the strongest it will ever say is
   ``SUSPICIOUS_CODE_PATH``); ``validation`` says whether the location exists in
   the pinned snapshot. A response can therefore show "2 claimed, 1 verified"
   instead of quietly displaying a line number that was never checked.
2. **Every citation is a resolvable reference string**, ``FILE:path:10-20`` or
   ``CAUSAL_CANDIDATE:<uuid>``, so the frontend can make it clickable and the
   validator can make it falsifiable (§29, §30).
3. **Nothing executable crosses the boundary.** A client may ask for a
   repository to be indexed at a revision, or a question to be answered. It can
   never supply a command, a path outside the repository, or a provider
   parameter that reaches a process — this phase analyses, it never modifies
   (§66).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import Field, field_validator

from app.models.causal import ConfidenceLevel
from app.models.code import (
    CodeRelationshipType,
    CodeSymbolType,
    CodeVersionStatus,
    DebugAnalysisStatus,
    DebugMessageRole,
    DebugSessionStatus,
    EvidenceKind,
    HypothesisCategory,
    HypothesisValidationStatus,
    LocationValidation,
    ParseStatus,
    ReferenceKind,
    RiskSignalType,
    SnapshotStatus,
    ToolCallStatus,
    TraceMappingKind,
)
from app.models.deployment import RepositoryIndexStatus
from app.schemas.base import BaseSchema

DEBUGGER_ENGINE_VERSION = "phase6-debugger-v1"

#: The vocabulary of §2. ARGUS never reports a confirmed bug, because the
#: available validation cannot establish one.
LOCATION_LABELS = ("LIKELY_FAULT_LOCATION", "SUSPICIOUS_CODE_PATH")


# ---------------------------------------------------------------------------
# Repositories (§6, §8, §55)
# ---------------------------------------------------------------------------
class RepositoryCreateRequest(BaseSchema):
    provider: str = Field(
        "local",
        max_length=50,
        description="Repository provider: 'local' or 'git'.",
    )
    repository_url: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="Filesystem path or remote URL the provider can read.",
    )
    default_branch: Optional[str] = Field(default="main", max_length=255)
    language: Optional[str] = Field(default=None, max_length=32)
    last_indexed_commit: Optional[str] = Field(default=None, max_length=64)

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        allowed = {"local", "git"}
        if value not in allowed:
            raise ValueError(
                f"unsupported provider '{value}'; supported: {', '.join(sorted(allowed))}"
            )
        return value


class RepositoryResponse(BaseSchema):
    id: uuid.UUID
    project_id: uuid.UUID
    provider: str
    repository_url: str
    default_branch: str
    connection_status: str
    language: Optional[str] = None
    framework: Optional[str] = None
    index_status: RepositoryIndexStatus
    last_indexed_at: Optional[datetime] = None
    last_indexed_commit: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    #: Whether a usable snapshot exists, and at which revision.
    latest_snapshot_id: Optional[uuid.UUID] = None
    latest_commit_sha: Optional[str] = None
    snapshot_count: int = 0
    #: Stated so a client never has to guess: what this provider can and cannot do.
    capabilities: list[str] = Field(default_factory=list)


class RepositoryListResponse(BaseSchema):
    items: list[RepositoryResponse]
    total: int


class SnapshotResponse(BaseSchema):
    id: uuid.UUID
    project_id: uuid.UUID
    repository_id: uuid.UUID
    commit_sha: Optional[str] = None
    branch: Optional[str] = None
    commit_at: Optional[datetime] = None
    commit_message: Optional[str] = None
    commit_author: Optional[str] = None
    provider_name: str
    version_status: CodeVersionStatus
    #: How the revision was chosen — the §8 account, verbatim.
    version_evidence: Optional[str] = None
    status: SnapshotStatus
    indexed_at: Optional[datetime] = None
    file_count: int = 0
    symbol_count: int = 0
    languages: list[str] = Field(default_factory=list)
    error: Optional[str] = None
    created_at: datetime


class SnapshotListResponse(BaseSchema):
    items: list[SnapshotResponse]
    total: int


class IndexRequest(BaseSchema):
    reference: Optional[str] = Field(
        default=None,
        max_length=512,
        description=(
            "Revision to index. Omit to index the provider's current revision; "
            "for an incident prefer the deployment-derived revision."
        ),
    )
    incremental: bool = Field(
        default=True,
        description="Reuse files whose content hash is unchanged from the base snapshot.",
    )
    max_files: Optional[int] = Field(default=None, ge=1, le=50_000)


class IndexRunResponse(BaseSchema):
    id: uuid.UUID
    snapshot_id: uuid.UUID
    repository_id: uuid.UUID
    status: DebugAnalysisStatus
    trigger: Optional[str] = None
    incremental: bool = False
    base_commit_sha: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    files_seen: int = 0
    files_indexed: int = 0
    files_reused: int = 0
    files_added: int = 0
    files_modified: int = 0
    files_deleted: int = 0
    files_failed: int = 0
    symbols_indexed: int = 0
    references_indexed: int = 0
    relationships_indexed: int = 0
    #: Stated rather than hidden: JS/TS files are indexed by a structural scanner,
    #: not a full parser, and the count is reported so that is visible.
    files_heuristic: int = 0
    files_partial: int = 0
    errors: list[Any] = Field(default_factory=list)
    snapshot: Optional[SnapshotResponse] = None


class IndexResponse(BaseSchema):
    run: IndexRunResponse
    snapshot: SnapshotResponse
    #: Plain-language account of what the pass did and did not interpret.
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Code intelligence (§11–§14, §23, §44)
# ---------------------------------------------------------------------------
class SymbolResponse(BaseSchema):
    id: uuid.UUID
    snapshot_id: uuid.UUID
    file_path: str
    symbol_name: str
    qualified_name: str
    symbol_type: CodeSymbolType
    language: Optional[str] = None
    start_line: int
    end_line: int
    signature: Optional[str] = None
    documentation: Optional[str] = None
    is_async: bool = False
    complexity: Optional[int] = None
    route: Optional[str] = None
    http_method: Optional[str] = None
    component_id: Optional[uuid.UUID] = None
    #: Canonical reference for this definition (§29).
    reference: str
    caller_count: int = 0
    callee_count: int = 0
    signals: list["RiskSignalResponse"] = Field(default_factory=list)


class SymbolDetailResponse(SymbolResponse):
    source: Optional[str] = None
    callers: list["SymbolEdgeResponse"] = Field(default_factory=list)
    callees: list["SymbolEdgeResponse"] = Field(default_factory=list)
    related_files: list[str] = Field(default_factory=list)
    references: list["ReferenceResponse"] = Field(default_factory=list)


class SymbolEdgeResponse(BaseSchema):
    qualified_name: str
    file_path: str
    start_line: int
    end_line: int
    relationship: CodeRelationshipType
    confidence: float
    line: int = 0
    reference: str


class ReferenceResponse(BaseSchema):
    name: str
    file_path: str
    line: int
    kind: ReferenceKind
    resolved: bool = False
    symbol_id: Optional[uuid.UUID] = None


class SymbolListResponse(BaseSchema):
    items: list[SymbolResponse]
    total: int
    truncated: bool = False


class CodeFileResponse(BaseSchema):
    id: uuid.UUID
    path: str
    language: Optional[str] = None
    module_name: Optional[str] = None
    size_bytes: int = 0
    line_count: int = 0
    is_test: bool = False
    parse_status: ParseStatus
    parse_error: Optional[str] = None
    last_commit_sha: Optional[str] = None
    last_modified_at: Optional[datetime] = None
    last_author: Optional[str] = None
    symbol_count: int = 0


class CodeFileListResponse(BaseSchema):
    items: list[CodeFileResponse]
    total: int
    truncated: bool = False


class CodeSearchResponse(BaseSchema):
    snapshot_id: uuid.UUID
    query: str
    #: Kept apart on purpose: "a definition is named timeout" and "the word
    #: timeout appears in this function" are different findings.
    symbols: list[SymbolResponse] = Field(default_factory=list)
    source_matches: list[SymbolResponse] = Field(default_factory=list)
    references: list[ReferenceResponse] = Field(default_factory=list)
    truncated: bool = False


class RiskSignalResponse(BaseSchema):
    id: uuid.UUID
    signal_type: RiskSignalType
    value: float
    unit: Optional[str] = None
    file_path: str
    symbol_id: Optional[uuid.UUID] = None
    detail: Optional[str] = None
    observed_at: datetime
    #: Named explicitly: these are investigation signals, never a bug score (§44).
    interpretation: str = "investigation signal, not a defect assessment"


class RiskSignalListResponse(BaseSchema):
    items: list[RiskSignalResponse]
    total: int
    #: How many signals each type contributed, so an empty list is explainable.
    by_type: dict[str, int] = Field(default_factory=dict)


class SnapshotSummaryResponse(BaseSchema):
    snapshot: SnapshotResponse
    files: int = 0
    symbols: int = 0
    relationships: int = 0
    references: int = 0
    tests: int = 0
    languages: dict[str, int] = Field(default_factory=dict)
    framework: Optional[str] = None
    signals_by_type: dict[str, int] = Field(default_factory=dict)
    #: Stated so a caller knows what the index can and cannot answer.
    limitations: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Trace → code (§15–§17)
# ---------------------------------------------------------------------------
class TraceMappingResponse(BaseSchema):
    id: uuid.UUID
    snapshot_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    operation: Optional[str] = None
    service_name: Optional[str] = None
    endpoint: Optional[str] = None
    http_method: Optional[str] = None
    mapping_kind: TraceMappingKind
    symbol_id: Optional[uuid.UUID] = None
    file_path: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    confidence: float = 0.0
    evidence: Optional[str] = None
    #: Present exactly when the mapping failed, so "unmapped" is never silent.
    unmapped_reason: Optional[str] = None
    reference: Optional[str] = None


class TraceMappingListResponse(BaseSchema):
    incident_id: uuid.UUID
    snapshot_id: Optional[uuid.UUID] = None
    items: list[TraceMappingResponse]
    total: int
    mapped: int = 0
    unmapped: int = 0
    #: Reasons tallied, so an incident with no mapping says why.
    unmapped_reasons: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Change history (§18, §19, §20)
# ---------------------------------------------------------------------------
class CommitResponse(BaseSchema):
    sha: str
    short_sha: str
    author: Optional[str] = None
    committed_at: Optional[datetime] = None
    #: Metadata only — never read as a statement of intent (§18).
    message: Optional[str] = None
    files_changed: int = 0
    parents: list[str] = Field(default_factory=list)


class HistoryResponse(BaseSchema):
    repository_id: uuid.UUID
    revision: Optional[str] = None
    path: Optional[str] = None
    items: list[CommitResponse]
    truncated: bool = False
    reason: Optional[str] = None


class DiffEntryResponse(BaseSchema):
    path: str
    status: str
    old_path: Optional[str] = None


class DiffResponse(BaseSchema):
    repository_id: uuid.UUID
    base: Optional[str] = None
    head: Optional[str] = None
    items: list[DiffEntryResponse]
    truncated: bool = False
    reason: Optional[str] = None


class BlameLineResponse(BaseSchema):
    line: int
    commit_sha: Optional[str] = None
    author: Optional[str] = None
    committed_at: Optional[datetime] = None


class BlameResponse(BaseSchema):
    repository_id: uuid.UUID
    path: str
    items: list[BlameLineResponse]
    truncated: bool = False
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Debug sessions (§34–§36, §47–§52)
# ---------------------------------------------------------------------------
class DebugSessionCreateRequest(BaseSchema):
    title: Optional[str] = Field(default=None, max_length=512)
    repository_id: Optional[uuid.UUID] = None
    snapshot_id: Optional[uuid.UUID] = None
    created_by: Optional[str] = Field(default=None, max_length=255)
    #: Run the first analysis immediately. Refused when no snapshot can be
    #: resolved, because a session without a pinned revision cannot claim code.
    run_analysis: bool = True
    #: Index the deployment-derived revision before analysing (slow but thorough).
    index_snapshot: bool = False


class DebugCodeLocationResponse(BaseSchema):
    id: uuid.UUID
    file_path: str
    symbol_name: Optional[str] = None
    symbol_id: Optional[uuid.UUID] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    label: str
    reason: str = ""
    confidence: ConfidenceLevel
    validation: LocationValidation
    validation_detail: Optional[str] = None
    evidence_refs: list[str] = Field(default_factory=list)
    reference: str = ""
    #: False when ``validation`` is anything but VALID; the frontend must not
    #: render an unverified location as a finding.
    displayable: bool = False


class DebugEvidenceResponse(BaseSchema):
    id: uuid.UUID
    kind: EvidenceKind
    polarity: str
    reference: str
    label: Optional[str] = None
    source_table: Optional[str] = None
    source_id: Optional[uuid.UUID] = None
    quote: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    component_id: Optional[uuid.UUID] = None
    valid: bool = False
    validation_error: Optional[str] = None
    strength: float = 0.0
    observed_at: Optional[datetime] = None


class DebugHypothesisResponse(BaseSchema):
    id: uuid.UUID
    description: str
    category: HypothesisCategory
    confidence: ConfidenceLevel
    validation_status: HypothesisValidationStatus
    rationale: Optional[str] = None
    testable: bool = False
    test_approach: Optional[str] = None
    recurrence_count: int = 0
    locations: list[DebugCodeLocationResponse] = Field(default_factory=list)
    supporting_evidence: list[DebugEvidenceResponse] = Field(default_factory=list)
    contradicting_evidence: list[DebugEvidenceResponse] = Field(default_factory=list)


class DebugMessageResponse(BaseSchema):
    id: uuid.UUID
    role: DebugMessageRole
    content: str
    created_by: Optional[str] = None
    evidence_refs: list[str] = Field(default_factory=list)
    metadata: Optional[dict] = None
    created_at: datetime


class DebugToolCallResponse(BaseSchema):
    id: uuid.UUID
    tool_name: str
    arguments: Optional[dict] = None
    status: ToolCallStatus
    result_summary: Optional[str] = None
    result_count: Optional[int] = None
    result_bytes: Optional[int] = None
    truncated: bool = False
    error: Optional[str] = None
    started_at: datetime
    duration_ms: Optional[int] = None


class DebugAssistantAnswer(BaseSchema):
    message_id: uuid.UUID
    answer: str
    evidence: list[str] = Field(default_factory=list)
    invalid_references: list[dict] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    confidence: ConfidenceLevel
    tool_calls: list[dict] = Field(default_factory=list)
    degraded_reason: Optional[str] = None
    budget: dict = Field(default_factory=dict)


class DebugAskRequest(BaseSchema):
    question: str = Field(
        ...,
        min_length=3,
        max_length=4000,
        description="A question about this incident, answered only from stored evidence.",
    )
    asked_by: Optional[str] = Field(default=None, max_length=255)


class DebugSessionResponse(BaseSchema):
    id: uuid.UUID
    project_id: uuid.UUID
    incident_id: uuid.UUID
    repository_id: Optional[uuid.UUID] = None
    snapshot_id: Optional[uuid.UUID] = None
    title: Optional[str] = None
    status: DebugSessionStatus
    created_by: Optional[str] = None
    version_status: CodeVersionStatus
    version_note: Optional[str] = None
    context_version: str = "1"
    summary: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class DebugSessionDetailResponse(DebugSessionResponse):
    snapshot: Optional[SnapshotResponse] = None
    repository: Optional[RepositoryResponse] = None
    latest_analysis: Optional["DebugAnalysisResponse"] = None
    locations: list[DebugCodeLocationResponse] = Field(default_factory=list)
    hypotheses: list[DebugHypothesisResponse] = Field(default_factory=list)
    messages: list[DebugMessageResponse] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class DebugSessionListResponse(BaseSchema):
    items: list[DebugSessionResponse]
    total: int


class DebugAnalysisResponse(BaseSchema):
    id: uuid.UUID
    session_id: uuid.UUID
    snapshot_id: Optional[uuid.UUID] = None
    status: DebugAnalysisStatus
    kind: str = "incident_analysis"
    provider_name: Optional[str] = None
    model_name: Optional[str] = None
    prompt_version: str = "1"
    context_version: str = "1"
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    tool_call_count: int = 0
    files_accessed: int = 0
    context_bytes: Optional[int] = None
    confidence: ConfidenceLevel
    summary: Optional[str] = None
    #: Claims the validator refused (§30) — returned, never hidden.
    invalid_references: list[dict] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    recommended_inspections: list[str] = Field(default_factory=list)
    #: True when the model failed and the deterministic investigation is all there is (§43).
    degraded: bool = False
    degraded_reason: Optional[str] = None
    locations: list[DebugCodeLocationResponse] = Field(default_factory=list)
    hypotheses: list[DebugHypothesisResponse] = Field(default_factory=list)
    evidence: list[DebugEvidenceResponse] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class DebugTimelineEvent(BaseSchema):
    at: datetime
    kind: str
    title: str
    detail: Optional[str] = None
    reference: Optional[str] = None


class DebugTimelineResponse(BaseSchema):
    session_id: uuid.UUID
    incident_id: uuid.UUID
    items: list[DebugTimelineEvent]
    #: Ready-made caveats so an incomplete timeline explains itself.
    notes: list[str] = Field(default_factory=list)


class InvestigationResponse(BaseSchema):
    """The deterministic investigation (§43), always available.

    Returned whether or not a model ran, so an engineer never sees "AI
    unavailable, nothing can be determined" as the whole answer.
    """

    session_id: uuid.UUID
    incident_id: uuid.UUID
    snapshot_id: Optional[uuid.UUID] = None
    version_status: CodeVersionStatus
    version_note: Optional[str] = None
    context_version: str = "1"
    built_at: Optional[datetime] = None
    summary: str = ""
    evidence: list[dict] = Field(default_factory=list)
    sections: dict = Field(default_factory=dict)
    caveats: list[str] = Field(default_factory=list)
    budget: Optional[dict] = None
    redaction: Optional[dict] = None


class DebuggerMetricsResponse(BaseSchema):
    """Coverage tallies — what the debugger can and cannot currently answer."""

    sessions: int = 0
    sessions_completed: int = 0
    analyses: int = 0
    analyses_degraded: int = 0
    hypotheses: int = 0
    by_validation_status: dict[str, int] = Field(default_factory=dict)
    locations_claimed: int = 0
    locations_valid: int = 0
    locations_rejected: int = 0
    invalid_references: int = 0
    tool_calls: int = 0
    tool_calls_refused: int = 0
    repositories: int = 0
    snapshots: int = 0
    index_status: dict[str, int] = Field(default_factory=dict)
    engine_version: str = DEBUGGER_ENGINE_VERSION
    limitations: list[str] = Field(default_factory=list)


DebugSessionDetailResponse.model_rebuild()


__all__ = [
    "BlameLineResponse",
    "BlameResponse",
    "CodeFileListResponse",
    "CodeFileResponse",
    "CodeSearchResponse",
    "CommitResponse",
    "DEBUGGER_ENGINE_VERSION",
    "DebugAnalysisResponse",
    "DebugAskRequest",
    "DebugAssistantAnswer",
    "DebugCodeLocationResponse",
    "DebugEvidenceResponse",
    "DebugHypothesisResponse",
    "DebugMessageResponse",
    "DebugSessionCreateRequest",
    "DebugSessionDetailResponse",
    "DebugSessionListResponse",
    "DebugSessionResponse",
    "DebugTimelineEvent",
    "DebugTimelineResponse",
    "DebugToolCallResponse",
    "DebuggerMetricsResponse",
    "DiffEntryResponse",
    "DiffResponse",
    "HistoryResponse",
    "IndexRequest",
    "IndexResponse",
    "IndexRunResponse",
    "InvestigationResponse",
    "LOCATION_LABELS",
    "ReferenceResponse",
    "RepositoryCreateRequest",
    "RepositoryListResponse",
    "RepositoryResponse",
    "RiskSignalListResponse",
    "RiskSignalResponse",
    "SnapshotListResponse",
    "SnapshotResponse",
    "SnapshotSummaryResponse",
    "SymbolDetailResponse",
    "SymbolEdgeResponse",
    "SymbolListResponse",
    "SymbolResponse",
    "TraceMappingListResponse",
    "TraceMappingResponse",
]
