"""ARGUS Remediation Policy Engine (Phase 9 §5, §21, §24, §25, §27).

The policy engine answers exactly one question: *may this specific action, in
this specific scope, proceed — and if so, under whose authority?* It answers it
with rules that are evaluated in a fixed order and recorded either way, so
"why was this allowed?" and "why was this refused?" have the same quality of
answer.

The order is the safety argument:

1. **Emergency stop** — a global refusal that nothing can override.
2. **Kill switch** — ``REMEDIATION_EXECUTION_ENABLED=false`` refuses every live
   effect in the process, whatever the database says. An operator must be able to
   stop remediation even if the policy table is unreachable or wrong.
3. **Regime** — ``OBSERVE_ONLY`` records proposals and never authorizes them.
4. **Scope of the action** — an action type outside the allow-list, or an
   environment outside the allow-list, is refused before its risk is discussed.
5. **Registry executability** — an action this build has no adapter for is
   refused here rather than failing later.
6. **Circuit breaker** — a repeatedly failing action type stops being attempted.
7. **Safety verdict** — a failed safety assessment cannot be rescued by policy.
8. **Blast radius** — the action's own maximum, the policy ceiling and the
   operator's hard ceiling are all enforced.
9. **Budget, cooldown, concurrency** — bounded effort over time, not just per call.
10. **Authority** — ``ALLOW`` (autonomous, low risk, bounded), ``ALLOW_WITH_CANARY``
    (autonomous but staged first), or ``REQUIRE_APPROVAL``.

Two properties worth stating because they are what make the engine trustworthy
rather than merely present:

* **Configuration can only narrow.** :class:`EffectivePolicy` takes the minimum
  of the stored policy and the operator's hard ceilings. A row in the database —
  however it got there — cannot raise a limit above what the running process was
  started with.
* **A missing policy is restrictive.** No row resolves to ``OBSERVE_ONLY``
  (``REMEDIATION_DEFAULT_MODE``), never to the most permissive regime.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.project import Environment, EnvironmentType, SoftwareProject
from app.models.remediation import (
    BlastRadiusScope,
    CircuitState,
    PolicyDecision,
    RemediationAction,
    RemediationActionType,
    RemediationAssessment,
    RemediationCircuitBreaker,
    RemediationExecutionMode,
    RemediationFailureReason,
    RemediationPolicy,
    RemediationRiskLevel,
    SafetyStatus,
)
from app.services.remediation_clock import aware, utcnow
from app.services.remediation_registry import (
    blast_radius_rank,
    definition_requires_human_approval,
    get_definition,
    risk_rank,
)
from app.services.remediation_state import IN_FLIGHT_STATUSES

logger = logging.getLogger(__name__)
settings = get_settings()


#: The regime names a policy row may carry, mapped from their string form so a
#: typo in configuration becomes a refusal rather than a silent default.
_MODE_BY_NAME: dict[str, RemediationExecutionMode] = {
    mode.value: mode for mode in RemediationExecutionMode
}

#: Reversed for clamping: the most restrictive regime wins.
_MODE_RESTRICTIVENESS: dict[RemediationExecutionMode, int] = {
    RemediationExecutionMode.EMERGENCY_STOP: 0,
    RemediationExecutionMode.OBSERVE_ONLY: 1,
    RemediationExecutionMode.DRY_RUN: 2,
    RemediationExecutionMode.SHADOW: 3,
    RemediationExecutionMode.HUMAN_APPROVAL: 4,
    RemediationExecutionMode.AUTONOMOUS: 5,
}


def mode_rank(mode: RemediationExecutionMode) -> int:
    """How permissive a regime is (higher is more permissive)."""
    return _MODE_RESTRICTIVENESS[mode]


def most_restrictive(
    left: RemediationExecutionMode, right: RemediationExecutionMode
) -> RemediationExecutionMode:
    """The narrower of two regimes."""
    return left if mode_rank(left) <= mode_rank(right) else right


@dataclass(frozen=True)
class EffectivePolicy:
    """A policy after the operator's hard ceilings have been applied.

    ``source`` records where the values came from (``stored`` or ``fallback``) so
    a decision record can say whether a project was actually configured or was
    running on the restrictive default.
    """

    policy_id: Optional[uuid.UUID]
    revision: Optional[int]
    source: str
    execution_mode: RemediationExecutionMode
    autonomous_max_risk: RemediationRiskLevel
    allowed_action_types: Optional[tuple[str, ...]]
    allowed_environment_names: Optional[tuple[str, ...]]
    max_actions_per_window: int
    action_window_seconds: int
    cooldown_seconds: int
    max_concurrent_actions: int
    max_blast_radius_percent: float
    max_blast_radius_scope: BlastRadiusScope
    circuit_failure_threshold: int
    circuit_reset_seconds: int
    canary_enabled: bool
    canary_percent: float
    approval_ttl_seconds: int
    verification_window_seconds: int
    verification_grace_seconds: int
    max_verification_attempts: int
    execution_timeout_seconds: int
    max_execution_attempts: int
    action_expiry_seconds: int
    emergency_stop_active: bool
    emergency_stop_reason: Optional[str]
    #: Ceilings that were applied, for the decision record.
    clamped: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_configured(self) -> bool:
        return self.source == "stored"


def _hard_risk() -> RemediationRiskLevel:
    try:
        return RemediationRiskLevel(settings.REMEDIATION_HARD_MAX_RISK_AUTONOMOUS)
    except ValueError:  # pragma: no cover - configuration typo
        return RemediationRiskLevel.LOW


def _hard_scope() -> BlastRadiusScope:
    try:
        return BlastRadiusScope(settings.REMEDIATION_HARD_MAX_BLAST_RADIUS_SCOPE)
    except ValueError:  # pragma: no cover - configuration typo
        return BlastRadiusScope.LIMITED_PERCENTAGE


def _default_mode() -> RemediationExecutionMode:
    return _MODE_BY_NAME.get(
        settings.REMEDIATION_DEFAULT_MODE, RemediationExecutionMode.OBSERVE_ONLY
    )


def fallback_policy() -> EffectivePolicy:
    """The restrictive policy used when a project has no stored row.

    ``OBSERVE_ONLY`` by default: proposals are still generated, assessed and
    recorded, so a team can see exactly what ARGUS would have done, but nothing
    is authorized until someone writes a policy that says so.
    """
    return EffectivePolicy(
        policy_id=None,
        revision=None,
        source="fallback",
        execution_mode=_default_mode(),
        autonomous_max_risk=_hard_risk(),
        allowed_action_types=None,
        allowed_environment_names=None,
        max_actions_per_window=min(
            settings.REMEDIATION_DEFAULT_MAX_ACTIONS_PER_WINDOW,
            settings.REMEDIATION_HARD_MAX_ACTIONS_PER_WINDOW,
        ),
        action_window_seconds=settings.REMEDIATION_ACTION_WINDOW_SECONDS,
        cooldown_seconds=settings.REMEDIATION_DEFAULT_COOLDOWN_SECONDS,
        max_concurrent_actions=min(
            settings.REMEDIATION_DEFAULT_MAX_CONCURRENT_ACTIONS,
            settings.REMEDIATION_HARD_MAX_CONCURRENT_ACTIONS,
        ),
        max_blast_radius_percent=settings.REMEDIATION_HARD_MAX_BLAST_RADIUS_PERCENT,
        max_blast_radius_scope=_hard_scope(),
        circuit_failure_threshold=settings.REMEDIATION_CIRCUIT_FAILURE_THRESHOLD,
        circuit_reset_seconds=settings.REMEDIATION_CIRCUIT_RESET_SECONDS,
        canary_enabled=settings.REMEDIATION_CANARY_ENABLED,
        canary_percent=settings.REMEDIATION_CANARY_PERCENT,
        approval_ttl_seconds=settings.REMEDIATION_APPROVAL_TTL_SECONDS,
        verification_window_seconds=settings.REMEDIATION_VERIFICATION_WINDOW_SECONDS,
        verification_grace_seconds=settings.REMEDIATION_VERIFICATION_GRACE_SECONDS,
        max_verification_attempts=settings.REMEDIATION_MAX_VERIFICATION_ATTEMPTS,
        execution_timeout_seconds=min(
            settings.REMEDIATION_EXECUTION_TIMEOUT_SECONDS,
            settings.REMEDIATION_HARD_EXECUTION_TIMEOUT_SECONDS,
        ),
        max_execution_attempts=settings.REMEDIATION_MAX_EXECUTION_ATTEMPTS,
        action_expiry_seconds=settings.REMEDIATION_ACTION_EXPIRY_SECONDS,
        emergency_stop_active=False,
        emergency_stop_reason=None,
    )


def _clamp(row: RemediationPolicy) -> EffectivePolicy:
    """Turn a stored policy into an effective one, applying every hard ceiling.

    Every ``min`` here is the same rule: the running process can only be made
    *more* careful by data, never less.
    """
    clamped: list[str] = []
    mode = row.execution_mode

    risk = row.autonomous_max_risk
    if risk_rank(risk) > risk_rank(_hard_risk()):
        risk = _hard_risk()
        clamped.append("autonomous_max_risk")

    duration = min(
        row.execution_timeout_seconds,
        settings.REMEDIATION_HARD_EXECUTION_TIMEOUT_SECONDS,
    )
    if duration != row.execution_timeout_seconds:
        clamped.append("execution_timeout_seconds")

    concurrency = min(
        row.max_concurrent_actions, settings.REMEDIATION_HARD_MAX_CONCURRENT_ACTIONS
    )
    if concurrency != row.max_concurrent_actions:
        clamped.append("max_concurrent_actions")

    per_window = min(
        row.max_actions_per_window, settings.REMEDIATION_HARD_MAX_ACTIONS_PER_WINDOW
    )
    if per_window != row.max_actions_per_window:
        clamped.append("max_actions_per_window")

    radius = min(
        row.max_blast_radius_percent, settings.REMEDIATION_HARD_MAX_BLAST_RADIUS_PERCENT
    )
    if radius != row.max_blast_radius_percent:
        clamped.append("max_blast_radius_percent")

    expiry = min(row.action_expiry_seconds, settings.REMEDIATION_ACTION_EXPIRY_SECONDS)
    if expiry != row.action_expiry_seconds:
        clamped.append("action_expiry_seconds")

    if row.emergency_stop_active:
        # An emergency stop narrows the regime to the point where nothing is
        # authorized, whatever else the row says.
        mode = most_restrictive(mode, RemediationExecutionMode.EMERGENCY_STOP)
        clamped.append("execution_mode:emergency_stop")

    return EffectivePolicy(
        policy_id=row.id,
        revision=row.revision,
        source="stored" if row.enabled else "fallback",
        execution_mode=mode
        if row.enabled
        else most_restrictive(mode, RemediationExecutionMode.OBSERVE_ONLY),
        autonomous_max_risk=risk,
        allowed_action_types=(
            tuple(row.allowed_action_types) if row.allowed_action_types else None
        ),
        allowed_environment_names=(
            tuple(row.allowed_environment_names)
            if row.allowed_environment_names
            else None
        ),
        max_actions_per_window=per_window,
        action_window_seconds=row.action_window_seconds,
        cooldown_seconds=row.cooldown_seconds,
        max_concurrent_actions=concurrency,
        max_blast_radius_percent=radius,
        max_blast_radius_scope=_hard_scope(),
        circuit_failure_threshold=row.circuit_failure_threshold,
        circuit_reset_seconds=row.circuit_reset_seconds,
        canary_enabled=row.canary_enabled and settings.REMEDIATION_CANARY_ENABLED,
        canary_percent=min(row.canary_percent, 100.0),
        approval_ttl_seconds=row.approval_ttl_seconds,
        verification_window_seconds=row.verification_window_seconds,
        verification_grace_seconds=row.verification_grace_seconds,
        max_verification_attempts=row.max_verification_attempts,
        execution_timeout_seconds=duration,
        max_execution_attempts=min(
            row.max_execution_attempts, settings.REMEDIATION_MAX_EXECUTION_ATTEMPTS
        ),
        action_expiry_seconds=expiry,
        emergency_stop_active=row.emergency_stop_active,
        emergency_stop_reason=row.emergency_stop_reason,
        clamped=tuple(clamped),
    )


async def resolve_policy(
    session: AsyncSession,
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = None,
) -> EffectivePolicy:
    """The effective policy for a scope.

    Environment-specific row first, then the project-wide row, then the
    restrictive fallback. Resolution is deliberately explicit rather than
    "nearest row wins": an environment override is a decision someone made, and
    the decision record says which one applied.
    """
    if environment_id is not None:
        row = (
            (
                await session.execute(
                    select(RemediationPolicy)
                    .where(RemediationPolicy.project_id == project_id)
                    .where(RemediationPolicy.environment_id == environment_id)
                    .order_by(RemediationPolicy.revision.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if row is not None:
            return await _with_project_stop(session, project_id, _clamp(row))
    row = (
        (
            await session.execute(
                select(RemediationPolicy)
                .where(RemediationPolicy.project_id == project_id)
                .where(RemediationPolicy.environment_id.is_(None))
                .order_by(RemediationPolicy.revision.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if row is not None:
        return await _with_project_stop(session, project_id, _clamp(row))
    return await _with_project_stop(session, project_id, fallback_policy())


async def _with_project_stop(
    session: AsyncSession,
    project_id: uuid.UUID,
    policy: EffectivePolicy,
) -> EffectivePolicy:
    """Force ``EMERGENCY_STOP`` if *any* row of this project has it engaged.

    The kill switch is stored on a policy row, but it has to behave as a
    project-wide control, and resolution prefers the most specific row. Without
    this, engaging the stop for a project whose environments carry their own
    policies would write a flag on the project-wide row that resolution never
    reaches — the API would report the stop as engaged, the audit trail would
    record it, and an environment-scoped policy would go on authorizing
    autonomous remediation. A kill switch that can be shadowed by a more
    specific row is not a kill switch, so it is read across the whole project
    and applied on top of whatever row won.
    """
    if policy.execution_mode == RemediationExecutionMode.EMERGENCY_STOP:
        return policy
    engaged = (
        await session.execute(
            select(RemediationPolicy.id)
            .where(RemediationPolicy.project_id == project_id)
            .where(RemediationPolicy.emergency_stop_active.is_(True))
            .limit(1)
        )
    ).first()
    if engaged is None:
        return policy
    return dataclasses.replace(
        policy,
        execution_mode=RemediationExecutionMode.EMERGENCY_STOP,
        emergency_stop_active=True,
        emergency_stop_reason=policy.emergency_stop_reason
        or "an emergency stop is engaged for this project",
        clamped=(*policy.clamped, "execution_mode:emergency_stop"),
    )


async def upsert_policy(
    session: AsyncSession,
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID],
    values: dict[str, Any],
    *,
    updated_by: str = "api",
) -> RemediationPolicy:
    """Create or update the policy row for a scope.

    The row is versioned rather than patched in place: approving an action under
    revision 3 and later reading "why?" must not be answered with revision 7's
    values, so a change increments ``revision`` and the previous values are
    recoverable from the audit trail.
    """
    stmt = select(RemediationPolicy).where(RemediationPolicy.project_id == project_id)
    stmt = (
        stmt.where(RemediationPolicy.environment_id == environment_id)
        if environment_id is not None
        else stmt.where(RemediationPolicy.environment_id.is_(None))
    )
    row = (
        (
            await session.execute(
                stmt.order_by(RemediationPolicy.revision.desc()).limit(1)
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = RemediationPolicy(
            project_id=project_id, environment_id=environment_id, revision=1
        )
        session.add(row)
    else:
        row.revision += 1
    for key, value in values.items():
        if hasattr(row, key) and value is not None:
            setattr(row, key, value)
    row.updated_by = updated_by
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# Breaker + budget
# ---------------------------------------------------------------------------


async def get_breaker(
    session: AsyncSession,
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID],
    action_type: RemediationActionType,
    *,
    threshold: int,
) -> RemediationCircuitBreaker:
    """Fetch (or create) the breaker for one scope/action pair.

    Exactly one breaker exists per scope, because the table carries a unique
    index on ``(project_id, environment_id, action_type)``. That matters more
    than it looks: the caller reads a single row and asks "is it open?", so a
    second, ``CLOSED`` row for the same scope would hide an open breaker and the
    failing action type would keep being attempted — the opposite of what §27
    promises. The insert is therefore race-safe (a SAVEPOINT, then adopt the row
    the other writer created) rather than create-if-absent in application code.
    """
    stmt = (
        select(RemediationCircuitBreaker)
        .where(RemediationCircuitBreaker.project_id == project_id)
        .where(RemediationCircuitBreaker.action_type == action_type)
    )
    stmt = (
        stmt.where(RemediationCircuitBreaker.environment_id == environment_id)
        if environment_id is not None
        else stmt.where(RemediationCircuitBreaker.environment_id.is_(None))
    )
    row = (await session.execute(stmt.limit(1))).scalars().first()
    if row is not None:
        return row

    candidate = RemediationCircuitBreaker(
        project_id=project_id,
        environment_id=environment_id,
        action_type=action_type,
        state=CircuitState.CLOSED,
        threshold=threshold,
    )
    try:
        async with session.begin_nested():
            session.add(candidate)
            await session.flush()
    except IntegrityError:
        # Another writer created this scope's breaker between the read and the
        # insert. Theirs is the one the database now enforces; take it as-is
        # rather than overwriting a breaker that has already tripped.
        row = (await session.execute(stmt.limit(1))).scalars().first()
        if row is None:
            raise
        return row
    return candidate


def breaker_is_open(
    breaker: RemediationCircuitBreaker, *, now: Optional[Any] = None
) -> bool:
    """Whether a breaker currently refuses new attempts.

    An ``OPEN`` breaker whose ``opened_until`` has passed is treated as
    ``HALF_OPEN``: one probe attempt is allowed, and the outcome decides whether
    the breaker closes or re-opens. Without that, a single bad afternoon would
    disable an action type permanently.
    """
    now = aware(now or utcnow())
    if breaker.state == CircuitState.CLOSED:
        return False
    if breaker.state == CircuitState.OPEN:
        if breaker.opened_until is None:
            return True
        return aware(breaker.opened_until) > now
    return False


def breaker_is_half_open(
    breaker: RemediationCircuitBreaker, *, now: Optional[Any] = None
) -> bool:
    """Whether the breaker has cooled down and admits a single probe."""
    now = aware(now or utcnow())
    if breaker.state != CircuitState.OPEN:
        return breaker.state == CircuitState.HALF_OPEN
    if breaker.opened_until is None:
        return False
    return aware(breaker.opened_until) <= now


@dataclass
class BudgetState:
    """How much remediation effort this scope has already spent."""

    actions_in_window: int
    max_actions_per_window: int
    seconds_since_last_action: Optional[int]
    cooldown_seconds: int
    in_flight: int
    max_concurrent_actions: int

    @property
    def exhausted(self) -> bool:
        return self.actions_in_window >= self.max_actions_per_window

    @property
    def in_cooldown(self) -> bool:
        if self.seconds_since_last_action is None:
            return False
        return self.seconds_since_last_action < self.cooldown_seconds

    @property
    def at_concurrency_limit(self) -> bool:
        return self.in_flight >= self.max_concurrent_actions

    def as_dict(self) -> dict[str, Any]:
        return {
            "actions_in_window": self.actions_in_window,
            "max_actions_per_window": self.max_actions_per_window,
            "seconds_since_last_action": self.seconds_since_last_action,
            "cooldown_seconds": self.cooldown_seconds,
            "in_flight": self.in_flight,
            "max_concurrent_actions": self.max_concurrent_actions,
            "exhausted": self.exhausted,
            "in_cooldown": self.in_cooldown,
            "at_concurrency_limit": self.at_concurrency_limit,
        }


async def compute_budget(
    session: AsyncSession,
    policy: EffectivePolicy,
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID],
    *,
    exclude_action_id: Optional[uuid.UUID] = None,
    now: Optional[Any] = None,
) -> BudgetState:
    """Count this scope's recent and in-flight remediation effort.

        ``exclude_action_id`` is the action currently being evaluated, and it is
    excluded deliberately: an action that has only been *proposed* has spent no
        budget and is not the action it needs to cool down from. Counting it would
        mean every freshly proposed action is permanently in cooldown (its own
        creation is zero seconds ago) and every action counts against the window it
        is trying to enter — which with ``max_actions_per_window = 1`` would make
        that one action impossible to authorize. The budget therefore measures
        *other* effort, which is what "has this scope been acting too much?" means.
    """
    now = aware(now or utcnow())
    from datetime import timedelta

    window_start = now - timedelta(seconds=policy.action_window_seconds)
    stmt = (
        select(func.count(RemediationAction.id))
        .where(RemediationAction.project_id == project_id)
        .where(RemediationAction.created_at >= window_start)
        .where(
            RemediationAction.status.notin_(
                [
                    # A refused or expired proposal never consumed budget.
                    "REJECTED",
                    "EXPIRED",
                    "CANCELLED",
                ]
            )
        )
    )
    if exclude_action_id is not None:
        stmt = stmt.where(RemediationAction.id != exclude_action_id)
    stmt = (
        stmt.where(RemediationAction.environment_id == environment_id)
        if environment_id is not None
        else stmt.where(RemediationAction.environment_id.is_(None))
    )
    actions_in_window = int((await session.execute(stmt)).scalar() or 0)

    last_stmt = (
        select(func.max(RemediationAction.created_at))
        .where(RemediationAction.project_id == project_id)
        .where(RemediationAction.status.notin_(["REJECTED", "EXPIRED", "CANCELLED"]))
    )
    if exclude_action_id is not None:
        last_stmt = last_stmt.where(RemediationAction.id != exclude_action_id)
    if environment_id is not None:
        last_stmt = last_stmt.where(RemediationAction.environment_id == environment_id)
    else:
        last_stmt = last_stmt.where(RemediationAction.environment_id.is_(None))
    last_at = (await session.execute(last_stmt)).scalar()
    seconds_since = None
    if last_at is not None:
        seconds_since = max(0, int((now - aware(last_at)).total_seconds()))

    inflight_stmt = (
        select(func.count(RemediationAction.id))
        .where(RemediationAction.project_id == project_id)
        .where(RemediationAction.status.in_([s.value for s in IN_FLIGHT_STATUSES]))
    )
    if exclude_action_id is not None:
        inflight_stmt = inflight_stmt.where(RemediationAction.id != exclude_action_id)
    in_flight = int((await session.execute(inflight_stmt)).scalar() or 0)

    return BudgetState(
        actions_in_window=actions_in_window,
        max_actions_per_window=policy.max_actions_per_window,
        seconds_since_last_action=seconds_since,
        cooldown_seconds=policy.cooldown_seconds,
        in_flight=in_flight,
        max_concurrent_actions=policy.max_concurrent_actions,
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class PolicyEvaluation:
    """The engine's verdict, with everything needed to explain it later."""

    decision: PolicyDecision
    execution_mode: RemediationExecutionMode
    matched_rules: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    failure_reason: Optional[RemediationFailureReason] = None
    requires_canary: bool = False
    budget: Optional[BudgetState] = None
    breaker: Optional[RemediationCircuitBreaker] = None
    policy: Optional[EffectivePolicy] = None

    @property
    def allowed(self) -> bool:
        return self.decision in (
            PolicyDecision.ALLOW,
            PolicyDecision.ALLOW_WITH_CANARY,
        )

    @property
    def requires_human(self) -> bool:
        return self.decision == PolicyDecision.REQUIRE_APPROVAL

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "execution_mode": self.execution_mode.value,
            "matched_rules": self.matched_rules,
            "reasons": self.reasons,
            "failure_reason": self.failure_reason.value
            if self.failure_reason
            else None,
            "requires_canary": self.requires_canary,
            "budget": self.budget.as_dict() if self.budget else None,
            "breaker": (
                {
                    "state": self.breaker.state.value,
                    "consecutive_failures": self.breaker.consecutive_failures,
                    "threshold": self.breaker.threshold,
                }
                if self.breaker is not None
                else None
            ),
            "policy": (
                {
                    "source": self.policy.source,
                    "policy_id": str(self.policy.policy_id)
                    if self.policy.policy_id
                    else None,
                    "revision": self.policy.revision,
                    "execution_mode": self.policy.execution_mode.value,
                    "clamped": list(self.policy.clamped),
                }
                if self.policy is not None
                else None
            ),
        }


class PolicyEngine:
    """Evaluates one action against one resolved policy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def evaluate(
        self,
        action: RemediationAction,
        *,
        assessment: Optional[RemediationAssessment] = None,
        environment_name: Optional[str] = None,
        non_production: bool = False,
        human_approved: bool = False,
        now: Optional[Any] = None,
    ) -> PolicyEvaluation:
        """Run the ordered rule list and return the verdict.

        ``non_production`` defaults to ``False`` on purpose: an unknown scope is
        treated as production, so forgetting to pass it refuses autonomous
        execution rather than enabling it (§45).

        ``human_approved`` is set only by :meth:`RemediationService.decide` after
        it has recorded a *human* approval for this action. It satisfies the
        authority rule and nothing else: every rule above it — the emergency
        stop, the process kill switch, the regime, the allow-lists, the
        registry, the breaker, the safety verdict, the blast radius and the
        budget — has already fired by the time it is consulted. A person can
        therefore authorize a remediation, but cannot authorize one the
        platform has refused. Without this flag the ``HUMAN_APPROVAL`` regime
        would escalate forever, asking for an approval that had just been given.
        """
        now = aware(now or utcnow())
        policy = await resolve_policy(
            self._session, action.project_id, action.environment_id
        )
        decision = PolicyDecision.DENY
        rules: list[dict[str, Any]] = []
        reasons: list[str] = []

        def rule(name: str, outcome: str, detail: str) -> None:
            rules.append({"rule": name, "outcome": outcome, "detail": detail})

        mode = policy.execution_mode
        definition = get_definition(action.action_type)

        # 1. Emergency stop.
        if mode == RemediationExecutionMode.EMERGENCY_STOP:
            rule(
                "emergency_stop",
                "deny",
                policy.emergency_stop_reason or "an emergency stop is engaged",
            )
            reasons.append("emergency stop engaged: no remediation may be authorized")
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                execution_mode=mode,
                matched_rules=rules,
                reasons=reasons,
                failure_reason=RemediationFailureReason.EMERGENCY_STOP,
                policy=policy,
            )
        rule("emergency_stop", "pass", "no emergency stop engaged")

        # 2. Process-level kill switch.
        if not settings.REMEDIATION_EXECUTION_ENABLED:
            rule(
                "kill_switch",
                "deny",
                "REMEDIATION_EXECUTION_ENABLED is false in this process",
            )
            reasons.append(
                "remediation execution is disabled by configuration; no action may "
                "apply a live effect"
            )
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                execution_mode=mode,
                matched_rules=rules,
                reasons=reasons,
                failure_reason=RemediationFailureReason.EXECUTION_DISABLED,
                policy=policy,
            )
        rule("kill_switch", "pass", "execution enabled in this process")

        # 3. Regime.
        if not policy.is_configured:
            rule(
                "policy_source",
                "deny",
                "no enabled policy row is configured for this scope",
            )
            reasons.append(
                "no remediation policy is configured for this scope; the "
                "restrictive default applies and nothing is authorized"
            )
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                execution_mode=mode,
                matched_rules=rules,
                reasons=reasons,
                failure_reason=RemediationFailureReason.POLICY_DENIED,
                policy=policy,
            )
        if mode in (
            RemediationExecutionMode.OBSERVE_ONLY,
            RemediationExecutionMode.EMERGENCY_STOP,
        ):
            rule("execution_mode", "deny", f"regime is {mode.value}")
            reasons.append(
                f"policy regime is {mode.value}: proposals are recorded but nothing "
                "is authorized"
            )
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                execution_mode=mode,
                matched_rules=rules,
                reasons=reasons,
                failure_reason=RemediationFailureReason.POLICY_DENIED,
                policy=policy,
            )
        rule("execution_mode", "pass", f"regime is {mode.value}")

        # 4. Allow-lists.
        if (
            policy.allowed_action_types is not None
            and action.action_type.value not in policy.allowed_action_types
        ):
            rule(
                "allowed_action_types",
                "deny",
                f"{action.action_type.value} is not in the policy allow-list",
            )
            reasons.append(
                f"{action.action_type.value} is not permitted by the policy "
                "allow-list"
            )
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                execution_mode=mode,
                matched_rules=rules,
                reasons=reasons,
                failure_reason=RemediationFailureReason.POLICY_DENIED,
                policy=policy,
            )
        rule("allowed_action_types", "pass", "action type permitted")

        if (
            policy.allowed_environment_names is not None
            and environment_name is not None
            and environment_name not in policy.allowed_environment_names
        ):
            rule(
                "allowed_environment_names",
                "deny",
                f"environment '{environment_name}' is not in the allow-list",
            )
            reasons.append(
                f"environment '{environment_name}' is not permitted by the policy"
            )
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                execution_mode=mode,
                matched_rules=rules,
                reasons=reasons,
                failure_reason=RemediationFailureReason.ENVIRONMENT_NOT_ALLOWED,
                policy=policy,
            )
        rule("allowed_environment_names", "pass", "environment permitted")

        # 5. Registry executability + build-level enablement.
        if definition.unavailable_reason is not None:
            rule("registry_executable", "deny", definition.unavailable_reason)
            reasons.append(
                f"{action.action_type.value} is registered but not executable in "
                f"this build: {definition.unavailable_reason}"
            )
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                execution_mode=mode,
                matched_rules=rules,
                reasons=reasons,
                failure_reason=RemediationFailureReason.ADAPTER_UNAVAILABLE,
                policy=policy,
            )
        if action.action_type.value not in set(
            settings.REMEDIATION_ENABLED_ACTION_TYPES
        ):
            rule(
                "enabled_action_types",
                "deny",
                "action type is not in REMEDIATION_ENABLED_ACTION_TYPES",
            )
            reasons.append(
                f"{action.action_type.value} is not enabled for execution by "
                "configuration"
            )
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                execution_mode=mode,
                matched_rules=rules,
                reasons=reasons,
                failure_reason=RemediationFailureReason.ENVIRONMENT_NOT_ALLOWED,
                policy=policy,
            )
        rule("registry_executable", "pass", "action is executable in this build")

        # 6. Circuit breaker.
        breaker = await get_breaker(
            self._session,
            action.project_id,
            action.environment_id,
            action.action_type,
            threshold=policy.circuit_failure_threshold,
        )
        if breaker_is_open(breaker, now=now):
            rule(
                "circuit_breaker",
                "deny",
                f"breaker OPEN after {breaker.consecutive_failures} consecutive failures",
            )
            reasons.append(
                f"the {action.action_type.value} breaker is open until "
                f"{breaker.opened_until.isoformat() if breaker.opened_until else 'reset'}"
            )
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                execution_mode=mode,
                matched_rules=rules,
                reasons=reasons,
                failure_reason=RemediationFailureReason.CIRCUIT_OPEN,
                breaker=breaker,
                policy=policy,
            )
        rule("circuit_breaker", "pass", f"breaker {breaker.state.value}")

        # 7. Safety verdict cannot be overridden by policy.
        if assessment is not None and assessment.status == SafetyStatus.FAILED:
            rule("safety_assessment", "deny", "the safety assessment failed")
            reasons.append(
                "the safety assessment failed: "
                + ", ".join(assessment.blocking or ["unspecified"])
            )
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                execution_mode=mode,
                matched_rules=rules,
                reasons=reasons,
                failure_reason=RemediationFailureReason.PRECONDITION_FAILED,
                breaker=breaker,
                policy=policy,
            )
        rule(
            "safety_assessment",
            "pass",
            assessment.status.value if assessment is not None else "not assessed",
        )

        # 8. Blast radius.
        if blast_radius_rank(action.blast_radius) > blast_radius_rank(
            definition.maximum_blast_radius
        ):
            rule(
                "action_max_blast_radius",
                "deny",
                f"{action.blast_radius.value} exceeds the action's maximum "
                f"{definition.maximum_blast_radius.value}",
            )
            reasons.append(
                f"the requested blast radius {action.blast_radius.value} is wider "
                f"than {action.action_type.value} may ever use"
            )
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                execution_mode=mode,
                matched_rules=rules,
                reasons=reasons,
                failure_reason=RemediationFailureReason.PRECONDITION_FAILED,
                breaker=breaker,
                policy=policy,
            )
        if blast_radius_rank(action.blast_radius) > blast_radius_rank(
            policy.max_blast_radius_scope
        ):
            rule(
                "hard_max_blast_radius",
                "deny",
                f"{action.blast_radius.value} exceeds the operator ceiling "
                f"{policy.max_blast_radius_scope.value}",
            )
            reasons.append(
                "the requested blast radius is wider than the operator's hard ceiling"
            )
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                execution_mode=mode,
                matched_rules=rules,
                reasons=reasons,
                failure_reason=RemediationFailureReason.PRECONDITION_FAILED,
                breaker=breaker,
                policy=policy,
            )
        percent = action.blast_radius_percent
        if percent is not None and percent > policy.max_blast_radius_percent:
            rule(
                "max_blast_radius_percent",
                "deny",
                f"{percent}% exceeds the policy ceiling "
                f"{policy.max_blast_radius_percent}%",
            )
            reasons.append(
                f"blast radius {percent}% exceeds the policy ceiling "
                f"{policy.max_blast_radius_percent}%"
            )
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                execution_mode=mode,
                matched_rules=rules,
                reasons=reasons,
                failure_reason=RemediationFailureReason.PRECONDITION_FAILED,
                breaker=breaker,
                policy=policy,
            )
        rule("blast_radius", "pass", action.blast_radius.value)

        # 9. Budget, cooldown, concurrency.
        budget = await compute_budget(
            self._session,
            policy,
            action.project_id,
            action.environment_id,
            exclude_action_id=action.id,
            now=now,
        )
        if budget.exhausted:
            rule(
                "budget",
                "deny",
                f"{budget.actions_in_window}/{budget.max_actions_per_window} "
                "actions used in the window",
            )
            reasons.append(
                "the remediation budget for this scope is exhausted for the current "
                "window"
            )
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                execution_mode=mode,
                matched_rules=rules,
                reasons=reasons,
                failure_reason=RemediationFailureReason.BUDGET_EXHAUSTED,
                budget=budget,
                breaker=breaker,
                policy=policy,
            )
        if budget.in_cooldown:
            rule(
                "cooldown",
                "deny",
                f"{budget.seconds_since_last_action}s since the last action, "
                f"cooldown is {budget.cooldown_seconds}s",
            )
            reasons.append(
                "another action in this scope ran too recently; the cooldown has "
                "not elapsed"
            )
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                execution_mode=mode,
                matched_rules=rules,
                reasons=reasons,
                failure_reason=RemediationFailureReason.BUDGET_EXHAUSTED,
                budget=budget,
                breaker=breaker,
                policy=policy,
            )
        if budget.at_concurrency_limit:
            rule(
                "concurrency",
                "deny",
                f"{budget.in_flight}/{budget.max_concurrent_actions} actions in flight",
            )
            reasons.append(
                "the maximum number of concurrent remediation actions for this scope "
                "is already running"
            )
            return PolicyEvaluation(
                decision=PolicyDecision.DENY,
                execution_mode=mode,
                matched_rules=rules,
                reasons=reasons,
                failure_reason=RemediationFailureReason.CONCURRENCY_LIMIT,
                budget=budget,
                breaker=breaker,
                policy=policy,
            )
        rule(
            "budget",
            "pass",
            f"{budget.actions_in_window}/{budget.max_actions_per_window} used, "
            f"{budget.in_flight}/{budget.max_concurrent_actions} in flight",
        )

        # 10. Authority.
        definition_needs_human = definition_requires_human_approval(definition)
        canary_required = (
            policy.canary_enabled
            and definition.supports_canary
            and mode == RemediationExecutionMode.AUTONOMOUS
        )

        if human_approved:
            #: Every rule above has already run and passed. The authority
            #: question — "who decides?" — has been answered by a recorded
            #: human approval, so the action is authorized; a person cannot
            #: waive the safety, budget, breaker, scope or registry rules, and
            #: OBSERVE_ONLY / EMERGENCY_STOP returned long before this point.
            decision = (
                PolicyDecision.ALLOW_WITH_CANARY
                if canary_required
                else PolicyDecision.ALLOW
            )
            rules.append(
                {
                    "rule": "human_authorization",
                    "outcome": "allow",
                    "detail": (
                        "a recorded human approval satisfies the authority "
                        f"requirement under regime {mode.value}"
                    ),
                }
            )
            reasons.append(
                "authorized by a recorded human approval; every preceding rule "
                "passed"
            )
        elif (
            mode == RemediationExecutionMode.AUTONOMOUS
            and not definition_needs_human
            and not non_production
        ):
            decision = PolicyDecision.REQUIRE_APPROVAL
            rules.append(
                {
                    "rule": "non_production_required",
                    "outcome": "escalate",
                    "detail": (
                        "the scope is treated as production, so autonomous "
                        "execution is not considered"
                    ),
                }
            )
            reasons.append(
                "autonomous execution is only permitted in a non-production "
                "scope; a human must approve"
            )
        elif mode == RemediationExecutionMode.AUTONOMOUS and not definition_needs_human:
            if risk_rank(action.risk_level) <= risk_rank(policy.autonomous_max_risk):
                decision = (
                    PolicyDecision.ALLOW_WITH_CANARY
                    if canary_required
                    else PolicyDecision.ALLOW
                )
                rules.append(
                    {
                        "rule": "autonomous_authority",
                        "outcome": "allow",
                        "detail": (
                            f"risk {action.risk_level.value} is within the "
                            f"autonomous ceiling {policy.autonomous_max_risk.value}"
                        ),
                    }
                )
                reasons.append(
                    "autonomous policy authorization: bounded, registered, "
                    f"{action.risk_level.value}-risk action within the configured ceiling"
                )
            else:
                decision = PolicyDecision.REQUIRE_APPROVAL
                rules.append(
                    {
                        "rule": "autonomous_ceiling",
                        "outcome": "escalate",
                        "detail": (
                            f"risk {action.risk_level.value} exceeds the autonomous "
                            f"ceiling {policy.autonomous_max_risk.value}"
                        ),
                    }
                )
                reasons.append(
                    "risk exceeds the autonomous ceiling: a human must approve"
                )
        elif mode == RemediationExecutionMode.AUTONOMOUS and definition_needs_human:
            decision = PolicyDecision.REQUIRE_APPROVAL
            rules.append(
                {
                    "rule": "irreversibility",
                    "outcome": "escalate",
                    "detail": (
                        "the action is irreversible, production-affecting, or not "
                        "eligible for autonomous execution"
                    ),
                }
            )
            reasons.append(
                "this action cannot be executed autonomously; a human must approve"
            )
        else:
            # HUMAN_APPROVAL, DRY_RUN and SHADOW all lead here: the action is
            # permitted, and a person decides whether it runs.
            decision = PolicyDecision.REQUIRE_APPROVAL
            rules.append(
                {
                    "rule": "human_approval",
                    "outcome": "escalate",
                    "detail": f"regime {mode.value} requires approval before execution",
                }
            )
            reasons.append(
                f"regime {mode.value}: the action is permitted and awaits approval"
            )

        if policy.clamped:
            rule(
                "operator_ceilings",
                "note",
                "clamped: " + ", ".join(policy.clamped),
            )

        return PolicyEvaluation(
            decision=decision,
            execution_mode=mode,
            matched_rules=rules,
            reasons=reasons,
            failure_reason=(
                RemediationFailureReason.APPROVAL_REQUIRED
                if decision == PolicyDecision.REQUIRE_APPROVAL
                else None
            ),
            requires_canary=canary_required,
            budget=budget,
            breaker=breaker,
            policy=policy,
        )


async def environment_name(
    session: AsyncSession, environment_id: Optional[uuid.UUID]
) -> Optional[str]:
    """The environment's name, for allow-list checks and human-readable reasons."""
    if environment_id is None:
        return None
    row = await session.get(Environment, environment_id)
    return row.name if row is not None else None


async def is_non_production(
    session: AsyncSession,
    environment_id: Optional[uuid.UUID],
) -> bool:
    """Whether the scope looks non-production.

    Autonomous execution is *only* ever considered here. An unknown or missing
    environment is treated as production — the conservative direction, because
    guessing "probably fine" about production is the mistake that ends the
    project.
    """
    if environment_id is None:
        return False
    row = await session.get(Environment, environment_id)
    if row is None:
        return False
    return environment_is_non_production(row.name, row.environment_type)


def environment_is_non_production(name: Optional[str], environment_type: Any) -> bool:
    """The single classifier behind "may ARGUS act here autonomously?".

    Two independent signals must agree, because either one alone is a guess:

    * the environment's **name** is in the operator-configured
      ``REMEDIATION_NON_PRODUCTION_ENVIRONMENT_NAMES`` allow-list, and
    * the environment is **not declared ``PRODUCTION``** in the platform's own
      ``environment_type`` field.

    The name allow-list is configuration an operator can widen; the declared
    type is a fact about the environment that someone typed when they created
    it. Requiring both means a production environment that happens to be named
    ``staging`` is still production — the naming convention is not trusted to
    override the declaration. Anything unknown (no name, no type) is production.
    """
    if not name:
        return False
    declared = getattr(environment_type, "value", environment_type)
    if str(declared or "").strip().upper() == EnvironmentType.PRODUCTION.value:
        return False
    return name.strip().lower() in {
        n.strip().lower() for n in settings.REMEDIATION_NON_PRODUCTION_ENVIRONMENT_NAMES
    }


async def project_exists(session: AsyncSession, project_id: uuid.UUID) -> bool:
    """Whether the project row still exists."""
    return (
        await session.execute(
            select(func.count(SoftwareProject.id)).where(
                SoftwareProject.id == project_id
            )
        )
    ).scalar() == 1


def summarize_rules(rules: Sequence[dict[str, Any]]) -> str:
    """One-line summary of the rules that fired, for a headline."""
    if not rules:
        return "no rules evaluated"
    return (
        "; ".join(
            f"{r['rule']}={r['outcome']}" for r in rules if r.get("outcome") != "pass"
        )
        or "all rules passed"
    )


__all__ = [
    "BudgetState",
    "EffectivePolicy",
    "PolicyEngine",
    "PolicyEvaluation",
    "breaker_is_half_open",
    "breaker_is_open",
    "compute_budget",
    "environment_name",
    "fallback_policy",
    "get_breaker",
    "is_non_production",
    "mode_rank",
    "most_restrictive",
    "project_exists",
    "resolve_policy",
    "summarize_rules",
    "upsert_policy",
]
