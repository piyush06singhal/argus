"""ARGUS Platform Schemas (Phase 11 §68–§71, §115).

Response models for the unified platform surface.

The shape rule, applied consistently: **a derived number is never returned
without its provenance.** Components come back with their state *and the reason
for it*; SLO readings come back with their sample count and window; reports come
back with their limitations; scores come back with their methodology. A response
that is convenient to render but cannot be argued with is the failure mode these
schemas exist to prevent.

Errors follow §70 through :class:`PlatformErrorResponse` — ``code``, ``message``,
``details``, ``request_id``, ``timestamp`` — and never carry a stack trace.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import Field

from app.schemas.base import BaseSchema


# ---------------------------------------------------------------------------
# §70 — the error model
# ---------------------------------------------------------------------------
class PlatformErrorResponse(BaseSchema):
    """The unified error shape (§70)."""

    code: str
    message: str
    details: Optional[dict[str, Any]] = None
    request_id: Optional[str] = None
    timestamp: datetime
    #: §70: never a stack trace. Present so the contract is explicit rather than
    #: a matter of restraint.
    trace_exposed: bool = False


# ---------------------------------------------------------------------------
# §2–§5 — system state
# ---------------------------------------------------------------------------
class ComponentStateItem(BaseSchema):
    id: str
    name: str
    component_type: str
    environment_id: Optional[str] = None
    state: str
    state_reason: Optional[str] = None
    state_evidence: dict[str, Any] = Field(default_factory=dict)


class DependencyItem(BaseSchema):
    id: str
    source_component_id: str
    source_name: Optional[str] = None
    target_component_id: str
    target_name: Optional[str] = None
    dependency_type: str


class StateCounts(BaseSchema):
    INCIDENT: int = 0
    RECOVERING: int = 0
    DEGRADED: int = 0
    AT_RISK: int = 0
    HEALTHY: int = 0
    UNKNOWN: int = 0


class SystemHealthSummary(BaseSchema):
    components_total: int = 0
    components_with_evidence: int = 0
    components_unknown: int = 0
    state_counts: dict[str, int] = Field(default_factory=dict)
    coverage_percent: float = 0.0


class SystemStateResponse(BaseSchema):
    """The §2 unified state, with its own completeness statement."""

    project_id: str
    environment_id: Optional[str] = None
    as_of: datetime
    components: list[ComponentStateItem] = Field(default_factory=list)
    dependencies: list[DependencyItem] = Field(default_factory=list)
    health: SystemHealthSummary = Field(default_factory=SystemHealthSummary)
    state_counts: dict[str, int] = Field(default_factory=dict)
    active_anomalies: list[dict[str, Any]] = Field(default_factory=list)
    active_incidents: list[dict[str, Any]] = Field(default_factory=list)
    predicted_risks: list[dict[str, Any]] = Field(default_factory=list)
    recent_changes: list[dict[str, Any]] = Field(default_factory=list)
    active_remediations: list[dict[str, Any]] = Field(default_factory=list)
    reliability_patterns: list[dict[str, Any]] = Field(default_factory=list)
    data_quality: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)


class StateTransitionItem(BaseSchema):
    id: uuid.UUID
    component_id: uuid.UUID
    previous_state: Optional[str] = None
    new_state: str
    trigger: str
    reason: Optional[str] = None
    evidence: Optional[dict[str, Any]] = None
    source: str
    occurred_at: datetime


class StateHistoryResponse(BaseSchema):
    component_id: uuid.UUID
    transitions: list[StateTransitionItem] = Field(default_factory=list)
    as_of_state: Optional[str] = None
    note: str = (
        "a state is derived from evidence at a moment; the transition history is "
        "what makes a past state queryable rather than re-derived"
    )


# ---------------------------------------------------------------------------
# §7, §8 — context
# ---------------------------------------------------------------------------
class ContextSnapshotResponse(BaseSchema):
    id: uuid.UUID
    project_id: uuid.UUID
    scope: str
    fingerprint: str
    as_of: datetime
    snapshot: dict[str, Any]
    created_by: Optional[str] = None
    created_at: datetime


class ContextResponse(BaseSchema):
    references: dict[str, Any]
    description: Optional[str] = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    unavailable: dict[str, str] = Field(default_factory=dict)
    fingerprint: str


# ---------------------------------------------------------------------------
# §14–§16 — cases
# ---------------------------------------------------------------------------
class CaseSummary(BaseSchema):
    id: str
    reference: str
    title: str
    summary: Optional[str] = None
    status: str
    trigger: str
    severity: Optional[str] = None
    project_id: str
    environment_id: Optional[str] = None
    primary_component_id: Optional[str] = None
    component_ids: list[str] = Field(default_factory=list)
    incident_id: Optional[str] = None
    opened_at: datetime
    closed_at: Optional[datetime] = None
    opened_by: Optional[str] = None
    duration_seconds: Optional[float] = None
    timeline_entries: int = 0
    last_event_at: Optional[datetime] = None
    allowed_transitions: list[str] = Field(default_factory=list)


class TimelineEntryItem(BaseSchema):
    sequence: int
    occurred_at: datetime
    kind: str
    event_type: str
    title: str
    detail: Optional[str] = None
    component_id: Optional[uuid.UUID] = None
    source: str
    evidence: Optional[dict[str, Any]] = None
    actor: Optional[str] = None
    system_action: bool = False
    result: Optional[str] = None


class CaseListResponse(BaseSchema):
    cases: list[CaseSummary] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


class CaseDetailResponse(BaseSchema):
    case: CaseSummary
    timeline: list[TimelineEntryItem] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    workflows: list[dict[str, Any]] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class CaseStatusChangeRequest(BaseSchema):
    status: str
    reason: Optional[str] = None
    actor: Optional[str] = None


# ---------------------------------------------------------------------------
# §27, §28 — the case assistant
# ---------------------------------------------------------------------------
class AssistantQuestionRequest(BaseSchema):
    question: str = Field(min_length=1, max_length=1000)
    include_evidence: bool = False


class CitationItem(BaseSchema):
    kind: str
    source: str
    row_id: str
    label: str
    detail: Optional[str] = None


class CaseAssistantCapabilityResponse(BaseSchema):
    """What the case assistant will and will not do (§28).

    The sheet is served even when the assistant is switched off, so an operator
    can see that it is off and read the guarantees they would be relying on — a
    disabled capability is a fact to display, not a 404 to hit.
    """

    enabled: bool
    grounded_in: str
    guarantees: list[str] = Field(default_factory=list)
    refuses: list[str] = Field(default_factory=list)
    #: The question shapes the assistant recognises, so a caller can see the
    #: surface rather than discovering it by trial and error.
    questions: list[str] = Field(default_factory=list)


class AssistantAnswerResponse(BaseSchema):
    question: str
    intent: str
    answer: str
    confidence: float
    confidence_reason: str
    citations: list[CitationItem] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    narrator: Optional[str] = None
    grounding: dict[str, Any] = Field(default_factory=dict)
    evidence: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# §19–§25 — the dashboard
# ---------------------------------------------------------------------------
class OverviewResponse(BaseSchema):
    project_id: str
    as_of: datetime
    executive_summary: dict[str, Any] = Field(default_factory=dict)
    health: dict[str, Any] = Field(default_factory=dict)
    state_counts: dict[str, int] = Field(default_factory=dict)
    active_incidents: list[dict[str, Any]] = Field(default_factory=list)
    predicted_risks: list[dict[str, Any]] = Field(default_factory=list)
    active_remediations: list[dict[str, Any]] = Field(default_factory=list)
    recent_changes: list[dict[str, Any]] = Field(default_factory=list)
    top_risky_components: list[dict[str, Any]] = Field(default_factory=list)
    recent_recoveries: list[dict[str, Any]] = Field(default_factory=list)
    learning_insights: dict[str, Any] = Field(default_factory=dict)
    argus_health: dict[str, Any] = Field(default_factory=dict)
    data_quality: dict[str, Any] = Field(default_factory=dict)
    open_cases: list[dict[str, Any]] = Field(default_factory=list)
    slo: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)


class ActivityItem(BaseSchema):
    id: str
    event_type: str
    title: str
    source: str
    occurred_at: datetime
    subject_type: Optional[str] = None
    subject_id: Optional[str] = None
    component_id: Optional[str] = None
    case_id: Optional[str] = None
    correlation_id: Optional[str] = None
    link: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    processed: bool = False


class ActivityResponse(BaseSchema):
    items: list[ActivityItem] = Field(default_factory=list)
    limit: int = 50
    offset: int = 0


class StoryResponse(BaseSchema):
    correlation_id: str
    events: list[ActivityItem] = Field(default_factory=list)
    stage_count: int = 0
    note: str


# ---------------------------------------------------------------------------
# §30, §31 — the service catalog
# ---------------------------------------------------------------------------
class CatalogEntryResponse(BaseSchema):
    component_id: str
    name: str
    component_type: str
    environment_id: Optional[str] = None
    environment_name: Optional[str] = None
    owner: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[dict[str, Any]] = Field(default_factory=list)
    dependents: list[dict[str, Any]] = Field(default_factory=list)
    endpoints: list[dict[str, Any]] = Field(default_factory=list)
    state: str
    state_reason: Optional[str] = None
    available: Optional[bool] = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    risk: dict[str, Any] = Field(default_factory=dict)
    incident_history: list[dict[str, Any]] = Field(default_factory=list)
    deployment_history: list[dict[str, Any]] = Field(default_factory=list)
    remediation_history: list[dict[str, Any]] = Field(default_factory=list)
    reliability_profile: Optional[dict[str, Any]] = None
    scorecard: Optional[dict[str, Any]] = None
    unavailable: dict[str, str] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)


class CatalogListResponse(BaseSchema):
    services: list[CatalogEntryResponse] = Field(default_factory=list)
    total: int = 0


class OwnershipRequest(BaseSchema):
    team: str = Field(min_length=1, max_length=255)
    owner_name: Optional[str] = None
    contact_email: Optional[str] = None
    repository_owner: Optional[str] = None
    on_call: Optional[str] = None
    documentation_url: Optional[str] = None
    actor: Optional[str] = None


# ---------------------------------------------------------------------------
# §32–§35 — SLOs
# ---------------------------------------------------------------------------
class SloItem(BaseSchema):
    slo_id: str
    name: str
    indicator: str
    target: float
    comparison: str
    unit: Optional[str] = None
    component_id: Optional[str] = None
    enabled: bool = True
    status: str
    reading: Optional[float] = None
    burn_rate: Optional[float] = None
    burn_state: Optional[str] = None
    remaining_percent: Optional[float] = None
    computed_at: Optional[datetime] = None
    never_evaluated: bool = False


class SloOverviewResponse(BaseSchema):
    as_of: datetime
    objectives_total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    objectives: list[SloItem] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class SloEvaluationResponse(BaseSchema):
    slo_id: str
    name: str
    indicator: str
    window_start: datetime
    window_end: datetime
    status: str
    reading: Optional[float] = None
    target: float
    comparison: str
    sample_count: int = 0
    allowed_failure: Optional[float] = None
    observed_failure: Optional[float] = None
    remaining: Optional[float] = None
    remaining_percent: Optional[float] = None
    burn_rate: Optional[float] = None
    burn_state: str
    compliance_percent: Optional[float] = None
    data_quality: str = "OK"
    evidence: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)


class SloCreateRequest(BaseSchema):
    name: str = Field(min_length=1, max_length=255)
    indicator: str
    target: float
    comparison: str = "AT_LEAST"
    metric_name: Optional[str] = None
    component_id: Optional[uuid.UUID] = None
    environment_id: Optional[uuid.UUID] = None
    window_seconds: int = 86400
    unit: Optional[str] = None
    description: Optional[str] = None
    actor: Optional[str] = None


class ErrorBudgetResponse(BaseSchema):
    slo_id: str
    name: str
    latest: Optional[dict[str, Any]] = None
    history: list[dict[str, Any]] = Field(default_factory=list)
    definition: str


# ---------------------------------------------------------------------------
# §38–§41, §84 — change intelligence
# ---------------------------------------------------------------------------
class ChangeListResponse(BaseSchema):
    changes: list[dict[str, Any]] = Field(default_factory=list)
    note: str = (
        "a change near an incident is worth looking at; ARGUS does not claim the "
        "change caused it"
    )


class EnvironmentComparisonResponse(BaseSchema):
    project_id: str
    left: dict[str, Any]
    right: dict[str, Any]
    differences: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ChangeFailureRateResponse(BaseSchema):
    window_days: int
    deployments_total: int = 0
    deployments_succeeded: int = 0
    deployments_failed: int = 0
    deployments_rolled_back: int = 0
    deployments_with_incident: int = 0
    failure_rate: Optional[float] = None
    methodology: str
    limitations: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §57–§60, §105–§107 — platform health
# ---------------------------------------------------------------------------
class SubsystemHealthItem(BaseSchema):
    name: str
    status: str
    required: bool = False
    optional: bool = True
    latency_ms: Optional[float] = None
    detail: Optional[str] = None
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    queue_depth: Optional[int] = None
    error_rate: Optional[float] = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class PlatformHealthResponse(BaseSchema):
    as_of: datetime
    status: str
    ready: bool
    subsystems: list[SubsystemHealthItem] = Field(default_factory=list)
    degraded_capabilities: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)


class ReadinessResponse(BaseSchema):
    ready: bool
    required_subsystems: list[SubsystemHealthItem] = Field(default_factory=list)
    optional_subsystems: list[SubsystemHealthItem] = Field(default_factory=list)
    reason: str


class DependencyHealthResponse(BaseSchema):
    as_of: datetime
    dependencies: list[SubsystemHealthItem] = Field(default_factory=list)
    required_count: int = 0
    optional_count: int = 0
    graceful_degradation: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# §87–§90 — data quality
# ---------------------------------------------------------------------------
class DataQualityIssueItem(BaseSchema):
    id: uuid.UUID
    kind: str
    severity: str
    status: str
    subject_type: str
    subject_id: uuid.UUID
    component_id: Optional[uuid.UUID] = None
    title: str
    detail: Optional[str] = None
    evidence: Optional[dict[str, Any]] = None
    suggestion: Optional[str] = None
    detected_at: datetime
    last_seen_at: datetime
    occurrence_count: int = 1


class DataQualityResponse(BaseSchema):
    summary: dict[str, Any] = Field(default_factory=dict)
    issues: list[DataQualityIssueItem] = Field(default_factory=list)
    descriptions: dict[str, str] = Field(default_factory=dict)


class DataQualityStatusRequest(BaseSchema):
    status: str
    actor: Optional[str] = None


# ---------------------------------------------------------------------------
# §91–§94 — configuration
# ---------------------------------------------------------------------------
class ConfigurationVersionItem(BaseSchema):
    id: uuid.UUID
    scope: str
    scope_id: Optional[uuid.UUID] = None
    version: int
    settings: dict[str, Any] = Field(default_factory=dict)
    redacted_fields: list[str] = Field(default_factory=list)
    previous_version: Optional[int] = None
    change_summary: Optional[str] = None
    changed_by: Optional[str] = None
    reason: Optional[str] = None
    rolled_back_from: Optional[int] = None
    created_at: datetime


class ConfigurationResponse(BaseSchema):
    project_id: str
    sections: dict[str, Any] = Field(default_factory=dict)
    overrides: dict[str, Any] = Field(default_factory=dict)
    redacted_fields: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    versions: list[ConfigurationVersionItem] = Field(default_factory=list)


class ConfigurationUpdateRequest(BaseSchema):
    scope: str
    settings: dict[str, Any]
    scope_id: Optional[uuid.UUID] = None
    change_summary: Optional[str] = None
    reason: Optional[str] = None
    actor: Optional[str] = None


class ConfigurationRollbackRequest(BaseSchema):
    scope: str
    target_version: int
    scope_id: Optional[uuid.UUID] = None
    actor: Optional[str] = None
    reason: Optional[str] = None


class FeatureFlagsResponse(BaseSchema):
    flags: dict[str, bool] = Field(default_factory=dict)
    reasons: dict[str, str] = Field(default_factory=dict)
    defaults: str


# ---------------------------------------------------------------------------
# §53–§56 — notifications
# ---------------------------------------------------------------------------
class NotificationItem(BaseSchema):
    id: uuid.UUID
    kind: str
    severity: str
    status: str
    title: str
    body: Optional[str] = None
    source: str
    subject_type: Optional[str] = None
    subject_id: Optional[uuid.UUID] = None
    case_id: Optional[uuid.UUID] = None
    link: Optional[str] = None
    evidence: Optional[dict[str, Any]] = None
    occurrence_count: int = 1
    channels_attempted: Optional[list[str]] = None
    delivery: Optional[dict[str, Any]] = None
    delivered_at: Optional[datetime] = None
    read_at: Optional[datetime] = None
    acknowledged_by: Optional[str] = None
    created_at: datetime


class NotificationListResponse(BaseSchema):
    notifications: list[NotificationItem] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class NotificationAckRequest(BaseSchema):
    actor: Optional[str] = None
    acknowledge: bool = False


# ---------------------------------------------------------------------------
# §17, §18 — search
# ---------------------------------------------------------------------------
class SearchHitItem(BaseSchema):
    kind: str
    id: str
    title: str
    subtitle: Optional[str] = None
    status: Optional[str] = None
    occurred_at: Optional[str] = None
    component_id: Optional[str] = None
    route: Optional[str] = None
    matched_field: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseSchema):
    query: str
    total: int = 0
    by_kind: dict[str, int] = Field(default_factory=dict)
    results: dict[str, list[SearchHitItem]] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class SearchHelpResponse(BaseSchema):
    kinds: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    filters: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §37, §77–§85 — reports
# ---------------------------------------------------------------------------
class ReportResponse(BaseSchema):
    kind: str
    window_days: int
    project_id: str
    environment_id: Optional[str] = None
    generated_at: datetime
    sections: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)


class PostmortemResponse(BaseSchema):
    incident_id: str
    title: str
    generated_at: datetime
    sections: dict[str, Any] = Field(default_factory=dict)
    narrative: Optional[str] = None
    narrative_provider: Optional[str] = None
    narrative_unavailable_reason: Optional[str] = None
    unknowns: list[str] = Field(default_factory=list)
    follow_up_actions: list[dict[str, Any]] = Field(default_factory=list)
    note: str


class ImprovementPlanResponse(BaseSchema):
    window_days: int
    items: list[dict[str, Any]] = Field(default_factory=list)
    criteria: str
    note: str
    truncated: bool = False


# ---------------------------------------------------------------------------
# §51–§53 — integrations
# ---------------------------------------------------------------------------
class IntegrationRegistryResponse(BaseSchema):
    providers: dict[str, Any] = Field(default_factory=dict)
    note: str


class WebhookRequirementsResponse(BaseSchema):
    headers: dict[str, str] = Field(default_factory=dict)
    requirements: list[str] = Field(default_factory=list)
    rejections: list[str] = Field(default_factory=list)


class WebhookReceiptResponse(BaseSchema):
    accepted: bool
    event_type: Optional[str] = None
    delivery_id: Optional[str] = None
    fingerprint: Optional[str] = None
    idempotent_replay: bool = False
    reason: Optional[str] = None
    code: Optional[str] = None
    payload_summary: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "ActivityItem",
    "ActivityResponse",
    "AssistantAnswerResponse",
    "AssistantQuestionRequest",
    "CaseDetailResponse",
    "CaseListResponse",
    "CaseStatusChangeRequest",
    "CaseSummary",
    "CatalogEntryResponse",
    "CatalogListResponse",
    "ChangeFailureRateResponse",
    "ChangeListResponse",
    "CitationItem",
    "ComponentStateItem",
    "ConfigurationResponse",
    "ConfigurationRollbackRequest",
    "ConfigurationUpdateRequest",
    "ConfigurationVersionItem",
    "ContextResponse",
    "ContextSnapshotResponse",
    "DataQualityIssueItem",
    "DataQualityResponse",
    "DataQualityStatusRequest",
    "DependencyHealthResponse",
    "DependencyItem",
    "EnvironmentComparisonResponse",
    "ErrorBudgetResponse",
    "FeatureFlagsResponse",
    "ImprovementPlanResponse",
    "IntegrationRegistryResponse",
    "NotificationAckRequest",
    "NotificationItem",
    "NotificationListResponse",
    "OverviewResponse",
    "OwnershipRequest",
    "PlatformErrorResponse",
    "PlatformHealthResponse",
    "PostmortemResponse",
    "ReadinessResponse",
    "ReportResponse",
    "SearchHelpResponse",
    "SearchHitItem",
    "SearchResponse",
    "SloCreateRequest",
    "SloEvaluationResponse",
    "SloItem",
    "SloOverviewResponse",
    "StateCounts",
    "StateHistoryResponse",
    "StateTransitionItem",
    "StoryResponse",
    "SubsystemHealthItem",
    "SystemHealthSummary",
    "SystemStateResponse",
    "TimelineEntryItem",
    "WebhookReceiptResponse",
    "WebhookRequirementsResponse",
]
