"""Phase 10 — the reliability intelligence HTTP surface (§51–§62, §89–§91).

The API is where the phase's guarantees become observable, so these tests check
the boundary itself:

* every endpoint that reads requires a project, and an unknown project is a 404;
* another project's knowledge, experience, recommendation or run is a 404 —
  never a 403 that confirms it exists;
* knowledge a human has not approved is visible *as* pending, not as believed;
* a review decision the lifecycle forbids is a 409, not a silent no-op;
* there is no endpoint here that executes anything;
* search answers from stored rows, cites them, and says "no comparable
  historical case was found" when there is none (§49, §90).

Rows are seeded through ``db_session`` — the same database the client uses — and
asserted through HTTP, so every response describes real stored state.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from tests.phase6_helpers import build_scope
from tests.phase10_helpers import (
    emit_anomaly,
    emit_incident,
    hours_before,
    make_knowledge,
    record_series,
    utcnow,
)

from app.models.intelligence import RecommendationType
from app.models.project import SoftwareProject
from app.services.recommendation_engine import (
    RecommendationDraft,
    ReliabilityRecommendationEngine,
)


async def _project(client, name: str = "Phase10 API") -> dict:
    response = client.post(
        "/api/v1/projects",
        json={"name": name, "slug": f"phase10-{uuid.uuid4().hex[:8]}"},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


async def _seeded(client, db_session, *, count: int = 5, **episode_kwargs):
    """A project created through HTTP, then seeded with real episodes.

    The project goes through the API (so scoping is exercised) and the telemetry
    is written directly (so the fixture controls the timestamps).
    """
    project = await _project(client)
    project_row = await db_session.get(SoftwareProject, uuid.UUID(project["id"]))
    environment, component = await build_scope(db_session, project_row.id)
    episodes = await record_series(
        db_session,
        project_row,
        environment,
        component,
        count=count,
        first_started_at=hours_before(utcnow(), 24 * (count + 2)),
        spacing_hours=24.0,
        **episode_kwargs,
    )
    await db_session.commit()
    return project, project_row, environment, component, episodes


async def _open_incident(db_session, project_row, environment, component, **kwargs):
    started = utcnow() - timedelta(minutes=kwargs.pop("minutes_open", 20))
    incident = await emit_incident(
        db_session,
        project_row,
        environment,
        component,
        detected_at=started,
        resolved_at=None,
        status="OPEN",
        **kwargs,
    )
    await emit_anomaly(
        db_session,
        project_row,
        environment,
        component,
        detected_at=started,
        anomaly_type="ERROR_RATE_SPIKE",
        metric_name="http.checkout.error_rate",
    )
    await db_session.commit()
    return incident


# ---------------------------------------------------------------------------
# Health, dashboard, metrics
# ---------------------------------------------------------------------------


def test_health_reports_the_learning_configuration(client):
    response = client.get("/api/v1/intelligence/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["learning_enabled"] is True
    #: Autonomous activation is off by default and the endpoint says so (§73).
    assert body["auto_activation_enabled"] is False
    assert body["include_ai_generated"] is False
    assert body["minimum_samples"]["validation"] >= 1


def test_dashboard_requires_a_project(client):
    assert client.get("/api/v1/intelligence/dashboard").status_code == 422
    assert (
        client.get(
            f"/api/v1/intelligence/dashboard?project_id={uuid.uuid4()}"
        ).status_code
        == 404
    )


async def test_dashboard_counts_real_rows(client, db_session):
    project, project_row, _, _, episodes = await _seeded(client, db_session, count=2)
    await make_knowledge(db_session, project_row, status="ACTIVE")
    await db_session.commit()

    response = client.get(f"/api/v1/intelligence/dashboard?project_id={project['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["active_knowledge"] == 1
    assert body["experiences"] == len(episodes)
    assert body["knowledge_by_status"]["ACTIVE"] == 1
    assert body["recently_learned"]


async def test_metrics_report_gaps_as_well_as_successes(client, db_session):
    project, project_row, _, _, _ = await _seeded(client, db_session, count=2)
    await make_knowledge(db_session, project_row, status="ACTIVE")
    await make_knowledge(
        db_session,
        project_row,
        status="REJECTED",
        feature_signature="remediation:other",
    )
    await db_session.commit()

    response = client.get(f"/api/v1/intelligence/metrics?project_id={project['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["knowledge_validated_or_active"] == 1
    assert body["knowledge_rejected"] == 1
    assert body["pattern_validation_rate"] == pytest.approx(0.5)
    assert body["experiences"] == 2


# ---------------------------------------------------------------------------
# Knowledge (§53)
# ---------------------------------------------------------------------------


async def test_knowledge_list_and_detail_carry_their_evidence(client, db_session):
    project, project_row, _, _, _ = await _seeded(client, db_session, count=4)
    row = await make_knowledge(
        db_session,
        project_row,
        status="CANDIDATE",
        sample_count=4,
        success_count=3,
        details={"action_type": "restart_service"},
    )
    await db_session.commit()

    listing = client.get(f"/api/v1/intelligence/knowledge?project_id={project['id']}")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["status"] == "CANDIDATE"
    assert item["sample_count"] == 4
    #: §7. A pattern is never returned without the facts that qualify it.
    assert item["limitations"]
    assert item["sources"]
    assert item["algorithm_version"]

    detail = client.get(
        f"/api/v1/intelligence/knowledge/{row.id}?project_id={project['id']}"
    )
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["knowledge"]["id"] == str(row.id)
    assert payload["versions"]
    assert payload["versions"][0]["sample_count"] == 4


async def test_an_unknown_filter_value_is_rejected_not_ignored(client, db_session):
    project, _, _, _, _ = await _seeded(client, db_session, count=1)
    response = client.get(
        f"/api/v1/intelligence/knowledge?project_id={project['id']}&status=NOT_A_STATUS"
    )
    assert response.status_code == 422
    assert "NOT_A_STATUS" in response.text


async def test_candidate_knowledge_can_be_reviewed_and_activated(client, db_session):
    project, project_row, _, _, _ = await _seeded(client, db_session, count=5)
    row = await make_knowledge(db_session, project_row, status="VALIDATED")
    await db_session.commit()

    response = client.post(
        f"/api/v1/intelligence/knowledge/{row.id}/review?project_id={project['id']}",
        json={
            "decision": "APPROVE",
            "reviewer": "engineer@example.com",
            "reason": "checked the cases",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["knowledge"]["status"] == "ACTIVE"
    assert body["knowledge"]["reviewed_by"] == "engineer@example.com"
    assert body["reviews"][0]["decision"] == "APPROVE"


async def test_a_candidate_cannot_be_activated_directly(client, db_session):
    """A review cannot skip validation; the refusal is explicit (§72, §74)."""
    project, project_row, _, _, _ = await _seeded(client, db_session, count=2)
    row = await make_knowledge(db_session, project_row, status="CANDIDATE")
    await db_session.commit()

    response = client.post(
        f"/api/v1/intelligence/knowledge/{row.id}/review?project_id={project['id']}",
        json={
            "decision": "APPROVE",
            "reviewer": "engineer@example.com",
            "reason": "looks right to me",
        },
    )
    assert response.status_code == 409
    assert "cannot be activated" in response.text


async def test_a_review_decision_without_a_reason_is_refused(client, db_session):
    """§72, §78. The reviewer's reasoning is part of the record, not a footnote.

    The client refuses to submit an unexplained decision; the API refuses to
    store one, because a caller is not obliged to be the client.
    """
    project, project_row, _, _, _ = await _seeded(client, db_session, count=2)
    row = await make_knowledge(db_session, project_row, status="VALIDATED")
    await db_session.commit()

    response = client.post(
        f"/api/v1/intelligence/knowledge/{row.id}/review?project_id={project['id']}",
        json={"decision": "APPROVE", "reviewer": "engineer@example.com"},
    )
    assert response.status_code == 422


async def test_an_unknown_review_decision_is_rejected(client, db_session):
    project, project_row, _, _, _ = await _seeded(client, db_session, count=1)
    row = await make_knowledge(db_session, project_row, status="VALIDATED")
    await db_session.commit()
    response = client.post(
        f"/api/v1/intelligence/knowledge/{row.id}/review?project_id={project['id']}",
        json={"decision": "PROBABLY_FINE", "reviewer": "engineer@example.com"},
    )
    assert response.status_code == 422


async def test_the_version_ledger_is_readable(client, db_session):
    project, project_row, _, _, _ = await _seeded(client, db_session, count=5)
    row = await make_knowledge(db_session, project_row, status="VALIDATED")
    await db_session.commit()
    client.post(
        f"/api/v1/intelligence/knowledge/{row.id}/review?project_id={project['id']}",
        json={
            "decision": "APPROVE",
            "reviewer": "engineer@example.com",
            "reason": "the sample and the scope support it",
        },
    )
    response = client.get(
        f"/api/v1/intelligence/knowledge/{row.id}/versions?project_id={project['id']}"
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert items
    #: §26. A version is a snapshot of what was believed, not a pointer.
    assert items[0]["snapshot"]["status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# Experiences and patterns (§54, §56)
# ---------------------------------------------------------------------------


async def test_experiences_are_listed_with_their_signatures(client, db_session):
    project, _, _, _, _ = await _seeded(client, db_session, count=3)
    listing = client.get(f"/api/v1/intelligence/experiences?project_id={project['id']}")
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert len(items) == 3
    assert items[0]["failure_label"]
    assert items[0]["failure_fingerprint"]
    assert items[0]["resolution_label"]
    #: §9. Normalized structured features, never raw telemetry.
    signature = items[0]["failure_signature"]
    assert "metric_behaviors" in signature
    assert "anomaly_types" in signature
    assert all("raw" not in key for key in signature)

    detail = client.get(
        f"/api/v1/intelligence/experiences/{items[0]['id']}?project_id={project['id']}"
    )
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    stages = [entry["stage"] for entry in payload["timeline"]]
    assert stages[0] == "detection"
    assert stages[-1] == "outcome"
    assert payload["component"]["name"]


async def test_the_pattern_explorer_hides_retired_patterns_by_default(
    client, db_session
):
    project, project_row, _, _, _ = await _seeded(client, db_session, count=2)
    await make_knowledge(
        db_session, project_row, status="ACTIVE", feature_signature="live:one"
    )
    await make_knowledge(
        db_session, project_row, status="DEPRECATED", feature_signature="dead:one"
    )
    await db_session.commit()

    live = client.get(f"/api/v1/intelligence/patterns?project_id={project['id']}")
    assert live.status_code == 200, live.text
    assert [item["status"] for item in live.json()["items"]] == ["ACTIVE"]

    everything = client.get(
        f"/api/v1/intelligence/patterns?project_id={project['id']}&include_retired=true"
    )
    assert everything.status_code == 200
    assert len(everything.json()["items"]) == 2


# ---------------------------------------------------------------------------
# Recommendations (§57, §40, §42, §43)
# ---------------------------------------------------------------------------


async def test_recommendations_can_be_generated_decided_and_closed(client, db_session):
    project, project_row, environment, component, _ = await _seeded(
        client, db_session, count=4
    )
    incident = await _open_incident(
        db_session,
        project_row,
        environment,
        component,
        fingerprint="checkout_error_spike",
        title="Checkout failures",
    )

    generated = client.get(
        f"/api/v1/intelligence/incidents/{incident.id}/recommendations"
        f"?project_id={project['id']}&generate=true"
    )
    assert generated.status_code == 200, generated.text
    items = generated.json()["items"]
    assert items, "no recommendation was produced for an open incident"

    target = items[0]
    #: §40. Advice without its evidence is not advice.
    assert target["current_evidence"]
    assert target["limitations"]

    detail = client.get(
        f"/api/v1/intelligence/recommendations/{target['id']}?project_id={project['id']}"
    )
    assert detail.status_code == 200, detail.text

    decided = client.post(
        f"/api/v1/intelligence/recommendations/{target['id']}/decide"
        f"?project_id={project['id']}",
        json={
            "decision": "ACCEPTED",
            "actor": "oncall@example.com",
            "reason": "matches what we saw",
        },
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["recommendation"]["status"] == "ACCEPTED"
    assert decided.json()["recommendation"]["decided_by"] == "oncall@example.com"

    outcome = client.post(
        f"/api/v1/intelligence/recommendations/{target['id']}/outcome"
        f"?project_id={project['id']}",
        json={"verdict": "EFFECTIVE", "recorded_by": "oncall@example.com"},
    )
    assert outcome.status_code == 200, outcome.text
    body = outcome.json()
    assert body["recommendation"]["status"] == "EFFECTIVE"
    assert body["outcomes"][0]["verdict"] == "EFFECTIVE"


async def test_an_invalid_outcome_verdict_is_rejected(client, db_session):
    project, project_row, _, _, _ = await _seeded(client, db_session, count=1)
    engine = ReliabilityRecommendationEngine()
    row = await engine.persist(
        db_session,
        RecommendationDraft(
            project_id=project_row.id,
            recommendation_type=RecommendationType.INVESTIGATE_COMPONENT,
            title="fixture",
            rationale="fixture",
            current_evidence={"source": "test"},
        ),
    )
    await db_session.commit()

    response = client.post(
        f"/api/v1/intelligence/recommendations/{row.id}/outcome?project_id={project['id']}",
        json={"verdict": "PROBABLY_FINE", "recorded_by": "someone"},
    )
    assert response.status_code == 422


async def test_a_dismissed_recommendation_explains_itself(client, db_session):
    project, project_row, _, _, _ = await _seeded(client, db_session, count=1)
    engine = ReliabilityRecommendationEngine()
    row = await engine.persist(
        db_session,
        RecommendationDraft(
            project_id=project_row.id,
            recommendation_type=RecommendationType.INVESTIGATE_COMPONENT,
            title="fixture",
            rationale="fixture",
            current_evidence={"source": "test"},
        ),
    )
    await db_session.commit()
    response = client.post(
        f"/api/v1/intelligence/recommendations/{row.id}/decide?project_id={project['id']}",
        json={
            "decision": "DISMISSED",
            "actor": "oncall@example.com",
            "reason": "known noise",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()["recommendation"]
    assert body["status"] == "DISMISSED"
    assert body["decision"]["reason"] == "known noise"


# ---------------------------------------------------------------------------
# Component profiles and effectiveness (§58, §15, §16, §45)
# ---------------------------------------------------------------------------


async def test_component_profile_is_available_after_a_run(client, db_session):
    project, project_row, _, component, _ = await _seeded(client, db_session, count=6)
    from app.services.component_profiles import recompute_profiles

    await recompute_profiles(db_session, project_id=project_row.id)
    await db_session.commit()

    response = client.get(
        f"/api/v1/intelligence/components/{component.id}/profile?project_id={project['id']}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["component"]["name"] == component.name
    assert body["profiles"]
    assert body["profiles"][0]["incident_count"] >= 1
    assert body["profiles"][0]["breakdown"] is not None


async def test_effectiveness_reports_counts_and_refuses_to_overclaim(
    client, db_session
):
    project, _, _, _, _ = await _seeded(client, db_session, count=4)
    response = client.get(
        f"/api/v1/intelligence/remediation-effectiveness?project_id={project['id']}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["observational_label"] == "OBSERVATIONAL COMPARISON"
    assert body["headline"]
    for bucket in body["buckets"]:
        #: §15. A count and its sample size, never a bare percentage.
        assert bucket["comparable"] >= bucket["successful"]
        assert bucket["minimum_samples"] >= 1
        if bucket["insufficient"]:
            assert bucket["limitations"]


async def test_comparing_actions_is_labelled_observational(client, db_session):
    project, _, _, _, _ = await _seeded(client, db_session, count=4)
    response = client.get(
        "/api/v1/intelligence/remediation-effectiveness/compare"
        f"?project_id={project['id']}&action_a=RESTART_SERVICE&action_b=ROLLBACK_DEPLOYMENT"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["label"] == "OBSERVATIONAL COMPARISON"
    assert body["verdict"] in (
        "INSUFFICIENT_EVIDENCE",
        "NO_MEANINGFUL_DIFFERENCE",
        "FAVOURS_RESTART_SERVICE",
        "FAVOURS_ROLLBACK_DEPLOYMENT",
    )
    assert body["limitations"]


# ---------------------------------------------------------------------------
# Search (§46–§49, §89, §90)
# ---------------------------------------------------------------------------


async def test_search_says_so_when_there_is_no_history(client, db_session):
    """A component the project has never had an episode on (§97).

    The project *does* have history — just none of it comparable to this
    incident. "No comparable case" has to mean comparable, not "this project is
    empty", or the answer would be useless the moment a project has any past.
    """
    project, project_row, _, _, _ = await _seeded(client, db_session, count=4)
    environment, component = await build_scope(
        db_session, project_row.id, component="brand-new-service"
    )
    await db_session.commit()
    incident = await _open_incident(
        db_session,
        project_row,
        environment,
        component,
        fingerprint="brand_new_shape",
        title="Something we have never seen",
    )

    response = client.get(
        f"/api/v1/intelligence/search?project_id={project['id']}"
        f"&q=have%20we%20seen%20this%20before&incident_id={incident.id}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"] == "recurrence"
    #: §49. No fabricated history, no invented cause.
    assert body["answer"] == "No comparable historical case was found."
    assert body["evidence_available"] is False
    assert body["citations"] == []


async def test_search_cites_the_episodes_it_found(client, db_session):
    project, project_row, environment, component, _ = await _seeded(
        client, db_session, count=4
    )
    incident = await _open_incident(
        db_session,
        project_row,
        environment,
        component,
        fingerprint="checkout_error_spike",
    )

    response = client.get(
        f"/api/v1/intelligence/search?project_id={project['id']}"
        f"&q=have%20we%20seen%20this%20before&incident_id={incident.id}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["evidence_available"] is True
    assert body["experiences"]
    assert body["citations"], "a match with no citation would be unverifiable"
    for citation in body["citations"]:
        assert citation["type"] in (
            "experience",
            "incident",
            "knowledge",
            "remediation",
        )
        assert citation["id"]
    #: §50. A citation that no longer resolves is reported, not rendered.
    assert body["warnings"] == []


async def test_search_for_fixes_reports_effectiveness_counts(client, db_session):
    project, _, _, _, _ = await _seeded(client, db_session, count=4)
    response = client.get(
        f"/api/v1/intelligence/search?project_id={project['id']}"
        "&q=what%20fixed%20similar%20incidents"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"] == "remediation"
    assert body["answer"]
    for bucket in body["effectiveness"]:
        assert bucket["comparable"] >= 1


# ---------------------------------------------------------------------------
# Learning runs (§59, §27)
# ---------------------------------------------------------------------------


async def test_a_run_can_be_triggered_and_read_back(client, db_session):
    project, _, _, _, _ = await _seeded(client, db_session, count=5)
    response = client.post(
        "/api/v1/intelligence/learning-runs",
        json={"project_id": project["id"], "trigger": "manual"},
    )
    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["status"] == "COMPLETED"
    assert summary["run_id"]
    #: The fixture built its episodes through the real builder, so the run has
    #: no pending events to convert: it mines the stored corpus. Only a run
    #: that consumes an event creates an experience (§7), which the next test
    #: pins down.
    assert summary["patterns_discovered"] >= 1
    assert summary["projects"] == [project["id"]]

    runs = client.get(f"/api/v1/intelligence/learning-runs?project_id={project['id']}")
    assert runs.status_code == 200, runs.text
    assert runs.json()["total"] >= 1

    detail = client.get(
        f"/api/v1/intelligence/learning-runs/{summary['run_id']}?project_id={project['id']}"
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["run"]["patterns_discovered"] == summary["patterns_discovered"]
    assert body["run"]["data_cutoff"]


async def test_a_run_converts_a_pending_event_into_an_experience(client, db_session):
    """§7. Raw events do not become knowledge; a run turns them into episodes.

    This is the whole pipeline: the incident is written, the hook publishes an
    event, and only the run creates the experience that pattern mining then
    reads.
    """
    from app.services.learning_hooks import record_incident_completed

    project, project_row, environment, component, _ = await _seeded(
        client, db_session, count=0
    )
    incident = await emit_incident(
        db_session,
        project_row,
        environment,
        component,
        detected_at=utcnow() - timedelta(hours=2),
        resolved_at=utcnow() - timedelta(hours=1),
        status="RESOLVED",
        fingerprint="novel_shape",
    )
    await db_session.commit()
    await record_incident_completed(db_session, incident=incident)
    await db_session.commit()

    response = client.post(
        "/api/v1/intelligence/learning-runs",
        json={"project_id": project["id"], "trigger": "manual"},
    )
    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["events_processed"] >= 1
    assert summary["experiences_created"] == 1

    detail = client.get(f"/api/v1/intelligence/experiences?project_id={project['id']}")
    assert detail.status_code == 200
    assert detail.json()["total"] == 1


async def test_triggering_a_run_for_an_unknown_project_is_a_404(client):
    response = client.post(
        "/api/v1/intelligence/learning-runs",
        json={"project_id": str(uuid.uuid4()), "trigger": "manual"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Controls (§63, §76, §80)
# ---------------------------------------------------------------------------


def test_event_hooks_expose_what_is_learned_from(client):
    response = client.get("/api/v1/intelligence/event-hooks")
    assert response.status_code == 200, response.text
    body = response.json()
    #: §77. AI-generated evidence is recorded but not learned from by default.
    assert "AI_GENERATED" not in body["trusted_provenance"]
    assert "INCIDENT_RESOLVED" in body["enabled_event_types"]


async def test_event_hooks_can_be_narrowed_but_not_widened(client, db_session):
    project, _, _, _, _ = await _seeded(client, db_session, count=1)
    narrowed = client.put(
        "/api/v1/intelligence/event-hooks",
        json={
            "project_id": project["id"],
            "enabled_event_types": ["INCIDENT_RESOLVED"],
            "trusted_provenance": ["OBSERVABILITY", "AI_GENERATED"],
            "updated_by": "operator@example.com",
        },
    )
    assert narrowed.status_code == 200, narrowed.text
    body = narrowed.json()
    assert body["enabled_event_types"] == ["INCIDENT_RESOLVED"]
    #: The hook asked for AI-generated data but the deployment does not trust it,
    #: so the answer is the deployment's — a row cannot widen trust.
    assert "AI_GENERATED" not in body["trusted_provenance"]


async def test_an_unknown_event_type_in_a_hook_is_rejected(client, db_session):
    project, _, _, _, _ = await _seeded(client, db_session, count=1)
    response = client.put(
        "/api/v1/intelligence/event-hooks",
        json={"project_id": project["id"], "enabled_event_types": ["SOMETHING_ELSE"]},
    )
    assert response.status_code == 422


async def test_sweep_endpoint_runs_and_reports(client, db_session):
    project, _, _, _, _ = await _seeded(client, db_session, count=5)
    response = client.post(
        f"/api/v1/intelligence/sweep?project_id={project['id']}&force=true"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["disabled"] is False
    assert body["projects_run"] >= 1
    assert body["runs"]


async def test_experiments_are_listed_and_activate_nothing(client, db_session):
    """§74. An experiment scores an algorithm; it cannot promote one."""
    project, _, _, _, _ = await _seeded(client, db_session, count=1)
    response = client.get(
        f"/api/v1/intelligence/experiments?project_id={project['id']}"
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


# ---------------------------------------------------------------------------
# Isolation (§62, §91)
# ---------------------------------------------------------------------------


async def test_another_projects_knowledge_is_invisible(client, db_session):
    project_a, row_a, _, _, _ = await _seeded(client, db_session, count=2)
    knowledge = await make_knowledge(db_session, row_a, status="ACTIVE")
    await db_session.commit()

    project_b = await _project(client, "Phase10 Other")

    listing = client.get(f"/api/v1/intelligence/knowledge?project_id={project_b['id']}")
    assert listing.status_code == 200
    assert listing.json()["total"] == 0

    detail = client.get(
        f"/api/v1/intelligence/knowledge/{knowledge.id}?project_id={project_b['id']}"
    )
    #: 404, not 403: the row's existence is not disclosed (§62).
    assert detail.status_code == 404


async def test_another_projects_experience_and_run_are_404(client, db_session):
    _, project_row, _, _, episodes = await _seeded(client, db_session, count=2)
    from app.services.learning_run import execute_learning_run

    summary = await execute_learning_run(
        db_session, project_id=project_row.id, trigger="test"
    )
    await db_session.commit()
    project_b = await _project(client, "Phase10 Other")
    experience_id = episodes[0]["experience"].id

    assert (
        client.get(
            f"/api/v1/intelligence/experiences/{experience_id}?project_id={project_b['id']}"
        ).status_code
        == 404
    )
    assert summary.run_id is not None
    assert (
        client.get(
            f"/api/v1/intelligence/learning-runs/{summary.run_id}?project_id={project_b['id']}"
        ).status_code
        == 404
    )


async def test_another_projects_component_profile_is_404(client, db_session):
    _, _, _, component, _ = await _seeded(client, db_session, count=1)
    project_b = await _project(client, "Phase10 Other")
    response = client.get(
        f"/api/v1/intelligence/components/{component.id}/profile?project_id={project_b['id']}"
    )
    assert response.status_code == 404


async def test_a_recommendation_from_another_project_cannot_be_decided(
    client, db_session
):
    _, project_row, _, _, _ = await _seeded(client, db_session, count=1)
    engine = ReliabilityRecommendationEngine()
    row = await engine.persist(
        db_session,
        RecommendationDraft(
            project_id=project_row.id,
            recommendation_type=RecommendationType.INVESTIGATE_COMPONENT,
            title="fixture",
            rationale="fixture",
            current_evidence={"source": "test"},
        ),
    )
    await db_session.commit()

    project_b = await _project(client, "Phase10 Other")
    response = client.post(
        f"/api/v1/intelligence/recommendations/{row.id}/decide?project_id={project_b['id']}",
        json={"decision": "ACCEPTED", "actor": "intruder@example.com"},
    )
    assert response.status_code == 404


async def test_search_does_not_see_another_projects_history(client, db_session):
    """Project A has four comparable episodes; project B must see none of them."""
    await _seeded(client, db_session, count=4)
    project_b = await _project(client, "Phase10 Other")
    project_row_b = await db_session.get(SoftwareProject, uuid.UUID(project_b["id"]))
    environment_b, component_b = await build_scope(db_session, project_row_b.id)
    incident_b = await _open_incident(
        db_session,
        project_row_b,
        environment_b,
        component_b,
        fingerprint="checkout_error_spike",
    )

    response = client.get(
        f"/api/v1/intelligence/search?project_id={project_b['id']}"
        f"&q=have%20we%20seen%20this%20before&incident_id={incident_b.id}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"] == "No comparable historical case was found."
    assert body["experiences"] == []


async def test_a_foreign_incident_cannot_be_used_as_search_context(client, db_session):
    _, project_row, environment, component, _ = await _seeded(
        client, db_session, count=1
    )
    incident = await _open_incident(db_session, project_row, environment, component)
    project_b = await _project(client, "Phase10 Other")
    response = client.get(
        f"/api/v1/intelligence/search?project_id={project_b['id']}"
        f"&q=have%20we%20seen%20this%20before&incident_id={incident.id}"
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# The surface is data (§62)
# ---------------------------------------------------------------------------


def _iter_intelligence_operations(schema):
    for path, methods in schema["paths"].items():
        if "/intelligence" not in path:
            continue
        for method, operation in methods.items():
            yield path, method, operation


def test_no_intelligence_endpoint_accepts_a_command_or_a_path(client):
    """§62. The surface is data. Nothing here takes a command, URL or file path.

    Checked against the generated OpenAPI document rather than by reading the
    routes, so a handler added later cannot escape the rule.
    """
    forbidden = {"command", "cmd", "script", "shell", "exec", "url", "path", "ssh"}
    schema = client.get("/openapi.json").json()
    components = schema.get("components", {}).get("schemas", {})
    checked = 0
    for path, method, operation in _iter_intelligence_operations(schema):
        body = operation.get("requestBody")
        if not body:
            continue
        json_body = body.get("content", {}).get("application/json")
        if not json_body:
            continue
        ref = json_body.get("schema", {}).get("$ref")
        if not ref:
            continue
        model = components.get(ref.split("/")[-1], {})
        checked += 1
        for field in model.get("properties", {}):
            assert field not in forbidden, f"{method.upper()} {path} accepts {field!r}"
    assert (
        checked >= 3
    ), "the OpenAPI shape changed; this test no longer inspects the writes"


def test_there_is_no_intelligence_endpoint_that_executes_anything(client):
    """§62, §74. Nothing on this surface can act; the most it can do is record."""
    schema = client.get("/openapi.json").json()
    mutating = {
        f"{method.upper()} {path}"
        for path, method, _ in _iter_intelligence_operations(schema)
        if method.lower() in ("post", "put", "patch", "delete")
    }
    allowed = {
        "POST /api/v1/intelligence/knowledge/{knowledge_id}/review",
        "POST /api/v1/intelligence/recommendations/{recommendation_id}/decide",
        "POST /api/v1/intelligence/recommendations/{recommendation_id}/outcome",
        "POST /api/v1/intelligence/learning-runs",
        "PUT /api/v1/intelligence/event-hooks",
        "POST /api/v1/intelligence/sweep",
    }
    assert mutating == allowed, (
        "a mutating endpoint was added to the learning surface; §62 requires "
        f"it to be reviewed explicitly: {sorted(mutating ^ allowed)}"
    )
