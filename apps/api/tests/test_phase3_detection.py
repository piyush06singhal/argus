"""Phase 3 — anomaly rule engine tests (§18–§19, §41).

Exercises the engine end to end against persisted telemetry: firing, dedup,
cooldown, persistence gating, suppression recording, and project isolation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.anomaly import (
    Anomaly,
    AnomalyBaseline,
    AnomalyFingerprint,
    AnomalyObservation,
    AnomalyRule,
    AnomalySeverity,
    AnomalyStatus,
    AnomalySuppression,
    AnomalyType,
    MaintenanceWindow,
    RuleCondition,
)
from app.models.ingestion import HealthCheckEvent, HealthStatus
from app.models.observability import (
    LogRecord,
    MetricRecord,
    MetricType,
    Severity,
    TraceRecord,
    TraceStatus,
)
from app.models.project import Environment, SoftwareProject
from app.models.system import SystemComponent
from app.services.anomaly_detection import AnomalyDetectionService

NOW = datetime(2026, 9, 19, 14, 30, tzinfo=timezone.utc)


async def _seed(db_session: AsyncSession):
    project = SoftwareProject(name="Detect Proj", slug=f"detect-{uuid.uuid4().hex[:8]}")
    db_session.add(project)
    await db_session.flush()
    env = Environment(
        project_id=project.id, name="production", environment_type="PRODUCTION"
    )
    component = SystemComponent(
        project_id=project.id, component_type="SERVICE", name="Checkout"
    )
    db_session.add_all([env, component])
    await db_session.flush()
    return project.id, env.id, component.id


async def _add_metric(
    db_session: AsyncSession,
    project_id,
    env_id,
    component_id,
    name: str,
    value: float,
    at: datetime,
) -> None:
    db_session.add(
        MetricRecord(
            project_id=project_id,
            environment_id=env_id,
            component_id=component_id,
            timestamp=at,
            metric_name=name,
            metric_type=MetricType.GAUGE,
            value=value,
        )
    )
    await db_session.flush()


def _rule(project_id, env_id, component_id, **overrides) -> AnomalyRule:
    payload = {
        "project_id": project_id,
        "environment_id": env_id,
        "component_id": component_id,
        "name": "latency p95",
        "anomaly_type": AnomalyType.LATENCY_SPIKE,
        "condition": RuleCondition.THRESHOLD,
        "metric_name": "http.checkout.latency.p95",
        "threshold": 500.0,
        "severity": AnomalySeverity.HIGH,
        "window_seconds": 300,
        "min_samples": 2,
        "cooldown_seconds": 0,
        "persistence_cycles": 1,
    }
    payload.update(overrides)
    return AnomalyRule(**payload)


async def _count_anomalies(db_session: AsyncSession) -> int:
    return await db_session.scalar(select(func.count()).select_from(Anomaly))


class TestThresholdDetection:
    async def test_fires_and_persists(self, db_session: AsyncSession) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(_rule(project_id, env_id, component_id))
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            900.0,
            NOW,
        )
        await db_session.flush()

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        assert result.rules_evaluated == 1
        assert result.anomalies_opened == 1

        anomaly = await db_session.scalar(select(Anomaly))
        assert anomaly is not None
        assert anomaly.anomaly_type is AnomalyType.LATENCY_SPIKE
        assert anomaly.status is AnomalyStatus.DETECTED
        assert anomaly.observed_value == 900.0
        assert anomaly.severity is AnomalySeverity.HIGH
        # Explainability is persisted, not recomputed later.
        assert anomaly.metadata_["severity_reasons"]
        assert anomaly.metadata_["detector_reason"]

    async def test_does_not_fire_below_threshold(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(_rule(project_id, env_id, component_id))
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            100.0,
            NOW,
        )
        await db_session.flush()

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        assert result.anomalies_opened == 0
        assert await _count_anomalies(db_session) == 0

    async def test_no_telemetry_no_anomaly(self, db_session: AsyncSession) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(_rule(project_id, env_id, component_id))
        await db_session.flush()

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        assert result.rules_evaluated == 1
        assert result.anomalies_opened == 0


class TestDeduplication:
    async def test_repeated_run_does_not_duplicate(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(_rule(project_id, env_id, component_id))
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            900.0,
            NOW,
        )
        await db_session.flush()

        service = AnomalyDetectionService(db_session, now=NOW)
        await service.run(project_id=project_id, environment_id=env_id)
        await service.run(project_id=project_id, environment_id=env_id)
        assert await _count_anomalies(db_session) == 1

    async def test_new_observation_extends_same_anomaly(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(_rule(project_id, env_id, component_id))
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            900.0,
            NOW,
        )
        await db_session.flush()
        await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )

        # A genuinely newer sample is a new cycle → a second observation.
        later = NOW + timedelta(seconds=30)
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            950.0,
            later,
        )
        await db_session.flush()
        await AnomalyDetectionService(db_session, now=later).run(
            project_id=project_id, environment_id=env_id
        )

        assert await _count_anomalies(db_session) == 1
        anomaly = await db_session.scalar(select(Anomaly))
        assert anomaly is not None
        assert anomaly.observation_count == 2
        observations = await db_session.scalar(
            select(func.count()).select_from(AnomalyObservation)
        )
        assert observations == 2

    async def test_cooldown_suppresses_extra_observations(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(_rule(project_id, env_id, component_id, cooldown_seconds=600))
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            900.0,
            NOW,
        )
        await db_session.flush()
        await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )

        later = NOW + timedelta(seconds=30)  # inside the 600s cooldown
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            950.0,
            later,
        )
        await db_session.flush()
        await AnomalyDetectionService(db_session, now=later).run(
            project_id=project_id, environment_id=env_id
        )

        observations = await db_session.scalar(
            select(func.count()).select_from(AnomalyObservation)
        )
        assert observations == 1  # last_seen updated, no flood

    async def test_baseline_rows_are_recorded_for_audit(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(
            _rule(
                project_id,
                env_id,
                component_id,
                condition=RuleCondition.BASELINE_DEVIATION,
                multiplier=1.5,
                threshold=None,
            )
        )
        for i, value in enumerate([100.0, 105.0, 110.0, 900.0]):
            await _add_metric(
                db_session,
                project_id,
                env_id,
                component_id,
                "http.checkout.latency.p95",
                value,
                NOW - timedelta(seconds=10 * (3 - i)),
            )
        await db_session.flush()

        await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        baselines = await db_session.scalar(
            select(func.count()).select_from(AnomalyBaseline)
        )
        assert baselines >= 1


class TestPersistenceCycles:
    async def test_requires_multiple_cycles(self, db_session: AsyncSession) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(_rule(project_id, env_id, component_id, persistence_cycles=2))
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            900.0,
            NOW,
        )
        await db_session.flush()

        # First cycle: held, not opened.
        await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        assert await _count_anomalies(db_session) == 0

        # Second cycle with newer telemetry: opened.
        later = NOW + timedelta(seconds=20)
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            910.0,
            later,
        )
        await db_session.flush()
        await AnomalyDetectionService(db_session, now=later).run(
            project_id=project_id, environment_id=env_id
        )
        assert await _count_anomalies(db_session) == 1

    async def test_unchanged_telemetry_is_not_a_new_cycle(
        self, db_session: AsyncSession
    ) -> None:
        """Re-sweeping identical data must not accumulate persistence cycles."""
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(_rule(project_id, env_id, component_id, persistence_cycles=2))
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            900.0,
            NOW,
        )
        await db_session.flush()

        for _ in range(4):
            await AnomalyDetectionService(db_session, now=NOW).run(
                project_id=project_id, environment_id=env_id
            )
        assert await _count_anomalies(db_session) == 0


class TestSuppression:
    async def test_suppression_rule_records_reason(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(_rule(project_id, env_id, component_id))
        db_session.add(
            AnomalySuppression(
                project_id=project_id,
                environment_id=env_id,
                anomaly_type=AnomalyType.LATENCY_SPIKE,
                reason="known noisy metric",
                starts_at=NOW - timedelta(hours=1),
            )
        )
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            900.0,
            NOW,
        )
        await db_session.flush()

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        assert result.anomalies_opened == 1
        assert result.suppressed == 1
        anomaly = await db_session.scalar(select(Anomaly))
        assert anomaly is not None
        assert anomaly.suppressed is True
        assert "known noisy metric" in (anomaly.suppression_reason or "")

    async def test_maintenance_window_records_reason(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(_rule(project_id, env_id, component_id))
        db_session.add(
            MaintenanceWindow(
                project_id=project_id,
                environment_id=env_id,
                name="DB maintenance",
                starts_at=NOW - timedelta(minutes=10),
                ends_at=NOW + timedelta(minutes=50),
            )
        )
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            900.0,
            NOW,
        )
        await db_session.flush()

        await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        anomaly = await db_session.scalar(select(Anomaly))
        assert anomaly is not None
        assert anomaly.suppressed is True
        assert "DB maintenance" in (anomaly.suppression_reason or "")

    async def test_expired_suppression_does_not_apply(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(_rule(project_id, env_id, component_id))
        db_session.add(
            AnomalySuppression(
                project_id=project_id,
                reason="old window",
                starts_at=NOW - timedelta(hours=5),
                ends_at=NOW - timedelta(hours=1),
            )
        )
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            900.0,
            NOW,
        )
        await db_session.flush()

        await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        anomaly = await db_session.scalar(select(Anomaly))
        assert anomaly is not None
        assert anomaly.suppressed is False


class TestInsufficientBaseline:
    async def test_never_manufactures_an_anomaly(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(
            _rule(
                project_id,
                env_id,
                component_id,
                condition=RuleCondition.BASELINE_DEVIATION,
                multiplier=1.5,
                threshold=None,
                min_samples=10,
            )
        )
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            9999.0,
            NOW,
        )
        await db_session.flush()

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        assert result.anomalies_opened == 0
        assert result.insufficient_baselines >= 1


class TestLogAndTraceDetection:
    async def test_error_rate_from_logs(self, db_session: AsyncSession) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(
            _rule(
                project_id,
                env_id,
                component_id,
                name="checkout error rate",
                anomaly_type=AnomalyType.ERROR_RATE_SPIKE,
                condition=RuleCondition.ERROR_RATE,
                metric_name=None,
                threshold=0.05,
            )
        )
        for i in range(10):
            db_session.add(
                LogRecord(
                    project_id=project_id,
                    environment_id=env_id,
                    component_id=component_id,
                    timestamp=NOW - timedelta(seconds=i),
                    level=Severity.ERROR if i < 5 else Severity.INFO,
                    message="boom",
                )
            )
        await db_session.flush()

        await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        anomaly = await db_session.scalar(select(Anomaly))
        assert anomaly is not None
        assert anomaly.anomaly_type is AnomalyType.ERROR_RATE_SPIKE
        assert anomaly.observed_value == pytest.approx(0.5)

    async def test_log_pattern_spike(self, db_session: AsyncSession) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(
            _rule(
                project_id,
                env_id,
                component_id,
                name="db timeout pattern",
                anomaly_type=AnomalyType.LOG_PATTERN_SPIKE,
                condition=RuleCondition.PATTERN_SPIKE,
                metric_name=None,
                multiplier=2.0,
                min_samples=2,
                window_seconds=300,
            )
        )
        # Four baseline slices with one occurrence, last slice spiking.
        for i in range(4):
            db_session.add(
                LogRecord(
                    project_id=project_id,
                    environment_id=env_id,
                    component_id=component_id,
                    timestamp=NOW - timedelta(seconds=250 - i * 60),
                    level=Severity.ERROR,
                    message=f"ERROR database timeout user_id={i}",
                )
            )
        for i in range(10):
            db_session.add(
                LogRecord(
                    project_id=project_id,
                    environment_id=env_id,
                    component_id=component_id,
                    timestamp=NOW - timedelta(seconds=5 + i),
                    level=Severity.ERROR,
                    message=f"ERROR database timeout user_id={100 + i}",
                )
            )
        await db_session.flush()

        await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        anomaly = await db_session.scalar(select(Anomaly))
        assert anomaly is not None
        assert anomaly.anomaly_type is AnomalyType.LOG_PATTERN_SPIKE
        assert anomaly.pattern_template is not None
        assert "<num>" in anomaly.pattern_template

    async def test_trace_failure_rate(self, db_session: AsyncSession) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(
            _rule(
                project_id,
                env_id,
                component_id,
                name="trace failures",
                anomaly_type=AnomalyType.TRACE_FAILURE_SPIKE,
                condition=RuleCondition.TRACE_FAILURE_RATE,
                metric_name=None,
                threshold=0.05,
            )
        )
        for i in range(10):
            db_session.add(
                TraceRecord(
                    project_id=project_id,
                    environment_id=env_id,
                    trace_id=f"trace-{i}",
                    start_time=NOW - timedelta(seconds=i),
                    status=TraceStatus.ERROR if i < 3 else TraceStatus.OK,
                )
            )
        await db_session.flush()

        await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        anomaly = await db_session.scalar(select(Anomaly))
        assert anomaly is not None
        assert anomaly.anomaly_type is AnomalyType.TRACE_FAILURE_SPIKE
        assert anomaly.observed_value == pytest.approx(0.3)


class TestHealthDetection:
    async def test_degradation_fires(self, db_session: AsyncSession) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(
            _rule(
                project_id,
                env_id,
                component_id,
                name="health",
                anomaly_type=AnomalyType.HEALTH_DEGRADATION,
                condition=RuleCondition.HEALTH_TRANSITION,
                metric_name=None,
            )
        )
        db_session.add_all(
            [
                HealthCheckEvent(
                    project_id=project_id,
                    environment_id=env_id,
                    component_id=component_id,
                    timestamp=NOW - timedelta(minutes=1),
                    status=HealthStatus.HEALTHY,
                ),
                HealthCheckEvent(
                    project_id=project_id,
                    environment_id=env_id,
                    component_id=component_id,
                    timestamp=NOW,
                    status=HealthStatus.DEGRADED,
                ),
            ]
        )
        await db_session.flush()

        await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        anomaly = await db_session.scalar(select(Anomaly))
        assert anomaly is not None
        assert anomaly.anomaly_type is AnomalyType.HEALTH_DEGRADATION

    async def test_repeated_state_does_not_refire(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(
            _rule(
                project_id,
                env_id,
                component_id,
                name="health",
                anomaly_type=AnomalyType.HEALTH_DEGRADATION,
                condition=RuleCondition.HEALTH_TRANSITION,
                metric_name=None,
            )
        )
        db_session.add_all(
            [
                HealthCheckEvent(
                    project_id=project_id,
                    environment_id=env_id,
                    component_id=component_id,
                    timestamp=NOW - timedelta(minutes=2),
                    status=HealthStatus.DEGRADED,
                ),
                HealthCheckEvent(
                    project_id=project_id,
                    environment_id=env_id,
                    component_id=component_id,
                    timestamp=NOW - timedelta(minutes=1),
                    status=HealthStatus.DEGRADED,
                ),
            ]
        )
        await db_session.flush()

        await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        assert await _count_anomalies(db_session) == 0


class TestIsolation:
    async def test_project_scope_is_enforced(self, db_session: AsyncSession) -> None:
        project_a, env_a, comp_a = await _seed(db_session)
        project_b, env_b, comp_b = await _seed(db_session)
        db_session.add(_rule(project_a, env_a, comp_a))
        # Telemetry belongs to project B only.
        await _add_metric(
            db_session,
            project_b,
            env_b,
            comp_b,
            "http.checkout.latency.p95",
            900.0,
            NOW,
        )
        await db_session.flush()

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_a, environment_id=env_a
        )
        assert result.anomalies_opened == 0

    async def test_environment_scope_is_enforced(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        other_env = Environment(
            project_id=project_id, name="staging", environment_type="STAGING"
        )
        db_session.add(other_env)
        await db_session.flush()
        db_session.add(_rule(project_id, env_id, component_id))
        await _add_metric(
            db_session,
            project_id,
            other_env.id,
            component_id,
            "http.checkout.latency.p95",
            900.0,
            NOW,
        )
        await db_session.flush()

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        assert result.anomalies_opened == 0

    async def test_disabled_rule_is_skipped(self, db_session: AsyncSession) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(_rule(project_id, env_id, component_id, enabled=False))
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            900.0,
            NOW,
        )
        await db_session.flush()

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        assert result.rules_evaluated == 0


class TestDeterminism:
    async def test_same_inputs_same_fingerprint(self, db_session: AsyncSession) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(_rule(project_id, env_id, component_id))
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            900.0,
            NOW,
        )
        await db_session.flush()
        await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        first = await db_session.scalar(select(Anomaly))

        # Re-detecting the identical situation yields the identical key.
        await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=env_id
        )
        second = await db_session.scalar(select(Anomaly))
        assert first is not None and second is not None
        assert first.fingerprint == second.fingerprint


class TestExactScopeIsolation:
    """Scope is exact, in both directions.

    ``environment_id=None`` means "environment-less telemetry", never "every
    environment". These three tests pin that: a project-scope pass must not read
    environment-scoped rows, a project-scope pass must still read unattributed
    rows, and one environment must never read another's rows.
    """

    async def test_project_scope_ignores_environment_telemetry(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, component_id = await _seed(db_session)
        db_session.add(_rule(project_id, None, component_id))
        await _add_metric(
            db_session,
            project_id,
            env_id,
            component_id,
            "http.checkout.latency.p95",
            9_000.0,
            NOW,
        )
        await db_session.flush()

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=None
        )
        assert result.rules_evaluated == 1
        assert result.anomalies_opened == 0
        assert await _count_anomalies(db_session) == 0

    async def test_project_scope_reads_unattributed_telemetry(
        self, db_session: AsyncSession
    ) -> None:
        project_id, _env_id, component_id = await _seed(db_session)
        db_session.add(_rule(project_id, None, component_id))
        await _add_metric(
            db_session,
            project_id,
            None,
            component_id,
            "http.checkout.latency.p95",
            9_000.0,
            NOW,
        )
        await db_session.flush()

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=None
        )
        assert result.anomalies_opened == 1
        anomaly = await db_session.scalar(select(Anomaly))
        assert anomaly is not None and anomaly.environment_id is None

    async def test_environment_scope_ignores_other_environments(
        self, db_session: AsyncSession
    ) -> None:
        project_id, prod_id, component_id = await _seed(db_session)
        staging = Environment(
            project_id=project_id, name="staging", environment_type="STAGING"
        )
        db_session.add(staging)
        await db_session.flush()

        db_session.add(_rule(project_id, prod_id, component_id))
        await _add_metric(
            db_session,
            project_id,
            staging.id,
            component_id,
            "http.checkout.latency.p95",
            9_000.0,
            NOW,
        )
        await db_session.flush()

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=prod_id
        )
        assert result.anomalies_opened == 0
        assert await _count_anomalies(db_session) == 0


class TestConcurrentDetection:
    """Two detectors, one sample, one fingerprint row.

    Detection runs in two places at once: an explicit ``/anomalies/detect`` call
    and the background sweep that evaluates every active project. Both can reach
    the same sample with no registry row yet, and both then insert the same
    ``(project_id, fingerprint)``. The loser used to fail the whole request with
    an ``IntegrityError`` — a live platform run showed it as a 500 on detection.

    The claim is therefore atomic: a lost insert is retried as an *adoption* of
    the rival's row, and when that row is still uncommitted (invisible to this
    session) nothing is opened on a guess.
    """

    async def test_claiming_a_taken_fingerprint_adopts_the_existing_row(
        self, db_session: AsyncSession
    ) -> None:
        project_id, prod_id, component_id = await _seed(db_session)
        taken = AnomalyFingerprint(
            project_id=project_id,
            environment_id=prod_id,
            component_id=component_id,
            fingerprint="fingerprint-owned-by-the-rival",
            anomaly_type=AnomalyType.METRIC_THRESHOLD,
            anomaly_id=None,
            occurrence_count=0,
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
        db_session.add(taken)
        await db_session.flush()

        claimed = await AnomalyDetectionService(db_session, now=NOW)._claim_registry(
            project_id=project_id,
            environment_id=prod_id,
            component_id=component_id,
            fingerprint="fingerprint-owned-by-the-rival",
            anomaly_type=AnomalyType.METRIC_THRESHOLD,
        )
        assert claimed is not None and claimed.id == taken.id
        assert await _count_registries(db_session) == 1

    async def test_an_uncommitted_rival_neither_fails_nor_invents_a_state(
        self, db_session: AsyncSession, monkeypatch
    ) -> None:
        project_id, prod_id, component_id = await _seed(db_session)
        taken = AnomalyFingerprint(
            project_id=project_id,
            environment_id=prod_id,
            component_id=component_id,
            fingerprint="fingerprint-in-flight",
            anomaly_type=AnomalyType.METRIC_THRESHOLD,
            anomaly_id=None,
            occurrence_count=0,
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
        db_session.add(taken)
        await db_session.flush()

        service = AnomalyDetectionService(db_session, now=NOW)

        async def invisible(project_id_arg, fingerprint_arg):
            """What a rival's uncommitted row looks like from this session."""
            return None

        monkeypatch.setattr(service, "_fingerprint_row", invisible)

        claimed = await service._claim_registry(
            project_id=project_id,
            environment_id=prod_id,
            component_id=component_id,
            fingerprint="fingerprint-in-flight",
            anomaly_type=AnomalyType.METRIC_THRESHOLD,
        )
        #: No exception, no second row, and no fabricated state: the caller is
        #: told it cannot count this cycle rather than being handed a lie.
        assert claimed is None
        assert await _count_registries(db_session) == 1
        #: And the savepoint held the damage: the transaction is still usable.
        assert await db_session.scalar(select(Anomaly)) is None


async def _count_registries(db_session: AsyncSession) -> int:
    return int(
        await db_session.scalar(select(func.count()).select_from(AnomalyFingerprint))
        or 0
    )
