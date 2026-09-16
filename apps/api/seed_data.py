"""ARGUS Seed Data Script."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory, init_db
from app.models.deployment import DeploymentEvent, DeploymentStatus
from app.models.incident import EvidenceType, Incident, IncidentEvidence, IncidentSeverity, IncidentStatus
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
from app.models.system import ComponentCategory, ComponentDependency, ComponentStatus, DependencyType, SystemComponent


SEED_PROJECT_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")


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
            id=uuid.UUID("10000000-0000-0000-0000-000000000001"),
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
                id=uuid.UUID("20000000-0000-0000-0000-000000000001"),
                project_id=project.id,
                name="Production",
                environment_type=EnvironmentType.PRODUCTION,
                description="Production environment",
            ),
            "staging": Environment(
                id=uuid.UUID("20000000-0000-0000-0000-000000000002"),
                project_id=project.id,
                name="Staging",
                environment_type=EnvironmentType.STAGING,
                description="Staging environment",
            ),
            "development": Environment(
                id=uuid.UUID("20000000-0000-0000-0000-000000000003"),
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
                id=uuid.UUID("30000000-0000-0000-0000-000000000001"),
                project_id=project.id,
                environment_id=environments["production"].id,
                name="Web Frontend",
                component_type=ComponentCategory.FRONTEND,
                description="React-based web frontend",
                status=ComponentStatus.HEALTHY,
            ),
            "api_gateway": SystemComponent(
                id=uuid.UUID("30000000-0000-0000-0000-000000000002"),
                project_id=project.id,
                environment_id=environments["production"].id,
                name="API Gateway",
                component_type=ComponentCategory.SERVICE,
                description="Nginx-based API gateway and load balancer",
                status=ComponentStatus.HEALTHY,
            ),
            "checkout_service": SystemComponent(
                id=uuid.UUID("30000000-0000-0000-0000-000000000003"),
                project_id=project.id,
                environment_id=environments["production"].id,
                name="Checkout Service",
                component_type=ComponentCategory.SERVICE,
                description="Handles checkout and order processing",
                status=ComponentStatus.DEGRADED,
            ),
            "inventory_service": SystemComponent(
                id=uuid.UUID("30000000-0000-0000-0000-000000000004"),
                project_id=project.id,
                environment_id=environments["production"].id,
                name="Inventory Service",
                component_type=ComponentCategory.SERVICE,
                description="Manages product inventory and stock levels",
                status=ComponentStatus.UNHEALTHY,
            ),
            "payment_service": SystemComponent(
                id=uuid.UUID("30000000-0000-0000-0000-000000000005"),
                project_id=project.id,
                environment_id=environments["production"].id,
                name="Payment Service",
                component_type=ComponentCategory.SERVICE,
                description="Processes payment transactions",
                status=ComponentStatus.HEALTHY,
            ),
            "postgresql": SystemComponent(
                id=uuid.UUID("30000000-0000-0000-0000-000000000006"),
                project_id=project.id,
                environment_id=environments["production"].id,
                name="PostgreSQL",
                component_type=ComponentCategory.DATABASE,
                description="Primary relational database",
                status=ComponentStatus.DEGRADED,
            ),
            "redis": SystemComponent(
                id=uuid.UUID("30000000-0000-0000-0000-000000000007"),
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
                id=uuid.UUID("40000000-0000-0000-0000-000000000001"),
                source_component_id=components["web_frontend"].id,
                target_component_id=components["api_gateway"].id,
                dependency_type=DependencyType.HTTP,
                description="Frontend calls API Gateway",
            ),
            ComponentDependency(
                id=uuid.UUID("40000000-0000-0000-0000-000000000002"),
                source_component_id=components["api_gateway"].id,
                target_component_id=components["checkout_service"].id,
                dependency_type=DependencyType.HTTP,
                description="API Gateway routes to Checkout Service",
            ),
            ComponentDependency(
                id=uuid.UUID("40000000-0000-0000-0000-000000000003"),
                source_component_id=components["checkout_service"].id,
                target_component_id=components["inventory_service"].id,
                dependency_type=DependencyType.HTTP,
                description="Checkout Service calls Inventory Service",
            ),
            ComponentDependency(
                id=uuid.UUID("40000000-0000-0000-0000-000000000004"),
                source_component_id=components["checkout_service"].id,
                target_component_id=components["payment_service"].id,
                dependency_type=DependencyType.HTTP,
                description="Checkout Service calls Payment Service",
            ),
            ComponentDependency(
                id=uuid.UUID("40000000-0000-0000-0000-000000000005"),
                source_component_id=components["checkout_service"].id,
                target_component_id=components["redis"].id,
                dependency_type=DependencyType.CACHE,
                description="Checkout Service uses Redis for caching",
            ),
            ComponentDependency(
                id=uuid.UUID("40000000-0000-0000-0000-000000000006"),
                source_component_id=components["inventory_service"].id,
                target_component_id=components["postgresql"].id,
                dependency_type=DependencyType.DATABASE,
                description="Inventory Service reads from PostgreSQL",
            ),
            ComponentDependency(
                id=uuid.UUID("40000000-0000-0000-0000-000000000007"),
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
                id=uuid.UUID("50000000-0000-0000-0000-000000000001"),
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
                id=uuid.UUID("50000000-0000-0000-0000-000000000002"),
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
                id=uuid.UUID("60000000-0000-0000-0000-000000000001"),
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
                id=uuid.UUID("60000000-0000-0000-0000-000000000002"),
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
                id=uuid.UUID("70000000-0000-0000-0000-000000000001"),
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
                id=uuid.UUID("70000000-0000-0000-0000-000000000002"),
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
                id=uuid.UUID("70000000-0000-0000-0000-000000000003"),
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
                id=uuid.UUID("70000000-0000-0000-0000-000000000004"),
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
                id=uuid.UUID("70000000-0000-0000-0000-000000000005"),
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
                id=uuid.UUID("70000000-0000-0000-0000-000000000006"),
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
                id=uuid.UUID("70000000-0000-0000-0000-000000000007"),
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
                id=uuid.UUID("70000000-0000-0000-0000-000000000008"),
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
                id=uuid.UUID("80000000-0000-0000-0000-000000000001"),
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
                id=uuid.UUID("80000000-0000-0000-0000-000000000002"),
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
                id=uuid.UUID("80000000-0000-0000-0000-000000000003"),
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
                id=uuid.UUID("80000000-0000-0000-0000-000000000004"),
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
                id=uuid.UUID("80000000-0000-0000-0000-000000000005"),
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
                id=uuid.UUID("90000000-0000-0000-0000-000000000001"),
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
                id=uuid.UUID("90000000-0000-0000-0000-000000000002"),
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
                id=uuid.UUID("90000000-0000-0000-0000-000000000003"),
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
                id=uuid.UUID("90000000-0000-0000-0000-000000000004"),
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
                id=uuid.UUID("90000000-0000-0000-0000-000000000005"),
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
            id=uuid.UUID("a0000000-0000-0000-0000-000000000001"),
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

        # Create evidence
        evidence_items = [
            IncidentEvidence(
                id=uuid.UUID("b0000000-0000-0000-0000-000000000001"),
                incident_id=str(incident.id),
                evidence_type=EvidenceType.DEPLOYMENT,
                source_id=str(deployments[0].id),
                timestamp=deployments[0].deployed_at,
                relevance_score=0.9,
                description="Inventory Service v2.3.1 deployed 45 minutes before incident started",
            ),
            IncidentEvidence(
                id=uuid.UUID("b0000000-0000-0000-0000-000000000002"),
                incident_id=str(incident.id),
                evidence_type=EvidenceType.LOG,
                source_id=str(logs[2].id),
                timestamp=logs[2].timestamp,
                relevance_score=0.95,
                description="Database connection pool exhausted in Inventory Service",
            ),
            IncidentEvidence(
                id=uuid.UUID("b0000000-0000-0000-0000-000000000003"),
                incident_id=str(incident.id),
                evidence_type=EvidenceType.METRIC,
                source_id=str(metrics[0].id),
                timestamp=metrics[0].timestamp,
                relevance_score=0.85,
                description="Database connection pool at maximum capacity (25/25)",
            ),
            IncidentEvidence(
                id=uuid.UUID("b0000000-0000-0000-0000-000000000004"),
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
        print("  Incidents: 1")
        print(f"  Evidence items: {len(evidence_items)}")


if __name__ == "__main__":
    asyncio.run(seed_demo_data())
