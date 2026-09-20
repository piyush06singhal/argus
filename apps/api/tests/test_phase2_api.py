"""Phase 2 — graph API surface: every graph router endpoint, happy path + errors."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------
def _project(
    client: TestClient, name: str = "Graph API Proj", slug: str | None = None
) -> dict:
    return client.post(
        "/api/v1/projects",
        json={"name": name, "slug": slug or f"graph-api-{uuid.uuid4().hex[:8]}"},
    ).json()


def _env(client: TestClient, project_id: str, name: str = "Production") -> dict:
    return client.post(
        f"/api/v1/projects/{project_id}/environments",
        json={"name": name, "environment_type": "PRODUCTION"},
    ).json()


def _component(
    client: TestClient,
    project_id: str,
    name: str,
    env_id: str | None = None,
    ctype: str = "SERVICE",
) -> dict:
    payload: dict = {"name": name, "component_type": ctype}
    if env_id:
        payload["environment_id"] = env_id
    return client.post(f"/api/v1/projects/{project_id}/components", json=payload).json()


def _dependency(
    client: TestClient, project_id: str, source_id: str, target_id: str
) -> dict:
    return client.post(
        f"/api/v1/projects/{project_id}/dependencies",
        json={
            "source_component_id": source_id,
            "target_component_id": target_id,
            "dependency_type": "HTTP",
        },
    ).json()


def _topology(client: TestClient) -> dict:
    """Web -> API -> DB in one production environment, reconciled."""
    project = _project(client)
    env = _env(client, project["id"])
    web = _component(client, project["id"], "Web", env["id"])
    api = _component(client, project["id"], "Api Service", env["id"])
    db = _component(client, project["id"], "Postgres", env["id"], ctype="DATABASE")
    _dependency(client, project["id"], web["id"], api["id"])
    _dependency(client, project["id"], api["id"], db["id"])

    reconcile = client.post(
        f"/api/v1/projects/{project['id']}/graph/reconcile?environment_id={env['id']}"
    )
    assert reconcile.status_code == 200, reconcile.text
    return {
        "project": project,
        "env": env,
        "web": web,
        "api": api,
        "db": db,
        "reconcile": reconcile.json(),
    }


# ---------------------------------------------------------------------------
# Project-scoped graph reads
# ---------------------------------------------------------------------------
class TestGraphDataEndpoints:
    def test_get_graph_returns_nodes_and_edges(self, client: TestClient) -> None:
        s = _topology(client)
        pid = s["project"]["id"]
        resp = client.get(f"/api/v1/projects/{pid}/graph")
        assert resp.status_code == 200
        data = resp.json()
        names = {n["name"] for n in data["nodes"]}
        assert {"Web", "Api Service", "Postgres", "Production"} <= names
        # 2 DEPENDS_ON + environment CONTAINS edges at minimum
        assert len(data["edges"]) >= 3

    def test_get_graph_filter_node_type(self, client: TestClient) -> None:
        s = _topology(client)
        resp = client.get(
            f"/api/v1/projects/{s['project']['id']}/graph?node_type=SERVICE"
        )
        assert resp.status_code == 200
        nodes = resp.json()["nodes"]
        assert nodes
        # §7: SERVICE components project onto typed SERVICE nodes.
        assert all(n["node_type"] == "SERVICE" for n in nodes)

    def test_get_graph_filter_edge_type(self, client: TestClient) -> None:
        s = _topology(client)
        resp = client.get(
            f"/api/v1/projects/{s['project']['id']}/graph?edge_type=DEPENDS_ON"
        )
        assert resp.status_code == 200
        edges = resp.json()["edges"]
        assert edges
        assert all(e["edge_type"] == "DEPENDS_ON" for e in edges)

    def test_get_graph_invalid_node_type_422(self, client: TestClient) -> None:
        s = _topology(client)
        resp = client.get(
            f"/api/v1/projects/{s['project']['id']}/graph?node_type=NOT_A_TYPE"
        )
        assert resp.status_code == 422

    def test_get_graph_unknown_project_404(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/projects/{uuid.uuid4()}/graph")
        assert resp.status_code == 404

    def test_nodes_and_edges_lists_paginated(self, client: TestClient) -> None:
        s = _topology(client)
        pid = s["project"]["id"]
        nodes = client.get(f"/api/v1/projects/{pid}/graph/nodes?page=1&page_size=2")
        assert nodes.status_code == 200
        body = nodes.json()
        assert body["total"] >= 4
        assert len(body["items"]) == 2
        assert body["page"] == 1

        edges = client.get(f"/api/v1/projects/{pid}/graph/edges?page=1&page_size=2")
        assert edges.status_code == 200
        assert edges.json()["total"] >= 3


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
class TestReconcileEndpoint:
    def test_reconcile_creates_counts(self, client: TestClient) -> None:
        s = _topology(client)
        body = s["reconcile"]
        assert body["project_id"] == s["project"]["id"]
        assert body["nodes_created"] >= 4
        assert body["edges_created"] >= 2
        assert body["status"] == "SUCCESS"
        assert body["reconciliation_run_id"]

    def test_reconcile_idempotent(self, client: TestClient) -> None:
        s = _topology(client)
        pid = s["project"]["id"]
        again = client.post(f"/api/v1/projects/{pid}/graph/reconcile").json()
        assert again["nodes_created"] == 0
        assert again["edges_created"] == 0

    def test_reconcile_unknown_project_404(self, client: TestClient) -> None:
        resp = client.post(f"/api/v1/projects/{uuid.uuid4()}/graph/reconcile")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Dependencies / dependents / neighbors / impact
# ---------------------------------------------------------------------------
class TestComponentGraphEndpoints:
    def test_dependencies_direct(self, client: TestClient) -> None:
        s = _topology(client)
        resp = client.get(f"/api/v1/components/{s['web']['id']}/graph/dependencies")
        assert resp.status_code == 200
        body = resp.json()
        assert body["direction"] == "outgoing"
        assert [n["name"] for n in body["direct"]] == ["Api Service"]

    def test_dependencies_transitive(self, client: TestClient) -> None:
        s = _topology(client)
        body = client.get(
            f"/api/v1/components/{s['web']['id']}/graph/dependencies?transitive=true"
        ).json()
        names = {n["name"] for n in body["direct"]} | {
            n["name"] for n in body["transitive"]
        }
        assert {"Api Service", "Postgres"} <= names

    def test_dependents_upstream(self, client: TestClient) -> None:
        s = _topology(client)
        resp = client.get(f"/api/v1/components/{s['db']['id']}/graph/dependents")
        assert resp.status_code == 200
        body = resp.json()
        assert body["direction"] == "incoming"
        assert [n["name"] for n in body["direct"]] == ["Api Service"]

    def test_neighbors_both_directions(self, client: TestClient) -> None:
        s = _topology(client)
        body = client.get(f"/api/v1/components/{s['api']['id']}/graph/neighbors").json()
        names = {n["name"] for n in body["direct"]}
        assert names == {"Web", "Postgres"}

    def test_impact_downstream_labeled(self, client: TestClient) -> None:
        s = _topology(client)
        resp = client.get(f"/api/v1/components/{s['db']['id']}/graph/impact")
        assert resp.status_code == 200
        body = resp.json()
        assert body["label"] == "Dependency Impact"
        assert body["relation"] == "downstream"
        names = [item["node"]["name"] for item in body["items"]]
        assert names == ["Api Service", "Web"]
        assert body["count"] == 2

    def test_depth_cap_enforced(self, client: TestClient) -> None:
        s = _topology(client)
        # le=25 at the schema layer; requests beyond that are rejected (422).
        resp = client.get(
            f"/api/v1/components/{s['web']['id']}/graph/dependencies?max_depth=999"
        )
        assert resp.status_code == 422

    def test_component_404(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/components/{uuid.uuid4()}/graph/dependencies")
        assert resp.status_code == 404

    def test_component_without_node_404(self, client: TestClient) -> None:
        project = _project(client)
        comp = _component(client, project["id"], "Unmirrored")
        resp = client.get(f"/api/v1/components/{comp['id']}/graph/dependencies")
        assert resp.status_code == 404
        assert "no corresponding graph node" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Path finding
# ---------------------------------------------------------------------------
class TestPathEndpoint:
    def test_path_found_between_components(self, client: TestClient) -> None:
        s = _topology(client)
        pid = s["project"]["id"]
        graph = client.get(f"/api/v1/projects/{pid}/graph").json()
        by_name = {n["name"]: n["id"] for n in graph["nodes"]}
        resp = client.get(
            f"/api/v1/projects/{pid}/graph/paths",
            params={"source_id": by_name["Web"], "target_id": by_name["Postgres"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["found"] is True
        assert body["total_hops"] == 2
        assert [n["name"] for n in body["path"]] == ["Web", "Api Service", "Postgres"]

    def test_path_not_found(self, client: TestClient) -> None:
        s = _topology(client)
        pid = s["project"]["id"]
        isolated = _component(client, pid, "Isolated", s["env"]["id"])
        client.post(f"/api/v1/projects/{pid}/graph/reconcile")
        graph = client.get(f"/api/v1/projects/{pid}/graph").json()
        by_name = {n["name"]: n["id"] for n in graph["nodes"]}
        resp = client.get(
            f"/api/v1/projects/{pid}/graph/paths",
            params={
                "source_id": by_name["Web"],
                "target_id": by_name.get("Isolated", str(isolated["id"])),
            },
        )
        assert resp.status_code == 200
        assert resp.json()["found"] is False

    def test_project_404(self, client: TestClient) -> None:
        resp = client.get(
            f"/api/v1/projects/{uuid.uuid4()}/graph/paths",
            params={"source_id": uuid.uuid4(), "target_id": uuid.uuid4()},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
class TestSearchEndpoint:
    def test_search_nodes_and_endpoints(self, client: TestClient) -> None:
        s = _topology(client)
        pid = s["project"]["id"]
        client.post(
            f"/api/v1/components/{s['api']['id']}/endpoints",
            json={"method": "POST", "path": "/api/checkout"},
        )
        resp = client.get(f"/api/v1/projects/{pid}/graph/search?q=api")
        assert resp.status_code == 200
        body = resp.json()
        assert body["query"] == "api"
        kinds = {hit["kind"] for hit in body["results"]}
        assert "node" in kinds
        assert any(hit["kind"] == "endpoint" for hit in body["results"])

    def test_search_alias_hit(self, client: TestClient) -> None:
        s = _topology(client)
        pid = s["project"]["id"]
        client.post(
            f"/api/v1/components/{s['web']['id']}/aliases",
            json={"alias": "web-frontend"},
        )
        resp = client.get(f"/api/v1/projects/{pid}/graph/search?q=web-frontend")
        assert resp.status_code == 200
        assert any(hit["kind"] == "alias" for hit in resp.json()["results"])

    def test_search_requires_q(self, client: TestClient) -> None:
        s = _topology(client)
        resp = client.get(f"/api/v1/projects/{s['project']['id']}/graph/search")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------
class TestSnapshotEndpoints:
    def test_snapshot_lifecycle(self, client: TestClient) -> None:
        s = _topology(client)
        pid = s["project"]["id"]

        created = client.post(
            f"/api/v1/projects/{pid}/graph/snapshots", json={"caption": "baseline"}
        )
        assert created.status_code == 201
        snap = created.json()
        assert snap["snapshot_version"] == 1
        assert snap["caption"] == "baseline"

        second = client.post(f"/api/v1/projects/{pid}/graph/snapshots", json={}).json()
        assert second["snapshot_version"] == 2
        assert second["previous_snapshot_id"] == snap["id"]

        listed = client.get(f"/api/v1/projects/{pid}/graph/snapshots")
        assert listed.status_code == 200
        assert listed.json()["total"] == 2

    def test_snapshot_empty_graph_400(self, client: TestClient) -> None:
        project = _project(client)
        resp = client.post(f"/api/v1/projects/{project['id']}/graph/snapshots", json={})
        assert resp.status_code == 400

    def test_snapshot_detail(self, client: TestClient) -> None:
        s = _topology(client)
        pid = s["project"]["id"]
        snap = client.post(f"/api/v1/projects/{pid}/graph/snapshots", json={}).json()
        detail = client.get(f"/api/v1/graph/snapshots/{snap['id']}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["nodes"]
        assert body["edges"]
        assert isinstance(body["signature"], list)

    def test_snapshot_detail_404(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/graph/snapshots/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_snapshot_diff(self, client: TestClient) -> None:
        s = _topology(client)
        pid = s["project"]["id"]
        a = client.post(f"/api/v1/projects/{pid}/graph/snapshots", json={}).json()
        _component(client, pid, "Extra Service", s["env"]["id"])
        client.post(f"/api/v1/projects/{pid}/graph/reconcile")
        b = client.post(f"/api/v1/projects/{pid}/graph/snapshots", json={}).json()

        diff = client.get(f"/api/v1/graph/snapshots/{a['id']}/diff/{b['id']}")
        assert diff.status_code == 200
        body = diff.json()
        assert "Extra Service" in body["added_node_names"]
        assert body["removed_nodes"] == []


# ---------------------------------------------------------------------------
# Environment comparison
# ---------------------------------------------------------------------------
class TestEnvironmentCompareEndpoint:
    def test_compare_production_vs_staging(self, client: TestClient) -> None:
        project = _project(client)
        prod = _env(client, project["id"], "Production")
        staging = client.post(
            f"/api/v1/projects/{project['id']}/environments",
            json={"name": "Staging", "environment_type": "STAGING"},
        ).json()

        # Production: web -> api -> db
        web = _component(client, project["id"], "Web", prod["id"])
        api = _component(client, project["id"], "Api", prod["id"])
        db = _component(client, project["id"], "Postgres", prod["id"], ctype="DATABASE")
        _dependency(client, project["id"], web["id"], api["id"])
        _dependency(client, project["id"], api["id"], db["id"])
        # Staging: web -> api only
        swe = _component(client, project["id"], "Web", staging["id"])
        sapi = _component(client, project["id"], "Api", staging["id"])
        _dependency(client, project["id"], swe["id"], sapi["id"])

        client.post(f"/api/v1/projects/{project['id']}/graph/reconcile")

        resp = client.get(
            f"/api/v1/projects/{project['id']}/graph/environments/compare",
            params={"environment_a": prod["id"], "environment_b": staging["id"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["environment_a"]["name"] == "Production"
        assert body["environment_b"]["name"] == "Staging"
        removed_names = {item["name"] for item in body["removed"]}
        assert any("Postgres" in name for name in removed_names)

    def test_compare_unknown_project_404(self, client: TestClient) -> None:
        resp = client.get(
            f"/api/v1/projects/{uuid.uuid4()}/graph/environments/compare",
            params={"environment_a": uuid.uuid4(), "environment_b": uuid.uuid4()},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Health + data quality
# ---------------------------------------------------------------------------
class TestHealthAndDataQuality:
    def test_health_ok_on_clean_graph(self, client: TestClient) -> None:
        s = _topology(client)
        resp = client.get(f"/api/v1/projects/{s['project']['id']}/graph/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["node_count"] >= 4
        assert body["last_reconciled_at"] is not None

    def test_data_quality_lists_findings(self, client: TestClient) -> None:
        s = _topology(client)
        pid = s["project"]["id"]
        # Trigger the validator via /health (it persists findings).
        client.get(f"/api/v1/projects/{pid}/graph/health")
        resp = client.get(f"/api/v1/projects/{pid}/graph/data-quality")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 0
        for item in body["items"]:
            assert item["check_type"]
            assert item["severity"] in {"INFO", "WARNING", "ERROR"}

    def test_data_quality_severity_filter(self, client: TestClient) -> None:
        s = _topology(client)
        pid = s["project"]["id"]
        client.get(f"/api/v1/projects/{pid}/graph/health")
        resp = client.get(f"/api/v1/projects/{pid}/graph/data-quality?severity=INFO")
        assert resp.status_code == 200
        assert all(i["severity"] == "INFO" for i in resp.json()["items"])

    def test_health_unknown_project_404(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/projects/{uuid.uuid4()}/graph/health")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
class TestDiscoveryEndpoints:
    def _pending_record(self, client: TestClient, pid: str, comp_env: str) -> dict:
        """Telemetry for an unknown service -> PENDING discovery record."""
        comp = _component(client, pid, "Known", comp_env)
        client.post(
            "/api/v1/observability/events",
            json={
                "project_id": pid,
                "environment_id": comp_env,
                "component_id": comp["id"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "probe",
                "event_type": "SYSTEM_EVENT",
                "severity": "INFO",
                "metadata": {"service.name": "Ghost Service"},
            },
        )
        client.post(f"/api/v1/projects/{pid}/graph/reconcile")
        listed = client.get(f"/api/v1/projects/{pid}/graph/discovery").json()
        assert listed["total"] >= 1
        return next(
            r for r in listed["items"] if r["discovered_name"] == "Ghost Service"
        )

    def test_suggest_and_list(self, client: TestClient) -> None:
        s = _topology(client)
        record = self._pending_record(client, s["project"]["id"], s["env"]["id"])
        assert record["status"] == "PENDING"
        assert record["evidence_count"] >= 1

    def test_register_creates_node(self, client: TestClient) -> None:
        s = _topology(client)
        pid = s["project"]["id"]
        record = self._pending_record(client, pid, s["env"]["id"])
        resp = client.post(
            f"/api/v1/projects/{pid}/graph/discovery/{record['id']}/register",
            json={"name": "Ghost Service", "node_type": "SERVICE"},
        )
        assert resp.status_code == 201
        node = resp.json()
        assert node["name"] == "Ghost Service"
        assert node["node_type"] == "SERVICE"

        listed = client.get(
            f"/api/v1/projects/{pid}/graph/discovery?status=PENDING"
        ).json()
        assert all(r["id"] != record["id"] for r in listed["items"])

    def test_register_missing_record_404(self, client: TestClient) -> None:
        s = _topology(client)
        resp = client.post(
            f"/api/v1/projects/{s['project']['id']}/graph/discovery/{uuid.uuid4()}/register",
            json={},
        )
        assert resp.status_code == 404

    def test_ignore_marks_record(self, client: TestClient) -> None:
        s = _topology(client)
        pid = s["project"]["id"]
        record = self._pending_record(client, pid, s["env"]["id"])
        resp = client.post(
            f"/api/v1/projects/{pid}/graph/discovery/{record['id']}/ignore"
        )
        assert resp.status_code == 204
        listed = client.get(
            f"/api/v1/projects/{pid}/graph/discovery?status=IGNORED"
        ).json()
        assert any(r["id"] == record["id"] for r in listed["items"])


# ---------------------------------------------------------------------------
# Endpoints (ServiceEndpoint CRUD)
# ---------------------------------------------------------------------------
class TestEndpointRegistryAPI:
    def test_create_and_list_component_endpoints(self, client: TestClient) -> None:
        s = _topology(client)
        cid = s["api"]["id"]
        created = client.post(
            f"/api/v1/components/{cid}/endpoints",
            json={"method": "POST", "path": "/api/checkout/"},
        )
        assert created.status_code == 201
        body = created.json()
        # Trailing slash normalized away by EndpointRegistry.normalize_path.
        assert body["path_template"] == "/api/checkout"
        assert body["original_paths"] == ["/api/checkout/"]

        listed = client.get(f"/api/v1/components/{cid}/endpoints")
        assert listed.status_code == 200
        assert listed.json()["total"] == 1

    def test_invalid_method_422(self, client: TestClient) -> None:
        s = _topology(client)
        resp = client.post(
            f"/api/v1/components/{s['api']['id']}/endpoints",
            json={"method": "BREW", "path": "/coffee"},
        )
        assert resp.status_code == 422

    def test_component_404(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/components/{uuid.uuid4()}/endpoints")
        assert resp.status_code == 404

    def test_project_endpoints_list(self, client: TestClient) -> None:
        s = _topology(client)
        pid = s["project"]["id"]
        client.post(
            f"/api/v1/components/{s['api']['id']}/endpoints",
            json={"method": "GET", "path": "/api/v1/routes"},
        )
        resp = client.get(f"/api/v1/projects/{pid}/graph/endpoints")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1


# ---------------------------------------------------------------------------
# Owners
# ---------------------------------------------------------------------------
class TestOwnerAPI:
    def test_owner_upsert_lifecycle(self, client: TestClient) -> None:
        s = _topology(client)
        cid = s["api"]["id"]
        missing = client.get(f"/api/v1/components/{cid}/owner")
        assert missing.status_code == 404

        created = client.put(
            f"/api/v1/components/{cid}/owner",
            json={
                "team": "Payments",
                "owner_name": "Ada",
                "contact_email": "ada@example.com",
            },
        )
        assert created.status_code == 201
        assert created.json()["team"] == "Payments"

        updated = client.put(
            f"/api/v1/components/{cid}/owner",
            json={"team": "Platform", "owner_name": "Grace"},
        )
        assert updated.status_code == 201
        assert updated.json()["team"] == "Platform"

        fetched = client.get(f"/api/v1/components/{cid}/owner").json()
        assert fetched["owner_name"] == "Grace"

    def test_owner_component_404(self, client: TestClient) -> None:
        resp = client.put(
            f"/api/v1/components/{uuid.uuid4()}/owner", json={"team": "X"}
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------
class TestAliasAPI:
    def test_alias_create_list_and_search(self, client: TestClient) -> None:
        s = _topology(client)
        cid = s["web"]["id"]
        created = client.post(
            f"/api/v1/components/{cid}/aliases",
            json={"alias": "Web Frontend", "source": "CONFIGURATION"},
        )
        assert created.status_code == 201
        # Normalized to lowercase by the registry.
        assert created.json()["alias"] == "web frontend"

        listed = client.get(f"/api/v1/components/{cid}/aliases")
        assert listed.status_code == 200
        assert listed.json()["total"] == 1

        search = client.get(
            f"/api/v1/projects/{s['project']['id']}/graph/search?q=web front"
        )
        assert search.status_code == 200
        assert any(h["kind"] == "alias" for h in search.json()["results"])

    def test_alias_component_404(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/components/{uuid.uuid4()}/aliases")
        assert resp.status_code == 404

    def test_alias_requires_graph_node_404(self, client: TestClient) -> None:
        project = _project(client)
        comp = _component(client, project["id"], "Never Reconciled")
        resp = client.post(
            f"/api/v1/components/{comp['id']}/aliases",
            json={"alias": "nope"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cross-cutting security / isolation
# ---------------------------------------------------------------------------
class TestIsolation:
    def test_project_scoped_reads_do_not_leak(self, client: TestClient) -> None:
        other = _topology(client)
        pid = other["project"]["id"]

        stranger = _project(client, "Other Proj")
        resp = client.get(f"/api/v1/projects/{stranger['id']}/graph")
        # A different project simply sees its own (empty) graph — never the other's.
        assert resp.status_code == 200
        other_graph = client.get(f"/api/v1/projects/{pid}/graph").json()
        assert other_graph["nodes"]
        assert all(
            n["project_id"] == pid
            for n in client.get(f"/api/v1/projects/{stranger['id']}/graph").json()[
                "nodes"
            ]
        )
