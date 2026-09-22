"""Phase 9 — the control plane actually controls the runtime (§10, §26).

A native remediation is only real if the job it names obeys it. The rest of the
Phase 9 suite proves that a ``PAUSE_BACKGROUND_JOB`` action writes a control row
and that the control plane reads it back; these tests prove the other half, which
is the half that matters in production: **the sweeps and the worker consult that
row before doing their work.**

Three properties are pinned here, and each of them is a way the feature could
have been nominal instead of real:

* a pause for a project stops that project's work — detection, reaping, planning;
* a pause for one project leaves every *other* project untouched, because an
  action scoped to one component must not silently stop the platform;
* a pause past its own deadline holds nothing down, even before housekeeping has
  retired the row.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.anomaly import (
    Anomaly,
    AnomalyRule,
    AnomalySeverity,
    AnomalyType,
    RuleCondition,
)
from app.models.code import DebugAnalysisRun, DebugAnalysisStatus
from app.models.deployment import CodeRepository, RepositoryIndexStatus
from app.models.fix import PatchWorkspace, WorkspaceStatus
from app.models.observability import MetricRecord, MetricType
from app.models.project import Environment, SoftwareProject
from app.models.remediation import (
    RemediationAction,
    RemediationActionType,
    RemediationControlKind,
    RemediationControlState,
    RemediationExecution,
    RemediationExecutionMode,
    RemediationProposal,
)
from app.models.reproduction import ExperimentStatus, ReproductionExperiment
from app.models.system import SystemComponent
from app.services.anomaly_sweep import run_detection_sweep
from app.services.code_sweep import sweep_code_intelligence_once
from app.services.fix_sweep import sweep_fix_workspaces_once
from app.services.remediation_controls import (
    apply_control,
    paused_scope_ids,
)
from app.services.remediation_service import RemediationService
from app.services.remediation_sweep import sweep_remediations_once
from app.services.reproduction_orchestrator import ReproductionOrchestrator
from tests.phase9_helpers import METRIC_ERROR_RATE, build_incident, set_policy
from tests.phase8_helpers import emit_metric_series


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _factory(db_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


async def _scope(db_session: AsyncSession, *, env_name: str = "production"):
    suffix = uuid.uuid4().hex[:8]
    project = SoftwareProject(name=f"Gate {suffix}", slug=f"gate-{suffix}")
    db_session.add(project)
    await db_session.flush()
    #: The declared type has to agree with the name: a scope only counts as
    #: non-production when both signals say so (see
    #: ``remediation_policy.environment_is_non_production``), and several tests
    #: here rely on an autonomous scope.
    environment = Environment(
        project_id=project.id,
        name=env_name,
        environment_type=(
            "PRODUCTION" if env_name.strip().lower() == "production" else "STAGING"
        ),
    )
    component = SystemComponent(
        project_id=project.id, component_type="SERVICE", name="Checkout"
    )
    db_session.add_all([environment, component])
    await db_session.flush()
    return project, environment, component


async def _pause(
    db_session: AsyncSession,
    job: str,
    *,
    project_id=None,
    environment_id=None,
    expires_at=None,
):
    """Apply a real pause through the real control-plane writer."""
    return await apply_control(
        db_session,
        kind=RemediationControlKind.BACKGROUND_JOB,
        scope_key=job,
        state=RemediationControlState.PAUSED,
        project_id=project_id,
        environment_id=environment_id,
        applied_by="test",
        reason="a test paused this job",
        expires_at=expires_at,
    )


async def _seed_breaching_telemetry(
    db_session: AsyncSession, project, environment, component
):
    """Telemetry that a detection rule will fire on, so the sweep has real work."""
    db_session.add(
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
            min_samples=1,
            cooldown_seconds=0,
            persistence_cycles=1,
        )
    )
    db_session.add(
        MetricRecord(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            timestamp=utcnow(),
            metric_name="http.checkout.latency.p95",
            metric_type=MetricType.GAUGE,
            value=900.0,
        )
    )
    await db_session.flush()


# ---------------------------------------------------------------------------
# Reading the control plane as a set of scopes
# ---------------------------------------------------------------------------


async def test_paused_scope_ids_splits_global_from_per_project(db_session):
    _, _, other = await _scope(db_session)
    project_a, _, _ = await _scope(db_session)
    await _pause(db_session, "anomaly_sweep", project_id=project_a.id)
    await _pause(db_session, "anomaly_sweep", project_id=None)
    await _pause(db_session, "code_sweep", project_id=other.id)
    await db_session.flush()

    is_global, ids = await paused_scope_ids(db_session, "anomaly_sweep")
    assert is_global is True
    assert ids == {project_a.id}, "a different job's pause must not be included"

    is_global_code, code_ids = await paused_scope_ids(db_session, "code_sweep")
    assert is_global_code is False
    assert code_ids == {other.id}

    is_global_none, none_ids = await paused_scope_ids(db_session, "fix_sweep")
    assert is_global_none is False
    assert none_ids == set()


async def test_an_expired_pause_holds_nothing_down(db_session):
    """Expiry is honoured on read, before housekeeping has retired the row.

    A pause that outlives its own deadline because a sweeper was busy is exactly
    the failure mode that turns a temporary remediation into a permanent outage.
    """
    project, _, _ = await _scope(db_session)
    await _pause(
        db_session,
        "anomaly_sweep",
        project_id=project.id,
        expires_at=utcnow() - timedelta(seconds=1),
    )
    await db_session.flush()

    is_global, ids = await paused_scope_ids(db_session, "anomaly_sweep")
    assert is_global is False
    assert ids == set()


# ---------------------------------------------------------------------------
# Detection sweep (§10)
# ---------------------------------------------------------------------------


async def test_the_detection_sweep_skips_a_paused_project(db_session, db_engine):
    project, environment, component = await _scope(db_session)
    await _seed_breaching_telemetry(db_session, project, environment, component)
    await _pause(
        db_session,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )
    await db_session.commit()
    factory = _factory(db_engine)

    result = await run_detection_sweep(factory)

    assert result.suppressed >= 1
    assert result.anomalies_opened == 0
    async with factory() as fresh:
        count = await fresh.scalar(select(func.count()).select_from(Anomaly))
        assert count == 0, "a paused detector must not write anomalies"


async def test_the_detection_sweep_still_runs_unpaused_projects(db_session, db_engine):
    """The pause is scoped: one project's decision must not stop another's."""
    paused, paused_env, paused_component = await _scope(db_session)
    await _seed_breaching_telemetry(db_session, paused, paused_env, paused_component)
    running, running_env, running_component = await _scope(db_session)
    await _seed_breaching_telemetry(db_session, running, running_env, running_component)
    await _pause(
        db_session,
        "anomaly_sweep",
        project_id=paused.id,
        environment_id=paused_env.id,
    )
    await db_session.commit()
    factory = _factory(db_engine)

    await run_detection_sweep(factory)

    async with factory() as fresh:
        rows = list((await fresh.execute(select(Anomaly))).scalars().all())
    assert rows, "the unpaused project must still be detected"
    assert {row.project_id for row in rows} == {running.id}


async def test_an_unpaused_detection_sweep_writes_anomalies(db_session, db_engine):
    """The control for the previous case: without a pause, the same fixture fires."""
    project, environment, component = await _scope(db_session)
    await _seed_breaching_telemetry(db_session, project, environment, component)
    await db_session.commit()
    factory = _factory(db_engine)

    result = await run_detection_sweep(factory)

    assert result.anomalies_opened == 1
    assert result.suppressed == 0


# ---------------------------------------------------------------------------
# Remediation sweep (§10, §39)
# ---------------------------------------------------------------------------


async def _degraded_project(db_session: AsyncSession):
    """A project the planner will actually propose something for."""
    project, environment, component = await _scope(db_session, env_name="staging")
    from tests.phase6_helpers import build_causal_analysis

    now = utcnow()
    from app.models.ingestion import HealthCheckEvent, HealthStatus

    for index in range(6):
        db_session.add(
            HealthCheckEvent(
                project_id=project.id,
                environment_id=environment.id,
                component_id=component.id,
                timestamp=now - timedelta(seconds=60 * (6 - index)),
                status=(HealthStatus.UNHEALTHY if index >= 3 else HealthStatus.HEALTHY),
                latency_ms=300.0,
            )
        )
    await emit_metric_series(
        db_session,
        project,
        environment,
        component,
        metric_name=METRIC_ERROR_RATE,
        values=[0.2] * 6,
        end=now,
        step_seconds=60,
        unit="ratio",
    )
    incident = await build_incident(db_session, project, environment, component)
    await build_causal_analysis(db_session, incident, component)
    await db_session.flush()
    return project, environment, component


async def test_the_remediation_sweep_skips_a_paused_project(db_session, db_engine):
    project, environment, component = await _degraded_project(db_session)
    await _pause(db_session, "remediation_sweep", project_id=project.id)
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await sweep_remediations_once(factory, project_id=project.id)

    assert summary["paused_projects"] == 1
    assert summary["proposals_created"] == 0
    async with factory() as fresh:
        proposals = await fresh.scalar(
            select(func.count()).select_from(RemediationProposal)
        )
        assert proposals == 0, "a paused sweep must not plan anything"


async def test_the_remediation_sweep_plans_when_not_paused(db_session, db_engine):
    project, environment, component = await _degraded_project(db_session)
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await sweep_remediations_once(factory, project_id=project.id)

    assert summary["paused_projects"] == 0
    assert summary["proposals_created"] >= 1


async def test_a_paused_project_is_not_executed_by_the_sweep(db_session, db_engine):
    """The strongest form: an authorized autonomous action stays put if paused."""
    project, environment, component = await _scope(db_session, env_name="staging")
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.AUTONOMOUS,
        autonomous_max_risk="LOW",
        allowed_action_types=[RemediationActionType.PAUSE_BACKGROUND_JOB.value],
        cooldown_seconds=0,
        canary_enabled=False,
    )
    from tests.phase9_helpers import make_action

    incident = await build_incident(db_session, project, environment, component)
    action = await make_action(
        db_session,
        project,
        environment,
        component,
        incident_id=incident.id,
        source_id=incident.id,
    )
    service = RemediationService(db_session)
    await service.assess(action)
    await service.evaluate_policy(action)
    assert action.status.value == "AUTHORIZED"
    await _pause(db_session, "remediation_sweep", project_id=project.id)
    await db_session.commit()
    factory = _factory(db_engine)

    await sweep_remediations_once(factory, project_id=project.id, plan=False)

    async with factory() as fresh:
        re_read = await fresh.get(RemediationAction, action.id)
        assert re_read.status.value == "AUTHORIZED"
        assert re_read.started_at is None
        executions = await fresh.scalar(
            select(func.count()).select_from(RemediationExecution)
        )
        assert executions == 0


# ---------------------------------------------------------------------------
# Reapers (§10)
# ---------------------------------------------------------------------------


async def test_the_code_sweep_skips_a_globally_paused_scope(db_session, db_engine):
    project, _, _ = await _scope(db_session)
    db_session.add(
        DebugAnalysisRun(
            project_id=project.id,
            session_id=uuid.uuid4(),
            status=DebugAnalysisStatus.RUNNING,
            kind="INVESTIGATE",
            started_at=utcnow() - timedelta(hours=6),
        )
    )
    await _pause(db_session, "code_sweep", project_id=None)
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await sweep_code_intelligence_once(factory)

    assert summary.get("paused_skipped") is True
    async with factory() as fresh:
        run = (await fresh.execute(select(DebugAnalysisRun))).scalars().first()
        assert (
            run.status == DebugAnalysisStatus.RUNNING
        ), "a paused reaper must not close the runs it was told to leave alone"


async def test_the_code_sweep_reaps_unpaused_projects_only(db_session, db_engine):
    paused, _, _ = await _scope(db_session)
    reaped, _, _ = await _scope(db_session)
    for project in (paused, reaped):
        db_session.add(
            DebugAnalysisRun(
                project_id=project.id,
                session_id=uuid.uuid4(),
                status=DebugAnalysisStatus.RUNNING,
                kind="INVESTIGATE",
                started_at=utcnow() - timedelta(hours=6),
            )
        )
    await _pause(db_session, "code_sweep", project_id=paused.id)
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await sweep_code_intelligence_once(factory)

    assert any("runs_failed" == key for key in summary)
    assert summary["paused_skipped"] == 1
    async with factory() as fresh:
        by_project = {
            row.project_id: row.status
            for row in (await fresh.execute(select(DebugAnalysisRun))).scalars().all()
        }
    assert by_project[paused.id] == DebugAnalysisStatus.RUNNING
    assert by_project[reaped.id] == DebugAnalysisStatus.FAILED


async def test_the_code_sweep_resets_a_stuck_repository(db_session, db_engine):
    """The control for the pause tests: without one, the reaper acts."""
    project, _, _ = await _scope(db_session)
    repository = CodeRepository(
        project_id=project.id,
        provider="local",
        repository_url="/tmp/argus-gate-fixture",
        index_status=RepositoryIndexStatus.INDEXING,
        updated_at=utcnow() - timedelta(hours=2),
    )
    db_session.add(repository)
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await sweep_code_intelligence_once(factory)

    assert summary["repos_reset"] == [str(repository.id)]


async def test_the_fix_sweep_leaves_a_paused_projects_workspaces_alone(
    db_session, db_engine, tmp_path
):
    from app.models.fix import FixHypothesis, Patch

    paused, paused_env, paused_component = await _scope(db_session)
    reaped, reaped_env, reaped_component = await _scope(db_session)

    workspaces = {}
    for project, environment, component in (
        (paused, paused_env, paused_component),
        (reaped, reaped_env, reaped_component),
    ):
        incident = await build_incident(db_session, project, environment, component)
        hypothesis = FixHypothesis(
            project_id=project.id,
            incident_id=incident.id,
            title="h",
            description="d",
            proposed_change="c",
        )
        db_session.add(hypothesis)
        await db_session.flush()
        patch = Patch(
            project_id=project.id, fix_hypothesis_id=hypothesis.id, patch_content=""
        )
        db_session.add(patch)
        await db_session.flush()
        root = tmp_path / f"ws-{project.id}"
        root.mkdir()
        workspace = PatchWorkspace(
            project_id=project.id,
            patch_id=patch.id,
            branch_name=f"argus/fix/{project.id}",
            root_path=str(root),
            status=WorkspaceStatus.PATCH_APPLIED,
            created_at_workspace=utcnow() - timedelta(hours=3),
        )
        db_session.add(workspace)
        await db_session.flush()
        workspaces[project.id] = (workspace, root)

    await _pause(db_session, "fix_sweep", project_id=paused.id)
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await sweep_fix_workspaces_once(factory)

    assert summary["paused_skipped"] == 1
    async with factory() as fresh:
        paused_row = await fresh.get(PatchWorkspace, workspaces[paused.id][0].id)
        reaped_row = await fresh.get(PatchWorkspace, workspaces[reaped.id][0].id)
    assert paused_row.status == WorkspaceStatus.PATCH_APPLIED
    assert (
        workspaces[paused.id][1].exists()
    ), "a paused reaper must not delete the directory it was told to leave alone"
    assert reaped_row.status == WorkspaceStatus.DESTROYED
    assert not workspaces[reaped.id][1].exists()


async def test_the_reproduction_reaper_skips_a_paused_projects_experiment(
    db_session, db_engine
):
    paused, paused_env, paused_component = await _scope(db_session)
    reaped, reaped_env, reaped_component = await _scope(db_session)
    stale_at = utcnow() - timedelta(hours=2)
    rows = {}
    for project, environment, component in (
        (paused, paused_env, paused_component),
        (reaped, reaped_env, reaped_component),
    ):
        incident = await build_incident(db_session, project, environment, component)
        experiment = ReproductionExperiment(
            project_id=project.id,
            environment_id=environment.id,
            incident_id=incident.id,
            status=ExperimentStatus.RUNNING,
            timeout_at=stale_at,
            requested_by="test",
        )
        db_session.add(experiment)
        await db_session.flush()
        rows[project.id] = experiment.id

    await _pause(db_session, "reproduction_sweep", project_id=paused.id)
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await ReproductionOrchestrator(factory).sweep_stale(
        paused_project_ids={paused.id}
    )

    assert summary["paused_skipped"] >= 1
    assert str(rows[paused.id]) not in summary["timed_out"]
    assert str(rows[reaped.id]) in summary["timed_out"]
    async with factory() as fresh:
        assert (
            await fresh.get(ReproductionExperiment, rows[paused.id])
        ).status == ExperimentStatus.RUNNING
        assert (
            await fresh.get(ReproductionExperiment, rows[reaped.id])
        ).status == ExperimentStatus.TIMED_OUT
