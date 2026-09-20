"""ARGUS Change History Service (Phase 6 §18, §19).

Read-only history over a repository provider: recent commits, per-file history,
diffs and blame. It is a thin, bounded layer on purpose — every call goes to the
provider for the *pinned* revision, and nothing here interprets the result.

One rule is enforced in the API rather than left to the caller: a commit message
is **metadata**, never evidence of intent. The service returns messages verbatim
and the debugging prompt is explicit that they are untrusted data, so "fix
timeout handling" in a commit subject cannot be read as a statement of what the
change actually did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app.core.config import get_settings
from app.services.repository_provider import (
    BlameLine,
    CommitInfo,
    DiffEntry,
    ProviderError,
    RepositoryError,
    RepositoryProvider,
)

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class HistoryResult:
    """A bounded slice of history, with truncation stated rather than implied."""

    commits: list[dict]
    truncated: bool = False
    reason: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "commits": self.commits,
            "truncated": self.truncated,
            "reason": self.reason,
        }


class ChangeHistoryService:
    """Bounded, failure-tolerant history queries for one repository."""

    MAX_COMMITS = 50

    def __init__(self, provider: RepositoryProvider) -> None:
        self.provider = provider

    async def recent_changes(
        self, revision: Optional[str] = None, *, limit: int = 20
    ) -> HistoryResult:
        bound = max(1, min(limit, self.MAX_COMMITS))
        try:
            commits = await self.provider.get_history(limit=bound, revision=revision)
        except (RepositoryError, ProviderError) as error:
            return HistoryResult(commits=[], truncated=False, reason=str(error))
        return HistoryResult(
            commits=[_commit_dict(item) for item in commits],
            truncated=len(commits) >= bound,
        )

    async def file_history(
        self, path: str, *, revision: Optional[str] = None, limit: int = 10
    ) -> HistoryResult:
        bound = max(1, min(limit, self.MAX_COMMITS))
        try:
            commits = await self.provider.get_history(
                path=path, limit=bound, revision=revision
            )
        except (RepositoryError, ProviderError) as error:
            return HistoryResult(commits=[], truncated=False, reason=str(error))
        return HistoryResult(
            commits=[_commit_dict(item) for item in commits],
            truncated=len(commits) >= bound,
        )

    async def get_commit(self, revision: str) -> Optional[CommitInfo]:
        try:
            return await self.provider.get_commit(revision)
        except (RepositoryError, ProviderError) as error:
            logger.info("commit %s unavailable: %s", revision, error)
            return None

    async def commit_diff(
        self, base: Optional[str], head: Optional[str], *, path: Optional[str] = None
    ) -> tuple[list[dict], Optional[str]]:
        """Diff two revisions, filtered to ``path`` when given."""
        try:
            entries = await self.provider.diff(base, head)
        except (RepositoryError, ProviderError) as error:
            return [], str(error)
        rows = [_diff_dict(item) for item in entries]
        if path:
            rows = [row for row in rows if row["path"] == path]
        return rows[:200], None

    async def get_blame(
        self, path: str, revision: Optional[str] = None
    ) -> tuple[list[dict], Optional[str]]:
        """Line attribution, with a reason whenever the provider cannot give it.

        A provider without VCS metadata answers with no lines. Returning that as
        a bare empty list would be indistinguishable from "this file has no
        recorded history" — a silent gap in exactly the place the debugger claims
        to be rigorous — so the reason is stated instead.
        """
        try:
            lines = await self.provider.get_blame(path, revision)
        except (RepositoryError, ProviderError) as error:
            return [], str(error)
        if not lines:
            try:
                info = await self.provider.describe()
            except (RepositoryError, ProviderError):
                info = None
            if info is None or not info.vcs_present:
                return [], (
                    "the configured provider has no VCS metadata for this repository, "
                    "so line attribution is unavailable"
                )
            return [], f"no recorded history for {path} at this revision"
        bound = min(len(lines), settings.CODE_BLAME_MAX_LINES)
        return [_blame_dict(item) for item in lines[:bound]], None


def _commit_dict(commit: CommitInfo) -> dict:
    return {
        "sha": commit.sha,
        "short_sha": (commit.sha or "")[:12],
        "author": commit.author,
        "committed_at": commit.committed_at.isoformat()
        if commit.committed_at
        else None,
        #: Metadata only. Never treated as a statement of intent (§18).
        "message": commit.message,
        "parents": list(commit.parents or []),
        "files_changed": commit.files_changed,
    }


def _diff_dict(entry: DiffEntry) -> dict:
    return {
        "path": entry.path,
        "status": entry.status,
        "old_path": entry.old_path,
    }


def _blame_dict(line: BlameLine) -> dict:
    #: The dataclass names the commit ``sha``; the API exposes it as
    #: ``commit_sha`` alongside its short form for display.
    return {
        "line": line.line,
        "commit_sha": (line.sha or "")[:12] or None,
        "author": line.author,
        "committed_at": line.committed_at.isoformat() if line.committed_at else None,
    }


__all__ = ["ChangeHistoryService", "HistoryResult"]
