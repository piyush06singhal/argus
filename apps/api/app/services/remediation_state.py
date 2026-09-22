"""ARGUS Remediation State Machine (Phase 9 §5).

One source of truth for which remediation status changes are legal. The API,
the executor, the verifier, the rollback engine and the sweeper all route
through :func:`transition` rather than assigning ``action.status`` directly, so
"never skip state transitions silently" is enforced by construction rather than
by review.

Three deliberate properties:

* **Illegal transitions raise.** ``assert_transition`` raises
  :class:`IllegalTransition`, which is a programming error, not a user error: a
  caller attempting one has a bug, and swallowing it would put a status in the
  database that no gate actually produced.
* **Re-entering the current status is a no-op.** Every mutating helper is
  idempotent, because the sweeper and a queued job can both legitimately reach
  the same step.
* **Terminal means terminal — with two exceptions that exist for real reasons.**
  ``BLOCKED`` is *not* terminal (an operator may configure the missing adapter,
  or release an emergency stop, and the action should be re-evaluable), and
  ``FAILED`` is not terminal (bounded retry, or rollback). Nothing revives
  ``REJECTED``, ``CANCELLED`` or ``EXPIRED``: a rejected remediation must be
  re-proposed, which is what makes the rejection meaningful.

State names are the §5 lifecycle. ``AUTHORIZED → AWAITING_APPROVAL`` is legal
only in one direction of meaning: it is how a dry run or a shadow pass returns
*itself* to the queue while its real work is still unexecuted. Nothing else can
use it, and the executor is the only caller.
"""

from __future__ import annotations

from typing import Iterable, Optional

from app.models.remediation import RemediationStatus


class IllegalTransition(RuntimeError):
    """A status change the state machine does not permit."""

    def __init__(self, current: RemediationStatus, target: RemediationStatus) -> None:
        super().__init__(
            f"illegal remediation transition {current.value} -> {target.value}"
        )
        self.current = current
        self.target = target


#: Legal target statuses for each current status.
REMEDIATION_TRANSITIONS: dict[RemediationStatus, frozenset[RemediationStatus]] = {
    # Proposed: nothing has been checked yet.
    RemediationStatus.PROPOSED: frozenset(
        {
            RemediationStatus.VALIDATING,
            RemediationStatus.REJECTED,
            RemediationStatus.CANCELLED,
            RemediationStatus.EXPIRED,
        }
    ),
    # Validation: parameters, preconditions and the registry consult.
    RemediationStatus.VALIDATING: frozenset(
        {
            RemediationStatus.VALIDATING,
            RemediationStatus.POLICY_REVIEW,
            RemediationStatus.REJECTED,
            RemediationStatus.BLOCKED,
            RemediationStatus.EXPIRED,
            RemediationStatus.CANCELLED,
        }
    ),
    # Policy review: safety + policy have their say.
    RemediationStatus.POLICY_REVIEW: frozenset(
        {
            RemediationStatus.POLICY_REVIEW,
            RemediationStatus.AWAITING_APPROVAL,
            RemediationStatus.AUTHORIZED,
            RemediationStatus.REJECTED,
            RemediationStatus.BLOCKED,
            RemediationStatus.EXPIRED,
            RemediationStatus.CANCELLED,
        }
    ),
    # Awaiting approval: a human, or a policy rule standing in for one.
    RemediationStatus.AWAITING_APPROVAL: frozenset(
        {
            RemediationStatus.AUTHORIZED,
            RemediationStatus.REJECTED,
            RemediationStatus.EXPIRED,
            RemediationStatus.BLOCKED,
            RemediationStatus.CANCELLED,
        }
    ),
    # Authorized: cleared to run, not yet running.
    RemediationStatus.AUTHORIZED: frozenset(
        {
            RemediationStatus.SCHEDULED,
            RemediationStatus.EXECUTING,
            RemediationStatus.AWAITING_APPROVAL,
            RemediationStatus.BLOCKED,
            RemediationStatus.EXPIRED,
            RemediationStatus.CANCELLED,
        }
    ),
    # Scheduled: queued for a worker. Distinct from AUTHORIZED so a crash
    # between authorization and execution is visible rather than invisible.
    RemediationStatus.SCHEDULED: frozenset(
        {
            RemediationStatus.EXECUTING,
            RemediationStatus.AUTHORIZED,
            RemediationStatus.BLOCKED,
            RemediationStatus.EXPIRED,
            RemediationStatus.CANCELLED,
        }
    ),
    # Executing: an attempt is in flight, or just finished, for this action.
    RemediationStatus.EXECUTING: frozenset(
        {
            RemediationStatus.VERIFYING,
            RemediationStatus.FAILED,
            RemediationStatus.ROLLING_BACK,
            # Refusal before any effect (no adapter, breaker open, kill switch).
            RemediationStatus.BLOCKED,
            # A dry run or shadow pass returns to the queue unexecuted.
            RemediationStatus.AWAITING_APPROVAL,
            # A retry goes back to SCHEDULED explicitly, incrementing attempt.
            RemediationStatus.SCHEDULED,
            RemediationStatus.EXPIRED,
        }
    ),
    # Verifying: the effect was applied; now observe whether it helped.
    RemediationStatus.VERIFYING: frozenset(
        {
            RemediationStatus.VERIFYING,
            RemediationStatus.VERIFIED,
            RemediationStatus.FAILED,
            RemediationStatus.ROLLING_BACK,
            RemediationStatus.BLOCKED,
        }
    ),
    # Verified: ran, and the system behaved as expected.
    RemediationStatus.VERIFIED: frozenset({RemediationStatus.ROLLING_BACK}),
    # Failed: ran badly, or could not be verified. Either roll back or retry.
    RemediationStatus.FAILED: frozenset(
        {
            RemediationStatus.ROLLING_BACK,
            RemediationStatus.SCHEDULED,
            RemediationStatus.ROLLED_BACK,
            RemediationStatus.BLOCKED,
        }
    ),
    # Rolling back: reverting, and the revert is itself verified.
    RemediationStatus.ROLLING_BACK: frozenset(
        {RemediationStatus.ROLLED_BACK, RemediationStatus.FAILED}
    ),
    # Terminal states.
    RemediationStatus.ROLLED_BACK: frozenset(),
    RemediationStatus.REJECTED: frozenset(),
    RemediationStatus.CANCELLED: frozenset(),
    RemediationStatus.EXPIRED: frozenset(),
    # Blocked is deliberately recoverable: the blocker may be removed.
    # Blocked: the platform refused it, but a person may still act.
    RemediationStatus.BLOCKED: frozenset(
        {
            # Re-evaluable: an operator can fix the cause (configure an adapter,
            # release an emergency stop, correct a parameter) and try again.
            RemediationStatus.POLICY_REVIEW,
            RemediationStatus.VALIDATING,
            RemediationStatus.CANCELLED,
            RemediationStatus.EXPIRED,
            # The manual-execution path (§26): ARGUS refused to perform the
            # action, a human performed it outside the platform, and the record
            # of that goes straight to verification. This is the one edge that
            # lets a person override a refusal — it is deliberately not
            # ``EXECUTING``, because ARGUS itself may never apply an effect from
            # a blocked action (``may_apply_effect`` still excludes it), and the
            # claim is verified from telemetry rather than trusted.
            RemediationStatus.VERIFYING,
        }
    ),
}

#: Statuses from which nothing further can happen.
TERMINAL_STATUSES: frozenset[RemediationStatus] = frozenset(
    status for status, targets in REMEDIATION_TRANSITIONS.items() if not targets
)

#: Statuses that represent work in flight — at most one action per scope may be
#: here at a time (concurrency guard, §25).
IN_FLIGHT_STATUSES: frozenset[RemediationStatus] = frozenset(
    {
        RemediationStatus.SCHEDULED,
        RemediationStatus.EXECUTING,
        RemediationStatus.VERIFYING,
        RemediationStatus.ROLLING_BACK,
    }
)

#: Statuses where an action is still "live" (not finished, not refused).
OPEN_STATUSES: frozenset[RemediationStatus] = frozenset(
    status for status in RemediationStatus if status not in TERMINAL_STATUSES
)

#: The timestamp column to set when a status is entered (§2).
STATUS_TIMESTAMP_FIELDS: dict[RemediationStatus, str] = {
    RemediationStatus.AUTHORIZED: "authorized_at",
    RemediationStatus.EXECUTING: "started_at",
    RemediationStatus.VERIFIED: "completed_at",
    RemediationStatus.FAILED: "completed_at",
    RemediationStatus.ROLLED_BACK: "rollback_performed_at",
}


def can_transition(current: RemediationStatus, target: RemediationStatus) -> bool:
    """Whether ``current -> target`` is legal (idempotent changes allowed)."""
    if current == target:
        return True
    return target in REMEDIATION_TRANSITIONS.get(current, frozenset())


def allowed_targets(current: RemediationStatus) -> frozenset[RemediationStatus]:
    """Every legal target from ``current``, including itself."""
    return REMEDIATION_TRANSITIONS.get(current, frozenset()) | {current}


def assert_transition(current: RemediationStatus, target: RemediationStatus) -> None:
    """Raise :class:`IllegalTransition` unless the change is legal."""
    if not can_transition(current, target):
        raise IllegalTransition(current, target)


def apply_transition(action, target: RemediationStatus) -> bool:
    """Move an action to ``target`` if legal, stamping its lifecycle timestamp.

    Returns ``True`` when the status actually changed. Re-entering the current
    status is a no-op and returns ``False``, which is what lets the sweeper and
    a worker job both call this without racing into a duplicate audit entry.
    """
    current = action.status
    if current == target:
        return False
    assert_transition(current, target)
    action.status = target
    field_name = STATUS_TIMESTAMP_FIELDS.get(target)
    if field_name and getattr(action, field_name, None) is None:
        from app.services.remediation_clock import utcnow

        setattr(action, field_name, utcnow())
    return True


def is_terminal(status: RemediationStatus) -> bool:
    """Whether the action is finished for good."""
    return status in TERMINAL_STATUSES


def is_in_flight(status: RemediationStatus) -> bool:
    """Whether the action currently occupies an execution slot."""
    return status in IN_FLIGHT_STATUSES


def may_apply_effect(status: RemediationStatus) -> bool:
    """Whether a live effect may be attempted from this status.

    Only ``AUTHORIZED`` and ``SCHEDULED`` qualify. Notably ``AWAITING_APPROVAL``
    does not: an approval that has not been recorded cannot be inferred from the
    fact that a policy allowed the action earlier.
    """
    return status in (RemediationStatus.AUTHORIZED, RemediationStatus.SCHEDULED)


def describe_path(
    current: RemediationStatus, target: RemediationStatus
) -> Optional[list[RemediationStatus]]:
    """Shortest legal path from ``current`` to ``target``, or ``None``.

    Used by the UI to explain *why* a transition is unavailable instead of
    merely hiding the button, and by tests to prove reachability.
    """
    if current == target:
        return [current]
    frontier: list[list[RemediationStatus]] = [[current]]
    seen: set[RemediationStatus] = {current}
    while frontier:
        path = frontier.pop(0)
        for nxt in REMEDIATION_TRANSITIONS.get(path[-1], frozenset()):
            if nxt in seen:
                continue
            new_path = [*path, nxt]
            if nxt == target:
                return new_path
            seen.add(nxt)
            frontier.append(new_path)
    return None


def iter_transitions() -> Iterable[tuple[RemediationStatus, RemediationStatus]]:
    """Every legal ``(from, to)`` pair, for documentation and tests."""
    for current, targets in REMEDIATION_TRANSITIONS.items():
        for target in sorted(targets, key=lambda s: s.value):
            yield current, target


__all__ = [
    "IllegalTransition",
    "IN_FLIGHT_STATUSES",
    "OPEN_STATUSES",
    "REMEDIATION_TRANSITIONS",
    "STATUS_TIMESTAMP_FIELDS",
    "TERMINAL_STATUSES",
    "allowed_targets",
    "apply_transition",
    "assert_transition",
    "can_transition",
    "describe_path",
    "is_in_flight",
    "is_terminal",
    "iter_transitions",
    "may_apply_effect",
]
