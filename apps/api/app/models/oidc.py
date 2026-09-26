"""ARGUS OIDC / SSO Models (Hardening W2).

Three rows carry the whole feature, and each one exists for a reason a token
table cannot cover:

* :class:`OidcLoginState` — one in-flight authorization-code login. It holds the
  PKCE verifier and the nonce, and it is **single-use**: a consumed state can
  never be spent again.
* :class:`ExternalIdentity` — a person as the identity provider sees them.
  ``(provider, subject)`` is the identity; the email is *display*, never
  identity, because an IdP may recycle an address but never a subject.
* :class:`AuthSource` — recorded on every credential row, so "how did this
  session come to exist" is a column rather than an inference.

Why the login state lives in Postgres instead of a signed cookie: a signature
proves *we* minted the state, not that it has not already been spent. With more
than one replica, replay protection has to be shared, and the database is
already the shared thing. A replayed callback therefore finds
``consumed_at`` set and is refused — which is the property that matters, since
the code it carries is single-use at the provider only if the *client* treats it
that way.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import Boolean, DateTime, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.auth import TokenRole
from app.models.base import BaseModel, JSONType


class OidcLoginState(BaseModel):
    """A single-use authorization-code login.

    ``state`` is the anti-CSRF value the provider echoes back; ``nonce`` binds
    the ID token to *this* request; ``code_verifier`` is the PKCE secret whose
    hash (``code_challenge``) travelled through the browser. Only the verifier
    is stored, never the hash, because the hash is public by construction.

    ``redirect_uri`` is stored with the state so the token exchange uses
    exactly the URI the authorization request declared — providers require
    byte-identical values, and re-deriving it at callback time is how
    intermittent ``invalid_grant`` errors happen.
    """

    __tablename__ = "oidc_login_states"
    __table_args__ = (
        Index("uq_oidc_login_states_state", "state", unique=True),
        Index("ix_oidc_login_states_expires", "expires_at"),
    )

    state: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    provider: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Where to send the browser back to once the exchange is done.
    return_to: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: Set on first use. Never cleared — that is what makes replay impossible.
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    client_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    def is_spendable(self, now: datetime) -> bool:
        """Is this state still usable exactly once?"""
        return self.consumed_at is None and self.expires_at > now


class ExternalIdentity(BaseModel):
    """A person, as provisioned from an identity provider.

    The role and the project grants stored here are a **record of the last
    login**, not the source of policy: claims are re-read on every login and
    overwrite both. Storing the last resolved values is what makes
    "what did ARGUS think this person was allowed to do on Tuesday" answerable
    without the IdP, and it is also what a diff shows when access changes.

    ``disabled_at`` is the kill switch: disabling an identity revokes every
    session it holds, so a removed employee loses access everywhere at once
    rather than at token expiry.
    """

    __tablename__ = "external_identities"
    __table_args__ = (
        Index(
            "uq_external_identities_provider_subject",
            "provider",
            "subject",
            unique=True,
        ),
        Index("ix_external_identities_email", "email"),
    )

    provider: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The IdP's stable user id (``sub``). Never the email.
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    #: Last resolved role and grants (see the class docstring).
    role: Mapped[TokenRole] = mapped_column(Enum(TokenRole), nullable=False)
    project_ids: Mapped[List[str]] = mapped_column(
        JSONType, default=list, nullable=False
    )
    #: The claims that produced the role/grant decision, trimmed to the ones
    #: ARGUS actually reads plus a small allowlist. Storing the whole token
    #: would be storing a credential.
    claims_snapshot: Mapped[Dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_login_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_login_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    login_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_login_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    disabled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    disabled_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    @property
    def is_disabled(self) -> bool:
        return self.disabled_at is not None

    @property
    def granted_project_ids(self) -> list[uuid.UUID]:
        """Grants as UUIDs, skipping anything unparseable (claims are untrusted)."""
        out: list[uuid.UUID] = []
        for raw in self.project_ids or []:
            try:
                out.append(uuid.UUID(str(raw)))
            except (ValueError, TypeError, AttributeError):
                continue
        return out


__all__ = ["ExternalIdentity", "OidcLoginState"]
