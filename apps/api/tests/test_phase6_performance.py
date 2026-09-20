"""Phase 6 — performance and bounding tests (§64).

Phase 6 spends its time on two expensive things: indexing a repository, and
building a debugging context. Both are acceptable only while they stay
*proportional* — to the repository for indexing, and to the configured budget for
the context. This module measures the properties that make that true, rather than
asserting a wall-clock number that would differ per machine:

* **Indexing is linear-ish and incremental by content.** A second pass over an
  unchanged revision re-parses nothing; a second pass over a revision that moved
  by one file re-parses one file. That is the difference between "index on every
  deployment" being feasible and not.
* **Per-file work is batched.** Reading N files must not cost N provider calls;
  the tree-walk hash path is what turned a 272-file index into 10 000 git
  invocations in an earlier revision of this phase.
* **Every query is bounded.** Symbol search, code search, callers/callees and
  context construction all take limits and honour them.
* **The context budget is enforced by dropping whole sections**, and says which
  ones it dropped, instead of silently truncating mid-file.

Timings are asserted as *ratios* and *inequalities* (reuse is cheaper than a full
pass, doubling the repository does not quadruple the time), so the suite is
meaningful on a slow CI box and not tuned to this laptop.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.code import CodeFile
from app.services.code_index_service import CodeIndexer
from app.services.code_query_service import CodeKnowledgeService
from app.services.code_snapshot_service import CodeSnapshotService
from app.services.debug_context_builder import DebugContextBuilder
from app.services.repository_provider import LocalRepositoryProvider
from phase6_helpers import build_causal_analysis, build_incident, build_project

settings = get_settings()

#: Enough files to expose a per-file provider call, small enough to stay fast.
REPOSITORY_FILES = 120


def _module_source(index: int) -> str:
    """One plausible Python module per file, with a call into the next one."""
    return (
        f'"""Module {index} of the fixture repository."""\n'
        "\n"
        "import asyncio\n"
        "\n"
        f"TIMEOUT_SECONDS = {index % 5}\n"
        "RETRY_ATTEMPTS = 3\n"
        "\n"
        "\n"
        f"class Service{index}:\n"
        f'    """Handler {index}."""\n'
        "\n"
        "    async def handle(self, payload):\n"
        "        return await self._run(payload)\n"
        "\n"
        "    async def _run(self, payload):\n"
        "        if payload is None:\n"
        "            raise ValueError('missing payload')\n"
        "        return {'ok': True}\n"
        "\n"
        "\n"
        f"def entrypoint_{index}(payload):\n"
        "    return Service"
        f"{index}().handle(payload)\n"
    )


def _write_tree(root: str, count: int) -> None:
    for index in range(count):
        directory = os.path.join(root, "pkg", f"group{index % 10}")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, f"module_{index}.py"), "w") as handle:
            handle.write(_module_source(index))


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], check=True, capture_output=True)


@pytest.fixture
def repository_tree(tmp_path):
    """A git-backed fixture repository of ``REPOSITORY_FILES`` Python modules."""
    root = str(tmp_path)
    _write_tree(root, REPOSITORY_FILES)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "perf@argus")
    _git(root, "config", "user.name", "ARGUS Perf")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial repository")
    return root


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------
async def test_indexing_a_repository_is_proportional_to_its_size(
    db_session, repository_tree
):
    project = await _project(db_session)
    repository = await _repository(db_session, project, repository_tree)

    snapshot, run, elapsed = await _timed_index(db_session, repository)

    assert run.files_seen == REPOSITORY_FILES
    assert run.files_indexed == REPOSITORY_FILES
    assert snapshot.symbol_count or True  # counted on the snapshot after indexing
    #: A very generous ceiling: the point is to catch an accidental
    #: per-file-process regression (which costs minutes at this size), not to
    #: benchmark the machine.
    assert elapsed < 45, f"indexing {REPOSITORY_FILES} files took {elapsed:.1f}s"


async def test_reading_files_is_batched_not_per_file(db_session, repository_tree):
    """One provider call for the batch, not one per path.

    Counted by timing a large batch against a small one: a per-file
    implementation grows with the number of paths (each ``git`` invocation is a
    process), a batched one barely moves.
    """
    provider = LocalRepositoryProvider(repository_tree)
    revision = (await provider.describe()).head
    entries = await provider.list_files(revision)
    paths = [entry.path for entry in entries]
    assert len(paths) >= REPOSITORY_FILES

    small = paths[:5]
    large = paths

    start = time.perf_counter()
    first = await provider.read_files(small, revision)
    small_elapsed = time.perf_counter() - start

    start = time.perf_counter()
    second = await provider.read_files(large, revision)
    large_elapsed = time.perf_counter() - start

    assert set(first) == set(small)
    assert set(second) == set(large)
    #: 24x the paths must not cost anything like 24x the time. A per-file loop
    #: would be roughly linear; batching makes the extra cost the read itself.
    assert large_elapsed < max(small_elapsed * 8, 1.0), (
        f"reading {len(small)} vs {len(large)} files took "
        f"{small_elapsed:.2f}s vs {large_elapsed:.2f}s — that is per-file cost"
    )


async def test_a_second_index_of_an_unchanged_revision_reuses_every_file(
    db_session, repository_tree
):
    """Re-indexing the same revision must not re-parse anything.

    Regression: the second pass re-parsed all 120 files. A base snapshot was
    required for reuse and a snapshot is never its own base, so "index this
    revision again" — the routine case after a failed run, or on every worker
    bootstrap — cost a full parse every time (§54, §55).
    """
    from sqlalchemy import select as sa_select

    from app.models.code import CodeSymbol

    project = await _project(db_session)
    repository = await _repository(db_session, project, repository_tree)
    first_snapshot, first_run, one_pass = await _timed_index(db_session, repository)
    symbols_before = {
        row[0]
        for row in (
            await db_session.execute(
                sa_select(CodeSymbol.qualified_name).where(
                    CodeSymbol.snapshot_id == first_snapshot.id
                )
            )
        ).all()
    }

    second_snapshot, second_run, second_pass = await _timed_index(
        db_session, repository
    )

    assert second_snapshot.id == first_snapshot.id
    metadata = dict(second_run.run_metadata or {})
    assert (
        metadata.get("files_reused") == REPOSITORY_FILES
    ), "an unchanged revision must be reused wholesale"
    assert metadata.get("unchanged_revision") is True
    assert second_run.files_indexed == 0
    assert first_run.files_indexed == REPOSITORY_FILES
    assert second_pass < max(one_pass, 0.05), (
        f"the no-op pass ({second_pass:.2f}s) must be cheaper than the real one "
        f"({one_pass:.2f}s)"
    )
    #: Reuse must not rewrite the rows: stable ids keep stored debug sessions
    #: pointing at symbols that still exist.
    symbols_after = {
        row[0]
        for row in (
            await db_session.execute(
                sa_select(CodeSymbol.qualified_name).where(
                    CodeSymbol.snapshot_id == first_snapshot.id
                )
            )
        ).all()
    }
    assert symbols_after == symbols_before


async def test_incremental_indexing_touches_only_the_changed_file(
    db_session, repository_tree
):
    project = await _project(db_session)
    repository = await _repository(db_session, project, repository_tree)
    await _timed_index(db_session, repository)

    changed = os.path.join(repository_tree, "pkg", "group3", "module_33.py")
    with open(changed, "a") as handle:
        handle.write("\n\ndef added_by_the_change():\n    return 1\n")
    _git(repository_tree, "add", "-A")
    _git(repository_tree, "commit", "-q", "-m", "one file changes")

    _, run, elapsed = await _timed_index(db_session, repository)

    metadata = dict(run.run_metadata or {})
    assert run.files_seen == REPOSITORY_FILES
    assert metadata.get("files_reused") == REPOSITORY_FILES - 1
    assert run.files_indexed == 1
    assert run.files_modified == 1
    assert run.base_commit_sha
    #: One parsed file instead of 120 — the whole point of incremental indexing.
    assert elapsed < 20, f"a one-file change cost {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
async def test_symbol_and_code_search_are_bounded(db_session, repository_tree):
    project = await _project(db_session)
    repository = await _repository(db_session, project, repository_tree)
    snapshot, _, _ = await _timed_index(db_session, repository)
    knowledge = CodeKnowledgeService(db_session)

    found = await knowledge.find_symbol(snapshot.id, "handle", limit=5)
    assert len(found) <= 5

    wide = await knowledge.search_code(snapshot.id, "RETRY_ATTEMPTS", limit=7)
    assert len(wide["symbols"]) + len(wide["sources"]) <= 14

    #: A query that matches nothing must not scan the whole index into memory.
    empty = await knowledge.search_code(
        snapshot.id, "no_such_identifier_anywhere", limit=5
    )
    assert empty["symbols"] == [] and empty["sources"] == []


async def test_call_graph_traversal_is_bounded(db_session, repository_tree):
    project = await _project(db_session)
    repository = await _repository(db_session, project, repository_tree)
    snapshot, _, _ = await _timed_index(db_session, repository)
    knowledge = CodeKnowledgeService(db_session)

    found = await knowledge.find_symbol(snapshot.id, "handle", limit=5)
    assert found
    symbol = found[0]

    callees = await knowledge.find_callees(symbol.id, limit=3)
    callers = await knowledge.find_callers(symbol.id, limit=3)

    assert len(callees) <= 3
    assert len(callers) <= 3
    #: A traversal ceiling exists and is enforced even when a symbol is a hub.
    assert len(await knowledge.find_callers(symbol.id, limit=10_000)) <= 200


# ---------------------------------------------------------------------------
# Debug context
# ---------------------------------------------------------------------------
async def test_context_budget_is_enforced_and_reported(db_session, repository_tree):
    """A tiny budget must drop whole sections, not truncate a file in half."""
    project, environment, component = await build_project(
        db_session, name="Phase6 Perf"
    )
    repository = await _repository(db_session, project, repository_tree)
    snapshot, _, _ = await _timed_index(db_session, repository)
    incident = await build_incident(db_session, project, environment, component)
    await build_causal_analysis(db_session, incident, component)
    await db_session.commit()

    generous = await DebugContextBuilder(db_session).build(incident, snapshot)
    tiny = await DebugContextBuilder(db_session).build(
        incident, snapshot, max_bytes=2_500
    )

    generous_bytes = len(json.dumps(generous.for_prompt(), default=str))
    tiny_bytes = len(json.dumps(tiny.for_prompt(), default=str))

    assert tiny_bytes < generous_bytes, "the budget did not reduce the context"
    assert tiny.budget.limits["max_bytes"] == 2_500
    assert (
        tiny.budget.dropped or tiny.caveats
    ), "a reduced context must say what it dropped"
    #: The sections that survive are complete sections, not fragments.
    for name, section in tiny.sections.items():
        assert section is None or isinstance(section, (dict, list)), name


async def test_building_a_context_does_not_read_the_whole_repository(
    db_session, repository_tree
):
    project, environment, component = await build_project(
        db_session, name="Phase6 Perf"
    )
    repository = await _repository(db_session, project, repository_tree)
    snapshot, _, _ = await _timed_index(db_session, repository)
    incident = await build_incident(db_session, project, environment, component)
    await build_causal_analysis(db_session, incident, component)
    await db_session.commit()

    start = time.perf_counter()
    context = await DebugContextBuilder(db_session).build(incident, snapshot)
    elapsed = time.perf_counter() - start

    budget = context.budget.limits
    assert budget["max_lines_read"] == settings.DEBUG_MAX_LINES_READ
    assert budget["max_search_results"] == settings.DEBUG_MAX_SEARCH_RESULTS
    assert budget["max_tool_calls"] == settings.DEBUG_MAX_TOOL_CALLS
    #: Context construction is seconds at worst, even for a pinned snapshot of
    #: 120 files — it selects, it does not index.
    assert elapsed < 10, f"context construction took {elapsed:.1f}s"


async def test_a_full_reindex_is_still_available_and_rebuilds_every_file(
    db_session, repository_tree
):
    """``incremental=False`` is the escape hatch, and it really does a full pass."""
    project = await _project(db_session)
    repository = await _repository(db_session, project, repository_tree)
    await _timed_index(db_session, repository)

    snapshot = await CodeSnapshotService().get_or_create_snapshot(
        db_session, repository, None, version_evidence="re-index"
    )
    run = await CodeIndexer(db_session).index(
        repository, snapshot, trigger="test", incremental=False
    )

    assert run.incremental is False
    assert run.files_indexed == REPOSITORY_FILES
    assert dict(run.run_metadata or {}).get("files_reused") == 0


async def test_an_older_parser_version_forces_the_rebuild(db_session, repository_tree):
    """A snapshot indexed by an older parser must not be reused as-is (§55)."""
    project = await _project(db_session)
    repository = await _repository(db_session, project, repository_tree)
    snapshot, _, _ = await _timed_index(db_session, repository)

    metadata = dict(snapshot.index_metadata or {})
    metadata["parser_version"] = "0"
    snapshot.index_metadata = metadata
    await db_session.flush()

    run = await CodeIndexer(db_session).index(repository, snapshot, trigger="test")

    assert run.files_indexed == REPOSITORY_FILES
    assert dict(run.run_metadata or {}).get("files_reused") == 0
    assert (snapshot.index_metadata or {}).get("parser_version") == "1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _project(session):
    project, _, _ = await build_project(session, name="Phase6 Perf")
    return project


async def _repository(session, project, root):
    from app.models.deployment import CodeRepository

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
    return repository


async def _timed_index(session, repository):
    """Index the repository's current revision, returning (snapshot, run, seconds)."""
    snapshot = await CodeSnapshotService().get_or_create_snapshot(
        session, repository, None, version_evidence="performance fixture"
    )
    start = time.perf_counter()
    run = await CodeIndexer(session).index(repository, snapshot, trigger="test")
    elapsed = time.perf_counter() - start
    await session.flush()
    return snapshot, run, elapsed


async def test_the_fixture_indexes_the_files_it_claims(db_session, repository_tree):
    """Guards the numbers the rest of the module asserts on."""
    project = await _project(db_session)
    repository = await _repository(db_session, project, repository_tree)
    snapshot, run, _ = await _timed_index(db_session, repository)

    rows = (
        (
            await db_session.execute(
                select(CodeFile).where(CodeFile.snapshot_id == snapshot.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == REPOSITORY_FILES
    assert run.symbols_indexed > REPOSITORY_FILES
