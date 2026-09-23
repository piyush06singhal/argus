"""Phase 10 — learned relationships (§23, §24, §25, §31, §84).

The knowledge-graph integration is small but easy to get wrong in ways that look
fine: an observation becomes an architectural claim, a co-occurrence grows an
arrow, a re-run doubles the sample count, or last month's episode leaks into this
month's cutoff. Each of those is a test here.

What is asserted, in order:

* **§24** — a learned relationship is never a dependency: ``is_dependency`` is
  false, the disclaimer is present, and ``graph_edges`` is untouched.
* Direction comes from a *supported* root-cause hypothesis, never from sorting,
  and an undirected co-failure stays undirected through the API.
* **Idempotency** — running the stage twice over unchanged history leaves the same
  counts. A pipeline that inflates support is worse than one that learns nothing.
* **§84** — an episode that ended after the cutoff contributes nothing.
* **Small samples** — one episode is not a relationship, and the stage says so
  rather than storing it with ``sample_count=1``.
* **Isolation** — another project's episodes are invisible to this project's build.
* **§25** — an edge history stops confirming is marked ``STALE``, kept, and
  excluded from the default view.
* **Filtering** — an unknown ``kind`` is refused instead of answered.

Rows are built by ``record_episode``, which runs the real experience builder, so
every edge asserted here is derived from history the pipeline can actually make.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select

from tests.phase10_helpers import (
    build_project,
    build_scope,
    hours_before,
    record_episode,
    utcnow,
)

from app.models.causal import (
    AnalysisStatus,
    CandidateStatus,
    CandidateType,
    CausalAnalysis,
    ConfidenceLevel,
    RootCauseCandidate,
)
from app.models.intelligence import (
    DataProvenance,
    LearnedRelationship,
    LearningEvent,
    LearningEventType,
    LearningRun,
    RelationshipKind,
    RelationshipStatus,
)
from app.models.project import SoftwareProject
from app.models.system import ComponentDependency, DependencyType, SystemComponent
from app.services.experience_builder import (
    build_experience_draft,
    persist_experience,
)
from app.services.learning_run import execute_learning_run
from app.services.relationship_builder import (
    HISTORICAL_RELATIONSHIP_NOTE,
    build_relationships,
)


async def _second_component(session, project, name: str = "inventory-service"):
    """A second component in the project, so a pair can exist."""
    row = SystemComponent(
        project_id=project.id,
        name=f"{name}-{uuid.uuid4().hex[:6]}",
        component_type="SERVICE",
    )
    session.add(row)
    await session.flush()
    return row


async def _episode(
    session,
    project,
    environment,
    component,
    *,
    start: Optional[datetime] = None,
    hours_ago: float = 1.0,
    duration_minutes: float = 20.0,
    extra_components=(),
    **kwargs,
):
    """One complete episode, placed explicitly in time.

    ``extra_components`` mirrors Phase 3 correlation: the anomalies those
    components emitted are linked to the incident, and the experience is rebuilt
    so its component set is the one the pipeline would assemble. Writing the
    anomalies without linking them would leave a single-component experience and
    every relationship assertion would pass vacuously.
    """
    began = start if start is not None else hours_before(utcnow(), hours_ago)
    resolved = began + timedelta(minutes=duration_minutes)
    episode = await record_episode(
        session,
        project,
        environment,
        component,
        started_at=began,
        resolved_at=resolved,
        extra_components=extra_components,
        **kwargs,
    )
    if extra_components:
        incident = episode["incident"]
        await _correlate(session, incident=incident, start=began, end=resolved)
        draft, _reason = await build_experience_draft(
            session, incident_id=incident.id, as_of=resolved + timedelta(seconds=1)
        )
        if draft is not None:
            experience, _status = await persist_experience(session, draft)
            episode["experience"] = experience
    return episode


async def _correlate(session, *, incident, start: datetime, end: datetime) -> None:
    """Attribute the window's anomalies to the incident, as Phase 3 does."""
    from app.models.anomaly import Anomaly

    rows = list(
        (
            await session.scalars(
                select(Anomaly)
                .where(Anomaly.project_id == incident.project_id)
                .where(Anomaly.detected_at >= start)
                .where(Anomaly.detected_at <= end)
            )
        ).all()
    )
    for row in rows:
        if row.incident_id is None:
            row.incident_id = incident.id
    await session.flush()


def _as_aware(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive timestamps; comparisons need them aware."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _edges(
    session, project, kind: Optional[RelationshipKind] = None
) -> list[LearnedRelationship]:
    stmt = select(LearnedRelationship).where(
        LearnedRelationship.project_id == project.id
    )
    if kind is not None:
        stmt = stmt.where(LearnedRelationship.kind == kind)
    return list((await session.scalars(stmt)).all())


async def _add_root_cause(
    session,
    project,
    episode,
    *,
    component,
    status: CandidateStatus,
    confidence: ConfidenceLevel,
):
    """Attach a Phase 4 root-cause candidate to an episode's experience.

    The candidate is written directly because building a complete causal analysis
    is Phase 4's own suite's job. What matters here is that the relationship
    stage reads the candidate's *status and confidence* and nothing else.
    """
    incident = episode["incident"]
    analysis = CausalAnalysis(
        project_id=project.id,
        environment_id=incident.environment_id,
        incident_id=incident.id,
        status=AnalysisStatus.COMPLETED,
        started_at=incident.detected_at,
        completed_at=incident.resolved_at,
        overall_confidence=confidence,
        summary="fixture analysis",
    )
    session.add(analysis)
    await session.flush()
    candidate = RootCauseCandidate(
        analysis_id=analysis.id,
        project_id=project.id,
        component_id=component.id,
        candidate_type=CandidateType.APPLICATION_COMPONENT,
        status=status,
        confidence=confidence,
        score=0.8,
        explanation="fixture candidate",
    )
    session.add(candidate)
    await session.flush()

    experience = episode["experience"]
    experience.root_cause_candidate_id = candidate.id
    experience.causal_analysis_id = analysis.id
    await session.flush()
    return candidate


# ---------------------------------------------------------------------- basics


async def test_two_components_in_one_episode_produce_one_undirected_edge(db_session):
    project, environment, checkout = await build_project(db_session)
    inventory = await _second_component(db_session, project)

    for hours in (3, 12):
        await _episode(
            db_session,
            project,
            environment,
            checkout,
            hours_ago=hours,
            extra_components=[inventory],
        )

    result = await build_relationships(
        db_session, project_id=project.id, cutoff=utcnow()
    )
    #: Two derived edges per remediated episode: the co-failure, and the fact
    #: that acting on checkout coincided with inventory recovering.
    assert result.edges_created == 2, result.as_dict()

    edges = await _edges(db_session, project, RelationshipKind.SHARED_FAILURE)
    assert len(edges) == 1
    edge = edges[0]
    assert edge.kind is RelationshipKind.SHARED_FAILURE
    #: §24. The whole point: co-occurrence carries no direction.
    assert edge.directed is False
    assert edge.sample_count == 2
    assert edge.status is RelationshipStatus.ACTIVE
    assert edge.confidence.value == "LOW"
    assert HISTORICAL_RELATIONSHIP_NOTE in edge.limitations[0]
    #: Both ends are real stored components.
    assert {edge.source_component_id, edge.target_component_id} == {
        checkout.id,
        inventory.id,
    }


async def test_a_single_episode_is_not_a_relationship(db_session):
    """§86 — a one-observation pattern is not established."""
    project, environment, checkout = await build_project(db_session)
    inventory = await _second_component(db_session, project)
    await _episode(
        db_session,
        project,
        environment,
        checkout,
        hours_ago=3,
        extra_components=[inventory],
    )

    result = await build_relationships(
        db_session, project_id=project.id, cutoff=utcnow()
    )
    assert result.edges_low_support == 2
    assert result.edges_created == 0
    assert await _edges(db_session, project) == []


async def test_a_single_component_episode_yields_nothing(db_session):
    project, environment, checkout = await build_project(db_session)
    await _episode(db_session, project, environment, checkout, hours_ago=3)

    result = await build_relationships(
        db_session, project_id=project.id, cutoff=utcnow()
    )
    assert result.edges_considered == 0
    assert await _edges(db_session, project) == []


async def test_learning_relationships_are_not_written_to_graph_edges(db_session):
    """§24 — the structural graph must stay structural."""
    from app.models.graph import GraphEdge

    project, environment, checkout = await build_project(db_session)
    inventory = await _second_component(db_session, project)
    for hours in (3, 8):
        await _episode(
            db_session,
            project,
            environment,
            checkout,
            hours_ago=hours,
            extra_components=[inventory],
        )

    before = int(await db_session.scalar(select(func.count(GraphEdge.id))) or 0)
    await build_relationships(db_session, project_id=project.id, cutoff=utcnow())
    after = int(await db_session.scalar(select(func.count(GraphEdge.id))) or 0)
    assert before == after
    #: And the relationships landed in their own table.
    assert len(await _edges(db_session, project)) == 2


# ------------------------------------------------------------------ direction


async def test_a_supported_root_cause_directs_a_propagation_edge(db_session):
    project, environment, checkout = await build_project(db_session)
    inventory = await _second_component(db_session, project)

    for hours in (30, 40):
        episode = await _episode(
            db_session,
            project,
            environment,
            checkout,
            hours_ago=hours,
            extra_components=[inventory],
        )
        await _add_root_cause(
            db_session,
            project,
            episode,
            component=inventory,
            status=CandidateStatus.SUPPORTED,
            confidence=ConfidenceLevel.HIGH,
        )

    await build_relationships(db_session, project_id=project.id, cutoff=utcnow())
    edges = await _edges(db_session, project, RelationshipKind.FAILURE_PROPAGATION)
    assert len(edges) == 1
    edge = edges[0]
    #: Direction comes from the hypothesis: inventory → checkout.
    assert edge.source_component_id == inventory.id
    assert edge.target_component_id == checkout.id
    assert edge.directed is True
    assert any("hypothesis" in note for note in edge.limitations)
    #: The same pair is not also stored as a co-failure: one observation, one
    #: edge, or the same episodes would be counted twice.
    assert await _edges(db_session, project, RelationshipKind.SHARED_FAILURE) == []


async def test_a_weak_root_cause_does_not_direct_anything(db_session):
    """A ``WEAKENED``/low-confidence candidate is not a direction (§27, §28)."""
    project, environment, checkout = await build_project(db_session)
    inventory = await _second_component(db_session, project)

    for hours in (30, 40):
        episode = await _episode(
            db_session,
            project,
            environment,
            checkout,
            hours_ago=hours,
            extra_components=[inventory],
        )
        await _add_root_cause(
            db_session,
            project,
            episode,
            component=inventory,
            status=CandidateStatus.WEAKENED,
            confidence=ConfidenceLevel.LOW,
        )

    await build_relationships(db_session, project_id=project.id, cutoff=utcnow())
    assert await _edges(db_session, project, RelationshipKind.FAILURE_PROPAGATION) == []
    shared = await _edges(db_session, project, RelationshipKind.SHARED_FAILURE)
    assert len(shared) == 1
    assert shared[0].directed is False


async def test_human_confirmation_is_recorded_as_the_direction_source(db_session):
    project, environment, checkout = await build_project(db_session)
    inventory = await _second_component(db_session, project)

    for hours in (30, 40):
        episode = await _episode(
            db_session,
            project,
            environment,
            checkout,
            hours_ago=hours,
            extra_components=[inventory],
        )
        await _add_root_cause(
            db_session,
            project,
            episode,
            component=inventory,
            status=CandidateStatus.SUPPORTED,
            confidence=ConfidenceLevel.HIGH,
        )
        incident = episode["incident"]
        db_session.add(
            LearningEvent(
                project_id=project.id,
                event_type=LearningEventType.ROOT_CAUSE_CONFIRMED,
                subject_id=incident.id,
                dedup_key=f"confirm-{incident.id}",
                payload={},
                occurred_at=incident.resolved_at,
                provenance=DataProvenance.HUMAN_ENTERED,
            )
        )
    await db_session.flush()

    await build_relationships(db_session, project_id=project.id, cutoff=utcnow())
    edge = (await _edges(db_session, project, RelationshipKind.FAILURE_PROPAGATION))[0]
    assert any("human reviewer" in note for note in edge.limitations)
    #: §78. Human confirmation improves the *direction*; it does not touch the
    #: sample count and does not promote the edge into a dependency.
    assert edge.sample_count == 2


# ---------------------------------------------------------------- idempotency


async def test_running_twice_does_not_inflate_support(db_session):
    """§85 — the same history processed twice is one result, not two."""
    project, environment, checkout = await build_project(db_session)
    inventory = await _second_component(db_session, project)
    for hours in (3, 8, 20):
        await _episode(
            db_session,
            project,
            environment,
            checkout,
            hours_ago=hours,
            extra_components=[inventory],
        )

    first = await build_relationships(
        db_session, project_id=project.id, cutoff=utcnow()
    )
    assert first.edges_created == 2
    edge = (await _edges(db_session, project, RelationshipKind.SHARED_FAILURE))[0]
    assert edge.sample_count == 3

    second = await build_relationships(
        db_session, project_id=project.id, cutoff=utcnow()
    )
    assert second.edges_created == 0
    assert second.edges_updated == 2

    rows = await _edges(db_session, project, RelationshipKind.SHARED_FAILURE)
    assert len(rows) == 1
    assert rows[0].sample_count == 3
    assert len(rows[0].evidence) == 3


async def test_a_learning_run_refreshes_rather_than_duplicates_edges(db_session):
    project, environment, checkout = await build_project(db_session)
    inventory = await _second_component(db_session, project)
    for hours in (3, 8):
        await _episode(
            db_session,
            project,
            environment,
            checkout,
            hours_ago=hours,
            extra_components=[inventory],
        )

    first = await execute_learning_run(
        db_session, project_id=project.id, trigger="test"
    )
    assert first.relationships_created == 2
    assert first.relationships_updated == 0

    second = await execute_learning_run(
        db_session, project_id=project.id, trigger="test"
    )
    assert second.relationships_created == 0
    assert second.relationships_updated == 2
    assert len(await _edges(db_session, project)) == 2

    run = await db_session.get(LearningRun, uuid.UUID(second.run_id))
    assert run is not None
    assert run.relationships_created == 0
    assert run.relationships_updated == 2


# ------------------------------------------------------------ temporal purity


async def test_an_episode_after_the_cutoff_is_not_learned_from(db_session):
    """§31/§84 — future outcomes must not shape a relationship *as of* a cutoff."""
    project, environment, checkout = await build_project(db_session)
    inventory = await _second_component(db_session, project)
    cutoff = utcnow()
    for hours in (24, 30):
        await _episode(
            db_session,
            project,
            environment,
            checkout,
            hours_ago=hours,
            extra_components=[inventory],
        )
    #: One episode strictly after the boundary: it must not count.
    await _episode(
        db_session,
        project,
        environment,
        checkout,
        start=cutoff + timedelta(hours=2),
        extra_components=[inventory],
    )

    result = await build_relationships(db_session, project_id=project.id, cutoff=cutoff)
    assert result.edges_created == 2
    edge = (await _edges(db_session, project, RelationshipKind.SHARED_FAILURE))[0]
    assert edge.sample_count == 2
    #: The stored boundary is inside the window, so the third episode was not read.
    assert _as_aware(edge.coverage_end) <= cutoff


# ------------------------------------------------------------------- staleness


async def test_an_edge_history_stops_confirming_is_marked_stale_not_deleted(db_session):
    """§25/§42 — decay marks, it never removes."""
    project, environment, checkout = await build_project(db_session)
    inventory = await _second_component(db_session, project)
    for hours in (48, 60):
        await _episode(
            db_session,
            project,
            environment,
            checkout,
            hours_ago=hours,
            extra_components=[inventory],
        )
    await build_relationships(
        db_session, project_id=project.id, cutoff=utcnow(), lookback_days=3650
    )
    assert all(
        row.status is RelationshipStatus.ACTIVE
        for row in await _edges(db_session, project)
    )

    #: A build whose window excludes the supporting episodes confirms nothing.
    later = utcnow() + timedelta(days=200)
    result = await build_relationships(
        db_session, project_id=project.id, cutoff=later, lookback_days=30
    )
    assert result.edges_stale == 2
    rows = await _edges(db_session, project)
    assert len(rows) == 2
    assert {row.status for row in rows} == {RelationshipStatus.STALE}
    #: Kept, with its evidence: the topology change is itself the fact.
    assert all(row.sample_count == 2 for row in rows)
    assert all(row.evidence for row in rows)


async def test_a_stale_edge_becomes_active_again_when_history_confirms_it(db_session):
    project, environment, checkout = await build_project(db_session)
    inventory = await _second_component(db_session, project)
    for hours in (48, 60):
        await _episode(
            db_session,
            project,
            environment,
            checkout,
            hours_ago=hours,
            extra_components=[inventory],
        )
    await build_relationships(
        db_session, project_id=project.id, cutoff=utcnow(), lookback_days=3650
    )
    later = utcnow() + timedelta(days=200)
    await build_relationships(
        db_session, project_id=project.id, cutoff=later, lookback_days=30
    )
    assert {row.status for row in await _edges(db_session, project)} == {
        RelationshipStatus.STALE
    }

    await build_relationships(
        db_session, project_id=project.id, cutoff=utcnow(), lookback_days=3650
    )
    assert {row.status for row in await _edges(db_session, project)} == {
        RelationshipStatus.ACTIVE
    }


# -------------------------------------------------------------------- isolation


async def test_another_projects_episodes_do_not_produce_edges(db_session):
    first, _environment, _component = await build_project(db_session)
    other, other_environment, other_component = await build_project(db_session)
    neighbour = await _second_component(db_session, other)
    for hours in (3, 8):
        await _episode(
            db_session,
            other,
            other_environment,
            other_component,
            hours_ago=hours,
            extra_components=[neighbour],
        )

    result = await build_relationships(db_session, project_id=first.id, cutoff=utcnow())
    assert result.edges_created == 0
    assert await _edges(db_session, first) == []

    #: The other project's own build does produce its edges, so the empty result
    #: above is isolation and not a broken fixture.
    await build_relationships(db_session, project_id=other.id, cutoff=utcnow())
    assert len(await _edges(db_session, other)) == 2
    assert await _edges(db_session, first) == []


# ---------------------------------------------------------------- HTTP surface


async def test_the_api_refuses_an_unknown_kind_and_a_foreign_project(
    client, db_session
):
    response = client.post(
        "/api/v1/projects",
        json={"name": "Relationships", "slug": f"rel-{uuid.uuid4().hex[:8]}"},
    )
    assert response.status_code in (200, 201), response.text
    project_id = response.json()["id"]

    refused = client.get(
        "/api/v1/intelligence/relationships",
        params={"project_id": project_id, "kind": "TOTALLY_MADE_UP"},
    )
    #: Answering "no results" to a typo would look exactly like a real answer.
    assert refused.status_code == 422

    unknown = client.get(
        "/api/v1/intelligence/relationships",
        params={"project_id": str(uuid.uuid4())},
    )
    assert unknown.status_code == 404


async def test_the_api_labels_every_relationship_as_historical(client, db_session):
    response = client.post(
        "/api/v1/projects",
        json={"name": "RelAPI", "slug": f"relapi-{uuid.uuid4().hex[:8]}"},
    )
    assert response.status_code in (200, 201), response.text
    project = response.json()
    project_row = await db_session.get(SoftwareProject, uuid.UUID(project["id"]))
    environment, checkout = await build_scope(db_session, project_row.id)
    inventory = await _second_component(db_session, project_row)
    for hours in (3, 8):
        await _episode(
            db_session,
            project_row,
            environment,
            checkout,
            hours_ago=hours,
            extra_components=[inventory],
        )
    await build_relationships(db_session, project_id=project_row.id, cutoff=utcnow())
    #: The HTTP client runs its own session, so the writes must be visible to it.
    await db_session.commit()

    listed = client.get(
        "/api/v1/intelligence/relationships", params={"project_id": project["id"]}
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 2
    assert HISTORICAL_RELATIONSHIP_NOTE in body["relationship_note"]
    assert body["limitations"]
    #: Selected by kind, not by position: the two edges carry the same support,
    #: so their order depends on component ids and would make this flaky.
    item = next(row for row in body["items"] if row["kind"] == "SHARED_FAILURE")
    #: §24 — the payload itself refuses to be read as a dependency.
    assert item["is_dependency"] is False
    assert item["directed"] is False
    assert item["disclaimer"] == HISTORICAL_RELATIONSHIP_NOTE
    assert item["sample_count"] == 2
    assert item["source_component_name"] and item["target_component_name"]
    assert item["limitations"]

    #: And the directed edge says it is directed — the flag is per row, not
    #: per response.
    influence = next(
        row for row in body["items"] if row["kind"] == "REMEDIATION_INFLUENCE"
    )
    assert influence["directed"] is True
    assert influence["source_component_name"] == checkout.name

    #: A component-scoped read sees them from either end.
    for component in (inventory.id, checkout.id):
        scoped = client.get(
            "/api/v1/intelligence/relationships",
            params={"project_id": project["id"], "component_id": str(component)},
        )
        assert scoped.status_code == 200
        assert scoped.json()["total"] == 2

    profile = client.get(
        f"/api/v1/intelligence/components/{inventory.id}/profile",
        params={"project_id": project["id"]},
    )
    assert profile.status_code == 200, profile.text
    assert profile.json()["relationships"][0]["is_dependency"] is False


async def test_stale_edges_are_excluded_from_the_default_view(client, db_session):
    response = client.post(
        "/api/v1/projects",
        json={"name": "RelStale", "slug": f"relstale-{uuid.uuid4().hex[:8]}"},
    )
    assert response.status_code in (200, 201), response.text
    project = response.json()
    project_row = await db_session.get(SoftwareProject, uuid.UUID(project["id"]))
    environment, checkout = await build_scope(db_session, project_row.id)
    inventory = await _second_component(db_session, project_row)
    for hours in (48, 60):
        await _episode(
            db_session,
            project_row,
            environment,
            checkout,
            hours_ago=hours,
            extra_components=[inventory],
        )
    await build_relationships(
        db_session, project_id=project_row.id, cutoff=utcnow(), lookback_days=3650
    )
    await build_relationships(
        db_session,
        project_id=project_row.id,
        cutoff=utcnow() + timedelta(days=200),
        lookback_days=30,
    )
    await db_session.commit()

    default = client.get(
        "/api/v1/intelligence/relationships", params={"project_id": project["id"]}
    )
    #: What history no longer confirms is not current belief.
    assert default.json()["total"] == 0

    explicit = client.get(
        "/api/v1/intelligence/relationships",
        params={"project_id": project["id"], "status": "STALE"},
    )
    assert explicit.json()["total"] == 2
    assert {item["status"] for item in explicit.json()["items"]} == {"STALE"}


async def test_metrics_report_relationship_health(db_session):
    project, environment, checkout = await build_project(db_session)
    inventory = await _second_component(db_session, project)
    for hours in (3, 8):
        await _episode(
            db_session,
            project,
            environment,
            checkout,
            hours_ago=hours,
            extra_components=[inventory],
        )
    await build_relationships(db_session, project_id=project.id, cutoff=utcnow())

    from app.services.intelligence_service import learning_metrics

    metrics = await learning_metrics(db_session, project_id=project.id)
    assert metrics["relationships_active"] == 2
    assert metrics["relationships_stale"] == 0
    #: A co-failure is counted as undirected, so it is never reported as a
    #: propagation.
    assert metrics["relationships_undirected"] == 1


# ------------------------------------------------------------------- flavours


async def test_dependency_degradation_follows_the_declared_dependency(db_session):
    """Direction comes from configuration, not from the observed symptom order."""
    project, environment, checkout = await build_project(db_session)
    inventory = await _second_component(db_session, project)
    #: Tenancy comes from the components, so the pair is what identifies the row.
    db_session.add(
        ComponentDependency(
            source_component_id=checkout.id,
            target_component_id=inventory.id,
            dependency_type=DependencyType.HTTP,
        )
    )
    await db_session.flush()

    for hours in (3, 8):
        await _episode(
            db_session,
            project,
            environment,
            checkout,
            hours_ago=hours,
            extra_components=[inventory],
        )

    await build_relationships(db_session, project_id=project.id, cutoff=utcnow())
    edges = await _edges(db_session, project, RelationshipKind.DEPENDENCY_DEGRADATION)
    assert len(edges) == 1
    #: The dependency's health is the upstream end; the component that depends on
    #: it is downstream, whatever order the failures appeared in.
    assert edges[0].source_component_id == inventory.id
    assert edges[0].target_component_id == checkout.id
    assert any("declared dependency" in note for note in edges[0].limitations)


async def test_remediation_influence_uses_the_action_target(db_session):
    project, environment, checkout = await build_project(db_session)
    inventory = await _second_component(db_session, project)
    for hours in (3, 8):
        await _episode(
            db_session,
            project,
            environment,
            checkout,
            hours_ago=hours,
            extra_components=[inventory],
        )

    await build_relationships(db_session, project_id=project.id, cutoff=utcnow())
    edges = await _edges(db_session, project, RelationshipKind.REMEDIATION_INFLUENCE)
    assert len(edges) == 1
    #: The action ran on checkout and inventory recovered in the same episodes.
    assert edges[0].source_component_id == checkout.id
    assert edges[0].target_component_id == inventory.id
    assert edges[0].supporting_count == 2
    assert edges[0].directed is True
