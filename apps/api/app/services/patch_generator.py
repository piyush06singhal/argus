"""ARGUS Patch Generation (Phase 7 §8, §9, §20, §21, §22, §57).

Turns a fix hypothesis into candidate patches. Two generators share one
output contract:

* **The deterministic generator** needs no model at all. It composes the
  patch from the *stored* file content of the pinned snapshot: the target
  hunk is computed against the real bytes, so the diff always applies.
  This is what makes the demo (§59) and the whole pipeline honest without
  an AI provider.
* **The AI generator** (§21) wraps :class:`~app.services.ai_debugger.
  resolve_provider`. The model receives *bounded, redacted* context — never
  repository-wide access — and must return the structured §21 shape. Its
  output is validated with Pydantic (§22): malformed JSON, unknown files,
  invalid risk/confidence are rejections, never silently repaired.

Both generators produce a :class:`GeneratedPatch` carrying the measured
minimality numbers (§9) and the §20 explanation block. Neither writes to the
database; the service layer persists.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from pydantic import BaseModel as PydanticBaseModel
from pydantic import Field, ValidationError, field_validator

from app.services.patch_parser import ParsedPatch, parse_unified_diff
from app.services.source_redaction import SourceRedactor

logger = logging.getLogger(__name__)


class PatchGenerationError(ValueError):
    """A patch could not be generated — recorded as GENERATION_FAILED (§57)."""


# ---------------------------------------------------------------------------
# §21/§22 — the structured model-output contract
# ---------------------------------------------------------------------------


class ModelPatchProposal(PydanticBaseModel):
    """The §21 JSON shape, validated (§22).

    Pydantic is the gate: invalid JSON raises before this class is even
    constructed; unknown files, invalid risk/confidence, and an empty patch
    raise here — and are recorded, never repaired (§22).
    """

    summary: str = Field(min_length=1, max_length=500)
    files: list[str] = Field(default_factory=list)
    patch: str = Field(min_length=1)
    reasoning_summary: str = Field(default="", max_length=2000)
    expected_behavior: list[str] = Field(default_factory=list)
    risk: str = "MEDIUM"
    confidence: str = "MEDIUM"

    @field_validator("risk")
    @classmethod
    def _risk(cls, value: str) -> str:
        allowed = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
        if value not in allowed:
            raise ValueError(f"risk must be one of {sorted(allowed)}")
        return value

    @field_validator("confidence")
    @classmethod
    def _confidence(cls, value: str) -> str:
        allowed = {"LOW", "MEDIUM", "HIGH"}
        if value not in allowed:
            raise ValueError(f"confidence must be one of {sorted(allowed)}")
        return value

    @field_validator("files")
    @classmethod
    def _files_clean(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            probe = item.strip()
            #: ``lstrip("./")`` would happily strip a leading ``..`` too, so
            #: the traversal check must run on the *unstripped* path; a
            #: backslash is rejected outright as a Windows-style path.
            if probe.startswith("/") or ".." in probe.split("/") or "\\" in probe:
                raise ValueError(f"file path escapes the workspace: {item!r}")
            cleaned.append(probe)
        return cleaned


# ---------------------------------------------------------------------------
# §9 — minimality measurement
# ---------------------------------------------------------------------------


@dataclass
class PatchMeasurements:
    """§9's numbers, measured from the parsed diff — never claimed."""

    files_changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    symbols_modified: list[str] = field(default_factory=list)
    affected_paths: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "files_changed": self.files_changed,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "symbols_modified": list(self.symbols_modified),
            "affected_paths": list(self.affected_paths),
        }


def measure(
    parsed: ParsedPatch, *, known_symbols: Sequence[str] = ()
) -> PatchMeasurements:
    """Measure a parsed patch (§9). Symbols are matched by name occurrence."""
    affected = parsed.paths
    blob = "\n".join(
        line.content
        for item in parsed.files
        for hunk in item.hunks
        for line in hunk.lines
        if line.is_addition or line.is_removal
    )
    symbols = [
        name
        for name in known_symbols
        if name and re.search(rf"\b{re.escape(name)}\b", blob)
    ]
    return PatchMeasurements(
        files_changed=parsed.file_count,
        lines_added=parsed.lines_added,
        lines_removed=parsed.lines_removed,
        symbols_modified=symbols,
        affected_paths=affected,
    )


def render_unified_diff(
    *,
    before: str,
    after: str,
    path: str,
    context: int = 3,
) -> str:
    """Render a unified diff for one file, in the ``a/``/``b/`` form."""
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    body = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=context,
        )
    )
    if not body:
        return ""
    # difflib emits "--- a/path\t\n" with an empty date; strip trailing tabs.
    cleaned = [line.rstrip("\t").rstrip("\n") for line in body]
    return "\n".join(cleaned) + "\n"


# ---------------------------------------------------------------------------
# The generated-patch result
# ---------------------------------------------------------------------------


@dataclass
class GeneratedPatch:
    """One candidate patch, ready for the service layer to persist (§8)."""

    patch_content: str
    parsed: ParsedPatch
    measurements: PatchMeasurements
    explanation: dict[str, Any]
    risk: str
    confidence: str
    generated_by: str
    generation_model: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "patch_content": self.patch_content,
            "measurements": self.measurements.as_dict(),
            "explanation": self.explanation,
            "risk": self.risk,
            "confidence": self.confidence,
            "generated_by": self.generated_by,
            "generation_model": self.generation_model,
        }


def _explanation_block(
    *,
    summary: str,
    reasoning: str,
    expected_behavior: Sequence[str],
    evidence_refs: Sequence[dict],
    hypothesis_title: str,
    measurements: PatchMeasurements,
) -> dict[str, Any]:
    """The §20 block: what, why, which hypothesis, evidence, expected."""
    return {
        "what_changed": summary,
        "why_changed": reasoning or summary,
        "addresses_hypothesis": hypothesis_title,
        "evidence": [
            item.get("reference", "") for item in evidence_refs if item.get("reference")
        ],
        "expected_behavior": list(expected_behavior),
        "unchanged_behavior": (
            "Behaviour outside the changed files — including the public API "
            "surface and existing test expectations — is unchanged."
        ),
        "measurements": measurements.as_dict(),
    }


# ---------------------------------------------------------------------------
# Deterministic generator (§59 demo; no model required)
# ---------------------------------------------------------------------------


#: Substitution recipes the deterministic generator knows how to apply.
#: Each recipe targets an evidence-named defect shape; a hypothesis that
#: matches none of them is refused rather than half-applied (§57).
@dataclass
class _Recipe:
    name: str
    category_markers: tuple[str, ...]
    #: (pattern, replacement) pairs applied to the file text.
    substitutions: tuple[tuple[str, str], ...]
    summary: str
    risk: str = "MEDIUM"


_RECIPES: tuple[_Recipe, ...] = (
    _Recipe(
        name="bound_db_timeout",
        category_markers=("timeout", "database", "query", "db"),
        substitutions=(
            # The demo's planted defect: a timeout configured below one second
            # turns a slow query into an immediate failure. The fix restores a
            # sane budget and keeps the guard meaningful.
            ("DB_TIMEOUT_SECONDS = 0.25", "DB_TIMEOUT_SECONDS = 2.0"),
            ("DB_TIMEOUT_SECONDS = 0.5", "DB_TIMEOUT_SECONDS = 2.0"),
        ),
        summary=(
            "Restore the database query timeout to a workable budget so slow "
            "queries are waited on rather than failed immediately"
        ),
    ),
    _Recipe(
        name="bound_retries",
        category_markers=("retry", "retries", "backoff"),
        substitutions=(
            ("RETRY_ATTEMPTS = 7", "RETRY_ATTEMPTS = 2"),
            ("RETRY_ATTEMPTS = 5", "RETRY_ATTEMPTS = 2"),
            ("RETRY_ATTEMPTS = 3", "RETRY_ATTEMPTS = 2"),
        ),
        summary=(
            "Bound the retry loop so a persistent downstream failure fails "
            "within the caller's deadline instead of being amplified"
        ),
        risk="LOW",
    ),
    _Recipe(
        name="linear_to_capped_backoff",
        category_markers=("backoff", "retry"),
        substitutions=(
            (
                "return RETRY_BACKOFF_SECONDS * (attempt + 1)",
                "return RETRY_BACKOFF_SECONDS * min(attempt + 1, MAX_BACKOFF_MULTIPLIER)",
            ),
            (
                "RETRY_BACKOFF_SECONDS = 0.4",
                "RETRY_BACKOFF_SECONDS = 0.4\nMAX_BACKOFF_MULTIPLIER = 2",
            ),
        ),
        summary=(
            "Cap the linear backoff so retries cannot push the request past "
            "its deadline"
        ),
        risk="LOW",
    ),
)


class DeterministicPatchGenerator:
    """Generates the smallest evidence-shaped patch without a model (§59).

    The generator reads the *stored* file content of the pinned snapshot,
    applies the recipe's substitutions, and renders a real unified diff —
    so the output always parses and always applies to the base commit.
    A hypothesis whose defect shape no recipe matches is refused (§57):
    no patch is invented.
    """

    name = "deterministic"

    def __init__(self, *, redactor: Optional[SourceRedactor] = None) -> None:
        self._redactor = redactor or SourceRedactor()

    def generate(
        self,
        *,
        hypothesis_title: str,
        hypothesis_description: str,
        category: str,
        proposed_change: str,
        scope_files: Sequence[str],
        target_symbols: Sequence[str] = (),
        evidence_refs: Sequence[dict] = (),
        file_contents: dict[str, str],
    ) -> GeneratedPatch:
        """Compose the patch from the stored file bytes (§8, §9)."""
        blob = " ".join(
            filter(
                None,
                [hypothesis_description, proposed_change, category.replace("_", " ")],
            )
        ).lower()

        chosen: Optional[_Recipe] = None
        for recipe in _RECIPES:
            if any(marker in blob for marker in recipe.category_markers):
                chosen = recipe
                break
        if chosen is None:
            raise PatchGenerationError(
                "no deterministic recipe matches this hypothesis's evidence; "
                "ARGUS will not guess a patch (§57)"
            )

        changed: dict[str, str] = {}
        for path in scope_files:
            text = file_contents.get(path)
            if text is None:
                continue
            updated = text
            applied_here = False
            for pattern, replacement in chosen.substitutions:
                if pattern in updated:
                    updated = updated.replace(pattern, replacement)
                    applied_here = True
            if applied_here and updated != text:
                changed[path] = updated

        if not changed:
            raise PatchGenerationError(
                f"the '{chosen.name}' fix matched the hypothesis but none of its "
                "substitutions applied to the scoped files' stored content; "
                "the defect shape may already be fixed (§57)"
            )

        diff_parts: list[str] = []
        for path in sorted(changed):
            diff = render_unified_diff(
                before=file_contents[path], after=changed[path], path=path
            )
            if diff:
                diff_parts.append(diff)
        patch_text = "\n".join(diff_parts)
        if not patch_text.strip():
            raise PatchGenerationError("the composed patch is empty (§57)")

        parsed = parse_unified_diff(patch_text)
        measurements = measure(parsed, known_symbols=target_symbols)
        risk = chosen.risk
        if measurements.files_changed > 2 or (
            measurements.lines_added + measurements.lines_removed > 20
        ):
            risk = "MEDIUM"

        return GeneratedPatch(
            patch_content=patch_text,
            parsed=parsed,
            measurements=measurements,
            explanation=_explanation_block(
                summary=chosen.summary,
                reasoning=(
                    f"Derived from the debugging evidence: {hypothesis_description}"
                ),
                expected_behavior=[
                    "The original failure no longer reproduces.",
                    "Existing behaviour outside the changed files is unchanged.",
                ],
                evidence_refs=evidence_refs,
                hypothesis_title=hypothesis_title,
                measurements=measurements,
            ),
            risk=risk,
            confidence="MEDIUM",
            generated_by=self.name,
        )


# ---------------------------------------------------------------------------
# AI generator (§21)
# ---------------------------------------------------------------------------

_PATCH_PROMPT = """You are ARGUS's fix generator. Produce the smallest defensible
unified diff that fixes the described failure.

HARD CONSTRAINTS (violating any of these makes the patch invalid):
- Change ONLY these files: {scope}
- Do not add dependencies.
- Do not remove or weaken tests; do not disable linting or type checking.
- Do not modify CI, build or verification configuration.
- Do not include secrets.
- Keep the patch small and focused.

INCIDENT: {incident_title}
HYPOTHESIS: {hypothesis}
PROPOSED CHANGE: {proposed_change}
CATEGORY: {category}

EVIDENCE (already validated):
{evidence}

FILE CONTENTS (redacted, the only code you may change):
{files}

Respond with JSON only, in exactly this shape:
{{
  "summary": "one sentence: what changed",
  "files": ["{first_scope}"],
  "patch": "unified diff with --- a/<path> / +++ b/<path> headers",
  "reasoning_summary": "two sentences: why this change addresses the evidence",
  "expected_behavior": ["behaviour after the fix"],
  "risk": "LOW|MEDIUM|HIGH|CRITICAL",
  "confidence": "LOW|MEDIUM|HIGH"
}}
"""


class AIFixGenerator:
    """Model-assisted patch generation over the existing provider (§21)."""

    name = "ai"

    def __init__(
        self,
        provider: Any,
        *,
        model_name: Optional[str] = None,
        redactor: Optional[SourceRedactor] = None,
        max_file_bytes: int = 20_000,
    ) -> None:
        self.provider = provider
        self.model_name = model_name
        self._redactor = redactor or SourceRedactor()
        self._max_file_bytes = max_file_bytes

    def generate(
        self,
        *,
        incident_title: str,
        hypothesis_title: str,
        hypothesis_description: str,
        proposed_change: str,
        category: str,
        scope_files: Sequence[str],
        evidence_refs: Sequence[dict] = (),
        target_symbols: Sequence[str] = (),
        file_contents: dict[str, str],
    ) -> GeneratedPatch:
        if not scope_files:
            raise PatchGenerationError("the fix hypothesis has an empty scope")
        if not file_contents:
            raise PatchGenerationError(
                "no stored file content is available for the scoped paths; "
                "refusing to let the model guess file contents (§21)"
            )

        files_block = ""
        for path in scope_files:
            text = file_contents.get(path)
            if text is None:
                # §57 — an unknown file is not offered to the model at all;
                # inventing content for it is the hallucinated-file failure.
                logger.info("scope path %s has no stored content; omitted", path)
                continue
            encoded = text.encode("utf-8")
            if len(encoded) > self._max_file_bytes:
                text = text[: self._max_file_bytes] + "\n# … truncated"
            redacted, _report = self._redactor.redact(text)
            files_block += f"--- {path} ---\n{redacted}\n"
        if not files_block:
            raise PatchGenerationError(
                "none of the scoped paths have stored content; generation refused"
            )

        evidence_block = (
            "\n".join(
                f"- {item.get('reference', '')} ({item.get('kind', '')})"
                for item in evidence_refs
                if item.get("reference")
            )
            or "- (no additional evidence references)"
        )
        prompt = _PATCH_PROMPT.format(
            scope=", ".join(scope_files),
            first_scope=scope_files[0],
            incident_title=incident_title,
            hypothesis=f"{hypothesis_title}: {hypothesis_description}",
            proposed_change=proposed_change,
            category=category,
            evidence=evidence_block,
            files=files_block,
        )

        try:
            raw: dict[str, Any] = _run_async(
                self.provider.complete_structured(prompt, response_schema={})
            )
        except Exception as error:  # noqa: BLE001 - §57 records, never fabricates
            raise PatchGenerationError(
                f"AI provider failed: {type(error).__name__}: {error}"
            ) from error

        try:
            proposal = ModelPatchProposal.model_validate(raw)
        except ValidationError as error:
            raise PatchGenerationError(
                f"model output failed schema validation (§22): {error.errors()[:3]}"
            ) from error

        declared = set(proposal.files) or set(scope_files)
        parsed = parse_unified_diff(proposal.patch)
        patch_paths = set(parsed.paths)
        if not patch_paths:
            raise PatchGenerationError(
                "the model returned a patch with no file changes"
            )
        if not patch_paths.issubset(declared | set(scope_files)):
            unknown = sorted(patch_paths - (declared | set(scope_files)))
            raise PatchGenerationError(
                f"the model's patch touches files outside the requested scope "
                f"(§57): {', '.join(unknown)}"
            )
        if patch_paths - set(file_contents):
            missing = sorted(patch_paths - set(file_contents))
            raise PatchGenerationError(
                f"the model wrote a diff for a file whose content was never "
                f"provided — hallucinated file (§57): {', '.join(missing)}"
            )

        measurements = measure(parsed, known_symbols=target_symbols)
        return GeneratedPatch(
            patch_content=proposal.patch,
            parsed=parsed,
            measurements=measurements,
            explanation=_explanation_block(
                summary=proposal.summary,
                reasoning=proposal.reasoning_summary,
                expected_behavior=proposal.expected_behavior,
                evidence_refs=evidence_refs,
                hypothesis_title=hypothesis_title,
                measurements=measurements,
            ),
            risk=proposal.risk,
            confidence=proposal.confidence,
            generated_by=self.name,
            generation_model=getattr(self.provider, "model", None) or self.model_name,
        )


def _run_async(coroutine: Any) -> Any:
    """Run one coroutine to completion from sync code (the worker is async;
    generation itself is a pure CPU-bound step)."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(coroutine)).result(timeout=120)
    return asyncio.run(coroutine)


__all__ = [
    "AIFixGenerator",
    "DeterministicPatchGenerator",
    "GeneratedPatch",
    "ModelPatchProposal",
    "PatchGenerationError",
    "PatchMeasurements",
    "measure",
    "render_unified_diff",
]
