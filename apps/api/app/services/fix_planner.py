"""ARGUS Fix Planning (Phase 7 §5, §6, §10, §20).

Turns a debugging session's stored evidence into a :class:`FixHypothesis` —
the *idea* a patch will implement. The planner is deliberately conservative:

* A hypothesis exists only when real rows exist behind it: a debug session,
  ideally an analysis with validated code locations, and — when Phase 5 ran
  one — a reproduction experiment whose behaviour the fix must change.
  With no evidence the planner refuses rather than invents (§5: "must
  originate from actual debugging evidence").
* The category (§6) is *chosen from evidence keywords in the analysis text and
  the failure shape*; when nothing supports one it stays ``UNKNOWN``.
* The scope allowlist (§10) starts from the analysis's *validated* locations
  only, plus the symbols' files. An engineer may widen it explicitly; nothing
  widens it implicitly.
* Excluded paths (§10) are the sensitive-area defaults from the safety
  validator — the hypothesis may *not* opt into them implicitly either.

The planner never produces a patch; it produces the plan a generator must
respect, and the evidence references (§20) an explanation must cite.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.fix import FixHypothesis

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.causal import RootCauseCandidate
from app.models.code import (
    DebugAnalysisRun,
    DebugCodeLocation,
    DebugEvidence,
    DebugHypothesis,
    DebugSession,
    DebugSessionStatus,
    LocationValidation,
)
from app.models.fix import FixCategory, FixStatus, RiskLevel
from app.models.incident import Incident
from app.services.patch_safety import SENSITIVE_PATTERNS

logger = logging.getLogger(__name__)

#: Imported through a module-level alias so the annotation below resolves for
#: the type checker without a circular import at module-eval time (the fix
#: models import nothing from services, so this is safe).
from app.models.fix import FixHypothesis as _FixHypothesis  # noqa: E402


class FixPlanningError(ValueError):
    """A fix hypothesis cannot be planned, with the reason recorded."""


#: §6 — keyword → category. Evidence-led: a category is chosen only when the
#: analysis text or the failing symbols actually name the concept.
_CATEGORY_KEYWORDS: tuple[tuple[FixCategory, tuple[str, ...]], ...] = (
    (FixCategory.RETRY_FIX, ("retry", "retries", "backoff", "retry budget")),
    (FixCategory.TIMEOUT_FIX, ("timeout", "timed out", "deadline", "time out")),
    (FixCategory.ERROR_HANDLING, ("error handling", "exception", "unhandled", "catch")),
    (
        FixCategory.DATABASE_QUERY_FIX,
        ("query", "database", "sql", "connection pool", "db "),
    ),
    (FixCategory.VALIDATION_FIX, ("validation", "validate", "invalid input", "schema")),
    (
        FixCategory.RESOURCE_HANDLING,
        ("memory", "leak", "file handle", "descriptor", "resource"),
    ),
    (
        FixCategory.CONCURRENCY_FIX,
        ("race", "deadlock", "lock", "concurrent", "thread", "async"),
    ),
    (
        FixCategory.API_CONTRACT_FIX,
        ("api contract", "status code", "response shape", "payload", "4xx", "5xx"),
    ),
    (
        FixCategory.CONFIGURATION_FIX,
        ("configuration", "config", "environment variable", "setting"),
    ),
    (
        FixCategory.DEPENDENCY_HANDLING,
        ("dependency", "package", "library version", "incompatible"),
    ),
    (
        FixCategory.PERFORMANCE_FIX,
        ("latency", "slow", "performance", "n+1", "hot path"),
    ),
    (
        FixCategory.BUG_FIX,
        ("off by", "wrong variable", "typo", "logic error", "swapped"),
    ),
)


@dataclass
class PlannedFix:
    """The planner's output: a hypothesis ready to persist (not yet persisted)."""

    project_id: Any
    incident_id: Any
    title: str
    description: str
    proposed_change: str
    expected_behavior: str
    category: FixCategory
    scope_files: list[str] = field(default_factory=list)
    excluded_paths: list[str] = field(default_factory=list)
    supporting_evidence: list[dict[str, Any]] = field(default_factory=list)
    target_symbols: list[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    confidence: str = "LOW"
    debug_session_id: Optional[Any] = None
    analysis_run_id: Optional[Any] = None
    root_cause_candidate_id: Optional[Any] = None
    reproduction_experiment_id: Optional[Any] = None
    repository_id: Optional[Any] = None
    snapshot_id: Optional[Any] = None

    def model(self) -> "FixHypothesis":
        """Materialise the plan as a model row (not yet persisted)."""
        return _FixHypothesis(
            project_id=self.project_id,
            incident_id=self.incident_id,
            debug_session_id=self.debug_session_id,
            analysis_run_id=self.analysis_run_id,
            root_cause_candidate_id=self.root_cause_candidate_id,
            reproduction_experiment_id=self.reproduction_experiment_id,
            repository_id=self.repository_id,
            snapshot_id=self.snapshot_id,
            title=self.title,
            description=self.description,
            proposed_change=self.proposed_change,
            expected_behavior=self.expected_behavior,
            category=self.category,
            scope_files=self.scope_files,
            excluded_paths=self.excluded_paths,
            supporting_evidence=self.supporting_evidence,
            target_symbols=self.target_symbols,
            risk_level=self.risk_level,
            confidence=self.confidence,
            status=FixStatus.HYPOTHESIZED,
            created_by="argus",
        )


def derive_category(*texts: str) -> FixCategory:
    """The evidence-led category (§6), or ``UNKNOWN``."""
    blob = " \n".join(text for text in texts if text).lower()
    if not blob.strip():
        return FixCategory.UNKNOWN
    for category, keywords in _CATEGORY_KEYWORDS:
        for keyword in keywords:
            if keyword in blob:
                return category
    return FixCategory.UNKNOWN


def default_excluded_paths() -> list[str]:
    """The §10 defaults: every sensitive area is excluded unless widened."""
    return sorted({pattern for pattern, _area in SENSITIVE_PATTERNS})


class FixPlanner:
    """Builds a fix hypothesis from a debug session's stored evidence (§5)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def plan_from_session(
        self,
        debug_session_id: uuid.UUID,
        *,
        title: Optional[str] = None,
        scope_override: Optional[Sequence[str]] = None,
        root_cause_candidate_id: Optional[uuid.UUID] = None,
        reproduction_experiment_id: Optional[uuid.UUID] = None,
        created_by: Optional[str] = None,
    ) -> PlannedFix:
        """Plan a fix from the session's latest analysis (§5, §20).

        Refuses — with a recorded reason — when the session has no analysis,
        no validated code locations, and no hypothesis to build on: that is
        the ``GENERATION_FAILED``-before-the-fact case, and pretending
        otherwise would produce a fix hypothesis with nothing behind it.
        """
        debug_session = await self.session.get(DebugSession, debug_session_id)
        if debug_session is None:
            raise FixPlanningError(f"unknown debug session {debug_session_id}")
        if debug_session.status not in (
            DebugSessionStatus.COMPLETED,
            DebugSessionStatus.WAITING_FOR_VALIDATION,
        ):
            raise FixPlanningError(
                f"debug session {debug_session_id} is {debug_session.status.value}; "
                "a fix needs a completed analysis"
            )

        analysis = await self.session.scalar(
            select(DebugAnalysisRun)
            .where(DebugAnalysisRun.session_id == debug_session.id)
            .order_by(DebugAnalysisRun.started_at.desc())
            .limit(1)
        )
        if analysis is None:
            raise FixPlanningError("the session has no analysis run to plan from")

        incident = await self.session.get(Incident, debug_session.incident_id)
        if incident is None:
            raise FixPlanningError("the session's incident no longer exists")

        # -- validated locations: the seed of the scope (§10) ---------------
        #: The validation column *is* the anti-hallucination gate (Phase 6):
        #: only locations whose file/symbol/lines resolved against the pinned
        #: snapshot may seed a fix scope.
        locations = (
            (
                await self.session.execute(
                    select(DebugCodeLocation).where(
                        DebugCodeLocation.analysis_run_id == analysis.id,
                        DebugCodeLocation.validation == LocationValidation.VALID,
                    )
                )
            )
            .scalars()
            .all()
        )
        validated = list(locations)
        scope_files = list(
            dict.fromkeys([item.file_path for item in validated if item.file_path])
        )
        target_symbols = list(
            dict.fromkeys(
                [item.symbol_name or "" for item in validated if item.symbol_name]
            )
        )

        hypothesis_rows = (
            (
                await self.session.execute(
                    select(DebugHypothesis).where(
                        DebugHypothesis.analysis_run_id == analysis.id
                    )
                )
            )
            .scalars()
            .all()
        )
        evidence_rows: list[DebugEvidence] = []
        if hypothesis_rows:
            evidence_rows = list(
                (
                    await self.session.execute(
                        select(DebugEvidence).where(
                            DebugEvidence.hypothesis_id.in_(
                                [row.id for row in hypothesis_rows]
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )

        # -- the primary hypothesis text (§5) -------------------------------
        primary = None
        for hypothesis_row in hypothesis_rows:
            if hypothesis_row.validation_status.value in (
                "SUPPORTED",
                "PARTIALLY_SUPPORTED",
            ):
                primary = hypothesis_row
                break
        primary = primary or (hypothesis_rows[0] if hypothesis_rows else None)

        if not scope_files and primary is None:
            raise FixPlanningError(
                "the analysis has no validated code locations and no hypotheses; "
                "there is no evidence to plan a fix from"
            )

        candidate: Optional[RootCauseCandidate] = None
        #: Hypotheses carry no RCA pointer in the Phase 6 model; the candidate
        #: comes from the explicit argument or nowhere.
        if root_cause_candidate_id is not None:
            candidate = await self.session.get(
                RootCauseCandidate, root_cause_candidate_id
            )

        # -- evidence references (§20) ---------------------------------------
        evidence_refs: list[dict[str, Any]] = []
        seen_refs: set[str] = set()
        for evidence_row in evidence_rows:
            reference = evidence_row.reference or ""
            if reference and reference not in seen_refs:
                seen_refs.add(reference)
                evidence_refs.append(
                    {
                        "reference": reference,
                        "kind": evidence_row.kind.value,
                        "polarity": evidence_row.polarity.value,
                    }
                )
        if analysis.id is not None:
            evidence_refs.append(
                {"reference": f"debug-analysis:{analysis.id}", "kind": "analysis"}
            )
        if candidate is not None:
            evidence_refs.append(
                {"reference": f"causal-candidate:{candidate.id}", "kind": "rca"}
            )
        if reproduction_experiment_id is not None:
            evidence_refs.append(
                {
                    "reference": f"reproduction:{reproduction_experiment_id}",
                    "kind": "reproduction",
                }
            )

        # -- the texts (§5, §20) ---------------------------------------------
        description_parts = [primary.description for primary in [primary] if primary]
        if not description_parts:
            description_parts = [item.reason for item in validated[:3] if item.reason]
        description = " ".join(description_parts) or (
            f"Failure observed on {incident.title}; see linked evidence."
        )
        category = derive_category(
            description,
            analysis.summary or "",
            *[item.label for item in validated[:5]],
        )
        proposed_change = self._proposed_change_text(primary, category, validated)
        expected_behavior = self._expected_behavior_text(analysis, primary)

        planned = PlannedFix(
            project_id=debug_session.project_id,
            incident_id=debug_session.incident_id,
            title=title or (primary.description[:180] if primary else incident.title),
            description=description,
            proposed_change=proposed_change,
            expected_behavior=expected_behavior,
            category=category,
            scope_files=sorted(set(scope_override or scope_files)),
            excluded_paths=default_excluded_paths(),
            supporting_evidence=evidence_refs,
            target_symbols=target_symbols,
            risk_level=RiskLevel.MEDIUM,
            confidence=(analysis.confidence.value if analysis.confidence else "LOW"),
            debug_session_id=debug_session.id,
            analysis_run_id=analysis.id,
            root_cause_candidate_id=candidate.id if candidate else None,
            reproduction_experiment_id=reproduction_experiment_id,
            repository_id=debug_session.repository_id,
            snapshot_id=debug_session.snapshot_id,
        )
        if scope_override is not None:
            planned.scope_files = sorted(
                {item.strip() for item in scope_override if item.strip()}
            )
        return planned

    # ------------------------------------------------------------------
    def _proposed_change_text(self, primary, category: FixCategory, locations) -> str:
        """A concrete, hypothesis-shaped statement of the change (§5)."""
        symbol_note = ""
        if locations:
            names = ", ".join(
                filter(None, (item.symbol_name for item in locations[:3]))
            )
            if names:
                symbol_note = f" (in {names})"
        category_text = {
            FixCategory.RETRY_FIX: "Bound the retry behaviour so the failure cannot be amplified",
            FixCategory.TIMEOUT_FIX: "Correct the timeout handling so the caller fails within its deadline",
            FixCategory.ERROR_HANDLING: "Handle the failure path explicitly instead of propagating it",
            FixCategory.DATABASE_QUERY_FIX: "Repair the database access so queries complete within budget",
            FixCategory.VALIDATION_FIX: "Validate the offending input before it causes the failure",
            FixCategory.RESOURCE_HANDLING: "Release/acquire the resource deterministically",
            FixCategory.CONCURRENCY_FIX: "Serialise the racy access",
            FixCategory.API_CONTRACT_FIX: "Restore the expected API contract",
            FixCategory.CONFIGURATION_FIX: "Correct the misapplied configuration",
            FixCategory.DEPENDENCY_HANDLING: "Resolve the dependency incompatibility",
            FixCategory.PERFORMANCE_FIX: "Remove the introduced latency",
            FixCategory.BUG_FIX: "Correct the faulty logic",
            FixCategory.UNKNOWN: "Apply the smallest change that removes the failure",
        }.get(category, "Apply the smallest change that removes the failure")
        return f"{category_text}{symbol_note}."

    def _expected_behavior_text(self, analysis, primary) -> str:
        if primary is not None and primary.test_approach:
            return primary.test_approach
        if analysis.summary:
            return (
                "The original failure no longer reproduces; existing behaviour "
                "outside the changed path is unchanged."
            )
        return (
            "The original failure no longer reproduces; existing behaviour "
            "outside the changed path is unchanged."
        )


__all__ = [
    "FixPlanner",
    "FixPlanningError",
    "PlannedFix",
    "default_excluded_paths",
    "derive_category",
]
