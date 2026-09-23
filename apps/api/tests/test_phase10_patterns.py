"""Phase 10 — pattern discovery, effectiveness and chronic signals (§17–§22, §43–§45).

Two claims have to be held apart here, and both suites are written to hold them:

* **Effectiveness is a count over a stated sample, never a guarantee (§15, §16).**
  Every bucket carries its sample size, the minimum it was measured against, and
  whether that sample is even sufficient to describe a rate. A bucket that is
  too small says so instead of printing 100%.
* **A pattern is an observation, not a rule (§18, §20, §22).** A recurring shape
  is labelled ``OBSERVED PATTERN``; a component with repeated trouble produces a
  signal to investigate, never an action against it.

The comparison functions get their own tests because §45 is where a system is
most tempted to turn "A looked better than B" into "use A".
"""

from __future__ import annotations

from datetime import timedelta


from tests.phase10_helpers import (
    build_project,
    build_scope,
    emit_incident,
    hours_before,
    record_episode,
    record_series,
    utcnow,
)

from app.models.intelligence import (
    KnowledgeScope,
    KnowledgeType,
)
from app.services.component_profiles import (
    compute_profile,
    recompute_profiles,
)
from app.services.pattern_miners import (
    OBSERVED_PATTERN_NOTE,
    ComponentReliabilityPatternMiner,
    DependencyPatternMiner,
    DeploymentPatternMiner,
    FailurePatternMiner,
    PredictivePatternMiner,
    RecoveryPatternMiner,
    RegressionPatternMiner,
    default_miners,
    load_corpus,
)
from app.services.remediation_effectiveness import (
    OBSERVATIONAL_LABEL,
    action_effectiveness,
    compare_actions,
    counterfactual_comparison,
    load_observations,
)


async def _history(db_session, *, count=6, **kwargs):
    project, environment, component = await build_project(db_session)
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=count,
        first_started_at=hours_before(utcnow(), 24 * (count + 2)),
        **kwargs,
    )
    await db_session.commit()
    return project, environment, component


# ---------------------------------------------------------------------------
# The miners (§17–§22)
# ---------------------------------------------------------------------------


def test_every_documented_pattern_type_has_a_miner():
    """§3, §17–§22. A knowledge type with no miner would be documented-only."""
    types = {miner.knowledge_type for miner in default_miners()}
    assert KnowledgeType.FAILURE_PATTERN in types
    assert KnowledgeType.REMEDIATION_PATTERN in types
    assert KnowledgeType.REGRESSION_PATTERN in types
    assert KnowledgeType.DEPLOYMENT_PATTERN in types
    assert KnowledgeType.FAILURE_PATTERN in types
    assert KnowledgeType.COMPONENT_RELIABILITY_PATTERN in types
    assert KnowledgeType.RECOVERY_PATTERN in types
    assert KnowledgeType.PREDICTIVE_PATTERN in types


async def test_failure_patterns_are_labelled_observations_not_causes(db_session):
    """§18. An association is not a mechanism."""
    project, environment, component = await _history(db_session, count=6)
    corpus = await load_corpus(db_session, project_id=project.id, cutoff=utcnow())
    patterns = await FailurePatternMiner().mine(db_session, corpus)
    assert patterns
    for pattern in patterns:
        assert pattern.knowledge_type == KnowledgeType.FAILURE_PATTERN
        assert pattern.experience_ids
        assert OBSERVED_PATTERN_NOTE.lower() in pattern.description.lower() or (
            pattern.limitations
        )
        #: Nothing in a failure pattern claims a cause.
        assert "root_cause" not in pattern.details
        assert "because" not in pattern.description.lower()


async def test_a_failure_pattern_carries_its_component_scope(db_session):
    project, environment, component = await _history(db_session, count=6)
    corpus = await load_corpus(db_session, project_id=project.id, cutoff=utcnow())
    patterns = await FailurePatternMiner().mine(db_session, corpus)
    assert patterns
    assert all(
        pattern.scope
        in (KnowledgeScope.COMPONENT_SPECIFIC, KnowledgeScope.PROJECT_LEVEL)
        for pattern in patterns
    )


async def _patch_history(
    db_session, project, environment, component, *, regressed: int, clean: int
):
    """Phase 7 rows: fixes, their patches, and the verification runs (§19).

    The regression miner reads the *patch* history rather than the experience
    corpus, because a change category is a property of a patch and not of a
    failure signature — so the fixture has to build that history for real.
    """
    from app.models.fix import FixCategory, FixHypothesis, Patch, PatchVerificationRun

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
    for index in range(regressed + clean):
        completed = utcnow() - timedelta(days=10 - index)
        hypothesis = FixHypothesis(
            project_id=project.id,
            incident_id=incident.id,
            title=f"timeout fix {index}",
            description=f"timeout handling regression fixture {index}",
            proposed_change="Restore the timeout budget",
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
                regression_detected=index < regressed,
                verdict_reason="REGRESSION_DETECTED"
                if index < regressed
                else "VERIFIED",
            )
        )
    await db_session.commit()


async def test_a_regression_pattern_comes_from_the_patch_history(db_session):
    """§19. An association over change categories, read from Patch #N outcomes."""
    project, environment, component = await build_project(db_session)
    await _patch_history(
        db_session, project, environment, component, regressed=3, clean=1
    )
    corpus = await load_corpus(db_session, project_id=project.id, cutoff=utcnow())
    patterns = await RegressionPatternMiner().mine(db_session, corpus)
    assert patterns, "three regressed verifications should produce a regression pattern"
    for pattern in patterns:
        assert pattern.sample_count == 4
        assert pattern.success_count == 1
        assert pattern.sources
        #: §19. A property of this project's history, never a claim about the category.
        assert "not a rule about" in pattern.description
        assert OBSERVED_PATTERN_NOTE in pattern.limitations
        assert pattern.details["regressions"] == 3


async def test_a_category_with_no_regressions_is_not_a_regression_pattern(db_session):
    project, environment, component = await build_project(db_session)
    await _patch_history(
        db_session, project, environment, component, regressed=0, clean=4
    )
    corpus = await load_corpus(db_session, project_id=project.id, cutoff=utcnow())
    assert await RegressionPatternMiner().mine(db_session, corpus) == []


async def test_the_regression_miner_declines_without_any_patch_history(db_session):
    project, _, _ = await _history(db_session, count=4)
    corpus = await load_corpus(db_session, project_id=project.id, cutoff=utcnow())
    assert await RegressionPatternMiner().mine(db_session, corpus) == []


async def test_deployment_patterns_are_associations_not_predictions(db_session):
    """§20. Change size + incident frequency is a pattern, not a forecast."""
    project, environment, component = await _history(db_session, count=5)
    corpus = await load_corpus(db_session, project_id=project.id, cutoff=utcnow())
    patterns = await DeploymentPatternMiner().mine(db_session, corpus)
    for pattern in patterns:
        assert "will" not in pattern.description.lower()
        assert pattern.limitations or pattern.details is not None


async def test_dependency_miners_refuse_to_claim_causation(db_session):
    project, environment, component = await _history(db_session, count=5)
    corpus = await load_corpus(db_session, project_id=project.id, cutoff=utcnow())
    patterns = await DependencyPatternMiner().mine(db_session, corpus)
    for pattern in patterns:
        assert "caused" not in pattern.description.lower()


async def test_recovery_and_predictive_miners_only_speak_from_history(db_session):
    project, environment, component = await _history(db_session, count=6)
    corpus = await load_corpus(db_session, project_id=project.id, cutoff=utcnow())
    for miner in (RecoveryPatternMiner(), PredictivePatternMiner()):
        patterns = await miner.mine(db_session, corpus)
        for pattern in patterns:
            assert pattern.sample_count >= 1
            assert pattern.experience_ids
            assert pattern.sources


async def test_the_component_miner_reports_a_signal_not_a_verdict(db_session):
    """§22. Chronic means "look here", never "act on this"."""
    project, environment, component = await _history(db_session, count=9)
    #: The miner reads the stored profiles the §21 sweep produces, so the
    #: fixture has to compute them rather than hand-write a signal.
    await recompute_profiles(db_session, project_id=project.id)
    await db_session.commit()
    corpus = await load_corpus(db_session, project_id=project.id, cutoff=utcnow())
    patterns = await ComponentReliabilityPatternMiner().mine(db_session, corpus)
    for pattern in patterns:
        assert pattern.component_id == component.id
        lowered = pattern.description.lower()
        assert "disable" not in lowered
        assert "should be removed" not in lowered


async def test_a_two_component_corpus_keeps_patterns_separate(db_session):
    project, environment, checkout = await build_project(db_session)
    other_environment, inventory = await build_scope(
        db_session, project.id, component="inventory-service"
    )
    await record_series(
        db_session,
        project,
        environment,
        checkout,
        count=6,
        first_started_at=hours_before(utcnow(), 24 * 8),
    )
    await record_series(
        db_session,
        project,
        other_environment,
        inventory,
        count=6,
        first_started_at=hours_before(utcnow(), 24 * 8),
    )
    await db_session.commit()
    await recompute_profiles(db_session, project_id=project.id)
    await db_session.commit()
    corpus = await load_corpus(db_session, project_id=project.id, cutoff=utcnow())
    patterns = await ComponentReliabilityPatternMiner().mine(db_session, corpus)
    scoped = {pattern.component_id for pattern in patterns if pattern.component_id}
    assert {checkout.id, inventory.id} <= scoped


# ---------------------------------------------------------------------------
# Effectiveness (§15, §16)
# ---------------------------------------------------------------------------


async def test_effectiveness_reports_counts_and_its_sample_size(db_session):
    (
        project,
        _,
        _,
    ) = await _history(db_session, count=6)
    buckets = await action_effectiveness(
        db_session, project_id=project.id, breakdown=["action"]
    )
    assert buckets
    for bucket in buckets:
        assert bucket.comparable >= bucket.successful
        assert bucket.comparable >= 0
        assert bucket.minimum_samples >= 1
        assert bucket.experience_ids
        headline = bucket.headline()
        assert (
            "of" in headline
            or "No comparable" in headline
            or "insufficient" in headline
        )
        #: §15. A bare percentage is never the whole statement.
        assert f"{bucket.comparable}" in headline or "No comparable" in headline


async def test_a_thin_sample_refuses_to_describe_a_rate(db_session):
    (
        project,
        _,
        _,
    ) = await _history(db_session, count=1)
    buckets = await action_effectiveness(
        db_session, project_id=project.id, breakdown=["action"]
    )
    for bucket in buckets:
        if bucket.comparable < bucket.minimum_samples:
            assert bucket.insufficient is True
            assert bucket.limitations
            assert "insufficient" in bucket.headline()


async def test_effectiveness_is_broken_down_by_component_and_not_only_globally(
    db_session,
):
    """§16. One global rate hides the component where it fails."""
    project, environment, checkout = await build_project(db_session)
    inventory_environment, inventory = await build_scope(
        db_session, project.id, component="inventory-service"
    )
    await record_series(
        db_session,
        project,
        environment,
        checkout,
        count=5,
        first_started_at=hours_before(utcnow(), 24 * 7),
        outcome="EFFECTIVE",
    )
    for index in range(5):
        started = hours_before(utcnow(), 24 * (6 - index))
        await record_episode(
            db_session,
            project,
            inventory_environment,
            inventory,
            started_at=started,
            resolved_at=started + timedelta(minutes=40),
            action_type="RESTART_SERVICE",
            outcome="INEFFECTIVE",
            verification_verdict="FAILED",
        )
    await db_session.commit()

    by_component = await action_effectiveness(
        db_session, project_id=project.id, breakdown=["action", "component"]
    )
    values = {bucket.dimension_value for bucket in by_component}
    assert str(checkout.id) in values
    assert str(inventory.id) in values

    checkout_bucket = next(
        bucket for bucket in by_component if bucket.dimension_value == str(checkout.id)
    )
    inventory_bucket = next(
        bucket for bucket in by_component if bucket.dimension_value == str(inventory.id)
    )
    #: The same action, two different histories — which is the whole point of §16.
    assert checkout_bucket.success_ratio > inventory_bucket.success_ratio


async def test_effectiveness_never_returns_a_percentage_without_the_counts(db_session):
    project, _, _ = await _history(db_session, count=4)
    bucket = (
        await action_effectiveness(
            db_session, project_id=project.id, breakdown=["action"]
        )
    )[0]
    payload = bucket.as_dict()
    assert "success_ratio" in payload
    assert payload["comparable"] >= 0
    assert (
        payload["successful"]
        + payload["partially_successful"]
        + payload["failed"]
        + payload["rolled_back"]
        + payload["unresolved"]
        == payload["comparable"]
    )


async def test_a_cutoff_keeps_later_outcomes_out_of_the_counts(db_session):
    project, environment, component = await build_project(db_session)
    await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=utcnow() - timedelta(days=40),
        resolved_at=utcnow() - timedelta(days=40) + timedelta(minutes=30),
    )
    await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=utcnow() - timedelta(hours=3),
        resolved_at=utcnow() - timedelta(hours=2),
    )
    await db_session.commit()
    cutoff = utcnow() - timedelta(days=10)
    buckets = await action_effectiveness(
        db_session, project_id=project.id, breakdown=["action"], cutoff=cutoff
    )
    assert sum(bucket.comparable for bucket in buckets) == 1


# ---------------------------------------------------------------------------
# Comparison and counterfactuals (§44, §45)
# ---------------------------------------------------------------------------


async def test_comparing_actions_labels_the_result_observational(db_session):
    project, environment, component = await build_project(db_session)
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=6,
        first_started_at=hours_before(utcnow(), 24 * 8),
        outcome="EFFECTIVE",
    )
    for index in range(6):
        started = hours_before(utcnow(), 24 * (6 - index))
        await record_episode(
            db_session,
            project,
            environment,
            component,
            started_at=started,
            resolved_at=started + timedelta(minutes=50),
            action_type="ROLLBACK_DEPLOYMENT",
            outcome="INEFFECTIVE",
            verification_verdict="FAILED",
        )
    await db_session.commit()

    result = await compare_actions(
        db_session,
        project_id=project.id,
        action_a="RESTART_SERVICE",
        action_b="ROLLBACK_DEPLOYMENT",
    )
    assert result["label"] == OBSERVATIONAL_LABEL
    assert result["verdict"].startswith("FAVOURS_")
    assert "observational difference" in result["summary"]
    assert "causal" not in result["summary"].replace(
        "not a demonstrated causal effect", ""
    )
    assert result["limitations"]
    for action in ("RESTART_SERVICE", "ROLLBACK_DEPLOYMENT"):
        assert result["actions"][action]["comparable"] >= 1
        assert result["actions"][action]["headline"]


async def test_a_thin_comparison_returns_insufficient_evidence(db_session):
    project, _, _ = await _history(db_session, count=1)
    result = await compare_actions(
        db_session,
        project_id=project.id,
        action_a="RESTART_SERVICE",
        action_b="SCALE_SERVICE_WITHIN_LIMIT",
    )
    assert result["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert "no comparison is defensible" in result["summary"]


async def test_a_counterfactual_is_labelled_an_observational_comparison(db_session):
    """§44. "What happened when it wasn't done" is not a causal estimate."""
    project, environment, component = await build_project(db_session)
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=4,
        first_started_at=hours_before(utcnow(), 24 * 6),
        outcome="EFFECTIVE",
    )
    for index in range(4):
        started = hours_before(utcnow(), 24 * (5 - index))
        await record_episode(
            db_session,
            project,
            environment,
            component,
            started_at=started,
            resolved_at=started + timedelta(minutes=25),
            remediate=False,
        )
    await db_session.commit()

    #: The label is the composite the builder produced, not the raw fingerprint —
    #: a comparison has to be over the same *kind* of failure, so the fixture
    #: uses the real one rather than a hand-written approximation.
    observations = await load_observations(db_session, project_id=project.id)
    labels = {item.failure_label for item in observations}
    assert len(labels) == 1, "the fixture should produce one comparable failure shape"
    label = labels.pop()
    result = await counterfactual_comparison(
        db_session,
        project_id=project.id,
        failure_label=label,
        action_type="RESTART_SERVICE",
    )
    assert result["label"] == OBSERVATIONAL_LABEL
    #: §44. The result says, in its own words, that it does not establish a cause.
    assert any(
        "cannot establish a causal effect" in note for note in result["limitations"]
    )
    assert result["limitations"]
    assert result["taken"]["comparable"] >= 1
    assert result["not_taken"]["comparable"] >= 1
    assert result["sufficient_evidence"] is True
    assert result["summary"]
    #: The two groups are named as they are, not dressed up as arms of a trial.
    assert set(result) >= {"taken", "not_taken", "sufficient_evidence", "limitations"}


# ---------------------------------------------------------------------------
# Component profiles and chronic signals (§21, §22)
# ---------------------------------------------------------------------------


async def test_profiles_are_computed_over_multiple_windows(db_session):
    project, _, component = await _history(db_session, count=8)
    profiles = await recompute_profiles(db_session, project_id=project.id)
    await db_session.commit()
    windows = {profile.window_days for profile in profiles}
    assert len(windows) >= 2, "a single window cannot show a trend (§21)"
    for profile in profiles:
        assert profile.incident_count >= 1
        assert profile.anomaly_count >= 1
        assert profile.breakdown is not None


async def test_a_component_with_repeated_trouble_raises_a_signal(db_session):
    """§22. The signal triggers investigation; it never disables the component."""
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
            rollback=index % 3 == 0,
            outcome="HARMFUL" if index % 3 == 0 else "EFFECTIVE",
        )
    await db_session.commit()

    profile = await compute_profile(
        db_session, project_id=project.id, component_id=component.id, window_days=90
    )
    await db_session.commit()
    assert profile.chronic_signal is True
    assert profile.chronic_reasons
    assert any(
        "incident" in reason or "rollback" in reason or "recovery" in reason
        for reason in profile.chronic_reasons
    )


async def test_a_healthy_component_does_not_raise_a_signal(db_session):
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
    profile = await compute_profile(
        db_session, project_id=project.id, component_id=component.id, window_days=30
    )
    assert profile.chronic_signal is False
    assert profile.chronic_reasons == []


async def test_a_profile_reports_recovery_time_over_its_window(db_session):
    project, environment, component = await build_project(db_session)
    for index in range(4):
        started = hours_before(utcnow(), 24 * (6 - index))
        await record_episode(
            db_session,
            project,
            environment,
            component,
            started_at=started,
            resolved_at=started + timedelta(minutes=30 * (index + 1)),
        )
    await db_session.commit()
    profile = await compute_profile(
        db_session, project_id=project.id, component_id=component.id, window_days=90
    )
    assert profile.mean_recovery_seconds is not None
    assert profile.mean_recovery_seconds > 0


async def test_a_quiet_component_is_profiled_as_quiet_not_as_healthy(db_session):
    """§21. An empty profile means "no evidence"; it is never a clean bill of health."""
    project, environment, component = await build_project(db_session)
    empty_environment, empty = await build_scope(
        db_session, project.id, component="never-fails"
    )
    started = utcnow() - timedelta(days=1)
    await record_episode(
        db_session,
        project,
        environment,
        component,
        started_at=started,
        resolved_at=started + timedelta(minutes=15),
    )
    await db_session.commit()
    profiles = await recompute_profiles(db_session, project_id=project.id)
    by_component = {profile.component_id: profile for profile in profiles}
    assert component.id in by_component
    assert by_component[component.id].incident_count == 1
    if empty.id in by_component:
        quiet = by_component[empty.id]
        assert quiet.incident_count == 0
        assert quiet.chronic_signal is False
        assert quiet.mean_recovery_seconds is None
        assert quiet.forecast_outcome_count == 0
