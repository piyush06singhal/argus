"""ARGUS Knowledge Validation (Phase 10 §32–§37, §70).

Judges a mined candidate pattern. It answers three separate questions, and the
separation is the point:

* **Is this a real pattern?** (§32) — sample size, repeatability, data quality.
* **Is it stable?** (§35) — does it hold across two adjacent time windows, or did
  it appear once and evaporate?
* **How much does it generalise?** (§36, §37) — is a "project-level" claim
  actually one component wearing a big hat?

The verdict never silently promotes: it returns a
:class:`ValidationVerdict` with every check recorded, so a rejected pattern
shows *which* check failed and a promoted one shows what it is standing on. That
is what makes §53's knowledge detail page possible at all.

Two deliberate refusals:

* **Instability blocks activation, not candidacy.** A pattern seen only in the
  most recent window is still worth showing as a candidate — it may be a new
  problem — but it may not become ACTIVE on that evidence.
* **Weak evidence stays LOW.** A success ratio near 50% with a small sample is
  not "medium confidence"; it is a coin flip, and the verdict says so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from app.core.config import Settings, get_settings
from app.models.intelligence import KnowledgeConfidence, KnowledgeScope
from app.services.intelligence_state import sample_tier
from app.services.pattern_miners import CorpusEntry, ExperienceCorpus, MinedPattern

logger = logging.getLogger(__name__)

#: A "project-level" claim must not be produced by one component. Above this
#: share of instances from a single component, the scope is downgraded (§37).
CROSS_COMPONENT_DOMINANCE = 0.8

#: Share of POOR-quality records above which the sample is not trustworthy (§30).
MAX_POOR_SHARE = 0.2

#: Support ratios inside this band are ambiguous: the pattern is as likely to be
#: coincidental as real, so it may not reach HIGH confidence (§34).
AMBIGUOUS_BAND = (0.4, 0.6)


@dataclass
class CheckResult:
    """One validation check."""

    name: str
    passed: bool
    detail: str
    blocking: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "blocking": self.blocking,
        }


@dataclass
class ValidationVerdict:
    """The result of validating one candidate pattern."""

    passed: bool
    tier: str
    confidence: KnowledgeConfidence
    checks: list[CheckResult] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    scope: Optional[KnowledgeScope] = None
    #: Whether the pattern may leave CANDIDATE (§33).
    promotable: bool = False
    #: Whether the pattern may become ACTIVE without a human (§35, §73).
    activatable: bool = False
    stability: str = "unknown"
    requires_review: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "tier": self.tier,
            "confidence": self.confidence.value,
            "checks": [check.as_dict() for check in self.checks],
            "limitations": list(self.limitations),
            "scope": self.scope.value if self.scope else None,
            "promotable": self.promotable,
            "activatable": self.activatable,
            "stability": self.stability,
            "requires_review": self.requires_review,
        }

    @property
    def failed_checks(self) -> list[str]:
        return [check.name for check in self.checks if not check.passed]


class KnowledgeValidationService:
    """Evaluates candidate patterns against the §32 checklist."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()

    # -- entry point ------------------------------------------------------
    def validate(
        self,
        pattern: MinedPattern,
        *,
        corpus: ExperienceCorpus,
        cutoff: Optional[datetime] = None,
        conflict_ids: Sequence[str] = (),
    ) -> ValidationVerdict:
        """Run every check for one pattern and summarise the verdict."""
        boundary = _aware(cutoff) or corpus.cutoff
        entries = _entries_for(pattern, corpus)
        checks: list[CheckResult] = []
        limitations = list(pattern.limitations)

        tier, sample_check = self._check_sample_size(pattern)
        checks.append(sample_check)

        quality_check, poor_share = self._check_data_quality(pattern, corpus, entries)
        checks.append(quality_check)

        temporal_check = self._check_temporal_consistency(entries, boundary)
        checks.append(temporal_check)

        stability, stability_check = self._check_stability(entries, boundary)
        checks.append(stability_check)

        scope, scope_check = self._check_scope(pattern, entries)
        checks.append(scope_check)

        support_check, ambiguous = self._check_support(pattern)
        checks.append(support_check)

        conflict_check = self._check_conflicts(conflict_ids)
        checks.append(conflict_check)

        blocking_failures = [
            check for check in checks if not check.passed and check.blocking
        ]

        confidence = self._confidence(
            pattern=pattern,
            tier=tier,
            stability=stability,
            poor_share=poor_share,
            ambiguous=ambiguous,
            stable_ok=stability_check.passed,
        )

        if stability == "unstable":
            limitations.append(
                "The pattern appears in only one of the two adjacent windows; it may be drifting."
            )
        if stability == "insufficient_history":
            limitations.append(
                "History is shorter than two stability windows, so stability could not be established."
            )
        if poor_share > 0:
            limitations.append(
                f"{poor_share:.0%} of the underlying episodes are flagged as low-quality data."
            )
        if conflict_ids:
            limitations.append(
                "Conflicting knowledge exists for the same scope; both are kept and the conflict is not silently resolved."
            )
        if pattern.scope != scope:
            limitations.append(
                f"The pattern was mined at {pattern.scope.value} scope but the evidence is "
                f"concentrated in one component; it is recorded as {scope.value}."
            )

        passed = not blocking_failures
        promotable = (
            passed
            and sample_check.passed
            and stability
            in (
                "stable",
                "insufficient_history",
            )
        )
        #: §35: only a *stable* pattern may be activated without a human.
        activatable = (
            passed
            and promotable
            and stability == "stable"
            and confidence == KnowledgeConfidence.HIGH
            and not conflict_ids
        )
        requires_review = not activatable

        return ValidationVerdict(
            passed=passed,
            tier=tier,
            confidence=confidence,
            checks=checks,
            limitations=limitations,
            scope=scope,
            promotable=promotable,
            activatable=activatable,
            stability=stability,
            requires_review=requires_review,
        )

    # -- checks -----------------------------------------------------------
    def _check_sample_size(self, pattern: MinedPattern) -> tuple[str, CheckResult]:
        tier = sample_tier(
            pattern.sample_count,
            candidate=self.settings.INTELLIGENCE_MIN_SAMPLES_CANDIDATE,
            validation=self.settings.INTELLIGENCE_MIN_SAMPLES_VALIDATION,
            high_confidence=self.settings.INTELLIGENCE_MIN_SAMPLES_HIGH_CONFIDENCE,
        )
        passed = (
            pattern.sample_count >= self.settings.INTELLIGENCE_MIN_SAMPLES_VALIDATION
        )
        detail = (
            f"{tier}: {pattern.sample_count} observations "
            f"(candidate≥{self.settings.INTELLIGENCE_MIN_SAMPLES_CANDIDATE}, "
            f"validation≥{self.settings.INTELLIGENCE_MIN_SAMPLES_VALIDATION}, "
            f"high≥{self.settings.INTELLIGENCE_MIN_SAMPLES_HIGH_CONFIDENCE})"
        )
        return tier, CheckResult(name="sample_size", passed=passed, detail=detail)

    def _check_data_quality(
        self,
        pattern: MinedPattern,
        corpus: ExperienceCorpus,
        entries: Sequence[CorpusEntry],
    ) -> tuple[CheckResult, float]:
        """Reject samples dominated by episodes whose own data was unusable."""
        if not entries:
            return (
                CheckResult(
                    name="data_quality",
                    passed=False,
                    detail="no underlying episodes could be resolved from the corpus",
                ),
                0.0,
            )
        poor = sum(1 for entry in entries if entry.quality == "POOR")
        share = poor / len(entries)
        passed = share <= MAX_POOR_SHARE
        return (
            CheckResult(
                name="data_quality",
                passed=passed,
                detail=f"{poor} of {len(entries)} episodes flagged poor ({share:.0%})",
            ),
            share,
        )

    def _check_temporal_consistency(
        self, entries: Sequence[CorpusEntry], cutoff: datetime
    ) -> CheckResult:
        """Every instance must predate the cutoff — the §31 leakage assertion."""
        future = [entry for entry in entries if _aware(entry.occurred_at) > cutoff]
        if future:
            return CheckResult(
                name="temporal_consistency",
                passed=False,
                detail=(
                    f"{len(future)} of {len(entries)} episodes fall after the learning cutoff; "
                    f"the pattern would be built from future knowledge"
                ),
            )
        return CheckResult(
            name="temporal_consistency",
            passed=True,
            detail=f"all {len(entries)} episodes precede the cutoff",
        )

    def _check_stability(
        self, entries: Sequence[CorpusEntry], cutoff: datetime
    ) -> tuple[str, CheckResult]:
        """Two adjacent windows of observations vs one (§35)."""
        window = timedelta(days=self.settings.INTELLIGENCE_STABILITY_WINDOW_DAYS)
        recent_start = cutoff - window
        previous_start = cutoff - (window * 2)

        recent = [e for e in entries if _aware(e.occurred_at) >= recent_start]
        previous = [
            e for e in entries if previous_start <= _aware(e.occurred_at) < recent_start
        ]
        observed_span = _observed_span(entries)

        if observed_span is not None and observed_span < window * 2:
            return (
                "insufficient_history",
                CheckResult(
                    name="stability",
                    passed=True,
                    blocking=False,
                    detail=(
                        f"history spans {observed_span.days}d, less than the two "
                        f"{self.settings.INTELLIGENCE_STABILITY_WINDOW_DAYS}d windows required"
                    ),
                ),
            )
        if recent and previous:
            return (
                "stable",
                CheckResult(
                    name="stability",
                    passed=True,
                    detail=f"{len(recent)} observations in the recent window, {len(previous)} in the previous",
                ),
            )
        return (
            "unstable",
            CheckResult(
                name="stability",
                passed=False,
                blocking=False,
                detail=(
                    f"the pattern appears in only one adjacent window "
                    f"(recent={len(recent)}, previous={len(previous)})"
                ),
            ),
        )

    def _check_scope(
        self, pattern: MinedPattern, entries: Sequence[CorpusEntry]
    ) -> tuple[KnowledgeScope, CheckResult]:
        """Downgrade a project-level claim that is really one component (§37)."""
        if (
            pattern.scope != KnowledgeScope.PROJECT_LEVEL
            and pattern.component_id is not None
        ):
            return (
                pattern.scope,
                CheckResult(
                    name="cross_component_consistency",
                    passed=True,
                    detail="component-scoped knowledge: no generalisation is claimed",
                ),
            )
        components = [entry.component_id for entry in entries if entry.component_id]
        if not components:
            return (
                pattern.scope,
                CheckResult(
                    name="cross_component_consistency",
                    passed=True,
                    detail="no component attribution available; scope left as mined",
                ),
            )
        counts: dict[str, int] = {}
        for component in components:
            counts[component] = counts.get(component, 0) + 1
        dominant, count = max(counts.items(), key=lambda item: item[1])
        share = count / len(components)
        if share >= CROSS_COMPONENT_DOMINANCE:
            return (
                KnowledgeScope.COMPONENT_SPECIFIC,
                CheckResult(
                    name="cross_component_consistency",
                    passed=False,
                    blocking=False,
                    detail=(
                        f"{share:.0%} of instances come from a single component; "
                        f"a project-wide claim is not supported"
                    ),
                ),
            )
        return (
            pattern.scope,
            CheckResult(
                name="cross_component_consistency",
                passed=True,
                detail=f"instances spread across {len(counts)} components (max {share:.0%})",
            ),
        )

    def _check_support(self, pattern: MinedPattern) -> tuple[CheckResult, bool]:
        """Whether the observable ratio is decisive or a coin flip (§34)."""
        support = pattern.support_strength
        if support is None:
            return (
                CheckResult(
                    name="support_strength",
                    passed=True,
                    blocking=False,
                    detail="the pattern has no success/failure ratio to evaluate",
                ),
                False,
            )
        low, high = AMBIGUOUS_BAND
        ambiguous = low <= support <= high and pattern.sample_count < (
            self.settings.INTELLIGENCE_MIN_SAMPLES_HIGH_CONFIDENCE
        )
        passed = support >= self.settings.INTELLIGENCE_MIN_SUPPORT_STRENGTH or ambiguous
        detail = f"support {support:.0%} over {pattern.sample_count} observations"
        if ambiguous:
            detail += " (ambiguous: the outcome is close to a coin flip)"
        return (
            CheckResult(name="support_strength", passed=passed, detail=detail),
            ambiguous,
        )

    def _check_conflicts(self, conflict_ids: Sequence[str]) -> CheckResult:
        """§66/§67: a conflict is surfaced, never auto-resolved."""
        if conflict_ids:
            return CheckResult(
                name="false_discovery",
                passed=False,
                blocking=False,
                detail=(
                    f"{len(conflict_ids)} existing knowledge item(s) with the same scope "
                    f"claim something different; the conflict is recorded for human review"
                ),
            )
        return CheckResult(
            name="false_discovery",
            passed=True,
            detail="no conflicting knowledge was found for this scope",
        )

    # -- confidence -------------------------------------------------------
    def _confidence(
        self,
        *,
        pattern: MinedPattern,
        tier: str,
        stability: str,
        poor_share: float,
        ambiguous: bool,
        stable_ok: bool,
    ) -> KnowledgeConfidence:
        """Bucket the evidence (§34).

        The ordering of these refusals is deliberate: nothing below pushes
        confidence *up*; each condition can only hold it down.
        """
        if pattern.sample_count < self.settings.INTELLIGENCE_MIN_SAMPLES_CANDIDATE:
            return KnowledgeConfidence.UNKNOWN
        if poor_share > MAX_POOR_SHARE:
            return KnowledgeConfidence.LOW
        if ambiguous:
            return KnowledgeConfidence.LOW
        if stability == "unstable" and not stable_ok:
            return KnowledgeConfidence.LOW
        if (
            tier == "STRONG"
            and stability == "stable"
            and pattern.support_strength is not None
            and pattern.support_strength
            >= self.settings.INTELLIGENCE_MIN_SUPPORT_STRENGTH
        ):
            return KnowledgeConfidence.HIGH
        if pattern.sample_count >= self.settings.INTELLIGENCE_MIN_SAMPLES_VALIDATION:
            return KnowledgeConfidence.MEDIUM
        return KnowledgeConfidence.LOW


def _entries_for(pattern: MinedPattern, corpus: ExperienceCorpus) -> list[CorpusEntry]:
    """Resolve a pattern's experience ids back to corpus entries."""
    wanted = set(pattern.experience_ids)
    return [entry for entry in corpus.entries if entry.experience_id in wanted]


def _observed_span(entries: Sequence[CorpusEntry]) -> Optional[timedelta]:
    if not entries:
        return None
    moments = [_aware(entry.occurred_at) for entry in entries]
    return max(moments) - min(moments)


def _aware(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


__all__ = [
    "AMBIGUOUS_BAND",
    "CROSS_COMPONENT_DOMINANCE",
    "CheckResult",
    "KnowledgeValidationService",
    "MAX_POOR_SHARE",
    "ValidationVerdict",
]
