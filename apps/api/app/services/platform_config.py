"""ARGUS Configuration Center (Phase 11 §44, §91–§94).

Centralized, versioned configuration, and the validation that stops bad
configuration from being stored in the first place.

The central decision: **configuration is append-only**. An update writes a new
version row; nothing is edited in place and nothing is deleted. That is §92–§93
taken seriously, and it is the only way an autonomous platform can answer the
question that matters after an incident: *what was ARGUS configured to do when it
did that?* A rollback (§94) writes yet another version, recording which version it
restored.

Secrets (§92, §46): a configuration row may state that a secret **is set**, but
never stores it. :func:`redact_settings` strips any key that looks like a
credential into ``redacted_fields`` before a version is written, so the version
ledger can be shown to an operator without exposing anything — and without a
second copy of the secret existing in a table nobody remembers to encrypt.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.platform import (
    ConfigurationScope,
    ConfigurationVersion,
    PlatformEventType,
)

logger = logging.getLogger(__name__)

#: Key fragments that mark a value as a credential. Matched case-insensitively
#: against the key, so ``webhook_secret`` and ``apiKey`` are both caught.
SECRET_KEY_FRAGMENTS = (
    "secret",
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "webhook_secret",
)

#: Scopes whose settings an operator may change through the API. Anything not
#: listed here is readable but not writable — a deliberate default-deny.
WRITABLE_SCOPES = frozenset(
    {
        ConfigurationScope.PROJECT_SETTINGS.value,
        ConfigurationScope.SLO.value,
        ConfigurationScope.NOTIFICATIONS.value,
        ConfigurationScope.LEARNING.value,
        ConfigurationScope.RETENTION.value,
    }
)


class ConfigurationError(ValueError):
    """A configuration value that must not be stored."""


#: Numeric bounds per setting name, applied wherever the setting appears. §92's
#: "validation" is this: a target outside 0–1, a window beyond the maximum, or a
#: negative retention is refused rather than stored and discovered later.
BOUNDS: dict[str, tuple[float, float]] = {
    "target": (0.0, 1.0),
    "window_seconds": (60.0, 2_592_000.0),
    "retention_days": (1.0, 3_650.0),
    "min_samples": (1.0, 10_000.0),
    "cooldown_seconds": (0.0, 86_400.0),
    "burn_elevated": (0.0, 1000.0),
    "burn_fast": (0.0, 1000.0),
    "burn_critical": (0.0, 1000.0),
    "concurrency": (1.0, 100.0),
    "batch_size": (1.0, 10_000.0),
}

#: §32. Indicators whose target is a 0..1 *ratio* — a share of the window, not a
#: quantity. Only these may be bounded at 1.
RATIO_INDICATORS: tuple[str, ...] = ("AVAILABILITY", "ERROR_RATE")

#: The ceiling for a magnitude target (latency in milliseconds, saturation in
#: percent). It exists to catch a typo that dropped a decimal point, not to imply
#: that 500ms is an invalid latency objective — bounding every target at 1.0 would
#: make a latency SLO impossible to express at all, which §32 requires.
MAX_MAGNITUDE_TARGET: float = 1_000_000.0


def bounds_for(scope: str, settings: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """The bounds that apply to *this* configuration, which depend on scope.

    A single global ``target`` bound is subtly wrong: an availability target is a
    ratio (0..1) while a latency target is a measurement in the objective's unit.
    Applying the ratio bound everywhere would refuse every latency objective.
    """
    if scope != ConfigurationScope.SLO.value:
        return BOUNDS
    indicator = str(settings.get("indicator", ""))
    if indicator in RATIO_INDICATORS:
        return BOUNDS
    return {**BOUNDS, "target": (0.0, MAX_MAGNITUDE_TARGET)}


#: Enum-ish settings with their allowed values. Refusing an unknown value here is
#: what stops a typo from silently disabling a subsystem.
ALLOWED_VALUES: dict[str, tuple[str, ...]] = {
    "indicator": ("AVAILABILITY", "LATENCY", "ERROR_RATE", "SATURATION", "CUSTOM"),
    "comparison": ("AT_LEAST", "AT_MOST"),
    "notifications_enabled": ("true", "false"),
    "learning_enabled": ("true", "false"),
    "ai_assistant_enabled": ("true", "false"),
    "cross_project_intelligence": ("true", "false"),
}


def _aware(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def looks_like_secret(key: str) -> bool:
    lower = str(key).lower()
    return any(fragment in lower for fragment in SECRET_KEY_FRAGMENTS)


def redact_settings(settings: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Strip credentials, returning ``(safe_settings, redacted_keys)``.

    A redacted key is *replaced* by a marker rather than removed, so the ledger
    shows that the setting exists and is configured, without its value. Deleting
    it would make a configured integration look unconfigured.
    """
    safe: dict[str, Any] = {}
    redacted: list[str] = []
    for key, value in (settings or {}).items():
        if looks_like_secret(str(key)):
            redacted.append(str(key))
            safe[str(key)] = "<redacted>"
            continue
        if isinstance(value, dict):
            nested, nested_redacted = redact_settings(value)
            safe[str(key)] = nested
            redacted.extend(f"{key}.{name}" for name in nested_redacted)
            continue
        safe[str(key)] = value
    return safe, redacted


def validate_configuration(
    *,
    scope: str,
    settings: dict[str, Any],
    settings_obj: Optional[Settings] = None,
) -> None:
    """Refuse a configuration that cannot be safely applied (§92).

    Raises :class:`ConfigurationError` with every problem found, not just the
    first: an operator fixing configuration should learn all of it at once.
    """
    settings_obj = settings_obj or get_settings()
    problems: list[str] = []
    if not isinstance(settings, dict) or not settings:
        raise ConfigurationError("configuration must be a non-empty object")

    bounds = bounds_for(scope, settings)
    for key, value in settings.items():
        name = str(key)
        if name in bounds:
            low, high = bounds[name]
            if value is None or isinstance(value, bool):
                problems.append(f"{name} must be a number between {low} and {high}")
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                problems.append(f"{name} must be a number, got {value!r}")
                continue
            if not (low <= number <= high):
                problems.append(f"{name}={number} is outside {low}..{high}")
        if name in ALLOWED_VALUES and str(value) not in ALLOWED_VALUES[name]:
            problems.append(
                f"{name}={value!r} is not one of {', '.join(ALLOWED_VALUES[name])}"
            )
        if isinstance(value, str) and len(value) > 2000:
            problems.append(f"{name} is longer than 2000 characters")

    if scope == ConfigurationScope.SLO.value:
        metric = settings.get("metric_name")
        indicator = str(settings.get("indicator", ""))
        if not metric and indicator != "CUSTOM":
            problems.append(
                "an objective must name a metric unless its indicator is CUSTOM"
            )
    if scope not in WRITABLE_SCOPES:
        problems.append(
            f"scope {scope} is not writable through the API; it is reported from "
            "environment configuration"
        )
    if problems:
        raise ConfigurationError("; ".join(problems))


async def next_version(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scope: ConfigurationScope,
    scope_id: Optional[uuid.UUID],
) -> int:
    stmt = select(ConfigurationVersion.version).where(
        ConfigurationVersion.project_id == project_id,
        ConfigurationVersion.scope == scope,
    )
    if scope_id is None:
        stmt = stmt.where(ConfigurationVersion.scope_id.is_(None))
    else:
        stmt = stmt.where(ConfigurationVersion.scope_id == scope_id)
    highest = await session.scalar(stmt.order_by(ConfigurationVersion.version.desc()))
    return int(highest or 0) + 1


async def record_configuration(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scope: ConfigurationScope | str,
    settings: dict[str, Any],
    scope_id: Optional[uuid.UUID] = None,
    change_summary: Optional[str] = None,
    changed_by: Optional[str] = None,
    reason: Optional[str] = None,
    authorizing_actor: Optional[str] = None,
    rolled_back_from: Optional[int] = None,
    now: Optional[datetime] = None,
) -> ConfigurationVersion:
    """Append a configuration version (§93)."""
    if isinstance(scope, str):
        scope = ConfigurationScope(scope)
    moment = _aware(now)
    safe, redacted = redact_settings(settings)
    previous = await current_configuration(
        session, project_id=project_id, scope=scope, scope_id=scope_id
    )
    version = ConfigurationVersion(
        project_id=project_id,
        scope=scope,
        scope_id=scope_id,
        version=await next_version(
            session, project_id=project_id, scope=scope, scope_id=scope_id
        ),
        settings=safe,
        redacted_fields=redacted or None,
        previous_version=previous.version if previous else None,
        change_summary=change_summary,
        changed_by=changed_by,
        reason=reason,
        rolled_back_from=rolled_back_from,
        authorizing_actor=authorizing_actor or changed_by,
    )
    session.add(version)
    await session.flush()

    from app.services.platform_events import safely_publish_event

    await safely_publish_event(
        session,
        project_id=project_id,
        event_type=PlatformEventType.CONFIGURATION_CHANGED,
        source="platform_config",
        subject_type="configuration",
        subject_id=version.id,
        occurred_at=moment,
        payload={
            "scope": scope.value,
            "version": version.version,
            "previous_version": version.previous_version,
            "change_summary": change_summary,
            "actor": changed_by,
            "rolled_back_from": rolled_back_from,
            "redacted_fields": redacted,
        },
        dedup_extra=["configuration", scope.value, version.version],
    )
    return version


async def current_configuration(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scope: ConfigurationScope | str,
    scope_id: Optional[uuid.UUID] = None,
) -> Optional[ConfigurationVersion]:
    """The live version of a scope."""
    if isinstance(scope, str):
        scope = ConfigurationScope(scope)
    stmt = (
        select(ConfigurationVersion)
        .where(
            ConfigurationVersion.project_id == project_id,
            ConfigurationVersion.scope == scope,
        )
        .order_by(ConfigurationVersion.version.desc())
        .limit(1)
    )
    if scope_id is None:
        stmt = stmt.where(ConfigurationVersion.scope_id.is_(None))
    else:
        stmt = stmt.where(ConfigurationVersion.scope_id == scope_id)
    return (await session.scalars(stmt)).first()


async def configuration_history(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scope: Optional[ConfigurationScope | str] = None,
    scope_id: Optional[uuid.UUID] = None,
    limit: int = 50,
) -> list[ConfigurationVersion]:
    """The version ledger, newest first (§93)."""
    stmt = (
        select(ConfigurationVersion)
        .where(ConfigurationVersion.project_id == project_id)
        .order_by(
            ConfigurationVersion.created_at.desc(), ConfigurationVersion.version.desc()
        )
        .limit(limit)
    )
    if scope is not None:
        stmt = stmt.where(
            ConfigurationVersion.scope
            == (ConfigurationScope(scope) if isinstance(scope, str) else scope)
        )
    if scope_id is not None:
        stmt = stmt.where(ConfigurationVersion.scope_id == scope_id)
    return list((await session.scalars(stmt)).all())


def diff_versions(
    left: ConfigurationVersion, right: ConfigurationVersion
) -> dict[str, Any]:
    """Field-by-field comparison of two versions (§93)."""
    left_settings = left.settings or {}
    right_settings = right.settings or {}
    keys = sorted(set(left_settings) | set(right_settings))
    changes: list[dict[str, Any]] = []
    for key in keys:
        before = left_settings.get(key)
        after = right_settings.get(key)
        if before == after:
            continue
        changes.append({"field": key, "before": before, "after": after})
    redacted = sorted(
        set(left.redacted_fields or []) | set(right.redacted_fields or [])
    )
    return {
        "left": {
            "version": left.version,
            "created_at": _aware(left.created_at).isoformat(),
        },
        "right": {
            "version": right.version,
            "created_at": _aware(right.created_at).isoformat(),
        },
        "changes": changes,
        "changed_fields": len(changes),
        "redacted_fields": redacted,
        "note": (
            "redacted fields are compared by presence only: their values are "
            "never stored here"
        ),
    }


async def rollback_configuration(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scope: ConfigurationScope | str,
    target_version: int,
    scope_id: Optional[uuid.UUID] = None,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> ConfigurationVersion:
    """Restore a previous version by writing a new one (§94).

    Safety follows §94: this restores *configuration*, never an action, and the
    new version records ``rolled_back_from`` so the history reads as a restore
    rather than an unexplained change.
    """
    if isinstance(scope, str):
        scope = ConfigurationScope(scope)
    moment = _aware(now)
    #: A project-wide version has ``scope_id IS NULL``; comparing with ``==`` would
    #: never match it, so the clause is chosen explicitly.
    scope_clause = (
        ConfigurationVersion.scope_id.is_(None)
        if scope_id is None
        else ConfigurationVersion.scope_id == scope_id
    )
    target = (
        await session.scalars(
            select(ConfigurationVersion).where(
                ConfigurationVersion.project_id == project_id,
                ConfigurationVersion.scope == scope,
                ConfigurationVersion.version == target_version,
                scope_clause,
            )
        )
    ).first()
    if target is None:
        raise ConfigurationError(
            f"no version {target_version} exists for scope {scope.value}"
        )
    restored = await record_configuration(
        session,
        project_id=project_id,
        scope=scope,
        scope_id=scope_id,
        settings=target.settings or {},
        change_summary=f"restored configuration version {target_version}",
        changed_by=actor,
        reason=reason or "operator requested a rollback",
        rolled_back_from=target_version,
        now=moment,
    )
    return restored


# ---------------------------------------------------------------------------
# §44 — the settings an operator sees
# ---------------------------------------------------------------------------
#: The settings surfaces §44 names, each with the values ARGUS will use when no
#: project override exists. These are *reported*, not invented: each one reads
#: the environment configuration the running process actually uses.
def default_settings_summary(settings: Optional[Settings] = None) -> dict[str, Any]:
    settings = settings or get_settings()
    return {
        "observability": {
            "anomaly_detection_enabled": settings.ANOMALY_DETECTION_ENABLED,
            "anomaly_sweep_interval_seconds": settings.ANOMALY_SWEEP_INTERVAL_SECONDS,
            "ingestion_flush_interval": settings.INGESTION_FLUSH_INTERVAL,
            "retention_events_days": getattr(settings, "RETENTION_EVENTS_DAYS", None),
        },
        "prediction": {
            "forecasting_enabled": settings.RELIABILITY_FORECASTING_ENABLED,
            "sweep_interval_seconds": settings.RELIABILITY_SWEEP_INTERVAL_SECONDS,
        },
        "remediation": {
            "execution_enabled": settings.REMEDIATION_EXECUTION_ENABLED,
            "canary_enabled": settings.REMEDIATION_CANARY_ENABLED,
            "planner_enabled": settings.REMEDIATION_PLANNER_ENABLED,
            "sweep_interval_seconds": settings.REMEDIATION_SWEEP_INTERVAL_SECONDS,
        },
        "learning": {
            "enabled": settings.INTELLIGENCE_LEARNING_ENABLED,
            "auto_activate": settings.INTELLIGENCE_AUTO_ACTIVATE_ENABLED,
            "sweep_interval_seconds": settings.INTELLIGENCE_SWEEP_INTERVAL_SECONDS,
            "confidence_threshold": settings.INTELLIGENCE_MIN_SAMPLES_HIGH_CONFIDENCE,
        },
        "notifications": {
            "channels": list(settings.PLATFORM_NOTIFICATION_CHANNELS),
            "cooldown_seconds": settings.PLATFORM_NOTIFICATION_COOLDOWN_SECONDS,
            "batch_limit": settings.PLATFORM_NOTIFICATION_BATCH_LIMIT,
        },
        "retention": {
            "platform_events_days": settings.RETENTION_PLATFORM_EVENTS_DAYS,
            "platform_snapshots_days": settings.RETENTION_PLATFORM_SNAPSHOTS_DAYS,
            "platform_notifications_days": settings.RETENTION_PLATFORM_NOTIFICATIONS_DAYS,
        },
        "slo": {
            "burn_elevated": settings.PLATFORM_BURN_ELEVATED,
            "burn_fast": settings.PLATFORM_BURN_FAST,
            "burn_critical": settings.PLATFORM_BURN_CRITICAL,
            "max_window_seconds": settings.PLATFORM_SLO_MAX_WINDOW_SECONDS,
        },
        "security": {
            "webhook_tolerance_seconds": settings.PLATFORM_WEBHOOK_TOLERANCE_SECONDS,
            "webhook_secret_set": bool(settings.PLATFORM_WEBHOOK_SECRET),
            "cross_project_intelligence_enabled": (
                settings.PLATFORM_CROSS_PROJECT_INTELLIGENCE_ENABLED
            ),
            "ai_assistant_enabled": settings.PLATFORM_AI_ASSISTANT_ENABLED,
        },
        "platform": {
            "enabled": settings.PLATFORM_ENABLED,
            "sweep_interval_seconds": settings.PLATFORM_SWEEP_INTERVAL_SECONDS,
            "auto_case_enabled": settings.PLATFORM_AUTO_CASE_ENABLED,
            "search_rate_limit_per_minute": settings.PLATFORM_SEARCH_RATE_LIMIT_PER_MINUTE,
        },
    }


@dataclass
class EffectiveConfiguration:
    """Environment defaults plus whatever a project has overridden (§44)."""

    project_id: uuid.UUID
    sections: dict[str, Any] = field(default_factory=dict)
    overrides: dict[str, Any] = field(default_factory=dict)
    redacted_fields: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": str(self.project_id),
            "sections": self.sections,
            "overrides": self.overrides,
            "redacted_fields": list(self.redacted_fields),
            "notes": list(self.notes),
        }


async def effective_configuration(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    settings: Optional[Settings] = None,
) -> EffectiveConfiguration:
    """The configuration ARGUS is actually running with for this project."""
    settings = settings or get_settings()
    result = EffectiveConfiguration(
        project_id=project_id, sections=default_settings_summary(settings)
    )
    for scope in ConfigurationScope:
        version = await current_configuration(
            session, project_id=project_id, scope=scope
        )
        if version is None:
            continue
        result.overrides[scope.value] = {
            "version": version.version,
            "settings": version.settings,
            "changed_by": version.changed_by,
            "change_summary": version.change_summary,
            "created_at": _aware(version.created_at).isoformat(),
        }
        result.redacted_fields.extend(version.redacted_fields or [])
    if not result.overrides:
        result.notes.append(
            "no project-level configuration has been recorded; the environment "
            "defaults above are in effect"
        )
    if result.redacted_fields:
        result.notes.append(
            "some configured fields are secrets and are reported by presence only"
        )
    return result


async def feature_flags(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """§61/§62: which capabilities are on, and what turns them off.

    Reads both layers that can disable a capability — the environment switch and
    the Phase 9 control plane — and reports which one is responsible, because
    "why is this off?" is the question an operator actually asks.
    """
    settings = settings or get_settings()
    flags = {
        "predictive_reliability": settings.RELIABILITY_FORECASTING_ENABLED,
        "autonomous_remediation": settings.REMEDIATION_EXECUTION_ENABLED,
        "learning": settings.INTELLIGENCE_LEARNING_ENABLED,
        "ai_assistant": settings.PLATFORM_AI_ASSISTANT_ENABLED,
        "cross_project_intelligence": settings.PLATFORM_CROSS_PROJECT_INTELLIGENCE_ENABLED,
        "control_plane": settings.PLATFORM_ENABLED,
        "auto_case": settings.PLATFORM_AUTO_CASE_ENABLED,
    }
    reasons: dict[str, str] = {}
    try:
        from app.services.remediation_controls import feature_enabled

        control_flags = (
            "predictive_reliability",
            "autonomous_remediation",
            "learning",
            "ai_assistant",
        )
        for flag in control_flags:
            if not flags.get(flag):
                reasons[flag] = "switched off in the environment configuration"
                continue
            enabled = await feature_enabled(session, flag, project_id=project_id)
            if not enabled:
                flags[flag] = False
                reasons[flag] = "disabled by an audited ARGUS control-plane action"
    except Exception:  # pragma: no cover - the control table may be absent
        reasons["_control_plane"] = "the control plane could not be read"
    return {
        "flags": flags,
        "reasons": reasons,
        "defaults": (
            "capabilities that can cause external side effects are disabled by "
            "default and must be switched on deliberately (§62)"
        ),
    }


__all__ = [
    "ALLOWED_VALUES",
    "BOUNDS",
    "SECRET_KEY_FRAGMENTS",
    "WRITABLE_SCOPES",
    "ConfigurationError",
    "EffectiveConfiguration",
    "configuration_history",
    "current_configuration",
    "default_settings_summary",
    "diff_versions",
    "effective_configuration",
    "feature_flags",
    "looks_like_secret",
    "next_version",
    "record_configuration",
    "redact_settings",
    "rollback_configuration",
    "validate_configuration",
]
