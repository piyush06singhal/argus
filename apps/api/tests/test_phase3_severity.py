"""Phase 3 — explainable severity (§7) and deterministic fingerprints (§16, §25)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models.anomaly import AnomalySeverity, AnomalyType
from app.services.anomaly_severity import (
    SeveritySignals,
    compute_severity,
    downgrade_severity,
    max_severity,
    severity_at_least,
)
from app.services.fingerprints import (
    anomaly_fingerprint,
    anomaly_fingerprint_material,
    bucket_start,
    incident_fingerprint,
)

NOW = datetime(2026, 9, 19, 14, 22, tzinfo=timezone.utc)


class TestMagnitudeFloor:
    def test_small_deviation_stays_at_base(self) -> None:
        decision = compute_severity(
            SeveritySignals(base=AnomalySeverity.LOW, deviation_relative=0.1)
        )
        assert decision.severity is AnomalySeverity.LOW

    def test_two_x_deviation_is_high(self) -> None:
        decision = compute_severity(
            SeveritySignals(base=AnomalySeverity.LOW, deviation_relative=2.5)
        )
        assert decision.severity is AnomalySeverity.HIGH
        assert any("2x" in r or "deviation" in r for r in decision.reasons)

    def test_five_x_deviation_is_critical(self) -> None:
        decision = compute_severity(
            SeveritySignals(base=AnomalySeverity.LOW, deviation_relative=5.5)
        )
        assert decision.severity is AnomalySeverity.CRITICAL

    def test_high_z_score(self) -> None:
        decision = compute_severity(
            SeveritySignals(base=AnomalySeverity.LOW, z_score=4.5)
        )
        assert decision.severity is AnomalySeverity.HIGH

    def test_error_rate_floor(self) -> None:
        decision = compute_severity(
            SeveritySignals(base=AnomalySeverity.LOW, error_rate=0.3)
        )
        assert decision.severity is AnomalySeverity.CRITICAL

    def test_ratio_floor(self) -> None:
        decision = compute_severity(
            SeveritySignals(base=AnomalySeverity.LOW, ratio=3.5)
        )
        assert decision.severity is AnomalySeverity.HIGH

    def test_magnitude_never_lowers_base(self) -> None:
        decision = compute_severity(
            SeveritySignals(base=AnomalySeverity.CRITICAL, deviation_relative=0.01)
        )
        assert decision.severity is AnomalySeverity.CRITICAL


class TestEscalations:
    def test_critical_component_escalates(self) -> None:
        escalated = compute_severity(
            SeveritySignals(
                base=AnomalySeverity.MEDIUM, component_criticality="CRITICAL"
            )
        )
        assert escalated.severity is AnomalySeverity.HIGH
        assert any("criticality" in r for r in escalated.reasons)

    def test_long_persistence_escalates(self) -> None:
        decision = compute_severity(
            SeveritySignals(base=AnomalySeverity.MEDIUM, duration_seconds=2000.0)
        )
        assert decision.severity is AnomalySeverity.HIGH

    def test_medium_persistence_sets_high_floor(self) -> None:
        decision = compute_severity(
            SeveritySignals(base=AnomalySeverity.LOW, duration_seconds=400.0)
        )
        assert decision.severity is AnomalySeverity.HIGH

    def test_blast_radius_escalates(self) -> None:
        decision = compute_severity(
            SeveritySignals(base=AnomalySeverity.MEDIUM, affected_downstream=7)
        )
        assert decision.severity is AnomalySeverity.HIGH
        assert any("downstream" in r for r in decision.reasons)

    def test_capped_at_critical(self) -> None:
        decision = compute_severity(
            SeveritySignals(
                base=AnomalySeverity.CRITICAL,
                deviation_relative=9.0,
                duration_seconds=9999.0,
                component_criticality="CRITICAL",
                affected_downstream=50,
            )
        )
        assert decision.severity is AnomalySeverity.CRITICAL


class TestExplainability:
    def test_reasons_always_present(self) -> None:
        decision = compute_severity(SeveritySignals(base=AnomalySeverity.LOW))
        assert decision.reasons  # never an unexplained severity

    def test_factors_recorded(self) -> None:
        decision = compute_severity(
            SeveritySignals(base=AnomalySeverity.MEDIUM, error_rate=0.2)
        )
        assert decision.factors["error_rate"] == 0.2
        assert decision.factors["base"] == "MEDIUM"

    def test_escalated_flag(self) -> None:
        plain = compute_severity(SeveritySignals(base=AnomalySeverity.LOW))
        raised = compute_severity(
            SeveritySignals(base=AnomalySeverity.LOW, deviation_relative=3.0)
        )
        assert raised.escalated is True
        assert isinstance(plain.escalated, bool)


class TestSeverityHelpers:
    def test_max_severity(self) -> None:
        assert (
            max_severity(AnomalySeverity.LOW, AnomalySeverity.HIGH)
            is AnomalySeverity.HIGH
        )
        assert (
            max_severity(AnomalySeverity.CRITICAL, AnomalySeverity.MEDIUM)
            is AnomalySeverity.CRITICAL
        )

    def test_severity_at_least(self) -> None:
        assert (
            severity_at_least(AnomalySeverity.LOW, AnomalySeverity.MEDIUM)
            is AnomalySeverity.MEDIUM
        )
        assert (
            severity_at_least(AnomalySeverity.CRITICAL, AnomalySeverity.LOW)
            is AnomalySeverity.CRITICAL
        )

    def test_downgrade_is_explicit_and_bounded(self) -> None:
        assert downgrade_severity(AnomalySeverity.HIGH) is AnomalySeverity.MEDIUM
        assert downgrade_severity(AnomalySeverity.LOW) is AnomalySeverity.LOW
        assert (
            downgrade_severity(AnomalySeverity.CRITICAL, levels=5)
            is AnomalySeverity.LOW
        )


class TestAnomalyFingerprint:
    def test_stable_across_calls(self) -> None:
        args = dict(
            project_id="p1",
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            discriminator="http.checkout",
            environment_id="production",
            component_id="checkout-service",
        )
        assert anomaly_fingerprint(**args) == anomaly_fingerprint(**args)
        assert len(anomaly_fingerprint(**args)) == 64

    def test_different_metric_differs(self) -> None:
        base = dict(project_id="p1", anomaly_type=AnomalyType.LATENCY_SPIKE)
        a = anomaly_fingerprint(**base, discriminator="http.checkout")
        b = anomaly_fingerprint(**base, discriminator="http.payment")
        assert a != b

    def test_different_component_differs(self) -> None:
        base = dict(project_id="p1", anomaly_type=AnomalyType.LATENCY_SPIKE)
        a = anomaly_fingerprint(**base, component_id="checkout")
        b = anomaly_fingerprint(**base, component_id="inventory")
        assert a != b

    def test_project_isolation(self) -> None:
        base = dict(anomaly_type=AnomalyType.LATENCY_SPIKE, discriminator="m")
        assert anomaly_fingerprint(**base, project_id="p1") != anomaly_fingerprint(
            **base, project_id="p2"
        )

    def test_material_is_readable(self) -> None:
        material = anomaly_fingerprint_material(
            project_id="proj",
            anomaly_type=AnomalyType.LATENCY_SPIKE,
            discriminator="http.checkout",
            environment_id="production",
            component_id="checkout-service",
        )
        assert "production" in material
        assert "latency_spike" in material
        assert "http.checkout" in material


class TestIncidentFingerprint:
    def test_stable_and_related_order_independent(self) -> None:
        base = dict(
            project_id="p1",
            primary_component_id="checkout",
            dominant_anomaly_type=AnomalyType.LATENCY_SPIKE,
            environment_id="production",
            time_bucket=1000,
        )
        a = incident_fingerprint(**base, related_component_ids=["b", "a", "c"])
        b = incident_fingerprint(**base, related_component_ids=["c", "a", "b"])
        assert a == b

    def test_time_bucket_separates_recurrences(self) -> None:
        base = dict(
            project_id="p1",
            primary_component_id="checkout",
            dominant_anomaly_type=AnomalyType.LATENCY_SPIKE,
        )
        assert incident_fingerprint(**base, time_bucket=1000) != incident_fingerprint(
            **base, time_bucket=2000
        )

    def test_primary_component_matters(self) -> None:
        base = dict(
            project_id="p1",
            dominant_anomaly_type=AnomalyType.LATENCY_SPIKE,
            time_bucket=1000,
        )
        assert incident_fingerprint(
            **base, primary_component_id="a"
        ) != incident_fingerprint(**base, primary_component_id="b")


class TestBucket:
    def test_bucket_is_floored(self) -> None:
        start = bucket_start(NOW, bucket_seconds=3600)
        assert start % 3600 == 0
        assert start <= int(NOW.timestamp())

    def test_same_bucket_for_close_times(self) -> None:
        a = bucket_start(NOW)
        b = bucket_start(NOW + timedelta(minutes=5))
        assert a == b

    def test_different_bucket_for_distant_times(self) -> None:
        assert bucket_start(NOW) != bucket_start(NOW + timedelta(hours=3))

    def test_naive_datetime_treated_as_utc(self) -> None:
        naive = datetime(2026, 9, 19, 14, 22)
        assert bucket_start(naive) % 3600 == 0
