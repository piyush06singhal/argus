"""ARGUS Reliability Intelligence Models (Phase 10 §2, §6, §26, §27, §69).

The learning domain. Every table here is derived data: it records what ARGUS
concluded *from* Phase 0–9 rows, never instead of them. Deleting an incident
cascades the experiences that reference it, so the learning tables can never
become a shadow copy of history the platform has itself forgotten.

Three invariants shape the whole domain:

* **Provenance or nothing.** A knowledge row without sources is a claim without
  evidence; the models make the empty case impossible.
* **Candidate until validated.** Only ``VALIDATED``/``ACTIVE`` knowledge may
  influence a recommendation, and the lifecycle is a state machine in
  :mod:`app.services.intelligence_state`, not a free-text column.
* **Sample size is a column, not a footnote.** Every pattern carries
  ``sample_count`` and its coverage window, because "it worked once" and "it
  worked in 34 of 42 comparable cases" must not be expressible by the same row
  shape.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel, JSONType, Guid


class KnowledgeType(str, enum.Enum):
    """What kind of reliability knowledge this is (§3)."""

    INCIDENT_PATTERN = "INCIDENT_PATTERN"
    FAILURE_PATTERN = "FAILURE_PATTERN"
    ANOMALY_PATTERN = "ANOMALY_PATTERN"
    REMEDIATION_PATTERN = "REMEDIATION_PATTERN"
    REGRESSION_PATTERN = "REGRESSION_PATTERN"
    DEPENDENCY_PATTERN = "DEPENDENCY_PATTERN"
    DEPLOYMENT_PATTERN = "DEPLOYMENT_PATTERN"
    RESOURCE_PATTERN = "RESOURCE_PATTERN"
    PREDICTIVE_PATTERN = "PREDICTIVE_PATTERN"
    RECOVERY_PATTERN = "RECOVERY_PATTERN"
    COMPONENT_RELIABILITY_PATTERN = "COMPONENT_RELIABILITY_PATTERN"


class KnowledgeStatus(str, enum.Enum):
    """Knowledge lifecycle (§4). Only VALIDATED/ACTIVE can be recommended."""

    CANDIDATE = "CANDIDATE"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class KnowledgeScope(str, enum.Enum):
    """How far the knowledge claims to generalise (§37)."""

    COMPONENT_SPECIFIC = "COMPONENT_SPECIFIC"
    SERVICE_CLASS = "SERVICE_CLASS"
    PROJECT_LEVEL = "PROJECT_LEVEL"
    CROSS_PROJECT = "CROSS_PROJECT"


class KnowledgeConfidence(str, enum.Enum):
    """Bucketed confidence (§34). Weak evidence yields LOW or UNKNOWN, not a
    decimal that implies more precision than the sample supports."""

    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class DataProvenance(str, enum.Enum):
    """Where a record came from — the data-poisoning defence (§76)."""

    OBSERVABILITY = "OBSERVABILITY"
    SYSTEM_GENERATED = "SYSTEM_GENERATED"
    HUMAN_ENTERED = "HUMAN_ENTERED"
    AI_GENERATED = "AI_GENERATED"
    IMPORTED = "IMPORTED"
    MOCK = "MOCK"


class LearningEventType(str, enum.Enum):
    """Completed outcomes that are worth learning from (§6)."""

    INCIDENT_RESOLVED = "INCIDENT_RESOLVED"
    REMEDIATION_COMPLETED = "REMEDIATION_COMPLETED"
    PATCH_VERIFIED = "PATCH_VERIFIED"
    PATCH_REGRESSION_DETECTED = "PATCH_REGRESSION_DETECTED"
    FORECAST_CONFIRMED = "FORECAST_CONFIRMED"
    FORECAST_FALSE_POSITIVE = "FORECAST_FALSE_POSITIVE"
    FORECAST_MISSED = "FORECAST_MISSED"
    ROOT_CAUSE_CONFIRMED = "ROOT_CAUSE_CONFIRMED"
    ROOT_CAUSE_REJECTED = "ROOT_CAUSE_REJECTED"
    REPRODUCTION_CONFIRMED = "REPRODUCTION_CONFIRMED"
    REPRODUCTION_FAILED = "REPRODUCTION_FAILED"
    ROLLBACK_COMPLETED = "ROLLBACK_COMPLETED"


class RecommendationType(str, enum.Enum):
    """Recommendations are advisory (§39). None of these executes anything."""

    INVESTIGATE_COMPONENT = "INVESTIGATE_COMPONENT"
    INVESTIGATE_DEPENDENCY = "INVESTIGATE_DEPENDENCY"
    REVIEW_RECENT_CHANGE = "REVIEW_RECENT_CHANGE"
    REVIEW_REMEDIATION = "REVIEW_REMEDIATION"
    RUN_REPRODUCTION = "RUN_REPRODUCTION"
    CONSIDER_ROLLBACK = "CONSIDER_ROLLBACK"
    CONSIDER_RESTART = "CONSIDER_RESTART"
    CONSIDER_TRAFFIC_SHIFT = "CONSIDER_TRAFFIC_SHIFT"
    REVIEW_CAPACITY = "REVIEW_CAPACITY"
    REVIEW_CONFIGURATION = "REVIEW_CONFIGURATION"


class RecommendationStatus(str, enum.Enum):
    """What happened to a recommendation (§81). Acceptance is not correctness,
    so the outcome is tracked separately from the decision."""

    OPEN = "OPEN"
    ACCEPTED = "ACCEPTED"
    DISMISSED = "DISMISSED"
    EXPIRED = "EXPIRED"
    EFFECTIVE = "EFFECTIVE"
    INEFFECTIVE = "INEFFECTIVE"
    REGRESSION_CAUSING = "REGRESSION_CAUSING"


class LearningRunStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class LearningExperimentStatus(str, enum.Enum):
    """Learning-experiment lifecycle (§69). Nothing activates automatically."""

    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class RelationshipKind(str, enum.Enum):
    """Historical relationships learned from outcomes (§23).

    These are deliberately *not* :class:`~app.models.graph.GraphEdgeType`
    members. ``DEPENDS_ON``/``CALLS`` say how a system is wired; these say what
    history has observed travelling between two components. Conflating them
    would let an observed co-failure masquerade as a declared dependency (§24).
    """

    #: Failures observed at ``source`` coincided with failures at ``target``,
    #: where ``source`` was the episode's more upstream component.
    FAILURE_PROPAGATION = "FAILURE_PROPAGATION"
    #: Failures at two components coincided with no direction established. This
    #: kind is *always* undirected (§24): co-occurrence is not a claim about
    #: which end caused which.
    SHARED_FAILURE = "SHARED_FAILURE"
    #: ``target`` degraded while ``source`` — a declared dependency of it — was
    #: itself unhealthy. Direction follows the dependency, not the symptom.
    DEPENDENCY_DEGRADATION = "DEPENDENCY_DEGRADATION"
    #: Remediating ``source`` was followed by ``target`` recovering in the same
    #: episode. Association with an observed outcome, never proof of mechanism.
    REMEDIATION_INFLUENCE = "REMEDIATION_INFLUENCE"


class RelationshipStatus(str, enum.Enum):
    """Whether history still supports this relationship (§25, §42).

    Relationships are never deleted by decay: a stale edge is evidence that the
    topology *changed*, which is exactly the thing worth keeping.
    """

    ACTIVE = "ACTIVE"
    STALE = "STALE"
    SUPERSEDED = "SUPERSEDED"


#: Refuse to publish a learned relationship below this many episodes. One
#: co-occurrence is an anecdote; the phase is explicit that small samples must
#: not read as established knowledge.
RELATIONSHIP_MIN_SAMPLES = 2


def knowledge_fingerprint(
    *,
    knowledge_type: "KnowledgeType",
    scope: "KnowledgeScope",
    project_id: Optional[uuid.UUID],
    component_id: Optional[uuid.UUID],
    feature_signature: str,
) -> str:
    """Deterministic identity of a pattern (§65).

    The same pattern discovered twice — by two runs, or incrementally — must
    update one row, not create two. Scope is part of the identity deliberately:
    "restart works for checkout" and "restart works project-wide" are different
    knowledge items, which is what makes conflicting patterns representable
    (§66) instead of silently merged.
    """
    import hashlib

    payload = "|".join(
        [
            knowledge_type.value,
            scope.value,
            str(project_id) if project_id else "-",
            str(component_id) if component_id else "-",
            feature_signature,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ReliabilityExperience(BaseModel):
    """One completed reliability episode (§8) — the unit of history.

    A retrospective assembled from rows that already exist: the incident, the
    signals observed before it, the causal analysis, the reproduction, the fix,
    the remediation and its outcome. Nothing here is new evidence; it is a
    normalised shape that the learning pipeline can reason about without
    re-joining eleven tables every time.
    """

    __tablename__ = "reliability_experiences"
    __table_args__ = (
        Index(
            "ix_reliability_experiences_project_time",
            "project_id",
            "start_time",
        ),
        Index(
            "ix_reliability_experiences_signature",
            "project_id",
            "failure_fingerprint",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: These references are ``DEFERRABLE INITIALLY DEFERRED`` so that a project
    #: delete can clear them after the cascades on the same row have run: see
    #: revision ``d3e4f5a6b7c8``. The check is moved, not weakened — a dangling
    #: reference still fails the transaction.
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey(
            "environments.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
        index=True,
    )
    incident_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey(
            "incidents.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
        index=True,
    )
    primary_component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey(
            "system_components.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
        index=True,
    )

    #: The Phase 9 remediation that resolved (or attempted to resolve) it, when
    #: there was one. Experiences without remediations are still experiences:
    #: an incident that recovered on its own is history too.
    remediation_action_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey(
            "remediation_actions.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
        index=True,
    )

    #: Phase 4 / Phase 5 / Phase 7 references, kept nullable: every phase of
    #: the pipeline is optional in practice and the experience records what
    #: actually happened, not an idealised run.
    causal_analysis_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), nullable=True, index=True
    )
    root_cause_candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), nullable=True, index=True
    )
    reproduction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), nullable=True, index=True
    )
    patch_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), nullable=True, index=True
    )

    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recovery_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    #: Structured signatures (§9, §10) — normalised features, never raw
    #: telemetry. ``failure_fingerprint`` is the hashed form used for grouping
    #: and deduplication; ``failure_signature`` keeps the human-readable parts.
    failure_signature: Mapped[dict] = mapped_column(JSONType, nullable=False)
    failure_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resolution_signature: Mapped[Optional[dict]] = mapped_column(
        JSONType, nullable=True
    )

    #: Outcome of the episode, from the outcome enums of the phases that
    #: produced it (RemediationOutcome when remediated, otherwise recovery).
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    #: Where the assembled data came from (§76).
    provenance: Mapped[DataProvenance] = mapped_column(
        SAEnum(DataProvenance, name="intelligence_provenance"),
        default=DataProvenance.OBSERVABILITY,
        nullable=False,
        index=True,
    )
    #: Quality verdict from the pre-learning gate (§30): POOR rows are excluded
    #: from pattern mining, not silently learned from.
    data_quality: Mapped[str] = mapped_column(String(20), nullable=False, default="OK")

    #: Set of component ids the episode touched, for retrieval by component
    #: beyond the primary one. Kept as JSON (not an association table) because
    #: it is always read whole with the experience.
    component_ids: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)

    #: The learning run that created this row, for auditability (§79).
    learning_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), nullable=True, index=True
    )
    #: Set when a later run revisits an episode whose outcome changed.
    supersedes_experience_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), nullable=True
    )


class LearningEvent(BaseModel):
    """An append-only record of a completed outcome (§6, §63).

    The inbox of learning: pipeline phases publish here when something
    *finishes*, and the learning run consumes events idempotently
    (``dedup_key``). An event that names a missing row is recorded anyway —
    the run marks it unprocessable rather than losing the fact that something
    happened.
    """

    __tablename__ = "learning_events"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_learning_events_dedup_key"),
        Index("ix_learning_events_project_created", "project_id", "created_at"),
        Index("ix_learning_events_unprocessed", "processed_at", "project_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[LearningEventType] = mapped_column(
        SAEnum(LearningEventType, name="intelligence_learning_event_type"),
        nullable=False,
        index=True,
    )
    #: The row this event is about — e.g. the incident id for
    #: INCIDENT_RESOLVED, the action id for REMEDIATION_COMPLETED.
    subject_id: Mapped[uuid.UUID] = mapped_column(Guid(), nullable=False, index=True)
    #: Deterministic identity of the outcome itself: the same incident
    #: resolving twice publishes the same key and the pipeline treats it as one
    #: logical event (§64).
    dedup_key: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    provenance: Mapped[DataProvenance] = mapped_column(
        SAEnum(DataProvenance, name="intelligence_provenance"),
        default=DataProvenance.OBSERVABILITY,
        nullable=False,
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    processed_by_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), nullable=True
    )
    #: Why an event could not be turned into an experience, when so.
    unprocessable_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ReliabilityKnowledge(BaseModel):
    """A learned, scoped, versioned piece of reliability knowledge (§2).

    The pattern is the *row*; the evidence behind it lives in ``sources`` and
    ``experience_ids``. Status follows the §4 lifecycle and is only moved
    through :mod:`app.services.intelligence_state`.
    """

    __tablename__ = "reliability_knowledge"
    __table_args__ = (
        #: One live pattern per fingerprint; superseded/deprecated rows keep
        #: their history under a new fingerprint suffix.
        Index(
            "ix_reliability_knowledge_project_status",
            "project_id",
            "status",
        ),
        Index(
            "ix_reliability_knowledge_project_type",
            "project_id",
            "knowledge_type",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    knowledge_type: Mapped[KnowledgeType] = mapped_column(
        SAEnum(KnowledgeType, name="intelligence_knowledge_type"),
        nullable=False,
        index=True,
    )
    status: Mapped[KnowledgeStatus] = mapped_column(
        SAEnum(KnowledgeStatus, name="intelligence_knowledge_status"),
        default=KnowledgeStatus.CANDIDATE,
        nullable=False,
        index=True,
    )
    scope: Mapped[KnowledgeScope] = mapped_column(
        SAEnum(KnowledgeScope, name="intelligence_knowledge_scope"),
        default=KnowledgeScope.COMPONENT_SPECIFIC,
        nullable=False,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey(
            "environments.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey(
            "system_components.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    #: §65. See :func:`knowledge_fingerprint`.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Human-readable feature signature (e.g. "dependency_latency+rising_p95"),
    #: the pre-hash form of the fingerprint and the thing a person reads.
    feature_signature: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Provenance (§5): typed references to the rows that produced this
    #: knowledge. Never empty — a pattern with no sources is a guess.
    sources: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    experience_ids: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)

    #: §7, §34. Sample facts are first-class columns.
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    coverage_start: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    coverage_end: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confidence: Mapped[KnowledgeConfidence] = mapped_column(
        SAEnum(KnowledgeConfidence, name="intelligence_confidence"),
        default=KnowledgeConfidence.UNKNOWN,
        nullable=False,
    )
    #: Outcome ratio where the pattern has a success/failure shape, else None.
    #: Stored for sorting, never shown without its sample count.
    support_strength: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    #: Which algorithm and feature schema produced this (§26, §68).
    algorithm: Mapped[str] = mapped_column(String(80), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(40), nullable=False)
    feature_schema_version: Mapped[str] = mapped_column(String(40), nullable=False)

    #: Validation results (§32): stability across windows, cross-environment
    #: behaviour, false-discovery assessment — recorded, not assumed.
    validation: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    #: What this knowledge does *not* claim (§34, §13 of the phase).
    limitations: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)

    #: §26/§25. Current version counter and the row it supersedes.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supersedes_knowledge_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), nullable=True
    )
    #: §72. Who reviewed this, when, and why — for the states that require it.
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    review_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    #: §25. When the pattern was last confirmed by data; decay compares now
    #: against this, and the sweeper marks stale rows rather than deleting them.
    last_confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class LearnedRelationship(BaseModel):
    """A component-to-component relationship learned from history (§23, §24).

    Stored apart from ``graph_edges`` on purpose. A graph edge states a fact
    about the system ("A calls B"); this row states a fact about *history*
    ("failures at A coincided with failures at B in 7 of 7 comparable
    episodes"). A reader that renders the second as the first would turn an
    observation into an architectural claim, so this table carries its own
    sample count, its own provenance and its own ``limitations``.
    """

    __tablename__ = "intelligence_relationships"
    __table_args__ = (
        #: One row per (project, environment, source, target, kind). The
        #: COALESCE sentinel collapses NULL environment the same way
        #: ``graph_edges`` does, so an unspecified environment cannot produce
        #: duplicate edges.
        Index(
            "uq_intelligence_relationships_key",
            "project_id",
            "source_component_id",
            "target_component_id",
            "kind",
            text("COALESCE(environment_id, '00000000-0000-0000-0000-000000000000')"),
            unique=True,
        ),
        Index(
            "ix_intelligence_relationships_source",
            "project_id",
            "source_component_id",
            "kind",
        ),
        Index(
            "ix_intelligence_relationships_target",
            "project_id",
            "target_component_id",
            "kind",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey(
            "environments.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
        index=True,
    )
    source_component_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("system_components.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_component_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("system_components.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[RelationshipKind] = mapped_column(
        SAEnum(RelationshipKind, name="intelligence_relationship_kind"),
        nullable=False,
        index=True,
    )
    #: False for :attr:`RelationshipKind.SHARED_FAILURE`, which asserts no
    #: direction at all. Kept as a column rather than inferred by the client so
    #: an undirected observation cannot be drawn with an arrow by accident.
    directed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    status: Mapped[RelationshipStatus] = mapped_column(
        SAEnum(RelationshipStatus, name="intelligence_relationship_status"),
        default=RelationshipStatus.ACTIVE,
        nullable=False,
        index=True,
    )

    #: §34. Episodes supporting the relationship, and — where the kind has an
    #: outcome shape — how many of them showed the relationship "working".
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    supporting_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    confidence: Mapped[KnowledgeConfidence] = mapped_column(
        SAEnum(KnowledgeConfidence, name="intelligence_confidence"),
        default=KnowledgeConfidence.UNKNOWN,
        nullable=False,
    )

    #: §5/§76. Typed citations: experience/incident ids and the knowledge rows
    #: whose mining surfaced this edge. Bounded — the point is inspectability.
    evidence: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    #: §24. Printed next to the edge wherever it is shown.
    limitations: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)

    #: §26/§68 provenance, identical to knowledge rows so an operator can ask
    #: which algorithm produced an edge and invalidate its output by version.
    provenance: Mapped[DataProvenance] = mapped_column(
        SAEnum(DataProvenance, name="intelligence_provenance"),
        default=DataProvenance.OBSERVABILITY,
        nullable=False,
        index=True,
    )
    algorithm: Mapped[str] = mapped_column(String(80), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(40), nullable=False)
    feature_schema_version: Mapped[str] = mapped_column(String(40), nullable=False)

    coverage_start: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    coverage_end: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    #: §79. The run that last refreshed this edge.
    learning_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), nullable=True, index=True
    )


class KnowledgeVersion(BaseModel):
    """Immutable snapshot of one knowledge revision (§26).

    ``ReliabilityKnowledge`` is the *current* view; this table is the ledger.
    Activating, updating or deprecating writes a version row first, so the
    history of what ARGUS believed and when is never rewritten.
    """

    __tablename__ = "intelligence_knowledge_versions"
    __table_args__ = (
        Index("ix_knowledge_versions_knowledge", "knowledge_id", "version"),
    )

    knowledge_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("reliability_knowledge.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[KnowledgeStatus] = mapped_column(
        SAEnum(KnowledgeStatus, name="intelligence_knowledge_status"),
        nullable=False,
    )
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[KnowledgeConfidence] = mapped_column(
        SAEnum(KnowledgeConfidence, name="intelligence_confidence"), nullable=False
    )
    snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False)
    #: The run that produced this version, for the §79 audit chain.
    learning_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(Guid(), nullable=True)
    activated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deactivated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class LearningRun(BaseModel):
    """One execution of the learning pipeline (§27)."""

    __tablename__ = "intelligence_learning_runs"
    __table_args__ = (
        Index("ix_learning_runs_project_started", "project_id", "started_at"),
    )

    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    status: Mapped[LearningRunStatus] = mapped_column(
        SAEnum(LearningRunStatus, name="intelligence_run_status"),
        default=LearningRunStatus.QUEUED,
        nullable=False,
        index=True,
    )
    trigger: Mapped[str] = mapped_column(String(30), nullable=False, default="manual")
    #: Incremental checkpoint (§28): events at or before this timestamp were
    #: consumed by previous runs; this run must not reprocess them.
    data_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    events_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    experiences_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    experiences_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    patterns_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    patterns_validated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    patterns_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    knowledge_activated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_flagged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: §23. Learned relationships the run wrote or refreshed, reported so a
    #: reviewer can tell "nothing was learned" from "learning was not run".
    relationships_created: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    relationships_updated: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    algorithm_versions: Mapped[dict] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    error_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class LearningExperiment(BaseModel):
    """An evaluation of a candidate algorithm or parameter set (§69).

    Deliberately inert: an experiment reads history, scores itself, and stops.
    There is no code path from an experiment to activation (§74) — a run whose
    numbers look good still has to be turned into knowledge through the normal
    validated pipeline.
    """

    __tablename__ = "intelligence_learning_experiments"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(80), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[LearningExperimentStatus] = mapped_column(
        SAEnum(LearningExperimentStatus, name="intelligence_experiment_status"),
        default=LearningExperimentStatus.RUNNING,
        nullable=False,
        index=True,
    )
    dataset_window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    dataset_window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: The temporal cut the experiment is scored *as of* (§31): knowledge the
    #: experiment claims must be derivable from rows at or before this moment.
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    feature_schema: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    parameters: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    metrics: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requested_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class ComponentReliabilityProfile(BaseModel):
    """Historical reliability facts about one component (§21).

    Not a prediction and not a score: counts, rates and durations computed over
    an explicit window from rows that exist. Recomputed by the learning run;
    the window it covers is stored on the row so a stale profile is detectable
    as stale rather than authoritative.
    """

    __tablename__ = "intelligence_component_profiles"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "component_id",
            "window_days",
            name="uq_component_profiles_scope",
        ),
        Index("ix_component_profiles_project", "project_id", "window_days"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    component_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("system_components.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    window_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    incident_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    anomaly_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remediation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rollback_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    regression_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Mean recovery time in seconds over the incidents in the window.
    mean_recovery_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    forecast_outcome_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    forecast_true_positive_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    #: §22. Whether the window's history constitutes a chronic-reliability
    #: signal. The flag recommends investigation; it never changes behaviour.
    chronic_signal: Mapped[bool] = mapped_column(nullable=False, default=False)
    chronic_reasons: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    breakdown: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)


class ReliabilityRecommendation(BaseModel):
    """An evidence-backed advisory (§38–§42).

    A recommendation names the knowledge, experiences and current evidence it
    stands on, states its uncertainty, and — critically for Phase 9 — reports
    what the policy would require if it were acted on. It never executes.
    """

    __tablename__ = "intelligence_recommendations"
    __table_args__ = (
        Index("ix_recommendations_project_status", "project_id", "status"),
        Index("ix_recommendations_project_component", "project_id", "component_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey(
            "environments.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey(
            "system_components.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    #: What triggered this recommendation — usually the current incident.
    incident_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey(
            "incidents.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    forecast_id: Mapped[Optional[uuid.UUID]] = mapped_column(Guid(), nullable=True)

    recommendation_type: Mapped[RecommendationType] = mapped_column(
        SAEnum(RecommendationType, name="intelligence_recommendation_type"),
        nullable=False,
    )
    status: Mapped[RecommendationStatus] = mapped_column(
        SAEnum(RecommendationStatus, name="intelligence_recommendation_status"),
        default=RecommendationStatus.OPEN,
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)

    #: §40. Why: knowledge ids, experience ids and current-evidence references,
    #: each with what it contributed. Empty sources cannot be persisted.
    knowledge_ids: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    experience_ids: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    current_evidence: Mapped[dict] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    #: §40/§41. Uncertainty and the ranking criteria, shown to the user.
    limitations: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    ranking: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    #: §42. What Phase 9 would demand if a human chose to act on this.
    policy_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    #: Historical context the recommendation is built from (§15/§16).
    historical: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    confidence: Mapped[KnowledgeConfidence] = mapped_column(
        SAEnum(KnowledgeConfidence, name="intelligence_confidence"),
        default=KnowledgeConfidence.UNKNOWN,
        nullable=False,
    )

    #: §43. What actually happened after this recommendation was acted on (or
    #: dismissed) — the record ARGUS learns from when it was wrong.
    decision: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    outcome: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    decided_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    fingerprint: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )


class RecommendationOutcome(BaseModel):
    """The observed result of an accepted recommendation (§43, §81)."""

    __tablename__ = "intelligence_recommendation_outcomes"
    __table_args__ = (
        Index("ix_recommendation_outcomes_project", "project_id", "recorded_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("intelligence_recommendations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    verdict: Mapped[str] = mapped_column(String(30), nullable=False)
    #: The remediation (if any) that was executed because of it.
    remediation_action_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), nullable=True
    )
    incident_id: Mapped[Optional[uuid.UUID]] = mapped_column(Guid(), nullable=True)
    detail: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recorded_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class KnowledgeReview(BaseModel):
    """A human review decision on a knowledge item (§72)."""

    __tablename__ = "intelligence_knowledge_reviews"
    __table_args__ = (
        Index("ix_knowledge_reviews_knowledge", "knowledge_id", "created_at"),
    )

    knowledge_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("reliability_knowledge.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    knowledge_version: Mapped[int] = mapped_column(Integer, nullable=False)


class LearningEventHook(BaseModel):
    """Registry of which pipeline event types are enabled (§63, §73).

    Configuration in the database rather than code so an operator can see and
    change what ARGUS is learning from — and so ``AI_GENERATED`` sources can be
    excluded wholesale without a deploy.
    """

    __tablename__ = "intelligence_event_hooks"

    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
    )
    #: LearningEventType values that are consumed when they occur. Absent means
    #: the event is recorded but not learned from.
    enabled_event_types: Mapped[list] = mapped_column(
        JSONType, nullable=False, default=list
    )
    #: DataProvenance values the pipeline will learn from. ``AI_GENERATED`` and
    #: ``MOCK`` are excluded by default (§76, §77).
    trusted_provenance: Mapped[list] = mapped_column(
        JSONType, nullable=False, default=list
    )
    updated_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


#: No ORM relationships here on purpose: knowledge/version rows are read in
#: explicit queries with their own ordering, and a lazy relationship between
#: them would silently issue N+1 selects inside the learning run.

__all__ = [
    "KnowledgeType",
    "KnowledgeStatus",
    "KnowledgeScope",
    "KnowledgeConfidence",
    "DataProvenance",
    "LearningEventType",
    "RecommendationType",
    "RecommendationStatus",
    "LearningRunStatus",
    "LearningExperimentStatus",
    "RelationshipKind",
    "RelationshipStatus",
    "RELATIONSHIP_MIN_SAMPLES",
    "knowledge_fingerprint",
    "ReliabilityExperience",
    "LearningEvent",
    "ReliabilityKnowledge",
    "LearnedRelationship",
    "KnowledgeVersion",
    "LearningRun",
    "LearningExperiment",
    "ComponentReliabilityProfile",
    "ReliabilityRecommendation",
    "RecommendationOutcome",
    "KnowledgeReview",
    "LearningEventHook",
]
