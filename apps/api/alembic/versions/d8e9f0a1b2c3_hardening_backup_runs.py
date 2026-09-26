"""Hardening W10: backup run history

Revision ID: d8e9f0a1b2c3
Revises: b2c3d4e5f6a7
Create Date: 2026-09-25 12:00:00.000000

Creates ``backup_runs``: one row per dump and per restore rehearsal, written by
the backup scheduler and read by the API to export backup freshness and to alert
when the newest recovery point is too old.

The table is not derived data in the usual sense — it is the platform's own
evidence that it can recover — so it is deliberately *not* pruned with
telemetry: a year of rows is a few hundred kilobytes, and the history of a
failing schedule is exactly what an operator needs when asking "how long has
this been broken?".
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Spelled ``sa.UUID()`` for the same reason as every other ARGUS table: SQLite
#: stores it as CHAR(32) while PostgreSQL gets a native uuid, and a mismatched
#: foreign key is rejected only by the real database.
_ID_TYPE = sa.UUID()


def upgrade() -> None:
    op.create_table(
        "backup_runs",
        sa.Column("id", _ID_TYPE, primary_key=True),
        sa.Column(
            "kind",
            sa.Enum("FULL", "DRILL", name="backupkind"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("RUNNING", "SUCCEEDED", "FAILED", name="backuprunstatus"),
            nullable=False,
        ),
        sa.Column(
            "trigger",
            sa.Enum("SCHEDULED", "MANUAL", name="backuptrigger"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("dump_path", sa.String(length=1024), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("table_count", sa.Integer(), nullable=True),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("run_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    #: The freshness query is "newest SUCCEEDED row of kind X", evaluated on
    #: every scrape. Without these the endpoint scans the table on each of them.
    op.create_index(
        "ix_backup_runs_kind_status_started",
        "backup_runs",
        ["kind", "status", "started_at"],
    )
    op.create_index("ix_backup_runs_started_at", "backup_runs", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_backup_runs_started_at", table_name="backup_runs")
    op.drop_index("ix_backup_runs_kind_status_started", table_name="backup_runs")
    op.drop_table("backup_runs")
    sa.Enum(name="backupkind").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="backuprunstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="backuptrigger").drop(op.get_bind(), checkfirst=True)
