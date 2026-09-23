"""ARGUS Reliability Context (Phase 11 §7, §8).

The shared context object modules exchange, and the frozen snapshot of it at one
instant.

Why a context object exists at all: a recommendation, a remediation assessment,
a postmortem and an audit entry all need the *same* set of references — the
project, the environment, the components, the incident, the analysis, the
reproduction — and each of them independently re-resolving those references is
how the phases started to drift apart. One builder, one shape.

Why snapshots exist: every conclusion above is made **at a time**. A
recommendation produced on Tuesday must be readable on Friday as it was reasoned
out on Tuesday, not silently re-evaluated against Friday's world. A snapshot
stores the bounded context that was available, plus a fingerprint so two
snapshots of an unchanged situation can be recognised as identical.

Bounded is a rule, not a nicety: a snapshot caps every collection it includes
(§65). A snapshot that tries to capture "everything" is both enormous and slower
than the thing it was taken for.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.platform import ReliabilityContextSnapshot

logger = logging.getLogger(__name__)

#: Which parts a context can include. Named so a caller can ask for exactly what
#: it needs instead of paying for a full assembly.
CONTEXT_SECTIONS = (
    "project",
    "environment",
    "components",
    "incident",
    "anomalies",
    "graph",
    "analysis",
    "reproduction",
    "debugging",
    "patch",
    "prediction",
    "remediation",
    "knowledge",
    "state",
    "changes",
)

#: Caps applied per section. Deliberately small: a context is a summary a module
#: can read in full, not an export.
SECTION_LIMITS = {
    "components": 50,
    "anomalies": 25,
    "graph": 50,
    "analysis": 10,
    "reproduction": 10,
    "patch": 10,
    "prediction": 10,
    "remediation": 10,
    "debugging": 5,
    "knowledge": 5,
    "changes": 15,
}


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@dataclass
class ReliabilityContext:
    """The §7 shared context: references, not copies."""

    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    component_id: Optional[uuid.UUID] = None
    component_ids: list[uuid.UUID] = field(default_factory=list)
    incident_id: Optional[uuid.UUID] = None
    case_id: Optional[uuid.UUID] = None
    anomaly_ids: list[uuid.UUID] = field(default_factory=list)
    graph_node_ids: list[uuid.UUID] = field(default_factory=list)
    analysis_id: Optional[uuid.UUID] = None
    candidate_ids: list[uuid.UUID] = field(default_factory=list)
    reproduction_experiment_ids: list[uuid.UUID] = field(default_factory=list)
    debug_session_id: Optional[uuid.UUID] = None
    patch_ids: list[uuid.UUID] = field(default_factory=list)
    hypothesis_id: Optional[uuid.UUID] = None
    forecast_ids: list[uuid.UUID] = field(default_factory=list)
    remediation_action_ids: list[uuid.UUID] = field(default_factory=list)
    knowledge_ids: list[uuid.UUID] = field(default_factory=list)
    change_ids: list[uuid.UUID] = field(default_factory=list)
    #: A short human-readable statement of what this context is about.
    description: Optional[str] = None
    #: Anything the caller wants carried without being a first-class reference.
    attributes: dict[str, Any] = field(default_factory=dict)
    #: Sections that could not be assembled, with the reason (§59).
    unavailable: dict[str, str] = field(default_factory=dict)

    def references(self) -> dict[str, Any]:
        """Every stored row this context points at, as strings."""
        return {
            "project_id": str(self.project_id),
            "environment_id": str(self.environment_id) if self.environment_id else None,
            "component_id": str(self.component_id) if self.component_id else None,
            "component_ids": [str(c) for c in self.component_ids],
            "incident_id": str(self.incident_id) if self.incident_id else None,
            "case_id": str(self.case_id) if self.case_id else None,
            "anomaly_ids": [str(a) for a in self.anomaly_ids],
            "graph_node_ids": [str(n) for n in self.graph_node_ids],
            "analysis_id": str(self.analysis_id) if self.analysis_id else None,
            "candidate_ids": [str(c) for c in self.candidate_ids],
            "reproduction_experiment_ids": [
                str(e) for e in self.reproduction_experiment_ids
            ],
            "debug_session_id": str(self.debug_session_id)
            if self.debug_session_id
            else None,
            "patch_ids": [str(p) for p in self.patch_ids],
            "hypothesis_id": str(self.hypothesis_id) if self.hypothesis_id else None,
            "forecast_ids": [str(f) for f in self.forecast_ids],
            "remediation_action_ids": [str(a) for a in self.remediation_action_ids],
            "knowledge_ids": [str(k) for k in self.knowledge_ids],
            "change_ids": [str(c) for c in self.change_ids],
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "references": self.references(),
            "description": self.description,
            "attributes": self.attributes,
            "unavailable": self.unavailable,
        }

    def fingerprint(self) -> str:
        """A stable identity for this context's *references*.

        Attributes are excluded on purpose: a context that differs only in a
        caller-supplied label describes the same situation, and deduplicating
        snapshots on it would be wrong.
        """
        payload = json.dumps(self.references(), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:64]

    def describe(self) -> str:
        if self.description:
            return self.description
        parts = []
        if self.incident_id:
            parts.append(f"incident {self.incident_id}")
        if self.component_id:
            parts.append(f"component {self.component_id}")
        if not parts:
            parts.append(f"project {self.project_id}")
        return "context over " + ", ".join(parts)


async def build_context(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = None,
    component_id: Optional[uuid.UUID] = None,
    incident_id: Optional[uuid.UUID] = None,
    case_id: Optional[uuid.UUID] = None,
    include: Optional[Sequence[str]] = None,
    include_state: bool = False,
    settings: Optional[Settings] = None,
    as_of: Optional[datetime] = None,
) -> ReliabilityContext:
    """Assemble the shared context for a situation (§7).

    The incident is the strongest anchor: when it is known, its component,
    analyses, reproductions and remediations are discovered from it, because
    those links were stored by the phases that created them. A component-only
    context is assembled from the component outward and honestly states what it
    could not answer.
    """
    from app.models.anomaly import Anomaly
    from app.models.causal import CausalAnalysis, RootCauseCandidate
    from app.models.code import DebugSession
    from app.models.fix import FixHypothesis, Patch
    from app.models.incident import Incident
    from app.models.reliability import ReliabilityForecast
    from app.models.remediation import RemediationAction
    from app.models.reproduction import ReproductionExperiment
    from app.models.system import SystemComponent

    settings = settings or get_settings()
    moment = _aware(as_of) or datetime.now(timezone.utc)
    include_set = set(include) if include else None

    context = ReliabilityContext(
        project_id=project_id,
        environment_id=environment_id,
        component_id=component_id,
        incident_id=incident_id,
        case_id=case_id,
    )

    def wants(section: str) -> bool:
        return include_set is None or section in include_set

    # -- the incident, and everything anchored on it
    if incident_id is not None:
        incident = await session.get(Incident, incident_id)
        if incident is None or incident.project_id != project_id:
            context.unavailable["incident"] = "no such incident in this project"
            context.incident_id = None
        else:
            context.environment_id = context.environment_id or incident.environment_id
            context.component_id = context.component_id or incident.primary_component_id
            if wants("anomalies"):
                anomaly_rows = (
                    await session.scalars(
                        select(Anomaly)
                        .where(Anomaly.incident_id == incident.id)
                        .order_by(Anomaly.detected_at)
                        .limit(SECTION_LIMITS["anomalies"])
                    )
                ).all()
                context.anomaly_ids = [row.id for row in anomaly_rows]
            if wants("analysis"):
                analyses = (
                    await session.scalars(
                        select(CausalAnalysis)
                        .where(CausalAnalysis.incident_id == incident.id)
                        .order_by(CausalAnalysis.created_at.desc())
                        .limit(SECTION_LIMITS["analysis"])
                    )
                ).all()
                if analyses:
                    context.analysis_id = analyses[0].id
                    candidates = (
                        await session.scalars(
                            select(RootCauseCandidate)
                            .where(RootCauseCandidate.analysis_id == analyses[0].id)
                            .order_by(RootCauseCandidate.score.desc())
                            .limit(5)
                        )
                    ).all()
                    context.candidate_ids = [row.id for row in candidates]
            if wants("reproduction"):
                experiments = (
                    await session.scalars(
                        select(ReproductionExperiment)
                        .where(ReproductionExperiment.incident_id == incident.id)
                        .order_by(ReproductionExperiment.created_at.desc())
                        .limit(SECTION_LIMITS["reproduction"])
                    )
                ).all()
                context.reproduction_experiment_ids = [row.id for row in experiments]
            if wants("patch"):
                hypotheses = (
                    await session.scalars(
                        select(FixHypothesis)
                        .where(FixHypothesis.incident_id == incident.id)
                        .order_by(FixHypothesis.created_at.desc())
                        .limit(SECTION_LIMITS["patch"])
                    )
                ).all()
                if hypotheses:
                    context.hypothesis_id = hypotheses[0].id
                    patches = (
                        await session.scalars(
                            select(Patch)
                            .where(
                                Patch.fix_hypothesis_id.in_([h.id for h in hypotheses])
                            )
                            .order_by(Patch.created_at.desc())
                            .limit(SECTION_LIMITS["patch"])
                        )
                    ).all()
                    context.patch_ids = [row.id for row in patches]
            if wants("debugging"):
                sessions = (
                    await session.scalars(
                        select(DebugSession)
                        .where(DebugSession.incident_id == incident.id)
                        .order_by(DebugSession.created_at.desc())
                        .limit(SECTION_LIMITS["debugging"])
                    )
                ).all()
                if sessions:
                    context.debug_session_id = sessions[0].id
            if wants("remediation"):
                actions = (
                    await session.scalars(
                        select(RemediationAction)
                        .where(RemediationAction.incident_id == incident.id)
                        .order_by(RemediationAction.created_at.desc())
                        .limit(SECTION_LIMITS["remediation"])
                    )
                ).all()
                context.remediation_action_ids = [row.id for row in actions]

    # -- component-anchored sections
    if context.component_id is not None:
        component = await session.get(SystemComponent, context.component_id)
        if component is None or component.project_id != project_id:
            context.unavailable["component"] = "no such component in this project"
            context.component_id = None
        else:
            context.component_ids = [component.id]

    if context.component_id is None and wants("components"):
        component_stmt = (
            select(SystemComponent.id)
            .where(SystemComponent.project_id == project_id)
            .limit(SECTION_LIMITS["components"])
        )
        if context.environment_id is not None:
            component_stmt = component_stmt.where(
                SystemComponent.environment_id == context.environment_id
            )
        context.component_ids = list((await session.scalars(component_stmt)).all())

    if wants("anomalies") and not context.anomaly_ids and context.component_ids:
        anomaly_rows = (
            await session.scalars(
                select(Anomaly)
                .where(
                    Anomaly.project_id == project_id,
                    Anomaly.component_id.in_(context.component_ids),
                )
                .order_by(Anomaly.detected_at.desc())
                .limit(SECTION_LIMITS["anomalies"])
            )
        ).all()
        context.anomaly_ids = [row.id for row in anomaly_rows]

    if wants("prediction") and context.component_ids:
        forecasts = (
            await session.scalars(
                select(ReliabilityForecast)
                .where(
                    ReliabilityForecast.project_id == project_id,
                    ReliabilityForecast.component_id.in_(context.component_ids),
                )
                .order_by(ReliabilityForecast.generated_at.desc())
                .limit(SECTION_LIMITS["prediction"])
            )
        ).all()
        context.forecast_ids = [row.id for row in forecasts]

    if wants("knowledge"):
        try:
            from app.models.intelligence import (
                KnowledgeStatus,
                ReliabilityKnowledge,
            )

            knowledge_stmt = (
                select(ReliabilityKnowledge.id)
                .where(
                    ReliabilityKnowledge.project_id == project_id,
                    ReliabilityKnowledge.status.in_(
                        [KnowledgeStatus.ACTIVE, KnowledgeStatus.VALIDATED]
                    ),
                )
                .order_by(ReliabilityKnowledge.sample_count.desc())
                .limit(SECTION_LIMITS["knowledge"])
            )
            context.knowledge_ids = list((await session.scalars(knowledge_stmt)).all())
        except Exception:  # pragma: no cover - learning is optional (§59)
            context.unavailable["knowledge"] = "the learning layer did not answer"

    if wants("graph") and context.component_ids:
        try:
            from app.models.graph import GraphNode

            #: Graph nodes reference their subject as ``(entity_kind,
            #: entity_id)``; a component's node is the one whose entity points
            #: at the component id. Matching the alias table instead would
            #: silently miss components that were only ever seen in traces.
            nodes = (
                await session.scalars(
                    select(GraphNode.id)
                    .where(
                        GraphNode.project_id == project_id,
                        GraphNode.entity_id.in_(context.component_ids),
                    )
                    .limit(SECTION_LIMITS["graph"])
                )
            ).all()
            context.graph_node_ids = list(nodes)
        except Exception:  # pragma: no cover - graph is optional (§59)
            context.unavailable["graph"] = "the graph layer did not answer"

    if wants("changes"):
        try:
            from app.models.deployment import DeploymentEvent

            deployments = (
                await session.scalars(
                    select(DeploymentEvent.id)
                    .where(
                        DeploymentEvent.project_id == project_id,
                        DeploymentEvent.deployed_at
                        >= moment
                        - timedelta(days=settings.PLATFORM_STATE_CHANGE_WINDOW_DAYS),
                    )
                    .order_by(DeploymentEvent.deployed_at.desc())
                    .limit(SECTION_LIMITS["changes"])
                )
            ).all()
            context.change_ids = list(deployments)
        except Exception:  # pragma: no cover - deployments are optional
            context.unavailable["changes"] = "deployment history did not answer"

    if include_state:
        context.attributes["state"] = {}
    return context


async def snapshot_context(
    session: AsyncSession,
    *,
    context: ReliabilityContext,
    scope: str,
    as_of: Optional[datetime] = None,
    created_by: Optional[str] = None,
    include_system_state: bool = False,
    settings: Optional[Settings] = None,
) -> ReliabilityContextSnapshot:
    """Freeze a context into a stored snapshot (§8)."""
    settings = settings or get_settings()
    moment = _aware(as_of) or datetime.now(timezone.utc)
    snapshot: dict[str, Any] = {"context": context.as_dict()}
    if include_system_state:
        try:
            from app.services.system_state import build_system_state

            state = await build_system_state(
                session,
                project_id=context.project_id,
                environment_id=context.environment_id,
                now=moment,
                settings=settings,
                include=(
                    "health",
                    "active_incidents",
                    "active_anomalies",
                    "predicted_risks",
                ),
            )
            snapshot["state"] = {
                "as_of": state.as_of.isoformat(),
                "health": state.health,
                "active_incidents": state.active_incidents,
                "active_anomalies": state.active_anomalies,
                "predicted_risks": state.predicted_risks,
                "limitations": state.limitations,
            }
        except Exception:  # pragma: no cover - a snapshot must still be taken
            logger.warning("system state unavailable for snapshot", exc_info=True)
            snapshot["state"] = {"unavailable": "system state assembly failed"}

    row = ReliabilityContextSnapshot(
        project_id=context.project_id,
        environment_id=context.environment_id,
        component_id=context.component_id,
        case_id=context.case_id,
        incident_id=context.incident_id,
        scope=scope,
        snapshot=snapshot,
        fingerprint=context.fingerprint(),
        as_of=moment,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    return row


async def get_snapshot(
    session: AsyncSession,
    *,
    snapshot_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
) -> Optional[ReliabilityContextSnapshot]:
    row = await session.get(ReliabilityContextSnapshot, snapshot_id)
    if row is None:
        return None
    if project_id is not None and row.project_id != project_id:
        return None
    return row


async def recent_snapshots(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    case_id: Optional[uuid.UUID] = None,
    limit: int = 25,
) -> list[ReliabilityContextSnapshot]:
    stmt = (
        select(ReliabilityContextSnapshot)
        .where(ReliabilityContextSnapshot.project_id == project_id)
        .order_by(ReliabilityContextSnapshot.created_at.desc())
        .limit(limit)
    )
    if case_id is not None:
        stmt = stmt.where(ReliabilityContextSnapshot.case_id == case_id)
    return list((await session.scalars(stmt)).all())


__all__ = [
    "CONTEXT_SECTIONS",
    "SECTION_LIMITS",
    "ReliabilityContext",
    "build_context",
    "get_snapshot",
    "recent_snapshots",
    "snapshot_context",
]
