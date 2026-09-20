"""ARGUS Anomaly State Machine (Phase 3 §8).

Mirrors the incident state machine: one source of truth for legal anomaly
status changes, shared by the API, the rule engine, and the expiry sweep.

A resolved anomaly may be **reopened** to ``DETECTED`` when the same condition
recurs inside its fingerprint; ``EXPIRED`` is terminal because expiry means the
evidence window closed, and a later recurrence is a genuinely new anomaly.
"""

from __future__ import annotations

from typing import Iterable

from app.models.anomaly import AnomalyStatus

#: Legal target statuses for each current status.
ANOMALY_TRANSITIONS: dict[AnomalyStatus, frozenset[AnomalyStatus]] = {
    AnomalyStatus.DETECTED: frozenset(
        {
            AnomalyStatus.ACKNOWLEDGED,
            AnomalyStatus.INVESTIGATING,
            AnomalyStatus.RESOLVED,
            AnomalyStatus.EXPIRED,
        }
    ),
    AnomalyStatus.ACKNOWLEDGED: frozenset(
        {
            AnomalyStatus.INVESTIGATING,
            AnomalyStatus.RESOLVED,
            AnomalyStatus.EXPIRED,
        }
    ),
    AnomalyStatus.INVESTIGATING: frozenset(
        {AnomalyStatus.RESOLVED, AnomalyStatus.EXPIRED}
    ),
    # Recurrence reopens a resolved anomaly rather than creating noise.
    AnomalyStatus.RESOLVED: frozenset({AnomalyStatus.DETECTED, AnomalyStatus.EXPIRED}),
    # Expiry is terminal: the evidence window has closed.
    AnomalyStatus.EXPIRED: frozenset(),
}

#: Statuses that carry a lifecycle timestamp when entered.
ANOMALY_TIMESTAMP_FIELDS: dict[AnomalyStatus, str] = {
    AnomalyStatus.ACKNOWLEDGED: "acknowledged_at",
    AnomalyStatus.RESOLVED: "resolved_at",
}


class InvalidAnomalyTransition(ValueError):
    """Raised when an illegal anomaly status transition is attempted."""

    def __init__(self, current: AnomalyStatus, target: AnomalyStatus) -> None:
        self.current = current
        self.target = target
        allowed = ", ".join(sorted(s.value for s in allowed_transitions(current)))
        super().__init__(
            f"Illegal anomaly transition {current.value} -> {target.value}. "
            f"Allowed from {current.value}: {allowed or 'none'}"
        )


def _coerce(status: AnomalyStatus | str) -> AnomalyStatus:
    try:
        return AnomalyStatus(status)
    except ValueError as e:
        raise ValueError(f"Unknown anomaly status: {status!r}") from e


def allowed_transitions(current: AnomalyStatus | str) -> frozenset[AnomalyStatus]:
    """Return the legal target statuses from ``current`` (excluding itself)."""
    return ANOMALY_TRANSITIONS.get(_coerce(current), frozenset())


def can_transition(current: AnomalyStatus | str, target: AnomalyStatus | str) -> bool:
    """True when the transition is legal (same-status is a legal no-op)."""
    current_enum = _coerce(current)
    target_enum = _coerce(target)
    if current_enum is target_enum:
        return True
    return target_enum in ANOMALY_TRANSITIONS.get(current_enum, frozenset())


def assert_transition(
    current: AnomalyStatus | str, target: AnomalyStatus | str
) -> AnomalyStatus:
    """Validate a transition, raising :class:`InvalidAnomalyTransition`."""
    if not can_transition(current, target):
        raise InvalidAnomalyTransition(_coerce(current), _coerce(target))
    return _coerce(target)


def is_terminal(status: AnomalyStatus | str) -> bool:
    """True when a status has no outgoing transitions (``EXPIRED``)."""
    return not ANOMALY_TRANSITIONS.get(_coerce(status), frozenset())


def timestamp_field_for(status: AnomalyStatus | str) -> str | None:
    """Which lifecycle timestamp a status change should stamp, if any."""
    return ANOMALY_TIMESTAMP_FIELDS.get(_coerce(status))


def transitions_as_values(current: AnomalyStatus | str) -> Iterable[str]:
    """Human/UI-friendly allowed transitions (sorted values, excluding self)."""
    return sorted(s.value for s in allowed_transitions(current))


__all__ = [
    "ANOMALY_TRANSITIONS",
    "ANOMALY_TIMESTAMP_FIELDS",
    "InvalidAnomalyTransition",
    "allowed_transitions",
    "can_transition",
    "assert_transition",
    "is_terminal",
    "timestamp_field_for",
    "transitions_as_values",
]
