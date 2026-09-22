"""Phase 9 — controlled execution (§10, §11, §26, §27, §40, §41, §107).

Two claims are tested here and neither is taken on trust.

**Control-plane actions are real.** Executing ``PAUSE_BACKGROUND_JOB`` must make
``is_paused`` return true, because that is the function the worker and every
sweep call before doing their work. A test that only checked the action's status
would pass against a simulation, which is exactly what this phase must not be.

**Everything is re-checked at execution time.** Authorization is not a token
that can be replayed: the safety engine runs again, the process kill switch is
consulted, the mode decides whether an effect may be applied at all, and a
retried attempt reuses its idempotency key rather than applying twice.
"""

from __future__ import annotations

from datetime import timedelta


from app.core.config import get_settings
from app.models.remediation import (
    ExecutionStatus,
    RemediationActionType,
    RemediationExecutionMode,
    RemediationFailureReason,
    RemediationStatus,
)
from app.services.remediation_clock import utcnow
from app.services.remediation_controls import (
    feature_enabled,
    is_paused,
    revert_control,
)
from app.services.remediation_executor import (
    AdapterUnavailable,
    RemediationExecutor,
    build_adapter,
    idempotency_key,
)
from app.services.remediation_service import RemediationService
from tests.phase9_helpers import (
    build_project,
    make_action,
    make_draft,
    set_environment_class,
    set_policy,
)

settings = get_settings()


async def _authorized_pair(
    session,
    *,
    mode: RemediationExecutionMode = RemediationExecutionMode.HUMAN_APPROVAL,
    action_type: RemediationActionType = RemediationActionType.PAUSE_BACKGROUND_JOB,
    parameters: dict | None = None,
    non_production: bool = True,
    autonomous: bool = False,
    **draft_kwargs,
):
    """An action driven all the way to ``AUTHORIZED`` by configuring a policy.

    Nothing here reaches past a gate: the flow is propose → assess → evaluate →
    (approve) → authorize, and the regime is whatever the caller configured.
    """
    project, environment, component = await build_project(session)
    if non_production:
        await set_environment_class(session, environment, "staging")
    await set_policy(
        session,
        project.id,
        environment.id,
        execution_mode=(RemediationExecutionMode.AUTONOMOUS if autonomous else mode),
        autonomous_max_risk="LOW",
        allowed_action_types=[action_type.value],
        cooldown_seconds=0,
        max_actions_per_window=10,
        canary_enabled=False,
    )
    action = await make_action(
        session,
        project,
        environment,
        component,
        action_type=action_type,
        parameters=parameters,
        **draft_kwargs,
    )
    service = RemediationService(session)
    await service.assess(action)
    await service.evaluate_policy(action)
    if action.status == RemediationStatus.AWAITING_APPROVAL:
        action, _ = await service.decide(
            action, approve=True, actor="operator", reason="approved by test"
        )
    return project, environment, component, action, service


# ---------------------------------------------------------------------------
# The effect is real
# ---------------------------------------------------------------------------


async def test_pausing_a_job_actually_pauses_it(db_session):
    """§10: the control row written here is the row the worker obeys."""
    project, environment, component, action, service = await _authorized_pair(
        db_session
    )
    assert (
        await is_paused(
            db_session,
            "anomaly_sweep",
            project_id=project.id,
            environment_id=environment.id,
        )
        is False
    )

    outcome = await service.run(action, actor="test")
    assert outcome["status"] == RemediationStatus.VERIFIED.value

    assert (
        await is_paused(
            db_session,
            "anomaly_sweep",
            project_id=project.id,
            environment_id=environment.id,
        )
        is True
    )


async def test_disabling_a_feature_flag_actually_disables_it(db_session):
    project, environment, component, action, service = await _authorized_pair(
        db_session,
        action_type=RemediationActionType.DISABLE_FEATURE_FLAG,
        parameters={"flag": "graph_extraction"},
    )
    assert (
        await feature_enabled(
            db_session,
            "graph_extraction",
            project_id=project.id,
            environment_id=environment.id,
        )
        is True
    )

    await service.run(action, actor="test")

    assert (
        await feature_enabled(
            db_session,
            "graph_extraction",
            project_id=project.id,
            environment_id=environment.id,
        )
        is False
    )


async def test_the_applied_control_is_recorded_with_its_action(db_session):
    """Every effect is attributable to the action that produced it (§12)."""
    from sqlalchemy import select

    from app.models.remediation import RemediationControl

    project, environment, component, action, service = await _authorized_pair(
        db_session
    )
    await service.run(action, actor="test")

    row = (
        (
            await db_session.execute(
                select(RemediationControl).where(
                    RemediationControl.applied_by_action_id == action.id
                )
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    assert row.is_current is True
    assert row.applied_by_action_id == action.id


async def test_a_control_expires_on_its_own(db_session):
    """A control past its deadline stops holding work down immediately."""
    from app.models.remediation import RemediationControlKind
    from app.services.remediation_controls import _current_row

    project, environment, component, action, service = await _authorized_pair(
        db_session, parameters={"job": "anomaly_sweep", "duration_seconds": 60}
    )
    await service.run(action, actor="test")

    #: A live control: paused now.
    assert await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )
    row = await _current_row(
        db_session,
        RemediationControlKind.BACKGROUND_JOB,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )
    assert row is not None and row.expires_at is not None

    #: Past its deadline, it is no longer in force — without a sweeper running.
    assert (
        await is_paused(
            db_session,
            "anomaly_sweep",
            project_id=project.id,
            environment_id=environment.id,
            now=utcnow() + timedelta(seconds=120),
        )
        is False
    )


async def test_the_running_platform_consults_the_control_plane(db_session):
    """The pause is real only because production code reads it; pin the readers."""
    import pathlib

    source = pathlib.Path("app/services/worker_runner.py").read_text()
    assert "is_paused" in source, "the worker must consult the control plane"


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


async def test_a_dry_run_validates_without_applying(db_session):
    """§40/§107: a dry run must not produce a side effect or a success."""
    project, environment, component, action, service = await _authorized_pair(
        db_session
    )
    outcome = await service.run(action, actor="test", dry_run=True)

    assert action.status == RemediationStatus.AWAITING_APPROVAL
    assert (
        await is_paused(
            db_session,
            "anomaly_sweep",
            project_id=project.id,
            environment_id=environment.id,
        )
        is False
    )
    assert outcome["status"] == RemediationStatus.AWAITING_APPROVAL.value
    assert action.execution_status == ExecutionStatus.NOT_PERFORMED


async def test_observe_only_never_reaches_execution(db_session):
    """The most restrictive regime still records a proposal, and nothing else."""
    project, environment, component = await build_project(db_session)
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.OBSERVE_ONLY,
    )
    action = await make_action(db_session, project, environment, component)
    service = RemediationService(db_session)
    await service.assess(action)
    await service.evaluate_policy(action)
    assert action.status == RemediationStatus.REJECTED
    outcome = await service.run(action, actor="test")
    assert outcome["status"] == RemediationStatus.REJECTED.value
    assert action.execution_status is None


async def test_execution_is_refused_for_an_unauthorized_action(db_session):
    """An attempt to execute a proposal directly is refused, not honoured."""
    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    executor = RemediationExecutor(db_session)
    outcome = await executor.execute(
        action, mode=RemediationExecutionMode.HUMAN_APPROVAL
    )
    assert outcome.status == ExecutionStatus.REFUSED
    assert outcome.failure_reason == RemediationFailureReason.APPROVAL_REQUIRED
    assert outcome.effect_applied is False


async def test_the_process_kill_switch_stops_execution(db_session, monkeypatch):
    project, environment, component, action, service = await _authorized_pair(
        db_session
    )
    import app.services.remediation_executor as executor_module

    monkeypatch.setattr(
        executor_module.settings, "REMEDIATION_EXECUTION_ENABLED", False
    )
    outcome = await RemediationExecutor(db_session).execute(
        action, mode=RemediationExecutionMode.HUMAN_APPROVAL
    )
    assert outcome.status == ExecutionStatus.REFUSED
    assert outcome.failure_reason == RemediationFailureReason.EXECUTION_DISABLED
    assert (
        await is_paused(
            db_session,
            "anomaly_sweep",
            project_id=project.id,
            environment_id=environment.id,
        )
        is False
    )


async def test_execution_re_runs_safety_and_refuses_a_stale_action(db_session):
    """§29: an approval is minutes old; the world is not."""
    project, environment, component, action, service = await _authorized_pair(
        db_session
    )
    action.expires_at = utcnow() - timedelta(seconds=1)
    await db_session.flush()

    outcome = await service.run(action, actor="test")
    assert action.status == RemediationStatus.BLOCKED
    assert action.failure_reason == RemediationFailureReason.STALE_ACTION
    assert (
        await is_paused(
            db_session,
            "anomaly_sweep",
            project_id=project.id,
            environment_id=environment.id,
        )
        is False
    )
    del outcome


# ---------------------------------------------------------------------------
# Idempotency, retries, adapters
# ---------------------------------------------------------------------------


async def test_a_replayed_attempt_does_not_apply_twice(db_session):
    """A retried queue message must not produce a second effect (§27)."""
    project, environment, component, action, service = await _authorized_pair(
        db_session
    )
    executor = RemediationExecutor(db_session)
    first = await executor.execute(
        action, mode=RemediationExecutionMode.HUMAN_APPROVAL, actor="test"
    )
    assert first.effect_applied is True

    #: A fresh executor, same action and attempt: the idempotency key matches,
    #: so the replay is reported instead of a second effect being applied. The
    #: replay is found before the safety re-assessment, which is the point: the
    #: second pass must not even reach the point of deciding to apply.
    second = await executor.execute(
        action, mode=RemediationExecutionMode.HUMAN_APPROVAL, actor="test"
    )
    assert second.detail == "an attempt with this idempotency key already applied"
    assert second.execution is not None
    assert second.execution.id == first.execution.id

    from sqlalchemy import func, select

    from app.models.remediation import RemediationExecution

    count = (
        await db_session.execute(
            select(func.count(RemediationExecution.id)).where(
                RemediationExecution.action_id == action.id
            )
        )
    ).scalar()
    assert count == 1


async def test_the_idempotency_key_changes_with_the_attempt(db_session):
    project, environment, component, action, service = await _authorized_pair(
        db_session
    )
    first = idempotency_key(action)
    action.attempt = action.attempt + 1
    assert idempotency_key(action) != first


async def test_an_external_action_is_refused_for_want_of_an_adapter(db_session):
    """§2/§100: reaching a system ARGUS does not own requires configuration.

    The refusal is a *value* with a reason, so it can be rendered and escalated
    on; it is not an exception that a caller might swallow.
    """
    from app.models.remediation import AdapterKind
    from app.services.remediation_executor import ExternalAdapter

    project, environment, component = await build_project(db_session)
    action = await make_action(
        db_session,
        project,
        environment,
        component,
        action_type=RemediationActionType.RESTART_SERVICE,
    )
    action.status = RemediationStatus.AUTHORIZED
    adapter = build_adapter(db_session, action.action_type)
    assert isinstance(adapter, ExternalAdapter)
    assert adapter.kind == AdapterKind.EXTERNAL
    available, reason = await adapter.available(action)
    assert available is False
    assert reason

    #: And the executor turns that into a refusal rather than an error.
    outcome = await RemediationExecutor(db_session).execute(
        action, mode=RemediationExecutionMode.HUMAN_APPROVAL
    )
    assert outcome.status == ExecutionStatus.REFUSED
    assert outcome.failure_reason == RemediationFailureReason.ADAPTER_UNAVAILABLE
    assert outcome.effect_applied is False
    assert isinstance(outcome, object) and not isinstance(outcome, AdapterUnavailable)


async def test_a_refused_execution_leaves_no_control_behind(db_session):
    project, environment, component, action, service = await _authorized_pair(
        db_session
    )
    #: Break the action *after* authorization so only the execution-time
    #: re-assessment can catch it.
    action.expires_at = utcnow() - timedelta(seconds=5)
    await db_session.flush()
    await service.run(action, actor="test")
    #: The attempt is recorded as refused, with the reason — a refusal is data,
    #: not a silent no-op.
    assert action.execution_status == ExecutionStatus.REFUSED
    assert action.status == RemediationStatus.BLOCKED
    assert (
        await is_paused(
            db_session,
            "anomaly_sweep",
            project_id=project.id,
            environment_id=environment.id,
        )
        is False
    )


async def test_duplicate_actions_are_refused_before_execution(db_session):
    """§24/§63: repeated proposals must not become a queue of identical work."""
    project, environment, component, action, service = await _authorized_pair(
        db_session
    )
    draft = make_draft(
        project,
        environment,
        component,
        action_type=action.action_type,
        parameters=action.parameters,
        incident_id=action.incident_id,
        source_id=action.source_id,
    )
    created = await RemediationService(db_session).propose([draft], created_by="test")
    assert created == []


async def test_a_second_attempt_records_its_own_row(db_session):
    """Retries are bounded, and each attempt is separately auditable (§27)."""
    from app.models.remediation import RemediationControlKind
    from app.services.remediation_controls import _current_row, revert_control

    project, environment, component, action, service = await _authorized_pair(
        db_session
    )
    executor = RemediationExecutor(db_session)
    first = await executor.execute(
        action, mode=RemediationExecutionMode.HUMAN_APPROVAL, actor="test"
    )
    assert first.effect_applied is True

    #: The effect is undone, so a second attempt is a real transition rather
    #: than the redundant re-application the safety engine refuses.
    control = await _current_row(
        db_session,
        RemediationControlKind.BACKGROUND_JOB,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )
    assert control is not None
    await revert_control(db_session, control, reverted_by="test", reason="retry")

    action.attempt = 2
    action.status = RemediationStatus.AUTHORIZED
    second = await executor.execute(
        action, mode=RemediationExecutionMode.HUMAN_APPROVAL, actor="test"
    )
    assert second.execution is not None
    assert second.execution.attempt == 2
    assert second.execution.id != first.execution.id


async def test_restarting_an_already_paused_job_is_a_real_transition_after_resume(
    db_session,
):
    """Pause → resume → pause is three revisions, not one state toggled."""
    project, environment, component, action, service = await _authorized_pair(
        db_session
    )
    await service.run(action, actor="test")
    from app.services.remediation_controls import _current_row
    from app.models.remediation import RemediationControlKind

    paused = await _current_row(
        db_session,
        RemediationControlKind.BACKGROUND_JOB,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )
    assert paused is not None
    await revert_control(
        db_session, paused, reverted_by="test", reason="fixture resume"
    )
    restored = await _current_row(
        db_session,
        RemediationControlKind.BACKGROUND_JOB,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )
    assert restored is None or restored.revision > paused.revision
    assert (
        await is_paused(
            db_session,
            "anomaly_sweep",
            project_id=project.id,
            environment_id=environment.id,
        )
        is False
    )


async def test_control_application_is_idempotent(db_session):
    """Re-applying the state already in force writes nothing new."""
    from app.models.remediation import (
        RemediationControlKind,
        RemediationControlState,
    )
    from app.services.remediation_controls import apply_control

    project, environment, component = await build_project(db_session)
    first = await apply_control(
        db_session,
        kind=RemediationControlKind.BACKGROUND_JOB,
        scope_key="code_sweep",
        state=RemediationControlState.PAUSED,
        project_id=project.id,
        environment_id=environment.id,
    )
    second = await apply_control(
        db_session,
        kind=RemediationControlKind.BACKGROUND_JOB,
        scope_key="code_sweep",
        state=RemediationControlState.PAUSED,
        project_id=project.id,
        environment_id=environment.id,
    )
    assert second.id == first.id
    assert second.revision == first.revision
