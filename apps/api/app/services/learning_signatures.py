"""ARGUS Failure & Resolution Signatures (Phase 10 §9, §10, §31).

Two pure, deterministic value objects that turn a pile of evidence rows into a
*normalized* shape the learning pipeline can compare and mine:

* :class:`FailureSignature` — what a failure looked like (anomaly types, metric
  behaviour, affected components, dependency/deployment/resource context).
* :class:`ResolutionSignature` — what was done about it and what happened
  (action types, verification verdict, rollback, recovery bucket).

Rules that make the rest of the phase possible:

* **Pure functions only.** No session, no clock, no I/O. A signature computed
  twice from the same rows is byte-identical, which is what lets
  :func:`failure_fingerprint` be used as a grouping key and a dedup key (§64).
* **Bounded, never raw.** Every free-text-ish list is normalized (lowercased,
  trimmed, sorted, de-duplicated) and truncated to
  :data:`MAX_SIGNATURE_VALUES`. A signature is a *summary* of telemetry, never a
  copy of it — otherwise "don't include raw unlimited telemetry" would be a
  comment rather than a property.
* **Unknown is explicit.** A missing severity, metric or outcome becomes
  :data:`UNKNOWN` rather than being silently dropped, because a pattern mined
  from rows whose context was missing must not look identical to one mined from
  rows where the context was simply "nothing notable".
* **Schema versioned.** ``FEATURE_SCHEMA_VERSION`` is part of the fingerprint, so
  changing what counts as a feature invalidates old groupings instead of
  silently mixing two definitions in one bucket (§26).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

#: Bump when the normalized feature set changes meaning. Part of every
#: fingerprint and of the pattern identity, so old rows do not silently join new
#: buckets.
FEATURE_SCHEMA_VERSION = "1.0"

#: How many values of any one feature class a signature keeps. The cap is a
#: design decision: it keeps a "noisy" incident (300 anomalous metrics) from
#: being incomparable to a quiet one.
MAX_SIGNATURE_VALUES = 8

UNKNOWN = "unknown"

#: Metric name fragments → the behaviour class they belong to. Ordered: the
#: first match wins, so more specific fragments come first.
_METRIC_CLASSES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("error", "failure", "5xx", "exception", "exception_rate"), "error_rate"),
    (("latency", "duration", "response_time", "p95", "p99", "rt"), "latency"),
    (("throughput", "rps", "qps", "requests_per"), "throughput"),
    (("cpu", "memory", "disk", "iops", "heap", "gc_"), "resource"),
    (("queue", "backlog", "lag", "pending", "inflight"), "queue_depth"),
    (("timeout", "timed_out", "deadline"), "timeout"),
    (("saturation", "utilization", "utilisation"), "saturation"),
)

_BUCKET_EDGES: tuple[tuple[int, str], ...] = (
    (300, "IMMEDIATE"),  # under 5 minutes
    (1800, "SHORT"),  # under 30 minutes
    (7200, "MODERATE"),  # under 2 hours
    (86400, "LONG"),  # under a day
)

_ISO_RE = re.compile(r"[^a-z0-9_.:/-]+")


def _normalize_value(value: Any) -> str:
    """Lowercase, trim and collapse an evidence value into a stable token."""
    text = str(value).strip().lower()
    text = _ISO_RE.sub("_", text)
    text = text.strip("_")
    return text or UNKNOWN


def _normalize_values(values: Optional[Iterable[Any]]) -> tuple[str, ...]:
    """Normalize a list of evidence values deterministically.

    Sorting makes the result order-independent (the same evidence in a
    different order is the same signature), and the cap bounds it.
    """
    if not values:
        return ()
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        token = _normalize_value(value)
        seen.add(token)
    return tuple(sorted(seen)[:MAX_SIGNATURE_VALUES])


def classify_metric(metric_name: str) -> str:
    """Map a metric name onto a behaviour class for comparison.

    Deliberately coarse: ``http_request_duration_p99`` and
    ``checkout_latency_ms`` are the same *kind* of behaviour, and a similarity
    engine that cannot see that would only ever match identical metric names.
    """
    name = _normalize_value(metric_name)
    for fragments, label in _METRIC_CLASSES:
        if any(fragment in name for fragment in fragments):
            return label
    return "other"


def classify_direction(
    *, observed: Optional[float], expected: Optional[float], tolerance: float = 0.1
) -> str:
    """Whether an observed value rose, fell or held, relative to its baseline.

    A missing baseline is ``unknown`` rather than ``flat``: not knowing the
    expected value is not evidence that nothing moved.
    """
    if observed is None or expected is None:
        return UNKNOWN
    try:
        observed_f = float(observed)
        expected_f = float(expected)
    except (TypeError, ValueError):
        return UNKNOWN
    if expected_f == 0:
        if observed_f == 0:
            return "flat"
        return "up" if observed_f > 0 else "down"
    delta = (observed_f - expected_f) / abs(expected_f)
    if delta > tolerance:
        return "up"
    if delta < -tolerance:
        return "down"
    return "flat"


def metric_behavior(metric_name: str, direction: str) -> str:
    """The comparable token for one metric observation, e.g. ``error_rate:up``."""
    return f"{classify_metric(metric_name)}:{_normalize_value(direction)}"


def direction_suffix(direction: str) -> str:
    """The short human form used in feature-signature labels."""
    return {"up": "rising", "down": "falling", "flat": "steady"}.get(
        direction, "unknown"
    )


def recovery_bucket(seconds: Optional[int]) -> str:
    """Coarse time-to-recovery class (§10). ``UNKNOWN`` when unmeasurable."""
    if seconds is None:
        return UNKNOWN
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return UNKNOWN
    if value < 0:
        return UNKNOWN
    for edge, label in _BUCKET_EDGES:
        if value <= edge:
            return label
    return "EXTENDED"


def _canonical(
    *, kind: str, values: Sequence[str], version: str = FEATURE_SCHEMA_VERSION
) -> str:
    return f"{kind}|{version}|" + ",".join(values)


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FailureSignature:
    """A normalized description of a failure (§9).

    Every field is optional in the sense that it may be ``unknown``; none of
    them is free-form text. ``truncated`` records that some evidence was dropped
    by the cap, so a downstream reader can tell a quiet incident from a noisy
    one whose detail was clipped.
    """

    incident_kind: str = UNKNOWN
    severity: str = UNKNOWN
    anomaly_types: tuple[str, ...] = ()
    metric_behaviors: tuple[str, ...] = ()
    affected_components: tuple[str, ...] = ()
    dependency_conditions: tuple[str, ...] = ()
    deployment_context: tuple[str, ...] = ()
    resource_pressure: tuple[str, ...] = ()
    log_patterns: tuple[str, ...] = ()
    trace_patterns: tuple[str, ...] = ()
    health_state: str = UNKNOWN
    truncated: bool = False
    schema_version: str = FEATURE_SCHEMA_VERSION

    # -- serialization ----------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "incident_kind": self.incident_kind,
            "severity": self.severity,
            "anomaly_types": list(self.anomaly_types),
            "metric_behaviors": list(self.metric_behaviors),
            "affected_components": list(self.affected_components),
            "dependency_conditions": list(self.dependency_conditions),
            "deployment_context": list(self.deployment_context),
            "resource_pressure": list(self.resource_pressure),
            "log_patterns": list(self.log_patterns),
            "trace_patterns": list(self.trace_patterns),
            "health_state": self.health_state,
            "truncated": self.truncated,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "FailureSignature":
        """Rebuild a signature from stored JSON.

        Tolerant by design: an experience written by an older schema version is
        still readable, and the version it carries is preserved so the mismatch
        is visible rather than papered over.
        """
        if not data:
            return cls()
        return cls(
            incident_kind=str(data.get("incident_kind", UNKNOWN)),
            severity=str(data.get("severity", UNKNOWN)),
            anomaly_types=tuple(data.get("anomaly_types") or ()),
            metric_behaviors=tuple(data.get("metric_behaviors") or ()),
            affected_components=tuple(data.get("affected_components") or ()),
            dependency_conditions=tuple(data.get("dependency_conditions") or ()),
            deployment_context=tuple(data.get("deployment_context") or ()),
            resource_pressure=tuple(data.get("resource_pressure") or ()),
            log_patterns=tuple(data.get("log_patterns") or ()),
            trace_patterns=tuple(data.get("trace_patterns") or ()),
            health_state=str(data.get("health_state", UNKNOWN)),
            truncated=bool(data.get("truncated", False)),
            schema_version=str(data.get("schema_version", FEATURE_SCHEMA_VERSION)),
        )

    # -- identity ---------------------------------------------------------
    def feature_keys(self, *, include_components: bool = True) -> tuple[str, ...]:
        """The canonical token list used for fingerprints and labels.

        Component identity is optional because ``COMPONENT_SPECIFIC`` knowledge
        already names its component: including it again would make the same
        pattern fingerprint differently per component and defeat §65.
        """
        keys: list[str] = [f"kind:{self.incident_kind}", f"sev:{self.severity}"]
        keys += [f"anom:{value}" for value in self.anomaly_types]
        keys += [f"metric:{value}" for value in self.metric_behaviors]
        keys += [f"dep:{value}" for value in self.dependency_conditions]
        keys += [f"deploy:{value}" for value in self.deployment_context]
        keys += [f"res:{value}" for value in self.resource_pressure]
        keys += [f"log:{value}" for value in self.log_patterns]
        keys += [f"trace:{value}" for value in self.trace_patterns]
        keys += [f"health:{self.health_state}"]
        if include_components:
            keys += [f"comp:{value}" for value in self.affected_components]
        return tuple(keys)

    def fingerprint(self, *, include_components: bool = True) -> str:
        """Deterministic identity of this failure shape (§64, §65)."""
        return _digest(
            _canonical(
                kind="failure",
                values=self.feature_keys(include_components=include_components),
                version=self.schema_version,
            )
        )

    # -- presentation -----------------------------------------------------
    def label(self, *, parts: int = 4) -> str:
        """A short human-readable signature, e.g. ``latency rising + error_rate``.

        This is what ends up in ``feature_signature`` on a knowledge row — the
        pre-hash form that a person reads in the UI (§53).
        """
        tokens: list[str] = []
        if self.incident_kind != UNKNOWN:
            tokens.append(self.incident_kind)
        for behavior in self.metric_behaviors:
            metric, _, direction = behavior.partition(":")
            tokens.append(f"{metric}_{direction_suffix(direction)}")
        for value in self.anomaly_types:
            tokens.append(value)
        for value in self.dependency_conditions:
            tokens.append(f"dep_{value}")
        if not tokens:
            tokens.append(UNKNOWN)
        return "+".join(tokens[:parts])

    def describe(self) -> str:
        """A sentence a person can read without decoding tokens (§53)."""
        fragments: list[str] = []
        if self.anomaly_types:
            fragments.append("anomalies " + ", ".join(self.anomaly_types))
        if self.metric_behaviors:
            fragments.append("metric behaviour " + ", ".join(self.metric_behaviors))
        if self.dependency_conditions:
            fragments.append(
                "dependency state " + ", ".join(self.dependency_conditions)
            )
        if self.deployment_context:
            fragments.append("deployment context " + ", ".join(self.deployment_context))
        if self.resource_pressure:
            fragments.append("resource pressure " + ", ".join(self.resource_pressure))
        if not fragments:
            return "no distinguishing features were recorded"
        return "; ".join(fragments)


@dataclass(frozen=True)
class ResolutionSignature:
    """A normalized description of what was done and what happened (§10)."""

    action_types: tuple[str, ...] = ()
    outcome: str = UNKNOWN
    verification_verdict: str = UNKNOWN
    rollback_performed: bool = False
    patch_verified: bool = False
    feature_flag_change: bool = False
    restart: bool = False
    traffic_shift: bool = False
    scaling: bool = False
    configuration_change: bool = False
    recovery_bucket: str = UNKNOWN
    recovery_seconds: Optional[int] = None
    schema_version: str = FEATURE_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_types": list(self.action_types),
            "outcome": self.outcome,
            "verification_verdict": self.verification_verdict,
            "rollback_performed": self.rollback_performed,
            "patch_verified": self.patch_verified,
            "feature_flag_change": self.feature_flag_change,
            "restart": self.restart,
            "traffic_shift": self.traffic_shift,
            "scaling": self.scaling,
            "configuration_change": self.configuration_change,
            "recovery_bucket": self.recovery_bucket,
            "recovery_seconds": self.recovery_seconds,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "ResolutionSignature":
        if not data:
            return cls()
        return cls(
            action_types=tuple(data.get("action_types") or ()),
            outcome=str(data.get("outcome", UNKNOWN)),
            verification_verdict=str(data.get("verification_verdict", UNKNOWN)),
            rollback_performed=bool(data.get("rollback_performed", False)),
            patch_verified=bool(data.get("patch_verified", False)),
            feature_flag_change=bool(data.get("feature_flag_change", False)),
            restart=bool(data.get("restart", False)),
            traffic_shift=bool(data.get("traffic_shift", False)),
            scaling=bool(data.get("scaling", False)),
            configuration_change=bool(data.get("configuration_change", False)),
            recovery_bucket=str(data.get("recovery_bucket", UNKNOWN)),
            recovery_seconds=(
                int(data["recovery_seconds"])
                if data.get("recovery_seconds") is not None
                else None
            ),
            schema_version=str(data.get("schema_version", FEATURE_SCHEMA_VERSION)),
        )

    def feature_keys(self) -> tuple[str, ...]:
        keys = [f"action:{value}" for value in self.action_types]
        keys += [
            f"outcome:{self.outcome}",
            f"verify:{self.verification_verdict}",
            f"recovery:{self.recovery_bucket}",
        ]
        for name, flag in (
            ("rollback", self.rollback_performed),
            ("patch", self.patch_verified),
            ("flag", self.feature_flag_change),
            ("restart", self.restart),
            ("traffic", self.traffic_shift),
            ("scale", self.scaling),
            ("config", self.configuration_change),
        ):
            if flag:
                keys.append(f"did:{name}")
        return tuple(keys)

    def fingerprint(self) -> str:
        return _digest(
            _canonical(
                kind="resolution",
                values=self.feature_keys(),
                version=self.schema_version,
            )
        )

    def label(self, *, parts: int = 3) -> str:
        tokens: list[str] = [value for value in self.action_types]
        if self.outcome != UNKNOWN:
            tokens.append(self.outcome)
        if self.recovery_bucket != UNKNOWN:
            tokens.append(f"recovery_{self.recovery_bucket.lower()}")
        if not tokens:
            tokens.append(UNKNOWN)
        return "+".join(tokens[:parts])

    @property
    def succeeded(self) -> bool:
        """Whether the resolution is one ARGUS would call a success.

        ``EFFECTIVE`` or a ``VERIFIED`` verdict, and no rollback: a resolution
        that had to be reversed is not a success even if the system later
        recovered (§14, §15 — this feeds effectiveness, so it must be strict).
        """
        if self.rollback_performed:
            return False
        return self.outcome == "effective" or self.verification_verdict == "verified"


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def build_failure_signature(
    *,
    incident_kind: Optional[str] = None,
    severity: Optional[str] = None,
    anomaly_types: Optional[Iterable[Any]] = None,
    metric_observations: Optional[
        Iterable[Mapping[str, Any] | tuple[str, Any, Any]]
    ] = None,
    component_ids: Optional[Iterable[Any]] = None,
    dependency_conditions: Optional[Iterable[Any]] = None,
    deployment_context: Optional[Iterable[Any]] = None,
    resource_pressure: Optional[Iterable[Any]] = None,
    log_patterns: Optional[Iterable[Any]] = None,
    trace_patterns: Optional[Iterable[Any]] = None,
    health_state: Optional[str] = None,
) -> FailureSignature:
    """Assemble a :class:`FailureSignature` from normalized evidence.

    Accepts plain values or ORM objects' scalar attributes; the caller does the
    querying. ``metric_observations`` entries may be mappings with
    ``metric_name``/``observed_value``/``expected_value`` or
    ``(name, observed, expected)`` tuples.
    """
    anomaly_values = _normalize_values(anomaly_types)
    behaviors: list[str] = []
    for observation in metric_observations or ():
        if isinstance(observation, Mapping):
            name = observation.get("metric_name") or observation.get("name")
            observed = observation.get("observed_value", observation.get("observed"))
            expected = observation.get("expected_value", observation.get("expected"))
        else:
            name, observed, expected = observation
        if not name:
            continue
        direction = classify_direction(observed=observed, expected=expected)
        behaviors.append(metric_behavior(str(name), direction))

    dependency_values = _normalize_values(dependency_conditions)
    resource_values = _normalize_values(resource_pressure)
    #: Saturation is both a resource condition and a threshold state; keeping
    #: the token in the resource set makes "resource exhausted" comparable
    #: whether it arrived as an anomaly type or as a metric.
    for behavior in behaviors:
        metric, _, direction = behavior.partition(":")
        if metric in ("resource", "saturation") and direction == "up":
            resource_values = tuple(sorted(set(resource_values) | {"elevated"}))

    return FailureSignature(
        incident_kind=_normalize_value(incident_kind) if incident_kind else UNKNOWN,
        severity=_normalize_value(severity) if severity else UNKNOWN,
        anomaly_types=anomaly_values,
        metric_behaviors=_normalize_values(behaviors),
        affected_components=_normalize_values(component_ids),
        dependency_conditions=dependency_values,
        deployment_context=_normalize_values(deployment_context),
        resource_pressure=resource_values,
        log_patterns=_normalize_values(log_patterns),
        trace_patterns=_normalize_values(trace_patterns),
        health_state=_normalize_value(health_state) if health_state else UNKNOWN,
        truncated=_was_truncated(
            anomaly_types, metric_observations, component_ids, log_patterns
        ),
    )


def _was_truncated(*collections: Optional[Iterable[Any]]) -> bool:
    """Whether any evidence list exceeded the cap and will be clipped."""
    for values in collections:
        if values is None:
            continue
        try:
            if len(list(values)) > MAX_SIGNATURE_VALUES:  # type: ignore[arg-type]
                return True
        except TypeError:
            continue
    return False


#: Action types that imply each resolution flag. Kept as data so the mapping is
#: reviewable in one place rather than buried in a builder.
_ACTION_FLAGS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("RESTART_SERVICE", "RESTART_INSTANCE"), "restart"),
    (("DISABLE_FEATURE_FLAG", "ENABLE_FEATURE_FLAG"), "feature_flag_change"),
    (("ROUTE_TRAFFIC_TO_HEALTHY_INSTANCE",), "traffic_shift"),
    (("SCALE_SERVICE_WITHIN_LIMIT",), "scaling"),
    (
        ("ROLLBACK_DEPLOYMENT", "ROLLBACK_CONFIGURATION", "APPLY_VERIFIED_PATCH"),
        "configuration_change",
    ),
)


def build_resolution_signature(
    *,
    action_types: Optional[Iterable[Any]] = None,
    outcome: Optional[str] = None,
    verification_verdict: Optional[str] = None,
    rollback_performed: bool = False,
    patch_verified: bool = False,
    recovery_seconds: Optional[int] = None,
) -> ResolutionSignature:
    """Assemble a :class:`ResolutionSignature` from stored outcome rows."""
    normalized_actions = _normalize_values(action_types)
    upper_actions = {value.upper() for value in action_types or () if value}
    fields: dict[str, Any] = {name: False for _, name in _ACTION_FLAGS}
    for fragments, name in _ACTION_FLAGS:
        if any(action in fragments for action in upper_actions):
            fields[name] = True
    fields.update(
        action_types=normalized_actions,
        outcome=_normalize_value(outcome) if outcome else UNKNOWN,
        verification_verdict=(
            _normalize_value(verification_verdict) if verification_verdict else UNKNOWN
        ),
        rollback_performed=bool(rollback_performed),
        patch_verified=bool(patch_verified),
        recovery_bucket=recovery_bucket(recovery_seconds),
        recovery_seconds=recovery_seconds,
    )
    return ResolutionSignature(**fields)


def comparable_seconds(
    start: Optional[datetime], end: Optional[datetime]
) -> Optional[int]:
    """Whole seconds between two timestamps, or ``None`` when unusable.

    Naive timestamps are treated as UTC rather than guessed at as local time: a
    silent timezone assumption would show up later as an unexplained
    recovery-time outlier in the effectiveness numbers.
    """
    if start is None or end is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    delta = (end - start).total_seconds()
    if delta < 0:
        return None
    return int(delta)


@dataclass
class SignaturePair:
    """A failure and the resolution that followed it, for pattern mining."""

    failure: FailureSignature
    resolution: Optional[ResolutionSignature] = None
    experience_id: Optional[str] = None
    occurred_at: Optional[datetime] = None
    component_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "FEATURE_SCHEMA_VERSION",
    "MAX_SIGNATURE_VALUES",
    "UNKNOWN",
    "FailureSignature",
    "ResolutionSignature",
    "SignaturePair",
    "build_failure_signature",
    "build_resolution_signature",
    "classify_direction",
    "classify_metric",
    "comparable_seconds",
    "direction_suffix",
    "metric_behavior",
    "recovery_bucket",
]
