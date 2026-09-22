"""ARGUS Remediation Executor (Phase 9 §26, §27, §40, §41).

The only module in ARGUS that changes anything. It is therefore built to make
execution *hard*:

* **Authorization is re-checked here, and so is safety.** The executor re-runs the
  safety assessment with ``at_execution=True`` (fresh preconditions, fresh
  staleness, fresh duplicate check) and refuses unless the action is in a state
  the state machine says may apply an effect. An approval obtained ten minutes ago
  is not an approval of the world *now*.
* **Adapters are selected from the registry, never from input.** A handler is
  chosen by ``action_type``; parameters have already been validated against that
  definition. There is no path from a request body to a handler.
* **An unavailable adapter is a refusal, not a failure.** Actions whose effect
  lands on a system ARGUS does not own return ``REFUSED`` with
  ``ADAPTER_UNAVAILABLE`` — the honest answer for a platform that holds no
  credentials — and the action is BLOCKED with an explanation rather than being
  reported as something that tried and broke.
* **Retries are bounded and idempotent.** Every attempt carries an
  ``idempotency_key``; a retried job that finds a successful attempt for the same
  key returns it instead of applying the effect twice. Attempts are capped by the
  policy, and after that the breaker opens.
* **Dry runs cannot be mistaken for live work.** ``effect_applied`` is only true
  when a real adapter applied a real change; ``DRY_RUN`` and ``SHADOW`` return
  ``NOT_PERFORMED`` regardless of how well everything validated.

Adapters implement exactly one of three kinds:

``CONTROL_PLANE``
    ARGUS's own runtime, through :mod:`app.services.remediation_controls`. Real,
    reversible and verifiable, because the worker and the sweeps read the rows it
    writes.
``WORKSPACE``
    An isolated copy of a registered repository, for validating a verified patch.
    Never the live tree.
``EXTERNAL``
    A system ARGUS does not own. Refused until an operator configures an adapter.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.fix import Patch, PatchStatus, PatchVerificationRun, VerificationStatus
from app.models.remediation import (
    AdapterKind,
    ExecutionStatus,
    RemediationAction,
    RemediationActionType,
    RemediationControlKind,
    RemediationControlState,
    RemediationExecution,
    RemediationExecutionMode,
    RemediationFailureReason,
    RemediationStatus,
)
from app.services.remediation_clock import aware, utcnow
from app.services.remediation_controls import apply_control, revert_control
from app.services.remediation_registry import get_definition
from app.services.remediation_safety import SafetyEngine

logger = logging.getLogger(__name__)
settings = get_settings()


#: The state each control-plane action applies.
_CONTROL_TARGET_STATE: dict[RemediationActionType, RemediationControlState] = {
    RemediationActionType.PAUSE_BACKGROUND_JOB: RemediationControlState.PAUSED,
    RemediationActionType.RESUME_BACKGROUND_JOB: RemediationControlState.RESUMED,
    RemediationActionType.DISABLE_FEATURE_FLAG: RemediationControlState.DISABLED,
    RemediationActionType.ENABLE_FEATURE_FLAG: RemediationControlState.ENABLED,
    RemediationActionType.DISABLE_DEGRADED_DEPENDENCY: RemediationControlState.SUPPRESSED,
}

_CONTROL_KIND: dict[RemediationActionType, RemediationControlKind] = {
    RemediationActionType.PAUSE_BACKGROUND_JOB: RemediationControlKind.BACKGROUND_JOB,
    RemediationActionType.RESUME_BACKGROUND_JOB: RemediationControlKind.BACKGROUND_JOB,
    RemediationActionType.DISABLE_FEATURE_FLAG: RemediationControlKind.FEATURE_FLAG,
    RemediationActionType.ENABLE_FEATURE_FLAG: RemediationControlKind.FEATURE_FLAG,
    RemediationActionType.DISABLE_DEGRADED_DEPENDENCY: RemediationControlKind.DEPENDENCY_SUPPRESSION,
}

#: The control's scope key for each control-plane action.
_CONTROL_SCOPE_KEY_PARAMETER: dict[RemediationActionType, str] = {
    RemediationActionType.PAUSE_BACKGROUND_JOB: "job",
    RemediationActionType.RESUME_BACKGROUND_JOB: "job",
    RemediationActionType.DISABLE_FEATURE_FLAG: "flag",
    RemediationActionType.ENABLE_FEATURE_FLAG: "flag",
    RemediationActionType.DISABLE_DEGRADED_DEPENDENCY: "dependency_component_id",
}


@dataclass
class AdapterResult:
    """What an adapter did, in structured form.

    ``effect_applied`` is the field the whole pipeline keys off: only a ``True``
    here may lead to verification, and only verification may lead to a claim that
    a remediation worked.
    """

    status: ExecutionStatus
    effect_applied: bool = False
    steps: list[dict[str, Any]] = field(default_factory=list)
    control_ids: list[str] = field(default_factory=list)
    output_summary: str = ""
    failure_reason: Optional[RemediationFailureReason] = None
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


class AdapterUnavailable(RuntimeError):
    """The platform has no way to apply this action in this environment."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BaseAdapter:
    """Common adapter surface. One method: apply, or explain why not."""

    name = "base"
    kind = AdapterKind.EXTERNAL

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def available(self, action: RemediationAction) -> tuple[bool, Optional[str]]:
        """Whether this adapter can act here. ``(False, reason)`` means refuse."""
        return True, None

    async def apply(
        self, action: RemediationAction, *, mode: RemediationExecutionMode
    ) -> AdapterResult:  # pragma: no cover - abstract
        raise NotImplementedError

    async def validate_only(
        self, action: RemediationAction, *, mode: RemediationExecutionMode
    ) -> AdapterResult:
        """Plan what would be done, without doing any of it.

        The default implementation is honest about being a plan: it reports the
        steps it *would* take and applies nothing.
        """
        return AdapterResult(
            status=ExecutionStatus.NOT_PERFORMED,
            effect_applied=False,
            steps=[
                {
                    "step": "validate",
                    "detail": "parameters and preconditions validated",
                },
                {
                    "step": "would_apply",
                    "detail": f"{self.name} would apply {action.action_type.value}",
                },
            ],
            output_summary=(
                f"{mode.value}: {action.action_type.value} was validated but not "
                "applied"
            ),
        )

    async def revert(
        self, action: RemediationAction, execution: RemediationExecution
    ) -> AdapterResult:
        """Undo the effect of an execution, if this adapter can."""
        return AdapterResult(
            status=ExecutionStatus.REFUSED,
            effect_applied=False,
            failure_reason=RemediationFailureReason.ADAPTER_UNAVAILABLE,
            output_summary=f"{self.name} cannot revert {action.action_type.value}",
        )


class ControlPlaneAdapter(BaseAdapter):
    """Applies ARGUS-native controls (§10).

    Every effect is a row the running platform reads before it does work, which is
    why this adapter can honestly claim to have applied something: the pause it
    writes is the pause the worker obeys.
    """

    name = "argus_control_plane"
    kind = AdapterKind.CONTROL_PLANE

    async def apply(
        self, action: RemediationAction, *, mode: RemediationExecutionMode
    ) -> AdapterResult:
        parameters = action.parameters or {}
        scope_parameter = _CONTROL_SCOPE_KEY_PARAMETER[action.action_type]
        scope_key_value = parameters.get(scope_parameter)
        if not scope_key_value:
            return AdapterResult(
                status=ExecutionStatus.FAILED,
                failure_reason=RemediationFailureReason.PARAMETER_INVALID,
                error=f"parameter '{scope_parameter}' is required",
                output_summary="the control target could not be determined",
            )

        kind = _CONTROL_KIND[action.action_type]
        state = _CONTROL_TARGET_STATE[action.action_type]
        now = aware(utcnow())
        duration = parameters.get("duration_seconds")
        expires_at = (
            now + timedelta(seconds=int(duration))
            if isinstance(duration, int)
            else None
        )
        scope_key = str(scope_key_value)

        # Reverting a paused job that is *already* paused must restore, not
        # re-apply: the control plane owns that decision, so it is delegated.
        from app.services.remediation_controls import _current_row  # internal by design

        existing = await _current_row(
            self._session,
            kind,
            scope_key,
            project_id=action.project_id,
            environment_id=action.environment_id,
        )

        if (
            action.action_type == RemediationActionType.RESUME_BACKGROUND_JOB
            and existing
        ):
            restored = await revert_control(
                self._session,
                existing,
                reverted_by=action.executed_by or "remediation-executor",
                reason=f"resumed by action {action.id}",
                now=now,
            )
            control = restored or existing
            return AdapterResult(
                status=ExecutionStatus.SUCCEEDED,
                effect_applied=True,
                steps=[
                    {
                        "step": "revert_control",
                        "detail": f"'{scope_key}' resumed from {existing.state.value}",
                    }
                ],
                control_ids=[str(control.id)],
                output_summary=f"'{scope_key}' resumed",
                metadata={"scope_key": scope_key, "kind": kind.value},
            )

        control = await apply_control(
            self._session,
            kind=kind,
            scope_key=scope_key,
            state=state,
            project_id=action.project_id,
            environment_id=action.environment_id,
            component_id=action.component_id,
            applied_by_action_id=action.id,
            applied_by=action.executed_by or "remediation-executor",
            reason=f"applied by remediation action {action.id}",
            expires_at=expires_at,
            now=now,
        )
        return AdapterResult(
            status=ExecutionStatus.SUCCEEDED,
            effect_applied=True,
            steps=[
                {
                    "step": "apply_control",
                    "detail": (
                        f"'{scope_key}' set to {state.value} (revision "
                        f"{control.revision})"
                    ),
                }
            ],
            control_ids=[str(control.id)],
            output_summary=(
                f"'{scope_key}' is now {state.value}"
                + (f" until {expires_at.isoformat()}" if expires_at is not None else "")
            ),
            metadata={
                "scope_key": scope_key,
                "kind": kind.value,
                "previous_state": (
                    control.previous_state.value if control.previous_state else None
                ),
            },
        )

    async def revert(
        self, action: RemediationAction, execution: RemediationExecution
    ) -> AdapterResult:
        """Undo an applied control by restoring its previous state exactly."""
        from app.models.remediation import RemediationControl

        ids = [str(value) for value in (execution.control_ids or [])]
        reverted = 0
        for raw in ids:
            try:
                control_id = uuid.UUID(raw)
            except (ValueError, TypeError):
                continue
            control = await self._session.get(RemediationControl, control_id)
            if control is None or not control.is_current:
                continue
            await revert_control(
                self._session,
                control,
                reverted_by="remediation-rollback",
                reason=f"rollback of action {action.id}",
            )
            reverted += 1
        if reverted == 0:
            return AdapterResult(
                status=ExecutionStatus.REFUSED,
                effect_applied=False,
                failure_reason=RemediationFailureReason.PRECONDITION_FAILED,
                output_summary=(
                    "no current control row belonged to that execution, so there "
                    "was nothing to revert"
                ),
            )
        return AdapterResult(
            status=ExecutionStatus.SUCCEEDED,
            effect_applied=True,
            steps=[
                {"step": "revert_control", "detail": f"reverted {reverted} control(s)"}
            ],
            output_summary=f"reverted {reverted} control(s)",
        )


async def resolve_patch_repository(session: AsyncSession, patch: Patch):
    """The repository a patch was generated against, if it is still registered.

    Resolution goes through the patch's own ``repository_id``, falling back to
    its fix hypothesis. A patch whose repository is gone cannot be re-validated in
    a workspace, and the adapter says so rather than seeding from nothing.
    """
    from app.models.deployment import CodeRepository

    repository_id = patch.repository_id
    if repository_id is None and patch.fix_hypothesis_id is not None:
        from app.models.fix import FixHypothesis

        hypothesis = await session.get(FixHypothesis, patch.fix_hypothesis_id)
        if hypothesis is not None:
            repository_id = hypothesis.repository_id
    if repository_id is None:
        return None
    return await session.get(CodeRepository, repository_id)


class WorkspaceAdapter(BaseAdapter):
    """Validates a verified patch inside an isolated workspace copy (§26).

    This adapter never touches a live tree. It seeds a workspace from the
    repository the patch was generated against, applies the diff with the Phase 7
    git allow-list, and — when a test command is detectable — runs it. If no
    repository or provider is available it refuses rather than pretending.
    """

    name = "argus_patch_workspace"
    kind = AdapterKind.WORKSPACE

    async def _resolve_patch(self, action: RemediationAction) -> Optional[Patch]:
        raw = (action.parameters or {}).get("patch_id")
        if raw is None:
            return None
        try:
            patch_id = uuid.UUID(str(raw))
        except (ValueError, TypeError):
            return None
        return await self._session.get(Patch, patch_id)

    async def available(self, action: RemediationAction) -> tuple[bool, Optional[str]]:
        patch = await self._resolve_patch(action)
        if patch is None:
            return False, "the referenced patch does not exist"
        if patch.status != PatchStatus.VERIFIED:
            return False, f"the patch status is {patch.status.value}, not VERIFIED"
        run = (
            (
                await self._session.execute(
                    select(PatchVerificationRun)
                    .where(PatchVerificationRun.patch_id == patch.id)
                    .where(PatchVerificationRun.status == VerificationStatus.VERIFIED)
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if run is None:
            return False, "the patch has no passed verification run"
        try:
            repository = await resolve_patch_repository(self._session, patch)
        except Exception:  # noqa: BLE001 - an unusable repository is a refusal
            repository = None
        if repository is None:
            return False, (
                "the repository this patch was generated against is not "
                "registered, so no workspace can be seeded"
            )
        if not (repository.local_path or repository.repository_url):
            return False, "the repository has neither a local path nor a URL"
        return True, None

    async def apply(
        self, action: RemediationAction, *, mode: RemediationExecutionMode
    ) -> AdapterResult:
        ok, reason = await self.available(action)
        if not ok:
            raise AdapterUnavailable(reason or "workspace adapter unavailable")

        patch = await self._resolve_patch(action)
        assert patch is not None  # guaranteed by available()
        repository = await resolve_patch_repository(self._session, patch)
        if repository is None:  # pragma: no cover - race with available()
            raise AdapterUnavailable("the repository is no longer registered")

        from app.services.patch_workspace import PatchWorkspaceManager
        from app.services.repository_provider import provider_for_repository

        try:
            provider = provider_for_repository(repository)
        except Exception as error:  # noqa: BLE001 - provider errors are refusals
            raise AdapterUnavailable(
                f"the repository provider refused: {error}"
            ) from error
        source_dir = getattr(provider, "root", None)
        if not source_dir:
            raise AdapterUnavailable(
                "the repository provider cannot expose a local tree to seed a "
                "workspace from"
            )

        manager = PatchWorkspaceManager()
        try:
            workspace, record = manager.create(
                patch_experiment_id=action.id,
                candidate_key=f"remediation{action.attempt}",
                source_dir=Path(str(source_dir)),
            )
        except Exception as error:  # noqa: BLE001 - reported as a refusal
            raise AdapterUnavailable(
                f"a workspace could not be created: {type(error).__name__}: {error}"
            ) from error

        steps: list[dict[str, Any]] = [
            {
                "step": "workspace_created",
                "detail": (
                    f"seeded {record.file_count} files at "
                    f"{(record.base_commit_sha or '')[:12]}"
                ),
            }
        ]
        try:
            applied = workspace.apply_patch(patch.patch_content or "")
            steps.append(
                {
                    "step": "patch_applied",
                    "detail": str(applied.get("detail", "applied on a clean tree")),
                }
            )
            diff = workspace.working_diff()
            test_result: dict[str, Any] = {"result": "NOT_OBSERVABLE"}
            if bool((action.parameters or {}).get("verify_tests", True)):
                test_result = await self._run_tests(workspace, steps=steps)
            return AdapterResult(
                status=ExecutionStatus.SUCCEEDED,
                effect_applied=True,
                steps=steps,
                output_summary=(
                    "the patch applied cleanly to a fresh workspace copy; "
                    f"tests: {test_result.get('result')}"
                ),
                metadata={
                    "workspace_root": record.root_path,
                    "base_commit_sha": record.base_commit_sha,
                    "diff_bytes": len(diff or ""),
                    "test_result": test_result,
                },
            )
        except Exception as error:  # noqa: BLE001 - reported, then cleaned up
            return AdapterResult(
                status=ExecutionStatus.FAILED,
                effect_applied=False,
                steps=steps,
                failure_reason=RemediationFailureReason.HANDLER_ERROR,
                error=f"{type(error).__name__}: {error}"[:2000],
                output_summary="the patch could not be applied to the workspace",
            )
        finally:
            # The workspace is disposable by design: nothing survives an attempt.
            manager.destroy(workspace)

    async def _run_tests(
        self, workspace: Any, *, steps: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Run the detected test command, or report that none was detectable."""
        from app.services.patch_commands import CommandExecutor, detect_commands

        root = Path(str(workspace.root))
        commands = detect_commands(root)
        if not commands:
            steps.append(
                {
                    "step": "tests",
                    "detail": "no test command was detected in the workspace",
                }
            )
            return {"result": "NOT_OBSERVABLE", "reason": "no test command detected"}
        name = next(iter(commands))
        executor = CommandExecutor()
        try:
            result = executor.execute(root, name)
        except Exception as error:  # noqa: BLE001 - an unrunnable command is data
            steps.append(
                {"step": "tests", "detail": f"'{name}' could not be run: {error}"}
            )
            return {"result": "NOT_OBSERVABLE", "reason": str(error)[:300]}
        steps.append({"step": "tests", "detail": f"'{name}' exited {result.exit_code}"})
        if result.timed_out:
            return {
                "result": "FAIL",
                "command": name,
                "timed_out": True,
                "output_tail": result.output_tail,
            }
        if result.exit_code is None:
            return {
                "result": "NOT_OBSERVABLE",
                "command": name,
                "reason": "the test command produced no exit code",
            }
        return {
            "result": "PASS" if result.exit_code == 0 else "FAIL",
            "command": name,
            "exit_code": result.exit_code,
            "output_tail": result.output_tail,
        }


class ExternalAdapter(BaseAdapter):
    """A placeholder for systems ARGUS does not own (§2 of the principles).

    It exists so the refusal is a first-class, testable path with a real reason,
    rather than an action that appears to be attempted and mysteriously fails.
    Configuring a real adapter is the operator's decision and requires credentials
    ARGUS deliberately does not ship with.
    """

    name = "unconfigured_external"
    kind = AdapterKind.EXTERNAL

    def __init__(
        self, session: AsyncSession, action_type: RemediationActionType
    ) -> None:
        super().__init__(session)
        self._action_type = action_type

    async def available(self, action: RemediationAction) -> tuple[bool, Optional[str]]:
        definition = get_definition(action.action_type)
        return False, definition.unavailable_reason or (
            "no adapter is configured for this external system"
        )

    async def apply(
        self, action: RemediationAction, *, mode: RemediationExecutionMode
    ) -> AdapterResult:
        _, reason = await self.available(action)
        raise AdapterUnavailable(reason or "no adapter configured")


def build_adapter(
    session: AsyncSession, action_type: RemediationActionType
) -> BaseAdapter:
    """Select the adapter for an action type from the registry's declaration.

    The registry — not the request — decides where an action's effect lands, so
    there is no way to reach a different adapter by changing a payload.
    """
    definition = get_definition(action_type)
    if definition.adapter_kind == AdapterKind.CONTROL_PLANE:
        return ControlPlaneAdapter(session)
    if definition.adapter_kind == AdapterKind.WORKSPACE:
        return WorkspaceAdapter(session)
    return ExternalAdapter(session, action_type)


def idempotency_key(action: RemediationAction) -> str:
    """A deterministic key for "this attempt of this action"."""
    import hashlib

    payload = f"{action.id}:{action.attempt}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ExecutionOutcome:
    """The executor's report for one attempt."""

    execution: Optional[RemediationExecution]
    status: ExecutionStatus
    effect_applied: bool
    failure_reason: Optional[RemediationFailureReason] = None
    detail: str = ""
    refused: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status == ExecutionStatus.SUCCEEDED

    @property
    def should_rollback(self) -> bool:
        """Whether a partial effect is known to be in place and needs undoing."""
        return self.effect_applied and self.status != ExecutionStatus.SUCCEEDED


class RemediationExecutor:
    """Applies an authorized action, once, with everything re-checked."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        execution_timeout_seconds: int = settings.REMEDIATION_EXECUTION_TIMEOUT_SECONDS,
    ) -> None:
        self._session = session
        self._timeout = min(
            execution_timeout_seconds,
            settings.REMEDIATION_HARD_EXECUTION_TIMEOUT_SECONDS,
        )

    async def execute(
        self,
        action: RemediationAction,
        *,
        mode: Optional[RemediationExecutionMode] = None,
        actor: str = "executor",
        now: Optional[Any] = None,
        force_dry_run: bool = False,
    ) -> ExecutionOutcome:
        """Execute one attempt of ``action``.

        Returns an :class:`ExecutionOutcome`; the caller decides what the action's
        status becomes. Raising is reserved for genuine programming errors — every
        expected refusal is a value, so a refusal cannot be lost in an exception
        handler.
        """
        now = aware(now or utcnow())
        mode = mode or action.execution_mode

        if not settings.REMEDIATION_EXECUTION_ENABLED:
            return ExecutionOutcome(
                execution=None,
                status=ExecutionStatus.REFUSED,
                effect_applied=False,
                failure_reason=RemediationFailureReason.EXECUTION_DISABLED,
                detail="remediation execution is disabled in this process",
                refused=True,
            )

        if force_dry_run and mode.allows_live_effect:
            mode = RemediationExecutionMode.DRY_RUN

        #: The idempotency check runs before the status guard, because a
        #: redelivered queue message is not an execution *attempt*: by the time
        #: it arrives the action may legitimately be in ``VERIFYING`` or even
        #: ``VERIFIED``, and reporting "an attempt with this key already
        #: applied" is both accurate and harmless. A first attempt has no
        #: matching key, so this can never authorize anything by itself.
        key = idempotency_key(action)
        replay = await self._find_applied(key)
        if replay is not None:
            return ExecutionOutcome(
                execution=replay,
                status=replay.status,
                effect_applied=replay.effect_applied,
                detail="an attempt with this idempotency key already applied",
            )

        #: ``EXECUTING`` is admitted alongside the two authorizing states. The
        #: orchestration layer moves an action to ``EXECUTING`` *after*
        #: :func:`may_apply_effect` has been checked, and the state machine makes
        #: that transition reachable only from ``AUTHORIZED`` or ``SCHEDULED`` —
        #: so refusing it here would refuse every execution the pipeline itself
        #: had just authorized. The guard's job is to stop an action that was
        #: never authorized, and those are exactly the states it still excludes.
        permitted_statuses = (
            RemediationStatus.AUTHORIZED,
            RemediationStatus.SCHEDULED,
            RemediationStatus.EXECUTING,
        )
        if action.status not in permitted_statuses and mode.allows_live_effect:
            return ExecutionOutcome(
                execution=None,
                status=ExecutionStatus.REFUSED,
                effect_applied=False,
                failure_reason=RemediationFailureReason.APPROVAL_REQUIRED,
                detail=(
                    f"the action is in {action.status.value}; only an authorized or "
                    "scheduled action may apply an effect"
                ),
                refused=True,
            )

        # Fresh safety assessment: approvals are minutes old, systems are not.
        report = await SafetyEngine(self._session).assess(
            action, now=now, at_execution=True
        )
        if not report.passed:
            return ExecutionOutcome(
                execution=None,
                status=ExecutionStatus.REFUSED,
                effect_applied=False,
                failure_reason=(
                    report.failure_reason
                    or RemediationFailureReason.PRECONDITION_FAILED
                ),
                detail="safety re-assessment failed at execution time: "
                + ", ".join(report.blocking),
                refused=True,
            )

        adapter = build_adapter(self._session, action.action_type)

        record = RemediationExecution(
            action_id=action.id,
            project_id=action.project_id,
            environment_id=action.environment_id,
            component_id=action.component_id,
            action_type=action.action_type,
            mode=mode,
            adapter_kind=adapter.kind,
            adapter_name=adapter.name,
            attempt=action.attempt,
            status=ExecutionStatus.PENDING,
            idempotency_key=key,
            started_at=now,
            executed_by=actor,
        )
        self._session.add(record)
        await self._session.flush()

        if not mode.allows_live_effect:
            result = await adapter.validate_only(action, mode=mode)
            await self._finalize(record, result, now=now)
            return ExecutionOutcome(
                execution=record,
                status=result.status,
                effect_applied=False,
                detail=result.output_summary,
            )

        available, reason = await adapter.available(action)
        if not available:
            result = AdapterResult(
                status=ExecutionStatus.REFUSED,
                effect_applied=False,
                failure_reason=RemediationFailureReason.ADAPTER_UNAVAILABLE,
                output_summary=reason or "no adapter is available",
                error=reason,
            )
            await self._finalize(record, result, now=now)
            return ExecutionOutcome(
                execution=record,
                status=ExecutionStatus.REFUSED,
                effect_applied=False,
                failure_reason=RemediationFailureReason.ADAPTER_UNAVAILABLE,
                detail=reason or "no adapter is available",
                refused=True,
            )

        record.status = ExecutionStatus.RUNNING
        await self._session.flush()
        try:
            result = await asyncio.wait_for(
                adapter.apply(action, mode=mode), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            result = AdapterResult(
                status=ExecutionStatus.FAILED,
                effect_applied=False,
                failure_reason=RemediationFailureReason.TIMEOUT,
                error=f"the adapter exceeded {self._timeout}s",
                output_summary="the attempt timed out",
            )
        except AdapterUnavailable as error:
            result = AdapterResult(
                status=ExecutionStatus.REFUSED,
                effect_applied=False,
                failure_reason=RemediationFailureReason.ADAPTER_UNAVAILABLE,
                error=error.message,
                output_summary=error.message,
            )
        except Exception as error:  # noqa: BLE001 - a handler failure is data
            result = AdapterResult(
                status=ExecutionStatus.FAILED,
                effect_applied=False,
                failure_reason=RemediationFailureReason.HANDLER_ERROR,
                error=f"{type(error).__name__}: {error}"[:2000],
                output_summary="the adapter raised an unexpected error",
            )

        await self._finalize(record, result, now=now)
        return ExecutionOutcome(
            execution=record,
            status=result.status,
            effect_applied=result.effect_applied,
            failure_reason=result.failure_reason,
            detail=result.output_summary,
            refused=result.status == ExecutionStatus.REFUSED,
        )

    async def reverts_for(
        self, action: RemediationAction, execution: Optional[RemediationExecution]
    ) -> AdapterResult:
        """Undo one applied execution through its own adapter."""
        if execution is None or not execution.effect_applied:
            return AdapterResult(
                status=ExecutionStatus.REFUSED,
                effect_applied=False,
                failure_reason=RemediationFailureReason.NOT_ACTIONABLE,
                output_summary="the execution applied no effect, so nothing is undone",
            )
        adapter = build_adapter(self._session, action.action_type)
        return await adapter.revert(action, execution)

    async def _find_applied(self, key: str) -> Optional[RemediationExecution]:
        """A prior attempt with the same key that actually applied something."""
        stmt = (
            select(RemediationExecution)
            .where(RemediationExecution.idempotency_key == key)
            .where(RemediationExecution.effect_applied.is_(True))
            .order_by(RemediationExecution.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def _finalize(
        self, record: RemediationExecution, result: AdapterResult, *, now: Any
    ) -> None:
        """Write the attempt's outcome onto its row."""
        finished = aware(utcnow())
        record.status = result.status
        record.effect_applied = result.effect_applied
        record.steps = result.steps
        record.control_ids = result.control_ids
        record.output_summary = (result.output_summary or "")[:4000]
        record.failure_reason = result.failure_reason
        record.error = (result.error or "")[:2000] if result.error else None
        record.completed_at = finished
        record.duration_ms = max(
            0, int((finished - aware(record.started_at or now)).total_seconds() * 1000)
        )
        if result.metadata:
            record.metadata_ = result.metadata
        await self._session.flush()

    async def pending_replays(self) -> Sequence[RemediationExecution]:
        """Attempts that finished RUNNING — i.e. a process died mid-attempt.

        The sweeper uses this to close them out; an attempt stuck in RUNNING
        forever would leave its action occupying a concurrency slot for good.
        """
        stmt = (
            select(RemediationExecution)
            .where(RemediationExecution.status == ExecutionStatus.RUNNING)
            .order_by(RemediationExecution.started_at)
            .limit(100)
        )
        return (await self._session.execute(stmt)).scalars().all()


__all__ = [
    "AdapterResult",
    "AdapterUnavailable",
    "BaseAdapter",
    "ControlPlaneAdapter",
    "ExecutionOutcome",
    "ExternalAdapter",
    "RemediationExecutor",
    "WorkspaceAdapter",
    "build_adapter",
    "idempotency_key",
    "resolve_patch_repository",
]
