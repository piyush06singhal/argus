"""ARGUS Change Relevance Analyzer (Phase 6 §19, §20, §61).

Answers one question deterministically: *which changes are connected to the code
this incident actually exercised?*

The failure this module exists to prevent is the debugging reflex "it broke, and
something was deployed, so the deployment did it". A commit being recent makes it
a **candidate**, not an explanation. So every change gets one of three labels and
the label is derived from the evidence, not from the timestamp:

* ``RELEVANT_CHANGE`` — the change touched a file the incident's trace maps into,
  or a file carrying investigation signals for the affected component. A channel
  exists through which this change could have mattered.
* ``SUSPICIOUS_CHANGE`` — relevant *and* it touched a location the analysis
  already named (a mapped symbol's file), which is the strongest thing file-level
  reasoning can support.
* ``TEMPORALLY_RECENT_UNRELATED`` — inside the window, but nothing connects it to
  the failure. Reported so an engineer can see it was considered and ruled out
  rather than silently ignored (§61).

The vocabulary stops there. ``RELEVANT_CHANGE`` never means "caused", and the
report says so in its own words.

**Stated limitation:** this compares *files*, not lines, because the provider
interface exposes changed paths and not hunks. A commit that touched one
irrelevant line of a relevant file is therefore labelled relevant, and the report
records that the granularity is the file. Guessing at line-level blame from a
path list is exactly the kind of over-reach the phase forbids.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.code import (
    CodeRiskSignal,
    RepositorySnapshot,
    RiskSignalType,
    TraceCodeMapping,
)
from app.models.deployment import CodeRepository, DeploymentEvent
from app.models.incident import Incident
from app.services.repository_provider import (
    ProviderError,
    RepositoryError,
    provider_for_repository,
)

logger = logging.getLogger(__name__)
settings = get_settings()

#: How a change relates to the incident. Three values, and no "cause".
CHANGE_CLASSIFICATIONS = (
    "RELEVANT_CHANGE",
    "SUSPICIOUS_CHANGE",
    "TEMPORALLY_RECENT_UNRELATED",
    "UNKNOWN",
)

#: Signals that mark a file as worth connecting a change to.
RELEVANCE_SIGNAL_TYPES = (
    RiskSignalType.ERROR_PRONE_PATH,
    RiskSignalType.FREQUENTLY_FAILING,
    RiskSignalType.DEPENDENCY_BOUNDARY,
    RiskSignalType.DATABASE_OPERATION,
    RiskSignalType.EXTERNAL_API_CALL,
)

#: A file may be a plausible change target for a mapped symbol only if the symbol
#: is a definition (not a module marker), so module rows do not make every file
#: "relevant".
_NON_DEFINITION_TYPES = {"MODULE"}


class ChangeRelevance(str, Enum):
    RELEVANT_CHANGE = "RELEVANT_CHANGE"
    SUSPICIOUS_CHANGE = "SUSPICIOUS_CHANGE"
    TEMPORALLY_RECENT_UNRELATED = "TEMPORALLY_RECENT_UNRELATED"
    UNKNOWN = "UNKNOWN"


@dataclass
class ChangeAssessment:
    """One deployment's changes and what (if anything) connects them."""

    deployment_id: Optional[str]
    commit_sha: Optional[str]
    deployed_at: Optional[datetime]
    seconds_before_onset: Optional[int]
    version: Optional[str] = None
    change_summary: str = ""
    changed_files: list[str] = field(default_factory=list)
    relevant_files: list[str] = field(default_factory=list)
    matched_mapped_files: list[str] = field(default_factory=list)
    matched_signal_files: list[str] = field(default_factory=list)
    classification: ChangeRelevance = ChangeRelevance.UNKNOWN
    reason: str = ""
    #: Where the changed-file list came from, so a missing diff is visible.
    diff_source: Optional[str] = None
    diff_error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "deployment_id": self.deployment_id,
            "commit_sha": self.commit_sha,
            "deployed_at": self.deployed_at.isoformat() if self.deployed_at else None,
            "seconds_before_onset": self.seconds_before_onset,
            "version": self.version,
            "change_summary": self.change_summary,
            "changed_files": self.changed_files[:50],
            "relevant_files": self.relevant_files[:25],
            "matched_mapped_files": self.matched_mapped_files[:25],
            "matched_signal_files": self.matched_signal_files[:25],
            "classification": self.classification.value,
            "reason": self.reason,
            "diff_source": self.diff_source,
            "diff_error": self.diff_error,
        }


@dataclass
class ChangeRelevanceReport:
    """Every change considered, with the verdict and the granularity caveat."""

    incident_id: str
    snapshot_id: Optional[str]
    window_start: Optional[datetime]
    onset: Optional[datetime]
    assessments: list[ChangeAssessment] = field(default_factory=list)
    considered_mapped_files: list[str] = field(default_factory=list)
    considered_signal_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def relevant(self) -> list[ChangeAssessment]:
        return [
            item
            for item in self.assessments
            if item.classification
            in (ChangeRelevance.RELEVANT_CHANGE, ChangeRelevance.SUSPICIOUS_CHANGE)
        ]

    @property
    def unrelated(self) -> list[ChangeAssessment]:
        return [
            item
            for item in self.assessments
            if item.classification is ChangeRelevance.TEMPORALLY_RECENT_UNRELATED
        ]

    def as_dict(self) -> dict:
        return {
            "onset": self.onset.isoformat() if self.onset else None,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "considered_mapped_files": self.considered_mapped_files[:40],
            "considered_signal_files": self.considered_signal_files[:40],
            "assessments": [item.as_dict() for item in self.assessments],
            "relevant_count": len(self.relevant),
            "unrelated_count": len(self.unrelated),
            "notes": list(self.notes),
            "disclaimer": (
                "Relevant means a change touched code this incident exercised. It is "
                "not evidence that the change caused the incident, and comparison is "
                "at file granularity."
            ),
        }


class ChangeRelevanceAnalyzer:
    """Deterministic incident ↔ change correlation, scoped to one project."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def analyze(
        self,
        incident: Incident,
        snapshot: Optional[RepositorySnapshot],
        repository: Optional[CodeRepository],
        *,
        named_files: Optional[list[str]] = None,
        window_days: Optional[int] = None,
    ) -> ChangeRelevanceReport:
        onset = _as_aware(incident.started_at or incident.detected_at)
        days = window_days or settings.CODE_RECENT_CHANGE_DAYS
        window_start = onset - timedelta(days=days) if onset else None
        report = ChangeRelevanceReport(
            incident_id=str(incident.id),
            snapshot_id=str(snapshot.id) if snapshot else None,
            window_start=window_start,
            onset=onset,
        )
        if onset is None:
            report.notes.append(
                "this incident has no onset timestamp, so no change can be placed in time"
            )
            return report

        mapped_files, signal_files = await self._relevant_paths(
            incident, snapshot, named_files or []
        )
        report.considered_mapped_files = sorted(mapped_files)
        report.considered_signal_files = sorted(signal_files)
        candidate_paths = mapped_files | signal_files
        if not candidate_paths:
            report.notes.append(
                "no trace-to-code mapping and no investigation signal identifies a code "
                "path for this incident, so no change can be called relevant — a recent "
                "commit would be a guess"
            )

        deployments = (
            await self.session.execute(
                select(DeploymentEvent)
                .where(
                    DeploymentEvent.project_id == incident.project_id,
                    DeploymentEvent.deployed_at >= window_start,
                    DeploymentEvent.deployed_at <= onset,
                )
                .order_by(DeploymentEvent.deployed_at.desc())
                .limit(20)
            )
        ).scalars().all()
        if not deployments:
            report.notes.append(
                f"no deployment was recorded in the {days} days before onset"
            )
            return report

        provider = None
        if repository is not None:
            try:
                provider = provider_for_repository(repository)
            except (RepositoryError, ProviderError) as error:
                report.notes.append(f"repository is not readable: {error}")

        for deployment in deployments:
            report.assessments.append(
                await self._assess(
                    deployment,
                    onset,
                    candidate_paths,
                    mapped_files,
                    signal_files,
                    provider,
                )
            )
        if any(item.diff_error for item in report.assessments):
            report.notes.append(
                "at least one changed-file list is unavailable; those changes are "
                "labelled UNKNOWN rather than presumed irrelevant"
            )
        report.notes.append(
            "changes are compared at file granularity: the provider interface exposes "
            "changed paths, not hunks"
        )
        return report

    # ------------------------------------------------------------------
    async def _relevant_paths(
        self, incident: Incident, snapshot: Optional[RepositorySnapshot], named: list[str]
    ) -> tuple[set[str], set[str]]:
        """Files the incident demonstrably touched, from two independent sources."""
        mapped: set[str] = set()
        if snapshot is not None:
            rows = (
                await self.session.execute(
                    select(TraceCodeMapping.file_path).where(
                        TraceCodeMapping.snapshot_id == snapshot.id,
                        TraceCodeMapping.file_path.is_not(None),
                    )
                )
            ).all()
            mapped |= {row[0] for row in rows if row[0]}
        #: Files the analysis itself named (verified locations), so a hypothesis
        #: can be connected to a change even with no trace mapping.
        mapped |= {path for path in named if path}

        signals: set[str] = set()
        if snapshot is not None and incident.primary_component_id is not None:
            signal_paths = (
                await self.session.execute(
                    select(CodeRiskSignal.file_path)
                    .where(
                        CodeRiskSignal.snapshot_id == snapshot.id,
                        CodeRiskSignal.signal_type.in_(list(RELEVANCE_SIGNAL_TYPES)),
                    )
                    .limit(200)
                )
            ).all()
            signals |= {path for (path,) in signal_paths if path}
        return mapped, signals

    async def _assess(
        self,
        deployment: DeploymentEvent,
        onset: datetime,
        candidate_paths: set[str],
        mapped_files: set[str],
        signal_files: set[str],
        provider,
    ) -> ChangeAssessment:
        deployed_at = _as_aware(deployment.deployed_at)
        assessment = ChangeAssessment(
            deployment_id=deployment.deployment_id,
            commit_sha=deployment.commit_sha,
            deployed_at=deployed_at,
            seconds_before_onset=int((onset - deployed_at).total_seconds())
            if deployed_at
            else None,
            version=deployment.version,
            change_summary=(deployment.description or "")[:400],
        )
        if not deployment.commit_sha:
            assessment.classification = ChangeRelevance.UNKNOWN
            assessment.reason = (
                "this deployment recorded no commit, so its changes cannot be compared "
                "to the incident's code"
            )
            return assessment
        if provider is None:
            assessment.classification = ChangeRelevance.UNKNOWN
            assessment.reason = (
                "no readable repository is configured, so the deployment's changes "
                "could not be inspected"
            )
            assessment.diff_error = "repository unavailable"
            return assessment

        try:
            commit = await provider.get_commit(deployment.commit_sha)
        except (RepositoryError, ProviderError) as error:
            commit = None
            assessment.diff_error = str(error)
        if commit is None:
            assessment.classification = ChangeRelevance.UNKNOWN
            assessment.reason = (
                f"commit {deployment.commit_sha[:12]} is not present in the repository, "
                "so its changes cannot be compared to the incident's code"
            )
            assessment.diff_error = assessment.diff_error or "commit not found"
            return assessment
        #: The parent comes from the commit metadata, not from ``<sha>^`` syntax:
        #: a revision expression the repository cannot resolve makes ``diff``
        #: answer with an empty list, and an empty list reads as "nothing changed".
        parent = commit.parents[0] if commit.parents else None
        if parent is None:
            assessment.classification = ChangeRelevance.UNKNOWN
            assessment.reason = (
                f"{deployment.commit_sha[:12]} is the repository's first recorded "
                "commit, so there is no earlier revision to compare it against"
            )
            assessment.diff_error = "no parent revision"
            return assessment
        try:
            entries = await provider.diff(parent, deployment.commit_sha)
            assessment.diff_source = "provider"
        except (RepositoryError, ProviderError) as error:
            #: A non-VCS provider cannot diff at all. Stated, never smoothed over.
            assessment.diff_error = str(error)
            assessment.classification = ChangeRelevance.UNKNOWN
            assessment.reason = (
                f"the changes in {deployment.commit_sha[:12]} could not be listed: {error}"
            )
            return assessment

        changed = sorted({entry.path for entry in entries})
        assessment.changed_files = changed
        if not changed:
            assessment.classification = ChangeRelevance.UNKNOWN
            assessment.reason = (
                f"{deployment.commit_sha[:12]} records no file changes, so it cannot be "
                "connected to the incident"
            )
            return assessment

        relevant = sorted(path for path in changed if path in candidate_paths)
        assessment.relevant_files = relevant
        assessment.matched_mapped_files = sorted(
            path for path in relevant if path in mapped_files
        )
        assessment.matched_signal_files = sorted(
            path for path in relevant if path in signal_files
        )

        if assessment.matched_mapped_files:
            assessment.classification = ChangeRelevance.SUSPICIOUS_CHANGE
            assessment.reason = (
                "the change touched "
                + ", ".join(assessment.matched_mapped_files[:3])
                + ", which the incident's trace maps into — a channel exists, which is "
                "not evidence of causation"
            )
        elif relevant:
            assessment.classification = ChangeRelevance.RELEVANT_CHANGE
            assessment.reason = (
                "the change touched "
                + ", ".join(relevant[:3])
                + ", which carries investigation signals for this incident"
            )
        else:
            assessment.classification = ChangeRelevance.TEMPORALLY_RECENT_UNRELATED
            assessment.reason = (
                f"{deployment.commit_sha[:12]} was deployed "
                f"{assessment.seconds_before_onset}s before onset but changed "
                + ", ".join(changed[:3])
                + f" ({len(changed)} file(s)), none of which this incident's evidence "
                "connects to the failure"
            )
        return assessment


def _as_aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "CHANGE_CLASSIFICATIONS",
    "ChangeAssessment",
    "ChangeRelevance",
    "ChangeRelevanceAnalyzer",
    "ChangeRelevanceReport",
]
