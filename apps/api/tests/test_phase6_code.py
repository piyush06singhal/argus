"""Phase 6 — repository, parser, symbol and mapping tests (§63).

Four groups, each asserting behaviour rather than the presence of code:

* **Repository** (§5, §18, §23): the provider reads a real git checkout — HEAD,
  history, blame, diff, per-file attribution — and refuses anything outside its
  root or its read-only command allow-list. It also must not *pretend*: a
  directory without version control is described as a working tree, not as a
  repository with an empty history that reads like "nothing ever changed".
* **Parser** (§11, §12): definitions, imports, calls, routes and syntax failures
  are extracted from real Python; JS/TS is read by the structural scanner and
  says so.
* **Symbols and graph** (§13, §14): callers, callees and cross-file resolution,
  all bounded and scoped to one snapshot.
* **Trace mapping** (§15, §16, §17): stack frames in several languages become
  locations, and a frame is never promoted to a cause.

The three defects the live smoke gate found have regression coverage at the
bottom: per-file commit attribution was never written, an explicitly pinned
revision was recorded as unresolved, and an unknown revision produced a
*silently empty* diff.
"""

from __future__ import annotations

from typing import NamedTuple

import hashlib
import os
import subprocess

from sqlalchemy import select

from app.models.code import CodeFile, ParseStatus
from app.services.code_parser import (
    HEURISTIC_PARSER_NOTE,
    PythonParser,
    parse_source,
)
from app.services.code_query_service import CodeKnowledgeService
from app.services.repository_provider import (
    LocalRepositoryProvider,
    ProviderError,
    RepositoryError,
    safe_relative_path,
)
from app.services.trace_code_mapper import StackTraceAnalyzer
from phase6_helpers import SAMPLE_FILES, build_project


# ---------------------------------------------------------------------------
# Repository (§5, §18)
# ---------------------------------------------------------------------------
async def test_local_provider_describes_a_git_checkout(tmp_path):
    _write_repo(tmp_path)
    provider = LocalRepositoryProvider(str(tmp_path))

    info = await provider.describe()

    assert info.vcs_present is True
    assert info.provider_name == "local"
    assert info.head and len(info.head) >= 7
    assert info.root == os.path.realpath(str(tmp_path))


async def test_local_provider_reports_a_working_tree_without_vcs(tmp_path):
    """No version control is stated, not disguised as an empty history."""
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "checkout.py").write_text("value = 1\n")
    provider = LocalRepositoryProvider(str(tmp_path))

    info = await provider.describe()
    history = await provider.get_history()
    commits = await provider.file_commits(["shop/checkout.py"])

    assert info.vcs_present is False
    assert [commit.message for commit in history] == [
        "working tree (no version control)"
    ]
    assert commits == {}, "no VCS means no per-file attribution to claim"
    assert await provider.get_blame("shop/checkout.py") == []


async def test_local_provider_refuses_a_root_outside_the_allow_list(
    tmp_path, monkeypatch
):
    #: ``get_settings()`` builds a fresh ``Settings`` per call, so the guard must
    #: be exercised through the instance the provider module actually holds —
    #: patching a new one would prove nothing about the running process.
    from app.services import repository_provider

    monkeypatch.setattr(
        repository_provider.settings,
        "CODE_ALLOWED_ROOTS",
        ["/definitely/not/this/path"],
    )

    try:
        LocalRepositoryProvider(str(tmp_path))
    except RepositoryError as error:
        assert "CODE_ALLOWED_ROOTS" in str(error)
    else:  # pragma: no cover - the assertion above is the point
        raise AssertionError("a root outside CODE_ALLOWED_ROOTS was accepted")


async def test_local_provider_reads_files_history_blame_and_diff(tmp_path):
    _write_repo(tmp_path)
    provider = LocalRepositoryProvider(str(tmp_path))

    paths = {entry.path for entry in await provider.list_files()}
    assert {"shop/checkout.py", "shop/inventory.py"} <= paths

    source = await provider.read_file("shop/checkout.py")
    assert "class CheckoutService" in source
    batched = await provider.read_files(["shop/checkout.py", "shop/inventory.py"])
    assert set(batched) == {"shop/checkout.py", "shop/inventory.py"}

    history = await provider.get_history()
    assert [commit.message for commit in history] == ["initial"]
    assert history[0].author == "ARGUS Fixture"
    assert history[0].committed_at is not None

    blame = await provider.get_blame("shop/inventory.py")
    assert blame and all(line.sha == history[0].sha for line in blame)

    assert await provider.diff(history[0].sha, history[0].sha) == []
    assert await provider.get_commit("nope-not-a-commit") is None
    assert await provider.resolve("nope-not-a-commit") is None


async def test_safe_relative_path_rejects_traversal():
    for hostile in ("../etc/passwd", "shop/../../etc", "/etc/passwd", ""):
        try:
            safe_relative_path(hostile)
        except RepositoryError:
            continue
        raise AssertionError(f"{hostile!r} was accepted as a relative path")
    assert safe_relative_path("./shop/checkout.py") == "shop/checkout.py"


async def test_read_file_refuses_a_path_outside_the_root(tmp_path):
    _write_repo(tmp_path)
    provider = LocalRepositoryProvider(str(tmp_path))

    try:
        await provider.read_file("../../etc/passwd")
    except RepositoryError:
        return
    raise AssertionError("a traversal path was read")


async def test_the_git_subcommand_allow_list_is_enforced(tmp_path):
    """A mutating verb cannot be reached, even by a bug elsewhere."""
    _write_repo(tmp_path)
    provider = LocalRepositoryProvider(str(tmp_path))
    git = await provider._vcs()
    assert git is not None

    for subcommand in ("push", "commit", "checkout", "reset"):
        try:
            await git._git([subcommand])
        except ProviderError as error:
            assert "allow-list" in str(error)
        else:  # pragma: no cover
            raise AssertionError(f"git {subcommand} was not refused")


async def test_file_commits_finds_the_newest_touching_commit_per_path(tmp_path):
    """One history walk; the newest commit that touched a path wins."""
    _write_repo(tmp_path)
    first = await _head(tmp_path)

    _append(tmp_path, "shop/inventory.py", "\n\n# tuned\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "tune the inventory module")
    second = await _head(tmp_path)

    commits = await LocalRepositoryProvider(str(tmp_path)).file_commits(
        ["shop/inventory.py", "shop/checkout.py", "shop/never-existed.py"]
    )

    assert set(commits) == {"shop/inventory.py", "shop/checkout.py"}
    assert commits["shop/inventory.py"].sha == second
    assert commits["shop/checkout.py"].sha == first
    assert commits["shop/inventory.py"].author == "ARGUS Fixture"
    assert await LocalRepositoryProvider(str(tmp_path)).file_commits([]) == {}


# ---------------------------------------------------------------------------
# Parser (§11, §12)
# ---------------------------------------------------------------------------
def test_python_parser_extracts_definitions_with_lines_and_signatures():
    parsed = PythonParser().parse("shop/checkout.py", SAMPLE_FILES["shop/checkout.py"])

    assert parsed.status is ParseStatus.PARSED
    by_name = {symbol.name: symbol for symbol in parsed.symbols}
    assert {"CheckoutService", "process", "reserve"} <= set(by_name)

    service = by_name["CheckoutService"]
    process = by_name["process"]
    assert service.start_line < process.start_line <= service.end_line
    assert process.qualified_name.endswith("CheckoutService.process")
    assert process.parent_index is not None
    assert parsed.symbols[process.parent_index].name == "CheckoutService"
    assert process.is_async is True
    assert "async def process" in (process.signature or "")

    reserve = by_name["reserve"]
    assert reserve.start_line > process.start_line
    assert reserve.end_line >= reserve.start_line


def test_python_parser_extracts_imports_and_call_references():
    parsed = PythonParser().parse("shop/checkout.py", SAMPLE_FILES["shop/checkout.py"])

    assert "shop.inventory" in parsed.imports
    names = {reference.name for reference in parsed.references}
    assert "InventoryRepository" in names
    assert any(name.endswith("fetch_stock") for name in names)
    assert all(reference.line >= 1 for reference in parsed.references)


def test_python_parser_recognises_route_decorators():
    source = (
        "from fastapi import APIRouter\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        "\n"
        "@router.post('/api/checkout')\n"
        "async def post_checkout(request):\n"
        "    return await service.process(request)\n"
    )
    parsed = PythonParser().parse("services/api/routes.py", source)

    assert [route for route, _ in parsed.routes] == ["POST /api/checkout"]
    handler = next(
        symbol for symbol in parsed.symbols if symbol.name == "post_checkout"
    )
    assert handler.route == "POST /api/checkout"
    assert handler.http_method == "POST"
    assert handler.symbol_type.value == "ROUTE"


def test_python_parser_does_not_invent_routes_for_ordinary_calls():
    parsed = PythonParser().parse(
        "shop/cache.py", "def get_key():\n    return cache.get('k')\n"
    )

    assert parsed.routes == []


def test_python_parser_marks_a_syntax_error_as_failed():
    parsed = PythonParser().parse("broken.py", "def broken(:\n    pass\n")

    assert parsed.status is ParseStatus.FAILED
    assert parsed.error
    assert parsed.symbols == []


def test_parse_source_dispatches_by_language_and_says_how_it_read_it():
    python = parse_source("shop/checkout.py", SAMPLE_FILES["shop/checkout.py"])
    assert python.metadata.get("parser") != HEURISTIC_PARSER_NOTE

    typescript = parse_source(
        "web/checkout.ts",
        "export class Client {\n  async post(path: string) {\n    return path;\n  }\n}\n",
    )
    assert typescript.metadata.get("parser") == HEURISTIC_PARSER_NOTE
    assert typescript.status in (ParseStatus.PARSED, ParseStatus.PARTIAL)
    assert any(symbol.name == "Client" for symbol in typescript.symbols)

    unknown = parse_source("Makefile", "all:\n\techo hi\n")
    assert unknown.status is ParseStatus.UNSUPPORTED
    assert unknown.symbols == []


# ---------------------------------------------------------------------------
# Symbols and the code graph (§13, §14)
# ---------------------------------------------------------------------------
async def test_knowledge_service_follows_the_call_chain_across_files(
    db_session, tmp_path
):
    _, snapshot = await _indexed(db_session, tmp_path)
    knowledge = CodeKnowledgeService(db_session)

    process = await _symbol(knowledge, snapshot.id, "CheckoutService.process")
    #: Each edge is a ``(relationship, symbol)`` pair: the confidence lives on the
    #: edge, which is why the API can show how sure each link is.
    callees = {
        (edge, symbol.qualified_name)
        for edge, symbol in await knowledge.find_callees(process.id, limit=20)
    }
    assert any(name.endswith("reserve") for _, name in callees)
    assert all(edge.confidence > 0 for edge, _ in callees)

    reserve = await _symbol(knowledge, snapshot.id, "reserve")
    deeper = {
        symbol.qualified_name
        for _, symbol in await knowledge.find_callees(reserve.id, limit=20)
    }
    assert any(
        "fetch_stock" in name for name in deeper
    ), "reserve() calls the repository, so the chain must cross files"

    fetch = await _symbol(knowledge, snapshot.id, "fetch_stock")
    callers = {
        symbol.qualified_name
        for _, symbol in await knowledge.find_callers(fetch.id, limit=20)
    }
    assert any(name.endswith("reserve") for name in callers)

    references = await knowledge.find_references(snapshot.id, "fetch_stock", limit=20)
    assert references and all(reference.line >= 1 for reference in references)


async def test_code_search_finds_the_source_line_and_stays_bounded(
    db_session, tmp_path
):
    _, snapshot = await _indexed(db_session, tmp_path)
    knowledge = CodeKnowledgeService(db_session)

    found = await knowledge.search_code(snapshot.id, "DB_TIMEOUT_SECONDS", limit=5)
    paths = {item.file_path for item in found["symbols"] + found["sources"]}
    assert "shop/inventory.py" in paths

    empty = await knowledge.search_code(snapshot.id, "zzz_no_such_text", limit=5)
    assert (
        empty["symbols"] == [] and empty["sources"] == [] and empty["references"] == []
    )

    capped = await knowledge.search_code(snapshot.id, "a", limit=3)
    assert len(capped["symbols"]) <= 3


async def test_symbol_lookup_is_scoped_to_one_snapshot(
    db_session, tmp_path, tmp_path_factory
):
    """A symbol from another revision must never satisfy a query for this one."""
    project, snapshot = await _indexed(db_session, tmp_path)

    other_root = tmp_path_factory.mktemp("other")
    _write_repo(other_root, message="second repository")
    repository = await _repository(db_session, project, str(other_root))
    other = await _index(db_session, repository)

    assert other.id != snapshot.id
    in_first = await _symbol(
        CodeKnowledgeService(db_session), snapshot.id, "CheckoutService.process"
    )
    assert in_first.snapshot_id == snapshot.id

    #: The same symbol exists in both revisions, but each query returns its own.
    in_other = await _symbol(
        CodeKnowledgeService(db_session), other.id, "CheckoutService.process"
    )
    assert in_other.snapshot_id == other.id
    assert in_other.id != in_first.id


async def test_every_indexed_symbol_lives_inside_its_file(db_session, tmp_path):
    """Line ranges are the phase's most-clicked claim; they must be real."""
    _, snapshot = await _indexed(db_session, tmp_path)
    from app.models.code import CodeSymbol

    files = {
        row.path: row.line_count
        for row in (
            await db_session.execute(
                select(CodeFile).where(CodeFile.snapshot_id == snapshot.id)
            )
        )
        .scalars()
        .all()
    }
    symbols = (
        (
            await db_session.execute(
                select(CodeSymbol).where(CodeSymbol.snapshot_id == snapshot.id)
            )
        )
        .scalars()
        .all()
    )

    assert symbols
    for symbol in symbols:
        assert symbol.file_path in files
        assert 1 <= symbol.start_line <= symbol.end_line <= files[symbol.file_path], (
            f"{symbol.qualified_name} claims {symbol.start_line}-{symbol.end_line} "
            f"in a {files[symbol.file_path]}-line file"
        )


# ---------------------------------------------------------------------------
# Trace mapping (§15, §16, §17)
# ---------------------------------------------------------------------------
def test_stack_trace_analyzer_parses_python_frames():
    trace = StackTraceAnalyzer().parse(
        "Traceback (most recent call last):\n"
        '  File "/srv/app/shop/checkout.py", line 21, in process\n'
        "    return await self.reserve(sku, quantity)\n"
        '  File "/srv/app/shop/inventory.py", line 25, in _query\n'
        '    raise TimeoutError("inventory database query timed out")\n'
        "TimeoutError: inventory database query timed out\n"
    )

    assert trace is not None
    assert trace.exception_type == "TimeoutError"
    assert len(trace.frames) == 2
    assert trace.frames[0].file_path == "/srv/app/shop/checkout.py"
    assert trace.frames[0].line == 21
    assert trace.frames[0].function == "process"
    assert trace.frames[1].file_path == "/srv/app/shop/inventory.py"
    assert trace.format == "python"


def test_stack_trace_analyzer_parses_javascript_frames():
    trace = StackTraceAnalyzer().parse(
        "TypeError: Cannot read properties of undefined\n"
        "    at CheckoutClient.post (/srv/app/web/checkout.ts:31:11)\n"
        "    at CheckoutClient.submit (/srv/app/web/checkout.ts:18:5)\n"
    )

    assert trace is not None
    assert [frame.file_path for frame in trace.frames] == [
        "/srv/app/web/checkout.ts",
        "/srv/app/web/checkout.ts",
    ]
    assert trace.frames[0].line == 31
    assert trace.frames[0].function == "CheckoutClient.post"
    assert trace.format == "javascript"


def test_stack_trace_analyzer_returns_none_for_text_that_is_not_a_trace():
    """``None`` and \"no frames\" are different answers; this is the former."""
    analyzer = StackTraceAnalyzer()

    assert analyzer.parse("nothing to see here") is None
    assert analyzer.parse("") is None
    assert analyzer.parse(None) is None


def test_a_frame_is_a_location_and_never_a_verdict():
    """§17: the top frame is not automatically the origin of the failure."""
    trace = StackTraceAnalyzer().parse(
        '  File "/srv/app/shop/inventory.py", line 25, in _query\n'
    )

    assert trace is not None
    frame = trace.frames[0]
    assert frame.file_path.endswith("shop/inventory.py")
    assert not hasattr(frame, "is_root_cause")
    assert not hasattr(frame, "confidence")


# ---------------------------------------------------------------------------
# Regressions for the defects the live gate found
# ---------------------------------------------------------------------------
async def test_indexing_records_each_files_last_change(db_session, tmp_path):
    """Regression: ``code_files.last_commit_sha`` was never written at all.

    The column, the API field and the code viewer's \"last changed\" line all
    existed while the indexer left them null, so ARGUS reported \"no commit
    information\" for every file in a repository that had a full history.
    """
    project, snapshot = await _indexed(db_session, tmp_path)
    head = await _head(tmp_path)

    rows = (
        (
            await db_session.execute(
                select(CodeFile).where(CodeFile.snapshot_id == snapshot.id)
            )
        )
        .scalars()
        .all()
    )

    assert rows
    assert {row.last_commit_sha for row in rows} == {head}
    assert {row.last_author for row in rows} == {"ARGUS Fixture"}
    assert all(row.last_modified_at is not None for row in rows)


async def test_indexing_does_not_invent_commit_attribution_without_vcs(
    db_session, tmp_path
):
    """No VCS: the columns stay null instead of borrowing the snapshot's revision."""
    project = await _project(db_session)
    repository = await _plain_repository(db_session, project, str(tmp_path))
    snapshot = await _index(db_session, repository)

    rows = (
        (
            await db_session.execute(
                select(CodeFile).where(CodeFile.snapshot_id == snapshot.id)
            )
        )
        .scalars()
        .all()
    )

    assert rows
    assert {row.last_commit_sha for row in rows} == {None}
    assert {row.last_author for row in rows} == {None}


async def test_incremental_reuse_keeps_the_attribution_it_copied(db_session, tmp_path):
    project, snapshot = await _indexed(db_session, tmp_path)
    original = {
        row.path: row.last_commit_sha
        for row in (
            await db_session.execute(
                select(CodeFile).where(CodeFile.snapshot_id == snapshot.id)
            )
        )
        .scalars()
        .all()
    }

    _append(tmp_path, "shop/inventory.py", "\n\n# tuned again\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "tune the inventory module again")

    from app.services.code_index_service import CodeIndexer
    from app.services.code_snapshot_service import CodeSnapshotService

    repository = await _repository_of(db_session, snapshot)
    assert repository is not None
    second = await CodeSnapshotService().get_or_create_snapshot(
        db_session, repository, None, version_evidence="second revision"
    )
    run = await CodeIndexer(db_session).index(repository, second, trigger="test")

    metadata = dict(run.run_metadata or {})
    assert (
        metadata.get("files_reused", 0) > 0
    ), "the incremental pass must reuse unchanged files"
    assert run.base_commit_sha == snapshot.commit_sha
    reused = {
        row.path: row.last_commit_sha
        for row in (
            await db_session.execute(
                select(CodeFile).where(CodeFile.snapshot_id == second.id)
            )
        )
        .scalars()
        .all()
    }
    for path, sha in reused.items():
        assert sha, f"{path} lost its attribution across the incremental pass"
    assert reused["shop/checkout.py"] == original["shop/checkout.py"]


async def test_an_explicitly_pinned_revision_is_recorded_as_resolved(
    client, db_session
):
    """Regression: a caller-pinned commit was recorded as an UNKNOWN version."""
    fixture = await _indexed_via_api(client, db_session)

    response = client.post(
        f"/api/v1/projects/{fixture.project_id}/repositories/{fixture.repository_id}/index",
        json={"reference": fixture.snapshot["commit_sha"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["snapshot"]["version_status"] == "RESOLVED"
    assert fixture.snapshot["commit_sha"][:12] in body["snapshot"]["version_evidence"]


async def test_an_unresolvable_revision_is_recorded_as_unknown(client, db_session):
    fixture = await _indexed_via_api(client, db_session)

    response = client.post(
        f"/api/v1/projects/{fixture.project_id}/repositories/{fixture.repository_id}/index",
        json={"reference": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["snapshot"]["version_status"] == "UNKNOWN"
    assert "not present" in body["snapshot"]["version_evidence"]


async def test_diff_refuses_a_revision_the_repository_does_not_have(client, db_session):
    """Regression: an unknown base returned ``200`` with an empty diff.

    \"Nothing changed between these revisions\" and \"that revision does not exist\"
    are different answers, and the first one was being given for the second.
    """
    fixture = await _indexed_via_api(client, db_session)
    snapshot = fixture.snapshot
    base = f"/api/v1/projects/{fixture.project_id}/repositories/{fixture.repository_id}"

    response = client.get(
        f"{base}/diff",
        params={
            "base": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "head": snapshot["commit_sha"],
        },
    )
    assert response.status_code == 422
    assert "does not exist" in response.json()["detail"]

    response = client.get(
        f"{base}/diff", params={"base": snapshot["commit_sha"], "head": "nope"}
    )
    assert response.status_code == 422

    #: The genuine comparison still works.
    response = client.get(
        f"{base}/diff",
        params={"base": snapshot["commit_sha"], "head": snapshot["commit_sha"]},
    )
    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_history_and_blame_refuse_an_unknown_revision(client, db_session):
    fixture = await _indexed_via_api(client, db_session)
    base = f"/api/v1/projects/{fixture.project_id}/repositories/{fixture.repository_id}"

    assert (
        client.get(f"{base}/history", params={"reference": "deadbeef"}).status_code
        == 422
    )
    assert (
        client.get(
            f"{base}/blame",
            params={"path": "shop/checkout.py", "reference": "deadbeef"},
        ).status_code
        == 422
    )
    #: ``HEAD`` stays allowed: it is a name the provider resolves itself.
    assert (
        client.get(f"{base}/history", params={"reference": "HEAD"}).status_code == 200
    )


async def test_file_metadata_is_served_from_the_snapshot(client, db_session):
    """The code viewer's \"last changed\" line must not be empty in a real repo."""
    fixture = await _indexed_via_api(client, db_session)

    files = client.get(f"/api/v1/snapshots/{fixture.snapshot['id']}/files").json()[
        "items"
    ]
    inventory = next(item for item in files if item["path"] == "shop/inventory.py")

    assert inventory["last_commit_sha"] == fixture.snapshot["commit_sha"]
    assert inventory["last_author"] == "ARGUS Fixture"
    assert inventory["last_modified_at"] is not None
    assert inventory["parse_status"] == "PARSED"


async def test_stored_content_hash_matches_the_source_on_disk(client, db_session):
    """Reuse is decided by content hash, so a wrong hash silently re-parses or
    worsens: a file the hashes disagree about is parsed again (slow), and a file
    the hashes agree about while the content differs would be *reused* (wrong)."""
    from app.models.code import CodeFile as Model

    fixture = await _indexed_via_api(client, db_session)
    rows = (
        (
            await db_session.execute(
                select(Model).where(Model.snapshot_id == fixture.snapshot["id"])
            )
        )
        .scalars()
        .all()
    )

    assert rows
    for row in rows:
        assert row.content_hash, f"{row.path} has no content hash"
        #: Two legitimate schemes, and the format tells them apart: a git provider
        #: hashes cheaply with the blob id (sha1), a plain directory with sha256 of
        #: the bytes. What matters is that the stored hash is derived from the
        #: content — a hash that is not would make reuse copy the wrong file.
        blob = subprocess.run(
            ["git", "-C", fixture.root, "hash-object", row.path],
            capture_output=True,
            text=True,
        )
        expected = {
            hashlib.sha256(SAMPLE_FILES[row.path].encode("utf-8")).hexdigest(),
        }
        if blob.returncode == 0:
            expected.add(blob.stdout.strip())
        assert (
            row.content_hash in expected
        ), f"{row.path} hash {row.content_hash} matches neither its bytes nor its blob"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _project(session):
    project, _, _ = await build_project(session, name="Phase6 Code")
    return project


async def _repository(session, project, root):
    from app.models.deployment import CodeRepository, RepositoryIndexStatus

    repository = CodeRepository(
        project_id=project.id,
        provider="local",
        repository_url=root,
        local_path=root,
        default_branch="main",
        language="python",
        index_status=RepositoryIndexStatus.PENDING,
    )
    session.add(repository)
    await session.flush()
    return repository


async def _plain_repository(session, project, root):
    for path, content in SAMPLE_FILES.items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as handle:
            handle.write(content)
    return await _repository(session, project, root)


async def _index(session, repository):
    from app.services.code_index_service import CodeIndexer
    from app.services.code_snapshot_service import CodeSnapshotService

    snapshot = await CodeSnapshotService().get_or_create_snapshot(
        session, repository, None, version_evidence="fixture"
    )
    await CodeIndexer(session).index(repository, snapshot, trigger="test")
    return snapshot


async def _indexed(session, root, *, message: str = "initial"):
    project = await _project(session)
    _write_repo(root, message=message)
    repository = await _repository(session, project, str(root))
    snapshot = await _index(session, repository)
    return project, snapshot


async def _repository_of(session, snapshot):
    from app.models.deployment import CodeRepository

    return await session.get(CodeRepository, snapshot.repository_id)


async def _symbol(knowledge, snapshot_id, needle: str):
    found = await knowledge.find_symbol(snapshot_id, needle.split(".")[-1], limit=50)
    for symbol in found:
        if symbol.qualified_name.endswith(needle):
            return symbol
    raise AssertionError(f"{needle} was not indexed")


class _IndexedViaApi(NamedTuple):
    """What an HTTP-registered fixture exposes to a test."""

    project_id: str
    snapshot: dict
    repository_id: object
    root: str


async def _indexed_via_api(client, session) -> _IndexedViaApi:
    """Register and index the fixture through HTTP, as the smoke gate does."""
    import tempfile

    project, _, _ = await build_project(session, name="Phase6 API Code")
    await session.commit()

    root = tempfile.mkdtemp(prefix="phase6-api-")
    _write_repo(root)

    response = client.post(
        f"/api/v1/projects/{project.id}/repositories",
        json={"provider": "local", "repository_url": root, "default_branch": "main"},
    )
    assert response.status_code == 200, response.text
    repository_id = response.json()["id"]

    response = client.post(
        f"/api/v1/projects/{project.id}/repositories/{repository_id}/index",
        json={},
    )
    assert response.status_code == 200, response.text
    return _IndexedViaApi(
        str(project.id), response.json()["snapshot"], repository_id, root
    )


def _write_repo(root, *, message: str = "initial") -> None:
    root = str(root)
    for path, content in SAMPLE_FILES.items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as handle:
            handle.write(content)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "fixture@argus")
    _git(root, "config", "user.name", "ARGUS Fixture")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def _append(root, path: str, text: str) -> None:
    with open(os.path.join(str(root), path), "a") as handle:
        handle.write(text)


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


async def _head(root) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
