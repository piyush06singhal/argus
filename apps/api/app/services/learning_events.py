"""ARGUS Learning Event Inbox (Phase 10 §6, §63, §64, §76).

The pipeline's *producers* live in Phases 1–9 (an incident resolves, a
remediation verifies, a patch regresses). They publish here, and the learning
run consumes what was published. Three properties matter:

* **Idempotent by construction.** Every event carries a ``dedup_key`` derived from
  the outcome itself, and :func:`publish_learning_event` writes at most one row
  per key. Publishing the same completion twice — a retried request, a sweep
  that re-runs, a worker that restarted mid-flight — produces one logical event,
  so it cannot produce two patterns (§64).
* **Never load-bearing.** :func:`safely_publish_learning_event` swallows every
  error: a project must never fail to resolve an incident because the *learning*
  table was unavailable. Learning is a consumer of history, not a dependency of
  it.
* **Recorded even when unusable.** An event that names a row the run cannot
  assemble into an experience is still stored and marked
  ``unprocessable_reason``. Dropping it would make the learning layer's coverage
  invisible (§79).

Trust is a first-class input (§76): each event carries its provenance, and the
run refuses to learn from sources outside the trusted set. AI-generated
hypotheses are recorded — they are evidence of what ARGUS thought — but are not
trained on as if they were outcomes (§77).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.intelligence import (
    DataProvenance,
    LearningEvent,
    LearningEventHook,
    LearningEventType,
)
from app.services.intelligence_state import (
    DEFAULT_ENABLED_EVENT_TYPES,
    trusted_provenance_names,
)

logger = logging.getLogger(__name__)


def dedup_key_for(
    event_type: LearningEventType,
    subject_id: Any,
    *extra: Any,
) -> str:
    """Deterministic identity for a completed outcome (§64).

    The subject is *not* optional: an event without a subject could be published
    twice with different meanings and still collapse into one row. Callers whose
    subject can legitimately complete more than once (an incident reopened and
    re-resolved) pass the completion moment as an extra part, which is what makes
    the second completion a genuinely new event.
    """
    parts = [event_type.value, str(subject_id)]
    parts += [str(item) for item in extra if item is not None]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:120]


async def publish_learning_event(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    event_type: LearningEventType,
    subject_id: uuid.UUID,
    occurred_at: Optional[datetime] = None,
    payload: Optional[dict[str, Any]] = None,
    provenance: DataProvenance = DataProvenance.SYSTEM_GENERATED,
    dedup_extra: Sequence[Any] = (),
    flush: bool = True,
) -> Optional[LearningEvent]:
    """Record one completed outcome, at most once.

    Returns the new row, or ``None`` when an event with the same identity already
    exists — ``None`` is the normal, expected answer for a repeated call and is
    not an error.
    """
    key = dedup_key_for(event_type, subject_id, *dedup_extra)
    moment = occurred_at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    existing = await session.scalar(
        select(LearningEvent.id).where(LearningEvent.dedup_key == key)
    )
    if existing is not None:
        return None

    event = LearningEvent(
        project_id=project_id,
        event_type=event_type,
        subject_id=subject_id,
        dedup_key=key,
        payload=dict(payload or {}),
        occurred_at=moment,
        provenance=provenance,
    )
    session.add(event)
    if not flush:
        return event

    try:
        #: A savepoint, not a bare flush: two publishers racing on the same key
        #: must not poison the caller's transaction for the *other* work it is
        #: doing. On conflict only this insert is rolled back.
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        logger.debug("learning event already recorded (dedup_key=%s)", key)
        return None
    return event


async def safely_publish_learning_event(
    session: AsyncSession,
    *,
    project_id: Optional[uuid.UUID],
    event_type: LearningEventType,
    subject_id: Optional[uuid.UUID],
    occurred_at: Optional[datetime] = None,
    payload: Optional[dict[str, Any]] = None,
    provenance: DataProvenance = DataProvenance.SYSTEM_GENERATED,
    dedup_extra: Sequence[Any] = (),
) -> Optional[LearningEvent]:
    """Publish best-effort, for callers that must not fail if learning is down.

    Used by the Phase 3–9 services: the alternative — letting a learning-table
    error break incident resolution — inverts the dependency the phase is built
    on.
    """
    if project_id is None or subject_id is None:
        return None
    try:
        return await publish_learning_event(
            session,
            project_id=project_id,
            event_type=event_type,
            subject_id=subject_id,
            occurred_at=occurred_at,
            payload=payload,
            provenance=provenance,
            dedup_extra=dedup_extra,
        )
    except Exception:  # pragma: no cover - defensive; must never propagate
        logger.warning(
            "failed to record learning event %s for subject %s",
            event_type.value,
            subject_id,
            exc_info=True,
        )
        return None


# --------------------------------------------------------------------------
# Consumption
# --------------------------------------------------------------------------


async def claim_unprocessed_events(
    session: AsyncSession,
    *,
    project_id: Optional[uuid.UUID],
    cutoff: datetime,
    limit: int,
    trusted: Optional[Iterable[DataProvenance]] = None,
) -> list[LearningEvent]:
    """Fetch events that are due, oldest first, bounded by ``limit``.

    ``cutoff`` is the §31 temporal boundary and is applied to ``occurred_at``,
    not to when the row was written: an outcome that happened after the cutoff
    must not influence knowledge the run claims to have as of the cutoff, even
    if the row was inserted before it.
    """
    stmt = (
        select(LearningEvent)
        .where(LearningEvent.processed_at.is_(None))
        .where(LearningEvent.occurred_at <= cutoff)
        .order_by(LearningEvent.occurred_at.asc(), LearningEvent.created_at.asc())
        .limit(limit)
    )
    if project_id is not None:
        stmt = stmt.where(LearningEvent.project_id == project_id)
    if trusted is not None:
        allowed = list(trusted)
        if allowed:
            stmt = stmt.where(LearningEvent.provenance.in_(allowed))
        else:
            return []

    return list((await session.scalars(stmt)).all())


async def mark_event_processed(
    session: AsyncSession,
    event: LearningEvent,
    *,
    run_id: Optional[uuid.UUID],
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> None:
    """Mark one event consumed, with the reason when it produced nothing."""
    event.processed_at = now or datetime.now(timezone.utc)
    event.processed_by_run_id = run_id
    if reason is not None:
        event.unprocessable_reason = reason


async def enabled_event_types(
    session: AsyncSession, *, project_id: Optional[uuid.UUID] = None
) -> frozenset[LearningEventType]:
    """Which event types this project learns from (§63).

    A project-level hook row wins over the global one, and the default is every
    event type — the permissive case is the *default*, and narrowing it is an
    explicit operator act.
    """
    hook = await _resolve_hook(session, project_id=project_id)
    if hook is None or not hook.enabled_event_types:
        return frozenset(DEFAULT_ENABLED_EVENT_TYPES)
    resolved: set[LearningEventType] = set()
    for value in hook.enabled_event_types:
        try:
            resolved.add(LearningEventType(str(value)))
        except ValueError:
            logger.warning("ignoring unknown learning event type in hook: %r", value)
    return frozenset(resolved)


async def trusted_provenance(
    session: AsyncSession, *, project_id: Optional[uuid.UUID] = None
) -> frozenset[DataProvenance]:
    """Which provenance classes this project learns from (§76, §77).

    The settings decide what is trusted; a hook row may only *narrow* that set,
    never widen it. An operator cannot turn on learning from AI output by editing
    a database row if the deployment has not allowed it — the trust decision
    stays where the deployment made it.
    """
    settings = get_settings()
    base = set(
        trusted_provenance_names(
            include_ai=settings.INTELLIGENCE_INCLUDE_AI_GENERATED,
            include_mock=settings.INTELLIGENCE_INCLUDE_MOCK,
        )
    )
    hook = await _resolve_hook(session, project_id=project_id)
    if hook is None or not hook.trusted_provenance:
        return frozenset(base)
    narrowed: set[DataProvenance] = set()
    for value in hook.trusted_provenance:
        try:
            candidate = DataProvenance(str(value))
        except ValueError:
            logger.warning("ignoring unknown provenance in hook: %r", value)
            continue
        if candidate in base:
            narrowed.add(candidate)
    return frozenset(narrowed or base)


async def _resolve_hook(
    session: AsyncSession, *, project_id: Optional[uuid.UUID]
) -> Optional[LearningEventHook]:
    if project_id is not None:
        hook = await session.scalar(
            select(LearningEventHook).where(LearningEventHook.project_id == project_id)
        )
        if hook is not None:
            return hook
    return await session.scalar(
        select(LearningEventHook).where(LearningEventHook.project_id.is_(None))
    )


async def set_event_hook(
    session: AsyncSession,
    *,
    project_id: Optional[uuid.UUID],
    enabled_event_types: Optional[Iterable[str]] = None,
    trusted_provenance_classes: Optional[Iterable[str]] = None,
    updated_by: Optional[str] = None,
) -> LearningEventHook:
    """Create or update the hook row for a project (or the global default)."""
    hook = (
        await _resolve_hook(session, project_id=project_id)
        if project_id is None
        else (
            await session.scalar(
                select(LearningEventHook).where(
                    LearningEventHook.project_id == project_id
                )
            )
        )
    )
    if hook is None or (project_id is not None and hook.project_id != project_id):
        hook = LearningEventHook(
            project_id=project_id,
            enabled_event_types=[],
            trusted_provenance=[],
        )
        session.add(hook)

    if enabled_event_types is not None:
        hook.enabled_event_types = [str(value) for value in enabled_event_types]
    if trusted_provenance_classes is not None:
        hook.trusted_provenance = [str(value) for value in trusted_provenance_classes]
    if updated_by is not None:
        hook.updated_by = updated_by
    await session.flush()
    return hook


__all__ = [
    "claim_unprocessed_events",
    "dedup_key_for",
    "enabled_event_types",
    "mark_event_processed",
    "publish_learning_event",
    "safely_publish_learning_event",
    "set_event_hook",
    "trusted_provenance",
]
