"""ARGUS Reproduction Artifact Store (Phase 5 §41, §42).

Stores the auditable output of an experiment: the plan, the manifest, the
environment snapshots, the captured telemetry, the comparisons, and the verdict.

Two properties, both of which are the reason to have a store at all:

* **Content-addressed.** Every artifact records a SHA-256 of its bytes. A stored
  artifact can therefore be verified against what it claims to be, which is what
  makes an old experiment's evidence still mean something.
* **Immutable after the experiment (§42).** Writing a *different* artifact under
  an existing name is refused, not silently overwritten. History is the one thing
  a reliability tool must not rewrite, and an overwrite is indistinguishable from
  a bug at read time.

Artifacts deliberately live **outside** the sandbox working tree: the sandbox is
disposable and is destroyed as soon as the run ends, while the artifacts are the
record that outlives it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from app.models.reproduction import ArtifactType
from app.services.reproduction_sandbox import artifact_root_base

logger = logging.getLogger(__name__)


class ArtifactError(RuntimeError):
    """Raised when an artifact cannot be stored or verified."""


@dataclass
class ArtifactRecord:
    """A stored artifact's identity and provenance."""

    artifact_type: ArtifactType
    name: str
    storage_location: str
    size_bytes: int
    content_hash: str
    content_type: str = "application/json"
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type.value,
            "name": self.name,
            "storage_location": self.storage_location,
            "size_bytes": self.size_bytes,
            "content_hash": self.content_hash,
            "content_type": self.content_type,
            "metadata": self.metadata,
        }


class ReproductionArtifactStore:
    """Writes and verifies experiment artifacts."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = Path(root) if root is not None else artifact_root_base()

    @property
    def root(self) -> Path:
        return self._root

    def experiment_dir(self, experiment_id: Any) -> Path:
        """Artifacts for one experiment are grouped under its own directory."""
        directory = self._root / str(experiment_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def store(
        self,
        *,
        experiment_id: Any,
        name: str,
        payload: Union[dict[str, Any], list[Any], str, bytes],
        artifact_type: ArtifactType,
        run_id: Optional[Any] = None,
        content_type: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ArtifactRecord:
        """Write an artifact once, returning its hash and location.

        ``name`` is relative to the experiment directory; directories in it are
        created as needed. A name that is an absolute path, or that escapes the
        experiment directory, is refused — the store only ever writes inside its
        own root.
        """
        safe_name = self._safe_name(name)
        directory = self.experiment_dir(experiment_id)
        path = directory / safe_name
        path.parent.mkdir(parents=True, exist_ok=True)

        data, resolved_type = self._serialize(payload, content_type)
        digest = hashlib.sha256(data).hexdigest()

        if path.exists():
            existing = hashlib.sha256(path.read_bytes()).hexdigest()
            if existing != digest:
                raise ArtifactError(
                    f"Artifact {safe_name!r} already exists for this experiment with "
                    "different content. Artifacts are immutable after an "
                    "experiment completes (§42)."
                )
            # Identical content: idempotent re-write, no error.
            logger.debug("Artifact %s already stored identically", safe_name)

        path.write_bytes(data)
        return ArtifactRecord(
            artifact_type=artifact_type,
            name=safe_name,
            storage_location=str(path.relative_to(self._root)),
            size_bytes=len(data),
            content_hash=digest,
            content_type=resolved_type,
            metadata=dict(metadata or {}),
        )

    def read(self, record: ArtifactRecord) -> bytes:
        """Read an artifact's bytes, refusing to escape the store root."""
        path = (self._root / record.storage_location).resolve()
        root = self._root.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ArtifactError(
                f"Artifact location escapes the store root: {record.storage_location}"
            ) from exc
        if not path.exists():
            raise ArtifactError(f"Artifact not found: {record.storage_location}")
        return path.read_bytes()

    def verify(self, record: ArtifactRecord) -> bool:
        """True when the stored bytes still match the recorded hash."""
        try:
            data = self.read(record)
        except ArtifactError:
            return False
        return hashlib.sha256(data).hexdigest() == record.content_hash

    # -- internals -------------------------------------------------------
    @staticmethod
    def _safe_name(name: str) -> str:
        trimmed = (name or "").strip().lstrip("/")
        if not trimmed:
            raise ArtifactError("Artifact name must not be empty")
        if ".." in Path(trimmed).parts:
            raise ArtifactError(f"Artifact name must not traverse directories: {name}")
        return trimmed

    @staticmethod
    def _serialize(
        payload: Union[dict[str, Any], list[Any], str, bytes],
        content_type: Optional[str],
    ) -> tuple[bytes, str]:
        if isinstance(payload, bytes):
            return payload, content_type or "application/octet-stream"
        if isinstance(payload, str):
            return payload.encode("utf-8"), content_type or "text/plain; charset=utf-8"
        return (
            json.dumps(payload, indent=1, sort_keys=True, default=str).encode("utf-8"),
            content_type or "application/json",
        )


__all__ = [
    "ArtifactError",
    "ArtifactRecord",
    "ReproductionArtifactStore",
]
