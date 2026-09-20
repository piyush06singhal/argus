#!/usr/bin/env python3
"""ARGUS Phase 3 anomaly & incident benchmark (§48, §47).

Builds synthetic telemetry and anomalies at two scales and measures the
deterministic operations that the API and worker depend on:

* detection over a telemetry window (rules × samples)
* fingerprint deduplication on re-run (the noise-reduction path)
* correlation clustering (union-find over candidate anomalies)
* incident persistence (timeline + evidence writes)
* the dashboard / reliability-metric aggregates

Results are informational — they do not claim production-scale performance.
Numbers are reported for whatever machine runs it.

Usage:
    python infrastructure/anomaly-benchmark.py                 # temp SQLite
    DATABASE_URL=... python infrastructure/anomaly-benchmark.py --postgres
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "api"))


def _setup_database_url() -> None:
    """Point DATABASE_URL at a temp SQLite file unless --postgres is passed.

    Must run BEFORE importing ``app.core.database`` — the async engine is
    created at import time from the resolved settings.
    """
    parser = argparse.ArgumentParser(description="ARGUS anomaly benchmark")
    parser.add_argument(
        "--postgres",
        action="store_true",
        help="Use DATABASE_URL (default: temp SQLite file)",
    )
    args, _ = parser.parse_known_args()
    if not args.postgres:
        fd, path = tempfile.mkstemp(suffix=".anomalybench.db")
        os.close(fd)
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"


_setup_database_url()

from sqlalchemy import func, select  # noqa: E402

from app.core.database import Base, async_session_factory, engine  # noqa: E402
from app.models.anomaly import (  # noqa: E402
    Anomaly,
    AnomalyRule,
    AnomalySeverity,
    AnomalySource,
    AnomalyStatus,
    AnomalyType,
    BaselineStrategy,
    RuleCondition,
)
from app.models.observability import MetricRecord, MetricType  # noqa: E402
from app.models.project import Environment, SoftwareProject  # noqa: E402
from app.models.system import (  # noqa: E402
    ComponentCategory,
    ComponentDependency,
    SystemComponent,
)
from app.services.anomaly_detection import AnomalyDetectionService  # noqa: E402
from app.services.anomaly_metrics import (  # noqa: E402
    incident_dashboard,
    reliability_metrics,
)
from app.services.incident_correlation import (  # noqa: E402
    IncidentCorrelationEngine,
)
from app.services.incident_manager import IncidentManager  # noqa: E402

RESULTS: list[tuple[str, float]] = []


def record(label: str, started: float) -> None:
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    RESULTS.append((label, elapsed_ms))
    print(f"  {label:<58} {elapsed_ms:>9.1f} ms")


async def _init_schema() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _build_world(
    *, components: int, samples_per_component: int, anomalies: int
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    """Create a project, a component graph, telemetry, and ungrouped anomalies."""
    async with async_session_factory() as db:
        project = SoftwareProject(
            name=f"Bench {components}", slug=f"bench-{uuid.uuid4().hex[:10]}"
        )
        db.add(project)
        await db.flush()
        environment = Environment(
            project_id=project.id,
            name="production",
            environment_type="PRODUCTION",
        )
        db.add(environment)
        await db.flush()

        now = datetime.now(timezone.utc)
        nodes: list[SystemComponent] = []
        for index in range(components):
            node = SystemComponent(
                project_id=project.id,
                environment_id=environment.id,
                name=f"svc-{index:04d}",
                component_type=ComponentCategory.SERVICE,
            )
            nodes.append(node)
            db.add(node)
        await db.flush()

        # A chain plus cross-links: realistic adjacency for the correlator.
        for index in range(components - 1):
            db.add(
                ComponentDependency(
                    source_component_id=nodes[index].id,
                    target_component_id=nodes[index + 1].id,
                    dependency_type="HTTP",
                )
            )
        for index in range(0, max(0, components - 4), 4):
            db.add(
                ComponentDependency(
                    source_component_id=nodes[index].id,
                    target_component_id=nodes[index + 2].id,
                    dependency_type="HTTP",
                )
            )
        await db.flush()

        # Telemetry for the detection pass.
        for index, node in enumerate(nodes):
            for sample in range(samples_per_component):
                db.add(
                    MetricRecord(
                        project_id=project.id,
                        environment_id=environment.id,
                        component_id=node.id,
                        timestamp=now - timedelta(seconds=(samples_per_component - sample) * 10),
                        metric_name="bench.latency.p95",
                        metric_type=MetricType.GAUGE,
                        value=100.0 + (sample % 5),
                        unit="ms",
                    )
                )

        db.add(
            AnomalyRule(
                project_id=project.id,
                environment_id=environment.id,
                name="Bench latency ceiling",
                anomaly_type=AnomalyType.LATENCY_SPIKE,
                condition=RuleCondition.THRESHOLD,
                metric_name="bench.latency.p95",
                baseline_strategy=BaselineStrategy.STATIC,
                expected_value=100.0,
                threshold=1_000.0,
                min_samples=3,
                window_seconds=3600,
                cooldown_seconds=0,
                persistence_cycles=1,
                severity=AnomalySeverity.HIGH,
                enabled=True,
            )
        )

        # Pre-existing ungrouped anomalies for the correlation pass.
        for index in range(anomalies):
            node = nodes[index % len(nodes)]
            db.add(
                Anomaly(
                    project_id=project.id,
                    environment_id=environment.id,
                    component_id=node.id,
                    anomaly_type=(
                        AnomalyType.LATENCY_SPIKE
                        if index % 3 == 0
                        else AnomalyType.ERROR_RATE_SPIKE
                    ),
                    severity=(
                        AnomalySeverity.HIGH
                        if index % 5 == 0
                        else AnomalySeverity.MEDIUM
                    ),
                    status=AnomalyStatus.DETECTED,
                    source=AnomalySource.METRIC,
                    metric_name=f"bench.metric.{index % 7}",
                    fingerprint=f"bench-{index}-{uuid.uuid4().hex}",
                    detected_at=now - timedelta(seconds=(index % 240)),
                )
            )
        await db.commit()
        return project.id, environment.id, [n.id for n in nodes]


async def bench_detection(
    project_id: uuid.UUID, environment_id: uuid.UUID, *, label: str
) -> None:
    async with async_session_factory() as session:
        service = AnomalyDetectionService(session)
        started = time.perf_counter()
        result = await service.run(
            project_id=project_id, environment_id=environment_id
        )
        await session.commit()
    record(f"detection pass ({label}; rules={result.rules_evaluated})", started)

    # A second pass measures the dedup path rather than fresh inserts.
    async with async_session_factory() as session:
        service = AnomalyDetectionService(session)
        started = time.perf_counter()
        rerun = await service.run(project_id=project_id, environment_id=environment_id)
        await session.commit()
    record(
        f"detection re-run / dedup ({label}; opened={rerun.anomalies_opened}, "
        f"updated={rerun.anomalies_updated})",
        started,
    )


async def bench_correlation(
    project_id: uuid.UUID, environment_id: uuid.UUID, *, label: str
) -> None:
    async with async_session_factory() as session:
        engine_c = IncidentCorrelationEngine(session)
        started = time.perf_counter()
        clusters = await engine_c.correlate_scope(
            project_id=project_id, environment_id=environment_id
        )
    record(f"correlation clustering ({label}; clusters={len(clusters)})", started)

    async with async_session_factory() as session:
        started = time.perf_counter()
        result = await IncidentManager(session).process_scope(
            project_id=project_id, environment_id=environment_id
        )
        await session.commit()
    record(
        f"incident persistence ({label}; created={result.incidents_created}, "
        f"linked={result.anomalies_linked})",
        started,
    )


async def bench_reads(
    project_id: uuid.UUID, environment_id: uuid.UUID, *, label: str
) -> None:
    async with async_session_factory() as session:
        started = time.perf_counter()
        metrics = await reliability_metrics(
            session, project_id=project_id, environment_id=environment_id
        )
    record(
        f"reliability metrics ({label}; anomalies={metrics.anomalies_detected})",
        started,
    )

    async with async_session_factory() as session:
        started = time.perf_counter()
        dashboard = await incident_dashboard(
            session, project_id=project_id, environment_id=environment_id
        )
    record(
        f"dashboard aggregate ({label}; buckets={len(dashboard['anomalies_over_time'])})",
        started,
    )

    async with async_session_factory() as session:
        started = time.perf_counter()
        total = await session.scalar(
            select(func.count(Anomaly.id)).where(Anomaly.project_id == project_id)
        )
    record(f"indexed anomaly count ({label}; rows={total})", started)


async def run_scale(
    *, components: int, samples: int, anomalies: int, label: str
) -> None:
    print(f"\n--- scale {label}: {components} components, "
          f"{components * samples} metric samples, {anomalies} anomalies ---")
    project_id, environment_id, _ids = await _build_world(
        components=components, samples_per_component=samples, anomalies=anomalies
    )
    await bench_detection(project_id, environment_id, label=label)
    await bench_correlation(project_id, environment_id, label=label)
    await bench_reads(project_id, environment_id, label=label)


def main() -> int:
    asyncio.run(_init_schema())
    asyncio.run(run_scale(components=100, samples=20, anomalies=200, label="100/2000/200"))
    asyncio.run(run_scale(components=500, samples=20, anomalies=1000, label="500/10000/1000"))
    asyncio.run(engine.dispose())

    print("\n=== Results ===")
    print(f"{'operation':<58} {'ms':>9}")
    for label, ms in RESULTS:
        print(f"{label:<58} {ms:>9.1f}")

    print(
        "\nNote: informational benchmark on synthetic data — detection and "
        "correlation are bounded by configuration "
        "(ANOMALY_MAX_TELEMETRY_SAMPLES / CORRELATION_MAX_ANOMALIES), so these "
        "numbers describe the configured caps, not an unbounded scan."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
