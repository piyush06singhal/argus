"""ARGUS Fix & Verification Service (Phase 7 §5–§8, §23, §33, §41, §58).

The orchestration layer between the HTTP surface and the pure Phase 7
services (parser, safety, generators, workspace, verification). It owns the
*state transitions* and their persistence; the services own the *judgement*.

The rules that shape it:

* **A hypothesis exists only on evidence** (§5): planning goes through
  :class:`~app.services.fix_planner.FixPlanningService`, which refuses when
  the debug session carries no analysis to plan from.
* **A patch is parsed before it is stored** (§7, §11): an unparseable diff is
  recorded as ``PARSE_FAILED`` with the parser's reason — never stored raw.
* **Verification is the only path to ``VERIFIED``** (§33–§34): the stored
  verdict comes from the verification engine's run, not from anyone's
  opinion, and a patch without a verified run stays unverified no matter how
  plausible it looks.
* **Review is human** (§41, §71): only a ``PatchReviewAction`` row moves the
  review state, and approval stops there — nothing merges or deploys.
* **Multiple candidates stay isolated** (§58): every verification attempt
  gets its own workspace, created from the pinned snapshot and destroyed
  after the run.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.code import RepositorySnapshot
from app.models.incident import Incident
from app.models.fix import (
    FixHypothesis,
    FixStatus,
    Patch,
    PatchArtifact,
    PatchComparison,
    PatchRegressionTest,
    PatchReviewAction,
    PatchStatus,
    PatchTestRun,
    PatchVerificationRun,
    PatchWorkspace,
    ReviewAction,
    ReviewState,
    TamperingFlag,
    TestRunKind,
    VerificationLevel,
    VerificationStatus,
    WorkspaceStatus,
)
from app.services.fix_planner import FixPlanner, PlannedFix
from app.services.patch_artifacts import store_verification_artifacts
from app.services.patch_generator import (
    AIFixGenerator,
    DeterministicPatchGenerator,
    PatchGenerationError,
)
from app.services.patch_parser import parse_unified_diff
from app.services.patch_safety import PatchSafetyValidator
from app.services.patch_verification import (
    PatchVerificationEngine,
    VerificationThresholds,
)
from app.services.patch_workspace import PatchWorkspaceManager

logger = logging.getLogger(__name__)


class FixWorkflowError(ValueError):
    """A fix workflow step cannot proceed, with the reason recorded."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Persistence adapters
# ---------------------------------------------------------------------------


async def plan_fix_hypothesis(
    db: AsyncSession,
    *,
    debug_session_id: uuid.UUID,
    title: Optional[str] = None,
    scope_override: Optional[list[str]] = None,
    root_cause_candidate_id: Optional[uuid.UUID] = None,
    reproduction_experiment_id: Optional[uuid.UUID] = None,
    created_by: Optional[str] = None,
) -> FixHypothesis:
    """Plan and persist a fix hypothesis from a debug session (§5)."""
    planner = FixPlanner(db)
    planned: PlannedFix = await planner.plan_from_session(
        debug_session_id,
        title=title,
        scope_override=scope_override,
        root_cause_candidate_id=root_cause_candidate_id,
        reproduction_experiment_id=reproduction_experiment_id,
        created_by=created_by,
    )
    hypothesis = FixHypothesis(
        project_id=planned.project_id,
        incident_id=planned.incident_id,
        debug_session_id=planned.debug_session_id,
        analysis_run_id=planned.analysis_run_id,
        root_cause_candidate_id=planned.root_cause_candidate_id,
        reproduction_experiment_id=planned.reproduction_experiment_id,
        repository_id=planned.repository_id,
        snapshot_id=planned.snapshot_id,
        title=planned.title,
        description=planned.description,
        proposed_change=planned.proposed_change,
        expected_behavior=planned.expected_behavior,
        category=planned.category,
        scope_files=planned.scope_files,
        excluded_paths=planned.excluded_paths,
        supporting_evidence=planned.supporting_evidence,
        target_symbols=planned.target_symbols,
        risk_level=planned.risk_level,
        confidence=planned.confidence,
        status=FixStatus.HYPOTHESIZED,
        created_by=created_by,
    )
    db.add(hypothesis)
    await db.flush()
    return hypothesis


async def generate_patch_for_hypothesis(
    db: AsyncSession,
    *,
    hypothesis_id: uuid.UUID,
    generated_by: str = "deterministic",
    patch_content: Optional[str] = None,
    patch_experiment_id: Optional[uuid.UUID] = None,
) -> Patch:
    """Generate (or accept, for AI-driven flows) and persist a patch (§7, §8).

    ``patch_content`` supplied by an AI caller is parsed and safety-checked
    here *before* storage; the deterministic generator composes its own diff
    from the snapshot's stored file content. Either way the row that lands in
    the database has passed the parser — or is recorded as the failure class
    it earned (§57: no fabricated patch, no silently repaired diff).
    """
    hypothesis = await db.get(FixHypothesis, hypothesis_id)
    if hypothesis is None:
        raise FixWorkflowError(f"unknown fix hypothesis {hypothesis_id}")
    if hypothesis.status in (FixStatus.REJECTED, FixStatus.SUPERSEDED):
        raise FixWorkflowError(
            f"hypothesis is {hypothesis.status.value}; it accepts no new patches"
        )

    hypothesis.status = FixStatus.PATCHING

    snapshot: Optional[RepositorySnapshot] = None
    if hypothesis.snapshot_id is not None:
        snapshot = await db.get(RepositorySnapshot, hypothesis.snapshot_id)

    generation_model: Optional[str] = None
    try:
        if patch_content is not None:
            parsed = parse_unified_diff(patch_content)
            measurements_files = parsed.file_count
            lines_added = parsed.lines_added
            lines_removed = parsed.lines_removed
            explanation = {
                "summary": "Model-proposed patch, parsed and stored as received.",
                "expected_behavior": [],
                "evidence": list(hypothesis.supporting_evidence or []),
            }
            generated_by_used = generated_by
        else:
            file_contents = _snapshot_file_contents(snapshot)
            if generated_by == "ai":
                #: §8 — the model never receives unrestricted repository access:
                #: it is given the scoped files' stored bytes (redacted), the
                #: hypothesis and the evidence references, and its diff is
                #: parsed, scope-checked and safety-validated like any other.
                #: A provider that cannot answer fails loudly (§57) — nothing is
                #: fabricated to fill the gap.
                from app.services.ai_debugger import resolve_provider

                incident = await db.get(Incident, hypothesis.incident_id)
                result = AIFixGenerator(resolve_provider()).generate(
                    incident_title=(incident.title if incident else hypothesis.title),
                    hypothesis_title=hypothesis.title,
                    hypothesis_description=hypothesis.description,
                    proposed_change=hypothesis.proposed_change,
                    category=hypothesis.category.value,
                    scope_files=list(hypothesis.scope_files or []),
                    target_symbols=list(hypothesis.target_symbols or []),
                    evidence_refs=list(hypothesis.supporting_evidence or []),
                    file_contents=file_contents,
                )
            else:
                result = DeterministicPatchGenerator().generate(
                    hypothesis_title=hypothesis.title,
                    hypothesis_description=hypothesis.description,
                    category=hypothesis.category.value,
                    proposed_change=hypothesis.proposed_change,
                    scope_files=list(hypothesis.scope_files or []),
                    target_symbols=list(hypothesis.target_symbols or []),
                    evidence_refs=list(hypothesis.supporting_evidence or []),
                    file_contents=file_contents,
                )
            patch_content = result.patch_content
            parsed = result.parsed
            measurements_files = result.measurements.files_changed
            lines_added = result.measurements.lines_added
            lines_removed = result.measurements.lines_removed
            explanation = result.explanation
            generated_by_used = result.generated_by
            generation_model = result.generation_model
    except PatchGenerationError as error:
        patch = Patch(
            project_id=hypothesis.project_id,
            fix_hypothesis_id=hypothesis.id,
            patch_experiment_id=patch_experiment_id,
            repository_id=hypothesis.repository_id,
            snapshot_id=hypothesis.snapshot_id,
            base_commit_sha=(snapshot.commit_sha if snapshot else None),
            patch_content="",
            changed_files=0,
            lines_added=0,
            lines_removed=0,
            affected_paths=[],
            generated_by=generated_by,
            status=PatchStatus.GENERATION_FAILED,
            explanation={},
            failure_reason=str(error),
        )
        db.add(patch)
        hypothesis.status = FixStatus.GENERATION_FAILED
        await db.flush()
        return patch
    except ValueError as error:
        patch = Patch(
            project_id=hypothesis.project_id,
            fix_hypothesis_id=hypothesis.id,
            patch_experiment_id=patch_experiment_id,
            repository_id=hypothesis.repository_id,
            snapshot_id=hypothesis.snapshot_id,
            base_commit_sha=(snapshot.commit_sha if snapshot else None),
            patch_content=patch_content or "",
            changed_files=0,
            lines_added=0,
            lines_removed=0,
            affected_paths=[],
            generated_by=generated_by,
            status=(
                PatchStatus.PARSE_FAILED
                if "parse" in str(error).lower()
                else PatchStatus.VALIDATION_FAILED
            ),
            explanation={},
            failure_reason=str(error),
        )
        db.add(patch)
        hypothesis.status = FixStatus.GENERATION_FAILED
        await db.flush()
        return patch

    #: Safety gate runs *before* storage (§13): a diff that would leave the
    #: scope, touch sensitive files, or carry a secret is stored as
    #: VALIDATION_FAILED with the findings — evidence for the engineer, not a
    #: silent drop.
    safety = PatchSafetyValidator()
    safety_result = safety.validate(
        parsed,
        scope_files=list(hypothesis.scope_files or []),
        excluded_paths=list(hypothesis.excluded_paths or []),
    )
    if not safety_result.ok:
        patch = Patch(
            project_id=hypothesis.project_id,
            fix_hypothesis_id=hypothesis.id,
            patch_experiment_id=patch_experiment_id,
            repository_id=hypothesis.repository_id,
            snapshot_id=hypothesis.snapshot_id,
            base_commit_sha=(snapshot.commit_sha if snapshot else None),
            patch_content=patch_content or "",
            changed_files=parsed.file_count,
            lines_added=parsed.lines_added,
            lines_removed=parsed.lines_removed,
            affected_paths=parsed.paths,
            generated_by=generated_by_used,
            status=PatchStatus.VALIDATION_FAILED,
            explanation=explanation,
            failure_reason="safety validation failed: "
            + "; ".join(finding.message for finding in safety_result.hard_findings),
        )
        db.add(patch)
        hypothesis.status = FixStatus.GENERATION_FAILED
        await db.flush()
        return patch

    patch = Patch(
        project_id=hypothesis.project_id,
        fix_hypothesis_id=hypothesis.id,
        patch_experiment_id=patch_experiment_id,
        repository_id=hypothesis.repository_id,
        snapshot_id=hypothesis.snapshot_id,
        base_commit_sha=(snapshot.commit_sha if snapshot else None),
        patch_content=patch_content or "",
        changed_files=measurements_files,
        lines_added=lines_added,
        lines_removed=lines_removed,
        symbols_modified=list(parsed_symbols(parsed, hypothesis)),
        affected_paths=parsed.paths,
        generated_by=generated_by_used,
        generation_model=generation_model,
        status=PatchStatus.GENERATED,
        explanation=explanation,
    )
    db.add(patch)
    hypothesis.status = FixStatus.PATCH_GENERATED
    await db.flush()
    return patch


def parsed_symbols(parsed: Any, hypothesis: FixHypothesis) -> list[str]:
    """Symbols from the hypothesis that the diff actually mentions (§9)."""
    import re

    blob = "\n".join(
        line.content
        for item in parsed.files
        for hunk in item.hunks
        for line in hunk.lines
        if line.is_addition or line.is_removal
    )
    return [
        name
        for name in (hypothesis.target_symbols or [])
        if name and re.search(rf"\b{re.escape(name)}\b", blob)
    ]


def _snapshot_file_contents(snapshot: Optional[RepositorySnapshot]) -> dict[str, str]:
    """Read the pinned snapshot's file contents for the deterministic generator.

    The snapshot's ``root_path`` is the directory the Phase 6 indexer
    materialised for reading; reading it is a read-only operation on *ARGUS's*
    stored copy — never the user's working tree.
    """
    if snapshot is None or not snapshot.root_path:
        raise PatchGenerationError(
            "the hypothesis has no indexed snapshot to read file contents from; "
            "index the repository revision first (§8)"
        )
    root = Path(snapshot.root_path)
    if not root.is_dir():
        raise PatchGenerationError(
            f"snapshot storage {snapshot.root_path} is missing on disk; "
            "re-index the revision (§8)"
        )
    contents: dict[str, str] = {}
    for file_path in sorted(root.rglob("*")):
        if file_path.is_file() and file_path.suffix == ".py":
            relative = file_path.relative_to(root).as_posix()
            try:
                contents[relative] = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
    if not contents:
        raise PatchGenerationError(
            "the snapshot contains no readable Python files to compose a patch from (§8)"
        )
    return contents


# ---------------------------------------------------------------------------
# Verification pipeline
# ---------------------------------------------------------------------------


@dataclass
class VerificationRequest:
    """Everything one verification run needs (§33)."""

    patch_id: uuid.UUID
    baseline_reproduced: bool
    baseline_metrics: Optional[dict[str, float]] = None
    patched_metrics: Optional[dict[str, float]] = None
    baseline_failure_signature: str = "the original failure"
    patched_still_reproduces: bool = False
    source_dir: Optional[Path] = None


async def verify_patch(
    db: AsyncSession,
    *,
    request: VerificationRequest,
    workspace_manager: Optional[PatchWorkspaceManager] = None,
    engine: Optional[PatchVerificationEngine] = None,
) -> PatchVerificationRun:
    """Run the verification ladder for one patch and persist everything (§34).

    A fresh isolated workspace is created for this run (§58), the patch is
    applied inside it, the ladder executes, and the workspace is destroyed —
    on every path. The stored verdict is the engine's, not a re-derivation.
    """
    patch = await db.get(Patch, request.patch_id)
    if patch is None:
        raise FixWorkflowError(f"unknown patch {request.patch_id}")
    if patch.status in (
        PatchStatus.PARSE_FAILED,
        PatchStatus.VALIDATION_FAILED,
        PatchStatus.GENERATION_FAILED,
    ):
        raise FixWorkflowError(
            f"patch is {patch.status.value}; it cannot be verified — fix "
            "generation or validate a new candidate first"
        )

    hypothesis = await db.get(FixHypothesis, patch.fix_hypothesis_id)
    run = PatchVerificationRun(
        project_id=patch.project_id,
        patch_id=patch.id,
        status=VerificationStatus.RUNNING,
        started_at=_utcnow(),
        baseline_failure_reproduced=request.baseline_reproduced,
    )
    db.add(run)
    await db.flush()

    patch.status = PatchStatus.APPLIED

    manager = workspace_manager or PatchWorkspaceManager()
    verifier = engine or PatchVerificationEngine(thresholds=VerificationThresholds())

    snapshot: Optional[RepositorySnapshot] = None
    if patch.snapshot_id is not None:
        snapshot = await db.get(RepositorySnapshot, patch.snapshot_id)
    if snapshot is None or not snapshot.root_path:
        run.status = VerificationStatus.FAILED
        run.completed_at = _utcnow()
        run.duration_ms = int(
            (run.completed_at - run.started_at).total_seconds() * 1000
        )
        run.verdict_reason = (
            "no indexed snapshot is pinned to this patch; verification has "
            "nothing materialised to build a workspace from"
        )
        await db.flush()
        return run

    workspace_record = PatchWorkspace(
        project_id=patch.project_id,
        patch_id=patch.id,
        patch_experiment_id=patch.patch_experiment_id,
        repository_id=patch.repository_id,
        branch_name=f"argus/fix/patch-{str(patch.id)[:8]}",
        base_commit_sha=patch.base_commit_sha,
        status=WorkspaceStatus.CREATING,
        created_at_workspace=_utcnow(),
    )
    db.add(workspace_record)
    await db.flush()
    run.workspace_id = workspace_record.id

    #: ``root_path`` comes back from the database as a string, and a caller may
    #: hand over a path-like too — coerce once, here, so every consumer below
    #: genuinely has a :class:`Path`.
    source_dir = (
        Path(request.source_dir)
        if request.source_dir is not None
        else Path(snapshot.root_path)
    )
    try:
        git_workspace, ws_record = manager.create(
            patch_experiment_id=str(patch.id).replace("-", "")[:12],
            candidate_key=f"verify-{str(run.id)[:8]}",
            source_dir=source_dir,
        )
    except Exception as error:  # noqa: BLE001 - recorded, never silent
        run.status = VerificationStatus.FAILED
        run.completed_at = _utcnow()
        run.duration_ms = int(
            (run.completed_at - run.started_at).total_seconds() * 1000
        )
        run.verdict_reason = f"workspace creation failed: {error}"
        workspace_record.status = WorkspaceStatus.FAILED
        workspace_record.destroyed_at = _utcnow()
        await db.flush()
        return run

    workspace_record.status = WorkspaceStatus.PATCH_APPLIED
    workspace_record.root_path = str(git_workspace.root)
    workspace_record.branch_name = ws_record.branch_name
    workspace_record.base_commit_sha = ws_record.base_commit_sha

    try:
        outcome = verifier.verify(
            workspace=git_workspace,
            parsed=parse_unified_diff(patch.patch_content),
            patch_diff=patch.patch_content,
            scope_files=list(hypothesis.scope_files or []) if hypothesis else [],
            baseline_reproduced=request.baseline_reproduced,
            baseline_metrics=request.baseline_metrics,
            patched_metrics=request.patched_metrics,
            baseline_failure_signature=request.baseline_failure_signature,
            target_symbol=(
                (hypothesis.target_symbols or [""])[0] if hypothesis else ""
            ),
            hypothesis_title=hypothesis.title if hypothesis else "",
            patched_still_reproduces=request.patched_still_reproduces,
        )
    finally:
        destroyed = manager.destroy(git_workspace)
        workspace_record.status = WorkspaceStatus.DESTROYED
        workspace_record.destroyed_at = _utcnow()
        workspace_record.workspace_metadata = {
            "destroyed": destroyed.get("destroyed"),
            "destroy_error": destroyed.get("error"),
        }

    # -- persist the outcome (§45, §64) ------------------------------------
    run.status = (
        VerificationStatus.VERIFIED
        if outcome.status == "VERIFIED"
        else VerificationStatus.NOT_VERIFIED
    )
    run.level = VerificationLevel(outcome.level)
    run.confidence = outcome.confidence
    run.confidence_reason = outcome.confidence_reason
    run.tampering_flag = TamperingFlag(outcome.tampering_flag)
    run.verification_env_intact = outcome.verification_env_intact
    run.baseline_failure_reproduced = outcome.baseline_failure_reproduced
    run.patched_failure_reproduced = outcome.patched_failure_reproduced
    run.regression_detected = outcome.regression_detected
    run.completed_at = _utcnow()
    run.duration_ms = int((run.completed_at - run.started_at).total_seconds() * 1000)
    run.verdict_reason = outcome.verdict_reason
    run.evidence = outcome.evidence

    if outcome.status == "VERIFIED":
        patch.status = PatchStatus.VERIFIED
    elif "BUILD_FAILED" in outcome.verdict_reason:
        patch.status = PatchStatus.BUILD_FAILED
    elif "tests failed" in outcome.verdict_reason:
        patch.status = PatchStatus.TEST_FAILED
    elif "regression detected" in outcome.verdict_reason:
        patch.status = PatchStatus.TEST_FAILED
    elif outcome.regression.get("verdict") == "FAILS_ON_PATCHED":
        patch.status = PatchStatus.REPRODUCTION_FAILED
    else:
        patch.status = PatchStatus.TEST_FAILED

    for entry in outcome.test_runs:
        db.add(
            PatchTestRun(
                project_id=patch.project_id,
                verification_run_id=run.id,
                workspace_id=workspace_record.id,
                kind=TestRunKind(entry["kind"]),
                command_key=entry.get("command_key") or "none",
                command_resolved=entry.get("command_key"),
                unknown_configuration=bool(entry.get("unknown_configuration")),
                exit_code=entry.get("exit_code"),
                timed_out=bool(entry.get("timed_out")),
                duration_ms=entry.get("duration_ms"),
                output_tail=entry.get("output_tail"),
                selected_tests=entry.get("selected", []),
                selection_reason=entry.get("selection_reason"),
            )
        )

    regression = outcome.regression or {}
    if regression.get("path"):
        db.add(
            PatchRegressionTest(
                project_id=patch.project_id,
                verification_run_id=run.id,
                name=f"regression:{regression['path']}",
                file_path=regression["path"],
                origin=regression.get("origin", "generated"),
                content_hash=regression.get("content_hash"),
                ran_on_base=regression.get("failed_on_base") is not None,
                failed_on_base=bool(regression.get("failed_on_base")),
                ran_on_patched=regression.get("passed_on_patched") is not None,
                passed_on_patched=bool(regression.get("passed_on_patched")),
                valid=regression.get("verdict") == "VALID",
                invalid_reason=regression.get("invalid_reason"),
            )
        )

    #: §45 — the run's evidence is written to disk, hashed and stored. It lives
    #: outside the workspace (which is destroyed above) so a reviewer can still
    #: re-hash the diff and the logs after the run is gone.
    await store_verification_artifacts(
        db,
        patch=patch,
        run=run,
        outcome=outcome,
        regression_source=outcome.regression_source,
    )

    comparison = outcome.comparison or {}
    if comparison:
        db.add(
            PatchComparison(
                project_id=patch.project_id,
                verification_run_id=run.id,
                metrics=comparison.get("metrics", {}),
                regressions=comparison.get("regressions", []),
                thresholds=comparison.get("thresholds", {}),
                summary=comparison.get("summary"),
                causal_chain_resolved=comparison.get("causal_chain_resolved"),
                causal_chain_note=comparison.get("causal_chain_note"),
            )
        )

    await db.flush()

    #: Phase 10 (§6, §63): a finished verification is an outcome the learning
    #: layer consumes — verified, or verified with a regression. Best effort by
    #: construction (``safely_publish_learning_event``), so a learning-table
    #: problem cannot fail a verification that already happened.
    from app.services.learning_hooks import record_patch_verification

    await record_patch_verification(
        db,
        patch=patch,
        verification=run,
        incident_id=getattr(hypothesis, "incident_id", None),
    )
    return run


# ---------------------------------------------------------------------------
# Review (§41, §70, §71)
# ---------------------------------------------------------------------------

_REVIEW_TRANSITIONS: dict[ReviewAction, ReviewState] = {
    ReviewAction.APPROVE: ReviewState.APPROVED,
    ReviewAction.REJECT: ReviewState.REJECTED,
    ReviewAction.REQUEST_CHANGES: ReviewState.CHANGES_REQUESTED,
    ReviewAction.REGENERATE: ReviewState.AWAITING_REVIEW,
}


async def record_review_action(
    db: AsyncSession,
    *,
    patch_id: uuid.UUID,
    action: ReviewAction,
    actor: str = "engineer",
    reason: Optional[str] = None,
    new_patch_id: Optional[uuid.UUID] = None,
) -> PatchReviewAction:
    """Record the human decision and move the review state (§41, §71).

    Phase 7 stops here by design: an ``APPROVE`` row is *stored* — nothing
    merges, deploys, or releases (§74). A REJECT closes the patch; REGENERATE
    leaves it awaiting a new candidate.
    """
    patch = await db.get(Patch, patch_id)
    if patch is None:
        raise FixWorkflowError(f"unknown patch {patch_id}")

    #: §41 — a decision is final for this candidate. Re-approving (or rejecting
    #: twice) would add an audit row that says nothing new, and the UI already
    #: offers no action once a decision is recorded; the two must agree.
    current_state = await patch_review_state(db, patch.id)
    if current_state in (ReviewState.APPROVED, ReviewState.REJECTED):
        raise FixWorkflowError(
            f"this patch is already {current_state.value}; record the next step "
            "by generating a new candidate instead (§41)"
        )

    if action == ReviewAction.APPROVE and patch.status != PatchStatus.VERIFIED:
        raise FixWorkflowError(
            f"only a VERIFIED patch can be approved; this one is "
            f"{patch.status.value} (§41)"
        )
    if action == ReviewAction.APPROVE:
        run = await _latest_run(db, patch.id)
        if run is None or run.status != VerificationStatus.VERIFIED:
            raise FixWorkflowError(
                "the latest verification run does not hold a VERIFIED verdict; "
                "a patch is never approved on a stale or absent run (§34, §41)"
            )

    row = PatchReviewAction(
        project_id=patch.project_id,
        patch_id=patch.id,
        action=action,
        actor=actor,
        reason=reason,
        new_patch_id=new_patch_id,
        audit_metadata={
            "patch_status_at_decision": patch.status.value,
            "recorded_at": _utcnow().isoformat(),
        },
    )
    db.add(row)

    if action == ReviewAction.REJECT:
        patch.status = PatchStatus.REJECTED
    elif action == ReviewAction.REGENERATE:
        patch.status = PatchStatus.SUPERSEDED

    await db.flush()
    return row


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def patch_review_state(db: AsyncSession, patch_id: uuid.UUID) -> ReviewState:
    """The current review state, derived from actions (§41)."""
    rows = (
        (
            await db.execute(
                select(PatchReviewAction)
                .where(PatchReviewAction.patch_id == patch_id)
                .order_by(PatchReviewAction.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return ReviewState.AWAITING_REVIEW
    return _REVIEW_TRANSITIONS.get(rows[0].action, ReviewState.AWAITING_REVIEW)


async def _latest_run(
    db: AsyncSession, patch_id: uuid.UUID
) -> Optional[PatchVerificationRun]:
    """The most recent verification run for a patch, if any."""
    return (
        (
            await db.execute(
                select(PatchVerificationRun)
                .where(PatchVerificationRun.patch_id == patch_id)
                .order_by(PatchVerificationRun.started_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


def verification_checklist(
    *,
    run: Optional[PatchVerificationRun],
    test_runs: Sequence[PatchTestRun],
    regression_tests: Sequence[PatchRegressionTest],
    patch: Optional[Patch] = None,
) -> list[dict[str, Any]]:
    """§64's checklist, derived *only* from stored rows.

    Nothing here re-runs or re-judges: each entry cites the stored fact that
    establishes it, so an exported report cannot claim a step the engine never
    performed. A step that was not applicable is omitted rather than ticked.
    """
    if run is None:
        return []
    evidence: dict[str, Any] = dict(run.evidence or {})

    def runs_of(kind: str) -> list[PatchTestRun]:
        return [item for item in test_runs if item.kind.value == kind]

    def all_ok(items: Sequence[PatchTestRun]) -> bool:
        return all(
            item.exit_code == 0 and not item.timed_out
            for item in items
            if item.exit_code is not None
        )

    regression = regression_tests[0] if regression_tests else None
    entries: list[dict[str, Any]] = [
        {
            "step": "patch_parses",
            "label": "Patch parses and applies",
            "ok": bool(evidence.get("applied")),
            "detail": "the patch was applied to a workspace",
        },
    ]
    if patch is not None and patch.affected_paths:
        entries.append(
            {
                "step": "scope_valid",
                "label": "Scope valid",
                "ok": True,
                "detail": f"touched {', '.join(patch.affected_paths)} inside the fix scope",
            }
        )
    static_runs = runs_of("STATIC")
    if static_runs:
        entries.append(
            {
                "step": "static_checks",
                "label": "Static checks pass",
                "ok": all_ok(static_runs),
                "detail": ", ".join(
                    f"{item.command_key}={item.exit_code}" for item in static_runs
                ),
            }
        )
    build_runs = runs_of("BUILD")
    if build_runs:
        entries.append(
            {
                "step": "build",
                "label": "Build succeeds",
                "ok": all_ok(build_runs),
                "detail": f"{build_runs[0].command_key} exit {build_runs[0].exit_code}",
            }
        )
    unit_runs = runs_of("UNIT")
    if unit_runs:
        entries.append(
            {
                "step": "existing_tests",
                "label": "Existing tests pass",
                "ok": all_ok(unit_runs),
                "detail": f"{unit_runs[0].command_key} exit {unit_runs[0].exit_code}",
            }
        )
    entries.extend(
        [
            {
                "step": "regression_fails_on_base",
                "label": "Regression test fails on baseline",
                "ok": bool(regression and regression.failed_on_base),
                "detail": "the generated test demonstrates the defect (§29)",
            },
            {
                "step": "regression_passes_on_patch",
                "label": "Regression test passes after patch",
                "ok": bool(regression and regression.passed_on_patched),
                "detail": "the generated test passes on the patched tree (§29)",
            },
            {
                "step": "baseline_reproduced",
                "label": "Original failure reproduced on baseline",
                "ok": bool(run.baseline_failure_reproduced),
                "detail": "Phase 5's experiment demonstrated the failure pre-patch (§33)",
            },
            {
                "step": "patched_fixed",
                "label": "Original failure absent after patch",
                "ok": run.patched_failure_reproduced is False,
                "detail": "the patched tree no longer reproduces the failure (§63)",
            },
            {
                "step": "no_regression",
                "label": "No significant regression detected",
                "ok": not run.regression_detected,
                "detail": "every compared dimension stays inside its threshold (§37)",
            },
            {
                "step": "environment_intact",
                "label": "Verification environment unchanged",
                "ok": bool(run.verification_env_intact),
                "detail": "the patch did not modify the verification tooling (§56)",
            },
            {
                "step": "no_tampering",
                "label": "No test tampering detected",
                "ok": run.tampering_flag == TamperingFlag.NONE,
                "detail": f"tampering flag {run.tampering_flag.value} (§55)",
            },
        ]
    )
    entries.append(
        {
            "step": "verdict",
            "label": "Final verification",
            "ok": run.status == VerificationStatus.VERIFIED,
            "detail": run.verdict_reason or "no verdict recorded",
        }
    )
    return entries


async def export_verification_report(
    db: AsyncSession, *, patch_id: uuid.UUID, fmt: str = "json"
) -> dict[str, Any]:
    """The §72 export: everything an external reviewer needs, one payload."""
    patch = await db.get(Patch, patch_id)
    if patch is None:
        raise FixWorkflowError(f"unknown patch {patch_id}")
    hypothesis = await db.get(FixHypothesis, patch.fix_hypothesis_id)
    runs = (
        (
            await db.execute(
                select(PatchVerificationRun)
                .where(PatchVerificationRun.patch_id == patch.id)
                .order_by(PatchVerificationRun.started_at.desc())
            )
        )
        .scalars()
        .all()
    )
    latest = runs[0] if runs else None
    test_runs = (
        (
            await db.execute(
                select(PatchTestRun)
                .where(PatchTestRun.verification_run_id == latest.id)
                .order_by(PatchTestRun.created_at.asc())
            )
        )
        .scalars()
        .all()
        if latest
        else []
    )
    regression_tests = (
        (
            await db.execute(
                select(PatchRegressionTest)
                .where(PatchRegressionTest.verification_run_id == latest.id)
                .order_by(PatchRegressionTest.created_at.asc())
            )
        )
        .scalars()
        .all()
        if latest
        else []
    )
    return {
        "patch": {
            "id": str(patch.id),
            "status": patch.status.value,
            "changed_files": patch.changed_files,
            "lines_added": patch.lines_added,
            "lines_removed": patch.lines_removed,
            "affected_paths": patch.affected_paths,
            "generated_by": patch.generated_by,
            "explanation": patch.explanation,
        },
        "hypothesis": {
            "id": str(hypothesis.id) if hypothesis else None,
            "title": hypothesis.title if hypothesis else None,
            "category": hypothesis.category.value if hypothesis else None,
            "scope_files": hypothesis.scope_files if hypothesis else [],
            "supporting_evidence": hypothesis.supporting_evidence if hypothesis else [],
        },
        "verification": {
            "status": latest.status.value if latest else None,
            "level": latest.level.value if latest else None,
            "confidence": latest.confidence if latest else None,
            "confidence_reason": latest.confidence_reason if latest else None,
            "verdict_reason": latest.verdict_reason if latest else None,
            "tampering_flag": latest.tampering_flag.value if latest else None,
            "regression_detected": latest.regression_detected if latest else None,
        },
        "review_state": (await patch_review_state(db, patch.id)).value,
        "artifact_count": await db.scalar(
            select(func.count())
            .select_from(PatchArtifact)
            .where(PatchArtifact.patch_id == patch.id)
        )
        or 0,
        #: §64 — the checklist, derived from the stored rows above rather than
        #: restated, so the two can never disagree.
        "verification_checklist": verification_checklist(
            run=latest,
            test_runs=list(test_runs),
            regression_tests=list(regression_tests),
            patch=patch,
        )
        if latest
        else [],
        #: §74 — the boundary is part of the export: this phase produces a
        #: reviewed candidate, never a deployment.
        "boundary": {
            "merged": False,
            "deployed": False,
            "released": False,
            "note": (
                "Phase 7 stops at human review. Nothing is merged, deployed or "
                "released by ARGUS."
            ),
        },
        "patch_diff": patch.patch_content,
    }


__all__ = [
    "FixWorkflowError",
    "VerificationRequest",
    "export_verification_report",
    "generate_patch_for_hypothesis",
    "patch_review_state",
    "plan_fix_hypothesis",
    "record_review_action",
    "verify_patch",
]
