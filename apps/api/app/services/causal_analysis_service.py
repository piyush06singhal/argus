"""ARGUS Causal Analysis Service (Phase 4 §6, §29, §35, §36, §44, §46).

The orchestrator: one incident in → one **versioned, auditable, evidence-backed**
analysis out. It wires the analyzers in the documented order

    incident → anomalies → changes/traces/graph → temporal → candidates →
    causal graph → scoring → primary selection → persistence

and it is the only Phase 4 module that touches the database. Three rules are
enforced here rather than assumed:

* **Exact scope.** Every read is filtered by ``project_id`` *and* the
  incident's environment (``IS NULL`` when the incident is environment-less),
  so a production incident can never be explained by staging telemetry.
* **Honest answers.** No candidate reaching the documented threshold means the
  primary root cause stays ``UNKNOWN`` with ``INSUFFICIENT`` confidence (§29).
  Forcing a winner is a bug, not a feature.
* **Reproducibility.** The full bounded input set is hashed; asking for an
  analysis of an unchanged incident returns the existing version instead of
  minting a duplicate (§34 idempotency), while re-analysis after new evidence
  appends a new version and preserves the old one (§35).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import ensure_utc_or_now, utcnow
from app.models.anomaly import Anomaly, AnomalyObservation, AnomalyType
from app.models.causal import (
    AnalysisStatus,
    CandidateStatus,
    CandidateType,
    CausalAnalysis,
    CausalEvidence,
    CausalEvidenceCategory,
    CausalRelationship,
    ConfidenceLevel,
    EvidencePolarity,
    RootCauseCandidate,
)
from app.models.incident import Incident, IncidentEvidence
from app.models.system import ComponentCategory, SystemComponent
from app.services.candidate_generator import CandidateSeed, CausalCandidateGenerator
from app.services.causal_graph import (
    CausalGraphBuilder,
    CausalValidator,
    EvidenceSpec,
    HypothesisNode,
)
from app.services.change_analyzer import ChangeAnalyzer
from app.services.dependency_analyzer import DependencyAnalyzer
from app.services.root_cause_scorer import RootCauseScorer, ScoredCandidate
from app.services.trace_analyzer import TraceAnalyzer

logger = logging.getLogger(__name__)

#: Bumped when the engine's behaviour changes; stored on every analysis row so
#: a result can always be traced back to the code that produced it (§44).
ENGINE_VERSION = "phase4-1.0.0"

#: Component categories that make a component a data store for candidate typing.
_DATASTORE_CATEGORIES = {ComponentCategory.DATABASE, ComponentCategory.CACHE}
#: Categories that are outside our blast radius.
_EXTERNAL_CATEGORIES = {ComponentCategory.EXTERNAL_API}

#: Deterministic evidence strength per origin (documented, not tuned per demo).
_STRENGTH_BY_SOURCE: dict[str, float] = {
    "span_records": 0.9,
    "traces": 0.8,
    "anomalies": 0.7,
    "incident_evidence": 0.6,
    "component_dependencies": 0.6,
    "graph_edges": 0.5,
    "deployment_events": 0.5,
    "deployments": 0.5,
    "configuration_change_events": 0.5,
    "configuration_changes": 0.5,
}

_ANOMALY_TYPE_CATEGORY: dict[AnomalyType, CausalEvidenceCategory] = {
    AnomalyType.RESOURCE_USAGE_SPIKE: CausalEvidenceCategory.RESOURCE,
    AnomalyType.LOG_PATTERN_SPIKE: CausalEvidenceCategory.LOG,
    AnomalyType.HEALTH_DEGRADATION: CausalEvidenceCategory.HEALTH,
    AnomalyType.TRACE_FAILURE_SPIKE: CausalEvidenceCategory.TRACE,
}


@dataclass
class CausalAnalysisOutcome:
    """Result of an analysis request — the row plus whether it is new."""

    analysis: CausalAnalysis
    created: bool
    reused_reason: Optional[str] = None


class CausalAnalysisService:
    """Runs, versions, and stores root-cause analyses for one incident."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        generator: Optional[CausalCandidateGenerator] = None,
        builder: Optional[CausalGraphBuilder] = None,
        scorer: Optional[RootCauseScorer] = None,
    ) -> None:
        self._session = session
        self._settings = get_settings()
        self._generator = generator or CausalCandidateGenerator(
            max_candidates=self._settings.CAUSAL_MAX_CANDIDATES
        )
        self._builder = builder or CausalGraphBuilder(validator=CausalValidator())
        self._scorer = scorer or RootCauseScorer()

    # ------------------------------------------------------------------ public
    async def latest_analysis(
        self, *, project_id: uuid.UUID, incident_id: uuid.UUID
    ) -> Optional[CausalAnalysis]:
        """Newest stored analysis for the incident (``None`` when never run)."""
        stmt = (
            select(CausalAnalysis)
            .where(
                CausalAnalysis.project_id == project_id,
                CausalAnalysis.incident_id == incident_id,
            )
            .order_by(CausalAnalysis.analysis_version.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def analysis_history(
        self, *, project_id: uuid.UUID, incident_id: uuid.UUID, limit: int = 20
    ) -> list[CausalAnalysis]:
        """All versions, newest first — the audit trail (§43, §44)."""
        stmt = (
            select(CausalAnalysis)
            .where(
                CausalAnalysis.project_id == project_id,
                CausalAnalysis.incident_id == incident_id,
            )
            .order_by(CausalAnalysis.analysis_version.desc())
            .limit(max(1, min(100, limit)))
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def analyze_incident(
        self,
        *,
        project_id: uuid.UUID,
        incident: Incident,
        trigger: str = "api",
        requested_by: Optional[str] = None,
        force: bool = False,
    ) -> CausalAnalysisOutcome:
        """Run (or reuse) the causal analysis for one incident.

        ``incident`` must already be ownership-validated by the caller; this
        method re-checks ``project_id`` before reading anything.
        """
        if incident.project_id != project_id:
            raise ValueError("incident does not belong to this project")
        if not self._settings.CAUSAL_ANALYSIS_ENABLED and not force:
            raise RuntimeError(
                "Causal analysis is disabled (CAUSAL_ANALYSIS_ENABLED=false)"
            )

        anomalies = await self._load_anomalies(project_id, incident)
        fingerprint = self._input_fingerprint(incident, anomalies)

        latest = await self.latest_analysis(
            project_id=project_id, incident_id=incident.id
        )
        if (
            not force
            and latest is not None
            and latest.status is AnalysisStatus.COMPLETED
            and (latest.analysis_metadata or {}).get("input_fingerprint") == fingerprint
        ):
            return CausalAnalysisOutcome(
                analysis=latest,
                created=False,
                reused_reason=(
                    "Input evidence is unchanged since analysis "
                    f"v{latest.analysis_version}; returning the stored result "
                    "(use force=true to re-run)."
                ),
            )

        return await self._run(
            project_id=project_id,
            incident=incident,
            anomalies=anomalies,
            fingerprint=fingerprint,
            trigger=trigger,
            requested_by=requested_by,
        )

    # ------------------------------------------------------------------ loading
    async def _load_anomalies(
        self, project_id: uuid.UUID, incident: Incident
    ) -> list[Anomaly]:
        """Incident's anomalies, exact-scope and suppression-aware (§45, §46)."""
        stmt = (
            select(Anomaly)
            .where(
                Anomaly.project_id == project_id,
                Anomaly.incident_id == incident.id,
                Anomaly.suppressed.is_(False),
            )
            .order_by(Anomaly.detected_at)
        )
        # Exact environment scope: an environment-less incident reads only
        # environment-less anomalies (Phase 3 parity — `None` never means "all").
        if incident.environment_id is None:
            stmt = stmt.where(Anomaly.environment_id.is_(None))
        else:
            stmt = stmt.where(Anomaly.environment_id == incident.environment_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def _load_observation_times(
        self, project_id: uuid.UUID, anomalies: list[Anomaly]
    ) -> dict[uuid.UUID, datetime]:
        """Earliest stored observation per anomaly — the telemetry timeline.

        This is the timestamp the phase-4 ordering logic must use. Falling back
        to ``detected_at`` (a sweep-wide instant) would make every anomaly look
        simultaneous and silently destroy the temporal analysis.
        """
        if not anomalies:
            return {}
        by_id = {a.id: ensure_utc_or_now(a.detected_at) for a in anomalies}
        stmt = select(AnomalyObservation).where(
            AnomalyObservation.project_id == project_id,
            AnomalyObservation.anomaly_id.in_(list(by_id)),
        )
        for row in (await self._session.execute(stmt)).scalars().all():
            if row.observed_at is None:
                continue
            at = ensure_utc_or_now(row.observed_at)
            existing = by_id.get(row.anomaly_id)
            if existing is None or at < existing:
                by_id[row.anomaly_id] = at
        return by_id

    async def _load_components(
        self, project_id: uuid.UUID, component_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, SystemComponent]:
        if not component_ids:
            return {}
        stmt = select(SystemComponent).where(
            SystemComponent.project_id == project_id,
            SystemComponent.id.in_(component_ids),
        )
        return {
            row.id: row for row in (await self._session.execute(stmt)).scalars().all()
        }

    async def _incident_evidence_map(
        self, incident_id: uuid.UUID
    ) -> dict[str, uuid.UUID]:
        """Phase 3 evidence ``source_id`` → row id, for cross-linking (§23)."""
        stmt = select(IncidentEvidence).where(
            IncidentEvidence.incident_id == incident_id
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.source_id: row.id for row in rows if row.source_id}

    # ------------------------------------------------------------------ running
    async def _run(
        self,
        *,
        project_id: uuid.UUID,
        incident: Incident,
        anomalies: list[Anomaly],
        fingerprint: str,
        trigger: str,
        requested_by: Optional[str],
    ) -> CausalAnalysisOutcome:
        settings = self._settings
        # ``anomalies.detected_at`` is when the *sweep* noticed the anomaly, so
        # every anomaly in one sweep shares it. The moment a degradation
        # actually began is the telemetry timestamp Phase 3 stored with the
        # anomaly's observations — that, and only that, can order a timeline.
        observed_at = await self._load_observation_times(project_id, anomalies)
        detector_instant = ensure_utc_or_now(
            incident.detected_at or incident.started_at
        )
        onset = min([detector_instant, *observed_at.values()])
        last_anomaly_at = max(observed_at.values(), default=detector_instant)
        window_start = onset - timedelta(
            seconds=settings.CAUSAL_EVIDENCE_WINDOW_SECONDS
        )
        window_end = last_anomaly_at + timedelta(
            seconds=settings.CAUSAL_RECOVERY_WINDOW_SECONDS
        )
        if window_end < onset:
            window_end = onset + timedelta(
                seconds=settings.CAUSAL_RECOVERY_WINDOW_SECONDS
            )

        affected_component_ids = {
            a.component_id for a in anomalies if a.component_id is not None
        }
        components = await self._load_components(project_id, affected_component_ids)

        # -- structural context (bounded) ------------------------------------
        dependency_context = await DependencyAnalyzer(
            self._session, max_hops=settings.CAUSAL_MAX_DEPENDENCY_HOPS
        ).build_context(project_id, affected_component_ids, incident.environment_id)
        provider_ids = set()
        for component_id in affected_component_ids:
            provider_ids |= dependency_context.providers_of(component_id)
        neighbour_ids = set(dependency_context.component_ids)
        components = await self._load_components(
            project_id, affected_component_ids | provider_ids | neighbour_ids
        )

        # -- changes (§17, §18, §19) -----------------------------------------
        change_analyzer = ChangeAnalyzer(
            self._session,
            proximity_seconds=settings.CAUSAL_CHANGE_PROXIMITY_SECONDS,
            max_changes=settings.CAUSAL_MAX_EVIDENCE,
        )
        changes = await change_analyzer.load_changes(
            project_id,
            environment_id=incident.environment_id,
            start=window_start,
            end=window_end,
        )
        change_result = change_analyzer.assess(
            changes,
            onset=onset,
            affected_component_ids=affected_component_ids,
            provider_component_ids=provider_ids,
        )

        # -- traces (§15, §16) ------------------------------------------------
        trace_analyzer = TraceAnalyzer(
            self._session, max_traces=settings.CAUSAL_MAX_TRACES
        )
        trace_result = await trace_analyzer.analyze_window(
            project_id,
            environment_id=incident.environment_id,
            start=window_start,
            end=window_end,
            component_ids=dependency_context.component_ids or None,
        )

        # -- candidates (§22) --------------------------------------------------
        databases = {
            cid
            for cid, row in components.items()
            if row.component_type in _DATASTORE_CATEGORIES
        }
        externals = {
            cid
            for cid, row in components.items()
            if row.component_type in _EXTERNAL_CATEGORIES
        }
        anomaly_components = [
            (
                a.component_id,
                self._component_label(a.component_id, components)
                or (a.description or a.anomaly_type.value),
                a.anomaly_type,
                observed_at.get(a.id, detector_instant),
            )
            for a in anomalies
        ]
        seeds = self._generator.generate(
            project_id=project_id,
            onset=onset,
            anomaly_components=anomaly_components,
            change_result=change_result,
            trace_result=trace_result,
            external_component_ids=externals,
            database_component_ids=databases,
        )
        nodes = self._build_nodes(
            seeds=seeds,
            anomalies=anomalies,
            observation_times=observed_at,
            components=components,
            change_result=change_result,
            dependency_context=dependency_context,
            onset=onset,
        )

        graph = self._builder.build(
            nodes=nodes,
            trace_result=trace_result,
            dependency_context=dependency_context,
            change_result=change_result,
        )
        recovery_order = self._recovery_order(nodes, anomalies)
        scored = self._scorer.score(graph=graph, recovery_order=recovery_order)

        (
            primary,
            overall_confidence,
            summary,
            missing_evidence,
        ) = self._select_primary(scored, trace_result, change_result, graph)
        metadata = self._analysis_metadata(
            window_start=window_start,
            window_end=window_end,
            onset=onset,
            fingerprint=fingerprint,
            seeds=seeds,
            graph=graph,
            trace_result=trace_result,
            change_result=change_result,
            dependency_truncated=dependency_context.truncated,
            evidence_cap_hit=sum(len(n.evidence) for n in nodes.values())
            >= settings.CAUSAL_MAX_EVIDENCE,
        )

        return await self._persist(
            project_id=project_id,
            incident=incident,
            graph=graph,
            scored=scored,
            primary=primary,
            overall_confidence=overall_confidence,
            summary=summary,
            missing_evidence=missing_evidence,
            metadata=metadata,
            trigger=trigger,
            requested_by=requested_by,
        )

    # ------------------------------------------------------------------ nodes
    def _build_nodes(
        self,
        *,
        seeds: list[CandidateSeed],
        anomalies: list[Anomaly],
        observation_times: dict[uuid.UUID, datetime],
        components: dict[uuid.UUID, SystemComponent],
        change_result,
        dependency_context,
        onset: datetime,
    ) -> dict[tuple, HypothesisNode]:
        """Seeds → hypothesis nodes with their stored-fact evidence attached."""
        first_anomaly: dict[uuid.UUID, datetime] = {}
        anomalies_by_component: dict[uuid.UUID, list[Anomaly]] = {}
        for anomaly in anomalies:
            if anomaly.component_id is None:
                continue
            at = observation_times.get(
                anomaly.id, ensure_utc_or_now(anomaly.detected_at)
            )
            if (
                anomaly.component_id not in first_anomaly
                or at < first_anomaly[anomaly.component_id]
            ):
                first_anomaly[anomaly.component_id] = at
            anomalies_by_component.setdefault(anomaly.component_id, []).append(anomaly)

        change_at = {
            assessment.event.event_id: assessment.event.at
            for assessment in change_result.assessments
        }
        change_contradictions = {
            assessment.event.event_id: assessment.reason
            for assessment in change_result.contradictions
        }

        nodes: dict[tuple, HypothesisNode] = {}
        for seed in seeds:
            label = self._component_label(seed.component_id, components) or seed.label
            if seed.event_id is not None:
                first_observed = change_at.get(seed.event_id, onset)
            elif seed.component_id is not None:
                first_observed = first_anomaly.get(seed.component_id, onset)
            else:
                first_observed = onset

            node = HypothesisNode(
                key=seed.dedup_key,
                candidate_type=seed.candidate_type.value,
                component_id=seed.component_id,
                event_id=seed.event_id,
                label=label,
                explanation=seed.explanation,
                first_observed_at=first_observed,
                is_external=seed.is_external,
            )
            node.evidence = self._seed_evidence(
                seed=seed,
                anomalies_by_component=anomalies_by_component,
                observation_times=observation_times,
                contradiction_reason=change_contradictions.get(seed.event_id),
            )
            nodes[seed.dedup_key] = self._dedupe_evidence(node)

        self._attach_temporal_precedence(nodes)
        self._attach_contradictions(
            nodes=nodes,
            anomalies_by_component=anomalies_by_component,
            dependency_context=dependency_context,
        )
        return nodes

    def _attach_temporal_precedence(self, nodes: dict[tuple, HypothesisNode]) -> None:
        """Record which candidates degraded before which (§11).

        Precedence is *supporting context* — the engine says exactly that in
        the quote and never treats it as proof. It is still real evidence: a
        component that was still healthy when everything else had already
        degraded is a weaker explanation than one that moved first.
        """
        # Only *component* hypotheses participate: a change candidate also
        # carries the component it touched, but its timestamp is when the
        # change was published, not when anything degraded.
        component_nodes = {
            key: node
            for key, node in nodes.items()
            if node.component_id is not None
            and node.event_id is None
            and node.at is not None
        }
        for key, node in component_nodes.items():
            node_at = node.at
            if node_at is None:  # pragma: no cover - filtered above
                continue
            later = {
                other_key: other
                for other_key, other in component_nodes.items()
                if other_key != key and (other.at or node_at) > node_at
            }
            if not later:
                continue
            latest_at = max(
                other.at for other in later.values() if other.at is not None
            )
            node.evidence.append(
                EvidenceSpec(
                    category=CausalEvidenceCategory.TEMPORAL,
                    polarity=EvidencePolarity.SUPPORTING,
                    source_table="anomalies",
                    source_id=None,
                    quote=(
                        f"This component degraded first ({node_at.isoformat()}); "
                        f"{len(later)} other affected component(s) degraded up to "
                        f"{int((latest_at - node_at).total_seconds())}s later"
                    ),
                    explanation=(
                        "Temporal precedence among stored anomalies — supporting "
                        "context only; occurring before does not by itself establish "
                        "causing"
                    ),
                    component_id=node.component_id,
                    observed_at=node_at,
                    strength=min(0.9, 0.5 + 0.1 * len(later)),
                )
            )

    def _seed_evidence(
        self,
        *,
        seed: CandidateSeed,
        anomalies_by_component: dict[uuid.UUID, list[Anomaly]],
        observation_times: dict[uuid.UUID, datetime],
        contradiction_reason: Optional[str],
    ) -> list[EvidenceSpec]:
        """Each ``origin_ref`` becomes an evidence row pointing at stored data."""
        specs: list[EvidenceSpec] = []
        component_anomalies = (
            anomalies_by_component.get(seed.component_id, [])
            if seed.component_id is not None
            else []
        )
        for source_table, source_id, quote, observed_at in seed.origin_refs:
            if source_table == "anomalies":
                # One fact per stored anomaly: each carries its own type, so a
                # component showing a latency *and* a log spike contributes two
                # distinct evidence categories rather than one blurred quote.
                for anomaly in component_anomalies:
                    specs.append(
                        EvidenceSpec(
                            category=self._category_for_anomaly(anomaly),
                            polarity=EvidencePolarity.SUPPORTING,
                            source_table="anomalies",
                            source_id=anomaly.id,
                            quote=(
                                anomaly.description
                                or f"{anomaly.anomaly_type.value} observed on "
                                f"{seed.label}"
                            ),
                            explanation=seed.explanation,
                            component_id=seed.component_id,
                            observed_at=observation_times.get(
                                anomaly.id, ensure_utc_or_now(anomaly.detected_at)
                            ),
                            strength=_STRENGTH_BY_SOURCE.get(source_table, 0.4),
                        )
                    )
                continue
            specs.append(
                EvidenceSpec(
                    category=self._category_for(source_table, seed.candidate_type),
                    polarity=EvidencePolarity.SUPPORTING,
                    source_table=source_table,
                    source_id=source_id,
                    quote=quote,
                    explanation=seed.explanation,
                    component_id=seed.component_id,
                    observed_at=observed_at,
                    strength=_STRENGTH_BY_SOURCE.get(source_table, 0.4),
                )
            )
        if contradiction_reason:
            # A change that happened *after* onset cannot explain the onset —
            # recorded as contradicting evidence on its own candidate (§12).
            specs.append(
                EvidenceSpec(
                    category=CausalEvidenceCategory.CONTRADICTING,
                    polarity=EvidencePolarity.CONTRADICTING,
                    source_table="change_timing",
                    source_id=seed.event_id,
                    quote=contradiction_reason,
                    explanation=(
                        "The change occurred after the incident had already begun; "
                        "it cannot be the origin of the initial degradation"
                    ),
                    component_id=seed.component_id,
                    observed_at=None,
                    strength=1.0,
                )
            )
        return specs[: self._settings.CAUSAL_MAX_EVIDENCE]

    @staticmethod
    def _dedupe_evidence(node: HypothesisNode) -> HypothesisNode:
        """Collapse identical facts (same source row, same quote).

        A repeated observation is one fact, not many: counting it repeatedly
        would inflate a candidate's evidence mass and its apparent confidence.
        """
        seen: set[tuple] = set()
        kept: list[EvidenceSpec] = []
        for spec in node.evidence:
            key = (spec.source_table, spec.source_id, spec.quote)
            if key in seen:
                continue
            seen.add(key)
            kept.append(spec)
        node.evidence = kept
        return node

    @staticmethod
    def _first_anomaly_id(
        anomalies_by_component: dict[uuid.UUID, list[Anomaly]],
        component_id: uuid.UUID,
    ) -> Optional[uuid.UUID]:
        rows = anomalies_by_component.get(component_id)
        return rows[0].id if rows else None

    @staticmethod
    def _category_for_anomaly(anomaly: Anomaly) -> CausalEvidenceCategory:
        """An anomaly's own type decides its evidence category (§23)."""
        return _ANOMALY_TYPE_CATEGORY.get(
            anomaly.anomaly_type, CausalEvidenceCategory.METRIC
        )

    def _category_for(
        self,
        source_table: str,
        candidate_type: CandidateType,
    ) -> CausalEvidenceCategory:
        if source_table in ("span_records", "traces", "trace_records"):
            return CausalEvidenceCategory.TRACE
        if source_table == "anomalies":
            return CausalEvidenceCategory.METRIC
        if source_table in ("deployment_events", "deployments"):
            return CausalEvidenceCategory.DEPLOYMENT
        if source_table in ("configuration_change_events", "configuration_changes"):
            return CausalEvidenceCategory.CONFIGURATION
        if source_table in ("component_dependencies", "graph_edges"):
            return CausalEvidenceCategory.DEPENDENCY
        if source_table == "change_timing":
            return CausalEvidenceCategory.CONTRADICTING
        return CausalEvidenceCategory.METRIC

    def _attach_contradictions(
        self,
        *,
        nodes: dict[tuple, HypothesisNode],
        anomalies_by_component: dict[uuid.UUID, list[Anomaly]],
        dependency_context,
    ) -> None:
        """Record effect-before-cause contradictions per candidate (§24).

        When a caller was already failing *before* the component it depends on,
        the dependency direction is contradicted by the timing — the provider's
        own candidate must carry that fact rather than quietly being ranked
        first.
        """
        for key, node in nodes.items():
            if node.component_id is None or node.at is None:
                continue
            for provider_id in dependency_context.providers_of(node.component_id):
                provider_node = next(
                    (n for n in nodes.values() if n.component_id == provider_id), None
                )
                if provider_node is None or provider_node.at is None:
                    continue
                if node.at < provider_node.at:
                    delta = int((provider_node.at - node.at).total_seconds())
                    provider_node.evidence.append(
                        EvidenceSpec(
                            category=CausalEvidenceCategory.CONTRADICTING,
                            polarity=EvidencePolarity.CONTRADICTING,
                            source_table="anomalies",
                            source_id=self._first_anomaly_id(
                                anomalies_by_component, provider_id
                            ),
                            quote=(
                                f"{node.label} was already degraded {delta}s before "
                                f"{provider_node.label} — the dependent failed first"
                            ),
                            explanation=(
                                "A dependency cannot have caused a failure that "
                                "preceded its own degradation; this weakens the "
                                "provider hypothesis for the incident onset"
                            ),
                            component_id=provider_id,
                            observed_at=node.at,
                            strength=0.8,
                        )
                    )

    @staticmethod
    def _recovery_order(
        nodes: dict[tuple, HypothesisNode], anomalies: list[Anomaly]
    ) -> list[tuple[tuple, datetime]]:
        """(node_key, recovered_at) for components that recovered (§31)."""
        recovered: dict[uuid.UUID, datetime] = {}
        for anomaly in anomalies:
            if anomaly.component_id is None or anomaly.resolved_at is None:
                continue
            at = ensure_utc_or_now(anomaly.resolved_at)
            if (
                anomaly.component_id not in recovered
                or at < recovered[anomaly.component_id]
            ):
                recovered[anomaly.component_id] = at
        pairs = [
            (key, recovered[node.component_id])
            for key, node in nodes.items()
            if node.component_id in recovered
        ]
        pairs.sort(key=lambda pair: (pair[1], str(pair[0])))
        return pairs

    # ------------------------------------------------------------------ selection
    def _select_primary(
        self,
        scored: list[ScoredCandidate],
        trace_result,
        change_result,
        graph,
    ) -> tuple[Optional[ScoredCandidate], ConfidenceLevel, str, list[str]]:
        """Choose the primary hypothesis — or honestly decline to (§29, §30)."""
        missing: list[str] = []
        if not trace_result.has_directional_evidence:
            missing.append("span-level traces showing the failing call path")
            if trace_result.traces_without_spans:
                missing.append(
                    f"spans for {trace_result.traces_without_spans} failing trace(s) "
                    "in the window"
                )
        if not change_result.temporally_relevant:
            missing.append("a deployment or configuration change before onset")
        if change_result.skipped_missing_timestamp:
            missing.append(
                f"timestamps for {change_result.skipped_missing_timestamp} change event(s)"
            )
        if not graph.edges:
            missing.append("any evidence-backed relationship between candidates")

        if not scored:
            return (
                None,
                ConfidenceLevel.INSUFFICIENT,
                # Same lead as the below-threshold case (§50): an engineer and a
                # client both need one consistent way to read "no answer from
                # ARGUS" rather than two differently-worded refusals.
                "Insufficient evidence to determine a root cause. No "
                "evidence-anchored candidate could be generated: no anomalies, "
                "changes, or failing traces were available inside the evidence "
                "window.",
                missing,
            )

        top = scored[0]
        threshold = self._settings.CAUSAL_PRIMARY_MIN_SCORE
        if top.confidence is ConfidenceLevel.INSUFFICIENT or top.score < threshold:
            summary = (
                "Insufficient evidence to determine a root cause. "
                f"The best-supported candidate ({top.label}) reached only "
                f"score {top.score:.2f} with {top.confidence.value} confidence. "
                "Available telemetry establishes correlated observations, but not "
                "enough directional evidence to decide which component initiated "
                "the failure."
            )
            return None, ConfidenceLevel.INSUFFICIENT, summary, missing

        limitation = (
            "ARGUS reconstructs causality from stored telemetry, traces and the "
            "dependency graph; it is evidence-supported, not mathematically proven."
        )
        summary = (
            f"Most-supported explanation: {top.label} — {top.confidence_reason}. "
            f"Score {top.score:.2f} from {top.supporting_count} supporting fact(s) "
            f"across {len(top.supporting_categories)} evidence categor"
            f"{'y' if len(top.supporting_categories) == 1 else 'ies'}"
            + (
                f", with {top.contradicting_count} contradicting fact(s)"
                if top.contradicting_count
                else "; no contradicting evidence was observed"
            )
            + f". {limitation}"
        )
        missing.extend(top.uncertainty.get("missing", []))
        return top, top.confidence, summary, missing

    # ------------------------------------------------------------------ metadata
    def _analysis_metadata(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        onset: datetime,
        fingerprint: str,
        seeds: list[CandidateSeed],
        graph,
        trace_result,
        change_result,
        dependency_truncated: bool,
        evidence_cap_hit: bool,
    ) -> dict:
        """Reproducibility record: the exact bounded inputs used (§44, §46)."""
        return {
            "input_fingerprint": fingerprint,
            "engine_version": ENGINE_VERSION,
            "onset": onset.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "window_seconds": self._settings.CAUSAL_EVIDENCE_WINDOW_SECONDS,
            "candidates_generated": len(seeds),
            "candidate_budget": self._settings.CAUSAL_MAX_CANDIDATES,
            "edges_built": len(graph.edges),
            "rejected_pairs": [
                {"source": src, "target": tgt, "reason": why}
                for src, tgt, why in graph.rejected_pairs
            ],
            "trace_edges": len(trace_result.edges),
            "propagations": len(trace_result.propagations),
            "traces_without_spans": trace_result.traces_without_spans,
            "changes_assessed": len(change_result.assessments),
            "changes_skipped_missing_timestamp": (
                change_result.skipped_missing_timestamp
            ),
            "dependency_context_truncated": dependency_truncated,
            "evidence_cap_hit": evidence_cap_hit,
            "max_traces": self._settings.CAUSAL_MAX_TRACES,
            "max_dependency_hops": self._settings.CAUSAL_MAX_DEPENDENCY_HOPS,
            "change_proximity_seconds": self._settings.CAUSAL_CHANGE_PROXIMITY_SECONDS,
        }

    def _input_fingerprint(self, incident: Incident, anomalies: list[Anomaly]) -> str:
        """Deterministic hash of the evidence set (drives idempotency §34)."""
        parts = [
            str(incident.id),
            ensure_utc_or_now(incident.detected_at or incident.started_at).isoformat(),
            str(incident.environment_id),
            ENGINE_VERSION,
        ]
        parts.extend(
            f"{a.id}:{a.fingerprint}:{a.observation_count}:"
            f"{ensure_utc_or_now(a.detected_at).isoformat()}:"
            f"{ensure_utc_or_now(a.resolved_at).isoformat() if a.resolved_at else '-'}"
            for a in sorted(anomalies, key=lambda row: str(row.id))
        )
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    # ------------------------------------------------------------------ persistence
    async def _persist(
        self,
        *,
        project_id: uuid.UUID,
        incident: Incident,
        graph,
        scored: list[ScoredCandidate],
        primary: Optional[ScoredCandidate],
        overall_confidence: ConfidenceLevel,
        summary: str,
        missing_evidence: list[str],
        metadata: dict,
        trigger: str,
        requested_by: Optional[str],
    ) -> CausalAnalysisOutcome:
        now = utcnow()
        prior = await self._session.scalar(
            select(func.max(CausalAnalysis.analysis_version)).where(
                CausalAnalysis.incident_id == incident.id,
                CausalAnalysis.project_id == project_id,
            )
        )
        analysis = CausalAnalysis(
            project_id=project_id,
            environment_id=incident.environment_id,
            incident_id=incident.id,
            analysis_version=int(prior or 0) + 1,
            status=AnalysisStatus.RUNNING,
            trigger=trigger,
            requested_by=requested_by,
            analysis_version_tag=ENGINE_VERSION,
            started_at=now,
            overall_confidence=overall_confidence,
            summary=summary,
            missing_evidence=missing_evidence or None,
            analysis_metadata=metadata,
        )
        self._session.add(analysis)
        await self._session.flush()

        # -- candidates + their evidence --------------------------------------
        rows_by_key: dict[tuple, RootCauseCandidate] = {}
        scored_by_key = {candidate.key: candidate for candidate in scored}
        for key, node in graph.nodes.items():
            scored_node = scored_by_key.get(key)
            if scored_node is None:
                continue
            # Counts are taken from the node's own evidence rather than copied
            # from the scorer: the scorer ran on these same specs, so the two
            # are equal, but deriving them here makes it structurally impossible
            # for a candidate to report fewer facts than it stores (§24).
            supporting_total = sum(
                1
                for spec in node.evidence
                if spec.polarity is EvidencePolarity.SUPPORTING
            )
            contradicting_total = sum(
                1
                for spec in node.evidence
                if spec.polarity is EvidencePolarity.CONTRADICTING
            )
            neutral_total = sum(
                1 for spec in node.evidence if spec.polarity is EvidencePolarity.NEUTRAL
            )
            row = RootCauseCandidate(
                analysis_id=analysis.id,
                project_id=project_id,
                component_id=node.component_id,
                event_id=node.event_id,
                candidate_type=self._candidate_type(node.candidate_type),
                status=self._candidate_status(scored_node),
                score=round(scored_node.score, 6),
                confidence=scored_node.confidence,
                is_external=node.is_external,
                first_observed_at=node.first_observed_at,
                supporting_evidence_count=supporting_total,
                contradicting_evidence_count=contradicting_total,
                neutral_evidence_count=neutral_total,
                explanation=node.explanation or None,
                score_breakdown=scored_node.breakdown.as_dict(),
                reasons=scored_node.reasons[:50],
                uncertainty=scored_node.uncertainty,
            )
            self._session.add(row)
            rows_by_key[key] = row
        await self._session.flush()

        for key, node in graph.nodes.items():
            candidate_row = rows_by_key.get(key)
            if candidate_row is None:
                continue
            for spec in node.evidence:
                self._session.add(
                    CausalEvidence(
                        analysis_id=analysis.id,
                        candidate_id=candidate_row.id,
                        project_id=project_id,
                        category=spec.category,
                        polarity=spec.polarity,
                        source_table=spec.source_table,
                        source_id=spec.source_id,
                        quote=spec.quote[:2000],
                        explanation=spec.explanation[:2000],
                        component_id=spec.component_id,
                        observed_at=spec.observed_at,
                        strength=round(spec.strength, 4),
                    )
                )

        # -- relationships (edges), with their own evidence --------------------
        for edge in graph.edges:
            source_row = rows_by_key.get(edge.source_key)
            target_row = rows_by_key.get(edge.target_key)
            if source_row is None or target_row is None:
                continue
            supporting = [
                spec
                for spec in edge.evidence
                if spec.polarity is EvidencePolarity.SUPPORTING
            ]
            relationship = CausalRelationship(
                analysis_id=analysis.id,
                project_id=project_id,
                source_candidate_id=source_row.id,
                target_candidate_id=target_row.id,
                relationship_type=edge.relationship_type,
                confidence=self._edge_confidence(edge),
                supporting_evidence_count=len(supporting),
                contradicting_evidence_count=len([n for n in edge.contradiction_notes]),
                temporal_alignment_seconds=edge.temporal_alignment_seconds,
                structural_support=edge.structural_support,
                observational_support=edge.observational_support,
                contradiction_notes=edge.contradiction_notes or None,
                explanation=edge.explanation,
            )
            self._session.add(relationship)
            await self._session.flush()

            for spec in supporting:
                # ``candidate_id`` is set because the schema requires a candidate
                # owner, but the row is edge-scoped (``relationship_id``), so
                # candidate-level reads skip it — otherwise the target would
                # appear to hold evidence its confidence was never computed on.
                self._session.add(
                    CausalEvidence(
                        analysis_id=analysis.id,
                        candidate_id=target_row.id,
                        project_id=project_id,
                        category=spec.category,
                        polarity=spec.polarity,
                        source_table=spec.source_table,
                        source_id=spec.source_id,
                        relationship_id=relationship.id,
                        quote=spec.quote[:2000],
                        explanation=spec.explanation[:2000],
                        component_id=spec.component_id,
                        observed_at=spec.observed_at,
                        strength=round(spec.strength, 4),
                    )
                )

        # -- primary candidate + completion ------------------------------------
        if primary is not None:
            primary_row = rows_by_key.get(primary.key)
            if primary_row is not None:
                primary_row.status = CandidateStatus.SUPPORTED
                analysis.primary_candidate_id = primary_row.id

        analysis.status = AnalysisStatus.COMPLETED
        analysis.completed_at = utcnow()
        await self._session.flush()
        return CausalAnalysisOutcome(analysis=analysis, created=True)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _candidate_type(value: str) -> CandidateType:
        try:
            return CandidateType(value)
        except ValueError:  # pragma: no cover - defensive, enum drift only
            return CandidateType.UNKNOWN

    @staticmethod
    def _candidate_status(scored: ScoredCandidate) -> CandidateStatus:
        """Candidate lifecycle label derived from its own evidence (§7)."""
        if scored.contradicting_count and scored.confidence is ConfidenceLevel.LOW:
            return CandidateStatus.WEAKENED
        if scored.confidence in (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM):
            return CandidateStatus.SUPPORTED
        if scored.confidence is ConfidenceLevel.INSUFFICIENT:
            return CandidateStatus.WITHDRAWN
        return CandidateStatus.UNDER_EVALUATION

    @staticmethod
    def _edge_confidence(edge) -> ConfidenceLevel:
        supporting = len(
            [s for s in edge.evidence if s.polarity is EvidencePolarity.SUPPORTING]
        )
        if edge.observational_support and supporting >= 2:
            return ConfidenceLevel.HIGH
        if supporting >= 2 or edge.structural_support == 2:
            return ConfidenceLevel.MEDIUM
        return ConfidenceLevel.LOW

    @staticmethod
    def _component_label(
        component_id: Optional[uuid.UUID],
        components: dict[uuid.UUID, SystemComponent],
    ) -> str:
        if component_id is None:
            return ""
        row = components.get(component_id)
        if row is None:
            return ""
        return row.name


__all__ = ["CausalAnalysisService", "CausalAnalysisOutcome", "ENGINE_VERSION"]
