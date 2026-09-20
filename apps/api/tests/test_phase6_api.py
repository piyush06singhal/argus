"""Phase 6 API tests (§47–§52).

The surface has two halves and both are tested here:

* the **investigation** surface — repositories, indexing, code search, trace
  mappings, sessions, hypotheses, evidence, timeline, metrics;
* the **refusals** — a mutating call without ``project_id``, a resource from
  another project, a session asked to claim code with no pinned snapshot.

Refusals get equal weight because they are what keeps a UUID from being treated
as authority, and because the phase's central promise — no claim without
evidence — is only credible if the API cannot be talked out of it.

Rows are seeded through ``db_session`` (the same database the client uses) and
asserted through HTTP, so the responses describe real stored state.
"""

from __future__ import annotations

import os
import subprocess
import uuid

from phase6_helpers import SAMPLE_FILES, build_incident, build_scope


async def _project(client, name: str) -> str:
    response = client.post(
        "/api/v1/projects",
        json={
            "name": name,
            "slug": f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:4]}",
        },
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


async def _registered_repository(
    client, project_id: str, tmp_path, name: str = "repo", *, return_root: bool = False
) -> dict:
    """Register a repository through the API against a real fixture checkout."""
    root = str(tmp_path / name)
    for path, content in SAMPLE_FILES.items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as handle:
            handle.write(content)
    subprocess.run(["git", "-C", root, "init", "-q"], check=True)
    subprocess.run(["git", "-C", root, "config", "user.email", "api@argus"], check=True)
    subprocess.run(["git", "-C", root, "config", "user.name", "ARGUS API"], check=True)
    subprocess.run(["git", "-C", root, "add", "-A"], check=True)
    subprocess.run(["git", "-C", root, "commit", "-q", "-m", "initial"], check=True)

    response = client.post(
        f"/api/v1/projects/{project_id}/repositories",
        json={"provider": "local", "repository_url": root, "default_branch": "main"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    if return_root:
        return {"repository": body, "path": root}
    return body


def _index(client, project_id: str, repository_id: str, **body) -> dict:
    response = client.post(
        f"/api/v1/projects/{project_id}/repositories/{repository_id}/index",
        json={"incremental": True, **body},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Repositories and indexing
# ---------------------------------------------------------------------------
async def test_register_and_index_a_repository(client, tmp_path):
    project_id = await _project(client, "Phase6 API")
    repository = await _registered_repository(client, project_id, tmp_path)
    assert repository["provider"] == "local"
    assert repository["index_status"] == "PENDING"
    assert repository["connection_status"] == "CONNECTED"
    assert {"read_files", "history"} <= set(repository["capabilities"])

    result = _index(client, project_id, repository["id"])
    assert result["snapshot"]["commit_sha"]
    assert result["snapshot"]["status"] in {"READY", "PARTIAL"}
    assert result["run"]["files_indexed"] >= 4
    assert result["run"]["symbols_indexed"] >= 8
    assert result["run"]["relationships_indexed"] > 0
    assert result["notes"], "the index response explains what it did"

    listed = client.get(f"/api/v1/projects/{project_id}/repositories").json()
    assert listed["total"] == 1
    assert listed["items"][0]["latest_snapshot_id"] == result["snapshot"]["id"]
    assert listed["items"][0]["index_status"] == "INDEXED"
    assert listed["items"][0]["snapshot_count"] == 1


async def test_revision_is_the_snapshot_key(client, tmp_path):
    project_id = await _project(client, "Phase6 Reindex")
    repository = await _registered_repository(client, project_id, tmp_path)
    first = _index(client, project_id, repository["id"])
    second = _index(client, project_id, repository["id"])
    assert first["snapshot"]["id"] == second["snapshot"]["id"], "the revision is the key"
    #: Re-indexing an unchanged revision is a true no-op: nothing is re-parsed
    #: (the perf guarantee) and every file is reported as what it is — reused.
    #: Symbol ids stay stable, so a stored session's references keep resolving.
    assert second["run"]["files_indexed"] == 0
    assert second["run"]["files_reused"] >= 4
    assert second["run"]["incremental"] is True


async def test_incremental_index_reuses_unchanged_files(client, tmp_path):
    """§55: moving the repository index only what changed."""
    project_id = await _project(client, "Phase6 Incremental")
    root = await _registered_repository(client, project_id, tmp_path, return_root=True)
    repository = root["repository"]
    first = _index(client, project_id, repository["id"])
    first_sha = first["snapshot"]["commit_sha"]

    #: One changed file, one new commit.
    with open(os.path.join(root["path"], "shop", "inventory.py")) as handle:
        content = handle.read()
    with open(os.path.join(root["path"], "shop", "inventory.py"), "w") as handle:
        handle.write(content.replace("DB_TIMEOUT_SECONDS = 0.5", "DB_TIMEOUT_SECONDS = 0.25"))
    subprocess.run(["git", "-C", root["path"], "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", root["path"], "commit", "-q", "-m", "reduce timeout"], check=True
    )

    second = _index(client, project_id, repository["id"])
    assert second["snapshot"]["commit_sha"] != first_sha
    assert second["snapshot"]["id"] != first["snapshot"]["id"]
    assert second["run"]["base_commit_sha"] == first_sha, "the base revision is recorded"
    assert second["run"]["files_reused"] >= 3, "unchanged files are reused"
    assert second["run"]["files_modified"] >= 1, "the changed file is re-parsed"
    assert second["run"]["incremental"] is True

    snapshots = client.get(
        f"/api/v1/projects/{project_id}/repositories/{repository['id']}/snapshots"
    ).json()
    assert snapshots["total"] == 2, "the previous snapshot is preserved, not replaced"


async def test_registering_an_unreadable_path_is_rejected(client, tmp_path):
    project_id = await _project(client, "Phase6 Bad Repo")
    response = client.post(
        f"/api/v1/projects/{project_id}/repositories",
        json={"provider": "local", "repository_url": str(tmp_path / "does-not-exist")},
    )
    assert response.status_code == 422
    assert "not readable" in response.json()["detail"]


async def test_known_providers_only_and_no_credential_fields(client):
    project_id = await _project(client, "Phase6 Provider")
    unsupported = client.post(
        f"/api/v1/projects/{project_id}/repositories",
        json={"provider": "github", "repository_url": "https://example.com/x.git"},
    )
    assert unsupported.status_code == 422

    smuggled = client.post(
        f"/api/v1/projects/{project_id}/repositories",
        json={
            "provider": "local",
            "repository_url": "/tmp",
            "credentials": "hunter2",
        },
    )
    assert smuggled.status_code == 422, "credential fields are refused outright"


async def test_repository_operations_require_project_scope(client, tmp_path):
    owner = await _project(client, "Phase6 Owner")
    intruder = await _project(client, "Phase6 Intruder")
    repository = await _registered_repository(client, owner, tmp_path)

    cross = client.post(
        f"/api/v1/projects/{intruder}/repositories/{repository['id']}/index",
        json={"incremental": True},
    )
    assert cross.status_code == 404, "an out-of-scope repository is not visible"

    cross_read = client.get(
        f"/api/v1/projects/{intruder}/repositories/{repository['id']}"
    )
    assert cross_read.status_code == 404

    missing = client.get(f"/api/v1/projects/{uuid.uuid4()}/repositories")
    assert missing.status_code == 404


async def test_snapshot_and_code_intelligence_endpoints(client, tmp_path):
    project_id = await _project(client, "Phase6 Code")
    repository = await _registered_repository(client, project_id, tmp_path)
    indexed = _index(client, project_id, repository["id"])
    snapshot_id = indexed["snapshot"]["id"]

    summary = client.get(f"/api/v1/snapshots/{snapshot_id}").json()
    assert summary["files"] >= 4
    assert summary["symbols"] >= 8
    assert summary["languages"]
    assert isinstance(summary["limitations"], list)

    files = client.get(f"/api/v1/snapshots/{snapshot_id}/files").json()
    assert files["total"] >= 4
    assert any(row["is_test"] for row in files["items"])
    tests_only = client.get(
        f"/api/v1/snapshots/{snapshot_id}/files", params={"tests_only": True}
    ).json()
    assert tests_only["total"] >= 1
    assert all(row["is_test"] for row in tests_only["items"])

    symbols = client.get(
        f"/api/v1/snapshots/{snapshot_id}/symbols", params={"query": "process"}
    ).json()
    assert symbols["items"]
    assert symbols["items"][0]["reference"].startswith("FILE:")

    symbol_id = symbols["items"][0]["id"]
    detail = client.get(f"/api/v1/snapshots/{snapshot_id}/symbols/{symbol_id}").json()
    assert detail["file_path"] == "shop/checkout.py"
    assert detail["source"]
    assert detail["callers"] or detail["callees"]

    search = client.get(
        f"/api/v1/snapshots/{snapshot_id}/search", params={"query": "RETRY"}
    ).json()
    assert search["symbols"] or search["source_matches"]

    signals = client.get(f"/api/v1/snapshots/{snapshot_id}/risk-signals").json()
    assert isinstance(signals["items"], list)
    for row in signals["items"]:
        assert "defect" in row["interpretation"], "signals are not bug scores"


async def test_unknown_snapshot_is_404(client):
    assert client.get(f"/api/v1/snapshots/{uuid.uuid4()}").status_code == 404
    assert client.get(f"/api/v1/snapshots/{uuid.uuid4()}/files").status_code == 404


async def test_symbol_from_another_snapshot_is_not_visible(client, tmp_path):
    first = await _project(client, "Phase6 Snap A")
    repository_a = await _registered_repository(client, first, tmp_path, "a")
    snapshot_a = _index(client, first, repository_a["id"])["snapshot"]["id"]
    symbol_id = client.get(
        f"/api/v1/snapshots/{snapshot_a}/symbols", params={"query": "process"}
    ).json()["items"][0]["id"]

    second = await _project(client, "Phase6 Snap B")
    repository_b = await _registered_repository(client, second, tmp_path, "b")
    snapshot_b = _index(client, second, repository_b["id"])["snapshot"]["id"]

    response = client.get(f"/api/v1/snapshots/{snapshot_b}/symbols/{symbol_id}")
    assert response.status_code == 404
    assert "not found in this snapshot" in response.json()["detail"]


async def test_snapshot_reads_can_be_scoped_to_a_project(client, tmp_path):
    owner = await _project(client, "Phase6 Scope A")
    other = await _project(client, "Phase6 Scope B")
    repository = await _registered_repository(client, owner, tmp_path)
    snapshot_id = _index(client, owner, repository["id"])["snapshot"]["id"]

    assert client.get(f"/api/v1/snapshots/{snapshot_id}").status_code == 200
    assert (
        client.get(
            f"/api/v1/snapshots/{snapshot_id}", params={"project_id": owner}
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/v1/snapshots/{snapshot_id}", params={"project_id": other}
        ).status_code
        == 404
    )


async def test_change_history_over_the_api(client, tmp_path):
    project_id = await _project(client, "Phase6 History")
    repository = await _registered_repository(client, project_id, tmp_path)
    _index(client, project_id, repository["id"])
    base = f"/api/v1/projects/{project_id}/repositories/{repository['id']}"

    history = client.get(f"{base}/history")
    assert history.status_code == 200
    assert history.json()["items"], "the initial commit is visible"

    file_history = client.get(f"{base}/history", params={"path": "shop/checkout.py"})
    assert file_history.status_code == 200

    diff = client.get(f"{base}/diff")
    assert diff.status_code == 200

    #: Blame is provider-dependent: the local directory provider has no VCS, so
    #: it answers with an empty list *and* the reason. An empty list without a
    #: reason would be indistinguishable from "this file has no history".
    blame = client.get(f"{base}/blame", params={"path": "shop/checkout.py"})
    assert blame.status_code == 200
    body = blame.json()
    assert body["items"] or body["reason"], "an empty blame names its reason"
    assert body["path"] == "shop/checkout.py"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
async def _session_fixture(client, db_session, tmp_path, name: str = "Phase6 Session"):
    """A project created over HTTP with telemetry seeded into the *same* project.

    The project id comes from the API and is then used for every seeded row, so
    the incident's spans really do belong to the scoped project — otherwise the
    trace-mapping isolation this suite checks would be untestable.
    """
    project_id = await _project(client, name)
    repository = await _registered_repository(client, project_id, tmp_path, name.replace(" ", ""))
    snapshot = _index(client, project_id, repository["id"])["snapshot"]
    environment, component = await build_scope(db_session, uuid.UUID(project_id))
    incident = await build_incident(db_session, _Scoped(project_id), environment, component)
    await db_session.commit()
    return project_id, repository, snapshot, incident


class _Scoped:
    """A minimal stand-in for a project row that only needs its id."""

    def __init__(self, project_id: str) -> None:
        self.id = uuid.UUID(project_id)


async def test_debug_session_lifecycle(client, db_session, tmp_path):
    project_id, repository, snapshot, incident = await _session_fixture(
        client, db_session, tmp_path
    )
    created = client.post(
        f"/api/v1/incidents/{incident.id}/debug-sessions",
        params={"project_id": project_id},
        json={
            "repository_id": repository["id"],
            "created_by": "engineer",
            "run_analysis": True,
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "COMPLETED"
    assert body["snapshot_id"] == snapshot["id"]
    assert body["version_status"] in {"RESOLVED", "INFERRED", "UNKNOWN"}
    assert body["latest_analysis"] is not None
    assert body["counts"]["evidence"] >= 1
    assert body["messages"], "the analysis leaves a message trail"

    session_id = body["id"]
    assert client.get(f"/api/v1/debug-sessions/{session_id}").json()["id"] == session_id

    analysis = client.get(f"/api/v1/debug-sessions/{session_id}/analysis").json()
    assert analysis["status"] in {"COMPLETED", "DEGRADED"}
    assert analysis["degraded"] is True, "the mock provider is declared, not faked"
    assert analysis["degraded_reason"]
    for location in analysis["locations"]:
        #: A location is displayable exactly when it validated.
        assert location["displayable"] == (location["validation"] == "VALID")
        if not location["displayable"]:
            assert location["validation_detail"]

    locations = client.get(f"/api/v1/debug-sessions/{session_id}/locations").json()
    assert isinstance(locations, list)
    valid_only = client.get(
        f"/api/v1/debug-sessions/{session_id}/locations", params={"valid_only": True}
    ).json()
    assert all(row["validation"] == "VALID" for row in valid_only)

    hypotheses = client.get(f"/api/v1/debug-sessions/{session_id}/hypotheses").json()
    assert hypotheses
    assert all(
        row["validation_status"]
        in {"UNVERIFIED", "SUPPORTED", "PARTIALLY_SUPPORTED", "WEAKENED", "REFUTED"}
        for row in hypotheses
    )
    assert hypotheses[0]["supporting_evidence"] or hypotheses[0]["rationale"]

    evidence = client.get(f"/api/v1/debug-sessions/{session_id}/evidence").json()
    assert evidence
    assert all(row["valid"] for row in evidence)

    timeline = client.get(f"/api/v1/debug-sessions/{session_id}/timeline").json()
    kinds = {row["kind"] for row in timeline["items"]}
    assert "INCIDENT_DETECTED" in kinds
    assert "ANALYSIS" in kinds
    assert isinstance(timeline["notes"], list)

    investigation = client.get(
        f"/api/v1/debug-sessions/{session_id}/investigation"
    ).json()
    assert investigation["snapshot_id"] == snapshot["id"]
    assert investigation["evidence"]
    assert investigation["budget"]["max_bytes"] > 0

    sessions = client.get(f"/api/v1/incidents/{incident.id}/debug-sessions").json()
    assert sessions["total"] >= 1


async def test_session_without_a_snapshot_still_answers(client, db_session, tmp_path):
    project_id = await _project(client, "Phase6 Bare")
    environment, component = await build_scope(db_session, uuid.UUID(project_id))
    incident = await build_incident(db_session, _Scoped(project_id), environment, component)
    await db_session.commit()

    created = client.post(
        f"/api/v1/incidents/{incident.id}/debug-sessions",
        params={"project_id": project_id},
        json={"run_analysis": True},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["snapshot_id"] is None
    analysis = body["latest_analysis"]
    assert all(row["validation"] != "VALID" for row in analysis["locations"])
    assert analysis["missing_evidence"], "the absence of a snapshot is stated"
    assert any("snapshot" in item for item in analysis["missing_evidence"])
    #: §43 — the deterministic investigation is still available and still useful.
    assert analysis["summary"]


async def test_debug_session_scope_is_enforced(client, db_session, tmp_path):
    owner, repository, _snapshot, incident = await _session_fixture(
        client, db_session, tmp_path, "Phase6 Sess Owner"
    )
    intruder = await _project(client, "Phase6 Sess Intruder")
    session = client.post(
        f"/api/v1/incidents/{incident.id}/debug-sessions",
        params={"project_id": owner},
        json={"repository_id": repository["id"], "run_analysis": False},
    ).json()

    #: A mutating call without project_id is refused, not defaulted.
    no_scope = client.post(f"/api/v1/debug-sessions/{session['id']}/analyze")
    assert no_scope.status_code == 422
    assert "project_id" in no_scope.json()["detail"]

    cross_scope = client.post(
        f"/api/v1/debug-sessions/{session['id']}/analyze", params={"project_id": intruder}
    )
    assert cross_scope.status_code == 404

    read_cross = client.get(
        f"/api/v1/debug-sessions/{session['id']}", params={"project_id": intruder}
    )
    assert read_cross.status_code == 404

    #: Reads without a scope still work: scope is opt-in enforcement.
    assert client.get(f"/api/v1/debug-sessions/{session['id']}").status_code == 200


async def test_asking_a_question_is_bounded_and_grounded(client, db_session, tmp_path):
    project_id, repository, _snapshot, incident = await _session_fixture(
        client, db_session, tmp_path, "Phase6 Ask"
    )
    session = client.post(
        f"/api/v1/incidents/{incident.id}/debug-sessions",
        params={"project_id": project_id},
        json={"repository_id": repository["id"], "run_analysis": True},
    ).json()

    answer = client.post(
        f"/api/v1/debug-sessions/{session['id']}/messages",
        params={"project_id": project_id},
        json={"question": "Which file should I inspect first?", "asked_by": "engineer"},
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["answer"]
    assert body["invalid_references"] == [] or all(
        item.get("reason") for item in body["invalid_references"]
    )
    assert body["budget"]["max_calls"] >= 1
    assert body["confidence"] in {"INSUFFICIENT", "LOW", "MEDIUM", "HIGH"}

    too_short = client.post(
        f"/api/v1/debug-sessions/{session['id']}/messages",
        params={"project_id": project_id},
        json={"question": "hi"},
    )
    assert too_short.status_code == 422

    missing_scope = client.post(
        f"/api/v1/debug-sessions/{session['id']}/messages", json={"question": "why?"}
    )
    assert missing_scope.status_code == 422

    messages = client.get(f"/api/v1/debug-sessions/{session['id']}/messages").json()
    roles = [row["role"] for row in messages]
    assert "ENGINEER" in roles and "ARGUS" in roles
    assert isinstance(client.get(f"/api/v1/debug-sessions/{session['id']}/tools").json(), list)


async def test_analysis_endpoints_404_for_an_unknown_session(client, tmp_path):
    project_id = await _project(client, "Phase6 Missing")
    unknown = uuid.uuid4()
    for method, path in (
        ("get", f"/api/v1/debug-sessions/{unknown}"),
        ("get", f"/api/v1/debug-sessions/{unknown}/analysis"),
        ("get", f"/api/v1/debug-sessions/{unknown}/locations"),
        ("get", f"/api/v1/debug-sessions/{unknown}/hypotheses"),
        ("get", f"/api/v1/debug-sessions/{unknown}/evidence"),
        ("get", f"/api/v1/debug-sessions/{unknown}/messages"),
        ("get", f"/api/v1/debug-sessions/{unknown}/timeline"),
        ("get", f"/api/v1/debug-sessions/{unknown}/investigation"),
        ("get", f"/api/v1/debug-sessions/{unknown}/tools"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 404, path
    analyze = client.post(
        f"/api/v1/debug-sessions/{unknown}/analyze", params={"project_id": project_id}
    )
    assert analyze.status_code == 404


async def test_analysis_is_404_before_it_has_run(client, db_session, tmp_path):
    project_id, repository, _snapshot, incident = await _session_fixture(
        client, db_session, tmp_path, "Phase6 NoAnalysis"
    )
    session = client.post(
        f"/api/v1/incidents/{incident.id}/debug-sessions",
        params={"project_id": project_id},
        json={"repository_id": repository["id"], "run_analysis": False},
    ).json()
    response = client.get(f"/api/v1/debug-sessions/{session['id']}/analysis")
    assert response.status_code == 404
    assert "no analysis yet" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Trace mapping and metrics
# ---------------------------------------------------------------------------
async def test_incident_code_mappings_report_why_they_are_empty(client, db_session, tmp_path):
    project_id = await _project(client, "Phase6 Mapping")
    environment, component = await build_scope(db_session, uuid.UUID(project_id))
    incident = await build_incident(db_session, _Scoped(project_id), environment, component)
    await db_session.commit()

    response = client.get(f"/api/v1/incidents/{incident.id}/code-mappings")
    assert response.status_code == 200
    assert response.json()["total"] == 0

    refresh = client.get(
        f"/api/v1/incidents/{incident.id}/code-mappings", params={"refresh": True}
    )
    assert refresh.status_code == 422
    assert "no indexed snapshot" in refresh.json()["detail"]


async def test_incident_code_mappings_after_indexing(client, db_session, tmp_path):
    project_id, repository, snapshot, incident = await _session_fixture(
        client, db_session, tmp_path, "Phase6 Mapping2"
    )
    response = client.get(
        f"/api/v1/incidents/{incident.id}/code-mappings", params={"refresh": True}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["snapshot_id"] == snapshot["id"]
    assert body["total"] >= 1
    assert repository["id"]
    for item in body["items"]:
        assert item["mapping_kind"]
        if not item["file_path"]:
            #: An unmapped span states why; it is never silently omitted.
            assert item["unmapped_reason"]


async def test_debugger_metrics_expose_the_weak_spots(client, db_session, tmp_path):
    project_id, repository, _snapshot, incident = await _session_fixture(
        client, db_session, tmp_path, "Phase6 Metrics"
    )
    client.post(
        f"/api/v1/incidents/{incident.id}/debug-sessions",
        params={"project_id": project_id},
        json={"repository_id": repository["id"], "run_analysis": True},
    )

    response = client.get("/api/v1/debugger/metrics", params={"project_id": project_id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sessions"] >= 1
    assert body["analyses"] >= 1
    assert body["analyses_degraded"] >= 1, "the mock provider degrades and that is counted"
    assert body["locations_claimed"] >= body["locations_valid"]
    assert body["engine_version"] == "phase6-debugger-v1"
    assert body["limitations"], "the metrics name what is weak"

    unknown = client.get("/api/v1/debugger/metrics", params={"project_id": uuid.uuid4()})
    assert unknown.status_code == 404


async def test_metrics_work_without_a_project_scope(client):
    response = client.get("/api/v1/debugger/metrics")
    assert response.status_code == 200
    assert "limitations" in response.json()


async def test_openapi_lists_the_phase_6_surface(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/api/v1/projects/{project_id}/repositories",
        "/api/v1/projects/{project_id}/repositories/{repository_id}",
        "/api/v1/projects/{project_id}/repositories/{repository_id}/index",
        "/api/v1/projects/{project_id}/repositories/{repository_id}/snapshots",
        "/api/v1/projects/{project_id}/repositories/{repository_id}/history",
        "/api/v1/projects/{project_id}/repositories/{repository_id}/blame",
        "/api/v1/projects/{project_id}/repositories/{repository_id}/diff",
        "/api/v1/snapshots/{snapshot_id}",
        "/api/v1/snapshots/{snapshot_id}/files",
        "/api/v1/snapshots/{snapshot_id}/symbols",
        "/api/v1/snapshots/{snapshot_id}/symbols/{symbol_id}",
        "/api/v1/snapshots/{snapshot_id}/search",
        "/api/v1/snapshots/{snapshot_id}/risk-signals",
        "/api/v1/incidents/{incident_id}/code-mappings",
        "/api/v1/incidents/{incident_id}/debug-sessions",
        "/api/v1/debug-sessions/{session_id}",
        "/api/v1/debug-sessions/{session_id}/analyze",
        "/api/v1/debug-sessions/{session_id}/analysis",
        "/api/v1/debug-sessions/{session_id}/locations",
        "/api/v1/debug-sessions/{session_id}/hypotheses",
        "/api/v1/debug-sessions/{session_id}/evidence",
        "/api/v1/debug-sessions/{session_id}/messages",
        "/api/v1/debug-sessions/{session_id}/tools",
        "/api/v1/debug-sessions/{session_id}/timeline",
        "/api/v1/debug-sessions/{session_id}/investigation",
        "/api/v1/debugger/metrics",
    ):
        assert path in paths, path
