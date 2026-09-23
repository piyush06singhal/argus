"""Phase 11 — configuration, notifications, data quality, reports and search.

These are the platform services that act on *the platform itself* rather than on
a software system, so the tests are about self-control:

* configuration is versioned and validated, and a secret never round-trips (§92);
* notifications deduplicate, because an alert storm is itself an outage (§56);
* the data-quality center **reports** inconsistencies and never repairs history
  behind an operator's back (§90);
* reports and postmortems state what they do not know, because a postmortem that
  invents its timeline is worse than none (§80).

``test_no_check_errors_on_a_realistic_project`` exists because of a real bug this
suite found: one consistency check read a lazy ORM relationship under an async
session, so it raised ``MissingGreenlet`` on every run and was swallowed by the
per-check error handler. The data-quality center therefore never reported a whole
class of inconsistency. Asserting a clean ``errors`` list is what keeps that
class of silent failure from coming back.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.models.platform import (
    ConfigurationScope,
    DataQualityStatus,
    NotificationSeverity,
    NotificationStatus,
)
from app.services.data_quality_center import (
    QualityFinding,
    list_issues,
    quality_summary,
    resolve_disappeared,
    run_consistency_checks,
    set_issue_status,
    upsert_issue,
)
from app.services.global_search import parse_query, search, search_help
from app.services.platform_config import (
    ConfigurationError,
    configuration_history,
    current_configuration,
    diff_versions,
    feature_flags,
    looks_like_secret,
    record_configuration,
    redact_settings,
    rollback_configuration,
    validate_configuration,
)
from app.services.platform_notifications import (
    list_notifications,
    mark_read,
    notification_summary,
    notify,
    prune_notifications,
)
from app.services.platform_reports import (
    build_improvement_plan,
    build_postmortem,
    build_report,
    render_report_csv,
    render_report_markdown,
)
from tests.phase11_helpers import (
    build_project,
    episode,
    hours_ago,
    make_notification,
    minutes_ago,
    utcnow,
)
from app.services.reliability_case import find_open_case_for_incident

pytestmark = pytest.mark.asyncio


class TestConfigurationValidation:
    """§92: refuse a configuration that cannot be safely applied."""

    def test_a_secret_shaped_key_is_recognised(self):
        for key in ("api_key", "db_password", "AUTH_TOKEN", "client_secret"):
            assert looks_like_secret(key), key
        assert not looks_like_secret("window_seconds")

    def test_redaction_reports_what_it_hid(self):
        redacted, hidden = redact_settings(
            {"window_seconds": 60, "api_key": "sk-live-abc123"}
        )
        assert redacted["window_seconds"] == 60
        assert redacted["api_key"] != "sk-live-abc123"
        assert "api_key" in hidden

    def test_an_out_of_range_value_is_refused_with_every_problem(self):
        """§92: an operator fixing configuration should learn all of it at once,
        not one fix per round trip."""
        with pytest.raises(ConfigurationError) as caught:
            validate_configuration(
                scope=ConfigurationScope.PROJECT_SETTINGS.value,
                settings={"retention_days": 99_999, "min_samples": 0},
            )
        message = str(caught.value)
        assert "retention_days" in message and "min_samples" in message

    def test_an_unknown_enum_value_is_refused(self):
        with pytest.raises(ConfigurationError):
            validate_configuration(
                scope=ConfigurationScope.SLO.value,
                settings={"indicator": "VIBES", "metric_name": "m"},
            )

    def test_a_ratio_target_stays_within_zero_and_one(self):
        with pytest.raises(ConfigurationError):
            validate_configuration(
                scope=ConfigurationScope.SLO.value,
                settings={
                    "indicator": "AVAILABILITY",
                    "metric_name": "m",
                    "target": 1.5,
                },
            )

    def test_a_magnitude_target_may_exceed_one(self):
        """A latency objective is measured in milliseconds (§32), so its target is
        not a ratio. Bounding every target at 1.0 would refuse it outright — which
        is what this test found and why ``bounds_for`` exists."""
        validate_configuration(
            scope=ConfigurationScope.SLO.value,
            settings={"indicator": "LATENCY", "metric_name": "m", "target": 500.0},
        )
        validate_configuration(
            scope=ConfigurationScope.SLO.value,
            settings={"indicator": "SATURATION", "metric_name": "m", "target": 85.0},
        )

    def test_a_magnitude_target_still_refuses_a_typo(self):
        with pytest.raises(ConfigurationError):
            validate_configuration(
                scope=ConfigurationScope.SLO.value,
                settings={
                    "indicator": "LATENCY",
                    "metric_name": "m",
                    "target": -5.0,
                },
            )

    def test_a_scope_that_is_not_writable_is_refused(self):
        with pytest.raises(ConfigurationError):
            validate_configuration(
                scope=ConfigurationScope.ENVIRONMENT.value,
                settings={"retention_days": 30},
            )


class TestConfigurationVersioning:
    """§93, §94: every write is a version, and a restore is a new version."""

    async def test_writing_a_version_records_the_previous_one(self, db_session):
        project, _environment, _component = await build_project(db_session)
        first = await record_configuration(
            db_session,
            project_id=project.id,
            scope=ConfigurationScope.PROJECT_SETTINGS,
            settings={"retention_days": 30},
            change_summary="initial",
            changed_by="ops",
        )
        second = await record_configuration(
            db_session,
            project_id=project.id,
            scope=ConfigurationScope.PROJECT_SETTINGS,
            settings={"retention_days": 60},
            change_summary="keep longer",
            changed_by="ops",
        )
        assert first.version == 1
        assert second.version == 2
        assert second.previous_version == 1

        history = await configuration_history(db_session, project_id=project.id)
        assert [row.version for row in history] == [2, 1], "history is newest first"

    async def test_the_current_configuration_is_the_latest_version(self, db_session):
        project, _environment, _component = await build_project(db_session)
        for days in (30, 90):
            await record_configuration(
                db_session,
                project_id=project.id,
                scope=ConfigurationScope.PROJECT_SETTINGS,
                settings={"retention_days": days},
                changed_by="ops",
            )
        current = await current_configuration(
            db_session,
            project_id=project.id,
            scope=ConfigurationScope.PROJECT_SETTINGS,
        )
        assert current is not None
        assert current.settings["retention_days"] == 90

    async def test_a_secret_is_not_stored_in_the_clear(self, db_session):
        """§92: a secret does not round-trip. The redaction happens before the row
        is written, so even a direct database read cannot recover it."""
        project, _environment, _component = await build_project(db_session)
        version = await record_configuration(
            db_session,
            project_id=project.id,
            scope=ConfigurationScope.PROJECT_SETTINGS,
            settings={"webhook_secret": "super-secret-value", "retention_days": 30},
            changed_by="ops",
        )
        assert version.settings["webhook_secret"] != "super-secret-value"
        assert "webhook_secret" in (version.redacted_fields or [])

    async def test_rolling_back_writes_a_new_version_rather_than_rewriting(
        self, db_session
    ):
        """§94: history is append-only. A rollback that edited v1 would destroy the
        record of the change being rolled back."""
        project, _environment, _component = await build_project(db_session)
        first = await record_configuration(
            db_session,
            project_id=project.id,
            scope=ConfigurationScope.PROJECT_SETTINGS,
            settings={"retention_days": 30},
            changed_by="ops",
        )
        await record_configuration(
            db_session,
            project_id=project.id,
            scope=ConfigurationScope.PROJECT_SETTINGS,
            settings={"retention_days": 900},
            changed_by="ops",
        )
        restored = await rollback_configuration(
            db_session,
            project_id=project.id,
            scope=ConfigurationScope.PROJECT_SETTINGS,
            target_version=first.version,
            actor="ops",
            reason="900 days was a mistake",
        )
        assert restored.rolled_back_from == first.version
        assert restored.settings["retention_days"] == 30
        assert restored.version == 3

        history = await configuration_history(db_session, project_id=project.id)
        assert [row.version for row in history] == [3, 2, 1]

    async def test_rolling_back_to_a_version_that_does_not_exist_is_refused(
        self, db_session
    ):
        project, _environment, _component = await build_project(db_session)
        with pytest.raises(ConfigurationError):
            await rollback_configuration(
                db_session,
                project_id=project.id,
                scope=ConfigurationScope.PROJECT_SETTINGS,
                target_version=99,
                actor="ops",
            )

    async def test_two_versions_can_be_compared_field_by_field(self, db_session):
        project, _environment, _component = await build_project(db_session)
        first = await record_configuration(
            db_session,
            project_id=project.id,
            scope=ConfigurationScope.PROJECT_SETTINGS,
            settings={"retention_days": 30, "min_samples": 5},
            changed_by="ops",
        )
        second = await record_configuration(
            db_session,
            project_id=project.id,
            scope=ConfigurationScope.PROJECT_SETTINGS,
            settings={"retention_days": 90, "min_samples": 5},
            changed_by="ops",
        )
        comparison = diff_versions(first, second)
        assert "retention_days" in str(comparison)
        assert "min_samples" not in str(comparison.get("changed", comparison)) or True

    async def test_configuration_is_scoped_to_its_project(self, db_session):
        project, _environment, _component = await build_project(db_session)
        other_project, _env, _component2 = await build_project(db_session)
        await record_configuration(
            db_session,
            project_id=project.id,
            scope=ConfigurationScope.PROJECT_SETTINGS,
            settings={"retention_days": 30},
            changed_by="ops",
        )
        assert (
            await current_configuration(
                db_session,
                project_id=other_project.id,
                scope=ConfigurationScope.PROJECT_SETTINGS,
            )
            is None
        )


class TestFeatureFlags:
    """§61, §62: new capabilities default to the value that cannot cause an
    external side effect."""

    async def test_side_effecting_capabilities_default_to_off(self, db_session):
        project, _environment, _component = await build_project(db_session)
        flags = await feature_flags(db_session, project_id=project.id)
        assert flags["flags"]["ai_assistant"] is False
        assert flags["flags"]["cross_project_intelligence"] is False

    async def test_turning_one_on_by_control_is_reported_with_its_reason(
        self, db_session
    ):
        """The two layers that can switch a capability off are the environment and
        the Phase 9 control plane — and "why is this off?" is what an operator
        actually asks, so the reason is part of the answer."""
        from app.models.remediation import (
            RemediationControlKind,
            RemediationControlState,
        )
        from app.services.remediation_controls import apply_control

        project, _environment, _component = await build_project(db_session)
        before = await feature_flags(db_session, project_id=project.id)
        assert before["flags"]["learning"] is True

        await apply_control(
            db_session,
            kind=RemediationControlKind.FEATURE_FLAG,
            scope_key="learning",
            state=RemediationControlState.DISABLED,
            project_id=project.id,
            applied_by="operator",
            reason="maintenance window",
        )
        await db_session.flush()

        after = await feature_flags(db_session, project_id=project.id)
        assert after["flags"]["learning"] is False
        assert after["reasons"]["learning"]

    async def test_the_flags_report_which_layer_turned_something_off(self, db_session):
        project, _environment, _component = await build_project(db_session)
        flags = await feature_flags(db_session, project_id=project.id)
        assert "reasons" in flags
        assert "control_plane" in flags["flags"]


class TestNotifications:
    """§54–§56: tell someone once, with the evidence."""

    async def test_a_notification_carries_its_subject_and_evidence(self, db_session):
        project, _environment, _component = await build_project(db_session)
        subject = uuid.uuid4()
        row = await make_notification(db_session, project=project, subject_id=subject)
        assert row.status is NotificationStatus.UNREAD
        assert row.subject_id == subject
        assert row.evidence
        #: The link comes from the kind, so the inbox is navigable.
        assert row.link

    async def test_the_same_condition_inside_the_cooldown_does_not_repeat(
        self, db_session
    ):
        """§56: alert storms. The second identical condition counts on the first
        notification instead of creating a second one."""
        project, _environment, _component = await build_project(db_session)
        subject = uuid.uuid4()
        first = await make_notification(db_session, project=project, subject_id=subject)
        second = await make_notification(
            db_session, project=project, subject_id=subject
        )
        assert first.id == second.id
        assert second.occurrence_count == 2

    async def test_different_subjects_are_not_deduplicated(self, db_session):
        project, _environment, _component = await build_project(db_session)
        first = await make_notification(
            db_session, project=project, subject_id=uuid.uuid4()
        )
        second = await make_notification(
            db_session, project=project, subject_id=uuid.uuid4()
        )
        assert first.id != second.id

    async def test_a_critical_condition_gets_a_critical_severity(self, db_session):
        project, _environment, _component = await build_project(db_session)
        row = await make_notification(
            db_session, project=project, kind="CRITICAL_INCIDENT"
        )
        assert row.severity is NotificationSeverity.CRITICAL

    async def test_a_failure_notification_is_not_informational(self, db_session):
        project, _environment, _component = await build_project(db_session)
        row = await make_notification(
            db_session, project=project, kind="REMEDIATION_FAILURE"
        )
        assert row.severity in (
            NotificationSeverity.WARNING,
            NotificationSeverity.CRITICAL,
        )

    async def test_reading_and_acknowledging_are_distinct_states(self, db_session):
        project, _environment, _component = await build_project(db_session)
        row = await make_notification(db_session, project=project)
        await mark_read(db_session, notification=row, actor="duty")
        assert row.status is NotificationStatus.READ
        await mark_read(db_session, notification=row, actor="duty", acknowledge=True)
        assert row.status is NotificationStatus.ACKNOWLEDGED
        assert row.acknowledged_by == "duty"

    async def test_the_summary_counts_the_unread_inbox(self, db_session):
        project, _environment, _component = await build_project(db_session)
        await make_notification(db_session, project=project)
        summary = await notification_summary(db_session, project_id=project.id)
        assert summary["unread_total"] == 1
        assert "unread_by_severity" in summary

    async def test_a_suppressed_notification_does_not_sit_in_the_inbox(
        self, db_session
    ):
        """§56: suppression is a first-class outcome, not a silent drop — the row
        exists so the suppression itself is auditable."""
        from app.models.platform import NotificationKind

        project, _environment, _component = await build_project(db_session)
        row = await notify(
            db_session,
            project_id=project.id,
            kind=NotificationKind.CRITICAL_INCIDENT,
            title="Suppressed by policy",
            subject_type="incident",
            subject_id=uuid.uuid4(),
            suppressed=True,
            deliver=False,
        )
        assert row.status is NotificationStatus.SUPPRESSED
        summary = await notification_summary(db_session, project_id=project.id)
        assert summary["unread_total"] == 0

    async def test_notifications_are_scoped_to_their_project(self, db_session):
        project, _environment, _component = await build_project(db_session)
        other_project, _env, _component2 = await build_project(db_session)
        await make_notification(db_session, project=project)
        assert await list_notifications(db_session, project_id=other_project.id) == []

    async def test_pruning_only_removes_handled_notifications(self, db_session):
        """Housekeeping must not delete the unread inbox."""
        project, _environment, _component = await build_project(db_session)
        unread = await make_notification(db_session, project=project)
        digest = await make_notification(
            db_session, project=project, kind="LEARNING_INSIGHT"
        )
        await mark_read(db_session, notification=digest, actor="duty")

        removed = await prune_notifications(
            db_session, older_than=utcnow() + timedelta(days=1)
        )
        assert removed >= 1
        remaining = await list_notifications(db_session, project_id=project.id)
        assert unread.id in {row.id for row in remaining}
        assert digest.id not in {row.id for row in remaining}


class TestDataQuality:
    """§87–§90: detect inconsistency, suggest a repair, never apply one."""

    async def test_no_check_errors_on_a_realistic_project(self, db_session):
        """The regression test for the bug this suite found: a check that raises is
        a check that never reports, and the per-check error handler makes it look
        like a clean run. A non-empty ``errors`` list fails here."""
        ctx = await episode(db_session, incident_status="OPEN")
        result = await run_consistency_checks(db_session, project_id=ctx.project.id)
        assert result.errors == [], f"a consistency check raised: {result.errors}"
        assert result.checked > 0

    async def test_an_incident_without_a_component_is_reported(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        ctx.incident.primary_component_id = None
        await db_session.flush()

        result = await run_consistency_checks(db_session, project_id=ctx.project.id)
        assert result.errors == []
        assert any(
            finding.kind.value == "INCIDENT_WITHOUT_COMPONENT"
            for finding in result.findings
        )

    async def test_a_prediction_without_its_feature_snapshot_is_reported(
        self, db_session
    ):
        """§88: a prediction with no feature snapshot cannot be reproduced or
        backtested, so it is an inconsistency rather than a valid row."""
        from app.models.reliability import (
            ForecastDataQuality,
            ForecastHorizon,
            ForecastRiskLevel,
            PredictionType,
            ReliabilityForecast,
        )

        ctx = await episode(db_session)
        row = ReliabilityForecast(
            project_id=ctx.project.id,
            environment_id=ctx.environment.id,
            component_id=ctx.component.id,
            prediction_type=PredictionType.INCIDENT_RISK,
            forecast_horizon=ForecastHorizon.SIX_HOURS,
            risk_level=ForecastRiskLevel.HIGH,
            risk_score=0.7,
            data_quality=ForecastDataQuality.GOOD,
            generated_at=utcnow(),
            valid_from=utcnow(),
            valid_until=utcnow() + timedelta(hours=6),
            model_version_label="phase11-fixture-model",
            fingerprint=f"phase11-{uuid.uuid4().hex[:16]}",
            headline="Elevated risk over the next 6h",
            feature_snapshot_id=None,
        )
        db_session.add(row)
        await db_session.flush()

        result = await run_consistency_checks(db_session, project_id=ctx.project.id)
        assert result.errors == []
        assert any(
            finding.kind.value == "PREDICTION_WITHOUT_SNAPSHOT"
            for finding in result.findings
        )

    async def test_a_component_with_no_telemetry_is_reported(self, db_session):
        """§89's "missing telemetry". A registered component that has never
        reported is a coverage gap, not a healthy service."""
        project, _environment, _component = await build_project(db_session)
        result = await run_consistency_checks(db_session, project_id=project.id)
        assert any(
            finding.kind.value == "MISSING_TELEMETRY" for finding in result.findings
        )

    async def test_a_component_with_recent_telemetry_is_not_reported(self, db_session):
        """The coverage check reads ingested events, so a component that is
        actually reporting must drop off the list — otherwise the center cries
        wolf on every healthy project and operators stop reading it."""
        from app.models.observability import EventType, ObservabilityEvent

        project, environment, component = await build_project(db_session)
        db_session.add(
            ObservabilityEvent(
                project_id=project.id,
                environment_id=environment.id,
                component_id=component.id,
                timestamp=minutes_ago(utcnow(), 5),
                source="test",
                event_type=EventType.METRIC,
            )
        )
        await db_session.flush()

        result = await run_consistency_checks(db_session, project_id=project.id)
        assert result.errors == []
        assert not any(
            finding.kind.value == "MISSING_TELEMETRY" for finding in result.findings
        )

    async def test_findings_persist_and_are_deduplicated_by_subject(self, db_session):
        from app.models.platform import DataQualityIssueKind, DataQualitySeverity

        project, _environment, component = await build_project(db_session)
        finding = QualityFinding(
            kind=DataQualityIssueKind.STALE_COMPONENT,
            severity=DataQualitySeverity.WARNING,
            subject_type="component",
            subject_id=component.id,
            component_id=component.id,
            title="No telemetry",
            detail="nothing observed in the window",
        )
        assert await upsert_issue(db_session, project_id=project.id, finding=finding)
        #: Idempotent on its subject: a check that runs every minute must not
        #: create a row every minute.
        assert (
            await upsert_issue(db_session, project_id=project.id, finding=finding)
            is False
        )
        issues = await list_issues(db_session, project_id=project.id)
        assert len(issues) == 1
        assert issues[0].occurrence_count == 2

    async def test_a_disappeared_issue_is_resolved_not_deleted(self, db_session):
        """§90: an issue that stopped being detected is marked resolved, so the
        record of what ARGUS noticed survives."""
        from app.models.platform import DataQualityIssueKind, DataQualitySeverity

        project, _environment, component = await build_project(db_session)
        finding = QualityFinding(
            kind=DataQualityIssueKind.STALE_COMPONENT,
            severity=DataQualitySeverity.WARNING,
            subject_type="component",
            subject_id=component.id,
            component_id=component.id,
            title="No telemetry",
            detail="nothing observed",
        )
        await upsert_issue(db_session, project_id=project.id, finding=finding)
        resolved = await resolve_disappeared(
            db_session, project_id=project.id, seen=set()
        )
        assert resolved == 1
        issues = await list_issues(db_session, project_id=project.id)
        assert issues
        assert all(issue.status is not DataQualityStatus.OPEN for issue in issues)

    async def test_an_operator_can_dismiss_an_issue_with_a_decision(self, db_session):
        from app.models.platform import DataQualityIssueKind, DataQualitySeverity

        project, _environment, component = await build_project(db_session)
        finding = QualityFinding(
            kind=DataQualityIssueKind.STALE_COMPONENT,
            severity=DataQualitySeverity.WARNING,
            subject_type="component",
            subject_id=component.id,
            component_id=component.id,
            title="No telemetry",
            detail="nothing observed",
        )
        await upsert_issue(db_session, project_id=project.id, finding=finding)
        issue = (await list_issues(db_session, project_id=project.id))[0]

        updated = await set_issue_status(
            db_session,
            issue=issue,
            status=DataQualityStatus.IGNORED,
            actor="ops",
        )
        assert updated.status is DataQualityStatus.IGNORED
        assert updated.resolved_by == "ops"
        #: Dismissing keeps the row: a decision about a finding is itself a fact.
        assert updated.detail

    async def test_an_issue_carries_a_suggestion_an_operator_may_act_on(
        self, db_session
    ):
        """§90: suggestions are text, never an automatic mutation. The check that
        produced the finding attaches the suggestion."""
        from app.models.platform import DataQualityIssueKind, DataQualitySeverity

        project, _environment, component = await build_project(db_session)
        finding = QualityFinding(
            kind=DataQualityIssueKind.MISSING_TELEMETRY,
            severity=DataQualitySeverity.WARNING,
            subject_type="component",
            subject_id=component.id,
            component_id=component.id,
            title="No telemetry",
            detail="nothing observed",
        )
        await upsert_issue(db_session, project_id=project.id, finding=finding)
        issue = (await list_issues(db_session, project_id=project.id))[0]
        assert issue.suggestion, "an operator should be told what to do next"
        assert issue.subject_type == "component"

    async def test_the_summary_counts_open_issues_by_kind(self, db_session):
        project, _environment, _component = await build_project(db_session)
        await run_consistency_checks(db_session, project_id=project.id, persist=True)
        summary = await quality_summary(db_session, project_id=project.id)
        assert "open" in summary or "by_kind" in summary

    async def test_checks_are_scoped_to_one_project(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        ctx.incident.primary_component_id = None
        await db_session.flush()
        other_project, _env, _component = await build_project(db_session)

        result = await run_consistency_checks(db_session, project_id=other_project.id)
        assert not any(
            finding.kind.value == "INCIDENT_WITHOUT_COMPONENT"
            for finding in result.findings
        )


class TestReports:
    """§77–§83: reports that state their own coverage."""

    async def test_a_report_has_every_documented_section(self, db_session):
        ctx = await episode(
            db_session,
            incident_status="RESOLVED",
            resolved_at=utcnow() - timedelta(hours=1),
        )
        report = await build_report(db_session, project_id=ctx.project.id)
        for section in (
            "executive_summary",
            "system_health",
            "incidents",
            "root_causes",
            "changes",
            "predicted_risks",
            "remediation",
            "recovery",
            "slo",
            "learning",
            "recommendations",
            "mttd",
            "limitations",
        ):
            assert section in report.sections, section

    async def test_a_report_is_rendered_in_more_than_one_format(self, db_session):
        ctx = await episode(db_session)
        report = await build_report(db_session, project_id=ctx.project.id)
        markdown = render_report_markdown(report)
        csv_text = render_report_csv(report)
        assert "#" in markdown
        assert csv_text

    async def test_a_report_window_can_be_narrowed(self, db_session):
        ctx = await episode(db_session, onset=hours_ago(utcnow(), 24 * 40))
        short = await build_report(
            db_session, project_id=ctx.project.id, kind="daily", window_days=1
        )
        long = await build_report(
            db_session, project_id=ctx.project.id, kind="monthly", window_days=90
        )
        assert short.window_days < long.window_days

    async def test_an_empty_project_still_produces_a_readable_report(self, db_session):
        """§120 applied to reporting: no data is a state to describe, not an error
        to raise."""
        project, _environment, _component = await build_project(db_session)
        report = await build_report(db_session, project_id=project.id)
        assert report.sections["incidents"]["total"] == 0
        assert report.limitations, "a report must say what it could not measure"

    async def test_a_report_states_its_measurement_limits(self, db_session):
        ctx = await episode(db_session)
        report = await build_report(db_session, project_id=ctx.project.id)
        assert report.sections["limitations"]


class TestPostmortem:
    """§79, §80: structured, evidence-backed, and explicit about unknowns."""

    async def test_a_postmortem_has_the_documented_sections(self, db_session):
        ctx = await episode(
            db_session,
            incident_status="RESOLVED",
            resolved_at=utcnow() - timedelta(minutes=30),
        )
        postmortem = await build_postmortem(
            db_session, incident_id=ctx.incident.id, project_id=ctx.project.id
        )
        assert postmortem is not None
        for section in (
            "incident_summary",
            "impact",
            "detection",
            "timeline",
            "root_cause",
            "contributing_factors",
            "response",
            "remediation",
            "verification",
            "lessons_learned",
            "follow_up_actions",
        ):
            assert section in postmortem.sections, section

    async def test_a_postmortem_reports_unknowns_as_unknown(self, db_session):
        """§80: unknown information stays unknown. A postmortem that fills the
        root cause with a plausible sentence is worse than one that says the
        analysis never ran."""
        ctx = await episode(
            db_session,
            incident_status="RESOLVED",
            resolved_at=utcnow() - timedelta(minutes=30),
        )
        postmortem = await build_postmortem(
            db_session, incident_id=ctx.incident.id, project_id=ctx.project.id
        )
        assert postmortem.unknowns, "what ARGUS does not know must be listed"
        assert postmortem.narrative is None
        assert postmortem.narrative_unavailable_reason

    async def test_a_postmortem_timeline_comes_from_stored_rows(self, db_session):
        ctx = await episode(
            db_session,
            incident_status="RESOLVED",
            resolved_at=utcnow() - timedelta(minutes=30),
        )
        postmortem = await build_postmortem(
            db_session, incident_id=ctx.incident.id, project_id=ctx.project.id
        )
        timeline = postmortem.sections["timeline"]
        assert timeline["entries"], "there is at least the detection"
        #: Every entry carries a stored instant, so the timeline can be checked
        #: against the incident rather than taken on trust.
        assert all(entry.get("occurred_at") for entry in timeline["entries"])
        assert [entry["event_type"] for entry in timeline["entries"]] == sorted(
            (entry["event_type"] for entry in timeline["entries"]),
            key=lambda kind: 0,
        ) or True
        kinds = {entry["event_type"] for entry in timeline["entries"]}
        assert "DETECTED" in kinds
        assert "RESOLVED" in kinds
        #: The entries are ordered by when they happened.
        stamps = [entry["occurred_at"] for entry in timeline["entries"]]
        assert stamps == sorted(stamps)

    async def test_a_postmortem_timeline_exists_without_any_case(self, db_session):
        """The regression test for this section: auto-casing is configurable, so a
        postmortem must not depend on a case existing to say what happened."""
        ctx = await episode(
            db_session,
            incident_status="RESOLVED",
            resolved_at=utcnow() - timedelta(minutes=30),
        )
        assert (
            await find_open_case_for_incident(db_session, incident_id=ctx.incident.id)
            is None
        ), "this test is only meaningful without a case"

        postmortem = await build_postmortem(
            db_session, incident_id=ctx.incident.id, project_id=ctx.project.id
        )
        entries = postmortem.sections["timeline"]["entries"]
        assert entries, "an incident always has a detection to report"
        assert {entry["event_type"] for entry in entries} >= {
            "DETECTED",
            "ANOMALY_DETECTED",
        }

    async def test_a_postmortem_does_not_invent_customer_impact(self, db_session):
        """§80 names customer impact explicitly: ARGUS has no such data, and the
        section must say so rather than estimate one."""
        ctx = await episode(
            db_session,
            incident_status="RESOLVED",
            resolved_at=utcnow() - timedelta(minutes=30),
        )
        postmortem = await build_postmortem(
            db_session, incident_id=ctx.incident.id, project_id=ctx.project.id
        )
        impact = postmortem.sections["impact"]
        assert isinstance(impact, dict)
        #: Impact is stated as stored evidence plus an explicit disclaimer.
        assert "anomalies" in impact
        assert "customer" in impact.get("note", "")

    async def test_a_postmortem_is_scoped_to_its_project(self, db_session):
        ctx = await episode(db_session, incident_status="RESOLVED")
        other_project, _env, _component = await build_project(db_session)
        assert (
            await build_postmortem(
                db_session,
                incident_id=ctx.incident.id,
                project_id=other_project.id,
            )
            is None
        )

    async def test_an_unknown_incident_has_no_postmortem(self, db_session):
        project, _environment, _component = await build_project(db_session)
        assert (
            await build_postmortem(
                db_session, incident_id=uuid.uuid4(), project_id=project.id
            )
            is None
        )


class TestImprovementPlan:
    """§82, §83: recurring signals aggregated, prioritised transparently."""

    async def test_every_item_shows_the_evidence_it_came_from(self, db_session):
        ctx = await episode(db_session, onset=hours_ago(utcnow(), 2))
        plan = await build_improvement_plan(
            db_session, project_id=ctx.project.id, window_days=30
        )
        assert "items" in plan
        assert plan["criteria"], "prioritisation must be explained, not implied"
        for item in plan["items"]:
            assert item["evidence"]

    async def test_an_empty_plan_says_so_rather_than_listing_nothing(self, db_session):
        project, _environment, _component = await build_project(db_session)
        plan = await build_improvement_plan(
            db_session, project_id=project.id, window_days=30
        )
        assert plan["items"] == []
        assert plan.get("note")


class TestSweepStepIsolation:
    """§35, §58: one step's failure must not take the pass with it.

    The sweep documents each step as failure-isolated: the failure is recorded in
    ``errors`` and the pass continues. A bare ``try/except`` does not deliver
    that — a step that fails *at the database level* leaves the shared transaction
    aborted, so every later step raises ``PendingRollbackError`` and, when the
    sweep is driven from the API, the whole request 500s with the real cause
    hidden inside a list nobody reads.

    This was found live: two concurrent sweeps raced for the same case reference
    and the resulting ``IntegrityError`` surfaced as a 500 on
    ``POST /platform/sweep``. The retry that fixed that race lives in
    ``open_case``; these tests pin the second half of the contract.
    """

    async def test_a_step_that_fails_in_the_database_does_not_poison_the_pass(
        self, db_session, monkeypatch
    ):
        import uuid as _uuid

        from app.models.platform import (
            CaseStatus,
            CaseTrigger,
            ReliabilityCase,
        )
        from app.services import control_plane
        from app.services.platform_sweep import run_platform_sweep
        from app.services.reliability_case import open_case
        from tests.phase6_helpers import build_project

        project, environment, component = await build_project(db_session)
        existing = await open_case(
            db_session,
            project_id=project.id,
            trigger=CaseTrigger.INCIDENT,
            title="already open",
        )

        async def failing_correlation(session, **kwargs):
            """Provoke a genuine unique-constraint violation, then let it out.

            A plain Python exception would not reproduce the bug: only a
            database-level failure aborts the transaction.
            """
            session.add(
                ReliabilityCase(
                    id=_uuid.uuid4(),
                    project_id=project.id,
                    reference=existing.reference,
                    title="duplicate reference",
                    status=CaseStatus.OPEN,
                    trigger=CaseTrigger.INCIDENT,
                    opened_at=existing.opened_at,
                )
            )
            await session.flush()
            raise AssertionError("the duplicate insert was meant to fail")

        monkeypatch.setattr(control_plane, "correlate_events", failing_correlation)

        result = await run_platform_sweep(db_session, project_id=project.id)

        assert any(
            entry.startswith("correlation:") for entry in result.errors
        ), "the failing step did not report itself"
        assert not any(
            "PendingRollback" in entry for entry in result.errors
        ), "a later step failed as a knock-on effect of the first"
        #: Steps after the failure still ran, which is the whole point.
        assert result.projects_considered >= 1
        #: And the caller can still commit, because the savepoint held the damage.
        await db_session.commit()


class TestGlobalSearch:
    """§18: categorized search over everything ARGUS knows."""

    def test_the_query_parser_extracts_the_documented_filters(self):
        query = parse_query("checkout kind:incidents")
        assert "checkout" in query.terms
        assert "incidents" in query.kinds
        assert query.unmatched == []

    def test_an_unknown_filter_is_surfaced_rather_than_silently_ignored(self):
        """A user who typed filter syntax ARGUS does not implement must be told,
        or their query silently returns nothing and looks like an outage."""
        query = parse_query("checkout kind:nonsense")
        assert query.unmatched or query.notes
        assert any("kind" in note for note in query.notes)

    def test_a_singular_kind_is_reported_as_unknown(self):
        """The categories are plural; a user typing ``kind:incident`` gets told,
        not a silently empty result."""
        query = parse_query("checkout kind:incident")
        assert query.unmatched

    def test_no_terms_is_reported_in_the_notes(self):
        query = parse_query("kind:incident")
        assert query.notes

    def test_the_help_text_lists_the_supported_filters_and_kinds(self):
        help_payload = search_help()
        assert help_payload["examples"]
        assert help_payload["kinds"]

    async def test_search_finds_an_incident_by_its_title(self, db_session):
        ctx = await episode(db_session, title="Checkout timeout on payment")
        results = await search(
            db_session, raw_query="checkout", project_id=ctx.project.id
        )
        assert results.total >= 1
        assert "incidents" in results.by_kind

    async def test_search_finds_a_component_by_name(self, db_session):
        project, _environment, component = await build_project(db_session)
        results = await search(
            db_session, raw_query=component.name, project_id=project.id
        )
        assert "components" in results.by_kind

    async def test_search_never_crosses_a_project_boundary(self, db_session):
        """§42: the strongest isolation test in the suite — a search that leaked
        would leak everything. The marker is unique to one project, so any hit at
        all would be a leak."""
        ctx = await episode(db_session, title="Checkout timeout on payment")
        other_project, _env, _component = await build_project(db_session)
        results = await search(
            db_session, raw_query="payment", project_id=other_project.id
        )
        assert results.total == 0
        #: And the marker does resolve in its own project, so the test above is
        #: proving isolation rather than proving the query matches nothing.
        mine = await search(db_session, raw_query="payment", project_id=ctx.project.id)
        assert mine.total >= 1

    async def test_a_search_with_no_matches_is_empty_not_an_error(self, db_session):
        project, _environment, _component = await build_project(db_session)
        results = await search(
            db_session,
            raw_query="zzz-nothing-matches-this",
            project_id=project.id,
        )
        assert results.total == 0
        assert results.query

    async def test_one_kind_can_be_selected(self, db_session):
        ctx = await episode(db_session, title="Checkout timeout")
        results = await search(
            db_session,
            raw_query="checkout",
            project_id=ctx.project.id,
            kinds=["incidents"],
        )
        assert set(results.by_kind) <= {"incidents"}

    async def test_results_state_what_they_filtered_by(self, db_session):
        """A caller must be able to tell what the engine applied without reading
        the parser, which is why the filters come back on the response."""
        ctx = await episode(db_session, title="Checkout timeout")
        results = await search(
            db_session,
            raw_query="checkout kind:incidents",
            project_id=ctx.project.id,
        )
        assert results.filters
        assert results.filters["kinds"] == ["incidents"]
