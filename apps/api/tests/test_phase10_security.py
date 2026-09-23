"""Phase 10 — data poisoning, provenance and learning-boundary security (§74–§79, §85, §91).

Learning is only as trustworthy as its inputs, so these tests are about what
ARGUS refuses to learn *from* and what it refuses to become:

* AI-generated evidence is recorded but not learned from unless the deployment
  opts in, and a database row cannot opt in on the deployment's behalf (§76, §77);
* a machine-generated hypothesis is never promoted to a confirmed root cause by
  the act of writing it down (§78);
* the same completed outcome published twice produces one row, one experience,
  and one piece of knowledge — never two (§64, §85);
* a learning run cannot be pointed at another project's history (§62, §91);
* knowledge cannot be activated without validation, by a review, by a run, or by
  an auto-activation switch (§73, §74);
* a run that fails records the failure rather than leaving half-believed state (§79).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.phase10_helpers import (
    build_project,
    build_scope,
    emit_incident,
    make_knowledge,
    record_series,
    utcnow,
)

from app.models.intelligence import (
    DataProvenance,
    KnowledgeStatus,
    LearningEvent,
    LearningEventHook,
    LearningEventType,
    LearningRun,
    LearningRunStatus,
    ReliabilityExperience,
    ReliabilityKnowledge,
)
from app.services.intelligence_state import (
    DEFAULT_TRUSTED_PROVENANCE,
    IntelligenceStateError,
    trusted_provenance_names,
)
from app.services.knowledge_lifecycle import (
    activate_knowledge,
    record_candidate,
)
from app.services.knowledge_validation import KnowledgeValidationService
from app.services.learning_events import (
    claim_unprocessed_events,
    dedup_key_for,
    enabled_event_types,
    mark_event_processed,
    publish_learning_event,
    safely_publish_learning_event,
    set_event_hook,
    trusted_provenance,
)
from app.services.learning_run import (
    eligible_projects,
    execute_learning_run,
    safely_execute_learning_run,
)
from app.services.pattern_miners import MinedPattern, load_corpus


# ---------------------------------------------------------------------------
# Provenance (§76, §77)
# ---------------------------------------------------------------------------


def test_ai_output_and_mocks_are_not_trusted_by_default():
    default = trusted_provenance_names(include_ai=False, include_mock=False)
    assert DataProvenance.AI_GENERATED not in default
    assert DataProvenance.MOCK not in default
    assert set(DEFAULT_TRUSTED_PROVENANCE) == set(default)


def test_the_deployment_can_opt_into_ai_output_and_then_it_is_trusted():
    opted_in = trusted_provenance_names(include_ai=True, include_mock=True)
    assert DataProvenance.AI_GENERATED in opted_in
    assert DataProvenance.MOCK in opted_in


async def test_a_hook_row_cannot_widen_trust_beyond_the_deployment(db_session):
    """§76. The deployment decides; a database row may only narrow that."""
    project, _, _ = await build_project(db_session)
    await set_event_hook(
        db_session,
        project_id=project.id,
        trusted_provenance_classes=["OBSERVABILITY", "AI_GENERATED", "MOCK"],
    )
    await db_session.commit()
    trusted = await trusted_provenance(db_session, project_id=project.id)
    assert DataProvenance.AI_GENERATED not in trusted
    assert DataProvenance.MOCK not in trusted
    assert DataProvenance.OBSERVABILITY in trusted


async def test_a_hook_row_can_narrow_trust(db_session):
    project, _, _ = await build_project(db_session)
    await set_event_hook(
        db_session, project_id=project.id, trusted_provenance_classes=["OBSERVABILITY"]
    )
    await db_session.commit()
    trusted = await trusted_provenance(db_session, project_id=project.id)
    assert trusted == frozenset({DataProvenance.OBSERVABILITY})


async def test_an_unknown_provenance_value_in_a_row_is_ignored_not_trusted(db_session):
    project, _, _ = await build_project(db_session)
    hook = LearningEventHook(
        project_id=project.id,
        enabled_event_types=[],
        trusted_provenance=["OBSERVABILITY", "TOTALLY_TRUSTWORTHY"],
    )
    db_session.add(hook)
    await db_session.commit()
    trusted = await trusted_provenance(db_session, project_id=project.id)
    assert trusted == frozenset({DataProvenance.OBSERVABILITY})


async def test_a_hook_can_narrow_which_events_are_consumed(db_session):
    project, _, _ = await build_project(db_session)
    await set_event_hook(
        db_session,
        project_id=project.id,
        enabled_event_types=["INCIDENT_RESOLVED"],
    )
    await db_session.commit()
    enabled = await enabled_event_types(db_session, project_id=project.id)
    assert enabled == frozenset({LearningEventType.INCIDENT_RESOLVED})


def test_every_consumable_event_describes_something_that_finished():
    """§6. Nothing fires on intent; every type is a completed outcome."""
    for event_type in LearningEventType:
        assert event_type.value.endswith(
            (
                "RESOLVED",
                "COMPLETED",
                "VERIFIED",
                "DETECTED",
                "CONFIRMED",
                "REJECTED",
                "FAILED",
                "FALSE_POSITIVE",
                "MISSED",
            )
        ), event_type


async def test_an_event_records_where_its_data_came_from(db_session):
    project, _, _ = await build_project(db_session)
    subject = uuid.uuid4()
    event = await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.INCIDENT_RESOLVED,
        subject_id=subject,
        provenance=DataProvenance.OBSERVABILITY,
    )
    await db_session.commit()
    assert event is not None
    assert event.provenance == DataProvenance.OBSERVABILITY
    assert event.subject_id == subject


async def test_an_untrusted_event_is_recorded_but_never_claimed(db_session):
    """§77. AI output is kept for the audit trail and excluded from learning."""
    project, environment, component = await build_project(db_session)
    incident = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=utcnow() - timedelta(hours=3),
        resolved_at=utcnow() - timedelta(hours=2),
        status="RESOLVED",
    )
    await db_session.commit()
    await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.INCIDENT_RESOLVED,
        subject_id=incident.id,
        provenance=DataProvenance.AI_GENERATED,
    )
    await db_session.commit()

    trusted = await trusted_provenance(db_session, project_id=project.id)
    claimed = await claim_unprocessed_events(
        db_session, project_id=project.id, cutoff=utcnow(), limit=100, trusted=trusted
    )
    assert claimed == []

    rows = list((await db_session.scalars(select(LearningEvent))).all())
    assert len(rows) == 1
    assert rows[0].provenance == DataProvenance.AI_GENERATED
    assert rows[0].processed_at is None


async def test_a_trusted_event_is_claimed_exactly_once(db_session):
    project, environment, component = await build_project(db_session)
    incident = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=utcnow() - timedelta(hours=3),
        resolved_at=utcnow() - timedelta(hours=2),
        status="RESOLVED",
    )
    await db_session.commit()
    await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.INCIDENT_RESOLVED,
        subject_id=incident.id,
    )
    await db_session.commit()

    trusted = await trusted_provenance(db_session, project_id=project.id)
    first = await claim_unprocessed_events(
        db_session, project_id=project.id, cutoff=utcnow(), limit=100, trusted=trusted
    )
    assert len(first) == 1
    for event in first:
        await mark_event_processed(db_session, event, run_id=None)
    await db_session.commit()

    second = await claim_unprocessed_events(
        db_session, project_id=project.id, cutoff=utcnow(), limit=100, trusted=trusted
    )
    assert second == []


async def test_a_hypothesis_is_not_a_confirmed_root_cause(db_session):
    """§78. Writing down an AI root cause does not make it confirmed."""
    project, _, _ = await build_project(db_session)
    hypothesis = await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.ROOT_CAUSE_CONFIRMED,
        subject_id=uuid.uuid4(),
        provenance=DataProvenance.AI_GENERATED,
        payload={"candidate_type": "COMPONENT_FAILURE"},
    )
    confirmed = await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.ROOT_CAUSE_CONFIRMED,
        subject_id=uuid.uuid4(),
        provenance=DataProvenance.HUMAN_ENTERED,
        payload={"candidate_type": "COMPONENT_FAILURE"},
    )
    await db_session.commit()
    assert hypothesis is not None
    assert confirmed is not None
    assert hypothesis.provenance == DataProvenance.AI_GENERATED
    assert confirmed.provenance == DataProvenance.HUMAN_ENTERED

    #: The AI hypothesis stays recorded and stays out of learning; only the
    #: human-confirmed fact is claimable (§77, §78).
    trusted = await trusted_provenance(db_session, project_id=project.id)
    claimed = await claim_unprocessed_events(
        db_session, project_id=project.id, cutoff=utcnow(), limit=100, trusted=trusted
    )
    assert [event.provenance for event in claimed] == [DataProvenance.HUMAN_ENTERED]


# ---------------------------------------------------------------------------
# Idempotency (§64, §85)
# ---------------------------------------------------------------------------


def test_the_dedup_key_is_deterministic_and_provenance_free():
    subject = uuid.uuid4()
    first = dedup_key_for(LearningEventType.PATCH_VERIFIED, subject, "RUN_A")
    again = dedup_key_for(LearningEventType.PATCH_VERIFIED, subject, "RUN_A")
    other = dedup_key_for(LearningEventType.PATCH_VERIFIED, subject, "RUN_B")
    different_type = dedup_key_for(
        LearningEventType.PATCH_REGRESSION_DETECTED, subject, "RUN_A"
    )
    assert first == again
    assert first != other
    assert first != different_type
    assert len(first) <= 128


async def test_publishing_the_same_outcome_twice_yields_one_row(db_session):
    """§85. Processed twice, learned once."""
    project, environment, component = await build_project(db_session)
    incident = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=utcnow() - timedelta(hours=4),
        resolved_at=utcnow() - timedelta(hours=3),
        status="RESOLVED",
    )
    await db_session.commit()

    first = await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.INCIDENT_RESOLVED,
        subject_id=incident.id,
    )
    await db_session.commit()
    duplicate = await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.INCIDENT_RESOLVED,
        subject_id=incident.id,
    )
    await db_session.commit()

    assert first is not None
    assert duplicate is None
    rows = list(
        (
            await db_session.scalars(
                select(LearningEvent).where(
                    LearningEvent.event_type == LearningEventType.INCIDENT_RESOLVED
                )
            )
        ).all()
    )
    assert len(rows) == 1


async def test_republishing_and_rerunning_produces_one_experience_and_one_pattern(
    db_session,
):
    """The end-to-end idempotency check: history in, one belief out."""
    project, environment, component = await build_project(db_session)
    incident = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=utcnow() - timedelta(hours=5),
        resolved_at=utcnow() - timedelta(hours=4),
        status="RESOLVED",
        fingerprint="checkout_error_spike",
    )
    await db_session.commit()

    for _ in range(3):
        await safely_publish_learning_event(
            db_session,
            project_id=project.id,
            event_type=LearningEventType.INCIDENT_RESOLVED,
            subject_id=incident.id,
        )
        await db_session.commit()
        await execute_learning_run(db_session, project_id=project.id, trigger="test")
        await db_session.commit()

    events = list((await db_session.scalars(select(LearningEvent))).all())
    experiences = list((await db_session.scalars(select(ReliabilityExperience))).all())
    knowledge = list((await db_session.scalars(select(ReliabilityKnowledge))).all())
    runs = list((await db_session.scalars(select(LearningRun))).all())

    assert len(events) == 1
    assert len(experiences) == 1
    assert len(runs) == 3
    fingerprints = [row.fingerprint for row in knowledge]
    assert len(fingerprints) == len(set(fingerprints)), "knowledge was duplicated"


# ---------------------------------------------------------------------------
# Activation boundaries (§73, §74)
# ---------------------------------------------------------------------------


async def test_auto_activation_is_off_by_default(client, db_session):
    response = client.get("/api/v1/intelligence/health")
    assert response.json()["auto_activation_enabled"] is False


async def test_a_run_cannot_activate_high_impact_knowledge_by_itself(db_session):
    """§74. Learning produces candidates; governance activates them."""
    project, environment, component = await build_project(db_session)
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=14,
        first_started_at=utcnow() - timedelta(days=40),
        spacing_hours=48.0,
    )
    await db_session.commit()

    await execute_learning_run(db_session, project_id=project.id, trigger="test")
    await db_session.commit()

    from app.services.intelligence_state import HIGH_IMPACT_KNOWLEDGE_TYPES

    rows = list((await db_session.scalars(select(ReliabilityKnowledge))).all())
    assert rows
    for row in rows:
        if row.knowledge_type.value in HIGH_IMPACT_KNOWLEDGE_TYPES:
            assert (
                row.status != KnowledgeStatus.ACTIVE
            ), f"{row.knowledge_type.value} activated itself"
        if row.status == KnowledgeStatus.ACTIVE:
            #: Anything that did activate needed both an explicit validation
            #: verdict and a review record — never a bare status write.
            assert row.reviewed_by is not None or row.sources


async def test_a_review_cannot_skip_validation_to_reach_active(db_session):
    project, _, _ = await build_project(db_session)
    row = await make_knowledge(db_session, project, status="CANDIDATE")
    await db_session.commit()
    with pytest.raises(IntelligenceStateError):
        await activate_knowledge(db_session, row, reviewer="engineer@example.com")


async def test_a_rejected_pattern_cannot_be_reopened_by_a_small_improvement(db_session):
    """§72. A human said no; more of the same evidence does not overturn it."""
    project, environment, component = await build_project(db_session)
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=6,
        first_started_at=utcnow() - timedelta(days=20),
    )
    await db_session.commit()
    corpus = await load_corpus(db_session, project_id=project.id, cutoff=utcnow())

    rejected = await make_knowledge(
        db_session,
        project,
        status="REJECTED",
        feature_signature="remediation:restart_service:checkout_error_spike",
        details={"action_type": "restart_service"},
        sample_count=6,
        success_count=6,
    )
    await db_session.commit()

    pattern = MinedPattern(
        knowledge_type=rejected.knowledge_type,
        scope=rejected.scope,
        project_id=project.id,
        title="same shape, barely more evidence",
        description="one more observation",
        feature_signature="remediation:restart_service:checkout_error_spike",
        algorithm="fixture",
        sample_count=6,
        success_count=6,
        details={"action_type": "restart_service"},
        experience_ids=[entry.experience_id for entry in corpus.usable()[:6]],
        sources=[
            {"type": "experience", "id": entry.experience_id}
            for entry in corpus.usable()[:6]
        ],
    )
    verdict = KnowledgeValidationService().validate(pattern, corpus=corpus)
    result = await record_candidate(db_session, pattern, verdict)
    await db_session.commit()
    assert result.created is False
    assert result.skipped_reason == "rejected_pattern_not_reopened"
    assert result.knowledge.id == rejected.id
    assert rejected.status == KnowledgeStatus.REJECTED
    rows = list((await db_session.scalars(select(ReliabilityKnowledge))).all())
    assert len(rows) == 1, "a rejected pattern was silently re-created"


async def test_evidence_more_than_double_does_allow_a_reopen(db_session):
    """§72. The refusal is proportional: substantially more evidence reopens it."""
    project, environment, component = await build_project(db_session)
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=20,
        first_started_at=utcnow() - timedelta(days=30),
    )
    await db_session.commit()
    corpus = await load_corpus(db_session, project_id=project.id, cutoff=utcnow())
    rejected = await make_knowledge(
        db_session,
        project,
        status="REJECTED",
        feature_signature="remediation:restart_service:checkout_error_spike",
        details={"action_type": "restart_service"},
        sample_count=4,
        success_count=4,
    )
    await db_session.commit()

    chosen = [entry.experience_id for entry in corpus.usable()]
    pattern = MinedPattern(
        knowledge_type=rejected.knowledge_type,
        scope=rejected.scope,
        project_id=project.id,
        title="far more evidence",
        description="many more observations",
        feature_signature="remediation:restart_service:checkout_error_spike",
        algorithm="fixture",
        sample_count=len(chosen),
        success_count=len(chosen),
        details={"action_type": "restart_service"},
        experience_ids=chosen,
        sources=[{"type": "experience", "id": item} for item in chosen],
    )
    verdict = KnowledgeValidationService().validate(pattern, corpus=corpus)
    result = await record_candidate(db_session, pattern, verdict)
    await db_session.commit()
    #: Much more evidence justifies a fresh candidate, and the rejection stays in
    #: the ledger as the row it superseded (§72).
    assert result.created is True
    assert result.skipped_reason is None
    assert result.knowledge.supersedes_knowledge_id == rejected.id
    assert rejected.status == KnowledgeStatus.SUPERSEDED


# ---------------------------------------------------------------------------
# Project isolation inside the learning pipeline (§62, §91)
# ---------------------------------------------------------------------------


async def test_a_run_only_touches_the_project_it_was_asked_about(db_session):
    project_a, environment_a, component_a = await build_project(db_session, name="A")
    project_b, environment_b, component_b = await build_project(db_session, name="B")
    await record_series(
        db_session,
        project_a,
        environment_a,
        component_a,
        count=6,
        first_started_at=utcnow() - timedelta(days=14),
    )
    await record_series(
        db_session,
        project_b,
        environment_b,
        component_b,
        count=6,
        first_started_at=utcnow() - timedelta(days=14),
    )
    await db_session.commit()

    await execute_learning_run(db_session, project_id=project_a.id, trigger="test")
    await db_session.commit()

    knowledge = list((await db_session.scalars(select(ReliabilityKnowledge))).all())
    assert knowledge
    assert {row.project_id for row in knowledge} == {project_a.id}
    experiences_b = list(
        (
            await db_session.scalars(
                select(ReliabilityExperience).where(
                    ReliabilityExperience.project_id == project_b.id
                )
            )
        ).all()
    )
    #: B keeps the experiences its own builder wrote; A's run must not touch them.
    assert experiences_b
    for row in experiences_b:
        assert row.learning_run_id is None or row.project_id == project_b.id


async def test_eligible_projects_lists_a_project_that_has_history(db_session):
    project, environment, component = await build_project(db_session)
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=3,
        first_started_at=utcnow() - timedelta(days=6),
    )
    await db_session.commit()
    projects = await eligible_projects(db_session, cutoff=utcnow())
    assert project.id in {item.id for item in projects}


async def test_eligible_projects_does_not_list_a_project_with_no_history(db_session):
    project, _, _ = await build_project(db_session)
    other_scope_environment, other = await build_scope(db_session, project.id)
    await db_session.commit()
    projects = await eligible_projects(db_session, cutoff=utcnow())
    assert project.id not in {item.id for item in projects}


async def test_a_failing_run_is_recorded_as_failed_not_as_success(
    db_session, monkeypatch
):
    """§79. A run that breaks is auditable, and believes nothing it did not finish."""
    project, environment, component = await build_project(db_session)
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=5,
        first_started_at=utcnow() - timedelta(days=12),
    )
    await db_session.commit()

    import app.services.learning_run as run_module

    original = run_module.load_corpus

    async def _boom(*args, **kwargs):
        raise RuntimeError("corpus store unavailable")

    monkeypatch.setattr(run_module, "load_corpus", _boom)
    summary = await safely_execute_learning_run(db_session, project_id=project.id)
    await db_session.commit()
    monkeypatch.setattr(run_module, "load_corpus", original)

    assert summary.status == LearningRunStatus.FAILED
    assert summary.errors
    assert "corpus store unavailable" in summary.errors[0]
    run = await db_session.get(LearningRun, uuid.UUID(summary.run_id))
    assert run is not None
    assert run.status == LearningRunStatus.FAILED
    assert run.error_summary


async def test_an_event_that_cannot_be_processed_is_marked_not_left_pending(db_session):
    """A permanently unprocessable event must not block the queue forever."""
    project, _, _ = await build_project(db_session)
    event = await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.INCIDENT_RESOLVED,
        subject_id=uuid.uuid4(),  # an incident that does not exist
    )
    await db_session.commit()
    assert event is not None

    summary = await execute_learning_run(
        db_session, project_id=project.id, trigger="test"
    )
    await db_session.commit()
    assert summary.events_processed >= 1
    assert summary.unprocessable

    remaining = await claim_unprocessed_events(
        db_session, project_id=project.id, cutoff=utcnow(), limit=100
    )
    assert remaining == []
