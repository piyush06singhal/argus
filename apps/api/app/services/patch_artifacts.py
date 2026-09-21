"""ARGUS Patch Artifact Store (Phase 7 §45, §72).

Records the evidence a reviewer needs *outside* the database: the diff, the
command output, the generated regression test, the comparison and the verdict.
Each artifact is written to disk under the artifact root, hashed with SHA-256,
and stored as a row that points at it.

Two properties matter:

* **Content-addressed.** ``content_hash`` is computed from the exact bytes
  written, so a reviewer can re-hash the file and compare. A hash that were
  merely copied from a response body would prove nothing.
* **Immutable once terminal.** Artifacts belonging to a verification run that
  reached a terminal state are stored with ``immutable=True``; nothing rewrites
  them, so the evidence for a verdict cannot change after the fact.

Artifacts deliberately live outside the workspace: the workspace exists to be
destroyed (§48), and evidence that dies with it is not evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fix import Patch, PatchArtifact, PatchVerificationRun

logger = logging.getLogger(__name__)


def artifact_root() -> Path:
    """Root directory for patch artifacts.

    Under the same base the sandboxes use, so a single mount/compose volume
    covers both, but in its own subtree — a destroyed sandbox never removes
    artifacts.
    """
    from app.services.reproduction_sandbox import sandbox_root_base

    root = sandbox_root_base() / "patch-artifacts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write(root: Path, relative: str, payload: str) -> tuple[Path, int, str]:
    """Write ``payload`` and return ``(path, size, sha256)``."""
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    data = payload.encode("utf-8")
    target.write_bytes(data)
    return target, len(data), hashlib.sha256(data).hexdigest()


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _terminal(run: PatchVerificationRun) -> bool:
    from app.models.fix import VerificationStatus

    return run.status in {
        VerificationStatus.VERIFIED,
        VerificationStatus.NOT_VERIFIED,
        VerificationStatus.FAILED,
        VerificationStatus.CANCELLED,
    }


async def store_verification_artifacts(
    db: AsyncSession,
    *,
    patch: Patch,
    run: PatchVerificationRun,
    outcome: Any,
    regression_source: Optional[str] = None,
) -> list[PatchArtifact]:
    """Persist one verification run's artifacts (§45).

    Returns the rows created. Every path is under ``patch_id/run_id/``, so two
    runs of the same patch can never overwrite each other's evidence.
    """
    root = artifact_root() / str(patch.id) / str(run.id)
    immutable = _terminal(run)
    rows: list[PatchArtifact] = []

    def store(name: str, artifact_type: str, payload: str) -> None:
        path, size, digest = _write(root, name, payload)
        rows.append(
            PatchArtifact(
                project_id=patch.project_id,
                patch_id=patch.id,
                verification_run_id=run.id,
                artifact_type=artifact_type,
                name=name,
                storage_path=str(path),
                size_bytes=size,
                content_hash=digest,
                immutable=immutable,
                artifact_metadata={
                    "verification_status": run.status.value,
                    "verification_level": run.level.value,
                    "terminal": immutable,
                },
            )
        )

    try:
        store("patch.diff", "PATCH_DIFF", patch.patch_content or "")
        store(
            "test_results.json",
            "TEST_RESULTS",
            _json(
                [
                    {
                        "kind": entry.get("kind"),
                        "command_key": entry.get("command_key"),
                        "exit_code": entry.get("exit_code"),
                        "timed_out": entry.get("timed_out"),
                        "duration_ms": entry.get("duration_ms"),
                        "selected_tests": entry.get("selected", []),
                        "selection_reason": entry.get("selection_reason"),
                    }
                    for entry in getattr(outcome, "test_runs", []) or []
                ]
            ),
        )
        store(
            "build_logs.json",
            "BUILD_LOGS",
            _json(
                [
                    {
                        "kind": entry.get("kind"),
                        "command_key": entry.get("command_key"),
                        "exit_code": entry.get("exit_code"),
                        "output_tail": entry.get("output_tail"),
                    }
                    for entry in getattr(outcome, "test_runs", []) or []
                    if entry.get("kind") in {"STATIC", "BUILD"}
                ]
            ),
        )
        if regression_source:
            store(
                "regression_test.py",
                "REGRESSION_TEST",
                regression_source,
            )
        store(
            "comparison_report.json",
            "COMPARISON_REPORT",
            _json(getattr(outcome, "comparison", {}) or {}),
        )
        store(
            "verification_report.json",
            "VERIFICATION_REPORT",
            _json(
                {
                    "status": getattr(outcome, "status", None),
                    "level": getattr(outcome, "level", None),
                    "confidence": getattr(outcome, "confidence", None),
                    "confidence_reason": getattr(outcome, "confidence_reason", None),
                    "verdict_reason": getattr(outcome, "verdict_reason", None),
                    "tampering_flag": getattr(outcome, "tampering_flag", None),
                    "verification_env_intact": getattr(
                        outcome, "verification_env_intact", None
                    ),
                    "baseline_failure_reproduced": getattr(
                        outcome, "baseline_failure_reproduced", None
                    ),
                    "patched_failure_reproduced": getattr(
                        outcome, "patched_failure_reproduced", None
                    ),
                    "regression_detected": getattr(
                        outcome, "regression_detected", None
                    ),
                    "regression": getattr(outcome, "regression", {}) or {},
                    "evidence": getattr(outcome, "evidence", {}) or {},
                }
            ),
        )
    except OSError as error:  # pragma: no cover - disk failure is environmental
        #: A verification verdict must never fail because an artifact could not
        #: be written; the failure is logged and the rows already created are
        #: returned, so the run's own record is unaffected.
        logger.warning("artifact storage failed for patch %s: %s", patch.id, error)

    for row in rows:
        db.add(row)
    if rows:
        await db.flush()
    return rows


async def patch_artifacts(db: AsyncSession, patch_id: uuid.UUID) -> list[PatchArtifact]:
    """Every artifact stored for one patch, oldest first."""
    from sqlalchemy import select

    result = await db.execute(
        select(PatchArtifact)
        .where(PatchArtifact.patch_id == patch_id)
        .order_by(PatchArtifact.created_at.asc())
    )
    return list(result.scalars().all())


__all__ = [
    "artifact_root",
    "patch_artifacts",
    "store_verification_artifacts",
]
