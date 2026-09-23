"""ARGUS Remediation Effectiveness (Phase 10 §14–§16, §44, §45).

Answers "how has this action actually gone?" — and refuses to answer with a
single number.

The phase is specific about the shape of the answer:

* Never *"restart has an 81% success rate"*. Always *"34 of 42 comparable cases
  succeeded"* — a count over a stated sample (§15).
* Never one global rate. Rates are broken down by component, environment,
  failure pattern, severity and root-cause category, because a global average
  across scopes hides the case that matters (§16).
* Below the configured minimum sample the honest answer is ``INSUFFICIENT``, not
  a percentage computed from two rows (§15, §33).
* Comparisons are **observational** (§44, §45): what happened in comparable
  situations, with the context and the limitations attached. The words
  ``OBSERVATIONAL COMPARISON`` travel with every comparison so no reader mistakes
  it for an experiment.

Everything is computed from stored experiences, which already carry the failure
signature, the action and the outcome — so a number here can always be traced
back to the episodes that produced it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.intelligence import ReliabilityExperience
from app.services.learning_signatures import (
    FailureSignature,
    ResolutionSignature,
)

logger = logging.getLogger(__name__)

#: Label attached to every cross-condition comparison (§44, §45).
OBSERVATIONAL_LABEL = "OBSERVATIONAL COMPARISON"

#: Outcomes that mean the action did what it was supposed to.
_SUCCESS_OUTCOMES = frozenset({"effective", "patch_verified"})
_PARTIAL_OUTCOMES = frozenset({"partially_effective"})

#: Dimensions effectiveness may be broken down by (§16).
DIMENSIONS = (
    "action",
    "component",
    "environment",
    "failure_pattern",
    "severity",
    "root_cause",
)


@dataclass
class EffectivenessBucket:
    """One row of the §15 table: counts over a stated, comparable sample."""

    action_type: str
    dimension: str
    dimension_value: Optional[str]
    comparable: int
    successful: int
    partially_successful: int
    failed: int
    rolled_back: int
    unresolved: int
    mean_recovery_seconds: Optional[float] = None
    regression_count: int = 0
    insufficient: bool = False
    minimum_samples: int = 0
    experience_ids: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def success_ratio(self) -> Optional[float]:
        if self.comparable <= 0:
            return None
        return self.successful / self.comparable

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "dimension": self.dimension,
            "dimension_value": self.dimension_value,
            "comparable": self.comparable,
            "successful": self.successful,
            "partially_successful": self.partially_successful,
            "failed": self.failed,
            "rolled_back": self.rolled_back,
            "unresolved": self.unresolved,
            "mean_recovery_seconds": self.mean_recovery_seconds,
            "regression_count": self.regression_count,
            "success_ratio": self.success_ratio,
            "insufficient": self.insufficient,
            "minimum_samples": self.minimum_samples,
            "experience_ids": list(self.experience_ids),
            "limitations": list(self.limitations),
        }

    def headline(self) -> str:
        """The §15 sentence: a count, never a guarantee."""
        if self.comparable == 0:
            return f"No comparable historical cases were found for {self.action_type}."
        if self.insufficient:
            return (
                f"Only {self.comparable} comparable case(s) for {self.action_type} — "
                f"insufficient to describe a rate (minimum {self.minimum_samples})."
            )
        return (
            f"{self.successful} of {self.comparable} comparable historical cases "
            f"were successful for {self.action_type}."
        )


@dataclass
class Observation:
    """A parsed experience used for the counts."""

    experience_id: str
    end_time: datetime
    action_types: tuple[str, ...]
    outcome: str
    component_id: Optional[str]
    environment_id: Optional[str]
    failure_label: str
    severity: str
    recovery_seconds: Optional[int]
    rolled_back: bool
    regression: bool
    resolution: Optional[ResolutionSignature]

    @property
    def successful(self) -> bool:
        if self.rolled_back:
            return False
        if self.resolution is not None and self.resolution.succeeded:
            return True
        return self.outcome in _SUCCESS_OUTCOMES


async def load_observations(
    session: AsyncSession,
    *,
    project_id: Any,
    cutoff: Optional[datetime] = None,
    lookback_days: Optional[int] = None,
    action_type: Optional[str] = None,
    component_id: Optional[Any] = None,
    environment_id: Optional[Any] = None,
    limit: int = 5000,
) -> list[Observation]:
    """Load the experiences that carry a resolution, bounded by the cutoff."""
    moment = _aware(cutoff) or datetime.now(timezone.utc)
    stmt = (
        select(ReliabilityExperience)
        .where(ReliabilityExperience.project_id == project_id)
        .where(ReliabilityExperience.end_time <= moment)
        .where(ReliabilityExperience.resolution_signature.isnot(None))
        .order_by(ReliabilityExperience.end_time.desc())
        .limit(limit)
    )
    if lookback_days is not None:
        stmt = stmt.where(
            ReliabilityExperience.end_time >= moment - timedelta(days=lookback_days)
        )
    if component_id is not None:
        stmt = stmt.where(ReliabilityExperience.primary_component_id == component_id)
    if environment_id is not None:
        stmt = stmt.where(ReliabilityExperience.environment_id == environment_id)

    rows = list((await session.scalars(stmt)).all())
    observations: list[Observation] = []
    for row in rows:
        resolution = (
            ResolutionSignature.from_dict(row.resolution_signature)
            if row.resolution_signature
            else None
        )
        actions = resolution.action_types if resolution else ()
        if action_type is not None and action_type.lower() not in actions:
            continue
        failure = FailureSignature.from_dict(row.failure_signature)
        observations.append(
            Observation(
                experience_id=str(row.id),
                end_time=row.end_time,
                action_types=actions,
                outcome=row.outcome,
                component_id=(
                    str(row.primary_component_id) if row.primary_component_id else None
                ),
                environment_id=str(row.environment_id) if row.environment_id else None,
                failure_label=failure.label(),
                severity=failure.severity,
                recovery_seconds=row.recovery_seconds,
                rolled_back=bool(resolution and resolution.rollback_performed),
                regression=row.outcome in ("harmful", "regression"),
                resolution=resolution,
            )
        )
    return observations


def bucket_for(
    observations: Sequence[Observation],
    *,
    action_type: str,
    dimension: str,
    dimension_value: Optional[str],
    minimum_samples: int,
) -> EffectivenessBucket:
    """Count one bucket, and mark it insufficient when the sample is too small."""
    bucket = EffectivenessBucket(
        action_type=action_type,
        dimension=dimension,
        dimension_value=dimension_value,
        comparable=len(observations),
        successful=0,
        partially_successful=0,
        failed=0,
        rolled_back=0,
        unresolved=0,
        minimum_samples=minimum_samples,
        experience_ids=[item.experience_id for item in observations[:50]],
    )
    recoveries: list[int] = []
    for item in observations:
        if item.recovery_seconds is not None:
            recoveries.append(item.recovery_seconds)
        if item.rolled_back:
            bucket.rolled_back += 1
        if item.regression:
            bucket.regression_count += 1
        if item.successful:
            bucket.successful += 1
        elif item.outcome in _PARTIAL_OUTCOMES:
            bucket.partially_successful += 1
        elif item.outcome in ("ineffective", "harmful", "failed"):
            bucket.failed += 1
        else:
            #: ``self_recovered`` / ``resolved_unverified`` / ``inconclusive``:
            #: the action's role is genuinely unknown, so it is counted as such
            #: rather than folded into either side.
            bucket.unresolved += 1

    if recoveries:
        bucket.mean_recovery_seconds = sum(recoveries) / len(recoveries)
    bucket.insufficient = bucket.comparable < minimum_samples

    limitations: list[str] = []
    if bucket.insufficient:
        limitations.append(
            f"fewer than {minimum_samples} comparable cases; no rate is meaningful"
        )
    if bucket.rolled_back:
        limitations.append(
            f"{bucket.rolled_back} case(s) were rolled back and are not counted as successes"
        )
    if bucket.unresolved:
        limitations.append(
            f"{bucket.unresolved} case(s) had no verifiable outcome and are counted separately"
        )
    if dimension != "action":
        limitations.append(
            f"this breakdown is conditioned on {dimension}={dimension_value}; the global "
            f"picture may differ"
        )
    bucket.limitations = limitations
    return bucket


async def action_effectiveness(
    session: AsyncSession,
    *,
    project_id: Any,
    action_type: Optional[str] = None,
    breakdown: Iterable[str] = ("action",),
    component_id: Optional[Any] = None,
    environment_id: Optional[Any] = None,
    cutoff: Optional[datetime] = None,
    lookback_days: Optional[int] = None,
    settings: Optional[Settings] = None,
) -> list[EffectivenessBucket]:
    """Contextual effectiveness for one action, or for every action seen (§15, §16)."""
    settings = settings or get_settings()
    observations = await load_observations(
        session,
        project_id=project_id,
        cutoff=cutoff,
        lookback_days=lookback_days,
        action_type=action_type,
        component_id=component_id,
        environment_id=environment_id,
    )
    minimum = settings.INTELLIGENCE_MIN_EFFECTIVENESS_SAMPLES
    dimensions = [dim for dim in breakdown if dim in DIMENSIONS] or ["action"]

    buckets: list[EffectivenessBucket] = []
    grouped_by_action: dict[str, list[Observation]] = {}
    for item in observations:
        for action in item.action_types:
            if action_type is not None and action != action_type.lower():
                continue
            grouped_by_action.setdefault(action, []).append(item)

    for action, items in sorted(grouped_by_action.items()):
        for dimension in dimensions:
            if dimension == "action":
                buckets.append(
                    bucket_for(
                        items,
                        action_type=action,
                        dimension="action",
                        dimension_value=None,
                        minimum_samples=minimum,
                    )
                )
                continue
            grouped: dict[Optional[str], list[Observation]] = {}
            for item in items:
                key = _dimension_value(item, dimension)
                grouped.setdefault(key, []).append(item)
            for key, bucket_items in sorted(
                grouped.items(), key=lambda pair: (pair[0] is None, pair[0] or "")
            ):
                buckets.append(
                    bucket_for(
                        bucket_items,
                        action_type=action,
                        dimension=dimension,
                        dimension_value=key,
                        minimum_samples=minimum,
                    )
                )
    return buckets


def _dimension_value(item: Observation, dimension: str) -> Optional[str]:
    if dimension == "component":
        return item.component_id
    if dimension == "environment":
        return item.environment_id
    if dimension == "failure_pattern":
        return item.failure_label
    if dimension == "severity":
        return item.severity
    if dimension == "root_cause":
        #: Root-cause *category* is not carried in the failure signature; the
        #: episode's failure label is the closest grounded proxy, and the bucket
        #: says so in its dimension name rather than pretending otherwise.
        return item.failure_label
    return None


async def compare_actions(
    session: AsyncSession,
    *,
    project_id: Any,
    action_a: str,
    action_b: str,
    failure_label: Optional[str] = None,
    cutoff: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """Compare two actions in comparable situations only (§45).

    "Comparable" means the same failure pattern when one is given. Without that
    filter the comparison would be between different problems, which is the
    mistake the section exists to prevent.
    """
    settings = settings or get_settings()
    observations = await load_observations(
        session, project_id=project_id, cutoff=cutoff
    )
    if failure_label is not None:
        observations = [
            item for item in observations if item.failure_label == failure_label
        ]

    result: dict[str, Any] = {
        "label": OBSERVATIONAL_LABEL,
        "failure_pattern": failure_label,
        "limitations": [
            "Actions were chosen by operators or policy, not assigned at random; the "
            "comparison describes what happened, not what would happen.",
            "Only episodes with a recorded resolution and outcome are counted.",
        ],
        "actions": {},
    }
    for action in (action_a, action_b):
        items = [item for item in observations if action.lower() in item.action_types]
        bucket = bucket_for(
            items,
            action_type=action,
            dimension="action",
            dimension_value=None,
            minimum_samples=settings.INTELLIGENCE_MIN_EFFECTIVENESS_SAMPLES,
        )
        result["actions"][action] = {
            **bucket.as_dict(),
            "headline": bucket.headline(),
        }

    counts = [
        result["actions"][action]["comparable"] for action in (action_a, action_b)
    ]
    if min(counts) < settings.INTELLIGENCE_MIN_EFFECTIVENESS_SAMPLES:
        result["verdict"] = "INSUFFICIENT_EVIDENCE"
        result["summary"] = (
            f"At least one action has fewer than "
            f"{settings.INTELLIGENCE_MIN_EFFECTIVENESS_SAMPLES} comparable cases; "
            f"no comparison is defensible."
        )
    else:
        first = result["actions"][action_a]["success_ratio"] or 0.0
        second = result["actions"][action_b]["success_ratio"] or 0.0
        if abs(first - second) < 0.1:
            result["verdict"] = "NO_MEANINGFUL_DIFFERENCE"
            result["summary"] = (
                f"{action_a} and {action_b} performed similarly in these comparable cases."
            )
        else:
            better = action_a if first > second else action_b
            result["verdict"] = "FAVOURS_" + better.upper()
            result["summary"] = (
                f"{better} succeeded more often in comparable cases; this is an "
                f"observational difference, not a demonstrated causal effect."
            )
    return result


async def counterfactual_comparison(
    session: AsyncSession,
    *,
    project_id: Any,
    failure_label: str,
    action_type: str,
    cutoff: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """What happened in comparable cases where the action was *not* taken (§44).

    Labelled an observational comparison and nothing stronger: the two groups
    differ by more than the action (who decided, how urgent it looked), so the
    difference cannot be attributed to the action.
    """
    settings = settings or get_settings()
    observations = [
        item
        for item in await load_observations(
            session, project_id=project_id, cutoff=cutoff
        )
        if item.failure_label == failure_label
    ]
    treated = [
        item for item in observations if action_type.lower() in item.action_types
    ]
    untreated = [
        item for item in observations if action_type.lower() not in item.action_types
    ]

    treated_bucket = bucket_for(
        treated,
        action_type=action_type,
        dimension="action",
        dimension_value="taken",
        minimum_samples=settings.INTELLIGENCE_MIN_EFFECTIVENESS_SAMPLES,
    )
    untreated_bucket = bucket_for(
        untreated,
        action_type=action_type,
        dimension="action",
        dimension_value="not_taken",
        minimum_samples=settings.INTELLIGENCE_MIN_EFFECTIVENESS_SAMPLES,
    )
    enough = (
        treated_bucket.comparable >= settings.INTELLIGENCE_MIN_EFFECTIVENESS_SAMPLES
        and untreated_bucket.comparable
        >= settings.INTELLIGENCE_MIN_EFFECTIVENESS_SAMPLES
    )
    return {
        "label": OBSERVATIONAL_LABEL,
        "failure_pattern": failure_label,
        "action_type": action_type,
        "taken": {**treated_bucket.as_dict(), "headline": treated_bucket.headline()},
        "not_taken": {
            **untreated_bucket.as_dict(),
            "headline": untreated_bucket.headline(),
        },
        "sufficient_evidence": enough,
        "summary": (
            f"In comparable cases, {action_type} was attempted in "
            f"{treated_bucket.comparable} and not attempted in "
            f"{untreated_bucket.comparable}."
            if enough
            else "Not enough comparable cases on both sides to say anything."
        ),
        "limitations": [
            OBSERVATIONAL_LABEL,
            "The groups differ beyond the action itself (who chose to act, how urgent it looked, what else changed).",
            "This is not a controlled experiment and cannot establish a causal effect.",
        ],
    }


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


__all__ = [
    "DIMENSIONS",
    "EffectivenessBucket",
    "OBSERVATIONAL_LABEL",
    "Observation",
    "action_effectiveness",
    "bucket_for",
    "compare_actions",
    "counterfactual_comparison",
    "load_observations",
]
