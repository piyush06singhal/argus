"""ARGUS Repository Indexer (Phase 6 §9, §54, §55).

Turns a repository snapshot into stored code intelligence: files, symbols, name
occurrences, resolved edges, and deterministic risk signals.

Four ideas carry the correctness and performance story:

**Incremental by content hash, never by timestamp.** A file is reused from the
base snapshot when its content hash matches — the git blob id when the provider
supplies one, sha256 otherwise. Timestamps lie (a checkout rewrites every mtime
without changing a byte; a rebase changes bytes while mtimes can stay put), and
an index that silently skips a changed file produces an analysis against code
that was never deployed.

**Reused files are reconstructed, not re-parsed.** Their stored symbols,
references and source are read back into the same in-memory shape the parser
produces, so the graph builder sees the *whole* snapshot and the call graph stays
complete. Skipping the parse is the saving; skipping the resolution would break
every edge that crosses an unchanged file — which is nearly all of them.

**Resolution happens before persistence, not after.** Symbols are stored first,
the graph is built over the whole snapshot in memory, and only then are
references written *with* the definition they bound to and the strategy that
bound them. Writing references first and patching them afterwards would either
cost a second pass of row updates or leave the resolution unstored.

**Partial results are recorded, not hidden.** A file that is too large, in an
unsupported language, or syntactically broken is stored with its parse status and
reason. A heuristic (JS/TS) parse is *not* degradation — it is the declared shape
of that snapshot and is reported as a count. Only a real failure moves the
snapshot to ``PARTIAL``, so the warning means something.
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import uuid as uuid_module
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.code import (
    CodeFile,
    CodeIndexRun,
    CodeReference,
    CodeRelationship,
    CodeRiskSignal,
    CodeSymbol,
    CodeSymbolType,
    DebugAnalysisStatus,
    ParseStatus,
    RepositoryIndexStatus,
    RepositorySnapshot,
    RiskSignalType,
    SnapshotStatus,
)
from app.models.deployment import CodeRepository
from app.services.code_graph import (
    CodeGraphBuilder,
    CodeGraphResult,
    NOT_ATTEMPTED,
    ResolvedReference,
)
from app.services.code_parser import (
    PARSER_VERSION,
    HEURISTIC_PARSER_NOTE,
    ParsedFile,
    ParsedReference,
    ParsedSymbol,
    parse_source,
    supported_languages,
)
from app.services.repository_provider import (
    CommitInfo,
    FileEntry,
    ProviderError,
    RepositoryError,
    provider_for_repository,
)

logger = logging.getLogger(__name__)
settings = get_settings()

#: Rows per executemany statement. Large enough that the per-statement overhead
#: disappears, small enough that one statement never becomes a memory spike.
INSERT_CHUNK_ROWS = 500

#: Frameworks worth naming in the repository row, keyed by the dependency token
#: that implies them. Detection is a plain manifest scan — a *label* for the UI,
#: never an input to a hypothesis.
FRAMEWORK_TOKENS = (
    ("fastapi", "fastapi"),
    ("django", "django"),
    ("flask", "flask"),
    ("starlette", "starlette"),
    ("express", "express"),
    ("nestjs", "@nestjs/core"),
    ("next", "next"),
    ("react", "react"),
    ("vue", "vue"),
    ("svelte", "svelte"),
    ("sqlalchemy", "sqlalchemy"),
    ("prisma", "@prisma/client"),
)

#: Manifest files scanned for framework detection.
MANIFEST_FILES = frozenset(
    {
        "requirements.txt",
        "pyproject.toml",
        "setup.py",
        "package.json",
        "go.mod",
        "cargo.toml",
        "pom.xml",
    }
)


@dataclass
class IndexStats:
    """Everything one indexing pass did — stored on the run row."""

    files_seen: int = 0
    files_indexed: int = 0
    files_reused: int = 0
    files_added: int = 0
    files_modified: int = 0
    files_deleted: int = 0
    #: Files whose contents could not be indexed at all.
    files_failed: int = 0
    #: Files read but not fully interpreted (a real partial parse, not the
    #: heuristic JS/TS scanner).
    files_partial: int = 0
    #: Files handled by the JS/TS structural scanner — this snapshot's normal
    #: state for those languages, reported so the number is visible rather than
    #: silently indistinguishable from a failure.
    files_heuristic: int = 0
    symbols: int = 0
    references: int = 0
    relationships: int = 0
    resolved_references: int = 0
    unresolved_references: int = 0
    not_attempted_references: int = 0
    by_resolution: dict = field(default_factory=dict)
    by_kind: dict = field(default_factory=dict)
    languages: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    reused_paths: list = field(default_factory=list)
    deleted_paths: list = field(default_factory=list)
    truncated: bool = False

    def merge_graph(self, graph: CodeGraphResult) -> None:
        self.resolved_references = graph.stats.resolved_references
        self.unresolved_references = graph.stats.unresolved_references
        self.not_attempted_references = graph.stats.not_attempted
        self.by_resolution = dict(graph.stats.by_resolution)
        self.by_kind = dict(graph.stats.by_kind)

    def resolution_ratio(self) -> float:
        total = self.resolved_references + self.unresolved_references
        return (self.resolved_references / total) if total else 0.0


class CodeIndexer:
    """Indexes one snapshot.

    Safe to re-run: a re-index clears the snapshot's existing rows first, so
    running it twice cannot duplicate symbols or leave orphaned edges behind.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    async def index(
        self,
        repository: CodeRepository,
        snapshot: RepositorySnapshot,
        *,
        trigger: str = "api",
        requested_by: Optional[str] = None,
        incremental: Optional[bool] = None,
        max_files: Optional[int] = None,
    ) -> CodeIndexRun:
        """Index ``snapshot`` and return the completed run row."""
        started = datetime.now(timezone.utc)
        run = CodeIndexRun(
            project_id=repository.project_id,
            repository_id=repository.id,
            snapshot_id=snapshot.id,
            status=DebugAnalysisStatus.RUNNING,
            trigger=trigger,
            requested_by=requested_by,
            incremental=False,
            started_at=started,
        )
        self.session.add(run)
        await self.session.flush()
        stats = IndexStats()

        try:
            provider = provider_for_repository(repository)
            entries = await provider.list_files(snapshot.commit_sha)
        except (RepositoryError, ProviderError) as exc:
            return await self._fail(run, snapshot, repository, str(exc))
        except Exception as exc:  # noqa: BLE001 - a provider bug must not 500
            return await self._fail(run, snapshot, repository, repr(exc))

        limit = (
            max_files if max_files is not None else settings.CODE_MAX_FILES_PER_SNAPSHOT
        )
        stats.truncated = len(entries) > limit
        entries = entries[:limit]
        stats.files_seen = len(entries)

        base = await self._base_snapshot(snapshot)
        #: Re-indexing the *same* revision with the same parser has nothing to do:
        #: the snapshot's rows already are the answer, and the previous pass's
        #: output is byte-for-byte what this pass would write. Rewriting them costs
        #: a full parse — the exact work §54 exists to remove — so the pass is
        #: reported as an incremental one that reused everything. ``incremental=False``
        #: still forces the full pass, which is the escape hatch after a parser
        #: upgrade; the recorded ``parser_version`` is what makes that distinction
        #: observable rather than a guess.
        already_indexed = await self._indexed_file_count(snapshot)
        if (
            incremental is not False
            and already_indexed
            and (snapshot.index_metadata or {}).get("parser_version") == PARSER_VERSION
            and len(entries) == already_indexed
        ):
            return await self._finish_unchanged_revision(
                run, repository, snapshot, entries, already_indexed
            )

        #: Incremental is the default when a base exists, because the alternative
        #: (re-parsing every file on every commit) is what makes indexing too slow
        #: to run per-deployment. ``incremental=False`` forces a full pass.
        use_incremental = (
            incremental is not False
            and base is not None
            and base.commit_sha != snapshot.commit_sha
            and len(entries) <= settings.CODE_MAX_FILES_PER_SNAPSHOT
        )
        run.incremental = use_incremental
        run.base_commit_sha = base.commit_sha if use_incremental and base else None

        reusable = await self._reusable_files(base) if use_incremental else {}
        diff = await self._diff_paths(repository, base, snapshot, entries)
        #: Per-file last-change attribution, in one provider call. Best-effort by
        #: design: a provider without VCS cannot answer, and a failure here must
        #: not cost the whole index — the columns stay null and say nothing,
        #: which is the honest state for "we could not determine this".
        try:
            file_commits = await provider.file_commits(
                [entry.path for entry in entries], snapshot.commit_sha
            )
        except (RepositoryError, ProviderError):
            file_commits = {}

        await self._clear_snapshot(snapshot)

        #: One batched read for every file that needs parsing. Reading them one at
        #: a time spent 35s of a 76s index waiting on `git` process startup rather
        #: than doing any work (measured), because ``parse_source`` for the whole
        #: repository is under 3s. Files that will be reused from the base snapshot
        #: are not read at all.
        needs_read = [
            entry
            for entry in entries
            if not _reusable(entry, reusable)
            and entry.size_bytes <= settings.CODE_MAX_FILE_BYTES
        ]
        sources = await provider.read_files(
            [entry.path for entry in needs_read], snapshot.commit_sha
        )

        parsed_files: list[ParsedFile] = []
        symbol_ids: dict[str, object] = {}
        replaced_paths: list[str] = []

        for entry in entries:
            try:
                parsed = await self._index_one(
                    repository,
                    snapshot,
                    base,
                    entry,
                    reusable,
                    symbol_ids,
                    sources,
                    file_commits.get(entry.path),
                )
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
                stats.files_failed += 1
                if len(stats.errors) < 50:
                    stats.errors.append({"path": entry.path, "error": repr(exc)[:300]})
                logger.warning("failed to index %s: %r", entry.path, exc)
                continue
            if parsed is None:
                continue
            metadata = parsed.metadata or {}
            if metadata.get("reused"):
                stats.files_reused += 1
                stats.reused_paths.append(entry.path)
            else:
                stats.files_indexed += 1
                if metadata.get("replaced_existing"):
                    #: A file the base revision also had, re-parsed because its
                    #: content differs: that is a modification, observed directly
                    #: rather than inferred by a provider that may not be able to
                    #: diff (a plain directory has no VCS to ask).
                    replaced_paths.append(entry.path)
            parsed_files.append(parsed)
            stats.symbols += len(parsed.symbols)
            language = parsed.language or "unknown"
            stats.languages[language] = stats.languages.get(language, 0) + 1
            heuristic = (parsed.metadata or {}).get("parser") == HEURISTIC_PARSER_NOTE
            if heuristic:
                stats.files_heuristic += 1
            if parsed.status is ParseStatus.FAILED or (
                parsed.status is ParseStatus.PARTIAL and not heuristic
            ):
                stats.files_partial += 1
                if len(stats.errors) < 50:
                    stats.errors.append(
                        {
                            "path": entry.path,
                            "status": parsed.status.value,
                            "error": parsed.error or "parsed partially",
                        }
                    )

        stats.files_added = len(diff.get("added", []))
        stats.files_modified = len(diff.get("modified", []))
        stats.files_deleted = len(diff.get("deleted", []))
        if (
            diff.get("source") != "provider"
            and not stats.files_modified
            and replaced_paths
        ):
            #: Neither the provider diff nor the hash comparison could classify
            #: the change (no VCS, no hashes from the listing), so the files this
            #: pass actually re-parsed count as modified. Reporting zero while
            #: visibly rewriting four files would be a worse answer.
            stats.files_modified = len(replaced_paths)
        stats.deleted_paths = diff.get("deleted", [])

        # ---- resolve, then persist the references and the graph -----------
        graph = CodeGraphBuilder(parsed_files).build()
        stats.merge_graph(graph)
        stats.references = await self._persist_references(snapshot, graph, symbol_ids)
        stats.relationships = await self._persist_graph(snapshot, graph, symbol_ids)
        await self._persist_risk_signals(
            repository, snapshot, parsed_files, graph, symbol_ids
        )

        await self._finalize(
            run,
            snapshot,
            repository,
            stats,
            started,
            parser_partial=bool(stats.files_partial or stats.files_failed),
        )
        return run

    # ------------------------------------------------------------------
    # Pre-flight helpers
    # ------------------------------------------------------------------
    async def _indexed_file_count(self, snapshot: RepositorySnapshot) -> int:
        """How many files this snapshot currently holds."""
        return int(
            (
                await self.session.execute(
                    select(func.count(CodeFile.id)).where(
                        CodeFile.snapshot_id == snapshot.id
                    )
                )
            ).scalar_one()
            or 0
        )

    async def _finish_unchanged_revision(
        self,
        run: CodeIndexRun,
        repository: CodeRepository,
        snapshot: RepositorySnapshot,
        entries: list[FileEntry],
        file_count: int,
    ) -> CodeIndexRun:
        """Complete a run for a revision that is already indexed and unchanged.

        Every previously indexed file is reported as *reused*, which is what it
        is: the rows that would be produced already exist. Nothing is deleted or
        rewritten, so symbol and relationship ids stay stable across a re-index —
        which also means a stored debugging session's references keep resolving.
        """
        run.incremental = True
        run.base_commit_sha = snapshot.commit_sha
        run.files_seen = len(entries)
        run.files_indexed = 0
        run.symbols_indexed = snapshot.symbol_count or 0
        run.references_indexed = 0
        run.relationships_indexed = 0
        run.status = DebugAnalysisStatus.COMPLETED
        run.completed_at = datetime.now(timezone.utc)
        run.duration_ms = max(
            1, int((run.completed_at - run.started_at).total_seconds() * 1000)
        )
        run.run_metadata = {
            "files_reused": file_count,
            "reused_paths": [entry.path for entry in entries][:200],
            "deleted_paths": [],
            "by_resolution": {},
            "files_heuristic": 0,
            "unchanged_revision": True,
            "parser_version": PARSER_VERSION,
        }
        snapshot.status = (
            SnapshotStatus.READY
            if snapshot.status in (SnapshotStatus.CREATED, SnapshotStatus.INDEXING)
            else snapshot.status
        )
        snapshot.indexed_at = snapshot.indexed_at or run.completed_at
        await self.session.flush()
        logger.info(
            "revision %s of repository %s is already indexed; reusing %d files",
            (snapshot.commit_sha or "unversioned")[:12],
            repository.id,
            file_count,
        )
        return run

    async def _base_snapshot(
        self, snapshot: RepositorySnapshot
    ) -> Optional[RepositorySnapshot]:
        """The most recent earlier ready snapshot of the same repository.

        Only accepts one with a commit sha: a base without a revision cannot be
        diffed or matched by content hash, so reusing from it would mean guessing
        that files are unchanged.
        """
        stmt = (
            select(RepositorySnapshot)
            .where(
                RepositorySnapshot.repository_id == snapshot.repository_id,
                RepositorySnapshot.id != snapshot.id,
                RepositorySnapshot.commit_sha.is_not(None),
                RepositorySnapshot.status.in_(
                    [SnapshotStatus.READY, SnapshotStatus.PARTIAL]
                ),
            )
            .order_by(RepositorySnapshot.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def _reusable_files(
        self, base: Optional[RepositorySnapshot]
    ) -> dict[str, CodeFile]:
        """Base-snapshot files whose content hash can be trusted for reuse."""
        if base is None:
            return {}
        rows = (
            (
                await self.session.execute(
                    select(CodeFile).where(CodeFile.snapshot_id == base.id)
                )
            )
            .scalars()
            .all()
        )
        return {
            row.path: row
            for row in rows
            if row.content_hash
            and row.parse_status
            in (ParseStatus.PARSED, ParseStatus.PARTIAL, ParseStatus.UNSUPPORTED)
        }

    async def _diff_paths(
        self,
        repository: CodeRepository,
        base: Optional[RepositorySnapshot],
        snapshot: RepositorySnapshot,
        entries: list[FileEntry],
    ) -> dict:
        """Changed/added/deleted paths versus the base revision (§55).

        Prefers the provider's own diff and falls back to content hashes — both
        are real comparisons, and neither invents a change that did not happen.
        """
        if base is None or not base.commit_sha or not snapshot.commit_sha:
            return {"added": [], "modified": [], "deleted": [], "source": "none"}
        changes = []
        try:
            provider = provider_for_repository(repository)
            changes = await provider.diff(base.commit_sha, snapshot.commit_sha)
        except (RepositoryError, ProviderError):
            changes = []
        added: list[str] = []
        modified: list[str] = []
        deleted: list[str] = []
        for change in changes:
            if change.status == "ADDED":
                added.append(change.path)
            elif change.status == "DELETED":
                deleted.append(change.path)
            else:
                modified.append(change.path)
        if changes:
            return {
                "added": added,
                "modified": modified,
                "deleted": deleted,
                "source": "provider",
            }

        #: The provider could not diff (a plain directory, or a shallow clone), so
        #: the stored content hashes decide instead.
        current = {entry.path: entry.content_hash for entry in entries}
        base_rows = (
            await self.session.execute(
                select(CodeFile.path, CodeFile.content_hash).where(
                    CodeFile.snapshot_id == base.id
                )
            )
        ).all()
        base_map = {row[0]: row[1] for row in base_rows}
        for path, digest in current.items():
            if path not in base_map:
                added.append(path)
            elif digest and base_map[path] and digest != base_map[path]:
                modified.append(path)
        for path in base_map:
            if path not in current:
                deleted.append(path)
        return {"added": added, "modified": modified, "deleted": deleted}

    async def _clear_snapshot(self, snapshot: RepositorySnapshot) -> None:
        """Remove a previous indexing of this same snapshot, edges included."""
        await self.session.execute(
            delete(CodeRelationship).where(CodeRelationship.snapshot_id == snapshot.id)
        )
        for table in (CodeReference, CodeSymbol, CodeRiskSignal):
            await self.session.execute(
                delete(table).where(table.snapshot_id == snapshot.id)
            )
        await self.session.execute(
            delete(CodeFile).where(CodeFile.snapshot_id == snapshot.id)
        )
        await self.session.flush()

    # ------------------------------------------------------------------
    # Per-file indexing
    # ------------------------------------------------------------------
    async def _index_one(
        self,
        repository: CodeRepository,
        snapshot: RepositorySnapshot,
        base: Optional[RepositorySnapshot],
        entry: FileEntry,
        reusable: dict[str, CodeFile],
        symbol_ids: dict[str, object],
        sources: dict[str, str],
        commit: Optional[CommitInfo] = None,
    ) -> Optional[ParsedFile]:
        """Index one file; returns the ``ParsedFile`` the graph builder needs."""
        base_file = reusable.get(entry.path) if base is not None else None
        if _reusable(entry, reusable) and base_file is not None:
            return await self._reuse_file(repository, snapshot, base_file, symbol_ids)

        if entry.size_bytes > settings.CODE_MAX_FILE_BYTES:
            #: Stored but never read: the file exists, and pretending it does not
            #: would make a \"no code found here\" answer uncorrectable.
            reason = (
                f"file is {entry.size_bytes} bytes, above the "
                f"{settings.CODE_MAX_FILE_BYTES} byte indexing limit"
            )
            await self._store_file(
                repository,
                snapshot,
                entry,
                parse_status=ParseStatus.UNSUPPORTED,
                parse_error=reason,
                parser=None,
                commit=commit,
            )
            return ParsedFile(
                path=entry.path,
                language=entry.language or "unknown",
                status=ParseStatus.UNSUPPORTED,
                error=reason,
                metadata={"reason": "size-limit"},
            )

        source = sources.get(entry.path)
        if source is None:
            reason = "file could not be read from the provider"
            await self._store_file(
                repository,
                snapshot,
                entry,
                parse_status=ParseStatus.FAILED,
                parse_error=reason,
                parser=None,
                commit=commit,
            )
            return ParsedFile(
                path=entry.path,
                language=entry.language or "unknown",
                status=ParseStatus.FAILED,
                error=reason,
            )

        lines = source.splitlines()
        if len(lines) > settings.CODE_MAX_FILE_LINES:
            source = "\n".join(lines[: settings.CODE_MAX_FILE_LINES])
            lines = source.splitlines()

        content_hash = (
            entry.content_hash
            or hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()
        )
        #: Second chance at reuse, for providers that cannot hash cheaply.
        #: A git provider supplies blob ids up front, so the decision is made
        #: before reading. A plain directory supplies none, so the file has to be
        #: read — but the *parse* (the expensive part: AST build, symbol and
        #: reference extraction) is still skippable when the hash matches. Without
        #: this, `incremental=true` silently re-parses a whole repository for
        #: every non-VCS checkout.
        if base_file is not None and base_file.content_hash == content_hash:
            return await self._reuse_file(repository, snapshot, base_file, symbol_ids)
        replaced_existing = base_file is not None

        parsed = parse_source(entry.path, source, entry.language)
        code_file_id = await self._store_file(
            repository,
            snapshot,
            entry,
            line_count=len(lines),
            content_hash=content_hash,
            parse_status=parsed.status,
            parse_error=parsed.error,
            parser=(parsed.metadata or {}).get("parser"),
            commit=commit,
        )
        await self._store_symbols(
            repository, snapshot, code_file_id, parsed, symbol_ids
        )
        if replaced_existing:
            parsed.metadata["replaced_existing"] = True
        return parsed

    async def _store_file(
        self,
        repository: CodeRepository,
        snapshot: RepositorySnapshot,
        entry: FileEntry,
        *,
        line_count: int = 0,
        content_hash: Optional[str] = None,
        parse_status: ParseStatus = ParseStatus.PENDING,
        parse_error: Optional[str] = None,
        parser: Optional[str] = None,
        commit: Optional[CommitInfo] = None,
    ):
        """Insert one ``code_files`` row and return its id.

        ``commit`` is the newest commit that touched this path. It is recorded
        separately from the snapshot's own revision because they are different
        facts: an unchanged file's last change may be months older than the
        deployment that shipped it, and the "recently modified" signals are
        wrong if the two are conflated.
        """
        file_id = uuid_module.uuid4()
        row = {
            "id": file_id,
            "project_id": repository.project_id,
            "repository_id": repository.id,
            "snapshot_id": snapshot.id,
            "path": entry.path,
            "language": entry.language,
            "module_name": posixpath.splitext(entry.path)[0].replace("/", ".")[:512],
            "size_bytes": entry.size_bytes,
            "line_count": line_count,
            "content_hash": content_hash,
            "is_test": entry.is_test,
            "deleted": False,
            "parse_status": parse_status,
            "parse_error": parse_error,
            "last_commit_sha": commit.sha if commit else None,
            "last_modified_at": commit.committed_at if commit else None,
            "last_author": commit.author if commit else None,
            "file_metadata": {"is_test": entry.is_test, "parser": parser},
        }
        await self.session.execute(insert(CodeFile), [row])
        return file_id

    async def _store_symbols(
        self,
        repository: CodeRepository,
        snapshot: RepositorySnapshot,
        code_file_id,
        parsed: ParsedFile,
        symbol_ids: dict[str, object],
    ) -> None:
        """Bulk-insert a parsed file's symbol definitions."""
        if not parsed.symbols:
            return
        ids: list[object] = []
        rows: list[dict] = []
        for symbol in parsed.symbols:
            symbol_id = uuid_module.uuid4()
            ids.append(symbol_id)
            symbol_ids[symbol.qualified_name] = symbol_id
            rows.append(
                {
                    "id": symbol_id,
                    "project_id": repository.project_id,
                    "repository_id": repository.id,
                    "snapshot_id": snapshot.id,
                    "file_id": code_file_id,
                    "file_path": parsed.path,
                    "symbol_name": symbol.name[:512],
                    "qualified_name": symbol.qualified_name[:1024],
                    "symbol_type": symbol.symbol_type,
                    "language": parsed.language,
                    "start_line": max(symbol.start_line, 1),
                    "end_line": max(symbol.end_line, symbol.start_line),
                    "signature": symbol.signature or None,
                    "source": symbol.source,
                    "documentation": symbol.documentation,
                    "symbol_hash": _hash_text(symbol.source),
                    "parent_symbol_id": None,
                    "is_async": symbol.is_async,
                    "complexity": symbol.complexity,
                    "route": symbol.route[:512] if symbol.route else None,
                    "http_method": symbol.http_method,
                    "symbol_metadata": _safe_metadata(symbol.metadata),
                }
            )
        for index, symbol in enumerate(parsed.symbols):
            parent_index = symbol.parent_index
            if parent_index is not None and parent_index < index:
                rows[index]["parent_symbol_id"] = ids[parent_index]
        await self._bulk_insert(CodeSymbol, rows)

    async def _reuse_file(
        self,
        repository: CodeRepository,
        snapshot: RepositorySnapshot,
        base_file: CodeFile,
        symbol_ids: dict[str, object],
    ) -> Optional[ParsedFile]:
        """Copy an unchanged file's symbols into this snapshot and rebuild it.

        The reconstruction is what keeps the graph complete: the caller gets a
        ``ParsedFile`` identical in shape to a parsed one, so the graph builder
        resolves calls across unchanged files exactly as it would have.
        """
        old_symbols = (
            (
                await self.session.execute(
                    select(CodeSymbol)
                    .where(CodeSymbol.file_id == base_file.id)
                    #: Parent-before-child ordering: a nested definition never starts
                    #: before the one containing it, so ordering by line guarantees a
                    #: parent's new id exists when its child needs it.
                    .order_by(CodeSymbol.start_line, CodeSymbol.qualified_name)
                )
            )
            .scalars()
            .all()
        )
        old_references = (
            (
                await self.session.execute(
                    select(CodeReference).where(CodeReference.file_id == base_file.id)
                )
            )
            .scalars()
            .all()
        )

        new_file_id = uuid_module.uuid4()
        await self.session.execute(
            insert(CodeFile),
            [
                {
                    "id": new_file_id,
                    "project_id": repository.project_id,
                    "repository_id": repository.id,
                    "snapshot_id": snapshot.id,
                    "path": base_file.path,
                    "language": base_file.language,
                    "module_name": base_file.module_name,
                    "size_bytes": base_file.size_bytes,
                    "line_count": base_file.line_count,
                    "content_hash": base_file.content_hash,
                    "is_test": base_file.is_test,
                    "deleted": False,
                    "parse_status": base_file.parse_status,
                    "parse_error": base_file.parse_error,
                    "last_commit_sha": base_file.last_commit_sha,
                    "last_modified_at": base_file.last_modified_at,
                    "last_author": base_file.last_author,
                    "file_metadata": {
                        **(base_file.file_metadata or {}),
                        "reused_from_snapshot": str(base_file.snapshot_id),
                    },
                }
            ],
        )

        new_by_old_id: dict[object, object] = {}
        rows: list[dict] = []
        for old in old_symbols:
            symbol_id = uuid_module.uuid4()
            new_by_old_id[old.id] = symbol_id
            symbol_ids[old.qualified_name] = symbol_id
            rows.append(
                {
                    "id": symbol_id,
                    "project_id": repository.project_id,
                    "repository_id": repository.id,
                    "snapshot_id": snapshot.id,
                    "file_id": new_file_id,
                    "file_path": old.file_path,
                    "symbol_name": old.symbol_name,
                    "qualified_name": old.qualified_name,
                    "symbol_type": old.symbol_type,
                    "language": old.language,
                    "start_line": old.start_line,
                    "end_line": old.end_line,
                    "signature": old.signature,
                    "source": old.source,
                    "documentation": old.documentation,
                    "symbol_hash": old.symbol_hash,
                    "parent_symbol_id": None,
                    "is_async": old.is_async,
                    "complexity": old.complexity,
                    "route": old.route,
                    "http_method": old.http_method,
                    "symbol_metadata": old.symbol_metadata,
                }
            )
        for index, old in enumerate(old_symbols):
            if old.parent_symbol_id in new_by_old_id:
                rows[index]["parent_symbol_id"] = new_by_old_id[old.parent_symbol_id]
        await self._bulk_insert(CodeSymbol, rows)

        #: References are re-inserted here (rather than during the resolve pass)
        #: so ``find_references`` works on a reused file too; their resolutions
        #: are recomputed by the graph builder and written in the second pass,
        #: which is why the copied rows carry no target.
        if old_references:
            await self._bulk_insert(
                CodeReference,
                [
                    {
                        "id": uuid_module.uuid4(),
                        "project_id": repository.project_id,
                        "snapshot_id": snapshot.id,
                        "file_id": new_file_id,
                        "file_path": old.file_path,
                        "name": old.name,
                        "reference_kind": old.reference_kind,
                        "line": old.line,
                        "column": old.column,
                        "enclosing_symbol_id": new_by_old_id.get(
                            old.enclosing_symbol_id
                        ),
                        "symbol_id": None,
                        "resolution": old.resolution,
                        "reference_metadata": old.reference_metadata,
                    }
                    for old in old_references
                ],
            )

        return _reconstruct_parsed_file(base_file, old_symbols, old_references)

    # ------------------------------------------------------------------
    # Resolved references and graph edges
    # ------------------------------------------------------------------
    async def _persist_references(
        self,
        snapshot: RepositorySnapshot,
        graph: CodeGraphResult,
        symbol_ids: dict[str, object],
    ) -> int:
        """Write the resolved occurrences, replacing the rows for reused files.

        Occurrences from freshly parsed files have no row yet; occurrences from
        reused files were copied with their resolution blank. Both are handled by
        one delete-and-insert per file path, which keeps the two paths from having
        to agree about which rows already exist.
        """
        by_path: dict[str, list[ResolvedReference]] = {}
        for reference in graph.references:
            by_path.setdefault(reference.file_path, []).append(reference)
        if not by_path:
            return 0

        file_ids: dict[str, object] = {
            row[0]: row[1]
            for row in (
                await self.session.execute(
                    select(CodeFile.path, CodeFile.id).where(
                        CodeFile.snapshot_id == snapshot.id
                    )
                )
            ).all()
        }
        written = 0
        for path, references in by_path.items():
            file_id = file_ids.get(path)
            if file_id is None:
                continue
            await self.session.execute(
                delete(CodeReference).where(
                    CodeReference.snapshot_id == snapshot.id,
                    CodeReference.file_path == path,
                )
            )
            rows = []
            for reference in references:
                rows.append(
                    {
                        "id": uuid_module.uuid4(),
                        "project_id": snapshot.project_id,
                        "snapshot_id": snapshot.id,
                        "file_id": file_id,
                        "file_path": reference.file_path,
                        "name": reference.name[:512],
                        "reference_kind": reference.reference_kind,
                        "line": reference.line,
                        "column": reference.column,
                        "enclosing_symbol_id": symbol_ids.get(
                            reference.enclosing_qualified or ""
                        ),
                        "symbol_id": symbol_ids.get(reference.target_qualified or ""),
                        "resolution": reference.resolution,
                        "reference_metadata": _safe_metadata(reference.metadata)
                        or {"not_attempted": reference.resolution == NOT_ATTEMPTED},
                    }
                )
            await self._bulk_insert(CodeReference, rows)
            written += len(rows)
        return written

    async def _persist_graph(
        self,
        snapshot: RepositorySnapshot,
        graph: CodeGraphResult,
        symbol_ids: dict[str, object],
    ) -> int:
        """Store resolved edges, skipping any whose endpoint has no symbol row.

        A missing endpoint means the target lives outside the snapshot (a library,
        a builtin), which is expected — and must not become a dangling edge.
        """
        rows: list[dict] = []
        seen: set[tuple] = set()
        for edge in graph.relationships:
            source_id = symbol_ids.get(edge.source_qualified)
            target_id = symbol_ids.get(edge.target_qualified)
            if source_id is None or target_id is None:
                continue
            key = (
                str(source_id),
                str(target_id),
                edge.relationship_type.value,
                edge.line,
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "id": uuid_module.uuid4(),
                    "project_id": snapshot.project_id,
                    "snapshot_id": snapshot.id,
                    "source_symbol_id": source_id,
                    "target_symbol_id": target_id,
                    "relationship_type": edge.relationship_type,
                    "line": edge.line or 0,
                    "confidence": edge.confidence,
                    "evidence": edge.evidence,
                    "component_id": None,
                    "relationship_metadata": _safe_metadata(edge.metadata),
                }
            )
        await self._bulk_insert(CodeRelationship, rows)
        return len(rows)

    async def _persist_risk_signals(
        self,
        repository: CodeRepository,
        snapshot: RepositorySnapshot,
        parsed_files: list[ParsedFile],
        graph: CodeGraphResult,
        symbol_ids: dict[str, object],
    ) -> int:
        """Write deterministic investigation signals (§44).

        Only signals *measurable from the snapshot* are written. Signals needing
        history (``FREQUENTLY_CHANGED``, ``FREQUENTLY_FAILING``) are produced
        later by the service that has incident and commit data — this pass would
        have to guess, and a guessed signal is worse than an absent one because it
        looks like evidence.
        """
        now = datetime.now(timezone.utc)
        fan_in = graph.stats.fan_in or {}
        fan_out = graph.stats.fan_out or {}
        rows: list[dict] = []

        def add(symbol_name, path, signal_type, value, unit, detail) -> None:
            rows.append(
                {
                    "id": uuid_module.uuid4(),
                    "project_id": repository.project_id,
                    "snapshot_id": snapshot.id,
                    "symbol_id": symbol_ids.get(symbol_name),
                    "file_path": path,
                    "signal_type": signal_type,
                    "value": float(value),
                    "unit": unit,
                    "detail": detail,
                    "observed_at": now,
                    "signal_metadata": {"source": "index"},
                }
            )

        for parsed in parsed_files:
            for symbol in parsed.symbols:
                metadata = symbol.metadata or {}
                if (
                    symbol.complexity
                    and symbol.complexity >= settings.CODE_COMPLEXITY_SIGNAL_THRESHOLD
                ):
                    add(
                        symbol.qualified_name,
                        parsed.path,
                        RiskSignalType.HIGH_COMPLEXITY,
                        symbol.complexity,
                        "branches",
                        f"{symbol.complexity} branch points in this definition",
                    )
                incoming = fan_in.get(symbol.qualified_name, 0)
                if incoming >= settings.CODE_FAN_SIGNAL_THRESHOLD:
                    add(
                        symbol.qualified_name,
                        parsed.path,
                        RiskSignalType.HIGH_FAN_IN,
                        incoming,
                        "callers",
                        f"{incoming} resolved call sites target this definition",
                    )
                outgoing = fan_out.get(symbol.qualified_name, 0)
                if outgoing >= settings.CODE_FAN_SIGNAL_THRESHOLD:
                    add(
                        symbol.qualified_name,
                        parsed.path,
                        RiskSignalType.HIGH_FAN_OUT,
                        outgoing,
                        "callees",
                        f"this definition makes {outgoing} resolved calls",
                    )
                if metadata.get("database_operations"):
                    operations = sorted(set(metadata["database_operations"]))
                    add(
                        symbol.qualified_name,
                        parsed.path,
                        RiskSignalType.DATABASE_OPERATION,
                        len(operations),
                        "operations",
                        "calls " + ", ".join(operations[:4]),
                    )
                if metadata.get("external_api_calls"):
                    calls = sorted(set(metadata["external_api_calls"]))
                    add(
                        symbol.qualified_name,
                        parsed.path,
                        RiskSignalType.EXTERNAL_API_CALL,
                        len(calls),
                        "calls",
                        "calls " + ", ".join(calls[:4]),
                    )
                if metadata.get("raises"):
                    raises = sorted(set(metadata["raises"]))
                    add(
                        symbol.qualified_name,
                        parsed.path,
                        RiskSignalType.ERROR_PRONE_PATH,
                        len(raises),
                        "raises",
                        "raises " + ", ".join(raises[:4]),
                    )
                if symbol.symbol_type in (CodeSymbolType.ROUTE, CodeSymbolType.HANDLER):
                    add(
                        symbol.qualified_name,
                        parsed.path,
                        RiskSignalType.DEPENDENCY_BOUNDARY,
                        1.0,
                        "boundary",
                        "serves an inbound request (process boundary)",
                    )
        await self._bulk_insert(CodeRiskSignal, rows)
        return len(rows)

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------
    async def _finalize(
        self,
        run: CodeIndexRun,
        snapshot: RepositorySnapshot,
        repository: CodeRepository,
        stats: IndexStats,
        started: datetime,
        *,
        parser_partial: bool,
    ) -> None:
        finished = datetime.now(timezone.utc)
        #: Counted from the table rather than from a running total: the row count
        #: is the fact, and a counter that drifts from it would make the snapshot's
        #: own summary unreliable.
        snapshot.file_count = int(
            await self.session.scalar(
                select(func.count())
                .select_from(CodeFile)
                .where(CodeFile.snapshot_id == snapshot.id)
            )
            or 0
        )
        snapshot.symbol_count = stats.symbols
        snapshot.languages = sorted(stats.languages)
        snapshot.indexed_at = finished
        snapshot.status = (
            SnapshotStatus.PARTIAL
            if parser_partial or stats.truncated
            else SnapshotStatus.READY
        )
        snapshot.index_metadata = {
            #: Recorded so a later pass can tell "the same revision, indexed by the
            #: same parser" from "a revision indexed by an older parser, which must
            #: be rebuilt". Without it, skipping the re-index would be a guess.
            "parser_version": PARSER_VERSION,
            "files_seen": stats.files_seen,
            "files_indexed": stats.files_indexed,
            "files_reused": stats.files_reused,
            "files_failed": stats.files_failed,
            "files_partial": stats.files_partial,
            "files_heuristic": stats.files_heuristic,
            "languages": stats.languages,
            "resolved_references": stats.resolved_references,
            "unresolved_references": stats.unresolved_references,
            "not_attempted_references": stats.not_attempted_references,
            "resolution_ratio": round(stats.resolution_ratio(), 4),
            "resolutions": stats.by_resolution,
            "references_by_kind": stats.by_kind,
            "parsers": supported_languages(),
            "heuristic_parsers": [HEURISTIC_PARSER_NOTE],
            "truncated": stats.truncated,
        }

        run.status = DebugAnalysisStatus.COMPLETED
        run.completed_at = finished
        run.duration_ms = int((finished - started).total_seconds() * 1000)
        run.files_seen = stats.files_seen
        run.files_indexed = stats.files_indexed
        run.files_added = stats.files_added
        run.files_modified = stats.files_modified
        run.files_deleted = stats.files_deleted
        #: One number for \"this file's intelligence is incomplete\" across both
        #: causes, so a partial snapshot cannot be read as a complete one.
        run.files_failed = stats.files_failed + stats.files_partial
        run.symbols_indexed = stats.symbols
        run.references_indexed = stats.references
        run.relationships_indexed = stats.relationships
        run.errors = stats.errors or None
        run.run_metadata = {
            #: The count is recorded as well as the (capped) path list: the API
            #: reports ``files_reused`` from this key, and deriving it from a list
            #: capped at 200 would under-report a large incremental pass. Without
            #: the key the response read ``0`` for every run that reused anything.
            "files_reused": stats.files_reused,
            "reused_paths": stats.reused_paths[:200],
            "deleted_paths": stats.deleted_paths[:200],
            "by_resolution": stats.by_resolution,
            "files_heuristic": stats.files_heuristic,
        }

        repository.index_status = (
            RepositoryIndexStatus.PARTIAL
            if snapshot.status is SnapshotStatus.PARTIAL
            else RepositoryIndexStatus.INDEXED
        )
        repository.last_indexed_at = finished
        repository.last_indexed_commit = snapshot.commit_sha
        if stats.languages:
            repository.language = max(
                stats.languages.items(), key=lambda item: item[1]
            )[0][:32]
        framework = await self._detect_framework(repository, snapshot.commit_sha)
        if framework:
            repository.framework = framework[:64]
        await self.session.flush()

    async def _detect_framework(
        self, repository: CodeRepository, commit_sha: Optional[str]
    ) -> Optional[str]:
        """A bounded manifest scan. A label for the UI, never evidence."""
        try:
            provider = provider_for_repository(repository)
            entries = await provider.list_files(commit_sha)
        except (RepositoryError, ProviderError):
            return None
        manifests = [
            entry.path
            for entry in entries
            if posixpath.basename(entry.path).lower() in MANIFEST_FILES
        ]
        matches: list[tuple[str, int]] = []
        for path in manifests[:6]:
            try:
                text = await provider.read_file(path, commit_sha)
            except (RepositoryError, ProviderError):
                continue
            if not text:
                continue
            lowered = text.lower()
            for label, token in FRAMEWORK_TOKENS:
                if token in lowered:
                    matches.append((label, len(token)))
        if not matches:
            return None
        #: Longest matching dependency token wins, so ``@nestjs/core`` beats an
        #: incidental ``next`` substring elsewhere in the same manifest.
        return max(matches, key=lambda item: item[1])[0]

    async def _fail(
        self,
        run: CodeIndexRun,
        snapshot: RepositorySnapshot,
        repository: CodeRepository,
        error: str,
    ) -> CodeIndexRun:
        from sqlalchemy import update

        finished = datetime.now(timezone.utc)
        await self.session.execute(
            update(CodeIndexRun)
            .where(CodeIndexRun.id == run.id)
            .values(
                status=DebugAnalysisStatus.FAILED,
                completed_at=finished,
                errors=[{"error": error[:500]}],
            )
        )
        await self.session.execute(
            update(RepositorySnapshot)
            .where(RepositorySnapshot.id == snapshot.id)
            .values(status=SnapshotStatus.FAILED, error=error[:2000])
        )
        await self.session.execute(
            update(CodeRepository)
            .where(CodeRepository.id == repository.id)
            .values(index_status=RepositoryIndexStatus.FAILED)
        )
        await self.session.flush()
        #: Refresh in-session state so a caller holding these objects sees the
        #: failure rather than the values it had before the Core update.
        await self.session.refresh(run)
        await self.session.refresh(snapshot)
        await self.session.refresh(repository)
        logger.warning("indexing failed for snapshot %s: %s", snapshot.id, error[:200])
        return run

    # ------------------------------------------------------------------
    # Bulk insert
    # ------------------------------------------------------------------
    async def _bulk_insert(self, model, rows: list[dict]) -> None:
        """Insert ``rows`` with executemany, in chunks.

        Row-by-row ORM inserts cost one round trip per row: on a 272-file
        repository that was ~65,000 round trips and a 70-second index. Chunked
        executemany makes the same work a few hundred statements.
        """
        if not rows:
            return
        for start in range(0, len(rows), INSERT_CHUNK_ROWS):
            await self.session.execute(
                insert(model), rows[start : start + INSERT_CHUNK_ROWS]
            )


def _reusable(entry: FileEntry, reusable: dict[str, CodeFile]) -> bool:
    """Whether this entry can be copied from the base snapshot instead of parsed.

    Requires a content hash on *both* sides: an entry the provider could not hash
    (a plain directory listing) has nothing to compare, and assuming equality
    would silently analyse stale symbols.
    """
    base_file = reusable.get(entry.path)
    return bool(
        base_file is not None
        and entry.content_hash
        and base_file.content_hash
        and entry.content_hash == base_file.content_hash
    )


async def index_repository_snapshot(
    session: AsyncSession,
    repository: CodeRepository,
    snapshot: RepositorySnapshot,
    **kwargs,
) -> CodeIndexRun:
    """Convenience wrapper mirroring the other phases' service entry points."""
    return await CodeIndexer(session).index(repository, snapshot, **kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _reconstruct_parsed_file(
    code_file: CodeFile,
    symbols: Sequence[CodeSymbol],
    references: Sequence[CodeReference],
) -> ParsedFile:
    """Rebuild the parser's in-memory shape from stored rows.

    This is what lets an incremental pass stay correct: the graph builder needs
    every symbol of the snapshot, including the ones that were not re-parsed.
    Reconstruction keeps the invariant that only *parsing* was skipped.
    """
    parsed = ParsedFile(
        path=code_file.path,
        language=code_file.language or "unknown",
        line_count=code_file.line_count,
        status=code_file.parse_status,
        error=code_file.parse_error,
        metadata={"reused": True, **(code_file.file_metadata or {})},
    )
    index_by_id: dict[object, int] = {}
    for position, row in enumerate(symbols):
        index_by_id[row.id] = position
    for row in symbols:
        parsed.symbols.append(
            ParsedSymbol(
                name=row.symbol_name,
                qualified_name=row.qualified_name,
                symbol_type=row.symbol_type,
                start_line=row.start_line,
                end_line=row.end_line,
                signature=row.signature,
                documentation=row.documentation,
                source=row.source,
                parent_index=index_by_id.get(row.parent_symbol_id),
                complexity=row.complexity or 0,
                is_async=row.is_async,
                route=row.route,
                http_method=row.http_method,
                metadata=dict(row.symbol_metadata or {}),
            )
        )
        if row.route:
            parsed.routes.append((row.route, index_by_id.get(row.id)))
    for reference_row in references:
        metadata = dict(reference_row.reference_metadata or {})
        parsed.references.append(
            ParsedReference(
                name=reference_row.name,
                kind=reference_row.reference_kind,
                line=reference_row.line,
                column=reference_row.column,
                enclosing_index=index_by_id.get(reference_row.enclosing_symbol_id),
                #: The module hint must be restored, not just the metadata dict: it
                #: is a first-class field on the in-memory reference, and without it
                #: every import-alias resolution in a reused file would silently
                #: fail and its call edges would vanish from the graph.
                module_hint=metadata.get("module"),
                metadata=metadata,
            )
        )
    return parsed


def _hash_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _safe_metadata(metadata: Optional[dict]) -> Optional[dict]:
    """Store metadata only if it is JSON-serializable and small.

    A parser bug that put a non-serializable object into metadata would otherwise
    fail the whole indexing run at flush time; truncating and stringifying keeps
    the failure local to one file.
    """
    if not metadata:
        return None
    try:
        encoded = json.dumps(metadata, default=str)
    except (TypeError, ValueError):
        return {"_unserializable": True}
    if len(encoded) > 20_000:
        return {"_truncated": True, "keys": sorted(str(key) for key in metadata)}
    return json.loads(encoded)
