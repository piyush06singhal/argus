"""ARGUS Phase 4 counterexample & unknown scenarios (§49, §50).

Two deterministic scenarios that test the *limits* of the causal engine — the
part that makes it trustworthy rather than merely confident:

* **Counterexample (§49)** — the incident starts at ``T-14m`` and the deployment
  lands at ``T-2m``. A naive correlator reports "deployment caused it". The
  engine must record a ``TEMPORAL_CONTRADICTION``, refuse the deployment as the
  origin, and say so in the response.
* **Unknown (§50)** — several components degrade in the same window with no
  spans, no dependency path and no change nearby. The honest answer is
  ``UNKNOWN`` with ``INSUFFICIENT`` confidence. That is a *passing* result.

Both scenarios use the real pipeline end to end: telemetry is ingested, the
Phase 3 detectors run, correlation builds the incident, and Phase 4 analyses the
stored evidence. Nothing is written into the analysis tables directly, and no
expected answer is handed to the engine.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc_or_now
from app.models.anomaly import (
    Anomaly,
    AnomalyRule,
    AnomalySeverity,
    AnomalyType,
    BaselineStrategy,
    RuleCondition,
)
from app.models.causal import CausalAnalysis, RootCauseCandidate
from app.models.deployment import DeploymentEvent, DeploymentStatus
from app.models.incident import Incident
from app.models.observability import (
    LogRecord,
    MetricRecord,
    MetricType,
    Severity,
    TraceRecord,
    TraceStatus,
)
from app.models.project import Environment, SoftwareProject
from app.models.system import ComponentCategory, ComponentDependency, SystemComponent
from app.services.anomaly_detection import AnomalyDetectionService
from app.services.causal_analysis_service import CausalAnalysisService
from app.services.incident_manager import IncidentManager

#: Fixed rule ids, mirroring the demo's convention (reproducible fingerprints).
COUNTEREXAMPLE_RULE_IDS = {
    "checkout_errors": uuid.UUID("60000000-0000-0000-0000-00000000c001"),
    "checkout_latency": uuid.UUID("60000000-0000-0000-0000-00000000c002"),
}
UNKNOWN_RULE_IDS = {
    "peer_a_latency": uuid.UUID("60000000-0000-0000-0000-00000000d001"),
    "peer_b_latency": uuid.UUID("60000000-0000-0000-0000-00000000d002"),
    "peer_c_errors": uuid.UUID("60000000-0000-0000-0000-00000000d003"),
}

METRIC_CHECKOUT_ERRORS = "http.checkout.error_rate"
METRIC_CHECKOUT_P95 = "http.checkout.latency.p95"
METRIC_PEER_LATENCY = "rpc.peer.latency.p95"
METRIC_PEER_ERRORS = "rpc.peer.error_rate"

RULE_WINDOW_SECONDS = 600


class _ScenarioError(RuntimeError):
    """Raised when a scenario's own telemetry fails to produce an incident."""


async def _build_topology(
    db: AsyncSession,
    *,
    name: str,
    slug_prefix: str,
    links: list[tuple[str, str, str, str]],
) -> tuple[SoftwareProject, Environment, dict[str, SystemComponent]]:
    """Create a throwaway project/environment/topology for one scenario.

    ``links`` — ``(source_key, source_type, target_key, target_type)``.
    """
    project = SoftwareProject(name=name, slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}")
    db.add(project)
    await db.flush()
    environment = Environment(
        project_id=project.id,
        name="Production",
        environment_type="PRODUCTION",
    )
    db.add(environment)
    await db.flush()

    categories = {
        "SERVICE": ComponentCategory.SERVICE,
        "DATABASE": ComponentCategory.DATABASE,
        "GATEWAY": ComponentCategory.APPLICATION,
        "CACHE": ComponentCategory.CACHE,
    }
    components: dict[str, SystemComponent] = {}
    for source_key, source_type, target_key, target_type in links:
        for key, kind in ((source_key, source_type), (target_key, target_type)):
            if key in components:
                continue
            components[key] = SystemComponent(
                project_id=project.id,
                environment_id=environment.id,
                name=key.replace("_", " ").title(),
                component_type=categories[kind],
            )
            db.add(components[key])
    await db.flush()
    for source_key, _source_type, target_key, _target_type in links:
        db.add(
            ComponentDependency(
                source_component_id=components[source_key].id,
                target_component_id=components[target_key].id,
                dependency_type="HTTP",
            )
        )
    await db.flush()
    return project, environment, components


async def _run_pipeline(
    db: AsyncSession,
    *,
    project: SoftwareProject,
    environment: Environment,
    now: datetime,
) -> tuple[Optional[Incident], list[Anomaly]]:
    await AnomalyDetectionService(db, now=now).run(
        project_id=project.id, environment_id=environment.id
    )
    await IncidentManager(db, now=now).process_scope(
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
    return incident, anomalies


async def _analyze(
    db: AsyncSession, incident: Incident, *, trigger: str
) -> tuple[CausalAnalysis, list[RootCauseCandidate]]:
    outcome = await CausalAnalysisService(db).analyze_incident(
        project_id=incident.project_id,
        incident=incident,
        trigger=trigger,
        requested_by="scenario",
        force=True,
    )
    candidates = list(
        (
            await db.execute(
                select(RootCauseCandidate)
                .where(RootCauseCandidate.analysis_id == outcome.analysis.id)
                .order_by(RootCauseCandidate.score.desc())
            )
        )
        .scalars()
        .all()
    )
    return outcome.analysis, candidates


# ---------------------------------------------------------------------------
# §49 — counterexample: the change lands after the incident started
# ---------------------------------------------------------------------------
async def seed_counterexample_scenario(
    db: AsyncSession, *, now: Optional[datetime] = None
) -> dict:
    """Degradation begins long before the deployment; the engine must say so.

    The deployment is deliberately *near the detection instant* (so a naive
    "recent change" heuristic would blame it) but *after* the first anomaly.
    """
    reference = ensure_utc_or_now(now)
    project, environment, components = await _build_topology(
        db,
        name="Counterexample Checkout",
        slug_prefix="counterexample-demo",
        links=[
            ("checkout_service", "SERVICE", "inventory_service", "SERVICE"),
            ("inventory_service", "SERVICE", "inventory_db", "DATABASE"),
        ],
    )
    checkout = components["checkout_service"]
    inventory = components["inventory_service"]

    # Timing is the whole point of this scenario. Degradation begins four
    # minutes before detection; the deployment lands 30 seconds before
    # detection — *after* onset but close enough that a naive "recent change"
    # heuristic would blame it.
    first_failure = reference - timedelta(minutes=4)
    for offset in range(12, 4, -1):
        at = reference - timedelta(minutes=offset)
        for component, metric_name, value in (
            (checkout, METRIC_CHECKOUT_ERRORS, 0.004),
            (checkout, METRIC_CHECKOUT_P95, 210.0),
            (inventory, METRIC_PEER_LATENCY, 140.0),
        ):
            db.add(
                MetricRecord(
                    project_id=project.id,
                    environment_id=environment.id,
                    component_id=component.id,
                    timestamp=at,
                    metric_name=metric_name,
                    metric_type=MetricType.GAUGE,
                    value=value,
                )
            )
    for seconds, metric_name, value in (
        (240, METRIC_CHECKOUT_ERRORS, 0.31),
        (210, METRIC_CHECKOUT_ERRORS, 0.27),
        (180, METRIC_CHECKOUT_ERRORS, 0.24),
        (210, METRIC_CHECKOUT_P95, 940.0),
        (150, METRIC_CHECKOUT_P95, 880.0),
    ):
        db.add(
            MetricRecord(
                project_id=project.id,
                environment_id=environment.id,
                component_id=checkout.id,
                timestamp=reference - timedelta(seconds=seconds),
                metric_name=metric_name,
                metric_type=MetricType.GAUGE,
                value=value,
            )
        )
    for index in range(6):
        db.add(
            LogRecord(
                project_id=project.id,
                environment_id=environment.id,
                component_id=checkout.id,
                timestamp=first_failure + timedelta(seconds=index * 20),
                level=Severity.ERROR,
                message="checkout request failed: stock reservation rejected",
                service="checkout-service",
            )
        )
    # A failing trace *before* the deployment, so direction is decidable.
    db.add(
        TraceRecord(
            project_id=project.id,
            environment_id=environment.id,
            trace_id="counterexample-fail-01",
            name="POST /api/checkout",
            start_time=first_failure,
            end_time=first_failure + timedelta(milliseconds=1800),
            duration_ms=1800.0,
            status=TraceStatus.ERROR,
        )
    )

    db.add(
        AnomalyRule(
            id=COUNTEREXAMPLE_RULE_IDS["checkout_errors"],
            project_id=project.id,
            environment_id=environment.id,
            component_id=checkout.id,
            name="Checkout Error Rate",
            description="Checkout error rate above the configured ceiling",
            anomaly_type=AnomalyType.ERROR_RATE_SPIKE,
            condition=RuleCondition.THRESHOLD,
            metric_name=METRIC_CHECKOUT_ERRORS,
            baseline_strategy=BaselineStrategy.STATIC,
            expected_value=0.004,
            threshold=0.05,
            min_samples=3,
            window_seconds=RULE_WINDOW_SECONDS,
            cooldown_seconds=0,
            persistence_cycles=1,
            severity=AnomalySeverity.HIGH,
            enabled=True,
            created_by="scenario",
        )
    )
    db.add(
        AnomalyRule(
            id=COUNTEREXAMPLE_RULE_IDS["checkout_latency"],
            project_id=project.id,
            environment_id=environment.id,
            component_id=checkout.id,
            name="Checkout P95 Latency",
            description="Checkout p95 latency above the configured ceiling",
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            condition=RuleCondition.THRESHOLD,
            metric_name=METRIC_CHECKOUT_P95,
            baseline_strategy=BaselineStrategy.STATIC,
            expected_value=210.0,
            threshold=500.0,
            min_samples=3,
            window_seconds=RULE_WINDOW_SECONDS,
            cooldown_seconds=0,
            persistence_cycles=1,
            severity=AnomalySeverity.HIGH,
            enabled=True,
            created_by="scenario",
        )
    )
    # The late deployment — the trap this scenario exists to spring.
    deployment = DeploymentEvent(
        project_id=project.id,
        environment_id=environment.id,
        component_id=checkout.id,
        deployment_id="deploy-checkout-late",
        version="2099.1.0",
        commit_sha="f" * 40,
        deployed_at=reference - timedelta(seconds=30),
        status=DeploymentStatus.SUCCESS,
        description="Deployment published after the incident had already begun",
    )
    db.add(deployment)
    await db.flush()

    incident, anomalies = await _run_pipeline(
        db, project=project, environment=environment, now=reference
    )
    if incident is None:
        raise _ScenarioError("counterexample scenario produced no incident")
    analysis, candidates = await _analyze(db, incident, trigger="counterexample-demo")

    deployment_candidates = [
        candidate for candidate in candidates if candidate.event_id == deployment.id
    ]
    return {
        "project_id": str(project.id),
        "incident_id": str(incident.id),
        "analysis_id": str(analysis.id),
        "analysis_version": analysis.analysis_version,
        "overall_confidence": analysis.overall_confidence.value,
        "primary_candidate_id": (
            str(analysis.primary_candidate_id)
            if analysis.primary_candidate_id
            else None
        ),
        "summary": analysis.summary,
        "missing_evidence": analysis.missing_evidence,
        "deployment_event_id": str(deployment.id),
        "deployment_candidate_id": (
            str(deployment_candidates[0].id) if deployment_candidates else None
        ),
        "deployment_contradicting_evidence": (
            deployment_candidates[0].contradicting_evidence_count
            if deployment_candidates
            else 0
        ),
        "deployment_confidence": (
            deployment_candidates[0].confidence.value if deployment_candidates else None
        ),
        "deployment_is_primary": bool(
            deployment_candidates
            and analysis.primary_candidate_id == deployment_candidates[0].id
        ),
        "anomalies": [anomaly.anomaly_type.value for anomaly in anomalies],
        "candidates": [
            {
                "id": str(candidate.id),
                "type": candidate.candidate_type.value,
                "score": candidate.score,
                "confidence": candidate.confidence.value,
                "supporting": candidate.supporting_evidence_count,
                "contradicting": candidate.contradicting_evidence_count,
                "event_id": str(candidate.event_id) if candidate.event_id else None,
                "reasons": candidate.reasons or [],
            }
            for candidate in candidates
        ],
    }


# ---------------------------------------------------------------------------
# §50 — unknown: correlated anomalies, no directional evidence
# ---------------------------------------------------------------------------
async def seed_unknown_scenario(
    db: AsyncSession, *, now: Optional[datetime] = None
) -> dict:
    """Three unrelated peers degrade together with nothing to order them.

    No spans, no dependency path between the peers, no change nearby: the
    evidence is genuinely insufficient, and the engine must return
    ``UNKNOWN``/``INSUFFICIENT`` rather than pick a winner.
    """
    reference = ensure_utc_or_now(now)
    project, environment, components = await _build_topology(
        db,
        name="Unknown Root Cause",
        slug_prefix="unknown-demo",
        # A gateway builds the *incident*, but the peers have no relation to
        # each other, so nothing orders them.
        links=[
            ("gateway", "GATEWAY", "peer_a", "SERVICE"),
            ("gateway", "GATEWAY", "peer_b", "SERVICE"),
            ("gateway", "GATEWAY", "peer_c", "SERVICE"),
        ],
    )
    peer_a = components["peer_a"]
    peer_b = components["peer_b"]
    peer_c = components["peer_c"]

    for offset in range(22, 17, -1):
        at = reference - timedelta(minutes=offset)
        for component in (peer_a, peer_b, peer_c):
            db.add(
                MetricRecord(
                    project_id=project.id,
                    environment_id=environment.id,
                    component_id=component.id,
                    timestamp=at,
                    metric_name=METRIC_PEER_LATENCY,
                    metric_type=MetricType.GAUGE,
                    value=120.0,
                )
            )

    # Three near-simultaneous degradations, deliberately tens of seconds apart
    # so no ordering conclusion can be drawn from them.
    for seconds, component_id, metric_name, value in (
        (40, peer_a.id, METRIC_PEER_LATENCY, 780.0),
        (35, peer_b.id, METRIC_PEER_LATENCY, 760.0),
        (30, peer_c.id, METRIC_PEER_ERRORS, 0.22),
    ):
        db.add(
            MetricRecord(
                project_id=project.id,
                environment_id=environment.id,
                component_id=component_id,
                timestamp=reference - timedelta(seconds=seconds),
                metric_name=metric_name,
                metric_type=MetricType.GAUGE,
                value=value,
            )
        )

    db.add(
        AnomalyRule(
            id=UNKNOWN_RULE_IDS["peer_a_latency"],
            project_id=project.id,
            environment_id=environment.id,
            component_id=peer_a.id,
            name="Peer A Latency",
            description="Peer A latency above ceiling",
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            condition=RuleCondition.THRESHOLD,
            metric_name=METRIC_PEER_LATENCY,
            baseline_strategy=BaselineStrategy.STATIC,
            expected_value=120.0,
            threshold=500.0,
            min_samples=3,
            window_seconds=RULE_WINDOW_SECONDS,
            cooldown_seconds=0,
            persistence_cycles=1,
            severity=AnomalySeverity.HIGH,
            enabled=True,
            created_by="scenario",
        )
    )
    db.add(
        AnomalyRule(
            id=UNKNOWN_RULE_IDS["peer_b_latency"],
            project_id=project.id,
            environment_id=environment.id,
            component_id=peer_b.id,
            name="Peer B Latency",
            description="Peer B latency above ceiling",
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            condition=RuleCondition.THRESHOLD,
            metric_name=METRIC_PEER_LATENCY,
            baseline_strategy=BaselineStrategy.STATIC,
            expected_value=120.0,
            threshold=500.0,
            min_samples=3,
            window_seconds=RULE_WINDOW_SECONDS,
            cooldown_seconds=0,
            persistence_cycles=1,
            severity=AnomalySeverity.HIGH,
            enabled=True,
            created_by="scenario",
        )
    )
    db.add(
        AnomalyRule(
            id=UNKNOWN_RULE_IDS["peer_c_errors"],
            project_id=project.id,
            environment_id=environment.id,
            component_id=peer_c.id,
            name="Peer C Error Rate",
            description="Peer C error rate above ceiling",
            anomaly_type=AnomalyType.ERROR_RATE_SPIKE,
            condition=RuleCondition.THRESHOLD,
            metric_name=METRIC_PEER_ERRORS,
            baseline_strategy=BaselineStrategy.STATIC,
            expected_value=0.002,
            threshold=0.05,
            min_samples=3,
            window_seconds=RULE_WINDOW_SECONDS,
            cooldown_seconds=0,
            persistence_cycles=1,
            severity=AnomalySeverity.MEDIUM,
            enabled=True,
            created_by="scenario",
        )
    )
    await db.flush()

    incident, anomalies = await _run_pipeline(
        db, project=project, environment=environment, now=reference
    )
    if incident is None:
        raise _ScenarioError("unknown scenario produced no incident")
    analysis, candidates = await _analyze(db, incident, trigger="unknown-demo")

    return {
        "project_id": str(project.id),
        "incident_id": str(incident.id),
        "analysis_id": str(analysis.id),
        "analysis_version": analysis.analysis_version,
        "overall_confidence": analysis.overall_confidence.value,
        "primary_candidate_id": (
            str(analysis.primary_candidate_id)
            if analysis.primary_candidate_id
            else None
        ),
        "summary": analysis.summary,
        "missing_evidence": analysis.missing_evidence or [],
        "anomalies": [anomaly.anomaly_type.value for anomaly in anomalies],
        "candidates": [
            {
                "id": str(candidate.id),
                "type": candidate.candidate_type.value,
                "score": candidate.score,
                "confidence": candidate.confidence.value,
                "supporting": candidate.supporting_evidence_count,
                "contradicting": candidate.contradicting_evidence_count,
                "reasons": candidate.reasons or [],
            }
            for candidate in candidates
        ],
    }


__all__ = [
    "COUNTEREXAMPLE_RULE_IDS",
    "UNKNOWN_RULE_IDS",
    "METRIC_CHECKOUT_ERRORS",
    "METRIC_CHECKOUT_P95",
    "METRIC_PEER_ERRORS",
    "METRIC_PEER_LATENCY",
    "seed_counterexample_scenario",
    "seed_unknown_scenario",
]
