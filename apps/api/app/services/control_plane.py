"""ARGUS Reliability Control Plane (Phase 11 §6, §10, §19–§25).

The orchestration layer. It does four things, and refuses to do anything else:

1. **Aggregates system state** by delegating to :mod:`app.services.system_state`.
2. **Correlates events into one story per situation** (§10) by consuming the
   platform event stream and writing to case timelines.
3. **Maintains the case as the operational object** (§14) — opening cases where
   something is worth working on, mirroring what other phases concluded onto the
   case timeline, and never editing another phase's rows.
4. **Answers the dashboard's questions** (§19–§25) by composing the phase services
   rather than reimplementing them.

The one architectural instruction this module follows literally is §6's "do not
put all business logic into one enormous service". Every capability lives in its
own module — state derivation, SLOs, change intelligence, health, quality,
reports — and this file is the *conductor*: it sequences them and exposes the
composed answers. If a function here is longer than the query it runs, that is a
smell that logic belongs in a specialized service.

Nothing here authorizes, executes or remediates. A case can be moved to
``AUTHORIZED`` only because Phase 9 already authorized an action; the control
plane has no path to Phase 9's gates.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.services.platform_time import (
    aware as _aware,
    elapsed_seconds,
    optional_stamp,
)
from app.models.platform import (
    CaseStatus,
    CaseTrigger,
    DataQualityStatus,
    PlatformEvent,
    PlatformEventType,
    ReliabilityCase,
    ReliabilityWorkflow,
    TimelineEntryKind,
    WorkflowStage,
)
from app.models.project import Environment, ProjectStatus, SoftwareProject

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# §10 — event correlation
# ---------------------------------------------------------------------------
@dataclass
class CorrelationResult:
    """What one pass of event correlation did."""

    consumed: int = 0
    cases_opened: int = 0
    timeline_entries: int = 0
    notifications: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "consumed": self.consumed,
            "cases_opened": self.cases_opened,
            "timeline_entries": self.timeline_entries,
            "notifications": self.notifications,
            "errors": list(self.errors),
        }


#: Which case a platform event belongs to is decided by the situation it is
#: about, in this order. Declared as data so the rule is inspectable.
def _case_anchor(event: PlatformEvent) -> Optional[tuple[str, uuid.UUID]]:
    if event.case_id is not None:
        return ("case", event.case_id)
    if event.event_type in (
        PlatformEventType.INCIDENT_CREATED,
        PlatformEventType.INCIDENT_UPDATED,
        PlatformEventType.RCA_COMPLETED,
        PlatformEventType.REPRODUCTION_COMPLETED,
        PlatformEventType.PATCH_VERIFIED,
        PlatformEventType.REMEDIATION_PROPOSED,
        PlatformEventType.REMEDIATION_STARTED,
        PlatformEventType.REMEDIATION_COMPLETED,
        PlatformEventType.REMEDIATION_ROLLED_BACK,
    ):
        if event.subject_type == "incident" and event.subject_id:
            return ("incident", event.subject_id)
    return None


#: Timeline kind per event type — the mapping the unified timeline depends on.
EVENT_KIND: dict[PlatformEventType, TimelineEntryKind] = {
    PlatformEventType.COMPONENT_STATE_CHANGED: TimelineEntryKind.STATE_CHANGE,
    PlatformEventType.ANOMALY_DETECTED: TimelineEntryKind.EVIDENCE,
    PlatformEventType.INCIDENT_CREATED: TimelineEntryKind.EVIDENCE,
    PlatformEventType.INCIDENT_UPDATED: TimelineEntryKind.EVIDENCE,
    PlatformEventType.RCA_COMPLETED: TimelineEntryKind.ANALYSIS,
    PlatformEventType.REPRODUCTION_COMPLETED: TimelineEntryKind.ANALYSIS,
    PlatformEventType.PATCH_VERIFIED: TimelineEntryKind.VERIFICATION,
    PlatformEventType.FORECAST_GENERATED: TimelineEntryKind.PREDICTION,
    PlatformEventType.RISK_CHANGED: TimelineEntryKind.PREDICTION,
    PlatformEventType.REMEDIATION_PROPOSED: TimelineEntryKind.RECOMMENDATION,
    PlatformEventType.REMEDIATION_STARTED: TimelineEntryKind.EXECUTION,
    PlatformEventType.REMEDIATION_COMPLETED: TimelineEntryKind.RECOVERY,
    PlatformEventType.REMEDIATION_ROLLED_BACK: TimelineEntryKind.EXECUTION,
    PlatformEventType.LEARNING_COMPLETED: TimelineEntryKind.LEARNING,
    PlatformEventType.DEPLOYMENT_RECORDED: TimelineEntryKind.EVIDENCE,
    PlatformEventType.SLO_STATUS_CHANGED: TimelineEntryKind.EVIDENCE,
    PlatformEventType.ERROR_BUDGET_BURN: TimelineEntryKind.EVIDENCE,
    PlatformEventType.DATA_QUALITY_ISSUE: TimelineEntryKind.DATA_QUALITY,
    PlatformEventType.CONFIGURATION_CHANGED: TimelineEntryKind.DECISION,
    PlatformEventType.NOTIFICATION_RAISED: TimelineEntryKind.NOTE,
}

#: Human-readable verb per event type, so the timeline reads as sentences.
EVENT_TITLE: dict[PlatformEventType, str] = {
    PlatformEventType.COMPONENT_STATE_CHANGED: "Component state changed",
    PlatformEventType.ANOMALY_DETECTED: "Anomaly detected",
    PlatformEventType.INCIDENT_CREATED: "Incident opened",
    PlatformEventType.INCIDENT_UPDATED: "Incident updated",
    PlatformEventType.RCA_COMPLETED: "Root cause analysis completed",
    PlatformEventType.REPRODUCTION_COMPLETED: "Failure reproduction finished",
    PlatformEventType.PATCH_VERIFIED: "Patch verification finished",
    PlatformEventType.FORECAST_GENERATED: "Risk forecast generated",
    PlatformEventType.RISK_CHANGED: "Predicted risk changed",
    PlatformEventType.REMEDIATION_PROPOSED: "Remediation proposed",
    PlatformEventType.REMEDIATION_STARTED: "Remediation started",
    PlatformEventType.REMEDIATION_COMPLETED: "Remediation completed",
    PlatformEventType.REMEDIATION_ROLLED_BACK: "Remediation rolled back",
    PlatformEventType.LEARNING_COMPLETED: "Learning completed",
    PlatformEventType.DEPLOYMENT_RECORDED: "Deployment recorded",
    PlatformEventType.SLO_STATUS_CHANGED: "Objective status changed",
    PlatformEventType.ERROR_BUDGET_BURN: "Error budget burn detected",
    PlatformEventType.DATA_QUALITY_ISSUE: "Data quality issue found",
    PlatformEventType.CASE_OPENED: "Case opened",
    PlatformEventType.CASE_CLOSED: "Case closed",
    PlatformEventType.CASE_STATUS_CHANGED: "Case status changed",
    PlatformEventType.CONFIGURATION_CHANGED: "Configuration changed",
    PlatformEventType.NOTIFICATION_RAISED: "Notification raised",
}


async def correlate_events(
    session: AsyncSession,
    *,
    limit: int = 200,
    project_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> CorrelationResult:
    """Consume pending events into case timelines (§10).

    Every event is *attributed* to the case it belongs to, its timeline entry is
    appended, and the event is marked processed. Events that belong to no case
    (a project-level forecast, a data-quality finding) are marked processed too:
    leaving them pending would make the control plane re-examine them forever,
    and "unrelated to any open situation" is a conclusion, not a backlog.
    """
    from app.services.platform_events import mark_processed, pending_events
    from app.services.reliability_case import append_timeline

    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    result = CorrelationResult()
    events = await pending_events(session, limit=limit, project_id=project_id)
    processed: list[uuid.UUID] = []

    for event in events:
        result.consumed += 1
        processed.append(event.id)
        if event.project_id is None:
            continue
        anchor = _case_anchor(event)
        case: Optional[ReliabilityCase] = None
        if anchor is not None:
            kind, subject_id = anchor
            if kind == "case":
                case = await session.get(ReliabilityCase, subject_id)
            else:
                from app.services.reliability_case import find_open_case_for_incident

                case = await find_open_case_for_incident(
                    session, incident_id=subject_id
                )
                if case is None and settings.PLATFORM_AUTO_CASE_ENABLED:
                    from app.models.incident import Incident

                    incident = await session.get(Incident, subject_id)
                    if incident is not None:
                        case = await ensure_case_for_incident(
                            session, incident=incident, settings=settings, now=moment
                        )
                        if case is not None:
                            result.cases_opened += 1
        if case is None:
            continue
        if case.project_id != event.project_id:
            continue
        entry = await append_timeline(
            session,
            case=case,
            kind=EVENT_KIND.get(event.event_type, TimelineEntryKind.EVIDENCE),
            event_type=event.event_type.value,
            title=EVENT_TITLE.get(event.event_type, event.event_type.value),
            detail=_event_detail(event),
            source=event.source,
            evidence={
                "event_id": str(event.id),
                "subject_type": event.subject_type,
                "subject_id": str(event.subject_id) if event.subject_id else None,
                "correlation_id": event.correlation_id,
                "payload": event.payload,
            },
            component_id=event.component_id,
            system_action=True,
            result=_event_result(event),
            occurred_at=_aware(event.occurred_at),
            dedup_key=f"event:{event.id}",
        )
        if entry is not None:
            result.timeline_entries += 1

    if processed:
        await mark_processed(
            session, event_ids=processed, consumer="control_plane", now=moment
        )
    return result


def _event_detail(event: PlatformEvent) -> Optional[str]:
    payload = event.payload or {}
    for key in ("title", "summary", "message", "detail", "description", "reason"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _event_result(event: PlatformEvent) -> Optional[str]:
    payload = event.payload or {}
    for key in ("status", "result", "state", "risk_level", "outcome", "verdict"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


# ---------------------------------------------------------------------------
# §14 — case maintenance
# ---------------------------------------------------------------------------
async def ensure_case_for_incident(
    session: AsyncSession,
    *,
    incident: Any,
    settings: Optional[Settings] = None,
    now: Optional[datetime] = None,
    opened_by: Optional[str] = None,
) -> Optional[ReliabilityCase]:
    """Open a case for an incident that does not have one yet (§14).

    Idempotent: the live case for the incident is returned if it exists. A case is
    only opened for an incident that is actually worth working — CRITICAL, HIGH,
    or any unresolved incident whose component is DEGRADED — because one case per
    informational blip would drown the real ones.
    """
    from app.services.reliability_case import (
        find_open_case_for_incident,
        open_case,
    )

    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    existing = await find_open_case_for_incident(session, incident_id=incident.id)
    if existing is not None:
        return existing

    severity = getattr(incident.severity, "value", str(incident.severity))
    status = getattr(incident.status, "value", str(incident.status))
    if status in ("RESOLVED", "CLOSED"):
        return None
    if severity not in ("CRITICAL", "HIGH") and status == "OPEN":
        return None

    component_ids: list[uuid.UUID] = []
    if incident.primary_component_id:
        component_ids.append(incident.primary_component_id)
    from app.models.anomaly import Anomaly

    linked = (
        await session.scalars(
            select(Anomaly.component_id).where(
                Anomaly.incident_id == incident.id,
                Anomaly.component_id.is_not(None),
            )
        )
    ).all()
    for component_id in linked:
        #: The query filters ``component_id IS NOT NULL``, but the declared column
        #: is nullable — so the None is dropped here rather than asserted away.
        if component_id is not None and component_id not in component_ids:
            component_ids.append(component_id)

    case = await open_case(
        session,
        project_id=incident.project_id,
        trigger=CaseTrigger.INCIDENT,
        title=incident.title,
        environment_id=incident.environment_id,
        summary=incident.summary
        or f"Incident {severity} detected at {_aware(incident.detected_at).isoformat()}",
        severity=severity,
        incident_id=incident.id,
        primary_component_id=incident.primary_component_id,
        component_ids=component_ids,
        source_type="incident",
        source_id=incident.id,
        opened_by=opened_by,
        opened_at=moment,
    )
    await _start_default_workflow(session, case=case, settings=settings, now=moment)
    return case


async def _start_default_workflow(
    session: AsyncSession,
    *,
    case: ReliabilityCase,
    settings: Settings,
    now: datetime,
) -> Optional[ReliabilityWorkflow]:
    """Give a new case a workflow, unless one is already running (§11)."""
    from app.services.reliability_workflow import (
        active_workflow_for_case,
        start_workflow,
    )

    running = await active_workflow_for_case(session, case_id=case.id)
    if running is not None:
        return running
    return await start_workflow(
        session,
        project_id=case.project_id,
        case=case,
        stage=WorkflowStage.DETECTED,
        triggered_by="control_plane",
        now=now,
        settings=settings,
    )


async def open_case_for_forecast(
    session: AsyncSession,
    *,
    forecast: Any,
    settings: Optional[Settings] = None,
    now: Optional[datetime] = None,
) -> Optional[ReliabilityCase]:
    """Open a case for a high-risk prediction (§14 ``FORECAST`` trigger).

    A prediction is not an incident, and the case says so: its trigger is
    ``FORECAST`` and its summary states that nothing has failed yet.
    """
    from app.models.reliability import ForecastRiskLevel, ForecastStatus
    from app.services.reliability_case import (
        LIVE_CASE_STATUSES,
        open_case,
    )

    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    if getattr(forecast.risk_level, "value", None) not in (
        ForecastRiskLevel.HIGH.value,
        ForecastRiskLevel.CRITICAL.value,
    ):
        return None
    if getattr(forecast.status, "value", None) not in (
        ForecastStatus.GENERATED.value,
        ForecastStatus.ACTIVE.value,
    ):
        return None

    existing = (
        await session.scalars(
            select(ReliabilityCase).where(
                ReliabilityCase.project_id == forecast.project_id,
                ReliabilityCase.source_type == "forecast",
                ReliabilityCase.source_id == forecast.id,
                ReliabilityCase.status.in_(list(LIVE_CASE_STATUSES)),
            )
        )
    ).first()
    if existing is not None:
        return existing

    component_name = None
    if forecast.component_id is not None:
        from app.models.system import SystemComponent

        component = await session.get(SystemComponent, forecast.component_id)
        component_name = component.name if component else None

    case = await open_case(
        session,
        project_id=forecast.project_id,
        trigger=CaseTrigger.FORECAST,
        title=(
            "Elevated predicted risk"
            + (f" on {component_name}" if component_name else "")
            + f" ({getattr(forecast.risk_level, 'value', forecast.risk_level)})"
        ),
        environment_id=forecast.environment_id,
        summary=(
            "a reliability forecast predicts elevated risk. Nothing has failed: "
            "this case exists so the prediction can be watched, and it is closed "
            "without action if the window passes quietly."
        ),
        severity=getattr(forecast.risk_level, "value", None),
        primary_component_id=forecast.component_id,
        component_ids=[forecast.component_id] if forecast.component_id else [],
        source_type="forecast",
        source_id=forecast.id,
        opened_at=moment,
        metadata={
            "risk_score": forecast.risk_score,
            "confidence": forecast.confidence,
            "prediction_type": getattr(
                forecast.prediction_type, "value", str(forecast.prediction_type)
            ),
        },
    )
    await _start_default_workflow(session, case=case, settings=settings, now=moment)
    return case


async def open_case_for_slo_burn(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    slo: Any,
    snapshot: Any,
    settings: Optional[Settings] = None,
    now: Optional[datetime] = None,
) -> ReliabilityCase:
    """Open a case for an error-budget burn (§36).

    §36 is explicit that a burn plus a rising error rate plus a recent deployment
    is a *reliability signal*, not a cause. The case therefore states the three
    facts and refuses to connect them.
    """
    from app.services.reliability_case import LIVE_CASE_STATUSES, open_case

    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    existing = (
        await session.scalars(
            select(ReliabilityCase).where(
                ReliabilityCase.project_id == project_id,
                ReliabilityCase.source_type == "slo",
                ReliabilityCase.source_id == slo.id,
                ReliabilityCase.status.in_(list(LIVE_CASE_STATUSES)),
            )
        )
    ).first()
    if existing is not None:
        return existing

    burn_state = getattr(
        getattr(snapshot, "burn_state", None), "value", str(snapshot.burn_state)
    )
    case = await open_case(
        session,
        project_id=project_id,
        trigger=CaseTrigger.SLO_BURN,
        title=f"Error budget burn ({burn_state}) on '{slo.name}'",
        environment_id=slo.environment_id,
        summary=(
            f"the error budget for '{slo.name}' is being consumed faster than "
            f"planned (burn state {burn_state}, rate "
            f"{snapshot.burn_rate if snapshot.burn_rate is not None else 'unknown'}). "
            "This is a reliability signal: ARGUS does not claim a cause."
        ),
        severity=("CRITICAL" if burn_state == "CRITICAL_BURN" else "HIGH"),
        primary_component_id=slo.component_id,
        component_ids=[slo.component_id] if slo.component_id else [],
        source_type="slo",
        source_id=slo.id,
        opened_at=moment,
        metadata={
            "burn_rate": snapshot.burn_rate,
            "burn_state": burn_state,
            "remaining_percent": snapshot.remaining_percent,
            "slo_status": getattr(snapshot.status, "value", str(snapshot.status)),
        },
    )
    await _start_default_workflow(session, case=case, settings=settings, now=moment)
    return case


async def sync_case_with_phases(
    session: AsyncSession,
    *,
    case: ReliabilityCase,
    now: Optional[datetime] = None,
) -> list[str]:
    """Mirror what the phases concluded onto a case's timeline (§10, §15).

    A pull-based complement to the push-based event stream: because events are
    best-effort, this pass guarantees a case eventually reflects the analyses,
    reproductions, patches, forecasts and remediations that exist for it, even if
    an event was lost. Returns the timeline entries it added.
    """
    from app.services.reliability_case import append_timeline

    moment = _aware(now) or datetime.now(timezone.utc)
    added: list[str] = []
    if case.incident_id is None:
        return added

    from app.models.causal import AnalysisStatus, CausalAnalysis
    from app.models.fix import Patch, PatchStatus
    from app.models.remediation import RemediationAction
    from app.models.reproduction import ExperimentStatus, ReproductionExperiment

    analyses = (
        await session.scalars(
            select(CausalAnalysis).where(
                CausalAnalysis.incident_id == case.incident_id,
                #: ``AnalysisStatus`` has no PARTIAL: a run either finished or it
                #: did not, and a half-written analysis is reported by its absence.
                CausalAnalysis.status.in_([AnalysisStatus.COMPLETED]),
            )
        )
    ).all()
    for analysis in analyses:
        entry = await append_timeline(
            session,
            case=case,
            kind=TimelineEntryKind.ANALYSIS,
            event_type="RCA_COMPLETED",
            title="Root cause analysis completed",
            detail=getattr(analysis, "summary", None),
            source="causal_analysis",
            evidence={"analysis_id": str(analysis.id)},
            occurred_at=getattr(analysis, "completed_at", None) or moment,
            dedup_key=f"analysis:{analysis.id}",
        )
        if entry is not None:
            added.append("analysis")

    experiments = (
        await session.scalars(
            select(ReproductionExperiment).where(
                ReproductionExperiment.incident_id == case.incident_id,
                ReproductionExperiment.status.in_(
                    [
                        ExperimentStatus.COMPLETED,
                        ExperimentStatus.FAILED,
                        ExperimentStatus.TIMED_OUT,
                    ]
                ),
            )
        )
    ).all()
    for experiment in experiments:
        entry = await append_timeline(
            session,
            case=case,
            kind=TimelineEntryKind.ANALYSIS,
            event_type="REPRODUCTION_COMPLETED",
            title="Failure reproduction finished",
            detail=(
                f"result: "
                f"{getattr(getattr(experiment, 'result', None), 'value', 'unknown')}"
            ),
            source="reproduction",
            evidence={
                "experiment_id": str(experiment.id),
                "result": getattr(getattr(experiment, "result", None), "value", None),
                "confidence": getattr(experiment, "confidence", None),
            },
            result=getattr(getattr(experiment, "result", None), "value", None),
            occurred_at=getattr(experiment, "completed_at", None) or moment,
            dedup_key=f"experiment:{experiment.id}",
        )
        if entry is not None:
            added.append("reproduction")

    from app.models.fix import FixHypothesis

    patches = (
        await session.scalars(
            select(Patch)
            .join(FixHypothesis, FixHypothesis.id == Patch.fix_hypothesis_id)
            .where(
                FixHypothesis.incident_id == case.incident_id,
                Patch.status.in_(
                    [
                        PatchStatus.VERIFIED,
                        PatchStatus.BUILD_FAILED,
                        PatchStatus.TEST_FAILED,
                    ]
                ),
            )
        )
    ).all()
    for patch in patches:
        entry = await append_timeline(
            session,
            case=case,
            kind=TimelineEntryKind.VERIFICATION,
            event_type="PATCH_VERIFIED",
            title=f"Patch verification {getattr(patch.status, 'value', '')}",
            source="fix_verification",
            evidence={"patch_id": str(patch.id), "changed_files": patch.changed_files},
            result=getattr(patch.status, "value", None),
            occurred_at=getattr(patch, "updated_at", None) or moment,
            dedup_key=f"patch:{patch.id}",
        )
        if entry is not None:
            added.append("patch")

    actions = (
        await session.scalars(
            select(RemediationAction).where(
                RemediationAction.incident_id == case.incident_id
            )
        )
    ).all()
    for action in actions:
        entry = await append_timeline(
            session,
            case=case,
            kind=TimelineEntryKind.EXECUTION,
            event_type="REMEDIATION_RECORDED",
            title=(
                f"Remediation {getattr(action.action_type, 'value', '')} "
                f"({getattr(action.status, 'value', '')})"
            ),
            source="remediation",
            evidence={
                "action_id": str(action.id),
                "execution_mode": getattr(
                    action.execution_mode, "value", str(action.execution_mode)
                ),
                "outcome": getattr(getattr(action, "outcome", None), "value", None),
            },
            result=getattr(action.status, "value", None),
            occurred_at=getattr(action, "completed_at", None)
            or getattr(action, "created_at", None)
            or moment,
            dedup_key=f"remediation:{action.id}",
        )
        if entry is not None:
            added.append("remediation")
    return added


# ---------------------------------------------------------------------------
# §19–§25 — the dashboard
# ---------------------------------------------------------------------------
@dataclass
class Overview:
    """The unified dashboard payload (§19, §20, §22, §23)."""

    project_id: uuid.UUID
    as_of: datetime
    executive_summary: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    state_counts: dict[str, int] = field(default_factory=dict)
    active_incidents: list[dict[str, Any]] = field(default_factory=list)
    predicted_risks: list[dict[str, Any]] = field(default_factory=list)
    active_remediations: list[dict[str, Any]] = field(default_factory=list)
    recent_changes: list[dict[str, Any]] = field(default_factory=list)
    top_risky_components: list[dict[str, Any]] = field(default_factory=list)
    recent_recoveries: list[dict[str, Any]] = field(default_factory=list)
    learning_insights: dict[str, Any] = field(default_factory=dict)
    argus_health: dict[str, Any] = field(default_factory=dict)
    data_quality: dict[str, Any] = field(default_factory=dict)
    open_cases: list[dict[str, Any]] = field(default_factory=list)
    slo: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": str(self.project_id),
            "as_of": self.as_of.isoformat(),
            "executive_summary": self.executive_summary,
            "health": self.health,
            "state_counts": self.state_counts,
            "active_incidents": self.active_incidents,
            "predicted_risks": self.predicted_risks,
            "active_remediations": self.active_remediations,
            "recent_changes": self.recent_changes,
            "top_risky_components": self.top_risky_components,
            "recent_recoveries": self.recent_recoveries,
            "learning_insights": self.learning_insights,
            "argus_health": self.argus_health,
            "data_quality": self.data_quality,
            "open_cases": self.open_cases,
            "slo": self.slo,
            "limitations": list(self.limitations),
        }


async def build_overview(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> Overview:
    """Compose the unified dashboard (§19, §20).

    Sections come from the services that own them; this function sequences them
    and states the *executive summary* (§23) in the same place, because a summary
    assembled in the UI is a summary that disagrees with the page it sits on.
    """
    from app.services.data_quality_center import quality_summary
    from app.services.platform_health import platform_health
    from app.services.reliability_case import case_summary, list_cases
    from app.services.reliability_case import LIVE_CASE_STATUSES
    from app.services.slo_service import slo_overview
    from app.services.system_state import build_system_state

    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    state = await build_system_state(
        session,
        project_id=project_id,
        environment_id=environment_id,
        now=moment,
        settings=settings,
    )

    overview = Overview(
        project_id=project_id,
        as_of=moment,
        health=state.health,
        state_counts=state.state_counts(),
        active_incidents=state.active_incidents,
        predicted_risks=state.predicted_risks,
        active_remediations=state.active_remediations,
        recent_changes=state.recent_changes,
        limitations=list(state.limitations),
    )

    # -- top risky components: predicted risk first, then live state
    risk_rank = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}
    state_rank = {"INCIDENT": 5, "RECOVERING": 4, "DEGRADED": 3, "AT_RISK": 2}
    by_component: dict[str, dict[str, Any]] = {}
    for component in state.components:
        by_component[component["id"]] = {
            "component_id": component["id"],
            "name": component["name"],
            "state": component["state"],
            "predicted_risk": None,
            "reasons": [component.get("state_reason")],
        }
    for risk in state.predicted_risks:
        key = risk.get("component_id")
        if key and key in by_component:
            by_component[key]["predicted_risk"] = risk.get("risk_level")
    ranked = sorted(
        by_component.values(),
        key=lambda item: (
            state_rank.get(item["state"], 0),
            risk_rank.get(item["predicted_risk"] or "UNKNOWN", 0),
        ),
        reverse=True,
    )
    overview.top_risky_components = [
        item for item in ranked if item["state"] != "HEALTHY"
    ][:10]

    # -- recent recoveries
    from app.models.incident import Incident, IncidentStatus

    recoveries = (
        await session.scalars(
            select(Incident)
            .where(
                Incident.project_id == project_id,
                Incident.status.in_([IncidentStatus.RESOLVED, IncidentStatus.CLOSED]),
                Incident.resolved_at.is_not(None),
                Incident.resolved_at >= moment - timedelta(days=7),
            )
            .order_by(Incident.resolved_at.desc())
            .limit(10)
        )
    ).all()
    overview.recent_recoveries = [
        {
            "id": str(incident.id),
            "title": incident.title,
            "severity": getattr(incident.severity, "value", str(incident.severity)),
            #: A recovery list only contains resolved incidents, but the column is
            #: nullable — ``optional_stamp`` keeps the type honest instead of
            #: assuming, and a NULL still renders as ``null``.
            "resolved_at": optional_stamp(incident.resolved_at),
            "duration_seconds": elapsed_seconds(
                incident.detected_at, incident.resolved_at
            ),
        }
        for incident in recoveries
    ]

    # -- open cases
    cases = await list_cases(
        session, project_id=project_id, statuses=LIVE_CASE_STATUSES, limit=10
    )
    overview.open_cases = [await case_summary(session, case=case) for case in cases]

    # -- learning insights (read-only, optional)
    try:
        from app.models.intelligence import KnowledgeStatus, ReliabilityKnowledge

        active_knowledge = await session.scalar(
            select(func.count(ReliabilityKnowledge.id)).where(
                ReliabilityKnowledge.project_id == project_id,
                ReliabilityKnowledge.status == KnowledgeStatus.ACTIVE,
            )
        )
        overview.learning_insights = {
            "active_patterns": active_knowledge or 0,
            "available": True,
        }
    except Exception:  # pragma: no cover - learning is optional (§59)
        overview.learning_insights = {
            "active_patterns": None,
            "available": False,
            "reason": "the learning layer did not answer",
        }

    # -- argus's own health
    health = await platform_health(session, settings=settings, now=moment)
    overview.argus_health = health.as_dict()

    # -- data quality + SLO
    overview.data_quality = await quality_summary(session, project_id=project_id)
    try:
        overview.slo = await slo_overview(
            session, project_id=project_id, settings=settings
        )
    except Exception:  # pragma: no cover - SLOs are optional
        logger.warning("SLO overview unavailable", exc_info=True)
        overview.slo = {"objectives": 0, "limitations": ["SLO summary failed"]}

    # -- §23 executive summary, generated dynamically
    overview.executive_summary = {
        "components_monitored": overview.health.get("components_total", 0),
        "components_with_evidence": overview.health.get("components_with_evidence", 0),
        "active_incidents": len(overview.active_incidents),
        "components_at_elevated_risk": len(
            [
                risk
                for risk in overview.predicted_risks
                if risk.get("risk_level") in ("HIGH", "CRITICAL")
            ]
        ),
        "remediations_executing": len(overview.active_remediations),
        "recent_recoveries": len(overview.recent_recoveries),
        "open_cases": len(overview.open_cases),
        "open_data_quality_issues": overview.data_quality.get("open_issues", 0),
        "headline": _headline(overview),
    }
    return overview


def _headline(overview: Overview) -> str:
    summary = overview.executive_summary
    parts = [f"{summary.get('components_monitored', 0)} components monitored"]
    incidents = summary.get("active_incidents", 0)
    if incidents:
        parts.append(f"{incidents} active incident(s)")
    risky = summary.get("components_at_elevated_risk", 0)
    if risky:
        parts.append(f"{risky} component(s) at elevated predicted risk")
    executing = summary.get("remediations_executing", 0)
    if executing:
        parts.append(f"{executing} remediation(s) in flight")
    recoveries = summary.get("recent_recoveries", 0)
    if recoveries:
        parts.append(f"{recoveries} recent recovery(ies)")
    unknown = overview.state_counts.get("UNKNOWN", 0)
    if unknown:
        parts.append(f"{unknown} component(s) with no evidence")
    return ", ".join(parts)


async def engineering_summary(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """The §24 engineering view: depth a person needs, not a headline."""
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    from app.services.change_intelligence import change_failure_rate, recent_changes
    from app.services.platform_metrics import time_to_recover_breakdown

    summary: dict[str, Any] = {}

    # -- top anomalies in the last day
    from app.models.anomaly import Anomaly, AnomalyStatus

    anomalies = (
        await session.scalars(
            select(Anomaly)
            .where(
                Anomaly.project_id == project_id,
                Anomaly.status.notin_([AnomalyStatus.RESOLVED, AnomalyStatus.EXPIRED]),
                Anomaly.detected_at >= moment - timedelta(days=1),
            )
            .order_by(Anomaly.severity.desc(), Anomaly.detected_at.desc())
            .limit(10)
        )
    ).all()
    summary["top_anomalies"] = [
        {
            "id": str(anomaly.id),
            "anomaly_type": getattr(
                anomaly.anomaly_type, "value", str(anomaly.anomaly_type)
            ),
            "severity": getattr(anomaly.severity, "value", str(anomaly.severity)),
            "component_id": str(anomaly.component_id) if anomaly.component_id else None,
            "metric_name": anomaly.metric_name,
            "detected_at": _aware(anomaly.detected_at).isoformat(),
        }
        for anomaly in anomalies
    ]

    # -- top dependencies by incident association (read-only, bounded)
    from app.models.incident import Incident
    from app.models.system import ComponentDependency, SystemComponent

    project_components = (
        await session.scalars(
            select(SystemComponent.id).where(SystemComponent.project_id == project_id)
        )
    ).all()
    dependencies = (
        await session.scalars(
            select(ComponentDependency)
            .where(
                #: No project column on this table: components carry ownership (§42).
                ComponentDependency.source_component_id.in_(project_components),
                ComponentDependency.target_component_id.in_(project_components),
            )
            .limit(settings.PLATFORM_STATE_DEPENDENCY_LIMIT)
        )
    ).all()
    target_counts = (
        await session.execute(
            select(Incident.primary_component_id, func.count())
            .where(Incident.project_id == project_id)
            .group_by(Incident.primary_component_id)
        )
    ).all()
    incident_by_component = {row[0]: row[1] for row in target_counts}
    ranked_deps = sorted(
        (
            {
                "source_component_id": str(dependency.source_component_id),
                "target_component_id": str(dependency.target_component_id),
                "target_incidents": incident_by_component.get(
                    dependency.target_component_id, 0
                ),
            }
            for dependency in dependencies
        ),
        key=lambda item: item["target_incidents"],
        reverse=True,
    )
    summary["top_dependencies"] = ranked_deps[:10]

    # -- RCA confidence distribution
    from app.models.causal import AnalysisStatus, CausalAnalysis, RootCauseCandidate

    analysis_rows = (
        await session.scalars(
            select(CausalAnalysis).where(
                CausalAnalysis.project_id == project_id,
                CausalAnalysis.status == AnalysisStatus.COMPLETED,
            )
        )
    ).all()
    confidence_buckets: dict[str, int] = {}
    for analysis in analysis_rows:
        candidate = (
            await session.scalars(
                select(RootCauseCandidate)
                .where(RootCauseCandidate.analysis_id == analysis.id)
                .order_by(RootCauseCandidate.score.desc())
                .limit(1)
            )
        ).first()
        if candidate is None:
            continue
        bucket = getattr(candidate.confidence, "value", "UNKNOWN")
        confidence_buckets[bucket] = confidence_buckets.get(bucket, 0) + 1
    summary["rca_confidence"] = {
        "completed_analyses": len(analysis_rows),
        "by_confidence": confidence_buckets,
    }

    # -- reproduction + patch verification
    from app.models.reproduction import ReproductionExperiment

    repro_rows = (
        await session.execute(
            select(ReproductionExperiment.status, func.count())
            .where(ReproductionExperiment.project_id == project_id)
            .group_by(ReproductionExperiment.status)
        )
    ).all()
    summary["reproduction_status"] = {
        getattr(status, "value", str(status)): int(count)
        for status, count in repro_rows
    }
    from app.models.fix import Patch

    patch_rows = (
        await session.execute(
            select(Patch.status, func.count())
            .where(Patch.project_id == project_id)
            .group_by(Patch.status)
        )
    ).all()
    summary["patch_verification"] = {
        getattr(status, "value", str(status)): int(count)
        for status, count in patch_rows
    }

    # -- predictions and their measured accuracy
    try:
        from app.models.reliability import ForecastOutcome, PredictionOutcomeType

        outcome_rows = (
            await session.execute(
                select(ForecastOutcome.outcome, func.count())
                .where(ForecastOutcome.project_id == project_id)
                .group_by(ForecastOutcome.outcome)
            )
        ).all()
        counts = {
            getattr(outcome, "value", str(outcome)): int(count)
            for outcome, count in outcome_rows
        }
        answered = sum(
            counts.get(key, 0)
            for key in (
                PredictionOutcomeType.TRUE_POSITIVE.value,
                PredictionOutcomeType.FALSE_POSITIVE.value,
            )
        )
        summary["prediction_accuracy"] = {
            "by_outcome": counts,
            "precision": (
                round(
                    counts.get(PredictionOutcomeType.TRUE_POSITIVE.value, 0) / answered,
                    4,
                )
                if answered
                else None
            ),
            "evaluated": answered,
            "methodology": (
                "precision = TRUE_POSITIVE / (TRUE_POSITIVE + FALSE_POSITIVE): the "
                "share of raised alerts that were followed by the predicted "
                "failure. TRUE_NEGATIVE and INCONCLUSIVE are excluded because "
                "they are not alerts."
            ),
        }
    except Exception:  # pragma: no cover - forecasting is optional (§59)
        summary["prediction_accuracy"] = {
            "by_outcome": {},
            "precision": None,
            "evaluated": 0,
            "reason": "prediction evaluation did not answer",
        }

    # -- remediations
    from app.models.remediation import RemediationAction

    remediation_rows = (
        await session.execute(
            select(RemediationAction.outcome, func.count())
            .where(
                RemediationAction.project_id == project_id,
                RemediationAction.outcome.is_not(None),
            )
            .group_by(RemediationAction.outcome)
        )
    ).all()
    summary["remediation_history"] = {
        getattr(outcome, "value", str(outcome)): int(count)
        for outcome, count in remediation_rows
    }

    # -- changes and delivery metrics
    summary["recent_changes"] = await recent_changes(
        session, project_id=project_id, since=moment - timedelta(days=7), limit=20
    )
    summary["change_failure_rate"] = (
        await change_failure_rate(session, project_id=project_id, settings=settings)
    ).as_dict()
    summary["time_to_recover"] = await time_to_recover_breakdown(
        session, project_id=project_id, settings=settings, now=moment
    )
    return summary


async def activity_feed(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
    event_types: Optional[Sequence[PlatformEventType]] = None,
) -> dict[str, Any]:
    """The §25 ARGUS activity feed, each item linking to its source."""
    from app.services.platform_events import recent_events

    events = await recent_events(
        session,
        project_id=project_id,
        event_types=event_types,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [_activity_item(event) for event in events],
        "limit": limit,
        "offset": offset,
    }


def _activity_item(event: PlatformEvent) -> dict[str, Any]:
    """One feed item, with the link to the object it is about.

    The link is derived from the subject that was stored, so every item can be
    followed back — §4's traceability requirement, applied to the feed.
    """
    subject = None
    link = None
    if event.subject_type and event.subject_id:
        subject = event.subject_type
        routes = {
            "incident": f"/incidents/{event.subject_id}",
            "case": f"/cases/{event.subject_id}",
            "component": f"/services/{event.subject_id}",
            "anomaly": f"/anomalies/{event.subject_id}",
            "forecast": f"/predictions/{event.subject_id}",
            "remediation_action": f"/remediation/{event.subject_id}",
            "patch": f"/fix/{event.subject_id}",
            "experiment": f"/reproduction/{event.subject_id}",
            "slo": f"/slo/{event.subject_id}",
            "knowledge": f"/intelligence/patterns/{event.subject_id}",
        }
        link = routes.get(subject)
    return {
        "id": str(event.id),
        "event_type": event.event_type.value,
        "title": EVENT_TITLE.get(event.event_type, event.event_type.value),
        "source": event.source,
        "occurred_at": _aware(event.occurred_at).isoformat(),
        "subject_type": subject,
        "subject_id": str(event.subject_id) if event.subject_id else None,
        "component_id": str(event.component_id) if event.component_id else None,
        "case_id": str(event.case_id) if event.case_id else None,
        "correlation_id": event.correlation_id,
        "link": link,
        "payload": event.payload,
        "processed": event.processed_at is not None,
    }


async def system_story(
    session: AsyncSession, *, correlation_id: str, limit: int = 200
) -> dict[str, Any]:
    """The §10 chain for one situation, in order, with its limits stated."""
    from app.services.platform_events import story_for_correlation

    events = await story_for_correlation(
        session, correlation_id=correlation_id, limit=limit
    )
    return {
        "correlation_id": correlation_id,
        "events": [_activity_item(event) for event in events],
        "stage_count": len(events),
        "note": (
            "events are ordered by the time they were recorded. Order is "
            "evidence of sequence, not proof of causation: ARGUS reports the "
            "story, the causal analysis reports the causes."
        ),
    }


async def project_overview_cards(
    session: AsyncSession, *, limit: int = 50
) -> list[dict[str, Any]]:
    """A one-line state per project, for multi-project operators (§42)."""
    from app.models.platform import DataQualityIssue

    projects = (
        await session.scalars(
            select(SoftwareProject)
            .where(SoftwareProject.status != ProjectStatus.ARCHIVED)
            .order_by(SoftwareProject.name)
            .limit(limit)
        )
    ).all()
    cards: list[dict[str, Any]] = []
    for project in projects:
        open_cases = await session.scalar(
            select(func.count(ReliabilityCase.id)).where(
                ReliabilityCase.project_id == project.id,
                ReliabilityCase.status.notin_(
                    [CaseStatus.CLOSED, CaseStatus.CANCELLED]
                ),
            )
        )
        open_issues = await session.scalar(
            select(func.count(DataQualityIssue.id)).where(
                DataQualityIssue.project_id == project.id,
                DataQualityIssue.status == DataQualityStatus.OPEN,
            )
        )
        cards.append(
            {
                "project_id": str(project.id),
                "name": project.name,
                "status": getattr(project.status, "value", str(project.status)),
                "open_cases": int(open_cases or 0),
                "open_data_quality_issues": int(open_issues or 0),
            }
        )
    return cards


async def environment_cards(
    session: AsyncSession, *, project_id: uuid.UUID, settings: Optional[Settings] = None
) -> list[dict[str, Any]]:
    """Per-environment health, so the operator can zoom out (§21)."""
    settings = settings or get_settings()
    environments = (
        await session.scalars(
            select(Environment)
            .where(Environment.project_id == project_id)
            .order_by(Environment.name)
        )
    ).all()
    from app.services.system_state import build_system_state

    cards: list[dict[str, Any]] = []
    for environment in environments:
        state = await build_system_state(
            session,
            project_id=project_id,
            environment_id=environment.id,
            settings=settings,
            include=("health",),
        )
        cards.append(
            {
                "environment_id": str(environment.id),
                "name": environment.name,
                "type": getattr(environment.environment_type, "value", None),
                "health": state.health,
                "state_counts": state.state_counts(),
                "limitations": state.limitations,
            }
        )
    return cards


async def route_incident_events(
    session: AsyncSession,
    *,
    limit: int = 500,
) -> CorrelationResult:
    """Open cases for live high-severity incidents and correlate their events.

    This is the scheduled entry point the sweep calls: it is the mechanism by
    which a situation that arrived while nobody was watching still becomes a case.
    """
    from app.models.incident import Incident, IncidentSeverity, IncidentStatus

    result = CorrelationResult()
    incidents = (
        await session.scalars(
            select(Incident)
            .where(
                Incident.status.notin_(
                    [IncidentStatus.RESOLVED, IncidentStatus.CLOSED]
                ),
                Incident.severity.in_(
                    [IncidentSeverity.HIGH, IncidentSeverity.CRITICAL]
                ),
            )
            .order_by(Incident.detected_at.desc())
            .limit(limit)
        )
    ).all()
    from app.services.reliability_case import find_open_case_for_incident

    for incident in incidents:
        existing = await find_open_case_for_incident(session, incident_id=incident.id)
        if existing is not None:
            await sync_case_with_phases(session, case=existing)
            continue
        case = await ensure_case_for_incident(session, incident=incident)
        if case is not None:
            result.cases_opened += 1
            await sync_case_with_phases(session, case=case)
    return result


__all__ = [
    "EVENT_KIND",
    "EVENT_TITLE",
    "CorrelationResult",
    "Overview",
    "activity_feed",
    "build_overview",
    "correlate_events",
    "engineering_summary",
    "ensure_case_for_incident",
    "environment_cards",
    "open_case_for_forecast",
    "open_case_for_slo_burn",
    "project_overview_cards",
    "route_incident_events",
    "system_story",
    "sync_case_with_phases",
]
