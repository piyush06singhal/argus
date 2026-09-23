"""Phase 10: defer the reference checks on learned rows

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-09-23 15:00:00.000000

Deleting a project failed once the learning layer had recorded an episode:

    update or delete on table "environments" violates foreign key constraint
    "reliability_experiences_remediation_action_id_fkey" on table
    "reliability_experiences"
    DETAIL: Key (remediation_action_id)=(bc73add1-…) is not present in table
    "remediation_actions".

Nothing was wrong with the data. The failure is an ordering property of
cascading referential actions on one row: deleting an environment cascades to
its ``remediation_actions`` (CASCADE) *and* updates ``reliability_experiences``
to clear ``environment_id`` (SET NULL). PostgreSQL validates **every** foreign key
of a row it updates, not just the column that changed, so that update re-checked
``remediation_action_id`` at a moment when the action it pointed at had already
been deleted by the same statement — a transient inconsistency inside one
statement, reported as a violation that no reader of the data could have
observed.

The fix is to move that check to the end of the transaction:

* the constraint is unchanged — a dangling reference still fails, just at
  ``COMMIT`` rather than mid-cascade;
* by then the sibling SET NULL actions have run, so the row is genuinely
  consistent and the check passes.

Only the ``SET NULL`` references need it. A ``CASCADE`` reference is deleted
rather than updated, so it can never re-check a bystander column, and the
``project_id`` CASCADEs are left exactly as they were.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Every table this phase owns. The reference checks are discovered from the
#: catalog rather than listed by hand: a constraint that exists but is forgotten
#: here is exactly the bug this revision fixes, and a hard-coded list is how it
#: would come back.
_PHASE10_TABLES: tuple[str, ...] = (
    "reliability_experiences",
    "reliability_knowledge",
    "learning_events",
    "intelligence_knowledge_versions",
    "intelligence_learning_runs",
    "intelligence_learning_experiments",
    "intelligence_component_profiles",
    "intelligence_recommendations",
    "intelligence_recommendation_outcomes",
    "intelligence_knowledge_reviews",
    "intelligence_event_hooks",
    "intelligence_relationships",
)


def _set_null_constraints() -> list[tuple[str, str]]:
    """``(table, constraint)`` for every SET NULL reference on our tables.

    ``confdeltype = 'n'`` is PostgreSQL's code for ``ON DELETE SET NULL``.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return []
    rows = bind.execute(
        sa.text(
            "SELECT rel.relname, con.conname "
            "FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace "
            "WHERE con.contype = 'f' "
            "AND con.confdeltype = 'n' "
            "AND nsp.nspname = 'public' "
            "AND rel.relname = ANY(:tables) "
            "ORDER BY rel.relname, con.conname"
        ),
        {"tables": list(_PHASE10_TABLES)},
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def _alter(pairs: Sequence[tuple[str, str]], action: str) -> None:
    if not pairs:
        return
    for table, name in pairs:
        #: Both identifiers come from ``pg_constraint``, never from input.
        op.execute(sa.text(f'ALTER TABLE "{table}" ALTER CONSTRAINT "{name}" {action}'))


def upgrade() -> None:
    _alter(_set_null_constraints(), "DEFERRABLE INITIALLY DEFERRED")


def downgrade() -> None:
    _alter(_set_null_constraints(), "NOT DEFERRABLE INITIALLY IMMEDIATE")
