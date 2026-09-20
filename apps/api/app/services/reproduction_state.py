"""ARGUS Reproduction State Machine (Phase 5 §37, §38, §39).

One source of truth for which experiment/run/fault status changes are legal,
shared by the API, the orchestrator, the workers, and the reaper. The UI can
therefore only ever offer transitions the backend will accept.

Three machines live here because three lifecycles have genuinely different
rules:

* **experiment** — the happy path is strictly ordered (you cannot compare before
  you have collected telemetry), and failure exits are terminal. ``CANCELLED``
  and ``TIMED_OUT`` are *not* failures of the run: they are controlled stops
  that must still reach cleanup.
* **run** — one repetition; terminal on completion.
* **fault** — ``PLANNED → ACTIVE → COMPLETED``, where ``SKIPPED`` is the honest
  outcome when a fault could never be injected.

Every transition is idempotent: setting the current status again is a no-op
rather than an error, so a retried worker job cannot fail on bookkeeping.
"""

from __future__ import annotations

from typing import Iterable

from app.models.reproduction import (
    ExperimentStatus,
    FaultStatus,
    RunStatus,
)

#: Legal target statuses for each experiment status.
EXPERIMENT_TRANSITIONS: dict[ExperimentStatus, frozenset[ExperimentStatus]] = {
    ExperimentStatus.PLANNED: frozenset(
        {
            ExperimentStatus.VALIDATING,
            ExperimentStatus.CANCELLED,
            ExperimentStatus.FAILED,
        }
    ),
    ExperimentStatus.VALIDATING: frozenset(
        {
            ExperimentStatus.PROVISIONING,
            # The closing phase is reachable from every non-terminal state: a
            # timeout, a cancellation, or a failed run must still be able to
            # compare what was collected and record a verdict rather than
            # leaving the experiment dangling.
            ExperimentStatus.COMPARING,
            ExperimentStatus.FAILED,
            ExperimentStatus.CANCELLED,
        }
    ),
    ExperimentStatus.PROVISIONING: frozenset(
        {
            ExperimentStatus.READY,
            ExperimentStatus.COMPARING,
            ExperimentStatus.FAILED,
            ExperimentStatus.TIMED_OUT,
            ExperimentStatus.CANCELLED,
        }
    ),
    ExperimentStatus.READY: frozenset(
        {
            ExperimentStatus.REPLAYING,
            ExperimentStatus.COMPARING,
            ExperimentStatus.FAILED,
            ExperimentStatus.TIMED_OUT,
            ExperimentStatus.CANCELLED,
        }
    ),
    ExperimentStatus.REPLAYING: frozenset(
        {
            ExperimentStatus.RUNNING,
            ExperimentStatus.COLLECTING,
            ExperimentStatus.COMPARING,
            ExperimentStatus.FAILED,
            ExperimentStatus.TIMED_OUT,
            ExperimentStatus.CANCELLED,
        }
    ),
    ExperimentStatus.RUNNING: frozenset(
        {
            ExperimentStatus.COLLECTING,
            ExperimentStatus.COMPARING,
            ExperimentStatus.FAILED,
            ExperimentStatus.TIMED_OUT,
            ExperimentStatus.CANCELLED,
        }
    ),
    ExperimentStatus.COLLECTING: frozenset(
        {
            # Repeatability (§34): a new repetition provisions a *fresh*
            # sandbox and replays again — a legal loop inside one experiment,
            # not a second experiment. A fresh sandbox per repetition is what
            # keeps runs independent; reusing one would let the first run's
            # state change the second run's result.
            ExperimentStatus.PROVISIONING,
            ExperimentStatus.COMPARING,
            ExperimentStatus.FAILED,
            ExperimentStatus.TIMED_OUT,
            ExperimentStatus.CANCELLED,
        }
    ),
    ExperimentStatus.COMPARING: frozenset(
        {
            ExperimentStatus.COMPLETED,
            ExperimentStatus.FAILED,
            ExperimentStatus.TIMED_OUT,
            ExperimentStatus.CANCELLED,
        }
    ),
    # Terminal: an experiment is a historical record once finished.
    ExperimentStatus.COMPLETED: frozenset(),
    ExperimentStatus.FAILED: frozenset(),
    ExperimentStatus.CANCELLED: frozenset(),
    ExperimentStatus.TIMED_OUT: frozenset(),
}

#: Statuses that carry a lifecycle timestamp when entered.
EXPERIMENT_TIMESTAMP_FIELDS: dict[ExperimentStatus, str] = {
    ExperimentStatus.VALIDATING: "started_at",
    ExperimentStatus.COMPLETED: "completed_at",
    ExperimentStatus.FAILED: "completed_at",
    ExperimentStatus.CANCELLED: "completed_at",
    ExperimentStatus.TIMED_OUT: "completed_at",
}

#: The happy path, exposed for the UI progress bar.
EXPERIMENT_HAPPY_PATH: tuple[ExperimentStatus, ...] = (
    ExperimentStatus.PLANNED,
    ExperimentStatus.VALIDATING,
    ExperimentStatus.PROVISIONING,
    ExperimentStatus.READY,
    ExperimentStatus.REPLAYING,
    ExperimentStatus.RUNNING,
    ExperimentStatus.COLLECTING,
    ExperimentStatus.COMPARING,
    ExperimentStatus.COMPLETED,
)

#: Terminal experiment statuses — no further transition is possible.
TERMINAL_EXPERIMENT_STATUSES = frozenset(
    {
        ExperimentStatus.COMPLETED,
        ExperimentStatus.FAILED,
        ExperimentStatus.CANCELLED,
        ExperimentStatus.TIMED_OUT,
    }
)

#: Statuses after which a sandbox must already be destroyed (§55). Cleanup runs
#: before these are entered, so an experiment in one of them that still owns a
#: live sandbox is an anomaly the reaper reports.
CLEANUP_REQUIRED_STATUSES = TERMINAL_EXPERIMENT_STATUSES

RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED}
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.TIMED_OUT,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.TIMED_OUT: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}

FAULT_TRANSITIONS: dict[FaultStatus, frozenset[FaultStatus]] = {
    FaultStatus.PLANNED: frozenset(
        {FaultStatus.ACTIVE, FaultStatus.SKIPPED, FaultStatus.FAILED}
    ),
    FaultStatus.ACTIVE: frozenset({FaultStatus.COMPLETED, FaultStatus.FAILED}),
    FaultStatus.COMPLETED: frozenset(),
    FaultStatus.FAILED: frozenset(),
    FaultStatus.SKIPPED: frozenset(),
}


class InvalidReproductionTransition(ValueError):
    """Raised when an illegal experiment/run/fault status transition is tried."""

    def __init__(self, entity: str, current: object, target: object) -> None:
        self.entity = entity
        self.current = current
        self.target = target
        super().__init__(
            f"Illegal {entity} transition {current} -> {target}"  # type: ignore[str-bytes-safe]
        )


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------
def _coerce_experiment(status: ExperimentStatus | str) -> ExperimentStatus:
    try:
        return ExperimentStatus(status)
    except ValueError as e:
        raise ValueError(f"Unknown experiment status: {status!r}") from e


def allowed_experiment_transitions(
    current: ExperimentStatus | str,
) -> frozenset[ExperimentStatus]:
    """Legal target statuses from ``current`` (excluding itself)."""
    return EXPERIMENT_TRANSITIONS.get(_coerce_experiment(current), frozenset())


def can_transition_experiment(
    current: ExperimentStatus | str, target: ExperimentStatus | str
) -> bool:
    """True when the transition is legal (same-status is a legal no-op)."""
    current_enum = _coerce_experiment(current)
    target_enum = _coerce_experiment(target)
    if current_enum is target_enum:
        return True
    return target_enum in EXPERIMENT_TRANSITIONS.get(current_enum, frozenset())


def assert_experiment_transition(
    current: ExperimentStatus | str, target: ExperimentStatus | str
) -> ExperimentStatus:
    """Validate a transition, raising :class:`InvalidReproductionTransition`."""
    if not can_transition_experiment(current, target):
        raise InvalidReproductionTransition("experiment", current, target)
    return _coerce_experiment(target)


def is_experiment_terminal(status: ExperimentStatus | str) -> bool:
    return _coerce_experiment(status) in TERMINAL_EXPERIMENT_STATUSES


def experiment_timestamp_field(status: ExperimentStatus | str) -> str | None:
    """Which lifecycle timestamp a status change should stamp, if any."""
    return EXPERIMENT_TIMESTAMP_FIELDS.get(_coerce_experiment(status))


def experiment_transitions_as_values(current: ExperimentStatus | str) -> Iterable[str]:
    """Human/UI-friendly allowed transitions (sorted, excluding self)."""
    return sorted(s.value for s in allowed_experiment_transitions(current))


# ---------------------------------------------------------------------------
# Run + fault (same shape, different enum)
# ---------------------------------------------------------------------------
def can_transition_run(current: RunStatus | str, target: RunStatus | str) -> bool:
    current_enum = RunStatus(current)
    target_enum = RunStatus(target)
    if current_enum is target_enum:
        return True
    return target_enum in RUN_TRANSITIONS.get(current_enum, frozenset())


def assert_run_transition(
    current: RunStatus | str, target: RunStatus | str
) -> RunStatus:
    if not can_transition_run(current, target):
        raise InvalidReproductionTransition("run", current, target)
    return RunStatus(target)


def can_transition_fault(current: FaultStatus | str, target: FaultStatus | str) -> bool:
    current_enum = FaultStatus(current)
    target_enum = FaultStatus(target)
    if current_enum is target_enum:
        return True
    return target_enum in FAULT_TRANSITIONS.get(current_enum, frozenset())


def assert_fault_transition(
    current: FaultStatus | str, target: FaultStatus | str
) -> FaultStatus:
    if not can_transition_fault(current, target):
        raise InvalidReproductionTransition("fault", current, target)
    return FaultStatus(target)


__all__ = [
    "EXPERIMENT_TRANSITIONS",
    "EXPERIMENT_TIMESTAMP_FIELDS",
    "EXPERIMENT_HAPPY_PATH",
    "TERMINAL_EXPERIMENT_STATUSES",
    "CLEANUP_REQUIRED_STATUSES",
    "RUN_TRANSITIONS",
    "FAULT_TRANSITIONS",
    "InvalidReproductionTransition",
    "allowed_experiment_transitions",
    "can_transition_experiment",
    "assert_experiment_transition",
    "is_experiment_terminal",
    "experiment_timestamp_field",
    "experiment_transitions_as_values",
    "can_transition_run",
    "assert_run_transition",
    "can_transition_fault",
    "assert_fault_transition",
]
