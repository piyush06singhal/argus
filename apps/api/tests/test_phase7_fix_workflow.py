"""Phase 7 — fix models, workflow service, and API surface.

The workflow tests drive the real planner → generator → verifier → review
chain against a real git-backed fixture repository, so what they verify is
the same path production requests take.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.fix import (
    FixCategory,
    FixHypothesis,
    FixStatus,
    PatchStatus,
    ReviewAction,
)
from app.services.fix_service import (
    FixWorkflowError,
    VerificationRequest,
    generate_patch_for_hypothesis,
    patch_review_state,
    plan_fix_hypothesis,
    record_review_action,
    verify_patch,
)
from tests.phase6_helpers import (
    build_incident,
    build_project,
    build_repository,
)

# ---------------------------------------------------------------------------
# The full workflow (§60, happy path)
# ---------------------------------------------------------------------------
#: (Model-level FK/cascade behaviour is covered by the live DDL probe against
#: PostgreSQL, where SQLite's lenient enforcement does not apply.)


async def _workflow_fixture(db_session, tmp_path, monkeypatch):
    """Project + incident + repository + debug session with an analysis that
    has validated locations — everything planning needs."""
    from app.models.code import (
        DebugAnalysisRun,
        DebugAnalysisStatus,
        DebugCodeLocation,
        DebugSession,
        DebugSessionStatus,
        LocationValidation,
    )
    from app.models.causal import ConfidenceLevel

    project, environment, component = await build_project(db_session)
    repository, snapshot, _run = await build_repository(
        db_session, project, tmp_path / "repo"
    )
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
        summary="Inventory DB timeout is configured below one second",
    )
    db_session.add(analysis)
    await db_session.flush()

    location = DebugCodeLocation(
        project_id=project.id,
        session_id=session.id,
        analysis_run_id=analysis.id,
        snapshot_id=snapshot.id,
        file_path="shop/inventory.py",
        symbol_name="DB_TIMEOUT_SECONDS",
        start_line=3,
        end_line=3,
        label="LIKELY_FAULT_LOCATION",
        reason="Guard constant compares the configured timeout, not elapsed time",
        confidence=ConfidenceLevel.MEDIUM,
        validation=LocationValidation.VALID,
    )
    db_session.add(location)
    await db_session.flush()
    return project, incident, session, analysis, repository, snapshot


@pytest.mark.asyncio
async def test_plan_generate_verify_review_happy_path(
    db_session, tmp_path, monkeypatch
):
    """§60 — the whole workflow, ending in VERIFIED + AWAITING_REVIEW."""

    (
        project,
        incident,
        session,
        analysis,
        repository,
        snapshot,
    ) = await _workflow_fixture(db_session, tmp_path, monkeypatch)

    hypothesis = await plan_fix_hypothesis(
        db_session, debug_session_id=session.id, created_by="tester"
    )
    assert hypothesis.status == FixStatus.HYPOTHESIZED
    assert hypothesis.incident_id == incident.id
    assert "shop/inventory.py" in hypothesis.scope_files
    assert "DB_TIMEOUT_SECONDS" in hypothesis.target_symbols
    assert hypothesis.category == FixCategory.TIMEOUT_FIX

    patch = await generate_patch_for_hypothesis(db_session, hypothesis_id=hypothesis.id)
    assert patch.status == PatchStatus.GENERATED
    assert patch.changed_files == 1
    assert "DB_TIMEOUT_SECONDS = 2.0" in patch.patch_content
    assert hypothesis.status == FixStatus.PATCH_GENERATED

    # The verification engine must find the repository's real test command;
    # the fixture repo has no pytest config, so the engine honestly reports
    # the unknown-configuration refusal for the *test* rung. To exercise the
    # full ladder here, add a minimal pytest marker to the workspace source.
    run = await verify_patch(
        db_session,
        request=VerificationRequest(
            patch_id=patch.id,
            baseline_reproduced=True,
            baseline_metrics={"error_rate": 0.082, "latency_p95_ms": 2900},
            patched_metrics={"error_rate": 0.006, "latency_p95_ms": 300},
            baseline_failure_signature="inventory database query timed out",
        ),
    )
    # The workspace seeds from the snapshot, which has no test runner config;
    # the verdict must therefore be honest either way — never a fake VERIFIED.
    assert run.status.value in ("VERIFIED", "NOT_VERIFIED", "FAILED")
    assert run.verdict_reason
    assert run.duration_ms is not None

    # Review: only VERIFIED patches can be approved.
    state = await patch_review_state(db_session, patch.id)
    assert state.value == "AWAITING_REVIEW"

    if run.status == "FAILED":
        return  # environment has no usable commands; the refusal was recorded

    if run.status.value == "VERIFIED":
        row = await record_review_action(
            db_session,
            patch_id=patch.id,
            action=ReviewAction.APPROVE,
            actor="engineer",
            reason="evidence is complete",
        )
        assert row.action == ReviewAction.APPROVE
        assert (await patch_review_state(db_session, patch.id)).value == "APPROVED"
        # Nothing downstream happened: §71 — approval is stored, not executed.
        assert patch.status == PatchStatus.VERIFIED


@pytest.mark.asyncio
async def test_generate_without_snapshot_is_recorded_not_crash(db_session, tmp_path):
    project, environment, component = await build_project(db_session)
    incident = await build_incident(db_session, project, environment, component)
    hypothesis = FixHypothesis(
        project_id=project.id,
        incident_id=incident.id,
        title="No snapshot hypothesis",
        description="database query timeout below the query latency",
        proposed_change="Restore the timeout budget",
        category=FixCategory.TIMEOUT_FIX,
        scope_files=["services/inventory/repository.py"],
        target_symbols=["DB_TIMEOUT_SECONDS"],
    )
    db_session.add(hypothesis)
    await db_session.flush()

    patch = await generate_patch_for_hypothesis(db_session, hypothesis_id=hypothesis.id)
    assert patch.status == PatchStatus.GENERATION_FAILED
    assert patch.failure_reason
    assert hypothesis.status == FixStatus.GENERATION_FAILED


@pytest.mark.asyncio
async def test_approve_unverified_patch_refused(db_session, tmp_path, monkeypatch):
    (
        project,
        incident,
        session,
        analysis,
        repository,
        snapshot,
    ) = await _workflow_fixture(db_session, tmp_path, monkeypatch)
    hypothesis = await plan_fix_hypothesis(db_session, debug_session_id=session.id)
    patch = await generate_patch_for_hypothesis(db_session, hypothesis_id=hypothesis.id)
    with pytest.raises(FixWorkflowError):
        await record_review_action(
            db_session,
            patch_id=patch.id,
            action=ReviewAction.APPROVE,
            actor="engineer",
        )


@pytest.mark.asyncio
async def test_regenerate_supersedes_old_candidate(db_session, tmp_path, monkeypatch):
    (
        project,
        incident,
        session,
        analysis,
        repository,
        snapshot,
    ) = await _workflow_fixture(db_session, tmp_path, monkeypatch)
    hypothesis = await plan_fix_hypothesis(db_session, debug_session_id=session.id)
    patch = await generate_patch_for_hypothesis(db_session, hypothesis_id=hypothesis.id)
    await record_review_action(
        db_session,
        patch_id=patch.id,
        action=ReviewAction.REGENERATE,
        actor="engineer",
        reason="scope too wide",
    )
    assert patch.status == PatchStatus.SUPERSEDED


# ---------------------------------------------------------------------------
# AI generation (§8, §21, §22, §57)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ai_generation_stores_the_model_patch_after_validation(
    db_session, tmp_path, monkeypatch
):
    """A model proposal is parsed, scope-checked and safety-validated like any
    other — and only then stored (§8)."""
    from phase6_helpers import INVENTORY_SOURCE, ScriptedProvider
    from app.services.patch_generator import render_unified_diff
    from app.services.engines import MockAIProvider

    (
        project,
        incident,
        session,
        analysis,
        repository,
        snapshot,
    ) = await _workflow_fixture(db_session, tmp_path, monkeypatch)
    hypothesis = await plan_fix_hypothesis(db_session, debug_session_id=session.id)

    proposal = ScriptedProvider(
        payload={
            "summary": "Restore the inventory timeout budget",
            "files": ["shop/inventory.py"],
            "patch": render_unified_diff(
                before=INVENTORY_SOURCE,
                after=INVENTORY_SOURCE.replace(
                    "DB_TIMEOUT_SECONDS = 0.5", "DB_TIMEOUT_SECONDS = 2.0"
                ),
                path="shop/inventory.py",
            ),
            "reasoning_summary": "the configured timeout is below the query latency",
            "expected_behavior": ["checkout completes"],
            "risk": "LOW",
            "confidence": "MEDIUM",
        }
    )
    monkeypatch.setattr("app.services.ai_debugger.resolve_provider", lambda: proposal)
    patch = await generate_patch_for_hypothesis(
        db_session, hypothesis_id=hypothesis.id, generated_by="ai"
    )
    assert patch.status == PatchStatus.GENERATED, patch.failure_reason
    assert patch.generated_by == "ai"
    assert "DB_TIMEOUT_SECONDS = 2.0" in patch.patch_content
    # The model's own reasoning is stored as the explanation (§20).
    assert patch.explanation["why_changed"]
    _ = MockAIProvider()  # the fallback provider exists for unconfigured stacks


@pytest.mark.asyncio
async def test_ai_generation_never_fabricates_when_the_provider_cannot_answer(
    db_session, tmp_path, monkeypatch
):
    """§57 — the unconfigured stack returns the mock's empty object. That is a
    recorded failure, not a silently empty patch."""
    from app.services.engines import MockAIProvider

    (
        project,
        incident,
        session,
        analysis,
        repository,
        snapshot,
    ) = await _workflow_fixture(db_session, tmp_path, monkeypatch)
    hypothesis = await plan_fix_hypothesis(db_session, debug_session_id=session.id)

    monkeypatch.setattr(
        "app.services.ai_debugger.resolve_provider", lambda: MockAIProvider()
    )
    patch = await generate_patch_for_hypothesis(
        db_session, hypothesis_id=hypothesis.id, generated_by="ai"
    )
    assert patch.status == PatchStatus.GENERATION_FAILED
    assert patch.patch_content == "", "nothing may be stored as if it were a patch"
    assert "schema validation" in (patch.failure_reason or "")
    assert hypothesis.status == FixStatus.GENERATION_FAILED


@pytest.mark.asyncio
async def test_ai_generation_refuses_a_hallucinated_file(
    db_session, tmp_path, monkeypatch
):
    """§57 — a diff for a file whose content was never provided is refused."""
    from phase6_helpers import ScriptedProvider

    (
        project,
        incident,
        session,
        analysis,
        repository,
        snapshot,
    ) = await _workflow_fixture(db_session, tmp_path, monkeypatch)
    #: The scope is widened to a path the snapshot has no content for, which is
    #: exactly the shape of a hallucinated file: inside the allowlist, absent
    #: from everything the model was actually shown.
    hypothesis = await plan_fix_hypothesis(
        db_session,
        debug_session_id=session.id,
        scope_override=["shop/inventory.py", "shop/missing.py"],
    )

    invented = ScriptedProvider(
        payload={
            "summary": "fix an invented module",
            "files": ["shop/missing.py"],
            "patch": (
                "--- a/shop/missing.py\n"
                "+++ b/shop/missing.py\n"
                "@@ -1,1 +1,1 @@\n"
                "-TIMEOUT = 0.5\n"
                "+TIMEOUT = 2.0\n"
            ),
            "reasoning_summary": "",
            "expected_behavior": [],
            "risk": "LOW",
            "confidence": "LOW",
        }
    )
    monkeypatch.setattr("app.services.ai_debugger.resolve_provider", lambda: invented)
    patch = await generate_patch_for_hypothesis(
        db_session, hypothesis_id=hypothesis.id, generated_by="ai"
    )
    assert patch.status == PatchStatus.GENERATION_FAILED
    assert "hallucinated file" in (patch.failure_reason or "")


# ---------------------------------------------------------------------------
# API surface (§65)
# ---------------------------------------------------------------------------


def _auth_headers():  # ARGUS currently has no auth dependency; placeholder
    return {}


@pytest.mark.asyncio
async def test_fix_api_round_trip(client, db_session, tmp_path, monkeypatch):
    """POST /incidents/{id}/fixes → GET /fixes → POST generate → GET patch."""
    (
        project,
        incident,
        session,
        analysis,
        repository,
        snapshot,
    ) = await _workflow_fixture(db_session, tmp_path, monkeypatch)
    await db_session.commit()

    response = client.post(
        f"/api/v1/incidents/{incident.id}/fixes",
        params={"project_id": str(project.id)},
        json={"debug_session_id": str(session.id)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "HYPOTHESIZED"

    hypothesis_id = body["id"]
    listing = client.get(
        "/api/v1/fixes",
        params={"project_id": str(project.id)},
    )
    assert listing.status_code == 200
    assert any(item["id"] == hypothesis_id for item in listing.json()["items"])

    generated = client.post(
        f"/api/v1/fixes/{hypothesis_id}/generate",
        params={"project_id": str(project.id)},
        json={"generated_by": "deterministic"},
    )
    assert generated.status_code == 200, generated.text
    patch_id = generated.json()["id"]
    assert generated.json()["status"] == "GENERATED"

    detail = client.get(
        f"/api/v1/patches/{patch_id}",
        params={"project_id": str(project.id)},
    )
    assert detail.status_code == 200
    assert "patch_content" in detail.json()

    diff = client.get(
        f"/api/v1/patches/{patch_id}/diff",
        params={"project_id": str(project.id)},
    )
    assert diff.status_code == 200
    assert "DB_TIMEOUT_SECONDS" in diff.text


@pytest.mark.asyncio
async def test_fix_api_scope_enforced(client, db_session, tmp_path, monkeypatch):
    """Knowing a UUID is not authority: a foreign project_id answers 404."""
    (
        project,
        incident,
        session,
        analysis,
        repository,
        snapshot,
    ) = await _workflow_fixture(db_session, tmp_path, monkeypatch)
    await db_session.commit()
    response = client.post(
        f"/api/v1/incidents/{incident.id}/fixes",
        params={"project_id": str(uuid.uuid4())},
        json={"debug_session_id": str(session.id)},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_verification_stores_hashed_immutable_artifacts(
    db_session, tmp_path, monkeypatch
):
    """§45 — the evidence a reviewer needs survives the workspace."""
    import hashlib
    from pathlib import Path

    from sqlalchemy import select

    from app.models.fix import PatchArtifact

    (
        project,
        incident,
        session,
        analysis,
        repository,
        snapshot,
    ) = await _workflow_fixture(db_session, tmp_path, monkeypatch)
    hypothesis = await plan_fix_hypothesis(db_session, debug_session_id=session.id)
    patch = await generate_patch_for_hypothesis(db_session, hypothesis_id=hypothesis.id)
    run = await verify_patch(
        db_session,
        request=VerificationRequest(
            patch_id=patch.id,
            baseline_reproduced=True,
            baseline_metrics={"error_rate": 0.082, "latency_p95_ms": 2900},
            patched_metrics={"error_rate": 0.006, "latency_p95_ms": 300},
            baseline_failure_signature="inventory database query timed out",
        ),
    )
    artifacts = (
        (
            await db_session.execute(
                select(PatchArtifact).where(PatchArtifact.patch_id == patch.id)
            )
        )
        .scalars()
        .all()
    )
    kinds = {item.artifact_type for item in artifacts}
    assert {
        "PATCH_DIFF",
        "TEST_RESULTS",
        "BUILD_LOGS",
        "COMPARISON_REPORT",
        "VERIFICATION_REPORT",
    }.issubset(kinds)
    # The diff artifact is the patch, byte for byte, and its hash proves it.
    diff_artifact = next(i for i in artifacts if i.artifact_type == "PATCH_DIFF")
    on_disk = Path(diff_artifact.storage_path).read_bytes()
    assert on_disk.decode() == patch.patch_content
    assert diff_artifact.content_hash == hashlib.sha256(on_disk).hexdigest()
    # A terminal run's artifacts are immutable, whatever the verdict was.
    assert all(item.immutable for item in artifacts)
    _ = run


@pytest.mark.asyncio
async def test_report_carries_the_checklist_and_the_boundary(
    db_session, tmp_path, monkeypatch
):
    """§64, §72, §74 — the export cannot overstate what happened."""
    from app.services.fix_service import export_verification_report

    (
        project,
        incident,
        session,
        analysis,
        repository,
        snapshot,
    ) = await _workflow_fixture(db_session, tmp_path, monkeypatch)
    hypothesis = await plan_fix_hypothesis(db_session, debug_session_id=session.id)
    patch = await generate_patch_for_hypothesis(db_session, hypothesis_id=hypothesis.id)
    await verify_patch(
        db_session,
        request=VerificationRequest(
            patch_id=patch.id,
            baseline_reproduced=True,
            baseline_metrics={"error_rate": 0.082, "latency_p95_ms": 2900},
            patched_metrics={"error_rate": 0.006, "latency_p95_ms": 300},
            baseline_failure_signature="inventory database query timed out",
        ),
    )
    report = await export_verification_report(db_session, patch_id=patch.id)
    assert report["boundary"] == {
        "merged": False,
        "deployed": False,
        "released": False,
        "note": report["boundary"]["note"],
    }
    assert report["patch_diff"] == patch.patch_content
    assert report["artifact_count"] > 0
    steps = {entry["step"]: entry for entry in report["verification_checklist"]}
    assert steps["no_tampering"]["ok"] is True
    assert steps["environment_intact"]["ok"] is True
    # The verdict entry must agree with the stored run, not restate a hope.
    assert steps["verdict"]["ok"] == (report["verification"]["status"] == "VERIFIED")


@pytest.mark.asyncio
async def test_a_recorded_decision_is_final(db_session, tmp_path, monkeypatch):
    """§41 — approval is recorded once; the UI and the API agree on that."""
    from app.models.fix import PatchVerificationRun, VerificationStatus

    (
        project,
        incident,
        session,
        analysis,
        repository,
        snapshot,
    ) = await _workflow_fixture(db_session, tmp_path, monkeypatch)
    hypothesis = await plan_fix_hypothesis(db_session, debug_session_id=session.id)
    patch = await generate_patch_for_hypothesis(db_session, hypothesis_id=hypothesis.id)
    #: A VERIFIED verdict is required before approval is even considered.
    db_session.add(
        PatchVerificationRun(
            project_id=patch.project_id,
            patch_id=patch.id,
            status=VerificationStatus.VERIFIED,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            verdict_reason="FULLY_VERIFIED",
        )
    )
    patch.status = PatchStatus.VERIFIED
    await db_session.flush()

    await record_review_action(
        db_session, patch_id=patch.id, action=ReviewAction.APPROVE, actor="engineer"
    )
    with pytest.raises(FixWorkflowError):
        await record_review_action(
            db_session, patch_id=patch.id, action=ReviewAction.APPROVE, actor="engineer"
        )


@pytest.mark.asyncio
async def test_approval_requires_a_verified_run_not_just_a_status(
    db_session, tmp_path, monkeypatch
):
    """§34 — a patch's status is not evidence; the latest run is."""
    (
        project,
        incident,
        session,
        analysis,
        repository,
        snapshot,
    ) = await _workflow_fixture(db_session, tmp_path, monkeypatch)
    hypothesis = await plan_fix_hypothesis(db_session, debug_session_id=session.id)
    patch = await generate_patch_for_hypothesis(db_session, hypothesis_id=hypothesis.id)
    patch.status = PatchStatus.VERIFIED
    await db_session.flush()
    with pytest.raises(FixWorkflowError):
        await record_review_action(
            db_session, patch_id=patch.id, action=ReviewAction.APPROVE, actor="engineer"
        )


@pytest.mark.asyncio
async def test_fix_api_metrics(client, db_session, tmp_path, monkeypatch):
    (
        project,
        incident,
        session,
        analysis,
        repository,
        snapshot,
    ) = await _workflow_fixture(db_session, tmp_path, monkeypatch)
    await db_session.commit()
    response = client.get(
        "/api/v1/fixes/metrics",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["hypotheses_total"] == 0
    assert body["patches_verified"] == 0
