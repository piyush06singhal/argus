"""ARGUS Service Catalog (Phase 11 §30, §31).

The "what exists?" answer, assembled from what the earlier phases already know:
components from the system model, endpoints and ownership from the Phase 2 graph,
dependencies from Phase 0/2, health from the system-state derivation, incident and
remediation history from Phases 3/9, risk from Phase 8 and the reliability
profile from Phase 10.

Two rules from §31 shape the ownership model:

* **Do not invent owners.** With no ownership row, the catalog answers
  ``UNKNOWN`` — never a guess from a repository name, an email domain, or the
  last person who happened to commit.
* **Unknown is visible.** An unowned component is surfaced as an operational fact
  (nobody to page), not hidden. That is the difference between a catalog that
  helps during an incident and one that merely lists services.

Every section is bounded, and every unavailable section says so: a catalog entry
that silently omits its incident history reads as "this service never has
incidents".
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.services.platform_config import ConfigurationError
from app.services.platform_time import aware as _aware
from app.models.graph import ComponentOwner, ServiceEndpoint
from app.models.incident import Incident
from app.models.reliability import (
    ForecastRiskLevel,
    ForecastStatus,
    ReliabilityForecast,
)
from app.models.remediation import RemediationAction
from app.models.system import ComponentDependency, SystemComponent

logger = logging.getLogger(__name__)

#: The literal answer when a field is unknown. Exported so the API, the UI and the
#: tests use one spelling.
UNKNOWN = "UNKNOWN"


@dataclass
class CatalogEntry:
    """One service's catalog record (§30)."""

    component_id: uuid.UUID
    name: str
    component_type: str
    environment_id: Optional[str]
    environment_name: Optional[str] = None

    # -- §31 ownership
    owner: dict[str, Any] = field(
        default_factory=lambda: {
            "team": UNKNOWN,
            "owner": UNKNOWN,
            "on_call": UNKNOWN,
            "repository": UNKNOWN,
            "documentation": UNKNOWN,
            "known": False,
        }
    )

    # -- structure
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    dependents: list[dict[str, Any]] = field(default_factory=list)
    endpoints: list[dict[str, Any]] = field(default_factory=list)

    # -- operations
    state: str = "UNKNOWN"
    state_reason: Optional[str] = None
    available: Optional[bool] = None
    metrics: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    incident_history: list[dict[str, Any]] = field(default_factory=list)
    deployment_history: list[dict[str, Any]] = field(default_factory=list)
    remediation_history: list[dict[str, Any]] = field(default_factory=list)
    reliability_profile: Optional[dict[str, Any]] = None
    scorecard: Optional[dict[str, Any]] = None
    unavailable: dict[str, str] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "component_id": str(self.component_id),
            "name": self.name,
            "component_type": self.component_type,
            "environment_id": self.environment_id,
            "environment_name": self.environment_name,
            "owner": self.owner,
            "dependencies": self.dependencies,
            "dependents": self.dependents,
            "endpoints": self.endpoints,
            "state": self.state,
            "state_reason": self.state_reason,
            "available": self.available,
            "metrics": self.metrics,
            "risk": self.risk,
            "incident_history": self.incident_history,
            "deployment_history": self.deployment_history,
            "remediation_history": self.remediation_history,
            "reliability_profile": self.reliability_profile,
            "scorecard": self.scorecard,
            "unavailable": self.unavailable,
            "limitations": list(self.limitations),
        }


async def list_catalog(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = None,
    limit: int = 200,
    offset: int = 0,
    settings: Optional[Settings] = None,
) -> list[CatalogEntry]:
    """The catalog index: every component with its ownership and live state.

    Deliberately lighter than :func:`catalog_entry`: the index loads ownership,
    endpoints, dependencies and derived state in bulk, and leaves the expensive
    per-component history to the detail view.
    """
    settings = settings or get_settings()
    stmt = (
        select(SystemComponent)
        .where(SystemComponent.project_id == project_id)
        .order_by(SystemComponent.name)
        .limit(limit)
        .offset(offset)
    )
    if environment_id is not None:
        stmt = stmt.where(SystemComponent.environment_id == environment_id)
    components = list((await session.scalars(stmt)).all())
    if not components:
        return []

    component_ids = [component.id for component in components]
    from app.models.project import Environment

    environments = {
        environment.id: environment
        for environment in (
            await session.scalars(
                select(Environment).where(Environment.project_id == project_id)
            )
        ).all()
    }

    owners = {
        owner.component_id: owner
        for owner in (
            await session.scalars(
                select(ComponentOwner).where(
                    ComponentOwner.component_id.in_(component_ids)
                )
            )
        ).all()
    }
    endpoints: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for endpoint in (
        await session.scalars(
            select(ServiceEndpoint).where(
                ServiceEndpoint.component_id.in_(component_ids)
            )
        )
    ).all():
        endpoints.setdefault(endpoint.component_id, []).append(
            {
                "method": endpoint.method,
                "path": endpoint.path_template,
                "external": endpoint.is_external,
            }
        )
    dependencies: dict[uuid.UUID, list[dict[str, Any]]] = {}
    dependents: dict[uuid.UUID, list[dict[str, Any]]] = {}
    names = {component.id: component.name for component in components}
    #: Scoped by the project's own component ids rather than a project column the
    #: table does not have (§42).
    project_component_ids = list(names)
    for dependency in (
        await session.scalars(
            select(ComponentDependency).where(
                ComponentDependency.source_component_id.in_(project_component_ids),
                ComponentDependency.target_component_id.in_(project_component_ids),
            )
        )
    ).all():
        dependencies.setdefault(dependency.source_component_id, []).append(
            {
                "component_id": str(dependency.target_component_id),
                "name": names.get(dependency.target_component_id),
                "type": getattr(
                    dependency.dependency_type, "value", str(dependency.dependency_type)
                ),
            }
        )
        dependents.setdefault(dependency.target_component_id, []).append(
            {
                "component_id": str(dependency.source_component_id),
                "name": names.get(dependency.source_component_id),
                "type": getattr(
                    dependency.dependency_type, "value", str(dependency.dependency_type)
                ),
            }
        )

    from app.services.system_state import derive_component_states

    states = {
        result.component_id: result
        for result in await derive_component_states(
            session, project_id=project_id, component_ids=component_ids
        )
    }

    entries: list[CatalogEntry] = []
    for component in components:
        owner = owners.get(component.id)
        state = states.get(component.id)
        #: ``environment_id`` is nullable on a component; a missing environment is
        #: reported as unknown, not looked up as the None key.
        environment = (
            environments.get(component.environment_id)
            if component.environment_id is not None
            else None
        )
        entry = CatalogEntry(
            component_id=component.id,
            name=component.name,
            component_type=getattr(
                component.component_type, "value", str(component.component_type)
            ),
            environment_id=str(component.environment_id)
            if component.environment_id
            else None,
            environment_name=environment.name if environment else None,
            owner=_owner_dict(owner),
            dependencies=dependencies.get(component.id, []),
            dependents=dependents.get(component.id, []),
            endpoints=endpoints.get(component.id, []),
            state=state.state.value if state else UNKNOWN,
            state_reason=state.reason if state else None,
        )
        if owner is None:
            entry.limitations.append(
                "no ownership is recorded for this component, so there is nobody "
                "to page: ownership is reported as UNKNOWN rather than inferred"
            )
        entries.append(entry)
    return entries


def _owner_dict(owner: Optional[ComponentOwner]) -> dict[str, Any]:
    if owner is None:
        return {
            "team": UNKNOWN,
            "owner": UNKNOWN,
            "on_call": UNKNOWN,
            "repository": UNKNOWN,
            "documentation": UNKNOWN,
            "known": False,
        }
    return {
        "team": owner.team,
        "owner": owner.owner_name or UNKNOWN,
        "on_call": owner.on_call or UNKNOWN,
        "repository": owner.repository_owner or UNKNOWN,
        "documentation": owner.documentation_url or UNKNOWN,
        "known": True,
    }


async def catalog_entry(
    session: AsyncSession,
    *,
    component_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    window_days: int = 30,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> Optional[CatalogEntry]:
    """The full §30 record for one service."""
    settings = settings or get_settings()
    moment = _aware(now) or datetime.now(timezone.utc)
    component = await session.get(SystemComponent, component_id)
    if component is None:
        return None
    if project_id is not None and component.project_id != project_id:
        return None
    project_id = component.project_id

    index = await list_catalog(
        session,
        project_id=project_id,
        environment_id=None,
        limit=settings.PLATFORM_COMPARISON_COMPONENT_LIMIT,
        settings=settings,
    )
    entry = next((item for item in index if item.component_id == component_id), None)
    if entry is None:  # pragma: no cover - the component was just fetched
        return None

    window = timedelta(days=window_days)

    # -- incident history
    incidents = (
        await session.scalars(
            select(Incident)
            .where(
                Incident.primary_component_id == component_id,
                Incident.detected_at >= moment - window,
            )
            .order_by(Incident.detected_at.desc())
            .limit(25)
        )
    ).all()
    entry.incident_history = [
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
        for incident in incidents
    ]
    if not entry.incident_history:
        entry.limitations.append(
            f"no incident was attributed to this component in the last {window_days} "
            "days"
        )

    # -- deployment history
    from app.models.deployment import DeploymentEvent

    deployments = (
        await session.scalars(
            select(DeploymentEvent)
            .where(
                DeploymentEvent.component_id == component_id,
                DeploymentEvent.deployed_at >= moment - window,
            )
            .order_by(DeploymentEvent.deployed_at.desc())
            .limit(25)
        )
    ).all()
    entry.deployment_history = [
        {
            "id": str(deployment.id),
            "version": deployment.version,
            "status": getattr(deployment.status, "value", str(deployment.status)),
            "deployed_at": _aware(deployment.deployed_at).isoformat(),
        }
        for deployment in deployments
    ]

    # -- risk
    forecast = (
        await session.scalars(
            select(ReliabilityForecast)
            .where(
                ReliabilityForecast.component_id == component_id,
                ReliabilityForecast.status.in_(
                    [ForecastStatus.GENERATED, ForecastStatus.ACTIVE]
                ),
            )
            .order_by(ReliabilityForecast.generated_at.desc())
            .limit(1)
        )
    ).first()
    if forecast is not None:
        entry.risk = {
            "risk_level": getattr(
                forecast.risk_level, "value", str(forecast.risk_level)
            ),
            "risk_score": forecast.risk_score,
            "confidence": forecast.confidence,
            "data_quality": getattr(
                forecast.data_quality, "value", str(forecast.data_quality)
            ),
            "prediction_type": getattr(
                forecast.prediction_type, "value", str(forecast.prediction_type)
            ),
            "valid_until": _aware(forecast.valid_until).isoformat(),
            "elevated": forecast.risk_level
            in (ForecastRiskLevel.HIGH, ForecastRiskLevel.CRITICAL),
        }
    else:
        entry.risk = {
            "risk_level": UNKNOWN,
            "risk_score": None,
            "elevated": None,
            "note": "no active forecast exists for this component",
        }

    # -- remediation history
    actions = (
        await session.scalars(
            select(RemediationAction)
            .where(RemediationAction.component_id == component_id)
            .order_by(RemediationAction.created_at.desc())
            .limit(25)
        )
    ).all()
    entry.remediation_history = [
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

    # -- reliability profile (Phase 10, optional)
    try:
        from app.models.intelligence import ComponentReliabilityProfile

        profile = (
            await session.scalars(
                select(ComponentReliabilityProfile)
                .where(ComponentReliabilityProfile.component_id == component_id)
                .order_by(ComponentReliabilityProfile.created_at.desc())
                .limit(1)
            )
        ).first()
        if profile is not None:
            entry.reliability_profile = {
                "window_days": getattr(profile, "window_days", None),
                "incident_count": getattr(profile, "incident_count", None),
                "anomaly_count": getattr(profile, "anomaly_count", None),
                "computed_at": _aware(profile.created_at).isoformat(),
            }
        else:
            entry.unavailable["reliability_profile"] = (
                "no reliability profile has been computed for this component yet"
            )
    except Exception:  # pragma: no cover - learning is optional (§59)
        entry.unavailable["reliability_profile"] = "the learning layer did not answer"

    # -- scorecard (§75)
    try:
        from app.services.platform_metrics import component_scorecard

        scorecard = await component_scorecard(
            session,
            component_id=component_id,
            window_days=window_days,
            now=moment,
            settings=settings,
        )
        entry.scorecard = scorecard.as_dict()
    except Exception as exc:  # pragma: no cover - metrics are optional
        logger.warning("scorecard unavailable for %s", component_id, exc_info=True)
        entry.unavailable["scorecard"] = f"the scorecard failed: {type(exc).__name__}"
    return entry


async def catalog_summary(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """The catalog's own health: how much of it is actually known."""
    settings = settings or get_settings()
    entries = await list_catalog(
        session,
        project_id=project_id,
        limit=settings.PLATFORM_COMPARISON_COMPONENT_LIMIT,
        settings=settings,
    )
    owned = sum(1 for entry in entries if entry.owner.get("known"))
    with_endpoints = sum(1 for entry in entries if entry.endpoints)
    with_deps = sum(1 for entry in entries if entry.dependencies)
    state_counts: dict[str, int] = {}
    for entry in entries:
        state_counts[entry.state] = state_counts.get(entry.state, 0) + 1
    limitations: list[str] = []
    unowned = len(entries) - owned
    if unowned:
        limitations.append(
            f"{unowned} of {len(entries)} component(s) have no recorded owner; an "
            "unowned service cannot be escalated to anyone"
        )
    return {
        "components": len(entries),
        "owned": owned,
        "unowned": unowned,
        "with_endpoints": with_endpoints,
        "with_dependencies": with_deps,
        "by_state": state_counts,
        "limitations": limitations,
    }


async def set_ownership(
    session: AsyncSession,
    *,
    component_id: uuid.UUID,
    team: str,
    owner_name: Optional[str] = None,
    contact_email: Optional[str] = None,
    repository_owner: Optional[str] = None,
    on_call: Optional[str] = None,
    documentation_url: Optional[str] = None,
    actor: Optional[str] = None,
) -> ComponentOwner:
    """Record ownership for a component (§31).

    Upsert rather than insert-or-fail: ownership genuinely changes hands, and the
    catalog should record the current owner rather than accumulate conflicting
    rows. The change is audited through the platform event the caller publishes.
    """
    existing = (
        await session.scalars(
            select(ComponentOwner).where(ComponentOwner.component_id == component_id)
        )
    ).first()
    if existing is None:
        component = await session.get(SystemComponent, component_id)
        if component is None:
            raise ConfigurationError(
                "the component no longer exists, so ownership cannot be recorded"
            )
        existing = ComponentOwner(
            project_id=component.project_id,
            component_id=component_id,
            team=team,
            owner_name=owner_name,
            contact_email=contact_email,
            repository_owner=repository_owner,
            on_call=on_call,
            documentation_url=documentation_url,
        )
        session.add(existing)
    else:
        existing.team = team
        existing.owner_name = owner_name
        existing.contact_email = contact_email
        existing.repository_owner = repository_owner
        existing.on_call = on_call
        existing.documentation_url = documentation_url
    await session.flush()
    return existing


async def dependency_blast_radius(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    component_id: uuid.UUID,
    max_hops: int = 3,
) -> dict[str, Any]:
    """Which components a failure here could affect (§39, §22).

    Structural, and labelled as such: a dependency establishes a *channel*
    through which failure can travel, never a promise that it will. The traversal
    is bounded by ``max_hops`` and reports the bound, because an unbounded graph
    walk is how a "quick" catalog view becomes an outage.
    """
    frontier = {component_id}
    visited: set[uuid.UUID] = set()
    hops: list[list[str]] = []
    for _ in range(max(1, max_hops)):
        #: Filtering on the target alone already confines the walk to this
        #: project: a dependency's endpoints are project components by
        #: construction, so no extra join is needed (§42, §65).
        dependents = (
            await session.scalars(
                select(ComponentDependency.source_component_id).where(
                    ComponentDependency.target_component_id.in_(list(frontier)),
                )
            )
        ).all()
        next_frontier = {row for row in dependents if row not in visited}
        if not next_frontier:
            break
        visited |= next_frontier
        frontier = next_frontier
        hops.append([str(row) for row in sorted(next_frontier, key=str)])
    return {
        "component_id": str(component_id),
        "affected_by_hop": hops,
        "affected_total": len(visited),
        "max_hops": max_hops,
        "truncated": len(hops) >= max_hops,
        "note": (
            "a dependency is a structural channel for failure, not a prediction "
            "that it travels; nothing here asserts causality"
        ),
    }


__all__ = [
    "UNKNOWN",
    "CatalogEntry",
    "catalog_entry",
    "catalog_summary",
    "dependency_blast_radius",
    "list_catalog",
    "set_ownership",
]
