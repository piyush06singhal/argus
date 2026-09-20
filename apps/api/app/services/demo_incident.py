"""ARGUS Checkout Latency Incident — deterministic demo (§49–§50).

Generates the exact scenario from the Phase 3 specification and runs detection
plus correlation over it, producing a real incident built entirely from stored
evidence:

```
T-2m   deployment published
T-40s  Checkout p95 latency 220ms  -> 890ms
T-35s  Inventory dependency latency 150ms -> 620ms
T-30s  Checkout error rate 0.8% -> 7.2%
T-2m..  of traces fail (ERROR/TIMEOUT)
T-25s  Checkout health HEALTHY -> DEGRADED
T-90s  configuration change recorded
```

Design decisions worth stating explicitly:

* **Relative timing, absolute reproducibility.** The scenario is anchored to the
  detection instant ``T`` rather than a wall-clock time, so it produces the same
  incident whenever it runs. Every offset is fixed, so the result is
  deterministic; pass ``now`` to reproduce an exact instant.
* **Real pipeline, no shortcuts.** Nothing is written directly into
  ``incidents``. Telemetry is ingested, rules are evaluated by the real
  detection service, and the incident is created by the real correlation engine.
  If the detector regresses, the demo fails — which is the point.
* **Deterministic ids.** Rule ids are fixed UUIDs so a re-run reproduces the same
  fingerprints and therefore the same incident fingerprint.
* **No causality.** The deployment is attached as temporal context with the
  explicit "does not establish that the deployment caused the incident" wording.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc_or_now
from app.models.anomaly import (
    Anomaly,
    AnomalyFingerprint,
    AnomalyObservation,
    AnomalyRule,
    AnomalySeverity,
    AnomalyType,
    BaselineStrategy,
    RuleCondition,
)
from app.models.deployment import DeploymentEvent, DeploymentStatus
from app.models.incident import Incident, IncidentEvidence, IncidentTimelineEvent
from app.models.ingestion import (
    ConfigurationChangeEvent,
    HealthCheckEvent,
    HealthStatus,
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
from app.models.project import Environment, SoftwareProject
from app.models.system import SystemComponent
from app.services.anomaly_detection import AnomalyDetectionService
from app.services.fingerprints import anomaly_fingerprint
from app.services.incident_manager import IncidentManager

#: Fixed rule ids — stable across runs, so fingerprints (and therefore the
#: incident fingerprint) are reproducible.
RULE_IDS = {
    "checkout_latency": uuid.UUID("50000000-0000-0000-0000-00000000b001"),
    "checkout_error_rate": uuid.UUID("50000000-0000-0000-0000-00000000b002"),
    "inventory_latency": uuid.UUID("50000000-0000-0000-0000-00000000b003"),
    "trace_failures": uuid.UUID("50000000-0000-0000-0000-00000000b004"),
    "checkout_health": uuid.UUID("50000000-0000-0000-0000-00000000b005"),
    "connection_refused": uuid.UUID("50000000-0000-0000-0000-00000000b006"),
    "db_latency": uuid.UUID("50000000-0000-0000-0000-00000000b007"),
}

#: Metric names used by the scenario — referenced by the rules below and by the
#: evaluation expectations in tests.
METRIC_CHECKOUT_P95 = "http.checkout.latency.p95"
METRIC_CHECKOUT_ERROR_RATE = "http.checkout.error_rate"
METRIC_INVENTORY_P95 = "http.inventory.latency.p95"
#: Phase 4 §48: the datastore at the root of the story must be *observable*, or
#: the causal engine would be reasoning about a component with no telemetry.
METRIC_DB_LATENCY = "db.query.latency.p95"

CHECKOUT_P95_BASELINE = 220.0
CHECKOUT_P95_SPIKE = 890.0
CHECKOUT_ERROR_BASELINE = 0.008
CHECKOUT_ERROR_SPIKE = 0.072
INVENTORY_P95_BASELINE = 150.0
INVENTORY_P95_SPIKE = 620.0
DB_LATENCY_BASELINE = 18.0
DB_LATENCY_SPIKE = 640.0

CONNECTION_REFUSED_MESSAGE = "upstream connection refused calling inventory-service"
DB_POOL_EXHAUSTED_MESSAGE = "query timeout after 5000ms on postgres primary"

RULE_WINDOW_SECONDS = 600

#: Marker prefixes/values used to scope cleanup to *demo* rows only, so
#: ``--force`` can never delete real ingested telemetry.
DEMO_TRACE_PREFIX = "demo-trace-"
DEMO_DEPLOYMENT_ID = "deploy-checkout-20260919-1420"
DEMO_CONFIG_CHANGE_ID = "cfg-checkout-pool-9"
DEMO_METRICS = (
    METRIC_CHECKOUT_P95,
    METRIC_CHECKOUT_ERROR_RATE,
    METRIC_INVENTORY_P95,
    METRIC_DB_LATENCY,
)
#: Keys the datastore may be registered under. The seeded production topology
#: calls it ``postgresql``; scenarios that build their own topology use
#: ``inventory_db``. Both are the same role in the story.
_DATASTORE_KEYS = ("postgresql", "inventory_db", "database", "postgres")

INVENTORY_TIMEOUT_MESSAGE = "inventory-service read timed out after 2000ms"
DEMO_LOG_MESSAGES = (
    CONNECTION_REFUSED_MESSAGE,
    "checkout retry exhausted after 3 attempts",
    DB_POOL_EXHAUSTED_MESSAGE,
    INVENTORY_TIMEOUT_MESSAGE,
)
#: Span ids are prefixed so a ``--force`` re-seed can remove exactly its own
#: span tree without touching really-ingested traces.
DEMO_SPAN_PREFIX = "demo-span-"


#: Rule fields a ``--force`` re-seed realigns with the canonical definition.
#: Identity (``id``/``project_id``) and timestamps are deliberately excluded.
_REFRESHABLE_RULE_FIELDS = (
    "environment_id",
    "component_id",
    "name",
    "description",
    "anomaly_type",
    "condition",
    "metric_name",
    "baseline_strategy",
    "expected_value",
    "threshold",
    "multiplier",
    "z_threshold",
    "min_samples",
    "window_seconds",
    "cooldown_seconds",
    "persistence_cycles",
    "severity",
    "severity_policy",
    "enabled",
    "created_by",
)


def _datastore_component(
    components: dict[str, SystemComponent],
) -> Optional[SystemComponent]:
    """Resolve the datastore role across topologies (see ``_DATASTORE_KEYS``)."""
    for key in _DATASTORE_KEYS:
        component = components.get(key)
        if component is not None:
            return component
    return None


def build_rules(
    *,
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    components: dict[str, SystemComponent],
) -> list[AnomalyRule]:
    """The seven deterministic rules the scenario is detected by (§18).

    Phase 4 adds a datastore rule: without observable database degradation the
    causal engine would have to guess at the root of the chain instead of
    reading it (§48).
    """
    checkout = components["checkout_service"].id
    inventory = components["inventory_service"].id
    datastore = _datastore_component(components)
    if datastore is None:
        raise ValueError(
            "the demo scenario needs a datastore component "
            f"(one of {', '.join(_DATASTORE_KEYS)})"
        )
    postgres = datastore.id
    return [
        AnomalyRule(
            id=RULE_IDS["db_latency"],
            project_id=project_id,
            environment_id=environment_id,
            component_id=postgres,
            name="Database Query Latency",
            description="Query p95 latency above the datastore's configured ceiling",
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            condition=RuleCondition.THRESHOLD,
            metric_name=METRIC_DB_LATENCY,
            baseline_strategy=BaselineStrategy.STATIC,
            expected_value=DB_LATENCY_BASELINE,
            threshold=300.0,
            min_samples=3,
            window_seconds=RULE_WINDOW_SECONDS,
            cooldown_seconds=0,
            persistence_cycles=1,
            severity=AnomalySeverity.HIGH,
            enabled=True,
            created_by="demo-seed",
        ),
        AnomalyRule(
            id=RULE_IDS["checkout_latency"],
            project_id=project_id,
            environment_id=environment_id,
            component_id=checkout,
            name="Checkout P95 Latency",
            description="Checkout p95 latency above the configured ceiling",
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            condition=RuleCondition.THRESHOLD,
            metric_name=METRIC_CHECKOUT_P95,
            baseline_strategy=BaselineStrategy.STATIC,
            expected_value=CHECKOUT_P95_BASELINE,
            threshold=500.0,
            min_samples=3,
            window_seconds=RULE_WINDOW_SECONDS,
            cooldown_seconds=0,
            persistence_cycles=1,
            severity=AnomalySeverity.HIGH,
            enabled=True,
            created_by="demo-seed",
        ),
        AnomalyRule(
            id=RULE_IDS["checkout_error_rate"],
            project_id=project_id,
            environment_id=environment_id,
            component_id=checkout,
            name="Checkout Error Rate",
            description="Checkout error rate above the configured ceiling",
            anomaly_type=AnomalyType.ERROR_RATE_SPIKE,
            condition=RuleCondition.THRESHOLD,
            metric_name=METRIC_CHECKOUT_ERROR_RATE,
            baseline_strategy=BaselineStrategy.STATIC,
            expected_value=CHECKOUT_ERROR_BASELINE,
            threshold=0.05,
            min_samples=3,
            window_seconds=RULE_WINDOW_SECONDS,
            cooldown_seconds=0,
            persistence_cycles=1,
            severity=AnomalySeverity.HIGH,
            enabled=True,
            created_by="demo-seed",
        ),
        AnomalyRule(
            id=RULE_IDS["inventory_latency"],
            project_id=project_id,
            environment_id=environment_id,
            component_id=inventory,
            name="Inventory Dependency Latency",
            description=(
                "Inventory p95 latency deviating from its rolling baseline "
                "(comparison against this system's own normal)"
            ),
            anomaly_type=AnomalyType.METRIC_BASELINE_DEVIATION,
            condition=RuleCondition.BASELINE_DEVIATION,
            metric_name=METRIC_INVENTORY_P95,
            baseline_strategy=BaselineStrategy.ROLLING,
            multiplier=2.5,
            min_samples=5,
            window_seconds=RULE_WINDOW_SECONDS,
            cooldown_seconds=0,
            persistence_cycles=1,
            severity=AnomalySeverity.MEDIUM,
            enabled=True,
            created_by="demo-seed",
        ),
        AnomalyRule(
            id=RULE_IDS["trace_failures"],
            project_id=project_id,
            environment_id=environment_id,
            component_id=checkout,
            name="Checkout Trace Failure Rate",
            description="Share of failing checkout traces above the ceiling",
            anomaly_type=AnomalyType.TRACE_FAILURE_SPIKE,
            condition=RuleCondition.TRACE_FAILURE_RATE,
            metric_name="trace.checkout.failure_rate",
            baseline_strategy=BaselineStrategy.STATIC,
            threshold=0.15,
            min_samples=5,
            window_seconds=RULE_WINDOW_SECONDS,
            cooldown_seconds=0,
            persistence_cycles=1,
            severity=AnomalySeverity.HIGH,
            enabled=True,
            created_by="demo-seed",
        ),
        AnomalyRule(
            id=RULE_IDS["checkout_health"],
            project_id=project_id,
            environment_id=environment_id,
            component_id=checkout,
            name="Checkout Health Degradation",
            description="Checkout health transitioned away from HEALTHY",
            anomaly_type=AnomalyType.HEALTH_DEGRADATION,
            condition=RuleCondition.HEALTH_TRANSITION,
            metric_name="health.checkout.state",
            baseline_strategy=BaselineStrategy.STATIC,
            min_samples=2,
            window_seconds=RULE_WINDOW_SECONDS,
            cooldown_seconds=0,
            persistence_cycles=1,
            severity=AnomalySeverity.MEDIUM,
            enabled=True,
            created_by="demo-seed",
        ),
        AnomalyRule(
            id=RULE_IDS["connection_refused"],
            project_id=project_id,
            environment_id=environment_id,
            component_id=checkout,
            name="Connection Refused Log Spike",
            description="Spike in 'connection refused' log patterns",
            anomaly_type=AnomalyType.LOG_PATTERN_SPIKE,
            condition=RuleCondition.PATTERN_SPIKE,
            metric_name="connection refused",
            baseline_strategy=BaselineStrategy.ROLLING,
            multiplier=3.0,
            min_samples=2,
            window_seconds=RULE_WINDOW_SECONDS,
            cooldown_seconds=0,
            persistence_cycles=1,
            severity=AnomalySeverity.MEDIUM,
            enabled=True,
            created_by="demo-seed",
        ),
    ]


async def _clear_demo_detections(
    db: AsyncSession, *, project_id: uuid.UUID, environment_id: uuid.UUID
) -> dict[str, int]:
    """Remove the anomalies/incidents this demo previously produced.

    A re-anchored run must be *coherent*: if the telemetry moves to the new
    ``now`` but the anomalies keep their old timestamps, the deployment context
    silently drifts ("74 seconds before" instead of the story's two minutes).
    Re-anchoring therefore rebuilds the detections too.

    Scoped strictly to demo-owned rows: anomalies must reference one of the
    demo's fixed rule ids, and a deleted incident must have *only* demo
    anomalies, so a real incident can never be removed by a demo re-run.
    """
    removed: dict[str, int] = {}
    rule_ids = list(RULE_IDS.values())

    anomalies = list(
        (await db.execute(select(Anomaly).where(Anomaly.rule_id.in_(rule_ids))))
        .scalars()
        .all()
    )
    anomaly_ids = [a.id for a in anomalies]
    incident_ids = sorted({a.incident_id for a in anomalies if a.incident_id})

    # Only delete an incident that is entirely demo-owned.
    deletable: list[uuid.UUID] = []
    for incident_id in incident_ids:
        foreign = await db.scalar(
            select(func.count())
            .select_from(Anomaly)
            .where(
                Anomaly.incident_id == incident_id,
                Anomaly.rule_id.notin_(rule_ids),
            )
        )
        if not foreign:
            deletable.append(incident_id)

    if deletable:
        for entity in (IncidentTimelineEvent, IncidentEvidence):
            rows = list(
                (
                    await db.execute(
                        select(entity).where(entity.incident_id.in_(deletable))
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                await db.delete(row)
            removed.setdefault("incident_children", 0)
            removed["incident_children"] += len(rows)

        incidents = list(
            (await db.execute(select(Incident).where(Incident.id.in_(deletable))))
            .scalars()
            .all()
        )
        for incident in incidents:
            await db.delete(incident)
        removed["incidents"] = len(incidents)

    if anomaly_ids:
        observations = list(
            (
                await db.execute(
                    select(AnomalyObservation).where(
                        AnomalyObservation.anomaly_id.in_(anomaly_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        for observation in observations:
            await db.delete(observation)
        removed["anomaly_observations"] = len(observations)

        registries = list(
            (
                await db.execute(
                    select(AnomalyFingerprint).where(
                        AnomalyFingerprint.anomaly_id.in_(anomaly_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        for registry in registries:
            await db.delete(registry)
        removed["anomaly_fingerprints"] = len(registries)

        for anomaly in anomalies:
            await db.delete(anomaly)
        removed["anomalies"] = len(anomalies)

    await db.flush()
    return removed


async def _clear_demo_telemetry(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    components: dict[str, SystemComponent],
    now: datetime,
) -> dict[str, int]:
    """Remove only the telemetry this demo previously ingested.

    Every predicate is scoped to a demo marker (metric name, log message,
    ``demo-trace-`` prefix, demo deployment/change id) *and* the project, so a
    re-run can never delete genuinely ingested data. Without this, ``--force``
    would collide with the fixed trace ids it deliberately reuses.
    """
    checkout = components["checkout_service"].id
    inventory = components["inventory_service"].id
    removed: dict[str, int] = {}

    async def purge(stmt, label: str) -> None:
        rows = list((await db.execute(stmt)).scalars().all())
        for row in rows:
            await db.delete(row)
        removed[label] = len(rows)

    await purge(
        select(MetricRecord).where(
            MetricRecord.project_id == project_id,
            MetricRecord.environment_id == environment_id,
            MetricRecord.metric_name.in_(DEMO_METRICS),
        ),
        "metrics",
    )
    await purge(
        select(LogRecord).where(
            LogRecord.project_id == project_id,
            LogRecord.message.in_(DEMO_LOG_MESSAGES),
        ),
        "logs",
    )
    await purge(
        select(SpanRecord).where(
            SpanRecord.project_id == project_id,
            SpanRecord.span_id.startswith(DEMO_SPAN_PREFIX),
        ),
        "spans",
    )
    await purge(
        select(TraceRecord).where(
            TraceRecord.project_id == project_id,
            TraceRecord.trace_id.startswith(DEMO_TRACE_PREFIX),
        ),
        "traces",
    )
    await purge(
        select(HealthCheckEvent).where(
            HealthCheckEvent.project_id == project_id,
            HealthCheckEvent.component_id.in_([checkout, inventory]),
            HealthCheckEvent.timestamp >= now - timedelta(hours=2),
        ),
        "health_checks",
    )
    await purge(
        select(DeploymentEvent).where(
            DeploymentEvent.project_id == project_id,
            DeploymentEvent.deployment_id == DEMO_DEPLOYMENT_ID,
        ),
        "deployments",
    )
    await purge(
        select(ConfigurationChangeEvent).where(
            ConfigurationChangeEvent.project_id == project_id,
            ConfigurationChangeEvent.change_id == DEMO_CONFIG_CHANGE_ID,
        ),
        "configuration_changes",
    )
    await db.flush()
    return removed


async def _emit_telemetry(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    components: dict[str, SystemComponent],
    now: datetime,
) -> dict[str, int]:
    """Ingest the scripted telemetry for the scenario."""
    checkout = components["checkout_service"].id
    inventory = components["inventory_service"].id
    datastore = _datastore_component(components)
    if datastore is None:
        raise ValueError(
            "the demo scenario needs a datastore component "
            f"(one of {', '.join(_DATASTORE_KEYS)})"
        )
    postgres = datastore.id
    counts = {
        "metrics": 0,
        "logs": 0,
        "traces": 0,
        "spans": 0,
        "health_checks": 0,
        "deployments": 0,
        "configuration_changes": 0,
    }

    def metric(
        name: str,
        value: float,
        at: datetime,
        component_id,
        unit: Optional[str] = None,
    ) -> None:
        db.add(
            MetricRecord(
                project_id=project_id,
                environment_id=environment_id,
                component_id=component_id,
                timestamp=at,
                metric_name=name,
                metric_type=MetricType.GAUGE,
                value=value,
                unit=unit,
                labels={"service": "checkout-service"},
            )
        )
        counts["metrics"] += 1

    # --- Baseline: the system's own normal, every 60s for 6 minutes ---------
    for offset in range(9, 3, -1):
        at = now - timedelta(minutes=offset)
        metric(METRIC_CHECKOUT_P95, CHECKOUT_P95_BASELINE, at, checkout, "ms")
        metric(METRIC_INVENTORY_P95, INVENTORY_P95_BASELINE, at, inventory, "ms")
        metric(METRIC_DB_LATENCY, DB_LATENCY_BASELINE, at, postgres, "ms")
        metric(
            METRIC_CHECKOUT_ERROR_RATE,
            CHECKOUT_ERROR_BASELINE,
            at,
            checkout,
            "ratio",
        )

    # --- The incident, upstream-first (§48) ---------------------------------
    # The datastore degrades first, then the service that reads it, then the
    # caller. The engine must *derive* that order from this telemetry — it is
    # never told which component is the root.
    metric(
        METRIC_DB_LATENCY,
        DB_LATENCY_SPIKE,
        now - timedelta(seconds=100),
        postgres,
        "ms",
    )
    for index in range(4):
        db.add(
            LogRecord(
                project_id=project_id,
                environment_id=environment_id,
                component_id=postgres,
                timestamp=now - timedelta(seconds=95) + timedelta(seconds=index * 4),
                level=Severity.ERROR,
                message=DB_POOL_EXHAUSTED_MESSAGE,
                service="postgres",
            )
        )
        counts["logs"] += 1

    metric(
        METRIC_INVENTORY_P95,
        INVENTORY_P95_SPIKE,
        now - timedelta(seconds=80),
        inventory,
        "ms",
    )
    for index in range(3):
        db.add(
            LogRecord(
                project_id=project_id,
                environment_id=environment_id,
                component_id=inventory,
                timestamp=now - timedelta(seconds=75) + timedelta(seconds=index * 5),
                level=Severity.ERROR,
                message=INVENTORY_TIMEOUT_MESSAGE,
                service="inventory-service",
            )
        )
        counts["logs"] += 1

    metric(
        METRIC_CHECKOUT_P95,
        CHECKOUT_P95_SPIKE,
        now - timedelta(seconds=50),
        checkout,
        "ms",
    )
    metric(
        METRIC_CHECKOUT_ERROR_RATE,
        CHECKOUT_ERROR_SPIKE,
        now - timedelta(seconds=30),
        checkout,
        "ratio",
    )

    # Traces: a healthy majority, then a failing tail (§14).
    for index in range(14):
        started = now - timedelta(minutes=9) + timedelta(seconds=index * 30)
        db.add(
            TraceRecord(
                project_id=project_id,
                environment_id=environment_id,
                trace_id=f"demo-trace-ok-{index:02d}",
                name="POST /api/checkout",
                start_time=started,
                end_time=started + timedelta(milliseconds=240),
                duration_ms=240.0,
                status=TraceStatus.OK,
            )
        )
        counts["traces"] += 1
    for index in range(6):
        started = now - timedelta(seconds=150) + timedelta(seconds=index * 20)
        trace_id = f"demo-trace-fail-{index:02d}"
        db.add(
            TraceRecord(
                project_id=project_id,
                environment_id=environment_id,
                trace_id=trace_id,
                name="POST /api/checkout",
                start_time=started,
                end_time=started + timedelta(milliseconds=2900),
                duration_ms=2900.0,
                status=TraceStatus.ERROR if index % 2 == 0 else TraceStatus.TIMEOUT,
            )
        )
        counts["traces"] += 1
        # The span tree is what makes direction *observable* rather than
        # guessed: the datastore call fails first, inside the inventory call,
        # inside the checkout request (§15). Removing these spans drops the
        # analysis to structural + temporal evidence only — which is exactly
        # what the confidence model reports.
        counts["spans"] += _emit_failing_span_tree(
            db,
            project_id=project_id,
            trace_id=trace_id,
            started=started,
            index=index,
            checkout=checkout,
            inventory=inventory,
            postgres=postgres,
        )

    # Logs: a couple of slow-path warnings, then a 'connection refused' spike.
    for index in range(2):
        db.add(
            LogRecord(
                project_id=project_id,
                environment_id=environment_id,
                component_id=checkout,
                timestamp=now - timedelta(minutes=6) + timedelta(seconds=index * 45),
                level=Severity.WARN,
                message="checkout retry exhausted after 3 attempts",
                service="checkout-service",
            )
        )
        counts["logs"] += 1
    for index in range(7):
        db.add(
            LogRecord(
                project_id=project_id,
                environment_id=environment_id,
                component_id=checkout,
                timestamp=now - timedelta(seconds=60) + timedelta(seconds=index * 5),
                level=Severity.ERROR,
                message=CONNECTION_REFUSED_MESSAGE,
                service="checkout-service",
            )
        )
        counts["logs"] += 1

    # Health checks: HEALTHY then DEGRADED (§15).
    db.add(
        HealthCheckEvent(
            project_id=project_id,
            environment_id=environment_id,
            component_id=checkout,
            timestamp=now - timedelta(minutes=5),
            status=HealthStatus.HEALTHY,
        )
    )
    db.add(
        HealthCheckEvent(
            project_id=project_id,
            environment_id=environment_id,
            component_id=checkout,
            timestamp=now - timedelta(seconds=25),
            status=HealthStatus.DEGRADED,
        )
    )
    counts["health_checks"] += 2

    # Deployment 2 minutes before detection — temporal context only (§31).
    db.add(
        DeploymentEvent(
            project_id=project_id,
            environment_id=environment_id,
            component_id=checkout,
            deployment_id="deploy-checkout-20260919-1420",
            version="2026.09.19-rc3",
            commit_sha="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
            deployed_at=now - timedelta(minutes=2),
            status=DeploymentStatus.SUCCESS,
            description="Deployment published 2 minutes before detection",
        )
    )
    counts["deployments"] += 1

    # Configuration change 90 seconds before detection (§32).
    db.add(
        ConfigurationChangeEvent(
            project_id=project_id,
            environment_id=environment_id,
            component_id=checkout,
            change_id="cfg-checkout-pool-9",
            timestamp=now - timedelta(seconds=90),
            source="demo-seed",
            description="Connection pool max_size changed",
        )
    )
    counts["configuration_changes"] += 1

    await db.flush()
    return counts


def _emit_failing_span_tree(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    trace_id: str,
    started: datetime,
    index: int,
    checkout: uuid.UUID,
    inventory: uuid.UUID,
    postgres: uuid.UUID,
) -> int:
    """One failing checkout request, as three nested spans.

    The database call starts and fails first; the inventory read times out
    waiting on it; the checkout request then fails. Failure onsets are
    therefore ordered datastore → service → caller *inside a single request*,
    which is the strongest directional evidence Phase 1 stores (§15, §16).
    """
    root_id = f"{DEMO_SPAN_PREFIX}root-{index:02d}"
    inventory_id = f"{DEMO_SPAN_PREFIX}inv-{index:02d}"
    db_id = f"{DEMO_SPAN_PREFIX}db-{index:02d}"
    failure_status = TraceStatus.ERROR if index % 2 == 0 else TraceStatus.TIMEOUT
    spans = [
        SpanRecord(
            project_id=project_id,
            trace_id=trace_id,
            span_id=root_id,
            parent_span_id=None,
            component_id=checkout,
            operation="POST /api/checkout",
            start_time=started,
            end_time=started + timedelta(milliseconds=2900),
            duration_ms=2900.0,
            status=failure_status,
        ),
        SpanRecord(
            project_id=project_id,
            trace_id=trace_id,
            span_id=inventory_id,
            parent_span_id=root_id,
            component_id=inventory,
            operation="GET /inventory/reserve",
            start_time=started + timedelta(milliseconds=50),
            end_time=started + timedelta(milliseconds=2750),
            duration_ms=2700.0,
            status=TraceStatus.TIMEOUT,
        ),
        SpanRecord(
            project_id=project_id,
            trace_id=trace_id,
            span_id=db_id,
            parent_span_id=inventory_id,
            component_id=postgres,
            operation="SELECT stock_levels",
            start_time=started + timedelta(milliseconds=100),
            end_time=started + timedelta(milliseconds=2600),
            duration_ms=2500.0,
            status=TraceStatus.TIMEOUT,
        ),
    ]
    for span in spans:
        db.add(span)
    return len(spans)


async def seed_checkout_incident(
    db: AsyncSession,
    *,
    project: SoftwareProject,
    environment: Environment,
    components: dict[str, SystemComponent],
    now: Optional[datetime] = None,
    force: bool = False,
) -> dict:
    """Create the demo scenario and return a summary of what happened.

    Idempotent: a second call is a no-op unless ``force`` is set, and even then
    the same fingerprints and timestamps mean the incident is reused rather than
    duplicated.
    """
    reference = ensure_utc_or_now(now)

    if not force:
        existing = await db.scalar(
            select(func.count())
            .select_from(AnomalyRule)
            .where(AnomalyRule.project_id == project.id)
        )
        if existing and existing > 0:
            return {"skipped": True, "reason": "demo rules already present"}

    rules = build_rules(
        project_id=project.id,
        environment_id=environment.id,
        components=components,
    )
    existing_ids = set(
        (
            await db.execute(
                select(AnomalyRule.id).where(
                    AnomalyRule.id.in_([rule.id for rule in rules])
                )
            )
        )
        .scalars()
        .all()
    )
    for rule in rules:
        if rule.id in existing_ids:
            if not force:
                continue
            # ``--force`` *reconciles* rather than skips. Rules are keyed by
            # fixed demo ids, so skipping a pre-existing one would pin the
            # scenario to whatever scope an earlier run used — a corrected
            # re-seed could never repair it.
            persisted = await db.get(AnomalyRule, rule.id)
            if persisted is not None:
                for field in _REFRESHABLE_RULE_FIELDS:
                    setattr(persisted, field, getattr(rule, field))
        else:
            db.add(rule)
    await db.flush()

    cleared: dict[str, int] = {}
    detections_cleared: dict[str, int] = {}
    if force:
        # Detections first: their fingerprints referenced the old telemetry.
        detections_cleared = await _clear_demo_detections(
            db, project_id=project.id, environment_id=environment.id
        )
        cleared = await _clear_demo_telemetry(
            db,
            project_id=project.id,
            environment_id=environment.id,
            components=components,
            now=reference,
        )

    telemetry = await _emit_telemetry(
        db,
        project_id=project.id,
        environment_id=environment.id,
        components=components,
        now=reference,
    )

    detection = await AnomalyDetectionService(db, now=reference).run(
        project_id=project.id, environment_id=environment.id
    )
    correlation = await IncidentManager(db, now=reference).process_scope(
        project_id=project.id, environment_id=environment.id
    )

    incident = (
        await db.execute(
            select(Incident)
            .where(Incident.project_id == project.id)
            .order_by(Incident.detected_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    anomalies = list(
        (
            await db.execute(
                select(Anomaly)
                .where(Anomaly.project_id == project.id)
                .order_by(Anomaly.detected_at)
            )
        )
        .scalars()
        .all()
    )

    return {
        "skipped": False,
        "rules": len(rules),
        "telemetry_purged": cleared,
        "detections_purged": detections_cleared,
        "telemetry": telemetry,
        "detection": detection.as_dict(),
        "correlation": correlation.as_dict(),
        "incident_id": str(incident.id) if incident else None,
        "incident_severity": (
            incident.severity.value
            if incident and hasattr(incident.severity, "value")
            else None
        ),
        "anomalies": [
            {
                "id": str(a.id),
                "type": a.anomaly_type.value
                if hasattr(a.anomaly_type, "value")
                else str(a.anomaly_type),
                "severity": a.severity.value
                if hasattr(a.severity, "value")
                else str(a.severity),
                "observed": a.observed_value,
                "expected": a.expected_value,
                "component_id": str(a.component_id) if a.component_id else None,
                "fingerprint": a.fingerprint,
            }
            for a in anomalies
        ],
    }


async def expected_demo_fingerprints(
    *,
    project_id: uuid.UUID,
    environment_id: uuid.UUID,
    components: dict[str, SystemComponent],
) -> dict[str, str]:
    """The fingerprints the scenario must produce — used by tests and the smoke.

    Exposed so a regression in the fingerprinting scheme is caught as a broken
    demo rather than a silently shifted incident.
    """
    checkout = components["checkout_service"].id
    inventory = components["inventory_service"].id
    datastore = _datastore_component(components)
    postgres = datastore.id if datastore is not None else None
    if postgres is None:
        raise ValueError("the demo scenario needs a datastore component")
    return {
        "db_latency": anomaly_fingerprint(
            project_id=project_id,
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            discriminator=METRIC_DB_LATENCY,
            environment_id=environment_id,
            component_id=postgres,
        ),
        "checkout_latency": anomaly_fingerprint(
            project_id=project_id,
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            discriminator=METRIC_CHECKOUT_P95,
            environment_id=environment_id,
            component_id=checkout,
        ),
        "checkout_error_rate": anomaly_fingerprint(
            project_id=project_id,
            anomaly_type=AnomalyType.ERROR_RATE_SPIKE,
            discriminator=METRIC_CHECKOUT_ERROR_RATE,
            environment_id=environment_id,
            component_id=checkout,
        ),
        "inventory_latency": anomaly_fingerprint(
            project_id=project_id,
            anomaly_type=AnomalyType.METRIC_BASELINE_DEVIATION,
            discriminator=METRIC_INVENTORY_P95,
            environment_id=environment_id,
            component_id=inventory,
        ),
    }


__all__ = [
    "RULE_IDS",
    "METRIC_CHECKOUT_P95",
    "METRIC_CHECKOUT_ERROR_RATE",
    "METRIC_INVENTORY_P95",
    "METRIC_DB_LATENCY",
    "DB_LATENCY_BASELINE",
    "DB_LATENCY_SPIKE",
    "CONNECTION_REFUSED_MESSAGE",
    "DB_POOL_EXHAUSTED_MESSAGE",
    "INVENTORY_TIMEOUT_MESSAGE",
    "DEMO_TRACE_PREFIX",
    "DEMO_SPAN_PREFIX",
    "DEMO_DEPLOYMENT_ID",
    "DEMO_CONFIG_CHANGE_ID",
    "build_rules",
    "seed_checkout_incident",
    "expected_demo_fingerprints",
]
