"""Phase hardening W5: the second-audit-pass data-quality kinds

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-24 09:00:00.000000

Adds five members to the ``data_quality_issue_kind`` PostgreSQL enum:

* ``MISSING_TIMESTAMP``     — a terminal state with no recorded moment
* ``MISSING_PROVENANCE``    — a derived row that cites no source
* ``IMPOSSIBLE_TRANSITION`` — a row whose own fields cannot coexist
* ``MISSING_AUDIT_EVENT``   — a state change nothing recorded
* ``CORRUPTED_ARTIFACT``    — stored bytes that no longer match their hash

There is deliberately no ``DUPLICATE_INCIDENT``: the partial unique index on
``incidents (project_id, fingerprint)`` for unresolved statuses already makes
two live duplicates impossible to insert, and the implementation proved it by
failing to create one.

PostgreSQL enforces enum membership at the type level, so the values must exist
*before* the code that writes them runs — migration order matters here, not
merely table shape. ``ADD VALUE`` commits the new label to the type; it cannot
be run inside a transaction block on PostgreSQL < 12, and this project pins
PostgreSQL 16 (``postgres:16-alpine``), where it can.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
#: Follows the hardening auth revision (``c1d2e3f4a5b6``), which follows the
#: Phase 11 head. See the note in that file: the id was ``a1b2c3d4e5f6`` until it
#: turned out to collide with the Phase 1 migration and broke deployment.
down_revision: Union[str, None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: In the order the checks run, so a reader can follow the enum beside the code.
NEW_KINDS = (
    "MISSING_TIMESTAMP",
    "MISSING_PROVENANCE",
    "IMPOSSIBLE_TRANSITION",
    "MISSING_AUDIT_EVENT",
    "CORRUPTED_ARTIFACT",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":  # pragma: no cover - SQLite/batch
        # SQLite stores enums as VARCHAR and validates membership in Python, so
        # there is no type to extend. Tests build the schema from metadata.
        return
    for kind in NEW_KINDS:
        #: ``IF NOT EXISTS`` keeps the migration idempotent: on a database where
        #: an operator already added a label by hand, this is a no-op rather than
        #: a failed upgrade that blocks the rest of the release.
        op.execute(f"ALTER TYPE data_quality_issue_kind ADD VALUE IF NOT EXISTS '{kind}'")


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum type without rewriting the
    # type and every dependent column. An added label is therefore permanent in
    # practice, and pretending otherwise would produce a downgrade that silently
    # corrupts rows still holding the value. Stated here rather than attempted.
    pass
