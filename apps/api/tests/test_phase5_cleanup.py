"""Phase 5 cleanup, cancellation and worker tests (§36–§40, §53–§55).

§55 is the requirement these tests exist for: after every experiment the sandbox
is destroyed, and when cleanup fails that failure is *recorded and retried*
rather than leaked. The interesting cases are therefore the ones where something
goes wrong — a cancelled run, an experiment whose worker died, a sandbox whose
owner is gone — because the happy path is already covered by the scenario tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.reproduction import (
    ExperimentStatus,
    ReproductionRun,
    ReproductionResult,
    ReproductionSandbox,
    RunStatus,
    SandboxNetworkPolicy,
    SandboxStatus,
)
from app.services.reproduction_orchestrator import (
    OrchestrationError,
    ReproductionOrchestrator,
    classify_failure,
)
from app.services.reproduction_sandbox import (
    LocalProcessBackend,
    SandboxError,
    SandboxManager,
    SandboxSpec,
    sandbox_root_base,
)
from app.services.reproduction_sweep import sweep_reproductions_once

NOW = datetime.now(timezone.utc)


def _factory(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


class TestCancellation:
    async def test_cancelling_a_planned_experiment_stops_it_and_leaves_no_sandbox(
        self, db_session: AsyncSession, db_engine
    ) -> None:
        from test_phase5_demo import _prepare

        orchestrator, experiment, _candidate, _components, _incident = await _prepare(
            db_session, db_engine, repetitions=3
        )
        before = {item.name for item in sandbox_root_base().glob("argus-repro-*")}
        # Cancelled before the first repetition: the run loop checks the flag
        # before provisioning, so nothing is ever started.
        await orchestrator.request_cancel(experiment.id, reason="pytest cancellation")
        finished = await orchestrator.run_experiment(experiment.id)
        await db_session.commit()

        assert finished.status is ExperimentStatus.CANCELLED
        assert finished.result is ReproductionResult.NOT_RUN
        assert finished.cancel_requested_at is not None
        sandboxes = list(
            (
                await db_session.execute(
                    select(ReproductionSandbox).where(
                        ReproductionSandbox.experiment_id == experiment.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert sandboxes == [], "a cancelled experiment must not leave a sandbox row"
        assert {
            item.name for item in sandbox_root_base().glob("argus-repro-*")
        } == before

    async def test_cancelling_a_terminal_experiment_is_a_no_op(
        self, db_session: AsyncSession, db_engine
    ) -> None:
        from test_phase5_demo import _prepare

        orchestrator, experiment, _candidate, _components, _incident = await _prepare(
            db_session, db_engine
        )
        finished = await orchestrator.run_experiment(experiment.id)
        await db_session.commit()
        assert finished.status is ExperimentStatus.COMPLETED
        # Cancellation must not rewrite history: an experiment that ran stays ran.
        again = await orchestrator.request_cancel(experiment.id, reason="too late")
        assert again.status is ExperimentStatus.COMPLETED
        assert again.cancel_requested_at is None

    async def test_cancelling_an_unknown_experiment_is_an_error(
        self, db_engine
    ) -> None:
        orchestrator = ReproductionOrchestrator(_factory(db_engine))
        with pytest.raises(OrchestrationError):
            await orchestrator.request_cancel(uuid.uuid4())


class TestTimeoutAndReaper:
    async def test_a_stale_experiment_is_timed_out_and_its_sandbox_destroyed(
        self, db_session: AsyncSession, db_engine
    ) -> None:
        from app.models.reproduction import ReproductionExperiment

        from test_phase5_demo import _prepare

        orchestrator, experiment, _candidate, _components, _incident = await _prepare(
            db_session, db_engine
        )
        # The orchestrator writes through its own session, so the row is re-read
        # here: mutating a detached instance would silently write nothing.
        experiment = await db_session.get(ReproductionExperiment, experiment.id)
        assert experiment is not None
        # Simulate the path this test exists for: a worker took a sandbox as far
        # as READY and then died, leaving a live sandbox and a running experiment.
        manager = SandboxManager(LocalProcessBackend())
        handle = await manager.create(
            SandboxSpec(
                template="demo_commerce",
                services=["datastore", "inventory"],
                timeout_seconds=60,
            ),
            experiment_id=experiment.id,
            run_index=1,
        )
        await manager.start(handle)
        row = ReproductionSandbox(
            experiment_id=experiment.id,
            project_id=experiment.project_id,
            sandbox_key=handle.sandbox_key,
            backend=handle.backend,
            status=SandboxStatus.READY,
            network_policy=handle.network_policy,
            root_path=str(handle.root_path),
            resource_limits=handle.metadata.get("limits") or {},
            metadata_=handle.metadata,
        )
        db_session.add(row)
        experiment.status = ExperimentStatus.RUNNING
        experiment.timeout_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        await db_session.commit()

        summary = await sweep_reproductions_once(_factory(db_engine))

        assert str(experiment.id) in summary["timed_out"]
        assert handle.sandbox_key in summary["sandboxes_cleaned"]
        assert summary["cleanup_failures"] == []
        # The reaper writes through its own session, so read the result from a
        # fresh one: an identity-mapped object would just replay what it knew.
        async with _factory(db_engine)() as fresh:
            assert (
                await fresh.scalar(
                    select(ReproductionExperiment.status).where(
                        ReproductionExperiment.id == experiment.id
                    )
                )
                is ExperimentStatus.TIMED_OUT
            )
            sandbox_row = (
                await fresh.execute(
                    select(ReproductionSandbox).where(ReproductionSandbox.id == row.id)
                )
            ).scalar_one()
            assert sandbox_row.status is SandboxStatus.DESTROYED
            assert sandbox_row.destroyed_at is not None
        # §55: the working tree is gone, not merely marked as gone.
        assert not Path(handle.root_path).exists()

    async def test_a_live_experiment_is_left_alone_by_the_reaper(
        self, db_session: AsyncSession, db_engine
    ) -> None:
        from app.models.reproduction import ReproductionExperiment

        from test_phase5_demo import _prepare

        _orchestrator, created, _candidate, _components, _incident = await _prepare(
            db_session, db_engine
        )
        experiment = await db_session.get(ReproductionExperiment, created.id)
        assert experiment is not None
        experiment.status = ExperimentStatus.REPLAYING
        experiment.timeout_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        row = ReproductionSandbox(
            experiment_id=experiment.id,
            project_id=experiment.project_id,
            sandbox_key="argus-repro-live",
            backend=LocalProcessBackend.kind,
            status=SandboxStatus.READY,
            network_policy=SandboxNetworkPolicy.ISOLATED,
            root_path=str(sandbox_root_base() / "argus-repro-live"),
            resource_limits={},
        )
        db_session.add(row)
        await db_session.commit()

        summary = await sweep_reproductions_once(_factory(db_engine))
        assert str(experiment.id) not in summary["timed_out"]
        assert not summary["sandboxes_cleaned"]
        await db_session.refresh(experiment)
        assert experiment.status is ExperimentStatus.REPLAYING

    async def test_the_reaper_survives_its_own_failure(
        self, db_engine, monkeypatch
    ) -> None:
        def _explode(*_args, **_kwargs):
            raise RuntimeError("simulated reaper bug")

        monkeypatch.setattr(
            ReproductionOrchestrator, "sweep_stale", _explode, raising=True
        )
        summary = await sweep_reproductions_once(_factory(db_engine))
        # A reaper that crashes silently is indistinguishable from a leak, so the
        # failure is returned as data.
        assert "error" in summary
        assert "simulated reaper bug" in summary["error"]


class TestFailureClassification:
    def test_every_failure_lands_in_the_documented_taxonomy(self) -> None:
        from app.models.reproduction import FailureClass

        cases = [
            (
                SandboxError("service 'inventory' was not ready within 60s"),
                FailureClass.ENVIRONMENT_ERROR,
            ),
            (
                SandboxError("docker run failed: daemon is unavailable"),
                FailureClass.ENVIRONMENT_ERROR,
            ),
            (
                SandboxError("something else in the sandbox broke"),
                FailureClass.SANDBOX_ERROR,
            ),
            (MemoryError(), FailureClass.RESOURCE_LIMIT),
            (OSError("too many open files"), FailureClass.RESOURCE_LIMIT),
            (ValueError("unexpected"), FailureClass.UNKNOWN),
        ]
        for error, expected in cases:
            assert classify_failure(error) is expected, error

    def test_a_timeout_is_classified_as_a_timeout(self) -> None:
        import asyncio

        from app.models.reproduction import FailureClass

        assert classify_failure(asyncio.TimeoutError()) is FailureClass.TIMEOUT


class TestWorkerJobs:
    def test_the_reproduction_job_kind_is_wired_into_the_worker(self) -> None:
        import inspect

        from app.services import worker_runner
        from app.services.queue import enqueue_reproduction_run

        # The job kind the queue writes is the kind the worker dispatches; a
        # mismatch would enqueue work nothing ever runs.
        assert "reproduction_run" in inspect.getsource(worker_runner.process_event_job)
        assert callable(enqueue_reproduction_run)
        source = inspect.getsource(worker_runner.process_reproduction_run_job)
        # Idempotent by construction: the orchestrator returns early for a
        # terminal experiment, and the job refuses a payload without an id.
        assert "is_experiment_terminal" in source or "run_experiment" in source
        assert "PermanentJobError" in source

    async def test_running_a_terminal_experiment_again_changes_nothing(
        self, db_session: AsyncSession, db_engine
    ) -> None:
        from test_phase5_demo import _prepare

        orchestrator, experiment, _candidate, _components, _incident = await _prepare(
            db_session, db_engine
        )
        first = await orchestrator.run_experiment(experiment.id)
        await db_session.commit()
        runs_before = list(
            (
                await db_session.execute(
                    select(ReproductionRun).where(
                        ReproductionRun.experiment_id == experiment.id
                    )
                )
            )
            .scalars()
            .all()
        )
        second = await orchestrator.run_experiment(experiment.id)
        await db_session.commit()
        runs_after = list(
            (
                await db_session.execute(
                    select(ReproductionRun).where(
                        ReproductionRun.experiment_id == experiment.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert second.status is first.status
        assert len(runs_after) == len(
            runs_before
        ), "a retried job must not re-run a run"
        assert all(run.status is RunStatus.COMPLETED for run in runs_after)
