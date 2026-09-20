"""Phase 2: cascade deletes for component-scoped references

Revision ID: b7c8d9e0f1a2
Revises: f6d7e8c9b4a2
Create Date: 2026-09-19 09:00:00.000000

A project delete cascades to its components, but the nullable
component-scoped FKs (spans, logs, metrics, observability_events,
deployment_events, configuration_change_events) had no ON DELETE action:
SQLAlchemy NULLs them first, then PostgreSQL rejects the component delete
with a FK violation. These references are evidence of a component; when the
component is deleted they have no meaning, so they cascade.

Also drops component_dependencies/component_owners/service_endpoints
restrictions from components to projects — those are handled by ORM-level
cascade ("all, delete-orphan") so a plain ON DELETE is never reached.

"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text as sa_text


# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, None] = "f6d7e8c9b4a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column) pairs with component references that must die with the
# component instead of blocking the delete. NULLable evidence rows would
# otherwise be NULLed by the ORM and then rejected by the FK; NOT NULL owned
# rows (endpoints, owners, health checks) simply have no ON DELETE action.
_COMPONENT_CASCADE = [
    ("spans", "component_id"),
    ("log_records", "component_id"),
    ("metric_records", "component_id"),
    ("observability_events", "component_id"),
    ("deployment_events", "component_id"),
    ("configuration_change_events", "component_id"),
    ("health_check_events", "component_id"),
    ("service_endpoints", "component_id"),
    ("component_owners", "component_id"),
]


def _constraints_for(table: str, column: str):
    conn = op.get_bind()
    rows = conn.execute(
        sa_text(
            "SELECT conname FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace "
            "WHERE con.contype = 'f' AND rel.relname = :table "
            "AND nsp.nspname = 'public' "
            "AND (SELECT a.attname FROM pg_attribute a "
            "     WHERE a.attrelid = con.conrelid AND a.attnum = ANY(con.conkey) "
            "     ORDER BY a.attnum LIMIT 1) = :column"
        ),
        {"table": table, "column": column},
    ).fetchall()
    return [r[0] for r in rows]


def _apply_cascade(pairs, ref_table: str) -> None:
    for table, column in pairs:
        for name in _constraints_for(table, column):
            op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            None,
            table,
            ref_table,
            [column],
            ["id"],
            ondelete="CASCADE",
        )


def upgrade() -> None:
    _apply_cascade(_COMPONENT_CASCADE, "system_components")


def downgrade() -> None:
    for table, column in _COMPONENT_CASCADE:
        for name in _constraints_for(table, column):
            op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            None,
            table,
            "system_components",
            [column],
            ["id"],
        )
