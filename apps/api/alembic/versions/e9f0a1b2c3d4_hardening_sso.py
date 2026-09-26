"""Hardening W2: single sign-on (OIDC)

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-09-26 09:00:00.000000

Adds the SSO domain:

* ``external_identities``  — a provisioned person: their provider ``sub``, the
  role and grants their claims last resolved to, and the disable switch that
  revokes every session they hold;
* ``oidc_login_states``    — one row per in-flight authorization-code login,
  consumed exactly once (the PKCE verifier and nonce live here and nowhere
  else);
* ``api_tokens.auth_source`` / ``api_tokens.external_identity_id`` — so a
  session minted by an identity provider is distinguishable from a machine
  token, and can be revoked when the person behind it is disabled.

Two DDL details are load-bearing, and both are recorded because getting them
wrong is invisible until deployment:

1. **``ADD COLUMN`` with an enum does not create the type on PostgreSQL.** The
   ``authsource`` type is therefore created explicitly before the column is
   added; ``tokenrole`` already exists (W1) and is referenced with
   ``create_type=False`` so this migration cannot try to create it twice.
2. **A new ``NOT NULL`` column needs a server default.** Existing rows must
   acquire a value, or the migration fails on any database that already has
   tokens in it — which is every database in production. ``TOKEN`` is also the
   truthful value for every pre-existing row: none of them came from SSO.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

# revision identifiers, used by Alembic.
revision: str = "e9f0a1b2c3d4"
down_revision: Union[str, None] = "d8e9f0a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Native ``uuid`` on PostgreSQL, ``CHAR(32)`` elsewhere — see W1's migration
#: for why a mismatched parent type produces a table PostgreSQL refuses.
_ID_TYPE = sa.UUID()

_JSON_TYPE = sa.JSON().with_variant(pg.JSONB(), "postgresql")


def _enum(name: str, *values: str, create_type: bool) -> sa.types.TypeEngine:
    """A column type for an enum that exists (or is created) on PostgreSQL.

    SQLite has no enum types, so it gets a ``VARCHAR``: the ORM still binds and
    reads the member names, and a fast unit-test database does not have to
    pretend to enforce something it cannot.
    """
    if op.get_bind().dialect.name == "postgresql":
        return pg.ENUM(*values, name=name, create_type=create_type)
    return sa.String(32)


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    if is_postgres:
        #: Owned by this revision. ``checkfirst`` keeps a re-run safe.
        sa.Enum("TOKEN", "OIDC", name="authsource").create(bind, checkfirst=True)

    op.create_table(
        "external_identities",
        sa.Column("id", _ID_TYPE, primary_key=True),
        sa.Column("provider", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column(
            "role",
            _enum("tokenrole", "ADMIN", "OPERATOR", "VIEWER", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "project_ids",
            _JSON_TYPE,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "claims_snapshot",
            _JSON_TYPE,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "email_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("first_login_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "login_count", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("last_login_ip", sa.String(64), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_reason", sa.Text(), nullable=True),
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
        "uq_external_identities_provider_subject",
        "external_identities",
        ["provider", "subject"],
        unique=True,
    )
    op.create_index("ix_external_identities_email", "external_identities", ["email"])

    op.create_table(
        "oidc_login_states",
        sa.Column("id", _ID_TYPE, primary_key=True),
        sa.Column("state", sa.String(128), nullable=False),
        sa.Column("nonce", sa.String(128), nullable=False),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column("redirect_uri", sa.String(512), nullable=False),
        sa.Column("provider", sa.String(255), nullable=False),
        sa.Column("return_to", sa.String(512), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_ip", sa.String(64), nullable=True),
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
        "uq_oidc_login_states_state", "oidc_login_states", ["state"], unique=True
    )
    op.create_index("ix_oidc_login_states_expires", "oidc_login_states", ["expires_at"])

    op.add_column(
        "api_tokens",
        sa.Column(
            "auth_source",
            _enum("authsource", "TOKEN", "OIDC", create_type=False),
            nullable=False,
            server_default=sa.text("'TOKEN'"),
        ),
    )
    op.add_column(
        "api_tokens",
        sa.Column(
            "external_identity_id",
            _ID_TYPE,
            sa.ForeignKey("external_identities.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_api_tokens_external_identity_id", "api_tokens", ["external_identity_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_api_tokens_external_identity_id", table_name="api_tokens")
    op.drop_column("api_tokens", "external_identity_id")
    op.drop_column("api_tokens", "auth_source")
    op.drop_index("ix_oidc_login_states_expires", table_name="oidc_login_states")
    op.drop_index("uq_oidc_login_states_state", table_name="oidc_login_states")
    op.drop_table("oidc_login_states")
    op.drop_index("ix_external_identities_email", table_name="external_identities")
    op.drop_index(
        "uq_external_identities_provider_subject", table_name="external_identities"
    )
    op.drop_table("external_identities")
    sa.Enum(name="authsource").drop(op.get_bind(), checkfirst=True)
