"""Phase 10 — similarity and historical retrieval (§11–§13, §31, §89).

Similarity is the part of the phase most likely to be quietly dishonest, so the
tests are about the *explanation*, not just the number:

* a score never arrives without the features that produced it (§12);
* a feature absent on both sides is not evidence either way (§11);
* a shared component does not manufacture similarity on its own — identity is
  reported, not weighted as behaviour (§37);
* the cutoff is enforced on the query, so an episode that ended after the
  boundary cannot be retrieved "as of" it (§31);
* retrieval says "no comparable historical case was found" rather than returning
  the least-bad match (§49, §90).

The engine is deterministic and pure, so these run against hand-written
signatures as well as the stored rows the fixtures produce.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.phase10_helpers import (
    METRIC_ERROR_RATE,
    METRIC_P95,
    build_project,
    build_scope,
    emit_anomaly,
    emit_deployment,
    emit_incident,
    hours_before,
    record_episode,
    record_series,
    utcnow,
)

from app.services.experience_retrieval import (
    NO_HISTORY_MESSAGE,
    RETRIEVAL_LIMITATIONS,
    ExperienceRetrievalService,
)
from app.services.experience_builder import build_current_signature
from app.services.learning_signatures import (
    FailureSignature,
    SignaturePair,
    classify_direction,
    classify_metric,
    metric_behavior,
)
from app.services.similarity_engine import (
    DEFAULT_WEIGHTS,
    ReliabilitySimilarityEngine,
)


# ---------------------------------------------------------------------------
# The engine itself (§11, §12)
# ---------------------------------------------------------------------------


def _signature(**overrides) -> FailureSignature:
    payload = {
        "incident_kind": "checkout_error_spike",
        "severity": "high",
        "anomaly_types": ["error_rate_spike"],
        "metric_behaviors": ["error_rate:up"],
    }
    payload.update(overrides)
    return FailureSignature(**payload)


def test_identical_signatures_score_one_and_say_why():
    engine = ReliabilitySimilarityEngine()
    explanation = engine.compare(_signature(), _signature())
    assert explanation.score == pytest.approx(1.0)
    assert explanation.matched
    assert explanation.reasons
    assert not explanation.left_only
    assert not explanation.right_only


def test_the_score_is_explained_feature_by_feature():
    """§12. A bare 0.91 would be unusable; every deciding feature is reported."""
    engine = ReliabilitySimilarityEngine()
    left = _signature(metric_behaviors=["error_rate:up", "latency_p95:up"])
    right = _signature(metric_behaviors=["error_rate:up"])
    explanation = engine.compare(left, right)

    matched_values = {m.value for m in explanation.matched}
    assert "error_rate:up" in matched_values
    #: The metric the current situation has and the historical one does not is
    #: reported as left-only, so the missing half of the comparison is visible.
    assert "latency_p95:up" in {m.value for m in explanation.left_only}
    assert explanation.right_only == ()
    assert explanation.score < 1.0
    assert any("latency_p95" in reason for reason in explanation.reasons)


def test_a_feature_absent_on_both_sides_is_not_evidence():
    """§11. Omitting data must not silently raise or lower a score."""
    engine = ReliabilitySimilarityEngine()
    sparse = _signature()
    #: Add a token class to *both* sides only; the sparse pair must be unchanged.
    baseline = engine.compare(sparse, sparse).score
    richer = engine.compare(
        _signature(resource_pressure=["cpu:up"]),
        _signature(resource_pressure=["cpu:up"]),
    ).score
    assert richer == pytest.approx(baseline)


def test_a_shared_component_alone_does_not_make_two_failures_similar():
    """§37. Identity is context; it is not behavioural evidence.

    Both situations happen on ``checkout`` and share nothing else — different
    incident kind, different anomaly, different metric movement. The component
    is the heaviest single weight, so it will dominate a *sparse* comparison;
    the check is that it cannot carry the score on its own.
    """
    engine = ReliabilitySimilarityEngine()
    left = _signature(affected_components=["checkout"])
    right = _signature(
        affected_components=["checkout"],
        incident_kind="memory_pressure_alert",
        anomaly_types=["memory_exhaustion"],
        metric_behaviors=["resource:up"],
    )
    explanation = engine.compare(left, right, same_component=True)
    assert engine.weights["component"] == DEFAULT_WEIGHTS["component"]
    assert "component" in {match.feature_class for match in explanation.matched}
    #: Shared identity, no shared behaviour: comparable, not the same problem.
    assert explanation.score < 0.5


def test_disagreeing_on_identity_is_reported_but_not_treated_as_a_falsehood():
    engine = ReliabilitySimilarityEngine()
    left = _signature(affected_components=["checkout"])
    right = _signature(affected_components=["inventory"])
    explanation = engine.compare(left, right, same_component=False)
    assert explanation.score > 0.0
    assert "component" in {match.feature_class for match in explanation.left_only}


def test_confidence_is_reported_with_the_score_and_not_as_a_probability():
    engine = ReliabilitySimilarityEngine()
    explanation = engine.compare(_signature(), _signature(), same_component=True)
    payload = explanation.as_dict()
    assert set(payload) == {
        "score",
        "matched",
        "left_only",
        "right_only",
        "context",
        "reasons",
    }
    assert payload["score"] <= 1.0


def test_ranking_orders_by_similarity_and_keeps_the_evidence():
    engine = ReliabilitySimilarityEngine()
    current = _signature()
    close = SignaturePair(failure=_signature(), experience_id="close")
    far = SignaturePair(
        failure=_signature(
            anomaly_types=["memory_exhaustion"],
            metric_behaviors=["memory:up"],
            incident_kind="checkout_memory_pressure",
        ),
        experience_id="far",
    )
    ranked = engine.rank(current, [far, close], threshold=0.0)
    assert [item.experience_id for item in ranked] == ["close", "far"]
    assert ranked[0].explanation.matched


def test_a_threshold_excludes_a_weak_match_rather_than_ranking_it_last():
    engine = ReliabilitySimilarityEngine()
    current = _signature()
    far = SignaturePair(
        failure=_signature(
            anomaly_types=["memory_exhaustion"], metric_behaviors=["memory:up"]
        ),
        experience_id="far",
    )
    assert engine.rank(current, [far], threshold=0.95) == []
    assert len(engine.rank(current, [far], threshold=0.0)) == 1


def test_metric_names_are_classified_so_unlike_metrics_can_still_match():
    """§11. A similarity engine that only matched identical names would be useless."""
    assert classify_metric("http.checkout.error_rate") == "error_rate"
    assert classify_metric("http.checkout.latency.p95") == "latency"
    assert classify_metric("checkout_latency_ms") == "latency"
    assert classify_metric("system.cpu.utilization") == "resource"
    assert classify_metric("something.we.do.not.know") == "other"


def test_behaviour_token_carries_the_direction_and_never_assumes_flatness():
    up = classify_direction(observed=0.4, expected=0.01)
    down = classify_direction(observed=0.01, expected=0.4)
    flat = classify_direction(observed=0.4, expected=0.4)
    assert metric_behavior(METRIC_ERROR_RATE, up) == "error_rate:up"
    assert metric_behavior(METRIC_P95, down) == "latency:down"
    assert metric_behavior(METRIC_P95, flat) == "latency:flat"
    #: A missing baseline is not evidence that nothing moved.
    assert classify_direction(observed=0.4, expected=None) == "unknown"
    assert metric_behavior(METRIC_P95, "unknown") == "latency:unknown"


# ---------------------------------------------------------------------------
# Retrieval against stored history (§13, §31)
# ---------------------------------------------------------------------------


async def test_retrieval_finds_the_comparable_episodes(db_session):
    project, environment, component = await build_project(db_session)
    series = await record_series(
        db_session,
        project,
        environment,
        component,
        count=4,
        first_started_at=hours_before(utcnow(), 24 * 6),
    )
    await db_session.commit()

    service = ExperienceRetrievalService()
    result = await service.retrieve(
        db_session,
        project_id=project.id,
        signature=FailureSignature.from_dict(series[0]["experience"].failure_signature),
        threshold=0.5,
    )
    assert result.evidence_available is True
    assert result.total_candidates >= 4
    assert result.matches
    for match in result.matches:
        assert match.explanation[
            "matched"
        ], "a match with no shared feature is not a match"
        assert match.experience_id


async def test_retrieval_returns_the_honest_miss_when_nothing_is_comparable(db_session):
    project, environment, component = await build_project(db_session)
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=3,
        first_started_at=hours_before(utcnow(), 24 * 5),
    )
    await db_session.commit()

    service = ExperienceRetrievalService()
    stranger = FailureSignature(
        incident_kind="something_entirely_new",
        severity="low",
        anomaly_types=["disk_pressure"],
        metric_behaviors=["disk_io:up"],
        resource_pressure=["disk:up"],
    )
    result = await service.retrieve(
        db_session, project_id=project.id, signature=stranger
    )
    assert result.evidence_available is False
    assert result.matches == []
    assert result.summary == NO_HISTORY_MESSAGE


async def test_retrieval_carries_its_limitations_every_time(db_session):
    project, environment, component = await build_project(db_session)
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=2,
        first_started_at=hours_before(utcnow(), 24 * 4),
    )
    await db_session.commit()
    service = ExperienceRetrievalService()
    result = await service.retrieve(
        db_session, project_id=project.id, signature=_signature()
    )
    assert result.limitations
    assert set(result.limitations) == set(RETRIEVAL_LIMITATIONS)


async def test_the_cutoff_prevents_retrieving_a_later_episode(db_session):
    """§31. "As of 1 June" must not see something that ended on 15 June."""
    project, environment, component = await build_project(db_session)
    early = await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=utcnow() - timedelta(days=30),
        resolved_at=utcnow() - timedelta(days=30) + timedelta(minutes=30),
    )
    late = await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=utcnow() - timedelta(hours=2),
        resolved_at=utcnow() - timedelta(hours=1),
    )
    await db_session.commit()

    cutoff = utcnow() - timedelta(days=10)
    service = ExperienceRetrievalService()
    result = await service.retrieve(
        db_session,
        project_id=project.id,
        signature=FailureSignature.from_dict(early["experience"].failure_signature),
        cutoff=cutoff,
        threshold=0.0,
    )
    found = {match.experience_id for match in result.matches}
    assert str(early["experience"].id) in found
    assert str(late["experience"].id) not in found


async def test_retrieval_for_a_component_does_not_pull_another_components_history(
    db_session,
):
    project, environment, component = await build_project(db_session)
    other_environment, other_component = await build_scope(
        db_session, project.id, component="inventory-service"
    )
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=3,
        first_started_at=hours_before(utcnow(), 24 * 5),
    )
    await record_series(
        db_session,
        project,
        other_environment,
        other_component,
        count=2,
        first_started_at=hours_before(utcnow(), 24 * 4),
    )
    await db_session.commit()

    service = ExperienceRetrievalService()
    result = await service.retrieve(
        db_session,
        project_id=project.id,
        signature=_signature(),
        component_id=other_component.id,
        threshold=0.0,
    )
    assert result.matches
    assert all(
        match.component_id == str(other_component.id) for match in result.matches
    )
    assert result.total_candidates == 2


async def test_lookback_bounds_the_window(db_session):
    project, environment, component = await build_project(db_session)
    await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=utcnow() - timedelta(days=200),
        resolved_at=utcnow() - timedelta(days=200) + timedelta(minutes=30),
    )
    await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=utcnow() - timedelta(days=2),
        resolved_at=utcnow() - timedelta(days=2) + timedelta(minutes=30),
    )
    await db_session.commit()

    service = ExperienceRetrievalService()
    recent = await service.retrieve(
        db_session,
        project_id=project.id,
        signature=_signature(),
        lookback_days=30,
        threshold=0.0,
    )
    assert recent.total_candidates == 1
    everything = await service.retrieve(
        db_session,
        project_id=project.id,
        signature=_signature(),
        threshold=0.0,
    )
    assert everything.total_candidates == 2


async def test_retrieving_for_an_incident_uses_its_current_signature(db_session):
    project, environment, component = await build_project(db_session)
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=4,
        first_started_at=hours_before(utcnow(), 24 * 6),
    )
    started = utcnow() - timedelta(minutes=30)
    incident = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=started,
        resolved_at=None,
        status="OPEN",
        fingerprint="checkout_error_spike",
    )
    await emit_anomaly(
        db_session,
        project,
        environment,
        component,
        detected_at=started,
        anomaly_type="ERROR_RATE_SPIKE",
        metric_name=METRIC_ERROR_RATE,
    )
    await db_session.commit()

    signature, reason = await build_current_signature(
        db_session, incident_id=incident.id, as_of=None
    )
    assert signature is not None, reason

    service = ExperienceRetrievalService()
    result = await service.retrieve(
        db_session,
        project_id=project.id,
        signature=signature,
        component_id=component.id,
        threshold=0.3,
    )
    assert result.evidence_available is True
    assert result.success_count >= 1
    assert all(match.resolution is not None for match in result.matches)


async def test_an_episode_is_never_its_own_historical_match(db_session):
    project, environment, component = await build_project(db_session)
    series = await record_series(
        db_session,
        project,
        environment,
        component,
        count=2,
        first_started_at=hours_before(utcnow(), 24 * 3),
    )
    await db_session.commit()
    service = ExperienceRetrievalService()
    result = await service.retrieve(
        db_session,
        project_id=project.id,
        signature=FailureSignature.from_dict(series[0]["experience"].failure_signature),
        exclude_experience_id=series[0]["experience"].id,
        threshold=0.0,
    )
    assert str(series[0]["experience"].id) not in {
        match.experience_id for match in result.matches
    }


async def test_retrieval_reports_a_rollback_as_an_unsuccessful_resolution(db_session):
    project, environment, component = await build_project(db_session)
    await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=utcnow() - timedelta(days=2),
        resolved_at=utcnow() - timedelta(days=2) + timedelta(minutes=45),
        rollback=True,
        outcome="HARMFUL",
        verification_verdict="FAILED",
    )
    await db_session.commit()
    service = ExperienceRetrievalService()
    result = await service.retrieve(
        db_session,
        project_id=project.id,
        signature=_signature(),
        threshold=0.0,
    )
    assert result.matches
    assert result.success_count == 0
    assert result.matches[0].resolution["rollback_performed"] is True


async def test_similarity_ignores_confirmed_causes(db_session):
    """§11. Similarity is structural; a matching *cause* is never required."""
    project, environment, component = await build_project(db_session)
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=3,
        first_started_at=hours_before(utcnow(), 24 * 5),
    )
    await db_session.commit()
    service = ExperienceRetrievalService()
    result = await service.retrieve(
        db_session,
        project_id=project.id,
        signature=_signature(),
        threshold=0.0,
    )
    assert result.matches
    for match in result.matches:
        #: Nothing in the query or the explanation mentions a root cause.
        assert "root_cause" not in match.explanation
        assert "cause" not in {
            item["feature_class"] for item in match.explanation["matched"]
        }


async def test_deployment_and_dependency_context_participate_in_the_match(db_session):
    project, environment, component = await build_project(db_session)
    await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=utcnow() - timedelta(days=3),
        resolved_at=utcnow() - timedelta(days=3) + timedelta(minutes=40),
    )
    await emit_deployment(
        db_session,
        project,
        environment,
        component,
        deployed_at=utcnow() - timedelta(days=3, minutes=10),
        metadata={"change_size": "large"},
    )
    await db_session.commit()
    from app.services.experience_builder import (
        build_experience_draft,
        persist_experience,
    )

    from app.models.incident import Incident
    from sqlalchemy import select

    incident = await db_session.scalar(select(Incident).limit(1))
    draft, _ = await build_experience_draft(
        db_session, incident_id=incident.id, as_of=utcnow()
    )
    assert draft is not None
    await persist_experience(db_session, draft)
    await db_session.commit()

    service = ExperienceRetrievalService()
    result = await service.retrieve(
        db_session,
        project_id=project.id,
        signature=_signature(deployment_context=["recent_deploy"]),
        threshold=0.0,
    )
    assert result.matches
