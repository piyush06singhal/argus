"""ARGUS Remediation Control Plane (Phase 9 §10, §26).

This module is what makes Phase 9's native actions *real* rather than simulated.
A control-plane action writes a :class:`RemediationControl` row, and the code
that does the actual work — the ingestion worker, every background sweep — calls
:func:`is_paused` / :func:`feature_enabled` before it runs.

Why this is the honest scope: ARGUS owns a small, well-understood runtime
(queues, sweeps, its own feature gates). Acting on *that* is genuinely safe,
genuinely reversible and genuinely verifiable, so Phase 9 can offer real
execution without holding credentials to systems it does not own. Everything
outside this boundary is registered, proposed and approved but refused with
``ADAPTER_UNAVAILABLE`` until an operator configures an adapter — which is the
"default DENY" principle applied to integrations rather than rhetoric.

Three rules the implementation enforces:

* **A control is never mutated in place and never deleted.** Applying a new state
  supersedes the current row and inserts the next revision, so the history of
  what was paused, by which action, and for how long survives.
* **Expiry is honoured on read.** A control whose ``expires_at`` has passed is
  treated as absent *before* any caller acts on it, even if housekeeping has not
  yet run. A pause must not outlive its own deadline because a sweeper was busy.
* **Absence means "no control", never "paused".** The default of every lookup is
  the platform's normal behaviour; a lost row cannot silently stop the system.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.remediation import (
    RemediationControl,
    RemediationControlKind,
    RemediationControlState,
)
from app.services.remediation_clock import aware, is_expired, utcnow

logger = logging.getLogger(__name__)

#: States that mean "this control is actively holding something down".
_ACTIVE_STATES: frozenset[RemediationControlState] = frozenset(
    {
        RemediationControlState.PAUSED,
        RemediationControlState.DISABLED,
        RemediationControlState.SUPPRESSED,
    }
)


def scope_filter(
    column_project,
    column_environment,
    project_id: Optional[uuid.UUID],
    environment_id: Optional[uuid.UUID],
):
    """Match a control that applies to this scope.

    A row scoped to ``NULL`` project/environment is global; a row scoped to the
    same project applies to every environment of that project. Both are matched,
    which is what makes a project-level pause actually pause the project.
    """
    conditions = []
    if project_id is None:
        conditions.append(column_project.is_(None))
    else:
        conditions.append(or_(column_project.is_(None), column_project == project_id))
    if environment_id is None:
        conditions.append(column_environment.is_(None))
    else:
        conditions.append(
            or_(column_environment.is_(None), column_environment == environment_id)
        )
    return and_(*conditions)


async def _current_row(
    session: AsyncSession,
    kind: RemediationControlKind,
    scope_key: str,
    *,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
) -> Optional[RemediationControl]:
    stmt = (
        select(RemediationControl)
        .where(RemediationControl.kind == kind)
        .where(RemediationControl.scope_key == scope_key)
        .where(RemediationControl.is_current.is_(True))
        .where(
            scope_filter(
                RemediationControl.project_id,
                RemediationControl.environment_id,
                project_id,
                environment_id,
            )
        )
        .order_by(RemediationControl.applied_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def effective_state(
    session: AsyncSession,
    kind: RemediationControlKind,
    scope_key: str,
    *,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
) -> Optional[RemediationControlState]:
    """The state currently in force for this control, or ``None``.

    Expiry is applied here rather than left to housekeeping: a control past its
    deadline must not keep holding work down because a sweeper has not run.
    """
    now = aware(now or utcnow())
    row = await _current_row(
        session,
        kind,
        scope_key,
        project_id=project_id,
        environment_id=environment_id,
    )
    if row is None:
        return None
    if is_expired(row.expires_at, now=now):
        return None
    return row.state


async def is_paused(
    session: AsyncSession,
    job: str,
    *,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Whether a named background job is currently paused for this scope.

    This is the function the worker and every sweep call. It fails *open* on
    error (see :func:`safely_is_paused`) in the sense that a missing or
    unreadable control means "run normally" — an availability problem in the
    control table must not silently switch the platform off.
    """
    state = await effective_state(
        session,
        RemediationControlKind.BACKGROUND_JOB,
        job,
        project_id=project_id,
        environment_id=environment_id,
        now=now,
    )
    return state == RemediationControlState.PAUSED


async def safely_is_paused(
    session: AsyncSession,
    job: str,
    *,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
) -> bool:
    """``is_paused`` that never raises: a lookup failure means "not paused"."""
    try:
        return await is_paused(
            session,
            job,
            project_id=project_id,
            environment_id=environment_id,
            now=now,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "Control-plane lookup failed for %s (treating as active): %s", job, e
        )
        return False


async def paused_scope_ids(
    session: AsyncSession,
    job: str,
    *,
    now: Optional[datetime] = None,
) -> tuple[bool, set[uuid.UUID]]:
    """Every scope a job is paused in, in one query: ``(global, project_ids)``.

    A background sweep has no single scope, so a per-scope pause cannot be
    answered by :func:`is_paused` — which is correct for the runtime decision
    ("is this job paused *here*?") and useless for the sweep's own question
    ("is this job paused *anywhere*?"). Sweeps without a project loop use this
    so a pause costs one query per pass rather than one per row, and so an
    operator who pauses a reaper for one project does not stop it for every
    other project — a much wider effect than the action's own blast radius.

    Expiry is applied here too: a control past its deadline holds nothing down.
    """
    now = aware(now or utcnow())
    rows = (
        (
            await session.execute(
                select(RemediationControl)
                .where(RemediationControl.kind == RemediationControlKind.BACKGROUND_JOB)
                .where(RemediationControl.scope_key == job)
                .where(RemediationControl.is_current.is_(True))
                .where(RemediationControl.state.in_(tuple(_ACTIVE_STATES)))
            )
        )
        .scalars()
        .all()
    )
    global_pause = False
    project_ids: set[uuid.UUID] = set()
    for row in rows:
        if is_expired(row.expires_at, now=now):
            continue
        if row.project_id is None:
            global_pause = True
        else:
            project_ids.add(row.project_id)
    return global_pause, project_ids


async def safely_paused_scope_ids(
    session: AsyncSession,
    job: str,
    *,
    now: Optional[datetime] = None,
) -> tuple[bool, set[uuid.UUID]]:
    """``paused_scope_ids`` that never raises: a lookup failure means "not paused"."""
    try:
        return await paused_scope_ids(session, job, now=now)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "Control-plane scope lookup failed for %s (treating as active): %s", job, e
        )
        return False, set()


async def feature_enabled(
    session: AsyncSession,
    flag: str,
    *,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Whether an ARGUS-owned feature flag is currently enabled.

    Disabled means *this* flag was explicitly turned off by an audited action.
    No control row means enabled — the platform's configured default.
    """
    state = await effective_state(
        session,
        RemediationControlKind.FEATURE_FLAG,
        flag,
        project_id=project_id,
        environment_id=environment_id,
        now=now,
    )
    return state != RemediationControlState.DISABLED


async def safely_feature_enabled(
    session: AsyncSession,
    flag: str,
    *,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
) -> bool:
    """``feature_enabled`` that never raises: a lookup failure means "enabled"."""
    try:
        return await feature_enabled(
            session,
            flag,
            project_id=project_id,
            environment_id=environment_id,
            now=now,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "Feature-flag lookup failed for %s (treating as enabled): %s", flag, e
        )
        return True


async def suppressed_dependency_ids(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    environment_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
) -> set[str]:
    """Component ids currently suppressed from correlation.

    Consumers treat a suppressed dependency as *not evidence* for the duration
    of the suppression. Returning the ids rather than a boolean lets a caller
    filter a query in one pass.
    """
    now = aware(now or utcnow())
    stmt = (
        select(RemediationControl)
        .where(RemediationControl.kind == RemediationControlKind.DEPENDENCY_SUPPRESSION)
        .where(RemediationControl.is_current.is_(True))
        .where(RemediationControl.state == RemediationControlState.SUPPRESSED)
        .where(RemediationControl.project_id == project_id)
    )
    if environment_id is not None:
        stmt = stmt.where(
            or_(
                RemediationControl.environment_id.is_(None),
                RemediationControl.environment_id == environment_id,
            )
        )
    rows = (await session.execute(stmt)).scalars().all()
    suppressed: set[str] = set()
    for row in rows:
        if is_expired(row.expires_at, now=now):
            continue
        if row.component_id is not None:
            suppressed.add(str(row.component_id))
        elif row.scope_key:
            suppressed.add(str(row.scope_key))
    return suppressed


async def current_controls(
    session: AsyncSession,
    *,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    any_environment: bool = False,
) -> Sequence[RemediationControl]:
    """Every control row that is current (and not expired) for a scope.

    ``any_environment`` is for the *console*, not for the runtime. The runtime
    helpers (``is_paused``, ``feature_enabled``) must match a scope exactly: a
    job paused in staging must not read as paused in production. But an operator
    asking "what is ARGUS holding down in this project?" without naming an
    environment wants every control in force, and answering that question with
    only the environment-less rows would report "nothing is paused" while a job
    is in fact paused — the exact lie a console must not tell.
    """
    now = aware(utcnow())
    #: Annotated because the first element is a ``BinaryExpression`` and a
    #: disjunction is a ``ColumnElement``; the narrower inferred type would
    #: reject the ``or_`` clauses below.
    conditions: list[Any] = [RemediationControl.is_current.is_(True)]
    if project_id is None:
        conditions.append(RemediationControl.project_id.is_(None))
    else:
        conditions.append(
            or_(
                RemediationControl.project_id.is_(None),
                RemediationControl.project_id == project_id,
            )
        )
    if not any_environment:
        if environment_id is None:
            conditions.append(RemediationControl.environment_id.is_(None))
        else:
            conditions.append(
                or_(
                    RemediationControl.environment_id.is_(None),
                    RemediationControl.environment_id == environment_id,
                )
            )
    stmt = (
        select(RemediationControl)
        .where(and_(*conditions))
        .order_by(
            RemediationControl.environment_id,
            RemediationControl.kind,
            RemediationControl.scope_key,
        )
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [row for row in rows if not is_expired(row.expires_at, now=now)]


async def apply_control(
    session: AsyncSession,
    *,
    kind: RemediationControlKind,
    scope_key: str,
    state: RemediationControlState,
    project_id: Optional[uuid.UUID] = None,
    environment_id: Optional[uuid.UUID] = None,
    component_id: Optional[uuid.UUID] = None,
    applied_by_action_id: Optional[uuid.UUID] = None,
    applied_by: str = "system",
    reason: Optional[str] = None,
    expires_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> RemediationControl:
    """Apply a control state, superseding whatever was current.

    Idempotent by intent: re-applying the state that is already in force returns
    the existing row and writes nothing, so a retried execution cannot
    manufacture a chain of revisions that never happened.
    """
    now = aware(now or utcnow())
    existing = await _current_row(
        session,
        kind,
        scope_key,
        project_id=project_id,
        environment_id=environment_id,
    )
    if existing is not None and not is_expired(existing.expires_at, now=now):
        if existing.state == state:
            return existing
        if existing.expires_at != expires_at:
            existing.expires_at = expires_at

    revision = (existing.revision + 1) if existing is not None else 1
    previous_state = existing.state if existing is not None else None
    if existing is not None:
        existing.is_current = False
        existing.reverted_at = now

    row = RemediationControl(
        project_id=project_id,
        environment_id=environment_id,
        component_id=component_id,
        kind=kind,
        scope_key=scope_key,
        state=state,
        previous_state=previous_state,
        is_current=True,
        revision=revision,
        applied_by_action_id=applied_by_action_id,
        applied_at=now,
        expires_at=expires_at,
        applied_by=applied_by,
        reason=reason,
    )
    session.add(row)
    await session.flush()
    return row


async def revert_control(
    session: AsyncSession,
    control: RemediationControl,
    *,
    reverted_by: str = "system",
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[RemediationControl]:
    """Undo a control exactly.

    When the control replaced an earlier state, that state is restored as a new
    revision — so a pause that replaced an earlier pause is genuinely restored
    rather than assumed. When there was no earlier state, the control is simply
    retired (``is_current=False``) and the platform's normal behaviour returns.
    """
    now = aware(now or utcnow())
    if not control.is_current:
        return control
    if control.previous_state is None:
        control.is_current = False
        control.reverted_at = now
        await session.flush()
        return None
    restored = await apply_control(
        session,
        kind=control.kind,
        scope_key=control.scope_key,
        state=control.previous_state,
        project_id=control.project_id,
        environment_id=control.environment_id,
        component_id=control.component_id,
        applied_by_action_id=control.applied_by_action_id,
        applied_by=reverted_by,
        reason=reason or f"revert of revision {control.revision}",
        expires_at=None,
        now=now,
    )
    return restored


def describe(control: RemediationControl) -> dict:
    """A JSON-safe control description for the API and for verification."""
    return {
        "id": str(control.id),
        "kind": control.kind.value,
        "scope_key": control.scope_key,
        "state": control.state.value,
        "previous_state": (
            control.previous_state.value if control.previous_state else None
        ),
        "is_current": control.is_current,
        "revision": control.revision,
        "applied_at": control.applied_at.isoformat() if control.applied_at else None,
        "expires_at": control.expires_at.isoformat() if control.expires_at else None,
        "reverted_at": (
            control.reverted_at.isoformat() if control.reverted_at else None
        ),
        "applied_by": control.applied_by,
        "reason": control.reason,
    }


def iter_active(rows: Iterable[RemediationControl]) -> list[RemediationControl]:
    """Filter controls down to those actively holding something down."""
    return [row for row in rows if row.state in _ACTIVE_STATES]


__all__ = [
    "apply_control",
    "current_controls",
    "describe",
    "effective_state",
    "feature_enabled",
    "is_paused",
    "iter_active",
    "paused_scope_ids",
    "revert_control",
    "safely_feature_enabled",
    "safely_is_paused",
    "safely_paused_scope_ids",
    "scope_filter",
    "suppressed_dependency_ids",
]
