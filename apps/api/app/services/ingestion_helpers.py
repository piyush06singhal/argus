"""ARGUS Ingestion helpers — dedup fingerprints, correlation, and
component/environment resolution.

Phase 1 §20/§21/§30:
  - EventFingerprint  : canonical, deterministic hash for at-least-once dedup.
  - CorrelationEngine : assigns correlation groups from shared request/trace IDs.
  - ComponentResolver / EnvironmentResolver: bind telemetry to the knowledge
    graph by service-name / source-name heuristics.
"""
from __future__ import annotations

import hashlib
import uuid as uuid_lib
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Environment, SoftwareProject
from app.models.system import SystemComponent


class EventFingerprint:
    """Deterministic identity for an event, stable across retries.

    The fingerprint is keyed only on immutable event attributes (project,
    source, event type, timestamp, and a canonical JSON of stable payload
    keys) so a re-delivered event hashes identically.
    """

    _VERSION = "v1"

    def __init__(self, payload: dict[str, Any] | None = None):
        self._payload = payload or {}

    @classmethod
    def compute(
        cls,
        *,
        project_id: uuid_lib.UUID,
        source: str,
        event_type: str,
        timestamp: Any,
        payload: dict[str, Any] | None,
        stable_keys: Optional[list[str]] = None,
    ) -> str:
        """Compute the canonical fingerprint string."""
        content = cls._stable_content(payload, stable_keys)
        body = b"|".join(
            [
                cls._VERSION.encode(),
                str(project_id).encode(),
                source.encode(),
                event_type.encode(),
                str(timestamp).encode(),
                content,
            ]
        )
        return hashlib.sha256(body).hexdigest()

    @staticmethod
    def _stable_content(payload: dict[str, Any] | None, stable_keys: Optional[list[str]]) -> bytes:
        """Serialize a stable subset of the payload for hashing.

        Only the listed stable_keys (or, when absent, all keys whose value
        is str/int/float/bool) participate — volatile fields like timing
        metadata never enter the fingerprint.
        """
        payload = payload or {}
        if stable_keys is not None:
            subset = {k: payload.get(k) for k in stable_keys if k in payload}
        else:
            subset = {
                k: v
                for k, v in payload.items()
                if isinstance(v, (str, int, float, bool)) and not k.startswith("_")
            }
        canonical = sorted(subset.items(), key=lambda kv: kv[0])
        return repr(canonical).encode()


class CorrelationEngine:
    """Assign a correlation_id to an event.

    Events that share a request_id, trace_id, or deployment_id within a window
    belong to the same correlation group. When no anchor is present a random
    id is assigned so every event is traceable.
    """

    __slots__ = ("_lock", "_anchors", "_max_anchors")

    def __init__(self) -> None:
        import asyncio
        self._lock = asyncio.Lock()
        # anchor -> correlation_id
        self._anchors: dict[str, str] = {}
        self._max_anchors = 4096

    def correlate(
        self,
        *,
        request_id: Optional[str],
        trace_id: Optional[str],
        deployment_id: Optional[str],
    ) -> str:
        """Return a correlation_id for an event carrying the given anchors."""
        for anchor in (request_id, trace_id, deployment_id):
            if anchor:
                existing = self._anchors.get(anchor)
                if existing:
                    return existing
        # New group — arbitrary, unique correlation id.
        correlation_id = uuid_lib.uuid4().hex[:32]
        for anchor in (request_id, trace_id, deployment_id):
            if anchor:
                if len(self._anchors) >= self._max_anchors:
                    self._anchors.clear()
                self._anchors[anchor] = correlation_id
        return correlation_id


class ComponentResolver:
    """Resolve a component_id from telemetry metadata.

    Matching candidates (in priority order):
      1. Explicit ``component_id`` in the payload.
      2. ``component`` / ``service`` name matching a SystemComponent.name.
      3. ``source`` matching a component's slug/name.
      4. ``None`` when nothing matches or a project has no components.
    """

    def __init__(self, db: AsyncSession):
        self._db = db
        self._cache: dict[tuple[uuid_lib.UUID, str], Optional[uuid_lib.UUID]] = {}

    async def resolve(
        self,
        *,
        project_id: uuid_lib.UUID,
        payload: dict[str, Any],
        source: str,
    ) -> Optional[uuid_lib.UUID]:
        component_id = payload.get("component_id")
        if isinstance(component_id, uuid_lib.UUID):
            return component_id
        if isinstance(component_id, str):
            try:
                return uuid_lib.UUID(component_id)
            except ValueError:
                pass

        candidate = (
            payload.get("component")
            or payload.get("service")
            or self._from_source_name(source)
        )
        if not candidate:
            return None

        key = (project_id, str(candidate))
        if key in self._cache:
            return self._cache[key]

        result = await self._db.execute(
            select(SystemComponent.id).where(
                SystemComponent.project_id == project_id,
                SystemComponent.name == candidate,
            )
        )
        resolved = result.scalar_one_or_none()
        self._cache[key] = resolved
        return resolved

    @staticmethod
    def _from_source_name(source: str) -> Optional[str]:
        # "log:cart-service" -> "cart-service"
        if ":" in source:
            return source.split(":", 1)[1]
        return None


class EnvironmentResolver:
    """Resolve an environment_id from telemetry metadata.

    Priority: explicit ``environment_id`` / ``environment`` in the payload,
    else the first active environment of the project with a matching key.
    """

    def __init__(self, db: AsyncSession):
        self._db = db
        self._cache: dict[tuple[uuid_lib.UUID, str], Optional[uuid_lib.UUID]] = {}

    async def resolve(
        self,
        *,
        project_id: uuid_lib.UUID,
        payload: dict[str, Any],
    ) -> Optional[uuid_lib.UUID]:
        env_id = payload.get("environment_id")
        if isinstance(env_id, uuid_lib.UUID):
            return env_id
        if isinstance(env_id, str):
            try:
                return uuid_lib.UUID(env_id)
            except ValueError:
                pass

        env_name = payload.get("environment")
        if not env_name:
            return None

        key = (project_id, str(env_name))
        if key in self._cache:
            return self._cache[key]

        result = await self._db.execute(
            select(Environment.id).where(
                Environment.project_id == project_id,
                Environment.name == env_name,
            )
        )
        resolved = result.scalar_one_or_none()
        self._cache[key] = resolved
        return resolved