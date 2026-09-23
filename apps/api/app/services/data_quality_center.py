"""ARGUS Data Quality Center (Phase 11 §87–§90).

Cross-phase consistency checks. Every check asks the same shape of question —
*does this row still point at something that exists, and does it have the
evidence the phase promised?* — and every finding is a stored issue with its
subject, its evidence and a suggestion.

Two rules, both about not making things worse:

**Report, never mutate.** §90 says do not silently change important historical
data, so nothing here writes to another phase's rows. A finding is a row in
``data_quality_issues`` and a sentence an operator can act on. There is no
auto-repair, because every candidate repair (delete the orphan, rewrite the
reference, backfill the missing evidence) is a decision about history that a
platform should not make on its own.

**Resolve by observation, not by assertion.** An issue that no longer reproduces
is marked RESOLVED automatically, with the moment recorded. That is the one write
this module performs on its own findings — and it is a statement that the *check*
stopped failing, which the check itself just verified.
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
from app.models.anomaly import Anomaly
from app.models.incident import Incident
from app.services.platform_time import aware as _aware
from app.models.platform import (
    DataQualityIssue,
    DataQualityIssueKind,
    DataQualitySeverity,
    DataQualityStatus,
    PlatformEventType,
)
from app.models.system import SystemComponent

logger = logging.getLogger(__name__)

#: What each check means, in one line, for the UI and the report. Kept beside the
#: checks so a finding is never displayed without its meaning.
ISSUE_DESCRIPTIONS: dict[DataQualityIssueKind, str] = {
    DataQualityIssueKind.ORPHANED_RECORD: (
        "a row references a parent that no longer exists"
    ),
    DataQualityIssueKind.INCIDENT_WITHOUT_COMPONENT: (
        "an incident affects no component, so its impact cannot be located"
    ),
    DataQualityIssueKind.PREDICTION_WITHOUT_SNAPSHOT: (
        "a forecast has no feature snapshot, so it cannot be reproduced"
    ),
    DataQualityIssueKind.REMEDIATION_WITHOUT_AUTHORIZATION: (
        "a remediation action reached execution without an authorization record"
    ),
    DataQualityIssueKind.KNOWLEDGE_WITHOUT_EVIDENCE: (
        "learned knowledge cites no supporting episodes"
    ),
    DataQualityIssueKind.STALE_COMPONENT: (
        "a component has received no telemetry for a long time"
    ),
    DataQualityIssueKind.MISSING_TELEMETRY: (
        "a component expected to emit telemetry emitted none"
    ),
    DataQualityIssueKind.BROKEN_RELATIONSHIP: (
        "a dependency or graph edge points at a component that is gone"
    ),
    DataQualityIssueKind.INCONSISTENT_STATE: (
        "two subsystems disagree about the same fact"
    ),
    DataQualityIssueKind.INVALID_EVIDENCE: (
        "an evidence row is present but empty or unreadable"
    ),
}

#: Suggested operator actions, by kind (§90).
ISSUE_SUGGESTIONS: dict[DataQualityIssueKind, str] = {
    DataQualityIssueKind.ORPHANED_RECORD: (
        "review the referencing row; if the parent was deleted intentionally, "
        "the reference should be cleared by an explicit, audited decision"
    ),
    DataQualityIssueKind.INCIDENT_WITHOUT_COMPONENT: (
        "attribute the incident to a component, or accept it as unlocated"
    ),
    DataQualityIssueKind.PREDICTION_WITHOUT_SNAPSHOT: (
        "re-run forecasting for this scope so the prediction has a snapshot"
    ),
    DataQualityIssueKind.REMEDIATION_WITHOUT_AUTHORIZATION: (
        "investigate the authorization path; an executed action must have one"
    ),
    DataQualityIssueKind.KNOWLEDGE_WITHOUT_EVIDENCE: (
        "review the pattern in the intelligence workspace and confirm or reject it"
    ),
    DataQualityIssueKind.STALE_COMPONENT: (
        "reconnect telemetry for this component, or mark it as retired"
    ),
    DataQualityIssueKind.MISSING_TELEMETRY: (
        "reconnect telemetry, or confirm the component is expected to be silent"
    ),
    DataQualityIssueKind.BROKEN_RELATIONSHIP: (
        "re-run graph reconciliation, or remove the stale relationship explicitly"
    ),
    DataQualityIssueKind.INCONSISTENT_STATE: (
        "review both sides and correct the subsystem that is wrong"
    ),
    DataQualityIssueKind.INVALID_EVIDENCE: (
        "review the evidence row; empty evidence should not be cited"
    ),
}


@dataclass
class QualityFinding:
    """One detected inconsistency, before it is stored."""

    kind: DataQualityIssueKind
    subject_type: str
    subject_id: uuid.UUID
    title: str
    severity: DataQualitySeverity = DataQualitySeverity.WARNING
    detail: Optional[str] = None
    evidence: Optional[dict[str, Any]] = None
    component_id: Optional[uuid.UUID] = None
    environment_id: Optional[uuid.UUID] = None


@dataclass
class QualityRunResult:
    """What one consistency pass found."""

    checked: int = 0
    findings: list[QualityFinding] = field(default_factory=list)
    opened: int = 0
    updated: int = 0
    resolved: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        for finding in self.findings:
            by_kind[finding.kind.value] = by_kind.get(finding.kind.value, 0) + 1
        return {
            "checked": self.checked,
            "findings": len(self.findings),
            "by_kind": by_kind,
            "opened": self.opened,
            "updated": self.updated,
            "resolved": self.resolved,
            "errors": list(self.errors),
        }


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------
async def _check_incidents_without_component(
    session: AsyncSession, *, project_id: uuid.UUID, limit: int
) -> list[QualityFinding]:
    rows = (
        await session.scalars(
            select(Incident)
            .where(
                Incident.project_id == project_id,
                Incident.primary_component_id.is_(None),
            )
            .order_by(Incident.detected_at.desc())
            .limit(limit)
        )
    ).all()
    if not rows:
        return []
    #: The anomaly count is fetched with an explicit grouped query rather than by
    #: reading ``incident.anomalies``: that relationship is lazy, and touching it
    #: under an async session raises ``MissingGreenlet``. Reads must be explicit
    #: here, not only for speed.
    anomaly_counts: dict[uuid.UUID, int] = {
        incident_id: count
        for incident_id, count in (
            await session.execute(
                select(Anomaly.incident_id, func.count())
                .where(Anomaly.incident_id.in_([row.id for row in rows]))
                .group_by(Anomaly.incident_id)
            )
        ).all()
        if incident_id is not None
    }
    return [
        QualityFinding(
            kind=DataQualityIssueKind.INCIDENT_WITHOUT_COMPONENT,
            subject_type="incident",
            subject_id=incident.id,
            title=f"Incident '{incident.title}' affects no component",
            severity=DataQualitySeverity.INFO,
            detail=(
                "the incident has no primary component, so its impact cannot be "
                "located on the system map or attributed to a service"
            ),
            evidence={
                "status": getattr(incident.status, "value", str(incident.status)),
                "fingerprint": incident.fingerprint,
                "anomaly_count": int(anomaly_counts.get(incident.id, 0)),
            },
            environment_id=incident.environment_id,
        )
        for incident in rows
    ]


async def _check_predictions_without_snapshot(
    session: AsyncSession, *, project_id: uuid.UUID, limit: int
) -> list[QualityFinding]:
    from app.models.reliability import ReliabilityForecast

    rows = (
        await session.scalars(
            select(ReliabilityForecast)
            .where(
                ReliabilityForecast.project_id == project_id,
                #: The link lives on the forecast (``feature_snapshot_id``), not on
                #: the snapshot: a snapshot is written first and the forecast
                #: points at it (§17 of Phase 8). A NULL pointer is the orphan.
                ReliabilityForecast.feature_snapshot_id.is_(None),
            )
            .order_by(ReliabilityForecast.generated_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        QualityFinding(
            kind=DataQualityIssueKind.PREDICTION_WITHOUT_SNAPSHOT,
            subject_type="forecast",
            subject_id=forecast.id,
            title="Forecast has no feature snapshot",
            detail=(
                "a forecast without its feature snapshot cannot be explained or "
                "reproduced, which the predictive phase guarantees"
            ),
            evidence={
                "risk_level": getattr(
                    forecast.risk_level, "value", str(forecast.risk_level)
                ),
                "prediction_type": getattr(
                    forecast.prediction_type, "value", str(forecast.prediction_type)
                ),
            },
            component_id=forecast.component_id,
            environment_id=forecast.environment_id,
        )
        for forecast in rows
    ]


async def _check_remediations_without_authorization(
    session: AsyncSession, *, project_id: uuid.UUID, limit: int
) -> list[QualityFinding]:
    from app.models.remediation import (
        RemediationAction,
        RemediationApproval,
        RemediationExecution,
        RemediationStatus,
    )

    executed_statuses = [
        RemediationStatus.EXECUTING,
        RemediationStatus.VERIFYING,
        RemediationStatus.VERIFIED,
        RemediationStatus.FAILED,
        RemediationStatus.ROLLING_BACK,
        RemediationStatus.ROLLED_BACK,
    ]
    rows = (
        await session.scalars(
            select(RemediationAction)
            .where(
                RemediationAction.project_id == project_id,
                RemediationAction.status.in_(executed_statuses),
                ~select(RemediationApproval.id)
                .where(RemediationApproval.action_id == RemediationAction.id)
                .exists(),
                ~select(RemediationExecution.id)
                .where(
                    RemediationExecution.action_id == RemediationAction.id,
                    RemediationExecution.status == "SUCCEEDED",
                )
                .exists(),
            )
            .order_by(RemediationAction.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        QualityFinding(
            kind=DataQualityIssueKind.REMEDIATION_WITHOUT_AUTHORIZATION,
            subject_type="remediation_action",
            subject_id=action.id,
            title="Remediation action has no approval and no successful execution",
            severity=DataQualitySeverity.CRITICAL,
            detail=(
                "Phase 9 requires an authorization before an action executes; this "
                "row is in an executed state with neither an approval nor a "
                "successful execution record"
            ),
            evidence={
                "status": getattr(action.status, "value", str(action.status)),
                "action_type": getattr(
                    action.action_type, "value", str(action.action_type)
                ),
                "execution_mode": getattr(
                    action.execution_mode, "value", str(action.execution_mode)
                ),
            },
            component_id=action.component_id,
            environment_id=action.environment_id,
        )
        for action in rows
    ]


async def _check_knowledge_without_evidence(
    session: AsyncSession, *, project_id: uuid.UUID, limit: int
) -> list[QualityFinding]:
    try:
        from app.models.intelligence import (
            KnowledgeStatus,
            ReliabilityKnowledge,
        )

        rows = (
            await session.scalars(
                select(ReliabilityKnowledge)
                .where(
                    ReliabilityKnowledge.project_id == project_id,
                    ReliabilityKnowledge.status.notin_(
                        [KnowledgeStatus.DEPRECATED, KnowledgeStatus.REJECTED]
                    ),
                    ReliabilityKnowledge.sample_count <= 0,
                )
                .order_by(ReliabilityKnowledge.created_at.desc())
                .limit(limit)
            )
        ).all()
    except Exception as exc:  # pragma: no cover - learning is optional
        logger.info("knowledge evidence check skipped: %s", exc)
        return []
    return [
        QualityFinding(
            kind=DataQualityIssueKind.KNOWLEDGE_WITHOUT_EVIDENCE,
            subject_type="knowledge",
            subject_id=knowledge.id,
            title="Learned knowledge cites no supporting episodes",
            detail=(
                "a pattern with zero supporting samples cannot be justified, and "
                "the governance rules say provenance is required"
            ),
            evidence={
                "status": getattr(knowledge.status, "value", str(knowledge.status)),
                "sample_count": knowledge.sample_count,
            },
        )
        for knowledge in rows
    ]


async def _check_stale_components(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    limit: int,
    stale_after: timedelta,
    now: datetime,
) -> list[QualityFinding]:
    from app.models.observability import ObservabilityEvent

    last_seen = (
        select(
            ObservabilityEvent.component_id,
            func.max(ObservabilityEvent.timestamp).label("last_seen"),
        )
        .where(
            ObservabilityEvent.project_id == project_id,
            ObservabilityEvent.component_id.is_not(None),
        )
        .group_by(ObservabilityEvent.component_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(SystemComponent, last_seen.c.last_seen)
            .outerjoin(last_seen, last_seen.c.component_id == SystemComponent.id)
            .where(SystemComponent.project_id == project_id)
            .limit(limit)
        )
    ).all()
    findings: list[QualityFinding] = []
    for component, last_seen_at in rows:
        moment = _aware(last_seen_at)
        if moment is None:
            findings.append(
                QualityFinding(
                    kind=DataQualityIssueKind.MISSING_TELEMETRY,
                    subject_type="component",
                    subject_id=component.id,
                    title=f"Component '{component.name}' has never emitted telemetry",
                    severity=DataQualitySeverity.INFO,
                    detail=(
                        "no observability event has ever been attributed to this "
                        "component, so its state is UNKNOWN"
                    ),
                    evidence={"component_name": component.name},
                    component_id=component.id,
                    environment_id=component.environment_id,
                )
            )
        elif now - moment > stale_after:
            findings.append(
                QualityFinding(
                    kind=DataQualityIssueKind.STALE_COMPONENT,
                    subject_type="component",
                    subject_id=component.id,
                    title=f"Component '{component.name}' has gone quiet",
                    detail=(
                        f"the last telemetry arrived "
                        f"{int((now - moment).total_seconds() // 3600)}h ago"
                    ),
                    evidence={
                        "component_name": component.name,
                        "last_telemetry_at": moment.isoformat(),
                    },
                    component_id=component.id,
                    environment_id=component.environment_id,
                )
            )
    return findings


async def _check_broken_relationships(
    session: AsyncSession, *, project_id: uuid.UUID, limit: int
) -> list[QualityFinding]:
    from app.models.system import ComponentDependency

    component_ids = select(SystemComponent.id).where(
        SystemComponent.project_id == project_id
    )
    rows = (
        await session.scalars(
            select(ComponentDependency)
            .where(
                #: ``component_dependencies`` carries no project column; the scope
                #: comes from the component ids of this project (§42).
                (
                    ComponentDependency.source_component_id.notin_(component_ids)
                    | ComponentDependency.target_component_id.notin_(component_ids)
                ),
            )
            .limit(limit)
        )
    ).all()
    return [
        QualityFinding(
            kind=DataQualityIssueKind.BROKEN_RELATIONSHIP,
            subject_type="component_dependency",
            subject_id=dependency.id,
            title="A dependency points at a component that no longer exists",
            detail=(
                "the dependency survives one of its endpoints, so the system map "
                "would show an edge into nothing"
            ),
            evidence={
                "source_component_id": str(dependency.source_component_id),
                "target_component_id": str(dependency.target_component_id),
            },
        )
        for dependency in rows
    ]


async def _check_inconsistent_state(
    session: AsyncSession, *, project_id: uuid.UUID, limit: int
) -> list[QualityFinding]:
    """The §5 cross-system consistency check, made concrete.

    A resolved incident whose component is still reporting DEGRADED is exactly
    the contradiction §5 names. The check does not decide which side is wrong —
    it reports both, which is what an operator needs to see.
    """
    from app.services.system_state import (
        UNRESOLVED_INCIDENT_STATUSES,
        derive_component_states,
    )

    resolved = (
        await session.scalars(
            select(Incident)
            .where(
                Incident.project_id == project_id,
                Incident.status.in_(["RESOLVED", "CLOSED"]),
                Incident.primary_component_id.is_not(None),
                Incident.resolved_at.is_not(None),
            )
            .order_by(Incident.resolved_at.desc())
            .limit(limit)
        )
    ).all()
    if not resolved:
        return []

    #: ``primary_component_id`` is nullable, and the query above already excludes
    #: the NULLs for this check — but narrowing here keeps the call honest instead
    #: of relying on a remote invariant.
    component_ids = [
        row.primary_component_id for row in resolved if row.primary_component_id
    ]
    if not component_ids:
        return []
    states = await derive_component_states(
        session, project_id=project_id, component_ids=component_ids
    )
    by_component = {state.component_id: state for state in states}

    findings: list[QualityFinding] = []
    for incident in resolved:
        #: The column is nullable; an incident with no component is already a
        #: separate finding, so it is skipped here rather than keyed on None.
        if incident.primary_component_id is None:
            continue
        state = by_component.get(incident.primary_component_id)
        if state is None:
            continue
        if state.state.value != "DEGRADED":
            continue
        #: Only a *newer* incident on the same component makes this consistent;
        #: without one, the two subsystems disagree.
        newer = await session.scalar(
            select(func.count(Incident.id)).where(
                Incident.project_id == project_id,
                Incident.primary_component_id == incident.primary_component_id,
                Incident.status.in_(list(UNRESOLVED_INCIDENT_STATUSES)),
            )
        )
        if newer:
            continue
        findings.append(
            QualityFinding(
                kind=DataQualityIssueKind.INCONSISTENT_STATE,
                subject_type="incident",
                subject_id=incident.id,
                title=("Incident is resolved but its component is still DEGRADED"),
                severity=DataQualitySeverity.CRITICAL,
                detail=(
                    "the incident subsystem reports RESOLVED while the derived "
                    "component state reports DEGRADED from an unresolved "
                    "HIGH/CRITICAL anomaly"
                ),
                evidence={
                    "incident_status": getattr(
                        incident.status, "value", str(incident.status)
                    ),
                    "component_state": state.state.value,
                    "component_state_reason": state.reason,
                    "component_id": str(incident.primary_component_id),
                },
                component_id=incident.primary_component_id,
                environment_id=incident.environment_id,
            )
        )
    return findings


#: The checks, in the order they are run. Declared as data so the sweep, the API
#: and the tests cannot disagree about what "checks 7 of 7" means.
CHECKS = (
    "incidents_without_component",
    "predictions_without_snapshot",
    "remediations_without_authorization",
    "knowledge_without_evidence",
    "stale_components",
    "broken_relationships",
    "inconsistent_state",
)


async def run_consistency_checks(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
    persist: bool = True,
) -> QualityRunResult:
    """Run every check for one project and store the findings (§88)."""
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    batch = settings.PLATFORM_DATA_QUALITY_BATCH
    result = QualityRunResult()

    runners: tuple[tuple[str, Any, dict[str, Any]], ...] = (
        ("incidents_without_component", _check_incidents_without_component, {}),
        ("predictions_without_snapshot", _check_predictions_without_snapshot, {}),
        (
            "remediations_without_authorization",
            _check_remediations_without_authorization,
            {},
        ),
        ("knowledge_without_evidence", _check_knowledge_without_evidence, {}),
        (
            "stale_components",
            _check_stale_components,
            {
                "stale_after": timedelta(days=settings.PLATFORM_STALE_COMPONENT_DAYS),
                "now": moment,
            },
        ),
        ("broken_relationships", _check_broken_relationships, {}),
        ("inconsistent_state", _check_inconsistent_state, {}),
    )

    for name, runner, extra in runners:
        result.checked += 1
        try:
            findings = await runner(
                session, project_id=project_id, limit=batch, **extra
            )
        except Exception as exc:
            #: One failing check must not stop the others: §60's failure
            #: isolation applies to the platform's own diagnostics.
            logger.warning("data-quality check %s failed", name, exc_info=True)
            result.errors.append(f"{name}: {exc}")
            continue
        result.findings.extend(findings)

    if not persist:
        return result

    seen: set[tuple[str, str, uuid.UUID]] = set()
    for finding in result.findings:
        seen.add((finding.kind.value, finding.subject_type, finding.subject_id))
        opened = await upsert_issue(
            session, project_id=project_id, finding=finding, now=moment
        )
        if opened:
            result.opened += 1
        else:
            result.updated += 1

    result.resolved = await resolve_disappeared(
        session, project_id=project_id, seen=seen, now=moment
    )
    return result


async def upsert_issue(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    finding: QualityFinding,
    now: Optional[datetime] = None,
) -> bool:
    """Store a finding, refreshing its occurrence count. Returns True if new.

    One row per (kind, subject) — a check that fails every sweep for a week is one
    issue with a count of seven, not seven issues.
    """
    moment = _aware(now) or datetime.now(timezone.utc)
    existing = (
        await session.scalars(
            select(DataQualityIssue).where(
                DataQualityIssue.project_id == project_id,
                DataQualityIssue.kind == finding.kind,
                DataQualityIssue.subject_type == finding.subject_type,
                DataQualityIssue.subject_id == finding.subject_id,
            )
        )
    ).first()

    if existing is not None:
        existing.last_seen_at = moment
        existing.occurrence_count += 1
        existing.detail = finding.detail
        existing.evidence = finding.evidence
        existing.suggestion = ISSUE_SUGGESTIONS.get(finding.kind)
        if existing.status in (DataQualityStatus.RESOLVED, DataQualityStatus.IGNORED):
            #: It came back. Reopening is honest; staying resolved is not.
            existing.status = DataQualityStatus.OPEN
            existing.resolved_at = None
            existing.resolved_by = None
        await session.flush()
        return False

    issue = DataQualityIssue(
        project_id=project_id,
        environment_id=finding.environment_id,
        kind=finding.kind,
        severity=finding.severity,
        status=DataQualityStatus.OPEN,
        subject_type=finding.subject_type,
        subject_id=finding.subject_id,
        component_id=finding.component_id,
        title=finding.title[:500],
        detail=finding.detail,
        evidence=finding.evidence,
        suggestion=ISSUE_SUGGESTIONS.get(finding.kind),
        detected_at=moment,
        last_seen_at=moment,
        occurrence_count=1,
    )
    session.add(issue)
    await session.flush()

    from app.services.platform_events import safely_publish_event

    await safely_publish_event(
        session,
        project_id=project_id,
        environment_id=finding.environment_id,
        event_type=PlatformEventType.DATA_QUALITY_ISSUE,
        source="data_quality_center",
        subject_type=finding.subject_type,
        subject_id=finding.subject_id,
        component_id=finding.component_id,
        occurred_at=moment,
        payload={
            "kind": finding.kind.value,
            "severity": finding.severity.value,
            "title": finding.title,
        },
    )
    return True


async def resolve_disappeared(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    seen: set[tuple[str, str, uuid.UUID]],
    now: Optional[datetime] = None,
) -> int:
    """Mark open issues that no longer reproduce as RESOLVED (§90)."""
    moment = _aware(now) or datetime.now(timezone.utc)
    open_issues = (
        await session.scalars(
            select(DataQualityIssue).where(
                DataQualityIssue.project_id == project_id,
                DataQualityIssue.status == DataQualityStatus.OPEN,
            )
        )
    ).all()
    resolved = 0
    for issue in open_issues:
        key = (issue.kind.value, issue.subject_type, issue.subject_id)
        if key in seen:
            continue
        issue.status = DataQualityStatus.RESOLVED
        issue.resolved_at = moment
        issue.resolved_by = "consistency-check"
        resolved += 1
    if resolved:
        await session.flush()
    return resolved


async def quality_summary(
    session: AsyncSession, *, project_id: uuid.UUID
) -> dict[str, Any]:
    """The dashboard's data-quality strip."""
    rows = (
        await session.execute(
            select(DataQualityIssue.kind, DataQualityIssue.severity, func.count())
            .where(
                DataQualityIssue.project_id == project_id,
                DataQualityIssue.status == DataQualityStatus.OPEN,
            )
            .group_by(DataQualityIssue.kind, DataQualityIssue.severity)
        )
    ).all()
    by_kind: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    total = 0
    for kind, severity, count in rows:
        by_kind[kind.value] = by_kind.get(kind.value, 0) + int(count)
        by_severity[severity.value] = by_severity.get(severity.value, 0) + int(count)
        total += int(count)
    return {
        "open_issues": total,
        "by_kind": by_kind,
        "by_severity": by_severity,
        "healthy": total == 0,
        "note": (
            "no open consistency issues were found"
            if total == 0
            else f"{total} consistency issue(s) are open"
        ),
    }


async def list_issues(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    statuses: Optional[Sequence[DataQualityStatus]] = None,
    kinds: Optional[Sequence[DataQualityIssueKind]] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[DataQualityIssue]:
    stmt = (
        select(DataQualityIssue)
        .where(DataQualityIssue.project_id == project_id)
        .order_by(DataQualityIssue.last_seen_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if statuses:
        stmt = stmt.where(DataQualityIssue.status.in_(list(statuses)))
    if kinds:
        stmt = stmt.where(DataQualityIssue.kind.in_(list(kinds)))
    return list((await session.scalars(stmt)).all())


async def set_issue_status(
    session: AsyncSession,
    *,
    issue: DataQualityIssue,
    status: DataQualityStatus,
    actor: Optional[str] = None,
    now: Optional[datetime] = None,
) -> DataQualityIssue:
    """A person's decision about an issue — the only status change we do not make."""
    moment = _aware(now) or datetime.now(timezone.utc)
    issue.status = status
    if status in (DataQualityStatus.RESOLVED, DataQualityStatus.IGNORED):
        issue.resolved_at = moment
        issue.resolved_by = actor
    else:
        issue.resolved_at = None
        issue.resolved_by = None
    await session.flush()
    return issue


__all__ = [
    "CHECKS",
    "ISSUE_DESCRIPTIONS",
    "ISSUE_SUGGESTIONS",
    "QualityFinding",
    "QualityRunResult",
    "list_issues",
    "quality_summary",
    "resolve_disappeared",
    "run_consistency_checks",
    "set_issue_status",
    "upsert_issue",
]
