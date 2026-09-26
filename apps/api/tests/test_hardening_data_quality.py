"""ARGUS hardening W5 — the checks the second audit pass added.

Phase 11's data-quality center asked whether a row still points at something
that exists. These six checks ask the harder questions: is the row *internally
possible*, is it *traceable*, is it *recorded*, and are its stored bytes still
what its hash claims?

Each test builds the exact defect and asserts the exact finding — including that
the finding names the row an operator has to act on, because a finding without a
subject is a log line, not a work item.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete

from app.models.incident import (
    Incident,
    IncidentTimelineEvent,
    TimelineEventType,
)
from app.models.intelligence import DataProvenance, ReliabilityExperience
from app.models.reproduction import (
    ArtifactType,
    ConfidenceLevel,
    ExperimentStatus,
    ReproductionArtifact,
    ReproductionExperiment,
    ReproductionResult,
)
from app.services import data_quality_center as dq
from app.services.data_quality_center import run_consistency_checks
from app.services.platform_time import utcnow
from app.services.reproduction_artifacts import ReproductionArtifactStore
from tests.phase11_helpers import episode


def _kinds(result) -> set[str]:
    return {finding.kind.value for finding in result.findings}


def _finding(result, kind: str):
    return next(f for f in result.findings if f.kind.value == kind)


#: The kinds this module owns. A finding outside this set comes from a different
#: check and is not what these tests are about.
W5_KINDS = (
    "MISSING_TIMESTAMP",
    "IMPOSSIBLE_TRANSITION",
    "MISSING_AUDIT_EVENT",
    "MISSING_PROVENANCE",
    "CORRUPTED_ARTIFACT",
)


async def _record_history(session, ctx) -> None:
    """Give the fixture's incident the history production would have written.

    ``phase11_helpers.episode`` inserts the incident row directly; the incident
    manager always appends an ``INCIDENT_CREATED`` timeline event when it does.
    A test about *false positives* has to build the row the platform actually
    produces, or it is testing the fixture.
    """
    session.add(
        IncidentTimelineEvent(
            incident_id=ctx.incident.id,
            project_id=ctx.project.id,
            environment_id=ctx.environment.id,
            event_type=TimelineEventType.INCIDENT_CREATED,
            occurred_at=ctx.incident.detected_at,
            title="Incident created from correlated anomalies",
            provenance="test",
        )
    )
    await session.flush()


class TestCleanProjectsAreClean:
    async def test_a_realistic_project_raises_nothing(self, db_session):
        """The regression guard: a check that raises reports nothing, and the
        per-check error handler makes that look like a clean run."""
        ctx = await episode(
            db_session,
            incident_status="RESOLVED",
            resolved_at=utcnow() + timedelta(seconds=30),
        )
        result = await run_consistency_checks(db_session, project_id=ctx.project.id)
        assert result.errors == [], result.errors
        assert result.checked == len(dq.CHECKS)

    async def test_a_healthy_episode_produces_no_false_findings(self, db_session):
        """A well-formed incident must not trip the new checks."""
        ctx = await episode(db_session, incident_status="OPEN")
        await _record_history(db_session, ctx)

        result = await run_consistency_checks(db_session, project_id=ctx.project.id)

        false_positives = _kinds(result).intersection(W5_KINDS)
        assert false_positives == set(), f"false positives: {false_positives}"


class TestMissingTimestamp:
    async def test_a_resolved_incident_without_a_resolution_time_is_reported(
        self, db_session
    ):
        ctx = await episode(db_session, incident_status="RESOLVED", resolved_at=None)

        result = await run_consistency_checks(db_session, project_id=ctx.project.id)

        assert "MISSING_TIMESTAMP" in _kinds(result)
        finding = _finding(result, "MISSING_TIMESTAMP")
        assert finding.subject_type == "incident"
        assert finding.subject_id == ctx.incident.id

    async def test_an_open_incident_is_not_reported(self, db_session):
        """Only a terminal state implies a moment that should have been recorded."""
        ctx = await episode(db_session, incident_status="OPEN")
        result = await run_consistency_checks(db_session, project_id=ctx.project.id)
        hours = (
            _finding(result, "MISSING_TIMESTAMP")
            if "MISSING_TIMESTAMP" in _kinds(result)
            else None
        )
        assert hours is None


class TestDuplicateIncidentsAreStructurallyImpossible:
    async def test_the_database_refuses_two_live_incidents_per_fingerprint(
        self, db_session
    ):
        """The invariant is enforced by the schema, not detected afterwards.

        This test exists because a duplicate-incident *check* was proposed and
        then deleted: the partial unique index makes the state unreachable, so a
        detector would have implied a gap that does not exist. The assertion is
        on the constraint itself, which is what keeps the claim true.
        """
        from sqlalchemy.exc import IntegrityError

        ctx = await episode(db_session, incident_status="OPEN")
        twin = Incident(
            project_id=ctx.project.id,
            environment_id=ctx.environment.id,
            primary_component_id=ctx.component.id,
            title=ctx.incident.title,
            description="a second live incident for the same failure",
            severity=ctx.incident.severity,
            status=ctx.incident.status,
            fingerprint=ctx.incident.fingerprint,
            detected_at=utcnow(),
        )
        db_session.add(twin)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_a_resolved_incident_may_be_repeated(self, db_session):
        """History is allowed to contain the same failure twice; *live* is not."""
        ctx = await episode(db_session, incident_status="OPEN")
        earlier = Incident(
            project_id=ctx.project.id,
            environment_id=ctx.environment.id,
            primary_component_id=ctx.component.id,
            title=ctx.incident.title,
            description="the earlier occurrence, resolved",
            severity=ctx.incident.severity,
            status="RESOLVED",
            fingerprint=ctx.incident.fingerprint,
            detected_at=utcnow() - timedelta(hours=2),
            resolved_at=utcnow() - timedelta(hours=1),
        )
        db_session.add(earlier)
        await db_session.flush()
        assert earlier.id is not None


class TestImpossibleTransitions:
    async def test_an_incident_resolved_before_detection_is_reported(self, db_session):
        ctx = await episode(
            db_session,
            incident_status="RESOLVED",
            resolved_at=utcnow() + timedelta(seconds=30),
        )
        ctx.incident.resolved_at = ctx.incident.detected_at - timedelta(minutes=5)
        await db_session.flush()

        result = await run_consistency_checks(db_session, project_id=ctx.project.id)

        assert "IMPOSSIBLE_TRANSITION" in _kinds(result)
        finding = _finding(result, "IMPOSSIBLE_TRANSITION")
        assert finding.severity.value == "CRITICAL"
        assert finding.subject_id == ctx.incident.id

    async def test_a_resolved_incident_in_the_right_order_is_not_reported(
        self, db_session
    ):
        #: ``resolved_at`` must be evaluated *after* the fixture's own ``utcnow()``
        #: (which it uses for ``detected_at``), or the test itself would create an
        #: incident resolved microseconds before it was detected.
        ctx = await episode(
            db_session,
            incident_status="RESOLVED",
            resolved_at=utcnow() + timedelta(seconds=30),
        )
        await _record_history(db_session, ctx)
        result = await run_consistency_checks(db_session, project_id=ctx.project.id)
        assert "IMPOSSIBLE_TRANSITION" not in _kinds(result)


class TestMissingAuditEvents:
    async def test_an_incident_with_no_timeline_is_reported(self, db_session):
        """The platform claims every incident has a history; this checks its own
        claim against its own data."""
        ctx = await episode(db_session, incident_status="OPEN")
        await db_session.execute(
            delete(IncidentTimelineEvent).where(
                IncidentTimelineEvent.incident_id == ctx.incident.id
            )
        )
        await db_session.flush()

        result = await run_consistency_checks(db_session, project_id=ctx.project.id)

        assert "MISSING_AUDIT_EVENT" in _kinds(result)
        finding = _finding(result, "MISSING_AUDIT_EVENT")
        assert finding.subject_id == ctx.incident.id


class TestMissingProvenance:
    async def test_an_experience_without_an_incident_is_reported(self, db_session):
        """A derived row that cites no source is a claim about the past that no
        stored evidence supports."""
        ctx = await episode(
            db_session,
            incident_status="RESOLVED",
            resolved_at=utcnow() + timedelta(seconds=30),
        )
        experience = ReliabilityExperience(
            project_id=ctx.project.id,
            environment_id=ctx.environment.id,
            incident_id=None,
            primary_component_id=ctx.component.id,
            start_time=utcnow() - timedelta(hours=1),
            end_time=utcnow(),
            failure_signature={"kind": "latency"},
            failure_fingerprint=f"w5-{uuid.uuid4().hex[:32]}",
            outcome="RECOVERED",
            provenance=DataProvenance.OBSERVABILITY,
            data_quality="OK",
            component_ids=[str(ctx.component.id)],
        )
        db_session.add(experience)
        await db_session.flush()

        result = await run_consistency_checks(db_session, project_id=ctx.project.id)

        assert "MISSING_PROVENANCE" in _kinds(result)
        finding = _finding(result, "MISSING_PROVENANCE")
        assert finding.subject_type == "reliability_experience"
        assert finding.subject_id == experience.id


class TestCorruptedArtifacts:
    async def test_an_artifact_whose_bytes_changed_is_reported(
        self, db_session, tmp_path, monkeypatch
    ):
        """The store is content-addressed precisely so this is answerable: a
        mismatch means the evidence a verification cited is gone."""
        ctx = await episode(
            db_session,
            incident_status="RESOLVED",
            resolved_at=utcnow() + timedelta(seconds=30),
        )
        experiment = ReproductionExperiment(
            project_id=ctx.project.id,
            environment_id=ctx.environment.id,
            incident_id=ctx.incident.id,
            status=ExperimentStatus.COMPLETED,
            result=ReproductionResult.SUCCESSFUL,
            confidence=ConfidenceLevel.HIGH,
        )
        db_session.add(experiment)
        await db_session.flush()

        store = ReproductionArtifactStore(root=tmp_path)
        record = store.store(
            experiment_id=experiment.id,
            artifact_type=ArtifactType.REPRODUCTION_MANIFEST,
            name="manifest.json",
            payload={"reproduced": True},
        )
        artifact = ReproductionArtifact(
            experiment_id=experiment.id,
            project_id=ctx.project.id,
            artifact_type=ArtifactType.REPRODUCTION_MANIFEST,
            name=record.name,
            content_type=record.content_type,
            storage_location=record.storage_location,
            size_bytes=record.size_bytes,
            content_hash=record.content_hash,
        )
        db_session.add(artifact)
        await db_session.flush()

        # The check builds its own store, rooted at the configured location; the
        # test points it at this experiment's temp root.
        monkeypatch.setattr(
            dq,
            "ReproductionArtifactStore",
            lambda: ReproductionArtifactStore(root=tmp_path),
        )

        intact = await run_consistency_checks(db_session, project_id=ctx.project.id)
        assert "CORRUPTED_ARTIFACT" not in _kinds(intact), "an intact artifact reported"

        # Now the bytes change underneath the recorded hash.
        (tmp_path / record.storage_location).write_bytes(b'{"reproduced": false}')

        corrupted = await run_consistency_checks(db_session, project_id=ctx.project.id)
        assert "CORRUPTED_ARTIFACT" in _kinds(corrupted)
        finding = _finding(corrupted, "CORRUPTED_ARTIFACT")
        assert finding.subject_id == artifact.id
        assert finding.evidence["recorded_hash"] == record.content_hash

    async def test_a_missing_artifact_file_is_reported(
        self, db_session, tmp_path, monkeypatch
    ):
        """A vanished artifact is not 'intact, nothing to see' — it is lost."""
        ctx = await episode(
            db_session,
            incident_status="RESOLVED",
            resolved_at=utcnow() + timedelta(seconds=30),
        )
        experiment = ReproductionExperiment(
            project_id=ctx.project.id,
            environment_id=ctx.environment.id,
            incident_id=ctx.incident.id,
            status=ExperimentStatus.COMPLETED,
            result=ReproductionResult.SUCCESSFUL,
            confidence=ConfidenceLevel.HIGH,
        )
        db_session.add(experiment)
        await db_session.flush()

        db_session.add(
            ReproductionArtifact(
                experiment_id=experiment.id,
                project_id=ctx.project.id,
                artifact_type=ArtifactType.REPRODUCTION_PLAN,
                name="plan.json",
                content_type="application/json",
                storage_location=f"{experiment.id}/plan.json",
                size_bytes=10,
                content_hash="0" * 64,
            )
        )
        await db_session.flush()
        monkeypatch.setattr(
            dq,
            "ReproductionArtifactStore",
            lambda: ReproductionArtifactStore(root=tmp_path),
        )

        result = await run_consistency_checks(db_session, project_id=ctx.project.id)

        assert "CORRUPTED_ARTIFACT" in _kinds(result)


@pytest.mark.asyncio
async def test_every_kind_has_a_description_and_a_suggestion():
    """A finding with no meaning and no next step is noise in an operator's queue."""
    from app.models.platform import DataQualityIssueKind

    assert set(DataQualityIssueKind) == set(dq.ISSUE_DESCRIPTIONS)
    assert set(DataQualityIssueKind) == set(dq.ISSUE_SUGGESTIONS)
