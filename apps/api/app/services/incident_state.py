"""ARGUS Incident State Machine (Phase 3 §26).

One source of truth for which incident status changes are legal, shared by the
API and the incident manager. The UI can therefore only ever offer transitions
the backend accepts — the alternative (a permissive enum plus scattered
validation) is how invalid states end up in production data.

Handling and closure are distinct: ``MITIGATED`` means the impact stopped,
``RESOLVED`` means it is over, ``CLOSED`` means the investigation is done.
``RESOLVED`` and ``CLOSED`` may be **reopened** (a regression is a real thing),
and every transition is idempotent — setting the current status again is a
no-op rather than an error.
"""

from __future__ import annotations

from typing import Iterable

from app.models.incident import IncidentStatus

#: Legal target statuses for each current status.
INCIDENT_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.OPEN: frozenset(
        {
            IncidentStatus.ACKNOWLEDGED,
            IncidentStatus.INVESTIGATING,
            IncidentStatus.MITIGATED,
            IncidentStatus.RESOLVED,
            IncidentStatus.CLOSED,
        }
    ),
    IncidentStatus.ACKNOWLEDGED: frozenset(
        {
            IncidentStatus.INVESTIGATING,
            IncidentStatus.MITIGATED,
            IncidentStatus.RESOLVED,
            IncidentStatus.CLOSED,
        }
    ),
    IncidentStatus.INVESTIGATING: frozenset(
        {
            IncidentStatus.MITIGATED,
            IncidentStatus.RESOLVED,
            IncidentStatus.CLOSED,
        }
    ),
    IncidentStatus.MITIGATED: frozenset(
        {
            IncidentStatus.INVESTIGATING,
            IncidentStatus.RESOLVED,
            IncidentStatus.CLOSED,
        }
    ),
    # Reopening a resolved/closed incident is supported explicitly.
    IncidentStatus.RESOLVED: frozenset({IncidentStatus.CLOSED, IncidentStatus.OPEN}),
    IncidentStatus.CLOSED: frozenset({IncidentStatus.OPEN}),
}

#: Statuses that carry a lifecycle timestamp when entered.
INCIDENT_TIMESTAMP_FIELDS: dict[IncidentStatus, str] = {
    IncidentStatus.ACKNOWLEDGED: "acknowledged_at",
    IncidentStatus.RESOLVED: "resolved_at",
    IncidentStatus.CLOSED: "resolved_at",
}


class InvalidIncidentTransition(ValueError):
    """Raised when an illegal incident status transition is attempted."""

    def __init__(self, current: IncidentStatus, target: IncidentStatus) -> None:
        self.current = current
        self.target = target
        allowed = ", ".join(sorted(s.value for s in allowed_transitions(current)))
        super().__init__(
            f"Illegal incident transition {current.value} -> {target.value}. "
            f"Allowed from {current.value}: {allowed or 'none'}"
        )


def _coerce(status: IncidentStatus | str) -> IncidentStatus:
    try:
        return IncidentStatus(status)
    except ValueError as e:
        raise ValueError(f"Unknown incident status: {status!r}") from e


def allowed_transitions(current: IncidentStatus | str) -> frozenset[IncidentStatus]:
    """Return the legal target statuses from ``current`` (excluding itself)."""
    return INCIDENT_TRANSITIONS.get(_coerce(current), frozenset())


def can_transition(current: IncidentStatus | str, target: IncidentStatus | str) -> bool:
    """True when the transition is legal (same-status is a legal no-op)."""
    current_enum = _coerce(current)
    target_enum = _coerce(target)
    if current_enum is target_enum:
        return True
    return target_enum in INCIDENT_TRANSITIONS.get(current_enum, frozenset())


def assert_transition(
    current: IncidentStatus | str, target: IncidentStatus | str
) -> IncidentStatus:
    """Validate a transition, raising :class:`InvalidIncidentTransition`."""
    if not can_transition(current, target):
        raise InvalidIncidentTransition(_coerce(current), _coerce(target))
    return _coerce(target)


def is_terminal(status: IncidentStatus | str) -> bool:
    """True when a status has no outgoing transitions (none are terminal today,
    because CLOSED can be reopened — kept for callers that need the concept)."""
    return not INCIDENT_TRANSITIONS.get(_coerce(status), frozenset())


def timestamp_field_for(status: IncidentStatus | str) -> str | None:
    """Which lifecycle timestamp a status change should stamp, if any."""
    return INCIDENT_TIMESTAMP_FIELDS.get(_coerce(status))


def transitions_as_values(
    current: IncidentStatus | str,
) -> Iterable[str]:
    """Human/UI-friendly allowed transitions (sorted values, excluding self)."""
    return sorted(s.value for s in allowed_transitions(current))


__all__ = [
    "INCIDENT_TRANSITIONS",
    "INCIDENT_TIMESTAMP_FIELDS",
    "InvalidIncidentTransition",
    "allowed_transitions",
    "can_transition",
    "assert_transition",
    "is_terminal",
    "timestamp_field_for",
    "transitions_as_values",
]
