"""ARGUS Replay Engine (Phase 5 §17–§20).

Sends sanitized inputs into a sandbox and records exactly what came back.

The engine is where "safe by construction" has to be *enforced*, not assumed, so
every item passes a validation gate immediately before transmission (§18). A
failed validation is a recorded ``REJECTED`` outcome and **nothing is sent** —
the engine never partially trusts an item because sanitization happened earlier
in the pipeline.

Timing is relative, never absolute (§20). Original wall-clock timestamps are
kept as provenance only: reproducing a 14:20 incident at 09:05 has to preserve
the *intervals*, because the failure is a property of ordering and distance, not
of the clock face.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import httpx

from app.core.config import get_settings
from app.models.reproduction import ReplayInputSource, ReplayMode, ReplayStatus
from app.services.reproduction_sandbox import SandboxHandle, SandboxError
from app.services.reproduction_sanitizer import InputSanitizer

logger = logging.getLogger(__name__)
settings = get_settings()

#: Body bytes retained per replay response. Enough to quote the error, far too
#: little to accumulate an unbounded artifact.
BODY_PREVIEW_BYTES = 512

#: Concurrency ceiling for the parallel/burst modes. §19 makes concurrency
#: opt-in; this makes it also bounded.
MAX_PARALLELISM = 8

#: Interval used by RATE_LIMITED mode.
RATE_LIMIT_INTERVAL_SECONDS = 0.1


class ReplayError(SandboxError):
    """Raised for replay-level failures that invalidate a whole run."""


def payload_hash(payload: Optional[dict[str, Any]]) -> str:
    """Stable content hash of a replay payload (sorted keys, no whitespace)."""
    encoded = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class ReplayItem:
    """One input to replay, already sanitized and bounds-checked."""

    method: str
    target_service: str
    target_path: str
    payload: dict[str, Any] = field(default_factory=dict)
    relative_offset_ms: int = 0
    source: ReplayInputSource = ReplayInputSource.SYNTHETIC
    original_timestamp: Optional[datetime] = None
    plan_order: int = 0
    replay_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    def hash(self) -> str:
        return payload_hash(self.payload)


@dataclass
class ReplayResult:
    """The observed outcome of one replay item."""

    replay_id: str
    target_service: str
    method: str
    path: str
    plan_order: int
    status: ReplayStatus
    status_code: Optional[int] = None
    duration_ms: Optional[int] = None
    replay_timestamp: Optional[datetime] = None
    response_summary: Optional[dict[str, Any]] = None
    reject_reason: Optional[str] = None
    error: Optional[str] = None
    relative_offset_ms: int = 0
    payload_hash: Optional[str] = None
    redactions: Optional[list[dict[str, Any]]] = None

    @property
    def succeeded(self) -> bool:
        return self.status is ReplayStatus.SUCCEEDED

    @property
    def failed(self) -> bool:
        return self.status is ReplayStatus.FAILED


@dataclass
class ReplayOutcome:
    """Aggregate of a replay pass over every planned item."""

    results: list[ReplayResult] = field(default_factory=list)
    mode: ReplayMode = ReplayMode.SEQUENTIAL
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @property
    def request_count(self) -> int:
        return len(self.results)

    @property
    def success_count(self) -> int:
        return sum(1 for item in self.results if item.succeeded)

    @property
    def failure_count(self) -> int:
        return sum(1 for item in self.results if item.failed)

    @property
    def rejected_count(self) -> int:
        return sum(1 for item in self.results if item.status is ReplayStatus.REJECTED)

    @property
    def duration_ms(self) -> Optional[int]:
        if self.started_at is None or self.completed_at is None:
            return None
        return int((self.completed_at - self.started_at).total_seconds() * 1000)


class ReplayEngine:
    """Validates and replays inputs into one sandbox."""

    def __init__(self, sanitizer: Optional[InputSanitizer] = None) -> None:
        self._sanitizer = sanitizer or InputSanitizer()

    # -- preparation -----------------------------------------------------
    def prepare(self, items: Sequence[ReplayItem]) -> list[ReplayItem]:
        """Bound the replay set and re-sanitize every payload.

        Sanitization is applied here *and* re-verified at transmission. Doing it
        twice is intentional: this call makes the stored item safe, the gate
        makes the sent bytes safe, and neither is asked to prove the other.
        """
        if len(items) > settings.REPRO_MAX_REPLAY_REQUESTS:
            raise ReplayError(
                f"Replay set of {len(items)} exceeds the limit of "
                f"{settings.REPRO_MAX_REPLAY_REQUESTS} requests per run"
            )
        prepared: list[ReplayItem] = []
        for order, item in enumerate(items):
            sanitized, report = self._sanitizer.sanitize_payload(dict(item.payload))
            prepared.append(
                ReplayItem(
                    method=item.method.upper(),
                    target_service=item.target_service,
                    target_path=item.target_path,
                    payload=sanitized,
                    relative_offset_ms=item.relative_offset_ms,
                    source=item.source,
                    original_timestamp=item.original_timestamp,
                    plan_order=item.plan_order if item.plan_order else order,
                    replay_id=item.replay_id,
                )
            )
            _ = report  # report is persisted by the caller via the input row
        return prepared

    # -- validation gate (§18) -------------------------------------------
    def validate(self, item: ReplayItem, handle: SandboxHandle) -> Optional[str]:
        """Return a rejection reason, or ``None`` when the item may be sent."""
        if item.target_service not in handle.services:
            return f"target service {item.target_service!r} is not part of this sandbox"
        port = handle.service_port(item.target_service)
        if not port:
            return f"target service {item.target_service!r} has no bound port"
        if not handle.root_path.exists():
            return "sandbox working tree no longer exists"
        if not item.target_path.startswith("/"):
            return "target path must be absolute inside the sandbox"
        if (
            ".." in item.target_path
            or "//" in item.target_path
            or ":" in item.target_path
        ):
            return "target path must not contain '..', '//' or ':'"
        if item.method not in {
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "HEAD",
            "OPTIONS",
        }:
            return f"method {item.method!r} is not permitted"
        findings = self._sanitizer.assert_sanitized(item.payload)
        if findings:
            # The one rejection that protects real data: never transmit a
            # payload that still looks sensitive.
            return f"payload failed sanitization re-check ({findings[0]})"
        return None

    # -- execution -------------------------------------------------------
    async def replay(
        self,
        items: Sequence[ReplayItem],
        handle: SandboxHandle,
        *,
        mode: ReplayMode = ReplayMode.SEQUENTIAL,
        deadline: Optional[float] = None,
        on_item: Optional[Any] = None,
    ) -> ReplayOutcome:
        """Replay every item, returning per-item observations.

        ``on_item`` is an optional async callback invoked after each *successful
        or failed* transmission with ``(index, result)``; the orchestrator uses
        it to advance offset-triggered faults between items. Rejected items are
        not announced, because nothing happened in the sandbox.
        """
        outcome = ReplayOutcome(mode=mode)
        outcome.started_at = _utcnow()
        if not items:
            outcome.completed_at = _utcnow()
            return outcome

        sanitized = self.prepare(items)
        accepted: list[ReplayItem] = []
        for item in sanitized:
            reason = self.validate(item, handle)
            if reason is not None:
                outcome.results.append(
                    ReplayResult(
                        replay_id=item.replay_id,
                        target_service=item.target_service,
                        method=item.method,
                        path=item.target_path,
                        plan_order=item.plan_order,
                        status=ReplayStatus.REJECTED,
                        reject_reason=reason,
                        relative_offset_ms=item.relative_offset_ms,
                        payload_hash=item.hash(),
                    )
                )
                logger.warning(
                    "Replay item %s rejected by safety validation: %s",
                    item.replay_id,
                    reason,
                )
                continue
            accepted.append(item)

        if not accepted:
            outcome.completed_at = _utcnow()
            return outcome

        timeout = self._client_timeout(handle, accepted)
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:
            if mode in {ReplayMode.PARALLEL, ReplayMode.BURST}:
                results = await self._replay_concurrent(
                    client, accepted, handle, deadline, on_item
                )
            else:
                results = await self._replay_sequential(
                    client, accepted, handle, mode, deadline, on_item
                )
        outcome.results.extend(results)
        outcome.completed_at = _utcnow()
        return outcome

    @staticmethod
    def _client_timeout(
        handle: SandboxHandle, items: Sequence[ReplayItem]
    ) -> httpx.Timeout:
        """Client timeout derived from the sandbox's own service timeouts.

        The client must outlast the service so an observed failure is the
        *service* failing (its own 504) rather than the harness giving up first.
        Where a service has no declared timeout, a conservative floor applies.
        """
        configs = handle.metadata.get("service_configs", {})
        longest_ms = 0
        for item in items:
            config = configs.get(item.target_service, {})
            longest_ms = max(longest_ms, int(config.get("timeout_ms") or 0))
        seconds = max(5.0, (longest_ms / 1000.0) * 2.5)
        return httpx.Timeout(seconds, connect=2.0)

    async def _replay_sequential(
        self,
        client: httpx.AsyncClient,
        items: Sequence[ReplayItem],
        handle: SandboxHandle,
        mode: ReplayMode,
        deadline: Optional[float],
        on_item: Optional[Any],
    ) -> list[ReplayResult]:
        results: list[ReplayResult] = []
        started = time.monotonic()
        for index, item in enumerate(items):
            if deadline is not None and time.monotonic() > deadline:
                results.append(self._skipped(item, "experiment deadline reached"))
                continue
            if mode is ReplayMode.TIMED and item.relative_offset_ms:
                elapsed_ms = (time.monotonic() - started) * 1000.0
                wait_ms = item.relative_offset_ms - elapsed_ms
                if wait_ms > 0:
                    await asyncio.sleep(wait_ms / 1000.0)
            if mode is ReplayMode.RATE_LIMITED and index:
                await asyncio.sleep(RATE_LIMIT_INTERVAL_SECONDS)
            result = await self._send(client, item, handle)
            results.append(result)
            if on_item is not None:
                await on_item(index, result)
        return results

    async def _replay_concurrent(
        self,
        client: httpx.AsyncClient,
        items: Sequence[ReplayItem],
        handle: SandboxHandle,
        deadline: Optional[float],
        on_item: Optional[Any],
    ) -> list[ReplayResult]:
        """Parallel/burst replay with a bounded semaphore.

        Ordering of the *results* follows plan order regardless of completion
        order, so a concurrent run still produces a comparable sequence.
        """
        semaphore = asyncio.Semaphore(min(MAX_PARALLELISM, max(1, len(items))))
        results: list[Optional[ReplayResult]] = [None] * len(items)

        async def run(index: int, item: ReplayItem) -> None:
            async with semaphore:
                if deadline is not None and time.monotonic() > deadline:
                    results[index] = self._skipped(item, "experiment deadline reached")
                    return
                if item.relative_offset_ms:
                    await asyncio.sleep(item.relative_offset_ms / 1000.0)
                results[index] = await self._send(client, item, handle)
                if on_item is not None:
                    await on_item(index, results[index])

        await asyncio.gather(*(run(i, item) for i, item in enumerate(items)))
        return [item for item in results if item is not None]

    async def _send(
        self, client: httpx.AsyncClient, item: ReplayItem, handle: SandboxHandle
    ) -> ReplayResult:
        """Transmit one validated item and summarize the response."""
        port = handle.service_port(item.target_service)
        # Host and scheme are constructed here from the sandbox's own handle;
        # a client can never supply a destination.
        url = f"http://127.0.0.1:{port}{item.target_path}"
        started = time.monotonic()
        timestamp = _utcnow()
        try:
            response = await client.request(
                item.method,
                url,
                json=item.payload if item.method in {"POST", "PUT", "PATCH"} else None,
                headers={"Accept": "application/json", "User-Agent": "argus-replay/1"},
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            preview = response.text[:BODY_PREVIEW_BYTES]
            summary = {
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type"),
                "body_preview": preview,
                "body_hash": hashlib.sha256(response.content).hexdigest(),
                "error": _extract_error(preview),
            }
            return ReplayResult(
                replay_id=item.replay_id,
                target_service=item.target_service,
                method=item.method,
                path=item.target_path,
                plan_order=item.plan_order,
                status=ReplayStatus.SUCCEEDED
                if response.status_code < 500
                else ReplayStatus.FAILED,
                status_code=response.status_code,
                duration_ms=duration_ms,
                replay_timestamp=timestamp,
                response_summary=summary,
                error=None if response.status_code < 400 else _extract_error(preview),
                relative_offset_ms=item.relative_offset_ms,
                payload_hash=item.hash(),
            )
        except httpx.TimeoutException:
            return self._transport_failure(
                item, timestamp, started, "client timeout while awaiting response"
            )
        except httpx.HTTPError as exc:
            return self._transport_failure(
                item,
                timestamp,
                started,
                f"{type(exc).__name__}: {str(exc)[:200]}",
            )
        except Exception as exc:  # noqa: BLE001 - never let one item kill a run
            return self._transport_failure(
                item, timestamp, started, f"{type(exc).__name__}: {str(exc)[:200]}"
            )

    @staticmethod
    def _transport_failure(
        item: ReplayItem, timestamp: datetime, started: float, error: str
    ) -> ReplayResult:
        """A transport-level failure is an observation, not a crash.

        A connection reset is exactly what a CONNECTION_FAILURE fault produces,
        so this path must produce a recorded result the comparator can use.
        """
        return ReplayResult(
            replay_id=item.replay_id,
            target_service=item.target_service,
            method=item.method,
            path=item.target_path,
            plan_order=item.plan_order,
            status=ReplayStatus.FAILED,
            duration_ms=int((time.monotonic() - started) * 1000),
            replay_timestamp=timestamp,
            response_summary={"transport_error": error},
            error=error,
            relative_offset_ms=item.relative_offset_ms,
            payload_hash=item.hash(),
        )

    @staticmethod
    def _skipped(item: ReplayItem, reason: str) -> ReplayResult:
        return ReplayResult(
            replay_id=item.replay_id,
            target_service=item.target_service,
            method=item.method,
            path=item.target_path,
            plan_order=item.plan_order,
            status=ReplayStatus.SKIPPED,
            reject_reason=reason,
            relative_offset_ms=item.relative_offset_ms,
            payload_hash=item.hash(),
        )


def _extract_error(preview: str) -> Optional[str]:
    """Pull a human-readable error out of a JSON body preview, if present."""
    try:
        decoded = json.loads(preview)
    except json.JSONDecodeError:
        return None
    if isinstance(decoded, dict):
        value = decoded.get("error") or decoded.get("detail")
        return str(value)[:300] if value else None
    return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "BODY_PREVIEW_BYTES",
    "MAX_PARALLELISM",
    "ReplayEngine",
    "ReplayError",
    "ReplayItem",
    "ReplayOutcome",
    "ReplayResult",
    "payload_hash",
]
