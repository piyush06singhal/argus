"""Phase 3 — deterministic incident summary tests (§33–§34).

The summary is the human-facing surface, so these tests pin three properties:
it uses stored values, it is reproducible, and it never claims causation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.incident_summary import (
    NON_CAUSALITY_DISCLAIMER,
    SummaryAnomaly,
    SummaryComponent,
    SummaryTimelineItem,
    build_incident_summary,
)

NOW = datetime(2026, 9, 19, 14, 22, tzinfo=timezone.utc)


def _anomaly(**overrides) -> SummaryAnomaly:
    payload = {
        "anomaly_type": "LATENCY_SPIKE",
        "severity": "HIGH",
        "component_name": "checkout-service",
        "metric_name": "http.checkout.latency.p95",
        "pattern_template": None,
        "observed_value": 890.0,
        "expected_value": 220.0,
        "deviation": 3.0,
        "detected_at": NOW,
    }
    payload.update(overrides)
    return SummaryAnomaly(**payload)


class TestSummaryContent:
    def test_includes_component_and_values(self) -> None:
        text, generated = build_incident_summary(
            title="Checkout latency incident",
            severity="HIGH",
            status="OPEN",
            detected_at=NOW,
            primary_component_name="checkout-service",
            anomalies=[_anomaly()],
        )
        assert "checkout-service" in text
        assert "890" in text
        assert "220" in text
        assert "4.0x expected" in text  # 890/220, computed from raw values
        assert "anomalies" in generated

    def test_ratio_uses_raw_numbers_not_formatted_strings(self) -> None:
        """Regression: dividing by a formatted string produced nonsense."""
        text, _ = build_incident_summary(
            title="t",
            severity="HIGH",
            status="OPEN",
            detected_at=NOW,
            anomalies=[_anomaly(observed_value=1000.0, expected_value=250.0)],
        )
        assert "4.0x expected" in text

    def test_zero_expected_omits_ratio(self) -> None:
        text, _ = build_incident_summary(
            title="t",
            severity="LOW",
            status="OPEN",
            detected_at=NOW,
            anomalies=[_anomaly(observed_value=5.0, expected_value=0.0)],
        )
        assert "x expected" not in text

    def test_missing_values_do_not_get_invented(self) -> None:
        text, _ = build_incident_summary(
            title="t",
            severity="LOW",
            status="OPEN",
            detected_at=NOW,
            anomalies=[
                _anomaly(
                    observed_value=None,
                    expected_value=None,
                    metric_name=None,
                    pattern_template="boom",
                )
            ],
        )
        assert "boom" in text
        assert "None" not in text
        assert "nan" not in text.lower()

    def test_suppressed_anomaly_is_marked_not_hidden(self) -> None:
        text, _ = build_incident_summary(
            title="t",
            severity="LOW",
            status="OPEN",
            detected_at=NOW,
            anomalies=[_anomaly(suppressed=True)],
        )
        assert "suppressed" in text
        assert "recorded, not hidden" in text

    def test_no_anomalies_states_it_plainly(self) -> None:
        text, generated = build_incident_summary(
            title="t", severity="LOW", status="OPEN", detected_at=NOW
        )
        assert "No anomaly evidence" in text
        assert "anomalies" not in generated


class TestNonCausality:
    def test_disclaimer_always_present(self) -> None:
        for anomalies in ([], [_anomaly()]):
            text, _ = build_incident_summary(
                title="t",
                severity="HIGH",
                status="OPEN",
                detected_at=NOW,
                anomalies=anomalies,
            )
            assert NON_CAUSALITY_DISCLAIMER in text

    def test_deployment_wording_is_context_only(self) -> None:
        text, generated = build_incident_summary(
            title="t",
            severity="HIGH",
            status="OPEN",
            detected_at=NOW,
            anomalies=[_anomaly()],
            deployments=[
                SummaryTimelineItem(
                    label="Deployment deploy-1 (version 1.2.3)",
                    occurred_at=NOW - timedelta(minutes=2),
                    seconds_from_first_anomaly=120.0,
                )
            ],
        )
        assert "2 minutes before the first observed anomaly" in text
        assert "does not establish" in text
        assert "deployments" in generated

    def test_configuration_wording_is_context_only(self) -> None:
        text, generated = build_incident_summary(
            title="t",
            severity="HIGH",
            status="OPEN",
            detected_at=NOW,
            config_changes=[
                SummaryTimelineItem(
                    label="Configuration change cfg-1",
                    occurred_at=NOW - timedelta(seconds=45),
                    seconds_from_first_anomaly=45.0,
                )
            ],
        )
        assert "temporal context only" in text
        assert "configuration_changes" in generated


class TestDeterminism:
    def test_identical_inputs_identical_output(self) -> None:
        kwargs = dict(
            title="Checkout latency incident",
            severity="HIGH",
            status="OPEN",
            detected_at=NOW,
            primary_component_name="checkout-service",
            anomalies=[_anomaly(), _anomaly(detected_at=NOW + timedelta(seconds=30))],
            components=[
                SummaryComponent(
                    name="checkout-service", classification="DIRECTLY_OBSERVED"
                )
            ],
            deployments=[
                SummaryTimelineItem(
                    label="Deployment deploy-1",
                    occurred_at=NOW - timedelta(minutes=2),
                    seconds_from_first_anomaly=120.0,
                )
            ],
        )
        first, _ = build_incident_summary(**kwargs)
        second, _ = build_incident_summary(**kwargs)
        assert first == second

    def test_anomalies_are_ordered_chronologically(self) -> None:
        text, _ = build_incident_summary(
            title="t",
            severity="HIGH",
            status="OPEN",
            detected_at=NOW,
            anomalies=[
                _anomaly(metric_name="second", detected_at=NOW + timedelta(seconds=30)),
                _anomaly(metric_name="first", detected_at=NOW),
            ],
        )
        assert text.index("first") < text.index("second")

    def test_affected_components_section(self) -> None:
        text, generated = build_incident_summary(
            title="t",
            severity="HIGH",
            status="OPEN",
            detected_at=NOW,
            anomalies=[_anomaly()],
            components=[
                SummaryComponent(
                    name="checkout-service", classification="DIRECTLY_OBSERVED"
                ),
                SummaryComponent(
                    name="inventory-service", classification="DOWNSTREAM_CONTEXT"
                ),
            ],
        )
        assert "DIRECTLY_OBSERVED" in text
        assert "DOWNSTREAM_CONTEXT" in text
        assert "components" in generated
