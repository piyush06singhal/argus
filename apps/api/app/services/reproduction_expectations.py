"""ARGUS Expected Behaviour (Phase 5 §26).

The bridge between "what the incident showed" and "what the sandbox must show
for the reproduction to mean anything".

This module exists as its own unit for one reason: **expectations must never be
hand-written.** A reproduction compared against an invented expectation proves
nothing, so the only constructor here is
:meth:`ExpectedBehavior.from_evidence`, which takes the incident's own observed
signals and copies them. Every expectation therefore carries its provenance
(which incident signals produced it), and the API can show an engineer exactly
where each expected value came from.

Matching is deliberately coarse — component, kind, and a threshold — because a
narrower match would reward exact reproduction of noise rather than of failure
shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

#: What kind of behaviour a component is expected to exhibit.
KIND_ERROR = "ERROR"
KIND_SLOW = "SLOW"
KIND_DEGRADED = "HEALTH_DEGRADED"
KIND_OK = "OK"

VALID_KINDS = frozenset({KIND_ERROR, KIND_SLOW, KIND_DEGRADED, KIND_OK})


@dataclass(frozen=True)
class ExpectedSignal:
    """One expected observation, always traceable to incident evidence."""

    component: str
    kind: str
    operation: Optional[str] = None
    #: For ``SLOW``: the latency the incident actually exhibited, in ms.
    threshold_ms: Optional[float] = None
    #: Human-readable provenance, e.g. "anomaly 'Checkout P95 Latency'".
    evidence: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "kind": self.kind,
            "operation": self.operation,
            "threshold_ms": self.threshold_ms,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ExpectedSignal":
        return cls(
            component=str(payload.get("component") or ""),
            kind=str(payload.get("kind") or KIND_ERROR),
            operation=payload.get("operation"),
            threshold_ms=(
                float(payload["threshold_ms"])
                if payload.get("threshold_ms") is not None
                else None
            ),
            evidence=payload.get("evidence"),
        )


@dataclass
class ExpectedBehavior:
    """What a successful reproduction of this incident should look like (§26).

    ``sequence`` is the order the *effects* are expected to appear in, derived
    from the incident's own ordering (datastore first, checkout last). It is the
    strongest single statement the phase makes about a reproduction, so the
    comparator treats a matching sequence as the headline result and a
    non-matching one as ``PARTIAL`` at best.
    """

    components: list[str] = field(default_factory=list)
    sequence: list[str] = field(default_factory=list)
    signals: list[ExpectedSignal] = field(default_factory=list)
    window_seconds: int = 300
    #: Provenance: incident ids, analysis ids and rule names behind this shape.
    provenance: dict[str, Any] = field(default_factory=dict)
    #: Signals the planner wanted but could not derive; surfaced to the UI so
    #: an empty expectation list is never mistaken for "nothing to check".
    underivable: list[str] = field(default_factory=list)

    # -- serialization ---------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "components": list(self.components),
            "sequence": list(self.sequence),
            "signals": [signal.as_dict() for signal in self.signals],
            "window_seconds": self.window_seconds,
            "provenance": dict(self.provenance),
            "underivable": list(self.underivable),
        }

    @classmethod
    def from_dict(cls, payload: Optional[dict[str, Any]]) -> "ExpectedBehavior":
        if not payload:
            return cls()
        return cls(
            components=[str(item) for item in payload.get("components", [])],
            sequence=[str(item) for item in payload.get("sequence", [])],
            signals=[
                ExpectedSignal.from_dict(item) for item in payload.get("signals", [])
            ],
            window_seconds=int(payload.get("window_seconds") or 300),
            provenance=dict(payload.get("provenance") or {}),
            underivable=[str(item) for item in payload.get("underivable", [])],
        )

    # -- queries ---------------------------------------------------------
    def signals_for(self, component: str) -> list[ExpectedSignal]:
        return [item for item in self.signals if item.component == component]

    def signal_for(self, component: str, kind: str) -> Optional[ExpectedSignal]:
        for item in self.signals:
            if item.component == component and item.kind == kind:
                return item
        return None

    @property
    def is_empty(self) -> bool:
        return not self.signals and not self.sequence

    def summary(self) -> dict[str, Any]:
        return {
            "components": len(self.components),
            "signals": len(self.signals),
            "sequence_length": len(self.sequence),
            "window_seconds": self.window_seconds,
            "underivable": list(self.underivable),
        }

    # -- construction from evidence (§26) --------------------------------
    @classmethod
    def from_evidence(
        cls,
        *,
        components: Sequence[str],
        sequence: Sequence[str],
        error_components: Iterable[str] = (),
        slow_components: Iterable[tuple[str, float]] = (),
        degraded_components: Iterable[str] = (),
        window_seconds: int = 300,
        provenance: Optional[dict[str, Any]] = None,
        underivable: Optional[Sequence[str]] = None,
    ) -> "ExpectedBehavior":
        """Build expectations from signals the incident actually contained.

        Each argument is a *derived* input: ``error_components`` are components
        whose own telemetry showed errors, ``slow_components`` pairs a component
        with the latency it actually exhibited. Nothing is defaulted to a
        plausible-looking value.
        """
        signals: list[ExpectedSignal] = []
        for component in dict.fromkeys(error_components):
            signals.append(
                ExpectedSignal(
                    component=component,
                    kind=KIND_ERROR,
                    evidence="incident telemetry recorded errors on this component",
                )
            )
        for component, threshold in slow_components:
            signals.append(
                ExpectedSignal(
                    component=component,
                    kind=KIND_SLOW,
                    threshold_ms=float(threshold),
                    evidence=(
                        "incident telemetry recorded latency of "
                        f"{float(threshold):.0f}ms on this component"
                    ),
                )
            )
        for component in dict.fromkeys(degraded_components):
            signals.append(
                ExpectedSignal(
                    component=component,
                    kind=KIND_DEGRADED,
                    evidence="incident telemetry recorded a degraded health state",
                )
            )
        return cls(
            components=list(dict.fromkeys(components)),
            sequence=list(dict.fromkeys(sequence)),
            signals=signals,
            window_seconds=window_seconds,
            provenance=dict(provenance or {}),
            underivable=list(underivable or []),
        )


class ExpectationMatcher:
    """Judges captured observations against expected behaviour.

    Kept separate from :class:`ExpectedBehavior` so the *classification* rule
    lives in one place: capture uses it to label observations, and the
    comparator uses the same rule to decide whether an expected signal was
    actually satisfied. Two implementations would eventually disagree, and the
    disagreement would show up as an unexplained mismatch.
    """

    def __init__(self, expected: ExpectedBehavior) -> None:
        self._expected = expected

    #: Observation status values, kept as plain strings to avoid importing the
    #: model enum into a pure-logic module.
    EXPECTED = "EXPECTED"
    UNEXPECTED = "UNEXPECTED"
    NEUTRAL = "NEUTRAL"

    def classify(
        self,
        *,
        component: str,
        error: bool,
        duration_ms: Optional[float] = None,
        degraded: bool = False,
    ) -> tuple[str, bool, Optional[float]]:
        """Return ``(status, matched_expected, expected_value)``."""
        expected_error = self._expected.signal_for(component, KIND_ERROR)
        if error:
            if expected_error is not None:
                return self.EXPECTED, True, None
            return self.UNEXPECTED, False, None

        expected_slow = self._expected.signal_for(component, KIND_SLOW)
        if expected_slow is not None and duration_ms is not None:
            threshold = expected_slow.threshold_ms or 0.0
            # The sandbox is not the incident's host: an expectation is met at
            # 80% of the observed latency, so a slightly faster sandbox still
            # counts as reproducing the degradation that mattered.
            if duration_ms >= threshold * 0.8:
                return self.EXPECTED, True, threshold

        if degraded and self._expected.signal_for(component, KIND_DEGRADED) is not None:
            return self.EXPECTED, True, None
        return self.NEUTRAL, False, None

    def satisfied(
        self,
        matching: Sequence[Any],
        *,
        component: str,
        kind: str,
    ) -> bool:
        """Whether at least one captured observation satisfied an expectation.

        ``matching`` is any sequence of objects exposing ``component_name``,
        ``error``, ``duration_ms`` and ``attributes`` (an observation row).
        """
        expectation = self._expected.signal_for(component, kind)
        if expectation is None:
            return False
        for observation in matching:
            if getattr(observation, "component_name", None) != component:
                continue
            if kind == KIND_ERROR and getattr(observation, "error", False):
                return True
            if kind == KIND_SLOW:
                threshold = expectation.threshold_ms or 0.0
                duration = getattr(observation, "duration_ms", None)
                if duration is not None and float(duration) >= threshold * 0.8:
                    return True
            if kind == KIND_DEGRADED:
                attributes = getattr(observation, "attributes", None) or {}
                if attributes.get("state") not in (None, "UP"):
                    return True
        return False

    def missing(self, observations: Sequence[Any]) -> list[ExpectedSignal]:
        """Expectations that no captured observation satisfied (§26).

        A missing expectation is reported as an explicit observation by the
        capture service rather than being simply absent, because silent absence
        is indistinguishable from a clean run.
        """
        missing: list[ExpectedSignal] = []
        for expectation in self._expected.signals:
            if not self.satisfied(
                observations, component=expectation.component, kind=expectation.kind
            ):
                missing.append(expectation)
        return missing


__all__ = [
    "KIND_DEGRADED",
    "KIND_ERROR",
    "KIND_OK",
    "KIND_SLOW",
    "VALID_KINDS",
    "ExpectationMatcher",
    "ExpectedBehavior",
    "ExpectedSignal",
]
