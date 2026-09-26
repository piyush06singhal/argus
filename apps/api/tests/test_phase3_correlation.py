"""Phase 3 — correlation engine tests (§21–§24).

The false-merge cases matter most: two unrelated services failing at the same
moment must NOT become one incident.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.anomaly import Anomaly, AnomalySeverity, AnomalyStatus, AnomalyType
from app.models.project import Environment, SoftwareProject
from app.models.system import ComponentDependency, SystemComponent
from app.services.incident_correlation import (
    SIGNAL_GRAPH_ADJACENT,
    SIGNAL_SAME_COMPONENT,
    SIGNAL_SHARED_DEPLOYMENT_CONTEXT,
    IncidentCorrelationEngine,
    evaluate_pair,
)

NOW = datetime(2026, 9, 19, 14, 22, tzinfo=timezone.utc)
WINDOW = 300

#: Shared scope for the pure-function tests — two anomalies can only correlate
#: if they are in the same project *and* environment, so a per-call random
#: scope would (correctly) never link.
SHARED_PROJECT = uuid.uuid4()
SHARED_ENV = uuid.uuid4()


def _anomaly(**overrides) -> Anomaly:
    payload = {
        "id": uuid.uuid4(),
        "project_id": SHARED_PROJECT,
        "environment_id": SHARED_ENV,
        "component_id": uuid.uuid4(),
        "anomaly_type": AnomalyType.LATENCY_SPIKE,
        "severity": AnomalySeverity.HIGH,
        "status": AnomalyStatus.DETECTED,
        "fingerprint": uuid.uuid4().hex,
        "detected_at": NOW,
        "metric_name": "http.checkout.latency.p95",
    }
    payload.update(overrides)
    return Anomaly(**payload)


class TestEvaluatePair:
    def test_same_component_links(self) -> None:
        component = uuid.uuid4()
        a = _anomaly(component_id=component, detected_at=NOW)
        b = _anomaly(component_id=component, detected_at=NOW + timedelta(seconds=60))
        evidence = evaluate_pair(a, b, adjacency={}, window_seconds=WINDOW, max_hops=2)
        assert evidence is not None
        assert SIGNAL_SAME_COMPONENT in evidence.signals
        assert evidence.strength == 1.0

    def test_different_project_never_links(self) -> None:
        a = _anomaly()
        b = _anomaly(project_id=uuid.uuid4(), component_id=a.component_id)
        assert (
            evaluate_pair(a, b, adjacency={}, window_seconds=WINDOW, max_hops=2) is None
        )

    def test_different_environment_never_links(self) -> None:
        a = _anomaly()
        b = _anomaly(environment_id=uuid.uuid4(), component_id=a.component_id)
        assert (
            evaluate_pair(a, b, adjacency={}, window_seconds=WINDOW, max_hops=2) is None
        )

    def test_outside_window_does_not_link(self) -> None:
        component = uuid.uuid4()
        a = _anomaly(component_id=component, detected_at=NOW)
        b = _anomaly(
            component_id=component, detected_at=NOW + timedelta(seconds=WINDOW + 1)
        )
        assert (
            evaluate_pair(a, b, adjacency={}, window_seconds=WINDOW, max_hops=2) is None
        )

    def test_graph_adjacent_links(self) -> None:
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        a = _anomaly(component_id=c1)
        b = _anomaly(component_id=c2)
        adjacency = {frozenset({c1, c2}): 1}
        evidence = evaluate_pair(
            a, b, adjacency=adjacency, window_seconds=WINDOW, max_hops=2
        )
        assert evidence is not None
        assert any(s.startswith(SIGNAL_GRAPH_ADJACENT) for s in evidence.signals)

    def test_adjacency_beyond_max_hops_does_not_link(self) -> None:
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        a = _anomaly(component_id=c1)
        b = _anomaly(component_id=c2)
        adjacency = {frozenset({c1, c2}): 3}
        assert (
            evaluate_pair(a, b, adjacency=adjacency, window_seconds=WINDOW, max_hops=2)
            is None
        )

    def test_unrelated_same_time_anomalies_do_not_merge(self) -> None:
        """The §24 case: checkout latency vs unrelated analytics error."""
        a = _anomaly(component_id=uuid.uuid4(), anomaly_type=AnomalyType.LATENCY_SPIKE)
        b = _anomaly(
            component_id=uuid.uuid4(),
            anomaly_type=AnomalyType.ERROR_RATE_SPIKE,
            metric_name="http.analytics.errors",
        )
        assert (
            evaluate_pair(a, b, adjacency={}, window_seconds=WINDOW, max_hops=2) is None
        )

    def test_same_type_alone_is_not_enough(self) -> None:
        a = _anomaly(component_id=uuid.uuid4())
        b = _anomaly(component_id=uuid.uuid4())
        assert (
            evaluate_pair(a, b, adjacency={}, window_seconds=WINDOW, max_hops=2) is None
        )

    def test_unattributed_anomalies_group_only_on_shared_identity(self) -> None:
        a = _anomaly(component_id=None, metric_name="http.checkout.latency.p95")
        b = _anomaly(component_id=None, metric_name="http.checkout.latency.p95")
        evidence = evaluate_pair(a, b, adjacency={}, window_seconds=WINDOW, max_hops=2)
        assert evidence is not None

        c = _anomaly(component_id=None, metric_name="http.payment.latency.p95")
        assert (
            evaluate_pair(a, c, adjacency={}, window_seconds=WINDOW, max_hops=2) is None
        )

    def test_deployment_context_is_recorded_as_a_signal(self) -> None:
        component = uuid.uuid4()
        a = _anomaly(component_id=component)
        b = _anomaly(component_id=component)
        evidence = evaluate_pair(
            a,
            b,
            adjacency={},
            window_seconds=WINDOW,
            max_hops=2,
            deployment_components={component},
        )
        assert evidence is not None
        assert SIGNAL_SHARED_DEPLOYMENT_CONTEXT in evidence.signals

    def test_deployment_context_alone_does_not_link(self) -> None:
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        a = _anomaly(component_id=c1)
        b = _anomaly(component_id=c2)
        assert (
            evaluate_pair(
                a,
                b,
                adjacency={},
                window_seconds=WINDOW,
                max_hops=2,
                deployment_components={c1, c2},
            )
            is None
        )


async def _seed(db_session: AsyncSession, *, dependencies=None):
    project = SoftwareProject(name="Corr Proj", slug=f"corr-{uuid.uuid4().hex[:8]}")
    db_session.add(project)
    await db_session.flush()
    env = Environment(
        project_id=project.id, name="production", environment_type="PRODUCTION"
    )
    components = [
        SystemComponent(
            project_id=project.id, component_type="SERVICE", name=f"svc-{i}"
        )
        for i in range(3)
    ]
    db_session.add_all([env, *components])
    await db_session.flush()
    for source, target in dependencies or []:
        db_session.add(
            ComponentDependency(
                source_component_id=components[source].id,
                target_component_id=components[target].id,
                dependency_type="HTTP",
            )
        )
    await db_session.flush()
    return project.id, env.id, [c.id for c in components]


async def _add_anomaly(
    db_session: AsyncSession,
    project_id,
    env_id,
    component_id,
    *,
    at: datetime,
    severity: AnomalySeverity = AnomalySeverity.HIGH,
    anomaly_type: AnomalyType = AnomalyType.LATENCY_SPIKE,
) -> Anomaly:
    anomaly = Anomaly(
        project_id=project_id,
        environment_id=env_id,
        component_id=component_id,
        anomaly_type=anomaly_type,
        severity=severity,
        status=AnomalyStatus.DETECTED,
        fingerprint=uuid.uuid4().hex,
        detected_at=at,
    )
    db_session.add(anomaly)
    await db_session.flush()
    return anomaly


class TestClustering:
    async def test_same_component_clusters(self, db_session: AsyncSession) -> None:
        project_id, env_id, components = await _seed(db_session)
        await _add_anomaly(db_session, project_id, env_id, components[0], at=NOW)
        await _add_anomaly(
            db_session,
            project_id,
            env_id,
            components[0],
            at=NOW + timedelta(seconds=30),
        )
        clusters = await IncidentCorrelationEngine(
            db_session, now=NOW + timedelta(seconds=60)
        ).correlate_scope(project_id=project_id, environment_id=env_id)

        assert len(clusters) == 1
        assert len(clusters[0].anomalies) == 2

    async def test_adjacent_components_cluster(self, db_session: AsyncSession) -> None:
        project_id, env_id, components = await _seed(db_session, dependencies=[(0, 1)])
        await _add_anomaly(db_session, project_id, env_id, components[0], at=NOW)
        await _add_anomaly(
            db_session,
            project_id,
            env_id,
            components[1],
            at=NOW + timedelta(seconds=30),
        )
        clusters = await IncidentCorrelationEngine(
            db_session, now=NOW + timedelta(seconds=60)
        ).correlate_scope(project_id=project_id, environment_id=env_id)
        assert len(clusters) == 1
        assert len(clusters[0].anomalies) == 2

    async def test_unrelated_components_stay_separate(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, components = await _seed(db_session)  # no dependencies
        await _add_anomaly(db_session, project_id, env_id, components[0], at=NOW)
        await _add_anomaly(
            db_session, project_id, env_id, components[1], at=NOW + timedelta(seconds=1)
        )
        clusters = await IncidentCorrelationEngine(
            db_session, now=NOW + timedelta(seconds=60)
        ).correlate_scope(project_id=project_id, environment_id=env_id)
        assert len(clusters) == 2
        assert all(len(c.anomalies) == 1 for c in clusters)

    async def test_cluster_span_guard_prevents_chaining(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, components = await _seed(
            db_session, dependencies=[(0, 1), (1, 2)]
        )
        base = NOW
        await _add_anomaly(db_session, project_id, env_id, components[0], at=base)
        await _add_anomaly(
            db_session,
            project_id,
            env_id,
            components[1],
            at=base + timedelta(seconds=int(WINDOW * 0.9)),
        )
        await _add_anomaly(
            db_session,
            project_id,
            env_id,
            components[2],
            at=base + timedelta(seconds=int(WINDOW * 1.8)),
        )
        clusters = await IncidentCorrelationEngine(
            db_session,
            now=base + timedelta(seconds=int(WINDOW * 2)),
            window_seconds=WINDOW,
            max_hops=2,
        ).correlate_scope(project_id=project_id, environment_id=env_id)

        # A 1.8x-window span must not become one cluster.
        assert max(len(c.anomalies) for c in clusters) <= 2
        assert len(clusters) >= 2

    async def test_clusters_carry_explainable_rationale(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, components = await _seed(db_session)
        await _add_anomaly(db_session, project_id, env_id, components[0], at=NOW)
        await _add_anomaly(
            db_session,
            project_id,
            env_id,
            components[0],
            at=NOW + timedelta(seconds=30),
        )
        clusters = await IncidentCorrelationEngine(
            db_session, now=NOW + timedelta(seconds=60)
        ).correlate_scope(project_id=project_id, environment_id=env_id)
        rationale = clusters[0].rationale
        assert SIGNAL_SAME_COMPONENT in rationale["signals"]
        assert rationale["reasons"]
        assert "not causation" in rationale["disclaimer"]

    async def test_environment_is_a_hard_boundary(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, components = await _seed(db_session)
        other_env = Environment(
            project_id=project_id, name="staging", environment_type="STAGING"
        )
        db_session.add(other_env)
        await db_session.flush()
        await _add_anomaly(db_session, project_id, env_id, components[0], at=NOW)
        await _add_anomaly(db_session, project_id, other_env.id, components[0], at=NOW)
        clusters = await IncidentCorrelationEngine(
            db_session, now=NOW + timedelta(seconds=60)
        ).correlate_scope(project_id=project_id, environment_id=env_id)
        assert len(clusters) == 1
        assert len(clusters[0].anomalies) == 1  # staging anomaly excluded

    async def test_already_grouped_anomalies_are_skipped(
        self, db_session: AsyncSession
    ) -> None:
        from app.models.incident import Incident, IncidentSeverity

        project_id, env_id, components = await _seed(db_session)
        incident = Incident(
            project_id=project_id,
            title="existing",
            severity=IncidentSeverity.HIGH,
            detected_at=NOW,
        )
        db_session.add(incident)
        await db_session.flush()
        grouped = await _add_anomaly(
            db_session, project_id, env_id, components[0], at=NOW
        )
        grouped.incident_id = incident.id
        await db_session.flush()

        clusters = await IncidentCorrelationEngine(
            db_session, now=NOW + timedelta(seconds=60)
        ).correlate_scope(project_id=project_id, environment_id=env_id)
        assert clusters == []


class TestClusterSignals:
    def test_single_high_severity_opens_an_incident(self) -> None:
        from app.services.incident_correlation import CorrelationCluster

        cluster = CorrelationCluster(
            anomalies=[_anomaly(severity=AnomalySeverity.HIGH)]
        )
        assert cluster.should_open_incident() is True

    def test_single_low_severity_does_not(self) -> None:
        from app.services.incident_correlation import CorrelationCluster

        cluster = CorrelationCluster(anomalies=[_anomaly(severity=AnomalySeverity.LOW)])
        assert cluster.should_open_incident() is False

    def test_two_low_severity_anomalies_do(self) -> None:
        from app.services.incident_correlation import CorrelationCluster

        cluster = CorrelationCluster(
            anomalies=[
                _anomaly(severity=AnomalySeverity.LOW),
                _anomaly(severity=AnomalySeverity.LOW),
            ]
        )
        assert cluster.should_open_incident() is True

    async def test_dominant_type_and_primary_component(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, components = await _seed(db_session)
        await _add_anomaly(
            db_session,
            project_id,
            env_id,
            components[0],
            at=NOW,
            severity=AnomalySeverity.LOW,
            anomaly_type=AnomalyType.LATENCY_SPIKE,
        )
        await _add_anomaly(
            db_session,
            project_id,
            env_id,
            components[0],
            at=NOW + timedelta(seconds=10),
            severity=AnomalySeverity.CRITICAL,
            anomaly_type=AnomalyType.ERROR_RATE_SPIKE,
        )
        await _add_anomaly(
            db_session,
            project_id,
            env_id,
            components[0],
            at=NOW + timedelta(seconds=20),
            severity=AnomalySeverity.HIGH,
            anomaly_type=AnomalyType.ERROR_RATE_SPIKE,
        )
        clusters = await IncidentCorrelationEngine(
            db_session, now=NOW + timedelta(seconds=60)
        ).correlate_scope(project_id=project_id, environment_id=env_id)
        cluster = clusters[0]
        assert cluster.dominant_anomaly_type is AnomalyType.ERROR_RATE_SPIKE
        assert cluster.severity is AnomalySeverity.CRITICAL
        assert cluster.primary_component_id == components[0]
        assert cluster.time_span_seconds == 20.0


class TestFalseMergeProtection:
    """§24: shared timing alone must never be enough."""

    def test_same_metric_on_unrelated_components_does_not_link(self) -> None:
        """Identical metric names are context, not a link, when nothing else matches.

        Two services reporting ``http.checkout.latency.p95`` from unrelated parts
        of the graph are still two services.
        """
        a = _anomaly(component_id=uuid.uuid4())
        b = _anomaly(component_id=uuid.uuid4(), detected_at=NOW + timedelta(seconds=5))
        assert (
            evaluate_pair(a, b, adjacency={}, window_seconds=WINDOW, max_hops=2) is None
        )

    def test_shared_deployment_context_cannot_link_on_its_own(self) -> None:
        """A nearby deployment enriches the rationale; it is not a correlation signal."""
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        a = _anomaly(component_id=c1)
        b = _anomaly(component_id=c2, detected_at=NOW + timedelta(seconds=5))
        assert (
            evaluate_pair(
                a,
                b,
                adjacency={},
                window_seconds=WINDOW,
                max_hops=2,
                deployment_components={c1},
            )
            is None
        )

    def test_shared_deployment_context_is_recorded_when_linked(self) -> None:
        component = uuid.uuid4()
        a = _anomaly(component_id=component)
        b = _anomaly(component_id=component, detected_at=NOW + timedelta(seconds=5))
        evidence = evaluate_pair(
            a,
            b,
            adjacency={},
            window_seconds=WINDOW,
            max_hops=2,
            deployment_components={component},
        )
        assert evidence is not None
        assert SIGNAL_SHARED_DEPLOYMENT_CONTEXT in evidence.signals
        assert "nearby deployment" in evidence.reason

    def test_unattributed_anomalies_need_identical_evidence(self) -> None:
        """With no component at all, only an identical metric or pattern may link."""
        a = _anomaly(component_id=None)
        b = _anomaly(
            component_id=None,
            detected_at=NOW + timedelta(seconds=5),
            metric_name="http.analytics.errors",
        )
        assert (
            evaluate_pair(a, b, adjacency={}, window_seconds=WINDOW, max_hops=2) is None
        )
        c = _anomaly(
            component_id=None,
            detected_at=NOW + timedelta(seconds=5),
            metric_name=a.metric_name,
        )
        assert (
            evaluate_pair(a, c, adjacency={}, window_seconds=WINDOW, max_hops=2)
            is not None
        )

    async def test_duplicate_telemetry_collapses_into_one_cluster(
        self, db_session: AsyncSession
    ) -> None:
        """The same fingerprint re-detected must not become extra anomalies."""
        project_id, env_id, components = await _seed(db_session)
        first = await _add_anomaly(
            db_session, project_id, env_id, components[0], at=NOW
        )
        second = await _add_anomaly(
            db_session,
            project_id,
            env_id,
            components[0],
            at=NOW + timedelta(seconds=30),
        )
        # Point both rows at the same fingerprint, as dedup would.
        second.fingerprint = first.fingerprint
        await db_session.flush()

        clusters = await IncidentCorrelationEngine(
            db_session, now=NOW + timedelta(seconds=60)
        ).correlate_scope(project_id=project_id, environment_id=env_id)
        # Still one cluster (the dedup happens upstream, in the rule engine).
        assert len(clusters) == 1
        assert len(clusters[0].anomalies) == 2


class TestScopeIsolation:
    """A cluster never spans environments, even if a caller hands over mixed rows."""

    async def test_project_scope_correlates_each_environment_separately(
        self, db_session: AsyncSession
    ) -> None:
        """A project-wide pass groups per environment instead of finding nothing.

        This previously asserted ``clusters == []``: a project-wide correlation
        read only environment-less anomalies, so with real (environment-scoped)
        anomalies on the record it silently correlated nothing and opened no
        incident. The property that has to hold is *no cross-environment
        cluster*, and it is enforced by :meth:`build_clusters` partitioning —
        see ``test_mixed_scopes_are_partitioned_not_merged`` below. The cluster
        here belongs to the environment its anomalies came from.
        """
        project_id, env_id, components = await _seed(db_session)
        await _add_anomaly(db_session, project_id, env_id, components[0], at=NOW)
        await _add_anomaly(
            db_session,
            project_id,
            env_id,
            components[0],
            at=NOW + timedelta(seconds=5),
        )

        clusters = await IncidentCorrelationEngine(
            db_session, now=NOW + timedelta(seconds=10)
        ).correlate_scope(project_id=project_id, environment_id=None)

        assert len(clusters) == 1
        assert len(clusters[0].anomalies) == 2
        #: Attributed to the environment it was observed in — never merged and
        #: never reported as environment-less.
        assert {a.environment_id for a in clusters[0].anomalies} == {env_id}

    async def test_environment_scope_ignores_project_anomalies(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, components = await _seed(db_session)
        await _add_anomaly(db_session, project_id, None, components[0], at=NOW)
        await _add_anomaly(
            db_session,
            project_id,
            None,
            components[0],
            at=NOW + timedelta(seconds=5),
        )

        clusters = await IncidentCorrelationEngine(
            db_session, now=NOW + timedelta(seconds=10)
        ).correlate_scope(project_id=project_id, environment_id=env_id)
        assert clusters == []

    async def test_mixed_input_is_partitioned_not_merged(
        self, db_session: AsyncSession
    ) -> None:
        project_id, env_id, components = await _seed(db_session)
        staging = Environment(
            project_id=project_id, name="staging", environment_type="STAGING"
        )
        db_session.add(staging)
        await db_session.flush()

        await _add_anomaly(db_session, project_id, env_id, components[0], at=NOW)
        await _add_anomaly(
            db_session,
            project_id,
            staging.id,
            components[0],
            at=NOW + timedelta(seconds=5),
        )

        # Bypass the scoped loader on purpose: the guard must hold regardless.
        mixed = list(
            (
                await db_session.execute(
                    select(Anomaly).where(Anomaly.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(mixed) == 2
        clusters = await IncidentCorrelationEngine(
            db_session, now=NOW + timedelta(seconds=10)
        ).build_clusters(mixed)
        assert len(clusters) == 2
        for cluster in clusters:
            scopes = {a.environment_id for a in cluster.anomalies}
            assert len(scopes) == 1
