"""Phase 2 — graph data-quality: validator checks + health aggregation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deployment import CodeRepository
from app.models.graph import (
    DataQualitySeverity,
    GraphDataQualityRecord,
    GraphEdge,
    GraphEdgeSource,
    GraphEdgeStatus,
    GraphEdgeType,
    GraphNode,
    GraphNodeStatus,
    GraphNodeType,
    ServiceEndpoint,
)
from app.models.project import Environment, SoftwareProject
from app.models.system import SystemComponent
from app.services.graph_data_quality import GraphDataQualityService
from app.services.graph_registry import ComponentRegistry
from app.services.graph_validator import DataQualityIssue, GraphValidator


async def _seed(
    db_session: AsyncSession,
    names: tuple[str, ...] = ("web", "gateway"),
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    """Project + env + components; returns (project_id, env_id, {name: id})."""
    project = SoftwareProject(name="Dq Proj", slug="dq-proj")
    db_session.add(project)
    await db_session.flush()
    env = Environment(project_id=project.id, name="prod", environment_type="PRODUCTION")
    db_session.add(env)
    await db_session.flush()
    pending: dict[str, SystemComponent] = {}
    for name in names:
        comp = SystemComponent(
            project_id=project.id,
            environment_id=env.id,
            name=name,
            component_type="SERVICE",
        )
        db_session.add(comp)
        pending[name] = comp
    await db_session.flush()
    return project.id, env.id, {name: comp.id for name, comp in pending.items()}


async def _mirror_all(
    db_session: AsyncSession,
    project_id: uuid.UUID,
    ids: dict[str, uuid.UUID],
) -> dict[str, uuid.UUID]:
    """Mirror components into graph nodes; returns {name: graph_node_id}."""
    registry = ComponentRegistry(db_session)
    out: dict[str, uuid.UUID] = {}
    for name, cid in ids.items():
        comp = await db_session.get(SystemComponent, cid)
        assert comp is not None
        node = await registry.get_or_create_component_node(comp)
        out[name] = node.id
    return out


def _by_type(issues: List[DataQualityIssue], check_type: str) -> List[DataQualityIssue]:
    return [i for i in issues if i.check_type == check_type]


class TestValidatorChecks:
    async def test_orphan_node_flagged(self, db_session: AsyncSession) -> None:
        project_id, _, ids = await _seed(db_session, names=("web", "gateway", "db"))
        gids = await _mirror_all(db_session, project_id, ids)
        # Connect web -> gateway so only the db node has no incident edge.
        db_session.add(
            GraphEdge(
                project_id=project_id,
                source_node_id=gids["web"],
                target_node_id=gids["gateway"],
                edge_type=GraphEdgeType.CALLS,
                source=GraphEdgeSource.TRACE,
                confidence=0.9,
                status=GraphEdgeStatus.ACTIVE,
            )
        )
        await db_session.flush()

        issues = await GraphValidator(db_session).run_checks(project_id=project_id)
        orphans = _by_type(issues, "orphan_nodes")
        assert len(orphans) == 1
        assert orphans[0].severity == DataQualitySeverity.WARNING
        assert orphans[0].detail["name"] == "db"

    async def test_orphan_edge_flagged_error(self, db_session: AsyncSession) -> None:
        project_id, _, ids = await _seed(db_session)
        gids = await _mirror_all(db_session, project_id, ids)
        # Edge with a real target but a dangling source node id.
        now = datetime.now(timezone.utc)
        await db_session.execute(
            insert(GraphEdge).values(
                id=uuid.uuid4(),
                project_id=project_id,
                source_node_id=uuid.uuid4(),
                target_node_id=gids["gateway"],
                edge_type=GraphEdgeType.CALLS,
                source=GraphEdgeSource.TRACE,
                confidence=0.9,
                status=GraphEdgeStatus.ACTIVE,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        await db_session.flush()

        issues = await GraphValidator(db_session).run_checks(project_id=project_id)
        orphans = _by_type(issues, "orphan_edges")
        assert len(orphans) == 1
        assert orphans[0].severity == DataQualitySeverity.ERROR
        assert orphans[0].detail["missing"] == "source"

    async def test_duplicate_component_flagged(self, db_session: AsyncSession) -> None:
        project_id, _, ids = await _seed(db_session)
        gids = await _mirror_all(db_session, project_id, ids)
        assert len(gids) == 2
        # Plant a duplicate component mirror with the same name (different case).
        db_session.add(
            GraphNode(
                project_id=project_id,
                environment_id=None,
                node_type=GraphNodeType.SERVICE,
                entity_kind="system_component",
                entity_id=uuid.uuid4(),
                name="WEB",
                status=GraphNodeStatus.ACTIVE,
            )
        )
        await db_session.flush()

        issues = await GraphValidator(db_session).run_checks(project_id=project_id)
        dups = _by_type(issues, "duplicate_components")
        assert len(dups) == 1
        assert dups[0].severity == DataQualitySeverity.WARNING
        assert dups[0].detail["name"] == "web"
        assert dups[0].detail["count"] == 2
        assert len(dups[0].detail["node_ids"]) == 2

    async def test_unresolved_endpoint_flagged(self, db_session: AsyncSession) -> None:
        project_id, env_id, ids = await _seed(db_session)
        await _mirror_all(db_session, project_id, ids)
        # Endpoint under a mirrored component -> resolved.
        db_session.add(
            ServiceEndpoint(
                project_id=project_id,
                environment_id=env_id,
                component_id=ids["gateway"],
                method="GET",
                path_template="/api/x",
            )
        )
        # Endpoint under a component with no graph node -> unresolved.
        ghost = SystemComponent(
            project_id=project_id,
            environment_id=env_id,
            name="ghost",
            component_type="SERVICE",
        )
        db_session.add(ghost)
        await db_session.flush()
        db_session.add(
            ServiceEndpoint(
                project_id=project_id,
                environment_id=env_id,
                component_id=ghost.id,
                method="GET",
                path_template="/api/ghost",
            )
        )
        await db_session.flush()

        issues = await GraphValidator(db_session).run_checks(project_id=project_id)
        unresolved = _by_type(issues, "unresolved_endpoints")
        assert len(unresolved) == 1
        assert unresolved[0].severity == DataQualitySeverity.WARNING
        assert unresolved[0].detail["component_id"] == str(ghost.id)

    async def test_env_scope_mismatch_flagged(self, db_session: AsyncSession) -> None:
        project_id, env_id, ids = await _seed(db_session)
        env2 = Environment(
            project_id=project_id, name="staging", environment_type="STAGING"
        )
        db_session.add(env2)
        await db_session.flush()

        gids = await _mirror_all(db_session, project_id, ids)
        # Edge claims staging env but both endpoint nodes live in prod.
        db_session.add(
            GraphEdge(
                project_id=project_id,
                environment_id=env2.id,
                source_node_id=gids["web"],
                target_node_id=gids["gateway"],
                edge_type=GraphEdgeType.CALLS,
                source=GraphEdgeSource.TRACE,
                confidence=0.9,
                status=GraphEdgeStatus.ACTIVE,
            )
        )
        await db_session.flush()

        issues = await GraphValidator(db_session).run_checks(project_id=project_id)
        mismatches = _by_type(issues, "env_scope_mismatch")
        assert len(mismatches) == 1
        assert mismatches[0].severity == DataQualitySeverity.ERROR
        assert set(mismatches[0].detail["mismatching_sides"]) == {"source", "target"}

    async def test_stale_relationship_reported(self, db_session: AsyncSession) -> None:
        project_id, _, ids = await _seed(db_session)
        gids = await _mirror_all(db_session, project_id, ids)
        db_session.add(
            GraphEdge(
                project_id=project_id,
                source_node_id=gids["web"],
                target_node_id=gids["gateway"],
                edge_type=GraphEdgeType.CALLS,
                source=GraphEdgeSource.TRACE,
                confidence=0.9,
                status=GraphEdgeStatus.STALE,
            )
        )
        await db_session.flush()

        issues = await GraphValidator(db_session).run_checks(project_id=project_id)
        stale = _by_type(issues, "stale_relationships")
        assert len(stale) == 1
        assert stale[0].severity == DataQualitySeverity.INFO
        assert stale[0].detail["count"] == 1

    async def test_conflicting_alias_flagged(self, db_session: AsyncSession) -> None:
        project_id, _, ids = await _seed(db_session)
        gids = await _mirror_all(db_session, project_id, ids)
        registry = ComponentRegistry(db_session)
        await registry.add_alias(gids["web"], "api", project_id=project_id)
        await registry.add_alias(gids["gateway"], "api", project_id=project_id)
        await db_session.flush()

        issues = await GraphValidator(db_session).run_checks(project_id=project_id)
        conflicts = _by_type(issues, "conflicting_alias")
        assert len(conflicts) == 1
        assert conflicts[0].severity == DataQualitySeverity.WARNING
        assert conflicts[0].detail["node_count"] == 2

    async def test_lone_repository_reported(self, db_session: AsyncSession) -> None:
        project_id, _, ids = await _seed(db_session)
        assert ids  # noqa: F841
        repo = CodeRepository(
            project_id=project_id,
            provider="github",
            repository_url="https://github.com/piyush06singhal/argus-api",
        )
        db_session.add(repo)
        await db_session.flush()
        registry = ComponentRegistry(db_session)
        await registry.get_or_create_repository_node(repo)
        await db_session.flush()

        issues = await GraphValidator(db_session).run_checks(project_id=project_id)
        lone = _by_type(issues, "lone_repository")
        assert len(lone) == 1
        assert lone[0].severity == DataQualitySeverity.INFO


class TestDataQualityService:
    async def test_records_persisted(self, db_session: AsyncSession) -> None:
        project_id, _, ids = await _seed(db_session)
        await _mirror_all(db_session, project_id, ids)  # both nodes orphaned
        service = GraphDataQualityService(db_session, GraphValidator(db_session))
        await service.run_and_get_health(project_id=project_id)
        await db_session.flush()

        count = (
            await db_session.execute(
                select(func.count())
                .select_from(GraphDataQualityRecord)
                .where(GraphDataQualityRecord.project_id == project_id)
            )
        ).scalar()
        assert count and count >= 2  # two orphan nodes -> at least two records

    async def test_health_ok_false_on_error(self, db_session: AsyncSession) -> None:
        project_id, _, _ = await _seed(db_session)
        now = datetime.now(timezone.utc)
        await db_session.execute(
            insert(GraphEdge).values(
                id=uuid.uuid4(),
                project_id=project_id,
                source_node_id=uuid.uuid4(),
                target_node_id=uuid.uuid4(),
                edge_type=GraphEdgeType.CALLS,
                source=GraphEdgeSource.TRACE,
                status=GraphEdgeStatus.ACTIVE,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        await db_session.flush()

        service = GraphDataQualityService(db_session, GraphValidator(db_session))
        health = await service.run_and_get_health(project_id=project_id)
        assert health.ok is False
        assert any(
            row.severity == DataQualitySeverity.ERROR for row in health.data_quality
        )

    async def test_health_ok_true_without_errors(
        self, db_session: AsyncSession
    ) -> None:
        project_id, _, ids = await _seed(db_session)
        gids = await _mirror_all(db_session, project_id, ids)
        # Connect the pair so no node is orphaned; only informational checks may
        # fire, which must not flip `ok`.
        db_session.add(
            GraphEdge(
                project_id=project_id,
                source_node_id=gids["web"],
                target_node_id=gids["gateway"],
                edge_type=GraphEdgeType.CALLS,
                source=GraphEdgeSource.TRACE,
                confidence=0.9,
                status=GraphEdgeStatus.ACTIVE,
            )
        )
        await db_session.flush()

        service = GraphDataQualityService(db_session, GraphValidator(db_session))
        health = await service.run_and_get_health(project_id=project_id)
        assert health.ok is True

    async def test_current_health_read_only(self, db_session: AsyncSession) -> None:
        project_id, _, ids = await _seed(db_session)
        gids = await _mirror_all(db_session, project_id, ids)
        db_session.add(
            GraphEdge(
                project_id=project_id,
                source_node_id=gids["web"],
                target_node_id=gids["gateway"],
                edge_type=GraphEdgeType.CALLS,
                source=GraphEdgeSource.TRACE,
                status=GraphEdgeStatus.ACTIVE,
            )
        )
        await db_session.flush()
        db_session.add(
            GraphDataQualityRecord(
                project_id=project_id,
                check_type="stale_relationships",
                severity=DataQualitySeverity.INFO,
                detail={"count": 3},
            )
        )
        await db_session.flush()

        service = GraphDataQualityService(db_session, GraphValidator(db_session))
        health = await service.current_health(project_id=project_id)
        assert health.node_count == 2
        assert health.edge_count == 1
        assert any(
            row.check_type == "stale_relationships" for row in health.data_quality
        )
        assert health.ok is True
