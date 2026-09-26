"""ARGUS Seed Data Script."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import async_session_factory, init_db
from app.models.deployment import CodeRepository, DeploymentEvent, DeploymentStatus
from app.models.graph import GraphEdge, GraphNode
from app.models.incident import (
    EvidenceType,
    Incident,
    IncidentEvidence,
    IncidentSeverity,
    IncidentStatus,
    IncidentTimelineEvent,
    TimelineEventType,
)
from app.models.observability import (
    LogRecord,
    MetricRecord,
    MetricType,
    Severity,
    SpanRecord,
    TraceRecord,
    TraceStatus,
)
from app.models.project import Environment, EnvironmentType, SoftwareProject
from app.models.system import (
    ComponentCategory,
    ComponentDependency,
    ComponentStatus,
    DependencyType,
    SystemComponent,
)
from app.services.demo_incident import seed_checkout_incident
from app.services.endpoint_registry import EndpointRegistry
from app.services.graph_extractor import GraphExtractor
from app.services.graph_registry import ComponentRegistry
from app.services.graph_reconciler import GraphReconciler
from app.services.graph_snapshot_service import GraphSnapshotService
from app.schemas.graph import OwnerCreate


settings = get_settings()

SEED_PROJECT_ID = uuid.UUID("10000000-0000-0000-0000-00000000a001")


async def _seed_data_exists(db: AsyncSession) -> bool:
    """True when the demo dataset is already present.

    Keyed on the seed project's deterministic slug (equally unique and
    deterministic as the UUID, but avoids SQLAlchemy's ``UUID(as_uuid=True)``
    type hitting SQLite's ``process_result_value`` during comparison).

    The old ``select(SoftwareProject)`` + ``scalar_one_or_none()`` check raised
    MultipleResultsFound (container crash-loop on boot) as soon as a second
    project existed.
    """
    result = await db.execute(
        select(SoftwareProject).where(SoftwareProject.slug == "argus-demo-commerce")
    )
    return result.scalar_one_or_none() is not None


async def seed_demo_data() -> None:
    """Seed the database with demo data."""
    await init_db()

    async with async_session_factory() as db:
        # Check if the seed data already exists by its deterministic project id.
        if await _seed_data_exists(db):
            print("Demo data already exists. Skipping seed.")
            return

        print("Seeding demo data...")

        # Create project: ARGUS Demo Commerce
        project = SoftwareProject(
            id=uuid.UUID("10000000-0000-0000-0000-00000000a001"),
            name="ARGUS Demo Commerce",
            slug="argus-demo-commerce",
            description="A fictional e-commerce platform used to demonstrate ARGUS capabilities.",
            status="ACTIVE",
            repository_url="https://github.com/example/demo-commerce",
            repository_provider="github",
            default_branch="main",
        )
        db.add(project)
        await db.flush()

        # Create environments
        environments = {
            "production": Environment(
                id=uuid.UUID("20000000-0000-0000-0000-00000000a001"),
                project_id=project.id,
                name="Production",
                environment_type=EnvironmentType.PRODUCTION,
                description="Production environment",
            ),
            "staging": Environment(
                id=uuid.UUID("20000000-0000-0000-0000-00000000a002"),
                project_id=project.id,
                name="Staging",
                environment_type=EnvironmentType.STAGING,
                description="Staging environment",
            ),
            "development": Environment(
                id=uuid.UUID("20000000-0000-0000-0000-00000000a003"),
                project_id=project.id,
                name="Development",
                environment_type=EnvironmentType.DEVELOPMENT,
                description="Development environment",
            ),
        }
        for env in environments.values():
            db.add(env)
        await db.flush()

        # Create components
        components = {
            "web_frontend": SystemComponent(
                id=uuid.UUID("30000000-0000-0000-0000-00000000a001"),
                project_id=project.id,
                environment_id=environments["production"].id,
                name="Web Frontend",
                component_type=ComponentCategory.FRONTEND,
                description="React-based web frontend",
                status=ComponentStatus.HEALTHY,
            ),
            "api_gateway": SystemComponent(
                id=uuid.UUID("30000000-0000-0000-0000-00000000a002"),
                project_id=project.id,
                environment_id=environments["production"].id,
                name="API Gateway",
                component_type=ComponentCategory.SERVICE,
                description="Nginx-based API gateway and load balancer",
                status=ComponentStatus.HEALTHY,
            ),
            "checkout_service": SystemComponent(
                id=uuid.UUID("30000000-0000-0000-0000-00000000a003"),
                project_id=project.id,
                environment_id=environments["production"].id,
                name="Checkout Service",
                component_type=ComponentCategory.SERVICE,
                description="Handles checkout and order processing",
                status=ComponentStatus.DEGRADED,
            ),
            "inventory_service": SystemComponent(
                id=uuid.UUID("30000000-0000-0000-0000-00000000a004"),
                project_id=project.id,
                environment_id=environments["production"].id,
                name="Inventory Service",
                component_type=ComponentCategory.SERVICE,
                description="Manages product inventory and stock levels",
                status=ComponentStatus.UNHEALTHY,
            ),
            "payment_service": SystemComponent(
                id=uuid.UUID("30000000-0000-0000-0000-00000000a005"),
                project_id=project.id,
                environment_id=environments["production"].id,
                name="Payment Service",
                component_type=ComponentCategory.SERVICE,
                description="Processes payment transactions",
                status=ComponentStatus.HEALTHY,
            ),
            "postgresql": SystemComponent(
                id=uuid.UUID("30000000-0000-0000-0000-00000000a006"),
                project_id=project.id,
                environment_id=environments["production"].id,
                name="PostgreSQL",
                component_type=ComponentCategory.DATABASE,
                description="Primary relational database",
                status=ComponentStatus.DEGRADED,
            ),
            "redis": SystemComponent(
                id=uuid.UUID("30000000-0000-0000-0000-00000000a007"),
                project_id=project.id,
                environment_id=environments["production"].id,
                name="Redis",
                component_type=ComponentCategory.CACHE,
                description="Caching and session store",
                status=ComponentStatus.HEALTHY,
            ),
        }
        for comp in components.values():
            db.add(comp)
        await db.flush()

        # Create dependencies
        dependencies = [
            ComponentDependency(
                id=uuid.UUID("40000000-0000-0000-0000-00000000a001"),
                source_component_id=components["web_frontend"].id,
                target_component_id=components["api_gateway"].id,
                dependency_type=DependencyType.HTTP,
                description="Frontend calls API Gateway",
            ),
            ComponentDependency(
                id=uuid.UUID("40000000-0000-0000-0000-00000000a002"),
                source_component_id=components["api_gateway"].id,
                target_component_id=components["checkout_service"].id,
                dependency_type=DependencyType.HTTP,
                description="API Gateway routes to Checkout Service",
            ),
            ComponentDependency(
                id=uuid.UUID("40000000-0000-0000-0000-00000000a003"),
                source_component_id=components["checkout_service"].id,
                target_component_id=components["inventory_service"].id,
                dependency_type=DependencyType.HTTP,
                description="Checkout Service calls Inventory Service",
            ),
            ComponentDependency(
                id=uuid.UUID("40000000-0000-0000-0000-00000000a004"),
                source_component_id=components["checkout_service"].id,
                target_component_id=components["payment_service"].id,
                dependency_type=DependencyType.HTTP,
                description="Checkout Service calls Payment Service",
            ),
            ComponentDependency(
                id=uuid.UUID("40000000-0000-0000-0000-00000000a005"),
                source_component_id=components["checkout_service"].id,
                target_component_id=components["redis"].id,
                dependency_type=DependencyType.CACHE,
                description="Checkout Service uses Redis for caching",
            ),
            ComponentDependency(
                id=uuid.UUID("40000000-0000-0000-0000-00000000a006"),
                source_component_id=components["inventory_service"].id,
                target_component_id=components["postgresql"].id,
                dependency_type=DependencyType.DATABASE,
                description="Inventory Service reads from PostgreSQL",
            ),
            ComponentDependency(
                id=uuid.UUID("40000000-0000-0000-0000-00000000a007"),
                source_component_id=components["payment_service"].id,
                target_component_id=components["postgresql"].id,
                dependency_type=DependencyType.DATABASE,
                description="Payment Service reads from PostgreSQL",
            ),
        ]
        for dep in dependencies:
            db.add(dep)
        await db.flush()

        # Create deployment events
        base_time = datetime.now(timezone.utc) - timedelta(hours=6)
        deployments = [
            DeploymentEvent(
                id=uuid.UUID("50000000-0000-0000-0000-00000000a001"),
                project_id=project.id,
                environment_id=environments["production"].id,
                component_id=components["inventory_service"].id,
                deployment_id="deploy-inv-2024-001",
                version="2.3.1",
                commit_sha="a1b2c3d4e5f6g7h8i9j0",
                deployed_at=base_time - timedelta(minutes=45),
                status=DeploymentStatus.SUCCESS,
                description="Deploy Inventory Service v2.3.1 with new stock calculation logic",
            ),
            DeploymentEvent(
                id=uuid.UUID("50000000-0000-0000-0000-00000000a002"),
                project_id=project.id,
                environment_id=environments["production"].id,
                component_id=components["checkout_service"].id,
                deployment_id="deploy-checkout-2024-001",
                version="1.8.0",
                commit_sha="b2c3d4e5f6g7h8i9j0k1",
                deployed_at=base_time - timedelta(hours=2),
                status=DeploymentStatus.SUCCESS,
                description="Deploy Checkout Service v1.8.0 with UI improvements",
            ),
        ]
        for dep in deployments:
            db.add(dep)
        await db.flush()

        # Create traces
        trace_id_1 = "trace-checkout-001"
        trace_id_2 = "trace-checkout-002"
        traces = [
            TraceRecord(
                id=uuid.UUID("60000000-0000-0000-0000-00000000a001"),
                project_id=project.id,
                environment_id=environments["production"].id,
                trace_id=trace_id_1,
                name="checkout-process",
                start_time=base_time - timedelta(minutes=30),
                end_time=base_time - timedelta(minutes=30) + timedelta(seconds=45),
                duration_ms=45000,
                status=TraceStatus.OK,
            ),
            TraceRecord(
                id=uuid.UUID("60000000-0000-0000-0000-00000000a002"),
                project_id=project.id,
                environment_id=environments["production"].id,
                trace_id=trace_id_2,
                name="checkout-process",
                start_time=base_time - timedelta(minutes=15),
                end_time=base_time - timedelta(minutes=15) + timedelta(seconds=120),
                duration_ms=120000,
                status=TraceStatus.ERROR,
            ),
        ]
        for trace in traces:
            db.add(trace)
        await db.flush()

        # Create spans for trace 1
        spans_1 = [
            SpanRecord(
                id=uuid.UUID("70000000-0000-0000-0000-00000000a001"),
                trace_id=trace_id_1,
                span_id="span-1-1",
                project_id=project.id,
                component_id=components["web_frontend"].id,
                operation="POST /api/checkout",
                start_time=base_time - timedelta(minutes=30),
                end_time=base_time - timedelta(minutes=30) + timedelta(seconds=2),
                duration_ms=2000,
                status=TraceStatus.OK,
            ),
            SpanRecord(
                id=uuid.UUID("70000000-0000-0000-0000-00000000a002"),
                trace_id=trace_id_1,
                span_id="span-1-2",
                parent_span_id="span-1-1",
                project_id=project.id,
                component_id=components["api_gateway"].id,
                operation="route /api/checkout",
                start_time=base_time - timedelta(minutes=30) + timedelta(seconds=2),
                end_time=base_time - timedelta(minutes=30) + timedelta(seconds=4),
                duration_ms=2000,
                status=TraceStatus.OK,
            ),
            SpanRecord(
                id=uuid.UUID("70000000-0000-0000-0000-00000000a003"),
                trace_id=trace_id_1,
                span_id="span-1-3",
                parent_span_id="span-1-2",
                project_id=project.id,
                component_id=components["checkout_service"].id,
                operation="process-checkout",
                start_time=base_time - timedelta(minutes=30) + timedelta(seconds=4),
                end_time=base_time - timedelta(minutes=30) + timedelta(seconds=40),
                duration_ms=36000,
                status=TraceStatus.OK,
            ),
            SpanRecord(
                id=uuid.UUID("70000000-0000-0000-0000-00000000a004"),
                trace_id=trace_id_1,
                span_id="span-1-4",
                parent_span_id="span-1-3",
                project_id=project.id,
                component_id=components["inventory_service"].id,
                operation="check-inventory",
                start_time=base_time - timedelta(minutes=30) + timedelta(seconds=5),
                end_time=base_time - timedelta(minutes=30) + timedelta(seconds=25),
                duration_ms=20000,
                status=TraceStatus.OK,
            ),
            SpanRecord(
                id=uuid.UUID("70000000-0000-0000-0000-00000000a005"),
                trace_id=trace_id_1,
                span_id="span-1-5",
                parent_span_id="span-1-3",
                project_id=project.id,
                component_id=components["payment_service"].id,
                operation="process-payment",
                start_time=base_time - timedelta(minutes=30) + timedelta(seconds=5),
                end_time=base_time - timedelta(minutes=30) + timedelta(seconds=15),
                duration_ms=10000,
                status=TraceStatus.OK,
            ),
        ]
        for span in spans_1:
            db.add(span)
        await db.flush()

        # Create spans for trace 2 (the slow one)
        spans_2 = [
            SpanRecord(
                id=uuid.UUID("70000000-0000-0000-0000-00000000a006"),
                trace_id=trace_id_2,
                span_id="span-2-1",
                project_id=project.id,
                component_id=components["web_frontend"].id,
                operation="POST /api/checkout",
                start_time=base_time - timedelta(minutes=15),
                end_time=base_time - timedelta(minutes=15) + timedelta(seconds=2),
                duration_ms=2000,
                status=TraceStatus.OK,
            ),
            SpanRecord(
                id=uuid.UUID("70000000-0000-0000-0000-00000000a007"),
                trace_id=trace_id_2,
                span_id="span-2-2",
                parent_span_id="span-2-1",
                project_id=project.id,
                component_id=components["checkout_service"].id,
                operation="process-checkout",
                start_time=base_time - timedelta(minutes=15) + timedelta(seconds=4),
                end_time=base_time - timedelta(minutes=15) + timedelta(seconds=118),
                duration_ms=114000,
                status=TraceStatus.ERROR,
            ),
            SpanRecord(
                id=uuid.UUID("70000000-0000-0000-0000-00000000a008"),
                trace_id=trace_id_2,
                span_id="span-2-3",
                parent_span_id="span-2-2",
                project_id=project.id,
                component_id=components["inventory_service"].id,
                operation="check-inventory",
                start_time=base_time - timedelta(minutes=15) + timedelta(seconds=5),
                end_time=base_time - timedelta(minutes=15) + timedelta(seconds=115),
                duration_ms=110000,
                status=TraceStatus.ERROR,
            ),
        ]
        for span in spans_2:
            db.add(span)
        await db.flush()

        # Create log records
        logs = [
            LogRecord(
                id=uuid.UUID("80000000-0000-0000-0000-00000000a001"),
                project_id=project.id,
                environment_id=environments["production"].id,
                component_id=components["inventory_service"].id,
                timestamp=base_time - timedelta(minutes=28),
                level=Severity.INFO,
                message="Inventory check initiated for order #ORD-2847",
                service="inventory-service",
                trace_id=trace_id_2,
            ),
            LogRecord(
                id=uuid.UUID("80000000-0000-0000-0000-00000000a002"),
                project_id=project.id,
                environment_id=environments["production"].id,
                component_id=components["inventory_service"].id,
                timestamp=base_time - timedelta(minutes=27),
                level=Severity.WARN,
                message="Database query took 15234ms (threshold: 5000ms)",
                service="inventory-service",
                trace_id=trace_id_2,
            ),
            LogRecord(
                id=uuid.UUID("80000000-0000-0000-0000-00000000a003"),
                project_id=project.id,
                environment_id=environments["production"].id,
                component_id=components["inventory_service"].id,
                timestamp=base_time - timedelta(minutes=26),
                level=Severity.ERROR,
                message="Connection pool exhausted: 25/25 connections in use",
                service="inventory-service",
                trace_id=trace_id_2,
            ),
            LogRecord(
                id=uuid.UUID("80000000-0000-0000-0000-00000000a004"),
                project_id=project.id,
                environment_id=environments["production"].id,
                component_id=components["checkout_service"].id,
                timestamp=base_time - timedelta(minutes=25),
                level=Severity.ERROR,
                message="Checkout timeout after 120000ms waiting for inventory check",
                service="checkout-service",
                trace_id=trace_id_2,
            ),
            LogRecord(
                id=uuid.UUID("80000000-0000-0000-0000-00000000a005"),
                project_id=project.id,
                environment_id=environments["production"].id,
                component_id=components["inventory_service"].id,
                timestamp=base_time - timedelta(minutes=44),
                level=Severity.INFO,
                message="Inventory Service v2.3.1 deployment completed successfully",
                service="inventory-service",
            ),
        ]
        for log in logs:
            db.add(log)
        await db.flush()

        # Create metric records
        metrics = [
            MetricRecord(
                id=uuid.UUID("90000000-0000-0000-0000-00000000a001"),
                project_id=project.id,
                environment_id=environments["production"].id,
                component_id=components["inventory_service"].id,
                timestamp=base_time - timedelta(minutes=28),
                metric_name="db_connection_pool_active",
                metric_type=MetricType.GAUGE,
                value=25,
                unit="connections",
            ),
            MetricRecord(
                id=uuid.UUID("90000000-0000-0000-0000-00000000a002"),
                project_id=project.id,
                environment_id=environments["production"].id,
                component_id=components["inventory_service"].id,
                timestamp=base_time - timedelta(minutes=28),
                metric_name="db_query_duration_ms",
                metric_type=MetricType.HISTOGRAM,
                value=15234,
                unit="ms",
            ),
            MetricRecord(
                id=uuid.UUID("90000000-0000-0000-0000-00000000a003"),
                project_id=project.id,
                environment_id=environments["production"].id,
                component_id=components["checkout_service"].id,
                timestamp=base_time - timedelta(minutes=28),
                metric_name="checkout_request_duration_ms",
                metric_type=MetricType.HISTOGRAM,
                value=120000,
                unit="ms",
            ),
            MetricRecord(
                id=uuid.UUID("90000000-0000-0000-0000-00000000a004"),
                project_id=project.id,
                environment_id=environments["production"].id,
                component_id=components["checkout_service"].id,
                timestamp=base_time - timedelta(minutes=28),
                metric_name="checkout_requests_total",
                metric_type=MetricType.COUNTER,
                value=15,
                unit="requests",
            ),
            MetricRecord(
                id=uuid.UUID("90000000-0000-0000-0000-00000000a005"),
                project_id=project.id,
                environment_id=environments["production"].id,
                component_id=components["checkout_service"].id,
                timestamp=base_time - timedelta(minutes=28),
                metric_name="checkout_errors_total",
                metric_type=MetricType.COUNTER,
                value=8,
                unit="errors",
            ),
        ]
        for metric in metrics:
            db.add(metric)
        await db.flush()

        # Create incident
        incident = Incident(
            id=uuid.UUID("a0000000-0000-0000-0000-00000000a001"),
            project_id=project.id,
            environment_id=environments["production"].id,
            title="Checkout latency increased significantly after Inventory Service deployment",
            description=(
                "Following the deployment of Inventory Service v2.3.1, "
                "checkout requests have experienced significant latency increases. "
                "Multiple requests have timed out waiting for inventory checks. "
                "Database connection pool exhaustion observed in Inventory Service."
            ),
            severity=IncidentSeverity.HIGH,
            status=IncidentStatus.INVESTIGATING,
            detected_at=base_time - timedelta(minutes=25),
            started_at=base_time - timedelta(minutes=30),
            metadata_={
                "affected_components": [
                    str(components["checkout_service"].id),
                    str(components["inventory_service"].id),
                ],
                "triggered_by_deployment": str(deployments[0].id),
            },
        )
        db.add(incident)
        await db.flush()

        #: The demo incident must satisfy the invariant ARGUS enforces everywhere
        #: else — an incident has a recorded history. Seeding one without it made
        #: the reference dataset violate the platform's own data-quality rule
        #: (``MISSING_AUDIT_EVENT``) on first boot (found by the W5 checks).
        db.add(
            IncidentTimelineEvent(
                incident_id=incident.id,
                project_id=project.id,
                environment_id=environments["production"].id,
                event_type=TimelineEventType.INCIDENT_CREATED,
                occurred_at=incident.detected_at,
                title="Incident detected from deployment-correlated telemetry",
                description=(
                    "Checkout latency rose after the inventory-service deployment; "
                    "recorded as the incident's first history entry."
                ),
                provenance="seed",
            )
        )
        await db.flush()

        # Create evidence
        evidence_items = [
            IncidentEvidence(
                id=uuid.UUID("b0000000-0000-0000-0000-00000000a001"),
                incident_id=str(incident.id),
                evidence_type=EvidenceType.DEPLOYMENT,
                source_id=str(deployments[0].id),
                timestamp=deployments[0].deployed_at,
                relevance_score=0.9,
                description="Inventory Service v2.3.1 deployed 45 minutes before incident started",
            ),
            IncidentEvidence(
                id=uuid.UUID("b0000000-0000-0000-0000-00000000a002"),
                incident_id=str(incident.id),
                evidence_type=EvidenceType.LOG,
                source_id=str(logs[2].id),
                timestamp=logs[2].timestamp,
                relevance_score=0.95,
                description="Database connection pool exhausted in Inventory Service",
            ),
            IncidentEvidence(
                id=uuid.UUID("b0000000-0000-0000-0000-00000000a003"),
                incident_id=str(incident.id),
                evidence_type=EvidenceType.METRIC,
                source_id=str(metrics[0].id),
                timestamp=metrics[0].timestamp,
                relevance_score=0.85,
                description="Database connection pool at maximum capacity (25/25)",
            ),
            IncidentEvidence(
                id=uuid.UUID("b0000000-0000-0000-0000-00000000a004"),
                incident_id=str(incident.id),
                evidence_type=EvidenceType.TRACE,
                source_id=str(traces[1].id),
                timestamp=traces[1].start_time,
                relevance_score=0.8,
                description="Slow trace showing 120s checkout process with error",
            ),
        ]
        for evidence in evidence_items:
            db.add(evidence)
        await db.flush()

        graph_stats = await _seed_phase2_graph(db, project, environments, components)

        # Phase 3 (§49–§50): the deterministic "ARGUS Checkout Latency
        # Incident". Runs the real detectors and correlation engine over
        # scripted telemetry, so a fresh install has a genuine anomaly-to-
        # incident story to explore without any external observability source.
        phase3_stats = await seed_checkout_incident(
            db,
            project=project,
            environment=environments["production"],
            components=components,
        )

        await db.commit()
        print("Demo data seeded successfully!")
        print(f"  Project: {project.name} ({project.slug})")
        print(f"  Environments: {len(environments)}")
        print(f"  Components: {len(components)}")
        print(f"  Dependencies: {len(dependencies)}")
        print(f"  Deployments: {len(deployments)}")
        print(f"  Traces: {len(traces)}")
        print(f"  Spans: {len(spans_1) + len(spans_2)}")
        print(f"  Logs: {len(logs)}")
        print(f"  Metrics: {len(metrics)}")
        print("  Incidents: 2 (1 Phase 0 demo + 1 Phase 3 correlated)")
        print(f"  Evidence items: {len(evidence_items)}")
        print(
            "  Graph: "
            f"{graph_stats['nodes']} nodes, {graph_stats['edges']} edges, "
            f"{graph_stats['endpoints']} endpoints, "
            f"{graph_stats['snapshots']} snapshot"
        )
        if phase3_stats.get("skipped"):
            print(f"  Phase 3 demo: skipped ({phase3_stats.get('reason')})")
        else:
            detection = phase3_stats["detection"]
            correlation = phase3_stats["correlation"]
            print(
                "  Phase 3 demo: "
                f"{phase3_stats['rules']} rules, "
                f"{detection['anomalies_opened']} anomalies opened, "
                f"{correlation['incidents_created']} incident(s), "
                f"{detection['observations_recorded']} observations"
            )
            print(f"  Phase 3 incident: {phase3_stats['incident_id']}")


async def _seed_phase2_graph(
    db: AsyncSession,
    project: SoftwareProject,
    environments: dict[str, Environment],
    components: dict[str, SystemComponent],
) -> dict[str, int]:
    """Phase 2 (§72–75): knowledge-graph overlay over the demo topology.

    Idempotent by construction: it only runs on a fresh seed (guarded by
    ``_seed_data_exists``) and every write goes through the same upsert
    registries the API uses.
    """
    prod = environments["production"]
    staging = environments["staging"]
    # Trace that carries the §73 gateway -> external-payment span subtree.
    trace_id_1 = "trace-checkout-001"
    base_time = datetime.now(timezone.utc) - timedelta(hours=6)

    # ------------------------------------------------------------------
    # §74: Staging subset — Checkout -> Inventory -> Redis (no Payment).
    # ------------------------------------------------------------------
    staging_components: dict[str, SystemComponent] = {}
    staging_ids = {
        "checkout_service": uuid.UUID("38000000-0000-0000-0000-00000000a001"),
        "inventory_service": uuid.UUID("38000000-0000-0000-0000-00000000a002"),
        "redis": uuid.UUID("38000000-0000-0000-0000-00000000a003"),
    }
    for key, name, ctype in (
        ("checkout_service", "Checkout Service", ComponentCategory.SERVICE),
        ("inventory_service", "Inventory Service", ComponentCategory.SERVICE),
        ("redis", "Redis", ComponentCategory.CACHE),
    ):
        comp = SystemComponent(
            id=staging_ids[key],
            project_id=project.id,
            environment_id=staging.id,
            name=name,
            component_type=ctype,
            description=f"{name} (staging mirror)",
            status=ComponentStatus.HEALTHY,
        )
        db.add(comp)
        staging_components[key] = comp
    await db.flush()

    staging_deps = [
        ComponentDependency(
            id=uuid.UUID("48000000-0000-0000-0000-00000000a001"),
            source_component_id=staging_components["checkout_service"].id,
            target_component_id=staging_components["inventory_service"].id,
            dependency_type=DependencyType.HTTP,
            description="Staging: Checkout calls Inventory",
        ),
        ComponentDependency(
            id=uuid.UUID("48000000-0000-0000-0000-00000000a002"),
            source_component_id=staging_components["checkout_service"].id,
            target_component_id=staging_components["redis"].id,
            dependency_type=DependencyType.CACHE,
            description="Staging: Checkout uses Redis",
        ),
    ]
    for dep in staging_deps:
        db.add(dep)
    await db.flush()

    # ------------------------------------------------------------------
    # §72: External Payment API — production-only EXTERNAL_API dependency.
    # ------------------------------------------------------------------
    external_api = SystemComponent(
        id=uuid.UUID("30000000-0000-0000-0000-00000000a008"),
        project_id=project.id,
        environment_id=prod.id,
        name="External Payment API",
        component_type=ComponentCategory.EXTERNAL_API,
        description="Third-party payment processor API",
        status=ComponentStatus.UNKNOWN,
        metadata_={"repository_url": "https://github.com/piyush06singhal/argus-api"},
    )
    db.add(external_api)
    await db.flush()
    db.add(
        ComponentDependency(
            id=uuid.UUID("40000000-0000-0000-0000-00000000a008"),
            source_component_id=components["payment_service"].id,
            target_component_id=external_api.id,
            dependency_type=DependencyType.HTTP,
            description="Payment Service calls the External Payment API",
        )
    )
    await db.flush()

    # ------------------------------------------------------------------
    # §21: Code repositories -> IMPLEMENTS edges (component metadata match).
    # ------------------------------------------------------------------
    repositories = {
        "argus-api": CodeRepository(
            id=uuid.UUID("c0000000-0000-0000-0000-00000000a001"),
            project_id=project.id,
            provider="github",
            repository_url="https://github.com/piyush06singhal/argus-api",
            default_branch="main",
            connection_status="CONNECTED",
        ),
        "argus-web": CodeRepository(
            id=uuid.UUID("c0000000-0000-0000-0000-00000000a002"),
            project_id=project.id,
            provider="github",
            repository_url="https://github.com/piyush06singhal/argus-web",
            default_branch="main",
            connection_status="CONNECTED",
        ),
    }
    for repo in repositories.values():
        db.add(repo)
    # Components declare their repository_url in metadata so the extractor
    # can link repo -> component deterministically.
    repo_for_component = {
        "api_gateway": "argus-api",
        "checkout_service": "argus-api",
        "inventory_service": "argus-api",
        "payment_service": "argus-api",
        "web_frontend": "argus-web",
    }
    for comp_key, repo_key in repo_for_component.items():
        comp = components[comp_key]
        comp.metadata_ = {
            **(comp.metadata_ or {}),
            "repository_url": repositories[repo_key].repository_url,
        }
    await db.flush()

    # ------------------------------------------------------------------
    # §16/§17: Service endpoints (normalized via the real registry).
    # ------------------------------------------------------------------
    endpoints = EndpointRegistry(db)
    seeded_endpoints = [
        ("api_gateway", "GET", "/api/v1/routes"),
        ("api_gateway", "POST", "/api/v1/checkout"),
        ("checkout_service", "POST", "/api/checkout"),
        ("checkout_service", "GET", "/api/checkout/12345"),
        ("inventory_service", "GET", "/api/inventory/42"),
        ("inventory_service", "POST", "/api/inventory/42/stock"),
        ("payment_service", "POST", "/api/payments"),
    ]
    for comp_key, method, path in seeded_endpoints:
        await endpoints.record_endpoint(
            project_id=project.id,
            component_id=components[comp_key].id,
            method=method,
            path=path,
            environment_id=prod.id,
        )
    await db.flush()

    # ------------------------------------------------------------------
    # §34: Component ownership (explicit configuration only).
    # ------------------------------------------------------------------
    registry = ComponentRegistry(db)
    owners = {
        "web_frontend": (
            "Frontend Platform",
            "Ada Lovelace",
            "ada@argus.dev",
            "argus-web",
        ),
        "api_gateway": ("Platform", "Grace Hopper", "grace@argus.dev", "argus-api"),
        "checkout_service": ("Commerce", "Alan Turing", "alan@argus.dev", "argus-api"),
        "inventory_service": (
            "Commerce",
            "Edsger Dijkstra",
            "edsger@argus.dev",
            "argus-api",
        ),
        "payment_service": (
            "Payments",
            "Barbara Liskov",
            "barbara@argus.dev",
            "argus-api",
        ),
    }
    for comp_key, (team, owner, email, repo_owner) in owners.items():
        await registry.set_owner(
            components[comp_key].id,
            OwnerCreate(
                team=team,
                owner_name=owner,
                contact_email=email,
                repository_owner=repo_owner,
            ),
        )
    await db.flush()

    # ------------------------------------------------------------------
    # §14: Explicit aliases — one deliberately conflicting (data-quality demo).
    # ------------------------------------------------------------------
    checkout_node = await registry.get_or_create_component_node(
        components["checkout_service"]
    )
    for alias in ("checkout-service", "checkout_api", "service-checkout"):
        await registry.add_alias(
            checkout_node.id,
            alias,
            project_id=project.id,
            source="CONFIGURATION",
            confidence=1.0,
        )
    # 'order-service' also points at Inventory -> alias conflict WARNING.
    inventory_node = await registry.get_or_create_component_node(
        components["inventory_service"]
    )
    await registry.add_alias(
        checkout_node.id,
        "order-service",
        project_id=project.id,
        source="INFERENCE",
        confidence=0.5,
    )
    await registry.add_alias(
        inventory_node.id,
        "order-service",
        project_id=project.id,
        source="CONFIGURATION",
        confidence=1.0,
    )
    await db.flush()

    # ------------------------------------------------------------------
    # §73: Span metadata for endpoint capture (http.route on gateway span).
    # ------------------------------------------------------------------
    spans_extra = [
        SpanRecord(
            id=uuid.UUID("70000000-0000-0000-0000-00000000a009"),
            trace_id=trace_id_1,
            span_id="span-1-6",
            parent_span_id="span-1-5",
            project_id=project.id,
            component_id=external_api.id,
            operation="POST /api/payments",
            start_time=base_time - timedelta(minutes=30) + timedelta(seconds=6),
            end_time=base_time - timedelta(minutes=30) + timedelta(seconds=14),
            duration_ms=8000,
            status=TraceStatus.OK,
            metadata_={"http.method": "POST", "http.route": "/api/payments"},
        ),
        SpanRecord(
            id=uuid.UUID("70000000-0000-0000-0000-00000000a010"),
            trace_id=trace_id_1,
            span_id="span-1-7",
            parent_span_id="span-1-3",
            project_id=project.id,
            component_id=components["redis"].id,
            operation="cache.get session",
            start_time=base_time - timedelta(minutes=30) + timedelta(seconds=6),
            end_time=base_time - timedelta(minutes=30) + timedelta(seconds=7),
            duration_ms=1000,
            status=TraceStatus.OK,
            metadata_={},
        ),
    ]
    for span in spans_extra:
        db.add(span)
    await db.flush()

    # ------------------------------------------------------------------
    # §38: Run the graph pipeline so a fresh build shows a populated graph.
    # ------------------------------------------------------------------
    reconciler = GraphReconciler(db, registry)
    extractor = GraphExtractor(db, registry, reconciler=reconciler)
    all_spans = (
        (
            await db.execute(
                select(SpanRecord).where(SpanRecord.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    await extractor.extract_trace_graph(
        project_id=project.id, environment_id=None, spans=all_spans
    )
    await extractor.extract_log_references(project_id=project.id)
    await extractor.extract_deployment_edges(project_id=project.id)
    await extractor.extract_repository_edges(project_id=project.id)
    await extractor.extract_project_container(project.id)
    await reconciler.reconcile(project_id=project.id)

    # ------------------------------------------------------------------
    # §24: Snapshot v1 of the freshly built graph.
    # ------------------------------------------------------------------
    snapshot_service = GraphSnapshotService(db)
    snapshot = await snapshot_service.create_snapshot(
        project_id=project.id,
        environment_id=None,
        source="MANUAL",
        caption="Initial seeded architecture (Phase 2 demo)",
    )
    await db.flush()

    return {
        "nodes": await db.scalar(
            select(func.count())
            .select_from(GraphNode)
            .where(GraphNode.project_id == project.id)
        )
        or 0,
        "edges": await db.scalar(
            select(func.count())
            .select_from(GraphEdge)
            .where(GraphEdge.project_id == project.id)
        )
        or 0,
        "endpoints": len(seeded_endpoints),
        "snapshots": snapshot.snapshot_version,
    }


def main() -> int:
    """Boot-time seeding entry point (``python seed_data.py``).

    Honours ``SEED_DEMO`` (see :attr:`Settings.seed_demo_enabled`): unset means
    "seed in development, skip in production", so a production deployment never
    gets the ARGUS Demo Commerce project unless an operator explicitly asks for
    it. Skipping is logged loudly rather than silently, because "the UI is
    empty" should be an explained state and not a mystery.
    """
    if not settings.seed_demo_enabled:
        print(
            "argus-api: SEED_DEMO is off for "
            f"API_ENVIRONMENT={settings.API_ENVIRONMENT} — skipping the demo dataset."
        )
        return 0
    asyncio.run(seed_demo_data())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
