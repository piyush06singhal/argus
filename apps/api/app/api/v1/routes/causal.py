"""ARGUS Causal Analysis Routes (Phase 4 §34, §35, §37, §43, §45).

The investigation surface for root-cause analysis:

```text
POST /incidents/{id}/analyze                      run (or reuse) an analysis
GET  /incidents/{id}/causal-analysis              latest analysis + detail
GET  /incidents/{id}/causal-analysis/history      versioned audit trail (§43)
GET  /incidents/{id}/causal-analysis/{aid}        one specific version
GET  /incidents/{id}/causal-analysis/{aid}/explanation   structured reasons (§37)
GET  /incidents/{id}/root-causes                  ranked candidates
GET  /incidents/{id}/causal-graph                 nodes + edges (§40)
GET  /incidents/{id}/causal-chain                 evidence-backed chain (§32)
GET  /incidents/{id}/hypotheses                   alternatives + evidence (§28)
GET  /incidents/{id}/evidence-analysis            supporting vs contradicting (§24)
GET  /incidents/{id}/relationships/{rid}/explanation  why this edge exists (§41)
```

Isolation is enforced in SQL on every query: the incident is resolved through
the caller's scope, and analyses/evidence are filtered by ``project_id`` *and*
``incident_id``. An out-of-scope or unknown id is a 404, never data (§45).
"""

from __future__ import annotations

import uuid
from typing import Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_incident
from app.models.system import SystemComponent
from app.core.database import get_db
from app.models.causal import (
    CausalAnalysis,
    CausalEvidence,
    CausalRelationship,
    EvidencePolarity,
    RootCauseCandidate,
)
from app.schemas.causal import (
    AnalysisHistoryItem,
    AnalysisHistoryResponse,
    AnalyzeRequest,
    AnalyzeResponse,
    CausalAnalysisDetailResponse,
    CausalChainLinkResponse,
    CausalChainResponse,
    CausalEvidenceResponse,
    CausalGraphResponse,
    CausalRelationshipResponse,
    EvidenceAnalysisResponse,
    HypothesesResponse,
    HypothesisResponse,
    RootCauseCandidateList,
    RootCauseCandidateResponse,
)
from app.services.causal_analysis_service import CausalAnalysisService
from app.services.causal_explanation import (
    CAUSAL_DISCLAIMER,
    CausalExplanationService,
    LoadedAnalysis,
)

router = APIRouter(prefix="/incidents", tags=["causal-analysis"])

#: Nested collections are bounded — an unbounded evidence dump is not an API.
_MAX_ITEMS = 200
_MAX_HISTORY = 50


def _assert_analysis_scope(
    analysis: CausalAnalysis, *, incident_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """Defence in depth: the row must match the incident and the project."""
    if analysis.incident_id != incident_id or analysis.project_id != project_id:
        raise HTTPException(status_code=404, detail="Causal analysis not found")


async def _resolve_analysis(
    db: AsyncSession,
    *,
    incident_id: uuid.UUID,
    project_id: uuid.UUID,
    analysis_id: Optional[uuid.UUID] = None,
) -> CausalAnalysis:
    """Latest (or requested) analysis, 404 when it does not exist in scope."""
    service = CausalAnalysisService(db)
    if analysis_id is not None:
        from sqlalchemy import select

        analysis = (
            (
                await db.execute(
                    select(CausalAnalysis).where(
                        CausalAnalysis.id == analysis_id,
                        CausalAnalysis.project_id == project_id,
                        CausalAnalysis.incident_id == incident_id,
                    )
                )
            )
            .scalars()
            .first()
        )
    else:
        analysis = await service.latest_analysis(
            project_id=project_id, incident_id=incident_id
        )
    if analysis is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "No causal analysis exists for this incident yet. "
                f"POST /api/v1/incidents/{incident_id}/analyze to run one."
            ),
        )
    _assert_analysis_scope(analysis, incident_id=incident_id, project_id=project_id)
    return analysis


async def _load_full(
    db: AsyncSession,
    *,
    incident_id: uuid.UUID,
    project_id: uuid.UUID,
    analysis_id: Optional[uuid.UUID] = None,
) -> tuple[CausalAnalysis, LoadedAnalysis]:
    analysis = await _resolve_analysis(
        db, incident_id=incident_id, project_id=project_id, analysis_id=analysis_id
    )
    loaded = await CausalExplanationService(db).load_analysis(
        project_id=project_id, incident_id=incident_id, analysis_id=analysis.id
    )
    if loaded is None:  # pragma: no cover - resolved above, kept for safety
        raise HTTPException(status_code=404, detail="Causal analysis not found")
    return analysis, loaded


def _evidence_response(item: CausalEvidence) -> CausalEvidenceResponse:
    return CausalEvidenceResponse.model_validate(item)


async def _component_names(
    db: AsyncSession, candidates: Sequence[RootCauseCandidate]
) -> dict[uuid.UUID, str]:
    """Resolve candidate components to their names for every response.

    A candidate row stores ``component_id``, not a name — but "Database" reads
    very differently from ``30000000-…-a006``, and an unlabelled hypothesis is
    not explainable. Names are resolved here rather than denormalised into the
    analysis so a rename is reflected immediately.
    """
    component_ids = {
        candidate.component_id for candidate in candidates if candidate.component_id
    }
    if not component_ids:
        return {}
    rows = (
        await db.execute(
            select(SystemComponent.id, SystemComponent.name).where(
                SystemComponent.id.in_(component_ids)
            )
        )
    ).all()
    return {row.id: row.name for row in rows}


def _candidate_response(
    candidate: RootCauseCandidate, names: dict[uuid.UUID, str]
) -> RootCauseCandidateResponse:
    """Candidate payload with its component name resolved."""
    payload = RootCauseCandidateResponse.model_validate(candidate)
    if candidate.component_id is not None:
        payload.component_name = names.get(candidate.component_id)
    return payload


def _split_evidence(
    loaded: LoadedAnalysis, candidate_id: uuid.UUID
) -> tuple[list[CausalEvidence], list[CausalEvidence], list[CausalEvidence]]:
    supporting: list[CausalEvidence] = []
    contradicting: list[CausalEvidence] = []
    neutral: list[CausalEvidence] = []
    for item in loaded.evidence_for(candidate_id):
        if item.polarity is EvidencePolarity.SUPPORTING:
            supporting.append(item)
        elif item.polarity is EvidencePolarity.CONTRADICTING:
            contradicting.append(item)
        else:
            neutral.append(item)
    return supporting, contradicting, neutral


def _hypothesis(
    loaded: LoadedAnalysis,
    candidate: RootCauseCandidate,
    names: dict[uuid.UUID, str],
) -> HypothesisResponse:
    supporting, contradicting, neutral = _split_evidence(loaded, candidate.id)
    reason = CausalExplanationService.confidence_reason(candidate)
    categories = sorted({item.category.value for item in supporting})
    why = f"{candidate.confidence.value}: {reason}" + (
        f" (categories: {', '.join(categories)})" if categories else ""
    )
    return HypothesisResponse(
        candidate=_candidate_response(candidate, names),
        supporting=[_evidence_response(item) for item in supporting],
        contradicting=[_evidence_response(item) for item in contradicting],
        neutral=[_evidence_response(item) for item in neutral],
        why_confidence_differs=why,
    )


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------
@router.post("/{incident_id}/analyze", response_model=AnalyzeResponse)
async def analyze_incident(
    incident_id: uuid.UUID,
    payload: Optional[AnalyzeRequest] = None,
    project_id: Optional[uuid.UUID] = Query(
        None, description="Scope the incident to a project (recommended)"
    ),
    environment_id: Optional[uuid.UUID] = Query(
        None, description="Additional environment scope"
    ),
    force: bool = Query(
        False,
        description=(
            "Re-run even when the stored analysis already covers the current "
            "evidence. Without it, an up-to-date analysis is returned as-is."
        ),
    ),
    db: AsyncSession = Depends(get_db),
) -> AnalyzeResponse:
    """Run (or reuse) the causal analysis for an incident (§34, §35).

    Idempotent by design: an unchanged evidence set returns the existing
    version instead of appending a duplicate. New evidence creates a new
    version and preserves the previous one — history is never overwritten.
    """
    incident = await require_incident(
        db, incident_id, project_id=project_id, environment_id=environment_id
    )
    trigger = (payload.trigger if payload else None) or "manual"
    requested_by = payload.requested_by if payload else None
    service = CausalAnalysisService(db)
    try:
        outcome = await service.analyze_incident(
            project_id=incident.project_id,
            incident=incident,
            trigger=trigger,
            requested_by=requested_by,
            force=force,
        )
    except RuntimeError as exc:  # CAUSAL_ANALYSIS_ENABLED=false
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(outcome.analysis)
    return AnalyzeResponse(
        analysis_id=outcome.analysis.id,
        incident_id=outcome.analysis.incident_id,
        analysis_version=outcome.analysis.analysis_version,
        status=outcome.analysis.status,
        reused=not outcome.created,
    )


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
@router.get(
    "/{incident_id}/causal-analysis", response_model=CausalAnalysisDetailResponse
)
async def get_causal_analysis(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    analysis_id: Optional[uuid.UUID] = Query(
        None, description="A specific analysis version (defaults to the latest)"
    ),
    db: AsyncSession = Depends(get_db),
) -> CausalAnalysisDetailResponse:
    """Latest (or requested) analysis with its candidates, edges and evidence."""
    incident = await require_incident(
        db, incident_id, project_id=project_id, environment_id=environment_id
    )
    analysis, loaded = await _load_full(
        db,
        incident_id=incident_id,
        project_id=incident.project_id,
        analysis_id=analysis_id,
    )
    payload = CausalAnalysisDetailResponse.model_validate(analysis)
    ordered = sorted(loaded.candidates, key=lambda c: (-c.score, str(c.id)))
    names = await _component_names(db, ordered)
    payload.candidates = [_candidate_response(c, names) for c in ordered]
    payload.relationships = [
        CausalRelationshipResponse.model_validate(edge) for edge in loaded.relationships
    ]
    payload.evidence = [
        _evidence_response(item) for item in loaded.evidence[:_MAX_ITEMS]
    ]
    return payload


@router.get(
    "/{incident_id}/causal-analysis/history",
    response_model=AnalysisHistoryResponse,
)
async def get_analysis_history(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    limit: int = Query(20, ge=1, le=_MAX_HISTORY),
    db: AsyncSession = Depends(get_db),
) -> AnalysisHistoryResponse:
    """Every stored version, newest first, with what changed (§43, §44)."""
    incident = await require_incident(
        db, incident_id, project_id=project_id, environment_id=environment_id
    )
    service = CausalAnalysisService(db)
    versions = await service.analysis_history(
        project_id=incident.project_id, incident_id=incident_id, limit=limit
    )
    explanation_service = CausalExplanationService(db)
    items: list[AnalysisHistoryItem] = []
    for index, version in enumerate(versions):
        loaded = await explanation_service.load_analysis(
            project_id=incident.project_id,
            incident_id=incident_id,
            analysis_id=version.id,
        )
        candidates = loaded.candidates if loaded else []
        evidence = loaded.evidence if loaded else []
        relationships = loaded.relationships if loaded else []
        primary = next(
            (c for c in candidates if c.id == version.primary_candidate_id), None
        )
        previous = versions[index + 1] if index + 1 < len(versions) else None
        diff: Optional[dict] = None
        if previous is not None:
            previous_primary = previous.primary_candidate_id
            diff = {
                "primary_changed": previous_primary != version.primary_candidate_id,
                "previous_primary_candidate_id": (
                    str(previous_primary) if previous_primary else None
                ),
                "confidence_changed": (
                    previous.overall_confidence != version.overall_confidence
                ),
                "previous_confidence": previous.overall_confidence.value,
                "candidate_count_delta": len(candidates),
                "evidence_count": len(evidence),
                "new_contradictions": sum(
                    1
                    for item in evidence
                    if item.polarity is EvidencePolarity.CONTRADICTING
                ),
                "relationship_count": len(relationships),
            }
        items.append(
            AnalysisHistoryItem(
                analysis_id=version.id,
                analysis_version=version.analysis_version,
                status=version.status,
                overall_confidence=version.overall_confidence,
                primary_candidate_id=version.primary_candidate_id,
                primary_candidate_summary=(
                    primary.explanation if primary is not None else version.summary
                ),
                candidate_count=len(candidates),
                supporting_evidence_count=sum(
                    1
                    for item in evidence
                    if item.polarity is EvidencePolarity.SUPPORTING
                ),
                contradicting_evidence_count=sum(
                    1
                    for item in evidence
                    if item.polarity is EvidencePolarity.CONTRADICTING
                ),
                started_at=version.started_at,
                completed_at=version.completed_at,
                trigger=version.trigger,
                requested_by=version.requested_by,
                diff=diff,
            )
        )
    return AnalysisHistoryResponse(items=items, total=len(items))


@router.get(
    "/{incident_id}/causal-analysis/{analysis_id}/explanation",
)
async def get_analysis_explanation(
    incident_id: uuid.UUID,
    analysis_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Structured explanation: reasons, evidence, alternatives, limitations (§37).

    Concise and evidence-based — the API returns what ARGUS believes and why,
    derived from stored rows, never a hidden reasoning trace.
    """
    incident = await require_incident(
        db, incident_id, project_id=project_id, environment_id=environment_id
    )
    analysis, loaded = await _load_full(
        db,
        incident_id=incident_id,
        project_id=incident.project_id,
        analysis_id=analysis_id,
    )
    explanation = CausalExplanationService(db).explain_loaded(
        loaded,
        incident_label=incident.title,
        labels=await _component_names(db, loaded.candidates),
    )
    payload = explanation.as_dict()
    payload["status"] = analysis.status.value
    payload["disclaimer"] = CAUSAL_DISCLAIMER
    return payload


# ---------------------------------------------------------------------------
# Candidates / hypotheses / evidence
# ---------------------------------------------------------------------------
@router.get("/{incident_id}/root-causes", response_model=RootCauseCandidateList)
async def get_root_causes(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    analysis_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> RootCauseCandidateList:
    """Ranked hypotheses with their evidence counts and score breakdowns (§25)."""
    incident = await require_incident(
        db, incident_id, project_id=project_id, environment_id=environment_id
    )
    _, loaded = await _load_full(
        db,
        incident_id=incident_id,
        project_id=incident.project_id,
        analysis_id=analysis_id,
    )
    ordered = sorted(loaded.candidates, key=lambda c: (-c.score, str(c.id)))
    names = await _component_names(db, ordered)
    return RootCauseCandidateList(
        items=[_candidate_response(candidate, names) for candidate in ordered],
        total=len(ordered),
    )


@router.get("/{incident_id}/hypotheses", response_model=HypothesesResponse)
async def get_hypotheses(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    analysis_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> HypothesesResponse:
    """Every plausible explanation with its supporting/contradicting evidence (§28).

    The primary hypothesis is first when one was selected; when none was, the
    list is still returned ranked — with no winner implied.
    """
    incident = await require_incident(
        db, incident_id, project_id=project_id, environment_id=environment_id
    )
    analysis, loaded = await _load_full(
        db,
        incident_id=incident_id,
        project_id=incident.project_id,
        analysis_id=analysis_id,
    )
    ordered = sorted(loaded.candidates, key=lambda c: (-c.score, str(c.id)))
    if analysis.primary_candidate_id is not None:
        ordered.sort(key=lambda c: (0 if c.id == analysis.primary_candidate_id else 1))
    names = await _component_names(db, ordered)
    return HypothesesResponse(
        analysis_id=analysis.id,
        analysis_version=analysis.analysis_version,
        primary_candidate_id=analysis.primary_candidate_id,
        overall_confidence=analysis.overall_confidence,
        items=[_hypothesis(loaded, candidate, names) for candidate in ordered],
    )


@router.get("/{incident_id}/evidence-analysis", response_model=EvidenceAnalysisResponse)
async def get_evidence_analysis(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    analysis_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> EvidenceAnalysisResponse:
    """Supporting vs contradicting evidence per candidate, plus what was missing."""
    incident = await require_incident(
        db, incident_id, project_id=project_id, environment_id=environment_id
    )
    analysis, loaded = await _load_full(
        db,
        incident_id=incident_id,
        project_id=incident.project_id,
        analysis_id=analysis_id,
    )
    ordered = sorted(loaded.candidates, key=lambda c: (-c.score, str(c.id)))
    names = await _component_names(db, ordered)
    return EvidenceAnalysisResponse(
        analysis_id=analysis.id,
        candidates=[_hypothesis(loaded, candidate, names) for candidate in ordered],
        missing_evidence=analysis.missing_evidence,
    )


# ---------------------------------------------------------------------------
# Graph / chain / edge explanations
# ---------------------------------------------------------------------------
@router.get("/{incident_id}/causal-graph", response_model=CausalGraphResponse)
async def get_causal_graph(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    analysis_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> CausalGraphResponse:
    """The renderable causal graph (§9, §40).

    Node and edge rows carry their own confidence and evidence counts so the UI
    can distinguish observably-supported edges from correlation without
    recomputing anything.
    """
    incident = await require_incident(
        db, incident_id, project_id=project_id, environment_id=environment_id
    )
    analysis, loaded = await _load_full(
        db,
        incident_id=incident_id,
        project_id=incident.project_id,
        analysis_id=analysis_id,
    )
    ordered = sorted(loaded.candidates, key=lambda c: (-c.score, str(c.id)))
    names = await _component_names(db, ordered)
    return CausalGraphResponse(
        analysis_id=analysis.id,
        analysis_version=analysis.analysis_version,
        overall_confidence=analysis.overall_confidence,
        primary_candidate_id=analysis.primary_candidate_id,
        nodes=[_candidate_response(candidate, names) for candidate in ordered],
        edges=[
            CausalRelationshipResponse.model_validate(edge)
            for edge in loaded.relationships
        ],
    )


@router.get("/{incident_id}/causal-chain", response_model=CausalChainResponse)
async def get_causal_chain(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    analysis_id: Optional[uuid.UUID] = None,
    max_length: int = Query(6, ge=1, le=12),
    db: AsyncSession = Depends(get_db),
) -> CausalChainResponse:
    """The evidence-backed chain from the suspected origin to the symptoms (§32).

    Validated before it is returned (§33): an obviously impossible chain
    (effect before cause) is reported as invalid with its reasons instead of
    being presented as a finding.
    """
    incident = await require_incident(
        db, incident_id, project_id=project_id, environment_id=environment_id
    )
    analysis, loaded = await _load_full(
        db,
        incident_id=incident_id,
        project_id=incident.project_id,
        analysis_id=analysis_id,
    )
    service = CausalExplanationService(db)
    chain = service.extract_chain(
        candidates=loaded.candidates,
        relationships=loaded.relationships,
        primary_candidate_id=analysis.primary_candidate_id,
        max_length=max_length,
    )
    notes: list[str] = []
    if not chain:
        if analysis.primary_candidate_id is None:
            notes.append(
                "No chain is reported because no primary hypothesis reached the "
                "documented evidence threshold."
            )
        else:
            notes.append(
                "No directional, evidence-backed edge leaves the primary "
                "hypothesis, so no chain is claimed."
            )
    for index, edge in enumerate(chain):
        if edge.temporal_alignment_seconds is not None and (
            edge.temporal_alignment_seconds < 0
        ):
            notes.append(
                f"Link {index}: the target degraded "
                f"{-edge.temporal_alignment_seconds}s before the source — this link "
                "is a temporal contradiction, not a propagation."
            )
    valid = bool(chain) and not any(
        edge.temporal_alignment_seconds is not None
        and edge.temporal_alignment_seconds < 0
        for edge in chain
    )
    candidate_ids = [chain[0].source_candidate_id] if chain else []
    candidate_ids.extend(edge.target_candidate_id for edge in chain)
    return CausalChainResponse(
        analysis_id=analysis.id,
        chain=[
            CausalChainLinkResponse(
                source_candidate_id=edge.source_candidate_id,
                target_candidate_id=edge.target_candidate_id,
                relationship_type=edge.relationship_type,
                confidence=edge.confidence,
                temporal_alignment_seconds=edge.temporal_alignment_seconds,
                evidence_count=edge.supporting_evidence_count,
                explanation=edge.explanation,
            )
            for edge in chain
        ],
        candidate_ids=candidate_ids,
        valid=valid if chain else False,
        validation_notes=notes,
    )


@router.get("/{incident_id}/relationships/{relationship_id}/explanation")
async def get_relationship_explanation(
    incident_id: uuid.UUID,
    relationship_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Why ARGUS believes a specific relationship exists (§41).

    This is the evidence inspector's backing call: every quote returned is a
    stored row, and ``caveats`` states whatever weakens the edge.
    """
    incident = await require_incident(
        db, incident_id, project_id=project_id, environment_id=environment_id
    )
    from sqlalchemy import select

    relationship = (
        (
            await db.execute(
                select(CausalRelationship).where(
                    CausalRelationship.id == relationship_id,
                    CausalRelationship.project_id == incident.project_id,
                )
            )
        )
        .scalars()
        .first()
    )
    if relationship is None:
        raise HTTPException(status_code=404, detail="Relationship not found")
    analysis, loaded = await _load_full(
        db,
        incident_id=incident_id,
        project_id=incident.project_id,
        analysis_id=relationship.analysis_id,
    )
    if not any(item.id == relationship_id for item in loaded.relationships):
        raise HTTPException(status_code=404, detail="Relationship not found")
    explanation = CausalExplanationService(db).explain_relationship(
        relationship, loaded.evidence_for_relationship(relationship_id)
    )
    payload = explanation.as_dict()
    payload["analysis_id"] = str(analysis.id)
    payload["analysis_version"] = analysis.analysis_version
    return payload


__all__ = ["router"]
