"""Sweep leadership against real PostgreSQL (hardening — multi-worker scaling).

The claim this proves: with N worker processes, **one** runs each sweep per
interval, and a worker that dies frees the lease instead of wedging the fleet.
Both halves matter — the second is why an advisory lock was chosen over a
``leader`` row with a heartbeat, and it is the half that is easy to get wrong.

Skipped on SQLite, where the lease is deliberately a no-op because there is no
second process to coordinate with. The proof therefore only exists where the
mechanism does, which is what ``ARGUS_TEST_DB`` provides in CI.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.services.sweep_leader import SWEEP_NAMES, advisory_key, sweep_lease


def _require_postgres() -> str:
    url = os.getenv("ARGUS_TEST_DB") or str(get_settings().DATABASE_URL)
    if not url.startswith("postgresql"):
        pytest.skip(
            "advisory locks exist only on PostgreSQL; set ARGUS_TEST_DB to a "
            "postgresql+asyncpg:// URL (the Integration workflow does) to run "
            "the leadership proofs"
        )
    return url


@pytest.fixture
async def pg_factory():
    engine = create_async_engine(_require_postgres(), future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


class TestKeys:
    def test_every_sweep_has_its_own_key(self) -> None:
        """A shared key would make two sweeps mutually exclusive — silent, and
        the kind of bug that looks like 'that sweep just never runs'."""
        keys = {name: advisory_key(name) for name in SWEEP_NAMES}
        assert len(set(keys.values())) == len(SWEEP_NAMES)

    def test_keys_are_stable_and_in_range(self) -> None:
        # Stability matters: a key that changed between releases would let two
        # versions of ARGUS sweep simultaneously during a rolling upgrade.
        assert advisory_key("anomaly") == advisory_key("anomaly")
        for name in SWEEP_NAMES:
            key = advisory_key(name)
            assert -(2**63) <= key < 2**63


class TestLease:
    async def test_only_one_holder_at_a_time(self, pg_factory) -> None:
        """The whole point: a second process must be refused the tick."""
        async with sweep_lease(pg_factory, "anomaly") as first:
            assert first is True
            async with sweep_lease(pg_factory, "anomaly") as second:
                assert second is False, (
                    "two processes held the same sweep lease simultaneously; "
                    "a multi-worker deployment would duplicate every pass"
                )
        # Released on exit, so the next interval is available again.
        async with sweep_lease(pg_factory, "anomaly") as again:
            assert again is True

    async def test_different_sweeps_do_not_block_each_other(self, pg_factory) -> None:
        async with sweep_lease(pg_factory, "anomaly") as anomaly:
            assert anomaly is True
            async with sweep_lease(pg_factory, "remediation") as remediation:
                assert (
                    remediation is True
                ), "holding one sweep's lease must not stop an unrelated sweep"

    async def test_a_concurrent_race_produces_exactly_one_leader(
        self, pg_factory
    ) -> None:
        """Four workers waking at the same instant → one runs the pass."""

        async def attempt() -> bool:
            async with sweep_lease(pg_factory, "remediation") as leader:
                if not leader:
                    return False
                # Hold the lease briefly, as a real pass would, so the others
                # genuinely overlap rather than racing to a release.
                await asyncio.sleep(0.2)
                return True

        outcomes = await asyncio.gather(*(attempt() for _ in range(4)))
        assert outcomes.count(True) == 1, outcomes

    async def test_a_dead_worker_releases_its_lease(self, pg_factory) -> None:
        """No heartbeat, no expiry: PostgreSQL drops the lock with the session.

        This is the property that makes a crashed worker a non-event. It is
        proved by taking the lock from a *different* connection and closing that
        connection without unlocking, then confirming the lease is obtainable.
        """
        key = advisory_key("platform")
        engine = create_async_engine(_require_postgres(), future=True)
        connection = await engine.connect()
        try:
            got = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            )
            assert got is True
            # The lease is genuinely held while that session lives.
            async with sweep_lease(pg_factory, "platform") as leader:
                assert leader is False
        finally:
            # Simulate the process dying: close without unlocking.
            await connection.close()
            await engine.dispose()

        async with sweep_lease(pg_factory, "platform") as after_death:
            assert after_death is True, (
                "a lease outlived the session that held it; a crashed worker "
                "would wedge the sweep for every other replica"
            )

    async def test_the_lease_can_be_disabled(self, pg_factory) -> None:
        """For a deployment that coordinates elsewhere (or a single process)."""
        async with sweep_lease(pg_factory, "learning", enabled=False) as leader:
            assert leader is True
            async with sweep_lease(pg_factory, "learning", enabled=False) as second:
                assert second is True, "a disabled lease must not gate anything"
