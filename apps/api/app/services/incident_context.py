"""ARGUS Incident Context Builders (Phase 3 §30–§32, §39).

Read-only helpers that assemble the *context* sections of an incident detail
response: which components were affected and how they relate, the surrounding
knowledge-graph slice, and nearby deployments/configuration changes.

Hard boundaries upheld here:

* Graph context is structural. Nodes/edges are returned with an explicit
  disclaimer; nothing is labelled a cause.
* Deployment and configuration entries are temporal context only, and the
  distance from the first anomaly is computed, never guessed.
* Every query is bounded and scoped to the incident's project/environment.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import ensure_utc
from app.models.deployment import DeploymentEvent
from app.models.graph import GraphEdge, GraphEdgeStatus, GraphNode
from app.models.incident import Incident
from app.models.ingestion import ConfigurationChangeEvent
from app.models.system import ComponentDependency, SystemComponent

settings = get_settings()

_MAX_GRAPH_NODES = 200
_MAX_CONTEXT_ITEMS = 50


@dataclass
class AffectedComponentView:
    """A component in the incident's blast radius with its classification."""

    component_id: uuid.UUID
    name: Optional[str]
    classification: str
    reason: str
    anomaly_count: int = 0
    severity: Optional[str] = None


@dataclass
class GraphContext:
    """Structural (never causal) graph slice around an incident."""

    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    disclaimer: str = (
        "Graph relationships show structural context. Related components are "
        "not implied to be causes."
    )


async def component_names(
    db: AsyncSession, component_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Resolve component ids to names (bounded)."""
    ids = [c for c in component_ids if c is not None]
    if not ids:
        return {}
    stmt = select(SystemComponent.id, SystemComponent.name).where(
        SystemComponent.id.in_(ids[:_MAX_GRAPH_NODES])
    )
    return {component_id: name for component_id, name in (await db.execute(stmt)).all()}


async def build_graph_context(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    component_ids: Sequence[uuid.UUID],
    classifications: Optional[dict[uuid.UUID, str]] = None,
) -> GraphContext:
    """Return the graph nodes/edges around the incident's components.

    Bounded by ``INCIDENT_MAX_GRAPH_NODES``; only ACTIVE edges are returned, so
    stale/discovered-but-unconfirmed relationships cannot masquerade as current
    structure.
    """
    context = GraphContext()
    ids = [c for c in component_ids if c is not None][:_MAX_GRAPH_NODES]
    if not ids:
        return context

    node_stmt = (
        select(GraphNode)
        .where(
            GraphNode.project_id == project_id,
            GraphNode.entity_kind == "system_component",
            GraphNode.entity_id.in_(ids),
        )
        .limit(_MAX_GRAPH_NODES)
    )
    nodes = list((await db.execute(node_stmt)).scalars().all())
    if not nodes:
        return context

    node_ids = [n.id for n in nodes]
    edge_stmt = (
        select(GraphEdge)
        .where(
            GraphEdge.project_id == project_id,
            GraphEdge.status == GraphEdgeStatus.ACTIVE,
            or_(
                GraphEdge.source_node_id.in_(node_ids),
                GraphEdge.target_node_id.in_(node_ids),
            ),
        )
        .limit(_MAX_GRAPH_NODES * 4)
    )
    edges = list((await db.execute(edge_stmt)).scalars().all())

    classifications = classifications or {}
    context.nodes = [
        {
            "node_id": node.id,
            "name": node.name,
            "node_type": (
                node.node_type.value
                if hasattr(node.node_type, "value")
                else str(node.node_type)
            ),
            "classification": (
                classifications.get(node.entity_id, "DEPENDENCY_CONTEXT")
                if node.entity_id is not None
                else "DEPENDENCY_CONTEXT"
            ),
        }
        for node in nodes
    ]
    for edge in edges:
        # Only surface edges whose endpoints are both in this slice.
        if edge.source_node_id not in set(node_ids) or edge.target_node_id not in set(
            node_ids
        ):
            continue
        context.edges.append(
            {
                "source_node_id": edge.source_node_id,
                "target_node_id": edge.target_node_id,
                "edge_type": (
                    edge.edge_type.value
                    if hasattr(edge.edge_type, "value")
                    else str(edge.edge_type)
                ),
                "source": (
                    edge.source.value if hasattr(edge.source, "value") else None
                ),
            }
        )
    return context


async def build_dependency_components(
    db: AsyncSession, observed_ids: Sequence[uuid.UUID]
) -> list[AffectedComponentView]:
    """Classify directly observed components plus their direct neighbours (§30).

    Direction is meaningful: a component that the observed one *depends on* is
    ``DOWNSTREAM_CONTEXT``; a component that depends on it is
    ``UPSTREAM_CONTEXT``. Neither is claimed to be failing.
    """
    # Deduplicate while preserving order: an incident usually has several
    # anomalies on the same component, and one row per anomaly would report the
    # same component many times over.
    observed = list(dict.fromkeys(c for c in observed_ids if c is not None))
    if not observed:
        return []
    views = [
        AffectedComponentView(
            component_id=cid,
            name=None,
            classification="DIRECTLY_OBSERVED",
            reason="has a directly observed anomaly in this incident",
        )
        for cid in observed
    ]
    seen = set(observed)
    dep_stmt = (
        select(
            ComponentDependency.source_component_id,
            ComponentDependency.target_component_id,
        )
        .where(
            or_(
                ComponentDependency.source_component_id.in_(observed),
                ComponentDependency.target_component_id.in_(observed),
            )
        )
        .limit(_MAX_GRAPH_NODES * 4)
    )
    for source, target in (await db.execute(dep_stmt)).all():
        if source in set(observed) and target not in seen:
            seen.add(target)
            views.append(
                AffectedComponentView(
                    component_id=target,
                    name=None,
                    classification="DOWNSTREAM_CONTEXT",
                    reason="is a direct dependency of an observed component",
                )
            )
        elif target in set(observed) and source not in seen:
            seen.add(source)
            views.append(
                AffectedComponentView(
                    component_id=source,
                    name=None,
                    classification="UPSTREAM_CONTEXT",
                    reason="depends directly on an observed component",
                )
            )
    names = await component_names(db, [v.component_id for v in views])
    for view in views:
        view.name = names.get(view.component_id)
    return views


async def build_deployment_context(db: AsyncSession, incident: Incident) -> list[dict]:
    """Deployments within the context window of the incident (temporal only)."""
    if incident.detected_at is None:
        return []
    window = settings.INCIDENT_CONTEXT_WINDOW_SECONDS
    start = incident.detected_at - timedelta(seconds=window)
    end = (incident.resolved_at or incident.detected_at) + timedelta(seconds=window)

    stmt = (
        select(DeploymentEvent)
        .where(
            DeploymentEvent.project_id == incident.project_id,
            DeploymentEvent.deployed_at >= start,
            DeploymentEvent.deployed_at <= end,
        )
        .order_by(DeploymentEvent.deployed_at)
        .limit(_MAX_CONTEXT_ITEMS)
    )
    if incident.environment_id is not None:
        stmt = stmt.where(
            (DeploymentEvent.environment_id == incident.environment_id)
            | (DeploymentEvent.environment_id.is_(None))
        )
    items: list[dict] = []
    earliest = ensure_utc(incident.detected_at)
    for event in (await db.execute(stmt)).scalars().all():
        deployed_at = ensure_utc(event.deployed_at)
        seconds = None
        if earliest and deployed_at:
            seconds = (earliest - deployed_at).total_seconds()
        items.append(
            {
                "deployment_event_id": event.id,
                "deployment_id": event.deployment_id,
                "component_id": event.component_id,
                "version": event.version,
                "deployed_at": deployed_at,
                "status": (
                    event.status.value
                    if hasattr(event.status, "value")
                    else str(event.status)
                ),
                "seconds_before_first_anomaly": seconds,
                "is_context_only": True,
            }
        )
    return items


async def build_configuration_context(
    db: AsyncSession, incident: Incident
) -> list[dict]:
    """Configuration changes near the incident (temporal context only)."""
    if incident.detected_at is None:
        return []
    window = settings.INCIDENT_CONTEXT_WINDOW_SECONDS
    start = incident.detected_at - timedelta(seconds=window)
    end = (incident.resolved_at or incident.detected_at) + timedelta(seconds=window)

    stmt = (
        select(ConfigurationChangeEvent)
        .where(
            ConfigurationChangeEvent.project_id == incident.project_id,
            ConfigurationChangeEvent.timestamp >= start,
            ConfigurationChangeEvent.timestamp <= end,
        )
        .order_by(ConfigurationChangeEvent.timestamp)
        .limit(_MAX_CONTEXT_ITEMS)
    )
    if incident.environment_id is not None:
        stmt = stmt.where(
            (ConfigurationChangeEvent.environment_id == incident.environment_id)
            | (ConfigurationChangeEvent.environment_id.is_(None))
        )
    items: list[dict] = []
    for change in (await db.execute(stmt)).scalars().all():
        items.append(
            {
                "configuration_event_id": change.id,
                "component_id": change.component_id,
                "changed_at": ensure_utc(change.timestamp),
                "summary": change.description or change.change_id,
                "actor": change.source,
                "is_context_only": True,
            }
        )
    return items


__all__ = [
    "AffectedComponentView",
    "GraphContext",
    "build_graph_context",
    "build_dependency_components",
    "build_deployment_context",
    "build_configuration_context",
    "component_names",
]
