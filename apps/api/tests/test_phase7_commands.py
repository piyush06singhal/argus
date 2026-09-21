"""Phase 7 — command registry, executor security, and AI generation (§21, §22, §50–§53).

The command registry is what makes arbitrary execution structurally
impossible: these tests try to break out of it the ways an AI would.
"""

from __future__ import annotations

import pytest

from app.services.patch_commands import (
    REGISTRY,
    CommandExecutor,
    CommandRefused,
    NETWORK_OFFLINE,
    detect_commands,
    select_tests,
)


class TestCommandRegistry:
    def test_unknown_key_refused(self, tmp_path):
        executor = CommandExecutor()
        with pytest.raises(CommandRefused):
            executor.execute(tmp_path, "rm -rf /")

    def test_registry_keys_are_fixed_shape(self):
        """Every registry entry is a static allowlist entry — no free text."""
        for key, command in REGISTRY.items():
            assert command.key == key
            assert command.kind in ("static", "build", "test")
            assert all(isinstance(part, str) for part in command.argv_template)
            assert command.timeout_seconds > 0

    def test_network_offline_by_default(self):
        for command in REGISTRY.values():
            assert command.network == NETWORK_OFFLINE

    def test_detect_commands_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        detected = detect_commands(tmp_path)
        assert "python_tests" in detected["test"]
        assert "python_syntax" in detected["static"]

    def test_detect_commands_node(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"scripts": {"build": "tsc", "test": "vitest"}}'
        )
        detected = detect_commands(tmp_path)
        assert "node_build" in detected["build"]
        assert "node_tests" in detected["test"]

    def test_detect_commands_empty_repo(self, tmp_path):
        detected = detect_commands(tmp_path)
        assert detected == {"static": [], "build": [], "test": []}


class TestTestSelection:
    def test_selection_prefers_changed_module(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_inventory.py").write_text(
            "def test_a():\n    pass\n"
        )
        (tmp_path / "tests" / "test_unrelated.py").write_text(
            "def test_b():\n    pass\n"
        )
        detected = detect_commands(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        detected = detect_commands(tmp_path)
        reason, selected = select_tests(
            detected=detected,
            changed_files=["services/inventory/repository.py"],
            workspace_root=tmp_path,
        )
        assert selected == ["tests/test_inventory.py"]

    def test_selection_bounded(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        for index in range(30):
            (tmp_path / "tests" / f"test_mod_{index}.py").write_text(
                "def test():\n    pass\n"
            )
        detected = detect_commands(tmp_path)
        _, selected = select_tests(
            detected=detected,
            changed_files=["no_match/thing.py"],
            workspace_root=tmp_path,
            max_tests=20,
        )
        assert len(selected) <= 20


class TestExecutorSecurity:
    def test_output_is_redacted(self, tmp_path):
        """§52 — even allowlisted command output is secret-scanned."""
        # The python syntax command compiles a file containing a fake secret;
        # the redactor must scrub it from the recorded tail.
        module = tmp_path / "leaky.py"
        module.write_text('x = "AKIAIOSFODNN7EXAMPLE"\n')
        executor = CommandExecutor()
        result = executor.execute(tmp_path, "python_syntax", ["leaky.py"])
        # The command succeeds (the file compiles), but any echoed secret in
        # the tail must be redacted:
        assert "AKIAIOSFODNN7EXAMPLE" not in (result.output_tail or "")

    def test_executor_records_failure_not_crash(self, tmp_path):
        (tmp_path / "broken.py").write_text("def broken(:\n    pass\n")
        executor = CommandExecutor()
        result = executor.execute(tmp_path, "python_syntax", ["broken.py"])
        assert result.exit_code != 0
        assert not result.ok


class TestAIGeneration:
    """§21, §22 — the model-output contract, validated, never repaired."""

    #: The provider contract: ``complete_structured(prompt, response_schema)``
    #: awaited by the generator. Scripted, so a test pins what a *specific*
    #: malformed answer earns (§22).
    GOOD_PATCH = (
        "--- a/shop/checkout.py\n+++ b/shop/checkout.py\n"
        "@@ -1,1 +1,1 @@\n-RETRIES = 7\n+RETRIES = 2\n"
    )

    def _provider(self, payload):
        class _Scripted:
            async def complete_structured(self, prompt, *, response_schema):
                if isinstance(payload, Exception):
                    raise payload
                return payload

        return _Scripted()

    def _generate(self, provider, *, scope_files=("shop/checkout.py",)):
        from app.services.patch_generator import AIFixGenerator

        return AIFixGenerator(provider).generate(
            incident_title="checkout timeouts",
            hypothesis_title="retry amplification",
            hypothesis_description="retries extend requests past the deadline",
            proposed_change="bound the retry budget",
            category="RETRY_FIX",
            scope_files=list(scope_files),
            evidence_refs=[],
            file_contents={"shop/checkout.py": "RETRIES = 7\n"},
        )

    def test_valid_json_patch_accepted(self):
        result = self._generate(
            self._provider(
                {
                    "summary": "Bound the retry budget",
                    "files": ["shop/checkout.py"],
                    "patch": self.GOOD_PATCH,
                    "reasoning_summary": "Retries amplify the outage.",
                    "expected_behavior": ["Fails within the deadline."],
                    "risk": "LOW",
                    "confidence": "MEDIUM",
                }
            )
        )
        assert result.generated_by == "ai"
        assert result.parsed.paths == ["shop/checkout.py"]
        assert result.measurements.lines_removed == 1

    def test_invalid_risk_rejected(self):
        from app.services.patch_generator import PatchGenerationError

        with pytest.raises(PatchGenerationError):
            self._generate(
                self._provider(
                    {
                        "summary": "x",
                        "files": ["shop/checkout.py"],
                        "patch": self.GOOD_PATCH,
                        "risk": "CERTAIN",
                        "confidence": "MEDIUM",
                    }
                )
            )

    def test_traversal_file_rejected(self):
        """§22 — Pydantic refuses the model's traversal path outright."""
        from app.services.patch_generator import PatchGenerationError

        with pytest.raises(PatchGenerationError):
            self._generate(
                self._provider(
                    {
                        "summary": "x",
                        "files": ["../../etc/passwd"],
                        "patch": self.GOOD_PATCH,
                        "risk": "LOW",
                        "confidence": "HIGH",
                    }
                )
            )

    def test_out_of_scope_change_rejected(self):
        from app.services.patch_generator import PatchGenerationError

        with pytest.raises(PatchGenerationError):
            self._generate(
                self._provider(
                    {
                        "summary": "touch a file outside scope",
                        "files": ["billing/charger.py"],
                        "patch": "--- a/billing/charger.py\n+++ b/billing/charger.py\n@@ -1,1 +1,1 @@\n-a\n+b\n",
                        "risk": "LOW",
                        "confidence": "HIGH",
                    }
                )
            )

    def test_hallucinated_file_rejected(self):
        """§57 — a diff for a file whose content was never provided."""
        from app.services.patch_generator import PatchGenerationError

        with pytest.raises(PatchGenerationError):
            self._generate(
                self._provider(
                    {
                        "summary": "edits a file we never showed it",
                        "files": ["shop/inventory.py"],
                        "patch": "--- a/shop/inventory.py\n+++ b/shop/inventory.py\n@@ -1,1 +1,1 @@\n-a\n+b\n",
                        "risk": "LOW",
                        "confidence": "HIGH",
                    }
                )
            )

    def test_provider_failure_recorded_not_fabricated(self):
        from app.services.patch_generator import PatchGenerationError

        with pytest.raises(PatchGenerationError) as excinfo:
            self._generate(self._provider(RuntimeError("provider down")))
        assert "provider" in str(excinfo.value).lower()
