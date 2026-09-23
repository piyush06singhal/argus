"""ARGUS Platform Health & Self-Monitoring (Phase 11 §57–§60, §105–§107).

ARGUS needs to know when ARGUS is degraded, for the same reason it needs to know
when production is: an operator reading a dashboard that is silently missing half
its evidence will draw worse conclusions than one reading no dashboard.

What is checked (§57, §58):

* the database (reachable, migration head applied, pool pressure, slow queries),
* Redis (reachable, and honestly reported as *optional*),
* the ingestion worker's lag (unprocessed queue depth),
* each scheduled subsystem's last successful run (forecasts, remediation,
  learning, the control plane itself),
* the AI provider (configured or not — never a hard requirement),
* storage for artifacts.

The distinction that matters, and the reason §59 and §107 are separate
sentences: **readiness** requires only the things ARGUS cannot function without
(database, migrations). Everything optional — Redis, the AI provider, the
learning layer — degrades a *capability* and is reported as such. A readiness
probe that fails because an LLM vendor is down would take the whole platform out
of service for a reason no operator would accept.

Every check is failure-isolated (§60): one subsystem raising is reported as that
subsystem's status, never as an exception out of the health endpoint — a health
endpoint that fails when something is unhealthy is useless exactly when it is
needed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.services.platform_time import aware as _aware

logger = logging.getLogger(__name__)

#: Subsystem status vocabulary. ``UNKNOWN`` means the check itself could not run.
STATUS_OK = "OK"
STATUS_DEGRADED = "DEGRADED"
STATUS_DOWN = "DOWN"
STATUS_DISABLED = "DISABLED"
STATUS_UNKNOWN = "UNKNOWN"

#: Subsystems the platform can run without. A failure here never fails readiness.
OPTIONAL_SUBSYSTEMS = frozenset(
    {
        "redis",
        "ai_provider",
        "learning",
        "forecasting",
        "remediation",
        "artifact_storage",
        "notifications",
    }
)


@dataclass
class SubsystemHealth:
    """One subsystem's health, with what it means for the platform."""

    name: str
    status: str
    required: bool = False
    latency_ms: Optional[float] = None
    detail: Optional[str] = None
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    queue_depth: Optional[int] = None
    error_rate: Optional[float] = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "required": self.required,
            "optional": not self.required,
            "latency_ms": self.latency_ms,
            "detail": self.detail,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "queue_depth": self.queue_depth,
            "error_rate": self.error_rate,
            "metrics": self.metrics,
        }


@dataclass
class PlatformHealthReport:
    """The §58 ARGUSHealth document."""

    as_of: datetime
    status: str
    ready: bool
    subsystems: list[SubsystemHealth] = field(default_factory=list)
    degraded_capabilities: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "status": self.status,
            "ready": self.ready,
            "subsystems": [subsystem.as_dict() for subsystem in self.subsystems],
            "degraded_capabilities": list(self.degraded_capabilities),
            "notes": list(self.notes),
            "summary": self.summary,
        }

    @property
    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for subsystem in self.subsystems:
            counts[subsystem.status] = counts.get(subsystem.status, 0) + 1
        return counts


async def _check_database(
    session: AsyncSession, *, settings: Settings, now: datetime
) -> SubsystemHealth:
    """Database reachability, migration head, pool pressure and slow queries."""
    health = SubsystemHealth(name="database", status=STATUS_OK, required=True)
    started = time.perf_counter()
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        health.status = STATUS_DOWN
        health.detail = f"database unreachable: {type(exc).__name__}"
        health.last_failure_at = now.isoformat()
        return health
    health.latency_ms = round((time.perf_counter() - started) * 1000, 2)
    if health.latency_ms > settings.PLATFORM_DB_SLOW_QUERY_MS:
        health.status = STATUS_DEGRADED
        health.detail = (
            f"the probe took {health.latency_ms}ms, over the "
            f"{settings.PLATFORM_DB_SLOW_QUERY_MS}ms threshold"
        )

    # -- migration state (§105): are we running the schema this code expects?
    try:
        code_head = _code_migration_head()
        applied = await session.scalar(text("SELECT version_num FROM alembic_version"))
        health.metrics["migration_head"] = code_head
        health.metrics["migration_applied"] = applied
        if code_head and applied and code_head != applied:
            health.status = STATUS_DEGRADED
            health.detail = (
                health.detail + "; " if health.detail else ""
            ) + f"the database is at migration {applied}, the code expects {code_head}"
    except Exception as exc:
        #: A fresh SQLite test database has no alembic_version table; that is not
        #: a degradation, so the metric is simply absent.
        health.metrics["migration_status"] = f"unavailable: {type(exc).__name__}"

    # -- pool pressure
    try:
        bind = session.get_bind()
        pool = getattr(bind, "pool", None)
        if pool is not None and hasattr(pool, "checkedout") and hasattr(pool, "size"):
            size = pool.size() or 0
            checked_out = pool.checkedout()
            health.metrics["pool_size"] = size
            health.metrics["pool_checked_out"] = checked_out
            if (
                size
                and (checked_out / size) * 100.0
                >= settings.PLATFORM_DB_POOL_WARN_PERCENT
            ):
                health.status = STATUS_DEGRADED
                health.detail = (
                    health.detail + "; " if health.detail else ""
                ) + f"connection pool is {checked_out}/{size} in use"
    except Exception:  # pragma: no cover - pool introspection is best-effort
        pass
    health.last_success_at = now.isoformat()
    return health


def _code_migration_head() -> Optional[str]:
    """The newest revision the *code* knows about, without running Alembic."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config("alembic.ini")
        script = ScriptDirectory.from_config(config)
        heads = script.get_heads()
        return heads[0] if len(heads) == 1 else None
    except Exception:  # pragma: no cover - the config may be absent (tests)
        return None


def _redis_client(settings: Settings) -> Any:
    """A Redis client, built from settings, or ``None`` if the library is absent.

    The queue adapter owns the client it uses; this is a separate read-only
    connection for diagnostics, so a health check can never leave the worker's
    connection in a different state.
    """
    try:
        import redis.asyncio as aioredis
    except ImportError:  # pragma: no cover - redis is optional
        return None
    return aioredis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=0,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


async def _check_redis(*, settings: Settings, now: datetime) -> SubsystemHealth:
    """Redis: optional, and reported as DISABLED when not configured."""
    from app.services.queue import ALL_QUEUES

    health = SubsystemHealth(name="redis", status=STATUS_UNKNOWN, required=False)
    client = _redis_client(settings)
    if client is None:
        health.status = STATUS_DISABLED
        health.detail = "the redis client library is not installed"
        return health
    try:
        started = time.perf_counter()
        await client.ping()
        health.latency_ms = round((time.perf_counter() - started) * 1000, 2)
        health.status = STATUS_OK
        health.last_success_at = now.isoformat()
        depths: dict[str, int] = {}
        for queue_name in ALL_QUEUES:
            try:
                depths[queue_name] = int(await client.llen(queue_name) or 0)
            except Exception:  # pragma: no cover - a missing key is not a failure
                continue
        health.queue_depth = sum(depths.values())
        health.metrics["queue_depths"] = depths
        if health.queue_depth > settings.PLATFORM_QUEUE_DEPTH_WARN:
            health.status = STATUS_DEGRADED
            health.detail = (
                f"{health.queue_depth} job(s) are queued across "
                f"{len(depths)} queue(s), over the "
                f"{settings.PLATFORM_QUEUE_DEPTH_WARN} threshold: the worker is "
                "falling behind"
            )
        else:
            health.detail = f"{health.queue_depth} job(s) queued"
    except Exception as exc:
        health.status = STATUS_DEGRADED
        health.detail = (
            f"Redis unreachable ({type(exc).__name__}); ingestion continues but "
            "without the broker, so queued work is not being handed out"
        )
        health.last_failure_at = now.isoformat()
    finally:
        try:
            await client.aclose()
        except Exception:  # pragma: no cover
            pass
    return health


async def _check_ingestion_queue(
    session: AsyncSession, *, settings: Settings, now: datetime
) -> SubsystemHealth:
    """Ingestion health: dead-letter accumulation and rejected events (§58)."""
    health = SubsystemHealth(name="ingestion", status=STATUS_OK, required=False)
    try:
        from app.models.ingestion import IngestionFailure

        failures = await session.scalar(
            select(func.count(IngestionFailure.id)).where(
                IngestionFailure.failed_at >= now - timedelta(days=1)
            )
        )
        health.metrics["failures_last_24h"] = int(failures or 0)
        if int(failures or 0) > settings.PLATFORM_INGESTION_FAILURE_WARN:
            health.status = STATUS_DEGRADED
            health.detail = (
                f"{failures} event(s) were rejected in the last 24h, over the "
                f"{settings.PLATFORM_INGESTION_FAILURE_WARN} threshold"
            )
        else:
            health.detail = f"{failures or 0} rejected event(s) in the last 24h"
    except Exception as exc:
        health.status = STATUS_UNKNOWN
        health.detail = f"ingestion history unavailable ({type(exc).__name__})"
    health.last_success_at = now.isoformat()
    return health


async def _check_last_run(
    session: AsyncSession,
    *,
    name: str,
    model: Any,
    timestamp_column: str,
    max_age: timedelta,
    now: datetime,
    required: bool = False,
    disabled: bool = False,
) -> SubsystemHealth:
    """Whether a scheduled subsystem has run recently enough to be trusted.

    This is deliberately a *staleness* check rather than a heartbeat: a subsystem
    whose last row is older than its interval has implicitly failed, which is
    exactly the failure mode a heartbeat would hide.
    """
    health = SubsystemHealth(name=name, status=STATUS_UNKNOWN, required=required)
    if disabled:
        health.status = STATUS_DISABLED
        health.detail = "switched off by configuration"
        return health
    try:
        column = getattr(model, timestamp_column)
        latest = await session.scalar(select(func.max(column)))
    except Exception as exc:
        health.detail = f"no run history available ({type(exc).__name__})"
        return health
    if latest is None:
        health.status = STATUS_OK
        health.detail = "no runs recorded yet"
        return health
    moment = _aware(latest)
    if moment is None:
        #: Nothing has ever succeeded. Reported as unknown rather than as a very
        #: large age, which would read as "long overdue" instead of "never ran".
        return health
    age = now - moment
    health.last_success_at = moment.isoformat()
    health.metrics["age_seconds"] = round(age.total_seconds(), 1)
    if age > max_age * 3:
        health.status = STATUS_DEGRADED
        health.detail = (
            f"the last run was {int(age.total_seconds() // 60)} minutes ago, "
            f"more than three intervals ({int(max_age.total_seconds() // 60)}m each)"
        )
    elif age > max_age:
        health.status = STATUS_OK
        health.detail = "the last run is slightly overdue but within tolerance"
    else:
        health.status = STATUS_OK
        health.detail = f"last run {int(age.total_seconds())}s ago"
    return health


async def _check_ai_provider(*, settings: Settings, now: datetime) -> SubsystemHealth:
    """The AI provider is optional by construction (§59)."""
    health = SubsystemHealth(name="ai_provider", status=STATUS_UNKNOWN, required=False)
    enabled = bool(getattr(settings, "DEBUG_AI_ENABLED", False)) or bool(
        getattr(settings, "PLATFORM_AI_ASSISTANT_ENABLED", False)
    )
    if not enabled:
        health.status = STATUS_DISABLED
        health.detail = (
            "AI features are switched off; every deterministic capability "
            "(observability, incidents, RCA, reproduction, remediation) works "
            "without them"
        )
        return health
    provider = getattr(settings, "DEBUG_AI_PROVIDER", None) or "unconfigured"
    key = getattr(settings, "DEBUG_AI_API_KEY", "") or ""
    if not key:
        health.status = STATUS_DEGRADED
        health.detail = (
            f"AI is enabled (provider={provider}) but no credential is configured"
        )
        return health
    health.status = STATUS_OK
    health.detail = f"provider {provider} is configured"
    health.metrics["provider"] = provider
    return health


async def platform_health(
    session: AsyncSession,
    *,
    settings: Optional[Settings] = None,
    now: Optional[datetime] = None,
) -> PlatformHealthReport:
    """Run every §58 check, failure-isolated."""
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    report = PlatformHealthReport(as_of=moment, status=STATUS_OK, ready=True)

    checks: list[tuple[str, Any]] = [
        ("database", lambda: _check_database(session, settings=settings, now=moment)),
        ("redis", lambda: _check_redis(settings=settings, now=moment)),
        (
            "ingestion",
            lambda: _check_ingestion_queue(session, settings=settings, now=moment),
        ),
    ]

    for name, runner in checks:
        try:
            report.subsystems.append(await runner())
        except Exception as exc:
            logger.warning("health check %s raised", name, exc_info=True)
            report.subsystems.append(
                SubsystemHealth(
                    name=name,
                    status=STATUS_UNKNOWN,
                    required=name in ("database",),
                    detail=f"the check itself failed: {type(exc).__name__}",
                )
            )

    # -- scheduled subsystems, each with its own expected interval
    try:
        from app.models.reliability import ReliabilityForecast

        report.subsystems.append(
            await _check_last_run(
                session,
                name="forecasting",
                model=ReliabilityForecast,
                timestamp_column="generated_at",
                max_age=timedelta(seconds=settings.RELIABILITY_SWEEP_INTERVAL_SECONDS),
                now=moment,
                disabled=not settings.RELIABILITY_FORECASTING_ENABLED,
            )
        )
    except Exception as exc:  # pragma: no cover - optional subsystem
        report.subsystems.append(
            SubsystemHealth(
                name="forecasting",
                status=STATUS_UNKNOWN,
                detail=f"check failed: {type(exc).__name__}",
            )
        )

    try:
        from app.models.remediation import RemediationAction

        report.subsystems.append(
            await _check_last_run(
                session,
                name="remediation",
                model=RemediationAction,
                timestamp_column="updated_at",
                max_age=timedelta(seconds=settings.REMEDIATION_SWEEP_INTERVAL_SECONDS),
                now=moment,
                disabled=not settings.REMEDIATION_EXECUTION_ENABLED,
            )
        )
    except Exception as exc:  # pragma: no cover - optional subsystem
        report.subsystems.append(
            SubsystemHealth(
                name="remediation",
                status=STATUS_UNKNOWN,
                detail=f"check failed: {type(exc).__name__}",
            )
        )

    try:
        from app.models.intelligence import LearningRun

        report.subsystems.append(
            await _check_last_run(
                session,
                name="learning",
                model=LearningRun,
                timestamp_column="created_at",
                max_age=timedelta(seconds=settings.INTELLIGENCE_SWEEP_INTERVAL_SECONDS),
                now=moment,
                disabled=not settings.INTELLIGENCE_LEARNING_ENABLED,
            )
        )
    except Exception as exc:  # pragma: no cover - optional subsystem
        report.subsystems.append(
            SubsystemHealth(
                name="learning",
                status=STATUS_UNKNOWN,
                detail=f"check failed: {type(exc).__name__}",
            )
        )

    try:
        from app.models.platform import PlatformEvent

        report.subsystems.append(
            await _check_last_run(
                session,
                name="control_plane",
                model=PlatformEvent,
                timestamp_column="occurred_at",
                max_age=timedelta(seconds=settings.PLATFORM_SWEEP_INTERVAL_SECONDS),
                now=moment,
                disabled=not settings.PLATFORM_ENABLED,
            )
        )
    except Exception as exc:  # pragma: no cover - optional subsystem
        report.subsystems.append(
            SubsystemHealth(
                name="control_plane",
                status=STATUS_UNKNOWN,
                detail=f"check failed: {type(exc).__name__}",
            )
        )

    try:
        report.subsystems.append(
            await _check_ai_provider(settings=settings, now=moment)
        )
    except Exception as exc:  # pragma: no cover
        report.subsystems.append(
            SubsystemHealth(
                name="ai_provider",
                status=STATUS_UNKNOWN,
                detail=f"check failed: {type(exc).__name__}",
            )
        )

    # -- roll-up: readiness ignores optional subsystems, health does not
    for subsystem in report.subsystems:
        if subsystem.required and subsystem.status in (STATUS_DOWN, STATUS_UNKNOWN):
            report.ready = False
        if subsystem.status == STATUS_DOWN and subsystem.required:
            report.status = STATUS_DOWN
        elif subsystem.status == STATUS_DEGRADED:
            report.degraded_capabilities.append(subsystem.name)
            if report.status == STATUS_OK:
                report.status = STATUS_DEGRADED
        elif (
            subsystem.status == STATUS_DOWN
            and not subsystem.required
            and report.status == STATUS_OK
        ):
            report.status = STATUS_DEGRADED

    if report.degraded_capabilities:
        report.notes.append(
            "degraded: "
            + ", ".join(sorted(set(report.degraded_capabilities)))
            + ". ARGUS continues with reduced capability; nothing required is down."
        )
    if not report.ready:
        report.notes.append(
            "not ready: a required subsystem is unavailable. Readiness deliberately "
            "ignores optional subsystems so an AI or cache outage cannot take the "
            "platform out of service."
        )
    return report


async def readiness(
    session: AsyncSession, *, settings: Optional[Settings] = None
) -> dict[str, Any]:
    """The §107 readiness verdict: database and migrations only."""
    settings = settings or get_settings()
    report = await platform_health(session, settings=settings)
    required = [subsystem for subsystem in report.subsystems if subsystem.required]
    return {
        "ready": report.ready,
        "required_subsystems": [subsystem.as_dict() for subsystem in required],
        "optional_subsystems": [
            subsystem.as_dict()
            for subsystem in report.subsystems
            if not subsystem.required
        ],
        "reason": (
            "all required subsystems are answering"
            if report.ready
            else "a required subsystem is unavailable"
        ),
    }


async def dependencies(
    session: AsyncSession, *, settings: Optional[Settings] = None
) -> dict[str, Any]:
    """The §106 dependency report: what ARGUS relies on, and how hard."""
    settings = settings or get_settings()
    report = await platform_health(session, settings=settings)
    return {
        "as_of": report.as_of.isoformat(),
        "dependencies": [subsystem.as_dict() for subsystem in report.subsystems],
        "required_count": sum(1 for s in report.subsystems if s.required),
        "optional_count": sum(1 for s in report.subsystems if not s.required),
        "graceful_degradation": {
            "ai_unavailable": (
                "deterministic analysis, incidents, RCA, reproduction and "
                "remediation continue; AI-drafted narrative and the case "
                "assistant report themselves unavailable"
            ),
            "redis_unavailable": (
                "the durable database queue is used instead; caching is skipped"
            ),
            "forecasting_unavailable": (
                "observability, incidents and RCA continue; predictions are "
                "reported as unavailable rather than absent"
            ),
            "learning_unavailable": (
                "nothing blocks; incident ingestion and remediation do not depend "
                "on the learning tables"
            ),
        },
    }


def capability_available(report: PlatformHealthReport, capability: str) -> bool:
    """Whether a named capability should be advertised as available (§59).

    Read by the API layer *before* a feature is offered, so a degraded subsystem
    produces "the assistant is unavailable because the AI provider is not
    configured" instead of a stack trace.
    """
    for subsystem in report.subsystems:
        if subsystem.name == capability:
            return subsystem.status in (
                STATUS_OK,
                STATUS_UNKNOWN,
                STATUS_DISABLED,
            ) and (subsystem.status != STATUS_DISABLED)
    return True


__all__ = [
    "OPTIONAL_SUBSYSTEMS",
    "STATUS_DEGRADED",
    "STATUS_DISABLED",
    "STATUS_DOWN",
    "STATUS_OK",
    "STATUS_UNKNOWN",
    "PlatformHealthReport",
    "SubsystemHealth",
    "capability_available",
    "dependencies",
    "platform_health",
    "readiness",
]
