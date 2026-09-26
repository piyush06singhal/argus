"""ARGUS Ingestion Trust (Hardening W1).

Per-source credentials for the ingestion boundary:

* every :class:`~app.models.ingestion.ObservabilitySource` can carry an
  ``ingest_token_hash`` (SHA-256). The raw token is shown once at creation /
  rotation and never stored — the same contract as API tokens;
* :func:`resolve_ingest_token` maps a presented raw token to the project its
  source belongs to (``None`` for unknown/revoked — no existence oracle);
* :func:`issue_ingest_token` / :func:`rotate_ingest_token` are the only ways a
  token hash is written, and both append an authentication-audit row.

A source without a token hash is *closed*: the OTLP dependency treats a
missing hash as "no credential will ever match" rather than "anyone may
ingest". This is what makes the default posture fail-closed.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import INGEST_TOKEN_PREFIX, hash_token, utcnow
from app.models.auth import AuthAuditAction, AuthenticationAudit
from app.models.ingestion import ObservabilitySource

logger = logging.getLogger("argus.ingest_trust")


def _new_raw_token() -> str:
    return f"{INGEST_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


async def issue_ingest_token(
    db: AsyncSession,
    source: ObservabilitySource,
    *,
    actor: str,
) -> str:
    """Mint (or replace) the source's ingest token; return the raw secret once."""
    raw = _new_raw_token()
    source.ingest_token_hash = hash_token(raw)
    source.ingest_token_rotated_at = utcnow()
    db.add(
        AuthenticationAudit(
            action=AuthAuditAction.CREATED,
            reason=f"ingest token issued for source {source.id} by {actor}",
            occurred_at=utcnow(),
        )
    )
    await db.commit()
    return raw


async def rotate_ingest_token(
    db: AsyncSession,
    source: ObservabilitySource,
    *,
    actor: str,
) -> str:
    """Replace the source's token. The old secret stops working immediately."""
    return await issue_ingest_token(db, source, actor=actor)


async def resolve_ingest_token(db: AsyncSession, raw_token: str) -> Optional[uuid.UUID]:
    """Return the project a valid ingest token may ingest into, else ``None``.

    Only sources whose token hash matches AND that are not deleted resolve.
    There is no distinction in the response between unknown, rotated-out and
    revoked tokens — all are ``None`` (no existence oracle).
    """
    stmt = select(ObservabilitySource).where(
        ObservabilitySource.ingest_token_hash == hash_token(raw_token)
    )
    source = (await db.execute(stmt)).scalar_one_or_none()
    if source is None:
        return None
    return source.project_id


def source_matches_ingest_token(source: ObservabilitySource, raw_token: str) -> bool:
    """Does ``raw_token`` belong to *this* source?

    Used by the per-source webhook path, where matching the *project* is not
    enough: without this check one source's credential would deliver events as
    a sibling source in the same project. Constant-time comparison over the
    stored hash, and a source without a token never matches.
    """
    import hmac as hmac_module

    stored = source.ingest_token_hash
    if not stored:
        return False
    return hmac_module.compare_digest(stored, hash_token(raw_token))


async def revoke_ingest_token(
    db: AsyncSession,
    source: ObservabilitySource,
    *,
    actor: str,
) -> None:
    """Close the source: any previously issued token stops working."""
    source.ingest_token_hash = None
    source.ingest_token_rotated_at = utcnow()
    db.add(
        AuthenticationAudit(
            action=AuthAuditAction.REVOKED,
            reason=f"ingest token revoked for source {source.id} by {actor}",
            occurred_at=utcnow(),
        )
    )
    await db.commit()


def verify_webhook_signature(
    *,
    secret: str,
    timestamp: str,
    signature: str,
    body: bytes,
    tolerance_seconds: int = 300,
    now: Optional[datetime] = None,
) -> bool:
    """Verify an HMAC-signed ingestion webhook (replay-resistant).

    Scheme (mirrors the Phase 11 platform webhooks, proven code):
    ``X-Argus-Timestamp`` + ``X-Argus-Signature: sha256=<hex>`` where the MAC
    covers ``f"{timestamp}.{body}"``. Signatures older than
    ``tolerance_seconds`` are refused even when valid — that is the replay
    protection. Comparison is constant-time.
    """
    import hashlib
    import hmac as hmac_module

    if now is None:
        now = utcnow()
    try:
        ts = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return False
    if abs((now - ts).total_seconds()) > tolerance_seconds:
        return False
    if not signature.startswith("sha256="):
        return False
    expected = hmac_module.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()
    return hmac_module.compare_digest(expected, signature.removeprefix("sha256="))
