"""Tests for deployments."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient


class TestDeployments:
    """Test deployment ingestion and association."""

    def _setup(self, client: TestClient) -> dict:
        project = client.post(
            "/api/v1/projects", json={"name": "Deploy Proj", "slug": "deploy-proj"}
        ).json()
        comp = client.post(
            f"/api/v1/projects/{project['id']}/components",
            json={"name": "Inventory", "component_type": "SERVICE"},
        ).json()
        return {"project": project, "comp": comp}

    def test_create_deployment(self, client: TestClient) -> None:
        """Create a deployment."""
        s = self._setup(client)
        response = client.post(
            "/api/v1/deployments",
            json={
                "project_id": s["project"]["id"],
                "component_id": s["comp"]["id"],
                "deployment_id": "deploy-001",
                "version": "1.2.3",
                "commit_sha": "abc1234",
                "deployed_at": datetime.now(timezone.utc).isoformat(),
                "status": "SUCCESS",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "SUCCESS"
        assert data["version"] == "1.2.3"

    def test_list_project_deployments(self, client: TestClient) -> None:
        """List deployments for a project."""
        s = self._setup(client)
        ts = datetime.now(timezone.utc).isoformat()
        client.post(
            "/api/v1/deployments",
            json={
                "project_id": s["project"]["id"],
                "component_id": s["comp"]["id"],
                "deployment_id": "deploy-001",
                "deployed_at": ts,
            },
        )
        client.post(
            "/api/v1/deployments",
            json={
                "project_id": s["project"]["id"],
                "component_id": s["comp"]["id"],
                "deployment_id": "deploy-002",
                "deployed_at": ts,
            },
        )

        response = client.get(f"/api/v1/projects/{s['project']['id']}/deployments")
        assert response.status_code == 200
        assert response.json()["total"] == 2

    def test_update_deployment_status(self, client: TestClient) -> None:
        """Update deployment status to FAILED/ROLLED_BACK."""
        s = self._setup(client)
        deployment = client.post(
            "/api/v1/deployments",
            json={
                "project_id": s["project"]["id"],
                "deployment_id": "deploy-003",
                "deployed_at": datetime.now(timezone.utc).isoformat(),
            },
        ).json()

        response = client.put(
            f"/api/v1/deployments/{deployment['id']}",
            json={"status": "FAILED"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "FAILED"

    def test_deployment_requires_deployment_id(self, client: TestClient) -> None:
        """Deployment without a deployment_id is invalid."""
        s = self._setup(client)
        response = client.post(
            "/api/v1/deployments",
            json={
                "project_id": s["project"]["id"],
                "deployed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert response.status_code == 422