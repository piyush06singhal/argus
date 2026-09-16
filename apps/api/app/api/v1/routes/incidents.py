"""ARGUS Incident Routes."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.incident import Incident, IncidentEvidence
from app.schemas.incident import (
    EvidenceCreate,
    EvidenceList,
    EvidenceResponse,
    IncidentCreate,
    IncidentList,
    IncidentResponse,
    IncidentUpdate,
)

router = APIRouter(prefix="/incidents", tags=["Incidents"])


@router.post("", response_model=IncidentResponse, status_code=201)
async def create_incident(
    incident_data: IncidentCreate,
    db: AsyncSession = Depends(get_db),
) -> Incident:
    """Create a new incident."""
    incident = Incident(**incident_data.model_dump())
    db.add(incident)
    await db.flush()
    await db.refresh(incident)
    return incident


@router.get("", response_model=IncidentList)
async def list_incidents(
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    severity: Optional[str] = None,
    status: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> IncidentList:
    """List incidents with filtering."""
    query = select(Incident)
    count_query = select(func.count(Incident.id))

    if project_id:
        query = query.where(Incident.project_id == project_id)
        count_query = count_query.where(Incident.project_id == project_id)
    if environment_id:
        query = query.where(Incident.environment_id == environment_id)
        count_query = count_query.where(Incident.environment_id == environment_id)
    if severity:
        query = query.where(Incident.severity == severity)
        count_query = count_query.where(Incident.severity == severity)
    if status:
        query = query.where(Incident.status == status)
        count_query = count_query.where(Incident.status == status)
    if start_time:
        query = query.where(Incident.detected_at >= start_time)
        count_query = count_query.where(Incident.detected_at >= start_time)
    if end_time:
        query = query.where(Incident.detected_at <= end_time)
        count_query = count_query.where(Incident.detected_at <= end_time)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(Incident.detected_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    incidents = result.scalars().all()

    return IncidentList(
        items=[IncidentResponse.model_validate(i) for i in incidents],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.get("/{incident_id}", response_model=IncidentResponse)
async def get_incident(
    incident_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Incident:
    """Get an incident by ID."""
    result = await db.execute(
        select(Incident).where(Incident.id == incident_id)
    )
    incident = result.scalar_one_or_none()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@router.put("/{incident_id}", response_model=IncidentResponse)
async def update_incident(
    incident_id: uuid.UUID,
    incident_data: IncidentUpdate,
    db: AsyncSession = Depends(get_db),
) -> Incident:
    """Update an incident."""
    result = await db.execute(
        select(Incident).where(Incident.id == incident_id)
    )
    incident = result.scalar_one_or_none()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    update_data = incident_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(incident, field, value)

    await db.flush()
    await db.refresh(incident)
    return incident


# Evidence routes
@router.post("/{incident_id}/evidence", response_model=EvidenceResponse, status_code=201)
async def create_evidence(
    incident_id: uuid.UUID,
    evidence_data: EvidenceCreate,
    db: AsyncSession = Depends(get_db),
) -> IncidentEvidence:
    """Create evidence for an incident."""
    # Verify incident exists
    incident_result = await db.execute(
        select(Incident).where(Incident.id == incident_id)
    )
    if not incident_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Incident not found")

    evidence = IncidentEvidence(
        incident_id=incident_id,
        **evidence_data.model_dump(exclude={"incident_id"}),
    )
    db.add(evidence)
    await db.flush()
    await db.refresh(evidence)
    return evidence


@router.get("/{incident_id}/evidence", response_model=EvidenceList)
async def list_evidence(
    incident_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> EvidenceList:
    """List evidence for an incident."""
    result = await db.execute(
        select(IncidentEvidence).where(
            IncidentEvidence.incident_id == incident_id
        )
    )
    evidence = result.scalars().all()

    return EvidenceList(
        items=[EvidenceResponse.model_validate(e) for e in evidence],
        total=len(evidence),
        page=1,
        page_size=max(len(evidence), 1),
        total_pages=1 if evidence else 0,
    )
