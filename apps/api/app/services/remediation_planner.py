"""ARGUS Remediation Planner (Phase 9 §8, §9–§18).

The planner turns *evidence* into a bounded set of candidate remediations. It is
deliberately a rule engine, not a language model: every proposal it produces can
be traced to a specific strategy, and every strategy names the stored rows that
triggered it.

The contract each proposal must satisfy (§9, §6):

* **It names its evidence.** ``supporting_evidence`` carries real row ids — the
  incident, the causal candidate, the reproduction validation, the forecast, the
  verified patch. A strategy that would produce a proposal with no evidence
  produces nothing instead.
* **It declares its preconditions, verification and rollback** before anything is
  assessed, so the safety engine has something to check rather than a promise.
* **It states its limitations.** Every proposal carries the honest reasons it may
  not work; the planner never emits "this will fix it".
* **It never invents a target.** No component resolved means no proposal, which is
  why an incident without an affected component yields nothing at all rather than
  a project-wide guess.

Strategy catalogue (the §9 strategies, mapped to registered actions):

============  ==========================================  ==========================
Strategy      Trigger (all deterministic)                 Action
============  ==========================================  ==========================
RESTART       health checks unhealthy/degraded            RESTART_SERVICE
ROLLBACK      a deployment candidate precedes onset       ROLLBACK_DEPLOYMENT
SCALE         resource saturation or rising latency       SCALE_SERVICE_WITHIN_LIMIT
ISOLATE       a dependency-failure candidate              DISABLE_DEGRADED_DEPENDENCY
QUIESCE       saturation while ARGUS jobs are running     PAUSE_BACKGROUND_JOB
GATE          saturation while indexing is running        DISABLE_FEATURE_FLAG
APPLY_FIX     a Phase 7-verified patch exists             APPLY_VERIFIED_PATCH
============  ==========================================  ==========================

Two strategies deserve a note because they touch ARGUS itself. ``QUIESCE`` and
``GATE`` only fire when the evidence shows ARGUS's *own* background work running
during a resource-saturated incident — they are control-plane remediations of
ARGUS's runtime, not advice about the application. Nothing in this module can
propose disabling the analysis that produced the evidence, because that would be
suppressing the symptom the platform exists to report.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.anomaly import Anomaly
from app.models.causal import (
    CandidateStatus,
    CandidateType,
    CausalAnalysis,
    ConfidenceLevel,
    RootCauseCandidate,
)
from app.models.fix import (
    FixHypothesis,
    Patch,
    PatchStatus,
    PatchVerificationRun,
    VerificationStatus,
)
from app.models.incident import Incident
from app.models.reliability import (
    ForecastRiskLevel,
    ReliabilityForecast,
)
from app.models.reproduction import ReproductionValidation, ValidationOutcome
from app.models.remediation import (
    BlastRadiusScope,
    RemediationActionType,
    RemediationRiskLevel,
    RemediationSourceType,
)
from app.services.remediation_clock import aware, utcnow
from app.services.remediation_evidence import (
    error_rate,
    health_snapshot,
    latency_snapshot,
)
from app.services.remediation_safety import (
    build_rollback_plan,
    build_verification_plan,
)

logger = logging.getLogger(__name__)
settings = get_settings()

#: Confidence mapping from the causal engine's coarse levels to a 0–1 draft
#: confidence. Deliberately conservative: ``HIGH`` causal confidence is not
#: certainty about a fix, so it maps to 0.8 and never to 1.0.
_CONFIDENCE_FLOAT: dict[ConfidenceLevel, float] = {
    ConfidenceLevel.INSUFFICIENT: 0.10,
    ConfidenceLevel.LOW: 0.30,
    ConfidenceLevel.MEDIUM: 0.55,
    ConfidenceLevel.HIGH: 0.80,
}

#: Resource metric names that indicate saturation, matched by substring.
_SATURATION_HINTS = ("cpu", "memory", "disk", "usage_percent", "utilization")
#: The threshold above which a saturation metric counts as saturated.
_SATURATION_RATIO = 0.85


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def proposal_fingerprint(
    *,
    action_type: RemediationActionType,
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID],
    component_id: Optional[uuid.UUID],
    source_type: RemediationSourceType,
    source_id: Optional[uuid.UUID],
    parameters: dict[str, Any],
) -> str:
    """A stable identity for "this remediation of this target from this source".

    Used for deduplication: the same strategy firing twice on the same evidence
    is one proposal, not a queue of identical ones waiting to be approved.
    """
    payload = {
        "action_type": action_type.value,
        "project_id": str(project_id),
        "environment_id": str(environment_id) if environment_id else None,
        "component_id": str(component_id) if component_id else None,
        "source_type": source_type.value,
        "source_id": str(source_id) if source_id else None,
        "parameters": parameters,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


@dataclass
class ProposalDraft:
    """A candidate remediation, before it is persisted or gated."""

    project_id: uuid.UUID
    action_type: RemediationActionType
    source_type: RemediationSourceType
    strategy: str
    problem: str
    recommended_action: str
    expected_effect: str
    rationale: str
    risk_level: RemediationRiskLevel
    blast_radius: BlastRadiusScope
    confidence: float
    confidence_reason: str
    limitations: list[str] = field(default_factory=list)
    supporting_evidence: list[dict[str, Any]] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)
    preconditions: list[dict[str, Any]] = field(default_factory=list)
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    source_id: Optional[uuid.UUID] = None
    incident_id: Optional[uuid.UUID] = None
    forecast_id: Optional[uuid.UUID] = None
    causal_analysis_id: Optional[uuid.UUID] = None
    root_cause_candidate_id: Optional[uuid.UUID] = None
    reproduction_experiment_id: Optional[uuid.UUID] = None
    fix_hypothesis_id: Optional[uuid.UUID] = None
    patch_id: Optional[uuid.UUID] = None
    blast_radius_percent: Optional[float] = None
    generated_by: str = "SYSTEM"
    model_version: Optional[str] = None

    @property
    def fingerprint(self) -> str:
        return proposal_fingerprint(
            action_type=self.action_type,
            project_id=self.project_id,
            environment_id=self.environment_id,
            component_id=self.component_id,
            source_type=self.source_type,
            source_id=self.source_id,
            parameters=self.parameters,
        )

    def as_columns(self) -> dict[str, Any]:
        """The :class:`RemediationProposal` column values for this draft."""
        return {
            "project_id": self.project_id,
            "environment_id": self.environment_id,
            "component_id": self.component_id,
            "action_type": self.action_type,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "incident_id": self.incident_id,
            "forecast_id": self.forecast_id,
            "causal_analysis_id": self.causal_analysis_id,
            "root_cause_candidate_id": self.root_cause_candidate_id,
            "reproduction_experiment_id": self.reproduction_experiment_id,
            "fix_hypothesis_id": self.fix_hypothesis_id,
            "patch_id": self.patch_id,
            "problem": self.problem,
            "recommended_action": self.recommended_action,
            "expected_effect": self.expected_effect,
            "supporting_evidence": self.supporting_evidence,
            "parameters": self.parameters,
            "risk_level": self.risk_level,
            "blast_radius": self.blast_radius,
            "blast_radius_percent": self.blast_radius_percent,
            "preconditions": self.preconditions,
            "verification_plan": build_verification_plan(self.action_type),
            "rollback_plan": build_rollback_plan(self.action_type, self.parameters),
            "confidence": round(self.confidence, 3),
            "confidence_reason": self.confidence_reason,
            "limitations": self.limitations,
            "rationale": self.rationale,
            "strategy": self.strategy,
            "fingerprint": self.fingerprint,
        }


@dataclass
class EvidenceBundle:
    """Everything the planner gathered about one incident or forecast target."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID]
    component_id: Optional[uuid.UUID]
    component_name: Optional[str] = None
    health_total: int = 0
    health_unhealthy: int = 0
    health_degraded: int = 0
    health_latest: Optional[str] = None
    error_rate: Optional[float] = None
    error_samples: int = 0
    latency_mean_ms: Optional[float] = None
    latency_samples: int = 0
    latency_baseline_ms: Optional[float] = None
    saturation_metric: Optional[str] = None
    saturation_value: Optional[float] = None
    anomaly_count: int = 0
    analysis: Optional[CausalAnalysis] = None
    candidates: list[RootCauseCandidate] = field(default_factory=list)
    validations: list[ReproductionValidation] = field(default_factory=list)
    verified_patch: Optional[Patch] = None
    fix_hypothesis: Optional[FixHypothesis] = None
    verified_deployment_event_id: Optional[uuid.UUID] = None
    verified_deployment_version: Optional[str] = None
    running_jobs: list[str] = field(default_factory=list)
    forecast: Optional[ReliabilityForecast] = None
    incident_id: Optional[uuid.UUID] = None

    @property
    def degraded(self) -> bool:
        return (self.health_unhealthy + self.health_degraded) > 0

    @property
    def saturated(self) -> bool:
        return (
            self.saturation_value is not None
            and self.saturation_value >= _SATURATION_RATIO * 100.0
        )


class RemediationPlanner:
    """Generates remediation proposals from stored evidence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- public entry points ------------------------------------------------

    async def plan_for_incident(
        self,
        incident: Incident,
        *,
        now: Optional[datetime] = None,
    ) -> list[ProposalDraft]:
        """Every strategy that the incident's evidence supports."""
        now = aware(now or utcnow())
        bundle = await self._gather_incident_evidence(incident, now=now)
        if bundle is None or bundle.component_id is None:
            logger.info(
                "planner: incident %s has no resolvable component; no proposals",
                incident.id,
            )
            return []
        drafts = self._strategies(bundle, now=now)
        return self._bounded(drafts)

    async def plan_for_forecast(
        self,
        forecast: ReliabilityForecast,
        *,
        now: Optional[datetime] = None,
    ) -> list[ProposalDraft]:
        """Strategies for an elevated forecast (prediction-driven remediation)."""
        now = aware(now or utcnow())
        if forecast.risk_level in (ForecastRiskLevel.UNKNOWN,):
            # An unclassifiable forecast is not a reason to change a system.
            return []
        bundle = await self._gather_forecast_evidence(forecast, now=now)
        if bundle is None or bundle.component_id is None:
            return []
        drafts = self._strategies(bundle, now=now)
        return self._bounded(drafts)

    # -- evidence -----------------------------------------------------------

    async def _resolve_component(
        self, *, component_id: Optional[uuid.UUID], project_id: uuid.UUID
    ) -> Optional[uuid.UUID]:
        """Confirm a component id is real and in scope."""
        if component_id is None:
            return None
        from app.models.system import SystemComponent

        row = await self._session.get(SystemComponent, component_id)
        if row is None or row.project_id != project_id:
            return None
        return row.id

    async def _gather_incident_evidence(
        self, incident: Incident, *, now: datetime
    ) -> Optional[EvidenceBundle]:
        component_id = await self._resolve_component(
            component_id=incident.primary_component_id,
            project_id=incident.project_id,
        )
        if component_id is None:
            return None
        bundle = EvidenceBundle(
            project_id=incident.project_id,
            environment_id=incident.environment_id,
            component_id=component_id,
            incident_id=incident.id,
        )
        bundle.component_name = await self._component_name(component_id)
        onset = aware(incident.detected_at)
        window_seconds = max(900, settings.RELIABILITY_FEATURE_WINDOW_SECONDS)
        start = now - timedelta(seconds=window_seconds)

        await self._fill_observations(bundle, start=start, end=now)
        bundle.anomaly_count = int(
            (
                await self._session.execute(
                    select(func.count(Anomaly.id))
                    .where(Anomaly.project_id == incident.project_id)
                    .where(Anomaly.component_id == component_id)
                    .where(Anomaly.detected_at >= onset - timedelta(hours=1))
                )
            ).scalar()
            or 0
        )

        bundle.analysis, bundle.candidates = await self._latest_analysis(
            incident.project_id, incident.id
        )
        bundle.validations = await self._validations_for(
            incident.project_id, incident.id
        )
        (
            bundle.fix_hypothesis,
            bundle.verified_patch,
        ) = await self._verified_patch_for(incident.project_id, incident.id)
        (
            bundle.verified_deployment_event_id,
            bundle.verified_deployment_version,
        ) = await self._preceding_deployment(incident, now=now)
        bundle.running_jobs = await self._running_platform_jobs(
            incident.project_id, start=onset - timedelta(minutes=30), end=now
        )
        return bundle

    async def _gather_forecast_evidence(
        self, forecast: ReliabilityForecast, *, now: datetime
    ) -> Optional[EvidenceBundle]:
        component_id = await self._resolve_component(
            component_id=forecast.component_id, project_id=forecast.project_id
        )
        if component_id is None:
            return None
        bundle = EvidenceBundle(
            project_id=forecast.project_id,
            environment_id=forecast.environment_id,
            component_id=component_id,
            forecast=forecast,
        )
        bundle.component_name = await self._component_name(component_id)
        window_seconds = max(900, settings.RELIABILITY_FEATURE_WINDOW_SECONDS)
        start = now - timedelta(seconds=window_seconds)
        await self._fill_observations(bundle, start=start, end=now)
        bundle.running_jobs = await self._running_platform_jobs(
            forecast.project_id, start=start, end=now
        )
        bundle.anomaly_count = int(
            (
                await self._session.execute(
                    select(func.count(Anomaly.id))
                    .where(Anomaly.project_id == forecast.project_id)
                    .where(Anomaly.component_id == component_id)
                    .where(Anomaly.detected_at >= start)
                )
            ).scalar()
            or 0
        )
        return bundle

    async def _fill_observations(
        self, bundle: EvidenceBundle, *, start: datetime, end: datetime
    ) -> None:
        health = await health_snapshot(
            self._session,
            bundle.project_id,
            component_id=bundle.component_id,
            environment_id=bundle.environment_id,
            start=start,
            end=end,
        )
        bundle.health_total = health.total
        bundle.health_unhealthy = health.unhealthy
        bundle.health_degraded = health.degraded
        bundle.health_latest = (
            health.latest_status.value if health.latest_status else None
        )

        errors = await error_rate(
            self._session,
            bundle.project_id,
            component_id=bundle.component_id,
            environment_id=bundle.environment_id,
            start=start,
            end=end,
        )
        bundle.error_rate = errors.observed
        bundle.error_samples = errors.samples

        latency = await latency_snapshot(
            self._session,
            bundle.project_id,
            component_id=bundle.component_id,
            environment_id=bundle.environment_id,
            start=start,
            end=end,
        )
        bundle.latency_mean_ms = latency.mean
        bundle.latency_samples = latency.samples
        baseline = await latency_snapshot(
            self._session,
            bundle.project_id,
            component_id=bundle.component_id,
            environment_id=bundle.environment_id,
            start=start
            - timedelta(seconds=max(900, settings.RELIABILITY_BASELINE_WINDOW_SECONDS)),
            end=start,
        )
        bundle.latency_baseline_ms = baseline.mean

        saturation = await self._saturation(bundle, start=start, end=end)
        if saturation is not None:
            name, value = saturation
            bundle.saturation_metric = name
            bundle.saturation_value = value

    async def _saturation(
        self, bundle: EvidenceBundle, *, start: datetime, end: datetime
    ) -> Optional[tuple[str, float]]:
        """The highest saturation metric observed, if any name matches."""
        from app.models.observability import MetricRecord

        conditions = [
            MetricRecord.project_id == bundle.project_id,
            MetricRecord.timestamp >= start,
            MetricRecord.timestamp <= end,
        ]
        if bundle.component_id is not None:
            conditions.append(MetricRecord.component_id == bundle.component_id)
        rows = (
            await self._session.execute(
                select(MetricRecord.metric_name, func.max(MetricRecord.value))
                .where(and_(*conditions))
                .group_by(MetricRecord.metric_name)
                .limit(500)
            )
        ).all()
        best: Optional[tuple[str, float]] = None
        for name, value in rows:
            if value is None:
                continue
            lowered = (name or "").lower()
            if not any(hint in lowered for hint in _SATURATION_HINTS):
                continue
            numeric = float(value)
            if numeric <= 1.0:  # a 0..1 ratio metric
                numeric *= 100.0
            if best is None or numeric > best[1]:
                best = (name, numeric)
        return best

    async def _component_name(self, component_id: uuid.UUID) -> Optional[str]:
        from app.models.system import SystemComponent

        row = await self._session.get(SystemComponent, component_id)
        return row.name if row is not None else None

    async def _latest_analysis(
        self, project_id: uuid.UUID, incident_id: uuid.UUID
    ) -> tuple[Optional[CausalAnalysis], list[RootCauseCandidate]]:
        analysis = (
            (
                await self._session.execute(
                    select(CausalAnalysis)
                    .where(CausalAnalysis.incident_id == incident_id)
                    .order_by(CausalAnalysis.analysis_version.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if analysis is None:
            return None, []
        candidates = list(
            (
                await self._session.execute(
                    select(RootCauseCandidate)
                    .where(RootCauseCandidate.analysis_id == analysis.id)
                    .where(RootCauseCandidate.status != CandidateStatus.REFUTED)
                    .order_by(RootCauseCandidate.score.desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        return analysis, candidates

    async def _validations_for(
        self, project_id: uuid.UUID, incident_id: uuid.UUID
    ) -> list[ReproductionValidation]:
        """Reproduction validations for this incident's causal candidates.

        The link is ``validation.candidate_id -> candidate.analysis_id ->
        analysis.incident_id``; the candidate ids are fetched first so the query
        stays a simple ``IN`` rather than a three-way join.
        """
        candidate_ids = list(
            (
                await self._session.execute(
                    select(RootCauseCandidate.id)
                    .join(
                        CausalAnalysis,
                        CausalAnalysis.id == RootCauseCandidate.analysis_id,
                    )
                    .where(CausalAnalysis.incident_id == incident_id)
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        if not candidate_ids:
            return []
        rows = (
            (
                await self._session.execute(
                    select(ReproductionValidation)
                    .where(ReproductionValidation.project_id == project_id)
                    .where(ReproductionValidation.candidate_id.in_(candidate_ids))
                    .order_by(ReproductionValidation.created_at.desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def _verified_patch_for(
        self, project_id: uuid.UUID, incident_id: uuid.UUID
    ) -> tuple[Optional[FixHypothesis], Optional[Patch]]:
        """The newest Phase 7 hypothesis for this incident with a verified patch."""
        hypotheses = list(
            (
                await self._session.execute(
                    select(FixHypothesis)
                    .where(FixHypothesis.project_id == project_id)
                    .where(FixHypothesis.incident_id == incident_id)
                    .order_by(FixHypothesis.created_at.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        for hypothesis in hypotheses:
            patch = (
                (
                    await self._session.execute(
                        select(Patch)
                        .where(Patch.fix_hypothesis_id == hypothesis.id)
                        .where(Patch.status == PatchStatus.VERIFIED)
                        .order_by(Patch.created_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if patch is None:
                continue
            run = (
                (
                    await self._session.execute(
                        select(PatchVerificationRun)
                        .where(PatchVerificationRun.patch_id == patch.id)
                        .where(
                            PatchVerificationRun.status == VerificationStatus.VERIFIED
                        )
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if run is not None:
                return hypothesis, patch
        return None, None

    async def _preceding_deployment(
        self, incident: Incident, *, now: datetime
    ) -> tuple[Optional[uuid.UUID], Optional[str]]:
        """A deployment recorded before the incident onset, if one exists.

        Only deployments that *precede* onset qualify — a change made after the
        degradation began cannot explain it, which is the same rule Phase 4's
        change analyzer enforces.
        """
        from app.models.deployment import DeploymentEvent

        onset = aware(incident.detected_at)
        lookback = onset - timedelta(
            seconds=settings.RELIABILITY_BASELINE_WINDOW_SECONDS
        )
        stmt = (
            select(DeploymentEvent)
            .where(DeploymentEvent.project_id == incident.project_id)
            .where(DeploymentEvent.deployed_at >= lookback)
            .where(DeploymentEvent.deployed_at < onset)
        )
        if incident.primary_component_id is not None:
            stmt = stmt.where(
                DeploymentEvent.component_id == incident.primary_component_id
            )
        elif incident.environment_id is not None:
            stmt = stmt.where(DeploymentEvent.environment_id == incident.environment_id)
        row = (
            (
                await self._session.execute(
                    stmt.order_by(DeploymentEvent.deployed_at.desc()).limit(1)
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return None, None
        return row.id, row.version or row.deployment_id

    async def _running_platform_jobs(
        self, project_id: uuid.UUID, *, start: datetime, end: datetime
    ) -> list[str]:
        """ARGUS background work that produced output during the window.

        This is what licenses the ``QUIESCE``/``GATE`` strategies: they are only
        proposed when the platform's own jobs demonstrably ran while the target
        was saturated.
        """
        jobs: list[str] = []
        checks = (
            ("reproduction_sweep", "reproduction_runs", "created_at"),
            ("code_sweep", "code_index_runs", "created_at"),
            ("reliability_sweep", "reliability_forecasts", "created_at"),
            ("fix_sweep", "patch_verification_runs", "created_at"),
        )
        from sqlalchemy import text as sa_text

        for job, table, column in checks:
            # Table and column names are a fixed, reviewed list — never input.
            sql = sa_text(
                f"SELECT COUNT(*) FROM {table} "  # noqa: S608
                f"WHERE project_id = :project_id AND {column} >= :start "
                f"AND {column} <= :end"
            )
            try:
                count = (
                    await self._session.execute(
                        sql,
                        {
                            "project_id": str(project_id),
                            "start": start,
                            "end": end,
                        },
                    )
                ).scalar()
            except Exception:  # pragma: no cover - table absent in some deployments
                continue
            if count and int(count) > 0:
                jobs.append(job)
        return jobs

    # -- strategies ---------------------------------------------------------

    def _strategies(
        self, bundle: EvidenceBundle, *, now: datetime
    ) -> list[ProposalDraft]:
        drafts: list[ProposalDraft] = []
        drafts.extend(self._strategy_restart(bundle, now=now))
        drafts.extend(self._strategy_rollback(bundle, now=now))
        drafts.extend(self._strategy_scale(bundle, now=now))
        drafts.extend(self._strategy_isolate(bundle, now=now))
        drafts.extend(self._strategy_quiesce(bundle, now=now))
        drafts.extend(self._strategy_gate(bundle, now=now))
        drafts.extend(self._strategy_apply_fix(bundle, now=now))
        return drafts

    def _base_limits(self, bundle: EvidenceBundle) -> list[str]:
        """Limitations that apply to every remediation of this target."""
        limits = [
            "the evidence identifies where the problem is observed, not "
            "necessarily where it originates; a downstream symptom can have an "
            "upstream cause",
            "verification observes a window of telemetry after the action; a "
            "failure that recurs later than that window will not be caught here",
        ]
        if bundle.health_total == 0:
            limits.append(
                "no health checks were recorded in the window, so the effect of "
                "this action cannot be confirmed from health telemetry"
            )
        if bundle.error_samples == 0:
            limits.append(
                "no error-rate telemetry was recorded in the window; error-rate "
                "verification will be inconclusive for this target"
            )
        return limits

    def _evidence_rows(self, bundle: EvidenceBundle) -> list[dict[str, Any]]:
        """Structured evidence references, all of them real row ids."""
        rows: list[dict[str, Any]] = []
        if bundle.health_total:
            rows.append(
                {
                    "kind": "health_checks",
                    "count": bundle.health_total,
                    "unhealthy": bundle.health_unhealthy,
                    "degraded": bundle.health_degraded,
                    "latest_status": bundle.health_latest,
                }
            )
        if bundle.error_samples:
            rows.append(
                {
                    "kind": "error_rate",
                    "value": bundle.error_rate,
                    "samples": bundle.error_samples,
                }
            )
        if bundle.latency_samples:
            rows.append(
                {
                    "kind": "latency_ms",
                    "value": bundle.latency_mean_ms,
                    "baseline": bundle.latency_baseline_ms,
                    "samples": bundle.latency_samples,
                }
            )
        if bundle.saturation_value is not None:
            rows.append(
                {
                    "kind": "saturation",
                    "metric": bundle.saturation_metric,
                    "value": bundle.saturation_value,
                }
            )
        if bundle.anomaly_count:
            rows.append({"kind": "anomalies", "count": bundle.anomaly_count})
        if bundle.analysis is not None:
            rows.append(
                {
                    "kind": "causal_analysis",
                    "id": str(bundle.analysis.id),
                    "version": bundle.analysis.analysis_version,
                    "confidence": bundle.analysis.overall_confidence.value,
                }
            )
        for candidate in bundle.candidates[:3]:
            rows.append(
                {
                    "kind": "root_cause_candidate",
                    "id": str(candidate.id),
                    "candidate_type": candidate.candidate_type.value,
                    "score": candidate.score,
                    "confidence": candidate.confidence.value,
                }
            )
        for validation in bundle.validations[:3]:
            rows.append(
                {
                    "kind": "reproduction_validation",
                    "id": str(validation.id),
                    "outcome": validation.outcome.value,
                    "confidence": validation.confidence.value,
                }
            )
        if bundle.forecast is not None:
            rows.append(
                {
                    "kind": "reliability_forecast",
                    "id": str(bundle.forecast.id),
                    "risk_level": bundle.forecast.risk_level.value,
                    "risk_score": bundle.forecast.risk_score,
                    "horizon": bundle.forecast.forecast_horizon.value,
                }
            )
        return rows

    def _candidate_confidence(self, bundle: EvidenceBundle) -> tuple[float, str]:
        """Draft confidence, derived from the strongest real evidence available."""
        best = 0.10
        reason = "only the observation window supports this remediation"
        reproduction_supported = any(
            v.outcome == ValidationOutcome.SUPPORTED for v in bundle.validations
        )
        if reproduction_supported:
            supported = max(
                (
                    _CONFIDENCE_FLOAT.get(v.confidence, 0.3)
                    for v in bundle.validations
                    if v.outcome == ValidationOutcome.SUPPORTED
                ),
                default=0.5,
            )
            best = max(best, min(0.9, supported + 0.1))
            reason = "a reproduction experiment reproduced the failure"
        elif bundle.validations:
            inconclusive = any(
                v.outcome == ValidationOutcome.INCONCLUSIVE for v in bundle.validations
            )
            best = max(best, 0.35 if inconclusive else 0.25)
            reason = "reproduction was attempted but did not confirm the hypothesis"
        elif bundle.candidates:
            top = bundle.candidates[0]
            best = max(best, _CONFIDENCE_FLOAT.get(top.confidence, 0.3))
            reason = (
                f"the leading causal candidate is a {top.candidate_type.value} "
                f"hypothesis at {top.confidence.value} confidence"
            )
        if bundle.forecast is not None and bundle.forecast.risk_level in (
            ForecastRiskLevel.HIGH,
            ForecastRiskLevel.CRITICAL,
        ):
            best = max(best, 0.4)
            reason = f"{reason}; a {bundle.forecast.risk_level.value} forecast agrees"
        return round(min(best, 0.9), 3), reason

    def _candidate_of_type(
        self, bundle: EvidenceBundle, *types: CandidateType
    ) -> Optional[RootCauseCandidate]:
        for candidate in bundle.candidates:
            if candidate.candidate_type in types:
                return candidate
        return None

    def _strategy_restart(
        self, bundle: EvidenceBundle, *, now: datetime
    ) -> list[ProposalDraft]:
        """RESTART: the component is observably unhealthy now."""
        if not bundle.degraded:
            return []
        confidence, reason = self._candidate_confidence(bundle)
        name = bundle.component_name or "the affected component"
        restart_signal = (
            f"{bundle.health_unhealthy} unhealthy and {bundle.health_degraded} "
            f"degraded health checks in the observation window"
        )
        return [
            ProposalDraft(
                project_id=bundle.project_id,
                environment_id=bundle.environment_id,
                component_id=bundle.component_id,
                action_type=RemediationActionType.RESTART_SERVICE,
                source_type=(
                    RemediationSourceType.INCIDENT
                    if bundle.forecast is None
                    else RemediationSourceType.RELIABILITY_FORECAST
                ),
                source_id=(
                    bundle.forecast.id if bundle.forecast else bundle.incident_id
                ),
                strategy="RESTART",
                problem=(f"{name} is reporting poor health: {restart_signal}."),
                recommended_action=(
                    f"restart {name} so it re-establishes a clean runtime state, "
                    "if the operator agrees the degradation is runtime-state related"
                ),
                expected_effect=(
                    "health checks for the component return to a healthy state and "
                    "the error rate falls back towards its baseline"
                ),
                rationale=(
                    "a restart clears accumulated runtime state (leaked "
                    "connections, wedged workers, poisoned caches) and is the "
                    "cheapest reversible-ish intervention when the component is "
                    "itself unhealthy"
                ),
                risk_level=RemediationRiskLevel.MEDIUM,
                blast_radius=BlastRadiusScope.SINGLE_COMPONENT,
                confidence=confidence,
                confidence_reason=reason,
                limitations=[
                    *self._base_limits(bundle),
                    "a restart does not fix a defect: if the cause is code or data, "
                    "the degradation will return",
                    "a restart is not reversible — the pre-restart in-memory state "
                    "is gone — so it always requires human approval",
                ],
                supporting_evidence=self._evidence_rows(bundle),
                parameters={"graceful": True},
                preconditions=[
                    {
                        "name": "component_unhealthy",
                        "kind": "component_healthy",
                        "description": "the component is showing degraded health",
                    }
                ],
                root_cause_candidate_id=(
                    bundle.candidates[0].id if bundle.candidates else None
                ),
                causal_analysis_id=bundle.analysis.id if bundle.analysis else None,
                incident_id=bundle.incident_id,
                forecast_id=bundle.forecast.id if bundle.forecast else None,
            )
        ]

    def _strategy_rollback(
        self, bundle: EvidenceBundle, *, now: datetime
    ) -> list[ProposalDraft]:
        """ROLLBACK: a deployment precedes onset and the timing is suspicious."""
        change = self._candidate_of_type(
            bundle, CandidateType.DEPLOYMENT, CandidateType.CONFIGURATION_CHANGE
        )
        deployment_id = bundle.verified_deployment_event_id
        if change is None or deployment_id is None:
            return []
        confidence, reason = self._candidate_confidence(bundle)
        version = bundle.verified_deployment_version or "the previous revision"
        name = bundle.component_name or "the affected component"
        return [
            ProposalDraft(
                project_id=bundle.project_id,
                environment_id=bundle.environment_id,
                component_id=bundle.component_id,
                action_type=RemediationActionType.ROLLBACK_DEPLOYMENT,
                source_type=RemediationSourceType.ROOT_CAUSE_ANALYSIS,
                source_id=change.id,
                strategy="ROLLBACK",
                problem=(
                    f"a deployment ({version}) was recorded for {name} before the "
                    "degradation began, and the causal analysis ranks it as a "
                    "contributing change"
                ),
                recommended_action=(
                    f"roll {name} back to {version}, which is the revision in "
                    "effect before the change"
                ),
                expected_effect=(
                    "the degradation stops within the verification window, "
                    "consistent with the change having caused it"
                ),
                rationale=(
                    "rolling back is the highest-signal test of a change "
                    "hypothesis: if the change caused the degradation, reverting "
                    "it should end it"
                ),
                risk_level=RemediationRiskLevel.HIGH,
                blast_radius=BlastRadiusScope.SINGLE_COMPONENT,
                confidence=confidence,
                confidence_reason=reason,
                limitations=[
                    *self._base_limits(bundle),
                    "a rollback discards any work the deployment delivered; the "
                    "change may have been necessary",
                    "precedence is not causation: the deployment may be "
                    "coincidental to the onset",
                    "ARGUS never deploys or rolls back automatically; this is "
                    "proposed for a human to execute",
                ],
                supporting_evidence=self._evidence_rows(bundle),
                parameters={"deployment_id": str(deployment_id)},
                preconditions=[
                    {
                        "name": "deployment_precedes_onset",
                        "kind": "control_not_applied",
                        "description": "the deployment was recorded before onset",
                    }
                ],
                causal_analysis_id=bundle.analysis.id if bundle.analysis else None,
                root_cause_candidate_id=change.id,
                incident_id=bundle.incident_id,
                forecast_id=bundle.forecast.id if bundle.forecast else None,
            )
        ]

    def _strategy_scale(
        self, bundle: EvidenceBundle, *, now: datetime
    ) -> list[ProposalDraft]:
        """SCALE: capacity pressure, not a defect."""
        if not bundle.saturated:
            return []
        rising_latency = (
            bundle.latency_mean_ms is not None
            and bundle.latency_baseline_ms is not None
            and bundle.latency_mean_ms > bundle.latency_baseline_ms
        )
        if not rising_latency and (bundle.error_rate or 0.0) < 0.05:
            return []
        confidence, reason = self._candidate_confidence(bundle)
        name = bundle.component_name or "the affected component"
        return [
            ProposalDraft(
                project_id=bundle.project_id,
                environment_id=bundle.environment_id,
                component_id=bundle.component_id,
                action_type=RemediationActionType.SCALE_SERVICE_WITHIN_LIMIT,
                source_type=(
                    RemediationSourceType.RELIABILITY_FORECAST
                    if bundle.forecast is not None
                    else RemediationSourceType.INCIDENT
                ),
                source_id=bundle.forecast.id if bundle.forecast else None,
                strategy="SCALE",
                problem=(
                    f"{name} is saturated: {bundle.saturation_metric} reached "
                    f"{bundle.saturation_value:.1f}%"
                    + (
                        f" while latency rose from {bundle.latency_baseline_ms:.1f}ms "
                        f"to {bundle.latency_mean_ms:.1f}ms"
                        if rising_latency
                        else ""
                    )
                ),
                recommended_action=(
                    f"add replicas to {name} within the configured ceiling while the "
                    "capacity pressure is investigated"
                ),
                expected_effect=(
                    "utilisation per instance falls and latency returns towards its "
                    "baseline"
                ),
                rationale=(
                    "saturation with rising latency is a capacity signature; adding "
                    "bounded capacity relieves it without changing behaviour"
                ),
                risk_level=RemediationRiskLevel.MEDIUM,
                blast_radius=BlastRadiusScope.SINGLE_COMPONENT,
                confidence=confidence,
                confidence_reason=reason,
                limitations=[
                    *self._base_limits(bundle),
                    "adding capacity costs money and can mask a defect rather than "
                    "fix it",
                    "a leak or a pathological query will consume any capacity added",
                ],
                supporting_evidence=self._evidence_rows(bundle),
                parameters={"replicas": 2},
                incident_id=bundle.incident_id,
                forecast_id=bundle.forecast.id if bundle.forecast else None,
            )
        ]

    def _strategy_isolate(
        self, bundle: EvidenceBundle, *, now: datetime
    ) -> list[ProposalDraft]:
        """ISOLATE: a dependency-failure hypothesis, contained by suppression."""
        candidate = self._candidate_of_type(
            bundle, CandidateType.DEPENDENCY_FAILURE, CandidateType.EXTERNAL_DEPENDENCY
        )
        if candidate is None or candidate.component_id is None:
            return []
        confidence, reason = self._candidate_confidence(bundle)
        return [
            ProposalDraft(
                project_id=bundle.project_id,
                environment_id=bundle.environment_id,
                component_id=candidate.component_id,
                action_type=RemediationActionType.DISABLE_DEGRADED_DEPENDENCY,
                source_type=RemediationSourceType.ROOT_CAUSE_ANALYSIS,
                source_id=candidate.id,
                strategy="ISOLATE",
                problem=(
                    "the causal analysis ranks a failing dependency as a "
                    "contributing hypothesis, and its signals are masking others"
                ),
                recommended_action=(
                    "temporarily exclude that dependency from correlation so the "
                    "remaining signals are legible"
                ),
                expected_effect=(
                    "correlation stops attributing symptoms to the excluded "
                    "dependency, and other causes become visible"
                ),
                rationale=(
                    "a failing dependency that fails loudly can hide the real "
                    "signal behind it; suppressing it is reversible and changes "
                    "only what ARGUS concludes"
                ),
                risk_level=RemediationRiskLevel.LOW,
                blast_radius=BlastRadiusScope.SINGLE_COMPONENT,
                confidence=confidence,
                confidence_reason=reason,
                limitations=[
                    *self._base_limits(bundle),
                    "this suppresses a *correlation signal*, not the dependency's "
                    "traffic: the dependency keeps failing",
                    "while suppressed, genuine failures of that dependency will not "
                    "be correlated",
                ],
                supporting_evidence=self._evidence_rows(bundle),
                parameters={
                    "dependency_component_id": str(candidate.component_id),
                    "duration_seconds": 1800,
                },
                causal_analysis_id=bundle.analysis.id if bundle.analysis else None,
                root_cause_candidate_id=candidate.id,
                incident_id=bundle.incident_id,
            )
        ]

    def _strategy_quiesce(
        self, bundle: EvidenceBundle, *, now: datetime
    ) -> list[ProposalDraft]:
        """QUIESCE: ARGUS's own work is running against a saturated target."""
        if not bundle.saturated or not bundle.running_jobs:
            return []
        job = bundle.running_jobs[0]
        if job not in set(settings.REMEDIATION_KNOWN_BACKGROUND_JOBS):
            return []
        return [
            ProposalDraft(
                project_id=bundle.project_id,
                environment_id=bundle.environment_id,
                component_id=bundle.component_id,
                action_type=RemediationActionType.PAUSE_BACKGROUND_JOB,
                source_type=RemediationSourceType.INCIDENT,
                source_id=None,
                strategy="QUIESCE",
                problem=(
                    f"the target is saturated ({bundle.saturation_metric} at "
                    f"{bundle.saturation_value:.1f}%) while ARGUS's own "
                    f"{job} produced work against this project"
                ),
                recommended_action=(
                    f"pause {job} for 30 minutes to remove the platform's own load "
                    "from the affected scope"
                ),
                expected_effect=(
                    "the paused job stops producing work for the scope, and "
                    "saturation is relieved if the platform's load was a factor"
                ),
                rationale=(
                    "ARGUS's analysis is not free: indexing, verification and "
                    "reproduction consume the same resources as the systems they "
                    "observe. Pausing one is reversible and bounded, so it is "
                    "cheap to try and cheap to undo"
                ),
                risk_level=RemediationRiskLevel.LOW,
                blast_radius=BlastRadiusScope.ENVIRONMENT,
                confidence=0.35,
                confidence_reason=(
                    "the platform's own jobs demonstrably ran in the window and the "
                    "target is saturated, which is correlation rather than proof"
                ),
                limitations=[
                    *self._base_limits(bundle),
                    "pausing ARGUS's analysis means new evidence stops arriving for "
                    "the duration",
                    "this addresses the platform's contribution to load, not the "
                    "application's own resource use",
                    "the correlation between the job and the saturation is "
                    "temporal, not causal",
                ],
                supporting_evidence=[
                    *self._evidence_rows(bundle),
                    {"kind": "platform_job", "job": job},
                ],
                parameters={"job": job, "duration_seconds": 1800},
                incident_id=bundle.incident_id,
                forecast_id=bundle.forecast.id if bundle.forecast else None,
            )
        ]

    def _strategy_gate(
        self, bundle: EvidenceBundle, *, now: datetime
    ) -> list[ProposalDraft]:
        """GATE: the indexing pipeline is running against a saturated target."""
        if not bundle.saturated or "code_sweep" not in bundle.running_jobs:
            return []
        return [
            ProposalDraft(
                project_id=bundle.project_id,
                environment_id=bundle.environment_id,
                component_id=bundle.component_id,
                action_type=RemediationActionType.DISABLE_FEATURE_FLAG,
                source_type=RemediationSourceType.INCIDENT,
                source_id=None,
                strategy="GATE",
                problem=(
                    "the target is saturated while ARGUS indexing produced work "
                    "for this project"
                ),
                recommended_action=(
                    "disable the ARGUS code_indexing flag for this environment"
                ),
                expected_effect=(
                    "no further indexing runs are started for the scope until the "
                    "flag is re-enabled"
                ),
                rationale=(
                    "indexing is the most expensive ARGUS-owned activity and its "
                    "own flag makes it stoppable without touching the worker"
                ),
                risk_level=RemediationRiskLevel.LOW,
                blast_radius=BlastRadiusScope.ENVIRONMENT,
                confidence=0.35,
                confidence_reason=(
                    "indexing ran in the window and the target is saturated; the "
                    "relationship is temporal"
                ),
                limitations=[
                    *self._base_limits(bundle),
                    "code intelligence stops updating while the flag is disabled",
                    "this does not reduce the application's own resource usage",
                ],
                supporting_evidence=[
                    *self._evidence_rows(bundle),
                    {"kind": "platform_job", "job": "code_sweep"},
                ],
                parameters={"flag": "code_indexing"},
                incident_id=bundle.incident_id,
                forecast_id=bundle.forecast.id if bundle.forecast else None,
            )
        ]

    def _strategy_apply_fix(
        self, bundle: EvidenceBundle, *, now: datetime
    ) -> list[ProposalDraft]:
        """APPLY_FIX: a Phase 7-verified patch exists for this incident."""
        patch = bundle.verified_patch
        if patch is None:
            return []
        confidence = max(0.5, self._candidate_confidence(bundle)[0])
        return [
            ProposalDraft(
                project_id=bundle.project_id,
                environment_id=bundle.environment_id,
                component_id=bundle.component_id,
                action_type=RemediationActionType.APPLY_VERIFIED_PATCH,
                source_type=RemediationSourceType.VERIFIED_PATCH,
                source_id=patch.id,
                strategy="APPLY_FIX",
                problem=(
                    "a patch for this incident has already passed Phase 7 "
                    "verification, including a reproduction check and a regression "
                    "test"
                ),
                recommended_action=(
                    "apply the verified patch to an isolated workspace copy and "
                    "re-run its verification there"
                ),
                expected_effect=(
                    "the patch applies cleanly to the current revision and its "
                    "verification suite still passes"
                ),
                rationale=(
                    "the change is already evidence-backed; re-validating it "
                    "against the current revision is what makes it safe to hand to "
                    "a human for deployment"
                ),
                risk_level=RemediationRiskLevel.HIGH,
                blast_radius=BlastRadiusScope.SINGLE_COMPONENT,
                confidence=confidence,
                confidence_reason=(
                    "the patch has a passed verification run, including a "
                    "regression test, from Phase 7"
                ),
                limitations=[
                    *self._base_limits(bundle),
                    "the patch was verified against the revision it was generated "
                    "from; the base may have moved since",
                    "ARGUS does not deploy: this validates the change in a "
                    "workspace and stops there",
                ],
                supporting_evidence=[
                    *self._evidence_rows(bundle),
                    {
                        "kind": "verified_patch",
                        "id": str(patch.id),
                        "fix_hypothesis_id": (
                            str(bundle.fix_hypothesis.id)
                            if bundle.fix_hypothesis
                            else None
                        ),
                    },
                ],
                parameters={"patch_id": str(patch.id), "verify_tests": True},
                fix_hypothesis_id=(
                    bundle.fix_hypothesis.id if bundle.fix_hypothesis else None
                ),
                patch_id=patch.id,
                incident_id=bundle.incident_id,
                forecast_id=bundle.forecast.id if bundle.forecast else None,
            )
        ]

    # -- bounding -----------------------------------------------------------

    def _bounded(self, drafts: Sequence[ProposalDraft]) -> list[ProposalDraft]:
        """Apply the §8 bounds: a confidence floor and a per-run count cap.

        Ordered by confidence so the cap keeps the best-evidenced proposals. A
        draft below the confidence floor is dropped rather than surfaced as a
        guess — an actionable-looking proposal with no evidence is worse than no
        proposal, because someone will approve it.
        """
        floor = settings.REMEDIATION_MIN_PROPOSAL_CONFIDENCE
        kept = [draft for draft in drafts if draft.confidence >= floor]
        kept.sort(key=lambda d: (-d.confidence, d.action_type.value))
        return kept[: settings.REMEDIATION_MAX_PROPOSALS_PER_RUN]


__all__ = [
    "EvidenceBundle",
    "ProposalDraft",
    "RemediationPlanner",
    "proposal_fingerprint",
]
