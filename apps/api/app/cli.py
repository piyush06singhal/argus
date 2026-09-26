"""ARGUS operator CLI (Hardening W1).

Small, dependency-free administration surface for the things an operator must
be able to do *before* they hold a token — or after they have lost one:

```bash
python -m app.cli bootstrap-token                  # mint/print a root ADMIN token
python -m app.cli create-token --name ci --role OPERATOR --project <uuid>
python -m app.cli list-tokens
python -m app.cli revoke-token <token-uuid>
```

Run it inside the API container:

```bash
docker compose exec api python -m app.cli bootstrap-token
```

``create-token`` and ``bootstrap-token`` print the raw secret once, to stdout.
Everything else prints metadata. Nothing is ever printed twice — that is the
whole point of hash-only storage.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.database import async_session_factory, init_db
from app.models.auth import ApiToken, ApiTokenProject, TokenRole


def _parse_expiry(days: int | None) -> datetime | None:
    if not days:
        return None
    return datetime.now(timezone.utc) + timedelta(days=days)


async def _summary(db) -> list[str]:
    rows = (
        (await db.execute(select(ApiToken).order_by(ApiToken.created_at.desc())))
        .scalars()
        .all()
    )
    lines = []
    for token in rows:
        grants = (
            await db.execute(
                select(ApiTokenProject.project_id).where(
                    ApiTokenProject.token_id == token.id
                )
            )
        ).scalars()
        grant_list = ", ".join(str(g) for g in grants.all()) or "-"
        lines.append(
            f"{token.id}  {token.name[:28]:<28}  {token.role.value:<8}  "
            f"{token.status.value:<7}  last_used="
            f"{token.last_used_at.isoformat() if token.last_used_at else 'never':<32}  "
            f"projects={grant_list}"
        )
    return lines


async def _run(args: argparse.Namespace) -> int:
    from app.core.security import bootstrap_admin_token, create_token, revoke_token

    await init_db()
    async with async_session_factory() as db:
        if args.command == "bootstrap-token":
            raw = await bootstrap_admin_token(db)
            if raw is None:
                # No raw secret can be recovered from a hash. Minting a new
                # admin token is the documented recovery path, and it is
                # audited like any other creation.
                _recovery, raw = await create_token(
                    db,
                    name="admin-recovery",
                    role=TokenRole.ADMIN,
                    created_by="cli",
                    description="Recovery token minted via CLI",
                )
                print(
                    "An active ADMIN token already existed; a new recovery token "
                    "was minted instead (the old one still works until revoked).",
                    file=sys.stderr,
                )
            assert raw is not None
            print(raw)
            return 0

        if args.command == "create-token":
            projects = [uuid.UUID(p) for p in (args.project or [])]
            if args.role == "ADMIN" and projects:
                print("ADMIN tokens are unscoped; drop --project", file=sys.stderr)
                return 2
            token, raw = await create_token(
                db,
                name=args.name,
                role=TokenRole(args.role),
                expires_at=_parse_expiry(args.expires_in_days),
                project_ids=projects,
                created_by="cli",
                description=args.description,
            )
            print(f"id: {token.id}\nrole: {token.role.value}")
            print(raw)
            return 0

        if args.command == "list-tokens":
            for line in await _summary(db):
                print(line)
            return 0

        if args.command == "revoke-token":
            revoked = await revoke_token(db, uuid.UUID(args.token_id))
            if revoked is None:
                print("Token not found", file=sys.stderr)
                return 1
            print(
                f"revoked {revoked.id} ({revoked.name}) — status={revoked.status.value}"
            )
            return 0

    return 2


async def _run_worker() -> int:
    """Run the background half of ARGUS in its own process.

    The recommended production topology is one container per role: HTTP
    processes with ``BACKGROUND_JOBS_ENABLED=false``, and exactly one worker
    process running this command. Without this command there was no way to
    express that — the only way to run the worker was to also serve traffic.

    It refuses to start when ``BACKGROUND_JOBS_ENABLED=false``: an operator who
    set that flag on the *container that is supposed to work* has a
    misconfiguration, and a worker that silently exits zero would look healthy
    while nothing drained the queue.
    """
    import asyncio as _asyncio
    import logging
    import signal

    from app.core.config import get_settings

    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, (settings.LOG_LEVEL or "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("argus.worker")

    if not settings.BACKGROUND_JOBS_ENABLED:
        print(
            "BACKGROUND_JOBS_ENABLED=false: this process is configured as an "
            "HTTP process and will not run the worker. Set it to true on the "
            "container that should do background work.",
            file=sys.stderr,
        )
        return 2
    if settings.is_testing:
        print("API_ENVIRONMENT=test never runs background work.", file=sys.stderr)
        return 2

    await init_db()

    from app.services.anomaly_sweep import sweep_forever as sweep_anomalies
    from app.services.code_sweep import sweep_code_intelligence_forever
    from app.services.fix_sweep import sweep_fix_forever
    from app.services.intelligence_sweep import sweep_forever as sweep_learning
    from app.services.platform_sweep import sweep_forever as sweep_platform
    from app.services.reliability_sweep import sweep_reliability_forever
    from app.services.remediation_sweep import sweep_remediations_forever
    from app.services.reproduction_sweep import sweep_reproductions_forever
    from app.services.worker_runner import make_worker

    tasks: list[_asyncio.Task] = []
    worker = None
    if settings.INGESTION_WORKER_ENABLED:
        worker = make_worker(async_session_factory)
        tasks.append(_asyncio.create_task(worker.run_forever(), name="ingest-worker"))
    if settings.ANOMALY_DETECTION_ENABLED and settings.ANOMALY_SWEEP_ENABLED:
        tasks.append(
            _asyncio.create_task(sweep_anomalies(async_session_factory), name="detect")
        )
    if settings.REPRO_SWEEP_ENABLED:
        tasks.append(
            _asyncio.create_task(
                sweep_reproductions_forever(async_session_factory), name="repro"
            )
        )
    if settings.CODE_SWEEP_ENABLED:
        tasks.append(
            _asyncio.create_task(
                sweep_code_intelligence_forever(async_session_factory), name="code"
            )
        )
    if settings.FIX_SWEEP_ENABLED:
        tasks.append(
            _asyncio.create_task(sweep_fix_forever(async_session_factory), name="fix")
        )
    if settings.RELIABILITY_FORECASTING_ENABLED and settings.RELIABILITY_SWEEP_ENABLED:
        tasks.append(
            _asyncio.create_task(
                sweep_reliability_forever(async_session_factory), name="forecast"
            )
        )
    if settings.REMEDIATION_EXECUTION_ENABLED and settings.REMEDIATION_SWEEP_ENABLED:
        tasks.append(
            _asyncio.create_task(
                sweep_remediations_forever(async_session_factory), name="remediation"
            )
        )
    if settings.INTELLIGENCE_SWEEP_ENABLED:
        tasks.append(
            _asyncio.create_task(sweep_learning(async_session_factory), name="learning")
        )
    if settings.PLATFORM_SWEEP_ENABLED:
        tasks.append(
            _asyncio.create_task(sweep_platform(async_session_factory), name="platform")
        )

    logger.info(
        "worker process started: %d background tasks (%s)",
        len(tasks),
        ", ".join(task.get_name() for task in tasks),
    )

    stop = _asyncio.Event()
    loop = _asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            signal.signal(sig, lambda *_: stop.set())

    try:
        await stop.wait()
    finally:
        logger.info("worker process shutting down")
        if worker is not None:
            worker.stop()
        for task in tasks:
            task.cancel()
        await _asyncio.gather(*tasks, return_exceptions=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "bootstrap-token",
        help="print a root ADMIN token (mints one if none is active)",
    )

    create = sub.add_parser("create-token", help="mint a scoped token")
    create.add_argument("--name", required=True)
    create.add_argument(
        "--role", default="OPERATOR", choices=[r.value for r in TokenRole]
    )
    create.add_argument("--expires-in-days", type=int, default=None)
    create.add_argument(
        "--project",
        action="append",
        help="project UUID grant (repeatable; required for scoped tokens)",
    )
    create.add_argument("--description", default=None)

    sub.add_parser("list-tokens", help="list token metadata (never secrets)")

    revoke = sub.add_parser("revoke-token", help="revoke a token by id")
    revoke.add_argument("token_id")

    sub.add_parser(
        "worker",
        help=(
            "run the ingestion worker and every scheduled sweep without "
            "serving HTTP (the worker half of a split topology)"
        ),
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "worker":
        return asyncio.run(_run_worker())
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
