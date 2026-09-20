"""Phase 3 — anomaly & incident model invariants.

Covers the Phase 3 schema added in Increment 1: enum vocabulary, unique
constraints, relationship cascades, and the structural boundaries that keep
Phase 3 honest (suppression is recorded, never silent; anomalies survive an
incident delete because grouping is not ownership).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.anomaly import (
    Anomaly,
    AnomalyBaseline,
    AnomalyFingerprint,
    AnomalyObservation,
    AnomalyRule,
    AnomalySeverity,
    AnomalySource,
    AnomalyStatus,
    AnomalySuppression,
    AnomalyType,
    BaselineStrategy,
    MaintenanceWindow,
    RuleCondition,
)
from app.models.base import BaseModel
from app.models.incident import (
    EvidenceType,
    Incident,
    IncidentEvidence,
    IncidentSeverity,
    IncidentStatus,
    IncidentTimelineEvent,
    TimelineEventType,
)
from app.models.project import Environment, SoftwareProject
from app.models.system import SystemComponent


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _seed(db_session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a project + environment + one component."""
    project = SoftwareProject(name="Phase3 Proj", slug=f"phase3-{uuid.uuid4().hex[:8]}")
    db_session.add(project)
    await db_session.flush()
    env = Environment(
        project_id=project.id, name="production", environment_type="PRODUCTION"
    )
    component = SystemComponent(
        project_id=project.id, component_type="SERVICE", name="Checkout"
    )
    db_session.add_all([env, component])
    await db_session.flush()
    return project.id, env.id, component.id


class TestSchema:
    """The Phase 3 tables exist and carry the expected columns."""

    def test_phase3_tables_registered(self) -> None:
        tables = set(BaseModel.metadata.tables)
        for name in (
            "anomaly_rules",
            "anomaly_baselines",
            "anomaly_fingerprints",
            "anomalies",
            "anomaly_observations",
            "anomaly_suppressions",
            "maintenance_windows",
            "incident_timeline_events",
        ):
            assert name in tables

    def test_incident_extended_columns(self) -> None:
        cols = set(Incident.__table__.columns.keys())
        for name in (
            "fingerprint",
            "summary",
            "primary_component_id",
            "correlation_rationale",
            "acknowledged_at",
            "status_changed_by",
        ):
            assert name in cols

    def test_evidence_extended_columns(self) -> None:
        cols = set(IncidentEvidence.__table__.columns.keys())
        for name in (
            "component_id",
            "observed_value",
            "expected_value",
            "severity",
            "confidence",
            "provenance",
            "relevance_reason",
            "anomaly_id",
        ):
            assert name in cols


class TestEnums:
    """Enum vocabulary, including the values added to existing enums."""

    def test_anomaly_types(self) -> None:
        assert AnomalyType.LATENCY_SPIKE.value == "LATENCY_SPIKE"
        assert AnomalyType.TRACE_FAILURE_SPIKE.value == "TRACE_FAILURE_SPIKE"
        assert (
            AnomalyType.DEPLOYMENT_RELATED_CHANGE.value == "DEPLOYMENT_RELATED_CHANGE"
        )

    def test_severities_and_statuses(self) -> None:
        assert [s.value for s in AnomalySeverity] == [
            "LOW",
            "MEDIUM",
            "HIGH",
            "CRITICAL",
        ]
        assert AnomalyStatus.DETECTED.value == "DETECTED"
        assert AnomalyStatus.EXPIRED.value == "EXPIRED"

    def test_sources_and_conditions(self) -> None:
        assert AnomalySource.METRIC.value == "METRIC"
        assert BaselineStrategy.ROLLING.value == "ROLLING"
        assert RuleCondition.BASELINE_DEVIATION.value == "BASELINE_DEVIATION"

    def test_incident_status_gains_acknowledged(self) -> None:
        assert IncidentStatus.ACKNOWLEDGED.value == "ACKNOWLEDGED"
        # Existing values keep their names — no silent enum renames.
        assert IncidentStatus.OPEN.value == "OPEN"
        assert IncidentStatus.CLOSED.value == "CLOSED"

    def test_evidence_type_additions(self) -> None:
        assert EvidenceType.SPAN.value == "SPAN"
        assert EvidenceType.HEALTH_CHECK.value == "HEALTH_CHECK"
        assert EvidenceType.GRAPH.value == "GRAPH"
        assert EvidenceType.ANOMALY.value == "ANOMALY"

    def test_timeline_event_types(self) -> None:
        assert TimelineEventType.ANOMALY_DETECTED.value == "ANOMALY_DETECTED"
        assert TimelineEventType.DEPLOYMENT_OCCURRED.value == "DEPLOYMENT_OCCURRED"


class TestAnomalyRule:
    """Rule persistence + validation-scope uniqueness."""

    async def test_rule_roundtrip(self, db_session: AsyncSession) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        rule = AnomalyRule(
            project_id=project_id,
            environment_id=env_id,
            component_id=component_id,
            name="Checkout P95 Latency",
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            condition=RuleCondition.BASELINE_DEVIATION,
            metric_name="http.checkout.latency.p95",
            threshold=2.5,
            window_seconds=300,
            severity=AnomalySeverity.HIGH,
        )
        db_session.add(rule)
        await db_session.flush()

        assert rule.id is not None
        assert rule.enabled is True
        assert rule.min_samples == 5
        assert rule.cooldown_seconds == 300
        assert rule.baseline_strategy == BaselineStrategy.ROLLING

    async def test_rule_name_unique_per_project(self, db_session: AsyncSession) -> None:
        project_id, _, _ = await _seed(db_session)
        for _ in range(2):
            db_session.add(
                AnomalyRule(
                    project_id=project_id,
                    name="dup-rule",
                    anomaly_type=AnomalyType.ERROR_RATE_SPIKE,
                    condition=RuleCondition.ERROR_RATE,
                    severity=AnomalySeverity.MEDIUM,
                )
            )
        with pytest.raises(IntegrityError):
            await db_session.flush()


class TestBaseline:
    async def test_baseline_roundtrip(self, db_session: AsyncSession) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        baseline = AnomalyBaseline(
            project_id=project_id,
            environment_id=env_id,
            component_id=component_id,
            metric_name="http.checkout.latency.p95",
            strategy=BaselineStrategy.ROLLING,
            window_seconds=300,
            sample_count=42,
            mean=220.0,
            median=215.0,
            stddev=18.5,
            min_value=190.0,
            max_value=260.0,
            p50=215.0,
            p95=248.0,
            p99=259.0,
            expected_value=220.0,
            computed_at=_now(),
        )
        db_session.add(baseline)
        await db_session.flush()
        assert baseline.id is not None
        assert baseline.sample_count == 42


class TestAnomaly:
    async def test_anomaly_roundtrip(self, db_session: AsyncSession) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        anomaly = Anomaly(
            project_id=project_id,
            environment_id=env_id,
            component_id=component_id,
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            severity=AnomalySeverity.HIGH,
            fingerprint="fp-latency-1",
            detected_at=_now(),
            observed_value=890.0,
            expected_value=220.0,
            deviation=3.045,
        )
        db_session.add(anomaly)
        await db_session.flush()

        assert anomaly.status == AnomalyStatus.DETECTED
        assert anomaly.observation_count == 1
        assert anomaly.suppressed is False
        assert anomaly.incident_id is None

    async def test_observations_cascade_with_anomaly(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        anomaly = Anomaly(
            project_id=project_id,
            environment_id=env_id,
            component_id=component_id,
            anomaly_type=AnomalyType.ERROR_RATE_SPIKE,
            severity=AnomalySeverity.MEDIUM,
            fingerprint="fp-err-1",
            detected_at=_now(),
        )
        db_session.add(anomaly)
        await db_session.flush()

        for i in range(3):
            db_session.add(
                AnomalyObservation(
                    anomaly_id=anomaly.id,
                    project_id=project_id,
                    environment_id=env_id,
                    component_id=component_id,
                    observed_at=_now() + timedelta(seconds=i),
                    observed_value=float(i),
                )
            )
        await db_session.flush()

        count = await db_session.scalar(
            select(func.count())
            .select_from(AnomalyObservation)
            .where(AnomalyObservation.anomaly_id == anomaly.id)
        )
        assert count == 3

        await db_session.delete(anomaly)
        await db_session.flush()
        remaining = await db_session.scalar(
            select(func.count()).select_from(AnomalyObservation)
        )
        assert remaining == 0

    async def test_suppression_is_recorded_not_discarded(
        self, db_session: AsyncSession
    ) -> None:
        """A suppressed anomaly still exists — flagged, never hidden (§42)."""
        project_id, env_id, component_id = await _seed(db_session)
        suppression = AnomalySuppression(
            project_id=project_id,
            environment_id=env_id,
            component_id=component_id,
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            reason="maintenance window",
            starts_at=_now() - timedelta(minutes=5),
        )
        db_session.add(suppression)
        await db_session.flush()

        anomaly = Anomaly(
            project_id=project_id,
            environment_id=env_id,
            component_id=component_id,
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            severity=AnomalySeverity.LOW,
            fingerprint="fp-suppressed",
            detected_at=_now(),
            suppressed=True,
            suppression_rule_id=suppression.id,
            suppression_reason="maintenance window",
            suppressed_at=_now(),
        )
        db_session.add(anomaly)
        await db_session.flush()

        fetched = await db_session.get(Anomaly, anomaly.id)
        assert fetched is not None
        assert fetched.suppressed is True
        assert fetched.suppression_reason == "maintenance window"


class TestFingerprintRegistry:
    async def test_fingerprint_unique_per_project(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, _ = await _seed(db_session)
        base = {
            "project_id": project_id,
            "environment_id": env_id,
            "fingerprint": "checkout|production|LATENCY_SPIKE",
            "anomaly_type": AnomalyType.LATENCY_SPIKE,
            "first_seen_at": _now(),
            "last_seen_at": _now(),
        }
        db_session.add(AnomalyFingerprint(**base))
        await db_session.flush()

        db_session.add(AnomalyFingerprint(**base))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_same_fingerprint_allowed_in_other_project(
        self, db_session: AsyncSession
    ) -> None:
        """Dedup is project-scoped — projects never collide."""
        project_a, env_a, _ = await _seed(db_session)
        project_b = SoftwareProject(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
        db_session.add(project_b)
        await db_session.flush()

        for project_id in (project_a, project_b.id):
            db_session.add(
                AnomalyFingerprint(
                    project_id=project_id,
                    fingerprint="same|fingerprint",
                    anomaly_type=AnomalyType.LATENCY_SPIKE,
                    first_seen_at=_now(),
                    last_seen_at=_now(),
                )
            )
        await db_session.flush()
        total = await db_session.scalar(
            select(func.count()).select_from(AnomalyFingerprint)
        )
        assert total == 2


class TestIncidentTimeline:
    async def test_timeline_ordered_and_cascades(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        incident = Incident(
            project_id=project_id,
            environment_id=env_id,
            title="Checkout latency incident",
            severity=IncidentSeverity.HIGH,
            status=IncidentStatus.OPEN,
            detected_at=_now(),
        )
        db_session.add(incident)
        await db_session.flush()

        now = _now()
        # Insert out of order to prove the relationship sorts by occurred_at.
        for offset, event_type in (
            (2, TimelineEventType.HEALTH_CHANGED),
            (0, TimelineEventType.INCIDENT_CREATED),
            (1, TimelineEventType.ANOMALY_DETECTED),
        ):
            db_session.add(
                IncidentTimelineEvent(
                    incident_id=incident.id,
                    project_id=project_id,
                    environment_id=env_id,
                    event_type=event_type,
                    occurred_at=now + timedelta(minutes=offset),
                    title=event_type.value,
                )
            )
        await db_session.flush()
        await db_session.refresh(incident, ["timeline"])

        assert [e.event_type for e in incident.timeline] == [
            TimelineEventType.INCIDENT_CREATED,
            TimelineEventType.ANOMALY_DETECTED,
            TimelineEventType.HEALTH_CHANGED,
        ]
        assert all(e.is_context_only is False for e in incident.timeline)

        await db_session.delete(incident)
        await db_session.flush()
        remaining = await db_session.scalar(
            select(func.count()).select_from(IncidentTimelineEvent)
        )
        assert remaining == 0

    async def test_context_only_flag_marks_temporal_context(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, _ = await _seed(db_session)
        incident = Incident(
            project_id=project_id,
            title="ctx",
            severity=IncidentSeverity.LOW,
            detected_at=_now(),
        )
        db_session.add(incident)
        await db_session.flush()

        db_session.add(
            IncidentTimelineEvent(
                incident_id=incident.id,
                project_id=project_id,
                environment_id=env_id,
                event_type=TimelineEventType.DEPLOYMENT_OCCURRED,
                occurred_at=_now(),
                title="Deployment 2 minutes before first anomaly",
                is_context_only=True,
            )
        )
        await db_session.flush()
        event = await db_session.scalar(select(IncidentTimelineEvent))
        assert event is not None
        assert event.is_context_only is True


class TestIncidentGroupingBoundary:
    async def test_anomaly_survives_incident_delete(
        self, db_session: AsyncSession
    ) -> None:
        """Grouping is not ownership: deleting an incident must not destroy
        the anomaly evidence it merely grouped."""
        project_id, env_id, component_id = await _seed(db_session)
        incident = Incident(
            project_id=project_id,
            environment_id=env_id,
            title="grouped",
            severity=IncidentSeverity.MEDIUM,
            detected_at=_now(),
        )
        db_session.add(incident)
        await db_session.flush()

        anomaly = Anomaly(
            project_id=project_id,
            environment_id=env_id,
            component_id=component_id,
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            severity=AnomalySeverity.MEDIUM,
            fingerprint="fp-grouped",
            detected_at=_now(),
            incident_id=incident.id,
        )
        db_session.add(anomaly)
        await db_session.flush()

        await db_session.delete(incident)
        await db_session.flush()

        surviving = await db_session.get(Anomaly, anomaly.id)
        assert surviving is not None
        assert surviving.incident_id is None


class TestMaintenanceWindow:
    async def test_window_roundtrip(self, db_session: AsyncSession) -> None:
        project_id, env_id, _ = await _seed(db_session)
        window = MaintenanceWindow(
            project_id=project_id,
            environment_id=env_id,
            name="DB maintenance",
            starts_at=_now(),
            ends_at=_now() + timedelta(hours=1),
        )
        db_session.add(window)
        await db_session.flush()
        assert window.suppress_anomalies is True
        assert window.downgrade_severity is False
        assert window.enabled is True
