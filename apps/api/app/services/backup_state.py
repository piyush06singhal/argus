"""Read-side view of backup health (hardening W10).

One question, asked by the metrics endpoint and by nothing else: *how old is the
newest recovery point, and has a restore of it ever been rehearsed?*

Two decisions worth stating, because both are easy to get subtly wrong:

* **Freshness is measured from when the dump was taken, not when it finished.**
  The recovery point objective is about the data captured, so a three-hour dump
  that finished recently still leaves a three-hour-old recovery point. Using
  ``finished_at`` would make a slow dump look fresher than it is.
* **Verification is required to count.** A row is only evidence of a recovery
  point if the archive was fully readable; an unverified row counts as a
  *failed* attempt for freshness purposes, because trusting it would be trusting
  a file nobody has read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.backup import BackupKind, BackupRun, BackupRunStatus


@dataclass(frozen=True)
class BackupState:
    """What the platform knows about its own recoverability."""

    last_success_at: Optional[datetime] = None
    last_drill_at: Optional[datetime] = None
    last_size_bytes: Optional[int] = None
    last_duration_seconds: Optional[float] = None
    #: Outcomes in the window, keyed by status. A ``FAILED`` count that keeps
    #: climbing while ``last_success_at`` stands still is the pattern that says
    #: "the schedule is firing and cannot succeed".
    runs_by_status: dict[str, int] = field(default_factory=dict)

    def age_seconds(self, now: datetime) -> Optional[float]:
        """Seconds since the recovery point, or ``None`` if there never was one."""
        if self.last_success_at is None:
            return None
        return max(0.0, (now - self.last_success_at).total_seconds())

    @property
    def has_ever_succeeded(self) -> bool:
        return self.last_success_at is not None


async def _newest(
    session: AsyncSession,
    *,
    kind: BackupKind,
) -> Optional[BackupRun]:
    """The newest *verified, successful* run of ``kind``, or ``None``."""
    return (
        await session.execute(
            select(BackupRun)
            .where(
                BackupRun.kind == kind,
                BackupRun.status == BackupRunStatus.SUCCEEDED,
                BackupRun.verified.is_(True),
            )
            .order_by(BackupRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def backup_state(
    session: AsyncSession,
    *,
    now: Optional[datetime] = None,
    window_hours: int = 24,
) -> BackupState:
    """Assemble the current backup picture in four queries.

    Deliberately not one clever query: each of these answers a different
    question, and a joined version would make "no row" and "null column"
    indistinguishable — which is exactly the difference between "no backup has
    ever run" and "the last backup failed".
    """
    moment = now or datetime.now(tz=timezone.utc)
    since = moment - timedelta(hours=window_hours)

    full = await _newest(session, kind=BackupKind.FULL)
    drill = await _newest(session, kind=BackupKind.DRILL)

    status_rows = (
        await session.execute(
            select(BackupRun.status, func.count(BackupRun.id))
            .where(BackupRun.started_at >= since)
            .group_by(BackupRun.status)
        )
    ).all()

    return BackupState(
        last_success_at=full.started_at if full else None,
        last_drill_at=drill.started_at if drill else None,
        last_size_bytes=full.size_bytes if full else None,
        last_duration_seconds=full.duration_seconds if full else None,
        runs_by_status={
            getattr(status, "value", str(status)): int(count)
            for status, count in status_rows
        },
    )


__all__ = ["BackupState", "backup_state"]
