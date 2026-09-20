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

    yield

    # Shutdown
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
