"""ARGUS Authentication Routes (Hardening W1).

Token management for operators:

```text
GET    /api/v1/auth/whoami                who am I? (any valid token)
POST   /api/v1/auth/tokens                mint a token   (ADMIN)
GET    /api/v1/auth/tokens                list tokens    (ADMIN)
DELETE /api/v1/auth/tokens/{token_id}     revoke a token (ADMIN)
```

The raw token is returned **exactly once**, on creation. Everything the
listing shows afterwards is metadata: name, role, status, grants, last use.
A database dump therefore contains no usable credential, and the UI can never
"show the token again" — because there is nothing to show.

Every mutation writes an ``AuthenticationAudit`` row through the token
service, so "who minted this credential, and when" is a query, not a memory.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_project
from app.core.database import get_db
from app.core.edge import AuthContext, require_admin, require_auth_context
from app.core.security import create_token, revoke_token
from app.models.auth import ApiToken, ApiTokenProject, TokenRole, TokenStatus
from app.schemas.base import BaseSchema

router = APIRouter(prefix="/auth", tags=["Auth"])


class TokenCreate(BaseSchema):
    """Mint a token (ADMIN)."""

    name: str = Field(min_length=1, max_length=255)
    role: TokenRole = TokenRole.OPERATOR
    #: Days until expiry; omitted = no expiry (revoke to disable).
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)
    #: Project grants. Ignored for ADMIN tokens (which pass every project).
    project_ids: List[uuid.UUID] = Field(default_factory=list)
    description: Optional[str] = None


class TokenCreateResponse(BaseSchema):
    """The one and only time the raw secret is visible."""

    id: uuid.UUID
    name: str
    role: TokenRole
    expires_at: Optional[datetime] = None
    project_ids: List[uuid.UUID] = Field(default_factory=list)
    token: str
    warning: str = (
        "Store this token now — it is not retrievable again. Present it as "
        "'Authorization: Bearer <token>'."
    )


class TokenResponse(BaseSchema):
    """Token metadata (never the secret)."""

    id: uuid.UUID
    name: str
    role: TokenRole
    status: TokenStatus
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    created_by: Optional[str] = None
    description: Optional[str] = None
    created_at: datetime
    project_ids: List[uuid.UUID] = Field(default_factory=list)


class TokenList(BaseSchema):
    items: List[TokenResponse]
    total: int


class WhoAmI(BaseSchema):
    """The caller's effective identity — what the UI needs for role-aware UI."""

    token_id: uuid.UUID
    name: str
    role: TokenRole
    #: Empty list = every project (ADMIN). Otherwise the granted projects.
    project_ids: List[uuid.UUID]
    unrestricted: bool
    auth_enforced: bool


@router.get("/whoami", response_model=WhoAmI)
async def whoami(auth: AuthContext = Depends(require_auth_context)) -> WhoAmI:
    """Describe the caller's identity and effective scope."""
    from app.core.security import auth_disabled

    assert auth is not None  # the middleware always sets a context
    unrestricted = auth.is_admin or auth.project_ids is None
    grants = set() if unrestricted else set(auth.project_ids or ())
    return WhoAmI(
        token_id=auth.token_id,
        name=auth.name,
        role=auth.role,
        project_ids=sorted(grants),
        unrestricted=unrestricted,
        auth_enforced=not auth_disabled(),
    )


@router.post("/tokens", response_model=TokenCreateResponse, status_code=201)
async def create_api_token(
    data: TokenCreate,
    auth: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> TokenCreateResponse:
    """Mint a token. ADMIN only — the root credential can never be widened."""
    from datetime import timedelta, timezone

    # A scoped ADMIN makes no sense: ADMIN bypasses grants entirely, so an
    # ADMIN token with grants would silently ignore them. Refuse the
    # contradiction rather than store a misleading row.
    if data.role is TokenRole.ADMIN and data.project_ids:
        raise HTTPException(
            status_code=400,
            detail="ADMIN tokens are unscoped; omit project_ids or choose OPERATOR/VIEWER",
        )

    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=data.expires_in_days)
        if data.expires_in_days
        else None
    )
    for project_id in data.project_ids:
        # Grants must reference real projects: a typo would otherwise create a
        # token that silently cannot reach anything.
        await require_project(db, project_id)

    token, raw = await create_token(
        db,
        name=data.name,
        role=data.role,
        expires_at=expires_at,
        project_ids=data.project_ids,
        created_by=f"token:{auth.token_id}",
        description=data.description,
    )
    return TokenCreateResponse(
        id=token.id,
        name=token.name,
        role=token.role,
        expires_at=token.expires_at,
        project_ids=data.project_ids,
        token=raw,
    )


@router.get("/tokens", response_model=TokenList)
async def list_api_tokens(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> TokenList:
    """List token metadata (never secrets), newest first."""
    total = (await db.execute(select(func.count(ApiToken.id)))).scalar() or 0
    rows = (
        (
            await db.execute(
                select(ApiToken)
                .order_by(ApiToken.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    grant_rows = (
        (
            await db.execute(
                select(ApiTokenProject.token_id, ApiTokenProject.project_id).where(
                    ApiTokenProject.token_id.in_([t.id for t in rows])
                )
            )
        ).all()
        if rows
        else []
    )
    by_token: dict = {}
    for token_id, project_id in grant_rows:
        by_token.setdefault(token_id, []).append(project_id)

    return TokenList(
        items=[
            TokenResponse(
                id=t.id,
                name=t.name,
                role=t.role,
                status=t.status,
                expires_at=t.expires_at,
                revoked_at=t.revoked_at,
                last_used_at=t.last_used_at,
                created_by=t.created_by,
                description=t.description,
                created_at=t.created_at,
                project_ids=by_token.get(t.id, []),
            )
            for t in rows
        ],
        total=total,
    )


@router.delete("/tokens/{token_id}", response_model=TokenResponse)
async def revoke_api_token(
    token_id: uuid.UUID,
    auth: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Revoke a token immediately (idempotent)."""
    token = await revoke_token(db, token_id)
    if token is None:
        raise HTTPException(status_code=404, detail="Token not found")
    grants = (
        await db.execute(
            select(ApiTokenProject.project_id).where(
                ApiTokenProject.token_id == token.id
            )
        )
    ).scalars()
    return TokenResponse(
        id=token.id,
        name=token.name,
        role=token.role,
        status=token.status,
        expires_at=token.expires_at,
        revoked_at=token.revoked_at,
        last_used_at=token.last_used_at,
        created_by=token.created_by,
        description=token.description,
        created_at=token.created_at,
        project_ids=list(grants.all()),
    )


__all__ = ["router"]
