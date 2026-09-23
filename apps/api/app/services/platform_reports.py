"""ARGUS Reliability Reporting & Postmortems (Phase 11 §37, §77–§85).

Reports assembled from stored evidence, and postmortems whose every factual
sentence is traceable to a row.

The §80 rule is the most important thing in this module: **the AI may draft
narrative from structured evidence, and may not invent impact, root cause,
timeline, remediation or customer impact.** That is implemented as a hard split,
not a prompt instruction:

* :func:`build_postmortem` produces a fully populated *structured* document from
  the database — impact counts, the timeline, the candidates with their scores,
  the remediation record, the verification verdict. This is the truth.
* The narrative fields (``narrative``, ``lessons_narrative``) are the only places
  an AI may write, they are generated **from** that structure, and when no AI
  provider is configured the section is omitted with a reason rather than
  filled with plausible prose. A draft generator that always produces something
  is a generator that will produce something when it has nothing to say.

Unknowns stay unknown throughout: an incident with no causal analysis says "no
root cause analysis was completed", it does not say "the root cause was unclear".

§82's improvement plan aggregates recurring recommendations with *transparent*
criteria: each item shows the evidence that produced its priority, so an operator
can disagree with the ranking, which is the point.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.anomaly import Anomaly
from app.services.platform_time import aware as _aware, elapsed_seconds
from app.models.causal import AnalysisStatus, CausalAnalysis, RootCauseCandidate
from app.models.incident import Incident, IncidentStatus
from app.models.platform import ReliabilityCase, ReliabilityCaseTimeline
from app.models.remediation import (
    RemediationAction,
    RemediationOutcome,
    RemediationVerification,
)

logger = logging.getLogger(__name__)

#: The report kinds §37 names, with the window each defaults to.
REPORT_KINDS: dict[str, int] = {
    "daily": 1,
    "weekly": 7,
    "monthly": 30,
}

#: §77's report sections, in order. Declared so the API, the JSON export and the
#: CSV export cannot drift apart.
REPORT_SECTIONS = (
    "executive_summary",
    "system_health",
    "incidents",
    "root_causes",
    "changes",
    "predicted_risks",
    "remediation",
    "recovery",
    "slo",
    "learning",
    "recommendations",
    "limitations",
)


# ---------------------------------------------------------------------------
# §37, §77 — the reliability report
# ---------------------------------------------------------------------------
@dataclass
class ReliabilityReport:
    """A generated report with its sections and its honesty statement."""

    kind: str
    window_days: int
    project_id: uuid.UUID
    generated_at: datetime
    environment_id: Optional[uuid.UUID] = None
    sections: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "window_days": self.window_days,
            "project_id": str(self.project_id),
            "environment_id": str(self.environment_id) if self.environment_id else None,
            "generated_at": self.generated_at.isoformat(),
            "sections": self.sections,
            "limitations": list(self.limitations),
        }


async def build_report(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    kind: str = "weekly",
    window_days: Optional[int] = None,
    environment_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> ReliabilityReport:
    """Generate the §77 report for a window."""
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    days = window_days or REPORT_KINDS.get(kind, 7)
    window_start = moment - timedelta(days=days)
    report = ReliabilityReport(
        kind=kind,
        window_days=days,
        project_id=project_id,
        generated_at=moment,
        environment_id=environment_id,
    )

    from app.services.change_intelligence import change_failure_rate
    from app.services.control_plane import build_overview
    from app.services.platform_metrics import (
        mean_time_to_detect,
        time_to_recover_breakdown,
    )
    from app.services.slo_service import slo_overview

    # -- incidents
    incident_stmt = select(Incident).where(
        Incident.project_id == project_id,
        Incident.detected_at >= window_start,
        Incident.detected_at <= moment,
    )
    if environment_id is not None:
        incident_stmt = incident_stmt.where(Incident.environment_id == environment_id)
    incidents = list((await session.scalars(incident_stmt)).all())
    by_severity: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for incident in incidents:
        severity = getattr(incident.severity, "value", str(incident.severity))
        status = getattr(incident.status, "value", str(incident.status))
        by_severity[severity] = by_severity.get(severity, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
    resolved = [
        incident
        for incident in incidents
        if getattr(incident.status, "value", None)
        in (IncidentStatus.RESOLVED.value, IncidentStatus.CLOSED.value)
        and incident.resolved_at
    ]
    #: Guarded by ``incident.resolved_at`` in the comprehension above, but the
    #: column is nullable — ``elapsed_seconds`` keeps the None out of the list
    #: instead of relying on a filter several lines away.
    recovery_seconds = [
        seconds
        for seconds in (
            elapsed_seconds(incident.detected_at, incident.resolved_at)
            for incident in resolved
        )
        if seconds is not None
    ]
    report.sections["incidents"] = {
        "total": len(incidents),
        "resolved": len(resolved),
        "open": len(incidents) - len(resolved),
        "by_severity": by_severity,
        "by_status": by_status,
        "mean_recovery_seconds": (
            round(sum(recovery_seconds) / len(recovery_seconds), 1)
            if recovery_seconds
            else None
        ),
        "incidents": [
            {
                "id": str(incident.id),
                "title": incident.title,
                "severity": getattr(incident.severity, "value", str(incident.severity)),
                "status": getattr(incident.status, "value", str(incident.status)),
                "detected_at": _aware(incident.detected_at).isoformat(),
                "resolved_at": _aware(incident.resolved_at).isoformat()
                if incident.resolved_at
                else None,
            }
            for incident in incidents[:50]
        ],
    }

    # -- root causes
    analyses = (
        await session.scalars(
            select(CausalAnalysis).where(
                CausalAnalysis.project_id == project_id,
                CausalAnalysis.created_at >= window_start,
            )
        )
    ).all()
    candidate_summary: list[dict[str, Any]] = []
    for analysis in analyses:
        candidate = (
            await session.scalars(
                select(RootCauseCandidate)
                .where(RootCauseCandidate.analysis_id == analysis.id)
                .order_by(RootCauseCandidate.score.desc())
                .limit(1)
            )
        ).first()
        candidate_summary.append(
            {
                "analysis_id": str(analysis.id),
                "incident_id": str(analysis.incident_id),
                "status": getattr(analysis.status, "value", str(analysis.status)),
                "top_candidate": (
                    {
                        "candidate_type": getattr(
                            candidate.candidate_type,
                            "value",
                            str(candidate.candidate_type),
                        ),
                        "status": getattr(
                            candidate.status, "value", str(candidate.status)
                        ),
                        "score": candidate.score,
                        "confidence": getattr(candidate.confidence, "value", None)
                        if candidate.confidence
                        else None,
                    }
                    if candidate
                    else None
                ),
            }
        )
    report.sections["root_causes"] = {
        "analyses": len(analyses),
        "completed": sum(1 for a in analyses if a.status == AnalysisStatus.COMPLETED),
        "candidates": candidate_summary,
    }

    # -- changes
    from app.services.change_intelligence import recent_changes

    report.sections["changes"] = {
        "recent": await recent_changes(
            session,
            project_id=project_id,
            environment_id=environment_id,
            since=window_start,
            until=moment,
            limit=50,
        ),
        "change_failure_rate": (
            await change_failure_rate(
                session,
                project_id=project_id,
                window_days=days,
                settings=settings,
                now=moment,
            )
        ).as_dict(),
    }

    # -- remediation + recovery
    actions = (
        await session.scalars(
            select(RemediationAction).where(
                RemediationAction.project_id == project_id,
                RemediationAction.created_at >= window_start,
            )
        )
    ).all()
    outcomes: dict[str, int] = {}
    for action in actions:
        key = getattr(action.outcome, "value", None) or "INCONCLUSIVE"
        outcomes[key] = outcomes.get(key, 0) + 1
    effective = outcomes.get(RemediationOutcome.EFFECTIVE.value, 0) + outcomes.get(
        RemediationOutcome.PARTIALLY_EFFECTIVE.value, 0
    )
    report.sections["remediation"] = {
        "actions": len(actions),
        "by_outcome": outcomes,
        "effective_rate": round(effective / len(actions), 4) if actions else None,
        "actions_detail": [
            {
                "id": str(action.id),
                "action_type": getattr(
                    action.action_type, "value", str(action.action_type)
                ),
                "status": getattr(action.status, "value", str(action.status)),
                "outcome": getattr(action.outcome, "value", None)
                if action.outcome
                else None,
            }
            for action in actions[:50]
        ],
    }
    report.sections["recovery"] = await time_to_recover_breakdown(
        session,
        project_id=project_id,
        window_days=days,
        now=moment,
        settings=settings,
    )

    # -- health, predictions, SLO, learning, recommendations
    overview = await build_overview(
        session, project_id=project_id, environment_id=environment_id, now=moment
    )
    report.sections["executive_summary"] = overview.executive_summary
    report.sections["system_health"] = {
        "health": overview.health,
        "state_counts": overview.state_counts,
        "top_risky_components": overview.top_risky_components,
    }
    report.sections["predicted_risks"] = {
        "active": overview.predicted_risks,
        "accuracy": overview.argus_health.get("summary", {}),
    }
    report.sections["slo"] = await slo_overview(
        session, project_id=project_id, settings=settings, now=moment
    )
    report.sections["learning"] = {
        "active_patterns": overview.learning_insights.get("active_patterns"),
        "available": overview.learning_insights.get("available"),
        "insights": overview.learning_insights,
    }
    report.sections["recommendations"] = await build_improvement_plan(
        session, project_id=project_id, window_days=days, now=moment, settings=settings
    )
    report.sections["mttd"] = await mean_time_to_detect(
        session, project_id=project_id, window_days=days, now=moment
    )

    if not incidents:
        report.limitations.append(
            f"no incident was recorded in this {days}-day window, so the incident "
            "sections are empty rather than reassuring"
        )
    if report.sections["slo"].get("objectives_total", 0) == 0:
        report.limitations.append(
            "no service level objectives are defined, so availability and budget "
            "sections are absent"
        )
    report.limitations.append(
        "every figure here is derived from stored rows; sections that could not be "
        "measured are absent or null, never zero"
    )
    report.sections["limitations"] = list(report.limitations)
    return report


# ---------------------------------------------------------------------------
# §78 — exports
# ---------------------------------------------------------------------------
def render_report_json(report: ReliabilityReport) -> str:
    """§78 JSON export, using the project's artifact conventions (a string)."""
    return json.dumps(report.as_dict(), indent=2, sort_keys=True, default=str)


def render_report_csv(report: ReliabilityReport) -> str:
    """§78 CSV export of the *tabular* parts, with a section column.

    A CSV flattening of a nested document is always a lossy projection, so it is
    done explicitly: sections that are tables (incidents, changes, actions) get
    rows; sections that are summaries become key/value rows.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["section", "key", "value"])
    for section, value in report.sections.items():
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, (list, dict)):
                    writer.writerow(
                        [section, key, json.dumps(item, default=str)[:2000]]
                    )
                else:
                    writer.writerow([section, key, item])
        else:
            writer.writerow([section, "value", json.dumps(value, default=str)[:2000]])
    return buffer.getvalue()


def render_report_markdown(report: ReliabilityReport) -> str:
    """The §78 "PDF/report abstraction": a printable markdown render.

    No PDF library is introduced for this. §78 asks for a *report abstraction*,
    and a markdown document renders to PDF with tooling every operator already
    has — adding a PDF dependency would be a build risk for a presentation
    preference.
    """
    lines = [
        f"# Reliability report — {report.kind}",
        "",
        f"_Window: {report.window_days} days · generated {report.generated_at.isoformat()}_",
        "",
    ]
    summary = report.sections.get("executive_summary", {})
    if summary:
        lines += ["## Executive summary", "", f"{summary.get('headline', '')}", ""]
        for key, value in summary.items():
            if key == "headline":
                continue
            lines.append(f"- **{key}**: {value}")
        lines.append("")
    incidents = report.sections.get("incidents", {})
    lines += [
        "## Incidents",
        "",
        f"- total: {incidents.get('total')}",
        f"- resolved: {incidents.get('resolved')}",
        f"- open: {incidents.get('open')}",
        f"- by severity: {incidents.get('by_severity')}",
        "",
    ]
    for incident in incidents.get("incidents", [])[:20]:
        lines.append(
            f"- {incident['severity']} · {incident['title']} "
            f"({incident['status']}, detected {incident['detected_at']})"
        )
    lines.append("")
    plan = report.sections.get("recommendations", {})
    if plan.get("items"):
        lines += ["## Improvement plan", ""]
        for item in plan["items"]:
            lines.append(
                f"- **{item['title']}** (priority {item['priority']}) — "
                f"{item['rationale']}"
            )
        lines.append("")
    lines += ["## Limitations", ""]
    lines += [f"- {limitation}" for limitation in report.limitations]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# §79–§81 — postmortems
# ---------------------------------------------------------------------------
#: §79's sections. Declared as data so a postmortem is complete by construction.
POSTMORTEM_SECTIONS = (
    "incident_summary",
    "impact",
    "timeline",
    "detection",
    "root_cause",
    "contributing_factors",
    "response",
    "remediation",
    "verification",
    "lessons_learned",
    "follow_up_actions",
    "unknowns",
)


@dataclass
class Postmortem:
    """A structured postmortem (§79) with optional AI-drafted narrative (§80)."""

    incident_id: uuid.UUID
    title: str
    generated_at: datetime
    sections: dict[str, Any] = field(default_factory=dict)
    narrative: Optional[str] = None
    narrative_provider: Optional[str] = None
    narrative_unavailable_reason: Optional[str] = None
    unknowns: list[str] = field(default_factory=list)
    follow_up_actions: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "incident_id": str(self.incident_id),
            "title": self.title,
            "generated_at": self.generated_at.isoformat(),
            "sections": self.sections,
            "narrative": self.narrative,
            "narrative_provider": self.narrative_provider,
            "narrative_unavailable_reason": self.narrative_unavailable_reason,
            "unknowns": list(self.unknowns),
            "follow_up_actions": list(self.follow_up_actions),
            "note": (
                "every structured section is derived from stored rows. The "
                "narrative is the only drafted text, it is generated from those "
                "sections, and it is omitted entirely when no provider is "
                "configured rather than filled with plausible prose."
            ),
        }


async def build_postmortem(
    session: AsyncSession,
    *,
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
    draft_narrative: bool = False,
) -> Optional[Postmortem]:
    """Build a postmortem for an incident (§79, §80)."""
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    incident = await session.get(Incident, incident_id)
    if incident is None:
        return None
    if project_id is not None and incident.project_id != project_id:
        return None

    postmortem = Postmortem(
        incident_id=incident.id,
        title=incident.title,
        generated_at=moment,
    )
    severity = getattr(incident.severity, "value", str(incident.severity))
    status = getattr(incident.status, "value", str(incident.status))

    postmortem.sections["incident_summary"] = {
        "title": incident.title,
        "summary": incident.summary or incident.description,
        "severity": severity,
        "status": status,
        "detected_at": _aware(incident.detected_at).isoformat(),
        "resolved_at": _aware(incident.resolved_at).isoformat()
        if incident.resolved_at
        else None,
        "fingerprint": incident.fingerprint,
        "primary_component_id": str(incident.primary_component_id)
        if incident.primary_component_id
        else None,
    }

    # -- impact: what the rows support, and nothing more
    from app.models.anomaly import Anomaly

    anomaly_count = await session.scalar(
        select(func.count(Anomaly.id)).where(Anomaly.incident_id == incident.id)
    )
    affected_components = (
        await session.scalars(
            select(Anomaly.component_id)
            .where(
                Anomaly.incident_id == incident.id, Anomaly.component_id.is_not(None)
            )
            .distinct()
        )
    ).all()
    postmortem.sections["impact"] = {
        "anomalies": int(anomaly_count or 0),
        "affected_components": [str(row) for row in affected_components],
        "duration_seconds": (
            (
                _aware(incident.resolved_at) - _aware(incident.detected_at)
            ).total_seconds()
            if incident.resolved_at
            else None
        ),
        "note": (
            "impact is expressed as stored evidence — anomalies and affected "
            "components. ARGUS holds no customer-impact data and does not "
            "estimate any."
        ),
    }
    postmortem.unknowns.append(
        "customer or revenue impact is unknown: ARGUS has no access to that data"
    )

    # -- timeline from the case, when one exists
    case = (
        await session.scalars(
            select(ReliabilityCase)
            .where(ReliabilityCase.incident_id == incident.id)
            .order_by(ReliabilityCase.opened_at.desc())
            .limit(1)
        )
    ).first()
    timeline_entries: list[ReliabilityCaseTimeline] = []
    if case is not None:
        timeline_entries = list(
            (
                await session.scalars(
                    select(ReliabilityCaseTimeline)
                    .where(ReliabilityCaseTimeline.case_id == case.id)
                    .order_by(ReliabilityCaseTimeline.sequence)
                )
            ).all()
        )

    #: The incident's own stored milestones come first, and always. A case is an
    #: enrichment, not a precondition: auto-casing is configurable (§61), and a
    #: postmortem whose timeline was empty because no case happened to be open
    #: would read as "nothing occurred" — the opposite of the truth, and exactly
    #: what §79 asks this section to prevent.
    incident_timeline: list[dict[str, Any]] = []
    detected_at = _aware(incident.detected_at)
    if detected_at is not None:
        incident_timeline.append(
            {
                "source": "incident",
                "event_type": "DETECTED",
                "occurred_at": detected_at.isoformat(),
                "title": f"Incident detected: {incident.title}",
                "actor": None,
                "system_action": True,
                "evidence": {
                    "severity": getattr(
                        incident.severity, "value", str(incident.severity)
                    ),
                    "fingerprint": incident.fingerprint,
                },
            }
        )
    started_at = _aware(incident.started_at)
    if started_at is not None and started_at != detected_at:
        incident_timeline.append(
            {
                "source": "incident",
                "event_type": "STARTED",
                "occurred_at": started_at.isoformat(),
                "title": "The condition that caused this incident began",
                "actor": None,
                "system_action": True,
                "evidence": {},
            }
        )
    for anomaly in (
        await session.scalars(
            select(Anomaly)
            .where(Anomaly.incident_id == incident.id)
            .order_by(Anomaly.detected_at)
            .limit(settings.PLATFORM_POSTMORTEM_TIMELINE_LIMIT)
        )
    ).all():
        moment = _aware(anomaly.detected_at)
        incident_timeline.append(
            {
                "source": "anomaly",
                "event_type": "ANOMALY_DETECTED",
                "occurred_at": moment.isoformat() if moment else "",
                "title": (
                    f"{getattr(anomaly.anomaly_type, 'value', 'anomaly')} on "
                    f"{anomaly.metric_name or 'the component'}"
                ),
                "actor": None,
                "system_action": True,
                "evidence": {
                    "anomaly_id": str(anomaly.id),
                    "severity": getattr(
                        anomaly.severity, "value", str(anomaly.severity)
                    ),
                },
            }
        )
    resolved_at = _aware(incident.resolved_at)
    if resolved_at is not None:
        incident_timeline.append(
            {
                "source": "incident",
                "event_type": "RESOLVED",
                "occurred_at": resolved_at.isoformat(),
                "title": "The incident was resolved",
                "actor": None,
                "system_action": True,
                "evidence": {
                    "status": getattr(incident.status, "value", str(incident.status))
                },
            }
        )

    case_timeline = [
        {
            "source": "case",
            "sequence": entry.sequence,
            "occurred_at": _aware(entry.occurred_at).isoformat(),
            "event_type": entry.event_type,
            "kind": entry.kind.value,
            "title": entry.title,
            "detail": entry.detail,
            "actor": entry.actor,
            "system_action": entry.system_action,
            "evidence": entry.evidence,
        }
        for entry in timeline_entries
    ]
    postmortem.sections["timeline"] = {
        "case_reference": case.reference if case else None,
        "entries": sorted(
            incident_timeline + case_timeline,
            key=lambda entry: entry.get("occurred_at") or "",
        ),
        "notes": (
            "incident milestones and linked anomalies always appear; case entries "
            "are added when a reliability case exists for this incident"
        ),
    }
    if case is None:
        postmortem.unknowns.append(
            "no reliability case exists for this incident, so the timeline covers "
            "incident-level facts only"
        )

    # -- detection
    from app.services.platform_metrics import incident_recovery_breakdown

    breakdown = await incident_recovery_breakdown(session, incident=incident)
    postmortem.sections["detection"] = {
        "detected_at": _aware(incident.detected_at).isoformat(),
        "detection_seconds": breakdown.detection_seconds,
        "triage_seconds": breakdown.triage_seconds,
        "measured_stages": breakdown.measured_stages,
        "unmeasured_stages": breakdown.unmeasured_stages,
    }

    # -- root cause, straight from the causal analysis
    analysis = (
        await session.scalars(
            select(CausalAnalysis)
            .where(
                CausalAnalysis.incident_id == incident.id,
                CausalAnalysis.status == AnalysisStatus.COMPLETED,
            )
            .order_by(CausalAnalysis.created_at.desc())
            .limit(1)
        )
    ).first()
    if analysis is None:
        postmortem.sections["root_cause"] = {
            "status": "NOT_ANALYZED",
            "candidates": [],
        }
        postmortem.unknowns.append(
            "no completed root cause analysis exists for this incident"
        )
        postmortem.sections["contributing_factors"] = {"factors": []}
    else:
        candidates = list(
            (
                await session.scalars(
                    select(RootCauseCandidate)
                    .where(RootCauseCandidate.analysis_id == analysis.id)
                    .order_by(RootCauseCandidate.score.desc())
                    .limit(10)
                )
            ).all()
        )
        postmortem.sections["root_cause"] = {
            "status": "ANALYZED",
            "analysis_id": str(analysis.id),
            "confidence": getattr(
                getattr(candidates[0], "confidence", None), "value", None
            )
            if candidates
            else None,
            "candidates": [
                {
                    "id": str(candidate.id),
                    "candidate_type": getattr(
                        candidate.candidate_type, "value", str(candidate.candidate_type)
                    ),
                    "status": getattr(candidate.status, "value", str(candidate.status)),
                    "score": candidate.score,
                    "confidence": getattr(candidate.confidence, "value", None)
                    if candidate.confidence
                    else None,
                    "explanation": getattr(candidate, "explanation", None),
                }
                for candidate in candidates
            ],
        }
        postmortem.sections["contributing_factors"] = {
            "factors": [
                {
                    "candidate_type": getattr(
                        candidate.candidate_type, "value", str(candidate.candidate_type)
                    ),
                    "score": candidate.score,
                }
                for candidate in candidates[1:]
            ]
        }
        if not candidates:
            postmortem.unknowns.append(
                "the causal analysis produced no candidate, so no root cause could "
                "be stated"
            )

    # -- response: what ARGUS actually did
    postmortem.sections["response"] = {
        "timeline_entries": len(timeline_entries),
        "system_actions": sum(1 for entry in timeline_entries if entry.system_action),
        "human_actions": sum(1 for entry in timeline_entries if entry.actor),
    }

    # -- remediation + verification
    actions = list(
        (
            await session.scalars(
                select(RemediationAction)
                .where(RemediationAction.incident_id == incident.id)
                .order_by(RemediationAction.created_at)
            )
        ).all()
    )
    postmortem.sections["remediation"] = {
        "actions": [
            {
                "id": str(action.id),
                "action_type": getattr(
                    action.action_type, "value", str(action.action_type)
                ),
                "status": getattr(action.status, "value", str(action.status)),
                "outcome": getattr(action.outcome, "value", None)
                if action.outcome
                else None,
                "execution_mode": getattr(
                    action.execution_mode, "value", str(action.execution_mode)
                ),
                "created_at": _aware(action.created_at).isoformat(),
            }
            for action in actions
        ]
    }
    verifications: list[dict[str, Any]] = []
    for action in actions:
        rows = (
            await session.scalars(
                select(RemediationVerification).where(
                    RemediationVerification.action_id == action.id
                )
            )
        ).all()
        for verification in rows:
            verifications.append(
                {
                    "action_id": str(action.id),
                    "verdict": getattr(verification.verdict, "value", None),
                    "verified_by": verification.verified_by,
                    "window_start": _aware(verification.window_start).isoformat(),
                    "window_end": _aware(verification.window_end).isoformat(),
                    "passed": verification.passed_count,
                    "failed": verification.failed_count,
                    "not_observable": verification.not_observable_count,
                }
            )
    postmortem.sections["verification"] = {"verifications": verifications}
    if not actions:
        postmortem.unknowns.append(
            "no remediation action was recorded for this incident"
        )
    if not verifications:
        postmortem.unknowns.append(
            "no verification run exists, so recovery is inferred from the incident "
            "status rather than measured"
        )

    # -- lessons, from the learning layer where available
    postmortem.sections["lessons_learned"] = await _lessons(
        session, incident=incident, project_id=incident.project_id
    )

    # -- follow-up actions (§81): proposed, never executed
    postmortem.follow_up_actions = _follow_up_actions(
        postmortem=postmortem, incident=incident, actions=actions
    )
    postmortem.sections["follow_up_actions"] = postmortem.follow_up_actions
    postmortem.sections["unknowns"] = postmortem.unknowns

    if draft_narrative and settings.PLATFORM_AI_ASSISTANT_ENABLED:
        narrative, provider, reason = await _draft_narrative(postmortem)
        postmortem.narrative = narrative
        postmortem.narrative_provider = provider
        postmortem.narrative_unavailable_reason = reason
    else:
        postmortem.narrative_unavailable_reason = (
            "narrative drafting is switched off; the structured sections above are "
            "complete without it"
        )
    return postmortem


async def _lessons(
    session: AsyncSession, *, incident: Incident, project_id: uuid.UUID
) -> dict[str, Any]:
    """What the learning layer retrieved for this incident, or why it could not."""
    try:
        from app.services.experience_retrieval import ExperienceRetrievalService

        result = await ExperienceRetrievalService().retrieve_for_incident(
            session, incident_id=incident.id, project_id=project_id, limit=5
        )
        return {
            "available": True,
            "summary": result.summary,
            "similar": [match.as_dict() for match in result.matches],
        }
    except Exception as exc:
        return {
            "available": False,
            "reason": f"the learning layer did not answer ({type(exc).__name__})",
        }


def _follow_up_actions(
    *,
    postmortem: Postmortem,
    incident: Incident,
    actions: Sequence[RemediationAction],
) -> list[dict[str, Any]]:
    """§81 follow-up suggestions, derived from what the postmortem could not find.

    Each one names the gap that produced it. Nothing is filed anywhere: §81 says
    follow-up tasks are not executed automatically, so these are proposals with
    their reasoning attached.
    """
    proposals: list[dict[str, Any]] = []
    root_cause = postmortem.sections.get("root_cause", {})
    if root_cause.get("status") == "NOT_ANALYZED":
        proposals.append(
            {
                "action": "investigate recurring pattern",
                "title": f"Investigate why no root cause analysis exists for: {incident.title}",
                "rationale": "the incident has no completed causal analysis",
                "executed": False,
            }
        )
    if not postmortem.sections.get("verification", {}).get("verifications"):
        proposals.append(
            {
                "action": "improve monitoring",
                "title": "Add verification evidence for the recovery",
                "rationale": (
                    "recovery was inferred from the incident status, with no stored "
                    "verification run to point at"
                ),
                "executed": False,
            }
        )
    if not postmortem.sections.get("timeline", {}).get("entries"):
        proposals.append(
            {
                "action": "update documentation",
                "title": "Record an operational timeline for this incident",
                "rationale": "there is no unified timeline to learn from",
                "executed": False,
            }
        )
    for action in actions:
        if getattr(action, "outcome", None) == RemediationOutcome.INEFFECTIVE:
            proposals.append(
                {
                    "action": "review dependency",
                    "title": (
                        f"Review why the {getattr(action.action_type, 'value', 'action')} "
                        "remediation was ineffective"
                    ),
                    "rationale": "an action was recorded as ineffective",
                    "executed": False,
                }
            )
            break
    if not proposals:
        proposals.append(
            {
                "action": "no follow-up required",
                "title": "No follow-up action was identified from the stored evidence",
                "rationale": (
                    "the analysis, timeline and verification records are complete, so "
                    "nothing is missing"
                ),
                "executed": False,
            }
        )
    return proposals


async def _draft_narrative(
    postmortem: Postmortem,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Draft narrative text *from* the structured sections (§80).

    Returns ``(narrative, provider, unavailable_reason)``. When no AI provider is
    configured, the reason is returned and no text is produced — the honest
    outcome, and the one that keeps inventing impossible.
    """
    try:
        from app.services.ai_debugger import MockAIProvider, resolve_provider

        provider = resolve_provider()
    except Exception:
        return (
            None,
            None,
            (
                "no AI provider interface is available in this deployment, so no "
                "narrative was drafted. The structured sections are complete."
            ),
        )
    if isinstance(provider, MockAIProvider):
        #: The mock returns canned text. Using it would put invented prose in a
        #: postmortem, which §80 forbids — so it is refused with the reason.
        return (
            None,
            "mock",
            (
                "only the mock AI provider is available, and the mock returns "
                "canned text; §80 forbids inventing impact, root cause, timeline "
                "or remediation, so no narrative was drafted"
            ),
        )
    try:
        narrative = await _narrative_prompt(postmortem=postmortem, provider=provider)
    except Exception as exc:
        return (
            None,
            getattr(provider, "name", None),
            f"the provider call failed ({type(exc).__name__}); no narrative was drafted",
        )
    return narrative, getattr(provider, "name", "ai"), None


async def _narrative_prompt(*, postmortem: Postmortem, provider: Any) -> Optional[str]:
    """Ask the provider to narrate the structured sections it was given (§80).

    The prompt is built from the *already-derived* structure and instructs the
    model to use nothing else, and to say ``unknown`` where a section is empty.
    The model's output is returned as a draft; the structured sections remain the
    record.
    """
    structure = {
        section: postmortem.sections.get(section)
        for section in (
            "incident_summary",
            "impact",
            "detection",
            "root_cause",
            "remediation",
            "verification",
        )
    }
    prompt = (
        "Draft a short incident narrative from the evidence below. Use only the "
        "evidence given: do not invent impact, root cause, timeline events, "
        "remediation or customer impact. Where something is missing, say it is "
        "unknown.\n\n" + json.dumps(structure, default=str)[:8000]
    )
    answer = await provider.complete(prompt)
    text = getattr(answer, "text", None) or getattr(answer, "content", None)
    return str(text).strip() if text else None


# ---------------------------------------------------------------------------
# §82, §83 — the improvement plan
# ---------------------------------------------------------------------------
#: Priority bands and what puts an item in each. Transparent on purpose: an
#: operator must be able to argue with the ranking (§82).
def _priority_for(score: int) -> str:
    if score >= 6:
        return "HIGH"
    if score >= 3:
        return "MEDIUM"
    return "LOW"


async def build_improvement_plan(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    window_days: int = 30,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """Aggregate recurring reliability signals into a plan (§82, §83)."""
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    window_start = moment - timedelta(days=window_days)
    items: list[dict[str, Any]] = []

    # -- repeated incidents on one component
    rows = (
        await session.execute(
            select(Incident.primary_component_id, func.count())
            .where(
                Incident.project_id == project_id,
                Incident.detected_at >= window_start,
                Incident.primary_component_id.is_not(None),
            )
            .group_by(Incident.primary_component_id)
            .having(func.count() >= 2)
        )
    ).all()
    for component_id, count in rows:
        items.append(
            {
                "title": f"Reduce recurring incidents on one component ({count} in {window_days}d)",
                "category": "repeated_incidents",
                "component_id": str(component_id),
                "score": int(count),
                "evidence": {"incidents": int(count), "window_days": window_days},
                "rationale": (
                    f"{count} incidents were attributed to this component inside the "
                    "window; recurring incidents are the strongest signal for "
                    "investment"
                ),
            }
        )

    # -- ineffective remediation
    ineffective = (
        await session.execute(
            select(RemediationAction.action_type, func.count())
            .where(
                RemediationAction.project_id == project_id,
                RemediationAction.created_at >= window_start,
                RemediationAction.outcome == RemediationOutcome.INEFFECTIVE,
            )
            .group_by(RemediationAction.action_type)
        )
    ).all()
    for action_type, count in ineffective:
        items.append(
            {
                "title": (
                    f"Improve timeout handling / effectiveness of "
                    f"{getattr(action_type, 'value', action_type)}"
                ),
                "category": "ineffective_remediation",
                "score": int(count) * 2,
                "evidence": {
                    "action_type": getattr(action_type, "value", str(action_type)),
                    "count": int(count),
                },
                "rationale": (
                    "this remediation was recorded ineffective at least once, so the "
                    "underlying condition it targets is not addressed by it"
                ),
            }
        )

    # -- chronic anomalies on one metric
    metric_rows = (
        await session.execute(
            select(Anomaly.metric_name, func.count())
            .where(
                Anomaly.project_id == project_id,
                Anomaly.detected_at >= window_start,
                Anomaly.metric_name.is_not(None),
            )
            .group_by(Anomaly.metric_name)
            .having(func.count() >= 3)
        )
    ).all()
    for metric_name, count in metric_rows:
        items.append(
            {
                "title": f"Stabilize the metric '{metric_name}'",
                "category": "chronic_anomaly",
                "score": int(count),
                "evidence": {"metric_name": metric_name, "anomalies": int(count)},
                "rationale": (
                    "the same metric crossed a threshold repeatedly, which usually "
                    "means the threshold or the system has drifted"
                ),
            }
        )

    # -- data-quality gaps, straight from the quality center
    try:
        from app.models.platform import DataQualityIssue, DataQualityStatus

        quality_rows = (
            await session.execute(
                select(DataQualityIssue.kind, func.count())
                .where(
                    DataQualityIssue.project_id == project_id,
                    DataQualityIssue.status == DataQualityStatus.OPEN,
                )
                .group_by(DataQualityIssue.kind)
            )
        ).all()
        for kind, count in quality_rows:
            items.append(
                {
                    "title": f"Resolve data-quality issues of kind {getattr(kind, 'value', kind)}",
                    "category": "data_quality",
                    "score": int(count),
                    "evidence": {
                        "kind": getattr(kind, "value", str(kind)),
                        "open": int(count),
                    },
                    "rationale": (
                        "open consistency issues degrade every conclusion drawn from "
                        "this project's data"
                    ),
                }
            )
    except Exception:  # pragma: no cover - optional section
        pass

    # -- deployment risk
    from app.services.change_intelligence import change_failure_rate

    cfr = await change_failure_rate(
        session,
        project_id=project_id,
        window_days=window_days,
        settings=settings,
        now=moment,
    )
    if cfr.failure_rate is not None and cfr.deployments_total >= 3:
        items.append(
            {
                "title": "Reduce deployment risk",
                "category": "deployment_risk",
                "score": int(round(cfr.failure_rate * 10)),
                "evidence": cfr.as_dict(),
                "rationale": (
                    f"the change failure rate is {cfr.failure_rate:.0%} over the "
                    "window, which is high enough to warrant canaries or smaller "
                    "batches"
                ),
            }
        )

    for item in items:
        item["priority"] = _priority_for(int(item["score"]))
    items.sort(key=lambda item: (item["score"], item["title"]), reverse=True)
    return {
        "window_days": window_days,
        "items": items[:20],
        "criteria": (
            "priority is derived from the item's own evidence count, with two "
            "multipliers — ineffective remediation counts double, and change "
            "failure rate is scaled from its rate. HIGH ≥ 6, MEDIUM ≥ 3, else LOW. "
            "The criteria are shown so the ranking can be disagreed with."
        ),
        "note": (
            "these are signals, not verdicts: each item names the rows behind it, "
            "and none of them is an action ARGUS will take"
        ),
        "truncated": len(items) > 20,
    }


__all__ = [
    "POSTMORTEM_SECTIONS",
    "REPORT_KINDS",
    "REPORT_SECTIONS",
    "Postmortem",
    "ReliabilityReport",
    "build_improvement_plan",
    "build_postmortem",
    "build_report",
    "render_report_csv",
    "render_report_json",
    "render_report_markdown",
]
