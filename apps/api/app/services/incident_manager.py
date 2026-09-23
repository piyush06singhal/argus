"""ARGUS Incident Manager (Phase 3 §25–§33).

Turns correlated anomaly clusters into incidents: lifecycle, fingerprint
deduplication, timeline, structured evidence, affected-component classification,
deployment/configuration temporal context, and a deterministic summary.

Boundaries enforced here:

* **Dedup before create.** Clusters share a deterministic incident fingerprint
  (scope + primary component + dominant anomaly class + time bucket), so a
  recurring incident attaches to the existing one instead of spawning a new row.
* **Context is labelled.** Deployment/configuration entries and any graph
  neighbours are recorded with ``is_context_only`` / an explicit classification;
  the UI can therefore never render them as causes.
* **Idempotent.** Re-running the manager over the same cluster does not
  duplicate timeline events or evidence rows.
* **Nothing is fabricated.** Every timeline entry and evidence item maps to a
  stored record; the summary reads only those records.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import ensure_utc, utcnow
from app.models.anomaly import Anomaly, AnomalySeverity, AnomalyType
from app.models.deployment import DeploymentEvent
from app.models.graph import GraphEdge, GraphEdgeStatus, GraphNode
from app.models.incident import (
    EvidenceType,
    Incident,
    IncidentEvidence,
    IncidentSeverity,
    IncidentStatus,
    IncidentTimelineEvent,
    TimelineEventType,
)
from app.models.ingestion import ConfigurationChangeEvent
from app.models.system import ComponentDependency, SystemComponent
from app.services import incident_state
from app.services.anomaly_severity import (
    SeveritySignals,
    compute_severity,
    max_severity,
)
from app.services.fingerprints import bucket_start, incident_fingerprint
from app.services.incident_correlation import CorrelationCluster
from app.services.incident_summary import (
    SummaryAnomaly,
    SummaryComponent,
    SummaryTimelineItem,
    build_incident_summary,
)

logger = logging.getLogger(__name__)
settings = get_settings()

#: Affected-component classifications (§30). Explicit, never a causality claim.
CLASS_DIRECTLY_OBSERVED = "DIRECTLY_OBSERVED"
CLASS_UPSTREAM_CONTEXT = "UPSTREAM_CONTEXT"
CLASS_DOWNSTREAM_CONTEXT = "DOWNSTREAM_CONTEXT"
CLASS_DEPENDENCY_CONTEXT = "DEPENDENCY_CONTEXT"

_MAX_CONTEXT_EVIDENCE = 50

#: Incident severity ordering — kept separate from the anomaly severity engine
#: because ``IncidentSeverity`` and ``AnomalySeverity`` are distinct enums.
_INCIDENT_SEVERITY_ORDER = {
    IncidentSeverity.LOW: 0,
    IncidentSeverity.MEDIUM: 1,
    IncidentSeverity.HIGH: 2,
    IncidentSeverity.CRITICAL: 3,
}


def _max_incident_severity(
    a: IncidentSeverity, b: IncidentSeverity
) -> IncidentSeverity:
    """Higher of two incident severities (never lowers)."""
    rank_a = _INCIDENT_SEVERITY_ORDER.get(IncidentSeverity(a), 0)
    rank_b = _INCIDENT_SEVERITY_ORDER.get(IncidentSeverity(b), 0)
    return IncidentSeverity(a) if rank_a >= rank_b else IncidentSeverity(b)


@dataclass
class IncidentRunResult:
    """Summary of one correlation/incident pass."""

    clusters: int = 0
    incidents_created: int = 0
    incidents_updated: int = 0
    anomalies_linked: int = 0
    skipped_clusters: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "clusters": self.clusters,
            "incidents_created": self.incidents_created,
            "incidents_updated": self.incidents_updated,
            "anomalies_linked": self.anomalies_linked,
            "skipped_clusters": self.skipped_clusters,
            "errors": self.errors,
        }


@dataclass
class AffectedComponent:
    """A component in the observed blast radius with its classification."""

    component_id: uuid.UUID
    name: Optional[str]
    classification: str
    reason: str
    anomaly_count: int = 0
    severity: Optional[AnomalySeverity] = None


class IncidentManager:
    """Persists correlated clusters as incidents (§26–§33)."""

    def __init__(
        self, session: AsyncSession, *, now: Optional[datetime] = None
    ) -> None:
        self._session = session
        self._now = ensure_utc(now) or utcnow()

    # -- Entry point --------------------------------------------------------
    async def process_scope(
        self,
        *,
        project_id: uuid.UUID,
        environment_id: Optional[uuid.UUID] = None,
    ) -> IncidentRunResult:
        """Correlate un-grouped anomalies and persist the resulting incidents."""
        from app.services.incident_correlation import IncidentCorrelationEngine

        result = IncidentRunResult()
        engine = IncidentCorrelationEngine(self._session, now=self._now)
        clusters = await engine.correlate_scope(
            project_id=project_id, environment_id=environment_id
        )
        result.clusters = len(clusters)

        for cluster in clusters:
            if not cluster.anomalies:
                continue
            if not cluster.should_open_incident():
                result.skipped_clusters += 1
                continue
            try:
                created, linked = await self._persist_cluster(cluster)
            except Exception as e:  # one bad cluster must not stop the pass
                logger.exception("Incident persistence failed for a cluster")
                result.errors.append(f"{type(e).__name__}: {e}")
                continue
            if created:
                result.incidents_created += 1
            else:
                result.incidents_updated += 1
            result.anomalies_linked += linked

        # Close out incidents whose anomalies have all been resolved.
        await self._auto_resolve_stale(project_id, environment_id)
        await self._session.flush()
        return result

    # -- Persistence --------------------------------------------------------
    async def _persist_cluster(self, cluster: CorrelationCluster) -> tuple[bool, int]:
        anomalies = list(cluster.anomalies)
        project_id = anomalies[0].project_id
        environment_id = anomalies[0].environment_id
        earliest = cluster.earliest or self._now

        fingerprint = incident_fingerprint(
            project_id=project_id,
            primary_component_id=cluster.primary_component_id,
            dominant_anomaly_type=cluster.dominant_anomaly_type,
            environment_id=environment_id,
            time_bucket=bucket_start(earliest),
            related_component_ids=cluster.component_ids,
        )

        affected = await self._classify_components(cluster)
        severity = self._incident_severity(cluster, affected)

        incident = await self._find_incident(project_id, fingerprint)
        created = incident is None
        if incident is None:
            incident = Incident(
                project_id=project_id,
                environment_id=environment_id,
                title=self._title(cluster, affected),
                severity=severity,
                status=IncidentStatus.OPEN,
                detected_at=earliest,
                started_at=earliest,
                fingerprint=fingerprint,
                primary_component_id=cluster.primary_component_id,
                correlation_rationale=cluster.rationale,
            )
            existing = await self._insert_incident(incident, project_id, fingerprint)
            if existing is not None:
                #: Another pass opened the incident for this fingerprint while
                #: this one was between the lookup and the insert. Adopt its row
                #: and take the update path: the alternative is a duplicate
                #: incident with no anomalies attached to it (§25).
                incident = existing
                created = False
        if not created:
            # Reopen a resolved incident rather than duplicating it (§25).
            if IncidentStatus(incident.status) is IncidentStatus.RESOLVED:
                await self.transition(
                    incident,
                    IncidentStatus.OPEN,
                    actor="system:correlation",
                    note="New correlated anomalies detected for this incident",
                )
            incident.severity = _max_incident_severity(
                IncidentSeverity(incident.severity), severity
            )
            incident.correlation_rationale = cluster.rationale
            incident.primary_component_id = (
                cluster.primary_component_id or incident.primary_component_id
            )

        linked = 0
        for anomaly in anomalies:
            if anomaly.incident_id != incident.id:
                anomaly.incident_id = incident.id
                linked += 1
            await self._add_anomaly_evidence(incident, anomaly, affected)

        await self._add_created_timeline(incident, cluster, created)
        await self._add_component_timeline(
            incident, affected, occurred_at=earliest, created=created
        )

        deployments, config_changes = await self._context_items(incident, cluster)
        await self._add_context_evidence(
            incident, deployments, config_changes, earliest
        )

        await self._refresh_summary(
            incident, anomalies, affected, deployments, config_changes
        )
        return created, linked

    async def _insert_incident(
        self,
        incident: Incident,
        project_id: uuid.UUID,
        fingerprint: str,
    ) -> Optional[Incident]:
        """Insert a new incident, or return the one a concurrent pass created.

        The unique index on ``(project_id, fingerprint)`` for live incidents is
        what makes this safe: the conflicting insert blocks until the other
        transaction finishes and then fails, so by the time the error surfaces,
        the winning row is readable. The insert is wrapped in a savepoint so the
        failure rolls back only this statement — the surrounding correlation
        pass keeps its other work and continues as an update.
        """
        #: The savepoint is opened *before* the row is added: ``begin_nested``
        #: flushes pending state, and a pending incident at that point would
        #: raise outside the guard below.
        savepoint = await self._session.begin_nested()
        self._session.add(incident)
        try:
            await self._session.flush()
        except IntegrityError:
            await savepoint.rollback()
            existing = await self._find_incident(project_id, fingerprint)
            if existing is None:
                #: Not the fingerprint race (a foreign key, another constraint):
                #: re-raise rather than swallow a genuine persistence bug.
                raise
            return existing
        await savepoint.commit()
        return None

    async def _find_incident(
        self, project_id: uuid.UUID, fingerprint: str
    ) -> Optional[Incident]:
        stmt = (
            select(Incident)
            .where(
                Incident.project_id == project_id,
                Incident.fingerprint == fingerprint,
                Incident.status != IncidentStatus.CLOSED,
            )
            .order_by(Incident.detected_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    def _title(
        self, cluster: CorrelationCluster, affected: list[AffectedComponent]
    ) -> str:
        component = next(
            (c.name for c in affected if c.classification == CLASS_DIRECTLY_OBSERVED),
            None,
        )
        dominant = (
            cluster.dominant_anomaly_type.value
            if isinstance(cluster.dominant_anomaly_type, AnomalyType)
            else cluster.dominant_anomaly_type
        )
        subject = component or "Unattributed"
        return f"{subject}: {dominant or 'correlated anomalies'} ({len(cluster.anomalies)} anomalies)"

    def _incident_severity(
        self, cluster: CorrelationCluster, affected: list[AffectedComponent]
    ) -> IncidentSeverity:
        """Severity from the strongest anomaly, escalated by blast radius (§7).

        Uses the same explainable severity engine as anomalies — an incident
        severity is never an unexplained number.
        """
        base = AnomalySeverity(cluster.severity)
        decision = compute_severity(
            SeveritySignals(
                base=base,
                duration_seconds=cluster.time_span_seconds or None,
                affected_downstream=max(0, len(affected) - 1),
            )
        )
        return IncidentSeverity(decision.severity.value)

    # -- Affected components (§30) -----------------------------------------
    async def _classify_components(
        self, cluster: CorrelationCluster
    ) -> list[AffectedComponent]:
        observed_ids = cluster.component_ids
        if not observed_ids:
            return []

        names = await self._component_names(observed_ids)
        counts: dict[uuid.UUID, int] = {}
        severities: dict[uuid.UUID, AnomalySeverity] = {}
        for anomaly in cluster.anomalies:
            if anomaly.component_id is None:
                continue
            counts[anomaly.component_id] = counts.get(anomaly.component_id, 0) + 1
            current = severities.get(anomaly.component_id, AnomalySeverity.LOW)
            severities[anomaly.component_id] = max_severity(
                current, AnomalySeverity(anomaly.severity)
            )

        affected: list[AffectedComponent] = [
            AffectedComponent(
                component_id=component_id,
                name=names.get(component_id),
                classification=CLASS_DIRECTLY_OBSERVED,
                reason="has a directly observed anomaly in this incident",
                anomaly_count=counts.get(component_id, 0),
                severity=severities.get(component_id),
            )
            for component_id in observed_ids
        ]

        # Direction matters: an edge source -> target means "source depends on
        # target". Callers of an observed component are UPSTREAM_CONTEXT; the
        # components it depends on are DOWNSTREAM_CONTEXT.
        seen = set(observed_ids)
        dep_stmt = (
            select(
                ComponentDependency.source_component_id,
                ComponentDependency.target_component_id,
            )
            .where(
                ComponentDependency.source_component_id.in_(observed_ids)
                | ComponentDependency.target_component_id.in_(observed_ids)
            )
            .limit(2000)
        )
        for source, target in (await self._session.execute(dep_stmt)).all():
            if source in observed_ids and target not in seen:
                seen.add(target)
                affected.append(
                    AffectedComponent(
                        component_id=target,
                        name=None,
                        classification=CLASS_DOWNSTREAM_CONTEXT,
                        reason="is a direct dependency of an observed component",
                    )
                )
            elif target in observed_ids and source not in seen:
                seen.add(source)
                affected.append(
                    AffectedComponent(
                        component_id=source,
                        name=None,
                        classification=CLASS_UPSTREAM_CONTEXT,
                        reason="depends directly on an observed component",
                    )
                )

        # Evidence-derived graph edges without a clear direction are context only.
        node_stmt = select(GraphNode.id, GraphNode.entity_id).where(
            GraphNode.entity_kind == "system_component",
            GraphNode.entity_id.in_(seen),
        )
        node_to_entity = {
            node_id: entity_id
            for node_id, entity_id in (await self._session.execute(node_stmt)).all()
            if entity_id is not None
        }
        if node_to_entity:
            node_ids = set(node_to_entity)
            edge_stmt = (
                select(GraphEdge.source_node_id, GraphEdge.target_node_id)
                .where(
                    GraphEdge.status == GraphEdgeStatus.ACTIVE,
                    GraphEdge.source_node_id.in_(node_ids),
                    GraphEdge.target_node_id.in_(node_ids),
                )
                .limit(2000)
            )
            for source_node, target_node in (
                await self._session.execute(edge_stmt)
            ).all():
                for entity in (
                    node_to_entity.get(source_node),
                    node_to_entity.get(target_node),
                ):
                    if entity is None or entity in seen:
                        continue
                    seen.add(entity)
                    affected.append(
                        AffectedComponent(
                            component_id=entity,
                            name=None,
                            classification=CLASS_DEPENDENCY_CONTEXT,
                            reason="is structurally related to an observed component",
                        )
                    )

        # Resolve names for the context components too.
        missing = [c.component_id for c in affected if c.name is None]
        if missing:
            extra_names = await self._component_names(missing)
            for component in affected:
                if component.name is None:
                    component.name = extra_names.get(component.component_id)
        return affected

    async def _component_names(
        self, component_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        if not component_ids:
            return {}
        stmt = select(SystemComponent.id, SystemComponent.name).where(
            SystemComponent.id.in_(list(component_ids))
        )
        return {
            component_id: name
            for component_id, name in (await self._session.execute(stmt)).all()
        }

    # -- Timeline (§27) -----------------------------------------------------
    async def _add_created_timeline(
        self, incident: Incident, cluster: CorrelationCluster, created: bool
    ) -> None:
        if not created:
            return
        self._session.add(
            IncidentTimelineEvent(
                incident_id=incident.id,
                project_id=incident.project_id,
                environment_id=incident.environment_id,
                event_type=TimelineEventType.INCIDENT_CREATED,
                occurred_at=cluster.earliest or self._now,
                title="Incident created from correlated anomalies",
                description=incident.correlation_rationale.get("disclaimer")
                if incident.correlation_rationale
                else None,
                provenance="correlation",
                metadata_={"rationale": incident.correlation_rationale},
            )
        )

    async def _add_component_timeline(
        self,
        incident: Incident,
        affected: list[AffectedComponent],
        *,
        occurred_at: datetime,
        created: bool,
    ) -> None:
        if not created:
            return
        for component in affected:
            if component.classification != CLASS_DIRECTLY_OBSERVED:
                continue
            self._session.add(
                IncidentTimelineEvent(
                    incident_id=incident.id,
                    project_id=incident.project_id,
                    environment_id=incident.environment_id,
                    event_type=TimelineEventType.COMPONENT_AFFECTED,
                    occurred_at=occurred_at,
                    title=f"Component affected: {component.name or component.component_id}",
                    description=component.reason,
                    component_id=component.component_id,
                    is_context_only=False,
                    provenance="anomaly",
                )
            )

    async def _anomaly_has_timeline(
        self, incident_id: uuid.UUID, anomaly_id: uuid.UUID
    ) -> bool:
        stmt = select(IncidentTimelineEvent.id).where(
            IncidentTimelineEvent.incident_id == incident_id,
            IncidentTimelineEvent.anomaly_id == anomaly_id,
            IncidentTimelineEvent.event_type == TimelineEventType.ANOMALY_DETECTED,
        )
        return (await self._session.execute(stmt)).first() is not None

    # -- Evidence (§28–§29) -------------------------------------------------
    async def _evidence_exists(
        self,
        incident_id: uuid.UUID,
        evidence_type: EvidenceType,
        source_id: str,
    ) -> bool:
        stmt = select(IncidentEvidence.id).where(
            IncidentEvidence.incident_id == incident_id,
            IncidentEvidence.evidence_type == evidence_type,
            IncidentEvidence.source_id == source_id,
        )
        return (await self._session.execute(stmt)).first() is not None

    async def _add_anomaly_evidence(
        self,
        incident: Incident,
        anomaly: Anomaly,
        affected: list[AffectedComponent],
    ) -> None:
        source_id = str(anomaly.id)
        already = await self._evidence_exists(
            incident.id, EvidenceType.ANOMALY, source_id
        )
        classification = next(
            (
                c.classification
                for c in affected
                if c.component_id == anomaly.component_id
            ),
            CLASS_DIRECTLY_OBSERVED,
        )
        reason = (
            "directly observed on an affected component within the correlation window"
            if classification == CLASS_DIRECTLY_OBSERVED
            else f"component is {classification.lower().replace('_', ' ')}"
        )
        if not already:
            self._session.add(
                IncidentEvidence(
                    incident_id=incident.id,
                    evidence_type=EvidenceType.ANOMALY,
                    source_id=source_id,
                    timestamp=ensure_utc(anomaly.detected_at) or self._now,
                    component_id=anomaly.component_id,
                    observed_value=(
                        str(anomaly.observed_value)
                        if anomaly.observed_value is not None
                        else None
                    ),
                    expected_value=(
                        str(anomaly.expected_value)
                        if anomaly.expected_value is not None
                        else None
                    ),
                    severity=IncidentSeverity(AnomalySeverity(anomaly.severity).value),
                    confidence=anomaly.confidence,
                    provenance=(
                        anomaly.source.value
                        if hasattr(anomaly.source, "value")
                        else str(anomaly.source)
                    ),
                    relevance_reason=reason,
                    anomaly_id=anomaly.id,
                    relevance_score=1.0
                    if classification == CLASS_DIRECTLY_OBSERVED
                    else 0.5,
                    description=anomaly.description,
                )
            )
        if not await self._anomaly_has_timeline(incident.id, anomaly.id):
            self._session.add(
                IncidentTimelineEvent(
                    incident_id=incident.id,
                    project_id=incident.project_id,
                    environment_id=incident.environment_id,
                    event_type=TimelineEventType.ANOMALY_DETECTED,
                    occurred_at=ensure_utc(anomaly.detected_at) or self._now,
                    title=anomaly.description
                    or f"Anomaly detected: {anomaly.anomaly_type}",
                    description=anomaly.description,
                    component_id=anomaly.component_id,
                    anomaly_id=anomaly.id,
                    provenance=(
                        anomaly.source.value
                        if hasattr(anomaly.source, "value")
                        else str(anomaly.source)
                    ),
                )
            )

    async def _context_items(
        self, incident: Incident, cluster: CorrelationCluster
    ) -> tuple[list[SummaryTimelineItem], list[SummaryTimelineItem]]:
        """Nearby deployments and configuration changes (temporal context only)."""
        earliest = cluster.earliest or self._now
        window = settings.INCIDENT_CONTEXT_WINDOW_SECONDS
        start = earliest - timedelta(seconds=window)
        end = (cluster.latest or earliest) + timedelta(seconds=window)

        deployments: list[SummaryTimelineItem] = []
        dep_stmt = (
            select(DeploymentEvent)
            .where(
                DeploymentEvent.project_id == incident.project_id,
                DeploymentEvent.deployed_at >= start,
                DeploymentEvent.deployed_at <= end,
            )
            .order_by(DeploymentEvent.deployed_at)
            .limit(_MAX_CONTEXT_EVIDENCE)
        )
        if incident.environment_id is not None:
            dep_stmt = dep_stmt.where(
                (DeploymentEvent.environment_id == incident.environment_id)
                | (DeploymentEvent.environment_id.is_(None))
            )
        for event in (await self._session.execute(dep_stmt)).scalars().all():
            deployed_at = ensure_utc(event.deployed_at) or earliest
            seconds = (earliest - deployed_at).total_seconds()
            label = f"Deployment {event.deployment_id}" + (
                f" (version {event.version})" if event.version else ""
            )
            deployments.append(
                SummaryTimelineItem(
                    label=label,
                    occurred_at=deployed_at,
                    seconds_from_first_anomaly=seconds,
                )
            )

        config_items: list[SummaryTimelineItem] = []
        cfg_stmt = (
            select(ConfigurationChangeEvent)
            .where(
                ConfigurationChangeEvent.project_id == incident.project_id,
                ConfigurationChangeEvent.timestamp >= start,
                ConfigurationChangeEvent.timestamp <= end,
            )
            .order_by(ConfigurationChangeEvent.timestamp)
            .limit(_MAX_CONTEXT_EVIDENCE)
        )
        if incident.environment_id is not None:
            cfg_stmt = cfg_stmt.where(
                (ConfigurationChangeEvent.environment_id == incident.environment_id)
                | (ConfigurationChangeEvent.environment_id.is_(None))
            )
        for change in (await self._session.execute(cfg_stmt)).scalars().all():
            changed_at = ensure_utc(change.timestamp) or earliest
            seconds = (earliest - changed_at).total_seconds()
            label = f"Configuration change {change.change_id}"
            config_items.append(
                SummaryTimelineItem(
                    label=label,
                    occurred_at=changed_at,
                    seconds_from_first_anomaly=seconds,
                )
            )
        return deployments, config_items

    async def _add_context_evidence(
        self,
        incident: Incident,
        deployments: list[SummaryTimelineItem],
        config_changes: list[SummaryTimelineItem],
        earliest: datetime,
    ) -> None:
        for item in deployments:
            source_id = f"deployment:{item.occurred_at.isoformat()}:{item.label}"
            if await self._evidence_exists(
                incident.id, EvidenceType.DEPLOYMENT, source_id
            ):
                continue
            self._session.add(
                IncidentEvidence(
                    incident_id=incident.id,
                    evidence_type=EvidenceType.DEPLOYMENT,
                    source_id=source_id,
                    timestamp=item.occurred_at,
                    provenance="deployment",
                    # Wording matters: context, never cause.
                    relevance_reason=(
                        "occurred "
                        f"{abs(item.seconds_from_first_anomaly or 0):.0f}s "
                        "from the first anomaly (temporal context only)"
                    ),
                    description=item.label,
                    relevance_score=0.5,
                )
            )
            self._session.add(
                IncidentTimelineEvent(
                    incident_id=incident.id,
                    project_id=incident.project_id,
                    environment_id=incident.environment_id,
                    event_type=TimelineEventType.DEPLOYMENT_OCCURRED,
                    occurred_at=item.occurred_at,
                    title=item.label,
                    description=(
                        "Temporal context only — no causal relationship is claimed."
                    ),
                    is_context_only=True,
                    provenance="deployment",
                )
            )

        for item in config_changes:
            source_id = f"config:{item.occurred_at.isoformat()}:{item.label}"
            if await self._evidence_exists(
                incident.id, EvidenceType.CONFIGURATION_CHANGE, source_id
            ):
                continue
            self._session.add(
                IncidentEvidence(
                    incident_id=incident.id,
                    evidence_type=EvidenceType.CONFIGURATION_CHANGE,
                    source_id=source_id,
                    timestamp=item.occurred_at,
                    provenance="configuration",
                    relevance_reason=(
                        "recorded "
                        f"{abs(item.seconds_from_first_anomaly or 0):.0f}s "
                        "from the first anomaly (temporal context only)"
                    ),
                    description=item.label,
                    relevance_score=0.5,
                )
            )
            self._session.add(
                IncidentTimelineEvent(
                    incident_id=incident.id,
                    project_id=incident.project_id,
                    environment_id=incident.environment_id,
                    event_type=TimelineEventType.CONFIGURATION_CHANGED,
                    occurred_at=item.occurred_at,
                    title=item.label,
                    description=(
                        "Temporal context only — no causal relationship is claimed."
                    ),
                    is_context_only=True,
                    provenance="configuration",
                )
            )

    # -- Summary (§33) ------------------------------------------------------
    async def _refresh_summary(
        self,
        incident: Incident,
        anomalies: list[Anomaly],
        affected: list[AffectedComponent],
        deployments: list[SummaryTimelineItem],
        config_changes: list[SummaryTimelineItem],
    ) -> None:
        names = await self._component_names(
            [a.component_id for a in anomalies if a.component_id is not None]
        )
        summary_anomalies = [
            SummaryAnomaly(
                anomaly_type=(
                    anomaly.anomaly_type.value
                    if isinstance(anomaly.anomaly_type, AnomalyType)
                    else str(anomaly.anomaly_type)
                ),
                severity=AnomalySeverity(anomaly.severity).value,
                component_name=(
                    names.get(anomaly.component_id)
                    if anomaly.component_id is not None
                    else None
                ),
                metric_name=anomaly.metric_name,
                pattern_template=anomaly.pattern_template,
                observed_value=anomaly.observed_value,
                expected_value=anomaly.expected_value,
                deviation=anomaly.deviation,
                detected_at=ensure_utc(anomaly.detected_at) or self._now,
                suppressed=bool(anomaly.suppressed),
            )
            for anomaly in anomalies
        ]
        summary_components = [
            SummaryComponent(name=c.name, classification=c.classification)
            for c in affected
        ]
        primary_name = next(
            (
                c.name
                for c in affected
                if c.component_id == incident.primary_component_id
            ),
            None,
        )
        text, generated_from = build_incident_summary(
            title=incident.title,
            severity=IncidentSeverity(incident.severity).value,
            status=IncidentStatus(incident.status).value,
            detected_at=ensure_utc(incident.detected_at) or self._now,
            resolved_at=ensure_utc(incident.resolved_at),
            primary_component_name=primary_name,
            anomalies=summary_anomalies,
            components=summary_components,
            deployments=deployments,
            config_changes=config_changes,
        )
        incident.summary = text
        incident.correlation_rationale = {
            **(incident.correlation_rationale or {}),
            "summary_generated_from": generated_from,
            "summary_generated_at": self._now.isoformat(),
        }

    # -- Lifecycle (§26) ----------------------------------------------------
    async def transition(
        self,
        incident: Incident,
        target: IncidentStatus,
        *,
        actor: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Incident:
        """Apply a validated status transition and record it on the timeline."""
        current = IncidentStatus(incident.status)
        target_status = incident_state.assert_transition(current, target)
        if target_status is current:
            return incident

        incident.status = target_status
        incident.status_changed_by = actor
        timestamp_field = incident_state.timestamp_field_for(target_status)
        if timestamp_field is not None:
            setattr(incident, timestamp_field, self._now)
        if target_status is IncidentStatus.RESOLVED:
            incident.resolved_at = incident.resolved_at or self._now

        event_type = TimelineEventType.INCIDENT_STATUS_CHANGED
        if target_status is IncidentStatus.ACKNOWLEDGED:
            event_type = TimelineEventType.INCIDENT_ACKNOWLEDGED
        elif target_status is IncidentStatus.MITIGATED:
            event_type = TimelineEventType.INCIDENT_MITIGATED
        elif target_status is IncidentStatus.RESOLVED:
            event_type = TimelineEventType.INCIDENT_RESOLVED

        self._session.add(
            IncidentTimelineEvent(
                incident_id=incident.id,
                project_id=incident.project_id,
                environment_id=incident.environment_id,
                event_type=event_type,
                occurred_at=self._now,
                title=f"Status changed to {target_status.value}",
                description=note,
                provenance="lifecycle",
                metadata_={
                    "from": current.value,
                    "to": target_status.value,
                    "actor": actor,
                },
            )
        )

        #: Phase 10 §6. An incident reaching a terminal status is the event the
        #: learning pipeline consumes. Best-effort by construction: history is
        #: recorded here, and a learning problem must never block the
        #: transition that a responder asked for.
        if target_status in (IncidentStatus.RESOLVED, IncidentStatus.CLOSED):
            from app.services.learning_hooks import record_incident_completed

            await record_incident_completed(
                self._session, incident=incident, actor=actor
            )
        return incident

    async def _auto_resolve_stale(
        self, project_id: uuid.UUID, environment_id: Optional[uuid.UUID]
    ) -> None:
        """Resolve open incidents whose anomalies are all resolved/expired.

        Automatic, but auditable: the transition is attributed to
        ``system:auto_resolve`` and written to the timeline.
        """
        stmt = select(Incident).where(
            Incident.project_id == project_id,
            Incident.status.in_(
                [
                    IncidentStatus.OPEN,
                    IncidentStatus.ACKNOWLEDGED,
                    IncidentStatus.INVESTIGATING,
                ]
            ),
        )
        # Exact scope, matching correlation: a project-scope pass must never
        # touch (or resolve) incidents that belong to a specific environment.
        stmt = stmt.where(
            Incident.environment_id == environment_id
            if environment_id is not None
            else Incident.environment_id.is_(None)
        )
        incidents = list((await self._session.execute(stmt)).scalars().all())
        terminal = {
            "RESOLVED",
            "EXPIRED",
        }
        for incident in incidents:
            rows = list(
                (
                    await self._session.execute(
                        select(Anomaly.status).where(Anomaly.incident_id == incident.id)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                continue
            if all(
                (s.value if hasattr(s, "value") else str(s)) in terminal for s in rows
            ):
                await self.transition(
                    incident,
                    IncidentStatus.RESOLVED,
                    actor="system:auto_resolve",
                    note="All correlated anomalies are resolved or expired",
                )


__all__ = [
    "IncidentManager",
    "IncidentRunResult",
    "AffectedComponent",
    "CLASS_DIRECTLY_OBSERVED",
    "CLASS_UPSTREAM_CONTEXT",
    "CLASS_DOWNSTREAM_CONTEXT",
    "CLASS_DEPENDENCY_CONTEXT",
]
