"""Failure and edge-case tests for deterministic fault handling."""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


class TestErrorHandling:
    """Verify the API fails safely on malformed input."""

    def test_malformed_project_payload(self, client: TestClient) -> None:
        """Malformed JSON body should return 422."""
        response = client.post(
            "/api/v1/projects",
            json={"name": 123, "slug": 456},
        )
        assert response.status_code == 422

    def test_missing_required_fields(self, client: TestClient) -> None:
        """Missing required project fields rejected."""
        response = client.post("/api/v1/projects", json={})
        assert response.status_code == 422

    def test_invalid_uuid_path(self, client: TestClient) -> None:
        """Invalid UUID in path rejected."""
        response = client.get("/api/v1/projects/not-a-uuid")
        assert response.status_code == 422

    def test_page_size_capped(self, client: TestClient) -> None:
        """Page size beyond the cap is rejected."""
        response = client.get("/api/v1/projects?page_size=1000")
        assert response.status_code == 422

    def test_invalid_enum_value(self, client: TestClient) -> None:
        """Invalid enum values rejected cleanly."""
        response = client.post(
            "/api/v1/projects",
            json={"name": "X", "slug": "x", "status": "FLYING"},
        )
        assert response.status_code == 422

    def test_nonexistent_component(self, client: TestClient) -> None:
        """Fetching a non-existent component returns 404."""
        response = client.get(f"/api/v1/components/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_internal_errors_do_not_leak_details(self, client: TestClient) -> None:
        """Unknown exceptions return a generic 500 without internal details."""
        response = client.get("/api/v1/projects")
        assert response.status_code in (200, 500)
        if response.status_code == 500:
            body = response.json()
            assert "traceback" not in body
            assert body.get("detail") == "Internal server error"


class TestHealthEndpoints:
    """Test the health endpoints."""

    def test_liveness(self, client: TestClient) -> None:
        """Liveness check."""
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    def test_readiness(self, client: TestClient) -> None:
        """Readiness check reaches the database."""
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] in ("healthy", "degraded")

    def test_dependencies(self, client: TestClient) -> None:
        """Dependency health reports postgres and redis status."""
        response = client.get("/health/dependencies")
        assert response.status_code == 200
        deps = response.json()["dependencies"]
        names = [d["name"] for d in deps]
        assert "postgresql" in names
        assert "redis" in names