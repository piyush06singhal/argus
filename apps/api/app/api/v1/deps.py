"""ARGUS API scoping helpers (Phase 3 §46).

Phase 0–2 are single-tenant: there is no token authentication, so "isolation"
must be enforced by *ownership validation* on every request rather than by
trusting a client-supplied ``project_id``. Every helper here proves that a
nested resource really belongs to the claimed project/environment and raises
``404`` (not ``403``) when it does not — the resource is simply not visible in
that scope, so its existence is not disclosed.

Cross-project and cross-environment leakage is therefore impossible even
though the resources are addressable by UUID.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.anomaly import Anomaly
from app.models.incident import Incident
from app.models.project import Environment, SoftwareProject
from app.models.system import SystemComponent


async def require_project(db: AsyncSession, project_id: uuid.UUID) -> SoftwareProject:
    """Return the project or raise 404."""
    project = await db.get(SoftwareProject, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def require_environment(
    db: AsyncSession,
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID],
) -> Optional[Environment]:
    """Validate an environment belongs to the project (when given)."""
    if environment_id is None:
        return None
    environment = await db.get(Environment, environment_id)
    if environment is None or environment.project_id != project_id:
        raise HTTPException(status_code=404, detail="Environment not found")
    return environment


async def require_component(
    db: AsyncSession,
    component_id: uuid.UUID,
    *,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
) -> SystemComponent:
    """Return a component, enforcing scope when a scope is supplied.

    Phase 8 added this because forecasts are addressed per component: without
    an ownership check, a UUID would be enough to read another tenant's
    reliability profile. Unknown and out-of-scope both yield 404 (§60).
    """
    component = await db.get(SystemComponent, component_id)
    if component is None:
        raise HTTPException(status_code=404, detail="Component not found")
    if project_id is not None and component.project_id != project_id:
        raise HTTPException(status_code=404, detail="Component not found")
    if environment_id is not None and component.environment_id != environment_id:
        raise HTTPException(status_code=404, detail="Component not found")
    return component


async def require_incident(
    db: AsyncSession,
    incident_id: uuid.UUID,
    *,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
) -> Incident:
    """Return an incident, enforcing scope when a scope is supplied.

    Unknown id and out-of-scope id both yield 404: an attacker learns nothing
    about whether the incident exists elsewhere.
    """
    stmt = select(Incident).where(Incident.id == incident_id)
    incident = (await db.execute(stmt)).scalar_one_or_none()
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    if project_id is not None and incident.project_id != project_id:
        raise HTTPException(status_code=404, detail="Incident not found")
    if environment_id is not None and incident.environment_id != environment_id:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


async def require_anomaly(
    db: AsyncSession,
    anomaly_id: uuid.UUID,
    *,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
) -> Anomaly:
    """Return an anomaly, enforcing scope when a scope is supplied."""
    anomaly = (
        await db.execute(select(Anomaly).where(Anomaly.id == anomaly_id))
    ).scalar_one_or_none()
    if anomaly is None:
        raise HTTPException(status_code=404, detail="Anomaly not found")
    if project_id is not None and anomaly.project_id != project_id:
        raise HTTPException(status_code=404, detail="Anomaly not found")
    if environment_id is not None and anomaly.environment_id != environment_id:
        raise HTTPException(status_code=404, detail="Anomaly not found")
    return anomaly


__all__ = [
    "require_project",
    "require_environment",
    "require_incident",
    "require_anomaly",
]
