"""Phase 3: incident evidence cascade

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
Create Date: 2026-09-19 10:00:00.000000

``incident_timeline_events.incident_id`` already cascades, but
``incident_evidence.incident_id`` (a Phase 0 table) was ``NO ACTION``. Evidence
is *of* an incident — it cannot outlive it — and the NO ACTION behaviour also
made retention unsafe: deleting an incident past its retention window failed
whenever a dependent evidence row was still inside its own window.

Aligning the FK with the rest of the incident-scoped references (and with the
timeline table) makes incident deletion correct at the database level rather
than relying on an ORM cascade that a raw DELETE never sees.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "e0f1a2b3c4d5"
down_revision: Union[str, None] = "d9e0f1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT = "incident_evidence_incident_id_fkey"


def upgrade() -> None:
    """Make evidence deletion follow its incident."""
    op.drop_constraint(_CONSTRAINT, "incident_evidence", type_="foreignkey")
    op.create_foreign_key(
        _CONSTRAINT,
        "incident_evidence",
        "incidents",
        ["incident_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    """Restore the original NO ACTION constraint."""
    op.drop_constraint(_CONSTRAINT, "incident_evidence", type_="foreignkey")
    op.create_foreign_key(
        _CONSTRAINT,
        "incident_evidence",
        "incidents",
        ["incident_id"],
        ["id"],
    )
