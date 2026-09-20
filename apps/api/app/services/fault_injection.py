"""ARGUS Fault Injection Engine (Phase 5 §21, §22).

Applies controlled faults to a running sandbox — and only ever to a running
sandbox.

The mechanism is deliberately dumb and one-directional: ARGUS writes
``faults.json`` in the sandbox's own working tree and the target service
re-reads it per request. No IPC channel, no agent inside the sandbox, no RPC
surface that could be abused to run something. Fault injection becomes a file
write whose content is a typed, bounded spec.

Two things protect the boundary (§21, §56):

* **Target validation.** A fault target must be a service that this sandbox
  actually runs, resolved through the sandbox handle — never a host, never a
  URL, never a container name supplied by a client.
* **Site validation.** The working tree must be a live sandbox directory under
  the configured root. A fault request that names anything else is refused.

Every fault is audited (§22): what was injected, when, on what, for how long,
and how many requests observably carried it. The last part is *derived from the
captured telemetry*, not from a counter ARGUS incremented when it wrote the
file — which is what keeps "the sandbox failed naturally" and "we caused it"
distinguishable even after the fact.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from app.models.reproduction import FaultTrigger, FaultType
from app.services.reproduction_sandbox import (
    SandboxError,
    SandboxHandle,
    sandbox_root_base,
)

logger = logging.getLogger(__name__)

#: Sensible magnitude per fault type, used when the planner does not derive a
#: tighter one from the incident. LATENCY defaults *above* a typical caller
#: timeout because a latency fault that the caller tolerates reproduces nothing.
DEFAULT_MAGNITUDE_MS: dict[FaultType, int] = {
    FaultType.LATENCY: 1500,
    FaultType.TIMEOUT: 6000,
    FaultType.RESOURCE_PRESSURE: 1000,
}

#: Hard ceiling on any injected magnitude — a fault may never be used to make a
#: service hang indefinitely.
MAX_MAGNITUDE_MS = 60_000

#: Bounded activation window when a caller supplies one.
MAX_ACTIVATION_MS = 600_000


@dataclass
class FaultSpec:
    """A validated, sandbox-scoped fault instruction."""

    fault_type: FaultType
    target: str
    trigger: FaultTrigger = FaultTrigger.IMMEDIATE
    duration_ms: Optional[int] = None
    intensity: Optional[float] = None
    parameters: dict[str, Any] = field(default_factory=dict)
    #: Set when the trigger is a replay-position or offset trigger.
    after_replay_index: Optional[int] = None
    at_offset_ms: Optional[int] = None

    def magnitude_ms(self) -> Optional[int]:
        value = self.parameters.get("latency_ms")
        if value is None:
            return None
        try:
            return max(0, min(int(value), MAX_MAGNITUDE_MS))
        except (TypeError, ValueError):
            return None

    def as_active_entry(self) -> dict[str, Any]:
        """The JSON shape a sandbox service reads per request."""
        entry: dict[str, Any] = {
            "fault_type": self.fault_type.value,
            "target": self.target,
            "activated_at": time.time(),
        }
        if self.duration_ms is not None:
            entry["duration_ms"] = int(self.duration_ms)
        if self.intensity is not None:
            entry["intensity"] = float(self.intensity)
        magnitude = self.magnitude_ms()
        if magnitude is not None:
            entry["parameters"] = {"latency_ms": magnitude}
        elif self.parameters:
            entry["parameters"] = {
                key: value
                for key, value in self.parameters.items()
                if isinstance(value, (int, float, str, bool))
            }
        return entry

    def describe(self) -> dict[str, Any]:
        """Audit description used in plans, artifacts and API responses."""
        return {
            "fault_type": self.fault_type.value,
            "target": self.target,
            "trigger": self.trigger.value,
            "duration_ms": self.duration_ms,
            "intensity": self.intensity,
            "parameters": self.parameters,
            "scope": "sandbox",
        }


class FaultInjectionError(SandboxError):
    """Raised when a fault cannot be safely applied."""


def build_spec(
    *,
    fault_type: FaultType,
    target: str,
    services: Iterable[str],
    trigger: FaultTrigger = FaultTrigger.IMMEDIATE,
    duration_ms: Optional[int] = None,
    intensity: Optional[float] = None,
    parameters: Optional[dict[str, Any]] = None,
    after_replay_index: Optional[int] = None,
    at_offset_ms: Optional[int] = None,
) -> FaultSpec:
    """Validate and normalize one fault instruction.

    The ``services`` set is the sandbox's own service list, so an unknown target
    is rejected here — before anything is written anywhere.

    A position- or offset-based trigger without its condition is rejected rather
    than silently downgraded to ``IMMEDIATE``: a fault that fires at the wrong
    moment produces a plausible-looking experiment that proves nothing.
    """
    if target not in set(services):
        raise FaultInjectionError(
            f"Fault target {target!r} is not a service in this sandbox "
            f"(available: {', '.join(sorted(set(services))) or 'none'})"
        )
    if duration_ms is not None and not (0 <= int(duration_ms) <= MAX_ACTIVATION_MS):
        raise FaultInjectionError(
            f"Fault activation window must be 0..{MAX_ACTIVATION_MS} ms"
        )
    if intensity is not None and not (0.0 <= float(intensity) <= 1.0):
        raise FaultInjectionError("Fault intensity must be between 0 and 1")
    if trigger is FaultTrigger.AFTER_REPLAY_INDEX:
        if after_replay_index is None or int(after_replay_index) < 0:
            raise FaultInjectionError(
                "An AFTER_REPLAY_INDEX fault requires a non-negative "
                "after_replay_index"
            )
        after_replay_index = int(after_replay_index)
    elif after_replay_index is not None:
        raise FaultInjectionError(
            "after_replay_index is only meaningful for an AFTER_REPLAY_INDEX fault"
        )
    if trigger is FaultTrigger.AT_OFFSET:
        if at_offset_ms is None or int(at_offset_ms) < 0:
            raise FaultInjectionError(
                "An AT_OFFSET fault requires a non-negative at_offset_ms"
            )
        at_offset_ms = int(at_offset_ms)
    elif at_offset_ms is not None:
        raise FaultInjectionError(
            "at_offset_ms is only meaningful for an AT_OFFSET fault"
        )
    if trigger in {FaultTrigger.ON_REQUEST_COUNT, FaultTrigger.MANUAL}:
        raise FaultInjectionError(
            f"The {trigger.value} trigger cannot be driven inside an experiment: a "
            "sandbox fault is installed by ARGUS, so only IMMEDIATE, "
            "AFTER_REPLAY_INDEX and AT_OFFSET are supported"
        )
    params = dict(parameters or {})
    if "latency_ms" in params:
        try:
            magnitude = int(params["latency_ms"])
        except (TypeError, ValueError) as exc:
            raise FaultInjectionError(
                "parameters.latency_ms must be an integer"
            ) from exc
        if magnitude < 0 or magnitude > MAX_MAGNITUDE_MS:
            raise FaultInjectionError(
                f"Fault magnitude must be 0..{MAX_MAGNITUDE_MS} ms"
            )
        params["latency_ms"] = magnitude
    elif fault_type in DEFAULT_MAGNITUDE_MS:
        params["latency_ms"] = DEFAULT_MAGNITUDE_MS[fault_type]
    return FaultSpec(
        fault_type=fault_type,
        target=target,
        trigger=trigger,
        duration_ms=int(duration_ms) if duration_ms is not None else None,
        intensity=float(intensity) if intensity is not None else None,
        parameters=params,
        after_replay_index=after_replay_index,
        at_offset_ms=at_offset_ms,
    )


class FaultInjectionEngine:
    """Writes fault state into a sandbox and audits what was observed."""

    def __init__(self, *, now: Optional[Any] = None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))

    # -- safety ----------------------------------------------------------
    @staticmethod
    def assert_sandbox(handle: SandboxHandle) -> Path:
        """Prove the handle points at a live sandbox before writing anything.

        Fault injection outside the sandbox is impossible by construction: the
        only path this engine ever writes is ``<sandbox>/faults.json``, and the
        directory must be a descendant of the configured sandbox root.
        """
        root = Path(handle.root_path).resolve()
        base = sandbox_root_base().resolve()
        try:
            root.relative_to(base)
        except ValueError as exc:
            raise FaultInjectionError(
                f"Refusing to inject faults outside the sandbox root: {root}"
            ) from exc
        if not root.exists():
            raise FaultInjectionError(f"Sandbox working tree is gone: {root}")
        return root

    # -- state -----------------------------------------------------------
    def activate(
        self, handle: SandboxHandle, faults: Sequence[FaultSpec]
    ) -> list[dict[str, Any]]:
        """Install the active fault set for a sandbox at the start of a run."""
        root = self.assert_sandbox(handle)
        active = [
            spec.as_active_entry()
            for spec in faults
            if spec.trigger is FaultTrigger.IMMEDIATE and spec.target in handle.services
        ]
        self._write(root, active)
        return active

    def update_triggered(
        self,
        handle: SandboxHandle,
        faults: Sequence[FaultSpec],
        *,
        replay_index: int,
        elapsed_ms: int,
        already_active: set[str],
    ) -> list[dict[str, Any]]:
        """Activate any faults whose trigger condition is now satisfied.

        Called between replay items. ``already_active`` keys the faults already
        installed so a fault is never re-timestamped (which would extend its
        activation window indefinitely).
        """
        root = self.assert_sandbox(handle)
        active: list[dict[str, Any]] = []
        for spec in faults:
            key = f"{spec.fault_type.value}:{spec.target}"
            if key in already_active or spec.target not in handle.services:
                continue
            matched = False
            if (
                spec.trigger is FaultTrigger.AFTER_REPLAY_INDEX
                and spec.after_replay_index is not None
                and replay_index >= spec.after_replay_index
            ):
                matched = True
            elif (
                spec.trigger is FaultTrigger.AT_OFFSET
                and spec.at_offset_ms is not None
                and elapsed_ms >= spec.at_offset_ms
            ):
                matched = True
            if matched:
                entry = spec.as_active_entry()
                active.append(entry)
                already_active.add(key)
        if active:
            current = self.read_active(root)
            self._write(root, current + active)
        return active

    def clear(self, handle: SandboxHandle) -> None:
        """Remove every active fault (used after replay, before teardown)."""
        root = self.assert_sandbox(handle)
        self._write(root, [])

    @staticmethod
    def read_active(root: Path) -> list[dict[str, Any]]:
        path = root / "faults.json"
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        active = payload.get("active")
        return list(active) if isinstance(active, list) else []

    @staticmethod
    def _write(root: Path, active: list[dict[str, Any]]) -> None:
        path = root / "faults.json"
        path.write_text(json.dumps({"active": active}, indent=1), encoding="utf-8")

    # -- audit -----------------------------------------------------------
    def audit(
        self,
        *,
        specs: Sequence[FaultSpec],
        observations: Sequence[Any],
        started_at: Optional[datetime],
        ended_at: Optional[datetime],
    ) -> list[dict[str, Any]]:
        """Derive per-fault impact from *captured telemetry* (§22).

        ``requests_affected`` counts observed signals that carry this fault's
        label on its target service. Reading it off the captured evidence rather
        than off a write-time counter means the audit cannot claim an effect the
        sandbox never showed.
        """
        records: list[dict[str, Any]] = []
        for spec in specs:
            affected = 0
            labelled = False
            for observation in observations:
                attributes = getattr(observation, "attributes", None) or {}
                fault_label = attributes.get("injected_fault")
                if not fault_label:
                    continue
                if fault_label != spec.fault_type.value:
                    continue
                if getattr(observation, "source", None) != spec.target:
                    continue
                labelled = True
                if getattr(observation, "signal_type", None) is not None:
                    affected += 1
            if not labelled:
                status = "SKIPPED"
                result = (
                    f"No request reached {spec.target} while the "
                    f"{spec.fault_type.value} fault was active"
                )
            elif affected == 0:
                status = "COMPLETED"
                result = (
                    f"{spec.fault_type.value} injected on {spec.target}; the "
                    "sandbox did not record an effect"
                )
            else:
                status = "COMPLETED"
                result = (
                    f"{spec.fault_type.value} injected on {spec.target}; "
                    f"{affected} observed signal(s) carried the fault"
                )
            records.append(
                {
                    "spec": spec,
                    "status": status,
                    "injected": labelled,
                    "requests_affected": affected,
                    "result": result,
                    "started_at": started_at,
                    "ended_at": ended_at,
                }
            )
        return records


__all__ = [
    "DEFAULT_MAGNITUDE_MS",
    "MAX_ACTIVATION_MS",
    "MAX_MAGNITUDE_MS",
    "FaultInjectionEngine",
    "FaultInjectionError",
    "FaultSpec",
    "build_spec",
]
