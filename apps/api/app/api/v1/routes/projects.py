"""ARGUS Project Routes."""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.edge import require_auth_context
from app.api.v1.deps import require_project
from app.models.project import SoftwareProject
from app.schemas.project import (
    ProjectCreate,
    ProjectList,
    ProjectResponse,
    ProjectUpdate,
)

router = APIRouter(prefix="/projects", tags=["Projects"])


def _scope_project_query(query, auth):
    """Constrain a project query to the caller's grants (hardening W1).

    A scoped token listing projects must not see foreign ones — an unfiltered
    ``SELECT`` would leak the whole project inventory even though individual
    reads are grant-checked.

    The context is required, not optional: the middleware binds one for every
    authenticated ``/api`` route, so ``None`` only means something is wrong —
    and "something is wrong" must not read as "no filter" (that was the live
    authorization bypass this pass fixed).
    """
    if auth.is_admin or auth.project_ids is None:
        return query
    if not auth.project_ids:
        # A scoped token with no grants sees nothing — not everything.
        return query.where(SoftwareProject.id.is_(None))
    return query.where(SoftwareProject.id.in_(auth.project_ids))


@router.post("", response_model=ProjectResponse, status_code=201)
async def create_project(
    project_data: ProjectCreate,
    db: AsyncSession = Depends(get_db),
) -> SoftwareProject:
    """Create a new software project."""
    # Check for duplicate slug
    existing = await db.execute(
        select(SoftwareProject).where(SoftwareProject.slug == project_data.slug)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="Project with this slug already exists"
        )

    project = SoftwareProject(**project_data.model_dump())
    db.add(project)
    await db.flush()
    await db.refresh(project)
    return project


@router.get("", response_model=ProjectList)
async def list_projects(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> ProjectList:
    """List software projects visible to the caller."""
    query = select(SoftwareProject)
    count_query = select(func.count(SoftwareProject.id))

    if status:
        query = query.where(SoftwareProject.status == status)
        count_query = count_query.where(SoftwareProject.status == status)

    auth = require_auth_context()
    query = _scope_project_query(query, auth)
    count_query = _scope_project_query(count_query, auth)

    # Get total count
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Get paginated results
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    projects = result.scalars().all()

    return ProjectList(
        items=[ProjectResponse.model_validate(p) for p in projects],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SoftwareProject:
    """Get a software project by ID (grant-checked)."""
    return await require_project(db, project_id)


@router.put("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: uuid.UUID,
    project_data: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
) -> SoftwareProject:
    """Update a software project (grant-checked)."""
    project = await require_project(db, project_id)

    update_data = project_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(project, field, value)

    await db.flush()
    await db.refresh(project)
    return project


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Delete a software project.

    The delete cascades across every table that belongs to the project, so it is
    the widest write in the system, and it used to collide with the background
    sweeps: PostgreSQL reported a **deadlock** between a cascade ``DELETE FROM
    environments`` and a sweep's ``INSERT INTO error_budget_snapshots``, because
    each transaction held rows the other needed next.

    Taking the same per-project lock the sweeps take makes them mutually
    exclusive, in the only order that cannot deadlock: whoever holds the project
    row finishes its work first.
    """
    from app.services.project_lock import lock_project

    await lock_project(db, project_id=project_id)
    project = await require_project(db, project_id)

    await db.delete(project)
