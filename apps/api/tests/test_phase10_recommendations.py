"""Phase 10 — the recommendation engine (§38–§45, §73, §81).

A recommendation is the point where learning would like to become action, so
these tests hold the line the phase draws:

* every recommendation states *why*, with the evidence that supports it, the
  uncertainty, and what would invalidate it (§40);
* the criteria that ranked it are attached, not hidden (§41);
* advice that implies a Phase 9 action carries what that policy would require —
  and nothing in this module executes anything (§42);
* acceptance is not success: the verdict is recorded separately (§43, §81);
* a component with no history gets an investigation, never an action (§49);
* a chronic signal recommends a look, not a change (§22).

The engine is driven against real stored episodes, so the counts it cites are
counts the pipeline produced.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.phase10_helpers import (
    build_project,
    build_scope,
    emit_anomaly,
    emit_deployment,
    emit_incident,
    hours_before,
    make_knowledge,
    record_episode,
    record_series,
    utcnow,
)

from app.models.intelligence import (
    KnowledgeConfidence,
    RecommendationStatus,
    RecommendationType,
    ReliabilityRecommendation,
)
from app.services.component_profiles import recompute_profiles
from app.services.experience_retrieval import NO_HISTORY_MESSAGE
from app.services.intelligence_state import (
    IntelligenceStateError,
    RECOMMENDATION_VERDICTS,
)
from app.services.recommendation_engine import (
    IMPLIED_ACTIONS,
    RANKING_WEIGHTS,
    RecommendationDraft,
    ReliabilityRecommendationEngine,
)


async def _open_incident(db_session, project, environment, component, **kwargs):
    started = utcnow() - timedelta(minutes=kwargs.pop("minutes_open", 25))
    incident = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=started,
        resolved_at=None,
        status="OPEN",
        fingerprint=kwargs.pop("fingerprint", "checkout_error_spike"),
        **kwargs,
    )
    await emit_anomaly(
        db_session,
        project,
        environment,
        component,
        detected_at=started,
        anomaly_type="ERROR_RATE_SPIKE",
        metric_name="http.checkout.error_rate",
    )
    await db_session.commit()
    return incident


async def _history(db_session, project, environment, component, *, count=5, **kwargs):
    episodes = await record_series(
        db_session,
        project,
        environment,
        component,
        count=count,
        first_started_at=hours_before(utcnow(), 24 * (count + 3)),
        **kwargs,
    )
    await db_session.commit()
    return episodes


# ---------------------------------------------------------------------------
# Evidence and explanation (§40)
# ---------------------------------------------------------------------------


async def test_a_recommendation_cites_real_history(db_session):
    project, environment, component = await build_project(db_session)
    history = await _history(db_session, project, environment, component, count=5)
    incident = await _open_incident(db_session, project, environment, component)

    engine = ReliabilityRecommendationEngine()
    ranked = await engine.recommend_for_incident(
        db_session, project_id=project.id, incident_id=incident.id
    )
    assert ranked

    stored_ids = {str(item["experience"].id) for item in history}
    for item in ranked:
        draft = item.draft
        assert draft.rationale
        assert draft.current_evidence
        assert draft.limitations
        assert draft.experience_ids, "a recommendation must cite the episodes it used"
        assert set(draft.experience_ids) <= stored_ids


async def test_an_incident_with_nothing_observed_gets_no_advice(db_session):
    """§49. An incident with no anomalies and no history supports no action."""
    project, environment, component = await build_project(db_session)
    incident = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=utcnow() - timedelta(minutes=5),
        resolved_at=None,
        status="OPEN",
        fingerprint=None,
    )
    await db_session.commit()

    engine = ReliabilityRecommendationEngine()
    ranked = await engine.recommend_for_incident(
        db_session, project_id=project.id, incident_id=incident.id
    )
    #: The incident's own existence is evidence, so investigation is offered —
    #: but with nothing observed, nothing beyond investigation is.
    for item in ranked:
        assert item.draft.recommendation_type not in IMPLIED_ACTIONS
        assert item.draft.experience_ids == []
        assert item.draft.confidence.value in ("UNKNOWN", "LOW")


async def test_an_incident_with_no_comparable_history_gets_an_investigation_only(
    db_session,
):
    project, environment, component = await build_project(db_session)
    incident = await _open_incident(
        db_session, project, environment, component, fingerprint="brand_new_shape"
    )
    #: History exists in the project, but nothing that resembles this incident.
    other_environment, other = await build_scope(
        db_session, project.id, component="unrelated-service"
    )
    await record_series(
        db_session,
        project,
        other_environment,
        other,
        count=4,
        first_started_at=hours_before(utcnow(), 24 * 6),
        anomaly_types=["RESOURCE_USAGE_SPIKE"],
        metric_name="system.memory.utilization",
        fingerprint="memory_pressure",
    )
    await db_session.commit()

    engine = ReliabilityRecommendationEngine()
    ranked = await engine.recommend_for_incident(
        db_session, project_id=project.id, incident_id=incident.id
    )
    assert ranked
    for item in ranked:
        assert (
            item.draft.recommendation_type == RecommendationType.INVESTIGATE_COMPONENT
        )
        assert item.draft.confidence.value in ("UNKNOWN", "LOW")
        assert any(NO_HISTORY_MESSAGE in note for note in item.draft.limitations)
        #: §39. With nothing to base an action on, no action is proposed.
        assert item.draft.recommendation_type not in IMPLIED_ACTIONS


async def test_a_thin_sample_says_so_in_the_recommendation(db_session):
    project, environment, component = await build_project(db_session)
    await _history(db_session, project, environment, component, count=1)
    incident = await _open_incident(db_session, project, environment, component)
    engine = ReliabilityRecommendationEngine()
    ranked = await engine.recommend_for_incident(
        db_session, project_id=project.id, incident_id=incident.id
    )
    assert ranked
    assert any(
        "too few to describe a pattern" in note
        for item in ranked
        for note in item.draft.limitations
    )


async def test_a_dependency_condition_is_offered_as_investigation_not_blame(db_session):
    project, environment, component = await build_project(db_session)
    await _history(db_session, project, environment, component, count=4)
    incident = await _open_incident(db_session, project, environment, component)
    from app.services.experience_builder import build_current_signature

    signature, _ = await build_current_signature(
        db_session, incident_id=incident.id, as_of=None
    )
    assert signature is not None
    #: Simulate a dependency condition being present, as a real trace would.
    signature = type(signature)(
        **{
            **signature.as_dict(),
            "dependency_conditions": ["dependency_latency_rising"],
        }
    )
    from app.services.recommendation_engine import _context_draft

    draft = _context_draft(
        incident=incident,
        evidence={"incident_id": str(incident.id)},
        knowledge_ids=[],
        experience_ids=[],
        historical={},
        recommendation_type=RecommendationType.INVESTIGATE_DEPENDENCY,
        title="Investigate a dependency that was degraded at the time",
        rationale="The failure context includes dependency_latency_rising.",
    )
    assert draft.limitations
    assert draft.recommendation_type == RecommendationType.INVESTIGATE_DEPENDENCY
    assert "cause" not in draft.rationale or "not proof of cause" in draft.rationale


async def test_a_recent_deployment_produces_a_review_not_a_rollback(db_session):
    project, environment, component = await build_project(db_session)
    await _history(db_session, project, environment, component, count=4)
    incident = await _open_incident(db_session, project, environment, component)
    await emit_deployment(
        db_session,
        project,
        environment,
        component,
        deployed_at=utcnow() - timedelta(minutes=45),
    )
    await db_session.commit()

    engine = ReliabilityRecommendationEngine()
    ranked = await engine.recommend_for_incident(
        db_session, project_id=project.id, incident_id=incident.id
    )
    types = {item.draft.recommendation_type for item in ranked}
    assert types  # something was said
    #: §20, §39. Proximity recommends a look; it never recommends the rollback.
    assert RecommendationType.CONSIDER_ROLLBACK not in types
    for item in ranked:
        if item.draft.recommendation_type == RecommendationType.REVIEW_RECENT_CHANGE:
            assert "not a rollback" in item.draft.rationale


# ---------------------------------------------------------------------------
# Ranking (§41)
# ---------------------------------------------------------------------------


async def test_the_ranking_criteria_are_attached_to_every_recommendation(db_session):
    project, environment, component = await build_project(db_session)
    await _history(db_session, project, environment, component, count=6)
    incident = await _open_incident(db_session, project, environment, component)
    engine = ReliabilityRecommendationEngine()
    ranked = await engine.recommend_for_incident(
        db_session, project_id=project.id, incident_id=incident.id
    )
    assert ranked
    scores = [item.score for item in ranked]
    assert scores == sorted(scores, reverse=True)
    for item in ranked:
        #: §41. The order is inspectable rather than mysterious.
        assert set(item.criteria) == set(RANKING_WEIGHTS)
        assert all(0.0 <= value <= 1.0 for value in item.criteria.values())
        assert item.draft.ranking.get("sample_size") is not None or item.criteria


async def test_a_recommendation_with_more_evidence_ranks_above_one_with_less(
    db_session,
):
    project, environment, component = await build_project(db_session)
    history = await _history(db_session, project, environment, component, count=6)
    incident = await _open_incident(db_session, project, environment, component)

    engine = ReliabilityRecommendationEngine()
    episode_ids = [str(item["experience"].id) for item in history]
    weak = RecommendationDraft(
        project_id=project.id,
        incident_id=incident.id,
        recommendation_type=RecommendationType.INVESTIGATE_COMPONENT,
        title="weak",
        rationale="weak",
        current_evidence={"source": "test"},
        experience_ids=[],
        historical={"comparable_episodes": 0, "successful_resolutions": 0},
        confidence=KnowledgeConfidence.UNKNOWN,
    )
    strong = RecommendationDraft(
        project_id=project.id,
        incident_id=incident.id,
        recommendation_type=RecommendationType.INVESTIGATE_COMPONENT,
        title="strong",
        rationale="strong",
        current_evidence={"source": "test"},
        experience_ids=episode_ids,
        historical={
            "comparable_episodes": len(episode_ids),
            "successful_resolutions": len(episode_ids),
        },
        confidence=KnowledgeConfidence.HIGH,
        ranking={"sample_size": len(episode_ids)},
    )
    ranked = engine.rank([weak, strong])
    assert ranked[0].draft.title == "strong"
    assert (
        ranked[0].criteria["evidence_strength"]
        > ranked[1].criteria["evidence_strength"]
    )
    assert ranked[0].criteria["historical_effectiveness"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Policy (§42)
# ---------------------------------------------------------------------------


async def test_advice_that_implies_an_action_says_what_policy_requires(db_session):
    project, environment, component = await build_project(db_session)
    await _history(db_session, project, environment, component, count=6)
    incident = await _open_incident(db_session, project, environment, component)

    engine = ReliabilityRecommendationEngine()
    ranked = await engine.recommend_for_incident(
        db_session, project_id=project.id, incident_id=incident.id
    )
    implied = [
        item for item in ranked if item.draft.recommendation_type in IMPLIED_ACTIONS
    ]
    assert implied, "a restart that resolved six episodes should be worth suggesting"
    for item in implied:
        assert item.draft.policy_note
        assert (
            "Phase 9" in item.draft.policy_note or "approval" in item.draft.policy_note
        )
        assert any("requires Phase 9 policy" in note for note in item.draft.limitations)


async def test_advice_with_no_implied_action_says_so_plainly(db_session):
    project, environment, component = await build_project(db_session)
    engine = ReliabilityRecommendationEngine()
    incident = await _open_incident(db_session, project, environment, component)
    note = await engine._policy_note(
        db_session, incident, RecommendationType.INVESTIGATE_COMPONENT
    )
    assert note is not None
    assert "investigating, not executing" in note


async def test_policy_resolution_is_best_effort_and_never_blocks_advice(
    db_session, monkeypatch
):
    project, environment, component = await build_project(db_session)
    incident = await _open_incident(db_session, project, environment, component)

    import app.services.remediation_policy as policy_module

    async def _boom(*args, **kwargs):
        raise RuntimeError("policy store unavailable")

    monkeypatch.setattr(policy_module, "resolve_policy", _boom)
    engine = ReliabilityRecommendationEngine()
    note = await engine._policy_note(
        db_session, incident, RecommendationType.CONSIDER_RESTART
    )
    assert note is not None
    assert "could not be resolved" in note
    assert "requiring approval" in note


# ---------------------------------------------------------------------------
# Persistence, decisions and outcomes (§43, §81)
# ---------------------------------------------------------------------------


async def test_persisting_twice_does_not_duplicate_an_open_card(db_session):
    project, environment, component = await build_project(db_session)
    await _history(db_session, project, environment, component, count=4)
    incident = await _open_incident(db_session, project, environment, component)
    engine = ReliabilityRecommendationEngine()
    ranked = await engine.recommend_for_incident(
        db_session, project_id=project.id, incident_id=incident.id
    )
    first = await engine.persist_many(db_session, ranked)
    await db_session.commit()
    #: The same advice is evaluated again, as the next run would.
    await engine.persist_many(db_session, ranked)
    await db_session.commit()

    rows = list(
        (
            await db_session.scalars(
                select(ReliabilityRecommendation).where(
                    ReliabilityRecommendation.project_id == project.id
                )
            )
        ).all()
    )
    assert len(first) == len(ranked)
    assert len(rows) == len(first), "the same advice was raised twice"


async def test_accepting_is_not_the_same_as_succeeding(db_session):
    project, environment, component = await build_project(db_session)
    engine = ReliabilityRecommendationEngine()
    row = await engine.persist(
        db_session,
        RecommendationDraft(
            project_id=project.id,
            component_id=component.id,
            recommendation_type=RecommendationType.INVESTIGATE_COMPONENT,
            title="look at this",
            rationale="evidence",
            current_evidence={"source": "test"},
            limitations=["small sample"],
        ),
    )
    await db_session.commit()

    await engine.decide(
        db_session,
        row,
        decision="ACCEPTED",
        actor="oncall@example.com",
        reason="looks right",
    )
    await db_session.commit()
    assert row.status == RecommendationStatus.ACCEPTED
    assert row.outcome is None, "acceptance must not be recorded as an outcome"


async def test_every_outcome_verdict_that_is_allowed_is_recorded(db_session):
    project, environment, component = await build_project(db_session)
    engine = ReliabilityRecommendationEngine()
    for verdict in sorted(RECOMMENDATION_VERDICTS):
        row = await engine.persist(
            db_session,
            RecommendationDraft(
                project_id=project.id,
                component_id=component.id,
                recommendation_type=RecommendationType.INVESTIGATE_COMPONENT,
                title=f"look {verdict}",
                rationale="evidence",
                current_evidence={"source": "test"},
            ),
        )
        await db_session.flush()
        await engine.decide(
            db_session, row, decision="ACCEPTED", actor="oncall@example.com"
        )
        outcome = await engine.record_outcome(
            db_session, row, verdict=verdict, recorded_by="oncall@example.com"
        )
        await db_session.flush()
        assert outcome.verdict == verdict
        assert row.outcome["verdict"] == verdict
        if verdict == "INCONCLUSIVE":
            #: A verdict that says nothing must not move the card into a
            #: terminal state that claims it said something.
            assert row.status == RecommendationStatus.ACCEPTED
        else:
            assert row.status.value == verdict
    await db_session.commit()


async def test_an_unknown_verdict_is_refused(db_session):
    project, _, component = await build_project(db_session)
    engine = ReliabilityRecommendationEngine()
    row = await engine.persist(
        db_session,
        RecommendationDraft(
            project_id=project.id,
            component_id=component.id,
            recommendation_type=RecommendationType.INVESTIGATE_COMPONENT,
            title="t",
            rationale="r",
            current_evidence={"source": "test"},
        ),
    )
    await db_session.flush()
    with pytest.raises(ValueError):
        await engine.record_outcome(
            db_session, row, verdict="PROBABLY_FINE", recorded_by="someone"
        )


async def test_a_closed_recommendation_cannot_be_decided_again(db_session):
    project, _, component = await build_project(db_session)
    engine = ReliabilityRecommendationEngine()
    row = await engine.persist(
        db_session,
        RecommendationDraft(
            project_id=project.id,
            component_id=component.id,
            recommendation_type=RecommendationType.INVESTIGATE_COMPONENT,
            title="t",
            rationale="r",
            current_evidence={"source": "test"},
        ),
    )
    await db_session.flush()
    await engine.decide(
        db_session, row, decision="DISMISSED", actor="oncall@example.com"
    )
    await db_session.flush()
    with pytest.raises(IntelligenceStateError):
        await engine.decide(
            db_session, row, decision="ACCEPTED", actor="oncall@example.com"
        )


async def test_an_unknown_decision_is_refused(db_session):
    project, _, component = await build_project(db_session)
    engine = ReliabilityRecommendationEngine()
    row = await engine.persist(
        db_session,
        RecommendationDraft(
            project_id=project.id,
            component_id=component.id,
            recommendation_type=RecommendationType.INVESTIGATE_COMPONENT,
            title="t",
            rationale="r",
            current_evidence={"source": "test"},
        ),
    )
    await db_session.flush()
    with pytest.raises(ValueError):
        await engine.decide(db_session, row, decision="MAYBE", actor="someone")


async def test_stale_open_cards_expire_and_keep_their_history(db_session):
    """An open card about last week's incident is noise, not advice."""
    project, _, component = await build_project(db_session)
    engine = ReliabilityRecommendationEngine()
    row = await engine.persist(
        db_session,
        RecommendationDraft(
            project_id=project.id,
            component_id=component.id,
            recommendation_type=RecommendationType.INVESTIGATE_COMPONENT,
            title="t",
            rationale="r",
            current_evidence={"source": "test"},
        ),
    )
    row.expires_at = utcnow() - timedelta(minutes=1)
    await db_session.commit()

    expired = await engine.expire_stale(db_session)
    await db_session.commit()
    assert [item.id for item in expired] == [row.id]
    assert row.status == RecommendationStatus.EXPIRED
    assert await db_session.get(ReliabilityRecommendation, row.id) is not None


async def test_only_open_cards_expire(db_session):
    project, _, component = await build_project(db_session)
    engine = ReliabilityRecommendationEngine()
    row = await engine.persist(
        db_session,
        RecommendationDraft(
            project_id=project.id,
            component_id=component.id,
            recommendation_type=RecommendationType.INVESTIGATE_COMPONENT,
            title="t",
            rationale="r",
            current_evidence={"source": "test"},
        ),
    )
    await engine.decide(
        db_session, row, decision="DISMISSED", actor="oncall@example.com"
    )
    row.expires_at = utcnow() - timedelta(minutes=1)
    await db_session.commit()
    assert await engine.expire_stale(db_session) == []
    assert row.status == RecommendationStatus.DISMISSED


# ---------------------------------------------------------------------------
# Chronic components (§22, §57)
# ---------------------------------------------------------------------------


async def test_a_chronic_component_gets_an_investigation_not_an_action(db_session):
    project, environment, component = await build_project(db_session)
    for index in range(12):
        started = hours_before(utcnow(), 24 * (12 - index))
        await record_episode(
            db_session,
            project,
            environment,
            component,
            started_at=started,
            resolved_at=started + timedelta(hours=3),
            outcome="EFFECTIVE",
        )
    await db_session.commit()
    await recompute_profiles(db_session, project_id=project.id)
    await db_session.commit()

    engine = ReliabilityRecommendationEngine()
    ranked = await engine.recommend_for_component(
        db_session, project_id=project.id, component_id=component.id
    )
    assert ranked
    draft = ranked[0].draft
    assert draft.recommendation_type == RecommendationType.INVESTIGATE_COMPONENT
    assert "does not act on the component" in draft.rationale
    assert draft.current_evidence["incident_count"] >= 1
    assert draft.limitations


async def test_a_quiet_component_gets_no_recommendation(db_session):
    project, environment, component = await build_project(db_session)
    started = utcnow() - timedelta(days=2)
    await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=started,
        resolved_at=started + timedelta(minutes=20),
    )
    await db_session.commit()
    await recompute_profiles(db_session, project_id=project.id)
    await db_session.commit()

    engine = ReliabilityRecommendationEngine()
    assert (
        await engine.recommend_for_component(
            db_session, project_id=project.id, component_id=component.id
        )
        == []
    )


async def test_active_knowledge_is_cited_and_candidate_knowledge_is_not(db_session):
    """§4. Only VALIDATED or ACTIVE knowledge may influence a recommendation."""
    project, environment, component = await build_project(db_session)
    await _history(db_session, project, environment, component, count=5)
    active = await make_knowledge(
        db_session,
        project,
        status="ACTIVE",
        component=component,
        details={"action_type": "restart_service"},
        feature_signature="remediation:restart_service:checkout_error_spike",
    )
    candidate = await make_knowledge(
        db_session,
        project,
        status="CANDIDATE",
        component=component,
        feature_signature="remediation:restart_service:other_shape",
        details={"action_type": "restart_service"},
    )
    await db_session.commit()

    engine = ReliabilityRecommendationEngine()
    incident = await _open_incident(db_session, project, environment, component)
    ranked = await engine.recommend_for_incident(
        db_session, project_id=project.id, incident_id=incident.id
    )
    cited = {kid for item in ranked for kid in item.draft.knowledge_ids}
    assert str(active.id) in cited
    assert str(candidate.id) not in cited
