"""Phase 4 API tests (§34, §37, §43, §45, §47).

Covers the causal-analysis surface end to end through the HTTP layer:

* ``POST /analyze`` — runs, is idempotent, and re-runs on demand;
* reads — analysis detail, root causes, graph, chain, hypotheses, evidence
  analysis, edge explanations, version history;
* isolation — an out-of-scope incident or analysis is a 404, never data;
* the honest outcomes — an evidence-free incident returns ``INSUFFICIENT`` and
  no primary root cause rather than inventing one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _project(client: TestClient, slug: str) -> dict:
    return client.post(
        "/api/v1/projects", json={"name": slug.title(), "slug": slug}
    ).json()


def _environment(client: TestClient, project_id: str, name: str = "production") -> dict:
    return client.post(
        f"/api/v1/projects/{project_id}/environments",
        json={"name": name, "environment_type": "PRODUCTION"},
    ).json()


def _component(
    client: TestClient, project_id: str, name: str, kind: str = "SERVICE"
) -> dict:
    return client.post(
        f"/api/v1/projects/{project_id}/components",
        json={"name": name, "component_type": kind},
    ).json()


def _incident(client: TestClient, project_id: str, **overrides) -> dict:
    payload = {
        "project_id": project_id,
        "title": "Checkout latency",
        "severity": "HIGH",
        "detected_at": _now().isoformat(),
    }
    payload.update(overrides)
    response = client.post("/api/v1/incidents", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _analyze(client: TestClient, project_id: str, incident_id: str, **params) -> dict:
    response = client.post(
        f"/api/v1/incidents/{incident_id}/analyze",
        params={"project_id": project_id, **params},
        json={"trigger": "test", "requested_by": "pytest"},
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestAnalyzeEndpoint:
    def test_analyze_runs_and_is_idempotent(self, client: TestClient) -> None:
        project = _project(client, "causal-analyze")
        incident = _incident(client, project["id"])

        first = _analyze(client, project["id"], incident["id"])
        assert first["reused"] is False
        assert first["analysis_version"] == 1
        assert first["status"] == "COMPLETED"

        second = _analyze(client, project["id"], incident["id"])
        # Unchanged evidence returns the stored version instead of a duplicate.
        assert second["reused"] is True
        assert second["analysis_version"] == 1

        forced = _analyze(client, project["id"], incident["id"], force="true")
        assert forced["reused"] is False
        assert forced["analysis_version"] == 2

    def test_analysis_of_an_evidence_free_incident_is_honest(
        self, client: TestClient
    ) -> None:
        """No evidence must produce UNKNOWN, never a fabricated cause."""
        project = _project(client, "causal-empty")
        incident = _incident(client, project["id"])
        _analyze(client, project["id"], incident["id"])

        detail = client.get(
            f"/api/v1/incidents/{incident['id']}/causal-analysis",
            params={"project_id": project["id"]},
        ).json()
        assert detail["overall_confidence"] == "INSUFFICIENT"
        assert detail["primary_candidate_id"] is None
        assert "insufficient evidence" in detail["summary"].lower()
        assert detail["missing_evidence"]

        chain = client.get(
            f"/api/v1/incidents/{incident['id']}/causal-chain",
            params={"project_id": project["id"]},
        ).json()
        assert chain["chain"] == []
        assert chain["valid"] is False
        assert chain["validation_notes"]

    def test_reads_require_an_analysis_first(self, client: TestClient) -> None:
        project = _project(client, "causal-missing")
        incident = _incident(client, project["id"])
        response = client.get(
            f"/api/v1/incidents/{incident['id']}/causal-analysis",
            params={"project_id": project["id"]},
        )
        assert response.status_code == 404
        assert "analyze" in response.json()["detail"].lower()


class TestCausalReads:
    def _analysed(self, client: TestClient, slug: str) -> tuple[dict, dict, dict]:
        project = _project(client, slug)
        environment = _environment(client, project["id"])
        checkout = _component(client, project["id"], "Checkout Service")
        inventory = _component(client, project["id"], "Inventory Service")
        database = _component(client, project["id"], "Inventory DB", "DATABASE")
        client.post(
            f"/api/v1/projects/{project['id']}/components/dependencies",
            json={
                "source_component_id": checkout["id"],
                "target_component_id": inventory["id"],
                "dependency_type": "HTTP",
            },
        )
        client.post(
            f"/api/v1/projects/{project['id']}/components/dependencies",
            json={
                "source_component_id": inventory["id"],
                "target_component_id": database["id"],
                "dependency_type": "DATABASE",
            },
        )
        incident = _incident(
            client,
            project["id"],
            environment_id=environment["id"],
            detected_at=(_now() - timedelta(minutes=5)).isoformat(),
        )
        # A deployment *before* onset and a metric spike on the datastore: the
        # analysis has real stored facts to reason about.
        client.post(
            "/api/v1/deployments",
            json={
                "project_id": project["id"],
                "environment_id": environment["id"],
                "component_id": database["id"],
                "deployment_id": "deploy-db-1",
                "version": "1.2.3",
                "deployed_at": (_now() - timedelta(minutes=8)).isoformat(),
            },
        )
        for minutes_ago, value in ((4, 40.0), (3, 300.0), (2, 640.0)):
            client.post(
                "/api/v1/observability/metrics",
                json={
                    "project_id": project["id"],
                    "environment_id": environment["id"],
                    "component_id": database["id"],
                    "metric_name": "db.query.latency.p95",
                    "metric_type": "GAUGE",
                    "value": value,
                    "timestamp": (_now() - timedelta(minutes=minutes_ago)).isoformat(),
                },
            )
        return project, incident, database

    def test_analysis_detail_and_root_causes(self, client: TestClient) -> None:
        project, incident, _database = self._analysed(client, "causal-reads")
        _analyze(client, project["id"], incident["id"])

        detail = client.get(
            f"/api/v1/incidents/{incident['id']}/causal-analysis",
            params={"project_id": project["id"]},
        ).json()
        assert detail["incident_id"] == incident["id"]
        assert detail["analysis_version"] == 1
        assert "status" in detail
        assert isinstance(detail["candidates"], list)
        assert isinstance(detail["relationships"], list)
        assert isinstance(detail["evidence"], list)

        causes = client.get(
            f"/api/v1/incidents/{incident['id']}/root-causes",
            params={"project_id": project["id"]},
        ).json()
        assert causes["total"] == len(causes["items"])
        if causes["items"]:
            scores = [item["score"] for item in causes["items"]]
            assert scores == sorted(scores, reverse=True)

    def test_every_returned_edge_can_be_explained(self, client: TestClient) -> None:
        """§41: the evidence inspector must work for every edge the API returns."""
        project, incident, _database = self._analysed(client, "causal-edges")
        _analyze(client, project["id"], incident["id"])
        graph = client.get(
            f"/api/v1/incidents/{incident['id']}/causal-graph",
            params={"project_id": project["id"]},
        ).json()
        assert graph["disclaimer"]
        for edge in graph["edges"]:
            explanation = client.get(
                f"/api/v1/incidents/{incident['id']}/relationships/"
                f"{edge['id']}/explanation",
                params={"project_id": project["id"]},
            )
            assert explanation.status_code == 200, explanation.text
            body = explanation.json()
            assert body["directional"] == (
                body["relationship_type"] != "CORRELATES_WITH"
            )
            assert body["explanation"]
            if body["relationship_type"] == "CORRELATES_WITH":
                assert any(
                    "co-occurrence" in caveat.lower() for caveat in body["caveats"]
                )

    def test_hypotheses_and_evidence_analysis_split_evidence(
        self, client: TestClient
    ) -> None:
        project, incident, _database = self._analysed(client, "causal-evidence")
        _analyze(client, project["id"], incident["id"])
        hypotheses = client.get(
            f"/api/v1/incidents/{incident['id']}/hypotheses",
            params={"project_id": project["id"]},
        ).json()
        evidence = client.get(
            f"/api/v1/incidents/{incident['id']}/evidence-analysis",
            params={"project_id": project["id"]},
        ).json()
        assert hypotheses["analysis_id"] == evidence["analysis_id"]
        for item in evidence["candidates"]:
            split = (
                len(item["supporting"])
                + len(item["contradicting"])
                + len(item["neutral"])
            )
            assert split == (
                item["candidate"]["supporting_evidence_count"]
                + item["candidate"]["contradicting_evidence_count"]
                + item["candidate"]["neutral_evidence_count"]
            ), "the evidence split must reconcile with the candidate's counts"
            assert item["why_confidence_differs"]

    def test_explanation_is_structured_and_cites_stored_facts(
        self, client: TestClient
    ) -> None:
        project, incident, _database = self._analysed(client, "causal-explain")
        analysis = _analyze(client, project["id"], incident["id"])
        response = client.get(
            f"/api/v1/incidents/{incident['id']}/causal-analysis/"
            f"{analysis['analysis_id']}/explanation",
            params={"project_id": project["id"]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["disclaimer"]
        assert body["headline"]
        assert body["narrative"]
        assert body["limitations"]
        if body["primary"] is not None:
            assert body["primary"]["candidate_id"]
            assert body["primary"]["score_breakdown"]
            assert body["primary"]["why_this_confidence"]
            # Reasons cite evidence categories, never bare numbers.
            for reason in body["primary"]["reasons"]:
                assert reason.startswith("[")

    def test_history_records_versions_with_diffs(self, client: TestClient) -> None:
        project, incident, _database = self._analysed(client, "causal-history")
        _analyze(client, project["id"], incident["id"])
        _analyze(client, project["id"], incident["id"], force="true")

        history = client.get(
            f"/api/v1/incidents/{incident['id']}/causal-analysis/history",
            params={"project_id": project["id"]},
        ).json()
        assert history["total"] == 2
        versions = [item["analysis_version"] for item in history["items"]]
        assert versions == [2, 1]
        assert history["items"][0]["diff"] is not None
        assert history["items"][1]["diff"] is None


class TestCausalIsolation:
    def test_out_of_scope_project_is_404(self, client: TestClient) -> None:
        owner = _project(client, "causal-owner")
        other = _project(client, "causal-other")
        incident = _incident(client, owner["id"])
        _analyze(client, owner["id"], incident["id"])

        forbidden = client.get(
            f"/api/v1/incidents/{incident['id']}/causal-analysis",
            params={"project_id": other["id"]},
        )
        assert forbidden.status_code == 404

    def test_analyze_is_rejected_out_of_scope(self, client: TestClient) -> None:
        owner = _project(client, "causal-owner2")
        other = _project(client, "causal-other2")
        incident = _incident(client, owner["id"])
        response = client.post(
            f"/api/v1/incidents/{incident['id']}/analyze",
            params={"project_id": other["id"]},
        )
        assert response.status_code == 404

    def test_unknown_analysis_id_is_404_not_data(self, client: TestClient) -> None:
        project = _project(client, "causal-unknown")
        incident = _incident(client, project["id"])
        _analyze(client, project["id"], incident["id"])
        response = client.get(
            f"/api/v1/incidents/{incident['id']}/causal-analysis",
            params={
                "project_id": project["id"],
                "analysis_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        assert response.status_code == 404

    def test_analysis_of_another_incident_is_404(self, client: TestClient) -> None:
        project = _project(client, "causal-cross")
        first = _incident(client, project["id"], title="First")
        second = _incident(client, project["id"], title="Second")
        analysis = _analyze(client, project["id"], first["id"])
        response = client.get(
            f"/api/v1/incidents/{second['id']}/causal-analysis",
            params={
                "project_id": project["id"],
                "analysis_id": analysis["analysis_id"],
            },
        )
        assert response.status_code == 404

    def test_unknown_relationship_is_404(self, client: TestClient) -> None:
        project = _project(client, "causal-rel")
        incident = _incident(client, project["id"])
        _analyze(client, project["id"], incident["id"])
        response = client.get(
            f"/api/v1/incidents/{incident['id']}/relationships/"
            "00000000-0000-0000-0000-000000000000/explanation",
            params={"project_id": project["id"]},
        )
        assert response.status_code == 404
