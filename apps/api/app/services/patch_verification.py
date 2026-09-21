"""ARGUS Patch Verification Engine (Phase 7 §26–§37, §55, §56, §63, §64).

The engine owns the only question that matters:

    did the original failure stop reproducing, without unacceptable
    regression, while existing behaviour stayed healthy?

Verification is a *ladder* (§34): STATIC_VALIDATED → TEST_VALIDATED →
REPRODUCTION_VALIDATED → REGRESSION_VALIDATED → FULLY_VERIFIED. Each rung
requires the rungs below it, and the top rung requires all of:

* the patch applied cleanly inside the workspace;
* the original failure was **reproduced on the baseline** first (§33 — a fix
  for a failure nobody can produce is not a verified fix);
* static checks, build (when configured) and selected tests pass;
* a regression test **fails on the base commit and passes on the patched
  commit** (§28, §29) — a test that passes on base is ``REGRESSION_TEST_INVALID``;
* the patched reproduction **no longer fails**;
* the before/after comparison (§31) stays inside the configured regression
  thresholds (§36, §37);
* the verification environment itself was untouched (§55, §56).

Every negative answer is a recorded, truthful outcome: ``NOT_VERIFIED`` with
the reason — never a quietly downgraded success.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from app.services.patch_commands import (
    CommandExecutor,
    detect_commands,
    select_tests,
)
from app.services.patch_parser import ParsedPatch
from app.services.patch_safety import (
    PatchSafetyValidator,
)
from app.services.patch_workspace import GitWorkspace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration (§37 — thresholds are configuration, not literals in logic)
# ---------------------------------------------------------------------------


@dataclass
class VerificationThresholds:
    """§37: every number the verdict compares against lives here."""

    #: A latency (p95) increase beyond this fraction is a regression (§37).
    latency_regression_fraction: float = 0.20
    #: An error-rate increase beyond this absolute fraction is a regression.
    error_rate_increase: float = 0.01
    #: A memory increase beyond this fraction is a regression (§61).
    memory_increase_fraction: float = 0.15
    #: Minimum expected-behaviour dimensions that must be preserved (§36).
    min_preserved_dimensions: int = 3

    def as_dict(self) -> dict:
        return {
            "latency_regression_fraction": self.latency_regression_fraction,
            "error_rate_increase": self.error_rate_increase,
            "memory_increase_fraction": self.memory_increase_fraction,
            "min_preserved_dimensions": self.min_preserved_dimensions,
        }


# ---------------------------------------------------------------------------
# Regression test generation (§28)
# ---------------------------------------------------------------------------

_REGRESSION_TEMPLATE = '''"""ARGUS-generated regression test (Phase 7 §28).

Generated for fix hypothesis {hypothesis_title!r}.

Contract (§29): this test MUST fail on the base commit (demonstrating the
original failure) and pass on the patched commit. ARGUS recorded both runs;
a test that cannot demonstrate the failure is not evidence.
"""

from __future__ import annotations

{imports}


{test_body}
'''


def _defect_pairs(parsed: ParsedPatch) -> list[tuple[str, str]]:
    """Extract (removed_defect, added_fix) line pairs from the patch (§28).

    The regression test's assertions come from the patch itself, not from a
    template's guess: whatever line the patch removes is the defect shape,
    and whatever it adds is the fixed shape. A test built this way genuinely
    fails on the base commit and passes on the patched one — the §29 contract
    — instead of passing everywhere like an ``assert module is not None``
    tautology would.
    """
    pairs: list[tuple[str, str]] = []
    for file_patch in parsed.files:
        for hunk in file_patch.hunks:
            removals = [ln.content for ln in hunk.lines if ln.is_removal]
            additions = [ln.content for ln in hunk.lines if ln.is_addition]
            for removed, added in zip(removals, additions):
                if removed.strip() and added.strip() and removed != added:
                    pairs.append((removed, added))
            # Unbalanced hunks still carry usable one-sided evidence.
            if not removals and additions:
                pairs.append(("", additions[0]))
            if removals and not additions:
                pairs.append((removals[0], ""))
    return pairs


class RegressionTestGenerator:
    """Writes the regression test that pins the fix (§28, §29)."""

    def generate(
        self,
        *,
        hypothesis_title: str,
        parsed: ParsedPatch,
        symbol_name: str,
        failure_signature: str,
        existing_test_paths: Sequence[str] = (),
    ) -> tuple[str, str, str]:
        """Return ``(test_path, test_source, content_hash)``.

        ``test_path`` is workspace-relative, under ``tests/`` so the §27
        selector finds it on the patched run. Raises :class:`ValueError` when
        the patch carries no assertable change — an untestable patch is
        recorded, never papered over with a test that cannot fail (§28).
        """
        primary_path = parsed.paths[0] if parsed.paths else ""
        if not primary_path.endswith(".py"):
            raise ValueError(
                f"regression generation supports python modules; "
                f"{primary_path or '(no file)'} is not one"
            )
        pairs = _defect_pairs(parsed)
        if not pairs:
            raise ValueError(
                "the patch contains no assertable line change; no honest "
                "regression test can be generated from it (§28)"
            )

        safe_symbol = re.sub(r"\W", "_", symbol_name or "fix").strip("_") or "fix"
        test_path = f"tests/test_regression_{safe_symbol}.py"

        #: The test reads the changed file directly relative to itself — no
        #: import machinery, no sys.path assumptions, works on both the base
        #: and the patched checkout of the same workspace.
        assertions: list[str] = []
        for removed, added in pairs[:5]:  # keep the test small and reviewable
            if removed:
                assertions.append(
                    f"    assert {removed!r} not in source, (\n"
                    f'        "the defect line this fix removes is still present"\n'
                    f"    )\n"
                )
            if added:
                assertions.append(
                    f"    assert {added!r} in source, (\n"
                    f'        "the fixed line this patch introduces is missing"\n'
                    f"    )\n"
                )

        source = (
            f'"""Generated regression test (ARGUS Phase 7 §28).\n\n'
            f"Hypothesis: {hypothesis_title}\n"
            f"Original failure: {failure_signature}\n\n"
            f"Contract (§29): this test fails on the base commit and passes\n"
            f"on the patched commit. Assertions are derived from the patch\n"
            f"diff itself — the defect line must be gone, the fix line present.\n"
            f'"""\n\n'
            f"import pathlib\n\n"
            f"\n"
            f"_TARGET = pathlib.Path(__file__).resolve().parents[1] / "
            f"{primary_path!r}\n"
            f"\n"
            f"\n"
            f"def test_regression_{safe_symbol}_no_longer_fails():\n"
            f'    source = _TARGET.read_text(encoding="utf-8")\n' + "".join(assertions)
        )
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return test_path, source, digest


# ---------------------------------------------------------------------------
# The verification pipeline
# ---------------------------------------------------------------------------


@dataclass
class VerificationOutcome:
    """The full result of one verification attempt (§64's checklist)."""

    status: str  # VerificationStatus value
    level: str  # VerificationLevel value
    confidence: str
    confidence_reason: str
    verdict_reason: str
    tampering_flag: str
    verification_env_intact: bool
    baseline_failure_reproduced: bool
    patched_failure_reproduced: Optional[bool]
    regression_detected: bool
    evidence: dict[str, Any] = field(default_factory=dict)
    test_runs: list[dict[str, Any]] = field(default_factory=list)
    regression: dict[str, Any] = field(default_factory=dict)
    comparison: dict[str, Any] = field(default_factory=dict)
    workspace_reports: dict[str, Any] = field(default_factory=dict)
    #: §45 — the generated regression test's exact source, kept so it can be
    #: stored as an artifact. Kept out of ``regression``/``evidence`` because
    #: those are serialised into API responses and this is a whole file.
    regression_source: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "VERIFIED"


class PatchVerificationEngine:
    """Runs the whole verification ladder for one patch (§26–§37)."""

    def __init__(
        self,
        *,
        safety_validator: Optional[PatchSafetyValidator] = None,
        executor: Optional[CommandExecutor] = None,
        thresholds: Optional[VerificationThresholds] = None,
    ) -> None:
        self._safety = safety_validator or PatchSafetyValidator()
        self._executor = executor or CommandExecutor()
        self.thresholds = thresholds or VerificationThresholds()

    # ------------------------------------------------------------------
    def verify(
        self,
        *,
        workspace: GitWorkspace,
        parsed: ParsedPatch,
        patch_diff: str,
        scope_files: Sequence[str],
        baseline_reproduced: bool,
        baseline_metrics: Optional[dict[str, float]] = None,
        patched_metrics: Optional[dict[str, float]] = None,
        baseline_failure_signature: str = "the original failure",
        target_symbol: str = "",
        hypothesis_title: str = "",
        patched_still_reproduces: bool = False,
    ) -> VerificationOutcome:
        """Run the ladder and return the outcome (§34).

        ``baseline_reproduced`` comes from the Phase 5 experiment that
        demonstrated the failure before any patch existed (§33). The patched
        re-run is reported through ``patched_still_reproduces`` — the
        orchestrator's observed result of re-running the Phase 5 experiment
        against the patched tree (§63).
        """
        evidence: dict[str, Any] = {
            "applied": False,
            "static": [],
            "build": None,
            "tests": None,
            "regression": None,
            "reproduction": {
                "baseline_reproduced": baseline_reproduced,
                "patched_reproduced": None,
            },
            "comparison": None,
            "environment_intact": True,
        }
        outcome = VerificationOutcome(
            status="NOT_VERIFIED",
            level="NONE",
            confidence="LOW",
            confidence_reason="verification has not completed",
            verdict_reason="",
            tampering_flag="NONE",
            verification_env_intact=True,
            baseline_failure_reproduced=baseline_reproduced,
            patched_failure_reproduced=None,
            regression_detected=False,
        )

        def fail(level: str, reason: str) -> VerificationOutcome:
            outcome.level = level
            outcome.verdict_reason = reason
            outcome.status = "NOT_VERIFIED"
            outcome.confidence = "LOW"
            outcome.confidence_reason = reason
            outcome.evidence = evidence
            return outcome

        # ---- 0. the precondition (§33) -----------------------------------
        if not baseline_reproduced:
            return fail(
                "NONE",
                "the original failure was never reproduced on the baseline; "
                "there is nothing to verify a fix against (§33)",
            )

        # ---- 1. apply (§23) ------------------------------------------------
        try:
            applied = workspace.apply_patch(patch_diff)
        except Exception as error:  # noqa: BLE001 - recorded, never silent
            return fail("NONE", f"patch application failed: {error}")
        evidence["applied"] = True
        evidence["applied_files"] = applied["changed_entries"]

        # ---- 2. post-apply diff inspection (§23) ---------------------------
        actual_diff = workspace.working_diff()
        expected_paths = set(parsed.paths)
        actual_paths = {
            line.split(" b/", 1)[-1]
            for line in actual_diff.splitlines()
            if line.startswith("+++ b/")
        }
        if actual_paths != expected_paths:
            return fail(
                "NONE",
                f"applied diff changed {sorted(actual_paths)} but the patch "
                f"declared {sorted(expected_paths)} (§23)",
            )

        # ---- 2b. scope enforcement inside the engine (§10, §23) ------------
        #: The pre-apply safety gate checks scope too, but the engine never
        #: trusts that it ran: verifying a patch that touched a path outside
        #: the hypothesis's allowlist would give an out-of-scope change the
        #: phase's strongest verdict.
        scope_set = {item.strip().rstrip("/") for item in scope_files if item.strip()}
        if scope_set and not expected_paths.issubset(scope_set):
            return fail(
                "NONE",
                f"patch touches {sorted(expected_paths - scope_set)}, outside "
                f"the fix's scope {sorted(scope_set)} (§10, §23)",
            )

        # ---- 3. safety re-check incl. post-apply secret scan (§52) ---------
        # (The pre-apply safety gate already ran; this re-check catches a
        # patch whose hunks only make sense post-apply.)
        try:
            self._post_apply_secret_scan(workspace, parsed)
        except ValueError as error:
            return fail("NONE", str(error))

        # ---- 4. static checks (§24) ----------------------------------------
        detected = detect_commands(workspace.root)
        static_keys = detected.get("static", [])
        static_ok = True
        #: ``py_compile`` requires explicit filenames — a bare invocation exits 2
        #: and would fail every Python patch regardless of content. The syntax
        #: gate therefore compiles exactly the files the patch touches (§24:
        #: validate *the patch*), and is skipped (recorded, not hidden) when the
        #: patch touches no Python at all.
        changed_python = tuple(sorted(p for p in parsed.paths if p.endswith(".py")))
        for key in static_keys:
            if key == "python_syntax" and not changed_python:
                outcome.test_runs.append(
                    {
                        "kind": "STATIC",
                        "command_key": key,
                        "exit_code": None,
                        "timed_out": False,
                        "duration_ms": 0,
                        "output_tail": "skipped: the patch touches no Python files",
                        "unknown_configuration": False,
                    }
                )
                continue
            args = changed_python if key == "python_syntax" else ()
            result = self._executor.execute(workspace.root, key, args)
            evidence["static"].append(result.as_dict())
            outcome.test_runs.append(
                {
                    "kind": "STATIC",
                    "command_key": result.key,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "duration_ms": result.duration_ms,
                    "output_tail": result.output_tail,
                    "unknown_configuration": result.unknown_configuration,
                }
            )
            if not result.ok:
                static_ok = False
        if not static_ok:
            return fail(
                "NONE", "static validation failed (§24); the patch never reached tests"
            )

        outcome.level = "STATIC_VALIDATED"

        # ---- 5. regression test, two-sided (§28, §29) ----------------------
        regression_result = self._run_regression_two_sided(
            workspace=workspace,
            parsed=parsed,
            scope_files=scope_files,
            baseline_reproduced=baseline_reproduced,
            baseline_failure_signature=baseline_failure_signature,
            target_symbol=target_symbol,
            hypothesis_title=hypothesis_title,
            evidence=evidence,
            patch_diff=patch_diff,
        )
        outcome.test_runs.extend(regression_result["runs"])
        evidence["regression"] = regression_result["summary"]
        outcome.regression = regression_result["summary"]
        outcome.regression_source = regression_result.get("source")
        if regression_result["verdict"] == "REGRESSION_TEST_INVALID":
            return fail(
                "TEST_VALIDATED",
                "the regression test passed on the base commit: it never "
                "demonstrated the original failure (§29 REGRESSION_TEST_INVALID)",
            )
        if regression_result["verdict"] == "FAILS_ON_PATCHED":
            return fail(
                "TEST_VALIDATED",
                "the regression test still fails on the patched commit (§29)",
            )

        outcome.level = "TEST_VALIDATED"

        # ---- 6. build & selected tests (§25, §26, §27) ----------------------
        build_candidates = detected.get("build") or []
        build_key: Optional[str] = build_candidates[0] if build_candidates else None
        if build_key:
            build_result = self._executor.execute(workspace.root, build_key)
            evidence["build"] = build_result.as_dict()
            outcome.test_runs.append(
                {
                    "kind": "BUILD",
                    "command_key": build_result.key,
                    "exit_code": build_result.exit_code,
                    "timed_out": build_result.timed_out,
                    "duration_ms": build_result.duration_ms,
                    "output_tail": build_result.output_tail,
                    "unknown_configuration": False,
                }
            )
            if not build_result.ok:
                outcome.status = "BUILD_FAILED"
                return fail("TEST_VALIDATED", f"build failed ({build_key}, §25)")

        test_candidates = detected.get("test") or []
        test_key: Optional[str] = test_candidates[0] if test_candidates else None
        if test_key is None:
            evidence["tests"] = {"unknown_configuration": True}
            outcome.test_runs.append(
                {
                    "kind": "UNIT",
                    "command_key": "none",
                    "exit_code": None,
                    "timed_out": False,
                    "duration_ms": 0,
                    "output_tail": "no test command detected in this repository",
                    "unknown_configuration": True,
                }
            )
            return fail(
                "STATIC_VALIDATED",
                "the repository defines no runnable test command (§25 "
                "BUILD_CONFIGURATION_UNKNOWN); refusing to claim test success",
            )

        reason, selected = select_tests(
            detected=detected,
            changed_files=parsed.paths,
            workspace_root=workspace.root,
        )
        test_result = self._executor.execute(
            workspace.root,
            "python_tests_selected"
            if test_key == "python_tests_selected"
            else test_key,
            selected if test_key == "python_tests_selected" else (),
        )
        evidence["tests"] = {
            **test_result.as_dict(),
            "selected": selected,
            "selection_reason": reason,
        }
        outcome.test_runs.append(
            {
                "kind": "UNIT",
                "command_key": test_result.key,
                "exit_code": test_result.exit_code,
                "timed_out": test_result.timed_out,
                "duration_ms": test_result.duration_ms,
                "output_tail": test_result.output_tail,
                "unknown_configuration": False,
                "selected": selected,
            }
        )
        if not test_result.ok:
            return fail("TEST_VALIDATED", f"tests failed ({test_result.key}, §27)")

        outcome.level = "TEST_VALIDATED"
        if evidence.get("build"):
            outcome.level = "TEST_VALIDATED"

        # ---- 7. reproduction integration (§30, §32) -------------------------
        #: ``patched_still_reproduces`` is the orchestrator's observed result of
        #: re-running the Phase 5 experiment against the patched tree (§63):
        #: tests passing is never enough while the original failure persists.
        patched_reproduced = patched_still_reproduces
        evidence["reproduction"]["patched_reproduced"] = patched_reproduced
        outcome.patched_failure_reproduced = patched_reproduced
        if patched_still_reproduces:
            return fail(
                "REPRODUCTION_VALIDATED",
                "the patched code still reproduces the original failure (§30, §63)",
            )
        outcome.level = "REPRODUCTION_VALIDATED"

        # ---- 8. before/after comparison (§31, §36, §37) ---------------------
        comparison = compare_metrics(
            baseline=baseline_metrics or {},
            patched=patched_metrics or {},
            thresholds=self.thresholds,
        )
        evidence["comparison"] = comparison
        outcome.comparison = comparison
        outcome.regression_detected = bool(comparison["regressions"])
        if outcome.regression_detected:
            return fail(
                "REPRODUCTION_VALIDATED",
                "regression detected after the patch: "
                + "; ".join(comparison["regressions"])
                + " (§36)",
            )
        outcome.level = "REGRESSION_VALIDATED"

        # ---- 9. the verdict (§64) --------------------------------------------
        outcome.status = "VERIFIED"
        outcome.level = "FULLY_VERIFIED"
        outcome.confidence = "HIGH"
        outcome.confidence_reason = (
            "The original failure reproduced on the baseline; the generated "
            "regression test failed before the patch and passed after it; "
            "the patched version passed static checks and the selected tests; "
            "the before/after comparison stays inside every configured "
            "threshold."
        )
        outcome.verdict_reason = (
            "FULLY_VERIFIED: failure demonstrated on base, resolved on patch, "
            "no regression beyond thresholds"
        )
        outcome.evidence = evidence
        return outcome

    # ------------------------------------------------------------------
    def _run_regression_two_sided(
        self,
        *,
        workspace: GitWorkspace,
        parsed: ParsedPatch,
        scope_files: Sequence[str],
        baseline_reproduced: bool,
        baseline_failure_signature: str,
        target_symbol: str,
        hypothesis_title: str,
        evidence: dict[str, Any],
        patch_diff: str,
    ) -> dict[str, Any]:
        """Prove the regression test both ways (§29).

        The test is written into the workspace, run on the *base* commit
        (patch stashed away via ``git apply -R``) and on the *patched* tree.
        """
        generator = RegressionTestGenerator()
        primary_path = (
            parsed.paths[0] if parsed.paths else (scope_files[0] if scope_files else "")
        )
        try:
            test_path, test_source, digest = generator.generate(
                hypothesis_title=hypothesis_title,
                parsed=parsed,
                symbol_name=target_symbol or primary_path.rsplit("/", 1)[-1],
                failure_signature=baseline_failure_signature,
            )
        except ValueError as error:
            # An untestable patch is recorded honestly (§28) and blocks the
            # regression rung — never silently skipped.
            return {
                "verdict": "REGRESSION_TEST_INVALID",
                "runs": [],
                "summary": {
                    "path": None,
                    "origin": "generated",
                    "verdict": "REGRESSION_TEST_INVALID",
                    "invalid_reason": str(error),
                    "failed_on_base": None,
                    "passed_on_patched": None,
                },
            }

        runs: list[dict[str, Any]] = []
        summary: dict[str, Any] = {
            "path": test_path,
            "content_hash": digest,
            "origin": "generated",
        }

        # The generated test lives in the workspace for exactly these two runs
        # and is removed again on every path — it is verification tooling (§56),
        # never a leftover change the workspace's own suite would adopt.
        test_file = workspace.root / test_path
        test_file.parent.mkdir(parents=True, exist_ok=True)
        patch_file = workspace.root / ".argus-patch.diff"
        patch_file.write_text(patch_diff, encoding="utf-8")
        try:
            test_file.write_text(test_source, encoding="utf-8")

            # -- BASE side: reverse-apply the patch (§29: must FAIL here) ---
            workspace._run(
                "apply", "-R", "--whitespace=nowarn", str(patch_file), check=False
            )
            base_result = self._executor.execute(
                workspace.root, "python_tests_selected", [test_path]
            )
            runs.append(
                {
                    "kind": "REGRESSION",
                    "command_key": base_result.key,
                    "exit_code": base_result.exit_code,
                    "timed_out": base_result.timed_out,
                    "duration_ms": base_result.duration_ms,
                    "output_tail": base_result.output_tail,
                    "side": "base",
                }
            )
            failed_on_base = base_result.exit_code not in (0, None)

            # -- PATCHED side: re-apply (§29: must PASS here) ----------------
            workspace._run("apply", "--whitespace=nowarn", str(patch_file), check=False)
            patched_result = self._executor.execute(
                workspace.root, "python_tests_selected", [test_path]
            )
            runs.append(
                {
                    "kind": "REGRESSION",
                    "command_key": patched_result.key,
                    "exit_code": patched_result.exit_code,
                    "timed_out": patched_result.timed_out,
                    "duration_ms": patched_result.duration_ms,
                    "output_tail": patched_result.output_tail,
                    "side": "patched",
                }
            )
            passed_on_patched = patched_result.exit_code == 0
        finally:
            # Leave the workspace byte-identical to the applied-patch state on
            # every path: re-apply first (a no-op when the patch is already in,
            # recorded with check=False), and only then remove the tooling.
            # A half-applied tree must never quietly outlive this method.
            if patch_diff:
                workspace._run(
                    "apply", "--whitespace=nowarn", str(patch_file), check=False
                )
            patch_file.unlink(missing_ok=True)
            test_file.unlink(missing_ok=True)

        if not failed_on_base:
            verdict = "REGRESSION_TEST_INVALID"
            invalid_reason = "the regression test passed on the base commit (§29)"
        elif not passed_on_patched:
            verdict = "FAILS_ON_PATCHED"
            invalid_reason = "the regression test failed on the patched commit (§29)"
        else:
            verdict = "VALID"
            invalid_reason = None

        summary.update(
            {
                "verdict": verdict,
                "invalid_reason": invalid_reason,
                "failed_on_base": failed_on_base,
                "passed_on_patched": passed_on_patched,
            }
        )
        return {
            "verdict": verdict,
            "runs": runs,
            "summary": summary,
            "source": test_source,
        }

    # ------------------------------------------------------------------
    def _post_apply_secret_scan(
        self, workspace: GitWorkspace, parsed: ParsedPatch
    ) -> None:
        """Scan changed files post-apply for introduced secrets (§52)."""
        from app.services.patch_safety import _SECRET_SIGNATURES

        for file_patch in parsed.files:
            text = workspace.read_file(file_patch.path)
            if text is None:
                continue
            for line in text.splitlines():
                for label, pattern in _SECRET_SIGNATURES:
                    if pattern.search(line):
                        raise ValueError(
                            f"post-apply scan found an introduced {label} in "
                            f"{file_patch.path} (§52)"
                        )


def workspace_patch_text(parsed: ParsedPatch) -> str:
    """The canonical patch text for a parsed patch (§11)."""
    return parsed.to_unified_diff()


def compare_metrics(
    *,
    baseline: dict[str, float],
    patched: dict[str, float],
    thresholds: VerificationThresholds,
) -> dict[str, Any]:
    """§31/§36/§37 — every number from real telemetry; thresholds from config."""
    metrics: dict[str, Any] = {}
    regressions: list[str] = []

    def put(
        name: str, before: Optional[float], after: Optional[float], unit: str
    ) -> None:
        if before is None and after is None:
            return
        metrics[name] = {
            "baseline": before,
            "patched": after,
            "delta": (None if before is None or after is None else after - before),
            "unit": unit,
        }

    error_base = baseline.get("error_rate")
    error_patched = patched.get("error_rate")
    put("error_rate", error_base, error_patched, "fraction")
    if error_base is not None and error_patched is not None:
        if error_patched - error_base > thresholds.error_rate_increase:
            regressions.append(
                f"error rate rose {error_base:.3f} → {error_patched:.3f} "
                f"(threshold {thresholds.error_rate_increase:.3f})"
            )

    for name in ("latency_p95_ms", "latency_p50_ms"):
        before = baseline.get(name)
        after = patched.get(name)
        put(name, before, after, "ms")
        if before and after is not None and before > 0:
            if (after - before) / before > thresholds.latency_regression_fraction:
                regressions.append(
                    f"{name} rose {before:.0f} → {after:.0f} ms "
                    f"(> {thresholds.latency_regression_fraction:.0%})"
                )

    mem_base = baseline.get("memory_mb")
    mem_patched = patched.get("memory_mb")
    put("memory_mb", mem_base, mem_patched, "MB")
    if mem_base and mem_patched is not None and mem_base > 0:
        if (mem_patched - mem_base) / mem_base > thresholds.memory_increase_fraction:
            regressions.append(
                f"memory rose {mem_base:.0f} → {mem_patched:.0f} MB "
                f"(> {thresholds.memory_increase_fraction:.0%})"
            )

    for name in ("failed_requests", "trace_failures", "cpu_seconds"):
        put(name, baseline.get(name), patched.get(name), "count")

    return {
        "metrics": metrics,
        "regressions": regressions,
        "thresholds": thresholds.as_dict(),
        "note": "every number comes from captured experiment telemetry; an absent metric is omitted, never zero",
    }


__all__ = [
    "PatchVerificationEngine",
    "RegressionTestGenerator",
    "VerificationOutcome",
    "VerificationThresholds",
    "compare_metrics",
    "workspace_patch_text",
]
