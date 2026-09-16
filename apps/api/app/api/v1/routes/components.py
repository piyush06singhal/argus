"""ARGUS Component Routes."""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.project import SoftwareProject
from app.models.system import ComponentDependency, SystemComponent
from app.schemas.system import (
    ComponentCreate,
    ComponentList,
    ComponentResponse,
    ComponentUpdate,
    DependencyCreate,
    DependencyList,
    DependencyResponse,
    SystemMap,
    SystemMapEdge,
    SystemMapNode,
)

router = APIRouter(tags=["Components"])


# Global component routes
@router.get("/components", response_model=ComponentList)
async def list_components(
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> ComponentList:
    """List system components."""
    query = select(SystemComponent)
    count_query = select(func.count(SystemComponent.id))

    if project_id:
        query = query.where(SystemComponent.project_id == project_id)
        count_query = count_query.where(SystemComponent.project_id == project_id)
    if environment_id:
        query = query.where(SystemComponent.environment_id == environment_id)
        count_query = count_query.where(SystemComponent.environment_id == environment_id)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    components = result.scalars().all()

    return ComponentList(
        items=[ComponentResponse.model_validate(c) for c in components],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.get("/components/{component_id}", response_model=ComponentResponse)
async def get_component(
    component_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SystemComponent:
    """Get a component by ID."""
    result = await db.execute(
        select(SystemComponent).where(SystemComponent.id == component_id)
    )
    component = result.scalar_one_or_none()
    if not component:
        raise HTTPException(status_code=404, detail="Component not found")
    return component


@router.put("/components/{component_id}", response_model=ComponentResponse)
async def update_component(
    component_id: uuid.UUID,
    component_data: ComponentUpdate,
    db: AsyncSession = Depends(get_db),
) -> SystemComponent:
    """Update a component."""
    result = await db.execute(
        select(SystemComponent).where(SystemComponent.id == component_id)
    )
    component = result.scalar_one_or_none()
    if not component:
        raise HTTPException(status_code=404, detail="Component not found")

    update_data = component_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(component, field, value)

    await db.flush()
    await db.refresh(component)
    return component


@router.delete("/components/{component_id}", status_code=204)
async def delete_component(
    component_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Delete a component."""
    result = await db.execute(
        select(SystemComponent).where(SystemComponent.id == component_id)
    )
    component = result.scalar_one_or_none()
    if not component:
        raise HTTPException(status_code=404, detail="Component not found")

    await db.delete(component)


# Project-scoped component routes
@router.post("/projects/{project_id}/components", response_model=ComponentResponse, status_code=201)
async def create_project_component(
    project_id: uuid.UUID,
    component_data: ComponentCreate,
    db: AsyncSession = Depends(get_db),
) -> SystemComponent:
    """Create a component for a specific project."""
    # Verify project exists
    project_result = await db.execute(
        select(SoftwareProject).where(SoftwareProject.id == project_id)
    )
    if not project_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    component = SystemComponent(
        project_id=project_id,
        **component_data.model_dump(),
    )
    db.add(component)
    await db.flush()
    await db.refresh(component)
    return component


@router.get("/projects/{project_id}/components", response_model=ComponentList)
async def list_project_components(
    project_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> ComponentList:
    """List components for a specific project."""
    query = select(SystemComponent).where(
        SystemComponent.project_id == project_id
    )
    count_query = select(func.count(SystemComponent.id)).where(
        SystemComponent.project_id == project_id
    )

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    components = result.scalars().all()

    return ComponentList(
        items=[ComponentResponse.model_validate(c) for c in components],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


# Dependency routes
@router.post("/projects/{project_id}/dependencies", response_model=DependencyResponse, status_code=201)
async def create_dependency(
    project_id: uuid.UUID,
    dep_data: DependencyCreate,
    db: AsyncSession = Depends(get_db),
) -> ComponentDependency:
    """Create a dependency between components."""
    dependency = ComponentDependency(**dep_data.model_dump())
    db.add(dependency)
    await db.flush()
    await db.refresh(dependency)
    return dependency


@router.get("/projects/{project_id}/dependencies", response_model=DependencyList)
async def list_dependencies(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> DependencyList:
    """List dependencies for a project."""
    # Get all components for the project
    components_result = await db.execute(
        select(SystemComponent.id).where(
            SystemComponent.project_id == project_id
        )
    )
    component_ids = list(components_result.scalars().all())

    if not component_ids:
        return DependencyList(items=[], total=0, page=1, page_size=1, total_pages=0)

    # Get dependencies between those components
    result = await db.execute(
        select(ComponentDependency).where(
            ComponentDependency.source_component_id.in_(component_ids)
        )
    )
    dependencies = result.scalars().all()

    return DependencyList(
        items=[DependencyResponse.model_validate(d) for d in dependencies],
        total=len(dependencies),
        page=1,
        page_size=max(len(dependencies), 1),
        total_pages=1 if dependencies else 0,
    )


# System Map
@router.get("/projects/{project_id}/system-map", response_model=SystemMap)
async def get_system_map(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SystemMap:
    """Get the system map for a project."""
    # Get all components
    components_result = await db.execute(
        select(SystemComponent).where(
            SystemComponent.project_id == project_id
        )
    )
    components = components_result.scalars().all()

    component_ids = [c.id for c in components]

    # Get all dependencies
    dependencies: list[ComponentDependency] = []
    if component_ids:
        dep_result = await db.execute(
            select(ComponentDependency).where(
                ComponentDependency.source_component_id.in_(component_ids)
            )
        )
        dependencies = list(dep_result.scalars().all())

    return SystemMap(
        nodes=[
            SystemMapNode(
                id=c.id,
                name=c.name,
                component_type=c.component_type,
                status=c.status,
            )
            for c in components
        ],
        edges=[
            SystemMapEdge(
                source=d.source_component_id,
                target=d.target_component_id,
                dependency_type=d.dependency_type,
            )
            for d in dependencies
        ],
    )