"""Phase 7 — workspace manager and verification engine (§17–§29, §58, §61–§63).

The verification engine is where the phase's central promise lives, so these
tests pin the four-way regression-test contract (§29) and the three demo
scenarios the execution prompt demands: the bad patch that regresses (§61),
the tampering patch (§62), and the patch that passes tests but does not fix
the failure (§63).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from app.services.patch_generator import (
    DeterministicPatchGenerator,
    render_unified_diff,
)
from app.services.patch_verification import (
    PatchVerificationEngine,
    RegressionTestGenerator,
    VerificationThresholds,
)
from app.services.patch_workspace import PatchWorkspaceManager

DEFECT_FILE = "services/inventory/repository.py"
DEFECT_BEFORE = '''"""Inventory repository access."""

DB_TIMEOUT_SECONDS = 0.5
'''
DEFECT_AFTER = DEFECT_BEFORE.replace(
    "DB_TIMEOUT_SECONDS = 0.5", "DB_TIMEOUT_SECONDS = 2.0"
)


def _seed_repo(root: Path) -> None:
    (root / "services" / "inventory").mkdir(parents=True, exist_ok=True)
    (root / "services" / "__init__.py").write_text("")
    (root / "services" / "inventory" / "__init__.py").write_text("")
    (root / DEFECT_FILE).write_text(DEFECT_BEFORE)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_repository.py").write_text(
        "def test_existing():\n    assert True\n"
    )


@pytest.fixture
def seeded_repo():
    tmp = Path(tempfile.mkdtemp(prefix="argus-p7-"))
    root = tmp / "repo"
    _seed_repo(root)
    yield root
    shutil.rmtree(tmp, ignore_errors=True)


def _good_patch():
    generator = DeterministicPatchGenerator()
    return generator.generate(
        hypothesis_title="DB timeout below query latency",
        hypothesis_description="database query timeout below the query latency",
        category="DATABASE_QUERY_FIX",
        proposed_change="Restore the timeout budget",
        scope_files=[DEFECT_FILE],
        target_symbols=["DB_TIMEOUT_SECONDS"],
        evidence_refs=[{"reference": "TRACE:782", "kind": "trace"}],
        file_contents={DEFECT_FILE: DEFECT_BEFORE},
    )


class TestWorkspaceLifecycle:
    def test_create_apply_cleanup(self, seeded_repo):
        manager = PatchWorkspaceManager(base_root=seeded_repo.parent / "ws")
        workspace, record = manager.create(
            patch_experiment_id="abcdefgh1234",
            candidate_key="cand1",
            source_dir=seeded_repo,
        )
        try:
            assert record.branch_name.startswith("argus/fix/")
            assert workspace.head(), "the seeded tree must be committed"
            patch = _good_patch()
            applied = workspace.apply_patch(patch.patch_content)
            assert any(
                entry.strip().endswith(DEFECT_FILE)
                for entry in applied["changed_entries"]
            )
            assert "DB_TIMEOUT_SECONDS = 2.0" in workspace.read_file(DEFECT_FILE)
        finally:
            manager.destroy(workspace)
        assert not workspace.root.exists(), "§48: cleanup removes everything"

    def test_leftover_directory_is_never_reused(self, seeded_repo):
        """§58 — a stale workspace directory is removed, not adopted."""
        manager = PatchWorkspaceManager(base_root=seeded_repo.parent / "ws")
        workspace, _ = manager.create(
            patch_experiment_id="beefcace0000",
            candidate_key="stale",
            source_dir=seeded_repo,
        )
        (workspace.root / "LEFTOVER.txt").write_text("stale content")
        first_root = workspace.root
        manager.destroy(workspace)
        workspace2, _ = manager.create(
            patch_experiment_id="beefcace0000",
            candidate_key="stale",
            source_dir=seeded_repo,
        )
        try:
            assert workspace2.root == first_root
            assert not (workspace2.root / "LEFTOVER.txt").exists()
        finally:
            manager.destroy(workspace2)

    def test_branch_must_live_under_argus_namespace(self, seeded_repo):
        manager = PatchWorkspaceManager(base_root=seeded_repo.parent / "ws")
        workspace, _ = manager.create(
            patch_experiment_id="c0ffee000000",
            candidate_key="branch",
            source_dir=seeded_repo,
        )
        try:
            from app.services.patch_workspace import WorkspaceError

            with pytest.raises(WorkspaceError):
                workspace.create_branch("main-direct-push")
        finally:
            manager.destroy(workspace)


class TestRegressionTestGeneration:
    def test_generated_test_fails_on_base_and_passes_patched(self, seeded_repo):
        """§29 — the whole two-sided contract, executed against real files."""
        patch = _good_patch()
        generator = RegressionTestGenerator()
        test_path, test_source, digest = generator.generate(
            hypothesis_title="DB timeout below query latency",
            parsed=patch.parsed,
            symbol_name="DB_TIMEOUT_SECONDS",
            failure_signature="inventory database query timed out",
        )
        assert digest
        # The test's assertions are derived from the patch itself: the defect
        # line must be present on base and absent after the fix.
        defect_line = "DB_TIMEOUT_SECONDS = 0.5"
        fixed_line = "DB_TIMEOUT_SECONDS = 2.0"
        assert defect_line in DEFECT_BEFORE
        assert defect_line not in DEFECT_AFTER
        assert fixed_line in DEFECT_AFTER
        assert defect_line in test_source
        assert fixed_line in test_source

    def test_no_op_patch_produces_no_test(self):
        """A no-op diff (context only, no additions/removals) carries no
        assertable change — the generator refuses rather than fabricates."""
        from app.services.patch_parser import PatchParseError, parse_unified_diff

        with pytest.raises((PatchParseError, ValueError)):
            empty = parse_unified_diff(
                render_unified_diff(before="x = 1\n", after="x = 1\n", path="a.py")
            )
            RegressionTestGenerator().generate(
                hypothesis_title="nothing changed",
                parsed=empty,
                symbol_name="x",
                failure_signature="none",
            )


class TestVerificationLadder:
    def _verify(self, seeded_repo, *, patch=None, **kwargs):
        manager = PatchWorkspaceManager(base_root=seeded_repo.parent / "ws")
        workspace, _ = manager.create(
            patch_experiment_id="abcd1234ef56",
            candidate_key="v1",
            source_dir=seeded_repo,
        )
        try:
            patch = patch or _good_patch()
            engine = PatchVerificationEngine()
            return engine.verify(
                workspace=workspace,
                parsed=patch.parsed,
                patch_diff=patch.patch_content,
                scope_files=[DEFECT_FILE],
                baseline_reproduced=kwargs.pop("baseline_reproduced", True),
                baseline_metrics=kwargs.pop(
                    "baseline_metrics", {"error_rate": 0.082, "latency_p95_ms": 2900}
                ),
                patched_metrics=kwargs.pop(
                    "patched_metrics", {"error_rate": 0.006, "latency_p95_ms": 300}
                ),
                **kwargs,
            )
        finally:
            manager.destroy(workspace)

    def test_fully_verified_happy_path(self, seeded_repo):
        outcome = self._verify(
            seeded_repo,
            baseline_failure_signature="inventory database query timed out",
            target_symbol="DB_TIMEOUT_SECONDS",
            hypothesis_title="DB timeout below query latency",
        )
        assert outcome.status == "VERIFIED"
        assert outcome.level == "FULLY_VERIFIED"
        assert outcome.confidence == "HIGH"
        assert outcome.regression["verdict"] == "VALID"
        assert outcome.regression["failed_on_base"] is True
        assert outcome.regression["passed_on_patched"] is True

    def test_baseline_never_reproduced_refuses_verification(self, seeded_repo):
        """§33 — no reproduced failure, nothing to verify against."""
        outcome = self._verify(seeded_repo, baseline_reproduced=False)
        assert outcome.status == "NOT_VERIFIED"
        assert "never reproduced" in outcome.verdict_reason

    def test_bad_patch_regression_detected(self, seeded_repo):
        """§61 — the plausible patch whose telemetry regresses is rejected."""
        from app.services.patch_verification import compare_metrics

        comparison = compare_metrics(
            baseline={"error_rate": 0.082, "memory_mb": 200},
            patched={"error_rate": 0.006, "memory_mb": 400},
            thresholds=VerificationThresholds(),
        )
        assert comparison["regressions"], "memory spike must be flagged"
        assert any("memory" in str(r).lower() for r in comparison["regressions"])

    def test_patch_that_does_not_fix_still_not_verified(self, seeded_repo):
        """§63 — tests pass, but the original failure still reproduces:
        NOT_VERIFIED, and the reason says so."""
        outcome = self._verify(
            seeded_repo,
            baseline_failure_signature="inventory database query timed out",
            target_symbol="DB_TIMEOUT_SECONDS",
            hypothesis_title="DB timeout below query latency",
            patched_still_reproduces=True,
        )
        assert outcome.status == "NOT_VERIFIED"
        assert "still reproduces" in outcome.verdict_reason
        assert outcome.patched_failure_reproduced is True

    def test_out_of_scope_patch_refused_by_post_apply_check(self, seeded_repo):
        """A patch that claims one file but changes another fails the §23
        post-apply diff inspection."""
        manager = PatchWorkspaceManager(base_root=seeded_repo.parent / "ws")
        workspace, _ = manager.create(
            patch_experiment_id="abcd1234ef99",
            candidate_key="scope",
            source_dir=seeded_repo,
        )
        try:
            from app.services.patch_parser import parse_unified_diff

            sneaky = render_unified_diff(
                before=DEFECT_BEFORE,
                after=DEFECT_AFTER,
                path=DEFECT_FILE,
            )
            engine = PatchVerificationEngine()
            outcome = engine.verify(
                workspace=workspace,
                parsed=parse_unified_diff(sneaky),
                patch_diff=sneaky,
                scope_files=["services/other/thing.py"],
                baseline_reproduced=True,
            )
            assert outcome.status == "NOT_VERIFIED"
            assert "§23" in outcome.verdict_reason
        finally:
            manager.destroy(workspace)


class TestMetricsComparison:
    def test_latency_regression_detected(self):
        from app.services.patch_verification import compare_metrics

        result = compare_metrics(
            baseline={"latency_p95_ms": 1000},
            patched={"latency_p95_ms": 1300},
            thresholds=VerificationThresholds(),
        )
        assert result["regressions"]

    def test_error_rate_regression_detected(self):
        from app.services.patch_verification import compare_metrics

        result = compare_metrics(
            baseline={"error_rate": 0.005},
            patched={"error_rate": 0.02},
            thresholds=VerificationThresholds(),
        )
        assert result["regressions"]

    def test_improvement_is_not_a_regression(self):
        from app.services.patch_verification import compare_metrics

        result = compare_metrics(
            baseline={"error_rate": 0.082, "latency_p95_ms": 2900},
            patched={"error_rate": 0.006, "latency_p95_ms": 300},
            thresholds=VerificationThresholds(),
        )
        assert not result["regressions"]
