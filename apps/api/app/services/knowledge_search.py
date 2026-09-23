"""ARGUS Grounded Knowledge Search (Phase 10 §46–§50).

Answers questions about history from stored rows, with citations.

The pipeline §48 describes is implemented literally:

```text
question → intent → structured retrieval → evidence selection → answer
                                     ↓
                          citation validation (§50)
```

Two refusals make it trustworthy:

* **No fabrication.** The answer text is *assembled* from retrieved facts, not
  generated. Every sentence is a template over rows that exist, and a question
  with no matching evidence returns the literal
  ``No comparable historical case was found.`` (§49) rather than a plausible
  guess. There is a place for an LLM in front of this — but it reads
  :meth:`KnowledgeSearchService.search` output, it does not replace it.
* **Every citation is verified.** :func:`verify_citations` resolves each id back
  to its table before the answer is returned, and an id that does not resolve is
  dropped and reported. That check is what makes "the AI must not invent
  historical evidence" a property of the code rather than a hope about a prompt.

Intent detection is deliberately simple keyword matching. A model that guessed
the wrong intent would silently answer a different question; a keyword table that
misses falls back to plain knowledge search, which is honest about what it did.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.incident import Incident
from app.models.intelligence import (
    KnowledgeStatus,
    KnowledgeType,
    ReliabilityExperience,
    ReliabilityKnowledge,
)
from app.models.remediation import RemediationAction
from app.services.experience_retrieval import (
    ExperienceRetrievalService,
    NO_HISTORY_MESSAGE,
    RetrievalResult,
)
from app.services.remediation_effectiveness import (
    action_effectiveness,
)

logger = logging.getLogger(__name__)

#: Keyword → intent. Order matters: the first match wins, so the more specific
#: phrasings come first.
INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "regression",
        ("regression", "regressed", "broke after", "caused a bug", "rollback"),
    ),
    (
        "remediation",
        (
            "what fixed",
            "how do we fix",
            "resolve",
            "resolved",
            "remediation",
            "restart",
        ),
    ),
    (
        "deployment",
        ("deploy", "deployment", "release", "change"),
    ),
    (
        "component",
        ("which component", "components", "chronic", "unreliable"),
    ),
    (
        "prediction",
        ("forecast", "predict", "risk"),
    ),
    (
        "recurrence",
        (
            "seen this",
            "seen before",
            "happened before",
            "similar",
            "recurring",
            "again",
        ),
    ),
)

#: The framing every answer carries, including — especially — the ones that
#: found nothing. "No comparable case" is a claim too, and it is the one most
#: likely to be read as "nothing like this ever happened" (§49).
BASELINE_LIMITATIONS: tuple[str, ...] = (
    "This answer is built only from records stored in this project.",
    "A missing result means no comparable record was found, not that nothing happened.",
)

#: The citation table a ``type`` maps onto, for :func:`verify_citations`.
_CITATION_MODELS: dict[str, Any] = {}


def detect_intent(question: str) -> str:
    """Classify a question into one of the supported intents."""
    text = (question or "").lower()
    for intent, keywords in INTENT_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return intent
    return "knowledge"


@dataclass
class SearchCitation:
    """One row the answer stands on (§50)."""

    type: str
    id: str
    label: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "id": self.id, "label": self.label}


@dataclass
class SearchAnswer:
    """The answer to a historical question."""

    question: str
    intent: str
    answer: str
    evidence_available: bool
    citations: list[SearchCitation] = field(default_factory=list)
    knowledge: list[dict[str, Any]] = field(default_factory=list)
    experiences: list[dict[str, Any]] = field(default_factory=list)
    effectiveness: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "intent": self.intent,
            "answer": self.answer,
            "evidence_available": self.evidence_available,
            "citations": [citation.as_dict() for citation in self.citations],
            "knowledge": list(self.knowledge),
            "experiences": list(self.experiences),
            "effectiveness": list(self.effectiveness),
            "limitations": list(self.limitations),
            "warnings": list(self.warnings),
        }


async def verify_citations(
    session: AsyncSession, citations: Sequence[SearchCitation]
) -> tuple[list[SearchCitation], list[str]]:
    """Resolve every citation id, returning the valid ones and the missing ones.

    A citation that cannot be resolved is *dropped and reported*, never rendered:
    a user following a link to a row that does not exist has been shown a
    fabrication, whether or not a model wrote it.

    The citation map is registered here rather than only inside the search
    service: this is a public helper, and a caller that reached it first would
    otherwise find every citation "missing" and silently drop correct ones.
    """
    _register_citation_models()
    valid: list[SearchCitation] = []
    unresolved: list[str] = []
    for citation in citations:
        found = await _citation_exists(session, citation)
        if found:
            valid.append(citation)
        else:
            unresolved.append(f"{citation.type}:{citation.id}")
    return valid, unresolved


async def _citation_exists(session: AsyncSession, citation: SearchCitation) -> bool:
    try:
        identifier = uuid.UUID(citation.id)
    except (ValueError, AttributeError, TypeError):
        return False

    model = _CITATION_MODELS.get(citation.type)
    if model is None:
        return False
    return (
        await session.scalar(select(model.id).where(model.id == identifier).limit(1))
    ) is not None


def _register_citation_models() -> None:
    """Populate the citation map once, after the models are importable."""
    if _CITATION_MODELS:
        return
    _CITATION_MODELS.update(
        {
            "incident": Incident,
            "experience": ReliabilityExperience,
            "knowledge": ReliabilityKnowledge,
            "remediation": RemediationAction,
        }
    )


class KnowledgeSearchService:
    """Retrieval-backed question answering over ARGUS history."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        retrieval: Optional[ExperienceRetrievalService] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.retrieval = retrieval or ExperienceRetrievalService(self.settings)

    async def search(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        question: str,
        incident_id: Optional[uuid.UUID] = None,
        component_id: Optional[uuid.UUID] = None,
        cutoff: Optional[datetime] = None,
        limit: int = 10,
    ) -> SearchAnswer:
        """Answer a question from stored evidence."""
        _register_citation_models()
        intent = detect_intent(question)
        if intent == "recurrence" and incident_id is not None:
            return await self._answer_recurrence(
                session,
                project_id=project_id,
                question=question,
                incident_id=incident_id,
                cutoff=cutoff,
                limit=limit,
            )
        if intent == "remediation":
            return await self._answer_remediation(
                session,
                project_id=project_id,
                question=question,
                component_id=component_id,
                cutoff=cutoff,
                limit=limit,
            )
        if intent == "regression":
            return await self._answer_by_knowledge_type(
                session,
                project_id=project_id,
                question=question,
                knowledge_type=KnowledgeType.REGRESSION_PATTERN,
                cutoff=cutoff,
                limit=limit,
            )
        if intent == "deployment":
            return await self._answer_by_knowledge_type(
                session,
                project_id=project_id,
                question=question,
                knowledge_type=KnowledgeType.DEPLOYMENT_PATTERN,
                cutoff=cutoff,
                limit=limit,
            )
        if intent == "component":
            return await self._answer_components(
                session,
                project_id=project_id,
                question=question,
                cutoff=cutoff,
                limit=limit,
            )
        if intent == "prediction":
            return await self._answer_by_knowledge_type(
                session,
                project_id=project_id,
                question=question,
                knowledge_type=KnowledgeType.PREDICTIVE_PATTERN,
                cutoff=cutoff,
                limit=limit,
            )
        return await self._answer_by_knowledge_text(
            session,
            project_id=project_id,
            question=question,
            component_id=component_id,
            cutoff=cutoff,
            limit=limit,
        )

    # -- intents ----------------------------------------------------------
    async def _answer_recurrence(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        question: str,
        incident_id: uuid.UUID,
        cutoff: Optional[datetime],
        limit: int,
    ) -> SearchAnswer:
        result = await self.retrieval.retrieve_for_incident(
            session,
            incident_id=incident_id,
            project_id=project_id,
            cutoff=cutoff,
            limit=limit,
        )
        return await self._from_retrieval(
            session, question=question, intent="recurrence", result=result
        )

    async def _answer_remediation(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        question: str,
        component_id: Optional[uuid.UUID],
        cutoff: Optional[datetime],
        limit: int,
    ) -> SearchAnswer:
        buckets = await action_effectiveness(
            session,
            project_id=project_id,
            component_id=component_id,
            cutoff=cutoff,
            settings=self.settings,
        )
        answer = SearchAnswer(
            question=question,
            intent="remediation",
            answer=NO_HISTORY_MESSAGE,
            evidence_available=False,
            limitations=[
                "Effectiveness is a count over comparable episodes, not a guarantee.",
            ],
        )
        if not buckets:
            return await self._finalize(session, answer)

        buckets = sorted(buckets, key=lambda item: -item.comparable)[:limit]
        answer.effectiveness = [bucket.as_dict() for bucket in buckets]
        answer.evidence_available = True
        answer.answer = " ".join(bucket.headline() for bucket in buckets[:3])
        answer.limitations.extend(
            {note for bucket in buckets[:3] for note in bucket.limitations}
        )
        answer.citations = [
            SearchCitation(type="experience", id=experience_id)
            for bucket in buckets[:3]
            for experience_id in bucket.experience_ids[:5]
        ]
        return await self._finalize(session, answer)

    async def _answer_by_knowledge_type(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        question: str,
        knowledge_type: KnowledgeType,
        cutoff: Optional[datetime],
        limit: int,
    ) -> SearchAnswer:
        rows = await self._knowledge_rows(
            session,
            project_id=project_id,
            knowledge_type=knowledge_type,
            cutoff=cutoff,
            limit=limit,
        )
        answer = SearchAnswer(
            question=question,
            intent=_intent_name_for(knowledge_type),
            answer=NO_HISTORY_MESSAGE,
            evidence_available=False,
        )
        if not rows:
            return await self._finalize(session, answer)
        answer.evidence_available = True
        answer.knowledge = [_knowledge_dict(row) for row in rows]
        answer.answer = " ".join(row.description for row in rows[:3])
        answer.citations = [
            SearchCitation(type="knowledge", id=str(row.id), label=row.title)
            for row in rows
        ]
        answer.limitations = [
            "Patterns describe this project's history under the conditions recorded.",
        ]
        return await self._finalize(session, answer)

    async def _answer_components(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        question: str,
        cutoff: Optional[datetime],
        limit: int,
    ) -> SearchAnswer:
        rows = await self._knowledge_rows(
            session,
            project_id=project_id,
            knowledge_type=KnowledgeType.COMPONENT_RELIABILITY_PATTERN,
            cutoff=cutoff,
            limit=limit,
        )
        answer = SearchAnswer(
            question=question,
            intent="component",
            answer=NO_HISTORY_MESSAGE,
            evidence_available=False,
        )
        if not rows:
            return await self._finalize(session, answer)
        answer.evidence_available = True
        answer.knowledge = [_knowledge_dict(row) for row in rows]
        names = ", ".join(row.title for row in rows[:3])
        answer.answer = (
            f"{len(rows)} component reliability pattern(s) are recorded: {names}. "
            f"Each recommends investigation; ARGUS does not modify a component."
        )
        answer.citations = [
            SearchCitation(type="knowledge", id=str(row.id), label=row.title)
            for row in rows
        ]
        return await self._finalize(session, answer)

    async def _answer_by_knowledge_text(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        question: str,
        component_id: Optional[uuid.UUID],
        cutoff: Optional[datetime],
        limit: int,
    ) -> SearchAnswer:
        """Fallback: match knowledge by its own text, and say that is what happened."""
        terms = [term for term in question.lower().split() if len(term) > 3][:6]
        stmt = (
            select(ReliabilityKnowledge)
            .where(ReliabilityKnowledge.project_id == project_id)
            .where(
                ReliabilityKnowledge.status.in_(
                    [
                        KnowledgeStatus.VALIDATED,
                        KnowledgeStatus.ACTIVE,
                        KnowledgeStatus.CANDIDATE,
                    ]
                )
            )
            .limit(limit * 4)
        )
        if component_id is not None:
            stmt = stmt.where(
                or_(
                    ReliabilityKnowledge.component_id == component_id,
                    ReliabilityKnowledge.component_id.is_(None),
                )
            )
        if cutoff is not None:
            stmt = stmt.where(ReliabilityKnowledge.created_at <= cutoff)
        rows = list((await session.scalars(stmt)).all())

        matched = [
            row
            for row in rows
            if not terms
            or any(
                term
                in (
                    row.title + " " + row.description + " " + row.feature_signature
                ).lower()
                for term in terms
            )
        ][:limit]

        answer = SearchAnswer(
            question=question,
            intent="knowledge",
            answer=NO_HISTORY_MESSAGE,
            evidence_available=bool(matched),
            limitations=[
                "This answer matched stored knowledge by keyword; it did not reason about the question.",
            ],
        )
        if not matched:
            return answer
        answer.knowledge = [_knowledge_dict(row) for row in matched]
        answer.answer = " ".join(row.description for row in matched[:3])
        answer.citations = [
            SearchCitation(type="knowledge", id=str(row.id), label=row.title)
            for row in matched
        ]
        return await self._finalize(session, answer)

    # -- helpers ----------------------------------------------------------
    async def _from_retrieval(
        self,
        session: AsyncSession,
        *,
        question: str,
        intent: str,
        result: RetrievalResult,
    ) -> SearchAnswer:
        answer = SearchAnswer(
            question=question,
            intent=intent,
            answer=result.summary,
            evidence_available=result.evidence_available,
            limitations=list(result.limitations),
        )
        if not result.matches:
            return await self._finalize(session, answer)

        answer.experiences = [match.as_dict() for match in result.matches]
        actions: dict[str, int] = {}
        successes: dict[str, int] = {}
        for match in result.matches:
            for action in (match.resolution or {}).get("action_types") or []:
                actions[action] = actions.get(action, 0) + 1
                if match.resolution and match.resolution.get("outcome") == "effective":
                    successes[action] = successes.get(action, 0) + 1
        if actions:
            top = max(actions.items(), key=lambda item: item[1])[0]
            answer.answer = (
                f"{len(result.matches)} comparable historical episode(s); "
                f"{successes.get(top, 0)} of the {actions[top]} where {top.upper()} was used "
                f"recovered. Sample size: {len(result.matches)}."
            )
        answer.limitations.append("Similarity is signature-based, not causal.")
        answer.citations = [
            SearchCitation(
                type="experience",
                id=match.experience_id,
                label=f"similarity {match.similarity:.2f}",
            )
            for match in result.matches
        ] + [
            SearchCitation(type="incident", id=match.incident_id)
            for match in result.matches
            if match.incident_id
        ]
        return await self._finalize(session, answer)

    async def _knowledge_rows(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        knowledge_type: KnowledgeType,
        cutoff: Optional[datetime],
        limit: int,
    ) -> list[ReliabilityKnowledge]:
        """Knowledge of one type, excluding retired rows and anything from the future."""
        stmt = (
            select(ReliabilityKnowledge)
            .where(ReliabilityKnowledge.project_id == project_id)
            .where(ReliabilityKnowledge.knowledge_type == knowledge_type)
            .where(
                ReliabilityKnowledge.status.in_(
                    [
                        KnowledgeStatus.VALIDATED,
                        KnowledgeStatus.ACTIVE,
                        KnowledgeStatus.CANDIDATE,
                    ]
                )
            )
            .order_by(ReliabilityKnowledge.sample_count.desc())
            .limit(limit)
        )
        if cutoff is not None:
            stmt = stmt.where(ReliabilityKnowledge.created_at <= cutoff)
        return list((await session.scalars(stmt)).all())

    async def _finalize(
        self, session: AsyncSession, answer: SearchAnswer
    ) -> SearchAnswer:
        """Verify citations and attach the framing, before the answer escapes (§50)."""
        answer.limitations = _framed(answer.limitations)
        valid, unresolved = await verify_citations(session, answer.citations)
        answer.citations = valid
        if unresolved:
            answer.warnings.append(
                "Dropped unresolvable citation(s): " + ", ".join(unresolved)
            )
        if not valid and answer.evidence_available:
            #: Everything it cited was invented or deleted. The honest answer is
            #: that there is no evidence, not the unverified text.
            answer.evidence_available = False
            answer.answer = NO_HISTORY_MESSAGE
            answer.knowledge = []
            answer.experiences = []
            answer.effectiveness = []
        return answer


def _framed(limitations: Sequence[str]) -> list[str]:
    """The answer's own notes plus the baseline framing, in that order."""
    notes = list(BASELINE_LIMITATIONS)
    for note in limitations:
        if note and note not in notes:
            notes.append(note)
    return notes


def _knowledge_dict(row: ReliabilityKnowledge) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "knowledge_type": row.knowledge_type.value,
        "status": row.status.value,
        "title": row.title,
        "description": row.description,
        "sample_count": row.sample_count,
        "success_count": row.success_count,
        "confidence": row.confidence.value,
        "support_strength": row.support_strength,
        "scope": row.scope.value,
        "algorithm": row.algorithm,
        "version": row.version,
        "coverage_start": row.coverage_start.isoformat()
        if row.coverage_start
        else None,
        "coverage_end": row.coverage_end.isoformat() if row.coverage_end else None,
        "limitations": list(row.limitations or []),
        "experience_ids": list(row.experience_ids or []),
        "sources": list(row.sources or []),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _intent_name_for(knowledge_type: KnowledgeType) -> str:
    return {
        KnowledgeType.REGRESSION_PATTERN: "regression",
        KnowledgeType.DEPLOYMENT_PATTERN: "deployment",
        KnowledgeType.PREDICTIVE_PATTERN: "prediction",
        KnowledgeType.COMPONENT_RELIABILITY_PATTERN: "component",
        KnowledgeType.REMEDIATION_PATTERN: "remediation",
    }.get(knowledge_type, "knowledge")


__all__ = [
    "INTENT_KEYWORDS",
    "KnowledgeSearchService",
    "NO_HISTORY_MESSAGE",
    "SearchAnswer",
    "SearchCitation",
    "detect_intent",
    "verify_citations",
]
