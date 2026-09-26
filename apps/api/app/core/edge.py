"""ARGUS Edge Middleware (Hardening W1): body limits, rate limiting, auth.

Three middlewares, applied in the order they are listed in ``main.py``:

1. :class:`BodySizeLimitMiddleware` — refuses declared-oversized bodies with
   ``413`` before they are read into memory.
2. :class:`RateLimitMiddleware` — a token bucket keyed by bearer token
   (falling back to client IP), debited through a *shared* Redis bucket when one
   is configured, so the ceiling holds across replicas instead of multiplying
   by their count. See :mod:`app.core.rate_limit` for the backend choice and,
   more importantly, for what happens when Redis is unreachable: the request is
   served per-process and the downgrade is counted and logged. A soft ceiling
   that announces itself, never a silent one.
3. :class:`AuthMiddleware` — the single authentication funnel. Every
   ``/api/v1`` request that is not public (health, metrics, docs) resolves a
   credential and stores the :class:`AuthContext` in ``request.state``; route
   dependencies read it and enforce role/project floors. Because the funnel
   is central, no route can be forgotten — and the OpenAPI introspection test
   in ``tests/test_auth_security.py`` keeps the public-path set honest.

Auth decision (who authenticates):

* ``/api/v1/otlp/*`` — a per-source ingest token (or an OPERATOR+ API token);
* everything else under ``/api/v1`` — an API token;
* public paths — no credential (the set is fixed and tested).

With ``auth_disabled`` active (test env / explicit dev bypass) the funnel
marks requests as anonymous-allowed and route dependencies pass through —
one switch, no forked code paths.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.services.otlp_protobuf import (
    OtlpDecodeError,
    decode_otlp_protobuf,
    describe_expected_body,
    is_protobuf_content_type,
)

from contextvars import ContextVar

from app.core.database import commit_request_session
from app.core.rate_limit import Limiter, TokenBucket, build_limiter
from app.core.security import (
    AuthContext,
    INGEST_TOKEN_PREFIX,
    auth_disabled,
    hash_token,
    utcnow,
    verify_token,
)
from app.models.auth import AuthAuditAction, TokenRole

logger = logging.getLogger("argus.edge")

_STATE_KEY = "argus_auth_context"

#: Deliver the caller's context to dependency-layer checks
#: (``require_project_access``) even in code paths that have no ``Request``.
#: Set by :class:`AuthMiddleware` per request; reset after it.
auth_context_var: ContextVar[Optional[AuthContext]] = ContextVar(
    "argus_auth_context", default=None
)

#: HTTP methods that mutate state — the write floor (OPERATOR+) applies.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Routes that accept a *per-source ingest token* instead of an API token.
#: Ingest tokens are single-purpose: they open the ingestion boundary (OTLP
#: and the per-source webhook path) and nothing else, so a leaked collector
#: credential cannot read incidents, run reproduction, or approve remediation.
INGEST_ROUTE_PREFIXES = ("/api/v1/otlp", "/api/v1/ingestion/webhook")

#: Test seam: when set (not ``None``), overrides the process-wide auth
#: decision for every request without rebuilding the middleware stack. Tests
#: use it to exercise the enforcement path on the real app; production never
#: touches it.
_AUTH_DISABLED_OVERRIDE: Optional[bool] = None


def _auth_bypassed(static_value: bool) -> bool:
    """Resolve the auth bypass: test override wins, else the bound value."""
    if _AUTH_DISABLED_OVERRIDE is not None:
        return _AUTH_DISABLED_OVERRIDE
    return static_value


class AuthRequiredError(Exception):
    """Authentication/authorization refusal converted to a JSON response."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else {}


def get_auth(request: Request) -> AuthContext:
    """Read the authenticated context stored by :class:`AuthMiddleware`.

    Routes never parse credentials themselves; they read the context the
    funnel stored (or the anonymous context when auth is disabled).
    """
    ctx = getattr(request.state, _STATE_KEY, None)
    assert ctx is not None, "AuthMiddleware did not run — middleware ordering bug"
    return ctx


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Refuse declared-oversized request bodies with 413.

    Reads only ``Content-Length`` (a streaming attacker faking a small header
    is still bounded by the server's own read limits); ingestion endpoints get
    the configured OTLP headroom, everything else the same ceiling.
    """

    def __init__(self, app, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next) -> Response:
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "detail": f"Request body exceeds {self.max_bytes} bytes",
                            "error_code": "REQUEST_TOO_LARGE",
                        },
                        headers={"Retry-After": "0"},
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length header"},
                )
        return await call_next(request)


def json_nesting_depth(raw: bytes) -> int:
    """Maximum container nesting depth of a JSON document, without parsing it.

    Why scan instead of parse: the failure this guards against *is* the
    parser. ``json.loads`` descends recursively, so a body nested tens of
    thousands of levels deep raises ``RecursionError`` (or exhausts the C
    stack) before any validator runs. This is a single pass with an explicit
    counter — linear cost, constant memory, and no way to recurse.

    String literals and escapes are respected, so a brace inside a string
    value does not count. Malformed input simply returns whatever depth was
    reached: this is a bound, not a validator, and the parser still decides
    whether the body is well-formed.
    """
    depth = 0
    deepest = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
            continue
        if byte == 0x22:  # quote
            in_string = True
        elif byte in (0x7B, 0x5B):  # { [
            depth += 1
            if depth > deepest:
                deepest = depth
        elif byte in (0x7D, 0x5D):  # } ]
            if depth > 0:
                depth -= 1
    return deepest


class JsonDepthLimitMiddleware(BaseHTTPMiddleware):
    """Refuse absurdly nested JSON with 413 before a parser sees it.

    Only JSON content types are inspected. OTLP protobuf bodies and
    form/multipart uploads are passed through untouched — a binary body is not
    JSON and judging it by these rules would be wrong.

    Reading the body here costs nothing extra: Starlette caches the bytes on
    the request, and FastAPI would have read the same body to parse it. The
    refusal therefore happens *before* the expensive, recursive step instead
    of after it has already failed.
    """

    JSON_CONTENT_TYPES = frozenset({"application/json", "application/x-ndjson"})

    def __init__(self, app, max_depth: int) -> None:
        super().__init__(app)
        self.max_depth = max_depth

    @classmethod
    def _is_json(cls, request: Request) -> bool:
        content_type = (request.headers.get("content-type") or "").split(";")[0]
        return content_type.strip().lower() in cls.JSON_CONTENT_TYPES

    async def dispatch(self, request: Request, call_next) -> Response:
        if self._is_json(request):
            body = await request.body()
            if body and json_nesting_depth(body) > self.max_depth:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": (
                            f"JSON body nests deeper than {self.max_depth} levels"
                        ),
                        "error_code": "PAYLOAD_TOO_DEEP",
                    },
                )
        return await call_next(request)


#: The bucket primitive now lives with the backends. It stays importable from
#: here because it is the natural place to look for "how does the edge rate
#: limit?" and because the middleware suite pins its refill behaviour directly.
_TokenBucket = TokenBucket


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket rate limiting at the API edge.

    Keyed by the presented credential when there is one (so one compromised
    token cannot starve others) and by client IP otherwise. Health probes are
    exempt: a monitoring system must never be rate-limited into declaring the
    platform dead.
    """

    EXEMPT_PATHS = frozenset({"/", "/health/live", "/health/ready", "/metrics"})

    def __init__(
        self,
        app,
        per_minute: int,
        burst: int,
        enabled: bool = True,
        *,
        backend: str = "memory",
        redis_url: Optional[str] = None,
        key_prefix: str = "argus:rl:",
    ) -> None:
        super().__init__(app)
        # Literal flag: the caller (main.py) owns policy such as "disabled in
        # the test environment"; the class stays honest and directly testable.
        self.enabled = enabled
        self.capacity = max(burst, 1)
        self.refill = per_minute / 60.0
        #: The default stays per-process so a directly-constructed middleware
        #: behaves exactly as documented in isolation; the application passes
        #: the configured backend, which prefers Redis.
        self._limiter: Limiter = build_limiter(
            backend=backend,
            per_minute=per_minute,
            burst=burst,
            redis_url=redis_url,
            key_prefix=key_prefix,
        )

    def _key(self, request: Request) -> str:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return "t:" + hash_token(auth[7:])[:16]
        if request.client:
            return "ip:" + request.client.host
        return "ip:unknown"

    async def dispatch(self, request: Request, call_next) -> Response:
        if not self.enabled or request.url.path in self.EXEMPT_PATHS:
            return await call_next(request)
        decision = await self._limiter.acquire(self._key(request))
        quota_headers = {
            "X-RateLimit-Limit": str(self.capacity),
            "X-RateLimit-Remaining": str(decision.remaining),
        }
        if not decision.allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded; slow down",
                    "error_code": "RATE_LIMITED",
                },
                headers={
                    #: How long until one token exists again — not a constant.
                    #: An understated value makes clients retry into 429s; an
                    #: overstated one wastes capacity.
                    "Retry-After": str(decision.retry_after),
                    **quota_headers,
                },
            )
        response = await call_next(request)
        for name, value in quota_headers.items():
            response.headers.setdefault(name, value)
        return response


_ANON = AuthContext(
    token_id=uuid.UUID(int=0),
    role=TokenRole.ADMIN,
    project_ids=None,
    name="anonymous-auth-disabled",
)

#: The context bound to a route reachable *before* a credential exists (W2).
#:
#: A pre-authentication route must run with **no authority at all**. Reusing the
#: auth-disabled anonymous context here would hand a caller with no credential
#: an ADMIN identity with every project — correct for "auth is switched off",
#: catastrophic for "the login page". A refusal on every project is the right
#: default for code that is, by definition, not yet anyone.
_PUBLIC_BOOTSTRAP = AuthContext(
    token_id=uuid.UUID(int=0),
    role=TokenRole.VIEWER,
    project_ids=set(),
    name="public-pre-auth",
)


class AuthMiddleware:
    """The authentication funnel (see module docstring for the decision table).

    Deliberately **pure ASGI**, not ``BaseHTTPMiddleware``.

    Why that distinction is load-bearing: ``BaseHTTPMiddleware`` starts the
    downstream app in a task whose context is captured *before* ``dispatch``
    runs, so a ``ContextVar`` set inside the middleware never reaches the
    endpoint. Under ``TestClient`` the app happens to run in the caller's
    context and the value is visible — so the whole test suite passed while the
    live server resolved every request to a stale context and skipped every
    authorization check. A pure ASGI middleware calls the app directly, in the
    same context, so the funnel behaves identically wherever it runs.

    Do not convert this back to ``BaseHTTPMiddleware``. The regression is
    pinned by ``tests/test_auth_security.py::TestAuthContextPropagation``.
    """

    def __init__(
        self,
        app,
        public_paths,
        disabled: Optional[bool] = None,
        bootstrap_paths=None,
    ) -> None:
        self.app = app
        self.public_paths = frozenset(public_paths)
        #: Pre-authentication routes (SSO config/start/callback). Empty by
        #: default so a directly-constructed middleware behaves exactly as it
        #: did before this parameter existed.
        self.bootstrap_paths = frozenset(bootstrap_paths or ())
        self._docs_prefixes = ("/docs", "/redoc", "/openapi.json")
        #: Bound at construction — one Settings read per process, not per
        #: request (constructing Settings parses the whole environment).
        self.disabled = auth_disabled() if disabled is None else disabled

    def _is_public(self, path: str) -> bool:
        return path in self.public_paths or path.startswith(self._docs_prefixes)

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self.bootstrap_paths:
            #: Order matters: checked before the bypass branch so a login page
            #: never inherits the auth-disabled ADMIN context by accident.
            await self._continue(scope, receive, send, _PUBLIC_BOOTSTRAP)
            return
        if (
            _auth_bypassed(self.disabled)
            or self._is_public(path)
            or not path.startswith("/api/")
        ):
            await self._continue(scope, receive, send, _ANON)
            return

        from app.core.database import async_session_factory

        # Headers live in the scope; constructing a Request here reads no body,
        # so the request stream is still intact for the endpoint.
        request = Request(scope, receive)
        header = request.headers.get("authorization", "")
        raw = header[7:] if header.startswith("Bearer ") else None
        if raw is None:
            raw = request.headers.get("X-Argus-Ingest-Token")

        async with async_session_factory() as db:
            try:
                ctx = await self._resolve(db, request, raw, path)
                # Role floor: reads for everyone, writes for OPERATOR+.
                # (ADMIN-only routes enforce further via require_admin.)
                if scope.get("method") in WRITE_METHODS and not ctx.can_write:
                    raise AuthRequiredError(
                        403, "This action requires the OPERATOR role or higher"
                    )
            except AuthRequiredError as exc:
                refusal = JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                    headers=exc.headers,
                )
                await refusal(scope, receive, send)
                return

        await self._continue(scope, receive, send, ctx)

    async def _continue(self, scope, receive, send, ctx: AuthContext) -> None:
        """Serve the request with ``ctx`` bound for the whole call stack."""
        scope.setdefault("state", {})[_STATE_KEY] = ctx
        token = auth_context_var.set(ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            auth_context_var.reset(token)

    async def _resolve(
        self, db, request: Request, raw: Optional[str], path: str
    ) -> AuthContext:
        """Authenticate one request against the database, with audit."""
        if raw is None:
            await self._audit(
                db,
                action=AuthAuditAction.FAILED,
                reason="missing bearer token",
                path=path,
                ip=request.client.host if request.client else None,
            )
            raise AuthRequiredError(401, "Missing bearer token")

        is_ingest_route = path.startswith(INGEST_ROUTE_PREFIXES)
        if raw.startswith(INGEST_TOKEN_PREFIX):
            if not is_ingest_route:
                # Ingest tokens are single-purpose: they must not open the
                # general API even though they hash like credentials.
                await self._audit(
                    db,
                    action=AuthAuditAction.FAILED,
                    reason="ingest token used outside ingestion route",
                    path=path,
                    ip=request.client.host if request.client else None,
                )
                raise AuthRequiredError(401, "Invalid credentials")
            from app.services.ingest_trust import resolve_ingest_token

            project_id = await resolve_ingest_token(db, raw)
            if project_id is None:
                await self._audit(
                    db,
                    action=AuthAuditAction.FAILED,
                    reason="unknown ingest token",
                    path=path,
                    ip=request.client.host if request.client else None,
                )
                raise AuthRequiredError(401, "Invalid ingest token")
            return AuthContext(
                token_id=uuid.UUID(int=0),
                role=TokenRole.OPERATOR,
                project_ids={project_id},
                name="ingest-source",
            )

        resolved = await verify_token(db, raw)
        if resolved is None:
            await self._audit(
                db,
                action=AuthAuditAction.FAILED,
                reason="unknown, expired or revoked token",
                path=path,
                ip=request.client.host if request.client else None,
                hash_prefix=hash_token(raw)[:12],
            )
            raise AuthRequiredError(401, "Invalid or expired token")
        token, project_ids = resolved
        token.last_used_at = utcnow()
        await db.commit()
        return AuthContext(
            token_id=token.id,
            role=token.role,
            project_ids=None if token.role is TokenRole.ADMIN else project_ids,
            name=token.name,
        )

    async def _audit(self, db, *, action, reason, path, ip, hash_prefix=None) -> None:
        from app.models.auth import AuthenticationAudit

        db.add(
            AuthenticationAudit(
                action=action,
                reason=reason,
                request_path=path,
                client_ip=ip,
                token_hash_prefix=hash_prefix,
                occurred_at=utcnow(),
            )
        )
        await db.commit()


def get_auth_context() -> Optional[AuthContext]:
    """The caller's context for dependency-layer checks (no Request needed)."""
    return auth_context_var.get()


def require_auth_context() -> AuthContext:
    """The caller's context, or refuse the request.

    **Fail closed.** Guards used to read ``get_auth_context()`` and skip their
    check when it was ``None``. That turned "the context is missing" into "this
    caller is allowed to do anything" — the live authorization bypass that
    this hardening pass fixed. Every authorization decision must read the
    context through *this* function, so a missing context is a refusal and not
    an open door.

    For an authenticated ``/api`` route the context is always present (the
    middleware binds it, including the explicit anonymous context when auth is
    intentionally bypassed), so this raise only fires when something is wrong.
    """
    auth = auth_context_var.get()
    if auth is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="Not authenticated")
    return auth


async def require_admin() -> AuthContext:
    """FastAPI dependency: ADMIN role required (token management routes).

    Reads the context the middleware authenticated — no second credential
    parse, no second DB hit.
    """
    from fastapi import HTTPException

    auth = auth_context_var.get()
    if auth is None or not auth.is_admin:
        raise HTTPException(
            status_code=403, detail="This action requires the ADMIN role"
        )
    return auth


class OtlpProtobufMiddleware:
    """Accept OTLP/Protobuf at the OTLP paths, without a second ingestion path.

    A stock OpenTelemetry Collector's ``otlphttp`` exporter defaults to Protobuf;
    before this middleware ARGUS answered ``422`` to the protocol's own default
    encoding, which made "point your collector at ARGUS" true only after the user
    discovered an ``encoding: json`` flag.

    The transform is deliberately at the **edge**, not in the routes:

    * a Protobuf body is decoded into exactly the dictionary the JSON transport
      produces, then replayed downstream as ``application/json``;
    * therefore the route's own pydantic validation, the request-size limit, the
      rate limiter, the auth funnel, the project-scope check and the OpenAPI
      schema all keep working unchanged — and a decoder bug can never become a
      *second, differently-guarded* ingestion path;
    * one adapter, one pipeline: the decoded dict is the same input the JSON path
      already gets.

    Pure ASGI for the same reason :class:`AuthMiddleware` is: a
    ``BaseHTTPMiddleware`` would spawn the downstream endpoint in a task whose
    context was captured before this ran, which is precisely the bug that once
    made authorization a no-op.

    The declared ``Content-Length`` is checked *before* the body is buffered, so
    the byte ceiling that protects every other route also protects this one; an
    oversized Protobuf export is refused with the same ``413`` and never read.
    """

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        if not _is_otlp_export_path(
            scope.get("path", "")
        ) or not is_protobuf_content_type(headers.get("content-type")):
            await self.app(scope, receive, send)
            return

        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    # Refuse before reading, so an oversized export never lands
                    # in memory. Same status, same shape as the body-limit
                    # middleware, which never sees this request.
                    response = JSONResponse(
                        status_code=413,
                        content={
                            "detail": f"Request body exceeds {self.max_bytes} bytes",
                            "error_code": "REQUEST_TOO_LARGE",
                        },
                        headers={"Retry-After": "0"},
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length header"},
                )
                await response(scope, receive, send)
                return

        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"") or b""
            more = bool(message.get("more_body"))
            if len(body) > self.max_bytes:
                # A lying or absent Content-Length cannot get past this: the
                # accumulated size is bounded too, exactly like the JSON path.
                response = JSONResponse(
                    status_code=413,
                    content={
                        "detail": f"Request body exceeds {self.max_bytes} bytes",
                        "error_code": "REQUEST_TOO_LARGE",
                    },
                    headers={"Retry-After": "0"},
                )
                await response(scope, receive, send)
                return

        try:
            decoded = decode_otlp_protobuf(scope["path"], body)
        except OtlpDecodeError as exc:
            response = JSONResponse(
                status_code=400,
                content={
                    "detail": str(exc),
                    "error_code": "OTLP_PROTOBUF_DECODE_FAILED",
                    "expected": describe_expected_body(scope["path"]),
                },
            )
            await response(scope, receive, send)
            return

        # An ADMIN (or multi-grant) credential cannot be implied by the payload,
        # and OTLP/Protobuf has no field to carry ARGUS's tenancy extension, so
        # the destination may be named out-of-band. It is only ever *read* here:
        # the route's scope check still decides whether that project is allowed.
        query = scope.get("query_string", b"").decode("latin-1")
        for key, field in (
            ("project_id", "projectId"),
            ("environment_id", "environmentId"),
        ):
            value = _query_value(query, key)
            if value and field not in decoded:
                decoded[field] = value
        header_project = headers.get("x-argus-project-id")
        if header_project and "projectId" not in decoded:
            decoded["projectId"] = header_project
        header_environment = headers.get("x-argus-environment-id")
        if header_environment and "environmentId" not in decoded:
            decoded["environmentId"] = header_environment

        payload = json.dumps(decoded).encode("utf-8")
        rewritten = [
            (key, value)
            for key, value in scope.get("headers", [])
            if key.decode("latin-1").lower() not in {"content-type", "content-length"}
        ]
        rewritten.append((b"content-type", b"application/json"))
        rewritten.append((b"content-length", str(len(payload)).encode()))
        scope = {**scope, "headers": rewritten}

        delivered = False

        async def replay() -> dict:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {
                "type": "http.request",
                "body": payload,
                "more_body": False,
            }

        await self.app(scope, replay, send)


class CommitBeforeResponseMiddleware:
    """Commit the request's write **before** the response is sent.

    FastAPI runs a dependency's teardown after the response has gone out, and
    ``get_db`` commits there. That ordering is a real defect for any client that
    reads straight after it writes: a ``DELETE`` can return ``204`` while the
    next ``GET`` on another connection still sees the row, because the deleting
    transaction had not committed yet. Measured on this stack, the window is
    tens of milliseconds — and it is exactly the window an automated verifier
    lives in. The phase-10/11 gates and this project's own delete-cascade check
    read immediately after they write, and they failed intermittently for this
    reason rather than for the property they were asserting.

    So the commit happens here instead: immediately before
    ``http.response.start`` is forwarded, the session ``get_db`` opened is
    committed. Three properties make this safe:

    * **The error path is untouched.** An unexpected exception never reaches
      ``http.response.start``, so nothing is committed here; ``get_db`` still
      rolls back when the exception propagates through it.
    * **Refusals still leave their trail.** An ``HTTPException`` is turned into
      a response by the router's own exception handler *below* this middleware,
      so a refusal that wrote an audit row is committed here — the same
      outcome as before, just earlier.
    * **The teardown commit stays the authority.** It runs afterwards and is a
      no-op, so a response produced outside the router (a health check, a
      middleware short-circuit) behaves exactly as it did.

    Registered first in ``main.py``, i.e. innermost among the user middleware,
    so it sits directly above the router and sees the response the route
    produced. Pure ASGI, like ``AuthMiddleware``: a ``BaseHTTPMiddleware`` here
    would run the request in a task whose context is captured before the
    session is set, and the commit would silently never happen.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        committed = False

        async def send_and_commit(message: dict) -> None:
            nonlocal committed
            #: Only the response start matters, only once, and only before the
            #: first byte is delivered: after this the client can act on the
            #: response, and a write it cannot yet see is the bug being fixed.
            if not committed and message.get("type") == "http.response.start":
                committed = True
                await commit_request_session()
            await send(message)

        await self.app(scope, receive, send_and_commit)


def _is_otlp_export_path(path: str) -> bool:
    """True for the three OTLP HTTP export verbs, by shape not by a fixed list."""
    if "/otlp/" not in path:
        return False
    return path.rstrip("/").rsplit("/", 1)[-1] in {"traces", "logs", "metrics"}


def _query_value(query: str, key: str) -> Optional[str]:
    """Read one query parameter without importing a URL parser for a hot path."""
    from urllib.parse import parse_qs

    values = parse_qs(query, keep_blank_values=False)
    found = values.get(key)
    return found[0] if found else None
