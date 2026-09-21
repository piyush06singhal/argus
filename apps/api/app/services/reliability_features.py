"""ARGUS Reliability Feature Engine (Phase 8 §7–§18).

Turns stored Phase 1–7 evidence into structured predictive features for one
component, one environment and one instant in time.

Three properties carry the phase's guarantees:

**Everything is bounded by ``forecast_time``.** Every query in this module
filters on a timestamp ``<= forecast_time``. That is the leakage boundary
(§30, §65): a prediction made at 14:00 cannot be influenced by an incident at
15:00, and the tests assert it directly. The boundary is applied here, once,
rather than trusted to each caller.

**Missing data stays missing.** Metrics with no samples produce ``None``
features, never zeros — a metric that stopped arriving must not read as
"steady at zero", because that is exactly how a broken pipeline looks like
stability (§42). ``sample_sufficient`` and the data-quality verdict record how
thin the evidence was.

**Features are descriptions, not causes.** Dependency structure tells us a
failure *could* travel (Phase 2 §14); it never tells us it did. Feature names
say "degraded dependency" and "anomaly density", not "cause".

The engine is deliberately read-only: it never writes, never mutates a table
and never calls a model. Persisting the snapshot is the forecast service's job,
which keeps this module unit-testable against a plain database session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.anomaly import Anomaly, AnomalySeverity
from app.models.code import CodeRiskSignal, RepositorySnapshot, RiskSignalType
from app.models.deployment import DeploymentEvent, DeploymentStatus
from app.models.fix import Patch, PatchStatus
from app.models.incident import Incident, IncidentSeverity, IncidentStatus
from app.models.observability import LogRecord, MetricRecord, Severity, SpanRecord
from app.models.reliability import ForecastDataQuality, FeatureTrend
from app.models.system import ComponentDependency, DependencyType
from app.services import reliability_stats as stats

logger = logging.getLogger(__name__)
settings = get_settings()

#: Bumped whenever a change would produce different values for the same input
#: rows, so stored snapshots stay interpretable (§17).
FEATURE_SCHEMA_VERSION = "v1"

#: Canonical metric families the phase names explicitly (§8).
CANONICAL_METRICS: tuple[str, ...] = (
    "request_rate",
    "error_rate",
    "latency_p50",
    "latency_p95",
    "latency_p99",
    "cpu_utilization",
    "memory_utilization",
    "disk_utilization",
    "queue_depth",
    "connection_pool_usage",
)

#: Statistics computed for every canonical metric.
STAT_SUFFIXES: tuple[str, ...] = (
    "current",
    "mean",
    "median",
    "stddev",
    "min",
    "max",
    "slope",
    "change_rate",
    "deviation_from_baseline",
    "volatility",
)

#: Telemetry families that must be present for a forecast to be trustworthy.
#: These are the inputs a prediction actually depends on; the other families
#: enrich a forecast but their absence is not an ingestion fault.
TELEMETRY_FAMILIES: tuple[str, ...] = ("metrics", "errors", "latency")

#: Dependency types treated as hard infrastructure dependencies. Documented
#: rather than inferred: HTTP and external calls can degrade independently,
#: whereas a database/queue/cache/RPC edge is on the synchronous path.
HARD_DEPENDENCY_TYPES: frozenset[DependencyType] = frozenset(
    {
        DependencyType.DATABASE,
        DependencyType.QUEUE,
        DependencyType.CACHE,
        DependencyType.RPC,
    }
)

#: Severity order used when summarising anomaly populations.
_ANOMALY_SEVERITIES: tuple[AnomalySeverity, ...] = (
    AnomalySeverity.CRITICAL,
    AnomalySeverity.HIGH,
    AnomalySeverity.MEDIUM,
    AnomalySeverity.LOW,
)


def aware_utc(value: datetime) -> datetime:
    """Normalise a stored timestamp to aware UTC.

    PostgreSQL returns aware datetimes; SQLite's ``CURRENT_TIMESTAMP`` returns
    naive ones. Filtering on a naive value against an aware column (or the
    reverse) silently compares the wrong instants, so every boundary goes
    through here.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def canonical_metric_key(metric_name: str) -> Optional[str]:
    """Map a raw metric name onto a canonical family (§8).

    Deterministic substring rules over the name, checked most-specific first so
    ``http.checkout.latency.p95`` maps to ``latency_p95`` rather than to the
    generic latency family. Returns ``None`` when no rule matches — such a
    metric is still profiled and stored, it simply contributes no canonical
    feature, because inventing a mapping would be a guess.
    """
    name = (metric_name or "").lower()
    if not name:
        return None

    if "latency" in name or "duration" in name or "response_time" in name:
        if name.endswith(".p99") or ".p99" in name or "p99" in name:
            return "latency_p99"
        if name.endswith(".p95") or ".p95" in name or "p95" in name:
            return "latency_p95"
        if name.endswith(".p50") or ".p50" in name or "p50" in name:
            return "latency_p50"
        return "latency_p95"

    if "error_rate" in name or ("error" in name and "rate" in name):
        return "error_rate"
    if "error" in name and ("count" in name or "total" in name):
        return "error_rate"

    if "request" in name and "rate" in name:
        return "request_rate"
    if "request" in name and ("count" in name or "total" in name):
        return "request_rate"
    if name.endswith("throughput") or "throughput" in name:
        return "request_rate"

    if "cpu" in name:
        return "cpu_utilization"
    if "memory" in name or ".mem" in name or name.startswith("mem"):
        return "memory_utilization"
    if "disk" in name:
        return "disk_utilization"
    if "queue" in name:
        return "queue_depth"
    if "connection" in name or "pool" in name:
        return "connection_pool_usage"
    return None


@dataclass
class FeatureBundle:
    """Everything the features for one prediction depend on.

    ``numeric`` is the flat, model-facing map; ``detail`` holds the per-series
    profiles and structured summaries that explanations are built from;
    ``sources`` records which tables actually contributed and how many rows, so
    an auditor can see the basis of the numbers (§82).
    """

    project_id: Any
    environment_id: Optional[Any]
    component_id: Optional[Any]
    component_name: Optional[str]
    forecast_time: datetime
    window_start: datetime
    window_end: datetime
    baseline_window_start: datetime
    numeric: dict[str, Optional[float]] = field(default_factory=dict)
    trends: dict[str, str] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, int] = field(default_factory=dict)
    quality: ForecastDataQuality = ForecastDataQuality.INSUFFICIENT
    quality_notes: list[str] = field(default_factory=list)
    coverage: float = 0.0
    sample_count: int = 0
    window_seconds: int = 0
    #: Newest telemetry instant seen. Deliberately *not* part of the snapshot
    #: payload (which must be JSON-serialisable): it is working state used by
    #: staleness detection, while the detail map carries an ISO string for
    #: display.
    latest_telemetry_at: Optional[datetime] = None

    def value(self, name: str) -> Optional[float]:
        return self.numeric.get(name)

    def trend(self, name: str) -> FeatureTrend:
        raw = self.trends.get(name)
        if raw is None:
            return FeatureTrend.UNKNOWN
        try:
            return FeatureTrend(raw)
        except ValueError:
            return FeatureTrend.UNKNOWN

    def as_snapshot_payload(self) -> dict:
        """The JSON written to ``ForecastFeatureSnapshot.feature_values``."""
        return {
            "schema_version": FEATURE_SCHEMA_VERSION,
            "numeric": self.numeric,
            "trends": self.trends,
            "detail": self.detail,
        }


class ReliabilityFeatureEngine:
    """Builds a :class:`FeatureBundle` for one component and instant (§7)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- entry point ----------------------------------------------------
    async def build(
        self,
        *,
        project_id: Any,
        environment_id: Optional[Any],
        component_id: Optional[Any],
        forecast_time: datetime,
        component_name: Optional[str] = None,
        window_seconds: Optional[int] = None,
        baseline_seconds: Optional[int] = None,
    ) -> FeatureBundle:
        window_seconds = window_seconds or settings.RELIABILITY_FEATURE_WINDOW_SECONDS
        baseline_seconds = (
            baseline_seconds or settings.RELIABILITY_BASELINE_WINDOW_SECONDS
        )
        forecast_time = aware_utc(forecast_time)
        window_start = forecast_time - timedelta(seconds=window_seconds)
        baseline_start = forecast_time - timedelta(seconds=baseline_seconds)

        bundle = FeatureBundle(
            project_id=project_id,
            environment_id=environment_id,
            component_id=component_id,
            component_name=component_name,
            forecast_time=forecast_time,
            window_start=window_start,
            window_end=forecast_time,
            baseline_window_start=baseline_start,
            window_seconds=window_seconds,
        )

        await self._add_metrics(bundle)
        await self._add_errors(bundle)
        await self._add_latency(bundle)
        await self._add_incidents(bundle)
        await self._add_anomalies(bundle)
        await self._add_dependencies(bundle)
        await self._add_deployments(bundle)
        await self._add_code(bundle)
        self._add_resource_pressure(bundle)
        self._assess_quality(bundle)

        bundle.detail["component_name"] = component_name
        bundle.detail["forecast_time"] = forecast_time.isoformat()
        bundle.detail["window_seconds"] = window_seconds
        return bundle

    # -- metrics (§8) ---------------------------------------------------
    async def _add_metrics(self, bundle: FeatureBundle) -> None:
        rows = (
            await self.session.execute(
                select(
                    MetricRecord.metric_name, MetricRecord.timestamp, MetricRecord.value
                )
                .where(
                    *self._scope_clause(MetricRecord, bundle),
                    MetricRecord.timestamp >= bundle.baseline_window_start,
                    #: The leakage boundary (§65).
                    MetricRecord.timestamp <= bundle.forecast_time,
                )
                .order_by(MetricRecord.timestamp.asc())
                .limit(settings.RELIABILITY_MAX_SAMPLES_PER_METRIC * 4)
            )
        ).all()
        bundle.sources["metrics"] = len(rows)
        if not rows:
            bundle.quality_notes.append(
                "no metric samples were recorded for this scope before the forecast time"
            )
            return

        series: dict[str, list[tuple[datetime, float]]] = {}
        for metric_name, timestamp, value in rows:
            if metric_name is None or value is None:
                continue
            series.setdefault(str(metric_name), []).append(
                (aware_utc(timestamp), float(value))
            )

        #: Canonical metrics first, then the most-observed others, so the cap
        #: never drops a family the phase names (§59).
        ranked = sorted(
            series.items(),
            key=lambda item: (
                canonical_metric_key(item[0]) is None,
                -len(item[1]),
                item[0],
            ),
        )[: settings.RELIABILITY_MAX_METRIC_SERIES]

        profiles: dict[str, dict] = {}
        #: Canonical entries carry the profile, the cadence-independent hourly
        #: slope and the baseline mean, so the emitted features never depend on
        #: how often the metric happened to be scraped.
        canonical: dict[str, dict[str, Any]] = {}

        for metric_name, points in ranked:
            window_values = [value for ts, value in points if ts >= bundle.window_start]
            baseline_values = [
                value for ts, value in points if ts < bundle.window_start
            ]
            profile = stats.profile_series(
                metric_name,
                window_values,
                min_samples=settings.RELIABILITY_MIN_SAMPLES,
                flat_epsilon=settings.RELIABILITY_FLAT_EPSILON,
                volatile_ratio=settings.RELIABILITY_VOLATILE_RATIO,
                strong_slope=settings.RELIABILITY_STRONG_SLOPE,
            )
            baseline_mean = stats.mean(baseline_values)
            entry = profile.as_dict()
            entry["baseline_mean"] = baseline_mean
            entry["baseline_sample_count"] = len(baseline_values)
            entry["deviation_from_baseline"] = stats.change_rate(
                profile.mean, baseline_mean
            )
            profiles[metric_name] = entry

            key = canonical_metric_key(metric_name)
            if key is not None and key not in canonical:
                window_points = [
                    (ts, value) for ts, value in points if ts >= bundle.window_start
                ]
                canonical[key] = {
                    "profile": _with_baseline(profile, baseline_mean),
                    "hourly_slope": stats.slope_per_hour(
                        [value for _ts, value in window_points],
                        [ts for ts, _value in window_points],
                    ),
                    "baseline_mean": baseline_mean,
                }

        bundle.detail["metrics"] = profiles
        #: Newest metric timestamp drives staleness detection (§18, §42).
        newest_metric = max(
            (aware_utc(ts) for _n, points in ranked for ts, _v in points),
            default=None,
        )
        if newest_metric is not None:
            bundle.detail["metrics_latest_at"] = newest_metric.isoformat()
            _note_latest(bundle, newest_metric)
        bundle.numeric["metric_series_count"] = float(len(ranked))
        bundle.numeric["metric_sample_count"] = float(
            sum(len(points) for _n, points in ranked)
        )

        for key in CANONICAL_METRICS:
            canonical_entry = canonical.get(key)
            prefix = f"{key}_"
            if canonical_entry is None:
                for suffix in STAT_SUFFIXES:
                    bundle.numeric[f"{prefix}{suffix}"] = None
                bundle.trends[key] = FeatureTrend.UNKNOWN.value
                continue
            canonical_profile = canonical_entry["profile"]
            bundle.numeric[f"{prefix}current"] = canonical_profile.current
            bundle.numeric[f"{prefix}mean"] = canonical_profile.mean
            bundle.numeric[f"{prefix}median"] = canonical_profile.median
            bundle.numeric[f"{prefix}stddev"] = canonical_profile.stddev
            bundle.numeric[f"{prefix}min"] = canonical_profile.minimum
            bundle.numeric[f"{prefix}max"] = canonical_profile.maximum
            #: Hourly slope where timestamps allow it, falling back to the
            #: per-step slope only for degenerate series (all timestamps equal).
            hourly = canonical_entry["hourly_slope"]
            bundle.numeric[f"{prefix}slope"] = (
                hourly if hourly is not None else canonical_profile.normalized_slope
            )
            bundle.numeric[f"{prefix}slope_per_observation"] = (
                canonical_profile.normalized_slope
            )
            bundle.numeric[f"{prefix}change_rate"] = stats.change_rate(
                canonical_profile.current, canonical_profile.mean
            )
            bundle.numeric[f"{prefix}deviation_from_baseline"] = (
                canonical_profile.metadata.get("deviation_from_baseline")
            )
            bundle.numeric[f"{prefix}volatility"] = canonical_profile.volatility
            bundle.trends[key] = canonical_profile.trend.value

        bundle.sample_count += len(rows)

    # -- errors (§9) ----------------------------------------------------
    async def _add_errors(self, bundle: FeatureBundle) -> None:
        error_levels = [Severity.ERROR, Severity.FATAL]
        rows = (
            await self.session.execute(
                select(LogRecord.timestamp, LogRecord.message)
                .where(
                    *self._scope_clause(LogRecord, bundle),
                    LogRecord.timestamp >= bundle.window_start,
                    LogRecord.timestamp <= bundle.forecast_time,
                    LogRecord.level.in_(error_levels),
                )
                .order_by(LogRecord.timestamp.asc())
                .limit(settings.RELIABILITY_MAX_SAMPLES_PER_METRIC)
            )
        ).all()
        all_rows = (
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(LogRecord)
                    .where(
                        *self._scope_clause(LogRecord, bundle),
                        LogRecord.timestamp >= bundle.window_start,
                        LogRecord.timestamp <= bundle.forecast_time,
                    )
                )
            ).scalar()
        ) or 0
        bundle.sources["logs"] = int(all_rows)
        bundle.sources["error_logs"] = len(rows)
        newest_log = max((aware_utc(ts) for ts, _m in rows), default=None)
        if newest_log is not None:
            bundle.detail["logs_latest_at"] = newest_log.isoformat()
            _note_latest(bundle, newest_log)

        if not all_rows and not rows:
            bundle.quality_notes.append(
                "no log records were recorded for this scope in the window"
            )

        now = bundle.forecast_time
        windows = {"5m": 300, "15m": 900, "1h": 3600}
        for label, seconds in windows.items():
            since = now - timedelta(seconds=seconds)
            count = sum(1 for ts, _m in rows if aware_utc(ts) >= since)
            bundle.numeric[f"errors_last_{label}"] = float(count)

        bundle.numeric["error_log_count"] = float(len(rows))
        bundle.numeric["log_count"] = float(all_rows)
        bundle.numeric["error_log_ratio"] = stats.safe_ratio(
            float(len(rows)), float(all_rows)
        )

        #: Error rate change: newest third of the window vs oldest third. Two
        #: equal-length halves of the observed window — no data invented.
        third = max(
            int((bundle.forecast_time - bundle.window_start).total_seconds() / 3), 1
        )
        newest_cut = bundle.forecast_time - timedelta(seconds=third)
        oldest_cut = bundle.window_start + timedelta(seconds=third)
        newest = sum(1 for ts, _m in rows if aware_utc(ts) >= newest_cut)
        oldest = sum(1 for ts, _m in rows if aware_utc(ts) < oldest_cut)
        bundle.numeric["error_rate_change"] = stats.change_rate(
            float(newest), float(oldest)
        )
        bundle.detail["errors"] = {
            "newest_third_count": newest,
            "oldest_third_count": oldest,
            "third_seconds": third,
        }

        #: Distinct message prefixes stand in for error *types*: the platform
        #: has no error taxonomy, and a prefix is a measurement rather than a
        #: guess about intent.
        prefixes: dict[str, int] = {}
        for _ts, message in rows:
            prefix = (message or "")[:80]
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
        bundle.numeric["unique_error_types"] = float(len(prefixes))
        bundle.numeric["repeated_error_patterns"] = float(
            sum(1 for count in prefixes.values() if count > 1)
        )

        #: Burst frequency: minutes whose error count exceeds the window mean
        #: per-minute rate. A count, not a probability.
        minutes: dict[str, int] = {}
        for ts, _m in rows:
            key = aware_utc(ts).strftime("%Y-%m-%dT%H:%M")
            minutes[key] = minutes.get(key, 0) + 1
        total_minutes = max(
            int((bundle.forecast_time - bundle.window_start).total_seconds() // 60), 1
        )
        mean_per_minute = (len(rows) / total_minutes) if rows else 0.0
        bundle.numeric["error_burst_frequency"] = float(
            sum(1 for count in minutes.values() if count > mean_per_minute)
        )
        if prefixes:
            top_prefix, top_count = max(
                prefixes.items(), key=lambda item: (item[1], item[0])
            )
            bundle.detail["errors"]["dominant_pattern"] = top_prefix
            bundle.detail["errors"]["dominant_pattern_count"] = top_count

        bundle.sample_count += len(rows)

    # -- latency (§10) --------------------------------------------------
    async def _add_latency(self, bundle: FeatureBundle) -> None:
        #: Spans carry no ``environment_id`` of their own — they are scoped by
        #: project and component only (Phase 1). A component belongs to one
        #: environment, so the component filter already implies it.
        clause = [
            SpanRecord.start_time >= bundle.window_start,
            SpanRecord.start_time <= bundle.forecast_time,
        ]
        if bundle.project_id is not None:
            clause.append(SpanRecord.project_id == bundle.project_id)
        if bundle.component_id is not None:
            clause.append(SpanRecord.component_id == bundle.component_id)

        rows = (
            await self.session.execute(
                select(
                    SpanRecord.start_time,
                    SpanRecord.duration_ms,
                    SpanRecord.status,
                )
                .where(*clause)
                .order_by(SpanRecord.start_time.asc())
                .limit(settings.RELIABILITY_MAX_SAMPLES_PER_METRIC)
            )
        ).all()
        bundle.sources["spans"] = len(rows)
        if not rows:
            bundle.quality_notes.append(
                "no spans were recorded for this component in the window"
            )
            return

        durations = [float(d) for _ts, d, _s in rows if d is not None]
        failures = [
            1.0 if str(status.value) != "OK" else 0.0 for _ts, _d, status in rows
        ]
        profile = stats.profile_series(
            "span.duration_ms",
            durations,
            min_samples=settings.RELIABILITY_MIN_SAMPLES,
        )
        bundle.numeric["span_latency_p50"] = profile.p50
        bundle.numeric["span_latency_p95"] = profile.p95
        bundle.numeric["span_latency_p99"] = profile.p99
        bundle.numeric["span_latency_slope"] = profile.normalized_slope
        bundle.numeric["span_latency_volatility"] = profile.volatility
        bundle.trends["span_latency"] = profile.trend.value
        bundle.numeric["span_count"] = float(len(rows))
        bundle.numeric["span_failure_count"] = float(sum(failures))
        bundle.numeric["span_error_rate"] = (
            sum(failures) / len(failures) if failures else None
        )
        bundle.numeric["span_error_rate_trend"] = stats.normalized_slope(failures)

        #: Tail latency frequency: share of spans above twice the window median.
        #: A measured proportion with a stated rule, not a fitted percentile.
        if profile.median is not None and profile.median > 0:
            tail_cut = profile.median * 2.0
            tail = sum(1 for d in durations if d > tail_cut)
            bundle.numeric["tail_latency_frequency"] = tail / len(durations)
            bundle.detail["latency"] = {
                "tail_cut_ms": tail_cut,
                "tail_span_count": tail,
            }
        else:
            bundle.numeric["tail_latency_frequency"] = None
            bundle.detail["latency"] = {}

        bundle.detail.setdefault("latency", {})["duration_p50_ms"] = profile.p50
        bundle.detail.setdefault("latency", {})["duration_p95_ms"] = profile.p95
        newest_span = max((aware_utc(ts) for ts, _d, _s in rows), default=None)
        if newest_span is not None:
            bundle.detail["spans_latest_at"] = newest_span.isoformat()
            _note_latest(bundle, newest_span)
        bundle.sample_count += len(rows)

    # -- incidents (§11) ------------------------------------------------
    async def _add_incidents(self, bundle: FeatureBundle) -> None:
        scope = self._scope_clause(Incident, bundle)
        now = bundle.forecast_time
        buckets = {"24h": 1, "7d": 7, "30d": 30}
        counts: dict[str, int] = {}
        for label, days in buckets.items():
            since = now - timedelta(days=days)
            counts[label] = int(
                (
                    await self.session.execute(
                        select(func.count())
                        .select_from(Incident)
                        .where(
                            *scope,
                            Incident.detected_at >= since,
                            Incident.detected_at <= now,
                        )
                    )
                ).scalar()
                or 0
            )
        for label, count in counts.items():
            bundle.numeric[f"incidents_last_{label}"] = float(count)

        seven_ago = now - timedelta(days=7)
        previous_seven = now - timedelta(days=14)
        critical_last_7d = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Incident)
                    .where(
                        *scope,
                        Incident.detected_at >= seven_ago,
                        Incident.detected_at <= now,
                        Incident.severity.in_(
                            [IncidentSeverity.CRITICAL, IncidentSeverity.HIGH]
                        ),
                    )
                )
            ).scalar()
            or 0
        )
        previous_7d = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Incident)
                    .where(
                        *scope,
                        Incident.detected_at >= previous_seven,
                        Incident.detected_at < seven_ago,
                    )
                )
            ).scalar()
            or 0
        )
        bundle.numeric["critical_incidents_last_7d"] = float(critical_last_7d)
        #: Frequency trend compares two equal windows; a change over a zero
        #: baseline is reported as ``None`` rather than as infinite growth.
        bundle.numeric["incident_frequency_trend"] = stats.change_rate(
            float(counts["7d"]), float(previous_7d)
        )
        bundle.detail["incidents"] = {
            "last_7d": counts["7d"],
            "previous_7d": previous_7d,
            "critical_last_7d": critical_last_7d,
        }

        recent = (
            await self.session.execute(
                select(
                    Incident.detected_at,
                    Incident.resolved_at,
                    Incident.fingerprint,
                    Incident.status,
                )
                .where(*scope, Incident.detected_at <= now)
                .order_by(Incident.detected_at.desc())
                .limit(50)
            )
        ).all()
        bundle.sources["incidents"] = len(recent)

        last_detected = aware_utc(recent[0][0]) if recent and recent[0][0] else None
        bundle.numeric["time_since_last_incident_seconds"] = (
            (now - last_detected).total_seconds() if last_detected else None
        )

        resolutions = []
        for _detected, resolved, _fp, _status in recent:
            if resolved is not None and _detected is not None:
                delta = (aware_utc(resolved) - aware_utc(_detected)).total_seconds()
                if delta >= 0:
                    resolutions.append(delta)
        bundle.numeric["incident_resolution_time_average_seconds"] = (
            sum(resolutions) / len(resolutions) if resolutions else None
        )
        if len(resolutions) >= 4:
            half = len(resolutions) // 2
            #: ``recent`` is newest-first, so the first half is the newer set.
            newer = sum(resolutions[:half]) / half
            older = sum(resolutions[half:]) / (len(resolutions) - half)
            bundle.numeric["incident_resolution_time_trend"] = stats.change_rate(
                newer, older
            )
        else:
            bundle.numeric["incident_resolution_time_trend"] = None

        fingerprints = [fp for _d, _r, fp, _s in recent if fp]
        bundle.numeric["recurring_incident_count"] = float(
            len(fingerprints) - len(set(fingerprints))
        )
        open_incidents = sum(
            1
            for _d, _r, _fp, status in recent
            if status
            in (
                IncidentStatus.OPEN,
                IncidentStatus.ACKNOWLEDGED,
                IncidentStatus.INVESTIGATING,
            )
        )
        bundle.numeric["open_incident_count"] = float(open_incidents)

    # -- anomalies (§12) ------------------------------------------------
    async def _add_anomalies(self, bundle: FeatureBundle) -> None:
        scope = self._scope_clause(Anomaly, bundle)
        now = bundle.forecast_time
        for label, seconds in (("1h", 3600), ("6h", 21600), ("24h", 86400)):
            since = now - timedelta(seconds=seconds)
            count = int(
                (
                    await self.session.execute(
                        select(func.count())
                        .select_from(Anomaly)
                        .where(
                            *scope,
                            Anomaly.detected_at >= since,
                            Anomaly.detected_at <= now,
                        )
                    )
                ).scalar()
                or 0
            )
            bundle.numeric[f"anomalies_last_{label}"] = float(count)

        window_hours = max(
            (bundle.forecast_time - bundle.window_start).total_seconds() / 3600.0, 1e-6
        )
        in_window = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Anomaly)
                    .where(
                        *scope,
                        Anomaly.detected_at >= bundle.window_start,
                        Anomaly.detected_at <= now,
                    )
                )
            ).scalar()
            or 0
        )
        bundle.numeric["anomaly_frequency_per_hour"] = in_window / window_hours
        bundle.numeric["component_anomaly_density"] = in_window / window_hours

        day_ago = now - timedelta(days=1)
        severity_rows = (
            await self.session.execute(
                select(Anomaly.severity, func.count())
                .where(
                    *scope, Anomaly.detected_at >= day_ago, Anomaly.detected_at <= now
                )
                .group_by(Anomaly.severity)
            )
        ).all()
        distribution = {str(sev.value): int(count) for sev, count in severity_rows}
        bundle.detail["anomalies"] = {
            "severity_distribution_24h": distribution,
            "in_window": in_window,
        }
        for severity in _ANOMALY_SEVERITIES:
            bundle.numeric[f"anomaly_{severity.value.lower()}_count_24h"] = float(
                distribution.get(severity.value, 0)
            )

        occurred = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Anomaly)
                    .where(
                        *scope,
                        Anomaly.observation_count > 1,
                        Anomaly.detected_at <= now,
                    )
                )
            ).scalar()
            or 0
        )
        bundle.numeric["repeated_anomaly_patterns"] = float(occurred)
        bundle.sources["anomalies"] = in_window

        #: Anomaly *types* in the window. Stored because historical-similarity
        #: matching compares structured shape, and a severity histogram alone
        #: cannot tell a latency event from an error burst (§36).
        type_rows = (
            await self.session.execute(
                select(Anomaly.anomaly_type, func.count())
                .where(
                    *scope,
                    Anomaly.detected_at >= bundle.window_start,
                    Anomaly.detected_at <= now,
                )
                .group_by(Anomaly.anomaly_type)
            )
        ).all()
        bundle.detail["anomalies"]["types_in_window"] = {
            str(atype.value): int(count) for atype, count in type_rows
        }

    # -- dependencies (§13) --------------------------------------------
    async def _add_dependencies(self, bundle: FeatureBundle) -> None:
        if bundle.component_id is None:
            bundle.quality_notes.append(
                "no component scope, so dependency structure was not inspected"
            )
            return

        outgoing = (
            await self.session.execute(
                select(
                    ComponentDependency.target_component_id,
                    ComponentDependency.dependency_type,
                ).where(ComponentDependency.source_component_id == bundle.component_id)
            )
        ).all()
        incoming = (
            await self.session.execute(
                select(ComponentDependency.source_component_id).where(
                    ComponentDependency.target_component_id == bundle.component_id
                )
            )
        ).all()
        bundle.sources["dependencies"] = len(outgoing) + len(incoming)
        bundle.numeric["dependency_count"] = float(len(outgoing))
        bundle.numeric["upstream_dependency_count"] = float(len(incoming))
        bundle.numeric["critical_dependency_count"] = float(
            sum(1 for _t, dtype in outgoing if dtype in HARD_DEPENDENCY_TYPES)
        )

        target_ids = {tid for tid, _t in outgoing if tid is not None}
        #: A dependency is "degraded" when it has produced an anomaly or an
        #: incident in the last day. Structural association only (§14).
        degraded: set[Any] = set()
        if target_ids:
            day_ago = bundle.forecast_time - timedelta(days=1)
            bad_anomalies = (
                (
                    await self.session.execute(
                        select(Anomaly.component_id)
                        .where(
                            Anomaly.project_id == bundle.project_id,
                            Anomaly.component_id.in_(list(target_ids)),
                            Anomaly.detected_at >= day_ago,
                            Anomaly.detected_at <= bundle.forecast_time,
                        )
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
            bad_incidents = (
                (
                    await self.session.execute(
                        select(Incident.primary_component_id)
                        .where(
                            Incident.project_id == bundle.project_id,
                            Incident.primary_component_id.in_(list(target_ids)),
                            Incident.detected_at >= day_ago,
                            Incident.detected_at <= bundle.forecast_time,
                        )
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
            degraded = {cid for cid in (*bad_anomalies, *bad_incidents) if cid}

        bundle.numeric["degraded_dependency_count"] = float(len(degraded))
        bundle.numeric["dependency_failure_frequency"] = float(
            len(degraded) / len(target_ids) if target_ids else 0.0
        )
        #: Share of outgoing edges whose target is currently degraded. Named
        #: "impact" because it is structural reach, not observed propagation.
        bundle.numeric["downstream_impact"] = (
            float(len(degraded) / len(target_ids)) if target_ids else None
        )
        bundle.numeric["upstream_risk"] = float(
            len(degraded) / len(target_ids) if target_ids else 0.0
        )

        #: Mean latency trend across dependencies, read from their own metric
        #: series. An average of measured trends — never a causal claim.
        if target_ids:
            dep_metrics = (
                await self.session.execute(
                    select(
                        MetricRecord.metric_name,
                        MetricRecord.timestamp,
                        MetricRecord.value,
                    )
                    .where(
                        MetricRecord.project_id == bundle.project_id,
                        MetricRecord.component_id.in_(list(target_ids)),
                        MetricRecord.timestamp >= bundle.window_start,
                        MetricRecord.timestamp <= bundle.forecast_time,
                    )
                    .order_by(MetricRecord.timestamp.asc())
                    .limit(settings.RELIABILITY_MAX_SAMPLES_PER_METRIC)
                )
            ).all()
            grouped: dict[str, list[tuple[datetime, float]]] = {}
            for metric_name, timestamp, value in dep_metrics:
                key = canonical_metric_key(str(metric_name or ""))
                if (
                    key in ("latency_p95", "latency_p99", "latency_p50")
                    and value is not None
                ):
                    grouped.setdefault(key, []).append(
                        (aware_utc(timestamp), float(value))
                    )
            #: Hourly, so a dependency scraped less often is not read as calmer.
            trend_values = [
                stats.slope_per_hour(
                    [value for _ts, value in pairs], [ts for ts, _value in pairs]
                )
                for pairs in grouped.values()
            ]
            usable = [v for v in trend_values if v is not None]
            bundle.numeric["dependency_latency_trend"] = (
                sum(usable) / len(usable) if usable else None
            )
        else:
            bundle.numeric["dependency_latency_trend"] = None

        bundle.numeric["dependency_depth"] = float(
            await self._dependency_depth(bundle.component_id)
        )
        bundle.detail["dependencies"] = {
            "outgoing": len(outgoing),
            "incoming": len(incoming),
            "degraded_targets": len(degraded),
        }

    async def _dependency_depth(self, component_id: Any) -> int:
        """Bounded BFS over ``ComponentDependency`` (§13).

        Depth of the dependency chain, capped at
        ``RELIABILITY_MAX_DEPENDENCY_HOPS`` so a cyclic graph cannot loop and a
        pathological one cannot explode. Breadth, not reachability count.
        """
        max_hops = settings.RELIABILITY_MAX_DEPENDENCY_HOPS
        seen = {component_id}
        frontier = [component_id]
        depth = 0
        while frontier and depth < max_hops:
            children = (
                (
                    await self.session.execute(
                        select(ComponentDependency.target_component_id).where(
                            ComponentDependency.source_component_id.in_(frontier)
                        )
                    )
                )
                .scalars()
                .all()
            )
            next_frontier = [
                child for child in children if child is not None and child not in seen
            ]
            if not next_frontier:
                break
            seen.update(next_frontier)
            frontier = next_frontier
            depth += 1
        return depth

    # -- deployments (§14) ---------------------------------------------
    async def _add_deployments(self, bundle: FeatureBundle) -> None:
        scope = self._scope_clause(DeploymentEvent, bundle)
        now = bundle.forecast_time
        for label, seconds in (("24h", 86400), ("7d", 604800)):
            since = now - timedelta(seconds=seconds)
            count = int(
                (
                    await self.session.execute(
                        select(func.count())
                        .select_from(DeploymentEvent)
                        .where(
                            *scope,
                            DeploymentEvent.deployed_at >= since,
                            DeploymentEvent.deployed_at <= now,
                        )
                    )
                ).scalar()
                or 0
            )
            bundle.numeric[f"deployments_last_{label}"] = float(count)

        recent = (
            await self.session.execute(
                select(
                    DeploymentEvent.deployed_at,
                    DeploymentEvent.status,
                    DeploymentEvent.metadata_,
                )
                .where(*scope, DeploymentEvent.deployed_at <= now)
                .order_by(DeploymentEvent.deployed_at.desc())
                .limit(50)
            )
        ).all()
        bundle.sources["deployments"] = len(recent)
        bundle.numeric["deployment_count_observed"] = float(len(recent))
        bundle.numeric["deployment_frequency_per_day"] = (
            len(recent) / 30.0 if recent else 0.0
        )

        last_deployed = aware_utc(recent[0][0]) if recent and recent[0][0] else None
        bundle.numeric["recent_deployment_age_seconds"] = (
            (now - last_deployed).total_seconds() if last_deployed else None
        )

        failed = sum(
            1 for _ts, status, _m in recent if status is DeploymentStatus.FAILED
        )
        rolled_back = sum(
            1 for _ts, status, _m in recent if status is DeploymentStatus.ROLLED_BACK
        )
        bundle.numeric["deployment_failure_frequency"] = (
            failed / len(recent) if recent else 0.0
        )
        bundle.numeric["rollback_frequency"] = (
            rolled_back / len(recent) if recent else 0.0
        )

        #: Size signals come from deployment metadata where a pipeline supplied
        #: them. Absent means absent — never defaulted to a plausible number.
        files_changed = _sum_metadata(recent, ("files_changed", "file_count"))
        lines_added = _sum_metadata(recent, ("lines_added", "additions"))
        lines_removed = _sum_metadata(recent, ("lines_removed", "deletions"))
        services_changed = _sum_metadata(recent, ("services_changed",))
        bundle.numeric["deployment_files_changed"] = files_changed
        bundle.numeric["deployment_lines_added"] = lines_added
        bundle.numeric["deployment_lines_removed"] = lines_removed
        bundle.numeric["deployment_services_changed"] = services_changed
        bundle.numeric["deployment_size"] = (
            None
            if lines_added is None and lines_removed is None
            else (lines_added or 0.0) + (lines_removed or 0.0)
        )
        bundle.detail["deployments"] = {
            "failed": failed,
            "rolled_back": rolled_back,
            "observed": len(recent),
        }

    # -- code (§15) -----------------------------------------------------
    async def _add_code(self, bundle: FeatureBundle) -> None:
        since = bundle.forecast_time - timedelta(days=settings.CODE_RECENT_CHANGE_DAYS)
        rows = (
            (
                (
                    await self.session.execute(
                        select(CodeRiskSignal.signal_type, func.count())
                        .where(
                            CodeRiskSignal.project_id == bundle.project_id,
                            CodeRiskSignal.observed_at >= since,
                            CodeRiskSignal.observed_at <= bundle.forecast_time,
                        )
                        .group_by(CodeRiskSignal.signal_type)
                    )
                ).all()
            )
            if bundle.project_id is not None
            else []
        )
        bundle.sources["code_signals"] = sum(int(count) for _t, count in rows)
        by_type = {str(stype.value): int(count) for stype, count in rows}
        bundle.detail["code"] = {"signals_by_type": by_type}

        bundle.numeric["recent_code_changes"] = float(
            by_type.get(RiskSignalType.RECENTLY_MODIFIED.value, 0)
            + by_type.get(RiskSignalType.FREQUENTLY_CHANGED.value, 0)
        )
        bundle.numeric["recent_code_churn"] = float(
            by_type.get(RiskSignalType.FREQUENTLY_CHANGED.value, 0)
        )
        bundle.numeric["high_risk_change_count"] = float(
            by_type.get(RiskSignalType.ERROR_PRONE_PATH.value, 0)
            + by_type.get(RiskSignalType.FREQUENTLY_FAILING.value, 0)
        )
        bundle.numeric["recently_modified_components"] = float(
            1 if by_type.get(RiskSignalType.RECENTLY_MODIFIED.value) else 0
        )

        snapshots = 0
        if bundle.project_id is not None:
            snapshots = int(
                (
                    await self.session.execute(
                        select(func.count())
                        .select_from(RepositorySnapshot)
                        .where(
                            RepositorySnapshot.project_id == bundle.project_id,
                            RepositorySnapshot.created_at >= since,
                            RepositorySnapshot.created_at <= bundle.forecast_time,
                        )
                    )
                ).scalar()
                or 0
            )
        bundle.numeric["recent_repository_snapshots"] = float(snapshots)

        #: Phase 7 verification outcomes, scoped to this component's incidents
        #: when a component is known. Fix counters are predictive features, not
        #: a statement about code quality (§15).
        if bundle.project_id is None:
            bundle.numeric["verified_fix_count"] = None
            bundle.numeric["failed_patch_count"] = None
        else:
            patch_scope = [Patch.project_id == bundle.project_id]
            verified = int(
                (
                    await self.session.execute(
                        select(func.count())
                        .select_from(Patch)
                        .where(
                            *patch_scope,
                            Patch.status == PatchStatus.VERIFIED,
                            Patch.created_at <= bundle.forecast_time,
                        )
                    )
                ).scalar()
                or 0
            )
            failed = int(
                (
                    await self.session.execute(
                        select(func.count())
                        .select_from(Patch)
                        .where(
                            *patch_scope,
                            Patch.status.in_(
                                [
                                    PatchStatus.VALIDATION_FAILED,
                                    PatchStatus.BUILD_FAILED,
                                    PatchStatus.TEST_FAILED,
                                    PatchStatus.REPRODUCTION_FAILED,
                                    PatchStatus.PARSE_FAILED,
                                ]
                            ),
                            Patch.created_at <= bundle.forecast_time,
                        )
                    )
                ).scalar()
                or 0
            )
            bundle.numeric["verified_fix_count"] = float(verified)
            bundle.numeric["failed_patch_count"] = float(failed)

        bundle.numeric["regression_count"] = float(
            by_type.get(RiskSignalType.FREQUENTLY_FAILING.value, 0)
        )
        bundle.numeric["recent_regression_signals"] = float(
            by_type.get(RiskSignalType.ERROR_PRONE_PATH.value, 0)
        )

    # -- resource pressure (§16) ---------------------------------------
    def _add_resource_pressure(self, bundle: FeatureBundle) -> None:
        """Derive resource pressure from the canonical metric features.

        Pure derivation from numbers already in the bundle — no extra query, so
        the resource view can never disagree with the metric view.
        """
        ceilings = {
            "cpu_utilization": settings.RELIABILITY_CPU_CEILING_PERCENT,
            "memory_utilization": settings.RELIABILITY_MEMORY_CEILING_PERCENT,
            "disk_utilization": settings.RELIABILITY_DISK_CEILING_PERCENT,
            "queue_depth": settings.RELIABILITY_QUEUE_CEILING,
            "connection_pool_usage": settings.RELIABILITY_CONNECTION_CEILING,
        }
        saturation_ratio = settings.RELIABILITY_SATURATION_RATIO
        evaluated = 0
        saturated = 0
        for metric, ceiling in ceilings.items():
            slope = bundle.numeric.get(f"{metric}_slope")
            bundle.numeric[f"{metric}_trend"] = slope
            current = bundle.numeric.get(f"{metric}_current")
            is_sat = stats.is_saturated(
                current, ceiling=ceiling, saturation_ratio=saturation_ratio
            )
            bundle.numeric[f"{metric}_saturated"] = 1.0 if is_sat else 0.0
            if current is not None and ceiling is not None:
                evaluated += 1
                if is_sat:
                    saturated += 1
        bundle.numeric["resource_metrics_evaluated"] = float(evaluated)
        bundle.numeric["resource_saturated_count"] = float(saturated)
        bundle.numeric["resource_saturation_rate"] = (
            saturated / evaluated if evaluated else None
        )
        bundle.detail["resources"] = {
            "saturation_ratio": saturation_ratio,
            "ceilings": {k: v for k, v in ceilings.items() if v is not None},
            "evaluated": evaluated,
            "saturated": saturated,
        }

    # -- data quality (§18) --------------------------------------------
    def _assess_quality(self, bundle: FeatureBundle) -> None:
        """Compute coverage and the data-quality verdict, with reasons.

        Coverage is the share of *telemetry* families (metrics, errors,
        latency) that produced any usable row — the families a prediction
        depends on. The other families enrich a forecast, so their absence is
        recorded as a note rather than treated as an ingestion failure.

        Staleness demotes the verdict: telemetry that stopped arriving an hour
        before the forecast is not evidence of stability (§42).
        """
        present = 0
        for family, source_key in (
            ("metrics", "metrics"),
            ("errors", "logs"),
            ("latency", "spans"),
        ):
            count = bundle.sources.get(source_key, 0)
            if family == "errors":
                #: Log rows feed the error features; the family counts as
                #: present when any log was recorded at all.
                count = bundle.sources.get("logs", 0)
            if count:
                present += 1

        bundle.coverage = present / len(TELEMETRY_FAMILIES)
        bundle.sources["telemetry_families_present"] = present
        bundle.sources["telemetry_families_expected"] = len(TELEMETRY_FAMILIES)

        metric_samples = int(bundle.numeric.get("metric_sample_count") or 0)
        if present == 0:
            bundle.quality = ForecastDataQuality.INSUFFICIENT
            bundle.quality_notes.append(
                "no telemetry (metrics, logs or spans) exists before the "
                "forecast time for this scope"
            )
        elif bundle.coverage < settings.RELIABILITY_MIN_COVERAGE:
            bundle.quality = ForecastDataQuality.POOR
            bundle.quality_notes.append(
                f"only {present} of {len(TELEMETRY_FAMILIES)} telemetry families "
                "produced data in the window"
            )
        elif bundle.coverage < settings.RELIABILITY_GOOD_COVERAGE:
            bundle.quality = ForecastDataQuality.PARTIAL
        else:
            bundle.quality = ForecastDataQuality.GOOD

        if 0 < metric_samples < settings.RELIABILITY_MIN_SAMPLES:
            #: Too few observations to form a baseline is *not* evidence of
            #: calm: a single quiet sample would otherwise read as LOW risk,
            #: which is the false comfort §18 and §69 forbid. Below the
            #: configured floor the verdict is INSUFFICIENT, and the predictor
            #: refuses to score. The floor is configurable
            #: (``RELIABILITY_MIN_SAMPLES``) so an operator can lower it rather
            #: than have ARGUS guess.
            bundle.quality = ForecastDataQuality.INSUFFICIENT
            bundle.quality_notes.append(
                f"only {metric_samples} metric samples were observed in the "
                f"window (minimum {settings.RELIABILITY_MIN_SAMPLES} needed to "
                "form a baseline), so no risk score can be justified"
            )
        if bundle.quality is ForecastDataQuality.GOOD and not bundle.sample_count:
            bundle.quality = ForecastDataQuality.PARTIAL
            bundle.quality_notes.append("the window produced no usable feature samples")

        telemetry_events = sum(
            bundle.sources.get(key, 0) for key in ("metrics", "logs", "spans")
        )
        if telemetry_events:
            bundle.detail["telemetry_event_count"] = telemetry_events

        self._assess_staleness(bundle)

    def _assess_staleness(self, bundle: FeatureBundle) -> None:
        """Detect telemetry that stopped arriving before the forecast (§18, §42).

        A pipeline that broke two hours ago produces a window of *no rows*,
        which is indistinguishable from a quiet system unless the age of the
        newest sample is checked explicitly. Staleness therefore demotes the
        verdict rather than being reported as a footnote: a forecast built on
        six-hour-old telemetry is not a forecast about now.
        """
        newest = bundle.latest_telemetry_at
        if newest is None:
            bundle.numeric["telemetry_staleness_seconds"] = None
            bundle.numeric["telemetry_is_stale"] = None
            return

        staleness = (bundle.forecast_time - newest).total_seconds()
        bundle.detail["telemetry_latest_at"] = newest.isoformat()
        bundle.numeric["telemetry_staleness_seconds"] = staleness
        threshold = settings.RELIABILITY_STALE_TELEMETRY_SECONDS
        is_stale = staleness > threshold
        bundle.numeric["telemetry_is_stale"] = 1.0 if is_stale else 0.0
        if not is_stale:
            return

        bundle.quality_notes.append(
            f"the newest telemetry is {staleness / 60:.0f} minutes older than the "
            f"forecast time (stale beyond {threshold / 60:.0f} minutes), so the "
            "window may reflect a broken pipeline rather than a quiet system"
        )
        if bundle.quality in (
            ForecastDataQuality.GOOD,
            ForecastDataQuality.PARTIAL,
        ):
            bundle.quality = ForecastDataQuality.POOR

    # -- helpers --------------------------------------------------------
    def _scope_clause(self, model: Any, bundle: FeatureBundle) -> list[Any]:
        """Project/environment/component scope for one model.

        Environment and component filters are applied only when a scope was
        requested, so a project-wide forecast does not silently mix
        environments. Component-scoped models require the column to exist —
        every model used here has ``component_id``.
        """
        clauses: list[Any] = []
        if bundle.project_id is not None:
            clauses.append(model.project_id == bundle.project_id)
        if bundle.environment_id is not None and hasattr(model, "environment_id"):
            clauses.append(model.environment_id == bundle.environment_id)
        if bundle.component_id is not None and hasattr(model, "component_id"):
            clauses.append(model.component_id == bundle.component_id)
        if not clauses:
            raise ValueError(
                "a project scope is required to build reliability features"
            )
        return clauses


def _note_latest(bundle: FeatureBundle, moment: datetime) -> None:
    """Track the newest telemetry instant across every family."""
    if bundle.latest_telemetry_at is None or moment > bundle.latest_telemetry_at:
        bundle.latest_telemetry_at = moment


def _with_baseline(
    profile: stats.SeriesProfile, baseline_mean: Optional[float]
) -> stats.SeriesProfile:
    """Attach the baseline deviation to a profile's metadata.

    ``SeriesProfile`` is frozen, so the derived value is carried in its
    ``metadata`` map rather than mutating the profile — the same object then
    still hashes and compares consistently in tests.
    """
    return stats.SeriesProfile(
        metric_name=profile.metric_name,
        sample_count=profile.sample_count,
        current=profile.current,
        mean=profile.mean,
        median=profile.median,
        stddev=profile.stddev,
        minimum=profile.minimum,
        maximum=profile.maximum,
        p50=profile.p50,
        p95=profile.p95,
        p99=profile.p99,
        slope=profile.slope,
        normalized_slope=profile.normalized_slope,
        volatility=profile.volatility,
        trend=profile.trend,
        sample_sufficient=profile.sample_sufficient,
        metadata={
            **profile.metadata,
            "baseline_mean": baseline_mean,
            "deviation_from_baseline": stats.change_rate(profile.mean, baseline_mean),
        },
    )


def _sum_metadata(rows: Sequence[Any], keys: Sequence[str]) -> Optional[float]:
    """Sum a numeric field across deployment rows' metadata.

    ``None`` when no row carried the field at all — an absent signal stays
    absent rather than becoming zero (§14).
    """
    total = 0.0
    found = False
    for _ts, _status, meta in rows:
        if not isinstance(meta, dict):
            continue
        for key in keys:
            value = meta.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total += float(value)
                found = True
                break
    return total if found else None


def combine_clauses(*clauses: Any) -> Any:
    """Convenience ``AND`` over optional clause lists (used by tests/callers)."""
    flat = [clause for clause in clauses if clause is not None]
    if not flat:
        return True
    return and_(True, *flat)


def any_of(*clauses: Any) -> Any:
    """``OR`` helper mirroring :func:`combine_clauses`."""
    flat = [clause for clause in clauses if clause is not None]
    if not flat:
        return False
    return or_(*flat)


__all__ = [
    "CANONICAL_METRICS",
    "FEATURE_SCHEMA_VERSION",
    "HARD_DEPENDENCY_TYPES",
    "STAT_SUFFIXES",
    "TELEMETRY_FAMILIES",
    "FeatureBundle",
    "ReliabilityFeatureEngine",
    "any_of",
    "aware_utc",
    "canonical_metric_key",
    "combine_clauses",
]
