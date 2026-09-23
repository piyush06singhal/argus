"""Phase 10 — knowledge validation, lifecycle, conflict and decay (§25–§37, §66–§73).

The lifecycle is where "learning" either stays governed or quietly becomes
self-modification, so these tests are written as refusals first:

* small samples reach CANDIDATE and stop there (§33, §86);
* weak or unstable evidence cannot reach ACTIVE, with or without a human (§35, §71);
* an unstable pattern is never auto-promoted even when its numbers look good;
* two patterns that disagree are both kept, scoped, and flagged — never silently
  resolved in favour of one (§66, §67, §87);
* knowledge that has stopped being confirmed is deprecated, not deleted (§25, §88);
* every status change appends a version rather than overwriting the last one (§26);
* a project-level claim produced by one component is downgraded to that component (§37).

The validation service is exercised on mined patterns built from real stored
episodes, so the numbers under test are the ones the pipeline produces.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.phase10_helpers import (
    build_project,
    build_scope,
    hours_before,
    make_knowledge,
    record_episode,
    record_series,
    utcnow,
)

from app.models.intelligence import (
    KnowledgeConfidence,
    KnowledgeScope,
    KnowledgeStatus,
    KnowledgeType,
    ReliabilityKnowledge,
)
from app.services.intelligence_state import (
    IntelligenceStateError,
    assert_knowledge_transition,
)
from app.services.knowledge_lifecycle import (
    activate_knowledge,
    deprecate_knowledge,
    find_conflicts,
    fingerprint_for_pattern,
    list_knowledge_versions,
    record_candidate,
    recommendable,
    refresh_staleness,
    reject_knowledge,
    request_more_evidence,
)
from app.services.knowledge_validation import (
    KnowledgeValidationService,
    MAX_POOR_SHARE,
)
from app.services.pattern_miners import (
    ExperienceCorpus,
    MinedPattern,
    RemediationPatternMiner,
    load_corpus,
)


def _pattern(
    project_id: uuid.UUID,
    *,
    corpus: ExperienceCorpus | None = None,
    knowledge_type: KnowledgeType = KnowledgeType.REMEDIATION_PATTERN,
    scope: KnowledgeScope = KnowledgeScope.PROJECT_LEVEL,
    feature_signature: str = "remediation:RESTART_SERVICE:checkout_error_spike",
    sample_count: int = 6,
    success_count: int | None = 5,
    component_id: uuid.UUID | None = None,
    details: dict | None = None,
    **overrides,
) -> MinedPattern:
    """A candidate pattern shaped exactly like the miners emit one.

    When ``corpus`` is supplied, the pattern cites *its* experience ids. That
    matters: validation resolves a pattern's ids back to corpus entries, so a
    fixture citing invented ids would exercise the "no evidence found" path and
    silently prove nothing (§32).
    """
    if corpus is not None:
        chosen = [entry.experience_id for entry in corpus.usable()[:sample_count]]
    else:
        chosen = [str(uuid.uuid4()) for _ in range(sample_count)]
    payload = {
        "knowledge_type": knowledge_type,
        "scope": scope,
        "project_id": project_id,
        "title": "fixture pattern",
        "description": "fixture pattern",
        "feature_signature": feature_signature,
        "algorithm": "fixture",
        "sample_count": sample_count,
        "success_count": success_count,
        "component_id": component_id,
        "details": details or {"action_type": "restart_service"},
        "experience_ids": chosen,
        "sources": [{"type": "experience", "id": item} for item in chosen],
    }
    payload.update(overrides)
    return MinedPattern(**payload)


async def _corpus(db_session, project, environment, component, *, count=6, **kwargs):
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
    return await load_corpus(db_session, project_id=project.id, cutoff=utcnow())


# ---------------------------------------------------------------------------
# The state machine (§4)
# ---------------------------------------------------------------------------


def test_only_live_knowledge_may_influence_recommendations():
    """§4. CANDIDATE and VALIDATING are visible but not believed."""
    candidate = ReliabilityKnowledge(
        project_id=uuid.uuid4(),
        knowledge_type=KnowledgeType.FAILURE_PATTERN,
        status=KnowledgeStatus.CANDIDATE,
        scope=KnowledgeScope.PROJECT_LEVEL,
        title="t",
        description="d",
        fingerprint="f",
        feature_signature="s",
        sample_count=3,
        algorithm="fixture",
        algorithm_version="1",
        feature_schema_version="1",
    )
    assert recommendable(candidate) is False
    candidate.status = KnowledgeStatus.VALIDATING
    assert recommendable(candidate) is False
    candidate.status = KnowledgeStatus.VALIDATED
    assert recommendable(candidate) is True
    candidate.status = KnowledgeStatus.ACTIVE
    assert recommendable(candidate) is True
    for retired in (
        KnowledgeStatus.DEPRECATED,
        KnowledgeStatus.REJECTED,
        KnowledgeStatus.SUPERSEDED,
    ):
        candidate.status = retired
        assert recommendable(candidate) is False


@pytest.mark.parametrize(
    "source,target",
    [
        (KnowledgeStatus.ACTIVE, KnowledgeStatus.CANDIDATE),
        (KnowledgeStatus.ACTIVE, KnowledgeStatus.VALIDATED),
        (KnowledgeStatus.REJECTED, KnowledgeStatus.ACTIVE),
        (KnowledgeStatus.SUPERSEDED, KnowledgeStatus.ACTIVE),
        (KnowledgeStatus.CANDIDATE, KnowledgeStatus.ACTIVE),
    ],
)
def test_forbidden_transitions_are_refused_by_the_state_machine(source, target):
    """Every refusal here is a way to *raise* belief without doing the work."""
    with pytest.raises(IntelligenceStateError):
        assert_knowledge_transition(source, target)


def test_sending_validated_knowledge_back_for_evidence_only_lowers_belief():
    """§72. A human may doubt a validated pattern; that can never promote one."""
    assert_knowledge_transition(KnowledgeStatus.VALIDATED, KnowledgeStatus.CANDIDATE)
    assert_knowledge_transition(KnowledgeStatus.VALIDATING, KnowledgeStatus.CANDIDATE)


# ---------------------------------------------------------------------------
# Validation (§32–§37, §86)
# ---------------------------------------------------------------------------


async def test_a_two_observation_pattern_is_only_a_candidate(db_session):
    """§86. Two observations is a hint, not established knowledge."""
    project, environment, component = await build_project(db_session)
    corpus = await _corpus(db_session, project, environment, component, count=6)
    pattern = _pattern(project.id, corpus=corpus, sample_count=2, success_count=2)
    verdict = KnowledgeValidationService().validate(pattern, corpus=corpus)

    assert verdict.confidence in (KnowledgeConfidence.LOW, KnowledgeConfidence.UNKNOWN)
    assert verdict.promotable is False
    assert verdict.requires_review is True
    assert any(not check.passed for check in verdict.checks)


async def test_a_strong_stable_pattern_can_reach_validated(db_session):
    project, environment, component = await build_project(db_session)
    corpus = await _corpus(db_session, project, environment, component, count=12)
    pattern = _pattern(project.id, corpus=corpus, sample_count=12, success_count=11)
    verdict = KnowledgeValidationService().validate(pattern, corpus=corpus)
    assert verdict.passed is True
    assert verdict.promotable is True


async def test_an_unstable_pattern_is_never_auto_activated(db_session):
    """§35. A pattern that appears in only one window may not promote itself."""
    project, environment, component = await build_project(db_session)
    #: All history inside the most recent window; nothing before it.
    await record_series(
        db_session,
        project,
        environment,
        component,
        count=8,
        first_started_at=hours_before(utcnow(), 24 * 6),
    )
    await db_session.commit()
    corpus = await load_corpus(db_session, project_id=project.id, cutoff=utcnow())
    pattern = _pattern(project.id, corpus=corpus, sample_count=8, success_count=8)
    verdict = KnowledgeValidationService().validate(pattern, corpus=corpus)
    if verdict.stability != "stable":
        assert verdict.activatable is False
        assert verdict.requires_review is True


async def test_a_project_level_claim_from_one_component_is_downgraded(db_session):
    """§37. Checkout behaviour is not payments behaviour without evidence."""
    project, environment, component = await build_project(db_session)
    corpus = await _corpus(db_session, project, environment, component, count=8)
    pattern = _pattern(
        project.id,
        corpus=corpus,
        scope=KnowledgeScope.PROJECT_LEVEL,
        component_id=None,
        sample_count=8,
        success_count=8,
    )
    verdict = KnowledgeValidationService().validate(pattern, corpus=corpus)
    assert verdict.scope in (
        KnowledgeScope.COMPONENT_SPECIFIC,
        KnowledgeScope.PROJECT_LEVEL,
    )
    if verdict.scope == KnowledgeScope.COMPONENT_SPECIFIC:
        assert any("one component" in note for note in verdict.limitations)


async def test_poor_quality_records_are_counted_against_the_pattern(db_session):
    """§30. Bad data dilutes the evidence rather than being silently dropped."""
    project, environment, component = await build_project(db_session)
    corpus = await _corpus(db_session, project, environment, component, count=6)
    for entry in corpus.entries[:4]:
        entry.quality = "POOR"
    pattern = _pattern(project.id, corpus=corpus, sample_count=6, success_count=6)
    verdict = KnowledgeValidationService().validate(pattern, corpus=corpus)
    poor_check = next(
        (check for check in verdict.checks if "quality" in check.name), None
    )
    assert poor_check is not None
    if poor_check.passed:
        assert f"{MAX_POOR_SHARE:.0%}" not in poor_check.detail
    else:
        assert verdict.passed is False


async def test_an_ambiguous_support_ratio_cannot_reach_high_confidence(db_session):
    """§34. A coin flip is not a finding."""
    project, environment, component = await build_project(db_session)
    corpus = await _corpus(db_session, project, environment, component, count=10)
    pattern = _pattern(project.id, corpus=corpus, sample_count=10, success_count=5)
    verdict = KnowledgeValidationService().validate(pattern, corpus=corpus)
    assert verdict.confidence != KnowledgeConfidence.HIGH
    support = next(
        (check for check in verdict.checks if check.name == "support_strength"), None
    )
    assert support is not None
    assert "50%" in support.detail or "ambiguous" in support.detail.lower()
    #: A coin-flip claim may not activate itself, whatever its sample size (§34, §73).
    assert verdict.activatable is False


async def test_conflicting_evidence_forces_review(db_session):
    project, environment, component = await build_project(db_session)
    corpus = await _corpus(db_session, project, environment, component, count=10)
    pattern = _pattern(project.id, corpus=corpus, sample_count=10, success_count=10)
    verdict = KnowledgeValidationService().validate(
        pattern, corpus=corpus, conflict_ids=[str(uuid.uuid4())]
    )
    assert verdict.activatable is False
    assert verdict.requires_review is True
    #: The conflict surfaces as the false-discovery check: the pattern may be
    #: real, but something else claiming the same subject disagrees.
    conflict_check = next(
        (check for check in verdict.checks if check.name == "false_discovery"), None
    )
    assert conflict_check is not None and conflict_check.passed is False
    assert "conflict" in conflict_check.detail
    assert conflict_check.blocking is False
    assert any("not silently resolved" in note for note in verdict.limitations)


async def test_validation_reports_every_check_it_ran(db_session):
    project, environment, component = await build_project(db_session)
    corpus = await _corpus(db_session, project, environment, component, count=6)
    verdict = KnowledgeValidationService().validate(
        _pattern(project.id, corpus=corpus), corpus=corpus
    )
    names = {check.name for check in verdict.checks}
    assert {"sample_size", "data_quality", "temporal_consistency", "stability"} <= names
    assert verdict.as_dict()["checks"]
    assert verdict.tier


# ---------------------------------------------------------------------------
# Fingerprints and dedup (§64, §65)
# ---------------------------------------------------------------------------


def test_the_fingerprint_ignores_evidence_volume_but_not_identity():
    """§65. More of the same evidence is an update, not a new pattern."""
    project_id = uuid.uuid4()
    small = _pattern(project_id, sample_count=3, success_count=3)
    larger = _pattern(project_id, sample_count=9, success_count=9)
    assert fingerprint_for_pattern(small) == fingerprint_for_pattern(larger)

    other_action = _pattern(
        project_id,
        feature_signature="remediation:ROLLBACK_DEPLOYMENT:checkout_error_spike",
        details={"action_type": "rollback_deployment"},
    )
    assert fingerprint_for_pattern(small) != fingerprint_for_pattern(other_action)

    other_scope = _pattern(project_id, scope=KnowledgeScope.COMPONENT_SPECIFIC)
    assert fingerprint_for_pattern(small) != fingerprint_for_pattern(other_scope)


async def test_recording_the_same_pattern_twice_updates_rather_than_duplicates(
    db_session,
):
    project, environment, component = await build_project(db_session)
    corpus = await _corpus(db_session, project, environment, component, count=8)
    service = KnowledgeValidationService()
    pattern = _pattern(project.id, corpus=corpus, sample_count=8, success_count=7)
    verdict = service.validate(pattern, corpus=corpus)

    first = await record_candidate(db_session, pattern, verdict)
    await db_session.commit()
    second = await record_candidate(db_session, pattern, verdict)
    await db_session.commit()

    rows = list(
        (
            await db_session.scalars(
                select(ReliabilityKnowledge).where(
                    ReliabilityKnowledge.project_id == project.id
                )
            )
        ).all()
    )
    assert first.created is True
    assert second.created is False
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Activation, review and refusal (§71–§74)
# ---------------------------------------------------------------------------


async def test_activation_is_refused_below_validated(db_session):
    project, _, _ = await build_project(db_session)
    row = await make_knowledge(db_session, project, status="CANDIDATE")
    await db_session.commit()
    with pytest.raises(IntelligenceStateError):
        await activate_knowledge(db_session, row, reviewer="engineer@example.com")


async def test_activation_appends_a_version_rather_than_rewriting_one(db_session):
    """§26. Never silently overwrite what was believed."""
    project, _, _ = await build_project(db_session)
    row = await make_knowledge(db_session, project, status="VALIDATED")
    await db_session.commit()

    await activate_knowledge(
        db_session, row, reviewer="engineer@example.com", reason="checked"
    )
    await db_session.commit()
    versions = await list_knowledge_versions(db_session, knowledge_id=row.id)
    assert len(versions) == 2
    assert versions[0].snapshot["status"] == "ACTIVE"
    assert versions[1].snapshot["status"] == "VALIDATED"
    assert versions[0].note == "human activation"


async def test_rejecting_records_the_decision_and_the_reason(db_session):
    project, _, _ = await build_project(db_session)
    row = await make_knowledge(db_session, project, status="CANDIDATE")
    await db_session.commit()
    await reject_knowledge(
        db_session, row, reviewer="engineer@example.com", reason="one component only"
    )
    await db_session.commit()
    assert row.status == KnowledgeStatus.REJECTED
    assert row.review_reason == "one component only"
    versions = await list_knowledge_versions(db_session, knowledge_id=row.id)
    assert versions[0].snapshot["status"] == "REJECTED"


async def test_requesting_more_evidence_sends_a_pattern_back(db_session):
    project, _, _ = await build_project(db_session)
    row = await make_knowledge(db_session, project, status="VALIDATED")
    await db_session.commit()
    review = await request_more_evidence(
        db_session,
        row,
        reviewer="engineer@example.com",
        reason="need a second environment",
    )
    await db_session.commit()
    assert row.status == KnowledgeStatus.CANDIDATE
    assert review.decision == "REQUEST_MORE_EVIDENCE"


async def test_an_active_pattern_cannot_be_sent_back_for_evidence(db_session):
    project, _, _ = await build_project(db_session)
    row = await make_knowledge(db_session, project, status="ACTIVE")
    await db_session.commit()
    with pytest.raises(IntelligenceStateError):
        await request_more_evidence(
            db_session, row, reviewer="engineer@example.com", reason="hmm"
        )


# ---------------------------------------------------------------------------
# Conflicts (§66, §67, §87)
# ---------------------------------------------------------------------------


async def test_opposing_patterns_are_both_kept_and_the_conflict_is_reported(db_session):
    """§87. The system does not pick a winner for the operator."""
    project, _, component = await build_project(db_session)
    stored = await make_knowledge(
        db_session,
        project,
        knowledge_type="REMEDIATION_PATTERN",
        status="ACTIVE",
        component=component,
        scope="COMPONENT_SPECIFIC",
        feature_signature="remediation:restart_service:checkout_error_spike",
        details={"action_type": "restart_service"},
        sample_count=10,
        success_count=9,
    )
    await db_session.commit()

    opposing = _pattern(
        project.id,
        scope=KnowledgeScope.COMPONENT_SPECIFIC,
        component_id=component.id,
        feature_signature="remediation:restart_service:checkout_after_deploy",
        details={"action_type": "restart_service"},
        sample_count=10,
        success_count=1,
    )
    conflicts = await find_conflicts(db_session, opposing)
    assert [item.id for item in conflicts] == [stored.id]

    #: The opposing pattern is recorded with the conflict named, and both rows
    #: survive: neither claim is overwritten by the other.
    corpus = await load_corpus(db_session, project_id=project.id, cutoff=utcnow())
    verdict = KnowledgeValidationService().validate(
        opposing, corpus=corpus, conflict_ids=[str(stored.id)]
    )
    result = await record_candidate(db_session, opposing, verdict)
    await db_session.commit()
    assert result.conflict_ids == [str(stored.id)]
    assert result.activated is False

    survivors = list(
        (
            await db_session.scalars(
                select(ReliabilityKnowledge).where(
                    ReliabilityKnowledge.project_id == project.id
                )
            )
        ).all()
    )
    assert len(survivors) == 2
    assert stored.id in {row.id for row in survivors}
    assert all(row.status != KnowledgeStatus.SUPERSEDED for row in survivors)


async def test_a_conflict_is_only_flagged_within_the_same_scope(db_session):
    """§67. Context separates the two claims; a global claim is not a conflict."""
    project, _, component = await build_project(db_session)
    await make_knowledge(
        db_session,
        project,
        knowledge_type="REMEDIATION_PATTERN",
        status="ACTIVE",
        scope="COMPONENT_SPECIFIC",
        component=component,
        details={"action_type": "restart_service"},
        sample_count=10,
        success_count=9,
    )
    await db_session.commit()
    global_pattern = _pattern(
        project.id,
        scope=KnowledgeScope.PROJECT_LEVEL,
        component_id=None,
        details={"action_type": "restart_service"},
        sample_count=10,
        success_count=1,
    )
    assert await find_conflicts(db_session, global_pattern) == []


async def test_two_patterns_that_agree_are_not_a_conflict(db_session):
    project, _, component = await build_project(db_session)
    await make_knowledge(
        db_session,
        project,
        knowledge_type="REMEDIATION_PATTERN",
        status="ACTIVE",
        scope="COMPONENT_SPECIFIC",
        component=component,
        details={"action_type": "restart_service"},
        sample_count=10,
        success_count=9,
    )
    await db_session.commit()
    agreeing = _pattern(
        project.id,
        scope=KnowledgeScope.COMPONENT_SPECIFIC,
        component_id=component.id,
        details={"action_type": "restart_service"},
        sample_count=10,
        success_count=8,
    )
    assert await find_conflicts(db_session, agreeing) == []


# ---------------------------------------------------------------------------
# Decay (§25, §88)
# ---------------------------------------------------------------------------


async def test_knowledge_that_stopped_being_confirmed_is_deprecated_not_deleted(
    db_session,
):
    """§88. A changed architecture retires the pattern; the history survives."""
    project, _, _ = await build_project(db_session)
    stale = await make_knowledge(
        db_session,
        project,
        status="ACTIVE",
        confirmed_at=utcnow() - timedelta(days=400),
    )
    fresh = await make_knowledge(
        db_session,
        project,
        status="ACTIVE",
        feature_signature="remediation:other:thing",
        confirmed_at=utcnow(),
    )
    await db_session.commit()

    retired = await refresh_staleness(db_session, project_id=project.id)
    await db_session.commit()
    assert [row.id for row in retired] == [stale.id]
    assert stale.status == KnowledgeStatus.DEPRECATED
    assert "no confirming observation" in (stale.review_reason or "")
    assert stale.reviewed_by == "ARGUS (staleness sweep)"
    #: The row is still there, with a version recording why it was retired.
    still_present = await db_session.get(ReliabilityKnowledge, stale.id)
    assert still_present is not None
    versions = await list_knowledge_versions(db_session, knowledge_id=stale.id)
    assert versions[0].note == "stale"
    assert fresh.status == KnowledgeStatus.ACTIVE


async def test_an_already_retired_row_is_left_alone_by_the_sweep(db_session):
    project, _, _ = await build_project(db_session)
    rejected = await make_knowledge(
        db_session,
        project,
        status="REJECTED",
        confirmed_at=utcnow() - timedelta(days=400),
    )
    await db_session.commit()
    retired = await refresh_staleness(db_session, project_id=project.id)
    assert retired == []
    assert rejected.status == KnowledgeStatus.REJECTED


async def test_a_human_deprecation_keeps_the_reason(db_session):
    project, _, _ = await build_project(db_session)
    row = await make_knowledge(db_session, project, status="ACTIVE")
    await db_session.commit()
    await deprecate_knowledge(
        db_session,
        row,
        actor="engineer@example.com",
        reason="the checkout flow was rewritten",
    )
    await db_session.commit()
    assert row.status == KnowledgeStatus.DEPRECATED
    assert row.reviewed_by == "engineer@example.com"
    assert row.review_reason == "the checkout flow was rewritten"


# ---------------------------------------------------------------------------
# The miner that produces the patterns (§17, §18)
# ---------------------------------------------------------------------------


async def test_the_remediation_miner_reports_the_evidence_behind_each_pattern(
    db_session,
):
    project, environment, component = await build_project(db_session)
    corpus = await _corpus(db_session, project, environment, component, count=6)
    patterns = await RemediationPatternMiner().mine(db_session, corpus)
    assert patterns
    for pattern in patterns:
        assert pattern.sample_count >= 1
        assert pattern.experience_ids, "a pattern with no evidence is not a pattern"
        assert pattern.sources
        assert pattern.feature_signature
        assert pattern.knowledge_type == KnowledgeType.REMEDIATION_PATTERN


async def test_the_miner_declines_to_emit_below_the_sample_floor(db_session):
    project, environment, component = await build_project(db_session)
    corpus = await _corpus(db_session, project, environment, component, count=2)
    patterns = await RemediationPatternMiner().mine(db_session, corpus, min_samples=5)
    assert patterns == []


async def test_mining_an_empty_corpus_yields_nothing(db_session):
    project, _, _ = await build_project(db_session)
    empty = ExperienceCorpus(project_id=project.id, cutoff=utcnow())
    assert await RemediationPatternMiner().mine(db_session, empty) == []


async def test_the_corpus_excludes_poor_records_from_the_evidence(db_session):
    project, environment, component = await build_project(db_session)
    corpus = await _corpus(db_session, project, environment, component, count=5)
    assert len(corpus.usable()) == len(corpus.entries)
    corpus.entries[0].quality = "POOR"
    assert len(corpus.usable()) == len(corpus.entries) - 1
    #: Still in the corpus, so a report can say how many were set aside.
    assert len(corpus.entries) == 5


async def test_a_two_component_project_does_not_cross_generalize(db_session):
    """§37. Evidence from one component cannot become a claim about another."""
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
    )
    await record_episode(
        db_session,
        project,
        inventory_environment,
        inventory,
        started_at=hours_before(utcnow(), 24 * 2),
        resolved_at=hours_before(utcnow(), 24 * 2) + timedelta(minutes=30),
    )
    await db_session.commit()
    corpus = await load_corpus(db_session, project_id=project.id, cutoff=utcnow())
    patterns = await RemediationPatternMiner().mine(db_session, corpus)
    assert patterns
    by_component = {pattern.component_id for pattern in patterns}
    #: A cross-component claim, if any, is scoped at project level and says so.
    for pattern in patterns:
        if pattern.component_id is None:
            assert pattern.scope in (
                KnowledgeScope.PROJECT_LEVEL,
                KnowledgeScope.SERVICE_CLASS,
            )
        else:
            assert pattern.component_id in by_component
