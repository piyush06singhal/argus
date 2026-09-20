"""ARGUS Health Routes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.schemas.base import DependencyHealth, HealthResponse

router = APIRouter(tags=["Health"])

settings = get_settings()

# Redis connection settings derived from REDIS_URL / env defaults
REDIS_HOST = settings.REDIS_HOST
REDIS_PORT = settings.REDIS_PORT

settings = get_settings()


@router.get("/health/live", response_model=HealthResponse)
async def health_live() -> HealthResponse:
    """Liveness check - is the service running?"""
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc),
        version=settings.APP_VERSION,
        environment=settings.API_ENVIRONMENT,
    )


@router.get("/health/ready", response_model=HealthResponse)
async def health_ready(db: AsyncSession = Depends(get_db)) -> HealthResponse:
    """Readiness check - is the service ready to handle requests?"""
    try:
        # Check database connectivity
        await db.execute(select(1))
        db_status = "healthy"
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"

    status = "healthy" if db_status == "healthy" else "degraded"

    return HealthResponse(
        status=status,
        timestamp=datetime.now(timezone.utc),
        version=settings.APP_VERSION,
        environment=settings.API_ENVIRONMENT,
    )


@router.get("/health/dependencies")
async def health_dependencies(db: AsyncSession = Depends(get_db)) -> dict:
    """Check health of all dependencies."""
    dependencies = []

    # Check PostgreSQL
    try:
        start_time = datetime.now(timezone.utc)
        await db.execute(select(1))
        latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        dependencies.append(
            DependencyHealth(
                name="postgresql",
                status="healthy",
                latency_ms=latency,
            )
        )
    except Exception as e:
        dependencies.append(
            DependencyHealth(
                name="postgresql",
                status="unhealthy",
                error=str(e),
            )
        )

    # Check Redis via TCP connect + PING (no full client required in health check)
    try:
        start_time = datetime.now(timezone.utc)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(REDIS_HOST, REDIS_PORT),
            timeout=2.0,
        )
        writer.write(b"PING\r\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(16), timeout=2.0)
        writer.close()
        await writer.wait_closed()
        latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        if data and data.startswith(b"+PONG"):
            dependencies.append(
                DependencyHealth(name="redis", status="healthy", latency_ms=latency)
            )
        else:
            dependencies.append(
                DependencyHealth(
                    name="redis", status="unhealthy", error="unexpected PING response"
                )
            )
    except asyncio.TimeoutError:
        dependencies.append(
            DependencyHealth(
                name="redis", status="unhealthy", error="Redis PING timed out"
            )
        )
    except (ConnectionRefusedError, OSError) as e:
        dependencies.append(
            DependencyHealth(name="redis", status="unhealthy", error=str(e))
        )

    overall_status = "healthy"
    for dep in dependencies:
        if dep.status == "unhealthy":
            overall_status = "degraded"
            break

    return {
        "status": overall_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dependencies": [dep.model_dump() for dep in dependencies],
    }
