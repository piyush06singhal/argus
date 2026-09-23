"""Phase 5 engine tests (§9, §17–§22, §27–§34, §36–§42, §62).

Unit-level proof for the parts of Phase 5 that make *decisions*: what a replay
does with timing and modes, which faults are legal, how similarity is computed,
when a hypothesis is supported, when an experiment may move between states, and
what an artifact is. Each of these is exercised against real sandboxes only where
the sandbox is what is being tested; everything else is a pure function, because a
decision that can only be checked by running a container is a decision nobody can
regression-test.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models.reproduction import (
    ArtifactType,
    ComparisonDimension,
    ExperimentStatus,
    FailureClass,
    FaultTrigger,
    FaultType,
    ReplayInputSource,
    ReplayMode,
    ReplayStatus,
    ReproductionResult,
    SandboxNetworkPolicy,
    ValidationOutcome,
)
from app.services.environment_snapshot import MAJOR, MINOR, EnvironmentSnapshotService
from app.services.fault_injection import (
    FaultInjectionEngine,
    FaultInjectionError,
    build_spec,
)
from app.services.hypothesis_validator import HypothesisValidator
from app.services.replay_engine import ReplayEngine, ReplayItem
from app.services.reproduction_artifacts import (
    ArtifactError,
    ReproductionArtifactStore,
)
from app.services.reproduction_comparator import (
    DIMENSION_WEIGHTS,
    ComparisonResult,
    ReproductionComparator,
    bucket_for,
)
from app.services.reproduction_context import SourceBehavior
from app.services.reproduction_expectations import (
    ExpectedBehavior,
    ExpectationMatcher,
)
from app.services.reproduction_sandbox import (
    LocalProcessBackend,
    SandboxHandle,
    SandboxManager,
    SandboxSpec,
    load_template,
)
from app.services.reproduction_state import (
    InvalidReproductionTransition,
    allowed_experiment_transitions,
    assert_experiment_transition,
    can_transition_experiment,
    is_experiment_terminal,
)
from app.services.telemetry_capture import (
    CapturedSignal,
    CaptureResult,
    ObservationSignal,
    ObservationStatus,
    TelemetryCaptureService,
)

NOW = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------
class TestReplayModes:
    async def test_a_real_replay_reaches_a_real_service(self) -> None:
        manager = SandboxManager(LocalProcessBackend())
        handle = await manager.create(
            SandboxSpec(
                template="demo_commerce",
                services=["datastore", "inventory", "checkout"],
                timeout_seconds=60,
            ),
            experiment_id=uuid.uuid4(),
        )
        await manager.start(handle)
        try:
            engine = ReplayEngine()
            items = engine.prepare(
                [
                    ReplayItem(
                        method="POST",
                        target_service="checkout",
                        target_path="/checkout",
                        payload={"sku": "SKU-1", "quantity": 1},
                        source=ReplayInputSource.SYNTHETIC,
                    )
                ]
            )
            # No fault: the sandbox is healthy, so the replay must succeed.
            assert engine.validate(items[0], handle) is None
            outcome = await engine.replay(items, handle, mode=ReplayMode.SEQUENTIAL)
            assert outcome.request_count == 1
            assert outcome.success_count == 1
            result = outcome.results[0]
            assert result.status is ReplayStatus.SUCCEEDED
            assert result.status_code == 200
            assert result.duration_ms is not None and result.duration_ms >= 0
            assert result.payload_hash == items[0].hash()
        finally:
            await manager.destroy(handle)

    async def test_timed_replay_waits_for_the_planned_offset(self) -> None:
        manager = SandboxManager(LocalProcessBackend())
        handle = await manager.create(
            SandboxSpec(
                template="demo_commerce",
                services=["datastore", "inventory", "checkout"],
                timeout_seconds=60,
            ),
            experiment_id=uuid.uuid4(),
        )
        await manager.start(handle)
        try:
            engine = ReplayEngine()
            items = engine.prepare(
                [
                    ReplayItem(
                        method="POST",
                        target_service="checkout",
                        target_path="/checkout",
                        payload={"sku": "SKU-1", "quantity": 1},
                        relative_offset_ms=0,
                    ),
                    ReplayItem(
                        method="GET",
                        target_service="inventory",
                        target_path="/inventory/SKU-1",
                        relative_offset_ms=700,
                        plan_order=1,
                    ),
                ]
            )
            started = time.monotonic()
            outcome = await engine.replay(items, handle, mode=ReplayMode.TIMED)
            elapsed = (time.monotonic() - started) * 1000
            assert outcome.success_count == 2
            # Timing is the point of a timed replay, so it must actually wait: a
            # "timed" replay that fires immediately would misrepresent every
            # incident whose failure depended on a delay.
            assert elapsed >= 700, elapsed
            offsets = [item.relative_offset_ms for item in items]
            assert offsets == [0, 700]
        finally:
            await manager.destroy(handle)

    def test_concurrency_is_opt_in_and_bounded(self) -> None:
        from app.services.replay_engine import MAX_PARALLELISM

        assert MAX_PARALLELISM <= 16
        assert ReplayMode.PARALLEL in ReplayMode
        assert ReplayMode.BURST in ReplayMode


# ---------------------------------------------------------------------------
# Fault injection
# ---------------------------------------------------------------------------
class TestFaultEngine:
    def test_target_must_be_a_service_in_this_sandbox(self) -> None:
        with pytest.raises(FaultInjectionError):
            build_spec(
                fault_type=FaultType.LATENCY, target="host", services=["datastore"]
            )

    def test_trigger_requires_its_condition(self) -> None:
        with pytest.raises(FaultInjectionError):
            build_spec(
                fault_type=FaultType.LATENCY,
                target="datastore",
                services=["datastore"],
                trigger=FaultTrigger.AFTER_REPLAY_INDEX,
            )
        # And a condition without its trigger is equally meaningless.
        with pytest.raises(FaultInjectionError):
            build_spec(
                fault_type=FaultType.LATENCY,
                target="datastore",
                services=["datastore"],
                after_replay_index=2,
            )

    def test_manual_and_count_triggers_are_refused(self) -> None:
        # A fault nobody can schedule is not reproducible, so it is not offered.
        for trigger in (FaultTrigger.MANUAL, FaultTrigger.ON_REQUEST_COUNT):
            with pytest.raises(FaultInjectionError):
                build_spec(
                    fault_type=FaultType.LATENCY,
                    target="datastore",
                    services=["datastore"],
                    trigger=trigger,
                )

    def test_magnitude_and_intensity_are_bounded(self) -> None:
        with pytest.raises(FaultInjectionError):
            build_spec(
                fault_type=FaultType.LATENCY,
                target="datastore",
                services=["datastore"],
                duration_ms=10_000_000,
            )
        with pytest.raises(FaultInjectionError):
            build_spec(
                fault_type=FaultType.LATENCY,
                target="datastore",
                services=["datastore"],
                intensity=2.0,
            )

    def test_activation_and_clearing_write_only_inside_the_sandbox(self) -> None:
        handle = _sandbox_handle("faults")
        tmp_path = handle.root_path
        engine = FaultInjectionEngine()
        spec = build_spec(
            fault_type=FaultType.LATENCY,
            target="datastore",
            services=["datastore"],
            parameters={"latency_ms": 1500},
        )
        active = engine.activate(handle, [spec])
        assert len(active) == 1
        assert engine.read_active(tmp_path)[0]["fault_type"] == "LATENCY"
        # A second activation adds rather than duplicating.
        engine.update_triggered(
            handle,
            [
                build_spec(
                    fault_type=FaultType.HTTP_5XX,
                    target="datastore",
                    services=["datastore"],
                    trigger=FaultTrigger.AFTER_REPLAY_INDEX,
                    after_replay_index=0,
                )
            ],
            replay_index=0,
            elapsed_ms=1,
            already_active=set(),
        )
        assert len(engine.read_active(tmp_path)) == 2
        engine.clear(handle)
        assert engine.read_active(tmp_path) == []

    def test_a_fault_on_a_service_outside_the_sandbox_is_never_installed(self) -> None:
        handle = _sandbox_handle("faults-unknown")
        engine = FaultInjectionEngine()
        # Built for a different sandbox's service list, then applied here: the
        # activation must refuse rather than write a fault nothing can honour.
        spec = build_spec(
            fault_type=FaultType.LATENCY, target="payment", services=["payment"]
        )
        assert engine.activate(handle, [spec]) == []

    def test_a_fault_root_outside_the_sandbox_tree_is_refused(self, tmp_path) -> None:
        # The one guarantee that must hold even if every other check is bypassed:
        # a fault is written inside a sandbox directory or not at all (§21).
        handle = SandboxHandle(
            sandbox_key="argus-repro-outside",
            backend=LocalProcessBackend.kind,
            root_path=tmp_path,
            network_policy=SandboxNetworkPolicy.ISOLATED,
            services={"datastore": {"port": 1, "pid": 1}},
        )
        with pytest.raises(FaultInjectionError):
            FaultInjectionEngine().activate(
                handle,
                [
                    build_spec(
                        fault_type=FaultType.LATENCY,
                        target="datastore",
                        services=["datastore"],
                    )
                ],
            )

    def test_the_audit_derives_impact_from_captured_telemetry(self) -> None:
        _ = _sandbox_handle("audit")
        engine = FaultInjectionEngine()
        spec = build_spec(
            fault_type=FaultType.LATENCY, target="datastore", services=["datastore"]
        )

        records = engine.audit(
            specs=[spec],
            observations=[
                _signal(
                    "datastore", error=True, attributes={"injected_fault": "LATENCY"}
                ),
                _signal("inventory", error=True),
                _signal("checkout", error=True),
            ],
            started_at=NOW,
            ended_at=NOW,
        )
        assert len(records) == 1
        # Impact is read off the evidence, so it cannot claim an effect the
        # sandbox never showed.
        assert records[0]["requests_affected"] == 1
        assert records[0]["injected"] is True
        assert records[0]["spec"].describe()["scope"] == "sandbox"

    def test_an_unobserved_fault_is_recorded_as_not_injected(self) -> None:
        _ = _sandbox_handle("audit-none")
        engine = FaultInjectionEngine()
        spec = build_spec(
            fault_type=FaultType.LATENCY, target="datastore", services=["datastore"]
        )
        records = engine.audit(
            specs=[spec], observations=[], started_at=NOW, ended_at=NOW
        )
        assert records[0]["injected"] is False
        assert records[0]["requests_affected"] == 0


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
def _source(**overrides) -> SourceBehavior:
    values = {
        "incident_id": uuid.uuid4(),
        "onset": NOW,
        "window_start": NOW,
        "window_end": NOW,
        "sequence": ["datastore", "inventory", "checkout"],
        "components": ["datastore", "inventory", "checkout"],
        "error_components": ["datastore", "inventory", "checkout"],
        "latency_ms": {"datastore": 2500.0, "inventory": 2700.0, "checkout": 2900.0},
        "error_rate": 0.25,
        "span_count": 12,
        "error_span_count": 3,
        "trace_edges": [
            {
                "parent": "checkout",
                "child": "inventory",
                "operation": "x",
                "error": True,
            },
            {
                "parent": "inventory",
                "child": "datastore",
                "operation": "y",
                "error": True,
            },
        ],
        "log_patterns": {"datastore": ["query timeout after <n> ms"]},
        "recovery_order": [],
    }
    values.update(overrides)
    return SourceBehavior(**values)


def _capture(signals) -> CaptureResult:
    return CaptureResult(namespace="repro:test", signals=list(signals))


def _sandbox_handle(label: str) -> SandboxHandle:
    """A sandbox-shaped directory *inside* the sandbox root.

    Fault injection refuses to write outside the sandbox tree, so a handle has to
    point at a real sandbox directory for the positive cases — and that refusal
    is itself tested.
    """
    import shutil
    import tempfile

    from app.services.reproduction_sandbox import sandbox_root_base

    root = Path(
        tempfile.mkdtemp(prefix=f"argus-repro-{label}-", dir=sandbox_root_base())
    )
    for sub in ("ports", "telemetry", "state", "logs", "artifacts"):
        (root / sub).mkdir(exist_ok=True)
    (root / "faults.json").write_text('{"active": []}', encoding="utf-8")
    _cleanup.append(root)
    _ = shutil  # keep the import meaningful for readers of this helper
    return SandboxHandle(
        sandbox_key=f"argus-repro-{label}",
        backend=LocalProcessBackend.kind,
        root_path=root,
        network_policy=SandboxNetworkPolicy.ISOLATED,
        services={"datastore": {"port": 1, "pid": 1}},
    )


#: Directories created by :func:`_sandbox_handle`, removed by the autouse fixture.
_cleanup: list[Path] = []


@pytest.fixture(autouse=True)
def _cleanup_sandbox_dirs():
    yield
    import shutil

    while _cleanup:
        shutil.rmtree(_cleanup.pop(), ignore_errors=True)


def _signal(
    component: str,
    *,
    error: bool = False,
    duration: float | None = None,
    offset: int = 0,
    observed_at: datetime | None = None,
    signal_type: ObservationSignal = ObservationSignal.SPAN,
    attributes: dict | None = None,
) -> CapturedSignal:
    return CapturedSignal(
        signal_type=signal_type,
        status=ObservationStatus.UNEXPECTED if error else ObservationStatus.NEUTRAL,
        matched_expected=False,
        observed_at=observed_at or NOW,
        namespace="repro:test",
        component_name=component,
        source=component,
        error=error,
        duration_ms=duration,
        relative_offset_ms=offset,
        message="query timeout after 2500 ms" if error else None,
        attributes=dict(attributes or {}),
    )


class TestTelemetrySettle:
    """Waiting for a sandbox to settle (§21).

    The failure mode this guards is silent: a sandbox that keeps working after
    the caller gave up still writes the span that proves the injected fault was
    applied, so capturing early loses exactly the evidence the experiment
    exists to produce.
    """

    @staticmethod
    def _handle(root: Path):
        return SandboxHandle(
            sandbox_key="argus-repro-settle",
            backend=LocalProcessBackend.kind,
            root_path=root,
            network_policy=SandboxNetworkPolicy.ISOLATED,
            #: A service that will never answer the probe: `_sandbox_drained`
            #: is patched to keep reporting "not drained" below.
            services={"datastore": {"port": 1, "pid": 1}},
        )

    async def test_settle_waits_for_telemetry_that_arrives_late(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = TelemetryCaptureService()
        (tmp_path / "telemetry").mkdir()
        handle = self._handle(tmp_path)
        written = asyncio.Event()

        async def never_drained(_handle) -> bool:
            return False

        monkeypatch.setattr(
            TelemetryCaptureService, "_sandbox_drained", staticmethod(never_drained)
        )

        async def late_telemetry() -> None:
            await asyncio.sleep(0.3)
            (tmp_path / "telemetry" / "datastore.jsonl").write_text(
                '{"service": "datastore", "error": true}\n', encoding="utf-8"
            )
            written.set()

        task = asyncio.create_task(late_telemetry())
        #: The probe never answers, so it spends this budget — and the stability
        #: phase must still run afterwards.
        await service.wait_for_quiescence(
            handle, min_wait=0.05, max_wait=1.0, poll=0.05
        )

        #: The wait did not return before the evidence existed. Checking the
        #: event (rather than the elapsed time) is what makes this a test of the
        #: rule and not of the machine's speed.
        assert written.is_set()
        await task

    async def test_settle_returns_promptly_when_nothing_is_produced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A drained sandbox with no telemetry is settled, not waited on."""
        service = TelemetryCaptureService()
        (tmp_path / "telemetry").mkdir()
        handle = self._handle(tmp_path)

        async def drained(_handle) -> bool:
            return True

        monkeypatch.setattr(
            TelemetryCaptureService, "_sandbox_drained", staticmethod(drained)
        )

        started = time.monotonic()
        await service.wait_for_quiescence(
            handle, min_wait=0.05, max_wait=5.0, poll=0.05
        )
        assert time.monotonic() - started < 2.0


class TestComparator:
    def test_bucket_thresholds_are_coarse_on_purpose(self) -> None:
        # §29: a bucket, never a bare probability.
        assert bucket_for(0.95) == "HIGH"
        assert bucket_for(0.70) == "MEDIUM"
        assert bucket_for(0.45) == "LOW"
        assert bucket_for(0.10) == "INSUFFICIENT"
        assert bucket_for(None) == "INSUFFICIENT"

    def test_every_dimension_reports_its_formula(self) -> None:
        comparator = ReproductionComparator()
        comparison = comparator.compare(
            source=_source(),
            capture=_capture(
                [
                    _signal("datastore", error=True, duration=2600, offset=10),
                    _signal("inventory", error=True, duration=2650, offset=2500),
                    _signal("checkout", error=True, duration=2700, offset=2510),
                ]
            ),
        )
        assert set(comparison.dimensions) == {
            item.value for item in ComparisonDimension
        }
        for dimension in comparison.dimensions.values():
            assert dimension["formula"], dimension["dimension"]
            assert "score" in dimension or dimension["available"] is False
        assert comparison.formula_reference
        assert "Component =" in comparison.formula_reference

    def test_an_identical_reproduction_is_high_similarity(self) -> None:
        comparator = ReproductionComparator()
        comparison = comparator.compare(
            source=_source(),
            capture=_capture(
                [
                    _signal("datastore", error=True, duration=2500, offset=100),
                    _signal("inventory", error=True, duration=2700, offset=200),
                    _signal("checkout", error=True, duration=2900, offset=300),
                ]
            ),
            expected=ExpectedBehavior.from_evidence(
                components=["datastore", "inventory", "checkout"],
                sequence=["datastore", "inventory", "checkout"],
                error_components=["datastore", "inventory", "checkout"],
                slow_components=[("datastore", 2500.0)],
            ),
        )
        assert comparison.result is ReproductionResult.SUCCESSFUL
        assert comparison.overall_similarity in {"HIGH", "MEDIUM"}
        assert comparison.matched_components == ["checkout", "datastore", "inventory"]
        assert comparison.sequence_match is True

    def test_onset_order_uses_the_observed_instant_not_the_rounded_offset(
        self,
    ) -> None:
        """Two failures inside one millisecond still have a real order.

        The offset is whole milliseconds, and a faulted chain finishes inside
        one of them — so ordering on it made the *same* experiment report
        `SUCCESSFUL` on one run and `PARTIAL` on the next, decided by which
        telemetry file happened to be parsed first. That was the heaviest
        dimension in the score. This is the regression: the offsets tie, the
        instants do not, and the signals are deliberately listed in the wrong
        order.
        """
        comparator = ReproductionComparator()
        comparison = comparator.compare(
            source=_source(
                sequence=["datastore", "checkout"], components=["datastore", "checkout"]
            ),
            capture=_capture(
                [
                    _signal(
                        "checkout",
                        error=True,
                        offset=6,
                        observed_at=NOW + timedelta(milliseconds=1),
                    ),
                    _signal("datastore", error=True, offset=6, observed_at=NOW),
                ]
            ),
        )
        assert comparison.sequence_reproduced == ["datastore", "checkout"]
        assert comparison.sequence_match is True

    def test_a_clean_sandbox_is_a_failed_reproduction_not_a_success(
        self,
    ) -> None:
        comparator = ReproductionComparator()
        comparison = comparator.compare(
            source=_source(),
            capture=_capture([_signal("checkout", duration=20)]),
            expected=ExpectedBehavior.from_evidence(
                components=["checkout"],
                sequence=["checkout"],
                error_components=["checkout"],
            ),
        )
        assert comparison.result is ReproductionResult.FAILED
        assert comparison.missing_components

    def test_no_captured_telemetry_is_inconclusive_rather_than_failed(self) -> None:
        comparator = ReproductionComparator()
        comparison = comparator.compare(source=_source(), capture=_capture([]))
        # "The sandbox produced nothing" must never be scored as "the failure was
        # absent": they are different findings.
        assert comparison.result is ReproductionResult.INCONCLUSIVE

    def test_a_prefix_elsewhere_in_the_chain_is_partial(self) -> None:
        comparator = ReproductionComparator()
        comparison = comparator.compare(
            source=_source(),
            capture=_capture([_signal("datastore", error=True, duration=2500)]),
            expected=ExpectedBehavior.from_evidence(
                components=["datastore", "inventory", "checkout"],
                sequence=["datastore", "inventory", "checkout"],
                error_components=["datastore", "inventory", "checkout"],
            ),
        )
        assert comparison.result is ReproductionResult.PARTIAL
        assert comparison.expectations_matched >= 1
        assert comparison.expectations_total == 3

    def test_weights_cover_every_dimension(self) -> None:
        assert set(DIMENSION_WEIGHTS) == set(ComparisonDimension)

    def test_an_injected_fault_is_named_in_the_explanation(self) -> None:
        comparator = ReproductionComparator()
        comparison = comparator.compare(
            source=_source(),
            capture=_capture([_signal("datastore", error=True, duration=2500)]),
            faults=[{"injected": True, "target": "datastore", "fault_type": "LATENCY"}],
        )
        # "We forced it" is always part of the narrative: a reproduction ARGUS
        # injected must never read like one that emerged on its own.
        assert "induced rather than emergent" in comparison.explanation
        assert comparison.explanation.startswith("Reproduction PARTIAL:")
        assert "Overall similarity is" in comparison.explanation


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _comparison(
    result: ReproductionResult, similarity: str = "HIGH"
) -> ComparisonResult:
    return ComparisonResult(
        result=result,
        overall_similarity=similarity,
        similarity_score=0.9,
        expectations_total=3,
        expectations_matched=3 if result is ReproductionResult.SUCCESSFUL else 0,
    )


class TestHypothesisValidator:
    def test_all_runs_reproduced_supports(self) -> None:
        outcome = HypothesisValidator().validate(
            hypothesis_statement="h",
            comparisons=[_comparison(ReproductionResult.SUCCESSFUL)],
        )
        assert outcome.outcome is ValidationOutcome.SUPPORTED
        assert outcome.confidence.value in {"MEDIUM", "HIGH"}
        assert outcome.supporting

    def test_a_mixed_result_set_is_partially_supported_and_intermittent(self) -> None:
        result = HypothesisValidator().validate(
            hypothesis_statement="h",
            comparisons=[
                _comparison(ReproductionResult.SUCCESSFUL),
                _comparison(ReproductionResult.FAILED, "LOW"),
                _comparison(ReproductionResult.SUCCESSFUL),
                _comparison(ReproductionResult.FAILED, "LOW"),
            ],
        )
        assert result.outcome is ValidationOutcome.PARTIALLY_SUPPORTED
        assert result.determinism["classification"] == "INTERMITTENT"
        assert result.determinism["reproduction_rate"] == 0.5
        assert "not a probability" in result.determinism["note"]
        assert any("intermittent" in item.lower() for item in result.limitations)

    def test_a_clean_run_refutes_only_when_the_experiment_was_sound(self) -> None:
        validator = HypothesisValidator()
        sound = validator.validate(
            hypothesis_statement="h",
            comparisons=[_comparison(ReproductionResult.FAILED, "LOW")],
        )
        assert sound.outcome is ValidationOutcome.NOT_SUPPORTED

        # The same null result, but with a major environment difference: the
        # verdict must weaken to INCONCLUSIVE instead of blaming the hypothesis.
        unsound = validator.validate(
            hypothesis_statement="h",
            comparisons=[_comparison(ReproductionResult.FAILED, "LOW")],
            environment_differences=[
                {"field": "dependency_versions", "severity": MAJOR}
            ],
        )
        assert unsound.outcome is ValidationOutcome.INCONCLUSIVE
        assert unsound.confidence.value == "INSUFFICIENT"
        assert any("environment" in item for item in unsound.reasoning)

    def test_missing_inputs_also_cap_a_refutation(self) -> None:
        outcome = HypothesisValidator().validate(
            hypothesis_statement="h",
            comparisons=[_comparison(ReproductionResult.FAILED, "LOW")],
            missing_inputs=["The incident recorded no latency measurement"],
        )
        assert outcome.outcome is ValidationOutcome.INCONCLUSIVE

    def test_an_infrastructure_failure_is_not_evidence_about_the_hypothesis(
        self,
    ) -> None:
        outcome = HypothesisValidator().validate(
            hypothesis_statement="h",
            comparisons=[_comparison(ReproductionResult.FAILED, "LOW")],
            failure_classifications=[FailureClass.TIMEOUT],
        )
        assert outcome.outcome is ValidationOutcome.INCONCLUSIVE
        assert any("infrastructural" in item for item in outcome.reasoning)

    def test_no_comparison_at_all_is_inconclusive_with_its_own_reason(self) -> None:
        outcome = HypothesisValidator().validate(
            hypothesis_statement="h", comparisons=[]
        )
        assert outcome.outcome is ValidationOutcome.INCONCLUSIVE
        assert outcome.determinism["classification"] == "NOT_RUN"
        assert outcome.reasoning


# ---------------------------------------------------------------------------
# Expectations
# ---------------------------------------------------------------------------
class TestExpectations:
    def test_expectations_are_derived_from_evidence(self) -> None:
        behavior = ExpectedBehavior.from_evidence(
            components=["checkout"],
            sequence=["checkout"],
            error_components=["checkout"],
            slow_components=[("checkout", 2900.0)],
            provenance={"sources": ["SPAN"]},
        )
        assert behavior.signal_for("checkout", "ERROR") is not None
        assert behavior.signal_for("checkout", "SLOW").threshold_ms == 2900.0
        assert behavior.provenance["sources"] == ["SPAN"]

    def test_slow_is_met_at_eighty_percent_of_the_incident(self) -> None:
        behavior = ExpectedBehavior.from_evidence(
            components=["checkout"],
            sequence=["checkout"],
            slow_components=[("checkout", 1000.0)],
        )
        matcher = ExpectationMatcher(behavior)
        status, matched, _ = matcher.classify(
            component="checkout", error=False, duration_ms=800
        )
        assert matched is True and status == ExpectationMatcher.EXPECTED
        status, matched, _ = matcher.classify(
            component="checkout", error=False, duration_ms=700
        )
        assert matched is False

    def test_an_expectation_nobody_satisfied_is_reported_missing(self) -> None:
        behavior = ExpectedBehavior.from_evidence(
            components=["checkout"],
            sequence=["checkout"],
            error_components=["checkout"],
        )
        matcher = ExpectationMatcher(behavior)
        missing = matcher.missing([_signal("inventory", error=True)])
        assert [item.component for item in missing] == ["checkout"]

    def test_an_unexpected_error_is_labelled_unexpected(self) -> None:
        behavior = ExpectedBehavior.from_evidence(
            components=["checkout"], sequence=["checkout"]
        )
        matcher = ExpectationMatcher(behavior)
        status, matched, _ = matcher.classify(component="checkout", error=True)
        assert status == ExpectationMatcher.UNEXPECTED and matched is False

    def test_round_trip_through_the_stored_form(self) -> None:
        behavior = ExpectedBehavior.from_evidence(
            components=["checkout"],
            sequence=["checkout"],
            error_components=["checkout"],
            slow_components=[("checkout", 500.0)],
        )
        restored = ExpectedBehavior.from_dict(behavior.as_dict())
        assert restored.components == behavior.components
        assert restored.signals[0].kind == behavior.signals[0].kind
        assert ExpectedBehavior.from_dict(None).is_empty


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
class TestLifecycle:
    def test_the_documented_path_is_legal(self) -> None:
        path = [
            ExperimentStatus.PLANNED,
            ExperimentStatus.VALIDATING,
            ExperimentStatus.PROVISIONING,
            ExperimentStatus.READY,
            ExperimentStatus.REPLAYING,
            ExperimentStatus.RUNNING,
            ExperimentStatus.COLLECTING,
            ExperimentStatus.COMPARING,
            ExperimentStatus.COMPLETED,
        ]
        for current, target in zip(path, path[1:]):
            assert can_transition_experiment(current, target), (current, target)
        assert is_experiment_terminal(ExperimentStatus.COMPLETED)

    def test_terminal_states_are_terminal(self) -> None:
        for terminal in (
            ExperimentStatus.COMPLETED,
            ExperimentStatus.FAILED,
            ExperimentStatus.CANCELLED,
            ExperimentStatus.TIMED_OUT,
        ):
            assert is_experiment_terminal(terminal)
            assert allowed_experiment_transitions(terminal) == frozenset()
            assert not can_transition_experiment(terminal, ExperimentStatus.RUNNING)

    def test_illegal_transitions_are_refused_loudly(self) -> None:
        assert not can_transition_experiment(
            ExperimentStatus.PLANNED, ExperimentStatus.COMPLETED
        )
        with pytest.raises(InvalidReproductionTransition):
            assert_experiment_transition(
                ExperimentStatus.PLANNED, ExperimentStatus.READY
            )


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------
class TestArtifactStore:
    def test_artifacts_are_content_addressed_and_verifiable(self, tmp_path) -> None:
        store = ReproductionArtifactStore(tmp_path)
        experiment_id = uuid.uuid4()
        record = store.store(
            experiment_id=experiment_id,
            name="plan.json",
            payload={"a": 1},
            artifact_type=ArtifactType.REPRODUCTION_PLAN,
        )
        assert len(record.content_hash) == 64
        assert record.as_dict()["artifact_type"] == "REPRODUCTION_PLAN"
        assert store.verify(record) is True
        # Re-writing identical content is idempotent; different content is a
        # change to a record that is meant to be immutable.
        again = store.store(
            experiment_id=experiment_id,
            name="plan.json",
            payload={"a": 1},
            artifact_type=ArtifactType.REPRODUCTION_PLAN,
        )
        assert again.content_hash == record.content_hash
        with pytest.raises(ArtifactError):
            store.store(
                experiment_id=experiment_id,
                name="plan.json",
                payload={"a": 2},
                artifact_type=ArtifactType.REPRODUCTION_PLAN,
            )

    def test_an_artifact_name_cannot_escape_its_experiment_directory(
        self, tmp_path
    ) -> None:
        store = ReproductionArtifactStore(tmp_path)
        experiment_id = uuid.uuid4()
        for name in ("../../etc/passwd", "a/../../b", ""):
            with pytest.raises(ArtifactError):
                store.store(
                    experiment_id=experiment_id,
                    name=name,
                    payload={"x": 1},
                    artifact_type=ArtifactType.LOGS,
                )
        # An absolute-looking name is *relocated* inside the experiment's own
        # directory rather than written to the host path it names.
        record = store.store(
            experiment_id=experiment_id,
            name="/etc/passwd",
            payload={"x": 1},
            artifact_type=ArtifactType.LOGS,
        )
        written = (store.root / record.storage_location).resolve()
        assert written.is_relative_to((store.root / str(experiment_id)).resolve())

    def test_tampering_is_detected(self, tmp_path) -> None:
        store = ReproductionArtifactStore(tmp_path)
        experiment_id = uuid.uuid4()
        record = store.store(
            experiment_id=experiment_id,
            name="validation.json",
            payload={"ok": True},
            artifact_type=ArtifactType.VALIDATION_REPORT,
        )
        path = Path(store.root) / record.storage_location
        path.write_text('{"ok": false}', encoding="utf-8")
        assert store.verify(record) is False


# ---------------------------------------------------------------------------
# Environment snapshots
# ---------------------------------------------------------------------------
class TestEnvironmentSnapshot:
    def test_the_declared_environment_is_captured_sanitized_and_hashed(self) -> None:
        service = EnvironmentSnapshotService()
        snapshot = service.capture_original(template="demo_commerce")
        assert snapshot.application_version
        assert snapshot.dependency_versions
        assert snapshot.content_hash
        assert snapshot.metadata["declared_not_captured"] is True
        # Environment variables are allowlisted, so a declared password is not
        # carried into the snapshot at all — and the report says what happened.
        assert "DATABASE_PASSWORD" not in snapshot.environment_variables
        assert snapshot.sanitization

    def test_a_clean_sandbox_reports_no_major_difference(self) -> None:
        from app.services.reproduction_sandbox import SandboxHandle

        service = EnvironmentSnapshotService()
        original = service.capture_original(template="demo_commerce")
        template = load_template("demo_commerce")
        handle = SandboxHandle(
            sandbox_key="argus-repro-snap",
            backend=LocalProcessBackend.kind,
            root_path=Path("/tmp/argus-repro-snap"),
            network_policy=SandboxNetworkPolicy.ISOLATED,
            services={},
            metadata={
                "template": "demo_commerce",
                "service_configs": {
                    service["name"]: dict(service) for service in template["services"]
                },
                "limits": {},
            },
        )
        sandbox = service.capture_sandbox(handle, template="demo_commerce")
        differences = service.diff(original, sandbox)
        major = [item for item in differences if item["severity"] == MAJOR]
        assert major == [], major

    def test_a_version_mismatch_is_major(self) -> None:
        from app.services.reproduction_sandbox import SandboxHandle

        service = EnvironmentSnapshotService()
        original = service.capture_original(template="demo_commerce")
        template = load_template("demo_commerce")
        configs = {item["name"]: dict(item) for item in template["services"]}
        configs["inventory"]["app_version"] = "2.2.0"  # older than the incident's
        handle = SandboxHandle(
            sandbox_key="argus-repro-snap",
            backend=LocalProcessBackend.kind,
            root_path=Path("/tmp/argus-repro-snap"),
            network_policy=SandboxNetworkPolicy.ISOLATED,
            services={},
            metadata={
                "template": "demo_commerce",
                "service_configs": configs,
                "limits": {},
            },
        )
        sandbox = service.capture_sandbox(handle, template="demo_commerce")
        differences = service.diff(original, sandbox)
        assert service.has_major_differences(differences) is True
        changed = [
            key
            for item in differences
            if item["field"] == "dependency_versions"
            for key in item["changed_keys"]
        ]
        assert "inventory" in changed

    def test_a_missing_side_is_reported_rather_than_ignored(self) -> None:
        differences = EnvironmentSnapshotService().diff(None, None)
        assert len(differences) == 1
        assert differences[0]["severity"] == MINOR
