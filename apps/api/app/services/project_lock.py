"""ARGUS per-project write mutex (Phase 3 §10, §25; Phase 11 §10).

Detection and correlation write *related rows in more than one table* in a single
pass: a fingerprint registry row, an anomaly, evidence, an incident. Three things
run that pass: the ingestion worker (so newly arrived telemetry is detected
promptly), the scheduled detection sweep, and an operator's explicit
``POST /projects/{id}/anomalies/detect``.

When two of those run for the same project at the same instant, their writes
interleave. PostgreSQL caught the result as a **genuine deadlock** between a
fingerprint ``UPDATE`` and an anomaly ``UPDATE`` — each transaction holding a row
the other needed — and one of the two passes lost its whole transaction. A
duplicate-key violation on the fingerprint registry was the other symptom, now
handled separately by an atomic claim.

The fix is the standard one: serialise the writers. The project row is the natural
mutex, because every one of those writes belongs to exactly one project, and it is
taken with ``SELECT … FOR UPDATE`` so the lock is held for exactly as long as the
transaction doing the work — no process-local state, no lock file, and nothing to
clean up if a worker dies.

Callers must therefore take the lock **before** the first write and let the
transaction end normally. Passes that touch several projects (the sweeps) must
iterate them in a deterministic order, or two sweeps could take the same two
locks in opposite orders and deadlock on the mutex itself; both sweeps order by
``created_at`` for exactly that reason.

Dialect note: SQLite — which the unit suite runs on — has no row locks, and its
SQLAlchemy compiler emits nothing for ``FOR UPDATE``. The tests therefore call the
same helper on the same code path without pretending to test concurrency, and the
honest claim in the report is that the deadlock was reproduced and fixed against
PostgreSQL.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import SoftwareProject

__all__ = ["lock_project"]


async def lock_project(
    session: AsyncSession, *, project_id: Optional[uuid.UUID]
) -> None:
    """Take the project's write lock for the rest of this transaction.

    A no-op when ``project_id`` is ``None`` (a project-wide pass with no scope
    has nothing to serialise against) and on dialects without row locking.
    """
    if project_id is None:
        return
    #: ``FOR UPDATE`` is emitted by PostgreSQL and by nothing else — SQLite's
    #: compiler renders it as an empty string — so this stays one code path for
    #: production and for the suite.
    await session.execute(
        select(SoftwareProject.id)
        .where(SoftwareProject.id == project_id)
        .with_for_update()
    )
