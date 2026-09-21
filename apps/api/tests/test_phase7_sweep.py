"""Phase 7 — the fix-workspace reaper (§48, §54).

The reaper exists for the crash window between workspace creation and
cleanup; these tests pin the grace period, the unconditional destroy, and
the honest summary.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.fix import PatchWorkspace, WorkspaceStatus
from app.services.fix_sweep import sweep_fix_workspaces_once


def _factory(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
async def test_stale_workspace_reaped(db_session, db_engine, tmp_path):
    factory = _factory(db_engine)
    stale_dir = tmp_path / "stale-ws"
    stale_dir.mkdir()
    row = PatchWorkspace(
        project_id=None,
        patch_id=None,
        branch_name="argus/fix/old/one",
        root_path=str(stale_dir),
        status=WorkspaceStatus.PATCH_APPLIED,
        created_at_workspace=datetime.now(timezone.utc) - timedelta(hours=3),
    )
    # project_id/patch_id are non-nullable FKs; create minimal rows.
    from app.models.project import SoftwareProject
    from app.models.fix import Patch, FixHypothesis
    from app.models.incident import Incident, IncidentSeverity, IncidentStatus
    from app.models.project import Environment
    from app.models.system import SystemComponent

    suffix = datetime.now(timezone.utc).strftime("%H%M%S%f")
    project = SoftwareProject(name=f"p7-sweep {suffix}", slug=f"p7-sweep-{suffix}")
    db_session.add(project)
    await db_session.flush()
    environment = Environment(
        project_id=project.id, name="production", environment_type="PRODUCTION"
    )
    db_session.add(environment)
    await db_session.flush()
    component = SystemComponent(
        project_id=project.id, name="checkout", component_type="SERVICE"
    )
    db_session.add(component)
    await db_session.flush()
    onset = datetime.now(timezone.utc) - timedelta(minutes=30)
    incident = Incident(
        project_id=project.id,
        environment_id=environment.id,
        primary_component_id=component.id,
        title="sweep fixture",
        description="d",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        detected_at=onset,
        started_at=onset,
    )
    db_session.add(incident)
    await db_session.flush()
    hypothesis = FixHypothesis(
        project_id=project.id,
        incident_id=incident.id,
        title="h",
        description="d",
        proposed_change="c",
    )
    db_session.add(hypothesis)
    await db_session.flush()
    patch = Patch(
        project_id=project.id,
        fix_hypothesis_id=hypothesis.id,
        patch_content="",
    )
    db_session.add(patch)
    await db_session.flush()
    row.project_id = project.id
    row.patch_id = patch.id
    db_session.add(row)
    await db_session.commit()

    now = datetime.now(timezone.utc)
    summary = await sweep_fix_workspaces_once(factory, now=now)

    assert summary["stale_candidates"] >= 1
    assert summary["destroyed"] >= 1
    assert not stale_dir.exists(), "the stale directory must be gone"

    # Read through a *fresh* session so the assertion sees committed state.
    async with factory() as fresh:
        re_read = await fresh.get(PatchWorkspace, row.id)
        assert re_read.status == WorkspaceStatus.DESTROYED
        assert re_read.destroyed_at is not None
        assert re_read.workspace_metadata.get("reaped") is True


@pytest.mark.asyncio
async def test_recent_workspace_left_alone(db_session, db_engine, tmp_path):
    """The grace period protects a slow-but-alive verification."""
    factory = _factory(db_engine)
    from app.models.project import SoftwareProject
    from app.models.fix import Patch, FixHypothesis
    from app.models.incident import Incident, IncidentSeverity, IncidentStatus
    from app.models.project import Environment
    from app.models.system import SystemComponent

    suffix = datetime.now(timezone.utc).strftime("%H%M%S%f")
    project = SoftwareProject(name=f"p7-fresh {suffix}", slug=f"p7-fresh-{suffix}")
    db_session.add(project)
    await db_session.flush()
    environment = Environment(
        project_id=project.id, name="production", environment_type="PRODUCTION"
    )
    db_session.add(environment)
    await db_session.flush()
    component = SystemComponent(
        project_id=project.id, name="checkout", component_type="SERVICE"
    )
    db_session.add(component)
    await db_session.flush()
    onset = datetime.now(timezone.utc) - timedelta(minutes=30)
    incident = Incident(
        project_id=project.id,
        environment_id=environment.id,
        primary_component_id=component.id,
        title="sweep fixture fresh",
        description="d",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        detected_at=onset,
        started_at=onset,
    )
    db_session.add(incident)
    await db_session.flush()
    hypothesis = FixHypothesis(
        project_id=project.id,
        incident_id=incident.id,
        title="h",
        description="d",
        proposed_change="c",
    )
    db_session.add(hypothesis)
    await db_session.flush()
    patch = Patch(
        project_id=project.id, fix_hypothesis_id=hypothesis.id, patch_content=""
    )
    db_session.add(patch)
    await db_session.flush()

    live_dir = tmp_path / "live-ws"
    live_dir.mkdir()
    row = PatchWorkspace(
        project_id=project.id,
        patch_id=patch.id,
        branch_name="argus/fix/new/one",
        root_path=str(live_dir),
        status=WorkspaceStatus.PATCH_APPLIED,
        created_at_workspace=datetime.now(timezone.utc),  # just started
    )
    db_session.add(row)
    await db_session.commit()

    summary = await sweep_fix_workspaces_once(factory)

    assert summary["stale_candidates"] == 0
    assert live_dir.exists(), "a live workspace inside the grace period survives"

    async with factory() as fresh:
        re_read = await fresh.get(PatchWorkspace, row.id)
        assert re_read.status == WorkspaceStatus.PATCH_APPLIED
