"""ARGUS Authentication & Authorization Models (Hardening W1).

One domain, three rows, deliberately small:

* :class:`ApiToken` — a bearer token. The raw secret is shown exactly once at
  creation; the database stores only its SHA-256 hash, so a database dump can
  never be replayed as credentials.
* :class:`ApiTokenProject` — the projects a token may touch. An ``ADMIN`` token
  ignores this table; every other role is scoped to explicit grants. A missing
  grant is a ``404`` at the API layer (existence is never disclosed).
* :class:`AuthenticationAudit` — every authentication event worth answering
  for later: created, used, failed, revoked. Append-only; rows are never
  updated or deleted (a test pins this).

Roles are an enum, not free text, so the authorization surface is enumerable:
``ADMIN`` (everything), ``OPERATOR`` (write: ingest, triage, approve),
``VIEWER`` (read-only). Remediation approval additionally passes through
Phase 9's own gates — this layer never widens them.
"""

from __future__ import annotations

import enum
import hashlib
import secrets
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text
from app.models.base import BaseModel, Guid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.project import SoftwareProject

#: Prefix used for generated tokens. Makes leaked-token identification in logs
#: possible without making the token itself reconstructable.
TOKEN_PREFIX = "argus_"


def _aware(value: datetime) -> datetime:
    """Return a timezone-aware datetime.

    Every timestamp ARGUS stores is UTC, but a database without a native
    timezone type (SQLite, the fast test database) hands them back naive. A
    comparison against ``datetime.now(timezone.utc)`` then raises ``TypeError``
    — an expiry check that crashes in the unit suite and passes in production,
    which is the worst direction for a bug in an authentication path to face.
    Attaching UTC states a fact rather than guessing one: nothing in the
    platform writes a local-time value.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


#: Length of the random part of a token (secrets.token_urlsafe chars).
TOKEN_ENTROPY_CHARS = 32


def hash_token(raw_token: str) -> str:
    """Return the SHA-256 hex digest of a raw token.

    SHA-256 (not bcrypt/argon2) is the correct choice here: the token is a
    256-bit random secret, not a human password, so there is nothing to
    brute-force offline — and lookup-by-hash must stay indexable to keep
    every request's auth check O(1).
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_token() -> tuple[str, str]:
    """Return ``(raw_token, token_hash)``.

    The raw token is returned exactly once — at creation — and must never be
    persisted.
    """
    raw = f"{TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_ENTROPY_CHARS)}"
    return raw, hash_token(raw)


class TokenRole(str, enum.Enum):
    """What a token may do. Kept deliberately enumerable (hardening W1)."""

    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"
    VIEWER = "VIEWER"

    @property
    def can_read(self) -> bool:
        return True

    @property
    def can_write(self) -> bool:
        return self is not TokenRole.VIEWER

    @property
    def can_admin(self) -> bool:
        return self is TokenRole.ADMIN


class TokenStatus(str, enum.Enum):
    """Lifecycle of a token."""

    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class AuthSource(str, enum.Enum):
    """How a credential came into existence.

    Not decoration: a session minted by an identity provider is revoked when
    that provider disables the identity (hardening W2), while a machine token
    outlives any human's employment. An operator looking at the token list has
    to be able to tell the two apart, and so does the retention rule that
    bounds how many expired sessions accumulate.
    """

    TOKEN = "TOKEN"
    OIDC = "OIDC"


class AuthAuditAction(str, enum.Enum):
    """The authentication events the audit trail records."""

    CREATED = "CREATED"
    USED = "USED"
    FAILED = "FAILED"
    REVOKED = "REVOKED"


class ApiToken(BaseModel):
    """A bearer token.

    Invariants pinned by tests:

    * ``token_hash`` is unique — one row per secret;
    * the raw secret is never stored anywhere;
    * ``revoked_at``/``expires_at`` are the only ways a token stops working,
      and both are checked on every request.
    """

    __tablename__ = "api_tokens"
    __table_args__ = (Index("ix_api_tokens_status", "status"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    role: Mapped[TokenRole] = mapped_column(Enum(TokenRole), nullable=False)
    status: Mapped[TokenStatus] = mapped_column(
        Enum(TokenStatus), default=TokenStatus.ACTIVE, nullable=False
    )
    #: Optional expiry. ``None`` means the token lives until revoked.
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Who/what created this token (the bootstrap token records "bootstrap";
    #: tokens created via the API record the creating token's id).
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: ``TOKEN`` for a minted API credential, ``OIDC`` for an SSO session.
    #: Defaulted rather than nullable so an old row's meaning is unambiguous.
    auth_source: Mapped["AuthSource"] = mapped_column(
        Enum(AuthSource), default=lambda: AuthSource.TOKEN, nullable=False
    )
    #: The provisioned person behind an SSO session; ``None`` for machine
    #: tokens. ``SET NULL`` (not cascade) is deliberate: disabling a person
    #: must not silently delete the credentials they used, because the audit
    #: trail refers to them. Sessions are revoked explicitly instead.
    external_identity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("external_identities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    project_grants: Mapped[List["ApiTokenProject"]] = relationship(
        back_populates="token", cascade="all, delete-orphan"
    )

    def is_usable(self, now: datetime) -> bool:
        """Is this token accepted right now?"""
        if self.status is not TokenStatus.ACTIVE:
            return False
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and _aware(self.expires_at) <= now:
            return False
        return True


class ApiTokenProject(BaseModel):
    """A project grant: this token may act on this project.

    ``ADMIN`` tokens bypass this table by role; every other role must hold an
    explicit row per project it touches.
    """

    __tablename__ = "api_token_projects"
    __table_args__ = (
        Index(
            "uq_api_token_projects_token_project",
            "token_id",
            "project_id",
            unique=True,
        ),
    )

    token_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_tokens.id"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    token: Mapped["ApiToken"] = relationship(back_populates="project_grants")
    project: Mapped["SoftwareProject"] = relationship()


class AuthenticationAudit(BaseModel):
    """One append-only authentication event.

    Written by the auth layer itself, never by request handlers; there is no
    code path that mutates or deletes a row (a test attempts both and must
    fail). This is what makes "who did what with which credential" a query
    rather than a guess.
    """

    __tablename__ = "authentication_audit"
    __table_args__ = (
        Index("ix_authentication_audit_token_action", "token_id", "action"),
        Index("ix_authentication_audit_occurred", "occurred_at"),
    )

    token_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    #: The hash prefix of the presented secret (first 12 hex chars) — enough to
    #: correlate failures with a token, never enough to replay it.
    token_hash_prefix: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    action: Mapped[AuthAuditAction] = mapped_column(
        Enum(AuthAuditAction), nullable=False
    )
    #: Why a FAILED event happened (unknown token / expired / revoked / no grant).
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    request_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    client_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
