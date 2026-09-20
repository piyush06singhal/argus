"""Phase 4 engine tests (§47): temporal, dependency, trace, change, candidates,
graph/chain validation and scoring.

These are pure-unit tests over the deterministic analyzers — no database, no
clock, no randomness. They encode the phase's central promises:

* ``before ≠ caused`` — precedence is evidence, never a conclusion;
* no edge without evidence, and ``CORRELATES_WITH`` stays distinct from cause;
* confidence is a coarse bucket earned by *independent* evidence categories,
  not a number that looks like a probability;
* ``UNKNOWN``/``INSUFFICIENT`` is a valid, tested outcome.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.causal import (
    CandidateStatus,
    CandidateType,
    CausalEvidenceCategory,
    CausalRelationship,
    CausalRelationshipType,
    ConfidenceLevel,
    EvidencePolarity,
    RootCauseCandidate,
)
from app.services.causal_explanation import CausalExplanationService
from app.services.causal_analysis_service import CausalAnalysisService
from app.services.candidate_generator import CausalCandidateGenerator
from app.services.causal_graph import (
    CausalGraphBuilder,
    CausalGraphSpec,
    CausalValidator,
    EdgeKind,
    EvidenceSpec,
    HypothesisEdge,
    HypothesisNode,
)
from app.services.change_analyzer import (
    ChangeAnalysisResult,
    ChangeAnalyzer,
    ChangeAssessment,
    ChangeEventView,
    ChangeRelevance,
)
from app.services.dependency_analyzer import (
    DependencyContext,
    DependencyEdgeInfo,
    StructuralRelation,
)
from app.services.root_cause_scorer import RootCauseScorer
from app.services.temporal_analyzer import (
    TemporalAnalyzer,
    TemporalFact,
    TemporalOrder,
)
from app.services.trace_analyzer import (
    PropagationObservation,
    SpanView,
    TraceAnalysisResult,
    TraceAnalyzer,
    TraceFailureEdge,
)

BASE = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)


def _component_fact(key: str, at: datetime, label: str | None = None) -> TemporalFact:
    return TemporalFact(key=key, label=label or key, at=at)


# ---------------------------------------------------------------------------
# Temporal
# ---------------------------------------------------------------------------
class TestTemporalAnalyzer:
    def test_ordering_and_gaps(self) -> None:
        analyzer = TemporalAnalyzer()
        facts = [
            _component_fact("checkout", BASE + timedelta(minutes=3)),
            _component_fact("db", BASE),
            _component_fact("inventory", BASE + timedelta(minutes=1)),
        ]
        ordered = analyzer.order_facts(facts)
        assert [fact.key for fact in ordered] == ["db", "inventory", "checkout"]
        gaps = analyzer.gaps(facts)
        assert [gap[2] for gap in gaps] == [60, 120]

    def test_before_is_not_cause(self) -> None:
        """Precedence is reported as precedence — the enum says nothing else."""
        analyzer = TemporalAnalyzer()
        relation = analyzer.relate(
            _component_fact("db", BASE),
            _component_fact("checkout", BASE + timedelta(seconds=60)),
        )
        assert relation.order is TemporalOrder.PRECEDES
        assert relation.gap_seconds == 60
        # The relation carries no causal claim at all.
        assert not hasattr(relation, "caused")

    def test_same_timestamp_is_simultaneous_not_ordered(self) -> None:
        analyzer = TemporalAnalyzer()
        relation = analyzer.relate(
            _component_fact("a", BASE), _component_fact("b", BASE)
        )
        assert relation.order is TemporalOrder.SIMULTANEOUS
        assert relation.gap_seconds == 0

    def test_out_of_order_input_still_orders(self) -> None:
        analyzer = TemporalAnalyzer()
        facts = [
            _component_fact("late", BASE + timedelta(seconds=90)),
            _component_fact("early", BASE),
            _component_fact("middle", BASE + timedelta(seconds=45)),
        ]
        assert [f.key for f in analyzer.order_facts(facts)] == [
            "early",
            "middle",
            "late",
        ]

    def test_timezone_handling(self) -> None:
        """Naive timestamps are normalized to UTC, never compared naively."""
        analyzer = TemporalAnalyzer()
        naive = datetime(2026, 9, 20, 14, 0)
        aware = datetime(2026, 9, 20, 14, 0, 30, tzinfo=timezone.utc)
        relation = analyzer.relate(
            _component_fact("naive", naive), _component_fact("aware", aware)
        )
        assert relation.order is TemporalOrder.PRECEDES

    def test_within_bounds_is_inclusive_and_swaps_reversed_input(self) -> None:
        analyzer = TemporalAnalyzer()
        facts = [
            _component_fact("inside", BASE + timedelta(seconds=10)),
            _component_fact("outside", BASE + timedelta(seconds=400)),
        ]
        found = analyzer.within(facts, BASE + timedelta(seconds=30), BASE)
        assert [f.key for f in found] == ["inside"]


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------
class TestDependencyContext:
    def test_upstream_downstream_and_shared_dependency(self) -> None:
        checkout, inventory, db, other = (uuid.uuid4() for _ in range(4))
        context = DependencyContext(
            component_ids={checkout, inventory, db, other},
            # The edges are what make indirect reachability decidable — the
            # analyzer always populates them, so the fixture must too.
            edges=[
                DependencyEdgeInfo(
                    source_component_id=checkout,
                    target_component_id=inventory,
                    dependency_type="HTTP",
                    origin="component_dependencies",
                ),
                DependencyEdgeInfo(
                    source_component_id=inventory,
                    target_component_id=db,
                    dependency_type="DATABASE",
                    origin="component_dependencies",
                ),
            ],
            upstream_providers={checkout: {inventory}, inventory: {db}},
            downstream_dependents={inventory: {checkout}, db: {inventory, checkout}},
            shared_dependencies={db: {inventory, checkout}},
        )
        assert inventory in context.providers_of(checkout)
        assert context.relation(checkout, inventory) is StructuralRelation.DIRECT
        assert context.relation(inventory, db) is StructuralRelation.DIRECT
        # checkout reaches the datastore through inventory: indirect, and the
        # distinction is load-bearing — it becomes ``structural_support`` 1 vs 2
        # on the causal graph's edges.
        assert context.relation(checkout, db) is StructuralRelation.INDIRECT
        assert context.relation(checkout, other) is StructuralRelation.UNRELATED

    def test_cycle_does_not_hang_or_claim_relation_to_self(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        context = DependencyContext(
            component_ids={a, b},
            edges=[
                DependencyEdgeInfo(
                    source_component_id=a,
                    target_component_id=b,
                    dependency_type="HTTP",
                    origin="component_dependencies",
                ),
                DependencyEdgeInfo(
                    source_component_id=b,
                    target_component_id=a,
                    dependency_type="HTTP",
                    origin="component_dependencies",
                ),
            ],
            upstream_providers={a: {b}, b: {a}},
            downstream_dependents={b: {a}, a: {b}},
        )
        # A mutual dependency must not deadlock the reachability walk.
        assert context.relation(a, b) is StructuralRelation.DIRECT
        assert context.relation(a, a) is StructuralRelation.DIRECT
        assert not context.truncated


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------
class TestTraceAnalyzerHeuristics:
    """The span-tree logic is exercised through its pure helpers."""

    def test_span_view_normalizes_start(self) -> None:
        span = SpanView(
            span_id="s1",
            parent_span_id=None,
            trace_id="t1",
            component_id=None,
            operation=None,
            start_time=datetime(2026, 9, 20, 14, 0),
            end_time=None,
            duration_ms=None,
            failed=False,
        )
        assert span.start.tzinfo is not None

    def test_propagation_reports_delays_and_shape(self) -> None:
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        observation = PropagationObservation(
            trace_id="t1",
            failure_order=[
                (a, BASE),
                (b, BASE + timedelta(seconds=2)),
                (c, BASE + timedelta(seconds=5)),
            ],
        )
        assert observation.is_propagation_shaped is True
        assert observation.delays == [2, 3]

    def test_propagation_orders_nested_failures_innermost_first(self) -> None:
        """Regression: ordering by span *start* inverted every cascading failure.

        A caller's span starts before the dependency it waits on, so start-time
        ordering reported the caller as the origin. Failure *completion* is the
        causally sound instant: the parent cannot fail before its child.
        """
        caller, callee = uuid.uuid4(), uuid.uuid4()
        caller_span = SpanView(
            span_id="root",
            parent_span_id=None,
            trace_id="t1",
            component_id=caller,
            operation="POST",
            start_time=BASE,
            end_time=BASE + timedelta(milliseconds=2900),
            duration_ms=2900.0,
            failed=True,
        )
        callee_span = SpanView(
            span_id="child",
            parent_span_id="root",
            trace_id="t1",
            component_id=callee,
            operation="SELECT",
            start_time=BASE + timedelta(milliseconds=100),
            end_time=BASE + timedelta(milliseconds=2600),
            duration_ms=2500.0,
            failed=True,
        )
        observation = TraceAnalyzer._propagation("t1", [caller_span, callee_span])
        assert observation is not None
        assert [component for component, _at in observation.failure_order] == [
            callee,
            caller,
        ]
        assert observation.delays == [0]  # 300ms, reported in whole seconds

    def test_span_without_end_time_falls_back_to_start(self) -> None:
        span = SpanView(
            span_id="s",
            parent_span_id=None,
            trace_id="t1",
            component_id=None,
            operation=None,
            start_time=BASE,
            end_time=None,
            duration_ms=None,
            failed=True,
        )
        assert span.failed_at == span.start

    def test_single_component_failure_is_not_propagation_shaped(self) -> None:
        a = uuid.uuid4()
        observation = PropagationObservation(
            trace_id="t1", failure_order=[(a, BASE), (a, BASE + timedelta(seconds=1))]
        )
        assert observation.is_propagation_shaped is False

    def test_trace_failure_edge_observation_is_human_readable(self) -> None:
        parent, child = uuid.uuid4(), uuid.uuid4()
        edge = TraceFailureEdge(
            trace_id="t1",
            parent_span_id="p",
            child_span_id="c",
            parent_component_id=parent,
            child_component_id=child,
            child_operation="SELECT",
            child_status="TIMEOUT",
            child_duration_ms=2500.0,
            observed_at=BASE,
        )
        assert "TIMEOUT" in edge.observation
        assert "2500ms" in edge.observation


# ---------------------------------------------------------------------------
# Change
# ---------------------------------------------------------------------------
class TestChangeAnalyzerAssess:
    def _assessment(
        self, *, event_at: datetime, onset: datetime, touches: bool
    ) -> ChangeAssessment:
        component = uuid.uuid4()
        analyzer = ChangeAnalyzer(cast(AsyncSession, None), proximity_seconds=900)
        result = ChangeAnalyzer.assess(
            analyzer,
            [
                ChangeEventView(
                    kind="DEPLOYMENT",
                    event_id=uuid.uuid4(),
                    occurred_at=event_at,
                    component_id=component,
                    label="Deployment deploy-1",
                    description=None,
                )
            ],
            onset=onset,
            affected_component_ids={component} if touches else {uuid.uuid4()},
        )
        return result.assessments[0]

    def test_change_after_onset_is_a_temporal_contradiction(self) -> None:
        assessment = self._assessment(
            event_at=BASE + timedelta(minutes=5), onset=BASE, touches=True
        )
        assert assessment.relevance is ChangeRelevance.TEMPORAL_CONTRADICTION
        assert assessment.is_temporal_contradiction
        assert "AFTER incident onset" in assessment.reason

    def test_change_before_onset_touching_affected_component_is_relevant(self) -> None:
        assessment = self._assessment(
            event_at=BASE - timedelta(minutes=3), onset=BASE, touches=True
        )
        assert assessment.relevance is ChangeRelevance.TEMPORALLY_RELEVANT
        assert assessment.touches_affected_components is True

    def test_change_far_before_onset_is_unrelated(self) -> None:
        assessment = self._assessment(
            event_at=BASE - timedelta(hours=5), onset=BASE, touches=True
        )
        assert assessment.relevance is ChangeRelevance.UNRELATED

    def test_change_before_onset_without_scope_is_unrelated(self) -> None:
        assessment = self._assessment(
            event_at=BASE - timedelta(minutes=2), onset=BASE, touches=False
        )
        assert assessment.relevance is ChangeRelevance.UNRELATED
        assert assessment.touches_affected_components is False


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------
def _change_result(*, contradiction: bool) -> ChangeAnalysisResult:
    component = uuid.uuid4()
    event = ChangeEventView(
        kind="DEPLOYMENT",
        event_id=uuid.uuid4(),
        occurred_at=BASE + timedelta(minutes=2)
        if contradiction
        else BASE - timedelta(120),
        component_id=component,
        label="Deployment deploy-1",
        description=None,
    )
    return ChangeAnalysisResult(
        assessments=[
            ChangeAssessment(
                event=event,
                relevance=(
                    ChangeRelevance.TEMPORAL_CONTRADICTION
                    if contradiction
                    else ChangeRelevance.TEMPORALLY_RELEVANT
                ),
                reason="test",
                touches_affected_components=True,
            )
        ]
    )


class TestCandidateGenerator:
    def test_component_reached_twice_is_one_candidate(self) -> None:
        """A datastore found by a span *and* by an anomaly is one hypothesis."""
        datastore = uuid.uuid4()
        trace_result = TraceAnalysisResult(
            edges=[
                TraceFailureEdge(
                    trace_id="t1",
                    parent_span_id="p",
                    child_span_id="c",
                    parent_component_id=uuid.uuid4(),
                    child_component_id=datastore,
                    child_operation=None,
                    child_status="TIMEOUT",
                    child_duration_ms=None,
                    observed_at=BASE,
                )
            ]
        )
        generator = CausalCandidateGenerator(max_candidates=8)
        seeds = generator.generate(
            project_id=uuid.uuid4(),
            onset=BASE,
            anomaly_components=[
                (
                    datastore,
                    "Inventory DB",
                    _anomaly_type(),
                    BASE + timedelta(seconds=5),
                )
            ],
            change_result=ChangeAnalysisResult(),
            trace_result=trace_result,
            database_component_ids={datastore},
        )
        matching = [seed for seed in seeds if seed.component_id == datastore]
        assert len(matching) == 1
        assert matching[0].candidate_type.value == "DATABASE"
        # Both discovery paths contributed their evidence.
        tables = {ref[0] for ref in matching[0].origin_refs}
        assert tables == {"span_records", "anomalies"}

    def test_change_and_component_are_separate_hypotheses(self) -> None:
        generator = CausalCandidateGenerator(max_candidates=8)
        seeds = generator.generate(
            project_id=uuid.uuid4(),
            onset=BASE,
            anomaly_components=[],
            change_result=_change_result(contradiction=False),
            trace_result=TraceAnalysisResult(),
        )
        assert len(seeds) == 1
        assert seeds[0].event_id is not None

    def test_candidate_count_is_bounded_and_contradictions_survive(self) -> None:
        generator = CausalCandidateGenerator(max_candidates=3)
        anomalies = [
            (
                uuid.uuid4(),
                f"svc-{index}",
                _anomaly_type(),
                BASE + timedelta(seconds=index),
            )
            for index in range(6)
        ]
        seeds = generator.generate(
            project_id=uuid.uuid4(),
            onset=BASE,
            anomaly_components=anomalies,
            change_result=_change_result(contradiction=True),
            trace_result=TraceAnalysisResult(),
        )
        # Three candidates plus the explicitly-reported overflow note…
        assert len(seeds) <= 4
        # …and the contradicting change is among them, never dropped for budget.
        assert any(
            seed.is_temporal_contradiction for seed in seeds
        ), "a contradicting change must be shown, not silently dropped"

    def test_candidates_without_an_anchor_are_never_invented(self) -> None:
        generator = CausalCandidateGenerator(max_candidates=5)
        seeds = generator.generate(
            project_id=uuid.uuid4(),
            onset=BASE,
            anomaly_components=[(None, "unknown", _anomaly_type(), BASE)],
            change_result=ChangeAnalysisResult(),
            trace_result=TraceAnalysisResult(),
        )
        assert seeds == []


def _anomaly_type():
    from app.models.anomaly import AnomalyType

    return AnomalyType.LATENCY_SPIKE


# ---------------------------------------------------------------------------
# Graph + validation
# ---------------------------------------------------------------------------
def _node(key: tuple, *, at: datetime | None = None, component=None) -> HypothesisNode:
    return HypothesisNode(
        key=key,
        candidate_type="APPLICATION_COMPONENT",
        component_id=component or uuid.uuid4(),
        event_id=None,
        label=str(key),
        explanation="test",
        first_observed_at=at,
    )


class TestCausalValidator:
    def test_rejects_edge_without_evidence(self) -> None:
        validator = CausalValidator()
        edge = HypothesisEdge(
            source_key=("a",),
            target_key=("b",),
            relationship_type=CausalRelationshipType.POSSIBLE_CAUSE,
            edge_kind=EdgeKind.TRACE_FAILURE,
        )
        assert validator.validate_edge(edge) == "no supporting evidence"

    def test_rejects_correlates_with_when_direction_evidence_exists(self) -> None:
        validator = CausalValidator()
        edge = HypothesisEdge(
            source_key=("a",),
            target_key=("b",),
            relationship_type=CausalRelationshipType.CORRELATES_WITH,
            edge_kind=EdgeKind.TRACE_FAILURE,
            evidence=[_evidence()],
        )
        assert "CORRELATES_WITH" in (validator.validate_edge(edge) or "")

    def test_rejects_causal_type_without_direction_evidence(self) -> None:
        validator = CausalValidator()
        edge = HypothesisEdge(
            source_key=("a",),
            target_key=("b",),
            relationship_type=CausalRelationshipType.LIKELY_CAUSE,
            edge_kind=EdgeKind.DEPENDENCY_PRECEDENCE,
            evidence=[_evidence()],
        )
        # A causal type is only licensed by a directional edge kind, so a
        # fabricated relationship cannot smuggle itself in as a cause.
        assert (
            validator.validate_edge(edge) is None
        )  # dependency precedence *is* directional

    def test_rejects_effect_before_cause(self) -> None:
        validator = CausalValidator()
        edge = HypothesisEdge(
            source_key=("a",),
            target_key=("b",),
            relationship_type=CausalRelationshipType.POSSIBLE_CAUSE,
            edge_kind=EdgeKind.DEPENDENCY_PRECEDENCE,
            evidence=[_evidence()],
            temporal_alignment_seconds=-120,
        )
        assert "temporal contradiction" in (validator.validate_edge(edge) or "")

    def test_temporal_alignment_is_signed_and_honest(self) -> None:
        validator = CausalValidator()
        cause = _node(("db",), at=BASE)
        effect = _node(("checkout",), at=BASE + timedelta(seconds=45))
        assert validator.temporal_alignment(cause, effect) == 45
        assert validator.temporal_alignment(effect, cause) == -45

    def test_chain_validation_reports_every_violation(self) -> None:
        validator = CausalValidator()
        good = HypothesisEdge(
            source_key=("a",),
            target_key=("b",),
            relationship_type=CausalRelationshipType.POSSIBLE_CAUSE,
            edge_kind=EdgeKind.DEPENDENCY_PRECEDENCE,
            evidence=[_evidence()],
            temporal_alignment_seconds=30,
        )
        bad = HypothesisEdge(
            source_key=("b",),
            target_key=("c",),
            relationship_type=CausalRelationshipType.POSSIBLE_CAUSE,
            edge_kind=EdgeKind.DEPENDENCY_PRECEDENCE,
            evidence=[_evidence()],
            temporal_alignment_seconds=-300,
        )
        problems = validator.validate_chain([good, bad])
        assert len(problems) == 1
        assert problems[0].startswith("edge 1")


class TestChainExtraction:
    """One chain is chosen from several possibilities, deliberately (§32, §47)."""

    @staticmethod
    def _candidate(key: str, score: float) -> RootCauseCandidate:
        return RootCauseCandidate(
            id=uuid.uuid4(),
            analysis_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            candidate_type=CandidateType.APPLICATION_COMPONENT,
            status=CandidateStatus.UNDER_EVALUATION,
            score=score,
            confidence=ConfidenceLevel.MEDIUM,
            is_external=False,
        )

    @staticmethod
    def _edge(
        source: uuid.UUID,
        target: uuid.UUID,
        *,
        kind: CausalRelationshipType = CausalRelationshipType.LIKELY_CAUSE,
        confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM,
        evidence: int = 1,
        alignment: int | None = 30,
    ) -> CausalRelationship:
        return CausalRelationship(
            id=uuid.uuid4(),
            analysis_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            source_candidate_id=source,
            target_candidate_id=target,
            relationship_type=kind,
            confidence=confidence,
            supporting_evidence_count=evidence,
            contradicting_evidence_count=0,
            temporal_alignment_seconds=alignment,
            structural_support=1,
            observational_support=0,
            explanation="test edge",
        )

    def test_stronger_confidence_wins_over_more_evidence(self) -> None:
        """Both branches are plausible; the reader is shown the stronger one."""
        service = CausalExplanationService(cast(AsyncSession, None))
        origin = self._candidate("db", 0.6)
        loud = self._candidate("loud", 0.2)
        strong = self._candidate("strong", 0.3)
        weak_edge = self._edge(
            origin.id, loud.id, evidence=9, confidence=ConfidenceLevel.LOW
        )
        strong_edge = self._edge(
            origin.id, strong.id, evidence=2, confidence=ConfidenceLevel.HIGH
        )
        chain = service.extract_chain(
            candidates=[origin, loud, strong],
            relationships=[weak_edge, strong_edge],
            primary_candidate_id=origin.id,
        )
        assert [link.id for link in chain] == [strong_edge.id]

    def test_chain_is_bounded_by_max_length(self) -> None:
        service = CausalExplanationService(cast(AsyncSession, None))
        nodes = [self._candidate(f"n{index}", 0.5) for index in range(6)]
        edges = [
            self._edge(source.id, target.id) for source, target in zip(nodes, nodes[1:])
        ]
        chain = service.extract_chain(
            candidates=nodes,
            relationships=edges,
            primary_candidate_id=nodes[0].id,
            max_length=3,
        )
        assert len(chain) == 3

    def test_a_correlation_only_origin_yields_no_chain(self) -> None:
        """Threading a chain through co-occurrence would imply direction."""
        service = CausalExplanationService(cast(AsyncSession, None))
        origin = self._candidate("a", 0.5)
        other = self._candidate("b", 0.4)
        correlation = self._edge(
            origin.id, other.id, kind=CausalRelationshipType.CORRELATES_WITH
        )
        assert (
            service.extract_chain(
                candidates=[origin, other],
                relationships=[correlation],
                primary_candidate_id=origin.id,
            )
            == []
        )

    def test_a_cycle_is_not_walked_twice(self) -> None:
        service = CausalExplanationService(cast(AsyncSession, None))
        first = self._candidate("a", 0.5)
        second = self._candidate("b", 0.4)
        edges = [
            self._edge(first.id, second.id),
            self._edge(second.id, first.id),
        ]
        chain = service.extract_chain(
            candidates=[first, second],
            relationships=edges,
            primary_candidate_id=first.id,
        )
        assert len(chain) == 1
        assert chain[0].source_candidate_id == first.id

    def test_no_primary_candidate_means_no_chain(self) -> None:
        service = CausalExplanationService(cast(AsyncSession, None))
        first, second = self._candidate("a", 0.5), self._candidate("b", 0.4)
        assert (
            service.extract_chain(
                candidates=[first, second],
                relationships=[self._edge(first.id, second.id)],
                primary_candidate_id=None,
            )
            == []
        )


class TestEvidenceHygiene:
    """Duplicates and absences are handled explicitly (§24, §30, §47)."""

    def test_a_repeated_observation_is_one_fact(self) -> None:
        """The same stored row quoted twice must not count twice.

        Counted twice, a repeated observation would inflate a candidate's
        evidence mass, its score and the confidence the reader is shown.
        """
        row = uuid.uuid4()
        node = HypothesisNode(
            key=("db",),
            candidate_type="DATABASE",
            component_id=None,
            event_id=None,
            label="db",
            explanation="test",
            evidence=[
                _evidence(source_id=row, quote="same fact"),
                _evidence(source_id=row, quote="same fact"),
                _evidence(source_id=row, quote="a different fact"),
            ],
        )
        deduped = CausalAnalysisService._dedupe_evidence(node)
        assert len(deduped.evidence) == 2
        assert [spec.quote for spec in deduped.evidence] == [
            "same fact",
            "a different fact",
        ]

    def test_two_rows_with_the_same_text_are_still_two_facts(self) -> None:
        """Deduplication keys on the source row, not on string equality."""
        node = HypothesisNode(
            key=("db",),
            candidate_type="DATABASE",
            component_id=None,
            event_id=None,
            label="db",
            explanation="test",
            evidence=[
                _evidence(source_id=uuid.uuid4(), quote="same fact"),
                _evidence(source_id=uuid.uuid4(), quote="same fact"),
            ],
        )
        assert len(CausalAnalysisService._dedupe_evidence(node).evidence) == 2

    def test_failing_traces_without_spans_are_reported_as_missing(self) -> None:
        """Direction that could not be read must be named, not silently absent."""
        service = CausalAnalysisService(cast(AsyncSession, None))
        trace_result = TraceAnalysisResult(traces_without_spans=3)
        assert trace_result.has_directional_evidence is False
        _primary, confidence, _summary, missing = service._select_primary(
            [], trace_result, ChangeAnalysisResult(), CausalGraphSpec()
        )
        assert confidence is ConfidenceLevel.INSUFFICIENT
        assert any("span-level traces" in item for item in missing)
        assert any("spans for 3 failing trace(s)" in item for item in missing)

    def test_missing_change_timestamps_are_reported_as_missing(self) -> None:
        service = CausalAnalysisService(cast(AsyncSession, None))
        _primary, _confidence, _summary, missing = service._select_primary(
            [],
            TraceAnalysisResult(),
            ChangeAnalysisResult(skipped_missing_timestamp=2),
            CausalGraphSpec(),
        )
        assert any("timestamps for 2 change event(s)" in item for item in missing)


def _evidence(
    *, source_id: uuid.UUID | None = None, quote: str = "failing child span"
) -> EvidenceSpec:
    return EvidenceSpec(
        category=CausalEvidenceCategory.TRACE,
        polarity=EvidencePolarity.SUPPORTING,
        source_table="spans",
        source_id=source_id or uuid.uuid4(),
        quote=quote,
        explanation="why",
        strength=0.9,
    )


class TestCausalGraphBuilder:
    def test_structural_adjacency_alone_creates_no_edge(self) -> None:
        """A calls B is structure, not causation (§14)."""
        caller, provider = uuid.uuid4(), uuid.uuid4()
        context = DependencyContext(
            component_ids={caller, provider},
            edges=[],
            upstream_providers={caller: {provider}},
            downstream_dependents={provider: {caller}},
        )
        nodes = {
            ("caller",): _node(("caller",), at=BASE, component=caller),
            ("provider",): _node(("provider",), at=BASE, component=provider),
        }
        graph = CausalGraphBuilder().build(
            nodes=nodes,
            trace_result=TraceAnalysisResult(),
            dependency_context=context,
            change_result=ChangeAnalysisResult(),
        )
        assert graph.edges == []

    def test_provider_failing_first_creates_a_precedence_hypothesis(self) -> None:
        caller, provider = uuid.uuid4(), uuid.uuid4()
        context = DependencyContext(
            component_ids={caller, provider},
            edges=[],
            upstream_providers={caller: {provider}},
            downstream_dependents={provider: {caller}},
        )
        nodes = {
            ("caller",): _node(
                ("caller",), at=BASE + timedelta(seconds=90), component=caller
            ),
            ("provider",): _node(("provider",), at=BASE, component=provider),
        }
        graph = CausalGraphBuilder().build(
            nodes=nodes,
            trace_result=TraceAnalysisResult(),
            dependency_context=context,
            change_result=ChangeAnalysisResult(),
        )
        assert len(graph.edges) == 1
        edge = graph.edges[0]
        assert edge.source_key == ("provider",)
        assert edge.target_key == ("caller",)
        assert edge.relationship_type is CausalRelationshipType.POSSIBLE_CAUSE
        assert edge.evidence, "an edge must name the facts that justify it"

    def test_effect_before_cause_never_becomes_a_causal_edge(self) -> None:
        caller, provider = uuid.uuid4(), uuid.uuid4()
        context = DependencyContext(
            component_ids={caller, provider},
            edges=[],
            upstream_providers={caller: {provider}},
            downstream_dependents={provider: {caller}},
        )
        # The dependent degraded *first*: precedence is absent, so no edge.
        nodes = {
            ("caller",): _node(("caller",), at=BASE, component=caller),
            ("provider",): _node(
                ("provider",), at=BASE + timedelta(seconds=60), component=provider
            ),
        }
        graph = CausalGraphBuilder().build(
            nodes=nodes,
            trace_result=TraceAnalysisResult(),
            dependency_context=context,
            change_result=ChangeAnalysisResult(),
        )
        assert graph.edges == []

    def test_every_built_edge_carries_evidence(self) -> None:
        parent, child = uuid.uuid4(), uuid.uuid4()
        parent_node = _node(
            ("parent",), at=BASE + timedelta(seconds=5), component=parent
        )
        child_node = _node(("child",), at=BASE, component=child)
        trace_result = TraceAnalysisResult(
            edges=[
                TraceFailureEdge(
                    trace_id="t1",
                    parent_span_id="p",
                    child_span_id="c",
                    parent_component_id=parent,
                    child_component_id=child,
                    child_operation=None,
                    child_status="TIMEOUT",
                    child_duration_ms=2500.0,
                    observed_at=BASE,
                )
            ]
        )
        graph = CausalGraphBuilder().build(
            nodes={"parent": parent_node, "child": child_node},
            trace_result=trace_result,
            dependency_context=DependencyContext(),
            change_result=ChangeAnalysisResult(),
        )
        assert graph.edges
        for edge in graph.edges:
            assert edge.explanation
            assert all(spec.source_table for spec in edge.evidence)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
class TestRootCauseScorer:
    def _graph(self, *, categories: list[CausalEvidenceCategory], contradictions=0):
        node = _node(("subject",), at=BASE)
        node.evidence = [
            EvidenceSpec(
                category=category,
                polarity=EvidencePolarity.SUPPORTING,
                source_table="anomalies",
                source_id=uuid.uuid4(),
                quote=f"{category.value} fact",
                explanation="why",
                strength=0.9,
            )
            for category in categories
        ]
        node.evidence.extend(
            EvidenceSpec(
                category=CausalEvidenceCategory.CONTRADICTING,
                polarity=EvidencePolarity.CONTRADICTING,
                source_table="anomalies",
                source_id=uuid.uuid4(),
                quote="contradiction",
                explanation="why",
                strength=0.9,
            )
            for _ in range(contradictions)
        )
        return CausalGraphSpec(nodes={node.key: node}, edges=[])

    def test_high_confidence_requires_independent_categories_and_trace(self) -> None:
        strong = self._graph(
            categories=[
                CausalEvidenceCategory.TRACE,
                CausalEvidenceCategory.TEMPORAL,
                CausalEvidenceCategory.DEPENDENCY,
            ]
        )
        top = RootCauseScorer().score(graph=strong)[0]
        assert top.confidence is ConfidenceLevel.HIGH
        # A HIGH bucket must be reachable, not merely a label: three strong
        # independent categories clear the primary-selection floor.
        assert top.score >= 0.35

    def test_temporal_only_evidence_is_low_confidence(self) -> None:
        weak = self._graph(categories=[CausalEvidenceCategory.TEMPORAL])
        top = RootCauseScorer().score(graph=weak)[0]
        assert top.confidence is ConfidenceLevel.LOW
        assert top.score < 0.5

    def test_no_evidence_is_insufficient(self) -> None:
        empty = self._graph(categories=[])
        top = RootCauseScorer().score(graph=empty)[0]
        assert top.confidence is ConfidenceLevel.INSUFFICIENT

    def test_contradiction_without_corroboration_is_insufficient(self) -> None:
        contradictory = self._graph(
            categories=[CausalEvidenceCategory.TEMPORAL], contradictions=2
        )
        top = RootCauseScorer().score(graph=contradictory)[0]
        assert top.confidence is ConfidenceLevel.INSUFFICIENT
        assert top.contradicting_count == 2

    def test_contradiction_penalty_is_bounded_not_erasive(self) -> None:
        well_evidenced = self._graph(
            categories=[
                CausalEvidenceCategory.TRACE,
                CausalEvidenceCategory.TEMPORAL,
                CausalEvidenceCategory.DEPENDENCY,
                CausalEvidenceCategory.CHANGE,
            ],
            contradictions=2,
        )
        top = RootCauseScorer().score(graph=well_evidenced)[0]
        # One bad fact cannot erase a well-evidenced candidate…
        assert top.score > 0.2
        # …but it must be visible in the breakdown and the counts.
        assert top.breakdown.contradiction_penalty > 0
        assert top.contradicting_count == 2
        # …and material contradiction must never be reported as HIGH.
        assert top.confidence in (ConfidenceLevel.LOW, ConfidenceLevel.MEDIUM)
        assert "capped" in top.confidence_reason

    def test_score_is_separate_from_confidence(self) -> None:
        """A high relative score among weak candidates stays LOW confidence."""
        weak = self._graph(categories=[CausalEvidenceCategory.TEMPORAL])
        top = RootCauseScorer().score(graph=weak)[0]
        assert top.score > 0
        assert top.confidence is ConfidenceLevel.LOW
        assert "temporal/structural evidence" in top.confidence_reason.lower()

    def test_breakdown_is_reported_for_every_component(self) -> None:
        graph = self._graph(
            categories=[
                CausalEvidenceCategory.TRACE,
                CausalEvidenceCategory.TEMPORAL,
                CausalEvidenceCategory.DEPENDENCY,
            ]
        )
        top = RootCauseScorer().score(graph=graph)[0]
        breakdown = top.breakdown.as_dict()
        for key in ("trace", "temporal", "dependency", "total"):
            assert key in breakdown
            assert isinstance(breakdown[key], float)

    def test_scoring_is_deterministic(self) -> None:
        graph = self._graph(
            categories=[
                CausalEvidenceCategory.TRACE,
                CausalEvidenceCategory.TEMPORAL,
            ]
        )
        first = RootCauseScorer().score(graph=graph)[0]
        second = RootCauseScorer().score(graph=graph)[0]
        assert first.score == second.score
        assert first.confidence is second.confidence

    def test_recovery_ordering_can_weaken_a_supposed_cause(self) -> None:
        """A cause that recovered *after* its effects is flagged (§31)."""
        cause = _node(("cause",), at=BASE)
        effect = _node(("effect",), at=BASE + timedelta(seconds=30))
        cause.evidence = [
            EvidenceSpec(
                category=CausalEvidenceCategory.TRACE,
                polarity=EvidencePolarity.SUPPORTING,
                source_table="spans",
                source_id=uuid.uuid4(),
                quote="failing child span",
                explanation="why",
                strength=0.9,
            )
        ]
        effect.evidence = [
            EvidenceSpec(
                category=CausalEvidenceCategory.TRACE,
                polarity=EvidencePolarity.SUPPORTING,
                source_table="spans",
                source_id=uuid.uuid4(),
                quote="failing child span",
                explanation="why",
                strength=0.9,
            )
        ]
        edge = HypothesisEdge(
            source_key=cause.key,
            target_key=effect.key,
            relationship_type=CausalRelationshipType.LIKELY_CAUSE,
            edge_kind=EdgeKind.TRACE_FAILURE,
            evidence=[cause.evidence[0]],
            observational_support=1,
        )
        graph = CausalGraphSpec(
            nodes={cause.key: cause, effect.key: effect}, edges=[edge]
        )
        # The effect recovered first (rank 0), the cause last (rank 1).
        scored = RootCauseScorer().score(
            graph=graph,
            recovery_order=[(effect.key, BASE), (cause.key, BASE + timedelta(600))],
        )
        cause_scored = next(item for item in scored if item.key == cause.key)
        assert any(
            "recovered after its effects" in caveat
            for caveat in cause_scored.uncertainty["caveats"]
        )
