"""ARGUS Repository Providers (Phase 6 §5–§10, §56).

Provider-neutral, **read-only** access to source code. Everything a debugging
analysis knows about code enters through this module, which is what lets the
security story be simple: if a read is not possible through this interface, it
does not happen.

The security model, in order of importance:

* **No shell.** ``git`` is invoked as an argv list with ``shell=False``. A path
  containing ``; rm -rf /`` is a filename, not a command.
* **Allowlisted plumbing.** Only the read-only ``git`` subcommands in
  :data:`ALLOWED_GIT_SUBCOMMANDS` may run. ``checkout``, ``reset``, ``clean``,
  ``push``, ``fetch``, ``submodule`` and friends are absent by construction, so
  ARGUS cannot mutate a repository it was pointed at — not because it is
  careful, but because the operation is not reachable.
* **No checkout for history.** Reading a past revision uses
  ``git ls-tree``/``git cat-file``, which serve blobs out of the object
  database. ARGUS therefore never creates a worktree, never touches the index,
  and never leaves the working copy in a different state than it found it.
* **Path confinement.** Every requested path is normalised and verified to stay
  inside the repository root. Absolute paths, ``..`` segments and NUL bytes are
  rejected before any I/O (a symlink escaping the root is rejected too: the
  resolved path is re-checked).
* **Bounded work.** File size, file count, output bytes and wall-clock time all
  have ceilings, so a hostile or merely enormous repository cannot turn a read
  into a resource exhaustion.

Providers never fetch over the network. A Git provider reads a working copy that
already exists on disk; cloning/fetching is a deliberate operator action, which
is why ``clone``/``fetch`` are declared on the interface but raise
:class:`NetworkAccessDisabled` in the built-in implementations.
"""

from __future__ import annotations

import abc
import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class RepositoryError(RuntimeError):
    """Base class for repository access failures."""


class RepositoryNotFound(RepositoryError):
    """The configured repository root does not exist or is not a directory."""


class UnsafePathError(RepositoryError):
    """A requested path escapes the repository root or is otherwise invalid."""


class ProviderError(RepositoryError):
    """A provider operation failed (bad revision, corrupt object, timeout)."""


class NetworkAccessDisabled(RepositoryError):
    """A network operation was requested on a provider that refuses them.

    Raised rather than silently ignored: a caller that believes it fetched
    something would otherwise analyse stale code and report it as current.
    """


class GitUnavailable(RepositoryError):
    """No usable ``git`` binary, or the directory is not a Git working copy."""


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FileEntry:
    """One file discovered in a snapshot."""

    path: str
    size_bytes: int
    language: Optional[str] = None
    is_test: bool = False
    #: Content hash when the provider can supply one cheaply (git gives us the
    #: blob id); otherwise the indexer computes sha256 while reading.
    content_hash: Optional[str] = None


@dataclass(frozen=True)
class CommitInfo:
    """A commit (or, for non-VCS providers, a synthetic single revision)."""

    sha: str
    message: str = ""
    author: str = ""
    committed_at: Optional[datetime] = None
    parents: tuple[str, ...] = ()
    files_changed: int = 0


@dataclass(frozen=True)
class BlameLine:
    """One blamed line: who last touched it, and in which commit."""

    line: int
    sha: str
    author: str = ""
    committed_at: Optional[datetime] = None


@dataclass(frozen=True)
class DiffEntry:
    """One file's change between two revisions."""

    status: str  # ADDED | MODIFIED | DELETED | RENAMED | COPIED | OTHER
    path: str
    old_path: Optional[str] = None


@dataclass(frozen=True)
class RepositoryInfo:
    """What a provider can say about the repository as a whole."""

    provider_name: str
    root: str
    vcs_present: bool
    head: Optional[str] = None
    branch: Optional[str] = None
    default_branch: Optional[str] = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Language / path classification
# ---------------------------------------------------------------------------
#: Extension → language. Deliberately small and explicit: a language without a
#: parser is indexed as a file (so it can be searched and shown) but produces no
#: symbols, and the interface for adding one is exactly this table plus a parser.
EXTENSION_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".sql": "sql",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".md": "markdown",
    ".sh": "shell",
    ".bash": "shell",
    ".dockerfile": "dockerfile",
    ".tf": "terraform",
    ".proto": "protobuf",
    ".graphql": "graphql",
}

#: Filenames with no (or a misleading) extension.
FILENAME_LANGUAGES: dict[str, str] = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
    "gemfile": "ruby",
    "rakefile": "ruby",
    "procfile": "text",
    "go.mod": "go",
    "cargo.toml": "rust",
    "requirements.txt": "text",
    "pyproject.toml": "toml",
    "package.json": "json",
    "tsconfig.json": "json",
}

#: Languages with a real parser behind them. The rest are indexed as text:
#: searchable, shown in the viewer, and honestly reported as unparsed.
PARSED_LANGUAGES = frozenset({"python", "javascript", "typescript", "json"})


def detect_language(path: str) -> Optional[str]:
    """Best-effort language for ``path`` (never guesses from content)."""
    base = os.path.basename(path).lower()
    if base in FILENAME_LANGUAGES:
        return FILENAME_LANGUAGES[base]
    _, ext = os.path.splitext(base)
    return EXTENSION_LANGUAGES.get(ext)


def is_test_path(path: str) -> bool:
    """Whether ``path`` looks like a test, by convention only.

    Convention-based detection can be wrong either way, so it is recorded as a
    *property* (used to de-prioritise tests in the context budget) rather than
    used to exclude anything.
    """
    lowered = path.lower().replace("\\", "/")
    base = os.path.basename(lowered)
    if base.startswith("test_") or base.endswith("_test.py"):
        return True
    if ".test." in base or ".spec." in base:
        return True
    if base in {"conftest.py", "test.py", "tests.py"}:
        return True
    parts = lowered.split("/")
    return any(
        part in {"tests", "test", "__tests__", "spec", "specs"} for part in parts[:-1]
    )


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------
def _normalise_root(root: str) -> str:
    if not root:
        raise RepositoryNotFound("repository has no local path configured")
    expanded = os.path.realpath(os.path.expanduser(root))
    if not os.path.isdir(expanded):
        raise RepositoryNotFound(f"repository root is not a directory: {root}")
    return expanded


def _enforce_allowed_roots(root: str) -> None:
    """Refuse a root outside the configured allow-list (§56)."""
    allowed = [r for r in (settings.CODE_ALLOWED_ROOTS or []) if r]
    if not allowed:
        #: Single-tenant/dev default: any readable directory. Documented in
        #: docs/phase-6.md — the guard exists, it is just not armed.
        return
    for candidate in allowed:
        real = os.path.realpath(os.path.expanduser(candidate))
        if root == real or root.startswith(real.rstrip(os.sep) + os.sep):
            return
    raise UnsafePathError(
        "repository root is outside CODE_ALLOWED_ROOTS; set CODE_ALLOWED_ROOTS "
        "to include it if this is intentional"
    )


def safe_relative_path(path: str) -> str:
    """Validate and normalise a repository-relative path.

    Rejects the three shapes that make path handling dangerous: absolute paths
    (which would ignore the root), parent traversal, and NUL bytes (which
    truncate at the syscall boundary).
    """
    if not path or not path.strip():
        raise UnsafePathError("empty path")
    if "\x00" in path:
        raise UnsafePathError("path contains a NUL byte")
    candidate = path.replace("\\", "/").strip()
    if candidate.startswith("/") or os.path.isabs(candidate):
        raise UnsafePathError(f"absolute paths are not allowed: {path!r}")
    if len(candidate) > 1 and candidate[1] == ":":  # Windows drive letter
        raise UnsafePathError(f"absolute paths are not allowed: {path!r}")
    parts: list[str] = []
    for segment in candidate.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise UnsafePathError(f"parent traversal is not allowed: {path!r}")
        parts.append(segment)
    if not parts:
        raise UnsafePathError(f"path resolves to the repository root: {path!r}")
    return "/".join(parts)


def confined_join(root: str, path: str) -> str:
    """Join ``path`` onto ``root``, proving the result stays inside ``root``."""
    relative = safe_relative_path(path)
    joined = os.path.normpath(os.path.join(root, relative))
    real_root = os.path.realpath(root)
    #: ``realpath`` also resolves symlinks, so a link pointing outside the root
    #: is caught here rather than being followed.
    real_joined = os.path.realpath(joined)
    if real_joined != real_root and not real_joined.startswith(
        real_root.rstrip(os.sep) + os.sep
    ):
        raise UnsafePathError(f"path escapes the repository root: {path!r}")
    return joined


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------
class RepositoryProvider(abc.ABC):
    """Read-only access to one repository at any requested revision.

    ``revision`` is an opaque reference (commit sha, branch, tag) that the
    provider resolves; ``None`` always means "the provider's current state",
    which for a VCS-backed provider is HEAD and for a plain directory is the
    working tree. Callers that need reproducibility resolve the revision first
    (:meth:`resolve`) and then pass the resulting sha, which is what makes an
    analysis reproducible after the branch moves on (§7, §8).
    """

    name: str = "base"

    @abc.abstractmethod
    async def describe(self) -> RepositoryInfo:
        """Return provider-level facts about the repository."""

    @abc.abstractmethod
    async def resolve(self, reference: Optional[str]) -> Optional[str]:
        """Canonicalise a reference to a revision id, or ``None`` if unknown."""

    @abc.abstractmethod
    async def list_files(self, revision: Optional[str] = None) -> list[FileEntry]:
        """Every indexable file at ``revision`` (bounded)."""

    @abc.abstractmethod
    async def read_file(
        self, path: str, revision: Optional[str] = None
    ) -> Optional[str]:
        """File contents, or ``None`` when the path does not exist."""

    async def read_files(
        self, paths: Sequence[str], revision: Optional[str] = None
    ) -> dict[str, str]:
        """Read many files at one revision, keyed by path.

        Exists because reading a repository file-by-file is dominated by process
        startup, not by I/O: on a 272-file repository the per-file path spent 35s
        of a 76s index inside ``git``. Providers override this to batch; the base
        implementation is a correct fallback. Unreadable or missing paths are
        simply absent from the result — never silently empty.
        """
        result: dict[str, str] = {}
        for path in paths:
            try:
                content = await self.read_file(path, revision)
            except (RepositoryError, ProviderError):
                continue
            if content is not None:
                result[path] = content
        return result

    @abc.abstractmethod
    async def get_commit(self, revision: Optional[str]) -> Optional[CommitInfo]:
        """Metadata for one commit."""

    @abc.abstractmethod
    async def get_history(
        self,
        path: Optional[str] = None,
        limit: int = 50,
        revision: Optional[str] = None,
    ) -> list[CommitInfo]:
        """Commits touching ``path`` (or the whole repository when ``None``)."""

    async def file_commits(
        self,
        paths: Sequence[str],
        revision: Optional[str] = None,
    ) -> dict[str, CommitInfo]:
        """The newest commit that touched each path, from one history walk.

        The whole point of this method is that it is **one** call. Asking a
        provider ``git log -1 -- <path>`` per file turns a 10 000-file index into
        10 000 processes, and a file's last change is one of the facts the code
        viewer and the recurrence signals are built on. A provider that cannot
        answer returns nothing, leaving the columns null rather than stamping
        every file with the deployed revision — the revision that was *deployed*
        is not the revision each file was last *changed* in.
        """
        return {}

    @abc.abstractmethod
    async def get_blame(
        self, path: str, revision: Optional[str] = None, max_lines: Optional[int] = None
    ) -> list[BlameLine]:
        """Per-line attribution for one file (empty when unsupported)."""

    @abc.abstractmethod
    async def diff(self, base: Optional[str], head: Optional[str]) -> list[DiffEntry]:
        """Files changed between two revisions."""

    # -- network operations, disabled by default ---------------------------
    async def clone(self, destination: str) -> None:
        """Deliberately unimplemented for the built-in providers."""
        raise NetworkAccessDisabled(
            "ARGUS providers do not clone: point the repository at an existing "
            "local working copy (offline checkout) instead"
        )

    async def fetch(self) -> None:
        """Deliberately unimplemented for the built-in providers."""
        raise NetworkAccessDisabled(
            "ARGUS providers do not fetch: refreshing a working copy is an "
            "operator action, so that every analysis states which revision it used"
        )


# ---------------------------------------------------------------------------
# Local (no VCS) provider
# ---------------------------------------------------------------------------
#: Sentinel so the VCS-delegation cache can distinguish "not looked up yet" from
#: "looked up and found nothing".
_UNSET = object()


class LocalRepositoryProvider(RepositoryProvider):
    """A plain directory on disk, with no version control.

    Every file is part of one implicit revision whose id is a deterministic hash
    of the whole tree. That hash is what makes the snapshot meaningful: it pins
    *exactly* the content that was analysed, so re-indexing a changed directory
    produces a different snapshot instead of silently rewriting the old one.
    """

    name = "local"

    def __init__(self, root: str) -> None:
        self.root = _normalise_root(root)
        _enforce_allowed_roots(self.root)
        #: Memoised tree signature — ``describe()`` is called by ``resolve()`` and
        #: by the indexer, and walking a large tree repeatedly would make a
        #: simple revision lookup cost more than the indexing it precedes.
        self._head_cache: Optional[str] = None
        #: ``_UNSET`` until looked up; ``None`` once found to have no usable git.
        self._vcs_cache: object = _UNSET

    # -- helpers -----------------------------------------------------------
    def _walk(self) -> list[FileEntry]:
        max_files = settings.CODE_MAX_FILES_PER_SNAPSHOT
        excluded = set(settings.CODE_EXCLUDED_DIRS)
        entries: list[FileEntry] = []
        for directory, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in excluded)
            for filename in sorted(filenames):
                absolute = os.path.join(directory, filename)
                relative = os.path.relpath(absolute, self.root).replace(os.sep, "/")
                try:
                    size = os.path.getsize(absolute)
                except OSError:
                    continue
                entries.append(
                    FileEntry(
                        path=relative,
                        size_bytes=size,
                        language=detect_language(relative),
                        is_test=is_test_path(relative),
                        #: A plain directory has no blob ids; the indexer computes
                        #: sha256 while reading, which is why this stays ``None``
                        #: rather than being a made-up value.
                        content_hash=None,
                    )
                )
                if len(entries) >= max_files:
                    return entries
        return entries

    # -- VCS delegation ----------------------------------------------------
    async def _vcs(self) -> Optional["GitRepositoryProvider"]:
        """A git view of this directory, when it is a git working copy.

        A "local" repository in ARGUS means "a directory ARGUS reads directly",
        which in practice is usually a git checkout. Without this delegation the
        local provider reports no VCS at all, and the consequences are not
        cosmetic: change history, diffs and blame are unavailable, so incident ↔
        commit correlation (§19) has nothing to stand on and the debugging
        analysis cannot say which change touched the failing path.

        Delegation rather than auto-detection in ``provider_for`` keeps the
        provider's identity (and therefore the stored ``provider_name``) exactly
        as configured, so no existing snapshot is reinterpreted.
        """
        if self._vcs_cache is not _UNSET:
            return self._vcs_cache  # type: ignore[return-value]
        delegate: Optional[GitRepositoryProvider] = None
        if os.path.isdir(os.path.join(self.root, ".git")) or os.path.isfile(
            os.path.join(self.root, ".git")
        ):
            try:
                delegate = GitRepositoryProvider(self.root)
                if not await delegate._vcs_present():
                    delegate = None
            except (GitUnavailable, RepositoryError):
                delegate = None
        self._vcs_cache = delegate
        return delegate

    # -- interface ---------------------------------------------------------
    async def describe(self) -> RepositoryInfo:
        delegate = await self._vcs()
        if delegate is not None:
            try:
                info = await delegate.describe()
            except (RepositoryError, ProviderError):
                info = None
            if info is not None:
                #: The directory is the thing being described; the *name* stays
                #: the configured provider so snapshots remain traceable to it.
                return RepositoryInfo(
                    provider_name=self.name,
                    root=self.root,
                    vcs_present=True,
                    head=info.head,
                    branch=info.branch,
                    default_branch=info.default_branch,
                    notes=[
                        "directory is a version-control working copy; history, "
                        "diff and blame are available through git",
                        *info.notes,
                    ],
                )

        def _run() -> RepositoryInfo:
            if self._head_cache is None:
                entries = self._walk()
                digest = hashlib.sha256()
                for entry in entries:
                    digest.update(f"{entry.path}:{entry.size_bytes}\n".encode())
                self._head_cache = digest.hexdigest()[:40]
            return RepositoryInfo(
                provider_name=self.name,
                root=self.root,
                vcs_present=False,
                head=self._head_cache,
                notes=["directory is not a version-control working copy"],
            )

        return await asyncio.to_thread(_run)

    async def resolve(self, reference: Optional[str]) -> Optional[str]:
        delegate = await self._vcs()
        if delegate is not None and reference is not None:
            #: A commit sha must resolve to *this* repository's history; a sha
            #: the checkout does not contain stays unresolved, which is what makes
            #: the caller downgrade its version confidence instead of silently
            #: analysing the wrong revision.
            try:
                return await delegate.resolve(reference)
            except (RepositoryError, ProviderError):
                return None
        info = await self.describe()
        if reference is None:
            return info.head
        return reference

    async def list_files(self, revision: Optional[str] = None) -> list[FileEntry]:
        delegate = await self._vcs()
        if delegate is not None and revision:
            try:
                return await delegate.list_files(revision)
            except (RepositoryError, ProviderError):
                logger.info(
                    "falling back to a directory walk for revision %s", revision
                )
        return await asyncio.to_thread(self._walk)

    async def read_file(
        self, path: str, revision: Optional[str] = None
    ) -> Optional[str]:
        delegate = await self._vcs()
        if delegate is not None and revision:
            try:
                text = await delegate.read_file(path, revision)
                if text is not None:
                    return text
            except (RepositoryError, ProviderError):
                logger.info("git read failed for %s@%s", path, revision)

        def _run() -> Optional[str]:
            absolute = confined_join(self.root, path)
            if not os.path.isfile(absolute):
                return None
            if os.path.getsize(absolute) > settings.CODE_MAX_FILE_BYTES:
                return None
            with open(absolute, encoding="utf-8", errors="replace") as handle:
                return handle.read()

        return await asyncio.to_thread(_run)

    async def read_files(
        self, paths: Sequence[str], revision: Optional[str] = None
    ) -> dict[str, str]:
        delegate = await self._vcs()
        if delegate is not None and revision:
            try:
                found = await delegate.read_files(paths, revision)
                if found:
                    return found
            except (RepositoryError, ProviderError):
                logger.info("git batch read failed for revision %s", revision)
        return await super().read_files(paths, revision)

    async def get_commit(self, revision: Optional[str]) -> Optional[CommitInfo]:
        delegate = await self._vcs()
        if delegate is not None:
            try:
                commit = await delegate.get_commit(revision)
            except (RepositoryError, ProviderError):
                commit = None
            if commit is not None:
                return commit
        info = await self.describe()
        if revision is not None and revision != info.head:
            return None
        return CommitInfo(
            sha=info.head or "working-tree",
            message="working tree (no version control)",
            author="",
            committed_at=None,
        )

    async def get_history(
        self,
        path: Optional[str] = None,
        limit: int = 50,
        revision: Optional[str] = None,
    ) -> list[CommitInfo]:
        delegate = await self._vcs()
        if delegate is not None:
            try:
                return await delegate.get_history(
                    path=path, limit=limit, revision=revision
                )
            except (RepositoryError, ProviderError):
                logger.info(
                    "git history unavailable; reporting the current revision only"
                )
        #: A directory without VCS has no history. Returning the single current
        #: revision (rather than an error) keeps every caller's loop identical.
        commit = await self.get_commit(revision)
        return [commit] if commit else []

    async def file_commits(
        self,
        paths: Sequence[str],
        revision: Optional[str] = None,
    ) -> dict[str, CommitInfo]:
        delegate = await self._vcs()
        if delegate is None:
            return {}
        return await delegate.file_commits(paths, revision)

    async def get_blame(
        self, path: str, revision: Optional[str] = None, max_lines: Optional[int] = None
    ) -> list[BlameLine]:
        delegate = await self._vcs()
        if delegate is None:
            return []
        try:
            return await delegate.get_blame(path, revision, max_lines)
        except (RepositoryError, ProviderError):
            return []

    async def diff(self, base: Optional[str], head: Optional[str]) -> list[DiffEntry]:
        if base is None or head is None or base == head:
            return []
        delegate = await self._vcs()
        if delegate is not None:
            return await delegate.diff(base, head)
        #: Without VCS there is nothing to diff between; report it as "unknown"
        #: rather than as an empty change set, which would read as "nothing
        #: changed" and quietly weaken the analysis.
        raise ProviderError(
            "cannot diff a directory without version control; "
            "configure a git repository to get change history"
        )


# ---------------------------------------------------------------------------
# Git provider (read-only, plumbing only)
# ---------------------------------------------------------------------------
#: The complete set of git subcommands ARGUS will run. Every entry is read-only.
#: Mutating verbs are absent, so they cannot be invoked even by a bug.
ALLOWED_GIT_SUBCOMMANDS = frozenset(
    {
        "rev-parse",
        "rev-list",
        "ls-tree",
        "cat-file",
        "show",
        "log",
        "blame",
        "diff",
        "diff-tree",
        "symbolic-ref",
        "describe",
        "for-each-ref",
    }
)

#: Ceiling on captured git output, so a huge repository cannot exhaust memory.
MAX_GIT_OUTPUT_BYTES = 24_000_000

#: Paths per ``git cat-file --batch`` invocation. Bounds peak memory to roughly
#: this many file contents while keeping the round-trip count tiny.
BATCH_READ_PATHS = 200


def _parse_batch_output(output: bytes, paths: list[str]) -> dict[str, str]:
    """Parse ``git cat-file --batch`` output, which is length-delimited.

    The protocol is ``<sha> <type> <size>\n<contents>\n`` per object, or
    ``<object> missing\n``. Sizes are bytes, so the parser must work on raw
    bytes — decoding first would corrupt offsets for any non-UTF-8 file (an
    image, a minified bundle) and desynchronise every later read.
    """
    result: dict[str, str] = {}
    position = 0
    total = len(output)
    for path in paths:
        if position >= total:
            break
        newline = output.find(b"\n", position)
        if newline < 0:
            break
        header = output[position:newline]
        position = newline + 1
        parts = header.split()
        if len(parts) < 3:
            #: ``<object> missing`` — or a directory, which git reports the same
            #: way here. Either way there is no content to return.
            continue
        try:
            size = int(parts[2])
        except ValueError:
            continue
        body = output[position : position + size]
        position += size + 1  # trailing newline after the content
        if parts[1] != b"blob":
            continue
        result[path] = body.decode("utf-8", errors="replace")
    return result


class GitRepositoryProvider(RepositoryProvider):
    """A Git working copy, read at any revision without touching the tree."""

    name = "git"

    def __init__(self, root: str) -> None:
        self.root = _normalise_root(root)
        _enforce_allowed_roots(self.root)
        self._git_path = shutil.which("git")

    # -- plumbing ----------------------------------------------------------
    async def _git(self, args: Sequence[str], *, allow_failure: bool = False) -> str:
        """Run one allowlisted git command and return stdout.

        ``-c`` configuration is passed on the command line rather than read from
        the environment or a user config file: ARGUS will not run a
        repository-supplied alias, pager or hook.
        """
        if not self._git_path:
            raise GitUnavailable("git is not installed in this environment")
        if not args:
            raise ProviderError("git invoked with no subcommand")
        subcommand = args[0]
        if subcommand not in ALLOWED_GIT_SUBCOMMANDS:
            #: Defence in depth: the allow-list is a security boundary, so a
            #: violation is an error we want visible, not a silently skipped call.
            raise ProviderError(
                f"git subcommand {subcommand!r} is not on the read-only allow-list"
            )

        #: Bound outside the closure so the narrowing below survives into it —
        #: mypy cannot see that the ``if not self._git_path`` guard at the top of
        #: this method still holds inside the thread function.
        git_path = self._git_path

        def _run() -> str:
            command = [
                git_path,
                "--no-pager",
                "-c",
                "core.pager=cat",
                "-c",
                "safe.directory=*",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                self.root,
                *args,
            ]
            try:
                completed = subprocess.run(  # noqa: S603 - argv list, shell=False
                    command,
                    capture_output=True,
                    timeout=settings.CODE_GIT_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ProviderError(
                    f"git {subcommand} timed out after "
                    f"{settings.CODE_GIT_TIMEOUT_SECONDS}s"
                ) from exc
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace").strip()
                if allow_failure:
                    return ""
                raise ProviderError(f"git {subcommand} failed: {detail[:400]}")
            if len(completed.stdout) > MAX_GIT_OUTPUT_BYTES:
                raise ProviderError(
                    f"git {subcommand} produced more than "
                    f"{MAX_GIT_OUTPUT_BYTES} bytes; narrow the request"
                )
            return completed.stdout.decode("utf-8", errors="replace")

        return await asyncio.to_thread(_run)

    async def _vcs_present(self) -> bool:
        if not self._git_path:
            return False
        try:
            output = await self._git(
                ["rev-parse", "--is-inside-work-tree"], allow_failure=True
            )
        except ProviderError:
            return False
        return output.strip() == "true"

    # -- interface ---------------------------------------------------------
    async def describe(self) -> RepositoryInfo:
        if not await self._vcs_present():
            #: Graceful degradation: an un-versioned directory is still usable,
            #: and saying so is better than failing the whole analysis.
            local = LocalRepositoryProvider(self.root)
            info = await local.describe()
            return RepositoryInfo(
                provider_name=LocalRepositoryProvider.name,
                root=self.root,
                vcs_present=False,
                head=info.head,
                notes=["no git repository found; indexed as a plain directory"],
            )
        head = (
            await self._git(["rev-parse", "HEAD"], allow_failure=True)
        ).strip() or None
        branch = (
            await self._git(["rev-parse", "--abbrev-ref", "HEAD"], allow_failure=True)
        ).strip() or None
        return RepositoryInfo(
            provider_name=self.name,
            root=self.root,
            vcs_present=True,
            head=head,
            branch=branch,
            default_branch="main",
        )

    async def resolve(self, reference: Optional[str]) -> Optional[str]:
        if not await self._vcs_present():
            return await LocalRepositoryProvider(self.root).resolve(reference)
        if reference is None:
            reference = "HEAD"
        output = await self._git(
            ["rev-parse", "--verify", f"{reference}^{{commit}}"], allow_failure=True
        )
        sha = output.strip().splitlines()[-1].strip() if output.strip() else ""
        if not sha:
            return None
        #: Only accept a real object id, so a crafted reference cannot smuggle
        #: arbitrary text into a later ``show`` invocation.
        if len(sha) < 7 or any(c not in "0123456789abcdef" for c in sha.lower()):
            return None
        return sha

    async def list_files(self, revision: Optional[str] = None) -> list[FileEntry]:
        if not await self._vcs_present():
            return await LocalRepositoryProvider(self.root).list_files(revision)
        target = await self.resolve(revision) or "HEAD"
        output = await self._git(["ls-tree", "-r", "-l", "-z", target])
        entries: list[FileEntry] = []
        excluded = set(settings.CODE_EXCLUDED_DIRS)
        for record in output.split("\x00"):
            if not record:
                continue
            # <mode> SP <type> SP <object> SP <size> TAB <path>
            try:
                meta, path = record.split("\t", 1)
                parts = meta.split()
                object_id, size = parts[2], parts[3]
            except (ValueError, IndexError):
                continue
            if any(segment in excluded for segment in path.split("/")[:-1]):
                continue
            if path.split("/")[-1] in excluded:
                continue
            try:
                size_bytes = int(size)
            except ValueError:
                size_bytes = 0
            entries.append(
                FileEntry(
                    path=path,
                    size_bytes=size_bytes,
                    language=detect_language(path),
                    is_test=is_test_path(path),
                    content_hash=object_id,
                )
            )
            if len(entries) >= settings.CODE_MAX_FILES_PER_SNAPSHOT:
                break
        return entries

    async def read_file(
        self, path: str, revision: Optional[str] = None
    ) -> Optional[str]:
        relative = safe_relative_path(path)
        if not await self._vcs_present():
            return await LocalRepositoryProvider(self.root).read_file(
                relative, revision
            )
        target = await self.resolve(revision) or "HEAD"
        try:
            output = await self._git(
                ["cat-file", "blob", f"{target}:{relative}"], allow_failure=True
            )
        except ProviderError:
            return None
        if output != "":
            return output
        #: ``allow_failure`` turns both "missing path" and "empty file" into an
        #: empty string, and those are very different facts — so the type is
        #: checked explicitly rather than assuming one of them.
        check = await self._git(
            ["cat-file", "-t", f"{target}:{relative}"], allow_failure=True
        )
        return "" if check.strip() == "blob" else None

    async def read_files(
        self, paths: Sequence[str], revision: Optional[str] = None
    ) -> dict[str, str]:
        """Read many blobs in one ``git cat-file --batch`` process.

        Chunked so a huge snapshot cannot balloon memory, and still strictly
        read-only: ``cat-file`` serves objects out of the object database and
        never touches the index or the working tree.
        """
        if not paths:
            return {}
        if not await self._vcs_present():
            return await LocalRepositoryProvider(self.root).read_files(
                list(paths), revision
            )
        target = await self.resolve(revision) or "HEAD"
        result: dict[str, str] = {}
        for start in range(0, len(paths), BATCH_READ_PATHS):
            chunk = list(paths[start : start + BATCH_READ_PATHS])
            result.update(await self._batch_read(target, chunk))
        return result

    async def _batch_read(self, target: str, paths: Sequence[str]) -> dict[str, str]:
        if not self._git_path:
            raise GitUnavailable("git is not installed in this environment")
        git_path = self._git_path
        #: A path containing a newline would break the one-object-per-line input
        #: protocol, silently shifting every subsequent read by one. Those paths
        #: are read individually instead, so correctness never depends on the
        #: batching optimisation.
        clean = [path for path in paths if "\n" not in path and "\r" not in path]
        odd = [path for path in paths if path not in clean]
        result: dict[str, str] = {}
        if odd:
            result.update(await super().read_files(odd, target))
        if not clean:
            return result
        safe_paths = [safe_relative_path(path) for path in clean]

        def _run() -> dict[str, str]:
            import subprocess

            payload = "".join(f"{target}:{path}\n" for path in safe_paths).encode(
                "utf-8"
            )
            command = [
                git_path,
                "--no-pager",
                "-c",
                "core.pager=cat",
                "-c",
                "safe.directory=*",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                self.root,
                "cat-file",
                "--batch",
            ]
            try:
                completed = subprocess.run(  # noqa: S603 - argv list, shell=False
                    command,
                    input=payload,
                    capture_output=True,
                    timeout=settings.CODE_GIT_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ProviderError(
                    f"git cat-file --batch timed out after "
                    f"{settings.CODE_GIT_TIMEOUT_SECONDS}s"
                ) from exc
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace").strip()
                raise ProviderError(f"git cat-file --batch failed: {detail[:400]}")
            return _parse_batch_output(completed.stdout, safe_paths)

        result.update(await asyncio.to_thread(_run))
        return result

    async def get_commit(self, revision: Optional[str]) -> Optional[CommitInfo]:
        if not await self._vcs_present():
            return await LocalRepositoryProvider(self.root).get_commit(revision)
        sha = await self.resolve(revision)
        if not sha:
            return None
        #: ``%x00`` separators keep commit messages containing newlines intact:
        #: splitting on newlines would silently truncate a multi-line message.
        output = await self._git(
            ["show", "-s", "--format=%H%x00%an%x00%aI%x00%P%x00%B", sha],
            allow_failure=True,
        )
        fields = output.split("\x00", 4)
        if len(fields) < 5:
            return CommitInfo(sha=sha)
        committed_at = _parse_iso(fields[2])
        parents = tuple(p for p in fields[3].split() if p)
        return CommitInfo(
            sha=fields[0].strip() or sha,
            message=fields[4].strip(),
            author=fields[1].strip(),
            committed_at=committed_at,
            parents=parents,
        )

    async def get_history(
        self,
        path: Optional[str] = None,
        limit: int = 50,
        revision: Optional[str] = None,
    ) -> list[CommitInfo]:
        if not await self._vcs_present():
            return await LocalRepositoryProvider(self.root).get_history(
                path, limit, revision
            )
        target = await self.resolve(revision) or "HEAD"
        capped = max(1, min(limit, settings.CODE_HISTORY_MAX_COMMITS))
        args = [
            "log",
            f"--max-count={capped}",
            "--format=%H%x00%an%x00%aI%x00%P%x00%B%x01",
            target,
        ]
        if path is not None:
            args.extend(["--", safe_relative_path(path)])
        output = await self._git(args, allow_failure=True)
        commits: list[CommitInfo] = []
        for chunk in output.split("\x01"):
            chunk = chunk.strip("\n")
            if not chunk:
                continue
            fields = chunk.split("\x00", 4)
            if len(fields) < 4:
                continue
            commits.append(
                CommitInfo(
                    sha=fields[0].strip(),
                    author=fields[1].strip(),
                    committed_at=_parse_iso(fields[2]),
                    parents=tuple(p for p in fields[3].split() if p),
                    message=(fields[4].strip() if len(fields) > 4 else ""),
                )
            )
        return commits

    async def file_commits(
        self,
        paths: Sequence[str],
        revision: Optional[str] = None,
    ) -> dict[str, CommitInfo]:
        """Newest touching commit per path, from a single ``git log`` walk.

        One subprocess for the whole batch. ``--name-only`` makes git print the
        changed paths under each commit, so the *first* occurrence of a path while
        walking newest-first is that path's most recent change — no per-file call
        and no blame run. Paths are chunked because an argv list of ten thousand
        paths exceeds the platform's argument limit, and each chunk is still one
        process rather than one per file.
        """
        if not paths:
            return {}
        if not await self._vcs_present():
            return {}
        target = await self.resolve(revision) or "HEAD"
        capped = max(1, settings.CODE_HISTORY_MAX_COMMITS)
        found: dict[str, CommitInfo] = {}
        chunk_size = 200
        wanted = [safe_relative_path(path) for path in paths]
        for start in range(0, len(wanted), chunk_size):
            chunk = wanted[start : start + chunk_size]
            output = await self._git(
                [
                    "log",
                    f"--max-count={capped}",
                    "--name-only",
                    "--format=%x02%H%x00%an%x00%aI",
                    target,
                    "--",
                    *chunk,
                ],
                allow_failure=True,
            )
            current: Optional[CommitInfo] = None
            for line in output.splitlines():
                if line.startswith("\x02"):
                    fields = line[1:].split("\x00", 2)
                    if len(fields) < 3:
                        current = None
                        continue
                    current = CommitInfo(
                        sha=fields[0].strip(),
                        author=fields[1].strip(),
                        committed_at=_parse_iso(fields[2]),
                    )
                    continue
                path = line.strip()
                #: Newest first, so the first commit seen for a path wins. A path
                #: outside the requested chunk is git echoing something else —
                #: ignore it rather than attributing the wrong commit.
                if current is None or not path or path in found:
                    continue
                if path in chunk:
                    found[path] = current
        return found

    async def get_blame(
        self, path: str, revision: Optional[str] = None, max_lines: Optional[int] = None
    ) -> list[BlameLine]:
        relative = safe_relative_path(path)
        if not await self._vcs_present():
            return []
        target = await self.resolve(revision) or "HEAD"
        limit = min(
            max_lines or settings.CODE_BLAME_MAX_LINES, settings.CODE_BLAME_MAX_LINES
        )
        try:
            output = await self._git(
                [
                    "blame",
                    "--line-porcelain",
                    "-L",
                    f"1,{limit}",
                    target,
                    "--",
                    relative,
                ],
                allow_failure=True,
            )
        except ProviderError:
            return []
        return _parse_blame(output)

    async def diff(self, base: Optional[str], head: Optional[str]) -> list[DiffEntry]:
        if not await self._vcs_present():
            return await LocalRepositoryProvider(self.root).diff(base, head)
        base_sha = await self.resolve(base) if base else None
        head_sha = await self.resolve(head) if head else None
        if not base_sha or not head_sha or base_sha == head_sha:
            return []
        output = await self._git(
            ["diff", "--name-status", "--no-renames", base_sha, head_sha],
            allow_failure=True,
        )
        entries: list[DiffEntry] = []
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = {
                "A": "ADDED",
                "M": "MODIFIED",
                "D": "DELETED",
                "R": "RENAMED",
                "C": "COPIED",
            }.get(parts[0][:1].upper(), "OTHER")
            entries.append(DiffEntry(status=status, path=parts[-1]))
        return entries


def _parse_iso(value: str) -> Optional[datetime]:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_blame(output: str) -> list[BlameLine]:
    """Parse ``--line-porcelain`` blame output into one entry per line."""
    lines: list[BlameLine] = []
    current_sha = ""
    current_author = ""
    current_time: Optional[datetime] = None
    line_number = 0
    for raw in output.splitlines():
        if raw.startswith("author "):
            current_author = raw[len("author ") :]
        elif raw.startswith("author-time "):
            try:
                stamp = int(raw[len("author-time ") :].strip())
                current_time = datetime.fromtimestamp(stamp, tz=timezone.utc)
            except (ValueError, OSError):
                current_time = None
        elif raw.startswith("\t"):
            # The content line (TAB-prefixed) ends one blame entry. Checking the
            # prefix rather than mere presence of a tab matters: a source line
            # containing a tab is not a header, and a header never starts with one.
            line_number += 1
            lines.append(
                BlameLine(
                    line=line_number,
                    sha=current_sha,
                    author=current_author,
                    committed_at=current_time,
                )
            )
        elif len(raw) >= 40 and all(c in "0123456789abcdef" for c in raw[:40].lower()):
            # "<sha> <orig-line> <final-line> [<group-size>]"
            parts = raw.split()
            if parts:
                current_sha = parts[0]
    return lines


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def provider_for(
    provider_name: Optional[str],
    url_or_path: Optional[str],
    local_path: Optional[str] = None,
) -> RepositoryProvider:
    """Build the right provider for a repository row.

    Preference order is explicit rather than magical: an explicit ``local_path``
    wins, otherwise the repository URL is treated as a path when it looks like
    one. A remote URL is refused with a clear error instead of being interpreted
    as a local directory named ``https:``.
    """
    candidate = local_path or url_or_path or ""
    if not candidate:
        raise RepositoryNotFound(
            "repository has neither a local path nor a URL configured"
        )
    normalised = (provider_name or "").strip().lower()
    if candidate.startswith(("http://", "https://", "git@", "ssh://", "git://")):
        raise NetworkAccessDisabled(
            "ARGUS reads local working copies only. Clone the repository next to "
            "ARGUS and set its local path; remote fetching is an operator action."
        )
    if normalised == "git":
        return GitRepositoryProvider(candidate)
    if normalised in ("local", "", "directory", "filesystem"):
        return LocalRepositoryProvider(candidate)
    #: Unknown provider name: still try the local directory, but say so in the
    #: returned object's name so the snapshot records what actually happened.
    logger.info("unknown repository provider %r; falling back to local", provider_name)
    return LocalRepositoryProvider(candidate)


def provider_for_repository(repository) -> RepositoryProvider:
    """Convenience wrapper for an ORM ``CodeRepository`` row."""
    return provider_for(
        getattr(repository, "provider", None),
        getattr(repository, "repository_url", None),
        getattr(repository, "local_path", None),
    )
