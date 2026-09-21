"""Phase 7 demo scenarios (§59–§63).

These run the *real* pipeline against the *real* demo application
(``demo/argus-commerce``) — its own planted defect, its own git history, its own
test suite. Nothing about the answer is written into the engine: the retry loop
that amplifies a sub-second database timeout lives in the demo's source, and the
fix is derived from the pinned snapshot's stored bytes.

Four scenarios, each asserting the behaviour the phase is judged on:

``§60`` — the full workflow, ending ``VERIFIED`` and ``AWAITING_REVIEW``.
``§61`` — a patch that fixes the timeout but regresses another dimension:
         ``REGRESSION_DETECTED``, ``NOT_VERIFIED``.
``§62`` — a patch that deletes the failing test: ``TEST_TAMPERING``, refused
         before it can reach a workspace.
``§63`` — a patch that builds and passes tests but leaves the original failure
         reproducing: ``NOT_VERIFIED``, and the reason says exactly that.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.fix_service import (
    FixWorkflowError,
    VerificationRequest,
    generate_patch_for_hypothesis,
    patch_review_state,
    plan_fix_hypothesis,
    record_review_action,
    verify_patch,
)
from app.services.patch_generator import render_unified_diff
from tests.phase6_helpers import build_incident, build_project

#: The repository under demonstration. Copied (never mutated) into a temp tree,
#: so the demo's own checkout is used as *evidence*, not as a scratch area.
DEMO_ROOT = Path(__file__).resolve().parents[3] / "demo" / "argus-commerce"

DEFECT_FILE = "services/inventory/repository.py"
DEFECT_LINE = "DB_TIMEOUT_SECONDS = 0.25"
FIXED_LINE = "DB_TIMEOUT_SECONDS = 2.0"
FAILURE_SIGNATURE = "inventory database query timed out"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _materialise_demo(tmp_path: Path) -> Path:
    """A git-backed copy of the demo application at its failing revision."""
    root = tmp_path / "argus-commerce"
    shutil.copytree(
        DEMO_ROOT,
        root,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "demo@argus")
    _git(root, "config", "user.name", "ARGUS Demo")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "demo commerce: halve the database timeout")
    return root


async def _index_demo(db_session, project, root: Path):
    """Index the demo checkout so the fix has real stored file bytes to read."""
    from app.models.deployment import CodeRepository, RepositoryIndexStatus
    from app.services.code_index_service import CodeIndexer
    from app.services.code_snapshot_service import CodeSnapshotService

    repository = CodeRepository(
        project_id=project.id,
        provider="local",
        repository_url=str(root),
        local_path=str(root),
        default_branch="main",
        language="python",
    )
    db_session.add(repository)
    await db_session.flush()
    snapshot = await CodeSnapshotService().get_or_create_snapshot(
        db_session, repository, None, version_evidence="phase 7 demo"
    )
    await CodeIndexer(db_session).index(repository, snapshot, trigger="phase7-demo")
    repository.index_status = RepositoryIndexStatus.INDEXED
    repository.last_indexed_commit = snapshot.commit_sha
    await db_session.flush()
    return repository, snapshot


async def _demo_fixture(db_session, tmp_path):
    """Project + incident + causal analysis + a debug session whose validated
    location *is* the demo's planted defect."""
    from app.models.causal import ConfidenceLevel
    from app.models.code import (
        DebugAnalysisRun,
        DebugAnalysisStatus,
        DebugCodeLocation,
        DebugSession,
        DebugSessionStatus,
        LocationValidation,
    )

    project, environment, component = await build_project(
        db_session, name="Phase7 Demo"
    )
    root = _materialise_demo(tmp_path)
    repository, snapshot = await _index_demo(db_session, project, root)
    incident = await build_incident(db_session, project, environment, component)

    session = DebugSession(
        project_id=project.id,
        incident_id=incident.id,
        repository_id=repository.id,
        snapshot_id=snapshot.id,
        status=DebugSessionStatus.COMPLETED,
    )
    db_session.add(session)
    await db_session.flush()

    analysis = DebugAnalysisRun(
        project_id=project.id,
        session_id=session.id,
        status=DebugAnalysisStatus.COMPLETED,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=4),
        completed_at=datetime.now(timezone.utc) - timedelta(minutes=3),
        confidence=ConfidenceLevel.MEDIUM,
        summary=(
            "The inventory query timeout is configured below one second, so "
            "every attempt fails immediately and the checkout retry loop "
            "amplifies a fast failure into a multi-second request."
        ),
    )
    db_session.add(analysis)
    await db_session.flush()

    db_session.add(
        DebugCodeLocation(
            project_id=project.id,
            session_id=session.id,
            analysis_run_id=analysis.id,
            snapshot_id=snapshot.id,
            file_path=DEFECT_FILE,
            symbol_name="DB_TIMEOUT_SECONDS",
            start_line=16,
            end_line=16,
            label="LIKELY_FAULT_LOCATION",
            reason=(
                "The timeout constant is below the query latency, so the "
                "database call times out on every attempt"
            ),
            confidence=ConfidenceLevel.MEDIUM,
            validation=LocationValidation.VALID,
        )
    )
    await db_session.flush()
    return project, incident, session, snapshot, root


async def _verify(
    db_session,
    patch,
    *,
    metrics,
    baseline=None,
    still_reproduces=False,
    source_dir,
):
    return await verify_patch(
        db_session,
        request=VerificationRequest(
            patch_id=patch.id,
            baseline_reproduced=True,
            baseline_metrics=baseline or {"error_rate": 0.082, "latency_p95_ms": 2900},
            patched_metrics=metrics,
            baseline_failure_signature=FAILURE_SIGNATURE,
            patched_still_reproduces=still_reproduces,
            source_dir=source_dir,
        ),
    )


# ---------------------------------------------------------------------------
# §60 — the successful fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_successful_fix_is_verified_and_awaits_review(db_session, tmp_path):
    project, incident, session, snapshot, root = await _demo_fixture(
        db_session, tmp_path
    )

    # 1-5: a hypothesis planned from the debug session's validated location.
    hypothesis = await plan_fix_hypothesis(
        db_session, debug_session_id=session.id, created_by="demo"
    )
    assert hypothesis.scope_files == [DEFECT_FILE], hypothesis.scope_files
    assert hypothesis.target_symbols == ["DB_TIMEOUT_SECONDS"]
    #: The category is derived from the evidence text, never forced (§6): the
    #: failure shape names both the retry loop and the timeout, and either
    #: category is evidence-supported.
    assert hypothesis.category.value in {"RETRY_FIX", "TIMEOUT_FIX"}

    # 6-7: a patch generated from the snapshot's stored bytes.
    patch = await generate_patch_for_hypothesis(db_session, hypothesis_id=hypothesis.id)
    assert patch.status.value == "GENERATED", patch.failure_reason
    assert DEFECT_LINE in patch.patch_content
    assert FIXED_LINE in patch.patch_content

    # 8-19: the workspace ladder, against the demo's real test suite.
    run = await _verify(
        db_session,
        patch,
        metrics={"error_rate": 0.006, "latency_p95_ms": 300},
        source_dir=snapshot.root_path,
    )
    assert run.status.value == "VERIFIED", run.verdict_reason
    assert run.level.value == "FULLY_VERIFIED"
    regression = run.evidence["regression"]
    assert regression["verdict"] == "VALID"
    assert regression["failed_on_base"] is True
    assert regression["passed_on_patched"] is True
    assert run.regression_detected is False
    assert patch.status.value == "VERIFIED"

    # The demo's own suite ran for real and passed on the patched tree.
    assert run.evidence["tests"]["exit_code"] == 0, run.evidence["tests"]

    # 20: it stops at human review. Nothing was merged or deployed.
    assert (await patch_review_state(db_session, patch.id)).value == "AWAITING_REVIEW"
    await record_review_action(
        db_session,
        patch_id=patch.id,
        action="APPROVE",
        actor="engineer",
        reason="evidence complete",
    )
    assert (await patch_review_state(db_session, patch.id)).value == "APPROVED"
    assert (
        patch.status.value == "VERIFIED"
    ), "§71: an approval records a decision, it does not deploy anything"


# ---------------------------------------------------------------------------
# §61 — a plausible patch that regresses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_bad_patch_regression_is_detected(db_session, tmp_path):
    project, incident, session, snapshot, root = await _demo_fixture(
        db_session, tmp_path
    )
    hypothesis = await plan_fix_hypothesis(db_session, debug_session_id=session.id)
    patch = await generate_patch_for_hypothesis(db_session, hypothesis_id=hypothesis.id)

    # The patch resolves the timeout — but pushes memory up well past the
    # configured threshold. "Plausible" is not "verified" (§1).
    run = await _verify(
        db_session,
        patch,
        baseline={
            "error_rate": 0.082,
            "latency_p95_ms": 2900,
            "memory_mb": 200,
        },
        metrics={"error_rate": 0.006, "latency_p95_ms": 300, "memory_mb": 400},
        source_dir=snapshot.root_path,
    )
    assert run.status.value == "NOT_VERIFIED"
    assert run.regression_detected is True
    assert "regression detected" in run.verdict_reason
    assert any(
        "memory" in str(item).lower()
        for item in run.evidence["comparison"]["regressions"]
    )
    assert patch.status.value != "VERIFIED"
    # A regressing patch cannot be approved.
    with pytest.raises(FixWorkflowError):
        await record_review_action(
            db_session, patch_id=patch.id, action="APPROVE", actor="engineer"
        )


# ---------------------------------------------------------------------------
# §62 — a patch that tampers with the tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_test_tampering_is_refused_before_any_workspace(
    db_session, tmp_path
):
    project, incident, session, snapshot, root = await _demo_fixture(
        db_session, tmp_path
    )
    # Scope deliberately widens to include the test file, so the *only* reason
    # to refuse is the tampering itself — not an out-of-scope path.
    hypothesis = await plan_fix_hypothesis(
        db_session,
        debug_session_id=session.id,
        scope_override=[DEFECT_FILE, "tests/test_checkout.py"],
    )

    test_source = (root / "tests" / "test_checkout.py").read_text()
    tampering_diff = render_unified_diff(
        before=test_source, after="", path="tests/test_checkout.py"
    )

    patch = await generate_patch_for_hypothesis(
        db_session,
        hypothesis_id=hypothesis.id,
        patch_content=tampering_diff,
        generated_by="ai",
    )
    assert patch.status.value == "VALIDATION_FAILED", patch.failure_reason
    assert "tampering" in patch.failure_reason.lower()

    # The tampering patch never reached a workspace, so it can never verify.
    with pytest.raises(FixWorkflowError):
        await _verify(
            db_session,
            patch,
            metrics={},
            source_dir=snapshot.root_path,
        )


# ---------------------------------------------------------------------------
# §63 — a patch that does not fix the failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_non_fixing_patch_is_not_verified(db_session, tmp_path):
    project, incident, session, snapshot, root = await _demo_fixture(
        db_session, tmp_path
    )
    hypothesis = await plan_fix_hypothesis(db_session, debug_session_id=session.id)
    patch = await generate_patch_for_hypothesis(db_session, hypothesis_id=hypothesis.id)

    run = await _verify(
        db_session,
        patch,
        metrics={"error_rate": 0.006, "latency_p95_ms": 300},
        still_reproduces=True,
        source_dir=snapshot.root_path,
    )
    assert run.status.value == "NOT_VERIFIED"
    assert run.patched_failure_reproduced is True
    assert "still reproduces" in run.verdict_reason
    # The distinction §63 mandates: tests green, verification still refused.
    assert run.evidence["tests"]["exit_code"] == 0
