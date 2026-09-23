"""ARGUS Notification Engine (Phase 11 §54–§56).

One notification, potentially many channels, and — the part that actually
matters — an alert storm that cannot happen.

The design is driven by §56. Deduplication runs on **(project, fingerprint,
time bucket)**, where the bucket is the configured cooldown divided into the
clock. So a condition that stays true for an hour produces one notification with
an occurrence count, not sixty. The row *is* the in-app channel; email and
webhook are delivery *attempts* recorded on the same row, so "was anyone told?"
is answerable from one table instead of from provider logs.

Providers are abstractions (§54): :class:`NotificationProvider` with
``IN_APP``, :class:`EmailProvider` and :class:`WebhookProvider` implementations
that all record the attempt and do nothing external by default. Nothing here
hard-codes an SMTP host or a Slack URL, and no provider is required for ARGUS to
work — a missing provider degrades a channel, never the platform (§59).

Severity is not decoration: ``SUPPRESSED`` exists as a first-class status so a
notification can be recorded as "this fired but we chose not to surface it",
which is different from it never having fired.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, Sequence, cast

from sqlalchemy import func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.platform import (
    NotificationChannel,
    NotificationKind,
    NotificationSeverity,
    NotificationStatus,
    PlatformEventType,
    PlatformNotification,
)

logger = logging.getLogger(__name__)

#: Default severity per kind. Stated here so the API cannot invent one.
KIND_SEVERITY: dict[NotificationKind, NotificationSeverity] = {
    NotificationKind.CRITICAL_INCIDENT: NotificationSeverity.CRITICAL,
    NotificationKind.HIGH_PREDICTED_RISK: NotificationSeverity.WARNING,
    NotificationKind.REMEDIATION_APPROVAL: NotificationSeverity.WARNING,
    NotificationKind.REMEDIATION_FAILURE: NotificationSeverity.CRITICAL,
    NotificationKind.ROLLBACK: NotificationSeverity.CRITICAL,
    NotificationKind.SLO_BURN: NotificationSeverity.WARNING,
    NotificationKind.LEARNING_INSIGHT: NotificationSeverity.INFO,
    NotificationKind.SYSTEM_DEGRADATION: NotificationSeverity.CRITICAL,
    NotificationKind.DATA_QUALITY: NotificationSeverity.INFO,
}

#: Where each kind should link, so a notification is actionable.
KIND_ROUTE: dict[NotificationKind, str] = {
    NotificationKind.CRITICAL_INCIDENT: "/incidents",
    NotificationKind.HIGH_PREDICTED_RISK: "/predictions",
    NotificationKind.REMEDIATION_APPROVAL: "/remediation",
    NotificationKind.REMEDIATION_FAILURE: "/remediation",
    NotificationKind.ROLLBACK: "/remediation",
    NotificationKind.SLO_BURN: "/slo",
    NotificationKind.LEARNING_INSIGHT: "/intelligence",
    NotificationKind.SYSTEM_DEGRADATION: "/health",
    NotificationKind.DATA_QUALITY: "/data-quality",
}


def _aware(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def compute_bucket(*, moment: datetime, cooldown_seconds: int) -> int:
    """The dedup bucket a moment falls into (§56).

    Integer division of the epoch by the cooldown: two occurrences inside one
    window share a bucket and collapse, and the next window raises a fresh
    notification — which is the behaviour an operator wants (a reminder, not a
    flood).
    """
    cooldown = max(1, int(cooldown_seconds))
    return int(_aware(moment).timestamp()) // cooldown


def compute_fingerprint(kind: NotificationKind, subject_id: uuid.UUID) -> str:
    """Identity of the *condition*, so the same condition dedupes and a
    different one does not."""
    import hashlib

    return hashlib.sha256(f"{kind.value}|{subject_id}".encode("utf-8")).hexdigest()[:64]


# ---------------------------------------------------------------------------
# Providers (§54, §55)
# ---------------------------------------------------------------------------
@dataclass
class DeliveryResult:
    """One channel's attempt, whether or not a provider exists for it."""

    channel: str
    delivered: bool
    detail: str
    provider: str = "none"

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "delivered": self.delivered,
            "detail": self.detail,
            "provider": self.provider,
        }


class NotificationProvider(Protocol):
    """What a notification channel must be able to do.

    Deliberately tiny: ARGUS decides *what* to say and *whether* to say it; a
    provider only decides how the message leaves the building.
    """

    channel: NotificationChannel

    async def deliver(self, notification: PlatformNotification) -> DeliveryResult: ...


class InAppProvider:
    """The default and only always-available channel: the row itself."""

    channel = NotificationChannel.IN_APP

    async def deliver(self, notification: PlatformNotification) -> DeliveryResult:
        return DeliveryResult(
            channel=self.channel.value,
            delivered=True,
            detail="stored as an in-app notification",
            provider="in_app",
        )


class RecordingEmailProvider:
    """An email abstraction that records the attempt without a provider.

    §54 asks for an email *abstraction*, not an SMTP client. This records what
    would have been sent, which makes the notification path testable end to end
    without a mail server — and honest: ``delivered`` is ``False``, because
    nothing left the process.
    """

    channel = NotificationChannel.EMAIL

    def __init__(self, transport: Optional[Any] = None, enabled: bool = False) -> None:
        self.transport = transport
        self.enabled = enabled

    async def deliver(self, notification: PlatformNotification) -> DeliveryResult:
        if not self.enabled or self.transport is None:
            return DeliveryResult(
                channel=self.channel.value,
                delivered=False,
                detail=(
                    "no email provider is configured; the message is recorded on "
                    "the notification but not sent"
                ),
                provider="recording",
            )
        try:
            await self.transport.send(
                subject=notification.title, body=notification.body or ""
            )
        except Exception as exc:
            return DeliveryResult(
                channel=self.channel.value,
                delivered=False,
                detail=f"the configured provider failed: {type(exc).__name__}",
                provider="transport",
            )
        return DeliveryResult(
            channel=self.channel.value,
            delivered=True,
            detail="sent",
            provider="transport",
        )


class RecordingWebhookProvider:
    """A webhook abstraction that records the attempt (§54, §53).

    Signing and delivery belong to :mod:`app.services.integrations`, which owns
    the signature scheme; this class only reports whether one is configured.
    """

    channel = NotificationChannel.WEBHOOK

    def __init__(self, url: Optional[str] = None, secret_set: bool = False) -> None:
        self.url = url
        self.secret_set = secret_set

    async def deliver(self, notification: PlatformNotification) -> DeliveryResult:
        if not self.url:
            return DeliveryResult(
                channel=self.channel.value,
                delivered=False,
                detail="no webhook target is configured",
                provider="recording",
            )
        if not self.secret_set:
            #: Refusing to deliver unsigned is the security-relevant choice: a
            #: receiver that cannot verify a payload should not trust it.
            return DeliveryResult(
                channel=self.channel.value,
                delivered=False,
                detail=(
                    "a webhook target is configured but no signing secret is set, "
                    "so the payload is recorded and not sent"
                ),
                provider="recording",
            )
        return DeliveryResult(
            channel=self.channel.value,
            delivered=False,
            detail=(
                "the target is configured and signed payloads are supported; no "
                "outbound transport is wired in this deployment, so the attempt "
                "is recorded"
            ),
            provider="recording",
        )


def build_providers(settings: Settings) -> dict[NotificationChannel, Any]:
    """The configured channels. In-app is always present (§54)."""
    channels = {
        NotificationChannel(channel)
        for channel in settings.PLATFORM_NOTIFICATION_CHANNELS
        if channel in NotificationChannel.__members__
    }
    providers: dict[NotificationChannel, Any] = {
        NotificationChannel.IN_APP: InAppProvider()
    }
    if NotificationChannel.EMAIL in channels:
        providers[NotificationChannel.EMAIL] = RecordingEmailProvider(enabled=False)
    if NotificationChannel.WEBHOOK in channels:
        providers[NotificationChannel.WEBHOOK] = RecordingWebhookProvider(
            url=getattr(settings, "PLATFORM_WEBHOOK_URL", None),
            secret_set=bool(settings.PLATFORM_WEBHOOK_SECRET),
        )
    return providers


# ---------------------------------------------------------------------------
# Raising notifications
# ---------------------------------------------------------------------------
async def notify(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    kind: NotificationKind,
    title: str,
    subject_type: str,
    subject_id: uuid.UUID,
    body: Optional[str] = None,
    severity: Optional[NotificationSeverity] = None,
    environment_id: Optional[uuid.UUID] = None,
    case_id: Optional[uuid.UUID] = None,
    link: Optional[str] = None,
    evidence: Optional[dict[str, Any]] = None,
    source: str = "platform",
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
    deliver: bool = True,
    suppressed: bool = False,
) -> PlatformNotification:
    """Raise a notification, deduplicated by condition and window (§55, §56)."""
    settings = settings or get_settings()
    moment = _aware(now)
    fingerprint = compute_fingerprint(kind, subject_id)
    bucket = compute_bucket(
        moment=moment, cooldown_seconds=settings.PLATFORM_NOTIFICATION_COOLDOWN_SECONDS
    )

    existing = (
        await session.scalars(
            select(PlatformNotification).where(
                PlatformNotification.project_id == project_id,
                PlatformNotification.fingerprint == fingerprint,
                PlatformNotification.dedup_bucket == bucket,
            )
        )
    ).first()
    if existing is not None:
        #: Same condition, same window: count it, do not repeat it.
        existing.occurrence_count += 1
        existing.body = body or existing.body
        await session.flush()
        return existing

    notification = PlatformNotification(
        project_id=project_id,
        environment_id=environment_id,
        kind=kind,
        severity=severity or KIND_SEVERITY.get(kind, NotificationSeverity.WARNING),
        status=(
            NotificationStatus.SUPPRESSED if suppressed else NotificationStatus.UNREAD
        ),
        title=title[:500],
        body=body,
        source=source,
        subject_type=subject_type,
        subject_id=subject_id,
        case_id=case_id,
        link=link or KIND_ROUTE.get(kind),
        evidence=evidence,
        fingerprint=fingerprint,
        dedup_bucket=bucket,
        occurrence_count=1,
    )
    session.add(notification)
    await session.flush()

    attempts: list[dict[str, Any]] = []
    if deliver and not suppressed:
        for channel, provider in build_providers(settings).items():
            try:
                result = await provider.deliver(notification)
            except Exception as exc:  # pragma: no cover - a provider must not break us
                result = DeliveryResult(
                    channel=channel.value,
                    delivered=False,
                    detail=f"provider raised {type(exc).__name__}",
                )
            attempts.append(result.as_dict())
            if result.delivered:
                notification.delivered_at = moment
    notification.channels_attempted = [
        attempt["channel"] for attempt in attempts
    ] or None
    notification.delivery = {"attempts": attempts} if attempts else None
    await session.flush()

    from app.services.platform_events import safely_publish_event

    await safely_publish_event(
        session,
        project_id=project_id,
        environment_id=environment_id,
        event_type=PlatformEventType.NOTIFICATION_RAISED,
        source="notifications",
        subject_type=subject_type,
        subject_id=subject_id,
        case_id=case_id,
        occurred_at=moment,
        payload={
            "kind": kind.value,
            "severity": notification.severity.value,
            "title": notification.title,
            "channels": notification.channels_attempted,
            "suppressed": suppressed,
        },
        dedup_extra=[kind.value, bucket],
    )
    return notification


async def notify_from_event(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    event_type: PlatformEventType,
    subject_type: str,
    subject_id: uuid.UUID,
    title: str,
    payload: Optional[dict[str, Any]] = None,
    environment_id: Optional[uuid.UUID] = None,
    case_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> Optional[PlatformNotification]:
    """Map a platform event to a notification, when the event warrants one (§55).

    Returns ``None`` for event types that are informational, so the mapping is
    explicit rather than "notify on everything".
    """
    mapping: dict[PlatformEventType, tuple[NotificationKind, NotificationSeverity]] = {
        PlatformEventType.INCIDENT_CREATED: (
            NotificationKind.CRITICAL_INCIDENT,
            NotificationSeverity.CRITICAL,
        ),
        PlatformEventType.REMEDIATION_ROLLED_BACK: (
            NotificationKind.ROLLBACK,
            NotificationSeverity.CRITICAL,
        ),
        PlatformEventType.ERROR_BUDGET_BURN: (
            NotificationKind.SLO_BURN,
            NotificationSeverity.WARNING,
        ),
        PlatformEventType.DATA_QUALITY_ISSUE: (
            NotificationKind.DATA_QUALITY,
            NotificationSeverity.INFO,
        ),
        PlatformEventType.LEARNING_COMPLETED: (
            NotificationKind.LEARNING_INSIGHT,
            NotificationSeverity.INFO,
        ),
    }
    entry = mapping.get(event_type)
    if entry is None:
        return None
    kind, severity = entry
    #: An incident notification is only raised for an incident that is actually
    #: severe: notifying for every LOW incident is how alert fatigue starts.
    if kind == NotificationKind.CRITICAL_INCIDENT:
        incident_severity = str((payload or {}).get("severity", "")).upper()
        if incident_severity not in ("CRITICAL", "HIGH"):
            return None
    return await notify(
        session,
        project_id=project_id,
        kind=kind,
        title=title,
        body=_event_body(event_type, payload),
        severity=severity,
        subject_type=subject_type,
        subject_id=subject_id,
        environment_id=environment_id,
        case_id=case_id,
        evidence=payload,
        source="platform_events",
        now=now,
        settings=settings,
    )


def _event_body(
    event_type: PlatformEventType, payload: Optional[dict[str, Any]]
) -> Optional[str]:
    if not payload:
        return None
    interesting = (
        "severity",
        "status",
        "burn_state",
        "burn_rate",
        "risk_level",
        "reason",
        "detail",
        "kind",
    )
    parts = [
        f"{key}: {payload[key]}" for key in interesting if payload.get(key) is not None
    ]
    return "; ".join(parts) or None


async def raise_platform_degradation(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    report: Any,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> Optional[PlatformNotification]:
    """§55 ``SYSTEM_DEGRADATION``: ARGUS telling an operator about itself.

    Deduplicated like everything else, and keyed on the *set* of degraded
    subsystems — so a changing degradation cluster is a new notification while a
    steady one is not repeated.
    """
    degraded = sorted(set(getattr(report, "degraded_capabilities", []) or []))
    if not degraded and getattr(report, "ready", True):
        return None
    moment = _aware(now) or getattr(report, "as_of", datetime.now(timezone.utc))
    import hashlib

    subject = uuid.UUID(hashlib.sha256(",".join(degraded).encode()).hexdigest()[:32])
    title = (
        "ARGUS is ready but degraded: " + ", ".join(degraded)
        if degraded
        else "ARGUS is not ready: a required subsystem is unavailable"
    )
    return await notify(
        session,
        project_id=project_id,
        kind=NotificationKind.SYSTEM_DEGRADATION,
        title=title,
        body="; ".join(getattr(report, "notes", []) or []) or None,
        subject_type="platform",
        subject_id=subject,
        evidence={"degraded": degraded, "ready": getattr(report, "ready", None)},
        source="platform_health",
        now=moment,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Inbox operations
# ---------------------------------------------------------------------------
async def list_notifications(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    statuses: Optional[Sequence[NotificationStatus]] = None,
    kinds: Optional[Sequence[NotificationKind]] = None,
    severities: Optional[Sequence[NotificationSeverity]] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PlatformNotification]:
    stmt = (
        select(PlatformNotification)
        .where(PlatformNotification.project_id == project_id)
        .order_by(PlatformNotification.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if statuses:
        stmt = stmt.where(PlatformNotification.status.in_(list(statuses)))
    if kinds:
        stmt = stmt.where(PlatformNotification.kind.in_(list(kinds)))
    if severities:
        stmt = stmt.where(PlatformNotification.severity.in_(list(severities)))
    return list((await session.scalars(stmt)).all())


async def mark_read(
    session: AsyncSession,
    *,
    notification: PlatformNotification,
    actor: Optional[str] = None,
    acknowledge: bool = False,
    now: Optional[datetime] = None,
) -> PlatformNotification:
    moment = _aware(now)
    notification.status = (
        NotificationStatus.ACKNOWLEDGED if acknowledge else NotificationStatus.READ
    )
    notification.read_at = moment
    if acknowledge:
        notification.acknowledged_by = actor
    await session.flush()
    return notification


async def notification_summary(
    session: AsyncSession, *, project_id: uuid.UUID
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(
                PlatformNotification.status,
                PlatformNotification.severity,
                func.count(),
            )
            .where(PlatformNotification.project_id == project_id)
            .group_by(PlatformNotification.status, PlatformNotification.severity)
        )
    ).all()
    by_status: dict[str, int] = {}
    unread_by_severity: dict[str, int] = {}
    for status, severity, count in rows:
        by_status[status.value] = by_status.get(status.value, 0) + int(count)
        if status == NotificationStatus.UNREAD:
            unread_by_severity[severity.value] = unread_by_severity.get(
                severity.value, 0
            ) + int(count)
    return {
        "by_status": by_status,
        "unread_by_severity": unread_by_severity,
        "unread_total": sum(unread_by_severity.values()),
        "in_app_always_available": True,
    }


async def prune_notifications(
    session: AsyncSession, *, older_than: datetime, limit: int = 5000
) -> int:
    """Retention for notifications (§49), never deleting unread ones.

    An unread notification is work somebody has not seen; deleting it silently
    would lose information an operator was supposed to act on.
    """
    from sqlalchemy import delete

    stmt = (
        delete(PlatformNotification)
        .where(
            PlatformNotification.created_at < older_than,
            PlatformNotification.status != NotificationStatus.UNREAD,
        )
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    #: A DML statement yields a ``CursorResult``; the base annotation hides it.
    return int(cast(CursorResult, result).rowcount or 0)


__all__ = [
    "KIND_ROUTE",
    "KIND_SEVERITY",
    "DeliveryResult",
    "InAppProvider",
    "NotificationProvider",
    "RecordingEmailProvider",
    "RecordingWebhookProvider",
    "build_providers",
    "compute_bucket",
    "compute_fingerprint",
    "list_notifications",
    "mark_read",
    "notification_summary",
    "notify",
    "notify_from_event",
    "prune_notifications",
    "raise_platform_degradation",
]
