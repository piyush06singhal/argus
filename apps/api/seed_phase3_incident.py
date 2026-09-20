"""Run the Phase 3 demo incident against the seeded demo project.

Use this to (re)generate the deterministic ``ARGUS Checkout Latency Incident``
on an environment that is already seeded — for example after upgrading an
existing database, or to re-anchor the scenario to "now" for a live demo:

    python seed_phase3_incident.py                # create if absent
    python seed_phase3_incident.py --force         # re-ingest telemetry + re-detect

It is intentionally *not* a fixture loader: it ingests telemetry and runs the
real detection and correlation pipeline, so the incident it produces is the same
one the product would produce from real data.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory, init_db
from app.models.project import Environment, SoftwareProject
from app.models.system import SystemComponent
from app.services.demo_incident import seed_checkout_incident

DEMO_SLUG = "argus-demo-commerce"

#: Component names in the seeded demo topology, mapped to the demo's keys.
_COMPONENT_KEYS = {
    "checkout_service": "Checkout Service",
    "inventory_service": "Inventory Service",
    "api_gateway": "API Gateway",
    "payment_service": "Payment Service",
    "redis": "Redis",
    # Phase 4 §48: the datastore at the root of the causal chain must be in
    # scope, or the demo cannot exercise datastore-rooted analysis.
    "postgresql": "PostgreSQL",
}


async def _load_demo_scope(db: AsyncSession):
    project = (
        await db.execute(
            select(SoftwareProject).where(SoftwareProject.slug == DEMO_SLUG)
        )
    ).scalar_one_or_none()
    if project is None:
        raise SystemExit(
            f"Demo project '{DEMO_SLUG}' not found. Run `python seed_data.py` first."
        )

    environments = list(
        (
            await db.execute(
                select(Environment).where(Environment.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    production = next(
        (
            env
            for env in environments
            if (env.environment_type or "").upper().endswith("PRODUCTION")
            or str(env.environment_type) == "PRODUCTION"
        ),
        environments[0] if environments else None,
    )
    if production is None:
        raise SystemExit(
            "The demo project has no environment to attach an incident to."
        )

    # Scoped to the chosen environment: the demo topology mirrors component
    # names across environments ("Checkout Service" exists in production *and*
    # as a staging mirror), so a name-only lookup silently binds the production
    # incident to a staging component.
    components = {
        comp.name: comp
        for comp in (
            await db.execute(
                select(SystemComponent).where(
                    SystemComponent.project_id == project.id,
                    SystemComponent.environment_id == production.id,
                )
            )
        )
        .scalars()
        .all()
    }
    resolved: dict[str, SystemComponent] = {}
    for key, name in _COMPONENT_KEYS.items():
        component = components.get(name)
        if component is None:
            raise SystemExit(
                f"Demo component '{name}' is missing in {production.name}; "
                "re-run `python seed_data.py`."
            )
        resolved[key] = component
    return project, production, resolved


async def main(force: bool) -> None:
    await init_db()
    async with async_session_factory() as db:
        project, environment, components = await _load_demo_scope(db)
        result = await seed_checkout_incident(
            db,
            project=project,
            environment=environment,
            components=components,
            force=force,
        )
        await db.commit()
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-ingest telemetry and re-run detection even if the demo exists",
    )
    args = parser.parse_args()
    asyncio.run(main(args.force))
