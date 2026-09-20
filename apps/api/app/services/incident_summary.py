"""ARGUS Deterministic Incident Summary (Phase 3 §33–§34).

Builds a human-readable incident summary **from stored evidence only**. It never
invents a value, never guesses a cause, and always states the temporal-context
boundary explicitly.

The function is pure: give it records, get text back. That keeps the summary
reproducible — two runs over the same incident produce byte-identical output —
and makes it testable without a database.

An LLM may *rephrase* this text later (``IncidentSummarizer``), but it may not
replace it: the structured evidence stays the source of truth and the system is
fully functional with no model at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

#: Appended to (or embedded in) every summary. The boundary is not optional.
NON_CAUSALITY_DISCLAIMER = (
    "This summary describes observed evidence and temporal context. "
    "It does not establish what caused the incident."
)


@dataclass(frozen=True)
class SummaryAnomaly:
    """Minimal anomaly facts needed for a summary line."""

    anomaly_type: str
    severity: str
    component_name: Optional[str]
    metric_name: Optional[str]
    pattern_template: Optional[str]
    observed_value: Optional[float]
    expected_value: Optional[float]
    deviation: Optional[float]
    detected_at: datetime
    suppressed: bool = False


@dataclass(frozen=True)
class SummaryComponent:
    """A component in the observed blast radius."""

    name: Optional[str]
    classification: str


@dataclass(frozen=True)
class SummaryTimelineItem:
    """A deployment or configuration change near the incident (context only)."""

    label: str
    occurred_at: datetime
    seconds_from_first_anomaly: Optional[float] = None


def _format_value(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    if abs(value) >= 1000 or (value != 0 and abs(value) < 0.01):
        return f"{value:.3g}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_delta(seconds: Optional[float]) -> str:
    if seconds is None:
        return "shortly"
    magnitude = abs(seconds)
    if magnitude < 90:
        return f"{magnitude:.0f} seconds"
    if magnitude < 5400:
        return f"{magnitude / 60:.0f} minutes"
    return f"{magnitude / 3600:.1f} hours"


def _relative_phrase(seconds: Optional[float], *, before: bool) -> str:
    direction = "before" if before else "after"
    if seconds is None:
        return f"{direction} the first observed anomaly"
    return f"{_format_delta(seconds)} {direction} the first observed anomaly"


def build_incident_summary(
    *,
    title: str,
    severity: str,
    status: str,
    detected_at: datetime,
    resolved_at: Optional[datetime] = None,
    primary_component_name: Optional[str] = None,
    anomalies: Sequence[SummaryAnomaly] = (),
    components: Sequence[SummaryComponent] = (),
    deployments: Sequence[SummaryTimelineItem] = (),
    config_changes: Sequence[SummaryTimelineItem] = (),
    truncated: bool = False,
) -> tuple[str, list[str]]:
    """Return ``(summary_text, generated_from)`` for an incident.

    ``generated_from`` names the evidence classes actually used, so a reader can
    tell which sections were backed by data rather than omitted.
    """
    generated_from: list[str] = []
    lines: list[str] = []

    subject = primary_component_name or "The affected system"
    lead = (
        f"{subject} was associated with {len(anomalies)} correlated "
        f"anomal{'y' if len(anomalies) == 1 else 'ies'}, detected at "
        f"{detected_at.isoformat()} (severity {severity}, status {status})."
    )
    if anomalies:
        generated_from.append("anomalies")
    lines.append(lead)
    lines.append(f"Incident: {title}")

    if anomalies:
        lines.append("")
        lines.append("Observed anomalies:")
        for anomaly in sorted(anomalies, key=lambda a: a.detected_at):
            label = anomaly.component_name or "unattributed component"
            detail = (
                anomaly.metric_name or anomaly.pattern_template or anomaly.anomaly_type
            )
            observed = _format_value(anomaly.observed_value)
            expected = _format_value(anomaly.expected_value)
            raw_observed = anomaly.observed_value
            raw_expected = anomaly.expected_value
            if observed is not None and expected is not None:
                # 4x degradation reads better than a floating-point percentage.
                # The ratio uses the RAW values — dividing by the formatted
                # string would be a type error at best and a wrong number at
                # worst.
                if (
                    raw_expected is not None
                    and raw_expected != 0
                    and raw_observed is not None
                ):
                    ratio = raw_observed / raw_expected
                    change = f" ({ratio:.1f}x expected)"
                else:
                    change = ""
                line = (
                    f"- {label}: {detail} observed {observed} vs expected "
                    f"{expected}{change} [{anomaly.severity}]"
                )
            elif observed is not None:
                line = f"- {label}: {detail} observed {observed} [{anomaly.severity}]"
            else:
                line = f"- {label}: {detail} [{anomaly.severity}]"
            if anomaly.suppressed:
                line += " (suppressed by policy — recorded, not hidden)"
            lines.append(line)

    if components:
        generated_from.append("components")
        lines.append("")
        lines.append("Affected components (observed blast radius):")
        for component in components:
            name = component.name or "unattributed"
            lines.append(f"- {name}: {component.classification}")

    if deployments:
        generated_from.append("deployments")
        lines.append("")
        for item in sorted(deployments, key=lambda d: d.occurred_at):
            lines.append(
                f"{item.label} occurred "
                f"{_relative_phrase(item.seconds_from_first_anomaly, before=(item.seconds_from_first_anomaly or 0) >= 0)}."
            )
        lines.append(
            "Deployment timing is temporal context only and does not establish "
            "that the deployment caused the incident."
        )

    if config_changes:
        generated_from.append("configuration_changes")
        lines.append("")
        for item in sorted(config_changes, key=lambda c: c.occurred_at):
            lines.append(
                f"{item.label} was recorded "
                f"{_relative_phrase(item.seconds_from_first_anomaly, before=(item.seconds_from_first_anomaly or 0) >= 0)}."
            )
        lines.append(
            "Configuration timing is temporal context only and does not "
            "establish that the change caused the incident."
        )

    if not anomalies:
        lines.append("")
        lines.append("No anomaly evidence is attached to this incident yet.")

    if truncated:
        lines.append("")
        lines.append(
            "Some evidence was omitted from this summary to keep it bounded; "
            "the full evidence set is available via the incident API."
        )

    lines.append("")
    lines.append(NON_CAUSALITY_DISCLAIMER)
    return "\n".join(lines), generated_from


def summarizer_interface_note() -> str:
    """Documentation hook for the optional AI summarizer (§34)."""
    return (
        "An optional provider-neutral IncidentSummarizer may rephrase this text, "
        "but structured evidence remains the source of truth and no model is "
        "required for Phase 3 to function."
    )


__all__ = [
    "NON_CAUSALITY_DISCLAIMER",
    "SummaryAnomaly",
    "SummaryComponent",
    "SummaryTimelineItem",
    "build_incident_summary",
    "summarizer_interface_note",
]
