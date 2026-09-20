"""ARGUS Explainable Severity (Phase 3 §7).

Severity is computed by **deterministic, documented rules**, never by a model
and never as an arbitrary score. Every decision carries the list of reasons
that produced it, so any severity shown in the UI can be justified line by line.

The algorithm is intentionally two-stage:

1. **Magnitude floor** — how far the observation is from expectation sets a
   minimum severity (a 9x latency ratio is at least HIGH regardless of
   anything else).
2. **Context escalations** — criticality, persistence, and blast radius each
   raise severity by one level, recording why. Nothing can lower severity here;
   downgrades are explicit (maintenance windows, §43) and are recorded on the
   anomaly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.models.anomaly import AnomalySeverity

SEVERITY_RANK: dict[AnomalySeverity, int] = {
    AnomalySeverity.LOW: 0,
    AnomalySeverity.MEDIUM: 1,
    AnomalySeverity.HIGH: 2,
    AnomalySeverity.CRITICAL: 3,
}
RANK_TO_SEVERITY: dict[int, AnomalySeverity] = {v: k for k, v in SEVERITY_RANK.items()}

#: A component of CRITICAL criticality escalates severity by one level.
_ESCALATING_CRITICALITY = {"CRITICAL"}
#: Persistence beyond this many seconds is itself an escalation.
_PERSISTENCE_ESCALATE_SECONDS = 1800.0
#: Persistence beyond this many seconds sets a HIGH floor.
_PERSISTENCE_FLOOR_SECONDS = 300.0
#: This many downstream components affected is an escalation.
_BLAST_RADIUS_ESCALATE = 5


def _rank(severity: AnomalySeverity | str) -> int:
    try:
        return SEVERITY_RANK[AnomalySeverity(severity)]
    except (ValueError, KeyError):
        return 0


def _from_rank(rank: int) -> AnomalySeverity:
    return RANK_TO_SEVERITY[max(0, min(3, rank))]


def max_severity(a: AnomalySeverity, b: AnomalySeverity) -> AnomalySeverity:
    """Return the higher of two severities."""
    return a if _rank(a) >= _rank(b) else b


def severity_at_least(a: AnomalySeverity, floor: AnomalySeverity) -> AnomalySeverity:
    """Raise ``a`` to at least ``floor`` (never lowers)."""
    return max_severity(a, floor)


def downgrade_severity(severity: AnomalySeverity, levels: int = 1) -> AnomalySeverity:
    """Explicitly lower severity (maintenance windows). Recorded, never silent."""
    return _from_rank(_rank(severity) - max(0, int(levels)))


@dataclass(frozen=True)
class SeveritySignals:
    """The deterministic inputs to a severity decision."""

    base: AnomalySeverity = AnomalySeverity.MEDIUM
    deviation_relative: Optional[float] = None
    z_score: Optional[float] = None
    error_rate: Optional[float] = None
    ratio: Optional[float] = None
    duration_seconds: Optional[float] = None
    component_criticality: Optional[str] = None
    affected_downstream: int = 0


@dataclass(frozen=True)
class SeverityDecision:
    """A severity plus the reasons that produced it."""

    severity: AnomalySeverity
    reasons: list[str] = field(default_factory=list)
    factors: dict = field(default_factory=dict)

    @property
    def escalated(self) -> bool:
        return len(self.reasons) > 0


def _magnitude_floor(
    signals: SeveritySignals,
) -> tuple[Optional[AnomalySeverity], list[str]]:
    """Highest severity implied purely by how large the deviation is."""
    floor: Optional[AnomalySeverity] = None
    reasons: list[str] = []

    def consider(candidate: AnomalySeverity, reason: str) -> None:
        nonlocal floor
        if floor is None or _rank(candidate) > _rank(floor):
            floor = candidate
            reasons.append(reason)

    if signals.deviation_relative is not None:
        magnitude = abs(signals.deviation_relative)
        if magnitude >= 5.0:
            consider(AnomalySeverity.CRITICAL, f"deviation {magnitude:.2f}x >= 5x")
        elif magnitude >= 2.0:
            consider(AnomalySeverity.HIGH, f"deviation {magnitude:.2f}x >= 2x")
        elif magnitude >= 0.5:
            consider(AnomalySeverity.MEDIUM, f"deviation {magnitude:.2f}x >= 0.5x")

    if signals.z_score is not None:
        magnitude = abs(signals.z_score)
        if magnitude >= 6.0:
            consider(AnomalySeverity.CRITICAL, f"z-score {magnitude:.2f} >= 6")
        elif magnitude >= 4.0:
            consider(AnomalySeverity.HIGH, f"z-score {magnitude:.2f} >= 4")
        elif magnitude >= 3.0:
            consider(AnomalySeverity.MEDIUM, f"z-score {magnitude:.2f} >= 3")

    if signals.error_rate is not None:
        rate = signals.error_rate
        if rate >= 0.25:
            consider(AnomalySeverity.CRITICAL, f"error rate {rate:.2%} >= 25%")
        elif rate >= 0.10:
            consider(AnomalySeverity.HIGH, f"error rate {rate:.2%} >= 10%")
        elif rate >= 0.03:
            consider(AnomalySeverity.MEDIUM, f"error rate {rate:.2%} >= 3%")

    if signals.ratio is not None:
        if signals.ratio >= 5.0:
            consider(AnomalySeverity.CRITICAL, f"ratio {signals.ratio:.2f}x >= 5x")
        elif signals.ratio >= 3.0:
            consider(AnomalySeverity.HIGH, f"ratio {signals.ratio:.2f}x >= 3x")
        elif signals.ratio >= 2.0:
            consider(AnomalySeverity.MEDIUM, f"ratio {signals.ratio:.2f}x >= 2x")

    return floor, reasons


def compute_severity(signals: SeveritySignals) -> SeverityDecision:
    """Compute an explainable severity from deterministic signals (§7)."""
    reasons: list[str] = []
    rank = _rank(signals.base)

    floor, floor_reasons = _magnitude_floor(signals)
    if floor is not None:
        if _rank(floor) > rank:
            rank = _rank(floor)
            reasons.extend(floor_reasons)
            reasons.append(f"raised to {_from_rank(rank).value} by magnitude")
        else:
            reasons.append(f"magnitude within {signals.base.value} baseline")

    if signals.duration_seconds is not None:
        if signals.duration_seconds >= _PERSISTENCE_ESCALATE_SECONDS:
            rank += 1
            reasons.append(
                f"persisted {signals.duration_seconds:.0f}s "
                f">= {_PERSISTENCE_ESCALATE_SECONDS:.0f}s (escalated)"
            )
        elif signals.duration_seconds >= _PERSISTENCE_FLOOR_SECONDS:
            if rank < _rank(AnomalySeverity.HIGH):
                rank = _rank(AnomalySeverity.HIGH)
                reasons.append(
                    f"persisted {signals.duration_seconds:.0f}s "
                    f">= {_PERSISTENCE_FLOOR_SECONDS:.0f}s (HIGH floor)"
                )

    criticality = (signals.component_criticality or "").upper()
    if criticality in _ESCALATING_CRITICALITY:
        rank += 1
        reasons.append("component criticality CRITICAL (escalated)")

    if signals.affected_downstream >= _BLAST_RADIUS_ESCALATE:
        rank += 1
        reasons.append(
            f"{signals.affected_downstream} downstream components affected "
            f">= {_BLAST_RADIUS_ESCALATE} (escalated)"
        )

    severity = _from_rank(rank)
    if not reasons:
        reasons.append(f"base severity {signals.base.value} retained")

    return SeverityDecision(
        severity=severity,
        reasons=reasons,
        factors={
            "base": signals.base.value,
            "deviation_relative": signals.deviation_relative,
            "z_score": signals.z_score,
            "error_rate": signals.error_rate,
            "ratio": signals.ratio,
            "duration_seconds": signals.duration_seconds,
            "component_criticality": signals.component_criticality,
            "affected_downstream": signals.affected_downstream,
        },
    )


__all__ = [
    "SEVERITY_RANK",
    "RANK_TO_SEVERITY",
    "SeveritySignals",
    "SeverityDecision",
    "compute_severity",
    "max_severity",
    "severity_at_least",
    "downgrade_severity",
]
