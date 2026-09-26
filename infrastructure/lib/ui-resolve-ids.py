#!/usr/bin/env python3
"""ARGUS UI gate — resolve real row ids from the live API.

The UI gate visits every dynamic route with a *real* row id, because a route
rendered with a fabricated id proves nothing: a detail page with no data and a
detail page whose data fetch is broken look identical from the outside.

Three mistakes this resolver exists to prevent, all of which it made first:

1. **Assuming the first project has data.** ``/projects?page_size=1`` returns
   whichever project sorts first, which on a long-lived stack is an empty
   scratch project left behind by an earlier gate. Every scoped list then
   returns zero items and every dynamic route reports SKIP — the gate passes
   while testing nothing. So the candidate projects are harvested from the
   endpoints that actually carry rows, richest first.
2. **Assuming one envelope.** The API is deliberately not uniform: most lists
   are ``{items: [...]}``, remediation actions are ``{actions: [...]}``,
   reliability cases are ``{cases: [...]}``, SLO objectives live *inside* a
   summary object as ``{objectives: [...]}``, and a trace's route id is its
   ``trace_id`` rather than its row ``id``. Guessing one shape silently yields
   no id; this reads every shape the API documents.
3. **Assuming every endpoint is scope-optional.** Some are global under an admin
   token, some require ``project_id``. Rather than hard-code that split (it was
   the source of the original bug), each lookup tries the unscoped call first
   and falls back to walking candidate projects.

Output is shell assignments for the gate to ``eval``, so a lookup that cannot be
satisfied still produces a clean empty value and an honest SKIP rather than an
error. Auth travels as ``Authorization: Bearer`` from ``$ARGUS_TOKEN`` — the same
credential the gate's ``curl`` shim uses (hardening W1).
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.request
from typing import Any

API = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
TOKEN = os.environ.get("ARGUS_TOKEN", "")

#: How many candidate projects a scoped lookup will walk. Bounded on purpose:
#: the walk costs one request per candidate, and an unbounded one (every project
#: ever created) drove the resolver into the platform's own rate limiter on a
#: 168-project dev stack. Candidates are ranked by observed data volume, so the
#: projects that hold rows are always inside this window.
MAX_CANDIDATE_PROJECTS = 40

#: Envelope keys that carry a list, in the order worth trying. ``items`` first
#: because it is the platform's dominant shape.
LIST_KEYS = ("items", "actions", "cases", "experiments", "runs", "forecasts", "objectives")

#: Row keys that hold the id a *route* needs, tried in order. Most rows are
#: addressed by ``id``; a trace route takes the OTel ``trace_id`` (a string, not
#: the row's uuid); an SLO objective is a read model whose id is ``slo_id``. The
#: aliases are declared rather than guessed at, because picking a foreign key by
#: accident would send the gate to a route with somebody else's id.
ID_ALIASES = ("id", "trace_id", "slo_id")


#: The platform rate-limits per credential (W1: ``RATE_LIMIT_BURST`` of 120, then
#: ``RATE_LIMIT_PER_MINUTE``). A gate that resolves ids by walking projects can
#: exceed the burst — and a ``429`` that is treated as "no such row" turns the
#: whole dynamic-route section into silent SKIPs while the gate reports success.
#: So a throttle is waited out, not mistaken for absence.
RATE_LIMIT_RETRIES = 4

#: Set when a throttle was actually engaged, so the gate can say so out loud.
THROTTLED = False


def get(path: str) -> Any:
    """GET a JSON document, or ``None`` for any non-2xx / unparseable answer.

    A ``429`` is retried with the server's own ``Retry-After`` (or exponential
    backoff when absent) rather than reported as a missing row.
    """
    global THROTTLED
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        request = urllib.request.Request(f"{API}{path}")
        if TOKEN:
            request.add_header("Authorization", f"Bearer {TOKEN}")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == RATE_LIMIT_RETRIES:
                return None
            THROTTLED = True
            retry_after = error.headers.get("Retry-After") if error.headers else None
            try:
                delay = float(retry_after) if retry_after else float(2**attempt)
            except ValueError:
                delay = float(2**attempt)
            time.sleep(min(delay, 10.0))
        except (urllib.error.URLError, ValueError, TimeoutError):
            return None
    return None


def first_row(document: Any) -> dict:
    """The first row of whichever list envelope this document uses."""
    if isinstance(document, dict):
        for key in LIST_KEYS:
            value = document.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value[0]
    if isinstance(document, list) and document and isinstance(document[0], dict):
        return document[0]
    return {}


def row_id(row: dict) -> str:
    """The id a route actually needs, across the API's id-naming conventions.

    A rectangular ``id`` assumption silently yielded an empty id for objectives
    (whose read model names it ``slo_id``), which made the SLO detail route look
    like "no data in the stack" long after objectives existed.
    """
    for key in ID_ALIASES:
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def candidate_projects() -> list[str]:
    """Project ids that are known to have data, in a deterministic order.

    Two properties matter here, and the first attempt had neither:

    * **Deterministic.** List endpoints do not promise a stable row order (no
      ORDER BY is implied), so discovery order alone made the *resolved ids*
      change between runs — a scoped lookup would find a forecast on one run and
      silently miss it on the next. Candidates are therefore scored and sorted
      rather than trusted as returned.
    * **Complete enough.** A project that only ever appears on the incidents
      list itself still has to be reachable, so the harvest pages through the
      endpoints and falls back to the project list.

    Score = the number of observed rows the project owns, so the projects the
    platform has actually exercised rank first; ties break on the id so the order
    is total (and therefore stable run to run).
    """
    scores: dict[str, int] = {}

    def remember(project_id: Any) -> None:
        if isinstance(project_id, str) and project_id:
            scores[project_id] = scores.get(project_id, 0) + 1

    # Ordered by how much downstream data each kind of row implies: a project
    # with incidents has anomalies, components and traces by construction.
    ranked_sources: dict[str, int] = {}
    for path in (
        "/api/v1/incidents?page_size=100",
        "/api/v1/remediation/actions?page_size=100",
        "/api/v1/anomalies?page_size=100",
        "/api/v1/intelligence/learning-runs?page_size=100",
        "/api/v1/components?page_size=100",
        "/api/v1/observability/traces?page_size=100",
    ):
        document = get(path)
        if not isinstance(document, dict):
            continue
        rows: list[dict] = []
        for key in LIST_KEYS:
            value = document.get(key)
            if isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, dict))
        for row in rows:
            remember(row.get("project_id"))
        # A kind that contributed rows also contributed spread, which is what
        # makes a project *broadly* exercised rather than merely deep in one list.
        for row in rows:
            project_id = row.get("project_id")
            if isinstance(project_id, str) and project_id:
                ranked_sources[project_id] = ranked_sources.get(project_id, 0) + 1

    # Any project at all, so a stack whose rows carry no project_id (or whose
    # data kinds are entirely absent) still resolves the project-scoped pages.
    # These score lowest by construction: no observed rows.
    for page in (1, 2, 3, 4):
        document = get(f"/api/v1/projects?page_size=50&page={page}")
        if not isinstance(document, dict):
            break
        items = document.get("items")
        if not isinstance(items, list) or not items:
            break
        for row in items:
            if not isinstance(row, dict):
                continue
            project_id = row.get("project_id") or row.get("id")
            if isinstance(project_id, str) and project_id:
                scores.setdefault(project_id, 0)

    ranked = sorted(
        scores.items(), key=lambda item: (-item[1], -ranked_sources.get(item[0], 0), item[0])
    )
    return [project_id for project_id, _ in ranked][:MAX_CANDIDATE_PROJECTS]


PROJECTS = candidate_projects()


def scoped(project_id: str) -> str:
    return f"&project_id={project_id}" if project_id else ""


def resolve(path: str, prefer: str = "") -> str:
    """Resolve one row id: unscoped first, then across the candidate projects.

    ``prefer`` is tried before the ranked list — it is the project the pages
    themselves will be scoped to, so a row there is the most useful one to test
    with (a detail page and its owning list then agree on the project).
    """
    unscoped = first_row(get(path))
    if unscoped:
        return row_id(unscoped)
    candidates = ([prefer] if prefer else []) + [
        project_id for project_id in PROJECTS if project_id != prefer
    ]
    for project_id in candidates:
        scoped_row = first_row(get(f"{path}{'&' if '?' in path else '?'}project_id={project_id}"))
        if scoped_row:
            return row_id(scoped_row)
    return ""


def resolve_incident() -> str:
    return resolve("/api/v1/incidents?page_size=1")


def resolve_metric_name(project_id: str) -> str:
    """A metric the anchor project actually reports.

    The SLO API refuses an objective that does not name a metric ("an objective
    must name a metric unless its indicator is CUSTOM"), so a gate that wants to
    open the objective detail page has to name a real one — inventing a name
    would be rejected and the route would skip forever.
    """
    for candidate in ([project_id] if project_id else []) + PROJECTS:
        document = get(f"/api/v1/observability/metrics?page_size=1&project_id={candidate}")
        row = first_row(document)
        name = row.get("metric_name")
        if isinstance(name, str) and name:
            return name
    return ""


def resolve_scoped_many(wants: dict[str, str], prefer: str) -> dict[str, str]:
    """Resolve several project-scoped ids in one pass over the candidates.

    One walk per endpoint (the first version) multiplied the request count by the
    number of kinds and helped push the gate into the platform's own rate limiter.
    Visiting each candidate project **once** and asking it for every still-missing
    kind is both fewer requests and faster to finish, because the projects that
    hold one kind usually hold the neighbouring ones too.
    """
    remaining = dict(wants)
    resolved: dict[str, str] = {}
    for project_id in ([prefer] if prefer else []) + [
        item for item in PROJECTS if item != prefer
    ]:
        for name in list(remaining):
            path = remaining[name]
            row = first_row(
                get(f"{path}{'&' if '?' in path else '?'}project_id={project_id}")
            )
            if row:
                resolved[name] = row_id(row)
                del remaining[name]
        if not remaining:
            break
    return {name: resolved.get(name, "") for name in wants}


def session_under(incident_id: str) -> str:
    return row_id(first_row(get(f"/api/v1/incidents/{incident_id}/debug-sessions?page_size=1")))


def resolve_debug_session(incident_id: str, project_id: str) -> str:
    """Find an incident that already has a debugging session.

    A session exists only under an incident somebody actually investigated, which
    is rarely the newest one, and there is no global sessions list to ask — so
    the only way is to look under incidents. Done naively (walk the platform's
    incidents until one answers) that is hundreds of requests and the resolver
    throttles itself.

    So candidates are gathered **one request per project** for the top-ranked
    projects, then interleaved: the newest incident of each project first, then
    the second of each, and so on. A stack with any sessions at all therefore
    answers within a couple of rounds, whichever project happens to hold them.
    """
    if incident_id:
        found = session_under(incident_id)
        if found:
            return found

    ordered: list[str] = []
    for candidate in [project_id, *PROJECTS[:8]]:
        if not candidate:
            continue
        document = get(f"/api/v1/incidents?page_size=25&project_id={candidate}")
        items = document.get("items") if isinstance(document, dict) else None
        if not isinstance(items, list):
            continue
        for index, row in enumerate(items):
            if isinstance(row, dict) and row.get("id"):
                # Interleave by age: index 0 of every project, then index 1, …
                position = index * 100 + len(ordered)
                ordered.append((position, str(row["id"])))  # type: ignore[arg-type]
    for _, candidate_id in sorted(ordered):
        found = session_under(candidate_id)
        if found:
            return found
    return ""


def main() -> int:
    incident_id = resolve_incident()
    anomaly_id = resolve("/api/v1/anomalies?page_size=1")
    component_id = resolve("/api/v1/components?page_size=1")
    trace_id = resolve("/api/v1/observability/traces?page_size=1")

    # The project scope for the *page* assertions is the project that owns the
    # incident we already have — that is what makes a detail route render data
    # rather than its empty state.
    project_id = ""
    if incident_id:
        document = get(f"/api/v1/incidents/{incident_id}")
        row = document if isinstance(document, dict) else {}
        project_id = str(row.get("project_id") or "")
    if not project_id:
        project_id = PROJECTS[0] if PROJECTS else ""

    session_id = resolve_debug_session(incident_id, project_id)

    scoped_ids = resolve_scoped_many(
        {
            "fix_id": "/api/v1/fixes?page_size=1",
            "repro_id": "/api/v1/reproductions?page_size=1",
            "knowledge_id": "/api/v1/intelligence/knowledge?page_size=1",
            "experience_id": "/api/v1/intelligence/experiences?page_size=1",
            "run_id": "/api/v1/intelligence/learning-runs?page_size=1",
            "rec_id": "/api/v1/intelligence/recommendations?page_size=1",
            "forecast_id": "/api/v1/reliability/forecasts?page_size=1",
            # There is no standalone objectives list: they arrive inside the SLO
            # overview, so the id is read as an objective row.
            "slo_id": "/api/v1/platform/slo?page_size=1",
            "case_id": "/api/v1/platform/cases?page_size=1",
        },
        project_id,
    )

    resolved = {
        "project_id": project_id,
        "incident_id": incident_id,
        "anomaly_id": anomaly_id,
        "component_id": component_id,
        "trace_id": trace_id,
        "action_id": resolve("/api/v1/remediation/actions?page_size=1"),
        "session_id": session_id,
        "slo_metric": resolve_metric_name(project_id),
        **scoped_ids,
    }

    for name, value in resolved.items():
        print(f"export {name}={shlex.quote(value)}")

    if THROTTLED:
        # Reported, not hidden: the ids are still real, but the gate had to wait
        # for the platform's own rate limiter, and the operator should know the
        # run was throttled rather than assume a slow stack.
        print(
            "note: the API rate limiter was engaged while resolving ids "
            "(requests were retried after Retry-After)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
