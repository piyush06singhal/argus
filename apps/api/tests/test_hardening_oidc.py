"""Hardening W2 — single sign-on (OIDC).

The tests run the **real** client code against an in-process identity provider:
discovery, the token exchange, JWKS lookup and signature verification all
execute exactly as they do in production, over HTTP, through
``httpx.MockTransport``. Nothing in :mod:`app.services.oidc` is monkeypatched,
because the parts worth proving are precisely the parts a fake would bypass.

The provider is hostile where it has to be. It can be told to publish a
different issuer, rotate its key, sign with HMAC, emit ``alg: none``, mint a
token for somebody else's audience, or hand back a token whose nonce belongs to
a different login — and the client must refuse every one of them.

Refusals are asserted on the *stable reason string*, not on prose: the reason is
what reaches the audit trail and the operator's grep, so it is the contract.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from app.services.oidc import (
    ALLOWED_ID_TOKEN_ALGORITHMS,
    OidcClient,
    OidcError,
    claim_value,
    configuration,
    decide_from_claims,
    safe_return_to,
)

from tests.phase6_helpers import build_project

ISSUER = "https://idp.example.test"
JWKS_URI = f"{ISSUER}/jwks"
AUTHORIZE_ENDPOINT = f"{ISSUER}/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/token"
CLIENT_ID = "argus-console"
#: Long enough for HS256 to be legitimate. A short secret here would make the
#: *forged-token* test warning about key length, which trains a reader to ignore
#: cryptography warnings in the suite.
CLIENT_SECRET = "s3cret-shared-with-the-provider-0123456789abcd"
REDIRECT_URI = "http://localhost:3000/auth/callback"


@pytest.fixture
def enforce_auth(monkeypatch):
    """Turn the real auth funnel on for one test (the same seam W1 uses).

    Defined here rather than imported so this module proves the behaviour of
    the real middleware against these routes, exactly as the W1 suite does. A
    test that reused another module's fixture would still be running *its*
    fixtures, which is not a distinction worth reasoning about.
    """
    from app.core import edge

    monkeypatch.setattr(edge, "_AUTH_DISABLED_OVERRIDE", False)
    yield


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _int_to_b64(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return _b64url(value.to_bytes(length, "big"))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FakeProvider:
    """A minimal, deliberately configurable OpenID Connect provider.

    Only the parts ARGUS uses are implemented, and each hostile behaviour is a
    flag rather than a separate fake: one provider, many lies, so a passing test
    cannot be explained by which fake was used.
    """

    def __init__(self, *, kid: str = "test-key-1") -> None:
        self.kid = kid
        self._private: RSAPrivateKey = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        #: Knobs the tests turn to make the provider misbehave.
        self.announced_issuer: str = ISSUER
        self.expected_audience: str = CLIENT_ID
        self.algorithm: str = "RS256"
        self.published_kids: Optional[list[str]] = None
        self.nonce: Optional[str] = None
        self.expires_in: int = 300
        self.claim_overrides: dict[str, Any] = {}
        self.refuse_exchange: bool = False
        self.tokens: dict[str, dict[str, Any]] = {}
        #: Set when the provider received a verifier it did not expect.
        self.bad_verifier = False
        self.exchanges = 0

    # -- keys -------------------------------------------------------------
    def jwk(self, kid: Optional[str] = None) -> dict[str, Any]:
        numbers = self._private.public_key().public_numbers()
        return {
            "kty": "RSA",
            "use": "sig",
            "alg": self.algorithm,
            "kid": kid or self.kid,
            "n": _int_to_b64(numbers.n),
            "e": _int_to_b64(numbers.e),
        }

    @property
    def jwks_document(self) -> dict[str, Any]:
        kids = self.published_kids or [self.kid]
        return {"keys": [self.jwk(kid=kid) for kid in kids]}

    # -- tokens -----------------------------------------------------------
    def issue(
        self,
        *,
        subject: str = "user-123",
        nonce: Optional[str] = None,
        claims: Optional[dict[str, Any]] = None,
        algorithm: Optional[str] = None,
        audience: Optional[str] = None,
        issuer: Optional[str] = None,
        expires_in: Optional[int] = None,
        kid: Optional[str] = None,
    ) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": issuer if issuer is not None else self.announced_issuer,
            "aud": audience if audience is not None else self.expected_audience,
            "sub": subject,
            "iat": now,
            "exp": now + (self.expires_in if expires_in is None else expires_in),
            "email": "dana@example.test",
            "email_verified": True,
            "name": "Dana Ops",
        }
        if nonce is not None:
            payload["nonce"] = nonce
        payload.update(self.claim_overrides)
        payload.update(claims or {})
        alg = algorithm or self.algorithm
        key: Any = self._private
        if alg.startswith("HS"):
            key = CLIENT_SECRET
        elif alg == "none":
            key = ""
        return jwt.encode(payload, key, algorithm=alg, headers={"kid": kid or self.kid})

    # -- HTTP -------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.announced_issuer,
                    "authorization_endpoint": AUTHORIZE_ENDPOINT,
                    "token_endpoint": TOKEN_ENDPOINT,
                    "jwks_uri": JWKS_URI,
                    "token_endpoint_auth_methods_supported": [
                        "client_secret_basic",
                        "client_secret_post",
                    ],
                },
            )
        if path == "/jwks":
            return httpx.Response(200, json=self.jwks_document)
        if path == "/token":
            self.exchanges += 1
            if self.refuse_exchange:
                return httpx.Response(400, json={"error": "invalid_grant"})
            form = dict(httpx.QueryParams(request.content.decode()))
            code = form.get("code", "")
            verifier = form.get("code_verifier", "")
            if form.get("grant_type") != "authorization_code" or not verifier:
                return httpx.Response(400, json={"error": "invalid_request"})
            if not self._client_authenticated(request, form):
                return httpx.Response(401, json={"error": "invalid_client"})
            return httpx.Response(200, json=self.tokens.get(code, {}))
        return httpx.Response(404, json={"error": "not_found"})

    def _client_authenticated(
        self, request: httpx.Request, form: dict[str, Any]
    ) -> bool:
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            decoded = base64.b64decode(header[6:]).decode()
            return decoded == f"{CLIENT_ID}:{CLIENT_SECRET}"
        return form.get("client_secret") == CLIENT_SECRET


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def oidc_settings(monkeypatch, provider: FakeProvider):
    """Enable SSO for one test, pointed at the fake provider.

    Configured through the **environment**, not by mutating a settings object.
    ``get_settings()`` builds a fresh ``Settings`` on every call, so patching
    an instance leaves every code path that reads settings for itself — which
    includes the HTTP routes — reading the unpatched environment. That is not a
    subtlety worth re-discovering: this fixture is the only place these values
    are set, and it sets them where the application actually looks.

    The returned object is a real ``Settings`` built from that environment, so
    a test that needs to adjust one value can still do so explicitly before
    building its client.
    """
    from app.core.config import Settings

    environment = {
        "OIDC_ENABLED": "true",
        "OIDC_ISSUER": ISSUER,
        "OIDC_CLIENT_ID": CLIENT_ID,
        "OIDC_CLIENT_SECRET": CLIENT_SECRET,
        "OIDC_REDIRECT_URI": REDIRECT_URI,
        "OIDC_PROVIDER_NAME": "Test IdP",
        "OIDC_ROLE_CLAIM": "groups",
        "OIDC_ADMIN_CLAIM_VALUES": json.dumps(["argus-admins"]),
        "OIDC_OPERATOR_CLAIM_VALUES": json.dumps(["argus-operators"]),
        "OIDC_ALLOWED_EMAIL_DOMAINS": json.dumps([]),
        "OIDC_REQUIRE_VERIFIED_EMAIL": "true",
        "OIDC_DEFAULT_ROLE": "VIEWER",
        "OIDC_PROJECT_CLAIM": "argus_projects",
        "OIDC_SINGLE_SESSION": "true",
        "OIDC_SESSION_TTL_SECONDS": "3600",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    return Settings()


@pytest.fixture
def build_client(provider: FakeProvider, oidc_settings):
    """Build a client **after** a test has finished adjusting the settings.

    ``OidcConfiguration`` is a frozen snapshot by design — one place turns the
    environment into the values the flow uses. That means a test which changes
    a setting has to build its client afterwards, or it would be asserting
    against the configuration it started with. The factory makes the ordering
    explicit instead of leaving it to fixture resolution order.

    The transport intercepts HTTP while leaving every line of the client's own
    request construction, discovery caching, JWKS handling and verification in
    the path under test.
    """

    def _build() -> OidcClient:
        return OidcClient(
            configuration(oidc_settings),
            http_client=httpx.AsyncClient(transport=provider.transport()),
        )

    return _build


@pytest.fixture
def oidc_client(build_client) -> OidcClient:
    """The default client, with the fixture's configuration as-is."""
    return build_client()


async def _authorize(
    client: OidcClient, provider: FakeProvider, db_session, *, claims=None, **kwargs
):
    """Drive begin() → provider → complete() and return the login outcome."""
    url = await client.begin(db_session)
    params = httpx.QueryParams(url.split("?", 1)[1])
    code = f"code-{uuid.uuid4().hex[:8]}"
    provider.tokens[code] = {
        "id_token": provider.issue(nonce=params["nonce"], claims=claims, **kwargs),
        "access_token": "opaque",
        "token_type": "Bearer",
    }
    return await client.complete(db_session, code=code, state=params["state"])


# ---------------------------------------------------------------------------
# The happy path — and the shape of what it mints
# ---------------------------------------------------------------------------


class TestSuccessfulLogin:
    async def test_login_url_carries_pkce_and_a_fresh_state(
        self, oidc_client: OidcClient, db_session
    ) -> None:
        url = await oidc_client.begin(db_session)

        assert url.startswith(AUTHORIZE_ENDPOINT)
        params = httpx.QueryParams(url.split("?", 1)[1])
        assert params["response_type"] == "code"
        assert params["client_id"] == CLIENT_ID
        assert params["code_challenge_method"] == "S256"
        assert params["code_challenge"]
        assert params["state"] and params["nonce"]
        #: The verifier is the secret that proves *this* client started the
        #: flow. It must never appear in a URL the browser handles.
        assert "code_verifier" not in url

    async def test_the_verifier_is_stored_server_side_for_the_exchange(
        self, oidc_client: OidcClient, db_session
    ) -> None:
        from sqlalchemy import select

        from app.models.oidc import OidcLoginState

        url = await oidc_client.begin(db_session)
        params = httpx.QueryParams(url.split("?", 1)[1])
        row = (
            await db_session.execute(
                select(OidcLoginState).where(OidcLoginState.state == params["state"])
            )
        ).scalar_one()
        assert row.code_verifier
        assert row.consumed_at is None

    async def test_a_verified_login_mints_a_working_session(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        from app.core.security import verify_token
        from app.models.auth import AuthSource, TokenRole

        outcome = await _authorize(oidc_client, provider, db_session)

        assert outcome.raw_token.startswith("argus_")
        assert outcome.token.auth_source is AuthSource.OIDC
        assert outcome.token.external_identity_id == outcome.identity.id
        resolved = await verify_token(db_session, outcome.raw_token)
        assert resolved is not None
        token, grants = resolved
        assert token.role is TokenRole.VIEWER
        assert grants == set()

    async def test_the_role_comes_from_the_claim_allowlist(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        from app.models.auth import TokenRole

        outcome = await _authorize(
            oidc_client,
            provider,
            db_session,
            claims={"groups": ["everyone", "argus-operators"]},
        )
        assert outcome.decision.role is TokenRole.OPERATOR

        provider.claim_overrides = {"groups": ["argus-admins"]}
        second = await _authorize(oidc_client, provider, db_session)
        assert second.decision.role is TokenRole.ADMIN
        #: An ADMIN bypasses grants, so no grant rows are written at all — the
        #: absence is the honest representation of how authorization works.
        assert second.token.id is not None
        assert second.decision.project_ids == ()

    async def test_project_grants_come_from_the_claim_and_must_exist(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        """A claim naming a foreign project must not create access."""
        from app.core.security import verify_token
        from app.models.auth import ApiTokenProject
        from sqlalchemy import select

        project, _, _ = await build_project(db_session)
        stranger = uuid.uuid4()

        provider.claim_overrides = {
            "groups": ["argus-operators"],
            "argus_projects": [str(project.id), str(stranger), "not-a-uuid"],
        }
        outcome = await _authorize(oidc_client, provider, db_session)

        grants = (
            (
                await db_session.execute(
                    select(ApiTokenProject.project_id).where(
                        ApiTokenProject.token_id == outcome.token.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert grants == [project.id]

        resolved = await verify_token(db_session, outcome.raw_token)
        assert resolved is not None
        assert resolved[1] == {project.id}

        #: Both kinds of "named but not granted" are recorded on the identity,
        #: so a typo in a claim is visible to an operator instead of looking
        #: exactly like a person who simply has no access.
        snapshot = outcome.identity.claims_snapshot
        assert snapshot["project_claim_matched"] == [str(project.id)]
        assert str(stranger) in snapshot["project_claim_unmatched"]
        assert "not-a-uuid" in snapshot["project_claim_unparseable"]


# ---------------------------------------------------------------------------
# Replay, forgery and expiry — the refusals that matter most
# ---------------------------------------------------------------------------


class TestStateHandling:
    async def test_a_state_cannot_be_spent_twice(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        """The single-use property, which is why the state lives in the DB."""
        url = await oidc_client.begin(db_session)
        params = httpx.QueryParams(url.split("?", 1)[1])
        code = "code-once"
        provider.tokens[code] = {
            "id_token": provider.issue(nonce=params["nonce"]),
            "access_token": "a",
        }
        first = await oidc_client.complete(db_session, code=code, state=params["state"])
        assert first.raw_token

        with pytest.raises(OidcError) as excinfo:
            await oidc_client.complete(db_session, code=code, state=params["state"])
        assert excinfo.value.reason == "state_already_used"

    async def test_an_unknown_state_is_refused(
        self, oidc_client: OidcClient, db_session
    ) -> None:
        with pytest.raises(OidcError) as excinfo:
            await oidc_client.complete(db_session, code="c", state="forged")
        assert excinfo.value.reason == "state_unknown"

    async def test_an_expired_state_is_refused(
        self, oidc_client: OidcClient, db_session
    ) -> None:
        from sqlalchemy import select

        from app.models.oidc import OidcLoginState

        url = await oidc_client.begin(db_session)
        params = httpx.QueryParams(url.split("?", 1)[1])
        row = (
            await db_session.execute(
                select(OidcLoginState).where(OidcLoginState.state == params["state"])
            )
        ).scalar_one()
        row.expires_at = utcnow() - timedelta(seconds=1)
        await db_session.commit()

        with pytest.raises(OidcError) as excinfo:
            await oidc_client.complete(db_session, code="c", state=params["state"])
        assert excinfo.value.reason == "state_expired"

    async def test_a_redirect_guess_is_rejected_as_an_open_redirect(self) -> None:
        assert safe_return_to("/incidents?x=1") == "/incidents?x=1"
        assert safe_return_to(None) is None
        #: Absolute, protocol-relative and header-splitting values are dropped
        #: rather than sanitized.
        assert safe_return_to("https://evil.test/") is None
        assert safe_return_to("//evil.test") is None
        assert safe_return_to("/\\evil.test") is None
        assert safe_return_to("/ok\r\nSet-Cookie: x=1") is None


class TestTokenVerification:
    async def test_a_tampered_signature_is_refused(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        url = await oidc_client.begin(db_session)
        params = httpx.QueryParams(url.split("?", 1)[1])
        issued = provider.issue(nonce=params["nonce"])
        head, payload, signature = issued.split(".")
        #: Flip one character of the signature, keeping it well-formed base64url.
        flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
        code = "code-tampered"
        provider.tokens[code] = {
            "id_token": f"{head}.{payload}.{flipped}",
            "access_token": "a",
        }
        with pytest.raises(OidcError) as excinfo:
            await oidc_client.complete(db_session, code=code, state=params["state"])
        assert excinfo.value.reason == "id_token_invalid"

    async def test_an_unsigned_token_is_refused(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        """``alg: none`` is a missing signature, not a weak one."""
        url = await oidc_client.begin(db_session)
        params = httpx.QueryParams(url.split("?", 1)[1])
        unsigned = provider.issue(nonce=params["nonce"], algorithm="none")
        code = "code-none"
        provider.tokens[code] = {"id_token": unsigned, "access_token": "a"}
        with pytest.raises(OidcError) as excinfo:
            await oidc_client.complete(db_session, code=code, state=params["state"])
        assert excinfo.value.reason == "id_token_invalid"

    async def test_an_hmac_token_signed_with_the_client_secret_is_refused(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        """The classic confusion: a symmetric signature using a public value.

        An attacker who knows the client secret (or a public client secret)
        signs a token with HS256. A verifier that picks the algorithm from the
        token header accepts it. ARGUS only ever accepts asymmetric algorithms,
        so the key is looked up in the provider's JWKS and the token fails.
        """
        url = await oidc_client.begin(db_session)
        params = httpx.QueryParams(url.split("?", 1)[1])
        forged = provider.issue(nonce=params["nonce"], algorithm="HS256")
        code = "code-hmac"
        provider.tokens[code] = {"id_token": forged, "access_token": "a"}
        with pytest.raises(OidcError) as excinfo:
            await oidc_client.complete(db_session, code=code, state=params["state"])
        assert excinfo.value.reason == "id_token_invalid"

    def test_no_symmetric_or_unsecured_algorithm_is_ever_allowed(self) -> None:
        assert "none" not in ALLOWED_ID_TOKEN_ALGORITHMS
        assert not [a for a in ALLOWED_ID_TOKEN_ALGORITHMS if a.startswith("HS")]

    async def test_a_token_for_another_audience_is_refused(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        url = await oidc_client.begin(db_session)
        params = httpx.QueryParams(url.split("?", 1)[1])
        code = "code-aud"
        provider.tokens[code] = {
            "id_token": provider.issue(
                nonce=params["nonce"], audience="some-other-client"
            ),
            "access_token": "a",
        }
        with pytest.raises(OidcError) as excinfo:
            await oidc_client.complete(db_session, code=code, state=params["state"])
        assert excinfo.value.reason == "id_token_invalid"

    async def test_a_token_from_another_issuer_is_refused(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        url = await oidc_client.begin(db_session)
        params = httpx.QueryParams(url.split("?", 1)[1])
        code = "code-iss"
        provider.tokens[code] = {
            "id_token": provider.issue(
                nonce=params["nonce"], issuer="https://evil.example.test"
            ),
            "access_token": "a",
        }
        with pytest.raises(OidcError) as excinfo:
            await oidc_client.complete(db_session, code=code, state=params["state"])
        assert excinfo.value.reason == "id_token_invalid"

    async def test_an_expired_token_is_refused(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        url = await oidc_client.begin(db_session)
        params = httpx.QueryParams(url.split("?", 1)[1])
        code = "code-exp"
        provider.tokens[code] = {
            "id_token": provider.issue(nonce=params["nonce"], expires_in=-3600),
            "access_token": "a",
        }
        with pytest.raises(OidcError) as excinfo:
            await oidc_client.complete(db_session, code=code, state=params["state"])
        assert excinfo.value.reason == "id_token_invalid"

    async def test_a_nonce_from_another_login_is_refused(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        """A valid, correctly-signed token that is simply about a different login."""
        url = await oidc_client.begin(db_session)
        params = httpx.QueryParams(url.split("?", 1)[1])
        code = "code-nonce"
        provider.tokens[code] = {
            "id_token": provider.issue(nonce="some-other-nonce"),
            "access_token": "a",
        }
        with pytest.raises(OidcError) as excinfo:
            await oidc_client.complete(db_session, code=code, state=params["state"])
        assert excinfo.value.reason == "nonce_mismatch"

    async def test_an_unknown_signing_key_is_refused(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        url = await oidc_client.begin(db_session)
        params = httpx.QueryParams(url.split("?", 1)[1])
        code = "code-kid"
        provider.tokens[code] = {
            "id_token": provider.issue(nonce=params["nonce"], kid="never-published"),
            "access_token": "a",
        }
        with pytest.raises(OidcError) as excinfo:
            await oidc_client.complete(db_session, code=code, state=params["state"])
        assert excinfo.value.reason == "id_token_invalid"

    async def test_key_rotation_is_recovered_without_a_restart(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        """An unknown ``kid`` triggers one JWKS refetch — providers rotate."""
        await oidc_client.jwks()  # prime the cache with the old key
        provider.kid = "test-key-2"
        outcome = await _authorize(oidc_client, provider, db_session)
        assert outcome.raw_token

    async def test_discovery_that_announces_a_different_issuer_is_refused(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        """The configured issuer is a trust anchor, not a hint."""
        provider.announced_issuer = "https://evil.example.test"
        with pytest.raises(OidcError) as excinfo:
            await oidc_client.begin(db_session)
        assert excinfo.value.reason == "token_exchange_failed"

    async def test_a_refused_exchange_produces_no_credential(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        from sqlalchemy import func, select

        from app.models.auth import ApiToken

        url = await oidc_client.begin(db_session)
        params = httpx.QueryParams(url.split("?", 1)[1])
        provider.refuse_exchange = True
        before = (
            await db_session.execute(select(func.count(ApiToken.id)))
        ).scalar() or 0
        with pytest.raises(OidcError) as excinfo:
            await oidc_client.complete(db_session, code="code-x", state=params["state"])
        assert excinfo.value.reason == "token_exchange_failed"
        after = (
            await db_session.execute(select(func.count(ApiToken.id)))
        ).scalar() or 0
        assert after == before


# ---------------------------------------------------------------------------
# Authorization policy — who may sign in, and as what
# ---------------------------------------------------------------------------


class TestAuthorizationPolicy:
    async def test_an_unverified_email_is_refused(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        provider.claim_overrides = {"email_verified": False}
        with pytest.raises(OidcError) as excinfo:
            await _authorize(oidc_client, provider, db_session)
        assert excinfo.value.reason == "email_not_verified"

    async def test_an_unverified_email_may_be_accepted_when_configured(
        self, build_client, provider: FakeProvider, db_session, oidc_settings
    ) -> None:
        oidc_settings.OIDC_REQUIRE_VERIFIED_EMAIL = False
        provider.claim_overrides = {"email_verified": False}
        outcome = await _authorize(build_client(), provider, db_session)
        assert outcome.raw_token

    async def test_the_email_domain_allowlist_is_enforced(
        self, build_client, provider: FakeProvider, db_session, oidc_settings
    ) -> None:
        oidc_settings.OIDC_ALLOWED_EMAIL_DOMAINS = ["example.test"]
        client = build_client()
        provider.claim_overrides = {"email": "contractor@other.test"}
        with pytest.raises(OidcError) as excinfo:
            await _authorize(client, provider, db_session)
        assert excinfo.value.reason == "email_domain_not_allowed"

        provider.claim_overrides = {"email": "dana@example.test"}
        outcome = await _authorize(client, provider, db_session)
        assert outcome.raw_token

    async def test_a_missing_email_cannot_satisfy_a_domain_allowlist(
        self, build_client, provider: FakeProvider, db_session, oidc_settings
    ) -> None:
        oidc_settings.OIDC_ALLOWED_EMAIL_DOMAINS = ["example.test"]
        provider.claim_overrides = {"email": None}
        with pytest.raises(OidcError) as excinfo:
            await _authorize(build_client(), provider, db_session)
        assert excinfo.value.reason == "email_domain_not_allowed"

    async def test_no_subject_is_refused(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        provider.claim_overrides = {"sub": ""}
        with pytest.raises(OidcError) as excinfo:
            await _authorize(oidc_client, provider, db_session)
        assert excinfo.value.reason == "claims_invalid"

    def test_role_mapping_is_an_allowlist_not_a_guess(self, oidc_settings) -> None:
        from app.models.auth import TokenRole

        config = configuration(oidc_settings)
        cases = [
            (["argus-admins"], TokenRole.ADMIN),
            (["argus-admins", "argus-operators"], TokenRole.ADMIN),
            (["argus-operators"], TokenRole.OPERATOR),
            (["everyone"], TokenRole.VIEWER),
            ([], TokenRole.VIEWER),
            (["ARGUS-ADMINS"], TokenRole.VIEWER),  # claim values are exact
        ]
        for groups, expected in cases:
            decision = decide_from_claims(
                {
                    "sub": "s",
                    "email": "a@example.test",
                    "email_verified": True,
                    "groups": groups,
                },
                config,
            )
            assert decision.role is expected, groups

    def test_dotted_role_claims_are_traversed(self) -> None:
        assert claim_value(
            {"realm_access": {"roles": ["ops"]}}, "realm_access.roles"
        ) == ["ops"]
        assert claim_value({"realm_access": {}}, "realm_access.roles") is None
        assert claim_value({"a": {"b": [1]}}, "a.b.c") is None

    def test_a_nested_role_claim_resolves(self, oidc_settings) -> None:
        """Providers nest roles; a flat lookup would silently grant the floor."""
        from app.models.auth import TokenRole

        oidc_settings.OIDC_ROLE_CLAIM = "realm_access.roles"
        config = configuration(oidc_settings)
        decision = decide_from_claims(
            {
                "sub": "s",
                "email": "a@example.test",
                "email_verified": True,
                "realm_access": {"roles": ["argus-operators"]},
            },
            config,
        )
        assert decision.role is TokenRole.OPERATOR

    def test_the_configured_default_role_is_honoured_exactly(
        self, oidc_settings
    ) -> None:
        """A deliberate floor is not silently downgraded."""
        from app.models.auth import TokenRole

        oidc_settings.OIDC_DEFAULT_ROLE = "OPERATOR"
        config = configuration(oidc_settings)
        assert config.default_role is TokenRole.OPERATOR
        oidc_settings.OIDC_DEFAULT_ROLE = "not-a-role"
        assert configuration(oidc_settings).default_role is TokenRole.VIEWER


# ---------------------------------------------------------------------------
# Identity lifecycle — the disable switch
# ---------------------------------------------------------------------------


class TestIdentityLifecycle:
    async def test_claims_are_re_read_on_every_login(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        """A revocation at the provider must take effect at the next login."""
        from app.models.auth import TokenRole

        provider.claim_overrides = {"groups": ["argus-admins"]}
        first = await _authorize(oidc_client, provider, db_session)
        assert first.decision.role is TokenRole.ADMIN

        provider.claim_overrides = {"groups": []}
        second = await _authorize(oidc_client, provider, db_session)
        assert second.decision.role is TokenRole.VIEWER
        #: Same person, refreshed — not a second identity.
        assert second.identity.id == first.identity.id
        assert second.identity.login_count == 2

    async def test_a_renewed_session_revokes_the_previous_one(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        from app.core.security import verify_token
        from app.models.auth import TokenStatus

        first = await _authorize(oidc_client, provider, db_session)
        second = await _authorize(oidc_client, provider, db_session)

        assert second.raw_token != first.raw_token
        assert await verify_token(db_session, first.raw_token) is None
        assert await verify_token(db_session, second.raw_token) is not None
        await db_session.refresh(first.token)
        assert first.token.status is TokenStatus.REVOKED

    async def test_disabling_an_identity_revokes_every_session(
        self, build_client, provider: FakeProvider, db_session, oidc_settings
    ) -> None:
        from app.core.security import verify_token
        from app.services.oidc import disable_identity

        oidc_settings.OIDC_SINGLE_SESSION = False  # two live sessions
        client = build_client()
        first = await _authorize(client, provider, db_session)
        second = await _authorize(client, provider, db_session)
        assert await verify_token(db_session, first.raw_token) is not None
        assert await verify_token(db_session, second.raw_token) is not None

        disabled = await disable_identity(db_session, first.identity.id)
        assert disabled is not None and disabled.is_disabled
        assert await verify_token(db_session, first.raw_token) is None
        assert await verify_token(db_session, second.raw_token) is None

    async def test_a_disabled_identity_cannot_log_in_again(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        from app.services.oidc import disable_identity

        outcome = await _authorize(oidc_client, provider, db_session)
        await disable_identity(db_session, outcome.identity.id)

        with pytest.raises(OidcError) as excinfo:
            await _authorize(oidc_client, provider, db_session)
        assert excinfo.value.reason == "identity_disabled"

    async def test_login_is_recorded_on_the_authentication_audit(
        self, oidc_client: OidcClient, provider: FakeProvider, db_session
    ) -> None:
        from sqlalchemy import select

        from app.models.auth import AuthenticationAudit

        outcome = await _authorize(oidc_client, provider, db_session)
        rows = (
            (
                await db_session.execute(
                    select(AuthenticationAudit).where(
                        AuthenticationAudit.token_id == outcome.token.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows, "an SSO login must leave an audit trail"
        assert any("oidc-login" in (row.reason or "") for row in rows)


# ---------------------------------------------------------------------------
# The HTTP surface: what is public, what is not, and what it says
# ---------------------------------------------------------------------------


class TestHttpSurface:
    def test_the_configuration_probe_is_reachable_anonymously(
        self, client, enforce_auth
    ) -> None:
        from app.core.security import PUBLIC_BOOTSTRAP_PATHS

        assert "/api/v1/auth/oidc/config" in PUBLIC_BOOTSTRAP_PATHS
        response = client.get("/api/v1/auth/oidc/config")
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is False
        assert body["login_path"] == "/api/v1/auth/oidc/login"
        #: No secret is ever exposed here — this response is unauthenticated.
        assert "client_secret" not in json.dumps(body)

    def test_the_pre_auth_surface_is_exactly_three_paths(self) -> None:
        """A fourth path would be a hole; this test is what notices."""
        from app.core.security import PUBLIC_BOOTSTRAP_PATHS

        assert PUBLIC_BOOTSTRAP_PATHS == frozenset(
            {
                "/api/v1/auth/oidc/config",
                "/api/v1/auth/oidc/login",
                "/api/v1/auth/oidc/callback",
            }
        )

    def test_identity_management_is_not_public(self, client, enforce_auth) -> None:
        for method, path in (
            ("get", "/api/v1/auth/oidc/identities"),
            ("post", f"/api/v1/auth/oidc/identities/{uuid.uuid4()}/disable"),
            ("post", f"/api/v1/auth/oidc/identities/{uuid.uuid4()}/enable"),
        ):
            response = getattr(client, method)(path)
            assert (
                response.status_code == 401
            ), f"{method.upper()} {path} did not refuse"

    def test_a_disabled_provider_sends_the_browser_back_with_a_reason(
        self, client, enforce_auth
    ) -> None:
        response = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
        assert response.status_code == 302
        assert "error=oidc_disabled" in response.headers["location"]

    def test_the_callback_refuses_a_forged_state_with_a_machine_readable_code(
        self, client, enforce_auth, oidc_settings
    ) -> None:
        response = client.post(
            "/api/v1/auth/oidc/callback",
            json={"code": "whatever", "state": "forged"},
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["error_code"] == "state_unknown"

    async def test_the_admin_listing_returns_provisioned_people(
        self, client, db_session, enforce_auth
    ) -> None:
        from app.core.security import create_token
        from app.models.auth import TokenRole

        _, raw = await create_token(db_session, name="admin", role=TokenRole.ADMIN)
        response = client.get(
            "/api/v1/auth/oidc/identities",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert response.status_code == 200
        assert response.json()["total"] == 0


# ---------------------------------------------------------------------------
# Configuration safety
# ---------------------------------------------------------------------------


class TestConfigurationSafety:
    def test_production_refuses_a_plaintext_issuer(self) -> None:
        """The JWKS is the trust anchor; fetching it over http verifies nothing."""
        from app.core.config import Settings

        with pytest.raises(Exception):
            Settings(
                API_ENVIRONMENT="production",
                OIDC_ENABLED=True,
                OIDC_ISSUER="http://idp.internal",
                OIDC_CLIENT_ID=CLIENT_ID,
                OIDC_REDIRECT_URI=REDIRECT_URI,
            )

    def test_a_half_configured_provider_refuses_to_boot(self) -> None:
        from app.core.config import Settings

        with pytest.raises(Exception):
            Settings(
                API_ENVIRONMENT="test", OIDC_ENABLED=True, OIDC_CLIENT_ID=CLIENT_ID
            )

    def test_disabled_oidc_ignores_empty_settings(self) -> None:
        from app.core.config import Settings

        settings = Settings(API_ENVIRONMENT="test", OIDC_ENABLED=False, OIDC_ISSUER="")
        assert settings.OIDC_ENABLED is False

    def test_a_non_https_issuer_is_allowed_outside_production(self) -> None:
        """A local Keycloak is the normal way to test this."""
        from app.core.config import Settings

        settings = Settings(
            API_ENVIRONMENT="test",
            OIDC_ENABLED=True,
            OIDC_ISSUER="http://localhost:8081/realms/argus",
            OIDC_CLIENT_ID=CLIENT_ID,
            OIDC_REDIRECT_URI=REDIRECT_URI,
        )
        assert settings.OIDC_ENABLED is True


class TestSessionRetention:
    """A busy deployment's credential list must not grow without bound.

    The rule is narrower than "older than N days", and the difference is the
    whole point: a credential that still works is never deleted by a sweep.
    """

    async def test_expired_sessions_are_retired_but_live_ones_are_kept(
        self, db_session
    ) -> None:
        from app.models.auth import ApiToken, AuthSource, TokenRole, TokenStatus
        from app.services.oidc import expire_and_prune_sessions

        now = utcnow()
        live = ApiToken(
            name="live",
            token_hash="a" * 64,
            role=TokenRole.VIEWER,
            status=TokenStatus.ACTIVE,
            expires_at=now + timedelta(hours=1),
            auth_source=AuthSource.OIDC,
        )
        stale = ApiToken(
            name="stale",
            token_hash="b" * 64,
            role=TokenRole.VIEWER,
            status=TokenStatus.ACTIVE,
            expires_at=now - timedelta(hours=1),
            auth_source=AuthSource.OIDC,
        )
        long_lived_machine = ApiToken(
            name="machine",
            token_hash="c" * 64,
            role=TokenRole.OPERATOR,
            status=TokenStatus.ACTIVE,
            #: Old by ``created_at`` and with no expiry at all.
            auth_source=AuthSource.TOKEN,
        )
        db_session.add_all([live, stale, long_lived_machine])
        await db_session.flush()

        expired, deleted = await expire_and_prune_sessions(
            db_session, retention_days=30
        )
        assert expired == 1
        assert deleted == 0  # too new to delete, but no longer "active"

        await db_session.refresh(stale)
        await db_session.refresh(live)
        assert stale.status is TokenStatus.EXPIRED
        assert live.status is TokenStatus.ACTIVE

    async def test_only_dead_sessions_are_deleted(self, db_session) -> None:
        from sqlalchemy import select

        from app.models.auth import ApiToken, AuthSource, TokenRole, TokenStatus
        from app.services.oidc import expire_and_prune_sessions

        old = ApiToken(
            name="old-revoked",
            token_hash="d" * 64,
            role=TokenRole.VIEWER,
            status=TokenStatus.REVOKED,
            auth_source=AuthSource.OIDC,
        )
        still_working = ApiToken(
            name="old-but-working",
            token_hash="e" * 64,
            role=TokenRole.VIEWER,
            status=TokenStatus.ACTIVE,
            auth_source=AuthSource.OIDC,
        )
        db_session.add_all([old, still_working])
        await db_session.flush()
        #: Age both rows well past the window without touching their status.
        old.created_at = utcnow() - timedelta(days=90)
        still_working.created_at = utcnow() - timedelta(days=90)
        await db_session.commit()

        _, deleted = await expire_and_prune_sessions(db_session, retention_days=30)
        assert deleted == 1

        remaining = (
            (
                await db_session.execute(
                    select(ApiToken.name).where(ApiToken.auth_source == AuthSource.OIDC)
                )
            )
            .scalars()
            .all()
        )
        assert remaining == ["old-but-working"]


class TestRetentionSweepIntegration:
    async def test_the_platform_retention_sweep_covers_sso_sessions(
        self, db_session
    ) -> None:
        """The rule is wired into the sweep operators actually run."""
        from app.models.auth import ApiToken, AuthSource, TokenRole, TokenStatus
        from app.services.retention import RetentionService

        dead = ApiToken(
            name="long-dead",
            token_hash="f" * 64,
            role=TokenRole.VIEWER,
            status=TokenStatus.REVOKED,
            auth_source=AuthSource.OIDC,
        )
        db_session.add(dead)
        await db_session.flush()
        dead.created_at = utcnow() - timedelta(days=365)
        await db_session.commit()

        summary = await RetentionService(db_session).run_sweep()
        names = {result.table: result.deleted for result in summary.results}
        assert names.get("oidc_sessions") == 1


def test_the_login_attempt_helpers_move_the_right_counters() -> None:
    """The counters an alert hangs off are exercised, not assumed."""
    from app.core import runtime_metrics
    from app.services.oidc import record_login_attempt

    runtime_metrics.reset()
    record_login_attempt("success")
    record_login_attempt("failure")
    record_login_attempt("failure")
    values = runtime_metrics.snapshot()
    assert values["argus_oidc_logins_total"] == 1
    assert values["argus_oidc_login_failures_total"] == 2
    runtime_metrics.reset()
