"""ARGUS Single Sign-On Routes (Hardening W2).

```text
GET    /api/v1/auth/oidc/config                     is SSO on? what is it called?   (public)
GET    /api/v1/auth/oidc/login                      start a login (302)             (public)
POST   /api/v1/auth/oidc/callback                   finish a login                  (public)
GET    /api/v1/auth/oidc/identities                 provisioned people              (ADMIN)
POST   /api/v1/auth/oidc/identities/{id}/disable    disable + revoke sessions       (ADMIN)
POST   /api/v1/auth/oidc/identities/{id}/enable     re-enable a person              (ADMIN)
```

Three of these are public **by necessity**, and that is the whole reason this
module exists as its own file rather than as extra methods on the token routes:
before a login there is no credential to present. They are public in the narrow
sense — the middleware admits them without a token and binds a context with
**zero grants**, so nothing downstream can mistake "no credential yet" for
"privilege". The tests assert both halves: that these three are reachable
anonymously, and that nothing else under ``/auth/oidc`` is.

The callback is a ``POST`` because the browser page, not the provider, calls
it: the provider sends the browser to the web app's callback URL, and that page
posts ``{code, state}`` here. The ID token and the session secret are created
server-side and returned in the response body, so neither ever appears in a
URL, a ``Referer`` header or an access log.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.edge import AuthContext, require_admin
from app.models.oidc import ExternalIdentity
from app.schemas.base import BaseSchema
from app.services.oidc import (
    OidcClient,
    OidcError,
    configuration,
    disable_identity,
    record_login_attempt,
)

logger = logging.getLogger("argus.oidc")

router = APIRouter(prefix="/auth/oidc", tags=["Auth"])


class OidcPublicConfig(BaseSchema):
    """What a login page needs, and nothing an attacker can use."""

    enabled: bool
    provider_name: str
    #: Relative URL the browser should navigate to.
    login_path: str
    #: The URL registered at the provider as the callback, so an operator can
    #: compare it with what the provider has *without* reading the environment.
    redirect_uri: str
    #: True when every identity must arrive with a verified email address.
    requires_verified_email: bool
    #: The domains allowed to sign in, when the deployment restricts them.
    allowed_email_domains: List[str] = Field(default_factory=list)


class OidcCallbackRequest(BaseSchema):
    """The authorization code and state, as the browser received them."""

    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=1, max_length=512)


class OidcLoginResponse(BaseSchema):
    """A minted SSO session. The secret appears here and never again."""

    token: str
    expires_at: datetime
    token_id: uuid.UUID
    role: str
    #: Human-readable identity, for the console to greet the user by.
    display_name: Optional[str] = None
    email: Optional[str] = None
    provider: str
    project_ids: List[uuid.UUID] = Field(default_factory=list)
    unrestricted: bool = False
    #: Where the browser should go next (the console's own home by default).
    redirect_to: str
    warning: str = (
        "Store this token now — it is not retrievable again. Present it as "
        "'Authorization: Bearer <token>'."
    )


class ExternalIdentityResponse(BaseSchema):
    """A provisioned person as ARGUS last saw them at login."""

    id: uuid.UUID
    provider: str
    subject: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    role: str
    project_ids: List[str] = Field(default_factory=list)
    email_verified: bool
    first_login_at: datetime
    last_login_at: datetime
    login_count: int
    last_login_ip: Optional[str] = None
    disabled: bool
    disabled_at: Optional[datetime] = None
    disabled_reason: Optional[str] = None
    #: Every session the person currently holds — the blast radius of a
    #: revocation, shown rather than described.
    active_sessions: int = 0


class ExternalIdentityList(BaseSchema):
    items: List[ExternalIdentityResponse]
    total: int


class IdentityStateChange(BaseSchema):
    id: uuid.UUID
    disabled: bool
    revoked_sessions: int


def _browser_error_target() -> str:
    """Where to send a browser whose login could not even start.

    The registered callback URL when there is one — the web app's own callback
    page renders the error — otherwise the API root, so a misconfigured
    deployment still fails with something readable instead of a raw JSON blob
    in the address bar.
    """
    config = configuration()
    return config.redirect_uri or "/"


@router.get("/config", response_model=OidcPublicConfig)
async def oidc_public_config() -> OidcPublicConfig:
    """Describe the sign-in options on this deployment.

    Public on purpose: a login page has to know whether to render an SSO button
    *before* anyone has a credential. Nothing here is a secret — the provider
    name, the callback URL the operator registered, and the policy switches.
    """
    config = configuration()
    return OidcPublicConfig(
        enabled=config.enabled,
        provider_name=config.provider_name,
        login_path="/api/v1/auth/oidc/login",
        redirect_uri=config.redirect_uri,
        requires_verified_email=config.require_verified_email,
        allowed_email_domains=sorted(config.allowed_domains),
    )


@router.get("/login")
async def oidc_login(
    request: Request,
    return_to: Optional[str] = Query(default=None, max_length=512),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Start the authorization-code flow.

    Responds with a redirect either way: to the provider on success, or back to
    the web app's callback page carrying the refusal reason. A 4xx JSON body
    would be technically correct and useless — the caller here is a browser
    mid-navigation, and it needs somewhere to land.
    """
    target = _browser_error_target()
    try:
        client = OidcClient()
        try:
            url = await client.begin(
                db,
                return_to=return_to,
                client_ip=request.client.host if request.client else None,
            )
        finally:
            await client.aclose()
    except OidcError as exc:
        record_login_attempt("failure")
        logger.info("SSO login refused before redirect: %s", exc.reason)
        separator = "&" if "?" in target else "?"
        return RedirectResponse(
            url=f"{target}{separator}error={exc.reason}",
            status_code=302,
        )
    return RedirectResponse(url=url, status_code=302)


@router.post("/callback", response_model=OidcLoginResponse)
async def oidc_callback(
    data: OidcCallbackRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> OidcLoginResponse:
    """Finish the flow: verify the ID token, then mint a session.

    Every refusal is a ``4xx`` with a stable ``error_code`` so the callback page
    can explain itself, and every attempt — either way — is counted and, for a
    refusal, written to the authentication audit trail by the client itself.
    """
    from fastapi import HTTPException

    from app.core.security import AuthAuditAction, write_auth_audit

    client = OidcClient()
    try:
        outcome = await client.complete(
            db,
            code=data.code,
            state=data.state,
            client_ip=request.client.host if request.client else None,
        )
    except OidcError as exc:
        record_login_attempt("failure")
        logger.warning("SSO login refused: %s", exc.reason)
        #: The credential is refused, so the trail must say so. Best effort is
        #: wrong here (unlike USED events): an unrecorded refusal is the blind
        #: spot an attacker would live in.
        await write_auth_audit(
            db,
            action=AuthAuditAction.FAILED,
            reason=f"oidc:{exc.reason}",
            request_path="/api/v1/auth/oidc/callback",
            client_ip=request.client.host if request.client else None,
        )
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_code": exc.reason, "message": exc.detail},
        ) from exc
    finally:
        await client.aclose()

    record_login_attempt("success")
    identity = outcome.identity
    return OidcLoginResponse(
        token=outcome.raw_token,
        expires_at=outcome.expires_at,
        token_id=outcome.token.id,
        role=outcome.decision.role.value,
        display_name=identity.display_name,
        email=identity.email,
        provider=identity.provider,
        project_ids=list(identity.granted_project_ids),
        unrestricted=outcome.decision.is_admin,
        redirect_to=outcome.redirect_to,
    )


async def _identity_response(
    db: AsyncSession, identity: ExternalIdentity
) -> ExternalIdentityResponse:
    """Render a person, counting the sessions that a revocation would end."""
    from sqlalchemy import func

    from app.models.auth import ApiToken, TokenStatus

    active = (
        await db.execute(
            select(func.count(ApiToken.id)).where(
                ApiToken.external_identity_id == identity.id,
                ApiToken.status == TokenStatus.ACTIVE,
            )
        )
    ).scalar() or 0
    return ExternalIdentityResponse(
        id=identity.id,
        provider=identity.provider,
        subject=identity.subject,
        email=identity.email,
        display_name=identity.display_name,
        role=identity.role.value,
        project_ids=list(identity.project_ids or []),
        email_verified=identity.email_verified,
        first_login_at=identity.first_login_at,
        last_login_at=identity.last_login_at,
        login_count=identity.login_count,
        last_login_ip=identity.last_login_ip,
        disabled=identity.is_disabled,
        disabled_at=identity.disabled_at,
        disabled_reason=identity.disabled_reason,
        active_sessions=active,
    )


@router.get("/identities", response_model=ExternalIdentityList)
async def list_external_identities(
    _: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    include_disabled: bool = Query(default=True),
) -> ExternalIdentityList:
    """List provisioned people — the access surface, not a user directory.

    ADMIN-only: it names the humans who can reach this deployment, which is
    itself sensitive. ``include_disabled=false`` narrows it to the people who
    can currently sign in.
    """
    stmt = select(ExternalIdentity).order_by(ExternalIdentity.last_login_at.desc())
    if not include_disabled:
        stmt = stmt.where(ExternalIdentity.disabled_at.is_(None))
    rows = (await db.execute(stmt)).scalars().all()
    items = [await _identity_response(db, row) for row in rows]
    return ExternalIdentityList(items=items, total=len(items))


@router.post("/identities/{identity_id}/disable", response_model=IdentityStateChange)
async def disable_external_identity(
    identity_id: uuid.UUID,
    _: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> IdentityStateChange:
    """Disable a person and revoke every session they hold, atomically.

    Deactivating someone at the provider is not enough: their ARGUS session is
    a bearer token that no longer consults the provider. This is the operation
    that actually ends their access, which is why it does both things.
    """
    from fastapi import HTTPException

    from app.models.auth import ApiToken, TokenStatus

    before = (
        (
            await db.execute(
                select(ApiToken).where(
                    ApiToken.external_identity_id == identity_id,
                    ApiToken.status == TokenStatus.ACTIVE,
                )
            )
        )
        .scalars()
        .all()
    )
    identity = await disable_identity(db, identity_id)
    if identity is None:
        raise HTTPException(status_code=404, detail="Identity not found")
    return IdentityStateChange(
        id=identity.id, disabled=True, revoked_sessions=len(list(before))
    )


@router.post("/identities/{identity_id}/enable", response_model=IdentityStateChange)
async def enable_external_identity(
    identity_id: uuid.UUID,
    _: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> IdentityStateChange:
    """Re-enable a person.

    Deliberately does **not** restore the revoked sessions: re-enabling means
    "may sign in again", not "the credential you were holding works again".
    The next login mints a fresh session.
    """
    from fastapi import HTTPException

    identity = await db.get(ExternalIdentity, identity_id)
    if identity is None:
        raise HTTPException(status_code=404, detail="Identity not found")
    identity.disabled_at = None
    identity.disabled_reason = None
    await db.commit()
    return IdentityStateChange(id=identity.id, disabled=False, revoked_sessions=0)


__all__ = ["router"]
