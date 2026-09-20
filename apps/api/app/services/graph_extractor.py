"""ARGUS Graph Extractor.

Turns *observed* telemetry and deployment/repository records into typed graph
edges with strict provenance. Every extraction proof is a ``TRACE``, ``LOG``,
``DEPLOYMENT``, or ``REPOSITORY`` source edge written through
``GraphReconciler.upsert_edge``, so observed evidence can never override a
configured/manual edge, and metadata is redacted before persist.

The trace path is deterministic and loop-free: parent/child span pairs whose
components differ yield one directed edge (parent-component → child-component),
typed by the target's effective node kind (database/cache → ``READS_FROM``,
queue → ``PUBLISHES_TO``, external API → ``CALLS``, else ``CALLS``). Missing
or orphan parents are handled without fabricating edges.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deployment import CodeRepository, DeploymentEvent, DeploymentStatus
from app.models.graph import (
    GraphEdgeSource,
    GraphEdgeType,
    GraphNode,
    GraphNodeType,
)
from app.models.observability import LogRecord, ObservabilityEvent, SpanRecord
from app.models.project import Environment
from app.models.system import ComponentCategory, SystemComponent
from app.services.endpoint_registry import EndpointRegistry
from app.services.graph_reconciler import GraphReconciler
from app.services.graph_registry import ComponentRegistry
from app.services.redaction import RedactionEngine


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get(meta: Optional[dict], *keys: str) -> Optional[str]:
    """First non-None string value among candidate keys in a metadata dict."""
    if not meta:
        return None
    for key in keys:
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _log_contains_reference(meta: Optional[dict]) -> Optional[str]:
    """A component-name reference in a log's metadata, if any (redacted keys skipped)."""
    if not meta:
        return None
    for key in ("peer_service", "upstream_component", "downstream_component"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            lowered = key.lower()
            if any(
                skip in lowered for skip in ("secret", "token", "password", "api_key")
            ):
                continue
            return value.strip()
    return None


#: Component category -> effective node-type key for trace edge typing.
_CATEGORY_TO_NODE_TYPE: Dict[ComponentCategory, GraphNodeType] = {
    ComponentCategory.DATABASE: GraphNodeType.DATABASE,
    ComponentCategory.CACHE: GraphNodeType.CACHE,
    ComponentCategory.QUEUE: GraphNodeType.QUEUE,
    ComponentCategory.EXTERNAL_API: GraphNodeType.EXTERNAL_API,
}


#: Trace metadata keys that reveal the *actual* dependency kind.
_METHOD_KEYS = ("http.request.method", "http.method")
_ROUTE_KEYS = ("http.route", "url.path", "http.target")
_DEPENDENCY_HINT_KEYS: Dict[str, str] = {
    "db.system": "DATABASE",
    "db.name": "DATABASE",
    "messaging.system": "QUEUE",
    "rpc.system": "RPC",
    "cache.system": "CACHE",
}


@dataclass
class ExtractionStats:
    """Outcome of one extraction pass."""

    spans_seen: int = 0
    edges_created: int = 0
    edges_updated: int = 0
    distinct_edges: Set[str] = field(default_factory=set)
    endpoints_recorded: int = 0

    @property
    def total(self) -> int:
        return self.edges_created + self.edges_updated


class GraphExtractor:
    """Derive graph edges from observed telemetry and deployment records."""

    #: Target-kind -> edge type (component category consulted first).
    _EDGE_TYPE_BY_TARGET: Dict[GraphNodeType, GraphEdgeType] = {
        GraphNodeType.DATABASE: GraphEdgeType.READS_FROM,
        GraphNodeType.CACHE: GraphEdgeType.READS_FROM,
        GraphNodeType.QUEUE: GraphEdgeType.PUBLISHES_TO,
        GraphNodeType.EXTERNAL_API: GraphEdgeType.CALLS,
    }

    def __init__(
        self,
        db: AsyncSession,
        registry: ComponentRegistry,
        edge_policy: Optional[Callable[..., GraphEdgeType]] = None,
        reconciler: Optional[GraphReconciler] = None,
    ) -> None:
        self._db = db
        self._registry = registry
        self._edge_policy = edge_policy
        self._reconciler = reconciler or GraphReconciler(db, registry)
        self._endpoints = EndpointRegistry(db)
        self._redactor = RedactionEngine()

    # ------------------------------------------------------------------
    # Trace -> graph
    # ------------------------------------------------------------------
    async def extract_trace_graph(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
        spans: Sequence[SpanRecord],
        events: Sequence[ObservabilityEvent] = (),
    ) -> ExtractionStats:
        """Materialize typed edges from a batch of spans and/or trace events.

        Both ends of every edge must be resolvable SystemComponents. Edges are
        deduped on (source, target, edge_type, environment). Late children
        (parent ingested earlier) are resolved against the DB.

        Two evidence shapes are accepted (identical edge semantics):
        - ``spans``: Phase 0/1 ``SpanRecord`` rows with explicit parent chains.
        - ``events``: OTLP-ingested spans stored as TRACE-typed
          ``ObservabilityEvent`` rows whose payloads carry ``trace_id`` /
          ``span_id`` / ``parent_span_id``.
        """
        stats = ExtractionStats(spans_seen=len(spans) + len(events))

        batch_ids = {s.span_id for s in spans}
        missing_parents = {
            s.parent_span_id
            for s in spans
            if s.parent_span_id and s.parent_span_id not in batch_ids
        }
        spans_by_id: Dict[str, SpanRecord] = {s.span_id: s for s in spans}
        if missing_parents:
            rows = await self._db.execute(
                select(SpanRecord).where(SpanRecord.span_id.in_([*missing_parents]))
            )
            for span in rows.scalars().all():
                # Prefer the in-batch copy; keep DB-resolved parents for traversal.
                spans_by_id.setdefault(span.span_id, span)

        parents_by_id = spans_by_id
        # Traversal must include DB-resolved parents so a late child (whose
        # parent was ingested in an earlier batch) still yields an edge.
        all_spans = list(spans_by_id.values())

        # Orphan-aware roots: no parent, or parent unknown even after DB lookup.
        known_ids = set(parents_by_id) | batch_ids
        children: Dict[str, List[SpanRecord]] = {}
        for span in all_spans:
            if span.parent_span_id in known_ids:
                children.setdefault(span.parent_span_id, []).append(span)

        roots = [
            s
            for s in all_spans
            if s.parent_span_id not in known_ids or s.parent_span_id is None
        ]

        # Ensure root ordering is deterministic.
        roots.sort(key=lambda s: (s.start_time, s.span_id))

        order: List[SpanRecord] = []
        visited: set[str] = set()
        queue: List[SpanRecord] = list(roots)
        while queue:
            current = queue.pop(0)
            if current.span_id in visited:
                continue
            visited.add(current.span_id)
            order.append(current)
            queue.extend(children.get(current.span_id, []))

        for parent in order:
            for child in children.get(parent.span_id, []):
                await self._maybe_trace_edge(
                    stats, project_id, environment_id, parent, child
                )

        # OTLP evidence: trace-shaped ObservabilityEvent payloads.
        if events:
            await self._extract_event_edges(
                stats=stats,
                project_id=project_id,
                environment_id=environment_id,
                events=events,
            )

        return stats

    async def _extract_event_edges(
        self,
        *,
        stats: ExtractionStats,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
        events: Sequence[ObservabilityEvent],
    ) -> None:
        """Edges from OTLP-style TRACE events (payload-borne parent chains)."""
        # Group by trace_id, build parent/child pairs from the payloads.
        by_trace: Dict[str, List[ObservabilityEvent]] = {}
        for event in events:
            payload = event.payload or {}
            span_id = payload.get("span_id")
            trace_id = payload.get("trace_id")
            if not span_id or not trace_id:
                continue
            by_trace.setdefault(str(trace_id), []).append(event)

        for trace_id, group in sorted(by_trace.items()):
            events_by_span: Dict[str, ObservabilityEvent] = {
                str(e.payload.get("span_id")): e  # type: ignore[union-attr]
                for e in group
            }
            for child_event in group:
                payload = child_event.payload or {}
                parent_span_id = payload.get("parent_span_id")
                if not parent_span_id:
                    continue
                parent_event = events_by_span.get(str(parent_span_id))
                if parent_event is None:
                    continue  # orphan/late parent — nothing to fabricate
                await self._maybe_event_edge(
                    stats=stats,
                    project_id=project_id,
                    environment_id=environment_id,
                    trace_id=str(trace_id),
                    parent=parent_event,
                    child=child_event,
                )

    async def _maybe_event_edge(
        self,
        *,
        stats: ExtractionStats,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
        trace_id: str,
        parent: ObservabilityEvent,
        child: ObservabilityEvent,
    ) -> None:
        """One CALLS edge per cross-component parent/child event pair."""
        if not parent.component_id or not child.component_id:
            return
        if parent.component_id == child.component_id:
            return
        src_comp = await self._db.get(SystemComponent, parent.component_id)
        tgt_comp = await self._db.get(SystemComponent, child.component_id)
        if src_comp is None or tgt_comp is None:
            return
        if src_comp.project_id != project_id or tgt_comp.project_id != project_id:
            return

        src_node = await self._registry.get_or_create_component_node(src_comp)
        tgt_node = await self._registry.get_or_create_component_node(tgt_comp)
        edge_type = self._effective_edge_type(tgt_comp, tgt_node)
        dependency_type = self._infer_dependency_type(child.payload)

        _, mode = await self._reconciler.upsert_edge(
            project_id=project_id,
            source_node_id=src_node.id,
            target_node_id=tgt_node.id,
            edge_type=edge_type,
            source=GraphEdgeSource.TRACE,
            confidence=0.9,
            environment_id=environment_id,
            dependency_type=dependency_type,
            metadata_={
                "evidence_kind": "parent_child_span_event",
                "trace_id": trace_id,
            },
        )
        key = (src_node.id, tgt_node.id, edge_type.value, environment_id)
        stats.distinct_edges.add(str(key))
        if mode == "created":
            stats.edges_created += 1
        elif mode == "updated_evidence":
            stats.edges_updated += 1

    async def _maybe_trace_edge(
        self,
        stats: ExtractionStats,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
        parent: SpanRecord,
        child: SpanRecord,
    ) -> None:
        if not parent.component_id or not child.component_id:
            return
        if parent.component_id == child.component_id:
            return
        src_comp = await self._db.get(SystemComponent, parent.component_id)
        tgt_comp = await self._db.get(SystemComponent, child.component_id)
        if src_comp is None or tgt_comp is None:
            return
        if src_comp.project_id != project_id or tgt_comp.project_id != project_id:
            return

        src_node = await self._registry.get_or_create_component_node(src_comp)
        tgt_node = await self._registry.get_or_create_component_node(tgt_comp)
        edge_type = self._effective_edge_type(tgt_comp, tgt_node)
        dependency_type = self._infer_dependency_type(child.metadata_)

        _, mode = await self._reconciler.upsert_edge(
            project_id=project_id,
            source_node_id=src_node.id,
            target_node_id=tgt_node.id,
            edge_type=edge_type,
            source=GraphEdgeSource.TRACE,
            confidence=0.9,
            environment_id=environment_id,
            dependency_type=dependency_type,
            metadata_={
                "evidence_kind": "parent_child_span",
                "trace_id": child.trace_id or parent.trace_id or "",
            },
        )
        key = (src_node.id, tgt_node.id, edge_type.value, environment_id)
        stats.distinct_edges.add(str(key))
        if mode == "created":
            stats.edges_created += 1
        elif mode == "updated_evidence":
            stats.edges_updated += 1

    def _effective_edge_type(
        self, component: SystemComponent, node: GraphNode
    ) -> GraphEdgeType:
        category_type = _CATEGORY_TO_NODE_TYPE.get(component.component_type)
        key = category_type or node.node_type
        if self._edge_policy is not None:
            return self._edge_policy(key, component)  # type: ignore[operator]
        return self._EDGE_TYPE_BY_TARGET.get(key, GraphEdgeType.CALLS)

    def _infer_dependency_type(self, meta: Optional[dict]) -> Optional[str]:
        if not meta:
            return None
        explicit = _get(meta, "dependency_type")
        if explicit:
            return explicit.upper()
        for key, kind in _DEPENDENCY_HINT_KEYS.items():
            if _get(meta, key):
                return kind
        if _get(meta, *_METHOD_KEYS) or _get(meta, *_ROUTE_KEYS):
            return "HTTP"
        return None

    async def _record_spans(self, spans: Sequence[SpanRecord]) -> int:
        """Placeholder hook for span-level bookkeeping (kept minimal)."""
        return len(spans)

    # ------------------------------------------------------------------
    # Trace -> endpoint records
    # ------------------------------------------------------------------
    async def extract_endpoint_refs(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
        spans: Sequence[SpanRecord],
    ) -> ExtractionStats:
        """Capture http.route / url.path references into the endpoint registry."""
        stats = ExtractionStats(spans_seen=len(spans))
        for span in spans:
            method = _get(span.metadata_, *_METHOD_KEYS)
            route = _get(span.metadata_, *_ROUTE_KEYS)
            if not span.component_id:
                continue
            if not route:
                continue
            method = method or "GET"
            await self._endpoints.record_endpoint(
                project_id=project_id,
                component_id=span.component_id,
                method=method,
                path=route,
                environment_id=environment_id,
                is_external=False,
                metadata_=self._redactor.redact(span.metadata_ or {}),
            )
            stats.endpoints_recorded += 1
        return stats

    # ------------------------------------------------------------------
    # Log references -> edges
    # ------------------------------------------------------------------
    async def extract_log_references(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
        limit: int = 500,
    ) -> ExtractionStats:
        """Turn log peer/upstream/downstream references into ``LOG`` edges."""
        stats = ExtractionStats()
        stmt = (
            select(LogRecord)
            .where(LogRecord.project_id == project_id, LogRecord.metadata_.isnot(None))
            .order_by(LogRecord.timestamp.desc())
            .limit(limit)
        )
        if environment_id is not None:
            stmt = stmt.where(LogRecord.environment_id == environment_id)
        rows = (await self._db.execute(stmt)).scalars().all()
        seen: set[tuple] = set()
        for log in rows:
            if log.component_id is None:
                continue
            reference = _log_contains_reference(log.metadata_)
            if not reference:
                continue
            target = await self._registry.resolve_component_node_by_name(
                project_id, reference
            )
            if target is None:
                continue
            src_comp = await self._db.get(SystemComponent, log.component_id)
            if src_comp is None:
                continue
            src_node = await self._registry.get_or_create_component_node(src_comp)
            key = (src_node.id, target.id, environment_id)
            if key in seen:
                continue
            seen.add(key)
            _, mode = await self._reconciler.upsert_edge(
                project_id=project_id,
                source_node_id=src_node.id,
                target_node_id=target.id,
                edge_type=GraphEdgeType.CALLS,
                source=GraphEdgeSource.LOG,
                confidence=0.7,
                environment_id=environment_id,
                metadata_={
                    "evidence_kind": "log_reference",
                    "reference_field": reference,
                    "last_seen_evidence_at": _now_iso(),
                },
            )
            if mode == "created":
                stats.edges_created += 1
                stats.distinct_edges.add(str(key))
            elif mode == "updated_evidence":
                stats.edges_updated += 1
        return stats

    # ------------------------------------------------------------------
    # Deployment events -> edges
    # ------------------------------------------------------------------
    async def extract_deployment_edges(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
    ) -> ExtractionStats:
        """Edge component DEPLOYED_AS environment for live deployments."""
        stats = ExtractionStats()
        stmt = (
            select(DeploymentEvent)
            .where(
                DeploymentEvent.project_id == project_id,
                DeploymentEvent.component_id.isnot(None),
                DeploymentEvent.environment_id.isnot(None),
            )
            .order_by(DeploymentEvent.deployed_at.desc())
        )
        if environment_id is not None:
            stmt = stmt.where(DeploymentEvent.environment_id == environment_id)
        events = (await self._db.execute(stmt)).scalars().all()
        live: List[DeploymentEvent] = [
            e
            for e in events
            if e.status in (DeploymentStatus.SUCCESS, DeploymentStatus.STARTED)
        ]
        visited: set[tuple] = set()
        for event in live:
            if event.component_id is None or event.environment_id is None:
                continue
            src_node = await self._registry.resolve_component_node(
                project_id, event.component_id
            )
            if src_node is None:
                continue
            tgt_node = await self._registry.get_or_create_environment_node(
                event.environment_id, project_id
            )
            key = (src_node.id, tgt_node.id, event.environment_id)
            if key in visited:
                continue
            visited.add(key)
            _, mode = await self._reconciler.upsert_edge(
                project_id=project_id,
                source_node_id=src_node.id,
                target_node_id=tgt_node.id,
                edge_type=GraphEdgeType.DEPLOYED_AS,
                source=GraphEdgeSource.DEPLOYMENT,
                confidence=1.0,
                environment_id=event.environment_id,
                metadata_={
                    "deployment_id": event.deployment_id,
                    "version": event.version or "",
                    "last_deployed_at": event.deployed_at.isoformat(),
                },
            )
            if mode == "created":
                stats.edges_created += 1
                stats.distinct_edges.add(str(key))
            elif mode == "updated_evidence":
                stats.edges_updated += 1
        return stats

    # ------------------------------------------------------------------
    # Repository -> IMPLEMENTS component edges
    # ------------------------------------------------------------------
    async def extract_repository_edges(
        self, *, project_id: uuid.UUID
    ) -> ExtractionStats:
        """Link a repository to the component it implements, when the component
        declares the same ``repository_url`` in its metadata."""
        stats = ExtractionStats()
        repos = (
            (
                await self._db.execute(
                    select(CodeRepository).where(
                        CodeRepository.project_id == project_id
                    )
                )
            )
            .scalars()
            .all()
        )
        comps = (
            (
                await self._db.execute(
                    select(SystemComponent).where(
                        SystemComponent.project_id == project_id
                    )
                )
            )
            .scalars()
            .all()
        )

        by_url: Dict[str, SystemComponent] = {}
        for comp in comps:
            url = (comp.metadata_ or {}).get("repository_url")
            if isinstance(url, str) and url.strip():
                by_url[self._strip_scheme(url.strip())] = comp

        for repo in repos:
            repo_key = self._strip_scheme(repo.repository_url)
            matched = by_url.get(repo_key)
            if matched is None:
                continue
            repo_node = await self._registry.get_or_create_repository_node(repo)
            comp_node = await self._registry.get_or_create_component_node(matched)
            _, mode = await self._reconciler.upsert_edge(
                project_id=project_id,
                source_node_id=repo_node.id,
                target_node_id=comp_node.id,
                edge_type=GraphEdgeType.IMPLEMENTS,
                source=GraphEdgeSource.REPOSITORY,
                confidence=0.9,
                metadata_={
                    "repository_url": repo.repository_url,
                    "last_repo_evidence_at": _now_iso(),
                },
            )
            key = (repo_node.id, comp_node.id)
            if mode == "created":
                stats.edges_created += 1
                stats.distinct_edges.add(str(key))
            elif mode == "updated_evidence":
                stats.edges_updated += 1
        return stats

    @staticmethod
    def _strip_scheme(url: str) -> str:
        if "://" in url:
            return url.split("://", 1)[1].rstrip("/")
        return url.rstrip("/")

    # ------------------------------------------------------------------
    # Project container edges
    # ------------------------------------------------------------------
    async def extract_project_container(self, project_id: uuid.UUID) -> ExtractionStats:
        """Ensure project -> environment CONTAINS edges exist."""
        stats = ExtractionStats()
        project_node = await self._registry.get_or_create_project_node(project_id)
        env_rows = await self._db.execute(
            select(Environment).where(Environment.project_id == project_id)
        )
        for env in env_rows.scalars().all():
            env_node = await self._registry.get_or_create_environment_node(
                env.id, project_id
            )
            _, mode = await self._reconciler.upsert_edge(
                project_id=project_id,
                source_node_id=project_node.id,
                target_node_id=env_node.id,
                edge_type=GraphEdgeType.CONTAINS,
                source=GraphEdgeSource.CONFIGURATION,
                confidence=1.0,
                environment_id=env.id,
            )
            if mode == "created":
                stats.edges_created += 1
            elif mode == "updated_evidence":
                stats.edges_updated += 1
        return stats
