"""ARGUS Reproduction Planner (Phase 5 §7–§9, §26).

Turns a Phase 4 hypothesis into an explicit, inspectable experiment plan.

Everything the plan contains is **derived**, never invented:

* the target component comes from the candidate's own ``component_id``, mapped to
  a sandbox service through the template's declared aliases — a component the
  template does not know about is a planning failure, not a silent guess;
* the fault magnitude comes from the latency the incident actually exhibited on
  that component, floored above the *caller's* timeout so the fault can
  propagate at all;
* the expected behaviour is copied from the incident's own signals, so a
  comparison is always against the hypothesis as it was stated;
* a hypothesis that cannot be represented as a fault (a deployment, a
  configuration change) is planned as a **baseline run with no injection**, and
  the plan says so — that absence is what makes the §60 counterexample a
  meaningful experiment instead of a rigged one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from app.core.config import get_settings
from app.models.causal import CandidateType, ConfidenceLevel, RootCauseCandidate
from app.models.reproduction import (
    FaultTrigger,
    FaultType,
    ReplayInputSource,
    ReplayMode,
    ReproductionStrategy,
    SandboxNetworkPolicy,
)
from app.services.fault_injection import FaultInjectionEngine, FaultSpec, build_spec
from app.services.replay_engine import ReplayItem
from app.services.reproduction_context import SourceBehavior
from app.services.reproduction_expectations import ExpectedBehavior
from app.services.reproduction_sandbox import (
    DEFAULT_TEMPLATE,
    load_template,
    match_service,
)

logger = logging.getLogger(__name__)
settings = get_settings()

REPRODUCTION_ENGINE_VERSION = "phase5-reproduction-v1"


def _enum(enum_cls: Any, value: Any) -> Any:
    """Coerce an enum value that may arrive as a member or as its raw text.

    Fault overrides come from two places — the API (Pydantic enum members) and
    stored plan metadata (plain strings). ``str(member)`` on a ``str``-mixin
    enum yields ``"FaultType.LATENCY"``, which would be a confusing validation
    error rather than a correct value, so the value is unwrapped first.
    """
    if isinstance(value, enum_cls):
        return value
    raw = getattr(value, "value", value)
    return enum_cls(raw)


#: A latency fault must exceed the caller's timeout, or the caller tolerates it
#: and nothing propagates. 1.25x leaves the fault clearly above the line without
#: turning a 400ms timeout into a 30-second experiment.
FAULT_MARGIN = 1.25

#: Candidate types whose reproduction is a dependency fault rather than a
#: request replay.
_FAULTABLE_TYPES = {
    CandidateType.DATABASE,
    CandidateType.RESOURCE_EXHAUSTION,
    CandidateType.INFRASTRUCTURE,
    CandidateType.DEPENDENCY_FAILURE,
    CandidateType.EXTERNAL_DEPENDENCY,
    CandidateType.DATA_ISSUE,
}

#: Candidate types that cannot be represented by injecting a fault. These are
#: planned as a baseline (un-injected) run, which is the honest experiment: if the
#: failure does not occur without the change, the change is implicated; if it
#: does, the change was not necessary.
_BASELINE_TYPES = {
    CandidateType.DEPLOYMENT,
    CandidateType.CONFIGURATION_CHANGE,
}

#: Fault chosen per candidate type when injecting.
_FAULT_BY_TYPE: dict[CandidateType, FaultType] = {
    CandidateType.DATABASE: FaultType.LATENCY,
    CandidateType.RESOURCE_EXHAUSTION: FaultType.LATENCY,
    CandidateType.INFRASTRUCTURE: FaultType.DEPENDENCY_UNAVAILABLE,
    CandidateType.DEPENDENCY_FAILURE: FaultType.DEPENDENCY_UNAVAILABLE,
    CandidateType.EXTERNAL_DEPENDENCY: FaultType.DEPENDENCY_UNAVAILABLE,
    CandidateType.DATA_ISSUE: FaultType.RESPONSE_CORRUPTION,
    CandidateType.APPLICATION_COMPONENT: FaultType.HTTP_5XX,
}


class PlanningError(RuntimeError):
    """Raised when a hypothesis cannot be expressed as a safe experiment."""


@dataclass
class PlanData:
    """A complete plan: what runs, what is injected, and what is expected."""

    template: str
    strategy: ReproductionStrategy
    target_service: Optional[str]
    target_component_id: Optional[Any]
    target_component_name: str
    target_version: Optional[str] = None
    objectives: dict[str, Any] = field(default_factory=dict)
    required_services: list[str] = field(default_factory=list)
    required_dependencies: list[str] = field(default_factory=list)
    input_sources: list[dict[str, Any]] = field(default_factory=list)
    expected_behavior: Optional[ExpectedBehavior] = None
    safety_constraints: dict[str, Any] = field(default_factory=dict)
    resource_limits: dict[str, Any] = field(default_factory=dict)
    network_policy: SandboxNetworkPolicy = SandboxNetworkPolicy.ISOLATED
    timeout_seconds: int = 300
    repetitions: int = 1
    replay_mode: ReplayMode = ReplayMode.SEQUENTIAL
    derived_from: dict[str, Any] = field(default_factory=dict)
    #: Executable parts (not serialized into the plan row itself).
    faults: list[FaultSpec] = field(default_factory=list)
    items: list[ReplayItem] = field(default_factory=list)
    hypothesis: dict[str, Any] = field(default_factory=dict)
    missing_inputs: list[str] = field(default_factory=list)
    environment_name: Optional[str] = None

    @property
    def injected(self) -> bool:
        return bool(self.faults)

    def as_dict(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "strategy": self.strategy.value,
            "target_service": self.target_service,
            "target_component_name": self.target_component_name,
            "objectives": self.objectives,
            "required_services": self.required_services,
            "required_dependencies": self.required_dependencies,
            "input_sources": self.input_sources,
            "expected_behavior": (
                self.expected_behavior.as_dict() if self.expected_behavior else None
            ),
            "safety_constraints": self.safety_constraints,
            "resource_limits": self.resource_limits,
            "network_policy": self.network_policy.value,
            "timeout_seconds": self.timeout_seconds,
            "repetitions": self.repetitions,
            "replay_mode": self.replay_mode.value,
            "faults": [spec.describe() for spec in self.faults],
            "missing_inputs": self.missing_inputs,
        }

    def manifest(self) -> dict[str, Any]:
        """The §43 manifest — enough to understand how the experiment ran."""
        return {
            "template": self.template,
            "application_version": self.target_version,
            "strategy": self.strategy.value,
            "services": list(self.required_services),
            "dependencies": list(self.required_dependencies),
            "inputs": [
                {
                    "method": item.method,
                    "target": f"{item.target_service}{item.target_path}",
                    "payload_hash": item.hash(),
                    "offset_ms": item.relative_offset_ms,
                }
                for item in self.items
            ],
            "faults": [spec.describe() for spec in self.faults],
            "repetitions": self.repetitions,
            "network_policy": self.network_policy.value,
            "resource_limits": self.resource_limits,
            "timeout_seconds": self.timeout_seconds,
            "safety_constraints": self.safety_constraints,
        }


class ReproductionPlanner:
    """Builds a plan from a hypothesis, an incident, and a system template."""

    def __init__(
        self,
        *,
        template: str = DEFAULT_TEMPLATE,
        fault_engine: Optional[FaultInjectionEngine] = None,
    ) -> None:
        self._template_name = template
        self._faults = fault_engine or FaultInjectionEngine()

    def plan(
        self,
        *,
        incident_id: Any,
        project_id: Any,
        source: SourceBehavior,
        candidate: Optional[RootCauseCandidate],
        candidate_component_name: Optional[str],
        candidate_type: Optional[CandidateType],
        candidate_confidence: Optional[ConfidenceLevel],
        candidate_evidence: Sequence[dict[str, Any]] = (),
        analysis_id: Optional[Any] = None,
        repetitions: Optional[int] = None,
        replay_mode: Optional[ReplayMode] = None,
        network_policy: Optional[SandboxNetworkPolicy] = None,
        timeout_seconds: Optional[int] = None,
        strategy: Optional[ReproductionStrategy] = None,
        fault_overrides: Sequence[dict[str, Any]] = (),
        input_overrides: Sequence[ReplayItem] = (),
        resource_limits: Optional[dict[str, Any]] = None,
    ) -> PlanData:
        template = load_template(self._template_name)
        services = {svc["name"]: dict(svc) for svc in template["services"]}
        entry_service = template.get("entry_service")

        target_service = match_service(template, candidate_component_name)
        missing_inputs: list[str] = []

        if candidate is None:
            raise PlanningError(
                "This incident has no root-cause hypothesis to reproduce. Run a "
                "causal analysis first."
            )
        if target_service is None:
            raise PlanningError(
                f"Component {candidate_component_name!r} is not part of the "
                f"reproduction template {self._template_name!r}, so no experiment "
                "can be constructed for it. Known services: "
                f"{', '.join(sorted(services))}."
            )

        # ---- strategy + faults (the safety-relevant decision) --------------
        chosen_faults: list[FaultSpec] = []
        if strategy is None:
            strategy = (
                ReproductionStrategy.DEPENDENCY_FAULT
                if candidate_type in _FAULTABLE_TYPES
                or candidate_component_name in {"inventory", "checkout"}
                else ReproductionStrategy.SYNTHETIC_INPUT_REPLAY
            )
            if candidate_type in _BASELINE_TYPES:
                strategy = ReproductionStrategy.CONFIGURATION_REPLAY
            elif candidate_type in _FAULTABLE_TYPES:
                strategy = ReproductionStrategy.DEPENDENCY_FAULT

        if fault_overrides:
            for override in fault_overrides:
                chosen_faults.append(
                    build_spec(
                        # ``_enum`` accepts either a value or a member, so a caller
                        # passing a Pydantic enum does not smuggle "FaultType.LATENCY"
                        # past validation as a string.
                        fault_type=_enum(FaultType, override["fault_type"]),
                        target=str(override["target"]),
                        services=services,
                        trigger=_enum(
                            FaultTrigger,
                            override.get("trigger") or FaultTrigger.IMMEDIATE,
                        ),
                        duration_ms=override.get("duration_ms"),
                        intensity=override.get("intensity"),
                        parameters=override.get("parameters"),
                        after_replay_index=override.get("after_replay_index"),
                        at_offset_ms=override.get("at_offset_ms"),
                    )
                )
        elif candidate_type not in _BASELINE_TYPES:
            fault = self.derive_fault(
                services=services,
                candidate_type=candidate_type,
                target_service=target_service,
                source=source,
                component_name=candidate_component_name,
            )
            if fault is None:
                missing_inputs.append(
                    f"No latency measurement for {candidate_component_name} in the "
                    "incident, so the injected fault magnitude is the default"
                )
            else:
                chosen_faults.append(fault)
        else:
            # A deployment/config hypothesis is tested by *absence*: run the
            # baseline. Recording the reason here is what lets the comparison
            # interpret a non-failure correctly rather than as a broken plan.
            missing_inputs.append(
                f"A {candidate_type.value} cannot be replayed into the sandbox; the "
                "experiment runs the un-changed baseline, so the expected failure "
                "may legitimately not occur"
            )

        # ---- inputs --------------------------------------------------------
        items = list(input_overrides) or self.derive_inputs(
            template=template,
            target_service=target_service,
            entry_service=entry_service,
        )
        if not items:
            missing_inputs.append(
                "The template defines no replayable operation for "
                f"{target_service}; no request can be replayed"
            )

        # ---- required services --------------------------------------------
        required = self.required_services(services, target_service=target_service)
        if not required:
            raise PlanningError(f"No runnable service chain reaches {target_service!r}")

        expected = self.build_expectations(
            source=source,
            template=template,
            required_services=required,
        )
        if expected.is_empty:
            missing_inputs.append(
                "The incident carries no comparable telemetry (no affected "
                "components and no failing spans), so there is no expected "
                "behaviour to compare against"
            )

        limits = {
            "cpu_seconds": settings.REPRO_MAX_CPU_SECONDS,
            "memory_mb": settings.REPRO_MAX_MEMORY_MB,
            "disk_mb": settings.REPRO_MAX_DISK_MB,
            "processes": settings.REPRO_MAX_PROCESSES,
            "timeout_seconds": (
                timeout_seconds or settings.REPRO_EXPERIMENT_TIMEOUT_SECONDS
            ),
        }
        limits.update(resource_limits or {})

        target_version = services[target_service].get("app_version")
        statement = self.hypothesis_statement(
            component=target_service,
            candidate_type=candidate_type,
            expected=expected,
        )
        resolved_mode = replay_mode or self.default_replay_mode()
        resolved_policy = network_policy or SandboxNetworkPolicy.ISOLATED

        return PlanData(
            template=self._template_name,
            strategy=strategy,
            target_service=target_service,
            target_component_id=candidate.component_id,
            target_component_name=candidate_component_name or target_service,
            target_version=str(target_version) if target_version else None,
            objectives={
                "hypothesis": statement,
                "candidate_type": candidate_type.value if candidate_type else None,
                "candidate_score": round(float(candidate.score or 0.0), 4),
                "candidate_confidence": (
                    candidate_confidence.value if candidate_confidence else None
                ),
                "evidence": list(candidate_evidence)[:20],
                "objective": (
                    "Determine whether the hypothesised failure is reproducible in "
                    "an isolated sandbox, and how closely the reproduction matches "
                    "the original incident."
                ),
                "guardrail": (
                    "A successful reproduction demonstrates that this failure is "
                    "reachable under these conditions. It does not establish that "
                    "this was the cause in production."
                ),
                "on_missing": missing_inputs,
            },
            required_services=required,
            required_dependencies=[
                name for name in required if services[name].get("dependency")
            ],
            input_sources=[self._input_descriptor(item) for item in items],
            expected_behavior=expected,
            safety_constraints=self.safety_constraints(network_policy=resolved_policy),
            resource_limits=limits,
            network_policy=resolved_policy,
            timeout_seconds=int(limits["timeout_seconds"]),
            repetitions=self.resolved_repetitions(repetitions),
            replay_mode=resolved_mode,
            derived_from={
                "incident_id": str(incident_id),
                "project_id": str(project_id),
                "analysis_id": str(analysis_id) if analysis_id else None,
                "candidate_id": str(candidate.id),
                "source_summary": source.as_dict(),
                "template": self._template_name,
                "engine_version": REPRODUCTION_ENGINE_VERSION,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                #: Stored so a later run executes the *planned* mode rather than
                #: whatever the configuration happens to say at that moment.
                "replay_mode": resolved_mode.value,
                "network_policy": resolved_policy.value,
            },
            faults=chosen_faults,
            items=items,
            hypothesis={
                "statement": statement,
                "candidate_id": str(candidate.id),
                "candidate_type": candidate_type.value if candidate_type else None,
                "component_id": str(candidate.component_id)
                if candidate.component_id
                else None,
                "component_name": candidate_component_name,
                "expected_failure": (
                    "the expected components fail in the incident's order"
                ),
                "expected_components": expected.components,
                "expected_sequence": expected.sequence,
                "expected_signals": [signal.as_dict() for signal in expected.signals],
                "expected_time_window_seconds": expected.window_seconds,
                "supporting_evidence": list(candidate_evidence)[:20],
            },
            missing_inputs=missing_inputs,
            environment_name=self._template_name,
        )

    # -- derivation ------------------------------------------------------
    @staticmethod
    def alias_map(
        *, source: SourceBehavior, template: dict[str, Any]
    ) -> dict[str, str]:
        """Map every component the incident names onto the sandbox's service name.

        Derived from the same template aliases :meth:`match_service` uses while
        planning, so the fault target, the expectation, and the comparison all
        speak one vocabulary. Components the template does not know are simply
        absent from the map — the caller keeps them verbatim, which is what makes
        them appear as *missing* rather than silently matching nothing.
        """
        names: set[str] = set()
        for collection in (
            source.components,
            source.sequence,
            source.error_components,
            source.degraded_components,
            source.recovery_order,
        ):
            names.update(collection)
        names.update(source.latency_ms)
        names.update(source.log_patterns)
        mapped: dict[str, str] = {}
        for name in sorted(names):
            service = match_service(template, name)
            if service:
                mapped[name] = service
        return mapped

    @staticmethod
    def default_replay_mode() -> ReplayMode:
        try:
            return ReplayMode(
                (settings.REPRO_DEFAULT_REPLAY_MODE or "SEQUENTIAL").upper()
            )
        except ValueError:
            return ReplayMode.SEQUENTIAL

    @staticmethod
    def resolved_repetitions(requested: Optional[int]) -> int:
        value = requested or settings.REPRO_DEFAULT_REPETITIONS
        return max(1, min(int(value), settings.REPRO_MAX_REPETITIONS))

    def derive_fault(
        self,
        *,
        services: dict[str, dict[str, Any]],
        candidate_type: Optional[CandidateType],
        target_service: str,
        source: SourceBehavior,
        component_name: Optional[str],
    ) -> Optional[FaultSpec]:
        """Choose the fault that can produce the hypothesised failure.

        Magnitude comes from the incident's own latency for that component when it
        has one, raised above the caller's timeout so the effect can propagate.
        Absence of a measurement is reported (``None``) rather than papered over.
        """
        fault_type = _FAULT_BY_TYPE.get(candidate_type or CandidateType.UNKNOWN)
        if fault_type is None:
            fault_type = (
                FaultType.LATENCY
                if services.get(target_service, {}).get("role") == "datastore"
                else FaultType.HTTP_5XX
            )
        parameters: dict[str, Any] = {}
        if fault_type in {
            FaultType.LATENCY,
            FaultType.TIMEOUT,
            FaultType.RESOURCE_PRESSURE,
        }:
            observed = self._first_latency(source, component_name, target_service)
            caller_timeout = self._caller_timeout(services, target_service)
            if observed is None and caller_timeout is None:
                magnitude = None
            else:
                floor = int((caller_timeout or 1000) * FAULT_MARGIN)
                magnitude = max(floor, int(observed or 0))
            if magnitude is not None:
                parameters["latency_ms"] = magnitude
        spec = build_spec(
            fault_type=fault_type,
            target=target_service,
            services=services,
            duration_ms=None,
            intensity=1.0,
            parameters=parameters,
        )
        # No measurement and no caller timeout means the fault magnitude is a
        # default; say so rather than presenting it as derived.
        if fault_type in {FaultType.LATENCY, FaultType.TIMEOUT} and not parameters:
            return None
        return spec

    @staticmethod
    def _first_latency(
        source: SourceBehavior, *names: Optional[str]
    ) -> Optional[float]:
        for name in names:
            if not name:
                continue
            for candidate_name, value in source.latency_ms.items():
                if candidate_name.strip().lower() == name.strip().lower():
                    return float(value)
        return None

    @staticmethod
    def _caller_timeout(
        services: dict[str, dict[str, Any]], target: str
    ) -> Optional[float]:
        """The tightest timeout among services that call ``target``."""
        timeouts = [
            float(config.get("timeout_ms") or 0)
            for config in services.values()
            if config.get("dependency") == target and config.get("timeout_ms")
        ]
        return min(timeouts) if timeouts else None

    @staticmethod
    def required_services(
        services: dict[str, dict[str, Any]], *, target_service: str
    ) -> list[str]:
        """Target plus every service whose calls could carry the failure upward.

        A fault on the datastore is only observable through the services that
        depend on it, so reproducing "the datastore caused checkout failures"
        requires the whole chain to be running — including the entry service that
        actually receives the replayed request.
        """
        required = {target_service}
        changed = True
        while changed:
            changed = False
            for name, config in services.items():
                dependency = config.get("dependency")
                if dependency in required and name not in required:
                    required.add(name)
                    changed = True
        # Start order is dependency-first, which the sandbox backend also expects.
        ordered: list[str] = []
        while len(ordered) < len(required):
            progressed = False
            for name in required:
                if name in ordered:
                    continue
                dependency = services.get(name, {}).get("dependency")
                if dependency is None or dependency in ordered:
                    ordered.append(name)
                    progressed = True
            if not progressed:
                # A cycle in the topology: keep the remaining names in a stable
                # order rather than looping forever.
                ordered.extend(sorted(name for name in required if name not in ordered))
        return ordered

    def derive_inputs(
        self,
        *,
        template: dict[str, Any],
        target_service: str,
        entry_service: Optional[str],
    ) -> list[ReplayItem]:
        """Choose which operations to replay, deepest-relevant first.

        The entry service's operation is always replayed when the target is not
        the entry (otherwise the failure cannot propagate), and the target
        service is probed directly so the sandbox shows the failure at two depths
        of the same chain — which is what makes the reproduced sequence
        comparable to the incident's.
        """
        operations: dict[str, dict[str, Any]] = template.get("operations") or {}
        items: list[ReplayItem] = []
        order = 0
        entry = entry_service if entry_service in operations else None
        if entry and entry != target_service:
            descriptor = operations[entry]
            items.append(
                ReplayItem(
                    method=str(descriptor.get("method", "POST")),
                    target_service=entry,
                    target_path=str(descriptor.get("path", "/")),
                    payload=dict(descriptor.get("payload") or {}),
                    source=ReplayInputSource.SYNTHETIC,
                    plan_order=order,
                )
            )
            order += 1
        target_operation = operations.get(target_service)
        if target_operation is not None:
            items.append(
                ReplayItem(
                    method=str(target_operation.get("method", "GET")),
                    target_service=target_service,
                    target_path=str(target_operation.get("path", "/")),
                    payload=dict(target_operation.get("payload") or {}),
                    source=ReplayInputSource.SYNTHETIC,
                    plan_order=order,
                )
            )
        return items

    @staticmethod
    def build_expectations(
        *,
        source: SourceBehavior,
        template: dict[str, Any],
        required_services: Sequence[str],
    ) -> ExpectedBehavior:
        """Copy the incident's signals into sandbox service names (§26)."""

        def to_service(name: Optional[str]) -> Optional[str]:
            return match_service(template, name)

        components: list[str] = []
        sequence: list[str] = []
        for name in source.components:
            mapped = to_service(name)
            if mapped and mapped not in components:
                components.append(mapped)
        for name in source.sequence:
            mapped = to_service(name)
            if mapped and mapped not in sequence:
                sequence.append(mapped)
        if not components:
            # The incident named components the template does not know; fall back
            # to the services the plan will actually run so the expectation is
            # still a statement about *this* experiment.
            components = list(required_services)
        if not sequence:
            sequence = list(components)

        error_components = [
            mapped
            for mapped in (to_service(name) for name in source.error_components)
            if mapped
        ]
        if not error_components and source.error_components:
            # Incident evidence named a component outside the template: the
            # expectation is that the *chain* fails, which is what matters.
            error_components = [
                name for name in components if name != required_services[0]
            ]

        slow_components: list[tuple[str, float]] = []
        for name, value in source.latency_ms.items():
            mapped = to_service(name)
            if mapped and value > 0:
                slow_components.append((mapped, float(value)))

        underivable: list[str] = []
        if not error_components:
            underivable.append(
                "The incident recorded no component-level error signal, so no "
                "error expectation could be derived"
            )
        if not slow_components:
            underivable.append(
                "The incident recorded no latency measurement, so no degradation "
                "threshold could be derived"
            )

        return ExpectedBehavior.from_evidence(
            components=list(dict.fromkeys(components)),
            sequence=list(dict.fromkeys(sequence)),
            error_components=error_components,
            slow_components=slow_components,
            degraded_components=[
                mapped
                for mapped in (to_service(name) for name in source.degraded_components)
                if mapped
            ],
            window_seconds=settings.CORRELATION_WINDOW_SECONDS,
            provenance={
                "source_sequence": list(source.sequence),
                "source_error_components": list(source.error_components),
                "source_latency_ms": dict(source.latency_ms),
                "sources": source.provenance,
            },
            underivable=underivable,
        )

    @staticmethod
    def hypothesis_statement(
        *,
        component: str,
        candidate_type: Optional[CandidateType],
        expected: ExpectedBehavior,
    ) -> str:
        """A falsifiable one-line claim, naming the chain it predicts."""
        chain = " → ".join(expected.sequence) if expected.sequence else component
        kind = (
            candidate_type.value.lower().replace("_", " ")
            if candidate_type
            else "cause"
        )
        return (
            f"An induced {kind} on {component} produces the incident's failure "
            f"sequence ({chain}) with the same affected components."
        )

    @staticmethod
    def safety_constraints(*, network_policy: SandboxNetworkPolicy) -> dict[str, Any]:
        """The explicit promises a plan makes, shown before execution (§47)."""
        return {
            "production_access": "BLOCKED",
            "production_credentials": "NEVER_AVAILABLE",
            "credentials": "SANITIZED",
            "host_filesystem": "PRIVATE_WORKDIR_ONLY",
            "arbitrary_commands": "REJECTED_TYPED_FAULTS_ONLY",
            "network_policy": network_policy.value,
            "scope": "sandbox",
            "cleanup": "MANDATORY",
            "resource_limits": "ENFORCED_BY_OS",
        }

    @staticmethod
    def _input_descriptor(item: ReplayItem) -> dict[str, Any]:
        return {
            "method": item.method,
            "target_service": item.target_service,
            "target_path": item.target_path,
            "source": item.source.value,
            "payload_hash": item.hash(),
            "relative_offset_ms": item.relative_offset_ms,
        }


__all__ = [
    "FAULT_MARGIN",
    "REPRODUCTION_ENGINE_VERSION",
    "PlanData",
    "PlanningError",
    "ReproductionPlanner",
]
