"""ARGUS Reproduction Orchestrator (Phase 5 §15, §22, §37–§43, §54, §55).

Drives one experiment from ``PLANNED`` to a terminal state: provisions a fresh
sandbox per repetition, injects the planned faults, replays the planned inputs,
waits for the sandbox to settle, captures telemetry, compares it against the
incident, validates the hypothesis, stores the artifacts, and destroys the
sandbox.

The parts that carry the phase's guarantees:

* **One sandbox per repetition (§34).** A fresh environment per run is what makes
  "7 of 10 runs reproduced it" a statement about the system rather than about
  accumulated sandbox state.
* **Cleanup is unconditional (§55).** Every path out of a repetition — success,
  failure, timeout, cancellation — goes through a ``finally`` that destroys the
  sandbox and records the attempt. A cleanup failure is written to the row and
  marks it orphaned; it is never swallowed.
* **The lifecycle is a state machine, not a counter (§37).** Every transition
  goes through :mod:`app.services.reproduction_state`, so an illegal ordering is
  impossible rather than merely unlikely.
* **Cancellation and timeouts are checked between phases and inside replay
  (§38, §39).** A long fault is bounded: the replay engine receives a monotonic
  deadline, so a five-minute experiment cannot become an hour-long one.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.causal import (
    ConfidenceLevel,
    RootCauseCandidate,
)
from app.models.reproduction import (
    ArtifactType,
    EnvironmentSnapshot,
    ExperimentStatus,
    FailureClass,
    FaultStatus,
    ReplayMode,
    ReproductionArtifact,
    ReproductionComparison,
    ReproductionExperiment,
    ReproductionFault,
    ReproductionHypothesis,
    ReproductionInput,
    ReproductionObservation,
    ReproductionPlan,
    ReproductionResult,
    ReproductionRun,
    ReproductionSandbox,
    ReproductionValidation,
    RunStatus,
    SandboxNetworkPolicy,
    SandboxStatus,
    SnapshotSource,
)
from app.models.system import SystemComponent
from app.services.environment_snapshot import EnvironmentSnapshotService
from app.services.fault_injection import FaultInjectionEngine
from app.services.hypothesis_validator import HypothesisValidator
from app.services.replay_engine import ReplayEngine, ReplayItem
from app.services.reproduction_artifacts import (
    ArtifactRecord,
    ReproductionArtifactStore,
)
from app.services.reproduction_comparator import (
    ComparisonResult,
    ReproductionComparator,
)
from app.services.reproduction_context import SourceBehaviorLoader, ensure_utc
from app.services.reproduction_expectations import ExpectedBehavior
from app.services.reproduction_planner import (
    PlanData,
    ReproductionPlanner,
)
from app.services.reproduction_sandbox import (
    SandboxError,
    SandboxHandle,
    SandboxManager,
    SandboxSpec,
    load_template,
    sandbox_key_for,
    utcnow,
)
from app.services.reproduction_state import (
    assert_experiment_transition,
    is_experiment_terminal,
)
from app.services.telemetry_capture import CaptureResult, TelemetryCaptureService

logger = logging.getLogger(__name__)
settings = get_settings()

ORPHAN_THRESHOLD_SECONDS = 600


class OrchestrationError(RuntimeError):
    """Raised when an experiment cannot proceed at all."""


@dataclass
class RepetitionOutcome:
    """What one repetition produced, in memory, before persistence."""

    run_index: int
    sandbox_key: str
    handle: Optional[SandboxHandle] = None
    capture: Optional[CaptureResult] = None
    comparison: Optional[ComparisonResult] = None
    replay_outcome: Any = None
    fault_records: list[dict[str, Any]] = None  # type: ignore[assignment]
    failure: Optional[str] = None
    failure_class: Optional[FailureClass] = None
    sandbox_cleanup: dict[str, Any] = None  # type: ignore[assignment]
    duration_ms: int = 0
    faults_injected: int = 0

    def __post_init__(self) -> None:
        if self.fault_records is None:
            self.fault_records = []
        if self.sandbox_cleanup is None:
            self.sandbox_cleanup = {}


def _elapsed_ms(started: float) -> int:
    """Wall-clock milliseconds since ``started`` (a ``time.monotonic()`` value)."""
    return int((time.monotonic() - started) * 1000)


def classify_failure(exc: BaseException) -> FailureClass:
    """Map an exception to the §36 failure taxonomy.

    The taxonomy matters because it is what the validator uses to tell "the
    failure did not reproduce" from "the experiment did not run". A timeout or a
    missing dependency is *not* evidence about the hypothesis.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return FailureClass.TIMEOUT
    if isinstance(exc, SandboxError):
        message = str(exc).lower()
        if "not ready" in message or "exited during startup" in message:
            return FailureClass.ENVIRONMENT_ERROR
        if "daemon is unavailable" in message or "docker" in message:
            return FailureClass.ENVIRONMENT_ERROR
        return FailureClass.SANDBOX_ERROR
    if isinstance(exc, MemoryError):
        return FailureClass.RESOURCE_LIMIT
    if isinstance(exc, OSError):
        text = str(exc).lower()
        if "too many open files" in text or "cannot allocate" in text:
            return FailureClass.RESOURCE_LIMIT
        return FailureClass.ENVIRONMENT_ERROR
    return FailureClass.UNKNOWN


class ReproductionOrchestrator:
    """Runs reproduction experiments end to end."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        template: str = "demo_commerce",
        sandbox_manager: Optional[SandboxManager] = None,
        planner: Optional[ReproductionPlanner] = None,
        replay_engine: Optional[ReplayEngine] = None,
        capture_service: Optional[TelemetryCaptureService] = None,
        comparator: Optional[ReproductionComparator] = None,
        validator: Optional[HypothesisValidator] = None,
        fault_engine: Optional[FaultInjectionEngine] = None,
        artifact_store: Optional[ReproductionArtifactStore] = None,
        snapshot_service: Optional[EnvironmentSnapshotService] = None,
    ) -> None:
        self._factory = session_factory
        self._template = template
        self._sandboxes = sandbox_manager or SandboxManager()
        self._planner = planner or ReproductionPlanner(template=template)
        self._replay = replay_engine or ReplayEngine()
        self._capture = capture_service or TelemetryCaptureService()
        self._comparator = comparator or ReproductionComparator()
        self._validator = validator or HypothesisValidator()
        self._faults = fault_engine or FaultInjectionEngine()
        self._artifacts = artifact_store or ReproductionArtifactStore()
        self._snapshots = snapshot_service or EnvironmentSnapshotService()

    # ------------------------------------------------------------------ plan
    async def prepare(
        self,
        *,
        incident: Any,
        candidate: Optional[RootCauseCandidate],
        candidate_evidence: Sequence[dict[str, Any]] = (),
        analysis_id: Optional[uuid.UUID] = None,
        repetitions: Optional[int] = None,
        replay_mode: Optional[ReplayMode] = None,
        network_policy: Optional[SandboxNetworkPolicy] = None,
        timeout_seconds: Optional[int] = None,
        strategy: Optional[Any] = None,
        fault_overrides: Sequence[dict[str, Any]] = (),
        input_overrides: Sequence[ReplayItem] = (),
        requested_by: Optional[str] = None,
        trigger: str = "api",
    ) -> ReproductionExperiment:
        """Create the experiment, its plan, hypothesis, and snapshots.

        Planning is where an impossible experiment is refused: a hypothesis whose
        component is not in the reproduction template raises here, before an
        experiment row exists, so the API can return a clear 422 instead of a
        half-created record that can never run.
        """
        async with self._factory() as session:
            loader = SourceBehaviorLoader(session)
            source = await loader.load(incident)
            component_name = await self._component_name(session, candidate)

            plan_data = self._planner.plan(
                incident_id=incident.id,
                project_id=incident.project_id,
                source=source,
                candidate=candidate,
                candidate_component_name=component_name,
                candidate_type=candidate.candidate_type if candidate else None,
                candidate_confidence=candidate.confidence if candidate else None,
                candidate_evidence=candidate_evidence,
                analysis_id=analysis_id,
                repetitions=repetitions,
                replay_mode=replay_mode,
                network_policy=network_policy,
                timeout_seconds=timeout_seconds,
                strategy=strategy,
                fault_overrides=fault_overrides,
                input_overrides=input_overrides,
            )

            version = (
                await session.scalar(
                    select(
                        func.coalesce(
                            func.max(ReproductionExperiment.experiment_version), 0
                        )
                    ).where(ReproductionExperiment.incident_id == incident.id)
                )
                or 0
            )
            experiment = ReproductionExperiment(
                project_id=incident.project_id,
                environment_id=incident.environment_id,
                incident_id=incident.id,
                causal_analysis_id=analysis_id,
                candidate_id=candidate.id if candidate else None,
                experiment_version=int(version) + 1,
                status=ExperimentStatus.PLANNED,
                result=ReproductionResult.NOT_RUN,
                confidence="INSUFFICIENT",
                trigger=trigger,
                requested_by=requested_by,
                repetitions=plan_data.repetitions,
                telemetry_namespace=self._capture.namespace_for("pending"),
                metadata_={
                    "plan": plan_data.as_dict(),
                    "missing_inputs": plan_data.missing_inputs,
                    "template": plan_data.template,
                },
            )
            session.add(experiment)
            await session.flush()
            # Assigned *after* the flush: the primary key is generated by the
            # insert, so naming the namespace beforehand would persist
            # ``repro:None`` for every experiment (§24).
            experiment.telemetry_namespace = self._capture.namespace_for(experiment.id)
            await session.flush()

            session.add(
                ReproductionPlan(
                    experiment_id=experiment.id,
                    project_id=experiment.project_id,
                    strategy=plan_data.strategy,
                    target_component_id=plan_data.target_component_id,
                    target_component_name=plan_data.target_component_name,
                    target_version=plan_data.target_version,
                    objectives=plan_data.objectives,
                    required_services=plan_data.required_services,
                    required_dependencies=plan_data.required_dependencies,
                    input_sources=plan_data.input_sources,
                    expected_behavior=(
                        plan_data.expected_behavior.as_dict()
                        if plan_data.expected_behavior
                        else None
                    ),
                    safety_constraints=plan_data.safety_constraints,
                    resource_limits=plan_data.resource_limits,
                    network_policy=plan_data.network_policy,
                    timeout_seconds=plan_data.timeout_seconds,
                    repetitions=plan_data.repetitions,
                    derived_from=plan_data.derived_from,
                )
            )
            session.add(
                ReproductionHypothesis(
                    experiment_id=experiment.id,
                    project_id=experiment.project_id,
                    source_analysis_id=analysis_id,
                    candidate_id=candidate.id if candidate else None,
                    candidate_type=plan_data.hypothesis.get("candidate_type"),
                    component_id=plan_data.target_component_id,
                    component_name=plan_data.target_component_name,
                    statement=plan_data.hypothesis["statement"],
                    expected_failure=plan_data.hypothesis.get("expected_failure"),
                    expected_components=plan_data.hypothesis.get("expected_components"),
                    expected_sequence=plan_data.hypothesis.get("expected_sequence"),
                    expected_signals=plan_data.hypothesis.get("expected_signals"),
                    expected_time_window_seconds=plan_data.hypothesis.get(
                        "expected_time_window_seconds"
                    ),
                    supporting_evidence=plan_data.hypothesis.get("supporting_evidence"),
                )
            )
            for spec in plan_data.faults:
                session.add(
                    ReproductionFault(
                        experiment_id=experiment.id,
                        project_id=experiment.project_id,
                        fault_type=spec.fault_type,
                        target=spec.target,
                        target_component_id=plan_data.target_component_id,
                        scope="sandbox",
                        trigger=spec.trigger,
                        parameters=spec.parameters or None,
                        duration_ms=spec.duration_ms,
                        intensity=spec.intensity,
                        status=FaultStatus.PLANNED,
                        injected=False,
                    )
                )
            for order, item in enumerate(plan_data.items):
                session.add(
                    ReproductionInput(
                        experiment_id=experiment.id,
                        project_id=experiment.project_id,
                        input_index=order,
                        plan_order=item.plan_order,
                        source=item.source,
                        method=item.method,
                        target_service=item.target_service,
                        target_path=item.target_path,
                        payload=item.payload,
                        payload_hash=item.hash(),
                        relative_offset_ms=item.relative_offset_ms,
                        status=None,
                    )
                )

            # Snapshots are captured at plan time so the comparison has a
            # baseline even if the sandbox never starts.
            original = self._snapshots.capture_original(
                template=plan_data.template,
                environment_name=plan_data.environment_name,
            )
            session.add(
                EnvironmentSnapshot(
                    experiment_id=experiment.id,
                    project_id=experiment.project_id,
                    source=SnapshotSource.ORIGINAL,
                    label=original.label,
                    captured_at=original.captured_at,
                    application_version=original.application_version,
                    schema_version=original.schema_version,
                    runtime_versions=original.runtime_versions,
                    dependency_versions=original.dependency_versions,
                    configuration=original.configuration,
                    environment_variables=original.environment_variables,
                    feature_flags=original.feature_flags,
                    service_topology=original.service_topology,
                    resource_limits=original.resource_limits,
                    sanitization=original.sanitization,
                    content_hash=original.content_hash,
                    snapshot_metadata=original.metadata,
                )
            )
            await session.commit()
            await session.refresh(experiment)
            return experiment

    @staticmethod
    async def _component_name(
        session: AsyncSession, candidate: Optional[RootCauseCandidate]
    ) -> Optional[str]:
        if candidate is None or candidate.component_id is None:
            return None
        component = await session.get(SystemComponent, candidate.component_id)
        return component.name if component else None

    # ----------------------------------------------------------------- start
    async def run_experiment(self, experiment_id: uuid.UUID) -> ReproductionExperiment:
        """Execute a planned experiment to a terminal state."""
        async with self._factory() as session:
            experiment = await session.get(ReproductionExperiment, experiment_id)
            if experiment is None:
                raise OrchestrationError(f"Unknown experiment {experiment_id}")
            if is_experiment_terminal(experiment.status):
                # Idempotent: a retried job must not re-run a finished experiment.
                return experiment

        plan_data, source, hypothesis, expected = await self._load_plan(experiment_id)
        deadline_monotonic = time.monotonic() + plan_data.timeout_seconds

        await self._set_status(
            experiment_id,
            ExperimentStatus.VALIDATING,
            summary="Validating the plan and the sandbox environment",
        )
        try:
            await self._validate(plan_data, expected)
        except Exception as exc:  # noqa: BLE001 - recorded, never silent
            logger.error("Experiment %s failed validation: %s", experiment_id, exc)
            await self._set_status(
                experiment_id,
                ExperimentStatus.FAILED,
                failure_classification=classify_failure(exc),
                summary=f"Validation failed: {exc}",
            )
            return await self._get(experiment_id)

        sandbox_spec = SandboxSpec(
            template=plan_data.template,
            network_policy=plan_data.network_policy,
            timeout_seconds=plan_data.timeout_seconds,
            services=plan_data.required_services,
            resource_limits=plan_data.resource_limits,
        )

        outcomes: list[RepetitionOutcome] = []
        terminal_reason: Optional[str] = None
        terminal_status: Optional[ExperimentStatus] = None

        for run_index in range(1, plan_data.repetitions + 1):
            if await self._cancel_requested(experiment_id):
                terminal_status = ExperimentStatus.CANCELLED
                terminal_reason = "Cancelled by an operator before the run started"
                break
            if time.monotonic() > deadline_monotonic:
                terminal_status = ExperimentStatus.TIMED_OUT
                terminal_reason = (
                    f"Experiment exceeded its {plan_data.timeout_seconds}s budget"
                )
                break

            outcome = await self._run_repetition(
                experiment_id=experiment_id,
                run_index=run_index,
                plan_data=plan_data,
                source=source,
                expected=expected,
                sandbox_spec=sandbox_spec,
                deadline_monotonic=deadline_monotonic,
            )
            outcomes.append(outcome)
            if outcome.failure_class is FailureClass.TIMEOUT:
                terminal_status = ExperimentStatus.TIMED_OUT
                terminal_reason = outcome.failure or "Run timed out"
                break
            if outcome.failure and outcome.failure_class in {
                FailureClass.ENVIRONMENT_ERROR,
                FailureClass.SANDBOX_ERROR,
            }:
                terminal_status = ExperimentStatus.FAILED
                terminal_reason = outcome.failure
                break

        await self._finalize(
            experiment_id=experiment_id,
            plan_data=plan_data,
            expected=expected,
            source=source,
            hypothesis=hypothesis,
            outcomes=outcomes,
            terminal_status=terminal_status,
            terminal_reason=terminal_reason,
        )
        return await self._get(experiment_id)

    async def _validate(self, plan_data: PlanData, expected: ExpectedBehavior) -> None:
        """Refuse to run an experiment that cannot produce a usable observation."""
        if not plan_data.items:
            raise OrchestrationError(
                "The plan contains no replayable inputs, so nothing would exercise "
                "the hypothesised failure"
            )
        if plan_data.expected_behavior is None or expected.is_empty:
            raise OrchestrationError(
                "The plan has no expected behaviour: the incident carries no "
                "comparable telemetry, so a comparison would be meaningless"
            )
        # Resolving the backend here means a missing Docker daemon fails as a
        # validation error with a clear message, before a sandbox row exists.
        from app.services.reproduction_sandbox import backend_for

        backend_for()

    # ------------------------------------------------------------ repetition
    async def _run_repetition(
        self,
        *,
        experiment_id: uuid.UUID,
        run_index: int,
        plan_data: PlanData,
        source: Any,
        expected: ExpectedBehavior,
        sandbox_spec: SandboxSpec,
        deadline_monotonic: float,
    ) -> RepetitionOutcome:
        """Provision, inject, replay, capture, compare — and always clean up."""
        started = time.monotonic()
        outcome = RepetitionOutcome(
            run_index=run_index,
            # One sandbox per repetition, so the name carries the run index.
            sandbox_key=sandbox_key_for(experiment_id, run_index),
        )
        handle: Optional[SandboxHandle] = None
        sandbox_row_id: Optional[uuid.UUID] = None
        run_id: Optional[uuid.UUID] = None

        try:
            await self._set_status(
                experiment_id,
                ExperimentStatus.PROVISIONING,
                summary=f"Provisioning sandbox for run {run_index}",
            )
            run_id = await self._create_run(experiment_id, plan_data, run_index)

            # ---- provision -------------------------------------------------
            try:
                handle = await self._sandboxes.create(
                    sandbox_spec, experiment_id=experiment_id, run_index=run_index
                )
                outcome.handle = handle
                sandbox_row_id = await self._create_sandbox_row(
                    experiment_id, handle, plan_data
                )
                handle.metadata["keep_workdir"] = False
                await self._sandboxes.start(handle)
                await self._mark_sandbox(
                    sandbox_row_id, SandboxStatus.READY, handle=handle
                )
            except SandboxError as exc:
                outcome.failure = str(exc)
                outcome.failure_class = classify_failure(exc)
                outcome.duration_ms = _elapsed_ms(started)
                await self._fail_run(experiment_id, run_id, outcome)
                return outcome

            await self._record_sandbox_snapshot(experiment_id, handle)
            await self._set_status(
                experiment_id,
                ExperimentStatus.READY,
                summary=f"Sandbox {handle.sandbox_key} ready",
            )

            # ---- inject + replay -------------------------------------------
            await self._set_status(
                experiment_id,
                ExperimentStatus.REPLAYING,
                summary=f"Replaying {len(plan_data.items)} input(s) into the sandbox",
            )
            active = self._faults.activate(handle, plan_data.faults)
            outcome.faults_injected = len(active)
            activated_keys = {
                f"{item['fault_type']}:{item['target']}" for item in active
            }
            replay_started = utcnow()

            async def on_item(index: int, result: Any) -> None:
                elapsed_ms = int((utcnow() - replay_started).total_seconds() * 1000)
                self._faults.update_triggered(
                    handle,
                    plan_data.faults,
                    replay_index=index,
                    elapsed_ms=elapsed_ms,
                    already_active=activated_keys,
                )

            replay_outcome = await self._replay.replay(
                plan_data.items,
                handle,
                mode=plan_data.replay_mode,
                deadline=deadline_monotonic,
                on_item=on_item,
            )
            outcome.replay_outcome = replay_outcome
            await self._set_status(
                experiment_id,
                ExperimentStatus.RUNNING,
                summary=(
                    f"{replay_outcome.success_count} replay(s) observed, "
                    f"{replay_outcome.failure_count} failed"
                ),
            )

            # Faults are cleared before capture: a fault still active at capture
            # time would be indistinguishable from continued failure.
            self._faults.clear(handle)
            await self._set_status(
                experiment_id,
                ExperimentStatus.COLLECTING,
                summary="Waiting for the sandbox to settle, then capturing telemetry",
            )
            capture = await self._capture.capture(
                handle,
                experiment_id=experiment_id,
                expected=expected,
                run_started_at=replay_started,
            )
            outcome.capture = capture

            outcome.fault_records = self._faults.audit(
                specs=plan_data.faults,
                observations=capture.signals,
                started_at=replay_started,
                ended_at=utcnow(),
            )

            comparison = self._comparator.compare(
                source=source,
                capture=capture,
                expected=expected,
                # Translation of incident component names into sandbox service
                # names happens here, once, from the template the plan names — so
                # the comparison compares like with like.
                alias_map=self._planner.alias_map(
                    source=source, template=load_template(plan_data.template)
                ),
                faults=[
                    record["spec"].describe() | {"injected": record["injected"]}
                    for record in outcome.fault_records
                ],
            )
            outcome.comparison = comparison

            await self._persist_run_details(
                experiment_id=experiment_id,
                run_id=run_id,
                plan_data=plan_data,
                outcome=outcome,
                sandbox_row_id=sandbox_row_id,
            )
            # Stamp the duration *before* finalizing: the run is written once,
            # and a duration written later would leave a completed run reading
            # ``0 ms`` for however long the finalize and cleanup take.
            outcome.duration_ms = _elapsed_ms(started)
            await self._complete_run(experiment_id, run_id, outcome)

        except asyncio.CancelledError:  # pragma: no cover - cooperative cancel
            outcome.failure = "Run cancelled"
            outcome.failure_class = FailureClass.UNKNOWN
            raise
        except Exception as exc:  # noqa: BLE001 - every failure is recorded
            logger.exception("Repetition %s failed", run_index)
            outcome.failure = f"{type(exc).__name__}: {exc}"
            outcome.failure_class = classify_failure(exc)
            outcome.duration_ms = _elapsed_ms(started)
            if run_id is not None:
                await self._fail_run(experiment_id, run_id, outcome)

        finally:
            outcome.duration_ms = _elapsed_ms(started)
            if handle is not None:
                outcome.sandbox_cleanup = await self._sandboxes.destroy(handle)
                if sandbox_row_id is not None:
                    await self._mark_sandbox(
                        sandbox_row_id,
                        SandboxStatus.DESTROYED,
                        handle=handle,
                        cleanup=outcome.sandbox_cleanup,
                    )
            # The duration now covers teardown too, so the stored value is
            # restamped once the repetition has fully unwound.
            if run_id is not None:
                await self._stamp_run_duration(run_id, outcome.duration_ms)
        return outcome

    # -------------------------------------------------------------- finalize
    async def _finalize(
        self,
        *,
        experiment_id: uuid.UUID,
        plan_data: PlanData,
        expected: ExpectedBehavior,
        source: Any,
        hypothesis: ReproductionHypothesis,
        outcomes: Sequence[RepetitionOutcome],
        terminal_status: Optional[ExperimentStatus],
        terminal_reason: Optional[str],
    ) -> None:
        """Compare, validate, store artifacts, and close the experiment out."""
        await self._set_status(
            experiment_id,
            ExperimentStatus.COMPARING,
            summary="Comparing the reproduction against the original incident",
        )

        comparisons = [
            outcome.comparison for outcome in outcomes if outcome.comparison is not None
        ]
        differences = await self._difference_analysis(experiment_id)
        fault_payload = self._fault_payload(outcomes)

        validation = self._validator.validate(
            hypothesis_statement=hypothesis.statement,
            comparisons=comparisons,
            environment_differences=differences,
            missing_inputs=plan_data.missing_inputs,
            faults=fault_payload,
            failure_classifications=[
                outcome.failure_class for outcome in outcomes if outcome.failure_class
            ],
            replay_reached_services=any(
                outcome.capture is not None and outcome.capture.total_signals > 0
                for outcome in outcomes
            ),
        )
        await self._persist_validation(
            experiment_id=experiment_id,
            hypothesis=hypothesis,
            validation=validation,
        )

        await self._persist_artifacts(
            experiment_id=experiment_id, plan_data=plan_data, outcomes=outcomes
        )
        await self._record_faults(experiment_id, outcomes)

        result = self._overall_result(outcomes)
        cleanup_failed = [
            outcome for outcome in outcomes if outcome.sandbox_cleanup.get("error")
        ]
        summary = validation.summary
        if cleanup_failed:
            summary += (
                f" Warning: {len(cleanup_failed)} sandbox cleanup attempt(s) failed "
                "and were recorded as orphans for retry."
            )

        status = terminal_status
        if status is None:
            status = ExperimentStatus.COMPLETED if outcomes else ExperimentStatus.FAILED
        if terminal_reason:
            summary = f"{terminal_reason}. {summary}"

        await self._set_status(
            experiment_id,
            status,
            result=result,
            confidence=validation.confidence,
            failure_classification=self._closing_failure_class(
                outcomes=outcomes,
                terminal_status=terminal_status,
            ),
            summary=summary,
            extra_metadata={
                "validation_outcome": validation.outcome.value,
                "determinism": validation.determinism,
                "runs": [
                    {
                        "run_index": outcome.run_index,
                        "result": (
                            outcome.comparison.result.value
                            if outcome.comparison
                            else ReproductionResult.NOT_RUN.value
                        ),
                        "similarity": (
                            outcome.comparison.overall_similarity
                            if outcome.comparison
                            else None
                        ),
                        "failure_class": (
                            outcome.failure_class.value
                            if outcome.failure_class
                            else None
                        ),
                        "duration_ms": outcome.duration_ms,
                    }
                    for outcome in outcomes
                ],
            },
        )

    # ---------------------------------------------------------------- helpers
    async def _load_plan(
        self, experiment_id: uuid.UUID
    ) -> tuple[PlanData, Any, ReproductionHypothesis, ExpectedBehavior]:
        """Rebuild the executable plan and the incident context from storage."""
        async with self._factory() as session:
            experiment = await session.get(ReproductionExperiment, experiment_id)
            if experiment is None:
                raise OrchestrationError(f"Unknown experiment {experiment_id}")
            plan_row = (
                await session.execute(
                    select(ReproductionPlan).where(
                        ReproductionPlan.experiment_id == experiment_id
                    )
                )
            ).scalar_one_or_none()
            hypothesis = (
                await session.execute(
                    select(ReproductionHypothesis).where(
                        ReproductionHypothesis.experiment_id == experiment_id
                    )
                )
            ).scalar_one_or_none()
            if plan_row is None or hypothesis is None:
                raise OrchestrationError(
                    "Experiment has no plan or hypothesis; it was never planned"
                )

            fault_rows = list(
                (
                    await session.execute(
                        select(ReproductionFault).where(
                            ReproductionFault.experiment_id == experiment_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            input_rows = list(
                (
                    await session.execute(
                        select(ReproductionInput)
                        .where(ReproductionInput.experiment_id == experiment_id)
                        .order_by(ReproductionInput.plan_order)
                    )
                )
                .scalars()
                .all()
            )
            metadata = dict(experiment.metadata_ or {})
            plan_metadata = dict(metadata.get("plan") or {})
            expected = ExpectedBehavior.from_dict(plan_row.expected_behavior)

            plan_data = PlanData(
                template=str(plan_metadata.get("template") or self._template),
                strategy=plan_row.strategy,
                target_service=plan_metadata.get("target_service"),
                target_component_id=plan_row.target_component_id,
                target_component_name=plan_row.target_component_name,
                target_version=plan_row.target_version,
                objectives=dict(plan_row.objectives or {}),
                required_services=list(plan_row.required_services or []),
                required_dependencies=list(plan_row.required_dependencies or []),
                input_sources=list(plan_row.input_sources or []),
                expected_behavior=expected,
                safety_constraints=dict(plan_row.safety_constraints or {}),
                resource_limits=dict(plan_row.resource_limits or {}),
                network_policy=plan_row.network_policy,
                timeout_seconds=plan_row.timeout_seconds,
                repetitions=plan_row.repetitions,
                replay_mode=_replay_mode_from(plan_row.derived_from),
                derived_from=dict(plan_row.derived_from or {}),
                faults=[self._fault_from_row(row) for row in fault_rows],
                items=[
                    ReplayItem(
                        method=str(row.method or "GET"),
                        target_service=str(row.target_service or ""),
                        target_path=str(row.target_path or "/"),
                        payload=dict(row.payload or {}),
                        relative_offset_ms=int(row.relative_offset_ms or 0),
                        source=row.source,
                        plan_order=int(row.plan_order or 0),
                    )
                    for row in input_rows
                ],
                hypothesis={},
                missing_inputs=list(metadata.get("missing_inputs") or []),
                environment_name=(plan_row.derived_from or {}).get("template"),
            )
            # The source behaviour is not stored as an artifact of the row, so it
            # is re-derived from the incident the same way the planner derived it.
            from app.models.incident import Incident  # local: avoid import cycle

            incident = await session.get(Incident, experiment.incident_id)
            if incident is None:
                raise OrchestrationError("The experiment's incident no longer exists")
            source = await SourceBehaviorLoader(session).load(incident)
            return plan_data, source, hypothesis, expected

    def _fault_from_row(self, row: ReproductionFault):
        from app.services.fault_injection import FaultSpec

        return FaultSpec(
            fault_type=row.fault_type,
            target=row.target,
            trigger=row.trigger,
            duration_ms=row.duration_ms,
            intensity=row.intensity,
            parameters=dict(row.parameters or {}),
        )

    @staticmethod
    def _closing_failure_class(
        *,
        outcomes: Sequence[RepetitionOutcome],
        terminal_status: Optional[ExperimentStatus],
    ) -> Optional[FailureClass]:
        """The §36 classification for the experiment as a whole.

        Precedence: an explicit timeout outranks a run-level cause, and a run
        that failed for an infrastructural reason is reported as such — that is
        the fact the validator reads to avoid mistaking "we could not run it" for
        "it did not happen".
        """
        if terminal_status is ExperimentStatus.TIMED_OUT:
            return FailureClass.TIMEOUT
        if not outcomes:
            return FailureClass.UNKNOWN
        return outcomes[-1].failure_class if outcomes[-1].failure else None

    async def _difference_analysis(
        self, experiment_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        """Diff the captured snapshots (§33)."""
        async with self._factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(EnvironmentSnapshot)
                        .where(EnvironmentSnapshot.experiment_id == experiment_id)
                        .order_by(EnvironmentSnapshot.captured_at)
                    )
                )
                .scalars()
                .all()
            )
        original = next(
            (row for row in rows if row.source is SnapshotSource.ORIGINAL), None
        )
        sandbox = next(
            (row for row in reversed(rows) if row.source is SnapshotSource.SANDBOX),
            None,
        )
        if original is None:
            return []
        return self._snapshots.diff(
            self._snapshot_to_data(original),
            self._snapshot_to_data(sandbox) if sandbox is not None else None,
        )

    @staticmethod
    def _snapshot_to_data(row: EnvironmentSnapshot):
        from app.services.environment_snapshot import SnapshotData

        return SnapshotData(
            source=row.source.value,
            captured_at=ensure_utc(row.captured_at),
            label=row.label,
            application_version=row.application_version,
            schema_version=row.schema_version,
            runtime_versions=dict(row.runtime_versions or {}),
            dependency_versions=dict(row.dependency_versions or {}),
            configuration=dict(row.configuration or {}),
            environment_variables=dict(row.environment_variables or {}),
            feature_flags=dict(row.feature_flags or {}),
            service_topology=dict(row.service_topology or {}),
            resource_limits=dict(row.resource_limits or {}),
            sanitization=dict(row.sanitization or {}),
            content_hash=row.content_hash,
        )

    @staticmethod
    def _fault_payload(outcomes: Sequence[RepetitionOutcome]) -> list[dict[str, Any]]:
        if not outcomes:
            return []
        return [
            {
                "target": record["spec"].target,
                "injected": record["injected"],
                "fault_type": record["spec"].fault_type.value,
            }
            for record in outcomes[0].fault_records
        ]

    @staticmethod
    def _overall_result(outcomes: Sequence[RepetitionOutcome]) -> ReproductionResult:
        results = [
            outcome.comparison.result
            for outcome in outcomes
            if outcome.comparison is not None
        ]
        if not results:
            # No repetition produced a comparison. If nothing ran at all (a
            # cancellation before the first run), the honest value is NOT_RUN:
            # INCONCLUSIVE would suggest an attempt whose evidence was unusable.
            return (
                ReproductionResult.NOT_RUN
                if not outcomes
                else ReproductionResult.INCONCLUSIVE
            )
        if ReproductionResult.SUCCESSFUL in results:
            return ReproductionResult.SUCCESSFUL
        if ReproductionResult.PARTIAL in results:
            return ReproductionResult.PARTIAL
        if ReproductionResult.INCONCLUSIVE in results:
            return ReproductionResult.INCONCLUSIVE
        return ReproductionResult.FAILED

    # ------------------------------------------------------------ persistence
    async def _get(self, experiment_id: uuid.UUID) -> ReproductionExperiment:
        async with self._factory() as session:
            experiment = await session.get(ReproductionExperiment, experiment_id)
            if experiment is None:  # pragma: no cover - deleted mid-run
                raise OrchestrationError(f"Unknown experiment {experiment_id}")
            return experiment

    async def _set_status(
        self,
        experiment_id: uuid.UUID,
        status: ExperimentStatus,
        *,
        result: Optional[ReproductionResult] = None,
        confidence: Optional[ConfidenceLevel] = None,
        summary: Optional[str] = None,
        failure_classification: Optional[FailureClass] = None,
        lifecycle: bool = True,
        extra_metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        from app.services.reproduction_state import EXPERIMENT_TIMESTAMP_FIELDS

        async with self._factory() as session:
            experiment = await session.get(ReproductionExperiment, experiment_id)
            if experiment is None:
                return
            if experiment.status is not status:
                assert_experiment_transition(experiment.status, status)
            experiment.status = status
            if result is not None:
                experiment.result = result
            if confidence is not None:
                experiment.confidence = confidence
            if summary is not None:
                experiment.summary = summary
            if failure_classification is not None:
                experiment.failure_classification = failure_classification
            if lifecycle:
                field_name = EXPERIMENT_TIMESTAMP_FIELDS.get(status)
                if field_name and getattr(experiment, field_name) is None:
                    setattr(experiment, field_name, utcnow())
                if (
                    status is ExperimentStatus.VALIDATING
                    and experiment.started_at is None
                ):
                    experiment.started_at = utcnow()
                    experiment.timeout_at = experiment.started_at + timedelta(
                        seconds=self._planned_timeout(experiment)
                    )
            if extra_metadata:
                metadata = dict(experiment.metadata_ or {})
                metadata.update(extra_metadata)
                experiment.metadata_ = metadata
            await session.commit()

    @staticmethod
    def _planned_timeout(experiment: ReproductionExperiment) -> int:
        """The plan's timeout, falling back to the configured default.

        The value is copied onto ``timeout_at`` so a reaper can close an
        experiment whose worker died without needing to parse the plan (§39).
        """
        metadata = dict(experiment.metadata_ or {})
        plan = metadata.get("plan")
        planned = plan.get("timeout_seconds") if isinstance(plan, dict) else None
        try:
            return int(planned or settings.REPRO_EXPERIMENT_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            return settings.REPRO_EXPERIMENT_TIMEOUT_SECONDS

    async def _create_run(
        self, experiment_id: uuid.UUID, plan_data: PlanData, run_index: int
    ) -> uuid.UUID:
        async with self._factory() as session:
            experiment = await session.get(ReproductionExperiment, experiment_id)
            run = ReproductionRun(
                experiment_id=experiment_id,
                project_id=experiment.project_id if experiment else uuid.uuid4(),
                run_index=run_index,
                status=RunStatus.RUNNING,
                result=ReproductionResult.NOT_RUN,
                started_at=utcnow(),
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)
            return run.id

    async def _complete_run(
        self,
        experiment_id: uuid.UUID,
        run_id: uuid.UUID,
        outcome: RepetitionOutcome,
    ) -> None:
        await self._update_run(
            experiment_id,
            run_id,
            status=RunStatus.COMPLETED,
            result=outcome.comparison.result
            if outcome.comparison
            else ReproductionResult.INCONCLUSIVE,
            failure_classification=outcome.failure_class,
            failed=False,
            outcome=outcome,
        )

    async def _fail_run(
        self,
        experiment_id: uuid.UUID,
        run_id: uuid.UUID,
        outcome: RepetitionOutcome,
    ) -> None:
        await self._update_run(
            experiment_id,
            run_id,
            status=(
                RunStatus.TIMED_OUT
                if outcome.failure_class is FailureClass.TIMEOUT
                else RunStatus.FAILED
            ),
            result=ReproductionResult.INCONCLUSIVE,
            failure_classification=outcome.failure_class,
            failed=True,
            outcome=outcome,
        )

    async def _stamp_run_duration(self, run_id: uuid.UUID, duration_ms: int) -> None:
        """Record a run's wall-clock duration once it has fully unwound."""
        async with self._factory() as session:
            await session.execute(
                update(ReproductionRun)
                .where(ReproductionRun.id == run_id)
                .values(duration_ms=duration_ms)
            )
            await session.commit()

    async def _update_run(
        self,
        experiment_id: uuid.UUID,
        run_id: uuid.UUID,
        *,
        status: RunStatus,
        result: ReproductionResult,
        failure_classification: Optional[FailureClass],
        failed: bool,
        outcome: RepetitionOutcome,
    ) -> None:
        """Finalize one repetition and advance the experiment's run counter.

        The counter is incremented in the same transaction that finalizes the
        run and with a SQL-level ``+ 1``: a read-modify-write would lose an
        increment if two repetitions ever finalized concurrently, and a
        counter written only at the end would be zero for an experiment that
        crashed mid-way — exactly when the operator needs the true progress.
        """
        async with self._factory() as session:
            run = await session.get(ReproductionRun, run_id)
            if run is None:
                return
            run.status = status
            run.result = result
            run.failure_classification = failure_classification
            run.completed_at = utcnow()
            run.duration_ms = outcome.duration_ms
            run.error = outcome.failure
            if outcome.replay_outcome is not None:
                run.replay_request_count = outcome.replay_outcome.request_count
                run.replay_success_count = outcome.replay_outcome.success_count
                run.replay_failure_count = outcome.replay_outcome.failure_count
                run.replay_rejected_count = outcome.replay_outcome.rejected_count
            if outcome.capture is not None:
                run.observation_count = outcome.capture.total_signals
                run.telemetry_bytes = outcome.capture.bytes_read
            run.faults_applied = [
                record["spec"].describe() | {"injected": record["injected"]}
                for record in outcome.fault_records
            ] or None
            if failed:
                run.notes = (
                    f"Run failed ({failure_classification.value if failure_classification else 'UNKNOWN'}): "
                    f"{outcome.failure}"
                )
            await session.execute(
                update(ReproductionExperiment)
                .where(ReproductionExperiment.id == experiment_id)
                .values(completed_runs=ReproductionExperiment.completed_runs + 1)
            )
            await session.commit()

    async def _create_sandbox_row(
        self,
        experiment_id: uuid.UUID,
        handle: SandboxHandle,
        plan_data: PlanData,
    ) -> uuid.UUID:
        async with self._factory() as session:
            experiment = await session.get(ReproductionExperiment, experiment_id)
            row = ReproductionSandbox(
                experiment_id=experiment_id,
                project_id=experiment.project_id if experiment else None,
                sandbox_key=handle.sandbox_key,
                backend=handle.backend,
                status=SandboxStatus.CREATING,
                network_policy=handle.network_policy,
                root_path=str(handle.root_path),
                resource_limits=dict(plan_data.resource_limits),
                created_at_sandbox=handle.created_at,
                metadata_={
                    "template": plan_data.template,
                    "services_planned": plan_data.required_services,
                    "rlimit_applied": handle.metadata.get("rlimit_applied"),
                },
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row.id

    async def _mark_sandbox(
        self,
        sandbox_id: uuid.UUID,
        status: SandboxStatus,
        *,
        handle: Optional[SandboxHandle] = None,
        cleanup: Optional[dict[str, Any]] = None,
    ) -> None:
        """Update a sandbox row, always recording cleanup attempts (§55)."""
        async with self._factory() as session:
            row = await session.get(ReproductionSandbox, sandbox_id)
            if row is None:
                return
            row.status = status
            if handle is not None:
                row.services = {
                    name: dict(entry) for name, entry in handle.services.items()
                }
                row.process_ids = list(handle.process_ids)
                row.container_ids = list(handle.container_ids)
                if status is SandboxStatus.READY:
                    row.started_at = datetime.now(timezone.utc)
            if status is SandboxStatus.STOPPED:
                row.stopped_at = datetime.now(timezone.utc)
            if status is SandboxStatus.DESTROYED:
                row.destroyed_at = datetime.now(timezone.utc)
            if cleanup is not None:
                row.cleanup_attempts = (row.cleanup_attempts or 0) + 1
                row.cleanup_error = cleanup.get("error")
                row.orphaned = bool(cleanup.get("error")) or (
                    cleanup.get("destroyed") is False
                )
            await session.commit()

    async def _record_sandbox_snapshot(
        self, experiment_id: uuid.UUID, handle: SandboxHandle
    ) -> None:
        snapshot = self._snapshots.capture_sandbox(handle, template=self._template)
        async with self._factory() as session:
            experiment = await session.get(ReproductionExperiment, experiment_id)
            session.add(
                EnvironmentSnapshot(
                    experiment_id=experiment_id,
                    project_id=experiment.project_id if experiment else uuid.uuid4(),
                    source=SnapshotSource.SANDBOX,
                    label=snapshot.label,
                    captured_at=snapshot.captured_at,
                    application_version=snapshot.application_version,
                    schema_version=snapshot.schema_version,
                    runtime_versions=snapshot.runtime_versions,
                    dependency_versions=snapshot.dependency_versions,
                    configuration=snapshot.configuration,
                    environment_variables=snapshot.environment_variables,
                    feature_flags=snapshot.feature_flags,
                    service_topology=snapshot.service_topology,
                    resource_limits=snapshot.resource_limits,
                    sanitization=snapshot.sanitization,
                    content_hash=snapshot.content_hash,
                    snapshot_metadata=snapshot.metadata,
                )
            )
            await session.commit()

    async def _persist_run_details(
        self,
        *,
        experiment_id: uuid.UUID,
        run_id: uuid.UUID,
        plan_data: PlanData,
        outcome: RepetitionOutcome,
        sandbox_row_id: Optional[uuid.UUID],
    ) -> None:
        """Write observations, replay outcomes, and the comparison for one run."""
        capture = outcome.capture
        if capture is None:
            return
        async with self._factory() as session:
            experiment = await session.get(ReproductionExperiment, experiment_id)
            project_id = experiment.project_id if experiment else uuid.uuid4()
            namespace = capture.namespace

            session.add_all(
                [
                    ReproductionObservation(
                        experiment_id=experiment_id,
                        project_id=project_id,
                        run_id=run_id,
                        sandbox_id=sandbox_row_id,
                        namespace=namespace,
                        signal_type=signal.signal_type,
                        status=signal.status,
                        matched_expected=signal.matched_expected,
                        observed_at=signal.observed_at,
                        relative_offset_ms=signal.relative_offset_ms,
                        component_name=signal.component_name,
                        source=signal.source,
                        metric_name=signal.metric_name,
                        value=signal.value,
                        unit=signal.unit,
                        expected_value=signal.expected_value,
                        severity=signal.severity,
                        message=(signal.message or "")[:4000] or None,
                        operation=signal.operation,
                        duration_ms=signal.duration_ms,
                        error=signal.error,
                        trace_id=signal.trace_id,
                        span_id=signal.span_id,
                        parent_span_id=signal.parent_span_id,
                        attributes=signal.attributes or None,
                    )
                    for signal in capture.observations
                ]
            )

            if outcome.replay_outcome is not None:
                input_rows = {
                    row.id: row
                    for row in (
                        await session.execute(
                            select(ReproductionInput).where(
                                ReproductionInput.experiment_id == experiment_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                }
                for row in input_rows.values():
                    row.run_id = run_id
                for result in outcome.replay_outcome.results:
                    matching = next(
                        (
                            row
                            for row in input_rows.values()
                            if row.plan_order == result.plan_order
                        ),
                        None,
                    )
                    if matching is None:
                        continue
                    matching.replay_id = result.replay_id
                    matching.status = result.status
                    matching.status_code = result.status_code
                    matching.duration_ms = result.duration_ms
                    matching.replay_timestamp = result.replay_timestamp
                    matching.response_summary = result.response_summary
                    matching.reject_reason = result.reject_reason
                    matching.error = result.error

            if outcome.comparison is not None:
                comparison = outcome.comparison
                session.add(
                    ReproductionComparison(
                        experiment_id=experiment_id,
                        project_id=project_id,
                        run_id=run_id,
                        overall_similarity=comparison.overall_similarity,
                        similarity_score=comparison.similarity_score,
                        result=comparison.result,
                        dimensions=comparison.dimensions,
                        formula_reference=comparison.formula_reference,
                        component_overlap=comparison.component_overlap,
                        matched_components=comparison.matched_components,
                        missing_components=comparison.missing_components,
                        extra_components=comparison.extra_components,
                        sequence_original=comparison.sequence_original,
                        sequence_reproduced=comparison.sequence_reproduced,
                        sequence_match=comparison.sequence_match,
                        metric_deltas=comparison.metric_deltas,
                        error_comparison=comparison.error_comparison,
                        trace_topology=comparison.trace_topology,
                        log_pattern=comparison.log_pattern,
                        recovery=comparison.recovery,
                        temporal=comparison.temporal,
                        original_summary=comparison.original_summary,
                        reproduced_summary=comparison.reproduced_summary,
                        explanation=comparison.explanation,
                    )
                )
            await session.commit()

    async def _record_faults(
        self, experiment_id: uuid.UUID, outcomes: Sequence[RepetitionOutcome]
    ) -> None:
        """Update fault rows with the derived audit (§22)."""
        async with self._factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(ReproductionFault).where(
                            ReproductionFault.experiment_id == experiment_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return
            for row in rows:
                statuses: list[FaultStatus] = []
                affected = 0
                result_notes: list[str] = []
                for outcome in outcomes:
                    for record in outcome.fault_records:
                        spec = record["spec"]
                        if (
                            spec.target == row.target
                            and spec.fault_type == row.fault_type
                        ):
                            statuses.append(FaultStatus(record["status"]))
                            affected += int(record["requests_affected"])
                            result_notes.append(str(record["result"]))
                if not statuses:
                    row.status = FaultStatus.SKIPPED
                    row.result = "The experiment never reached the injection phase"
                    continue
                row.status = (
                    FaultStatus.COMPLETED
                    if FaultStatus.COMPLETED in statuses
                    else statuses[-1]
                )
                row.injected = any(
                    record["injected"]
                    for outcome in outcomes
                    for record in outcome.fault_records
                    if record["spec"].target == row.target
                    and record["spec"].fault_type == row.fault_type
                )
                row.requests_affected = affected
                row.result = "; ".join(dict.fromkeys(result_notes))[:2000]
            await session.commit()

    async def _persist_validation(
        self,
        *,
        experiment_id: uuid.UUID,
        hypothesis: ReproductionHypothesis,
        validation: Any,
    ) -> None:
        async with self._factory() as session:
            experiment = await session.get(ReproductionExperiment, experiment_id)
            existing = (
                await session.execute(
                    select(ReproductionValidation).where(
                        ReproductionValidation.experiment_id == experiment_id
                    )
                )
            ).scalar_one_or_none()
            payload = {
                "experiment_id": experiment_id,
                "project_id": experiment.project_id if experiment else uuid.uuid4(),
                "candidate_id": hypothesis.candidate_id,
                "outcome": validation.outcome,
                "confidence": validation.confidence,
                "summary": validation.summary,
                "supporting_observations": validation.supporting,
                "contradicting_observations": validation.contradicting,
                "environment_differences": validation.environment_differences,
                "missing_inputs": validation.missing_inputs,
                "determinism": validation.determinism,
                "limitations": validation.limitations,
            }
            if existing is None:
                session.add(ReproductionValidation(**payload))
            else:
                for key, value in payload.items():
                    setattr(existing, key, value)
            await session.commit()

    async def _persist_artifacts(
        self,
        *,
        experiment_id: uuid.UUID,
        plan_data: PlanData,
        outcomes: Sequence[RepetitionOutcome],
    ) -> None:
        """Store every artifact the experiment produced (§41)."""
        records: list[ArtifactRecord] = []

        async def store(
            name: str, payload: Any, artifact_type: ArtifactType, **kw: Any
        ):
            try:
                record = self._artifacts.store(
                    experiment_id=experiment_id,
                    name=name,
                    payload=payload,
                    artifact_type=artifact_type,
                    **kw,
                )
                records.append(record)
            except Exception as exc:  # noqa: BLE001 - artifacts must not lose the run
                logger.error("Artifact %s could not be stored: %s", name, exc)

        await store(
            "plan.json",
            plan_data.as_dict(),
            ArtifactType.REPRODUCTION_PLAN,
        )
        await store(
            "manifest.json",
            plan_data.manifest(),
            ArtifactType.REPRODUCTION_MANIFEST,
        )

        for outcome in outcomes:
            run_label = f"run-{outcome.run_index}"
            if outcome.capture is not None:
                await store(
                    f"{run_label}/telemetry.json",
                    [
                        {
                            "signal_type": signal.signal_type.value,
                            "status": signal.status.value,
                            "component": signal.component_name,
                            "operation": signal.operation,
                            "duration_ms": signal.duration_ms,
                            "error": signal.error,
                            "message": signal.message,
                            "offset_ms": signal.relative_offset_ms,
                            "attributes": signal.attributes,
                        }
                        for signal in outcome.capture.observations
                    ],
                    ArtifactType.TELEMETRY_SNAPSHOT,
                    run_id=None,
                )
            if outcome.replay_outcome is not None:
                await store(
                    f"{run_label}/replay-manifest.json",
                    [
                        {
                            "replay_id": result.replay_id,
                            "target": f"{result.target_service}{result.path}",
                            "status": result.status.value,
                            "status_code": result.status_code,
                            "duration_ms": result.duration_ms,
                            "payload_hash": result.payload_hash,
                            "reject_reason": result.reject_reason,
                            "error": result.error,
                        }
                        for result in outcome.replay_outcome.results
                    ],
                    ArtifactType.REPLAY_MANIFEST,
                )
            if outcome.comparison is not None:
                comparison = outcome.comparison
                await store(
                    f"{run_label}/comparison.json",
                    {
                        "result": comparison.result.value,
                        "overall_similarity": comparison.overall_similarity,
                        "similarity_score": comparison.similarity_score,
                        "dimensions": comparison.dimensions,
                        "formula_reference": comparison.formula_reference,
                        "sequence": {
                            "original": comparison.sequence_original,
                            "reproduced": comparison.sequence_reproduced,
                            "match": comparison.sequence_match,
                        },
                        "explanation": comparison.explanation,
                    },
                    ArtifactType.COMPARISON_RESULT,
                )
            if outcome.fault_records:
                await store(
                    f"{run_label}/faults.json",
                    [
                        {
                            "fault_type": record["spec"].fault_type.value,
                            "target": record["spec"].target,
                            "status": record["status"],
                            "injected": record["injected"],
                            "requests_affected": record["requests_affected"],
                            "result": record["result"],
                        }
                        for record in outcome.fault_records
                    ],
                    ArtifactType.FAULT_RECORD,
                )
            if outcome.handle is not None:
                await store(
                    f"{run_label}/sandbox.json",
                    {
                        "sandbox_key": outcome.handle.sandbox_key,
                        "backend": outcome.handle.backend.value,
                        "network_policy": outcome.handle.network_policy.value,
                        "services": outcome.handle.services,
                        "cleanup": outcome.sandbox_cleanup,
                        "resource_limits": outcome.handle.metadata.get("limits"),
                        "rlimit_applied": outcome.handle.metadata.get("rlimit_applied"),
                    },
                    ArtifactType.SANDBOX_METADATA,
                )

        validation_row = None
        async with self._factory() as session:
            validation_row = (
                await session.execute(
                    select(ReproductionValidation).where(
                        ReproductionValidation.experiment_id == experiment_id
                    )
                )
            ).scalar_one_or_none()
        if validation_row is not None:
            await store(
                "validation.json",
                {
                    "outcome": validation_row.outcome.value,
                    "confidence": validation_row.confidence.value,
                    "summary": validation_row.summary,
                    "supporting": validation_row.supporting_observations,
                    "contradicting": validation_row.contradicting_observations,
                    "environment_differences": validation_row.environment_differences,
                    "determinism": validation_row.determinism,
                    "limitations": validation_row.limitations,
                },
                ArtifactType.VALIDATION_REPORT,
            )

        if records:
            async with self._factory() as session:
                experiment = await session.get(ReproductionExperiment, experiment_id)
                project_id = experiment.project_id if experiment else uuid.uuid4()
                for record in records:
                    session.add(
                        ReproductionArtifact(
                            experiment_id=experiment_id,
                            project_id=project_id,
                            artifact_type=record.artifact_type,
                            name=record.name,
                            content_type=record.content_type,
                            storage_location=record.storage_location,
                            size_bytes=record.size_bytes,
                            content_hash=record.content_hash,
                            immutable=True,
                            metadata_=record.metadata or None,
                        )
                    )
                metadata = dict(experiment.metadata_ or {}) if experiment else {}
                names = set(metadata.get("artifact_names") or []) | {
                    record.name for record in records
                }
                metadata["artifact_names"] = sorted(names)
                metadata["artifact_count"] = len(names)
                if experiment is not None:
                    experiment.metadata_ = metadata
                await session.commit()

    # ------------------------------------------------------------ operations
    async def request_cancel(
        self, experiment_id: uuid.UUID, reason: Optional[str] = None
    ) -> ReproductionExperiment:
        """Flag a running experiment for cancellation (§38).

        Cooperative rather than forceful: the running repetition sees the flag
        between phases and unwinds through its ``finally``, so the sandbox is
        destroyed and the audit is written. Killing the worker would leave the
        sandbox behind.
        """
        async with self._factory() as session:
            experiment = await session.get(ReproductionExperiment, experiment_id)
            if experiment is None:
                raise OrchestrationError(f"Unknown experiment {experiment_id}")
            if is_experiment_terminal(experiment.status):
                return experiment
            experiment.cancel_requested_at = utcnow()
            metadata = dict(experiment.metadata_ or {})
            metadata["cancel_reason"] = reason
            experiment.metadata_ = metadata
            await session.commit()
            await session.refresh(experiment)
            return experiment

    async def _cancel_requested(self, experiment_id: uuid.UUID) -> bool:
        async with self._factory() as session:
            experiment = await session.get(ReproductionExperiment, experiment_id)
            return bool(experiment and experiment.cancel_requested_at)

    async def sweep_stale(
        self,
        *,
        paused_project_ids: Optional[set[uuid.UUID]] = None,
    ) -> dict[str, Any]:
        """Reap timed-out experiments and orphaned sandboxes (§39, §54, §55).

        Runs from the periodic sweep. Two jobs: close experiments whose deadline
        passed while nobody was driving them, and destroy sandbox rows whose
        experiment is terminal or gone. Both are reported, because a reaper that
        silently fails is indistinguishable from a leak.

        ``paused_project_ids`` carries the scopes a Phase 9 ``PAUSE_BACKGROUND_JOB``
        remediation has paused this reaper in. The reaper has no project loop of
        its own, so a pause is honoured per row — pausing ``reproduction_sweep``
        for one project must not withhold reaping from every other project, which
        would be a far wider effect than the action's own blast radius.
        """
        now = datetime.now(timezone.utc)
        paused_project_ids = paused_project_ids or set()
        timed_out: list[str] = []
        cleaned: list[str] = []
        orphans: list[str] = []
        paused_skipped = 0

        async with self._factory() as session:
            stale = list(
                (
                    await session.execute(
                        select(ReproductionExperiment).where(
                            ReproductionExperiment.timeout_at.is_not(None),
                            ReproductionExperiment.timeout_at < now,
                            ReproductionExperiment.status.notin_(
                                [
                                    ExperimentStatus.COMPLETED,
                                    ExperimentStatus.FAILED,
                                    ExperimentStatus.CANCELLED,
                                    ExperimentStatus.TIMED_OUT,
                                ]
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
        for experiment in stale:
            if getattr(experiment, "project_id", None) in paused_project_ids:
                paused_skipped += 1
                continue
            await self._set_status(
                experiment.id,
                ExperimentStatus.TIMED_OUT,
                summary=(
                    "Timed out: the experiment exceeded its budget and no worker was "
                    "driving it. Any sandbox it owned has been scheduled for cleanup."
                ),
            )
            timed_out.append(str(experiment.id))

        async with self._factory() as session:
            live_sandboxes = list(
                (
                    await session.execute(
                        select(ReproductionSandbox).where(
                            ReproductionSandbox.status.in_(
                                [
                                    SandboxStatus.CREATING,
                                    SandboxStatus.READY,
                                    SandboxStatus.STOPPING,
                                ]
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
        for sandbox in live_sandboxes:
            if getattr(sandbox, "project_id", None) in paused_project_ids:
                paused_skipped += 1
                continue
            owner = await self._get_optional(sandbox.experiment_id)
            should_clean = owner is None or is_experiment_terminal(owner.status)
            if not should_clean and owner is not None and owner.timeout_at:
                should_clean = ensure_utc(owner.timeout_at) < now
            if not should_clean:
                continue
            handle = _handle_from_row(sandbox)
            cleanup = await self._sandboxes.destroy(handle)
            await self._mark_sandbox(
                sandbox.id, SandboxStatus.DESTROYED, handle=handle, cleanup=cleanup
            )
            cleaned.append(sandbox.sandbox_key)
            if cleanup.get("error"):
                orphans.append(sandbox.sandbox_key)

        return {
            "timed_out": timed_out,
            "sandboxes_cleaned": cleaned,
            "cleanup_failures": orphans,
            "paused_skipped": paused_skipped,
            "checked_at": now.isoformat(),
        }

    async def _get_optional(
        self, experiment_id: Optional[uuid.UUID]
    ) -> Optional[ReproductionExperiment]:
        if experiment_id is None:
            return None
        async with self._factory() as session:
            return await session.get(ReproductionExperiment, experiment_id)


def _replay_mode_from(derived_from: Optional[dict[str, Any]]) -> ReplayMode:
    """Recover the plan's replay mode, defaulting only if it was never stored."""
    raw = (derived_from or {}).get("replay_mode")
    if raw:
        try:
            return ReplayMode(str(raw))
        except ValueError:
            pass
    return ReproductionPlanner.default_replay_mode()


def _handle_from_row(row: ReproductionSandbox) -> SandboxHandle:
    """Rebuild a handle for cleanup from a sandbox row.

    Only the fields cleanup needs are reconstructed; a reaper never starts
    anything, so nothing here can resurrect a sandbox.
    """
    from pathlib import Path

    return SandboxHandle(
        sandbox_key=row.sandbox_key,
        backend=row.backend,
        root_path=Path(row.root_path or ""),
        network_policy=row.network_policy,
        services=dict(row.services or {}),
        process_ids=[int(pid) for pid in (row.process_ids or [])],
        container_ids=list(row.container_ids or []),
        metadata={
            "keep_workdir": False,
            "cleanup_only": True,
        },
        network_name=str((row.metadata_ or {}).get("network_name") or "") or None,
    )


__all__ = [
    "ORPHAN_THRESHOLD_SECONDS",
    "OrchestrationError",
    "RepetitionOutcome",
    "ReproductionOrchestrator",
    "classify_failure",
]
