"""ARGUS Knowledge Lifecycle (Phase 10 §4, §25, §26, §65–§67, §71–§74).

Where a validated candidate becomes — or does not become — reliability
knowledge. Everything here is bookkeeping with teeth:

* **One live pattern per fingerprint** (§65). The fingerprint includes type,
  scope, project, component and feature signature, so re-discovering a pattern
  *updates* it instead of accumulating near-duplicates. Updating is not
  overwriting: every material change writes a :class:`KnowledgeVersion` row
  first, which is the ledger §26 asks for.
* **A rejection is remembered.** A pattern a human rejected is not silently
  re-created by the next run just because the data still supports it. It takes
  materially more evidence to reopen it, and the reopening is recorded.
* **Conflicts coexist** (§66, §67). Two patterns that disagree are both stored
  with their scope; the resolver reports the conflict and never picks a winner
  on its own.
* **Decay deprecates, never deletes** (§25). Stale knowledge keeps its row, its
  versions and its reason, because an audit of "what did ARGUS believe in June"
  must still be answerable.
* **No self-approval** (§74). Activation requires either a human review or a
  policy that explicitly allows autonomous activation, and high-impact knowledge
  types can never take the autonomous path.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.intelligence import (
    KnowledgeConfidence,
    KnowledgeReview,
    KnowledgeStatus,
    KnowledgeType,
    KnowledgeVersion,
    ReliabilityKnowledge,
    knowledge_fingerprint,
)
from app.services.intelligence_state import (
    IntelligenceStateError,
    assert_knowledge_transition,
    is_recommendable,
    requires_human_review,
)
from app.services.knowledge_validation import ValidationVerdict
from app.services.pattern_miners import MinedPattern

logger = logging.getLogger(__name__)

#: Support gap that makes two same-subject patterns a real conflict (§66).
#: "usually works" (≥60%) against "often fails" (≤40%) is a disagreement worth
#: showing; 52% against 48% is noise.
CONFLICT_SUPPORT_GAP = 0.2

#: A rejected pattern reopens only when the evidence has grown by this factor.
REJECTION_REOPEN_FACTOR = 2.0


@dataclass
class KnowledgeUpsertResult:
    """What happened to one pattern when it met the lifecycle."""

    knowledge: Optional[ReliabilityKnowledge]
    created: bool = False
    updated: bool = False
    version_created: bool = False
    status_changed: bool = False
    activated: bool = False
    skipped_reason: Optional[str] = None
    conflict_ids: list[str] = field(default_factory=list)

    @property
    def status(self) -> Optional[KnowledgeStatus]:
        return self.knowledge.status if self.knowledge is not None else None


def fingerprint_for_pattern(pattern: MinedPattern) -> str:
    """The §65 identity of a mined pattern."""
    return knowledge_fingerprint(
        knowledge_type=pattern.knowledge_type,
        scope=pattern.scope,
        project_id=pattern.project_id,
        component_id=pattern.component_id,
        feature_signature=pattern.feature_signature,
    )


async def find_knowledge_by_fingerprint(
    session: AsyncSession, *, project_id: uuid.UUID, fingerprint: str
) -> list[ReliabilityKnowledge]:
    """Every row with this identity, most recent first."""
    stmt = (
        select(ReliabilityKnowledge)
        .where(ReliabilityKnowledge.project_id == project_id)
        .where(ReliabilityKnowledge.fingerprint == fingerprint)
        .order_by(ReliabilityKnowledge.created_at.desc())
    )
    return list((await session.scalars(stmt)).all())


async def find_conflicts(
    session: AsyncSession,
    pattern: MinedPattern,
    *,
    exclude_ids: Sequence[uuid.UUID] = (),
) -> list[ReliabilityKnowledge]:
    """Existing live knowledge that disagrees with this pattern (§66).

    "Disagrees" is narrow on purpose: same subject (the same action, the same
    change category, the same risk level), same scope, and an opposite verdict
    on whether the thing works. Anything looser would flag every pair of
    patterns that merely differ.
    """
    subject = _conflict_subject(pattern)
    if subject is None:
        return []

    stmt = (
        select(ReliabilityKnowledge)
        .where(ReliabilityKnowledge.project_id == pattern.project_id)
        .where(ReliabilityKnowledge.knowledge_type == pattern.knowledge_type)
        .where(ReliabilityKnowledge.scope == pattern.scope)
        .where(
            ReliabilityKnowledge.status.in_(
                [KnowledgeStatus.VALIDATED, KnowledgeStatus.ACTIVE]
            )
        )
    )
    if pattern.component_id is None:
        stmt = stmt.where(ReliabilityKnowledge.component_id.is_(None))
    else:
        stmt = stmt.where(ReliabilityKnowledge.component_id == pattern.component_id)
    if exclude_ids:
        stmt = stmt.where(ReliabilityKnowledge.id.notin_(list(exclude_ids)))

    rows = list((await session.scalars(stmt.limit(200))).all())
    conflicts: list[ReliabilityKnowledge] = []
    for row in rows:
        if (
            _conflict_subject_from_details(pattern.knowledge_type, row.validation or {})
            != subject
        ):
            continue
        if not _verdicts_disagree(row.support_strength, pattern.support_strength):
            continue
        conflicts.append(row)
    return conflicts


def _conflict_subject(pattern: MinedPattern) -> Optional[str]:
    details = pattern.details or {}
    if pattern.knowledge_type == KnowledgeType.REMEDIATION_PATTERN:
        return f"action:{details.get('action_type')}"
    if pattern.knowledge_type == KnowledgeType.REGRESSION_PATTERN:
        return f"category:{details.get('category')}"
    if pattern.knowledge_type == KnowledgeType.PREDICTIVE_PATTERN:
        return f"risk:{details.get('risk_level')}"
    if pattern.knowledge_type == KnowledgeType.FAILURE_PATTERN:
        return f"label:{details.get('label')}"
    return None


def _conflict_subject_from_details(
    knowledge_type: KnowledgeType, details: dict[str, Any]
) -> Optional[str]:
    """The subject a *stored* row is about.

    Stored under ``validation.details``, which is exactly where
    :func:`record_candidate` puts the miner's own details — so the comparison
    works on knowledge written by any run, not only by this process.
    """
    inner = details.get("details") if isinstance(details, dict) else None
    payload = inner if isinstance(inner, dict) else (details or {})
    if knowledge_type == KnowledgeType.REMEDIATION_PATTERN:
        return f"action:{payload.get('action_type')}"
    if knowledge_type == KnowledgeType.REGRESSION_PATTERN:
        return f"category:{payload.get('category')}"
    if knowledge_type == KnowledgeType.PREDICTIVE_PATTERN:
        return f"risk:{payload.get('risk_level')}"
    if knowledge_type == KnowledgeType.FAILURE_PATTERN:
        return f"label:{payload.get('label')}"
    return None


def _verdicts_disagree(left: Optional[float], right: Optional[float]) -> bool:
    """Whether two support ratios point in opposite directions (§66)."""
    if left is None or right is None:
        return False
    if left >= 0.6 and right <= 0.4:
        return True
    if right >= 0.6 and left <= 0.4:
        return True
    return (
        abs(left - right) >= CONFLICT_SUPPORT_GAP and (left - 0.5) * (right - 0.5) < 0
    )


async def record_candidate(
    session: AsyncSession,
    pattern: MinedPattern,
    verdict: ValidationVerdict,
    *,
    learning_run_id: Optional[uuid.UUID] = None,
    settings: Optional[Settings] = None,
    now: Optional[datetime] = None,
) -> KnowledgeUpsertResult:
    """Persist a validated candidate and move it as far as the rules allow."""
    settings = settings or get_settings()
    moment = now or datetime.now(timezone.utc)
    fingerprint = fingerprint_for_pattern(pattern)

    existing_rows = await find_knowledge_by_fingerprint(
        session, project_id=pattern.project_id, fingerprint=fingerprint
    )
    live = [row for row in existing_rows if row.status not in _RETIRED_STATUSES]
    retired = [row for row in existing_rows if row.status in _RETIRED_STATUSES]

    conflicts = await find_conflicts(
        session, pattern, exclude_ids=[row.id for row in existing_rows]
    )

    #: §72. A human said no. Re-mining must not overturn that silently, so a
    #: rejected pattern is checked *before* the create path: the gate has to run
    #: where it can actually fire, and a rejected row is retired by definition.
    rejected_row = next(
        (row for row in retired if row.status == KnowledgeStatus.REJECTED), None
    )
    if (
        rejected_row is not None
        and pattern.sample_count
        < (rejected_row.sample_count or 0) * REJECTION_REOPEN_FACTOR
    ):
        return KnowledgeUpsertResult(
            knowledge=rejected_row,
            skipped_reason="rejected_pattern_not_reopened",
            conflict_ids=[str(item.id) for item in conflicts],
        )

    if live:
        row = live[0]
        changed = _apply_pattern(row, pattern, verdict, moment=moment)
        version_created = False
        if changed:
            version_created = await _write_version(
                session,
                row,
                run_id=learning_run_id,
                note="re-observation",
                moment=moment,
            )
        activated, status_changed = await _advance(
            session,
            row,
            verdict,
            run_id=learning_run_id,
            settings=settings,
            moment=moment,
        )
        return KnowledgeUpsertResult(
            knowledge=row,
            updated=True,
            version_created=version_created,
            status_changed=status_changed,
            activated=activated,
            conflict_ids=[str(item.id) for item in conflicts],
        )

    row = ReliabilityKnowledge(
        project_id=pattern.project_id,
        knowledge_type=pattern.knowledge_type,
        status=KnowledgeStatus.CANDIDATE,
        scope=verdict.scope or pattern.scope,
        environment_id=pattern.environment_id,
        component_id=pattern.component_id,
        title=pattern.title,
        description=pattern.description,
        fingerprint=fingerprint,
        feature_signature=pattern.feature_signature,
        sources=list(pattern.sources),
        experience_ids=list(pattern.experience_ids),
        sample_count=pattern.sample_count,
        success_count=pattern.success_count,
        coverage_start=pattern.coverage_start,
        coverage_end=pattern.coverage_end,
        confidence=verdict.confidence,
        support_strength=pattern.support_strength,
        algorithm=pattern.algorithm,
        algorithm_version="1.0",
        feature_schema_version="1.0",
        validation={
            **verdict.as_dict(),
            "details": pattern.details,
            "conflicts": [str(item.id) for item in conflicts],
        },
        limitations=list(verdict.limitations),
        version=1,
        supersedes_knowledge_id=retired[0].id if retired else None,
        last_confirmed_at=moment,
    )
    session.add(row)
    await session.flush()
    if retired:
        #: The retired row keeps its history and stops being referenced. This is
        #: retired → retired, never a promotion, so it cannot be a way to raise
        #: belief without doing the work.
        predecessor = retired[0]
        predecessor.supersedes_knowledge_id = None
        assert_knowledge_transition(predecessor.status, KnowledgeStatus.SUPERSEDED)
        predecessor.status = KnowledgeStatus.SUPERSEDED
    await _write_version(
        session, row, run_id=learning_run_id, note="created", moment=moment
    )
    activated, status_changed = await _advance(
        session, row, verdict, run_id=learning_run_id, settings=settings, moment=moment
    )
    return KnowledgeUpsertResult(
        knowledge=row,
        created=True,
        version_created=True,
        status_changed=status_changed,
        activated=activated,
        conflict_ids=[str(item.id) for item in conflicts],
    )


_RETIRED_STATUSES = (
    KnowledgeStatus.DEPRECATED,
    KnowledgeStatus.REJECTED,
    KnowledgeStatus.SUPERSEDED,
)


def _apply_pattern(
    row: ReliabilityKnowledge,
    pattern: MinedPattern,
    verdict: ValidationVerdict,
    *,
    moment: datetime,
) -> bool:
    """Fold a re-observation into an existing row.

    Returns whether anything material changed — sample growth alone is not a
    "change" worth versioning, but a new confidence bucket or a moved scope is.
    """
    material = (
        row.confidence != verdict.confidence
        or row.sample_count != pattern.sample_count
        or row.success_count != pattern.success_count
        or (verdict.scope is not None and row.scope != verdict.scope)
    )
    row.sample_count = pattern.sample_count
    row.success_count = pattern.success_count
    row.support_strength = pattern.support_strength
    row.coverage_start = pattern.coverage_start or row.coverage_start
    row.coverage_end = pattern.coverage_end or row.coverage_end
    row.experience_ids = list(pattern.experience_ids)
    row.sources = list(pattern.sources)
    row.title = pattern.title
    row.description = pattern.description
    row.confidence = verdict.confidence
    row.limitations = list(verdict.limitations)
    row.validation = {
        **verdict.as_dict(),
        "details": pattern.details,
    }
    if verdict.scope is not None:
        row.scope = verdict.scope
    #: §25. A pattern the data still shows is freshly confirmed, and that is what
    #: the decay sweep reads.
    row.last_confirmed_at = moment
    return material


async def _advance(
    session: AsyncSession,
    row: ReliabilityKnowledge,
    verdict: ValidationVerdict,
    *,
    run_id: Optional[uuid.UUID],
    settings: Settings,
    moment: datetime,
) -> tuple[bool, bool]:
    """Move the row as far toward ACTIVE as the rules permit.

    Returns ``(activated, status_changed)``.
    """
    status_changed = False
    if row.status in (KnowledgeStatus.REJECTED, KnowledgeStatus.SUPERSEDED):
        return False, False

    if not verdict.promotable:
        return False, False

    if row.status == KnowledgeStatus.CANDIDATE:
        assert_knowledge_transition(row.status, KnowledgeStatus.VALIDATING)
        row.status = KnowledgeStatus.VALIDATING
        status_changed = True

    if row.status == KnowledgeStatus.VALIDATING:
        assert_knowledge_transition(row.status, KnowledgeStatus.VALIDATED)
        row.status = KnowledgeStatus.VALIDATED
        status_changed = True

    if row.status != KnowledgeStatus.VALIDATED:
        return False, status_changed

    auto_ok = (
        settings.INTELLIGENCE_AUTO_ACTIVATE_ENABLED
        and verdict.activatable
        and not requires_human_review(
            row.knowledge_type.value,
            confidence_high_enough=row.confidence == KnowledgeConfidence.HIGH,
            auto_activate_enabled=settings.INTELLIGENCE_AUTO_ACTIVATE_ENABLED,
        )
    )
    if not auto_ok:
        #: §73/§74. Waiting for a human is the default answer, and it is a
        #: recorded state, not a silent no-op.
        return False, status_changed

    assert_knowledge_transition(row.status, KnowledgeStatus.ACTIVE)
    row.status = KnowledgeStatus.ACTIVE
    row.reviewed_at = moment
    row.reviewed_by = "ARGUS (policy-allowed autonomous activation)"
    row.review_reason = (
        "informational pattern, stable evidence, autonomous activation enabled"
    )
    await _write_version(
        session, row, run_id=run_id, note="autonomous activation", moment=moment
    )
    return True, True


async def activate_knowledge(
    session: AsyncSession,
    knowledge: ReliabilityKnowledge,
    *,
    reviewer: str,
    reason: Optional[str] = None,
    run_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
) -> ReliabilityKnowledge:
    """Activate knowledge by explicit human decision (§71, §72).

    Refuses unless the row is at least VALIDATED: activation is a step in a
    lifecycle, not a way to skip one.
    """
    moment = now or datetime.now(timezone.utc)
    if knowledge.status == KnowledgeStatus.CANDIDATE:
        raise IntelligenceStateError(
            "candidate knowledge cannot be activated without validation"
        )
    assert_knowledge_transition(knowledge.status, KnowledgeStatus.ACTIVE)
    knowledge.status = KnowledgeStatus.ACTIVE
    knowledge.reviewed_at = moment
    knowledge.reviewed_by = reviewer
    knowledge.review_reason = reason
    session.add(
        KnowledgeReview(
            knowledge_id=knowledge.id,
            project_id=knowledge.project_id,
            decision="APPROVE",
            reviewer=reviewer,
            reason=reason,
            knowledge_version=knowledge.version,
        )
    )
    await _write_version(
        session, knowledge, run_id=run_id, note="human activation", moment=moment
    )
    await session.flush()
    return knowledge


async def deprecate_knowledge(
    session: AsyncSession,
    knowledge: ReliabilityKnowledge,
    *,
    actor: str,
    reason: str,
    decision: str = "DEPRECATE",
    now: Optional[datetime] = None,
) -> ReliabilityKnowledge:
    """Retire knowledge, keeping the row and the reason (§25, §72)."""
    moment = now or datetime.now(timezone.utc)
    assert_knowledge_transition(knowledge.status, KnowledgeStatus.DEPRECATED)
    knowledge.status = KnowledgeStatus.DEPRECATED
    knowledge.reviewed_at = moment
    knowledge.reviewed_by = actor
    knowledge.review_reason = reason
    session.add(
        KnowledgeReview(
            knowledge_id=knowledge.id,
            project_id=knowledge.project_id,
            decision=decision,
            reviewer=actor,
            reason=reason,
            knowledge_version=knowledge.version,
        )
    )
    await _write_version(session, knowledge, note="deprecated", moment=moment)
    await session.flush()
    return knowledge


async def reject_knowledge(
    session: AsyncSession,
    knowledge: ReliabilityKnowledge,
    *,
    reviewer: str,
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> ReliabilityKnowledge:
    """Reject a candidate, permanently unless the evidence grows (§72)."""
    moment = now or datetime.now(timezone.utc)
    assert_knowledge_transition(knowledge.status, KnowledgeStatus.REJECTED)
    knowledge.status = KnowledgeStatus.REJECTED
    knowledge.reviewed_at = moment
    knowledge.reviewed_by = reviewer
    knowledge.review_reason = reason
    session.add(
        KnowledgeReview(
            knowledge_id=knowledge.id,
            project_id=knowledge.project_id,
            decision="REJECT",
            reviewer=reviewer,
            reason=reason,
            knowledge_version=knowledge.version,
        )
    )
    await _write_version(session, knowledge, note="rejected", moment=moment)
    await session.flush()
    return knowledge


async def request_more_evidence(
    session: AsyncSession,
    knowledge: ReliabilityKnowledge,
    *,
    reviewer: str,
    reason: str,
    now: Optional[datetime] = None,
) -> KnowledgeReview:
    """Send a candidate back for more data, recording the request (§72)."""
    moment = now or datetime.now(timezone.utc)
    if knowledge.status not in (KnowledgeStatus.VALIDATING, KnowledgeStatus.VALIDATED):
        raise IntelligenceStateError(
            f"cannot request more evidence for knowledge in {knowledge.status.value}"
        )
    assert_knowledge_transition(knowledge.status, KnowledgeStatus.CANDIDATE)
    knowledge.status = KnowledgeStatus.CANDIDATE
    knowledge.reviewed_at = moment
    knowledge.reviewed_by = reviewer
    knowledge.review_reason = reason
    review = KnowledgeReview(
        knowledge_id=knowledge.id,
        project_id=knowledge.project_id,
        decision="REQUEST_MORE_EVIDENCE",
        reviewer=reviewer,
        reason=reason,
        knowledge_version=knowledge.version,
    )
    session.add(review)
    await session.flush()
    return review


async def refresh_staleness(
    session: AsyncSession,
    *,
    project_id: Optional[uuid.UUID] = None,
    settings: Optional[Settings] = None,
    now: Optional[datetime] = None,
    limit: int = 500,
) -> list[ReliabilityKnowledge]:
    """Deprecate knowledge that new data has stopped confirming (§25).

    Deprecation, not deletion: the row, its versions and the reason why it was
    retired all survive, so the answer to "what did ARGUS believe, and when did
    it stop believing it" remains available.
    """
    settings = settings or get_settings()
    moment = now or datetime.now(timezone.utc)
    horizon = moment - timedelta(days=settings.INTELLIGENCE_KNOWLEDGE_STALE_AFTER_DAYS)

    stmt = (
        select(ReliabilityKnowledge)
        .where(
            ReliabilityKnowledge.status.in_(
                [KnowledgeStatus.VALIDATED, KnowledgeStatus.ACTIVE]
            )
        )
        .limit(limit)
    )
    if project_id is not None:
        stmt = stmt.where(ReliabilityKnowledge.project_id == project_id)

    retired: list[ReliabilityKnowledge] = []
    for row in (await session.scalars(stmt)).all():
        confirmed = row.last_confirmed_at or row.updated_at
        if _aware(confirmed) > horizon:
            continue
        try:
            assert_knowledge_transition(row.status, KnowledgeStatus.DEPRECATED)
        except IntelligenceStateError:
            continue
        row.status = KnowledgeStatus.DEPRECATED
        row.reviewed_at = moment
        row.reviewed_by = "ARGUS (staleness sweep)"
        row.review_reason = (
            f"no confirming observation since {_aware(confirmed).date()} "
            f"(>{settings.INTELLIGENCE_KNOWLEDGE_STALE_AFTER_DAYS}d)"
        )
        await _write_version(session, row, note="stale", moment=moment)
        retired.append(row)
    if retired:
        await session.flush()
    return retired


async def list_knowledge_versions(
    session: AsyncSession, *, knowledge_id: uuid.UUID
) -> list[KnowledgeVersion]:
    """The version ledger for one knowledge item, newest first (§26)."""
    stmt = (
        select(KnowledgeVersion)
        .where(KnowledgeVersion.knowledge_id == knowledge_id)
        .order_by(KnowledgeVersion.version.desc(), KnowledgeVersion.created_at.desc())
    )
    return list((await session.scalars(stmt)).all())


async def _write_version(
    session: AsyncSession,
    knowledge: ReliabilityKnowledge,
    *,
    run_id: Optional[uuid.UUID] = None,
    note: str,
    moment: datetime,
) -> bool:
    """Append an immutable snapshot of the current revision (§26).

    ``run_id`` is optional because not every revision comes from a learning run:
    a human deprecation and the staleness sweep both write versions with no
    originating run, and inventing one would put a false entry in the §79 audit
    chain.
    """
    version_number = knowledge.version or 1
    exists = await session.scalar(
        select(KnowledgeVersion.id)
        .where(KnowledgeVersion.knowledge_id == knowledge.id)
        .where(KnowledgeVersion.version == version_number)
        .where(KnowledgeVersion.note == note)
    )
    if exists is not None and note == "re-observation":
        #: A run that re-observes the same pattern does not need a new ledger
        #: row per pass; the row is already recorded at this version.
        return False

    session.add(
        KnowledgeVersion(
            knowledge_id=knowledge.id,
            version=version_number,
            status=knowledge.status,
            sample_count=knowledge.sample_count,
            confidence=knowledge.confidence,
            snapshot={
                "title": knowledge.title,
                "description": knowledge.description,
                "status": knowledge.status.value,
                "confidence": knowledge.confidence.value,
                "sample_count": knowledge.sample_count,
                "success_count": knowledge.success_count,
                "support_strength": knowledge.support_strength,
                "scope": knowledge.scope.value,
                "feature_signature": knowledge.feature_signature,
                "experience_ids": knowledge.experience_ids,
                "limitations": knowledge.limitations,
            },
            learning_run_id=run_id,
            activated_at=(
                moment if knowledge.status == KnowledgeStatus.ACTIVE else None
            ),
            deactivated_at=(
                moment
                if knowledge.status
                in (KnowledgeStatus.DEPRECATED, KnowledgeStatus.SUPERSEDED)
                else None
            ),
            note=note,
        )
    )
    await session.flush()
    return True


def recommendable(knowledge: ReliabilityKnowledge) -> bool:
    """Whether this row may be cited by a recommendation (§4).

    Delegates to the lifecycle map rather than restating it: two definitions of
    "recommendable" is exactly how a deprecated pattern ends up quoted in the UI.
    """
    return is_recommendable(knowledge.status)


def _aware(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


__all__ = [
    "CONFLICT_SUPPORT_GAP",
    "KnowledgeUpsertResult",
    "REJECTION_REOPEN_FACTOR",
    "activate_knowledge",
    "deprecate_knowledge",
    "find_conflicts",
    "find_knowledge_by_fingerprint",
    "fingerprint_for_pattern",
    "list_knowledge_versions",
    "recommendable",
    "record_candidate",
    "refresh_staleness",
    "reject_knowledge",
    "request_more_evidence",
]
