"""ARGUS Phase 7 — Fix Generation & Verification Schemas (§5–§7, §41, §64, §72).

The same honesty rules as every phase, applied to fixes:

1. **A patch and its verdict are separate fields.** ``status`` says where the
   patch stands in its lifecycle; the verification block says what evidence
   exists — and a patch with no verification run says so explicitly rather
   than inheriting optimism.
2. **Every measured number is measured.** ``changed_files``, ``lines_added``,
   ``lines_removed`` come from the parsed diff; the response never includes a
   "quality score" or other invented number (§39).
3. **Review is represented as what happened.** Actions are recorded events
   with an actor and a timestamp; there is no implicit approval anywhere
   (§41, §71).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import Field, field_validator

from app.models.fix import (
    FixCategory,
    FixStatus,
    PatchFormat,
    PatchStatus,
    ReviewAction,
    ReviewState,
    RiskLevel,
    TamperingFlag,
    VerificationLevel,
    VerificationStatus,
    WorkspaceStatus,
)
from app.schemas.base import BaseSchema

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class FixHypothesisCreateRequest(BaseSchema):
    """Plan a fix hypothesis from a debug session (§5)."""

    debug_session_id: uuid.UUID
    title: Optional[str] = Field(default=None, max_length=200)
    scope_override: Optional[list[str]] = None
    root_cause_candidate_id: Optional[uuid.UUID] = None
    reproduction_experiment_id: Optional[uuid.UUID] = None
    #: Who planned it. Recorded on the hypothesis so the audit trail starts at
    #: the hypothesis, not at the first patch (§5, §70).
    created_by: Optional[str] = Field(default=None, max_length=64)

    @field_validator("scope_override")
    @classmethod
    def _scope_clean(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        cleaned = []
        for item in value:
            probe = item.strip()
            if not probe:
                continue
            if probe.startswith("/") or ".." in probe.split("/") or "\\" in probe:
                raise ValueError(f"scope path escapes the repository: {item!r}")
            cleaned.append(probe)
        return cleaned or None


class PatchGenerateRequest(BaseSchema):
    """Generate a patch for a hypothesis (§8)."""

    generated_by: str = Field(default="deterministic", max_length=32)
    patch_experiment_id: Optional[uuid.UUID] = None


class PatchVerifyRequest(BaseSchema):
    """Run the verification ladder for a patch (§33–§34)."""

    baseline_reproduced: bool
    baseline_metrics: Optional[dict[str, float]] = None
    patched_metrics: Optional[dict[str, float]] = None
    baseline_failure_signature: str = Field(
        default="the original failure", max_length=500
    )
    patched_still_reproduces: bool = False


class PatchReviewRequest(BaseSchema):
    """A human decision (§41).

    ``action`` is optional and informational: the endpoint already knows which
    action it performs (``/approve`` approves), so a client cannot create a
    mismatch by sending one action to another endpoint's path.
    """

    """A human decision (§41, §70)."""

    action: Optional[ReviewAction] = None
    actor: str = Field(default="engineer", max_length=64)
    reason: Optional[str] = Field(default=None, max_length=2000)
    new_patch_id: Optional[uuid.UUID] = None


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class FixHypothesisResponse(BaseSchema):
    id: uuid.UUID
    project_id: uuid.UUID
    incident_id: uuid.UUID
    debug_session_id: Optional[uuid.UUID] = None
    analysis_run_id: Optional[uuid.UUID] = None
    root_cause_candidate_id: Optional[uuid.UUID] = None
    reproduction_experiment_id: Optional[uuid.UUID] = None
    repository_id: Optional[uuid.UUID] = None
    snapshot_id: Optional[uuid.UUID] = None
    title: str
    description: str
    proposed_change: str
    expected_behavior: Optional[str] = None
    category: FixCategory
    scope_files: list[str] = Field(default_factory=list)
    excluded_paths: list[str] = Field(default_factory=list)
    supporting_evidence: list[Any] = Field(default_factory=list)
    target_symbols: list[str] = Field(default_factory=list)
    risk_level: RiskLevel
    confidence: str
    status: FixStatus
    created_by: Optional[str] = None
    created_at: datetime


class FixHypothesisListResponse(BaseSchema):
    items: list[FixHypothesisResponse]
    total: int
    truncated: bool = False


class PatchResponse(BaseSchema):
    id: uuid.UUID
    project_id: uuid.UUID
    fix_hypothesis_id: uuid.UUID
    patch_experiment_id: Optional[uuid.UUID] = None
    base_commit_sha: Optional[str] = None
    patch_format: PatchFormat
    changed_files: int
    lines_added: int
    lines_removed: int
    symbols_modified: list[str] = Field(default_factory=list)
    affected_paths: list[str] = Field(default_factory=list)
    generated_by: str
    generation_model: Optional[str] = None
    status: PatchStatus
    explanation: dict[str, Any] = Field(default_factory=dict)
    failure_reason: Optional[str] = None
    created_at: datetime
    review_state: Optional[ReviewState] = None


class PatchListResponse(BaseSchema):
    items: list[PatchResponse]
    total: int
    truncated: bool = False


class PatchTestRunResponse(BaseSchema):
    id: uuid.UUID
    kind: str
    command_key: str
    command_resolved: Optional[str] = None
    unknown_configuration: bool = False
    exit_code: Optional[int] = None
    timed_out: bool = False
    duration_ms: Optional[int] = None
    tests_total: Optional[int] = None
    tests_passed: Optional[int] = None
    tests_failed: Optional[int] = None
    output_tail: Optional[str] = None
    selected_tests: list[str] = Field(default_factory=list)
    selection_reason: Optional[str] = None


class PatchRegressionTestResponse(BaseSchema):
    id: uuid.UUID
    name: str
    file_path: str
    origin: str
    ran_on_base: bool
    failed_on_base: bool
    ran_on_patched: bool
    passed_on_patched: bool
    valid: bool
    invalid_reason: Optional[str] = None
    content_hash: Optional[str] = None


class PatchComparisonResponse(BaseSchema):
    id: uuid.UUID
    metrics: dict[str, Any] = Field(default_factory=dict)
    regressions: list[Any] = Field(default_factory=list)
    thresholds: dict[str, Any] = Field(default_factory=dict)
    causal_chain_resolved: Optional[bool] = None
    causal_chain_note: Optional[str] = None
    summary: Optional[str] = None


class PatchVerificationRunResponse(BaseSchema):
    id: uuid.UUID
    patch_id: uuid.UUID
    workspace_id: Optional[uuid.UUID] = None
    status: VerificationStatus
    level: VerificationLevel
    confidence: str
    confidence_reason: Optional[str] = None
    tampering_flag: TamperingFlag
    verification_env_intact: bool
    baseline_failure_reproduced: bool
    patched_failure_reproduced: Optional[bool] = None
    regression_detected: bool
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    verdict_reason: Optional[str] = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    test_runs: list[PatchTestRunResponse] = Field(default_factory=list)
    regression_tests: list[PatchRegressionTestResponse] = Field(default_factory=list)
    comparisons: list[PatchComparisonResponse] = Field(default_factory=list)


class PatchWorkspaceResponse(BaseSchema):
    id: uuid.UUID
    patch_id: uuid.UUID
    branch_name: str
    base_commit_sha: Optional[str] = None
    patched_commit_sha: Optional[str] = None
    status: WorkspaceStatus
    created_at_workspace: Optional[datetime] = None
    destroyed_at: Optional[datetime] = None
    workspace_metadata: dict[str, Any] = Field(default_factory=dict)


class PatchReviewActionResponse(BaseSchema):
    id: uuid.UUID
    patch_id: uuid.UUID
    action: ReviewAction
    actor: str
    reason: Optional[str] = None
    new_patch_id: Optional[uuid.UUID] = None
    audit_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class PatchDetailResponse(PatchResponse):
    """One patch with its diff and verification history (§42, §44)."""

    patch_content: str
    review_actions: list[PatchReviewActionResponse] = Field(default_factory=list)
    verification_runs: list[PatchVerificationRunResponse] = Field(default_factory=list)
    workspaces: list[PatchWorkspaceResponse] = Field(default_factory=list)


class FixMetricsResponse(BaseSchema):
    """§67 dashboard tallies — counts, never scores."""

    hypotheses_total: int = 0
    patches_total: int = 0
    patches_verified: int = 0
    patches_awaiting_review: int = 0
    patches_rejected: int = 0
    verification_runs_total: int = 0
    tampering_flags_total: int = 0
    regressions_detected_total: int = 0


__all__ = [
    "FixHypothesisCreateRequest",
    "FixHypothesisListResponse",
    "FixHypothesisResponse",
    "FixMetricsResponse",
    "PatchComparisonResponse",
    "PatchDetailResponse",
    "PatchGenerateRequest",
    "PatchListResponse",
    "PatchRegressionTestResponse",
    "PatchResponse",
    "PatchReviewActionResponse",
    "PatchReviewRequest",
    "PatchVerificationRunResponse",
    "PatchVerifyRequest",
    "PatchWorkspaceResponse",
]
