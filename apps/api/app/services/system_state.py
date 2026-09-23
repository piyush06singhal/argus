"""ARGUS Unified System State (Phase 11 §2–§6).

One computed answer to "what is the state of this system right now", assembled
from the rows Phases 1–10 already wrote. Nothing here is authoritative about
another phase's facts: the observability subsystem owns telemetry, the graph
owns structure, the incident subsystem owns incidents, the forecast service owns
predictions. This module *reads* them and states a conclusion, with its evidence.

The state is computed, not stored — with one exception that is the whole point of
the design:

* **Component state is derived every time it is asked for**, from the current
  rows. There is no ``components.operational_state`` column to go stale, disagree
  with the incident table, or be written by something that forgot a rule.
* **Transitions are stored** (§5). When the derivation produces a state different
  from the last recorded one, a ``component_state_transitions`` row is appended
  with the trigger and the evidence. So "what was production doing at 14:05" is a
  query, and every state change can be argued with.

State precedence (§4) is declared in :data:`app.models.platform.STATE_PRECEDENCE`
and applied by :func:`derive_state`, and the conditions for each state are stated
in one place rather than distributed across UI code:

============  ==========================================================
state         condition (first match wins)
============  ==========================================================
INCIDENT      an unresolved incident has this component as its primary or
              linked affected component
RECOVERING    an incident on this component resolved inside the recovery
              window, or a successful remediation completed inside it
DEGRADED      an unresolved anomaly of severity HIGH/CRITICAL is open on it
AT_RISK       an active forecast rates it HIGH or CRITICAL, or an
              unresolved anomaly of severity LOW/MEDIUM is open on it
HEALTHY       recent telemetry exists for it and nothing above applies
UNKNOWN       no evidence at all — no telemetry, anomaly, incident or
              forecast inside the lookback window
============  ==========================================================

``UNKNOWN`` is deliberately last and deliberately not ``HEALTHY``: absence of
evidence is not evidence of health, and a platform that reports a silent
component as healthy is the failure mode this phase exists to remove.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.services.platform_time import aware as _aware
from app.models.anomaly import Anomaly, AnomalySeverity, AnomalyStatus
from app.models.incident import Incident, IncidentStatus
from app.models.observability import ObservabilityEvent
from app.models.platform import (
    STATE_PRECEDENCE,
    ComponentOperationalState,
    ComponentStateTransition,
    PlatformEventType,
    StateTransitionTrigger,
)
from app.models.reliability import (
    ForecastRiskLevel,
    ForecastStatus,
    ReliabilityForecast,
)
from app.models.remediation import (
    RemediationAction,
    RemediationOutcome,
    RemediationStatus,
)
from app.models.system import ComponentDependency, SystemComponent

logger = logging.getLogger(__name__)

#: Incident statuses that still count as live. Mirrors the incident subsystem's
#: own terminal set; kept as a constant so the derivation cannot drift from it
#: silently — if Phase 3 ever changes the set, this line changes with it.
UNRESOLVED_INCIDENT_STATUSES = (
    IncidentStatus.OPEN,
    IncidentStatus.ACKNOWLEDGED,
    IncidentStatus.INVESTIGATING,
    IncidentStatus.MITIGATED,
)
RESOLVED_INCIDENT_STATUSES = (IncidentStatus.RESOLVED, IncidentStatus.CLOSED)
UNRESOLVED_ANOMALY_STATUSES = (
    AnomalyStatus.DETECTED,
    AnomalyStatus.ACKNOWLEDGED,
    AnomalyStatus.INVESTIGATING,
)
ACTIVE_FORECAST_STATUSES = (ForecastStatus.GENERATED, ForecastStatus.ACTIVE)
ELEVATED_RISK_LEVELS = (ForecastRiskLevel.HIGH, ForecastRiskLevel.CRITICAL)


@dataclass
class StateEvidence:
    """Everything that produced one component's state, kept together.

    The ``reason`` string is what a person reads; the id lists are what makes it
    checkable. Both are stored on the transition row.
    """

    incident_ids: list[uuid.UUID] = field(default_factory=list)
    anomaly_ids: list[uuid.UUID] = field(default_factory=list)
    forecast_ids: list[uuid.UUID] = field(default_factory=list)
    remediation_ids: list[uuid.UUID] = field(default_factory=list)
    last_telemetry_at: Optional[datetime] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "incident_ids": [str(i) for i in self.incident_ids],
            "anomaly_ids": [str(i) for i in self.anomaly_ids],
            "forecast_ids": [str(i) for i in self.forecast_ids],
            "remediation_ids": [str(i) for i in self.remediation_ids],
            "last_telemetry_at": self.last_telemetry_at.isoformat()
            if self.last_telemetry_at
            else None,
        }


@dataclass
class ComponentStateResult:
    """One component's derived state, with the evidence behind it."""

    component_id: uuid.UUID
    state: ComponentOperationalState
    reason: str
    trigger: StateTransitionTrigger
    evidence: StateEvidence = field(default_factory=StateEvidence)
    recovery_until: Optional[datetime] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "component_id": str(self.component_id),
            "state": self.state.value,
            "reason": self.reason,
            "evidence": self.evidence.as_dict(),
        }


@dataclass
class SystemState:
    """The §2 computed view of one project (optionally one environment)."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID]
    as_of: datetime
    components: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    health: dict[str, Any] = field(default_factory=dict)
    active_anomalies: list[dict[str, Any]] = field(default_factory=list)
    active_incidents: list[dict[str, Any]] = field(default_factory=list)
    predicted_risks: list[dict[str, Any]] = field(default_factory=list)
    recent_changes: list[dict[str, Any]] = field(default_factory=list)
    active_remediations: list[dict[str, Any]] = field(default_factory=list)
    reliability_patterns: list[dict[str, Any]] = field(default_factory=list)
    data_quality: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def state_counts(self) -> dict[str, int]:
        counts = {state.value: 0 for state in STATE_PRECEDENCE}
        for component in self.components:
            key = str(component.get("state"))
            counts[key] = counts.get(key, 0) + 1
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": str(self.project_id),
            "environment_id": str(self.environment_id) if self.environment_id else None,
            "as_of": self.as_of.isoformat(),
            "components": self.components,
            "dependencies": self.dependencies,
            "health": self.health,
            "active_anomalies": self.active_anomalies,
            "active_incidents": self.active_incidents,
            "predicted_risks": self.predicted_risks,
            "recent_changes": self.recent_changes,
            "active_remediations": self.active_remediations,
            "reliability_patterns": self.reliability_patterns,
            "data_quality": self.data_quality,
            "state_counts": self.state_counts(),
            "limitations": list(self.limitations),
        }


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------
@dataclass
class ComponentSignals:
    """The raw inputs for one component's derivation.

    Batched by the caller so the dashboard does not run four queries per
    component (§65): one query per signal type, then a dictionary lookup.
    """

    live_incidents: list[tuple[uuid.UUID, IncidentStatus, datetime]] = field(
        default_factory=list
    )
    resolved_incidents: list[tuple[uuid.UUID, datetime]] = field(default_factory=list)
    open_anomalies: list[tuple[uuid.UUID, AnomalySeverity]] = field(
        default_factory=list
    )
    active_forecasts: list[tuple[uuid.UUID, ForecastRiskLevel]] = field(
        default_factory=list
    )
    completed_remediations: list[tuple[uuid.UUID, RemediationOutcome, datetime]] = (
        field(default_factory=list)
    )
    last_telemetry_at: Optional[datetime] = None

    def is_empty(self) -> bool:
        return not (
            self.live_incidents
            or self.resolved_incidents
            or self.open_anomalies
            or self.active_forecasts
            or self.completed_remediations
            or self.last_telemetry_at
        )


def derive_state(
    component_id: uuid.UUID,
    signals: ComponentSignals,
    *,
    now: datetime,
    recovery_window: timedelta,
    telemetry_window: timedelta,
) -> ComponentStateResult:
    """Apply the documented precedence to one component's signals (§3, §4).

    Pure and deterministic: same signals, same window, same answer. This is the
    function the tests pin, which is why the conditions live here and not in a
    serializer.
    """
    evidence = StateEvidence(last_telemetry_at=signals.last_telemetry_at)

    # 1. INCIDENT — something is broken right now.
    if signals.live_incidents:
        evidence.incident_ids = [row[0] for row in signals.live_incidents]
        return ComponentStateResult(
            component_id=component_id,
            state=ComponentOperationalState.INCIDENT,
            reason=(
                f"{len(signals.live_incidents)} unresolved incident(s) affect this "
                "component"
            ),
            trigger=StateTransitionTrigger.INCIDENT_OPENED,
            evidence=evidence,
        )

    # 2. RECOVERING — it was broken, recently, and nothing is broken now.
    recovery_until = None
    for incident_id, resolved_at in signals.resolved_incidents:
        moment = _aware(resolved_at)
        if moment and now - moment <= recovery_window:
            recovery_until = moment + recovery_window
            evidence.incident_ids.append(incident_id)
            break
    if recovery_until is None:
        for action_id, outcome, completed_at in signals.completed_remediations:
            moment = _aware(completed_at)
            if moment and now - moment <= recovery_window:
                recovery_until = moment + recovery_window
                evidence.remediation_ids.append(action_id)
                break
    if recovery_until is not None:
        return ComponentStateResult(
            component_id=component_id,
            state=ComponentOperationalState.RECOVERING,
            reason=(
                "a resolved incident or completed remediation is inside the "
                f"{int(recovery_window.total_seconds())}s recovery window"
            ),
            trigger=StateTransitionTrigger.INCIDENT_RESOLVED,
            evidence=evidence,
            recovery_until=recovery_until,
        )

    # 3. DEGRADED — an unresolved severe anomaly.
    severe = [
        row
        for row in signals.open_anomalies
        if row[1] in (AnomalySeverity.HIGH, AnomalySeverity.CRITICAL)
    ]
    if severe:
        evidence.anomaly_ids = [row[0] for row in signals.open_anomalies]
        return ComponentStateResult(
            component_id=component_id,
            state=ComponentOperationalState.DEGRADED,
            reason=(
                f"{len(severe)} unresolved HIGH/CRITICAL anomaly(ies) are open on "
                "this component"
            ),
            trigger=StateTransitionTrigger.ANOMALY_DETECTED,
            evidence=evidence,
        )

    # 4. AT_RISK — predicted elevated risk, or a milder open anomaly.
    elevated = [
        row for row in signals.active_forecasts if row[1] in ELEVATED_RISK_LEVELS
    ]
    if elevated:
        evidence.forecast_ids = [row[0] for row in signals.active_forecasts]
        return ComponentStateResult(
            component_id=component_id,
            state=ComponentOperationalState.AT_RISK,
            reason=(
                f"{len(elevated)} active forecast(s) rate this component "
                "HIGH or CRITICAL risk"
            ),
            trigger=StateTransitionTrigger.FORECAST_RISK,
            evidence=evidence,
        )
    if signals.open_anomalies:
        evidence.anomaly_ids = [row[0] for row in signals.open_anomalies]
        return ComponentStateResult(
            component_id=component_id,
            state=ComponentOperationalState.AT_RISK,
            reason="an unresolved LOW/MEDIUM anomaly is open on this component",
            trigger=StateTransitionTrigger.ANOMALY_DETECTED,
            evidence=evidence,
        )

    # 5. HEALTHY — evidence arrived recently and nothing above applies.
    last = _aware(signals.last_telemetry_at)
    if last is not None and now - last <= telemetry_window:
        return ComponentStateResult(
            component_id=component_id,
            state=ComponentOperationalState.HEALTHY,
            reason="telemetry observed inside the lookback window; no open signal",
            trigger=StateTransitionTrigger.EVIDENCE,
            evidence=evidence,
        )

    # 6. UNKNOWN — no evidence. Never reported as healthy.
    reason = (
        "no telemetry has been observed inside the lookback window"
        if last is not None
        else "no telemetry, anomaly, incident or forecast evidence exists"
    )
    return ComponentStateResult(
        component_id=component_id,
        state=ComponentOperationalState.UNKNOWN,
        reason=reason,
        trigger=StateTransitionTrigger.DATA_GAP,
        evidence=evidence,
    )


async def collect_signals(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    component_ids: Sequence[uuid.UUID],
    environment_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> dict[uuid.UUID, ComponentSignals]:
    """Batch-load every signal needed to derive state for these components.

    Four bounded queries regardless of component count — the alternative (a
    query per component) is what makes a dashboard take seconds.
    """
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    signals: dict[uuid.UUID, ComponentSignals] = {
        cid: ComponentSignals() for cid in component_ids
    }
    if not component_ids:
        return signals

    anomaly_window_start = moment - timedelta(
        seconds=settings.PLATFORM_STATE_ANOMALY_WINDOW_SECONDS
    )
    telemetry_window_start = moment - timedelta(
        seconds=settings.PLATFORM_STATE_TELEMETRY_WINDOW_SECONDS
    )
    recovery_window_start = moment - timedelta(
        seconds=settings.PLATFORM_STATE_RECOVERY_WINDOW_SECONDS
    )

    # -- live incidents, by primary component and by linked anomaly component
    incident_rows = (
        await session.execute(
            select(
                Incident.id,
                Incident.status,
                Incident.detected_at,
                Incident.primary_component_id,
                Incident.environment_id,
            ).where(
                Incident.project_id == project_id,
                Incident.status.in_(list(UNRESOLVED_INCIDENT_STATUSES)),
            )
        )
    ).all()
    linked_incidents: dict[
        uuid.UUID, list[tuple[uuid.UUID, IncidentStatus, datetime]]
    ] = {}
    for incident_id, status, detected_at, primary_component_id, _env in incident_rows:
        if primary_component_id in signals:
            signals[primary_component_id].live_incidents.append(
                (incident_id, status, detected_at)
            )
            linked_incidents.setdefault(incident_id, []).append(
                (incident_id, status, detected_at)
            )
    #: An incident that names no primary component still affects the components
    #: its anomalies were detected on — that link is what Phase 3 stored.
    orphan_incident_ids = [
        incident_id
        for incident_id, _status, _detected, primary, _env in incident_rows
        if primary not in signals
    ]
    if orphan_incident_ids:
        incident_lookup = {
            incident_id: (status, detected_at)
            for incident_id, status, detected_at, _p, _e in incident_rows
        }
        anomaly_links = (
            await session.execute(
                select(Anomaly.incident_id, Anomaly.component_id).where(
                    Anomaly.project_id == project_id,
                    Anomaly.incident_id.in_(orphan_incident_ids),
                )
            )
        ).all()
        for incident_id, component_id in anomaly_links:
            if component_id in signals and incident_id in incident_lookup:
                status, detected_at = incident_lookup[incident_id]
                row = (incident_id, status, detected_at)
                if row not in signals[component_id].live_incidents:
                    signals[component_id].live_incidents.append(row)

    # -- recently resolved incidents (recovery window)
    resolved_rows = (
        await session.execute(
            select(
                Incident.id, Incident.resolved_at, Incident.primary_component_id
            ).where(
                Incident.project_id == project_id,
                Incident.status.in_(list(RESOLVED_INCIDENT_STATUSES)),
                Incident.resolved_at.is_not(None),
                Incident.resolved_at >= recovery_window_start,
            )
        )
    ).all()
    for incident_id, resolved_at, primary_component_id in resolved_rows:
        if primary_component_id in signals:
            signals[primary_component_id].resolved_incidents.append(
                (incident_id, resolved_at)
            )

    # -- open anomalies
    anomaly_rows = (
        await session.execute(
            select(Anomaly.id, Anomaly.component_id, Anomaly.severity).where(
                Anomaly.project_id == project_id,
                Anomaly.component_id.in_(list(component_ids)),
                Anomaly.status.in_(list(UNRESOLVED_ANOMALY_STATUSES)),
                Anomaly.suppressed.is_(False),
                Anomaly.detected_at >= anomaly_window_start,
            )
        )
    ).all()
    for anomaly_id, component_id, severity in anomaly_rows:
        signals[component_id].open_anomalies.append((anomaly_id, severity))

    # -- active forecasts at elevated risk
    forecast_rows = (
        await session.execute(
            select(
                ReliabilityForecast.id,
                ReliabilityForecast.component_id,
                ReliabilityForecast.risk_level,
            ).where(
                ReliabilityForecast.project_id == project_id,
                ReliabilityForecast.component_id.in_(list(component_ids)),
                ReliabilityForecast.status.in_(list(ACTIVE_FORECAST_STATUSES)),
                ReliabilityForecast.valid_until >= moment,
            )
        )
    ).all()
    for forecast_id, component_id, risk_level in forecast_rows:
        signals[component_id].active_forecasts.append((forecast_id, risk_level))

    # -- recently completed remediations (recovery window)
    remediation_rows = (
        await session.execute(
            select(
                RemediationAction.id,
                RemediationAction.component_id,
                RemediationAction.outcome,
                RemediationAction.completed_at,
            ).where(
                RemediationAction.project_id == project_id,
                RemediationAction.component_id.in_(list(component_ids)),
                RemediationAction.completed_at.is_not(None),
                RemediationAction.completed_at >= recovery_window_start,
                RemediationAction.outcome.in_(
                    [
                        RemediationOutcome.EFFECTIVE,
                        RemediationOutcome.PARTIALLY_EFFECTIVE,
                    ]
                ),
            )
        )
    ).all()
    for action_id, component_id, outcome, completed_at in remediation_rows:
        signals[component_id].completed_remediations.append(
            (action_id, outcome, completed_at)
        )

    # -- last telemetry per component (one grouped query)
    telemetry_rows = (
        await session.execute(
            select(
                ObservabilityEvent.component_id,
                func.max(ObservabilityEvent.timestamp),
            )
            .where(
                ObservabilityEvent.project_id == project_id,
                ObservabilityEvent.component_id.in_(list(component_ids)),
            )
            .group_by(ObservabilityEvent.component_id)
        )
    ).all()
    for component_id, last_at in telemetry_rows:
        if component_id in signals:
            signals[component_id].last_telemetry_at = last_at
            aware_last = _aware(last_at)
            if aware_last is not None and aware_last < telemetry_window_start:
                #: Old telemetry does not make a component healthy; it makes the
                #: last-seen timestamp honest and the state UNKNOWN.
                pass

    return signals


async def derive_component_states(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    component_ids: Sequence[uuid.UUID],
    environment_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> list[ComponentStateResult]:
    """Derive state for a set of components (no transitions written)."""
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    signals = await collect_signals(
        session,
        project_id=project_id,
        component_ids=component_ids,
        environment_id=environment_id,
        now=moment,
        settings=settings,
    )
    recovery_window = timedelta(seconds=settings.PLATFORM_STATE_RECOVERY_WINDOW_SECONDS)
    telemetry_window = timedelta(
        seconds=settings.PLATFORM_STATE_TELEMETRY_WINDOW_SECONDS
    )
    return [
        derive_state(
            component_id,
            signals[component_id],
            now=moment,
            recovery_window=recovery_window,
            telemetry_window=telemetry_window,
        )
        for component_id in component_ids
    ]


async def current_states(
    session: AsyncSession, *, project_id: uuid.UUID
) -> dict[uuid.UUID, ComponentStateTransition]:
    """The latest recorded transition per component.

    Reads the transition history rather than a state column because the history
    *is* the source: the newest row for a component is its current state.
    """
    stmt = (
        select(ComponentStateTransition)
        .where(ComponentStateTransition.project_id == project_id)
        .order_by(
            ComponentStateTransition.component_id,
            ComponentStateTransition.occurred_at.desc(),
            ComponentStateTransition.created_at.desc(),
        )
    )
    latest: dict[uuid.UUID, ComponentStateTransition] = {}
    for row in (await session.scalars(stmt)).all():
        latest.setdefault(row.component_id, row)
    return latest


async def record_transitions(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    results: Iterable[ComponentStateResult],
    environment_by_component: Optional[dict[uuid.UUID, Optional[uuid.UUID]]] = None,
    now: Optional[datetime] = None,
    case_id: Optional[uuid.UUID] = None,
    emit_events: bool = True,
) -> list[ComponentStateTransition]:
    """Append a transition row for every component whose state actually changed.

    Returns only the transitions that were written. Recording is *idempotent*: a
    recomputation that produces the same state writes nothing, so the history
    stays a history of changes rather than a log of polls.
    """
    moment = _aware(now) or datetime.now(timezone.utc)
    results = list(results)
    if not results:
        return []

    previous = await current_states(session, project_id=project_id)
    written: list[ComponentStateTransition] = []
    from app.services.platform_events import correlation_id_for, safely_publish_event

    for result in results:
        before = previous.get(result.component_id)
        before_state = before.new_state if before else None
        if before_state == result.state:
            continue
        transition = ComponentStateTransition(
            project_id=project_id,
            environment_id=(environment_by_component or {}).get(result.component_id),
            component_id=result.component_id,
            previous_state=before_state,
            new_state=result.state,
            trigger=result.trigger,
            evidence=result.evidence.as_dict() | {"reason": result.reason},
            reason=result.reason,
            source="system_state",
            occurred_at=moment,
            case_id=case_id,
        )
        session.add(transition)
        written.append(transition)

        if emit_events:
            await safely_publish_event(
                session,
                project_id=project_id,
                environment_id=transition.environment_id,
                event_type=PlatformEventType.COMPONENT_STATE_CHANGED,
                source="system_state",
                subject_type="component",
                subject_id=result.component_id,
                component_id=result.component_id,
                case_id=case_id,
                #: Anchored so the state change joins the rest of its situation's
                #: story (§10). A change inside a case belongs to that case; a
                #: standalone change is anchored on the component, which is the
                #: only subject that is certainly the same across recomputations.
                correlation_id=(
                    correlation_id_for(kind="case", subject_id=case_id)
                    if case_id is not None
                    else correlation_id_for(
                        kind="component", subject_id=result.component_id
                    )
                ),
                occurred_at=moment,
                payload={
                    "previous_state": before_state.value if before_state else None,
                    "new_state": result.state.value,
                    "reason": result.reason,
                    "trigger": result.trigger.value,
                },
                dedup_extra=[
                    before_state.value if before_state else "none",
                    result.state.value,
                ],
            )
    if written:
        await session.flush()
    return written


async def state_history(
    session: AsyncSession,
    *,
    component_id: uuid.UUID,
    since: Optional[datetime] = None,
    limit: int = 200,
) -> list[ComponentStateTransition]:
    """The state timeline for one component, oldest first."""
    stmt = (
        select(ComponentStateTransition)
        .where(ComponentStateTransition.component_id == component_id)
        .order_by(ComponentStateTransition.occurred_at)
        .limit(limit)
    )
    if since is not None:
        stmt = stmt.where(ComponentStateTransition.occurred_at >= _aware(since))
    return list((await session.scalars(stmt)).all())


async def state_at(
    session: AsyncSession,
    *,
    component_id: uuid.UUID,
    moment: datetime,
) -> Optional[ComponentOperationalState]:
    """Reconstruct a component's state as of a past instant (§5).

    The most recent transition at or before ``moment`` is the answer; with no
    such transition the component's state is genuinely unknown, which is
    reported as ``None`` rather than guessed as healthy.
    """
    stmt = (
        select(ComponentStateTransition)
        .where(
            ComponentStateTransition.component_id == component_id,
            ComponentStateTransition.occurred_at <= _aware(moment),
        )
        .order_by(ComponentStateTransition.occurred_at.desc())
        .limit(1)
    )
    row = (await session.scalars(stmt)).first()
    return row.new_state if row is not None else None


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
async def build_system_state(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
    write_transitions: bool = False,
    include: Optional[Sequence[str]] = None,
) -> SystemState:
    """Assemble the §2 system state for one project scope.

    Every section is bounded: components by the configured cap, incidents,
    anomalies, forecasts and remediations by their own limits, and graph
    traversal by one hop. The dashboard calls this; a dashboard that runs
    unbounded queries is a dashboard that takes down the database it reports on
    (§65).
    """
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    include_set = set(include) if include else None

    def wants(section: str) -> bool:
        return include_set is None or section in include_set

    component_stmt = (
        select(SystemComponent)
        .where(SystemComponent.project_id == project_id)
        .order_by(SystemComponent.name)
        .limit(settings.PLATFORM_STATE_COMPONENT_LIMIT)
    )
    if environment_id is not None:
        component_stmt = component_stmt.where(
            SystemComponent.environment_id == environment_id
        )
    components = list((await session.scalars(component_stmt)).all())
    component_ids = [c.id for c in components]
    environment_by_component = {c.id: c.environment_id for c in components}

    state = SystemState(
        project_id=project_id,
        environment_id=environment_id,
        as_of=moment,
    )

    # -- components + derived state
    results = await derive_component_states(
        session,
        project_id=project_id,
        component_ids=component_ids,
        environment_id=environment_id,
        now=moment,
        settings=settings,
    )
    by_id = {r.component_id: r for r in results}
    components_by_id = {c.id: c for c in components}
    if write_transitions and component_ids:
        await record_transitions(
            session,
            project_id=project_id,
            results=results,
            environment_by_component=environment_by_component,
            now=moment,
        )
    for component in components:
        result = by_id.get(component.id)
        state.components.append(
            {
                "id": str(component.id),
                "name": component.name,
                "component_type": getattr(
                    component.component_type, "value", str(component.component_type)
                ),
                "environment_id": str(component.environment_id)
                if component.environment_id
                else None,
                "state": result.state.value
                if result
                else ComponentOperationalState.UNKNOWN.value,
                "state_reason": result.reason if result else "not evaluated",
                "state_evidence": result.evidence.as_dict() if result else {},
            }
        )

    # -- dependencies (one hop, bounded)
    if wants("dependencies") and component_ids:
        #: ``component_dependencies`` has no project column — ownership lives on
        #: the components. Scoping by the project's component ids is therefore the
        #: only correct filter, and it is also an indexed one.
        dep_stmt = (
            select(ComponentDependency)
            .where(
                ComponentDependency.source_component_id.in_(component_ids),
                ComponentDependency.target_component_id.in_(component_ids),
            )
            .limit(settings.PLATFORM_STATE_DEPENDENCY_LIMIT)
        )
        dependencies = list((await session.scalars(dep_stmt)).all())
        for dependency in dependencies:
            source = components_by_id.get(dependency.source_component_id)
            target = components_by_id.get(dependency.target_component_id)
            state.dependencies.append(
                {
                    "id": str(dependency.id),
                    "source_component_id": str(dependency.source_component_id),
                    "source_name": source.name if source else None,
                    "target_component_id": str(dependency.target_component_id),
                    "target_name": target.name if target else None,
                    "dependency_type": getattr(
                        dependency.dependency_type,
                        "value",
                        str(dependency.dependency_type),
                    ),
                }
            )

    # -- active anomalies
    if wants("active_anomalies"):
        anomaly_stmt = (
            select(Anomaly)
            .where(
                Anomaly.project_id == project_id,
                Anomaly.status.in_(list(UNRESOLVED_ANOMALY_STATUSES)),
                Anomaly.suppressed.is_(False),
            )
            .order_by(Anomaly.detected_at.desc())
            .limit(settings.PLATFORM_STATE_ACTIVE_LIMIT)
        )
        if environment_id is not None:
            anomaly_stmt = anomaly_stmt.where(Anomaly.environment_id == environment_id)
        for anomaly in (await session.scalars(anomaly_stmt)).all():
            state.active_anomalies.append(
                {
                    "id": str(anomaly.id),
                    "component_id": str(anomaly.component_id)
                    if anomaly.component_id
                    else None,
                    "component_name": (
                        components_by_id[anomaly.component_id].name
                        if anomaly.component_id in components_by_id
                        else None
                    ),
                    "anomaly_type": getattr(
                        anomaly.anomaly_type, "value", str(anomaly.anomaly_type)
                    ),
                    "severity": getattr(
                        anomaly.severity, "value", str(anomaly.severity)
                    ),
                    "status": getattr(anomaly.status, "value", str(anomaly.status)),
                    "metric_name": anomaly.metric_name,
                    "detected_at": _aware(anomaly.detected_at).isoformat()
                    if _aware(anomaly.detected_at)
                    else None,
                    "incident_id": str(anomaly.incident_id)
                    if anomaly.incident_id
                    else None,
                }
            )

    # -- active incidents
    if wants("active_incidents"):
        incident_stmt = (
            select(Incident)
            .where(
                Incident.project_id == project_id,
                Incident.status.in_(list(UNRESOLVED_INCIDENT_STATUSES)),
            )
            .order_by(Incident.detected_at.desc())
            .limit(settings.PLATFORM_STATE_ACTIVE_LIMIT)
        )
        if environment_id is not None:
            incident_stmt = incident_stmt.where(
                Incident.environment_id == environment_id
            )
        for incident in (await session.scalars(incident_stmt)).all():
            state.active_incidents.append(
                {
                    "id": str(incident.id),
                    "title": incident.title,
                    "severity": getattr(
                        incident.severity, "value", str(incident.severity)
                    ),
                    "status": getattr(incident.status, "value", str(incident.status)),
                    "primary_component_id": str(incident.primary_component_id)
                    if incident.primary_component_id
                    else None,
                    "detected_at": _aware(incident.detected_at).isoformat()
                    if _aware(incident.detected_at)
                    else None,
                    "fingerprint": incident.fingerprint,
                }
            )

    # -- predicted risks
    if wants("predicted_risks"):
        forecast_stmt = (
            select(ReliabilityForecast)
            .where(
                ReliabilityForecast.project_id == project_id,
                ReliabilityForecast.status.in_(list(ACTIVE_FORECAST_STATUSES)),
                ReliabilityForecast.valid_until >= moment,
            )
            .order_by(ReliabilityForecast.risk_level.desc())
            .limit(settings.PLATFORM_STATE_ACTIVE_LIMIT)
        )
        if environment_id is not None:
            forecast_stmt = forecast_stmt.where(
                ReliabilityForecast.environment_id == environment_id
            )
        for forecast in (await session.scalars(forecast_stmt)).all():
            state.predicted_risks.append(
                {
                    "id": str(forecast.id),
                    "component_id": str(forecast.component_id)
                    if forecast.component_id
                    else None,
                    "component_name": (
                        components_by_id[forecast.component_id].name
                        if forecast.component_id in components_by_id
                        else None
                    ),
                    "prediction_type": getattr(
                        forecast.prediction_type, "value", str(forecast.prediction_type)
                    ),
                    "risk_level": getattr(
                        forecast.risk_level, "value", str(forecast.risk_level)
                    ),
                    "risk_score": forecast.risk_score,
                    "confidence": forecast.confidence,
                    "data_quality": getattr(
                        forecast.data_quality, "value", str(forecast.data_quality)
                    ),
                    "valid_until": _aware(forecast.valid_until).isoformat()
                    if _aware(forecast.valid_until)
                    else None,
                }
            )

    # -- active remediations
    if wants("active_remediations"):
        active_statuses = [
            RemediationStatus.PROPOSED,
            RemediationStatus.VALIDATING,
            RemediationStatus.POLICY_REVIEW,
            RemediationStatus.AWAITING_APPROVAL,
            RemediationStatus.AUTHORIZED,
            RemediationStatus.SCHEDULED,
            RemediationStatus.EXECUTING,
            RemediationStatus.VERIFYING,
            RemediationStatus.ROLLING_BACK,
        ]
        remediation_stmt = (
            select(RemediationAction)
            .where(
                RemediationAction.project_id == project_id,
                RemediationAction.status.in_(active_statuses),
            )
            .order_by(RemediationAction.created_at.desc())
            .limit(settings.PLATFORM_STATE_ACTIVE_LIMIT)
        )
        for action in (await session.scalars(remediation_stmt)).all():
            state.active_remediations.append(
                {
                    "id": str(action.id),
                    "action_type": getattr(
                        action.action_type, "value", str(action.action_type)
                    ),
                    "status": getattr(action.status, "value", str(action.status)),
                    "risk_level": getattr(action.risk_level, "value", None)
                    if action.risk_level
                    else None,
                    "execution_mode": getattr(
                        action.execution_mode, "value", str(action.execution_mode)
                    ),
                    "component_id": str(action.component_id)
                    if action.component_id
                    else None,
                    "created_at": _aware(action.created_at).isoformat()
                    if _aware(action.created_at)
                    else None,
                }
            )

    # -- recent changes
    if wants("recent_changes"):
        from app.services.change_intelligence import recent_changes

        state.recent_changes = await recent_changes(
            session,
            project_id=project_id,
            environment_id=environment_id,
            since=moment - timedelta(days=settings.PLATFORM_STATE_CHANGE_WINDOW_DAYS),
            limit=settings.PLATFORM_STATE_ACTIVE_LIMIT,
        )

    # -- reliability patterns (Phase 10 knowledge, read-only)
    if wants("reliability_patterns"):
        try:
            from app.models.intelligence import (
                KnowledgeStatus,
                ReliabilityKnowledge,
            )

            pattern_stmt = (
                select(ReliabilityKnowledge)
                .where(
                    ReliabilityKnowledge.project_id == project_id,
                    ReliabilityKnowledge.status.in_(
                        [KnowledgeStatus.ACTIVE, KnowledgeStatus.VALIDATED]
                    ),
                )
                .order_by(ReliabilityKnowledge.sample_count.desc())
                .limit(20)
            )
            for knowledge in (await session.scalars(pattern_stmt)).all():
                state.reliability_patterns.append(
                    {
                        "id": str(knowledge.id),
                        "knowledge_type": getattr(
                            knowledge.knowledge_type,
                            "value",
                            str(knowledge.knowledge_type),
                        ),
                        "title": getattr(knowledge, "title", None),
                        "confidence": getattr(knowledge.confidence, "value", None)
                        if knowledge.confidence
                        else None,
                        "sample_count": knowledge.sample_count,
                    }
                )
        except Exception:  # pragma: no cover - learning is optional (§59)
            state.limitations.append(
                "reliability patterns unavailable: the learning layer did not answer"
            )

    # -- data quality summary
    if wants("data_quality"):
        from app.services.data_quality_center import quality_summary

        state.data_quality = await quality_summary(session, project_id=project_id)

    # -- health roll-up + honest limitations
    counts = state.state_counts()
    total = len(state.components)
    known = total - counts.get(ComponentOperationalState.UNKNOWN.value, 0)
    state.health = {
        "components_total": total,
        "components_with_evidence": known,
        "components_unknown": counts.get(ComponentOperationalState.UNKNOWN.value, 0),
        "state_counts": counts,
        "coverage_percent": round((known / total) * 100.0, 1) if total else 0.0,
    }
    if total == 0:
        state.limitations.append(
            "no components are registered in this scope; nothing can be stated "
            "about its reliability"
        )
    elif known == 0:
        state.limitations.append(
            "no component in this scope has telemetry inside the lookback window"
        )
    if len(components) >= settings.PLATFORM_STATE_COMPONENT_LIMIT:
        state.limitations.append(
            f"component list truncated at {settings.PLATFORM_STATE_COMPONENT_LIMIT}"
        )
    return state


__all__ = [
    "ComponentSignals",
    "ComponentStateResult",
    "ELEVATED_RISK_LEVELS",
    "StateEvidence",
    "SystemState",
    "collect_signals",
    "current_states",
    "derive_component_states",
    "derive_state",
    "build_system_state",
    "record_transitions",
    "state_at",
    "state_history",
]
