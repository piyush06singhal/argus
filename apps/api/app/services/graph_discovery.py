"""ARGUS Graph Discovery Engine.

Weak-evidence component suggestions. Ingestion evidence can reference a peer or
service name that does not map to any known component (e.g. ``peer_service`` on
a span whose peer is a freshly-rolled service, or ``service.name`` in span
resource metadata that does not resolve to a mirrored node). ``suggest`` scans
``spans``/``observability_events`` metadata for such names, buckets them by
lower-cased identity, and maintains ``GraphDiscoveryRecord(status=PENDING)``
rows. Conversion to a real node is **always explicit** — ``register`` is the
only path that creates a ``GENERIC`` node from a record, and ``ignore`` marks a
record so it stops reappearing.

Resolution rules:
- If a candidate name resolves to a known node (name, case-insensitive name, or
  alias) via the component registry, it is *not* a discovery candidate.
- Names already REGISTERED or IGNORED (case-insensitive) are skipped.
- Otherwise the PENDING bucket is upserted: evidence count/sources grow,
  identity snapshot refreshed, ``confidence`` is the empirical hit rate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import (
    DiscoveredComponentStatus,
    GraphDiscoveryRecord,
    GraphNode,
    GraphNodeType,
)
from app.models.observability import ObservabilityEvent, SpanRecord
from app.models.system import SystemComponent
from app.services.graph_registry import ComponentRegistry


def _now() -> datetime:
    return datetime.now(timezone.utc)


#: Metadata keys that name a peer/self service worth checking for resolution.
#: Prefix-variants (e.g. ``peers.0.service``) are not scanned — only the
#: literal keys below, mirroring how the extractor reads dependency hints.
_EVIDENCE_KEYS = (
    "service.name",
    "peer_service",
    "peer.service.name",
    "http.peer_service",
)


@dataclass
class DiscoveryStats:
    """Outcome of one ``suggest`` pass."""

    candidates_seen: int = 0
    records_created: int = 0
    records_updated: int = 0


class GraphDiscoveryEngine:
    """Scan evidence for unresolved names and maintain PENDING records."""

    def __init__(self, db: AsyncSession, registry: ComponentRegistry) -> None:
        self._db = db
        self._registry = registry

    # ------------------------------------------------------------------
    # Suggestion scan
    # ------------------------------------------------------------------
    async def suggest(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
    ) -> Tuple[List[GraphDiscoveryRecord], DiscoveryStats]:
        """Scan span/event metadata for unresolved service names.

        Returns ``(upserted records, stats)``. Called on demand (reconcile or
        API) — evidence sources are re-scanned so records converge to the
        current evidence set.
        """
        stats = DiscoveryStats()
        evidence = await self._candidate_evidence(project_id, environment_id)
        seen: List[GraphDiscoveryRecord] = []
        for name, evidence_key in evidence:
            if await self._registry.resolve_component_node_by_name(project_id, name):
                continue  # already known — not a discovery candidate
            if not name.strip():
                continue
            # REGISTERED/IGNORED buckets are terminal — skip, never resurrect.
            if await self._terminal_record(project_id, name):
                continue
            record, created = await self._upsert_record(
                project_id=project_id,
                environment_id=environment_id,
                discovered_name=name,
                evidence_key=evidence_key,
            )
            if created:
                stats.records_created += 1
            else:
                stats.records_updated += 1
            seen.append(record)
        stats.candidates_seen = len(evidence)
        await self._db.flush()
        return seen, stats

    async def _candidate_evidence(
        self,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
    ) -> List[Tuple[str, str]]:
        """Distinct ``(service/peer name, evidence key)`` pairs, project-scoped."""
        evidence: List[Tuple[str, str]] = []
        seen_lower: set[str] = set()

        span_stmt = select(SpanRecord.metadata_).where(
            SpanRecord.project_id == project_id
        )
        # Spans carry no environment column — scope via the owning component.
        if environment_id is not None:
            span_stmt = span_stmt.join(
                SystemComponent, SystemComponent.id == SpanRecord.component_id
            ).where(SystemComponent.environment_id == environment_id)
        for (meta,) in (await self._db.execute(span_stmt)).all():
            found = self._first_value(meta)
            if found is None:
                continue
            name, key = found
            if name.lower() not in seen_lower:
                seen_lower.add(name.lower())
                evidence.append((name, key))

        event_stmt = (
            select(ObservabilityEvent.metadata_)
            .where(ObservabilityEvent.project_id == project_id)
            .order_by(ObservabilityEvent.ingested_at.desc())
        )
        if environment_id is not None:
            event_stmt = event_stmt.where(
                ObservabilityEvent.environment_id == environment_id
            )
        rows = (await self._db.execute(event_stmt.limit(2000))).all()
        for (meta,) in rows:
            found = self._first_value(meta)
            if found is None:
                continue
            name, key = found
            if name.lower() not in seen_lower:
                seen_lower.add(name.lower())
                evidence.append((name, key))
        return evidence

    @staticmethod
    def _first_value(meta: Optional[dict]) -> Optional[Tuple[str, str]]:
        """First ``(name, evidence key)`` in a metadata dict, or ``None``."""
        if not meta:
            return None
        for key in _EVIDENCE_KEYS:
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), key
        return None

    async def _terminal_record(
        self, project_id: uuid.UUID, name: str
    ) -> Optional[GraphDiscoveryRecord]:
        """A REGISTERED/IGNORED record for this exact name, if any."""
        return (
            await self._db.execute(
                select(GraphDiscoveryRecord).where(
                    GraphDiscoveryRecord.project_id == project_id,
                    GraphDiscoveryRecord.discovered_name == name,
                    GraphDiscoveryRecord.status != DiscoveredComponentStatus.PENDING,
                )
            )
        ).scalar_one_or_none()

    async def _upsert_record(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID],
        discovered_name: str,
        evidence_key: str,
    ) -> Tuple[GraphDiscoveryRecord, bool]:
        """Create or bump one PENDING record keyed by its exact name."""
        existing = (
            await self._db.execute(
                select(GraphDiscoveryRecord)
                .where(
                    GraphDiscoveryRecord.project_id == project_id,
                    GraphDiscoveryRecord.discovered_name == discovered_name,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            # REGISTERED/IGNORED buckets are terminal — never resurrected.
            if existing.status != DiscoveredComponentStatus.PENDING:
                return existing, False
            existing.evidence_count += 1
            sources = list(existing.evidence_sources or [])
            if evidence_key not in sources:
                sources.append(evidence_key)
            existing.evidence_sources = sources
            existing.last_seen_at = _now()
            existing.identity_hint = self._identity_hint(project_id, environment_id)
            self._db.add(existing)
            return existing, False

        now = _now()
        record = GraphDiscoveryRecord(
            project_id=project_id,
            environment_id=environment_id,
            discovered_name=discovered_name,
            suggested_node_type=GraphNodeType.UNKNOWN,
            identity_hint=self._identity_hint(project_id, environment_id),
            evidence_count=1,
            evidence_sources=[evidence_key],
            confidence=1.0,
            status=DiscoveredComponentStatus.PENDING,
            first_seen_at=now,
            last_seen_at=now,
        )
        self._db.add(record)
        await self._db.flush()
        await self._db.refresh(record)
        return record, True

    @staticmethod
    def _identity_hint(
        project_id: uuid.UUID, environment_id: Optional[uuid.UUID]
    ) -> dict:
        return {
            "project_id": str(project_id),
            "environment_id": str(environment_id) if environment_id else None,
            "basis": "evidence",
        }

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------
    async def list_records(
        self,
        *,
        project_id: uuid.UUID,
        status: Optional[DiscoveredComponentStatus] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[GraphDiscoveryRecord], int]:
        stmt = select(GraphDiscoveryRecord).where(
            GraphDiscoveryRecord.project_id == project_id
        )
        count = select(func.count(GraphDiscoveryRecord.id)).where(
            GraphDiscoveryRecord.project_id == project_id
        )
        if status is not None:
            stmt = stmt.where(GraphDiscoveryRecord.status == status)
            count = count.where(GraphDiscoveryRecord.status == status)
        total = (await self._db.execute(count)).scalar() or 0
        rows = list(
            (
                await self._db.execute(
                    stmt.order_by(
                        GraphDiscoveryRecord.evidence_count.desc(),
                        GraphDiscoveryRecord.last_seen_at.desc(),
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return rows, int(total)

    # ------------------------------------------------------------------
    # Explicit resolution
    # ------------------------------------------------------------------
    async def register(
        self,
        record_id: uuid.UUID,
        *,
        name: Optional[str] = None,
        node_type: Optional[GraphNodeType] = None,
    ) -> GraphNode:
        """Convert a PENDING record into a ``GENERIC`` node.

        The node carries ``entity_kind="generic"`` with a fresh ``entity_id`` —
        it is not a mirror of any canonical entity, so it never conflicts with
        ``UniqueConstraint(project_id, entity_kind, entity_id)``. Never
        automatic: raises ``ValueError`` for missing or non-PENDING records.
        """
        record = await self._db.get(GraphDiscoveryRecord, record_id)
        if record is None or record.status != DiscoveredComponentStatus.PENDING:
            raise ValueError("Discovery record not found or not pending")

        node, _ = await self._registry.mirror_node(
            node_type=node_type or record.suggested_node_type or GraphNodeType.UNKNOWN,
            entity_kind="generic",
            entity_id=uuid.uuid4(),
            project_id=record.project_id,
            environment_id=record.environment_id,
            name=name or record.discovered_name,
            description=f"Registered from discovery record {record.id}",
            metadata_={
                "discovery_record_id": str(record.id),
                "evidence_sources": record.evidence_sources or [],
            },
        )
        record.status = DiscoveredComponentStatus.REGISTERED
        record.last_seen_at = _now()
        self._db.add(record)
        await self._db.flush()
        return node

    async def ignore(self, record_id: uuid.UUID) -> None:
        """Mark a PENDING record IGNORED so it stops reappearing as a candidate."""
        record = await self._db.get(GraphDiscoveryRecord, record_id)
        if record is None or record.status != DiscoveredComponentStatus.PENDING:
            raise ValueError("Discovery record not found or not pending")
        record.status = DiscoveredComponentStatus.IGNORED
        record.last_seen_at = _now()
        self._db.add(record)
        await self._db.flush()
