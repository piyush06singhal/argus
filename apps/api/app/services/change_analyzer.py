"""ARGUS Change Analyzer (Phase 4 §17–§20).

Compares **stored change events** (deployments, configuration changes) against
an incident's onset and observed degradation. This is where the §12 rule is
enforced as code:

* a change *before* onset (within the proximity window) is temporally
  relevant — supporting evidence for a change candidate, never proof;
* a change *after* onset yields a ``TEMPORAL_CONTRADICTION``: it cannot
  explain the initial degradation, though it may matter for later behaviour;
* a change that never touched the affected components or their providers is
  ``UNRELATED`` unless its timing is close — "deployment happened near the
  incident" alone is not relevance.

Nothing here reads a clock or fabricates a change: both event types come from
the Phase 0/1 tables, scoped exactly like Phase 3 (``environment_id=None``
means environment-less rows).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc_or_now
from app.models.deployment import DeploymentEvent
from app.models.ingestion import ConfigurationChangeEvent


class ChangeRelevance(str, Enum):
    """Verdict of the timing+scope comparison for one change (§12, §18)."""

    TEMPORALLY_RELEVANT = "TEMPORALLY_RELEVANT"  # before onset, in window
    TEMPORAL_CONTRADICTION = "TEMPORAL_CONTRADICTION"  # after onset
    UNRELATED = "UNRELATED"  # outside window / wrong scope


@dataclass(frozen=True)
class ChangeEventView:
    """Normalized change event (deployment or configuration change)."""

    kind: str  # "DEPLOYMENT" | "CONFIGURATION_CHANGE"
    event_id: uuid.UUID
    occurred_at: datetime
    component_id: Optional[uuid.UUID]
    label: str
    description: Optional[str]
    version: Optional[str] = None
    #: For deployments: SUCCESS/FAILED/ROLLBACK; config changes have none.
    status: Optional[str] = None
    source: Optional[str] = None

    @property
    def at(self) -> datetime:
        return ensure_utc_or_now(self.occurred_at)


@dataclass
class ChangeAssessment:
    """One change's relevance to the incident (§18 fields, computed)."""

    event: ChangeEventView
    relevance: ChangeRelevance
    reason: str
    #: Seconds before onset (negative = after onset) when in scope.
    seconds_before_onset: Optional[int] = None
    #: Does the change touch an affected component or one of its providers?
    touches_affected_components: bool = False

    @property
    def is_temporal_contradiction(self) -> bool:
        return self.relevance is ChangeRelevance.TEMPORAL_CONTRADICTION


@dataclass
class ChangeAnalysisResult:
    assessments: list[ChangeAssessment] = field(default_factory=list)
    #: Changes with no usable timestamp (reported, not invented).
    skipped_missing_timestamp: int = 0

    @property
    def temporally_relevant(self) -> list[ChangeAssessment]:
        return [
            a
            for a in self.assessments
            if a.relevance is ChangeRelevance.TEMPORALLY_RELEVANT
        ]

    @property
    def contradictions(self) -> list[ChangeAssessment]:
        return [a for a in self.assessments if a.is_temporal_contradiction]


class ChangeAnalyzer:
    """Compares stored changes with incident onset (§17)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        proximity_seconds: int = 900,
        max_changes: int = 50,
    ) -> None:
        self._session = session
        self._proximity = max(60, int(proximity_seconds))
        self._max_changes = max_changes

    async def load_changes(
        self,
        project_id: uuid.UUID,
        *,
        environment_id: uuid.UUID | None,
        start: datetime,
        end: datetime,
    ) -> list[ChangeEventView]:
        """All stored changes in ``[start, end]`` (deployments + config)."""
        lo, hi = ensure_utc_or_now(start), ensure_utc_or_now(end)
        changes: list[ChangeEventView] = []

        dep_stmt = select(DeploymentEvent).where(
            DeploymentEvent.project_id == project_id,
            DeploymentEvent.deployed_at >= lo,
            DeploymentEvent.deployed_at <= hi,
        )
        dep_stmt = dep_stmt.where(
            DeploymentEvent.environment_id == environment_id
            if environment_id is not None
            else DeploymentEvent.environment_id.is_(None)
        )
        dep_stmt = dep_stmt.order_by(DeploymentEvent.deployed_at).limit(
            self._max_changes
        )
        for row in (await self._session.execute(dep_stmt)).scalars().all():
            changes.append(
                ChangeEventView(
                    kind="DEPLOYMENT",
                    event_id=row.id,
                    occurred_at=row.deployed_at,
                    component_id=row.component_id,
                    label=f"Deployment {row.deployment_id}"
                    + (f" (version {row.version})" if row.version else ""),
                    description=row.description,
                    version=row.version,
                    status=getattr(getattr(row, "status", None), "value", None),
                    source=None,
                )
            )

        cfg_stmt = select(ConfigurationChangeEvent).where(
            ConfigurationChangeEvent.project_id == project_id,
            ConfigurationChangeEvent.timestamp >= lo,
            ConfigurationChangeEvent.timestamp <= hi,
        )
        cfg_stmt = cfg_stmt.where(
            ConfigurationChangeEvent.environment_id == environment_id
            if environment_id is not None
            else ConfigurationChangeEvent.environment_id.is_(None)
        )
        cfg_stmt = cfg_stmt.order_by(ConfigurationChangeEvent.timestamp).limit(
            self._max_changes
        )
        for change in (await self._session.execute(cfg_stmt)).scalars().all():
            changes.append(
                ChangeEventView(
                    kind="CONFIGURATION_CHANGE",
                    event_id=change.id,
                    occurred_at=change.timestamp,
                    component_id=change.component_id,
                    label=f"Configuration change {change.change_id}",
                    description=change.description,
                    source=change.source,
                )
            )
        changes.sort(key=lambda c: c.at)
        return changes

    def assess(
        self,
        changes: Sequence[ChangeEventView],
        *,
        onset: datetime,
        affected_component_ids: set[uuid.UUID],
        provider_component_ids: set[uuid.UUID] | None = None,
    ) -> ChangeAnalysisResult:
        """Compare each change against the incident onset (§18, §19).

        ``affected_component_ids`` — components with observed anomalies.
        ``provider_component_ids`` — components those depend on (a change to a
        provider is relevant even if the provider itself shows no anomaly).
        """
        result = ChangeAnalysisResult()
        onset_utc = ensure_utc_or_now(onset)
        providers = provider_component_ids or set()
        window_start = onset_utc - timedelta(seconds=self._proximity)

        for event in changes:
            if event.occurred_at is None:
                result.skipped_missing_timestamp += 1
                continue
            touches = event.component_id is not None and (
                event.component_id in affected_component_ids
                or event.component_id in providers
            )
            gap = int((event.at - onset_utc).total_seconds())

            if event.at > onset_utc:
                relevance = ChangeRelevance.TEMPORAL_CONTRADICTION
                reason = (
                    f"{event.label} occurred {abs(gap)}s AFTER incident onset; "
                    "it cannot explain the initial degradation"
                )
            elif event.at < window_start:
                relevance = ChangeRelevance.UNRELATED
                reason = (
                    f"{event.label} occurred {abs(gap)}s before onset, "
                    f"outside the {self._proximity}s proximity window"
                )
            elif not touches:
                relevance = ChangeRelevance.UNRELATED
                reason = (
                    f"{event.label} is temporally close to onset but did not "
                    "touch an affected component or its dependencies"
                )
            else:
                relevance = ChangeRelevance.TEMPORALLY_RELEVANT
                reason = (
                    f"{event.label} occurred {abs(gap)}s before onset and "
                    "touches an affected component or its dependencies"
                )
            result.assessments.append(
                ChangeAssessment(
                    event=event,
                    relevance=relevance,
                    reason=reason,
                    seconds_before_onset=-gap if gap <= 0 else gap,
                    touches_affected_components=touches,
                )
            )
        result.assessments.sort(key=lambda a: a.event.at)
        return result

    async def analyze(
        self,
        project_id: uuid.UUID,
        *,
        environment_id: uuid.UUID | None,
        onset: datetime,
        affected_component_ids: set[uuid.UUID],
        provider_component_ids: set[uuid.UUID] | None = None,
        lookback_seconds: int | None = None,
    ) -> ChangeAnalysisResult:
        """Load + assess in one call (window = proximity unless told wider)."""
        onset_utc = ensure_utc_or_now(onset)
        lookback = max(self._proximity, lookback_seconds or self._proximity)
        changes = await self.load_changes(
            project_id,
            environment_id=environment_id,
            start=onset_utc - timedelta(seconds=lookback),
            end=onset_utc + timedelta(seconds=self._proximity),
        )
        return self.assess(
            changes,
            onset=onset_utc,
            affected_component_ids=affected_component_ids,
            provider_component_ids=provider_component_ids,
        )


__all__ = [
    "ChangeAnalysisResult",
    "ChangeAnalyzer",
    "ChangeAssessment",
    "ChangeEventView",
    "ChangeRelevance",
]
