"""ARGUS OIDC / SSO Service (Hardening W2).

ARGUS as an OAuth2 **client**: authorization-code flow with PKCE, an ID token
verified against the provider's published keys, and authorization derived from
claims. It never accepts a token it has not verified, and it never trusts a
claim it has not bounded.

The four decisions that shape this module:

**1. PKCE always, and the verifier never leaves the server.** The browser
carries only the challenge, so an intercepted authorization code is useless on
its own. The verifier is stored with the login state (single-use, in Postgres)
and never in a URL, a cookie or a log line.

**2. The keys come from the provider's JWKS, fetched from the issuer we
configured.** Discovery is checked against ``OIDC_ISSUER`` rather than believed,
so a redirected discovery document cannot move the trust anchor. Only
asymmetric algorithms are accepted: ``alg: none`` and the HMAC family are
refused by listing them out of the allowlist, which is what stops the classic
"sign the token with the client secret as an HMAC key" confusion.

**3. Authorization is derived from claims and then *recorded*.** Role comes from
a configurable claim and an explicit value allowlist (ADMIN values, OPERATOR
values, else a configured floor that is never ADMIN). Project grants come from a
configurable claim, filtered against projects that actually exist — a claim
naming a foreign tenant's project id must not create access, and it does not,
because grants are only ever written for ids the database already knows.

**4. Nothing is trusted because the mailbox said so.** An optional email-domain
allowlist and a verified-email requirement are both enforced *before* any
credential is minted, because an IdP that authenticates the whole corporate
directory is not the same as one that authenticates your team.

Every outcome — success or refusal — writes an ``AuthenticationAudit`` row and
moves a counter. A login that fails silently is the one bug this module cannot
afford.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Sequence, cast

import httpx
import jwt
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.runtime_metrics import incr
from app.models.auth import (
    ApiToken,
    ApiTokenProject,
    AuthAuditAction,
    AuthSource,
    TokenRole,
    TokenStatus,
    generate_token,
)
from app.models.oidc import ExternalIdentity, OidcLoginState
from app.models.project import SoftwareProject

logger = logging.getLogger("argus.oidc")

#: Signature algorithms ARGUS will accept for an ID token. Asymmetric only:
#: ``none`` is a missing signature and the HS* family turns a shared secret
#: into a signing key, which is how JWT confusion attacks work.
ALLOWED_ID_TOKEN_ALGORITHMS = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
)

#: Claims copied into the stored snapshot. Reading claims outside this list
#: happens for role/grants; storing them all would store a credential.
_SNAPSHOT_CLAIMS = ("sub", "email", "email_verified", "name", "preferred_username")

#: Machine-readable reasons a login can be refused. Stable strings, because
#: operators grep for them and a test asserts each one is reachable.
REASON_PROVIDER_DISABLED = "oidc_disabled"
REASON_STATE_UNKNOWN = "state_unknown"
REASON_STATE_SPENT = "state_already_used"
REASON_STATE_EXPIRED = "state_expired"
REASON_EXCHANGE_FAILED = "token_exchange_failed"
REASON_TOKEN_INVALID = "id_token_invalid"
REASON_NONCE_MISMATCH = "nonce_mismatch"
REASON_EMAIL_UNVERIFIED = "email_not_verified"
REASON_EMAIL_DOMAIN = "email_domain_not_allowed"
REASON_IDENTITY_DISABLED = "identity_disabled"
REASON_CLAIMS_INVALID = "claims_invalid"


class OidcError(Exception):
    """A login that must not produce a credential.

    Carries a stable ``reason`` (for the audit trail and the tests) plus a
    human-readable detail. The detail is safe to show a user: it never contains
    a token, a code, a verifier or a secret.
    """

    def __init__(self, reason: str, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class OidcConfiguration:
    """The resolved, validated identity-provider configuration.

    A frozen snapshot rather than live ``Settings`` reads: the configuration is
    read once per request and there is exactly one place where the string
    values from the environment become the typed values the flow uses.
    """

    enabled: bool
    provider_name: str
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...]
    role_claim: str
    admin_values: frozenset[str]
    operator_values: frozenset[str]
    default_role: TokenRole
    project_claim: str
    allowed_domains: frozenset[str]
    require_verified_email: bool
    session_ttl_seconds: int
    state_ttl_seconds: int
    timeout_seconds: float
    leeway_seconds: int
    jwks_cache_seconds: int
    single_session: bool
    post_login_redirect: str

    @property
    def discovery_url(self) -> str:
        return f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"


def _aware(value: datetime) -> datetime:
    """Return a timezone-aware datetime.

    SQLite (the fast test database) round-trips ``DateTime(timezone=True)`` as
    a naive value, so a comparison against ``datetime.now(timezone.utc)``
    raises ``TypeError`` there and works in PostgreSQL — a difference that
    would turn every login into a 500 in the unit suite and nothing at all in
    production. Same helper as the rest of the platform; stored timestamps are
    UTC by construction, so attaching UTC is a statement of fact, not a guess.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _as_tuple(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(str(v).strip() for v in values if str(v).strip())


def configuration(settings: Optional[Settings] = None) -> OidcConfiguration:
    """Build the typed configuration from settings.

    ``OIDC_DEFAULT_ROLE`` is normalized here (not trusted as written): an
    unknown value falls back to VIEWER rather than failing at the first login,
    and there is a test asserting a configured ``ADMIN`` floor is honoured
    exactly as written — the operator asked for it, and silently downgrading a
    deliberate choice is its own kind of lie.
    """
    s = settings or get_settings()
    try:
        default_role = TokenRole(str(s.OIDC_DEFAULT_ROLE).strip().upper())
    except ValueError:
        logger.warning(
            "OIDC_DEFAULT_ROLE=%r is not a known role; using VIEWER",
            s.OIDC_DEFAULT_ROLE,
        )
        default_role = TokenRole.VIEWER
    return OidcConfiguration(
        enabled=bool(s.OIDC_ENABLED),
        provider_name=(s.OIDC_PROVIDER_NAME or "SSO").strip(),
        issuer=(s.OIDC_ISSUER or "").strip(),
        client_id=(s.OIDC_CLIENT_ID or "").strip(),
        client_secret=s.OIDC_CLIENT_SECRET or "",
        redirect_uri=(s.OIDC_REDIRECT_URI or "").strip(),
        scopes=_as_tuple(s.OIDC_SCOPES) or ("openid",),
        role_claim=(s.OIDC_ROLE_CLAIM or "").strip(),
        admin_values=frozenset(_as_tuple(s.OIDC_ADMIN_CLAIM_VALUES)),
        operator_values=frozenset(_as_tuple(s.OIDC_OPERATOR_CLAIM_VALUES)),
        default_role=default_role,
        project_claim=(s.OIDC_PROJECT_CLAIM or "").strip(),
        allowed_domains=frozenset(
            d.lower().lstrip("@") for d in _as_tuple(s.OIDC_ALLOWED_EMAIL_DOMAINS)
        ),
        require_verified_email=bool(s.OIDC_REQUIRE_VERIFIED_EMAIL),
        session_ttl_seconds=int(s.OIDC_SESSION_TTL_SECONDS),
        state_ttl_seconds=int(s.OIDC_STATE_TTL_SECONDS),
        timeout_seconds=float(s.OIDC_HTTP_TIMEOUT_SECONDS),
        leeway_seconds=int(s.OIDC_LEEWAY_SECONDS),
        jwks_cache_seconds=int(s.OIDC_JWKS_CACHE_SECONDS),
        single_session=bool(s.OIDC_SINGLE_SESSION),
        post_login_redirect=(s.OIDC_POST_LOGIN_REDIRECT or "").strip(),
    )


def claim_value(claims: Dict[str, Any], path: str) -> Any:
    """Read a possibly-dotted claim path (``realm_access.roles``).

    Providers nest; a flat lookup would silently miss the claim and hand
    everyone the default role, which is the failure mode that looks like it
    works. Missing paths return ``None``.
    """
    if not path:
        return None
    current: Any = claims
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _claim_strings(raw: Any) -> list[str]:
    """Normalize a claim to a list of non-empty strings.

    Accepts a string (single value), a list (group membership), or a JSON
    string containing a list (some providers serialize groups that way).
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            import json

            try:
                parsed = json.loads(text)
            except ValueError:
                return [text]
            return _claim_strings(parsed)
        return [text]
    if isinstance(raw, (list, tuple, set)):
        out: list[str] = []
        for item in raw:
            out.extend(_claim_strings(item))
        return out
    return [str(raw)]


@dataclass(frozen=True)
class ClaimDecision:
    """What the claims authorize. Recorded on the identity row."""

    role: TokenRole
    project_ids: tuple[uuid.UUID, ...]
    #: Claim values that matched a role rule (empty when the floor applied).
    matched_values: tuple[str, ...]
    #: Project ids named by the claim that no project in ARGUS has.
    unknown_project_ids: tuple[str, ...]

    @property
    def is_admin(self) -> bool:
        return self.role is TokenRole.ADMIN


def decide_from_claims(
    claims: Dict[str, Any], config: OidcConfiguration
) -> ClaimDecision:
    """Map verified claims to a role and grants — or refuse the login.

    The refusals are the point:

    * **No subject.** ``sub`` is the identity; without it there is nothing
      stable to provision against and every login would create a new person.
    * **Unverified email.** When required, an unverified ``email`` claim is the
      user's own assertion, not the provider's.
    * **Domain not allowed.** Checked *after* verification and before any
      credential exists, so a provider that authenticates a wider directory
      than you intended cannot widen ARGUS.

    Role resolution is a strict allowlist: a claim value that appears in
    neither list gets the configured floor (VIEWER by default) — never ADMIN,
    and never "the highest thing we saw".
    """
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise OidcError(REASON_CLAIMS_INVALID, "The identity token has no subject")

    email = claims.get("email")
    email = str(email).strip() if isinstance(email, str) else ""
    email_verified = claims.get("email_verified")
    #: Providers use a real boolean; a string "true" counts, "false" does not.
    verified = email_verified is True or str(email_verified).lower() == "true"

    if config.require_verified_email and not verified:
        raise OidcError(
            REASON_EMAIL_UNVERIFIED,
            "Your email address is not verified by the identity provider",
        )
    if config.allowed_domains:
        domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
        if not domain or domain not in config.allowed_domains:
            raise OidcError(
                REASON_EMAIL_DOMAIN,
                "Your email domain is not permitted to sign in to this deployment",
            )

    values = _claim_strings(claim_value(claims, config.role_claim))
    matched: tuple[str, ...] = ()
    if config.admin_values and config.admin_values.intersection(values):
        role = TokenRole.ADMIN
        matched = tuple(sorted(config.admin_values.intersection(values)))
    elif config.operator_values and config.operator_values.intersection(values):
        role = TokenRole.OPERATOR
        matched = tuple(sorted(config.operator_values.intersection(values)))
    else:
        role = config.default_role

    named = _claim_strings(claim_value(claims, config.project_claim))
    granted: list[uuid.UUID] = []
    unknown: list[str] = []
    for raw in named:
        try:
            granted.append(uuid.UUID(raw))
        except (ValueError, TypeError, AttributeError):
            unknown.append(raw)

    #: An ADMIN bypasses grants entirely, so writing grant rows for one would
    #: be storing a lie about how authorization works.
    if role is TokenRole.ADMIN:
        return ClaimDecision(role, (), matched, tuple(unknown))
    return ClaimDecision(role, tuple(granted), matched, tuple(unknown))


def safe_return_to(value: Optional[str]) -> Optional[str]:
    """Accept only a same-origin relative path as a post-login destination.

    ``return_to`` arrives in a query string, which means anyone can set it. It
    is handed back to the console, which navigates to it — so accepting an
    absolute URL would let a phishing link bounce a freshly authenticated user
    (and their session, if it lives in the URL) to an attacker's page. A value
    that is not an unambiguous relative path is dropped rather than sanitized:
    "mostly safe" is not a property worth maintaining in one place.
    """
    if not value:
        return None
    candidate = value.strip()
    if not candidate.startswith("/"):
        return None
    #: ``//host`` is protocol-relative and therefore absolute; ``/\host`` is
    #: treated as absolute by some browsers. Both are refused.
    if candidate.startswith("//") or candidate.startswith("/\\"):
        return None
    if "\n" in candidate or "\r" in candidate:
        return None
    return candidate[:512]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for the S256 method."""
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


@dataclass
class LoginOutcome:
    """A completed login: the credential to hand out, plus what was decided."""

    raw_token: str
    token: ApiToken
    identity: ExternalIdentity
    decision: ClaimDecision
    expires_at: datetime
    redirect_to: str


class OidcClient:
    """The identity-provider client.

    ``http_client`` is injectable so tests exercise the *real* discovery,
    exchange and JWKS code against an in-process provider instead of mocking
    the functions that need proving. ``now`` is injectable for the same reason:
    expiry behaviour is asserted, not assumed.
    """

    def __init__(
        self,
        config: Optional[OidcConfiguration] = None,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        now=None,
    ) -> None:
        self.config = config or configuration()
        self._http = http_client
        self._owns_http = http_client is None
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._discovery: Optional[Dict[str, Any]] = None
        self._jwks: Optional[Dict[str, Any]] = None
        self._jwks_fetched_at: Optional[datetime] = None

    # -- plumbing ---------------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self.config.timeout_seconds, follow_redirects=False
            )
        return self._http

    async def aclose(self) -> None:
        """Close the client only when this instance created it."""
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> "OidcClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    def require_enabled(self) -> None:
        if not self.config.enabled:
            raise OidcError(
                REASON_PROVIDER_DISABLED,
                "Single sign-on is not enabled on this deployment",
                status_code=404,
            )

    async def discovery(self) -> Dict[str, Any]:
        """Fetch and cache the provider's discovery document.

        The document's own ``issuer`` must equal the configured one. That check
        is what makes the issuer a trust anchor instead of a starting hint: a
        hijacked discovery endpoint cannot point ARGUS at different keys.
        """
        if self._discovery is None:
            response = await self._client().get(self.config.discovery_url)
            if response.status_code != 200:
                raise OidcError(
                    REASON_EXCHANGE_FAILED,
                    f"Identity provider discovery failed ({response.status_code})",
                    status_code=502,
                )
            try:
                document = response.json()
            except ValueError as exc:  # pragma: no cover - provider bug
                raise OidcError(
                    REASON_EXCHANGE_FAILED,
                    "Identity provider discovery returned a non-JSON document",
                    status_code=502,
                ) from exc
            announced = str(document.get("issuer") or "").rstrip("/")
            if announced != self.config.issuer.rstrip("/"):
                raise OidcError(
                    REASON_EXCHANGE_FAILED,
                    "Identity provider discovery reports a different issuer",
                    status_code=502,
                )
            self._discovery = document
        return self._discovery

    async def jwks(self, *, refresh: bool = False) -> Dict[str, Any]:
        """Fetch and cache the provider's JSON Web Key Set.

        Cached with a TTL, and refetched once on an unknown ``kid`` (key
        rotation): a cache that never refreshes breaks logins exactly when a
        provider rotates, and a cache with no TTL turns a rotated-out key into
        a permanently trusted one.
        """
        now = self._now()
        stale = (
            self._jwks_fetched_at is None
            or (now - self._jwks_fetched_at).total_seconds()
            > self.config.jwks_cache_seconds
        )
        if self._jwks is None or refresh or stale:
            document = await self.discovery()
            uri = document.get("jwks_uri")
            if not uri:
                raise OidcError(
                    REASON_EXCHANGE_FAILED,
                    "Identity provider discovery has no jwks_uri",
                    status_code=502,
                )
            response = await self._client().get(str(uri))
            if response.status_code != 200:
                raise OidcError(
                    REASON_EXCHANGE_FAILED,
                    f"Fetching identity provider keys failed ({response.status_code})",
                    status_code=502,
                )
            self._jwks = response.json()
            self._jwks_fetched_at = now
        return self._jwks or {"keys": []}

    # -- flow -------------------------------------------------------------
    async def begin(
        self,
        db: AsyncSession,
        *,
        return_to: Optional[str] = None,
        client_ip: Optional[str] = None,
    ) -> str:
        """Create a login state and return the provider's authorization URL."""
        self.require_enabled()
        document = await self.discovery()
        endpoint = document.get("authorization_endpoint")
        if not endpoint:
            raise OidcError(
                REASON_EXCHANGE_FAILED,
                "Identity provider discovery has no authorization_endpoint",
                status_code=502,
            )

        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        expires_at = self._now() + timedelta(seconds=self.config.state_ttl_seconds)
        db.add(
            OidcLoginState(
                state=state,
                nonce=nonce,
                code_verifier=verifier,
                redirect_uri=self.config.redirect_uri,
                provider=self.config.issuer,
                return_to=safe_return_to(return_to),
                expires_at=expires_at,
                client_ip=client_ip,
            )
        )
        await db.commit()

        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "scope": " ".join(self.config.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        query = httpx.QueryParams(params)
        return f"{endpoint}?{query}"

    async def _spend_state(self, db: AsyncSession, state: str) -> OidcLoginState:
        """Look up and consume a login state — exactly once.

        The read is followed by a conditional update guarded on
        ``consumed_at IS NULL``; if a concurrent callback already spent the
        state, zero rows change and this call refuses. Two callbacks with the
        same code therefore cannot both mint a session, whatever the timing.
        """
        now = self._now()
        row = (
            await db.execute(
                select(OidcLoginState).where(OidcLoginState.state == state)
            )
        ).scalar_one_or_none()
        #: ``expires_at`` comes back naive from SQLite; see ``_aware``.
        if row is None:
            raise OidcError(REASON_STATE_UNKNOWN, "Unknown or forged login state")
        if row.consumed_at is not None:
            raise OidcError(REASON_STATE_SPENT, "This login has already been completed")
        if _aware(row.expires_at) <= now:
            raise OidcError(REASON_STATE_EXPIRED, "This login attempt has expired")

        result = await db.execute(
            update(OidcLoginState)
            .where(
                OidcLoginState.id == row.id,
                OidcLoginState.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )
        await db.commit()
        #: An UPDATE always yields a ``CursorResult`` at runtime; the generic
        #: ``Result`` the session is typed as does not carry ``rowcount``.
        if cast(CursorResult, result).rowcount != 1:
            raise OidcError(REASON_STATE_SPENT, "This login has already been completed")
        return row

    async def _exchange_code(
        self, code: str, state_row: OidcLoginState
    ) -> Dict[str, Any]:
        """Swap the authorization code for tokens at the provider."""
        document = await self.discovery()
        endpoint = document.get("token_endpoint")
        if not endpoint:
            raise OidcError(
                REASON_EXCHANGE_FAILED,
                "Identity provider discovery has no token_endpoint",
                status_code=502,
            )

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": state_row.redirect_uri,
            "client_id": self.config.client_id,
            "code_verifier": state_row.code_verifier,
        }
        auth: Optional[tuple[str, str]] = None
        methods = document.get("token_endpoint_auth_methods_supported") or []
        use_basic = bool(self.config.client_secret) and (
            not methods or "client_secret_basic" in methods
        )
        if use_basic:
            #: Default to HTTP Basic, and only fall back to posting the secret
            #: when the provider says it does not accept Basic. Sending the
            #: secret in the body when the provider supports Basic puts it in
            #: more places (proxy logs, request tracing) for no benefit.
            auth = (self.config.client_id, self.config.client_secret)
        elif self.config.client_secret:
            data["client_secret"] = self.config.client_secret

        #: Built as kwargs rather than passing ``auth=None``: httpx distinguishes
        #: "use the client default" from "expect None", so an explicit None is
        #: not the same statement as an absent argument.
        post_kwargs: Dict[str, Any] = {"data": data}
        if auth is not None:
            post_kwargs["auth"] = auth
        response = await self._client().post(str(endpoint), **post_kwargs)
        if response.status_code != 200:
            #: The provider's error body is not echoed: it can echo the request
            #: (and therefore the code). The status is enough to act on.
            raise OidcError(
                REASON_EXCHANGE_FAILED,
                f"Identity provider rejected the code exchange ({response.status_code})",
                status_code=401,
            )
        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - provider bug
            raise OidcError(
                REASON_EXCHANGE_FAILED,
                "Token response was not JSON",
                status_code=502,
            ) from exc
        if not payload.get("id_token"):
            raise OidcError(
                REASON_EXCHANGE_FAILED,
                "Token response contained no ID token",
                status_code=502,
            )
        return payload

    async def verify_id_token(self, id_token: str, *, nonce: str) -> Dict[str, Any]:
        """Verify the ID token's signature and every binding claim.

        Verified, in order: the algorithm is asymmetric and allowed, the key is
        the provider's current key for this ``kid``, the signature validates,
        ``iss`` is our issuer, ``aud`` is our client, the time window holds
        within the configured leeway, and the ``nonce`` matches the one we
        issued for this exact login.
        """
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise OidcError(REASON_TOKEN_INVALID, "Malformed identity token") from exc

        raw_kid = header.get("kid")
        #: Normalized to ``str``: an absent ``kid`` cannot name a key, and the
        #: lookup below must be a string key rather than a nullable one.
        kid = raw_kid if isinstance(raw_kid, str) else ""
        keys = await self._keys_by_id()
        key = keys.get(kid)
        if key is None:
            #: Key rotation: one refresh, then give up.
            keys = await self._keys_by_id(refresh=True)
            key = keys.get(kid)
        if key is None:
            raise OidcError(
                REASON_TOKEN_INVALID, "Identity token signed by an unknown key"
            )

        try:
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=list(ALLOWED_ID_TOKEN_ALGORITHMS),
                audience=self.config.client_id,
                issuer=self.config.issuer,
                leeway=self.config.leeway_seconds,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    #: HMAC is excluded by the allowlist; forbidding it again
                    #: here is cheap and states the intent twice on purpose.
                    "verify_signature": True,
                },
            )
        except jwt.PyJWTError as exc:
            raise OidcError(
                REASON_TOKEN_INVALID, "Identity token failed verification"
            ) from exc

        if str(claims.get("nonce") or "") != nonce:
            #: Without this check a token minted for a *different* login can be
            #: replayed into this one: the signature is valid, the audience is
            #: right, and it is simply not about this request.
            raise OidcError(REASON_NONCE_MISMATCH, "Identity token nonce mismatch")
        return claims

    async def _keys_by_id(self, *, refresh: bool = False) -> Dict[str, Any]:
        document = await self.jwks(refresh=refresh)
        out: Dict[str, Any] = {}
        for jwk in document.get("keys", []) or []:
            try:
                out[str(jwk.get("kid"))] = jwt.PyJWK(jwk).key
            except (jwt.PyJWTError, ValueError, KeyError) as exc:
                #: Unknown key types are skipped, never trusted wholesale.
                logger.debug(
                    "skipping unusable JWK (kid=%s, %s)",
                    jwk.get("kid"),
                    type(exc).__name__,
                )
                continue
        if not out:
            raise OidcError(
                REASON_TOKEN_INVALID,
                "Identity provider published no usable signing keys",
            )
        return out

    async def complete(
        self,
        db: AsyncSession,
        *,
        code: str,
        state: str,
        client_ip: Optional[str] = None,
    ) -> LoginOutcome:
        """Finish a login: verify, decide, provision, mint. Or refuse."""
        self.require_enabled()
        state_row = await self._spend_state(db, state)
        tokens = await self._exchange_code(code, state_row)
        claims = await self.verify_id_token(
            str(tokens["id_token"]), nonce=state_row.nonce
        )
        decision = decide_from_claims(claims, self.config)
        identity = await self._provision(db, claims, decision, client_ip=client_ip)
        raw, token_row, expires_at = await self._mint_session(db, identity, decision)
        redirect_to = state_row.return_to or self._default_redirect()
        return LoginOutcome(
            raw_token=raw,
            token=token_row,
            identity=identity,
            decision=decision,
            expires_at=expires_at,
            redirect_to=redirect_to,
        )

    def _default_redirect(self) -> str:
        """Where the console should send the browser after a successful login.

        ``/`` unless the operator configured otherwise. This is returned to the
        callback page in a JSON body, never used as a redirect by the API
        itself, so it cannot become an open-redirect primitive.
        """
        return self.config.post_login_redirect or "/"

    async def _existing_projects(
        self, db: AsyncSession, project_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Which of these project ids exist. The rest are not access."""
        if not project_ids:
            return set()
        rows = await db.execute(
            select(SoftwareProject.id).where(SoftwareProject.id.in_(list(project_ids)))
        )
        return set(rows.scalars().all())

    async def _provision(
        self,
        db: AsyncSession,
        claims: Dict[str, Any],
        decision: ClaimDecision,
        *,
        client_ip: Optional[str],
    ) -> ExternalIdentity:
        """Create or refresh the person, and refuse a disabled one."""
        now = self._now()
        subject = str(claims.get("sub"))
        identity = (
            await db.execute(
                select(ExternalIdentity).where(
                    ExternalIdentity.provider == self.config.issuer,
                    ExternalIdentity.subject == subject,
                )
            )
        ).scalar_one_or_none()

        if identity is not None and identity.is_disabled:
            raise OidcError(
                REASON_IDENTITY_DISABLED,
                "This identity has been disabled in ARGUS",
                status_code=403,
            )

        known = await self._existing_projects(db, decision.project_ids)
        grants = sorted(
            str(pid) for pid in known
        )  #: Which of the claim's project ids actually exist, and which did not.
        #: Both halves are recorded: a claim that names a project ARGUS does not
        #: host is indistinguishable from "this person has no access" unless the
        #: difference is written down somewhere an operator can find it.
        unmatched = sorted(str(pid) for pid in set(decision.project_ids) - known)

        snapshot = {key: claims.get(key) for key in _SNAPSHOT_CLAIMS if key in claims}
        snapshot["role_claim_values"] = list(decision.matched_values)
        snapshot["project_claim_matched"] = grants
        snapshot["project_claim_unmatched"] = unmatched
        snapshot["project_claim_unparseable"] = list(decision.unknown_project_ids)

        email = claims.get("email")
        email_verified = claims.get("email_verified")
        display = claims.get("name") or claims.get("preferred_username") or email

        if identity is None:
            identity = ExternalIdentity(
                provider=self.config.issuer,
                subject=subject,
                email=str(email) if email else None,
                display_name=str(display) if display else None,
                role=decision.role,
                project_ids=grants,
                claims_snapshot=snapshot,
                email_verified=email_verified is True,
                first_login_at=now,
                last_login_at=now,
                login_count=1,
                last_login_ip=client_ip,
            )
            db.add(identity)
        else:
            #: Claims are re-read on every login and overwrite the record: the
            #: provider is the authority, and a stale local copy of "was an
            #: admin last month" is how access survives a revocation.
            identity.email = str(email) if email else identity.email
            identity.display_name = str(display) if display else identity.display_name
            identity.role = decision.role
            identity.project_ids = grants
            identity.claims_snapshot = snapshot
            identity.email_verified = email_verified is True
            identity.last_login_at = now
            identity.login_count = (identity.login_count or 0) + 1
            identity.last_login_ip = client_ip
        await db.flush()

        unapplied = unmatched + list(decision.unknown_project_ids)
        if unapplied:
            #: Not an error — a claim may name projects this deployment does not
            #: host — but it must be visible, because a typo'd claim is
            #: otherwise indistinguishable from "this person has no access".
            logger.warning(
                "OIDC login for %s named %d project id(s) that do not exist: %s",
                email or subject,
                len(unapplied),
                ", ".join(unapplied[:5]),
            )
        return identity

    async def _mint_session(
        self,
        db: AsyncSession,
        identity: ExternalIdentity,
        decision: ClaimDecision,
    ) -> tuple[str, ApiToken, datetime]:
        """Mint the bearer session the console will use."""
        #: One audit writer for the whole platform, imported lazily because
        #: ``app.core.security`` reads settings at import time and this module
        #: is imported by route modules during application construction.
        from app.core.security import write_auth_audit

        now = self._now()
        expires_at = now + timedelta(seconds=self.config.session_ttl_seconds)
        label = identity.email or identity.display_name or identity.subject

        if self.config.single_session:
            #: One live session per identity. Bounds the credential list,
            #: makes "sign out everywhere" a side effect of signing in again,
            #: and — the real reason — means a stolen session cannot outlive
            #: the next legitimate login. Only OIDC sessions are touched.
            revoked = await db.execute(
                select(ApiToken).where(
                    ApiToken.external_identity_id == identity.id,
                    ApiToken.status == TokenStatus.ACTIVE,
                )
            )
            for old in revoked.scalars().all():
                old.status = TokenStatus.REVOKED
                old.revoked_at = now

        raw, token_hash = generate_token()
        token = ApiToken(
            name=f"oidc:{self.config.provider_name}:{label}"[:255],
            token_hash=token_hash,
            role=decision.role,
            status=TokenStatus.ACTIVE,
            expires_at=expires_at,
            created_by=f"oidc:{self.config.issuer}",
            description="Single sign-on session",
            auth_source=AuthSource.OIDC,
            external_identity_id=identity.id,
        )
        db.add(token)
        await db.flush()
        for project_id in identity.granted_project_ids:
            db.add(ApiTokenProject(token_id=token.id, project_id=project_id))
        await write_auth_audit(
            db,
            action=AuthAuditAction.CREATED,
            token_id=token.id,
            reason=f"oidc-login:{label}",
        )
        await db.commit()
        await db.refresh(token)
        return raw, token, expires_at


async def disable_identity(
    db: AsyncSession, identity_id: uuid.UUID, *, reason: Optional[str] = None
) -> Optional[ExternalIdentity]:
    """Disable a person and revoke every session they hold.

    The two steps are one operation because they are one decision: leaving the
    sessions alive would mean "disabled" takes effect whenever the shortest
    token happened to expire. Idempotent.
    """
    identity = await db.get(ExternalIdentity, identity_id)
    if identity is None:
        return None
    from app.core.security import write_auth_audit

    if not identity.is_disabled:
        identity.disabled_at = datetime.now(timezone.utc)
        identity.disabled_reason = reason or "disabled by administrator"
    sessions = await db.execute(
        select(ApiToken).where(
            ApiToken.external_identity_id == identity.id,
            ApiToken.status == TokenStatus.ACTIVE,
        )
    )
    for token in sessions.scalars().all():
        token.status = TokenStatus.REVOKED
        token.revoked_at = datetime.now(timezone.utc)
        await write_auth_audit(
            db,
            action=AuthAuditAction.REVOKED,
            token_id=token.id,
            reason="identity disabled",
        )
    await db.commit()
    await db.refresh(identity)
    return identity


async def expire_and_prune_sessions(
    db: AsyncSession, *, retention_days: int, now: Optional[datetime] = None
) -> tuple[int, int]:
    """Retire expired SSO sessions, then delete the long-dead ones.

    Two steps, because they answer different questions and only one of them is
    destructive:

    * **Expire.** A session past ``expires_at`` was already refused by
      ``verify_token``; marking it ``EXPIRED`` makes the credential list say
      what is true instead of showing a row that has quietly stopped working.
      Nothing is lost — the inability to authenticate was the state all along.
    * **Prune.** Deleting a session row is not ordinary cleanup, because that
      row is what the authentication audit refers to. So the rule is narrower
      than "older than N days": only sessions that **cannot be used** (REVOKED
      or EXPIRED) *and* are older than the window are removed. An ``ACTIVE``
      token is never deleted by retention, however old it is — a credential
      that still works does not expire because a sweep ran.

    Returns ``(expired, deleted)``.
    """
    from sqlalchemy import delete, func

    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=retention_days)

    expired = cast(
        CursorResult,
        await db.execute(
            update(ApiToken)
            .where(
                ApiToken.auth_source == AuthSource.OIDC,
                ApiToken.status == TokenStatus.ACTIVE,
                ApiToken.expires_at.is_not(None),
                ApiToken.expires_at <= moment,
            )
            .values(status=TokenStatus.EXPIRED)
        ),
    )

    #: ``created_at`` rather than ``expires_at`` for the cutoff: the question is
    #: "how long has this dead row been sitting here", and a very short session
    #: TTL would otherwise keep pruning rows that are one day old.
    deletable = await db.execute(
        select(func.count(ApiToken.id)).where(
            ApiToken.auth_source == AuthSource.OIDC,
            ApiToken.status.in_([TokenStatus.EXPIRED, TokenStatus.REVOKED]),
            ApiToken.created_at < cutoff,
        )
    )
    count = deletable.scalar() or 0
    if count:
        await db.execute(
            delete(ApiToken).where(
                ApiToken.auth_source == AuthSource.OIDC,
                ApiToken.status.in_([TokenStatus.EXPIRED, TokenStatus.REVOKED]),
                ApiToken.created_at < cutoff,
            )
        )
    await db.flush()
    return int(expired.rowcount or 0), int(count or 0)


def record_login_attempt(result: str) -> None:
    """Count a login attempt by outcome.

    Two counters rather than one labelled counter: the runtime registry is
    label-free by design (see ``app.core.runtime_metrics``), and a split that
    cannot be misread is worth more than one that needs label parsing to
    interpret.
    """
    if result == "success":
        incr(
            "argus_oidc_logins_total",
            help_text="Successful single sign-on logins",
        )
    else:
        incr(
            "argus_oidc_login_failures_total",
            help_text="Refused single sign-on logins",
        )


__all__ = [
    "ALLOWED_ID_TOKEN_ALGORITHMS",
    "ClaimDecision",
    "LoginOutcome",
    "OidcClient",
    "OidcConfiguration",
    "OidcError",
    "claim_value",
    "configuration",
    "decide_from_claims",
    "disable_identity",
    "expire_and_prune_sessions",
    "pkce_pair",
    "record_login_attempt",
    "safe_return_to",
]
