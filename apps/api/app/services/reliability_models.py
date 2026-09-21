"""ARGUS Predictive Model Registry (Phase 8 §23, §24, §25, §67).

Model identity and model *admission*. Two responsibilities, both about being
honest rather than clever:

**Every forecast names its model.** The registry registers a
:class:`~app.models.reliability.ReliabilityModelVersion` row for each
deterministic predictor on first use and hands back the id, so
``ReliabilityForecast.model_version_id`` and ``model_version_label`` always
resolve to a stored, inspectable version with its parameters (§82).

**An ML model must earn the right to run.** :func:`assess_ml_sufficiency`
checks the seven conditions §24 names — sample count, positives, negatives,
temporal coverage, feature completeness, class balance and leakage — and
returns a per-check verdict. When any check fails the caller falls back to the
deterministic baseline. There is deliberately no path in this module that
fabricates training data, and no code path anywhere in the phase that promotes
a model to ``ACTIVE``: models are activated by an operator, not by a request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.reliability import (
    PredictionType,
    ReliabilityModelStatus,
    ReliabilityModelType,
    ReliabilityModelVersion,
)
from app.services.reliability_predictors import (
    DETERMINISTIC_PREDICTORS,
    ReliabilityPredictor,
)

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass(frozen=True)
class SufficiencyCheck:
    """One gate condition, with the numbers that decided it (§24)."""

    name: str
    passed: bool
    detail: str
    observed: Optional[float] = None
    required: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "observed": self.observed,
            "required": self.required,
        }


@dataclass(frozen=True)
class MLSufficiency:
    """The verdict on whether an ML model may be used at all (§24).

    ``sufficient`` is only true when *every* check passes. The failing checks
    are carried so the fallback is explainable rather than a silent downgrade.
    """

    sufficient: bool
    checks: list[SufficiencyCheck] = field(default_factory=list)

    @property
    def failures(self) -> list[SufficiencyCheck]:
        return [check for check in self.checks if not check.passed]

    @property
    def reason(self) -> str:
        if self.sufficient:
            return "all data-sufficiency checks passed"
        return "insufficient training data: " + "; ".join(
            f"{check.name} ({check.detail})" for check in self.failures
        )

    def as_dict(self) -> dict:
        return {
            "sufficient": self.sufficient,
            "reason": self.reason,
            "checks": [check.as_dict() for check in self.checks],
        }


def assess_ml_sufficiency(
    *,
    sample_count: int,
    positive_count: int,
    negative_count: int,
    coverage: Optional[float],
    earliest_sample: Optional[datetime],
    latest_sample: Optional[datetime],
    feature_schema_version: str,
    expected_schema_version: str,
    label_end: Optional[datetime] = None,
    training_cutoff: Optional[datetime] = None,
) -> MLSufficiency:
    """Run the §24 gate over a candidate training set.

    Deterministic and side-effect free: given the same counts it returns the
    same verdict, so the decision to fall back is reproducible and testable
    (§67).

    The leakage check is explicit and load-bearing: a training row whose label
    ends after the training cutoff would let future information into the model,
    which §30 forbids. Failing that check blocks ML entirely — it is not a
    warning.
    """
    checks: list[SufficiencyCheck] = []

    checks.append(
        SufficiencyCheck(
            name="sample_count",
            passed=sample_count >= settings.RELIABILITY_ML_MIN_SAMPLES,
            detail=(
                f"{sample_count} labelled samples; "
                f"minimum {settings.RELIABILITY_ML_MIN_SAMPLES}"
            ),
            observed=float(sample_count),
            required=float(settings.RELIABILITY_ML_MIN_SAMPLES),
        )
    )
    checks.append(
        SufficiencyCheck(
            name="positive_outcomes",
            passed=positive_count >= settings.RELIABILITY_ML_MIN_POSITIVES,
            detail=(
                f"{positive_count} positive outcomes; minimum "
                f"{settings.RELIABILITY_ML_MIN_POSITIVES}"
            ),
            observed=float(positive_count),
            required=float(settings.RELIABILITY_ML_MIN_POSITIVES),
        )
    )
    checks.append(
        SufficiencyCheck(
            name="negative_outcomes",
            passed=negative_count >= settings.RELIABILITY_ML_MIN_NEGATIVES,
            detail=(
                f"{negative_count} negative outcomes; minimum "
                f"{settings.RELIABILITY_ML_MIN_NEGATIVES}"
            ),
            observed=float(negative_count),
            required=float(settings.RELIABILITY_ML_MIN_NEGATIVES),
        )
    )

    #: Temporal coverage: the data has to span a usable stretch of time, not
    #: just contain enough rows in one afternoon.
    span_seconds: Optional[float] = None
    if earliest_sample is not None and latest_sample is not None:
        span_seconds = (latest_sample - earliest_sample).total_seconds()
    required_span = settings.RELIABILITY_BACKTEST_MIN_TRAINING_SECONDS * 24
    checks.append(
        SufficiencyCheck(
            name="temporal_coverage",
            passed=span_seconds is not None and span_seconds >= required_span,
            detail=(
                "training data spans "
                + (
                    f"{span_seconds / 86400:.1f} days"
                    if span_seconds is not None
                    else "an unknown period"
                )
                + f"; minimum {required_span / 86400:.1f} days"
            ),
            observed=span_seconds,
            required=float(required_span),
        )
    )

    checks.append(
        SufficiencyCheck(
            name="feature_completeness",
            passed=coverage is not None
            and coverage >= settings.RELIABILITY_ML_MIN_COVERAGE,
            detail=(
                f"feature coverage {coverage:.0%}"
                if coverage is not None
                else "feature coverage unknown"
            )
            + f"; minimum {settings.RELIABILITY_ML_MIN_COVERAGE:.0%}",
            observed=coverage,
            required=settings.RELIABILITY_ML_MIN_COVERAGE,
        )
    )

    #: Class balance: refuse when one class is so rare that any metric would be
    #: noise. Expressed as the minority share of labelled rows.
    labelled = positive_count + negative_count
    minority = min(positive_count, negative_count) / labelled if labelled else 0.0
    checks.append(
        SufficiencyCheck(
            name="class_balance",
            passed=labelled > 0 and minority >= 0.10,
            detail=(
                f"minority class is {minority:.1%} of labelled outcomes; " "minimum 10%"
            ),
            observed=minority,
            required=0.10,
        )
    )

    schema_ok = feature_schema_version == expected_schema_version
    checks.append(
        SufficiencyCheck(
            name="feature_schema",
            passed=schema_ok,
            detail=(
                f"dataset schema {feature_schema_version!r}"
                + (
                    " matches the model's expected schema"
                    if schema_ok
                    else f" does not match {expected_schema_version!r}"
                )
            ),
        )
    )

    if label_end is not None and training_cutoff is not None:
        no_leak = label_end <= training_cutoff
        detail = (
            "labels end at or before the training cutoff"
            if no_leak
            else (
                "labels extend past the training cutoff, which would leak "
                "future information into training"
            )
        )
    else:
        no_leak = False
        detail = "the label window could not be bounded, so leakage cannot be ruled out"
    checks.append(SufficiencyCheck(name="no_leakage", passed=no_leak, detail=detail))

    return MLSufficiency(
        sufficient=all(check.passed for check in checks), checks=checks
    )


class ReliabilityModelRegistry:
    """Registers and resolves predictive model versions (§25)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure_version(
        self, predictor: ReliabilityPredictor
    ) -> ReliabilityModelVersion:
        """Get or create the stored version for a deterministic predictor.

        Parameters are refreshed on an existing row only when the predictor's
        declared parameters actually changed, so re-registering after a config
        change records the new parameters without churning rows or losing the
        calibration metrics an evaluation may have attached.
        """
        existing = (
            (
                await self.session.execute(
                    select(ReliabilityModelVersion).where(
                        ReliabilityModelVersion.model_name == predictor.name,
                        ReliabilityModelVersion.version == predictor.version,
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            if existing.parameters != predictor.parameters:
                existing.parameters = dict(predictor.parameters)
            return existing

        row = ReliabilityModelVersion(
            model_name=predictor.name,
            model_type=predictor.model_type,
            version=predictor.version,
            algorithm=predictor.__class__.__name__,
            training_window_seconds=settings.RELIABILITY_FEATURE_WINDOW_SECONDS,
            parameters=dict(predictor.parameters),
            status=ReliabilityModelStatus.ACTIVE,
            description=(predictor.__doc__ or "").strip().split("\n")[0] or None,
            metadata_={"deterministic": True},
        )
        self.session.add(row)
        await self.session.flush()
        logger.info(
            "registered reliability model version %s/%s",
            predictor.name,
            predictor.version,
        )
        return row

    async def get_for_type(
        self, prediction_type: PredictionType
    ) -> list[ReliabilityModelVersion]:
        """Every registered version that can score a prediction type."""
        applicable = [
            predictor
            for predictor in DETERMINISTIC_PREDICTORS
            if predictor.supports(prediction_type)
        ]
        versions: list[ReliabilityModelVersion] = []
        for predictor in applicable:
            versions.append(await self.ensure_version(predictor))
        return versions

    async def resolve(self, prediction_type: PredictionType) -> ReliabilityPredictor:
        """The deterministic predictor that will score this prediction type.

        Selection is by the declared order of :data:`DETERMINISTIC_PREDICTORS`,
        so it is stable across runs and processes. An ML provider would be
        consulted here first and fall back to this same deterministic pick when
        :func:`assess_ml_sufficiency` refuses it (§24, §67).
        """
        for predictor in DETERMINISTIC_PREDICTORS:
            if predictor.supports(prediction_type):
                return predictor
        raise ValueError(
            f"no deterministic predictor can score {prediction_type.value}"
        )

    async def ml_available(
        self, prediction_type: PredictionType, model_type: ReliabilityModelType
    ) -> tuple[bool, str]:
        """Whether a stored ML model of this family is *active* and can run.

        Phase 8 registers the families so the interface is real, but nothing
        activates one: this returns ``False`` with a stated reason until an
        operator-validated version exists. The caller's fallback is therefore
        the normal path, not an error path.
        """
        row = (
            (
                await self.session.execute(
                    select(ReliabilityModelVersion).where(
                        ReliabilityModelVersion.model_type == model_type,
                        ReliabilityModelVersion.status == ReliabilityModelStatus.ACTIVE,
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return (
                False,
                f"no active {model_type.value} model is registered for "
                f"{prediction_type.value}; the deterministic baseline is used",
            )
        return True, "an active model version is available"


def deterministic_predictor_names() -> Sequence[str]:
    """Names of the shipped deterministic predictors, for docs and the API."""
    return tuple(predictor.name for predictor in DETERMINISTIC_PREDICTORS)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def window_end(start: datetime, hours: int) -> datetime:
    """Small date helper used by backtests to step a window forward."""
    return start + timedelta(hours=hours)


__all__ = [
    "MLSufficiency",
    "ReliabilityModelRegistry",
    "SufficiencyCheck",
    "assess_ml_sufficiency",
    "deterministic_predictor_names",
    "utcnow",
    "window_end",
]
