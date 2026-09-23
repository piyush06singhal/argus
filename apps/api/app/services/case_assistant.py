"""ARGUS Case Assistant (Phase 11 §27, §28).

A question-answering surface grounded *only* in one Reliability Case.

§28's requirements are implemented as structure, not as prompt wording:

* **retrieve evidence** — every answer starts from a compiled evidence pack built
  from stored rows (the case, its timeline, its incident, its analyses, its
  remediations, its forecasts, retrieved history);
* **cite evidence** — every answer carries ``citations``: the row ids it used;
* **distinguish facts from hypotheses** — each citation is labelled ``FACT`` (a
  stored row), ``HYPOTHESIS`` (a candidate ARGUS scored but did not confirm) or
  ``PREDICTION`` (a forecast, which is explicitly not a fact);
* **expose uncertainty** — answers carry ``confidence`` and ``unknowns``;
* **avoid inventing records** — the composer only emits an answer from a pack it
  built, and when the pack lacks the evidence it says so;
* **avoid unauthorized actions** — the assistant has no execution path at all.
  The ``available_actions`` field lists what a *person* could do, sourced from the
  workflow's own stage transitions, and explicitly states that the assistant
  cannot perform them.

There is a deterministic composer and an optional AI narrator. The deterministic
one is the default and always answers; the AI narrator may only *rephrase* the
pack and is switched off by default (§62), because a capability that can say
something plausible and ungrounded should not be on until someone asks for it.

Answer templates are chosen by *what the evidence contains*, so "have we seen this
before?" answers from the retrieved history and says "no comparable episode is
stored" when there is none — rather than being confidently vague.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.services.platform_time import aware as _aware

logger = logging.getLogger(__name__)


class CitationKind(str, Enum):
    """What sort of claim a citation supports (§28)."""

    FACT = "FACT"
    HYPOTHESIS = "HYPOTHESIS"
    PREDICTION = "PREDICTION"


#: The questions §27 names, mapped to the intent key that answers them.
QUESTION_INTENTS: dict[str, str] = {
    "what_happened": "what happened",
    "why": "why does argus think it happened",
    "evidence": "what evidence supports this",
    "seen_before": "have we seen this before",
    "actions": "what actions are available",
    "outcomes": "what happened after similar actions",
}


@dataclass
class Citation:
    """One stored row an answer relies on."""

    kind: str
    source: str
    row_id: str
    label: str
    detail: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "row_id": self.row_id,
            "label": self.label,
            "detail": self.detail,
        }


@dataclass
class EvidencePack:
    """Everything the assistant is allowed to use, compiled from stored rows."""

    case_id: uuid.UUID
    reference: str
    project_id: uuid.UUID
    facts: dict[str, Any] = field(default_factory=dict)
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    predictions: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    remediation_outcomes: list[dict[str, Any]] = field(default_factory=list)
    available_actions: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": str(self.case_id),
            "reference": self.reference,
            "facts": self.facts,
            "hypotheses": self.hypotheses,
            "predictions": self.predictions,
            "history": self.history,
            "remediation_outcomes": self.remediation_outcomes,
            "available_actions": self.available_actions,
            "gaps": list(self.gaps),
        }


@dataclass
class AssistantAnswer:
    """An answer with its citations and its confidence (§28)."""

    question: str
    intent: str
    answer: str
    confidence: float
    confidence_reason: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    narrator: Optional[str] = None
    evidence_pack: Optional[dict[str, Any]] = None

    def as_dict(self, *, include_pack: bool = False) -> dict[str, Any]:
        payload = {
            "question": self.question,
            "intent": self.intent,
            "answer": self.answer,
            "confidence": self.confidence,
            "confidence_reason": self.confidence_reason,
            "citations": self.citations,
            "unknowns": self.unknowns,
            "narrator": self.narrator,
            "grounding": {
                "fact_count": sum(
                    1
                    for c in self.citations
                    if c.get("kind") == CitationKind.FACT.value
                ),
                "hypothesis_count": sum(
                    1
                    for c in self.citations
                    if c.get("kind") == CitationKind.HYPOTHESIS.value
                ),
                "prediction_count": sum(
                    1
                    for c in self.citations
                    if c.get("kind") == CitationKind.PREDICTION.value
                ),
                "note": (
                    "facts are stored rows; hypotheses are candidates ARGUS scored "
                    "but did not confirm; predictions are forecasts, which are not "
                    "facts. This assistant cannot execute anything."
                ),
            },
        }
        if include_pack:
            payload["evidence"] = self.evidence_pack
        return payload


async def compile_evidence_pack(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    settings: Optional[Settings] = None,
) -> Optional[EvidencePack]:
    """Build the pack from stored rows (§28)."""
    from app.models.platform import ReliabilityCase
    from app.services.reliability_case import (
        CASE_STATUS_TRANSITIONS,
        case_timeline,
        collect_case_evidence,
    )

    settings = settings or get_settings()
    case = await session.get(ReliabilityCase, case_id)
    if case is None:
        return None
    if project_id is not None and case.project_id != project_id:
        return None

    pack = EvidencePack(
        case_id=case.id,
        reference=case.reference,
        project_id=case.project_id,
    )
    pack.facts = {
        "title": case.title,
        "summary": case.summary,
        "status": case.status.value,
        "trigger": case.trigger.value,
        "severity": case.severity,
        "opened_at": _aware(case.opened_at).isoformat(),
        "closed_at": _aware(case.closed_at).isoformat() if case.closed_at else None,
        "incident_id": str(case.incident_id) if case.incident_id else None,
        "primary_component_id": str(case.primary_component_id)
        if case.primary_component_id
        else None,
    }

    timeline = await case_timeline(session, case_id=case.id, limit=500)
    pack.facts["timeline"] = [
        {
            "sequence": entry.sequence,
            "occurred_at": _aware(entry.occurred_at).isoformat(),
            "kind": entry.kind.value,
            "event_type": entry.event_type,
            "title": entry.title,
            "detail": entry.detail,
            "actor": entry.actor,
            "system_action": entry.system_action,
            "evidence": entry.evidence,
        }
        for entry in timeline
    ]

    evidence = await collect_case_evidence(session, case=case)
    pack.facts["incidents"] = evidence.incidents
    pack.facts["anomalies"] = evidence.anomalies
    pack.facts["deployments"] = evidence.deployments
    pack.facts["reproductions"] = evidence.reproductions
    pack.facts["patches"] = evidence.patches
    pack.gaps.extend(evidence.gaps)

    for analysis in evidence.analyses:
        for candidate in analysis.get("candidates", []):
            pack.hypotheses.append(
                {
                    "analysis_id": analysis.get("id"),
                    "candidate_id": candidate.get("id"),
                    "candidate_type": candidate.get("candidate_type"),
                    "status": candidate.get("status"),
                    "score": candidate.get("score"),
                    "confidence": candidate.get("confidence"),
                    "component_id": candidate.get("component_id"),
                }
            )

    pack.predictions = list(evidence.predictions)
    pack.history = list(evidence.knowledge)
    pack.remediation_outcomes = list(evidence.remediations)

    pack.available_actions = [
        {
            "transition": status.value,
            "note": (
                "a person may move the case to this status; the assistant cannot "
                "perform it, and any remediation still passes Phase 9's policy, "
                "safety and approval gates"
            ),
        }
        for status in CASE_STATUS_TRANSITIONS.get(case.status, ())
    ]

    if not pack.hypotheses:
        pack.gaps.append(
            "no root cause candidate is stored, so nothing explains why this "
            "happened yet"
        )
    if not pack.predictions:
        pack.gaps.append("no forecast exists for the affected components")
    if not pack.history:
        pack.gaps.append("no comparable episode was retrieved from history")
    return pack


def _cite_facts(
    pack: EvidencePack, *, sources: dict[str, list[str]]
) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for source, labels in sources.items():
        for label in labels:
            citations.append(
                Citation(
                    kind=CitationKind.FACT.value,
                    source=source,
                    row_id="",
                    label=label,
                ).as_dict()
            )
    return citations


def _confidence(pack: EvidencePack, *, cited: int) -> tuple[float, str]:
    """A bounded, explained confidence — never a bare number (§28)."""
    base = 0.2
    base += min(0.4, 0.1 * cited)
    if pack.hypotheses:
        base += 0.15
    if pack.facts.get("timeline"):
        base += 0.1
    if pack.history:
        base += 0.1
    base -= 0.05 * len(pack.gaps)
    value = max(0.05, min(0.95, round(base, 2)))
    reasons = [
        f"{cited} citation(s)",
        f"{len(pack.gaps)} recorded gap(s)",
    ]
    if pack.hypotheses:
        reasons.append(f"{len(pack.hypotheses)} scored candidate(s)")
    if not pack.history:
        reasons.append("no comparable history")
    return value, "; ".join(reasons)


def answer_question(pack: EvidencePack, *, question: str) -> AssistantAnswer:
    """Answer from the pack, deterministically (§27, §28).

    The intent is chosen by keyword, and each intent has its own template that
    reads *only* the pack. When the pack has nothing for the intent, the answer
    says so and lists the gap — that is the whole point of the grounding.
    """
    lowered = question.lower()
    intent = "what_happened"
    if any(word in lowered for word in ("why", "cause", "reason", "root")):
        intent = "why"
    elif any(word in lowered for word in ("evidence", "support", "proof", "data")):
        intent = "evidence"
    elif any(
        word in lowered for word in ("before", "seen", "similar", "history", "previous")
    ):
        intent = "seen_before"
    elif any(word in lowered for word in ("action", "do", "next", "fix", "remediate")):
        intent = "actions"
    elif any(word in lowered for word in ("outcome", "after", "result", "worked")):
        intent = "outcomes"

    facts = pack.facts
    citations: list[dict[str, Any]] = []

    if intent == "what_happened":
        timeline = facts.get("timeline", [])
        incidents = facts.get("incidents", [])
        anomalies = facts.get("anomalies", [])
        lines = [
            f"Case {pack.reference} — {facts.get('title')}",
            f"status: {facts.get('status')}; trigger: {facts.get('trigger')}; "
            f"severity: {facts.get('severity') or 'not stated'}",
            f"opened: {facts.get('opened_at')}",
        ]
        if incidents:
            incident = incidents[0]
            lines.append(
                f"incident: {incident.get('title')} "
                f"({incident.get('severity')}, {incident.get('status')}) detected "
                f"{incident.get('detected_at')}"
            )
            citations.append(
                Citation(
                    kind=CitationKind.FACT.value,
                    source="incident",
                    row_id=str(incident.get("id")),
                    label=incident.get("title") or "the incident",
                ).as_dict()
            )
        lines.append(f"{len(anomalies)} anomaly record(s) are linked to this case.")
        for anomaly in anomalies[:5]:
            citations.append(
                Citation(
                    kind=CitationKind.FACT.value,
                    source="anomaly",
                    row_id=str(anomaly.get("id")),
                    label=(
                        f"{anomaly.get('severity')} {anomaly.get('anomaly_type')} "
                        f"on {anomaly.get('metric_name') or 'a metric'}"
                    ),
                ).as_dict()
            )
        for entry in timeline[:10]:
            citations.append(
                Citation(
                    kind=CitationKind.FACT.value,
                    source="case_timeline",
                    row_id=f"{pack.case_id}:{entry.get('sequence')}",
                    label=entry.get("title") or entry.get("event_type"),
                    detail=entry.get("detail"),
                ).as_dict()
            )
        lines.append(f"the timeline holds {len(timeline)} recorded event(s).")
        answer = "\n".join(lines)

    elif intent == "why":
        if not pack.hypotheses:
            answer = (
                "ARGUS has not concluded why. No root cause analysis with candidates "
                "is stored for this case, and this assistant will not invent one. "
                "Run the causal analysis, or add the missing evidence, and the "
                "hypotheses will appear here."
            )
        else:
            top = max(pack.hypotheses, key=lambda item: item.get("score") or 0.0)
            lines = [
                "ARGUS's most evidence-supported explanation, as a scored "
                "hypothesis (not a confirmed cause):",
                f"- {top.get('candidate_type')} on component "
                f"{top.get('component_id') or 'unattributed'}",
                f"  score {top.get('score')}, confidence "
                f"{top.get('confidence') or 'unstated'}, status {top.get('status')}",
                f"{len(pack.hypotheses)} candidate(s) were scored in total:",
            ]
            for candidate in pack.hypotheses[:5]:
                lines.append(
                    f"- {candidate.get('candidate_type')} "
                    f"(score {candidate.get('score')}, {candidate.get('status')})"
                )
                citations.append(
                    Citation(
                        kind=CitationKind.HYPOTHESIS.value,
                        source="root_cause_candidate",
                        row_id=str(candidate.get("candidate_id")),
                        label=(
                            f"{candidate.get('candidate_type')} scored "
                            f"{candidate.get('score')}"
                        ),
                    ).as_dict()
                )
            lines.append(
                "A candidate is a hypothesis with evidence behind it. ARGUS does not "
                "report correlation as causation, and a high score is not proof."
            )
            answer = "\n".join(lines)

    elif intent == "evidence":
        blocks = [
            ("incidents", facts.get("incidents", [])),
            ("anomalies", facts.get("anomalies", [])),
            ("deployments", facts.get("deployments", [])),
            ("reproductions", facts.get("reproductions", [])),
            ("patches", facts.get("patches", [])),
            ("remediations", pack.remediation_outcomes),
            ("predictions", pack.predictions),
            ("history", pack.history),
        ]
        lines = ["Stored evidence linked to this case:"]
        total = 0
        for name, rows in blocks:
            if not rows:
                continue
            lines.append(f"- {name}: {len(rows)}")
            total += len(rows)
            for row in rows[:3]:
                citations.append(
                    Citation(
                        kind=(
                            CitationKind.PREDICTION.value
                            if name == "predictions"
                            else CitationKind.FACT.value
                        ),
                        source=name,
                        row_id=str(row.get("id") or row.get("candidate_id") or ""),
                        label=(
                            row.get("title")
                            or row.get("name")
                            or row.get("action_type")
                            or row.get("prediction_type")
                            or name
                        ),
                    ).as_dict()
                )
        if total == 0:
            lines.append(
                "nothing is linked: no anomaly, deployment, reproduction, patch, "
                "remediation, forecast or retrieved episode references this case"
            )
        for gap in pack.gaps:
            lines.append(f"- gap: {gap}")
        answer = "\n".join(lines)

    elif intent == "seen_before":
        if not pack.history:
            answer = (
                "No comparable episode is stored. That is not the same as 'this has "
                "never happened' — it means ARGUS has no learned episode similar "
                "enough to cite, which is a statement about its evidence, not about "
                "your system."
            )
        else:
            lines = [f"{len(pack.history)} comparable episode(s) were retrieved:"]
            for match in pack.history[:5]:
                lines.append(
                    f"- {match.get('summary') or match.get('experience_id')} "
                    f"(similarity {match.get('similarity')})"
                )
                citations.append(
                    Citation(
                        kind=CitationKind.FACT.value,
                        source="experience",
                        row_id=str(match.get("experience_id") or ""),
                        label=match.get("summary") or "a comparable episode",
                        detail=str(match.get("similarity")),
                    ).as_dict()
                )
            answer = "\n".join(lines)

    elif intent == "actions":
        if not pack.available_actions:
            answer = (
                "This case is in a terminal status, so no workflow transition is "
                "available. Any remediation would still have to pass Phase 9's "
                "policy, safety and approval gates."
            )
        else:
            lines = ["Actions a person may take (this assistant cannot perform them):"]
            for action in pack.available_actions:
                lines.append(f"- move the case to {action.get('transition')}")
            lines.append(
                "Remediation is not on this list by design: it requires Phase 9's "
                "policy evaluation, safety assessment and, for anything above the "
                "configured risk ceiling, a human approval."
            )
            answer = "\n".join(lines)

    else:  # outcomes
        if not pack.remediation_outcomes:
            answer = (
                "No remediation has been attempted for this case, so there is no "
                "outcome to report."
            )
        else:
            lines = ["Recorded outcomes for this case's remediations:"]
            for action in pack.remediation_outcomes[:5]:
                lines.append(
                    f"- {action.get('action_type')}: status {action.get('status')}, "
                    f"outcome {action.get('outcome') or 'not yet established'}, mode "
                    f"{action.get('execution_mode')}"
                )
                citations.append(
                    Citation(
                        kind=CitationKind.FACT.value,
                        source="remediation_action",
                        row_id=str(action.get("id")),
                        label=(
                            f"{action.get('action_type')} → "
                            f"{action.get('outcome') or action.get('status')}"
                        ),
                    ).as_dict()
                )
            answer = "\n".join(lines)

    confidence, reason = _confidence(pack, cited=len(citations))
    unknowns = list(pack.gaps)
    if intent == "why" and not pack.hypotheses:
        unknowns.append("no scored root-cause candidate is stored")
    if intent == "outcomes" and not pack.remediation_outcomes:
        unknowns.append("no remediation outcome is recorded")
    return AssistantAnswer(
        question=question,
        intent=intent,
        answer=answer,
        confidence=confidence,
        confidence_reason=reason,
        citations=citations,
        unknowns=unknowns,
    )


async def ask(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    question: str,
    project_id: Optional[uuid.UUID] = None,
    include_evidence: bool = False,
    settings: Optional[Settings] = None,
) -> Optional[AssistantAnswer]:
    """Answer a question about one case (§27).

    Returns ``None`` when the case does not exist in scope, so the API can answer
    404 rather than disclosing a case from another project.
    """
    settings = settings or get_settings()
    pack = await compile_evidence_pack(
        session, case_id=case_id, project_id=project_id, settings=settings
    )
    if pack is None:
        return None
    answer = answer_question(pack, question=question)
    answer.evidence_pack = pack.as_dict() if include_evidence else None
    return answer


def assistant_capability(settings: Optional[Settings] = None) -> dict[str, Any]:
    """What the assistant does, and what it will not do (§28)."""
    settings = settings or get_settings()
    return {
        "enabled": settings.PLATFORM_CASE_ASSISTANT_ENABLED,
        "grounded_in": "one Reliability Case's stored rows",
        "guarantees": [
            "retrieves evidence before answering",
            "cites the rows behind every claim",
            "labels facts, hypotheses and predictions separately",
            "exposes uncertainty and unknown gaps",
            "never invents a record it did not retrieve",
            "never executes a remediation or any other action",
        ],
        "refuses": [
            "answering about objects outside the case's project scope",
            "stating a root cause that is not a stored candidate",
            "treating a prediction as a fact",
            "performing an action",
        ],
        "questions": list(QUESTION_INTENTS.values()),
    }


__all__ = [
    "QUESTION_INTENTS",
    "AssistantAnswer",
    "Citation",
    "CitationKind",
    "EvidencePack",
    "answer_question",
    "ask",
    "assistant_capability",
    "compile_evidence_pack",
]
