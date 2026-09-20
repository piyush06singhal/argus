"""Timezone-safe datetime helpers (single canonical implementation).

PostgreSQL returns timezone-aware datetimes for ``timestamptz`` while SQLite (the
test suite) hands back naive ones. Any comparison, subtraction, or window
arithmetic that mixes a stored timestamp with a request payload therefore works
in production and raises ``TypeError: can't compare offset-naive and
offset-aware datetimes`` under test — or worse, silently misorders windows.

Every module that does timestamp arithmetic uses these helpers rather than a
local copy, so the normalization cannot be forgotten in one place and not
another.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    """Current time, always timezone-aware (UTC)."""
    return datetime.now(timezone.utc)


def ensure_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Return ``value`` as timezone-aware UTC, or ``None`` if it is ``None``.

    A naive datetime is *interpreted* as UTC rather than converted: ARGUS stores
    UTC everywhere, so a naive value is a storage-format difference, not a
    different instant.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def ensure_utc_or_now(value: Optional[datetime]) -> datetime:
    """Like :func:`ensure_utc`, but substitutes "now" for a missing value.

    For code paths that need a concrete instant to compare against — never for
    persisting a timestamp the caller did not supply.
    """
    return ensure_utc(value) or utcnow()
