"""Shared builders for the Phase 6 test suites.

Deliberately small and explicit. The debugger's guarantees depend on *exactly*
what the context contains, so a test that wants to prove "an out-of-snapshot
line range is rejected" needs a repository whose line counts it knows.

The sample repository reproduces the phase's canonical defect shape: a checkout
service whose retry loop amplifies a timeout configured down to sub-second.
Nothing in the debugger knows that — the tests check that the *evidence* is what
points at it.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.engines import AIModelProvider

#: Two files with known line counts, a caller/callee pair, a route and a test
#: file. ``checkout.py`` line 20 is the retry call; ``inventory.py`` line 15 is
#: the raising query. Those numbers are asserted in tests, so the file is padded
#: deliberately.
CHECKOUT_SOURCE = '''"""Sample checkout service (Phase 6 fixture)."""

import asyncio

from shop.inventory import InventoryRepository

RETRY_ATTEMPTS = 7
RETRY_BACKOFF_SECONDS = 0.4


class CheckoutService:
    def __init__(self, repository: InventoryRepository) -> None:
        self.repository = repository

    async def process(self, sku: str, quantity: int):
        last_error = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                return await self.reserve(sku, quantity)
            except TimeoutError as error:
                last_error = error
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        raise last_error

    async def reserve(self, sku: str, quantity: int):
        stock = await self.repository.fetch_stock(sku)
        if not stock:
            raise ValueError("unknown sku")
        return {"sku": sku, "quantity": quantity}
'''

INVENTORY_SOURCE = '''"""Sample inventory access (Phase 6 fixture)."""

DB_TIMEOUT_SECONDS = 0.5


class InventoryRepository:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    async def fetch_stock(self, sku: str):
        return await self._query(f"select stock from inventory where sku = '{sku}'")

    async def _query(self, sql: str):
        if DB_TIMEOUT_SECONDS < 1:
            raise TimeoutError("inventory database query timed out")
        return {"sku": "ok"}
'''

ROUTES_SOURCE = '''"""Sample HTTP surface (Phase 6 fixture)."""

from shop.checkout import CheckoutService


def create_checkout_route(service: CheckoutService):
    async def post_checkout(request):
        return await service.process(request["sku"], request["quantity"])

    post_checkout.route = "POST /checkout"
    return post_checkout
'''

TEST_SOURCE = '''"""Sample test (Phase 6 fixture)."""


def test_checkout_retries():
    assert True
'''

SAMPLE_FILES = {
    "shop/checkout.py": CHECKOUT_SOURCE,
    "shop/inventory.py": INVENTORY_SOURCE,
    "api/routes.py": ROUTES_SOURCE,
    "tests/test_checkout.py": TEST_SOURCE,
}

#: Where the fixture's known-relevant lines are, so tests can assert on real
#: numbers instead of re-deriving them.
CHECKOUT_LINES = len(CHECKOUT_SOURCE.splitlines())
INVENTORY_LINES = len(INVENTORY_SOURCE.splitlines())


class ScriptedProvider(AIModelProvider):
    """A provider that returns a prepared answer, or fails on demand.

    Scripted rather than mock-fed through settings so a test can assert what
    happens to a *specific* malformed or fabricated answer — which is where the
    validator's value actually lies.
    """

    name = "scripted"

    def __init__(
        self,
        payload: Any = None,
        *,
        error: Optional[Exception] = None,
        answers: Optional[list] = None,
    ) -> None:
        self.payload = payload
        self.error = error
        self.answers = list(answers or [])
        self.calls: list[dict] = []

    async def complete(self, prompt: str, **kwargs: Any) -> str:
        return "{}"

    async def complete_structured(
        self, prompt: str, response_schema: dict, **kwargs: Any
    ) -> dict:
        self.calls.append({"prompt": prompt, "messages": kwargs.get("messages")})
        if self.error is not None:
            raise self.error
        if self.answers:
            return self.answers.pop(0)
        return self.payload if isinstance(self.payload, dict) else {}

    async def embed(self, text: str) -> list[float]:
        return [0.0]


async def build_repository(
    session: AsyncSession, project, tmp_path, *, commit_message: str = "initial"
):
    """Create a git-backed repository and index it, returning (repo, snapshot)."""
    from app.models.deployment import CodeRepository, RepositoryIndexStatus
    from app.services.code_index_service import CodeIndexer
    from app.services.code_snapshot_service import CodeSnapshotService

    root = str(tmp_path)
    for path, content in SAMPLE_FILES.items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as handle:
            handle.write(content)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "fixture@argus")
    _git(root, "config", "user.name", "ARGUS Fixture")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", commit_message)

    repository = CodeRepository(
        project_id=project.id,
        provider="local",
        repository_url=root,
        local_path=root,
        default_branch="main",
        language="python",
    )
    session.add(repository)
    await session.flush()

    snapshot = await CodeSnapshotService().get_or_create_snapshot(
        session, repository, None, version_evidence="test fixture"
    )
    run = await CodeIndexer(session).index(repository, snapshot, trigger="test")
    repository.index_status = RepositoryIndexStatus.INDEXED
    repository.last_indexed_commit = snapshot.commit_sha
    await session.flush()
    return repository, snapshot, run


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], check=True, capture_output=True)


async def build_causal_analysis(session: AsyncSession, incident, component):
    """A completed Phase 4 analysis with one supported candidate.

    Included by default because it is what a real investigation has: Phase 6's
    job is to reason over Phase 4's output, not to re-derive it.
    """
    from app.models.causal import (
        AnalysisStatus,
        CandidateType,
        CausalAnalysis,
        CausalEvidence,
        CausalEvidenceCategory,
        ConfidenceLevel,
        EvidencePolarity,
        RootCauseCandidate,
    )

    analysis = CausalAnalysis(
        project_id=incident.project_id,
        environment_id=incident.environment_id,
        incident_id=incident.id,
        analysis_version=1,
        status=AnalysisStatus.COMPLETED,
        started_at=incident.detected_at,
        completed_at=incident.detected_at,
        overall_confidence=ConfidenceLevel.MEDIUM,
        summary="Inventory query timeout with retry amplification",
        missing_evidence=["no database query plan evidence"],
    )
    session.add(analysis)
    await session.flush()
    candidate = RootCauseCandidate(
        analysis_id=analysis.id,
        project_id=incident.project_id,
        component_id=component.id,
        candidate_type=CandidateType.APPLICATION_COMPONENT,
        score=0.72,
        confidence=ConfidenceLevel.MEDIUM,
        first_observed_at=incident.detected_at,
        supporting_evidence_count=2,
        contradicting_evidence_count=0,
        explanation=(
            "Inventory latency caused inventory timeouts, which propagated into "
            "checkout failures"
        ),
        uncertainty={"gaps": ["no query plan evidence"]},
    )
    session.add(candidate)
    await session.flush()
    session.add(
        CausalEvidence(
            analysis_id=analysis.id,
            candidate_id=candidate.id,
            project_id=incident.project_id,
            category=CausalEvidenceCategory.TRACE,
            polarity=EvidencePolarity.SUPPORTING,
            source_table="spans",
            quote="InventoryRepository.fetch_stock failed after 3800ms",
            explanation="failing child span under POST /checkout",
            strength=0.9,
        )
    )
    await session.flush()
    return analysis, candidate


async def build_incident(
    session: AsyncSession,
    project,
    environment,
    component,
    *,
    failing: bool = True,
    stack_trace: Optional[str] = None,
    logs: bool = True,
    causal: bool = True,
):
    """An incident with one failing trace, a stack trace, a deployment and (by
    default) a completed causal analysis."""
    from app.models.deployment import DeploymentEvent, DeploymentStatus
    from app.models.incident import Incident, IncidentSeverity, IncidentStatus
    from app.models.observability import (
        LogRecord,
        Severity as LogSeverity,
        SpanRecord,
        TraceRecord,
        TraceStatus,
    )

    onset = datetime.now(timezone.utc) - timedelta(minutes=30)
    incident = Incident(
        project_id=project.id,
        environment_id=environment.id,
        primary_component_id=component.id,
        title="Checkout failures: inventory timeouts",
        description="POST /checkout times out",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        detected_at=onset,
        started_at=onset,
        summary="Error rate 33% on POST /checkout",
    )
    session.add(incident)
    await session.flush()

    trace_id = f"trace-{uuid.uuid4().hex[:10]}"
    session.add(
        TraceRecord(
            project_id=project.id,
            environment_id=environment.id,
            trace_id=trace_id,
            name="POST /checkout",
            start_time=onset,
            duration_ms=4000,
            status=TraceStatus.ERROR if failing else TraceStatus.OK,
        )
    )
    await session.flush()
    parent = SpanRecord(
        trace_id=trace_id,
        span_id=f"span-{uuid.uuid4().hex[:8]}",
        project_id=project.id,
        component_id=component.id,
        operation="POST /checkout",
        start_time=onset,
        duration_ms=4000,
        status=TraceStatus.ERROR if failing else TraceStatus.OK,
        metadata_={"http.route": "/checkout", "http.method": "POST"},
    )
    session.add(parent)
    await session.flush()
    session.add(
        SpanRecord(
            trace_id=trace_id,
            span_id=f"span-{uuid.uuid4().hex[:8]}",
            parent_span_id=parent.span_id,
            project_id=project.id,
            component_id=component.id,
            operation="InventoryRepository.fetch_stock",
            start_time=onset + timedelta(milliseconds=200),
            duration_ms=3800,
            status=TraceStatus.ERROR if failing else TraceStatus.OK,
        )
    )
    if logs:
        session.add(
            LogRecord(
                project_id=project.id,
                environment_id=environment.id,
                component_id=component.id,
                timestamp=onset + timedelta(seconds=4),
                level=LogSeverity.ERROR,
                service="checkout-service",
                message=stack_trace
                or (
                    "Traceback (most recent call last):\n"
                    '  File "/srv/app/shop/checkout.py", line 20, in process\n'
                    "    return await self.reserve(sku, quantity)\n"
                    '  File "/srv/app/shop/inventory.py", line 15, in _query\n'
                    '    raise TimeoutError("inventory database query timed out")\n'
                    "TimeoutError: inventory database query timed out"
                ),
                trace_id=trace_id,
            )
        )
    session.add(
        DeploymentEvent(
            project_id=project.id,
            environment_id=environment.id,
            component_id=component.id,
            deployment_id=f"deploy-{uuid.uuid4().hex[:6]}",
            version="1.4.2",
            commit_sha=(await _head_sha(session, project.id)),
            status=DeploymentStatus.SUCCESS,
            deployed_at=onset - timedelta(minutes=12),
            description="reduced database timeout to 500ms",
        )
    )
    await session.flush()
    if causal:
        await build_causal_analysis(session, incident, component)
    return incident


async def _head_sha(session: AsyncSession, project_id) -> Optional[str]:
    from sqlalchemy import select

    from app.models.code import RepositorySnapshot

    row = (
        await session.execute(
            select(RepositorySnapshot)
            .where(RepositorySnapshot.project_id == project_id)
            .order_by(RepositorySnapshot.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    return row.commit_sha if row else None


async def build_project(session: AsyncSession, *, name: str = "Phase6 Fixture"):
    """Project, environment and component — the minimum a session needs."""
    import uuid as _uuid

    from app.models.project import SoftwareProject

    suffix = _uuid.uuid4().hex[:6]
    project = SoftwareProject(name=f"{name} {suffix}", slug=f"phase6-{suffix}")
    session.add(project)
    await session.flush()
    environment, component = await build_scope(session, project.id)
    return project, environment, component


async def build_scope(session: AsyncSession, project_id, *, component: str = "checkout-service"):
    """Environment and component under an *existing* project id.

    Needed because the API tests create the project through HTTP and then seed
    telemetry directly: re-pointing a seeded incident at the API's project would
    orphan its spans, and a trace mapping for a span in another project is
    exactly the isolation bug these tests would then fail to catch.
    """
    from app.models.project import Environment
    from app.models.system import SystemComponent

    environment = Environment(
        project_id=project_id, name="production", environment_type="PRODUCTION"
    )
    session.add(environment)
    await session.flush()
    created = SystemComponent(
        project_id=project_id, name=component, component_type="SERVICE"
    )
    session.add(created)
    await session.flush()
    return environment, created
