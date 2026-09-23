"""Phase 11 — unified system state (§2–§5, §87, §88).

The state machine is the phase's foundation, so these tests pin it where it can
actually be wrong: the **precedence**. Every other Phase 11 feature renders this
state, and a precedence bug would show a component with an open incident as
healthy — the contradiction §5 says ARGUS must detect rather than display.

The precedence is tested as a table of signal combinations rather than one
scenario per assertion, because the interesting cases are the *overlaps*: an
incident and a high forecast, a mild anomaly and no telemetry. Those are the ones
a hand-written if-chain gets wrong.

Two of these tests are worth more than the rest. ``test_no_telemetry_is_unknown``
pins silence as UNKNOWN rather than HEALTHY, and
``test_a_medium_anomaly_is_at_risk_not_degraded`` pins the severity split — both
are the kind of shortcut that would look right in a dashboard and be wrong in an
incident review.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.anomaly import AnomalySeverity
from app.models.incident import IncidentStatus
from app.models.platform import ComponentOperationalState, StateTransitionTrigger
from app.models.reliability import ForecastRiskLevel
from app.services.system_state import (
    ComponentSignals,
    derive_component_states,
    derive_state,
    record_transitions,
    state_at,
    state_history,
)
from tests.phase11_helpers import episode, hours_ago, minutes_ago, utcnow

pytestmark = pytest.mark.asyncio

RECOVERY_WINDOW = timedelta(minutes=15)
TELEMETRY_WINDOW = timedelta(minutes=10)


def _state(signals: ComponentSignals, *, component_id=None, now=None):
    return derive_state(
        component_id or uuid.uuid4(),
        signals,
        now=now or utcnow(),
        recovery_window=RECOVERY_WINDOW,
        telemetry_window=TELEMETRY_WINDOW,
    )


class TestPrecedence:
    """§4: the documented order, pinned as a table."""

    def test_a_live_incident_outranks_every_other_signal(self):
        signals = ComponentSignals(
            live_incidents=[(uuid.uuid4(), IncidentStatus.OPEN, utcnow())],
            open_anomalies=[(uuid.uuid4(), AnomalySeverity.CRITICAL)],
            active_forecasts=[(uuid.uuid4(), ForecastRiskLevel.HIGH)],
            last_telemetry_at=utcnow(),
        )
        result = _state(signals)
        assert result.state is ComponentOperationalState.INCIDENT
        assert result.trigger is StateTransitionTrigger.INCIDENT_OPENED
        #: The evidence names the incident, so the UI can link to it rather than
        #: just colouring the node.
        assert result.evidence.incident_ids

    def test_a_recent_recovery_outranks_a_high_forecast(self):
        """RECOVERING sits above AT_RISK: it *was* broken, which is strictly more
        than a prediction that it might be."""
        signals = ComponentSignals(
            resolved_incidents=[(uuid.uuid4(), utcnow() - timedelta(minutes=5))],
            active_forecasts=[(uuid.uuid4(), ForecastRiskLevel.HIGH)],
            last_telemetry_at=utcnow(),
        )
        result = _state(signals)
        assert result.state is ComponentOperationalState.RECOVERING
        assert result.trigger is StateTransitionTrigger.INCIDENT_RESOLVED
        assert result.recovery_until is not None

    def test_a_recovery_outside_the_window_is_not_recovering(self):
        """The window is load-bearing: an incident resolved an hour ago does not
        leave the component in RECOVERING forever."""
        signals = ComponentSignals(
            resolved_incidents=[(uuid.uuid4(), utcnow() - timedelta(hours=1))],
            last_telemetry_at=utcnow(),
        )
        result = _state(signals)
        assert result.state is ComponentOperationalState.HEALTHY

    def test_a_critical_anomaly_without_an_incident_is_degraded(self):
        signals = ComponentSignals(
            open_anomalies=[(uuid.uuid4(), AnomalySeverity.HIGH)],
            last_telemetry_at=utcnow(),
        )
        result = _state(signals)
        assert result.state is ComponentOperationalState.DEGRADED
        assert result.trigger is StateTransitionTrigger.ANOMALY_DETECTED

    def test_a_medium_anomaly_is_at_risk_not_degraded(self):
        """The severity split: a MEDIUM anomaly is a warning, not a degradation.
        Collapsing the two would make every noisy metric look like an outage."""
        signals = ComponentSignals(
            open_anomalies=[(uuid.uuid4(), AnomalySeverity.MEDIUM)],
            last_telemetry_at=utcnow(),
        )
        result = _state(signals)
        assert result.state is ComponentOperationalState.AT_RISK

    def test_a_high_forecast_without_an_anomaly_is_at_risk(self):
        """A prediction is not an incident: the component is flagged, not
        declared degraded — the same epistemic rule as Phase 8."""
        signals = ComponentSignals(
            active_forecasts=[(uuid.uuid4(), ForecastRiskLevel.CRITICAL)],
            last_telemetry_at=utcnow(),
        )
        result = _state(signals)
        assert result.state is ComponentOperationalState.AT_RISK
        assert result.trigger is StateTransitionTrigger.FORECAST_RISK

    def test_an_unknown_forecast_is_not_elevated_risk(self):
        """Phase 8's UNKNOWN means "not enough evidence", never "no risk" — and
        equally never "high risk". It must not promote a component to AT_RISK."""
        signals = ComponentSignals(
            active_forecasts=[(uuid.uuid4(), ForecastRiskLevel.UNKNOWN)],
            last_telemetry_at=utcnow(),
        )
        result = _state(signals)
        assert result.state is ComponentOperationalState.HEALTHY

    def test_a_low_forecast_is_not_elevated_risk(self):
        signals = ComponentSignals(
            active_forecasts=[(uuid.uuid4(), ForecastRiskLevel.LOW)],
            last_telemetry_at=utcnow(),
        )
        assert _state(signals).state is ComponentOperationalState.HEALTHY

    def test_healthy_needs_recent_telemetry_and_no_signal(self):
        signals = ComponentSignals(last_telemetry_at=utcnow())
        result = _state(signals)
        assert result.state is ComponentOperationalState.HEALTHY
        assert result.trigger is StateTransitionTrigger.EVIDENCE

    def test_no_evidence_at_all_is_unknown_not_healthy(self):
        """The load-bearing negative. A component ARGUS knows nothing about is
        UNKNOWN — a different and actionable statement from HEALTHY."""
        result = _state(ComponentSignals())
        assert result.state is ComponentOperationalState.UNKNOWN
        assert result.trigger is StateTransitionTrigger.DATA_GAP
        assert "no telemetry" in result.reason

    def test_stale_telemetry_is_unknown(self):
        signals = ComponentSignals(last_telemetry_at=utcnow() - timedelta(hours=3))
        result = _state(signals)
        assert result.state is ComponentOperationalState.UNKNOWN

    def test_derivation_is_deterministic(self):
        signals = ComponentSignals(
            open_anomalies=[(uuid.uuid4(), AnomalySeverity.HIGH)],
            last_telemetry_at=utcnow(),
        )
        first = _state(signals)
        second = _state(signals)
        assert first.state is second.state
        assert first.reason == second.reason


class TestDerivationFromStoredEvidence:
    """The same derivation, but driven by rows rather than hand-built signals."""

    async def test_an_open_incident_makes_its_component_incident(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        results = await derive_component_states(
            db_session,
            project_id=ctx.project.id,
            component_ids=[ctx.component.id],
        )
        assert len(results) == 1
        assert results[0].state is ComponentOperationalState.INCIDENT

    async def test_a_resolved_incident_makes_its_component_recovering(self, db_session):
        moment = utcnow()
        ctx = await episode(
            db_session,
            onset=hours_ago(moment, 2),
            incident_status="RESOLVED",
            resolved_at=minutes_ago(moment, 5),
        )
        results = await derive_component_states(
            db_session,
            project_id=ctx.project.id,
            component_ids=[ctx.component.id],
        )
        assert results[0].state is ComponentOperationalState.RECOVERING

    async def test_a_component_with_no_evidence_is_unknown(self, db_session):
        from tests.phase6_helpers import build_project

        project, _environment, component = await build_project(db_session)
        results = await derive_component_states(
            db_session,
            project_id=project.id,
            component_ids=[component.id],
        )
        assert results[0].state is ComponentOperationalState.UNKNOWN

    async def test_another_projects_incident_does_not_colour_this_component(
        self, db_session
    ):
        """§42: state is derived per project."""
        from tests.phase6_helpers import build_project

        await episode(db_session, incident_status="OPEN")
        other_project, _env, other_component = await build_project(
            db_session, name="Other Tenant"
        )
        results = await derive_component_states(
            db_session,
            project_id=other_project.id,
            component_ids=[other_component.id],
        )
        assert results[0].state is ComponentOperationalState.UNKNOWN


class TestTransitions:
    """§5: the history is a history of *changes*, not a log of polls."""

    async def test_the_first_evaluation_writes_a_transition(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        results = await derive_component_states(
            db_session,
            project_id=ctx.project.id,
            component_ids=[ctx.component.id],
        )
        written = await record_transitions(
            db_session,
            project_id=ctx.project.id,
            results=results,
            environment_by_component={ctx.component.id: ctx.environment.id},
        )
        assert len(written) == 1
        assert written[0].previous_state is None
        assert written[0].new_state is ComponentOperationalState.INCIDENT
        assert written[0].evidence["reason"]
        assert written[0].environment_id == ctx.environment.id

    async def test_recomputing_an_unchanged_state_writes_nothing(self, db_session):
        """Idempotent by design: polling must not append rows, or the history
        becomes unreadable exactly when it is being read."""
        ctx = await episode(db_session, incident_status="OPEN")
        results = await derive_component_states(
            db_session,
            project_id=ctx.project.id,
            component_ids=[ctx.component.id],
        )
        first = await record_transitions(
            db_session, project_id=ctx.project.id, results=results
        )
        second = await record_transitions(
            db_session, project_id=ctx.project.id, results=results
        )
        assert len(first) == 1
        assert second == []

    async def test_a_changed_state_is_appended_with_its_predecessor(self, db_session):
        """The incident closes, but the HIGH anomaly is still open — so the state
        moves INCIDENT → DEGRADED rather than to HEALTHY. That is the precedence
        chain doing its job: closing the incident does not clear the degradation.
        """
        ctx = await episode(db_session, incident_status="OPEN")
        results = await derive_component_states(
            db_session,
            project_id=ctx.project.id,
            component_ids=[ctx.component.id],
        )
        await record_transitions(db_session, project_id=ctx.project.id, results=results)

        ctx.incident.status = IncidentStatus.RESOLVED
        ctx.incident.resolved_at = utcnow() - timedelta(hours=5)
        await db_session.flush()

        later = await derive_component_states(
            db_session,
            project_id=ctx.project.id,
            component_ids=[ctx.component.id],
        )
        written = await record_transitions(
            db_session, project_id=ctx.project.id, results=later
        )
        assert len(written) == 1
        assert written[0].previous_state is ComponentOperationalState.INCIDENT
        assert written[0].new_state is ComponentOperationalState.DEGRADED

    async def test_closing_every_signal_returns_the_component_to_unknown(
        self, db_session
    ):
        """With the incident resolved *and* the anomaly closed, nothing is left —
        and because there is no telemetry either, the answer is UNKNOWN rather
        than a fabricated HEALTHY."""
        from app.models.anomaly import AnomalyStatus

        ctx = await episode(db_session, incident_status="OPEN")
        ctx.incident.status = IncidentStatus.RESOLVED
        ctx.incident.resolved_at = utcnow() - timedelta(hours=5)
        ctx.anomaly.status = AnomalyStatus.RESOLVED
        await db_session.flush()

        results = await derive_component_states(
            db_session,
            project_id=ctx.project.id,
            component_ids=[ctx.component.id],
        )
        assert results[0].state is ComponentOperationalState.UNKNOWN

    async def test_history_reconstructs_the_state_at_a_past_instant(self, db_session):
        """§5: ``state_at`` is what makes "what did ARGUS think at 10:00?"
        answerable — the requirement that turns transitions into history."""
        ctx = await episode(db_session, incident_status="OPEN")
        results = await derive_component_states(
            db_session,
            project_id=ctx.project.id,
            component_ids=[ctx.component.id],
        )
        await record_transitions(db_session, project_id=ctx.project.id, results=results)

        current = await state_at(
            db_session, component_id=ctx.component.id, moment=utcnow()
        )
        assert current is ComponentOperationalState.INCIDENT

        before_anything = await state_at(
            db_session,
            component_id=ctx.component.id,
            moment=utcnow() - timedelta(days=2),
        )
        assert before_anything is None, (
            "before the first transition there is no known state; a guessed "
            "HEALTHY would be a fabricated fact"
        )

    async def test_history_is_readable_for_one_component(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        results = await derive_component_states(
            db_session,
            project_id=ctx.project.id,
            component_ids=[ctx.component.id],
        )
        await record_transitions(db_session, project_id=ctx.project.id, results=results)
        rows = await state_history(db_session, component_id=ctx.component.id)
        assert len(rows) == 1
        assert rows[0].component_id == ctx.component.id


class TestStateChangeEvents:
    """§10: a state change is part of the situation's story."""

    async def test_a_transition_publishes_a_correlated_event(self, db_session):
        from app.models.platform import PlatformEvent, PlatformEventType

        ctx = await episode(db_session, incident_status="OPEN")
        results = await derive_component_states(
            db_session,
            project_id=ctx.project.id,
            component_ids=[ctx.component.id],
        )
        await record_transitions(db_session, project_id=ctx.project.id, results=results)
        await db_session.flush()

        events = (
            await db_session.scalars(
                select(PlatformEvent).where(
                    PlatformEvent.event_type
                    == PlatformEventType.COMPONENT_STATE_CHANGED
                )
            )
        ).all()
        assert len(events) == 1
        #: Anchored on the component, so the change joins its own story (§10).
        assert events[0].correlation_id == f"component:{ctx.component.id}"
        assert events[0].payload["new_state"] == "INCIDENT"

    async def test_events_can_be_suppressed_for_a_bulk_recompute(self, db_session):
        from app.models.platform import PlatformEvent, PlatformEventType

        ctx = await episode(db_session, incident_status="OPEN")
        results = await derive_component_states(
            db_session,
            project_id=ctx.project.id,
            component_ids=[ctx.component.id],
        )
        await record_transitions(
            db_session,
            project_id=ctx.project.id,
            results=results,
            emit_events=False,
        )
        await db_session.flush()
        events = (
            await db_session.scalars(
                select(PlatformEvent).where(
                    PlatformEvent.event_type
                    == PlatformEventType.COMPONENT_STATE_CHANGED
                )
            )
        ).all()
        assert events == []


class TestSystemState:
    """§2: the composed view, and the honesty of its gaps."""

    async def test_state_counts_sum_to_its_components(self, db_session):
        from app.services.system_state import build_system_state

        ctx = await episode(db_session, incident_status="OPEN")
        state = await build_system_state(
            db_session,
            project_id=ctx.project.id,
            environment_id=ctx.environment.id,
            include=("components", "health", "active_incidents"),
        )
        assert state.project_id == ctx.project.id
        counts = state.state_counts()
        assert sum(counts.values()) == len(state.components)
        assert state.active_incidents

    async def test_state_can_be_narrowed_to_the_sections_requested(self, db_session):
        """Components are always resolved — health is *derived* from them, so
        skipping them would mean reporting health without measuring it. The
        optional sections are what ``include`` narrows."""
        from app.services.system_state import build_system_state

        ctx = await episode(db_session, incident_status="OPEN")
        narrow = await build_system_state(
            db_session, project_id=ctx.project.id, include=("health",)
        )
        full = await build_system_state(db_session, project_id=ctx.project.id)

        assert narrow.components, "state cannot be computed without its components"
        assert narrow.active_incidents == []
        assert narrow.dependencies == []
        assert full.active_incidents, "the un-narrowed view does include incidents"

    async def test_an_empty_scope_says_so_rather_than_looking_healthy(self, db_session):
        """A composed view must disclose what it could not measure; otherwise an
        empty project reads as an all-clear."""
        from app.services.system_state import build_system_state
        from tests.phase6_helpers import build_project

        project, _environment, _component = await build_project(db_session)
        #: Remove the component so the scope genuinely has nothing to report on.
        await db_session.delete(_component)
        await db_session.flush()

        state = await build_system_state(db_session, project_id=project.id)
        assert state.components == []
        assert any(
            "no components" in note.lower() for note in state.limitations
        ), state.limitations
        assert state.as_of is not None

    async def test_a_healthy_scope_reports_no_limitations(self, db_session):
        """The other half of the contract: limitations lists *failures*, not
        decoration. An all-clear run must not pad the list to look thorough."""
        from app.services.system_state import build_system_state

        ctx = await episode(db_session, incident_status="OPEN")
        state = await build_system_state(db_session, project_id=ctx.project.id)
        assert state.limitations == []

    async def test_state_is_scoped_to_one_project(self, db_session):
        """§42: a project's state never includes another project's incidents."""
        from app.services.system_state import build_system_state
        from tests.phase6_helpers import build_project

        ctx = await episode(db_session, incident_status="OPEN")
        other_project, _env, _component = await build_project(db_session)

        mine = await build_system_state(db_session, project_id=ctx.project.id)
        theirs = await build_system_state(db_session, project_id=other_project.id)

        assert mine.active_incidents
        assert theirs.active_incidents == []
        assert {c["id"] for c in mine.components}.isdisjoint(
            {c["id"] for c in theirs.components}
        )
