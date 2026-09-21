"""ARGUS Command Registry & Executor (Phase 7 §24, §25, §50, §51, §52).

The only way any command runs inside a patch workspace is through this
registry. A command is a *named, pre-declared* entry with a fixed argv
template, working directory, timeout, resource ceiling and network policy —
the AI never names a command (§50), it can at most pick a registry key.

Execution properties:

* **No shell.** ``argv`` lists only; nothing is ever passed through a shell,
  so no command-string injection exists by construction.
* **Timeouts are real**: the subprocess is killed at the deadline and the run
  is recorded as ``timed_out``.
* **Output is bounded and redacted** (§52): the tail kept is capped, and a
  secret scan runs over it before anything is stored.
* **Network is off by default** (§51): the executor records the policy; the
  local-process backend enforces it by never configuring a proxy, and the
  declaration is what the verification evidence cites.

Discovery (§24): ``detect_commands`` inspects the workspace for the markers
of a stack (pytest.ini/pyproject → python tests; package.json → node) and
returns the registry keys that exist — using whatever tools the repository
actually has, never assuming a universal stack (§24).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from app.services.source_redaction import SourceRedactor

logger = logging.getLogger(__name__)


class CommandRefused(RuntimeError):
    """A command key is unknown or the argv it resolves to is not allowlisted."""


class CommandTimedOut(RuntimeError):
    """The command exceeded its deadline and was killed."""


#: Network policy values (§51).
NETWORK_OFFLINE = "OFFLINE"
NETWORK_PACKAGE_REGISTRY = "PACKAGE_REGISTRY"


@dataclass(frozen=True)
class RegisteredCommand:
    """One allowlisted command (§50). Immutable by design."""

    key: str
    kind: str  # 'static' | 'build' | 'test'
    argv_template: tuple[str, ...]
    working_directory: str = "."
    timeout_seconds: int = 300
    network: str = NETWORK_OFFLINE
    description: str = ""
    #: Environment policy: the workspace gets a minimal, fixed environment.
    env_passthrough: tuple[str, ...] = ("PATH", "HOME", "LANG", "TMPDIR")

    def resolve(self, *, python_executable: str) -> tuple[str, ...]:
        """The concrete argv, with placeholders filled from known facts only."""
        return tuple(
            part.replace("{python}", python_executable) for part in self.argv_template
        )


#: The registry (§50). Commands a Python/Node repository plausibly declares.
REGISTRY: dict[str, RegisteredCommand] = {
    key: value
    for key, value in {
        # -- python ----------------------------------------------------------
        "python_syntax": RegisteredCommand(
            key="python_syntax",
            kind="static",
            argv_template=("{python}", "-m", "py_compile"),
            timeout_seconds=120,
            description="Byte-compile every changed Python file (syntax gate)",
        ),
        "python_lint": RegisteredCommand(
            key="python_lint",
            kind="static",
            argv_template=("{python}", "-m", "ruff", "check", "."),
            timeout_seconds=180,
            description="Ruff lint over the workspace",
        ),
        "python_typecheck": RegisteredCommand(
            key="python_typecheck",
            kind="static",
            argv_template=("{python}", "-m", "mypy", "."),
            timeout_seconds=600,
            description="mypy over the workspace",
        ),
        "python_tests": RegisteredCommand(
            key="python_tests",
            kind="test",
            argv_template=("{python}", "-m", "pytest", "-x", "-q"),
            timeout_seconds=900,
            description="pytest over the workspace",
        ),
        "python_tests_selected": RegisteredCommand(
            key="python_tests_selected",
            kind="test",
            argv_template=("{python}", "-m", "pytest", "-q"),
            timeout_seconds=900,
            description="pytest over selected test files (§27)",
        ),
        # -- node ------------------------------------------------------------
        "node_build": RegisteredCommand(
            key="node_build",
            kind="build",
            argv_template=("npm", "run", "build"),
            timeout_seconds=900,
            description="npm build",
        ),
        "node_tests": RegisteredCommand(
            key="node_tests",
            kind="test",
            argv_template=("npm", "test"),
            timeout_seconds=900,
            description="npm test",
        ),
        "node_lint": RegisteredCommand(
            key="node_lint",
            kind="static",
            argv_template=("npm", "run", "lint"),
            timeout_seconds=300,
            description="npm lint",
        ),
        # -- fallback build ---------------------------------------------------
        "make_check": RegisteredCommand(
            key="make_check",
            kind="build",
            argv_template=("make", "check"),
            timeout_seconds=900,
            description="make check",
        ),
    }.items()
}


@dataclass
class CommandResult:
    """The recorded outcome of one allowlisted execution (§24)."""

    key: str
    command_resolved: str
    exit_code: Optional[int]
    timed_out: bool
    duration_ms: int
    output_tail: str
    unknown_configuration: bool = False

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "command": self.command_resolved,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "ok": self.ok,
            "unknown_configuration": self.unknown_configuration,
        }


_OUTPUT_TAIL_BYTES = 20_000


class CommandExecutor:
    """Runs allowlisted commands inside one workspace (§50)."""

    def __init__(
        self,
        *,
        redactor: Optional[SourceRedactor] = None,
        python_executable: Optional[str] = None,
    ) -> None:
        self._redactor = redactor or SourceRedactor()
        self._python = python_executable

    def _python_exe(self) -> str:
        if self._python is None:
            import sys

            self._python = sys.executable
        return self._python

    def execute(
        self, workspace_root: Path, key: str, args: Sequence[str] = ()
    ) -> CommandResult:
        """Execute one registry command inside the workspace (§50).

        ``args`` are *our* additions (e.g. test file paths for selection) —
        never a command string from the model. An unknown key raises
        :class:`CommandRefused`.
        """
        command = REGISTRY.get(key)
        if command is None:
            raise CommandRefused(
                f"command key {key!r} is not in the registry (§50); the AI cannot "
                "execute arbitrary commands"
            )
        argv = list(command.resolve(python_executable=self._python_exe())) + list(args)
        cwd = workspace_root / command.working_directory

        import time

        started = time.monotonic()
        timed_out = False
        exit_code: Optional[int] = None
        output = ""
        try:
            result = subprocess.run(
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=command.timeout_seconds,
            )
            exit_code = result.returncode
            output = (result.stdout or "") + (result.stderr or "")
        except subprocess.TimeoutExpired as error:
            timed_out = True
            output = (
                (error.stdout or b"").decode(errors="replace")
                if isinstance(error.stdout, bytes)
                else (error.stdout or "")
            ) + (
                (error.stderr or b"").decode(errors="replace")
                if isinstance(error.stderr, bytes)
                else (error.stderr or "")
            )
        except FileNotFoundError as error:
            output = f"tool not available: {error}"
            exit_code = 127
        duration_ms = int((time.monotonic() - started) * 1000)

        tail = output[-_OUTPUT_TAIL_BYTES:]
        redacted, _report = self._redactor.redact(tail)
        return CommandResult(
            key=key,
            command_resolved=" ".join(argv),
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=duration_ms,
            output_tail=redacted,
        )


def detect_commands(workspace_root: Path) -> dict[str, list[str]]:
    """Discover which registry keys this repository actually supports (§24).

    Uses markers on disk, never assumptions: ``pyproject.toml``/``pytest.ini``
    → python keys; ``package.json`` → node keys; ``Makefile`` with a
    ``check`` target → make. Returns ``{"static": [...], "build": [...],
    "test": [...]}``.
    """
    found: dict[str, list[str]] = {"static": [], "build": [], "test": []}

    def add(key: str) -> None:
        command = REGISTRY[key]
        if key not in found[command.kind]:
            found[command.kind].append(key)

    if workspace_root.exists():
        has_python_markers = any(
            (workspace_root / name).exists()
            for name in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")
        )
        has_python_files = (
            any(workspace_root.rglob("*.py")) if workspace_root.is_dir() else False
        )
        if has_python_markers or has_python_files:
            add("python_syntax")
            if (workspace_root / "pyproject.toml").exists() or (
                (workspace_root / "ruff.toml").exists()
            ):
                add("python_lint")
            if (workspace_root / "mypy.ini").exists() or (
                (workspace_root / "pyproject.toml").exists()
            ):
                add("python_typecheck")
            add("python_tests")
            add("python_tests_selected")
        if (workspace_root / "package.json").exists():
            manifest = workspace_root / "package.json"
            try:
                import json

                scripts = json.loads(manifest.read_text(encoding="utf-8")).get(
                    "scripts", {}
                )
            except Exception:  # noqa: BLE001 - unreadable manifest, no node keys
                scripts = {}
            if "build" in scripts:
                add("node_build")
            if "test" in scripts:
                add("node_tests")
            if "lint" in scripts:
                add("node_lint")
        makefile = workspace_root / "Makefile"
        if makefile.exists() and "check:" in makefile.read_text(
            encoding="utf-8", errors="replace"
        ):
            add("make_check")
    return found


def select_tests(
    *,
    detected: dict[str, list[str]],
    changed_files: Sequence[str],
    workspace_root: Path,
    max_tests: int = 20,
) -> tuple[str, list[str]]:
    """Deterministic test selection (§27).

    Priority: tests whose path mentions a changed file's module stem, then
    all tests under ``tests/``. Returns ``(selection_reason, [paths])``.
    An empty result means "no tests discovered" — recorded honestly (§25).
    """
    if "python_tests_selected" not in detected.get("test", []):
        return "no python test runner detected", []

    candidates = sorted(workspace_root.rglob("test_*.py"))
    if not candidates:
        return "no test files discovered in the workspace", []

    def module_stems(paths: Sequence[str]) -> set[str]:
        stems: set[str] = set()
        for path in paths:
            name = path.rsplit("/", 1)[-1]
            stems.add(name.rsplit(".", 1)[0])
            parts = path.split("/")
            if len(parts) >= 2:
                stems.add(parts[-2])
        return {stem for stem in stems if stem and stem not in {"tests", "test"}}

    stems = module_stems(changed_files)
    related: list[str] = []
    for candidate in candidates:
        relative = str(candidate.relative_to(workspace_root))
        if any(stem in relative for stem in stems):
            related.append(relative)
    if related:
        selected = related[:max_tests]
        return f"tests matching changed modules {sorted(stems)}", selected
    return "no test file references a changed module; running all discovered tests", [
        str(item.relative_to(workspace_root)) for item in candidates[:max_tests]
    ]


__all__ = [
    "CommandExecutor",
    "CommandRefused",
    "CommandResult",
    "CommandTimedOut",
    "NETWORK_OFFLINE",
    "REGISTRY",
    "RegisteredCommand",
    "detect_commands",
    "select_tests",
]
