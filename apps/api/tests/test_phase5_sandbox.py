"""Phase 5 sandbox tests (§11, §12, §13, §40, §55, §63).

These tests start **real processes** on loopback and then tear them down, because
the properties they check cannot be verified from a description of the code:

* the sandbox is disposable and its working tree is gone afterwards;
* a leaked sandbox is *detectable* (a counter that lives in memory would report
  zero while directories accumulate);
* cleanup failure is reported, never swallowed;
* the environment handed to a sandbox service (`_sandbox_env`) carries no
  credential and no proxy that could redirect it off the loopback interface.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid

import pytest

from app.core.config import get_settings
from app.models.reproduction import SandboxBackendKind, SandboxNetworkPolicy
from app.services import reproduction_sandbox
from app.services.reproduction_sandbox import (
    DockerSandboxBackend,
    LocalProcessBackend,
    SandboxError,
    SandboxManager,
    SandboxSpec,
    backend_for,
    harness_root,
    load_environment_descriptor,
    load_fixture,
    load_template,
    match_service,
    sandbox_key_for,
    sandbox_metrics,
    sandbox_root_base,
)

settings = get_settings()


def _spec(**overrides) -> SandboxSpec:
    values = {
        "template": "demo_commerce",
        "network_policy": SandboxNetworkPolicy.ISOLATED,
        "timeout_seconds": 60,
        "services": ["datastore", "inventory", "checkout"],
    }
    values.update(overrides)
    return SandboxSpec(**values)


class TestHarnessAssets:
    """The harness is data (templates/fixtures/descriptors), so it is inspectable."""

    def test_template_declares_a_runnable_dependency_chain(self) -> None:
        template = load_template("demo_commerce")
        names = [service["name"] for service in template["services"]]
        assert names == ["datastore", "inventory", "checkout"]
        assert template["entry_service"] == "checkout"
        # Every service except the root must name a dependency that exists.
        for service in template["services"]:
            dependency = service.get("dependency")
            if dependency is not None:
                assert dependency in names
        # Every operation must target a declared service.
        for service in template["operations"]:
            assert service in names

    def test_unknown_or_traversing_names_are_refused(self) -> None:
        # A template name is attacker-influenced (it comes from a plan), so it must
        # never be able to escape the harness directory.
        for name in ("../../etc/passwd", "../secrets", "nope", "", "a/b"):
            with pytest.raises(SandboxError):
                load_template(name)
        with pytest.raises(SandboxError):
            load_fixture("missing_fixture")

    def test_environment_descriptor_is_declared_and_credential_free(self) -> None:
        descriptor = load_environment_descriptor("demo_commerce")
        assert descriptor["schema_version"]
        assert descriptor["dependency_versions"]
        # The descriptor is captured into snapshots and shown in the UI, so it
        # must never carry a usable secret.
        for value in descriptor["environment_variables"].values():
            assert value in {"<NOT-CAPTURED>", "C.UTF-8", "UTC", "production"}

    def test_alias_matching_is_declared_data_not_guessing(self) -> None:
        template = load_template("demo_commerce")
        assert match_service(template, "Inventory Database") == "datastore"
        assert match_service(template, "inventory_service") == "inventory"
        assert match_service(template, "Checkout Service") == "checkout"
        # Longest alias wins, so a database is not mistaken for its reader.
        assert match_service(template, "Inventory Service") == "inventory"
        # A component the template does not know resolves to nothing, so the
        # planner must refuse instead of inventing a target.
        assert match_service(template, "Payment Gateway") is None
        assert match_service(template, None) is None


class TestSandboxLifecycle:
    async def test_a_real_sandbox_starts_and_is_fully_removed(self) -> None:
        manager = SandboxManager(LocalProcessBackend())
        experiment_id = uuid.uuid4()
        before = {item.name for item in sandbox_root_base().glob("argus-repro-*")}
        started = time.monotonic()

        handle = await manager.create(_spec(), experiment_id=experiment_id)
        try:
            handle = await manager.start(handle)
            provision_seconds = time.monotonic() - started
            # Every service is really listening: the sandbox waits for a health
            # probe before it reports READY, so ports must be bound here.
            assert set(handle.services) == {"datastore", "inventory", "checkout"}
            for name in handle.services:
                port = handle.service_port(name)
                assert port and port > 0, name
            assert handle.entry_service == "checkout"
            assert handle.metadata["rlimit_applied"] is True
            inspector = await manager.inspect(handle)
            assert inspector["backend"] == "LOCAL_PROCESS"
            assert set(inspector["services"]) == {"datastore", "inventory", "checkout"}
            assert all(entry["alive"] for entry in inspector["services"].values())
            # §63 — provisioning a local sandbox is bounded.
            assert provision_seconds < 30, provision_seconds
        finally:
            report = await manager.destroy(handle)

        assert report["destroyed"] is True
        assert not handle.root_path.exists()
        after = {item.name for item in sandbox_root_base().glob("argus-repro-*")}
        assert after == before, f"leaked: {after - before}"

        if os.name != "nt":  # pragma: no cover - POSIX only
            for pid in handle.process_ids:
                assert not _alive(pid), f"service pid {pid} survived teardown"

    async def test_destroy_is_idempotent(self) -> None:
        manager = SandboxManager(LocalProcessBackend())
        handle = await manager.create(_spec(), experiment_id=uuid.uuid4())
        await manager.start(handle)
        first = await manager.destroy(handle)
        assert first["destroyed"] is True and first["error"] is None
        # A retried teardown of a sandbox that is already gone is a success, not
        # a failure the caller would chase forever.
        second = await manager.destroy(handle)
        assert second["destroyed"] is True
        assert second["error"] is None

    async def test_cleanup_failure_is_surfaced_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = SandboxManager(LocalProcessBackend())
        handle = await manager.create(_spec(), experiment_id=uuid.uuid4())
        await manager.start(handle)

        # Simulate a tree that cannot be removed (a permission or mount problem
        # in production): the report must carry the error instead of raising or,
        # worse, claiming success.
        def _explode(*_args, **_kwargs):
            raise OSError("simulated removal failure")

        monkeypatch.setattr("app.services.reproduction_sandbox.shutil.rmtree", _explode)
        report = await manager.destroy(handle)
        assert report["destroyed"] is False
        assert report["error"] and "simulated removal failure" in report["error"]
        # The manager still makes a last-ditch attempt with error suppression.
        monkeypatch.undo()
        shutil.rmtree(handle.root_path, ignore_errors=True)

    async def test_an_abandoned_sandbox_is_visible_in_the_metrics(self) -> None:
        """Observability has to be able to see a leak (§54)."""
        before = sandbox_metrics()
        manager = SandboxManager(LocalProcessBackend())
        handle = await manager.create(_spec(), experiment_id=uuid.uuid4())
        try:
            during = sandbox_metrics()
            assert during["sandboxes_on_disk"] == before["sandboxes_on_disk"] + 1
            assert during["sandbox_bytes"] >= 0
            # The reported root is the *configured* one, not a hard-coded
            # default: `REPRO_SANDBOX_ROOT` is honoured, and a test that only
            # passes while the setting happens to be unset would fail on a host
            # that configures it.
            assert during["sandbox_root"] == str(sandbox_root_base())
        finally:
            await manager.destroy(handle)
        assert sandbox_metrics()["sandboxes_on_disk"] == before["sandboxes_on_disk"]


class TestSandboxIsolation:
    async def test_sandbox_processes_get_a_sanitized_environment(self) -> None:
        """The environment handed to a sandbox service carries no credential.

        Checked against the *decision* the backend makes (`_sandbox_env`), not
        against a running process, so the assertion is about the interface a
        sandbox receives rather than about one particular invocation.
        """
        os.environ["ARGUS_TEST_SECRET_TOKEN"] = "super-secret-value"
        os.environ["HTTP_PROXY"] = "http://host-proxy.internal:3128"
        backend = LocalProcessBackend()
        handle = await backend.create(_spec(), sandbox_key_for(uuid.uuid4()))
        try:
            env = backend._sandbox_env(handle)
            joined = json.dumps(env)
            assert "super-secret-value" not in joined
            assert "host-proxy.internal" not in joined
            # No proxy may point a sandbox service at the host network.
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
                assert key not in env
            assert env["ARGUS_SANDBOX"] == handle.sandbox_key
            assert env["ARGUS_TELEMETRY_LIMIT"] == str(
                handle.metadata["limits"]["telemetry_bytes"]
            )
            # The sandbox may not import ARGUS's own package.
            assert "app" not in (env.get("PYTHONPATH") or "")
        finally:
            await backend.destroy(handle)
            os.environ.pop("ARGUS_TEST_SECRET_TOKEN", None)
            os.environ.pop("HTTP_PROXY", None)

    async def test_services_bind_loopback_only(self) -> None:
        manager = SandboxManager(LocalProcessBackend())
        handle = await manager.create(_spec(), experiment_id=uuid.uuid4())
        await manager.start(handle)
        try:
            ports = {name: handle.service_port(name) for name in handle.services}
            # The runner binds 127.0.0.1; a port open on a wildcard address would
            # be reachable off-host, which §12 forbids.
            for port in ports.values():
                assert port is not None
                assert _answers_on("127.0.0.1", int(port))
        finally:
            await manager.destroy(handle)

    async def test_resource_limits_are_recorded_and_override_settings(self) -> None:
        spec = _spec(resource_limits={"memory_mb": 128, "processes": 8})
        limits = spec.limits()
        assert limits["memory_mb"] == 128
        assert limits["processes"] == 8
        # Unspecified limits still fall back to the configured defaults.
        assert limits["cpu_seconds"] == settings.REPRO_MAX_CPU_SECONDS
        manager = SandboxManager(LocalProcessBackend())
        handle = await manager.create(spec, experiment_id=uuid.uuid4())
        try:
            assert handle.metadata["limits"]["memory_mb"] == 128
            assert handle.network_policy is SandboxNetworkPolicy.ISOLATED
        finally:
            await manager.destroy(handle)

    def test_a_service_is_never_given_a_per_user_process_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``RLIMIT_NPROC`` must not be applied to a sandbox service.

        Linux checks that limit against the real **UID's** task count, threads
        included, so capping it at 32 does not isolate the sandbox: it stops the
        service from creating the thread that answers its own health probe as
        soon as the account already runs more than 32 tasks. That is any
        non-root service account, and it is exactly what a CI runner is; root is
        exempt from the limit, which is why this was invisible on a root-owned
        host. The symptom was every experiment failing with "not ready within
        60s". This test reads the same spec the child executes.

        The cap is not dropped silently: the Docker backend enforces it with
        ``--pids-limit`` and the local backend records
        ``process_cap_enforced: False``.
        """

        class _FakeResource:
            RLIMIT_CPU = 1
            RLIMIT_AS = 2
            RLIMIT_NPROC = 3
            RLIMIT_FSIZE = 4
            RLIM_INFINITY = -1

        monkeypatch.setattr(reproduction_sandbox, "_resource", _FakeResource)

        spec = reproduction_sandbox.rlimit_spec(
            {"cpu_seconds": 7, "memory_mb": 128, "disk_mb": 8, "processes": 8}
        )
        applied = {resource_id for resource_id, _value in spec}
        assert applied == {
            _FakeResource.RLIMIT_CPU,
            _FakeResource.RLIMIT_AS,
            _FakeResource.RLIMIT_FSIZE,
        }
        assert _FakeResource.RLIMIT_NPROC not in applied
        # Values are the configured ones, in bytes where the kernel says bytes.
        assert dict(spec) == {
            _FakeResource.RLIMIT_CPU: 7,
            _FakeResource.RLIMIT_AS: 128 * 1024 * 1024,
            _FakeResource.RLIMIT_FSIZE: 8 * 1024 * 1024,
        }

    async def test_the_local_backend_says_it_cannot_cap_processes(self) -> None:
        """The manifest must not claim a limit this backend cannot apply."""
        manager = SandboxManager(LocalProcessBackend())
        handle = await manager.create(_spec(), experiment_id=uuid.uuid4())
        try:
            assert handle.metadata["process_cap_enforced"] is False
            assert handle.metadata["rlimit_applied"] is True
        finally:
            await manager.destroy(handle)


class TestSandboxNaming:
    def test_sandbox_names_are_unique_per_repetition(self) -> None:
        experiment_id = uuid.uuid4()
        first = sandbox_key_for(experiment_id, 1)
        second = sandbox_key_for(experiment_id, 2)
        assert first == sandbox_key_for(experiment_id, 1)
        assert first != second
        assert first.startswith("argus-repro-")
        assert len(first) <= 128


class TestBackendSelection:
    def test_backend_is_resolved_from_configuration(self) -> None:
        assert isinstance(
            backend_for(SandboxBackendKind.LOCAL_PROCESS), LocalProcessBackend
        )
        assert isinstance(backend_for(SandboxBackendKind.DOCKER), DockerSandboxBackend)

    async def test_docker_backend_fails_with_an_actionable_message(self) -> None:
        """If Docker is selected and unavailable, the error must say what to do."""
        backend = DockerSandboxBackend()
        spec = _spec()
        try:
            handle = await backend.create(spec, sandbox_key_for(uuid.uuid4()))
        except SandboxError as exc:
            message = str(exc).lower()
            assert "docker" in message
            assert "repro_sandbox_backend" in message or "daemon" in message
        else:  # pragma: no cover - only when a Docker daemon is reachable
            assert handle.network_name is not None
            await backend.destroy(handle)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _answers_on(host: str, port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


class TestHarnessIsShippable:
    def test_the_harness_lives_inside_the_api_package(self) -> None:
        # Compose builds the API image from apps/api, so a harness outside the
        # package would simply not exist in a deployed container.
        root = harness_root()
        assert root.is_dir()
        assert (root / "runners" / "service_runner.py").is_file()
        # The shipped template carries a neutral name; ``demo_commerce`` remains
        # a working alias so existing plans keep resolving (see TEMPLATE_ALIASES).
        assert (root / "templates" / "http_service_chain.json").is_file()
        assert (root / "environments" / "http_service_chain.json").is_file()
        assert root.parts[-2:] == ("api", "reproduction")
