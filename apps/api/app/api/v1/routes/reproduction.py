"""ARGUS Reproduction Routes (Phase 5 §44–§51).

The investigation surface for failure reproduction:

```text
POST   /incidents/{id}/reproductions         plan an experiment (never executes)
GET    /incidents/{id}/reproductions         experiment history for an incident
GET    /reproductions                        experiments in a project scope
GET    /reproductions/{rid}                  one experiment, fully hydrated
POST   /reproductions/{rid}/start            execute (explicit confirmation only)
POST   /reproductions/{rid}/cancel           stop a running experiment
POST   /reproductions/{rid}/retry            plan a fresh attempt, same hypothesis
GET    /reproductions/{rid}/plan             the explicit plan
GET    /reproductions/{rid}/safety           the §47 confirmation payload
GET    /reproductions/{rid}/status           live status + progress
GET    /reproductions/{rid}/inputs           the planned replay inputs
GET    /reproductions/{rid}/telemetry        captured, namespaced telemetry
GET    /reproductions/{rid}/artifacts        immutable artifacts with hashes
GET    /reproductions/{rid}/comparison       per-run comparison + formulas
GET    /reproductions/{rid}/validation       the verdict, evidence and limits
GET    /reproductions/{rid}/environment      original vs sandbox snapshots
GET    /reproductions/{rid}/faults           every injected fault and its impact
GET    /reproductions/{rid}/manifest         the §43 manifest
```

Isolation rules, made explicit because this phase can *execute* things:

* a mutating request (start / cancel / retry) **requires** ``project_id`` and the
  experiment must belong to it — knowing a UUID is not authority to run it;
* reads accept an optional ``project_id``; when supplied it is enforced, and an
  out-of-scope id answers 404 rather than leaking the row;
* ``POST /start`` requires ``confirm_sandbox=true`` in the body, so the safety
  confirmation page (§47) is a real gate rather than decoration;
* a request may name only a *logical* sandbox service, an HTTP method/path and a
  *typed* fault. No URL, image, command or free-form parameter reaches execution,
  which is what keeps §57 (no arbitrary code execution) true at the boundary.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_incident, require_project
from app.core.config import get_settings
from app.core.database import async_session_factory, get_db
from app.models.causal import CausalAnalysis, RootCauseCandidate
from app.models.reproduction import (
    EnvironmentSnapshot,
    ExperimentStatus,
    SandboxBackendKind,
    ObservationStatus,
    ReproductionArtifact,
    ReproductionComparison,
    ReproductionExperiment,
    ReproductionFault,
    ReproductionHypothesis,
    ReproductionInput,
    ReproductionObservation,
    ReproductionPlan,
    ReproductionRun,
    ReproductionSandbox,
    ReproductionValidation,
    RunStatus,
    SandboxStatus,
)
from app.schemas.reproduction import (
    CancelReproductionRequest,
    CreateReproductionRequest,
    EnvironmentSnapshotResponse,
    ExperimentProgress,
    FaultSpecRequest,
    ReproductionArtifactListResponse,
    ReproductionArtifactResponse,
    ReproductionComparisonListResponse,
    ReproductionComparisonResponse,
    ReproductionExperimentDetailResponse,
    ReproductionExperimentListResponse,
    ReproductionExperimentResponse,
    ReproductionFaultListResponse,
    ReproductionFaultResponse,
    ReproductionHistoryEntry,
    ReproductionHistoryResponse,
    ReproductionHypothesisResponse,
    ReproductionInputResponse,
    ReproductionManifestResponse,
    ReproductionMetricsResponse,
    ReproductionObservationResponse,
    ReproductionPlanResponse,
    ReproductionRunResponse,
    ReproductionSafetyPreviewResponse,
    ReproductionSandboxResponse,
    ReproductionStatusResponse,
    ReproductionTelemetryResponse,
    ReproductionValidationResponse,
    ResourceUsage,
    StartReproductionRequest,
)
from app.services.causal_explanation import CausalExplanationService
from app.services.queue import enqueue_reproduction_run
from app.services.replay_engine import ReplayItem
from app.services.reproduction_orchestrator import ReproductionOrchestrator
from app.services.reproduction_planner import PlanningError
from app.services.reproduction_context import ensure_utc
from app.services.reproduction_sandbox import (
    DEFAULT_TEMPLATE,
    SandboxError,
    backend_for,
    load_template,
    sandbox_key_for,
    sandbox_metrics,
)
from app.services.reproduction_state import (
    EXPERIMENT_HAPPY_PATH,
    experiment_transitions_as_values,
    is_experiment_terminal,
)

settings = get_settings()

router = APIRouter(tags=["reproduction"])

#: Nested collections are bounded: an unbounded telemetry dump is not an API.
_MAX_ITEMS = 500
_MAX_HISTORY = 50

_DEFAULT_TEMPLATE = DEFAULT_TEMPLATE


def _enum_text(value: Any) -> str:
    """A nullable enum's plain name for a tally key.

    ``str()`` is wrong here: these enums mix in ``str``, so ``str(member)``
    renders ``"ExperimentStatus.PLANNED"`` while ``.value`` renders
    ``"PLANNED"``. The latter is what a metrics consumer expects.
    """
    if value is None:
        return "UNKNOWN"
    return str(getattr(value, "value", value))


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------
async def _get_experiment(
    db: AsyncSession,
    experiment_id: uuid.UUID,
    *,
    project_id: Optional[uuid.UUID] = None,
    require_scope: bool = False,
) -> ReproductionExperiment:
    """Fetch an experiment, enforcing project scope when it is claimed."""
    if require_scope and project_id is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "project_id is required for this operation: it is what proves the "
                "caller owns the experiment they are asking ARGUS to execute"
            ),
        )
    experiment = await db.get(ReproductionExperiment, experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="Reproduction experiment not found")
    if project_id is not None and experiment.project_id != project_id:
        # 404 rather than 403: an out-of-scope experiment is simply not visible.
        raise HTTPException(status_code=404, detail="Reproduction experiment not found")
    return experiment


async def _assert_incident_scope(
    db: AsyncSession, experiment: ReproductionExperiment
) -> None:
    """Defence in depth: the incident must live in the experiment's own project."""
    await require_incident(db, experiment.incident_id, project_id=experiment.project_id)


async def _template_for(experiment: ReproductionExperiment) -> dict:
    """The system template an experiment was planned against."""
    metadata = dict(experiment.metadata_ or {})
    name = metadata.get("template") or _DEFAULT_TEMPLATE
    try:
        return load_template(str(name))
    except SandboxError:  # pragma: no cover - template ships with the image
        return {"aliases": {}, "services": []}


def _hydrated(
    *,
    experiment: ReproductionExperiment,
    plan: Optional[ReproductionPlan],
    hypothesis: Optional[ReproductionHypothesis],
    runs: list[ReproductionRun],
    validation: Optional[ReproductionValidation],
    sandbox: Optional[ReproductionSandbox],
    faults: list[ReproductionFault],
    input_count: int,
) -> ReproductionExperimentDetailResponse:
    return ReproductionExperimentDetailResponse(
        experiment=ReproductionExperimentResponse.model_validate(experiment),
        plan=(
            ReproductionPlanResponse.model_validate(plan) if plan is not None else None
        ),
        hypothesis=(
            ReproductionHypothesisResponse.model_validate(hypothesis)
            if hypothesis is not None
            else None
        ),
        runs=[ReproductionRunResponse.model_validate(run) for run in runs],
        validation=(
            ReproductionValidationResponse.model_validate(validation)
            if validation is not None
            else None
        ),
        sandbox=(
            ReproductionSandboxResponse.model_validate(sandbox)
            if sandbox is not None
            else None
        ),
        faults=[ReproductionFaultResponse.model_validate(row) for row in faults],
        input_count=input_count,
        available_transitions=list(experiment_transitions_as_values(experiment.status)),
    )


async def _hydrate(
    db: AsyncSession, experiment: ReproductionExperiment
) -> ReproductionExperimentDetailResponse:
    """Load the full experiment graph in a handful of bounded queries."""
    plan = (
        await db.execute(
            select(ReproductionPlan).where(
                ReproductionPlan.experiment_id == experiment.id
            )
        )
    ).scalar_one_or_none()
    hypothesis = (
        await db.execute(
            select(ReproductionHypothesis).where(
                ReproductionHypothesis.experiment_id == experiment.id
            )
        )
    ).scalar_one_or_none()
    runs = list(
        (
            await db.execute(
                select(ReproductionRun)
                .where(ReproductionRun.experiment_id == experiment.id)
                .order_by(ReproductionRun.run_index)
            )
        )
        .scalars()
        .all()
    )
    validation = (
        await db.execute(
            select(ReproductionValidation).where(
                ReproductionValidation.experiment_id == experiment.id
            )
        )
    ).scalar_one_or_none()
    faults = list(
        (
            await db.execute(
                select(ReproductionFault)
                .where(ReproductionFault.experiment_id == experiment.id)
                .order_by(ReproductionFault.created_at)
            )
        )
        .scalars()
        .all()
    )
    sandbox = (
        await db.execute(
            select(ReproductionSandbox)
            .where(ReproductionSandbox.experiment_id == experiment.id)
            .order_by(ReproductionSandbox.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    input_count = int(
        await db.scalar(
            select(func.count(ReproductionInput.id)).where(
                ReproductionInput.experiment_id == experiment.id
            )
        )
        or 0
    )
    return _hydrated(
        experiment=experiment,
        plan=plan,
        hypothesis=hypothesis,
        runs=runs,
        validation=validation,
        sandbox=sandbox,
        faults=faults,
        input_count=input_count,
    )


async def _load_plan(
    db: AsyncSession, experiment_id: uuid.UUID
) -> Optional[ReproductionPlan]:
    return (
        await db.execute(
            select(ReproductionPlan).where(
                ReproductionPlan.experiment_id == experiment_id
            )
        )
    ).scalar_one_or_none()


async def _primary_candidate(
    db: AsyncSession, analysis: CausalAnalysis
) -> Optional[RootCauseCandidate]:
    if analysis.primary_candidate_id is not None:
        candidate = await db.get(RootCauseCandidate, analysis.primary_candidate_id)
        if candidate is not None:
            return candidate
    return (
        await db.execute(
            select(RootCauseCandidate)
            .where(RootCauseCandidate.analysis_id == analysis.id)
            .order_by(RootCauseCandidate.score.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _fault_override(fault: FaultSpecRequest) -> dict:
    """Flatten a validated fault request into the planner's override shape.

    Every enum-ish field goes through :func:`_enum_text` because
    ``BaseSchema`` sets ``use_enum_values=True``: a *request* model hands back
    the plain value (``"LATENCY"``), while the planner also accepts enum
    members from internal callers. Reading ``.value`` here would either crash on
    the request path or mangle the name on the internal one — this handles both.
    """
    return {
        "fault_type": _enum_text(fault.fault_type),
        "target": fault.target,
        "trigger": _enum_text(fault.trigger),
        "duration_ms": fault.duration_ms,
        "intensity": fault.intensity,
        "parameters": fault.parameters,
        "after_replay_index": fault.after_replay_index,
        "at_offset_ms": fault.at_offset_ms,
    }


async def _candidate_evidence(
    db: AsyncSession, analysis: CausalAnalysis, candidate: RootCauseCandidate
) -> list[dict]:
    """A bounded, quotable evidence list for the plan's objectives."""
    service = CausalExplanationService(db)
    loaded = await service.load_analysis(
        project_id=analysis.project_id,
        incident_id=analysis.incident_id,
        analysis_id=analysis.id,
    )
    if loaded is None:
        return []
    return [
        {
            "category": item.category.value,
            "polarity": item.polarity.value,
            "quote": item.quote,
            "strength": item.strength,
        }
        for item in loaded.evidence_for(candidate.id)[:20]
    ]


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
@router.post(
    "/incidents/{incident_id}/reproductions",
    response_model=ReproductionExperimentDetailResponse,
    status_code=201,
)
async def create_reproduction(
    incident_id: uuid.UUID,
    payload: Optional[CreateReproductionRequest] = None,
    project_id: Optional[uuid.UUID] = Query(
        None, description="Scope the incident to a project (recommended)"
    ),
    environment_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> ReproductionExperimentDetailResponse:
    """Plan an experiment for an incident's hypothesis. **Never executes it.**

    Planning and execution are separate calls on purpose: a plan is something an
    engineer reviews, and §47 requires explicit confirmation before anything runs.
    """
    if not settings.REPRODUCTION_ENABLED:
        raise HTTPException(
            status_code=409,
            detail=(
                "Failure reproduction is disabled (REPRODUCTION_ENABLED=false). "
                "Existing experiments remain queryable."
            ),
        )
    incident = await require_incident(
        db, incident_id, project_id=project_id, environment_id=environment_id
    )
    request = payload or CreateReproductionRequest()

    # A hypothesis must come from a causal analysis: reproducing a guess is not
    # an experiment.
    analysis: Optional[CausalAnalysis] = None
    if request.causal_analysis_id is not None:
        analysis = await db.get(CausalAnalysis, request.causal_analysis_id)
        if (
            analysis is None
            or analysis.project_id != incident.project_id
            or analysis.incident_id != incident.id
        ):
            raise HTTPException(status_code=404, detail="Causal analysis not found")
    else:
        analysis = (
            await db.execute(
                select(CausalAnalysis)
                .where(
                    CausalAnalysis.project_id == incident.project_id,
                    CausalAnalysis.incident_id == incident.id,
                )
                .order_by(CausalAnalysis.analysis_version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if analysis is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "This incident has no causal analysis, so there is no hypothesis to "
                "reproduce. Run POST /api/v1/incidents/{id}/analyze first."
            ),
        )

    candidate: Optional[RootCauseCandidate] = None
    if request.candidate_id is not None:
        candidate = await db.get(RootCauseCandidate, request.candidate_id)
        if candidate is None or candidate.analysis_id != analysis.id:
            raise HTTPException(
                status_code=404,
                detail="Root-cause candidate not found in this analysis",
            )
    else:
        candidate = await _primary_candidate(db, analysis)
    if candidate is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "The analysis has no candidate to reproduce — ARGUS did not find "
                "enough evidence to form a hypothesis. Run a fresh analysis once "
                "more telemetry is available."
            ),
        )

    evidence = await _candidate_evidence(db, analysis, candidate)
    input_overrides = [
        ReplayItem(
            method=item.method,
            target_service=item.target_service,
            target_path=item.target_path,
            payload=dict(item.payload or {}),
            relative_offset_ms=item.relative_offset_ms,
            source=item.source,
        )
        for item in (request.inputs or [])
    ]

    orchestrator = ReproductionOrchestrator(async_session_factory)
    try:
        created = await orchestrator.prepare(
            incident=incident,
            candidate=candidate,
            candidate_evidence=evidence,
            analysis_id=analysis.id,
            repetitions=request.repetitions,
            replay_mode=request.replay_mode,
            network_policy=request.network_policy,
            timeout_seconds=request.timeout_seconds,
            strategy=request.strategy,
            fault_overrides=[
                _fault_override(fault) for fault in (request.faults or [])
            ],
            input_overrides=input_overrides,
            requested_by=request.requested_by,
            trigger="api",
        )
    except PlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SandboxError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Re-read through the request's session so every object in the response is
    # bound to one identity map and one transaction.
    experiment = await _get_experiment(db, created.id)
    return await _hydrate(db, experiment)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
@router.get(
    "/incidents/{incident_id}/reproductions",
    response_model=ReproductionHistoryResponse,
)
async def list_incident_reproductions(
    incident_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(_MAX_HISTORY, ge=1, le=_MAX_HISTORY),
    db: AsyncSession = Depends(get_db),
) -> ReproductionHistoryResponse:
    """An incident's experiment history (§51), newest version first."""
    incident = await require_incident(db, incident_id, project_id=project_id)
    experiments = list(
        (
            await db.execute(
                select(ReproductionExperiment)
                .where(ReproductionExperiment.incident_id == incident.id)
                .order_by(ReproductionExperiment.experiment_version.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    items: list[ReproductionHistoryEntry] = []
    for experiment in experiments:
        validation = (
            await db.execute(
                select(ReproductionValidation).where(
                    ReproductionValidation.experiment_id == experiment.id
                )
            )
        ).scalar_one_or_none()
        hypothesis = (
            await db.execute(
                select(ReproductionHypothesis).where(
                    ReproductionHypothesis.experiment_id == experiment.id
                )
            )
        ).scalar_one_or_none()
        metadata = dict(experiment.metadata_ or {})
        duration = metadata.get("duration_ms")
        if duration is None and experiment.started_at and experiment.completed_at:
            duration = int(
                (experiment.completed_at - experiment.started_at).total_seconds() * 1000
            )
        items.append(
            ReproductionHistoryEntry(
                experiment_id=experiment.id,
                experiment_version=experiment.experiment_version,
                status=experiment.status,
                result=experiment.result,
                outcome=validation.outcome if validation is not None else None,
                confidence=experiment.confidence,
                duration_ms=duration,
                repetitions=experiment.repetitions,
                created_at=experiment.created_at,
                completed_at=experiment.completed_at,
                hypothesis=hypothesis.statement if hypothesis is not None else None,
                summary=experiment.summary,
            )
        )
    return ReproductionHistoryResponse(
        incident_id=incident.id, items=items, total=len(items)
    )


@router.get("/reproductions", response_model=ReproductionExperimentListResponse)
async def list_reproductions(
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    incident_id: Optional[uuid.UUID] = Query(None),
    status: Optional[ExperimentStatus] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> ReproductionExperimentListResponse:
    """Experiments in a project scope, newest first."""
    await require_project(db, project_id)
    filters = [ReproductionExperiment.project_id == project_id]
    if incident_id is not None:
        filters.append(ReproductionExperiment.incident_id == incident_id)
    if status is not None:
        filters.append(ReproductionExperiment.status == status)

    total = int(
        await db.scalar(select(func.count(ReproductionExperiment.id)).where(*filters))
        or 0
    )
    rows = list(
        (
            await db.execute(
                select(ReproductionExperiment)
                .where(*filters)
                .order_by(ReproductionExperiment.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return ReproductionExperimentListResponse(
        items=[ReproductionExperimentResponse.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


@router.get("/reproductions/metrics", response_model=ReproductionMetricsResponse)
async def get_reproduction_metrics(
    project_id: uuid.UUID = Query(..., description="Project scope (required)"),
    db: AsyncSession = Depends(get_db),
) -> ReproductionMetricsResponse:
    """How ARGUS's own experiments are behaving (§54).

    Declared *before* ``/reproductions/{experiment_id}`` so the literal path wins
    the route match; reporting metrics must not be shadowed by an id lookup.

    Everything is derived from stored rows plus the filesystem, because a tally
    held in memory cannot see a leak: it reports zero while directories pile up.
    """
    await require_project(db, project_id)

    experiments_rows = list(
        (
            await db.execute(
                select(ReproductionExperiment).where(
                    ReproductionExperiment.project_id == project_id
                )
            )
        )
        .scalars()
        .all()
    )
    run_rows = list(
        (
            await db.execute(
                select(ReproductionRun).where(ReproductionRun.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    sandbox_rows = list(
        (
            await db.execute(
                select(ReproductionSandbox).where(
                    ReproductionSandbox.project_id == project_id
                )
            )
        )
        .scalars()
        .all()
    )

    live_states = {
        SandboxStatus.CREATING,
        SandboxStatus.READY,
        SandboxStatus.STOPPING,
    }
    live = [row for row in sandbox_rows if row.status in live_states]

    def _tally(values: list[Any]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for value in values:
            counts[_enum_text(value)] = counts.get(_enum_text(value), 0) + 1
        return counts

    def _mean_ms(values: list[float]) -> Optional[int]:
        return int(sum(values) / len(values)) if values else None

    def _elapsed_ms(start: Any, end: Any) -> Optional[float]:
        if start is None or end is None:
            return None
        return (ensure_utc(end) - ensure_utc(start)).total_seconds() * 1000

    experiment_durations = [
        value
        for value in (
            _elapsed_ms(row.started_at, row.completed_at) for row in experiments_rows
        )
        if value is not None
    ]
    provision_durations = [
        value
        for value in (
            _elapsed_ms(row.created_at_sandbox, row.started_at) for row in sandbox_rows
        )
        if value is not None
    ]
    cleanup_durations = [
        value
        for value in (
            _elapsed_ms(row.stopped_at, row.destroyed_at) for row in sandbox_rows
        )
        if value is not None
    ]

    return ReproductionMetricsResponse(
        project_id=project_id,
        experiments=_tally([row.status for row in experiments_rows]),
        results=_tally([row.result for row in experiments_rows]),
        runs_completed=sum(1 for row in run_rows if row.status is RunStatus.COMPLETED),
        runs_failed=sum(1 for row in run_rows if row.status is RunStatus.FAILED),
        sandboxes_total=len(sandbox_rows),
        sandboxes_destroyed=sum(
            1 for row in sandbox_rows if row.status is SandboxStatus.DESTROYED
        ),
        live_sandboxes=len(live),
        # A live sandbox that already failed a cleanup attempt is the leak §55
        # exists to catch, and is counted apart from one a running experiment
        # legitimately still owns.
        orphaned_sandboxes=sum(1 for row in live if row.cleanup_error or row.orphaned),
        cleanup_failures=sum(1 for row in sandbox_rows if row.cleanup_error),
        durations_ms={
            "experiment_avg": _mean_ms(experiment_durations),
            "run_avg": _mean_ms(
                [float(row.duration_ms) for row in run_rows if row.duration_ms]
            ),
            "sandbox_provision_avg": _mean_ms(provision_durations),
            "sandbox_cleanup_avg": _mean_ms(cleanup_durations),
        },
        failures_by_class=_tally(
            [
                row.failure_classification
                for row in experiments_rows
                if row.failure_classification is not None
            ]
        ),
        backend=str(settings.REPRO_SANDBOX_BACKEND or "local").lower(),
        network_policy=str(settings.REPRO_NETWORK_POLICY or "ISOLATED").upper(),
        sandbox_disk=sandbox_metrics(),
    )


@router.get(
    "/reproductions/{experiment_id}",
    response_model=ReproductionExperimentDetailResponse,
)
async def get_reproduction(
    experiment_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> ReproductionExperimentDetailResponse:
    experiment = await _get_experiment(db, experiment_id, project_id=project_id)
    return await _hydrate(db, experiment)


@router.get(
    "/reproductions/{experiment_id}/plan", response_model=ReproductionPlanResponse
)
async def get_reproduction_plan(
    experiment_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> ReproductionPlanResponse:
    experiment = await _get_experiment(db, experiment_id, project_id=project_id)
    plan = await _load_plan(db, experiment.id)
    if plan is None:
        raise HTTPException(status_code=404, detail="This experiment has no plan")
    return ReproductionPlanResponse.model_validate(plan)


@router.get(
    "/reproductions/{experiment_id}/safety",
    response_model=ReproductionSafetyPreviewResponse,
)
async def get_reproduction_safety(
    experiment_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> ReproductionSafetyPreviewResponse:
    """Everything an engineer must see before confirming execution (§47)."""
    experiment = await _get_experiment(db, experiment_id, project_id=project_id)
    plan = await _load_plan(db, experiment.id)
    if plan is None:
        raise HTTPException(status_code=404, detail="This experiment has no plan")
    faults = list(
        (
            await db.execute(
                select(ReproductionFault).where(
                    ReproductionFault.experiment_id == experiment.id
                )
            )
        )
        .scalars()
        .all()
    )

    blocked: list[str] = []
    warnings: list[str] = []
    if not settings.REPRODUCTION_ENABLED:
        blocked.append("Failure reproduction is disabled (REPRODUCTION_ENABLED=false)")
    if is_experiment_terminal(experiment.status):
        blocked.append(f"This experiment is already {experiment.status.value}")
    try:
        backend_kind = backend_for().kind
    except SandboxError as exc:
        # The configured backend is unusable; report it and fall back to the
        # default name so the preview still renders (can_start is already false).
        blocked.append(str(exc))
        backend_kind = SandboxBackendKind.LOCAL_PROCESS

    template = await _template_for(experiment)
    unknown_services = [
        name
        for name in (plan.required_services or [])
        if name not in (template.get("aliases") or {})
    ]
    if unknown_services:
        # Should be impossible: the planner resolves services through the same
        # aliases. Reported rather than swallowed, because a service the sandbox
        # cannot start means the experiment cannot mean anything.
        blocked.append(
            "The plan names services this template cannot start: "
            + ", ".join(unknown_services)
        )

    if plan.network_policy.value != "ISOLATED":
        warnings.append(
            f"Network policy is {plan.network_policy.value}: the sandbox may reach "
            "allow-listed dependencies rather than nothing at all."
        )
    if plan.strategy.value == "CONFIGURATION_REPLAY":
        warnings.append(
            "This hypothesis is a change rather than a live fault, so the experiment "
            "runs the un-changed baseline. The failure NOT occurring is a meaningful "
            "result here, not a broken plan."
        )
    if (plan.derived_from or {}).get("missing_inputs"):
        warnings.append(
            "The plan is missing some inputs it would ideally replay; see "
            "GET /reproductions/{id}/plan for the list."
        )

    return ReproductionSafetyPreviewResponse(
        experiment_id=experiment.id,
        sandbox=sandbox_key_for(experiment.id),
        backend=backend_kind,
        network_policy=plan.network_policy,
        production_access="BLOCKED",
        credentials="SANITIZED",
        resource_limits=dict(plan.resource_limits or {}),
        timeout_seconds=plan.timeout_seconds,
        repetitions=plan.repetitions,
        services=list(plan.required_services or []),
        faults=[
            {
                "fault_type": fault.fault_type.value,
                "target": fault.target,
                "status": fault.status.value,
                "injected": fault.injected,
            }
            for fault in faults
        ],
        warnings=warnings,
        can_start=not blocked,
        blocked_reasons=blocked,
    )


@router.get(
    "/reproductions/{experiment_id}/status", response_model=ReproductionStatusResponse
)
async def get_reproduction_status(
    experiment_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> ReproductionStatusResponse:
    """Live status for the experiment page (§48). Polled; no new infra needed."""
    experiment = await _get_experiment(db, experiment_id, project_id=project_id)
    runs = list(
        (
            await db.execute(
                select(ReproductionRun)
                .where(ReproductionRun.experiment_id == experiment.id)
                .order_by(ReproductionRun.run_index)
            )
        )
        .scalars()
        .all()
    )
    sandbox = (
        await db.execute(
            select(ReproductionSandbox)
            .where(ReproductionSandbox.experiment_id == experiment.id)
            .order_by(ReproductionSandbox.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    faults = list(
        (
            await db.execute(
                select(ReproductionFault).where(
                    ReproductionFault.experiment_id == experiment.id
                )
            )
        )
        .scalars()
        .all()
    )
    inputs_total = int(
        await db.scalar(
            select(func.count(ReproductionInput.id)).where(
                ReproductionInput.experiment_id == experiment.id
            )
        )
        or 0
    )
    plan = await _load_plan(db, experiment.id)
    latest = runs[-1] if runs else None

    elapsed: Optional[float] = None
    if experiment.started_at is not None:
        end = experiment.completed_at or experiment.updated_at
        elapsed = max(0.0, (end - experiment.started_at).total_seconds())

    path = list(EXPERIMENT_HAPPY_PATH)
    step = path.index(experiment.status) + 1 if experiment.status in path else len(path)
    return ReproductionStatusResponse(
        experiment_id=experiment.id,
        status=experiment.status,
        result=experiment.result,
        progress=ExperimentProgress(
            status=experiment.status,
            step=step,
            total_steps=len(path),
            percent=int(round(step / len(path) * 100)),
            is_terminal=is_experiment_terminal(experiment.status),
            elapsed_seconds=elapsed,
        ),
        sandbox=(
            ReproductionSandboxResponse.model_validate(sandbox)
            if sandbox is not None
            else None
        ),
        runs_completed=experiment.completed_runs,
        repetitions=experiment.repetitions,
        replay_total=inputs_total,
        replay_completed=(latest.replay_request_count if latest is not None else 0),
        faults_active=sum(1 for fault in faults if fault.status.value == "ACTIVE"),
        faults_total=len(faults),
        resources=ResourceUsage(
            limits=dict((plan.resource_limits if plan is not None else None) or {}),
            observed=(
                {
                    "telemetry_bytes": latest.telemetry_bytes,
                    "observations": latest.observation_count,
                    "duration_ms": latest.duration_ms,
                }
                if latest is not None
                else None
            ),
        ),
        latest_run=(
            ReproductionRunResponse.model_validate(latest)
            if latest is not None
            else None
        ),
        cancel_requested=experiment.cancel_requested_at is not None,
        timeout_at=experiment.timeout_at,
    )


@router.get(
    "/reproductions/{experiment_id}/inputs",
    response_model=list[ReproductionInputResponse],
)
async def get_reproduction_inputs(
    experiment_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(200, ge=1, le=_MAX_ITEMS),
    db: AsyncSession = Depends(get_db),
) -> list[ReproductionInputResponse]:
    """The planned replay inputs, so the setup page can show them before start (§46)."""
    experiment = await _get_experiment(db, experiment_id, project_id=project_id)
    rows = list(
        (
            await db.execute(
                select(ReproductionInput)
                .where(ReproductionInput.experiment_id == experiment.id)
                .order_by(ReproductionInput.plan_order)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [ReproductionInputResponse.model_validate(row) for row in rows]


@router.get(
    "/reproductions/{experiment_id}/telemetry",
    response_model=ReproductionTelemetryResponse,
)
async def get_reproduction_telemetry(
    experiment_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    run_id: Optional[uuid.UUID] = Query(None),
    only_errors: bool = Query(False),
    limit: int = Query(200, ge=1, le=_MAX_ITEMS),
    db: AsyncSession = Depends(get_db),
) -> ReproductionTelemetryResponse:
    """Captured reproduction telemetry — always in its own namespace (§24)."""
    experiment = await _get_experiment(db, experiment_id, project_id=project_id)
    filters = [ReproductionObservation.experiment_id == experiment.id]
    if run_id is not None:
        filters.append(ReproductionObservation.run_id == run_id)
    if only_errors:
        filters.append(ReproductionObservation.error.is_(True))

    rows = list(
        (
            await db.execute(
                select(ReproductionObservation)
                .where(*filters)
                .order_by(
                    ReproductionObservation.observed_at, ReproductionObservation.id
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    total = int(
        await db.scalar(
            select(func.count(ReproductionObservation.id)).where(
                ReproductionObservation.experiment_id == experiment.id
            )
        )
        or 0
    )
    matched = int(
        await db.scalar(
            select(func.count(ReproductionObservation.id)).where(
                ReproductionObservation.experiment_id == experiment.id,
                ReproductionObservation.matched_expected.is_(True),
            )
        )
        or 0
    )
    missing = int(
        await db.scalar(
            select(func.count(ReproductionObservation.id)).where(
                ReproductionObservation.experiment_id == experiment.id,
                ReproductionObservation.status == ObservationStatus.MISSING,
            )
        )
        or 0
    )
    return ReproductionTelemetryResponse(
        experiment_id=experiment.id,
        namespace=experiment.telemetry_namespace or str(experiment.id),
        items=[ReproductionObservationResponse.model_validate(row) for row in rows],
        total=total,
        expected_count=matched + missing,
        matched_count=matched,
        missing_count=missing,
    )


@router.get(
    "/reproductions/{experiment_id}/artifacts",
    response_model=ReproductionArtifactListResponse,
)
async def get_reproduction_artifacts(
    experiment_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> ReproductionArtifactListResponse:
    """Immutable artifacts with their content hashes (§41, §42)."""
    experiment = await _get_experiment(db, experiment_id, project_id=project_id)
    rows = list(
        (
            await db.execute(
                select(ReproductionArtifact)
                .where(ReproductionArtifact.experiment_id == experiment.id)
                .order_by(ReproductionArtifact.name)
            )
        )
        .scalars()
        .all()
    )
    return ReproductionArtifactListResponse(
        experiment_id=experiment.id,
        items=[ReproductionArtifactResponse.model_validate(row) for row in rows],
        total=len(rows),
    )


@router.get(
    "/reproductions/{experiment_id}/comparison",
    response_model=ReproductionComparisonListResponse,
)
async def get_reproduction_comparison(
    experiment_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> ReproductionComparisonListResponse:
    """Per-run comparisons, with the formulas that produced every score (§28)."""
    experiment = await _get_experiment(db, experiment_id, project_id=project_id)
    rows = list(
        (
            await db.execute(
                select(ReproductionComparison)
                .where(ReproductionComparison.experiment_id == experiment.id)
                .order_by(ReproductionComparison.created_at)
            )
        )
        .scalars()
        .all()
    )
    aggregate: Optional[dict] = None
    if rows:
        scores = [
            row.similarity_score for row in rows if row.similarity_score is not None
        ]
        aggregate = {
            "runs": len(rows),
            "mean_similarity": round(sum(scores) / len(scores), 4) if scores else None,
            "buckets": sorted({row.overall_similarity.value for row in rows}),
            "results": sorted({row.result.value for row in rows}),
            "sequence_matched": all(bool(row.sequence_match) for row in rows),
            "note": (
                "Similarity is a coarse bucket over explainable dimensions. It is "
                "not a probability that the hypothesis is true."
            ),
        }
    return ReproductionComparisonListResponse(
        experiment_id=experiment.id,
        items=[ReproductionComparisonResponse.model_validate(row) for row in rows],
        total=len(rows),
        aggregate=aggregate,
    )


@router.get(
    "/reproductions/{experiment_id}/validation",
    response_model=ReproductionValidationResponse,
)
async def get_reproduction_validation(
    experiment_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> ReproductionValidationResponse:
    """The verdict, its evidence, and the reasons it might be wrong (§31, §32)."""
    experiment = await _get_experiment(db, experiment_id, project_id=project_id)
    validation = (
        await db.execute(
            select(ReproductionValidation).where(
                ReproductionValidation.experiment_id == experiment.id
            )
        )
    ).scalar_one_or_none()
    if validation is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "This experiment has no validation yet. A verdict is written when "
                "the experiment reaches the comparing phase."
            ),
        )
    return ReproductionValidationResponse.model_validate(validation)


@router.get(
    "/reproductions/{experiment_id}/environment",
    response_model=list[EnvironmentSnapshotResponse],
)
async def get_reproduction_environment(
    experiment_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> list[EnvironmentSnapshotResponse]:
    """The original and sandbox snapshots, for difference analysis (§33)."""
    experiment = await _get_experiment(db, experiment_id, project_id=project_id)
    rows = list(
        (
            await db.execute(
                select(EnvironmentSnapshot)
                .where(EnvironmentSnapshot.experiment_id == experiment.id)
                .order_by(EnvironmentSnapshot.captured_at)
            )
        )
        .scalars()
        .all()
    )
    return [EnvironmentSnapshotResponse.model_validate(row) for row in rows]


@router.get(
    "/reproductions/{experiment_id}/faults",
    response_model=ReproductionFaultListResponse,
)
async def get_reproduction_faults(
    experiment_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> ReproductionFaultListResponse:
    """The fault audit: what was injected, and what it observably affected (§22)."""
    experiment = await _get_experiment(db, experiment_id, project_id=project_id)
    rows = list(
        (
            await db.execute(
                select(ReproductionFault)
                .where(ReproductionFault.experiment_id == experiment.id)
                .order_by(ReproductionFault.created_at)
            )
        )
        .scalars()
        .all()
    )
    return ReproductionFaultListResponse(
        experiment_id=experiment.id,
        items=[ReproductionFaultResponse.model_validate(row) for row in rows],
        total=len(rows),
        injected_total=sum(1 for row in rows if row.injected),
    )


@router.get(
    "/reproductions/{experiment_id}/manifest",
    response_model=ReproductionManifestResponse,
)
async def get_reproduction_manifest(
    experiment_id: uuid.UUID,
    project_id: Optional[uuid.UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> ReproductionManifestResponse:
    """The §43 manifest: enough to understand how the experiment was executed."""
    experiment = await _get_experiment(db, experiment_id, project_id=project_id)
    plan = await _load_plan(db, experiment.id)
    if plan is None:
        raise HTTPException(status_code=404, detail="This experiment has no plan")
    inputs = list(
        (
            await db.execute(
                select(ReproductionInput)
                .where(ReproductionInput.experiment_id == experiment.id)
                .order_by(ReproductionInput.plan_order)
            )
        )
        .scalars()
        .all()
    )
    faults = list(
        (
            await db.execute(
                select(ReproductionFault).where(
                    ReproductionFault.experiment_id == experiment.id
                )
            )
        )
        .scalars()
        .all()
    )
    artifacts = list(
        (
            await db.execute(
                select(ReproductionArtifact).where(
                    ReproductionArtifact.experiment_id == experiment.id
                )
            )
        )
        .scalars()
        .all()
    )
    return ReproductionManifestResponse(
        experiment_id=experiment.id,
        experiment_version=experiment.experiment_version,
        status=experiment.status,
        application_version=plan.target_version,
        strategy=plan.strategy,
        services=list(plan.required_services or []),
        dependencies=list(plan.required_dependencies or []),
        inputs=[
            {
                "method": row.method,
                "target": f"{row.target_service}{row.target_path}",
                "payload_hash": row.payload_hash,
                "offset_ms": row.relative_offset_ms,
                "source": row.source.value,
                "status": row.status.value,
            }
            for row in inputs
        ],
        faults=[
            {
                "fault_type": row.fault_type.value,
                "target": row.target,
                "status": row.status.value,
                "injected": row.injected,
                "requests_affected": row.requests_affected,
            }
            for row in faults
        ],
        repetitions=plan.repetitions,
        network_policy=plan.network_policy,
        resource_limits=dict(plan.resource_limits or {}),
        timeout_seconds=plan.timeout_seconds,
        artifact_hashes=[
            {
                "name": row.name,
                "artifact_type": row.artifact_type.value,
                "content_hash": row.content_hash,
                "size_bytes": row.size_bytes,
            }
            for row in artifacts
        ],
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@router.post(
    "/reproductions/{experiment_id}/start",
    response_model=ReproductionExperimentDetailResponse,
    status_code=202,
)
async def start_reproduction(
    experiment_id: uuid.UUID,
    payload: StartReproductionRequest,
    project_id: uuid.UUID = Query(
        ..., description="Required: proves ownership of the experiment to execute"
    ),
    db: AsyncSession = Depends(get_db),
) -> ReproductionExperimentDetailResponse:
    """Queue an experiment for execution (§44, §47).

    Execution is asynchronous because a run takes minutes; the response reports
    the queued state and the client polls ``/status``. ``confirm_sandbox=true``
    is enforced by the request schema, so an experiment never starts merely
    because a plan exists.
    """
    if not settings.REPRODUCTION_ENABLED:
        raise HTTPException(
            status_code=409,
            detail="Failure reproduction is disabled (REPRODUCTION_ENABLED=false)",
        )
    experiment = await _get_experiment(
        db, experiment_id, project_id=project_id, require_scope=True
    )
    await _assert_incident_scope(db, experiment)
    if is_experiment_terminal(experiment.status):
        raise HTTPException(
            status_code=409,
            detail=(
                f"This experiment is already {experiment.status.value}. Plan a new "
                "experiment (POST /reproductions/{id}/retry) to try again."
            ),
        )
    if experiment.status is not ExperimentStatus.PLANNED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This experiment is {experiment.status.value} and already has a run "
                "in flight or completed."
            ),
        )
    if await _load_plan(db, experiment.id) is None:
        raise HTTPException(
            status_code=409, detail="This experiment has no plan to run"
        )

    if not await enqueue_reproduction_run(
        experiment_id=experiment.id, project_id=experiment.project_id
    ):
        raise HTTPException(
            status_code=503,
            detail=(
                "The job queue is unavailable, so the experiment was not started. "
                "Nothing was executed; retry once the broker is reachable."
            ),
        )

    # Move to VALIDATING so a second start cannot enqueue a duplicate run. The
    # worker's own transition to VALIDATING is a no-op, and it is the worker that
    # stamps ``started_at``/``timeout_at`` — so a crashed API request cannot
    # leave a deadline-less experiment behind.
    if experiment.status is ExperimentStatus.PLANNED:
        experiment.status = ExperimentStatus.VALIDATING
    if payload.requested_by:
        experiment.requested_by = payload.requested_by
    await db.commit()
    await db.refresh(experiment)
    return await _hydrate(db, experiment)


@router.post(
    "/reproductions/{experiment_id}/cancel",
    response_model=ReproductionExperimentDetailResponse,
)
async def cancel_reproduction(
    experiment_id: uuid.UUID,
    payload: Optional[CancelReproductionRequest] = None,
    project_id: uuid.UUID = Query(..., description="Required: proves ownership"),
    db: AsyncSession = Depends(get_db),
) -> ReproductionExperimentDetailResponse:
    """Request cancellation of a running experiment (§38).

    Cooperative: the running repetition notices between phases and unwinds
    through its cleanup path, so the sandbox is destroyed and the audit is
    written. Killing the worker instead would leak the sandbox.
    """
    experiment = await _get_experiment(
        db, experiment_id, project_id=project_id, require_scope=True
    )
    await _assert_incident_scope(db, experiment)
    if is_experiment_terminal(experiment.status):
        raise HTTPException(
            status_code=409,
            detail=f"This experiment is already {experiment.status.value}",
        )
    orchestrator = ReproductionOrchestrator(async_session_factory)
    updated = await orchestrator.request_cancel(
        experiment.id, reason=payload.reason if payload else None
    )
    del updated
    fresh = await _get_experiment(db, experiment_id)
    return await _hydrate(db, fresh)


@router.post(
    "/reproductions/{experiment_id}/retry",
    response_model=ReproductionExperimentDetailResponse,
    status_code=201,
)
async def retry_reproduction(
    experiment_id: uuid.UUID,
    payload: Optional[CreateReproductionRequest] = None,
    project_id: uuid.UUID = Query(..., description="Required: proves ownership"),
    db: AsyncSession = Depends(get_db),
) -> ReproductionExperimentDetailResponse:
    """Plan a **fresh** experiment testing the same hypothesis.

    Deliberately not "re-run this row": an experiment is a historical record, and
    reusing it would erase the first attempt's observations. A retry creates a new
    version with a new plan, so both attempts stay comparable (§51).
    """
    if not settings.REPRODUCTION_ENABLED:
        raise HTTPException(
            status_code=409,
            detail="Failure reproduction is disabled (REPRODUCTION_ENABLED=false)",
        )
    previous = await _get_experiment(
        db, experiment_id, project_id=project_id, require_scope=True
    )
    await _assert_incident_scope(db, previous)
    body = payload or CreateReproductionRequest()
    if body.candidate_id is None and previous.candidate_id is not None:
        body = body.model_copy(update={"candidate_id": previous.candidate_id})
    if body.causal_analysis_id is None:
        body = body.model_copy(
            update={"causal_analysis_id": previous.causal_analysis_id}
        )
    return await create_reproduction(
        incident_id=previous.incident_id,
        payload=body,
        project_id=project_id,
        environment_id=previous.environment_id,
        db=db,
    )


__all__ = ["router"]
