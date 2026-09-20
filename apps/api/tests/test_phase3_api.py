"""Phase 3 API tests (§35, §46).

Covers rule CRUD + validation, anomaly reads/lifecycle, suppressions,
maintenance windows, incident intelligence endpoints, the lifecycle actions,
and — importantly — server-side scoping: an out-of-scope project/environment
yields 404, never data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project(client: TestClient, slug: str) -> dict:
    return client.post(
        "/api/v1/projects", json={"name": slug.title(), "slug": slug}
    ).json()


def _environment(client: TestClient, project_id: str, name: str = "production") -> dict:
    return client.post(
        f"/api/v1/projects/{project_id}/environments",
        json={"name": name, "environment_type": "PRODUCTION"},
    ).json()


def _component(client: TestClient, project_id: str, name: str) -> dict:
    return client.post(
        f"/api/v1/projects/{project_id}/components",
        json={"name": name, "component_type": "SERVICE"},
    ).json()


def _incident(client: TestClient, project_id: str, **overrides) -> dict:
    payload = {
        "project_id": project_id,
        "title": "Checkout latency",
        "severity": "HIGH",
        "detected_at": _now(),
    }
    payload.update(overrides)
    response = client.post("/api/v1/incidents", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class TestAnomalyRules:
    def test_create_and_list_rule(self, client: TestClient) -> None:
        project = _project(client, "rule-proj")
        response = client.post(
            "/api/v1/anomaly-rules",
            json={
                "project_id": project["id"],
                "name": "Checkout P95 Latency",
                "anomaly_type": "LATENCY_SPIKE",
                "condition": "BASELINE_DEVIATION",
                "metric_name": "http.checkout.latency.p95",
                "multiplier": 2.5,
                "severity": "HIGH",
            },
        )
        assert response.status_code == 201, response.text
        rule = response.json()
        assert rule["multiplier"] == 2.5

        listing = client.get(f"/api/v1/anomaly-rules?project_id={project['id']}")
        assert listing.status_code == 200
        assert listing.json()["total"] == 1

        patch = client.patch(
            f"/api/v1/anomaly-rules/{rule['id']}", json={"enabled": False}
        )
        assert patch.status_code == 200
        assert patch.json()["enabled"] is False

    def test_unevaluable_rule_rejected(self, client: TestClient) -> None:
        """A BASELINE_DEVIATION rule with no multiplier must be refused (§18)."""
        project = _project(client, "rule-bad")
        response = client.post(
            "/api/v1/anomaly-rules",
            json={
                "project_id": project["id"],
                "name": "Bad rule",
                "anomaly_type": "LATENCY_SPIKE",
                "condition": "BASELINE_DEVIATION",
                "metric_name": "x",
                "severity": "HIGH",
            },
        )
        assert response.status_code == 422

    def test_rule_on_unknown_project_is_404(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/anomaly-rules",
            json={
                "project_id": "00000000-0000-0000-0000-000000000000",
                "name": "x",
                "anomaly_type": "LATENCY_SPIKE",
                "condition": "THRESHOLD",
                "metric_name": "x",
                "threshold": 5,
                "severity": "LOW",
            },
        )
        assert response.status_code == 404


class TestAnomalyReads:
    def test_list_and_filter_empty(self, client: TestClient) -> None:
        project = _project(client, "anom-list")
        response = client.get(f"/api/v1/anomalies?project_id={project['id']}")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 0
        assert body["items"] == []

    def test_missing_anomaly_is_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/anomalies/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404

    def test_detect_on_unknown_project_is_404(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/projects/00000000-0000-0000-0000-000000000000/anomalies/detect"
        )
        assert response.status_code == 404

    def test_detect_runs_cleanly_with_no_rules(self, client: TestClient) -> None:
        project = _project(client, "anom-detect")
        response = client.post(f"/api/v1/projects/{project['id']}/anomalies/detect")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["detection"]["rules_evaluated"] == 0
        assert body["correlation"]["clusters"] == 0


class TestSuppressionsAndWindows:
    def test_suppression_lifecycle(self, client: TestClient) -> None:
        project = _project(client, "supp-proj")
        response = client.post(
            "/api/v1/anomaly-suppressions",
            json={
                "project_id": project["id"],
                "reason": "planned database maintenance",
                "starts_at": _now(),
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["enabled"] is True

        listing = client.get(
            f"/api/v1/anomaly-suppressions?project_id={project['id']}&active_only=true"
        )
        assert listing.json()["total"] == 1

    def test_suppression_rejects_inverted_window(self, client: TestClient) -> None:
        project = _project(client, "supp-bad")
        start = datetime.now(timezone.utc)
        response = client.post(
            "/api/v1/anomaly-suppressions",
            json={
                "project_id": project["id"],
                "reason": "x",
                "starts_at": start.isoformat(),
                "ends_at": (start - timedelta(hours=1)).isoformat(),
            },
        )
        assert response.status_code == 422

    def test_maintenance_window_requires_an_effect(self, client: TestClient) -> None:
        project = _project(client, "mw-bad")
        start = datetime.now(timezone.utc)
        response = client.post(
            "/api/v1/maintenance-windows",
            json={
                "project_id": project["id"],
                "name": "noop",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat(),
                "suppress_anomalies": False,
                "downgrade_severity": False,
            },
        )
        assert response.status_code == 422

    def test_maintenance_window_create(self, client: TestClient) -> None:
        project = _project(client, "mw-ok")
        start = datetime.now(timezone.utc)
        response = client.post(
            "/api/v1/maintenance-windows",
            json={
                "project_id": project["id"],
                "name": "Production DB maintenance",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat(),
                "downgrade_severity": True,
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["downgrade_severity"] is True

    def test_suppression_is_deactivated_not_deleted(self, client: TestClient) -> None:
        """An audit trail is only meaningful if the mute can be switched off."""
        project = _project(client, "supp-off")
        created = client.post(
            "/api/v1/anomaly-suppressions",
            json={
                "project_id": project["id"],
                "reason": "planned maintenance",
                "starts_at": _now(),
            },
        ).json()
        assert (
            client.get(
                f"/api/v1/anomaly-suppressions?project_id={project['id']}&active_only=true"
            ).json()["total"]
            == 1
        )

        patched = client.patch(
            f"/api/v1/anomaly-suppressions/{created['id']}",
            json={"enabled": False},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["enabled"] is False

        # Still on file, no longer active.
        listing = client.get(f"/api/v1/anomaly-suppressions?project_id={project['id']}")
        assert listing.json()["total"] == 1
        assert (
            client.get(
                f"/api/v1/anomaly-suppressions?project_id={project['id']}&active_only=true"
            ).json()["total"]
            == 0
        )

    def test_suppression_patch_is_project_scoped(self, client: TestClient) -> None:
        owner = _project(client, "supp-owner")
        other = _project(client, "supp-other")
        created = client.post(
            "/api/v1/anomaly-suppressions",
            json={
                "project_id": owner["id"],
                "reason": "maintenance",
                "starts_at": _now(),
            },
        ).json()
        response = client.patch(
            f"/api/v1/anomaly-suppressions/{created['id']}"
            f"?project_id={other['id']}",
            json={"enabled": False},
        )
        assert response.status_code == 404

    def test_suppression_patch_rejects_ends_before_start(
        self, client: TestClient
    ) -> None:
        project = _project(client, "supp-invert")
        start = datetime.now(timezone.utc)
        created = client.post(
            "/api/v1/anomaly-suppressions",
            json={
                "project_id": project["id"],
                "reason": "maintenance",
                "starts_at": start.isoformat(),
            },
        ).json()
        response = client.patch(
            f"/api/v1/anomaly-suppressions/{created['id']}",
            json={"ends_at": (start - timedelta(hours=1)).isoformat()},
        )
        assert response.status_code == 422

    def test_maintenance_window_patch_closes_it_early(self, client: TestClient) -> None:
        project = _project(client, "mw-patch")
        start = datetime.now(timezone.utc)
        created = client.post(
            "/api/v1/maintenance-windows",
            json={
                "project_id": project["id"],
                "name": "DB maintenance",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=2)).isoformat(),
            },
        ).json()

        closed = client.patch(
            f"/api/v1/maintenance-windows/{created['id']}",
            json={"ends_at": (start + timedelta(minutes=5)).isoformat()},
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["ends_at"] < created["ends_at"]

        # The create-time invariant still holds through a patch.
        noop = client.patch(
            f"/api/v1/maintenance-windows/{created['id']}",
            json={"suppress_anomalies": False},
        )
        assert noop.status_code == 422

    def test_maintenance_window_patch_is_project_scoped(
        self, client: TestClient
    ) -> None:
        owner = _project(client, "mw-owner")
        other = _project(client, "mw-other")
        start = datetime.now(timezone.utc)
        created = client.post(
            "/api/v1/maintenance-windows",
            json={
                "project_id": owner["id"],
                "name": "DB maintenance",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat(),
            },
        ).json()
        response = client.patch(
            f"/api/v1/maintenance-windows/{created['id']}?project_id={other['id']}",
            json={"enabled": False},
        )
        assert response.status_code == 404


class TestIncidentIntelligence:
    def test_timeline_anomalies_components_graph_summary(
        self, client: TestClient
    ) -> None:
        project = _project(client, "inc-intel")
        component = _component(client, project["id"], "checkout-service")
        incident = _incident(
            client, project["id"], primary_component_id=component["id"]
        )

        timeline = client.get(f"/api/v1/incidents/{incident['id']}/timeline")
        assert timeline.status_code == 200
        assert timeline.json()["total"] == 0

        note = client.post(
            f"/api/v1/incidents/{incident['id']}/timeline",
            json={"occurred_at": _now(), "title": "Handover", "actor": "oncall"},
        )
        assert note.status_code == 201
        assert note.json()["event_type"] == "NOTE"
        assert note.json()["provenance"] == "manual"

        # Only NOTE events may be hand-authored; facts are derived.
        bad_note = client.post(
            f"/api/v1/incidents/{incident['id']}/timeline",
            json={
                "event_type": "DEPLOYMENT_OCCURRED",
                "occurred_at": _now(),
                "title": "invented",
            },
        )
        assert bad_note.status_code == 422

        anomalies = client.get(f"/api/v1/incidents/{incident['id']}/anomalies")
        assert anomalies.status_code == 200
        assert anomalies.json()["total"] == 0

        components = client.get(f"/api/v1/incidents/{incident['id']}/components")
        assert components.status_code == 200

        graph = client.get(f"/api/v1/incidents/{incident['id']}/graph")
        assert graph.status_code == 200
        assert "not implied to be causes" in graph.json()["disclaimer"]

        deployments = client.get(f"/api/v1/incidents/{incident['id']}/deployments")
        assert deployments.status_code == 200
        assert deployments.json() == []

        summary = client.get(f"/api/v1/incidents/{incident['id']}/summary")
        assert summary.status_code == 200
        body = summary.json()
        assert "does not establish" in body["text"]
        assert "checkout-service" in body["text"]

    def test_lifecycle_actions(self, client: TestClient) -> None:
        project = _project(client, "inc-life")
        incident = _incident(client, project["id"])

        ack = client.post(
            f"/api/v1/incidents/{incident['id']}/acknowledge",
            json={"actor": "oncall"},
        )
        assert ack.status_code == 200
        assert ack.json()["status"] == "ACKNOWLEDGED"
        assert ack.json()["status_changed_by"] == "oncall"

        investigate = client.post(
            f"/api/v1/incidents/{incident['id']}/investigate", json={}
        )
        assert investigate.json()["status"] == "INVESTIGATING"

        resolve = client.post(f"/api/v1/incidents/{incident['id']}/resolve", json={})
        assert resolve.json()["status"] == "RESOLVED"
        assert resolve.json()["resolved_at"] is not None

        reopen = client.post(f"/api/v1/incidents/{incident['id']}/reopen", json={})
        assert reopen.json()["status"] == "OPEN"

        timeline = client.get(f"/api/v1/incidents/{incident['id']}/timeline")
        types = [e["event_type"] for e in timeline.json()["items"]]
        assert "INCIDENT_ACKNOWLEDGED" in types
        assert "INCIDENT_RESOLVED" in types

    def test_illegal_transition_through_put_is_409(self, client: TestClient) -> None:
        project = _project(client, "inc-illegal")
        incident = _incident(client, project["id"])
        client.post(f"/api/v1/incidents/{incident['id']}/acknowledge", json={})

        # ACKNOWLEDGED -> OPEN is not a legal transition.
        response = client.put(
            f"/api/v1/incidents/{incident['id']}", json={"status": "OPEN"}
        )
        assert response.status_code == 409


class TestMetricsAndDashboard:
    def test_reliability_metrics_shape(self, client: TestClient) -> None:
        project = _project(client, "metrics-proj")
        response = client.get(f"/api/v1/projects/{project['id']}/reliability-metrics")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["anomalies_detected"] == 0
        assert body["incidents_open"] == 0
        # The MTTR definition must be reported alongside the number.
        assert "resolved_at" in body["mttr_definition"]
        assert body["mtta_seconds"] is None

    def test_dashboard_payload(self, client: TestClient) -> None:
        project = _project(client, "dash-proj")
        _incident(client, project["id"], severity="CRITICAL")
        response = client.get(
            f"/api/v1/projects/{project['id']}/incident-dashboard"
            "?window_seconds=86400&bucket_seconds=3600"
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["metrics"]["incidents_created"] == 1
        assert body["metrics"]["incidents_open"] == 1
        assert len(body["recent_incidents"]) == 1
        assert isinstance(body["anomalies_over_time"], list)
        assert body["severity_distribution"] == {}

    def test_prometheus_exposes_phase3_metrics(self, client: TestClient) -> None:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "argus_anomalies_detected_24h" in response.text
        assert "argus_incidents_open" in response.text
        assert "argus_anomalies_deduplicated" in response.text


class TestDetectionEndToEnd:
    """Rule → telemetry → detect → incident → affected components, via the API.

    This covers two bugs found only by running the live gate: the anomaly detail
    endpoint lazy-loading its relationship (500), and the affected-components
    endpoint returning one row per anomaly instead of one row per component.
    """

    def _run(self, client: TestClient):
        project = _project(client, "e2e-detect")
        environment = _environment(client, project["id"], "production")
        component = _component(client, project["id"], "checkout-service")
        db_component = _component(client, project["id"], "inventory-service")

        client.post(
            f"/api/v1/projects/{project['id']}/dependencies",
            json={
                "source_component_id": component["id"],
                "target_component_id": db_component["id"],
                "dependency_type": "HTTP",
            },
        )

        rule = client.post(
            "/api/v1/anomaly-rules",
            json={
                "project_id": project["id"],
                "environment_id": environment["id"],
                "component_id": component["id"],
                "name": "E2E checkout latency",
                "anomaly_type": "LATENCY_SPIKE",
                "condition": "THRESHOLD",
                "metric_name": "e2e.checkout.latency.p95",
                "threshold": 100,
                "expected_value": 40,
                "severity": "HIGH",
                "min_samples": 1,
                "window_seconds": 600,
                "cooldown_seconds": 0,
            },
        )
        assert rule.status_code == 201, rule.text

        metric = client.post(
            "/api/v1/observability/metrics",
            json={
                "project_id": project["id"],
                "environment_id": environment["id"],
                "component_id": component["id"],
                "timestamp": _now(),
                "metric_name": "e2e.checkout.latency.p95",
                "metric_type": "GAUGE",
                "value": 950,
                "unit": "ms",
            },
        )
        assert metric.status_code == 201, metric.text

        detect = client.post(
            f"/api/v1/projects/{project['id']}/anomalies/detect"
            f"?environment_id={environment['id']}"
        )
        assert detect.status_code == 200, detect.text
        body = detect.json()
        assert body["detection"]["anomalies_opened"] == 1
        assert body["correlation"]["incidents_created"] == 1
        return project, environment, component, db_component

    def test_anomaly_detail_returns_explanation(self, client: TestClient) -> None:
        project, _environment, _component, _db_component = self._run(client)
        listing = client.get(
            f"/api/v1/anomalies?project_id={project['id']}"
            "&metric_name=e2e.checkout.latency.p95"
        )
        assert listing.json()["total"] == 1
        anomaly_id = listing.json()["items"][0]["id"]

        detail = client.get(f"/api/v1/anomalies/{anomaly_id}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert len(body["observations"]) >= 1
        assert body["explanation"]["why_detected"]
        assert body["explanation"]["threshold_exceeded"]
        assert "not the probability" in body["explanation"]["confidence_meaning"]
        # The declared normal, not the crossed threshold.
        assert body["expected_value"] == 40
        assert body["threshold"] == 100

    def test_affected_components_are_unique_with_counts(
        self, client: TestClient
    ) -> None:
        project, _environment, component, db_component = self._run(client)
        incidents = client.get(f"/api/v1/incidents?project_id={project['id']}")
        incident_id = incidents.json()["items"][0]["id"]

        response = client.get(f"/api/v1/incidents/{incident_id}/components")
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        ids = [i["component_id"] for i in items]
        assert len(ids) == len(set(ids)), "one row per component, not per anomaly"

        by_id = {i["component_id"]: i for i in items}
        assert by_id[component["id"]]["classification"] == "DIRECTLY_OBSERVED"
        assert by_id[component["id"]]["anomaly_count"] == 1
        # The rule asked for HIGH; 950 against a ceiling of 100 escalates it.
        # The point of the assertion is that severity is *reported*, not null.
        assert by_id[component["id"]]["severity"] in ("HIGH", "CRITICAL")
        # The dependency is present as context, and clearly not as a failure.
        assert by_id[db_component["id"]]["classification"] == "DOWNSTREAM_CONTEXT"
        assert by_id[db_component["id"]]["anomaly_count"] == 0

    def test_summary_lists_the_observed_blast_radius(self, client: TestClient) -> None:
        project, _environment, component, db_component = self._run(client)
        incidents = client.get(f"/api/v1/incidents?project_id={project['id']}")
        incident_id = incidents.json()["items"][0]["id"]

        summary = client.get(f"/api/v1/incidents/{incident_id}/summary")
        assert summary.status_code == 200, summary.text
        text = summary.json()["text"]
        assert "checkout-service" in text
        assert "DOWNSTREAM_CONTEXT" in text
        assert db_component["id"] not in text  # names, not raw ids
        assert "does not establish" in text


class TestIsolation:
    def test_incident_scoped_by_project(self, client: TestClient) -> None:
        project_a = _project(client, "iso-a")
        project_b = _project(client, "iso-b")
        incident = _incident(client, project_a["id"])

        # In-scope works.
        ok = client.get(
            f"/api/v1/incidents/{incident['id']}?project_id={project_a['id']}"
        )
        assert ok.status_code == 200

        # Out-of-scope is invisible (404, not 403).
        leak = client.get(
            f"/api/v1/incidents/{incident['id']}?project_id={project_b['id']}"
        )
        assert leak.status_code == 404

        for suffix in ("timeline", "anomalies", "components", "graph", "summary"):
            resp = client.get(
                f"/api/v1/incidents/{incident['id']}/{suffix}"
                f"?project_id={project_b['id']}"
            )
            assert resp.status_code == 404, suffix

    def test_environment_scoping(self, client: TestClient) -> None:
        project_a = _project(client, "iso-env-a")
        project_b = _project(client, "iso-env-b")
        env_b = _environment(client, project_b["id"])
        incident = _incident(client, project_a["id"])

        # A foreign environment may not be used to widen access.
        resp = client.get(
            f"/api/v1/incidents/{incident['id']}"
            f"?project_id={project_a['id']}&environment_id={env_b['id']}"
        )
        assert resp.status_code == 404

    def test_evidence_component_must_share_project(self, client: TestClient) -> None:
        project_a = _project(client, "iso-ev-a")
        project_b = _project(client, "iso-ev-b")
        foreign_component = _component(client, project_b["id"], "other")
        incident = _incident(client, project_a["id"])

        response = client.post(
            f"/api/v1/incidents/{incident['id']}/evidence",
            json={
                "evidence_type": "METRIC",
                "source_id": "m-1",
                "timestamp": _now(),
                "component_id": foreign_component["id"],
            },
        )
        assert response.status_code == 404

    def test_incident_create_on_unknown_project_is_404(
        self, client: TestClient
    ) -> None:
        response = client.post(
            "/api/v1/incidents",
            json={
                "project_id": "00000000-0000-0000-0000-000000000000",
                "title": "x",
                "severity": "LOW",
                "detected_at": _now(),
            },
        )
        assert response.status_code == 404

    def test_list_anomalies_on_unknown_project_is_404(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/anomalies?project_id=00000000-0000-0000-0000-000000000000"
        )
        assert response.status_code == 404
