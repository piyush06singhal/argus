"""Detection scope resolution (hardening W3).

A detection run carries an optional environment scope, and until this module
existed nothing pinned what that scope means for *telemetry reads* — every
evaluator simply applied the run's environment, with ``None`` meaning
"environment-less rows only".

Real telemetry always carries the environment it came from, so the consequence
was silent and absolute: ``POST /projects/{id}/anomalies/detect`` (the default,
project-wide call, and the call a new user makes) evaluated every enabled rule
and read **no samples at all** — ``anomalies_opened: 0`` with an empty
``errors`` list, which looks exactly like "nothing is wrong". Scoping the run to
one environment worked, which is why the phase gates (which pass
``environment_id``) never noticed.

These tests pin the three properties the fix has to hold at once:

1. a project-wide run evaluates a project-wide rule **once per environment** and
   fires (the defect);
2. an environment is **never pooled** with another — a rule that declares an
   environment is only ever read against that environment, and a rule belonging
   to another environment cannot fire on this one's telemetry;
3. an environment-scoped run still reads exactly that environment (unchanged),
   and environment-less rows written directly over the API remain visible.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.anomaly import (
    Anomaly,
    AnomalyRule,
    AnomalySeverity,
    AnomalyType,
    RuleCondition,
)
from app.models.observability import MetricRecord, MetricType
from app.models.project import Environment, SoftwareProject
from app.models.system import SystemComponent
from app.services.anomaly_detection import AnomalyDetectionService

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
METRIC = "scope.checkout.latency.p95"


async def _seed(db_session: AsyncSession, *, environments: list[str]):
    """A project with the named environments and one component."""
    project = SoftwareProject(name="Scope Proj", slug=f"scope-{uuid.uuid4().hex[:8]}")
    db_session.add(project)
    await db_session.flush()

    envs = {}
    for name in environments:
        env = Environment(
            project_id=project.id,
            name=name,
            environment_type="PRODUCTION" if name == "production" else "STAGING",
        )
        db_session.add(env)
        envs[name] = env
    component = SystemComponent(
        project_id=project.id, component_type="SERVICE", name="Checkout"
    )
    db_session.add(component)
    await db_session.flush()
    return project.id, {name: env.id for name, env in envs.items()}, component.id


def _rule(
    project_id,
    *,
    environment_id=None,
    component_id=None,
    name="p95 over 500ms",
    threshold: float = 500.0,
) -> AnomalyRule:
    return AnomalyRule(
        project_id=project_id,
        environment_id=environment_id,
        component_id=component_id,
        name=name,
        anomaly_type=AnomalyType.LATENCY_SPIKE,
        condition=RuleCondition.THRESHOLD.value,
        baseline_strategy="ROLLING",
        threshold=threshold,
        metric_name=METRIC,
        min_samples=1,
        window_seconds=3600,
        cooldown_seconds=0,
        persistence_cycles=1,
        severity=AnomalySeverity.HIGH,
        enabled=True,
    )


async def _add_metric(
    db_session: AsyncSession,
    project_id,
    *,
    environment_id,
    component_id,
    value: float,
    at: datetime | None = None,
) -> None:
    db_session.add(
        MetricRecord(
            project_id=project_id,
            environment_id=environment_id,
            component_id=component_id,
            timestamp=at or NOW,
            metric_name=METRIC,
            metric_type=MetricType.GAUGE,
            value=value,
        )
    )
    await db_session.flush()


async def _anomalies(db_session: AsyncSession) -> list[Anomaly]:
    result = await db_session.execute(select(Anomaly))
    return list(result.scalars().all())


class TestProjectWideRun:
    """The default call: no environment scope on the run."""

    async def test_project_wide_run_reads_environment_scoped_telemetry(
        self, db_session: AsyncSession
    ) -> None:
        project_id, envs, component_id = await _seed(
            db_session, environments=["production"]
        )
        db_session.add(_rule(project_id))
        await _add_metric(
            db_session,
            project_id,
            environment_id=envs["production"],
            component_id=component_id,
            value=900.0,
        )

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id
        )

        assert result.rules_evaluated == 1
        assert result.errors == []
        assert result.anomalies_opened == 1, (
            "a project-wide run must read the project's environment-scoped "
            "telemetry; reading only environment-less rows reports a clean "
            "project while the fault is on the record"
        )
        anomalies = await _anomalies(db_session)
        assert len(anomalies) == 1
        assert anomalies[0].environment_id == envs["production"]

    async def test_no_pooling_between_environments(
        self, db_session: AsyncSession
    ) -> None:
        """Each environment is evaluated on its own samples.

        Production is healthy and staging is spiking: exactly one anomaly, and
        it belongs to staging. Pooling the two would average the spike away, and
        a shared baseline would let one environment's fault fire the other's.
        """
        project_id, envs, component_id = await _seed(
            db_session, environments=["production", "staging"]
        )
        db_session.add(_rule(project_id))
        await _add_metric(
            db_session,
            project_id,
            environment_id=envs["production"],
            component_id=component_id,
            value=120.0,
        )
        await _add_metric(
            db_session,
            project_id,
            environment_id=envs["staging"],
            component_id=component_id,
            value=900.0,
        )

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id
        )

        assert result.anomalies_opened == 1
        anomalies = await _anomalies(db_session)
        assert [a.environment_id for a in anomalies] == [envs["staging"]]

    async def test_rule_scoped_to_one_environment_cannot_fire_in_another(
        self, db_session: AsyncSession
    ) -> None:
        project_id, envs, component_id = await _seed(
            db_session, environments=["production", "staging"]
        )
        #: The rule watches production only; the spike is in staging.
        db_session.add(_rule(project_id, environment_id=envs["production"]))
        await _add_metric(
            db_session,
            project_id,
            environment_id=envs["staging"],
            component_id=component_id,
            value=900.0,
        )

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id
        )

        assert result.anomalies_opened == 0
        assert await _anomalies(db_session) == []

    async def test_environment_less_telemetry_is_still_visible(
        self, db_session: AsyncSession
    ) -> None:
        """The bucket direct API writes land in must not be lost by the fix."""
        project_id, _envs, component_id = await _seed(
            db_session, environments=["production"]
        )
        db_session.add(_rule(project_id))
        await _add_metric(
            db_session,
            project_id,
            environment_id=None,
            component_id=component_id,
            value=900.0,
        )

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id
        )

        assert result.anomalies_opened == 1


class TestEnvironmentScopedRun:
    """Passing an environment to the run keeps its original, exact meaning."""

    async def test_scoped_run_reads_only_that_environment(
        self, db_session: AsyncSession
    ) -> None:
        project_id, envs, component_id = await _seed(
            db_session, environments=["production", "staging"]
        )
        db_session.add(_rule(project_id))
        await _add_metric(
            db_session,
            project_id,
            environment_id=envs["staging"],
            component_id=component_id,
            value=900.0,
        )

        scoped = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=envs["production"]
        )
        assert scoped.anomalies_opened == 0, (
            "an environment-scoped run must not read another environment's " "telemetry"
        )

        #: And the staging run does see it — proving the sample was readable.
        staging = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=envs["staging"]
        )
        assert staging.anomalies_opened == 1

    async def test_scoped_run_skips_other_environments_rules(
        self, db_session: AsyncSession
    ) -> None:
        project_id, envs, component_id = await _seed(
            db_session, environments=["production", "staging"]
        )
        db_session.add(
            _rule(
                project_id,
                environment_id=envs["staging"],
                name="staging rule",
                threshold=1.0,
            )
        )
        await _add_metric(
            db_session,
            project_id,
            environment_id=envs["production"],
            component_id=component_id,
            value=900.0,
        )

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id, environment_id=envs["production"]
        )

        assert result.rules_evaluated == 0
        assert result.anomalies_opened == 0

    async def test_window_still_bounds_the_read(self, db_session: AsyncSession) -> None:
        """Scope resolution must not widen the time window (§19 bounds)."""
        project_id, envs, component_id = await _seed(
            db_session, environments=["production"]
        )
        db_session.add(_rule(project_id))
        await _add_metric(
            db_session,
            project_id,
            environment_id=envs["production"],
            component_id=component_id,
            value=900.0,
            at=NOW - timedelta(hours=4),
        )

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id
        )

        assert result.anomalies_opened == 0


class TestUnchangedDefinitions:
    @pytest.mark.parametrize(
        "unused",
        [None],
    )
    async def test_rule_without_metric_name_is_unaffected(
        self, db_session: AsyncSession, unused
    ) -> None:
        """A health rule (no metric read) still evaluates in a wide run."""
        project_id, _envs, _component_id = await _seed(
            db_session, environments=["production"]
        )
        rule = _rule(project_id, name="health rule")
        rule.condition = RuleCondition.HEALTH_TRANSITION.value
        rule.metric_name = None
        db_session.add(rule)
        await db_session.flush()

        result = await AnomalyDetectionService(db_session, now=NOW).run(
            project_id=project_id
        )

        assert result.rules_evaluated == 1
