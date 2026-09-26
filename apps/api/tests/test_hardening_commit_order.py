"""Hardening — a write must be visible to the request that follows it.

FastAPI runs a dependency's teardown *after* the response has been sent, and
``get_db`` used to commit there and nowhere else. The consequence is a race that
is invisible in ordinary use and fatal to an automated one: a ``DELETE`` returns
``204`` and the very next ``GET`` still sees the row, because the deleting
transaction had not committed yet. Measured against the running stack, ten of
twelve immediate reads after a delete saw the deleted project — which is what
made the phase-10/11 gates and the project-delete cascade check fail
intermittently while asserting something else entirely.

``CommitBeforeResponseMiddleware`` closes that by committing the request's
session just before ``http.response.start`` is forwarded.

**Why the ordering is asserted against the middleware rather than only over the
API.** ``TestClient`` drains a response completely before the next request, so
it runs the teardown and cannot reproduce the race — an end-to-end test alone
would pass with or without the fix, which is worse than no test. So the ordering
*contract* is asserted directly: the middleware commits before it forwards the
response start, and it does not commit at all when the route raised. The
end-to-end tests below remain as the property, catching a regression that
removes the commit entirely.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.database import current_request_session
from app.core.edge import CommitBeforeResponseMiddleware


class _RecordingSession:
    """A stand-in for the request's ``AsyncSession`` that records its commits."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.commits = 0
        self._in_transaction = True

    def in_transaction(self) -> bool:
        return self._in_transaction

    async def commit(self) -> None:
        self.commits += 1
        self._in_transaction = False
        self.order.append("commit")


async def _no_receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


def test_the_middleware_is_registered_on_the_application() -> None:
    """The middleware only exists for the app if ``main.py`` installs it.

    The ordering tests below exercise it directly, so without this they would
    keep passing after the registration line was deleted — which is the actual
    regression this feature can suffer.
    """
    from app.main import app

    installed = [middleware.cls for middleware in app.user_middleware]
    assert CommitBeforeResponseMiddleware in installed, installed


class TestTheCommitHappensBeforeTheResponseIsSent:
    async def test_the_session_is_committed_before_the_first_byte(
        self,
    ) -> None:
        order: list[str] = []
        session = _RecordingSession(order)

        async def app(scope: Any, receive: Any, send: Any) -> None:
            #: What ``get_db`` does for the request being served.
            current_request_session.set(session)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        async def send(message: dict) -> None:
            order.append(message["type"])

        middleware = CommitBeforeResponseMiddleware(app)
        await middleware({"type": "http", "path": "/"}, _no_receive, send)

        #: The whole contract, in one assertion: the write lands before the
        #: client is told the request succeeded.
        assert order[0] == "commit", order
        assert order[1:] == ["http.response.start", "http.response.body"]
        assert session.commits == 1

    async def test_a_failed_request_commits_nothing(self) -> None:
        """The error path must stay a rollback, not an early commit."""
        order: list[str] = []
        session = _RecordingSession(order)

        async def app(scope: Any, receive: Any, send: Any) -> None:
            current_request_session.set(session)
            raise RuntimeError("the route blew up before producing a response")

        async def send(message: dict) -> None:
            order.append(message["type"])

        middleware = CommitBeforeResponseMiddleware(app)
        with pytest.raises(RuntimeError):
            await middleware({"type": "http", "path": "/"}, _no_receive, send)

        assert session.commits == 0
        assert order == []

    async def test_a_lifespan_scope_is_passed_through(self) -> None:
        seen: list[str] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            seen.append(scope["type"])

        middleware = CommitBeforeResponseMiddleware(app)
        await middleware({"type": "lifespan"}, _no_receive, lambda message: None)  # type: ignore[arg-type]
        assert seen == ["lifespan"]


def _create_project(client: TestClient, suffix: str) -> str:
    response = client.post(
        "/api/v1/projects",
        json={"name": f"commit order {suffix}", "slug": f"commit-order-{suffix}"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


class TestReadYourWritesOverTheApi:
    def test_a_deleted_project_is_gone_for_the_next_request(
        self, client: TestClient
    ) -> None:
        project_id = _create_project(client, uuid.uuid4().hex[:8])

        deleted = client.delete(f"/api/v1/projects/{project_id}")
        assert deleted.status_code == 204
        assert client.get(f"/api/v1/projects/{project_id}").status_code == 404

    def test_a_created_project_is_listed_for_the_next_request(
        self, client: TestClient
    ) -> None:
        project_id = _create_project(client, uuid.uuid4().hex[:8])

        listing = client.get("/api/v1/projects", params={"page_size": 100})
        assert listing.status_code == 200
        assert project_id in [item["id"] for item in listing.json()["items"]]

    def test_a_refused_write_leaves_no_row_behind(self, client: TestClient) -> None:
        """A failed insert must not become visible because of an early commit."""
        suffix = uuid.uuid4().hex[:8]
        first = client.post(
            "/api/v1/projects",
            json={"name": "duplicate", "slug": f"dupe-{suffix}"},
        )
        assert first.status_code == 201

        second = client.post(
            "/api/v1/projects",
            json={"name": "duplicate", "slug": f"dupe-{suffix}"},
        )
        assert second.status_code in (400, 409), second.text

        listing = client.get("/api/v1/projects", params={"page_size": 100})
        matches = [
            item for item in listing.json()["items"] if item["slug"] == f"dupe-{suffix}"
        ]
        assert len(matches) == 1
