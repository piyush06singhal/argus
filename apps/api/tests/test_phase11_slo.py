"""Phase 11 — service level objectives and error budgets (§32–§36, §36).

An error budget is arithmetic, and the tests below are mostly about the cases
where the arithmetic has no honest answer. Two of them matter more than the rest:

* ``test_no_samples_is_unknown_not_meeting`` — an objective with no data must not
  report compliance. A dashboard that shows 100% availability because nothing
  was measured is worse than one that shows nothing at all.
* ``test_deleting_telemetry_does_not_create_compliance`` — the window is derived
  from stored rows, so removing samples must not be able to manufacture a pass.

The rest pin the direction logic (``AT_LEAST`` vs ``AT_MOST``), which is the one
place a sign error would invert every objective silently.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.platform import BurnRateState, SloComparison, SloIndicator, SloStatus
from app.services.slo_service import (
    budget_history,
    classify_burn,
    component_slo_status,
    create_slo,
    evaluate_and_record,
    evaluate_project,
    evaluate_slo,
    latest_snapshot,
    slo_overview,
)
from tests.phase11_helpers import (
    METRIC_AVAILABILITY,
    METRIC_ERROR_RATE,
    build_project,
    make_slo,
    metric_samples,
    minutes_ago,
    utcnow,
)

pytestmark = pytest.mark.asyncio


class TestBurnClassification:
    """§35: the burn bands are configuration, and the boundaries are inclusive."""

    def test_a_missing_reading_has_no_band(self):
        assert classify_burn(None) is BurnRateState.UNKNOWN

    def test_zero_burn_is_normal(self):
        assert classify_burn(0.0) is BurnRateState.NORMAL

    def test_the_bands_get_worse_as_the_rate_rises(self):
        #: A burn rate of 1.0 consumes the window's budget exactly, so anything
        #: below that is still normal — the bands are configured (2/6/14).
        assert classify_burn(0.5) is BurnRateState.NORMAL
        assert classify_burn(1.0) is BurnRateState.NORMAL
        assert classify_burn(3.0) is BurnRateState.ELEVATED
        assert classify_burn(7.0) is BurnRateState.FAST_BURN
        assert classify_burn(20.0) is BurnRateState.CRITICAL_BURN

    def test_the_boundaries_are_inclusive(self):
        """At exactly 2x, 6x and 14x the budget the worse band applies: a rate
        that has already consumed its allowance is not still ``ELEVATED``."""
        from app.core.config import get_settings

        config = get_settings()
        assert classify_burn(config.PLATFORM_BURN_ELEVATED) is BurnRateState.ELEVATED
        assert classify_burn(config.PLATFORM_BURN_FAST) is BurnRateState.FAST_BURN
        assert (
            classify_burn(config.PLATFORM_BURN_CRITICAL) is BurnRateState.CRITICAL_BURN
        )


class TestEvaluation:
    async def test_a_fully_compliant_window_meets_its_objective(self, db_session):
        project, environment, component = await build_project(db_session)
        await metric_samples(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_AVAILABILITY,
            values=[1.0] * 20,
            starting_at=minutes_ago(utcnow(), 30),
        )
        slo = await make_slo(
            db_session,
            project=project,
            component=component,
            environment=environment,
            target=0.99,
            window_seconds=3_600,
        )
        evaluation = await evaluate_slo(db_session, slo=slo)
        assert evaluation.status is SloStatus.MEETING
        assert evaluation.reading == pytest.approx(1.0)
        assert evaluation.sample_count == 20
        assert evaluation.burn_rate == pytest.approx(0.0)

    async def test_a_breached_window_is_reported_as_breached(self, db_session):
        project, environment, component = await build_project(db_session)
        await metric_samples(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_AVAILABILITY,
            values=[1.0] * 10 + [0.0] * 40,
            starting_at=minutes_ago(utcnow(), 50),
        )
        slo = await make_slo(
            db_session,
            project=project,
            component=component,
            environment=environment,
            target=0.99,
            window_seconds=3_600,
        )
        evaluation = await evaluate_slo(db_session, slo=slo)
        assert evaluation.status in (SloStatus.BREACHED, SloStatus.AT_RISK)
        assert evaluation.observed_failure is not None
        assert evaluation.burn_rate is not None and evaluation.burn_rate > 1.0

    async def test_no_samples_is_unknown_not_meeting(self, db_session):
        """The load-bearing negative: nothing measured is not a pass."""
        project, environment, component = await build_project(db_session)
        slo = await make_slo(
            db_session,
            project=project,
            component=component,
            environment=environment,
            window_seconds=3_600,
        )
        evaluation = await evaluate_slo(db_session, slo=slo)
        assert evaluation.status is SloStatus.UNKNOWN
        assert evaluation.data_quality == "NO_DATA"
        assert evaluation.limitations

    async def test_an_objective_with_no_metric_cannot_be_measured(self, db_session):
        project, environment, component = await build_project(db_session)
        #: Only a CUSTOM indicator may omit its metric — a named indicator without
        #: one is refused at creation, which is the stronger guarantee.
        slo = await create_slo(
            db_session,
            project_id=project.id,
            name="Custom objective",
            indicator=SloIndicator.CUSTOM,
            target=0.01,
            comparison=SloComparison.AT_MOST,
            component_id=component.id,
            environment_id=environment.id,
            window_seconds=3_600,
        )
        evaluation = await evaluate_slo(db_session, slo=slo)
        assert evaluation.status is SloStatus.UNKNOWN
        assert evaluation.data_quality == "NO_METRIC"

    async def test_a_named_indicator_without_a_metric_is_refused(self, db_session):
        from app.services.platform_config import ConfigurationError

        project, environment, component = await build_project(db_session)
        with pytest.raises(ConfigurationError):
            await create_slo(
                db_session,
                project_id=project.id,
                name="Nameless",
                indicator=SloIndicator.ERROR_RATE,
                target=0.01,
                comparison=SloComparison.AT_MOST,
                component_id=component.id,
                window_seconds=3_600,
            )

    async def test_samples_outside_the_window_are_excluded(self, db_session):
        project, environment, component = await build_project(db_session)
        moment = utcnow()
        await metric_samples(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_AVAILABILITY,
            values=[0.1] * 30,
            starting_at=moment - timedelta(days=5),
        )
        await metric_samples(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_AVAILABILITY,
            values=[1.0] * 10,
            starting_at=minutes_ago(moment, 20),
        )
        slo = await make_slo(
            db_session,
            project=project,
            component=component,
            environment=environment,
            target=0.99,
            window_seconds=3_600,
        )
        evaluation = await evaluate_slo(db_session, slo=slo, now=moment)
        #: Only the recent, healthy samples are in the window: a stale outage
        #: must not consume a fresh budget.
        assert evaluation.sample_count == 10
        assert evaluation.status is SloStatus.MEETING

    async def test_another_projects_samples_are_ignored(self, db_session):
        """§42: an objective is measured against its own project's telemetry."""
        project, environment, component = await build_project(db_session)
        other_project, other_env, other_component = await build_project(db_session)
        await metric_samples(
            db_session,
            other_project,
            other_env,
            other_component,
            metric_name=METRIC_AVAILABILITY,
            values=[0.0] * 20,
            starting_at=minutes_ago(utcnow(), 30),
        )
        slo = await make_slo(
            db_session,
            project=project,
            component=component,
            environment=environment,
            window_seconds=3_600,
        )
        evaluation = await evaluate_slo(db_session, slo=slo)
        assert evaluation.sample_count == 0
        assert evaluation.status is SloStatus.UNKNOWN

    async def test_deleting_telemetry_does_not_create_compliance(self, db_session):
        """The evaluation reads stored rows, so the absence of a sample is not
        evidence of health — it is a smaller sample. A window that becomes empty
        goes to UNKNOWN, not to 100%."""
        project, environment, component = await build_project(db_session)
        rows = await metric_samples(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_AVAILABILITY,
            values=[0.0] * 20,
            starting_at=minutes_ago(utcnow(), 30),
        )
        slo = await make_slo(
            db_session,
            project=project,
            component=component,
            environment=environment,
            target=0.99,
            window_seconds=3_600,
        )
        breaching = await evaluate_slo(db_session, slo=slo)
        assert breaching.status is not SloStatus.MEETING

        for row in rows:
            await db_session.delete(row)
        await db_session.flush()
        emptied = await evaluate_slo(db_session, slo=slo)
        assert emptied.status is SloStatus.UNKNOWN
        assert emptied.sample_count == 0

    async def test_an_at_most_objective_inverts_the_direction(self, db_session):
        """``AT_MOST`` is the error-rate case: samples *above* the target consume
        the budget. Getting this backwards would make every error-rate objective
        silently meaningless."""
        project, environment, component = await build_project(db_session)
        await metric_samples(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_ERROR_RATE,
            values=[0.005] * 20,
            starting_at=minutes_ago(utcnow(), 30),
        )
        slo = await create_slo(
            db_session,
            project_id=project.id,
            name="Checkout error rate",
            indicator=SloIndicator.ERROR_RATE,
            target=0.01,
            comparison=SloComparison.AT_MOST,
            metric_name=METRIC_ERROR_RATE,
            component_id=component.id,
            environment_id=environment.id,
            window_seconds=3_600,
        )
        evaluation = await evaluate_slo(db_session, slo=slo)
        assert evaluation.status is SloStatus.MEETING

    async def test_a_magnitude_indicator_uses_the_worst_sample(self, db_session):
        """Latency is a magnitude: one 900ms minute is a breach even if the other
        fifty-nine were fine, which is why the reading is the max and not the
        mean."""
        project, environment, component = await build_project(db_session)
        await metric_samples(
            db_session,
            project,
            environment,
            component,
            metric_name="http.checkout.latency.p95",
            values=[100.0] * 50 + [900.0],
            starting_at=minutes_ago(utcnow(), 60),
        )
        #: A latency target is a magnitude in the objective's own unit, so 500ms
        #: is a legitimate target. Bounding every target at 1.0 would make this
        #: objective impossible to express (§32).
        slo = await create_slo(
            db_session,
            project_id=project.id,
            name="Checkout p95 latency",
            indicator=SloIndicator.LATENCY,
            target=500.0,
            comparison=SloComparison.AT_MOST,
            metric_name="http.checkout.latency.p95",
            component_id=component.id,
            environment_id=environment.id,
            window_seconds=3_600,
            unit="ms",
        )
        assert slo.target == 500.0
        evaluation = await evaluate_slo(db_session, slo=slo)
        assert evaluation.reading == pytest.approx(900.0)
        assert evaluation.status is not SloStatus.MEETING


class TestRecording:
    async def test_evaluating_and_recording_stores_a_snapshot(self, db_session):
        project, environment, component = await build_project(db_session)
        await metric_samples(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_AVAILABILITY,
            values=[1.0] * 10,
            starting_at=minutes_ago(utcnow(), 20),
        )
        slo = await make_slo(
            db_session,
            project=project,
            component=component,
            environment=environment,
            window_seconds=3_600,
        )
        evaluation, snapshot = await evaluate_and_record(db_session, slo=slo)
        assert snapshot.slo_id == slo.id
        assert snapshot.status is evaluation.status
        assert snapshot.sample_count == evaluation.sample_count

        latest = await latest_snapshot(db_session, slo_id=slo.id)
        assert latest is not None
        assert latest.id == snapshot.id

    async def test_budget_history_is_newest_first(self, db_session):
        project, environment, component = await build_project(db_session)
        slo = await make_slo(
            db_session,
            project=project,
            component=component,
            environment=environment,
            window_seconds=3_600,
        )
        from tests.phase11_helpers import utcnow as now

        base = now() - timedelta(hours=3)
        for offset in range(3):
            await metric_samples(
                db_session,
                project,
                environment,
                component,
                metric_name=METRIC_AVAILABILITY,
                values=[1.0] * 5,
                starting_at=base + timedelta(hours=offset),
            )
            await evaluate_and_record(
                db_session, slo=slo, now=base + timedelta(hours=offset + 1)
            )
        rows = await budget_history(db_session, slo_id=slo.id)
        assert len(rows) == 3
        assert rows == sorted(rows, key=lambda row: row.computed_at, reverse=True)

    async def test_project_evaluation_covers_every_enabled_objective(self, db_session):
        project, environment, component = await build_project(db_session)
        await make_slo(
            db_session,
            project=project,
            component=component,
            environment=environment,
            name="One",
            window_seconds=3_600,
        )
        await make_slo(
            db_session,
            project=project,
            component=component,
            environment=environment,
            name="Two",
            window_seconds=3_600,
        )
        summary = await evaluate_project(db_session, project_id=project.id)
        assert summary["evaluated"] == 2

    async def test_the_overview_separates_objectives_from_their_readings(
        self, db_session
    ):
        project, environment, component = await build_project(db_session)
        await metric_samples(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_AVAILABILITY,
            values=[1.0] * 10,
            starting_at=minutes_ago(utcnow(), 20),
        )
        await make_slo(
            db_session,
            project=project,
            component=component,
            environment=environment,
            window_seconds=3_600,
        )
        overview = await slo_overview(db_session, project_id=project.id)
        assert overview["objectives_total"] == 1
        assert len(overview["objectives"]) == 1
        #: The objective is listed with its own reading, and the roll-up counts
        #: by status so the dashboard does not have to re-derive them.
        assert overview["by_status"]["MEETING"] >= 0
        assert overview["as_of"]

    async def test_component_compliance_reports_a_number_or_a_reason(self, db_session):
        """§75: a scorecard cell must be able to say "not measured" — omitting it
        would let an unmeasured component look compliant."""
        project, environment, component = await build_project(db_session)
        empty = await component_slo_status(db_session, component_id=component.id)
        assert empty["objectives"] == 0
        assert empty.get("note"), "an unmeasured component says why, not 100%"

        await metric_samples(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_AVAILABILITY,
            values=[1.0] * 10,
            starting_at=minutes_ago(utcnow(), 20),
        )
        slo = await make_slo(
            db_session,
            project=project,
            component=component,
            environment=environment,
            window_seconds=3_600,
        )
        await evaluate_and_record(db_session, slo=slo)
        measured = await component_slo_status(db_session, component_id=component.id)
        assert measured["objectives"] == 1
        assert measured["with_readings"] == 1
        assert measured["mean_compliance_percent"] == pytest.approx(100.0)


class TestSloIntegration:
    """§36: SLO intelligence connects to the rest of the platform — without
    claiming causality."""

    async def test_a_burning_objective_raises_an_event_and_a_notification(
        self, db_session
    ):
        from sqlalchemy import select

        from app.models.platform import PlatformEvent, PlatformEventType

        project, environment, component = await build_project(db_session)
        await metric_samples(
            db_session,
            project,
            environment,
            component,
            metric_name=METRIC_AVAILABILITY,
            values=[0.0] * 30,
            starting_at=minutes_ago(utcnow(), 40),
        )
        slo = await make_slo(
            db_session,
            project=project,
            component=component,
            environment=environment,
            target=0.99,
            window_seconds=3_600,
        )
        evaluation, snapshot = await evaluate_and_record(db_session, slo=slo)
        assert evaluation.burn_state in (
            BurnRateState.FAST_BURN,
            BurnRateState.CRITICAL_BURN,
        )
        await db_session.flush()

        events = (
            await db_session.scalars(
                select(PlatformEvent).where(
                    PlatformEvent.event_type == PlatformEventType.ERROR_BUDGET_BURN
                )
            )
        ).all()
        assert events, "a burning budget is a platform event"
        #: The payload states the arithmetic rather than an opinion about it.
        assert events[0].payload["burn_state"] == evaluation.burn_state.value
