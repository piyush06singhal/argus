#!/usr/bin/env python3
"""ARGUS Demo Commerce — sandboxed service runner (Phase 5 §58).

This is the **system under test**. ARGUS provisions one process per service and
replays requests against the entry service; the failure that ARGUS captures is
produced by these processes actually calling each other over loopback HTTP,
actually timing out, and actually emitting the telemetry that is later compared
against the original incident.

Why a generic runner instead of three hand-written services: the three services
differ only in their role's call shape (``checkout`` POSTs, ``inventory`` GETs,
``datastore`` answers queries), so one runner driven by a topology file keeps the
call semantics identical across roles and makes the fault-injection surface a
single auditable place instead of three.

Design constraints (all of them are load-bearing):

* **Standard library only.** The sandbox is isolated from ARGUS's environment,
  so this file imports nothing outside the Python standard library and never
  imports ``app.*``.
* **Loopback only.** It binds ``127.0.0.1`` on an ephemeral port and writes the
  chosen port to a port file; it can never be reached off the host.
* **Real timeouts.** Downstream calls use a socket timeout, so an injected
  latency genuinely produces a client-side timeout rather than a fake error.
* **Telemetry is evidence.** Spans carry parent/child ids, so the captured
  reproduction has a real call topology to compare against the incident, and any
  injected fault is labelled on the span that experienced it — which is how
  ARGUS distinguishes "reproduced naturally" from "we forced it".

Usage::

    python service_runner.py --service inventory --runtime runtime.json \
        --workdir /tmp/argus-repro-xyz --ready-timeout 20

Writes:
    <workdir>/ports/<service>.port   bound port (once listening)
    <workdir>/telemetry/<service>.jsonl  spans/logs/metrics/health
    <workdir>/state/<service>.json   exit state, for post-mortem
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import random
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

VERSION = "1.0.0"

#: Hard ceiling on telemetry bytes per service. The sandbox has a telemetry
#: size limit (§40); exceeding it truncates with an explicit marker rather than
#: silently growing without bound.
DEFAULT_TELEMETRY_LIMIT = 4_000_000

#: Faults whose ``intensity`` is a *per-request application probability* rather
#: than a magnitude. Without this, a failure cannot be injected intermittently:
#: every request either fails or succeeds forever, which makes the difference
#: between "this system always fails" and "this system fails one time in two"
#: invisible to the experiment -- and that difference is the whole point of the
#: repeatability analysis (§34, §61).
PROBABILISTIC_FAULTS = frozenset(
    {
        "HTTP_4XX",
        "HTTP_5XX",
        "DEPENDENCY_UNAVAILABLE",
        "CONNECTION_FAILURE",
        "RESPONSE_CORRUPTION",
    }
)


# ---------------------------------------------------------------------------
# Telemetry sink
# ---------------------------------------------------------------------------
class TelemetryWriter:
    """Append-only JSONL telemetry, safe for the handler's thread pool."""

    def __init__(self, path: str, limit_bytes: int = DEFAULT_TELEMETRY_LIMIT) -> None:
        self._path = path
        self._limit = limit_bytes
        self._lock = threading.Lock()
        self._written = 0
        self._truncated = False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._handle = open(path, "a", encoding="utf-8")

    def emit(self, event: dict[str, Any]) -> None:
        event.setdefault("emitted_at", time.time())
        line = json.dumps(event, separators=(",", ":"), default=str)
        with self._lock:
            if self._truncated:
                return
            if self._written + len(line) + 1 > self._limit:
                self._truncated = True
                marker = json.dumps(
                    {
                        "type": "telemetry_truncated",
                        "reason": f"telemetry limit of {self._limit} bytes reached",
                        "bytes_written": self._written,
                    }
                )
                self._handle.write(marker + "\n")
                self._handle.flush()
                return
            self._handle.write(line + "\n")
            self._written += len(line) + 1
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            try:
                self._handle.flush()
                self._handle.close()
            except Exception:  # pragma: no cover - best effort on shutdown
                pass


# ---------------------------------------------------------------------------
# Fault injection (read from the sandbox's faults.json)
# ---------------------------------------------------------------------------
class FaultState:
    """Active faults for this service, re-read on every request.

    Re-reading a small JSON file per request is what lets ARGUS activate and
    clear faults *during* a run without an IPC channel into the sandbox: the
    orchestrator writes ``faults.json`` and the very next request observes it.
    That keeps the control interface one-directional and trivially auditable.
    """

    def __init__(self, path: str, service: str) -> None:
        self._path = path
        self._service = service
        self._mtime: Optional[float] = None
        self._active: list[dict[str, Any]] = []

    def reload(self) -> list[dict[str, Any]]:
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            self._active = []
            self._mtime = None
            return []
        if self._mtime == mtime:
            return self._active
        self._mtime = mtime
        try:
            with open(self._path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return self._active
        now = time.time()
        active: list[dict[str, Any]] = []
        for entry in payload.get("active", []):
            if entry.get("target") not in (self._service, "*"):
                continue
            started = float(entry.get("activated_at") or 0)
            duration = entry.get("duration_ms")
            if duration is not None and started and now > started + (duration / 1000.0):
                continue
            active.append(entry)
        self._active = active
        return active

    def current(self) -> Optional[dict[str, Any]]:
        """The single most severe active fault, if any.

        One fault per request keeps the observed behaviour attributable: if two
        faults applied to the same request, the captured telemetry could not say
        which one produced the failure.
        """
        active = self.reload()
        if not active:
            return None
        order = {
            "CONNECTION_FAILURE": 0,
            "DEPENDENCY_UNAVAILABLE": 1,
            "HTTP_5XX": 2,
            "TIMEOUT": 3,
            "RESOURCE_PRESSURE": 4,
            "RESPONSE_CORRUPTION": 5,
            "HTTP_4XX": 6,
            "LATENCY": 7,
        }
        return sorted(active, key=lambda f: order.get(f.get("fault_type", ""), 99))[0]


# ---------------------------------------------------------------------------
# Service behaviour
# ---------------------------------------------------------------------------
class Service:
    """One demo-commerce service: a role, a dependency, and real HTTP calls."""

    def __init__(self, config: dict[str, Any], workdir: str, service: str) -> None:
        self.name = service
        self.role = config.get("role", "service")
        self.app_version = config.get("app_version", "1.0.0")
        self.base_latency_ms = float(config.get("base_latency_ms", 10))
        self.timeout_ms = float(config.get("timeout_ms", 800))
        self.error_rate = float(config.get("error_rate", 0.0))
        self.pool_size = int(config.get("pool_size", 10))
        self.dependency = config.get("dependency")
        self.dependency_port = None
        self.workdir = workdir
        self._inflight = 0
        self._requests_total = 0
        self._lock = threading.Lock()
        self.telemetry = TelemetryWriter(
            os.path.join(workdir, "telemetry", f"{service}.jsonl"),
            limit_bytes=int(
                os.environ.get("ARGUS_TELEMETRY_LIMIT", DEFAULT_TELEMETRY_LIMIT)
            ),
        )
        self.faults = FaultState(os.path.join(workdir, "faults.json"), service)
        #: Deterministic jitter per service+request index keeps repetitions
        #: comparable while avoiding pathological alignment of latency samples.
        self._jitter = random.Random(f"{service}:{VERSION}")
        #: A *separate*, genuinely random stream for everything probabilistic:
        #: injected fault application and the configured natural error rate.
        #: Sharing the deterministic stream would make a "flaky dependency"
        #: behave identically in every repetition, so an intermittent failure
        #: could never be observed. Setting ``ARGUS_SANDBOX_SEED`` restores
        #: byte-for-byte repeatability when an experiment needs it.
        noise_seed = os.environ.get("ARGUS_SANDBOX_SEED")
        self._noise = random.Random(
            f"{service}:{VERSION}:{noise_seed}"
            if noise_seed
            else f"{service}:{VERSION}:{os.urandom(16).hex()}"
        )

    # -- ports -----------------------------------------------------------
    def resolve_dependency_port(self, runtime: dict[str, Any]) -> None:
        if not self.dependency:
            return
        dep = runtime["services"].get(self.dependency)
        if dep is None:
            raise SystemExit(f"dependency {self.dependency!r} missing from runtime")
        self.dependency_port = dep["port"]

    # -- telemetry helpers ------------------------------------------------
    def emit_span(
        self,
        *,
        trace_id: str,
        span_id: str,
        parent_span_id: Optional[str],
        operation: str,
        duration_ms: float,
        status: str,
        error: Optional[str] = None,
        fault: Optional[str] = None,
        attributes: Optional[dict[str, Any]] = None,
        component: Optional[str] = None,
    ) -> None:
        """Record one span.

        ``component`` defaults to this service, and is overridden exactly once:
        for the *outbound* span of a call, which belongs to the component being
        called. Production telemetry attributes a database query span to the
        database (that is how the original incident records its timed-out
        ``SELECT stock_levels``), and a sandbox whose spans never cross a service
        boundary would make ARGUS's trace-topology comparison measure an
        instrumentation difference rather than a behavioural one.
        """
        payload: dict[str, Any] = {
            "type": "span",
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "service": self.name,
            "component": component or self.name,
            "operation": operation,
            "app_version": self.app_version,
            "duration_ms": round(duration_ms, 3),
            "status": status,
        }
        if error:
            payload["error"] = error
        if fault:
            payload["fault"] = fault
            payload.setdefault("attributes", {})["injected_fault"] = fault
        if attributes:
            payload.setdefault("attributes", {}).update(attributes)
        self.telemetry.emit(payload)

    def emit_log(self, level: str, message: str, **extra: Any) -> None:
        self.telemetry.emit(
            {"type": "log", "service": self.name, "level": level, "message": message, **extra}
        )

    def emit_metric(self, name: str, value: float, unit: str = "ms") -> None:
        self.telemetry.emit(
            {
                "type": "metric",
                "service": self.name,
                "name": name,
                "value": value,
                "unit": unit,
            }
        )

    def emit_health(self, state: str) -> None:
        self.telemetry.emit({"type": "health", "service": self.name, "state": state})

    # -- fault application -------------------------------------------------
    def apply_fault(self) -> Optional[dict[str, Any]]:
        """Return the active fault for this request (already delayed if LATENCY)."""
        fault = self.faults.current()
        if fault is None:
            return None
        fault_type = fault.get("fault_type")
        intensity = float(fault.get("intensity") or 1.0)
        parameters = fault.get("parameters") or {}
        # A probabilistic fault applies to *this* request only with probability
        # ``intensity``; when it does not apply the request is served normally and
        # the telemetry says so, which is what makes an intermittent reproduction
        # measurable instead of assumed.
        if fault_type in PROBABILISTIC_FAULTS and intensity < 1.0:
            if self._noise.random() >= intensity:
                self.emit_metric("fault.not_applied", 1.0, "count")
                return None
        # Two different knobs, deliberately separate: ``parameters.latency_ms``
        # is the *magnitude* the fault imposes on a request, while
        # ``duration_ms`` is the *activation window* (handled in FaultState).
        # Conflating them would make a fault's own magnitude determine when it
        # switches off.
        magnitude_ms = parameters.get("latency_ms")
        if fault_type == "LATENCY":
            delay_ms = float(
                magnitude_ms
                if magnitude_ms is not None
                else self.base_latency_ms * (1 + intensity * 10)
            )
            time.sleep(max(delay_ms, 0) / 1000.0)
        elif fault_type == "TIMEOUT":
            # Sleep past the caller's timeout so the *caller* observes a
            # timeout — which is the behaviour the incident shows, rather than
            # a service that politely returns a slow error.
            time.sleep(max(float(magnitude_ms or 0), 5000) / 1000.0)
        elif fault_type == "RESOURCE_PRESSURE":
            time.sleep(min(float(magnitude_ms or 250), 2000) / 1000.0)
        return fault

    # -- downstream call ---------------------------------------------------
    def call_dependency(
        self,
        *,
        trace_id: str,
        parent_span_id: str,
        path: str,
        method: str = "GET",
        body: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[dict[str, Any]], Optional[str], float]:
        """Call the dependency over loopback HTTP with a real timeout.

        Returns ``(payload, error, duration_ms)``. Every failure mode the
        incident classifies (timeout, connection failure, HTTP error, corrupt
        body) arrives here as a distinct, real observation.

        Each call also emits one span attributed to the *dependency* — the
        caller's view of the call, the same shape the incident's own telemetry
        has for its ``SELECT stock_levels`` span. It carries
        ``client_observed: true`` so the attribution is never mistaken for the
        dependency having reported on itself.
        """
        if not self.dependency or not self.dependency_port:
            return None, "no dependency configured", 0.0
        started = time.time()
        connection = None
        operation = f"{self.dependency}{(urlparse(path).path or '/')}"

        def finish(
            payload: Optional[dict[str, Any]],
            error: Optional[str],
            duration_ms: float,
        ) -> tuple[Optional[dict[str, Any]], Optional[str], float]:
            self.emit_span(
                trace_id=trace_id,
                span_id=uuid.uuid4().hex[:16],
                parent_span_id=parent_span_id,
                operation=operation,
                duration_ms=duration_ms,
                status="ERROR" if error else "OK",
                error=error,
                component=self.dependency,
                attributes={"client_observed": True, "call_path": path},
            )
            return payload, error, duration_ms

        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", self.dependency_port, timeout=self.timeout_ms / 1000.0
            )
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            payload_body = json.dumps(body) if body is not None else None
            connection.request(method, path, body=payload_body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            duration_ms = (time.time() - started) * 1000.0
            self.emit_metric(
                f"dependency.{self.dependency}.duration_ms", duration_ms, "ms"
            )
            if response.status >= 400:
                return finish(None, f"HTTP {response.status}", duration_ms)
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # RESPONSE_CORRUPTION surfaces exactly here.
                return finish(None, "corrupt response body", duration_ms)
            return finish(decoded, None, duration_ms)
        except socket.timeout:
            duration_ms = (time.time() - started) * 1000.0
            self.emit_metric(
                f"dependency.{self.dependency}.duration_ms", duration_ms, "ms"
            )
            return finish(None, "timeout", duration_ms)
        except (ConnectionError, OSError) as exc:
            duration_ms = (time.time() - started) * 1000.0
            return finish(None, f"connection failure: {type(exc).__name__}", duration_ms)
        except http.client.HTTPException as exc:
            duration_ms = (time.time() - started) * 1000.0
            return finish(None, f"protocol error: {type(exc).__name__}", duration_ms)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # pragma: no cover
                    pass

    # -- request handling --------------------------------------------------
    def handle(self, path: str, method: str, body: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, Any]]:
        """Dispatch one request; returns ``(status, body, span_info)``."""
        parsed = urlparse(path)
        trace_id = uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]
        started = time.time()
        query = parse_qs(parsed.query)

        with self._lock:
            self._inflight += 1
            inflight = self._inflight
            self._requests_total += 1
        try:
            status, payload, error, attributes = self._route(
                parsed.path, method, body, query, trace_id, span_id, inflight
            )
            duration_ms = (time.time() - started) * 1000.0
            fault = attributes.pop("_fault", None)
            # Harness control paths are deliberately silent: a probe is not
            # application traffic, and recording it would put harness noise into
            # the telemetry ARGUS compares against the incident.
            # Transport-level outcomes live in ``attributes`` during routing
            # (they are not part of the service's own return value) but the HTTP
            # layer needs them, so they are lifted back onto the payload here.
            if attributes.pop("_drop_connection", None):
                payload = {**payload, "_drop_connection": True}
            if attributes.pop("_corrupt", None):
                payload = {**payload, "_corrupt": True}
            if attributes.pop("_silent", False):
                return status, payload, {"trace_id": trace_id, "span_id": span_id}
            self.emit_span(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=None,
                operation=f"{self.name}.{parsed.path.strip('/').split('/')[0] or 'root'}",
                duration_ms=duration_ms,
                status="ERROR" if error else "OK",
                error=error,
                fault=fault,
                attributes=attributes or None,
            )
            self.emit_metric("http.request.duration_ms", duration_ms, "ms")
            if error:
                self.emit_log(
                    "ERROR", f"{self.name} failed: {error}", trace_id=trace_id
                )
            self.emit_health("DEGRADED" if error else "UP")
        finally:
            # The in-flight counter is released only after this request's
            # telemetry is on disk, which is what makes ``inflight == 0`` a
            # trustworthy "nothing more is coming" signal for the collector.
            with self._lock:
                self._inflight -= 1
        return status, payload, {"trace_id": trace_id, "span_id": span_id}

    def _route(
        self,
        path: str,
        method: str,
        body: dict[str, Any],
        query: dict[str, list[str]],
        trace_id: str,
        span_id: str,
        inflight: int,
    ) -> tuple[int, dict[str, Any], Optional[str], dict[str, Any]]:
        attributes: dict[str, Any] = {}

        if path == "/health":
            attributes["_fault"] = None
            attributes["_silent"] = True
            return 200, {"status": "UP", "service": self.name}, None, attributes

        if path == "/__meta__":
            attributes["_fault"] = None
            attributes["_silent"] = True
            return (
                200,
                {
                    "service": self.name,
                    "role": self.role,
                    "app_version": self.app_version,
                    "dependency": self.dependency,
                    "pid": os.getpid(),
                },
                None,
                attributes,
            )

        if path == "/__status__":
            # Quiescence probe: ARGUS waits for ``inflight == 0`` on every
            # service before capturing telemetry, so a still-running faulted
            # request cannot be truncated out of the evidence. The probe itself
            # is excluded from the count — otherwise the act of asking "are you
            # busy?" would always answer "yes".
            with self._lock:
                outstanding = max(0, self._inflight - 1)
                total = self._requests_total
            attributes["_fault"] = None
            attributes["_silent"] = True
            return (
                200,
                {
                    "service": self.name,
                    "status": "IDLE" if outstanding == 0 else "BUSY",
                    "inflight": outstanding,
                    "requests_total": total,
                    "pid": os.getpid(),
                },
                None,
                attributes,
            )

        # An injected fault on *this* service changes its own behaviour.
        fault = self.apply_fault()
        if fault is not None:
            attributes["_fault"] = fault.get("fault_type")
            fault_type = fault.get("fault_type")
            if fault_type == "CONNECTION_FAILURE":
                # Handled by the caller: the handler drops the connection.
                attributes["_drop_connection"] = True
                return 0, {}, "connection refused", attributes
            if fault_type == "HTTP_4XX":
                return 400, {"error": "bad request (injected)"}, "HTTP 400", attributes
            if fault_type in {"HTTP_5XX", "DEPENDENCY_UNAVAILABLE"}:
                return (
                    503,
                    {"error": f"{self.dependency or self.name} unavailable (injected)"},
                    "HTTP 503",
                    attributes,
                )
            if fault_type == "RESOURCE_PRESSURE":
                if inflight > self.pool_size:
                    return (
                        503,
                        {"error": "connection pool exhausted (injected)"},
                        "pool exhausted",
                        attributes,
                    )
                return (
                    200,
                    {"warning": "connection pool under pressure (injected)"},
                    None,
                    attributes,
                )
            if fault_type == "RESPONSE_CORRUPTION":
                attributes["_corrupt"] = True
                return 200, {"rows": []}, None, attributes

        try:
            if self.role == "datastore":
                table = (query.get("table") or ["inventory"])[0]
                payload = self._datastore_query(table, trace_id, span_id, attributes)
            elif self.name == "inventory":
                payload = self._inventory_get(path, trace_id, span_id, attributes)
            else:
                payload = self._checkout_post(method, body, trace_id, span_id, attributes)
        except _ServiceFailure as failure:
            return failure.status, {"error": failure.message}, failure.message, attributes

        return 200, payload, None, attributes

    # -- role implementations ---------------------------------------------
    def _datastore_query(
        self, table: str, trace_id: str, span_id: str, attributes: dict[str, Any]
    ) -> dict[str, Any]:
        latency = self.base_latency_ms + self._jitter.uniform(0, 3)
        time.sleep(latency / 1000.0)
        self.emit_metric(f"datastore.query.latency_ms", latency, "ms")
        attributes["table"] = table
        return {
            "table": table,
            "rows": [{"sku": "SKU-1", "available": 25}, {"sku": "SKU-2", "available": 4}],
            "latency_ms": round(latency, 3),
        }

    def _inventory_get(
        self, path: str, trace_id: str, span_id: str, attributes: dict[str, Any]
    ) -> dict[str, Any]:
        parts = [p for p in path.split("/") if p]
        sku = parts[1] if len(parts) > 1 else "SKU-1"
        attributes["sku"] = sku
        child_span = uuid.uuid4().hex[:16]
        payload, error, duration_ms = self.call_dependency(
            trace_id=trace_id,
            parent_span_id=span_id,
            path="/query?table=inventory",
        )
        status = "ERROR" if error else "OK"
        self.emit_span(
            trace_id=trace_id,
            span_id=child_span,
            parent_span_id=span_id,
            operation="datastore.query",
            duration_ms=duration_ms,
            status=status,
            error=error,
            attributes={"table": "inventory"},
        )
        if error == "timeout":
            raise _ServiceFailure(504, f"inventory datastore timeout after {int(duration_ms)}ms")
        if error:
            raise _ServiceFailure(503, f"inventory datastore unavailable: {error}")
        assert payload is not None
        available = next(
            (row["available"] for row in payload.get("rows", []) if row.get("sku") == sku),
            0,
        )
        if self.error_rate and self._noise.random() < self.error_rate:
            raise _ServiceFailure(500, "inventory internal error")
        return {"sku": sku, "available": available, "source": "datastore"}

    def _checkout_post(
        self,
        method: str,
        body: dict[str, Any],
        trace_id: str,
        span_id: str,
        attributes: dict[str, Any],
    ) -> dict[str, Any]:
        sku = str(body.get("sku") or "SKU-1")
        quantity = int(body.get("quantity") or 1)
        attributes["sku"] = sku
        child_span = uuid.uuid4().hex[:16]
        payload, error, duration_ms = self.call_dependency(
            trace_id=trace_id,
            parent_span_id=span_id,
            path=f"/inventory/{sku}",
        )
        self.emit_span(
            trace_id=trace_id,
            span_id=child_span,
            parent_span_id=span_id,
            operation="inventory.get",
            duration_ms=duration_ms,
            status="ERROR" if error else "OK",
            error=error,
            attributes={"sku": sku},
        )
        if error == "timeout":
            raise _ServiceFailure(504, f"inventory timeout after {int(duration_ms)}ms")
        if error:
            raise _ServiceFailure(503, f"inventory unavailable: {error}")
        assert payload is not None
        if int(payload.get("available", 0)) < quantity:
            raise _ServiceFailure(409, "insufficient stock")
        return {
            "order_id": uuid.uuid4().hex[:12],
            "sku": sku,
            "quantity": quantity,
            "status": "CONFIRMED",
        }


class _ServiceFailure(Exception):
    """A request-level failure that becomes an HTTP error response."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
def make_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"argus-demo/{service.name}/{VERSION}"

        def log_message(self, *args: Any) -> None:  # silence stderr access logs
            return

        def _read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}
            return decoded if isinstance(decoded, dict) else {"value": decoded}

        def _respond(self, status: int, body: dict[str, Any], corrupt: bool = False) -> None:
            if corrupt:
                payload = b'{"rows": [{"sku": "SKU-1", "avail'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _dispatch(self, method: str) -> None:
            path = self.path or "/"
            body = self._read_body() if method in {"POST", "PUT", "PATCH"} else {}
            status, payload, _info = service.handle(path, method, body)
            if payload.get("_drop_connection"):
                # A CONNECTION_FAILURE fault: close without responding, so the
                # caller observes a real transport failure.
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return
            corrupt = bool(payload.pop("_corrupt", False))
            self._respond(status or 503, payload, corrupt=corrupt)

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch("HEAD")

    return Handler


def write_state(workdir: str, service: str, state: dict[str, Any]) -> None:
    path = os.path.join(workdir, "state", f"{service}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=1, default=str)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ARGUS demo commerce service runner")
    parser.add_argument("--service", required=True)
    parser.add_argument("--runtime", required=True, help="resolved topology JSON")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)

    with open(args.runtime, encoding="utf-8") as handle:
        runtime = json.load(handle)
    config = runtime["services"].get(args.service)
    if config is None:
        print(f"service {args.service!r} not in runtime topology", file=sys.stderr)
        return 2

    service = Service(config, args.workdir, args.service)
    service.resolve_dependency_port(runtime)

    ports_dir = os.path.join(args.workdir, "ports")
    os.makedirs(ports_dir, exist_ok=True)
    port_file = os.path.join(ports_dir, f"{args.service}.port")

    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(service))
    httpd.daemon_threads = True
    bound_port = httpd.server_address[1]

    # Announce readiness only once the socket is actually accepting, so ARGUS
    # never replays into a port that is not listening yet.
    with open(port_file, "w", encoding="utf-8") as handle:
        handle.write(str(bound_port))
    service.emit_health("UP")
    service.emit_log("INFO", f"{args.service} listening on {args.host}:{bound_port}")
    write_state(
        args.workdir,
        args.service,
        {
            "service": args.service,
            "pid": os.getpid(),
            "port": bound_port,
            "status": "running",
            "version": VERSION,
        },
    )

    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:  # pragma: no cover - sandbox shutdown path
        pass
    finally:
        httpd.server_close()
        write_state(
            args.workdir,
            args.service,
            {
                "service": args.service,
                "pid": os.getpid(),
                "port": bound_port,
                "status": "stopped",
                "version": VERSION,
            },
        )
        service.telemetry.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
