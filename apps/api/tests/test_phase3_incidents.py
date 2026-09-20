"""Phase 3 — incident manager tests (§25–§33)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.anomaly import (
    Anomaly,
    AnomalySeverity,
    AnomalySource,
    AnomalyStatus,
    AnomalyType,
)
from app.models.deployment import DeploymentEvent
from app.models.incident import (
    EvidenceType,
    Incident,
    IncidentEvidence,
    IncidentStatus,
    IncidentTimelineEvent,
    TimelineEventType,
)
from app.models.ingestion import ConfigurationChangeEvent
from app.models.project import Environment, SoftwareProject
from app.models.system import ComponentDependency, SystemComponent
from app.services import incident_state
from app.services.incident_manager import (
    CLASS_DIRECTLY_OBSERVED,
    CLASS_DOWNSTREAM_CONTEXT,
    CLASS_UPSTREAM_CONTEXT,
    IncidentManager,
)

NOW = datetime(2026, 9, 19, 14, 22, tzinfo=timezone.utc)


async def _seed(db_session: AsyncSession, *, name: str = "Checkout"):
    project = SoftwareProject(name="Inc Proj", slug=f"inc-{uuid.uuid4().hex[:8]}")
    db_session.add(project)
    await db_session.flush()
    env = Environment(
        project_id=project.id, name="production", environment_type="PRODUCTION"
    )
    checkout = SystemComponent(
        project_id=project.id, component_type="SERVICE", name=f"{name}"
    )
    inventory = SystemComponent(
        project_id=project.id, component_type="SERVICE", name=f"{name}-inventory"
    )
    gateway = SystemComponent(
        project_id=project.id, component_type="SERVICE", name=f"{name}-gateway"
    )
    db_session.add_all([env, checkout, inventory, gateway])
    await db_session.flush()
    # checkout -> inventory  ⇒  inventory is DOWNSTREAM, gateway is UPSTREAM.
    db_session.add(
        ComponentDependency(
            source_component_id=checkout.id,
            target_component_id=inventory.id,
            dependency_type="HTTP",
        )
    )
    db_session.add(
        ComponentDependency(
            source_component_id=gateway.id,
            target_component_id=checkout.id,
            dependency_type="HTTP",
        )
    )
    await db_session.flush()
    return project.id, env.id, checkout.id, inventory.id, gateway.id


async def _add_anomaly(
    db_session: AsyncSession,
    project_id,
    env_id,
    component_id,
    *,
    at: datetime,
    severity: AnomalySeverity = AnomalySeverity.HIGH,
    anomaly_type: AnomalyType = AnomalyType.LATENCY_SPIKE,
    observed: float = 890.0,
    expected: float = 220.0,
) -> Anomaly:
    anomaly = Anomaly(
        project_id=project_id,
        environment_id=env_id,
        component_id=component_id,
        anomaly_type=anomaly_type,
        severity=severity,
        status=AnomalyStatus.DETECTED,
        source=AnomalySource.METRIC,
        fingerprint=uuid.uuid4().hex,
        detected_at=at,
        observed_value=observed,
        expected_value=expected,
        metric_name="http.checkout.latency.p95",
    )
    db_session.add(anomaly)
    await db_session.flush()
    return anomaly


async def _run(db_session: AsyncSession, project_id, env_id, *, now=None):
    return await IncidentManager(db_session, now=now or NOW).process_scope(
        project_id=project_id, environment_id=env_id
    )


class TestIncidentCreation:
    async def test_creates_incident_from_correlated_anomalies(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, checkout, _, _ = await _seed(db_session)
        a1 = await _add_anomaly(db_session, project_id, env_id, checkout, at=NOW)
        a2 = await _add_anomaly(
            db_session,
            project_id,
            env_id,
            checkout,
            at=NOW + timedelta(seconds=60),
            anomaly_type=AnomalyType.ERROR_RATE_SPIKE,
        )

        result = await _run(db_session, project_id, env_id)
        assert result.incidents_created == 1
        assert result.anomalies_linked == 2

        incident = await db_session.scalar(select(Incident))
        assert incident is not None
        assert incident.status is IncidentStatus.OPEN
        assert incident.fingerprint
        assert incident.summary and "does not establish" in incident.summary
        assert incident.correlation_rationale is not None
        assert incident.primary_component_id == checkout

        await db_session.refresh(a1)
        await db_session.refresh(a2)
        assert a1.incident_id == incident.id
        assert a2.incident_id == incident.id

    async def test_single_low_severity_does_not_open_incident(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, checkout, _, _ = await _seed(db_session)
        await _add_anomaly(
            db_session,
            project_id,
            env_id,
            checkout,
            at=NOW,
            severity=AnomalySeverity.LOW,
        )
        result = await _run(db_session, project_id, env_id)
        assert result.incidents_created == 0
        assert result.skipped_clusters == 1

    async def test_single_high_severity_opens_incident(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, checkout, _, _ = await _seed(db_session)
        await _add_anomaly(db_session, project_id, env_id, checkout, at=NOW)
        result = await _run(db_session, project_id, env_id)
        assert result.incidents_created == 1

    async def test_unrelated_anomalies_produce_two_incidents(
        self, db_session: AsyncSession
    ) -> None:
        """False-merge protection end to end."""
        project_id, env_id, checkout, inventory, _ = await _seed(db_session)
        await _add_anomaly(db_session, project_id, env_id, checkout, at=NOW)
        await _add_anomaly(
            db_session, project_id, env_id, inventory, at=NOW + timedelta(seconds=1)
        )
        # checkout -> inventory is a real edge, so these DO correlate.
        result = await _run(db_session, project_id, env_id)
        assert result.incidents_created == 1

        # Now two structurally unrelated components (add a disconnected one).
        project2, env2, c2, _, _ = await _seed(db_session, name="Analytics")
        orphan = SystemComponent(
            project_id=project2, component_type="SERVICE", name="orphan"
        )
        db_session.add(orphan)
        await db_session.flush()
        await _add_anomaly(db_session, project2, env2, c2, at=NOW)
        await _add_anomaly(
            db_session, project2, env2, orphan.id, at=NOW + timedelta(seconds=1)
        )
        result2 = await _run(db_session, project2, env2)
        assert result2.incidents_created == 2


class TestDeduplication:
    async def test_rerun_does_not_duplicate(self, db_session: AsyncSession) -> None:
        project_id, env_id, checkout, _, _ = await _seed(db_session)
        await _add_anomaly(db_session, project_id, env_id, checkout, at=NOW)
        await _add_anomaly(
            db_session, project_id, env_id, checkout, at=NOW + timedelta(seconds=60)
        )
        await _run(db_session, project_id, env_id)
        second = await _run(db_session, project_id, env_id)

        assert second.incidents_created == 0
        incidents = await db_session.scalar(select(func.count()).select_from(Incident))
        assert incidents == 1
        # Evidence and timeline must not duplicate either.
        evidence = await db_session.scalar(
            select(func.count())
            .select_from(IncidentEvidence)
            .where(IncidentEvidence.evidence_type == EvidenceType.ANOMALY)
        )
        assert evidence == 2
        created_events = await db_session.scalar(
            select(func.count())
            .select_from(IncidentTimelineEvent)
            .where(
                IncidentTimelineEvent.event_type == TimelineEventType.INCIDENT_CREATED
            )
        )
        assert created_events == 1


class TestTimelineAndEvidence:
    async def test_timeline_is_complete_and_ordered(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, checkout, _, _ = await _seed(db_session)
        await _add_anomaly(db_session, project_id, env_id, checkout, at=NOW)
        await _add_anomaly(
            db_session, project_id, env_id, checkout, at=NOW + timedelta(seconds=60)
        )
        await _run(db_session, project_id, env_id)

        incident = await db_session.scalar(select(Incident))
        assert incident is not None
        await db_session.refresh(incident, ["timeline"])
        types = [e.event_type for e in incident.timeline]
        assert TimelineEventType.INCIDENT_CREATED in types
        assert types.count(TimelineEventType.ANOMALY_DETECTED) == 2
        assert types.count(TimelineEventType.COMPONENT_AFFECTED) == 1
        occurred = [e.occurred_at for e in incident.timeline]
        assert occurred == sorted(occurred)

    async def test_anomaly_evidence_carries_provenance_and_relevance(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, checkout, _, _ = await _seed(db_session)
        await _add_anomaly(db_session, project_id, env_id, checkout, at=NOW)
        await _add_anomaly(
            db_session, project_id, env_id, checkout, at=NOW + timedelta(seconds=60)
        )
        await _run(db_session, project_id, env_id)

        rows = list(
            (
                await db_session.execute(
                    select(IncidentEvidence).where(
                        IncidentEvidence.evidence_type == EvidenceType.ANOMALY
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        for row in rows:
            assert (row.provenance or "").lower() == "metric"
            assert row.relevance_reason
            assert "cause" not in (row.relevance_reason or "").lower()
            assert row.observed_value == "890.0"


class TestBlastRadius:
    async def test_classification_direction(self, db_session: AsyncSession) -> None:
        project_id, env_id, checkout, inventory, gateway = await _seed(db_session)
        await _add_anomaly(db_session, project_id, env_id, checkout, at=NOW)
        await _add_anomaly(
            db_session, project_id, env_id, checkout, at=NOW + timedelta(seconds=60)
        )
        manager = IncidentManager(db_session, now=NOW)
        result = await manager.process_scope(
            project_id=project_id, environment_id=env_id
        )
        assert result.incidents_created == 1

        cluster_engine = manager
        # Inspect via the classification helper directly for clarity.
        from app.services.incident_correlation import IncidentCorrelationEngine

        clusters = await IncidentCorrelationEngine(db_session, now=NOW).build_clusters(
            list(
                (
                    await db_session.execute(
                        select(Anomaly).where(Anomaly.component_id == checkout)
                    )
                )
                .scalars()
                .all()
            )
        )
        affected = await cluster_engine._classify_components(clusters[0])
        by_component = {c.component_id: c for c in affected}
        assert by_component[checkout].classification == CLASS_DIRECTLY_OBSERVED
        assert by_component[inventory].classification == CLASS_DOWNSTREAM_CONTEXT
        assert by_component[gateway].classification == CLASS_UPSTREAM_CONTEXT

    async def test_component_names_are_resolved(self, db_session: AsyncSession) -> None:
        project_id, env_id, checkout, inventory, _ = await _seed(db_session)
        await _add_anomaly(db_session, project_id, env_id, checkout, at=NOW)
        await _add_anomaly(
            db_session, project_id, env_id, checkout, at=NOW + timedelta(seconds=60)
        )
        await _run(db_session, project_id, env_id)
        incident = await db_session.scalar(select(Incident))
        assert incident is not None
        assert incident.title.startswith("Checkout:")
        assert (
            "Checkout-inventory" in (incident.summary or "")
            or "inventory" in (incident.summary or "").lower()
        )


class TestContext:
    async def test_deployment_context_is_marked_context_only(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, checkout, _, _ = await _seed(db_session)
        db_session.add(
            DeploymentEvent(
                project_id=project_id,
                environment_id=env_id,
                component_id=checkout,
                deployment_id="deploy-1",
                version="1.2.3",
                deployed_at=NOW - timedelta(minutes=2),
            )
        )
        await db_session.flush()
        await _add_anomaly(db_session, project_id, env_id, checkout, at=NOW)
        await _add_anomaly(
            db_session, project_id, env_id, checkout, at=NOW + timedelta(seconds=60)
        )
        await _run(db_session, project_id, env_id)

        incident = await db_session.scalar(select(Incident))
        assert incident is not None
        timeline = [
            e
            for e in (
                await db_session.execute(
                    select(IncidentTimelineEvent).where(
                        IncidentTimelineEvent.incident_id == incident.id,
                        IncidentTimelineEvent.event_type
                        == TimelineEventType.DEPLOYMENT_OCCURRED,
                    )
                )
            )
            .scalars()
            .all()
        ]
        assert len(timeline) == 1
        assert timeline[0].is_context_only is True
        assert "no causal relationship is claimed" in (timeline[0].description or "")

        evidence = list(
            (
                await db_session.execute(
                    select(IncidentEvidence).where(
                        IncidentEvidence.evidence_type == EvidenceType.DEPLOYMENT
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(evidence) == 1
        assert "temporal context only" in (evidence[0].relevance_reason or "")
        assert "2 minutes before the first observed anomaly" in (incident.summary or "")

    async def test_configuration_context(self, db_session: AsyncSession) -> None:
        project_id, env_id, checkout, _, _ = await _seed(db_session)
        db_session.add(
            ConfigurationChangeEvent(
                project_id=project_id,
                environment_id=env_id,
                component_id=checkout,
                change_id="cfg-1",
                timestamp=NOW - timedelta(seconds=45),
            )
        )
        await db_session.flush()
        await _add_anomaly(db_session, project_id, env_id, checkout, at=NOW)
        await _add_anomaly(
            db_session, project_id, env_id, checkout, at=NOW + timedelta(seconds=60)
        )
        await _run(db_session, project_id, env_id)

        evidence = list(
            (
                await db_session.execute(
                    select(IncidentEvidence).where(
                        IncidentEvidence.evidence_type
                        == EvidenceType.CONFIGURATION_CHANGE
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(evidence) == 1
        assert "temporal context only" in (evidence[0].relevance_reason or "")


class TestLifecycle:
    async def test_acknowledge_and_resolve(self, db_session: AsyncSession) -> None:
        project_id, env_id, checkout, _, _ = await _seed(db_session)
        await _add_anomaly(db_session, project_id, env_id, checkout, at=NOW)
        await _add_anomaly(
            db_session, project_id, env_id, checkout, at=NOW + timedelta(seconds=60)
        )
        await _run(db_session, project_id, env_id)
        incident = await db_session.scalar(select(Incident))
        assert incident is not None

        manager = IncidentManager(db_session, now=NOW + timedelta(minutes=5))
        await manager.transition(
            incident, IncidentStatus.ACKNOWLEDGED, actor="oncall", note="picked up"
        )
        assert incident.status is IncidentStatus.ACKNOWLEDGED
        assert incident.acknowledged_at is not None
        assert incident.status_changed_by == "oncall"

        await manager.transition(incident, IncidentStatus.INVESTIGATING, actor="oncall")
        await manager.transition(incident, IncidentStatus.RESOLVED, actor="oncall")
        assert incident.resolved_at is not None

        await db_session.refresh(incident, ["timeline"])
        assert any(
            e.event_type == TimelineEventType.INCIDENT_ACKNOWLEDGED
            for e in incident.timeline
        )
        assert any(
            e.event_type == TimelineEventType.INCIDENT_RESOLVED
            for e in incident.timeline
        )

    async def test_illegal_transition_rejected(self, db_session: AsyncSession) -> None:
        project_id, env_id, checkout, _, _ = await _seed(db_session)
        await _add_anomaly(db_session, project_id, env_id, checkout, at=NOW)
        incident = Incident(
            project_id=project_id,
            title="t",
            severity="LOW",
            status=IncidentStatus.CLOSED,
            detected_at=NOW,
        )
        db_session.add(incident)
        await db_session.flush()

        manager = IncidentManager(db_session, now=NOW)
        with pytest.raises(incident_state.InvalidIncidentTransition):
            await manager.transition(incident, IncidentStatus.MITIGATED)

    async def test_auto_resolve_when_all_anomalies_closed(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, checkout, _, _ = await _seed(db_session)
        a1 = await _add_anomaly(db_session, project_id, env_id, checkout, at=NOW)
        a2 = await _add_anomaly(
            db_session, project_id, env_id, checkout, at=NOW + timedelta(seconds=60)
        )
        await _run(db_session, project_id, env_id)
        incident = await db_session.scalar(select(Incident))
        assert incident is not None
        assert incident.status is IncidentStatus.OPEN

        a1.status = AnomalyStatus.RESOLVED
        a2.status = AnomalyStatus.RESOLVED
        await db_session.flush()

        await IncidentManager(
            db_session, now=NOW + timedelta(minutes=10)
        ).process_scope(project_id=project_id, environment_id=env_id)
        await db_session.refresh(incident)
        assert incident.status is IncidentStatus.RESOLVED
        assert incident.status_changed_by == "system:auto_resolve"

    async def test_recurrence_reopens_rather_than_duplicates(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, checkout, _, _ = await _seed(db_session)
        a1 = await _add_anomaly(db_session, project_id, env_id, checkout, at=NOW)
        a2 = await _add_anomaly(
            db_session, project_id, env_id, checkout, at=NOW + timedelta(seconds=60)
        )
        await _run(db_session, project_id, env_id)
        incident = await db_session.scalar(select(Incident))
        assert incident is not None

        a1.status = AnomalyStatus.RESOLVED
        a2.status = AnomalyStatus.RESOLVED
        await db_session.flush()
        await IncidentManager(
            db_session, now=NOW + timedelta(minutes=10)
        ).process_scope(project_id=project_id, environment_id=env_id)
        await db_session.refresh(incident)
        assert incident.status is IncidentStatus.RESOLVED

        # A new anomaly in the same bucket must reopen, not duplicate.
        await _add_anomaly(
            db_session, project_id, env_id, checkout, at=NOW + timedelta(seconds=90)
        )
        await db_session.flush()
        await IncidentManager(
            db_session, now=NOW + timedelta(minutes=11)
        ).process_scope(project_id=project_id, environment_id=env_id)
        count = await db_session.scalar(select(func.count()).select_from(Incident))
        assert count == 1
        await db_session.refresh(incident)
        assert incident.status is IncidentStatus.OPEN


class TestIsolation:
    async def test_correlation_never_crosses_projects(
        self, db_session: AsyncSession
    ) -> None:
        project_a, env_a, comp_a, _, _ = await _seed(db_session, name="Alpha")
        project_b, env_b, comp_b, _, _ = await _seed(db_session, name="Beta")
        await _add_anomaly(db_session, project_a, env_a, comp_a, at=NOW)
        await _add_anomaly(
            db_session, project_b, env_b, comp_b, at=NOW + timedelta(seconds=1)
        )

        result_a = await _run(db_session, project_a, env_a)
        assert result_a.incidents_created == 1
        incidents_a = list(
            (
                await db_session.execute(
                    select(Incident).where(Incident.project_id == project_a)
                )
            )
            .scalars()
            .all()
        )
        assert len(incidents_a) == 1
        linked = list(
            (
                await db_session.execute(
                    select(Anomaly).where(Anomaly.incident_id == incidents_a[0].id)
                )
            )
            .scalars()
            .all()
        )
        assert linked
        assert all(a.project_id == project_a for a in linked)


class TestScopeIsolation:
    """The manager obeys the same exact-scope rule as detection/correlation."""

    async def test_project_scope_ignores_environment_anomalies(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, comp, _, _ = await _seed(db_session)
        await _add_anomaly(db_session, project_id, env_id, comp, at=NOW)

        result = await _run(db_session, project_id, None)
        assert result.incidents_created == 0
        count = await db_session.scalar(select(func.count()).select_from(Incident))
        assert count == 0

    async def test_project_scope_does_not_touch_environment_incidents(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, comp, _, _ = await _seed(db_session)
        await _add_anomaly(db_session, project_id, env_id, comp, at=NOW)
        created = await _run(db_session, project_id, env_id)
        assert created.incidents_created == 1

        incident = await db_session.scalar(select(Incident))
        assert incident is not None
        before = incident.status

        # A project-scope pass must not see — let alone change — it.
        await _run(db_session, project_id, None, now=NOW + timedelta(hours=6))
        await db_session.refresh(incident)
        assert incident.status is before
        assert incident.environment_id == env_id
