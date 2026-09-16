"""Tests for incidents and incident evidence."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient


class TestIncidents:
    """Test incident lifecycle."""

    def _setup(self, client: TestClient) -> dict:
        project = client.post(
            "/api/v1/projects", json={"name": "Inc Proj", "slug": "inc-proj"}
        ).json()
        return {"project": project}

    def test_create_incident(self, client: TestClient) -> None:
        """Create an incident."""
        s = self._setup(client)
        response = client.post(
            "/api/v1/incidents",
            json={
                "project_id": s["project"]["id"],
                "title": "Checkout latency increase",
                "severity": "HIGH",
                "detected_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["severity"] == "HIGH"
        assert data["status"] == "OPEN"

    def test_incident_lifecycle(self, client: TestClient) -> None:
        """Incident status transitions via PUT."""
        s = self._setup(client)
        incident = client.post(
            "/api/v1/incidents",
            json={
                "project_id": s["project"]["id"],
                "title": "Latency",
                "severity": "MEDIUM",
                "detected_at": datetime.now(timezone.utc).isoformat(),
            },
        ).json()

        # Investigate
        resp = client.put(
            f"/api/v1/incidents/{incident['id']}",
            json={"status": "INVESTIGATING"},
        )
        assert resp.json()["status"] == "INVESTIGATING"

        # Resolve
        resp = client.put(
            f"/api/v1/incidents/{incident['id']}",
            json={"status": "RESOLVED"},
        )
        assert resp.json()["status"] == "RESOLVED"

    def test_list_incidents_filter(self, client: TestClient) -> None:
        """Filter incidents by severity."""
        s = self._setup(client)
        ts = datetime.now(timezone.utc).isoformat()
        client.post(
            "/api/v1/incidents",
            json={"project_id": s["project"]["id"], "title": "A", "severity": "LOW", "detected_at": ts},
        )
        client.post(
            "/api/v1/incidents",
            json={"project_id": s["project"]["id"], "title": "B", "severity": "CRITICAL", "detected_at": ts},
        )

        response = client.get("/api/v1/incidents?severity=CRITICAL")
        assert response.status_code == 200
        assert response.json()["total"] == 1

    def test_add_evidence(self, client: TestClient) -> None:
        """Attach evidence to an incident."""
        s = self._setup(client)
        incident = client.post(
            "/api/v1/incidents",
            json={
                "project_id": s["project"]["id"],
                "title": "Latency",
                "severity": "HIGH",
                "detected_at": datetime.now(timezone.utc).isoformat(),
            },
        ).json()

        response = client.post(
            f"/api/v1/incidents/{incident['id']}/evidence",
            json={
                "evidence_type": "LOG",
                "source_id": "log-123",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "relevance_score": 0.8,
                "description": "Connection pool exhausted",
            },
        )
        assert response.status_code == 201
        assert response.json()["evidence_type"] == "LOG"

        # List evidence
        response = client.get(f"/api/v1/incidents/{incident['id']}/evidence")
        assert response.status_code == 200
        assert response.json()["total"] == 1

    def test_evidence_on_missing_incident(self, client: TestClient) -> None:
        """Evidence cannot be added to a non-existent incident."""
        response = client.post(
            "/api/v1/incidents/00000000-0000-0000-0000-000000000000/evidence",
            json={
                "evidence_type": "LOG",
                "source_id": "x",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert response.status_code == 404

    def test_get_missing_incident(self, client: TestClient) -> None:
        """Fetch a non-existent incident returns 404."""
        response = client.get("/api/v1/incidents/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404