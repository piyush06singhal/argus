"""ARGUS Remediation Action Registry (Phase 9 §3, §4).

The registry is the *closed* vocabulary of Phase 9. Everything downstream —
planner, safety engine, policy engine, executor — can only ever refer to an
action by its :class:`RemediationActionType`, and every property that matters
for safety is declared here, once, in code:

* how dangerous it is (``risk_level``)
* where its effect lands (``adapter_kind``)
* the only parameters it accepts, with types and bounds (``parameters``)
* how it is undone (``rollback_strategy``, ``inverse_action``)
* how success is judged (``verification_plan``)
* the largest scope it may ever touch (``maximum_blast_radius``)
* whether it may ever run unattended (``supports_autonomous_execution``)
* whether it touches a real production system (``production_effect``)

Three refusals are encoded structurally rather than by convention:

1. **No arbitrary commands.** There is no ``SERVER_COMMAND``-style member and no
   free-text parameter: a parameter is a name from :data:`PARAMETERS` with a
   declared type, a set of choices, or numeric bounds. An unknown key is a
   validation error, not something a handler ignores.
2. **Unconfigured means refused.** ``EXTERNAL`` actions are registered — so they
   can be proposed, assessed, approved and *manually recorded* — but the executor
   has no adapter for them by default and refuses with ``ADAPTER_UNAVAILABLE``.
   ARGUS does not pretend to be able to restart a system it has no credentials
   for (§2 of the safety principles).
3. **Irreversibility is declared up front.** An action whose
   ``rollback_strategy`` is ``NONE`` or ``MANUAL`` is escalated to human approval
   by the safety engine unconditionally, no matter how low its nominal risk is.

The registry is a pure data structure — importing it executes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.core.config import get_settings
from app.models.remediation import (
    AdapterKind,
    BlastRadiusScope,
    RemediationActionType,
    RemediationRiskLevel,
    RollbackStrategy,
    VerificationCheckKind,
)

settings = get_settings()


# ---------------------------------------------------------------------------
# Blast radius ordering
# ---------------------------------------------------------------------------

#: Ascending reach. Used to answer "is this scope wider than that ceiling?".
BLAST_RADIUS_ORDER: dict[BlastRadiusScope, int] = {
    BlastRadiusScope.SINGLE_INSTANCE: 0,
    BlastRadiusScope.SINGLE_COMPONENT: 1,
    BlastRadiusScope.LIMITED_PERCENTAGE: 2,
    BlastRadiusScope.ENVIRONMENT: 3,
}

#: Ascending severity.
RISK_ORDER: dict[RemediationRiskLevel, int] = {
    RemediationRiskLevel.LOW: 0,
    RemediationRiskLevel.MEDIUM: 1,
    RemediationRiskLevel.HIGH: 2,
    RemediationRiskLevel.CRITICAL: 3,
}


def risk_rank(level: RemediationRiskLevel) -> int:
    """Numeric rank for a risk level (higher is worse)."""
    return RISK_ORDER[level]


def blast_radius_rank(scope: BlastRadiusScope) -> int:
    """Numeric rank for a blast-radius scope (higher reaches further)."""
    return BLAST_RADIUS_ORDER[scope]


def max_risk(
    left: RemediationRiskLevel, right: RemediationRiskLevel
) -> RemediationRiskLevel:
    """The more severe of two risk levels."""
    return left if risk_rank(left) >= risk_rank(right) else right


def max_scope(left: BlastRadiusScope, right: BlastRadiusScope) -> BlastRadiusScope:
    """The wider of two blast-radius scopes."""
    return left if blast_radius_rank(left) >= blast_radius_rank(right) else right


# ---------------------------------------------------------------------------
# Parameter specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParameterSpec:
    """One accepted parameter — type, bounds and meaning.

    ``choices`` is a static tuple or the string ``"feature_flags"`` /
    ``"background_jobs"`` to mean "resolved from settings at validation time",
    so the accepted values stay in configuration rather than in a literal.
    """

    name: str
    kind: str  # "str" | "int" | "float" | "bool" | "uuid"
    required: bool = False
    choices: Optional[tuple[str, ...]] = None
    choices_from: Optional[str] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    default: Any = None
    description: str = ""


@dataclass(frozen=True)
class ActionDefinition:
    """Everything the platform knows about one registered action (§4)."""

    action_type: RemediationActionType
    description: str
    risk_level: RemediationRiskLevel
    adapter_kind: AdapterKind
    parameters: tuple[ParameterSpec, ...]
    verification_plan: tuple[VerificationCheckKind, ...]
    rollback_strategy: RollbackStrategy
    maximum_blast_radius: BlastRadiusScope
    inverse_action: Optional[RemediationActionType] = None
    #: Whether the effect is applied to a system outside ARGUS's own runtime.
    production_effect: bool = False
    supports_canary: bool = False
    supports_autonomous_execution: bool = False
    #: Set when the action is *never* executable in this build; the value is the
    #: refusal reason a reader sees in the assessment.
    unavailable_reason: Optional[str] = None
    #: Named control-plane key parameter, when the action changes a control.
    control_kind: Optional[str] = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def requires_parameters(self) -> tuple[ParameterSpec, ...]:
        return tuple(p for p in self.parameters if p.required)

    @property
    def reversible(self) -> bool:
        """Whether the platform can undo this action *by itself*.

        ``MANUAL`` is deliberately excluded: a human undoing something outside
        ARGUS is not a rollback the platform can promise.
        """
        return self.rollback_strategy in (
            RollbackStrategy.INVERSE_ACTION,
            RollbackStrategy.RESTORE_PREVIOUS_STATE,
            RollbackStrategy.REVERT_WORKSPACE,
        )


def _control_plane_checks() -> tuple[VerificationCheckKind, ...]:
    """The checks a control-plane action is verified with.

    Control state is read back *and* the systemic effect is measured. The first
    alone would only prove that a row was written, which is why it is never the
    only check.
    """
    return (
        VerificationCheckKind.CONTROL_STATE,
        VerificationCheckKind.CONTROL_EFFECT,
        VerificationCheckKind.HEALTH_STATUS,
        VerificationCheckKind.ERROR_RATE,
        VerificationCheckKind.NEW_INCIDENTS,
    )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


REGISTRY: dict[RemediationActionType, ActionDefinition] = {}


def _register(definition: ActionDefinition) -> ActionDefinition:
    if definition.action_type in REGISTRY:  # pragma: no cover - import guard
        raise RuntimeError(f"duplicate action registration: {definition.action_type}")
    REGISTRY[definition.action_type] = definition
    return definition


_register(
    ActionDefinition(
        action_type=RemediationActionType.RESTART_SERVICE,
        description="Restart a degraded service so it re-establishes its runtime state.",
        risk_level=RemediationRiskLevel.MEDIUM,
        adapter_kind=AdapterKind.EXTERNAL,
        parameters=(
            ParameterSpec(
                "graceful",
                "bool",
                required=False,
                default=True,
                description="Drain in-flight work before restarting.",
            ),
        ),
        verification_plan=(
            VerificationCheckKind.HEALTH_STATUS,
            VerificationCheckKind.ERROR_RATE,
            VerificationCheckKind.LATENCY,
            VerificationCheckKind.NEW_INCIDENTS,
        ),
        rollback_strategy=RollbackStrategy.NONE,
        maximum_blast_radius=BlastRadiusScope.SINGLE_COMPONENT,
        supports_canary=True,
        unavailable_reason=(
            "no orchestration adapter is configured for this environment; a "
            "human must perform the restart and record it"
        ),
        notes=(
            "A restart is not reversible in the sense the platform can promise: "
            "the pre-restart in-memory state is gone. Approval is therefore "
            "always required.",
        ),
    )
)

_register(
    ActionDefinition(
        action_type=RemediationActionType.RESTART_INSTANCE,
        description="Restart a single failing instance, leaving its siblings running.",
        risk_level=RemediationRiskLevel.MEDIUM,
        adapter_kind=AdapterKind.EXTERNAL,
        parameters=(
            ParameterSpec("instance_id", "str", required=True),
            ParameterSpec("graceful", "bool", required=False, default=True),
        ),
        verification_plan=(
            VerificationCheckKind.INSTANCE_COUNT,
            VerificationCheckKind.HEALTH_STATUS,
            VerificationCheckKind.ERROR_RATE,
        ),
        rollback_strategy=RollbackStrategy.NONE,
        maximum_blast_radius=BlastRadiusScope.SINGLE_INSTANCE,
        supports_canary=False,
        unavailable_reason=(
            "no instance-management adapter is configured for this environment"
        ),
    )
)

_register(
    ActionDefinition(
        action_type=RemediationActionType.DISABLE_FEATURE_FLAG,
        description=(
            "Disable an ARGUS-owned behaviour flag to stop a suspect code path "
            "from running."
        ),
        risk_level=RemediationRiskLevel.LOW,
        adapter_kind=AdapterKind.CONTROL_PLANE,
        parameters=(
            ParameterSpec(
                "flag",
                "str",
                required=True,
                choices_from="feature_flags",
                description="Which ARGUS-owned flag to disable.",
            ),
        ),
        verification_plan=_control_plane_checks(),
        rollback_strategy=RollbackStrategy.INVERSE_ACTION,
        maximum_blast_radius=BlastRadiusScope.ENVIRONMENT,
        inverse_action=RemediationActionType.ENABLE_FEATURE_FLAG,
        supports_canary=True,
        supports_autonomous_execution=True,
        control_kind="FEATURE_FLAG",
        notes=(
            "Bounded to ARGUS's own flags: the platform deliberately cannot "
            "reach into an application's feature-flag provider, so an "
            "application flag is proposed but never executed here.",
        ),
    )
)

_register(
    ActionDefinition(
        action_type=RemediationActionType.ENABLE_FEATURE_FLAG,
        description="Re-enable an ARGUS-owned behaviour flag after a fix or a rollback.",
        risk_level=RemediationRiskLevel.MEDIUM,
        adapter_kind=AdapterKind.CONTROL_PLANE,
        parameters=(
            ParameterSpec("flag", "str", required=True, choices_from="feature_flags"),
        ),
        verification_plan=_control_plane_checks(),
        rollback_strategy=RollbackStrategy.INVERSE_ACTION,
        maximum_blast_radius=BlastRadiusScope.ENVIRONMENT,
        inverse_action=RemediationActionType.DISABLE_FEATURE_FLAG,
        supports_canary=False,
        supports_autonomous_execution=False,
        control_kind="FEATURE_FLAG",
        notes=(
            "Enabling is not the mirror of disabling: it can *introduce* work "
            "that was previously stopped, so it is never autonomous.",
        ),
    )
)

_register(
    ActionDefinition(
        action_type=RemediationActionType.ROLLBACK_DEPLOYMENT,
        description="Roll a deployment back to its previous revision.",
        risk_level=RemediationRiskLevel.HIGH,
        adapter_kind=AdapterKind.EXTERNAL,
        parameters=(
            ParameterSpec("deployment_id", "uuid", required=True),
            ParameterSpec("target_revision", "str", required=False),
        ),
        verification_plan=(
            VerificationCheckKind.HEALTH_STATUS,
            VerificationCheckKind.ERROR_RATE,
            VerificationCheckKind.LATENCY,
            VerificationCheckKind.NEW_INCIDENTS,
        ),
        rollback_strategy=RollbackStrategy.MANUAL,
        maximum_blast_radius=BlastRadiusScope.SINGLE_COMPONENT,
        production_effect=True,
        unavailable_reason=(
            "no deployment adapter is configured; ARGUS never deploys, so this "
            "is proposed for a human to execute"
        ),
        notes=(
            "Rolling *forward* is not a rollback, so the platform marks this "
            "MANUAL rather than claiming it can undo itself.",
        ),
    )
)

_register(
    ActionDefinition(
        action_type=RemediationActionType.ROLLBACK_CONFIGURATION,
        description="Restore a previous configuration revision.",
        risk_level=RemediationRiskLevel.HIGH,
        adapter_kind=AdapterKind.EXTERNAL,
        parameters=(
            ParameterSpec("config_key", "str", required=True),
            ParameterSpec("previous_revision", "str", required=True),
        ),
        verification_plan=(
            VerificationCheckKind.HEALTH_STATUS,
            VerificationCheckKind.ERROR_RATE,
            VerificationCheckKind.NEW_INCIDENTS,
        ),
        rollback_strategy=RollbackStrategy.MANUAL,
        maximum_blast_radius=BlastRadiusScope.SINGLE_COMPONENT,
        production_effect=True,
        unavailable_reason=(
            "no configuration adapter is configured for this environment"
        ),
    )
)

_register(
    ActionDefinition(
        action_type=RemediationActionType.SCALE_SERVICE_WITHIN_LIMIT,
        description="Add replicas to absorb load, within a hard replica ceiling.",
        risk_level=RemediationRiskLevel.MEDIUM,
        adapter_kind=AdapterKind.EXTERNAL,
        parameters=(
            ParameterSpec("replicas", "int", required=True, minimum=1, maximum=10),
        ),
        verification_plan=(
            VerificationCheckKind.INSTANCE_COUNT,
            VerificationCheckKind.LATENCY,
            VerificationCheckKind.ERROR_RATE,
        ),
        rollback_strategy=RollbackStrategy.MANUAL,
        maximum_blast_radius=BlastRadiusScope.SINGLE_COMPONENT,
        production_effect=True,
        unavailable_reason=(
            "no scaling adapter is configured; ARGUS does not hold capacity "
            "credentials"
        ),
        notes=(
            "Money is spent by this action, which is why the replica ceiling is "
            "in the parameter schema rather than in a caller's head.",
        ),
    )
)

_register(
    ActionDefinition(
        action_type=RemediationActionType.DISABLE_DEGRADED_DEPENDENCY,
        description=(
            "Temporarily exclude a failing dependency from correlation so it "
            "stops masking other signals."
        ),
        risk_level=RemediationRiskLevel.LOW,
        adapter_kind=AdapterKind.CONTROL_PLANE,
        parameters=(
            ParameterSpec("dependency_component_id", "uuid", required=True),
            ParameterSpec(
                "duration_seconds", "int", required=False, minimum=60, maximum=3600
            ),
        ),
        verification_plan=_control_plane_checks(),
        rollback_strategy=RollbackStrategy.RESTORE_PREVIOUS_STATE,
        maximum_blast_radius=BlastRadiusScope.SINGLE_COMPONENT,
        supports_autonomous_execution=True,
        control_kind="DEPENDENCY_SUPPRESSION",
        notes=(
            "This suppresses *correlation*, never traffic: it changes what ARGUS "
            "concludes, not what the application does.",
        ),
    )
)

_register(
    ActionDefinition(
        action_type=RemediationActionType.ROUTE_TRAFFIC_TO_HEALTHY_INSTANCE,
        description="Move traffic off an unhealthy instance onto a healthy one.",
        risk_level=RemediationRiskLevel.HIGH,
        adapter_kind=AdapterKind.EXTERNAL,
        parameters=(
            ParameterSpec("unhealthy_instance_id", "str", required=True),
            ParameterSpec("healthy_instance_id", "str", required=True),
        ),
        verification_plan=(
            VerificationCheckKind.HEALTH_STATUS,
            VerificationCheckKind.ERROR_RATE,
            VerificationCheckKind.LATENCY,
        ),
        rollback_strategy=RollbackStrategy.MANUAL,
        maximum_blast_radius=BlastRadiusScope.LIMITED_PERCENTAGE,
        production_effect=True,
        unavailable_reason=(
            "no load-balancer adapter is configured for this environment"
        ),
    )
)

_register(
    ActionDefinition(
        action_type=RemediationActionType.APPLY_VERIFIED_PATCH,
        description=(
            "Apply a Phase 7-verified patch to an isolated workspace copy and "
            "re-run its verification there."
        ),
        risk_level=RemediationRiskLevel.HIGH,
        adapter_kind=AdapterKind.WORKSPACE,
        parameters=(
            ParameterSpec("patch_id", "uuid", required=True),
            ParameterSpec("verify_tests", "bool", required=False, default=True),
        ),
        verification_plan=(
            VerificationCheckKind.WORKSPACE_DIFF,
            VerificationCheckKind.TEST_RESULT,
        ),
        rollback_strategy=RollbackStrategy.REVERT_WORKSPACE,
        maximum_blast_radius=BlastRadiusScope.SINGLE_COMPONENT,
        supports_canary=False,
        unavailable_reason=(
            "a workspace adapter is only available once the patch's own "
            "repository is registered and its verification workspace can be "
            "recreated"
        ),
        notes=(
            "ARGUS does not deploy. This action produces and validates the "
            "change inside a sandbox; shipping it remains a human/CD decision.",
        ),
    )
)

_register(
    ActionDefinition(
        action_type=RemediationActionType.PAUSE_BACKGROUND_JOB,
        description="Pause an ARGUS background job that is amplifying an incident.",
        risk_level=RemediationRiskLevel.LOW,
        adapter_kind=AdapterKind.CONTROL_PLANE,
        parameters=(
            ParameterSpec(
                "job",
                "str",
                required=True,
                choices_from="background_jobs",
                description="Which background job to pause.",
            ),
            ParameterSpec(
                "duration_seconds",
                "int",
                required=False,
                minimum=60,
                maximum=86_400,
                description="Auto-resume after this long; omit for indefinite.",
            ),
        ),
        verification_plan=_control_plane_checks(),
        rollback_strategy=RollbackStrategy.INVERSE_ACTION,
        maximum_blast_radius=BlastRadiusScope.ENVIRONMENT,
        inverse_action=RemediationActionType.RESUME_BACKGROUND_JOB,
        supports_canary=False,
        supports_autonomous_execution=True,
        control_kind="BACKGROUND_JOB",
        notes=(
            "Pausing is not deleting: the job's queue and state are untouched "
            "and RESUME_BACKGROUND_JOB restarts it exactly where it stopped.",
        ),
    )
)

_register(
    ActionDefinition(
        action_type=RemediationActionType.RESUME_BACKGROUND_JOB,
        description="Resume a previously paused ARGUS background job.",
        risk_level=RemediationRiskLevel.MEDIUM,
        adapter_kind=AdapterKind.CONTROL_PLANE,
        parameters=(
            ParameterSpec("job", "str", required=True, choices_from="background_jobs"),
        ),
        verification_plan=_control_plane_checks(),
        rollback_strategy=RollbackStrategy.INVERSE_ACTION,
        maximum_blast_radius=BlastRadiusScope.ENVIRONMENT,
        inverse_action=RemediationActionType.PAUSE_BACKGROUND_JOB,
        supports_canary=False,
        supports_autonomous_execution=False,
        control_kind="BACKGROUND_JOB",
        notes=(
            "Resuming releases accumulated work, so it is never autonomous even "
            "though pausing is.",
        ),
    )
)


# ---------------------------------------------------------------------------
# Lookup + validation
# ---------------------------------------------------------------------------


def get_definition(action_type: RemediationActionType) -> ActionDefinition:
    """The registry entry for an action type.

    Raises ``KeyError`` for an unregistered type: every caller is expected to
    hold a :class:`RemediationActionType`, and a missing entry means the enum and
    the registry have drifted apart — a bug, not a user error.
    """
    return REGISTRY[action_type]


def all_definitions() -> Sequence[ActionDefinition]:
    """Every registered action, in enum order."""
    return tuple(REGISTRY[t] for t in RemediationActionType)


def definition_summary(definition: ActionDefinition) -> dict[str, Any]:
    """A JSON-safe description of one registry entry (§4, exposed by the API)."""
    return {
        "action_type": definition.action_type.value,
        "description": definition.description,
        "risk_level": definition.risk_level.value,
        "adapter_kind": definition.adapter_kind.value,
        "parameters": [
            {
                "name": p.name,
                "kind": p.kind,
                "required": p.required,
                "choices": list(p.choices) if p.choices else None,
                "choices_from": p.choices_from,
                "minimum": p.minimum,
                "maximum": p.maximum,
                "default": p.default,
                "description": p.description,
            }
            for p in definition.parameters
        ],
        "verification_plan": [c.value for c in definition.verification_plan],
        "rollback_strategy": definition.rollback_strategy.value,
        "inverse_action": (
            definition.inverse_action.value if definition.inverse_action else None
        ),
        "maximum_blast_radius": definition.maximum_blast_radius.value,
        "production_effect": definition.production_effect,
        "supports_canary": definition.supports_canary,
        "supports_autonomous_execution": definition.supports_autonomous_execution,
        "requires_human_approval": definition_requires_human_approval(definition),
        "reversible": definition.reversible,
        "executable_in_build": definition.unavailable_reason is None,
        "unavailable_reason": definition.unavailable_reason,
        "notes": list(definition.notes),
    }


def definition_requires_human_approval(definition: ActionDefinition) -> bool:
    """Whether a definition *itself* demands a human (§4 of the principles).

    Independent of policy: an irreversible action, an action that touches
    production, or an action with no autonomous support always needs a person.
    """
    if not definition.reversible:
        return True
    if definition.production_effect:
        return True
    if not definition.supports_autonomous_execution:
        return True
    return False


def _resolve_choices(spec: ParameterSpec) -> Optional[tuple[str, ...]]:
    if spec.choices is not None:
        return spec.choices
    if spec.choices_from == "feature_flags":
        return tuple(settings.REMEDIATION_KNOWN_FEATURE_FLAGS)
    if spec.choices_from == "background_jobs":
        return tuple(settings.REMEDIATION_KNOWN_BACKGROUND_JOBS)
    return None


def _coerce(spec: ParameterSpec, value: Any) -> tuple[Any, Optional[str]]:
    """Coerce and bound-check one supplied value. Returns ``(value, error)``."""
    if spec.kind == "bool":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true", None
        return None, f"parameter '{spec.name}' must be a boolean"
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return None, f"parameter '{spec.name}' must be an integer"
        coerced: Any = value
    elif spec.kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, f"parameter '{spec.name}' must be a number"
        coerced = float(value)
    elif spec.kind == "uuid":
        import uuid as _uuid

        try:
            coerced = _uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            return None, f"parameter '{spec.name}' must be a UUID"
        if spec.choices:
            pass
        return coerced, None
    else:  # "str"
        if not isinstance(value, str):
            return None, f"parameter '{spec.name}' must be a string"
        coerced = value.strip()
        if not coerced:
            return None, f"parameter '{spec.name}' must not be empty"
        if len(coerced) > 200:
            return None, f"parameter '{spec.name}' is too long"

    # The offending value is echoed back: an operator who mistyped a job name
    # needs to see what ARGUS read, not merely that it was refused.
    choices = _resolve_choices(spec)
    if choices is not None and str(coerced) not in choices:
        return None, (
            f"parameter '{spec.name}' must be one of "
            f"{', '.join(sorted(choices))} (got '{coerced}')"
        )
    if spec.minimum is not None and coerced < spec.minimum:
        return None, (
            f"parameter '{spec.name}' must be >= {spec.minimum} (got {coerced})"
        )
    if spec.maximum is not None and coerced > spec.maximum:
        return None, (
            f"parameter '{spec.name}' must be <= {spec.maximum} (got {coerced})"
        )
    return coerced, None


def validate_parameters(
    action_type: RemediationActionType,
    supplied: Optional[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Validate parameters against the registry (§3, §16).

    Returns ``(clean_parameters, errors)``. Unknown keys are errors rather than
    ignored: a caller that supplies a parameter the registry does not define is
    either confused or probing, and both deserve a refusal. Defaults are filled
    in so a handler never has to guess.
    """
    definition = get_definition(action_type)
    supplied = dict(supplied or {})
    errors: list[str] = []
    clean: dict[str, Any] = {}

    known = {p.name for p in definition.parameters}
    for key in supplied:
        if key not in known:
            errors.append(
                f"unknown parameter '{key}' for {action_type.value}; "
                f"accepted: {', '.join(sorted(known)) or 'none'}"
            )

    for spec in definition.parameters:
        if spec.name in supplied:
            value, error = _coerce(spec, supplied[spec.name])
            if error:
                errors.append(error)
            else:
                clean[spec.name] = value
        elif spec.required:
            errors.append(f"missing required parameter '{spec.name}'")
        elif spec.default is not None:
            clean[spec.name] = spec.default

    # Normalise UUIDs to strings so the JSON column stays serializable and
    # comparable across SQLite/PostgreSQL.
    for key, value in list(clean.items()):
        if hasattr(value, "hex"):
            clean[key] = str(value)

    return clean, errors


def action_types_allowed_by_config() -> set[str]:
    """Action types execution is configured to attempt at all (§41)."""
    return set(settings.REMEDIATION_ENABLED_ACTION_TYPES)


def iter_inverse_names() -> (
    Iterable[tuple[RemediationActionType, RemediationActionType]]
):
    """Every declared inverse pair, for registry self-checks and tests."""
    for definition in REGISTRY.values():
        if definition.inverse_action is not None:
            yield definition.action_type, definition.inverse_action


__all__ = [
    "ActionDefinition",
    "ParameterSpec",
    "REGISTRY",
    "BLAST_RADIUS_ORDER",
    "RISK_ORDER",
    "action_types_allowed_by_config",
    "all_definitions",
    "blast_radius_rank",
    "definition_requires_human_approval",
    "definition_summary",
    "get_definition",
    "iter_inverse_names",
    "max_risk",
    "max_scope",
    "risk_rank",
    "validate_parameters",
]
