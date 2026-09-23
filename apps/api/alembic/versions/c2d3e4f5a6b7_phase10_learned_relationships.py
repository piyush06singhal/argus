"""Phase 10: learned reliability relationships (knowledge-graph integration)

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-09-23 10:00:00.000000

Creates ``intelligence_relationships`` and its three enum types.

Why a new table instead of rows in ``graph_edges``:

That table answers "how is this system wired?" with ``DEPENDS_ON``, ``CALLS`` and
``DEPLOYS``, sourced from configuration, traces and deployments. This one answers
"what has history observed travelling between these two components?" — derived
from completed episodes, with a sample count attached. Writing a learned
co-failure into ``graph_edges`` would make an observation indistinguishable from a
declared dependency, and every consumer that trusts the graph (impact analysis,
dependency analyzer, incident context) would start reading a statistic as
architecture. The two domains stay separate and are joined explicitly, by
component, when a view wants both.

Two constraints carry the semantics:

* **One row per (project, environment, source, target, kind).** The unique index
  uses a ``COALESCE`` sentinel for the nullable environment, the same trick
  ``graph_edges`` uses, because PostgreSQL treats NULLs as distinct and would
  otherwise accept duplicate edges for the unspecified environment (SQLite, used
  by the test suite, behaves the same way).
* **Direction is a column, not a convention.** ``directed`` is false for
  ``SHARED_FAILURE``, so a co-occurrence can never be drawn as an arrow by a
  reader that only has the row.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Enum types created here, with their members: creation and teardown share one
#: source of truth so they cannot drift.
_NEW_ENUMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "intelligence_relationship_kind",
        (
            "FAILURE_PROPAGATION",
            "SHARED_FAILURE",
            "DEPENDENCY_DEGRADATION",
            "REMEDIATION_INFLUENCE",
        ),
    ),
    (
        "intelligence_relationship_status",
        ("ACTIVE", "STALE", "SUPERSEDED"),
    ),
    (
        "intelligence_confidence",
        ("UNKNOWN", "LOW", "MEDIUM", "HIGH"),
    ),
)


def _enum(name: str) -> postgresql.ENUM:
    """The column type for one of the types above.

    ``create_type=False``: types are created once up front, matching the Phase
    6–10 migrations. ``intelligence_confidence`` is shared with
    ``reliability_knowledge`` and already exists after ``b1c2d3e4f5a6``;
    ``checkfirst=True`` makes the create a no-op for it.
    """
    return postgresql.ENUM(name=name, create_type=False)


def _create_enum_types() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for name, values in _NEW_ENUMS:
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)


def _drop_enum_types() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for name, _values in reversed(_NEW_ENUMS):
        #: ``intelligence_confidence`` belongs to ``reliability_knowledge`` too:
        #: dropping it here would break the table this revision did not create,
        #: and dropping the table (below) does not need the type gone.
        if name == "intelligence_confidence":
            continue
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)


def upgrade() -> None:
    _create_enum_types()

    op.create_table(
        "intelligence_relationships",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "environment_id",
            postgresql.UUID(),
            sa.ForeignKey("environments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        #: CASCADE, not SET NULL: an edge with one missing end is not a weaker
        #: observation, it is an unreadable row.
        sa.Column(
            "source_component_id",
            postgresql.UUID(),
            sa.ForeignKey("system_components.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_component_id",
            postgresql.UUID(),
            sa.ForeignKey("system_components.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", _enum("intelligence_relationship_kind"), nullable=False),
        sa.Column(
            "directed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "status",
            _enum("intelligence_relationship_status"),
            nullable=False,
            server_default=sa.text("'ACTIVE'"),
        ),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("supporting_count", sa.Integer(), nullable=True),
        sa.Column(
            "confidence",
            _enum("intelligence_confidence"),
            nullable=False,
            server_default=sa.text("'UNKNOWN'"),
        ),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("limitations", postgresql.JSONB(), nullable=True),
        sa.Column(
            "provenance",
            _enum("intelligence_provenance"),
            nullable=False,
            server_default=sa.text("'OBSERVABILITY'"),
        ),
        sa.Column("algorithm", sa.String(80), nullable=False),
        sa.Column("algorithm_version", sa.String(40), nullable=False),
        sa.Column("feature_schema_version", sa.String(40), nullable=False),
        sa.Column("coverage_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("coverage_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("learning_run_id", postgresql.UUID(), nullable=True),
        sa.Index(
            "uq_intelligence_relationships_key",
            "project_id",
            "source_component_id",
            "target_component_id",
            "kind",
            sa.text("COALESCE(environment_id, '00000000-0000-0000-0000-000000000000')"),
            unique=True,
        ),
    )

    op.create_index(
        "ix_intelligence_relationships_project_id",
        "intelligence_relationships",
        ["project_id"],
    )
    op.create_index(
        "ix_intelligence_relationships_environment_id",
        "intelligence_relationships",
        ["environment_id"],
    )
    op.create_index(
        "ix_intelligence_relationships_source_component_id",
        "intelligence_relationships",
        ["source_component_id"],
    )
    op.create_index(
        "ix_intelligence_relationships_target_component_id",
        "intelligence_relationships",
        ["target_component_id"],
    )
    op.create_index(
        "ix_intelligence_relationships_kind",
        "intelligence_relationships",
        ["kind"],
    )
    op.create_index(
        "ix_intelligence_relationships_status",
        "intelligence_relationships",
        ["status"],
    )
    op.create_index(
        "ix_intelligence_relationships_last_seen_at",
        "intelligence_relationships",
        ["last_seen_at"],
    )
    op.create_index(
        "ix_intelligence_relationships_provenance",
        "intelligence_relationships",
        ["provenance"],
    )
    op.create_index(
        "ix_intelligence_relationships_learning_run_id",
        "intelligence_relationships",
        ["learning_run_id"],
    )
    op.create_index(
        "ix_intelligence_relationships_source",
        "intelligence_relationships",
        ["project_id", "source_component_id", "kind"],
    )
    op.create_index(
        "ix_intelligence_relationships_target",
        "intelligence_relationships",
        ["project_id", "target_component_id", "kind"],
    )


    # ------------------------------------------------------- run accounting
    #: The run ledger reports what the relationship stage produced, so "nothing
    #: was learned" is distinguishable from "learning did not run". Added here
    #: rather than to ``b1c2d3e4f5a6`` so that revision stays exactly as it was
    #: for anyone who has already applied it.
    op.add_column(
        "intelligence_learning_runs",
        sa.Column(
            "relationships_created",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "intelligence_learning_runs",
        sa.Column(
            "relationships_updated",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("intelligence_learning_runs", "relationships_updated")
    op.drop_column("intelligence_learning_runs", "relationships_created")
    op.drop_table("intelligence_relationships")
    _drop_enum_types()
