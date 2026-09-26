"""Hardening W10 — the shared rate limiter (multi-replica correctness).

Two kinds of test, and the distinction is stated rather than blurred:

* the **fake-broker** tests pin the Python side — decisions, degradation
  counting, headers, backend choice. They run a Python re-implementation of the
  bucket, so they prove *nothing* about the Lua script;
* the **Redis-gated** tests run the real script against a real server, and they
  are the only evidence that the ceiling is genuinely shared and that
  concurrent debits cannot over-admit. They skip loudly when no broker is
  listening, rather than passing by accident.

The claim under test is narrow and specific: with ``N`` replicas the effective
ceiling used to be ``N × RATE_LIMIT_PER_MINUTE``, because each process kept its
own dict. It is now one ceiling, and a downgrade to per-process limiting is
counted instead of silent.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from typing import Any

import pytest

from app.core import runtime_metrics
from app.core.rate_limit import (
    MemoryLimiter,
    RedisLimiter,
    build_limiter,
    retry_after_seconds,
)


def _broker_reachable() -> bool:
    """Whether a Redis broker is listening, checked without importing redis."""
    from app.core.config import get_settings

    settings = get_settings()
    try:
        with socket.create_connection(
            (settings.REDIS_HOST, settings.REDIS_PORT), timeout=1.0
        ):
            return True
    except OSError:
        return False


REDIS_GATED = pytest.mark.skipif(
    not _broker_reachable(),
    reason="no Redis broker listening (start one: docker compose up -d redis)",
)


@pytest.fixture(autouse=True)
def _clean_runtime_metrics():
    """Process-local counters are global; a test must not inherit another's."""
    runtime_metrics.reset()
    yield
    runtime_metrics.reset()


# ---------------------------------------------------------------------------
# Fake-broker seam: exercises the Python logic, not the Lua script
# ---------------------------------------------------------------------------
class _FakeScript:
    """The bucket arithmetic of the Lua script, in Python, over a shared dict.

    A shared dict is the point: two :class:`RedisLimiter` instances pointed at
    the same fake broker have to behave like two replicas against one Redis.
    """

    def __init__(self, store: dict[str, tuple[float, float]]) -> None:
        self._store = store

    async def __call__(self, *, keys: list[str], args: list[Any]) -> list[int]:
        key = keys[0]
        capacity = float(args[0])
        refill = float(args[1])
        now_ms = float(args[2])
        tokens, updated = self._store.get(key, (capacity, now_ms))
        elapsed = max(0.0, (now_ms - updated) / 1000.0)
        tokens = min(capacity, tokens + elapsed * refill)
        allowed = 0
        if tokens >= 1:
            tokens -= 1
            allowed = 1
        self._store[key] = (tokens, now_ms)
        return [allowed, int(tokens * 1000)]


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, tuple[float, float]] = {}

    def register_script(self, _lua: str) -> _FakeScript:
        return _FakeScript(self.store)

    async def aclose(self) -> None:
        return None


class _DownScript:
    """Every call fails, the way an unreachable broker fails."""

    async def __call__(self, **_kwargs: Any) -> list[int]:
        raise ConnectionError("redis is down")


def _install_fake(limiter: RedisLimiter, fake: _FakeRedis) -> None:
    """Point every loop at this broker.

    The limiter caches one client per event loop (an async client belongs to the
    loop that made it), so injecting is keyed the same way.
    """
    limiter._clients = _always(fake, fake.register_script(""))


def _install_down(limiter: RedisLimiter) -> None:
    limiter._clients = _always(object(), _DownScript())


class _AlwaysClients(dict):
    """A cache that answers for any loop with the injected pair."""

    def __init__(self, entry: tuple[Any, Any]) -> None:
        super().__init__()
        self._entry = entry

    def get(self, key: Any, default: Any = None) -> Any:
        return self._entry

    def __getitem__(self, key: Any) -> Any:
        return self._entry

    def values(self):  # type: ignore[override]
        return [self._entry]


def _always(client: Any, script: Any) -> _AlwaysClients:
    return _AlwaysClients((client, script))


class TestSharedCeilingLogic:
    async def test_two_replicas_with_one_bucket_enforce_one_ceiling(self):
        fake = _FakeRedis()
        # Two limiters, one broker: the same shape as two API processes.
        replica_a = RedisLimiter(per_minute=60, burst=3, redis_url="redis://fake")
        replica_b = RedisLimiter(per_minute=60, burst=3, redis_url="redis://fake")
        _install_fake(replica_a, fake)
        _install_fake(replica_b, fake)

        decisions = []
        for index in range(10):
            limiter = replica_a if index % 2 == 0 else replica_b
            decisions.append(await limiter.acquire("t:shared"))

        allowed = [d for d in decisions if d.allowed]
        assert len(allowed) == 3, (
            "burst is the ceiling for the *fleet*: alternating between two "
            "replicas must not buy a second bucket"
        )
        assert {d.backend for d in decisions} == {"redis"}

    async def test_unreachable_shared_limiter_still_serves_and_says_so(self):
        limiter = RedisLimiter(per_minute=60, burst=2, redis_url="redis://gone")
        _install_down(limiter)

        decision = await limiter.acquire("t:degraded")

        assert decision.allowed is True, "a broker outage must not stop traffic"
        assert decision.backend == "memory", "the degradation is attributable"
        assert (
            runtime_metrics.snapshot()["argus_rate_limit_fallbacks_total"] == 1
        ), "a silent downgrade is the failure this counter exists to prevent"
        assert runtime_metrics.snapshot()["argus_rate_limit_backend"] == 0

    async def test_degraded_limiter_still_enforces_its_own_burst(self):
        limiter = RedisLimiter(per_minute=60, burst=2, redis_url="redis://gone")
        _install_down(limiter)

        decisions = [await limiter.acquire("t:same") for _ in range(5)]

        assert sum(1 for d in decisions if d.allowed) == 2, (
            "degrading to per-process limiting is a smaller ceiling, never no "
            "ceiling"
        )

    async def test_recovery_is_reported(self):
        limiter = RedisLimiter(per_minute=60, burst=2, redis_url="redis://gone")
        _install_down(limiter)
        await limiter.acquire("t:recovers")
        assert runtime_metrics.snapshot()["argus_rate_limit_backend"] == 0

        fake = _FakeRedis()
        _install_fake(limiter, fake)
        decision = await limiter.acquire("t:recovers")

        assert decision.backend == "redis"
        assert runtime_metrics.snapshot()["argus_rate_limit_backend"] == 1, (
            "the gauge must return to 1, or a recovered fleet still looks "
            "degraded forever"
        )


class TestBackendChoice:
    def test_auto_prefers_redis_and_needs_a_url(self):
        assert isinstance(
            build_limiter(
                backend="auto", per_minute=60, burst=10, redis_url="redis://x"
            ),
            RedisLimiter,
        )
        assert isinstance(
            build_limiter(backend="auto", per_minute=60, burst=10, redis_url=None),
            MemoryLimiter,
        )

    def test_memory_is_honoured_even_with_a_url(self):
        limiter = build_limiter(
            backend="memory", per_minute=60, burst=10, redis_url="redis://x"
        )
        assert isinstance(limiter, MemoryLimiter)
        assert runtime_metrics.snapshot()["argus_rate_limit_backend"] == 0

    def test_redis_without_a_url_degrades_rather_than_crashing(self):
        limiter = build_limiter(
            backend="redis", per_minute=60, burst=10, redis_url=None
        )
        assert isinstance(limiter, MemoryLimiter)

    def test_instance_identity_is_always_exported(self):
        body = "\n".join(runtime_metrics.render_prometheus(version="0.1.0"))
        assert "argus_instance_info{" in body
        assert 'region="' in body
        assert 'version="0.1.0"' in body


class TestRetryAfter:
    def test_retry_after_reflects_the_real_deficit(self):
        # 1 token per second: one second buys the next token.
        assert retry_after_seconds(0.0, 1.0) == 1
        # Half a token per second: two seconds.
        assert retry_after_seconds(0.0, 0.5) == 2
        # A token and a half already banked: the next one is immediate.
        assert retry_after_seconds(1.5, 1.0) == 1
        # Never below one second: "retry in 0s" invites a tight retry loop.
        assert retry_after_seconds(0.0, 1000.0) == 1


class TestMiddlewareQuotaHeaders:
    def test_429_carries_retry_after_and_quota_headers(self):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from app.core.edge import RateLimitMiddleware

        async def ok(request):
            return PlainTextResponse("ok")

        application = Starlette(routes=[Route("/x", ok)])
        application.add_middleware(
            RateLimitMiddleware, per_minute=60, burst=2, enabled=True
        )
        client = TestClient(application)

        first = client.get("/x")
        assert first.status_code == 200
        assert first.headers["x-ratelimit-limit"] == "2"
        assert "x-ratelimit-remaining" in first.headers

        statuses = [client.get("/x").status_code for _ in range(4)]
        assert 429 in statuses
        blocked = next(
            response for response in (client.get("/x"),) if response.status_code == 429
        )
        assert blocked.headers["retry-after"] == "1"


# ---------------------------------------------------------------------------
# Redis-gated: the only tests that prove the Lua script
# ---------------------------------------------------------------------------
class TestRealRedis:
    def _limiter(self, *, burst: int, per_minute: int = 60) -> RedisLimiter:
        from app.core.config import get_settings

        return RedisLimiter(
            per_minute=per_minute,
            burst=burst,
            redis_url=get_settings().REDIS_URL,
            # Unique per test: these keys are real Redis keys, and a shared
            # namespace would make the suite order-dependent.
            key_prefix=f"argus:test:rl:{uuid.uuid4().hex}:",
        )

    @REDIS_GATED
    async def test_real_redis_shares_one_ceiling_between_replicas(self):
        first = self._limiter(burst=5)
        second = self._limiter(burst=5)
        # Same prefix: two processes, one bucket.
        second.key_prefix = first.key_prefix
        try:
            allowed = 0
            for index in range(12):
                limiter = first if index % 2 == 0 else second
                decision = await limiter.acquire("t:fleet")
                assert (
                    decision.backend == "redis"
                ), "the script must be the path taken, not the Python fallback"
                allowed += 1 if decision.allowed else 0
        finally:
            await self._cleanup(first, "t:fleet")
        assert allowed == 5

    @REDIS_GATED
    async def test_real_redis_cannot_over_admit_under_concurrency(self):
        """The race the old per-process dict could not have: 50 simultaneous
        debits against a burst of 10 must admit exactly 10.

        This is the property a read-then-write implementation loses, and the
        reason the refill is a single Lua script rather than GET/SET.
        """
        limiter = self._limiter(burst=10)
        try:
            decisions = await asyncio.gather(
                *(limiter.acquire("t:race") for _ in range(50))
            )
        finally:
            await self._cleanup(limiter, "t:race")
        assert sum(1 for d in decisions if d.allowed) == 10

    @REDIS_GATED
    async def test_real_redis_refills(self):
        limiter = self._limiter(burst=1, per_minute=600)  # 10 tokens/second
        try:
            assert (await limiter.acquire("t:refill")).allowed
            assert not (await limiter.acquire("t:refill")).allowed
            await asyncio.sleep(0.2)
            assert (await limiter.acquire("t:refill")).allowed
        finally:
            await self._cleanup(limiter, "t:refill")

    @REDIS_GATED
    async def test_bucket_expires_so_idle_credenials_do_not_accumulate(self):
        limiter = self._limiter(burst=3)
        try:
            await limiter.acquire("t:ttl")
            client = limiter._clients_for_loop()[0]
            ttl = await client.ttl(f"{limiter.key_prefix}t:ttl")
        finally:
            await self._cleanup(limiter, "t:ttl")
        assert ttl > 0, "an unexpiring bucket per credential is a memory leak"

    @REDIS_GATED
    async def test_middleware_on_real_redis_shares_the_ceiling(self):
        """End-to-end: two middleware instances, one broker, one ceiling."""
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from app.core.config import get_settings
        from app.core.edge import RateLimitMiddleware

        prefix = f"argus:test:rl:{uuid.uuid4().hex}:"

        async def ok(request):
            return PlainTextResponse("ok")

        def build() -> TestClient:
            application = Starlette(routes=[Route("/x", ok)])
            application.add_middleware(
                RateLimitMiddleware,
                per_minute=60,
                burst=4,
                enabled=True,
                backend="redis",
                redis_url=get_settings().REDIS_URL,
                key_prefix=prefix,
            )
            return TestClient(application)

        one, two = build(), build()
        try:
            statuses = [one.get("/x").status_code for _ in range(6)]
            statuses += [two.get("/x").status_code for _ in range(6)]
        finally:
            client = RedisLimiter(
                per_minute=60, burst=4, redis_url=get_settings().REDIS_URL
            )
            connection = client._clients_for_loop()[0]
            keys = await connection.keys(f"{prefix}*")
            if keys:
                await connection.delete(*keys)
            await client.close()

        assert (
            statuses.count(200) == 4
        ), "two replicas sharing a bucket admit the burst once, not once each"
        assert statuses.count(429) == 8

    async def _cleanup(self, limiter: RedisLimiter, key: str) -> None:
        connection = limiter._clients_for_loop()[0]
        await connection.delete(f"{limiter.key_prefix}{key}")
        await limiter.close()
