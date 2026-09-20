"""ARGUS Graph Reconciler.

Derives the graph overlay from the canonical Phase 0/1 store and maintains
edge provenance. ``GraphReconciler.upsert_edge`` is the single write path for
graph edges — every producer (configuration mirror, trace extractor, log
references, deployment events, repository edges) funnels through it so the
``source`` provenance and ``metadata_`` evidence semantics stay consistent:

- **Configured-wins**: edges whose ``source`` is ``MANUAL`` or
  ``CONFIGURATION`` are never overwritten by observed evidence — only their
  ``metadata_`` evidence and confidence grow.
- **Stale, never deleted**: edges stop being refreshed become ``STALE``; a
  disappearing canonical dependency does not delete the edge (history kept).

Reconciliation is idempotent and records each pass in
``graph_reconciliation_runs``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Literal, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.graph import (
    GraphEdge,
    GraphEdgeSource,
    GraphEdgeStatus,
    GraphEdgeType,
    GraphNode,
    GraphNodeType,
    GraphReconciliationRun,
    ReconciliationStatus,
)
from app.models.project import Environment, SoftwareProject
from app.models.system import ComponentDependency, SystemComponent
from app.services.graph_registry import ComponentRegistry, node_type_for_category

#: Provenance precedence — higher wins when a non-configured edge is upgraded
#: by stronger evidence.
_SOURCE_PRECEDENCE: Dict[GraphEdgeSource, int] = {
    GraphEdgeSource.TRACE: 5,
    GraphEdgeSource.LOG: 4,
    GraphEdgeSource.DEPLOYMENT: 3,
    GraphEdgeSource.REPOSITORY: 2,
    GraphEdgeSource.INFERENCE: 1,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ReconcileResult:
    """Outcome of one reconciliation pass."""

    run_id: uuid.UUID
    nodes_created: int = 0
    edges_created: int = 0
    edges_updated: int = 0
    edges_marked_stale: int = 0
    errors: List[str] = field(default_factory=list)
    source: GraphEdgeSource = GraphEdgeSource.CONFIGURATION


class GraphReconciler:
    """Mirror canonical entities into the graph; maintain edge provenance."""

    def __init__(
        self,
        db: AsyncSession,
        registry: ComponentRegistry,
        *extractors: object,
        stale_after_days: Optional[int] = None,
    ) -> None:
        self._db = db
        self._registry = registry
        self._extractors = extractors
        self._stale_after_days = (
            stale_after_days or get_settings().GRAPH_STALE_AFTER_DAYS
        )

    # ------------------------------------------------------------------
    # Edge write path (single funnel for every producer)
    # ------------------------------------------------------------------
    async def upsert_edge(
        self,
        *,
        project_id: uuid.UUID,
        source_node_id: uuid.UUID,
        target_node_id: uuid.UUID,
        edge_type: GraphEdgeType,
        source: GraphEdgeSource,
        confidence: float = 1.0,
        environment_id: Optional[uuid.UUID] = None,
        dependency_type: Optional[str] = None,
        metadata_: Optional[dict] = None,
        evidence_sources_seen: Optional[Sequence[str]] = None,
    ) -> Tuple[GraphEdge, Literal["created", "updated_nop", "updated_evidence"]]:
        """Create or refresh an edge under the shared provenance policy."""
        stmt = select(GraphEdge).where(
            GraphEdge.project_id == project_id,
            GraphEdge.source_node_id == source_node_id,
            GraphEdge.target_node_id == target_node_id,
            GraphEdge.edge_type == edge_type,
        )
        if environment_id is None:
            stmt = stmt.where(GraphEdge.environment_id.is_(None))
        else:
            stmt = stmt.where(GraphEdge.environment_id == environment_id)
        existing = (await self._db.execute(stmt)).scalar_one_or_none()

        if existing is None:
            incoming = [source.value, *(evidence_sources_seen or [])]
            edge = GraphEdge(
                project_id=project_id,
                environment_id=environment_id,
                source_node_id=source_node_id,
                target_node_id=target_node_id,
                edge_type=edge_type,
                dependency_type=dependency_type,
                confidence=confidence,
                source=source,
                status=GraphEdgeStatus.ACTIVE,
                metadata_={
                    "sources": incoming,
                    "first_source": incoming[0],
                    "last_evidence_at": _now().isoformat(),
                },
                first_seen_at=_now(),
                last_seen_at=_now(),
            )
            if metadata_:
                edge.metadata_ = {**(edge.metadata_ or {}), **metadata_}
            self._db.add(edge)
            await self._db.flush()
            await self._db.refresh(edge)
            return edge, "created"

        now = _now()
        changed_evidence = False
        incoming = [source.value, *(evidence_sources_seen or [])]
        sources_seen = list((existing.metadata_ or {}).get("sources", []))
        for src in incoming:
            if src not in sources_seen:
                sources_seen.append(src)
                changed_evidence = True

        existing.metadata_ = dict(existing.metadata_ or {})
        existing.metadata_["sources"] = sources_seen
        existing.metadata_["last_evidence_at"] = now.isoformat()
        if existing.metadata_.get("first_source") is None:
            existing.metadata_["first_source"] = source.value

        if existing.confidence is None or confidence > existing.confidence:
            existing.confidence = confidence
            changed_evidence = True

        # Configured-wins: never downgrade a MANUAL/CONFIGURATION edge.
        if existing.source in (GraphEdgeSource.MANUAL, GraphEdgeSource.CONFIGURATION):
            existing.last_seen_at = now
            mode: Literal["updated_nop", "updated_evidence"] = (
                "updated_evidence" if changed_evidence else "updated_nop"
            )
            return existing, mode

        # Observed edges may be upgraded by stronger evidence.
        if _SOURCE_PRECEDENCE.get(source, 0) >= _SOURCE_PRECEDENCE.get(
            existing.source, 0
        ):
            if existing.source != source:
                existing.source = source
                changed_evidence = True
        if existing.status == GraphEdgeStatus.STALE:
            existing.status = GraphEdgeStatus.ACTIVE
            changed_evidence = True
        existing.last_seen_at = now
        mode = "updated_evidence" if changed_evidence else "updated_nop"
        return existing, mode

    # ------------------------------------------------------------------
    # Stale policy
    # ------------------------------------------------------------------
    async def _mark_stale_edges(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
        stale_after_days: int,
    ) -> int:
        """Flag ACTIVE edges without fresh evidence as STALE (never delete)."""
        threshold = _now() - timedelta(days=stale_after_days)
        stmt = select(GraphEdge).where(
            GraphEdge.project_id == project_id,
            GraphEdge.status == GraphEdgeStatus.ACTIVE,
            GraphEdge.last_seen_at < threshold,
        )
        if environment_id is not None:
            stmt = stmt.where(GraphEdge.environment_id == environment_id)
        rows = (await self._db.execute(stmt)).scalars().all()
        count = 0
        for row in rows:
            row.status = GraphEdgeStatus.STALE
            count += 1
        return count

    async def _env_node(self, env: Environment, project_id: uuid.UUID) -> GraphNode:
        node, _ = await self._registry.mirror_node(
            node_type=GraphNodeType.ENVIRONMENT,
            entity_kind="environment",
            entity_id=env.id,
            project_id=project_id,
            environment_id=env.id,
            name=env.name,
        )
        return node

    async def _component_node(
        self, comp: SystemComponent, project_id: uuid.UUID
    ) -> Tuple[GraphNode, bool]:
        return await self._registry.mirror_node(
            node_type=node_type_for_category(comp.component_type),
            entity_kind="system_component",
            entity_id=comp.id,
            project_id=project_id,
            environment_id=comp.environment_id,
            name=comp.name,
            description=comp.description,
        )

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------
    async def reconcile(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
        run_source: GraphEdgeSource = GraphEdgeSource.CONFIGURATION,
    ) -> ReconcileResult:
        """Mirror canonical project state into the graph, then run extractors.

        Idempotent: mirroring is a set of upserts keyed on entity identity, so
        a second pass creates no duplicate nodes/edges.
        """
        project = await self._db.get(SoftwareProject, project_id)
        if project is None:
            raise ValueError(f"SoftwareProject {project_id} not found")

        run = GraphReconciliationRun(
            project_id=project_id,
            environment_id=environment_id,
            started_at=_now(),
            input_source=run_source,
        )
        self._db.add(run)
        await self._db.flush()

        result = ReconcileResult(run_id=run.id, source=run_source)
        minted: set[Tuple[uuid.UUID, uuid.UUID, str, Optional[uuid.UUID]]] = set()

        def _on_edge(
            line: Tuple[uuid.UUID, uuid.UUID, str, Optional[uuid.UUID]], mode: str
        ) -> None:
            if mode == "created":
                if line not in minted:
                    minted.add(line)
                    result.edges_created += 1
            elif mode == "updated_evidence":
                result.edges_updated += 1

        try:
            # Anchor: project node.
            _, project_created = await self._registry.mirror_node(
                node_type=GraphNodeType.PROJECT,
                entity_kind="project",
                entity_id=project_id,
                project_id=project_id,
                name=project.name,
                description=project.description,
            )
            if project_created:
                result.nodes_created += 1

            # Environments + project CONTAINS edges.
            env_rows = await self._db.execute(
                select(Environment)
                .where(Environment.project_id == project_id)
                .order_by(Environment.name)
            )
            envs: List[Environment] = list(env_rows.scalars().all())
            project_node = await self._registry.get_node_by_entity(
                project_id, "project", project_id
            )
            for env in envs:
                if environment_id is not None and env.id != environment_id:
                    continue
                env_node, env_created = await self._registry.mirror_node(
                    node_type=GraphNodeType.ENVIRONMENT,
                    entity_kind="environment",
                    entity_id=env.id,
                    project_id=project_id,
                    environment_id=env.id,
                    name=env.name,
                )
                if env_created:
                    result.nodes_created += 1
                if project_node is not None:
                    _, mode = await self.upsert_edge(
                        project_id=project_id,
                        source_node_id=project_node.id,
                        target_node_id=env_node.id,
                        edge_type=GraphEdgeType.CONTAINS,
                        source=GraphEdgeSource.CONFIGURATION,
                        confidence=1.0,
                        environment_id=env.id,
                    )
                    _on_edge(
                        (
                            project_node.id,
                            env_node.id,
                            GraphEdgeType.CONTAINS.value,
                            env.id,
                        ),
                        mode,
                    )

            # Components + environment CONTAINS edges.
            comp_stmt = select(SystemComponent).where(
                SystemComponent.project_id == project_id
            )
            if environment_id is not None:
                comp_stmt = comp_stmt.where(
                    SystemComponent.environment_id == environment_id
                )
            comp_rows = (
                (await self._db.execute(comp_stmt.order_by(SystemComponent.name)))
                .scalars()
                .all()
            )
            for comp in comp_rows:
                node, comp_created = await self._component_node(comp, project_id)
                if comp_created:
                    result.nodes_created += 1
                if comp.environment_id is not None:
                    env_node = await self._env_node(
                        await self._db.get(Environment, comp.environment_id),  # type: ignore[arg-type]
                        project_id,
                    )
                    # Semantics: ENVIRONMENT CONTAINS COMPONENT (the container
                    # is the source). Inverting this would make every
                    # dependency traversal "contain" its whole environment.
                    _, mode = await self.upsert_edge(
                        project_id=project_id,
                        source_node_id=env_node.id,
                        target_node_id=node.id,
                        edge_type=GraphEdgeType.CONTAINS,
                        source=GraphEdgeSource.CONFIGURATION,
                        confidence=1.0,
                        environment_id=comp.environment_id,
                    )
                    _on_edge(
                        (
                            env_node.id,
                            node.id,
                            GraphEdgeType.CONTAINS.value,
                            comp.environment_id,
                        ),
                        mode,
                    )

            # Dependencies -> DEPENDS_ON edges (only when both ends exist).
            dep_stmt = (
                select(ComponentDependency)
                .join(
                    SystemComponent,
                    SystemComponent.id == ComponentDependency.source_component_id,
                )
                .where(SystemComponent.project_id == project_id)
            )
            deps = (await self._db.execute(dep_stmt)).scalars().all()
            for dep in deps:
                src_comp = await self._db.get(SystemComponent, dep.source_component_id)
                tgt_comp = await self._db.get(SystemComponent, dep.target_component_id)
                if src_comp is None or tgt_comp is None:
                    continue
                src_node, _ = await self._component_node(src_comp, project_id)
                tgt_node, _ = await self._component_node(tgt_comp, project_id)
                env_id = src_comp.environment_id
                _, mode = await self.upsert_edge(
                    project_id=project_id,
                    source_node_id=src_node.id,
                    target_node_id=tgt_node.id,
                    edge_type=GraphEdgeType.DEPENDS_ON,
                    source=GraphEdgeSource.CONFIGURATION,
                    confidence=1.0,
                    dependency_type=(
                        dep.dependency_type.value
                        if hasattr(dep.dependency_type, "value")
                        else str(dep.dependency_type)
                    ),
                    environment_id=env_id,
                )
                _on_edge(
                    (src_node.id, tgt_node.id, GraphEdgeType.DEPENDS_ON.value, env_id),
                    mode,
                )

            # Stale sweep.
            result.edges_marked_stale += await self._mark_stale_edges(
                project_id=project_id,
                environment_id=environment_id,
                stale_after_days=self._stale_after_days,
            )

            # Component discovery (§36): unresolved telemetry names become
            # PENDING discovery records — suggestions only, never auto-nodes.
            try:
                from app.services.graph_discovery import GraphDiscoveryEngine

                discovery_engine = GraphDiscoveryEngine(self._db, self._registry)
                await discovery_engine.suggest(
                    project_id=project_id, environment_id=environment_id
                )
            except Exception as exc:  # noqa: BLE001 - discovery is advisory
                result.errors.append(f"discovery scan failed: {exc}")

            run.nodes_created = result.nodes_created
            run.edges_created = result.edges_created
            run.edges_updated = result.edges_updated
            run.edges_marked_stale = result.edges_marked_stale
            run.errors = result.errors or None
            run.status = ReconciliationStatus.SUCCESS
            run.finished_at = _now()
            await self._db.flush()
            return result

        except Exception as exc:  # pragma: no cover - defensive record
            run.status = ReconciliationStatus.FAILED
            run.errors = [str(exc)]
            run.finished_at = _now()
            await self._db.flush()
            raise

    async def get_last_run(
        self, project_id: uuid.UUID
    ) -> Optional[GraphReconciliationRun]:
        row = await self._db.execute(
            select(GraphReconciliationRun)
            .where(GraphReconciliationRun.project_id == project_id)
            .order_by(GraphReconciliationRun.started_at.desc())
            .limit(1)
        )
        return row.scalar_one_or_none()
