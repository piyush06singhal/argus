"""ARGUS Fix & Verification Routes (Phase 7 §65, §70–§72).

```text
POST   /incidents/{id}/fixes                     plan a fix hypothesis (§5)
GET    /incidents/{id}/fixes                     hypotheses for an incident
GET    /fixes                                    hypotheses (project-scoped)
GET    /fixes/{id}                               one hypothesis

POST   /fixes/{id}/generate                      generate a patch (§8)
GET    /fixes/{id}/patches                       patches for a hypothesis
GET    /patches                                  patches (project-scoped)
GET    /patches/{id}                             one patch + verification history
GET    /patches/{id}/diff                        the raw unified diff (§43)
GET    /patches/{id}/verification                the latest verification run (§64)
POST   /patches/{id}/verify                      run the verification ladder
GET    /patches/{id}/comparison                  BASE vs PATCHED (§31)
GET    /patches/{id}/artifacts                   hashed artifacts (§45)
POST   /patches/{id}/approve                     human decision (§41)
POST   /patches/{id}/reject                      human decision (§41)
POST   /patches/{id}/regenerate                  human decision (§41)
GET    /patches/{id}/report                      the §72 export
GET    /fixes/metrics                            dashboard tallies (§67)
```

Scope rules, following the Phase 3–6 convention:

* a mutating request **requires** ``project_id`` and the resource must belong
  to it — knowing a UUID is not authority to generate or verify a patch;
* reads accept an optional ``project_id``; when supplied it is enforced, and
  an out-of-scope id answers 404 rather than confirming the row exists;
* nothing here executes anything a client sent: verification runs only the
  registry's allowlisted commands inside an isolated workspace, and review
  actions only move ARGUS's own state — no merge, no deploy, no release
  (§71, §74).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_incident, require_project
from app.core.database import get_db
from app.models.fix import (
    FixHypothesis,
    Patch,
    PatchComparison,
    PatchRegressionTest,
    PatchReviewAction,
    PatchStatus,
    PatchTestRun,
    PatchVerificationRun,
    PatchWorkspace,
    ReviewAction,
)
from app.schemas.fix import (
    FixHypothesisCreateRequest,
    FixHypothesisListResponse,
    FixHypothesisResponse,
    FixMetricsResponse,
    PatchComparisonResponse,
    PatchDetailResponse,
    PatchGenerateRequest,
    PatchListResponse,
    PatchRegressionTestResponse,
    PatchResponse,
    PatchReviewActionResponse,
    PatchReviewRequest,
    PatchTestRunResponse,
    PatchVerificationRunResponse,
    PatchVerifyRequest,
    PatchWorkspaceResponse,
)
from app.services.fix_service import (
    FixWorkflowError,
    VerificationRequest,
    export_verification_report,
    generate_patch_for_hypothesis,
    patch_review_state,
    plan_fix_hypothesis,
    record_review_action,
    verify_patch,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_LIST_LIMIT = 100


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _require_hypothesis(
    db: AsyncSession, hypothesis_id: uuid.UUID, project_id: Optional[uuid.UUID]
) -> FixHypothesis:
    hypothesis = await db.get(FixHypothesis, hypothesis_id)
    if hypothesis is None or (
        project_id is not None and hypothesis.project_id != project_id
    ):
        raise HTTPException(status_code=404, detail="Fix hypothesis not found")
    return hypothesis


async def _require_patch(
    db: AsyncSession, patch_id: uuid.UUID, project_id: Optional[uuid.UUID]
) -> Patch:
    patch = await db.get(Patch, patch_id)
    if patch is None or (project_id is not None and patch.project_id != project_id):
        raise HTTPException(status_code=404, detail="Patch not found")
    return patch


def _hypothesis_response(row: FixHypothesis) -> FixHypothesisResponse:
    return FixHypothesisResponse(
        id=row.id,
        project_id=row.project_id,
        incident_id=row.incident_id,
        debug_session_id=row.debug_session_id,
        analysis_run_id=row.analysis_run_id,
        root_cause_candidate_id=row.root_cause_candidate_id,
        reproduction_experiment_id=row.reproduction_experiment_id,
        repository_id=row.repository_id,
        snapshot_id=row.snapshot_id,
        title=row.title,
        description=row.description,
        proposed_change=row.proposed_change,
        expected_behavior=row.expected_behavior,
        category=row.category,
        scope_files=list(row.scope_files or []),
        excluded_paths=list(row.excluded_paths or []),
        supporting_evidence=list(row.supporting_evidence or []),
        target_symbols=list(row.target_symbols or []),
        risk_level=row.risk_level,
        confidence=row.confidence,
        status=row.status,
        created_by=row.created_by,
        created_at=row.created_at,
    )


async def _patch_response(
    db: AsyncSession, row: Patch, *, with_review: bool = True
) -> PatchResponse:
    review_state = await patch_review_state(db, row.id) if with_review else None
    return PatchResponse(
        id=row.id,
        project_id=row.project_id,
        fix_hypothesis_id=row.fix_hypothesis_id,
        patch_experiment_id=row.patch_experiment_id,
        base_commit_sha=row.base_commit_sha,
        patch_format=row.patch_format,
        changed_files=row.changed_files,
        lines_added=row.lines_added,
        lines_removed=row.lines_removed,
        symbols_modified=list(row.symbols_modified or []),
        affected_paths=list(row.affected_paths or []),
        generated_by=row.generated_by,
        generation_model=row.generation_model,
        status=row.status,
        explanation=dict(row.explanation or {}),
        failure_reason=row.failure_reason,
        created_at=row.created_at,
        review_state=review_state,
    )


# ---------------------------------------------------------------------------
# Hypotheses (§5)
# ---------------------------------------------------------------------------


@router.post("/incidents/{incident_id}/fixes", response_model=FixHypothesisResponse)
async def create_fix_hypothesis(
    incident_id: uuid.UUID,
    request: FixHypothesisCreateRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> FixHypothesisResponse:
    """Plan a fix hypothesis from a debug session (§5)."""
    await require_project(db, project_id)
    await require_incident(db, incident_id, project_id=project_id)
    try:
        hypothesis = await plan_fix_hypothesis(
            db,
            debug_session_id=request.debug_session_id,
            title=request.title,
            scope_override=request.scope_override,
            root_cause_candidate_id=request.root_cause_candidate_id,
            reproduction_experiment_id=request.reproduction_experiment_id,
            created_by=request.created_by,
        )
    except FixWorkflowError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if hypothesis.incident_id != incident_id:
        raise HTTPException(
            status_code=422,
            detail="the debug session belongs to a different incident",
        )
    await db.commit()
    return _hypothesis_response(hypothesis)


@router.get("/incidents/{incident_id}/fixes", response_model=FixHypothesisListResponse)
async def list_incident_fixes(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(default=_LIST_LIMIT, ge=1, le=_LIST_LIMIT),
    db: AsyncSession = Depends(get_db),
) -> FixHypothesisListResponse:
    """Hypotheses for one incident."""
    await require_incident(db, incident_id, project_id=project_id)
    stmt = (
        select(FixHypothesis)
        .where(FixHypothesis.incident_id == incident_id)
        .order_by(FixHypothesis.created_at.desc())
        .limit(limit + 1)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    truncated = len(rows) > limit
    items = [_hypothesis_response(row) for row in rows[:limit]]
    return FixHypothesisListResponse(items=items, total=len(items), truncated=truncated)


@router.get("/fixes", response_model=FixHypothesisListResponse)
async def list_fixes(
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    incident_id: Optional[uuid.UUID] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(default=_LIST_LIMIT, ge=1, le=_LIST_LIMIT),
    db: AsyncSession = Depends(get_db),
) -> FixHypothesisListResponse:
    """Hypotheses in a project (§67 dashboard source)."""
    await require_project(db, project_id)
    stmt = (
        select(FixHypothesis)
        .where(FixHypothesis.project_id == project_id)
        .order_by(FixHypothesis.created_at.desc())
        .limit(limit + 1)
    )
    if incident_id is not None:
        stmt = stmt.where(FixHypothesis.incident_id == incident_id)
    if status:
        try:
            from app.models.fix import FixStatus

            stmt = stmt.where(FixHypothesis.status == FixStatus(status))
        except ValueError as error:
            raise HTTPException(
                status_code=422, detail=f"unknown fix status {status!r}"
            ) from error
    rows = list((await db.execute(stmt)).scalars().all())
    truncated = len(rows) > limit
    items = [_hypothesis_response(row) for row in rows[:limit]]
    return FixHypothesisListResponse(items=items, total=len(items), truncated=truncated)


@router.get("/fixes/metrics", response_model=FixMetricsResponse)
async def fix_metrics(
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> FixMetricsResponse:
    """§67 tallies for the fix dashboard."""
    await require_project(db, project_id)
    hypotheses_total = (
        await db.scalar(
            select(func.count())
            .select_from(FixHypothesis)
            .where(FixHypothesis.project_id == project_id)
        )
        or 0
    )
    patches_total = (
        await db.scalar(
            select(func.count())
            .select_from(Patch)
            .where(Patch.project_id == project_id)
        )
        or 0
    )
    patches_verified = (
        await db.scalar(
            select(func.count())
            .select_from(Patch)
            .where(Patch.project_id == project_id, Patch.status == PatchStatus.VERIFIED)
        )
        or 0
    )
    patches_rejected = (
        await db.scalar(
            select(func.count())
            .select_from(Patch)
            .where(Patch.project_id == project_id, Patch.status == PatchStatus.REJECTED)
        )
        or 0
    )
    verification_runs_total = (
        await db.scalar(
            select(func.count())
            .select_from(PatchVerificationRun)
            .where(PatchVerificationRun.project_id == project_id)
        )
        or 0
    )
    tampering_flags_total = (
        await db.scalar(
            select(func.count())
            .select_from(PatchVerificationRun)
            .where(
                PatchVerificationRun.project_id == project_id,
                PatchVerificationRun.tampering_flag != "NONE",
            )
        )
        or 0
    )
    regressions_detected_total = (
        await db.scalar(
            select(func.count())
            .select_from(PatchVerificationRun)
            .where(
                PatchVerificationRun.project_id == project_id,
                PatchVerificationRun.regression_detected.is_(True),
            )
        )
        or 0
    )
    patches_awaiting_review = 0
    patch_rows = (
        (
            await db.execute(
                select(Patch).where(
                    Patch.project_id == project_id,
                    Patch.status == PatchStatus.VERIFIED,
                )
            )
        )
        .scalars()
        .all()
    )
    for patch_row in patch_rows:
        from app.models.fix import ReviewState

        if (await patch_review_state(db, patch_row.id)) == ReviewState.AWAITING_REVIEW:
            patches_awaiting_review += 1
    return FixMetricsResponse(
        hypotheses_total=int(hypotheses_total),
        patches_total=int(patches_total),
        patches_verified=int(patches_verified),
        patches_awaiting_review=patches_awaiting_review,
        patches_rejected=int(patches_rejected),
        verification_runs_total=int(verification_runs_total),
        tampering_flags_total=int(tampering_flags_total),
        regressions_detected_total=int(regressions_detected_total),
    )


@router.get("/fixes/{hypothesis_id}", response_model=FixHypothesisResponse)
async def get_fix(
    hypothesis_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> FixHypothesisResponse:
    """One hypothesis."""
    hypothesis = await _require_hypothesis(db, hypothesis_id, project_id)
    return _hypothesis_response(hypothesis)


# ---------------------------------------------------------------------------
# Patch generation (§8)
# ---------------------------------------------------------------------------


@router.post("/fixes/{hypothesis_id}/generate", response_model=PatchResponse)
async def generate_patch(
    hypothesis_id: uuid.UUID,
    request: PatchGenerateRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> PatchResponse:
    """Generate a patch for the hypothesis (§8, §57)."""
    await require_project(db, project_id)
    hypothesis = await _require_hypothesis(db, hypothesis_id, project_id)
    _ = hypothesis
    try:
        patch = await generate_patch_for_hypothesis(
            db,
            hypothesis_id=hypothesis_id,
            generated_by=request.generated_by,
            patch_experiment_id=request.patch_experiment_id,
        )
    except FixWorkflowError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    await db.commit()
    return await _patch_response(db, patch)


@router.get("/fixes/{hypothesis_id}/patches", response_model=PatchListResponse)
async def list_hypothesis_patches(
    hypothesis_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(default=_LIST_LIMIT, ge=1, le=_LIST_LIMIT),
    db: AsyncSession = Depends(get_db),
) -> PatchListResponse:
    """Patches for one hypothesis (§58 candidates included)."""
    await _require_hypothesis(db, hypothesis_id, project_id)
    stmt = (
        select(Patch)
        .where(Patch.fix_hypothesis_id == hypothesis_id)
        .order_by(Patch.created_at.desc())
        .limit(limit + 1)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    truncated = len(rows) > limit
    items = [await _patch_response(db, row) for row in rows[:limit]]
    return PatchListResponse(items=items, total=len(items), truncated=truncated)


# ---------------------------------------------------------------------------
# Patches (§7)
# ---------------------------------------------------------------------------


@router.get("/patches", response_model=PatchListResponse)
async def list_patches(
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    status: Optional[str] = Query(None),
    limit: int = Query(default=_LIST_LIMIT, ge=1, le=_LIST_LIMIT),
    db: AsyncSession = Depends(get_db),
) -> PatchListResponse:
    """Patches in a project (§67 dashboard source)."""
    await require_project(db, project_id)
    stmt = (
        select(Patch)
        .where(Patch.project_id == project_id)
        .order_by(Patch.created_at.desc())
        .limit(limit + 1)
    )
    if status:
        try:
            stmt = stmt.where(Patch.status == PatchStatus(status))
        except ValueError as error:
            raise HTTPException(
                status_code=422, detail=f"unknown patch status {status!r}"
            ) from error
    rows = list((await db.execute(stmt)).scalars().all())
    truncated = len(rows) > limit
    items = [await _patch_response(db, row) for row in rows[:limit]]
    return PatchListResponse(items=items, total=len(items), truncated=truncated)


async def _verification_response(
    db: AsyncSession, run: PatchVerificationRun
) -> PatchVerificationRunResponse:
    """One verification run *with* the rows that produced its verdict.

    The engine stores test runs, the regression test and the comparison; a
    response that omitted them would show an empty evidence set next to a
    VERIFIED verdict, which reads as "nothing was checked". Everything the
    verdict rests on is returned with it (§34, §44, §64).
    """
    test_runs = (
        (
            await db.execute(
                select(PatchTestRun)
                .where(PatchTestRun.verification_run_id == run.id)
                .order_by(PatchTestRun.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    regression_tests = (
        (
            await db.execute(
                select(PatchRegressionTest)
                .where(PatchRegressionTest.verification_run_id == run.id)
                .order_by(PatchRegressionTest.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    comparisons = (
        (
            await db.execute(
                select(PatchComparison)
                .where(PatchComparison.verification_run_id == run.id)
                .order_by(PatchComparison.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return PatchVerificationRunResponse(
        id=run.id,
        patch_id=run.patch_id,
        workspace_id=run.workspace_id,
        status=run.status,
        level=run.level,
        confidence=run.confidence,
        confidence_reason=run.confidence_reason,
        tampering_flag=run.tampering_flag,
        verification_env_intact=run.verification_env_intact,
        baseline_failure_reproduced=run.baseline_failure_reproduced,
        patched_failure_reproduced=run.patched_failure_reproduced,
        regression_detected=run.regression_detected,
        started_at=run.started_at,
        completed_at=run.completed_at,
        duration_ms=run.duration_ms,
        verdict_reason=run.verdict_reason,
        evidence=dict(run.evidence or {}),
        test_runs=[
            PatchTestRunResponse(
                id=item.id,
                kind=item.kind.value,
                command_key=item.command_key,
                command_resolved=item.command_resolved,
                unknown_configuration=item.unknown_configuration,
                exit_code=item.exit_code,
                timed_out=item.timed_out,
                duration_ms=item.duration_ms,
                tests_total=item.tests_total,
                tests_passed=item.tests_passed,
                tests_failed=item.tests_failed,
                output_tail=item.output_tail,
                selected_tests=list(item.selected_tests or []),
                selection_reason=item.selection_reason,
            )
            for item in test_runs
        ],
        regression_tests=[
            PatchRegressionTestResponse(
                id=item.id,
                name=item.name,
                file_path=item.file_path,
                origin=item.origin,
                ran_on_base=item.ran_on_base,
                failed_on_base=item.failed_on_base,
                ran_on_patched=item.ran_on_patched,
                passed_on_patched=item.passed_on_patched,
                valid=item.valid,
                invalid_reason=item.invalid_reason,
                content_hash=item.content_hash,
            )
            for item in regression_tests
        ],
        comparisons=[
            PatchComparisonResponse(
                id=item.id,
                metrics=dict(item.metrics or {}),
                regressions=list(item.regressions or []),
                thresholds=dict(item.thresholds or {}),
                causal_chain_resolved=item.causal_chain_resolved,
                causal_chain_note=item.causal_chain_note,
                summary=item.summary,
            )
            for item in comparisons
        ],
    )


async def _patch_detail(db: AsyncSession, patch: Patch) -> PatchDetailResponse:
    base = await _patch_response(db, patch)
    actions = (
        (
            await db.execute(
                select(PatchReviewAction)
                .where(PatchReviewAction.patch_id == patch.id)
                .order_by(PatchReviewAction.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
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
    workspaces = (
        (
            await db.execute(
                select(PatchWorkspace)
                .where(PatchWorkspace.patch_id == patch.id)
                .order_by(PatchWorkspace.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return PatchDetailResponse(
        **base.model_dump(),
        patch_content=patch.patch_content,
        review_actions=[
            PatchReviewActionResponse(
                id=action.id,
                patch_id=action.patch_id,
                action=action.action,
                actor=action.actor,
                reason=action.reason,
                new_patch_id=action.new_patch_id,
                audit_metadata=dict(action.audit_metadata or {}),
                created_at=action.created_at,
            )
            for action in actions
        ],
        verification_runs=[await _verification_response(db, run) for run in runs],
        workspaces=[
            PatchWorkspaceResponse(
                id=ws.id,
                patch_id=ws.patch_id,
                branch_name=ws.branch_name,
                base_commit_sha=ws.base_commit_sha,
                patched_commit_sha=ws.patched_commit_sha,
                status=ws.status,
                created_at_workspace=ws.created_at_workspace,
                destroyed_at=ws.destroyed_at,
                workspace_metadata=dict(ws.workspace_metadata or {}),
            )
            for ws in workspaces
        ],
    )


@router.get("/patches/{patch_id}", response_model=PatchDetailResponse)
async def get_patch(
    patch_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> PatchDetailResponse:
    """One patch with its diff, verification history, and review audit (§42)."""
    patch = await _require_patch(db, patch_id, project_id)
    return await _patch_detail(db, patch)


@router.get("/patches/{patch_id}/diff", response_model=None)
async def get_patch_diff(
    patch_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """The raw unified diff, as stored (§43)."""
    patch = await _require_patch(db, patch_id, project_id)
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(
        content=patch.patch_content,
        media_type="text/x-diff; charset=utf-8",
        headers={"Content-Disposition": f'inline; filename="patch-{patch.id}.diff"'},
    )


# ---------------------------------------------------------------------------
# Verification (§33–§34)
# ---------------------------------------------------------------------------


@router.post("/patches/{patch_id}/verify", response_model=PatchVerificationRunResponse)
async def verify_patch_route(
    patch_id: uuid.UUID,
    request: PatchVerifyRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> PatchVerificationRunResponse:
    """Run the verification ladder (§34). Synchronous by design: the run is
    bounded by the command registry's timeouts and the caller sees the verdict
    it caused."""
    await require_project(db, project_id)
    # Phase 9 §10: patch verification spends real compute inside a workspace, so
    # it is one of the platform behaviours an ARGUS remediation can switch off.
    from app.services.remediation_controls import safely_feature_enabled

    if not await safely_feature_enabled(db, "fix_verification", project_id=project_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "patch verification is disabled for this project by a remediation "
                "control"
            ),
        )
    patch = await _require_patch(db, patch_id, project_id)
    _ = patch
    try:
        run = await verify_patch(
            db,
            request=VerificationRequest(
                patch_id=patch_id,
                baseline_reproduced=request.baseline_reproduced,
                baseline_metrics=request.baseline_metrics,
                patched_metrics=request.patched_metrics,
                baseline_failure_signature=request.baseline_failure_signature,
                patched_still_reproduces=request.patched_still_reproduces,
            ),
        )
    except FixWorkflowError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    await db.commit()
    return await _verification_response(db, run)


@router.get(
    "/patches/{patch_id}/verification", response_model=PatchVerificationRunResponse
)
async def latest_verification(
    patch_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> PatchVerificationRunResponse:
    """The latest verification run for a patch (§64)."""
    patch = await _require_patch(db, patch_id, project_id)
    run = await db.scalar(
        select(PatchVerificationRun)
        .where(PatchVerificationRun.patch_id == patch.id)
        .order_by(PatchVerificationRun.started_at.desc())
        .limit(1)
    )
    if run is None:
        raise HTTPException(
            status_code=404,
            detail="this patch has not been verified yet",
        )
    return await _verification_response(db, run)


@router.get("/patches/{patch_id}/comparison", response_model=None)
async def patch_comparison(
    patch_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """BASE vs PATCHED comparison rows (§31)."""
    patch = await _require_patch(db, patch_id, project_id)
    run = await db.scalar(
        select(PatchVerificationRun)
        .where(PatchVerificationRun.patch_id == patch.id)
        .order_by(PatchVerificationRun.started_at.desc())
        .limit(1)
    )
    if run is None:
        raise HTTPException(
            status_code=404, detail="this patch has no verification run"
        )
    #: Queried explicitly rather than through ``run.comparisons``: a lazy
    #: relationship cannot be loaded from async code, and the failure mode is a
    #: 500 on a read that should always work.
    comparisons = (
        (
            await db.execute(
                select(PatchComparison)
                .where(PatchComparison.verification_run_id == run.id)
                .order_by(PatchComparison.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(item.id),
                "metrics": dict(item.metrics or {}),
                "regressions": list(item.regressions or []),
                "thresholds": dict(item.thresholds or {}),
                "causal_chain_resolved": item.causal_chain_resolved,
                "causal_chain_note": item.causal_chain_note,
                "summary": item.summary,
            }
            for item in comparisons
        ],
        "total": len(comparisons),
    }


@router.get("/patches/{patch_id}/artifacts", response_model=None)
async def patch_artifacts(
    patch_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Hashed verification artifacts (§45)."""
    patch = await _require_patch(db, patch_id, project_id)
    from app.models.fix import PatchArtifact

    rows = (
        (
            await db.execute(
                select(PatchArtifact)
                .where(PatchArtifact.patch_id == patch.id)
                .order_by(PatchArtifact.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(row.id),
                "artifact_type": row.artifact_type,
                "name": row.name,
                "storage_path": row.storage_path,
                "size_bytes": row.size_bytes,
                "content_hash": row.content_hash,
                "immutable": row.immutable,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
        "total": len(rows),
    }


# ---------------------------------------------------------------------------
# Review (§41, §70, §71)
# ---------------------------------------------------------------------------


@router.post("/patches/{patch_id}/approve", response_model=PatchReviewActionResponse)
async def approve_patch(
    patch_id: uuid.UUID,
    request: PatchReviewRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> PatchReviewActionResponse:
    """Record an approval (§41). Nothing merges or deploys — §71, §74."""
    await require_project(db, project_id)
    patch = await _require_patch(db, patch_id, project_id)
    _ = patch
    try:
        row = await record_review_action(
            db,
            patch_id=patch_id,
            action=ReviewAction.APPROVE,
            actor=request.actor,
            reason=request.reason,
            new_patch_id=request.new_patch_id,
        )
    except FixWorkflowError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    await db.commit()
    return PatchReviewActionResponse(
        id=row.id,
        patch_id=row.patch_id,
        action=row.action,
        actor=row.actor,
        reason=row.reason,
        new_patch_id=row.new_patch_id,
        audit_metadata=dict(row.audit_metadata or {}),
        created_at=row.created_at,
    )


@router.post("/patches/{patch_id}/reject", response_model=PatchReviewActionResponse)
async def reject_patch(
    patch_id: uuid.UUID,
    request: PatchReviewRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> PatchReviewActionResponse:
    """Record a rejection (§41)."""
    await require_project(db, project_id)
    patch = await _require_patch(db, patch_id, project_id)
    _ = patch
    try:
        row = await record_review_action(
            db,
            patch_id=patch_id,
            action=ReviewAction.REJECT,
            actor=request.actor,
            reason=request.reason,
        )
    except FixWorkflowError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    await db.commit()
    return PatchReviewActionResponse(
        id=row.id,
        patch_id=row.patch_id,
        action=row.action,
        actor=row.actor,
        reason=row.reason,
        new_patch_id=row.new_patch_id,
        audit_metadata=dict(row.audit_metadata or {}),
        created_at=row.created_at,
    )


@router.post("/patches/{patch_id}/regenerate", response_model=PatchResponse)
async def regenerate_patch(
    patch_id: uuid.UUID,
    request: PatchReviewRequest,
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> PatchResponse:
    """Supersede this patch and generate a fresh candidate (§41, §58)."""
    await require_project(db, project_id)
    patch = await _require_patch(db, patch_id, project_id)
    try:
        await record_review_action(
            db,
            patch_id=patch_id,
            action=ReviewAction.REGENERATE,
            actor=request.actor,
            reason=request.reason,
        )
        new_patch = await generate_patch_for_hypothesis(
            db,
            hypothesis_id=patch.fix_hypothesis_id,
            generated_by="deterministic",
            patch_experiment_id=patch.patch_experiment_id,
        )
    except FixWorkflowError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    await db.commit()
    return await _patch_response(db, new_patch)


@router.get("/patches/{patch_id}/report", response_model=None)
async def verification_report(
    patch_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """The §72 export — everything an external reviewer needs."""
    await _require_patch(db, patch_id, project_id)
    try:
        return await export_verification_report(db, patch_id=patch_id)
    except FixWorkflowError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
