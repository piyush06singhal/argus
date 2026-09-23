"""ARGUS Platform Hooks (Phase 11 §9, §10).

The bridge from Phases 3–10 into the unified event stream. Each function takes a
row a previous phase has just finished with and publishes the corresponding
platform event.

Three rules, mirroring the learning hooks they sit beside:

* **Best effort, never blocking.** Every hook goes through
  ``safely_publish_event``, so a platform-event problem can never fail an
  incident resolution, a verification or a forecast evaluation. The control plane
  consumes history; it is not a dependency of it.
* **One correlation id per situation.** Events about one incident share
  ``incident:<id>``, so §10's chain is one query rather than a guess. The id is
  derived from the anchored subject, never from a timestamp comparison.
* **The hook states what happened, not what was requested.** Hooks fire on
  completion — an incident that resolved, a patch that verified, a forecast that
  was generated. Nothing fires because something was asked for.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.platform import PlatformEventType
from app.services.platform_events import correlation_id_for, safely_publish_event

logger = logging.getLogger(__name__)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _value_of(row: Any, attribute: str) -> Optional[str]:
    if row is None:
        return None
    value = getattr(row, attribute, None)
    if value is None:
        return None
    return getattr(value, "value", str(value))


async def record_incident_event(
    session: AsyncSession,
    *,
    incident: Any,
    created: bool = False,
) -> None:
    """An incident was opened or updated (§9 ``INCIDENT_CREATED``/``_UPDATED``)."""
    if incident is None or incident.project_id is None:
        return
    await safely_publish_event(
        session,
        project_id=incident.project_id,
        environment_id=getattr(incident, "environment_id", None),
        event_type=(
            PlatformEventType.INCIDENT_CREATED
            if created
            else PlatformEventType.INCIDENT_UPDATED
        ),
        source="incident_manager",
        subject_type="incident",
        subject_id=incident.id,
        component_id=getattr(incident, "primary_component_id", None),
        correlation_id=correlation_id_for(kind="incident", subject_id=incident.id),
        occurred_at=_aware(getattr(incident, "detected_at", None)),
        payload={
            "title": incident.title,
            "severity": _value_of(incident, "severity"),
            "status": _value_of(incident, "status"),
            "fingerprint": getattr(incident, "fingerprint", None),
        },
        dedup_extra=[_value_of(incident, "status") or ""],
    )


async def record_analysis_completed(
    session: AsyncSession, *, analysis: Any, incident_id: Optional[uuid.UUID] = None
) -> None:
    """A causal analysis completed (§9 ``RCA_COMPLETED``)."""
    if analysis is None or analysis.project_id is None:
        return
    anchor = incident_id or getattr(analysis, "incident_id", None)
    await safely_publish_event(
        session,
        project_id=analysis.project_id,
        event_type=PlatformEventType.RCA_COMPLETED,
        source="causal_analysis",
        subject_type="incident" if anchor else "analysis",
        subject_id=anchor or analysis.id,
        occurred_at=_aware(getattr(analysis, "completed_at", None)),
        correlation_id=(
            correlation_id_for(kind="incident", subject_id=anchor) if anchor else None
        ),
        payload={
            "analysis_id": str(analysis.id),
            "status": _value_of(analysis, "status"),
            "summary": getattr(analysis, "summary", None),
        },
        dedup_extra=[str(analysis.id)],
    )


async def record_root_cause_decided(
    session: AsyncSession,
    *,
    candidate: Any,
    incident_id: Optional[uuid.UUID] = None,
    confirmed: bool,
) -> None:
    """A root cause candidate was confirmed or rejected (§9 ``RCA_COMPLETED``)."""
    if candidate is None or candidate.project_id is None:
        return
    await safely_publish_event(
        session,
        project_id=candidate.project_id,
        event_type=PlatformEventType.RCA_COMPLETED,
        source="causal_analysis",
        subject_type="incident" if incident_id else "candidate",
        subject_id=incident_id or candidate.id,
        component_id=getattr(candidate, "component_id", None),
        occurred_at=_aware(getattr(candidate, "updated_at", None)),
        correlation_id=(
            correlation_id_for(kind="incident", subject_id=incident_id)
            if incident_id
            else None
        ),
        payload={
            "candidate_id": str(candidate.id),
            "candidate_type": _value_of(candidate, "candidate_type"),
            "status": _value_of(candidate, "status"),
            "confirmed": confirmed,
        },
        dedup_extra=[
            "candidate",
            str(candidate.id),
            _value_of(candidate, "status") or "",
        ],
    )


async def record_reproduction_finished(
    session: AsyncSession, *, experiment: Any
) -> None:
    """A reproduction experiment finished (§9 ``REPRODUCTION_COMPLETED``)."""
    if experiment is None or experiment.project_id is None:
        return
    incident_id = getattr(experiment, "incident_id", None)
    await safely_publish_event(
        session,
        project_id=experiment.project_id,
        environment_id=getattr(experiment, "environment_id", None),
        event_type=PlatformEventType.REPRODUCTION_COMPLETED,
        source="reproduction",
        subject_type="incident" if incident_id else "experiment",
        subject_id=incident_id or experiment.id,
        component_id=getattr(experiment, "component_id", None),
        occurred_at=_aware(getattr(experiment, "completed_at", None)),
        correlation_id=(
            correlation_id_for(kind="incident", subject_id=incident_id)
            if incident_id
            else None
        ),
        payload={
            "experiment_id": str(experiment.id),
            "status": _value_of(experiment, "status"),
            "result": _value_of(experiment, "result"),
            "confidence": getattr(experiment, "confidence", None),
        },
        dedup_extra=[str(experiment.id)],
    )


async def record_patch_verified(
    session: AsyncSession,
    *,
    patch: Any,
    verification: Any = None,
    incident_id: Optional[uuid.UUID] = None,
) -> None:
    """A patch verification finished (§9 ``PATCH_VERIFIED``).
    Published even when the verification *failed*: ``PATCH_VERIFIED`` is the
    event type for "a verification concluded", and its payload carries the
    verdict.
    """
    if patch is None or patch.project_id is None:
        return
    anchor = incident_id or getattr(patch, "incident_id", None)
    await safely_publish_event(
        session,
        project_id=patch.project_id,
        event_type=PlatformEventType.PATCH_VERIFIED,
        source="fix_verification",
        subject_type="patch",
        subject_id=patch.id,
        occurred_at=(
            _aware(getattr(verification, "completed_at", None))
            or datetime.now(timezone.utc)
        ),
        correlation_id=(
            correlation_id_for(kind="incident", subject_id=anchor) if anchor else None
        ),
        payload={
            "patch_status": _value_of(patch, "status"),
            "verification_status": _value_of(verification, "status"),
            "level": _value_of(verification, "level"),
            "regression_detected": bool(
                getattr(verification, "regression_detected", False)
            ),
            "incident_id": str(anchor) if anchor else None,
        },
        dedup_extra=[str(verification.id) if verification is not None else "none"],
    )


async def record_forecast_generated(session: AsyncSession, *, forecast: Any) -> None:
    """A forecast was generated (§9 ``FORECAST_GENERATED`` / ``RISK_CHANGED``)."""
    if forecast is None or forecast.project_id is None:
        return
    risk_level = _value_of(forecast, "risk_level")
    from app.models.reliability import ForecastRiskLevel

    elevated = risk_level in (
        ForecastRiskLevel.HIGH.value,
        ForecastRiskLevel.CRITICAL.value,
    )
    await safely_publish_event(
        session,
        project_id=forecast.project_id,
        environment_id=getattr(forecast, "environment_id", None),
        event_type=(
            PlatformEventType.RISK_CHANGED
            if elevated
            else PlatformEventType.FORECAST_GENERATED
        ),
        source="reliability_forecast",
        subject_type="forecast",
        subject_id=forecast.id,
        component_id=getattr(forecast, "component_id", None),
        occurred_at=_aware(getattr(forecast, "generated_at", None)),
        payload={
            "prediction_type": _value_of(forecast, "prediction_type"),
            "risk_level": risk_level,
            "risk_score": getattr(forecast, "risk_score", None),
            "confidence": getattr(forecast, "confidence", None),
            "data_quality": _value_of(forecast, "data_quality"),
        },
        dedup_extra=[str(getattr(forecast, "revision", "") or "")],
    )


async def record_remediation_event(
    session: AsyncSession, *, action: Any, event: str
) -> None:
    """A remediation was proposed, started, completed or rolled back (§9)."""
    if action is None or action.project_id is None:
        return
    mapping = {
        "proposed": PlatformEventType.REMEDIATION_PROPOSED,
        "started": PlatformEventType.REMEDIATION_STARTED,
        "completed": PlatformEventType.REMEDIATION_COMPLETED,
        "rolled_back": PlatformEventType.REMEDIATION_ROLLED_BACK,
    }
    event_type = mapping.get(event)
    if event_type is None:  # pragma: no cover - defensive
        return
    incident_id = getattr(action, "incident_id", None)
    await safely_publish_event(
        session,
        project_id=action.project_id,
        environment_id=getattr(action, "environment_id", None),
        event_type=event_type,
        source="remediation",
        subject_type="remediation_action",
        subject_id=action.id,
        component_id=getattr(action, "component_id", None),
        occurred_at=(
            _aware(getattr(action, "completed_at", None))
            or _aware(getattr(action, "started_at", None))
            or _aware(getattr(action, "created_at", None))
        ),
        correlation_id=(
            correlation_id_for(kind="incident", subject_id=incident_id)
            if incident_id
            else None
        ),
        payload={
            "action_type": _value_of(action, "action_type"),
            "status": _value_of(action, "status"),
            "outcome": _value_of(action, "outcome"),
            "execution_mode": _value_of(action, "execution_mode"),
            "incident_id": str(incident_id) if incident_id else None,
        },
        dedup_extra=[event, _value_of(action, "status") or ""],
    )


async def record_learning_completed(
    session: AsyncSession, *, project_id: uuid.UUID, run: Any = None
) -> None:
    """A learning run completed (§9 ``LEARNING_COMPLETED``)."""
    if project_id is None:
        return
    await safely_publish_event(
        session,
        project_id=project_id,
        event_type=PlatformEventType.LEARNING_COMPLETED,
        source="learning",
        subject_type="learning_run",
        subject_id=getattr(run, "id", None),
        occurred_at=_aware(getattr(run, "completed_at", None)),
        payload={
            "run_id": str(getattr(run, "id", "")) or None,
            "events_processed": getattr(run, "events_processed", None),
            "knowledge_created": getattr(run, "knowledge_created", None),
        },
        dedup_extra=[str(getattr(run, "id", ""))],
    )


async def record_deployment_event(session: AsyncSession, *, deployment: Any) -> None:
    """A deployment was recorded (§9 ``DEPLOYMENT_RECORDED``)."""
    if deployment is None or deployment.project_id is None:
        return
    await safely_publish_event(
        session,
        project_id=deployment.project_id,
        environment_id=getattr(deployment, "environment_id", None),
        event_type=PlatformEventType.DEPLOYMENT_RECORDED,
        source="deployment",
        subject_type="deployment",
        subject_id=deployment.id,
        component_id=getattr(deployment, "component_id", None),
        occurred_at=_aware(getattr(deployment, "deployed_at", None)),
        payload={
            "version": getattr(deployment, "version", None),
            "commit_sha": getattr(deployment, "commit_sha", None),
            "status": _value_of(deployment, "status"),
        },
        dedup_extra=[_value_of(deployment, "status") or ""],
    )


async def record_anomaly_event(
    session: AsyncSession, *, anomaly: Any, resolved: bool = False
) -> None:
    """An anomaly was detected or resolved (§9 ``ANOMALY_DETECTED``)."""
    if anomaly is None or anomaly.project_id is None:
        return
    await safely_publish_event(
        session,
        project_id=anomaly.project_id,
        environment_id=getattr(anomaly, "environment_id", None),
        event_type=PlatformEventType.ANOMALY_DETECTED,
        source="anomaly_detection",
        subject_type="anomaly",
        subject_id=anomaly.id,
        component_id=getattr(anomaly, "component_id", None),
        occurred_at=(
            _aware(getattr(anomaly, "resolved_at", None))
            if resolved
            else _aware(getattr(anomaly, "detected_at", None))
        ),
        payload={
            "anomaly_type": _value_of(anomaly, "anomaly_type"),
            "severity": _value_of(anomaly, "severity"),
            "status": _value_of(anomaly, "status"),
            "metric_name": getattr(anomaly, "metric_name", None),
            "resolved": resolved,
        },
        dedup_extra=["resolved" if resolved else "detected"],
    )


__all__ = [
    "record_analysis_completed",
    "record_anomaly_event",
    "record_deployment_event",
    "record_forecast_generated",
    "record_incident_event",
    "record_learning_completed",
    "record_patch_verified",
    "record_remediation_event",
    "record_reproduction_finished",
    "record_root_cause_decided",
]
