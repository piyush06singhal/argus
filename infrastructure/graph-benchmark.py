#!/usr/bin/env python3
"""ARGUS graph benchmark (§67).

Builds a synthetic software knowledge graph at two scales (100 nodes/500
edges, 1000 nodes/5000 edges) and measures the deterministic graph
operations: retrieval, dependency traversal, path finding, and impact
closure. Results are informational — they do not claim production-scale
performance.

Usage:
    python infrastructure/graph-benchmark.py              # temp SQLite
    DATABASE_URL=... python infrastructure/graph-benchmark.py --postgres
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "api"))


def _setup_database_url() -> None:
    """Point DATABASE_URL at a temp SQLite file unless --postgres is passed.

    Must run BEFORE importing ``app.core.database`` — the async engine is
    created at import time from the resolved settings.
    """
    parser = argparse.ArgumentParser(description="ARGUS graph benchmark")
    parser.add_argument(
        "--postgres",
        action="store_true",
        help="Use DATABASE_URL (default: temp SQLite file)",
    )
    args, _ = parser.parse_known_args()
    if not args.postgres:
        fd, path = tempfile.mkstemp(suffix=".bench.db")
        os.close(fd)
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"


_setup_database_url()

from sqlalchemy import select  # noqa: E402

from app.core.database import Base, async_session_factory, engine  # noqa: E402
from app.models.graph import GraphEdge, GraphNode  # noqa: E402
from app.models.project import Environment, SoftwareProject  # noqa: E402
from app.models.system import (  # noqa: E402
    ComponentCategory,
    ComponentDependency,
    ComponentStatus,
    DependencyType,
    SystemComponent,
)
from app.services.graph_impact import DependencyImpactAnalyzer  # noqa: E402
from app.services.graph_query_service import GraphQueryService  # noqa: E402
from app.services.graph_registry import ComponentRegistry  # noqa: E402
from app.services.graph_reconciler import GraphReconciler  # noqa: E402

RESULTS: List[Tuple[str, float]] = []


async def _init_schema() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def synthesize(n_nodes: int, n_edges: int) -> Tuple[uuid.UUID, List[uuid.UUID]]:
    """Create a synthetic project + component graph; return (project_id, ids)."""
    async with async_session_factory() as db:
        project = SoftwareProject(
            name=f"Bench {n_nodes}",
            slug=f"bench-{n_nodes}-{uuid.uuid4().hex[:8]}",
            description="Synthetic benchmark project",
        )
        db.add(project)
        await db.flush()
        env = Environment(
            project_id=project.id,
            name="production",
            environment_type="PRODUCTION",
        )
        db.add(env)
        await db.flush()

        components: List[SystemComponent] = []
        for i in range(n_nodes):
            category = (
                ComponentCategory.SERVICE
                if i % 5 != 4
                else ComponentCategory.DATABASE
            )
            components.append(
                SystemComponent(
                    project_id=project.id,
                    environment_id=env.id,
                    name=f"component-{i:05d}",
                    component_type=category,
                    status=ComponentStatus.HEALTHY,
                )
            )
            db.add(components[-1])
        await db.flush()

        # Chain (guarantees connectivity) + random extra edges (deterministic).
        rng = random.Random(42)
        edges: List[Tuple[int, int]] = []
        for i in range(1, n_nodes):
            edges.append((i, rng.randrange(i)))  # child -> some ancestor
        seen = set(edges)
        while len(edges) < n_edges:
            a = rng.randrange(n_nodes)
            b = rng.randrange(n_nodes)
            if a != b and (a, b) not in seen:
                seen.add((a, b))
                edges.append((a, b))
        for a, b in edges:
            db.add(
                ComponentDependency(
                    source_component_id=components[a].id,
                    target_component_id=components[b].id,
                    dependency_type=DependencyType.HTTP,
                )
            )
        await db.commit()

        # Materialize the graph overlay through the real pipeline.
        registry = ComponentRegistry(db)
        reconciler = GraphReconciler(db, registry)
        await reconciler.reconcile(project_id=project.id)
        await db.commit()
        return project.id, [c.id for c in components]


async def _node_id_for(component_id: uuid.UUID) -> uuid.UUID:
    async with async_session_factory() as db:
        row = (
            await db.execute(
                select(GraphNode).where(
                    GraphNode.entity_kind == "system_component",
                    GraphNode.entity_id == component_id,
                )
            )
        ).scalar_one()
        return row.id


async def bench_graph_retrieval(project_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        start = time.perf_counter()
        nodes = (
            (
                await db.execute(
                    select(GraphNode).where(GraphNode.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )
        edges = (
            (
                await db.execute(
                    select(GraphEdge).where(GraphEdge.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )
        elapsed = (time.perf_counter() - start) * 1000
        RESULTS.append((f"graph retrieval ({len(nodes)}n/{len(edges)}e)", elapsed))


async def bench_dependency_traversal(node_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        svc = GraphQueryService(db)
        start = time.perf_counter()
        await svc.get_dependencies(
            node_id=node_id, transitive=True, max_depth=10, max_nodes=2000
        )
        RESULTS.append(
            ("dependency traversal (transitive, depth 10)", (time.perf_counter() - start) * 1000)
        )


async def bench_find_path(source_component: uuid.UUID, target_component: uuid.UUID) -> None:
    source = await _node_id_for(source_component)
    target = await _node_id_for(target_component)
    async with async_session_factory() as db:
        svc = GraphQueryService(db)
        start = time.perf_counter()
        await svc.find_path(source_id=source, target_id=target, max_depth=25)
        RESULTS.append(
            ("find_path (BFS, depth cap 25)", (time.perf_counter() - start) * 1000)
        )


async def bench_impact_closure(node_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        analyzer = DependencyImpactAnalyzer(db, GraphQueryService(db))
        start = time.perf_counter()
        await analyzer.analyze_downstream(node_id=node_id, max_depth=25, max_nodes=2000)
        RESULTS.append(
            ("impact closure (downstream, depth 25)", (time.perf_counter() - start) * 1000)
        )


async def run_scale(n_nodes: int, n_edges: int) -> None:
    print(f"\n=== Scale: {n_nodes} components / {n_edges} edges ===")
    start = time.perf_counter()
    project_id, component_ids = await synthesize(n_nodes, n_edges)
    RESULTS.append(
        (f"synthesize + reconcile ({n_nodes}c/{n_edges}d)", (time.perf_counter() - start) * 1000)
    )
    await bench_graph_retrieval(project_id)
    await bench_dependency_traversal(await _node_id_for(component_ids[0]))
    await bench_find_path(component_ids[0], component_ids[-1])
    await bench_impact_closure(await _node_id_for(component_ids[-1]))


def main() -> int:
    asyncio.run(_init_schema())
    asyncio.run(run_scale(100, 500))
    asyncio.run(run_scale(1000, 5000))
    asyncio.run(engine.dispose())

    print("\n=== Results ===")
    print(f"{'operation':<46} {'ms':>10}")
    for label, ms in RESULTS:
        print(f"{label:<46} {ms:>10.1f}")

    print(
        "\nNote: informational benchmark on synthetic data — not a claim of "
        "production-scale performance."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
