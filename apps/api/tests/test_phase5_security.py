"""Phase 5 security tests (§3, §12, §15–§18, §56, §57).

The reproduction engine is the one part of ARGUS that executes things, so its
boundaries are tested as boundaries rather than as behaviour:

* nothing a user can send reaches a process environment or a payload unsanitized;
* a payload that still looks sensitive is **refused before transmission**, not
  cleaned up on the way out;
* the API surface rejects executable content — there is no field that carries a
  shell command, an image, a URL, or a host path;
* every read and every action is scoped to a project, so one tenant's experiment
  is invisible to another.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.config import get_settings
from app.models.reproduction import FaultType, ReplayMode, SandboxNetworkPolicy
from app.services.reproduction_sanitizer import (
    REDACTED,
    SECRET_SUBSTITUTES,
    ConfigSanitizer,
    InputSanitizer,
)
from app.services.reproduction_sandbox import SandboxSpec
from app.services.replay_engine import ReplayEngine, ReplayItem

settings = get_settings()


class TestInputSanitization:
    def test_secrets_are_redacted_and_identifiers_are_pseudonymized(self) -> None:
        sanitizer = InputSanitizer()
        payload = {
            "sku": "SKU-1",
            "quantity": 1,
            "user_id": "user-42",
            "email": "buyer@example.com",
            "password": "hunter2",
            "authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.abcdefgh.ijklmnop",
            "card_number": "4111111111111111",
        }
        sanitized, report = sanitizer.sanitize_payload(payload)

        # Non-identifying structure survives, so the request still exercises the
        # same code path in the sandbox.
        assert sanitized["sku"] == "SKU-1"
        assert sanitized["quantity"] == 1
        # Secrets are gone; nothing of the original remains.
        joined = repr(sanitized)
        for secret in (
            "hunter2",
            "buyer@example.com",
            "4111111111111111",
            "eyJhbGciOiJIUzI1NiJ9",
        ):
            assert secret not in joined
        assert sanitized["password"] == REDACTED
        # The identifier is replaced, not deleted: the request keeps its shape.
        assert sanitized["user_id"] != "user-42"
        assert len(sanitized["user_id"]) > 0
        assert report.redactions
        # The report names paths and kinds, never the values it found.
        assert "hunter2" not in repr(report.as_dict())

    def test_pseudonyms_are_stable_across_runs(self) -> None:
        sanitizer = InputSanitizer()
        first, _ = sanitizer.sanitize_payload({"user_id": "user-42"})
        second, _ = sanitizer.sanitize_payload({"user_id": "user-42"})
        # Repeatable pseudonyms are what let two reproductions be compared.
        assert first["user_id"] == second["user_id"]

    def test_a_nested_credential_blob_is_tainted_not_inspected(self) -> None:
        sanitizer = InputSanitizer()
        payload = {
            "credentials": {
                "aws": {"access_key": "AKIAIOSFODNN7EXAMPLE", "region": "eu-west-1"}
            },
            "note": "ok",
        }
        sanitized, report = sanitizer.sanitize_payload(payload)
        # Everything under the secret-named key is tainted, so a credential that
        # hides one level deeper cannot survive as a leaf nobody checked — and a
        # value beside it (the region) is redacted too, because being adjacent to
        # a credential is not a reason to trust it.
        assert "AKIAIOSFODNN7EXAMPLE" not in repr(sanitized)
        assert sanitized["credentials"]["aws"]["access_key"] == REDACTED
        assert sanitized["credentials"]["aws"]["region"] == REDACTED
        assert sanitized["note"] == "ok"
        assert report.redactions
        assert sanitizer.assert_sanitized(sanitized) == []

    def test_embedded_values_are_scrubbed_inside_free_text(self) -> None:
        sanitizer = InputSanitizer()
        sanitized, _ = sanitizer.sanitize_payload(
            {"note": "contact ops@example.com or +1 415 555 0100 to confirm"}
        )
        assert "ops@example.com" not in sanitized["note"]
        assert "415 555 0100" not in sanitized["note"]

    def test_pre_flight_check_is_independent_of_the_walk(self) -> None:
        sanitizer = InputSanitizer()
        # A payload that was never sanitized must be reported, and the finding
        # must name the path so an operator can act on it.
        findings = sanitizer.assert_sanitized({"password": "hunter2"})
        assert findings and "password" in findings[0]

    def test_absurd_nesting_is_refused_rather_than_guessed(self) -> None:
        from app.services.reproduction_sanitizer import SanitizationError

        sanitizer = InputSanitizer()
        payload: dict = {"a": {}}
        node = payload["a"]
        for _ in range(20):
            node["next"] = {}
            node = node["next"]
        # Depth that cannot be *proven* safe is refused: the experiment does not
        # run rather than running with an unverified payload (§15).
        with pytest.raises(SanitizationError):
            sanitizer.sanitize_payload(payload)


class TestConfigSanitization:
    def test_environment_is_minimal_and_never_carries_a_credential(self) -> None:
        sanitizer = ConfigSanitizer()
        env = {
            "PATH": "/usr/bin",
            "LANG": "C.UTF-8",
            "DATABASE_URL": "postgresql://prod:supersecret@db.internal:5432/app",
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "GITHUB_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "MY_PRIVATE_SETTING": "keep-me-out",
            "HTTP_PROXY": "http://proxy.internal:3128",
        }
        cleaned, report = sanitizer.sanitize_env(env)
        assert "AWS_SECRET_ACCESS_KEY" not in cleaned
        assert "GITHUB_TOKEN" not in cleaned
        assert cleaned.get("DATABASE_URL") == SECRET_SUBSTITUTES["DATABASE_URL"]
        assert "supersecret" not in repr(cleaned)
        # Anything not on the allowlist is dropped rather than forwarded.
        assert "MY_PRIVATE_SETTING" not in cleaned
        assert "HTTP_PROXY" not in cleaned
        assert cleaned["PATH"] == "/usr/bin"
        assert report.dropped_keys

    def test_configuration_documents_have_secrets_replaced(self) -> None:
        sanitizer = ConfigSanitizer()
        config = {
            "checkout": {
                "timeout_ms": 2700,
                "payment_provider": "sandbox-payments",
                "api_key": "sk_live_abcdefghijklmnop",
            }
        }
        cleaned, _ = sanitizer.sanitize_config(config)
        assert cleaned["checkout"]["timeout_ms"] == 2700
        assert cleaned["checkout"]["api_key"] == REDACTED
        assert sanitizer.assert_no_secrets(cleaned) == []


class TestReplaySafetyGate:
    """§18 — validation happens before execution, and failure means no execution."""

    def _handle(self, services):
        from app.services.reproduction_sandbox import SandboxHandle
        from app.models.reproduction import SandboxBackendKind

        root = services["root"]
        return SandboxHandle(
            sandbox_key="argus-repro-test",
            backend=SandboxBackendKind.LOCAL_PROCESS,
            root_path=root,
            network_policy=SandboxNetworkPolicy.ISOLATED,
            services={
                name: {"port": entry["port"], "pid": 1, "status": "healthy"}
                for name, entry in services.items()
                if name != "root"
            },
        )

    async def test_every_rejection_reason_is_specific(self, tmp_path) -> None:
        (tmp_path / "telemetry").mkdir()
        engine = ReplayEngine()
        handle = self._handle({"root": tmp_path, "checkout": {"port": 59999}})

        cases = {
            "unknown target service": ReplayItem(
                method="GET", target_service="payment", target_path="/x"
            ),
            "relative path": ReplayItem(
                method="GET", target_service="checkout", target_path="checkout"
            ),
            "traversal": ReplayItem(
                method="POST", target_service="checkout", target_path="/a/../.."
            ),
            "absolute host path": ReplayItem(
                method="POST", target_service="checkout", target_path="/etc/passwd:x"
            ),
            "shell method": ReplayItem(
                method="EXEC", target_service="checkout", target_path="/checkout"
            ),
            "unsanitized payload": ReplayItem(
                method="POST",
                target_service="checkout",
                target_path="/checkout",
                payload={"password": "hunter2"},
            ),
        }
        for label, item in cases.items():
            reason = engine.validate(item, handle)
            assert reason, f"{label} was accepted by the safety gate"
        # And a legitimate item passes the same gate.
        ok = ReplayItem(
            method="POST",
            target_service="checkout",
            target_path="/checkout",
            payload={"sku": "SKU-1"},
        )
        assert engine.validate(ok, handle) is None

    def test_replay_set_is_bounded(self) -> None:
        from app.services.replay_engine import ReplayError

        engine = ReplayEngine()
        items = [
            ReplayItem(method="GET", target_service="checkout", target_path="/x")
            for _ in range(settings.REPRO_MAX_REPLAY_REQUESTS + 1)
        ]
        with pytest.raises(ReplayError):
            engine.prepare(items)

    def test_prepare_sanitizes_and_preserves_order(self) -> None:
        engine = ReplayEngine()
        prepared = engine.prepare(
            [
                ReplayItem(
                    method="post",
                    target_service="checkout",
                    target_path="/checkout",
                    payload={"user_id": "user-42", "password": "hunter2"},
                ),
                ReplayItem(method="get", target_service="inventory", target_path="/i"),
            ]
        )
        assert [item.plan_order for item in prepared] == [0, 1]
        assert prepared[0].method == "POST"
        assert prepared[0].payload["password"] == REDACTED
        assert prepared[0].payload["user_id"] != "user-42"
        # Duplicate requests are kept as separate items: two identical calls are
        # two observations, and collapsing them would hide a rate-dependent fault.
        assert len({item.replay_id for item in prepared}) == 2


class TestApiRejectsExecutableContent:
    """§57 — nothing a client sends may become a command, image, path or URL."""

    def test_reproduction_request_has_no_executable_surface(self) -> None:
        from app.schemas.reproduction import (
            CreateReproductionRequest,
            FaultSpecRequest,
        )

        # ``extra="forbid"`` means an attempt to smuggle a shell command into a
        # typed request is a validation error, not a silently ignored field.
        for field_name in ("command", "docker_run", "image", "host_path", "url"):
            try:
                CreateReproductionRequest(**{field_name: "rm -rf /"})
            except Exception:
                continue
            raise AssertionError(f"{field_name} was accepted by the request schema")

        # A fault is a typed instruction with an allowlisted target; it cannot be
        # an arbitrary parameter blob either.
        try:
            FaultSpecRequest.model_validate(
                {
                    "fault_type": "SHELL",
                    "target": "datastore",
                    "parameters": {"cmd": "sh"},
                }
            )
        except Exception:
            pass
        else:
            raise AssertionError("an unknown fault type was accepted")

        # Even a valid fault cannot name a target outside the sandbox's services.
        from app.services.fault_injection import FaultInjectionError, build_spec

        with pytest.raises(FaultInjectionError):
            build_spec(
                fault_type=FaultType.LATENCY, target="../host", services=["datastore"]
            )

        # An empty body is legal and means "plan it from the incident".
        empty = CreateReproductionRequest()
        assert empty.repetitions is None and empty.inputs is None
        assert empty.network_policy is None and empty.candidate_id is None

    def test_network_policy_defaults_to_isolated(self) -> None:
        # Absent an explicit choice the plan is isolated; ``CONTROLLED_EGRESS``
        # requires an allowlist and is never assumed.
        spec = SandboxSpec(template="demo_commerce")
        assert spec.network_policy is SandboxNetworkPolicy.ISOLATED
        assert spec.network_policy is not SandboxNetworkPolicy.CONTROLLED_EGRESS

    def test_replay_mode_default_is_sequential(self) -> None:
        # Concurrency is opt-in (§19), so the default must not be parallel.
        assert ReplayMode(settings.REPRO_DEFAULT_REPLAY_MODE) is ReplayMode.SEQUENTIAL


class TestProjectIsolation:
    """Cross-project access is refused, on both the read and the write path."""

    async def test_another_project_cannot_read_or_start_an_experiment(
        self, client, db_session, db_engine
    ) -> None:
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from app.models.incident import Incident
        from app.models.project import SoftwareProject
        from app.services.reproduction_orchestrator import ReproductionOrchestrator
        from test_phase4_demo import _analyse_checkout

        project, _env, _components, analysis, candidates = await _analyse_checkout(
            db_session
        )
        await db_session.commit()
        incident = await db_session.get(Incident, analysis.incident_id)
        assert incident is not None
        orchestrator = ReproductionOrchestrator(
            async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        )
        experiment = await orchestrator.prepare(
            incident=incident, candidate=candidates[0], analysis_id=analysis.id
        )
        other = SoftwareProject(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
        db_session.add(other)
        await db_session.commit()

        owned = client.get(
            f"/api/v1/reproductions/{experiment.id}",
            params={"project_id": str(project.id)},
        )
        assert owned.status_code == 200

        stolen = client.get(
            f"/api/v1/reproductions/{experiment.id}",
            params={"project_id": str(other.id)},
        )
        assert stolen.status_code == 404

        started = client.post(
            f"/api/v1/reproductions/{experiment.id}/start",
            params={"project_id": str(other.id)},
            json={"confirm_sandbox": True},
        )
        assert started.status_code == 404

        listed = client.get(
            "/api/v1/reproductions", params={"project_id": str(other.id)}
        )
        assert listed.status_code == 200
        assert listed.json()["items"] == []
