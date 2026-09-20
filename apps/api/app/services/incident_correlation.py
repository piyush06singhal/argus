"""ARGUS Incident Correlation Engine (Phase 3 §21–§24).

Decides *which anomalies belong together*. This module only clusters — it does
not create incidents (that is the incident manager's job) and it never claims
causation.

Correlation requires **meaningful shared evidence**, not merely shared timing.
The linkage rule is deliberately conservative:

* Same project **and** same environment are hard preconditions. Production and
  staging anomalies can never merge, and neither can two projects'.
* Times must fall inside the correlation window.
* Two anomalies link only when they are on the **same component**, or on
  **structurally adjacent components** in the Phase 2 graph within a bounded
  hop count. Two unrelated services erroring at the same moment are *not*
  linked — that is the false-merge protection of §24.
* Additional context (same metric, same log template, a nearby deployment)
  enriches the recorded rationale but can never create a link on its own.
* A cluster's total time span is capped, so transitive chaining cannot grow a
  cluster into "everything that happened this hour".

Everything is deterministic and the rationale is stored with the cluster, so an
engineer can see exactly why two anomalies were grouped.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import ensure_utc_or_now
from app.models.anomaly import Anomaly, AnomalySeverity, AnomalyType
from app.models.deployment import DeploymentEvent
from app.models.graph import GraphEdge, GraphEdgeStatus, GraphNode
from app.models.system import ComponentDependency
from app.services.anomaly_severity import max_severity
from app.services.engines import IncidentCorrelator

logger = logging.getLogger(__name__)
settings = get_settings()

#: Correlation signal names — stable strings, stored verbatim in rationale.
SIGNAL_SAME_COMPONENT = "SAME_COMPONENT"
SIGNAL_GRAPH_ADJACENT = "GRAPH_ADJACENT"
SIGNAL_SAME_METRIC = "SAME_METRIC"
SIGNAL_SAME_PATTERN = "SAME_PATTERN"
SIGNAL_SAME_ANOMALY_TYPE = "SAME_ANOMALY_TYPE"
SIGNAL_SHARED_DEPLOYMENT_CONTEXT = "SHARED_DEPLOYMENT_CONTEXT"

#: Anomalies at or above this severity open an incident even alone.
INCIDENT_SEVERITY_FLOOR = AnomalySeverity.HIGH

#: Bounded edge loads so adjacency never becomes a full-graph scan.
MAX_EDGES_LOADED = 20_000


def _cluster_sort_key(cluster: "CorrelationCluster") -> tuple:
    """Deterministic cluster order, independent of the input ordering."""
    return (
        ensure_utc_or_now(cluster.earliest),
        str(cluster.primary_component_id or ""),
        tuple(sorted(cluster.rationale.get("anomaly_ids", []))),
    )


@dataclass(frozen=True)
class CorrelationEvidence:
    """Why two anomalies are (or are not) part of the same incident."""

    signals: tuple[str, ...]
    reason: str
    #: Evidence strength in [0, 1]. Grouping confidence, not causal probability.
    strength: float


@dataclass
class CorrelationCluster:
    """A group of anomalies judged to belong to the same incident."""

    anomalies: list[Anomaly] = field(default_factory=list)
    rationale: dict = field(default_factory=dict)
    primary_component_id: Optional[uuid.UUID] = None
    dominant_anomaly_type: Optional[AnomalyType] = None
    severity: AnomalySeverity = AnomalySeverity.LOW
    earliest: Optional[datetime] = None
    latest: Optional[datetime] = None

    @property
    def component_ids(self) -> list[uuid.UUID]:
        seen: list[uuid.UUID] = []
        for anomaly in self.anomalies:
            if anomaly.component_id is not None and anomaly.component_id not in seen:
                seen.append(anomaly.component_id)
        return seen

    @property
    def time_span_seconds(self) -> float:
        if self.earliest is None or self.latest is None:
            return 0.0
        return (self.latest - self.earliest).total_seconds()

    def should_open_incident(self) -> bool:
        """Open an incident for multi-anomaly clusters or severe single ones.

        A single LOW/MEDIUM anomaly stays in the Anomaly Center — promoting
        every one of them to an incident would make the incident list noise.
        """
        if len(self.anomalies) >= 2:
            return True
        return any(
            _severity_rank(a.severity) >= _severity_rank(INCIDENT_SEVERITY_FLOOR)
            for a in self.anomalies
        )


def _type_value(value: object) -> str:
    """Enum value as a plain string.

    ``str()`` on a ``str``-mixin Enum yields ``"AnomalyType.LATENCY_SPIKE"``,
    which then fails to round-trip back through the enum constructor.
    """
    if isinstance(value, AnomalyType):
        return value.value
    return str(value)


def _severity_rank(severity: AnomalySeverity | str) -> int:
    order = {
        AnomalySeverity.LOW: 0,
        AnomalySeverity.MEDIUM: 1,
        AnomalySeverity.HIGH: 2,
        AnomalySeverity.CRITICAL: 3,
    }
    try:
        return order[AnomalySeverity(severity)]
    except (ValueError, KeyError):
        return 0


def evaluate_pair(
    a: Anomaly,
    b: Anomaly,
    *,
    adjacency: dict[frozenset, int],
    window_seconds: int,
    max_hops: int,
    deployment_components: Optional[set] = None,
) -> Optional[CorrelationEvidence]:
    """Decide whether two anomalies belong together (§21–§24).

    Returns ``None`` when they must not be linked. Pure function of the two
    records plus adjacency — no database, so the rules are unit-testable.
    """
    if a.project_id != b.project_id:
        return None
    # Environment is a hard boundary: never correlate across environments.
    if a.environment_id != b.environment_id:
        return None

    ta, tb = ensure_utc_or_now(a.detected_at), ensure_utc_or_now(b.detected_at)
    if abs((ta - tb).total_seconds()) > max(1, int(window_seconds)):
        return None

    signals: list[str] = []
    same_component = a.component_id is not None and a.component_id == b.component_id
    if same_component:
        signals.append(SIGNAL_SAME_COMPONENT)

    hops: Optional[int] = None
    if a.component_id is not None and b.component_id is not None:
        hops = adjacency.get(frozenset({a.component_id, b.component_id}))
    adjacent = hops is not None and hops <= max(1, int(max_hops))
    if adjacent:
        signals.append(f"{SIGNAL_GRAPH_ADJACENT}:{hops}")

    if a.metric_name and a.metric_name == b.metric_name:
        signals.append(SIGNAL_SAME_METRIC)
    if a.pattern_template and a.pattern_template == b.pattern_template:
        signals.append(SIGNAL_SAME_PATTERN)
    if a.anomaly_type == b.anomaly_type:
        signals.append(SIGNAL_SAME_ANOMALY_TYPE)
    if deployment_components and (
        a.component_id in deployment_components
        or b.component_id in deployment_components
    ):
        signals.append(SIGNAL_SHARED_DEPLOYMENT_CONTEXT)

    # --- Linkage rule (deliberately narrow) --------------------------------
    if same_component or adjacent:
        linked = True
    elif a.component_id is None and b.component_id is None:
        # Unattributed anomalies carry no structural evidence; only an identical
        # metric or log template justifies grouping them.
        linked = bool(
            (a.metric_name and a.metric_name == b.metric_name)
            or (a.pattern_template and a.pattern_template == b.pattern_template)
        )
    else:
        linked = False

    if not linked:
        return None

    strength = 0.5
    if same_component:
        strength = 1.0
    elif adjacent:
        strength = 0.75

    reason_bits: list[str] = []
    if same_component:
        reason_bits.append("same component")
    if adjacent:
        reason_bits.append(f"components are {hops} hop(s) apart in the graph")
    if SIGNAL_SAME_METRIC in signals:
        reason_bits.append("same metric")
    if SIGNAL_SAME_PATTERN in signals:
        reason_bits.append("same log pattern")
    if SIGNAL_SAME_ANOMALY_TYPE in signals:
        reason_bits.append("same anomaly type")
    if SIGNAL_SHARED_DEPLOYMENT_CONTEXT in signals:
        reason_bits.append("a nearby deployment touched an affected component")
    reason_bits.append(
        f"within {abs((ta - tb).total_seconds()):.0f}s " f"(window {window_seconds}s)"
    )
    return CorrelationEvidence(
        signals=tuple(signals), reason="; ".join(reason_bits), strength=strength
    )


class IncidentCorrelationEngine(IncidentCorrelator):
    """Graph-aware, deterministic correlation over detected anomalies."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        now: Optional[datetime] = None,
        window_seconds: Optional[int] = None,
        max_hops: Optional[int] = None,
    ) -> None:
        self._session = session
        self._now = ensure_utc_or_now(now)
        self._window = int(
            window_seconds
            if window_seconds is not None
            else settings.CORRELATION_WINDOW_SECONDS
        )
        self._max_hops = int(
            max_hops
            if max_hops is not None
            else settings.CORRELATION_MAX_COMPONENT_HOPS
        )

    # -- ABC compliance -----------------------------------------------------
    async def correlate(self, anomalies: list[Any]) -> list[Any]:
        """Cluster the given anomalies (see :meth:`build_clusters`)."""
        return await self.build_clusters(anomalies)

    # -- Data loading -------------------------------------------------------
    async def load_candidates(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
        lookback_seconds: Optional[int] = None,
    ) -> list[Anomaly]:
        """Load un-grouped anomalies inside a bounded lookback window."""
        lookback = int(lookback_seconds or self._window * 4)
        since = self._now - timedelta(seconds=max(1, lookback))
        stmt = (
            select(Anomaly)
            .where(
                Anomaly.project_id == project_id,
                Anomaly.incident_id.is_(None),
                Anomaly.detected_at >= since,
            )
            .order_by(Anomaly.detected_at)
            .limit(settings.CORRELATION_MAX_ANOMALIES)
        )
        # Exact scope: None correlates environment-less anomalies only. Reading
        # every environment here would let a staging anomaly be grouped into a
        # production incident the moment they happen to be temporally close.
        stmt = stmt.where(
            Anomaly.environment_id == environment_id
            if environment_id is not None
            else Anomaly.environment_id.is_(None)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def correlate_scope(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
    ) -> list[CorrelationCluster]:
        """Convenience: load un-grouped anomalies and cluster them."""
        candidates = await self.load_candidates(
            project_id=project_id, environment_id=environment_id
        )
        return await self.build_clusters(candidates)

    # -- Clustering ---------------------------------------------------------
    async def build_clusters(
        self, anomalies: Sequence[Anomaly]
    ) -> list[CorrelationCluster]:
        """Group anomalies into clusters using union-find with a span guard.

        A cluster is always single-project **and** single-environment. Callers
        are expected to scope their query, but if a mixed set ever reaches here
        (a new call site, a regression, a merged page of results) the set is
        partitioned instead of correlated. Merging across scopes is a data-leak
        class of bug, not a tuning parameter, so it is refused structurally
        rather than left to every caller's discipline.
        """
        items = [a for a in anomalies if a is not None]
        if not items:
            return []

        scopes: dict[tuple, list[Anomaly]] = {}
        for anomaly in items:
            scopes.setdefault((anomaly.project_id, anomaly.environment_id), []).append(
                anomaly
            )
        if len(scopes) > 1:
            clusters: list[CorrelationCluster] = []
            for scope_items in scopes.values():
                clusters.extend(await self._build_single_scope(scope_items))
            clusters.sort(key=_cluster_sort_key)
            return clusters
        return await self._build_single_scope(items)

    async def _build_single_scope(
        self, anomalies: Sequence[Anomaly]
    ) -> list[CorrelationCluster]:
        """Cluster one ``(project, environment)`` scope (union-find + span guard)."""
        items = [a for a in anomalies if a is not None]
        if not items:
            return []
        # Ordering makes the result independent of input ordering.
        items = sorted(
            items, key=lambda a: (ensure_utc_or_now(a.detected_at), str(a.id))
        )

        component_ids = {a.component_id for a in items if a.component_id is not None}
        project_id = items[0].project_id
        adjacency = await self._adjacency(project_id, component_ids)
        deployment_components = await self._deployment_components(project_id, items)

        n = len(items)
        parent = list(range(n))
        members: dict[int, list[int]] = {i: [i] for i in range(n)}
        times = [ensure_utc_or_now(a.detected_at) for a in items]
        edges: dict[tuple[int, int], CorrelationEvidence] = {}

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(n):
            for j in range(i + 1, n):
                evidence = evaluate_pair(
                    items[i],
                    items[j],
                    adjacency=adjacency,
                    window_seconds=self._window,
                    max_hops=self._max_hops,
                    deployment_components=deployment_components,
                )
                if evidence is None:
                    continue
                root_i, root_j = find(i), find(j)
                if root_i == root_j:
                    edges[(i, j)] = evidence
                    continue
                combined = members[root_i] + members[root_j]
                span = max(times[k] for k in combined) - min(times[k] for k in combined)
                if span.total_seconds() > self._window:
                    # Transitive chaining would widen the cluster beyond the
                    # window; refuse the merge rather than over-group (§24).
                    continue
                parent[root_j] = root_i
                members[root_i] = combined
                del members[root_j]
                edges[(i, j)] = evidence

        grouped: dict[int, list[int]] = {}
        for i in range(n):
            grouped.setdefault(find(i), []).append(i)

        clusters: list[CorrelationCluster] = []
        for indices in grouped.values():
            members_list = sorted(
                indices,
                key=lambda i: (
                    ensure_utc_or_now(items[i].detected_at),
                    str(items[i].id),
                ),
            )
            cluster_anomalies = [items[i] for i in members_list]
            clusters.append(self._finalize(cluster_anomalies, items, edges))
        return clusters

    def _finalize(
        self,
        anomalies: list[Anomaly],
        items: Sequence[Anomaly],
        edges: dict[tuple[int, int], CorrelationEvidence],
    ) -> CorrelationCluster:
        times = [ensure_utc_or_now(a.detected_at) for a in anomalies]
        severity = AnomalySeverity.LOW
        for anomaly in anomalies:
            severity = max_severity(severity, AnomalySeverity(anomaly.severity))

        # Primary component: the one on the highest-severity, earliest anomaly.
        ordered = sorted(
            anomalies,
            key=lambda a: (
                -_severity_rank(a.severity),
                ensure_utc_or_now(a.detected_at),
            ),
        )
        primary = next(
            (a.component_id for a in ordered if a.component_id is not None), None
        )
        dominant = self._dominant_type(anomalies)

        index_of = {id(a): i for i, a in enumerate(items)}
        signals: list[str] = []
        reasons: list[str] = []
        for anomaly in anomalies:
            i = index_of.get(id(anomaly))
            if i is None:
                continue
            for (left, right), evidence in edges.items():
                if left == i or right == i:
                    for signal in evidence.signals:
                        if signal not in signals:
                            signals.append(signal)
                    if evidence.reason not in reasons:
                        reasons.append(evidence.reason)

        rationale = {
            "signals": signals,
            "reasons": reasons,
            "anomaly_count": len(anomalies),
            "component_count": len(
                {a.component_id for a in anomalies if a.component_id}
            ),
            "anomaly_ids": [str(a.id) for a in anomalies],
            "anomaly_types": sorted({_type_value(a.anomaly_type) for a in anomalies}),
            "window_seconds": self._window,
            "max_hops": self._max_hops,
            "disclaimer": (
                "Correlation groups anomalies by shared evidence. Grouping is not "
                "causation and does not identify a root cause."
            ),
        }
        return CorrelationCluster(
            anomalies=anomalies,
            rationale=rationale,
            primary_component_id=primary,
            dominant_anomaly_type=dominant,
            severity=severity,
            earliest=min(times) if times else None,
            latest=max(times) if times else None,
        )

    @staticmethod
    def _dominant_type(anomalies: Sequence[Anomaly]) -> Optional[AnomalyType]:
        """Most frequent type; ties broken by severity then name (deterministic)."""
        counts: dict[str, int] = {}
        for anomaly in anomalies:
            key = _type_value(anomaly.anomaly_type)
            counts[key] = counts.get(key, 0) + 1
        if not counts:
            return None
        best = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        try:
            return AnomalyType(best)
        except ValueError:
            return None

    # -- Graph adjacency ----------------------------------------------------
    async def _adjacency(
        self, project_id: uuid.UUID, component_ids: set[uuid.UUID]
    ) -> dict[frozenset, int]:
        """Shortest hop distance between the given components, within ``max_hops``.

        Uses the canonical ``component_dependencies`` **and** the Phase 2
        evidence-derived ``graph_edges`` (trace-discovered CALLS etc.), because
        either can be the only record of a real relationship. Bounded by
        ``MAX_EDGES_LOADED``; this is context, not a traversal API.
        """
        if len(component_ids) < 2:
            return {}
        neighbours: dict[uuid.UUID, set[uuid.UUID]] = {
            cid: set() for cid in component_ids
        }

        dep_stmt = (
            select(
                ComponentDependency.source_component_id,
                ComponentDependency.target_component_id,
            )
            .where(
                or_(
                    ComponentDependency.source_component_id.in_(component_ids),
                    ComponentDependency.target_component_id.in_(component_ids),
                )
            )
            .limit(MAX_EDGES_LOADED)
        )
        for source, target in (await self._session.execute(dep_stmt)).all():
            if source in neighbours and target in neighbours:
                neighbours[source].add(target)
                neighbours[target].add(source)

        # Evidence-derived edges: translate graph node ids back to entities.
        node_stmt = select(GraphNode.id, GraphNode.entity_id).where(
            GraphNode.project_id == project_id,
            GraphNode.entity_kind == "system_component",
            GraphNode.entity_id.in_(component_ids),
        )
        node_to_entity: dict[uuid.UUID, uuid.UUID] = {
            node_id: entity_id
            for node_id, entity_id in (await self._session.execute(node_stmt)).all()
            if entity_id is not None
        }
        if node_to_entity:
            edge_stmt = (
                select(GraphEdge.source_node_id, GraphEdge.target_node_id)
                .where(
                    GraphEdge.project_id == project_id,
                    GraphEdge.status == GraphEdgeStatus.ACTIVE,
                    GraphEdge.source_node_id.in_(node_to_entity.keys()),
                    GraphEdge.target_node_id.in_(node_to_entity.keys()),
                )
                .limit(MAX_EDGES_LOADED)
            )
            for source_node, target_node in (
                await self._session.execute(edge_stmt)
            ).all():
                source = node_to_entity.get(source_node)
                target = node_to_entity.get(target_node)
                if source and target and source != target:
                    neighbours[source].add(target)
                    neighbours[target].add(source)

        # BFS from each component of interest, recording distances to the others.
        distances: dict[frozenset, int] = {}
        for origin in component_ids:
            seen = {origin: 0}
            frontier = [origin]
            while frontier:
                next_frontier: list[uuid.UUID] = []
                for node in frontier:
                    depth = seen[node]
                    if depth >= self._max_hops:
                        continue
                    for neighbour in neighbours.get(node, set()):
                        if neighbour not in seen:
                            seen[neighbour] = depth + 1
                            next_frontier.append(neighbour)
                frontier = next_frontier
            for other, depth in seen.items():
                if other == origin or depth == 0:
                    continue
                distances.setdefault(frozenset({origin, other}), depth)
        return distances

    async def _deployment_components(
        self, project_id: uuid.UUID, anomalies: Sequence[Anomaly]
    ) -> set:
        """Components with a deployment near the anomalies (temporal context only)."""
        if not anomalies:
            return set()
        times = [ensure_utc_or_now(a.detected_at) for a in anomalies]
        start = min(times) - timedelta(seconds=settings.INCIDENT_CONTEXT_WINDOW_SECONDS)
        end = max(times) + timedelta(seconds=settings.INCIDENT_CONTEXT_WINDOW_SECONDS)
        stmt = select(DeploymentEvent.component_id).where(
            DeploymentEvent.project_id == project_id,
            DeploymentEvent.deployed_at >= start,
            DeploymentEvent.deployed_at <= end,
            DeploymentEvent.component_id.isnot(None),
        )
        return {
            component_id
            for (component_id,) in (await self._session.execute(stmt)).all()
            if component_id is not None
        }


__all__ = [
    "IncidentCorrelationEngine",
    "CorrelationCluster",
    "CorrelationEvidence",
    "evaluate_pair",
    "SIGNAL_SAME_COMPONENT",
    "SIGNAL_GRAPH_ADJACENT",
    "SIGNAL_SAME_METRIC",
    "SIGNAL_SAME_PATTERN",
    "SIGNAL_SAME_ANOMALY_TYPE",
    "SIGNAL_SHARED_DEPLOYMENT_CONTEXT",
]
