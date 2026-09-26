"""Hardening W1: authentication, authorization and ingestion trust

Revision ID: a1b2c3d4e5f6
Revises: fa1b2c3d4e5
Create Date: 2026-09-23 10:00:00.000000

Creates the auth domain:

* ``api_tokens``            — bearer tokens (hash-only storage, roles, expiry)
* ``api_token_projects``    — per-project grants for non-admin tokens
* ``authentication_audit``  — append-only authentication events

And extends ``observability_sources`` with per-source ingest-token hashes so
OTLP/webhook ingestion can be authenticated (raw tokens shown once at
creation, exactly like API tokens).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
#
#: NOTE: this revision was originally given the id ``a1b2c3d4e5f6``, which is
#: also the id of the Phase 1 ingestion migration. Two revisions sharing an id
#: does not fail at import time — it fails at *deployment* time, as "cycle
#: detected in revisions", and the API container then never boots. The id is
#: therefore unique, and ``tests/test_migrations_graph.py`` now asserts that
#: property (unique ids, one head, no cycles) so it cannot come back.
revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, None] = "fa1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON_TYPE = sa.JSON().with_variant(
    sa.dialects.postgresql.JSONB(), "postgresql"  # type: ignore[attr-defined]
)

#: The id type used by every table in ARGUS: native ``uuid`` on PostgreSQL,
#: ``CHAR(32)`` elsewhere. It is spelled ``sa.UUID()`` rather than
#: ``sa.CHAR(32)`` for a specific reason: ``projects.id`` is a real ``uuid``
#: column, and a ``CHAR(32)`` foreign key pointing at it is rejected by
#: PostgreSQL with "foreign key ... cannot be implemented: Key columns
#: \"project_id\" and \"id\" are of incompatible types: character and uuid".
#: SQLite accepts it either way, so this failure is invisible to the test suite
#: and appears only when the migration runs against the real database.
_ID_TYPE = sa.UUID()


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", _ID_TYPE, primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column(
            "role",
            sa.Enum("ADMIN", "OPERATOR", "VIEWER", name="tokenrole"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "REVOKED", "EXPIRED", name="tokenstatus"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        #: ``BaseModel`` records these with ``server_default=func.now()``, so
        #: the DDL must carry the default. Without it the ORM omits the columns
        #: on INSERT (as it is entitled to) and PostgreSQL rejects the row with
        #: "null value in column created_at violates not-null constraint" the
        #: first time the bootstrap token is minted.
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
    )
    op.create_index("ix_api_tokens_token_hash", "api_tokens", ["token_hash"], unique=True)
    op.create_index("ix_api_tokens_status", "api_tokens", ["status"])

    op.create_table(
        "api_token_projects",
        sa.Column("id", _ID_TYPE, primary_key=True),
        sa.Column(
            "token_id",
            _ID_TYPE,
            sa.ForeignKey("api_tokens.id"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            _ID_TYPE,
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        #: ``BaseModel`` records these with ``server_default=func.now()``, so
        #: the DDL must carry the default. Without it the ORM omits the columns
        #: on INSERT (as it is entitled to) and PostgreSQL rejects the row with
        #: "null value in column created_at violates not-null constraint" the
        #: first time the bootstrap token is minted.
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
    )
    op.create_index(
        "ix_api_token_projects_token_id", "api_token_projects", ["token_id"]
    )
    op.create_index(
        "ix_api_token_projects_project_id", "api_token_projects", ["project_id"]
    )
    op.create_index(
        "uq_api_token_projects_token_project",
        "api_token_projects",
        ["token_id", "project_id"],
        unique=True,
    )

    op.create_table(
        "authentication_audit",
        sa.Column("id", _ID_TYPE, primary_key=True),
        sa.Column("token_id", _ID_TYPE, nullable=True),
        sa.Column("token_hash_prefix", sa.String(16), nullable=True),
        sa.Column(
            "action",
            sa.Enum("CREATED", "USED", "FAILED", "REVOKED", name="authauditaction"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("request_path", sa.String(512), nullable=True),
        sa.Column("client_ip", sa.String(64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        #: ``BaseModel`` records these with ``server_default=func.now()``, so
        #: the DDL must carry the default. Without it the ORM omits the columns
        #: on INSERT (as it is entitled to) and PostgreSQL rejects the row with
        #: "null value in column created_at violates not-null constraint" the
        #: first time the bootstrap token is minted.
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
    )
    op.create_index(
        "ix_authentication_audit_token_id", "authentication_audit", ["token_id"]
    )
    op.create_index(
        "ix_authentication_audit_token_action",
        "authentication_audit",
        ["token_id", "action"],
    )
    op.create_index(
        "ix_authentication_audit_occurred", "authentication_audit", ["occurred_at"]
    )

    op.add_column(
        "observability_sources",
        sa.Column("ingest_token_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "observability_sources",
        sa.Column("ingest_token_rotated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("observability_sources", "ingest_token_rotated_at")
    op.drop_column("observability_sources", "ingest_token_hash")
    op.drop_index("ix_authentication_audit_occurred", table_name="authentication_audit")
    op.drop_index(
        "ix_authentication_audit_token_action", table_name="authentication_audit"
    )
    op.drop_index("ix_authentication_audit_token_id", table_name="authentication_audit")
    op.drop_table("authentication_audit")
    op.drop_index(
        "uq_api_token_projects_token_project", table_name="api_token_projects"
    )
    op.drop_index("ix_api_token_projects_project_id", table_name="api_token_projects")
    op.drop_index("ix_api_token_projects_token_id", table_name="api_token_projects")
    op.drop_table("api_token_projects")
    op.drop_index("ix_api_tokens_status", table_name="api_tokens")
    op.drop_index("ix_api_tokens_token_hash", table_name="api_tokens")
    op.drop_table("api_tokens")
    sa.Enum(name="authauditaction").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="tokenstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="tokenrole").drop(op.get_bind(), checkfirst=True)
