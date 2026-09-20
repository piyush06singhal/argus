"""ARGUS Temporal Analyzer (Phase 4 §10–§12, §31).

Pure, deterministic ordering analysis over timestamped facts. This module
answers "what happened, in what order, how close together, and what recovered
when" — and *only* that. It never claims that precedence is causation:

    before ≠ caused

Three outputs matter downstream:

* :class:`TemporalFact` — a normalized timestamped event (UTC, aware).
* :class:`TemporalRelation` — the ordering of two facts (PRECEDES / FOLLOWS /
  SIMULTANEOUS / OVERLAPS) with the gap in seconds.
* :class:`TemporalAssessment` — whether a candidate's timing supports,
  contradicts, or is neutral toward explaining an onset.

Contradiction (§12) is a first-class result: an event *after* the degradation
it is proposed to explain cannot explain it, and saying so is the analyzer's
job. Recovery ordering (§31) is analyzed symmetrically: the supposed cause
recovering *after* its alleged effects is weak/contradicting evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterable, Optional, Sequence

from app.core.time import ensure_utc, ensure_utc_or_now

#: Two events within this many seconds count as SIMULTANEOUS for ordering.
SIMULTANEITY_TOLERANCE_SECONDS = 2


class TemporalOrder(str, Enum):
    PRECEDES = "PRECEDES"
    FOLLOWS = "FOLLOWS"
    SIMULTANEOUS = "SIMULTANEOUS"
    OVERLAPS = "OVERLAPS"


class TemporalVerdict(str, Enum):
    """Timing's contribution to a causal hypothesis."""

    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    NEUTRAL = "NEUTRAL"


@dataclass(frozen=True)
class TemporalFact:
    """A normalized, timestamped observation used by ordering analysis."""

    key: str
    at: datetime
    label: str
    #: Optional interval end (for events with duration). ``None`` = instant.
    until: Optional[datetime] = None

    @property
    def at_utc(self) -> datetime:
        return ensure_utc_or_now(self.at)

    @property
    def until_utc(self) -> Optional[datetime]:
        return ensure_utc(self.until) if self.until is not None else None

    @property
    def end_utc(self) -> datetime:
        return self.until_utc or self.at_utc


def make_fact(
    key: str, at: Optional[datetime], label: str, until: Optional[datetime] = None
) -> Optional[TemporalFact]:
    """Build a fact, or ``None`` when the record has no usable timestamp.

    Naive datetimes are interpreted as UTC (storage is UTC everywhere); missing
    timestamps are skipped rather than guessed (no fabricated evidence).
    """
    if at is None:
        return None
    return TemporalFact(key=key, at=at, label=label, until=until)


@dataclass(frozen=True)
class TemporalRelation:
    """The temporal relation between two facts (direction: a → b)."""

    a_key: str
    b_key: str
    order: TemporalOrder
    #: Seconds from a's start to b's start (negative if a starts after b).
    gap_seconds: int
    #: True when a's interval also covers b's start (overlap).
    overlapping: bool


@dataclass
class TemporalAssessment:
    """Timing verdict for one candidate against one onset (§11–§12)."""

    verdict: TemporalVerdict
    reason: str
    #: Seconds between the candidate event and the onset (negative = after).
    lead_seconds: Optional[int] = None
    #: Structured notes for the explanation API (§37).
    details: dict = field(default_factory=dict)


class TemporalAnalyzer:
    """Deterministic ordering analysis. No database, no clock, no randomness."""

    def __init__(self, *, now: Optional[datetime] = None) -> None:
        # ``now`` exists only for callers that need a stable reference point
        # for still-open intervals; the analyzer itself never reads the clock.
        self._now = ensure_utc_or_now(now)

    # -- Pairwise relations -------------------------------------------------
    def relate(self, a: TemporalFact, b: TemporalFact) -> TemporalRelation:
        """Order two facts (direction: a → b)."""
        a_start, b_start = a.at_utc, b.at_utc
        a_end, b_end = a.end_utc, b.end_utc  # noqa: F841 (b_end kept for symmetry)
        gap = (b_start - a_start).total_seconds()
        overlapping = (
            a_start <= b_start <= a_end and (b_start - a_start).total_seconds() >= 0
        )
        if abs(gap) <= SIMULTANEITY_TOLERANCE_SECONDS:
            order = TemporalOrder.SIMULTANEOUS
        elif gap > 0:
            order = TemporalOrder.PRECEDES
        else:
            order = TemporalOrder.FOLLOWS
        if overlapping and order is not TemporalOrder.SIMULTANEOUS:
            order = TemporalOrder.OVERLAPS
        return TemporalRelation(
            a_key=a.key,
            b_key=b.key,
            order=order,
            gap_seconds=int(gap),
            overlapping=overlapping,
        )

    # -- Sequences ----------------------------------------------------------
    def order_facts(self, facts: Sequence[TemporalFact]) -> list[TemporalFact]:
        """Deterministic chronological ordering (ties broken by key, then label)."""
        return sorted(facts, key=lambda f: (f.at_utc, f.key, f.label))

    def gaps(
        self, facts: Sequence[TemporalFact]
    ) -> list[tuple[TemporalFact, TemporalFact, int]]:
        """Consecutive gaps in the chronologically ordered sequence."""
        ordered = self.order_facts(facts)
        out: list[tuple[TemporalFact, TemporalFact, int]] = []
        for prev, nxt in zip(ordered, ordered[1:]):
            out.append((prev, nxt, int((nxt.at_utc - prev.at_utc).total_seconds())))
        return out

    def within(
        self, facts: Iterable[TemporalFact], start: datetime, end: datetime
    ) -> list[TemporalFact]:
        """Facts whose start lies inside ``[start, end]`` (both normalized)."""
        lo = ensure_utc_or_now(start)
        hi = ensure_utc_or_now(end)
        if lo > hi:
            lo, hi = hi, lo
        return [f for f in facts if lo <= f.at_utc <= hi]

    def first_before(
        self, fact: TemporalFact, facts: Sequence[TemporalFact]
    ) -> Optional[TemporalFact]:
        """The latest fact that still starts before ``fact`` (immediate predecessor)."""
        predecessors = [
            f for f in facts if self.relate(f, fact).order is TemporalOrder.PRECEDES
        ]
        if not predecessors:
            return None
        return self.order_facts(predecessors)[-1]

    # -- Onset assessment (§11–§12) -----------------------------------------
    def assess_against_onset(
        self,
        candidate: TemporalFact,
        onset: TemporalFact,
        *,
        max_lead_seconds: int = 3600,
    ) -> TemporalAssessment:
        """Does the candidate's timing support explaining ``onset``?

        * Event strictly before the onset (within ``max_lead_seconds``):
          SUPPORTS (temporal support only — never causal proof).
        * Event after the onset: CONTRADICTS — it cannot explain what already
          happened (though it may be relevant to *later* behaviour).
        * Simultaneous or too early: NEUTRAL.
        """
        relation = self.relate(candidate, onset)
        gap = relation.gap_seconds
        details = {
            "candidate_at": candidate.at_utc.isoformat(),
            "onset_at": onset.at_utc.isoformat(),
            "gap_seconds": gap,
            "order": relation.order.value,
        }
        if relation.order in (TemporalOrder.PRECEDES, TemporalOrder.OVERLAPS):
            if gap > max_lead_seconds:
                return TemporalAssessment(
                    verdict=TemporalVerdict.NEUTRAL,
                    reason=(
                        f"{candidate.label} occurred {gap}s before {onset.label}, "
                        f"outside the {max_lead_seconds}s relevance window"
                    ),
                    lead_seconds=gap,
                    details=details,
                )
            return TemporalAssessment(
                verdict=TemporalVerdict.SUPPORTS,
                reason=f"{candidate.label} occurred {self._human_gap(gap)} before {onset.label}",
                lead_seconds=gap,
                details=details,
            )
        if relation.order is TemporalOrder.SIMULTANEOUS:
            return TemporalAssessment(
                verdict=TemporalVerdict.NEUTRAL,
                reason=f"{candidate.label} occurred at the same time as {onset.label}",
                lead_seconds=gap,
                details=details,
            )
        # Candidate after onset — cannot explain it (§12).
        return TemporalAssessment(
            verdict=TemporalVerdict.CONTRADICTS,
            reason=(
                f"{candidate.label} occurred {self._human_gap(-gap)} AFTER {onset.label}; "
                "it cannot explain the initial degradation "
                "(it may still be relevant to later behaviour)"
            ),
            lead_seconds=gap,
            details=details,
        )

    # -- Recovery analysis (§31) --------------------------------------------
    def assess_recovery(
        self,
        cause_recovery: Optional[TemporalFact],
        effect_recoveries: Sequence[TemporalFact],
    ) -> TemporalAssessment:
        """Ordering of the supposed cause's recovery vs its effects' recoveries.

        Supporting: cause recovered before (or with) the effects — consistent
        with the cause driving the effects. Contradicting: effects recovered
        while the cause was still degraded.
        """
        if cause_recovery is None:
            return TemporalAssessment(
                verdict=TemporalVerdict.NEUTRAL,
                reason="No recovery observed for the candidate component",
                details={"recovered": False},
            )
        if not effect_recoveries:
            return TemporalAssessment(
                verdict=TemporalVerdict.NEUTRAL,
                reason="No effect recoveries recorded yet to compare against",
                details={"recovered": True, "effect_recoveries": 0},
            )
        cause_at = cause_recovery.at_utc
        before = [e for e in effect_recoveries if e.at_utc > cause_at]
        after = [e for e in effect_recoveries if e.at_utc <= cause_at]
        details = {
            "recovered": True,
            "cause_recovered_at": cause_at.isoformat(),
            "effects_recovered_after_cause": len(before),
            "effects_recovered_before_cause": len(after),
        }
        if before and not after:
            return TemporalAssessment(
                verdict=TemporalVerdict.SUPPORTS,
                reason=(
                    f"{cause_recovery.label} recovered before all "
                    f"{len(before)} dependent recoveries"
                ),
                details=details,
            )
        if after and not before:
            return TemporalAssessment(
                verdict=TemporalVerdict.CONTRADICTS,
                reason=(
                    f"{len(after)} dependent(s) recovered before "
                    f"{cause_recovery.label} recovered — weakens the hypothesis"
                ),
                details=details,
            )
        return TemporalAssessment(
            verdict=TemporalVerdict.NEUTRAL,
            reason="Recovery ordering is mixed",
            details=details,
        )

    # -- Persistence (§10) --------------------------------------------------
    def persistence(self, fact: TemporalFact) -> int:
        """How long the fact's condition has persisted (seconds)."""
        end = fact.until_utc or self._now
        return max(0, int((end - fact.at_utc).total_seconds()))

    @staticmethod
    def _human_gap(seconds: int) -> str:
        seconds = abs(seconds)
        if seconds < 60:
            return f"{seconds} seconds"
        if seconds < 3600:
            return f"{seconds // 60}m {seconds % 60}s"
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


__all__ = [
    "SIMULTANEITY_TOLERANCE_SECONDS",
    "TemporalAnalyzer",
    "TemporalAssessment",
    "TemporalFact",
    "TemporalOrder",
    "TemporalRelation",
    "TemporalVerdict",
    "make_fact",
]
