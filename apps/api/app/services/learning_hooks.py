"""ARGUS Learning Hooks (Phase 10 §6, §63, §76, §79).

The one-line bridge between Phases 1–9 and the learning inbox. Each function
takes a row that a previous phase has just finished with and records that the
outcome happened.

Three rules, all of them about not making things worse:

* **Best effort, never blocking.** Every hook goes through
  :func:`~app.services.learning_events.safely_publish_learning_event`, so a
  learning-table problem can never fail an incident resolution, a remediation
  verification or a forecast evaluation. Learning consumes history; it is not a
  dependency of it.
* **Provenance is explicit.** An ARGUS-computed outcome is
  ``SYSTEM_GENERATED``; something a person recorded is ``HUMAN_ENTERED``; an AI
  hypothesis is ``AI_GENERATED`` and is excluded from learning by default
  (§77). The classification is made here rather than guessed downstream.
* **The hook states the outcome, not the intent.** Hooks fire on completion —
  an incident that resolved, a verification that finished, an outcome that was
  scored. Nothing fires because something was requested.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.intelligence import DataProvenance, LearningEventType
from app.services.learning_events import safely_publish_learning_event

logger = logging.getLogger(__name__)


async def record_incident_completed(
    session: AsyncSession,
    *,
    incident: Any,
    actor: Optional[str] = None,
) -> None:
    """An incident reached a terminal status (§6 ``INCIDENT_RESOLVED``)."""
    if incident is None or incident.project_id is None:
        return
    await safely_publish_learning_event(
        session,
        project_id=incident.project_id,
        event_type=LearningEventType.INCIDENT_RESOLVED,
        subject_id=incident.id,
        occurred_at=_aware(incident.resolved_at) or datetime.now(timezone.utc),
        payload={
            "status": getattr(incident.status, "value", str(incident.status)),
            "severity": getattr(incident.severity, "value", str(incident.severity)),
            "primary_component_id": str(incident.primary_component_id)
            if incident.primary_component_id
            else None,
            "fingerprint": incident.fingerprint,
            "actor": actor,
        },
        provenance=DataProvenance.SYSTEM_GENERATED,
    )


async def record_remediation_completed(
    session: AsyncSession,
    *,
    action: Any,
    actor: Optional[str] = None,
) -> None:
    """A remediation reached a terminal status (§6 ``REMEDIATION_COMPLETED``)."""
    if action is None or action.project_id is None:
        return
    await safely_publish_learning_event(
        session,
        project_id=action.project_id,
        event_type=LearningEventType.REMEDIATION_COMPLETED,
        subject_id=action.id,
        occurred_at=_aware(action.completed_at) or datetime.now(timezone.utc),
        payload={
            "action_type": getattr(
                action.action_type, "value", str(action.action_type)
            ),
            "status": getattr(action.status, "value", str(action.status)),
            "outcome": getattr(action.outcome, "value", action.outcome)
            if action.outcome
            else None,
            "incident_id": str(action.incident_id) if action.incident_id else None,
            "component_id": str(action.component_id) if action.component_id else None,
            "actor": actor,
        },
        provenance=DataProvenance.SYSTEM_GENERATED,
        #: The same action can complete, be rolled back and complete again; the
        #: completion moment is part of the identity so each is a real event.
        dedup_extra=[getattr(action.status, "value", str(action.status))],
    )


async def record_rollback_completed(
    session: AsyncSession, *, action: Any, rollback: Any = None
) -> None:
    """A remediation was rolled back (§6)."""
    if action is None or action.project_id is None:
        return
    await safely_publish_learning_event(
        session,
        project_id=action.project_id,
        event_type=LearningEventType.ROLLBACK_COMPLETED,
        subject_id=action.id,
        occurred_at=(
            _aware(getattr(rollback, "completed_at", None))
            or _aware(getattr(action, "rollback_performed_at", None))
            or datetime.now(timezone.utc)
        ),
        payload={
            "action_type": getattr(
                action.action_type, "value", str(action.action_type)
            ),
            "rollback_status": getattr(rollback, "status", None)
            and getattr(rollback.status, "value", str(rollback.status)),
            "trigger": getattr(rollback, "trigger", None)
            and getattr(rollback.trigger, "value", str(rollback.trigger)),
            "incident_id": str(action.incident_id) if action.incident_id else None,
        },
        provenance=DataProvenance.SYSTEM_GENERATED,
        dedup_extra=[getattr(rollback, "status", "unknown")],
    )


async def record_patch_verification(
    session: AsyncSession,
    *,
    patch: Any,
    verification: Any = None,
    incident_id: Optional[uuid.UUID] = None,
) -> None:
    """A patch verification finished — verified, or verified with a regression (§6).

    The subject is the patch, and the incident is passed in explicitly rather
    than read off the patch: ``Patch`` links to its hypothesis, and the caller
    already knows the incident the fix was proposed for.
    """
    if patch is None or patch.project_id is None:
        return
    incident_id = incident_id or getattr(patch, "incident_id", None)
    regression = bool(getattr(verification, "regression_detected", False))
    await safely_publish_learning_event(
        session,
        project_id=patch.project_id,
        event_type=(
            LearningEventType.PATCH_REGRESSION_DETECTED
            if regression
            else LearningEventType.PATCH_VERIFIED
        ),
        subject_id=patch.id,
        occurred_at=_aware(getattr(verification, "completed_at", None))
        or datetime.now(timezone.utc),
        payload={
            "patch_status": getattr(patch.status, "value", str(patch.status)),
            "verification_status": _value_of(verification, "status"),
            "level": _value_of(verification, "level"),
            "regression_detected": regression,
            #: ``Patch.changed_files`` is a count, not a list: coercing it with
            #: ``list()`` would raise on the first real verification and drop the
            #: event — a hook that looks wired and is not.
            "changed_files": int(getattr(patch, "changed_files", 0) or 0),
            "incident_id": str(incident_id) if incident_id else None,
        },
        provenance=DataProvenance.SYSTEM_GENERATED,
    )


async def record_forecast_outcome(
    session: AsyncSession,
    *,
    outcome: Any,
    forecast: Any = None,
) -> None:
    """A forecast horizon elapsed and was scored (§6)."""
    if outcome is None or outcome.project_id is None:
        return
    value = getattr(outcome.outcome, "value", str(outcome.outcome))
    event_type = {
        "TRUE_POSITIVE": LearningEventType.FORECAST_CONFIRMED,
        "FALSE_POSITIVE": LearningEventType.FORECAST_FALSE_POSITIVE,
        "FALSE_NEGATIVE": LearningEventType.FORECAST_MISSED,
    }.get(value)
    if event_type is None:
        #: INCONCLUSIVE and TRUE_NEGATIVE are recorded as outcomes but are not
        #: events the pipeline learns from; they carry no signal about a failure.
        return
    await safely_publish_learning_event(
        session,
        project_id=outcome.project_id,
        event_type=event_type,
        subject_id=outcome.forecast_id,
        occurred_at=_aware(outcome.evaluated_at) or datetime.now(timezone.utc),
        payload={
            "outcome": value,
            "predicted_risk_level": getattr(outcome, "predicted_risk_level", None)
            and getattr(outcome.predicted_risk_level, "value", None),
            "component_id": str(outcome.component_id) if outcome.component_id else None,
            "matched_incident_id": str(outcome.matched_incident_id)
            if outcome.matched_incident_id
            else None,
        },
        provenance=DataProvenance.SYSTEM_GENERATED,
    )


async def record_reproduction_result(session: AsyncSession, *, experiment: Any) -> None:
    """A reproduction experiment finished (§6)."""
    if experiment is None or experiment.project_id is None:
        return
    #: §6. Phase 5 records ``SUCCESSFUL`` when the failure was actually
    #: reproduced; PARTIAL and INCONCLUSIVE are not confirmations and are
    #: published as ``REPRODUCTION_FAILED`` — the honest bucket for "it did not
    #: reproduce", rather than dropping the event.
    result = _value_of(experiment, "result")
    confirmed = str(result or "").upper() == "SUCCESSFUL"
    await safely_publish_learning_event(
        session,
        project_id=experiment.project_id,
        event_type=(
            LearningEventType.REPRODUCTION_CONFIRMED
            if confirmed
            else LearningEventType.REPRODUCTION_FAILED
        ),
        subject_id=experiment.id,
        occurred_at=_aware(experiment.completed_at) or datetime.now(timezone.utc),
        payload={
            "result": result,
            "incident_id": str(experiment.incident_id)
            if experiment.incident_id
            else None,
            "confidence": getattr(experiment, "confidence", None),
        },
        provenance=DataProvenance.SYSTEM_GENERATED,
    )


def _value_of(row: Any, attribute: str) -> Optional[str]:
    """The string value of an enum-backed attribute, or ``None``."""
    if row is None:
        return None
    value = getattr(row, attribute, None)
    if value is None:
        return None
    return getattr(value, "value", str(value))


async def record_root_cause_decision(
    session: AsyncSession,
    *,
    candidate: Any,
    confirmed: bool,
    actor: Optional[str] = None,
) -> None:
    """A root cause was confirmed or rejected — by a person, or by evidence."""
    if candidate is None or candidate.project_id is None:
        return
    await safely_publish_learning_event(
        session,
        project_id=candidate.project_id,
        event_type=(
            LearningEventType.ROOT_CAUSE_CONFIRMED
            if confirmed
            else LearningEventType.ROOT_CAUSE_REJECTED
        ),
        subject_id=candidate.id,
        occurred_at=datetime.now(timezone.utc),
        payload={
            "candidate_type": _value_of(candidate, "candidate_type"),
            "status": _value_of(candidate, "status"),
            "actor": actor,
        },
        #: §77. A person's confirmation is human evidence; an evidence-driven
        #: status change is system evidence. The distinction is recorded here
        #: because it is the difference between a fact and an inference.
        provenance=(
            DataProvenance.HUMAN_ENTERED if actor else DataProvenance.SYSTEM_GENERATED
        ),
    )


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


__all__ = [
    "record_forecast_outcome",
    "record_incident_completed",
    "record_patch_verification",
    "record_remediation_completed",
    "record_reproduction_result",
    "record_rollback_completed",
    "record_root_cause_decision",
]
