"""ARGUS Sandbox Manager (Phase 5 §10–§13, §40, §55).

Provisions the disposable, isolated environment an experiment runs in, and — the
part that matters most — guarantees it goes away afterwards.

Two backends, chosen by ``REPRO_SANDBOX_BACKEND``:

``LOCAL_PROCESS`` (default)
    One process per service, in its own process group, behind the per-process
    POSIX resource limits (CPU seconds, address space, file size), with a
    sanitized environment, a private working tree, and loopback-only sockets.
    It is the default because it is *always* available: an experiment that needs
    a Docker daemon to run cannot be relied on, and a safety feature that is
    unavailable by default is not a safety feature. The process-count ceiling is
    the one limit it cannot apply — see ``_rlimit_preexec`` — so it records that
    in the sandbox metadata (``process_cap_enforced``) instead of implying it.

``DOCKER``
    One container per service on a dedicated **internal** Docker network, with
    ``--cap-drop ALL``, ``--security-opt no-new-privileges``, a read-only root
    filesystem, no host network, and CPU/memory limits plus a real per-container
    PID ceiling (``--pids-limit``). Opt-in, and it refuses clearly when the
    daemon is unreachable rather than silently falling back (a silent fallback
    would mean the operator's isolation choice was quietly ignored).

Isolation rules enforced here, not merely documented:

* the working tree is a fresh directory created *inside* the configured root;
* the child environment is the sanitizer's allowlist — never the host's;
* sockets bind ``127.0.0.1`` only, so nothing is reachable off-host;
* every service has a startup deadline, and a service that never becomes ready
  fails provisioning instead of being replayed into;
* ``destroy()`` always runs, always records the attempt, and reports failure
  instead of swallowing it (§55).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.config import get_settings
from app.models.reproduction import (
    SandboxBackendKind,
    SandboxNetworkPolicy,
    SandboxStatus,
)
from app.services.reproduction_sanitizer import ConfigSanitizer

logger = logging.getLogger(__name__)
settings = get_settings()

#: POSIX resource limits are unavailable on Windows; the backend degrades to
#: "separate processes, no rlimits" and says so in the sandbox metadata rather
#: than pretending the limits exist.
try:  # pragma: no cover - platform dependent
    import resource as _resource
except ImportError:  # pragma: no cover
    _resource = None  # type: ignore[assignment]


class SandboxError(RuntimeError):
    """Sandbox provisioning, startup, or cleanup failed."""


def harness_root() -> Path:
    """Directory holding the reproduction harness (templates/runners/fixtures).

    It lives inside the API package because the sandbox is provisioned *by* the
    API and the harness must therefore be present in the API image; a harness at
    the repository root would simply not exist in a deployed container.
    """
    return Path(__file__).resolve().parent.parent.parent / "reproduction"


def sandbox_root_base() -> Path:
    """Parent directory for all sandbox working trees."""
    configured = settings.REPRO_SANDBOX_ROOT.strip()
    if configured:
        base = Path(configured).expanduser()
    else:
        base = Path(tempfile.gettempdir()) / "argus-reproduction"
    base.mkdir(parents=True, exist_ok=True)
    return base


def artifact_root_base() -> Path:
    """Parent directory for experiment artifacts (survives sandbox teardown)."""
    configured = settings.REPRO_ARTIFACT_ROOT.strip()
    if configured:
        base = Path(configured).expanduser()
    else:
        base = Path.cwd() / "var" / "reproduction"
    base.mkdir(parents=True, exist_ok=True)
    return base


#: The shipped topology used when a caller does not name one. It is named for
#: what it *is* (an HTTP service chain) rather than for the demo dataset it
#: happens to resemble, so the product default is not the demo default.
DEFAULT_TEMPLATE = "http_service_chain"

#: Templates that existed under an older, demo-flavoured name. They keep
#: working because an experiment stored with that name must remain replayable —
#: renaming a shipped default is not a reason to break history.
TEMPLATE_ALIASES: dict[str, str] = {"demo_commerce": "http_service_chain"}


def resolve_template_name(name: str) -> str:
    """Map a template name to its current on-disk name (aliases included)."""
    return TEMPLATE_ALIASES.get(name, name)


def load_template(name: str) -> dict[str, Any]:
    """Load a reproduction topology template by name."""
    if not name or "/" in name or ".." in name:
        raise SandboxError(f"Invalid sandbox template name: {name!r}")
    path = harness_root() / "templates" / f"{resolve_template_name(name)}.json"
    if not path.exists():
        raise SandboxError(f"Unknown sandbox template: {name!r}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_environment_descriptor(name: str) -> dict[str, Any]:
    """Load the declared environment shape used for snapshot/difference analysis."""
    path = harness_root() / "environments" / f"{resolve_template_name(name)}.json"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_fixture(name: str) -> dict[str, Any]:
    """Load a replay fixture by name."""
    path = harness_root() / "fixtures" / f"{name}.json"
    if not path.exists():
        raise SandboxError(f"Unknown reproduction fixture: {name!r}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def match_service(
    template: dict[str, Any], component_name: Optional[str]
) -> Optional[str]:
    """Map an ARGUS component name to a sandbox service name.

    Matching is on normalized aliases declared by the template, so the mapping
    is data, not a hard-coded guess about what a component is called. A
    component the template does not know about resolves to ``None`` and the
    planner must then say so rather than inventing a target.
    """
    if not component_name:
        return None
    normalized = " ".join(component_name.strip().lower().replace("_", " ").split())
    aliases: dict[str, list[str]] = template.get("aliases", {})
    # Exact alias match first, then longest alias contained in the name, so
    # "Inventory Database" matches "inventory database" ahead of "inventory".
    for service, service_aliases in aliases.items():
        for alias in service_aliases:
            if normalized == alias:
                return service
    best: Optional[tuple[int, str]] = None
    for service, service_aliases in aliases.items():
        for alias in service_aliases:
            if alias and alias in normalized:
                if best is None or len(alias) > best[0]:
                    best = (len(alias), service)
    return best[1] if best else None


@dataclass
class SandboxSpec:
    """Everything the manager needs to build a sandbox — resolved by the planner."""

    template: str
    network_policy: SandboxNetworkPolicy = SandboxNetworkPolicy.ISOLATED
    timeout_seconds: int = 300
    services: list[str] = field(default_factory=list)
    resource_limits: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def limits(self) -> dict[str, Any]:
        """Effective limits: settings defaults overlaid by explicit overrides."""
        merged = {
            "cpu_seconds": settings.REPRO_MAX_CPU_SECONDS,
            "memory_mb": settings.REPRO_MAX_MEMORY_MB,
            "disk_mb": settings.REPRO_MAX_DISK_MB,
            "processes": settings.REPRO_MAX_PROCESSES,
            "timeout_seconds": settings.REPRO_EXPERIMENT_TIMEOUT_SECONDS,
            "telemetry_bytes": settings.REPRO_MAX_TELEMETRY_BYTES,
        }
        merged.update(
            {k: v for k, v in (self.resource_limits or {}).items() if v is not None}
        )
        return merged


@dataclass
class SandboxHandle:
    """A live sandbox: where it is, what runs in it, and how to stop it."""

    sandbox_key: str
    backend: SandboxBackendKind
    root_path: Path
    network_policy: SandboxNetworkPolicy
    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    process_ids: list[int] = field(default_factory=list)
    #: Live ``Popen`` handles, kept so the children can be *reaped*. A signalled
    #: child whose parent never waits for it stays in the process table as a
    #: zombie, so "stopped" without a ``wait()`` still consumes a pid for the
    #: lifetime of the API process. Never serialised (handles rebuilt from a row
    #: simply have none).
    processes: dict[str, Any] = field(default_factory=dict, repr=False)
    container_ids: list[str] = field(default_factory=list)
    network_name: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def entry_service(self) -> Optional[str]:
        return self.metadata.get("entry_service")

    def service_port(self, service: str) -> Optional[int]:
        entry = self.services.get(service)
        return int(entry["port"]) if entry and entry.get("port") else None


class _BaseBackend:
    """Common lifecycle contract for sandbox backends."""

    kind: SandboxBackendKind

    async def create(self, spec: SandboxSpec, sandbox_key: str) -> SandboxHandle:
        raise NotImplementedError

    async def start(self, handle: SandboxHandle) -> SandboxHandle:
        return handle

    async def stop(self, handle: SandboxHandle) -> None:
        raise NotImplementedError

    async def destroy(self, handle: SandboxHandle) -> None:
        raise NotImplementedError

    async def inspect(self, handle: SandboxHandle) -> dict[str, Any]:
        return {"services": handle.services, "alive": True}


def rlimit_spec(limits: dict[str, Any]) -> tuple[tuple[int, int], ...]:
    """The ``(resource, value)`` pairs applied to every sandbox service.

    Extracted so the decision is testable without forking: the regression this
    guards against was a limit that *looked* right in the child and broke the
    sandbox on hosts it was never run on (see :func:`_rlimit_preexec`).
    """
    if _resource is None:  # pragma: no cover - Windows
        return ()
    return (
        (_resource.RLIMIT_CPU, int(limits.get("cpu_seconds") or 60)),
        (_resource.RLIMIT_AS, int(limits.get("memory_mb") or 512) * 1024 * 1024),
        (_resource.RLIMIT_FSIZE, int(limits.get("disk_mb") or 64) * 1024 * 1024),
    )


def _rlimit_preexec(limits: dict[str, Any]):  # pragma: no cover - POSIX only
    """Build a ``preexec_fn`` that applies POSIX resource limits.

    Run between fork and exec, so the limits apply to the service process from
    its very first instruction. ``RLIMIT_AS``, ``RLIMIT_CPU`` and
    ``RLIMIT_FSIZE`` are per-process limits, which is what makes them usable
    here: they turn "please do not use much" into a kernel guarantee that a
    runaway fault (a RESOURCE_PRESSURE experiment, say) cannot take down the
    host.

    ``processes`` is deliberately **not** applied as ``RLIMIT_NPROC``. On Linux
    that limit is checked against the real **UID's** total task count — threads
    included — rather than against the process tree it is set on, so a small
    value does not isolate a sandbox from the host; it stops the sandbox from
    working on it. Measured on a non-root account with 44 tasks: with the
    shipped ``REPRO_MAX_PROCESSES=32`` the service process could not create the
    thread that answers its own health probe, so every experiment failed with
    "not ready within 60s" (that is a CI runner, or any service account that is
    not idle); with the cap raised out of the way the same tests pass in 13s.
    Root is exempt from ``RLIMIT_NPROC``, which is why the defect did not show
    up on a root-owned host. The Docker backend enforces a real per-sandbox cap
    with ``--pids-limit``; the local backend cannot without cgroups, and says so
    in ``process_cap_enforced`` on the sandbox metadata.
    """
    if _resource is None:
        return None

    spec = rlimit_spec(limits)

    def _apply() -> None:  # pragma: no cover - runs in the child
        os.setsid()
        for resource_id, value in spec:
            try:
                soft, hard = _resource.getrlimit(resource_id)
                limit = value if hard == _resource.RLIM_INFINITY else min(value, hard)
                _resource.setrlimit(resource_id, (limit, limit))
            except (ValueError, OSError):
                continue

    return _apply


class LocalProcessBackend(_BaseBackend):
    """Process-isolated sandbox: one supervised subprocess per service."""

    kind = SandboxBackendKind.LOCAL_PROCESS

    def __init__(self, sanitizer: Optional[ConfigSanitizer] = None) -> None:
        self._sanitizer = sanitizer or ConfigSanitizer()

    async def create(self, spec: SandboxSpec, sandbox_key: str) -> SandboxHandle:
        template = load_template(spec.template)
        root = Path(
            tempfile.mkdtemp(prefix=f"{sandbox_key}-", dir=str(sandbox_root_base()))
        )
        for sub in ("ports", "telemetry", "state", "logs", "artifacts"):
            (root / sub).mkdir(exist_ok=True)
        (root / "faults.json").write_text(json.dumps({"active": []}), encoding="utf-8")

        selected = spec.services or [svc["name"] for svc in template["services"]]
        configs = {svc["name"]: dict(svc) for svc in template["services"]}
        ordered = self._start_order(template, selected)

        return SandboxHandle(
            sandbox_key=sandbox_key,
            backend=self.kind,
            root_path=root,
            network_policy=spec.network_policy,
            metadata={
                "template": spec.template,
                "entry_service": template.get("entry_service"),
                "services_planned": ordered,
                "service_configs": {name: configs[name] for name in ordered},
                "limits": spec.limits(),
                "runner": template.get("runner", "runners/service_runner.py"),
                "rlimit_applied": _resource is not None,
                #: ``REPRO_MAX_PROCESSES`` is *not* enforceable here: see
                #: ``_rlimit_preexec``. Say so in the manifest instead of
                #: recording a limit that does not exist.
                "process_cap_enforced": False,
            },
        )

    @staticmethod
    def _start_order(template: dict[str, Any], selected: list[str]) -> list[str]:
        """Dependencies before dependents, so a caller always starts last."""
        configs = {svc["name"]: svc for svc in template["services"]}
        order: list[str] = []
        remaining = [name for name in selected if name in configs]

        def visit(name: str, seen: set[str]) -> None:
            if name in order or name in seen or name not in configs:
                return
            seen.add(name)
            dependency = configs[name].get("dependency")
            if dependency:
                visit(dependency, seen)
            if name not in order:
                order.append(name)

        for name in remaining:
            visit(name, set())
        return order

    def _sandbox_env(self, handle: SandboxHandle) -> dict[str, str]:
        """The minimal environment a sandbox service may inherit (§12)."""
        sanitized, _report = self._sanitizer.sanitize_env(dict(os.environ))
        sanitized.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        sanitized["PYTHONUNBUFFERED"] = "1"
        sanitized["PYTHONDONTWRITEBYTECODE"] = "1"
        sanitized["ARGUS_SANDBOX"] = handle.sandbox_key
        sanitized["ARGUS_TELEMETRY_LIMIT"] = str(
            handle.metadata.get("limits", {}).get("telemetry_bytes")
            or settings.REPRO_MAX_TELEMETRY_BYTES
        )
        # Loopback only: no proxy can point a sandbox service at the host network.
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            sanitized.pop(key, None)
        return sanitized

    def _write_runtime(self, handle: SandboxHandle) -> Path:
        return _write_runtime_config(handle)

    async def start(self, handle: SandboxHandle) -> SandboxHandle:
        runner = harness_root() / handle.metadata["runner"]
        if not runner.exists():
            raise SandboxError(f"Sandbox runner missing: {runner}")
        limits = handle.metadata.get("limits", {})
        deadline = time.monotonic() + max(
            5.0, float(settings.REPRO_PROVISION_TIMEOUT_SECONDS)
        )
        env = self._sandbox_env(handle)

        for name in handle.metadata["services_planned"]:
            runtime_path = self._write_runtime(handle)
            log_path = handle.root_path / "logs" / f"{name}.log"
            with log_path.open("ab") as log_handle:
                process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                    [
                        sys.executable,
                        str(runner),
                        "--service",
                        name,
                        "--runtime",
                        str(runtime_path),
                        "--workdir",
                        str(handle.root_path),
                    ],
                    cwd=str(handle.root_path),
                    env=env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    preexec_fn=_rlimit_preexec(limits),
                    start_new_session=_resource is None,
                )
            handle.process_ids.append(process.pid)
            handle.processes[name] = process
            port = await self._await_ready(handle, name, process, deadline)
            handle.services[name] = {
                "port": port,
                "pid": process.pid,
                "status": "healthy",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        return handle

    async def _await_ready(
        self,
        handle: SandboxHandle,
        name: str,
        process: subprocess.Popen,
        deadline: float,
    ) -> int:
        """Wait for a service to publish its port and answer a health probe.

        A service that exits, or one that never listens before the deadline,
        fails provisioning: replaying into a port nobody is listening on would
        produce a "reproduction" made of connection errors.
        """
        port_file = handle.root_path / "ports" / f"{name}.port"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SandboxError(
                    f"Sandbox service {name!r} exited during startup "
                    f"(code {process.returncode}); see logs/{name}.log"
                )
            if port_file.exists():
                try:
                    port = int(port_file.read_text().strip())
                except (ValueError, OSError):
                    port = 0
                if port and await self._probe(name, port):
                    return port
            await asyncio.sleep(0.1)
        raise SandboxError(
            f"Sandbox service {name!r} was not ready within "
            f"{settings.REPRO_PROVISION_TIMEOUT_SECONDS}s"
        )

    @staticmethod
    async def _probe(name: str, port: int) -> bool:
        """TCP + HTTP health probe, bounded so a hung service cannot stall us."""
        loop = asyncio.get_running_loop()
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                pass
        except OSError:
            return False
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _http_health, port), timeout=2.0
            )
        except (asyncio.TimeoutError, SandboxError):
            return False
        return bool(result)

    async def stop(self, handle: SandboxHandle) -> None:
        """Terminate every service: SIGTERM the group, then SIGKILL survivors."""
        for pid in handle.process_ids:
            self._signal_group(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if all(not _pid_alive(pid) for pid in handle.process_ids):
                break
            await asyncio.sleep(0.1)
        for pid in handle.process_ids:
            if _pid_alive(pid):
                self._signal_group(pid, signal.SIGKILL)
        for entry in handle.services.values():
            entry["status"] = "stopped"
        self._reap(handle)

    @staticmethod
    def _reap(handle: SandboxHandle) -> None:
        """Collect the sandbox's children so no zombie outlives the experiment."""
        for name, process in list(handle.processes.items()):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - hostile child
                logger.warning("Sandbox process for %s did not exit", name)
            except Exception:  # pragma: no cover - already reaped elsewhere
                continue
        handle.processes.clear()

    @staticmethod
    def _signal_group(pid: int, sig: int) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    async def destroy(self, handle: SandboxHandle) -> None:
        await self.stop(handle)
        keep = bool(handle.metadata.get("keep_workdir"))
        if keep:
            return
        # A second teardown of an already-removed sandbox is a *success*: nothing
        # is leaking. Reporting it as a cleanup failure would make the retry path
        # (§55) chase a sandbox that no longer exists, so the check is explicit
        # while a real removal failure still raises.
        if not handle.root_path.exists():
            return
        shutil.rmtree(handle.root_path, ignore_errors=False)

    async def inspect(self, handle: SandboxHandle) -> dict[str, Any]:
        alive = {}
        for name, entry in handle.services.items():
            alive[name] = {
                "pid": entry.get("pid"),
                "alive": _pid_alive(int(entry.get("pid") or 0)),
                "port": entry.get("port"),
            }
        return {
            "backend": self.kind.value,
            "services": alive,
            "root_exists": handle.root_path.exists(),
        }


def _http_health(port: int) -> bool:
    """Blocking HTTP health check, run in a thread by the async probe."""
    import http.client

    connection = None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
        connection.request("GET", "/health")
        response = connection.getresponse()
        response.read()
        return response.status < 500
    except Exception:
        return False
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:  # pragma: no cover
                pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_runtime_config(handle: SandboxHandle) -> Path:
    """Write the resolved topology the runners read at startup.

    Two port views exist and they are deliberately different: containers talk to
    each other over the sandbox's internal network (container ports), while
    ARGUS replays against host-loopback published ports. ``port_overrides``
    carries the container view so a service inside the sandbox never depends on
    a host port it cannot see.
    """
    configs = handle.metadata["service_configs"]
    overrides: dict[str, int] = handle.metadata.get("port_overrides") or {}
    services: dict[str, Any] = {}
    for name in handle.metadata["services_planned"]:
        config = dict(configs[name])
        entry = handle.services.get(name)
        if name in overrides:
            config["port"] = int(overrides[name])
        else:
            config["port"] = int(entry["port"]) if entry and entry.get("port") else 0
        services[name] = config
    runtime = {
        "template": handle.metadata["template"],
        "entry_service": handle.metadata["entry_service"],
        "services": services,
    }
    path = handle.root_path / "runtime.json"
    path.write_text(json.dumps(runtime, indent=1), encoding="utf-8")
    return path


def _free_loopback_port() -> int:
    """Ask the OS for an unused loopback port.

    Racy by nature, which is why the caller publishes it immediately and the
    readiness probe verifies the service actually answers on it — a lost race
    shows up as a provisioning failure, not as a silent misroute.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class DockerSandboxBackend(_BaseBackend):
    """Container sandbox on an internal Docker network (§10, §13).

    Container-to-container traffic stays on the ``--internal`` network; the only
    thing published to the host is one loopback port per service, bound to
    ``127.0.0.1`` so the sandbox is unreachable from the network the host is on.
    Inside the container the process binds ``0.0.0.0`` because that is what the
    port mapping requires — which is safe precisely because the network it is
    on has no route anywhere else (§13).

    Opt-in and explicit. If the daemon is unreachable the backend raises rather
    than degrading to the local backend: silently ignoring an operator's
    isolation choice would be the worst possible behaviour for a security
    control.
    """

    kind = SandboxBackendKind.DOCKER

    #: Container-side ports are deterministic, so the runtime topology can be
    #: written *before* any container starts (a service that read a missing
    #: runtime file would crash on boot).
    CONTAINER_PORT_BASE = 18000
    CONTAINER_PORT_STRIDE = 10

    def __init__(self, sanitizer: Optional[ConfigSanitizer] = None) -> None:
        self._sanitizer = sanitizer or ConfigSanitizer()

    async def _docker(self, *args: str, timeout: float = 60.0) -> str:
        process = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError as exc:  # pragma: no cover - daemon hang
            process.kill()
            raise SandboxError(f"docker {args[0]} timed out") from exc
        if process.returncode != 0:
            detail = (stderr or b"").decode(errors="replace").strip()
            raise SandboxError(f"docker {args[0]} failed: {detail[:400]}")
        return (stdout or b"").decode().strip()

    async def create(self, spec: SandboxSpec, sandbox_key: str) -> SandboxHandle:
        try:
            await self._docker("version", "--format", "{{.Server.Version}}", timeout=15)
        except (SandboxError, FileNotFoundError) as exc:
            raise SandboxError(
                "Docker sandbox backend selected but the daemon is unavailable: "
                f"{exc}. Set REPRO_SANDBOX_BACKEND=local or start Docker."
            ) from exc

        template = load_template(spec.template)
        root = Path(
            tempfile.mkdtemp(prefix=f"{sandbox_key}-", dir=str(sandbox_root_base()))
        )
        for sub in ("ports", "telemetry", "state", "logs", "artifacts"):
            (root / sub).mkdir(exist_ok=True)
        (root / "faults.json").write_text(json.dumps({"active": []}), encoding="utf-8")

        network_name = f"{sandbox_key}-net"
        # ``--internal`` means no route to the host or the internet at all.
        await self._docker("network", "create", "--internal", network_name)

        selected = spec.services or [svc["name"] for svc in template["services"]]
        configs = {svc["name"]: dict(svc) for svc in template["services"]}
        ordered = LocalProcessBackend._start_order(template, selected)
        container_ports = {
            name: self.CONTAINER_PORT_BASE + index * self.CONTAINER_PORT_STRIDE
            for index, name in enumerate(ordered)
        }

        return SandboxHandle(
            sandbox_key=sandbox_key,
            backend=self.kind,
            root_path=root,
            network_policy=spec.network_policy,
            network_name=network_name,
            metadata={
                "template": spec.template,
                "entry_service": template.get("entry_service"),
                "services_planned": ordered,
                "service_configs": {name: configs[name] for name in ordered},
                "limits": spec.limits(),
                "runner": template.get("runner", "runners/service_runner.py"),
                "container_ports": container_ports,
                "port_overrides": container_ports,
                #: ``--pids-limit`` below is a genuine per-container cap, so this
                #: backend enforces ``REPRO_MAX_PROCESSES`` and the local one
                #: does not (see ``_rlimit_preexec``).
                "process_cap_enforced": True,
            },
        )

    async def start(self, handle: SandboxHandle) -> SandboxHandle:
        assert handle.network_name is not None
        limits = handle.metadata.get("limits", {})
        runner = harness_root() / handle.metadata["runner"]
        container_ports: dict[str, int] = handle.metadata["container_ports"]

        # The runtime topology is written first: every container reads it during
        # startup, so it must exist before the first `docker run`.
        _write_runtime_config(handle)

        for name in handle.metadata["services_planned"]:
            host_port = _free_loopback_port()
            container_port = container_ports[name]
            container = f"{handle.sandbox_key}-{name}"
            # The runner is mounted read-only; only the sandbox's own
            # telemetry/ports/state directories are writable inside the
            # container, and nothing from the host's filesystem is exposed.
            await self._docker(
                "run",
                "-d",
                "--name",
                container,
                "--network",
                handle.network_name,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                #: The one place the process cap is enforced for real: it is a
                #: per-container limit, unlike RLIMIT_NPROC (see
                #: ``_rlimit_preexec``).
                "--pids-limit",
                str(int(limits.get("processes") or 32)),
                "--cpus",
                "1",
                "--memory",
                f"{int(limits.get('memory_mb') or 512)}m",
                "--tmpfs",
                "/tmp:rw,size=16m",
                #: Loopback-only publish: the sandbox is not reachable from the
                #: host's network, only from ARGUS on this machine.
                "-p",
                f"127.0.0.1:{host_port}:{container_port}",
                "-v",
                f"{runner}:/harness/service_runner.py:ro",
                "-v",
                f"{handle.root_path / 'telemetry'}:/work/telemetry:rw",
                "-v",
                f"{handle.root_path / 'ports'}:/work/ports:rw",
                "-v",
                f"{handle.root_path / 'state'}:/work/state:rw",
                "-v",
                f"{handle.root_path / 'logs'}:/work/logs:rw",
                "-v",
                f"{handle.root_path / 'faults.json'}:/work/faults.json:rw",
                "-e",
                "PYTHONUNBUFFERED=1",
                settings.REPRO_DOCKER_IMAGE,
                "python",
                "/harness/service_runner.py",
                "--service",
                name,
                "--workdir",
                "/work",
                "--host",
                "0.0.0.0",
                "--port",
                str(container_port),
                "--runtime",
                "/work/runtime.json",
            )
            handle.container_ids.append(container)
            handle.services[name] = {
                "port": host_port,
                "container_port": container_port,
                "container_id": container,
                "status": "starting",
            }

        deadline = time.monotonic() + max(
            10.0, float(settings.REPRO_PROVISION_TIMEOUT_SECONDS)
        )
        for name in handle.metadata["services_planned"]:
            port = int(handle.services[name]["port"])
            while time.monotonic() < deadline:
                if await LocalProcessBackend._probe(name, port):
                    handle.services[name]["status"] = "healthy"
                    handle.services[name]["started_at"] = datetime.now(
                        timezone.utc
                    ).isoformat()
                    break
                state = await self._container_state(f"{handle.sandbox_key}-{name}")
                if state in {"exited", "dead"}:
                    raise SandboxError(
                        f"Sandbox container {name!r} exited during startup "
                        f"(state={state}); see logs/{name}.log"
                    )
                await asyncio.sleep(0.25)
            else:
                raise SandboxError(
                    f"Sandbox container {name!r} was not ready within "
                    f"{settings.REPRO_PROVISION_TIMEOUT_SECONDS}s"
                )
        # Keep the containers' own view of each other's ports, never the host's.
        handle.metadata["port_overrides"] = container_ports
        return handle

    async def _container_state(self, container: str) -> str:
        try:
            return await self._docker(
                "inspect", "-f", "{{.State.Status}}", container, timeout=15
            )
        except SandboxError:
            return "absent"

    async def stop(self, handle: SandboxHandle) -> None:
        for container in handle.container_ids:
            try:
                await self._docker("stop", "-t", "2", container, timeout=30)
            except SandboxError as exc:  # already gone is fine
                logger.info("Container stop reported: %s", exc)
        for entry in handle.services.values():
            entry["status"] = "stopped"

    async def destroy(self, handle: SandboxHandle) -> None:
        await self.stop(handle)
        for container in handle.container_ids:
            try:
                await self._docker("rm", "-f", container, timeout=30)
            except SandboxError as exc:
                logger.info("Container removal reported: %s", exc)
        if handle.network_name:
            try:
                await self._docker("network", "rm", handle.network_name, timeout=30)
            except SandboxError as exc:
                logger.info("Network removal reported: %s", exc)
        if not handle.metadata.get("keep_workdir"):
            shutil.rmtree(handle.root_path, ignore_errors=False)

    async def inspect(self, handle: SandboxHandle) -> dict[str, Any]:
        states: dict[str, Any] = {}
        for container in handle.container_ids:
            try:
                states[container] = await self._docker(
                    "inspect", "-f", "{{.State.Status}}", container, timeout=15
                )
            except SandboxError:
                states[container] = "absent"
        return {"backend": self.kind.value, "containers": states}


def backend_for(kind: Optional[SandboxBackendKind] = None) -> _BaseBackend:
    """Resolve the configured backend, refusing unknown values loudly."""
    if kind is None:
        raw = (settings.REPRO_SANDBOX_BACKEND or "local").strip().lower()
        try:
            kind = (
                SandboxBackendKind.LOCAL_PROCESS
                if raw == "local"
                else SandboxBackendKind(raw.upper())
            )
        except ValueError as exc:
            raise SandboxError(
                f"Unknown REPRO_SANDBOX_BACKEND {raw!r}; use 'local' or 'docker'"
            ) from exc
    if kind is SandboxBackendKind.DOCKER:
        return DockerSandboxBackend()
    return LocalProcessBackend()


def sandbox_key_for(experiment_id: Any, run_index: Optional[int] = None) -> str:
    """Deterministic sandbox name: ``argus-repro-<short>[-r<n>]``.

    The run index is part of the name because an experiment with several
    repetitions provisions one sandbox *per repetition*: without it, the second
    repetition would collide with the first sandbox's unique key — and a
    collision here is not cosmetic, it is the difference between an experiment
    that records what it ran and one that fails on its own bookkeeping.
    """
    token = str(experiment_id).replace("-", "")[:12]
    suffix = f"-r{int(run_index)}" if run_index else ""
    return f"argus-repro-{token}{suffix}"


class SandboxManager:
    """Creates, tracks, and — always — destroys experiment sandboxes (§11, §55).

    The manager is deliberately async-and-explicit about failure: every method
    that can fail raises, and :meth:`destroy` reports cleanup failure instead of
    masking it, because a leaked sandbox is exactly what §55 exists to prevent.
    """

    def __init__(self, backend: Optional[_BaseBackend] = None) -> None:
        self._backend = backend

    @property
    def backend(self) -> _BaseBackend:
        if self._backend is None:
            self._backend = backend_for()
        return self._backend

    async def create(
        self,
        spec: SandboxSpec,
        *,
        experiment_id: Any,
        run_index: Optional[int] = None,
    ) -> SandboxHandle:
        """Allocate a fresh isolated environment (not yet started)."""
        key = sandbox_key_for(experiment_id, run_index)
        handle = await self.backend.create(spec, key)
        handle.metadata["spec"] = {
            "template": spec.template,
            "network_policy": spec.network_policy.value,
            "timeout_seconds": spec.timeout_seconds,
            "services": spec.services,
            "resource_limits": spec.limits(),
            **({"extra": spec.metadata} if spec.metadata else {}),
        }
        return handle

    async def start(self, handle: SandboxHandle) -> SandboxHandle:
        """Start every service and wait until each answers a health probe."""
        return await self.backend.start(handle)

    async def provision(
        self,
        spec: SandboxSpec,
        *,
        experiment_id: Any,
        run_index: Optional[int] = None,
    ) -> SandboxHandle:
        """Create + start in one step, used by the worker's provisioning job."""
        handle = await self.create(
            spec, experiment_id=experiment_id, run_index=run_index
        )
        try:
            return await self.start(handle)
        except Exception:
            # A half-built sandbox must not outlive the failure that produced it.
            await self.destroy(handle)
            raise

    async def stop(self, handle: SandboxHandle) -> None:
        await self.backend.stop(handle)

    async def destroy(
        self, handle: SandboxHandle, *, keep_workdir: Optional[bool] = None
    ) -> dict[str, Any]:
        """Tear the sandbox down, always attempting cleanup.

        Returns a report rather than raising, so the caller can record the
        attempt on the sandbox row and retry — an unreported cleanup failure is
        an orphan (§55).
        """
        if keep_workdir is not None:
            handle.metadata["keep_workdir"] = keep_workdir
        report: dict[str, Any] = {
            "destroyed": False,
            "error": None,
            "root_path": str(handle.root_path),
        }
        try:
            await self.backend.destroy(handle)
            report["destroyed"] = True
        except Exception as exc:  # noqa: BLE001 - cleanup must never raise upward
            report["error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("Sandbox %s cleanup failed: %s", handle.sandbox_key, exc)
            # Best effort at the filesystem level, but do not launder the error.
            try:
                shutil.rmtree(handle.root_path, ignore_errors=True)
            except Exception:  # pragma: no cover
                pass
        return report

    async def inspect(self, handle: SandboxHandle) -> dict[str, Any]:
        return await self.backend.inspect(handle)

    async def sweep_orphans(self, handle: SandboxHandle) -> list[int]:
        """Kill service processes that outlived a destroyed sandbox.

        Belt-and-braces for the local backend: even if a stop signal is lost,
        the sandbox's own recorded pids are checked and reaped.
        """
        survivors: list[int] = []
        for pid in handle.process_ids:
            if _pid_alive(pid):
                LocalProcessBackend._signal_group(pid, signal.SIGKILL)
                survivors.append(pid)
        return survivors


def sandbox_metrics() -> dict[str, Any]:
    """Observability of ARGUS's own sandbox usage (§54).

    Counts what exists on disk right now, which is the only view that can catch
    a leak: a counter incremented in memory would happily report zero while
    directories accumulate.
    """
    base = sandbox_root_base()
    existing = [item for item in base.glob("argus-repro-*") if item.is_dir()]
    total_bytes = 0
    for item in existing:
        for path in item.rglob("*"):
            try:
                if path.is_file():
                    total_bytes += path.stat().st_size
            except OSError:  # pragma: no cover - racing cleanup
                continue
    return {
        "sandbox_root": str(base),
        "sandboxes_on_disk": len(existing),
        "sandbox_bytes": total_bytes,
        "artifact_root": str(artifact_root_base()),
        "backend_configured": (settings.REPRO_SANDBOX_BACKEND or "local"),
        "rlimit_available": _resource is not None,
    }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_sandbox_id() -> uuid.UUID:
    return uuid.uuid4()


__all__ = [
    "DockerSandboxBackend",
    "LocalProcessBackend",
    "SandboxError",
    "SandboxHandle",
    "SandboxManager",
    "SandboxSpec",
    "SandboxStatus",
    "artifact_root_base",
    "backend_for",
    "harness_root",
    "DEFAULT_TEMPLATE",
    "TEMPLATE_ALIASES",
    "load_environment_descriptor",
    "resolve_template_name",
    "load_fixture",
    "load_template",
    "match_service",
    "new_sandbox_id",
    "sandbox_key_for",
    "sandbox_metrics",
    "sandbox_root_base",
    "utcnow",
]
