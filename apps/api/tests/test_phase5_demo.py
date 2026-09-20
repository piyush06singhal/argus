"""Phase 5 scenario tests (§58–§61) — the engine, executed for real.

These are Phase 5's integration tests, and they exercise the *whole* engine: a
Phase 3 incident is detected from ingested telemetry, Phase 4 derives a
hypothesis, Phase 5 plans an experiment from that hypothesis, provisions a real
sandbox, starts real processes, replays real HTTP requests, injects a real
fault, captures the telemetry those processes emitted, compares it against the
incident, validates the hypothesis, stores artifacts, and destroys the sandbox.

Nothing here is stubbed and nothing is told the answer:

* **Successful reproduction (§58, §59)** — the datastore hypothesis is injected
  at the magnitude the incident exhibited and the expected chain must actually
  fail inside the sandbox.
* **Counterexample (§60)** — a deployment hypothesis is planned as an
  *un-injected baseline run*. If the failure occurs anyway, the change was not
  necessary; if it does not, the change is implicated. The test asserts the
  engine reaches that conclusion from the experiment rather than by assumption.
* **Intermittent (§61)** — a probabilistic fault on the same incident, repeated,
  where the honest assertion is that the observed repeatability is reported as
  an observation over the runs that actually happened.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.causal import CandidateType
from app.models.incident import Incident
from app.models.reproduction import (
    ExperimentStatus,
    FaultType,
    ReproductionArtifact,
    ReproductionComparison,
    ReproductionExperiment,
    ReproductionFault,
    ReproductionResult,
    ReproductionRun,
    ReproductionSandbox,
    ReproductionValidation,
    SandboxStatus,
    ValidationOutcome,
)
from app.services.reproduction_orchestrator import ReproductionOrchestrator
from app.services.reproduction_sandbox import sandbox_root_base
from test_phase4_demo import _analyse_checkout


def _factory(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


async def _prepare(
    db_session: AsyncSession,
    db_engine,
    *,
    candidate_type: CandidateType | None = None,
    repetitions: int = 1,
    fault_overrides: list[dict] | None = None,
):
    """Build the checkout incident, pick a hypothesis, and plan the experiment."""
    _project, _env, components, analysis, candidates = await _analyse_checkout(
        db_session
    )
    await db_session.commit()
    incident = await db_session.get(Incident, analysis.incident_id)
    assert incident is not None

    if candidate_type is None:
        candidate = candidates[0]
    else:
        candidate = next(
            (item for item in candidates if item.candidate_type is candidate_type),
            None,
        )
        assert candidate is not None, f"scenario produced no {candidate_type} candidate"

    orchestrator = ReproductionOrchestrator(_factory(db_engine))
    experiment = await orchestrator.prepare(
        incident=incident,
        candidate=candidate,
        analysis_id=analysis.id,
        repetitions=repetitions,
        fault_overrides=fault_overrides or (),
        trigger="pytest",
    )
    return orchestrator, experiment, candidate, components, incident


class TestSuccessfulReproductionScenario:
    """§58, §59 — the datastore hypothesis, reproduced in a real sandbox."""

    async def test_the_planned_experiment_records_why_it_can_run(
        self, db_session: AsyncSession, db_engine
    ) -> None:
        orchestrator, experiment, candidate, _components, _incident = await _prepare(
            db_session, db_engine
        )
        plan = experiment.metadata_["plan"]
        # The plan must name the hypothesis, the chain, and the expected signals —
        # an experiment whose expectation is not written down cannot be compared.
        assert plan["target_service"] == "datastore"
        assert plan["expected_behavior"] is not None
        assert plan["expected_behavior"]["components"]
        assert plan["faults"], "a database hypothesis must plan an injected fault"
        assert plan["faults"][0]["fault_type"] == "LATENCY"
        assert plan["faults"][0]["target"] == "datastore"
        # Safety constraints are part of the plan, not the UI (§47).
        assert plan["safety_constraints"]["production_access"] == "BLOCKED"
        assert plan["safety_constraints"]["arbitrary_commands"].startswith("REJECTED")
        # And the plan is versioned and owned by the hypothesis it tests.
        assert experiment.candidate_id == candidate.id
        assert experiment.experiment_version == 1

    async def test_the_sandbox_reproduces_the_failure_end_to_end(
        self, db_session: AsyncSession, db_engine
    ) -> None:
        orchestrator, experiment, _candidate, _components, _incident = await _prepare(
            db_session, db_engine
        )
        sandbox_base = sandbox_root_base()
        before = {item.name for item in sandbox_base.glob("argus-repro-*")}

        finished = await orchestrator.run_experiment(experiment.id)
        await db_session.commit()

        assert finished.status is ExperimentStatus.COMPLETED, (
            f"experiment did not complete: {finished.status} "
            f"({finished.summary}); failure={finished.failure_classification}"
        )
        assert (
            finished.result is ReproductionResult.SUCCESSFUL
        ), f"reproduction result was {finished.result}: {finished.summary}"
        assert finished.started_at is not None and finished.completed_at is not None
        assert finished.confidence.value in {"MEDIUM", "HIGH"}
        # The run counter is what the history and live views report progress
        # with; a column that is never written would show "0 of 1" forever.
        assert finished.completed_runs == 1, finished.completed_runs

        # One repetition, one run, one sandbox — and the run observed traffic.
        run = (
            await db_session.execute(
                select(ReproductionRun).where(
                    ReproductionRun.experiment_id == experiment.id
                )
            )
        ).scalar_one()
        # A run that did real work must report how long it took: the duration is
        # written when the repetition finalizes, and a value stamped afterwards
        # would leave every completed run reading 0 ms.
        assert run.duration_ms and run.duration_ms > 0, run.duration_ms
        # Every planned request was actually sent — rejected requests mean the
        # safety gate stopped the replay, which would invalidate the run.
        assert run.replay_request_count >= 1
        assert run.replay_rejected_count == 0
        # The fault was on the datastore, so the entry request must have failed;
        # a clean replay would mean the injected fault never propagated.
        assert run.replay_failure_count >= 1
        assert run.observation_count > 0
        assert run.result is ReproductionResult.SUCCESSFUL

        # The comparison is stored per run with its dimensions explained.
        comparison = (
            await db_session.execute(
                select(ReproductionComparison).where(
                    ReproductionComparison.experiment_id == experiment.id
                )
            )
        ).scalar_one()
        assert comparison.result is ReproductionResult.SUCCESSFUL
        assert comparison.dimensions, "a similarity score must carry its dimensions"
        assert comparison.component_overlap is not None
        assert comparison.component_overlap["inputs"]["matched"] == [
            "checkout",
            "datastore",
            "inventory",
        ], comparison.component_overlap["inputs"]
        # The sequence the incident showed must be the sequence the sandbox showed.
        assert comparison.sequence_reproduced == [
            "datastore",
            "inventory",
            "checkout",
        ], comparison.sequence_reproduced
        assert comparison.sequence_match is True

        # The verdict is separate from the result and must be SUPPORTED here.
        validation = (
            await db_session.execute(
                select(ReproductionValidation).where(
                    ReproductionValidation.experiment_id == experiment.id
                )
            )
        ).scalar_one()
        assert validation.outcome is ValidationOutcome.SUPPORTED
        assert validation.supporting_observations
        assert validation.determinism["runs"] == 1
        assert validation.environment_differences is not None

        # Faults are audited separately from the failure they caused (§22).
        faults = list(
            (
                await db_session.execute(
                    select(ReproductionFault).where(
                        ReproductionFault.experiment_id == experiment.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(faults) == 1
        assert faults[0].fault_type is FaultType.LATENCY
        assert faults[0].target == "datastore"
        assert faults[0].scope == "sandbox"
        assert faults[0].injected is True
        assert faults[0].parameters["latency_ms"] > 0

        # Artifacts survive the sandbox and are content-addressed (§41, §42).
        artifacts = list(
            (
                await db_session.execute(
                    select(ReproductionArtifact).where(
                        ReproductionArtifact.experiment_id == experiment.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert artifacts, "an experiment must leave an auditable artifact trail"
        assert all(item.content_hash for item in artifacts)
        kinds = {item.artifact_type.value for item in artifacts}
        assert kinds >= {
            "REPRODUCTION_PLAN",
            "REPRODUCTION_MANIFEST",
            "TELEMETRY_SNAPSHOT",
            "COMPARISON_RESULT",
            "FAULT_RECORD",
            "VALIDATION_REPORT",
        }, kinds
        # Artifacts must be verifiable against their recorded hash (§42).
        assert all(
            len(item.content_hash or "") == 64 for item in artifacts
        ), "every artifact needs a content hash"

        # The sandbox is destroyed and its working tree is gone (§55).
        sandbox = (
            await db_session.execute(
                select(ReproductionSandbox).where(
                    ReproductionSandbox.experiment_id == experiment.id
                )
            )
        ).scalar_one()
        assert sandbox.status is SandboxStatus.DESTROYED
        assert sandbox.destroyed_at is not None
        assert not Path(sandbox.root_path).exists(), "the sandbox workdir leaked"

        after = {item.name for item in sandbox_base.glob("argus-repro-*")}
        assert after == before, f"cleanup leaked sandboxes: {after - before}"

    async def test_reproduction_telemetry_never_enters_the_incident_namespace(
        self, db_session: AsyncSession, db_engine
    ) -> None:
        orchestrator, experiment, _candidate, _components, incident = await _prepare(
            db_session, db_engine
        )
        await orchestrator.run_experiment(experiment.id)
        await db_session.commit()
        refreshed = await db_session.get(ReproductionExperiment, experiment.id)
        assert refreshed is not None
        assert refreshed.telemetry_namespace == f"repro:{experiment.id}"
        assert str(incident.id) not in refreshed.telemetry_namespace


class TestCounterexampleScenario:
    """§60 — a change hypothesis is tested by running *without* the change."""

    async def test_a_deployment_hypothesis_runs_as_an_uninjected_baseline(
        self, db_session: AsyncSession, db_engine
    ) -> None:
        orchestrator, experiment, candidate, _components, _incident = await _prepare(
            db_session, db_engine, candidate_type=CandidateType.DEPLOYMENT
        )
        plan = experiment.metadata_["plan"]
        assert candidate.candidate_type is CandidateType.DEPLOYMENT
        # The honest experiment: nothing is injected, and the plan says why.
        assert plan["faults"] == []
        assert any("baseline" in reason for reason in plan["missing_inputs"]), plan[
            "missing_inputs"
        ]

        finished = await orchestrator.run_experiment(experiment.id)
        await db_session.commit()

        # The sandbox ran cleanly and the expected failure did not appear. That is
        # a *result*, not an error, and it must not be reported as SUPPORTED.
        assert finished.status is ExperimentStatus.COMPLETED
        assert finished.result is not ReproductionResult.SUCCESSFUL
        assert finished.result in {
            ReproductionResult.FAILED,
            ReproductionResult.INCONCLUSIVE,
        }

        validation = (
            await db_session.execute(
                select(ReproductionValidation).where(
                    ReproductionValidation.experiment_id == experiment.id
                )
            )
        ).scalar_one()
        assert validation.outcome in {
            ValidationOutcome.NOT_SUPPORTED,
            ValidationOutcome.INCONCLUSIVE,
        }
        # A null result must come with the reason it is null, never on its own.
        assert validation.summary
        if validation.outcome is ValidationOutcome.INCONCLUSIVE:
            assert (
                validation.environment_differences
                or validation.missing_inputs
                or (validation.reasoning)
            )

    async def test_an_unreproduced_hypothesis_still_produces_evidence(
        self, db_session: AsyncSession, db_engine
    ) -> None:
        orchestrator, experiment, _candidate, _components, _incident = await _prepare(
            db_session, db_engine, candidate_type=CandidateType.DEPLOYMENT
        )
        finished = await orchestrator.run_experiment(experiment.id)
        await db_session.commit()

        artifacts = list(
            (
                await db_session.execute(
                    select(ReproductionArtifact).where(
                        ReproductionArtifact.experiment_id == experiment.id
                    )
                )
            )
            .scalars()
            .all()
        )
        # Contradicting evidence is still evidence: the experiment is preserved.
        assert finished.summary
        assert artifacts
        # No fault was injected, and the audit must not claim one was.
        faults = list(
            (
                await db_session.execute(
                    select(ReproductionFault).where(
                        ReproductionFault.experiment_id == experiment.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert faults == []


class TestIntermittentScenario:
    """§61, §34 — a probabilistic fault, repeated, reported as an observation."""

    async def test_repeated_probabilistic_runs_report_what_happened(
        self, db_session: AsyncSession, db_engine
    ) -> None:
        orchestrator, experiment, _candidate, _components, _incident = await _prepare(
            db_session,
            db_engine,
            repetitions=4,
            fault_overrides=[
                {
                    "fault_type": FaultType.HTTP_5XX,
                    "target": "datastore",
                    "intensity": 0.25,
                }
            ],
        )
        plan = experiment.metadata_["plan"]
        assert plan["repetitions"] == 4
        assert plan["faults"][0]["intensity"] == 0.25

        finished = await orchestrator.run_experiment(experiment.id)
        await db_session.commit()
        # Every configured repetition advanced the counter, so progress is the
        # truth even for an experiment that is partly non-deterministic.
        assert finished.completed_runs == 4, finished.completed_runs

        runs = list(
            (
                await db_session.execute(
                    select(ReproductionRun)
                    .where(ReproductionRun.experiment_id == experiment.id)
                    .order_by(ReproductionRun.run_index)
                )
            )
            .scalars()
            .all()
        )
        # Every configured repetition actually ran, each in its own sandbox.
        assert [run.run_index for run in runs] == [1, 2, 3, 4]
        sandboxes = list(
            (
                await db_session.execute(
                    select(ReproductionSandbox).where(
                        ReproductionSandbox.experiment_id == experiment.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(sandboxes) == 4
        assert all(item.status is SandboxStatus.DESTROYED for item in sandboxes)
        assert all(not Path(item.root_path).exists() for item in sandboxes)

        validation = (
            await db_session.execute(
                select(ReproductionValidation).where(
                    ReproductionValidation.experiment_id == experiment.id
                )
            )
        ).scalar_one()
        determinism = validation.determinism
        assert determinism["runs"] == 4
        # The rate is arithmetically the runs that reproduced — never a
        # probability imported from somewhere else (§34).
        comparisons = list(
            (
                await db_session.execute(
                    select(ReproductionComparison).where(
                        ReproductionComparison.experiment_id == experiment.id
                    )
                )
            )
            .scalars()
            .all()
        )
        reproduced = sum(
            1
            for item in comparisons
            if item.result
            in {ReproductionResult.SUCCESSFUL, ReproductionResult.PARTIAL}
        )
        assert determinism["reproduction_rate"] == round(reproduced / 4, 4)
        assert determinism["classification"] in {
            "DETERMINISTIC",
            "INTERMITTENT",
            "NOT_REPRODUCED",
            "REPRODUCED",
            "NOT_RUN",
        }
        assert "not a probability" in determinism["note"]
        if final := validation.outcome:
            assert final in {
                ValidationOutcome.SUPPORTED,
                ValidationOutcome.PARTIALLY_SUPPORTED,
                ValidationOutcome.NOT_SUPPORTED,
                ValidationOutcome.INCONCLUSIVE,
            }
