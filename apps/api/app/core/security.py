"""ARGUS Token Service (Hardening W1).

One module owns everything about credentials:

* bootstrap — first boot mints a root ``ADMIN`` token, prints it once;
* verification — hash lookup, expiry/revocation checks, constant behavior on
  failure (no existence oracle: unknown, expired and revoked all fail the same
  way and are only distinguished in the audit trail);
* authorization — role checks and per-project grants.

Enforcement topology (deliberate):

* **Authentication** happens once, centrally, in
  :class:`app.core.edge.AuthMiddleware` — no route can be forgotten, and the
  OpenAPI introspection test keeps the public-path set honest.
* **Role floors** — reads need any token, writes need OPERATOR+ — and
  **project grants** are enforced by the route layer reading the caller's
  :class:`AuthContext` from the contextvar the middleware sets
  (``get_auth_context()`` in ``app.core.edge``); ``require_project`` is the
  single choke point for grants, and ``require_admin`` guards the handful of
  administrative routes.

``AUTH_DISABLED`` semantics live in config; production refuses to boot with
it enabled.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.auth import (
    ApiToken,
    ApiTokenProject,
    AuthAuditAction,
    AuthenticationAudit,
    TokenRole,
    TokenStatus,
    generate_token,
    hash_token,
)

logger = logging.getLogger("argus.auth")

#: Routes that never require a token. Everything else under ``/api`` does —
#: proven by ``tests/test_auth_security.py::TestOpenAPIAuthCoverage``.
PUBLIC_PATHS = frozenset(
    {
        "/",
        "/health/live",
        "/health/ready",
        "/health/dependencies",
        "/metrics",
    }
)

#: Routes reachable **before any credential exists** (hardening W2).
#:
#: A sign-in flow logically cannot require the artifact it produces: the
#: configuration probe, the authorization redirect and the code exchange all
#: run for a caller who has no token yet. They are admitted anonymously and
#: bound to a context with *zero* grants (see ``app.core.edge``), so "no
#: credential" can never be read as "privilege". The set is exact and asserted
#: by a test: it is three paths, and anything else added to it fails that test
#: rather than passing review.
PUBLIC_BOOTSTRAP_PATHS = frozenset(
    {
        "/api/v1/auth/oidc/config",
        "/api/v1/auth/oidc/login",
        "/api/v1/auth/oidc/callback",
    }
)

#: Ingestion routes authenticate with *source* tokens instead of API tokens
#: (an OTLP collector must not hold an ADMIN credential).
INGEST_PATH_PREFIX = "/api/v1/otlp"

#: Prefix of a per-source ingest token. Disjoint from API tokens (``argus_``)
#: so the funnel can tell the two credential spaces apart from the secret
#: alone and each can be refused outside its own surface.
INGEST_TOKEN_PREFIX = "argus_ing_"


class AuthContext:
    """Everything authorization decisions need about the caller."""

    __slots__ = ("token_id", "role", "project_ids", "name")

    def __init__(
        self,
        token_id: uuid.UUID,
        role: TokenRole,
        project_ids: Optional[set],
        name: str,
    ) -> None:
        self.token_id = token_id
        self.role = role
        #: ``None`` = all projects (admin); otherwise the explicit grant set.
        self.project_ids = project_ids
        self.name = name

    @property
    def is_admin(self) -> bool:
        return self.role is TokenRole.ADMIN

    @property
    def can_write(self) -> bool:
        return self.role.can_write

    def can_access_project(self, project_id: Optional[uuid.UUID]) -> bool:
        if project_id is None:
            return True  # endpoints without a project scope carry no grant check
        if self.is_admin or self.project_ids is None:
            return True
        return project_id in self.project_ids


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _write_audit(
    db: AsyncSession,
    *,
    action: AuthAuditAction,
    token_id: Optional[uuid.UUID] = None,
    token_hash_prefix: Optional[str] = None,
    reason: Optional[str] = None,
    request_path: Optional[str] = None,
    client_ip: Optional[str] = None,
    best_effort: bool = False,
) -> None:
    """Append one authentication-audit row.

    With ``best_effort=True`` (USED events) an audit failure is logged, not
    raised: an audit-table outage must never take the API down. FAILED events
    are hard — a refused credential without a record is a hole.
    """
    row = AuthenticationAudit(
        token_id=token_id,
        token_hash_prefix=token_hash_prefix,
        action=action,
        reason=reason,
        request_path=request_path,
        client_ip=client_ip,
        occurred_at=utcnow(),
    )
    db.add(row)
    try:
        await db.flush()
    except Exception:  # noqa: BLE001 — see docstring; the API stays up either way
        await db.rollback()
        if best_effort:
            logger.warning("authentication audit write failed", exc_info=True)
        else:
            logger.error("authentication audit write FAILED", exc_info=True)
            raise


#: Public alias for the single audit writer. Every credential lifecycle event
#: on the platform — token minted, token used, token revoked, SSO login refused
#: — lands in this one table through this one function, so "how did this
#: session come to exist" has exactly one answer format.
write_auth_audit = _write_audit


async def bootstrap_admin_token(db: AsyncSession) -> Optional[str]:
    """Create the root ADMIN token on first boot.

    Returns the raw token (printed once to the container log) or ``None``
    when an active admin token already exists — bootstrapping is idempotent
    and never duplicates credentials.

    This runs inside the application's startup lifespan, so it has to hold for
    every state a real deployment can be in. Two are worth stating explicitly:

    * **More than one active admin token is normal, not an error.** The
      documented recovery path is to mint a replacement (``app.cli
      bootstrap-token``) and keep serving; revoking the old one is a separate,
      deliberate step. ``scalar_one_or_none()`` made that state fatal at the
      *next restart* — ``MultipleResultsFound`` aborts startup, so an operator
      who did exactly what the runbook says wakes up to an API that will not
      boot. The check is therefore "does an active admin token exist", which is
      a bounded existence question, not "is there exactly one".
    * **Concurrent workers.** Two processes booting together may both find no
      token and both mint one. That is the same situation and must not be fatal
      either, which is why the existence check is ordered and limited rather
      than unique-dependent.
    """
    existing = (
        await db.execute(
            select(ApiToken.id)
            .where(
                ApiToken.role == TokenRole.ADMIN,
                ApiToken.status == TokenStatus.ACTIVE,
            )
            .order_by(ApiToken.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None

    raw, token_hash = generate_token()
    token = ApiToken(
        name="bootstrap-admin",
        token_hash=token_hash,
        role=TokenRole.ADMIN,
        status=TokenStatus.ACTIVE,
        created_by="bootstrap",
        description="Root admin token minted on first boot",
    )
    db.add(token)
    await db.flush()
    await _write_audit(
        db,
        action=AuthAuditAction.CREATED,
        token_id=token.id,
        reason="bootstrap",
    )
    await db.commit()
    return raw


async def create_token(
    db: AsyncSession,
    *,
    name: str,
    role: TokenRole,
    expires_at: Optional[datetime] = None,
    project_ids: Optional[list[uuid.UUID]] = None,
    created_by: Optional[str] = None,
    description: Optional[str] = None,
) -> tuple[ApiToken, str]:
    """Create a token (API surface). Returns the row and the one-time secret.

    Also used by the CLI and the tests — one creation path, one audit trail.
    """
    if not name or not name.strip():
        raise ValueError("token name is required")
    raw, token_hash = generate_token()
    token = ApiToken(
        name=name.strip()[:255],
        token_hash=token_hash,
        role=role,
        status=TokenStatus.ACTIVE,
        expires_at=expires_at,
        created_by=created_by,
        description=description,
    )
    db.add(token)
    await db.flush()
    for pid in project_ids or []:
        db.add(ApiTokenProject(token_id=token.id, project_id=pid))
    await _write_audit(
        db,
        action=AuthAuditAction.CREATED,
        token_id=token.id,
        reason=f"created by {created_by or 'unknown'}",
    )
    await db.commit()
    await db.refresh(token)
    return token, raw


async def revoke_token(db: AsyncSession, token_id: uuid.UUID) -> Optional[ApiToken]:
    """Revoke a token immediately. Idempotent."""
    token = await db.get(ApiToken, token_id)
    if token is None:
        return None
    if token.status is TokenStatus.ACTIVE:
        token.status = TokenStatus.REVOKED
        token.revoked_at = utcnow()
        await _write_audit(db, action=AuthAuditAction.REVOKED, token_id=token.id)
        await db.commit()
        await db.refresh(token)
    return token


async def verify_token(
    db: AsyncSession, raw_token: str
) -> Optional[tuple[ApiToken, set]]:
    """Resolve a raw token to ``(token, project_grant_ids)`` or ``None``.

    Expired and revoked tokens resolve to ``None`` exactly like unknown ones —
    the difference is only ever written to the audit trail, never returned to
    the caller (no existence oracle).
    """
    stmt = select(ApiToken).where(ApiToken.token_hash == hash_token(raw_token))
    token = (await db.execute(stmt)).scalar_one_or_none()
    if token is None or not token.is_usable(utcnow()):
        return None
    grants = (
        await db.execute(
            select(ApiTokenProject.project_id).where(
                ApiTokenProject.token_id == token.id
            )
        )
    ).scalars()
    return token, set(grants.all())


def require_project_access(
    auth: AuthContext,
    project_id: Optional[uuid.UUID],
) -> None:
    """Enforce a project grant for non-admin tokens.

    A caller without the grant gets ``404`` — the same answer as an unknown
    project — so token-scoped users cannot even discover foreign project ids.
    Wired centrally in ``require_project`` so no route can forget it.
    """
    if not auth.can_access_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")


def enforce_ingest_scope(auth: AuthContext, project_id: uuid.UUID) -> None:
    """Bind an ingestion request to the credential's project (W1).

    An ingest token resolves to exactly one project; an API token must hold
    the grant. A body naming a different project is a cross-tenant write
    attempt: refused with 403 (the caller is authenticated; the *scope* is
    wrong — unlike the read API there is no discovery value to protect here,
    so clarity wins).
    """
    if auth.is_admin or auth.project_ids is None:
        return
    if project_id not in auth.project_ids:
        raise HTTPException(
            status_code=403,
            detail="Credential is not valid for this project",
        )


def auth_disabled() -> bool:
    """Is the auth bypass active for this process?

    Three-state semantics (see config): unset means enforced everywhere except
    the test environment; an explicit ``true`` works only outside production
    (production refuses to boot with it — config-level guard).
    """
    settings = get_settings()
    flag = settings.AUTH_DISABLED
    if flag is None:
        return settings.is_testing
    return flag and not settings.is_production


__all__ = [
    "AuthContext",
    "PUBLIC_PATHS",
    "PUBLIC_BOOTSTRAP_PATHS",
    "INGEST_PATH_PREFIX",
    "INGEST_TOKEN_PREFIX",
    "auth_disabled",
    "bootstrap_admin_token",
    "create_token",
    "enforce_ingest_scope",
    "hash_token",
    "require_project_access",
    "revoke_token",
    "utcnow",
    "verify_token",
    "write_auth_audit",
]
