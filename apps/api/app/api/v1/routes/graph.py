"""ARGUS Graph API Routes.

Phase 2 knowledge-graph endpoints: graph queries, traversals, snapshots,
environment comparisons, discovery, reconciliation, and endpoints/owners/aliases.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.graph import (
    ComponentOwner,
    DataQualitySeverity,
    DiscoveredComponentStatus,
    GraphDataQualityRecord,
    GraphEdgeSource,
    GraphEdgeStatus,
    GraphEdgeType,
    GraphNodeStatus,
    GraphNodeType,
    GraphSnapshot,
    ReconciliationStatus,
    ServiceEndpoint,
)
from app.models.project import SoftwareProject
from app.models.system import SystemComponent
from app.schemas.graph import (
    AliasCreate,
    AliasList,
    AliasResponse,
    DataQualityList,
    DataQualityResponse,
    DiscoveryList,
    DiscoveryRegister,
    DiscoveryResponse,
    EndpointCreate,
    EndpointList,
    EndpointResponse,
    EnvComparisonResponse,
    GraphData,
    GraphDependenciesResponse,
    GraphEdgeList,
    GraphEdgeResponse,
    GraphHealthResponse,
    GraphNodeList,
    GraphNodeResponse,
    ImpactResponse,
    OwnerCreate,
    OwnerResponse,
    PathResponse,
    ReconcileResponse,
    SearchHit,
    SearchResponse,
    SnapshotCreate,
    SnapshotDetailResponse,
    SnapshotDiffResponse,
    SnapshotList,
    SnapshotResponse,
)
from app.services.endpoint_registry import EndpointRegistry
from app.services.graph_data_quality import GraphDataQualityService
from app.services.graph_discovery import GraphDiscoveryEngine
from app.services.graph_impact import DependencyImpactAnalyzer
from app.services.graph_query_service import GraphQueryService
from app.services.graph_reconciler import GraphReconciler
from app.services.graph_registry import ComponentRegistry
from app.services.graph_snapshot_service import GraphSnapshotService
from app.services.graph_validator import GraphValidator

router = APIRouter(tags=["Graph"])


# Project-scoped graph endpoints


@router.get("/projects/{project_id}/graph", response_model=GraphData)
async def get_project_graph(
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = None,
    node_type: Optional[GraphNodeType] = None,
    status: Optional[GraphNodeStatus] = None,
    edge_type: Optional[GraphEdgeType] = None,
    source: Optional[GraphEdgeSource] = None,
    node_limit: int = Query(500, ge=1, le=1000),
    edge_limit: int = Query(2000, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
) -> GraphData:
    """Get full graph for a project with optional filters."""
    if not await db.get(SoftwareProject, project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    query_svc = GraphQueryService(db)
    nodes, edges = await query_svc.get_graph(
        project_id=project_id,
        environment_id=environment_id,
        node_type=node_type,
        status=status,
        edge_type=edge_type,
        source=source,
        node_limit=node_limit,
        edge_limit=edge_limit,
    )

    return GraphData(
        nodes=[GraphNodeResponse.model_validate(n) for n in nodes],
        edges=[GraphEdgeResponse.model_validate(e) for e in edges],
    )


@router.get("/projects/{project_id}/graph/nodes", response_model=GraphNodeList)
async def list_project_nodes(
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = None,
    node_type: Optional[GraphNodeType] = None,
    status: Optional[GraphNodeStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> GraphNodeList:
    """List graph nodes with pagination."""
    if not await db.get(SoftwareProject, project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    query_svc = GraphQueryService(db)
    nodes, edges = await query_svc.get_graph(
        project_id=project_id,
        environment_id=environment_id,
        node_type=node_type,
        status=status,
        node_limit=10000,
        edge_limit=1,
    )

    total = len(nodes)
    start = (page - 1) * page_size
    end = start + page_size
    page_nodes = nodes[start:end]

    return GraphNodeList(
        items=[GraphNodeResponse.model_validate(n) for n in page_nodes],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.get("/projects/{project_id}/graph/edges", response_model=GraphEdgeList)
async def list_project_edges(
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = None,
    edge_type: Optional[GraphEdgeType] = None,
    source: Optional[GraphEdgeSource] = None,
    status: Optional[GraphEdgeStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> GraphEdgeList:
    """List graph edges with pagination."""
    if not await db.get(SoftwareProject, project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    query_svc = GraphQueryService(db)
    nodes, edges = await query_svc.get_graph(
        project_id=project_id,
        environment_id=environment_id,
        edge_type=edge_type,
        source=source,
        node_limit=1,
        edge_limit=10000,
    )

    # Filter by status if provided (get_graph doesn't support edge status filter)
    if status is not None:
        edges = [e for e in edges if e.status == status]

    total = len(edges)
    start = (page - 1) * page_size
    end = start + page_size
    page_edges = edges[start:end]

    return GraphEdgeList(
        items=[GraphEdgeResponse.model_validate(e) for e in page_edges],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.get("/projects/{project_id}/graph/search", response_model=SearchResponse)
async def search_graph(
    project_id: uuid.UUID,
    q: str = Query(..., min_length=1),
    environment_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> SearchResponse:
    """Search nodes, endpoints, and aliases by name."""
    if not await db.get(SoftwareProject, project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    query_svc = GraphQueryService(db)
    registry = ComponentRegistry(db)

    # Search nodes
    nodes, _ = await query_svc.get_graph(
        project_id=project_id,
        environment_id=environment_id,
        node_limit=1000,
        edge_limit=1,
    )
    q_lower = q.lower()
    node_hits = [
        SearchHit(
            kind="node",
            id=n.id,
            name=n.name,
            project_id=n.project_id,
            environment_id=n.environment_id,
            node_type=n.node_type,
            score=1.0 if q_lower == n.name.lower() else 0.5,
        )
        for n in nodes
        if q_lower in n.name.lower()
    ]

    # Search endpoints
    endpoint_query = select(ServiceEndpoint).where(
        ServiceEndpoint.project_id == project_id
    )
    if environment_id:
        endpoint_query = endpoint_query.where(
            ServiceEndpoint.environment_id == environment_id
        )
    result = await db.execute(endpoint_query)
    endpoints = result.scalars().all()
    endpoint_hits = [
        SearchHit(
            kind="endpoint",
            id=ep.id,
            name=f"{ep.method} {ep.path_template}",
            project_id=ep.project_id,
            environment_id=ep.environment_id,
            score=0.7,
        )
        for ep in endpoints
        if q_lower in ep.path_template.lower() or q_lower in ep.method.lower()
    ]

    # Search aliases
    alias_matches = await registry.search_aliases(project_id, q)
    alias_hits = [
        SearchHit(
            kind="alias",
            id=a.id,
            name=a.alias,
            project_id=a.project_id,
            node_type=None,
            score=0.8,
        )
        for a in alias_matches[:20]
    ]

    all_hits = sorted(node_hits + endpoint_hits + alias_hits, key=lambda h: -h.score)
    return SearchResponse(query=q, results=all_hits[:50])


@router.get("/projects/{project_id}/graph/paths", response_model=PathResponse)
async def find_path_between_nodes(
    project_id: uuid.UUID,
    source_id: uuid.UUID = Query(...),
    target_id: uuid.UUID = Query(...),
    max_depth: int = Query(10, ge=1, le=25),
    max_nodes: int = Query(2000, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
) -> PathResponse:
    """Find shortest path between two nodes.

    ``source_id``/``target_id`` accept either graph node IDs or canonical
    component IDs (resolved through the overlay mapping).
    """
    if not await db.get(SoftwareProject, project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    # Clamp traversal bounds
    max_depth = min(max_depth, 25)
    max_nodes = min(max_nodes, 2000)

    # Resolve canonical component IDs to their graph nodes when needed.
    registry = ComponentRegistry(db)
    source_node = await registry.get_node_by_entity(
        project_id, "system_component", source_id
    )
    target_node = await registry.get_node_by_entity(
        project_id, "system_component", target_id
    )
    if source_node is not None:
        source_id = source_node.id
    if target_node is not None:
        target_id = target_node.id

    query_svc = GraphQueryService(db)
    path_result = await query_svc.find_path(
        source_id=source_id,
        target_id=target_id,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )

    return PathResponse(
        found=path_result.found,
        path=[GraphNodeResponse.model_validate(n) for n in path_result.nodes],
        edges=[GraphEdgeResponse.model_validate(e) for e in path_result.edges],
        total_hops=path_result.hops,
    )


# Snapshots


@router.get("/projects/{project_id}/graph/snapshots", response_model=SnapshotList)
async def list_snapshots(
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> SnapshotList:
    """List graph snapshots."""
    if not await db.get(SoftwareProject, project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    snapshot_svc = GraphSnapshotService(db)
    snapshots, total = await snapshot_svc.list_snapshots(
        project_id=project_id,
        environment_id=environment_id,
        page=page,
        page_size=page_size,
    )

    return SnapshotList(
        items=[SnapshotResponse.model_validate(s) for s in snapshots],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.post(
    "/projects/{project_id}/graph/snapshots",
    response_model=SnapshotResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_snapshot(
    project_id: uuid.UUID,
    snapshot_data: SnapshotCreate,
    db: AsyncSession = Depends(get_db),
) -> GraphSnapshot:
    """Create a graph snapshot."""
    if not await db.get(SoftwareProject, project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    snapshot_svc = GraphSnapshotService(db)
    try:
        snapshot = await snapshot_svc.create_snapshot(
            project_id=project_id,
            environment_id=snapshot_data.environment_id,
            source=snapshot_data.source,
            caption=snapshot_data.caption,
        )
        await db.commit()
        await db.refresh(snapshot)
        return snapshot
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/projects/{project_id}/graph/environments/compare",
    response_model=EnvComparisonResponse,
)
async def compare_environments(
    project_id: uuid.UUID,
    environment_a: uuid.UUID = Query(...),
    environment_b: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> EnvComparisonResponse:
    """Compare two environments of the same project."""
    if not await db.get(SoftwareProject, project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    query_svc = GraphQueryService(db)
    comparison = await query_svc.compare_environments(
        project_id=project_id, environment_a=environment_a, environment_b=environment_b
    )
    return comparison


@router.post("/projects/{project_id}/graph/reconcile", response_model=ReconcileResponse)
async def reconcile_graph(
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> ReconcileResponse:
    """Run graph reconciliation to sync canonical models to graph."""
    if not await db.get(SoftwareProject, project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    registry = ComponentRegistry(db)
    reconciler = GraphReconciler(db, registry)
    result = await reconciler.reconcile(
        project_id=project_id, environment_id=environment_id
    )
    await db.commit()

    run_row = await reconciler.get_last_run(project_id)
    return ReconcileResponse(
        reconciliation_run_id=result.run_id,
        project_id=project_id,
        environment_id=environment_id,
        input_source=result.source,
        nodes_created=result.nodes_created,
        edges_created=result.edges_created,
        edges_updated=result.edges_updated,
        edges_marked_stale=result.edges_marked_stale,
        errors=result.errors,
        status=run_row.status if run_row else ReconciliationStatus.SUCCESS,
        started_at=(run_row.started_at if run_row else datetime.now(timezone.utc)),
        finished_at=run_row.finished_at if run_row else None,
    )


@router.get("/projects/{project_id}/graph/health", response_model=GraphHealthResponse)
async def get_graph_health(
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> GraphHealthResponse:
    """Get graph health and data quality summary."""
    if not await db.get(SoftwareProject, project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    validator = GraphValidator(db)
    quality_svc = GraphDataQualityService(db, validator)
    health = await quality_svc.run_and_get_health(
        project_id=project_id, environment_id=environment_id
    )
    await db.commit()
    return health


@router.get("/projects/{project_id}/graph/discovery", response_model=DiscoveryList)
async def list_discovery_records(
    project_id: uuid.UUID,
    status_filter: Optional[DiscoveredComponentStatus] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> DiscoveryList:
    """List discovery records (weak-evidence component suggestions)."""
    if not await db.get(SoftwareProject, project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    registry = ComponentRegistry(db)
    discovery = GraphDiscoveryEngine(db, registry)
    records, total = await discovery.list_records(
        project_id=project_id, status=status_filter, page=page, page_size=page_size
    )

    return DiscoveryList(
        items=[DiscoveryResponse.model_validate(r) for r in records],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.post(
    "/projects/{project_id}/graph/discovery/{record_id}/register",
    response_model=GraphNodeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_discovery(
    project_id: uuid.UUID,
    record_id: uuid.UUID,
    register_data: DiscoveryRegister,
    db: AsyncSession = Depends(get_db),
) -> GraphNodeResponse:
    """Register a discovery record as a real graph node."""
    if not await db.get(SoftwareProject, project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    registry = ComponentRegistry(db)
    discovery = GraphDiscoveryEngine(db, registry)
    try:
        node = await discovery.register(
            record_id=record_id,
            name=register_data.name,
            node_type=register_data.node_type,
        )
        await db.commit()
        await db.refresh(node)
        return GraphNodeResponse.model_validate(node)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post(
    "/projects/{project_id}/graph/discovery/{record_id}/ignore", status_code=204
)
async def ignore_discovery(
    project_id: uuid.UUID,
    record_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Mark a discovery record as ignored."""
    if not await db.get(SoftwareProject, project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    registry = ComponentRegistry(db)
    discovery = GraphDiscoveryEngine(db, registry)
    try:
        await discovery.ignore(record_id)
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/projects/{project_id}/graph/data-quality", response_model=DataQualityList)
async def list_data_quality_records(
    project_id: uuid.UUID,
    severity: Optional[DataQualitySeverity] = None,
    environment_id: Optional[uuid.UUID] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> DataQualityList:
    """List persisted graph data-quality findings (newest first)."""
    if not await db.get(SoftwareProject, project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    query = select(GraphDataQualityRecord).where(
        GraphDataQualityRecord.project_id == project_id
    )
    count_query = select(func.count(GraphDataQualityRecord.id)).where(
        GraphDataQualityRecord.project_id == project_id
    )
    if severity is not None:
        query = query.where(GraphDataQualityRecord.severity == severity)
        count_query = count_query.where(GraphDataQualityRecord.severity == severity)
    if environment_id is not None:
        query = query.where(GraphDataQualityRecord.environment_id == environment_id)
        count_query = count_query.where(
            GraphDataQualityRecord.environment_id == environment_id
        )

    total = (await db.execute(count_query)).scalar() or 0
    rows = (
        (
            await db.execute(
                query.order_by(GraphDataQualityRecord.detected_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    return DataQualityList(
        items=[DataQualityResponse.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.get("/projects/{project_id}/graph/endpoints", response_model=EndpointList)
async def list_project_endpoints(
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> EndpointList:
    """List service endpoints for a project."""
    if not await db.get(SoftwareProject, project_id):
        raise HTTPException(status_code=404, detail="Project not found")

    query = select(ServiceEndpoint).where(ServiceEndpoint.project_id == project_id)
    count_query = select(func.count(ServiceEndpoint.id)).where(
        ServiceEndpoint.project_id == project_id
    )

    if environment_id:
        query = query.where(ServiceEndpoint.environment_id == environment_id)
        count_query = count_query.where(
            ServiceEndpoint.environment_id == environment_id
        )

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    endpoints = result.scalars().all()

    return EndpointList(
        items=[EndpointResponse.model_validate(ep) for ep in endpoints],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


# Global graph endpoints


@router.get("/graph/snapshots/{snapshot_id}", response_model=SnapshotDetailResponse)
async def get_snapshot_detail(
    snapshot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SnapshotDetailResponse:
    """Get snapshot with full node/edge contents."""
    snapshot_svc = GraphSnapshotService(db)
    try:
        detail = await snapshot_svc.snapshot_contents(snapshot_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if not detail:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return detail


@router.get("/graph/snapshots/{a_id}/diff/{b_id}", response_model=SnapshotDiffResponse)
async def diff_snapshots(
    a_id: uuid.UUID,
    b_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SnapshotDiffResponse:
    """Compare two snapshots."""
    snapshot_svc = GraphSnapshotService(db)
    try:
        diff = await snapshot_svc.diff_snapshots(a_id, b_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if not diff:
        raise HTTPException(status_code=404, detail="One or both snapshots not found")
    return diff


# Component-scoped graph endpoints


@router.get(
    "/components/{component_id}/graph/dependencies",
    response_model=GraphDependenciesResponse,
)
async def get_component_dependencies(
    component_id: uuid.UUID,
    transitive: bool = False,
    max_depth: int = Query(10, ge=1, le=25),
    max_nodes: int = Query(2000, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
) -> GraphDependenciesResponse:
    """Get dependencies (downstream) of a component."""
    component = await db.get(SystemComponent, component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Component not found")

    # Clamp traversal bounds
    max_depth = min(max_depth, 25)
    max_nodes = min(max_nodes, 2000)

    registry = ComponentRegistry(db)
    node = await registry.get_node_by_entity(
        component.project_id, "system_component", component_id
    )
    if not node:
        raise HTTPException(
            status_code=404, detail="Component has no corresponding graph node"
        )

    query_svc = GraphQueryService(db)
    result = await query_svc.get_dependencies(
        node_id=node.id, transitive=transitive, max_depth=max_depth, max_nodes=max_nodes
    )

    return GraphDependenciesResponse(
        component_id=component_id,
        direction="outgoing",
        direct=[GraphNodeResponse.model_validate(n) for n in result.direct],
        transitive=[GraphNodeResponse.model_validate(n) for n in result.transitive],
        direct_count=len(result.direct),
        transitive_count=len(result.transitive),
    )


@router.get(
    "/components/{component_id}/graph/dependents",
    response_model=GraphDependenciesResponse,
)
async def get_component_dependents(
    component_id: uuid.UUID,
    transitive: bool = False,
    max_depth: int = Query(10, ge=1, le=25),
    max_nodes: int = Query(2000, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
) -> GraphDependenciesResponse:
    """Get dependents (upstream) of a component."""
    component = await db.get(SystemComponent, component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Component not found")

    # Clamp traversal bounds
    max_depth = min(max_depth, 25)
    max_nodes = min(max_nodes, 2000)

    registry = ComponentRegistry(db)
    node = await registry.get_node_by_entity(
        component.project_id, "system_component", component_id
    )
    if not node:
        raise HTTPException(
            status_code=404, detail="Component has no corresponding graph node"
        )

    query_svc = GraphQueryService(db)
    result = await query_svc.get_dependents(
        node_id=node.id, transitive=transitive, max_depth=max_depth, max_nodes=max_nodes
    )

    return GraphDependenciesResponse(
        component_id=component_id,
        direction="incoming",
        direct=[GraphNodeResponse.model_validate(n) for n in result.direct],
        transitive=[GraphNodeResponse.model_validate(n) for n in result.transitive],
        direct_count=len(result.direct),
        transitive_count=len(result.transitive),
    )


@router.get(
    "/components/{component_id}/graph/neighbors",
    response_model=GraphDependenciesResponse,
)
async def get_component_neighbors(
    component_id: uuid.UUID,
    max_depth: int = Query(1, ge=1, le=25),
    max_nodes: int = Query(500, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
) -> GraphDependenciesResponse:
    """Get neighbors (both directions) of a component."""
    component = await db.get(SystemComponent, component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Component not found")

    # Clamp traversal bounds
    max_depth = min(max_depth, 25)
    max_nodes = min(max_nodes, 2000)

    registry = ComponentRegistry(db)
    node = await registry.get_node_by_entity(
        component.project_id, "system_component", component_id
    )
    if not node:
        raise HTTPException(
            status_code=404, detail="Component has no corresponding graph node"
        )

    query_svc = GraphQueryService(db)
    result = await query_svc.get_neighbors(
        node_id=node.id, max_depth=max_depth, max_nodes=max_nodes
    )

    return GraphDependenciesResponse(
        component_id=component_id,
        direction="outgoing",
        direct=[GraphNodeResponse.model_validate(n) for n in result.direct],
        transitive=[GraphNodeResponse.model_validate(n) for n in result.transitive],
        direct_count=len(result.direct),
        transitive_count=len(result.transitive),
    )


@router.get("/components/{component_id}/graph/impact", response_model=ImpactResponse)
async def get_component_impact(
    component_id: uuid.UUID,
    max_depth: int = Query(10, ge=1, le=25),
    max_nodes: int = Query(2000, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
) -> ImpactResponse:
    """Analyze downstream dependency impact."""
    component = await db.get(SystemComponent, component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Component not found")

    # Clamp traversal bounds
    max_depth = min(max_depth, 25)
    max_nodes = min(max_nodes, 2000)

    registry = ComponentRegistry(db)
    node = await registry.get_node_by_entity(
        component.project_id, "system_component", component_id
    )
    if not node:
        raise HTTPException(
            status_code=404, detail="Component has no corresponding graph node"
        )

    query_svc = GraphQueryService(db)
    impact_analyzer = DependencyImpactAnalyzer(db, query_svc)
    impact = await impact_analyzer.analyze_downstream(
        node_id=node.id, max_depth=max_depth, max_nodes=max_nodes
    )
    return impact


@router.get("/components/{component_id}/endpoints", response_model=EndpointList)
async def list_component_endpoints(
    component_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> EndpointList:
    """List endpoints for a component."""
    component = await db.get(SystemComponent, component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Component not found")

    query = select(ServiceEndpoint).where(ServiceEndpoint.component_id == component_id)
    count_query = select(func.count(ServiceEndpoint.id)).where(
        ServiceEndpoint.component_id == component_id
    )

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    endpoints = result.scalars().all()

    return EndpointList(
        items=[EndpointResponse.model_validate(ep) for ep in endpoints],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.post(
    "/components/{component_id}/endpoints",
    response_model=EndpointResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_component_endpoint(
    component_id: uuid.UUID,
    endpoint_data: EndpointCreate,
    db: AsyncSession = Depends(get_db),
) -> ServiceEndpoint:
    """Create an endpoint for a component."""
    component = await db.get(SystemComponent, component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Component not found")

    registry = EndpointRegistry(db)
    endpoint = await registry.record_endpoint(
        project_id=component.project_id,
        component_id=component_id,
        method=endpoint_data.method,
        path=endpoint_data.path,
        is_external=endpoint_data.is_external,
        environment_id=endpoint_data.environment_id,
        metadata_=endpoint_data.metadata_,
    )
    await db.commit()
    await db.refresh(endpoint)
    return endpoint


@router.get("/components/{component_id}/owner", response_model=OwnerResponse)
async def get_component_owner(
    component_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ComponentOwner:
    """Get component ownership information."""
    component = await db.get(SystemComponent, component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Component not found")

    result = await db.execute(
        select(ComponentOwner).where(ComponentOwner.component_id == component_id)
    )
    owner = result.scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=404, detail="Owner information not found")
    return owner


@router.put(
    "/components/{component_id}/owner",
    response_model=OwnerResponse,
    status_code=status.HTTP_201_CREATED,
)
async def set_component_owner(
    component_id: uuid.UUID,
    owner_data: OwnerCreate,
    db: AsyncSession = Depends(get_db),
) -> ComponentOwner:
    """Set or update component ownership."""
    component = await db.get(SystemComponent, component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Component not found")

    result = await db.execute(
        select(ComponentOwner).where(ComponentOwner.component_id == component_id)
    )
    owner = result.scalar_one_or_none()

    if owner:
        # Update existing
        for field, value in owner_data.model_dump(exclude_unset=True).items():
            setattr(owner, field, value)
    else:
        # Create new
        owner = ComponentOwner(
            project_id=component.project_id,
            component_id=component_id,
            **owner_data.model_dump(),
        )
        db.add(owner)

    await db.commit()
    await db.refresh(owner)
    return owner


@router.get("/components/{component_id}/aliases", response_model=AliasList)
async def list_component_aliases(
    component_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> AliasList:
    """List aliases for a component."""
    component = await db.get(SystemComponent, component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Component not found")

    registry = ComponentRegistry(db)
    node = await registry.get_node_by_entity(
        component.project_id, "system_component", component_id
    )
    if not node:
        raise HTTPException(
            status_code=404, detail="Component has no corresponding graph node"
        )

    aliases = await registry.list_aliases(node_id=node.id)
    aliases.sort(key=lambda a: a.alias)  # deterministic pagination order
    total = len(aliases)
    start = (page - 1) * page_size
    page_items = aliases[start : start + page_size]

    return AliasList(
        items=[AliasResponse.model_validate(a) for a in page_items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.post(
    "/components/{component_id}/aliases",
    response_model=AliasResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_component_alias(
    component_id: uuid.UUID,
    alias_data: AliasCreate,
    db: AsyncSession = Depends(get_db),
) -> AliasResponse:
    """Create an alias for a component."""
    component = await db.get(SystemComponent, component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Component not found")

    registry = ComponentRegistry(db)
    node = await registry.get_node_by_entity(
        component.project_id, "system_component", component_id
    )
    if not node:
        raise HTTPException(
            status_code=404, detail="Component has no corresponding graph node"
        )

    alias = await registry.add_alias(
        node_id=node.id,
        alias=alias_data.alias,
        project_id=component.project_id,
        source=alias_data.source,
        confidence=alias_data.confidence,
    )
    await db.commit()
    await db.refresh(alias)
    return AliasResponse.model_validate(alias)
