"""ARGUS Remediation Audit Trail (Phase 9 §35, §12).

Every gate decision, state change, execution, verification and rollback writes
an audit event. This module exists so that writing one is a one-liner at the call
site and cannot be forgotten at the interesting moments — the refusals.

The trail is **hash-chained**: each event carries ``prev_hash`` (the digest of the
event before it for the same action) and ``entry_hash`` (a digest over the event's
own content plus ``prev_hash``). Two consequences follow, and they are the reason
the chain exists:

* deleting an event breaks the chain at a specific, identifiable sequence number
  rather than leaving a tidy-looking history;
* editing an event's summary or detail changes its digest, so the chain no longer
  verifies.

This is tamper *evidence*, not tamper *proof*: an attacker with database write
access can recompute the whole chain. It is stated that way because claiming
otherwise would be exactly the kind of overstatement Phase 9 is supposed to
avoid. What it does give is that the honest case — an operator asking "was this
record altered?" — has a real, checkable answer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.remediation import (
    RemediationAction,
    RemediationActorType,
    RemediationAuditEvent,
    RemediationAuditEventType,
    RemediationStatus,
)
from app.services.remediation_clock import aware, utcnow

logger = logging.getLogger(__name__)

#: Fields included in the digest, in a fixed order. Order matters: the digest is
#: over a canonical form, never over a dict's incidental iteration order.
_DIGEST_FIELDS = (
    "action_id",
    "sequence",
    "event_type",
    "actor_type",
    "actor",
    "from_status",
    "to_status",
    "summary",
    "detail",
    "occurred_at",
    "prev_hash",
)


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(payload: dict[str, Any]) -> str:
    """The SHA-256 digest of an event payload's canonical form."""
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _payload(
    *,
    action_id: Optional[uuid.UUID],
    sequence: int,
    event_type: RemediationAuditEventType,
    actor_type: RemediationActorType,
    actor: Optional[str],
    from_status: Optional[RemediationStatus],
    to_status: Optional[RemediationStatus],
    summary: str,
    detail: Optional[dict],
    occurred_at: datetime,
    prev_hash: Optional[str],
) -> dict[str, Any]:
    return {
        "action_id": str(action_id) if action_id else None,
        "sequence": sequence,
        "event_type": event_type.value,
        "actor_type": actor_type.value,
        "actor": actor,
        "from_status": from_status.value if from_status else None,
        "to_status": to_status.value if to_status else None,
        "summary": summary,
        "detail": detail,
        "occurred_at": aware(occurred_at).isoformat(),
        "prev_hash": prev_hash,
    }


class AuditTrail:
    """Writes and verifies the per-action audit chain."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        action: Optional[RemediationAction],
        event_type: RemediationAuditEventType,
        *,
        project_id: Optional[uuid.UUID] = None,
        environment_id: Optional[uuid.UUID] = None,
        actor_type: RemediationActorType = RemediationActorType.SYSTEM,
        actor: str = "system",
        summary: str = "",
        detail: Optional[dict[str, Any]] = None,
        from_status: Optional[RemediationStatus] = None,
        to_status: Optional[RemediationStatus] = None,
        now: Optional[datetime] = None,
    ) -> RemediationAuditEvent:
        """Append one event to the chain.

        ``project_id`` is derived from the action when one is given, so a caller
        cannot accidentally file an event under the wrong tenant.
        """
        now = aware(now or utcnow())
        if project_id is None and action is not None:
            project_id = action.project_id
        if environment_id is None and action is not None:
            environment_id = action.environment_id
        if project_id is None:
            raise ValueError("an audit event requires a project scope")

        action_id = action.id if action is not None else None
        if from_status is None and action is not None:
            from_status = action.status if to_status is None else from_status

        for attempt in range(2):
            sequence, prev_hash = await self._next_link(action_id)
            payload = _payload(
                action_id=action_id,
                sequence=sequence,
                event_type=event_type,
                actor_type=actor_type,
                actor=actor,
                from_status=from_status,
                to_status=to_status,
                summary=summary,
                detail=detail,
                occurred_at=now,
                prev_hash=prev_hash,
            )
            row = RemediationAuditEvent(
                action_id=action_id,
                project_id=project_id,
                environment_id=environment_id,
                sequence=sequence,
                event_type=event_type,
                actor_type=actor_type,
                actor=actor,
                from_status=from_status,
                to_status=to_status,
                summary=summary,
                detail=detail,
                occurred_at=now,
                prev_hash=prev_hash,
                entry_hash=compute_hash(payload),
            )
            self._session.add(row)
            try:
                await self._session.flush()
                return row
            except IntegrityError:
                # Another writer took this sequence number. Roll back to the
                # savepoint-free boundary by expunging our row and retrying once
                # with a fresh sequence; a second failure is a genuine bug.
                await self._session.rollback()
                if attempt == 1:
                    raise
                logger.warning(
                    "audit sequence collision for action %s; retrying", action_id
                )
        raise RuntimeError("unreachable")  # pragma: no cover

    async def _next_link(
        self, action_id: Optional[uuid.UUID]
    ) -> tuple[int, Optional[str]]:
        """The next sequence number and the previous event's digest."""
        if action_id is None:
            stmt = (
                select(RemediationAuditEvent)
                .where(RemediationAuditEvent.action_id.is_(None))
                .order_by(RemediationAuditEvent.sequence.desc())
                .limit(1)
            )
        else:
            stmt = (
                select(RemediationAuditEvent)
                .where(RemediationAuditEvent.action_id == action_id)
                .order_by(RemediationAuditEvent.sequence.desc())
                .limit(1)
            )
        last = (await self._session.execute(stmt)).scalars().first()
        if last is None:
            return 1, None
        return last.sequence + 1, last.entry_hash

    async def history(
        self, action_id: uuid.UUID, *, limit: int = 500
    ) -> Sequence[RemediationAuditEvent]:
        """Every audit event for an action, in chain order."""
        stmt = (
            select(RemediationAuditEvent)
            .where(RemediationAuditEvent.action_id == action_id)
            .order_by(RemediationAuditEvent.sequence)
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def verify_chain(self, action_id: uuid.UUID) -> dict[str, Any]:
        """Recompute the chain and report the first break, if there is one.

        Returns ``{"intact": bool, "events": int, "broken_at": int|None,
        "reason": str|None}``. The digest is recomputed from the *stored* fields,
        so an edit to any stored field shows up as a mismatch.
        """
        events = await self.history(action_id)
        prev: Optional[str] = None
        for index, event in enumerate(events):
            if event.prev_hash != prev:
                return {
                    "intact": False,
                    "events": len(events),
                    "broken_at": event.sequence,
                    "reason": (
                        "the event does not link to the previous digest "
                        "(an event may have been removed or inserted)"
                    ),
                }
            payload = _payload(
                action_id=event.action_id,
                sequence=event.sequence,
                event_type=event.event_type,
                actor_type=event.actor_type,
                actor=event.actor,
                from_status=event.from_status,
                to_status=event.to_status,
                summary=event.summary,
                detail=event.detail,
                occurred_at=event.occurred_at,
                prev_hash=event.prev_hash,
            )
            if compute_hash(payload) != event.entry_hash:
                return {
                    "intact": False,
                    "events": len(events),
                    "broken_at": event.sequence,
                    "reason": "the event's content does not match its digest",
                }
            prev = event.entry_hash
        return {
            "intact": True,
            "events": len(events),
            "broken_at": None,
            "reason": None,
        }


def summarize_event(event: RemediationAuditEvent) -> dict[str, Any]:
    """A JSON-safe rendering of one audit event."""
    return {
        "id": str(event.id),
        "sequence": event.sequence,
        "event_type": event.event_type.value,
        "actor_type": event.actor_type.value,
        "actor": event.actor,
        "from_status": event.from_status.value if event.from_status else None,
        "to_status": event.to_status.value if event.to_status else None,
        "summary": event.summary,
        "detail": event.detail,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "entry_hash": event.entry_hash,
        "prev_hash": event.prev_hash,
    }


__all__ = [
    "AuditTrail",
    "compute_hash",
    "summarize_event",
]
