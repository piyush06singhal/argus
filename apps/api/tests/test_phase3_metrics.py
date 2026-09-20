"""Phase 3 metrics & retention tests (§44, §47)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.anomaly import (
    Anomaly,
    AnomalyFingerprint,
    AnomalySeverity,
    AnomalySource,
    AnomalyStatus,
    AnomalyType,
)
from app.models.incident import Incident, IncidentSeverity, IncidentStatus
from app.models.project import Environment, SoftwareProject
from app.services.anomaly_metrics import reliability_metrics
from app.services.retention import RetentionService

NOW = datetime(2026, 9, 19, 15, 0, tzinfo=timezone.utc)


async def _seed(db: AsyncSession):
    project = SoftwareProject(name="M", slug=f"m-{uuid.uuid4().hex[:8]}")
    db.add(project)
    await db.flush()
    env = Environment(
        project_id=project.id, name="production", environment_type="PRODUCTION"
    )
    db.add(env)
    await db.flush()
    return project.id, env.id


class TestReliabilityMetrics:
    async def test_counts_and_dedup(self, db_session: AsyncSession) -> None:
        project_id, env_id = await _seed(db_session)
        for i in range(3):
            db_session.add(
                Anomaly(
                    project_id=project_id,
                    environment_id=env_id,
                    anomaly_type=AnomalyType.LATENCY_SPIKE,
                    severity=AnomalySeverity.HIGH,
                    status=(
                        AnomalyStatus.RESOLVED if i == 0 else AnomalyStatus.DETECTED
                    ),
                    source=AnomalySource.METRIC,
                    fingerprint=f"fp-{i}",
                    detected_at=NOW - timedelta(minutes=i + 1),
                    suppressed=(i == 2),
                )
            )
        # One fingerprint seen 5 times => 4 collapsed duplicates.
        db_session.add(
            AnomalyFingerprint(
                project_id=project_id,
                environment_id=env_id,
                fingerprint="fp-0",
                anomaly_type=AnomalyType.LATENCY_SPIKE,
                occurrence_count=5,
                first_seen_at=NOW - timedelta(hours=1),
                last_seen_at=NOW,
            )
        )
        await db_session.flush()

        metrics = await reliability_metrics(
            db_session, project_id=project_id, window_seconds=3600, now=NOW
        )
        assert metrics.anomalies_detected == 3
        assert metrics.anomalies_open == 2
        assert metrics.anomalies_resolved == 1
        assert metrics.anomalies_suppressed == 1
        assert metrics.anomalies_deduplicated == 4
        assert metrics.anomalies_by_severity == {"HIGH": 3}
        assert metrics.anomalies_by_type == {"LATENCY_SPIKE": 3}

    async def test_mtta_and_mttr(self, db_session: AsyncSession) -> None:
        project_id, env_id = await _seed(db_session)
        # ack after 5 min, resolved after 30 min from detection.
        db_session.add(
            Incident(
                project_id=project_id,
                environment_id=env_id,
                title="a",
                severity=IncidentSeverity.HIGH,
                status=IncidentStatus.RESOLVED,
                detected_at=NOW - timedelta(hours=2),
                acknowledged_at=NOW - timedelta(hours=2) + timedelta(minutes=5),
                resolved_at=NOW - timedelta(hours=2) + timedelta(minutes=30),
            )
        )
        # ack after 15 min, resolved after 60 min.
        db_session.add(
            Incident(
                project_id=project_id,
                environment_id=env_id,
                title="b",
                severity=IncidentSeverity.LOW,
                status=IncidentStatus.RESOLVED,
                detected_at=NOW - timedelta(hours=1),
                acknowledged_at=NOW - timedelta(hours=1) + timedelta(minutes=15),
                resolved_at=NOW - timedelta(hours=1) + timedelta(minutes=60),
            )
        )
        await db_session.flush()

        metrics = await reliability_metrics(
            db_session, project_id=project_id, window_seconds=86400, now=NOW
        )
        assert metrics.incidents_created == 2
        assert metrics.incidents_resolved == 2
        assert metrics.incidents_open == 0
        # MTTA = mean(5, 15) = 10 minutes.
        assert metrics.mtta_seconds == 600.0
        # MTTR = mean(30, 60) = 45 minutes, measured from detection.
        assert metrics.mttr_seconds == 2700.0

    async def test_window_excludes_old_rows(self, db_session: AsyncSession) -> None:
        project_id, env_id = await _seed(db_session)
        db_session.add(
            Anomaly(
                project_id=project_id,
                environment_id=env_id,
                anomaly_type=AnomalyType.ERROR_RATE_SPIKE,
                severity=AnomalySeverity.MEDIUM,
                status=AnomalyStatus.DETECTED,
                source=AnomalySource.METRIC,
                fingerprint="old",
                detected_at=NOW - timedelta(days=3),
            )
        )
        await db_session.flush()
        metrics = await reliability_metrics(
            db_session, project_id=project_id, window_seconds=3600, now=NOW
        )
        assert metrics.anomalies_detected == 0


class TestRetentionPolicy:
    async def test_phase3_tables_are_swept(self, db_session: AsyncSession) -> None:
        policy = await RetentionService(db_session).get_policy()
        assert "anomalies" in policy
        assert "incidents" in policy
        assert "incident_timeline_events" in policy
        assert policy["incidents"] >= policy["anomalies"]
