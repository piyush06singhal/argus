"""Phase 5 API tests (§44–§51).

The API is two surfaces in one: a **planning** surface that must never execute
anything, and a **reading** surface that must explain an experiment completely.
Both are tested here against an experiment the engine actually ran, so the
responses describe real rows rather than fixtures.

Execution itself is deliberately not triggered through HTTP in these tests: the
endpoint enqueues a job, and a test has no worker. That path is asserted for its
*refusals* (no confirmation, already terminal, queue unreachable) because those
are the ones that decide whether a plan can run by accident.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.incident import Incident
from app.models.reproduction import ReproductionExperiment
from app.services.reproduction_orchestrator import ReproductionOrchestrator
from test_phase4_demo import _analyse_checkout

settings = get_settings()


async def _planned_experiment(db_session: AsyncSession, db_engine):
    """A real, planned experiment for a real incident — nothing executed."""
    project, _env, _components, analysis, candidates = await _analyse_checkout(
        db_session
    )
    await db_session.commit()
    incident = await db_session.get(Incident, analysis.incident_id)
    assert incident is not None
    orchestrator = ReproductionOrchestrator(
        async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    )
    experiment = await orchestrator.prepare(
        incident=incident,
        candidate=candidates[0],
        analysis_id=analysis.id,
        trigger="pytest",
    )
    return orchestrator, project, incident, experiment


class TestPlanningSurface:
    async def test_planning_creates_a_plan_and_executes_nothing(
        self, client, db_session, db_engine
    ) -> None:
        _orchestrator, project, incident, _experiment = await _planned_experiment(
            db_session, db_engine
        )
        response = client.post(
            f"/api/v1/incidents/{incident.id}/reproductions",
            params={"project_id": str(project.id)},
            json={},
        )
        # 201: a plan *creates* an experiment record, and creates nothing else.
        # Planning and running are separate calls so a plan is always something an
        # engineer reviewed first.
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["experiment"]["status"] == "PLANNED"
        assert body["experiment"]["result"] == "NOT_RUN"
        # A plan is a proposal: no sandbox and no run may exist yet.
        assert body["sandbox"] is None
        assert body["runs"] == []
        plan = body["plan"]
        assert plan["target_component_name"] == "Inventory Database"
        assert "datastore" in plan["required_services"]
        assert plan["safety_constraints"]["production_access"] == "BLOCKED"
        assert plan["repetitions"] >= 1
        assert plan["network_policy"] == "ISOLATED"
        assert body["hypothesis"]["expected_sequence"]
        assert "not a proof" in body["disclaimer"]

    async def test_the_plan_endpoint_explains_the_experiment(
        self, client, db_session, db_engine
    ) -> None:
        _orchestrator, project, _incident, experiment = await _planned_experiment(
            db_session, db_engine
        )
        response = client.get(
            f"/api/v1/reproductions/{experiment.id}/plan",
            params={"project_id": str(project.id)},
        )
        assert response.status_code == 200
        plan = response.json()
        assert plan["target_component_name"]
        assert plan["objectives"]["hypothesis"]
        assert plan["objectives"]["guardrail"]
        assert plan["expected_behavior"]["components"]
        assert plan["safety_constraints"]["arbitrary_commands"].startswith("REJECTED")
        assert plan["resource_limits"]["memory_mb"] > 0

    async def test_an_explicit_fault_spec_is_accepted_and_planned(
        self, client, db_session, db_engine
    ) -> None:
        """Regression: a well-formed fault spec must plan, not 500.

        ``BaseSchema`` sets ``use_enum_values=True``, so a request model returns
        the plain value (``"LATENCY"``) rather than an enum member. The planner
        override builder unwrapped it with ``.value`` and raised
        ``AttributeError`` — a 500 on the *supported* path of §21, reachable by
        any client that supplies the fault it wants tested.
        """
        _orchestrator, project, incident, _experiment = await _planned_experiment(
            db_session, db_engine
        )
        response = client.post(
            f"/api/v1/incidents/{incident.id}/reproductions",
            params={"project_id": str(project.id)},
            json={
                "repetitions": 3,
                "faults": [
                    {
                        "fault_type": "HTTP_5XX",
                        "target": "datastore",
                        "intensity": 0.25,
                    }
                ],
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["experiment"]["status"] == "PLANNED"
        assert body["plan"]["repetitions"] == 3
        # The caller's fault is what gets stored — not a default.
        faults = body["faults"]
        assert len(faults) == 1
        assert faults[0]["fault_type"] == "HTTP_5XX"
        assert faults[0]["target"] == "datastore"
        assert faults[0]["intensity"] == 0.25
        assert faults[0]["status"] == "PLANNED"
        # A planned fault has not run yet, and the API must not imply it has.
        assert faults[0]["injected"] is False
        # The strategy follows the fault: an injected dependency failure is a
        # dependency-fault experiment, and the plan says so.
        assert body["plan"]["strategy"] == "DEPENDENCY_FAULT"

    async def test_an_undrivable_fault_trigger_is_refused(
        self, client, db_session, db_engine
    ) -> None:
        """A trigger nothing inside a sandbox can fire must be rejected."""
        _orchestrator, project, incident, _experiment = await _planned_experiment(
            db_session, db_engine
        )
        response = client.post(
            f"/api/v1/incidents/{incident.id}/reproductions",
            params={"project_id": str(project.id)},
            json={
                "faults": [
                    {
                        "fault_type": "LATENCY",
                        "target": "datastore",
                        "trigger": "MANUAL",
                    }
                ]
            },
        )
        assert response.status_code == 422, response.text
        assert "trigger" in response.text.lower()

    async def test_the_safety_preview_shows_what_will_happen_before_it_does(
        self, client, db_session, db_engine
    ) -> None:
        _orchestrator, project, _incident, experiment = await _planned_experiment(
            db_session, db_engine
        )
        response = client.get(
            f"/api/v1/reproductions/{experiment.id}/safety",
            params={"project_id": str(project.id)},
        )
        assert response.status_code == 200
        safety = response.json()
        assert safety["sandbox"].startswith("argus-repro-")
        assert safety["production_access"] == "BLOCKED"
        assert safety["credentials"] == "SANITIZED"
        assert safety["network_policy"] == "ISOLATED"
        assert safety["timeout_seconds"] > 0
        assert safety["can_start"] is True

    async def test_the_manifest_is_sufficient_to_understand_the_run(
        self, client, db_session, db_engine
    ) -> None:
        _orchestrator, project, _incident, experiment = await _planned_experiment(
            db_session, db_engine
        )
        response = client.get(
            f"/api/v1/reproductions/{experiment.id}/manifest",
            params={"project_id": str(project.id)},
        )
        assert response.status_code == 200
        manifest = response.json()
        for key in ("services", "inputs", "faults", "repetitions", "network_policy"):
            assert key in manifest, key
        assert manifest["inputs"][0]["target"].startswith("checkout")
        assert manifest["inputs"][0]["payload_hash"]

    async def test_the_inputs_are_the_sanitized_ones_that_will_be_replayed(
        self, client, db_session, db_engine
    ) -> None:
        _orchestrator, project, _incident, experiment = await _planned_experiment(
            db_session, db_engine
        )
        response = client.get(
            f"/api/v1/reproductions/{experiment.id}/inputs",
            params={"project_id": str(project.id)},
        )
        assert response.status_code == 200
        items = response.json()
        assert items
        for item in items:
            assert item["target_service"] in {"checkout", "inventory", "datastore"}
            assert item["method"] in {"GET", "POST", "PUT", "PATCH", "DELETE"}
            assert item["payload_hash"]


class TestExecutionRefusals:
    async def test_start_requires_an_explicit_confirmation(
        self, client, db_session, db_engine
    ) -> None:
        _orchestrator, project, _incident, experiment = await _planned_experiment(
            db_session, db_engine
        )
        # §47: execution is never implicit. An empty body cannot start a run.
        refused = client.post(
            f"/api/v1/reproductions/{experiment.id}/start",
            params={"project_id": str(project.id)},
            json={},
        )
        assert refused.status_code == 422
        explicitly_false = client.post(
            f"/api/v1/reproductions/{experiment.id}/start",
            params={"project_id": str(project.id)},
            json={"confirm_sandbox": False},
        )
        assert explicitly_false.status_code == 422

    async def test_start_on_a_finished_experiment_is_refused(
        self, client, db_session, db_engine
    ) -> None:
        orchestrator, project, _incident, experiment = await _planned_experiment(
            db_session, db_engine
        )
        await orchestrator.run_experiment(experiment.id)
        response = client.post(
            f"/api/v1/reproductions/{experiment.id}/start",
            params={"project_id": str(project.id)},
            json={"confirm_sandbox": True},
        )
        assert response.status_code == 409
        assert "already" in response.json()["detail"].lower()

    async def test_start_without_a_queue_reports_that_nothing_ran(
        self, client, db_session, db_engine
    ) -> None:
        _orchestrator, project, _incident, experiment = await _planned_experiment(
            db_session, db_engine
        )
        response = client.post(
            f"/api/v1/reproductions/{experiment.id}/start",
            params={"project_id": str(project.id)},
            json={"confirm_sandbox": True},
        )
        # Either the queue accepted the job (202) or the API says plainly that
        # nothing ran (503). The one outcome that must never happen is a success
        # response that started nothing.
        assert response.status_code in (202, 503), response.text
        if response.status_code == 503:
            assert "not started" in response.json()["detail"].lower()
        else:
            assert response.json()["experiment"]["status"] in {
                "VALIDATING",
                "PROVISIONING",
            }

    async def test_cancel_on_an_unknown_experiment_is_a_404(self, client) -> None:
        response = client.post(
            f"/api/v1/reproductions/{uuid.uuid4()}/cancel",
            params={"project_id": str(uuid.uuid4())},
            json={},
        )
        assert response.status_code == 404


class TestReadingSurface:
    async def test_every_documented_endpoint_answers_for_a_finished_experiment(
        self, client, db_session, db_engine
    ) -> None:
        orchestrator, project, incident, experiment = await _planned_experiment(
            db_session, db_engine
        )
        finished = await orchestrator.run_experiment(experiment.id)
        await db_session.commit()
        params = {"project_id": str(project.id)}

        detail = client.get(f"/api/v1/reproductions/{experiment.id}", params=params)
        assert detail.status_code == 200, detail.text
        assert detail.json()["experiment"]["status"] == finished.status.value
        assert detail.json()["runs"], "a finished experiment must expose its runs"

        status = client.get(
            f"/api/v1/reproductions/{experiment.id}/status", params=params
        )
        assert status.status_code == 200
        progress = status.json()["progress"]
        assert progress["percent"] == 100
        assert progress["step"] == progress["total_steps"]
        assert progress["total_steps"] > 1, "the live page needs real progress steps"

        telemetry = client.get(
            f"/api/v1/reproductions/{experiment.id}/telemetry", params=params
        )
        assert telemetry.status_code == 200
        assert telemetry.json()["namespace"] == f"repro:{experiment.id}"

        artifacts = client.get(
            f"/api/v1/reproductions/{experiment.id}/artifacts", params=params
        )
        assert artifacts.status_code == 200
        assert artifacts.json()["items"]

        comparison = client.get(
            f"/api/v1/reproductions/{experiment.id}/comparison", params=params
        )
        assert comparison.status_code == 200
        items = comparison.json()["items"]
        assert items and items[0]["dimensions"]
        assert items[0]["formula_reference"]

        validation = client.get(
            f"/api/v1/reproductions/{experiment.id}/validation", params=params
        )
        assert validation.status_code == 200
        body = validation.json()
        assert body["outcome"] in {
            "SUPPORTED",
            "PARTIALLY_SUPPORTED",
            "NOT_SUPPORTED",
            "INCONCLUSIVE",
        }
        assert body["summary"]
        assert body["determinism"]["runs"] >= 1

        environment = client.get(
            f"/api/v1/reproductions/{experiment.id}/environment", params=params
        )
        assert environment.status_code == 200
        assert len(environment.json()) >= 2

        faults = client.get(
            f"/api/v1/reproductions/{experiment.id}/faults", params=params
        )
        assert faults.status_code == 200
        assert faults.json()["items"]

    async def test_history_lists_the_experiment_for_its_incident(
        self, client, db_session, db_engine
    ) -> None:
        orchestrator, project, incident, experiment = await _planned_experiment(
            db_session, db_engine
        )
        await orchestrator.run_experiment(experiment.id)
        await db_session.commit()
        response = client.get(
            f"/api/v1/incidents/{incident.id}/reproductions",
            params={"project_id": str(project.id)},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] >= 1
        entry = body["items"][0]
        for key in ("experiment_id", "result", "status", "created_at"):
            assert key in entry, key

    async def test_a_retry_creates_a_new_version_of_the_same_hypothesis(
        self, client, db_session, db_engine
    ) -> None:
        orchestrator, project, _incident, experiment = await _planned_experiment(
            db_session, db_engine
        )
        await orchestrator.run_experiment(experiment.id)
        await db_session.commit()
        response = client.post(
            f"/api/v1/reproductions/{experiment.id}/retry",
            params={"project_id": str(project.id)},
            json={},
        )
        # 201: a retry *creates* a new experiment rather than mutating the old one.
        assert response.status_code in (200, 201), response.text
        retry = response.json()["experiment"]
        assert retry["id"] != str(experiment.id)
        assert retry["experiment_version"] == 2
        # The retry tests the same hypothesis: the history stays comparable.
        assert retry["candidate_id"] == str(experiment.candidate_id)
        # A retry is a plan, not a re-run of the stored rows.
        assert retry["result"] == "NOT_RUN"

    async def test_the_project_list_is_scoped_and_paginated(
        self, client, db_session, db_engine
    ) -> None:
        orchestrator, project, _incident, experiment = await _planned_experiment(
            db_session, db_engine
        )
        await orchestrator.run_experiment(experiment.id)
        await db_session.commit()
        response = client.get(
            "/api/v1/reproductions",
            params={"project_id": str(project.id), "page_size": 5},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] >= 1
        assert body["page_size"] == 5
        assert all(item["project_id"] == str(project.id) for item in body["items"])

    async def test_reproduction_is_refused_when_the_feature_is_disabled(
        self, client, db_session, db_engine, monkeypatch
    ) -> None:
        from app.api.v1.routes import reproduction as routes

        monkeypatch.setattr(
            routes.settings, "REPRODUCTION_ENABLED", False, raising=False
        )
        _orchestrator, project, incident, _experiment = await _planned_experiment(
            db_session, db_engine
        )
        response = client.post(
            f"/api/v1/incidents/{incident.id}/reproductions",
            params={"project_id": str(project.id)},
            json={},
        )
        assert response.status_code == 409
        assert "disabled" in response.json()["detail"].lower()
        # Existing experiments stay queryable while the feature is off.
        monkeypatch.undo()
        experiment = await db_session.scalar(
            __import__("sqlalchemy").select(ReproductionExperiment.id)
        )
        assert experiment is not None


class TestPerformance:
    """§63 — the decisions that run per experiment must not be the slow part.

    Provisioning a sandbox is measured in the sandbox suite (under 30s). What is
    checked here is that planning, aliasing and comparison — the work done on
    every single experiment, and again on every retry — stay analytic.
    """

    def test_planning_and_aliasing_are_analytic(self) -> None:
        import time

        from app.services.reproduction_planner import ReproductionPlanner
        from app.services.reproduction_sandbox import load_template
        from test_phase5_engines import _source

        planner = ReproductionPlanner(template="demo_commerce")
        template = load_template("demo_commerce")
        source = _source()
        started = time.monotonic()
        for _ in range(200):
            planner.derive_inputs(
                template=template, target_service="datastore", entry_service="checkout"
            )
            planner.alias_map(source=source, template=template)
        elapsed = time.monotonic() - started
        assert (
            elapsed < 1.0
        ), f"planning is too slow to be per-experiment work: {elapsed:.2f}s"

    def test_comparison_throughput(self) -> None:
        import time

        from app.services.reproduction_comparator import ReproductionComparator
        from test_phase5_engines import _capture, _signal, _source

        comparator = ReproductionComparator()
        source = _source()
        capture = _capture(
            [
                _signal("datastore", error=True, duration=2500),
                _signal("inventory", error=True, duration=2600),
                _signal("checkout", error=True, duration=2700),
            ]
        )
        started = time.monotonic()
        for _ in range(100):
            comparator.compare(source=source, capture=capture)
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"comparison is too slow for a live page: {elapsed:.2f}s"
