"""Hardening W1 — security tests.

The discipline: refuse first. Every test pins one way the platform must say no
— to missing, forged, expired, revoked or ungranted credentials; to oversized
bodies; to burst traffic; to replayed webhook signatures. The last class pins
the public-path set, so a route added tomorrow without coverage is a failing
test rather than a silent hole.

How HTTP enforcement is exercised: the suite runs in the test environment,
where auth is disabled by default (``auth_disabled()`` → ``is_testing``). The
``enforce_auth`` fixture flips the middleware's documented test seam
(``edge._AUTH_DISABLED_OVERRIDE``) so the *real* app, the *real* middleware
and the *real* database are used — no forked code path is tested.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_module
import uuid
from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def enforce_auth(monkeypatch):
    """Turn the real auth funnel on for one test (test seam in edge.py)."""
    from app.core import edge

    monkeypatch.setattr(edge, "_AUTH_DISABLED_OVERRIDE", False)
    yield


def _bearer(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


async def _mk_token(db_session, *, role="ADMIN", name="t", project_ids=None):
    from app.core.security import create_token
    from app.models.auth import TokenRole

    token, raw = await create_token(
        db_session,
        name=name,
        role=TokenRole(role),
        project_ids=project_ids,
        created_by="test",
    )
    return token, raw


async def _mk_project(db_session, slug: str) -> uuid.UUID:
    from app.models.project import SoftwareProject

    project = SoftwareProject(name=f"Project {slug}", slug=slug)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project.id


async def _mk_source(db_session, project_id: uuid.UUID):
    from app.models.ingestion import ObservabilitySource, ObservabilitySourceCategory

    source = ObservabilitySource(
        project_id=project_id,
        name=f"collector-{uuid.uuid4().hex[:6]}",
        source_type=ObservabilitySourceCategory.OTEL,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


# ---------------------------------------------------------------------------
# Token service unit behaviour
# ---------------------------------------------------------------------------


class TestTokenService:
    async def test_created_token_authenticates(self, db_session):
        from app.core.security import verify_token

        token, raw = await _mk_token(db_session)
        resolved = await verify_token(db_session, raw)
        assert resolved is not None
        resolved_token, grants = resolved
        assert resolved_token.id == token.id
        assert grants == set()

    async def test_raw_token_is_never_stored(self, db_session):
        from sqlalchemy import select

        from app.models.auth import ApiToken

        _, raw = await _mk_token(db_session)
        rows = (await db_session.execute(select(ApiToken))).scalars().all()
        assert rows
        assert all(t.token_hash != raw for t in rows)
        assert all(len(t.token_hash) == 64 for t in rows)

    async def test_grants_round_trip(self, db_session):
        from app.core.security import verify_token

        project_id = await _mk_project(db_session, "grant-a")
        _, raw = await _mk_token(db_session, role="OPERATOR", project_ids=[project_id])
        resolved = await verify_token(db_session, raw)
        assert resolved is not None
        _, grants = resolved
        assert grants == {project_id}

    async def test_revoked_token_fails_like_unknown(self, db_session):
        from app.core.security import revoke_token, verify_token

        token, raw = await _mk_token(db_session)
        await revoke_token(db_session, token.id)
        assert await verify_token(db_session, raw) is None

    async def test_expired_token_fails(self, db_session):
        from app.core.security import verify_token

        token, raw = await _mk_token(db_session)
        token.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await db_session.commit()
        assert await verify_token(db_session, raw) is None

    async def test_revoke_is_idempotent(self, db_session):
        from app.core.security import revoke_token

        token, _ = await _mk_token(db_session)
        first = await revoke_token(db_session, token.id)
        second = await revoke_token(db_session, token.id)
        assert first is not None and second is not None
        assert first.revoked_at == second.revoked_at

    async def test_bootstrap_is_idempotent(self, db_session):
        from app.core.security import bootstrap_admin_token, verify_token

        first = await bootstrap_admin_token(db_session)
        second = await bootstrap_admin_token(db_session)
        assert first is not None
        assert second is None  # never a second admin credential
        assert await verify_token(db_session, first) is not None


# ---------------------------------------------------------------------------
# HTTP authentication & authorization (real app, real middleware)
# ---------------------------------------------------------------------------


class TestHttpAuth:
    def test_missing_token_is_401(self, client, enforce_auth):
        resp = client.get("/api/v1/projects")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"

    def test_forged_token_is_401(self, client, enforce_auth):
        resp = client.get("/api/v1/projects", headers=_bearer("argus_forged"))
        assert resp.status_code == 401

    def test_garbage_authorization_header_is_401(self, client, enforce_auth):
        resp = client.get("/api/v1/projects", headers={"Authorization": "Basic abc"})
        assert resp.status_code == 401

    async def test_valid_token_reaches_the_route(
        self, client, db_session, enforce_auth
    ):
        _, raw = await _mk_token(db_session)
        resp = client.get("/api/v1/projects", headers=_bearer(raw))
        assert resp.status_code == 200

    def test_public_paths_do_not_need_a_token(self, client, enforce_auth):
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
        assert client.get("/metrics").status_code == 200

    async def test_viewer_cannot_write(self, client, db_session, enforce_auth):
        _, raw = await _mk_token(db_session, role="VIEWER")
        resp = client.post(
            "/api/v1/projects",
            json={"name": "Nope", "slug": "nope"},
            headers=_bearer(raw),
        )
        assert resp.status_code == 403
        assert "OPERATOR" in resp.json()["detail"]

    async def test_scoped_token_cannot_read_foreign_project(
        self, client, db_session, enforce_auth
    ):
        granted = await _mk_project(db_session, "granted-p")
        foreign = await _mk_project(db_session, "foreign-p")
        _, raw = await _mk_token(db_session, role="OPERATOR", project_ids=[granted])

        ok = client.get(f"/api/v1/projects/{granted}", headers=_bearer(raw))
        assert ok.status_code == 200
        denied = client.get(f"/api/v1/projects/{foreign}", headers=_bearer(raw))
        # Same answer as an unknown project: existence not disclosed.
        assert denied.status_code == 404

    async def test_scoped_token_list_excludes_foreign_projects(
        self, client, db_session, enforce_auth
    ):
        granted = await _mk_project(db_session, "list-granted")
        await _mk_project(db_session, "list-foreign")
        _, raw = await _mk_token(db_session, role="OPERATOR", project_ids=[granted])

        resp = client.get("/api/v1/projects", headers=_bearer(raw))
        assert resp.status_code == 200
        ids = {p["id"] for p in resp.json()["items"]}
        assert str(granted) in ids
        assert len(ids) == 1

    async def test_failed_auth_is_audited(self, client, db_session, enforce_auth):
        from sqlalchemy import select

        from app.models.auth import AuthAuditAction, AuthenticationAudit

        client.get("/api/v1/projects", headers=_bearer("argus_forged"))
        rows = (
            (
                await db_session.execute(
                    select(AuthenticationAudit).where(
                        AuthenticationAudit.action == AuthAuditAction.FAILED
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows, "a refused credential must land in the audit trail"
        assert any("unknown" in (r.reason or "") for r in rows)

    async def test_successful_auth_updates_last_used(
        self, client, db_session, enforce_auth
    ):
        from sqlalchemy import select

        from app.models.auth import ApiToken

        token, raw = await _mk_token(db_session)
        assert token.last_used_at is None
        client.get("/api/v1/projects", headers=_bearer(raw))
        # Re-read through this session's identity map, then refresh to pick up
        # the middleware's own committed write ("last_used_at" is throttled in
        # production, but the first use always stamps).
        stored = (
            await db_session.execute(select(ApiToken).where(ApiToken.id == token.id))
        ).scalar_one()
        await db_session.refresh(stored)
        assert stored.last_used_at is not None


# ---------------------------------------------------------------------------
# Edge hardening: rate limiting and body limits
# ---------------------------------------------------------------------------


class TestEdgeHardening:
    def test_token_bucket_blocks_then_refills(self):
        import time as time_module

        from app.core.edge import _TokenBucket

        bucket = _TokenBucket(capacity=3, refill_per_second=1000)
        assert all(bucket.try_take() for _ in range(3))
        assert not bucket.try_take()
        time_module.sleep(0.01)
        assert bucket.try_take()

    def test_rate_limited_requests_get_429_with_retry_after(self):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from app.core.edge import RateLimitMiddleware

        async def ok(request):
            return PlainTextResponse("ok")

        application = Starlette(routes=[Route("/x", ok)])
        application.add_middleware(
            RateLimitMiddleware, per_minute=60, burst=2, enabled=True
        )
        test_client = TestClient(application)
        statuses = [test_client.get("/x").status_code for _ in range(6)]
        assert statuses.count(200) >= 1
        assert 429 in statuses
        blocked = [s for s in statuses if s == 429]
        assert blocked, "burst above the bucket must be refused"
        last = test_client.get("/x")
        assert last.status_code == 429
        assert last.headers.get("retry-after") == "1"

    def test_health_is_exempt_from_rate_limiting(self):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from app.core.edge import RateLimitMiddleware

        async def health(request):
            return PlainTextResponse("ok")

        application = Starlette(routes=[Route("/health/live", health)])
        application.add_middleware(
            RateLimitMiddleware, per_minute=1, burst=1, enabled=True
        )
        test_client = TestClient(application)
        assert all(test_client.get("/health/live").status_code == 200 for _ in range(5))

    def test_oversized_body_is_413(self):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from app.core.edge import BodySizeLimitMiddleware

        async def echo(request):
            return PlainTextResponse("ok")

        application = Starlette(routes=[Route("/x", echo, methods=["POST"])])
        application.add_middleware(BodySizeLimitMiddleware, max_bytes=16)
        test_client = TestClient(application)
        assert test_client.post("/x", content=b"tiny").status_code == 200
        big = test_client.post("/x", content=b"x" * 1024)
        assert big.status_code == 413
        assert big.json()["error_code"] == "REQUEST_TOO_LARGE"

    def test_rate_limit_buckets_are_per_credential(self):
        from app.core.edge import RateLimitMiddleware

        class _Req:
            def __init__(self, token):
                self.headers = {"authorization": f"Bearer {token}"}
                self.client = type("C", (), {"host": "1.2.3.4"})()
                self.url = type("U", (), {"path": "/x"})()

        middleware = RateLimitMiddleware(None, per_minute=60, burst=1, enabled=True)
        assert middleware._key(_Req("tok-a")) != middleware._key(_Req("tok-b"))


# ---------------------------------------------------------------------------
# Webhook signatures (replay resistance)
# ---------------------------------------------------------------------------


class TestWebhookSignatures:
    @staticmethod
    def _sign(secret: str, ts: str, body: bytes) -> str:
        mac = hmac_module.new(
            secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
        ).hexdigest()
        return f"sha256={mac}"

    def test_valid_signature_accepted(self):
        from app.services.ingest_trust import verify_webhook_signature

        secret = "whsec_test"
        ts = str(int(datetime.now(timezone.utc).timestamp()))
        body = b'{"event": "deploy"}'
        assert verify_webhook_signature(
            secret=secret,
            timestamp=ts,
            signature=self._sign(secret, ts, body),
            body=body,
        )

    def test_replayed_signature_refused(self):
        from app.services.ingest_trust import verify_webhook_signature

        secret = "whsec_test"
        old_ts = str(int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()))
        body = b"payload"
        assert not verify_webhook_signature(
            secret=secret,
            timestamp=old_ts,
            signature=self._sign(secret, old_ts, body),
            body=body,
        )

    def test_tampered_body_refused(self):
        from app.services.ingest_trust import verify_webhook_signature

        secret = "whsec_test"
        ts = str(int(datetime.now(timezone.utc).timestamp()))
        signature = self._sign(secret, ts, b"original")
        assert not verify_webhook_signature(
            secret=secret, timestamp=ts, signature=signature, body=b"tampered"
        )

    def test_garbage_timestamp_refused(self):
        from app.services.ingest_trust import verify_webhook_signature

        assert not verify_webhook_signature(
            secret="s", timestamp="not-a-number", signature="sha256=abcd", body=b"x"
        )

    def test_wrong_scheme_refused(self):
        from app.services.ingest_trust import verify_webhook_signature

        assert not verify_webhook_signature(
            secret="s", timestamp="123", signature="md5=abcd", body=b"x"
        )

    async def test_signed_webhook_is_verified_end_to_end(
        self, client, db_session, monkeypatch
    ):
        """The strict per-source webhook verifies raw bytes before parsing."""
        monkeypatch.setenv("PLATFORM_WEBHOOK_SECRET", "whsec_e2e")
        source = await _mk_source(db_session, await _mk_project(db_session, "wh-proj"))
        secret = "whsec_e2e"
        ts = str(int(datetime.now(timezone.utc).timestamp()))
        body = (
            b'{"event_type": "LOG", "timestamp": "%s", '
            b'"project_id": "%s", "payload": {"metric": "errors"}}'
            % (ts.encode(), str(source.project_id).encode())
        )
        good = client.post(
            f"/api/v1/ingestion/webhooks/{source.id}",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Argus-Timestamp": ts,
                "X-Argus-Signature": TestWebhookSignatures._sign(secret, ts, body),
            },
        )
        assert good.status_code == 202, good.text

        tampered = b'{"event_type": "LOG", "timestamp": "0", "payload": {}}'
        bad = client.post(
            f"/api/v1/ingestion/webhooks/{source.id}",
            content=tampered,
            headers={
                "Content-Type": "application/json",
                "X-Argus-Timestamp": ts,
                "X-Argus-Signature": TestWebhookSignatures._sign(secret, ts, body),
            },
        )
        assert bad.status_code == 401

    async def test_webhook_is_never_anonymous(self, client, enforce_auth):
        """With auth enforced and no credential, the webhook path is closed.

        The endpoint must never be reachable without a credential — this is
        the invariant, whatever the secret configuration happens to be.
        """
        resp = client.post(
            "/api/v1/ingestion/webhook",
            json={
                "event_type": "LOG",
                "timestamp": "2026-01-01T12:00:00Z",
                "payload": {},
                "project_id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 401

    async def test_unsigned_delivery_refused_when_secret_configured(
        self, client, db_session, monkeypatch
    ):
        """A configured secret makes signatures mandatory, not advisory."""
        monkeypatch.setenv("PLATFORM_WEBHOOK_SECRET", "whsec_required")
        source = await _mk_source(db_session, await _mk_project(db_session, "wh-req"))
        resp = client.post(
            f"/api/v1/ingestion/webhooks/{source.id}",
            content=b'{"event_type": "LOG", "timestamp": "0", "payload": {}}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 401
        assert "signature" in resp.json()["detail"].lower()

    async def test_authenticated_delivery_survives_without_a_secret(
        self, client, db_session, monkeypatch
    ):
        """No secret configured ⇒ the API token is the credential (Phase 1 §47).

        This is the documented posture: an operator who has not set up HMAC
        signing still needs an authenticated ARGUS token (and a project grant)
        to deliver events. Wire-compatibility with Phase 1 is preserved for
        callers that were already authenticated.
        """
        monkeypatch.delenv("PLATFORM_WEBHOOK_SECRET", raising=False)
        source = await _mk_source(db_session, await _mk_project(db_session, "wh-api"))
        resp = client.post(
            f"/api/v1/ingestion/webhooks/{source.id}",
            json={
                "event_type": "LOG",
                "timestamp": "2026-01-01T12:00:00Z",
                "payload": {"message": "authenticated delivery"},
            },
        )
        assert resp.status_code == 202, resp.text

    async def test_per_source_token_is_exact_source_scoped(self, db_session):
        """A source's ingest token must not work for a sibling source.

        Both sources live in the same project, so a project-level check would
        pass — the per-source webhook must compare against the source itself.
        """
        from app.services.ingest_trust import (
            issue_ingest_token,
            source_matches_ingest_token,
        )

        project_id = await _mk_project(db_session, "wh-sib")
        first = await _mk_source(db_session, project_id)
        second = await _mk_source(db_session, project_id)
        raw = await issue_ingest_token(db_session, first, actor="test")
        assert source_matches_ingest_token(first, raw)
        assert not source_matches_ingest_token(second, raw)
        # A source with no token never matches anything — fail closed.
        assert not source_matches_ingest_token(second, "argus_ing_anything")

    async def test_source_token_cannot_deliver_to_another_source(
        self, client, db_session, monkeypatch
    ):
        """End-to-end: sibling source token ⇒ 401 on the webhook route."""
        from app.services.ingest_trust import issue_ingest_token

        monkeypatch.delenv("PLATFORM_WEBHOOK_SECRET", raising=False)
        project_id = await _mk_project(db_session, "wh-cross")
        first = await _mk_source(db_session, project_id)
        second = await _mk_source(db_session, project_id)
        raw = await issue_ingest_token(db_session, first, actor="test")
        resp = client.post(
            f"/api/v1/ingestion/webhooks/{second.id}",
            json={
                "event_type": "LOG",
                "timestamp": "2026-01-01T12:00:00Z",
                "payload": {},
            },
            headers={"X-Argus-Ingest-Token": raw},
        )
        assert resp.status_code == 401

    async def test_source_delivery_accepts_its_own_token(
        self, client, db_session, monkeypatch
    ):
        """The source's own ingest token authenticates the delivery."""
        from app.services.ingest_trust import issue_ingest_token

        monkeypatch.delenv("PLATFORM_WEBHOOK_SECRET", raising=False)
        source = await _mk_source(db_session, await _mk_project(db_session, "wh-own"))
        raw = await issue_ingest_token(db_session, source, actor="test")
        resp = client.post(
            f"/api/v1/ingestion/webhooks/{source.id}",
            json={
                "event_type": "LOG",
                "timestamp": "2026-01-01T12:00:00Z",
                "payload": {"message": "own token"},
            },
            headers={"X-Argus-Ingest-Token": raw},
        )
        assert resp.status_code == 202, resp.text

    async def test_envelope_cannot_widen_scope_beyond_its_source(
        self, client, db_session, monkeypatch, enforce_auth
    ):
        """A delivery cannot name a foreign project and still be accepted.

        The envelope carries its own ``project_id``; a token scoped to one
        project must not be able to route events into another by declaring it.
        """
        monkeypatch.delenv("PLATFORM_WEBHOOK_SECRET", raising=False)
        allowed = await _mk_project(db_session, "wh-in")
        foreign = await _mk_project(db_session, "wh-out")
        source = await _mk_source(db_session, allowed)
        token, raw = await _mk_token(db_session, role="OPERATOR", project_ids=[allowed])
        resp = client.post(
            f"/api/v1/ingestion/webhooks/{source.id}",
            json={
                "event_type": "LOG",
                "timestamp": "2026-01-01T12:00:00Z",
                "payload": {},
                "project_id": str(foreign),
            },
            headers=_bearer(raw),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Ingest token lifecycle
# ---------------------------------------------------------------------------


class TestIngestTokenLifecycle:
    async def test_issue_then_resolve(self, db_session):
        from app.services.ingest_trust import issue_ingest_token, resolve_ingest_token

        project_id = await _mk_project(db_session, "ing-src")
        source = await _mk_source(db_session, project_id)
        raw = await issue_ingest_token(db_session, source, actor="test")
        assert raw.startswith("argus_ing_")
        assert await resolve_ingest_token(db_session, raw) == project_id

    async def test_rotate_invalidates_old_token(self, db_session):
        from app.services.ingest_trust import issue_ingest_token, resolve_ingest_token

        project_id = await _mk_project(db_session, "ing-rot")
        source = await _mk_source(db_session, project_id)
        first = await issue_ingest_token(db_session, source, actor="test")
        second = await issue_ingest_token(db_session, source, actor="test")
        assert first != second
        assert await resolve_ingest_token(db_session, first) is None
        assert await resolve_ingest_token(db_session, second) == project_id

    async def test_revoked_source_closes_ingestion(self, db_session):
        from app.services.ingest_trust import (
            issue_ingest_token,
            resolve_ingest_token,
            revoke_ingest_token,
        )

        project_id = await _mk_project(db_session, "ing-rev")
        source = await _mk_source(db_session, project_id)
        raw = await issue_ingest_token(db_session, source, actor="test")
        await revoke_ingest_token(db_session, source, actor="test")
        assert await resolve_ingest_token(db_session, raw) is None

    async def test_unknown_token_resolves_nothing(self, db_session):
        from app.services.ingest_trust import resolve_ingest_token

        assert await resolve_ingest_token(db_session, "argus_ing_whatever") is None


class TestCredentialSeparation:
    def test_ingest_token_is_refused_on_the_general_api(self, client, enforce_auth):
        # A source token must not open /api/v1/projects, even valid-looking.
        resp = client.get(
            "/api/v1/projects", headers=_bearer("argus_ing_somefaketoken")
        )
        assert resp.status_code == 401

    def test_viewer_context_cannot_write(self):
        from app.core.security import AuthContext
        from app.models.auth import TokenRole

        auth = AuthContext(
            token_id=uuid.uuid4(), role=TokenRole.VIEWER, project_ids=set(), name="v"
        )
        assert not auth.can_write
        operator = AuthContext(
            token_id=uuid.uuid4(), role=TokenRole.OPERATOR, project_ids=set(), name="o"
        )
        assert operator.can_write


# ---------------------------------------------------------------------------
# The public surface is exactly what the docs claim
# ---------------------------------------------------------------------------


class TestOpenAPIAuthCoverage:
    def test_public_surface_is_exactly_documented(self):
        from app.core.security import PUBLIC_PATHS

        assert PUBLIC_PATHS == frozenset(
            {"/", "/health/live", "/health/ready", "/health/dependencies", "/metrics"}
        )

    def test_anonymous_callers_are_refused_across_the_api(self, client, enforce_auth):
        """The funnel runs before routing, so anything under /api refuses.

        Representative read routes across phases: all 401 without a token.
        """
        samples = [
            "/api/v1/projects",
            "/api/v1/incidents",
            "/api/v1/anomalies",
            "/api/v1/graph/nodes",
            "/api/v1/platform/health",
            "/api/v1/intelligence/health",
        ]
        for path in samples:
            resp = client.get(path)
            assert resp.status_code == 401, f"GET {path} did not refuse"

    def test_ingest_routes_refuse_anonymous_callers(self, client, enforce_auth):
        resp = client.post(
            "/api/v1/otlp/v1/traces",
            json={"project_id": str(uuid.uuid4()), "resource_spans": []},
        )
        assert resp.status_code == 401

    def test_production_refuses_auth_disabled(self):
        from app.core.config import Settings

        with pytest.raises(Exception):
            Settings(API_ENVIRONMENT="production", AUTH_DISABLED=True)


class TestBootstrapIsIdempotentAndTolerant:
    """Startup must survive every credential state a real deployment reaches.

    ``bootstrap_admin_token`` runs inside the application lifespan, so anything
    it raises is a boot failure. The recovery runbook says: mint a replacement
    admin token with the CLI, then revoke the old one when convenient — which
    means "two active admin tokens" is a state operators are *told* to create.
    """

    async def test_first_boot_mints_a_token(self, db_session) -> None:
        from app.core.security import bootstrap_admin_token

        raw = await bootstrap_admin_token(db_session)
        assert raw is not None and raw.startswith("argus_")

    async def test_second_boot_mints_nothing(self, db_session) -> None:
        from app.core.security import bootstrap_admin_token

        await bootstrap_admin_token(db_session)
        assert await bootstrap_admin_token(db_session) is None

    async def test_boot_survives_several_active_admin_tokens(self, db_session) -> None:
        """The regression: this used to raise MultipleResultsFound at startup."""
        from app.core.security import (
            bootstrap_admin_token,
            create_token,
            generate_token,
        )

        await bootstrap_admin_token(db_session)
        #: A second admin credential exists — the documented recovery path.
        await create_token(db_session, name="recovery-admin", role="ADMIN")
        raw, token_hash = generate_token()
        from app.models.auth import ApiToken, TokenRole, TokenStatus

        db_session.add(
            ApiToken(
                name="recovery-admin-2",
                token_hash=token_hash,
                role=TokenRole.ADMIN,
                status=TokenStatus.ACTIVE,
            )
        )
        await db_session.flush()

        #: Must return None (an admin exists) rather than raising.
        assert await bootstrap_admin_token(db_session) is None

    async def test_permanently_revoked_admin_does_not_block_a_new_bootstrap(
        self, db_session
    ) -> None:
        """Revoking the only admin token must leave the platform recoverable."""
        from app.core.security import bootstrap_admin_token
        from app.models.auth import ApiToken, TokenRole, TokenStatus

        await bootstrap_admin_token(db_session)
        db_session.add(
            ApiToken(
                name="bootstrap-admin",
                token_hash="0" * 64,
                role=TokenRole.ADMIN,
                status=TokenStatus.REVOKED,
            )
        )
        await db_session.flush()
        #: An active admin still exists from the first bootstrap, so nothing is
        #: minted; the point is that this call does not raise.
        assert await bootstrap_admin_token(db_session) is None


# ---------------------------------------------------------------------------
# OTLP ingestion scope (W1 follow-up)
# ---------------------------------------------------------------------------
#
# The read side and the webhook side were already covered above. The three OTLP
# verbs were not — and that omission hid a real cross-tenant write: an ingest
# token minted for project A could name project B in the body and the row was
# persisted *into B*. These tests exist so that hole cannot come back.
#
# The distinction that matters:
#
#   * an ingest token resolves to exactly ONE project — the body may not widen it;
#   * a scoped API token must hold a grant for the body's project;
#   * an ADMIN token passes through (it is the operator's break-glass path).


class TestOtlpIngestScope:
    @staticmethod
    def _spans_body(project_id: uuid.UUID) -> dict:
        """A minimal, valid OTLP/JSON trace export for one project."""
        return {
            "projectId": str(project_id),
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "scope-probe"},
                            }
                        ]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "0af7651916cd43dd8448eb211c80319c",
                                    "spanId": "b7ad6b7169203331",
                                    "name": "GET /scope-probe",
                                    "startTimeUnixNano": "1758600000000000000",
                                    "endTimeUnixNano": "1758600000100000000",
                                    "status": {"code": 2},
                                }
                            ]
                        }
                    ],
                }
            ],
        }

    async def test_ingest_token_cannot_write_to_another_project(
        self, client, db_session, enforce_auth
    ):
        """The load-bearing test: the credential decides the scope, not the body.

        This is the exact shape of the defect that was found live: a token for
        project A, a body naming project B, and a persisted row in B.
        """
        from app.services.ingest_trust import issue_ingest_token

        granted = await _mk_project(db_session, "otlp-granted")
        foreign = await _mk_project(db_session, "otlp-foreign")
        source = await _mk_source(db_session, granted)
        raw = await issue_ingest_token(db_session, source, actor="test")

        resp = client.post(
            "/api/v1/otlp/v1/traces",
            json=self._spans_body(foreign),
            headers={"X-Argus-Ingest-Token": raw},
        )
        assert resp.status_code == 403, resp.text

    async def test_ingest_token_writes_to_its_own_project(
        self, client, db_session, enforce_auth
    ):
        """The refusal above must not be a blanket refusal: the happy path works."""
        from app.services.ingest_trust import issue_ingest_token

        granted = await _mk_project(db_session, "otlp-own")
        source = await _mk_source(db_session, granted)
        raw = await issue_ingest_token(db_session, source, actor="test")

        resp = client.post(
            "/api/v1/otlp/v1/traces",
            json=self._spans_body(granted),
            headers={"X-Argus-Ingest-Token": raw},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["accepted"] == 1

    async def test_scoped_api_token_cannot_write_to_ungranted_project(
        self, client, db_session, enforce_auth
    ):
        """A grant is a boundary for writes as well as reads."""
        granted = await _mk_project(db_session, "otlp-api-granted")
        foreign = await _mk_project(db_session, "otlp-api-foreign")
        _, raw = await _mk_token(db_session, role="OPERATOR", project_ids=[granted])

        allowed = client.post(
            "/api/v1/otlp/v1/traces",
            json=self._spans_body(granted),
            headers=_bearer(raw),
        )
        assert allowed.status_code == 200, allowed.text

        denied = client.post(
            "/api/v1/otlp/v1/traces",
            json=self._spans_body(foreign),
            headers=_bearer(raw),
        )
        assert denied.status_code == 403, denied.text

    async def test_otlp_protobuf_cannot_bypass_the_scope_check(
        self, client, db_session, enforce_auth
    ):
        """Decoding at the edge must not become a differently-guarded path.

        A Protobuf export has no field for ARGUS's tenancy extension, so the
        destination can be named out of band (query string or
        ``X-Argus-Project-Id``). That naming is read by the decoder and then
        checked by the *same* guard the JSON body goes through, so naming a
        project the credential does not hold is refused — including the case
        where the credential implies a project of its own and the caller names a
        different one.
        """
        from app.services.ingest_trust import issue_ingest_token

        from tests.test_otlp_protobuf import _traces_payload

        granted = await _mk_project(db_session, "otlp-proto-granted")
        foreign = await _mk_project(db_session, "otlp-proto-foreign")
        source = await _mk_source(db_session, granted)
        raw = await issue_ingest_token(db_session, source, actor="test")
        headers = {
            "X-Argus-Ingest-Token": raw,
            "Content-Type": "application/x-protobuf",
        }

        denied = client.post(
            f"/api/v1/otlp/v1/traces?project_id={foreign}",
            content=_traces_payload(),
            headers=headers,
        )
        assert denied.status_code == 403, denied.text

        spoofed = client.post(
            "/api/v1/otlp/v1/traces",
            content=_traces_payload(),
            headers={**headers, "X-Argus-Project-Id": str(foreign)},
        )
        assert spoofed.status_code == 403, spoofed.text

        allowed = client.post(
            f"/api/v1/otlp/v1/traces?project_id={granted}",
            content=_traces_payload(),
            headers=headers,
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["accepted"] == 1

    async def test_otlp_logs_and_metrics_apply_the_same_scope(
        self, client, db_session, enforce_auth
    ):
        """All three OTLP verbs share one scope rule — not just traces."""
        from app.services.ingest_trust import issue_ingest_token

        granted = await _mk_project(db_session, "otlp-all-verbs")
        foreign = await _mk_project(db_session, "otlp-all-verbs-foreign")
        source = await _mk_source(db_session, granted)
        raw = await issue_ingest_token(db_session, source, actor="test")
        headers = {"X-Argus-Ingest-Token": raw}

        logs = client.post(
            "/api/v1/otlp/v1/logs",
            json={"projectId": str(foreign), "resourceLogs": []},
            headers=headers,
        )
        assert logs.status_code == 403, logs.text
        metrics = client.post(
            "/api/v1/otlp/v1/metrics",
            json={"projectId": str(foreign), "resourceMetrics": []},
            headers=headers,
        )
        assert metrics.status_code == 403, metrics.text

    def test_admin_token_may_write_to_any_project(
        self, client, db_session, enforce_auth
    ):
        """Break-glass stays open: an ADMIN token is the operator's fallback."""
        import asyncio

        any_project = asyncio.get_event_loop().run_until_complete(
            _mk_project(db_session, "otlp-admin")
        )
        _, raw = asyncio.get_event_loop().run_until_complete(
            _mk_token(db_session, role="ADMIN")
        )
        resp = client.post(
            "/api/v1/otlp/v1/traces",
            json=self._spans_body(any_project),
            headers=_bearer(raw),
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Context propagation and fail-closed guards (live-isolation defect)
# ---------------------------------------------------------------------------
#
# These pin the *mechanism*, not just the outcome, because the outcome was
# already green while the live server was broken:
#
#   the context the middleware binds must actually reach the endpoint, and a
#   guard that cannot see it must refuse.
#
# `TestClient` runs the endpoint in the caller's context, so no behaviour test
# here can distinguish good middleware from bad — hence the structural
# assertion plus the fail-closed assertions, and the live HTTP gate at
# `infrastructure/e2e-smoke-hardening.sh` for the real end-to-end proof.


class TestAuthContextPropagation:
    def test_auth_middleware_is_pure_asgi(self):
        """``BaseHTTPMiddleware`` silently breaks context propagation.

        It starts the downstream app in a task whose context predates
        ``dispatch``, so any ContextVar set there is invisible to the endpoint.
        Under a real server that made every authorization check a no-op; under
        TestClient it looked fine. This assertion is the tripwire.
        """
        from starlette.middleware.base import BaseHTTPMiddleware

        from app.core.edge import AuthMiddleware

        assert not issubclass(AuthMiddleware, BaseHTTPMiddleware), (
            "AuthMiddleware must stay pure ASGI: BaseHTTPMiddleware does not "
            "propagate the auth ContextVar to the endpoint under uvicorn, "
            "which silently disables every authorization check."
        )

    def test_auth_middleware_binds_state_into_the_scope(self):
        """The context is also written to ``scope['state']`` for dependencies."""
        import anyio

        from app.core import edge
        from app.core.edge import _STATE_KEY, AuthMiddleware

        seen: dict = {}

        async def app(scope, receive, send):
            seen.update(scope.get("state", {}))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        # Public path ⇒ the anonymous context is bound without touching the DB.
        middleware = AuthMiddleware(app, public_paths=frozenset({"/health/live"}))
        scope = {
            "type": "http",
            "path": "/health/live",
            "method": "GET",
            "headers": [],
            "state": {},
        }

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(_message):
            return None

        anyio.run(middleware, scope, receive, send)
        assert _STATE_KEY in seen
        assert seen[_STATE_KEY] is edge._ANON

    def test_require_auth_context_fails_closed(self):
        """No context ⇒ 401, never a skipped check."""
        import pytest
        from fastapi import HTTPException

        from app.core.edge import require_auth_context

        with pytest.raises(HTTPException) as exc:
            require_auth_context()
        assert exc.value.status_code == 401

    async def test_require_project_fails_closed_without_a_context(self, db_session):
        """A guard that cannot see the caller must refuse the project.

        The pre-fix code read ``auth = get_auth_context()`` and skipped the
        grant check when it was ``None``, so a lost context read as "allowed".
        """
        import pytest
        from fastapi import HTTPException

        from app.api.v1.deps import require_project
        from app.core import edge

        project = await _mk_project(db_session, "fail-closed")
        reset = edge.auth_context_var.set(None)  # simulate a missing context
        try:
            with pytest.raises(HTTPException) as exc:
                await require_project(db_session, project)
        finally:
            edge.auth_context_var.reset(reset)
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Central path-scope enforcement
# ---------------------------------------------------------------------------
#
# The grant check used to live only in routes that remembered to call
# ``require_project``. An audit found project-scoped routes that validated the
# project merely *existed* — a token scoped to A could create environments in
# B. Coverage is now structural (a router-level dependency), and these tests
# pin both directions: the refusal and the legitimate call.


class TestPathScopeGuard:
    async def _scoped(self, db_session, project_id):
        _, raw = await _mk_token(db_session, role="OPERATOR", project_ids=[project_id])
        return raw

    async def test_scoped_token_cannot_create_in_a_foreign_project(
        self, client, db_session, enforce_auth
    ):
        """The exact live finding: 201 Created in a project the token cannot see."""
        granted = await _mk_project(db_session, "path-granted")
        foreign = await _mk_project(db_session, "path-foreign")
        raw = await self._scoped(db_session, granted)

        resp = client.post(
            f"/api/v1/projects/{foreign}/environments",
            json={"name": "sneaky", "environment_type": "STAGING"},
            headers=_bearer(raw),
        )
        assert resp.status_code == 404, resp.text

    async def test_scoped_token_can_create_in_its_own_project(
        self, client, db_session, enforce_auth
    ):
        """A guard that refuses everything is not a fix."""
        granted = await _mk_project(db_session, "path-own")
        raw = await self._scoped(db_session, granted)

        resp = client.post(
            f"/api/v1/projects/{granted}/environments",
            json={"name": "legit", "environment_type": "STAGING"},
            headers=_bearer(raw),
        )
        assert resp.status_code == 201, resp.text

    async def test_scoped_token_cannot_list_a_foreign_projects_environments(
        self, client, db_session, enforce_auth
    ):
        """Reads are covered by the same central guard, not just writes."""
        granted = await _mk_project(db_session, "path-list-granted")
        foreign = await _mk_project(db_session, "path-list-foreign")
        raw = await self._scoped(db_session, granted)

        assert (
            client.get(
                f"/api/v1/projects/{granted}/environments", headers=_bearer(raw)
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/api/v1/projects/{foreign}/environments", headers=_bearer(raw)
            ).status_code
            == 404
        )

    async def test_environment_id_path_cannot_be_used_as_a_side_door(
        self, client, db_session, enforce_auth
    ):
        """An ``environment_id`` path resolves through its own project."""
        from app.models.project import Environment

        granted = await _mk_project(db_session, "env-door-granted")
        foreign = await _mk_project(db_session, "env-door-foreign")
        foreign_env = Environment(
            project_id=foreign, name="foreign-env", environment_type="STAGING"
        )
        db_session.add(foreign_env)
        await db_session.commit()
        await db_session.refresh(foreign_env)
        raw = await self._scoped(db_session, granted)

        resp = client.get(
            f"/api/v1/environments/{foreign_env.id}", headers=_bearer(raw)
        )
        assert resp.status_code == 404, resp.text

    def test_an_admin_token_still_reaches_every_project(
        self, client, db_session, enforce_auth
    ):
        """Break-glass: the central guard must not lock out ADMIN."""
        import asyncio

        loop = asyncio.get_event_loop()
        target = loop.run_until_complete(_mk_project(db_session, "path-admin"))
        _, raw = loop.run_until_complete(_mk_token(db_session, role="ADMIN"))

        resp = client.get(
            f"/api/v1/projects/{target}/environments", headers=_bearer(raw)
        )
        assert resp.status_code == 200, resp.text


class TestOtlpCredentialDerivedProject:
    """A stock collector cannot send a top-level ``projectId`` — so the
    credential supplies it. These tests pin the inference *and* its limits.
    """

    @staticmethod
    def _spans(project_id: uuid.UUID | None) -> dict:
        body = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "collector"},
                            }
                        ]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "0af7651916cd43dd8448eb211c80319d",
                                    "spanId": "b7ad6b7169203332",
                                    "name": "GET /collector",
                                    "startTimeUnixNano": "1758600000000000000",
                                    "endTimeUnixNano": "1758600000100000000",
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        if project_id is not None:
            body["projectId"] = str(project_id)
        return body

    async def test_ingest_token_supplies_the_project_when_the_body_omits_it(
        self, client, db_session, enforce_auth
    ):
        """The vanilla-exporter path: no projectId in the payload, ever."""
        from app.services.ingest_trust import issue_ingest_token

        project = await _mk_project(db_session, "otlp-inferred")
        source = await _mk_source(db_session, project)
        raw = await issue_ingest_token(db_session, source, actor="test")

        resp = client.post(
            "/api/v1/otlp/v1/traces",
            json=self._spans(None),
            headers={"X-Argus-Ingest-Token": raw},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["accepted"] == 1

    async def test_a_single_grant_api_token_also_supplies_the_project(
        self, client, db_session, enforce_auth
    ):
        project = await _mk_project(db_session, "otlp-inferred-api")
        _, raw = await _mk_token(db_session, role="OPERATOR", project_ids=[project])

        resp = client.post(
            "/api/v1/otlp/v1/traces",
            json=self._spans(None),
            headers=_bearer(raw),
        )
        assert resp.status_code == 200, resp.text

    async def test_an_admin_token_must_name_the_project(
        self, client, db_session, enforce_auth
    ):
        """An unscoped credential implies nothing — guessing writes to a
        tenant the caller never named."""
        _, raw = await _mk_token(db_session, role="ADMIN")

        resp = client.post(
            "/api/v1/otlp/v1/traces",
            json=self._spans(None),
            headers=_bearer(raw),
        )
        assert resp.status_code == 400, resp.text
        assert "projectId" in resp.json()["detail"]

    async def test_a_multi_grant_token_must_name_the_project(
        self, client, db_session, enforce_auth
    ):
        first = await _mk_project(db_session, "otlp-multi-a")
        second = await _mk_project(db_session, "otlp-multi-b")
        _, raw = await _mk_token(
            db_session, role="OPERATOR", project_ids=[first, second]
        )

        resp = client.post(
            "/api/v1/otlp/v1/traces",
            json=self._spans(None),
            headers=_bearer(raw),
        )
        assert resp.status_code == 400, resp.text

    async def test_an_inferred_project_cannot_be_redirected_by_the_body(
        self, client, db_session, enforce_auth
    ):
        """Omitting the field is allowed; overriding it is not."""
        from app.services.ingest_trust import issue_ingest_token

        granted = await _mk_project(db_session, "otlp-infer-granted")
        foreign = await _mk_project(db_session, "otlp-infer-foreign")
        source = await _mk_source(db_session, granted)
        raw = await issue_ingest_token(db_session, source, actor="test")

        resp = client.post(
            "/api/v1/otlp/v1/traces",
            json=self._spans(foreign),
            headers={"X-Argus-Ingest-Token": raw},
        )
        assert resp.status_code == 403, resp.text
