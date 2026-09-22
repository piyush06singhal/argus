"""A single clock for the remediation engine (Phase 9 §28, §39).

Every timestamp the remediation engine writes — assessments, approvals,
executions, verification windows, control expiry, audit events — is produced by
:func:`utcnow`, so "when did this happen" has one answer and is timezone-aware
on every backend.

Timestamps are stored timezone-aware and compared timezone-aware. Naive
datetimes are upgraded rather than silently compared, because comparing a naive
``datetime.now()`` against a stored aware value raises on some backends and
*lies* on others depending on server timezone — a failure mode that would show
up as a remediation firing at the wrong moment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    """The current UTC time, always timezone-aware."""
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    """Return ``value`` as timezone-aware UTC.

    A naive value is assumed to be UTC rather than local: everything ARGUS writes
    is UTC, so a naive value that reaches here came from a caller that dropped
    the tzinfo, not from a user in another timezone.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def seconds_between(start: datetime, end: datetime) -> int:
    """Whole seconds between two instants, never negative."""
    delta = aware(end) - aware(start)
    return max(0, int(delta.total_seconds()))


def window(seconds: int, *, end: datetime | None = None) -> tuple[datetime, datetime]:
    """A ``(start, end)`` observation window ending now (or at ``end``)."""
    finish = aware(end or utcnow())
    return finish - timedelta(seconds=max(0, seconds)), finish


def is_expired(expires_at: datetime | None, *, now: datetime | None = None) -> bool:
    """Whether an expiry instant has passed. ``None`` never expires."""
    if expires_at is None:
        return False
    return aware(expires_at) <= aware(now or utcnow())


__all__ = [
    "aware",
    "is_expired",
    "seconds_between",
    "utcnow",
    "window",
]
