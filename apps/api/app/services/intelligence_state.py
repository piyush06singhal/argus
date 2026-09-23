"""ARGUS Learning Lifecycle (Phase 10 §4, §27, §72, §73).

One source of truth for which state changes the learning domain permits. Every
other module imports these maps instead of writing its own ``if status ==``
logic, because the failure mode a phase like this has is a *silently promoted
pattern* — a candidate that reached a dashboard reading as established fact.

The rules the maps encode:

* **Knowledge only moves forward.** ``CANDIDATE`` cannot jump to ``ACTIVE``
  without passing the states that record validation and review; ``DEPRECATED``
  and ``SUPERSEDED`` are terminal for learning purposes (history is kept, the
  pattern simply stops influencing anything).
* **Only VALIDATED/ACTIVE knowledge is recommendable.** Enforced by
  :func:`is_recommendable` and asserted again in the recommendation engine.
* **A review is a decision, not a status.** ``APPROVE``/``REJECT``/
  ``REQUEST_MORE_EVIDENCE``/``DEPRECATE`` are recorded as ``KnowledgeReview``
  rows and *then* applied, so the reason survives even if the status moves on.
* **Learning runs are finite.** ``COMPLETED``/``FAILED``/``CANCELLED`` are
  terminal; a run is never resumed, because a partially-applied pipeline that
  claims to still be RUNNING is how two runs end up writing the same pattern.
"""

from __future__ import annotations

from typing import Iterable, Optional

from app.models.intelligence import (
    DataProvenance,
    KnowledgeStatus,
    LearningEventType,
    LearningRunStatus,
    RecommendationStatus,
)


class IntelligenceStateError(ValueError):
    """Raised when a caller attempts a transition the lifecycle forbids."""


# --------------------------------------------------------------------------
# Knowledge (§4)
# --------------------------------------------------------------------------

#: Legal knowledge transitions. Everything not listed is refused.
KNOWLEDGE_TRANSITIONS: dict[KnowledgeStatus, frozenset[KnowledgeStatus]] = {
    KnowledgeStatus.CANDIDATE: frozenset(
        {
            KnowledgeStatus.VALIDATING,
            KnowledgeStatus.REJECTED,
            KnowledgeStatus.SUPERSEDED,
        }
    ),
    KnowledgeStatus.VALIDATING: frozenset(
        {
            KnowledgeStatus.VALIDATED,
            KnowledgeStatus.CANDIDATE,  # validation asked for more evidence
            KnowledgeStatus.REJECTED,
            KnowledgeStatus.DEPRECATED,
        }
    ),
    KnowledgeStatus.VALIDATED: frozenset(
        {
            KnowledgeStatus.ACTIVE,
            KnowledgeStatus.DEPRECATED,
            KnowledgeStatus.SUPERSEDED,
            KnowledgeStatus.REJECTED,
            # A human asked for more evidence. This only ever *lowers* belief,
            # so it cannot be used to reach a stronger state than validation
            # already granted (§72).
            KnowledgeStatus.CANDIDATE,
        }
    ),
    KnowledgeStatus.ACTIVE: frozenset(
        {
            KnowledgeStatus.DEPRECATED,
            KnowledgeStatus.SUPERSEDED,
        }
    ),
    KnowledgeStatus.DEPRECATED: frozenset(
        {
            KnowledgeStatus.VALIDATED,  # re-confirmed by the §25 decay sweep
            KnowledgeStatus.SUPERSEDED,
        }
    ),
    #: A rejected pattern stays rejected: only a human re-decides it (§72). The
    #: one exception is being *superseded* by a fresh, separately-validated
    #: candidate, which is retired → retired and can never raise belief.
    KnowledgeStatus.REJECTED: frozenset({KnowledgeStatus.SUPERSEDED}),
    KnowledgeStatus.SUPERSEDED: frozenset(),
}

#: The only statuses that may influence a recommendation (§4).
RECOMMENDABLE_KNOWLEDGE_STATUSES: frozenset[KnowledgeStatus] = frozenset(
    {KnowledgeStatus.VALIDATED, KnowledgeStatus.ACTIVE}
)

#: Statuses that mean "this row is history, not belief".
RETIRED_KNOWLEDGE_STATUSES: frozenset[KnowledgeStatus] = frozenset(
    {
        KnowledgeStatus.DEPRECATED,
        KnowledgeStatus.REJECTED,
        KnowledgeStatus.SUPERSEDED,
    }
)

#: Knowledge whose activation touches production behaviour, policy or security
#: must be reviewed by a human, whatever the §73 switch says (§73, §74).
HIGH_IMPACT_KNOWLEDGE_TYPES: frozenset[str] = frozenset(
    {
        # A remediation pattern informs what ARGUS may be asked to execute.
        "REMEDIATION_PATTERN",
        "RECOVERY_PATTERN",
        # A predictive pattern informs forecasts, which inform remediation.
        "PREDICTIVE_PATTERN",
    }
)

#: Human review decisions (§72).
REVIEW_DECISIONS: frozenset[str] = frozenset(
    {"APPROVE", "REJECT", "REQUEST_MORE_EVIDENCE", "DEPRECATE"}
)


def can_transition_knowledge(current: KnowledgeStatus, target: KnowledgeStatus) -> bool:
    """Whether ``current → target`` is a legal knowledge transition."""
    if current == target:
        return True
    return target in KNOWLEDGE_TRANSITIONS.get(current, frozenset())


def assert_knowledge_transition(
    current: KnowledgeStatus, target: KnowledgeStatus
) -> None:
    """Raise unless the transition is legal.

    Called before every write of ``ReliabilityKnowledge.status``, so an invalid
    state cannot be persisted by a caller that forgot the rules.
    """
    if not can_transition_knowledge(current, target):
        raise IntelligenceStateError(
            f"illegal knowledge transition {current.value} → {target.value}"
        )


def is_recommendable(status: KnowledgeStatus) -> bool:
    """Whether knowledge in this state may be cited by a recommendation (§4)."""
    return status in RECOMMENDABLE_KNOWLEDGE_STATUSES


def requires_human_review(
    knowledge_type: str,
    *,
    confidence_high_enough: bool,
    auto_activate_enabled: bool,
) -> bool:
    """Whether §73/§74 require a human before this knowledge may become ACTIVE.

    Three independent reasons to demand review, and any one is sufficient: the
    knowledge type is high-impact, an operator has not enabled autonomous
    activation, or the evidence is not strong enough to stand on its own.
    """
    if not auto_activate_enabled:
        return True
    if knowledge_type in HIGH_IMPACT_KNOWLEDGE_TYPES:
        return True
    return not confidence_high_enough


# --------------------------------------------------------------------------
# Recommendations (§43, §81)
# --------------------------------------------------------------------------

RECOMMENDATION_TRANSITIONS: dict[
    RecommendationStatus, frozenset[RecommendationStatus]
] = {
    RecommendationStatus.OPEN: frozenset(
        {
            RecommendationStatus.ACCEPTED,
            RecommendationStatus.DISMISSED,
            RecommendationStatus.EXPIRED,
        }
    ),
    #: An accepted recommendation's *outcome* is tracked separately, because
    #: "the engineer agreed" and "it worked" are different facts (§81).
    RecommendationStatus.ACCEPTED: frozenset(
        {
            RecommendationStatus.EFFECTIVE,
            RecommendationStatus.INEFFECTIVE,
            RecommendationStatus.REGRESSION_CAUSING,
        }
    ),
    RecommendationStatus.DISMISSED: frozenset(),
    RecommendationStatus.EXPIRED: frozenset(),
    RecommendationStatus.EFFECTIVE: frozenset(),
    RecommendationStatus.INEFFECTIVE: frozenset(),
    RecommendationStatus.REGRESSION_CAUSING: frozenset(),
}

#: Verdicts a recorded outcome may carry (§43).
RECOMMENDATION_VERDICTS: frozenset[str] = frozenset(
    {"EFFECTIVE", "INEFFECTIVE", "REGRESSION_CAUSING", "INCONCLUSIVE"}
)

_VERDICT_TO_STATUS: dict[str, RecommendationStatus] = {
    "EFFECTIVE": RecommendationStatus.EFFECTIVE,
    "INEFFECTIVE": RecommendationStatus.INEFFECTIVE,
    "REGRESSION_CAUSING": RecommendationStatus.REGRESSION_CAUSING,
}


def can_transition_recommendation(
    current: RecommendationStatus, target: RecommendationStatus
) -> bool:
    if current == target:
        return True
    return target in RECOMMENDATION_TRANSITIONS.get(current, frozenset())


def assert_recommendation_transition(
    current: RecommendationStatus, target: RecommendationStatus
) -> None:
    if not can_transition_recommendation(current, target):
        raise IntelligenceStateError(
            f"illegal recommendation transition {current.value} → {target.value}"
        )


def recommendation_status_for_verdict(verdict: str) -> Optional[RecommendationStatus]:
    """Map an outcome verdict onto the status it produces, if any.

    ``INCONCLUSIVE`` deliberately maps to ``None``: a verdict that says nothing
    must not move a recommendation into a terminal state that claims it did.
    """
    return _VERDICT_TO_STATUS.get(verdict.upper())


# --------------------------------------------------------------------------
# Learning runs (§27)
# --------------------------------------------------------------------------

RUN_TRANSITIONS: dict[LearningRunStatus, frozenset[LearningRunStatus]] = {
    LearningRunStatus.QUEUED: frozenset(
        {LearningRunStatus.RUNNING, LearningRunStatus.CANCELLED}
    ),
    LearningRunStatus.RUNNING: frozenset(
        {
            LearningRunStatus.COMPLETED,
            LearningRunStatus.FAILED,
            LearningRunStatus.CANCELLED,
        }
    ),
    LearningRunStatus.COMPLETED: frozenset(),
    LearningRunStatus.FAILED: frozenset(),
    LearningRunStatus.CANCELLED: frozenset(),
}

TERMINAL_RUN_STATUSES: frozenset[LearningRunStatus] = frozenset(
    {
        LearningRunStatus.COMPLETED,
        LearningRunStatus.FAILED,
        LearningRunStatus.CANCELLED,
    }
)


def can_transition_run(current: LearningRunStatus, target: LearningRunStatus) -> bool:
    if current == target:
        return True
    return target in RUN_TRANSITIONS.get(current, frozenset())


def assert_run_transition(
    current: LearningRunStatus, target: LearningRunStatus
) -> None:
    if not can_transition_run(current, target):
        raise IntelligenceStateError(
            f"illegal learning-run transition {current.value} → {target.value}"
        )


def is_terminal_run(status: LearningRunStatus) -> bool:
    return status in TERMINAL_RUN_STATUSES


# --------------------------------------------------------------------------
# Sample tiers (§33, §34)
# --------------------------------------------------------------------------


def sample_tier(
    sample_count: int,
    *,
    candidate: int,
    validation: int,
    high_confidence: int,
) -> str:
    """Name the evidence tier a sample size falls into.

    Returned as a string rather than an enum because it is explanatory: it ends
    up in ``validation`` JSON and in the report, where "INSUFFICIENT" needs to be
    read by a person.
    """
    if sample_count <= 0:
        return "NONE"
    if sample_count < candidate:
        return "INSUFFICIENT"
    if sample_count < validation:
        return "CANDIDATE"
    if sample_count < high_confidence:
        return "VALIDATION"
    return "STRONG"


# --------------------------------------------------------------------------
# Provenance and event hooks (§76, §77, §6)
# --------------------------------------------------------------------------

#: Event types consumed by default. Every one of them records something that
#: *finished*; nothing here fires on intent.
DEFAULT_ENABLED_EVENT_TYPES: tuple[LearningEventType, ...] = tuple(LearningEventType)

#: Provenance classes trusted by default. AI output and mocks are recorded but
#: not learned from, because a hypothesis is not an outcome (§77).
DEFAULT_TRUSTED_PROVENANCE: tuple[DataProvenance, ...] = (
    DataProvenance.OBSERVABILITY,
    DataProvenance.SYSTEM_GENERATED,
    DataProvenance.HUMAN_ENTERED,
)


def trusted_provenance_names(
    *, include_ai: bool, include_mock: bool
) -> tuple[DataProvenance, ...]:
    """The configured trust set, with the opt-in classes appended when allowed."""
    values = list(DEFAULT_TRUSTED_PROVENANCE)
    if include_ai:
        values.append(DataProvenance.AI_GENERATED)
    if include_mock:
        values.append(DataProvenance.MOCK)
    return tuple(values)


def coerce_enum(enum_cls: type, value: object) -> object:
    """Resolve a stored/JSON value back to an enum member.

    JSON columns and API filters hand back plain strings; comparing a string to
    an enum silently evaluates to ``False`` and produces knowledge that is
    quietly filed under the wrong scope. This raises instead.
    """
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value))  # type: ignore[call-arg]
    except ValueError as e:
        allowed: Iterable[str] = tuple(m.value for m in enum_cls)  # type: ignore[attr-defined]
        raise IntelligenceStateError(
            f"{value!r} is not a valid {enum_cls.__name__} (allowed: {sorted(allowed)})"
        ) from e


__all__ = [
    "DEFAULT_ENABLED_EVENT_TYPES",
    "DEFAULT_TRUSTED_PROVENANCE",
    "HIGH_IMPACT_KNOWLEDGE_TYPES",
    "IntelligenceStateError",
    "KNOWLEDGE_TRANSITIONS",
    "RECOMMENDABLE_KNOWLEDGE_STATUSES",
    "RECOMMENDATION_TRANSITIONS",
    "RECOMMENDATION_VERDICTS",
    "RETIRED_KNOWLEDGE_STATUSES",
    "REVIEW_DECISIONS",
    "RUN_TRANSITIONS",
    "TERMINAL_RUN_STATUSES",
    "assert_knowledge_transition",
    "assert_recommendation_transition",
    "assert_run_transition",
    "can_transition_knowledge",
    "can_transition_recommendation",
    "can_transition_run",
    "coerce_enum",
    "is_recommendable",
    "is_terminal_run",
    "recommendation_status_for_verdict",
    "requires_human_review",
    "sample_tier",
    "trusted_provenance_names",
]
