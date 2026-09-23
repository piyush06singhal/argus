"""ARGUS API - Main Application."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.routes import api_v1_router
from app.api.v1.routes.health import router as health_router
from app.core.config import get_settings
from app.core.database import async_session_factory, close_db, init_db
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan events."""
    # Startup
    await init_db()

    # Start the background ingestion worker (Phase 1 §41–§42). Skipped under
    # ``test`` so unit/integration tests don't contend with Redis, and can be
    # disabled via INGESTION_WORKER_ENABLED when running a drainer separately.
    worker = None
    worker_task: Optional[asyncio.Task] = None
    if not settings.is_testing and settings.INGESTION_WORKER_ENABLED:
        worker = make_worker(async_session_factory)
        worker_task = asyncio.create_task(worker.run_forever())

    # Scheduled detection sweep (Phase 3 §20). The ingest hook handles
    # responsiveness; this covers sparse/late telemetry and any ingestion path
    # without a hook. Skipped under ``test`` so tests never race a timer.
    sweep_task: Optional[asyncio.Task] = None
    if (
        not settings.is_testing
        and settings.ANOMALY_DETECTION_ENABLED
        and settings.ANOMALY_SWEEP_ENABLED
    ):
        sweep_task = asyncio.create_task(sweep_forever(async_session_factory))

    # Reproduction reaper (Phase 5 §39, §55). Closes experiments abandoned by a
    # dead worker and destroys sandboxes left behind, so a crash cannot leak a
    # sandbox or strand an experiment in a non-terminal state.
    repro_sweep_task: Optional[asyncio.Task] = None
    if not settings.is_testing and settings.REPRO_SWEEP_ENABLED:
        repro_sweep_task = asyncio.create_task(
            sweep_reproductions_forever(async_session_factory)
        )

    # Code-intelligence reaper (Phase 6 §55–§57). Closes debug sessions and
    # analysis runs abandoned by a dead process and un-sticks repositories
    # whose index_status is stuck at INDEXING.
    code_sweep_task: Optional[asyncio.Task] = None
    if not settings.is_testing and settings.CODE_SWEEP_ENABLED:
        code_sweep_task = asyncio.create_task(
            sweep_code_intelligence_forever(async_session_factory)
        )

    # Fix-workspace reaper (Phase 7 §48, §54). Destroys verification
    # workspaces left behind by a dead process.
    fix_sweep_task: Optional[asyncio.Task] = None
    if not settings.is_testing and settings.FIX_SWEEP_ENABLED:
        fix_sweep_task = asyncio.create_task(sweep_fix_forever(async_session_factory))

    # Scheduled predictive-reliability sweep (Phase 8 §58, §77). Generates the
    # forecasts that are due, scores the ones whose horizons have elapsed,
    # refreshes early warnings and expires what has passed. Skipped under
    # ``test`` so tests never race a timer; the worker queue and the API can
    # both drive the same work explicitly.
    reliability_sweep_task: Optional[asyncio.Task] = None
    if (
        not settings.is_testing
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
        not settings.is_testing
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
    if not settings.is_testing and settings.INTELLIGENCE_SWEEP_ENABLED:
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
    if not settings.is_testing and settings.PLATFORM_SWEEP_ENABLED:
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

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
