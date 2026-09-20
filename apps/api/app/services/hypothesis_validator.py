"""ARGUS Hypothesis Validator (Phase 5 §31–§36, §65).

Turns an experiment's observations into a statement about the *hypothesis* —
and is the place where the phase's most important rule is enforced in code:

    a failed reproduction does not refute the hypothesis.

Why that rule needs code and not just prose: the obvious implementation (result
``FAILED`` → verdict ``NOT_SUPPORTED``) is wrong in a way that looks right. The
sandbox may have lacked state, used a different dependency version, been unable
to reach a hidden dependency, or simply been too slow. Each of those makes the
experiment *uninformative*, not the hypothesis false. So this validator only
returns ``NOT_SUPPORTED`` when the experiment itself was **sound**:

* every run produced comparable telemetry (nothing ``INCONCLUSIVE``);
* the environment matched closely enough (no major differences, §33);
* the planned inputs were all available (nothing missing);
* the reproduction was genuinely attempted (faults actually landed, replays
  actually reached the services).

Failing any of those downgrades the verdict to ``INCONCLUSIVE`` with the specific
reason recorded, because "we could not test this properly" is a real answer and
"the hypothesis is wrong" is a much stronger claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from app.models.causal import ConfidenceLevel
from app.models.reproduction import (
    FailureClass,
    ReproductionResult,
    ValidationOutcome,
)
from app.services.reproduction_comparator import ComparisonResult

#: Environment difference severities that make an experiment unsound.
MAJOR = "major"
MINOR = "minor"

#: Standard limitations attached to every verdict, so the API never presents a
#: reproduction as more definitive than an experiment can be (§65).
BASE_LIMITATIONS: tuple[str, ...] = (
    "A reproduction is an experiment on a model of the system, not the "
    "production system itself.",
    "A failed reproduction does not by itself disprove the hypothesis; it may "
    "mean the sandbox lacked the relevant state or timing.",
    "Injected faults make the observed failure an induced one: this shows the "
    "failure is reachable, not that it is what actually happened in production.",
)


@dataclass
class ValidationResult:
    """The verdict, with everything needed to justify and bound it."""

    outcome: ValidationOutcome
    confidence: ConfidenceLevel
    summary: str
    supporting: list[dict[str, Any]] = field(default_factory=list)
    contradicting: list[dict[str, Any]] = field(default_factory=list)
    environment_differences: list[dict[str, Any]] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)
    determinism: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    #: Why the verdict is what it is, in order of consideration.
    reasoning: list[str] = field(default_factory=list)


class HypothesisValidator:
    """Judges a hypothesis against one experiment's runs."""

    def validate(
        self,
        *,
        hypothesis_statement: str,
        comparisons: Sequence[ComparisonResult],
        environment_differences: Sequence[dict[str, Any]] = (),
        missing_inputs: Sequence[str] = (),
        faults: Sequence[dict[str, Any]] = (),
        failure_classifications: Sequence[Optional[FailureClass]] = (),
        replay_reached_services: bool = True,
    ) -> ValidationResult:
        reasoning: list[str] = []
        major_differences = [
            item
            for item in environment_differences
            if str(item.get("severity")) == MAJOR
        ]

        if not comparisons:
            return ValidationResult(
                outcome=ValidationOutcome.INCONCLUSIVE,
                confidence=ConfidenceLevel.INSUFFICIENT,
                summary=(
                    "The experiment produced no comparable runs, so it says nothing "
                    "about the hypothesis."
                ),
                environment_differences=list(environment_differences),
                missing_inputs=list(missing_inputs),
                determinism=_determinism_block([]),
                limitations=list(BASE_LIMITATIONS)
                + ["No run produced telemetry that could be compared."],
                reasoning=["no comparison rows were produced"],
            )

        determinism = _determinism_block(comparisons)
        successful = [
            item for item in comparisons if item.result is ReproductionResult.SUCCESSFUL
        ]
        partial = [
            item for item in comparisons if item.result is ReproductionResult.PARTIAL
        ]
        failed = [
            item for item in comparisons if item.result is ReproductionResult.FAILED
        ]
        inconclusive = [
            item
            for item in comparisons
            if item.result is ReproductionResult.INCONCLUSIVE
        ]

        supporting: list[dict[str, Any]] = []
        contradicting: list[dict[str, Any]] = []

        for comparison in successful:
            supporting.append(
                {
                    "kind": "SUCCESSFUL_REPRODUCTION",
                    "similarity": comparison.overall_similarity,
                    "detail": comparison.explanation,
                    "matched_components": comparison.matched_components,
                    "sequence": comparison.sequence_reproduced,
                }
            )
        for comparison in partial:
            supporting.append(
                {
                    "kind": "PARTIAL_REPRODUCTION",
                    "similarity": comparison.overall_similarity,
                    "detail": comparison.explanation,
                    "matched_components": comparison.matched_components,
                    "missing_components": comparison.missing_components,
                }
            )
        for comparison in failed:
            contradicting.append(
                {
                    "kind": "NO_FAILURE_OBSERVED",
                    "similarity": comparison.overall_similarity,
                    "detail": comparison.explanation,
                    "expected_missing": comparison.expectations_missing,
                }
            )
        for comparison in inconclusive:
            contradicting.append(
                {
                    "kind": "INCONCLUSIVE_RUN",
                    "similarity": comparison.overall_similarity,
                    "detail": comparison.explanation,
                }
            )

        # A run-level failure classification explains *why* a run was null, which
        # is usually the difference between a refutation and an uninformative
        # experiment.
        classifications = [c.value for c in failure_classifications if c is not None]
        explainable_null = {
            FailureClass.ENVIRONMENT_ERROR.value,
            FailureClass.TIMEOUT.value,
            FailureClass.RESOURCE_LIMIT.value,
            FailureClass.DEPENDENCY_UNAVAILABLE.value,
            FailureClass.SANDBOX_ERROR.value,
            FailureClass.INSUFFICIENT_TELEMETRY.value,
            FailureClass.INPUT_ERROR.value,
        }

        if successful and not failed and not inconclusive:
            outcome = ValidationOutcome.SUPPORTED
            reasoning.append(
                f"all {len(successful)} run(s) reproduced the expected failure "
                "sequence"
            )
        elif successful:
            outcome = ValidationOutcome.PARTIALLY_SUPPORTED
            reasoning.append(
                f"{len(successful)} of {len(comparisons)} run(s) reproduced the "
                "failure while others did not"
            )
        elif partial:
            outcome = ValidationOutcome.PARTIALLY_SUPPORTED
            reasoning.append(
                f"{len(partial)} run(s) reproduced part of the expected behaviour"
            )
        elif inconclusive and not failed:
            outcome = ValidationOutcome.INCONCLUSIVE
            reasoning.append("every run was inconclusive")
        elif failed and not inconclusive:
            # The only path to NOT_SUPPORTED, and it requires a sound experiment.
            unsound: list[str] = []
            if major_differences:
                unsound.append(
                    f"the sandbox differs from the original environment in "
                    f"{len(major_differences)} major way(s)"
                )
            if missing_inputs:
                unsound.append(
                    f"{len(missing_inputs)} planned input(s) were unavailable"
                )
            if not replay_reached_services:
                unsound.append("replay traffic did not reach the sandbox services")
            if any(code in explainable_null for code in classifications):
                unsound.append(
                    "at least one run failed for an infrastructural reason "
                    f"({', '.join(sorted(set(classifications)))})"
                )
            if unsound:
                outcome = ValidationOutcome.INCONCLUSIVE
                reasoning.append(
                    "the sandbox did not reproduce the failure, but the experiment "
                    "was not sound: " + "; ".join(unsound)
                )
            else:
                outcome = ValidationOutcome.NOT_SUPPORTED
                reasoning.append(
                    "the sandbox ran the plan successfully and the expected failure "
                    "did not occur, under a matching environment"
                )
        else:
            outcome = ValidationOutcome.INCONCLUSIVE
            reasoning.append("the runs disagree in a way that cannot be attributed")

        confidence = _confidence_for(
            outcome=outcome,
            comparisons=comparisons,
            determinism=determinism,
            major_differences=major_differences,
            missing_inputs=missing_inputs,
        )

        injected = [item for item in faults if item.get("injected")]
        limitations = list(BASE_LIMITATIONS)
        if major_differences:
            limitations.append(
                "Major environment differences were detected, so the reproduction "
                "environment is only an approximation of the original."
            )
        if missing_inputs:
            limitations.append(
                "Some planned inputs were unavailable; the run exercised fewer "
                "conditions than the incident."
            )
        if determinism.get("classification") == "INTERMITTENT":
            limitations.append(
                "The reproduction is intermittent, so a single future run has a "
                "meaningful chance of not reproducing it."
            )
        if not injected:
            limitations.append(
                "No fault was injected: the failure either emerged on its own or "
                "did not occur."
            )
        elif injected:
            limitations.append(
                f"{len(injected)} fault(s) were injected on "
                f"{', '.join(sorted({str(item.get('target')) for item in injected}))}."
            )

        summary = _summary_for(
            outcome=outcome,
            hypothesis=hypothesis_statement,
            determinism=determinism,
            comparisons=comparisons,
            injected=len(injected),
        )

        return ValidationResult(
            outcome=outcome,
            confidence=confidence,
            summary=summary,
            supporting=supporting,
            contradicting=contradicting,
            environment_differences=list(environment_differences),
            missing_inputs=list(missing_inputs),
            determinism=determinism,
            limitations=limitations,
            reasoning=reasoning,
        )


def _determinism_block(comparisons: Sequence[ComparisonResult]) -> dict[str, Any]:
    """Repeatability as an *observation*, never as a causal probability (§34)."""
    runs = len(comparisons)
    if runs == 0:
        return {
            "runs": 0,
            "reproduction_rate": None,
            "classification": "NOT_RUN",
            "note": "No runs were comparable.",
        }
    successful = sum(
        1 for item in comparisons if item.result is ReproductionResult.SUCCESSFUL
    )
    partial = sum(
        1 for item in comparisons if item.result is ReproductionResult.PARTIAL
    )
    reproduced = successful + partial
    rate = reproduced / runs
    if runs == 1:
        classification = "REPRODUCED" if reproduced else "NOT_REPRODUCED"
    elif rate == 1.0:
        classification = "DETERMINISTIC"
    elif rate == 0.0:
        classification = "NOT_REPRODUCED"
    else:
        classification = "INTERMITTENT"
    return {
        "runs": runs,
        "successful_runs": successful,
        "partial_runs": partial,
        "reproduction_rate": round(rate, 4),
        "classification": classification,
        "note": (
            "Reproduction rate is an experimental observation over "
            f"{runs} run(s) — it is not a probability that the hypothesis is true."
        ),
    }


def _confidence_for(
    *,
    outcome: ValidationOutcome,
    comparisons: Sequence[ComparisonResult],
    determinism: dict[str, Any],
    major_differences: Sequence[dict[str, Any]],
    missing_inputs: Sequence[str],
) -> ConfidenceLevel:
    """Coarse confidence in the *verdict*, capped by experiment fidelity."""
    if outcome is ValidationOutcome.INCONCLUSIVE:
        return ConfidenceLevel.INSUFFICIENT
    buckets = {item.overall_similarity for item in comparisons}
    rate = determinism.get("reproduction_rate") or 0.0
    fully_matched = all(
        item.expectations_total > 0
        and item.expectations_matched == item.expectations_total
        for item in comparisons
        if item.result is ReproductionResult.SUCCESSFUL
    )
    if (
        outcome is ValidationOutcome.SUPPORTED
        and not major_differences
        and not missing_inputs
        and rate == 1.0
        and "HIGH" in buckets
        and fully_matched
    ):
        return ConfidenceLevel.HIGH
    if outcome is ValidationOutcome.NOT_SUPPORTED and not (
        major_differences or missing_inputs
    ):
        return ConfidenceLevel.MEDIUM
    if outcome in {ValidationOutcome.SUPPORTED, ValidationOutcome.NOT_SUPPORTED}:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


def _summary_for(
    *,
    outcome: ValidationOutcome,
    hypothesis: str,
    determinism: dict[str, Any],
    comparisons: Sequence[ComparisonResult],
    injected: int,
) -> str:
    rate = determinism.get("reproduction_rate")
    rate_text = (
        f" Reproduction rate {rate:.0%} over {determinism.get('runs')} run(s)."
        if isinstance(rate, (int, float))
        else ""
    )
    match = next(
        (
            item
            for item in comparisons
            if item.result is not ReproductionResult.INCONCLUSIVE
        ),
        comparisons[0] if comparisons else None,
    )
    detail = f" {match.explanation}" if match is not None else ""
    injection = (
        " The observed failure was induced by an injected fault."
        if injected
        else " No fault was injected."
    )
    verdicts = {
        ValidationOutcome.SUPPORTED: "SUPPORTED — an isolated reproduction produced the expected failure sequence.",
        ValidationOutcome.PARTIALLY_SUPPORTED: "PARTIALLY SUPPORTED — the reproduction matched only part of the expected behaviour.",
        ValidationOutcome.NOT_SUPPORTED: "NOT SUPPORTED — the expected failure did not occur in a sound reproduction.",
        ValidationOutcome.INCONCLUSIVE: "INCONCLUSIVE — the experiment did not produce a usable answer.",
    }
    return f"{verdicts[outcome]}{rate_text}{injection}{detail}"


__all__ = [
    "BASE_LIMITATIONS",
    "MAJOR",
    "MINOR",
    "HypothesisValidator",
    "ValidationResult",
]
