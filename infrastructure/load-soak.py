#!/usr/bin/env python3
"""ARGUS load and soak harness (hardening W6).

Answers one question with numbers instead of adjectives:

> What does this deployment actually do when real traffic arrives, and where
> does it stop being comfortable?

What it measures, per scenario:

* **throughput** — completed requests per second for the measured window;
* **latency percentiles** — p50/p95/p99 from every completed request, not from a
  sample of them;
* **error classes** — kept apart: a 4xx is the harness's own bug, a 5xx is the
  platform's, a timeout is the absence of an answer;
* **queue depth** — sampled from ``/metrics`` while the load runs, because the
  one thing a burst can do invisibly is pile up behind the API;
* **drain time** — for the ingestion scenarios, how long until the queue is
  empty again, which is the number an operator actually cares about.

It reports only what it measured, and labels every number with the concurrency
and the host it was measured on. There is no extrapolation in the output: this
is a *tested envelope*, not a capacity promise.

Usage:

    ARGUS_TOKEN=... python infrastructure/load-soak.py --scenario all
    python infrastructure/load-soak.py --scenario ingest --duration 60 --concurrency 16
    python infrastructure/load-soak.py --scenario read --project-id <uuid> --json

Stdlib only, so it runs wherever Python 3.12+ does — including a host that has
never installed ARGUS's dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_API = os.environ.get("ARGUS_API", "http://localhost:8000")
TOKEN = os.environ.get("ARGUS_TOKEN") or ""
TOKEN_CACHE = Path(
    os.environ.get("ARGUS_TOKEN_CACHE", "/tmp/argus-gate-token")
)


# ---------------------------------------------------------------------------
# HTTP plumbing (stdlib, so this runs on a bare host)
# ---------------------------------------------------------------------------
class HttpError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


def _request(
    method: str,
    url: str,
    payload: Optional[dict] = None,
    *,
    timeout: float = 30.0,
    token: Optional[str] = None,
) -> tuple[int, dict]:
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Content-Type", "application/json")
    credential = token if token is not None else TOKEN
    if credential:
        request.add_header("Authorization", f"Bearer {credential}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode() or "{}"
            return response.status, _loads(raw)
    except urllib.error.HTTPError as error:
        raw = error.read().decode(errors="replace")
        raise HttpError(error.code, raw) from error


def _loads(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw[:400]}
    return parsed if isinstance(parsed, dict) else {"_items": parsed}


def _resolve_token(explicit: Optional[str]) -> str:
    """$ARGUS_TOKEN, the gate cache, or nothing (a stack with auth disabled)."""
    if explicit:
        return explicit
    if TOKEN:
        return TOKEN
    try:
        return TOKEN_CACHE.read_text().strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------
@dataclass
class Sample:
    latency_ms: float
    status: int


@dataclass
class Result:
    scenario: str
    requests: int = 0
    latencies: list[float] = field(default_factory=list)
    by_class: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    wall_seconds: float = 0.0
    queue_depth_peak: Optional[int] = None
    queue_depth_samples: list[int] = field(default_factory=list)
    drain_seconds: Optional[float] = None

    def record(self, sample: Optional[Sample], error: Optional[Exception]) -> None:
        self.requests += 1
        if error is not None:
            if isinstance(error, HttpError):
                key = f"{error.status // 100}xx"
                self.by_class[key] = self.by_class.get(key, 0) + 1
                #: Keep the first few distinct messages: a load test that fails
                #: without saying why is a load test nobody trusts.
                if len(self.errors) < 5:
                    self.errors.append(f"HTTP {error.status}: {error.body[:160]}")
            elif isinstance(error, TimeoutError):
                self.by_class["timeout"] = self.by_class.get("timeout", 0) + 1
                if len(self.errors) < 5:
                    self.errors.append("timeout")
            else:
                self.by_class["transport"] = self.by_class.get("transport", 0) + 1
                if len(self.errors) < 5:
                    self.errors.append(f"{type(error).__name__}: {error}")
            return
        assert sample is not None
        key = f"{sample.status // 100}xx"
        self.by_class[key] = self.by_class.get(key, 0) + 1
        self.latencies.append(sample.latency_ms)

    def percentile(self, value: float) -> Optional[float]:
        if not self.latencies:
            return None
        ordered = sorted(self.latencies)
        #: Nearest-rank, stated rather than assumed: with N samples, p95 is the
        #: ceil(0.95 * N)-th value. No interpolation, so the number reported is
        #: always a latency that actually happened.
        index = min(len(ordered) - 1, max(0, int(value * len(ordered) + 0.9999) - 1))
        return ordered[index]

    def as_dict(self) -> dict[str, Any]:
        succeeded = self.by_class.get("2xx", 0) + self.by_class.get("3xx", 0)
        return {
            "scenario": self.scenario,
            "requests": self.requests,
            "succeeded": succeeded,
            "by_class": dict(sorted(self.by_class.items())),
            "throughput_rps": round(succeeded / self.wall_seconds, 2)
            if self.wall_seconds
            else None,
            "latency_ms": {
                "p50": _round(self.percentile(0.50)),
                "p95": _round(self.percentile(0.95)),
                "p99": _round(self.percentile(0.99)),
                "max": _round(max(self.latencies)) if self.latencies else None,
                "mean": _round(statistics.fmean(self.latencies))
                if self.latencies
                else None,
            },
            "queue_depth_peak": self.queue_depth_peak,
            "drain_seconds": _round(self.drain_seconds),
            "errors": self.errors,
        }


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 2)


# ---------------------------------------------------------------------------
# Queue observability during the run
# ---------------------------------------------------------------------------
def read_queue_depth(api: str, token: str) -> Optional[int]:
    """Sum of ``argus_ingestion_queue_depth`` across queues, or ``None``.

    Depth is the signal a latency number cannot show: requests can stay fast
    while the backlog grows, and the backlog is what turns a burst into an
    incident an hour later. ``None`` (not ``0``) when the series is absent — an
    unreachable broker must not read as an empty queue.
    """
    try:
        request = urllib.request.Request(f"{api}/metrics")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=5) as response:
            text = response.read().decode()
    except Exception:  # noqa: BLE001
        return None
    total = 0
    found = False
    for line in text.splitlines():
        if line.startswith("argus_ingestion_queue_depth{"):
            found = True
            try:
                total += int(float(line.rsplit(" ", 1)[1]))
            except (IndexError, ValueError):
                continue
    return total if found else None


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
@dataclass
class Target:
    project_id: str
    environment_id: Optional[str]
    component_id: Optional[str]


def ensure_target(api: str, token: str, explicit_project: Optional[str]) -> Target:
    """Use the supplied project, or build a throwaway one to load against."""
    if explicit_project:
        environment_id = None
        component_id = None
        try:
            _, environments = _request(
                "GET",
                f"{api}/api/v1/projects/{explicit_project}/environments",
                token=token,
            )
            items = environments.get("items") or []
            if items:
                environment_id = items[0]["id"]
        except Exception:  # noqa: BLE001
            pass
        try:
            _, components = _request(
                "GET",
                f"{api}/api/v1/projects/{explicit_project}/components",
                token=token,
            )
            items = components.get("items") or []
            if items:
                component_id = items[0]["id"]
        except Exception:  # noqa: BLE001
            pass
        return Target(explicit_project, environment_id, component_id)

    stamp = int(time.time())
    _, project = _request(
        "POST",
        f"{api}/api/v1/projects",
        {"name": f"Load soak {stamp}", "slug": f"load-soak-{stamp}"},
        token=token,
    )
    project_id = project["id"]
    _, environment = _request(
        "POST",
        f"{api}/api/v1/projects/{project_id}/environments",
        {"name": "production", "environment_type": "PRODUCTION"},
        token=token,
    )
    environment_id = environment["id"]
    _, component = _request(
        "POST",
        f"{api}/api/v1/projects/{project_id}/components",
        {
            "name": "load-target",
            "component_type": "APPLICATION",
            "environment_id": environment_id,
        },
        token=token,
    )
    print(f"  load target: project={project_id} environment={environment_id}")
    return Target(project_id, environment_id, component["id"])


def make_ingest_call(api: str, target: Target, *, sync: bool) -> Callable[[], int]:
    """One telemetry write: the sync pipeline, or the async queue path."""
    counter = {"n": 0}

    def call() -> int:
        counter["n"] += 1
        index = counter["n"]
        if sync:
            payload = {
                "project_id": target.project_id,
                "environment_id": target.environment_id,
                "events": [
                    {
                        "source_type": "load-soak",
                        "source_name": "load-soak",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event_type": "SYSTEM_EVENT",
                        "payload": {"sequence": index, "route": "/checkout"},
                        "metadata": {"harness": "load-soak"},
                    }
                ],
            }
            status, _ = _request("POST", f"{api}/api/v1/ingestion/bulk", payload)
            return status
        payload = {
            "project_id": target.project_id,
            "environment_id": target.environment_id,
            "events": [
                {
                    "source_type": "load-soak",
                    "source_name": "load-soak",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "event_type": "SYSTEM_EVENT",
                    "payload": {"sequence": index},
                }
            ],
        }
        status, _ = _request("POST", f"{api}/api/v1/ingestion/queue", payload)
        return status

    return call


def make_read_call(api: str, target: Target) -> Callable[[], int]:
    """The dashboard's read mix — what an operator's open tab generates."""
    endpoints = (
        f"/api/v1/incidents?project_id={target.project_id}&page_size=20",
        f"/api/v1/anomalies?project_id={target.project_id}&page_size=20",
        f"/api/v1/observability/events?project_id={target.project_id}&page_size=20",
        f"/api/v1/projects/{target.project_id}/graph/nodes?page_size=50",
        f"/api/v1/projects/{target.project_id}/reliability-metrics",
    )
    counter = {"n": 0}

    def call() -> int:
        counter["n"] += 1
        url = endpoints[counter["n"] % len(endpoints)]
        status, _ = _request("GET", f"{api}{url}")
        return status

    return call


def make_detect_call(api: str, target: Target) -> Callable[[], int]:
    """Detection + correlation for one project scope.

    The heaviest write path in the platform, and serialized by the per-project
    lock by design — which makes it the right thing to measure: this is where a
    busy project's ceiling lives.
    """

    def call() -> int:
        status, _ = _request(
            "POST",
            f"{api}/api/v1/projects/{target.project_id}/anomalies/detect",
            {"correlate": True},
        )
        return status

    return call


def make_metric_call(api: str, target: Target) -> Callable[[], int]:
    """A metric sample, the cheapest useful write."""
    counter = {"n": 0}

    def call() -> int:
        counter["n"] += 1
        status, _ = _request(
            "POST",
            f"{api}/api/v1/observability/metrics",
            {
                "project_id": target.project_id,
                "environment_id": target.environment_id,
                "component_id": target.component_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metric_name": "load.soak.latency.p95",
                "metric_type": "GAUGE",
                "value": 40 + (counter["n"] % 25),
                "unit": "ms",
            },
        )
        return status

    return call


SCENARIOS: dict[str, str] = {
    "ingest": "async queue write (enqueue only — measures the API edge)",
    "ingest-sync": "synchronous ingestion (measures the whole pipeline)",
    "metric": "single metric-sample write (the cheapest useful write)",
    "read": "dashboard read mix",
    "detect": "detection + correlation pass (the heaviest write)",
    "mixed": "4 ingest : 2 read : 1 metric",
}


def build_calls(api: str, target: Target, scenario: str) -> list[Callable[[], int]]:
    if scenario == "ingest":
        return [make_ingest_call(api, target, sync=False)]
    if scenario == "ingest-sync":
        return [make_ingest_call(api, target, sync=True)]
    if scenario == "metric":
        return [make_metric_call(api, target)]
    if scenario == "read":
        return [make_read_call(api, target)]
    if scenario == "detect":
        return [make_detect_call(api, target)]
    if scenario == "mixed":
        return [
            make_ingest_call(api, target, sync=False),
            make_ingest_call(api, target, sync=False),
            make_ingest_call(api, target, sync=False),
            make_ingest_call(api, target, sync=False),
            make_read_call(api, target),
            make_read_call(api, target),
            make_metric_call(api, target),
        ]
    raise SystemExit(f"unknown scenario: {scenario}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_scenario(
    *,
    api: str,
    token: str,
    target: Target,
    scenario: str,
    duration: float,
    concurrency: int,
    timeout: float,
    watch_queue: bool,
) -> Result:
    result = Result(scenario=scenario)
    calls = build_calls(api, target, scenario)
    counter = {"n": 0}
    lock = threading.Lock()
    deadline = time.monotonic() + duration

    def worker() -> None:
        while time.monotonic() < deadline:
            with lock:
                index = counter["n"]
                counter["n"] += 1
            call = calls[index % len(calls)]
            started = time.perf_counter()
            try:
                status = call()
                elapsed = (time.perf_counter() - started) * 1000
                with lock:
                    result.record(Sample(elapsed, status), None)
            except urllib.error.URLError as error:
                reason = getattr(error, "reason", error)
                with lock:
                    result.record(
                        None,
                        reason if isinstance(reason, TimeoutError) else error,
                    )
            except HttpError as error:
                with lock:
                    result.record(None, error)
            except Exception as error:  # noqa: BLE001
                with lock:
                    result.record(None, error)

    watcher = None
    started_at = time.monotonic()
    if watch_queue:
        watcher = threading.Thread(
            target=_watch_queue,
            args=(api, token, result, lambda: time.monotonic() < deadline),
            daemon=True,
        )
        watcher.start()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(worker) for _ in range(concurrency)]
        for future in futures:
            future.result()

    result.wall_seconds = max(0.001, time.monotonic() - started_at)

    if watcher is not None:
        watcher.join(timeout=5)
    if result.queue_depth_samples:
        result.queue_depth_peak = max(result.queue_depth_samples)

    # Drain: how long until the backlog the burst created is gone.
    if scenario in {"ingest", "ingest-sync", "mixed"} and watch_queue:
        result.drain_seconds = _wait_for_drain(api, token, timeout=180.0)

    return result


def _watch_queue(
    api: str,
    token: str,
    result: Result,
    still_running: Callable[[], bool],
) -> None:
    while still_running():
        depth = read_queue_depth(api, token)
        if depth is not None:
            result.queue_depth_samples.append(depth)
        time.sleep(2.0)


def _wait_for_drain(api: str, token: str, *, timeout: float) -> Optional[float]:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        depth = read_queue_depth(api, token)
        if depth is None:
            return None  # cannot observe the queue; do not claim a drain time
        if depth == 0:
            return time.monotonic() - started
        time.sleep(1.0)
    return None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def report(results: list[Result], *, api: str, concurrency: int, duration: float) -> None:
    print()
    print("=" * 78)
    print("ARGUS load-soak — tested envelope")
    print("=" * 78)
    print(f"  host        : {platform.platform()}")
    print(f"  cpu / python: {os.cpu_count()} cores / {platform.python_version()}")
    print(f"  api         : {api}")
    print(f"  window      : {duration:.0f}s per scenario at concurrency {concurrency}")
    print()
    header = f"{'scenario':<14}{'reqs':>8}{'rps':>9}{'p50':>9}{'p95':>9}{'p99':>9}{'err%':>8}"
    print(header)
    print("-" * len(header))
    for result in results:
        data = result.as_dict()
        errors = sum(
            count for key, count in data["by_class"].items() if key not in ("2xx", "3xx")
        )
        error_rate = (errors / data["requests"] * 100) if data["requests"] else 0.0
        print(
            f"{data['scenario']:<14}"
            f"{data['requests']:>8}"
            f"{_fmt(data['throughput_rps']):>9}"
            f"{_fmt(data['latency_ms']['p50']):>9}"
            f"{_fmt(data['latency_ms']['p95']):>9}"
            f"{_fmt(data['latency_ms']['p99']):>9}"
            f"{error_rate:>7.1f}%"
        )
    print()
    for result in results:
        data = result.as_dict()
        detail = [f"  {data['scenario']}:"]
        detail.append(f"classes={data['by_class']}")
        if data["queue_depth_peak"] is not None:
            detail.append(f"queue_peak={data['queue_depth_peak']}")
        if data["drain_seconds"] is not None:
            detail.append(f"drain={data['drain_seconds']}s")
        print("  ".join(detail))
        for error in data["errors"]:
            print(f"      - {error}")
    print()
    print("  Read this as: measured on the host above, at this concurrency, against")
    print("  a single-container stack with the demo dataset. Latency percentiles are")
    print("  nearest-rank over every completed request in the window; no values are")
    print("  extrapolated to other hardware, other data volumes, or higher load.")
    print("=" * 78)


def _fmt(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument(
        "--scenario",
        default="mixed",
        choices=[*SCENARIOS, "all"],
        help="workload to run ('all' runs every scenario except 'detect')",
    )
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--no-queue-watch",
        action="store_true",
        help="skip /metrics sampling (for a stack where /metrics is not reachable)",
    )
    args = parser.parse_args(argv)

    token = _resolve_token(args.token)
    if not token:
        print(
            "warn: no ARGUS_TOKEN resolved — assuming this stack runs with "
            "authentication disabled",
            file=sys.stderr,
        )

    print(f"ARGUS load-soak against {args.api}")
    print(f"  scenario   : {args.scenario}")
    print(f"  concurrency: {args.concurrency}   duration: {args.duration:.0f}s")
    target = ensure_target(args.api, token, args.project_id)

    scenarios = (
        [name for name in SCENARIOS if name != "detect"]
        if args.scenario == "all"
        else [args.scenario]
    )

    try:
        results: list[Result] = []
        for name in scenarios:
            print(f"  > {name}: {SCENARIOS[name]}")
            results.append(
                run_scenario(
                    api=args.api,
                    token=token,
                    target=target,
                    scenario=name,
                    duration=args.duration,
                    concurrency=args.concurrency,
                    timeout=args.timeout,
                    watch_queue=not args.no_queue_watch,
                )
            )
    except KeyboardInterrupt:  # pragma: no cover - operator interrupt
        print("\ninterrupted", file=sys.stderr)
        return 130

    if args.json:
        print(
            json.dumps(
                {
                    "api": args.api,
                    "concurrency": args.concurrency,
                    "duration_seconds": args.duration,
                    "host": {
                        "platform": platform.platform(),
                        "cpu_count": os.cpu_count(),
                        "python": platform.python_version(),
                    },
                    "project_id": target.project_id,
                    "results": [result.as_dict() for result in results],
                },
                indent=2,
            )
        )
    else:
        report(
            results,
            api=args.api,
            concurrency=args.concurrency,
            duration=args.duration,
        )

    failed = [
        result
        for result in results
        if result.by_class.get("5xx") or result.by_class.get("timeout")
    ]
    if failed:
        print(
            "FAIL: the platform returned 5xx or timed out under load "
            f"({', '.join(r.scenario for r in failed)})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
