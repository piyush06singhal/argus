"""ARGUS Deployment Models."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import String, Text, Enum, DateTime, ForeignKey
from app.models.base import Guid as UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, JSONType

#: Phase 6 indexing state. Defined with the code-intelligence models and imported
#: here because it describes *this* table's row; keeping one enum means the
#: repository list and the indexer can never disagree about what "indexed" means.
from app.models.code import RepositoryIndexStatus

if TYPE_CHECKING:
    from app.models.project import SoftwareProject


class DeploymentStatus(str, enum.Enum):
    """Deployment status."""

    STARTED = "STARTED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    UNKNOWN = "UNKNOWN"


class DeploymentEvent(BaseModel):
    """A deployment event."""

    __tablename__ = "deployment_events"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    environment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    component_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_components.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    deployment_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    version: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    commit_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    status: Mapped[DeploymentStatus] = mapped_column(
        Enum(DeploymentStatus), default=DeploymentStatus.UNKNOWN, nullable=False
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    # Relationships
    project: Mapped["SoftwareProject"] = relationship(
        "SoftwareProject", back_populates="deployment_events"
    )


class CodeRepository(BaseModel):
    """A code repository abstraction.

    Phase 6 extends this rather than introducing a second repository concept:
    ``provider``/``repository_url``/``default_branch`` already described *where*
    the code is, and the indexing fields below describe *what ARGUS knows about
    it*. Credentials are never stored here — ``credentials_ref`` names the
    environment variable that holds them, so a database dump can never leak a
    token (§6, §56).
    """

    __tablename__ = "code_repositories"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    repository_url: Mapped[str] = mapped_column(String(512), nullable=False)
    default_branch: Mapped[str] = mapped_column(
        String(100), default="main", nullable=False
    )
    connection_status: Mapped[str] = mapped_column(
        String(50), default="PENDING", nullable=False
    )
    configuration: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONType, nullable=True
    )

    # ---- Phase 6: code intelligence metadata (§6) -------------------------
    #: Name of the environment variable / secret reference holding credentials.
    #: Never the credential itself.
    credentials_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    #: Server-side working copy for the LOCAL provider (empty for remote ones).
    local_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    language: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    framework: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    index_status: Mapped[RepositoryIndexStatus] = mapped_column(
        Enum(RepositoryIndexStatus, name="repositoryindexstatus"),
        default=RepositoryIndexStatus.PENDING,
        nullable=False,
        index=True,
    )
    last_indexed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Set when the index is known to be behind the branch head, so "indexed at
    #: commit A" and "the branch has moved on" are both visible (§54).
    last_indexed_commit: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Relationships
    project: Mapped["SoftwareProject"] = relationship(
        "SoftwareProject", back_populates="code_repositories"
    )
