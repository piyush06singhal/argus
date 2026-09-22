"""Phase 9 — the remediation HTTP surface (§42–§44, §107).

The API is the boundary where the phase's guarantees are kept or lost, so the
tests check the boundary itself:

* the registry is readable and reports which actions are executable *here*;
* a proposal is not an authorization, no matter how it arrives;
* mutating requests demand a project and prove ownership;
* another project's rows are a 404, not a 403 that confirms they exist;
* the metrics endpoint counts refusals as prominently as successes;
* there is no endpoint anywhere that accepts a command.

Rows are seeded through ``db_session`` (the same database the client uses) and
asserted through HTTP, so every response describes real stored state.
"""

from __future__ import annotations

import uuid

from phase6_helpers import build_scope
from phase9_helpers import make_action, set_environment_class, set_policy

from app.models.incident import Incident, IncidentSeverity, IncidentStatus
from app.models.project import SoftwareProject
from app.models.remediation import (
    ExecutionStatus,
    RemediationActionType,
    RemediationExecutionMode,
    RemediationStatus,
    RollbackTrigger,
)
from app.services.remediation_clock import utcnow
from app.services.remediation_service import RemediationService


async def _project(client, name: str = "Phase9 API") -> dict:
    response = client.post(
        "/api/v1/projects",
        json={"name": name, "slug": f"phase9-{uuid.uuid4().hex[:8]}"},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


async def _scope(db_session, project_id: str):
    environment, component = await build_scope(db_session, uuid.UUID(project_id))
    project_row = await db_session.get(SoftwareProject, uuid.UUID(project_id))
    return project_row, environment, component


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_action_types_are_readable(client):
    response = client.get("/api/v1/remediation/action-types")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(RemediationActionType)
    by_name = {entry["action_type"]: entry for entry in body["actions"]}
    assert set(by_name) == {t.value for t in RemediationActionType}
    pause = by_name["PAUSE_BACKGROUND_JOB"]
    assert pause["executable_in_build"] is True
    assert pause["reversible"] is True
    assert pause["verification_plan"]
    assert pause["rollback_strategy"] == "INVERSE_ACTION"


def test_the_registry_says_which_actions_cannot_run_here(client):
    response = client.get("/api/v1/remediation/action-types")
    by_name = {entry["action_type"]: entry for entry in response.json()["actions"]}
    restart = by_name["RESTART_SERVICE"]
    assert restart["executable_in_build"] is False
    assert restart["unavailable_reason"]
    assert restart["requires_human_approval"] is True


def test_no_registry_entry_exposes_a_command_parameter(client):
    response = client.get("/api/v1/remediation/action-types")
    forbidden = {"command", "cmd", "script", "shell", "exec", "url", "ssh"}
    for entry in response.json()["actions"]:
        names = {parameter["name"] for parameter in entry["parameters"]}
        assert not (
            names & forbidden
        ), f"{entry['action_type']} exposes {sorted(names & forbidden)}"


# ---------------------------------------------------------------------------
# Scoping and isolation
# ---------------------------------------------------------------------------


def test_planning_requires_a_project(client):
    response = client.post("/api/v1/remediation/actions/plan", json={})
    assert response.status_code == 422


def test_planning_an_unknown_project_is_404(client):
    response = client.post(
        "/api/v1/remediation/actions/plan",
        json={"project_id": str(uuid.uuid4()), "incident_id": str(uuid.uuid4())},
    )
    assert response.status_code == 404


async def test_planning_without_a_source_is_refused(client):
    """Planning never invents a target; it needs an incident or a forecast."""
    project = await _project(client, "Phase9 Plan No Source")
    response = client.post(
        "/api/v1/remediation/actions/plan", json={"project_id": project["id"]}
    )
    assert response.status_code == 422
    assert "incident_id" in response.text or "forecast_id" in response.text


async def test_an_unknown_incident_is_404(client):
    project = await _project(client, "Phase9 Plan Unknown Incident")
    response = client.post(
        "/api/v1/remediation/actions/plan",
        json={"project_id": project["id"], "incident_id": str(uuid.uuid4())},
    )
    assert response.status_code == 404


async def test_another_projects_actions_are_not_visible(client, db_session):
    owner = await _project(client, "Phase9 Owner")
    stranger = await _project(client, "Phase9 Stranger")
    project_row, environment, component = await _scope(db_session, owner["id"])
    await make_action(db_session, project_row, environment, component)
    await db_session.commit()

    mine = client.get("/api/v1/remediation/actions", params={"project_id": owner["id"]})
    assert mine.status_code == 200
    assert mine.json()["count"] == 1

    theirs = client.get(
        "/api/v1/remediation/actions", params={"project_id": stranger["id"]}
    )
    assert theirs.status_code == 200
    assert theirs.json()["count"] == 0


async def test_another_projects_action_id_is_404_not_403(client, db_session):
    """A 403 would confirm the row exists; scope must not leak via status codes."""
    owner = await _project(client, "Phase9 Owner Detail")
    stranger = await _project(client, "Phase9 Stranger Detail")
    project_row, environment, component = await _scope(db_session, owner["id"])
    action = await make_action(db_session, project_row, environment, component)
    await db_session.commit()

    response = client.get(
        f"/api/v1/remediation/actions/{action.id}",
        params={"project_id": stranger["id"]},
    )
    assert response.status_code == 404


def test_listing_actions_for_an_unknown_project_is_404(client):
    response = client.get(
        "/api/v1/remediation/actions", params={"project_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404


def test_an_unknown_action_is_404(client):
    response = client.get(f"/api/v1/remediation/actions/{uuid.uuid4()}")
    assert response.status_code == 404


def test_mutating_endpoints_require_a_project(client):
    action_id = uuid.uuid4()
    cases = (
        (f"/api/v1/remediation/actions/{action_id}/approve", {"actor": "operator"}),
        (f"/api/v1/remediation/actions/{action_id}/reject", {"actor": "operator"}),
        (f"/api/v1/remediation/actions/{action_id}/execute", {"actor": "operator"}),
        (f"/api/v1/remediation/actions/{action_id}/rollback", {"actor": "operator"}),
        (f"/api/v1/remediation/actions/{action_id}/cancel", {"actor": "operator"}),
        (
            f"/api/v1/remediation/actions/{action_id}/record-execution",
            {"actor": "operator", "note": "did it by hand"},
        ),
    )
    for path, payload in cases:
        response = client.post(path, json=payload)
        assert response.status_code == 422, f"{path} accepted a request with no project"


# ---------------------------------------------------------------------------
# Propose → gates over HTTP
# ---------------------------------------------------------------------------


async def test_a_human_proposal_is_gated_not_executed(client, db_session):
    """§7: proposing is never authorizing, even when a person proposes it."""
    project = await _project(client, "Phase9 Proposal")
    _project_row, environment, component = await _scope(db_session, project["id"])
    await db_session.commit()

    response = client.post(
        "/api/v1/remediation/actions/propose",
        json={
            "project_id": project["id"],
            "environment_id": str(environment.id),
            "component_id": str(component.id),
            "action_type": "PAUSE_BACKGROUND_JOB",
            "description": "pause the anomaly sweep while the database is saturated",
            "reason": "saturation observed during the incident",
            "parameters": {"job": "anomaly_sweep"},
            "created_by": "operator",
            "auto_assess": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    #: No policy row exists for this project, so the restrictive default applies.
    assert body["status"] == RemediationStatus.REJECTED.value
    assert body["policy_status"] == "DENY"
    assert body["created_by"] == "operator"
    assert body["safety_status"] in ("PASSED", "PASSED_WITH_WARNINGS")
    assert body["execution_status"] is None


async def test_a_proposal_with_an_unknown_parameter_is_refused(client, db_session):
    project = await _project(client, "Phase9 Bad Param")
    _project_row, environment, component = await _scope(db_session, project["id"])
    await db_session.commit()

    response = client.post(
        "/api/v1/remediation/actions/propose",
        json={
            "project_id": project["id"],
            "environment_id": str(environment.id),
            "action_type": "PAUSE_BACKGROUND_JOB",
            "description": "do something unsupported",
            "parameters": {"job": "anomaly_sweep", "command": "rm -rf /"},
        },
    )
    assert response.status_code == 422
    assert "command" in response.text


async def test_an_unknown_action_type_is_rejected(client):
    project = await _project(client, "Phase9 Unknown Type")
    response = client.post(
        "/api/v1/remediation/actions/propose",
        json={
            "project_id": project["id"],
            "action_type": "RUN_SHELL_COMMAND",
            "description": "run a command",
            "parameters": {},
        },
    )
    assert response.status_code == 422


async def test_an_empty_description_is_rejected(client):
    project = await _project(client, "Phase9 Empty Description")
    response = client.post(
        "/api/v1/remediation/actions/propose",
        json={
            "project_id": project["id"],
            "action_type": "PAUSE_BACKGROUND_JOB",
            "description": "",
            "parameters": {"job": "anomaly_sweep"},
        },
    )
    assert response.status_code == 422


async def test_a_duplicate_human_proposal_is_refused(client, db_session):
    project = await _project(client, "Phase9 Duplicate Proposal")
    project_row, environment, component = await _scope(db_session, project["id"])
    await db_session.commit()
    payload = {
        "project_id": project["id"],
        "environment_id": str(environment.id),
        "component_id": str(component.id),
        "action_type": "PAUSE_BACKGROUND_JOB",
        "description": "pause the sweep",
        "parameters": {"job": "anomaly_sweep"},
        "auto_assess": False,
    }
    first = client.post("/api/v1/remediation/actions/propose", json=payload)
    assert first.status_code == 200, first.text
    second = client.post("/api/v1/remediation/actions/propose", json=payload)
    assert second.status_code == 409
    del project_row, component


async def test_a_proposal_needs_a_named_proposer(client):
    """Every proposal records who asked for it (§6, §12)."""
    project = await _project(client, "Phase9 Anonymous Proposal")
    response = client.post(
        "/api/v1/remediation/actions/propose",
        json={
            "project_id": project["id"],
            "action_type": "PAUSE_BACKGROUND_JOB",
            "description": "pause the sweep",
            "parameters": {"job": "anomaly_sweep"},
            "created_by": "",
        },
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Approve / execute over HTTP
# ---------------------------------------------------------------------------


async def _authorizable(client, db_session, name: str):
    project = await _project(client, name)
    project_row, environment, component = await _scope(db_session, project["id"])
    await set_environment_class(db_session, environment, "staging")
    await set_policy(
        db_session,
        project_row.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        allowed_action_types=["PAUSE_BACKGROUND_JOB"],
        cooldown_seconds=0,
        max_actions_per_window=10,
        canary_enabled=False,
    )
    action = await make_action(db_session, project_row, environment, component)
    service = RemediationService(db_session)
    await service.assess(action)
    await service.evaluate_policy(action)
    assert action.status == RemediationStatus.AWAITING_APPROVAL
    #: Approving here would hide the API's own approval endpoint, so the helper
    #: stops at the gate and the tests drive the decision over HTTP.
    await db_session.commit()
    return project, project_row, environment, component, action


async def _authorize_via_api(client, project_id: str, action, db_session=None) -> None:
    """Approve an action through the HTTP surface, as an operator would.

    The request runs on the client's own session, so the caller's object has to
    be refreshed before its attributes are read — otherwise the test would
    assert against a snapshot taken before the approval happened.
    """
    response = client.post(
        f"/api/v1/remediation/actions/{action.id}/approve",
        params={"project_id": project_id},
        json={"actor": "on-call", "reason": "reviewed the evidence"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == RemediationStatus.AUTHORIZED.value
    if db_session is not None:
        await db_session.refresh(action)


async def test_approval_authorizes_and_execution_verifies(client, db_session):
    """The whole human-approved path, over HTTP, with a real effect at the end."""
    from app.services.remediation_controls import is_paused

    project, _project_row, environment, _component, action = await _authorizable(
        client, db_session, "Phase9 Approve"
    )
    approved = client.post(
        f"/api/v1/remediation/actions/{action.id}/approve",
        params={"project_id": project["id"]},
        json={"actor": "on-call", "reason": "the sweep is saturating the database"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == RemediationStatus.AUTHORIZED.value
    assert approved.json()["authorized_by"] == "on-call"
    assert approved.json()["approved_at"] is not None

    executed = client.post(
        f"/api/v1/remediation/actions/{action.id}/run",
        params={"project_id": project["id"]},
        json={"actor": "on-call"},
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["status"] == RemediationStatus.VERIFIED.value
    assert executed.json()["outcome"] in ("EFFECTIVE", "PARTIALLY_EFFECTIVE")

    refreshed = await db_session.get(type(action), action.id)
    await db_session.refresh(refreshed)
    assert await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=refreshed.project_id,
        environment_id=environment.id,
    )


async def test_an_approval_requires_a_named_actor(client, db_session):
    project, _project_row, _environment, _component, action = await _authorizable(
        client, db_session, "Phase9 Approve No Actor"
    )
    response = client.post(
        f"/api/v1/remediation/actions/{action.id}/approve",
        params={"project_id": project["id"]},
        json={"reason": "no actor given"},
    )
    assert response.status_code == 422


async def test_an_approval_of_a_finished_action_is_refused(client, db_session):
    project, _project_row, environment, component, action = await _authorizable(
        client, db_session, "Phase9 Approve Finished"
    )
    action.status = RemediationStatus.REJECTED
    await db_session.commit()
    response = client.post(
        f"/api/v1/remediation/actions/{action.id}/approve",
        params={"project_id": project["id"]},
        json={"actor": "on-call"},
    )
    assert response.status_code == 409


async def test_execution_of_an_unauthorized_action_is_refused(client, db_session):
    """The whole point: a proposal cannot be executed by asking twice."""
    from app.services.remediation_controls import is_paused

    project, project_row, environment, component, _action = await _authorizable(
        client, db_session, "Phase9 Execute Unauthorized"
    )
    fresh = await make_action(
        db_session,
        project_row,
        environment,
        component,
        parameters={"job": "code_sweep"},
    )
    await db_session.commit()
    assert fresh.status == RemediationStatus.PROPOSED
    del component

    response = client.post(
        f"/api/v1/remediation/actions/{fresh.id}/execute",
        params={"project_id": project["id"]},
        json={"actor": "impatient"},
    )
    assert response.status_code in (200, 409)
    refreshed = await db_session.get(type(fresh), fresh.id)
    await db_session.refresh(refreshed)
    #: Asking to execute an unauthorized action may drive it up to the gate it
    #: is waiting on — AWAITING_APPROVAL is where it must stop. What it must
    #: never do is apply an effect.
    assert refreshed.status in (
        RemediationStatus.BLOCKED,
        RemediationStatus.PROPOSED,
        RemediationStatus.VALIDATING,
        RemediationStatus.POLICY_REVIEW,
        RemediationStatus.AWAITING_APPROVAL,
    )
    assert refreshed.status != RemediationStatus.VERIFIED
    assert refreshed.execution_status != ExecutionStatus.SUCCEEDED
    assert refreshed.outcome is None
    assert not await is_paused(
        db_session,
        "code_sweep",
        project_id=refreshed.project_id,
        environment_id=environment.id,
    )


async def test_an_expired_action_cannot_be_executed(client, db_session):
    """§29: authorized at 10:00 does not mean executable at 11:30."""
    from datetime import timedelta

    from app.services.remediation_controls import is_paused

    project, project_row, environment, _component, action = await _authorizable(
        client, db_session, "Phase9 Expired"
    )
    await _authorize_via_api(client, project["id"], action)
    action.expires_at = utcnow() - timedelta(seconds=1)
    await db_session.commit()

    response = client.post(
        f"/api/v1/remediation/actions/{action.id}/execute",
        params={"project_id": project["id"]},
        json={"actor": "on-call"},
    )
    assert response.status_code in (200, 409)
    refreshed = await db_session.get(type(action), action.id)
    await db_session.refresh(refreshed)
    assert refreshed.status == RemediationStatus.BLOCKED
    assert refreshed.failure_reason.value == "STALE_ACTION"
    assert not await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=project_row.id,
        environment_id=environment.id,
    )


async def test_a_rejected_action_is_terminal(client, db_session):
    project, _project_row, _environment, _component, action = await _authorizable(
        client, db_session, "Phase9 Reject"
    )
    response = client.post(
        f"/api/v1/remediation/actions/{action.id}/reject",
        params={"project_id": project["id"]},
        json={"actor": "on-call", "reason": "rollback is the better fix here"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == RemediationStatus.REJECTED.value

    again = client.post(
        f"/api/v1/remediation/actions/{action.id}/approve",
        params={"project_id": project["id"]},
        json={"actor": "on-call"},
    )
    assert again.status_code == 409


# ---------------------------------------------------------------------------
# Policy, stop, controls, breakers, metrics
# ---------------------------------------------------------------------------


async def test_the_effective_policy_is_readable_even_when_unset(client):
    project = await _project(client, "Phase9 Policy Read")
    response = client.get(
        "/api/v1/remediation/policy", params={"project_id": project["id"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "fallback"
    assert body["execution_mode"] == "OBSERVE_ONLY"
    assert body["clamped"] == []


async def test_a_policy_can_be_written_and_is_clamped_on_read(client):
    project = await _project(client, "Phase9 Policy Write")
    response = client.put(
        "/api/v1/remediation/policy",
        params={"project_id": project["id"]},
        json={
            "execution_mode": "HUMAN_APPROVAL",
            "max_actions_per_window": 100,
            "updated_by": "operator",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["execution_mode"] == "HUMAN_APPROVAL"
    assert body["revision"] >= 1
    assert body["max_actions_per_window"] <= 20
    assert "max_actions_per_window" in body["clamped"]


async def test_a_policy_with_an_unknown_action_type_is_rejected(client):
    project = await _project(client, "Phase9 Policy Bad List")
    response = client.put(
        "/api/v1/remediation/policy",
        params={"project_id": project["id"]},
        json={"allowed_action_types": ["NOT_A_REAL_ACTION"]},
    )
    assert response.status_code == 422


async def test_emergency_stop_engages_and_releases(client):
    project = await _project(client, "Phase9 Emergency Stop")
    response = client.post(
        "/api/v1/remediation/emergency-stop",
        params={"project_id": project["id"]},
        json={"engage": True, "actor": "operator", "reason": "in progress"},
    )
    assert response.status_code == 200
    assert response.json()["emergency_stop_active"] is True
    assert response.json()["execution_mode"] == "EMERGENCY_STOP"

    released = client.post(
        "/api/v1/remediation/emergency-stop",
        params={"project_id": project["id"]},
        json={"engage": False, "actor": "operator"},
    )
    assert released.status_code == 200
    assert released.json()["emergency_stop_active"] is False


async def test_an_emergency_stop_blocks_authorized_work(client, db_session):
    project, _project_row, _environment, _component, action = await _authorizable(
        client, db_session, "Phase9 Stop Blocks"
    )
    await _authorize_via_api(client, project["id"], action, db_session)
    assert action.status == RemediationStatus.AUTHORIZED

    response = client.post(
        "/api/v1/remediation/emergency-stop",
        params={"project_id": project["id"]},
        json={"engage": True, "actor": "operator", "reason": "stop everything"},
    )
    assert response.status_code == 200
    refreshed = await db_session.get(type(action), action.id)
    await db_session.refresh(refreshed)
    assert refreshed.status == RemediationStatus.BLOCKED
    assert refreshed.failure_reason.value == "EMERGENCY_STOP"


async def test_controls_and_breakers_are_listable(client):
    project = await _project(client, "Phase9 Controls")
    controls = client.get(
        "/api/v1/remediation/controls", params={"project_id": project["id"]}
    )
    assert controls.status_code == 200
    assert controls.json() == {"controls": [], "count": 0}

    breakers = client.get(
        "/api/v1/remediation/breakers", params={"project_id": project["id"]}
    )
    assert breakers.status_code == 200
    assert breakers.json()["count"] == 0


async def test_an_applied_control_is_visible_through_the_api(client, db_session):
    project, _project_row, _environment, _component, action = await _authorizable(
        client, db_session, "Phase9 Control Visible"
    )
    await _authorize_via_api(client, project["id"], action, db_session)
    ran = client.post(
        f"/api/v1/remediation/actions/{action.id}/run",
        params={"project_id": project["id"]},
        json={"actor": "on-call"},
    )
    assert ran.status_code == 200, ran.text
    assert ran.json()["status"] == RemediationStatus.VERIFIED.value
    response = client.get(
        "/api/v1/remediation/controls", params={"project_id": project["id"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    control = body["controls"][0]
    assert control["kind"] == "BACKGROUND_JOB"
    assert control["scope_key"] == "anomaly_sweep"
    assert control["state"] == "PAUSED"
    assert control["applied_by_action_id"] == str(action.id)
    assert control["effective"] is True


async def test_metrics_count_actions_and_refusals(client, db_session):
    project, _project_row, _environment, _component, action = await _authorizable(
        client, db_session, "Phase9 Metrics"
    )
    client.post(
        f"/api/v1/remediation/actions/{action.id}/reject",
        params={"project_id": project["id"]},
        json={"actor": "on-call", "reason": "not the right fix"},
    )
    response = client.get(
        "/api/v1/remediation/metrics", params={"project_id": project["id"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_actions"] == 1
    assert body["by_action_type"] == {"PAUSE_BACKGROUND_JOB": 1}
    assert body["emergency_stop_active"] is False
    assert sum(body["by_status"].values()) == body["total_actions"]


async def test_the_sweep_endpoint_runs(client):
    response = client.post("/api/v1/remediation/sweep", json={"plan": False})
    assert response.status_code == 200
    assert "projects" in response.json()


# ---------------------------------------------------------------------------
# The evidence chain
# ---------------------------------------------------------------------------


async def test_an_action_detail_carries_its_whole_chain(client, db_session):
    project, _project_row, _environment, _component, action = await _authorizable(
        client, db_session, "Phase9 Detail"
    )
    response = client.get(
        f"/api/v1/remediation/actions/{action.id}",
        params={"project_id": project["id"]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"]["id"] == str(action.id)
    assert body["proposal"] is not None
    assert body["assessments"]
    assert body["policy_decisions"]
    assert body["approvals"]
    assert body["audit"]
    assert body["audit_chain"]["intact"] is True
    #: Proposal, safety, policy and the approval request are already recorded by
    #: the time the action is waiting on a person.
    assert body["audit_chain"]["events"] >= 4
    assert "AUTHORIZED" in body["allowed_transitions"]
    assert body["action"]["safety_status"] != "FAILED"


async def test_an_audit_chain_can_be_verified_over_http(client, db_session):
    project, _project_row, _environment, _component, action = await _authorizable(
        client, db_session, "Phase9 Audit Verify"
    )
    response = client.get(
        f"/api/v1/remediation/actions/{action.id}/audit/verify",
        params={"project_id": project["id"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intact"] is True
    assert body["broken_at"] is None


async def test_the_audit_history_is_readable_over_http(client, db_session):
    project, _project_row, _environment, _component, action = await _authorizable(
        client, db_session, "Phase9 Audit History"
    )
    response = client.get(
        f"/api/v1/remediation/actions/{action.id}/audit",
        params={"project_id": project["id"]},
    )
    assert response.status_code == 200
    events = response.json()
    assert events
    assert events[0]["sequence"] == 1
    assert events[0]["event_type"] == "ACTION_PROPOSED"


async def test_an_incidents_actions_are_visible(client, db_session):
    project = await _project(client, "Phase9 Incident Actions")
    project_row, environment, component = await _scope(db_session, project["id"])
    incident = Incident(
        project_id=project_row.id,
        environment_id=environment.id,
        primary_component_id=component.id,
        title="checkout failures",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        detected_at=utcnow(),
        started_at=utcnow(),
        summary="error rate elevated",
    )
    db_session.add(incident)
    await db_session.flush()
    action = await make_action(
        db_session,
        project_row,
        environment,
        component,
        incident_id=incident.id,
        source_id=incident.id,
    )
    await db_session.commit()

    response = client.get(
        f"/api/v1/remediation/incidents/{incident.id}/actions",
        params={"project_id": project["id"]},
    )
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["actions"][0]["id"] == str(action.id)


# ---------------------------------------------------------------------------
# Manual execution
# ---------------------------------------------------------------------------


async def test_recording_a_manual_execution_verifies_the_claim(client, db_session):
    """§26: ARGUS records what a person did, then checks it from telemetry."""
    project = await _project(client, "Phase9 Manual Execution")
    project_row, environment, component = await _scope(db_session, project["id"])
    action = await make_action(db_session, project_row, environment, component)
    service = RemediationService(db_session)
    await service.assess(action)
    await service.evaluate_policy(action)
    #: The restrictive default rejects, which is a terminal state, so the record
    #: endpoint's own guard is exercised below with a blocked action instead.
    assert action.status == RemediationStatus.REJECTED
    action.status = RemediationStatus.BLOCKED
    await db_session.commit()

    response = client.post(
        f"/api/v1/remediation/actions/{action.id}/record-execution",
        params={"project_id": project["id"]},
        json={
            "actor": "on-call engineer",
            "note": "restarted the checkout service by hand from the runbook",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["executed_by"] == "on-call engineer"
    #: A recorded claim is verified, not trusted: with no telemetry it cannot be
    #: confirmed, so it must not come back as VERIFIED.
    assert body["status"] != RemediationStatus.VERIFIED.value
    assert body["execution_status"] == ExecutionStatus.SUCCEEDED.value


async def test_recording_a_manual_execution_for_a_live_action_is_refused(
    client, db_session
):
    project, _project_row, _environment, _component, action = await _authorizable(
        client, db_session, "Phase9 Manual Guard"
    )
    response = client.post(
        f"/api/v1/remediation/actions/{action.id}/record-execution",
        params={"project_id": project["id"]},
        json={"actor": "engineer", "note": "did it by hand"},
    )
    assert response.status_code == 409


async def test_a_manual_execution_records_a_reason_but_not_a_command(
    client, db_session
):
    """The note is prose for the audit trail, never something ARGUS executes."""
    project = await _project(client, "Phase9 Manual Note")
    project_row, environment, component = await _scope(db_session, project["id"])
    action = await make_action(db_session, project_row, environment, component)
    action.status = RemediationStatus.BLOCKED
    await db_session.commit()

    response = client.post(
        f"/api/v1/remediation/actions/{action.id}/record-execution",
        params={"project_id": project["id"]},
        json={"actor": "engineer", "note": "; rm -rf / #"},
    )
    assert response.status_code == 200
    body = response.json()
    #: It is stored as text and nothing else; the action never executed it.
    assert body["execution_status"] == ExecutionStatus.SUCCEEDED.value
    assert body["status"] != RemediationStatus.VERIFIED.value


# ---------------------------------------------------------------------------
# There is nothing to inject a command into
# ---------------------------------------------------------------------------


def test_no_request_schema_accepts_a_command_field():
    """A broad, cheap check: no Phase 9 request model has a command field."""
    from app.schemas import remediation as schemas

    forbidden = {"command", "cmd", "script", "shell", "exec", "argv", "url", "ssh"}
    checked = 0
    for name in dir(schemas):
        model = getattr(schemas, name)
        fields = getattr(model, "model_fields", None)
        if not isinstance(fields, dict):
            continue
        checked += 1
        assert not (
            set(fields) & forbidden
        ), f"{name} accepts {sorted(set(fields) & forbidden)}"
    assert checked > 15, "the schema module should expose the request models"


async def test_unexpected_fields_are_rejected_not_ignored(client, db_session):
    project, _project_row, _environment, _component, action = await _authorizable(
        client, db_session, "Phase9 Extra Fields"
    )
    response = client.post(
        f"/api/v1/remediation/actions/{action.id}/approve",
        params={"project_id": project["id"]},
        json={"actor": "on-call", "script": "rm -rf /"},
    )
    assert response.status_code == 422


async def test_a_rollback_request_cannot_name_a_script(client):
    project = await _project(client, "Phase9 Rollback Injection")
    response = client.post(
        f"/api/v1/remediation/actions/{uuid.uuid4()}/rollback",
        params={"project_id": project["id"]},
        json={"actor": "operator", "reason": "ok", "script": "rm -rf /"},
    )
    assert response.status_code == 422


async def test_rollback_over_http_reverses_the_effect(client, db_session):
    from app.services.remediation_controls import is_paused

    project, project_row, environment, _component, action = await _authorizable(
        client, db_session, "Phase9 Rollback HTTP"
    )
    await _authorize_via_api(client, project["id"], action, db_session)
    client.post(
        f"/api/v1/remediation/actions/{action.id}/run",
        params={"project_id": project["id"]},
        json={"actor": "on-call"},
    )
    response = client.post(
        f"/api/v1/remediation/actions/{action.id}/rollback",
        params={"project_id": project["id"]},
        json={"actor": "on-call", "reason": "the pause caused a backlog"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == RemediationStatus.ROLLED_BACK.value
    assert not await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=project_row.id,
        environment_id=environment.id,
    )


async def test_a_rollback_of_something_never_applied_is_refused(client, db_session):
    project, _project_row, _environment, _component, action = await _authorizable(
        client, db_session, "Phase9 Rollback Nothing"
    )
    response = client.post(
        f"/api/v1/remediation/actions/{action.id}/rollback",
        params={"project_id": project["id"]},
        json={"actor": "on-call", "reason": "undo it"},
    )
    assert response.status_code == 409


def test_the_trigger_enum_is_the_only_way_a_rollback_is_attributed():
    """Rollback triggers are enumerated, so an attribution cannot be invented."""

    assert RollbackTrigger.HUMAN_REQUEST in RollbackTrigger
    assert set(RollbackTrigger) >= {
        RollbackTrigger.HUMAN_REQUEST,
        RollbackTrigger.VERIFICATION_FAILED,
        RollbackTrigger.EXECUTION_FAILED,
    }
