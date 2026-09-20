"""ARGUS Graph Component Registry.

Central node/alias/owner accessors for the software knowledge graph. Keeps
``graph_nodes`` consistent with the canonical Phase 0/1 store: every
``SystemComponent`` / ``Environment`` / ``SoftwareProject`` / ``CodeRepository``
mirrors to exactly one node keyed by ``(project_id, entity_kind, entity_id)``
(the ``uq_graph_nodes_project_entity`` unique constraint). Metadata merges
shallowly on conflict so evidence from multiple sources accumulates without
duplicate rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deployment import CodeRepository
from app.models.graph import (
    ComponentOwner,
    GraphCriticality,
    GraphEdgeSource,
    GraphNode,
    GraphNodeAlias,
    GraphNodeStatus,
    GraphNodeType,
)
from app.models.project import Environment, SoftwareProject
from app.models.system import ComponentCategory, SystemComponent
from app.schemas.graph import OwnerCreate

#: §7 — canonical ``ComponentCategory`` projected onto typed graph nodes.
#: Categories with an exact ``GraphNodeType`` twin map directly; ``FRONTEND``
#: presents as ``APPLICATION``; anything unmapped stays generic ``COMPONENT``.
_CATEGORY_NODE_TYPES: Dict[ComponentCategory, GraphNodeType] = {
    category: GraphNodeType[category.name]
    for category in ComponentCategory
    if category.name in GraphNodeType.__members__
}
_CATEGORY_NODE_TYPES[ComponentCategory.FRONTEND] = GraphNodeType.APPLICATION


def node_type_for_category(category: Optional[ComponentCategory]) -> GraphNodeType:
    """Return the §7 node type for a canonical component category."""
    if category is None:
        return GraphNodeType.COMPONENT
    return _CATEGORY_NODE_TYPES.get(category, GraphNodeType.COMPONENT)


#: Mutable fields accepted by :meth:`ComponentRegistry.update_node`. Kept
#: explicit so the API cannot drift the graph node into a state that
#: contradicts its canonical mirror.
_NODE_MUTABLE_FIELDS = frozenset(
    {
        "name",
        "description",
        "status",
        "criticality",
        "external_identifier",
        "language",
        "framework",
        "runtime",
        "version",
        "repository_url",
        "documentation_url",
        "ownership_team",
        "metadata_",
    }
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ComponentRegistry:
    """Graph-node CRUD, alias resolution, and component ownership."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Core upsert
    # ------------------------------------------------------------------
    async def _upsert_node(
        self,
        *,
        node_type: GraphNodeType,
        entity_kind: str,
        entity_id: uuid.UUID,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
        name: str,
        description: Optional[str] = None,
        metadata_: Optional[dict] = None,
        last_seen: bool = True,
    ) -> Tuple[GraphNode, bool]:
        """Upsert a node by identity key; returns ``(node, created)``."""
        existing = await self.get_node_by_entity(project_id, entity_kind, entity_id)
        if existing is not None:
            existing.name = name
            if description is not None:
                existing.description = description
            if environment_id is not None:
                existing.environment_id = environment_id
            if metadata_:
                merged = dict(existing.metadata_ or {})
                merged.update(metadata_)
                existing.metadata_ = merged
            if last_seen:
                existing.last_seen_at = _now()
            return existing, False

        row = GraphNode(
            project_id=project_id,
            environment_id=environment_id,
            node_type=node_type,
            entity_kind=entity_kind,
            entity_id=entity_id,
            name=name,
            description=description,
            status=GraphNodeStatus.ACTIVE,
            criticality=GraphCriticality.UNKNOWN,
            metadata_=metadata_,
            first_seen_at=None if not last_seen else _now(),
            last_seen_at=None if not last_seen else _now(),
        )
        try:
            self._db.add(row)
            await self._db.flush()
            await self._db.refresh(row)
        except IntegrityError:
            # Concurrent insert lost the race — reuse the winner (rare).
            await self._db.rollback()
            existing = await self.get_node_by_entity(project_id, entity_kind, entity_id)
            if existing is not None:
                return existing, False
            raise
        return row, True

    async def get_or_create_project_node(self, project_id: uuid.UUID) -> GraphNode:
        """Mirror a project as ``PROJECT`` node (anchors the graph)."""
        project = await self._db.get(SoftwareProject, project_id)
        if project is None:
            raise ValueError(f"SoftwareProject {project_id} not found")
        node, _ = await self._upsert_node(
            node_type=GraphNodeType.PROJECT,
            entity_kind="project",
            entity_id=project_id,
            project_id=project_id,
            name=project.name,
            description=project.description,
        )
        return node

    async def get_or_create_environment_node(
        self,
        environment_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> GraphNode:
        """Mirror an environment as ``ENVIRONMENT`` node."""
        env = await self._db.get(Environment, environment_id)
        if env is None:
            raise ValueError(f"Environment {environment_id} not found")
        node, _ = await self._upsert_node(
            node_type=GraphNodeType.ENVIRONMENT,
            entity_kind="environment",
            entity_id=environment_id,
            project_id=project_id,
            environment_id=environment_id,
            name=env.name,
        )
        return node

    async def get_or_create_component_node(
        self, component: SystemComponent
    ) -> GraphNode:
        """Mirror a canonical system component as a typed graph node.

        §7: the node type projects the canonical ``ComponentCategory``
        (SERVICE/DATABASE/CACHE/...); unknown categories stay ``COMPONENT``.
        Copies descriptive fields and pulls supplementary ownership /
        repository info that the canonical model does not carry.
        """
        owner = await self.get_owner(component.id)
        repo_url = (component.metadata_ or {}).get("repository_url")
        node, _ = await self._upsert_node(
            node_type=node_type_for_category(component.component_type),
            entity_kind="system_component",
            entity_id=component.id,
            project_id=component.project_id,
            environment_id=component.environment_id,
            name=component.name,
            description=component.description,
            metadata_={
                "source_status": component.status.value if component.status else None
            },
        )
        node.ownership_team = owner.team if owner else None
        node.repository_url = repo_url or None
        return node

    async def get_or_create_repository_node(self, repo: CodeRepository) -> GraphNode:
        """Mirror a code repository as a ``REPOSITORY`` node."""
        provider = repo.provider
        basename = repo.repository_url.rstrip("/").rsplit("/", 1)[-1]
        name = f"{provider}/{basename}" if basename else provider
        node, _ = await self._upsert_node(
            node_type=GraphNodeType.REPOSITORY,
            entity_kind="repository",
            entity_id=repo.id,
            project_id=repo.project_id,
            name=name[:255],
            description=None,
            metadata_={"repository_url": repo.repository_url},
        )
        node.repository_url = repo.repository_url
        return node

    async def mirror_node(
        self,
        *,
        node_type: GraphNodeType,
        entity_kind: str,
        entity_id: uuid.UUID,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
        name: str,
        description: Optional[str] = None,
        metadata_: Optional[dict] = None,
    ) -> Tuple[GraphNode, bool]:
        """Reconciler write path — upsert a mirror node, return ``(node, created)``."""
        return await self._upsert_node(
            node_type=node_type,
            entity_kind=entity_kind,
            entity_id=entity_id,
            project_id=project_id,
            environment_id=environment_id,
            name=name,
            description=description,
            metadata_=metadata_,
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def get_node_by_id(self, node_id: uuid.UUID) -> Optional[GraphNode]:
        return await self._db.get(GraphNode, node_id)

    async def get_node_by_entity(
        self,
        project_id: uuid.UUID,
        entity_kind: str,
        entity_id: uuid.UUID,
    ) -> Optional[GraphNode]:
        row = await self._db.execute(
            select(GraphNode).where(
                GraphNode.project_id == project_id,
                GraphNode.entity_kind == entity_kind,
                GraphNode.entity_id == entity_id,
            )
        )
        return row.scalar_one_or_none()

    async def list_nodes(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
        node_type: Optional[GraphNodeType] = None,
        status: Optional[GraphNodeStatus] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[GraphNode], int]:
        """List nodes scoped to a project with optional filters."""
        stmt = select(GraphNode)
        count = select(func.count(GraphNode.id))
        where = [GraphNode.project_id == project_id]
        if environment_id is not None:
            where.append(GraphNode.environment_id == environment_id)
        if node_type is not None:
            where.append(GraphNode.node_type == node_type)
        if status is not None:
            where.append(GraphNode.status == status)
        stmt = stmt.where(*where)
        count = count.where(*where)
        total = (await self._db.execute(count)).scalar() or 0
        stmt = (
            stmt.order_by(GraphNode.name.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = list((await self._db.execute(stmt)).scalars().all())
        return items, int(total)

    async def update_node(
        self, node_id: uuid.UUID, updates: dict
    ) -> Optional[GraphNode]:
        """Apply a whitelisted subset of mutable fields to a node."""
        node = await self.get_node_by_id(node_id)
        if node is None:
            return None
        for key, value in updates.items():
            if key in _NODE_MUTABLE_FIELDS:
                setattr(node, key, value)
        if "metadata_" in updates:
            merged = dict(node.metadata_ or {})
            try:
                merged.update(updates["metadata_"] or {})
            except TypeError:
                merged = dict(node.metadata_ or {})
            node.metadata_ = merged
        return node

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------
    async def resolve_component_node(
        self,
        project_id: uuid.UUID,
        component_id_provided: uuid.UUID,
    ) -> Optional[GraphNode]:
        """Resolve a graph node from either a canonical ``system_components.id``
        or a graph node id."""
        component = await self._db.get(SystemComponent, component_id_provided)
        if component is not None and component.project_id == project_id:
            return await self.get_node_by_entity(
                project_id, "system_component", component.id
            )
        node = await self.get_node_by_id(component_id_provided)
        if node is not None and node.project_id == project_id:
            return node
        return None

    async def resolve_component_node_by_name(
        self, project_id: uuid.UUID, name: str
    ) -> Optional[GraphNode]:
        """Resolve a node by exact name, case-insensitive name, or alias."""
        cleaned = name.strip()
        if not cleaned:
            return None

        stmt = select(GraphNode).where(
            GraphNode.project_id == project_id,
            func.lower(GraphNode.name) == func.lower(cleaned),
        )
        row = await self._db.execute(stmt)
        node = row.scalar_one_or_none()
        if node is not None:
            return node

        return await self.resolve_node_by_alias(project_id, cleaned)

    # ------------------------------------------------------------------
    # Aliases
    # ------------------------------------------------------------------
    async def add_alias(
        self,
        node_id: uuid.UUID,
        alias: str,
        *,
        project_id: uuid.UUID,
        source: GraphEdgeSource = GraphEdgeSource.INFERENCE,
        confidence: Optional[float] = None,
    ) -> GraphNodeAlias:
        """Add a node alias (normalized lower-case); upsert on conflict."""
        normalized = alias.strip().lower()
        existing = await self._db.execute(
            select(GraphNodeAlias).where(
                GraphNodeAlias.node_id == node_id,
                GraphNodeAlias.alias == normalized,
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            row.source = source
            row.confidence = confidence
            return row
        obj = GraphNodeAlias(
            project_id=project_id,
            node_id=node_id,
            alias=normalized,
            source=source,
            confidence=confidence,
        )
        try:
            self._db.add(obj)
            await self._db.flush()
            await self._db.refresh(obj)
        except IntegrityError:
            await self._db.rollback()
            return await self.add_alias(
                node_id,
                normalized,
                project_id=project_id,
                source=source,
                confidence=confidence,
            )
        return obj

    async def list_aliases(self, node_id: uuid.UUID) -> List[GraphNodeAlias]:
        rows = await self._db.execute(
            select(GraphNodeAlias).where(GraphNodeAlias.node_id == node_id)
        )
        return list(rows.scalars().all())

    async def delete_alias(self, node_id: uuid.UUID, alias: str) -> bool:
        normalized = alias.strip().lower()
        rows = await self._db.execute(
            select(GraphNodeAlias).where(
                GraphNodeAlias.node_id == node_id,
                GraphNodeAlias.alias == normalized,
            )
        )
        obj = rows.scalar_one_or_none()
        if obj is None:
            return False
        await self._db.delete(obj)
        return True

    async def resolve_node_by_alias(
        self, project_id: uuid.UUID, alias: str
    ) -> Optional[GraphNode]:
        """Resolve a node whose alias matches (case-insensitive), project-scoped."""
        normalized = alias.strip().lower()
        if not normalized:
            return None
        rows = await self._db.execute(
            select(GraphNode)
            .join(GraphNodeAlias, GraphNodeAlias.node_id == GraphNode.id)
            .where(
                GraphNode.project_id == project_id,
                func.lower(GraphNodeAlias.alias) == func.lower(normalized),
            )
        )
        node = rows.scalar_one_or_none()
        if node is not None:
            return node
        # Fall back to a direct name match.
        name_rows = await self._db.execute(
            select(GraphNode).where(
                GraphNode.project_id == project_id,
                func.lower(GraphNode.name) == func.lower(normalized),
            )
        )
        return name_rows.scalar_one_or_none()

    async def search_aliases(
        self, project_id: uuid.UUID, query: str, limit: int = 50
    ) -> List[GraphNodeAlias]:
        """Case-insensitive substring search over aliases, project-scoped."""
        pattern = f"%{query.strip().lower()}%"
        rows = await self._db.execute(
            select(GraphNodeAlias)
            .join(GraphNode, GraphNode.id == GraphNodeAlias.node_id)
            .where(
                GraphNode.project_id == project_id,
                func.lower(GraphNodeAlias.alias).like(pattern),
            )
            .order_by(GraphNodeAlias.alias)
            .limit(limit)
        )
        return list(rows.scalars().all())

    # ------------------------------------------------------------------
    # Ownership
    # ------------------------------------------------------------------
    async def set_owner(
        self, component_id: uuid.UUID, owner: OwnerCreate
    ) -> ComponentOwner:
        """Upsert ownership for a component (one row per component)."""
        project = await self._db.execute(
            select(SystemComponent.project_id).where(SystemComponent.id == component_id)
        )
        project_id = project.scalar_one_or_none()
        if project_id is None:
            raise ValueError(f"SystemComponent {component_id} not found")

        existing = await self._db.execute(
            select(ComponentOwner).where(ComponentOwner.component_id == component_id)
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            row.team = owner.team
            row.owner_name = owner.owner_name
            row.contact_email = (
                str(owner.contact_email) if owner.contact_email else None
            )
            row.repository_owner = owner.repository_owner
            return row

        obj = ComponentOwner(
            project_id=project_id,
            component_id=component_id,
            team=owner.team,
            owner_name=owner.owner_name,
            contact_email=str(owner.contact_email) if owner.contact_email else None,
            repository_owner=owner.repository_owner,
        )
        try:
            self._db.add(obj)
            await self._db.flush()
            await self._db.refresh(obj)
        except IntegrityError:
            await self._db.rollback()
            return await self.set_owner(component_id, owner)
        return obj

    async def get_owner(self, component_id: uuid.UUID) -> Optional[ComponentOwner]:
        rows = await self._db.execute(
            select(ComponentOwner).where(ComponentOwner.component_id == component_id)
        )
        return rows.scalar_one_or_none()
