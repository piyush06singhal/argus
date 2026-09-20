"""ARGUS Test Configuration."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Ensure we can import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

# Use a file-backed SQLite DB for tests so connections across event loops
# (TestClient runs the app on its own loop) share the same data.
_test_db_path = tempfile.mktemp(suffix=".argus_test.db")
TEST_DATABASE_URL = f"sqlite+aiosqlite:///{_test_db_path}"

os.environ["API_ENVIRONMENT"] = "test"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from app.core.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402


def _cleanup_test_db() -> None:
    try:
        os.remove(_test_db_path)
    except FileNotFoundError:
        pass


@pytest.fixture(scope="session", autouse=True)
def _session_cleanup():
    yield
    _cleanup_test_db()


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def db_engine():
    """Create a file-backed SQLite database engine shared across the session."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(db_engine):
    """Delete all rows before each test to ensure isolation."""
    async with db_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(text(f"DELETE FROM {table.name}"))
    yield


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create a test database session."""
    session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session


@pytest.fixture
def client(db_engine) -> Generator[TestClient, None, None]:
    """Create a FastAPI test client wired to the test database."""

    session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
