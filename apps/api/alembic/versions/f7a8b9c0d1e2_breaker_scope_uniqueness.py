"""Phase 9: one circuit breaker per scope

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-09-22 11:30:00.000000

§27 promises that a repeatedly failing action type stops being attempted. The
breaker is read as a single row — "is it open?" — so if the table ever held two
rows for one ``(project, environment, action_type)`` scope, the reader could
pick the ``CLOSED`` one and the open breaker would be silently ignored. Two such
rows were reachable: nothing enforced the scope key, and a policy evaluation
racing another could insert a second.

This revision makes the scope key the database's business:

1. collapse existing duplicates to one row per scope, keeping the most
   restrictive one (an ``OPEN``/``HALF_OPEN`` breaker wins over a ``CLOSED``
   one, then the most recently updated) so no live breaker is discarded;
2. add a unique index over the scope. It is ``NULLS NOT DISTINCT`` because a
   project-wide breaker has a NULL ``environment_id``, and under the default SQL
   semantics two NULLs are distinct — which is exactly the duplicate that has to
   be impossible.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "remediation_circuit_breakers"
_INDEX = "uq_remediation_circuit_breakers_scope"

#: Postgres' default for an all-NULL uuid; only used to partition rows that have
#: no environment (a project-wide breaker).
_NO_ENVIRONMENT = "00000000-0000-0000-0000-000000000000"

_DEDUPE = f"""
WITH ranked AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY
                project_id,
                COALESCE(environment_id, '{_NO_ENVIRONMENT}'::uuid),
                action_type
            ORDER BY
                (state <> 'CLOSED') DESC,
                updated_at DESC NULLS LAST,
                created_at DESC NULLS LAST,
                id DESC
        ) AS rank
    FROM {_TABLE}
)
DELETE FROM {_TABLE}
WHERE id IN (SELECT id FROM ranked WHERE rank > 1);
"""


def upgrade() -> None:
    op.execute(_DEDUPE)
    op.create_index(
        _INDEX,
        _TABLE,
        ["project_id", "environment_id", "action_type"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
