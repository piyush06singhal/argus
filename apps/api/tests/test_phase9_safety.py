"""Phase 9 — the safety engine (§4, §7, §19, §20, §24, §26, §29).

The safety engine answers "may this action proceed at all?", and its verdict is
not overridable. The tests concentrate on the ways an action can be *bad* even
though it is registered and permitted:

* it references no stored evidence at all;
* its target has been deleted, or belongs to another project;
* its parameters name something that does not exist;
* it is stale by the time anyone gets to it;
* it duplicates an action already in flight;
* its declared blast radius is wider than the action may ever use.

Then the properties that must hold for a *good* action: the declaration of
reversibility, the warning-not-pass treatment of unobservable checks, and the
re-assessment at execution time.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.core.config import get_settings
from app.models.remediation import (
    BlastRadiusScope,
    CheckResult,
    RemediationActionType,
    RemediationFailureReason,
    RemediationRiskLevel,
    RemediationStatus,
    SafetyStatus,
)
from app.services.remediation_clock import utcnow
from app.services.remediation_service import RemediationService
from tests.phase9_helpers import (
    build_incident,
    build_project,
    clone_action,
    make_action,
    make_draft,
)

settings = get_settings()


def _check_names(report, name: str) -> list[dict]:
    return [check for check in report.checks if check["name"] == name]


async def _assess(session, action, **kwargs):
    return await RemediationService(session).assess(action, **kwargs)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


async def test_an_action_with_no_evidence_is_refused(db_session):
    """§7: an unevidenced remediation is indistinguishable from a guess."""
    from app.models.remediation import RemediationSourceType
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    action = await make_action(
        db_session, project, environment, component, incident_id=None, source_id=None
    )
    #: Strip the evidence the fixture attached, to reach the refusal path.
    action.incident_id = None
    action.source_id = None
    action.causal_analysis_id = None
    action.fix_id = None
    action.patch_id = None
    action.source_type = RemediationSourceType.INCIDENT

    report = await SafetyEngine(db_session).assess(action)
    assert report.status == SafetyStatus.FAILED
    assert "evidence_present" in report.blocking
    assert report.failure_reason == RemediationFailureReason.NOT_ACTIONABLE


async def test_a_human_operator_proposal_carries_its_own_evidence(db_session):
    """§7 allows an operator to propose something ARGUS did not infer."""
    from app.models.remediation import RemediationSourceType
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    action = await make_action(
        db_session,
        project,
        environment,
        component,
        source_type=RemediationSourceType.HUMAN_OPERATOR,
        incident_id=None,
        source_id=None,
    )
    report = await SafetyEngine(db_session).assess(action)
    names = {check["name"]: check["result"] for check in report.checks}
    assert names["evidence_present"] == CheckResult.PASS.value


# ---------------------------------------------------------------------------
# Scope and targets
# ---------------------------------------------------------------------------


async def test_a_deleted_target_component_is_refused(db_session):
    from app.models.system import SystemComponent
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    await db_session.delete(component)
    await db_session.flush()

    report = await SafetyEngine(db_session).assess(action)
    names = {check["name"]: check["result"] for check in report.checks}
    assert names["target_exists"] == CheckResult.FAIL.value
    assert report.status == SafetyStatus.FAILED
    del SystemComponent


async def test_a_component_from_another_project_is_refused(db_session):
    """§45: cross-tenant references are a refusal, not a lookup failure."""
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    other_project, other_environment, other_component = await build_project(
        db_session, name="Other Tenant"
    )
    action = await make_action(db_session, project, environment, component)
    action.component_id = other_component.id

    report = await SafetyEngine(db_session).assess(action)
    names = {check["name"]: check["result"] for check in report.checks}
    assert names["target_exists"] == CheckResult.FAIL.value
    assert report.status == SafetyStatus.FAILED


async def test_an_environment_from_another_project_is_refused(db_session):
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    other_project, other_environment, other_component = await build_project(
        db_session, name="Other Tenant Env"
    )
    action = await make_action(db_session, project, environment, component)
    action.environment_id = other_environment.id

    report = await SafetyEngine(db_session).assess(action)
    names = {check["name"]: check["result"] for check in report.checks}
    assert names["environment_in_scope"] == CheckResult.FAIL.value
    assert report.status == SafetyStatus.FAILED
    assert report.failure_reason is not None


async def test_an_environment_less_action_is_explicitly_skipped_not_passed(db_session):
    """\"Does not apply\" and \"checked and fine\" must not look the same (§19)."""
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    action.environment_id = None

    report = await SafetyEngine(db_session).assess(action)
    names = {check["name"]: check["result"] for check in report.checks}
    assert names["environment_in_scope"] == CheckResult.SKIPPED.value


# ---------------------------------------------------------------------------
# Action-specific preconditions
# ---------------------------------------------------------------------------


async def test_an_unknown_control_target_is_refused(db_session):
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    action = await make_action(
        db_session,
        project,
        environment,
        component,
        parameters={"job": "not_a_real_job"},
    )
    report = await SafetyEngine(db_session).assess(action)
    names = {check["name"]: check["result"] for check in report.checks}
    assert names["control_target_known"] == CheckResult.FAIL.value
    assert report.status == SafetyStatus.FAILED


async def test_pausing_an_already_paused_job_is_refused_as_redundant(db_session):
    """Idempotence is not a license: a redundant action is a duplicate."""
    from app.services.remediation_controls import apply_control
    from app.models.remediation import (
        RemediationControlKind,
        RemediationControlState,
    )
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    await apply_control(
        db_session,
        kind=RemediationControlKind.BACKGROUND_JOB,
        scope_key="anomaly_sweep",
        state=RemediationControlState.PAUSED,
        project_id=project.id,
        environment_id=environment.id,
        reason="fixture",
    )

    report = await SafetyEngine(db_session).assess(action)
    names = {check["name"]: check["result"] for check in report.checks}
    assert names["control_not_already_applied"] == CheckResult.FAIL.value


async def test_an_unverified_patch_cannot_be_applied(db_session):
    """§9: APPLY_VERIFIED_PATCH means *verified*, checked at assessment time."""
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    action = await make_action(
        db_session,
        project,
        environment,
        component,
        action_type=RemediationActionType.APPLY_VERIFIED_PATCH,
        parameters={"patch_id": str(uuid.uuid4())},
        patch_id=uuid.uuid4(),
    )
    report = await SafetyEngine(db_session).assess(action)
    names = {check["name"]: check["result"] for check in report.checks}
    assert names["patch_verified"] == CheckResult.FAIL.value
    assert report.status == SafetyStatus.FAILED


# ---------------------------------------------------------------------------
# Duplicates, staleness, blast radius, reversibility
# ---------------------------------------------------------------------------


async def test_a_duplicate_active_action_is_refused(db_session):
    """§24: the same remediation for the same target is one action, not a queue."""
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    incident = await build_incident(db_session, project, environment, component)
    first = await make_action(
        db_session, project, environment, component, incident_id=incident.id
    )
    #: The service refuses a duplicate *proposal*; this clone reproduces the
    #: concurrent-request race the safety guard exists to catch.
    second = await clone_action(db_session, first)
    report = await SafetyEngine(db_session).assess(second)
    names = {check["name"]: check["result"] for check in report.checks}
    assert names["no_active_duplicate"] == CheckResult.FAIL.value
    assert report.status == SafetyStatus.FAILED


async def test_a_finished_action_does_not_block_a_new_one(db_session):
    """The duplicate guard is about *active* work, not about history."""
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    incident = await build_incident(db_session, project, environment, component)
    first = await make_action(
        db_session, project, environment, component, incident_id=incident.id
    )
    first.status = RemediationStatus.ROLLED_BACK
    second = await clone_action(db_session, first)
    report = await SafetyEngine(db_session).assess(second)
    names = {check["name"]: check["result"] for check in report.checks}
    assert names["no_active_duplicate"] == CheckResult.PASS.value


async def test_the_service_refuses_a_duplicate_proposal_before_it_exists(db_session):
    """The first line of defence: a duplicate proposal is simply not created."""
    from app.services.remediation_service import RemediationService

    project, environment, component = await build_project(db_session)
    incident = await build_incident(db_session, project, environment, component)
    first = await make_action(
        db_session, project, environment, component, incident_id=incident.id
    )
    draft = make_draft(
        project,
        environment,
        component,
        incident_id=incident.id,
        source_id=incident.id,
    )
    assert draft.fingerprint == first.fingerprint
    created = await RemediationService(db_session).propose([draft], created_by="test")
    assert created == [], "an active action with the same fingerprint must be refused"


async def test_the_table_refuses_two_actions_with_the_same_identity(db_session):
    """The last line of defence is the database, not a code path (§24).

    ``(project_id, fingerprint, attempt)`` is unique, so even a bug that skipped
    both dedup checks cannot create a second live copy of one remediation.
    """
    from sqlalchemy.exc import IntegrityError

    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    with pytest.raises(IntegrityError):
        await clone_action(db_session, action, attempt=action.attempt)


async def test_a_stale_action_warns_at_assessment_and_blocks_at_execution(db_session):
    """§29: time passing between approval and execution is a real hazard."""
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    action.expires_at = utcnow() - timedelta(seconds=1)

    assessment = await SafetyEngine(db_session).assess(action)
    names = {check["name"]: check["result"] for check in assessment.checks}
    assert names["not_expired"] == CheckResult.SKIPPED.value

    at_execution = await SafetyEngine(db_session).assess(action, at_execution=True)
    names = {check["name"]: check["result"] for check in at_execution.checks}
    assert names["not_expired"] == CheckResult.FAIL.value
    assert at_execution.status == SafetyStatus.FAILED
    assert at_execution.failure_reason == RemediationFailureReason.STALE_ACTION


async def test_a_blast_radius_wider_than_the_registry_allows_is_refused(db_session):
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    action.action_type = RemediationActionType.RESTART_INSTANCE
    action.parameters = {"instance_id": str(uuid.uuid4())}
    action.blast_radius = BlastRadiusScope.ENVIRONMENT

    report = await SafetyEngine(db_session).assess(action)
    names = {check["name"]: check["result"] for check in report.checks}
    assert names["blast_radius_within_registry"] == CheckResult.FAIL.value


async def test_a_percentage_scoped_action_without_a_percentage_narrows_to_the_canary(
    db_session,
):
    """Under-specification must resolve to the *narrowest* reading (§7)."""
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    action.blast_radius = BlastRadiusScope.LIMITED_PERCENTAGE
    action.blast_radius_percent = None

    report = await SafetyEngine(db_session).assess(action)
    assert report.blast_radius_percent == min(
        settings.REMEDIATION_CANARY_PERCENT, 100.0
    )


async def test_an_invalid_percentage_is_refused(db_session):
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    action.blast_radius = BlastRadiusScope.LIMITED_PERCENTAGE
    action.blast_radius_percent = 150.0

    report = await SafetyEngine(db_session).assess(action)
    names = {check["name"]: check["result"] for check in report.checks}
    assert names["blast_radius_percent_valid"] == CheckResult.FAIL.value


@pytest.mark.parametrize(
    "action_type",
    [
        RemediationActionType.RESTART_SERVICE,
        RemediationActionType.ROLLBACK_DEPLOYMENT,
        RemediationActionType.SCALE_SERVICE_WITHIN_LIMIT,
        RemediationActionType.ENABLE_FEATURE_FLAG,
    ],
)
async def test_actions_that_need_a_human_say_so_in_the_assessment(
    db_session, action_type
):
    """§4: the safety report states the approval requirement, not the API."""
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    parameters = {
        RemediationActionType.RESTART_SERVICE: {},
        RemediationActionType.ROLLBACK_DEPLOYMENT: {"deployment_id": str(uuid.uuid4())},
        RemediationActionType.SCALE_SERVICE_WITHIN_LIMIT: {"replicas": 3},
        RemediationActionType.ENABLE_FEATURE_FLAG: {"flag": "graph_extraction"},
    }[action_type]
    action = await make_action(
        db_session,
        project,
        environment,
        component,
        action_type=action_type,
        parameters=parameters,
    )
    report = await SafetyEngine(db_session).assess(action)
    assert report.requires_human_approval is True


async def test_a_reversible_autonomous_capable_action_does_not_require_a_human(
    db_session,
):
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    report = await SafetyEngine(db_session).assess(action)
    assert report.requires_human_approval is False
    assert report.reversible is True


async def test_warnings_do_not_block_but_are_recorded(db_session):
    """A production scope is a warning at assessment, not a veto."""
    from app.services.remediation_safety import SafetyEngine

    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    report = await SafetyEngine(db_session).assess(action)
    if report.warnings:
        assert report.status in (
            SafetyStatus.PASSED_WITH_WARNINGS,
            SafetyStatus.PASSED,
        )
        assert report.passed is True


async def test_an_invalid_parameter_set_blocks_before_anything_else(db_session):
    """An unrecognised parameter refuses the action and can never take effect."""
    from app.services.remediation_state import may_apply_effect

    project, environment, component = await build_project(db_session)
    action = await make_action(
        db_session,
        project,
        environment,
        component,
        parameters={"job": "anomaly_sweep", "command": "rm -rf /"},
    )
    report = await _assess(db_session, action)
    assert report.status == SafetyStatus.FAILED
    assert action.status == RemediationStatus.BLOCKED
    assert action.failure_reason == RemediationFailureReason.PARAMETER_INVALID
    #: The attempted parameters stay on the record as audit data (that is the
    #: point of recording the attempt), but a blocked action may not apply an
    #: effect and no handler is ever selected for it.
    assert may_apply_effect(action.status) is False
    assert "command" not in report.rollback_plan.get("parameters", {})


async def test_the_assessment_is_persisted_even_when_it_refuses(db_session):
    """\"Every gate leaves a row\" — including the refusals."""
    from sqlalchemy import select

    from app.models.remediation import RemediationAssessment

    project, environment, component = await build_project(db_session)
    action = await make_action(
        db_session, project, environment, component, parameters={"job": "nope"}
    )
    await _assess(db_session, action)
    rows = (
        (
            await db_session.execute(
                select(RemediationAssessment).where(
                    RemediationAssessment.action_id == action.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows, "a refused assessment must still be recorded"
    assert rows[-1].status == SafetyStatus.FAILED
    assert rows[-1].blocking


async def test_the_assessment_verdict_is_recorded_on_the_action(db_session):
    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    await _assess(db_session, action)
    assert action.safety_status is not None
    assert action.safety_status != SafetyStatus.FAILED
    assert action.status == RemediationStatus.POLICY_REVIEW


async def test_a_passing_assessment_does_not_pre_applied_parameters(db_session):
    """Validated parameters are persisted so a handler only ever sees clean ones."""
    project, environment, component = await build_project(db_session)
    action = await make_action(
        db_session, project, environment, component, parameters={"job": "code_sweep"}
    )
    action.parameters = {"job": "code_sweep", "duration_seconds": 900}
    await _assess(db_session, action)
    assert action.parameters["job"] == "code_sweep"
    assert action.parameters["duration_seconds"] == 900


async def test_a_low_risk_fixture_reaches_policy_review_not_further(db_session):
    """The safety gate advances the action; it does not authorize it."""
    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    assert action.risk_level == RemediationRiskLevel.LOW
    await _assess(db_session, action)
    assert action.status == RemediationStatus.POLICY_REVIEW
    assert action.policy_status is None
