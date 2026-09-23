"""Phase 10 — the bridge from history to learning (§6, §8, §63).

A learning event names the row it is *about* — an incident resolution names the
incident, a remediation names the action, a patch verification names the patch —
while the pipeline assembles *incidents* into experiences. This suite pins the
mapping between the two, because the failure mode of getting it wrong is silent:
the event is recorded, marked processed, and the outcome it carried never reaches
the corpus.

Three things are checked, in increasing distance from the row:

* the hooks publish the subject and the incident link they promise (§6);
* an event about a linked row refreshes that row's episode, including when the
  row completed *after* the incident (§8 — an episode is what happened, not what
  happened before someone closed the ticket);
* an event cannot reach across projects through its own payload (§62, §91).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.intelligence import LearningEvent, LearningEventType
from app.services.learning_events import publish_learning_event
from app.services.learning_hooks import (
    record_patch_verification,
    record_remediation_completed,
    record_reproduction_result,
)
from app.services.learning_run import execute_learning_run
from tests.phase10_helpers import (
    build_project,
    build_scope,
    emit_incident,
    emit_remediation,
    hours_before,
    record_episode,
    utcnow,
)


async def _project_with_episode(db_session, **kwargs):
    """A project with one completed episode, and nothing learned from it yet."""
    project, environment, component = await build_project(db_session)
    resolved_at = hours_before(utcnow(), 2)
    episode = await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=hours_before(utcnow(), 3),
        resolved_at=resolved_at,
        **kwargs,
    )
    return project, environment, component, episode


@pytest.mark.asyncio
async def test_a_remediation_event_refreshes_its_incidents_episode(db_session):
    """The subject is the action; the episode is the incident it acted on.

    This is the regression test for a real failure: the action's id was read as
    an incident id, the event was marked processed with ``incident_not_found``,
    and a verified remediation was recorded but never learned from.
    """
    project, environment, component, episode = await _project_with_episode(
        db_session, remediate=False
    )
    incident = episode["incident"]
    assert episode["experience"].remediation_action_id is None

    #: The remediation is verified *after* the incident resolved — the ordinary
    #: order of events, and the one where a refresh is the only way the outcome
    #: can reach the corpus.
    action = await emit_remediation(
        db_session,
        project,
        environment,
        component,
        incident=incident,
        completed_at=incident.resolved_at,
    )
    await record_remediation_completed(db_session, action=action, actor="on-call")
    await db_session.flush()

    event = await db_session.scalar(
        select(LearningEvent).where(LearningEvent.subject_id == action.id)
    )
    assert event is not None
    assert event.event_type is LearningEventType.REMEDIATION_COMPLETED

    summary = await execute_learning_run(
        db_session, project_id=project.id, trigger="test"
    )
    assert summary.unprocessable == {}
    assert summary.events_processed == 1

    refreshed = await db_session.get(
        type(episode["experience"]), episode["experience"].id
    )
    assert refreshed is not None
    assert refreshed.remediation_action_id == action.id

    reloaded = await db_session.get(LearningEvent, event.id)
    assert reloaded is not None
    assert reloaded.processed_at is not None
    assert reloaded.unprocessable_reason != "incident_not_found"


@pytest.mark.asyncio
async def test_a_rollback_event_also_resolves_through_the_action(db_session):
    project, _, _, episode = await _project_with_episode(db_session)
    action = episode["action"]
    await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.ROLLBACK_COMPLETED,
        subject_id=action.id,
        occurred_at=episode["incident"].resolved_at,
        payload={"incident_id": None, "rollback_status": "SUCCEEDED"},
    )
    await db_session.flush()

    summary = await execute_learning_run(
        db_session, project_id=project.id, trigger="test"
    )
    #: The payload deliberately carries no incident: the walk through the action
    #: row is what has to find it.
    assert summary.unprocessable == {}


@pytest.mark.asyncio
async def test_a_reproduction_event_uses_the_incident_recorded_on_it(db_session):
    project, _, _, episode = await _project_with_episode(db_session)
    incident = episode["incident"]
    published = await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.REPRODUCTION_CONFIRMED,
        subject_id=uuid.uuid4(),
        payload={"incident_id": str(incident.id), "result": "SUCCESSFUL"},
    )
    await db_session.flush()
    assert published is not None

    summary = await execute_learning_run(
        db_session, project_id=project.id, trigger="test"
    )
    #: The subject is an experiment this fixture never wrote, so the payload is
    #: the only route to the incident — and it is the route that is taken.
    assert summary.unprocessable == {}


@pytest.mark.asyncio
async def test_an_event_cannot_reach_another_projects_episode(db_session):
    """A payload is caller data: naming a foreign incident must not teach it."""
    project, _, _, _ = await _project_with_episode(db_session)
    other_project, _, _ = await build_project(db_session, name="Other Project")
    other_environment, other_component = await build_scope(db_session, other_project.id)
    other_incident = await emit_incident(
        db_session,
        other_project,
        other_environment,
        other_component,
        detected_at=hours_before(utcnow(), 4),
        resolved_at=hours_before(utcnow(), 3),
        status="RESOLVED",
    )

    event = await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.REPRODUCTION_CONFIRMED,
        subject_id=uuid.uuid4(),
        payload={"incident_id": str(other_incident.id)},
    )
    await db_session.flush()
    assert event is not None

    summary = await execute_learning_run(
        db_session, project_id=project.id, trigger="test"
    )
    #: Recorded, with its reason — not silently dropped and not acted upon.
    assert summary.unprocessable.get(str(event.subject_id)) == "incident_not_found"
    assert str(other_incident.id) not in summary.unprocessable
    reloaded = await db_session.get(LearningEvent, event.id)
    assert reloaded is not None
    assert reloaded.processed_at is not None
    assert reloaded.unprocessable_reason == "incident_not_found"


@pytest.mark.asyncio
async def test_an_event_with_no_reachable_episode_says_so(db_session):
    """A root-cause decision whose candidate is gone is visible, not fatal."""
    project, _, _ = await build_project(db_session)
    await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.ROOT_CAUSE_CONFIRMED,
        subject_id=uuid.uuid4(),
    )
    await db_session.flush()

    summary = await execute_learning_run(
        db_session, project_id=project.id, trigger="test"
    )
    assert summary.events_processed == 1
    #: The candidate row itself is gone, which is a different fact from "the
    #: incident is gone" — and the ledger distinguishes them.
    assert set(summary.unprocessable.values()) == {"unresolvable_subject"}


@pytest.mark.asyncio
async def test_an_unlinked_event_type_is_recorded_as_not_incident_scoped(db_session):
    """Hypotheses and predictions refresh no episode, and the ledger says so."""
    project, _, _ = await build_project(db_session)
    event = await publish_learning_event(
        db_session,
        project_id=project.id,
        event_type=LearningEventType.FORECAST_CONFIRMED,
        subject_id=uuid.uuid4(),
    )
    await db_session.flush()
    assert event is not None

    summary = await execute_learning_run(
        db_session, project_id=project.id, trigger="test"
    )
    assert summary.events_processed == 1
    assert summary.unprocessable == {}
    reloaded = await db_session.get(LearningEvent, event.id)
    assert reloaded is not None
    assert reloaded.unprocessable_reason == "not_incident_scoped"


@pytest.mark.asyncio
async def test_the_hooks_publish_the_subject_and_the_incident_link(db_session):
    """The contract the resolver depends on, pinned at its source."""
    project, environment, component, episode = await _project_with_episode(
        db_session, remediate=False
    )
    incident = episode["incident"]
    action = await emit_remediation(
        db_session,
        project,
        environment,
        component,
        incident=incident,
        completed_at=incident.resolved_at,
    )
    await record_remediation_completed(db_session, action=action)
    await db_session.flush()

    event = await db_session.scalar(
        select(LearningEvent).where(LearningEvent.subject_id == action.id)
    )
    assert event is not None
    assert event.payload["incident_id"] == str(incident.id)
    assert event.payload["component_id"] == str(component.id)


@pytest.mark.asyncio
async def test_patch_verification_events_carry_a_count_not_a_row(db_session):
    """``Patch.changed_files`` is an integer, and the payload says so.

    Coercing it with ``list()`` raised on every real verification, which the
    best-effort publisher then swallowed — a hook wired to nothing.
    """
    project, _, _ = await build_project(db_session)
    patch_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    patch = SimpleNamespace(
        id=patch_id,
        project_id=project.id,
        changed_files=3,
        status=SimpleNamespace(value="VERIFIED"),
    )
    verification = SimpleNamespace(
        status=SimpleNamespace(value="VERIFIED"),
        level=SimpleNamespace(value="FULL"),
        regression_detected=False,
        completed_at=None,
    )
    await record_patch_verification(
        db_session, patch=patch, verification=verification, incident_id=incident_id
    )
    await db_session.flush()

    event = await db_session.scalar(
        select(LearningEvent).where(LearningEvent.subject_id == patch_id)
    )
    assert event is not None
    assert event.event_type is LearningEventType.PATCH_VERIFIED
    assert event.payload["changed_files"] == 3
    assert event.payload["incident_id"] == str(incident_id)


@pytest.mark.asyncio
async def test_a_reproduction_that_did_not_reproduce_is_recorded_as_such(db_session):
    project, _, _ = await build_project(db_session)
    experiment_id = uuid.uuid4()
    experiment = SimpleNamespace(
        id=experiment_id,
        project_id=project.id,
        incident_id=None,
        result=SimpleNamespace(value="INCONCLUSIVE"),
        confidence=None,
        completed_at=None,
    )
    await record_reproduction_result(db_session, experiment=experiment)
    await db_session.flush()

    event = await db_session.scalar(
        select(LearningEvent).where(LearningEvent.subject_id == experiment_id)
    )
    assert event is not None
    assert event.event_type is LearningEventType.REPRODUCTION_FAILED
