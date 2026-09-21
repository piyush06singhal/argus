"""Phase 7 — patch parser, safety validator, and minimality measurement.

The parser is the first gate: a diff that does not parse never reaches the
safety validator, the workspace, or the AI's confidence. These tests pin the
rejection classes (§11, §12) and the roundtrip fidelity the verifier relies
on.
"""

from __future__ import annotations

import pytest

from app.services.patch_parser import PatchParseError, parse_unified_diff
from app.services.patch_generator import measure, render_unified_diff

GOOD_BEFORE = '''"""Inventory repository."""

DB_TIMEOUT_SECONDS = 0.5


class InventoryRepository:
    async def fetch_stock(self, sku: str):
        return await self._query(f"select stock from inventory where sku = '{sku}'")
'''

GOOD_AFTER = GOOD_BEFORE.replace("DB_TIMEOUT_SECONDS = 0.5", "DB_TIMEOUT_SECONDS = 2.0")


def _good_diff() -> str:
    return render_unified_diff(
        before=GOOD_BEFORE, after=GOOD_AFTER, path="services/inventory/repository.py"
    )


class TestParserAccepts:
    def test_good_patch_parses_with_fidelity(self):
        parsed = parse_unified_diff(_good_diff())
        assert parsed.file_count == 1
        assert parsed.paths == ["services/inventory/repository.py"]
        assert parsed.lines_added == 1
        assert parsed.lines_removed == 1
        assert parsed.to_unified_diff() == _good_diff().rstrip("\n") or (
            parsed.to_unified_diff().strip() == _good_diff().strip()
        )

    def test_addition_only_hunk(self):
        diff = render_unified_diff(
            before="a = 1\n",
            after="a = 1\nb = 2\n",
            path="mod.py",
        )
        parsed = parse_unified_diff(diff)
        assert parsed.lines_added == 1
        assert parsed.lines_removed == 0

    def test_deletion_only_hunk(self):
        diff = render_unified_diff(
            before="a = 1\nb = 2\n",
            after="a = 1\n",
            path="mod.py",
        )
        parsed = parse_unified_diff(diff)
        assert parsed.lines_removed == 1

    def test_counts_are_measured_not_claimed(self):
        """§9: the measurements come from the diff, not from anyone's word."""
        parsed = parse_unified_diff(_good_diff())
        m = measure(parsed, known_symbols=["DB_TIMEOUT_SECONDS"])
        assert m.files_changed == 1
        assert m.lines_added == 1
        assert m.lines_removed == 1
        assert m.symbols_modified == ["DB_TIMEOUT_SECONDS"]


class TestParserRejections:
    """§11 — malformed patches are rejected, never repaired."""

    def test_empty_patch_rejected(self):
        with pytest.raises(PatchParseError):
            parse_unified_diff("")

    def test_missing_file_header_rejected(self):
        with pytest.raises(PatchParseError):
            parse_unified_diff("@@ -1,2 +1,2 @@\n-old\n+new\n")

    def test_missing_hunk_header_rejected(self):
        with pytest.raises(PatchParseError):
            parse_unified_diff("--- a/mod.py\n+++ b/mod.py\n-old\n+new\n")

    def test_line_count_mismatch_rejected(self):
        """Hunk claims 3 source lines but provides 1 — a diff that would
        mis-apply silently if accepted."""
        diff = "--- a/mod.py\n" "+++ b/mod.py\n" "@@ -1,3 +1,3 @@\n" "-a\n" "+b\n"
        with pytest.raises(PatchParseError):
            parse_unified_diff(diff)


class TestPathSafety:
    """§12 — path traversal and absolute paths are refusals, not warnings."""

    @pytest.mark.parametrize(
        "path",
        [
            "../etc/passwd",
            "/etc/passwd",
            "a/../../etc/passwd",
            "services/../../escape.py",
        ],
    )
    def test_traversal_paths_rejected(self, path):
        diff = f"--- a/{path}\n" f"+++ b/{path}\n" "@@ -1,1 +1,1 @@\n" "-old\n" "+new\n"
        with pytest.raises(PatchParseError):
            parse_unified_diff(diff)

    def test_windows_style_path_rejected(self):
        diff = (
            "--- a/..\\windows\\evil.py\n"
            "+++ b/..\\windows\\evil.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
        )
        with pytest.raises(PatchParseError):
            parse_unified_diff(diff)
