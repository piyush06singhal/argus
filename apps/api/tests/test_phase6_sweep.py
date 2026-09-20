"""Phase 6 sweep tests (§55–§57): the reaper for abandoned code-intelligence work.

Indexing and AI analysis run inside API requests. When the process dies
mid-request the rows it wrote claim work is still happening — a session stuck
in ANALYZING, a run stuck in RUNNING, a repository stuck in INDEXING. The sweep
marks each with the honest terminal state, and these tests pin the behaviour
including the cases the sweep must leave alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.code import (
    DebugAnalysisRun,
    DebugAnalysisStatus,
    DebugSession,
    DebugSessionStatus,
)
from app.models.deployment import CodeRepository, RepositoryIndexStatus
from app.services.code_sweep import sweep_code_intelligence_once

from phase6_helpers import build_incident, build_project


def _now() -> datetime:
    """Fresh clock, per call.

    A module-level ``NOW`` would be frozen at collection time — minutes before
    these tests run in a full-suite pass — and a run "started at NOW" would
    then legitimately be older than the sweep's grace period. Every "just
    happened" timestamp in this file must therefore be computed when the test
    executes, not when the module was imported.
    """
    return datetime.now(timezone.utc)


def _factory(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


async def _abandoned_run(
    db_session: AsyncSession,
    *,
    started: datetime,
    status: DebugAnalysisStatus = DebugAnalysisStatus.RUNNING,
    touch_old: bool = False,
) -> tuple:
    """A debug session with one analysis run in the given state."""
    project, environment, component = await build_project(db_session)
    incident = await build_incident(db_session, project, environment, component)
    debug_session = DebugSession(
        project_id=project.id,
        incident_id=incident.id,
        status=DebugSessionStatus.ANALYZING,
        created_by="pytest",
    )
    db_session.add(debug_session)
    await db_session.flush()
    run = DebugAnalysisRun(
        project_id=project.id,
        session_id=debug_session.id,
        status=status,
        kind="incident_analysis",
        started_at=started,
    )
    db_session.add(run)
    if touch_old:
        debug_session.updated_at = _now() - timedelta(hours=1)
    await db_session.flush()
    return debug_session.id, run.id


async def _repo(
    db_session: AsyncSession,
    tmp_path,
    *,
    status: RepositoryIndexStatus,
    touch_old: bool = False,
) -> tuple:
    """A repository row in the given index status (optionally old)."""
    import uuid as _uuid

    project, _environment, _component = await build_project(db_session)
    repository = CodeRepository(
        project_id=project.id,
        provider="local",
        repository_url=str(tmp_path),
        local_path=str(tmp_path),
        default_branch="main",
        language="python",
        index_status=status,
    )
    db_session.add(repository)
    await db_session.flush()
    repo_id = repository.id
    if touch_old:
        repository.updated_at = _now() - timedelta(hours=1)
        await db_session.flush()
    assert repo_id is not None or _uuid  # keep the import meaningful if unused
    return repo_id


class TestAbandonedAnalysisRuns:
    async def test_a_run_stuck_running_past_its_budget_is_failed_by_the_sweep(
        self, db_session, db_engine
    ) -> None:
        _sid, run_id = await _abandoned_run(
            db_session, started=_now() - timedelta(hours=1)
        )
        await db_session.commit()

        summary = await sweep_code_intelligence_once(_factory(db_engine))
        db_session.expire_all()
        refreshed = await db_session.get(DebugAnalysisRun, run_id)

        assert refreshed.status is DebugAnalysisStatus.FAILED
        assert "abandoned" in (refreshed.error or "")
        assert refreshed.completed_at is not None
        assert str(run_id) in summary["runs_failed"]

    async def test_a_run_still_within_its_budget_is_left_alone(
        self, db_session, db_engine
    ) -> None:
        _sid, run_id = await _abandoned_run(db_session, started=_now())
        await db_session.commit()

        await sweep_code_intelligence_once(_factory(db_engine))
        db_session.expire_all()
        refreshed = await db_session.get(DebugAnalysisRun, run_id)

        assert refreshed.status is DebugAnalysisStatus.RUNNING

    async def test_completed_runs_are_never_touched(
        self, db_session, db_engine
    ) -> None:
        _sid, run_id = await _abandoned_run(
            db_session,
            started=_now() - timedelta(hours=1),
            status=DebugAnalysisStatus.COMPLETED,
        )
        await db_session.commit()

        await sweep_code_intelligence_once(_factory(db_engine))
        db_session.expire_all()
        refreshed = await db_session.get(DebugAnalysisRun, run_id)

        assert refreshed.status is DebugAnalysisStatus.COMPLETED

    async def test_the_session_of_an_abandoned_run_is_closed_too(
        self, db_session, db_engine
    ) -> None:
        session_id, _run_id = await _abandoned_run(
            db_session, started=_now() - timedelta(hours=1), touch_old=True
        )
        await db_session.commit()

        summary = await sweep_code_intelligence_once(_factory(db_engine))
        db_session.expire_all()
        refreshed = await db_session.get(DebugSession, session_id)

        assert refreshed.status is DebugSessionStatus.FAILED
        assert "abandoned" in (refreshed.summary or "")
        assert str(session_id) in summary["sessions_failed"]

    async def test_an_active_session_just_touched_is_left_alone(
        self, db_session, db_engine
    ) -> None:
        session_id, _run_id = await _abandoned_run(db_session, started=_now())
        await db_session.commit()

        await sweep_code_intelligence_once(_factory(db_engine))
        db_session.expire_all()
        refreshed = await db_session.get(DebugSession, session_id)

        assert refreshed.status is DebugSessionStatus.ANALYZING

    async def test_the_sweep_survives_its_own_failure(self, db_engine) -> None:
        class ExplodingFactory:
            def __call__(self, *args, **kwargs):
                raise RuntimeError("simulated sweep bug")

        summary = await sweep_code_intelligence_once(ExplodingFactory())  # type: ignore[arg-type]

        assert "simulated sweep bug" in summary["error"]


class TestStuckRepositories:
    async def test_a_repository_stuck_indexing_is_reset_to_failed(
        self, db_session, db_engine, tmp_path
    ) -> None:
        repo_id = await _repo(
            db_session,
            tmp_path,
            status=RepositoryIndexStatus.INDEXING,
            touch_old=True,
        )
        await db_session.commit()

        summary = await sweep_code_intelligence_once(_factory(db_engine))
        db_session.expire_all()
        refreshed = await db_session.get(CodeRepository, repo_id)

        assert refreshed.index_status is RepositoryIndexStatus.FAILED
        assert str(repo_id) in summary["repos_reset"]

    async def test_a_repository_actually_indexing_now_is_left_alone(
        self, db_session, db_engine, tmp_path
    ) -> None:
        repo_id = await _repo(
            db_session, tmp_path, status=RepositoryIndexStatus.INDEXING
        )
        await db_session.commit()

        await sweep_code_intelligence_once(_factory(db_engine))
        db_session.expire_all()
        refreshed = await db_session.get(CodeRepository, repo_id)

        assert refreshed.index_status is RepositoryIndexStatus.INDEXING

    async def test_indexed_repositories_are_never_touched(
        self, db_session, db_engine, tmp_path
    ) -> None:
        repo_id = await _repo(
            db_session,
            tmp_path,
            status=RepositoryIndexStatus.INDEXED,
            touch_old=True,
        )
        await db_session.commit()

        await sweep_code_intelligence_once(_factory(db_engine))
        db_session.expire_all()
        refreshed = await db_session.get(CodeRepository, repo_id)

        assert refreshed.index_status is RepositoryIndexStatus.INDEXED


class TestIdempotence:
    async def test_a_second_pass_finds_nothing_left_to_do(
        self, db_session, db_engine
    ) -> None:
        _sid, _run_id = await _abandoned_run(
            db_session, started=_now() - timedelta(hours=1), touch_old=True
        )
        await db_session.commit()

        first = await sweep_code_intelligence_once(_factory(db_engine))
        second = await sweep_code_intelligence_once(_factory(db_engine))

        assert first["runs_failed"] or first["sessions_failed"]
        assert second["runs_failed"] == []
        assert second["sessions_failed"] == []
        assert second["repos_reset"] == []
