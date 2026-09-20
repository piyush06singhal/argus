"""ARGUS Reproduction Telemetry Capture (Phase 5 §23–§26).

Turns what the sandbox actually did into observations ARGUS can compare, judge
against expectations, and store.

The capture reads the sandbox's own telemetry files rather than accepting telemetry
pushed out of it. That is a deliberate inversion of the Phase 1 flow (where
external systems push to ARGUS): a sandbox that can push *into* ARGUS holds a
channel it could abuse, whereas a file inside a working tree the sandbox already
owns holds nothing. It also means a crashed or unreachable collector cannot lose
the evidence — the files are still there.

Three guarantees this module carries:

* **Namespace isolation (§24).** Every observation is stamped
  ``repro:<experiment_id>``. Reproduction telemetry is never written into the
  incident's own namespace, so a reproduction can never be mistaken for the
  thing it reproduces.
* **Expectation labelling (§26).** Each observation is judged against the plan's
  expected behaviour at capture time, and expectations that were *not* satisfied
  become explicit ``MISSING`` observations. A clean run and a run that never
  exercised the failure path must not look the same.
* **Bounded capture (§40).** Signal count and byte ceilings are enforced, with a
  recorded truncation marker instead of silent loss.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from app.core.config import get_settings
from app.models.reproduction import ObservationSignal, ObservationStatus
from app.services.reproduction_expectations import (
    ExpectationMatcher,
    ExpectedBehavior,
)
from app.services.reproduction_sandbox import SandboxHandle

logger = logging.getLogger(__name__)
settings = get_settings()

#: Signal-type mapping from the runners' JSONL line types.
_LINE_TYPES: dict[str, ObservationSignal] = {
    "span": ObservationSignal.SPAN,
    "log": ObservationSignal.LOG,
    "metric": ObservationSignal.METRIC,
    "health": ObservationSignal.HEALTH,
    "event": ObservationSignal.EVENT,
    "trace": ObservationSignal.TRACE,
    "deployment": ObservationSignal.DEPLOYMENT,
    "configuration": ObservationSignal.CONFIGURATION,
}

#: Log levels that count as an error observation.
_ERROR_LEVELS = {"ERROR", "FATAL", "CRITICAL"}


@dataclass
class CapturedSignal:
    """One captured signal, in the shape of a reproduction observation row.

    A plain dataclass rather than an ORM object so the comparator and the
    validator can work on captured data without a database session, which is
    what lets them be unit-tested against fixtures.
    """

    signal_type: ObservationSignal
    status: ObservationStatus
    matched_expected: bool
    observed_at: datetime
    relative_offset_ms: int
    namespace: str
    component_name: Optional[str] = None
    source: Optional[str] = None
    metric_name: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    expected_value: Optional[float] = None
    severity: Optional[str] = None
    message: Optional[str] = None
    operation: Optional[str] = None
    duration_ms: Optional[float] = None
    error: bool = False
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    parent_span_id: Optional[str] = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class CaptureResult:
    """Everything one capture pass observed."""

    namespace: str
    signals: list[CapturedSignal] = field(default_factory=list)
    missing: list[CapturedSignal] = field(default_factory=list)
    bytes_read: int = 0
    truncated: bool = False
    services_seen: list[str] = field(default_factory=list)
    parse_errors: int = 0

    @property
    def observations(self) -> list[CapturedSignal]:
        """Everything to persist — observed signals plus explicit absences."""
        return self.signals + self.missing

    @property
    def total_signals(self) -> int:
        return len(self.signals)

    @property
    def error_signals(self) -> list[CapturedSignal]:
        return [item for item in self.signals if item.error]

    @property
    def matched_count(self) -> int:
        return sum(1 for item in self.signals if item.matched_expected)

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    def by_component(self) -> dict[str, list[CapturedSignal]]:
        grouped: dict[str, list[CapturedSignal]] = {}
        for item in self.signals:
            grouped.setdefault(item.component_name or "unknown", []).append(item)
        return grouped


class TelemetryCaptureService:
    """Captures and judges sandbox telemetry."""

    def __init__(self) -> None:
        self._max_signals = settings.REPRO_MAX_TELEMETRY_SIGNALS
        self._max_bytes = settings.REPRO_MAX_TELEMETRY_BYTES

    @staticmethod
    def namespace_for(experiment_id: Any) -> str:
        """The reproduction namespace for an experiment (§24)."""
        return f"repro:{experiment_id}"

    # -- capture ---------------------------------------------------------
    async def wait_for_quiescence(
        self,
        handle: SandboxHandle,
        *,
        min_wait: float = 0.4,
        max_wait: float = 4.0,
        poll: float = 0.2,
    ) -> float:
        """Wait until the sandbox stops producing telemetry.

        This matters more than it looks. A faulted request keeps running inside
        the sandbox *after* the caller has given up — the datastore sleeps its
        injected latency, then records the span that proves the fault was
        applied. Capturing too early would miss exactly the evidence that
        distinguishes an injected fault from a natural failure, and the loss is
        silent: the file simply ends earlier.

        Two independent signals are used, because either alone is wrong:

        1. each service reports its own in-flight request count (``inflight``),
           and a service is drained when that is zero — a count of *the work
           the sandbox knows it is doing*;
        2. telemetry files must then stop changing across two polls, which
           covers the gap between a service answering the probe and the last
           buffered line reaching disk.
        """
        await asyncio.sleep(min_wait)
        deadline = time.monotonic() + max_wait
        drained = False
        while time.monotonic() < deadline:
            if await self._sandbox_drained(handle):
                drained = True
                break
            await asyncio.sleep(poll)
        if drained:
            previous = self._telemetry_fingerprint(handle)
            while time.monotonic() < deadline:
                await asyncio.sleep(poll)
                current = self._telemetry_fingerprint(handle)
                if current == previous:
                    break
                previous = current
        elapsed = max(
            0.0, min(max_wait, poll + (max_wait - (deadline - time.monotonic())))
        )
        return elapsed

    @staticmethod
    async def _sandbox_drained(handle: SandboxHandle) -> bool:
        """True when every reachable sandbox service reports zero in-flight."""
        reached = 0
        for name in handle.services:
            port = handle.service_port(name)
            if not port:
                continue
            status = await asyncio.get_running_loop().run_in_executor(
                None, _probe_status, port
            )
            if status is None:
                # A service that is gone cannot be doing work.
                continue
            reached += 1
            if int(status.get("inflight") or 0) > 0:
                return False
        return reached > 0 or not handle.services

    @staticmethod
    def _telemetry_fingerprint(handle: SandboxHandle) -> dict[str, tuple[int, float]]:
        telemetry_dir = handle.root_path / "telemetry"
        fingerprint: dict[str, tuple[int, float]] = {}
        if not telemetry_dir.exists():
            return fingerprint
        for path in telemetry_dir.glob("*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            fingerprint[path.name] = (stat.st_size, round(stat.st_mtime, 3))
        return fingerprint

    async def capture(
        self,
        handle: SandboxHandle,
        *,
        experiment_id: Any,
        expected: Optional[ExpectedBehavior] = None,
        run_started_at: Optional[datetime] = None,
        wait: bool = True,
    ) -> CaptureResult:
        """Wait for the sandbox to settle, then capture and judge its telemetry."""
        if wait:
            await self.wait_for_quiescence(handle)
        return self.capture_from_root(
            handle.root_path,
            experiment_id=experiment_id,
            expected=expected,
            run_started_at=run_started_at,
        )

    def capture_from_root(
        self,
        root: Path,
        *,
        experiment_id: Any,
        expected: Optional[ExpectedBehavior] = None,
        run_started_at: Optional[datetime] = None,
    ) -> CaptureResult:
        """Read every service's telemetry and judge it against expectations.

        Pure with respect to the sandbox: it reads files and returns signals, so
        it can be unit-tested against a directory of recorded telemetry.
        """
        namespace = self.namespace_for(experiment_id)
        behavior = expected or ExpectedBehavior()
        matcher = ExpectationMatcher(behavior)
        result = CaptureResult(namespace=namespace)
        origin = run_started_at or datetime.now(timezone.utc)

        telemetry_dir = Path(root) / "telemetry"
        if not telemetry_dir.exists():
            result.missing = self._missing_signals(
                matcher,
                namespace=namespace,
                origin=origin,
                observed=result.signals,
            )
            return result

        bytes_read = 0
        for path in sorted(telemetry_dir.glob("*.jsonl")):
            service = path.stem
            result.services_seen.append(service)
            if len(result.signals) >= self._max_signals:
                result.truncated = True
                break
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.warning("Could not read sandbox telemetry %s: %s", path, exc)
                result.parse_errors += 1
                continue
            bytes_read += len(content)
            if bytes_read > self._max_bytes:
                result.truncated = True
                content = content[: self._max_bytes]
            for line in content.splitlines():
                if len(result.signals) >= self._max_signals:
                    result.truncated = True
                    break
                signal = self._parse_line(
                    line,
                    service=service,
                    namespace=namespace,
                    matcher=matcher,
                    origin=origin,
                )
                if signal is None:
                    continue
                if signal is _PARSE_ERROR:
                    result.parse_errors += 1
                    continue
                result.signals.append(signal)

        result.bytes_read = bytes_read
        # Judged against what was *actually captured*, so an expectation that the
        # run satisfied is not also reported as missing.
        result.missing = self._missing_signals(
            matcher, namespace=namespace, origin=origin, observed=result.signals
        )
        return result

    def _parse_line(
        self,
        line: str,
        *,
        service: str,
        namespace: str,
        matcher: ExpectationMatcher,
        origin: datetime,
    ) -> Optional[CapturedSignal]:
        line = line.strip()
        if not line:
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return _PARSE_ERROR  # type: ignore[return-value]
        if not isinstance(payload, dict):
            return _PARSE_ERROR  # type: ignore[return-value]

        line_type = str(payload.get("type") or "")
        if line_type == "telemetry_truncated":
            # The runner hit its own size ceiling. Record it as an observation so
            # a short capture is visible in the UI rather than looking complete.
            return CapturedSignal(
                signal_type=ObservationSignal.LOG,
                status=ObservationStatus.UNEXPECTED,
                matched_expected=False,
                observed_at=origin,
                relative_offset_ms=0,
                namespace=namespace,
                component_name=service,
                source=service,
                severity="WARNING",
                message=(
                    "Sandbox telemetry was truncated at the configured byte "
                    f"limit: {payload.get('reason')}"
                ),
                attributes={"truncated": True, **payload},
            )

        signal_type = _LINE_TYPES.get(line_type, ObservationSignal.EVENT)
        component = str(payload.get("component") or payload.get("service") or service)
        observed_at = _parse_timestamp(payload.get("emitted_at"), origin)
        offset_ms = int(max(0.0, (observed_at - origin).total_seconds() * 1000))

        error = False
        duration_ms: Optional[float] = None
        metric_value: Optional[float] = None
        degraded = False
        severity = payload.get("level") or payload.get("severity")
        message = payload.get("message")

        if signal_type is ObservationSignal.SPAN:
            error = str(payload.get("status", "OK")).upper() == "ERROR"
            raw_duration = payload.get("duration_ms")
            duration_ms = float(raw_duration) if raw_duration is not None else None
            message = payload.get("error") or payload.get("operation")
        elif signal_type is ObservationSignal.LOG:
            error = str(severity or "").upper() in _ERROR_LEVELS
            if error:
                message = message or "sandbox logged an error"
        elif signal_type is ObservationSignal.HEALTH:
            state = str(payload.get("state") or "UP").upper()
            degraded = state != "UP"
            error = False
            message = f"health state {state}"
        elif signal_type is ObservationSignal.METRIC:
            # A metric's value is a value, not a latency: keeping it out of
            # ``duration_ms`` stops the comparator from treating gauges as
            # latency samples.
            raw_value = payload.get("value")
            metric_value = float(raw_value) if raw_value is not None else None

        attributes = dict(payload.get("attributes") or {})
        if payload.get("fault"):
            attributes.setdefault("injected_fault", payload["fault"])
        if signal_type is ObservationSignal.HEALTH:
            attributes.setdefault("state", str(payload.get("state") or "UP").upper())
        attributes.setdefault("sandbox_namespace", namespace)

        status, matched, expected_value = matcher.classify(
            component=component,
            error=error,
            duration_ms=duration_ms if signal_type is ObservationSignal.SPAN else None,
            degraded=degraded,
        )

        return CapturedSignal(
            signal_type=signal_type,
            status=ObservationStatus(status),
            matched_expected=matched,
            observed_at=observed_at,
            relative_offset_ms=offset_ms,
            namespace=namespace,
            component_name=component,
            source=str(payload.get("service") or service),
            metric_name=payload.get("name"),
            value=metric_value,
            unit=payload.get("unit"),
            expected_value=expected_value,
            severity=str(severity) if severity else None,
            message=str(message) if message else None,
            operation=payload.get("operation"),
            duration_ms=duration_ms,
            error=error,
            trace_id=payload.get("trace_id"),
            span_id=payload.get("span_id"),
            parent_span_id=payload.get("parent_span_id"),
            attributes=attributes,
        )

    @staticmethod
    def _missing_signals(
        matcher: ExpectationMatcher,
        *,
        namespace: str,
        origin: datetime,
        observed: Sequence[CapturedSignal],
    ) -> list[CapturedSignal]:
        """Explicit records for expectations the sandbox never satisfied."""
        return [
            CapturedSignal(
                signal_type=ObservationSignal.EVENT,
                status=ObservationStatus.MISSING,
                matched_expected=False,
                observed_at=origin,
                relative_offset_ms=0,
                namespace=namespace,
                component_name=expectation.component,
                source=None,
                severity="WARNING",
                message=(
                    f"Expected {expectation.kind} on {expectation.component} was "
                    "not observed in the sandbox"
                    + (f" ({expectation.evidence})" if expectation.evidence else "")
                ),
                attributes={
                    "expected_kind": expectation.kind,
                    "expected_threshold_ms": expectation.threshold_ms,
                    "missing": True,
                    "sandbox_namespace": namespace,
                },
            )
            # ``ExpectationMatcher.satisfied`` reads ``component_name``,
            # ``error``, ``duration_ms`` and ``attributes`` — exactly the shape
            # a captured signal already has, so no adapter is needed.
            for expectation in matcher.missing(observed)
        ]


def _probe_status(port: int) -> Optional[dict[str, Any]]:
    """Blocking quiescence probe against a sandbox service (thread-executed)."""
    import http.client

    connection = None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
        connection.request("GET", "/__status__")
        response = connection.getresponse()
        raw = response.read()
        if response.status >= 400:
            return None
        decoded = json.loads(raw.decode("utf-8"))
        return decoded if isinstance(decoded, dict) else None
    except Exception:
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:  # pragma: no cover
                pass


def _parse_timestamp(value: Any, fallback: datetime) -> datetime:
    """Parse a runner timestamp (epoch seconds) into an aware UTC datetime."""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return fallback
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return fallback
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return fallback


#: Sentinel returned by ``_parse_line`` for an unusable line.
_PARSE_ERROR = object()


def capture_service_files(root: Path) -> list[Path]:
    """List the telemetry files a sandbox produced (used by artifact storage)."""
    telemetry_dir = Path(root) / "telemetry"
    if not telemetry_dir.exists():
        return []
    return sorted(telemetry_dir.glob("*.jsonl"))


def run_output_files(root: Path) -> Iterable[Path]:
    """List sandbox process logs, for the process-output artifact."""
    logs = Path(root) / "logs"
    if not logs.exists():
        return []
    return sorted(logs.glob("*.log"))


__all__ = [
    "CaptureResult",
    "CapturedSignal",
    "TelemetryCaptureService",
    "capture_service_files",
    "run_output_files",
]
