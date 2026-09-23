"""Shared builders for the Phase 10 test suites.

Phase 10's guarantees are about *what history is allowed to become knowledge*,
so the fixtures here build genuine history — incidents, anomalies, remediations
and their verifications — rather than hand-written experience rows. A test that
asserted on a hand-written row would prove the miner works on input the pipeline
cannot actually produce.

Three deliberate properties:

* :func:`record_episode` writes a complete, realistic episode (incident →
  anomalies → remediation → verification) and then runs the **real** builder, so
  every test that needs an experience gets one the pipeline would have made.
* :func:`make_knowledge` writes knowledge directly *only* to set up states the
  lifecycle would take many runs to reach (an already-active pattern, a rejected
  one). The validation and activation paths themselves are always exercised
  through the real services.
* Time is always explicit: nothing here calls ``now()`` to decide when an
  incident happened. Temporal leakage tests are only meaningful if the fixture
  can place history on both sides of a cutoff.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from tests.phase6_helpers import build_project, build_scope  # noqa: F401  (re-export)
from tests.phase8_helpers import (  # noqa: F401  (re-export)
    emit_anomaly,
    emit_deployment,
    emit_incident,
    link_dependency,
)

#: Metric names used by the fixtures. Real-looking names matter: the signature
#: classifier maps them onto behaviour classes, and a fixture called ``metric1``
#: would silently test nothing.
METRIC_ERROR_RATE = "http.checkout.error_rate"
METRIC_P95 = "http.checkout.latency.p95"
METRIC_CPU = "system.cpu.utilization"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hours_before(moment: datetime, hours: float) -> datetime:
    return moment - timedelta(hours=hours)


def days_before(moment: datetime, days: float) -> datetime:
    return moment - timedelta(days=days)


async def emit_remediation(
    session: AsyncSession,
    project: Any,
    environment: Any,
    component: Any,
    *,
    incident: Any = None,
    action_type: str = "RESTART_SERVICE",
    status: str = "VERIFIED",
    outcome: Optional[str] = "EFFECTIVE",
    verification_verdict: Optional[str] = "VERIFIED",
    completed_at: Optional[datetime] = None,
    rollback: bool = False,
    parameters: Optional[dict] = None,
) -> Any:
    """Write a terminal remediation action, with its verification.

    Defaults describe the canonical successful case (restart, verified,
    effective) because that is the shape most tests need to *then* vary: the
    interesting assertions are about what happens when one of these is not what
    it appears.
    """
    from app.models.remediation import (
        AdapterKind,
        BlastRadiusScope,
        RemediationAction,
        RemediationActionType,
        RemediationExecutionMode,
        RemediationOutcome,
        RemediationRiskLevel,
        RemediationRollback,
        RemediationSourceType,
        RemediationStatus,
        RemediationVerification,
        RollbackStrategy,
        RollbackTrigger,
        VerificationVerdict,
    )

    moment = completed_at or utcnow()
    action = RemediationAction(
        project_id=project.id,
        environment_id=environment.id if environment else None,
        component_id=component.id if component else None,
        action_type=RemediationActionType(action_type),
        description=f"fixture {action_type}",
        reason="fixture remediation",
        risk_level=RemediationRiskLevel.LOW,
        blast_radius=BlastRadiusScope.SINGLE_COMPONENT,
        source_type=RemediationSourceType.INCIDENT,
        source_id=incident.id if incident is not None else None,
        incident_id=incident.id if incident is not None else None,
        parameters=parameters or {},
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        adapter_kind=AdapterKind.CONTROL_PLANE,
        status=RemediationStatus(status),
        outcome=RemediationOutcome(outcome) if outcome else None,
        rollback_strategy=(
            RollbackStrategy.INVERSE_ACTION if rollback else RollbackStrategy.NONE
        ),
        rollback_available=rollback,
        fingerprint=uuid.uuid4().hex,
        headline=f"fixture {action_type} on {getattr(component, 'name', 'component')}",
        created_by="phase10-fixture",
        started_at=moment - timedelta(minutes=2),
        completed_at=moment,
    )
    session.add(action)
    await session.flush()

    if verification_verdict is not None:
        session.add(
            RemediationVerification(
                action_id=action.id,
                project_id=project.id,
                environment_id=environment.id if environment else None,
                component_id=component.id if component else None,
                verdict=VerificationVerdict(verification_verdict),
                checks=[],
                passed_count=1 if verification_verdict == "VERIFIED" else 0,
                failed_count=0 if verification_verdict == "VERIFIED" else 1,
                not_observable_count=0,
                window_start=moment,
                window_end=moment + timedelta(minutes=5),
                summary="fixture verification",
            )
        )
    if rollback:
        session.add(
            RemediationRollback(
                action_id=action.id,
                project_id=project.id,
                trigger=RollbackTrigger.HUMAN_REQUEST,
                strategy=RollbackStrategy.INVERSE_ACTION,
                status="SUCCEEDED",
                requested_by="phase10-fixture",
                started_at=moment,
                completed_at=moment,
            )
        )
    await session.flush()
    return action


async def record_episode(
    session: AsyncSession,
    project: Any,
    environment: Any,
    component: Any,
    *,
    started_at: datetime,
    resolved_at: datetime,
    anomaly_types: Sequence[str] = ("LATENCY_SPIKE", "ERROR_RATE_SPIKE"),
    metric_name: str = METRIC_ERROR_RATE,
    observed: Optional[float] = 0.4,
    expected: Optional[float] = 0.01,
    severity: str = "HIGH",
    fingerprint: str = "checkout_error_spike",
    remediate: bool = True,
    action_type: str = "RESTART_SERVICE",
    outcome: str = "EFFECTIVE",
    verification_verdict: str = "VERIFIED",
    rollback: bool = False,
    extra_components: Iterable[Any] = (),
) -> dict[str, Any]:
    """Write one complete reliability episode and build its experience.

    Returns every row it made, keyed by role, so a test can assert on the
    experience *and* on the history it came from.
    """
    incident = await emit_incident(
        session,
        project,
        environment,
        component,
        detected_at=started_at,
        resolved_at=resolved_at,
        severity=severity,
        status="RESOLVED",
        fingerprint=fingerprint,
    )
    anomalies = []
    for anomaly_type in anomaly_types:
        anomalies.append(
            await emit_anomaly(
                session,
                project,
                environment,
                component,
                detected_at=started_at + timedelta(minutes=1),
                anomaly_type=anomaly_type,
                severity=severity,
                metric_name=metric_name,
            )
        )
    #: The anomaly's observed/expected values are what the metric-behaviour
    #: classifier reads, so they are set explicitly rather than left null.
    for anomaly in anomalies:
        anomaly.observed_value = observed
        anomaly.expected_value = expected
    for extra in extra_components:
        await emit_anomaly(
            session,
            project,
            environment,
            extra,
            detected_at=started_at + timedelta(minutes=2),
            anomaly_type="LATENCY_SPIKE",
            severity="MEDIUM",
            metric_name=METRIC_P95,
        )
    await session.flush()

    action = None
    if remediate:
        action = await emit_remediation(
            session,
            project,
            environment,
            component,
            incident=incident,
            action_type=action_type,
            status="ROLLED_BACK" if rollback else "VERIFIED",
            outcome=outcome,
            verification_verdict=verification_verdict,
            completed_at=resolved_at,
            rollback=rollback,
        )

    from app.services.experience_builder import (
        build_experience_draft,
        persist_experience,
    )

    draft, reason = await build_experience_draft(
        session, incident_id=incident.id, as_of=resolved_at + timedelta(seconds=1)
    )
    experience = None
    if draft is not None:
        experience, _status = await persist_experience(session, draft)
    await session.flush()

    return {
        "incident": incident,
        "anomalies": anomalies,
        "action": action,
        "draft": draft,
        "reason": reason,
        "experience": experience,
    }


async def record_series(
    session: AsyncSession,
    project: Any,
    environment: Any,
    component: Any,
    *,
    count: int,
    first_started_at: datetime,
    spacing_hours: float = 24.0,
    duration_minutes: float = 30.0,
    **episode_kwargs: Any,
) -> list[dict[str, Any]]:
    """A run of comparable episodes, spaced out so stability windows can see them."""
    episodes: list[dict[str, Any]] = []
    for index in range(count):
        started = first_started_at + timedelta(hours=spacing_hours * index)
        episodes.append(
            await record_episode(
                session,
                project,
                environment,
                component,
                started_at=started,
                resolved_at=started + timedelta(minutes=duration_minutes),
                **episode_kwargs,
            )
        )
    return episodes


async def make_knowledge(
    session: AsyncSession,
    project: Any,
    *,
    knowledge_type: str = "REMEDIATION_PATTERN",
    status: str = "ACTIVE",
    scope: str = "PROJECT_LEVEL",
    component: Any = None,
    title: str = "fixture knowledge",
    feature_signature: str = "remediation:RESTART_SERVICE:checkout_error_spike",
    details: Optional[dict] = None,
    sample_count: int = 6,
    success_count: int = 5,
    confidence: str = "MEDIUM",
    confirmed_at: Optional[datetime] = None,
    sources: Optional[list[dict]] = None,
) -> Any:
    """Insert knowledge directly, for states the lifecycle takes many runs to reach."""
    from app.models.intelligence import (
        KnowledgeConfidence,
        KnowledgeScope,
        KnowledgeStatus,
        KnowledgeType,
        ReliabilityKnowledge,
        knowledge_fingerprint,
    )

    ktype = KnowledgeType(knowledge_type)
    kscope = KnowledgeScope(scope)
    signature = feature_signature
    row = ReliabilityKnowledge(
        project_id=project.id,
        knowledge_type=ktype,
        status=KnowledgeStatus(status),
        scope=kscope,
        component_id=component.id if component is not None else None,
        title=title,
        description="fixture knowledge row",
        fingerprint=knowledge_fingerprint(
            knowledge_type=ktype,
            scope=kscope,
            project_id=project.id,
            component_id=component.id if component is not None else None,
            feature_signature=signature,
        ),
        feature_signature=signature,
        sources=sources
        if sources is not None
        else [{"type": "incident", "id": str(uuid.uuid4())}],
        experience_ids=[str(uuid.uuid4())],
        sample_count=sample_count,
        success_count=success_count,
        confidence=KnowledgeConfidence(confidence),
        support_strength=success_count / sample_count if sample_count else None,
        algorithm="fixture",
        algorithm_version="1.0",
        feature_schema_version="1.0",
        validation={"details": details or {}},
        limitations=["fixture"],
        last_confirmed_at=confirmed_at,
    )
    session.add(row)
    await session.flush()

    #: Every row the lifecycle creates gets its ledger entry (``_write_version``
    #: in :mod:`app.services.knowledge_lifecycle`). Reusing that writer rather
    #: than hand-rolling a ``KnowledgeVersion`` is deliberate: a fixture row
    #: without the version the pipeline always writes would let a test assert on
    #: a state production cannot reach.
    from app.services.knowledge_lifecycle import _write_version

    await _write_version(
        session,
        row,
        run_id=None,
        note="fixture",
        moment=confirmed_at or utcnow(),
    )
    await session.flush()
    return row


__all__ = [
    "METRIC_CPU",
    "METRIC_ERROR_RATE",
    "METRIC_P95",
    "build_project",
    "build_scope",
    "days_before",
    "emit_anomaly",
    "emit_deployment",
    "emit_incident",
    "emit_remediation",
    "hours_before",
    "link_dependency",
    "make_knowledge",
    "record_episode",
    "record_series",
    "utcnow",
]
