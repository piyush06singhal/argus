"""ARGUS Pattern Miners (Phase 10 §17–§22, §65).

Turns a corpus of :class:`~app.models.intelligence.ReliabilityExperience` rows
into *candidate* patterns. Nothing here activates anything: a miner proposes,
:mod:`app.services.knowledge_validation` judges, and
:mod:`app.services.knowledge_lifecycle` decides what becomes knowledge.

The rule the whole module is written around:

    **an observation is not a rule.**

So each miner emits, alongside the pattern, the facts that make it falsifiable —
``sample_count``, ``success_count``, the coverage window, and the experience ids
that produced it. A pattern with two observations is emitted as a candidate with
``sample_count=2`` and says so in its title; it is never described as a finding.

Miners are grouped by the question they answer:

* :class:`FailurePatternMiner`      — what recurs (§18)
* :class:`RemediationPatternMiner`  — what resolved it, and how often (§17)
* :class:`RegressionPatternMiner`   — what change categories regressed (§19)
* :class:`DeploymentPatternMiner`   — deployment-adjacent incidents (§20)
* :class:`DependencyPatternMiner`   — dependency-conditioned failures (§18)
* :class:`RecoveryPatternMiner`     — how long recovery tends to take (§17)
* :class:`ComponentReliabilityPatternMiner` — chronically unreliable components (§21, §22)
* :class:`PredictivePatternMiner`   — how accurate forecasts have been (§23)

Every association is labelled ``OBSERVED PATTERN`` in ``limitations``: the phase
is explicit that a recurring co-occurrence is not a causal claim, and the UI
renders that label verbatim.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deployment import DeploymentEvent
from app.models.fix import FixHypothesis, Patch, PatchVerificationRun
from app.models.intelligence import (
    ComponentReliabilityProfile,
    KnowledgeScope,
    KnowledgeType,
    ReliabilityExperience,
)
from app.models.reliability import (
    ForecastOutcome,
    ReliabilityForecast,
)
from app.services.learning_signatures import (
    FailureSignature,
    ResolutionSignature,
)

logger = logging.getLogger(__name__)

#: Algorithm identities recorded on every knowledge row (§26, §68).
ALGORITHM_VERSION = "1.0"

#: The label every mined association carries. Rendered in the UI verbatim.
OBSERVED_PATTERN_NOTE = (
    "OBSERVED PATTERN — an association in history, not a causal claim"
)

#: Outcomes that count as a successful resolution for effectiveness purposes.
_SUCCESS_OUTCOMES = frozenset({"effective", "partially_effective", "patch_verified"})

#: Minimum support for a pattern to be worth *emitting* at all. Validation
#: applies the stricter, configurable thresholds (§33); this floor only stops the
#: pipeline from storing single-observation noise.
MIN_EMIT_SAMPLES = 2


@dataclass
class CorpusEntry:
    """One experience, parsed for mining."""

    experience_id: str
    occurred_at: datetime
    end_time: datetime
    component_id: Optional[str]
    environment_id: Optional[str]
    failure: FailureSignature
    resolution: Optional[ResolutionSignature]
    outcome: str
    quality: str
    recovery_seconds: Optional[int] = None
    sources: list[dict] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        if self.resolution is not None and self.resolution.succeeded:
            return True
        return self.outcome in _SUCCESS_OUTCOMES


@dataclass
class ExperienceCorpus:
    """The bounded set of experiences a run mines over."""

    project_id: uuid.UUID
    cutoff: datetime
    entries: list[CorpusEntry] = field(default_factory=list)

    def usable(self) -> list[CorpusEntry]:
        """Entries pattern mining may use (§30).

        ``POOR`` rows are kept in the corpus — the report has to be able to say
        how many were excluded — but are never counted as evidence.
        """
        return [entry for entry in self.entries if entry.quality != "POOR"]

    def by_component(self) -> dict[Optional[str], list[CorpusEntry]]:
        grouped: dict[Optional[str], list[CorpusEntry]] = defaultdict(list)
        for entry in self.usable():
            grouped[entry.component_id].append(entry)
        return grouped

    def __len__(self) -> int:
        return len(self.entries)


async def load_corpus(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    cutoff: datetime,
    lookback_days: Optional[int] = None,
    limit: int = 5000,
) -> ExperienceCorpus:
    """Load the project's experiences up to ``cutoff`` (§28).

    ``cutoff`` is enforced in SQL on ``end_time``: an experience that ended after
    the boundary is future knowledge and must not be minable as of it — this is
    the temporal-leakage guard for the mining stage (§31).
    """
    stmt = (
        select(ReliabilityExperience)
        .where(ReliabilityExperience.project_id == project_id)
        .where(ReliabilityExperience.end_time <= cutoff)
        .order_by(ReliabilityExperience.end_time.desc())
        .limit(limit)
    )
    if lookback_days is not None:
        stmt = stmt.where(
            ReliabilityExperience.end_time >= cutoff - timedelta(days=lookback_days)
        )

    rows = list((await session.scalars(stmt)).all())
    entries: list[CorpusEntry] = []
    for row in rows:
        entries.append(
            CorpusEntry(
                experience_id=str(row.id),
                occurred_at=row.start_time,
                end_time=row.end_time,
                component_id=str(row.primary_component_id)
                if row.primary_component_id
                else None,
                environment_id=str(row.environment_id) if row.environment_id else None,
                failure=FailureSignature.from_dict(row.failure_signature),
                resolution=(
                    ResolutionSignature.from_dict(row.resolution_signature)
                    if row.resolution_signature
                    else None
                ),
                outcome=row.outcome,
                quality=row.data_quality,
                recovery_seconds=row.recovery_seconds,
                sources=[{"type": "experience", "id": str(row.id)}],
            )
        )
    return ExperienceCorpus(project_id=project_id, cutoff=cutoff, entries=entries)


@dataclass
class MinedPattern:
    """A candidate pattern, with the evidence that produced it (§32)."""

    knowledge_type: KnowledgeType
    scope: KnowledgeScope
    project_id: uuid.UUID
    title: str
    description: str
    feature_signature: str
    algorithm: str
    sample_count: int
    success_count: Optional[int] = None
    component_id: Optional[uuid.UUID] = None
    environment_id: Optional[uuid.UUID] = None
    coverage_start: Optional[datetime] = None
    coverage_end: Optional[datetime] = None
    experience_ids: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def support_strength(self) -> Optional[float]:
        """Fraction of the sample that supports the pattern, when it has one."""
        if self.success_count is None or self.sample_count <= 0:
            return None
        return self.success_count / self.sample_count


def _coverage(
    entries: Iterable[CorpusEntry],
) -> tuple[Optional[datetime], Optional[datetime]]:
    moments = [entry.occurred_at for entry in entries]
    if not moments:
        return None, None
    return min(moments), max(moments)


class PatternMiner:
    """Base class: a miner names itself and returns candidate patterns."""

    name = "base"
    knowledge_type = KnowledgeType.INCIDENT_PATTERN

    async def mine(
        self,
        session: AsyncSession,
        corpus: ExperienceCorpus,
        *,
        min_samples: int = MIN_EMIT_SAMPLES,
    ) -> list[MinedPattern]:  # pragma: no cover - abstract
        raise NotImplementedError


class FailurePatternMiner(PatternMiner):
    """Groups experiences by failure shape to find what recurs (§18).

    Grouping is per component *and* per signature label. Merging components would
    claim the same pattern applies everywhere, which is exactly the
    generalisation §37 forbids until the evidence supports it.
    """

    name = "failure_pattern_v1"
    knowledge_type = KnowledgeType.FAILURE_PATTERN

    async def mine(
        self,
        session: AsyncSession,
        corpus: ExperienceCorpus,
        *,
        min_samples: int = MIN_EMIT_SAMPLES,
    ) -> list[MinedPattern]:
        groups: dict[tuple[str, Optional[str]], list[CorpusEntry]] = defaultdict(list)
        for entry in corpus.usable():
            groups[(entry.failure.label(), entry.component_id)].append(entry)

        patterns: list[MinedPattern] = []
        for (label, component_id), entries in sorted(
            groups.items(), key=lambda item: (-len(item[1]), item[0][0])
        ):
            if len(entries) < min_samples:
                continue
            successes = sum(1 for entry in entries if entry.succeeded)
            start, end = _coverage(entries)
            patterns.append(
                MinedPattern(
                    knowledge_type=KnowledgeType.FAILURE_PATTERN,
                    scope=(
                        KnowledgeScope.COMPONENT_SPECIFIC
                        if component_id
                        else KnowledgeScope.PROJECT_LEVEL
                    ),
                    project_id=corpus.project_id,
                    title=f"Recurring failure pattern: {label}",
                    description=(
                        f"{len(entries)} completed episodes shared the failure signature "
                        f"'{label}'. {successes} of them were resolved; the rest are "
                        f"recorded as unresolved or unsuccessful."
                    ),
                    feature_signature=f"failure:{label}",
                    algorithm=self.name,
                    sample_count=len(entries),
                    success_count=successes,
                    component_id=_as_uuid(component_id),
                    coverage_start=start,
                    coverage_end=end,
                    experience_ids=[entry.experience_id for entry in entries],
                    sources=_sources(entries),
                    limitations=[
                        OBSERVED_PATTERN_NOTE,
                        "Similarity is based on normalized signatures, not on confirmed root causes.",
                    ],
                    details={"label": label, "outcomes": _outcome_counts(entries)},
                )
            )
        return patterns


class RemediationPatternMiner(PatternMiner):
    """Which action resolved which failure shape, and how often (§17)."""

    name = "remediation_pattern_v1"
    knowledge_type = KnowledgeType.REMEDIATION_PATTERN

    async def mine(
        self,
        session: AsyncSession,
        corpus: ExperienceCorpus,
        *,
        min_samples: int = MIN_EMIT_SAMPLES,
    ) -> list[MinedPattern]:
        groups: dict[tuple[str, str], list[CorpusEntry]] = defaultdict(list)
        for entry in corpus.usable():
            if entry.resolution is None or not entry.resolution.action_types:
                continue
            for action in entry.resolution.action_types:
                groups[(entry.failure.label(), action)].append(entry)

        patterns: list[MinedPattern] = []
        for (label, action), entries in sorted(
            groups.items(), key=lambda item: (-len(item[1]), item[0][1])
        ):
            if len(entries) < min_samples:
                continue
            successes = sum(1 for entry in entries if entry.succeeded)
            failures = len(entries) - successes
            start, end = _coverage(entries)
            patterns.append(
                MinedPattern(
                    knowledge_type=KnowledgeType.REMEDIATION_PATTERN,
                    scope=KnowledgeScope.PROJECT_LEVEL,
                    project_id=corpus.project_id,
                    title=f"{action} for '{label}': {successes} of {len(entries)}",
                    description=(
                        f"{action.upper()} has historically resolved {successes} of "
                        f"{len(entries)} comparable episodes with the failure signature "
                        f"'{label}'. {failures} did not resolve as a success."
                    ),
                    feature_signature=f"remediation:{action}:{label}",
                    algorithm=self.name,
                    sample_count=len(entries),
                    success_count=successes,
                    coverage_start=start,
                    coverage_end=end,
                    experience_ids=[entry.experience_id for entry in entries],
                    sources=_sources(entries),
                    limitations=[
                        OBSERVED_PATTERN_NOTE,
                        "Only episodes where this action was actually attempted are counted.",
                        "A high success rate is not a guarantee: it describes these cases only.",
                    ],
                    details={
                        "action_type": action,
                        "failure_label": label,
                        "outcomes": _outcome_counts(entries),
                    },
                )
            )
        return patterns


class RegressionPatternMiner(PatternMiner):
    """Which change categories regressed after verification (§19).

    Reads the Phase 7 verification rows directly rather than the experience
    corpus, because a regression is a property of a *patch*, and the patch's
    change category is not carried in the failure signature.
    """

    name = "regression_pattern_v1"
    knowledge_type = KnowledgeType.REGRESSION_PATTERN

    async def mine(
        self,
        session: AsyncSession,
        corpus: ExperienceCorpus,
        *,
        min_samples: int = MIN_EMIT_SAMPLES,
    ) -> list[MinedPattern]:
        stmt = (
            select(
                FixHypothesis.category,
                PatchVerificationRun.regression_detected,
                PatchVerificationRun.completed_at,
                PatchVerificationRun.id,
                PatchVerificationRun.project_id,
            )
            .join(Patch, Patch.fix_hypothesis_id == FixHypothesis.id)
            .join(PatchVerificationRun, PatchVerificationRun.patch_id == Patch.id)
            .where(FixHypothesis.project_id == corpus.project_id)
            .where(PatchVerificationRun.completed_at.isnot(None))
            .where(PatchVerificationRun.completed_at <= corpus.cutoff)
            .limit(1000)
        )
        rows = (await session.execute(stmt)).all()
        if not rows:
            return []

        groups: dict[str, list[Any]] = defaultdict(list)
        for row in rows:
            category = getattr(row.category, "value", str(row.category))
            groups[category].append(row)

        patterns: list[MinedPattern] = []
        for category, entries in sorted(groups.items(), key=lambda item: -len(item[1])):
            if len(entries) < min_samples:
                continue
            regressions = sum(1 for row in entries if row.regression_detected)
            if regressions == 0:
                #: A category with no regressions is not a *regression* pattern.
                continue
            moments = [row.completed_at for row in entries if row.completed_at]
            patterns.append(
                MinedPattern(
                    knowledge_type=KnowledgeType.REGRESSION_PATTERN,
                    scope=KnowledgeScope.PROJECT_LEVEL,
                    project_id=corpus.project_id,
                    title=f"Regression after {category} changes: {regressions} of {len(entries)}",
                    description=(
                        f"Verified patches in the {category} category regressed in "
                        f"{regressions} of {len(entries)} runs. This is a property of the "
                        f"change category in this project's history, not a rule about "
                        f"that category in general."
                    ),
                    feature_signature=f"regression:{category}",
                    algorithm=self.name,
                    sample_count=len(entries),
                    success_count=len(entries) - regressions,
                    coverage_start=min(moments) if moments else None,
                    coverage_end=max(moments) if moments else None,
                    sources=[
                        {"type": "patch_verification", "id": str(row.id)}
                        for row in entries[:20]
                    ],
                    limitations=[
                        OBSERVED_PATTERN_NOTE,
                        "Counts verification runs, so a single patch verified repeatedly is counted repeatedly.",
                    ],
                    details={"category": category, "regressions": regressions},
                )
            )
        return patterns


class DeploymentPatternMiner(PatternMiner):
    """Deployment-adjacent incidents, expressed as a rate rather than a hunch (§20)."""

    name = "deployment_pattern_v1"
    knowledge_type = KnowledgeType.DEPLOYMENT_PATTERN

    async def mine(
        self,
        session: AsyncSession,
        corpus: ExperienceCorpus,
        *,
        min_samples: int = MIN_EMIT_SAMPLES,
    ) -> list[MinedPattern]:
        groups: dict[str, list[CorpusEntry]] = defaultdict(list)
        for entry in corpus.usable():
            if (
                "recent_deployment" in entry.failure.deployment_context
                and entry.component_id
            ):
                groups[entry.component_id].append(entry)

        patterns: list[MinedPattern] = []
        for component_id, entries in groups.items():
            if len(entries) < min_samples:
                continue
            token = _as_uuid(component_id)
            deployment_count = 0
            if token is not None:
                #: The denominator matters: "3 incidents after a deploy" means
                #: something different on a component deployed 3 times than on
                #: one deployed 300 times, and the row records both.
                deployment_count = int(
                    await session.scalar(
                        select(func.count(DeploymentEvent.id))
                        .where(DeploymentEvent.project_id == corpus.project_id)
                        .where(DeploymentEvent.component_id == token)
                        .where(DeploymentEvent.deployed_at <= corpus.cutoff)
                    )
                    or 0
                )
            start, end = _coverage(entries)
            failed_context = sum(
                1
                for entry in entries
                if "failed_deployment" in entry.failure.deployment_context
            )
            patterns.append(
                MinedPattern(
                    knowledge_type=KnowledgeType.DEPLOYMENT_PATTERN,
                    scope=KnowledgeScope.COMPONENT_SPECIFIC,
                    project_id=corpus.project_id,
                    title=(
                        f"{len(entries)} incidents followed a recent deployment on this component"
                    ),
                    description=(
                        f"{len(entries)} completed episodes on this component began within "
                        f"{24} hours of a deployment; {failed_context} of those deployments "
                        f"had failed or been rolled back. The association is temporal — this "
                        f"does not establish that the deployment caused the incidents."
                    ),
                    feature_signature=f"deployment:recent:{component_id}",
                    algorithm=self.name,
                    sample_count=len(entries),
                    success_count=len(entries) - failed_context,
                    component_id=token,
                    coverage_start=start,
                    coverage_end=end,
                    experience_ids=[entry.experience_id for entry in entries],
                    sources=_sources(entries),
                    limitations=[
                        OBSERVED_PATTERN_NOTE,
                        "Deployments happen often; proximity alone is weak evidence.",
                    ],
                    details={
                        "failed_deployment_context": failed_context,
                        "deployments_observed": deployment_count,
                    },
                )
            )
        return patterns


class DependencyPatternMiner(PatternMiner):
    """Failures conditioned on dependency state (§18)."""

    name = "dependency_pattern_v1"
    knowledge_type = KnowledgeType.DEPENDENCY_PATTERN

    async def mine(
        self,
        session: AsyncSession,
        corpus: ExperienceCorpus,
        *,
        min_samples: int = MIN_EMIT_SAMPLES,
    ) -> list[MinedPattern]:
        groups: dict[tuple[str, str], list[CorpusEntry]] = defaultdict(list)
        for entry in corpus.usable():
            for condition in entry.failure.dependency_conditions:
                if condition in ("requires_dependencies",):
                    continue
                groups[(condition, entry.component_id or "project")].append(entry)

        patterns: list[MinedPattern] = []
        for (condition, component_key), entries in sorted(
            groups.items(), key=lambda item: -len(item[1])
        ):
            if len(entries) < min_samples:
                continue
            start, end = _coverage(entries)
            component_id = None if component_key == "project" else component_key
            patterns.append(
                MinedPattern(
                    knowledge_type=KnowledgeType.DEPENDENCY_PATTERN,
                    scope=(
                        KnowledgeScope.COMPONENT_SPECIFIC
                        if component_id
                        else KnowledgeScope.PROJECT_LEVEL
                    ),
                    project_id=corpus.project_id,
                    title=f"Dependency condition '{condition}' in {len(entries)} episodes",
                    description=(
                        f"{len(entries)} completed episodes were observed with the dependency "
                        f"condition '{condition}'. The condition is recorded as part of the "
                        f"failure context and is not by itself a cause."
                    ),
                    feature_signature=f"dependency:{condition}:{component_key}",
                    algorithm=self.name,
                    sample_count=len(entries),
                    success_count=sum(1 for entry in entries if entry.succeeded),
                    component_id=_as_uuid(component_id),
                    coverage_start=start,
                    coverage_end=end,
                    experience_ids=[entry.experience_id for entry in entries],
                    sources=_sources(entries),
                    limitations=[
                        OBSERVED_PATTERN_NOTE,
                        "Dependency state is inferred from anomalies and health checks of linked components.",
                    ],
                    details={"condition": condition},
                )
            )
        return patterns


class RecoveryPatternMiner(PatternMiner):
    """How long recovery takes for a given failure shape (§17)."""

    name = "recovery_pattern_v1"
    knowledge_type = KnowledgeType.RECOVERY_PATTERN

    #: Share of episodes that must agree on one bucket before the pattern is
    #: emitted. Below this the honest answer is "recovery times varied".
    DOMINANCE_THRESHOLD = 0.6

    async def mine(
        self,
        session: AsyncSession,
        corpus: ExperienceCorpus,
        *,
        min_samples: int = MIN_EMIT_SAMPLES,
    ) -> list[MinedPattern]:
        groups: dict[tuple[str, Optional[str]], list[CorpusEntry]] = defaultdict(list)
        for entry in corpus.usable():
            if entry.resolution is None:
                continue
            groups[(entry.failure.label(), entry.component_id)].append(entry)

        patterns: list[MinedPattern] = []
        for (label, component_id), entries in sorted(
            groups.items(), key=lambda item: -len(item[1])
        ):
            if len(entries) < min_samples:
                continue
            buckets: dict[str, int] = defaultdict(int)
            for entry in entries:
                bucket = (
                    entry.resolution.recovery_bucket if entry.resolution else "unknown"
                )
                buckets[bucket] += 1
            bucket, count = max(buckets.items(), key=lambda item: item[1])
            if bucket == "unknown":
                continue
            if count / len(entries) < self.DOMINANCE_THRESHOLD:
                continue
            start, end = _coverage(entries)
            patterns.append(
                MinedPattern(
                    knowledge_type=KnowledgeType.RECOVERY_PATTERN,
                    scope=(
                        KnowledgeScope.COMPONENT_SPECIFIC
                        if component_id
                        else KnowledgeScope.PROJECT_LEVEL
                    ),
                    project_id=corpus.project_id,
                    title=f"Recovery for '{label}' was typically {bucket.lower()}",
                    description=(
                        f"{count} of {len(entries)} comparable episodes recovered within the "
                        f"{bucket} window. Recovery time describes these episodes; it is not a "
                        f"promise about the next one."
                    ),
                    feature_signature=f"recovery:{label}:{bucket}",
                    algorithm=self.name,
                    sample_count=len(entries),
                    success_count=count,
                    component_id=_as_uuid(component_id),
                    coverage_start=start,
                    coverage_end=end,
                    experience_ids=[entry.experience_id for entry in entries],
                    sources=_sources(entries),
                    limitations=[
                        OBSERVED_PATTERN_NOTE,
                        "Recovery time measures the incident window, which includes detection and response delay.",
                    ],
                    details={"bucket": bucket, "distribution": dict(buckets)},
                )
            )
        return patterns


class ComponentReliabilityPatternMiner(PatternMiner):
    """Chronic-reliability signals from the component profiles (§21, §22)."""

    name = "component_reliability_v1"
    knowledge_type = KnowledgeType.COMPONENT_RELIABILITY_PATTERN

    async def mine(
        self,
        session: AsyncSession,
        corpus: ExperienceCorpus,
        *,
        min_samples: int = MIN_EMIT_SAMPLES,
    ) -> list[MinedPattern]:
        stmt = (
            select(ComponentReliabilityProfile)
            .where(ComponentReliabilityProfile.project_id == corpus.project_id)
            .where(ComponentReliabilityProfile.computed_at <= corpus.cutoff)
            .where(ComponentReliabilityProfile.chronic_signal.is_(True))
            .order_by(ComponentReliabilityProfile.incident_count.desc())
            .limit(100)
        )
        profiles = list((await session.scalars(stmt)).all())
        patterns: list[MinedPattern] = []
        for profile in profiles:
            if profile.incident_count < min_samples:
                continue
            reasons = list(profile.chronic_reasons or [])
            patterns.append(
                MinedPattern(
                    knowledge_type=KnowledgeType.COMPONENT_RELIABILITY_PATTERN,
                    scope=KnowledgeScope.COMPONENT_SPECIFIC,
                    project_id=corpus.project_id,
                    title=(
                        f"Component shows a chronic reliability signal over "
                        f"{profile.window_days}d"
                    ),
                    description=(
                        f"In the last {profile.window_days} days this component produced "
                        f"{profile.incident_count} incidents, {profile.anomaly_count} anomalies "
                        f"and {profile.remediation_count} remediations. Signal reasons: "
                        f"{', '.join(reasons) or 'not recorded'}. This recommends "
                        f"investigation; ARGUS does not change anything about the component."
                    ),
                    feature_signature=f"chronic:{profile.window_days}d",
                    algorithm=self.name,
                    sample_count=profile.incident_count,
                    component_id=profile.component_id,
                    coverage_start=(
                        profile.computed_at - timedelta(days=profile.window_days)
                    ),
                    coverage_end=profile.computed_at,
                    sources=[{"type": "component_profile", "id": str(profile.id)}],
                    limitations=[
                        OBSERVED_PATTERN_NOTE,
                        "Counts depend on what was ingested; a component with sparse telemetry can look quiet.",
                    ],
                    details={
                        "window_days": profile.window_days,
                        "anomaly_count": profile.anomaly_count,
                        "remediation_count": profile.remediation_count,
                        "regression_count": profile.regression_count,
                        "chronic_reasons": reasons,
                    },
                )
            )
        return patterns


class PredictivePatternMiner(PatternMiner):
    """How well forecasts have held up, by component and risk level (§23)."""

    name = "predictive_pattern_v1"
    knowledge_type = KnowledgeType.PREDICTIVE_PATTERN

    async def mine(
        self,
        session: AsyncSession,
        corpus: ExperienceCorpus,
        *,
        min_samples: int = MIN_EMIT_SAMPLES,
    ) -> list[MinedPattern]:
        stmt = (
            select(
                ForecastOutcome.component_id,
                ForecastOutcome.predicted_risk_level,
                ForecastOutcome.outcome,
                ForecastOutcome.evaluated_at,
                ForecastOutcome.id,
            )
            .join(
                ReliabilityForecast,
                ReliabilityForecast.id == ForecastOutcome.forecast_id,
            )
            .where(ReliabilityForecast.project_id == corpus.project_id)
            .where(ForecastOutcome.evaluated_at <= corpus.cutoff)
            .limit(2000)
        )
        rows = (await session.execute(stmt)).all()
        if not rows:
            return []

        groups: dict[tuple[Optional[uuid.UUID], str], list[Any]] = defaultdict(list)
        for row in rows:
            level = getattr(
                row.predicted_risk_level, "value", str(row.predicted_risk_level)
            )
            groups[(row.component_id, level)].append(row)

        patterns: list[MinedPattern] = []
        for (component_id, level), entries in sorted(
            groups.items(), key=lambda item: -len(item[1])
        ):
            if len(entries) < min_samples:
                continue
            #: The column is a plain string holding ``PredictionOutcomeType``
            #: values; only TRUE_POSITIVE counts as a confirmed forecast.
            confirmed = sum(
                1
                for row in entries
                if str(getattr(row.outcome, "value", row.outcome)) == "TRUE_POSITIVE"
            )
            if confirmed == 0:
                continue
            moments = [row.evaluated_at for row in entries if row.evaluated_at]
            patterns.append(
                MinedPattern(
                    knowledge_type=KnowledgeType.PREDICTIVE_PATTERN,
                    scope=(
                        KnowledgeScope.COMPONENT_SPECIFIC
                        if component_id
                        else KnowledgeScope.PROJECT_LEVEL
                    ),
                    project_id=corpus.project_id,
                    title=f"{level} forecasts confirmed {confirmed} of {len(entries)}",
                    description=(
                        f"Forecasts at risk level {level} were followed by the predicted event "
                        f"in {confirmed} of {len(entries)} evaluated horizons. A high number "
                        f"describes past calibration on this scope, not future certainty."
                    ),
                    feature_signature=f"predictive:{level}:{component_id or 'project'}",
                    algorithm=self.name,
                    sample_count=len(entries),
                    success_count=confirmed,
                    component_id=component_id,
                    coverage_start=min(moments) if moments else None,
                    coverage_end=max(moments) if moments else None,
                    sources=[
                        {"type": "forecast_outcome", "id": str(row.id)}
                        for row in entries[:20]
                    ],
                    limitations=[
                        OBSERVED_PATTERN_NOTE,
                        "Only forecasts whose horizon has elapsed are counted.",
                        "Calibration drifts; this pattern decays and is re-checked by the staleness sweep.",
                    ],
                    details={"risk_level": level, "confirmed": confirmed},
                )
            )
        return patterns


def _outcome_counts(entries: Sequence[CorpusEntry]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for entry in entries:
        counts[entry.outcome] += 1
    return dict(sorted(counts.items()))


def _sources(entries: Sequence[CorpusEntry]) -> list[dict]:
    """Provenance for a mined pattern: the experiences it came from (§5)."""
    seen: list[dict] = []
    for entry in entries[:20]:
        seen.append({"type": "experience", "id": entry.experience_id})
    for entry in entries:
        for source in entry.sources:
            if source.get("type") != "experience":
                seen.append(source)
                break
    return seen


def _as_uuid(value: Optional[str]) -> Optional[uuid.UUID]:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def default_miners() -> list[PatternMiner]:
    """The §17–§22 miner set, in the order a run executes them."""
    return [
        FailurePatternMiner(),
        RemediationPatternMiner(),
        RecoveryPatternMiner(),
        DependencyPatternMiner(),
        DeploymentPatternMiner(),
        RegressionPatternMiner(),
        ComponentReliabilityPatternMiner(),
        PredictivePatternMiner(),
    ]


__all__ = [
    "ALGORITHM_VERSION",
    "CorpusEntry",
    "ComponentReliabilityPatternMiner",
    "DependencyPatternMiner",
    "DeploymentPatternMiner",
    "ExperienceCorpus",
    "FailurePatternMiner",
    "MIN_EMIT_SAMPLES",
    "MinedPattern",
    "OBSERVED_PATTERN_NOTE",
    "PatternMiner",
    "PredictivePatternMiner",
    "RecoveryPatternMiner",
    "RegressionPatternMiner",
    "RemediationPatternMiner",
    "default_miners",
    "load_corpus",
]
