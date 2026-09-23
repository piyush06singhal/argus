"""ARGUS Change Intelligence (Phase 11 §38–§41, §84).

One view of what changed, and what reliability did afterwards.

The rule the whole module is built around, restated because it is easy to lose
in a deployment-correlation feature:

    correlation ≠ causation

A deployment 40 minutes before an incident is a *change worth looking at*, not a
cause, and nothing here phrases it otherwise. The code goes further than the
prose: the change-failure-rate definition (§84) counts a deployment as
incident-associated only when an incident actually began inside the window, and
even then it is reported as an association count with its window stated, never as
"this deployment caused an incident".

§39's deployment risk view is *advisory*. There is deliberately no code path from
this module to blocking a deployment: the phase says not to block without an
explicit future governance policy, so the module has no opinion to leak.
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
from app.services.platform_time import aware as _aware
from app.models.deployment import DeploymentEvent, DeploymentStatus
from app.models.incident import Incident, IncidentStatus
from app.models.ingestion import ConfigurationChangeEvent
from app.models.project import Environment

logger = logging.getLogger(__name__)

#: Deployment statuses that count as a *failed* delivery for §84.
FAILED_DEPLOYMENT_STATUSES = (DeploymentStatus.FAILED, DeploymentStatus.ROLLED_BACK)


@dataclass
class ChangeEntry:
    """One change of any kind, normalized for the unified view (§38)."""

    kind: str  # DEPLOYMENT | CONFIGURATION
    id: str
    occurred_at: str
    summary: str
    environment_id: Optional[str] = None
    component_id: Optional[str] = None
    status: Optional[str] = None
    reference: Optional[str] = None
    detail: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "occurred_at": self.occurred_at,
            "summary": self.summary,
            "environment_id": self.environment_id,
            "component_id": self.component_id,
            "status": self.status,
            "reference": self.reference,
            "detail": self.detail,
            "metadata": self.metadata,
        }


async def recent_changes(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Deployments and configuration changes in one merged, newest-first list.

    Two queries, one merge, hard caps — the unified change view is the kind of
    read that quietly becomes N+1 if each entry resolves its own component.
    """
    moment = _aware(until) or datetime.now(timezone.utc)
    start = _aware(since) or moment - timedelta(days=7)
    entries: list[ChangeEntry] = []

    deployment_stmt = (
        select(DeploymentEvent)
        .where(
            DeploymentEvent.project_id == project_id,
            DeploymentEvent.deployed_at >= start,
            DeploymentEvent.deployed_at <= moment,
        )
        .order_by(DeploymentEvent.deployed_at.desc())
        .limit(limit)
    )
    if environment_id is not None:
        deployment_stmt = deployment_stmt.where(
            DeploymentEvent.environment_id == environment_id
        )
    for deployment in (await session.scalars(deployment_stmt)).all():
        status = getattr(deployment.status, "value", str(deployment.status))
        entries.append(
            ChangeEntry(
                kind="DEPLOYMENT",
                id=str(deployment.id),
                occurred_at=_aware(deployment.deployed_at).isoformat(),
                summary=(
                    f"Deployment {deployment.version or deployment.commit_sha or 'unknown'}"
                    f" ({status})"
                ),
                environment_id=str(deployment.environment_id)
                if deployment.environment_id
                else None,
                component_id=str(deployment.component_id)
                if deployment.component_id
                else None,
                status=status,
                reference=deployment.commit_sha,
                detail=deployment.description,
                metadata={"version": deployment.version},
            )
        )

    config_stmt = (
        select(ConfigurationChangeEvent)
        .where(
            ConfigurationChangeEvent.project_id == project_id,
            ConfigurationChangeEvent.timestamp >= start,
            ConfigurationChangeEvent.timestamp <= moment,
        )
        .order_by(ConfigurationChangeEvent.timestamp.desc())
        .limit(limit)
    )
    if environment_id is not None:
        config_stmt = config_stmt.where(
            ConfigurationChangeEvent.environment_id == environment_id
        )
    for change in (await session.scalars(config_stmt)).all():
        entries.append(
            ChangeEntry(
                kind="CONFIGURATION",
                id=str(change.id),
                occurred_at=_aware(change.timestamp).isoformat(),
                summary=f"Configuration change {change.change_id}",
                environment_id=str(change.environment_id)
                if change.environment_id
                else None,
                component_id=str(change.component_id) if change.component_id else None,
                reference=change.change_id,
                detail=change.description,
                metadata={"source": change.source},
            )
        )

    entries.sort(key=lambda entry: entry.occurred_at, reverse=True)
    return [entry.as_dict() for entry in entries[:limit]]


@dataclass
class DeploymentReliabilityView:
    """The §40 view for one deployment."""

    deployment: dict[str, Any]
    incidents_after: list[dict[str, Any]] = field(default_factory=list)
    rollback: Optional[dict[str, Any]] = None
    similar_deployments: list[dict[str, Any]] = field(default_factory=list)
    risk_signals: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "deployment": self.deployment,
            "incidents_after": self.incidents_after,
            "rollback": self.rollback,
            "similar_deployments": self.similar_deployments,
            "risk_signals": self.risk_signals,
            "limitations": list(self.limitations),
        }


async def deployment_reliability_view(
    session: AsyncSession,
    *,
    deployment: DeploymentEvent,
    settings: Optional[Settings] = None,
) -> DeploymentReliabilityView:
    """Everything ARGUS knows about one deployment's reliability (§40).

    The incident window is stated in the output, because "3 incidents after this
    deployment" means nothing without knowing the window.
    """
    settings = settings or get_settings()
    moment = datetime.now(timezone.utc)
    window = timedelta(hours=settings.PLATFORM_DEPLOYMENT_INCIDENT_WINDOW_HOURS)
    deployed_at = _aware(deployment.deployed_at)

    view = DeploymentReliabilityView(
        deployment={
            "id": str(deployment.id),
            "version": deployment.version,
            "commit_sha": deployment.commit_sha,
            "status": getattr(deployment.status, "value", str(deployment.status)),
            "environment_id": str(deployment.environment_id)
            if deployment.environment_id
            else None,
            "component_id": str(deployment.component_id)
            if deployment.component_id
            else None,
            "deployed_at": deployed_at.isoformat(),
        }
    )

    incident_stmt = (
        select(Incident)
        .where(
            Incident.project_id == deployment.project_id,
            Incident.detected_at >= deployed_at,
            Incident.detected_at <= deployed_at + window,
            #: Also bounded above by *now*: a clock-skewed row dated in the future
            #: must not be attributed to this deployment.
            Incident.detected_at <= moment,
        )
        .order_by(Incident.detected_at)
        .limit(25)
    )
    if deployment.component_id is not None:
        incident_stmt = incident_stmt.where(
            Incident.primary_component_id == deployment.component_id
        )
    for incident in (await session.scalars(incident_stmt)).all():
        view.incidents_after.append(
            {
                "id": str(incident.id),
                "title": incident.title,
                "severity": getattr(incident.severity, "value", str(incident.severity)),
                "status": getattr(incident.status, "value", str(incident.status)),
                "detected_at": _aware(incident.detected_at).isoformat(),
                "minutes_after": round(
                    (_aware(incident.detected_at) - deployed_at).total_seconds() / 60.0,
                    1,
                ),
            }
        )

    if getattr(deployment.status, "value", None) == DeploymentStatus.ROLLED_BACK.value:
        view.rollback = {
            "status": DeploymentStatus.ROLLED_BACK.value,
            "observed_at": deployed_at.isoformat(),
            "note": "the deployment itself records a rollback",
        }
        view.risk_signals.append(
            {
                "signal": "DEPLOYMENT_ROLLED_BACK",
                "detail": "this deployment was rolled back",
            }
        )

    if deployment.component_id is not None:
        prior_stmt = (
            select(DeploymentEvent)
            .where(
                DeploymentEvent.project_id == deployment.project_id,
                DeploymentEvent.component_id == deployment.component_id,
                DeploymentEvent.id != deployment.id,
                DeploymentEvent.deployed_at < deployed_at,
            )
            .order_by(DeploymentEvent.deployed_at.desc())
            .limit(settings.PLATFORM_DEPLOYMENT_SIMILARITY_LIMIT)
        )
        prior = list((await session.scalars(prior_stmt)).all())
        for candidate in prior:
            prior_status = getattr(candidate.status, "value", str(candidate.status))
            view.similar_deployments.append(
                {
                    "id": str(candidate.id),
                    "version": candidate.version,
                    "status": prior_status,
                    "deployed_at": _aware(candidate.deployed_at).isoformat(),
                }
            )
            if prior_status in [s.value for s in FAILED_DEPLOYMENT_STATUSES]:
                view.risk_signals.append(
                    {
                        "signal": "PRIOR_DEPLOYMENT_FAILURE",
                        "detail": (
                            f"a previous deployment of this component "
                            f"({candidate.version or candidate.commit_sha}) ended "
                            f"{prior_status}"
                        ),
                    }
                )

    if not view.incidents_after:
        view.limitations.append(
            f"no incident began within {settings.PLATFORM_DEPLOYMENT_INCIDENT_WINDOW_HOURS}h "
            "of this deployment — which is not evidence that it was safe, only "
            "that nothing was recorded in that window"
        )
    else:
        view.limitations.append(
            "incidents inside the window are *associated* with this deployment by "
            "time alone; ARGUS does not claim the deployment caused them"
        )
    return view


async def change_risk_view(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    deployment: DeploymentEvent,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """The §39 advisory view for a new deployment.

    Advisory by construction: it returns signals and a summary, and there is no
    field or code path that could block or delay the deployment.
    """
    settings = settings or get_settings()
    base = await deployment_reliability_view(
        session, deployment=deployment, settings=settings
    )
    signals = list(base.risk_signals)

    if deployment.component_id is not None:
        from app.models.reliability import (
            ForecastRiskLevel,
            ReliabilityForecast,
        )

        forecast = (
            await session.scalars(
                select(ReliabilityForecast)
                .where(
                    ReliabilityForecast.project_id == project_id,
                    ReliabilityForecast.component_id == deployment.component_id,
                    ReliabilityForecast.risk_level.in_(
                        [ForecastRiskLevel.HIGH, ForecastRiskLevel.CRITICAL]
                    ),
                )
                .order_by(ReliabilityForecast.generated_at.desc())
                .limit(1)
            )
        ).first()
        if forecast is not None:
            signals.append(
                {
                    "signal": "ELEVATED_PREDICTED_RISK",
                    "detail": (
                        f"the affected component is predicted "
                        f"{getattr(forecast.risk_level, 'value', forecast.risk_level)} "
                        f"risk ({forecast.prediction_type.value})"
                    ),
                    "forecast_id": str(forecast.id),
                }
            )

        affected = await affected_components(
            session, component_id=deployment.component_id
        )
        base.limitations.append(
            f"{len(affected)} dependent component(s) could be affected by a change here"
        )
    else:
        affected = []

    return {
        "deployment": base.deployment,
        "advisory": True,
        "summary": _risk_summary(signals),
        "signals": signals,
        "components_changed": [
            str(deployment.component_id) if deployment.component_id else None
        ],
        "components_affected": affected,
        "historical_incidents": base.incidents_after,
        "similar_deployments": base.similar_deployments,
        "limitations": base.limitations
        + [
            "this view is advisory: ARGUS does not block or delay deployments "
            "without an explicit governance policy"
        ],
    }


def _risk_summary(signals: Sequence[dict[str, Any]]) -> str:
    if not signals:
        return "no risk signals are attached to this deployment"
    return "; ".join(str(signal.get("detail")) for signal in signals[:3])


async def affected_components(
    session: AsyncSession, *, component_id: uuid.UUID, limit: int = 50
) -> list[str]:
    """Direct dependents of a component, one hop, bounded (§22, §65)."""
    from app.models.system import ComponentDependency

    stmt = (
        select(ComponentDependency.target_component_id)
        .where(ComponentDependency.source_component_id == component_id)
        .limit(limit)
    )
    return [str(row) for row in (await session.scalars(stmt)).all()]


# ---------------------------------------------------------------------------
# §41 environment comparison
# ---------------------------------------------------------------------------
async def compare_environments(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    left_environment_id: uuid.UUID,
    right_environment_id: uuid.UUID,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """Compare two environments on the same dimensions (§41).

    Both sides are measured the same way so the comparison is meaningful; where a
    dimension cannot be measured, it is reported as ``null`` with a reason rather
    than as zero, because zero is a number and "we could not see" is not.
    """
    settings = settings or get_settings()
    left = await session.get(Environment, left_environment_id)
    right = await session.get(Environment, right_environment_id)
    if left is None or left.project_id != project_id:
        raise ValueError("left environment not found in this project")
    if right is None or right.project_id != project_id:
        raise ValueError("right environment not found in this project")

    async def _side(environment: Environment) -> dict[str, Any]:
        from app.models.anomaly import Anomaly, AnomalyStatus
        from app.models.system import SystemComponent

        component_count = await session.scalar(
            select(func.count(SystemComponent.id)).where(
                SystemComponent.project_id == project_id,
                SystemComponent.environment_id == environment.id,
            )
        )
        active_incidents = await session.scalar(
            select(func.count(Incident.id)).where(
                Incident.project_id == project_id,
                Incident.environment_id == environment.id,
                Incident.status.notin_(
                    [IncidentStatus.RESOLVED, IncidentStatus.CLOSED]
                ),
            )
        )
        open_anomalies = await session.scalar(
            select(func.count(Anomaly.id)).where(
                Anomaly.project_id == project_id,
                Anomaly.environment_id == environment.id,
                Anomaly.status != AnomalyStatus.RESOLVED,
            )
        )
        deployments = await session.scalar(
            select(func.count(DeploymentEvent.id)).where(
                DeploymentEvent.project_id == project_id,
                DeploymentEvent.environment_id == environment.id,
            )
        )
        recent_deployments = list(
            (
                await session.scalars(
                    select(DeploymentEvent)
                    .where(
                        DeploymentEvent.project_id == project_id,
                        DeploymentEvent.environment_id == environment.id,
                    )
                    .order_by(DeploymentEvent.deployed_at.desc())
                    .limit(settings.PLATFORM_COMPARISON_DEPLOYMENT_LIMIT)
                )
            ).all()
        )
        from app.services.system_state import build_system_state

        state = await build_system_state(
            session,
            project_id=project_id,
            environment_id=environment.id,
            settings=settings,
            include=("health", "predicted_risks"),
        )
        return {
            "environment_id": str(environment.id),
            "name": environment.name,
            "type": getattr(environment.environment_type, "value", None),
            "components": component_count or 0,
            "active_incidents": active_incidents or 0,
            "open_anomalies": open_anomalies or 0,
            "deployments_total": deployments or 0,
            "recent_deployments": [
                {
                    "id": str(deployment.id),
                    "version": deployment.version,
                    "status": getattr(
                        deployment.status, "value", str(deployment.status)
                    ),
                    "deployed_at": _aware(deployment.deployed_at).isoformat(),
                }
                for deployment in recent_deployments
            ],
            "health": state.health,
            "predicted_risks": state.predicted_risks,
            "limitations": state.limitations,
        }

    left_side = await _side(left)
    right_side = await _side(right)
    differences: list[dict[str, Any]] = []
    for key in (
        "components",
        "active_incidents",
        "open_anomalies",
    ):
        if left_side.get(key) != right_side.get(key):
            differences.append(
                {
                    "dimension": key,
                    "left": left_side.get(key),
                    "right": right_side.get(key),
                }
            )
    coverage_left = left_side["health"].get("coverage_percent")
    coverage_right = right_side["health"].get("coverage_percent")
    if coverage_left != coverage_right:
        differences.append(
            {
                "dimension": "telemetry_coverage_percent",
                "left": coverage_left,
                "right": coverage_right,
            }
        )
    notes = [
        "differences between environments are stated, never attributed: a "
        "development environment with fewer incidents is not evidence of a safer "
        "configuration"
    ]
    if coverage_left != coverage_right:
        notes.append(
            "the two environments have different telemetry coverage, so their "
            "health numbers are not directly comparable"
        )
    return {
        "project_id": str(project_id),
        "left": left_side,
        "right": right_side,
        "differences": differences,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# §84 change failure rate
# ---------------------------------------------------------------------------
@dataclass
class ChangeFailureRate:
    """The §84 metric with its methodology attached."""

    window_days: int
    deployments_total: int = 0
    deployments_succeeded: int = 0
    deployments_failed: int = 0
    deployments_rolled_back: int = 0
    deployments_with_incident: int = 0
    failure_rate: Optional[float] = None
    methodology: str = ""
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "deployments_total": self.deployments_total,
            "deployments_succeeded": self.deployments_succeeded,
            "deployments_failed": self.deployments_failed,
            "deployments_rolled_back": self.deployments_rolled_back,
            "deployments_with_incident": self.deployments_with_incident,
            "failure_rate": self.failure_rate,
            "methodology": self.methodology,
            "limitations": list(self.limitations),
        }


CHANGE_FAILURE_METHODOLOGY = (
    "failure_rate = (FAILED + ROLLED_BACK) / deployments with a recorded "
    "terminal status, over the window. Deployments whose status is UNKNOWN or "
    "still STARTED are excluded from the denominator rather than counted as "
    "successes — an unfinished deployment is not a successful one. "
    "deployments_with_incident counts deployments whose component saw an "
    "incident begin inside the deployment window; it is an *association* "
    "measure and is deliberately NOT part of failure_rate."
)


async def change_failure_rate(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    window_days: Optional[int] = None,
    settings: Optional[Settings] = None,
    now: Optional[datetime] = None,
) -> ChangeFailureRate:
    """Compute §84 with the exact definition above."""
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    days = window_days or settings.PLATFORM_CHANGE_FAILURE_WINDOW_DAYS
    window = timedelta(days=days)
    result = ChangeFailureRate(window_days=days, methodology=CHANGE_FAILURE_METHODOLOGY)

    deployments = list(
        (
            await session.scalars(
                select(DeploymentEvent)
                .where(
                    DeploymentEvent.project_id == project_id,
                    DeploymentEvent.deployed_at >= moment - window,
                    DeploymentEvent.deployed_at <= moment,
                )
                .order_by(DeploymentEvent.deployed_at.desc())
                .limit(2000)
            )
        ).all()
    )
    result.deployments_total = len(deployments)
    terminal = 0
    for deployment in deployments:
        status = getattr(deployment.status, "value", str(deployment.status))
        if status == DeploymentStatus.SUCCESS.value:
            result.deployments_succeeded += 1
            terminal += 1
        elif status == DeploymentStatus.FAILED.value:
            result.deployments_failed += 1
            terminal += 1
        elif status == DeploymentStatus.ROLLED_BACK.value:
            result.deployments_rolled_back += 1
            terminal += 1

    #: One bounded query for the association count, then a per-deployment lookup.
    #: Counting per deployment would be N+1 on the platform's own metrics path,
    #: which is exactly the shape §65 forbids.
    incident_window = timedelta(
        hours=settings.PLATFORM_DEPLOYMENT_INCIDENT_WINDOW_HOURS
    )
    if deployments:
        earliest = min(_aware(d.deployed_at) for d in deployments)
        incident_rows = (
            await session.execute(
                select(Incident.primary_component_id, Incident.detected_at).where(
                    Incident.project_id == project_id,
                    Incident.primary_component_id.is_not(None),
                    Incident.detected_at >= earliest,
                    Incident.detected_at <= moment,
                )
            )
        ).all()
        by_component: dict[uuid.UUID, list[datetime]] = {}
        for component_id, detected_at in incident_rows:
            by_component.setdefault(component_id, []).append(_aware(detected_at))
        for deployment in deployments:
            if deployment.component_id is None:
                continue
            deployed_at = _aware(deployment.deployed_at)
            if any(
                deployed_at <= detected <= deployed_at + incident_window
                for detected in by_component.get(deployment.component_id, [])
            ):
                result.deployments_with_incident += 1

    if terminal:
        result.failure_rate = round(
            (result.deployments_failed + result.deployments_rolled_back) / terminal, 4
        )
    else:
        result.limitations.append(
            "no deployment in the window has a terminal status, so no failure "
            "rate can be computed"
        )
    unfinished = result.deployments_total - terminal
    if unfinished:
        result.limitations.append(
            f"{unfinished} deployment(s) have no terminal status and were excluded "
            "from the denominator"
        )
    return result


__all__ = [
    "CHANGE_FAILURE_METHODOLOGY",
    "ChangeEntry",
    "ChangeFailureRate",
    "DeploymentReliabilityView",
    "affected_components",
    "change_failure_rate",
    "change_risk_view",
    "compare_environments",
    "deployment_reliability_view",
    "recent_changes",
]
