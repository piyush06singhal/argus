"""Phase 11 — the reliability case, its timeline and its evidence (§14–§17).

A case is the object that makes the whole phase navigable, so the tests here
concentrate on the properties that make navigation *safe*:

* the status machine is enforced in one place, so the UI cannot offer a move the
  backend refuses;
* the timeline is a story, not a poll — replaying an event appends nothing;
* evidence is *linked*, never copied, which is what stops a case from becoming a
  stale shadow of the tables it describes.

The last point is the one worth proving hardest: a case whose evidence was
snapshotted at open time would show an operator yesterday's anomalies while the
component kept degrading.
"""

from __future__ import annotations

import pytest

from app.models.platform import CaseStatus, CaseTrigger, TimelineEntryKind
from app.services.reliability_case import (
    CASE_STATUS_TRANSITIONS,
    CaseStateError,
    append_timeline,
    can_transition,
    case_summary,
    case_timeline,
    collect_case_evidence,
    find_open_case_for_incident,
    get_case,
    list_cases,
    next_reference,
    reference_number,
    transition_case,
)
from tests.phase11_helpers import episode, make_case, utcnow

pytestmark = pytest.mark.asyncio


class TestStatusMachine:
    """§14: one table of legal moves, shared by API and engine."""

    def test_a_case_cannot_skip_from_open_to_executing(self):
        """The rule that matters: nothing reaches EXECUTING without passing
        AUTHORIZED. A case is not a way around Phase 9's gates."""
        assert not can_transition(CaseStatus.OPEN, CaseStatus.EXECUTING)
        assert not can_transition(CaseStatus.DIAGNOSED, CaseStatus.EXECUTING)
        assert can_transition(CaseStatus.AUTHORIZED, CaseStatus.EXECUTING)

    def test_every_status_has_an_entry_in_the_table(self):
        """A missing entry would silently make a status terminal."""
        for status in CaseStatus:
            assert status in CASE_STATUS_TRANSITIONS

    def test_authorized_and_executing_are_reachable_only_in_order(self):
        chain = [
            CaseStatus.OPEN,
            CaseStatus.TRIAGED,
            CaseStatus.ANALYZING,
            CaseStatus.DIAGNOSED,
            CaseStatus.REMEDIATION_READY,
            CaseStatus.AUTHORIZED,
            CaseStatus.EXECUTING,
            CaseStatus.VERIFYING,
            CaseStatus.RESOLVED,
            CaseStatus.LEARNED,
        ]
        for current, target in zip(chain, chain[1:]):
            assert can_transition(current, target), f"{current} -> {target}"

    def test_terminal_states_lead_nowhere(self):
        """LEARNED is not terminal — closing is a separate, deliberate act, so
        the knowledge a case produced stays reviewable before it is sealed."""
        assert CASE_STATUS_TRANSITIONS[CaseStatus.LEARNED] == (CaseStatus.CLOSED,)
        for terminal in (CaseStatus.CLOSED, CaseStatus.CANCELLED):
            assert CASE_STATUS_TRANSITIONS[terminal] == ()


class TestOpeningCases:
    async def test_a_case_gets_a_human_reference_and_an_opening_entry(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            environment=ctx.environment,
            component=ctx.component,
            incident=ctx.incident,
        )
        assert case.reference.startswith("CASE-")
        assert case.status is CaseStatus.OPEN
        assert case.incident_id == ctx.incident.id

        entries = await case_timeline(db_session, case_id=case.id)
        assert len(entries) == 1
        assert entries[0].event_type == "CASE_OPENED"
        #: The entry says what the case is about, so the story starts with its
        #: trigger rather than with a bare status.
        assert entries[0].title

    async def test_references_are_unique_and_sequential_within_a_project(
        self, db_session
    ):
        from tests.phase6_helpers import build_project

        project, _environment, component = await build_project(db_session)
        first = await next_reference(db_session, project_id=project.id)
        await make_case(db_session, project=project, component=component)
        second = await next_reference(db_session, project_id=project.id)
        assert first != second

    def test_a_reference_number_is_read_numerically_not_lexically(self):
        """``max('CASE-9', 'CASE-10')`` is ``'CASE-9'``: ordering references as
        text stops growing at nine, so every later case re-derives a reference
        that already exists. This was found live, as a duplicate-key violation on
        ``POST /platform/sweep``.
        """
        assert reference_number("CASE-9") < reference_number("CASE-10")
        assert reference_number("CASE-1") == 1
        assert reference_number("CASE-1000") == 1000
        #: Anything unparseable is treated as "no number yet" rather than raising:
        #: a legacy or hand-written reference must not stop the next case opening.
        assert reference_number("CASE-") == 0
        assert reference_number("SOMETHING-ELSE") == 0
        assert reference_number(None) == 0

    async def test_the_eleventh_case_is_case_11(self, db_session):
        """The end-to-end form of the bug above: a project's tenth case does not
        make its eleventh impossible."""
        from tests.phase6_helpers import build_project

        project, _environment, component = await build_project(db_session)
        references = [
            (
                await make_case(db_session, project=project, component=component)
            ).reference
            for _ in range(11)
        ]
        assert references == [f"CASE-{number}" for number in range(1, 12)]

    async def test_losing_the_reference_race_still_opens_a_case(
        self, db_session, monkeypatch
    ):
        """The API sweep and the scheduled sweep routinely open cases at the same
        moment, and the loser of the ``(project_id, reference)`` race used to
        surface as a 500 with a poisoned transaction.

        A losing attempt must be retried under a savepoint with a freshly
        derived reference, and the caller's transaction must remain usable.
        """
        from app.models.platform import CaseTrigger
        from app.services import reliability_case as case_module

        ctx = await episode(db_session, incident_status="OPEN")
        taken = await make_case(
            db_session, project=ctx.project, component=ctx.component
        )

        attempts: list[int] = []
        real_next_reference = case_module.next_reference

        async def colliding(session, *, project_id):
            attempts.append(1)
            #: The first attempt claims the reference the rival already holds;
            #: every later one behaves normally.
            if len(attempts) == 1:
                return taken.reference
            return await real_next_reference(session, project_id=project_id)

        monkeypatch.setattr(case_module, "next_reference", colliding)

        opened = await case_module.open_case(
            db_session,
            project_id=ctx.project.id,
            trigger=CaseTrigger.INCIDENT,
            title="opened while the sweep was running",
        )

        assert len(attempts) == 2, "the collision was not retried"
        assert opened.reference != taken.reference
        #: The point of the savepoint: the session is still usable, so whatever
        #: else the caller was doing is not lost.
        following = await next_reference(db_session, project_id=ctx.project.id)
        assert following not in (opened.reference, taken.reference)

    async def test_an_exhausted_reference_race_is_reported_not_silent(
        self, db_session, monkeypatch
    ):
        """If every attempt collides, the caller hears about it rather than
        getting a phantom case."""
        from app.models.platform import CaseTrigger
        from app.services import reliability_case as case_module

        ctx = await episode(db_session, incident_status="OPEN")
        taken = await make_case(
            db_session, project=ctx.project, component=ctx.component
        )

        async def always_colliding(session, *, project_id):
            return taken.reference

        monkeypatch.setattr(case_module, "next_reference", always_colliding)
        with pytest.raises(RuntimeError, match="could not allocate a case reference"):
            await case_module.open_case(
                db_session,
                project_id=ctx.project.id,
                trigger=CaseTrigger.INCIDENT,
                title="never opens",
            )

    async def test_an_incident_gets_one_live_case_not_two(self, db_session):
        """Dedup by incident: a second correlation pass must adopt the existing
        case rather than open a parallel one, or two operators work two cases
        for one incident."""
        ctx = await episode(db_session, incident_status="OPEN")
        first = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        found = await find_open_case_for_incident(
            db_session, incident_id=ctx.incident.id
        )
        assert found is not None
        assert found.id == first.id

    async def test_a_closed_case_is_not_found_as_live(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        await transition_case(db_session, case=case, target=CaseStatus.CANCELLED)
        assert (
            await find_open_case_for_incident(db_session, incident_id=ctx.incident.id)
            is None
        )


class TestCaseTransitions:
    async def test_an_illegal_transition_is_refused_with_the_legal_moves(
        self, db_session
    ):
        ctx = await episode(db_session)
        case = await make_case(db_session, project=ctx.project, component=ctx.component)
        with pytest.raises(CaseStateError) as caught:
            await transition_case(
                db_session, case=case, target=CaseStatus.EXECUTING, actor="ops"
            )
        message = str(caught.value)
        assert "OPEN" in message and "EXECUTING" in message

    async def test_a_legal_transition_is_recorded_on_the_timeline(self, db_session):
        ctx = await episode(db_session)
        case = await make_case(db_session, project=ctx.project, component=ctx.component)
        await transition_case(
            db_session, case=case, target=CaseStatus.TRIAGED, actor="duty"
        )
        entries = await case_timeline(db_session, case_id=case.id)
        changed = [
            entry for entry in entries if entry.event_type == "CASE_STATUS_CHANGED"
        ]
        assert len(changed) == 1
        assert changed[0].actor == "duty"
        #: The entry names the status it moved *from* and the one it landed on,
        #: so the timeline reads as a chain rather than a list of facts.
        assert changed[0].evidence["previous_status"] == "OPEN"
        assert changed[0].result == "TRIAGED"
        assert "TRIAGED" in changed[0].title

    async def test_a_mirrored_move_is_still_recorded(self, db_session):
        """``enforce=False`` exists so the control plane can reflect a fact that
        already happened — but a reflected move that leaves no trace would make
        the timeline lie about the case's history."""
        ctx = await episode(db_session)
        case = await make_case(db_session, project=ctx.project, component=ctx.component)
        await transition_case(
            db_session,
            case=case,
            target=CaseStatus.RESOLVED,
            actor="argus",
            reason="the incident resolved on its own",
            enforce=False,
        )
        entries = await case_timeline(db_session, case_id=case.id)
        mirrored = [
            entry for entry in entries if entry.event_type == "CASE_STATUS_CHANGED"
        ]
        assert mirrored, "a mirrored move must leave a trace"
        assert mirrored[0].result == "RESOLVED"
        assert mirrored[0].evidence["previous_status"] == "OPEN"
        #: The reason travels with the move, so a reader can tell a reflected
        #: fact from a decision someone took.
        assert mirrored[0].detail == "the incident resolved on its own"

    async def test_a_move_with_no_actor_is_attributed_to_the_system(self, db_session):
        """The default attribution: no actor means ARGUS did it, and the timeline
        says so rather than leaving the move looking manual."""
        ctx = await episode(db_session)
        case = await make_case(db_session, project=ctx.project, component=ctx.component)
        await transition_case(db_session, case=case, target=CaseStatus.TRIAGED)
        entries = await case_timeline(db_session, case_id=case.id)
        changed = [e for e in entries if e.event_type == "CASE_STATUS_CHANGED"]
        assert changed[0].system_action is True

    async def test_moving_to_the_current_status_is_a_no_op(self, db_session):
        ctx = await episode(db_session)
        case = await make_case(db_session, project=ctx.project, component=ctx.component)
        before = len(await case_timeline(db_session, case_id=case.id))
        await transition_case(db_session, case=case, target=CaseStatus.OPEN)
        after = len(await case_timeline(db_session, case_id=case.id))
        assert before == after


class TestTimeline:
    """§15: one unified story."""

    async def test_the_timeline_is_ordered_and_marked_with_its_source(self, db_session):
        ctx = await episode(db_session)
        case = await make_case(db_session, project=ctx.project, component=ctx.component)
        await append_timeline(
            db_session,
            case=case,
            kind=TimelineEntryKind.ANALYSIS,
            event_type="RCA_COMPLETED",
            title="Root cause analysis completed",
            source="causal_analysis",
            detail="two candidates above the evidence floor",
            component_id=ctx.component.id,
        )
        entries = await case_timeline(db_session, case_id=case.id)
        assert entries == sorted(entries, key=lambda entry: entry.sequence)
        analysis = [entry for entry in entries if entry.event_type == "RCA_COMPLETED"]
        assert analysis
        assert analysis[0].source == "causal_analysis"

    async def test_replaying_the_same_event_appends_nothing(self, db_session):
        """Events are replayed; a replayed fact is not a second occurrence."""
        ctx = await episode(db_session)
        case = await make_case(db_session, project=ctx.project, component=ctx.component)
        for _ in range(3):
            await append_timeline(
                db_session,
                case=case,
                kind=TimelineEntryKind.EVIDENCE,
                event_type="ANOMALY_LINKED",
                title="Anomaly linked",
                source="control_plane",
                dedup_key="anomaly-42",
            )
        entries = await case_timeline(db_session, case_id=case.id)
        assert len([e for e in entries if e.event_type == "ANOMALY_LINKED"]) == 1

    async def test_two_different_events_both_append(self, db_session):
        ctx = await episode(db_session)
        case = await make_case(db_session, project=ctx.project, component=ctx.component)
        await append_timeline(
            db_session,
            case=case,
            kind=TimelineEntryKind.EVIDENCE,
            event_type="ANOMALY_LINKED",
            title="Anomaly linked",
            source="control_plane",
            dedup_key="anomaly-1",
        )
        await append_timeline(
            db_session,
            case=case,
            kind=TimelineEntryKind.EVIDENCE,
            event_type="ANOMALY_LINKED",
            title="Anomaly linked",
            source="control_plane",
            dedup_key="anomaly-2",
        )
        entries = await case_timeline(db_session, case_id=case.id)
        assert len([e for e in entries if e.event_type == "ANOMALY_LINKED"]) == 2

    async def test_a_system_action_is_distinguishable_from_a_person(self, db_session):
        ctx = await episode(db_session)
        case = await make_case(db_session, project=ctx.project, component=ctx.component)
        await append_timeline(
            db_session,
            case=case,
            kind=TimelineEntryKind.EXECUTION,
            event_type="REMEDIATION_STARTED",
            title="Canary started",
            source="remediation",
            system_action=True,
            actor="argus",
        )
        entries = await case_timeline(db_session, case_id=case.id)
        started = [e for e in entries if e.event_type == "REMEDIATION_STARTED"]
        assert started and started[0].system_action is True


class TestEvidence:
    """§16: evidence is linked, never copied."""

    async def test_evidence_references_the_real_stored_rows(self, db_session):
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            environment=ctx.environment,
            component=ctx.component,
            incident=ctx.incident,
        )
        payload = (await collect_case_evidence(db_session, case=case)).as_dict()
        assert any(row["id"] == str(ctx.incident.id) for row in payload["incidents"])
        assert any(row["id"] == str(ctx.anomaly.id) for row in payload["anomalies"])
        assert any(
            row["id"] == str(ctx.deployment.id) for row in payload["deployments"]
        ), "the deployment that preceded the onset is evidence too"

    async def test_evidence_is_read_at_request_time_not_frozen_at_open(
        self, db_session
    ):
        """The property that makes a case trustworthy: evidence added *after* the
        case opened still appears, because the case holds references."""
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            environment=ctx.environment,
            component=ctx.component,
            incident=ctx.incident,
        )
        before = (await collect_case_evidence(db_session, case=case)).as_dict()
        seen_before = len(before.get("anomalies", []))

        from tests.phase8_helpers import emit_anomaly

        await emit_anomaly(
            db_session,
            ctx.project,
            ctx.environment,
            ctx.component,
            detected_at=utcnow(),
            metric_name="http.checkout.error_rate",
        )
        after = (await collect_case_evidence(db_session, case=case)).as_dict()
        assert len(after.get("anomalies", [])) > seen_before

    async def test_every_section_is_present_even_when_empty(self, db_session):
        """A reader must be able to tell "no analyses exist" from "the section is
        missing". Omitting an empty section would make a case with no RCA look
        like a case whose RCA endpoint failed."""
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            environment=ctx.environment,
            component=ctx.component,
            incident=ctx.incident,
        )
        payload = (await collect_case_evidence(db_session, case=case)).as_dict()
        for section in (
            "analyses",
            "reproductions",
            "patches",
            "predictions",
            "remediations",
            "commits",
        ):
            assert section in payload
            assert payload[section] == []
        assert payload["gaps"] == []

    async def test_a_case_with_no_incident_records_the_gap(self, db_session):
        """``gaps`` names what could *not* be collected. A case opened by hand,
        with no incident behind it, has no situation for historical retrieval to
        match — and saying so is more useful than an empty knowledge list."""
        from tests.phase6_helpers import build_project

        project, environment, component = await build_project(db_session)
        case = await make_case(
            db_session,
            project=project,
            environment=environment,
            component=component,
            trigger="OPERATOR",
        )
        payload = (await collect_case_evidence(db_session, case=case)).as_dict()
        assert any("no incident" in gap.lower() for gap in payload["gaps"]), payload[
            "gaps"
        ]

    async def test_a_deleted_component_does_not_break_the_evidence(self, db_session):
        """Evidence collection must survive a subject disappearing — the phase
        explicitly reports orphans rather than crashing on them (§88)."""
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            environment=ctx.environment,
            component=ctx.component,
            incident=ctx.incident,
        )
        await db_session.delete(ctx.anomaly)
        await db_session.flush()
        payload = (await collect_case_evidence(db_session, case=case)).as_dict()
        assert any(row["id"] == str(ctx.incident.id) for row in payload["incidents"])
        assert payload["anomalies"] == []


class TestListingAndScope:
    async def test_a_case_is_only_visible_within_its_project(self, db_session):
        """§42: ``get_case`` with a scope returns None rather than the row."""
        from tests.phase6_helpers import build_project

        ctx = await episode(db_session)
        case = await make_case(db_session, project=ctx.project, component=ctx.component)
        other_project, _env, _component = await build_project(db_session)

        assert await get_case(db_session, case_id=case.id) is not None
        assert (
            await get_case(db_session, case_id=case.id, project_id=other_project.id)
            is None
        )

    async def test_listing_is_scoped_and_filterable(self, db_session):
        from tests.phase6_helpers import build_project

        ctx = await episode(db_session)
        await make_case(db_session, project=ctx.project, component=ctx.component)
        other_project, other_env, other_component = await build_project(db_session)
        await make_case(db_session, project=other_project, component=other_component)

        mine = await list_cases(db_session, project_id=ctx.project.id)
        assert len(mine) == 1
        assert mine[0].project_id == ctx.project.id

        cancelled = await list_cases(
            db_session, project_id=ctx.project.id, statuses=[CaseStatus.CANCELLED]
        )
        assert cancelled == []

    async def test_a_summary_reports_progress_without_loading_everything(
        self, db_session
    ):
        ctx = await episode(db_session, incident_status="OPEN")
        case = await make_case(
            db_session,
            project=ctx.project,
            component=ctx.component,
            incident=ctx.incident,
        )
        summary = await case_summary(db_session, case=case)
        assert summary["reference"] == case.reference
        assert summary["status"] == "OPEN"
        assert summary["trigger"] == CaseTrigger.INCIDENT.value
        assert summary["timeline_entries"] >= 1
