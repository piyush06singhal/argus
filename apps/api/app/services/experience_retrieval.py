"""ARGUS Historical Experience Retrieval (Phase 10 §13, §46, §49).

"Have we seen this before?" — answered from stored episodes, with the answer's
provenance attached.

The service is thin on purpose: the ranking lives in
:mod:`app.services.similarity_engine` and the episodes in
:mod:`app.models.intelligence`. What this module owns is the *contract* of a
retrieval:

* Every match carries its explanation, its outcome and its resolution. A caller
  cannot get a similarity score without the evidence that produced it.
* An empty result is positive information, not a gap: it returns the literal
  ``No comparable historical case was found.`` — the sentence §49 requires —
  rather than an empty list a consumer might paper over with a guess.
* The cutoff is enforced in SQL, so retrieval for a historical question cannot
  see cases that happened after it (§31).

Retrieval only ever *reads*. It is legal for a recommendation to be built on a
retrieval result; it is not possible for retrieval to change anything.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.intelligence import ReliabilityExperience
from app.services.learning_signatures import (
    FailureSignature,
    ResolutionSignature,
    SignaturePair,
)
from app.services.similarity_engine import (
    RankedExperience,
    ReliabilitySimilarityEngine,
)

logger = logging.getLogger(__name__)

#: §49. The required answer when there is no evidence. Kept as a constant so the
#: API, the UI and the tests cannot drift into three different phrasings.
NO_HISTORY_MESSAGE = "No comparable historical case was found."

#: Text attached to every retrieval result, because a matching signature is not a
#: matching cause.
RETRIEVAL_LIMITATIONS: tuple[str, ...] = (
    "Similarity is computed from normalized failure signatures, not from confirmed causes.",
    "Historical outcomes describe past episodes; they do not predict this one.",
)


@dataclass
class RetrievalMatch:
    """One historical episode that resembles the query."""

    experience_id: str
    similarity: float
    explanation: dict[str, Any]
    outcome: str
    occurred_at: Optional[datetime]
    component_id: Optional[str]
    environment_id: Optional[str]
    incident_id: Optional[str]
    resolution: Optional[dict[str, Any]] = None
    recovery_seconds: Optional[int] = None
    data_quality: str = "OK"
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "experience_id": self.experience_id,
            "similarity": round(self.similarity, 4),
            "explanation": self.explanation,
            "outcome": self.outcome,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "component_id": self.component_id,
            "environment_id": self.environment_id,
            "incident_id": self.incident_id,
            "resolution": self.resolution,
            "recovery_seconds": self.recovery_seconds,
            "data_quality": self.data_quality,
            "limitations": list(self.limitations),
        }


@dataclass
class RetrievalResult:
    """The answer to a historical question (§13)."""

    query: dict[str, Any]
    matches: list[RetrievalMatch] = field(default_factory=list)
    total_candidates: int = 0
    summary: str = NO_HISTORY_MESSAGE
    limitations: list[str] = field(default_factory=lambda: list(RETRIEVAL_LIMITATIONS))

    @property
    def evidence_available(self) -> bool:
        return bool(self.matches)

    @property
    def success_count(self) -> int:
        return sum(1 for match in self.matches if _is_success(match))

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "matches": [match.as_dict() for match in self.matches],
            "total_candidates": self.total_candidates,
            "evidence_available": self.evidence_available,
            "summary": self.summary,
            "limitations": list(self.limitations),
            "success_count": self.success_count,
        }


def _is_success(match: RetrievalMatch) -> bool:
    if match.resolution and match.resolution.get("rollback_performed"):
        return False
    if match.resolution and match.resolution.get("outcome") == "effective":
        return True
    return match.outcome in ("effective", "patch_verified")


class ExperienceRetrievalService:
    """Ranks stored episodes against a current situation."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        engine: Optional[ReliabilitySimilarityEngine] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.engine = engine or ReliabilitySimilarityEngine()

    async def load_candidates(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        cutoff: Optional[datetime] = None,
        component_id: Optional[uuid.UUID] = None,
        environment_id: Optional[uuid.UUID] = None,
        lookback_days: Optional[int] = None,
        exclude_experience_id: Optional[uuid.UUID] = None,
        limit: int = 2000,
    ) -> list[SignaturePair]:
        """Load episodes eligible to be compared, with the §31 cutoff applied."""
        moment = _aware(cutoff) or datetime.now(timezone.utc)
        stmt = (
            select(ReliabilityExperience)
            .where(ReliabilityExperience.project_id == project_id)
            .where(ReliabilityExperience.end_time <= moment)
            .order_by(ReliabilityExperience.end_time.desc())
            .limit(limit)
        )
        if component_id is not None:
            stmt = stmt.where(
                ReliabilityExperience.primary_component_id == component_id
            )
        if environment_id is not None:
            stmt = stmt.where(ReliabilityExperience.environment_id == environment_id)
        if lookback_days is not None:
            stmt = stmt.where(
                ReliabilityExperience.end_time >= moment - timedelta(days=lookback_days)
            )
        if exclude_experience_id is not None:
            stmt = stmt.where(ReliabilityExperience.id != exclude_experience_id)

        rows = list((await session.scalars(stmt)).all())
        candidates: list[SignaturePair] = []
        for row in rows:
            candidates.append(
                SignaturePair(
                    failure=FailureSignature.from_dict(row.failure_signature),
                    resolution=(
                        ResolutionSignature.from_dict(row.resolution_signature)
                        if row.resolution_signature
                        else None
                    ),
                    experience_id=str(row.id),
                    occurred_at=row.end_time,
                    component_id=(
                        str(row.primary_component_id)
                        if row.primary_component_id
                        else None
                    ),
                    metadata={
                        "environment_id": (
                            str(row.environment_id) if row.environment_id else None
                        ),
                        "incident_id": str(row.incident_id)
                        if row.incident_id
                        else None,
                        "outcome": row.outcome,
                        "recovery_seconds": row.recovery_seconds,
                        "data_quality": row.data_quality,
                    },
                )
            )
        return candidates

    async def retrieve(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        signature: FailureSignature,
        component_id: Optional[uuid.UUID] = None,
        environment_id: Optional[uuid.UUID] = None,
        cutoff: Optional[datetime] = None,
        threshold: Optional[float] = None,
        limit: Optional[int] = None,
        lookback_days: Optional[int] = None,
        exclude_experience_id: Optional[uuid.UUID] = None,
        query: Optional[dict[str, Any]] = None,
    ) -> RetrievalResult:
        """Fetch the episodes that most resemble ``signature``."""
        candidates = await self.load_candidates(
            session,
            project_id=project_id,
            cutoff=cutoff,
            component_id=component_id,
            environment_id=environment_id,
            lookback_days=lookback_days,
            exclude_experience_id=exclude_experience_id,
        )
        ranked = self.engine.rank(
            signature,
            candidates,
            threshold=(
                threshold
                if threshold is not None
                else self.settings.INTELLIGENCE_SIMILARITY_THRESHOLD
            ),
            limit=(
                limit
                if limit is not None
                else self.settings.INTELLIGENCE_SIMILARITY_MAX_RESULTS
            ),
            component_id=str(component_id) if component_id else None,
            environment_id=str(environment_id) if environment_id else None,
        )
        return self._result(
            ranked,
            total_candidates=len(candidates),
            query=query
            or {"signature": signature.label(), "fingerprint": signature.fingerprint()},
            component_id=component_id,
        )

    async def retrieve_for_incident(
        self,
        session: AsyncSession,
        *,
        incident_id: uuid.UUID,
        project_id: uuid.UUID,
        cutoff: Optional[datetime] = None,
        threshold: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> RetrievalResult:
        """Retrieve for a *current* incident, described by the shared builder."""
        from app.services.experience_builder import build_current_signature

        signature, reason = await build_current_signature(
            session, incident_id=incident_id, as_of=cutoff
        )
        if signature is None:
            return RetrievalResult(
                query={"incident_id": str(incident_id), "error": reason},
                summary=NO_HISTORY_MESSAGE,
            )

        #: The incident's own component and environment are the context; the
        #: environment is not part of the signature (§36).
        context = await self._incident_context(session, incident_id)
        result = await self.retrieve(
            session,
            project_id=project_id,
            signature=signature,
            component_id=context.get("component_id"),
            environment_id=context.get("environment_id"),
            cutoff=cutoff,
            threshold=threshold,
            limit=limit,
            query={
                "incident_id": str(incident_id),
                "signature": signature.label(),
                "fingerprint": signature.fingerprint(),
            },
        )
        return result

    async def remediation_history(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        signature: FailureSignature,
        threshold: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> RetrievalResult:
        """Similar episodes that were actually remediated (§13)."""
        result = await self.retrieve(
            session,
            project_id=project_id,
            signature=signature,
            threshold=threshold,
            limit=limit,
            query={"signature": signature.label(), "filter": "remediated"},
        )
        result.matches = [match for match in result.matches if match.resolution]
        result.summary = self._summarise(result.matches)
        return result

    # -- helpers ----------------------------------------------------------
    def _result(
        self,
        ranked: Sequence[RankedExperience],
        *,
        total_candidates: int,
        query: dict[str, Any],
        component_id: Optional[uuid.UUID] = None,
    ) -> RetrievalResult:
        matches: list[RetrievalMatch] = []
        for item in ranked:
            entry = item.experience
            matches.append(
                RetrievalMatch(
                    experience_id=entry.experience_id or "",
                    similarity=item.similarity,
                    explanation=item.explanation.as_dict(),
                    outcome=str(entry.metadata.get("outcome") or "unknown"),
                    occurred_at=entry.occurred_at,
                    component_id=entry.component_id,
                    environment_id=entry.metadata.get("environment_id"),
                    incident_id=entry.metadata.get("incident_id"),
                    resolution=(
                        entry.resolution.as_dict()
                        if entry.resolution is not None
                        else None
                    ),
                    recovery_seconds=entry.metadata.get("recovery_seconds"),
                    data_quality=str(entry.metadata.get("data_quality") or "OK"),
                    limitations=self._match_limitations(item, component_id),
                )
            )
        return RetrievalResult(
            query=query,
            matches=matches,
            total_candidates=total_candidates,
            summary=self._summarise(matches),
        )

    def _match_limitations(
        self, item: RankedExperience, component_id: Optional[uuid.UUID]
    ) -> list[str]:
        notes: list[str] = []
        entry = item.experience
        if entry.metadata.get("data_quality") == "POOR":
            notes.append("this episode's own data was flagged low quality")
        elif entry.metadata.get("data_quality") == "LIMITED":
            notes.append("this episode's data was partially limited")
        if component_id is not None and entry.component_id != str(component_id):
            notes.append("episode is from a different component")
        if item.explanation.right_only:
            differing = {match.feature_class for match in item.explanation.right_only}
            if differing:
                notes.append(
                    "historical case had features this one does not: "
                    + ", ".join(sorted(differing))
                )
        return notes

    def _summarise(self, matches: Sequence[RetrievalMatch]) -> str:
        """The §49 sentence, or a truthful description of what was found."""
        if not matches:
            return NO_HISTORY_MESSAGE
        successes = sum(1 for match in matches if _is_success(match))
        smallest = min(len(matches), 1)
        if len(matches) < self.settings.INTELLIGENCE_MIN_EFFECTIVENESS_SAMPLES:
            return (
                f"{len(matches)} comparable historical episode(s) found "
                f"({successes} successful) — too few to describe a pattern."
            )
        if smallest and successes == len(matches):
            return (
                f"{len(matches)} comparable historical episodes found; "
                f"all of them recovered after the recorded resolution."
            )
        return (
            f"{len(matches)} comparable historical episodes found; "
            f"{successes} recovered after the recorded resolution and "
            f"{len(matches) - successes} did not."
        )

    async def _incident_context(
        self, session: AsyncSession, incident_id: uuid.UUID
    ) -> dict[str, Optional[uuid.UUID]]:
        from app.models.incident import Incident

        incident = await session.get(Incident, incident_id)
        if incident is None:
            return {}
        return {
            "component_id": incident.primary_component_id,
            "environment_id": incident.environment_id,
        }


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


__all__ = [
    "ExperienceRetrievalService",
    "NO_HISTORY_MESSAGE",
    "RETRIEVAL_LIMITATIONS",
    "RetrievalMatch",
    "RetrievalResult",
]
