"""ARGUS Reliability Experience Builder (Phase 10 §8, §9, §10, §30, §31).

Converts rows Phases 1–9 already wrote into one normalized
:class:`~app.models.intelligence.ReliabilityExperience`: what failed, what was
observed, what was done, and what actually happened.

Two properties carry the phase:

* **Temporal purity (§31).** Every query is bounded by ``as_of``. An experience
  assembled "as of 1 June" cannot see 2 June's anomalies, deployments or
  outcomes, because the filter is applied in SQL rather than trusted to the
  caller. The experience's own ``end_time`` is also refused if it lies after the
  cutoff — a row whose hindsight is in the future is not learnable history.
* **Honest quality (§30).** Missing timestamps, inverted timestamps, absent
  signals and unverifiable outcomes are recorded on the row as ``data_quality``
  plus reasons. ``POOR`` rows are stored (they are evidence of a gap) but the
  miners refuse to build patterns from them.

Everything gathered is *derived*: nothing here writes back into the phases it
reads, and deleting the incident cascades the experience away with it.

Refs kept as opaque ids rather than foreign keys (causal analysis, candidate,
reproduction, patch) are deliberate: an experience records what happened, and a
circular FK between learning and analysis would make deleting an analysis take
its own history with it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import func, nulls_last, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.anomaly import Anomaly, AnomalyType
from app.models.causal import CausalAnalysis, RootCauseCandidate
from app.models.deployment import DeploymentEvent, DeploymentStatus
from app.models.fix import (
    FixHypothesis,
    Patch,
    PatchStatus,
    PatchVerificationRun,
    VerificationStatus,
)
from app.models.incident import Incident, IncidentStatus
from app.models.ingestion import HealthCheckEvent, HealthStatus
from app.models.intelligence import (
    DataProvenance,
    ReliabilityExperience,
)
from app.models.remediation import (
    RemediationAction,
    RemediationRollback,
    RemediationStatus,
    RemediationVerification,
)
from app.models.reproduction import ReproductionExperiment
from app.models.system import ComponentDependency
from app.services.learning_signatures import (
    FailureSignature,
    ResolutionSignature,
    build_failure_signature,
    build_resolution_signature,
    comparable_seconds,
)

logger = logging.getLogger(__name__)

#: How far before the incident a deployment still counts as context.
DEPLOYMENT_LOOKBACK_HOURS = 24

#: Worst-to-best ordering for health status tokens.
_HEALTH_ORDER = (
    HealthStatus.UNHEALTHY.value,
    HealthStatus.DEGRADED.value,
    HealthStatus.UNKNOWN.value,
    HealthStatus.HEALTHY.value,
)

#: Remediation statuses that mean the action reached an end, one way or another.
_TERMINAL_REMEDIATION_STATUSES = (
    RemediationStatus.VERIFIED,
    RemediationStatus.FAILED,
    RemediationStatus.ROLLED_BACK,
    RemediationStatus.CANCELLED,
    RemediationStatus.EXPIRED,
    RemediationStatus.BLOCKED,
)

QUALITY_OK = "OK"
QUALITY_LIMITED = "LIMITED"
QUALITY_POOR = "POOR"

#: Outcome tokens for episodes that were not resolved by a remediation.
OUTCOME_SELF_RECOVERED = "self_recovered"
OUTCOME_RESOLVED_UNVERIFIED = "resolved_unverified"
OUTCOME_UNRESOLVED = "unresolved"


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """UTC-normalize a stored timestamp, or ``None``."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _enum_value(value: Any) -> Optional[str]:
    """The string value of an enum-backed column, or the value unchanged."""
    if value is None:
        return None
    return getattr(value, "value", str(value))


@dataclass
class QualityVerdict:
    """The §30 pre-learning verdict for one candidate experience."""

    level: str = QUALITY_OK
    reasons: list[str] = field(default_factory=list)

    def flag(self, level: str, reason: str) -> None:
        """Record a problem, keeping the worst level seen."""
        self.reasons.append(reason)
        if level == QUALITY_POOR or self.level == QUALITY_POOR:
            self.level = QUALITY_POOR
        elif level == QUALITY_LIMITED and self.level == QUALITY_OK:
            self.level = QUALITY_LIMITED

    @property
    def usable(self) -> bool:
        """Whether pattern mining may use this row (§30)."""
        return self.level != QUALITY_POOR


@dataclass
class ExperienceDraft:
    """An assembled, not-yet-persisted experience."""

    project_id: uuid.UUID
    incident_id: uuid.UUID
    environment_id: Optional[uuid.UUID]
    primary_component_id: Optional[uuid.UUID]
    component_ids: list[str]
    start_time: datetime
    end_time: datetime
    recovery_seconds: Optional[int]
    failure_signature: FailureSignature
    resolution_signature: ResolutionSignature
    outcome: str
    provenance: DataProvenance
    quality: QualityVerdict
    remediation_action_id: Optional[uuid.UUID] = None
    causal_analysis_id: Optional[uuid.UUID] = None
    root_cause_candidate_id: Optional[uuid.UUID] = None
    reproduction_id: Optional[uuid.UUID] = None
    patch_id: Optional[uuid.UUID] = None
    #: Opaque source references for provenance (§5). Each entry is
    #: ``{"type": ..., "id": ...}``.
    sources: list[dict[str, str]] = field(default_factory=list)
    evidence_counts: dict[str, int] = field(default_factory=dict)

    @property
    def failure_fingerprint(self) -> str:
        return self.failure_signature.fingerprint()


@dataclass
class ExperienceBuildResult:
    """What one builder pass did, for the learning-run ledger."""

    created: list[ReliabilityExperience] = field(default_factory=list)
    updated: list[ReliabilityExperience] = field(default_factory=list)
    unchanged: int = 0
    flagged: int = 0
    unprocessable: dict[str, str] = field(default_factory=dict)

    @property
    def processed(self) -> int:
        return len(self.created) + len(self.updated) + self.unchanged


async def build_experience_draft(
    session: AsyncSession,
    *,
    incident_id: uuid.UUID,
    as_of: Optional[datetime] = None,
    provenance: DataProvenance = DataProvenance.SYSTEM_GENERATED,
) -> tuple[Optional[ExperienceDraft], Optional[str]]:
    """Assemble one experience, or explain why it cannot be assembled.

    Returns ``(draft, None)`` on success and ``(None, reason)`` when the incident
    cannot be turned into history — the reason is stored on the learning event, so
    an excluded incident is visible rather than missing (§79).
    """
    cutoff = _aware(as_of) or datetime.now(timezone.utc)
    incident = await session.get(Incident, incident_id)
    if incident is None:
        return None, "incident_not_found"
    if incident.status not in (
        IncidentStatus.MITIGATED,
        IncidentStatus.RESOLVED,
        IncidentStatus.CLOSED,
    ):
        return None, f"incident_not_completed:{_enum_value(incident.status)}"

    #: ``started_at`` and ``resolved_at`` are the episode's own timestamps; the
    #: detection and row-update moments are honest fallbacks, and ``updated_at``
    #: is used only when an incident was closed without recording when.
    start_time = _aware(incident.started_at) or _aware(incident.detected_at)
    end_time = _aware(incident.resolved_at) or _aware(incident.updated_at)
    quality = QualityVerdict()
    if start_time is None:
        quality.flag(QUALITY_POOR, "missing_start_time")
    if end_time is None:
        quality.flag(QUALITY_POOR, "missing_end_time")
    if start_time is not None and end_time is not None and end_time < start_time:
        quality.flag(QUALITY_POOR, "end_before_start")
    if start_time is None or end_time is None:
        #: Defensive: the Phase 3 schema requires ``detected_at``, so this is
        #: unreachable for rows written by ARGUS. It stays because a window is
        #: the precondition for every later query, and guessing one would be
        #: worse than refusing.
        return None, "incident_missing_timestamps"

    #: §31. Hindsight is not learnable: an experience that ends after the run's
    #: cutoff would let future outcomes shape historical knowledge.
    if end_time > cutoff:
        return None, "incident_after_cutoff"

    components: set[str] = set()
    if incident.primary_component_id is not None:
        components.add(str(incident.primary_component_id))

    anomalies = await _gather_anomalies(
        session, incident=incident, start=start_time, end=end_time, cutoff=cutoff
    )
    for anomaly in anomalies:
        if anomaly.component_id is not None:
            components.add(str(anomaly.component_id))

    remediation = await _gather_remediation(session, incident_id=incident.id)
    if remediation is not None and remediation.component_id is not None:
        components.add(str(remediation.component_id))

    patch, patch_verified, regression_detected = await _gather_patch(
        session, incident_id=incident.id, cutoff=cutoff
    )
    causal = await _gather_causal(session, incident_id=incident.id, cutoff=cutoff)
    reproduction = await _gather_reproduction(
        session, incident_id=incident.id, cutoff=cutoff
    )
    forecast = await _gather_forecast(
        session,
        project_id=incident.project_id,
        component_id=incident.primary_component_id,
        start=start_time,
    )

    failure_signature = await gather_failure_signature(
        session,
        incident=incident,
        anomalies=anomalies,
        component_tokens=sorted(components),
        start=start_time,
        end=end_time,
        cutoff=cutoff,
    )

    recovery_seconds = comparable_seconds(start_time, end_time)
    resolution_signature, outcome = await _resolution_for(
        session,
        incident=incident,
        remediation=remediation,
        patch_verified=patch_verified,
        recovery_seconds=recovery_seconds,
    )

    observed_metrics = [anomaly for anomaly in anomalies if anomaly.metric_name]
    if not anomalies and not observed_metrics:
        quality.flag(QUALITY_LIMITED, "no_observed_signals")
    if not any(anomaly.observed_value is not None for anomaly in anomalies):
        quality.flag(QUALITY_LIMITED, "no_observed_values")
    if regression_detected:
        quality.flag(QUALITY_LIMITED, "regression_detected")
    if len(components) > 1:
        quality.flag(QUALITY_LIMITED, "multi_component_episode")

    sources: list[dict[str, str]] = [
        {"type": "incident", "id": str(incident.id)},
    ]
    for anomaly in anomalies[:20]:
        sources.append({"type": "anomaly", "id": str(anomaly.id)})
    if remediation is not None:
        sources.append({"type": "remediation", "id": str(remediation.id)})
    if causal is not None:
        sources.append({"type": "rca", "id": str(causal.id)})
    if reproduction is not None:
        sources.append({"type": "reproduction", "id": str(reproduction.id)})
    if patch is not None:
        sources.append({"type": "patch", "id": str(patch.id)})
    if forecast is not None:
        sources.append({"type": "forecast", "id": str(forecast.id)})

    draft = ExperienceDraft(
        project_id=incident.project_id,
        incident_id=incident.id,
        environment_id=incident.environment_id,
        primary_component_id=incident.primary_component_id,
        component_ids=sorted(components),
        start_time=start_time,
        end_time=end_time,
        recovery_seconds=recovery_seconds,
        failure_signature=failure_signature,
        resolution_signature=resolution_signature,
        outcome=outcome,
        provenance=provenance,
        quality=quality,
        remediation_action_id=remediation.id if remediation is not None else None,
        causal_analysis_id=causal.id if causal is not None else None,
        root_cause_candidate_id=(
            causal.primary_candidate_id if causal is not None else None
        ),
        reproduction_id=reproduction.id if reproduction is not None else None,
        patch_id=patch.id if patch is not None else None,
        sources=sources,
        evidence_counts={
            "anomalies": len(anomalies),
            "components": len(components),
            "sources": len(sources),
        },
    )
    return draft, None


def incident_component_ids(components: Sequence[str]) -> list[uuid.UUID]:
    """Parse signature component tokens back into UUIDs for query filters."""
    resolved: list[uuid.UUID] = []
    for token in components:
        try:
            resolved.append(uuid.UUID(token))
        except (ValueError, AttributeError, TypeError):
            continue
    return resolved


async def gather_failure_signature(
    session: AsyncSession,
    *,
    incident: Incident,
    anomalies: Sequence[Anomaly],
    component_tokens: Sequence[str],
    start: datetime,
    end: datetime,
    cutoff: datetime,
) -> FailureSignature:
    """Build a failure signature from an incident and its observed signals.

    Shared with the retrieval and recommendation paths so that the signature used
    to look *back* at history is produced by exactly the same code as the
    signature that was stored *in* history. Two builders would be two different
    vocabularies, and every similarity number would be comparing apples to
    oranges.
    """
    component_ids = incident_component_ids(component_tokens)
    deployment_context = await _gather_deployment_context(
        session, component_ids=component_ids, start=start, cutoff=cutoff
    )
    dependency_conditions = await _gather_dependency_conditions(
        session,
        project_id=incident.project_id,
        component_ids=component_ids,
        start=start,
        end=end,
        cutoff=cutoff,
    )
    health_state = await _gather_health_state(
        session, component_ids=component_ids, start=start, end=end, cutoff=cutoff
    )

    return build_failure_signature(
        incident_kind=incident.fingerprint or None,
        severity=_enum_value(incident.severity),
        anomaly_types=[
            _enum_value(anomaly.anomaly_type)
            for anomaly in anomalies
            if anomaly.anomaly_type
        ],
        metric_observations=[
            {
                "metric_name": anomaly.metric_name,
                "observed_value": anomaly.observed_value,
                "expected_value": anomaly.expected_value,
            }
            for anomaly in anomalies
            if anomaly.metric_name
        ],
        component_ids=list(component_tokens),
        dependency_conditions=dependency_conditions,
        deployment_context=deployment_context,
        log_patterns=[
            anomaly.pattern_template
            for anomaly in anomalies
            if _enum_value(anomaly.anomaly_type) == AnomalyType.LOG_PATTERN_SPIKE.value
            and anomaly.pattern_template
        ],
        trace_patterns=[
            anomaly.pattern_template
            for anomaly in anomalies
            if _enum_value(anomaly.anomaly_type)
            == AnomalyType.TRACE_FAILURE_SPIKE.value
            and anomaly.pattern_template
        ],
        health_state=health_state,
    )


async def build_current_signature(
    session: AsyncSession,
    *,
    incident_id: uuid.UUID,
    as_of: Optional[datetime] = None,
) -> tuple[Optional[FailureSignature], Optional[str]]:
    """Build the signature of a *current* situation — completed or not.

    Used by retrieval and recommendation, where the whole point is to describe an
    incident that is still open. The §31 cutoff still applies: nothing after
    ``as_of`` may shape the description, so a retrieval cannot "see" an outcome
    that has not happened yet.
    """
    cutoff = _aware(as_of) or datetime.now(timezone.utc)
    incident = await session.get(Incident, incident_id)
    if incident is None:
        return None, "incident_not_found"

    start = _aware(incident.started_at) or _aware(incident.detected_at)
    end = _aware(incident.resolved_at) or min(
        cutoff, _aware(incident.updated_at) or cutoff
    )
    if start is None:
        return None, "incident_missing_start_time"
    if end < start:
        end = start

    components: set[str] = set()
    if incident.primary_component_id is not None:
        components.add(str(incident.primary_component_id))
    anomalies = await _gather_anomalies(
        session, incident=incident, start=start, end=min(end, cutoff), cutoff=cutoff
    )
    for anomaly in anomalies:
        if anomaly.component_id is not None:
            components.add(str(anomaly.component_id))

    signature = await gather_failure_signature(
        session,
        incident=incident,
        anomalies=anomalies,
        component_tokens=sorted(components),
        start=start,
        end=min(end, cutoff),
        cutoff=cutoff,
    )
    return signature, None


async def _gather_anomalies(
    session: AsyncSession,
    *,
    incident: Incident,
    start: datetime,
    end: datetime,
    cutoff: datetime,
) -> list[Anomaly]:
    """Anomalies belonging to this incident, or to its component in the window."""
    conditions = [Anomaly.incident_id == incident.id]
    if incident.primary_component_id is not None:
        conditions.append(Anomaly.component_id == incident.primary_component_id)
    stmt = (
        select(Anomaly)
        .where(Anomaly.project_id == incident.project_id)
        .where(or_(*conditions))
        .where(Anomaly.detected_at >= start)
        .where(Anomaly.detected_at <= end)
        .where(Anomaly.detected_at <= cutoff)
        .order_by(Anomaly.detected_at.asc())
        .limit(200)
    )
    return list((await session.scalars(stmt)).all())


async def _gather_remediation(
    session: AsyncSession, *, incident_id: uuid.UUID
) -> Optional[RemediationAction]:
    """The remediation that ended this incident, if any (§14).

    The *latest terminal* action is chosen, not the first: an incident that was
    remediated twice is history about the attempt that finally resolved it, and
    preferring the first would make a failed attempt look like the resolution.
    """
    stmt = (
        select(RemediationAction)
        .where(RemediationAction.incident_id == incident_id)
        .where(RemediationAction.status.in_(_TERMINAL_REMEDIATION_STATUSES))
        .order_by(
            nulls_last(RemediationAction.completed_at.desc()),
            RemediationAction.created_at.desc(),
        )
        .limit(1)
    )
    return await session.scalar(stmt)


async def _gather_patch(
    session: AsyncSession, *, incident_id: uuid.UUID, cutoff: datetime
) -> tuple[Optional[Patch], bool, bool]:
    """The fix's patch, whether it verified, and whether it regressed (§19)."""
    hypothesis = await session.scalar(
        select(FixHypothesis)
        .where(FixHypothesis.incident_id == incident_id)
        .order_by(FixHypothesis.created_at.desc())
        .limit(1)
    )
    if hypothesis is None:
        return None, False, False

    patch = await session.scalar(
        select(Patch)
        .where(Patch.fix_hypothesis_id == hypothesis.id)
        .order_by(Patch.created_at.desc())
        .limit(1)
    )
    if patch is None:
        return None, False, False

    run = await session.scalar(
        select(PatchVerificationRun)
        .where(PatchVerificationRun.patch_id == patch.id)
        .where(PatchVerificationRun.completed_at.isnot(None))
        .where(PatchVerificationRun.completed_at <= cutoff)
        .order_by(PatchVerificationRun.completed_at.desc())
        .limit(1)
    )
    #: The run is read through a cutoff filter, so a verification that completed
    #: after the run's boundary cannot make an older patch look verified.
    verified = patch.status == PatchStatus.VERIFIED or (
        run is not None and run.status == VerificationStatus.VERIFIED
    )
    regression = bool(run.regression_detected) if run is not None else False
    return patch, verified, regression


async def _gather_causal(
    session: AsyncSession, *, incident_id: uuid.UUID, cutoff: datetime
) -> Optional[CausalAnalysis]:
    """The most recent completed causal analysis for this incident."""
    stmt = (
        select(CausalAnalysis)
        .where(CausalAnalysis.incident_id == incident_id)
        .where(CausalAnalysis.started_at <= cutoff)
        .order_by(CausalAnalysis.analysis_version.desc())
        .limit(1)
    )
    analysis = await session.scalar(stmt)
    if analysis is None:
        return None
    candidate_count = await session.scalar(
        select(func.count(RootCauseCandidate.id)).where(
            RootCauseCandidate.analysis_id == analysis.id
        )
    )
    if not candidate_count:
        return None
    return analysis


async def _gather_reproduction(
    session: AsyncSession, *, incident_id: uuid.UUID, cutoff: datetime
) -> Optional[ReproductionExperiment]:
    stmt = (
        select(ReproductionExperiment)
        .where(ReproductionExperiment.incident_id == incident_id)
        .where(ReproductionExperiment.started_at <= cutoff)
        .order_by(ReproductionExperiment.experiment_version.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def _gather_forecast(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    component_id: Optional[uuid.UUID],
    start: datetime,
):
    """The forecast that was standing when the incident began (Phase 8)."""
    if component_id is None:
        return None
    from app.models.reliability import ReliabilityForecast

    stmt = (
        select(ReliabilityForecast)
        .where(ReliabilityForecast.project_id == project_id)
        .where(ReliabilityForecast.component_id == component_id)
        .where(ReliabilityForecast.generated_at <= start)
        .order_by(ReliabilityForecast.generated_at.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def _gather_deployment_context(
    session: AsyncSession,
    *,
    component_ids: Sequence[uuid.UUID],
    start: datetime,
    cutoff: datetime,
) -> list[str]:
    """Deployments recent enough to be context for this failure (§20)."""
    if not component_ids:
        return []
    lookback = start - timedelta(hours=DEPLOYMENT_LOOKBACK_HOURS)
    stmt = (
        select(DeploymentEvent)
        .where(DeploymentEvent.component_id.in_(list(component_ids)))
        .where(DeploymentEvent.deployed_at >= lookback)
        .where(DeploymentEvent.deployed_at <= start)
        .where(DeploymentEvent.deployed_at <= cutoff)
        .order_by(DeploymentEvent.deployed_at.desc())
        .limit(50)
    )
    deployments = list((await session.scalars(stmt)).all())
    tokens: set[str] = set()
    for deployment in deployments:
        tokens.add("recent_deployment")
        status = _enum_value(deployment.status)
        if status == DeploymentStatus.FAILED.value:
            tokens.add("failed_deployment")
        elif status == DeploymentStatus.ROLLED_BACK.value:
            tokens.add("rolled_back_deployment")
    if len(deployments) >= 3:
        tokens.add("frequent_deployments")
    return sorted(tokens)


async def _gather_dependency_conditions(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    component_ids: Sequence[uuid.UUID],
    start: datetime,
    end: datetime,
    cutoff: datetime,
) -> list[str]:
    """Whether a dependency of an affected component was itself unhealthy."""
    if not component_ids:
        return []
    dependency_targets = list(
        (
            await session.scalars(
                select(ComponentDependency.target_component_id).where(
                    ComponentDependency.source_component_id.in_(list(component_ids))
                )
            )
        ).all()
    )
    if not dependency_targets:
        return []
    degraded = await session.scalar(
        select(func.count(Anomaly.id))
        .where(Anomaly.project_id == project_id)
        .where(Anomaly.component_id.in_(dependency_targets))
        .where(Anomaly.detected_at >= start - timedelta(hours=1))
        .where(Anomaly.detected_at <= end)
        .where(Anomaly.detected_at <= cutoff)
    )
    tokens: set[str] = {"has_dependencies"}
    if degraded:
        tokens.add("dependency_degraded")
    unhealthy = await session.scalar(
        select(func.count(HealthCheckEvent.id))
        .where(HealthCheckEvent.component_id.in_(dependency_targets))
        .where(HealthCheckEvent.timestamp >= start - timedelta(hours=1))
        .where(HealthCheckEvent.timestamp <= end)
        .where(HealthCheckEvent.timestamp <= cutoff)
        .where(
            HealthCheckEvent.status.in_([HealthStatus.DEGRADED, HealthStatus.UNHEALTHY])
        )
    )
    if unhealthy:
        tokens.add("dependency_unhealthy")
    return sorted(tokens)


async def _gather_health_state(
    session: AsyncSession,
    *,
    component_ids: Sequence[uuid.UUID],
    start: datetime,
    end: datetime,
    cutoff: datetime,
) -> Optional[str]:
    """The worst health state observed for the affected components."""
    if not component_ids:
        return None
    stmt = (
        select(HealthCheckEvent.status)
        .where(HealthCheckEvent.component_id.in_(list(component_ids)))
        .where(HealthCheckEvent.timestamp >= start)
        .where(HealthCheckEvent.timestamp <= end)
        .where(HealthCheckEvent.timestamp <= cutoff)
        .limit(500)
    )
    statuses = [_enum_value(value) for value in (await session.scalars(stmt)).all()]
    for worst in _HEALTH_ORDER:
        if worst in statuses:
            return worst
    return None


async def _resolution_for(
    session: AsyncSession,
    *,
    incident: Incident,
    remediation: Optional[RemediationAction],
    patch_verified: bool,
    recovery_seconds: Optional[int],
) -> tuple[ResolutionSignature, str]:
    """Build the resolution signature and the episode's outcome token."""
    if remediation is None:
        fallback_outcome = (
            OUTCOME_SELF_RECOVERED
            if incident.resolved_at is not None
            else OUTCOME_RESOLVED_UNVERIFIED
        )
        #: A patch that verified without a remediation is still a resolution:
        #: ARGUS fixed the code rather than the running system.
        signature = build_resolution_signature(
            action_types=["APPLY_VERIFIED_PATCH"] if patch_verified else [],
            outcome="effective" if patch_verified else fallback_outcome,
            verification_verdict="verified" if patch_verified else "not_executed",
            patch_verified=patch_verified,
            recovery_seconds=recovery_seconds,
        )
        #: The episode's outcome token is the *normalized* one, so a caller never
        #: has to know that Phase 9 stores ``EFFECTIVE`` while Phase 10 compares
        #: ``effective``.
        return signature, signature.outcome

    verification = await session.scalar(
        select(RemediationVerification)
        .where(RemediationVerification.action_id == remediation.id)
        .order_by(RemediationVerification.created_at.desc())
        .limit(1)
    )
    rollback = await session.scalar(
        select(RemediationRollback)
        .where(RemediationRollback.action_id == remediation.id)
        .order_by(RemediationRollback.created_at.desc())
        .limit(1)
    )
    rollback_performed = rollback is not None or remediation.status in (
        RemediationStatus.ROLLED_BACK,
        RemediationStatus.ROLLING_BACK,
    )

    outcome_value = _enum_value(remediation.outcome) or OUTCOME_RESOLVED_UNVERIFIED
    if remediation.outcome is None and incident.resolved_at is not None:
        #: The action had no explicit outcome recorded. If the incident resolved
        #: anyway, say so honestly rather than inventing effectiveness.
        outcome_value = OUTCOME_RESOLVED_UNVERIFIED

    signature = build_resolution_signature(
        action_types=[_enum_value(remediation.action_type) or "unknown"],
        outcome=outcome_value,
        verification_verdict=(
            _enum_value(verification.verdict)
            if verification is not None
            else "not_executed"
        ),
        rollback_performed=rollback_performed,
        patch_verified=patch_verified,
        recovery_seconds=recovery_seconds,
    )
    return signature, signature.outcome


async def persist_experience(
    session: AsyncSession,
    draft: ExperienceDraft,
    *,
    learning_run_id: Optional[uuid.UUID] = None,
) -> tuple[ReliabilityExperience, str]:
    """Insert or refresh the experience for this incident.

    One experience per incident: a re-run that sees the same episode again must
    update the row it already has, not add a second one, or every later pattern
    count would double. An episode whose outcome or signature *changed* (a
    reopened incident that resolved differently) is updated in place and the
    change is visible through ``updated_at`` and the version counters.

    Returns ``(row, status)`` where status is ``created``, ``updated`` or
    ``unchanged``.
    """
    existing = await session.scalar(
        select(ReliabilityExperience).where(
            ReliabilityExperience.incident_id == draft.incident_id
        )
    )
    failure_dict = draft.failure_signature.as_dict()
    resolution_dict = draft.resolution_signature.as_dict()

    if existing is None:
        row = ReliabilityExperience(
            project_id=draft.project_id,
            environment_id=draft.environment_id,
            incident_id=draft.incident_id,
            primary_component_id=draft.primary_component_id,
            remediation_action_id=draft.remediation_action_id,
            causal_analysis_id=draft.causal_analysis_id,
            root_cause_candidate_id=draft.root_cause_candidate_id,
            reproduction_id=draft.reproduction_id,
            patch_id=draft.patch_id,
            start_time=draft.start_time,
            end_time=draft.end_time,
            recovery_seconds=draft.recovery_seconds,
            failure_signature=failure_dict,
            failure_fingerprint=draft.failure_fingerprint,
            resolution_signature=resolution_dict,
            outcome=draft.outcome,
            provenance=draft.provenance,
            data_quality=draft.quality.level,
            component_ids=draft.component_ids,
            learning_run_id=learning_run_id,
        )
        session.add(row)
        await session.flush()
        return row, "created"

    changed = (
        existing.failure_fingerprint != draft.failure_fingerprint
        or existing.outcome != draft.outcome
        or existing.data_quality != draft.quality.level
        or existing.end_time != draft.end_time
    )
    if not changed:
        return existing, "unchanged"

    existing.resolution_signature = resolution_dict
    existing.failure_signature = failure_dict
    existing.failure_fingerprint = draft.failure_fingerprint
    existing.outcome = draft.outcome
    existing.data_quality = draft.quality.level
    existing.end_time = draft.end_time
    existing.recovery_seconds = draft.recovery_seconds
    existing.component_ids = draft.component_ids
    existing.primary_component_id = draft.primary_component_id
    existing.environment_id = draft.environment_id
    existing.remediation_action_id = draft.remediation_action_id
    existing.causal_analysis_id = draft.causal_analysis_id
    existing.root_cause_candidate_id = draft.root_cause_candidate_id
    existing.reproduction_id = draft.reproduction_id
    existing.patch_id = draft.patch_id
    existing.learning_run_id = learning_run_id or existing.learning_run_id
    await session.flush()
    return existing, "updated"


async def build_experiences(
    session: AsyncSession,
    *,
    incident_ids: Sequence[uuid.UUID],
    cutoff: datetime,
    learning_run_id: Optional[uuid.UUID] = None,
    provenance: DataProvenance = DataProvenance.SYSTEM_GENERATED,
) -> ExperienceBuildResult:
    """Build (or refresh) experiences for a batch of incidents."""
    result = ExperienceBuildResult()
    for incident_id in incident_ids:
        draft, reason = await build_experience_draft(
            session, incident_id=incident_id, as_of=cutoff, provenance=provenance
        )
        if draft is None:
            if reason:
                result.unprocessable[str(incident_id)] = reason
            continue
        if not draft.quality.usable:
            result.flagged += 1
        _row, status = await persist_experience(
            session, draft, learning_run_id=learning_run_id
        )
        if status == "created":
            result.created.append(_row)
        elif status == "updated":
            result.updated.append(_row)
        else:
            result.unchanged += 1
    return result


__all__ = [
    "DEPLOYMENT_LOOKBACK_HOURS",
    "ExperienceBuildResult",
    "ExperienceDraft",
    "OUTCOME_RESOLVED_UNVERIFIED",
    "OUTCOME_SELF_RECOVERED",
    "OUTCOME_UNRESOLVED",
    "QUALITY_LIMITED",
    "QUALITY_OK",
    "QUALITY_POOR",
    "QualityVerdict",
    "build_current_signature",
    "build_experience_draft",
    "build_experiences",
    "gather_failure_signature",
    "incident_component_ids",
    "persist_experience",
]
