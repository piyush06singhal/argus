"""Phase 10 — the learning pipeline end to end (§7, §27–§29, §98).

These are integration tests: real incidents, real anomalies, a real remediation,
the real builder, the real miners, the real validator and the real lifecycle. If
any one of them is stubbed the test would be checking the stub.

The properties under test are the ones the phase is emphatic about: raw events
never become active knowledge, small samples stay candidates, a re-run changes
nothing, and a cutoff keeps the future out.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models.intelligence import (
    KnowledgeStatus,
    KnowledgeType,
    LearningEvent,
    LearningEventType,
    LearningRun,
    LearningRunStatus,
    RecommendationStatus,
    ReliabilityExperience,
    ReliabilityKnowledge,
    ReliabilityRecommendation,
)
from app.services.experience_builder import build_experience_draft
from app.services.learning_events import publish_learning_event
from app.services.learning_run import (
    execute_learning_run,
    pending_event_count,
    run_is_due,
)
from tests.phase10_helpers import (
    build_project,
    hours_before,
    record_series,
    utcnow,
)


async def _episodes(db_session, count: int = 5, spacing_hours: float = 24.0, **kwargs):
    project, environment, component = await build_project(db_session)
    episodes = await record_series(
        db_session,
        project,
        environment,
        component,
        count=count,
        first_started_at=hours_before(utcnow(), spacing_hours * (count + 1)),
        spacing_hours=spacing_hours,
        **kwargs,
    )
    return project, environment, component, episodes


@pytest.mark.asyncio
async def test_a_run_turns_history_into_candidate_knowledge(db_session):
    project, environment, component, episodes = await _episodes(db_session, count=4)

    summary = await execute_learning_run(
        db_session, project_id=project.id, trigger="test"
    )

    assert summary.status == LearningRunStatus.COMPLETED
    assert summary.patterns_discovered > 0
    #: Below the validation threshold (5 by default) the pattern is a candidate —
    #: never active, however consistent the history looks.
    knowledge = list(
        (
            await db_session.scalars(
                select(ReliabilityKnowledge).where(
                    ReliabilityKnowledge.project_id == project.id
                )
            )
        ).all()
    )
    assert knowledge, "the pipeline produced no knowledge at all"
    remediation_patterns = [
        row
        for row in knowledge
        if row.knowledge_type == KnowledgeType.REMEDIATION_PATTERN
    ]
    assert remediation_patterns
    row = remediation_patterns[0]
    assert row.sample_count == 4
    assert row.status == KnowledgeStatus.CANDIDATE
    assert row.confidence.value in ("LOW", "UNKNOWN")
    assert row.experience_ids
    assert row.sources
    assert row.version == 1
    assert row.limitations

    run = await db_session.scalar(
        select(LearningRun).where(LearningRun.id == uuid.UUID(summary.run_id))
    )
    assert run is not None
    assert run.status == LearningRunStatus.COMPLETED
    assert run.patterns_discovered == summary.patterns_discovered
    assert run.algorithm_versions["pattern_miners"]


@pytest.mark.asyncio
async def test_enough_stable_evidence_reaches_validated(db_session):
    """Five spread-out episodes validate the pattern; activation still needs a human."""
    project, _, _, _ = await _episodes(db_session, count=6, spacing_hours=24.0)

    summary = await execute_learning_run(
        db_session, project_id=project.id, trigger="test"
    )
    assert summary.patterns_validated > 0

    rows = list(
        (
            await db_session.scalars(
                select(ReliabilityKnowledge).where(
                    ReliabilityKnowledge.project_id == project.id,
                    ReliabilityKnowledge.knowledge_type
                    == KnowledgeType.REMEDIATION_PATTERN,
                )
            )
        ).all()
    )
    assert rows
    assert rows[0].status in (KnowledgeStatus.VALIDATED, KnowledgeStatus.ACTIVE)
    #: Autonomous activation is off by default, so nothing may be ACTIVE.
    assert all(row.status != KnowledgeStatus.ACTIVE for row in rows)
    assert all(row.status != KnowledgeStatus.ACTIVE for row in rows)


@pytest.mark.asyncio
async def test_repeated_runs_do_not_duplicate_knowledge(db_session):
    """§64, §85: a second pass over the same history adds nothing new.

    The first run computes the component profiles it later mines from, so a
    chronic-reliability pattern can legitimately appear on the *second* run. The
    property that must hold is that from then on nothing is duplicated: the same
    pattern is updated in place, one row per fingerprint.
    """
    project, _, _, _ = await _episodes(db_session, count=6)
    await execute_learning_run(db_session, project_id=project.id, trigger="test")
    await execute_learning_run(db_session, project_id=project.id, trigger="test")
    settled = list(
        (
            await db_session.scalars(
                select(ReliabilityKnowledge).where(
                    ReliabilityKnowledge.project_id == project.id
                )
            )
        ).all()
    )
    settled_count = len(settled)
    settled_samples = sum(row.sample_count for row in settled)
    assert settled_count > 0

    third = await execute_learning_run(
        db_session, project_id=project.id, trigger="test"
    )
    rows = list(
        (
            await db_session.scalars(
                select(ReliabilityKnowledge).where(
                    ReliabilityKnowledge.project_id == project.id
                )
            )
        ).all()
    )
    assert len(rows) == settled_count
    assert sum(row.sample_count for row in rows) == settled_samples
    assert third.knowledge_created == 0

    #: One live row per fingerprint: identity is what stops the duplication.
    live = [row for row in rows if row.status != KnowledgeStatus.SUPERSEDED]
    fingerprints = [row.fingerprint for row in live]
    assert len(fingerprints) == len(set(fingerprints))


@pytest.mark.asyncio
async def test_small_samples_stay_candidates(db_session):
    """§86: two observations must not become established knowledge."""
    project, _, _, _ = await _episodes(db_session, count=2)
    summary = await execute_learning_run(
        db_session, project_id=project.id, trigger="test"
    )

    rows = list(
        (
            await db_session.scalars(
                select(ReliabilityKnowledge).where(
                    ReliabilityKnowledge.project_id == project.id
                )
            )
        ).all()
    )
    assert rows
    assert all(row.status == KnowledgeStatus.CANDIDATE for row in rows)
    assert summary.patterns_validated == 0
    assert summary.knowledge_activated == 0


@pytest.mark.asyncio
async def test_events_are_consumed_exactly_once(db_session):
    project, environment, component, episodes = await _episodes(db_session, count=2)
    incident = episodes[0]["incident"]
    await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.INCIDENT_RESOLVED,
        subject_id=incident.id,
        occurred_at=incident.resolved_at,
    )
    await db_session.flush()
    assert await pending_event_count(db_session, project_id=project.id) == 1

    first = await execute_learning_run(
        db_session, project_id=project.id, trigger="test"
    )
    assert first.events_processed == 1
    assert await pending_event_count(db_session, project_id=project.id) == 0

    event = await db_session.scalar(select(LearningEvent).limit(1))
    assert event is not None
    assert event.processed_at is not None
    assert event.processed_by_run_id is not None

    second = await execute_learning_run(
        db_session, project_id=project.id, trigger="test"
    )
    assert second.events_processed == 0


@pytest.mark.asyncio
async def test_publishing_the_same_event_twice_records_one_row(db_session):
    project, environment, component, episodes = await _episodes(db_session, count=1)
    incident = episodes[0]["incident"]
    first = await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.INCIDENT_RESOLVED,
        subject_id=incident.id,
    )
    second = await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.INCIDENT_RESOLVED,
        subject_id=incident.id,
    )
    await db_session.flush()
    assert first is not None
    assert second is None
    count = await db_session.scalar(
        select(func.count(LearningEvent.id)).where(
            LearningEvent.subject_id == incident.id
        )
    )
    assert count == 1


@pytest.mark.asyncio
async def test_a_cutoff_keeps_later_episodes_out_of_the_sample(db_session):
    """§31, §84: a run as of a moment cannot learn from what happened after it."""
    project, environment, component = await build_project(db_session)
    start = hours_before(utcnow(), 24 * 12)
    episodes = await record_series(
        db_session,
        project,
        environment,
        component,
        count=6,
        first_started_at=start,
        spacing_hours=24.0,
    )
    cutoff = episodes[2]["incident"].resolved_at + timedelta(minutes=1)

    summary = await execute_learning_run(
        db_session, project_id=project.id, trigger="test", cutoff=cutoff
    )
    rows = list(
        (
            await db_session.scalars(
                select(ReliabilityKnowledge).where(
                    ReliabilityKnowledge.project_id == project.id,
                    ReliabilityKnowledge.knowledge_type
                    == KnowledgeType.REMEDIATION_PATTERN,
                )
            )
        ).all()
    )
    assert rows
    #: Only the episodes that had already happened are in the sample.
    assert max(row.sample_count for row in rows) <= 3
    assert summary.status == LearningRunStatus.COMPLETED

    later = await db_session.scalars(
        select(ReliabilityExperience).where(
            ReliabilityExperience.project_id == project.id,
            ReliabilityExperience.end_time > cutoff,
        )
    )
    for experience in later.all():
        for row in rows:
            assert str(experience.id) not in row.experience_ids


@pytest.mark.asyncio
async def test_experiences_are_only_built_from_completed_incidents(db_session):
    """An event naming an open incident produces a reason, not a fake experience."""
    project, environment, component = await build_project(db_session)
    started = hours_before(utcnow(), 2)

    from tests.phase10_helpers import emit_incident

    incident = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=started,
        resolved_at=None,
        status="OPEN",
        fingerprint="still_open",
    )
    await db_session.flush()
    await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.INCIDENT_RESOLVED,
        subject_id=incident.id,
        occurred_at=started,
    )
    await db_session.flush()

    summary = await execute_learning_run(
        db_session, project_id=project.id, trigger="test"
    )
    assert summary.experiences_created == 0
    assert str(incident.id) in summary.unprocessable
    assert summary.unprocessable[str(incident.id)].startswith("incident_not_completed")


@pytest.mark.asyncio
async def test_a_run_generates_evidence_backed_recommendations(db_session):
    """§38, §89: recommendations cite real experiences and carry their limits."""
    from tests.phase10_helpers import emit_anomaly, emit_incident

    project, environment, component, episodes = await _episodes(db_session, count=4)
    open_started = utcnow() - timedelta(minutes=30)
    #: The run discovers open incidents itself; the fixture only has to store one.
    await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=open_started,
        resolved_at=None,
        status="OPEN",
        fingerprint="checkout_error_spike",
    )
    await emit_anomaly(
        db_session,
        project,
        environment,
        component,
        detected_at=open_started + timedelta(minutes=1),
        anomaly_type="ERROR_RATE_SPIKE",
        metric_name="http.checkout.error_rate",
    )
    await db_session.flush()

    await execute_learning_run(db_session, project_id=project.id, trigger="test")

    recommendations = list(
        (
            await db_session.scalars(
                select(ReliabilityRecommendation).where(
                    ReliabilityRecommendation.project_id == project.id
                )
            )
        ).all()
    )
    assert recommendations, "no recommendation was produced for an open incident"
    for row in recommendations:
        assert row.rationale
        assert row.current_evidence
        assert row.limitations
        assert row.ranking
        if row.recommendation_type.value.startswith("CONSIDER"):
            #: An action-shaped recommendation must cite the history it stands on.
            assert row.experience_ids
        assert row.status in (RecommendationStatus.OPEN, RecommendationStatus.EXPIRED)


@pytest.mark.asyncio
async def test_recommendations_expire_after_their_ttl(db_session):
    from tests.phase10_helpers import emit_anomaly, emit_incident

    project, environment, component = await build_project(db_session)
    started = hours_before(utcnow(), 6)
    await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=started,
        resolved_at=None,
        status="OPEN",
        fingerprint="expiring_case",
    )
    await emit_anomaly(
        db_session,
        project,
        environment,
        component,
        detected_at=started + timedelta(minutes=1),
        anomaly_type="LATENCY_SPIKE",
        metric_name="http.checkout.latency.p95",
    )
    await db_session.flush()

    await execute_learning_run(db_session, project_id=project.id, trigger="test")
    rows = list(
        (
            await db_session.scalars(
                select(ReliabilityRecommendation).where(
                    ReliabilityRecommendation.project_id == project.id
                )
            )
        ).all()
    )
    assert rows

    future = utcnow() + timedelta(days=3)
    summary = await execute_learning_run(
        db_session, project_id=project.id, trigger="test", now=future, cutoff=future
    )
    refreshed = list(
        (
            await db_session.scalars(
                select(ReliabilityRecommendation).where(
                    ReliabilityRecommendation.project_id == project.id
                )
            )
        ).all()
    )
    assert any(row.status == RecommendationStatus.EXPIRED for row in refreshed)
    assert summary.recommendations_expired >= 1


@pytest.mark.asyncio
async def test_run_is_due_respects_the_interval(db_session):
    project, _, _, _ = await _episodes(db_session, count=1)
    assert await run_is_due(db_session, project_id=project.id, interval_seconds=3600)
    await execute_learning_run(db_session, project_id=project.id, trigger="sweep")
    assert not await run_is_due(
        db_session, project_id=project.id, interval_seconds=3600
    )
    assert await run_is_due(
        db_session,
        project_id=project.id,
        interval_seconds=3600,
        now=utcnow() + timedelta(hours=2),
    )


@pytest.mark.asyncio
async def test_profiles_and_chronic_signals_are_computed(db_session):
    """§21, §96: a component with repeated incidents gets a chronic signal."""
    project, environment, component, _ = await _episodes(
        db_session, count=7, spacing_hours=12.0
    )
    await execute_learning_run(db_session, project_id=project.id, trigger="test")

    from app.models.intelligence import ComponentReliabilityProfile

    profiles = list(
        (
            await db_session.scalars(
                select(ComponentReliabilityProfile).where(
                    ComponentReliabilityProfile.project_id == project.id,
                    ComponentReliabilityProfile.component_id == component.id,
                )
            )
        ).all()
    )
    assert profiles
    recent = [row for row in profiles if row.window_days == 30]
    assert recent
    assert recent[0].incident_count >= 5
    assert recent[0].chronic_signal is True
    assert recent[0].chronic_reasons
    #: The signal recommends investigation and changes nothing about the component.
    assert recent[0].breakdown["anomaly_types"]


@pytest.mark.asyncio
async def test_an_experience_is_rebuilt_when_its_incident_is_revisited(db_session):
    """A later run that sees a changed episode updates it rather than duplicating it."""
    project, environment, component, episodes = await _episodes(db_session, count=1)
    incident = episodes[0]["incident"]
    before = await db_session.scalar(
        select(func.count(ReliabilityExperience.id)).where(
            ReliabilityExperience.incident_id == incident.id
        )
    )
    assert before == 1

    incident.resolved_at = incident.resolved_at + timedelta(minutes=20)
    await db_session.flush()
    draft, reason = await build_experience_draft(
        db_session, incident_id=incident.id, as_of=utcnow()
    )
    assert draft is not None, reason
    from app.services.experience_builder import persist_experience

    _row, status = await persist_experience(db_session, draft)
    assert status == "updated"
    after = await db_session.scalar(
        select(func.count(ReliabilityExperience.id)).where(
            ReliabilityExperience.incident_id == incident.id
        )
    )
    assert after == 1
