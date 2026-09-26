"""ARGUS hardening W4 — failure, recovery and resilience verification.

Every test here enters a state the happy-path suites never reach: a dependency
is gone, the broker is down, a provider lies, a client sends a body nobody
intended to parse. Each asserts a *specific recorded outcome* — a status, a
reason string, a preserved row — because "it did not crash" is not a guarantee.

What is deliberately **not** repeated here, because the phase suites already
pin it: the remediation refusal matrix (duplicate, stale, blast radius,
rollback), the AI debugger's degradation contract, the reproduction sandbox
reaper, and the ingestion queue's synchronous fallback. This module covers the
layer beneath those, plus the two contract gaps found in the second audit pass:
dependency-outage reporting and request-body abuse.
"""

from __future__ import annotations

import json
import socket
import uuid

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import get_db
from app.core.edge import (
    BodySizeLimitMiddleware,
    JsonDepthLimitMiddleware,
    json_nesting_depth,
)
from app.main import app
from app.models.ingestion import IngestionFailure
from app.services.ai_debugger import OpenAICompatibleProvider
from app.services.patch_verification import PatchVerificationEngine
from app.services.queue import (
    IngestionQueue,
    IngestionWorker,
    PermanentJobError,
    make_job,
)


def _dead_port() -> int:
    """A port nothing is listening on (bound then released)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _session_factory(db_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


async def _noop_process(*, kind: str, payload: dict) -> None:
    """A processor that does nothing — used when only bookkeeping is tested."""


# ---------------------------------------------------------------------------
# 1. Dependency outage: the API must *say* what is broken
# ---------------------------------------------------------------------------
class TestDependencyOutage:
    def test_redis_outage_is_reported_as_degraded(self, client, monkeypatch) -> None:
        """A dead broker shows up as an unhealthy dependency, not a 500."""
        from app.api.v1.routes import health as health_routes

        monkeypatch.setattr(health_routes, "REDIS_PORT", _dead_port())

        response = client.get("/health/dependencies")

        assert response.status_code == 200
        body = response.json()
        redis = next(d for d in body["dependencies"] if d["name"] == "redis")
        assert redis["status"] == "unhealthy"
        assert redis["error"], "an unhealthy dependency must carry a reason"
        assert body["status"] == "degraded"

    def test_database_outage_is_reported_as_degraded(self, client) -> None:
        """A dead database degrades readiness without disclosing driver internals."""

        class _BrokenSession:
            async def execute(self, *args, **kwargs):
                raise OSError("connection refused")

        async def _broken_db():
            yield _BrokenSession()

        app.dependency_overrides[get_db] = _broken_db
        try:
            ready = client.get("/health/ready")
            assert ready.status_code == 200
            # The status is a *verdict*; the raw driver error is not echoed.
            assert ready.json()["status"] == "degraded"
            assert "connection refused" not in ready.text

            deps = client.get("/health/dependencies").json()
            postgres = next(
                d for d in deps["dependencies"] if d["name"] == "postgresql"
            )
            assert postgres["status"] == "unhealthy"
            assert deps["status"] == "degraded"
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_liveness_survives_a_dead_dependency(self, client, monkeypatch) -> None:
        """Liveness never depends on the things that can be down.

        A restart caused by a dependency outage is a restart that fixes
        nothing — the probe must answer 'the process is up' and nothing else.
        """
        from app.api.v1.routes import health as health_routes

        monkeypatch.setattr(health_routes, "REDIS_PORT", _dead_port())
        assert client.get("/health/live").json()["status"] == "healthy"


# ---------------------------------------------------------------------------
# 2. Queue bookkeeping: retries, dead-letters and corrupt payloads
# ---------------------------------------------------------------------------
class TestQueueResilience:
    async def test_a_corrupt_payload_cannot_stall_the_drain(self) -> None:
        """Unparseable jobs are consumed, so valid work behind them still runs."""

        class _FakeRedis:
            def __init__(self) -> None:
                self.pops = 0

            async def lpop(self, _name: str):
                self.pops += 1
                if self.pops == 1:
                    return "not json at all"
                return json.dumps(make_job(kind="event", payload={"project_id": "p"}))

        queue = IngestionQueue("argus:test:corrupt")
        queue._redis = _FakeRedis()

        job = await queue.pop(timeout=0)

        assert job is not None, "the valid job behind the corrupt one must be returned"
        assert job["kind"] == "event"
        assert queue._redis.pops == 2

    async def test_a_permanent_failure_is_dead_lettered_without_retrying(
        self, db_engine
    ) -> None:
        """A job that can never succeed must not be re-enqueued to burn the queue."""
        factory = _session_factory(db_engine)

        class _RecordingQueue:
            def __init__(self) -> None:
                self.pushes: list[dict] = []

            async def push(self, job: dict) -> None:
                self.pushes.append(job)

        async def _process(*, kind: str, payload: dict) -> None:
            raise PermanentJobError("the project this job references no longer exists")

        worker = IngestionWorker(factory, process=_process, max_retries=3)
        worker._queue = _RecordingQueue()  # type: ignore[assignment]

        await worker._handle(
            make_job(kind="event", payload={"project_id": str(uuid.uuid4())})
        )

        assert worker._queue.pushes == [], "a permanent failure must not be retried"
        async with factory() as session:
            rows = list((await session.scalars(select(IngestionFailure))).all())
        assert len(rows) == 1
        assert rows[0].error_type == "PermanentJobError"

    async def test_a_dead_letter_survives_its_own_project_being_deleted(
        self, db_engine
    ) -> None:
        """The failure record must outlive the entity it complains about.

        An FK-bound ``project_id`` would make the dead-letter write itself fail
        when the project is gone — which is a common cause of the failure — so
        the job's fate would go unrecorded.
        """
        factory = _session_factory(db_engine)
        worker = IngestionWorker(factory, process=_noop_process)

        await worker._dead_letter(
            "event",
            {"project_id": str(uuid.uuid4()), "source_id": "src-1"},
            3,
            RuntimeError("ingestion failed"),
        )

        async with factory() as session:
            row = await session.scalar(select(IngestionFailure))
        assert row is not None
        assert row.project_id is None, "a dangling reference must not be recorded"
        assert row.error_type == "RuntimeError"
        assert row.retry_count == 3
        assert row.payload_summary, "the job's identity must survive in redacted form"

    async def test_an_exhausted_transient_failure_is_retried_then_dead_lettered(
        self, db_engine
    ) -> None:
        """Backoff retries are bounded: max_retries, then one dead-letter row."""
        factory = _session_factory(db_engine)

        class _RecordingQueue:
            def __init__(self) -> None:
                self.pushes: list[dict] = []

            async def push(self, job: dict) -> None:
                self.pushes.append(job)

        async def _process(*, kind: str, payload: dict) -> None:
            raise RuntimeError("transient")

        worker = IngestionWorker(factory, process=_process, max_retries=1)
        worker._queue = _RecordingQueue()  # type: ignore[assignment]

        await worker._handle(
            make_job(kind="event", payload={"project_id": str(uuid.uuid4())})
        )

        assert len(worker._queue.pushes) == 1
        assert worker._queue.pushes[0]["_retries"] == 1

        # The re-enqueued job is on its last budget: a second failure is final.
        await worker._handle(worker._queue.pushes[0])
        async with factory() as session:
            rows = list((await session.scalars(select(IngestionFailure))).all())
        assert len(rows) == 1
        assert rows[0].retry_count == 1


# ---------------------------------------------------------------------------
# 3. Provider transport failures, one layer below the AIDebugger contract
# ---------------------------------------------------------------------------
class _ScriptedTransport(httpx.AsyncBaseTransport):
    def __init__(self, responder) -> None:
        self._responder = responder
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(len(self.requests))


def _provider_through(monkeypatch, transport: _ScriptedTransport, retries: int = 2):
    """Build a real provider whose HTTP layer is a scripted transport."""
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    return OpenAICompatibleProvider(
        api_key="test-key",
        base_url="https://provider.invalid/v1",
        model="test-model",
        timeout=1,
        retries=retries,
    )


def _json_response(body: dict) -> httpx.Response:
    return httpx.Response(200, json=body)


class TestProviderTransportFailures:
    async def test_an_http_error_is_retried_then_surfaced(self, monkeypatch) -> None:
        transport = _ScriptedTransport(
            lambda _n: httpx.Response(500, text="upstream exploded")
        )
        provider = _provider_through(monkeypatch, transport)

        with pytest.raises(RuntimeError) as caught:
            await provider.complete_structured("hello", {})

        assert len(transport.requests) == 3, "retries must be bounded, not infinite"
        assert "500" in str(caught.value)

    async def test_a_malformed_response_body_is_retried_then_surfaced(
        self, monkeypatch
    ) -> None:
        """A 200 with a non-JSON body is a failure, not an empty answer."""
        transport = _ScriptedTransport(
            lambda _n: httpx.Response(200, text="<html>oops")
        )
        provider = _provider_through(monkeypatch, transport)

        with pytest.raises(RuntimeError):
            await provider.complete_structured("hello", {})

        assert len(transport.requests) == 3

    async def test_a_response_with_no_choices_is_an_error(self, monkeypatch) -> None:
        """An empty completion must not be treated as 'the model said nothing'."""
        transport = _ScriptedTransport(lambda _n: _json_response({"choices": []}))
        provider = _provider_through(monkeypatch, transport)

        with pytest.raises(RuntimeError) as caught:
            await provider.complete("hello")

        assert "no choices" in str(caught.value)

    async def test_non_json_message_content_reaches_the_caller(
        self, monkeypatch
    ) -> None:
        """Structured mode must reject prose rather than parse it loosely."""
        transport = _ScriptedTransport(
            lambda _n: _json_response(
                {"choices": [{"message": {"content": "I think the bug is here"}}]}
            )
        )
        provider = _provider_through(monkeypatch, transport)

        with pytest.raises(ValueError):
            await provider.complete_structured("hello", {})

    async def test_a_timeout_is_bounded_and_surfaced(self, monkeypatch) -> None:
        def _timeout(_n: int) -> httpx.Response:
            raise httpx.ReadTimeout("provider timed out")

        transport = _ScriptedTransport(_timeout)
        provider = _provider_through(monkeypatch, transport, retries=1)

        with pytest.raises(RuntimeError) as caught:
            await provider.complete("hello")

        assert len(transport.requests) == 2
        assert "timed out" in str(caught.value)

    async def test_a_successful_call_records_usage(self, monkeypatch) -> None:
        """The deterministic path is unaffected — a good call still works."""
        transport = _ScriptedTransport(
            lambda _n: _json_response(
                {
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"total_tokens": 42},
                }
            )
        )
        provider = _provider_through(monkeypatch, transport)

        assert await provider.complete_structured("hello", {}) == {"ok": True}
        assert provider.last_usage == {"total_tokens": 42}


# ---------------------------------------------------------------------------
# 4. Request-contract abuse: clean refusals, never a stack trace
# ---------------------------------------------------------------------------
def _assert_clean(response, expected: set[int]) -> None:
    assert response.status_code in expected, response.text
    lowered = response.text.lower()
    assert "traceback" not in lowered
    assert 'file "' not in lowered
    assert "sqlalchemy" not in lowered


class TestRequestContractAbuse:
    def test_a_declared_oversized_body_is_refused_with_413(self) -> None:
        """The refusal happens on the header, before the body is read."""
        tiny = FastAPI()

        @tiny.post("/echo")
        async def echo(payload: dict) -> dict:
            return payload

        tiny.add_middleware(BodySizeLimitMiddleware, max_bytes=1024)
        with TestClient(tiny) as client:
            response = client.post(
                "/echo",
                content=b'{"pad": "' + b"x" * 8192 + b'"}',
                headers={"content-type": "application/json"},
            )

        assert response.status_code == 413
        assert response.json()["detail"]

    def test_malformed_json_is_a_clean_validation_error(self, client) -> None:
        response = client.post(
            "/api/v1/projects",
            content=b'{"name": "broken", ',
            headers={"content-type": "application/json"},
        )
        _assert_clean(response, {400, 422})

    def test_a_wrong_content_type_is_a_clean_validation_error(self, client) -> None:
        response = client.post(
            "/api/v1/projects",
            data={"name": "form", "slug": "form-encoded"},
        )
        _assert_clean(response, {400, 415, 422})

    def test_an_oversized_single_field_is_refused(self, client) -> None:
        response = client.post(
            "/api/v1/projects",
            json={"name": "x" * 5000, "slug": "oversized-name"},
        )
        _assert_clean(response, {422})

    def test_a_deeply_nested_payload_is_refused_before_it_is_parsed(
        self, client
    ) -> None:
        """Depth is a property of the bytes, so it is judged on them.

        Without this guard a body nested ~100k levels deep raises
        ``RecursionError`` inside ``json.loads`` — an unhandled 500 that costs
        the attacker a few kilobytes to produce.
        """
        nested: dict = {"leaf": True}
        for _ in range(20_000):
            nested = {"next": nested}
        response = client.post(
            "/api/v1/projects",
            content=json.dumps(
                {"name": "nested", "slug": "deeply-nested", "metadata": nested}
            ).encode(),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 413
        assert response.json()["error_code"] == "PAYLOAD_TOO_DEEP"

    def test_a_legitimately_nested_payload_still_works(self, client) -> None:
        """The guard must not be a tax on ordinary payloads."""
        response = client.post(
            "/api/v1/projects",
            json={
                "name": "nested-ok",
                "slug": "nested-within-limits",
                "metadata": {"a": {"b": {"c": [1, {"d": 2}]}}},
            },
        )
        assert response.status_code == 201

    def test_a_deep_brace_inside_a_string_is_not_depth(self, client) -> None:
        """Braces in a string value are text, not structure."""
        response = client.post(
            "/api/v1/projects",
            json={
                "name": "string-braces",
                "slug": "string-braces",
                "description": "{" * 500 + "}" * 500,
            },
        )
        assert response.status_code == 201

    def test_an_unexpected_json_type_is_refused(self, client) -> None:
        response = client.post(
            "/api/v1/projects",
            content=b"[1, 2, 3]",
            headers={"content-type": "application/json"},
        )
        _assert_clean(response, {400, 422})

    def test_a_traversal_shaped_id_is_refused(self, client) -> None:
        response = client.get("/api/v1/projects/../../etc/passwd")
        assert response.status_code in {404, 422}

    def test_an_unknown_route_is_a_clean_404(self, client) -> None:
        response = client.get("/api/v1/this-route-does-not-exist")
        _assert_clean(response, {404})


# ---------------------------------------------------------------------------
# 5. Verification integrity: an unproven baseline is never a pass
# ---------------------------------------------------------------------------
class TestJsonDepthScanner:
    """The scanner itself — it is the guard, so it gets its own tests."""

    def test_flat_document_has_depth_one(self) -> None:
        assert json_nesting_depth(b'{"a": 1}') == 1

    def test_nested_containers_count(self) -> None:
        assert json_nesting_depth(b'{"a": {"b": [[1]]}}') == 4

    def test_braces_inside_strings_do_not_count(self) -> None:
        assert json_nesting_depth(b'{"a": "{{{{{"}') == 1

    def test_escaped_quotes_do_not_end_the_string(self) -> None:
        assert json_nesting_depth(b'{"a": ""}}"}') == 1

    def test_malformed_input_returns_the_depth_reached(self) -> None:
        assert json_nesting_depth(b'{"a": {"b": ') == 2

    def test_depth_is_counted_for_a_hostile_body_without_recursing(self) -> None:
        """The scan is iterative, so depth 200k is a number, not a crash."""
        assert json_nesting_depth(b"[" * 200_000) == 200_000

    def test_the_middleware_exempts_non_json_content_types(self) -> None:
        """A binary body must not be judged by JSON rules."""
        tiny = FastAPI()

        @tiny.post("/otlp")
        async def otlp() -> dict:
            return {"ok": True}

        tiny.add_middleware(JsonDepthLimitMiddleware, max_depth=2)
        with TestClient(tiny) as client:
            response = client.post(
                "/otlp",
                content=b"[[[[[[[[",
                headers={"content-type": "application/x-protobuf"},
            )
        assert response.status_code == 200


class TestVerificationIntegrity:
    def test_verification_without_a_reproduced_baseline_is_not_verified(self) -> None:
        """No baseline reproduction ⇒ NOT_VERIFIED, and the workspace is untouched.

        ``workspace=None`` is deliberate: the engine must refuse *before* it
        reaches for one, so a missing precondition can never be papered over by
        a leftover sandbox.
        """
        outcome = PatchVerificationEngine().verify(
            workspace=None,  # type: ignore[arg-type]
            parsed=None,  # type: ignore[arg-type]
            patch_diff="",
            scope_files=[],
            baseline_reproduced=False,
        )

        assert outcome.status == "NOT_VERIFIED"
        assert outcome.level == "NONE"
        assert outcome.confidence == "LOW"
        assert "reproduced" in outcome.verdict_reason
        assert outcome.evidence["applied"] is False


class TestProposalWriteRace:
    """A duplicate proposal is an outcome, not a failure (hardening W4b).

    ``RemediationService.propose`` checks ``_is_duplicate`` before inserting,
    and a check is not a guarantee: the remediation sweep and the platform
    sweep hold *different* leases and both propose actions, so the row that
    satisfies the check can be written by the other one in between. The unique
    constraint ``(project_id, fingerprint, attempt)`` is the authority — and
    when it fires, the correct reading is "this remediation is already
    proposed", not "the sweep failed".

    This was found in the running stack: the API logged
    ``remediation sweep failed for project …: duplicate key value violates
    unique constraint "uq_remediation_actions_project_fingerprint_attempt"``
    while the platform was perfectly healthy. Without a savepoint the poisoned
    transaction also discards the rest of that project's work, which is why the
    catch alone would not have been enough.
    """

    async def test_a_fingerprint_already_in_the_batch_is_proposed_once(
        self, db_session
    ) -> None:
        from app.services.remediation_service import RemediationService
        from tests.phase6_helpers import build_project
        from tests.phase9_helpers import make_draft

        project, environment, component = await build_project(db_session)
        first = make_draft(project, environment, component)
        #: A second draft from a *different* incident that resolves to the same
        #: mechanism — exactly what a batch built from two incidents looks like.
        second = make_draft(project, environment, component)
        assert first.fingerprint == second.fingerprint

        created = await RemediationService(db_session).propose([first, second])
        assert len(created) == 1

    async def test_a_collision_the_check_missed_does_not_poison_the_session(
        self, db_session, monkeypatch
    ) -> None:
        from app.models.remediation import RemediationAction
        from app.services.remediation_service import RemediationService
        from tests.phase6_helpers import build_project
        from tests.phase9_helpers import make_draft

        project, environment, component = await build_project(db_session)
        drafts = [
            make_draft(project, environment, component),
            make_draft(project, environment, component),
        ]

        service = RemediationService(db_session)

        #: Simulate the race: the dedup check cannot see the row that is about
        #: to collide. (In production the other sweep wrote it microseconds
        #: earlier; the check and the insert are not one operation.)
        async def _blind(*args, **kwargs) -> bool:
            return False

        monkeypatch.setattr(service, "_is_duplicate", _blind)
        created = await service.propose(drafts)

        assert len(created) == 1, "the constraint must win over the check"
        #: The session is still usable afterwards. Without the SAVEPOINT this
        #: raises ``PendingRollbackError`` and every later write in the pass is
        #: lost, which is how a duplicate ends up reported as a project failure.
        rows = (await db_session.execute(select(RemediationAction))).scalars().all()
        assert len(rows) == 1
