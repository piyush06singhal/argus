"""Phase 10 — reliability experiences (§8, §9, §10, §30, §31).

The experience is the unit every later stage stands on, so the tests here are
about what it *records*: the normalized signature (not raw telemetry), the
outcome (including the honest "unresolved unverified" case), the quality verdict
that decides whether it may be mined, and the temporal boundary that keeps
hindsight out of history.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.models.intelligence import ReliabilityExperience
from app.services.experience_builder import (
    OUTCOME_RESOLVED_UNVERIFIED,
    OUTCOME_SELF_RECOVERED,
    QUALITY_LIMITED,
    QUALITY_POOR,
    build_experience_draft,
    build_experiences,
    persist_experience,
)
from tests.phase10_helpers import (
    METRIC_ERROR_RATE,
    build_project,
    build_scope,
    days_before,
    emit_anomaly,
    emit_deployment,
    emit_incident,
    emit_remediation,
    hours_before,
    record_episode,
    utcnow,
)


@pytest.mark.asyncio
async def test_episode_becomes_a_normalized_experience(db_session):
    """A remediated, resolved incident becomes one experience with both signatures."""
    project, environment, component = await build_project(db_session)
    started = utcnow() - timedelta(hours=3)
    resolved = started + timedelta(minutes=45)

    bundle = await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=started,
        resolved_at=resolved,
        fingerprint="checkout_error_spike",
    )
    experience = bundle["experience"]
    assert experience is not None, bundle["reason"]

    assert experience.incident_id == bundle["incident"].id
    assert experience.primary_component_id == component.id
    assert experience.remediation_action_id == bundle["action"].id
    assert experience.recovery_seconds == 45 * 60
    assert experience.data_quality != QUALITY_POOR
    #: The outcome token is normalized, so no caller has to know that Phase 9
    #: stores ``EFFECTIVE`` while the learning layer compares ``effective``.
    assert experience.outcome == experience.resolution_signature["outcome"]

    signature = experience.failure_signature
    assert signature["incident_kind"] == "checkout_error_spike"
    assert "latency_spike" in signature["anomaly_types"]
    assert "error_rate:up" in signature["metric_behaviors"]
    #: The signature is a summary, never a copy of telemetry.
    assert "observed_value" not in str(signature)
    assert len(experience.failure_fingerprint) == 64

    resolution = experience.resolution_signature
    assert resolution["action_types"] == ["restart_service"]
    assert resolution["restart"] is True
    assert resolution["verification_verdict"] == "verified"
    assert resolution["recovery_bucket"] == "MODERATE"
    assert experience.outcome == "effective"


@pytest.mark.asyncio
async def test_experience_without_remediation_records_self_recovery(db_session):
    """An incident that recovered on its own is history too — not a gap."""
    project, environment, component = await build_project(db_session)
    started = utcnow() - timedelta(hours=5)
    resolved = started + timedelta(minutes=20)

    bundle = await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=started,
        resolved_at=resolved,
        remediate=False,
    )
    experience = bundle["experience"]
    assert experience is not None
    assert experience.remediation_action_id is None
    assert experience.outcome == OUTCOME_SELF_RECOVERED
    assert experience.resolution_signature["action_types"] == []


@pytest.mark.asyncio
async def test_resolution_without_a_recorded_outcome_is_not_called_effective(
    db_session,
):
    """An action with no outcome must not be promoted to success by the builder."""
    project, environment, component = await build_project(db_session)
    started = utcnow() - timedelta(hours=2)
    resolved = started + timedelta(minutes=10)

    incident = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=started,
        resolved_at=resolved,
        fingerprint="checkout_error_spike",
    )
    await emit_anomaly(
        db_session,
        project,
        environment,
        component,
        detected_at=started + timedelta(minutes=1),
        anomaly_type="ERROR_RATE_SPIKE",
        metric_name=METRIC_ERROR_RATE,
    )
    await emit_remediation(
        db_session,
        project,
        environment,
        component,
        incident=incident,
        outcome=None,
        verification_verdict=None,
        completed_at=resolved,
    )
    await db_session.flush()

    draft, reason = await build_experience_draft(
        db_session, incident_id=incident.id, as_of=resolved + timedelta(seconds=1)
    )
    assert draft is not None, reason
    assert draft.outcome == OUTCOME_RESOLVED_UNVERIFIED
    assert draft.resolution_signature.succeeded is False


@pytest.mark.asyncio
async def test_rollback_is_recorded_as_an_unsuccessful_resolution(db_session):
    """A reversed action is not a success even if the incident later resolved."""
    project, environment, component = await build_project(db_session)
    started = utcnow() - timedelta(hours=4)
    resolved = started + timedelta(minutes=30)

    bundle = await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=started,
        resolved_at=resolved,
        outcome="HARMFUL",
        rollback=True,
        verification_verdict="FAILED",
    )
    experience = bundle["experience"]
    assert experience is not None
    assert experience.resolution_signature["rollback_performed"] is True
    assert experience.outcome == "harmful"


@pytest.mark.asyncio
async def test_experience_with_no_observed_signals_is_flagged_limited(db_session):
    """§30: a row with no signals is stored but marked, not silently mined."""
    project, environment, component = await build_project(db_session)
    started = utcnow() - timedelta(hours=1)

    incident = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=started,
        resolved_at=started + timedelta(minutes=5),
        fingerprint="unexplained",
    )
    await db_session.flush()

    draft, reason = await build_experience_draft(
        db_session, incident_id=incident.id, as_of=utcnow()
    )
    assert draft is not None, reason
    assert draft.quality.level == QUALITY_LIMITED
    assert "no_observed_signals" in draft.quality.reasons


@pytest.mark.asyncio
async def test_an_incident_that_has_not_completed_yields_a_reason_not_a_row(db_session):
    """An open incident is not history yet, and the refusal is explicit."""
    project, environment, component = await build_project(db_session)
    incident = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=utcnow() - timedelta(minutes=10),
        status="INVESTIGATING",
        fingerprint="open_one",
    )
    await db_session.flush()

    draft, reason = await build_experience_draft(
        db_session, incident_id=incident.id, as_of=utcnow()
    )
    assert draft is None
    assert reason is not None and reason.startswith("incident_not_completed")


@pytest.mark.asyncio
async def test_deployments_and_dependencies_become_context_tokens(db_session):
    """§20: deployment proximity and dependency state are recorded as context."""
    project, environment, component = await build_project(db_session)
    _, dependency = await build_scope(
        db_session, project.id, component="inventory-service"
    )
    from tests.phase10_helpers import link_dependency

    await link_dependency(db_session, project, component, dependency)

    started = utcnow() - timedelta(hours=6)
    resolved = started + timedelta(minutes=40)
    await emit_deployment(
        db_session,
        project,
        environment,
        component,
        deployed_at=started - timedelta(hours=2),
        status="SUCCESS",
    )
    bundle = await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=started,
        resolved_at=resolved,
        extra_components=[dependency],
        fingerprint="checkout_error_spike",
    )
    signature = bundle["experience"].failure_signature
    assert "recent_deployment" in signature["deployment_context"]
    assert "dependency_degraded" in signature["dependency_conditions"]


@pytest.mark.asyncio
async def test_rebuilding_the_same_episode_does_not_create_a_second_row(db_session):
    """§64: one episode is one experience, however many times a run sees it."""
    project, environment, component = await build_project(db_session)
    started = utcnow() - timedelta(hours=2)
    resolved = started + timedelta(minutes=15)
    bundle = await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=started,
        resolved_at=resolved,
    )
    incident = bundle["incident"]

    second, reason = await build_experience_draft(
        db_session, incident_id=incident.id, as_of=resolved + timedelta(seconds=1)
    )
    assert second is not None, reason
    _row, status = await persist_experience(db_session, second)
    assert status == "unchanged"

    count = len(
        (
            await db_session.execute(
                ReliabilityExperience.__table__.select().where(
                    ReliabilityExperience.incident_id == incident.id
                )
            )
        ).all()
    )
    assert count == 1


@pytest.mark.asyncio
async def test_a_changed_outcome_updates_the_existing_experience(db_session):
    """A reopened incident that resolved differently updates its row in place."""
    project, environment, component = await build_project(db_session)
    started = utcnow() - timedelta(hours=3)
    resolved = started + timedelta(minutes=10)
    bundle = await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=started,
        resolved_at=resolved,
    )
    incident = bundle["incident"]

    # The incident is reopened and resolves later with a different outcome.
    later = resolved + timedelta(hours=2)
    incident.resolved_at = later
    for anomaly in bundle["anomalies"]:
        anomaly.observed_value = 0.9
    await db_session.flush()

    draft, reason = await build_experience_draft(
        db_session, incident_id=incident.id, as_of=later + timedelta(seconds=1)
    )
    assert draft is not None, reason
    row, status = await persist_experience(db_session, draft)
    assert status == "updated"
    assert row.id == bundle["experience"].id
    assert row.end_time == later


@pytest.mark.asyncio
async def test_experience_may_not_be_built_from_the_future(db_session):
    """§31: nothing after the cutoff may enter the experience."""
    project, environment, component = await build_project(db_session)
    started = utcnow() - timedelta(hours=1)
    resolved = utcnow()

    incident = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=started,
        resolved_at=resolved,
        fingerprint="recent_one",
    )
    await db_session.flush()

    draft, reason = await build_experience_draft(
        db_session,
        incident_id=incident.id,
        as_of=resolved - timedelta(minutes=30),
    )
    assert draft is None
    assert reason == "incident_after_cutoff"


@pytest.mark.asyncio
async def test_anomalies_after_the_cutoff_are_excluded(db_session):
    """§31: the window filter is applied in the query, not trusted to callers."""
    project, environment, component = await build_project(db_session)
    started = utcnow() - timedelta(hours=6)
    resolved = started + timedelta(minutes=30)

    incident = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=started,
        resolved_at=resolved,
        fingerprint="bounded_window",
    )
    await emit_anomaly(
        db_session,
        project,
        environment,
        component,
        detected_at=started + timedelta(minutes=5),
        anomaly_type="LATENCY_SPIKE",
        metric_name=METRIC_ERROR_RATE,
    )
    await db_session.flush()

    draft, reason = await build_experience_draft(
        db_session, incident_id=incident.id, as_of=resolved + timedelta(minutes=1)
    )
    assert draft is not None, reason
    assert draft.failure_signature.anomaly_types == ("latency_spike",)


@pytest.mark.asyncio
async def test_a_resolved_incident_without_a_resolution_moment_uses_updated_at(
    db_session,
):
    """The window is established from the incident's own timestamps, honestly.

    ``detected_at`` is NOT NULL in the Phase 3 schema, so the "no window at all"
    branch is unreachable for rows ARGUS writes — the reachable question is which
    fallback the builder uses when a closer marked the incident resolved without
    recording when.
    """
    project, environment, component = await build_project(db_session)
    started = utcnow() - timedelta(hours=2)
    incident = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=started,
        resolved_at=None,
        status="RESOLVED",
        fingerprint="no_resolution_moment",
    )
    await db_session.flush()

    draft, reason = await build_experience_draft(
        db_session, incident_id=incident.id, as_of=utcnow()
    )
    assert draft is not None, reason
    assert draft.start_time == started
    #: SQLite hands back a naive ``updated_at``; the builder normalizes it to UTC
    #: rather than guessing a local timezone.
    assert draft.end_time.replace(tzinfo=None) == incident.updated_at


@pytest.mark.asyncio
async def test_build_experiences_reports_created_updated_and_unprocessable(db_session):
    """The batch entry point reports what it did, per incident."""
    project, environment, component = await build_project(db_session)
    started = utcnow() - timedelta(hours=8)
    good = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=started,
        resolved_at=started + timedelta(minutes=20),
        fingerprint="good_one",
    )
    open_one = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=started,
        resolved_at=None,
        status="OPEN",
        fingerprint="open_one",
    )
    await db_session.flush()

    result = await build_experiences(
        db_session,
        incident_ids=[good.id, open_one.id],
        cutoff=utcnow(),
    )
    assert len(result.created) == 1
    assert str(open_one.id) in result.unprocessable

    again = await build_experiences(db_session, incident_ids=[good.id], cutoff=utcnow())
    assert again.unchanged == 1
    assert not again.created


@pytest.mark.asyncio
async def test_missing_anomaly_values_are_flagged_but_not_fatal(db_session):
    """A signal with no observed value is a data-quality flag, not a crash."""
    project, environment, component = await build_project(db_session)
    started = utcnow() - timedelta(hours=2)
    incident = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=started,
        resolved_at=started + timedelta(minutes=10),
        fingerprint="no_values",
    )
    await emit_anomaly(
        db_session,
        project,
        environment,
        component,
        detected_at=started + timedelta(minutes=1),
        anomaly_type="ERROR_RATE_SPIKE",
        metric_name=METRIC_ERROR_RATE,
    )
    await db_session.flush()

    draft, reason = await build_experience_draft(
        db_session, incident_id=incident.id, as_of=utcnow()
    )
    assert draft is not None, reason
    assert "no_observed_values" in draft.quality.reasons


@pytest.mark.asyncio
async def test_unknown_incident_is_unprocessable(db_session):
    draft, reason = await build_experience_draft(
        db_session, incident_id=uuid.uuid4(), as_of=utcnow()
    )
    assert draft is None
    assert reason == "incident_not_found"


@pytest.mark.asyncio
async def test_deployment_lookback_is_bounded(db_session):
    """A deployment a week earlier is not context for today's failure (§20)."""
    project, environment, component = await build_project(db_session)
    started = utcnow() - timedelta(hours=4)
    await emit_deployment(
        db_session,
        project,
        environment,
        component,
        deployed_at=days_before(started, 7),
        status="SUCCESS",
    )
    bundle = await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=started,
        resolved_at=started + timedelta(minutes=10),
    )
    assert (
        "recent_deployment"
        not in bundle["experience"].failure_signature["deployment_context"]
    )

    await emit_deployment(
        db_session,
        project,
        environment,
        component,
        deployed_at=hours_before(started, 1),
        status="FAILED",
    )
    await db_session.flush()
    second, reason = await build_experience_draft(
        db_session,
        incident_id=bundle["incident"].id,
        as_of=started + timedelta(minutes=11),
    )
    assert second is not None, reason
    assert "failed_deployment" in second.failure_signature.deployment_context
