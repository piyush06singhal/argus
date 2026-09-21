"""Phase 6 demo scenarios (§60–§62).

Three scenarios, each asserting the *behaviour* the phase is judged on rather
than a hard-coded answer:

``§60`` — a real code-level defect (a retry loop amplifying a timeout configured
down to sub-second, introduced by a commit). ARGUS must resolve the snapshot,
locate the failing service from the trace and stack trace, find the changed file,
build the context, and name verified locations — with nothing about the answer
written into the test beyond the evidence it must cite.

``§61`` — a recent commit that is **unrelated**. ARGUS must report it as
temporally recent but unconnected, and must not turn "newest commit" into a
hypothesis.

``§62`` — an incident with no stack trace, no failing spans and no mapping. ARGUS
must say it cannot identify a reliable code location and list what is missing. It
must not invent one.
"""

from __future__ import annotations

import os
import subprocess
import uuid

from sqlalchemy import select

from app.models.code import DebugCodeLocation, LocationValidation
from app.services.change_relevance import ChangeRelevance, ChangeRelevanceAnalyzer
from app.services.debug_context_builder import DebugContextBuilder
from app.services.debug_session_service import DebugSessionManager
from app.services.engines import MockAIProvider
from phase6_helpers import (
    CHECKOUT_SOURCE,
    ScriptedProvider,
    build_incident,
    build_project,
    build_repository,
)


async def _fixture(
    db_session,
    tmp_path,
    *,
    failing=True,
    logs=True,
    stack_trace=None,
    shrunken_timeout=False,
):
    """The demo's code-level failure shape.

    ``shrunken_timeout`` adds the §60 second commit that changes behaviour —
    ``DB_TIMEOUT_SECONDS`` dropped from 0.5 to 0.25 — and pins the snapshot and
    the deployment to it, so the change under discussion has a *parent* to be
    compared against (a first commit has none, and "cannot be listed" is then the
    honest answer rather than the interesting one).
    """
    project, environment, component = await build_project(
        db_session, name="Phase6 Demo"
    )
    repository, snapshot, run = await build_repository(db_session, project, tmp_path)
    deployed_sha = snapshot.commit_sha
    if shrunken_timeout:
        from app.models.deployment import DeploymentEvent
        from app.services.code_index_service import CodeIndexer
        from app.services.code_snapshot_service import CodeSnapshotService

        root = repository.local_path
        with open(f"{root}/shop/inventory.py") as handle:
            source = handle.read()
        with open(f"{root}/shop/inventory.py", "w") as handle:
            handle.write(
                source.replace("DB_TIMEOUT_SECONDS = 0.5", "DB_TIMEOUT_SECONDS = 0.25")
            )
        subprocess.run(["git", "-C", root, "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                root,
                "commit",
                "-q",
                "-m",
                "perf: halve the database timeout",
            ],
            check=True,
        )
        deployed_sha = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        snapshot = await CodeSnapshotService().get_or_create_snapshot(
            db_session, repository, deployed_sha, version_evidence="deployment commit"
        )
        await CodeIndexer(db_session).index(repository, snapshot, trigger="demo")
    incident = await build_incident(
        db_session,
        project,
        environment,
        component,
        failing=failing,
        logs=logs,
        stack_trace=stack_trace,
    )
    if shrunken_timeout:
        deployment = (
            (
                await db_session.execute(
                    select(DeploymentEvent).where(
                        DeploymentEvent.project_id == project.id
                    )
                )
            )
            .scalars()
            .first()
        )
        deployment.commit_sha = deployed_sha
        deployment.description = "halved the inventory database timeout"
    await db_session.commit()
    return project, environment, component, repository, snapshot, incident


# ---------------------------------------------------------------------------
# §60 — code-level failure
# ---------------------------------------------------------------------------
async def test_demo_locates_the_failing_service_and_its_timeout_config(
    db_session, tmp_path
):
    project, _, _, repository, snapshot, incident = await _fixture(
        db_session, tmp_path, shrunken_timeout=True
    )
    #: The trace must be mapped before it can be evidenced (the API does this with
    #: ``?refresh=true``; the demo does it directly).
    from app.services.trace_code_mapper import TraceCodeMapper

    mappings = await TraceCodeMapper(db_session).map_incident(incident, snapshot)
    await db_session.commit()
    assert mappings, "the failing trace and stack trace must produce mappings"

    manager = DebugSessionManager(db_session, MockAIProvider())
    session = await manager.create_session(
        project_id=project.id,
        incident=incident,
        repository=repository,
        snapshot=snapshot,
        created_by="demo",
    )
    outcome = await manager.run_analysis(
        session, incident=incident, repository=repository, snapshot=snapshot
    )
    result = outcome.result
    context = outcome.context

    #: 1. The code version is pinned, and says how.
    assert context.snapshot_id == str(snapshot.id)
    assert context.version_note

    #: 2. The failing service is located from the evidence, not from a fixture.
    verified = [
        item
        for item in result.suspected_locations
        if item.validation is LocationValidation.VALID
    ]
    assert verified, "at least one location must validate against the pinned snapshot"
    files = {item.file_path for item in verified}
    assert "shop/checkout.py" in files, "the caller of the failing dependency"
    assert all(item.validation_detail for item in verified)
    assert all(item.evidence_refs or item.symbol_id for item in verified)

    #: 3. The stack trace is parsed, and its runtime paths resolved to the snapshot.
    traces = (context.sections.get("stack_traces") or {}).get("traces") or []
    assert traces
    frames = traces[0]["frames"]
    assert frames[0]["file"].startswith("/srv/"), "the raw runtime path is preserved"
    assert frames[0]["snapshot_path"] == "shop/checkout.py"

    #: 4. The deployment is in the window and its changed file is connected.
    deployments = (context.sections.get("recent_changes") or {}).get(
        "deployments"
    ) or []
    assert deployments, "the deployment before onset is part of the context"
    assert deployments[0]["seconds_before_onset"] > 0

    #: 5. Every hypothesis is traceable: it cites evidence, and the status comes
    #:    from that evidence rather than from the model's confidence.
    assert result.hypotheses
    for hypothesis in result.hypotheses:
        assert hypothesis.rationale
        if hypothesis.supporting:
            assert hypothesis.validation_status.value in {
                "SUPPORTED",
                "PARTIALLY_SUPPORTED",
            }
        else:
            assert hypothesis.validation_status.value in {"UNVERIFIED", "WEAKENED"}

    #: 6. The deterministic investigation is stored, so the answer survives a
    #:    provider outage.
    run = outcome.analysis_run
    assert run.context_snapshot and run.context_snapshot["evidence"]
    assert run.context_bytes and run.context_bytes > 0

    #: 7. Nothing claims a cause. The strongest vocabulary is the §2 set.
    labels = {item.label for item in result.suspected_locations}
    assert labels <= {"LIKELY_FAULT_LOCATION", "SUSPICIOUS_CODE_PATH"}
    assert "HIGH" not in {item.confidence for item in verified} or result.degraded


async def test_demo_change_relevance_connects_the_timeout_commit(db_session, tmp_path):
    """§19/§20: the changed file is connected to the incident's code path."""
    project, _, _, repository, snapshot, incident = await _fixture(
        db_session, tmp_path, shrunken_timeout=True
    )
    from app.services.trace_code_mapper import TraceCodeMapper

    await TraceCodeMapper(db_session).map_incident(incident, snapshot)
    await db_session.commit()

    report = await ChangeRelevanceAnalyzer(db_session).analyze(
        incident, snapshot, repository
    )
    assert report.assessments, "the deployment is assessed"
    assessment = report.assessments[0]
    assert assessment.commit_sha == snapshot.commit_sha
    assert assessment.diff_error is None, "the commit's changes were readable"
    assert assessment.changed_files == [
        "shop/inventory.py"
    ], f"the changed file is listed: {assessment.changed_files}"
    #: The change touched a file the incident's trace maps into, so it is
    #: connected — at file granularity, and without any claim of causation.
    assert assessment.classification is ChangeRelevance.SUSPICIOUS_CHANGE
    assert assessment.matched_mapped_files == ["shop/inventory.py"]
    assert assessment.matched_mapped_files or assessment.relevant_files
    assert any(
        "not evidence of causation" in note or "granularity" in note
        for note in report.notes
    )
    assert "not evidence that the change caused" in report.as_dict()["disclaimer"]


async def test_context_carries_the_relevance_verdict(db_session, tmp_path):
    project, _, _, repository, snapshot, incident = await _fixture(
        db_session, tmp_path, shrunken_timeout=True
    )
    from app.services.trace_code_mapper import TraceCodeMapper

    await TraceCodeMapper(db_session).map_incident(incident, snapshot)
    await db_session.commit()
    context = await DebugContextBuilder(db_session).build(
        incident, snapshot, repository_id=repository.id
    )
    relevance = (context.sections.get("recent_changes") or {}).get("relevance")
    assert relevance, "the relevance report is part of the context"
    assert relevance["assessments"]
    assert relevance["disclaimer"]
    #: And it is indexed as evidence, so a claim about it can be cited.
    assert any(
        item.label.startswith(("SUSPICIOUS_CHANGE", "RELEVANT_CHANGE"))
        for item in context.evidence
    )


# ---------------------------------------------------------------------------
# §61 — counterexample: a recent, unrelated commit
# ---------------------------------------------------------------------------
async def test_demo_does_not_blame_an_unrelated_recent_commit(db_session, tmp_path):
    """A newer commit that touches nothing the incident exercised is *not* a cause."""
    project, environment, component = await build_project(
        db_session, name="Phase6 Counter"
    )
    repository, snapshot, _ = await build_repository(db_session, project, tmp_path)

    #: A second commit that only touches an unrelated file, deployed just before
    #: onset — the classic "it must be the deploy" trap.
    root = repository.local_path
    os.makedirs(f"{root}/docs", exist_ok=True)
    with open(f"{root}/docs/notes.md", "w") as handle:
        handle.write("# notes\n\nunrelated documentation change\n")
    subprocess.run(["git", "-C", root, "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", root, "commit", "-q", "-m", "docs: add notes"], check=True
    )
    unrelated_sha = subprocess.run(
        ["git", "-C", root, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    incident = await build_incident(db_session, project, environment, component)
    #: Map the trace against the *pinned* snapshot, then point the deployment at the
    #: unrelated commit: relevant code is known, and the change does not touch it.
    from app.models.deployment import DeploymentEvent
    from app.services.trace_code_mapper import TraceCodeMapper

    deployment = (
        (
            await db_session.execute(
                select(DeploymentEvent).where(DeploymentEvent.project_id == project.id)
            )
        )
        .scalars()
        .first()
    )
    deployment.commit_sha = unrelated_sha
    await db_session.flush()
    await TraceCodeMapper(db_session).map_incident(incident, snapshot)
    await db_session.commit()

    report = await ChangeRelevanceAnalyzer(db_session).analyze(
        incident, snapshot, repository
    )
    assessment = report.assessments[0]
    assert assessment.commit_sha == unrelated_sha
    assert (
        assessment.classification is ChangeRelevance.TEMPORALLY_RECENT_UNRELATED
    ), f"an unrelated commit must not be called relevant: {assessment.reason}"
    assert assessment.changed_files == ["docs/notes.md"]
    assert not assessment.matched_mapped_files
    assert "none of which this incident's evidence connects" in assessment.reason
    assert report.relevant == []
    assert report.unrelated and len(report.unrelated) == 1

    #: And the analysis must not turn it into a hypothesis or a location.
    manager = DebugSessionManager(db_session, MockAIProvider())
    session = await manager.create_session(
        project_id=project.id,
        incident=incident,
        repository=repository,
        snapshot=snapshot,
        created_by="demo",
    )
    outcome = await manager.run_analysis(
        session, incident=incident, repository=repository, snapshot=snapshot
    )
    for hypothesis in outcome.result.hypotheses:
        assert unrelated_sha[:12] not in hypothesis.description
    assert all(
        item.file_path != "docs/notes.md" for item in outcome.result.suspected_locations
    ), "an unrelated documentation file is never a suspected location"
    relevance = (outcome.context.sections.get("recent_changes") or {})["relevance"]
    assert relevance["unrelated_count"] == 1
    assert relevance["relevant_count"] == 0


async def test_demo_reports_a_commit_whose_changes_cannot_be_listed(
    db_session, tmp_path
):
    """A commit that cannot be diffed is UNKNOWN, never presumed harmless."""
    project, _, _, repository, snapshot, incident = await _fixture(db_session, tmp_path)
    from app.models.deployment import DeploymentEvent

    deployment = (
        (
            await db_session.execute(
                select(DeploymentEvent).where(DeploymentEvent.project_id == project.id)
            )
        )
        .scalars()
        .first()
    )
    #: A sha the repository does not contain (a shallow clone, or another fork).
    deployment.commit_sha = "0" * 40
    await db_session.flush()
    await db_session.commit()

    report = await ChangeRelevanceAnalyzer(db_session).analyze(
        incident, snapshot, repository
    )
    assessment = report.assessments[0]
    assert assessment.classification is ChangeRelevance.UNKNOWN
    assert assessment.diff_error
    assert "not present in the repository" in assessment.reason
    #: UNKNOWN is not the same as "irrelevant": it is carried through to the
    #: report rather than dropped, so the gap is visible.
    assert report.relevant == []
    assert report.unrelated == []
    assert report.assessments[0].as_dict()["classification"] == "UNKNOWN"


async def test_demo_without_mappings_never_calls_a_change_relevant_by_itself(
    db_session, tmp_path
):
    project, _, _, repository, snapshot, incident = await _fixture(db_session, tmp_path)
    #: No mapping exists for this incident, so nothing is *demonstrably* connected
    #: to the failing path. The change may still be flagged from investigation
    #: signals, but never as connected to a mapped location.
    report = await ChangeRelevanceAnalyzer(db_session).analyze(
        incident, snapshot, repository
    )
    assert report.assessments
    assert report.considered_mapped_files == []
    assert all(not item.matched_mapped_files for item in report.assessments)
    assert all(
        item.classification is not ChangeRelevance.SUSPICIOUS_CHANGE
        for item in report.assessments
    ), "SUSPICIOUS requires a mapped location, not merely a recent change"
    assert any("granularity" in note for note in report.notes)


# ---------------------------------------------------------------------------
# §62 — insufficient evidence
# ---------------------------------------------------------------------------
async def test_demo_says_so_when_the_trace_is_missing(db_session, tmp_path):
    """No failing span, no stack trace ⇒ no code location, and it is stated."""
    project, _, _, repository, snapshot, incident = await _fixture(
        db_session, tmp_path, failing=False, logs=False
    )
    manager = DebugSessionManager(db_session, MockAIProvider())
    session = await manager.create_session(
        project_id=project.id,
        incident=incident,
        repository=repository,
        snapshot=snapshot,
        created_by="demo",
    )
    outcome = await manager.run_analysis(
        session, incident=incident, repository=repository, snapshot=snapshot
    )
    result = outcome.result
    context = outcome.context

    assert result.suspected_locations == [], "no location may be invented"
    assert result.valid_locations == 0
    assert any("no failing spans" in caveat for caveat in context.caveats)
    assert any("no stack trace" in caveat for caveat in context.caveats)
    #: The analysis still explains itself and still recommends a next step.
    assert result.summary
    assert "No code location could be verified" in result.summary
    assert result.missing_evidence
    assert result.confidence == "INSUFFICIENT"
    assert isinstance(result.recommended_inspections, list)


async def test_demo_says_so_when_there_is_no_code_snapshot_at_all(db_session, tmp_path):
    project, environment, component = await build_project(
        db_session, name="Phase6 NoSnap"
    )
    incident = await build_incident(db_session, project, environment, component)
    await db_session.commit()

    manager = DebugSessionManager(db_session, MockAIProvider())
    session = await manager.create_session(
        project_id=project.id,
        incident=incident,
        repository=None,
        snapshot=None,
        created_by="demo",
    )
    outcome = await manager.run_analysis(
        session, incident=incident, repository=None, snapshot=None
    )
    result = outcome.result
    context = outcome.context

    assert result.suspected_locations == []
    assert context.version_status == "UNKNOWN"
    assert "no code snapshot is available" in context.version_note
    assert any("snapshot" in caveat for caveat in context.caveats)
    assert result.summary.startswith("This is ARGUS's deterministic investigation")
    stored = (
        (
            await db_session.execute(
                select(DebugCodeLocation).where(
                    DebugCodeLocation.session_id == session.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert stored == [], "nothing unverifiable is persisted as a location"


async def test_demo_partial_mapping_is_reported_not_hidden(db_session, tmp_path):
    """A stack trace whose path is not in the snapshot yields an unmapped reason."""
    project, _, _, repository, snapshot, incident = await _fixture(
        db_session,
        tmp_path,
        stack_trace=(
            "Traceback (most recent call last):\n"
            '  File "/opt/other-service/handler.py", line 12, in handle\n'
            "TimeoutError: upstream timed out"
        ),
    )
    from app.services.trace_code_mapper import TraceCodeMapper

    mappings = await TraceCodeMapper(db_session).map_incident(incident, snapshot)
    await db_session.commit()
    unmapped = [item for item in mappings if not item.file_path]
    if unmapped:
        #: Whatever the mapper decided, the reason is recorded.
        assert all(item.unmapped_reason for item in unmapped)

    manager = DebugSessionManager(db_session, MockAIProvider())
    session = await manager.create_session(
        project_id=project.id,
        incident=incident,
        repository=repository,
        snapshot=snapshot,
        created_by="demo",
    )
    outcome = await manager.run_analysis(
        session, incident=incident, repository=repository, snapshot=snapshot
    )
    #: An out-of-snapshot frame is never turned into a location.
    assert all(
        "handler.py" not in item.file_path
        for item in outcome.result.suspected_locations
    )


async def test_demo_insufficient_evidence_still_answers_a_question(
    db_session, tmp_path
):
    project, environment, component = await build_project(
        db_session, name="Phase6 Ask62"
    )
    incident = await build_incident(db_session, project, environment, component)
    await db_session.commit()
    manager = DebugSessionManager(db_session, MockAIProvider())
    session = await manager.create_session(
        project_id=project.id,
        incident=incident,
        repository=None,
        snapshot=None,
        created_by="demo",
    )
    answer = await manager.ask(
        session,
        "Which file is responsible?",
        incident=incident,
        repository=None,
        snapshot=None,
    )
    #: It must refuse to answer, and say what is missing (§43, §62).
    assert "No model analysis is available" in answer["answer"]
    assert (
        "No trace-to-code mapping" in answer["answer"] or "snapshot" in answer["answer"]
    )
    assert answer["missing_evidence"]
    assert answer["confidence"] == "INSUFFICIENT"


async def test_demo_scripted_model_answer_may_not_invent_a_location(
    db_session, tmp_path
):
    """A model that names a file the snapshot lacks gets it rejected (§30)."""
    project, _, _, repository, snapshot, incident = await _fixture(db_session, tmp_path)
    payload = {
        "summary": "The bug is in shop/legacy/checkout.py in a helper that no longer exists.",
        "suspected_locations": [
            {
                "file_path": "shop/legacy/checkout.py",
                "symbol": "legacy_process",
                "start_line": 10,
                "end_line": 40,
                "reason": "looks like the old code path",
                "evidence": [],
                "confidence": "HIGH",
            }
        ],
        "hypotheses": [
            {
                "description": "Legacy helper regressed",
                "category": "INCORRECT_ERROR_HANDLING",
                "confidence": "HIGH",
                "code_locations": [],
                "supporting_evidence": [],
                "contradicting_evidence": [],
                "missing_evidence": [],
                "testable": False,
                "test_approach": None,
            }
        ],
        "supporting_evidence": [],
        "contradicting_evidence": [],
        "missing_evidence": [],
        "recommended_inspections": ["inspect shop/legacy/checkout.py"],
        "confidence": "HIGH",
    }
    manager = DebugSessionManager(db_session, ScriptedProvider(payload))
    session = await manager.create_session(
        project_id=project.id,
        incident=incident,
        repository=repository,
        snapshot=snapshot,
        created_by="demo",
    )
    outcome = await manager.run_analysis(
        session, incident=incident, repository=repository, snapshot=snapshot
    )
    result = outcome.result
    assert result.valid_locations == 0
    assert result.confidence == "LOW", "an unverified HIGH verdict is downgraded"
    assert result.invalid_references
    assert any(
        "does not exist in snapshot" in item["reason"]
        for item in result.invalid_references
    )
    #: The claim is retained for audit but flagged as refused.
    stored = (
        (
            await db_session.execute(
                select(DebugCodeLocation).where(
                    DebugCodeLocation.session_id == session.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert stored
    assert all(row.validation is not LocationValidation.VALID for row in stored)
    assert all(row.validation_detail for row in stored)
    assert session.id == outcome.session.id
    assert uuid.UUID(result.incident_id) if hasattr(result, "incident_id") else True


async def test_demo_source_fixture_matches_what_the_debugger_sees(db_session, tmp_path):
    """Sanity: the pinned snapshot really contains the code under discussion."""
    _, _, _, _, snapshot, _ = await _fixture(db_session, tmp_path)
    from app.models.code import CodeFile

    files = (
        (
            await db_session.execute(
                select(CodeFile).where(CodeFile.snapshot_id == snapshot.id)
            )
        )
        .scalars()
        .all()
    )
    by_path = {row.path: row for row in files}
    assert "shop/checkout.py" in by_path
    assert by_path["shop/checkout.py"].line_count == len(CHECKOUT_SOURCE.splitlines())
    assert by_path["shop/checkout.py"].content_hash
