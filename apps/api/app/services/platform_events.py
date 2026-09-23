"""ARGUS Platform Event Bus (Phase 11 §9, §10).

The unified event stream. Every phase that concludes something publishes here,
and the control plane consumes the stream to build one operational story per
situation:

    Deployment → latency anomaly → incident → RCA → prediction → remediation
              → recovery → learning

Three decisions shape this module, and each one is a deliberate rejection of a
heavier alternative:

**It is a table, not a broker.** Phase 10 already established the pattern — an
append-only row with a deterministic ``dedup_key`` and a ``processed_at`` marker
— and Phase 0 already runs a durable queue with bounded retry and dead-lettering.
Adding Kafka or Redis Streams here would buy nothing the phases actually need
(a small, ordered, replayable log) while adding a second thing that can be down.
An event stream that cannot be replayed is not evidence, and a row can always be
replayed.

**Publishing is best-effort and never blocking.** Every producer goes through
:func:`safely_publish_event`, exactly as Phase 10's learning hooks do: an event
table problem must be able to fail the *control plane*, never the incident
resolution that produced the event. A control plane that can break the system it
observes is not a control plane.

**Correlation is explicit, never inferred.** ``correlation_id`` is derived from
the subject that the situation is anchored on (one incident, one case, one
deployment) and set by the publisher. Nothing correlates events by comparing
timestamps and hoping — pattern-matching timelines is exactly how a platform
starts reporting causality it cannot support.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.platform import PlatformEvent, PlatformEventType

logger = logging.getLogger(__name__)


def _aware(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def compute_dedup_key(
    *,
    project_id: Optional[uuid.UUID],
    event_type: PlatformEventType,
    subject_id: Optional[uuid.UUID],
    occurred_at: datetime,
    extra: Sequence[Any] = (),
) -> str:
    """The event's identity: scope + kind + subject + the moment it happened.

    ``occurred_at`` is part of the key on purpose. The same subject legitimately
    produces the same event type several times (a remediation that completes,
    rolls back and completes again), and those are three real events, not one
    duplicate. Two publishers reporting the *same* fact about the *same* moment
    collapse to one row, which is what dedup is for.
    """
    parts = [
        str(project_id or ""),
        event_type.value,
        str(subject_id or ""),
        occurred_at.isoformat(),
        *[str(item) for item in extra],
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:64]


async def publish_event(
    session: AsyncSession,
    *,
    event_type: PlatformEventType,
    source: str,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    subject_type: Optional[str] = None,
    subject_id: Optional[uuid.UUID] = None,
    component_id: Optional[uuid.UUID] = None,
    case_id: Optional[uuid.UUID] = None,
    correlation_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    occurred_at: Optional[datetime] = None,
    dedup_extra: Sequence[Any] = (),
) -> Optional[PlatformEvent]:
    """Record one platform event, deduplicating identical facts.

    Returns the existing row when the same fact was already recorded, so a
    producer that fires twice (a retried job, a sweep overlapping the ingest
    hook) appends nothing.
    """
    if subject_id is None:
        #: An event with no subject cannot be traced to anything: refuse rather
        #: than store an unattributable row.
        return None
    moment = _aware(occurred_at)
    dedup_key = compute_dedup_key(
        project_id=project_id,
        event_type=event_type,
        subject_id=subject_id,
        occurred_at=moment,
        extra=(source, *dedup_extra),
    )
    existing = (
        await session.execute(
            select(PlatformEvent).where(PlatformEvent.dedup_key == dedup_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    event = PlatformEvent(
        project_id=project_id,
        environment_id=environment_id,
        event_type=event_type,
        case_id=case_id,
        correlation_id=correlation_id,
        source=source,
        subject_type=subject_type,
        subject_id=subject_id,
        component_id=component_id,
        payload=payload,
        occurred_at=moment,
        dedup_key=dedup_key,
    )
    session.add(event)
    await session.flush()
    return event


async def safely_publish_event(
    session: AsyncSession, **kwargs: Any
) -> Optional[PlatformEvent]:
    """``publish_event`` that never raises.

    Used by Phases 3–10 producer points: a platform-event problem must not be
    able to fail the thing that produced the event, for the same reason
    ``safely_publish_learning_event`` exists.
    """
    try:
        return await publish_event(session, **kwargs)
    except Exception:  # pragma: no cover - defensive; must never propagate
        logger.warning("platform event publish failed", exc_info=True)
        return None


def correlation_id_for(*, kind: str, subject_id: uuid.UUID) -> str:
    """The correlation id a situation's events share.

    Anchored on one stored subject (an incident, a case, a deployment) so every
    event about that situation carries the same id. This is what makes the §10
    chain a query instead of a heuristic.
    """
    return f"{kind}:{subject_id}"


# ---------------------------------------------------------------------------
# Consumption
# ---------------------------------------------------------------------------
@dataclass
class EventPage:
    """A page of the activity feed, with its own completeness statement."""

    events: list[PlatformEvent] = field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "events": self.events,
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
        }


async def pending_events(
    session: AsyncSession, *, limit: int = 200, project_id: Optional[uuid.UUID] = None
) -> list[PlatformEvent]:
    """Unconsumed events, oldest first — the control plane's work queue."""
    stmt = (
        select(PlatformEvent)
        .where(PlatformEvent.processed_at.is_(None))
        .order_by(PlatformEvent.occurred_at)
        .limit(limit)
    )
    if project_id is not None:
        stmt = stmt.where(PlatformEvent.project_id == project_id)
    return list((await session.scalars(stmt)).all())


async def mark_processed(
    session: AsyncSession,
    *,
    event_ids: Sequence[uuid.UUID],
    consumer: str,
    now: Optional[datetime] = None,
) -> int:
    """Record that a consumer has acted on these events."""
    if not event_ids:
        return 0
    result = await session.execute(
        update(PlatformEvent)
        .where(PlatformEvent.id.in_(list(event_ids)))
        .values(processed_at=_aware(now), consumed_by=consumer)
    )
    #: ``AsyncSession.execute`` is annotated as ``Result``, but a DML statement
    #: always yields a ``CursorResult`` — which is where ``rowcount`` lives.
    return int(cast(CursorResult, result).rowcount or 0)


async def events_for_subject(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    limit: int = 100,
) -> list[PlatformEvent]:
    """Everything the platform recorded about one stored subject."""
    stmt = (
        select(PlatformEvent)
        .where(PlatformEvent.subject_id == subject_id)
        .order_by(PlatformEvent.occurred_at)
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def story_for_correlation(
    session: AsyncSession, *, correlation_id: str, limit: int = 200
) -> list[PlatformEvent]:
    """The ordered chain of events that share one correlation id (§10).

    This is the read that turns "a deployment happened and later an incident was
    opened" into one story. It is reported as a *sequence*, and the API never
    presents the sequence as a causal chain: order is evidence, not proof.
    """
    stmt = (
        select(PlatformEvent)
        .where(PlatformEvent.correlation_id == correlation_id)
        .order_by(PlatformEvent.occurred_at)
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def recent_events(
    session: AsyncSession,
    *,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    event_types: Optional[Sequence[PlatformEventType]] = None,
    since: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PlatformEvent]:
    """The activity feed query: recent events, filtered, newest first."""
    stmt = select(PlatformEvent).order_by(PlatformEvent.occurred_at.desc())
    if project_id is not None:
        stmt = stmt.where(PlatformEvent.project_id == project_id)
    if environment_id is not None:
        stmt = stmt.where(PlatformEvent.environment_id == environment_id)
    if event_types:
        stmt = stmt.where(PlatformEvent.event_type.in_(list(event_types)))
    if since is not None:
        stmt = stmt.where(PlatformEvent.occurred_at >= _aware(since))
    stmt = stmt.limit(limit).offset(offset)
    return list((await session.scalars(stmt)).all())


async def prune_events(
    session: AsyncSession, *, older_than: datetime, limit: int = 5000
) -> int:
    """Retention for the event stream itself (§49).

    Only *processed* events are prunable: an unconsumed event is work the control
    plane has not done yet, and deleting it would silently drop the platform's
    own to-do list.
    """
    from sqlalchemy import delete

    stmt = (
        delete(PlatformEvent)
        .where(
            PlatformEvent.processed_at.is_not(None),
            PlatformEvent.occurred_at < older_than,
        )
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    return int(cast(CursorResult, result).rowcount or 0)


def summarize_window(
    events: Sequence[PlatformEvent], *, window: timedelta
) -> dict[str, int]:
    """Counts by event type inside a window — the dashboard's activity strip."""
    cutoff = datetime.now(timezone.utc) - window
    counts: dict[str, int] = {}
    for event in events:
        occurred = _aware(event.occurred_at)
        if occurred < cutoff:
            continue
        key = getattr(event.event_type, "value", str(event.event_type))
        counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = [
    "EventPage",
    "compute_dedup_key",
    "correlation_id_for",
    "events_for_subject",
    "mark_processed",
    "pending_events",
    "prune_events",
    "publish_event",
    "recent_events",
    "safely_publish_event",
    "story_for_correlation",
    "summarize_window",
]
