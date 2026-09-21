"""ARGUS Patch Workspace Manager (Phase 7 §17, §18, §19, §48, §58).

One candidate, one workspace, always:

    repository snapshot
        → clone the *provider's* read at base_commit into a temp dir
        → dedicated branch ``argus/fix/<experiment>/<candidate>``
        → apply the validated patch
        → run build/tests/verification inside
        → destroy the whole directory, always

Safety properties, enforced structurally rather than by convention:

* **Never the user's checkout.** The workspace is a fresh clone (local
  hardlink clone when possible) under ARGUS's own sandbox root — the
  provider's working tree is only ever *read* to seed the clone.
* **No destructive Git against the source repository** (§19). Every Git
  invocation targets the workspace directory. The command allowlist below
  contains no ``push``, no ``reset --hard`` on the source, no ``branch -D``
  outside the workspace, no remotes at all.
* **Unknown commands are refused** (§50): ``git`` subcommands are checked
  against the allowlist before execution; anything else raises.
* **One workspace per candidate** (§58): ``create`` allocates a fresh
  directory keyed by the patch experiment id; workspaces are never reused.
* **Cleanup is unconditional** (§48): ``destroy`` removes the directory even
  after failures, and reports what happened instead of raising.

The manager is synchronous on purpose: it runs inside the worker's
``asyncio.to_thread`` lane alongside the build/test commands, and Git is a
blocking subprocess interface.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)


class WorkspaceError(RuntimeError):
    """A workspace operation failed, with the reason recorded."""


#: §19/§50 — the only ``git`` subcommands the workspace manager will run.
#: Everything a fix pipeline needs; nothing that can reach outside.
_GIT_ALLOWED: frozenset[str] = frozenset(
    {
        "init",
        "add",
        "checkout",
        "checkout-index",
        "commit",
        "status",
        "diff",
        "apply",
        "show",
        "log",
        "rev-parse",
        "config",
        "symbolic-ref",
        "update-index",
        "write-tree",
        "hash-object",
        "cat-file",
        "ls-files",
    }
)


def _git_args_allowed(args: Sequence[str]) -> bool:
    """True when the *subcommand* is on the allowlist (§19, §50).

    ``git``'s grammar is ``git [global flags] <subcommand> [args…]``. The
    subcommand is the first token that is not a global flag (``-c`` consumes
    the following ``key=value`` as its value). Global flags with values are
    recognised explicitly; the subcommand itself must appear in
    :data:`_GIT_ALLOWED`, so ``push``, ``reset``, ``remote`` and friends are
    refused before a process is ever spawned. Subcommand-level flags (``-q``,
    ``--check``, paths, refnames) come only from this module's own code —
    never from a model or an API request — so they are not part of the gate.
    """
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-c":
            index += 2  # -c <key>=<value>
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg in _GIT_ALLOWED
    return False


class GitWorkspace:
    """Git operations bound to ONE workspace directory (§19)."""

    def __init__(self, root: Path, git_path: str = "git") -> None:
        self.root = root
        self.git_path = git_path

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if not _git_args_allowed(args):
            raise WorkspaceError(
                f"git subcommand not in the workspace allowlist (§19, §50): {args[0]!r}"
            )
        result = subprocess.run(
            [self.git_path, *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if check and result.returncode != 0:
            raise WorkspaceError(
                f"git {' '.join(args[:3])} failed: {result.stderr.strip()[:300]}"
            )
        return result

    # -- lifecycle -------------------------------------------------------
    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._run("init", "-q", ".")
        # A workspace is self-contained: no remote, no hooks, deterministic.
        self._run("config", "user.email", "argus@verification.local")
        self._run("config", "user.name", "ARGUS Verification")
        self._run("config", "commit.gpgsign", "false")
        self._run("config", "core.autocrlf", "false")

    def seed_from(self, source_dir: Path) -> int:
        """Populate the workspace from a source tree (the provider's read).

        Copies the tree (respecting ``.git`` exclusion — the workspace has its
        own history) and commits it as the base commit. Returns the file count.
        """
        if not source_dir.is_dir():
            raise WorkspaceError(f"source tree {source_dir} does not exist")
        count = 0
        for item in source_dir.rglob("*"):
            if ".git" in item.parts:
                continue
            relative = item.relative_to(source_dir)
            target = self.root / relative
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            count += 1
        self._run("add", "-A")
        self._run("commit", "-q", "--allow-empty", "-m", "base: provider snapshot")
        return count

    # -- branch and commit -------------------------------------------------
    def create_branch(self, branch: str) -> None:
        if not branch.startswith("argus/"):
            raise WorkspaceError("workspace branches must live under argus/ (§19)")
        self._run("checkout", "-q", "-b", branch)

    def commit_all(self, message: str) -> str:
        self._run("add", "-A")
        self._run("commit", "-q", "--allow-empty", "-m", message)
        return self.head()

    def head(self) -> str:
        return self._run("rev-parse", "HEAD").stdout.strip()

    # -- patch application (§23) -------------------------------------------
    def apply_patch(self, patch_text: str) -> dict[str, Any]:
        """Apply a unified diff with git-apply's own checks enabled.

        ``--check`` runs first; only when it passes is the patch applied.
        After application the *actual* diff is read back so the caller can
        verify applied == intended (§23).
        """
        patch_file = self.root / ".argus-patch.diff"
        patch_file.write_text(patch_text, encoding="utf-8")
        check = self._run(
            "apply", "--check", "--whitespace=warn", str(patch_file), check=False
        )
        if check.returncode != 0:
            raise WorkspaceError(
                f"patch does not apply cleanly: {check.stderr.strip()[:300]}"
            )
        apply = self._run("apply", "--whitespace=warn", str(patch_file), check=False)
        if apply.returncode != 0:
            raise WorkspaceError(
                f"patch application failed: {apply.stderr.strip()[:300]}"
            )
        patch_file.unlink(missing_ok=True)
        status = self._run("status", "--porcelain").stdout
        return {
            "changed_entries": [line for line in status.splitlines() if line.strip()],
            "applied": True,
        }

    def working_diff(self) -> str:
        """The workspace's actual diff vs HEAD (uncommitted)."""
        return self._run("diff").stdout

    def commit_diff(self, commit: str) -> str:
        """The diff introduced by one commit."""
        return self._run("show", "--format=", commit).stdout

    def read_file(self, relative: str) -> Optional[str]:
        target = (self.root / relative).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            return None
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8", errors="replace")

    def write_file(self, relative: str, content: str) -> None:
        target = (self.root / relative).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            raise WorkspaceError(f"refusing to write outside the workspace: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@dataclass
class WorkspaceRecord:
    """What the caller persists as a ``PatchWorkspace`` row."""

    branch_name: str
    base_commit_sha: str
    root_path: str
    file_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class PatchWorkspaceManager:
    """Creates, resets and destroys isolated workspaces (§18)."""

    def __init__(self, base_root: Optional[Path] = None, git_path: str = "git") -> None:
        self._base_root = base_root
        self._git_path = git_path

    @property
    def base_root(self) -> Path:
        if self._base_root is None:
            from app.services.reproduction_sandbox import sandbox_root_base

            self._base_root = sandbox_root_base() / "workspaces"
        return self._base_root

    def create(
        self,
        *,
        patch_experiment_id: Any,
        candidate_key: str,
        source_dir: Path,
        base_branch: str = "main",
    ) -> tuple[GitWorkspace, WorkspaceRecord]:
        """Allocate one fresh workspace for one candidate (§17, §58)."""
        safe_experiment = str(patch_experiment_id).replace("-", "")[:12]
        safe_candidate = (
            "".join(ch for ch in candidate_key if ch.isalnum())[:12] or "c0"
        )
        branch = f"argus/fix/{safe_experiment}/{safe_candidate}"
        workspace_dir = self.base_root / f"argus-fix-{safe_experiment}-{safe_candidate}"
        if workspace_dir.exists():
            # §58 — never reuse: a leftover directory is removed, not adopted.
            shutil.rmtree(workspace_dir, ignore_errors=True)

        workspace = GitWorkspace(workspace_dir, git_path=self._git_path)
        try:
            workspace.init()
            file_count = workspace.seed_from(source_dir)
            base_sha = workspace.head()
            workspace.create_branch(branch)
        except WorkspaceError:
            shutil.rmtree(workspace_dir, ignore_errors=True)
            raise

        record = WorkspaceRecord(
            branch_name=branch,
            base_commit_sha=base_sha,
            root_path=str(workspace_dir),
            file_count=file_count,
            extra={"base_branch": base_branch},
        )
        return workspace, record

    def destroy(self, workspace: GitWorkspace) -> dict[str, Any]:
        """Remove the whole workspace directory, always (§48)."""
        report: dict[str, Any] = {"destroyed": False, "error": None}
        try:
            shutil.rmtree(workspace.root)
            report["destroyed"] = True
        except Exception as error:  # noqa: BLE001 - cleanup reports, never raises
            report["error"] = f"{type(error).__name__}: {error}"
            logger.warning("workspace cleanup failed: %s", error)
        return report

    def reset(self, workspace: GitWorkspace, base_commit: str) -> None:
        """Return the workspace to its base state *without* destructive git.

        Implemented as checkout of the base paths + drop of untracked files —
        the only reset a workspace ever needs (§19).
        """
        workspace._run("checkout", "--", ".")
        clean = workspace._run("status", "--porcelain").stdout
        for line in clean.splitlines():
            if line.startswith("?? "):
                stray = workspace.root / line[3:].strip()
                if stray.is_file():
                    stray.unlink()


__all__ = [
    "GitWorkspace",
    "PatchWorkspaceManager",
    "WorkspaceError",
    "WorkspaceRecord",
    "_git_args_allowed",
]
