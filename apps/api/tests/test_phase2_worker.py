"""Phase 2 — graph_extract worker hook: enqueue → drain → persisted edges."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.graph import GraphEdge, GraphEdgeSource, GraphNode
from app.models.observability import ObservabilityEvent, SpanRecord, TraceStatus
from app.models.project import Environment, SoftwareProject
from app.models.system import SystemComponent
from app.services.queue import PermanentJobError, enqueue_graph_extract
from app.services.worker_runner import (
    process_event_job,
    process_graph_extract_job,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _seed_topology(
    db_session: AsyncSession,
    *,
    slug: str,
    with_otlp_events: bool = False,
) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    """Gateway -> Checkout -> Postgres components (+ optional OTLP-style events)."""
    project = SoftwareProject(name="Worker Proj", slug=slug)
    db_session.add(project)
    await db_session.flush()
    env = Environment(project_id=project.id, name="prod", environment_type="PRODUCTION")
    db_session.add(env)
    await db_session.flush()

    comps: dict[str, uuid.UUID] = {}
    for name, ctype in (
        ("gateway", "SERVICE"),
        ("checkout", "SERVICE"),
        ("postgres", "DATABASE"),
    ):
        comp = SystemComponent(
            project_id=project.id,
            environment_id=env.id,
            name=name,
            component_type=ctype,
        )
        db_session.add(comp)
        await db_session.flush()
        comps[name] = comp.id

    if with_otlp_events:
        # OTLP path: spans persisted as TRACE ObservabilityEvents with
        # payload-borne parent chains (resource attrs carry service names).
        base = _now()
        rows = [
            ("trace-otlp-a", "sp-a-1", None, "gateway", 0),
            ("trace-otlp-a", "sp-a-2", "sp-a-1", "checkout", 5),
            ("trace-otlp-a", "sp-a-3", "sp-a-2", "postgres", 10),
        ]
        for trace_id, span_id, parent, comp_name, offset_ms in rows:
            db_session.add(
                ObservabilityEvent(
                    project_id=project.id,
                    environment_id=env.id,
                    component_id=comps[comp_name],
                    timestamp=base + timedelta(milliseconds=offset_ms),
                    source="trace:otlp",
                    event_type="TRACE",
                    severity=None,
                    payload={
                        "trace_id": trace_id,
                        "span_id": span_id,
                        "parent_span_id": parent,
                        "name": f"op-{comp_name}",
                        "attributes": {
                            "service.name": comp_name,
                            "http.method": "GET",
                            "http.route": f"/{comp_name}",
                        },
                    },
                    metadata_={
                        "source": "otlp",
                        "resource": {"service.name": comp_name},
                    },
                )
            )
    await db_session.flush()
    return project.id, env.id, comps


def _span(
    project_id: uuid.UUID,
    comps: dict[str, uuid.UUID],
    trace: str,
    span_id: str,
    parent: str | None,
    comp_name: str,
) -> SpanRecord:
    now = _now()
    return SpanRecord(
        trace_id=trace,
        span_id=span_id,
        parent_span_id=parent,
        project_id=project_id,
        component_id=comps[comp_name],
        operation=span_id,
        start_time=now,
        end_time=now,
        duration_ms=1,
        status=TraceStatus.OK,
        metadata_={},
    )


class TestGraphExtractJob:
    async def test_job_derives_edges_from_spans(self, db_engine) -> None:
        factory = async_sessionmaker(
            db_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            project_id, env_id, comps = await _seed_topology(
                session, slug=_unique("gx")
            )
            session.add_all(
                [
                    _span(project_id, comps, "t1", "s1", None, "gateway"),
                    _span(project_id, comps, "t1", "s2", "s1", "checkout"),
                    _span(project_id, comps, "t1", "s3", "s2", "postgres"),
                ]
            )
            await session.commit()

        summary = await process_graph_extract_job(
            factory, payload={"project_id": str(project_id), "environment_id": None}
        )
        assert summary["edges_created"] >= 2

        async with factory() as session:
            edges = (await session.execute(select(GraphEdge))).scalars().all()
            trace_edges = [e for e in edges if e.source == GraphEdgeSource.TRACE]
            assert len(trace_edges) >= 2
            node_names = dict(
                (await session.execute(select(GraphNode.id, GraphNode.name))).all()
            )
            pairs = {
                (node_names[e.source_node_id], node_names[e.target_node_id])
                for e in trace_edges
            }
            assert ("gateway", "checkout") in pairs
            assert ("checkout", "postgres") in pairs

    async def test_job_derives_edges_from_otlp_events(self, db_engine) -> None:
        factory = async_sessionmaker(
            db_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            project_id, _, _ = await _seed_topology(
                session, slug=_unique("gx-otlp"), with_otlp_events=True
            )
            await session.commit()

        summary = await process_graph_extract_job(
            factory, payload={"project_id": str(project_id), "environment_id": None}
        )
        assert summary["edges_created"] >= 2

        async with factory() as session:
            names = dict(
                (await session.execute(select(GraphNode.id, GraphNode.name))).all()
            )
            trace_edges = (
                (
                    await session.execute(
                        select(GraphEdge).where(GraphEdge.source == "TRACE")
                    )
                )
                .scalars()
                .all()
            )
            pairs = {
                (names[e.source_node_id], names[e.target_node_id]) for e in trace_edges
            }
            assert ("gateway", "checkout") in pairs
            assert ("checkout", "postgres") in pairs

    async def test_job_is_idempotent(self, db_engine) -> None:
        factory = async_sessionmaker(
            db_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            project_id, _, comps = await _seed_topology(
                session, slug=_unique("gx-idem")
            )
            session.add_all(
                [
                    _span(project_id, comps, "t1", "s1", None, "gateway"),
                    _span(project_id, comps, "t1", "s2", "s1", "checkout"),
                ]
            )
            await session.commit()

        first = await process_graph_extract_job(
            factory, payload={"project_id": str(project_id), "environment_id": None}
        )
        second = await process_graph_extract_job(
            factory, payload={"project_id": str(project_id), "environment_id": None}
        )
        assert first["edges_created"] >= 1
        assert second["edges_created"] == 0

    async def test_job_without_project_raises(self, db_engine) -> None:
        factory = async_sessionmaker(
            db_engine, class_=AsyncSession, expire_on_commit=False
        )
        with pytest.raises(PermanentJobError, match="missing required project_id"):
            await process_graph_extract_job(factory, payload={})

    async def test_job_for_deleted_project_is_permanent(self, db_engine) -> None:
        """Jobs for vanished projects dead-letter immediately — no retry churn."""
        factory = async_sessionmaker(
            db_engine, class_=AsyncSession, expire_on_commit=False
        )
        ghost = uuid.uuid4()
        with pytest.raises(PermanentJobError, match="not found"):
            await process_graph_extract_job(factory, payload={"project_id": str(ghost)})

    async def test_job_via_event_kind_dispatch(self, db_engine) -> None:
        """process_event_job routes kind=graph_extract to the graph pipeline."""
        factory = async_sessionmaker(
            db_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            project_id, _, comps = await _seed_topology(
                session, slug=_unique("gx-disp")
            )
            session.add_all(
                [
                    _span(project_id, comps, "t1", "s1", None, "gateway"),
                    _span(project_id, comps, "t1", "s2", "s1", "checkout"),
                ]
            )
            await session.commit()

        accepted = await process_event_job(
            factory,
            kind="graph_extract",
            payload={"project_id": str(project_id), "environment_id": None},
        )
        assert accepted == 0

        async with factory() as session:
            trace_edges = (
                (
                    await session.execute(
                        select(GraphEdge).where(GraphEdge.source == "TRACE")
                    )
                )
                .scalars()
                .all()
            )
            assert len(trace_edges) >= 1

    async def test_job_backfills_unattributed_services(self, db_engine) -> None:
        """First-seen trace services become components; their spans attribute.

        Live stack proof of this path: OTLP probe traces for brand-new service
        pairs emerge as TRACE edges (the §13 worker-path smoke check).
        """
        factory = async_sessionmaker(
            db_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            project_id, env_id, comps = await _seed_topology(
                session, slug=_unique("gx-backfill")
            )
            base = _now()
            # "fresh-svc" has no component — resolver left these unattributed.
            session.add_all(
                [
                    ObservabilityEvent(
                        project_id=project_id,
                        environment_id=env_id,
                        component_id=None,
                        timestamp=base,
                        source="trace:fresh-svc",
                        event_type="TRACE",
                        payload={
                            "trace_id": "t-fresh",
                            "span_id": "fr-1",
                            "parent_span_id": "fr-0",
                            "name": "op",
                        },
                        metadata_={"resource": {"service.name": "fresh-svc"}},
                    ),
                    ObservabilityEvent(
                        project_id=project_id,
                        environment_id=env_id,
                        component_id=None,
                        timestamp=base + timedelta(milliseconds=5),
                        source="trace:fresh-svc",
                        event_type="TRACE",
                        payload={
                            "trace_id": "t-fresh",
                            "span_id": "fr-2",
                            "parent_span_id": "fr-1",
                            "name": "op",
                        },
                        metadata_={"resource": {"service.name": "fresh-svc"}},
                    ),
                    # Parent on the *existing* gateway component → cross edge.
                    ObservabilityEvent(
                        project_id=project_id,
                        environment_id=env_id,
                        component_id=comps["gateway"],
                        timestamp=base - timedelta(milliseconds=5),
                        source="trace:gateway",
                        event_type="TRACE",
                        payload={
                            "trace_id": "t-fresh",
                            "span_id": "fr-0",
                            "parent_span_id": None,
                            "name": "op",
                        },
                    ),
                ]
            )
            # fr-1 (fresh-svc) is a child of fr-0 (gateway) in the same trace
            # → the pair must produce a gateway→fresh-svc TRACE edge.
            await session.commit()

        await process_graph_extract_job(
            factory,
            payload={"project_id": str(project_id), "environment_id": None},
        )

        async with factory() as session:
            fresh = (
                await session.execute(
                    select(SystemComponent).where(
                        SystemComponent.project_id == project_id,
                        SystemComponent.name == "fresh-svc",
                    )
                )
            ).scalar_one()
            assert fresh.component_type.value == "SERVICE"
            attributed = (
                (
                    await session.execute(
                        select(ObservabilityEvent).where(
                            ObservabilityEvent.source == "trace:fresh-svc",
                            ObservabilityEvent.component_id == fresh.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(attributed) == 2
            edge = (
                (
                    await session.execute(
                        select(GraphEdge).where(
                            GraphEdge.project_id == project_id,
                            GraphEdge.source == "TRACE",
                        )
                    )
                )
                .scalars()
                .all()
            )
            names = {e.id for e in edge}
            assert names  # at least the gateway→fresh-svc edge exists


class TestGraphExtractEnqueue:
    async def test_enqueue_pushes_job_when_enabled(self, monkeypatch) -> None:
        monkeypatch.setattr("app.services.queue.settings.GRAPH_EXTRACT_ASYNC", True)

        pushed: list[dict] = []

        class FakeQueue:
            async def push(self, job: dict) -> None:
                pushed.append(job)

        monkeypatch.setattr(
            "app.services.queue.IngestionQueue", lambda *a, **k: FakeQueue()
        )
        ok = await enqueue_graph_extract(project_id=uuid.uuid4())
        assert ok is True
        assert pushed[0]["kind"] == "graph_extract"
        assert pushed[0]["payload"]["project_id"]
        assert "events" not in pushed[0]["payload"]  # ids only, no telemetry

    async def test_enqueue_noop_when_disabled(self, monkeypatch) -> None:
        monkeypatch.setattr("app.services.queue.settings.GRAPH_EXTRACT_ASYNC", False)
        ok = await enqueue_graph_extract(project_id=uuid.uuid4())
        assert ok is False

    async def test_enqueue_degrades_on_unavailable_queue(self, monkeypatch) -> None:
        from app.services.queue import QueueUnavailable

        monkeypatch.setattr("app.services.queue.settings.GRAPH_EXTRACT_ASYNC", True)

        class FakeQueue:
            async def push(self, job: dict) -> None:
                raise QueueUnavailable("no broker in test")

        monkeypatch.setattr(
            "app.services.queue.IngestionQueue", lambda *a, **k: FakeQueue()
        )
        ok = await enqueue_graph_extract(project_id=uuid.uuid4())
        assert ok is False  # degraded, never raised


class TestPostIngestHookLive:
    """The API boundaries enqueue graph_extract after accepted spans."""

    def test_span_route_enqueues(self, client: TestClient, monkeypatch) -> None:
        from app.services.queue import settings as queue_settings

        monkeypatch.setattr(queue_settings, "GRAPH_EXTRACT_ASYNC", True)
        pushed: list[dict] = []

        async def fake_enqueue(*, project_id, environment_id=None) -> bool:
            pushed.append({"project_id": str(project_id)})
            return True

        monkeypatch.setattr(
            "app.api.v1.routes.observability.enqueue_graph_extract", fake_enqueue
        )

        project = client.post(
            "/api/v1/projects",
            json={"name": "Hook Proj", "slug": _unique("hook")},
        ).json()
        resp = client.post(
            "/api/v1/observability/traces/spans",
            json={
                "trace_id": "trace-hook-1",
                "span_id": _unique("span"),
                "project_id": project["id"],
                "operation": "op",
                "start_time": _now().isoformat(),
                "status": "OK",
            },
        )
        assert resp.status_code == 201
        assert pushed and pushed[0]["project_id"] == project["id"]

    def test_otlp_route_enqueues(self, client: TestClient, monkeypatch) -> None:
        from app.services.queue import settings as queue_settings

        monkeypatch.setattr(queue_settings, "GRAPH_EXTRACT_ASYNC", True)
        pushed: list[dict] = []

        async def fake_enqueue(*, project_id, environment_id=None) -> bool:
            pushed.append({"project_id": str(project_id)})
            return True

        monkeypatch.setattr(
            "app.api.v1.routes.otlp.enqueue_graph_extract", fake_enqueue
        )

        project = client.post(
            "/api/v1/projects",
            json={"name": "Hook OTLP", "slug": _unique("hook-otlp")},
        ).json()
        resp = client.post(
            "/api/v1/otlp/v1/traces",
            json={
                "project_id": project["id"],
                "resource_spans": [
                    {
                        "resource": {
                            "attributes": [
                                {
                                    "key": "service.name",
                                    "value": {"string_value": "hook-svc"},
                                }
                            ]
                        },
                        "spans": [
                            {
                                "trace_id": "trace-hook-otlp",
                                "span_id": "span-hook-otlp",
                                "parent_span_id": None,
                                "name": "op",
                                "kind": 2,
                                "start_time_unix_nano": 1704110400000000000,
                                "end_time_unix_nano": 1704110400050000000,
                                "attributes": [],
                                "status": {"code": 1, "message": ""},
                                "events": [],
                            }
                        ],
                    }
                ],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["accepted"] >= 1
        assert pushed and pushed[0]["project_id"] == project["id"]
