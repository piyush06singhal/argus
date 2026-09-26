"""ARGUS Backup Run models (hardening W10).

The gap this closes is small and specific, and it is the one that matters most
in an incident: ARGUS could produce a backup (`infrastructure/backup.sh`) but
nothing ran it on a schedule, and nothing recorded whether it had ever worked.
A backup strategy whose freshness lives only in an operator's memory fails
silently and is discovered at the worst possible moment.

So every attempt is a row — including the failures, which are the interesting
ones. From this table the API derives:

* ``argus_backup_last_success_timestamp_seconds`` — how stale the newest
  recovery point is;
* ``argus_backup_last_drill_timestamp_seconds`` — whether a restore has been
  *rehearsed*, which verification alone never proves;
* ``argus_backup_runs_24h_by_status`` — whether the schedule is actually firing.

Rows are written by the backup scheduler, which runs as its own container using
the database's own client image (so ``pg_dump`` always matches the server's major
version) rather than by the API process. That is deliberate: the process that
parses untrusted input does not need a database-dumping binary, and granting it
one would be a privilege increase for no benefit.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class BackupKind(str, enum.Enum):
    """What the run produced.

    ``DRILL`` is not a smaller backup: it restores an existing dump into a
    scratch database and compares counts, which is the only thing that proves
    the archive reproduces the data.
    """

    FULL = "FULL"
    DRILL = "DRILL"


class BackupRunStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class BackupTrigger(str, enum.Enum):
    """Who or what caused the run.

    Kept separate from the outcome because "the schedule stopped firing" and
    "the schedule is firing and failing" need different responses, and the
    difference is invisible in a single status column.
    """

    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"


class BackupRun(BaseModel):
    """One dump or one restore rehearsal."""

    __tablename__ = "backup_runs"

    kind: Mapped[BackupKind] = mapped_column(
        Enum(BackupKind, name="backupkind"), nullable=False, index=True
    )
    status: Mapped[BackupRunStatus] = mapped_column(
        Enum(BackupRunStatus, name="backuprunstatus"), nullable=False, index=True
    )
    trigger: Mapped[BackupTrigger] = mapped_column(
        Enum(BackupTrigger, name="backuptrigger"),
        nullable=False,
        default=BackupTrigger.SCHEDULED,
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_seconds: Mapped[Optional[float]] = mapped_column(nullable=True)

    #: Path as the *backup host* sees it. Not used by the API to read anything —
    #: it is here so an operator can find the file from the row.
    dump_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    #: Proof the archive was fully readable, not merely present. A truncated
    #: dump lists its table of contents happily, so `size_bytes` alone would
    #: record a useless file as a successful backup.
    verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    table_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    row_count: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    #: Why it failed, verbatim. Never summarised into "backup failed": the
    #: difference between "no space left" and "authentication failed" is the
    #: whole value of the row.
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    #: Instance that performed the run, so a fleet can tell which replica's
    #: schedule is firing.
    run_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


__all__ = [
    "BackupKind",
    "BackupRun",
    "BackupRunStatus",
    "BackupTrigger",
]
