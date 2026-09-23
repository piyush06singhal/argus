"""ARGUS Service Level Objectives & Error Budgets (Phase 11 §32–§36).

Objectives are optional (§32), so this module does nothing at all for a
deployment that has none — no invented targets, no default "99.9%".

How an objective is evaluated, stated once so the API, the UI and the tests
cannot disagree:

1. The objective names a **metric** (``metric_name``) and an indicator. The
   indicator decides how the metric is turned into a *compliance reading*:

   * ``AVAILABILITY`` and ``ERROR_RATE`` — the metric is expected to be a ratio
     of good or bad events. Availability is summarized as the mean of the
     metric (a ratio averaged over time), error rate likewise.
   * ``LATENCY`` and ``SATURATION`` — the metric is a magnitude; the reading is
     the **worst** sample in the window (p100 of what was stored), because an
     objective about latency is about the tail, and averaging hides it.
   * ``CUSTOM`` — the metric is a magnitude, summarized like latency, and the
     objective's own ``unit`` and description carry the meaning.
2. Compliance compares the reading to ``target`` using ``comparison``
   (``AT_LEAST`` / ``AT_MOST``) — never assumed.
3. With no samples in the window, the status is ``UNKNOWN``. Not ``MEETING``:
   an unmeasured objective is not a met one.

**Error budget (§34)**: the fraction of the window a service may be outside the
objective, expressed in the indicator's own unit as ``allowed_failure``; the
observed amount of the *bad* direction is ``observed_failure``; the remainder is
the budget. **Burn rate** is observed-over-allowed scaled to the window, so a
rate of 1.0 consumes the whole window's budget exactly. The thresholds that turn
a number into a name (``ELEVATED`` / ``FAST_BURN`` / ``CRITICAL_BURN``) are
configuration (§35), because they are policy.

**§36**: a burn is a *signal*. :func:`evaluate_and_record` publishes it and can
open a case, and nothing in this module claims a cause.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.services.platform_time import aware as _aware
from app.models.observability import MetricRecord
from app.models.platform import (
    BurnRateState,
    ErrorBudgetSnapshot,
    PlatformEventType,
    ServiceLevelObjective,
    SloComparison,
    SloIndicator,
    SloStatus,
)

logger = logging.getLogger(__name__)

#: Indicators whose metric is a magnitude (summarized by its worst sample)
#: rather than a ratio (summarized by its mean).
MAGNITUDE_INDICATORS = (
    SloIndicator.LATENCY,
    SloIndicator.SATURATION,
    SloIndicator.CUSTOM,
)


@dataclass
class SloEvaluation:
    """One objective's reading for one window."""

    slo_id: uuid.UUID
    name: str
    indicator: str
    window_start: datetime
    window_end: datetime
    status: SloStatus = SloStatus.UNKNOWN
    reading: Optional[float] = None
    target: float = 0.0
    comparison: str = SloComparison.AT_LEAST.value
    sample_count: int = 0
    allowed_failure: Optional[float] = None
    observed_failure: Optional[float] = None
    remaining: Optional[float] = None
    remaining_percent: Optional[float] = None
    burn_rate: Optional[float] = None
    burn_state: BurnRateState = BurnRateState.UNKNOWN
    compliance_percent: Optional[float] = None
    data_quality: str = "OK"
    evidence: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "slo_id": str(self.slo_id),
            "name": self.name,
            "indicator": self.indicator,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "status": self.status.value,
            "reading": self.reading,
            "target": self.target,
            "comparison": self.comparison,
            "sample_count": self.sample_count,
            "allowed_failure": self.allowed_failure,
            "observed_failure": self.observed_failure,
            "remaining": self.remaining,
            "remaining_percent": self.remaining_percent,
            "burn_rate": self.burn_rate,
            "burn_state": self.burn_state.value,
            "compliance_percent": self.compliance_percent,
            "data_quality": self.data_quality,
            "evidence": self.evidence,
            "limitations": list(self.limitations),
        }


def classify_burn(
    burn_rate: Optional[float], *, settings: Optional[Settings] = None
) -> BurnRateState:
    """Turn a burn rate into a name using the configured thresholds (§35)."""
    settings = settings or get_settings()
    if burn_rate is None:
        return BurnRateState.UNKNOWN
    if burn_rate >= settings.PLATFORM_BURN_CRITICAL:
        return BurnRateState.CRITICAL_BURN
    if burn_rate >= settings.PLATFORM_BURN_FAST:
        return BurnRateState.FAST_BURN
    if burn_rate >= settings.PLATFORM_BURN_ELEVATED:
        return BurnRateState.ELEVATED
    return BurnRateState.NORMAL


def _reading_for(indicator: SloIndicator, values: Sequence[float]) -> Optional[float]:
    """Summarize samples according to the indicator's meaning."""
    if not values:
        return None
    if indicator in MAGNITUDE_INDICATORS:
        return max(values)
    return sum(values) / len(values)


def _bad_direction(indicator: SloIndicator, comparison: SloComparison) -> str:
    """Which way a sample has to move to consume the budget."""
    if comparison == SloComparison.AT_LEAST:
        return "BELOW"
    return "ABOVE"


def _failure_fraction(
    indicator: SloIndicator,
    comparison: SloComparison,
    target: float,
    values: Sequence[float],
) -> Optional[float]:
    """The observed failure as a 0..1 fraction of the window.

    For a ratio indicator this is the share of samples on the wrong side of the
    target; for a magnitude indicator it is the share of samples beyond it. Both
    are counts of samples, which is the only thing the stored metrics can
    honestly support — the alternative (time-weighted extrapolation) would invent
    coverage the data does not have.
    """
    if not values:
        return None
    if comparison == SloComparison.AT_LEAST:
        bad = sum(1 for value in values if value < target)
    else:
        bad = sum(1 for value in values if value > target)
    return bad / len(values)


async def evaluate_slo(
    session: AsyncSession,
    *,
    slo: ServiceLevelObjective,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> SloEvaluation:
    """Evaluate one objective over its own window (§33–§35)."""
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    window_seconds = min(slo.window_seconds, settings.PLATFORM_SLO_MAX_WINDOW_SECONDS)
    window_start = moment - timedelta(seconds=window_seconds)
    evaluation = SloEvaluation(
        slo_id=slo.id,
        name=slo.name,
        indicator=slo.indicator.value,
        window_start=window_start,
        window_end=moment,
        target=slo.target,
        comparison=slo.comparison.value,
    )

    if not slo.metric_name:
        evaluation.data_quality = "NO_METRIC"
        evaluation.limitations.append(
            "the objective names no metric, so it cannot be measured"
        )
        return evaluation

    stmt = select(MetricRecord.value).where(
        MetricRecord.project_id == slo.project_id,
        MetricRecord.metric_name == slo.metric_name,
        MetricRecord.timestamp >= window_start,
        MetricRecord.timestamp <= moment,
    )
    if slo.component_id is not None:
        stmt = stmt.where(MetricRecord.component_id == slo.component_id)
    if slo.environment_id is not None:
        stmt = stmt.where(MetricRecord.environment_id == slo.environment_id)
    values = [float(value) for value in (await session.scalars(stmt)).all()]
    evaluation.sample_count = len(values)
    evaluation.evidence = {
        "metric_name": slo.metric_name,
        "component_id": str(slo.component_id) if slo.component_id else None,
        "environment_id": str(slo.environment_id) if slo.environment_id else None,
        "window_seconds": window_seconds,
    }

    if not values:
        evaluation.data_quality = "NO_DATA"
        evaluation.limitations.append(
            "no samples for this metric in the window, so the objective's status "
            "is UNKNOWN rather than MEETING"
        )
        return evaluation

    evaluation.reading = round(_reading_for(slo.indicator, values) or 0.0, 6)

    # -- status
    if slo.comparison == SloComparison.AT_LEAST:
        evaluation.status = (
            SloStatus.MEETING
            if evaluation.reading >= slo.target
            else SloStatus.BREACHED
        )
    else:
        evaluation.status = (
            SloStatus.MEETING
            if evaluation.reading <= slo.target
            else SloStatus.BREACHED
        )

    # -- error budget
    failure_fraction = _failure_fraction(
        slo.indicator, slo.comparison, slo.target, values
    )
    if failure_fraction is not None:
        allowed_fraction = _allowed_failure_fraction(slo)
        allowed_failure = allowed_fraction
        observed_failure = failure_fraction
        evaluation.allowed_failure = round(allowed_failure, 6)
        evaluation.observed_failure = round(observed_failure, 6)
        remaining = allowed_failure - observed_failure
        evaluation.remaining = round(remaining, 6)
        evaluation.remaining_percent = (
            round((remaining / allowed_failure) * 100.0, 2) if allowed_failure else None
        )
        if allowed_fraction > 0:
            evaluation.burn_rate = round(observed_failure / allowed_fraction, 4)
        evaluation.burn_state = classify_burn(evaluation.burn_rate, settings=settings)
        evaluation.compliance_percent = round((1.0 - observed_failure) * 100.0, 4)

    if evaluation.status == SloStatus.MEETING and (
        evaluation.burn_state in (BurnRateState.FAST_BURN, BurnRateState.CRITICAL_BURN)
    ):
        #: Meeting on the reading but burning the budget: reported AT_RISK,
        #: because the objective is not breached *yet* and saying MEETING would
        #: hide the trajectory.
        evaluation.status = SloStatus.AT_RISK
        evaluation.limitations.append(
            "the current reading meets the objective but the error budget is "
            "burning fast, so the status is AT_RISK"
        )
    if slo.indicator not in (SloIndicator.AVAILABILITY, SloIndicator.ERROR_RATE):
        evaluation.limitations.append(
            "the reading is the worst stored sample in the window; ARGUS does not "
            "extrapolate percentiles it did not store"
        )
    return evaluation


def _allowed_failure_fraction(slo: ServiceLevelObjective) -> float:
    """How much of a window may be outside the objective, as a fraction.

    Derived from the target rather than invented: a 99.9% availability target
    allows 0.001 of the window; an AT_MOST error-rate target of 0.01 allows
    0.01. For a magnitude objective with no natural fraction, the tolerance is
    expressed against the target itself (a 5% overrun), and that choice is
    documented in the evaluation's limitations.
    """
    if slo.indicator in (SloIndicator.AVAILABILITY, SloIndicator.ERROR_RATE):
        if slo.comparison == SloComparison.AT_LEAST:
            return max(0.0, min(1.0, 1.0 - slo.target))
        return max(0.0, min(1.0, slo.target))
    return 0.05


async def record_evaluation(
    session: AsyncSession,
    *,
    evaluation: SloEvaluation,
    slo: ServiceLevelObjective,
    computed_at: Optional[datetime] = None,
) -> ErrorBudgetSnapshot:
    """Store one reading (§34)."""
    moment = _aware(computed_at) or evaluation.window_end
    snapshot = ErrorBudgetSnapshot(
        project_id=slo.project_id,
        slo_id=slo.id,
        component_id=slo.component_id,
        window_start=evaluation.window_start,
        window_end=evaluation.window_end,
        status=evaluation.status,
        allowed_failure=evaluation.allowed_failure,
        observed_failure=evaluation.observed_failure,
        remaining=evaluation.remaining,
        remaining_percent=evaluation.remaining_percent,
        burn_rate=evaluation.burn_rate,
        burn_state=evaluation.burn_state,
        compliance_percent=evaluation.compliance_percent,
        sample_count=evaluation.sample_count,
        data_quality=evaluation.data_quality,
        evidence=evaluation.evidence,
        limitations={"notes": evaluation.limitations},
        computed_at=moment,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


async def evaluate_and_record(
    session: AsyncSession,
    *,
    slo: ServiceLevelObjective,
    previous_status: Optional[SloStatus] = None,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
    open_case_on_burn: bool = True,
) -> tuple[SloEvaluation, ErrorBudgetSnapshot]:
    """Evaluate, store, and publish the §36 signal when a burn matters.

    A status change and an error-budget burn are both platform events; a burn at
    ``ELEVATED`` or worse can open a case, and the case text says explicitly that
    the signal is not a cause.
    """
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    evaluation = await evaluate_slo(session, slo=slo, now=moment, settings=settings)
    snapshot = await record_evaluation(session, evaluation=evaluation, slo=slo)

    from app.services.platform_events import safely_publish_event

    if previous_status is not None and previous_status != evaluation.status:
        await safely_publish_event(
            session,
            project_id=slo.project_id,
            environment_id=slo.environment_id,
            event_type=PlatformEventType.SLO_STATUS_CHANGED,
            source="slo_service",
            subject_type="slo",
            subject_id=slo.id,
            component_id=slo.component_id,
            occurred_at=moment,
            payload={
                "name": slo.name,
                "previous_status": previous_status.value,
                "status": evaluation.status.value,
                "reading": evaluation.reading,
                "target": slo.target,
            },
        )

    if evaluation.burn_state in (
        BurnRateState.ELEVATED,
        BurnRateState.FAST_BURN,
        BurnRateState.CRITICAL_BURN,
    ):
        await safely_publish_event(
            session,
            project_id=slo.project_id,
            environment_id=slo.environment_id,
            event_type=PlatformEventType.ERROR_BUDGET_BURN,
            source="slo_service",
            subject_type="slo",
            subject_id=slo.id,
            component_id=slo.component_id,
            occurred_at=moment,
            payload={
                "name": slo.name,
                "burn_state": evaluation.burn_state.value,
                "burn_rate": evaluation.burn_rate,
                "remaining_percent": evaluation.remaining_percent,
            },
        )
        if (
            open_case_on_burn
            and slo.alert_on_burn
            and evaluation.burn_state
            in (
                BurnRateState.FAST_BURN,
                BurnRateState.CRITICAL_BURN,
            )
        ):
            from app.services.control_plane import open_case_for_slo_burn

            await open_case_for_slo_burn(
                session,
                project_id=slo.project_id,
                slo=slo,
                snapshot=snapshot,
                settings=settings,
                now=moment,
            )
    return evaluation, snapshot


async def evaluate_project(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """Evaluate every enabled objective for a project (§33)."""
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    slos = list(
        (
            await session.scalars(
                select(ServiceLevelObjective)
                .where(
                    ServiceLevelObjective.project_id == project_id,
                    ServiceLevelObjective.enabled.is_(True),
                )
                .limit(settings.PLATFORM_SLO_SNAPSHOT_LIMIT)
            )
        ).all()
    )
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for slo in slos:
        previous = await latest_snapshot(session, slo_id=slo.id)
        try:
            evaluation, snapshot = await evaluate_and_record(
                session,
                slo=slo,
                previous_status=previous.status if previous else None,
                now=moment,
                settings=settings,
            )
        except Exception as exc:
            logger.warning("SLO evaluation failed for %s", slo.id, exc_info=True)
            errors.append(f"{slo.name}: {exc}")
            continue
        results.append(evaluation.as_dict() | {"snapshot_id": str(snapshot.id)})
    return {
        "objectives": len(slos),
        "evaluated": len(results),
        "breached": sum(1 for r in results if r["status"] == SloStatus.BREACHED.value),
        "at_risk": sum(1 for r in results if r["status"] == SloStatus.AT_RISK.value),
        "unknown": sum(1 for r in results if r["status"] == SloStatus.UNKNOWN.value),
        "burning": sum(
            1
            for r in results
            if r["burn_state"]
            in (
                BurnRateState.FAST_BURN.value,
                BurnRateState.CRITICAL_BURN.value,
            )
        ),
        "results": results,
        "errors": errors,
    }


async def latest_snapshot(
    session: AsyncSession, *, slo_id: uuid.UUID
) -> Optional[ErrorBudgetSnapshot]:
    stmt = (
        select(ErrorBudgetSnapshot)
        .where(ErrorBudgetSnapshot.slo_id == slo_id)
        .order_by(ErrorBudgetSnapshot.computed_at.desc())
        .limit(1)
    )
    return (await session.scalars(stmt)).first()


async def budget_history(
    session: AsyncSession, *, slo_id: uuid.UUID, limit: int = 100
) -> list[ErrorBudgetSnapshot]:
    stmt = (
        select(ErrorBudgetSnapshot)
        .where(ErrorBudgetSnapshot.slo_id == slo_id)
        .order_by(ErrorBudgetSnapshot.computed_at.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def slo_overview(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    settings: Optional[Settings] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The dashboard's SLO strip: latest reading per objective, plus totals."""
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    slos = list(
        (
            await session.scalars(
                select(ServiceLevelObjective)
                .where(ServiceLevelObjective.project_id == project_id)
                .order_by(ServiceLevelObjective.name)
                .limit(settings.PLATFORM_SLO_SNAPSHOT_LIMIT)
            )
        ).all()
    )
    objectives: list[dict[str, Any]] = []
    counts = {
        SloStatus.MEETING.value: 0,
        SloStatus.AT_RISK.value: 0,
        SloStatus.BREACHED.value: 0,
        SloStatus.UNKNOWN.value: 0,
    }
    for slo in slos:
        snapshot = await latest_snapshot(session, slo_id=slo.id)
        status = snapshot.status.value if snapshot else SloStatus.UNKNOWN.value
        counts[status] = counts.get(status, 0) + 1
        objectives.append(
            {
                "slo_id": str(slo.id),
                "name": slo.name,
                "indicator": slo.indicator.value,
                "target": slo.target,
                "comparison": slo.comparison.value,
                "unit": slo.unit,
                "component_id": str(slo.component_id) if slo.component_id else None,
                "enabled": slo.enabled,
                "status": status,
                "reading": snapshot.compliance_percent if snapshot else None,
                "burn_rate": snapshot.burn_rate if snapshot else None,
                "burn_state": snapshot.burn_state.value if snapshot else None,
                "remaining_percent": snapshot.remaining_percent if snapshot else None,
                "computed_at": _aware(snapshot.computed_at).isoformat()
                if snapshot
                else None,
                "never_evaluated": snapshot is None,
            }
        )
    limitations: list[str] = []
    if not slos:
        limitations.append(
            "this project has no service level objectives; reliability is reported "
            "from incidents and telemetry instead"
        )
    if counts[SloStatus.UNKNOWN.value]:
        limitations.append(
            f"{counts[SloStatus.UNKNOWN.value]} objective(s) have no reading yet — "
            "UNKNOWN means unmeasured, not met"
        )
    return {
        "as_of": moment.isoformat(),
        "objectives_total": len(slos),
        "by_status": counts,
        "objectives": objectives,
        "limitations": limitations,
    }


async def component_slo_status(
    session: AsyncSession,
    *,
    component_id: uuid.UUID,
    settings: Optional[Settings] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """SLO compliance for one component, for the scorecard (§75).

    Reads the stored snapshots rather than recomputing: the scorecard reports
    what ARGUS last measured, and ``now`` is accepted only for symmetry with the
    other evaluators.
    """
    settings = settings or get_settings()
    slos = list(
        (
            await session.scalars(
                select(ServiceLevelObjective).where(
                    ServiceLevelObjective.component_id == component_id,
                    ServiceLevelObjective.enabled.is_(True),
                )
            )
        ).all()
    )
    if not slos:
        return {"objectives": 0, "compliant": None, "note": "no objectives defined"}
    readings: list[float] = []
    for slo in slos:
        snapshot = await latest_snapshot(session, slo_id=slo.id)
        if snapshot and snapshot.compliance_percent is not None:
            readings.append(snapshot.compliance_percent)
    return {
        "objectives": len(slos),
        "with_readings": len(readings),
        "mean_compliance_percent": (
            round(sum(readings) / len(readings), 4) if readings else None
        ),
    }


async def create_slo(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    name: str,
    indicator: SloIndicator,
    target: float,
    comparison: SloComparison = SloComparison.AT_LEAST,
    metric_name: Optional[str] = None,
    component_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    window_seconds: int = 86_400,
    unit: Optional[str] = None,
    description: Optional[str] = None,
    actor: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> ServiceLevelObjective:
    """Create an objective, validating it before storing it."""
    from app.services.platform_config import validate_configuration

    settings = settings or get_settings()
    validate_configuration(
        scope="SLO",
        settings={
            "name": name,
            "indicator": indicator.value,
            "target": target,
            "window_seconds": window_seconds,
            "metric_name": metric_name,
        },
        settings_obj=settings,
    )
    slo = ServiceLevelObjective(
        project_id=project_id,
        environment_id=environment_id,
        component_id=component_id,
        name=name,
        description=description,
        indicator=indicator,
        comparison=comparison,
        metric_name=metric_name,
        target=target,
        window_seconds=window_seconds,
        unit=unit,
        enabled=True,
        alert_on_burn=True,
        created_by=actor,
    )
    session.add(slo)
    await session.flush()

    from app.services.platform_config import record_configuration

    await record_configuration(
        session,
        project_id=project_id,
        scope="SLO",
        scope_id=slo.id,
        settings={
            "name": name,
            "indicator": indicator.value,
            "comparison": comparison.value,
            "target": target,
            "metric_name": metric_name,
            "window_seconds": window_seconds,
            "unit": unit,
            "component_id": str(component_id) if component_id else None,
        },
        change_summary=f"objective '{name}' created",
        changed_by=actor,
        reason="new objective",
    )
    return slo


__all__ = [
    "MAGNITUDE_INDICATORS",
    "SloEvaluation",
    "budget_history",
    "classify_burn",
    "component_slo_status",
    "create_slo",
    "evaluate_and_record",
    "evaluate_project",
    "evaluate_slo",
    "latest_snapshot",
    "record_evaluation",
    "slo_overview",
]
