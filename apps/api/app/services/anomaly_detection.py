"""ARGUS Anomaly Rule Engine (Phase 3 §18–§19).

Ties the pieces together: load rules → read bounded telemetry → compute a
baseline → evaluate a deterministic detector → deduplicate → persist → emit.

Deliberate properties:

* **Bounded.** Every query is scoped to one project (and optionally one
  environment) and one time window, capped by settings. There is no full-history
  scan anywhere in this module.
* **Idempotent.** Re-running detection over the same telemetry does not create
  duplicate anomalies: the :mod:`app.services.fingerprints` key plus the
  ``anomaly_fingerprints`` registry collapse repeats into one evolving record.
* **Persistent before firing.** A rule with ``persistence_cycles > 1`` must fire
  in that many *distinct telemetry cycles* before an anomaly opens — and only a
  genuinely newer sample counts as a new cycle, so a repeated sweep over
  unchanged data cannot manufacture one.
* **Never silent.** Suppression and maintenance windows are recorded on the
  anomaly (``suppressed``, ``suppression_reason``); anomalies are still stored.
* **No causality.** Nothing here claims why something happened; it records what
  was observed and how far it is from expectation.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import ensure_utc, utcnow
from app.models.anomaly import (
    Anomaly,
    AnomalyBaseline,
    AnomalyFingerprint,
    AnomalyObservation,
    AnomalyRule,
    AnomalySeverity,
    AnomalySource,
    AnomalyStatus,
    AnomalySuppression,
    AnomalyType,
    BaselineStrategy,
    MaintenanceWindow,
    RuleCondition,
)
from app.models.graph import GraphCriticality, GraphNode
from app.models.ingestion import HealthCheckEvent
from app.models.project import Environment
from app.models.observability import (
    LogRecord,
    MetricRecord,
    Severity,
    TraceRecord,
    TraceStatus,
)
from app.services.anomaly_severity import SeveritySignals, compute_severity
from app.services.baseline import BaselineResult, build_baseline
from app.services.detectors import (
    DetectionOutcome,
    detect_baseline_deviation,
    detect_error_rate,
    detect_health_transition,
    detect_latency_ratio,
    detect_pattern_spike,
    detect_rate_change,
    detect_threshold,
    detect_trace_failure_rate,
    detect_z_score,
    normalize_log_pattern,
)
from app.services.engines import AnomalyDetector
from app.services.fingerprints import anomaly_fingerprint
from app.services.redaction import RedactionEngine

logger = logging.getLogger(__name__)
settings = get_settings()

#: Log severities that count as failures for error-rate detection.
_ERROR_SEVERITIES = {Severity.ERROR, Severity.FATAL}

#: Upper bound on how many environments a single *project-wide* run evaluates a
#: project-wide rule against. A project with more environments than this is told
#: so in ``DetectionRunResult.errors`` rather than being scanned unboundedly;
#: scoping the run to one environment always evaluates exactly that one.
MAX_ENVIRONMENTS_PER_RUN = 25


def _latest_timestamp(values: Any) -> Optional[datetime]:
    """Latest non-null timestamp from an iterable of datetime-likes."""
    latest: Optional[datetime] = None
    for value in values:
        ts = ensure_utc(value)
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def _slices(
    start: datetime, end: datetime, count: int
) -> list[tuple[datetime, datetime]]:
    """Split ``[start, end)`` into ``count`` equal, contiguous slices."""
    count = max(1, count)
    total = (end - start) / count
    return [(start + total * i, start + total * (i + 1)) for i in range(count)]


@dataclass
class DetectionRunResult:
    """Summary of one detection pass (bounded, serializable)."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    rules_evaluated: int = 0
    rules_fired: int = 0
    anomalies_opened: int = 0
    anomalies_updated: int = 0
    observations_recorded: int = 0
    suppressed: int = 0
    insufficient_baselines: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "project_id": str(self.project_id),
            "environment_id": str(self.environment_id) if self.environment_id else None,
            "rules_evaluated": self.rules_evaluated,
            "rules_fired": self.rules_fired,
            "anomalies_opened": self.anomalies_opened,
            "anomalies_updated": self.anomalies_updated,
            "observations_recorded": self.observations_recorded,
            "suppressed": self.suppressed,
            "insufficient_baselines": self.insufficient_baselines,
            "errors": self.errors,
        }


@dataclass
class _Evaluation:
    """A fired detector plus the context needed to persist it."""

    rule: AnomalyRule
    outcome: DetectionOutcome
    component_id: Optional[uuid.UUID]
    anomaly_type: AnomalyType
    discriminator: Optional[str]
    baseline: Optional[BaselineResult] = None
    latest_sample_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    sample_count: int = 0
    payload_summary: Optional[dict] = None


class AnomalyDetectionService(AnomalyDetector):
    """Deterministic anomaly detection over persisted telemetry (§19)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        now: Optional[datetime] = None,
        max_rules: Optional[int] = None,
    ) -> None:
        self._session = session
        self._now = ensure_utc(now) or utcnow()
        self._max_rules = max_rules or settings.ANOMALY_MAX_ANOMALIES_PER_RUN
        self._redaction = RedactionEngine()
        #: Per-run count of baselines that could not be computed (reported, not
        #: treated as anomalies — missing data is not failure).
        self._insufficient_baselines = 0

    # -- ABC compliance ----------------------------------------------------
    async def detect(self, events: list[Any]) -> list[Any]:
        """Run detection for each ``{project_id, environment_id}`` scope given."""
        found: list[Any] = []
        for scope in events:
            project_id = scope.get("project_id")
            if project_id is None:
                continue
            result = await self.run(
                project_id=uuid.UUID(str(project_id)),
                environment_id=(
                    uuid.UUID(str(scope["environment_id"]))
                    if scope.get("environment_id")
                    else None
                ),
            )
            found.extend(result.errors)
        return found

    # -- Main entry point ---------------------------------------------------
    async def run(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
    ) -> DetectionRunResult:
        """Evaluate every enabled rule for a project scope and persist findings."""
        result = DetectionRunResult(
            project_id=project_id, environment_id=environment_id
        )
        # Reset the per-run counter of baselines that could not be computed.
        self._insufficient_baselines = 0

        rules = await self._load_rules(project_id, environment_id)
        if len(rules) > self._max_rules:
            result.errors.append(
                f"rule count {len(rules)} exceeds cap {self._max_rules}; "
                "evaluating the first slice only"
            )
            rules = rules[: self._max_rules]

        #: Telemetry scopes to evaluate each rule against. Resolved once per run
        #: (one cheap metadata query) and then per rule — see :meth:`_rule_scopes`.
        project_environments = (
            []
            if environment_id is not None
            else await self._project_environment_ids(project_id)
        )
        if len(project_environments) > MAX_ENVIRONMENTS_PER_RUN:
            result.errors.append(
                f"project has {len(project_environments)} environments; "
                f"evaluating the first {MAX_ENVIRONMENTS_PER_RUN} only"
            )
            project_environments = project_environments[:MAX_ENVIRONMENTS_PER_RUN]

        for rule in rules:
            scopes = self._rule_scopes(rule, environment_id, project_environments)
            if not scopes:
                #: The run targets one environment and this rule belongs to
                #: another; ``_load_rules`` normally excludes it, and this keeps
                #: the invariant even if that filter changes.
                continue
            result.rules_evaluated += 1
            for scope in scopes:
                try:
                    evaluations = await self._evaluate_rule(
                        rule, project_id=project_id, environment_id=scope
                    )
                except Exception as e:  # one bad rule must not stop the pass
                    logger.exception("Rule %s evaluation failed", rule.id)
                    result.errors.append(f"rule {rule.id}: {type(e).__name__}: {e}")
                    continue

                for evaluation in evaluations:
                    if evaluation is None:
                        continue
                    result.rules_fired += 1
                    opened, updated, suppressed, recorded = await self._persist(
                        evaluation, project_id=project_id, environment_id=scope
                    )
                    result.anomalies_opened += opened
                    result.anomalies_updated += updated
                    result.suppressed += suppressed
                    result.observations_recorded += recorded

        result.insufficient_baselines = self._insufficient_baselines
        await self._session.flush()
        return result

    # -- Scope resolution ---------------------------------------------------
    async def _project_environment_ids(self, project_id: uuid.UUID) -> list[uuid.UUID]:
        """The project's environments — metadata, not a telemetry scan.

        A *project-wide* run has to know which environments to evaluate a
        project-wide rule against, or it reads only environment-less rows.
        Because real telemetry always carries the environment it came from, that
        mistake presents as a detection run that evaluates rules and finds
        nothing at all — which is exactly what a project-wide run did before
        this existed.
        """
        stmt = (
            select(Environment.id)
            .where(Environment.project_id == project_id)
            .order_by(Environment.name)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    def _rule_scopes(
        self,
        rule: AnomalyRule,
        run_environment: Optional[uuid.UUID],
        project_environments: Sequence[uuid.UUID],
    ) -> list[Optional[uuid.UUID]]:
        """Which telemetry scope(s) this rule must be evaluated against.

        The distinction that matters: an **environment** is never pooled with
        another. A rule that declares one is only ever read against that
        environment, and an environment-scoped run stays exact. What changed is
        the *project-wide* case, which now evaluates a project-wide rule once per
        environment (plus the environment-less bucket that direct API writes
        land in) instead of reading nothing.
        """
        if rule.environment_id is not None:
            if run_environment is not None and run_environment != rule.environment_id:
                return []
            return [rule.environment_id]
        if run_environment is not None:
            return [run_environment]
        #: Project-wide rule in a project-wide run: every environment, kept
        #: separate, and finally the environment-less rows (direct writes).
        return [*project_environments, None]

    # -- Rule loading -------------------------------------------------------
    async def _load_rules(
        self, project_id: uuid.UUID, environment_id: Optional[uuid.UUID]
    ) -> list[AnomalyRule]:
        stmt = select(AnomalyRule).where(
            AnomalyRule.project_id == project_id,
            AnomalyRule.enabled.is_(True),
        )
        if environment_id is not None:
            # Rules scoped to this environment or to the whole project.
            stmt = stmt.where(
                (AnomalyRule.environment_id == environment_id)
                | (AnomalyRule.environment_id.is_(None))
            )
        stmt = stmt.order_by(AnomalyRule.name)
        return list((await self._session.execute(stmt)).scalars().all())

    # -- Evaluation ---------------------------------------------------------
    async def _evaluate_rule(
        self,
        rule: AnomalyRule,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
    ) -> list[Optional[_Evaluation]]:
        """Dispatch on the rule's condition; return at most one evaluation."""
        condition = RuleCondition(rule.condition)
        if condition is RuleCondition.HEALTH_TRANSITION:
            evaluation = await self._evaluate_health(
                rule, project_id=project_id, environment_id=environment_id
            )
            return [evaluation]
        if condition is RuleCondition.PATTERN_SPIKE:
            return await self._evaluate_log_patterns(
                rule, project_id=project_id, environment_id=environment_id
            )
        if condition is RuleCondition.TRACE_FAILURE_RATE:
            evaluation = await self._evaluate_traces(
                rule, project_id=project_id, environment_id=environment_id
            )
            return [evaluation]
        if condition is RuleCondition.ERROR_RATE:
            evaluation = await self._evaluate_log_error_rate(
                rule, project_id=project_id, environment_id=environment_id
            )
            return [evaluation]
        # Metric-backed conditions share one telemetry read.
        evaluation = await self._evaluate_metric(
            rule, project_id=project_id, environment_id=environment_id
        )
        return [evaluation]

    async def _metric_samples(
        self,
        rule: AnomalyRule,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
    ) -> list[MetricRecord]:
        window = max(1, int(rule.window_seconds))
        since = self._now - timedelta(seconds=window)
        stmt = select(MetricRecord).where(
            MetricRecord.project_id == project_id,
            MetricRecord.metric_name == rule.metric_name,
            MetricRecord.timestamp >= since,
            MetricRecord.timestamp <= self._now,
        )
        # Scope is exact: an environment scope reads that environment's rows, and
        # ``None`` reads environment-less rows. ``None`` deliberately does NOT
        # mean "all environments" — pooling production with staging would let
        # one environment's incident fire another's anomaly.
        #
        # ``environment_id`` is therefore always a *resolved* scope, never the
        # raw run scope: :meth:`_rule_scopes` decides which scope(s) a rule is
        # evaluated in, so a project-wide run evaluates per environment instead
        # of reading only the environment-less bucket.
        stmt = stmt.where(
            MetricRecord.environment_id == environment_id
            if environment_id is not None
            else MetricRecord.environment_id.is_(None)
        )
        if rule.component_id is not None:
            stmt = stmt.where(MetricRecord.component_id == rule.component_id)
        stmt = stmt.order_by(MetricRecord.timestamp).limit(
            settings.ANOMALY_MAX_TELEMETRY_SAMPLES
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def _evaluate_metric(
        self,
        rule: AnomalyRule,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
    ) -> Optional[_Evaluation]:
        records = await self._metric_samples(rule, project_id, environment_id)
        if not records:
            return None

        values = [r.value for r in records]
        latest = records[-1]
        component_id = rule.component_id or latest.component_id
        latest_at = ensure_utc(latest.timestamp)
        discriminator = rule.metric_name

        baseline = self._baseline_for(rule)
        # A rolling baseline must not include the observation it is judging: a
        # spike would otherwise inflate its own expected value and partially
        # hide itself (self-contamination). The current sample is therefore
        # compared against the history that preceded it. Static baselines use
        # the declared value and are unaffected.
        baseline_values = values
        if (
            BaselineStrategy(rule.baseline_strategy) is BaselineStrategy.ROLLING
            and len(values) > 1
        ):
            baseline_values = values[:-1]
        result = baseline.calculate(baseline_values, metric_name=rule.metric_name or "")
        observed = float(latest.value)
        if not result.sufficient and RuleCondition(rule.condition) in (
            RuleCondition.BASELINE_DEVIATION,
            RuleCondition.Z_SCORE,
            RuleCondition.LATENCY_RATIO,
            RuleCondition.RATE_CHANGE,
        ):
            self._insufficient_baselines += 1

        # For latency-style percentiles, compare like with like (§12).
        percentile_key = None
        if RuleCondition(rule.condition) is RuleCondition.LATENCY_RATIO:
            percentile_key = (rule.metadata_ or {}).get("percentile_key")
        if percentile_key:
            result = baseline.expected_value(
                baseline_values,
                metric_name=rule.metric_name or "",
                percentile_key=percentile_key,
            )

        await self._persist_baseline(
            rule,
            result,
            project_id=project_id,
            environment_id=environment_id,
            component_id=component_id,
        )

        condition = RuleCondition(rule.condition)
        outcome: Optional[DetectionOutcome] = None
        if condition is RuleCondition.THRESHOLD:
            outcome = detect_threshold(
                observed=observed,
                threshold=rule.threshold,
                anomaly_type=AnomalyType(rule.anomaly_type),
                source=AnomalySource.METRIC,
                metric_name=rule.metric_name,
            )
        elif condition is RuleCondition.BASELINE_DEVIATION:
            deviation = baseline.detect_deviation(observed, result)
            outcome = detect_baseline_deviation(
                deviation_relative=deviation.relative if deviation else None,
                observed=observed,
                expected=result.expected_value,
                multiplier=rule.multiplier,
                anomaly_type=AnomalyType(rule.anomaly_type),
                metric_name=rule.metric_name,
                sufficient=result.sufficient,
            )
        elif condition is RuleCondition.Z_SCORE:
            deviation = baseline.detect_deviation(observed, result)
            outcome = detect_z_score(
                z_score=deviation.z_score if deviation else None,
                observed=observed,
                expected=result.expected_value,
                z_threshold=rule.z_threshold,
                anomaly_type=AnomalyType(rule.anomaly_type),
                metric_name=rule.metric_name,
                sufficient=result.sufficient,
            )
        elif condition is RuleCondition.LATENCY_RATIO:
            outcome = detect_latency_ratio(
                observed=observed,
                baseline=result,
                multiplier=rule.multiplier,
                anomaly_type=AnomalyType(rule.anomaly_type),
                metric_name=rule.metric_name,
            )
        elif condition is RuleCondition.RATE_CHANGE:
            slices = _slices(
                self._now - timedelta(seconds=max(1, int(rule.window_seconds))),
                self._now,
                min(6, max(2, int(rule.window_seconds) // 60 or 2)),
            )
            current = self._mean_in_slice(records, slices[-1])
            history = [self._mean_in_slice(records, window) for window in slices[:-1]]
            history_clean = [v for v in history if v is not None]
            baseline_samples = build_baseline(
                BaselineStrategy.ROLLING.value,
                min_samples=min(rule.min_samples, max(1, len(history_clean))),
                window_seconds=int(rule.window_seconds),
            ).calculate(history_clean)
            outcome = detect_rate_change(
                current=current,
                baseline=baseline_samples.expected_value,
                threshold=rule.threshold,
                multiplier=rule.multiplier,
                anomaly_type=AnomalyType(rule.anomaly_type),
                metric_name=rule.metric_name,
                sufficient=baseline_samples.sufficient,
            )
        else:
            return None

        if outcome is None or not outcome.fired:
            return None

        if condition is RuleCondition.THRESHOLD and rule.expected_value is not None:
            # `threshold` is the line that was crossed; `expected_value` is the
            # ``normal`` the metric should sit at. Collapsing them into one
            # number would lose the distinction the UI and explanations rely on
            # ("observed 890 vs expected 500" is misleading when 500 was only
            # the ceiling).
            outcome = replace(outcome, expected_value=rule.expected_value)

        return _Evaluation(
            rule=rule,
            outcome=outcome,
            component_id=component_id,
            anomaly_type=AnomalyType(rule.anomaly_type),
            discriminator=discriminator,
            baseline=result,
            latest_sample_at=latest_at,
            sample_count=len(values),
            payload_summary=self._redaction.payload_summary(
                {"metric_name": rule.metric_name, "value": observed}
            ),
        )

    @staticmethod
    def _mean_in_slice(
        records: Sequence[MetricRecord], window: tuple[datetime, datetime]
    ) -> Optional[float]:
        start, end = window
        values = [
            r.value
            for r in records
            if (ts := ensure_utc(r.timestamp)) is not None and start <= ts < end
        ]
        if not values:
            return None
        return sum(values) / len(values)

    async def _evaluate_log_error_rate(
        self,
        rule: AnomalyRule,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
    ) -> Optional[_Evaluation]:
        """Error rate from log severity: (ERROR+FATAL) / total logs (§11)."""
        since = self._now - timedelta(seconds=max(1, int(rule.window_seconds)))
        stmt = select(LogRecord).where(
            LogRecord.project_id == project_id,
            LogRecord.timestamp >= since,
            LogRecord.timestamp <= self._now,
        )
        stmt = stmt.where(
            LogRecord.environment_id == environment_id
            if environment_id is not None
            else LogRecord.environment_id.is_(None)
        )
        if rule.component_id is not None:
            stmt = stmt.where(LogRecord.component_id == rule.component_id)
        stmt = stmt.limit(settings.ANOMALY_MAX_TELEMETRY_SAMPLES)
        records = list((await self._session.execute(stmt)).scalars().all())
        if not records:
            return None
        failed = sum(1 for r in records if r.level in _ERROR_SEVERITIES)
        total = len(records)
        component_id = rule.component_id or records[-1].component_id
        outcome = detect_error_rate(
            failed=float(failed),
            total=float(total),
            threshold=rule.threshold,
            anomaly_type=AnomalyType(rule.anomaly_type),
            source=AnomalySource.LOG,
            metric_name=rule.metric_name,
        )
        if not outcome.fired:
            return None
        return _Evaluation(
            rule=rule,
            outcome=outcome,
            component_id=component_id,
            anomaly_type=AnomalyType(rule.anomaly_type),
            discriminator=rule.metric_name or "log.error_rate",
            latest_sample_at=_latest_timestamp(r.timestamp for r in records),
            sample_count=total,
            payload_summary=self._redaction.payload_summary(
                {"failed": failed, "total": total}
            ),
        )

    async def _evaluate_log_patterns(
        self,
        rule: AnomalyRule,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
    ) -> list[Optional[_Evaluation]]:
        """Detect spikes in normalized log patterns (§13).

        The window is split into equal slices: the last slice is "current", the
        preceding slices form the baseline distribution. That makes a spike a
        comparison against *this* system's normal, not a hardcoded number.
        """
        window_seconds = max(1, int(rule.window_seconds))
        since = self._now - timedelta(seconds=window_seconds)
        stmt = select(LogRecord).where(
            LogRecord.project_id == project_id,
            LogRecord.timestamp >= since,
            LogRecord.timestamp <= self._now,
            LogRecord.level.in_([Severity.ERROR, Severity.FATAL, Severity.WARN]),
        )
        stmt = stmt.where(
            LogRecord.environment_id == environment_id
            if environment_id is not None
            else LogRecord.environment_id.is_(None)
        )
        if rule.component_id is not None:
            stmt = stmt.where(LogRecord.component_id == rule.component_id)
        stmt = stmt.limit(settings.ANOMALY_MAX_TELEMETRY_SAMPLES)
        records = list((await self._session.execute(stmt)).scalars().all())
        if not records:
            return []

        slice_count = 5
        slices = _slices(since, self._now, slice_count)
        pattern_filter = rule.metric_name

        # template -> per-slice counts
        counts: dict[str, list[int]] = {}
        components: dict[str, Optional[uuid.UUID]] = {}
        for record in records:
            template = normalize_log_pattern(record.message)
            if not template:
                continue
            if pattern_filter and pattern_filter not in template:
                continue
            ts = ensure_utc(record.timestamp)
            if ts is None:
                continue
            index = next(
                (i for i, (start, end) in enumerate(slices) if start <= ts < end),
                slice_count - 1,
            )
            counts.setdefault(template, [0] * slice_count)[index] += 1
            components.setdefault(template, record.component_id)

        evaluations: list[Optional[_Evaluation]] = []
        for template, per_slice in sorted(counts.items()):
            current_count = float(per_slice[-1])
            baseline_history = [float(c) for c in per_slice[:-1]]
            baseline = build_baseline(
                BaselineStrategy.ROLLING.value,
                min_samples=min(rule.min_samples, max(1, len(baseline_history))),
                window_seconds=window_seconds,
            ).calculate(baseline_history)
            outcome = detect_pattern_spike(
                current_count=current_count,
                baseline=baseline,
                multiplier=rule.multiplier,
                pattern_template=template,
                metric_name=rule.metric_name,
            )
            if not outcome.fired:
                continue
            evaluations.append(
                _Evaluation(
                    rule=rule,
                    outcome=outcome,
                    component_id=rule.component_id or components.get(template),
                    anomaly_type=AnomalyType(rule.anomaly_type),
                    discriminator=template,
                    baseline=baseline,
                    latest_sample_at=self._now,
                    sample_count=sum(per_slice),
                    payload_summary=self._redaction.payload_summary(
                        {"pattern": template, "count": current_count}
                    ),
                )
            )
        return evaluations

    async def _evaluate_traces(
        self,
        rule: AnomalyRule,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
    ) -> Optional[_Evaluation]:
        """Trace failure rate from persisted traces (§14)."""
        since = self._now - timedelta(seconds=max(1, int(rule.window_seconds)))
        stmt = select(TraceRecord).where(
            TraceRecord.project_id == project_id,
            TraceRecord.start_time >= since,
            TraceRecord.start_time <= self._now,
        )
        stmt = stmt.where(
            TraceRecord.environment_id == environment_id
            if environment_id is not None
            else TraceRecord.environment_id.is_(None)
        )
        stmt = stmt.limit(settings.ANOMALY_MAX_TELEMETRY_SAMPLES)
        traces = list((await self._session.execute(stmt)).scalars().all())
        if not traces:
            return None
        failed_statuses = {TraceStatus.ERROR, TraceStatus.TIMEOUT}
        failed = sum(1 for t in traces if t.status in failed_statuses)
        total = len(traces)
        outcome = detect_trace_failure_rate(
            failed=float(failed),
            total=float(total),
            threshold=rule.threshold,
            metric_name=rule.metric_name,
        )
        if not outcome.fired:
            return None
        return _Evaluation(
            rule=rule,
            outcome=outcome,
            component_id=rule.component_id,
            anomaly_type=AnomalyType(rule.anomaly_type),
            discriminator=rule.metric_name or "trace.failure_rate",
            latest_sample_at=_latest_timestamp(t.start_time for t in traces),
            sample_count=total,
            payload_summary=self._redaction.payload_summary(
                {"failed": failed, "total": total}
            ),
        )

    async def _evaluate_health(
        self,
        rule: AnomalyRule,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
    ) -> Optional[_Evaluation]:
        """Health degradation from the two most recent health checks (§15)."""
        since = self._now - timedelta(seconds=max(1, int(rule.window_seconds)))
        stmt = select(HealthCheckEvent).where(
            HealthCheckEvent.project_id == project_id,
            HealthCheckEvent.timestamp >= since,
            HealthCheckEvent.timestamp <= self._now,
        )
        stmt = stmt.where(
            HealthCheckEvent.environment_id == environment_id
            if environment_id is not None
            else HealthCheckEvent.environment_id.is_(None)
        )
        if rule.component_id is not None:
            stmt = stmt.where(HealthCheckEvent.component_id == rule.component_id)
        stmt = stmt.order_by(HealthCheckEvent.timestamp).limit(200)
        events = list((await self._session.execute(stmt)).scalars().all())
        if len(events) < 2:
            return None

        # Evaluate the latest transition per component (the most recent pair).
        by_component: dict[Optional[uuid.UUID], list[HealthCheckEvent]] = {}
        for event in events:
            by_component.setdefault(event.component_id, []).append(event)
        for component_id, rows in by_component.items():
            if len(rows) < 2:
                continue
            previous, current = rows[-2], rows[-1]
            outcome = detect_health_transition(
                previous_status=previous.status.value
                if hasattr(previous.status, "value")
                else str(previous.status),
                current_status=current.status.value
                if hasattr(current.status, "value")
                else str(current.status),
                metric_name=rule.metric_name,
            )
            if not outcome.fired:
                continue
            return _Evaluation(
                rule=rule,
                outcome=outcome,
                component_id=rule.component_id or component_id,
                anomaly_type=AnomalyType(rule.anomaly_type),
                discriminator=rule.metric_name or "health.state",
                latest_sample_at=ensure_utc(current.timestamp),
                sample_count=len(rows),
                payload_summary=self._redaction.payload_summary(
                    {
                        "previous": outcome.metadata.get("previous_status"),
                        "current": outcome.metadata.get("current_status"),
                    }
                ),
            )
        return None

    async def _severity_signals(
        self, evaluation: _Evaluation, duration: Optional[float]
    ) -> SeveritySignals:
        """Build severity inputs from the *shape* of the fired condition.

        Each condition stores its deviation differently — a THRESHOLD outcome
        carries an absolute distance while BASELINE_DEVIATION carries a ratio.
        Feeding an absolute value where a ratio is expected would inflate
        severity, so the mapping is explicit rather than guessed.
        """
        condition = RuleCondition(evaluation.rule.condition)
        outcome = evaluation.outcome
        observed = outcome.observed_value
        expected = outcome.expected_value

        deviation_relative: Optional[float] = None
        error_rate: Optional[float] = None
        ratio: Optional[float] = None

        if condition is RuleCondition.BASELINE_DEVIATION:
            deviation_relative = outcome.deviation
        elif condition is RuleCondition.Z_SCORE:
            pass  # z-score is passed directly below
        elif condition in (RuleCondition.ERROR_RATE, RuleCondition.TRACE_FAILURE_RATE):
            error_rate = observed
        elif condition in (RuleCondition.LATENCY_RATIO, RuleCondition.PATTERN_SPIKE):
            meta_ratio = outcome.metadata.get("ratio")
            if isinstance(meta_ratio, (int, float)):
                ratio = float(meta_ratio)
            elif observed is not None and expected is not None and expected != 0:
                ratio = observed / expected
        elif condition is RuleCondition.RATE_CHANGE:
            meta_relative = outcome.metadata.get("relative")
            if isinstance(meta_relative, (int, float)):
                deviation_relative = float(meta_relative)
        elif condition is RuleCondition.THRESHOLD:
            if observed is not None and expected is not None and expected != 0:
                ratio = observed / expected

        return SeveritySignals(
            base=AnomalySeverity(evaluation.rule.severity),
            deviation_relative=deviation_relative,
            z_score=outcome.z_score,
            error_rate=error_rate,
            ratio=ratio,
            duration_seconds=duration,
            component_criticality=await self._component_criticality(
                evaluation.component_id
            ),
        )

    def _baseline_for(self, rule: AnomalyRule):
        return build_baseline(
            BaselineStrategy(rule.baseline_strategy).value,
            window_seconds=int(rule.window_seconds),
            min_samples=int(rule.min_samples),
            expected_value=rule.expected_value,
            expected_stat=(rule.metadata_ or {}).get("expected_stat", "mean"),
        )

    async def _persist_baseline(
        self,
        rule: AnomalyRule,
        result: BaselineResult,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
        component_id: Optional[uuid.UUID],
    ) -> None:
        """Record the computed baseline so detection is auditable (§51)."""
        stats = result.stats
        try:
            strategy = BaselineStrategy(result.strategy)
        except ValueError:
            strategy = BaselineStrategy.ROLLING
        self._session.add(
            AnomalyBaseline(
                project_id=project_id,
                environment_id=environment_id,
                component_id=component_id,
                metric_name=rule.metric_name
                or getattr(rule.anomaly_type, "value", str(rule.anomaly_type)),
                strategy=strategy,
                window_seconds=result.window_seconds,
                sample_count=stats.sample_count,
                mean=stats.mean,
                median=stats.median,
                stddev=stats.stddev,
                min_value=stats.min_value,
                max_value=stats.max_value,
                p50=stats.p50,
                p95=stats.p95,
                p99=stats.p99,
                expected_value=result.expected_value,
                computed_at=self._now,
                metadata_={
                    "sufficient": result.sufficient,
                    "reason": result.reason,
                    "rule_id": str(rule.id),
                },
            )
        )

    # -- Persistence, dedup, suppression -----------------------------------
    async def _persist(
        self,
        evaluation: _Evaluation,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
    ) -> tuple[int, int, int, int]:
        """Persist a firing evaluation. Returns (opened, updated, suppressed, obs)."""
        rule = evaluation.rule
        fingerprint = anomaly_fingerprint(
            project_id=project_id,
            anomaly_type=evaluation.anomaly_type,
            discriminator=evaluation.discriminator,
            environment_id=environment_id,
            component_id=evaluation.component_id,
        )
        registry = await self._fingerprint_row(project_id, fingerprint)
        is_new_cycle = self._is_new_cycle(registry, evaluation.latest_sample_at)

        suppression = await self._suppression_for(
            project_id=project_id,
            environment_id=environment_id,
            component_id=evaluation.component_id,
            anomaly_type=evaluation.anomaly_type,
            metric_name=evaluation.outcome.metric_name,
        )

        existing = await self._active_anomaly_for(project_id, fingerprint)
        duration = None
        if existing is not None:
            started = ensure_utc(existing.started_at or existing.detected_at)
            if started is not None:
                duration = (self._now - started).total_seconds()

        severity_decision = compute_severity(
            await self._severity_signals(evaluation, duration)
        )

        # Persistence gate: hold the candidate until enough distinct cycles fired.
        # Applies on the very first evaluation too, so a rule needing two cycles
        # does not open on its first look.
        if existing is None:
            consecutive = (
                int((registry.metadata_ or {}).get("consecutive_fires", 0))
                if registry is not None
                else 0
            )
            if is_new_cycle:
                consecutive += 1
            if registry is None:
                # Track the pending candidate before an anomaly exists, so the
                # cycle counter survives between runs.
                registry = await self._claim_registry(
                    project_id=project_id,
                    environment_id=environment_id,
                    component_id=evaluation.component_id,
                    fingerprint=fingerprint,
                    anomaly_type=evaluation.anomaly_type,
                )
                if registry is None:
                    # The rival's row exists but is not readable yet (it is
                    # uncommitted), so this evaluation cannot count its cycle.
                    # Returning zero is honest: nothing was opened or updated.
                    return (0, 0, 0, 0)
            registry.metadata_ = {
                **(registry.metadata_ or {}),
                "consecutive_fires": consecutive,
                "last_sample_at": (
                    evaluation.latest_sample_at.isoformat()
                    if evaluation.latest_sample_at
                    else None
                ),
            }
            if consecutive < max(1, int(rule.persistence_cycles)):
                return (0, 0, 0, 0)

        if existing is not None:
            # Capture the previous last-seen BEFORE overwriting it — the cooldown
            # compares against when we last saw this anomaly, not against now.
            previous_last_seen = ensure_utc(
                existing.last_seen_at or existing.detected_at
            )
            cooldown_elapsed = self._cooldown_elapsed(previous_last_seen, rule)
            if is_new_cycle:
                existing.observation_count += 1
            existing.last_seen_at = self._now
            existing.observed_value = evaluation.outcome.observed_value
            existing.expected_value = evaluation.outcome.expected_value
            existing.deviation = evaluation.outcome.deviation
            existing.z_score = evaluation.outcome.z_score
            existing.threshold = evaluation.outcome.threshold
            existing.confidence = evaluation.outcome.confidence
            existing.description = evaluation.outcome.description
            existing.severity = max(
                AnomalySeverity(existing.severity), severity_decision.severity
            )
            self._apply_suppression(existing, suppression, severity_decision)
            observed = 0
            if is_new_cycle and cooldown_elapsed:
                self._add_observation(existing, evaluation, project_id, environment_id)
                observed = 1
            await self._touch_registry(registry, existing, evaluation)
            return (0, 1, 1 if existing.suppressed else 0, observed)

        # Open a new anomaly.
        anomaly = Anomaly(
            project_id=project_id,
            environment_id=environment_id,
            component_id=evaluation.component_id,
            rule_id=rule.id,
            anomaly_type=evaluation.anomaly_type,
            severity=severity_decision.severity,
            status=AnomalyStatus.DETECTED,
            source=evaluation.outcome.source,
            metric_name=evaluation.outcome.metric_name,
            pattern_template=evaluation.outcome.pattern_template,
            observed_value=evaluation.outcome.observed_value,
            expected_value=evaluation.outcome.expected_value,
            deviation=evaluation.outcome.deviation,
            threshold=evaluation.outcome.threshold,
            z_score=evaluation.outcome.z_score,
            confidence=evaluation.outcome.confidence,
            fingerprint=fingerprint,
            description=evaluation.outcome.description,
            observation_count=1,
            detected_at=self._now,
            started_at=self._now,
            last_seen_at=self._now,
            metadata_={
                "severity_reasons": severity_decision.reasons,
                "severity_factors": severity_decision.factors,
                "detector_reason": evaluation.outcome.reason,
                "baseline": {
                    "strategy": evaluation.baseline.strategy
                    if evaluation.baseline
                    else None,
                    "sample_count": evaluation.sample_count,
                    "expected_value": evaluation.baseline.expected_value
                    if evaluation.baseline
                    else None,
                    "sufficient": evaluation.baseline.sufficient
                    if evaluation.baseline
                    else None,
                },
            },
        )
        self._apply_suppression(anomaly, suppression, severity_decision)
        self._session.add(anomaly)
        await self._session.flush()
        self._add_observation(anomaly, evaluation, project_id, environment_id)
        await self._touch_registry(
            registry, anomaly, evaluation, project_id, fingerprint
        )
        return (1, 0, 1 if anomaly.suppressed else 0, 1)

    async def _fingerprint_row(
        self, project_id: uuid.UUID, fingerprint: str
    ) -> Optional[AnomalyFingerprint]:
        stmt = select(AnomalyFingerprint).where(
            AnomalyFingerprint.project_id == project_id,
            AnomalyFingerprint.fingerprint == fingerprint,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _claim_registry(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
        component_id: Optional[uuid.UUID],
        fingerprint: str,
        anomaly_type: Any,
    ) -> Optional[AnomalyFingerprint]:
        """Create the pending fingerprint registry row, or adopt the rival's.

        Detection runs in two places at once: an explicit ``/anomalies/detect``
        call and the background sweep that evaluates every active project. Both
        can reach the same sample with no registry row yet, and both then insert
        the same ``(project_id, fingerprint)`` — the loser used to fail the whole
        request with an ``IntegrityError``, which a live run showed as a 500 on
        detection.

        A savepoint makes the insert atomic: on conflict the rival's row is
        re-read, so the two detectors *share* one registry instead of one of them
        crashing. The row is invisible until it commits, in which case ``None``
        is returned and the caller records nothing rather than guessing.
        """
        candidate = AnomalyFingerprint(
            project_id=project_id,
            environment_id=environment_id,
            component_id=component_id,
            fingerprint=fingerprint,
            anomaly_type=anomaly_type,
            anomaly_id=None,
            occurrence_count=0,
            first_seen_at=self._now,
            last_seen_at=self._now,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(candidate)
                await self._session.flush()
        except IntegrityError:
            return await self._fingerprint_row(project_id, fingerprint)
        return candidate

    async def _active_anomaly_for(
        self, project_id: uuid.UUID, fingerprint: str
    ) -> Optional[Anomaly]:
        """The current open anomaly for a fingerprint, if any.

        ``EXPIRED`` anomalies do not count — expiry is terminal, so a later
        recurrence is a genuinely new anomaly rather than a resurrection.
        """
        stmt = (
            select(Anomaly)
            .where(
                Anomaly.project_id == project_id,
                Anomaly.fingerprint == fingerprint,
                Anomaly.status.notin_([AnomalyStatus.RESOLVED, AnomalyStatus.EXPIRED]),
            )
            .order_by(Anomaly.detected_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _is_new_cycle(
        registry: Optional[AnomalyFingerprint], latest_sample_at: Optional[datetime]
    ) -> bool:
        """True when genuinely new telemetry arrived since the last evaluation.

        Without this, a repeated sweep over unchanged data would count as a new
        persistence cycle and could manufacture an anomaly from silence.
        """
        if latest_sample_at is None:
            return True
        if registry is None:
            return True
        last_sample = (registry.metadata_ or {}).get("last_sample_at")
        if not last_sample:
            return True
        try:
            parsed = ensure_utc(datetime.fromisoformat(str(last_sample)))
        except (ValueError, TypeError):
            return True
        if parsed is None:
            return True
        return latest_sample_at > parsed

    def _cooldown_elapsed(
        self, previous_last_seen: Optional[datetime], rule: AnomalyRule
    ) -> bool:
        """Whether enough time passed to record another observation (§41).

        Acts as noise reduction: within the cooldown the anomaly is still
        updated (``last_seen_at``, counters, latest values) but no new
        observation row is written, so a 10-second poll cannot produce a flood.
        """
        cooldown = max(0, int(rule.cooldown_seconds))
        if cooldown == 0 or previous_last_seen is None:
            return True
        return (previous_last_seen + timedelta(seconds=cooldown)) <= self._now

    def _add_observation(
        self,
        anomaly: Anomaly,
        evaluation: _Evaluation,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
    ) -> None:
        self._session.add(
            AnomalyObservation(
                anomaly_id=anomaly.id,
                project_id=project_id,
                environment_id=environment_id,
                component_id=evaluation.component_id,
                observed_at=evaluation.latest_sample_at or self._now,
                observed_value=evaluation.outcome.observed_value,
                expected_value=evaluation.outcome.expected_value,
                deviation=evaluation.outcome.deviation,
                z_score=evaluation.outcome.z_score,
                sample_count=evaluation.sample_count,
                payload_summary=evaluation.payload_summary,
            )
        )

    async def _touch_registry(
        self,
        registry: Optional[AnomalyFingerprint],
        anomaly: Anomaly,
        evaluation: _Evaluation,
        project_id: Optional[uuid.UUID] = None,
        fingerprint: Optional[str] = None,
    ) -> None:
        """Create/refresh the fingerprint registry row pointing at ``anomaly``.

        The create path is conflict-tolerant for the same reason
        :meth:`_claim_registry` is: the explicit detect endpoint and the
        background sweep can open the same anomaly at the same instant. The
        loser adopts the rival's row instead of failing the request.
        """
        now = self._now
        if registry is None:
            if project_id is None or fingerprint is None:
                return
            try:
                async with self._session.begin_nested():
                    self._session.add(
                        AnomalyFingerprint(
                            project_id=project_id,
                            environment_id=anomaly.environment_id,
                            component_id=anomaly.component_id,
                            fingerprint=fingerprint,
                            anomaly_type=anomaly.anomaly_type,
                            anomaly_id=anomaly.id,
                            occurrence_count=1,
                            first_seen_at=now,
                            last_seen_at=now,
                            metadata_={"consecutive_fires": 1},
                        )
                    )
                    await self._session.flush()
                return
            except IntegrityError:
                registry = await self._fingerprint_row(project_id, fingerprint)
                if registry is None:
                    return
        registry.anomaly_id = anomaly.id
        registry.occurrence_count += 1
        registry.last_seen_at = now
        registry.metadata_ = {
            **(registry.metadata_ or {}),
            "consecutive_fires": 0,
            "last_sample_at": (
                evaluation.latest_sample_at.isoformat()
                if evaluation.latest_sample_at
                else None
            ),
        }

    async def _suppression_for(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
        component_id: Optional[uuid.UUID],
        anomaly_type: AnomalyType,
        metric_name: Optional[str],
    ) -> Optional[str]:
        """Return a suppression reason, or ``None``.

        Both explicit suppression rules and active maintenance windows are
        considered. The reason is stored on the anomaly, so nothing is hidden.
        """
        now = self._now
        stmt = select(AnomalySuppression).where(
            AnomalySuppression.project_id == project_id,
            AnomalySuppression.enabled.is_(True),
            AnomalySuppression.starts_at <= now,
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        for row in rows:
            ends_at = ensure_utc(row.ends_at)
            if ends_at is not None and ends_at < now:
                continue
            if row.environment_id is not None and row.environment_id != environment_id:
                continue
            if row.component_id is not None and row.component_id != component_id:
                continue
            if row.anomaly_type is not None and row.anomaly_type != anomaly_type:
                continue
            if row.metric_name is not None and row.metric_name != metric_name:
                continue
            return f"suppression rule: {row.reason}"

        window_stmt = select(MaintenanceWindow).where(
            MaintenanceWindow.project_id == project_id,
            MaintenanceWindow.enabled.is_(True),
            MaintenanceWindow.suppress_anomalies.is_(True),
            MaintenanceWindow.starts_at <= now,
            MaintenanceWindow.ends_at >= now,
        )
        windows = list((await self._session.execute(window_stmt)).scalars().all())
        for window in windows:
            if (
                window.environment_id is not None
                and window.environment_id != environment_id
            ):
                continue
            return f"maintenance window: {window.name}"
        return None

    @staticmethod
    def _apply_suppression(
        anomaly: Anomaly, reason: Optional[str], severity_decision
    ) -> None:
        """Record (never apply silently) suppression on the anomaly."""
        if reason is None:
            return
        anomaly.suppressed = True
        anomaly.suppression_reason = reason[:255]
        anomaly.suppressed_at = utcnow()
        anomaly.metadata_ = {
            **(anomaly.metadata_ or {}),
            "severity_reasons": severity_decision.reasons,
        }

    async def _component_criticality(
        self, component_id: Optional[uuid.UUID]
    ) -> Optional[str]:
        if component_id is None:
            return None
        stmt = select(GraphNode.criticality).where(
            GraphNode.entity_kind == "system_component",
            GraphNode.entity_id == component_id,
        )
        value = (await self._session.execute(stmt)).scalars().first()
        if value is None:
            return None
        return value.value if isinstance(value, GraphCriticality) else str(value)


__all__ = ["AnomalyDetectionService", "DetectionRunResult"]
