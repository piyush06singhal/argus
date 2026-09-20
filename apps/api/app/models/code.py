"""ARGUS Code Intelligence & AI Debugger Models (Phase 6).

Phase 6 connects **production evidence to source code**. It answers "which code
was executing when this failed, what changed recently in it, and what does the
evidence actually support?" — without ever letting an AI model invent a file, a
symbol, a line range, a commit, or a reason.

The epistemic boundaries are enforced in *structure*, not prose:

* **A snapshot is mandatory.** Every code row carries a non-nullable
  ``snapshot_id`` (or ``snapshot_id`` + ``repository_id``). An incident is
  analysed against the exact tree that was deployed — "whatever is on the
  branch today" is expressed as a *different* snapshot with a recorded
  ``version_status`` of ``UNKNOWN``, never as a silent substitution (§7, §8).
* **Definition ≠ reference ≠ relationship.** ``CodeSymbol`` is a definition,
  ``CodeReference`` is a syntactic *occurrence* of a name at a location, and
  ``CodeRelationship`` is a *resolved* semantic edge between two symbols. They
  are three tables because "a name appears here" and "this function calls that
  function" are different claims with different failure modes.
* **A location is not a bug.** ``DebugCodeLocation`` stores a suspected location
  together with its ``validation`` outcome: a location whose file, symbol or
  line range does not exist in the analysed snapshot is stored as ``INVALID_*``
  rather than displayed. Fabricated references are *visible* as rejected
  evidence, which is what makes the hallucination guard auditable (§26, §30, §31).
* **A hypothesis is never a conclusion.** ``DebugHypothesis.validation_status``
  starts ``UNVERIFIED``; only deterministic evidence validation moves it, and the
  model has no value meaning "confirmed bug" (§2).
* **An AI answer is data.** ``DebugAnalysisRun`` records the provider, model,
  prompt/context version, tool calls and validation failures alongside the
  output, so any displayed claim can be traced back to how it was produced (§59).

Code intelligence extends the Phase 2 knowledge graph rather than replacing it:
``CodeRelationship.component_id`` and ``TraceCodeMapping.component_id`` point at
the same ``system_components`` rows the software graph uses, so a code edge and a
service edge meet in one graph.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, Guid as UUID, JSONType

#: Bounded confidence buckets are reused from Phase 4 (same PG enum type) so a
#: debugging confidence and a causal confidence are on one scale and cannot
#: drift apart. Evidence polarity is reused for the same reason.
from app.models.causal import ConfidenceLevel, EvidencePolarity

if TYPE_CHECKING:
    from app.models.incident import Incident


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class RepositoryIndexStatus(str, enum.Enum):
    """Indexing state of a repository (§6, §54).

    ``STALE`` is distinct from ``PENDING`` on purpose: a repository that was
    indexed at commit A and has since moved to commit B still has a *usable*
    index (for snapshot A) and must not be treated as unindexed.
    """

    PENDING = "PENDING"
    INDEXING = "INDEXING"
    INDEXED = "INDEXED"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    FAILED = "FAILED"


class SnapshotStatus(str, enum.Enum):
    """Lifecycle of one indexed source snapshot (§7)."""

    CREATED = "CREATED"
    INDEXING = "INDEXING"
    READY = "READY"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class CodeVersionStatus(str, enum.Enum):
    """How confidently the deployed revision was resolved (§8).

    ``UNKNOWN`` is a first-class outcome. When the deployed commit cannot be
    determined, the analysis proceeds against the best available snapshot and
    says so — it never silently substitutes the current branch.
    """

    RESOLVED = "RESOLVED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class ParseStatus(str, enum.Enum):
    """Outcome of parsing one file (§11)."""

    PENDING = "PENDING"
    PARSED = "PARSED"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"


class CodeSymbolType(str, enum.Enum):
    """What kind of definition a symbol is (§12)."""

    MODULE = "MODULE"
    CLASS = "CLASS"
    FUNCTION = "FUNCTION"
    METHOD = "METHOD"
    INTERFACE = "INTERFACE"
    VARIABLE = "VARIABLE"
    CONSTANT = "CONSTANT"
    ROUTE = "ROUTE"
    HANDLER = "HANDLER"
    MODEL = "MODEL"
    QUERY = "QUERY"
    SERVICE = "SERVICE"
    UNKNOWN = "UNKNOWN"


class ReferenceKind(str, enum.Enum):
    """The syntactic form of a reference occurrence (§14)."""

    CALL = "CALL"
    IMPORT = "IMPORT"
    ATTRIBUTE = "ATTRIBUTE"
    DEFINITION = "DEFINITION"
    TYPE = "TYPE"
    DECORATOR = "DECORATOR"
    ROUTE = "ROUTE"
    QUERY = "QUERY"
    UNKNOWN = "UNKNOWN"


class CodeRelationshipType(str, enum.Enum):
    """Resolved, directed edges of the *code* graph (§13).

    These extend the Phase 2 software graph: ``CALLS`` is a code-level fact
    ("this function invokes that one"), which is a different statement from the
    Phase 2 ``CALLS`` edge between components built from production traces.
    Keeping them in their own table with their own provenance prevents a
    static-analysis guess from being read as an observed call.
    """

    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    DEFINES = "DEFINES"
    IMPLEMENTS = "IMPLEMENTS"
    INHERITS = "INHERITS"
    READS = "READS"
    WRITES = "WRITES"
    QUERIES = "QUERIES"
    CALLS_API = "CALLS_API"
    HANDLES_ROUTE = "HANDLES_ROUTE"
    THROWS = "THROWS"
    CATCHES = "CATCHES"


class TraceMappingKind(str, enum.Enum):
    """How a span was connected to code (§15)."""

    ROUTE = "ROUTE"
    STACK_FRAME = "STACK_FRAME"
    SERVICE = "SERVICE"
    SYMBOL = "SYMBOL"
    OPERATION = "OPERATION"
    UNMAPPED = "UNMAPPED"


class LocationValidation(str, enum.Enum):
    """Whether a claimed code location exists in the analysed snapshot (§30, §31).

    Every value other than ``VALID`` and ``UNVERIFIED`` means the location was
    *rejected*: it is retained (so the rejection is auditable) but never shown
    as a finding.
    """

    VALID = "VALID"
    UNVERIFIED = "UNVERIFIED"
    INVALID_FILE = "INVALID_FILE"
    INVALID_SYMBOL = "INVALID_SYMBOL"
    INVALID_LINE_RANGE = "INVALID_LINE_RANGE"
    WRONG_SNAPSHOT = "WRONG_SNAPSHOT"


class RiskSignalType(str, enum.Enum):
    """Deterministic investigation signals (§44).

    Named ``*_SIGNAL``-shaped on purpose: none of these is a "bug score". Each
    one is a measured property of the code that makes a location worth
    inspecting, and the scorer never combines them into a quality judgement.
    """

    RECENTLY_MODIFIED = "RECENTLY_MODIFIED"
    HIGH_COMPLEXITY = "HIGH_COMPLEXITY"
    HIGH_FAN_IN = "HIGH_FAN_IN"
    HIGH_FAN_OUT = "HIGH_FAN_OUT"
    FREQUENTLY_FAILING = "FREQUENTLY_FAILING"
    FREQUENTLY_CHANGED = "FREQUENTLY_CHANGED"
    ERROR_PRONE_PATH = "ERROR_PRONE_PATH"
    DEPENDENCY_BOUNDARY = "DEPENDENCY_BOUNDARY"
    DATABASE_OPERATION = "DATABASE_OPERATION"
    EXTERNAL_API_CALL = "EXTERNAL_API_CALL"


class DebugSessionStatus(str, enum.Enum):
    """Lifecycle of a debugging session (§34)."""

    CREATED = "CREATED"
    CONTEXT_BUILDING = "CONTEXT_BUILDING"
    ANALYZING = "ANALYZING"
    WAITING_FOR_VALIDATION = "WAITING_FOR_VALIDATION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DebugAnalysisStatus(str, enum.Enum):
    """Outcome of one analysis run (§42, §43).

    ``DEGRADED`` is the important one: the model failed, but deterministic
    context was still assembled and returned. It exists so "AI unavailable" can
    never be presented as "nothing could be determined".
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    LIMIT_REACHED = "LIMIT_REACHED"


class DebugMessageRole(str, enum.Enum):
    """Who produced a message in a debug session (§35)."""

    ENGINEER = "ENGINEER"
    ARGUS = "ARGUS"
    SYSTEM = "SYSTEM"


class HypothesisCategory(str, enum.Enum):
    """Why-kind buckets for a debugging hypothesis (§27).

    ``UNKNOWN`` is a valid answer: the model is never forced to pick a category
    the evidence does not support.
    """

    INCORRECT_ERROR_HANDLING = "INCORRECT_ERROR_HANDLING"
    TIMEOUT_CONFIGURATION = "TIMEOUT_CONFIGURATION"
    RETRY_LOGIC = "RETRY_LOGIC"
    RESOURCE_EXHAUSTION = "RESOURCE_EXHAUSTION"
    DATABASE_QUERY = "DATABASE_QUERY"
    CONCURRENCY = "CONCURRENCY"
    RACE_CONDITION = "RACE_CONDITION"
    INVALID_STATE = "INVALID_STATE"
    INPUT_HANDLING = "INPUT_HANDLING"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    CONFIGURATION = "CONFIGURATION"
    API_CONTRACT = "API_CONTRACT"
    DATA_CONSISTENCY = "DATA_CONSISTENCY"
    UNKNOWN = "UNKNOWN"


class HypothesisValidationStatus(str, enum.Enum):
    """How far the stored evidence actually carries a hypothesis (§2, §28).

    There is deliberately no ``CONFIRMED`` value. The strongest state is
    ``SUPPORTED`` — "the available evidence is consistent with this and nothing
    we hold contradicts it" — which is a statement about ARGUS's evidence, not
    about the world.
    """

    UNVERIFIED = "UNVERIFIED"
    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    WEAKENED = "WEAKENED"
    REFUTED = "REFUTED"
    INVALID_REFERENCE = "INVALID_REFERENCE"


class EvidenceKind(str, enum.Enum):
    """What a debugging evidence row points at (§29)."""

    INCIDENT_EVIDENCE = "INCIDENT_EVIDENCE"
    ANOMALY = "ANOMALY"
    TRACE = "TRACE"
    SPAN = "SPAN"
    LOG = "LOG"
    METRIC = "METRIC"
    DEPLOYMENT = "DEPLOYMENT"
    CONFIGURATION_CHANGE = "CONFIGURATION_CHANGE"
    COMMIT = "COMMIT"
    FILE = "FILE"
    SYMBOL = "SYMBOL"
    REPRODUCTION = "REPRODUCTION"
    CAUSAL_ANALYSIS = "CAUSAL_ANALYSIS"
    CAUSAL_CANDIDATE = "CAUSAL_CANDIDATE"
    GRAPH_EDGE = "GRAPH_EDGE"
    RISK_SIGNAL = "RISK_SIGNAL"
    RECURRENCE = "RECURRENCE"
    MISSING = "MISSING"


class ToolCallStatus(str, enum.Enum):
    """Outcome of one read-only tool invocation (§37, §39)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    TRUNCATED = "TRUNCATED"


# ---------------------------------------------------------------------------
# RepositorySnapshot — the pinned tree every code claim is made against
# ---------------------------------------------------------------------------
class RepositorySnapshot(BaseModel):
    """One immutable, indexed view of a repository at a specific revision (§7).

    Snapshots are never mutated in place: re-indexing a *different* commit
    creates a new row, which is what makes an old incident's analysis
    reproducible months later.
    """

    __tablename__ = "repository_snapshots"
    __table_args__ = (
        Index("ix_repo_snapshots_repo_commit", "repository_id", "commit_sha"),
        Index("ix_repo_snapshots_project_status", "project_id", "status"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("code_repositories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    commit_sha: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    branch: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    #: When the revision itself was authored/committed, if known.
    commit_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    commit_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    commit_author: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    #: Provider-relative reference (git ref, workbook revision, …).
    reference: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    #: Where the tree was materialized for reading (absolute path of the
    #: checkout/working copy). Nullable because a provider may serve content
    #: without ever materializing a tree.
    root_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    provider_name: Mapped[str] = mapped_column(
        String(50), nullable=False, default="local"
    )

    version_status: Mapped[CodeVersionStatus] = mapped_column(
        SAEnum(CodeVersionStatus, name="codeversionstatus"),
        nullable=False,
        default=CodeVersionStatus.UNKNOWN,
    )
    #: Human-readable explanation of how the revision was resolved (§8) —
    #: e.g. "deployment 8f2c… recorded commit abc123" or "no deployment matched;
    #: fell back to default branch HEAD".
    version_evidence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[SnapshotStatus] = mapped_column(
        SAEnum(SnapshotStatus, name="snapshotstatus"),
        nullable=False,
        default=SnapshotStatus.CREATED,
        index=True,
    )
    indexed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    symbol_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    languages: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    index_metadata: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    files: Mapped[List["CodeFile"]] = relationship(
        "CodeFile", back_populates="snapshot", cascade="all, delete-orphan"
    )
    symbols: Mapped[List["CodeSymbol"]] = relationship(
        "CodeSymbol", back_populates="snapshot", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# CodeIndexRun — one (possibly incremental) indexing pass (§54, §55)
# ---------------------------------------------------------------------------
class CodeIndexRun(BaseModel):
    """Audit record for one indexing pass.

    Stored separately from the snapshot because a snapshot may be indexed more
    than once (incrementally, as commits land) and the *history* of what each
    pass did — added/modified/deleted files, failures — is the thing an operator
    needs when a symbol is unexpectedly missing.
    """

    __tablename__ = "code_index_runs"
    __table_args__ = (
        Index("ix_code_index_runs_snapshot_started", "snapshot_id", "started_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("code_repositories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("repository_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[DebugAnalysisStatus] = mapped_column(
        SAEnum(DebugAnalysisStatus, name="debuganalysisstatus"),
        nullable=False,
        default=DebugAnalysisStatus.PENDING,
    )
    #: What asked for the run: ``api``, ``worker``, ``seed``, ``watch``.
    trigger: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    requested_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    #: True when only the diff against ``base_commit_sha`` was re-parsed (§55).
    incremental: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    base_commit_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    files_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_indexed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_added: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_modified: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    symbols_indexed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    references_indexed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relationships_indexed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    errors: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    run_metadata: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)


# ---------------------------------------------------------------------------
# CodeFile
# ---------------------------------------------------------------------------
class CodeFile(BaseModel):
    """One source file inside a snapshot (§9, §12).

    ``content_hash`` is what makes incremental indexing correct: a file whose
    hash is unchanged needs no re-parse even if its mtime moved (§55).
    """

    __tablename__ = "code_files"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "path", name="uq_code_files_snapshot_path"),
        Index("ix_code_files_repo_path", "repository_id", "path"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("code_repositories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("repository_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    language: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, index=True
    )
    module_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    is_test: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    #: Set when the file was deleted from a later revision but its row (and
    #: symbols) remain valid for this historical snapshot.
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    parse_status: Mapped[ParseStatus] = mapped_column(
        SAEnum(ParseStatus, name="parsestatus"),
        nullable=False,
        default=ParseStatus.PENDING,
        index=True,
    )
    parse_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_commit_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_modified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_author: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    file_metadata: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    snapshot: Mapped["RepositorySnapshot"] = relationship(
        "RepositorySnapshot", back_populates="files"
    )
    symbols: Mapped[List["CodeSymbol"]] = relationship(
        "CodeSymbol", back_populates="file", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# CodeSymbol — a definition
# ---------------------------------------------------------------------------
class CodeSymbol(BaseModel):
    """One definition extracted from a file (§12).

    ``start_line``/``end_line`` are 1-based and inclusive and always come from a
    real parse of the real file: this is the anchor every "suspected location"
    is validated against, so a line range that does not exist in the snapshot is
    detectable rather than merely unlikely.
    """

    __tablename__ = "code_symbols"
    __table_args__ = (
        Index("ix_code_symbols_snapshot_name", "snapshot_id", "symbol_name"),
        Index("ix_code_symbols_snapshot_path", "snapshot_id", "file_path"),
        Index("ix_code_symbols_qualified", "snapshot_id", "qualified_name"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("code_repositories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("repository_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("code_files.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    file_path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    symbol_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    #: ``file.py:Class.method`` — stable, human-addressable identity used in
    #: evidence references and by the code viewer.
    qualified_name: Mapped[str] = mapped_column(
        String(1024), nullable=False, index=True
    )
    symbol_type: Mapped[CodeSymbolType] = mapped_column(
        SAEnum(CodeSymbolType, name="codesymboltype"),
        nullable=False,
        default=CodeSymbolType.UNKNOWN,
        index=True,
    )
    language: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    signature: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Full source text of the definition, redacted before it reaches a model
    #: (§57) but stored raw here so the code viewer shows real code.
    source: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    documentation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: sha256 of the symbol's own source — detects a definition changing even
    #: when the file's surrounding lines move (§55).
    symbol_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )

    parent_symbol_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("code_symbols.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_async: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Cyclomatic-ish count from the parser (branch points + 1). A *signal* used
    #: by the risk scorer, never a quality verdict (§44).
    complexity: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )
    #: Resolved component id when the symbol was matched to the Phase 2 graph.
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: Route metadata for ROUTE/HANDLER symbols (§15): "GET /checkout".
    route: Mapped[Optional[str]] = mapped_column(String(512), nullable=True, index=True)
    http_method: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    symbol_metadata: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    snapshot: Mapped["RepositorySnapshot"] = relationship(
        "RepositorySnapshot", back_populates="symbols"
    )
    file: Mapped[Optional["CodeFile"]] = relationship(
        "CodeFile", back_populates="symbols"
    )
    references: Mapped[List["CodeReference"]] = relationship(
        "CodeReference",
        back_populates="symbol",
        cascade="all, delete-orphan",
        foreign_keys="CodeReference.symbol_id",
    )


# ---------------------------------------------------------------------------
# CodeReference — an occurrence of a name at a location
# ---------------------------------------------------------------------------
class CodeReference(BaseModel):
    """One syntactic occurrence of a name (§14, §23).

    Unresolved references are stored with ``symbol_id = NULL``. That is
    deliberate: "we saw a call to ``reserve`` here but could not resolve which
    ``reserve``" is a real, useful partial fact, and dropping it would make the
    code graph look more complete than it is.
    """

    __tablename__ = "code_references"
    __table_args__ = (
        Index("ix_code_refs_snapshot_name", "snapshot_id", "name"),
        Index("ix_code_refs_enclosing", "enclosing_symbol_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("repository_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("code_files.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    file_path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    reference_kind: Mapped[ReferenceKind] = mapped_column(
        SAEnum(ReferenceKind, name="referencekind"),
        nullable=False,
        default=ReferenceKind.UNKNOWN,
        index=True,
    )
    line: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    column: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: The symbol *containing* this occurrence (the caller, for a call site).
    enclosing_symbol_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("code_symbols.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: The resolved target definition, when resolution succeeded.
    symbol_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("code_symbols.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    resolution: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reference_metadata: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    symbol: Mapped[Optional["CodeSymbol"]] = relationship(
        "CodeSymbol", back_populates="references", foreign_keys=[symbol_id]
    )


# ---------------------------------------------------------------------------
# CodeRelationship — a resolved edge in the code graph
# ---------------------------------------------------------------------------
class CodeRelationship(BaseModel):
    """One resolved, typed, directed edge between two symbols (§13).

    Static analysis is *not* observation. ``evidence`` records how the edge was
    derived (``import-alias``, ``unique-name-match``, ``route-decorator`` …) and
    ``confidence`` how certain that derivation is, so a call-graph guess can
    never be presented with the authority of a production trace.
    """

    __tablename__ = "code_relationships"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "source_symbol_id",
            "target_symbol_id",
            "relationship_type",
            "line",
            name="uq_code_relationship_edge",
        ),
        Index("ix_code_rel_source_type", "source_symbol_id", "relationship_type"),
        Index("ix_code_rel_target_type", "target_symbol_id", "relationship_type"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("repository_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_symbol_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("code_symbols.id", ondelete="CASCADE"), nullable=False
    )
    target_symbol_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("code_symbols.id", ondelete="CASCADE"), nullable=False
    )

    relationship_type: Mapped[CodeRelationshipType] = mapped_column(
        SAEnum(CodeRelationshipType, name="coderelationshiptype"),
        nullable=False,
        index=True,
    )
    #: Line in ``source_symbol_id``'s file where the edge originates.
    line: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    evidence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Optional bridge to the Phase 2 software graph — set when both ends resolve
    #: to one known component, so code and service topology meet in one place.
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    relationship_metadata: Mapped[Optional[dict]] = mapped_column(
        JSONType, nullable=True
    )


# ---------------------------------------------------------------------------
# TraceCodeMapping — span → code (§15)
# ---------------------------------------------------------------------------
class TraceCodeMapping(BaseModel):
    """One connection between a production span and a source location (§15).

    Mappings carry their ``confidence`` and their ``kind`` because a route match
    and a stack-frame match are not equally strong, and the debugging context
    must be able to say which one it is relying on.
    """

    __tablename__ = "trace_code_mappings"
    __table_args__ = (
        Index("ix_trace_code_mappings_trace", "trace_id"),
        Index("ix_trace_code_mappings_span", "span_id"),
        Index("ix_trace_code_mappings_project", "project_id", "created_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("repository_snapshots.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    trace_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    span_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    operation: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    service_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    endpoint: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True, index=True
    )
    http_method: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    mapping_kind: Mapped[TraceMappingKind] = mapped_column(
        SAEnum(TraceMappingKind, name="tracemappingkind"),
        nullable=False,
        default=TraceMappingKind.UNMAPPED,
        index=True,
    )
    symbol_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("code_symbols.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    file_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    start_line: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    end_line: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Why a mapping failed is as useful as the mapping: ``no-route-metadata``,
    #: ``no-stack-trace``, ``symbol-not-found``, ``file-not-indexed`` (§63).
    unmapped_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    mapping_metadata: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)


# ---------------------------------------------------------------------------
# CodeRiskSignal — deterministic investigation signals (§44)
# ---------------------------------------------------------------------------
class CodeRiskSignal(BaseModel):
    """One measured signal about a file or symbol (§44).

    Never combined into a score by the model layer: the debugger surfaces the
    individual signals and their measured values so an engineer can weigh them.
    """

    __tablename__ = "code_risk_signals"
    __table_args__ = (
        Index("ix_code_risk_signals_snapshot_type", "snapshot_id", "signal_type"),
        Index("ix_code_risk_signals_symbol", "symbol_id", "signal_type"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("repository_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    symbol_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("code_symbols.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)

    signal_type: Mapped[RiskSignalType] = mapped_column(
        SAEnum(RiskSignalType, name="risksignaltype"), nullable=False, index=True
    )
    value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unit: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    signal_metadata: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)


# ---------------------------------------------------------------------------
# DebugSession — one investigation (§34, §36)
# ---------------------------------------------------------------------------
class DebugSession(BaseModel):
    """One engineering investigation of one incident (§34).

    The session pins its snapshot: a follow-up question three days later is
    answered against the same tree the first analysis used, unless the engineer
    explicitly opens a new session.
    """

    __tablename__ = "debug_sessions"
    __table_args__ = (
        Index("ix_debug_sessions_incident", "incident_id", "created_at"),
        Index("ix_debug_sessions_project_status", "project_id", "status"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    repository_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("code_repositories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("repository_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: The Phase 4 analysis this session reasons over, when one exists.
    causal_analysis_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("causal_analyses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: The Phase 5 experiment whose reproduction evidence the session can cite.
    experiment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("reproduction_experiments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    status: Mapped[DebugSessionStatus] = mapped_column(
        SAEnum(DebugSessionStatus, name="debugsessionstatus"),
        nullable=False,
        default=DebugSessionStatus.CREATED,
        index=True,
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    #: Which snapshot the session locked onto, and how confident that resolution
    #: was — copied from the snapshot so the UI needs no join to be honest (§8).
    version_status: Mapped[CodeVersionStatus] = mapped_column(
        SAEnum(CodeVersionStatus, name="codeversionstatus", create_constraint=False),
        nullable=False,
        default=CodeVersionStatus.UNKNOWN,
    )
    version_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Bumped whenever the context builder's selection rules change, so an old
    #: analysis can be identified as produced by an older context policy (§59).
    context_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="1"
    )
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    session_metadata: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    incident: Mapped["Incident"] = relationship("Incident")
    messages: Mapped[List["DebugMessage"]] = relationship(
        "DebugMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="DebugMessage.created_at",
    )
    hypotheses: Mapped[List["DebugHypothesis"]] = relationship(
        "DebugHypothesis", back_populates="session", cascade="all, delete-orphan"
    )
    analysis_runs: Mapped[List["DebugAnalysisRun"]] = relationship(
        "DebugAnalysisRun", back_populates="session", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# DebugAnalysisRun — one model call, fully audited (§42, §59)
# ---------------------------------------------------------------------------
class DebugAnalysisRun(BaseModel):
    """One AI-debugging run, including everything needed to audit it (§59).

    The row exists whether or not the model succeeded: a ``DEGRADED`` run stores
    the deterministic context that was assembled so the response is still useful,
    and a ``FAILED`` run stores the error so "the AI was down" is a recorded
    fact rather than a support ticket.
    """

    __tablename__ = "debug_analysis_runs"
    __table_args__ = (
        Index("ix_debug_analysis_runs_session", "session_id", "started_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("debug_sessions.id", ondelete="CASCADE"), nullable=False
    )
    repository_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("code_repositories.id", ondelete="SET NULL"), nullable=True
    )
    snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("repository_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[DebugAnalysisStatus] = mapped_column(
        SAEnum(
            DebugAnalysisStatus, name="debuganalysisstatus", create_constraint=False
        ),
        nullable=False,
        default=DebugAnalysisStatus.PENDING,
        index=True,
    )
    #: What was asked: ``incident_analysis`` or ``question``.
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="incident_analysis"
    )
    provider_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    model_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    context_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="1"
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_accessed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Prompt/response sizes, so context bloat is measurable (§64).
    context_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    confidence: Mapped[ConfidenceLevel] = mapped_column(
        SAEnum(ConfidenceLevel, name="confidencelevel", create_constraint=False),
        nullable=False,
        default=ConfidenceLevel.INSUFFICIENT,
    )
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Claims the validator rejected because they pointed at something that does
    #: not exist (§30). Stored, never hidden.
    invalid_references: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    missing_evidence: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    recommended_inspections: Mapped[Optional[list]] = mapped_column(
        JSONType, nullable=True
    )
    #: The deterministic context, always stored: it is the fallback payload (§43).
    context_snapshot: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    #: Redaction report: how many secrets were removed before the model saw it.
    redaction_report: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    run_metadata: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    session: Mapped["DebugSession"] = relationship(
        "DebugSession", back_populates="analysis_runs"
    )
    tool_calls: Mapped[List["DebugToolCall"]] = relationship(
        "DebugToolCall", back_populates="analysis_run", cascade="all, delete-orphan"
    )
    code_locations: Mapped[List["DebugCodeLocation"]] = relationship(
        "DebugCodeLocation", back_populates="analysis_run", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# DebugMessage — the session conversation (§35, §36)
# ---------------------------------------------------------------------------
class DebugMessage(BaseModel):
    """One turn in a debugging conversation.

    Messages are scoped to a session and carry the analysis run that produced
    them, so a follow-up answer can always be traced to the context and the tool
    calls behind it (§36: no unrelated conversation leaks in, because there is
    nowhere for it to be stored).
    """

    __tablename__ = "debug_messages"
    __table_args__ = (
        Index("ix_debug_messages_session_created", "session_id", "created_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("debug_sessions.id", ondelete="CASCADE"), nullable=False
    )
    analysis_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("debug_analysis_runs.id", ondelete="SET NULL"), nullable=True
    )

    role: Mapped[DebugMessageRole] = mapped_column(
        SAEnum(DebugMessageRole, name="debugmessagerole"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    #: Canonical evidence references cited by this message, as structured rows
    #: rather than prose — this is what makes the references clickable (§29).
    evidence_refs: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    message_metadata: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    session: Mapped["DebugSession"] = relationship(
        "DebugSession", back_populates="messages"
    )


# ---------------------------------------------------------------------------
# DebugHypothesis (§27, §28)
# ---------------------------------------------------------------------------
class DebugHypothesis(BaseModel):
    """One debugging hypothesis, with the evidence that carries it.

    ``validation_status`` is computed from the stored evidence rows by the
    evidence validator (§28), never set by the model: the model proposes, the
    stored evidence decides how much weight the hypothesis has.
    """

    __tablename__ = "debug_hypotheses"
    __table_args__ = (Index("ix_debug_hypotheses_session", "session_id", "created_at"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("debug_sessions.id", ondelete="CASCADE"), nullable=False
    )
    analysis_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("debug_analysis_runs.id", ondelete="SET NULL"), nullable=True
    )

    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[HypothesisCategory] = mapped_column(
        SAEnum(HypothesisCategory, name="hypothesiscategory"),
        nullable=False,
        default=HypothesisCategory.UNKNOWN,
        index=True,
    )
    confidence: Mapped[ConfidenceLevel] = mapped_column(
        SAEnum(ConfidenceLevel, name="confidencelevel", create_constraint=False),
        nullable=False,
        default=ConfidenceLevel.INSUFFICIENT,
    )
    validation_status: Mapped[HypothesisValidationStatus] = mapped_column(
        SAEnum(HypothesisValidationStatus, name="hypothesisvalidationstatus"),
        nullable=False,
        default=HypothesisValidationStatus.UNVERIFIED,
        index=True,
    )
    #: Deterministic explanation of *why* the status is what it is.
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Whether a hypothesis can be tested by the Phase 5 engine, and how.
    testable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    test_approach: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Set when this hypothesis reproduces an earlier investigation's finding
    #: (§46) — a recurrence, not a defect claim.
    recurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hypothesis_metadata: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    session: Mapped["DebugSession"] = relationship(
        "DebugSession", back_populates="hypotheses"
    )
    evidence: Mapped[List["DebugEvidence"]] = relationship(
        "DebugEvidence", back_populates="hypothesis", cascade="all, delete-orphan"
    )
    locations: Mapped[List["DebugCodeLocation"]] = relationship(
        "DebugCodeLocation", back_populates="hypothesis", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# DebugCodeLocation — a suspected location, validated (§26, §30, §31)
# ---------------------------------------------------------------------------
class DebugCodeLocation(BaseModel):
    """One claimed code location and the result of validating it.

    The validation columns are the anti-hallucination mechanism: a location is
    only ``VALID`` if the file exists in the analysed snapshot, the named symbol
    exists in it, and the line range lies inside that file. Anything else is
    retained with the reason it failed, so the frontend can show "1 claimed
    location rejected — file not in snapshot abc123" instead of quietly
    displaying a fabricated line number.
    """

    __tablename__ = "debug_code_locations"
    __table_args__ = (
        Index("ix_debug_code_locations_analysis", "analysis_run_id"),
        Index("ix_debug_code_locations_symbol", "symbol_id"),
        Index("ix_debug_code_locations_file", "snapshot_id", "file_path"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("debug_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    analysis_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("debug_analysis_runs.id", ondelete="CASCADE"), nullable=True
    )
    hypothesis_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("debug_hypotheses.id", ondelete="CASCADE"), nullable=True
    )
    snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("repository_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    symbol_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("code_symbols.id", ondelete="SET NULL"), nullable=True
    )

    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    symbol_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    start_line: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    end_line: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    #: LIKELY_FAULT_LOCATION / SUSPICIOUS_CODE_PATH — the §2 vocabulary, kept as
    #: a string so the wording can be refined without a migration.
    label: Mapped[str] = mapped_column(
        String(64), nullable=False, default="SUSPICIOUS_CODE_PATH"
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[ConfidenceLevel] = mapped_column(
        SAEnum(ConfidenceLevel, name="confidencelevel", create_constraint=False),
        nullable=False,
        default=ConfidenceLevel.INSUFFICIENT,
    )
    validation: Mapped[LocationValidation] = mapped_column(
        SAEnum(LocationValidation, name="locationvalidation"),
        nullable=False,
        default=LocationValidation.UNVERIFIED,
        index=True,
    )
    validation_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    location_metadata: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    hypothesis: Mapped[Optional["DebugHypothesis"]] = relationship(
        "DebugHypothesis", back_populates="locations"
    )
    analysis_run: Mapped[Optional["DebugAnalysisRun"]] = relationship(
        "DebugAnalysisRun", back_populates="code_locations"
    )


# ---------------------------------------------------------------------------
# DebugEvidence (§28, §29)
# ---------------------------------------------------------------------------
class DebugEvidence(BaseModel):
    """One factual item cited by an analysis, grounded in a stored row (§28, §29).

    ``reference`` holds the canonical, clickable form (``FILE:checkout/service.py:142-168``,
    ``REPRODUCTION:<uuid>``, ``COMMIT:abc123``). ``valid`` records whether the
    reference resolved against stored data at analysis time, and
    ``validation_error`` why it did not — the model may not smuggle in a fact
    that never existed (§30).
    """

    __tablename__ = "debug_evidence"
    __table_args__ = (
        Index("ix_debug_evidence_session_kind", "session_id", "kind"),
        Index("ix_debug_evidence_hypothesis", "hypothesis_id"),
        Index("ix_debug_evidence_ref", "reference"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("debug_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    analysis_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("debug_analysis_runs.id", ondelete="CASCADE"), nullable=True
    )
    hypothesis_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("debug_hypotheses.id", ondelete="CASCADE"), nullable=True
    )

    kind: Mapped[EvidenceKind] = mapped_column(
        SAEnum(EvidenceKind, name="evidencekind"), nullable=False, index=True
    )
    polarity: Mapped[EvidencePolarity] = mapped_column(
        SAEnum(EvidencePolarity, name="evidencepolarity", create_constraint=False),
        nullable=False,
        default=EvidencePolarity.NEUTRAL,
        index=True,
    )
    #: Canonical reference string, e.g. ``FILE:app/x.py:10-20``.
    reference: Mapped[str] = mapped_column(String(1024), nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    #: The stored record this evidence came from, when it maps to one.
    source_table: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), nullable=True, index=True
    )
    #: Quoted fact (what the row says), redacted for display safety.
    quote: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Code excerpt for FILE/SYMBOL evidence, from the pinned snapshot.
    snippet: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    start_line: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    end_line: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("system_components.id", ondelete="SET NULL"), nullable=True
    )

    valid: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    validation_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: 0..1 strength within its kind (deterministic; documented in docs/phase-6.md).
    strength: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    observed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    evidence_metadata: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    hypothesis: Mapped[Optional["DebugHypothesis"]] = relationship(
        "DebugHypothesis", back_populates="evidence"
    )


# ---------------------------------------------------------------------------
# DebugToolCall — the bounded, read-only tool interface (§37–§39, §59)
# ---------------------------------------------------------------------------
class DebugToolCall(BaseModel):
    """One tool invocation by the debugger, with its result size and status.

    Stored for three reasons: limits can be enforced *across* turns (a session
    cannot accumulate unbounded reads), a rejected call is auditable, and the UI
    can show exactly which files the AI looked at.
    """

    __tablename__ = "debug_tool_calls"
    __table_args__ = (Index("ix_debug_tool_calls_run", "analysis_run_id"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("debug_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    analysis_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("debug_analysis_runs.id", ondelete="CASCADE"), nullable=True
    )

    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    arguments: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    status: Mapped[ToolCallStatus] = mapped_column(
        SAEnum(ToolCallStatus, name="toolcallstatus"),
        nullable=False,
        default=ToolCallStatus.PENDING,
        index=True,
    )
    #: Compact, human-readable result summary (never the full payload).
    result_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    result_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tool_metadata: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    analysis_run: Mapped[Optional["DebugAnalysisRun"]] = relationship(
        "DebugAnalysisRun", back_populates="tool_calls"
    )
