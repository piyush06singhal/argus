"""Phase 9 — the planner and the sweep (§8, §9, §11, §25, §27, §39).

Two claims are tested here, and neither is taken on trust.

**The planner proposes; it never invents and never authorizes.** Every draft must
target a component that exists *in this project*, must name stored evidence rows,
must pass the registry's own parameter validation, must stay inside the per-run
bound, and must state its limitations. An incident whose best evidence is the
observation window — no causal candidate, no reproduction, no forecast — is below
the confidence floor and therefore produces *nothing*: a proposal nobody could
justify is worse than no proposal, because someone will approve it.

**The sweep advances; it never decides.** It executes only what already cleared
its gates, expires what nobody acted on, closes what a dead process abandoned,
cools breakers and retires controls — and it records what it did. The tests that
matter most are the refusals: a proposal the sweep just created must still be
``PROPOSED`` afterwards, and an action waiting on a human must not be executed by
a scheduler because a scheduler happened to run.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.causal import CandidateType, ConfidenceLevel, RootCauseCandidate
from app.models.reliability import (
    ForecastDataQuality,
    ForecastHorizon,
    ForecastRiskLevel,
    ForecastStatus,
    PredictionType,
    ReliabilityForecast,
)
from app.models.remediation import (
    CircuitState,
    ExecutionStatus,
    RemediationAction,
    RemediationActionType,
    RemediationApproval,
    RemediationApprovalStatus,
    RemediationAuditEvent,
    RemediationAuditEventType,
    RemediationCircuitBreaker,
    RemediationControl,
    RemediationControlKind,
    RemediationControlState,
    RemediationExecution,
    RemediationExecutionMode,
    RemediationFailureReason,
    RemediationOutcome,
    RemediationProposal,
    RemediationStatus,
)
from app.services.remediation_clock import aware, utcnow
from app.services.remediation_controls import is_paused
from app.services.remediation_planner import RemediationPlanner
from app.services.remediation_registry import (
    definition_requires_human_approval,
    get_definition,
    validate_parameters,
)
from app.services.remediation_service import RemediationService
from app.services.remediation_sweep import sweep_remediations_once
from app.services.remediation_state import apply_transition
from tests.phase6_helpers import build_causal_analysis
from tests.phase8_helpers import emit_deployment, emit_metric_series
from tests.phase9_helpers import (
    METRIC_ERROR_RATE,
    METRIC_P95,
    build_incident,
    build_project,
    make_action,
    set_environment_class,
    set_policy,
)

settings = get_settings()

#: How many health checks the fixtures write inside the planner's window. Kept as
#: a constant so an assertion can compare the proposal's evidence count against
#: the rows that were actually stored rather than against a number written twice.
_HEALTH_CHECKS = 6
_HEALTH_UNHEALTHY = 3


def _factory(db_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


async def _write_health_checks(
    session: AsyncSession, project, environment, component
) -> None:
    """Health checks that end unhealthy, inside the planner's observation window.

    The last third are ``UNHEALTHY`` and the newest one is the latest, so the
    planner has a *current* health signal rather than a historical blip.
    """
    from app.models.ingestion import HealthCheckEvent, HealthStatus

    now = utcnow()
    for index in range(_HEALTH_CHECKS):
        session.add(
            HealthCheckEvent(
                project_id=project.id,
                environment_id=environment.id,
                component_id=component.id,
                timestamp=now - timedelta(seconds=60 * (_HEALTH_CHECKS - index)),
                status=(
                    HealthStatus.UNHEALTHY
                    if index >= _HEALTH_CHECKS - _HEALTH_UNHEALTHY
                    else HealthStatus.HEALTHY
                ),
                latency_ms=310.0,
            )
        )
    await session.flush()


async def _write_telemetry(session, project, environment, component) -> None:
    """Error-rate and latency series inside the window, both elevated."""
    await emit_metric_series(
        session,
        project,
        environment,
        component,
        metric_name=METRIC_ERROR_RATE,
        values=[0.18] * 6,
        end=utcnow(),
        step_seconds=60,
        unit="ratio",
    )
    await emit_metric_series(
        session,
        project,
        environment,
        component,
        metric_name=METRIC_P95,
        values=[940.0] * 6,
        end=utcnow(),
        step_seconds=60,
    )


async def _degraded_incident(
    session: AsyncSession,
    *,
    environment_name: str = "staging",
    with_analysis: bool = True,
    with_deployment_candidate: bool = False,
    project=None,
    environment=None,
    component=None,
):
    """A project whose component is observably unhealthy, with real evidence.

    ``environment_name`` matters: it is what makes a scope count as
    non-production, which is the only place autonomous execution is ever
    considered.
    """
    if project is None:
        project, environment, component = await build_project(session)
    assert project is not None and environment is not None and component is not None
    await set_environment_class(session, environment, environment_name)
    await _write_health_checks(session, project, environment, component)
    await _write_telemetry(session, project, environment, component)
    incident = await build_incident(session, project, environment, component)
    analysis = candidate = None
    if with_analysis and component is not None:
        analysis, candidate = await build_causal_analysis(session, incident, component)
        if with_deployment_candidate:
            session.add(
                RootCauseCandidate(
                    analysis_id=analysis.id,
                    project_id=project.id,
                    component_id=component.id,
                    candidate_type=CandidateType.DEPLOYMENT,
                    score=0.91,
                    confidence=ConfidenceLevel.HIGH,
                    first_observed_at=incident.detected_at,
                    supporting_evidence_count=3,
                    contradicting_evidence_count=0,
                    explanation="a deployment immediately preceded the onset",
                    uncertainty={"gaps": ["no diff evidence"]},
                )
            )
            await session.flush()
    return project, environment, component, incident, analysis, candidate


async def _forecast(
    session: AsyncSession,
    project,
    environment,
    component,
    *,
    risk_level: ForecastRiskLevel = ForecastRiskLevel.HIGH,
    now=None,
) -> ReliabilityForecast:
    """A stored ACTIVE forecast at a caller-chosen risk level."""
    now = now or utcnow()
    row = ReliabilityForecast(
        project_id=project.id,
        environment_id=environment.id,
        component_id=component.id,
        prediction_type=PredictionType.FAILURE_RISK,
        forecast_horizon=ForecastHorizon.ONE_HOUR,
        generated_at=now,
        valid_from=now,
        valid_until=now + timedelta(seconds=ForecastHorizon.ONE_HOUR.seconds),
        risk_level=risk_level,
        risk_score=0.72,
        data_quality=ForecastDataQuality.GOOD,
        model_version_label="rolling-trend/v1",
        fingerprint=f"fp-{uuid.uuid4().hex[:12]}",
        headline="Elevated failure risk over the next hour",
        status=ForecastStatus.ACTIVE,
    )
    session.add(row)
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# The planner never invents a target (§6, §8)
# ---------------------------------------------------------------------------


async def test_an_incident_with_no_component_proposes_nothing(db_session):
    """No resolvable target means no proposal — not a project-wide guess."""
    project, environment, component = await build_project(db_session)
    incident = await build_incident(db_session, project, environment, None)
    assert incident.primary_component_id is None

    drafts = await RemediationPlanner(db_session).plan_for_incident(incident)
    assert drafts == []


async def test_a_component_from_another_project_is_not_a_target(db_session):
    """Cross-project targeting is refused even when the id is real."""
    project, environment, component = await build_project(db_session)
    other_project, _, other_component = await build_project(db_session)
    incident = await build_incident(db_session, project, environment, other_component)
    incident.primary_component_id = other_component.id

    drafts = await RemediationPlanner(db_session).plan_for_incident(incident)
    assert drafts == [], "a foreign component must never be a remediation target"


async def test_observation_evidence_alone_is_below_the_floor(db_session):
    """§9: an unjustifiable proposal is dropped, not surfaced as a guess."""
    project, environment, component, incident, _, _ = await _degraded_incident(
        db_session, with_analysis=False
    )
    assert settings.REMEDIATION_MIN_PROPOSAL_CONFIDENCE > 0.10

    drafts = await RemediationPlanner(db_session).plan_for_incident(incident)
    assert drafts == [], (
        "health telemetry and an elevated error rate identify *where* the "
        "problem is, not what to do about it"
    )


# ---------------------------------------------------------------------------
# Every proposal is evidenced, registered and bounded (§9, §11, §16)
# ---------------------------------------------------------------------------


async def test_restart_is_proposed_from_a_causal_candidate(db_session):
    (
        project,
        environment,
        component,
        incident,
        analysis,
        candidate,
    ) = await _degraded_incident(db_session)
    drafts = await RemediationPlanner(db_session).plan_for_incident(incident)

    restarts = [
        draft
        for draft in drafts
        if draft.action_type == RemediationActionType.RESTART_SERVICE
    ]
    assert len(restarts) == 1, [d.action_type.value for d in drafts]
    draft = restarts[0]

    # The proposal is scoped to the incident's component and environment.
    assert draft.component_id == component.id
    assert draft.environment_id == environment.id
    assert draft.incident_id == incident.id

    # It names the causal analysis and the candidate it came from.
    assert draft.causal_analysis_id == analysis.id
    assert draft.root_cause_candidate_id == candidate.id

    # Its evidence counts match the rows that were stored, not a claim.
    health = [
        row for row in draft.supporting_evidence if row["kind"] == "health_checks"
    ]
    assert health and health[0]["count"] == _HEALTH_CHECKS
    assert health[0]["unhealthy"] == _HEALTH_UNHEALTHY
    assert health[0]["latest_status"] == "UNHEALTHY"

    # It declares preconditions, verification/rollback and honest limitations.
    assert draft.preconditions, "a precondition-free proposal is unverifiable"
    assert draft.limitations
    assert any("does not fix a defect" in line for line in draft.limitations)
    columns = draft.as_columns()
    assert columns[
        "verification_plan"
    ], "a proposal without a verification plan cannot be verified"
    assert columns["rollback_plan"] is not None
    assert columns["preconditions"] == draft.preconditions


async def test_no_proposal_claims_certainty(db_session):
    """A 0.9 ceiling: a fix hypothesis is never presented as a fact."""
    project, environment, component, incident, _, _ = await _degraded_incident(
        db_session, with_deployment_candidate=True
    )
    drafts = await RemediationPlanner(db_session).plan_for_incident(incident)
    assert drafts
    for draft in drafts:
        assert 0.0 < draft.confidence <= 0.9
        assert draft.confidence_reason
        assert draft.limitations


async def test_every_proposal_is_a_registered_action_with_valid_parameters(
    db_session,
):
    """The planner cannot propose an action the registry would refuse (§3)."""
    project, environment, component, incident, _, _ = await _degraded_incident(
        db_session, with_deployment_candidate=True
    )
    drafts = await RemediationPlanner(db_session).plan_for_incident(incident)
    assert drafts
    for draft in drafts:
        definition = get_definition(draft.action_type)
        assert definition.action_type == draft.action_type
        clean, errors = validate_parameters(draft.action_type, draft.parameters)
        assert errors == [], (draft.action_type.value, errors)
        assert clean


async def test_no_proposal_carries_a_command_or_credential_parameter(db_session):
    """§3: parameters are declared names, never a shell line or a secret."""
    project, environment, component, incident, _, _ = await _degraded_incident(
        db_session, with_deployment_candidate=True
    )
    drafts = await RemediationPlanner(db_session).plan_for_incident(incident)
    assert drafts
    forbidden = {
        "command",
        "cmd",
        "shell",
        "script",
        "exec",
        "env",
        "token",
        "password",
        "secret",
        "credential",
        "kubeconfig",
    }
    for draft in drafts:
        definition = get_definition(draft.action_type)
        allowed = {spec.name for spec in definition.parameters}
        assert set(draft.parameters) <= allowed, (
            draft.action_type.value,
            set(draft.parameters) - allowed,
        )
        assert not (set(draft.parameters) & forbidden)


async def test_every_referenced_evidence_id_resolves(db_session):
    """A proposal that cites an id must cite a row that exists (§6)."""
    from app.models.causal import CausalAnalysis
    from app.models.reproduction import ReproductionValidation

    project, environment, component, incident, _, _ = await _degraded_incident(
        db_session, with_deployment_candidate=True
    )
    drafts = await RemediationPlanner(db_session).plan_for_incident(incident)
    resolvers = {
        "causal_analysis": CausalAnalysis,
        "root_cause_candidate": RootCauseCandidate,
        "reproduction_validation": ReproductionValidation,
        "reliability_forecast": ReliabilityForecast,
    }
    assert drafts
    for draft in drafts:
        for row in draft.supporting_evidence:
            kind = row["kind"]
            if kind not in resolvers or "id" not in row:
                continue
            resolved = await db_session.get(resolvers[kind], uuid.UUID(row["id"]))
            assert resolved is not None, (kind, row["id"])


async def test_the_proposal_count_is_bounded_and_ordered(db_session):
    """§8: a bounded, best-evidenced set — never an unbounded list."""
    project, environment, component, incident, _, _ = await _degraded_incident(
        db_session, with_deployment_candidate=True
    )
    drafts = await RemediationPlanner(db_session).plan_for_incident(incident)
    assert len(drafts) <= settings.REMEDIATION_MAX_PROPOSALS_PER_RUN
    confidences = [draft.confidence for draft in drafts]
    assert confidences == sorted(confidences, reverse=True)
    assert len({draft.fingerprint for draft in drafts}) == len(drafts), (
        "two proposals for the same action and parameters would be deduplicated "
        "anyway; they must not be emitted twice"
    )


async def test_rollback_names_the_deployment_that_preceded_onset(db_session):
    """§9 strategy B: the deployment id is real and precedes the incident."""
    project, environment, component, incident, _, _ = await _degraded_incident(
        db_session, with_deployment_candidate=True
    )
    deployment = await emit_deployment(
        db_session,
        project,
        environment,
        component,
        deployed_at=aware(incident.detected_at) - timedelta(minutes=20),
    )
    drafts = await RemediationPlanner(db_session).plan_for_incident(incident)
    rollbacks = [
        draft
        for draft in drafts
        if draft.action_type == RemediationActionType.ROLLBACK_DEPLOYMENT
    ]
    assert len(rollbacks) == 1, [d.action_type.value for d in drafts]
    draft = rollbacks[0]
    assert draft.parameters["deployment_id"] == str(deployment.id)
    assert draft.source_type.value == "ROOT_CAUSE_ANALYSIS"

    # A rollback is production-affecting and irreversible-ish, so the registry
    # requires a human — asserted with the exact predicate the policy engine
    # consults, because the draft itself does not carry the authority question.
    definition = get_definition(draft.action_type)
    assert definition_requires_human_approval(definition) is True
    assert definition.supports_autonomous_execution is False
    assert definition.production_effect is True


async def test_a_deployment_after_onset_is_not_a_rollback_candidate(db_session):
    """Precedence is required: a change after onset cannot explain it."""
    project, environment, component, incident, _, _ = await _degraded_incident(
        db_session, with_deployment_candidate=True
    )
    await emit_deployment(
        db_session,
        project,
        environment,
        component,
        deployed_at=aware(incident.detected_at) + timedelta(minutes=5),
    )
    drafts = await RemediationPlanner(db_session).plan_for_incident(incident)
    assert not [
        draft
        for draft in drafts
        if draft.action_type == RemediationActionType.ROLLBACK_DEPLOYMENT
    ]


# ---------------------------------------------------------------------------
# Prediction-driven planning (§8, §7)
# ---------------------------------------------------------------------------


async def test_an_unknown_forecast_is_not_a_reason_to_act(db_session):
    """Insufficient evidence must not become a remediation."""
    project, environment, component = await build_project(db_session)
    await set_environment_class(db_session, environment, "staging")
    await _write_telemetry(db_session, project, environment, component)
    forecast = await _forecast(
        db_session,
        project,
        environment,
        component,
        risk_level=ForecastRiskLevel.UNKNOWN,
    )
    drafts = await RemediationPlanner(db_session).plan_for_forecast(forecast)
    assert drafts == []


async def test_an_elevated_forecast_plans_against_the_component(db_session):
    project, environment, component = await build_project(db_session)
    await set_environment_class(db_session, environment, "staging")
    await _write_health_checks(db_session, project, environment, component)
    await _write_telemetry(db_session, project, environment, component)
    forecast = await _forecast(db_session, project, environment, component)

    drafts = await RemediationPlanner(db_session).plan_for_forecast(forecast)
    assert drafts, "an unhealthy component on an elevated forecast is actionable"
    for draft in drafts:
        assert draft.component_id == component.id
        assert draft.forecast_id == forecast.id
    forecast_evidence = [
        row
        for draft in drafts
        for row in draft.supporting_evidence
        if row["kind"] == "reliability_forecast"
    ]
    assert forecast_evidence
    assert forecast_evidence[0]["id"] == str(forecast.id)


async def test_forecast_planning_never_targets_a_foreign_component(db_session):
    """A forecast whose component was reassigned is no longer a valid target."""
    project, environment, component = await build_project(db_session)
    other_project, _, _ = await build_project(db_session)
    forecast = await _forecast(db_session, project, environment, component)
    #: Point the forecast at a component that belongs to a different project.
    from app.models.system import SystemComponent

    foreign = SystemComponent(
        project_id=other_project.id, name="foreign", component_type="SERVICE"
    )
    db_session.add(foreign)
    await db_session.flush()
    forecast.component_id = foreign.id

    drafts = await RemediationPlanner(db_session).plan_for_forecast(forecast)
    assert drafts == []


# ---------------------------------------------------------------------------
# The sweep advances the pipeline; it never authorizes (§25, §27, §39)
# ---------------------------------------------------------------------------


async def test_the_sweep_on_an_empty_database_is_a_quiet_no_op(db_engine):
    summary = await sweep_remediations_once(_factory(db_engine))
    assert summary["projects"] == 0
    assert summary["proposals_created"] == 0
    assert summary["errors"] == []


async def test_the_sweep_plans_but_never_authorizes_or_executes(db_session, db_engine):
    """The core §27 guarantee, asserted on a proposal the sweep itself created."""
    project, environment, component, incident, _, _ = await _degraded_incident(
        db_session, with_deployment_candidate=True
    )
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await sweep_remediations_once(
        factory, project_id=project.id, now=utcnow()
    )
    assert summary["proposals_created"] >= 1
    assert summary["errors"] == []

    async with factory() as fresh:
        actions = list(
            (
                await fresh.execute(
                    select(RemediationAction).where(
                        RemediationAction.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert actions
        for action in actions:
            assert action.status not in (
                RemediationStatus.AUTHORIZED,
                RemediationStatus.EXECUTING,
                RemediationStatus.VERIFIED,
            ), f"the sweep authorized {action.action_type.value}"
            #: A missing policy is a restrictive default, so nothing it planned
            #: can be live either.
            assert action.execution_mode == RemediationExecutionMode.OBSERVE_ONLY
            assert action.authorized_at is None
            assert action.approved_by is None

        executions = (
            await fresh.execute(
                select(func.count(RemediationExecution.id)).where(
                    RemediationExecution.project_id == project.id
                )
            )
        ).scalar()
        assert executions == 0, "the sweep must not execute what it just planned"

        controls = (
            await fresh.execute(
                select(func.count(RemediationControl.id)).where(
                    RemediationControl.project_id == project.id
                )
            )
        ).scalar()
        assert controls == 0, "no control may be applied without authorization"


async def test_the_sweep_is_idempotent(db_session, db_engine):
    """Running twice must not double-propose: dedup is part of the contract."""
    project, environment, component, incident, _, _ = await _degraded_incident(
        db_session, with_deployment_candidate=True
    )
    await db_session.commit()
    factory = _factory(db_engine)

    first = await sweep_remediations_once(factory, project_id=project.id)
    assert first["proposals_created"] >= 1
    second = await sweep_remediations_once(factory, project_id=project.id)
    assert second["proposals_created"] == 0

    async with factory() as fresh:
        actions = (
            await fresh.execute(
                select(func.count(RemediationAction.id)).where(
                    RemediationAction.project_id == project.id
                )
            )
        ).scalar()
        proposals = (
            await fresh.execute(
                select(func.count(RemediationProposal.id)).where(
                    RemediationProposal.project_id == project.id
                )
            )
        ).scalar()
    assert actions == first["proposals_created"]
    assert proposals == first["proposals_created"]


async def test_the_sweep_does_not_execute_a_human_approved_action(
    db_session, db_engine
):
    """An approval is a person's decision, not a scheduler's opportunity."""
    project, environment, component = await build_project(db_session)
    await set_environment_class(db_session, environment, "staging")
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        allowed_action_types=[RemediationActionType.PAUSE_BACKGROUND_JOB.value],
        cooldown_seconds=0,
        max_actions_per_window=10,
        canary_enabled=False,
    )
    action = await make_action(db_session, project, environment, component)
    service = RemediationService(db_session)
    await service.assess(action)
    await service.evaluate_policy(action)
    action, _ = await service.decide(
        action, approve=True, actor="operator", reason="approved by test"
    )
    assert action.status == RemediationStatus.AUTHORIZED
    await db_session.commit()
    factory = _factory(db_engine)

    await sweep_remediations_once(factory, project_id=project.id, plan=False)

    async with factory() as fresh:
        re_read = await fresh.get(RemediationAction, action.id)
        assert re_read.status == RemediationStatus.AUTHORIZED, (
            "the sweep executed a human-approved action without the approver's "
            "window being reached"
        )
        assert re_read.started_at is None
        assert (
            await fresh.execute(
                select(func.count(RemediationExecution.id)).where(
                    RemediationExecution.action_id == action.id
                )
            )
        ).scalar() == 0


async def test_the_sweep_executes_an_autonomous_action_a_dead_process_left(
    db_session, db_engine
):
    """An AUTONOMOUS action that reached AUTHORIZED must not stall forever."""
    project, environment, component = await build_project(db_session)
    await set_environment_class(db_session, environment, "staging")
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.AUTONOMOUS,
        autonomous_max_risk="LOW",
        allowed_action_types=[RemediationActionType.PAUSE_BACKGROUND_JOB.value],
        cooldown_seconds=0,
        max_actions_per_window=10,
        canary_enabled=False,
    )
    action = await make_action(db_session, project, environment, component)
    service = RemediationService(db_session)
    await service.assess(action)
    await service.evaluate_policy(action)
    assert action.status == RemediationStatus.AUTHORIZED
    assert action.execution_mode == RemediationExecutionMode.AUTONOMOUS
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await sweep_remediations_once(factory, project_id=project.id, plan=False)
    assert summary["executed"] + summary["verified"] >= 1
    assert summary["errors"] == []

    async with factory() as fresh:
        re_read = await fresh.get(RemediationAction, action.id)
        assert re_read.status == RemediationStatus.VERIFIED, re_read.status
        assert re_read.started_at is not None
        #: The real effect: the control plane honours the pause.
        assert await is_paused(
            fresh,
            "anomaly_sweep",
            project_id=project.id,
            environment_id=environment.id,
        )


# ---------------------------------------------------------------------------
# Housekeeping: expiry, breakers, abandoned attempts, controls (§25, §27, §39)
# ---------------------------------------------------------------------------


async def test_the_sweep_expires_a_stale_action_and_records_it(db_session, db_engine):
    project, environment, component = await build_project(db_session)
    action = await make_action(db_session, project, environment, component)
    action.expires_at = utcnow() - timedelta(hours=2)
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await sweep_remediations_once(factory, project_id=project.id, plan=False)
    assert summary["expired_actions"] == 1

    async with factory() as fresh:
        re_read = await fresh.get(RemediationAction, action.id)
        assert re_read.status == RemediationStatus.EXPIRED
        assert re_read.failure_reason == RemediationFailureReason.STALE_ACTION
        events = list(
            (
                await fresh.execute(
                    select(RemediationAuditEvent).where(
                        RemediationAuditEvent.action_id == action.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any(
            event.event_type == RemediationAuditEventType.ACTION_EXPIRED
            for event in events
        ), "an expiry that is not recorded is an unexplained disappearance"


async def test_the_sweep_expires_an_undecided_approval(db_session, db_engine):
    project, environment, component = await build_project(db_session)
    #: Production, because the approval path is the one production is allowed to
    #: take — the sweep must tidy it up rather than leave it waiting forever.
    await set_environment_class(db_session, environment, "production")
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        allowed_action_types=[RemediationActionType.PAUSE_BACKGROUND_JOB.value],
        cooldown_seconds=0,
        canary_enabled=False,
    )
    action = await make_action(db_session, project, environment, component)
    service = RemediationService(db_session)
    await service.assess(action)
    await service.evaluate_policy(action)
    assert action.status == RemediationStatus.AWAITING_APPROVAL

    approval = (
        (
            await db_session.execute(
                select(RemediationApproval).where(
                    RemediationApproval.action_id == action.id
                )
            )
        )
        .scalars()
        .first()
    )
    assert approval is not None
    approval.expires_at = utcnow() - timedelta(minutes=5)
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await sweep_remediations_once(factory, project_id=project.id, plan=False)
    assert summary["expired_approvals"] == 1

    async with factory() as fresh:
        assert (
            await fresh.get(RemediationApproval, approval.id)
        ).status == RemediationApprovalStatus.EXPIRED
        re_read = await fresh.get(RemediationAction, action.id)
        assert re_read.status == RemediationStatus.EXPIRED
        assert re_read.failure_reason == RemediationFailureReason.APPROVAL_EXPIRED


async def test_the_sweep_expires_an_action_past_its_own_deadline(db_session, db_engine):
    """§107: a stale action expires instead of executing later.

    The approval's TTL normally runs out first, and that path is covered above.
    This is the case where it does not: the *action's* own deadline has passed
    while its approval is still pending, and an approver could otherwise decide
    on a decision whose moment has gone and execute it hours late. The action's
    deadline has to hold on its own — and the approval it leaves behind is
    closed with it, so nobody is offered a button that can no longer work.
    """
    project, environment, component = await build_project(db_session)
    await set_environment_class(db_session, environment, "staging")
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        allowed_action_types=[RemediationActionType.PAUSE_BACKGROUND_JOB.value],
        cooldown_seconds=0,
        canary_enabled=False,
    )
    action = await make_action(db_session, project, environment, component)
    service = RemediationService(db_session)
    await service.assess(action)
    await service.evaluate_policy(action)
    assert action.status == RemediationStatus.AWAITING_APPROVAL

    approval = (
        (
            await db_session.execute(
                select(RemediationApproval).where(
                    RemediationApproval.action_id == action.id
                )
            )
        )
        .scalars()
        .first()
    )
    assert approval is not None and approval.status == RemediationApprovalStatus.PENDING
    #: Only the action's deadline moves; the approval is still inside its TTL.
    assert approval.expires_at is not None and aware(approval.expires_at) > utcnow()
    action.expires_at = utcnow() - timedelta(minutes=5)
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await sweep_remediations_once(factory, project_id=project.id, plan=False)
    assert summary["expired_actions"] == 1

    async with factory() as fresh:
        re_read = await fresh.get(RemediationAction, action.id)
        assert re_read.status == RemediationStatus.EXPIRED
        assert re_read.failure_reason == RemediationFailureReason.STALE_ACTION
        assert re_read.execution_status is None
        assert (
            await fresh.get(RemediationApproval, approval.id)
        ).status == RemediationApprovalStatus.EXPIRED
    assert not await is_paused(
        db_session,
        "anomaly_sweep",
        project_id=project.id,
        environment_id=environment.id,
    )


async def test_the_sweep_cools_a_breaker_so_one_probe_is_allowed(db_session, db_engine):
    project, environment, component = await build_project(db_session)
    breaker = RemediationCircuitBreaker(
        project_id=project.id,
        environment_id=environment.id,
        action_type=RemediationActionType.PAUSE_BACKGROUND_JOB,
        state=CircuitState.OPEN,
        consecutive_failures=3,
        threshold=3,
        opened_at=utcnow() - timedelta(minutes=20),
        opened_until=utcnow() - timedelta(minutes=1),
    )
    db_session.add(breaker)
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await sweep_remediations_once(factory, project_id=project.id, plan=False)
    assert summary["breakers_cooled"] >= 1

    async with factory() as fresh:
        assert (
            await fresh.get(RemediationCircuitBreaker, breaker.id)
        ).state == CircuitState.HALF_OPEN


async def test_a_live_breaker_stays_open(db_session, db_engine):
    """Cooling is time-based: a breaker still inside its window is untouched."""
    project, environment, component = await build_project(db_session)
    breaker = RemediationCircuitBreaker(
        project_id=project.id,
        environment_id=environment.id,
        action_type=RemediationActionType.PAUSE_BACKGROUND_JOB,
        state=CircuitState.OPEN,
        consecutive_failures=3,
        threshold=3,
        opened_at=utcnow(),
        opened_until=utcnow() + timedelta(minutes=10),
    )
    db_session.add(breaker)
    await db_session.commit()
    factory = _factory(db_engine)

    await sweep_remediations_once(factory, project_id=project.id, plan=False)

    async with factory() as fresh:
        assert (
            await fresh.get(RemediationCircuitBreaker, breaker.id)
        ).state == CircuitState.OPEN


async def test_the_sweep_closes_an_attempt_a_dead_process_abandoned(
    db_session, db_engine
):
    project, environment, component = await build_project(db_session)
    await set_environment_class(db_session, environment, "staging")
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.AUTONOMOUS,
        autonomous_max_risk="LOW",
        allowed_action_types=[RemediationActionType.PAUSE_BACKGROUND_JOB.value],
        cooldown_seconds=0,
        canary_enabled=False,
    )
    action = await make_action(db_session, project, environment, component)
    service = RemediationService(db_session)
    await service.assess(action)
    await service.evaluate_policy(action)
    apply_transition(action, RemediationStatus.EXECUTING)

    stalled = RemediationExecution(
        action_id=action.id,
        project_id=project.id,
        environment_id=environment.id,
        component_id=component.id,
        action_type=action.action_type,
        mode=RemediationExecutionMode.AUTONOMOUS,
        adapter_kind=action.adapter_kind,
        attempt=1,
        status=ExecutionStatus.RUNNING,
        idempotency_key=f"dead-{uuid.uuid4().hex[:12]}",
        started_at=utcnow() - timedelta(hours=2),
    )
    db_session.add(stalled)

    alive = RemediationExecution(
        action_id=action.id,
        project_id=project.id,
        environment_id=environment.id,
        component_id=component.id,
        action_type=action.action_type,
        mode=RemediationExecutionMode.AUTONOMOUS,
        adapter_kind=action.adapter_kind,
        attempt=2,
        status=ExecutionStatus.RUNNING,
        idempotency_key=f"alive-{uuid.uuid4().hex[:12]}",
        started_at=utcnow() - timedelta(seconds=30),
    )
    db_session.add(alive)
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await sweep_remediations_once(factory, project_id=project.id, plan=False)
    assert summary["stuck_executions"] >= 1

    async with factory() as fresh:
        assert (
            await fresh.get(RemediationExecution, stalled.id)
        ).status == ExecutionStatus.FAILED
        assert (
            (await fresh.get(RemediationExecution, alive.id)).status
            == ExecutionStatus.RUNNING
        ), "a live attempt must never be clobbered by housekeeping"
        re_read = await fresh.get(RemediationAction, action.id)
        assert re_read.status == RemediationStatus.FAILED
        assert re_read.failure_reason == RemediationFailureReason.TIMEOUT


async def test_the_sweep_retires_an_expired_control(db_session, db_engine):
    """A temporary pause must not become permanent by being left labelled."""
    project, environment, component = await build_project(db_session)
    expired = RemediationControl(
        project_id=project.id,
        environment_id=environment.id,
        component_id=component.id,
        kind=RemediationControlKind.BACKGROUND_JOB,
        scope_key="anomaly_sweep",
        state=RemediationControlState.PAUSED,
        previous_state=RemediationControlState.RESUMED,
        is_current=True,
        revision=1,
        applied_at=utcnow() - timedelta(hours=3),
        expires_at=utcnow() - timedelta(hours=1),
    )
    permanent = RemediationControl(
        project_id=project.id,
        environment_id=environment.id,
        component_id=component.id,
        kind=RemediationControlKind.BACKGROUND_JOB,
        scope_key="code_sweep",
        state=RemediationControlState.PAUSED,
        previous_state=RemediationControlState.RESUMED,
        is_current=True,
        revision=1,
        applied_at=utcnow() - timedelta(hours=3),
        expires_at=None,
    )
    db_session.add_all([expired, permanent])
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await sweep_remediations_once(factory, project_id=project.id, plan=False)
    assert summary["controls_retired"] >= 1

    async with factory() as fresh:
        assert (await fresh.get(RemediationControl, expired.id)).is_current is False
        retired = await fresh.get(RemediationControl, expired.id)
        assert retired.reverted_at is not None
        assert (await fresh.get(RemediationControl, permanent.id)).is_current is True


# ---------------------------------------------------------------------------
# Re-verification (§28, §31)
# ---------------------------------------------------------------------------


async def test_the_sweep_leaves_a_verification_that_is_too_early_alone(
    db_session, db_engine
) -> None:
    project, environment, component = await build_project(db_session)
    await set_environment_class(db_session, environment, "staging")
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        allowed_action_types=[RemediationActionType.PAUSE_BACKGROUND_JOB.value],
        cooldown_seconds=0,
        max_actions_per_window=10,
        canary_enabled=False,
    )
    action = await make_action(db_session, project, environment, component)
    service = RemediationService(db_session)
    await service.assess(action)
    await service.evaluate_policy(action)
    action, _ = await service.decide(
        action, approve=True, actor="operator", reason="approved by test"
    )
    await service.execute(action, actor="operator")
    assert action.status == RemediationStatus.VERIFYING
    action.completed_at = utcnow()
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await sweep_remediations_once(factory, project_id=project.id, plan=False)
    assert summary["retried"] == 0
    assert summary["verified"] == 0

    async with factory() as fresh:
        assert (
            await fresh.get(RemediationAction, action.id)
        ).status == RemediationStatus.VERIFYING


async def test_the_sweep_reverifies_once_the_window_has_elapsed(db_session, db_engine):
    project, environment, component = await build_project(db_session)
    await set_environment_class(db_session, environment, "staging")
    await set_policy(
        db_session,
        project.id,
        environment.id,
        execution_mode=RemediationExecutionMode.HUMAN_APPROVAL,
        allowed_action_types=[RemediationActionType.PAUSE_BACKGROUND_JOB.value],
        cooldown_seconds=0,
        max_actions_per_window=10,
        canary_enabled=False,
    )
    action = await make_action(db_session, project, environment, component)
    service = RemediationService(db_session)
    await service.assess(action)
    await service.evaluate_policy(action)
    action, _ = await service.decide(
        action, approve=True, actor="operator", reason="approved by test"
    )
    await service.execute(action, actor="operator")
    action.completed_at = utcnow() - timedelta(hours=1)
    await db_session.commit()
    factory = _factory(db_engine)

    summary = await sweep_remediations_once(factory, project_id=project.id, plan=False)
    assert summary["verified"] >= 1

    async with factory() as fresh:
        re_read = await fresh.get(RemediationAction, action.id)
        assert re_read.status == RemediationStatus.VERIFIED, re_read.status
        assert re_read.outcome in (
            RemediationOutcome.EFFECTIVE,
            RemediationOutcome.PARTIALLY_EFFECTIVE,
        )


# ---------------------------------------------------------------------------
# Failure isolation (§39)
# ---------------------------------------------------------------------------


async def test_one_failing_project_does_not_stop_the_sweep(
    db_session, db_engine, monkeypatch
):
    """A pox on one project must not silently stop remediating the rest."""
    #: Two independent degraded projects: one whose planner raises, one whose
    #: planning must still happen.
    broken, _, _, _, _, _ = await _degraded_incident(
        db_session, with_deployment_candidate=True
    )
    await _degraded_incident(db_session, with_deployment_candidate=True)
    await db_session.commit()
    factory = _factory(db_engine)

    original = RemediationPlanner.plan_for_incident

    async def flaky(self, incident, *, now=None):
        if incident.project_id == broken.id:
            raise RuntimeError("simulated planner failure")
        return await original(self, incident, now=now)

    monkeypatch.setattr(RemediationPlanner, "plan_for_incident", flaky)

    summary = await sweep_remediations_once(factory)
    assert any(str(broken.id) in error for error in summary["errors"]), summary[
        "errors"
    ]
    assert (
        summary["proposals_created"] >= 1
    ), "the healthy project must still be planned for"
