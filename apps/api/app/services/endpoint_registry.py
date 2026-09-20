"""ARGUS Graph Endpoint Registry.

Normalizes observed HTTP paths into stable ``path_template`` records per
component so trace-derived endpoint capture collapses ``/api/checkout/123``
and ``/api/checkout/456`` into one row. Records upsert on the
``(component_id, method, path_template)`` unique key, preserving the distinct
raw paths seen as evidence.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import ServiceEndpoint

#: Path segments that look like dynamic id placeholders: UUIDs, hex runs,
#: numeric ids (>=3 digits — e.g. order/row ids, but not "v1"/"v2"), long
#: alphanumeric ids, or standalone "id"-like tokens.
_ID_SEGMENT = re.compile(
    r"^[0-9a-fA-F-]{8,64}$"
    r"|^[\d]{3,}$"
    r"|^[A-Za-z0-9_-]{18,}$"
    r"|^(id|key|name|slug)$"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class EndpointRegistry:
    """Normalized service-endpoint records for graph + observability use."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    @staticmethod
    def normalize_path(path: str) -> str:
        """Collapse dynamic segments into a canonical template.

        Pure (no DB). Deterministic:
          ``/api/checkout/`` -> ``/api/checkout``
          ``/api/v1/orders/123`` -> ``/api/v1/orders/{id}``
          ``/api/users/a1b2c3d4e5f6g7h8i9j0`` -> ``/api/users/{id}``
        """
        cleaned = path.strip().rstrip("/")
        if not cleaned:
            return "/"
        segments = [seg for seg in cleaned.split("/") if seg]
        normalized: List[str] = []
        for seg in segments:
            if _ID_SEGMENT.match(seg) or seg.lower() in {"id", "key", "name", "slug"}:
                if normalized and normalized[-1] != "{id}":
                    normalized.append("{id}")
                elif not normalized:
                    normalized.append("{id}")
            else:
                normalized.append(seg)
        return "/" + "/".join(normalized)

    async def record_endpoint(
        self,
        *,
        project_id: uuid.UUID,
        component_id: uuid.UUID,
        method: str,
        path: str,
        environment_id: Optional[uuid.UUID] = None,
        is_external: bool = False,
        metadata_: Optional[dict] = None,
    ) -> ServiceEndpoint:
        """Upsert an endpoint by its normalized template; keep original paths."""
        path_template = self.normalize_path(path)
        existing = await self._db.execute(
            select(ServiceEndpoint).where(
                ServiceEndpoint.component_id == component_id,
                ServiceEndpoint.method == method,
                ServiceEndpoint.path_template == path_template,
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            if path not in (row.original_paths or []):
                row.original_paths = list(row.original_paths or []) + [path]
            row.last_seen_at = _now()
            row.is_external = row.is_external or is_external
            if environment_id is not None:
                row.environment_id = environment_id
            return row

        obj = ServiceEndpoint(
            project_id=project_id,
            environment_id=environment_id,
            component_id=component_id,
            method=method,
            path_template=path_template,
            original_paths=[path],
            is_external=is_external,
            metadata_=metadata_,
            first_seen_at=_now(),
            last_seen_at=_now(),
        )
        try:
            self._db.add(obj)
            await self._db.flush()
            await self._db.refresh(obj)
        except IntegrityError:
            await self._db.rollback()
            return await self.record_endpoint(
                project_id=project_id,
                component_id=component_id,
                method=method,
                path=path,
                environment_id=environment_id,
                is_external=is_external,
                metadata_=metadata_,
            )
        return obj

    async def list_endpoints(
        self,
        *,
        project_id: uuid.UUID,
        component_id: Optional[uuid.UUID] = None,
        method: Optional[str] = None,
        path: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[ServiceEndpoint], int]:
        """List endpoints scoped to a project with optional filters."""
        stmt = select(ServiceEndpoint)
        count = select(func.count(ServiceEndpoint.id))
        where = [ServiceEndpoint.project_id == project_id]
        if component_id is not None:
            where.append(ServiceEndpoint.component_id == component_id)
        if method is not None:
            where.append(ServiceEndpoint.method == method)
        if path is not None:
            where.append(ServiceEndpoint.path_template == self.normalize_path(path))
        stmt = stmt.where(*where)
        count = count.where(*where)
        total = (await self._db.execute(count)).scalar() or 0
        stmt = (
            stmt.order_by(
                ServiceEndpoint.path_template.asc(), ServiceEndpoint.method.asc()
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = list((await self._db.execute(stmt)).scalars().all())
        return items, int(total)

    async def endpoints_for_component(
        self, component_id: uuid.UUID
    ) -> List[ServiceEndpoint]:
        rows = await self._db.execute(
            select(ServiceEndpoint).where(ServiceEndpoint.component_id == component_id)
        )
        return list(rows.scalars().all())
