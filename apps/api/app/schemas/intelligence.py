"""ARGUS Reliability Intelligence Schemas (Phase 10 §51–§61).

One rule shapes every response here: **a claim is never separated from the
evidence that backs it.** A knowledge item carries its ``sample_count``,
``coverage``, ``confidence``, ``algorithm`` and ``limitations``; a recommendation
carries its ``knowledge_ids``, ``experience_ids``, ``current_evidence``,
``limitations`` and the ``policy_note`` that says what Phase 9 would require.

That is why these schemas are wide rather than tidy. A narrower response would be
easier to render and would make it possible to show "restart works 81% of the
time" with the sample size dropped — which is exactly what §15 forbids.

Search responses carry ``citations`` and ``warnings``: an answer that cited a row
which no longer exists reports it rather than rendering a dead reference (§50).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import Field

from app.schemas.base import BaseSchema


# ---------------------------------------------------------------------------
# Knowledge (§53)
# ---------------------------------------------------------------------------


class KnowledgeItem(BaseSchema):
    """One learned pattern, with the §7 facts that qualify it."""

    id: uuid.UUID
    knowledge_type: str
    status: str
    scope: str
    component_id: Optional[uuid.UUID] = None
    environment_id: Optional[uuid.UUID] = None
    title: str
    description: str
    feature_signature: str
    sample_count: int
    success_count: Optional[int] = None
    support_strength: Optional[float] = None
    coverage_start: Optional[datetime] = None
    coverage_end: Optional[datetime] = None
    confidence: str
    algorithm: str
    algorithm_version: str
    feature_schema_version: str
    validation: Optional[dict[str, Any]] = None
    limitations: list[str] = Field(default_factory=list)
    version: int
    supersedes_knowledge_id: Optional[uuid.UUID] = None
    reviewed_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None
    review_reason: Optional[str] = None
    last_confirmed_at: Optional[datetime] = None
    sources: list[dict[str, Any]] = Field(default_factory=list)
    experience_ids: list[str] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class KnowledgeVersionItem(BaseSchema):
    id: uuid.UUID
    version: int
    status: str
    confidence: str
    sample_count: int
    snapshot: dict[str, Any]
    note: Optional[str] = None
    learning_run_id: Optional[uuid.UUID] = None
    activated_at: Optional[datetime] = None
    deactivated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class KnowledgeReviewItem(BaseSchema):
    id: uuid.UUID
    decision: str
    reviewer: str
    reason: Optional[str] = None
    knowledge_version: int
    created_at: Optional[datetime] = None


class KnowledgeListResponse(BaseSchema):
    items: list[KnowledgeItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class KnowledgeVersionListResponse(BaseSchema):
    """The §26 version ledger: one row per revision, newest first."""

    items: list[KnowledgeVersionItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class KnowledgeDetailResponse(BaseSchema):
    knowledge: KnowledgeItem
    versions: list[KnowledgeVersionItem] = Field(default_factory=list)
    reviews: list[KnowledgeReviewItem] = Field(default_factory=list)
    related: list[KnowledgeItem] = Field(default_factory=list)
    experiences: list["ExperienceItem"] = Field(default_factory=list)


class KnowledgeReviewRequest(BaseSchema):
    """A human decision about a pattern (§72)."""

    decision: str = Field(
        ..., description="APPROVE | REJECT | REQUEST_MORE_EVIDENCE | DEPRECATE"
    )
    reviewer: str = Field(..., min_length=1, max_length=255)
    #: §72, §78. Required, not optional: an unexplained activation is knowledge
    #: nobody can review later, and the reviewer who would object never sees it.
    #: The client already refuses to submit one without a reason; the API cannot
    #: depend on a client for its own governance.
    reason: str = Field(..., min_length=3, max_length=4000)


# ---------------------------------------------------------------------------
# Experiences (§54)
# ---------------------------------------------------------------------------


class ExperienceItem(BaseSchema):
    id: uuid.UUID
    project_id: uuid.UUID
    incident_id: Optional[uuid.UUID] = None
    environment_id: Optional[uuid.UUID] = None
    primary_component_id: Optional[uuid.UUID] = None
    remediation_action_id: Optional[uuid.UUID] = None
    start_time: datetime
    end_time: datetime
    recovery_seconds: Optional[int] = None
    outcome: str
    data_quality: str
    provenance: str
    component_ids: list[str] = Field(default_factory=list)
    failure_signature: dict[str, Any]
    failure_label: str
    failure_fingerprint: str
    resolution_signature: Optional[dict[str, Any]] = None
    resolution_label: Optional[str] = None
    learning_run_id: Optional[uuid.UUID] = None


class ExperienceTimelineEntry(BaseSchema):
    stage: str
    at: Optional[str] = None
    detail: Optional[str] = None


class ExperienceIncidentRef(BaseSchema):
    id: uuid.UUID
    title: str
    status: str
    severity: str


class ExperienceComponentRef(BaseSchema):
    id: uuid.UUID
    name: str
    #: The Phase 0 component category, when the caller resolved one. Optional
    #: because the same shape is reused for references built from stored ids.
    category: Optional[str] = None


class ExperienceRemediationRef(BaseSchema):
    id: uuid.UUID
    action_type: str
    status: str
    outcome: Optional[str] = None


class ExperienceListResponse(BaseSchema):
    items: list[ExperienceItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class ExperienceDetailResponse(BaseSchema):
    experience: ExperienceItem
    failure_signature: dict[str, Any]
    resolution_signature: Optional[dict[str, Any]] = None
    incident: Optional[ExperienceIncidentRef] = None
    component: Optional[ExperienceComponentRef] = None
    remediation: Optional[ExperienceRemediationRef] = None
    timeline: list[ExperienceTimelineEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Recommendations (§57)
# ---------------------------------------------------------------------------


class RecommendationItem(BaseSchema):
    id: uuid.UUID
    recommendation_type: str
    status: str
    title: str
    rationale: str
    confidence: str
    component_id: Optional[uuid.UUID] = None
    environment_id: Optional[uuid.UUID] = None
    incident_id: Optional[uuid.UUID] = None
    forecast_id: Optional[uuid.UUID] = None
    knowledge_ids: list[str] = Field(default_factory=list)
    experience_ids: list[str] = Field(default_factory=list)
    current_evidence: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    ranking: Optional[dict[str, Any]] = None
    historical: Optional[dict[str, Any]] = None
    policy_note: Optional[str] = None
    decision: Optional[dict[str, Any]] = None
    outcome: Optional[dict[str, Any]] = None
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class RecommendationOutcomeItem(BaseSchema):
    id: uuid.UUID
    verdict: str
    detail: Optional[dict[str, Any]] = None
    recorded_at: datetime
    recorded_by: Optional[str] = None


class RecommendationListResponse(BaseSchema):
    items: list[RecommendationItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class RecommendationDetailResponse(BaseSchema):
    recommendation: RecommendationItem
    outcomes: list[RecommendationOutcomeItem] = Field(default_factory=list)
    knowledge: list[KnowledgeItem] = Field(default_factory=list)
    experiences: list[ExperienceItem] = Field(default_factory=list)


class RecommendationDecisionRequest(BaseSchema):
    decision: str = Field(..., description="ACCEPTED | DISMISSED")
    actor: str = Field(..., min_length=1, max_length=255)
    reason: Optional[str] = Field(None, max_length=4000)


class RecommendationOutcomeRequest(BaseSchema):
    """What actually happened after a decision (§43, §81)."""

    verdict: str = Field(
        ..., description="EFFECTIVE | INEFFECTIVE | REGRESSION_CAUSING | INCONCLUSIVE"
    )
    recorded_by: str = Field(..., min_length=1, max_length=255)
    detail: Optional[dict[str, Any]] = None
    remediation_action_id: Optional[uuid.UUID] = None


# ---------------------------------------------------------------------------
# Component profiles (§58)
# ---------------------------------------------------------------------------


class ComponentProfileItem(BaseSchema):
    id: uuid.UUID
    component_id: uuid.UUID
    window_days: int
    computed_at: datetime
    incident_count: int
    anomaly_count: int
    remediation_count: int
    rollback_count: int
    regression_count: int
    mean_recovery_seconds: Optional[float] = None
    forecast_outcome_count: int
    forecast_true_positive_count: int
    chronic_signal: bool
    chronic_reasons: list[str] = Field(default_factory=list)
    breakdown: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Learned relationships (§23, §24)
#
# Declared before the profile schemas that embed them: a forward reference
# resolved at class-creation time would make the module import order load-bearing.
# ---------------------------------------------------------------------------


class LearnedRelationshipItem(BaseSchema):
    """A historically observed component relationship, with its support.

    ``directed`` is the load-bearing field: an undirected observation must not
    be drawn as an arrow. ``is_dependency`` is present and permanently false so
    that a client cannot render these as ``graph_edges`` without contradicting
    the payload it was given (§24).
    """

    id: uuid.UUID
    project_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    source_component_id: uuid.UUID
    source_component_name: str
    target_component_id: uuid.UUID
    target_component_name: str
    kind: str
    directed: bool
    status: str
    sample_count: int
    supporting_count: Optional[int] = None
    support_strength: Optional[float] = None
    confidence: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    provenance: str
    algorithm: str
    algorithm_version: str
    feature_schema_version: str
    coverage_start: Optional[datetime] = None
    coverage_end: Optional[datetime] = None
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    learning_run_id: Optional[uuid.UUID] = None
    is_dependency: bool = False
    disclaimer: str


class RelationshipListResponse(BaseSchema):
    items: list[LearnedRelationshipItem] = Field(default_factory=list)
    total: int
    page: int
    page_size: int
    total_pages: int = 0
    #: §24. Stated once for the whole response, so a list view cannot omit it.
    relationship_note: str
    limitations: list[str] = Field(default_factory=list)


class ComponentProfileResponse(BaseSchema):
    component: ExperienceComponentRef
    profiles: list[ComponentProfileItem] = Field(default_factory=list)
    knowledge: list[KnowledgeItem] = Field(default_factory=list)
    #: §23/§24. What history observed around this component. Separate from the
    #: structural graph on purpose: ``is_dependency`` is always false here.
    relationships: list[LearnedRelationshipItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Effectiveness (§15, §16, §45)
# ---------------------------------------------------------------------------


class EffectivenessBucketItem(BaseSchema):
    action_type: str
    dimension: str
    dimension_value: Optional[str] = None
    comparable: int
    successful: int
    partially_successful: int
    failed: int
    rolled_back: int
    unresolved: int
    mean_recovery_seconds: Optional[float] = None
    regression_count: int
    success_ratio: Optional[float] = None
    insufficient: bool
    minimum_samples: int
    experience_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class EffectivenessResponse(BaseSchema):
    buckets: list[EffectivenessBucketItem]
    headline: str
    observational_label: str
    limitations: list[str] = Field(default_factory=list)


class ActionComparisonResponse(BaseSchema):
    """An observational comparison between two actions (§45)."""

    label: str
    failure_pattern: Optional[str] = None
    actions: dict[str, Any]
    verdict: str
    summary: str
    limitations: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Learning runs (§59)
# ---------------------------------------------------------------------------


class LearningRunItem(BaseSchema):
    #: §23. Learned-relationship counts, part of the run ledger.
    relationships_created: int = 0
    relationships_updated: int = 0

    id: uuid.UUID
    project_id: Optional[uuid.UUID] = None
    status: str
    trigger: str
    data_cutoff: datetime
    last_processed_at: Optional[datetime] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    events_processed: int
    experiences_created: int
    experiences_updated: int
    patterns_discovered: int
    patterns_validated: int
    patterns_rejected: int
    knowledge_activated: int
    records_flagged: int
    algorithm_versions: dict[str, Any] = Field(default_factory=dict)
    error_summary: Optional[str] = None


class LearningEventItem(BaseSchema):
    id: uuid.UUID
    event_type: str
    subject_id: uuid.UUID
    occurred_at: datetime
    processed_at: Optional[datetime] = None
    unprocessable_reason: Optional[str] = None
    provenance: str


class LearningRunListResponse(BaseSchema):
    items: list[LearningRunItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class LearningRunDetailResponse(BaseSchema):
    run: LearningRunItem
    events: list[LearningEventItem] = Field(default_factory=list)


class LearningRunRequest(BaseSchema):
    """Trigger a run by hand (§63)."""

    project_id: uuid.UUID
    trigger: str = Field("manual", max_length=30)
    cutoff: Optional[datetime] = None
    lookback_days: Optional[int] = Field(None, ge=1, le=3650)
    generate_recommendations: bool = True


class LearningRunSummaryResponse(BaseSchema):
    run_id: Optional[str] = None
    status: str
    projects: list[str] = Field(default_factory=list)
    events_processed: int
    experiences_created: int
    experiences_updated: int
    experiences_flagged: int
    patterns_discovered: int
    patterns_validated: int
    patterns_rejected: int
    knowledge_created: int
    knowledge_updated: int
    knowledge_activated: int
    knowledge_deprecated: int
    recommendations_created: int
    recommendations_expired: int
    #: §23. What the relationship stage produced, so a run that learned nothing
    #: about component relationships is visible rather than implied.
    relationships_created: int = 0
    relationships_updated: int = 0
    relationships_stale: int = 0
    skipped_reasons: list[str] = Field(default_factory=list)
    unprocessable: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Search (§46–§50)
# ---------------------------------------------------------------------------


class SearchCitationItem(BaseSchema):
    type: str
    id: str
    label: Optional[str] = None


class SearchResponse(BaseSchema):
    question: str
    intent: str
    answer: str
    evidence_available: bool
    citations: list[SearchCitationItem] = Field(default_factory=list)
    knowledge: list[dict[str, Any]] = Field(default_factory=list)
    experiences: list[dict[str, Any]] = Field(default_factory=list)
    effectiveness: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Dashboard and metrics (§52, §80–§82)
# ---------------------------------------------------------------------------


class DashboardResponse(BaseSchema):
    knowledge_by_status: dict[str, int] = Field(default_factory=dict)
    knowledge_by_type: dict[str, int] = Field(default_factory=dict)
    active_knowledge: int
    validated_knowledge: int
    candidate_patterns: int
    stale_knowledge: int
    rejected_patterns: int
    recently_learned: list[KnowledgeItem] = Field(default_factory=list)
    experiences: int
    open_recommendations: int
    pending_events: int
    chronic_components: int
    last_run: Optional[LearningRunItem] = None


class LearningMetricsResponse(BaseSchema):
    #: §80. `relationships_undirected` is called out so a co-failure count is
    #: never read as a directed propagation count.
    relationships_active: int = 0
    relationships_stale: int = 0
    relationships_undirected: int = 0

    learning_runs: int
    learning_failures: int
    events_total: int
    events_pending: int
    experiences: int
    experiences_poor_quality: int
    knowledge_validated_or_active: int
    knowledge_candidates: int
    knowledge_rejected: int
    knowledge_stale: int
    pattern_validation_rate: Optional[float] = None
    recommendations_by_status: dict[str, int] = Field(default_factory=dict)
    recommendations_decided: int
    recommendation_success_rate: Optional[float] = None


class IntelligenceHealthResponse(BaseSchema):
    """§80. Is the learning layer itself working?"""

    learning_enabled: bool
    sweep_enabled: bool
    auto_activation_enabled: bool
    include_ai_generated: bool
    pending_events: int
    last_run_status: Optional[str] = None
    last_run_at: Optional[datetime] = None
    knowledge_stale_after_days: int
    minimum_samples: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Controls (§63, §76)
# ---------------------------------------------------------------------------


class EventHookResponse(BaseSchema):
    project_id: Optional[uuid.UUID] = None
    enabled_event_types: list[str] = Field(default_factory=list)
    trusted_provenance: list[str] = Field(default_factory=list)
    updated_by: Optional[str] = None


class EventHookUpdateRequest(BaseSchema):
    project_id: Optional[uuid.UUID] = None
    enabled_event_types: Optional[list[str]] = None
    trusted_provenance: Optional[list[str]] = None
    updated_by: Optional[str] = Field(None, max_length=255)


class SweepResponse(BaseSchema):
    projects_considered: int
    projects_run: int
    events_consumed: int
    knowledge_deprecated: int
    recommendations_expired: int
    runs: list[dict[str, Any]] = Field(default_factory=list)
    paused: bool
    disabled: bool
    errors: list[str] = Field(default_factory=list)


KnowledgeDetailResponse.model_rebuild()

__all__ = [
    "ActionComparisonResponse",
    "ComponentProfileItem",
    "ComponentProfileResponse",
    "DashboardResponse",
    "EffectivenessBucketItem",
    "EffectivenessResponse",
    "EventHookResponse",
    "EventHookUpdateRequest",
    "ExperienceDetailResponse",
    "ExperienceItem",
    "ExperienceListResponse",
    "IntelligenceHealthResponse",
    "KnowledgeDetailResponse",
    "KnowledgeItem",
    "KnowledgeListResponse",
    "KnowledgeReviewItem",
    "KnowledgeVersionListResponse",
    "KnowledgeReviewRequest",
    "KnowledgeVersionItem",
    "LearningEventItem",
    "LearningMetricsResponse",
    "LearningRunDetailResponse",
    "LearningRunItem",
    "LearningRunListResponse",
    "LearningRunRequest",
    "LearningRunSummaryResponse",
    "RecommendationDecisionRequest",
    "RecommendationDetailResponse",
    "RecommendationItem",
    "RecommendationListResponse",
    "RecommendationOutcomeItem",
    "RecommendationOutcomeRequest",
    "SearchCitationItem",
    "SearchResponse",
    "SweepResponse",
]
