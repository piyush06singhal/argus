"""Phase 3 — async detection: job processing, enqueue, scheduled sweep (§20)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.anomaly import (
    Anomaly,
    AnomalyRule,
    AnomalySeverity,
    AnomalyType,
    RuleCondition,
)
from app.models.observability import MetricRecord, MetricType
from app.models.project import Environment, SoftwareProject
from app.models.system import SystemComponent
from app.services.anomaly_sweep import run_detection_sweep
from app.services.queue import PermanentJobError
from app.services.worker_runner import process_anomaly_detect_job, process_event_job


def _now() -> datetime:
    """Current UTC time — the job uses the real clock, so tests must too.

    A hardcoded timestamp would fall outside the rule's 300s window and the
    detector would (correctly) find no telemetry.
    """
    return datetime.now(timezone.utc)


def _factory(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


async def _seed(db_session: AsyncSession, *, env_name: str = "production"):
    project = SoftwareProject(name="Worker Proj", slug=f"worker-{uuid.uuid4().hex[:8]}")
    db_session.add(project)
    await db_session.flush()
    env = Environment(
        project_id=project.id, name=env_name, environment_type="PRODUCTION"
    )
    component = SystemComponent(
        project_id=project.id, component_type="SERVICE", name="Checkout"
    )
    db_session.add_all([env, component])
    await db_session.flush()
    db_session.add(
        AnomalyRule(
            project_id=project.id,
            environment_id=env.id,
            component_id=component.id,
            name="latency p95",
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            condition=RuleCondition.THRESHOLD,
            metric_name="http.checkout.latency.p95",
            threshold=500.0,
            severity=AnomalySeverity.HIGH,
            window_seconds=300,
            min_samples=1,
            cooldown_seconds=0,
            persistence_cycles=1,
        )
    )
    db_session.add(
        MetricRecord(
            project_id=project.id,
            environment_id=env.id,
            component_id=component.id,
            timestamp=_now(),
            metric_name="http.checkout.latency.p95",
            metric_type=MetricType.GAUGE,
            value=900.0,
        )
    )
    await db_session.flush()
    return project.id, env.id, component.id


class TestAnomalyDetectJob:
    async def test_job_runs_detection(self, db_engine) -> None:
        factory = _factory(db_engine)
        async with factory() as session:
            project_id, env_id, _ = await _seed(session)
            await session.commit()

        summary = await process_anomaly_detect_job(
            factory,
            payload={"project_id": str(project_id), "environment_id": str(env_id)},
        )
        assert summary["anomalies_opened"] == 1

        async with factory() as session:
            count = await session.scalar(select(func.count()).select_from(Anomaly))
            assert count == 1

    async def test_job_is_idempotent(self, db_engine) -> None:
        factory = _factory(db_engine)
        async with factory() as session:
            project_id, env_id, _ = await _seed(session)
            await session.commit()

        payload = {"project_id": str(project_id), "environment_id": str(env_id)}
        await process_anomaly_detect_job(factory, payload=payload)
        await process_anomaly_detect_job(factory, payload=payload)

        async with factory() as session:
            count = await session.scalar(select(func.count()).select_from(Anomaly))
            assert count == 1

    async def test_job_for_missing_project_is_permanent(self, db_engine) -> None:
        factory = _factory(db_engine)
        ghost = uuid.uuid4()
        with pytest.raises(PermanentJobError, match="not found"):
            await process_anomaly_detect_job(
                factory, payload={"project_id": str(ghost)}
            )

    async def test_job_without_project_is_permanent(self, db_engine) -> None:
        factory = _factory(db_engine)
        with pytest.raises(PermanentJobError, match="project_id"):
            await process_anomaly_detect_job(factory, payload={})

    async def test_disabled_master_switch_skips(self, db_engine, monkeypatch) -> None:
        factory = _factory(db_engine)
        monkeypatch.setattr(
            "app.services.worker_runner.settings.ANOMALY_DETECTION_ENABLED", False
        )
        summary = await process_anomaly_detect_job(
            factory, payload={"project_id": str(uuid.uuid4())}
        )
        assert summary["skipped"] == "detection_disabled"

    async def test_dispatched_through_event_job(self, db_engine) -> None:
        factory = _factory(db_engine)
        async with factory() as session:
            project_id, env_id, _ = await _seed(session)
            await session.commit()

        await process_event_job(
            factory,
            kind="anomaly_detect",
            payload={"project_id": str(project_id), "environment_id": str(env_id)},
        )
        async with factory() as session:
            count = await session.scalar(select(func.count()).select_from(Anomaly))
            assert count == 1


class TestEnqueue:
    async def test_enqueue_pushes_ids_only(self, monkeypatch) -> None:
        from app.services.queue import enqueue_anomaly_detect

        pushed: list[dict] = []

        class FakeQueue:
            def __init__(self, *a, **k):
                pass

            async def push(self, job):
                pushed.append(job)

        monkeypatch.setattr("app.services.queue.IngestionQueue", FakeQueue)
        monkeypatch.setattr("app.services.queue.settings.ANOMALY_DETECTION_ASYNC", True)

        ok = await enqueue_anomaly_detect(project_id=uuid.uuid4())
        assert ok is True
        assert pushed[0]["kind"] == "anomaly_detect"
        assert "events" not in pushed[0]["payload"]

    async def test_enqueue_noop_when_disabled(self, monkeypatch) -> None:
        from app.services.queue import enqueue_anomaly_detect

        monkeypatch.setattr(
            "app.services.queue.settings.ANOMALY_DETECTION_ASYNC", False
        )
        assert await enqueue_anomaly_detect(project_id=uuid.uuid4()) is False

    async def test_enqueue_degrades_when_queue_unavailable(self, monkeypatch) -> None:
        from app.services.queue import QueueUnavailable, enqueue_anomaly_detect

        class BrokenQueue:
            def __init__(self, *a, **k):
                pass

            async def push(self, job):
                raise QueueUnavailable("down")

        monkeypatch.setattr("app.services.queue.IngestionQueue", BrokenQueue)
        monkeypatch.setattr("app.services.queue.settings.ANOMALY_DETECTION_ASYNC", True)
        assert await enqueue_anomaly_detect(project_id=uuid.uuid4()) is False


class TestSweep:
    async def test_sweep_is_per_environment(self, db_engine) -> None:
        """Environments must never be pooled: staging telemetry cannot fire a
        production-scoped rule (or vice versa)."""
        factory = _factory(db_engine)
        async with factory() as session:
            project_id, env_id, component_id = await _seed(session)
            staging = Environment(
                project_id=project_id, name="staging", environment_type="STAGING"
            )
            session.add(staging)
            await session.flush()
            # Same rule in staging, but no staging telemetry → must not fire.
            session.add(
                AnomalyRule(
                    project_id=project_id,
                    environment_id=staging.id,
                    component_id=component_id,
                    name="staging latency",
                    anomaly_type=AnomalyType.LATENCY_SPIKE,
                    condition=RuleCondition.THRESHOLD,
                    metric_name="http.checkout.latency.p95",
                    threshold=500.0,
                    severity=AnomalySeverity.HIGH,
                    window_seconds=300,
                    min_samples=1,
                    cooldown_seconds=0,
                    persistence_cycles=1,
                )
            )
            await session.commit()

        result = await run_detection_sweep(factory, now=_now())
        assert result.projects == 1
        assert result.scopes == 2  # production + staging
        assert result.anomalies_opened == 1

        async with factory() as session:
            rows = list((await session.execute(select(Anomaly))).scalars().all())
            assert len(rows) == 1
            assert rows[0].environment_id == env_id  # the one with telemetry

    async def test_sweep_skips_when_disabled(self, db_engine, monkeypatch) -> None:
        factory = _factory(db_engine)
        monkeypatch.setattr(
            "app.services.anomaly_sweep.settings.ANOMALY_DETECTION_ENABLED", False
        )
        result = await run_detection_sweep(factory, now=_now())
        assert result.skipped is True

    async def test_sweep_handles_no_projects(self, db_engine) -> None:
        factory = _factory(db_engine)
        result = await run_detection_sweep(factory, now=_now())
        assert result.projects == 0
        assert result.scopes == 0
