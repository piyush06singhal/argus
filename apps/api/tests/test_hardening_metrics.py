"""ARGUS hardening W5 — self-observability: is ARGUS visible while it works?

The §11 metric list is only real if the endpoint that publishes it *can* publish
it. These tests cover the series added in the second audit pass, and the two
rules that make them trustworthy:

* a metric is counted from the rows the service wrote, so it cannot disagree
  with the history it summarises;
* a signal that cannot be measured is **absent**, not zero — an unreachable
  Redis must not read as "the queue is empty".
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.models.reproduction import (
    ConfidenceLevel,
    ExperimentStatus,
    ReproductionExperiment,
    ReproductionResult,
)
from app.services.platform_time import utcnow
from app.services.queue import QueueUnavailable
from tests.phase11_helpers import episode


def _metrics(client: TestClient) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    return response.text


class TestPipelineOutcomeSeries:
    def test_the_quality_gauge_is_always_published(self, client):
        """Absent data must still be expressible: zero issues is a fact."""
        body = _metrics(client)
        assert "argus_data_quality_issues_open" in body

    def test_the_endpoint_never_fails_on_an_empty_database(self, client):
        """A scrape that 500s takes the whole monitoring stack down with it."""
        body = _metrics(client)
        assert "argus_api_up 1.0" in body

    async def test_a_reproduction_result_is_counted_with_its_result(
        self, db_session, client
    ):
        ctx = await episode(db_session, incident_status="OPEN")
        db_session.add(
            ReproductionExperiment(
                project_id=ctx.project.id,
                environment_id=ctx.environment.id,
                incident_id=ctx.incident.id,
                status=ExperimentStatus.COMPLETED,
                result=ReproductionResult.SUCCESSFUL,
                confidence=ConfidenceLevel.HIGH,
            )
        )
        await db_session.commit()

        body = _metrics(client)
        #: The label matches the metric's own name: ``_by_result`` is labelled
        #: ``result``. It used to be labelled ``status``, which meant an alert
        #: written from the documentation could never match the series.
        assert 'argus_reproduction_runs_24h_by_result{result="SUCCESSFUL"}' in body
        assert 'argus_reproduction_runs_24h_by_status{status="COMPLETED"}' in body

    async def test_a_pipeline_run_older_than_the_window_is_not_counted(
        self, db_session, client
    ):
        """The suffix is ``_24h``; a series that counted all history would lie."""
        ctx = await episode(db_session, incident_status="OPEN")
        row = ReproductionExperiment(
            project_id=ctx.project.id,
            environment_id=ctx.environment.id,
            incident_id=ctx.incident.id,
            status=ExperimentStatus.COMPLETED,
            result=ReproductionResult.FAILED,
            confidence=ConfidenceLevel.LOW,
        )
        db_session.add(row)
        await db_session.flush()
        row.created_at = utcnow() - timedelta(days=3)
        await db_session.commit()

        body = _metrics(client)
        #: The row aged out of the window, so the family reads zero — which is
        #: the point of emitting every enum member: "no failures in 24h" is
        #: readable from the series, instead of the series simply not existing.
        assert 'argus_reproduction_runs_24h_by_status{status="FAILED"} 0.0' in body


class TestQueueDepthHonesty:
    def test_an_unreachable_broker_reports_nothing_not_zero(self, client, monkeypatch):
        """The alert that matters most must not read as 'the queue is empty'."""
        from app.services import queue as queue_module

        def _unreachable(self):
            raise QueueUnavailable("redis is down")

        monkeypatch.setattr(queue_module.IngestionQueue, "_client", _unreachable)

        body = _metrics(client)

        assert "argus_ingestion_queue_depth" not in body

    def test_a_reachable_broker_publishes_depth_per_queue(self, client, monkeypatch):
        from app.services import queue as queue_module

        class _FakeRedis:
            async def llen(self, name: str) -> int:
                return 7

        monkeypatch.setattr(
            queue_module.IngestionQueue, "_client", lambda self: _FakeRedis()
        )

        body = _metrics(client)

        assert 'argus_ingestion_queue_depth{queue="argus:ingest:events"} 7.0' in body
        # Per queue, not summed: "ingestion is backed up" and "remediation is
        # backed up" need different responses from an operator.
        assert body.count("argus_ingestion_queue_depth{") >= 2

    def test_a_metric_failure_does_not_break_the_scrape(self, client, monkeypatch):
        from app.services import queue as queue_module

        class _ExplodingRedis:
            async def llen(self, name: str) -> int:
                raise RuntimeError("protocol error")

        monkeypatch.setattr(
            queue_module.IngestionQueue, "_client", lambda self: _ExplodingRedis()
        )

        body = _metrics(client)

        assert "argus_api_up 1.0" in body
        assert "argus_ingestion_queue_depth" not in body


class TestExpositionHygiene:
    """The scrape body is a wire format, and these are its two rules.

    Both were violated by this endpoint, and neither violation is visible from
    the API: Prometheus tolerates both, so only a test catches them.
    """

    def test_every_family_states_its_help_and_type_exactly_once(self, client):
        """A family's metadata belongs to the family, not to each sample.

        The exporter used to repeat HELP and TYPE per sample, which reported 87
        metadata blocks for 36 families — and any consumer that counts families
        by counting TYPE lines (a published number, in this project) over-counts.
        """
        body = _metrics(client)
        type_lines: dict[str, int] = {}
        help_lines: dict[str, int] = {}
        for line in body.splitlines():
            if line.startswith("# TYPE "):
                name = line[len("# TYPE ") :].split(" ", 1)[0]
                type_lines[name] = type_lines.get(name, 0) + 1
            elif line.startswith("# HELP "):
                name = line[len("# HELP ") :].split(" ", 1)[0]
                help_lines[name] = help_lines.get(name, 0) + 1

        assert type_lines, "the scrape published no families at all"
        duplicates = {n: c for n, c in type_lines.items() if c > 1}
        assert not duplicates, f"families with repeated TYPE lines: {duplicates}"
        assert set(help_lines) == set(
            type_lines
        ), "every family needs both HELP and TYPE, or neither"

    def test_metadata_precedes_the_samples_it_describes(self, client):
        """The one ordering rule the text format actually imposes."""
        body = _metrics(client)
        first_sample: dict[str, int] = {}
        type_at: dict[str, int] = {}
        for index, line in enumerate(body.splitlines()):
            if line.startswith("#") or not line.strip():
                if line.startswith("# TYPE "):
                    type_at[line[len("# TYPE ") :].split(" ", 1)[0]] = index
                continue
            name = line.split("{", 1)[0].split(" ", 1)[0]
            first_sample.setdefault(name, index)

        misplaced = [
            name
            for name, sample_at in first_sample.items()
            if name in type_at and type_at[name] > sample_at
        ]
        assert not misplaced, f"TYPE after first sample for: {misplaced}"


class TestSeriesExistenceIsGuaranteed:
    """Which referenced series may legitimately be missing, and which may not.

    `infrastructure/e2e-smoke-observability.sh` accepts a referenced series that
    is absent from the live scrape *if* the application source emits that name.
    That allowance is only safe if it stays narrow, so this class pins both ends
    of it: enum-labelled counters must exist for every enum member, and the only
    series allowed to depend on there being data are the population statistics.
    """

    #: Every referenced series that may be missing from a fresh deployment,
    #: with the condition. Absence is a *statement* in each case — "no resolved
    #: incident", "no backup has ever succeeded" — and reporting zero instead
    #: would be a lie ("resolved in zero seconds", "a backup finished in 1970").
    #: Where absence is the alarm itself, the rule fires on `absent()`.
    MAY_BE_ABSENT = {
        "argus_incident_mtta_seconds": "no incident acknowledged in the window",
        "argus_incident_mttr_seconds": "no incident resolved in the window",
        "argus_backup_last_success_timestamp_seconds": "no backup ever succeeded",
        "argus_backup_last_drill_timestamp_seconds": "no restore drill ever ran",
        "argus_backup_last_size_bytes": "the newest dump recorded no size",
        "argus_backup_last_duration_seconds": "the newest dump recorded no time",
        "argus_ingestion_queue_depth": (
            "the broker is unreachable — an unreachable queue is not an empty one"
        ),
    }

    def test_every_enum_labelled_counter_covers_its_whole_enum(self, client):
        """`{status="FAILED"} 0` is a fact; a missing series is nothing at all.

        A labelled counter that is simply absent cannot be alerted on: the rule
        loads, never matches, and reads as coverage. So every member is emitted
        whether or not a row exists.
        """
        from app.models.fix import VerificationStatus
        from app.models.intelligence import LearningRunStatus
        from app.models.observability import Severity
        from app.models.remediation import RemediationStatus
        from app.models.reproduction import ExperimentStatus, ReproductionResult

        body = _metrics(client)
        expected = [
            ("argus_logs_24h_by_severity", "severity", Severity),
            (
                "argus_patch_verifications_24h_by_status",
                "status",
                VerificationStatus,
            ),
            (
                "argus_remediation_actions_24h_by_status",
                "status",
                RemediationStatus,
            ),
            ("argus_learning_runs_24h_by_status", "status", LearningRunStatus),
            (
                "argus_reproduction_runs_24h_by_status",
                "status",
                ExperimentStatus,
            ),
            (
                "argus_reproduction_runs_24h_by_result",
                "result",
                ReproductionResult,
            ),
        ]
        for family, label, enum_type in expected:
            for member in enum_type:
                sample = f'{family}{{{label}="{member.value}"}}'
                assert sample in body, f"{sample} is not published"

    def test_only_declared_series_may_be_absent(self):
        """The gate's weak path, pinned to the reasons the code can justify.

        The gate accepts a referenced series that is absent from the live scrape
        if the application source emits that name — the allowance that makes
        alert rules meaningful on a deployment that has not yet produced the
        data they watch. That allowance is only as strong as its narrowness, so
        this test derives the conditional set from the code itself (emissions
        behind a `is not None` guard, or a best-effort `try`) and asserts it is
        exactly the reviewed list below.

        Adding a conditional series therefore fails here until someone writes
        down why it is allowed to be missing — which is the review this needs.
        """
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        app = root / "apps/api/app"
        source = "\n".join(path.read_text() for path in app.rglob("*.py"))

        referenced: set[str] = set()
        for relative in (
            "infrastructure/observability/prometheus/alerts.yml",
            "infrastructure/observability/grafana/dashboards/argus-overview.json",
        ):
            referenced |= set(
                re.findall(r"\bargus_[a-z0-9_]+\b", (root / relative).read_text())
            )
        assert referenced, "no series referenced at all — the check proves nothing"

        declared = {name for name in referenced if f'"{name}"' in source}
        unresolved = referenced - declared
        assert not unresolved, (
            "referenced by a rule or the dashboard but never emitted by the "
            f"API source: {sorted(unresolved)}"
        )

        derived = _conditional_names(app)
        assert derived == set(self.MAY_BE_ABSENT), (
            "a series may only be absent from a fresh deployment for a stated "
            f"reason; undocumented: {sorted(derived - set(self.MAY_BE_ABSENT))}, "
            f"no longer conditional: {sorted(set(self.MAY_BE_ABSENT) - derived)}"
        )


def _conditional_names(app_dir):
    """Series the source emits only when some condition holds.

    Two shapes count as conditional, both read from the AST rather than from a
    guess about formatting: an emission inside an `if <x> is not None` block,
    and one inside a `try` whose handler skips it. A name emitted unguarded
    *anywhere* is unconditional — that is what makes the derivation safe.

    Series whose name is composed at runtime (`_PREFIX + name`) are invisible to
    this scan and are covered instead by the live check in the gate.
    """
    import ast
    import re

    guarded: set[str] = set()
    unguarded: set[str] = set()

    def _is_guard(node: ast.AST) -> bool:
        if isinstance(node, ast.If):
            return any(
                isinstance(sub, ast.Compare)
                and any(isinstance(op, ast.IsNot) for op in sub.ops)
                and any(
                    isinstance(cmp, ast.Constant) and cmp.value is None
                    for cmp in sub.comparators
                )
                for sub in ast.walk(node.test)
            )
        return isinstance(node, ast.Try) and bool(node.handlers)

    def _walk(node: ast.AST, is_guarded: bool) -> None:
        for child in ast.iter_child_nodes(node):
            child_guarded = is_guarded or _is_guard(child)
            if (
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and re.fullmatch(r"argus_[a-z0-9_]+", child.value)
            ):
                (guarded if child_guarded else unguarded).add(child.value)
            _walk(child, child_guarded)

    for path in app_dir.rglob("*.py"):
        _walk(ast.parse(path.read_text()), False)

    return guarded - unguarded
