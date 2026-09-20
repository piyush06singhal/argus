"""Phase 3 — schema invariants: rule validation, window guards, round-trips.

The point of these tests is that a *misconfigured* rule or window is rejected at
the API boundary. A rule whose condition has nothing to evaluate would silently
detect nothing, which is worse than an error.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.models.anomaly import (
    AnomalySeverity,
    AnomalySource,
    AnomalyStatus,
    AnomalyType,
    RuleCondition,
)
from app.models.incident import (
    EvidenceType,
    IncidentSeverity,
    IncidentStatus,
    TimelineEventType,
)
from app.schemas.anomaly import (
    AffectedComponentResponse,
    AnomalyDetailResponse,
    AnomalyRuleCreate,
    AnomalyRuleList,
    AnomalyRuleResponse,
    AnomalyRuleUpdate,
    AnomalyStatusUpdate,
    AnomalySuppressionCreate,
    AnomalySuppressionResponse,
    ConfigurationContextItem,
    DeploymentContextItem,
    IncidentGraphContext,
    IncidentGraphEdge,
    IncidentGraphNode,
    IncidentSummaryResponse,
    MaintenanceWindowCreate,
    MaintenanceWindowResponse,
)
from app.schemas.incident import (
    EvidenceCreate,
    EvidenceResponse,
    IncidentCreate,
    IncidentResponse,
    LifecycleActionRequest,
    TimelineEventCreate,
    TimelineEventResponse,
)

NOW = datetime(2026, 9, 19, 14, 22, tzinfo=timezone.utc)


def _u() -> uuid.UUID:
    return uuid.uuid4()


def _base_rule(**overrides) -> dict:
    payload = {
        "project_id": _u(),
        "name": "Checkout P95 Latency",
        "anomaly_type": AnomalyType.LATENCY_SPIKE.value,
        "condition": RuleCondition.BASELINE_DEVIATION.value,
        "metric_name": "http.checkout.latency.p95",
        "multiplier": 2.5,
        "severity": AnomalySeverity.HIGH.value,
    }
    payload.update(overrides)
    return payload


class TestRuleValidation:
    def test_valid_rule(self) -> None:
        rule = AnomalyRuleCreate(**_base_rule())
        assert rule.name == "Checkout P95 Latency"
        assert rule.severity == "HIGH"
        assert rule.baseline_strategy == "ROLLING"
        assert rule.min_samples == 5
        assert rule.enabled is True

    def test_threshold_rule_requires_threshold(self) -> None:
        with pytest.raises(ValidationError, match="threshold"):
            AnomalyRuleCreate(
                **_base_rule(condition=RuleCondition.THRESHOLD.value, multiplier=None)
            )

    def test_deviation_rule_requires_multiplier(self) -> None:
        with pytest.raises(ValidationError, match="multiplier"):
            AnomalyRuleCreate(**_base_rule(multiplier=None))

    def test_z_score_rule_requires_z_threshold(self) -> None:
        with pytest.raises(ValidationError, match="z_threshold"):
            AnomalyRuleCreate(
                **_base_rule(condition=RuleCondition.Z_SCORE.value, multiplier=None)
            )

    def test_metric_rules_require_metric_name(self) -> None:
        with pytest.raises(ValidationError, match="metric_name"):
            AnomalyRuleCreate(**_base_rule(metric_name=None))

    def test_zero_multiplier_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AnomalyRuleCreate(**_base_rule(multiplier=0))

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AnomalyRuleCreate(**_base_rule(not_a_field="x"))

    def test_update_all_optional(self) -> None:
        update = AnomalyRuleUpdate(enabled=False)
        assert update.model_dump(exclude_unset=True) == {"enabled": False}


class TestWindowValidation:
    def test_suppression_requires_ordered_window(self) -> None:
        with pytest.raises(ValidationError, match="ends_at"):
            AnomalySuppressionCreate(
                project_id=_u(),
                reason="maintenance",
                starts_at=NOW,
                ends_at=NOW - timedelta(minutes=1),
            )

    def test_suppression_open_ended_allowed(self) -> None:
        s = AnomalySuppressionCreate(
            project_id=_u(), reason="known noisy metric", starts_at=NOW
        )
        assert s.ends_at is None
        assert s.enabled is True

    def test_maintenance_window_requires_positive_duration(self) -> None:
        with pytest.raises(ValidationError, match="ends_at"):
            MaintenanceWindowCreate(
                project_id=_u(),
                name="DB maintenance",
                starts_at=NOW,
                ends_at=NOW,
            )

    def test_maintenance_window_must_do_something(self) -> None:
        with pytest.raises(ValidationError, match="suppress"):
            MaintenanceWindowCreate(
                project_id=_u(),
                name="no-op window",
                starts_at=NOW,
                ends_at=NOW + timedelta(hours=1),
                suppress_anomalies=False,
                downgrade_severity=False,
            )

    def test_maintenance_window_valid(self) -> None:
        w = MaintenanceWindowCreate(
            project_id=_u(),
            name="DB maintenance",
            starts_at=NOW,
            ends_at=NOW + timedelta(hours=1),
        )
        assert w.suppress_anomalies is True
        assert w.downgrade_severity is False


class TestResponseSerialization:
    def test_rule_response_uses_metadata_alias(self) -> None:
        rule = AnomalyRuleResponse(
            id=_u(),
            created_at=NOW,
            updated_at=NOW,
            **_base_rule(),
            metadata_={"note": "x"},
        )
        dumped = rule.model_dump(by_alias=True)
        assert dumped["metadata"] == {"note": "x"}
        assert "metadata_" not in dumped

    def test_rule_list_is_paginated(self) -> None:
        listed = AnomalyRuleList.create([], total=0, page=1, page_size=20)
        assert listed.total_pages == 0
        assert listed.items == []

    def test_suppression_response(self) -> None:
        resp = AnomalySuppressionResponse(
            id=_u(),
            created_at=NOW,
            updated_at=NOW,
            project_id=_u(),
            reason="maintenance",
            starts_at=NOW,
            enabled=True,
        )
        assert resp.anomaly_type is None
        assert resp.metric_name is None

    def test_maintenance_window_response(self) -> None:
        resp = MaintenanceWindowResponse(
            id=_u(),
            created_at=NOW,
            updated_at=NOW,
            project_id=_u(),
            name="w",
            starts_at=NOW,
            ends_at=NOW + timedelta(hours=1),
            suppress_anomalies=True,
            downgrade_severity=False,
            enabled=True,
        )
        assert resp.name == "w"

    def test_anomaly_detail_explanation_optional(self) -> None:
        detail = AnomalyDetailResponse(
            id=_u(),
            created_at=NOW,
            updated_at=NOW,
            project_id=_u(),
            anomaly_type=AnomalyType.LATENCY_SPIKE.value,
            severity=AnomalySeverity.HIGH.value,
            status=AnomalyStatus.DETECTED.value,
            source=AnomalySource.METRIC.value,
            fingerprint="fp",
            observation_count=1,
            detected_at=NOW,
        )
        assert detail.explanation is None
        assert detail.observations == []


class TestIncidentSchemasBackwardCompatible:
    """Phase 0/1 incident payloads must keep validating unchanged."""

    def test_legacy_incident_create_still_valid(self) -> None:
        inc = IncidentCreate(
            project_id=_u(),
            title="Legacy incident",
            severity=IncidentSeverity.MEDIUM.value,
            detected_at=NOW,
        )
        assert inc.status == IncidentStatus.OPEN.value
        assert inc.fingerprint is None
        assert inc.correlation_rationale is None

    def test_incident_create_with_phase3_context(self) -> None:
        inc = IncidentCreate(
            project_id=_u(),
            title="Checkout latency incident",
            severity=IncidentSeverity.HIGH.value,
            detected_at=NOW,
            fingerprint="inc-fp-1",
            primary_component_id=_u(),
            correlation_rationale={"shared_components": ["checkout"]},
        )
        assert inc.fingerprint == "inc-fp-1"

    def test_incident_response_exposes_new_fields(self) -> None:
        resp = IncidentResponse(
            id=_u(),
            created_at=NOW,
            updated_at=NOW,
            project_id=_u(),
            title="t",
            severity=IncidentSeverity.HIGH.value,
            status=IncidentStatus.ACKNOWLEDGED.value,
            detected_at=NOW,
            acknowledged_at=NOW,
        )
        assert resp.status == "ACKNOWLEDGED"
        assert resp.status_changed_by is None

    def test_legacy_evidence_payload_still_valid(self) -> None:
        ev = EvidenceCreate(
            evidence_type=EvidenceType.LOG.value, source_id="log-1", timestamp=NOW
        )
        assert ev.component_id is None
        assert ev.relevance_reason is None

    def test_structured_evidence_payload(self) -> None:
        ev = EvidenceCreate(
            evidence_type=EvidenceType.METRIC.value,
            source_id="metric-1",
            timestamp=NOW,
            observed_value="890.0",
            expected_value="220.0",
            severity=IncidentSeverity.HIGH.value,
            confidence=0.9,
            provenance="rolling_baseline",
            relevance_reason="same component and time window",
        )
        assert ev.confidence == 0.9
        assert ev.relevance_reason == "same component and time window"

    def test_evidence_response_roundtrip(self) -> None:
        resp = EvidenceResponse(
            id=_u(),
            created_at=NOW,
            updated_at=NOW,
            incident_id=_u(),
            evidence_type=EvidenceType.ANOMALY.value,
            source_id="anomaly-1",
            timestamp=NOW,
        )
        assert resp.evidence_type == "ANOMALY"


class TestTimelineSchemas:
    def test_timeline_event_response_context_flag(self) -> None:
        ev = TimelineEventResponse(
            id=_u(),
            created_at=NOW,
            updated_at=NOW,
            incident_id=_u(),
            project_id=_u(),
            event_type=TimelineEventType.DEPLOYMENT_OCCURRED.value,
            occurred_at=NOW,
            title="Deployment 2 minutes before first anomaly",
            is_context_only=True,
        )
        assert ev.is_context_only is True

    def test_manual_note_defaults_to_note_kind(self) -> None:
        note = TimelineEventCreate(occurred_at=NOW, title="Investigated cache")
        assert note.event_type == TimelineEventType.NOTE.value

    def test_lifecycle_action_actor_optional(self) -> None:
        action = LifecycleActionRequest()
        assert action.actor is None

    def test_anomaly_status_update(self) -> None:
        update = AnomalyStatusUpdate(actor="oncall")
        assert update.actor == "oncall"


class TestIncidentIntelligenceReadModels:
    def test_affected_component_classification(self) -> None:
        comp = AffectedComponentResponse(
            component_id=_u(),
            name="checkout-service",
            classification="DIRECTLY_OBSERVED",
            reason="has a directly detected anomaly",
            anomaly_count=3,
            severity=AnomalySeverity.HIGH.value,
        )
        assert comp.classification == "DIRECTLY_OBSERVED"

    def test_graph_context_carries_disclaimer(self) -> None:
        ctx = IncidentGraphContext(
            nodes=[
                IncidentGraphNode(
                    node_id=_u(),
                    name="checkout",
                    node_type="SERVICE",
                    classification="DIRECTLY_OBSERVED",
                )
            ],
            edges=[
                IncidentGraphEdge(
                    source_node_id=_u(),
                    target_node_id=_u(),
                    edge_type="DEPENDS_ON",
                    source="CONFIGURATION",
                )
            ],
        )
        # The UI relies on this wording — related is not causal.
        assert "not implied to be causes" in ctx.disclaimer

    def test_deployment_context_is_context_only(self) -> None:
        item = DeploymentContextItem(
            deployment_event_id=_u(),
            deployment_id="deploy-1",
            deployed_at=NOW,
            seconds_before_first_anomaly=120.0,
        )
        assert item.is_context_only is True

    def test_configuration_context_is_context_only(self) -> None:
        item = ConfigurationContextItem(
            configuration_event_id=_u(), changed_at=NOW, summary="feature flag off"
        )
        assert item.is_context_only is True

    def test_incident_summary_response(self) -> None:
        summary = IncidentSummaryResponse(
            incident_id=_u(),
            title="Checkout latency incident",
            severity=IncidentSeverity.HIGH.value,
            status=IncidentStatus.OPEN.value,
            detected_at=NOW,
            text="Checkout service experienced elevated p95 latency…",
            generated_from=["anomalies", "deployments"],
        )
        assert "generated_from" in summary.model_dump()
