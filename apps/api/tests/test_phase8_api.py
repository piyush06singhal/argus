"""Phase 8 — the predictive reliability HTTP surface (§44, §60, §71).

These tests drive the real routes over an in-process ASGI transport against the
same database the fixtures write to, so what they verify is the request path a
dashboard takes — not a service call standing in for it.

Two properties get most of the attention here:

* **Scope is proven, not trusted.** Every read takes a ``project_id`` and a
  foreign id answers 404, so the tests assert that a UUID is *not* authority
  (§60).
* **Shape claims are honest.** A sample-less metric must be absent rather than
  zero; an empty heatmap must say why; a model registry must not leak customer
  evidence.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reliability import ReliabilityForecast

from app.core.database import get_db
from app.main import app
from tests.phase6_helpers import build_project
from tests.phase8_helpers import (
    degradation_timeline,
    emit_metric_series,
    recovery_timeline,
    utcnow,
)


@pytest_asyncio.fixture
async def api(db_session: AsyncSession):
    """An HTTP client bound to the test session, so seeded rows are visible."""

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://argus.test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


async def _generate(api: httpx.AsyncClient, project_id, horizons=None):
    return await api.post(
        "/api/v1/reliability/forecasts/generate",
        params={"project_id": str(project_id)},
        json={
            "dispatch": False,
            "prediction_types": ["FAILURE_RISK"],
            "horizons": horizons or ["ONE_HOUR"],
        },
    )


# ---------------------------------------------------------------------------
# Generation and the forecast surface
# ---------------------------------------------------------------------------


async def test_generate_stores_forecasts_with_signals_and_snapshot(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """A degrading component produces an auditable forecast, end to end."""
    project, environment, component = await build_project(db_session)
    await degradation_timeline(
        db_session, project, environment, component, onset=utcnow()
    )
    await db_session.commit()

    response = await _generate(api, project.id)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["dispatched"] is False
    assert payload["scopes"] >= 1
    assert payload["forecasts_created"] >= 1
    assert payload["errors"] == []

    listing = await api.get(
        "/api/v1/reliability/forecasts", params={"project_id": str(project.id)}
    )
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert items

    forecast = items[0]
    # Every forecast states its own provenance and limits (§1, §82).
    for key in (
        "prediction_type",
        "forecast_horizon",
        "generated_at",
        "valid_until",
        "risk_level",
        "model_version_label",
        "data_quality",
        "feature_snapshot_id",
        "headline",
        "limitations",
        "fingerprint",
    ):
        assert key in forecast

    detail = await api.get(f"/api/v1/reliability/forecasts/{forecast['id']}")
    assert detail.status_code == 200

    signals = await api.get(f"/api/v1/reliability/forecasts/{forecast['id']}/signals")
    assert signals.status_code == 200
    signal_rows = signals.json()
    assert signal_rows, "a stored forecast must carry the signals behind it"
    for signal in signal_rows:
        # A signal is predictive evidence, never a claimed cause (§6).
        assert signal["description"]
        assert "caused" not in signal["description"].lower()

    snapshot = await api.get(f"/api/v1/reliability/forecasts/{forecast['id']}/snapshot")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["feature_values"], "the feature snapshot must be reproducible"
    assert body["feature_window_start"] <= body["feature_window_end"]
    assert body["feature_schema_version"]


async def test_explanation_answers_the_four_required_questions(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """§34: what changed, why, what supports it, what is uncertain."""
    project, environment, component = await build_project(db_session)
    await degradation_timeline(
        db_session, project, environment, component, onset=utcnow()
    )
    await db_session.commit()
    await _generate(api, project.id)

    forecast_id = (
        await api.get(
            "/api/v1/reliability/forecasts", params={"project_id": str(project.id)}
        )
    ).json()["items"][0]["id"]

    response = await api.get(f"/api/v1/reliability/forecasts/{forecast_id}/explanation")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["headline"]
    assert isinstance(body["what_changed"], list)
    assert isinstance(body["why_risk_increased"], list)
    assert "signals" in body["what_supports_this"]
    assert isinstance(body["what_is_uncertain"], list)
    assert body["caveats"], "an explanation must state what it is not"
    assert any("not causal" in caveat for caveat in body["caveats"])
    assert "horizon_label" in body
    # The narrative layer is optional and off by default (§34): the API is
    # deterministic unless a provider is configured, and it says so rather than
    # leaving a reader to guess whether a sentence came from a model.
    assert body["ai_narrative"] is None
    assert body["ai_narrative_provider"] == "none"
    assert body["ai_narrative_degraded"] is False


# ---------------------------------------------------------------------------
# No-data honesty (§69) and the false-positive path (§68)
# ---------------------------------------------------------------------------


async def test_component_without_history_reports_unknown_not_low(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Insufficient evidence must read as UNKNOWN, never as LOW RISK (§69)."""
    project, environment, component = await build_project(db_session)
    # A single sample: present enough to be eligible, far from enough to judge.
    await emit_metric_series(
        db_session,
        project,
        environment,
        component,
        metric_name="http.checkout.latency.p95",
        values=[420.0],
        end=utcnow(),
    )
    await db_session.commit()

    response = await _generate(api, project.id)
    assert response.status_code == 200, response.text
    listing = await api.get(
        "/api/v1/reliability/forecasts", params={"project_id": str(project.id)}
    )
    items = listing.json()["items"]
    if not items:
        # Refusing to forecast at all is also correct; what is not allowed is a
        # confident LOW from one sample.
        assert response.json()["skipped"] or response.json()["forecasts_created"] == 0
        return
    assert items[0]["risk_level"] == "UNKNOWN"
    assert items[0]["failure_reason"] is not None
    assert items[0]["data_quality"] in {"POOR", "INSUFFICIENT", "PARTIAL"}


async def test_elevated_risk_after_recovery_never_reads_as_confirmed_failure(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """§68: a noisy window that recovers is not retro-labelled a failure."""
    project, environment, component = await build_project(db_session)
    await recovery_timeline(db_session, project, environment, component, end=utcnow())
    await db_session.commit()
    await _generate(api, project.id)

    items = (
        await api.get(
            "/api/v1/reliability/forecasts", params={"project_id": str(project.id)}
        )
    ).json()["items"]
    for forecast in items:
        assert forecast["status"] in {"GENERATED", "ACTIVE", "EXPIRED"}
        # Nothing here opens an incident, and nothing claims resolved failure.
        assert "confirmed" not in forecast["headline"].lower()
        assert "failed" not in forecast["headline"].lower()

    outcome = await api.get(f"/api/v1/reliability/forecasts/{items[0]['id']}/outcome")
    assert outcome.status_code == 200
    # No horizon has elapsed yet, so there is no outcome — not a null outcome.
    assert outcome.json() is None


# ---------------------------------------------------------------------------
# Heatmap, profile, health (§43, §46, §47, §52)
# ---------------------------------------------------------------------------


async def test_heatmap_only_shows_evidence_that_exists(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """A cell exists only where a forecast exists; empty says so explicitly."""
    project, environment, component = await build_project(db_session)
    empty = await api.get(
        "/api/v1/reliability/heatmap", params={"project_id": str(project.id)}
    )
    assert empty.status_code == 200
    assert empty.json()["cells"] == []
    assert empty.json()["empty_reason"]

    await degradation_timeline(
        db_session, project, environment, component, onset=utcnow()
    )
    await db_session.commit()
    await _generate(api, project.id, horizons=["ONE_HOUR", "SIX_HOURS"])

    response = await api.get(
        "/api/v1/reliability/heatmap", params={"project_id": str(project.id)}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cells"]
    cell = body["cells"][0]
    assert cell["worst_level"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN"}
    assert cell["evidence_count"] >= 1
    assert set(cell["by_horizon"]) <= {"ONE_HOUR", "SIX_HOURS"}
    assert body["horizons"]


async def test_heatmap_never_emits_a_nameless_cell(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """A component-scoped forecast that outlives its component is not a cell.

    Forecasts keep their history when a component is deleted (``ON DELETE SET
    NULL``), which is deliberate — but generation only ever produces
    *component-scoped* forecasts, so a NULL component reference can only mean
    "the component is gone". Presenting that as a cell would render a nameless
    column that reads like a project-wide claim. The row must stay retrievable
    while the heatmap declines to place it (§47).
    """
    project, environment, component = await build_project(db_session)
    await degradation_timeline(
        db_session, project, environment, component, onset=utcnow()
    )
    await db_session.commit()
    await _generate(api, project.id, horizons=["ONE_HOUR"])

    before = await api.get(
        "/api/v1/reliability/heatmap", params={"project_id": str(project.id)}
    )
    assert before.status_code == 200
    assert before.json()["cells"]
    forecast_id = before.json()["cells"][0]["component_id"]
    assert forecast_id is not None

    # Simulate the component being deleted: the forecast row survives, its
    # component reference does not. Done through the ORM so the UUID binds
    # exactly as the application binds it, whichever engine is under test.
    await db_session.execute(
        update(ReliabilityForecast)
        .where(ReliabilityForecast.project_id == project.id)
        .values(component_id=None)
    )
    await db_session.commit()

    after = await api.get(
        "/api/v1/reliability/heatmap", params={"project_id": str(project.id)}
    )
    assert after.status_code == 200
    body = after.json()
    assert all(cell["component_id"] is not None for cell in body["cells"])
    assert all(cell["component_name"] for cell in body["cells"])
    # The forecast itself is not erased — it is simply no longer a grid cell.
    assert body["empty_reason"]
    listed = await api.get(
        "/api/v1/reliability/forecasts", params={"project_id": str(project.id)}
    )
    assert listed.status_code == 200
    assert listed.json()["items"]


async def test_component_profile_is_a_read_only_projection(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    project, environment, component = await build_project(db_session)
    await degradation_timeline(
        db_session, project, environment, component, onset=utcnow()
    )
    await db_session.commit()
    await _generate(api, project.id)

    before = (
        await api.get(
            "/api/v1/reliability/forecasts",
            params={"project_id": str(project.id), "active_only": True},
        )
    ).json()["total"]

    response = await api.get(
        f"/api/v1/reliability/components/{component.id}/profile",
        params={"project_id": str(project.id)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["component_id"] == str(component.id)
    assert body["worst_risk"]
    assert "score" in body["reliability_score"]
    assert body["limitations"], "the profile must state the score's limits"

    after = (
        await api.get(
            "/api/v1/reliability/forecasts",
            params={"project_id": str(project.id), "active_only": True},
        )
    ).json()["total"]
    assert before == after, "opening a profile must not change what ARGUS believes"


async def test_health_reports_measured_metrics_with_their_sample_sizes(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    project, environment, component = await build_project(db_session)
    await degradation_timeline(
        db_session, project, environment, component, onset=utcnow()
    )
    await db_session.commit()
    await _generate(api, project.id)

    response = await api.get(
        "/api/v1/reliability/health", params={"project_id": str(project.id)}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["forecast_count"] >= 1
    assert "sample_count" in body["accuracy"]
    assert body["thresholds"], "thresholds must be configurable and published"
    assert body["limits"]
    assert "policy" in body["drift"]
    # The scope boundary is part of the payload, not just the docs (§87).
    assert any("does not" in note for note in body["limitations"])


# ---------------------------------------------------------------------------
# Evaluation and backtesting (§29, §30, §55)
# ---------------------------------------------------------------------------


async def test_evaluation_refuses_metrics_below_the_sample_floor(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    project, environment, component = await build_project(db_session)
    await degradation_timeline(
        db_session, project, environment, component, onset=utcnow()
    )
    await db_session.commit()
    await _generate(api, project.id)

    response = await api.post(
        "/api/v1/reliability/evaluate",
        params={"project_id": str(project.id), "window_days": 30, "dispatch": False},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Nothing has elapsed, so there is nothing scored — and the run says so.
    assert body["sample_count"] == 0
    assert body["metrics"].get("precision") in (None, 0)
    assert body["notes"], "a metric-less run must explain itself"

    history = await api.get(
        "/api/v1/reliability/evaluations", params={"project_id": str(project.id)}
    )
    assert history.status_code == 200
    assert history.json()["items"]


async def test_backtest_runs_and_records_its_steps(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    project, environment, component = await build_project(db_session)
    now = utcnow()
    # A degradation that *ended* in the past, so the walk-forward loop has both
    # a training window and a label window to score against.
    history = await degradation_timeline(
        db_session, project, environment, component, onset=now - timedelta(hours=6)
    )
    assert history["latency"][-1] > history["latency"][0]
    await db_session.commit()

    response = await api.post(
        "/api/v1/reliability/backtests",
        params={"project_id": str(project.id)},
        json={
            "start_time": (now - timedelta(hours=20)).isoformat(),
            "end_time": (now - timedelta(hours=1)).isoformat(),
            "training_window_seconds": 7200,
            "forecast_horizon": "ONE_HOUR",
            "prediction_type": "FAILURE_RISK",
            "step_seconds": 1800,
            "max_steps": 10,
            "component_ids": [str(component.id)],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["note"]
    backtest = body["items"][0]
    assert backtest["status"] in {"COMPLETED", "FAILED", "INCONCLUSIVE"}
    assert backtest["sample_count"] >= 0
    assert isinstance(backtest["steps"], list)

    detail = await api.get(
        f"/api/v1/reliability/backtests/{backtest['id']}",
        params={"project_id": str(project.id)},
    )
    assert detail.status_code == 200
    assert detail.json()["configuration"]["step_seconds"] == 1800

    listing = await api.get(
        "/api/v1/reliability/backtests", params={"project_id": str(project.id)}
    )
    assert listing.status_code == 200
    assert listing.json()["total"] == 1


async def test_backtest_rejects_a_window_that_cannot_be_scored(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    project, environment, component = await build_project(db_session)
    await db_session.commit()
    now = utcnow()
    response = await api.post(
        "/api/v1/reliability/backtests",
        params={"project_id": str(project.id)},
        json={
            "start_time": now.isoformat(),
            "end_time": (now - timedelta(hours=1)).isoformat(),
            "training_window_seconds": 3600,
        },
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Warnings, drift, models (§39, §41, §54)
# ---------------------------------------------------------------------------


async def test_warning_review_is_human_only(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """A warning can be acknowledged or dismissed — and nothing else (§8)."""
    from app.models.reliability import (
        EarlyWarningStatus,
        ForecastRiskLevel,
        ReliabilityEarlyWarning,
    )

    project, environment, component = await build_project(db_session)
    now = utcnow()
    warning = ReliabilityEarlyWarning(
        project_id=project.id,
        environment_id=environment.id,
        component_id=component.id,
        fingerprint=uuid.uuid4().hex,
        title="Checkout Service: Elevated 6-hour reliability risk",
        description="predicted risk over the next 6 hours",
        severity=ForecastRiskLevel.HIGH,
        status=EarlyWarningStatus.OPEN,
        occurrence_count=1,
        first_raised_at=now,
        last_raised_at=now,
    )
    db_session.add(warning)
    await db_session.commit()

    listing = await api.get(
        "/api/v1/reliability/warnings", params={"project_id": str(project.id)}
    )
    assert listing.status_code == 200
    assert listing.json()["total"] == 1

    ack = await api.post(
        f"/api/v1/reliability/warnings/{warning.id}/acknowledge",
        params={"project_id": str(project.id)},
        json={"actor": "oncall@argus", "reason": None},
    )
    assert ack.status_code == 200, ack.text
    assert ack.json()["status"] == "ACKNOWLEDGED"
    assert ack.json()["acknowledged_by"] == "oncall@argus"

    # The warn endpoint has no remediation counterpart: the phase stops at a
    # human deciding (§87).
    assert (
        await api.post(
            f"/api/v1/reliability/warnings/{warning.id}/rollback",
            params={"project_id": str(project.id)},
            json={},
        )
    ).status_code == 404


async def test_drift_reads_findings_and_assessment_needs_a_post(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Drift is observable from a GET; assessing it is an explicit POST."""
    project, environment, component = await build_project(db_session)
    now = utcnow()
    # Reference window: calm. Current window: a different distribution.
    await emit_metric_series(
        db_session,
        project,
        environment,
        component,
        metric_name="http.inventory.latency.p95",
        values=[100.0 + (index % 3) for index in range(30)],
        end=now - timedelta(hours=2),
        step_seconds=3600,
    )
    await emit_metric_series(
        db_session,
        project,
        environment,
        component,
        metric_name="http.inventory.latency.p95",
        values=[900.0 + (index % 3) for index in range(20)],
        end=now,
        step_seconds=600,
    )
    await db_session.commit()

    before = await api.get(
        "/api/v1/reliability/drift", params={"project_id": str(project.id)}
    )
    assert before.status_code == 200
    assert before.json()["items"] == []

    assessed = await api.post(
        "/api/v1/reliability/drift/assess",
        params={"project_id": str(project.id), "persist": True},
    )
    assert assessed.status_code == 200, assessed.text
    body = assessed.json()
    assert body["worst_status"] in {"STABLE", "WATCH", "FLAGGED"}
    assert body["review_policy"]
    assert body["retrain_performed"] is False
    assert body["model_activated"] is False

    after = await api.get(
        "/api/v1/reliability/drift", params={"project_id": str(project.id)}
    )
    assert after.status_code == 200
    assert after.json()["summary"]["total"] >= len(body["findings"])


async def test_model_registry_exposes_versions_without_customer_evidence(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    project, environment, component = await build_project(db_session)
    await degradation_timeline(
        db_session, project, environment, component, onset=utcnow()
    )
    await db_session.commit()
    await _generate(api, project.id)

    response = await api.get("/api/v1/reliability/models")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"], "generating a forecast must register its model version"
    model = body["items"][0]
    for key in (
        "model_name",
        "model_type",
        "version",
        "feature_schema_version",
        "parameters",
        "status",
        "calibration_status",
        "sample_count",
    ):
        assert key in model
    # The registry is global but must not carry tenant data.
    assert "project_id" not in model
    assert "component_id" not in model

    detail = await api.get(f"/api/v1/reliability/models/{model['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == model["id"]


# ---------------------------------------------------------------------------
# Isolation (§60, §71)
# ---------------------------------------------------------------------------


async def test_cross_project_access_is_invisible_not_forbidden(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """An out-of-scope id answers 404 so existence is never confirmed (§60)."""
    owner, owner_env, owner_component = await build_project(
        db_session, name="owner-project"
    )
    other, other_env, other_component = await build_project(
        db_session, name="other-project"
    )
    await degradation_timeline(
        db_session, owner, owner_env, owner_component, onset=utcnow()
    )
    await db_session.commit()
    await _generate(api, owner.id)

    forecast_id = (
        await api.get(
            "/api/v1/reliability/forecasts", params={"project_id": str(owner.id)}
        )
    ).json()["items"][0]["id"]

    # Reads scoped to the wrong project must not resolve.
    assert (
        await api.get(
            f"/api/v1/reliability/forecasts/{forecast_id}",
            params={"project_id": str(other.id)},
        )
    ).status_code == 404
    assert (
        await api.get(
            f"/api/v1/reliability/forecasts/{forecast_id}/explanation",
            params={"project_id": str(other.id)},
        )
    ).status_code == 404
    assert (
        await api.get(
            "/api/v1/reliability/forecasts",
            params={"project_id": str(other.id)},
        )
    ).json()["items"] == []

    # A component owned by another project is not addressable (§60).
    assert (
        await api.get(
            f"/api/v1/reliability/components/{owner_component.id}/profile",
            params={"project_id": str(other.id)},
        )
    ).status_code == 404
    assert (
        await api.get(
            f"/api/v1/reliability/components/{owner_component.id}/forecasts",
            params={"project_id": str(other.id)},
        )
    ).status_code == 404

    # Mutating requests must also refuse a foreign component.
    refused = await api.post(
        "/api/v1/reliability/backtests",
        params={"project_id": str(other.id)},
        json={
            "start_time": (utcnow() - timedelta(hours=8)).isoformat(),
            "end_time": (utcnow() - timedelta(hours=1)).isoformat(),
            "training_window_seconds": 3600,
            "component_ids": [str(owner_component.id)],
        },
    )
    assert refused.status_code == 404

    # And an unknown project is simply not found.
    assert (
        await api.get(
            "/api/v1/reliability/heatmap", params={"project_id": str(uuid.uuid4())}
        )
    ).status_code == 404


async def test_generate_refuses_an_unknown_project_and_environment(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    project, environment, component = await build_project(db_session)
    await db_session.commit()

    unknown_project = await api.post(
        "/api/v1/reliability/forecasts/generate",
        params={"project_id": str(uuid.uuid4())},
        json={"dispatch": False},
    )
    assert unknown_project.status_code == 404

    foreign_environment = await api.post(
        "/api/v1/reliability/forecasts/generate",
        params={"project_id": str(project.id)},
        json={"dispatch": False, "environment_id": str(uuid.uuid4())},
    )
    assert foreign_environment.status_code == 404


async def test_dispatch_reports_queue_state_without_pretending(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Async dispatch must answer honestly when no broker is reachable."""
    project, environment, component = await build_project(db_session)
    await db_session.commit()

    response = await api.post(
        "/api/v1/reliability/forecasts/generate",
        params={"project_id": str(project.id)},
        json={"dispatch": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dispatched"] in (True, False)
    assert body["message"]
    if not body["dispatched"]:
        # The failure is explained, not swallowed, and offers the synchronous path.
        assert "dispatch=false" in body["message"]


async def test_requests_reject_unknown_fields(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """The schemas forbid extra fields, so a typo cannot be silently ignored."""
    project, environment, component = await build_project(db_session)
    await db_session.commit()
    response = await api.post(
        "/api/v1/reliability/forecasts/generate",
        params={"project_id": str(project.id)},
        json={"dispatch": False, "auto_remediate": True},
    )
    assert response.status_code == 422
