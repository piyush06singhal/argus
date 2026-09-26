"""Shared token-bucket rate limiting (hardening: multi-replica correctness).

The limiter used to be a ``dict`` of buckets inside one process. That is correct
for exactly one API process and quietly wrong for more: with ``N`` replicas the
effective ceiling is ``N × RATE_LIMIT_PER_MINUTE``, and a client that has
saturated one replica is served by the next one it lands on. Nothing in the
system said so — the ceiling was documented as per-worker, which is honest but
still not a ceiling. This module makes it a real ceiling when Redis is available:

* **Redis backend** — an atomic Lua token bucket keyed by credential. Every
  replica refills and debits the *same* bucket, so the policy holds across a
  fleet. INCR-based fixed windows were rejected because they allow a 2× burst
  across a window boundary; a bucket preserves the shipped semantics exactly.
* **Memory backend** — the original per-process bucket, kept because it is the
  right answer for a single process and the fallback when Redis is gone.

**Degradation is a choice, and it is visible.** If a Redis call fails while the
Redis backend is configured, the request is still served — through the
per-process bucket — and the fallback is counted
(``argus_rate_limit_fallbacks_total``) and reflected in
``argus_rate_limit_backend``. Failing closed would turn a Redis outage into a
total API outage, which converts a soft ceiling into a hard one; serving while
saying exactly what happened keeps the platform up and keeps the operator
informed. What must never happen is a silent downgrade.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import weakref
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from app.core.runtime_metrics import gauge, incr

logger = logging.getLogger("argus.edge.rate_limit")

#: Keyed refill + debit, one round trip, no read-modify-write race between
#: replicas. Returns ``{allowed, tokens_remaining}``.
#:
#: ``ts`` is wall-clock milliseconds rather than a TTL-only window, because
#: replicas must agree on how much time has passed; ``EXPIRE`` is set on every
#: write so an idle credential's bucket disappears on its own.
_LUA_TOKEN_BUCKET = """
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now_ms
end
local elapsed = (now_ms - ts) / 1000.0
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + (elapsed * refill))
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now_ms)
redis.call('EXPIRE', KEYS[1], ttl)
return {allowed, math.floor(tokens * 1000)}
"""

_BACKEND_GAUGE = {
    "help_text": (
        "1 when rate limiting is shared across replicas, 0 when it is per-process"
    ),
}
_FALLBACK_HELP = {
    "help_text": (
        "Requests limited per-process because the shared limiter was unreachable"
    ),
}


class TokenBucket:
    """Classic token bucket: refill per second, allow bursts.

    The single-process primitive. Exposed (as ``_TokenBucket``) because the
    middleware suite pins its refill behaviour directly.
    """

    __slots__ = ("tokens", "capacity", "refill_per_second", "updated_at")

    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.tokens = float(capacity)
        self.updated_at = time.monotonic()

    def try_take(self, amount: float = 1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(
            self.capacity,
            self.tokens + (now - self.updated_at) * self.refill_per_second,
        )
        self.updated_at = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False


def retry_after_seconds(tokens: float, refill_per_second: float) -> int:
    """Seconds until one token exists again.

    The bucket refills continuously, so this is derived from the *actual*
    deficit: a constant 1 both overstates a fast refill and understates a slow
    one, and a client that acts on it would either give up capacity or retry
    straight back into a 429.
    """
    if tokens >= 1:
        return 1
    return max(1, math.ceil((1.0 - tokens) / max(refill_per_second, 1e-9)))


@dataclass(frozen=True)
class Decision:
    """The limiter's verdict for one request."""

    allowed: bool
    remaining: int
    #: ``redis`` or ``memory`` — recorded per decision, not per process, so a
    #: single request's degradation is attributable in a log line.
    backend: str
    retry_after: int = 1


class Limiter(Protocol):
    """What the middleware needs; both backends satisfy it."""

    name: str

    async def acquire(self, key: str) -> Decision:  # pragma: no cover - protocol
        ...

    async def close(self) -> None:  # pragma: no cover - protocol
        ...


class MemoryLimiter:
    """Per-process buckets, bounded by an idle sweep.

    The sweep matters under source-IP spoofing through a proxy: every distinct
    key costs one small object, so buckets that have refilled and gone quiet are
    dropped rather than accumulating for the life of the process.
    """

    name = "memory"

    def __init__(self, *, per_minute: int, burst: int) -> None:
        self.capacity = max(burst, 1)
        self.refill = per_minute / 60.0
        self._buckets: dict[str, TokenBucket] = {}
        self._last_sweep = time.monotonic()

    def _sweep(self) -> None:
        now = time.monotonic()
        if now - self._last_sweep < 300:
            return
        self._last_sweep = now
        stale = [
            key
            for key, bucket in self._buckets.items()
            if bucket.tokens >= bucket.capacity and now - bucket.updated_at > 600
        ]
        for key in stale:
            self._buckets.pop(key, None)

    def acquire_sync(self, key: str) -> Decision:
        self._sweep()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(self.capacity, self.refill)
            self._buckets[key] = bucket
        allowed = bucket.try_take()
        return Decision(
            allowed=allowed,
            remaining=int(bucket.tokens) if allowed else 0,
            backend="memory",
            retry_after=retry_after_seconds(bucket.tokens, self.refill),
        )

    async def acquire(self, key: str) -> Decision:
        return self.acquire_sync(key)

    async def close(self) -> None:
        return None


class RedisLimiter:
    """One shared bucket per key, debited atomically by every replica."""

    name = "redis"

    def __init__(
        self,
        *,
        per_minute: int,
        burst: int,
        redis_url: str,
        key_prefix: str = "argus:rl:",
    ) -> None:
        self.capacity = max(burst, 1)
        self.refill = per_minute / 60.0
        self.redis_url = redis_url
        self.key_prefix = key_prefix
        #: **One client per event loop, not one per limiter.** A redis-py async
        #: client holds futures bound to the loop that created it, so caching a
        #: single client across loops fails the moment the loop changes — which
        #: happens on a reload, in some servers, and in every test that drives
        #: the middleware through a portal. Keying by the running loop makes the
        #: limiter loop-agnostic instead of accidentally single-loop. A
        #: ``WeakKeyDictionary`` means a finished loop's connection is dropped
        #: with the loop rather than leaking.
        self._clients: weakref.WeakKeyDictionary[Any, tuple[Any, Any]] = (
            weakref.WeakKeyDictionary()
        )
        #: Per-process fallback, used only while Redis is unreachable. The
        #: ceiling degrades; it does not vanish.
        self._fallback = MemoryLimiter(per_minute=per_minute, burst=burst)
        self._last_warning = 0.0
        self._unavailable = False

    def _clients_for_loop(self) -> tuple[Any, Any]:
        """The (client, registered script) pair for the running event loop."""
        loop = asyncio.get_running_loop()
        entry = self._clients.get(loop)
        if entry is None:
            import redis.asyncio as aioredis

            client = aioredis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.25,
                socket_timeout=0.25,
                health_check_interval=30,
            )
            entry = (client, client.register_script(_LUA_TOKEN_BUCKET))
            self._clients[loop] = entry
        return entry

    def _ttl_seconds(self) -> int:
        """Long enough that an idle bucket's refill is never lost."""
        idle = self.capacity / max(self.refill, 1e-9)
        return int(max(60.0, idle * 2.0))

    async def acquire(self, key: str) -> Decision:
        try:
            _client, script = self._clients_for_loop()
            allowed, remaining_milli = await script(
                keys=[f"{self.key_prefix}{key}"],
                args=[
                    self.capacity,
                    self.refill,
                    int(time.time() * 1000),
                    self._ttl_seconds(),
                ],
            )
        except Exception as exc:  # noqa: BLE001 - any Redis failure degrades
            self._note_unavailable(exc)
            return self._fallback.acquire_sync(key)

        if self._unavailable:
            self._note_recovered()
        #: Millionths of a token, so the retry hint is not rounded to a whole
        #: token and wrong by up to a second.
        tokens = int(remaining_milli) / 1000.0
        allowed_bool = bool(int(allowed))
        return Decision(
            allowed=allowed_bool,
            remaining=int(tokens) if allowed_bool else 0,
            backend="redis",
            retry_after=retry_after_seconds(tokens, self.refill),
        )

    def _note_unavailable(self, exc: BaseException) -> None:
        incr("argus_rate_limit_fallbacks_total", amount=1, **_FALLBACK_HELP)
        gauge("argus_rate_limit_backend", 0, **_BACKEND_GAUGE)
        self._unavailable = True
        #: Throttled: an outage would otherwise log once per request and bury
        #: the rest of the log in the noise it is supposed to explain.
        now = time.monotonic()
        if now - self._last_warning > 60:
            self._last_warning = now
            logger.warning(
                "shared rate limiter unavailable (%s); limiting per-process "
                "until it recovers — the effective ceiling is now per-replica",
                type(exc).__name__,
            )

    def _note_recovered(self) -> None:
        self._unavailable = False
        gauge("argus_rate_limit_backend", 1, **_BACKEND_GAUGE)
        logger.info("shared rate limiter recovered; the ceiling is shared again")

    async def close(self) -> None:
        clients = [client for client, _ in self._clients.values()]
        self._clients.clear()
        for client in clients:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass


def build_limiter(
    *,
    backend: str,
    per_minute: int,
    burst: int,
    redis_url: Optional[str] = None,
    key_prefix: str = "argus:rl:",
) -> Limiter:
    """Choose a backend.

    ``auto`` (the default) prefers Redis and falls back to memory when no Redis
    URL is configured, so a single-container deployment needs no decision and a
    fleet gets the shared ceiling without being told to ask for it.
    """
    #: Registered at construction, so the series exists from the first scrape.
    #: A counter at zero is a true statement ("nothing has fallen back yet"),
    #: unlike a *timestamp* at zero, which would mean 1970 — which is why the
    #: backup series are absent rather than zeroed and these are not. It also
    #: makes `increase(...)` and an alert on it well-defined before the first
    #: fallback ever happens.
    incr("argus_rate_limit_fallbacks_total", amount=0, **_FALLBACK_HELP)

    wants_redis = backend.strip().lower() in {"redis", "auto"}
    if wants_redis and redis_url:
        gauge("argus_rate_limit_backend", 1, **_BACKEND_GAUGE)
        return RedisLimiter(
            per_minute=per_minute,
            burst=burst,
            redis_url=redis_url,
            key_prefix=key_prefix,
        )
    if backend.strip().lower() == "redis" and not redis_url:
        logger.warning(
            "RATE_LIMIT_BACKEND=redis but no REDIS_URL is configured; "
            "limiting per-process (the ceiling is per-replica)"
        )
    gauge("argus_rate_limit_backend", 0, **_BACKEND_GAUGE)
    return MemoryLimiter(per_minute=per_minute, burst=burst)
