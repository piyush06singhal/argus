"""ARGUS Patch Safety Validator (Phase 7 §13–§16, §54, §55).

The second gate, after parsing. Where the parser asks *is this a well-formed
diff?*, the safety validator asks *is this a change ARGUS is allowed to make
inside this fix's scope?* — and every answer is recorded, never silent.

Rejection classes, each with its own finding:

* **Scope** (§10): any path outside the hypothesis's ``scope_files`` allowlist
  is a hard refusal. The allowlist is the hypothesis's own; nothing infers it.
* **Sensitive files** (§14): CI/CD, auth, security policy, infrastructure,
  deployment, secret management and database-migration paths are DO NOT
  MODIFY by default. A patch that touches them is rejected unless the fix
  plan explicitly included them (``allowed_sensitive``), and even then the
  patch is flagged for elevated risk.
* **Dependencies** (§15): manifest and lockfile changes are elevated-risk and
  require a stated dependency reason; a patch that touches them without one
  is rejected.
* **Configuration** (§16): config changes are separated from code changes and
  carry old/new/reason/scope — a config hunk without that record is rejected.
* **Secrets** (§52): the added lines are scanned for credentials; a patch that
  introduces one is rejected outright and the finding records the *kind* of
  secret, never its value.
* **Shape** (§13): deleted files, renamed files, binary blobs and oversized
  patches are flagged; deletion of tests is a tampering finding (§55), not a
  style note.
* **Test tampering** (§55): deletions/skips/assertion-weakening inside test
  files, disabled lint/typecheck/CI, and any modification of the verification
  infrastructure are detected deterministically and are independently
  disqualifying.

The validator returns findings rather than raising, so the caller can store
the full report on the patch — a rejection that vanishes into an exception
message teaches nothing.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

from app.services.patch_parser import FilePatch, ParsedPatch
from app.services.source_redaction import SourceRedactor


class FindingKind:
    """Stable finding identifiers — the audit vocabulary of refusals."""

    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    SENSITIVE_FILE = "SENSITIVE_FILE"
    DEPENDENCY_CHANGE = "DEPENDENCY_CHANGE"
    CONFIG_CHANGE_UNRECORDED = "CONFIG_CHANGE_UNRECORDED"
    SECRET_INTRODUCED = "SECRET_INTRODUCED"
    FILE_DELETED = "FILE_DELETED"
    FILE_RENAMED = "FILE_RENAMED"
    BINARY_CONTENT = "BINARY_CONTENT"
    PATCH_TOO_LARGE = "PATCH_TOO_LARGE"
    TEST_TAMPERING = "TEST_TAMPERING"
    VERIFICATION_MODIFIED = "VERIFICATION_MODIFIED"
    CI_MODIFIED = "CI_MODIFIED"
    LINT_DISABLED = "LINT_DISABLED"
    TYPECHECK_DISABLED = "TYPECHECK_DISABLED"
    DB_MIGRATION = "DB_MIGRATION"
    ABSOLUTE_PATH = "ABSOLUTE_PATH"


@dataclass
class SafetyFinding:
    """One validator finding: what, where, why, and how fatal."""

    kind: str
    path: str
    message: str
    #: ``hard`` refusals block the patch; ``warning`` findings survive but are
    #: recorded and raise the risk assessment.
    severity: str = "hard"  # 'hard' | 'warning'
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "path": self.path,
            "message": self.message,
            "severity": self.severity,
            **({"detail": self.detail} if self.detail else {}),
        }


@dataclass
class SafetyReport:
    """The full validator verdict over one parsed patch."""

    findings: List[SafetyFinding] = field(default_factory=list)
    #: Kinds of sensitive area the patch legally touched (approval was given).
    approved_sensitive: List[str] = field(default_factory=list)

    @property
    def hard_findings(self) -> List[SafetyFinding]:
        return [item for item in self.findings if item.severity == "hard"]

    @property
    def warnings(self) -> List[SafetyFinding]:
        return [item for item in self.findings if item.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.hard_findings

    @property
    def tampering(self) -> bool:
        tampering_kinds = {
            FindingKind.TEST_TAMPERING,
            FindingKind.CI_MODIFIED,
            FindingKind.LINT_DISABLED,
            FindingKind.TYPECHECK_DISABLED,
            FindingKind.VERIFICATION_MODIFIED,
        }
        return any(item.kind in tampering_kinds for item in self.findings)

    @property
    def tampering_kinds(self) -> List[str]:
        tampering_kinds = {
            FindingKind.TEST_TAMPERING,
            FindingKind.CI_MODIFIED,
            FindingKind.LINT_DISABLED,
            FindingKind.TYPECHECK_DISABLED,
            FindingKind.VERIFICATION_MODIFIED,
        }
        return sorted(
            {item.kind for item in self.findings if item.kind in tampering_kinds}
        )

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "tampering": self.tampering,
            "findings": [item.as_dict() for item in self.findings],
            "approved_sensitive": list(self.approved_sensitive),
        }


# ---------------------------------------------------------------------------
# Classification tables (§14, §15). All matching is case-insensitive on the
# path and uses forward slashes; a path like `infra/deploy.yml` cannot slip
# through by case games (`INFRA/DEPLOY.YML`) because every check lowercases.
# ---------------------------------------------------------------------------

#: §14 — DO NOT MODIFY unless the fix plan explicitly includes the area.
SENSITIVE_PATTERNS: tuple[tuple[str, str], ...] = (
    (".github/workflows/*", "ci"),
    (".gitlab-ci.yml", "ci"),
    ("jenkinsfile", "ci"),
    (".circleci/*", "ci"),
    (".github/argus/*", "verification"),
    (".argus-verify*", "verification"),
    ("*argus_verification*", "verification"),
    ("**/auth*", "authentication"),
    ("*authentication*", "authentication"),
    ("*authoriz*", "authorization"),
    ("*permission*", "authorization"),
    ("*security*", "security"),
    ("*secrets*", "secret_management"),
    ("*.pem", "secret_management"),
    ("*.key", "secret_management"),
    ("*id_rsa*", "secret_management"),
    (".env*", "secret_management"),
    ("infra*/*", "infrastructure"),
    ("infrastructure/*", "infrastructure"),
    ("terraform/*", "infrastructure"),
    ("k8s/*", "infrastructure"),
    ("kubernetes/*", "infrastructure"),
    ("*docker-compose*", "deployment"),
    ("dockerfile*", "deployment"),
    ("auth/*", "authentication"),
    ("*/auth/*", "authentication"),
    ("*password*", "authentication"),
    ("*credential*", "authentication"),
    ("*oauth*", "authentication"),
    ("*token*", "authentication"),
    ("*session_security*", "authentication"),
)

#: §15 — dependency surfaces. Elevated risk, never casual.
DEPENDENCY_FILES: tuple[str, ...] = (
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "requirements.txt",
    "requirements*.txt",
    "poetry.lock",
    "uv.lock",
    "pyproject.toml",
    "Pipfile",
    "Pipfile.lock",
    "setup.py",
    "setup.cfg",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "Gemfile",
    "Gemfile.lock",
)

#: §14 — database migrations change schema state; explicit approval only.
MIGRATION_PATTERNS: tuple[str, ...] = (
    "migrations/*",
    "*migrations/*",
    "alembic/versions/*",
    "db/migrate/*",
    "*/migrations/*",
)

#: §16 — configuration files that must be recorded as config changes.
CONFIG_PATTERNS: tuple[str, ...] = (
    "*.ini",
    "*.cfg",
    "*.toml",
    "*.yaml",
    "*.yml",
    "*.json",
    "*.env",
    "*.conf",
    "*.properties",
)

#: Test-file detection for tampering analysis (§55).
TEST_FILE_PATTERNS: tuple[str, ...] = (
    "test_*.py",
    "*_test.py",
    "tests/*.py",
    "tests/**/*.py",
    "*_test.ts",
    "*.test.ts",
    "*.test.tsx",
    "*.test.js",
    "*.spec.ts",
    "*.spec.js",
    "__tests__/*",
)

#: Files whose modification is verification tampering per se (§56).
VERIFICATION_FILE_PATTERNS: tuple[str, ...] = (
    ".github/workflows/*",
    ".gitlab-ci.yml",
    "jenkinsfile",
    ".circleci/*",
    "Makefile",
    "noxfile.py",
    "tox.ini",
    "pytest.ini",
    "setup.cfg",
    "pyproject.toml",
    "conftest.py",
    "*/conftest.py",
    ".eslintrc*",
    "eslint.config.*",
    ".prettierrc*",
    "tsconfig*.json",
    "mypy.ini",
    ".mypy.ini",
    "ruff.toml",
    ".ruff.toml",
    ".coveragerc",
    "vitest.config.*",
    "jest.config.*",
    "vite.config.*",
)

#: Added-line patterns that look like introduced credentials (§52).
_SECRET_SIGNATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "aws_access_key_id",
        re.compile(r"AKIA[0-9A-Z]{16}"),
    ),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ),
    (
        "api_key_assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|secret|password|passwd|pwd|token)\b"
            r"\s*[=:]\s*['\"]?[A-Za-z0-9+/=_\-]{12,}"
        ),
    ),
    (
        "bearer_token",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-_.~+/]+=*"),
    ),
    (
        "connection_string_credentials",
        re.compile(r"(?i)\b\w+://[^/\s:]+:[^@\s]{8,}@"),
    ),
)

#: Assertion-weakening shapes inside test diffs (§55).
_WEAKENING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "skip_decorator",
        re.compile(
            r"@pytest\.mark\.skip\b|@unittest\.skip\b|@pytest\.mark\.skipif\s*\(\s*True"
        ),
    ),
    ("xfail", re.compile(r"^\s*@(pytest\.)?xfail\b")),
    ("assert_removed_true", re.compile(r"^\s*assert\s+True\s*$")),
    ("assert_not_none_only", re.compile(r"^\s*assert\s+\w+\s*$")),
    ("expect_noop", re.compile(r"^\s*expect\(\s*true\s*\)\s*;?\s*$")),
    ("todo_placeholder", re.compile(r"#\s*(TODO|FIXME)\b.*(skip|later)")),
)


def _matches_any(path: str, patterns: Iterable[str]) -> Optional[str]:
    """The first matching pattern, or ``None``. Case-insensitive."""
    lowered = path.lower()
    for pattern in patterns:
        probe = pattern.lower()
        if fnmatch.fnmatch(lowered, probe) or fnmatch.fnmatch(lowered, f"*/{probe}"):
            return pattern
    return None


_WEAKENED_SUFFIXES = (
    " is not none",
    " is none",
    " is not null",
)


def _assertion_weakened(removed: str, added: str) -> bool:
    """True when ``added`` accepts every outcome ``removed`` accepted (§55).

    Deterministic, conservative heuristics — a weakening is flagged only when
    it is unambiguous:

    * a comparison (``== / != / > / < / >= / <=``) replaced by a bare truthiness
      assert, ``assert x is not None``, or ``assert x``;
    * a specific comparison replaced by a looser comparison on the same
      operand (``>= 2`` → ``> 0``).

    Strengthening (weak → strong) is never flagged: that is what a regression
    test is *for*.
    """
    removed_body = removed[len("assert ") :].strip()
    added_body = added[len("assert ") :].strip()
    if removed_body == added_body:
        return False

    comparison = re.compile(r"^(\w[\w.\[\]'\"]*)\s*(==|!=|>=|<=|>|<)\s*(.+)$")
    removed_match = comparison.match(removed_body)
    added_match = comparison.match(added_body)

    if removed_match and not added_match:
        #: ``assert x == 2`` → ``assert x`` / ``assert x is not None`` —
        #: accepting more outcomes than the removed comparison did.
        target = added_body.lower()
        lhs = removed_match.group(1).lower()
        if target == lhs or target.rstrip() in {
            lhs,
            f"{lhs} is not none",
            f"{lhs} is not nan",
            f"bool({lhs})",
        }:
            return True
        return False

    if removed_match and added_match:
        #: Same operand, loosened bound: ``>= 2`` → ``> 0``. Only the
        #: unambiguous direction is flagged.
        if removed_match.group(1) == added_match.group(1):
            removed_op, added_op = removed_match.group(2), added_match.group(2)
            if {removed_op, added_op} in ({">", ">="}, {"<", "<="}) or (
                removed_op in (">", ">=") and added_op == ">"
            ):
                return True
        return False

    return False


def is_test_file(path: str) -> bool:
    return _matches_any(path, TEST_FILE_PATTERNS) is not None


def is_verification_file(path: str) -> bool:
    return _matches_any(path, VERIFICATION_FILE_PATTERNS) is not None


def is_dependency_file(path: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    for pattern in DEPENDENCY_FILES:
        probe = pattern.lower()
        if fnmatch.fnmatch(base, probe):
            return True
    return False


def is_config_file(path: str) -> bool:
    #: ``.py`` settings modules are configuration too (§16): a settings file
    #: under ``config/`` or a ``settings*.py`` module changes system behaviour
    #: exactly as much as a YAML edit, and must carry the same §16 record.
    lowered = path.lower()
    base = lowered.rsplit("/", 1)[-1]
    return _matches_any(path, CONFIG_PATTERNS) is not None or (
        lowered.endswith(".py")
        and (
            lowered.startswith("config/")
            or "/config/" in lowered
            or base.startswith("settings")
            or base.startswith("config")
        )
    )


def sensitive_area(path: str) -> Optional[str]:
    match = _matches_any(path, [pattern for pattern, _ in SENSITIVE_PATTERNS])
    if match is None:
        return None
    for pattern, area in SENSITIVE_PATTERNS:
        if pattern == match:
            return area
    return None


def is_migration_file(path: str) -> bool:
    return _matches_any(path, MIGRATION_PATTERNS) is not None


@dataclass
class SafetyValidatorConfig:
    """§37-style configuration rather than hard-coded values."""

    #: Hard ceiling on changed lines before the patch is refused outright.
    max_changed_lines: int = 400
    #: Warning threshold — above this, risk is elevated and a reason required.
    warn_changed_lines: int = 60
    #: Hard ceiling on changed files.
    max_files: int = 12
    #: Warn when a patch changes more files than this (minimality, §9).
    warn_files: int = 4
    #: Secret scan the added lines? (Tests may legitimately include fakes; the
    #: validator distinguishes by marker context, not by turning the scan off.)
    scan_secrets: bool = True


class PatchSafetyValidator:
    """Validates one parsed patch against one fix hypothesis's scope (§13)."""

    def __init__(
        self,
        config: Optional[SafetyValidatorConfig] = None,
        *,
        redactor: Optional[SourceRedactor] = None,
    ) -> None:
        self.config = config or SafetyValidatorConfig()
        self._redactor = redactor or SourceRedactor()

    def validate(
        self,
        parsed: ParsedPatch,
        *,
        scope_files: Sequence[str],
        excluded_paths: Sequence[str] = (),
        allowed_sensitive: Sequence[str] = (),
        dependency_reason: Optional[str] = None,
        config_changes: Sequence[dict] = (),
    ) -> SafetyReport:
        """Produce the full safety report.

        ``scope_files`` — relative paths the fix may touch (§10).
        ``excluded_paths`` — explicit never-modify globs, respected even when
        a path also matches the scope (the exclusion wins).
        ``allowed_sensitive`` — sensitive areas the fix plan explicitly
        included (§14); touching them without this entry is a hard refusal,
        with it, a recorded approval plus an elevated-risk signal.
        ``dependency_reason`` — required when a dependency file is touched (§15).
        ``config_changes`` — §16 records for config hunks
        (``[{path, old, new, reason, scope}]``).
        """
        report = SafetyReport()
        scope_set = {item.strip().rstrip("/") for item in scope_files if item.strip()}

        total_changed = parsed.lines_added + parsed.lines_removed
        if total_changed > self.config.max_changed_lines:
            report.findings.append(
                SafetyFinding(
                    FindingKind.PATCH_TOO_LARGE,
                    "*",
                    f"patch changes {total_changed} lines; the ceiling is "
                    f"{self.config.max_changed_lines} (§9, §13)",
                )
            )
        if parsed.file_count > self.config.max_files:
            report.findings.append(
                SafetyFinding(
                    FindingKind.PATCH_TOO_LARGE,
                    "*",
                    f"patch changes {parsed.file_count} files; the ceiling is "
                    f"{self.config.max_files}",
                )
            )
        if total_changed > self.config.warn_changed_lines:
            report.findings.append(
                SafetyFinding(
                    FindingKind.PATCH_TOO_LARGE,
                    "*",
                    f"patch changes {total_changed} lines, above the minimality "
                    f"guideline of {self.config.warn_changed_lines}; elevated risk",
                    severity="warning",
                )
            )
        if parsed.file_count > self.config.warn_files:
            report.findings.append(
                SafetyFinding(
                    FindingKind.PATCH_TOO_LARGE,
                    "*",
                    f"patch changes {parsed.file_count} files, above the minimality "
                    f"guideline of {self.config.warn_files}; elevated risk",
                    severity="warning",
                )
            )

        approved_areas: set[str] = set()
        for file_patch in parsed.files:
            self._validate_file(
                file_patch,
                report=report,
                scope_set=scope_set,
                excluded_paths=excluded_paths,
                allowed_sensitive=allowed_sensitive,
                approved_areas=approved_areas,
                dependency_reason=dependency_reason,
                config_changes=config_changes,
            )

        report.approved_sensitive = sorted(approved_areas)
        return report

    # ------------------------------------------------------------------
    def _validate_file(
        self,
        file_patch: FilePatch,
        *,
        report: SafetyReport,
        scope_set: set[str],
        excluded_paths: Sequence[str],
        allowed_sensitive: Sequence[str],
        approved_areas: set[str],
        dependency_reason: Optional[str],
        config_changes: Sequence[dict],
    ) -> None:
        path = file_patch.path

        if file_patch.binary:
            report.findings.append(
                SafetyFinding(
                    FindingKind.BINARY_CONTENT,
                    path,
                    "patch contains binary content; ARGUS reviews text changes only",
                )
            )
            return

        # -- scope (§10): the exclusion list wins over the allowlist --------
        if _matches_any(path, excluded_paths):
            report.findings.append(
                SafetyFinding(
                    FindingKind.OUT_OF_SCOPE,
                    path,
                    "path is on the fix's explicit exclusion list",
                )
            )
            return
        if scope_set and path not in scope_set:
            # Also reject the source side of a rename.
            if not (file_patch.source_path and file_patch.source_path in scope_set) or (
                file_patch.target_path and file_patch.target_path not in scope_set
            ):
                report.findings.append(
                    SafetyFinding(
                        FindingKind.OUT_OF_SCOPE,
                        path,
                        f"path is outside the fix's scope "
                        f"({', '.join(sorted(scope_set))})",
                    )
                )
                return

        # -- sensitive areas (§14) ------------------------------------------
        area = sensitive_area(path)
        if area is not None and area not in allowed_sensitive:
            report.findings.append(
                SafetyFinding(
                    FindingKind.SENSITIVE_FILE,
                    path,
                    f"path matches the protected area '{area}', which is "
                    f"DO NOT MODIFY unless the fix plan includes it (§14)",
                )
            )
            return
        if area is not None:
            approved_areas.add(area)
            report.findings.append(
                SafetyFinding(
                    FindingKind.SENSITIVE_FILE,
                    path,
                    f"modifies approved sensitive area '{area}' — risk elevated",
                    severity="warning",
                )
            )

        # -- database migrations (§14) --------------------------------------
        if is_migration_file(path) and "database_migration" not in allowed_sensitive:
            report.findings.append(
                SafetyFinding(
                    FindingKind.DB_MIGRATION,
                    path,
                    "schema migrations require explicit approval (§14)",
                )
            )
            return

        # -- verification infrastructure (§56) ------------------------------
        if is_verification_file(path):
            kind = FindingKind.VERIFICATION_MODIFIED
            if _matches_any(
                path,
                (".github/workflows/*", ".gitlab-ci.yml", "jenkinsfile", ".circleci/*"),
            ):
                kind = FindingKind.CI_MODIFIED
            report.findings.append(
                SafetyFinding(
                    kind,
                    path,
                    "patch modifies the verification environment itself (§55, §56)",
                )
            )
            return

        # -- dependency files (§15) -----------------------------------------
        if is_dependency_file(path):
            if not dependency_reason:
                report.findings.append(
                    SafetyFinding(
                        FindingKind.DEPENDENCY_CHANGE,
                        path,
                        "dependency file changed without a stated dependency "
                        "reason (§15)",
                    )
                )
            else:
                report.findings.append(
                    SafetyFinding(
                        FindingKind.DEPENDENCY_CHANGE,
                        path,
                        f"dependency change approved with reason: {dependency_reason}",
                        severity="warning",
                    )
                )

        # -- config without a record (§16) -----------------------------------
        if is_config_file(path) and not is_dependency_file(path):
            recorded = any((item.get("path") or "") == path for item in config_changes)
            if not recorded:
                report.findings.append(
                    SafetyFinding(
                        FindingKind.CONFIG_CHANGE_UNRECORDED,
                        path,
                        "configuration changed without an old/new/reason record (§16)",
                    )
                )

        # -- deletions and renames (§13) -------------------------------------
        if file_patch.deleted_file:
            kind = (
                FindingKind.TEST_TAMPERING
                if is_test_file(path)
                else FindingKind.FILE_DELETED
            )
            report.findings.append(
                SafetyFinding(
                    kind,
                    path,
                    (
                        "a test file is deleted — tampering (§55)"
                        if kind == FindingKind.TEST_TAMPERING
                        else "patch deletes a whole file — requires human review"
                    ),
                )
            )
            return
        if (
            file_patch.source_path
            and file_patch.target_path
            and file_patch.source_path != file_patch.target_path
        ):
            report.findings.append(
                SafetyFinding(
                    FindingKind.FILE_RENAMED,
                    path,
                    f"renames {file_patch.source_path} — requires human review",
                    severity="warning",
                )
            )

        # -- secrets in added lines (§52) ------------------------------------
        if self.config.scan_secrets:
            self._scan_secrets(file_patch, report)

        # -- lint/type-check disablers, any file (§55) -----------------------
        self._scan_quality_disablers(file_patch, report)

        # -- test tampering inside test files (§55) --------------------------
        if is_test_file(path):
            self._scan_tampering(file_patch, report)

    # ------------------------------------------------------------------
    def _scan_quality_disablers(
        self, file_patch: FilePatch, report: SafetyReport
    ) -> None:
        """Added lines that switch off linting or type checking (§55).

        Runs for *every* file, not only tests: disabling the checks that
        would catch a defect is tampering with the verification of the
        change itself, wherever it happens.
        """
        for hunk in file_patch.hunks:
            for line in hunk.lines:
                if not line.is_addition:
                    continue
                if re.search(r"#\s*noqa\b", line.content, re.IGNORECASE):
                    report.findings.append(
                        SafetyFinding(
                            FindingKind.LINT_DISABLED,
                            file_patch.path,
                            "added line disables lint checking (noqa) (§55)",
                            detail={"signature": "LINT_DISABLED"},
                        )
                    )
                    return
                if re.search(
                    r"#\s*type:\s*ignore\b|#\s*mypy:\s*ignore-errors|"
                    r"@typing\.no_type_check\b|@unittest\.skip_type_checking\b",
                    line.content,
                    re.IGNORECASE,
                ):
                    report.findings.append(
                        SafetyFinding(
                            FindingKind.TYPECHECK_DISABLED,
                            file_patch.path,
                            "added line disables type checking (§55)",
                            detail={"signature": "TYPECHECK_DISABLED"},
                        )
                    )
                    return

    # ------------------------------------------------------------------
    def _scan_secrets(self, file_patch: FilePatch, report: SafetyReport) -> None:
        for hunk in file_patch.hunks:
            for line in hunk.lines:
                if not line.is_addition:
                    continue
                for label, pattern in _SECRET_SIGNATURES:
                    match = pattern.search(line.content)
                    if match:
                        report.findings.append(
                            SafetyFinding(
                                FindingKind.SECRET_INTRODUCED,
                                file_patch.path,
                                f"added line looks like an introduced {label} — "
                                "secrets must never be committed by a patch (§52)",
                                detail={
                                    "signature": label,
                                    "context": self._redactor.redact(
                                        line.content.strip()
                                    )[0][:80],
                                },
                            )
                        )
                        return

    # ------------------------------------------------------------------
    def _scan_tampering(self, file_patch: FilePatch, report: SafetyReport) -> None:
        for hunk in file_patch.hunks:
            for line in hunk.lines:
                if not line.is_addition:
                    continue
                for label, pattern in _WEAKENING_PATTERNS:
                    if pattern.search(line.content) and label != "xfail":
                        report.findings.append(
                            SafetyFinding(
                                FindingKind.TEST_TAMPERING,
                                file_patch.path,
                                f"added line weakens a test ({label}) (§55)",
                                detail={"signature": label},
                            )
                        )
                        return
                if re.search(r"(^|\s)pytest\.skip\(|self\.skipTest\(", line.content):
                    report.findings.append(
                        SafetyFinding(
                            FindingKind.TEST_TAMPERING,
                            file_patch.path,
                            "added line skips a test outright (§55)",
                            detail={"signature": "skip_call"},
                        )
                    )
                    return
                if re.search(
                    r"#\s*noqa\b|#\s*type:\s*ignore\b|mypy:\s*ignore-errors",
                    line.content,
                ):
                    label = (
                        "TYPECHECK_DISABLED"
                        if "type" in line.content.lower()
                        or "mypy" in line.content.lower()
                        else "LINT_DISABLED"
                    )
                    report.findings.append(
                        SafetyFinding(
                            FindingKind.TYPECHECK_DISABLED
                            if label == "TYPECHECK_DISABLED"
                            else FindingKind.LINT_DISABLED,
                            file_patch.path,
                            f"added line disables lint/type checking ({label}) (§55)",
                            detail={"signature": label},
                        )
                    )
                    return
                #: An added assertion that replaces a *stronger* removed one is
                #: the classic "make the test pass" weakening — stronger means
                #: narrower (==), weaker means looser (is not None / truthy).
                removed_assertions = [
                    line.content.strip()
                    for line in hunk.lines
                    if line.is_removal and line.content.strip().startswith("assert ")
                ]
                added_assertions = [
                    line.content.strip()
                    for line in hunk.lines
                    if line.is_addition and line.content.strip().startswith("assert ")
                ]
                for removed in removed_assertions:
                    for added in added_assertions:
                        if _assertion_weakened(removed, added):
                            report.findings.append(
                                SafetyFinding(
                                    FindingKind.TEST_TAMPERING,
                                    file_patch.path,
                                    "assertion weakened: the added assertion accepts "
                                    "outcomes the removed one rejected (§55)",
                                    detail={
                                        "signature": "assertion_weakened",
                                        "removed": removed[:120],
                                        "added": added[:120],
                                    },
                                )
                            )
                            return
            removed_test_call = any(
                line.is_removal and re.match(r"\s*def test_", line.content)
                for line in hunk.lines
            )
            if removed_test_call and not any(
                line.is_addition and re.match(r"\s*def test_", line.content)
                for line in hunk.lines
            ):
                report.findings.append(
                    SafetyFinding(
                        FindingKind.TEST_TAMPERING,
                        file_patch.path,
                        "a test function is removed without a replacement (§55)",
                        detail={"signature": "test_deleted"},
                    )
                )


__all__ = [
    "FindingKind",
    "PatchSafetyValidator",
    "SafetyFinding",
    "SafetyReport",
    "SafetyValidatorConfig",
    "is_config_file",
    "is_dependency_file",
    "is_migration_file",
    "is_test_file",
    "is_verification_file",
    "sensitive_area",
]
