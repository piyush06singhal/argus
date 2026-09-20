"""Phase 2: cascade deletes for environment-scoped references

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-09-19 09:30:00.000000

Same argument as b7c8d9e0f1a2, one level up the ownership tree: evidence and
graph rows tied to an environment of a deleted project cannot outlive the
environment. These FKs were all NO ACTION, so deleting any project that had
environments with data failed with FK violations.

Also cascades the canonical rows themselves (traces, spans, log_records,
metric_records, observability_events) from projects, as a defense in depth
for direct project deletes that bypass ORM relationship cascades.
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text as sa_text


# revision identifiers, used by Alembic.
revision: str = "c8d9e0f1a2b3"
down_revision: Union[str, None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ENVIRONMENT_CASCADE = [
    ("traces", "environment_id"),
    ("log_records", "environment_id"),
    ("metric_records", "environment_id"),
    ("observability_events", "environment_id"),
    ("deployment_events", "environment_id"),
    ("configuration_change_events", "environment_id"),
    ("health_check_events", "environment_id"),
    ("observability_sources", "environment_id"),
    ("service_endpoints", "environment_id"),
    ("graph_nodes", "environment_id"),
    ("graph_edges", "environment_id"),
    ("graph_snapshots", "environment_id"),
    ("graph_discovery_records", "environment_id"),
    ("graph_reconciliation_runs", "environment_id"),
    ("graph_data_quality_records", "environment_id"),
]

# Evidence rows reference projects directly (project_id NOT NULL). They are
# normally removed via ORM cascades, but a plain DELETE FROM projects must
# not be blocked by them.
_PROJECT_CASCADE = [
    ("traces", "project_id"),
    ("spans", "project_id"),
    ("log_records", "project_id"),
    ("metric_records", "project_id"),
    ("observability_events", "project_id"),
    ("incidents", "project_id"),
    ("deployment_events", "project_id"),
    ("configuration_change_events", "project_id"),
    ("health_check_events", "project_id"),
    ("observability_sources", "project_id"),
    ("ingestion_failures", "project_id"),
    ("service_endpoints", "project_id"),
    ("component_owners", "project_id"),
    ("graph_nodes", "project_id"),
    ("graph_edges", "project_id"),
    ("graph_node_aliases", "project_id"),
    ("graph_snapshots", "project_id"),
    ("graph_discovery_records", "project_id"),
    ("graph_reconciliation_runs", "project_id"),
    ("graph_data_quality_records", "project_id"),
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


def _drop_cascade(pairs, ref_table: str) -> None:
    for table, column in pairs:
        for name in _constraints_for(table, column):
            op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(None, table, ref_table, [column], ["id"])


def upgrade() -> None:
    _apply_cascade(_ENVIRONMENT_CASCADE, "environments")
    _apply_cascade(_PROJECT_CASCADE, "projects")


def downgrade() -> None:
    _drop_cascade(_PROJECT_CASCADE, "projects")
    _drop_cascade(_ENVIRONMENT_CASCADE, "environments")
