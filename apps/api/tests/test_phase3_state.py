"""Phase 3 — lifecycle state machine tests (§8, §26).

These pin the *legal* transitions. Anything not asserted here as legal must be
rejected, so the API can never write an impossible state.
"""

from __future__ import annotations

import pytest

from app.models.anomaly import AnomalyStatus
from app.models.incident import IncidentStatus
from app.services import anomaly_state, incident_state


class TestIncidentTransitions:
    @pytest.mark.parametrize(
        "current,target",
        [
            ("OPEN", "ACKNOWLEDGED"),
            ("ACKNOWLEDGED", "INVESTIGATING"),
            ("INVESTIGATING", "MITIGATED"),
            ("MITIGATED", "RESOLVED"),
            ("RESOLVED", "CLOSED"),
            ("OPEN", "RESOLVED"),
            ("MITIGATED", "INVESTIGATING"),
            ("RESOLVED", "OPEN"),
            ("CLOSED", "OPEN"),
        ],
    )
    def test_legal(self, current: str, target: str) -> None:
        assert incident_state.can_transition(current, target) is True
        assert incident_state.assert_transition(current, target) == IncidentStatus(
            target
        )

    @pytest.mark.parametrize(
        "current,target",
        [
            ("OPEN", "OPEN"),  # same status: legal no-op
            ("CLOSED", "MITIGATED"),
            ("ACKNOWLEDGED", "OPEN"),
            ("INVESTIGATING", "ACKNOWLEDGED"),
        ],
    )
    def test_same_status_is_noop(self, current: str, target: str) -> None:
        """Only the identical-status case is legal among these."""
        if current == target:
            assert incident_state.can_transition(current, target) is True
        else:
            assert incident_state.can_transition(current, target) is False

    @pytest.mark.parametrize(
        "current,target",
        [
            ("CLOSED", "MITIGATED"),
            ("ACKNOWLEDGED", "OPEN"),
            ("INVESTIGATING", "ACKNOWLEDGED"),
            ("RESOLVED", "MITIGATED"),
        ],
    )
    def test_illegal_raises(self, current: str, target: str) -> None:
        with pytest.raises(incident_state.InvalidIncidentTransition):
            incident_state.assert_transition(current, target)

    def test_unknown_status_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown incident status"):
            incident_state.can_transition("NOT_A_STATUS", "OPEN")

    def test_allowed_transitions_used_by_ui(self) -> None:
        allowed = incident_state.transitions_as_values("OPEN")
        assert "ACKNOWLEDGED" in allowed
        assert "CLOSED" in allowed
        assert "OPEN" not in allowed  # self is never offered by the UI

    def test_timestamp_fields(self) -> None:
        assert incident_state.timestamp_field_for("ACKNOWLEDGED") == "acknowledged_at"
        assert incident_state.timestamp_field_for("RESOLVED") == "resolved_at"
        assert incident_state.timestamp_field_for("OPEN") is None

    def test_no_status_is_unreachable_trap(self) -> None:
        """Every status must be reachable from OPEN."""
        reachable: set[str] = set()
        frontier = ["OPEN"]
        while frontier:
            current = frontier.pop()
            for target in incident_state.transitions_as_values(current):
                if target not in reachable:
                    reachable.add(target)
                    frontier.append(target)
        assert reachable == {s.value for s in IncidentStatus}


class TestAnomalyTransitions:
    @pytest.mark.parametrize(
        "current,target",
        [
            ("DETECTED", "ACKNOWLEDGED"),
            ("ACKNOWLEDGED", "INVESTIGATING"),
            ("INVESTIGATING", "RESOLVED"),
            ("DETECTED", "EXPIRED"),
            ("RESOLVED", "DETECTED"),  # reopening
        ],
    )
    def test_legal(self, current: str, target: str) -> None:
        assert anomaly_state.can_transition(current, target) is True
        assert anomaly_state.assert_transition(current, target) == AnomalyStatus(target)

    def test_expired_is_terminal(self) -> None:
        assert anomaly_state.is_terminal("EXPIRED") is True
        assert anomaly_state.allowed_transitions("EXPIRED") == frozenset()
        assert anomaly_state.can_transition("EXPIRED", "DETECTED") is False

    def test_illegal_raises(self) -> None:
        with pytest.raises(anomaly_state.InvalidAnomalyTransition):
            anomaly_state.assert_transition("INVESTIGATING", "ACKNOWLEDGED")

    def test_unknown_status_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown anomaly status"):
            anomaly_state.can_transition("NOPE", "DETECTED")

    def test_timestamp_fields(self) -> None:
        assert anomaly_state.timestamp_field_for("ACKNOWLEDGED") == "acknowledged_at"
        assert anomaly_state.timestamp_field_for("DETECTED") is None

    def test_all_statuses_reachable_from_detected(self) -> None:
        reachable: set[str] = set()
        frontier = ["DETECTED"]
        while frontier:
            current = frontier.pop()
            for target in anomaly_state.transitions_as_values(current):
                if target not in reachable:
                    reachable.add(target)
                    frontier.append(target)
        assert reachable == {s.value for s in AnomalyStatus}
