"""ARGUS Platform Models (Phase 11 §2–§16, §32–§35, §44–§55, §88–§94).

The unified reliability control plane's own domain: the *derived* state of the
system (component states and the transitions between them), the operational
object that ties a situation together (a case and its timeline), the workflow
that drives it, the event stream everything publishes to, and the governance
objects that a platform needs to be operable at all (SLOs, error budgets,
notifications, data-quality issues, configuration versions).

Three rules shape every table here:

* **Derived, never authoritative.** Component state, SLO status and error budgets
  are *computed* from rows other phases own. Nothing in this module writes to
  telemetry, incidents, analyses, forecasts or remediation.
* **Evidence or nothing.** A state change, a timeline entry, an issue and a
  notification all carry the rows they were derived from. A conclusion that
  cannot name its evidence is not stored.
* **Scoped or refused.** Every row is project-scoped, and the one exception
  (cross-project intelligence, §43) is off by default with a recorded policy
  rather than an implicit reach across tenants.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import List, Optional

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
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, Guid, JSONType


# ---------------------------------------------------------------------------
# §3, §4 — component operational state
# ---------------------------------------------------------------------------
class ComponentOperationalState(str, enum.Enum):
    """The unified operational state of one component (§3).

    Ordered by precedence, most severe first — see
    :mod:`app.services.system_state` for the exact conditions and why the order
    is what it is. The order is declared here so the API, the UI and the tests
    cannot disagree about which of two true signals wins.
    """

    INCIDENT = "INCIDENT"
    RECOVERING = "RECOVERING"
    DEGRADED = "DEGRADED"
    AT_RISK = "AT_RISK"
    HEALTHY = "HEALTHY"
    UNKNOWN = "UNKNOWN"


#: Precedence, most severe first. A component showing several of these states at
#: once reports the first one that applies.
STATE_PRECEDENCE: tuple[ComponentOperationalState, ...] = (
    ComponentOperationalState.INCIDENT,
    ComponentOperationalState.RECOVERING,
    ComponentOperationalState.DEGRADED,
    ComponentOperationalState.AT_RISK,
    ComponentOperationalState.HEALTHY,
    ComponentOperationalState.UNKNOWN,
)


class StateTransitionTrigger(str, enum.Enum):
    """What caused a state change (§5). Never a free-text guess."""

    EVIDENCE = "EVIDENCE"
    INCIDENT_OPENED = "INCIDENT_OPENED"
    INCIDENT_RESOLVED = "INCIDENT_RESOLVED"
    ANOMALY_DETECTED = "ANOMALY_DETECTED"
    ANOMALY_RESOLVED = "ANOMALY_RESOLVED"
    FORECAST_RISK = "FORECAST_RISK"
    REMEDIATION_STARTED = "REMEDIATION_STARTED"
    REMEDIATION_COMPLETED = "REMEDIATION_COMPLETED"
    VERIFICATION = "VERIFICATION"
    DEPLOYMENT = "DEPLOYMENT"
    DATA_GAP = "DATA_GAP"
    RECOMPUTED = "RECOMPUTED"


class ComponentStateTransition(BaseModel):
    """One component's state change, with its evidence (§5).

    This is what makes state *historical*: the current state is a row's latest
    transition, and "what was production doing at 14:05" is a query rather than
    a reconstruction from logs.
    """

    __tablename__ = "component_state_transitions"
    __table_args__ = (
        Index(
            "ix_component_state_transitions_component_time",
            "component_id",
            "occurred_at",
        ),
        Index(
            "ix_component_state_transitions_project_time",
            "project_id",
            "occurred_at",
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
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    component_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("system_components.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    previous_state: Mapped[Optional[ComponentOperationalState]] = mapped_column(
        SAEnum(ComponentOperationalState, name="component_operational_state"),
        nullable=True,
    )
    new_state: Mapped[ComponentOperationalState] = mapped_column(
        SAEnum(ComponentOperationalState, name="component_operational_state"),
        nullable=False,
        index=True,
    )
    trigger: Mapped[StateTransitionTrigger] = mapped_column(
        SAEnum(StateTransitionTrigger, name="state_transition_trigger"), nullable=False
    )
    #: Every row the state was derived from, so a state can be argued with.
    evidence: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system_state"
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    case_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), ForeignKey("reliability_cases.id", ondelete="SET NULL"), nullable=True
    )


# ---------------------------------------------------------------------------
# §14–§16 — the reliability case
# ---------------------------------------------------------------------------
class CaseStatus(str, enum.Enum):
    """The case lifecycle (§11, §14). Terminal states are terminal."""

    OPEN = "OPEN"
    TRIAGED = "TRIAGED"
    ANALYZING = "ANALYZING"
    DIAGNOSED = "DIAGNOSED"
    REMEDIATION_READY = "REMEDIATION_READY"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    LEARNED = "LEARNED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class CaseTrigger(str, enum.Enum):
    """What opened the case. A case is always *about* something stored."""

    INCIDENT = "INCIDENT"
    ANOMALY = "ANOMALY"
    FORECAST = "FORECAST"
    DEPLOYMENT = "DEPLOYMENT"
    SLO_BURN = "SLO_BURN"
    OPERATOR = "OPERATOR"


class ReliabilityCase(BaseModel):
    """The unified operational object for a situation (§14).

    A case is deliberately *not* a second incident: it holds references to what
    other phases concluded (`incident_id`, analyses, reproductions, patches,
    forecasts, remediations, knowledge) and a timeline of what happened. The
    incident subsystem stays authoritative about the incident.
    """

    __tablename__ = "reliability_cases"
    __table_args__ = (
        Index("ix_reliability_cases_project_status", "project_id", "status"),
        Index("ix_reliability_cases_project_opened", "project_id", "opened_at"),
        UniqueConstraint(
            "project_id", "reference", name="uq_reliability_cases_reference"
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
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    #: Human-facing identifier, unique inside a project ("CASE-7").
    reference: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[CaseStatus] = mapped_column(
        SAEnum(CaseStatus, name="case_status"),
        nullable=False,
        default=CaseStatus.OPEN,
        index=True,
    )
    trigger: Mapped[CaseTrigger] = mapped_column(
        SAEnum(CaseTrigger, name="case_trigger"), nullable=False
    )
    severity: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    primary_component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    component_ids: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    #: The Phase 3 incident this case is about, when it has one. The incident
    #: remains the authority on its own lifecycle; a case never edits it.
    incident_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey("incidents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: Set when the case was opened by something other than an incident.
    source_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(Guid(), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The §8 snapshot taken when the case opened, so the situation can be read
    #: as it looked then rather than as it looks now.
    opening_snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), nullable=True
    )
    opened_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status_changed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status_changed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    timeline: Mapped[List["ReliabilityCaseTimeline"]] = relationship(
        "ReliabilityCaseTimeline",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="ReliabilityCaseTimeline.sequence",
    )


class TimelineEntryKind(str, enum.Enum):
    """What kind of thing a unified timeline entry records (§15)."""

    STATE_CHANGE = "STATE_CHANGE"
    EVIDENCE = "EVIDENCE"
    ANALYSIS = "ANALYSIS"
    PREDICTION = "PREDICTION"
    RECOMMENDATION = "RECOMMENDATION"
    DECISION = "DECISION"
    EXECUTION = "EXECUTION"
    VERIFICATION = "VERIFICATION"
    RECOVERY = "RECOVERY"
    LEARNING = "LEARNING"
    NOTE = "NOTE"
    DATA_QUALITY = "DATA_QUALITY"


class ReliabilityCaseTimeline(BaseModel):
    """The unified timeline: one ordered record of everything that happened (§15).

    Entries are written by the control plane as it observes other subsystems
    finish work, and each one names its `source` and its `evidence`, so the
    timeline can always be traced back to the rows behind it.
    """

    __tablename__ = "reliability_case_timeline"
    __table_args__ = (
        Index("ix_reliability_case_timeline_case_seq", "case_id", "sequence"),
        UniqueConstraint(
            "case_id", "dedup_key", name="uq_reliability_case_timeline_dedup"
        ),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("reliability_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    kind: Mapped[TimelineEntryKind] = mapped_column(
        SAEnum(TimelineEntryKind, name="timeline_entry_kind"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: Which subsystem produced the entry — never guessed, always the producer.
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The rows behind the entry (ids by type), so a reader can go and look.
    evidence: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    actor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    system_action: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: Identity of the underlying fact, so a replay of the same event does not
    #: append the entry twice.
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)

    case: Mapped["ReliabilityCase"] = relationship(
        "ReliabilityCase", back_populates="timeline"
    )


# ---------------------------------------------------------------------------
# §11–§13 — the workflow engine
# ---------------------------------------------------------------------------
class WorkflowStage(str, enum.Enum):
    """The reliability workflow's stages (§11).

    Not every situation needs every stage: a case whose incident resolved on its
    own goes DETECTED → RESOLVED → LEARNED, and the engine records the stages it
    skipped rather than pretending they happened.
    """

    DETECTED = "DETECTED"
    TRIAGED = "TRIAGED"
    ANALYZING = "ANALYZING"
    DIAGNOSED = "DIAGNOSED"
    REMEDIATION_READY = "REMEDIATION_READY"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    LEARNED = "LEARNED"


class WorkflowStatus(str, enum.Enum):
    """Where a workflow run is (§12). Terminal states are terminal."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class WorkflowStopReason(str, enum.Enum):
    """Why a workflow stopped without finishing (§13).

    Each of these is a *safety* stop: the engine halts the orchestration and
    says which precondition no longer holds, rather than continuing to act on a
    situation that has changed underneath it.
    """

    AUTHORIZATION_EXPIRED = "AUTHORIZATION_EXPIRED"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    INCIDENT_GONE = "INCIDENT_GONE"
    STATE_CHANGED = "STATE_CHANGED"
    POLICY_CHANGED = "POLICY_CHANGED"
    KILL_SWITCH = "KILL_SWITCH"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


class ReliabilityWorkflow(BaseModel):
    """One run of the reliability workflow for one case (§11–§13).

    The engine is deliberately small and provider-neutral: stages, transitions,
    a deadline, bounded attempts and an explicit stop reason. It orchestrates
    the phases that already exist; it does not reimplement any of them.
    """

    __tablename__ = "reliability_workflows"
    __table_args__ = (
        Index("ix_reliability_workflows_project_status", "project_id", "status"),
        Index("ix_reliability_workflows_case", "case_id", "created_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey("reliability_cases.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    stage: Mapped[WorkflowStage] = mapped_column(
        SAEnum(WorkflowStage, name="workflow_stage"),
        nullable=False,
        default=WorkflowStage.DETECTED,
    )
    status: Mapped[WorkflowStatus] = mapped_column(
        SAEnum(WorkflowStatus, name="workflow_status"),
        nullable=False,
        default=WorkflowStatus.PENDING,
        index=True,
    )
    #: Stages the run has passed through, in order. Kept so a skipped stage is
    #: visible as skipped rather than absent.
    completed_stages: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    #: The §7 context the run was started with, frozen.
    context: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    state: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: A waiting workflow wakes at `next_run_at`; nothing here is a busy loop.
    next_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    deadline_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stop_reason: Mapped[Optional[WorkflowStopReason]] = mapped_column(
        SAEnum(WorkflowStopReason, name="workflow_stop_reason"), nullable=True
    )
    stop_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


# ---------------------------------------------------------------------------
# §9, §10 — the unified event stream
# ---------------------------------------------------------------------------
class PlatformEventType(str, enum.Enum):
    """Domain events the platform publishes and correlates (§9)."""

    COMPONENT_STATE_CHANGED = "COMPONENT_STATE_CHANGED"
    ANOMALY_DETECTED = "ANOMALY_DETECTED"
    INCIDENT_CREATED = "INCIDENT_CREATED"
    INCIDENT_UPDATED = "INCIDENT_UPDATED"
    RCA_COMPLETED = "RCA_COMPLETED"
    REPRODUCTION_COMPLETED = "REPRODUCTION_COMPLETED"
    PATCH_VERIFIED = "PATCH_VERIFIED"
    FORECAST_GENERATED = "FORECAST_GENERATED"
    RISK_CHANGED = "RISK_CHANGED"
    REMEDIATION_PROPOSED = "REMEDIATION_PROPOSED"
    REMEDIATION_STARTED = "REMEDIATION_STARTED"
    REMEDIATION_COMPLETED = "REMEDIATION_COMPLETED"
    REMEDIATION_ROLLED_BACK = "REMEDIATION_ROLLED_BACK"
    LEARNING_COMPLETED = "LEARNING_COMPLETED"
    DEPLOYMENT_RECORDED = "DEPLOYMENT_RECORDED"
    SLO_STATUS_CHANGED = "SLO_STATUS_CHANGED"
    ERROR_BUDGET_BURN = "ERROR_BUDGET_BURN"
    DATA_QUALITY_ISSUE = "DATA_QUALITY_ISSUE"
    CASE_OPENED = "CASE_OPENED"
    CASE_CLOSED = "CASE_CLOSED"
    CASE_STATUS_CHANGED = "CASE_STATUS_CHANGED"
    CONFIGURATION_CHANGED = "CONFIGURATION_CHANGED"
    NOTIFICATION_RAISED = "NOTIFICATION_RAISED"


class PlatformEvent(BaseModel):
    """One published domain event (§9, §10).

    Reuses the platform's existing event *idiom* (an append-only row with a dedup
    key and a processed marker, exactly like Phase 10's learning events) rather
    than introducing a second broker: the queue already exists, and an event
    stream that cannot be replayed is not evidence.

    ``correlation_id`` is what turns a pile of events into one operational story:
    everything that happens because of one situation carries the same id, so
    "deployment → anomaly → incident → RCA → remediation → recovery → learning"
    is a query.
    """

    __tablename__ = "platform_events"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_platform_events_dedup_key"),
        Index("ix_platform_events_project_occurred", "project_id", "occurred_at"),
        Index("ix_platform_events_correlation", "correlation_id", "occurred_at"),
        Index("ix_platform_events_type_time", "event_type", "occurred_at"),
    )

    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[PlatformEventType] = mapped_column(
        SAEnum(PlatformEventType, name="platform_event_type"),
        nullable=False,
        index=True,
    )
    #: Ties the event to the case it belongs to, when it belongs to one.
    case_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), ForeignKey("reliability_cases.id", ondelete="SET NULL"), nullable=True
    )
    correlation_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    #: The phase that produced the event, so the platform never has to guess.
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    subject_id: Mapped[Optional[uuid.UUID]] = mapped_column(Guid(), nullable=True)
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey("system_components.id", ondelete="SET NULL"),
        nullable=True,
    )
    payload: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consumed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


# ---------------------------------------------------------------------------
# §8 — context snapshots
# ---------------------------------------------------------------------------
class ReliabilityContextSnapshot(BaseModel):
    """A frozen view of the situation at one instant (§8).

    Snapshots exist because every downstream decision is made *at a time*: a
    recommendation, a remediation assessment and a postmortem each need to show
    what was known when they were produced, not what is known now.
    """

    __tablename__ = "reliability_context_snapshots"
    __table_args__ = (
        Index(
            "ix_reliability_context_snapshots_project_created",
            "project_id",
            "created_at",
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
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey("system_components.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    case_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), ForeignKey("reliability_cases.id", ondelete="CASCADE"), nullable=True
    )
    incident_id: Mapped[Optional[uuid.UUID]] = mapped_column(Guid(), nullable=True)
    #: Which scope the snapshot covers: PROJECT | ENVIRONMENT | COMPONENT | CASE.
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    #: The snapshot itself: the §2 system state plus whatever references the
    #: caller asked to include. Bounded on purpose (see the builder).
    snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


# ---------------------------------------------------------------------------
# §32–§35 — objectives and error budgets
# ---------------------------------------------------------------------------
class SloIndicator(str, enum.Enum):
    """What an objective measures (§32). A closed set, not free text."""

    AVAILABILITY = "AVAILABILITY"
    LATENCY = "LATENCY"
    ERROR_RATE = "ERROR_RATE"
    SATURATION = "SATURATION"
    CUSTOM = "CUSTOM"


class SloComparison(str, enum.Enum):
    """Which side of the target is good. Stated, never assumed."""

    AT_LEAST = "AT_LEAST"
    AT_MOST = "AT_MOST"


class SloStatus(str, enum.Enum):
    """Compliance, computed per window (§33). ``UNKNOWN`` is first-class."""

    MEETING = "MEETING"
    AT_RISK = "AT_RISK"
    BREACHED = "BREACHED"
    UNKNOWN = "UNKNOWN"


class BurnRateState(str, enum.Enum):
    """Error-budget burn classification (§35). Thresholds are configuration."""

    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    FAST_BURN = "FAST_BURN"
    CRITICAL_BURN = "CRITICAL_BURN"
    UNKNOWN = "UNKNOWN"


class ServiceLevelObjective(BaseModel):
    """A reliability objective for a component (§32–§33).

    Optional by design: a deployment with no objectives gets no SLO read model at
    all, rather than a table full of invented targets.
    """

    __tablename__ = "service_level_objectives"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "name", "component_id", name="uq_slo_project_name_component"
        ),
        Index("ix_slo_project_enabled", "project_id", "enabled"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(),
        ForeignKey("system_components.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    indicator: Mapped[SloIndicator] = mapped_column(
        SAEnum(SloIndicator, name="slo_indicator"), nullable=False
    )
    comparison: Mapped[SloComparison] = mapped_column(
        SAEnum(SloComparison, name="slo_comparison"),
        nullable=False,
        default=SloComparison.AT_LEAST,
    )
    #: The metric this objective reads. Required for every indicator but
    #: ``CUSTOM``, which may instead carry an explicit measurement description.
    metric_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    target: Mapped[float] = mapped_column(Float, nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=86_400)
    unit: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: When a burn should raise a platform event (§36). Advisory, never an action.
    alert_on_burn: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )


class ErrorBudgetSnapshot(BaseModel):
    """One computed error-budget reading for one objective (§34, §35).

    Stored rather than computed on read, because a burn classification that
    cannot be looked at *afterwards* cannot be argued with — and because the
    report and trend views need history, not just the current number.
    """

    __tablename__ = "error_budget_snapshots"
    __table_args__ = (
        Index("ix_error_budget_snapshots_slo_time", "slo_id", "computed_at"),
        Index("ix_error_budget_snapshots_project_time", "project_id", "computed_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slo_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("service_level_objectives.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), ForeignKey("system_components.id", ondelete="CASCADE"), nullable=True
    )
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[SloStatus] = mapped_column(
        SAEnum(SloStatus, name="slo_status"), nullable=False
    )
    #: The objective's allowed failure budget over the window, in the indicator's
    #: own unit, and how much of it was consumed.
    allowed_failure: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    observed_failure: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    remaining: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    remaining_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    burn_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    burn_state: Mapped[BurnRateState] = mapped_column(
        SAEnum(BurnRateState, name="burn_rate_state"),
        nullable=False,
        default=BurnRateState.UNKNOWN,
    )
    compliance_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    data_quality: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    #: What the reading was derived from, and what it could not see.
    evidence: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    limitations: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


# ---------------------------------------------------------------------------
# §54–§56 — notifications
# ---------------------------------------------------------------------------
class NotificationKind(str, enum.Enum):
    """What an operator is being told (§55). Closed set, configurable on/off."""

    CRITICAL_INCIDENT = "CRITICAL_INCIDENT"
    HIGH_PREDICTED_RISK = "HIGH_PREDICTED_RISK"
    REMEDIATION_APPROVAL = "REMEDIATION_APPROVAL"
    REMEDIATION_FAILURE = "REMEDIATION_FAILURE"
    ROLLBACK = "ROLLBACK"
    SLO_BURN = "SLO_BURN"
    LEARNING_INSIGHT = "LEARNING_INSIGHT"
    SYSTEM_DEGRADATION = "SYSTEM_DEGRADATION"
    DATA_QUALITY = "DATA_QUALITY"


class NotificationSeverity(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class NotificationStatus(str, enum.Enum):
    UNREAD = "UNREAD"
    READ = "READ"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    SUPPRESSED = "SUPPRESSED"


class NotificationChannel(str, enum.Enum):
    IN_APP = "IN_APP"
    EMAIL = "EMAIL"
    WEBHOOK = "WEBHOOK"


class PlatformNotification(BaseModel):
    """One notification, already deduplicated (§54–§56).

    The row *is* the in-app channel. Email and webhook delivery are separate
    attempts recorded on the same row, so "was anyone told?" is answerable
    without reading provider logs.
    """

    __tablename__ = "platform_notifications"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "fingerprint", "dedup_bucket", name="uq_notifications_dedup"
        ),
        Index("ix_platform_notifications_project_created", "project_id", "created_at"),
        Index("ix_platform_notifications_status", "status", "severity"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), ForeignKey("environments.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[NotificationKind] = mapped_column(
        SAEnum(NotificationKind, name="notification_kind"), nullable=False
    )
    severity: Mapped[NotificationSeverity] = mapped_column(
        SAEnum(NotificationSeverity, name="notification_severity"),
        nullable=False,
        default=NotificationSeverity.WARNING,
    )
    status: Mapped[NotificationStatus] = mapped_column(
        SAEnum(NotificationStatus, name="notification_status"),
        nullable=False,
        default=NotificationStatus.UNREAD,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Where the notification came from, and what it is about.
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    subject_id: Mapped[Optional[uuid.UUID]] = mapped_column(Guid(), nullable=True)
    case_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), ForeignKey("reliability_cases.id", ondelete="SET NULL"), nullable=True
    )
    link: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    evidence: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    #: Dedup identity (§56): same fingerprint inside the same time bucket is one
    #: notification, with a count — not thirty.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    dedup_bucket: Mapped[int] = mapped_column(Integer, nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    channels_attempted: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    delivery: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


# ---------------------------------------------------------------------------
# §87–§90 — data quality
# ---------------------------------------------------------------------------
class DataQualityIssueKind(str, enum.Enum):
    """The consistency problems ARGUS checks for (§88)."""

    ORPHANED_RECORD = "ORPHANED_RECORD"
    INCIDENT_WITHOUT_COMPONENT = "INCIDENT_WITHOUT_COMPONENT"
    PREDICTION_WITHOUT_SNAPSHOT = "PREDICTION_WITHOUT_SNAPSHOT"
    REMEDIATION_WITHOUT_AUTHORIZATION = "REMEDIATION_WITHOUT_AUTHORIZATION"
    KNOWLEDGE_WITHOUT_EVIDENCE = "KNOWLEDGE_WITHOUT_EVIDENCE"
    STALE_COMPONENT = "STALE_COMPONENT"
    MISSING_TELEMETRY = "MISSING_TELEMETRY"
    BROKEN_RELATIONSHIP = "BROKEN_RELATIONSHIP"
    INCONSISTENT_STATE = "INCONSISTENT_STATE"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"
    #: Hardening W5 — the checks the second audit pass added, each one a
    #: different way a row can look fine and still be untrustworthy.
    #:
    #: There is deliberately no ``DUPLICATE_INCIDENT``. The audit that designed
    #: these checks proposed one, and implementation proved it unnecessary: a
    #: partial unique index on ``(project_id, fingerprint)`` for unresolved
    #: statuses makes two live incidents with one fingerprint impossible to
    #: insert. An invariant enforced by the schema does not need a detector —
    #: adding one would have implied a gap that does not exist.
    MISSING_TIMESTAMP = "MISSING_TIMESTAMP"
    MISSING_PROVENANCE = "MISSING_PROVENANCE"
    IMPOSSIBLE_TRANSITION = "IMPOSSIBLE_TRANSITION"
    MISSING_AUDIT_EVENT = "MISSING_AUDIT_EVENT"
    CORRUPTED_ARTIFACT = "CORRUPTED_ARTIFACT"


class DataQualitySeverity(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class DataQualityStatus(str, enum.Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    IGNORED = "IGNORED"


class DataQualityIssue(BaseModel):
    """One detected inconsistency, with its subject and a suggested fix (§88–§90).

    A suggestion is text an operator may act on. Nothing here mutates historical
    evidence: the platform reports the inconsistency and stops.
    """

    __tablename__ = "data_quality_issues"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "kind",
            "subject_type",
            "subject_id",
            name="uq_data_quality_subject",
        ),
        Index("ix_data_quality_project_status", "project_id", "status"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), ForeignKey("environments.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[DataQualityIssueKind] = mapped_column(
        SAEnum(DataQualityIssueKind, name="data_quality_issue_kind"),
        nullable=False,
        index=True,
    )
    severity: Mapped[DataQualitySeverity] = mapped_column(
        SAEnum(DataQualitySeverity, name="data_quality_severity"),
        nullable=False,
        default=DataQualitySeverity.WARNING,
    )
    status: Mapped[DataQualityStatus] = mapped_column(
        SAEnum(DataQualityStatus, name="data_quality_status"),
        nullable=False,
        default=DataQualityStatus.OPEN,
        index=True,
    )
    subject_type: Mapped[str] = mapped_column(String(48), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Guid(), nullable=False, index=True)
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Guid(), ForeignKey("system_components.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    evidence: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    #: What an operator could do about it. Advisory text, never an action.
    suggestion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


# ---------------------------------------------------------------------------
# §91–§94 — configuration versions
# ---------------------------------------------------------------------------
class ConfigurationScope(str, enum.Enum):
    """What a versioned configuration row configures (§91)."""

    PROJECT = "PROJECT"
    ENVIRONMENT = "ENVIRONMENT"
    PROJECT_SETTINGS = "PROJECT_SETTINGS"
    SLO = "SLO"
    REMEDIATION_POLICY = "REMEDIATION_POLICY"
    LEARNING = "LEARNING"
    NOTIFICATIONS = "NOTIFICATIONS"
    RETENTION = "RETENTION"
    INTEGRATIONS = "INTEGRATIONS"
    FEATURE_FLAGS = "FEATURE_FLAGS"


class ConfigurationVersion(BaseModel):
    """An append-only configuration revision (§93, §94).

    Configuration is *versioned* rather than updated in place because an
    autonomous platform's biggest audit question is "what was ARGUS configured
    to do when it did that?". A rollback writes a new version; nothing here
    rewrites history.
    """

    __tablename__ = "configuration_versions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "scope", "scope_id", "version", name="uq_config_version"
        ),
        Index(
            "ix_configuration_versions_scope_lookup", "project_id", "scope", "scope_id"
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Guid(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scope: Mapped[ConfigurationScope] = mapped_column(
        SAEnum(ConfigurationScope, name="configuration_scope"),
        nullable=False,
        index=True,
    )
    #: The row this configuration belongs to (an SLO id, a policy id), or null
    #: for a scope that is itself the subject (project settings, feature flags).
    scope_id: Mapped[Optional[uuid.UUID]] = mapped_column(Guid(), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    settings: Mapped[dict] = mapped_column(JSONType, nullable=False)
    #: Redacted, versioned and diffable: secrets never appear here, only their
    #: presence (``redacted_fields``) so an operator can see that a secret was
    #: set without the value being stored twice.
    redacted_fields: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    previous_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    change_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    changed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rolled_back_from: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    authorizing_actor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


__all__ = [
    "BurnRateState",
    "CaseStatus",
    "CaseTrigger",
    "ComponentOperationalState",
    "ComponentStateTransition",
    "ConfigurationScope",
    "ConfigurationVersion",
    "DataQualityIssue",
    "DataQualityIssueKind",
    "DataQualitySeverity",
    "DataQualityStatus",
    "ErrorBudgetSnapshot",
    "NotificationChannel",
    "NotificationKind",
    "NotificationSeverity",
    "NotificationStatus",
    "PlatformEvent",
    "PlatformEventType",
    "PlatformNotification",
    "ReliabilityCase",
    "ReliabilityCaseTimeline",
    "ReliabilityContextSnapshot",
    "ReliabilityWorkflow",
    "STATE_PRECEDENCE",
    "ServiceLevelObjective",
    "SloComparison",
    "SloIndicator",
    "SloStatus",
    "StateTransitionTrigger",
    "TimelineEntryKind",
    "WorkflowStage",
    "WorkflowStatus",
    "WorkflowStopReason",
]
