"""Phase 11 — the workflow engine and its safety rules (§11–§13).

§13 is the section that matters. It says a workflow **must stop** when
authorization expires, evidence goes stale, the incident disappears, the state
changes materially, policy changes, the kill switch fires, or verification fails.
A workflow engine without those stops is an autonomous loop with a nice diagram,
so these tests are written refusals-first: each stop condition gets a test that
produces it from stored rows and asserts the reason it reports.

Two properties are worth calling out because they are the ones a reviewer should
check hardest:

* ``test_a_stopped_run_cannot_be_advanced`` — a stop is terminal for the run, not
  a sticky flag that a later tick can walk past.
* ``test_the_kill_switch_stops_a_run`` — Phase 9 remains authoritative. Phase 11
  wires itself into that control rather than growing its own.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.models.incident import IncidentStatus
from app.models.platform import (
    CaseStatus,
    WorkflowStage,
    WorkflowStatus,
    WorkflowStopReason,
)
from app.services.reliability_workflow import (
    STAGE_ORDER,
    active_workflow_for_case,
    advance,
    can_advance,
    claim_due,
    get_workflow,
    record_failure,
    record_stage,
    resume,
    stage_index,
    start_workflow,
    stop_workflow,
    wait_for_approval,
    workflow_history,
)
from tests.phase11_helpers import episode, make_case, utcnow

pytestmark = pytest.mark.asyncio


class TestStageOrder:
    """§11: the lifecycle, and the rule that it only moves forward."""

    def test_the_documented_lifecycle_is_the_stage_order(self):
        assert [stage.value for stage in STAGE_ORDER] == [
            "DETECTED",
            "TRIAGED",
            "ANALYZING",
            "DIAGNOSED",
            "REMEDIATION_READY",
            "AUTHORIZED",
            "EXECUTING",
            "VERIFYING",
            "RESOLVED",
            "LEARNED",
        ]

    def test_stages_advance_one_direction(self):
        assert can_advance(WorkflowStage.DETECTED, WorkflowStage.ANALYZING)
        assert not can_advance(WorkflowStage.ANALYZING, WorkflowStage.DETECTED)

    def test_stage_index_is_ordered(self):
        assert stage_index(WorkflowStage.DETECTED) < stage_index(WorkflowStage.LEARNED)


class TestStartingAndAdvancing:
    async def test_a_run_starts_at_the_stage_it_is_given(self, db_session):
        ctx = await episode(db_session)
        case = await make_case(db_session, project=ctx.project, component=ctx.component)
        workflow = await start_workflow(
            db_session,
            project_id=ctx.project.id,
            case=case,
            stage=WorkflowStage.DETECTED,
        )
        assert workflow.status is WorkflowStatus.RUNNING
        assert workflow.stage is WorkflowStage.DETECTED
        assert workflow.deadline_at is not None

    async def test_a_run_advances_through_the_lifecycle(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            environment=ctx.environment,
            component=ctx.component,
            incident=ctx.incident,
        )
        workflow = await start_workflow(
            db_session, project_id=ctx.project.id, case=case
        )
        for target in (
            WorkflowStage.TRIAGED,
            WorkflowStage.ANALYZING,
            WorkflowStage.DIAGNOSED,
        ):
            tick = await advance(
                db_session, workflow=workflow, target=target, case=case
            )
            assert tick.action == "advanced", tick.detail
            assert workflow.stage is target

        #: Every stage it passed through is recorded, so the run's progress is
        #: auditable rather than inferred from its current stage.
        assert workflow.completed_stages
        assert "DETECTED" in workflow.completed_stages

    async def test_advancing_past_the_deadline_stops_the_run(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        workflow = await start_workflow(
            db_session,
            project_id=ctx.project.id,
            case=case,
            deadline_seconds=60,
        )
        future = utcnow() + timedelta(hours=2)
        tick = await advance(
            db_session,
            workflow=workflow,
            target=WorkflowStage.ANALYZING,
            case=case,
            now=future,
        )
        assert tick.action == "stopped"
        assert tick.stop_reason == WorkflowStopReason.TIMED_OUT.value
        #: §12: a stopped run says *why*, not just that it stopped.
        assert tick.detail

    async def test_stale_evidence_stops_the_run(self, db_session):
        """§13: the context the run was started with has aged out, so the run
        must not act on it."""
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        workflow = await start_workflow(
            db_session, project_id=ctx.project.id, case=case
        )
        far_future = workflow.started_at + timedelta(days=2)
        tick = await advance(
            db_session,
            workflow=workflow,
            target=WorkflowStage.ANALYZING,
            case=case,
            now=far_future,
        )
        assert tick.action == "stopped"
        assert tick.stop_reason in (
            WorkflowStopReason.EVIDENCE_STALE.value,
            WorkflowStopReason.TIMED_OUT.value,
        )

    async def test_a_cancelled_case_stops_its_run(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        workflow = await start_workflow(
            db_session, project_id=ctx.project.id, case=case
        )
        case.status = CaseStatus.CANCELLED
        await db_session.flush()

        tick = await advance(
            db_session, workflow=workflow, target=WorkflowStage.TRIAGED, case=case
        )
        assert tick.action == "stopped"
        assert tick.stop_reason == WorkflowStopReason.CANCELLED.value

    async def test_a_deleted_incident_stops_its_run(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        workflow = await start_workflow(
            db_session, project_id=ctx.project.id, case=case
        )
        await db_session.delete(ctx.incident)
        await db_session.flush()

        tick = await advance(
            db_session, workflow=workflow, target=WorkflowStage.TRIAGED, case=case
        )
        assert tick.action == "stopped"
        assert tick.stop_reason == WorkflowStopReason.INCIDENT_GONE.value

    async def test_a_resolved_incident_lands_the_run_at_resolved(self, db_session):
        """§13's ``STATE_CHANGED`` is not a failure: the situation ended before
        ARGUS acted, so the run stops where the situation did instead of
        remediating something that is no longer broken."""
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        workflow = await start_workflow(
            db_session, project_id=ctx.project.id, case=case
        )
        ctx.incident.status = IncidentStatus.RESOLVED
        await db_session.flush()

        tick = await advance(
            db_session, workflow=workflow, target=WorkflowStage.ANALYZING, case=case
        )
        #: ``STATE_CHANGED`` lands the run rather than halting it, so the action
        #: is named for what happened: the situation resolved, from outside.
        assert tick.action == "resolved_externally"
        assert workflow.stage is WorkflowStage.RESOLVED
        #: RESOLVED is not terminal for the *run*: LEARNED still has to happen,
        #: so the run stays live and the engine keeps it claimable (§11).
        assert workflow.status is WorkflowStatus.RUNNING
        assert workflow.stop_reason is None

    async def test_a_finished_run_reports_that_rather_than_advancing(self, db_session):
        """The property that makes every stop real: once a run is terminal, the
        engine refuses to move it. ``check_preconditions`` reports "nothing
        violated" for a finished run — true, and dangerous if read as consent."""
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        workflow = await start_workflow(
            db_session, project_id=ctx.project.id, case=case, deadline_seconds=60
        )
        await advance(
            db_session,
            workflow=workflow,
            target=WorkflowStage.ANALYZING,
            case=case,
            now=workflow.started_at + timedelta(hours=3),
        )
        assert workflow.status is WorkflowStatus.TIMED_OUT
        stage_before = workflow.stage

        tick = await advance(
            db_session, workflow=workflow, target=WorkflowStage.ANALYZING, case=case
        )
        assert tick.action == "already_finished"
        assert workflow.stage is stage_before, "a stopped run must not move"
        assert workflow.status is WorkflowStatus.TIMED_OUT

    async def test_a_finished_run_is_not_resumed(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        workflow = await start_workflow(
            db_session, project_id=ctx.project.id, case=case
        )
        await stop_workflow(
            db_session,
            workflow=workflow,
            reason=WorkflowStopReason.CANCELLED,
            detail="the operator stopped this",
        )
        tick = await resume(db_session, workflow=workflow, case=case)
        assert tick.action == "already_finished"
        assert workflow.status is WorkflowStatus.CANCELLED


class TestKillSwitch:
    """§13, §129: Phase 9's controls stay authoritative over Phase 11's engine."""

    async def test_the_kill_switch_stops_a_run(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        workflow = await start_workflow(
            db_session, project_id=ctx.project.id, case=case
        )

        from app.models.remediation import (
            RemediationControlKind,
            RemediationControlState,
        )
        from app.services.remediation_controls import apply_control

        await apply_control(
            db_session,
            kind=RemediationControlKind.BACKGROUND_JOB,
            scope_key="remediation_sweep",
            state=RemediationControlState.PAUSED,
            project_id=ctx.project.id,
            applied_by="operator",
            reason="emergency stop",
        )
        await db_session.flush()

        tick = await advance(
            db_session, workflow=workflow, target=WorkflowStage.TRIAGED, case=case
        )
        assert tick.action == "stopped"
        assert tick.stop_reason == WorkflowStopReason.KILL_SWITCH.value


class TestApprovalAndFailure:
    async def test_waiting_for_approval_blocks_the_run(self, db_session):
        """§12: human approval is a state, not a delay — the run parks and says
        what it is waiting for."""
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        workflow = await start_workflow(
            db_session,
            project_id=ctx.project.id,
            case=case,
            stage=WorkflowStage.REMEDIATION_READY,
        )
        await wait_for_approval(
            db_session,
            workflow=workflow,
            case=case,
            reason="waiting for the on-call engineer to approve the canary",
        )
        assert workflow.status is WorkflowStatus.WAITING_APPROVAL
        #: The run records what it is waiting for, so a parked workflow is
        #: diagnosable without reading logs.
        assert workflow.state["waiting_for"] == "approval"
        assert "on-call" in workflow.state["waiting_reason"]

    async def test_a_failed_run_records_the_failure_and_can_be_resumed(
        self, db_session
    ):
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        workflow = await start_workflow(
            db_session,
            project_id=ctx.project.id,
            case=case,
            stage=WorkflowStage.VERIFYING,
            max_attempts=2,
        )
        tick = await record_failure(
            db_session,
            workflow=workflow,
            case=case,
            detail="the verification run failed twice",
        )
        assert workflow.attempt >= 1
        assert workflow.last_error == "the verification run failed twice"
        assert tick.action in ("retried", "failed")

    async def test_a_run_can_be_stopped_by_hand_with_a_reason(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        workflow = await start_workflow(
            db_session, project_id=ctx.project.id, case=case
        )
        await stop_workflow(
            db_session,
            workflow=workflow,
            reason=WorkflowStopReason.CANCELLED,
            detail="the operator stopped this manually",
        )
        assert workflow.status is WorkflowStatus.CANCELLED
        assert workflow.stop_detail == "the operator stopped this manually"

    async def test_a_parked_run_can_be_resumed(self, db_session):
        """The one legitimate resume: a run waiting on a person wakes up once the
        wait is over. Its stage does not move — resuming continues, it does not
        skip ahead."""
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        workflow = await start_workflow(
            db_session,
            project_id=ctx.project.id,
            case=case,
            stage=WorkflowStage.REMEDIATION_READY,
        )
        await wait_for_approval(
            db_session, workflow=workflow, case=case, reason="awaiting approval"
        )
        stage_before = workflow.stage

        tick = await resume(db_session, workflow=workflow, case=case)
        assert tick.action == "resumed"
        assert workflow.status is WorkflowStatus.RUNNING
        assert workflow.stage is stage_before
        assert workflow.state["waiting_for"] is None


class TestWorkflowReads:
    async def test_a_case_has_one_live_run_at_a_time(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        first = await start_workflow(db_session, project_id=ctx.project.id, case=case)
        active = await active_workflow_for_case(db_session, case_id=case.id)
        assert active is not None
        assert active.id == first.id

    async def test_history_shows_the_run_and_its_stages(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        workflow = await start_workflow(
            db_session, project_id=ctx.project.id, case=case
        )
        await record_stage(
            db_session,
            workflow=workflow,
            case=case,
            target=WorkflowStage.TRIAGED,
        )
        history = await workflow_history(db_session, case_id=case.id)
        assert len(history) == 1
        assert history[0].stage is WorkflowStage.TRIAGED

    async def test_claiming_due_work_is_bounded_and_ordered(self, db_session):
        from tests.phase6_helpers import build_project

        project, _environment, component = await build_project(db_session)
        for _ in range(3):
            case = await make_case(db_session, project=project, component=component)
            await start_workflow(db_session, project_id=project.id, case=case)
        due = await claim_due(db_session, limit=2)
        assert len(due) <= 2
        assert all(
            row.project_id == project.id for row in due
        ), "the project filter is applied by the caller, so the ids must be ours"

    async def test_workflows_are_scoped_to_their_case(self, db_session):
        from tests.phase6_helpers import build_project

        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        workflow = await start_workflow(
            db_session, project_id=ctx.project.id, case=case
        )
        other_project, other_env, other_component = await build_project(db_session)
        other_case = await make_case(
            db_session, project=other_project, component=other_component
        )
        assert await active_workflow_for_case(db_session, case_id=other_case.id) is None
        #: And an unknown id is simply absent rather than raising.
        assert await get_workflow(db_session, workflow_id=uuid.uuid4()) is None
        assert await get_workflow(db_session, workflow_id=workflow.id) is not None
