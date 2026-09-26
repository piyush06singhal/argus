"""ARGUS sweep leadership (hardening — lets more than one worker run).

The problem this closes: every background sweep starts in the process lifespan,
so N worker processes run every sweep N times. A per-project write lock made that
**safe** — no deadlock, no duplicate incidents — but "safe duplication" is still
wasted work, and it is why the documented topology had to stop at *one* worker.

The fix is the standard one for scheduled work with no scheduler: a **lease**.
Before a pass, a process asks PostgreSQL for an advisory lock named after the
sweep. Exactly one holder wins; the others skip the tick and try again next
interval. Nothing is queued, nothing is retried, and no pass is ever *split* —
the winner runs the whole sweep.

Three properties are deliberate:

* **The lock is held on its own connection, for the pass only.** Advisory locks
  are session-scoped, so the lease keeps one connection open for the duration and
  releases it in ``finally``. The winner's other sessions (the per-project ones
  inside the sweep) are untouched.
* **A crashed holder cannot wedge the fleet.** PostgreSQL releases an advisory
  lock when the session ends, so a killed worker frees the lease immediately —
  there is no expiry to tune and no stale-lock reap. That is the biggest reason
  to prefer an advisory lock over a ``leader`` row with a heartbeat.
* **A single-process deployment loses nothing.** With one process the lease is
  always granted; on SQLite (the unit suite) there is nothing to coordinate, so
  the lease is a documented no-op and the proof lives in the PostgreSQL-gated
  tests, exactly as with :mod:`app.services.project_lock`.

Operator-triggered work is unaffected: this guards the *scheduler*
(``sweep_forever``), not the pass functions. An explicit
``POST /projects/{id}/anomalies/detect`` still runs when it is asked to.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings

logger = logging.getLogger(__name__)

__all__ = ["sweep_lease", "advisory_key", "SWEEP_NAMES"]

#: Every sweep that must run exactly once per interval across the fleet. Listed
#: explicitly so a new sweep that forgets to take a lease is visible in review.
SWEEP_NAMES = (
    "anomaly",
    "reproduction",
    "code",
    "fix",
    "reliability",
    "remediation",
    "learning",
    "platform",
)


def advisory_key(name: str) -> int:
    """A stable signed 64-bit key for a sweep name.

    Derived in Python rather than with PostgreSQL's ``hashtext`` on purpose:
    ``hashtext`` is undocumented and may change between major versions, and a
    changed key would silently hand two different sweeps the same lock (or none).
    The namespace prefix keeps these keys clear of any other advisory lock a
    future feature might take on the same database.
    """
    digest = hashlib.blake2b(f"argus:sweep:{name}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _supports_advisory_locks(
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Whether the configured database can express an advisory lock at all."""
    bind = session_factory.kw.get("bind")
    dialect = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect is not None:
        return dialect == "postgresql"
    return str(get_settings().DATABASE_URL).startswith("postgresql")


@asynccontextmanager
async def sweep_lease(
    session_factory: async_sessionmaker[AsyncSession],
    name: str,
    *,
    enabled: bool = True,
) -> AsyncIterator[bool]:
    """Yield ``True`` when this process may run the ``name`` sweep now.

    Yields ``False`` when another process holds the lease: the caller skips the
    tick quietly, because that is the normal, intended outcome in a fleet of
    workers rather than an error.
    """
    if not enabled or not _supports_advisory_locks(session_factory):
        # Single process, or a dialect that has no advisory locks (SQLite): there
        # is nothing to coordinate, so the pass runs. Not an error path.
        yield True
        return

    key = advisory_key(name)
    try:
        async with session_factory() as lease_session:
            acquired = await lease_session.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            )
            if not acquired:
                yield False
                return
            try:
                yield True
            finally:
                # Released explicitly so the connection returns to the pool free
                # of the lock; a process that dies before this point is still
                # covered, because the session ends with it.
                try:
                    await lease_session.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                    )
                    await lease_session.commit()
                except Exception as exc:  # pragma: no cover - shutdown path
                    logger.warning(
                        "Could not release the %s sweep lease cleanly: %s", name, exc
                    )
    except Exception as exc:
        # A lease that cannot be evaluated must not disable the platform: fall
        # back to running the pass, which the per-project lock still keeps safe.
        logger.warning(
            "Sweep lease unavailable for %s (%s); running the pass unconditionally",
            name,
            exc,
        )
        yield True
