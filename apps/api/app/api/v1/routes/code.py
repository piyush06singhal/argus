"""ARGUS Code Intelligence & AI Debugger Routes (Phase 6 §47–§52).

```text
POST   /projects/{pid}/repositories                  register a repository
GET    /projects/{pid}/repositories                  repositories in a project
GET    /projects/{pid}/repositories/{rid}            one repository
POST   /projects/{pid}/repositories/{rid}/index       index a revision into a snapshot
GET    /projects/{pid}/repositories/{rid}/snapshots   snapshot history (§55)
GET    /projects/{pid}/repositories/{rid}/history     recent commits (§18)
GET    /projects/{pid}/repositories/{rid}/blame       line authorship
GET    /projects/{pid}/repositories/{rid}/diff        revision diff
GET    /snapshots/{sid}                              snapshot summary
GET    /snapshots/{sid}/files                        indexed files
GET    /snapshots/{sid}/symbols                      symbol search (§12, §23)
GET    /snapshots/{sid}/symbols/{symbol_id}          symbol + callers/callees
GET    /snapshots/{sid}/search                       code search (§23)
GET    /snapshots/{sid}/risk-signals                 investigation signals (§44)
GET    /incidents/{iid}/code-mappings                trace → code (§15)
POST   /incidents/{iid}/debug-sessions               open a session (optionally analyse)
GET    /incidents/{iid}/debug-sessions               sessions for an incident
GET    /debug-sessions/{sid}                         session with its findings
POST   /debug-sessions/{sid}/analyze                 run the debugger
GET    /debug-sessions/{sid}/analysis                latest analysis run
GET    /debug-sessions/{sid}/locations               suspected locations + validation
GET    /debug-sessions/{sid}/hypotheses              hypotheses with their evidence
GET    /debug-sessions/{sid}/evidence                the evidence index
GET    /debug-sessions/{sid}/messages                the conversation
POST   /debug-sessions/{sid}/messages                ask a grounded question (§35)
GET    /debug-sessions/{sid}/tools                   tool-call audit trail (§59)
GET    /debug-sessions/{sid}/timeline                debugging timeline (§52)
GET    /debug-sessions/{sid}/investigation           deterministic context (§43)
GET    /debugger/metrics                             coverage tallies
```

Scope rules, following the Phase 3–5 convention:

* a mutating request **requires** ``project_id`` and the resource must belong to
  it — knowing a UUID is not authority to index a repository or open a session;
* reads accept an optional ``project_id``; when supplied it is enforced, and an
  out-of-scope id answers 404 rather than confirming the row exists;
* every collection is bounded, and every response says whether it was truncated.

Nothing here writes to a repository or executes anything it was handed: the only
outbound operations are reads through the repository provider.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_incident, require_project
from app.core.config import get_settings
from app.core.database import get_db
from app.models.code import (
    CodeVersionStatus,
    CodeFile,
    CodeIndexRun,
    CodeRiskSignal,
    CodeSymbol,
    DebugAnalysisRun,
    DebugAnalysisStatus,
    DebugCodeLocation,
    DebugEvidence,
    DebugHypothesis,
    DebugMessage,
    DebugSession,
    DebugSessionStatus,
    DebugToolCall,
    LocationValidation,
    RepositoryIndexStatus,
    RepositorySnapshot,
    SnapshotStatus,
    ToolCallStatus,
    TraceCodeMapping,
)
from app.models.deployment import CodeRepository, DeploymentEvent
from app.schemas.code import (
    BlameLineResponse,
    BlameResponse,
    CodeFileListResponse,
    CodeFileResponse,
    CodeSearchResponse,
    CommitResponse,
    DebugAnalysisResponse,
    DebugAskRequest,
    DebugAssistantAnswer,
    DebugCodeLocationResponse,
    DebugEvidenceResponse,
    DebugHypothesisResponse,
    DebugMessageResponse,
    DebugSessionCreateRequest,
    DebugSessionDetailResponse,
    DebugSessionListResponse,
    DebugSessionResponse,
    DebugTimelineEvent,
    DebugTimelineResponse,
    DebugToolCallResponse,
    DebuggerMetricsResponse,
    DiffEntryResponse,
    DiffResponse,
    HistoryResponse,
    IndexRequest,
    IndexResponse,
    IndexRunResponse,
    InvestigationResponse,
    ReferenceResponse,
    RepositoryCreateRequest,
    RepositoryListResponse,
    RepositoryResponse,
    RiskSignalListResponse,
    RiskSignalResponse,
    SnapshotListResponse,
    SnapshotResponse,
    SnapshotSummaryResponse,
    SymbolDetailResponse,
    SymbolEdgeResponse,
    SymbolListResponse,
    SymbolResponse,
    TraceMappingListResponse,
    TraceMappingResponse,
)
from app.services.change_history import ChangeHistoryService
from app.services.code_index_service import CodeIndexer
from app.services.code_query_service import CodeKnowledgeService
from app.services.code_snapshot_service import (
    CodeSnapshotService,
    CodeVersionResolver,
    RevisionResolution,
)
from app.services.debug_context_builder import DebugContextBuilder
from app.services.debug_session_service import DebugSessionManager
from app.services.repository_provider import (
    ProviderError,
    RepositoryError,
    provider_for_repository,
)
from app.services.trace_code_mapper import TraceCodeMapper

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(tags=["code-intelligence"])

#: Collections are bounded. An unbounded code dump is not an API.
MAX_ITEMS = 200
MAX_FILES = 500
DEFAULT_ITEMS = 50


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------
async def _get_repository(
    db: AsyncSession,
    repository_id: uuid.UUID,
    *,
    project_id: Optional[uuid.UUID] = None,
    require_scope: bool = False,
) -> CodeRepository:
    if require_scope and project_id is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "project_id is required for this operation: it is what proves the "
                "caller may read or index this repository"
            ),
        )
    repository = await db.get(CodeRepository, repository_id)
    if repository is None:
        raise HTTPException(status_code=404, detail="Repository not found")
    if project_id is not None and repository.project_id != project_id:
        # 404 rather than 403: an out-of-scope repository is not visible.
        raise HTTPException(status_code=404, detail="Repository not found")
    return repository


async def _require_resolvable_revision(
    provider, revision: Optional[str], label: str
) -> Optional[str]:
    """Refuse a named revision the repository does not contain.

    Shared by the history, blame and diff endpoints because they all take a
    user-supplied revision, and all three can silently produce a *plausible but
    wrong* answer for one that does not exist: an empty history, empty blame and
    "nothing changed" respectively. A provider without version control cannot
    confirm any revision, so it is refused rather than trusted — otherwise a
    checkout with no ``.git`` would report every revision as valid.
    """
    if not revision or revision.upper() == "HEAD":
        return revision
    try:
        info = await provider.describe()
        resolved = await provider.resolve(revision)
    except (RepositoryError, ProviderError) as error:
        raise HTTPException(
            status_code=422,
            detail=f"the {label} revision {revision!r} could not be resolved: {error}",
        ) from error
    if resolved is None or not info.vcs_present:
        raise HTTPException(
            status_code=422,
            detail=(
                f"the {label} revision {revision!r} does not exist in this "
                "repository; no result was computed"
            ),
        )
    return resolved


async def _get_snapshot(
    db: AsyncSession,
    snapshot_id: uuid.UUID,
    *,
    project_id: Optional[uuid.UUID] = None,
) -> RepositorySnapshot:
    snapshot = await db.get(RepositorySnapshot, snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    if project_id is not None and snapshot.project_id != project_id:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return snapshot


async def _get_debug_session(
    db: AsyncSession,
    session_id: uuid.UUID,
    *,
    project_id: Optional[uuid.UUID] = None,
    require_scope: bool = False,
) -> DebugSession:
    if require_scope and project_id is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "project_id is required for this operation: it is what proves the "
                "caller owns the debugging session they are asking ARGUS to run"
            ),
        )
    session = await db.get(DebugSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Debug session not found")
    if project_id is not None and session.project_id != project_id:
        raise HTTPException(status_code=404, detail="Debug session not found")
    return session


async def _provider_for(repository: CodeRepository):
    try:
        return provider_for_repository(repository)
    except (RepositoryError, ProviderError) as error:
        raise HTTPException(
            status_code=422,
            detail=f"repository is not readable by its provider: {error}",
        ) from error


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------
def _snapshot_response(snapshot: RepositorySnapshot) -> SnapshotResponse:
    return SnapshotResponse(
        id=snapshot.id,
        project_id=snapshot.project_id,
        repository_id=snapshot.repository_id,
        commit_sha=snapshot.commit_sha,
        branch=snapshot.branch,
        commit_at=snapshot.commit_at,
        commit_message=snapshot.commit_message,
        commit_author=snapshot.commit_author,
        provider_name=snapshot.provider_name,
        version_status=snapshot.version_status,
        version_evidence=snapshot.version_evidence,
        status=snapshot.status,
        indexed_at=snapshot.indexed_at,
        file_count=snapshot.file_count,
        symbol_count=snapshot.symbol_count,
        languages=list(snapshot.languages or []),
        error=snapshot.error,
        created_at=snapshot.created_at,
    )


def _index_run_response(
    run: CodeIndexRun, snapshot: Optional[RepositorySnapshot] = None
) -> IndexRunResponse:
    #: The indexer records the per-pass tallies on the *snapshot's*
    #: ``index_metadata`` (they describe the snapshot) and only the run's own
    #: counts on the run row. Reading the wrong one is how a re-index reports
    #: "0 files reused" while the snapshot says otherwise.
    metadata: dict[str, Any] = dict(snapshot.index_metadata or {}) if snapshot else {}
    metadata.update(dict(run.run_metadata or {}))
    return IndexRunResponse(
        id=run.id,
        snapshot_id=run.snapshot_id,
        repository_id=run.repository_id,
        status=run.status,
        trigger=run.trigger,
        incremental=run.incremental,
        base_commit_sha=run.base_commit_sha,
        started_at=run.started_at,
        completed_at=run.completed_at,
        duration_ms=run.duration_ms,
        files_seen=run.files_seen,
        files_indexed=run.files_indexed,
        files_reused=int(metadata.get("files_reused") or 0),
        files_added=run.files_added,
        files_modified=run.files_modified,
        files_deleted=run.files_deleted,
        files_failed=run.files_failed,
        symbols_indexed=run.symbols_indexed,
        references_indexed=run.references_indexed,
        relationships_indexed=run.relationships_indexed,
        files_heuristic=int(metadata.get("files_heuristic") or 0),
        files_partial=int(metadata.get("files_partial") or 0),
        errors=list(run.errors or []),
        snapshot=_snapshot_response(snapshot) if snapshot else None,
    )


def _symbol_response(symbol: CodeSymbol, **extra: Any) -> SymbolResponse:
    payload: dict[str, Any] = {
        "id": symbol.id,
        "snapshot_id": symbol.snapshot_id,
        "file_path": symbol.file_path,
        "symbol_name": symbol.symbol_name,
        "qualified_name": symbol.qualified_name,
        "symbol_type": symbol.symbol_type,
        "language": symbol.language,
        "start_line": symbol.start_line,
        "end_line": symbol.end_line,
        "signature": symbol.signature,
        "documentation": symbol.documentation,
        "is_async": symbol.is_async,
        "complexity": symbol.complexity,
        "route": symbol.route,
        "http_method": symbol.http_method,
        "component_id": symbol.component_id,
        "reference": f"FILE:{symbol.file_path}:{symbol.start_line}-{symbol.end_line}",
    }
    payload.update(extra)
    return SymbolResponse(**payload)


def _signal_response(signal: CodeRiskSignal) -> RiskSignalResponse:
    return RiskSignalResponse(
        id=signal.id,
        signal_type=signal.signal_type,
        value=signal.value,
        unit=signal.unit,
        file_path=signal.file_path,
        symbol_id=signal.symbol_id,
        detail=signal.detail,
        observed_at=signal.observed_at,
    )


def _location_response(location: DebugCodeLocation) -> DebugCodeLocationResponse:
    reference = f"FILE:{location.file_path}"
    if location.start_line:
        reference += (
            f":{location.start_line}-{location.end_line or location.start_line}"
        )
    refs = list((location.location_metadata or {}).get("evidence_refs") or [])
    return DebugCodeLocationResponse(
        id=location.id,
        file_path=location.file_path,
        symbol_name=location.symbol_name,
        symbol_id=location.symbol_id,
        start_line=location.start_line,
        end_line=location.end_line,
        label=location.label,
        reason=location.reason or "",
        confidence=location.confidence,
        validation=location.validation,
        validation_detail=location.validation_detail,
        evidence_refs=refs,
        reference=reference,
        displayable=location.validation is LocationValidation.VALID,
    )


def _evidence_response(evidence: DebugEvidence) -> DebugEvidenceResponse:
    return DebugEvidenceResponse(
        id=evidence.id,
        kind=evidence.kind,
        polarity=evidence.polarity.value,
        reference=evidence.reference,
        label=evidence.label,
        source_table=evidence.source_table,
        source_id=evidence.source_id,
        quote=evidence.quote,
        start_line=evidence.start_line,
        end_line=evidence.end_line,
        component_id=evidence.component_id,
        valid=evidence.valid,
        validation_error=evidence.validation_error,
        strength=evidence.strength,
        observed_at=evidence.observed_at,
    )


def _hypothesis_response(
    hypothesis: DebugHypothesis,
    locations: list[DebugCodeLocation],
    evidence: list[DebugEvidence],
) -> DebugHypothesisResponse:
    return DebugHypothesisResponse(
        id=hypothesis.id,
        description=hypothesis.description,
        category=hypothesis.category,
        confidence=hypothesis.confidence,
        validation_status=hypothesis.validation_status,
        rationale=hypothesis.rationale,
        testable=hypothesis.testable,
        test_approach=hypothesis.test_approach,
        recurrence_count=hypothesis.recurrence_count,
        locations=[_location_response(row) for row in locations],
        supporting_evidence=[
            _evidence_response(row)
            for row in evidence
            if row.polarity.value == "SUPPORTING"
        ],
        contradicting_evidence=[
            _evidence_response(row)
            for row in evidence
            if row.polarity.value == "CONTRADICTING"
        ],
    )


def _message_response(message: DebugMessage) -> DebugMessageResponse:
    return DebugMessageResponse(
        id=message.id,
        role=message.role,
        content=message.content,
        created_by=message.created_by,
        evidence_refs=list(message.evidence_refs or []),
        metadata=dict(message.message_metadata or {}) or None,
        created_at=message.created_at,
    )


def _session_response(session: DebugSession) -> DebugSessionResponse:
    return DebugSessionResponse(
        id=session.id,
        project_id=session.project_id,
        incident_id=session.incident_id,
        repository_id=session.repository_id,
        snapshot_id=session.snapshot_id,
        title=session.title,
        status=session.status,
        created_by=session.created_by,
        version_status=session.version_status,
        version_note=session.version_note,
        context_version=session.context_version,
        summary=session.summary,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _repository_response(
    repository: CodeRepository,
    *,
    snapshot: Optional[RepositorySnapshot],
    snapshot_count: int,
) -> RepositoryResponse:
    capabilities = ["read_files", "history", "diff", "blame"]
    if (repository.provider or "") == "local":
        capabilities.append("local_checkout")
    else:
        capabilities.append("git_remote")
    return RepositoryResponse(
        id=repository.id,
        project_id=repository.project_id,
        provider=repository.provider,
        repository_url=repository.repository_url,
        default_branch=repository.default_branch,
        connection_status=repository.connection_status,
        language=repository.language,
        framework=repository.framework,
        index_status=repository.index_status,
        last_indexed_at=repository.last_indexed_at,
        last_indexed_commit=repository.last_indexed_commit,
        created_at=repository.created_at,
        updated_at=repository.updated_at,
        latest_snapshot_id=snapshot.id if snapshot else None,
        latest_commit_sha=snapshot.commit_sha if snapshot else None,
        snapshot_count=snapshot_count,
        capabilities=capabilities,
    )


async def _analysis_response(
    db: AsyncSession, run: DebugAnalysisRun
) -> DebugAnalysisResponse:
    locations = (
        (
            await db.execute(
                select(DebugCodeLocation)
                .where(DebugCodeLocation.analysis_run_id == run.id)
                .order_by(DebugCodeLocation.validation, DebugCodeLocation.file_path)
                .limit(MAX_ITEMS)
            )
        )
        .scalars()
        .all()
    )
    hypotheses = (
        (
            await db.execute(
                select(DebugHypothesis)
                .where(DebugHypothesis.analysis_run_id == run.id)
                .order_by(DebugHypothesis.created_at)
                .limit(MAX_ITEMS)
            )
        )
        .scalars()
        .all()
    )
    evidence = (
        (
            await db.execute(
                select(DebugEvidence)
                .where(DebugEvidence.analysis_run_id == run.id)
                .order_by(DebugEvidence.kind, DebugEvidence.reference)
                .limit(MAX_ITEMS)
            )
        )
        .scalars()
        .all()
    )
    by_hypothesis: dict[Any, list[DebugEvidence]] = {}
    for row in evidence:
        by_hypothesis.setdefault(row.hypothesis_id, []).append(row)
    valid = sum(1 for row in locations if row.validation is LocationValidation.VALID)
    metadata = dict(run.run_metadata or {})
    return DebugAnalysisResponse(
        id=run.id,
        session_id=run.session_id,
        snapshot_id=run.snapshot_id,
        status=run.status,
        kind=run.kind,
        provider_name=run.provider_name,
        model_name=run.model_name,
        prompt_version=run.prompt_version,
        context_version=run.context_version,
        started_at=run.started_at,
        completed_at=run.completed_at,
        duration_ms=run.duration_ms,
        tool_call_count=run.tool_call_count,
        files_accessed=run.files_accessed,
        context_bytes=run.context_bytes,
        confidence=run.confidence,
        summary=run.summary,
        invalid_references=list(run.invalid_references or []),
        missing_evidence=list(run.missing_evidence or []),
        recommended_inspections=list(run.recommended_inspections or []),
        degraded=run.status is DebugAnalysisStatus.DEGRADED,
        degraded_reason=run.error,
        locations=[_location_response(row) for row in locations],
        hypotheses=[
            _hypothesis_response(
                row,
                [loc for loc in locations if loc.hypothesis_id == row.id],
                by_hypothesis.get(row.id, []),
            )
            for row in hypotheses
        ],
        evidence=[_evidence_response(row) for row in evidence if not row.hypothesis_id],
        counts={
            "locations": len(locations),
            "locations_valid": valid,
            "locations_rejected": len(locations) - valid,
            "hypotheses": len(hypotheses),
            "evidence": len(evidence),
            "invalid_references": len(run.invalid_references or []),
            "tool_calls": run.tool_call_count,
            "degraded": metadata.get(
                "degraded", run.status is DebugAnalysisStatus.DEGRADED
            ),
        },
    )


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/repositories", response_model=RepositoryResponse)
async def register_repository(
    project_id: uuid.UUID,
    payload: RepositoryCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> RepositoryResponse:
    """Register a repository for a project.

    The provider is validated by being *asked to describe the repository*: a path
    that does not exist, or a remote the provider refuses, is rejected here rather
    than at analysis time. Credentials are never accepted in the body — a
    repository URL carries no secret, and there is nowhere to put one.
    """
    await require_project(db, project_id)
    repository = CodeRepository(
        project_id=project_id,
        provider=payload.provider,
        repository_url=payload.repository_url,
        local_path=payload.repository_url if payload.provider == "local" else None,
        default_branch=payload.default_branch or "main",
        language=payload.language,
        last_indexed_commit=payload.last_indexed_commit,
        index_status=RepositoryIndexStatus.PENDING,
    )
    db.add(repository)
    await db.flush()

    provider = await _provider_for(repository)
    try:
        info = await provider.describe()
    except (RepositoryError, ProviderError) as error:
        await db.rollback()
        raise HTTPException(
            status_code=422, detail=f"repository is not readable: {error}"
        ) from error
    repository.default_branch = info.default_branch or repository.default_branch
    repository.connection_status = "CONNECTED"
    repository.metadata_ = {
        "provider_name": info.provider_name,
        "vcs_present": info.vcs_present,
        "root": info.root,
        "notes": list(info.notes or []),
    }
    await db.commit()
    await db.refresh(repository)
    return _repository_response(repository, snapshot=None, snapshot_count=0)


@router.get(
    "/projects/{project_id}/repositories", response_model=RepositoryListResponse
)
async def list_repositories(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(DEFAULT_ITEMS, ge=1, le=MAX_ITEMS),
) -> RepositoryListResponse:
    await require_project(db, project_id)
    rows = (
        (
            await db.execute(
                select(CodeRepository)
                .where(CodeRepository.project_id == project_id)
                .order_by(CodeRepository.created_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    counts: dict[Any, int] = {
        row[0]: int(row[1])
        for row in (
            await db.execute(
                select(
                    RepositorySnapshot.repository_id, func.count(RepositorySnapshot.id)
                )
                .where(RepositorySnapshot.project_id == project_id)
                .group_by(RepositorySnapshot.repository_id)
            )
        ).all()
    }
    latest: dict[Any, RepositorySnapshot] = {}
    if rows:
        snapshots = (
            (
                await db.execute(
                    select(RepositorySnapshot)
                    .where(
                        RepositorySnapshot.repository_id.in_([row.id for row in rows])
                    )
                    .order_by(RepositorySnapshot.created_at)
                )
            )
            .scalars()
            .all()
        )
        for snapshot in snapshots:
            latest[snapshot.repository_id] = snapshot
    return RepositoryListResponse(
        items=[
            _repository_response(
                row, snapshot=latest.get(row.id), snapshot_count=counts.get(row.id, 0)
            )
            for row in rows
        ],
        total=len(rows),
    )


@router.get(
    "/projects/{project_id}/repositories/{repository_id}",
    response_model=RepositoryResponse,
)
async def get_repository(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> RepositoryResponse:
    await require_project(db, project_id)
    repository = await _get_repository(db, repository_id, project_id=project_id)
    snapshot = (
        (
            await db.execute(
                select(RepositorySnapshot)
                .where(RepositorySnapshot.repository_id == repository.id)
                .order_by(RepositorySnapshot.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    count = (
        await db.execute(
            select(func.count(RepositorySnapshot.id)).where(
                RepositorySnapshot.repository_id == repository.id
            )
        )
    ).scalar_one()
    return _repository_response(
        repository, snapshot=snapshot, snapshot_count=int(count or 0)
    )


@router.post(
    "/projects/{project_id}/repositories/{repository_id}/index",
    response_model=IndexResponse,
)
async def index_repository(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    payload: IndexRequest,
    db: AsyncSession = Depends(get_db),
) -> IndexResponse:
    """Index a revision into a new (or existing) snapshot.

    Indexing is idempotent per revision: re-indexing the same commit reuses the
    snapshot row and re-parses only files whose content hash changed when
    ``incremental`` is set. The run row records exactly what was interpreted, so
    a partial index is visible rather than implied.
    """
    await require_project(db, project_id)
    repository = await _get_repository(
        db, repository_id, project_id=project_id, require_scope=True
    )
    provider = await _provider_for(repository)

    reference = payload.reference
    resolution = None
    if reference is None:
        #: No revision given: use the *deployed* revision when the repository has
        #: a recent deployment, otherwise the provider's current revision. This is
        #: what makes "index this repo so I can debug an incident" analyse the
        #: code that ran rather than whatever HEAD says now.
        deployment = (
            (
                await db.execute(
                    select(DeploymentEvent)
                    .where(
                        DeploymentEvent.project_id == project_id,
                        DeploymentEvent.commit_sha.is_not(None),
                    )
                    .order_by(DeploymentEvent.deployed_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if deployment is not None:
            reference = deployment.commit_sha
    else:
        #: An explicitly requested revision is the strongest resolution there is
        #: — but only once the *repository* confirms it exists. Recording a
        #: caller-supplied sha as RESOLVED without asking would let a typo (or a
        #: sha from another fork) pin an analysis to code the repository does not
        #: contain, and every downstream confidence would inherit the mistake.
        #: A provider with no revision history cannot verify anything, so its
        #: echo of the reference is explicitly not treated as confirmation.
        info = await provider.describe()
        try:
            resolved = await provider.resolve(reference)
        except (RepositoryError, ProviderError):
            resolved = None
        if resolved and info.vcs_present:
            resolution = RevisionResolution(
                reference=resolved,
                status=CodeVersionStatus.RESOLVED,
                evidence=(
                    f"the caller pinned revision {resolved[:12]}, which exists in "
                    "this repository's history"
                ),
            )
            reference = resolved
        else:
            resolution = RevisionResolution(
                reference=reference,
                status=CodeVersionStatus.UNKNOWN,
                evidence=(
                    f"requested revision {reference} "
                    + (
                        "is not present in this repository"
                        if info.vcs_present
                        else "cannot be verified: the provider has no revision history"
                    )
                    + "; indexed as the best available code, NOT confirmed as the "
                    "deployed revision"
                ),
            )

    repository.index_status = RepositoryIndexStatus.INDEXING
    await db.flush()
    snapshot = await CodeSnapshotService().get_or_create_snapshot(
        db,
        repository,
        reference,
        version_status=(resolution.status if resolution else CodeVersionStatus.UNKNOWN),
        version_evidence=(
            resolution.evidence
            if resolution
            else (
                f"indexed at {reference}"
                if reference
                else "indexed at the provider's revision"
            )
        ),
        provider=provider,
    )
    run = await CodeIndexer(db).index(
        repository,
        snapshot,
        trigger="api",
        incremental=payload.incremental,
        max_files=payload.max_files,
    )
    await db.refresh(snapshot)
    repository.index_status = (
        RepositoryIndexStatus.INDEXED
        if snapshot.status is SnapshotStatus.READY
        else RepositoryIndexStatus.PARTIAL
        if snapshot.status is SnapshotStatus.PARTIAL
        else RepositoryIndexStatus.FAILED
    )
    repository.last_indexed_at = snapshot.indexed_at or datetime.now(timezone.utc)
    repository.last_indexed_commit = snapshot.commit_sha
    repository.language = (snapshot.languages or [repository.language])[0]
    await db.commit()
    await db.refresh(snapshot)

    notes = [
        f"{run.files_indexed} file(s) indexed from {run.files_seen} seen",
        f"{run.symbols_indexed} symbol(s), {run.relationships_indexed} resolved relationship(s)",
    ]
    if run.files_failed:
        notes.append(f"{run.files_failed} file(s) could not be indexed at all")
    metadata = dict(run.run_metadata or {})
    if metadata.get("files_heuristic"):
        notes.append(
            f"{metadata['files_heuristic']} JS/TS file(s) were read by the structural "
            "scanner rather than a full parser"
        )
    if repository.index_status is RepositoryIndexStatus.FAILED:
        notes.append(f"indexing failed: {snapshot.error or 'see the run errors'}")
    return IndexResponse(
        run=_index_run_response(run, snapshot),
        snapshot=_snapshot_response(snapshot),
        notes=notes,
    )


@router.get(
    "/projects/{project_id}/repositories/{repository_id}/snapshots",
    response_model=SnapshotListResponse,
)
async def list_snapshots(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(DEFAULT_ITEMS, ge=1, le=MAX_ITEMS),
) -> SnapshotListResponse:
    await require_project(db, project_id)
    repository = await _get_repository(db, repository_id, project_id=project_id)
    rows = (
        (
            await db.execute(
                select(RepositorySnapshot)
                .where(RepositorySnapshot.repository_id == repository.id)
                .order_by(RepositorySnapshot.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return SnapshotListResponse(
        items=[_snapshot_response(row) for row in rows], total=len(rows)
    )


@router.get(
    "/projects/{project_id}/repositories/{repository_id}/history",
    response_model=HistoryResponse,
)
async def repository_history(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    reference: Optional[str] = Query(None, max_length=512),
    path: Optional[str] = Query(None, max_length=1024),
    limit: int = Query(20, ge=1, le=50),
) -> HistoryResponse:
    await require_project(db, project_id)
    repository = await _get_repository(db, repository_id, project_id=project_id)
    provider = await _provider_for(repository)
    await _require_resolvable_revision(provider, reference, "reference")
    history = ChangeHistoryService(provider)
    result = (
        await history.file_history(path, revision=reference, limit=limit)
        if path
        else await history.recent_changes(reference, limit=limit)
    )
    return HistoryResponse(
        repository_id=repository.id,
        revision=reference,
        path=path,
        items=[CommitResponse(**row) for row in result.commits],
        truncated=result.truncated,
        reason=result.reason,
    )


@router.get(
    "/projects/{project_id}/repositories/{repository_id}/blame",
    response_model=BlameResponse,
)
async def repository_blame(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    path: str = Query(..., min_length=1, max_length=1024),
    reference: Optional[str] = Query(None, max_length=512),
) -> BlameResponse:
    await require_project(db, project_id)
    repository = await _get_repository(db, repository_id, project_id=project_id)
    provider = await _provider_for(repository)
    await _require_resolvable_revision(provider, reference, "reference")
    rows, error = await ChangeHistoryService(provider).get_blame(path, reference)
    return BlameResponse(
        repository_id=repository.id,
        path=path,
        items=[BlameLineResponse(**row) for row in rows],
        truncated=len(rows) >= settings.CODE_BLAME_MAX_LINES,
        reason=error,
    )


@router.get(
    "/projects/{project_id}/repositories/{repository_id}/diff",
    response_model=DiffResponse,
)
async def repository_diff(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    base: Optional[str] = Query(None, max_length=512),
    head: Optional[str] = Query(None, max_length=512),
    path: Optional[str] = Query(None, max_length=1024),
) -> DiffResponse:
    await require_project(db, project_id)
    repository = await _get_repository(db, repository_id, project_id=project_id)
    provider = await _provider_for(repository)
    if base is None:
        snapshot = (
            (
                await db.execute(
                    select(RepositorySnapshot)
                    .where(RepositorySnapshot.repository_id == repository.id)
                    .order_by(RepositorySnapshot.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        base = snapshot.commit_sha if snapshot else None
    #: A revision the repository does not contain is an error, not an empty diff.
    #: Returning ``200`` with no entries reads as "nothing changed between these
    #: two revisions" — the most misleading answer this endpoint can give — while
    #: the truth is that one of them does not exist.
    await _require_resolvable_revision(provider, base, "base")
    await _require_resolvable_revision(provider, head, "head")
    rows, error = await ChangeHistoryService(provider).commit_diff(
        base, head, path=path
    )
    return DiffResponse(
        repository_id=repository.id,
        base=base,
        head=head,
        items=[DiffEntryResponse(**row) for row in rows],
        truncated=len(rows) >= 200,
        reason=error,
    )


# ---------------------------------------------------------------------------
# Snapshots and code intelligence
# ---------------------------------------------------------------------------
@router.get("/snapshots/{snapshot_id}", response_model=SnapshotSummaryResponse)
async def snapshot_summary(
    snapshot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
) -> SnapshotSummaryResponse:
    snapshot = await _get_snapshot(db, snapshot_id, project_id=project_id)
    knowledge = CodeKnowledgeService(db)
    summary = await knowledge.snapshot_summary(snapshot.id)
    metadata = dict(snapshot.index_metadata or {})
    limitations = []
    if snapshot.status is SnapshotStatus.PARTIAL:
        limitations.append(
            "some files could not be fully interpreted; symbols for them are missing"
        )
    if metadata.get("files_heuristic"):
        limitations.append(
            f"{metadata['files_heuristic']} JS/TS file(s) were scanned structurally: "
            "their symbols are reliable, their edges are best-effort"
        )
    if not snapshot.commit_sha:
        limitations.append(
            "this snapshot has no commit sha, so it cannot be pinned to a deployed revision"
        )
    return SnapshotSummaryResponse(
        snapshot=_snapshot_response(snapshot),
        files=int(summary.get("files") or 0),
        symbols=int(summary.get("symbols") or 0),
        relationships=int(summary.get("relationships") or 0),
        references=int(summary.get("references") or 0),
        tests=int(summary.get("tests") or 0),
        languages=dict(summary.get("languages_by_count") or {}),
        framework=metadata.get("framework") or metadata.get("detected_framework"),
        signals_by_type=dict(summary.get("signals_by_type") or {}),
        limitations=limitations,
    )


@router.get("/snapshots/{snapshot_id}/files", response_model=CodeFileListResponse)
async def snapshot_files(
    snapshot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
    path: Optional[str] = Query(None, max_length=1024),
    language: Optional[str] = Query(None, max_length=32),
    tests_only: bool = Query(False),
    limit: int = Query(DEFAULT_ITEMS, ge=1, le=MAX_FILES),
) -> CodeFileListResponse:
    snapshot = await _get_snapshot(db, snapshot_id, project_id=project_id)
    stmt = select(CodeFile).where(CodeFile.snapshot_id == snapshot.id)
    if path:
        stmt = stmt.where(CodeFile.path.ilike(f"%{path}%"))
    if language:
        stmt = stmt.where(CodeFile.language == language)
    if tests_only:
        stmt = stmt.where(CodeFile.is_test.is_(True))
    rows = (await db.execute(stmt.order_by(CodeFile.path).limit(limit))).scalars().all()
    counts: dict[Any, int] = {
        row[0]: int(row[1])
        for row in (
            await db.execute(
                select(CodeSymbol.file_id, func.count(CodeSymbol.id))
                .where(CodeSymbol.snapshot_id == snapshot.id)
                .group_by(CodeSymbol.file_id)
            )
        ).all()
    }
    return CodeFileListResponse(
        items=[
            CodeFileResponse(
                id=row.id,
                path=row.path,
                language=row.language,
                module_name=row.module_name,
                size_bytes=row.size_bytes,
                line_count=row.line_count,
                is_test=row.is_test,
                parse_status=row.parse_status,
                parse_error=row.parse_error,
                last_commit_sha=row.last_commit_sha,
                last_modified_at=row.last_modified_at,
                last_author=row.last_author,
                symbol_count=counts.get(row.id, 0),
            )
            for row in rows
        ],
        total=len(rows),
        truncated=len(rows) >= limit,
    )


@router.get("/snapshots/{snapshot_id}/symbols", response_model=SymbolListResponse)
async def search_symbols(
    snapshot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
    query: str = Query(..., min_length=1, max_length=512),
    file_path: Optional[str] = Query(None, max_length=1024),
    limit: int = Query(DEFAULT_ITEMS, ge=1, le=MAX_ITEMS),
) -> SymbolListResponse:
    snapshot = await _get_snapshot(db, snapshot_id, project_id=project_id)
    knowledge = CodeKnowledgeService(db)
    symbols = await knowledge.find_symbol(
        snapshot.id, query, limit=limit, file_path=file_path
    )
    hits = await knowledge.symbol_hits(snapshot.id, symbols)
    return SymbolListResponse(
        items=[
            _symbol_response(
                hit.symbol,
                caller_count=hit.caller_count,
                callee_count=hit.callee_count,
                signals=[_signal_response(row) for row in hit.signals],
            )
            for hit in hits
        ],
        total=len(hits),
        truncated=len(hits) >= limit,
    )


@router.get(
    "/snapshots/{snapshot_id}/symbols/{symbol_id}", response_model=SymbolDetailResponse
)
async def symbol_detail(
    snapshot_id: uuid.UUID,
    symbol_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(25, ge=1, le=50),
) -> SymbolDetailResponse:
    snapshot = await _get_snapshot(db, snapshot_id, project_id=project_id)
    knowledge = CodeKnowledgeService(db)
    symbol = await knowledge.symbol_by_id(symbol_id)
    if symbol is None or symbol.snapshot_id != snapshot.id:
        #: A symbol from another snapshot is not part of this revision.
        raise HTTPException(status_code=404, detail="Symbol not found in this snapshot")
    hits = await knowledge.symbol_hits(snapshot.id, [symbol])
    callers = await knowledge.find_callers(symbol.id, limit=limit)
    callees = await knowledge.find_callees(symbol.id, limit=limit)
    references = await knowledge.find_references(
        snapshot.id, symbol.symbol_name, limit=limit
    )
    related = await knowledge.find_related_files(symbol.id, limit=limit)
    hit = hits[0] if hits else None
    return SymbolDetailResponse(
        **_symbol_response(
            symbol,
            caller_count=len(callers),
            callee_count=len(callees),
            signals=[_signal_response(row) for row in (hit.signals if hit else [])],
        ).model_dump(),
        source=symbol.source,
        callers=[
            SymbolEdgeResponse(
                qualified_name=target.qualified_name,
                file_path=target.file_path,
                start_line=target.start_line,
                end_line=target.end_line,
                relationship=edge.relationship_type,
                confidence=edge.confidence,
                line=edge.line,
                reference=f"FILE:{target.file_path}:{target.start_line}-{target.end_line}",
            )
            for edge, target in callers
        ],
        callees=[
            SymbolEdgeResponse(
                qualified_name=target.qualified_name,
                file_path=target.file_path,
                start_line=target.start_line,
                end_line=target.end_line,
                relationship=edge.relationship_type,
                confidence=edge.confidence,
                line=edge.line,
                reference=f"FILE:{target.file_path}:{target.start_line}-{target.end_line}",
            )
            for edge, target in callees
        ],
        related_files=related,
        references=[
            ReferenceResponse(
                name=row.name,
                file_path=row.file_path,
                line=row.line,
                kind=row.reference_kind,
                resolved=row.symbol_id is not None,
                symbol_id=row.symbol_id,
            )
            for row in references
        ],
    )


@router.get("/snapshots/{snapshot_id}/search", response_model=CodeSearchResponse)
async def code_search(
    snapshot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    query: str = Query(..., min_length=1, max_length=512),
    project_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(DEFAULT_ITEMS, ge=1, le=MAX_ITEMS),
) -> CodeSearchResponse:
    snapshot = await _get_snapshot(db, snapshot_id, project_id=project_id)
    found = await CodeKnowledgeService(db).search_code(snapshot.id, query, limit=limit)
    symbols = found["symbols"]
    sources = found["sources"]
    references = found["references"]
    return CodeSearchResponse(
        snapshot_id=snapshot.id,
        query=query,
        symbols=[_symbol_response(row) for row in symbols],
        source_matches=[_symbol_response(row) for row in sources],
        references=[
            ReferenceResponse(
                name=row.name,
                file_path=row.file_path,
                line=row.line,
                kind=row.reference_kind,
                resolved=row.symbol_id is not None,
                symbol_id=row.symbol_id,
            )
            for row in references
        ],
        truncated=(
            len(symbols) >= limit or len(sources) >= limit or len(references) >= limit
        ),
    )


@router.get(
    "/snapshots/{snapshot_id}/risk-signals", response_model=RiskSignalListResponse
)
async def risk_signals(
    snapshot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
    file_path: Optional[str] = Query(None, max_length=1024),
    symbol_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(DEFAULT_ITEMS, ge=1, le=MAX_ITEMS),
) -> RiskSignalListResponse:
    snapshot = await _get_snapshot(db, snapshot_id, project_id=project_id)
    stmt = select(CodeRiskSignal).where(CodeRiskSignal.snapshot_id == snapshot.id)
    if file_path:
        stmt = stmt.where(CodeRiskSignal.file_path == file_path)
    if symbol_id:
        stmt = stmt.where(CodeRiskSignal.symbol_id == symbol_id)
    rows = (
        (await db.execute(stmt.order_by(CodeRiskSignal.value.desc()).limit(limit)))
        .scalars()
        .all()
    )
    by_type: dict[str, int] = {}
    for row in rows:
        by_type[row.signal_type.value] = by_type.get(row.signal_type.value, 0) + 1
    return RiskSignalListResponse(
        items=[_signal_response(row) for row in rows],
        total=len(rows),
        by_type=by_type,
    )


# ---------------------------------------------------------------------------
# Trace → code
# ---------------------------------------------------------------------------
@router.get(
    "/incidents/{incident_id}/code-mappings", response_model=TraceMappingListResponse
)
async def incident_code_mappings(
    incident_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
    snapshot_id: Optional[uuid.UUID] = Query(None),
    refresh: bool = Query(
        False, description="Recompute the mappings against the current snapshot."
    ),
) -> TraceMappingListResponse:
    """The trace→code mapping for an incident (§15).

    ``refresh`` recomputes; otherwise the stored mappings are returned. Both
    paths state which snapshot they were computed against, because a mapping is
    only meaningful for one revision.
    """
    incident = await require_incident(db, incident_id, project_id=project_id)
    snapshot: Optional[RepositorySnapshot] = None
    if snapshot_id is not None:
        snapshot = await _get_snapshot(db, snapshot_id, project_id=incident.project_id)
    else:
        snapshot = (
            (
                await db.execute(
                    select(RepositorySnapshot)
                    .where(
                        RepositorySnapshot.project_id == incident.project_id,
                        RepositorySnapshot.status.in_(
                            [SnapshotStatus.READY, SnapshotStatus.PARTIAL]
                        ),
                    )
                    .order_by(RepositorySnapshot.indexed_at.desc().nullslast())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    if refresh:
        if snapshot is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "no indexed snapshot is available for this project; index the "
                    "repository before mapping its traces"
                ),
            )
        await TraceCodeMapper(db).map_incident(incident, snapshot)
        await db.commit()

    stmt = select(TraceCodeMapping).where(
        TraceCodeMapping.project_id == incident.project_id
    )
    if snapshot is not None:
        stmt = stmt.where(TraceCodeMapping.snapshot_id == snapshot.id)
    rows = (
        (
            await db.execute(
                stmt.order_by(TraceCodeMapping.confidence.desc()).limit(MAX_ITEMS)
            )
        )
        .scalars()
        .all()
    )
    reasons: dict[str, int] = {}
    for row in rows:
        if row.unmapped_reason:
            reasons[row.unmapped_reason] = reasons.get(row.unmapped_reason, 0) + 1
    mapped = sum(1 for row in rows if row.symbol_id or row.file_path)
    return TraceMappingListResponse(
        incident_id=incident.id,
        snapshot_id=snapshot.id if snapshot else None,
        items=[
            TraceMappingResponse(
                id=row.id,
                snapshot_id=row.snapshot_id,
                component_id=row.component_id,
                trace_id=row.trace_id,
                span_id=row.span_id,
                operation=row.operation,
                service_name=row.service_name,
                endpoint=row.endpoint,
                http_method=row.http_method,
                mapping_kind=row.mapping_kind,
                symbol_id=row.symbol_id,
                file_path=row.file_path,
                start_line=row.start_line,
                end_line=row.end_line,
                confidence=row.confidence,
                evidence=row.evidence,
                unmapped_reason=row.unmapped_reason,
                reference=(
                    f"FILE:{row.file_path}:{row.start_line}-{row.end_line}"
                    if row.file_path
                    else None
                ),
            )
            for row in rows
        ],
        total=len(rows),
        mapped=mapped,
        unmapped=len(rows) - mapped,
        unmapped_reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Debug sessions
# ---------------------------------------------------------------------------
async def _resolve_session_assets(
    db: AsyncSession,
    incident,
    *,
    repository_id: Optional[uuid.UUID],
    snapshot_id: Optional[uuid.UUID],
):
    """Pick the repository and snapshot a session should be pinned to.

    Preference: an explicitly named snapshot, then an explicitly named
    repository's newest snapshot, then the newest snapshot in the project. The
    deployment-derived revision is *not* silently indexed here — indexing is an
    explicit act, and a session that cannot be pinned says so instead.
    """
    repository: Optional[CodeRepository] = None
    snapshot: Optional[RepositorySnapshot] = None
    if snapshot_id is not None:
        snapshot = await _get_snapshot(db, snapshot_id, project_id=incident.project_id)
        repository = await db.get(CodeRepository, snapshot.repository_id)
        return repository, snapshot
    if repository_id is not None:
        repository = await _get_repository(
            db, repository_id, project_id=incident.project_id
        )
        snapshot = (
            (
                await db.execute(
                    select(RepositorySnapshot)
                    .where(
                        RepositorySnapshot.repository_id == repository.id,
                        RepositorySnapshot.status.in_(
                            [SnapshotStatus.READY, SnapshotStatus.PARTIAL]
                        ),
                    )
                    .order_by(RepositorySnapshot.indexed_at.desc().nullslast())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        return repository, snapshot
    snapshot = (
        (
            await db.execute(
                select(RepositorySnapshot)
                .where(
                    RepositorySnapshot.project_id == incident.project_id,
                    RepositorySnapshot.status.in_(
                        [SnapshotStatus.READY, SnapshotStatus.PARTIAL]
                    ),
                )
                .order_by(RepositorySnapshot.indexed_at.desc().nullslast())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if snapshot is not None:
        repository = await db.get(CodeRepository, snapshot.repository_id)
    return repository, snapshot


async def _session_detail(
    db: AsyncSession, session: DebugSession
) -> DebugSessionDetailResponse:
    snapshot = (
        await db.get(RepositorySnapshot, session.snapshot_id)
        if session.snapshot_id
        else None
    )
    repository = (
        await db.get(CodeRepository, session.repository_id)
        if session.repository_id
        else None
    )
    locations = (
        (
            await db.execute(
                select(DebugCodeLocation)
                .where(DebugCodeLocation.session_id == session.id)
                .order_by(DebugCodeLocation.validation, DebugCodeLocation.file_path)
                .limit(MAX_ITEMS)
            )
        )
        .scalars()
        .all()
    )
    hypotheses = (
        (
            await db.execute(
                select(DebugHypothesis)
                .where(DebugHypothesis.session_id == session.id)
                .order_by(DebugHypothesis.created_at.desc())
                .limit(MAX_ITEMS)
            )
        )
        .scalars()
        .all()
    )
    evidence = (
        (
            await db.execute(
                select(DebugEvidence)
                .where(DebugEvidence.session_id == session.id)
                .order_by(DebugEvidence.kind)
                .limit(MAX_ITEMS)
            )
        )
        .scalars()
        .all()
    )
    messages = (
        (
            await db.execute(
                select(DebugMessage)
                .where(DebugMessage.session_id == session.id)
                .order_by(DebugMessage.created_at)
                .limit(MAX_ITEMS)
            )
        )
        .scalars()
        .all()
    )
    latest = (
        (
            await db.execute(
                select(DebugAnalysisRun)
                .where(DebugAnalysisRun.session_id == session.id)
                .order_by(DebugAnalysisRun.started_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    by_hypothesis: dict[Any, list[DebugEvidence]] = {}
    for row in evidence:
        by_hypothesis.setdefault(row.hypothesis_id, []).append(row)
    valid = sum(1 for row in locations if row.validation is LocationValidation.VALID)
    repository_response = None
    if repository is not None:
        repository_response = _repository_response(
            repository, snapshot=snapshot, snapshot_count=1
        )
    return DebugSessionDetailResponse(
        **_session_response(session).model_dump(),
        snapshot=_snapshot_response(snapshot) if snapshot else None,
        repository=repository_response,
        latest_analysis=await _analysis_response(db, latest) if latest else None,
        locations=[_location_response(row) for row in locations],
        hypotheses=[
            _hypothesis_response(
                row,
                [loc for loc in locations if loc.hypothesis_id == row.id],
                by_hypothesis.get(row.id, []),
            )
            for row in hypotheses
        ],
        messages=[_message_response(row) for row in messages],
        counts={
            "locations": len(locations),
            "locations_valid": valid,
            "locations_rejected": len(locations) - valid,
            "hypotheses": len(hypotheses),
            "evidence": len(evidence),
            "messages": len(messages),
        },
    )


@router.post(
    "/incidents/{incident_id}/debug-sessions", response_model=DebugSessionDetailResponse
)
async def create_debug_session(
    incident_id: uuid.UUID,
    payload: DebugSessionCreateRequest,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
) -> DebugSessionDetailResponse:
    """Open a debugging session for an incident.

    Mutating, so ``project_id`` is required. When ``index_snapshot`` is set the
    deployment-derived revision is indexed first — the slow, thorough path. When
    it is not and no snapshot exists, the session is created anyway and says in
    ``version_note`` that no code version is pinned, because "no code snapshot"
    is itself a finding (§43), not a reason to refuse the request.
    """
    if project_id is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "project_id is required for this operation: it is what proves the "
                "caller may open an investigation on this incident"
            ),
        )
    await require_project(db, project_id)
    incident = await require_incident(db, incident_id, project_id=project_id)

    repository, snapshot = await _resolve_session_assets(
        db,
        incident,
        repository_id=payload.repository_id,
        snapshot_id=payload.snapshot_id,
    )
    if repository is None:
        repository = (
            (
                await db.execute(
                    select(CodeRepository)
                    .where(CodeRepository.project_id == project_id)
                    .order_by(CodeRepository.created_at)
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    if payload.index_snapshot and repository is not None:
        provider = await _provider_for(repository)
        resolution = await CodeVersionResolver(provider).resolve_for_incident(
            db, incident
        )
        snapshot = await CodeSnapshotService().get_or_create_snapshot(
            db,
            repository,
            resolution.reference,
            version_status=resolution.status,
            version_evidence=resolution.evidence,
            branch=resolution.branch,
            provider=provider,
        )
        await CodeIndexer(db).index(repository, snapshot, trigger="api")
        await db.refresh(snapshot)
        repository.index_status = (
            RepositoryIndexStatus.INDEXED
            if snapshot.status is SnapshotStatus.READY
            else RepositoryIndexStatus.PARTIAL
        )
        repository.last_indexed_at = snapshot.indexed_at or datetime.now(timezone.utc)
        repository.last_indexed_commit = snapshot.commit_sha

    manager = DebugSessionManager(db)
    session = await manager.create_session(
        project_id=project_id,
        incident=incident,
        repository=repository,
        snapshot=snapshot,
        created_by=payload.created_by,
        title=payload.title,
    )
    if payload.run_analysis:
        await manager.run_analysis(
            session, incident=incident, repository=repository, snapshot=snapshot
        )
    await db.commit()
    await db.refresh(session)
    return await _session_detail(db, session)


@router.get(
    "/incidents/{incident_id}/debug-sessions", response_model=DebugSessionListResponse
)
async def list_debug_sessions(
    incident_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(DEFAULT_ITEMS, ge=1, le=MAX_ITEMS),
) -> DebugSessionListResponse:
    incident = await require_incident(db, incident_id, project_id=project_id)
    rows = (
        (
            await db.execute(
                select(DebugSession)
                .where(DebugSession.incident_id == incident.id)
                .order_by(DebugSession.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return DebugSessionListResponse(
        items=[_session_response(row) for row in rows], total=len(rows)
    )


@router.get("/debug-sessions/{session_id}", response_model=DebugSessionDetailResponse)
async def get_debug_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
) -> DebugSessionDetailResponse:
    session = await _get_debug_session(db, session_id, project_id=project_id)
    return await _session_detail(db, session)


@router.post(
    "/debug-sessions/{session_id}/analyze", response_model=DebugAnalysisResponse
)
async def analyze_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
    question: Optional[str] = Query(None, max_length=2000),
) -> DebugAnalysisResponse:
    """Run the debugger over a session's incident.

    The analysis is bounded by ``DEBUG_MAX_ANALYSIS_SECONDS`` and a provider
    failure degrades to the deterministic investigation rather than failing the
    request. A degraded run is returned as ``DEGRADED`` with its reason, never as
    an empty success.
    """
    await _get_debug_session(db, session_id, project_id=project_id, require_scope=True)
    session = await _get_debug_session(db, session_id, project_id=project_id)
    incident = await require_incident(
        db, session.incident_id, project_id=session.project_id
    )
    repository = (
        await db.get(CodeRepository, session.repository_id)
        if session.repository_id
        else None
    )
    snapshot = (
        await db.get(RepositorySnapshot, session.snapshot_id)
        if session.snapshot_id
        else None
    )
    manager = DebugSessionManager(db)
    outcome = await manager.run_analysis(
        session, incident=incident, repository=repository, snapshot=snapshot
    )
    await db.commit()
    return await _analysis_response(db, outcome.analysis_run)


@router.get(
    "/debug-sessions/{session_id}/analysis", response_model=DebugAnalysisResponse
)
async def latest_analysis(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
) -> DebugAnalysisResponse:
    await _get_debug_session(db, session_id, project_id=project_id)
    run = (
        (
            await db.execute(
                select(DebugAnalysisRun)
                .where(DebugAnalysisRun.session_id == session_id)
                .order_by(DebugAnalysisRun.started_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if run is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "this session has no analysis yet; POST /debug-sessions/{id}/analyze "
                "or read /investigation for the deterministic context"
            ),
        )
    return await _analysis_response(db, run)


@router.get(
    "/debug-sessions/{session_id}/locations",
    response_model=list[DebugCodeLocationResponse],
)
async def session_locations(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
    valid_only: bool = Query(False),
) -> list[DebugCodeLocationResponse]:
    await _get_debug_session(db, session_id, project_id=project_id)
    stmt = select(DebugCodeLocation).where(DebugCodeLocation.session_id == session_id)
    if valid_only:
        stmt = stmt.where(DebugCodeLocation.validation == LocationValidation.VALID)
    rows = (
        (await db.execute(stmt.order_by(DebugCodeLocation.file_path).limit(MAX_ITEMS)))
        .scalars()
        .all()
    )
    return [_location_response(row) for row in rows]


@router.get(
    "/debug-sessions/{session_id}/hypotheses",
    response_model=list[DebugHypothesisResponse],
)
async def session_hypotheses(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
) -> list[DebugHypothesisResponse]:
    await _get_debug_session(db, session_id, project_id=project_id)
    hypotheses = (
        (
            await db.execute(
                select(DebugHypothesis)
                .where(DebugHypothesis.session_id == session_id)
                .order_by(DebugHypothesis.created_at.desc())
                .limit(MAX_ITEMS)
            )
        )
        .scalars()
        .all()
    )
    locations = (
        (
            await db.execute(
                select(DebugCodeLocation).where(
                    DebugCodeLocation.session_id == session_id
                )
            )
        )
        .scalars()
        .all()
    )
    evidence = (
        (
            await db.execute(
                select(DebugEvidence).where(DebugEvidence.session_id == session_id)
            )
        )
        .scalars()
        .all()
    )
    return [
        _hypothesis_response(
            row,
            [loc for loc in locations if loc.hypothesis_id == row.id],
            [item for item in evidence if item.hypothesis_id == row.id],
        )
        for row in hypotheses
    ]


@router.get(
    "/debug-sessions/{session_id}/evidence", response_model=list[DebugEvidenceResponse]
)
async def session_evidence(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
    kind: Optional[str] = Query(None, max_length=32),
    valid_only: bool = Query(False),
) -> list[DebugEvidenceResponse]:
    await _get_debug_session(db, session_id, project_id=project_id)
    stmt = select(DebugEvidence).where(DebugEvidence.session_id == session_id)
    if kind:
        stmt = stmt.where(DebugEvidence.kind == kind.upper())
    if valid_only:
        stmt = stmt.where(DebugEvidence.valid.is_(True))
    rows = (
        (await db.execute(stmt.order_by(DebugEvidence.kind).limit(MAX_ITEMS)))
        .scalars()
        .all()
    )
    return [_evidence_response(row) for row in rows]


@router.get(
    "/debug-sessions/{session_id}/messages", response_model=list[DebugMessageResponse]
)
async def session_messages(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
) -> list[DebugMessageResponse]:
    await _get_debug_session(db, session_id, project_id=project_id)
    rows = (
        (
            await db.execute(
                select(DebugMessage)
                .where(DebugMessage.session_id == session_id)
                .order_by(DebugMessage.created_at)
                .limit(MAX_ITEMS)
            )
        )
        .scalars()
        .all()
    )
    return [_message_response(row) for row in rows]


@router.post(
    "/debug-sessions/{session_id}/messages", response_model=DebugAssistantAnswer
)
async def ask_debug_session(
    session_id: uuid.UUID,
    payload: DebugAskRequest,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
) -> DebugAssistantAnswer:
    """Ask a grounded follow-up question (§35).

    The answer is built from the session's own context and, when a provider is
    configured, bounded tool calls. Cited references that do not resolve are
    removed and reported in ``invalid_references`` — the answer never carries a
    fabricated citation.
    """
    await _get_debug_session(db, session_id, project_id=project_id, require_scope=True)
    session = await _get_debug_session(db, session_id, project_id=project_id)
    incident = await require_incident(
        db, session.incident_id, project_id=session.project_id
    )
    repository = (
        await db.get(CodeRepository, session.repository_id)
        if session.repository_id
        else None
    )
    snapshot = (
        await db.get(RepositorySnapshot, session.snapshot_id)
        if session.snapshot_id
        else None
    )
    manager = DebugSessionManager(db)
    try:
        answer = await manager.ask(
            session,
            payload.question,
            incident=incident,
            repository=repository,
            snapshot=snapshot,
            asked_by=payload.asked_by,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    await db.commit()
    return DebugAssistantAnswer(
        message_id=answer["message_id"],
        answer=answer["answer"],
        evidence=answer["evidence"],
        invalid_references=answer["invalid_references"],
        missing_evidence=answer["missing_evidence"],
        confidence=answer["confidence"],
        tool_calls=answer["tool_calls"],
        degraded_reason=answer["degraded_reason"],
        budget=answer["budget"],
    )


@router.get(
    "/debug-sessions/{session_id}/tools", response_model=list[DebugToolCallResponse]
)
async def session_tool_calls(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
) -> list[DebugToolCallResponse]:
    """The audit trail: which tools ran, what they were asked, how they ended."""
    await _get_debug_session(db, session_id, project_id=project_id)
    rows = (
        (
            await db.execute(
                select(DebugToolCall)
                .where(DebugToolCall.session_id == session_id)
                .order_by(DebugToolCall.started_at)
                .limit(MAX_ITEMS)
            )
        )
        .scalars()
        .all()
    )
    return [
        DebugToolCallResponse(
            id=row.id,
            tool_name=row.tool_name,
            arguments=row.arguments,
            status=row.status,
            result_summary=row.result_summary,
            result_count=row.result_count,
            result_bytes=row.result_bytes,
            truncated=row.truncated,
            error=row.error,
            started_at=row.started_at,
            duration_ms=row.duration_ms,
        )
        for row in rows
    ]


@router.get(
    "/debug-sessions/{session_id}/timeline", response_model=DebugTimelineResponse
)
async def session_timeline(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
) -> DebugTimelineResponse:
    """The debugging chain: deployment → anomaly → trace failure → code (§52).

    Built from stored rows only. Gaps are stated in ``notes`` rather than closed
    with an assumption, because a timeline that hides a missing link is worse than
    one that names it.
    """
    session = await _get_debug_session(db, session_id, project_id=project_id)
    incident = await require_incident(
        db, session.incident_id, project_id=session.project_id
    )
    items: list[DebugTimelineEvent] = []
    notes: list[str] = []

    onset = incident.started_at or incident.detected_at
    items.append(
        DebugTimelineEvent(
            at=onset,
            kind="INCIDENT_DETECTED",
            title="Incident detected",
            detail=incident.title,
            reference=f"INCIDENT:{incident.id}",
        )
    )
    from app.models.anomaly import Anomaly

    anomalies = (
        (
            await db.execute(
                select(Anomaly)
                .where(Anomaly.incident_id == incident.id)
                .order_by(Anomaly.detected_at)
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    for row in anomalies:
        items.append(
            DebugTimelineEvent(
                at=row.detected_at,
                kind="ANOMALY",
                title=f"{row.anomaly_type.value} on {row.metric_name or 'component'}",
                detail=row.description,
                reference=f"ANOMALY:{row.id}",
            )
        )
    if not anomalies:
        notes.append("no anomaly is linked to this incident")

    mappings = (
        (
            await db.execute(
                select(TraceCodeMapping)
                .where(
                    TraceCodeMapping.project_id == incident.project_id,
                    TraceCodeMapping.snapshot_id == session.snapshot_id,
                )
                .order_by(TraceCodeMapping.confidence.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    for mapping_row in mappings:
        items.append(
            DebugTimelineEvent(
                at=onset,
                kind="TRACE_TO_CODE",
                title=(
                    f"{mapping_row.mapping_kind.value}: "
                    f"{mapping_row.operation or mapping_row.endpoint or 'span'}"
                ),
                detail=(
                    f"{mapping_row.file_path}:{mapping_row.start_line}-"
                    f"{mapping_row.end_line}"
                    if mapping_row.file_path
                    else mapping_row.unmapped_reason
                ),
                reference=(
                    f"FILE:{mapping_row.file_path}:{mapping_row.start_line}-"
                    f"{mapping_row.end_line}"
                    if mapping_row.file_path
                    else None
                ),
            )
        )
    if not mappings:
        notes.append("no trace-to-code mapping exists for the session's snapshot")

    deployments = (
        (
            await db.execute(
                select(DeploymentEvent)
                .where(DeploymentEvent.project_id == incident.project_id)
                .order_by(DeploymentEvent.deployed_at.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    for deployment_row in deployments:
        items.append(
            DebugTimelineEvent(
                at=deployment_row.deployed_at,
                kind="DEPLOYMENT",
                title=(
                    f"deployment {deployment_row.deployment_id} "
                    f"({deployment_row.version or 'no version'})"
                ),
                detail=deployment_row.description,
                reference=(
                    f"COMMIT:{deployment_row.commit_sha}"
                    if deployment_row.commit_sha
                    else f"DEPLOYMENT:{deployment_row.deployment_id}"
                ),
            )
        )
    if not deployments:
        notes.append("no deployment is recorded in this project")

    analysis = (
        (
            await db.execute(
                select(DebugAnalysisRun)
                .where(DebugAnalysisRun.session_id == session.id)
                .order_by(DebugAnalysisRun.started_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if analysis is not None:
        items.append(
            DebugTimelineEvent(
                at=analysis.started_at,
                kind="ANALYSIS",
                title=f"ARGUS analysis ({analysis.status.value})",
                detail=analysis.error or (analysis.summary or "")[:400],
                reference=None,
            )
        )
    else:
        notes.append("this session has no analysis run yet")

    hypotheses = (
        (
            await db.execute(
                select(DebugHypothesis)
                .where(DebugHypothesis.session_id == session.id)
                .order_by(DebugHypothesis.created_at)
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    for hypothesis_row in hypotheses:
        items.append(
            DebugTimelineEvent(
                at=hypothesis_row.created_at,
                kind="HYPOTHESIS",
                title=(
                    f"{hypothesis_row.category.value} "
                    f"({hypothesis_row.validation_status.value})"
                ),
                detail=(hypothesis_row.description or "")[:400],
                reference=None,
            )
        )

    items.sort(key=lambda item: item.at)
    if session.snapshot_id is None:
        notes.append(
            "no code snapshot is pinned to this session; code-level events are absent "
            "because none could be established"
        )
    return DebugTimelineResponse(
        session_id=session.id, incident_id=incident.id, items=items, notes=notes
    )


@router.get(
    "/debug-sessions/{session_id}/investigation", response_model=InvestigationResponse
)
async def session_investigation(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
) -> InvestigationResponse:
    """The deterministic investigation (§43) — available with or without a model."""
    session = await _get_debug_session(db, session_id, project_id=project_id)
    incident = await require_incident(
        db, session.incident_id, project_id=session.project_id
    )
    snapshot = (
        await db.get(RepositorySnapshot, session.snapshot_id)
        if session.snapshot_id
        else None
    )
    context = await DebugContextBuilder(db).build(incident, snapshot)
    summary = context.sections.get("incident") or {}
    return InvestigationResponse(
        session_id=session.id,
        incident_id=incident.id,
        snapshot_id=session.snapshot_id,
        version_status=CodeVersionStatus(context.version_status),
        version_note=context.version_note,
        context_version=context.context_version,
        built_at=(
            datetime.fromisoformat(context.built_at) if context.built_at else None
        ),
        summary=(context.sections.get("incident") or {}).get("summary")
        or incident.summary
        or (summary.get("title") if isinstance(summary, dict) else "")
        or "",
        evidence=[item.as_dict() for item in context.evidence],
        sections=context.sections,
        caveats=context.caveats,
        budget=context.budget.as_dict() if context.budget else None,
        redaction=context.redaction,
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
@router.get("/debugger/metrics", response_model=DebuggerMetricsResponse)
async def debugger_metrics(
    db: AsyncSession = Depends(get_db),
    project_id: Optional[uuid.UUID] = Query(None),
) -> DebuggerMetricsResponse:
    """Coverage and honesty tallies for the debugger.

    Deliberately reports the counts that show where the debugger is *weak* —
    degraded analyses, rejected locations, refused tool calls — because those are
    the numbers that decide whether an analysis can be trusted.
    """
    if project_id is not None:
        await require_project(db, project_id)

    async def count(model, *conditions) -> int:
        stmt = select(func.count(model.id))
        if project_id is not None:
            stmt = stmt.where(model.project_id == project_id)
        for condition in conditions:
            stmt = stmt.where(condition)
        return int((await db.execute(stmt)).scalar_one() or 0)

    hypothesis_stmt = select(
        DebugHypothesis.validation_status, func.count(DebugHypothesis.id)
    )
    if project_id is not None:
        hypothesis_stmt = hypothesis_stmt.where(
            DebugHypothesis.project_id == project_id
        )
    by_status = {
        (key.value if hasattr(key, "value") else str(key)): value
        for key, value in (
            await db.execute(
                hypothesis_stmt.group_by(DebugHypothesis.validation_status)
            )
        ).all()
    }
    index_stmt = select(CodeRepository.index_status, func.count(CodeRepository.id))
    if project_id is not None:
        index_stmt = index_stmt.where(CodeRepository.project_id == project_id)
    index_status_rows = (
        await db.execute(index_stmt.group_by(CodeRepository.index_status))
    ).all()

    analyses = await count(DebugAnalysisRun)
    degraded = await count(
        DebugAnalysisRun, DebugAnalysisRun.status == DebugAnalysisStatus.DEGRADED
    )
    locations = await count(DebugCodeLocation)
    valid_locations = await count(
        DebugCodeLocation, DebugCodeLocation.validation == LocationValidation.VALID
    )
    tool_calls = await count(DebugToolCall)
    refused = await count(
        DebugToolCall, DebugToolCall.status == ToolCallStatus.REJECTED
    )
    invalid_stmt = select(DebugAnalysisRun.invalid_references)
    if project_id is not None:
        invalid_stmt = invalid_stmt.where(DebugAnalysisRun.project_id == project_id)
    invalid_refs = sum(
        len(list(row or [])) for row in (await db.execute(invalid_stmt)).scalars().all()
    )
    limitations = []
    if degraded:
        limitations.append(
            f"{degraded} analysis run(s) degraded to the deterministic investigation"
        )
    if locations - valid_locations:
        limitations.append(
            f"{locations - valid_locations} claimed location(s) were rejected as "
            "non-existent in the pinned snapshot"
        )
    if refused:
        limitations.append(
            f"{refused} tool call(s) were refused: unknown tool, out-of-snapshot path, "
            "or exhausted budget"
        )
    return DebuggerMetricsResponse(
        sessions=await count(DebugSession),
        sessions_completed=await count(
            DebugSession, DebugSession.status == DebugSessionStatus.COMPLETED
        ),
        analyses=analyses,
        analyses_degraded=degraded,
        hypotheses=await count(DebugHypothesis),
        by_validation_status=by_status,
        locations_claimed=locations,
        locations_valid=valid_locations,
        locations_rejected=locations - valid_locations,
        invalid_references=invalid_refs,
        tool_calls=tool_calls,
        tool_calls_refused=refused,
        repositories=await count(CodeRepository),
        snapshots=await count(RepositorySnapshot),
        index_status={
            (key.value if hasattr(key, "value") else str(key)): value
            for key, value in index_status_rows
        },
        limitations=limitations,
    )


__all__ = ["router"]
