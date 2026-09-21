"""Phase 7 — patch safety validator (§13–§16, §52, §55).

Every rejection class the phase names is pinned here: scope, sensitive files,
dependencies, configuration, migrations, CI/verification tampering, and
introduced secrets. A finding that would surprise the §55 mandate is a test
bug — the validator must be *stricter* than these tests, never laxer.
"""

from __future__ import annotations

import pytest

from app.services.patch_generator import render_unified_diff
from app.services.patch_parser import parse_unified_diff
from app.services.patch_safety import PatchSafetyValidator, is_test_file

REPO_PY = "services/inventory/repository.py"


def _diff(before: str, after: str, path: str = REPO_PY) -> str:
    return render_unified_diff(before=before, after=after, path=path)


def _validated(diff: str, **kwargs):
    parsed = parse_unified_diff(diff)
    kwargs.setdefault("scope_files", [REPO_PY])
    return parsed, PatchSafetyValidator().validate(parsed, **kwargs)


class TestScope:
    def test_in_scope_passes(self):
        diff = _diff("DB_TIMEOUT_SECONDS = 0.5\n", "DB_TIMEOUT_SECONDS = 2.0\n")
        parsed, report = _validated(diff)
        assert report.ok, [f.message for f in report.findings]

    def test_out_of_scope_is_hard_refusal(self):
        diff = _diff("x = 1\n", "x = 2\n", path="billing/charger.py")
        parsed, report = _validated(diff)
        assert not report.ok
        assert any("scope" in f.message.lower() for f in report.hard_findings)

    def test_traversal_in_patch_rejected_by_parser(self):
        """The parser refuses traversal before the validator sees it (§12)."""
        from app.services.patch_parser import PatchParseError

        with pytest.raises(PatchParseError):
            parse_unified_diff(
                "--- a/../billing/x.py\n+++ b/../billing/x.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
            )


class TestSensitiveFiles:
    """§14 — DO NOT MODIFY by default, approval only when explicit."""

    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/ci.yml",
            "infrastructure/main.tf",
            "docker-compose.yml",
            "alembic/versions/abc_phase7.py",
            "auth/passwords.py",
        ],
    )
    def test_sensitive_paths_refused_without_approval(self, path):
        diff = _diff("a = 1\n", "a = 2\n", path=path)
        parsed, report = _validated(diff, scope_files=[path])
        assert not report.ok
        assert report.hard_findings, path

    def test_sensitive_path_with_explicit_approval_is_recorded(self):
        """An approved sensitive area is recorded as a risk-elevating warning —
        but CI remains a §56 hard refusal regardless of approval, because the
        verification environment is never AI-modifiable."""
        diff = _diff("a = 1\n", "a = 2\n", path=".github/workflows/ci.yml")
        parsed, report = _validated(
            diff,
            scope_files=[".github/workflows/ci.yml"],
            allowed_sensitive=["ci"],
        )
        assert "ci" in report.approved_sensitive
        assert any(
            f.kind == "CI_MODIFIED" and f.severity == "hard" for f in report.findings
        )

    def test_non_ci_sensitive_approval_is_not_a_refusal(self):
        """e.g. an authentication file with explicit plan approval: warning,
        not a refusal."""
        diff = _diff("TIMEOUT = 1\n", "TIMEOUT = 2\n", path="auth/guards.py")
        parsed, report = _validated(
            diff,
            scope_files=["auth/guards.py"],
            allowed_sensitive=["authentication"],
        )
        assert report.approved_sensitive == ["authentication"]
        assert report.ok, [f.message for f in report.hard_findings]


class TestDependenciesAndConfig:
    """§15, §16 — dependency and configuration changes are elevated-risk."""

    def test_dependency_change_requires_reason(self):
        diff = _diff(
            '"fastapi>=0.110"\n', '"fastapi>=0.115"\n', path="requirements.txt"
        )
        parsed, report = _validated(diff, scope_files=["requirements.txt"])
        assert not report.ok
        assert any("dependency" in f.message.lower() for f in report.hard_findings)

    def test_dependency_change_with_reason_is_recorded(self):
        diff = _diff(
            '"fastapi>=0.110"\n', '"fastapi>=0.115"\n', path="requirements.txt"
        )
        parsed, report = _validated(
            diff,
            scope_files=["requirements.txt"],
            dependency_reason="CVE-2026-1234 fix required by security policy",
        )
        assert report.ok or not report.hard_findings
        assert any(f.kind == "DEPENDENCY_CHANGE" for f in report.findings)

    def test_config_change_without_records_flagged(self):
        diff = _diff("RETRIES = 3\n", "RETRIES = 5\n", path="config/settings.py")
        parsed, report = _validated(diff, scope_files=["config/settings.py"])
        assert any(
            f.kind == "CONFIGURATION_CHANGE" or "config" in f.message.lower()
            for f in report.findings
        )


class TestTampering:
    """§55 — test tampering is mandatory to catch and always disqualifying."""

    def test_deleting_a_test_is_tampering(self):
        before = "def test_checkout_retries():\n    assert True\n"
        diff = _diff(before, "", path="tests/test_checkout.py")
        parsed, report = _validated(diff, scope_files=["tests/test_checkout.py"])
        assert report.tampering
        assert "TEST_TAMPERING" in report.tampering_kinds
        assert any("deleted" in f.message.lower() for f in report.hard_findings)

    def test_weakening_an_assertion_is_tampering(self):
        before = "def test_budget():\n    assert result == 2\n"
        after = "def test_budget():\n    assert result is not None\n"
        diff = _diff(before, after, path="tests/test_checkout.py")
        parsed, report = _validated(diff, scope_files=["tests/test_checkout.py"])
        assert report.tampering
        assert any("weakened" in f.message.lower() for f in report.hard_findings)

    def test_skipping_a_test_is_tampering(self):
        before = "def test_budget():\n    assert x == 2\n"
        after = "@pytest.mark.skip\ndef test_budget():\n    assert x == 2\n"
        diff = _diff(before, after, path="tests/test_checkout.py")
        parsed, report = _validated(diff, scope_files=["tests/test_checkout.py"])
        assert report.tampering

    def test_disabling_lint_is_tampering(self):
        """§55 — disablers are caught in *any* file, not only tests."""
        before = "line = 1\n"
        after = "line = 1  # noqa\n"
        diff = _diff(before, after, path=REPO_PY)
        parsed, report = _validated(diff)
        assert report.tampering
        assert "LINT_DISABLED" in report.tampering_kinds

    def test_disabling_typecheck_is_tampering(self):
        before = "value = compute()\n"
        after = "value = compute()  # type: ignore\n"
        diff = _diff(before, after, path=REPO_PY)
        parsed, report = _validated(diff)
        assert "TYPECHECK_DISABLED" in report.tampering_kinds

    def test_modifying_ci_without_approval_refused(self):
        diff = _diff("steps: []\n", "steps: [x]\n", path=".github/workflows/ci.yml")
        parsed, report = _validated(diff, scope_files=[".github/workflows/ci.yml"])
        assert not report.ok

    def test_legitimate_test_strengthening_passes(self):
        """The inverse direction — making an assertion *stronger* — is the
        point of a regression test and must not be flagged."""
        before = "def test_budget():\n    assert result is not None\n"
        after = "def test_budget():\n    assert result == 2\n"
        diff = _diff(before, after, path="tests/test_checkout.py")
        parsed, report = _validated(diff, scope_files=["tests/test_checkout.py"])
        assert not report.tampering


class TestSecrets:
    """§52 — introduced secrets are hard refusals."""

    def test_introduced_aws_key_refused(self):
        before = "config = {}\n"
        after = 'config = {"aws_access_key_id": "AKIAIOSFODNN7EXAMPLE"}\n'
        diff = _diff(before, after)
        parsed, report = _validated(diff)
        assert not report.ok
        assert any("secret" in f.message.lower() for f in report.hard_findings)

    def test_introduced_password_refused(self):
        before = "db = connect()\n"
        after = 'db = connect(password="hunter2-secret")\n'
        diff = _diff(before, after)
        parsed, report = _validated(diff)
        assert not report.ok


class TestHelpers:
    def test_is_test_file(self):
        assert is_test_file("tests/test_checkout.py")
        assert is_test_file("shop/test_inventory.py")
        assert not is_test_file("shop/checkout.py")
