"""Concurrency proofs against real PostgreSQL (hardening W6 §4).

The hardening plan asked for the per-project mutex to be **proved**, not
asserted: *"run the stack with API_WORKERS=4, confirm sweeps are mutex-safe
(they are by design; prove it)"*. A unit test cannot answer that, for the reason
:mod:`app.services.project_lock` itself records:

    SQLite — which the unit suite runs on — has no row locks, and its SQLAlchemy
    compiler emits nothing for ``FOR UPDATE``.

So a concurrency test written against the suite's default database would pass
with the lock deleted. This module therefore runs only against PostgreSQL
(skipped loudly otherwise; the Integration workflow provides ``ARGUS_TEST_DB``),
and it asserts the two mechanisms the lock and the fingerprint registry actually
provide:

1. :func:`test_the_project_lock_blocks_a_second_writer` — the mutex serialises.
   While one transaction holds the project row, a second ``lock_project`` cannot
   proceed (a bounded wait times out), and it proceeds the instant the holder
   releases. This is the property the whole fix rests on.
2. :func:`test_concurrent_claims_share_one_registry_row` — the two detectors that
   meet on the same sample share one registry row. The loser of the insert race
   used to fail the whole request with an ``IntegrityError``; now it adopts the
   rival's row. The invariant asserted is *exactly one row*, with no unhandled
   exception.
3. :func:`test_concurrent_detection_passes_lose_nothing` — the shape the sweep
   actually runs (lock → detect → correlate → commit) under four concurrent
   sessions, on telemetry that deterministically fires a rule. Before the lock,
   PostgreSQL raised a genuine deadlock here and one pass lost its whole
   transaction. Every pass must commit, the fingerprint registry must hold
   exactly one row, and the anomaly must be recorded once — not four times.

The counterfactual (the unlocked race) is covered by
:func:`test_unlocked_passes_never_duplicate_the_registry`: it asserts the
invariant that must hold whatever happens, without asserting that a specific
error occurs — which would be a flaky test pretending to be a strong one.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings

NOW = datetime(2026, 9, 19, 14, 30, tzinfo=timezone.utc)
CONCURRENCY = 4


def _test_database_url() -> str:
    return os.getenv("ARGUS_TEST_DB") or get_settings().DATABASE_URL


def _require_postgres() -> str:
    """PostgreSQL or nothing — SQLite cannot express a row lock."""
    url = _test_database_url()
    if not url.startswith("postgresql"):
        pytest.skip(
            "row-level locking is only meaningful against PostgreSQL; set "
            "ARGUS_TEST_DB to a postgresql+asyncpg:// URL (the Integration "
            "workflow does) to run the concurrency proofs"
        )
    return url


@pytest.fixture
async def pg_engine():
    engine = create_async_engine(_require_postgres(), future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def pg_scope(pg_engine):
    """A throwaway project (and its environment/component) for one test.

    Created and dropped through the engine so the tests share no state with the
    SQLite suite and leave the database as they found it.
    """
    from app.models.anomaly import (
        AnomalyRule,
        AnomalySeverity,
        AnomalyType,
        RuleCondition,
    )
    from app.models.project import Environment, SoftwareProject
    from app.models.system import SystemComponent

    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with factory() as session:
        project = SoftwareProject(
            name="Concurrency Probe", slug=f"concurrency-{uuid.uuid4().hex[:10]}"
        )
        session.add(project)
        await session.flush()
        environment = Environment(
            project_id=project.id, name="production", environment_type="PRODUCTION"
        )
        component = SystemComponent(
            project_id=project.id, component_type="SERVICE", name="Checkout"
        )
        session.add_all([environment, component])
        await session.flush()
        session.add(
            AnomalyRule(
                project_id=project.id,
                environment_id=environment.id,
                component_id=component.id,
                name="latency p95",
                anomaly_type=AnomalyType.LATENCY_SPIKE,
                condition=RuleCondition.THRESHOLD,
                metric_name="http.checkout.latency.p95",
                threshold=500.0,
                severity=AnomalySeverity.HIGH,
                window_seconds=300,
                min_samples=2,
                cooldown_seconds=0,
                persistence_cycles=1,
            )
        )
        await session.commit()
        ids = {
            "project_id": project.id,
            "environment_id": environment.id,
            "component_id": component.id,
        }

    try:
        yield factory, ids
    finally:
        # Teardown goes through the ORM, like the platform's own delete path: the
        # cascade that removes a project's telemetry, anomalies and incidents is
        # declared on the relationships, not as a database-level ON DELETE, so a
        # bare ``DELETE FROM projects`` fails its foreign key — which is exactly
        # what a first version of this fixture did.
        from app.models.project import SoftwareProject as _Project

        async with factory() as session:
            project = await session.get(_Project, ids["project_id"])
            if project is not None:
                await session.delete(project)
                await session.commit()


async def _fire_once(factory, ids, *, locked: bool) -> str:
    """One detection pass, exactly as the sweep runs it.

    Returns a short outcome token rather than raising, so the caller can assert
    on *what* a failure was (the tests below care about the difference between
    "lost the race in a recognised way" and "crashed").
    """
    from app.services.anomaly_detection import AnomalyDetectionService
    from app.services.incident_manager import IncidentManager
    from app.services.project_lock import lock_project

    async with factory() as session:
        try:
            if locked:
                await lock_project(session, project_id=ids["project_id"])
            await AnomalyDetectionService(session, now=NOW).run(
                project_id=ids["project_id"],
                environment_id=ids["environment_id"],
            )
            await IncidentManager(session, now=NOW).process_scope(
                project_id=ids["project_id"],
                environment_id=ids["environment_id"],
            )
            await session.commit()
            return "committed"
        except Exception as exc:  # noqa: BLE001 — the test classifies it
            await session.rollback()
            return f"{type(exc).__name__}: {str(exc)[:120]}"


async def _registry_rows(factory, project_id) -> int:
    from app.models.anomaly import AnomalyFingerprint

    async with factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(AnomalyFingerprint)
                .where(AnomalyFingerprint.project_id == project_id)
            )
            or 0
        )


class TestProjectLock:
    async def test_the_project_lock_blocks_a_second_writer(
        self, pg_engine, pg_scope
    ) -> None:
        """The mutex really serialises — proved by blocking, not by inspection.

        The plan's claim is that concurrent sweeps are safe *because* one
        transaction holds the project row. This test measures that directly: a
        second acquisition must not complete while the first is open.
        """
        from app.services.project_lock import lock_project

        factory = async_sessionmaker(pg_engine, expire_on_commit=False)
        project_id = pg_scope[1]["project_id"]

        async with factory() as holder:
            await lock_project(holder, project_id=project_id)

            async with factory() as rival:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        lock_project(rival, project_id=project_id), timeout=1.0
                    )
                # The cancelled statement leaves the rival's transaction open but
                # idle; reset it so the connection is reusable.
                await rival.rollback()

            # The holder releases; the lock is now free.
            await holder.rollback()

            async with factory() as after:
                await asyncio.wait_for(
                    lock_project(after, project_id=project_id), timeout=5.0
                )
                await after.rollback()

    async def test_concurrent_claims_share_one_registry_row(
        self, pg_engine, pg_scope
    ) -> None:
        """Meeting on the same sample must not crash one of the two detectors."""
        from app.models.anomaly import AnomalyType
        from app.services.anomaly_detection import AnomalyDetectionService

        factory = async_sessionmaker(pg_engine, expire_on_commit=False)
        project_id = pg_scope[1]["project_id"]
        fingerprint = "f" * 64

        async def claim() -> str:
            async with factory() as session:
                service = AnomalyDetectionService(session, now=NOW)
                try:
                    row = await service._claim_registry(  # noqa: SLF001 — the mechanism under test
                        project_id=project_id,
                        environment_id=pg_scope[1]["environment_id"],
                        component_id=None,
                        fingerprint=fingerprint,
                        anomaly_type=AnomalyType.LATENCY_SPIKE,
                    )
                except Exception as exc:  # noqa: BLE001
                    await session.rollback()
                    return f"{type(exc).__name__}"
                await session.commit()
                # ``None`` is the documented, honest outcome when the rival's row
                # is not yet visible: the caller records nothing rather than
                # guessing. A crash is not an acceptable outcome.
                return "row" if row is not None else "deferred"

        outcomes = await asyncio.gather(*(claim() for _ in range(CONCURRENCY)))

        assert not [
            o
            for o in outcomes
            if o.startswith(("IntegrityError", "OperationalError", "DBAPIError"))
        ], f"a concurrent claim crashed instead of sharing: {outcomes}"
        assert outcomes.count("row") >= 1, outcomes
        assert await _registry_rows(factory, project_id) == 1


class TestLockIsLoadBearing:
    async def test_the_lock_emits_for_update_on_postgres(self, pg_engine, pg_scope):
        """The lock must not be able to quietly become a no-op.

        ``FOR UPDATE`` is emitted by PostgreSQL and by *nothing else* — SQLite's
        compiler renders it as an empty string — so deleting
        ``with_for_update()`` would leave the entire SQLite suite green while
        production lost its serialisation. This asserts the emitted SQL on the
        production dialect, which is the only place the difference is visible.
        """
        from sqlalchemy import event

        from app.services.project_lock import lock_project

        captured: list[str] = []

        @event.listens_for(pg_engine.sync_engine, "before_cursor_execute")
        def _record(conn, cursor, statement, parameters, context, executemany):
            captured.append(statement)

        factory = async_sessionmaker(pg_engine, expire_on_commit=False)
        async with factory() as session:
            await lock_project(session, project_id=pg_scope[1]["project_id"])
            await session.rollback()

        event.remove(pg_engine.sync_engine, "before_cursor_execute", _record)
        locking = [s for s in captured if "FROM projects" in s]
        assert locking, f"the lock issued no statement against projects: {captured}"
        assert any("FOR UPDATE" in s.upper() for s in locking), (
            "the project lock issued a plain SELECT — it is not serialising "
            f"anything on PostgreSQL: {locking}"
        )


class TestConcurrentSweeps:
    async def test_concurrent_detection_passes_lose_nothing(
        self, pg_engine, pg_scope
    ) -> None:
        """Four simultaneous detect+correlate passes, the way workers run them.

        This is the scenario that produced a real PostgreSQL deadlock (a
        fingerprint ``UPDATE`` and an anomaly ``UPDATE`` each holding a row the
        other needed) and lost one pass's entire transaction. With the project
        lock, every pass must commit and the sample must be recorded **once**.
        """
        from app.models.anomaly import Anomaly, AnomalyFingerprint
        from app.models.observability import MetricRecord, MetricType

        factory = async_sessionmaker(pg_engine, expire_on_commit=False)
        project_id = pg_scope[1]["project_id"]
        environment_id = pg_scope[1]["environment_id"]
        component_id = pg_scope[1]["component_id"]

        async with factory() as session:
            session.add(
                MetricRecord(
                    project_id=project_id,
                    environment_id=environment_id,
                    component_id=component_id,
                    timestamp=NOW - timedelta(seconds=1),
                    metric_name="http.checkout.latency.p95",
                    metric_type=MetricType.GAUGE,
                    value=900.0,
                )
            )
            await session.commit()

        outcomes = await asyncio.gather(
            *(_fire_once(factory, pg_scope[1], locked=True) for _ in range(CONCURRENCY))
        )

        assert (
            outcomes == ["committed"] * CONCURRENCY
        ), f"a locked pass did not commit: {outcomes}"

        async with factory() as session:
            fingerprints = int(
                await session.scalar(
                    select(func.count())
                    .select_from(AnomalyFingerprint)
                    .where(AnomalyFingerprint.project_id == project_id)
                )
                or 0
            )
            anomalies = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Anomaly)
                    .where(Anomaly.project_id == project_id)
                )
                or 0
            )

        assert fingerprints == 1, (
            "the registry must collapse repeated detections into one row, "
            f"found {fingerprints}"
        )
        assert anomalies == 1, (
            "four simultaneous passes must record the sample once, "
            f"found {anomalies} anomalies"
        )

    async def test_unlocked_passes_never_duplicate_the_registry(
        self, pg_engine, pg_scope
    ) -> None:
        """The invariant that must hold even when the lock is not taken.

        This test deliberately runs the passes **without** the mutex. It does not
        assert that a conflict occurs — that would be a flaky test dressed as a
        strong one, and the atomic claim is designed to absorb the race anyway.
        What it does assert is the part that must never be violated: the registry
        stays deduplicated, and any pass that fails does so in a recognised
        database-concurrency way (deadlock or serialisation failure), never with
        a silent partial write.
        """
        from app.models.anomaly import AnomalyFingerprint
        from app.models.observability import MetricRecord, MetricType

        factory = async_sessionmaker(pg_engine, expire_on_commit=False)
        project_id = pg_scope[1]["project_id"]
        environment_id = pg_scope[1]["environment_id"]
        component_id = pg_scope[1]["component_id"]

        async with factory() as session:
            session.add(
                MetricRecord(
                    project_id=project_id,
                    environment_id=environment_id,
                    component_id=component_id,
                    timestamp=NOW - timedelta(seconds=1),
                    metric_name="http.checkout.latency.p95",
                    metric_type=MetricType.GAUGE,
                    value=900.0,
                )
            )
            await session.commit()

        outcomes = await asyncio.gather(
            *(
                _fire_once(factory, pg_scope[1], locked=False)
                for _ in range(CONCURRENCY)
            )
        )

        recognised = ("DeadlockDetected", "SerializationError", "IntegrityError")
        unexpected = [
            o for o in outcomes if o != "committed" and not o.startswith(recognised)
        ]
        assert not unexpected, f"unrecognised concurrency failure: {unexpected}"

        async with factory() as session:
            fingerprints = int(
                await session.scalar(
                    select(func.count())
                    .select_from(AnomalyFingerprint)
                    .where(AnomalyFingerprint.project_id == project_id)
                )
                or 0
            )
        assert fingerprints <= 1, (
            "the unique constraint on (project_id, fingerprint) must hold even "
            f"under the unlocked race, found {fingerprints} rows"
        )
