"""Phase 10 — the deterministic demos (§93–§98).

Each demo is the phase's own narrative pinned as a test, and each is written so
that a failure means the story stopped being true rather than that a number
moved:

* **§93 the learning loop.** Two episodes resolve the same way, a third arrives,
  and ARGUS retrieves the two comparable cases and says "restart, on two
  comparable cases, small sample" — with the limitation attached.
* **§94 the failed remediation.** A fourth episode where the same action fails
  must change the picture: restart is *not* universally effective, and the
  knowledge is scoped rather than global.
* **§95 the regression.** A patch that verified and later regressed becomes a
  regression pattern, and the historical outcome stays visible.
* **§96 the chronic component.** Ten incidents raise a signal and an
  investigation recommendation. Nothing is disabled, paused or modified.
* **§97 no historical knowledge.** A brand-new component gets the honest miss.
* **§98 the learning run.** A full cycle reports what it actually processed.

Nothing here is hard-coded: every count in an assertion is read from stored rows
the run produced.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.phase10_helpers import (
    build_project,
    build_scope,
    emit_anomaly,
    emit_incident,
    hours_before,
    record_episode,
    record_series,
    utcnow,
)

from app.models.fix import FixCategory, FixHypothesis, Patch, PatchVerificationRun
from app.models.intelligence import (
    KnowledgeType,
    RecommendationType,
    ReliabilityExperience,
    ReliabilityKnowledge,
)
from app.services.component_profiles import recompute_profiles
from app.services.experience_retrieval import NO_HISTORY_MESSAGE
from app.services.knowledge_search import KnowledgeSearchService
from app.services.learning_hooks import record_incident_completed
from app.services.learning_run import execute_learning_run
from app.services.pattern_miners import RegressionPatternMiner, load_corpus
from app.services.recommendation_engine import ReliabilityRecommendationEngine


async def _learn(db_session, project):
    """Run the pipeline and commit, the way the sweep does."""
    summary = await execute_learning_run(
        db_session, project_id=project.id, trigger="demo"
    )
    await db_session.commit()
    return summary


async def _open_incident(db_session, project, environment, component, **kwargs):
    started = utcnow() - timedelta(minutes=kwargs.pop("minutes_open", 20))
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


# ---------------------------------------------------------------------------
# §93 — the learning loop
# ---------------------------------------------------------------------------


async def test_demo_93_inventory_latency_then_checkout_errors_then_a_repeat(db_session):
    """Inventory latency → checkout errors → restart → recovery, twice, then reuse."""
    project, environment, component = await build_project(
        db_session, name="Demo Commerce"
    )
    #: Two completed incidents: dependency latency surfaced as a checkout error
    #: spike, both resolved by restarting the inventory service.
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=2,
        first_started_at=hours_before(utcnow(), 24 * 12),
        spacing_hours=24.0,
        anomaly_types=("LATENCY_SPIKE", "ERROR_RATE_SPIKE"),
        metric_name="http.checkout.error_rate",
        fingerprint="checkout_error_spike",
        action_type="RESTART_SERVICE",
        outcome="EFFECTIVE",
    )
    await db_session.commit()

    #: The third incident is open and looks the same.
    incident = await _open_incident(db_session, project, environment, component)

    #: 1. Retrieval finds the two comparable episodes and explains each match.
    search = KnowledgeSearchService()
    answer = await search.search(
        db_session,
        project_id=project.id,
        question="Have we seen this before?",
        incident_id=incident.id,
    )
    assert answer.evidence_available is True
    assert len(answer.experiences) == 2
    for experience in answer.experiences:
        assert experience["explanation"]["matched"]
        assert experience["outcome"]
    assert "2 comparable historical episode(s)" in answer.answer
    assert "RESTART_SERVICE" in answer.answer
    assert answer.citations

    #: 2. The recommendation names the action, cites the episodes, and states the
    #:    small sample rather than presenting two cases as a rule.
    engine = ReliabilityRecommendationEngine()
    ranked = await engine.recommend_for_incident(
        db_session, project_id=project.id, incident_id=incident.id
    )
    assert ranked
    restart = next(
        (
            item
            for item in ranked
            if item.draft.recommendation_type == RecommendationType.CONSIDER_RESTART
        ),
        None,
    )
    assert restart is not None, "a restart that resolved both cases was not suggested"
    draft = restart.draft
    assert len(draft.experience_ids) == 2
    assert "2 comparable episodes" in draft.rationale
    assert any("too few to describe a pattern" in note for note in draft.limitations)
    assert any("do not guarantee" in note for note in draft.limitations)
    #: Two cases is weak evidence, and the confidence says so rather than
    #: dressing two episodes up as a rate (§34).
    assert draft.confidence.value in ("UNKNOWN", "LOW", "MEDIUM")

    #: 3. And it is advice: applying it still goes through Phase 9 policy.
    assert draft.policy_note
    assert "Phase 9" in draft.policy_note or "approval" in draft.policy_note


# ---------------------------------------------------------------------------
# §94 — a failed remediation changes the knowledge
# ---------------------------------------------------------------------------


async def test_demo_94_restart_fails_and_the_knowledge_becomes_contextual(db_session):
    """A fourth incident where restart does not work must change the picture."""
    project, environment, component = await build_project(
        db_session, name="Demo Commerce Failed Fix"
    )
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=3,
        first_started_at=hours_before(utcnow(), 24 * 16),
        spacing_hours=24.0,
        fingerprint="checkout_error_spike",
        action_type="RESTART_SERVICE",
        outcome="EFFECTIVE",
    )
    #: Incident #4: the same action, attempted again, fails and is escalated.
    failed_start = hours_before(utcnow(), 24 * 2)
    await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=failed_start,
        resolved_at=failed_start + timedelta(minutes=50),
        fingerprint="checkout_error_spike",
        action_type="RESTART_SERVICE",
        outcome="INEFFECTIVE",
        verification_verdict="FAILED",
        rollback=True,
    )
    await db_session.commit()

    summary = await _learn(db_session, project)
    assert summary.status.value == "COMPLETED"

    #: The effectiveness table now reports four comparable cases, not three, and
    #: counts the failure — it does not round it away.
    buckets = await _effectiveness(db_session, project)
    assert buckets
    overall = next(bucket for bucket in buckets if bucket.action_type)
    assert overall.comparable == 4
    assert overall.successful == 3
    assert overall.rolled_back == 1
    assert overall.success_ratio == pytest.approx(0.75)
    assert "3 of 4 comparable" in overall.headline()

    #: And ARGUS raises reviewing the remediation path, not repeating it.
    engine = ReliabilityRecommendationEngine()
    open_incident = await _open_incident(db_session, project, environment, component)
    ranked = await engine.recommend_for_incident(
        db_session, project_id=project.id, incident_id=open_incident.id
    )
    types = {item.draft.recommendation_type for item in ranked}
    assert RecommendationType.REVIEW_REMEDIATION in types
    review = next(
        item
        for item in ranked
        if item.draft.recommendation_type == RecommendationType.REVIEW_REMEDIATION
    )
    assert "rolled back" in review.draft.rationale
    #: The honest caveat: a rollback is not automatically proof of a bad action.
    assert any("precautionary" in note for note in review.draft.limitations)


async def _effectiveness(db_session, project):
    from app.services.remediation_effectiveness import action_effectiveness

    return await action_effectiveness(
        db_session, project_id=project.id, breakdown=["action"]
    )


# ---------------------------------------------------------------------------
# §95 — a regression that only shows up later
# ---------------------------------------------------------------------------


async def test_demo_95_a_verified_patch_that_later_regresses(db_session):
    project, environment, component = await build_project(
        db_session, name="Demo Commerce Regression"
    )
    incident = await emit_incident(
        db_session,
        project,
        environment,
        component,
        detected_at=utcnow() - timedelta(days=20),
        resolved_at=utcnow() - timedelta(days=19),
        status="RESOLVED",
    )
    await db_session.flush()

    #: Patch #5 verified, then regressed.
    for index, regressed in enumerate((False, True)):
        completed = utcnow() - timedelta(days=8 - index)
        hypothesis = FixHypothesis(
            project_id=project.id,
            incident_id=incident.id,
            title=f"timeout handling fix {index}",
            description="adjust the retry budget",
            proposed_change="Reduce the retry budget",
            category=FixCategory.TIMEOUT_FIX,
            scope_files=["services/inventory/repository.py"],
            target_symbols=["DB_TIMEOUT_SECONDS"],
        )
        db_session.add(hypothesis)
        await db_session.flush()
        patch = Patch(
            project_id=project.id,
            fix_hypothesis_id=hypothesis.id,
            patch_content="--- a/x\n+++ b/x\n",
            affected_paths=["services/inventory/repository.py"],
        )
        db_session.add(patch)
        await db_session.flush()
        db_session.add(
            PatchVerificationRun(
                project_id=project.id,
                patch_id=patch.id,
                started_at=completed - timedelta(minutes=5),
                completed_at=completed,
                regression_detected=regressed,
                verdict_reason="REGRESSION_DETECTED" if regressed else "VERIFIED",
            )
        )
    await db_session.commit()

    corpus = await load_corpus(db_session, project_id=project.id, cutoff=utcnow())
    patterns = await RegressionPatternMiner().mine(db_session, corpus)
    assert patterns
    timeout_pattern = next(
        item for item in patterns if item.details["category"] == "TIMEOUT_FIX"
    )
    assert timeout_pattern.details["regressions"] == 1
    assert timeout_pattern.sample_count == 2
    assert "not a rule about" in timeout_pattern.description

    #: The historical outcome is visible through the pipeline, not just in the
    #: miner: the run records it as knowledge.
    await _learn(db_session, project)
    stored = list(
        (
            await db_session.scalars(
                select(ReliabilityKnowledge).where(
                    ReliabilityKnowledge.project_id == project.id
                )
            )
        ).all()
    )
    regression = [
        row for row in stored if row.knowledge_type == KnowledgeType.REGRESSION_PATTERN
    ]
    assert regression, "the regression was not recorded as knowledge"
    for row in regression:
        assert row.sample_count >= 1
        assert row.experience_ids or row.sources
        assert row.limitations


# ---------------------------------------------------------------------------
# §96 — a chronic component
# ---------------------------------------------------------------------------


async def test_demo_96_checkout_is_chronically_unreliable_and_only_gets_investigated(
    db_session,
):
    project, environment, component = await build_project(
        db_session, name="Demo Commerce Chronic"
    )
    for index in range(10):
        started = hours_before(utcnow(), 24 * (10 - index))
        await record_episode(
            db_session,
            project,
            environment,
            component,
            started_at=started,
            resolved_at=started + timedelta(hours=3),
            fingerprint="checkout_error_spike",
            outcome="EFFECTIVE" if index % 2 else "INEFFECTIVE",
            verification_verdict="VERIFIED" if index % 2 else "FAILED",
            rollback=index % 3 == 0,
        )
    await db_session.commit()

    await _learn(db_session, project)
    profiles = await recompute_profiles(db_session, project_id=project.id)
    await db_session.commit()

    chronic = [profile for profile in profiles if profile.chronic_signal]
    assert chronic, "ten incidents should raise a chronic signal"
    assert all(profile.chronic_reasons for profile in chronic)

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

    #: §96. Nothing about the component was changed, stopped or disabled, and
    #: the signal itself created no remediation: the actions below are the
    #: fixture's own history, and the count must not move.
    from app.models.remediation import RemediationAction
    from app.models.system import SystemComponent

    def action_count(rows) -> int:
        return len({str(row.id) for row in rows})

    actions_before = action_count(
        (
            await db_session.scalars(
                select(RemediationAction).where(
                    RemediationAction.project_id == project.id
                )
            )
        ).all()
    )
    ranked = await engine.recommend_for_component(
        db_session, project_id=project.id, component_id=component.id
    )
    await db_session.commit()
    actions_after = action_count(
        (
            await db_session.scalars(
                select(RemediationAction).where(
                    RemediationAction.project_id == project.id
                )
            )
        ).all()
    )
    assert ranked
    assert actions_after == actions_before

    refreshed = await db_session.get(SystemComponent, component.id)
    assert refreshed is not None
    assert refreshed.status == component.status
    assert refreshed.name == component.name


# ---------------------------------------------------------------------------
# §97 — no historical knowledge
# ---------------------------------------------------------------------------


async def test_demo_97_a_brand_new_component_gets_the_honest_miss(db_session):
    project, environment, component = await build_project(
        db_session, name="Demo Commerce New Component"
    )
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=3,
        first_started_at=hours_before(utcnow(), 24 * 6),
    )
    new_environment, new_component = await build_scope(
        db_session, project.id, component="brand-new-checkout"
    )
    await db_session.commit()

    incident = await _open_incident(
        db_session,
        project,
        new_environment,
        new_component,
        fingerprint="never_seen_shape",
        title="A failure we have no history for",
    )
    answer = await KnowledgeSearchService().search(
        db_session,
        project_id=project.id,
        question="Have we seen this before?",
        incident_id=incident.id,
    )
    assert answer.evidence_available is False
    assert answer.answer == NO_HISTORY_MESSAGE
    assert answer.citations == []

    engine = ReliabilityRecommendationEngine()
    ranked = await engine.recommend_for_incident(
        db_session, project_id=project.id, incident_id=incident.id
    )
    #: Investigation only — ARGUS does not recommend an action it cannot support.
    assert ranked
    assert all(
        item.draft.recommendation_type == RecommendationType.INVESTIGATE_COMPONENT
        for item in ranked
    )
    assert all(item.draft.experience_ids == [] for item in ranked)
    assert all(
        any(NO_HISTORY_MESSAGE in note for note in item.draft.limitations)
        for item in ranked
    )


# ---------------------------------------------------------------------------
# §98 — a full learning cycle
# ---------------------------------------------------------------------------


async def test_demo_98_a_full_run_reports_what_it_actually_processed(db_session):
    project, environment, component = await build_project(
        db_session, name="Demo Commerce Learning Run"
    )
    #: A few completed incidents, each published as an event, so the run has real
    #: work: convert events → experiences → patterns → knowledge.
    for index in range(5):
        started = hours_before(utcnow(), 24 * (9 - index))
        incident = await emit_incident(
            db_session,
            project,
            environment,
            component,
            detected_at=started,
            resolved_at=started + timedelta(minutes=30),
            status="RESOLVED",
            fingerprint="checkout_error_spike",
        )
        await emit_anomaly(
            db_session,
            project,
            environment,
            component,
            detected_at=started + timedelta(minutes=1),
            anomaly_type="ERROR_RATE_SPIKE",
            metric_name="http.checkout.error_rate",
        )
        await db_session.flush()
        await record_incident_completed(db_session, incident=incident)
    await db_session.commit()

    summary = await _learn(db_session, project)

    #: Every number reported is a number that happened.
    assert summary.status.value == "COMPLETED"
    assert summary.run_id
    assert summary.events_processed == 5
    assert summary.experiences_created == 5
    assert summary.patterns_discovered >= 1
    assert summary.errors == []

    #: The ledger agrees with the summary, so the run is auditable after the fact.
    from app.models.intelligence import LearningRun

    run = await db_session.get(LearningRun, uuid.UUID(summary.run_id))
    assert run is not None
    assert run.status.value == "COMPLETED"
    assert run.events_processed == summary.events_processed
    assert run.patterns_discovered == summary.patterns_discovered
    assert run.data_cutoff is not None
    assert run.algorithm_versions

    experiences = list(
        (
            await db_session.scalars(
                select(ReliabilityExperience).where(
                    ReliabilityExperience.project_id == project.id
                )
            )
        ).all()
    )
    assert len(experiences) == 5
    assert all(row.learning_run_id is not None for row in experiences)

    #: A second run over the same history finds nothing new and duplicates
    #: nothing — the counts move, the knowledge does not double.
    before = {
        row.fingerprint
        for row in (
            await db_session.scalars(
                select(ReliabilityKnowledge).where(
                    ReliabilityKnowledge.project_id == project.id
                )
            )
        ).all()
    }
    second = await _learn(db_session, project)
    after = {
        row.fingerprint
        for row in (
            await db_session.scalars(
                select(ReliabilityKnowledge).where(
                    ReliabilityKnowledge.project_id == project.id
                )
            )
        ).all()
    }
    #: Nothing new arrived, so the second run converts no events and rebuilds
    #: nothing — and, critically, it does not double the knowledge.
    assert second.events_processed == 0
    assert second.experiences_created == 0
    assert second.experiences_updated == 0
    assert after == before
    assert len(after) >= 1
