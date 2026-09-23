"""Time handling shared by the Phase 11 platform services.

Every phase needed this and each one wrote its own `_aware`, with subtly
different behaviour. Consolidating it here is the small version of what Phase 11
is for, and it fixes a real class of bug rather than tidying: the model layer
declares most timestamp columns as ``Mapped[datetime]`` (NOT NULL) while a few —
``resolved_at``, ``completed_at``, ``acknowledged_at`` — really can be NULL. A
helper typed as ``Optional[datetime] -> Optional[datetime]`` erases that
distinction, so callers either crash on a None or, worse, paper over a missing
timestamp with a default and report a fabricated duration.

The overloads restore the distinction the model already knows:

* ``aware(datetime) -> datetime`` — a NOT NULL column stays non-optional, so
  ``aware(row.detected_at).isoformat()`` type-checks *and* is correct.
* ``aware(None) -> None`` — a nullable column stays nullable, so the caller has
  to decide what a missing instant means.

For the second case the answers differ by intent, and the helpers below name
them instead of hiding them:

* :func:`stamp` — render for display, empty string when unknown. Never "now".
* :func:`optional_stamp` — render for JSON, ``None`` when unknown.
* :func:`elapsed_seconds` / :func:`shift` — arithmetic that yields ``None``
  rather than inventing a datetime for a fact ARGUS does not have.
* :func:`earliest` / :func:`latest` — reduce an iterable, skipping NULLs.

The rule, consistent with every other phase: an unknown instant is reported as
unknown. It is never replaced by the current time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, overload

__all__ = [
    "aware",
    "elapsed_seconds",
    "earliest",
    "latest",
    "optional_stamp",
    "shift",
    "stamp",
    "utcnow",
]


@overload
def aware(value: datetime) -> datetime: ...


@overload
def aware(value: None) -> None: ...


@overload
def aware(value: Optional[datetime]) -> Optional[datetime]: ...


def aware(value: Optional[datetime]) -> Optional[datetime]:
    """Return ``value`` as a timezone-aware UTC instant, or ``None``.

    Naive values are *assumed* UTC rather than local: every row ARGUS writes is
    UTC, and guessing the server's local zone would make history depend on where
    the process happens to run.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utcnow() -> datetime:
    """Now, as an aware UTC instant."""
    return datetime.now(timezone.utc)


def stamp(value: Optional[datetime]) -> str:
    """ISO-8601 rendering for **display**, empty string when the instant is NULL.

    Deliberately not ``None``: these call sites build response dictionaries and
    templates where an empty cell reads as "unknown" and a missing key does not.
    """
    moment = aware(value)
    return moment.isoformat() if moment is not None else ""


def optional_stamp(value: Optional[datetime]) -> Optional[str]:
    """ISO-8601 rendering for JSON, ``None`` when the instant is NULL."""
    moment = aware(value)
    return moment.isoformat() if moment is not None else None


def elapsed_seconds(
    start: Optional[datetime], end: Optional[datetime]
) -> Optional[float]:
    """``end - start`` in seconds, or ``None`` when either side is unknown.

    Returning ``None`` rather than ``0.0`` is the point: a zero would be read as
    "instantaneous" and would quietly pull an average down.
    """
    first = aware(start)
    second = aware(end)
    if first is None or second is None:
        return None
    return (second - first).total_seconds()


def shift(value: Optional[datetime], delta: timedelta) -> Optional[datetime]:
    """Offset an instant, or ``None`` when there is nothing to offset."""
    moment = aware(value)
    return moment + delta if moment is not None else None


def earliest(values: Iterable[Optional[datetime]]) -> Optional[datetime]:
    """The oldest known instant, ignoring NULLs. ``None`` if none is known."""
    known = [moment for moment in (aware(value) for value in values) if moment]
    return min(known) if known else None


def latest(values: Iterable[Optional[datetime]]) -> Optional[datetime]:
    """The newest known instant, ignoring NULLs. ``None`` if none is known."""
    known = [moment for moment in (aware(value) for value in values) if moment]
    return max(known) if known else None
