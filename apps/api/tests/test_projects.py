"""Tests for project lifecycle and API endpoints."""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient



class TestProjectCrud:
    """Test project create/read/update/delete."""

    def test_create_project(self, client: TestClient) -> None:
        """Create a project via API."""
        response = client.post(
            "/api/v1/projects",
            json={
                "name": "E-commerce Platform",
                "slug": "e-commerce-platform",
                "description": "Test e-commerce system",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "E-commerce Platform"
        assert data["slug"] == "e-commerce-platform"
        assert data["status"] == "ACTIVE"
        assert "id" in data

    def test_create_project_duplicate_slug(self, client: TestClient) -> None:
        """Creating a project with a duplicate slug should fail."""
        payload = {
            "name": "First",
            "slug": "dup-slug",
        }
        assert client.post("/api/v1/projects", json=payload).status_code == 201
        response = client.post("/api/v1/projects", json=payload)
        assert response.status_code == 409

    def test_list_projects(self, client: TestClient) -> None:
        """List all projects."""
        client.post("/api/v1/projects", json={"name": "A", "slug": "proj-a"})
        client.post("/api/v1/projects", json={"name": "B", "slug": "proj-b"})

        response = client.get("/api/v1/projects")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 2
        assert len(data["items"]) >= 2

    def test_get_project(self, client: TestClient) -> None:
        """Get a single project."""
        created = client.post(
            "/api/v1/projects", json={"name": "Solo", "slug": "solo"}
        ).json()
        response = client.get(f"/api/v1/projects/{created['id']}")
        assert response.status_code == 200
        assert response.json()["name"] == "Solo"

    def test_get_project_not_found(self, client: TestClient) -> None:
        """Get a non-existent project."""
        response = client.get(f"/api/v1/projects/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_update_project(self, client: TestClient) -> None:
        """Update a project."""
        created = client.post(
            "/api/v1/projects", json={"name": "Original", "slug": "orig"}
        ).json()
        response = client.put(
            f"/api/v1/projects/{created['id']}",
            json={"name": "Updated", "status": "PAUSED"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated"
        assert data["status"] == "PAUSED"

    def test_delete_project(self, client: TestClient) -> None:
        """Delete a project."""
        created = client.post(
            "/api/v1/projects", json={"name": "Doomed", "slug": "doomed"}
        ).json()
        response = client.delete(f"/api/v1/projects/{created['id']}")
        assert response.status_code == 204

        # Verify it's gone
        response = client.get(f"/api/v1/projects/{created['id']}")
        assert response.status_code == 404

    def test_validation_rejects_bad_slug(self, client: TestClient) -> None:
        """Reject invalid slug format."""
        response = client.post(
            "/api/v1/projects",
            json={"name": "Bad", "slug": "Bad Slug!"},
        )
        assert response.status_code == 422


class TestEnvironments:
    """Test environment creation and association."""

    def test_create_environment(self, client: TestClient) -> None:
        """Create an environment for a project."""
        project = client.post(
            "/api/v1/projects", json={"name": "Env Proj", "slug": "env-proj"}
        ).json()

        response = client.post(
            f"/api/v1/projects/{project['id']}/environments",
            json={
                "name": "Production",
                "environment_type": "PRODUCTION",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Production"
        assert data["environment_type"] == "PRODUCTION"
        assert data["project_id"] == project["id"]

    def test_list_environments(self, client: TestClient) -> None:
        """List environments for a project."""
        project = client.post(
            "/api/v1/projects", json={"name": "Env List", "slug": "env-list"}
        ).json()
        client.post(
            f"/api/v1/projects/{project['id']}/environments",
            json={"name": "Prod", "environment_type": "PRODUCTION"},
        )
        client.post(
            f"/api/v1/projects/{project['id']}/environments",
            json={"name": "Dev", "environment_type": "DEVELOPMENT"},
        )

        response = client.get(f"/api/v1/projects/{project['id']}/environments")
        assert response.status_code == 200
        assert response.json()["total"] == 2

    def test_environment_requires_valid_type(self, client: TestClient) -> None:
        """Invalid environment type should be rejected."""
        project = client.post(
            "/api/v1/projects", json={"name": "Bad Env", "slug": "bad-env"}
        ).json()
        response = client.post(
            f"/api/v1/projects/{project['id']}/environments",
            json={"name": "Invalid", "environment_type": "NOT_A_TYPE"},
        )
        assert response.status_code == 422

    def test_get_environment(self, client: TestClient) -> None:
        """Get an environment by ID."""
        project = client.post(
            "/api/v1/projects", json={"name": "Env Get", "slug": "env-get"}
        ).json()
        env = client.post(
            f"/api/v1/projects/{project['id']}/environments",
            json={"name": "Staging", "environment_type": "STAGING"},
        ).json()

        response = client.get(f"/api/v1/environments/{env['id']}")
        assert response.status_code == 200
        assert response.json()["name"] == "Staging"

    def test_environment_isolation(self, client: TestClient) -> None:
        """Environments should not leak between projects."""
        proj_a = client.post(
            "/api/v1/projects", json={"name": "Isolated A", "slug": "iso-a"}
        ).json()
        proj_b = client.post(
            "/api/v1/projects", json={"name": "Isolated B", "slug": "iso-b"}
        ).json()

        client.post(
            f"/api/v1/projects/{proj_a['id']}/environments",
            json={"name": "Prod", "environment_type": "PRODUCTION"},
        )

        response = client.get(f"/api/v1/projects/{proj_b['id']}/environments")
        assert response.status_code == 200
        assert response.json()["total"] == 0