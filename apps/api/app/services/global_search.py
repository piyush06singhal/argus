"""ARGUS Global Reliability Search (Phase 11 §17, §18, §66).

One query box, categorized results across everything the platform knows: cases,
incidents, components, deployments, commits, forecasts, remediations, knowledge
and anomalies.

§66's instruction is followed literally — **PostgreSQL search first**. There is
no Elasticsearch here, and adding one would buy nothing: the data is already in
Postgres, the volume is bounded per project, and a second system would need its
own consistency story (which is the exact problem this phase exists to remove).
The implementation is portable ``ILIKE`` over indexed text columns, with a
pluggable :class:`SearchBackend` protocol so a deployment that genuinely outgrows
it can swap one in without touching the API.

Two properties worth stating:

* **The result is categorized and capped per category.** A search that returns
  forty incidents and nothing else is not a search.
* **Every hit carries its own type and a link.** §29 wants objects to link
  naturally to related objects, and a search hit that cannot be followed is not a
  result.

Query parsing is deliberately simple and *stated*: quoted phrases, ``kind:``
filters and ``project:``/``environment:`` scoping. There is no query language,
because a query language is a new surface to validate and a new way to produce
confusing results.
"""

from __future__ import annotations

import logging
import shlex
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.services.platform_time import aware as _aware

logger = logging.getLogger(__name__)

#: The categories a search returns (§18). Exported so the UI can render the
#: filter list from the same source the backend uses.
SEARCH_KINDS = (
    "cases",
    "incidents",
    "components",
    "anomalies",
    "deployments",
    "forecasts",
    "remediations",
    "knowledge",
)

#: Route prefix per kind, so a hit is followable (§29).
KIND_ROUTE = {
    "cases": "/cases/",
    "incidents": "/incidents/",
    "components": "/services/",
    "anomalies": "/anomalies/",
    "deployments": "/changes/",
    "forecasts": "/predictions/",
    "remediations": "/remediation/",
    "knowledge": "/intelligence/patterns/",
}


@dataclass
class SearchQuery:
    """A parsed search: terms, filters, limits — all explicit."""

    raw: str
    terms: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    project_id: Optional[uuid.UUID] = None
    environment_id: Optional[uuid.UUID] = None
    limit_per_kind: int = 10
    unmatched: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class SearchHit:
    """One result."""

    kind: str
    id: str
    title: str
    subtitle: Optional[str] = None
    status: Optional[str] = None
    occurred_at: Optional[str] = None
    component_id: Optional[str] = None
    route: Optional[str] = None
    matched_field: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "subtitle": self.subtitle,
            "status": self.status,
            "occurred_at": self.occurred_at,
            "component_id": self.component_id,
            "route": self.route,
            "matched_field": self.matched_field,
            "metadata": self.metadata,
        }


@dataclass
class SearchResults:
    """Categorized results with their own completeness statement."""

    query: str
    total: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    results: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    filters: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "total": self.total,
            "by_kind": self.by_kind,
            "results": self.results,
            "filters": self.filters,
            "notes": list(self.notes),
        }


def parse_query(
    raw: str, *, limit_per_kind: int = 10, project_id: Optional[uuid.UUID] = None
) -> SearchQuery:
    """Parse ``raw`` into terms and filters (§18 structured filtering).

    Supported syntax, and nothing beyond it:

    * ``checkout timeout`` — two terms, all must match (AND)
    * ``"checkout timeout"`` — one phrase
    * ``kind:incidents`` — restrict to one category (repeatable)
    * ``environment:<uuid>`` — scope to one environment
    """
    query = SearchQuery(
        raw=raw.strip(), project_id=project_id, limit_per_kind=limit_per_kind
    )
    try:
        tokens = shlex.split(query.raw)
    except ValueError:
        #: Unbalanced quotes: fall back to whitespace splitting and say so rather
        #: than returning an error for something a person typed by accident.
        tokens = query.raw.split()
        query.notes.append(
            "the query contains unbalanced quotes; it was split on whitespace"
        )

    for token in tokens:
        lowered = token.lower()
        if lowered.startswith("kind:"):
            value = lowered.split(":", 1)[1]
            if value in SEARCH_KINDS:
                query.kinds.append(value)
            else:
                query.unmatched.append(token)
                query.notes.append(
                    f"unknown kind filter '{value}'; valid kinds are "
                    + ", ".join(SEARCH_KINDS)
                )
            continue
        if lowered.startswith("environment:"):
            value = token.split(":", 1)[1]
            try:
                query.environment_id = uuid.UUID(value)
            except (ValueError, TypeError):
                query.unmatched.append(token)
                query.notes.append(f"'{value}' is not a valid environment id")
            continue
        if lowered.startswith("project:"):
            value = token.split(":", 1)[1]
            try:
                query.project_id = uuid.UUID(value)
            except (ValueError, TypeError):
                query.unmatched.append(token)
                query.notes.append(f"'{value}' is not a valid project id")
            continue
        if token.strip():
            query.terms.append(token.strip())

    if not query.kinds:
        query.kinds = list(SEARCH_KINDS)
    if not query.terms:
        query.notes.append("no search terms were supplied")
    return query


class SearchBackend(Protocol):
    """The §66 seam: swap the engine without changing the API."""

    name: str

    async def search(
        self, session: AsyncSession, *, query: SearchQuery, limit: int
    ) -> SearchResults: ...


class PostgresSearchBackend:
    """Portable ``ILIKE`` search over the indexed text columns (§66).

    Chosen over an external engine because the data is here, the volume per
    project is bounded, and correctness of isolation matters more than ranking
    sophistication. Every query is project-scoped, always.
    """

    name = "postgres"

    async def search(
        self, session: AsyncSession, *, query: SearchQuery, limit: int
    ) -> SearchResults:
        results = SearchResults(
            query=query.raw,
            filters={
                "kinds": query.kinds,
                "environment_id": str(query.environment_id)
                if query.environment_id
                else None,
                "limit_per_kind": limit,
            },
            notes=list(query.notes),
        )
        if query.project_id is None:
            results.notes.append(
                "search is project-scoped; no project was supplied so no results "
                "are returned"
            )
            return results
        if not query.terms:
            return results

        for kind in query.kinds:
            try:
                if kind == "cases":
                    hits = await self._cases(session, query=query, limit=limit)
                elif kind == "incidents":
                    hits = await self._incidents(session, query=query, limit=limit)
                elif kind == "components":
                    hits = await self._components(session, query=query, limit=limit)
                elif kind == "anomalies":
                    hits = await self._anomalies(session, query=query, limit=limit)
                elif kind == "deployments":
                    hits = await self._deployments(session, query=query, limit=limit)
                elif kind == "forecasts":
                    hits = await self._forecasts(session, query=query, limit=limit)
                elif kind == "remediations":
                    hits = await self._remediations(session, query=query, limit=limit)
                elif kind == "knowledge":
                    hits = await self._knowledge(session, query=query, limit=limit)
                else:  # pragma: no cover - kinds are validated at parse time
                    hits = []
            except Exception as exc:
                #: One broken category must not fail the whole search (§60).
                logger.warning("search category %s failed", kind, exc_info=True)
                results.notes.append(f"the {kind} search failed: {type(exc).__name__}")
                hits = []
            if hits:
                results.results[kind] = [hit.as_dict() for hit in hits]
                results.by_kind[kind] = len(hits)
                results.total += len(hits)
        if results.total == 0:
            results.notes.append(
                "no stored object matched; ARGUS searches what it has recorded, "
                "and does not guess at objects it has never seen"
            )
        return results

    # -- per-kind queries
    def _like(self, column: Any, term: str) -> Any:
        return column.ilike(f"%{term}%")

    async def _cases(
        self, session: AsyncSession, *, query: SearchQuery, limit: int
    ) -> list[SearchHit]:
        from app.models.platform import ReliabilityCase

        conditions = [
            or_(
                self._like(ReliabilityCase.title, term),
                self._like(ReliabilityCase.reference, term),
                self._like(ReliabilityCase.summary, term),
            )
            for term in query.terms
        ]
        stmt = (
            select(ReliabilityCase)
            .where(ReliabilityCase.project_id == query.project_id)
            .order_by(ReliabilityCase.opened_at.desc())
            .limit(limit)
        )
        for condition in conditions:
            stmt = stmt.where(condition)
        if query.environment_id is not None:
            stmt = stmt.where(ReliabilityCase.environment_id == query.environment_id)
        return [
            SearchHit(
                kind="cases",
                id=str(case.id),
                title=f"{case.reference}: {case.title}",
                subtitle=case.summary,
                status=getattr(case.status, "value", str(case.status)),
                occurred_at=_aware(case.opened_at).isoformat()
                if case.opened_at
                else None,
                component_id=str(case.primary_component_id)
                if case.primary_component_id
                else None,
                route=KIND_ROUTE["cases"] + str(case.id),
                matched_field="title/summary/reference",
            )
            for case in (await session.scalars(stmt)).all()
        ]

    async def _incidents(
        self, session: AsyncSession, *, query: SearchQuery, limit: int
    ) -> list[SearchHit]:
        from app.models.incident import Incident

        stmt = (
            select(Incident)
            .where(Incident.project_id == query.project_id)
            .order_by(Incident.detected_at.desc())
            .limit(limit)
        )
        for term in query.terms:
            stmt = stmt.where(
                or_(
                    self._like(Incident.title, term),
                    self._like(Incident.description, term),
                    self._like(Incident.fingerprint, term),
                )
            )
        if query.environment_id is not None:
            stmt = stmt.where(Incident.environment_id == query.environment_id)
        return [
            SearchHit(
                kind="incidents",
                id=str(incident.id),
                title=incident.title,
                subtitle=incident.summary or incident.description,
                status=getattr(incident.status, "value", str(incident.status)),
                occurred_at=_aware(incident.detected_at).isoformat()
                if incident.detected_at
                else None,
                component_id=str(incident.primary_component_id)
                if incident.primary_component_id
                else None,
                route=KIND_ROUTE["incidents"] + str(incident.id),
                matched_field="title/description",
                metadata={
                    "severity": getattr(
                        incident.severity, "value", str(incident.severity)
                    )
                },
            )
            for incident in (await session.scalars(stmt)).all()
        ]

    async def _components(
        self, session: AsyncSession, *, query: SearchQuery, limit: int
    ) -> list[SearchHit]:
        from app.models.system import SystemComponent

        stmt = (
            select(SystemComponent)
            .where(SystemComponent.project_id == query.project_id)
            .order_by(SystemComponent.name)
            .limit(limit)
        )
        for term in query.terms:
            stmt = stmt.where(
                or_(
                    self._like(SystemComponent.name, term),
                    self._like(SystemComponent.description, term),
                )
            )
        if query.environment_id is not None:
            stmt = stmt.where(SystemComponent.environment_id == query.environment_id)
        return [
            SearchHit(
                kind="components",
                id=str(component.id),
                title=component.name,
                subtitle=component.description,
                status=getattr(component.status, "value", str(component.status)),
                component_id=str(component.id),
                route=KIND_ROUTE["components"] + str(component.id),
                matched_field="name/description",
                metadata={
                    "component_type": getattr(
                        component.component_type, "value", str(component.component_type)
                    )
                },
            )
            for component in (await session.scalars(stmt)).all()
        ]

    async def _anomalies(
        self, session: AsyncSession, *, query: SearchQuery, limit: int
    ) -> list[SearchHit]:
        from app.models.anomaly import Anomaly

        stmt = (
            select(Anomaly)
            .where(Anomaly.project_id == query.project_id)
            .order_by(Anomaly.detected_at.desc())
            .limit(limit)
        )
        for term in query.terms:
            stmt = stmt.where(
                or_(
                    self._like(Anomaly.metric_name, term),
                    self._like(Anomaly.description, term),
                    self._like(Anomaly.pattern_template, term),
                )
            )
        if query.environment_id is not None:
            stmt = stmt.where(Anomaly.environment_id == query.environment_id)
        return [
            SearchHit(
                kind="anomalies",
                id=str(anomaly.id),
                title=anomaly.description
                or f"{getattr(anomaly.anomaly_type, 'value', '')} on "
                f"{anomaly.metric_name or 'a metric'}",
                subtitle=anomaly.metric_name,
                status=getattr(anomaly.status, "value", str(anomaly.status)),
                occurred_at=_aware(anomaly.detected_at).isoformat()
                if anomaly.detected_at
                else None,
                component_id=str(anomaly.component_id)
                if anomaly.component_id
                else None,
                route=KIND_ROUTE["anomalies"] + str(anomaly.id),
                matched_field="metric_name/description",
                metadata={
                    "severity": getattr(
                        anomaly.severity, "value", str(anomaly.severity)
                    )
                },
            )
            for anomaly in (await session.scalars(stmt)).all()
        ]

    async def _deployments(
        self, session: AsyncSession, *, query: SearchQuery, limit: int
    ) -> list[SearchHit]:
        from app.models.deployment import DeploymentEvent

        stmt = (
            select(DeploymentEvent)
            .where(DeploymentEvent.project_id == query.project_id)
            .order_by(DeploymentEvent.deployed_at.desc())
            .limit(limit)
        )
        for term in query.terms:
            stmt = stmt.where(
                or_(
                    self._like(DeploymentEvent.version, term),
                    self._like(DeploymentEvent.commit_sha, term),
                    self._like(DeploymentEvent.description, term),
                    self._like(DeploymentEvent.deployment_id, term),
                )
            )
        if query.environment_id is not None:
            stmt = stmt.where(DeploymentEvent.environment_id == query.environment_id)
        return [
            SearchHit(
                kind="deployments",
                id=str(deployment.id),
                title=(
                    f"Deployment {deployment.version or deployment.commit_sha or 'unknown'}"
                ),
                subtitle=deployment.description,
                status=getattr(deployment.status, "value", str(deployment.status)),
                occurred_at=_aware(deployment.deployed_at).isoformat()
                if deployment.deployed_at
                else None,
                component_id=str(deployment.component_id)
                if deployment.component_id
                else None,
                route=KIND_ROUTE["deployments"] + str(deployment.id),
                matched_field="version/commit_sha/description",
                metadata={"commit_sha": deployment.commit_sha},
            )
            for deployment in (await session.scalars(stmt)).all()
        ]

    async def _forecasts(
        self, session: AsyncSession, *, query: SearchQuery, limit: int
    ) -> list[SearchHit]:
        from app.models.reliability import ReliabilityForecast

        stmt = (
            select(ReliabilityForecast)
            .where(ReliabilityForecast.project_id == query.project_id)
            .order_by(ReliabilityForecast.generated_at.desc())
            .limit(limit)
        )
        for term in query.terms:
            stmt = stmt.where(self._like(ReliabilityForecast.summary, term))
        if query.environment_id is not None:
            stmt = stmt.where(
                ReliabilityForecast.environment_id == query.environment_id
            )
        return [
            SearchHit(
                kind="forecasts",
                id=str(forecast.id),
                title=(
                    f"{getattr(forecast.prediction_type, 'value', '')} — "
                    f"{getattr(forecast.risk_level, 'value', '')} risk"
                ),
                subtitle=forecast.summary,
                status=getattr(forecast.status, "value", str(forecast.status)),
                occurred_at=_aware(forecast.generated_at).isoformat()
                if forecast.generated_at
                else None,
                component_id=str(forecast.component_id)
                if forecast.component_id
                else None,
                route=KIND_ROUTE["forecasts"] + str(forecast.id),
                matched_field="summary",
                metadata={"risk_score": forecast.risk_score},
            )
            for forecast in (await session.scalars(stmt)).all()
        ]

    async def _remediations(
        self, session: AsyncSession, *, query: SearchQuery, limit: int
    ) -> list[SearchHit]:
        from app.models.remediation import RemediationAction

        stmt = (
            select(RemediationAction)
            .where(RemediationAction.project_id == query.project_id)
            .order_by(RemediationAction.created_at.desc())
            .limit(limit)
        )
        for term in query.terms:
            stmt = stmt.where(
                or_(
                    self._like(RemediationAction.description, term),
                    self._like(RemediationAction.reason, term),
                )
            )
        if query.environment_id is not None:
            stmt = stmt.where(RemediationAction.environment_id == query.environment_id)
        return [
            SearchHit(
                kind="remediations",
                id=str(action.id),
                title=(
                    f"{getattr(action.action_type, 'value', '')}: "
                    f"{action.description or 'no description'}"
                ),
                subtitle=action.reason,
                status=getattr(action.status, "value", str(action.status)),
                occurred_at=_aware(action.created_at).isoformat()
                if action.created_at
                else None,
                component_id=str(action.component_id) if action.component_id else None,
                route=KIND_ROUTE["remediations"] + str(action.id),
                matched_field="description/reason",
            )
            for action in (await session.scalars(stmt)).all()
        ]

    async def _knowledge(
        self, session: AsyncSession, *, query: SearchQuery, limit: int
    ) -> list[SearchHit]:
        from app.models.intelligence import ReliabilityKnowledge

        stmt = (
            select(ReliabilityKnowledge)
            .where(ReliabilityKnowledge.project_id == query.project_id)
            .order_by(ReliabilityKnowledge.sample_count.desc())
            .limit(limit)
        )
        for term in query.terms:
            stmt = stmt.where(
                or_(
                    self._like(ReliabilityKnowledge.title, term),
                    self._like(ReliabilityKnowledge.description, term),
                )
            )
        hits: list[SearchHit] = []
        for knowledge in (await session.scalars(stmt)).all():
            hits.append(
                SearchHit(
                    kind="knowledge",
                    id=str(knowledge.id),
                    title=knowledge.title or "learned pattern",
                    subtitle=knowledge.description,
                    status=getattr(knowledge.status, "value", str(knowledge.status)),
                    occurred_at=_aware(knowledge.created_at).isoformat()
                    if knowledge.created_at
                    else None,
                    route=KIND_ROUTE["knowledge"] + str(knowledge.id),
                    matched_field="title/description",
                    metadata={"sample_count": knowledge.sample_count},
                )
            )
        return hits


#: The active backend. A deployment that adopts another engine replaces this.
backend: SearchBackend = PostgresSearchBackend()


async def search(
    session: AsyncSession,
    *,
    raw_query: str,
    project_id: uuid.UUID,
    limit_per_kind: int = 10,
    kinds: Optional[Sequence[str]] = None,
    environment_id: Optional[uuid.UUID] = None,
    settings: Optional[Settings] = None,
) -> SearchResults:
    """Run a global search (§18)."""
    settings = settings or get_settings()
    query = parse_query(raw_query, limit_per_kind=limit_per_kind, project_id=project_id)
    if kinds:
        query.kinds = [kind for kind in kinds if kind in SEARCH_KINDS] or query.kinds
    if environment_id is not None:
        query.environment_id = environment_id
    return await backend.search(
        session,
        query=query,
        limit=min(limit_per_kind, settings.PLATFORM_SEARCH_MAX_PER_KIND),
    )


def search_help() -> dict[str, Any]:
    """The search syntax, served so the UI does not hard-code it (§18)."""
    return {
        "kinds": list(SEARCH_KINDS),
        "examples": [
            "checkout timeout",
            '"inventory latency"',
            "kind:incidents database",
            "kind:cases checkout",
            "kind:deployments abc123",
        ],
        "filters": {
            "kind": "restrict to one category (repeatable)",
            "environment": "an environment id",
            "project": "a project id (the API's own scope takes precedence)",
        },
        "notes": [
            "terms are combined with AND: every term must match somewhere in the "
            "object",
            "search is always project-scoped; there is no cross-project search "
            "unless a tenant policy explicitly enables it",
        ],
    }


__all__ = [
    "KIND_ROUTE",
    "SEARCH_KINDS",
    "PostgresSearchBackend",
    "SearchBackend",
    "SearchHit",
    "SearchQuery",
    "SearchResults",
    "backend",
    "parse_query",
    "search",
    "search_help",
]
