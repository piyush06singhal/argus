"""ARGUS API - Main Application."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.routes import api_v1_router
from app.api.v1.routes.health import router as health_router
from app.core.config import get_settings
from app.core.database import async_session_factory, close_db, init_db
from app.core.edge import (
    AuthMiddleware,
    BodySizeLimitMiddleware,
    CommitBeforeResponseMiddleware,
    JsonDepthLimitMiddleware,
    OtlpProtobufMiddleware,
    RateLimitMiddleware,
)
from app.core.runtime_metrics import gauge, init_counter
from app.core.security import (
    PUBLIC_BOOTSTRAP_PATHS,
    PUBLIC_PATHS,
    bootstrap_admin_token,
)
from app.services.anomaly_sweep import sweep_forever
from app.services.code_sweep import sweep_code_intelligence_forever
from app.services.fix_sweep import sweep_fix_forever
from app.services.intelligence_sweep import (
    sweep_forever as sweep_learning_forever,
)
from app.services.platform_sweep import sweep_forever as sweep_platform_forever
from app.services.reliability_sweep import sweep_reliability_forever
from app.services.remediation_sweep import sweep_remediations_forever
from app.services.reproduction_sweep import sweep_reproductions_forever
from app.services.worker_runner import make_worker

settings = get_settings()

logger = logging.getLogger("argus.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan events."""
    # Startup
    await init_db()

    # Identity-provider state, exported from the first scrape (hardening W2).
    # The counters are *initialized at zero* rather than left to appear on
    # first use: an alert on ``argus_oidc_login_failures_total`` must be able to
    # reference the series in a fresh deployment and mean "no failures" instead
    # of "no data" — the two are indistinguishable to ``rate()``.
    init_counter(
        "argus_oidc_logins_total", help_text="Successful single sign-on logins"
    )
    init_counter(
        "argus_oidc_login_failures_total",
        help_text="Refused single sign-on logins",
    )
    gauge(
        "argus_oidc_enabled",
        1.0 if settings.OIDC_ENABLED else 0.0,
        help_text="1 when single sign-on is configured on this deployment",
    )

    # Authentication bootstrap (hardening W1). On first boot, mint the root
    # ADMIN token and print it exactly once — an operator's only chance to
    # capture it. With auth disabled (test/dev bypass) this still runs so a
    # token exists when auth is turned on.
    if not settings.is_testing:
        async with async_session_factory() as db:
            raw_token = await bootstrap_admin_token(db)
            if raw_token is not None:
                logger.warning("=" * 72)
                logger.warning(
                    "ARGUS bootstrap admin token (shown ONCE — store it now): %s",
                    raw_token,
                )
                logger.warning("=" * 72)

    #: One flag for every background task in this process (hardening W6, G12).
    #: Nine independent timers that each decide for themselves whether to run is
    #: how a four-worker deployment ends up running every sweep four times. With
    #: this gate, "the HTTP processes serve and one worker process works" is a
    #: configuration, not an emergent property.
    background = settings.background_jobs_active
    if not background:
        logger.info(
            "Background jobs are disabled in this process "
            "(BACKGROUND_JOBS_ENABLED=%s, environment=%s): the API will serve "
            "requests, queue work, and let another process execute it.",
            settings.BACKGROUND_JOBS_ENABLED,
            settings.API_ENVIRONMENT,
        )

    # Start the background ingestion worker (Phase 1 §41–§42). Skipped under
    # ``test`` so unit/integration tests don't contend with Redis, and can be
    # disabled via INGESTION_WORKER_ENABLED when running a drainer separately.
    worker = None
    worker_task: Optional[asyncio.Task] = None
    if background and settings.INGESTION_WORKER_ENABLED:
        worker = make_worker(async_session_factory)
        worker_task = asyncio.create_task(worker.run_forever())

    # Scheduled detection sweep (Phase 3 §20). The ingest hook handles
    # responsiveness; this covers sparse/late telemetry and any ingestion path
    # without a hook. Skipped under ``test`` so tests never race a timer.
    sweep_task: Optional[asyncio.Task] = None
    if (
        background
        and settings.ANOMALY_DETECTION_ENABLED
        and settings.ANOMALY_SWEEP_ENABLED
    ):
        sweep_task = asyncio.create_task(sweep_forever(async_session_factory))

    # Reproduction reaper (Phase 5 §39, §55). Closes experiments abandoned by a
    # dead worker and destroys sandboxes left behind, so a crash cannot leak a
    # sandbox or strand an experiment in a non-terminal state.
    repro_sweep_task: Optional[asyncio.Task] = None
    if background and settings.REPRO_SWEEP_ENABLED:
        repro_sweep_task = asyncio.create_task(
            sweep_reproductions_forever(async_session_factory)
        )

    # Code-intelligence reaper (Phase 6 §55–§57). Closes debug sessions and
    # analysis runs abandoned by a dead process and un-sticks repositories
    # whose index_status is stuck at INDEXING.
    code_sweep_task: Optional[asyncio.Task] = None
    if background and settings.CODE_SWEEP_ENABLED:
        code_sweep_task = asyncio.create_task(
            sweep_code_intelligence_forever(async_session_factory)
        )

    # Fix-workspace reaper (Phase 7 §48, §54). Destroys verification
    # workspaces left behind by a dead process.
    fix_sweep_task: Optional[asyncio.Task] = None
    if background and settings.FIX_SWEEP_ENABLED:
        fix_sweep_task = asyncio.create_task(sweep_fix_forever(async_session_factory))

    # Scheduled predictive-reliability sweep (Phase 8 §58, §77). Generates the
    # forecasts that are due, scores the ones whose horizons have elapsed,
    # refreshes early warnings and expires what has passed. Skipped under
    # ``test`` so tests never race a timer; the worker queue and the API can
    # both drive the same work explicitly.
    reliability_sweep_task: Optional[asyncio.Task] = None
    if (
        background
        and settings.RELIABILITY_FORECASTING_ENABLED
        and settings.RELIABILITY_SWEEP_ENABLED
    ):
        reliability_sweep_task = asyncio.create_task(
            sweep_reliability_forever(async_session_factory)
        )

    # Scheduled remediation sweep (Phase 9 §39). Without it an action that policy
    # authorized and that no worker picked up — or one whose verification window
    # has since filled — would never reach a terminal state. The sweep never
    # authorizes anything and never decides anything: it expires what nobody acted
    # on, closes attempts a dead process abandoned, cools breakers, retires
    # controls past their deadline, and executes only what already cleared every
    # gate. Skipped under ``test`` so tests never race a timer.
    remediation_sweep_task: Optional[asyncio.Task] = None
    if (
        background
        and settings.REMEDIATION_EXECUTION_ENABLED
        and settings.REMEDIATION_SWEEP_ENABLED
    ):
        remediation_sweep_task = asyncio.create_task(
            sweep_remediations_forever(async_session_factory)
        )

    # Scheduled learning sweep (Phase 10 §29, §63, §80). Runs the learning
    # pipeline for projects that have waiting events, decays knowledge whose
    # confirming evidence has stopped arriving, and expires stale
    # recommendations. It never activates knowledge — that is the lifecycle's
    # decision under §71–§73. Skipped under ``test`` so tests never race a timer.
    learning_sweep_task: Optional[asyncio.Task] = None
    if background and settings.INTELLIGENCE_SWEEP_ENABLED:
        learning_sweep_task = asyncio.create_task(
            sweep_learning_forever(async_session_factory)
        )

    # Unified platform sweep (Phase 11 §10, §12, §35, §58, §88). Correlates
    # pending platform events into case timelines, routes live incidents into
    # cases, advances or stops due workflows, recomputes SLOs and error budgets,
    # and checks cross-phase consistency. It never authorizes anything and never
    # bypasses the Phase 9 control plane: a pause on ``platform_sweep`` stops the
    # pass. Skipped under ``test`` so tests never race a timer.
    platform_sweep_task: Optional[asyncio.Task] = None
    if background and settings.PLATFORM_SWEEP_ENABLED:
        platform_sweep_task = asyncio.create_task(
            sweep_platform_forever(async_session_factory)
        )

    yield

    # Shutdown
    if platform_sweep_task is not None:
        platform_sweep_task.cancel()
        try:
            await platform_sweep_task
        except asyncio.CancelledError:
            pass
    if learning_sweep_task is not None:
        learning_sweep_task.cancel()
        try:
            await learning_sweep_task
        except asyncio.CancelledError:
            pass
    if remediation_sweep_task is not None:
        remediation_sweep_task.cancel()
        try:
            await remediation_sweep_task
        except asyncio.CancelledError:
            pass
    if reliability_sweep_task is not None:
        reliability_sweep_task.cancel()
        try:
            await reliability_sweep_task
        except asyncio.CancelledError:
            pass
    if fix_sweep_task is not None:
        fix_sweep_task.cancel()
        try:
            await fix_sweep_task
        except asyncio.CancelledError:
            pass
    if code_sweep_task is not None:
        code_sweep_task.cancel()
        try:
            await code_sweep_task
        except asyncio.CancelledError:
            pass
    if repro_sweep_task is not None:
        repro_sweep_task.cancel()
        try:
            await repro_sweep_task
        except asyncio.CancelledError:
            pass
    if sweep_task is not None:
        sweep_task.cancel()
        try:
            await sweep_task
        except asyncio.CancelledError:
            pass
    if worker is not None and worker_task is not None:
        worker.stop()
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    await close_db()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=settings.APP_DESCRIPTION,
    lifespan=lifespan,
    docs_url="/docs" if settings.is_development else None,
    redoc_url="/redoc" if settings.is_development else None,
)

# Commit the request's database work before the response is sent, so a caller
# can always observe its own write. Added first, which makes it the innermost
# user middleware — right above the router — so it sees the response the route
# produced and nothing above it can send a response on the route's behalf.
app.add_middleware(CommitBeforeResponseMiddleware)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Edge hardening (W1). Middleware runs in reverse registration order, so the
# LAST added runs FIRST: auth → rate limit → body limit → CORS → routes.
app.add_middleware(
    AuthMiddleware,
    public_paths=PUBLIC_PATHS,
    #: The three SSO routes that must run before a credential exists. They are
    #: bound to a zero-grant context, never the auth-disabled ADMIN one.
    bootstrap_paths=PUBLIC_BOOTSTRAP_PATHS,
)
app.add_middleware(
    RateLimitMiddleware,
    per_minute=settings.RATE_LIMIT_PER_MINUTE,
    burst=settings.RATE_LIMIT_BURST,
    # The unit suite legitimately bursts far past any production ceiling; load
    # behaviour is verified by the limiter unit tests and the W6 harness.
    enabled=settings.RATE_LIMIT_ENABLED and not settings.is_testing,
    #: Shared bucket across replicas when Redis is configured (the ceiling used
    #: to multiply by the replica count). No URL is passed in the test
    #: environment, so a unit test can never open a connection.
    backend=settings.RATE_LIMIT_BACKEND,
    redis_url=None if settings.is_testing else settings.REDIS_URL,
    key_prefix=settings.RATE_LIMIT_REDIS_KEY_PREFIX,
)
app.add_middleware(
    JsonDepthLimitMiddleware,
    max_depth=settings.MAX_JSON_DEPTH,
)
app.add_middleware(
    BodySizeLimitMiddleware,
    max_bytes=settings.MAX_REQUEST_BODY_BYTES,
)
# OTLP/Protobuf acceptance. Added LAST so it runs FIRST: it must see the original
# Content-Type and Content-Length before the JSON-oriented middlewares inspect
# the request, and it hands the decoded payload on with the same size ceiling.
app.add_middleware(
    OtlpProtobufMiddleware,
    max_bytes=settings.MAX_REQUEST_BODY_BYTES,
)


# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle uncaught exceptions."""
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "error_code": "INTERNAL_ERROR",
        },
    )


# Prometheus scrape endpoint at root (standard Prometheus convention) and
# versioned under /api/v1 (included in api_v1_router).
from app.api.v1.routes.metrics_export import router as prometheus_router  # noqa: E402

# Include routers
app.include_router(health_router)
app.include_router(prometheus_router)
app.include_router(api_v1_router)


@app.get("/", tags=["Root"])
async def root() -> dict:
    """Root endpoint."""
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "description": settings.APP_DESCRIPTION,
        "environment": settings.API_ENVIRONMENT,
        "docs": "/docs" if settings.is_development else None,
    }
