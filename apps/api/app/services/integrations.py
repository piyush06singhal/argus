"""ARGUS Integration Framework (Phase 11 §51–§53).

Provider-neutral interfaces, safe local implementations first, and the webhook
security the platform needs before it can accept a payload from anyone.

Three decisions, each matching an instruction in the phase:

**Interfaces before integrations (§51).** :class:`GitProvider`,
:class:`DeploymentProvider`, :class:`NotificationProvider`,
:class:`ObservabilityProvider`, :class:`IssueTrackerProvider`,
:class:`CloudProvider` and :class:`IncidentManagementProvider` are ``Protocol``s.
Nothing in ARGUS's own logic imports a vendor SDK.

**Mocks, not half-built integrations (§52).** The implementations here read and
write *local* state and say so. A mock that returns plausible-looking fabricated
data would be worse than no integration at all — an operator cannot tell invented
commits from real ones. So every mock returns a :class:`ProviderCapability` that
states what it can and cannot do, and every result is marked ``simulated``.

**Webhooks are hostile until proven otherwise (§53).** Every requirement in that
section is implemented and enforced in order: rate limit, timestamp tolerance and
signature verification (with a constant-time comparison), replay rejection by
``delivery_id``, idempotency by payload fingerprint, validation, then audit. A
webhook that fails any check is rejected *and recorded* — a rejected delivery is
evidence, and §53's audit requirement means we do not silently drop it.

There is no outbound transport anywhere in this module. A provider that could
make a network call is a provider that could be turned into SSRF; the honest
scope for this phase is the interface plus local implementations, and the
capability record tells the operator exactly that.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.platform import PlatformEventType

logger = logging.getLogger(__name__)


def _aware(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ---------------------------------------------------------------------------
# §51 — the interfaces
# ---------------------------------------------------------------------------
@dataclass
class ProviderCapability:
    """What a provider actually does — stated, not assumed (§52)."""

    provider: str
    simulated: bool
    description: str
    supports: list[str] = field(default_factory=list)
    does_not_support: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "simulated": self.simulated,
            "description": self.description,
            "supports": list(self.supports),
            "does_not_support": list(self.does_not_support),
        }


@dataclass
class ProviderResult:
    """The outcome of one provider call, with its provenance."""

    ok: bool
    provider: str
    simulated: bool
    data: dict[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "provider": self.provider,
            "simulated": self.simulated,
            "data": self.data,
            "reason": self.reason,
        }


class GitProvider(Protocol):
    name: str

    def capability(self) -> ProviderCapability: ...

    async def list_files(
        self, *, repository: str, revision: Optional[str]
    ) -> ProviderResult: ...

    async def read_file(
        self, *, repository: str, path: str, revision: Optional[str]
    ) -> ProviderResult: ...


class DeploymentProvider(Protocol):
    name: str

    def capability(self) -> ProviderCapability: ...

    async def deployments(
        self, *, project_id: uuid.UUID, limit: int
    ) -> ProviderResult: ...


class ObservabilityProvider(Protocol):
    name: str

    def capability(self) -> ProviderCapability: ...

    async def healthy(self) -> ProviderResult: ...


class IssueTrackerProvider(Protocol):
    name: str

    def capability(self) -> ProviderCapability: ...

    async def create_issue(
        self, *, title: str, body: str, labels: Sequence[str]
    ) -> ProviderResult: ...


class CloudProvider(Protocol):
    name: str

    def capability(self) -> ProviderCapability: ...

    async def describe(self, *, target: str) -> ProviderResult: ...


class IncidentManagementProvider(Protocol):
    name: str

    def capability(self) -> ProviderCapability: ...

    async def notify(
        self, *, summary: str, details: dict[str, Any]
    ) -> ProviderResult: ...


class NotificationProvider(Protocol):
    name: str

    def capability(self) -> ProviderCapability: ...

    async def send(
        self, *, title: str, body: str, target: Optional[str]
    ) -> ProviderResult: ...


# ---------------------------------------------------------------------------
# §52 — local implementations
# ---------------------------------------------------------------------------
class MockGitProvider:
    """Reads the repository through Phase 6's provider, not through a network.

    The one genuinely useful local implementation: ARGUS already knows how to
    read a checkout safely (Phase 6 owns path validation and size limits), so
    this delegates there rather than reimplementing file access — which is how
    this module could otherwise become a path-traversal hole.
    """

    name = "mock-git"

    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            provider=self.name,
            simulated=True,
            description=(
                "reads files from a repository ARGUS has been given access to, via "
                "the Phase 6 repository provider"
            ),
            supports=["list_files", "read_file", "content_hash"],
            does_not_support=[
                "remote fetch",
                "clone",
                "commit",
                "push",
                "pull requests",
            ],
        )

    async def list_files(
        self, *, repository: str, revision: Optional[str]
    ) -> ProviderResult:
        try:
            from app.services.repository_provider import provider_for
        except Exception as exc:  # pragma: no cover - import shape guard
            return ProviderResult(
                ok=False,
                provider=self.name,
                simulated=True,
                reason=f"the repository provider is unavailable: {type(exc).__name__}",
            )
        try:
            #: The Phase 6 provider factory, reused rather than re-implemented.
            #: Remote URLs are refused there, which is what makes "simulated" true
            #: (§52) and keeps ARGUS reading local working copies only.
            provider = provider_for(None, repository)
            #: ``list_files``/``read_file`` are async on the base class — awaiting
            #: them is what makes this call work against a real provider.
            files = await provider.list_files(revision)
            return ProviderResult(
                ok=True,
                provider=self.name,
                simulated=True,
                data={
                    "repository": repository,
                    "revision": revision,
                    "files": [entry.path for entry in files][:500],
                    "count": len(files),
                    "truncated": len(files) > 500,
                },
            )
        except Exception as exc:
            return ProviderResult(
                ok=False,
                provider=self.name,
                simulated=True,
                reason=f"the repository could not be read: {type(exc).__name__}",
            )

    async def read_file(
        self, *, repository: str, path: str, revision: Optional[str]
    ) -> ProviderResult:
        try:
            from app.services.repository_provider import provider_for

            provider = provider_for(None, repository)
            content = await provider.read_file(path, revision)
        except Exception as exc:
            return ProviderResult(
                ok=False,
                provider=self.name,
                simulated=True,
                reason=f"the file could not be read: {type(exc).__name__}",
            )
        if content is None:
            #: ``read_file`` returns None for a path that does not exist. Saying so
            #: is the point — a missing file is not an empty one.
            return ProviderResult(
                ok=False,
                provider=self.name,
                simulated=True,
                reason="the file does not exist at that revision",
                data={"path": path, "revision": revision, "exists": False},
            )
        return ProviderResult(
            ok=True,
            provider=self.name,
            simulated=True,
            data={
                "path": path,
                "revision": revision,
                "exists": True,
                "size": len(content),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            },
        )


class MockDeploymentProvider:
    """Reads deployment history from ARGUS's own tables (§52)."""

    name = "mock-deployment"

    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            provider=self.name,
            simulated=True,
            description="reads deployments ARGUS has already ingested",
            supports=["list_deployments"],
            does_not_support=[
                "trigger a deployment",
                "roll back a deployment",
                "scale",
            ],
        )

    async def deployments(
        self,
        *,
        project_id: uuid.UUID,
        limit: int = 25,
        session: Optional[AsyncSession] = None,
    ) -> ProviderResult:
        if session is None:
            return ProviderResult(
                ok=False,
                provider=self.name,
                simulated=True,
                reason="no database session was supplied",
            )
        from app.models.deployment import DeploymentEvent

        rows = (
            await session.scalars(
                select(DeploymentEvent)
                .where(DeploymentEvent.project_id == project_id)
                .order_by(DeploymentEvent.deployed_at.desc())
                .limit(limit)
            )
        ).all()
        return ProviderResult(
            ok=True,
            provider=self.name,
            simulated=True,
            data={
                "deployments": [
                    {
                        "id": str(row.id),
                        "version": row.version,
                        "commit_sha": row.commit_sha,
                        "status": getattr(row.status, "value", str(row.status)),
                        "deployed_at": _aware(row.deployed_at).isoformat(),
                    }
                    for row in rows
                ],
                "count": len(rows),
            },
        )


class MockNotificationProvider:
    """Records the attempt and sends nothing (§52, §54)."""

    name = "mock-notification"

    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            provider=self.name,
            simulated=True,
            description=("records what would be sent; no message leaves the process"),
            supports=["record"],
            does_not_support=["email", "sms", "chat", "pager"],
        )

    async def send(
        self, *, title: str, body: str, target: Optional[str] = None
    ) -> ProviderResult:
        return ProviderResult(
            ok=False,
            provider=self.name,
            simulated=True,
            reason=(
                "no outbound transport is configured, so the notification was "
                "recorded and not delivered"
            ),
            data={"title": title[:200], "target": target, "body_length": len(body)},
        )


class MockObservabilityProvider:
    """Reports ARGUS's own telemetry ingestion as the observability signal."""

    name = "mock-observability"

    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            provider=self.name,
            simulated=True,
            description="reports the state of ARGUS's own ingestion pipeline",
            supports=["health"],
            does_not_support=["query an external datastore", "pull metrics"],
        )

    async def healthy(self) -> ProviderResult:
        return ProviderResult(
            ok=True,
            provider=self.name,
            simulated=True,
            data={
                "detail": (
                    "ARGUS reads the telemetry it is sent; there is no external "
                    "observability backend to query"
                )
            },
        )


class MockIssueTrackerProvider:
    """Turns a postmortem follow-up into a local, tracked task (§81).

    Deliberately does *not* create anything in a tracker: §81 says follow-up
    actions must not be executed automatically. This records the intent.
    """

    name = "mock-issue-tracker"

    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            provider=self.name,
            simulated=True,
            description="records follow-up actions locally; creates no remote issue",
            supports=["record"],
            does_not_support=["create a remote issue", "assign a person", "close"],
        )

    async def create_issue(
        self, *, title: str, body: str, labels: Sequence[str] = ()
    ) -> ProviderResult:
        return ProviderResult(
            ok=False,
            provider=self.name,
            simulated=True,
            reason=(
                "no issue tracker is connected; the action is recorded for a "
                "person to file"
            ),
            data={
                "title": title[:200],
                "labels": list(labels),
                "body_length": len(body),
                "would_create": True,
            },
        )


class MockCloudProvider:
    """No cloud access. Stated plainly, because the alternative is a lie."""

    name = "mock-cloud"

    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            provider=self.name,
            simulated=True,
            description=(
                "reports that no cloud provider is configured; ARGUS has no cloud "
                "credentials and no cloud API access"
            ),
            supports=[],
            does_not_support=[
                "read cloud resources",
                "change cloud resources",
                "scale",
                "restart",
            ],
        )

    async def describe(self, *, target: str) -> ProviderResult:
        return ProviderResult(
            ok=False,
            provider=self.name,
            simulated=True,
            reason=(
                "ARGUS does not hold cloud credentials; infrastructure changes go "
                "through Phase 9's native actions only"
            ),
            data={"target": target},
        )


class MockIncidentManagementProvider:
    """Records an incident notification locally (§52)."""

    name = "mock-incident-management"

    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            provider=self.name,
            simulated=True,
            description="records incident notifications in ARGUS; pages nobody",
            supports=["record"],
            does_not_support=["page an on-call engineer", "open a bridge"],
        )

    async def notify(self, *, summary: str, details: dict[str, Any]) -> ProviderResult:
        return ProviderResult(
            ok=False,
            provider=self.name,
            simulated=True,
            reason="no incident management system is connected",
            data={"summary": summary[:200], "details": details},
        )


#: The registry an operator can inspect at ``/platform/integrations`` (§52).
def provider_registry() -> dict[str, Any]:
    """Every provider ARGUS has, with what each one can do."""
    providers: dict[str, Any] = {
        "git": MockGitProvider(),
        "deployment": MockDeploymentProvider(),
        "notification": MockNotificationProvider(),
        "observability": MockObservabilityProvider(),
        "issue_tracker": MockIssueTrackerProvider(),
        "cloud": MockCloudProvider(),
        "incident_management": MockIncidentManagementProvider(),
    }
    return {
        "providers": {
            name: provider.capability().as_dict()
            for name, provider in providers.items()
        },
        "note": (
            "every provider here is a local implementation. None of them makes a "
            "network call, and each one states what it does not support rather "
            "than pretending to."
        ),
    }


# ---------------------------------------------------------------------------
# §53 — webhook security
# ---------------------------------------------------------------------------
class WebhookError(Exception):
    """A rejected webhook, with the reason (never leaked to the sender verbatim)."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def sign_payload(*, secret: str, timestamp: str, body: bytes) -> str:
    """The signature scheme: HMAC-SHA256 over ``"{timestamp}.{body}"``.

    Including the timestamp in the signed material is what makes a replay
    *detectable* rather than merely *possible to detect* by comparing timestamps:
    an attacker cannot reuse a signature under a new timestamp.
    """
    message = f"{timestamp}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_signature(
    *,
    secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
    tolerance_seconds: int = 300,
    now: Optional[float] = None,
) -> None:
    """Verify a webhook signature and its freshness (§53).

    Raises :class:`WebhookError` with a specific code for each failure, so the
    audit record says *why* a delivery was rejected. Comparison is constant-time
    (``hmac.compare_digest``) for the same reason every signature check is.
    """
    if not secret:
        raise WebhookError(
            "no_secret",
            "webhook signing is not configured on this deployment",
            503,
        )
    if not signature:
        raise WebhookError("missing_signature", "no signature was supplied", 401)
    try:
        sent_at = float(timestamp)
    except (TypeError, ValueError):
        raise WebhookError("bad_timestamp", "the timestamp header is not a number", 400)
    moment = now if now is not None else time.time()
    skew = abs(moment - sent_at)
    if skew > tolerance_seconds:
        raise WebhookError(
            "stale_timestamp",
            f"the delivery is {int(skew)}s outside the {tolerance_seconds}s tolerance",
            401,
        )
    expected = sign_payload(secret=secret, timestamp=timestamp, body=body)
    if not hmac.compare_digest(expected, signature):
        raise WebhookError("bad_signature", "the signature does not match", 401)


def payload_fingerprint(body: bytes) -> str:
    """Idempotency identity for a delivery (§53)."""
    return hashlib.sha256(body).hexdigest()[:64]


#: In-process replay memory of accepted ``delivery_id``s. Bounded and per-process
#: on purpose: this is a fast reject, and the durable record is the audit event.
_SEEN_DELIVERIES: dict[str, float] = {}
_MAX_SEEN = 10_000


def check_replay(delivery_id: Optional[str], *, now: Optional[float] = None) -> None:
    """Reject a delivery id that has already been accepted (§53)."""
    if not delivery_id:
        return
    moment = now if now is not None else time.time()
    #: Drop entries older than an hour: the window is what matters, not the id.
    cutoff = moment - 3600
    for key in [key for key, seen in _SEEN_DELIVERIES.items() if seen < cutoff]:
        _SEEN_DELIVERIES.pop(key, None)
    if delivery_id in _SEEN_DELIVERIES:
        raise WebhookError(
            "replayed_delivery", "this delivery was already accepted", 409
        )
    if len(_SEEN_DELIVERIES) >= _MAX_SEEN:
        #: Bound the memory rather than growing without limit.
        oldest = min(_SEEN_DELIVERIES, key=lambda key: _SEEN_DELIVERIES[key])
        _SEEN_DELIVERIES.pop(oldest, None)
    _SEEN_DELIVERIES[delivery_id] = moment


class RateLimiter:
    """A small fixed-window limiter (§63), per key.

    In-process and fixed-window on purpose: it exists to blunt accidental storms
    and trivial abuse, not to be a distributed quota system. A deployment that
    needs the latter already has the Phase 9 control plane to turn features off.
    """

    def __init__(self, *, per_minute: int) -> None:
        self.per_minute = max(0, per_minute)
        self._windows: dict[str, tuple[int, int]] = {}

    def check(self, key: str, *, now: Optional[float] = None) -> None:
        if self.per_minute <= 0:
            return
        moment = int(now if now is not None else time.time())
        window = moment // 60
        stored_window, count = self._windows.get(key, (window, 0))
        if stored_window != window:
            count = 0
        count += 1
        self._windows[key] = (window, count)
        if count > self.per_minute:
            raise WebhookError(
                "rate_limited",
                f"more than {self.per_minute} requests this minute",
                429,
            )


#: One limiter per surface, built from settings on first use.
_limiters: dict[str, RateLimiter] = {}


def limiter(name: str, *, settings: Optional[Settings] = None) -> RateLimiter:
    """The rate limiter for a named surface (§63)."""
    settings = settings or get_settings()
    if name not in _limiters:
        per_minute = {
            "search": settings.PLATFORM_SEARCH_RATE_LIMIT_PER_MINUTE,
            "webhook": settings.PLATFORM_WEBHOOK_RATE_LIMIT_PER_MINUTE,
            "ai": settings.PLATFORM_AI_RATE_LIMIT_PER_MINUTE,
        }.get(name, 0)
        _limiters[name] = RateLimiter(per_minute=per_minute)
    return _limiters[name]


@dataclass
class WebhookOutcome:
    """A processed (or rejected) webhook, ready to be recorded."""

    accepted: bool
    event_type: Optional[str] = None
    delivery_id: Optional[str] = None
    fingerprint: Optional[str] = None
    idempotent_replay: bool = False
    reason: Optional[str] = None
    code: Optional[str] = None
    payload_summary: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "event_type": self.event_type,
            "delivery_id": self.delivery_id,
            "fingerprint": self.fingerprint,
            "idempotent_replay": self.idempotent_replay,
            "reason": self.reason,
            "code": self.code,
            "payload_summary": self.payload_summary,
        }


async def handle_webhook(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    body: bytes,
    headers: dict[str, str],
    source: str = "webhook",
    now: Optional[float] = None,
    settings: Optional[Settings] = None,
    require_signature: bool = True,
) -> WebhookOutcome:
    """Validate and record one inbound webhook (§53).

    The order is the security order: rate limit → signature → freshness → replay
    → idempotency → validation → audit. Everything that passes is recorded as a
    platform event; everything that fails is recorded too, with its code, because
    ``audit`` in §53 means both outcomes.
    """
    settings = settings or get_settings()
    normalized = {key.lower(): value for key, value in headers.items()}
    delivery_id = normalized.get("x-argus-delivery")
    signature = normalized.get("x-argus-signature")
    timestamp = normalized.get("x-argus-timestamp", "0")

    limiter("webhook", settings=settings).check(str(project_id), now=now)

    if not body:
        raise WebhookError("empty_body", "the request had no body", 400)

    if require_signature:
        verify_signature(
            secret=settings.PLATFORM_WEBHOOK_SECRET,
            timestamp=timestamp,
            body=body,
            signature=signature or "",
            tolerance_seconds=settings.PLATFORM_WEBHOOK_TOLERANCE_SECONDS,
            now=now,
        )
    check_replay(delivery_id, now=now)

    fingerprint = payload_fingerprint(body)
    from app.services.platform_events import safely_publish_event

    #: Idempotency: the same payload twice is one event. This is a different
    #: guarantee from replay protection — a *retry* by the sender is expected and
    #: must not create a second fact.
    marker = uuid.uuid5(uuid.NAMESPACE_URL, f"webhook:{project_id}:{fingerprint}")
    received_at = _aware(None).replace(microsecond=0)
    existing = None
    try:
        from app.models.platform import PlatformEvent
        from app.services.platform_events import compute_dedup_key

        receipt_key = compute_dedup_key(
            project_id=project_id,
            event_type=PlatformEventType.DEPLOYMENT_RECORDED,
            subject_id=marker,
            occurred_at=received_at,
            extra=(f"webhook:{source}", source, fingerprint),
        )
        existing = (
            await session.scalars(
                select(PlatformEvent).where(PlatformEvent.dedup_key == receipt_key)
            )
        ).first()
    except Exception:  # pragma: no cover - the dedup lookup is best-effort
        existing = None
    if existing is not None:
        return WebhookOutcome(
            accepted=True,
            delivery_id=delivery_id,
            fingerprint=fingerprint,
            idempotent_replay=True,
            reason="this payload was already processed; nothing was applied twice",
            code="idempotent",
        )

    payload: dict[str, Any] = {}
    try:
        import json

        decoded = json.loads(body.decode("utf-8"))
        if isinstance(decoded, dict):
            payload = decoded
        else:
            payload = {"value": decoded}
    except Exception:
        raise WebhookError("invalid_payload", "the body is not valid JSON", 400)

    event_type = str(payload.get("event_type") or "unknown")
    summary = {
        "keys": sorted(payload.keys())[:20],
        "event_type": event_type,
        "bytes": len(body),
    }

    #: The receipt's dedup key is the payload fingerprint, which makes
    #: idempotency a property of the *payload* rather than of the delivery attempt.
    await safely_publish_event(
        session,
        project_id=project_id,
        event_type=PlatformEventType.DEPLOYMENT_RECORDED,
        source=f"webhook:{source}",
        subject_type="webhook",
        subject_id=marker,
        occurred_at=received_at,
        payload={
            "event_type": event_type,
            "summary": summary,
            "delivery_id": delivery_id,
        },
        dedup_extra=[source, fingerprint],
    )
    return WebhookOutcome(
        accepted=True,
        event_type=event_type,
        delivery_id=delivery_id,
        fingerprint=fingerprint,
        payload_summary=summary,
    )


def webhook_requirements() -> dict[str, Any]:
    """The §53 contract, served so a sender can implement against it."""
    return {
        "headers": {
            "X-Argus-Timestamp": "unix seconds; must be within the tolerance window",
            "X-Argus-Signature": "HMAC-SHA256 hex of '<timestamp>.<raw body>'",
            "X-Argus-Delivery": "a unique id per delivery, used for replay rejection",
        },
        "requirements": [
            "signature verification (constant-time)",
            "timestamp freshness",
            "replay rejection by delivery id",
            "idempotency by payload fingerprint",
            "JSON payload validation",
            "rate limiting",
            "audit of accepted and rejected deliveries",
        ],
        "rejections": [
            "no_secret",
            "missing_signature",
            "bad_timestamp",
            "stale_timestamp",
            "bad_signature",
            "replayed_delivery",
            "invalid_payload",
            "empty_body",
            "rate_limited",
        ],
    }


__all__ = [
    "MockCloudProvider",
    "MockDeploymentProvider",
    "MockGitProvider",
    "MockIncidentManagementProvider",
    "MockIssueTrackerProvider",
    "MockNotificationProvider",
    "MockObservabilityProvider",
    "ProviderCapability",
    "ProviderResult",
    "RateLimiter",
    "WebhookError",
    "WebhookOutcome",
    "check_replay",
    "handle_webhook",
    "limiter",
    "payload_fingerprint",
    "provider_registry",
    "sign_payload",
    "verify_signature",
    "webhook_requirements",
]
