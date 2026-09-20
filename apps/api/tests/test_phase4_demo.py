"""Phase 4 scenario tests (§48, §49, §50).

The three deterministic scenarios are Phase 4's integration tests: telemetry is
ingested, Phase 3 detects anomalies and correlates them into an incident, and
the causal engine analyses whatever is stored. Nothing is told to the engine,
and no expected answer is written into the analysis tables.

* **Checkout chain (§48)** — the datastore degrades first, then the service
  that reads it, then the caller. The engine must *derive* the datastore as the
  most-supported explanation and build the chain that connects the evidence.
* **Counterexample (§49)** — the deployment lands after onset and must be
  refuted by a temporal contradiction rather than blamed.
* **Unknown (§50)** — insufficient evidence must yield ``UNKNOWN``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.causal import (
    CausalEvidence,
    CausalRelationship,
    ConfidenceLevel,
    EvidencePolarity,
    RootCauseCandidate,
)
from app.models.project import Environment, SoftwareProject
from app.models.system import ComponentCategory, ComponentDependency, SystemComponent
from app.services.causal_analysis_service import CausalAnalysisService
from app.services.causal_explanation import CausalExplanationService
from app.services.demo_causal import (
    seed_counterexample_scenario,
    seed_unknown_scenario,
)
from app.services.demo_incident import seed_checkout_incident

NOW = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)


async def _checkout_topology(db: AsyncSession):
    project = SoftwareProject(
        name="Causal Demo", slug=f"causal-demo-{uuid.uuid4().hex[:8]}"
    )
    db.add(project)
    await db.flush()
    environment = Environment(
        project_id=project.id, name="Production", environment_type="PRODUCTION"
    )
    db.add(environment)
    await db.flush()
    components = {
        "checkout_service": SystemComponent(
            project_id=project.id,
            environment_id=environment.id,
            name="Checkout Service",
            component_type=ComponentCategory.SERVICE,
        ),
        "inventory_service": SystemComponent(
            project_id=project.id,
            environment_id=environment.id,
            name="Inventory Service",
            component_type=ComponentCategory.SERVICE,
        ),
        "inventory_db": SystemComponent(
            project_id=project.id,
            environment_id=environment.id,
            name="Inventory Database",
            component_type=ComponentCategory.DATABASE,
        ),
        "api_gateway": SystemComponent(
            project_id=project.id,
            environment_id=environment.id,
            name="API Gateway",
            component_type=ComponentCategory.APPLICATION,
        ),
        "payment_service": SystemComponent(
            project_id=project.id,
            environment_id=environment.id,
            name="Payment Service",
            component_type=ComponentCategory.SERVICE,
        ),
        "redis": SystemComponent(
            project_id=project.id,
            environment_id=environment.id,
            name="Redis",
            component_type=ComponentCategory.CACHE,
        ),
    }
    for component in components.values():
        db.add(component)
    await db.flush()
    for source, target in (
        ("api_gateway", "checkout_service"),
        ("checkout_service", "inventory_service"),
        ("inventory_service", "inventory_db"),
        ("checkout_service", "payment_service"),
        ("checkout_service", "redis"),
    ):
        db.add(
            ComponentDependency(
                source_component_id=components[source].id,
                target_component_id=components[target].id,
                dependency_type="HTTP",
            )
        )
    await db.flush()
    return project, environment, components


async def _analyse_checkout(db: AsyncSession):
    project, environment, components = await _checkout_topology(db)
    result = await seed_checkout_incident(
        db,
        project=project,
        environment=environment,
        components=components,
        now=NOW,
    )
    assert result["skipped"] is False
    assert result["incident_id"] is not None
    incident_id = uuid.UUID(result["incident_id"])
    from app.models.incident import Incident

    incident = await db.get(Incident, incident_id)
    assert incident is not None
    outcome = await CausalAnalysisService(db).analyze_incident(
        project_id=project.id,
        incident=incident,
        trigger="pytest",
        force=True,
    )
    await db.flush()
    candidates = list(
        (
            await db.execute(
                select(RootCauseCandidate)
                .where(RootCauseCandidate.analysis_id == outcome.analysis.id)
                .order_by(RootCauseCandidate.score.desc())
            )
        )
        .scalars()
        .all()
    )
    return project, environment, components, outcome.analysis, candidates


class TestCheckoutChainScenario:
    async def test_datastore_is_derived_as_the_most_supported_explanation(
        self, db_session: AsyncSession
    ) -> None:
        _project, _env, components, analysis, candidates = await _analyse_checkout(
            db_session
        )
        assert candidates, "the scenario must produce candidates"
        top = candidates[0]
        # The engine is never told the answer: it must reach the datastore from
        # the span tree, the ordering of the anomalies and the dependency path.
        assert top.component_id == components["inventory_db"].id
        assert top.candidate_type.value == "DATABASE"
        assert top.confidence in (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM)
        assert analysis.primary_candidate_id == top.id
        assert top.supporting_evidence_count > 0
        assert top.score_breakdown, "a score must always come with its breakdown"
        # Direction came from stored spans, so trace evidence must be present.
        assert any(
            reason.startswith("[TRACE]") for reason in (top.reasons or [])
        ), top.reasons

    async def test_the_causal_chain_connects_datastore_to_checkout(
        self, db_session: AsyncSession
    ) -> None:
        _project, _env, components, analysis, candidates = await _analyse_checkout(
            db_session
        )
        edges = list(
            (
                await db_session.execute(
                    select(CausalRelationship).where(
                        CausalRelationship.analysis_id == analysis.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert edges, "an evidence-backed sequence must produce relationships"
        by_id = {candidate.id: candidate for candidate in candidates}
        datastore_id = components["inventory_db"].id
        # Walk out of the primary hypothesis: the first hop must leave the
        # datastore toward the component that reads it.
        outgoing = [
            edge
            for edge in edges
            if edge.source_candidate_id == analysis.primary_candidate_id
        ]
        assert outgoing, "the primary hypothesis must explain something downstream"
        targets = {by_id[edge.target_candidate_id].component_id for edge in outgoing}
        assert components["inventory_service"].id in targets
        # And every edge in the scenario must name its evidence.
        assert all(edge.supporting_evidence_count > 0 for edge in edges)
        assert all(edge.explanation for edge in edges)
        # No edge may claim causation in the wrong direction: nothing should
        # point *into* the datastore as its downstream effect from the caller.
        into_datastore = [
            edge
            for edge in edges
            if by_id[edge.target_candidate_id].component_id == datastore_id
        ]
        assert (
            not into_datastore
        ), "the caller cannot be the cause of the datastore it depends on"

    async def test_deployment_is_context_not_primary(
        self, db_session: AsyncSession
    ) -> None:
        _project, _env, _components, analysis, candidates = await _analyse_checkout(
            db_session
        )
        deployments = [
            candidate
            for candidate in candidates
            if candidate.candidate_type.value == "DEPLOYMENT"
        ]
        assert deployments, "the deployment must appear as a considered alternative"
        assert analysis.primary_candidate_id not in {c.id for c in deployments}
        assert deployments[0].score < candidates[0].score

    async def test_analysis_is_reproducible(self, db_session: AsyncSession) -> None:
        _project, _env, _components, analysis, candidates = await _analyse_checkout(
            db_session
        )
        first = [(c.candidate_type.value, round(c.score, 6)) for c in candidates]
        from app.models.incident import Incident

        incident = await db_session.get(Incident, analysis.incident_id)
        assert incident is not None
        outcome = await CausalAnalysisService(db_session).analyze_incident(
            project_id=analysis.project_id,
            incident=incident,
            trigger="pytest-rerun",
            force=True,
        )
        rerun = list(
            (
                await db_session.execute(
                    select(RootCauseCandidate)
                    .where(RootCauseCandidate.analysis_id == outcome.analysis.id)
                    .order_by(RootCauseCandidate.score.desc())
                )
            )
            .scalars()
            .all()
        )
        assert [(c.candidate_type.value, round(c.score, 6)) for c in rerun] == first
        assert outcome.analysis.analysis_version == analysis.analysis_version + 1


class TestCounterexampleScenario:
    async def test_late_deployment_is_refuted_not_blamed(
        self, db_session: AsyncSession
    ) -> None:
        result = await seed_counterexample_scenario(db_session, now=NOW)
        assert result["deployment_candidate_id"] is not None
        # A change that happened after onset cannot explain the onset.
        assert result["deployment_contradicting_evidence"] >= 1
        assert result["deployment_is_primary"] is False
        assert result["deployment_confidence"] in {"INSUFFICIENT", "LOW"}
        deployment = next(
            candidate
            for candidate in result["candidates"]
            if candidate["event_id"] == result["deployment_event_id"]
        )
        assert any(
            "AFTER incident onset" in reason for reason in deployment["reasons"]
        ), deployment["reasons"]

    async def test_no_causal_edge_is_created_from_a_late_change(
        self, db_session: AsyncSession
    ) -> None:
        result = await seed_counterexample_scenario(db_session, now=NOW)
        analysis_id = uuid.UUID(result["analysis_id"])
        deployment_candidate_id = uuid.UUID(result["deployment_candidate_id"])
        edges = list(
            (
                await db_session.execute(
                    select(CausalRelationship).where(
                        CausalRelationship.analysis_id == analysis_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert not [
            edge
            for edge in edges
            if edge.source_candidate_id == deployment_candidate_id
        ], "a change that happened after onset must not trigger causal edges"


class TestUnknownScenario:
    async def test_insufficient_evidence_yields_unknown(
        self, db_session: AsyncSession
    ) -> None:
        result = await seed_unknown_scenario(db_session, now=NOW)
        assert result["primary_candidate_id"] is None
        assert result["overall_confidence"] == "INSUFFICIENT"
        assert "insufficient evidence" in (result["summary"] or "").lower()
        assert result[
            "missing_evidence"
        ], "declining to answer must come with what was missing"
        # The candidates are still returned, ranked — just not crowned.
        assert result["candidates"]
        assert all(
            candidate["confidence"] == "INSUFFICIENT"
            for candidate in result["candidates"]
        )

    async def test_no_candidate_outscores_the_others_unconvincingly(
        self, db_session: AsyncSession
    ) -> None:
        result = await seed_unknown_scenario(db_session, now=NOW)
        scores = [candidate["score"] for candidate in result["candidates"]]
        assert (
            max(scores) < 0.5
        ), "without directional evidence no candidate may clear the primary floor"


class TestEvidenceAttributionConsistency:
    """A candidate's counts must equal the evidence it can actually be shown.

    Edge-bound rows carry a ``candidate_id`` only because the schema demands an
    owner. If they were returned as the candidate's own evidence, a candidate
    would *display* more facts than the scorer counted when it chose that
    candidate's confidence — the counts on screen and the evidence on screen
    would disagree. The checkout scenario is what makes this testable: it is the
    analysis that actually produces edges.
    """

    async def test_candidate_counts_match_the_evidence_it_owns(
        self, db_session: AsyncSession
    ) -> None:
        project, _env, _components, analysis, candidates = await _analyse_checkout(
            db_session
        )
        rows = list(
            (
                await db_session.execute(
                    select(CausalEvidence).where(
                        CausalEvidence.analysis_id == analysis.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any(
            row.relationship_id is not None for row in rows
        ), "the scenario must exercise edge-bound evidence for this to mean anything"
        loaded = await CausalExplanationService(db_session).load_analysis(
            project_id=project.id,
            incident_id=analysis.incident_id,
            analysis_id=analysis.id,
        )
        assert loaded is not None
        for candidate in candidates:
            owned = loaded.evidence_for(candidate.id)
            counted = (
                candidate.supporting_evidence_count
                + candidate.contradicting_evidence_count
                + candidate.neutral_evidence_count
            )
            assert len(owned) == counted, candidate.candidate_type
            assert (
                sum(1 for item in owned if item.polarity is EvidencePolarity.SUPPORTING)
                == candidate.supporting_evidence_count
            )
            assert (
                sum(
                    1
                    for item in owned
                    if item.polarity is EvidencePolarity.CONTRADICTING
                )
                == candidate.contradicting_evidence_count
            )

    async def test_edge_evidence_remains_reachable_through_the_edge(
        self, db_session: AsyncSession
    ) -> None:
        """Excluding edge facts from candidates must not make them unreachable."""
        project, _env, _components, analysis, _candidates = await _analyse_checkout(
            db_session
        )
        loaded = await CausalExplanationService(db_session).load_analysis(
            project_id=project.id,
            incident_id=analysis.incident_id,
            analysis_id=analysis.id,
        )
        assert loaded is not None
        assert loaded.relationships
        for relationship in loaded.relationships:
            if relationship.supporting_evidence_count == 0:
                continue
            assert loaded.evidence_for_relationship(
                relationship.id
            ), "an edge that names supporting facts must expose them (§41)"


class TestEvidencePolarityOnScenarios:
    async def test_scenario_analyses_record_both_polarities_when_present(
        self, db_session: AsyncSession
    ) -> None:
        result = await seed_counterexample_scenario(db_session, now=NOW)
        analysis_id = uuid.UUID(result["analysis_id"])
        polarities = {
            item.polarity
            for item in (
                await db_session.execute(
                    select(CausalEvidence).where(
                        CausalEvidence.analysis_id == analysis_id
                    )
                )
            )
            .scalars()
            .all()
        }
        assert EvidencePolarity.SUPPORTING in polarities
        assert EvidencePolarity.CONTRADICTING in polarities
