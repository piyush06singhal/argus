"""Phase 4: causal analysis tables

Revision ID: f1a2b3c4d5e6
Revises: e0f1a2b3c4d5
Create Date: 2026-09-20 02:00:00.000000

Creates the causal-analysis domain:

* ``causal_analyses``       — one versioned analysis run per incident
* ``root_cause_candidates`` — hypotheses with score/confidence kept separate
* ``causal_relationships``  — the per-incident causal graph's directed edges
* ``causal_evidence``       — facts bound to candidates and to specific edges

Table order matters: ``causal_evidence`` carries ``relationship_id`` (a fact can
be bound to the specific graph edge it justifies), so relationships are created
before evidence.

Enum types are created once and referenced with ``create_type=False`` (the
Phase 3 pattern) so enums reused across tables cannot hit duplicate ``CREATE
TYPE``. Deletes cascade from the owning project and from an analysis to its
rows; component references are ``SET NULL`` because a candidate must survive
component deletion as history, not vanish with it.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e0f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ANALYSIS_STATUSES = ["PENDING", "RUNNING", "COMPLETED", "FAILED"]
CANDIDATE_TYPES = [
    "DEPLOYMENT",
    "CONFIGURATION_CHANGE",
    "APPLICATION_COMPONENT",
    "DATABASE",
    "EXTERNAL_DEPENDENCY",
    "INFRASTRUCTURE",
    "RESOURCE_EXHAUSTION",
    "DEPENDENCY_FAILURE",
    "DATA_ISSUE",
    "UNKNOWN",
]
CANDIDATE_STATUSES = [
    "HYPOTHESIS",
    "UNDER_EVALUATION",
    "SUPPORTED",
    "WEAKENED",
    "REFUTED",
    "WITHDRAWN",
]
RELATIONSHIP_TYPES = [
    "POSSIBLE_CAUSE",
    "LIKELY_CAUSE",
    "DOWNSTREAM_EFFECT",
    "CONTRIBUTES_TO",
    "BLOCKS",
    "TRIGGERS",
    "AMPLIFIES",
    "CORRELATES_WITH",
]
EVIDENCE_CATEGORIES = [
    "TEMPORAL",
    "TRACE",
    "DEPENDENCY",
    "CHANGE",
    "METRIC",
    "LOG",
    "HEALTH",
    "RESOURCE",
    "CONFIGURATION",
    "DEPLOYMENT",
    "RECOVERY",
    "CONTRADICTING",
]
EVIDENCE_POLARITIES = ["SUPPORTING", "CONTRADICTING", "NEUTRAL"]
CONFIDENCE_LEVELS = ["INSUFFICIENT", "LOW", "MEDIUM", "HIGH"]

#: (name, values) for the new enum types, created once at the top of upgrade().
_NEW_ENUMS = [
    ("analysisstatus", ANALYSIS_STATUSES),
    ("candidatetype", CANDIDATE_TYPES),
    ("candidatestatus", CANDIDATE_STATUSES),
    ("causalrelationshiptype", RELATIONSHIP_TYPES),
    ("causalevidencecategory", EVIDENCE_CATEGORIES),
    ("evidencepolarity", EVIDENCE_POLARITIES),
    ("confidencelevel", CONFIDENCE_LEVELS),
]


def _enum(values: list[str], name: str) -> postgresql.ENUM:
    """Reference an already-created enum type (never re-create it)."""
    return postgresql.ENUM(*values, name=name, create_type=False)


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
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)


def upgrade() -> None:
    _create_enum_types()

    # ---- 1. causal_analyses ------------------------------------------------
    op.create_table(
        "causal_analyses",
        sa.Column("id", postgresql.UUID(), nullable=False),
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
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("environment_id", postgresql.UUID(), nullable=True),
        sa.Column("incident_id", postgresql.UUID(), nullable=False),
        sa.Column("analysis_version", sa.Integer(), nullable=False),
        sa.Column("status", _enum(ANALYSIS_STATUSES, "analysisstatus"), nullable=False),
        sa.Column("trigger", sa.String(length=255), nullable=True),
        sa.Column("requested_by", sa.String(length=255), nullable=True),
        sa.Column("analysis_version_tag", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "overall_confidence",
            _enum(CONFIDENCE_LEVELS, "confidencelevel"),
            nullable=False,
        ),
        sa.Column("primary_candidate_id", postgresql.UUID(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "missing_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "analysis_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["environments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_causal_analyses_incident_version",
        "causal_analyses",
        ["incident_id", "analysis_version"],
        unique=True,
    )
    op.create_index("ix_causal_analyses_project", "causal_analyses", ["project_id"])

    # ---- 2. root_cause_candidates ------------------------------------------
    op.create_table(
        "root_cause_candidates",
        sa.Column("id", postgresql.UUID(), nullable=False),
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
        sa.Column("analysis_id", postgresql.UUID(), nullable=False),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("component_id", postgresql.UUID(), nullable=True),
        sa.Column("event_id", postgresql.UUID(), nullable=True),
        sa.Column("event_kind", sa.String(length=64), nullable=True),
        sa.Column(
            "candidate_type", _enum(CANDIDATE_TYPES, "candidatetype"), nullable=False
        ),
        sa.Column(
            "status", _enum(CANDIDATE_STATUSES, "candidatestatus"), nullable=False
        ),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column(
            "confidence", _enum(CONFIDENCE_LEVELS, "confidencelevel"), nullable=False
        ),
        sa.Column("is_external", sa.Boolean(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supporting_evidence_count", sa.Integer(), nullable=False),
        sa.Column("contradicting_evidence_count", sa.Integer(), nullable=False),
        sa.Column("neutral_evidence_count", sa.Integer(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column(
            "score_breakdown", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "uncertainty", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id"], ["causal_analyses.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rcc_analysis_score", "root_cause_candidates", ["analysis_id", "score"]
    )
    op.create_index("ix_rcc_component", "root_cause_candidates", ["component_id"])

    # ---- 3. causal_relationships (before evidence: evidence references it) --
    op.create_table(
        "causal_relationships",
        sa.Column("id", postgresql.UUID(), nullable=False),
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
        sa.Column("analysis_id", postgresql.UUID(), nullable=False),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column("source_candidate_id", postgresql.UUID(), nullable=False),
        sa.Column("target_candidate_id", postgresql.UUID(), nullable=False),
        sa.Column(
            "relationship_type",
            _enum(RELATIONSHIP_TYPES, "causalrelationshiptype"),
            nullable=False,
        ),
        sa.Column(
            "confidence", _enum(CONFIDENCE_LEVELS, "confidencelevel"), nullable=False
        ),
        sa.Column("supporting_evidence_count", sa.Integer(), nullable=False),
        sa.Column("contradicting_evidence_count", sa.Integer(), nullable=False),
        sa.Column("temporal_alignment_seconds", sa.Integer(), nullable=True),
        sa.Column("structural_support", sa.Integer(), nullable=False),
        sa.Column("observational_support", sa.Integer(), nullable=False),
        sa.Column(
            "contradiction_notes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_id"], ["causal_analyses.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_candidate_id"], ["root_cause_candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["target_candidate_id"], ["root_cause_candidates.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_candidate_id",
            "target_candidate_id",
            "relationship_type",
            name="uq_causal_rel_pair",
        ),
    )
    op.create_index(
        "ix_causal_relationships_analysis_id", "causal_relationships", ["analysis_id"]
    )
    # Type-filtered queries are served by the (source, target, type) unique
    # constraint above; no separate enum-column index is needed.

    # ---- 4. causal_evidence -------------------------------------------------
    op.create_table(
        "causal_evidence",
        sa.Column("id", postgresql.UUID(), nullable=False),
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
        sa.Column("analysis_id", postgresql.UUID(), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(), nullable=False),
        sa.Column("project_id", postgresql.UUID(), nullable=False),
        sa.Column(
            "category",
            _enum(EVIDENCE_CATEGORIES, "causalevidencecategory"),
            nullable=False,
        ),
        sa.Column(
            "polarity", _enum(EVIDENCE_POLARITIES, "evidencepolarity"), nullable=False
        ),
        sa.Column("source_table", sa.String(length=64), nullable=False),
        sa.Column("source_id", postgresql.UUID(), nullable=True),
        sa.Column("incident_evidence_id", postgresql.UUID(), nullable=True),
        sa.Column("relationship_id", postgresql.UUID(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("component_id", postgresql.UUID(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("strength", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_id"], ["causal_analyses.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["root_cause_candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["incident_evidence_id"], ["incident_evidence.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["relationship_id"], ["causal_relationships.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["component_id"], ["system_components.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_causal_evidence_candidate_id", "causal_evidence", ["candidate_id"]
    )
    op.create_index("ix_causal_evidence_polarity", "causal_evidence", ["polarity"])
    op.create_index(
        "ix_causal_evidence_source", "causal_evidence", ["source_table", "source_id"]
    )


def downgrade() -> None:
    op.drop_table("causal_evidence")
    op.drop_table("causal_relationships")
    op.drop_table("root_cause_candidates")
    op.drop_table("causal_analyses")
    _drop_enum_types()
