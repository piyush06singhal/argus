"""ARGUS Phase 7 — Fix Generation & Verification Models (§7, §46).

The domain's spine is the distinction the execution prompt draws in §1:

    Fix Suggestion ≠ Valid Patch ≠ Verified Fix ≠ Production-Safe Deployment

Every table encodes one link of that chain, and every state transition that
matters is a stored column rather than an inference:

* ``FixHypothesis``      — the *idea*, bound to real debugging evidence
* ``Patch``              — the *artifact*: a unified diff with generation provenance
* ``PatchWorkspace``     — the *isolated experiment*: a temp git worktree + branch
* ``PatchVerificationRun`` — one attempt to verify a patch, ending in exactly
  one verdict (§34 levels) — never a bare "passed"
* ``PatchTestRun`` / ``PatchRegressionTest`` — the evidence rows a verdict cites
* ``PatchComparison``    — BASE vs PATCHED, numbers from real telemetry only (§31)
* ``PatchRiskAssessment``— deterministic risk with an explicit explanation (§38)
* ``PatchReviewAction``  — the human decisions; ARGUS never auto-approves (§41)
* ``PatchArtifact``      — hashed, immutable-after-completion files (§45)

Safety invariants carried by the schema itself:

* A patch row never contains credentials — only diffs; secrets are redacted
  before storage by the safety validator.
* ``PatchReviewAction`` is the *only* path to an ``APPROVED`` review state; no
  service sets it as a side effect of verification.
* Verification rows record the commands that ran (from the allowlisted
  registry) and their exit status — "tests passed" is never asserted without
  a row that says which tests, in which workspace, at which commit.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, Guid as UUID, JSONType


# ---------------------------------------------------------------------------
# Enums (§1, §7, §34, §38, §41)
# ---------------------------------------------------------------------------


class FixCategory(str, enum.Enum):
    """What kind of change the evidence supports (§6).

    ``UNKNOWN`` is a first-class value: forcing a category the evidence does
    not name would be inventing structure, and every downstream default
    (risk floor, required checks) would then be built on an invention.
    """

    BUG_FIX = "BUG_FIX"
    ERROR_HANDLING = "ERROR_HANDLING"
    TIMEOUT_FIX = "TIMEOUT_FIX"
    RETRY_FIX = "RETRY_FIX"
    VALIDATION_FIX = "VALIDATION_FIX"
    RESOURCE_HANDLING = "RESOURCE_HANDLING"
    CONCURRENCY_FIX = "CONCURRENCY_FIX"
    DATABASE_QUERY_FIX = "DATABASE_QUERY_FIX"
    API_CONTRACT_FIX = "API_CONTRACT_FIX"
    CONFIGURATION_FIX = "CONFIGURATION_FIX"
    DEPENDENCY_HANDLING = "DEPENDENCY_HANDLING"
    PERFORMANCE_FIX = "PERFORMANCE_FIX"
    UNKNOWN = "UNKNOWN"


class FixStatus(str, enum.Enum):
    """Lifecycle of a fix hypothesis (§5)."""

    DRAFT = "DRAFT"
    HYPOTHESIZED = "HYPOTHESIZED"
    PATCHING = "PATCHING"
    PATCH_GENERATED = "PATCH_GENERATED"
    GENERATION_FAILED = "GENERATION_FAILED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class PatchStatus(str, enum.Enum):
    """The §1 chain as stored states (§7).

    ``GENERATED`` means only that the AI produced it. ``VALID``-equivalent
    facts (parsed, scope-checked) live on the verification run; ``VERIFIED``
    is reachable *only* through a verification run whose evidence chain is
    complete. Nothing here implies deployability.
    """

    GENERATED = "GENERATED"
    PARSE_FAILED = "PARSE_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    APPLIED = "APPLIED"
    BUILD_FAILED = "BUILD_FAILED"
    TEST_FAILED = "TEST_FAILED"
    REPRODUCTION_FAILED = "REPRODUCTION_FAILED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"
    GENERATION_FAILED = "GENERATION_FAILED"


class PatchFormat(str, enum.Enum):
    """Supported patch encodings (§11)."""

    UNIFIED_DIFF = "UNIFIED_DIFF"


class RiskLevel(str, enum.Enum):
    """Deterministic risk buckets (§38). Never a probability."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class VerificationLevel(str, enum.Enum):
    """Ladder of evidence (§34), ordered weakest to strongest."""

    NONE = "NONE"
    STATIC_VALIDATED = "STATIC_VALIDATED"
    TEST_VALIDATED = "TEST_VALIDATED"
    REPRODUCTION_VALIDATED = "REPRODUCTION_VALIDATED"
    REGRESSION_VALIDATED = "REGRESSION_VALIDATED"
    FULLY_VERIFIED = "FULLY_VERIFIED"


class VerificationStatus(str, enum.Enum):
    """Terminal state of one verification run (§33, §63)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    VERIFIED = "VERIFIED"
    NOT_VERIFIED = "NOT_VERIFIED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class WorkspaceStatus(str, enum.Enum):
    """Lifecycle of a temporary git workspace (§18)."""

    CREATING = "CREATING"
    READY = "READY"
    PATCH_APPLIED = "PATCH_APPLIED"
    BUSY = "BUSY"
    DESTROYED = "DESTROYED"
    FAILED = "FAILED"


class ReviewAction(str, enum.Enum):
    """Human decisions (§41, §71). There is no AUTO value by design."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    REGENERATE = "REGENERATE"
    EXPORT = "EXPORT"


class ReviewState(str, enum.Enum):
    """Where the human decision stands for a patch (§41)."""

    AWAITING_REVIEW = "AWAITING_REVIEW"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class TestRunKind(str, enum.Enum):
    """Which stage a recorded test run belongs to (§24–§27)."""

    STATIC = "STATIC"
    BUILD = "BUILD"
    UNIT = "UNIT"
    INTEGRATION = "INTEGRATION"
    REGRESSION = "REGRESSION"
    FULL_SUITE = "FULL_SUITE"


class TamperingFlag(str, enum.Enum):
    """Test-tampering detections (§55). Each is independently disqualifying."""

    NONE = "NONE"
    TEST_DELETED = "TEST_DELETED"
    ASSERTION_WEAKENED = "ASSERTION_WEAKENED"
    TEST_SKIPPED = "TEST_SKIPPED"
    EXPECTATION_CHANGED = "EXPECTATION_CHANGED"
    LINT_DISABLED = "LINT_DISABLED"
    TYPECHECK_DISABLED = "TYPECHECK_DISABLED"
    CI_MODIFIED = "CI_MODIFIED"
    VERIFICATION_MODIFIED = "VERIFICATION_MODIFIED"


# ---------------------------------------------------------------------------
# FixHypothesis (§5)
# ---------------------------------------------------------------------------


class FixHypothesis(BaseModel):
    """A proposed fix, bound to the debugging evidence that motivates it (§5).

    A hypothesis exists only when real rows exist behind it: a debug session
    (and usually an analysis run), the incident, and — when Phase 5 produced
    one — a reproduction experiment whose behaviour the patch must change.
    ``scope_files`` is the §10 allowlist: the patch validator will reject any
    diff touching a path outside it.
    """

    __tablename__ = "fix_hypotheses"
    __table_args__ = (
        Index("ix_fix_hypotheses_incident", "incident_id", "status"),
        Index("ix_fix_hypotheses_project", "project_id", "status"),
        Index("ix_fix_hypotheses_session", "debug_session_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    debug_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("debug_sessions.id", ondelete="SET NULL"), nullable=True
    )
    analysis_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("debug_analysis_runs.id", ondelete="SET NULL"), nullable=True
    )
    root_cause_candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("root_cause_candidates.id", ondelete="SET NULL"),
        nullable=True,
    )
    reproduction_experiment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("reproduction_experiments.id", ondelete="SET NULL"),
        nullable=True,
    )
    repository_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("code_repositories.id", ondelete="SET NULL"), nullable=True
    )
    snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("repository_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_change: Mapped[str] = mapped_column(Text, nullable=False)
    expected_behavior: Mapped[str] = mapped_column(Text, nullable=True)
    category: Mapped[FixCategory] = mapped_column(
        SAEnum(FixCategory, name="fixcategory", create_constraint=False),
        nullable=False,
        default=FixCategory.UNKNOWN,
    )

    #: §10 — the allowlist. Relative paths inside the target repository only.
    scope_files: Mapped[List] = mapped_column(JSONType, nullable=False, default=list)
    #: §10 — never modifiable without explicit approval; enforced in code.
    excluded_paths: Mapped[List] = mapped_column(JSONType, nullable=False, default=list)

    #: Evidence that motivated the hypothesis: canonical references
    #: (``TRACE:…``, ``EXP-…``, ``E<n>``), so the UI can link each one.
    supporting_evidence: Mapped[List] = mapped_column(
        JSONType, nullable=False, default=list
    )
    target_symbols: Mapped[List] = mapped_column(JSONType, nullable=False, default=list)

    risk_level: Mapped[RiskLevel] = mapped_column(
        SAEnum(RiskLevel, name="risklevel", create_constraint=False),
        nullable=False,
        default=RiskLevel.MEDIUM,
    )
    confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="LOW")

    status: Mapped[FixStatus] = mapped_column(
        SAEnum(FixStatus, name="fixstatus", create_constraint=False),
        nullable=False,
        default=FixStatus.HYPOTHESIZED,
        index=True,
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    patches: Mapped[List["Patch"]] = relationship(
        back_populates="hypothesis", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FixHypothesis {self.id} {self.status.value} {self.title[:40]!r}>"


# ---------------------------------------------------------------------------
# Patch (§7)
# ---------------------------------------------------------------------------


class Patch(BaseModel):
    """One generated patch — a diff plus its provenance (§7).

    The content is a standard unified diff (§11) that has passed the parser
    *before* storage: an unparseable diff is recorded as ``PARSE_FAILED`` with
    the parser's reason, not stored raw and hoped over.
    """

    __tablename__ = "patches"
    __table_args__ = (
        Index("ix_patches_hypothesis", "fix_hypothesis_id", "status"),
        Index("ix_patches_project", "project_id", "status"),
        Index("ix_patches_experiment", "patch_experiment_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    fix_hypothesis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("fix_hypotheses.id", ondelete="CASCADE"), nullable=False
    )
    #: Phase 7's own experiment grouping multiple candidates (§58). Nullable
    #: because a patch may be generated before an experiment exists.
    patch_experiment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), nullable=True, index=True
    )
    repository_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("code_repositories.id", ondelete="SET NULL"), nullable=True
    )
    snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("repository_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )

    base_commit_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    patch_format: Mapped[PatchFormat] = mapped_column(
        SAEnum(PatchFormat, name="patchformat", create_constraint=False),
        nullable=False,
        default=PatchFormat.UNIFIED_DIFF,
    )
    patch_content: Mapped[str] = mapped_column(Text, nullable=False)

    #: Measured, not claimed (§9).
    changed_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lines_added: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lines_removed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    symbols_modified: Mapped[List] = mapped_column(
        JSONType, nullable=False, default=list
    )
    affected_paths: Mapped[List] = mapped_column(JSONType, nullable=False, default=list)

    generated_by: Mapped[str] = mapped_column(
        String(32), nullable=False, default="argus"
    )
    generation_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    generation_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="1"
    )

    status: Mapped[PatchStatus] = mapped_column(
        SAEnum(PatchStatus, name="patchstatus", create_constraint=False),
        nullable=False,
        default=PatchStatus.GENERATED,
        index=True,
    )
    #: §20 — what changed, why, what should change, what should not.
    explanation: Mapped[Dict] = mapped_column(JSONType, nullable=False, default=dict)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    hypothesis: Mapped[FixHypothesis] = relationship(back_populates="patches")
    workspaces: Mapped[List["PatchWorkspace"]] = relationship(
        back_populates="patch", cascade="all, delete-orphan"
    )
    verification_runs: Mapped[List["PatchVerificationRun"]] = relationship(
        back_populates="patch", cascade="all, delete-orphan"
    )
    review_actions: Mapped[List["PatchReviewAction"]] = relationship(
        back_populates="patch", cascade="all, delete-orphan"
    )
    artifacts: Mapped[List["PatchArtifact"]] = relationship(
        back_populates="patch", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# PatchWorkspace (§17, §18)
# ---------------------------------------------------------------------------


class PatchWorkspace(BaseModel):
    """One isolated git worktree prepared for one candidate (§17, §18).

    A workspace is created from the repository *snapshot* the fix was made
    against, checked out at ``base_commit_sha`` onto a dedicated branch —
    never the user's working tree, never a shared checkout. One workspace per
    candidate (§58); workspaces are never reused between candidates.
    """

    __tablename__ = "patch_workspaces"
    __table_args__ = (
        Index("ix_patch_workspaces_patch", "patch_id", "status"),
        Index("ix_patch_workspaces_experiment", "patch_experiment_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    patch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("patches.id", ondelete="CASCADE"), nullable=False
    )
    patch_experiment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), nullable=True, index=True
    )
    repository_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("code_repositories.id", ondelete="SET NULL"), nullable=True
    )

    branch_name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_commit_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    patched_commit_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    root_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    status: Mapped[WorkspaceStatus] = mapped_column(
        SAEnum(WorkspaceStatus, name="workspacestatus", create_constraint=False),
        nullable=False,
        default=WorkspaceStatus.CREATING,
        index=True,
    )
    created_at_workspace: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    destroyed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    workspace_metadata: Mapped[Dict] = mapped_column(
        JSONType, nullable=False, default=dict
    )

    patch: Mapped[Patch] = relationship(back_populates="workspaces")


# ---------------------------------------------------------------------------
# Verification runs, test runs, regression tests (§24–§34, §46)
# ---------------------------------------------------------------------------


class PatchVerificationRun(BaseModel):
    """One attempt to verify one patch, ending in one verdict (§33, §34).

    The verdict is computed by :mod:`app.services.patch_verification` from the
    evidence rows below; it is stored, never recomputed on read, so a later
    code change cannot silently reinterpret what was concluded.
    """

    __tablename__ = "patch_verification_runs"
    __table_args__ = (
        Index("ix_patch_verification_patch", "patch_id", "status"),
        Index("ix_patch_verification_project", "project_id", "created_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    patch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("patches.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("patch_workspaces.id", ondelete="SET NULL"), nullable=True
    )
    #: The Phase 5 experiment re-run against the patched code (§30).
    reproduction_experiment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("reproduction_experiments.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[VerificationStatus] = mapped_column(
        SAEnum(VerificationStatus, name="verificationstatus", create_constraint=False),
        nullable=False,
        default=VerificationStatus.PENDING,
        index=True,
    )
    level: Mapped[VerificationLevel] = mapped_column(
        SAEnum(VerificationLevel, name="verificationlevel", create_constraint=False),
        nullable=False,
        default=VerificationLevel.NONE,
    )
    confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="LOW")
    #: §35 — the reason the confidence is what it is.
    confidence_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    #: The strongest tampering flag seen in any stage (§55).
    tampering_flag: Mapped[TamperingFlag] = mapped_column(
        SAEnum(TamperingFlag, name="tamperingflag", create_constraint=False),
        nullable=False,
        default=TamperingFlag.NONE,
    )
    #: §56 — was the verification environment itself untouched?
    verification_env_intact: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    baseline_failure_reproduced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    patched_failure_reproduced: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )
    regression_detected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    verdict_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: §64 — the checklist, as actually executed.
    evidence: Mapped[Dict] = mapped_column(JSONType, nullable=False, default=dict)

    patch: Mapped[Patch] = relationship(back_populates="verification_runs")
    test_runs: Mapped[List["PatchTestRun"]] = relationship(
        back_populates="verification_run", cascade="all, delete-orphan"
    )
    regression_tests: Mapped[List["PatchRegressionTest"]] = relationship(
        back_populates="verification_run", cascade="all, delete-orphan"
    )
    comparisons: Mapped[List["PatchComparison"]] = relationship(
        back_populates="verification_run", cascade="all, delete-orphan"
    )


class PatchTestRun(BaseModel):
    """One allowlisted command executed inside a workspace (§24, §26, §50).

    ``command_key`` names an entry in the command registry — the raw command
    line is stored only as resolved by the registry, never as the AI wrote it.
    A test run without a registry key cannot exist, which is what makes
    "arbitrary command execution" structurally impossible rather than
    merely discouraged (§50).
    """

    __tablename__ = "patch_test_runs"
    __table_args__ = (
        Index("ix_patch_test_runs_verification", "verification_run_id", "kind"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    verification_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("patch_verification_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(), ForeignKey("patch_workspaces.id", ondelete="SET NULL"), nullable=True
    )

    kind: Mapped[TestRunKind] = mapped_column(
        SAEnum(TestRunKind, name="testrunkind", create_constraint=False), nullable=False
    )
    command_key: Mapped[str] = mapped_column(String(64), nullable=False)
    command_resolved: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: §25 — the honest unknown, when the repository defines no command.
    unknown_configuration: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    timed_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tests_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tests_passed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tests_failed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tests_skipped: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: Truncated, redacted combined output (§52).
    output_tail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    selected_tests: Mapped[List] = mapped_column(JSONType, nullable=False, default=list)
    selection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    verification_run: Mapped[PatchVerificationRun] = relationship(
        back_populates="test_runs"
    )


class PatchRegressionTest(BaseModel):
    """A regression test and its two-sided proof (§28, §29).

    The four booleans below are the whole contract: the test must FAIL on the
    base commit (demonstrating the original failure) and PASS on the patched
    commit (demonstrating the fix). Anything else is recorded truthfully and
    disqualifies verification — including the tempting ``passes_on_base=True``
    case, which means the test never demonstrated the failure
    (``REGRESSION_TEST_INVALID``).
    """

    __tablename__ = "patch_regression_tests"
    __table_args__ = (Index("ix_patch_regression_verification", "verification_run_id"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    verification_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("patch_verification_runs.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="generated")
    #: ``existing`` (a test the repo already had, selected by §27) or
    #: ``generated`` (written by ARGUS into the workspace's tests/ directory).

    test_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    ran_on_base: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failed_on_base: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ran_on_patched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    passed_on_patched: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    base_output_tail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    patched_output_tail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    invalid_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    verification_run: Mapped[PatchVerificationRun] = relationship(
        back_populates="regression_tests"
    )


class PatchComparison(BaseModel):
    """BASE vs PATCHED, every number from real experiment telemetry (§31)."""

    __tablename__ = "patch_comparisons"
    __table_args__ = (
        Index("ix_patch_comparisons_verification", "verification_run_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    verification_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(),
        ForeignKey("patch_verification_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    baseline_experiment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("reproduction_experiments.id", ondelete="SET NULL"),
        nullable=True,
    )
    patched_experiment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("reproduction_experiments.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: Per-metric {baseline, patched, delta, unit, source} — numbers only from
    #: captured telemetry; an unavailable metric is absent, never zero (§31).
    metrics: Mapped[Dict] = mapped_column(JSONType, nullable=False, default=dict)
    #: §32 — did the original failure chain disappear?
    causal_chain_resolved: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )
    causal_chain_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: §36 — per-dimension regression findings.
    regressions: Mapped[List] = mapped_column(JSONType, nullable=False, default=list)
    #: §37 — the thresholds these conclusions were computed against.
    thresholds: Mapped[Dict] = mapped_column(JSONType, nullable=False, default=dict)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    verification_run: Mapped[PatchVerificationRun] = relationship(
        back_populates="comparisons"
    )


class PatchRiskAssessment(BaseModel):
    """Deterministic risk with its explicit explanation (§38, §39)."""

    __tablename__ = "patch_risk_assessments"
    __table_args__ = (Index("ix_patch_risk_patch", "patch_id"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    patch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("patches.id", ondelete="CASCADE"), nullable=False
    )
    risk_level: Mapped[RiskLevel] = mapped_column(
        SAEnum(RiskLevel, name="risklevel", create_constraint=False), nullable=False
    )
    #: Signal → explanation, in the order that drove the level (§38).
    signals: Mapped[List] = mapped_column(JSONType, nullable=False, default=list)
    #: §39 quality findings — findings, not a score.
    quality_findings: Mapped[List] = mapped_column(
        JSONType, nullable=False, default=list
    )
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")

    patch: Mapped[Patch] = relationship()


# ---------------------------------------------------------------------------
# Review actions and artifacts (§41, §45, §70)
# ---------------------------------------------------------------------------


class PatchReviewAction(BaseModel):
    """One human decision on one patch (§41, §70, §71).

    Only rows in this table move ``PatchReviewState``; verification never
    approves anything. Phase 7 ends at ``AWAITING_REVIEW`` (§71): an
    ``APPROVE`` row is *stored*, and nothing downstream acts on it
    automatically — no merge, no deploy, no release (§74).
    """

    __tablename__ = "patch_review_actions"
    __table_args__ = (
        Index("ix_patch_review_patch", "patch_id", "created_at"),
        Index("ix_patch_review_project", "project_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    patch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("patches.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[ReviewAction] = mapped_column(
        SAEnum(ReviewAction, name="reviewaction", create_constraint=False),
        nullable=False,
    )
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="engineer")
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Optional regenerated patch produced by a REGENERATE action.
    new_patch_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(), nullable=True)
    #: §70 — the audit event row: actor, action, result, artifact.
    audit_metadata: Mapped[Dict] = mapped_column(JSONType, nullable=False, default=dict)
    #: ``created_at`` comes from ``TimestampMixin`` and is the decision time.

    patch: Mapped[Patch] = relationship(back_populates="review_actions")


class PatchArtifact(BaseModel):
    """Hashed, content-addressed verification artifact (§45)."""

    __tablename__ = "patch_artifacts"
    __table_args__ = (Index("ix_patch_artifacts_patch", "patch_id", "artifact_type"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    patch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("patches.id", ondelete="CASCADE"), nullable=False
    )
    verification_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(),
        ForeignKey("patch_verification_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    artifact_type: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: §45 — immutable once the verification run reaches a terminal state.
    immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    artifact_metadata: Mapped[Dict] = mapped_column(
        JSONType, nullable=False, default=dict
    )

    patch: Mapped[Patch] = relationship(back_populates="artifacts")


__all__ = [
    "FixCategory",
    "FixHypothesis",
    "FixStatus",
    "Patch",
    "PatchArtifact",
    "PatchComparison",
    "PatchFormat",
    "PatchRegressionTest",
    "PatchReviewAction",
    "PatchRiskAssessment",
    "PatchStatus",
    "PatchTestRun",
    "PatchVerificationRun",
    "PatchWorkspace",
    "ReviewAction",
    "ReviewState",
    "RiskLevel",
    "TamperingFlag",
    "TestRunKind",
    "VerificationLevel",
    "VerificationStatus",
    "WorkspaceStatus",
]
