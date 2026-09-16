"""Tests for system components and dependencies."""
from __future__ import annotations


from fastapi.testclient import TestClient


class TestComponents:
    """Test component CRUD and dependencies."""

    def _setup_project(self, client: TestClient) -> dict:
        """Create a project and return it."""
        return client.post(
            "/api/v1/projects", json={"name": "Comp Proj", "slug": "comp-proj"}
        ).json()

    def test_create_component(self, client: TestClient) -> None:
        """Create a component for a project."""
        project = self._setup_project(client)
        response = client.post(
            f"/api/v1/projects/{project['id']}/components",
            json={
                "name": "Authentication Service",
                "component_type": "SERVICE",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Authentication Service"
        assert data["component_type"] == "SERVICE"
        assert data["project_id"] == project["id"]

    def test_list_components(self, client: TestClient) -> None:
        """List components for a project."""
        project = self._setup_project(client)
        client.post(
            f"/api/v1/projects/{project['id']}/components",
            json={"name": "API A", "component_type": "SERVICE"},
        )
        client.post(
            f"/api/v1/projects/{project['id']}/components",
            json={"name": "DB", "component_type": "DATABASE"},
        )

        response = client.get(f"/api/v1/projects/{project['id']}/components")
        assert response.status_code == 200
        assert response.json()["total"] == 2

    def test_get_component(self, client: TestClient) -> None:
        """Get a component by ID."""
        project = self._setup_project(client)
        comp = client.post(
            f"/api/v1/projects/{project['id']}/components",
            json={"name": "Worker", "component_type": "WORKER"},
        ).json()

        response = client.get(f"/api/v1/components/{comp['id']}")
        assert response.status_code == 200
        assert response.json()["name"] == "Worker"

    def test_update_component(self, client: TestClient) -> None:
        """Update a component status."""
        project = self._setup_project(client)
        comp = client.post(
            f"/api/v1/projects/{project['id']}/components",
            json={"name": "API", "component_type": "SERVICE"},
        ).json()

        response = client.put(
            f"/api/v1/components/{comp['id']}",
            json={"status": "DEGRADED"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "DEGRADED"

    def test_create_dependency(self, client: TestClient) -> None:
        """Create a dependency between components."""
        project = self._setup_project(client)
        comp_a = client.post(
            f"/api/v1/projects/{project['id']}/components",
            json={"name": "Service A", "component_type": "SERVICE"},
        ).json()
        comp_b = client.post(
            f"/api/v1/projects/{project['id']}/components",
            json={"name": "Database", "component_type": "DATABASE"},
        ).json()

        response = client.post(
            f"/api/v1/projects/{project['id']}/dependencies",
            json={
                "source_component_id": comp_a["id"],
                "target_component_id": comp_b["id"],
                "dependency_type": "DATABASE",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["source_component_id"] == comp_a["id"]
        assert data["dependency_type"] == "DATABASE"

    def test_list_dependencies(self, client: TestClient) -> None:
        """List dependencies for a project."""
        project = self._setup_project(client)
        comp_a = client.post(
            f"/api/v1/projects/{project['id']}/components",
            json={"name": "Frontend", "component_type": "FRONTEND"},
        ).json()
        comp_b = client.post(
            f"/api/v1/projects/{project['id']}/components",
            json={"name": "API", "component_type": "SERVICE"},
        ).json()
        client.post(
            f"/api/v1/projects/{project['id']}/dependencies",
            json={
                "source_component_id": comp_a["id"],
                "target_component_id": comp_b["id"],
                "dependency_type": "HTTP",
            },
        )

        response = client.get(f"/api/v1/projects/{project['id']}/dependencies")
        assert response.status_code == 200
        assert response.json()["total"] == 1

    def test_system_map(self, client: TestClient) -> None:
        """Get the system map for a project."""
        project = self._setup_project(client)
        comp_a = client.post(
            f"/api/v1/projects/{project['id']}/components",
            json={"name": "API", "component_type": "SERVICE"},
        ).json()
        comp_b = client.post(
            f"/api/v1/projects/{project['id']}/components",
            json={"name": "DB", "component_type": "DATABASE"},
        ).json()
        client.post(
            f"/api/v1/projects/{project['id']}/dependencies",
            json={
                "source_component_id": comp_a["id"],
                "target_component_id": comp_b["id"],
                "dependency_type": "DATABASE",
            },
        )

        response = client.get(f"/api/v1/projects/{project['id']}/system-map")
        assert response.status_code == 200
        data = response.json()
        assert len(data["nodes"]) == 2
        assert len(data["edges"]) == 1
        assert data["edges"][0]["source"] == comp_a["id"]

    def test_component_validation(self, client: TestClient) -> None:
        """Reject invalid component type."""
        project = self._setup_project(client)
        response = client.post(
            f"/api/v1/projects/{project['id']}/components",
            json={"name": "Bad", "component_type": "NOT_A_TYPE"},
        )
        assert response.status_code == 422

    def test_components_are_project_scoped(self, client: TestClient) -> None:
        """A component must belong to a project.

        ``system_components.project_id`` is NOT NULL, so a global
        ``POST /components`` (no project scope) can never succeed and is not
        exposed. Components are created via the project-scoped route.
        """
        # Global create must not exist (404 or 405), never a 500.
        response = client.post(
            "/api/v1/components",
            json={"name": "Orphan", "component_type": "SERVICE"},
        )
        assert response.status_code in (404, 405)

        # And a health check referencing the created component is writable.
        project = self._setup_project(client)
        comp = client.post(
            f"/api/v1/projects/{project['id']}/components",
            json={"name": "API", "component_type": "SERVICE"},
        ).json()
        health = client.post(
            "/api/v1/ingestion/health-checks",
            json={
                "project_id": project["id"],
                "component_id": comp["id"],
                "timestamp": "2026-09-16T00:00:00Z",
                "status": "HEALTHY",
                "latency_ms": 3.5,
            },
        )
        assert health.status_code == 201

    