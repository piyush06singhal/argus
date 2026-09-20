"""ARGUS Code Version Resolution & Snapshots (Phase 6 §7, §8, §55).

The question this module answers is the one that decides whether a Phase 6
analysis is trustworthy: **which source code was running when this incident
happened?**

The rule from §8 is implemented literally:

* a *resolved* revision is one ARGUS can name — a deployment event recorded a
  commit sha, and that commit exists in the repository;
* an *inferred* revision is one ARGUS can derive — the deployment recorded a
  version string that resolves to a tag/ref in the repository;
* anything else is ``UNKNOWN``, and the snapshot says so in
  ``version_evidence``.

There is deliberately no fourth option that quietly means *"the current
branch"*. When resolution fails ARGUS still indexes the best available revision
(so the investigation can proceed) but marks it ``UNKNOWN`` and states why, which
propagates into the debugging confidence. Analysing today's code as if it were
last week's is the single most damaging silent error this phase could make, so it
is structurally impossible to do it without saying so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.code import (
    CodeVersionStatus,
    RepositorySnapshot,
    SnapshotStatus,
)
from app.models.deployment import CodeRepository, DeploymentEvent
from app.models.incident import Incident
from app.models.system import SystemComponent
from app.services.repository_provider import (
    ProviderError,
    RepositoryError,
    RepositoryInfo,
    RepositoryProvider,
    provider_for_repository,
)

logger = logging.getLogger(__name__)
settings = get_settings()

#: How far before an incident's onset a deployment may be and still be the
#: candidate that introduced the running code. A deployment outside this window
#: is *older code*, not "the version under test": picking it would silently
#: analyse a revision that was replaced long before the incident.
DEPLOYMENT_LOOKBACK_HOURS = 24 * 14


@dataclass(frozen=True)
class RevisionResolution:
    """The outcome of trying to pin down the code version for an incident."""

    #: Revision to index. Present even when ``status`` is ``UNKNOWN`` — that is
    #: the best available revision, not a claim that it is the right one.
    reference: Optional[str]
    status: CodeVersionStatus
    #: Human-readable account of how the reference was chosen (§8).
    evidence: str
    deployment_id: Optional[str] = None
    deployment_version: Optional[str] = None
    deployed_at: Optional[datetime] = None
    branch: Optional[str] = None


class CodeVersionResolver:
    """Resolves the deployed revision for an incident from stored deployments."""

    def __init__(self, provider: RepositoryProvider) -> None:
        self._provider = provider

    async def resolve_for_incident(
        self, session: AsyncSession, incident: Incident
    ) -> RevisionResolution:
        """Best-effort revision resolution for ``incident`` (§8).

        Preference order, most to least specific:

        1. a deployment of an *affected component* at or before onset that
           recorded a commit sha;
        2. any deployment in the incident's environment in the same window that
           recorded a commit sha;
        3. a deployment whose recorded *version* resolves to a ref (``INFERRED``);
        4. the repository's current revision (``UNKNOWN``, with the reason).
        """
        component_ids = await _incident_component_ids(session, incident)
        deployments = await _candidate_deployments(session, incident, component_ids)

        branch = await _repository_branch(self._provider)

        for event in deployments:
            sha = (event.commit_sha or "").strip()
            if not sha:
                continue
            resolved = await self._safe_resolve(sha)
            if resolved:
                return RevisionResolution(
                    reference=resolved,
                    status=CodeVersionStatus.RESOLVED,
                    evidence=(
                        f"deployment {event.deployment_id} of "
                        f"{event.deployed_at.isoformat()} recorded commit {resolved[:12]}"
                    ),
                    deployment_id=event.deployment_id,
                    deployment_version=event.version,
                    deployed_at=event.deployed_at,
                    branch=branch,
                )
            #: The deployment named a commit the repository does not contain —
            #: usually a shallow clone or a different fork. Say so rather than
            #: falling through silently.
            logger.info(
                "deployment %s names commit %s which is not resolvable in the repository",
                event.deployment_id,
                sha,
            )

        for event in deployments:
            version = (event.version or "").strip()
            if not version:
                continue
            resolved = await self._safe_resolve(version)
            if resolved:
                return RevisionResolution(
                    reference=resolved,
                    status=CodeVersionStatus.INFERRED,
                    evidence=(
                        f"deployment {event.deployment_id} recorded version "
                        f"{version!r}, which resolves to {resolved[:12]}"
                    ),
                    deployment_id=event.deployment_id,
                    deployment_version=version,
                    deployed_at=event.deployed_at,
                    branch=branch,
                )

        head = await self._safe_head()
        if deployments:
            reason = (
                f"{len(deployments)} deployment(s) matched the incident window "
                "but none recorded a commit sha or a resolvable version"
            )
        else:
            reason = (
                "no deployment of an affected component was recorded within "
                f"{DEPLOYMENT_LOOKBACK_HOURS // 24} days before the incident"
            )
        if head:
            return RevisionResolution(
                reference=head,
                status=CodeVersionStatus.UNKNOWN,
                evidence=(
                    f"{reason}; analysed {head[:12]} (the repository's current "
                    "revision) as the best available code — this is NOT "
                    "confirmed to be the deployed revision"
                ),
                branch=branch,
            )
        return RevisionResolution(
            reference=None,
            status=CodeVersionStatus.UNKNOWN,
            evidence=f"{reason}; the repository has no resolvable current revision",
            branch=branch,
        )

    # -- provider helpers --------------------------------------------------
    async def _safe_resolve(self, reference: str) -> Optional[str]:
        try:
            return await self._provider.resolve(reference)
        except (RepositoryError, ProviderError):
            return None

    async def _safe_head(self) -> Optional[str]:
        try:
            info = await self._provider.describe()
        except (RepositoryError, ProviderError):
            return None
        return info.head


async def _repository_branch(provider: RepositoryProvider) -> Optional[str]:
    try:
        info: RepositoryInfo = await provider.describe()
    except (RepositoryError, ProviderError):
        return None
    return info.branch


async def _incident_component_ids(session: AsyncSession, incident: Incident) -> set:
    """Every component the incident is known to touch.

    Deliberately broad — evidence rows *and* anomaly rows *and* the primary
    component — because a deployment of any of them is a plausible source of the
    running code, and a narrow match would produce a false ``UNKNOWN``.
    """
    #: Imported here rather than at module scope: ``anomaly`` imports back into
    #: the incident models, and a module-level cycle would make the import order
    #: of the whole app load-bearing.
    from app.models.anomaly import Anomaly
    from app.models.incident import IncidentEvidence

    ids: set = set()
    if incident.primary_component_id:
        ids.add(incident.primary_component_id)
    rows = await session.execute(
        select(IncidentEvidence.component_id).where(
            IncidentEvidence.incident_id == incident.id,
            IncidentEvidence.component_id.is_not(None),
        )
    )
    ids.update(row[0] for row in rows if row[0])
    anomalies = await session.execute(
        select(Anomaly.component_id).where(
            Anomaly.incident_id == incident.id, Anomaly.component_id.is_not(None)
        )
    )
    ids.update(row[0] for row in anomalies if row[0])
    return ids


async def _candidate_deployments(
    session: AsyncSession, incident: Incident, component_ids: set
) -> list[DeploymentEvent]:
    """Deployments that could plausibly be the code under test, newest first."""
    onset = incident.started_at or incident.detected_at
    if onset is None:
        return []
    if onset.tzinfo is None:
        onset = onset.replace(tzinfo=timezone.utc)
    window_start = onset - timedelta(hours=DEPLOYMENT_LOOKBACK_HOURS)

    stmt = (
        select(DeploymentEvent)
        .where(
            DeploymentEvent.project_id == incident.project_id,
            DeploymentEvent.deployed_at <= onset,
            DeploymentEvent.deployed_at >= window_start,
        )
        .order_by(DeploymentEvent.deployed_at.desc())
        .limit(50)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    if not rows:
        return []

    def rank(event: DeploymentEvent) -> tuple[int, float]:
        score = 0
        if event.component_id and event.component_id in component_ids:
            score += 2
        if incident.environment_id and event.environment_id == incident.environment_id:
            score += 1
        return (-score, -event.deployed_at.timestamp())

    return sorted(rows, key=rank)


# ---------------------------------------------------------------------------
# Snapshot service
# ---------------------------------------------------------------------------
class CodeSnapshotService:
    """Creates and reuses immutable repository snapshots (§7, §55)."""

    async def get_or_create_snapshot(
        self,
        session: AsyncSession,
        repository: CodeRepository,
        reference: Optional[str],
        *,
        version_status: CodeVersionStatus = CodeVersionStatus.UNKNOWN,
        version_evidence: str = "",
        branch: Optional[str] = None,
        provider: Optional[RepositoryProvider] = None,
    ) -> RepositorySnapshot:
        """Return the snapshot for ``(repository, commit)``, creating it if new.

        Reuse is keyed on the *resolved commit*, not on the branch: indexing a
        branch twice produces the same snapshot while the branch has not moved,
        and two different snapshots once it has. That is what makes an incident
        from last month still analyse the code it actually ran.
        """
        provider = provider or provider_for_repository(repository)
        #: ``reference=None`` still goes through resolution, because the whole
        #: point of a snapshot is to name a revision. Resolving ``None`` yields
        #: the provider's current revision (HEAD for git, a tree hash for a plain
        #: directory); leaving it ``None`` would produce an unpinned snapshot that
        #: silently means "whatever the tree says next time it is read".
        resolved = reference
        try:
            resolved = await provider.resolve(reference) or reference
        except (RepositoryError, ProviderError):
            resolved = reference

        existing = await self._find_snapshot(session, repository.id, resolved)
        if existing is not None:
            #: A snapshot is immutable in *content* but may be re-indexed (e.g.
            #: after a parser upgrade), so the existing row is returned as-is.
            #: Its one mutable claim is how confidently the revision is known: a
            #: snapshot first indexed from a bare request is UNKNOWN, and a later
            #: call that can *prove* the same revision is resolved (a caller
            #: pinned it, a deployment named it) must be allowed to upgrade it.
            #: Refusing to would permanently mark the code version unknown and
            #: lower the confidence of every analysis of that incident.
            self._improve_version_confidence(
                existing,
                version_status=version_status,
                version_evidence=version_evidence,
            )
            await session.flush()
            return existing

        info = await _safe_describe(provider)
        commit_meta = None
        if resolved:
            try:
                commit_meta = await provider.get_commit(resolved)
            except (RepositoryError, ProviderError):
                commit_meta = None

        snapshot = RepositorySnapshot(
            project_id=repository.project_id,
            repository_id=repository.id,
            commit_sha=resolved,
            branch=branch or (info.branch if info else None),
            commit_at=commit_meta.committed_at if commit_meta else None,
            commit_message=commit_meta.message if commit_meta else None,
            commit_author=commit_meta.author if commit_meta else None,
            reference=reference,
            root_path=info.root if info else None,
            provider_name=info.provider_name if info else provider.name,
            version_status=version_status,
            version_evidence=version_evidence or None,
            status=SnapshotStatus.CREATED,
            languages=[],
            index_metadata={
                "vcs_present": bool(info.vcs_present) if info else False,
                "snapshot_notes": list(info.notes) if info else [],
            },
        )
        session.add(snapshot)
        await session.flush()
        return snapshot

    async def _find_snapshot(
        self, session: AsyncSession, repository_id, commit_sha: Optional[str]
    ) -> Optional[RepositorySnapshot]:
        stmt = select(RepositorySnapshot).where(
            RepositorySnapshot.repository_id == repository_id
        )
        if commit_sha is None:
            stmt = stmt.where(RepositorySnapshot.commit_sha.is_(None))
        else:
            stmt = stmt.where(RepositorySnapshot.commit_sha == commit_sha)
        stmt = stmt.order_by(RepositorySnapshot.created_at.desc()).limit(1)
        return (await session.execute(stmt)).scalars().first()

    async def latest_ready_snapshot(
        self, session: AsyncSession, repository_id
    ) -> Optional[RepositorySnapshot]:
        """The most recently indexed ready snapshot for a repository."""
        stmt = (
            select(RepositorySnapshot)
            .where(
                RepositorySnapshot.repository_id == repository_id,
                RepositorySnapshot.status.in_(
                    [SnapshotStatus.READY, SnapshotStatus.PARTIAL]
                ),
            )
            .order_by(RepositorySnapshot.indexed_at.desc().nullslast())
            .limit(1)
        )
        return (await session.execute(stmt)).scalars().first()

    @staticmethod
    def _improve_version_confidence(
        snapshot: RepositorySnapshot,
        *,
        version_status: CodeVersionStatus,
        version_evidence: str,
    ) -> None:
        """Raise (never lower) a stored snapshot's version confidence.

        Anything that only re-indexes the same commit passes ``UNKNOWN`` by
        default, so an unconditional write would let a routine re-index *erase*
        the evidence that proved which revision ran — the opposite of what §8
        protects. Only a strictly better status is accepted, and the evidence is
        replaced only when the status improves.
        """
        rank = {
            CodeVersionStatus.UNKNOWN: 0,
            CodeVersionStatus.INFERRED: 1,
            CodeVersionStatus.RESOLVED: 2,
        }
        current = CodeVersionStatus(snapshot.version_status)
        if rank[version_status] <= rank[current]:
            return
        snapshot.version_status = version_status
        snapshot.version_evidence = version_evidence or snapshot.version_evidence


async def _safe_describe(provider: RepositoryProvider) -> Optional[RepositoryInfo]:
    try:
        return await provider.describe()
    except (RepositoryError, ProviderError):
        return None


async def find_repository_for_project(
    session: AsyncSession, project_id, *, preferred_id=None
) -> Optional[CodeRepository]:
    """Pick the repository to analyse for a project.

    An explicit ``preferred_id`` always wins (the caller knows which repository
    an incident belongs to); otherwise the most recently indexed repository is
    chosen, falling back to the oldest row so a brand-new project is still
    analyzable before its first index.
    """
    if preferred_id:
        return (
            (
                await session.execute(
                    select(CodeRepository).where(CodeRepository.id == preferred_id)
                )
            )
            .scalars()
            .first()
        )
    stmt = (
        select(CodeRepository)
        .where(CodeRepository.project_id == project_id)
        .order_by(
            CodeRepository.last_indexed_at.desc().nullslast(), CodeRepository.created_at
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def resolve_component_for_symbol(
    session: AsyncSession, project_id, component_names: Sequence[str]
) -> dict:
    """Map service-ish names to Phase 2 component ids (§13).

    Used to join code edges to the software graph. Matching is by exact
    normalised name/slug only: a fuzzy match would attach code to the wrong
    component, and a wrong join is worse than no join because it looks
    authoritative.
    """
    if not component_names:
        return {}
    wanted = {
        name.strip().lower().replace("-", "_") for name in component_names if name
    }
    if not wanted:
        return {}
    rows = (
        (
            await session.execute(
                select(SystemComponent).where(SystemComponent.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    mapping: dict = {}
    for component in rows:
        for candidate in (component.name, getattr(component, "slug", None)):
            if not candidate:
                continue
            normalised = candidate.strip().lower().replace("-", "_")
            if normalised in wanted:
                mapping[candidate] = component.id
    return mapping
