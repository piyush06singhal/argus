"""Phase 3 deterministic demo scenario tests (§49).

The demo is the integration test for the whole phase: telemetry is ingested, the
real detectors run, and the real correlation engine builds the incident. If any
layer regresses, these assertions fail — which is the entire point of having a
deterministic scenario instead of a scripted fixture.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.anomaly import Anomaly, AnomalyRule
from app.models.deployment import DeploymentEvent
from app.models.incident import (
    EvidenceType,
    Incident,
    IncidentEvidence,
    IncidentStatus,
    IncidentTimelineEvent,
    TimelineEventType,
)
from app.models.project import Environment, SoftwareProject
from app.models.system import ComponentCategory, ComponentDependency, SystemComponent
from app.services.demo_incident import (
    CHECKOUT_P95_SPIKE,
    METRIC_CHECKOUT_P95,
    RULE_IDS,
    seed_checkout_incident,
)

NOW = datetime(2026, 9, 19, 14, 26, tzinfo=timezone.utc)


async def _seed_topology(db: AsyncSession):
    """The §49 topology: gateway -> checkout -> {inventory, payment, redis}."""
    project = SoftwareProject(name="Demo Commerce", slug=f"demo-{uuid.uuid4().hex[:8]}")
    db.add(project)
    await db.flush()
    environment = Environment(
        project_id=project.id,
        name="Production",
        environment_type="PRODUCTION",
    )
    db.add(environment)
    await db.flush()

    components = {
        "api_gateway": SystemComponent(
            project_id=project.id,
            environment_id=environment.id,
            name="API Gateway",
            component_type=ComponentCategory.SERVICE,
        ),
        "checkout_service": SystemComponent(
            project_id=project.id,
            environment_id=environment.id,
            name="Checkout Service",
            component_type=ComponentCategory.SERVICE,
        ),
        "inventory_service": SystemComponent(
            project_id=project.id,
            environment_id=environment.id,
            name="Inventory Service",
            component_type=ComponentCategory.SERVICE,
        ),
        "inventory_db": SystemComponent(
            project_id=project.id,
            environment_id=environment.id,
            name="Inventory Database",
            component_type=ComponentCategory.DATABASE,
        ),
        "payment_service": SystemComponent(
            project_id=project.id,
            environment_id=environment.id,
            name="Payment Service",
            component_type=ComponentCategory.SERVICE,
        ),
        "redis": SystemComponent(
            project_id=project.id,
            environment_id=environment.id,
            name="Redis Cache",
            component_type=ComponentCategory.CACHE,
        ),
    }
    for component in components.values():
        db.add(component)
    await db.flush()

    for source, target in (
        ("api_gateway", "checkout_service"),
        ("checkout_service", "inventory_service"),
        ("inventory_service", "inventory_db"),
        ("checkout_service", "payment_service"),
        ("checkout_service", "redis"),
    ):
        db.add(
            ComponentDependency(
                source_component_id=components[source].id,
                target_component_id=components[target].id,
                dependency_type="HTTP",
            )
        )
    await db.flush()
    return project, environment, components


async def _run_demo(db: AsyncSession):
    project, environment, components = await _seed_topology(db)
    result = await seed_checkout_incident(
        db,
        project=project,
        environment=environment,
        components=components,
        now=NOW,
    )
    await db.flush()
    return project, environment, components, result


class TestDemoScenario:
    async def test_demo_produces_one_incident(self, db_session: AsyncSession) -> None:
        project, _environment, _components, result = await _run_demo(db_session)

        assert result["skipped"] is False
        # The count tracks the scenario definition rather than a magic number:
        # Phase 4 added the datastore rule the causal chain needs (§48).
        assert result["rules"] == len(RULE_IDS)
        # Multiple distinct anomaly classes must be detected.
        types = {a["type"] for a in result["anomalies"]}
        assert "LATENCY_SPIKE" in types
        assert "ERROR_RATE_SPIKE" in types
        assert "TRACE_FAILURE_SPIKE" in types
        assert "HEALTH_DEGRADATION" in types
        assert "METRIC_BASELINE_DEVIATION" in types

        # ... and they must be correlated into exactly one incident.
        incidents = await db_session.scalar(
            select(func.count())
            .select_from(Incident)
            .where(Incident.project_id == project.id)
        )
        assert incidents == 1
        assert result["correlation"]["incidents_created"] == 1

        incident = (
            await db_session.execute(
                select(Incident).where(Incident.project_id == project.id)
            )
        ).scalar_one()
        assert incident.status is IncidentStatus.OPEN
        assert incident.fingerprint
        assert incident.primary_component_id is not None

    async def test_anomalies_are_linked_not_duplicated(
        self, db_session: AsyncSession
    ) -> None:
        project, _environment, _components, _result = await _run_demo(db_session)
        linked = await db_session.scalar(
            select(func.count())
            .select_from(Anomaly)
            .where(
                Anomaly.project_id == project.id,
                Anomaly.incident_id.isnot(None),
            )
        )
        total = await db_session.scalar(
            select(func.count())
            .select_from(Anomaly)
            .where(Anomaly.project_id == project.id)
        )
        assert linked == total
        assert total >= 5

    async def test_timeline_orders_deployment_before_anomalies(
        self, db_session: AsyncSession
    ) -> None:
        project, _environment, _components, _result = await _run_demo(db_session)
        incident = (
            await db_session.execute(
                select(Incident).where(Incident.project_id == project.id)
            )
        ).scalar_one()

        events = list(
            (
                await db_session.execute(
                    select(IncidentTimelineEvent)
                    .where(IncidentTimelineEvent.incident_id == incident.id)
                    .order_by(IncidentTimelineEvent.occurred_at)
                )
            )
            .scalars()
            .all()
        )
        types = [e.event_type for e in events]
        assert TimelineEventType.DEPLOYMENT_OCCURRED in types
        assert TimelineEventType.ANOMALY_DETECTED in types

        deployment_index = types.index(TimelineEventType.DEPLOYMENT_OCCURRED)
        first_anomaly_index = types.index(TimelineEventType.ANOMALY_DETECTED)
        assert deployment_index < first_anomaly_index

        deployment_event = events[deployment_index]
        assert deployment_event.is_context_only is True
        assert "no causal relationship is claimed" in (
            deployment_event.description or ""
        )

    async def test_summary_states_context_without_causality(
        self, db_session: AsyncSession
    ) -> None:
        project, _environment, _components, _result = await _run_demo(db_session)
        incident = (
            await db_session.execute(
                select(Incident).where(Incident.project_id == project.id)
            )
        ).scalar_one()
        assert incident.summary
        # The exact wording §49 mandates.
        assert "does not establish" in incident.summary
        # §49 mandates exactly this phrasing for the deployment context.
        assert "2 minutes before the first observed anomaly" in incident.summary
        assert "Deployment" in incident.summary
        # And the forbidden claim must never appear as an assertion: every
        # mention of causality must be inside a "does not establish" sentence.
        for line in incident.summary.splitlines():
            if "caused the incident" in line.lower():
                assert "does not establish" in line.lower(), line

    async def test_deployment_evidence_and_context(
        self, db_session: AsyncSession
    ) -> None:
        project, _environment, components, _result = await _run_demo(db_session)
        incident = (
            await db_session.execute(
                select(Incident).where(Incident.project_id == project.id)
            )
        ).scalar_one()

        deployment_evidence = list(
            (
                await db_session.execute(
                    select(IncidentEvidence).where(
                        IncidentEvidence.incident_id == incident.id,
                        IncidentEvidence.evidence_type == EvidenceType.DEPLOYMENT,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(deployment_evidence) == 1
        assert "temporal context only" in (
            deployment_evidence[0].relevance_reason or ""
        )

        deployment = (
            await db_session.execute(
                select(DeploymentEvent).where(DeploymentEvent.project_id == project.id)
            )
        ).scalar_one()
        checkout = components["checkout_service"]
        assert deployment.component_id == checkout.id
        # SQLite drops tzinfo on read; compare the instant, not the tz marker.
        assert deployment.deployed_at.replace(tzinfo=timezone.utc) == NOW - timedelta(
            minutes=2
        )

    async def test_affected_components_include_direct_and_context(
        self, db_session: AsyncSession
    ) -> None:
        project, _environment, components, _result = await _run_demo(db_session)
        incident = (
            await db_session.execute(
                select(Incident).where(Incident.project_id == project.id)
            )
        ).scalar_one()

        from app.services.incident_context import build_dependency_components

        observed = list(
            (
                await db_session.execute(
                    select(Anomaly.component_id).where(
                        Anomaly.incident_id == incident.id,
                        Anomaly.component_id.isnot(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        views = await build_dependency_components(db_session, observed)
        classifications = {v.component_id: v.classification for v in views}
        assert classifications[components["checkout_service"].id] == (
            "DIRECTLY_OBSERVED"
        )
        assert classifications[components["inventory_service"].id] in (
            "DOWNSTREAM_CONTEXT",
            "DIRECTLY_OBSERVED",
        )
        # The upstream caller is context, never a cause.
        assert classifications[components["api_gateway"].id] == "UPSTREAM_CONTEXT"

    async def test_demo_is_idempotent(self, db_session: AsyncSession) -> None:
        project, environment, components, _result = await _run_demo(db_session)
        second = await seed_checkout_incident(
            db_session,
            project=project,
            environment=environment,
            components=components,
            now=NOW,
        )
        assert second["skipped"] is True

        incidents = await db_session.scalar(
            select(func.count())
            .select_from(Incident)
            .where(Incident.project_id == project.id)
        )
        rules = await db_session.scalar(
            select(func.count())
            .select_from(AnomalyRule)
            .where(AnomalyRule.project_id == project.id)
        )
        assert incidents == 1
        assert rules == len(RULE_IDS)

    async def test_observed_value_matches_story(self, db_session: AsyncSession) -> None:
        project, _environment, _components, _result = await _run_demo(db_session)
        anomaly = (
            await db_session.execute(
                select(Anomaly).where(
                    Anomaly.project_id == project.id,
                    Anomaly.metric_name == METRIC_CHECKOUT_P95,
                )
            )
        ).scalar_one()
        assert anomaly.observed_value == CHECKOUT_P95_SPIKE
        assert anomaly.expected_value == 220.0
        assert anomaly.deviation and anomaly.deviation > 3


class TestDemoRuleReconciliation:
    """A ``--force`` re-seed must realign stale demo rules, not skip them.

    Rules are keyed by fixed demo ids. Skipping a pre-existing one would pin the
    scenario to whatever scope an earlier run used, so a corrected re-seed could
    never repair the data — which is exactly how a production incident ended up
    attributed to a staging component.
    """

    async def test_force_realigns_rule_scope(self, db_session: AsyncSession) -> None:
        project, environment, components, _result = await _run_demo(db_session)
        rule_id = RULE_IDS["checkout_latency"]
        rule = await db_session.get(AnomalyRule, rule_id)
        assert rule is not None

        # Simulate the stale state: a rule bound to a component it should not be.
        original_component = rule.component_id
        rule.component_id = uuid.uuid4()
        rule.threshold = 9_999.0
        await db_session.flush()

        await seed_checkout_incident(
            db_session,
            project=project,
            environment=environment,
            components=components,
            now=NOW,
            force=True,
        )
        await db_session.flush()
        await db_session.refresh(rule)
        assert rule.component_id == original_component
        assert rule.component_id == components["checkout_service"].id
        assert rule.threshold == 500.0

    async def test_detected_anomalies_use_the_production_component(
        self, db_session: AsyncSession
    ) -> None:
        project, environment, components, _result = await _run_demo(db_session)
        anomalies = list(
            (
                await db_session.execute(
                    select(Anomaly).where(Anomaly.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        assert anomalies
        # Every anomaly must belong to the demo environment and its components.
        for anomaly in anomalies:
            assert anomaly.environment_id == environment.id
            assert anomaly.component_id in {c.id for c in components.values()}
