"""ARGUS Deployment Routes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.deployment import DeploymentEvent
from app.schemas.deployment import (
    DeploymentCreate,
    DeploymentList,
    DeploymentResponse,
    DeploymentUpdate,
)

router = APIRouter(tags=["Deployments"])


@router.post("/deployments", response_model=DeploymentResponse, status_code=201)
async def create_deployment(
    deployment_data: DeploymentCreate,
    db: AsyncSession = Depends(get_db),
) -> DeploymentEvent:
    """Create a new deployment event."""
    deployment = DeploymentEvent(**deployment_data.model_dump())
    db.add(deployment)
    await db.flush()
    await db.refresh(deployment)
    return deployment


@router.get("/deployments", response_model=DeploymentList)
async def list_deployments(
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    component_id: Optional[uuid.UUID] = None,
    status: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> DeploymentList:
    """List deployment events with filtering."""
    query = select(DeploymentEvent)
    count_query = select(func.count(DeploymentEvent.id))

    if project_id:
        query = query.where(DeploymentEvent.project_id == project_id)
        count_query = count_query.where(DeploymentEvent.project_id == project_id)
    if environment_id:
        query = query.where(DeploymentEvent.environment_id == environment_id)
        count_query = count_query.where(
            DeploymentEvent.environment_id == environment_id
        )
    if component_id:
        query = query.where(DeploymentEvent.component_id == component_id)
        count_query = count_query.where(DeploymentEvent.component_id == component_id)
    if status:
        query = query.where(DeploymentEvent.status == status)
        count_query = count_query.where(DeploymentEvent.status == status)
    if start_time:
        query = query.where(DeploymentEvent.deployed_at >= start_time)
        count_query = count_query.where(DeploymentEvent.deployed_at >= start_time)
    if end_time:
        query = query.where(DeploymentEvent.deployed_at <= end_time)
        count_query = count_query.where(DeploymentEvent.deployed_at <= end_time)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(DeploymentEvent.deployed_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    deployments = result.scalars().all()

    return DeploymentList(
        items=[DeploymentResponse.model_validate(d) for d in deployments],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.get("/deployments/{deployment_id}", response_model=DeploymentResponse)
async def get_deployment(
    deployment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> DeploymentEvent:
    """Get a deployment by ID."""
    result = await db.execute(
        select(DeploymentEvent).where(DeploymentEvent.id == deployment_id)
    )
    deployment = result.scalar_one_or_none()
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return deployment


@router.put("/deployments/{deployment_id}", response_model=DeploymentResponse)
async def update_deployment(
    deployment_id: uuid.UUID,
    deployment_data: DeploymentUpdate,
    db: AsyncSession = Depends(get_db),
) -> DeploymentEvent:
    """Update a deployment event."""
    result = await db.execute(
        select(DeploymentEvent).where(DeploymentEvent.id == deployment_id)
    )
    deployment = result.scalar_one_or_none()
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    update_data = deployment_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(deployment, field, value)

    await db.flush()
    await db.refresh(deployment)
    return deployment


# Project-specific deployment routes
@router.get("/projects/{project_id}/deployments", response_model=DeploymentList)
async def list_project_deployments(
    project_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> DeploymentList:
    """List deployments for a specific project."""
    query = select(DeploymentEvent).where(DeploymentEvent.project_id == project_id)
    count_query = select(func.count(DeploymentEvent.id)).where(
        DeploymentEvent.project_id == project_id
    )

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(DeploymentEvent.deployed_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    deployments = result.scalars().all()

    return DeploymentList(
        items=[DeploymentResponse.model_validate(d) for d in deployments],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )
