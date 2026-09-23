"""Phase 3: one unresolved incident per correlation fingerprint

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-09-23 10:00:00.000000

Correlation can run concurrently from several places (the detect endpoint, the
async ingest hook, the sweep). Each pass looks up an incident by fingerprint and
inserts one when it finds nothing, and two passes in flight at the same time
cannot see each other's uncommitted insert — so both created an incident for the
same fingerprint. The anomalies were then linked by whichever pass committed
last, leaving a live incident holding no evidence (and, downstream, a lifecycle
that answered 409 on transitions against it).

This partial unique index makes the §25 invariant the database's job for the
state that must never exist twice — an *unresolved* incident. RESOLVED and
CLOSED rows are deliberately outside it: they are retired history that
correlation reopens rather than duplicates, and a reported incident may carry
whatever fingerprint its filer chose.

Live validation caught this (Phase 10 smoke gate); the model tests pin the
refusal *and* the two allowances (a resolved row does not block a recurrence, a
null fingerprint is unconstrained).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "uq_incidents_project_fingerprint_unresolved"
PREDICATE = "fingerprint IS NOT NULL AND status NOT IN ('RESOLVED', 'CLOSED')"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
        ON incidents (project_id, fingerprint)
        WHERE {PREDICATE}
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
