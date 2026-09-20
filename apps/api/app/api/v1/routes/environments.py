"""ARGUS Environment Routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.project import Environment, SoftwareProject
from app.schemas.project import (
    EnvironmentCreate,
    EnvironmentList,
    EnvironmentResponse,
)

router = APIRouter(tags=["Environments"])


@router.post(
    "/projects/{project_id}/environments",
    response_model=EnvironmentResponse,
    status_code=201,
)
async def create_environment(
    project_id: uuid.UUID,
    env_data: EnvironmentCreate,
    db: AsyncSession = Depends(get_db),
) -> Environment:
    """Create an environment for a project."""
    # Verify project exists
    project_result = await db.execute(
        select(SoftwareProject).where(SoftwareProject.id == project_id)
    )
    if not project_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    environment = Environment(project_id=project_id, **env_data.model_dump())
    db.add(environment)
    await db.flush()
    await db.refresh(environment)
    return environment


@router.get("/projects/{project_id}/environments", response_model=EnvironmentList)
async def list_environments(
    project_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> EnvironmentList:
    """List environments for a project."""
    query = select(Environment).where(Environment.project_id == project_id)
    count_query = select(func.count(Environment.id)).where(
        Environment.project_id == project_id
    )

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    environments = result.scalars().all()

    return EnvironmentList(
        items=[EnvironmentResponse.model_validate(e) for e in environments],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.get("/environments/{environment_id}", response_model=EnvironmentResponse)
async def get_environment(
    environment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Environment:
    """Get an environment by ID."""
    result = await db.execute(
        select(Environment).where(Environment.id == environment_id)
    )
    environment = result.scalar_one_or_none()
    if not environment:
        raise HTTPException(status_code=404, detail="Environment not found")
    return environment
