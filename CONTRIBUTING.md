# Contributing to ARGUS

Thank you for helping make ARGUS better! This guide gets you from clone to
green tests in a few minutes, and explains the project's rules so your PR
merges fast.

## Project overview

ARGUS (Autonomous Software Reliability & Engineering Intelligence) is a
self-hosted platform that ingests your observability data, builds a knowledge
graph of your system, detects anomalies, correlates incidents, performs root
cause analysis, reproduces failures safely, debugs and fixes code with human
approval, forecasts reliability risks, and learns from outcomes — all
deterministic and evidence-grounded.

- Monorepo: `apps/api` (Python/FastAPI), `apps/web` (Next.js),
  `infrastructure/` (Docker Compose, live smoke gates, load tooling).
- Architecture: see `docs/architecture.md` and `docs/unified-reliability-platform.md`.
- Product roadmap: `docs/roadmap.md`.

## Development setup

### Prerequisites

- **Python 3.12** (the supported runtime — CI enforces it)
- **Node.js 20** (LTS)
- **Docker + Docker Compose** (Postgres, Redis, live gates)
- A POSIX shell (macOS/Linux, or Git Bash on Windows)

### Quick start

```bash
git clone https://github.com/piyush06singhal/argus
cd argus

# 1. Backend
python3.12 -m venv apps/api/.venv
source apps/api/.venv/bin/activate
pip install -r apps/api/requirements-dev.txt

# 2. Frontend
cd apps/web && npm ci && cd ../..

# 3. Infrastructure + full stack
cp .env.example .env         # defaults are fine for development
docker compose up -d postgres redis
docker compose up -d api web # runs migrations + seeds demo data on first boot
```

Open http://localhost:3000 (web) and http://localhost:8000/docs (API).

### Environment

`.env.example` documents every variable with comments — required vs optional.
Never commit your `.env`.

## Running the checks (what CI runs)

```bash
# Backend (from apps/api, venv active)
ruff check app tests          # lint
ruff format --check app tests # formatting
mypy app                      # typecheck (224 modules)
python -m pytest tests/ -q    # full suite (SQLite, ~4 min)

# Frontend (from apps/web)
npx tsc --noEmit              # typecheck
npx next lint --dir app       # lint
npm test                      # vitest (231 tests)
npm run build                 # production build
```

### Live end-to-end gates

The `infrastructure/e2e-smoke-phase*.sh` scripts run the real HTTP pipeline
against the Docker Compose stack — this is how phases are verified:

```bash
docker compose up -d --build
bash infrastructure/e2e-smoke-phase1.sh   # ... through phase 11
```

Each gate prints `PASS/FAIL` counts and exits non-zero on failure. CI runs a
subset on PRs to `main`; run them locally before touching engine code.

## Pull request rules

1. **One logical change per PR.** If you fix a bug, the PR contains the fix and
   its regression test — nothing else.
2. **Tests are not optional.** Every bug fix ships with a test that fails
   without the fix. Every feature ships with tests covering the acceptance
   criteria in the issue.
3. **Determinism matters.** ARGUS's core promise is that scores, verdicts and
   policies come from deterministic engines — AI is an optional layer that can
   never override them. Do not introduce code paths where an AI output or a
   random value changes a stored verdict without an explicit, reviewed design
   change.
4. **Honesty over polish.** No fake success states, no swallowed errors, no
   "temporary" demo hacks. If the system can't answer something, it must say so.
5. **Run the checks above before pushing.** CI failing on lint/format is a
   waste of everyone's time.

### Commit messages

Present tense, imperative mood, focused:

```
fix: bound graph traversal depth by configuration, not constant
tests: pin remediation policy-bypass refusal
docs: document the ingestion token rotation endpoint
```

## Issue guidelines

- **Bug reports**: what you did, what you expected, what happened, and the
  relevant log lines or API responses. Include your deployment mode
  (compose/local dev) and versions.
- **Feature ideas**: the problem you're solving first, then the proposed
  solution. Check `docs/roadmap.md` — your idea may already be planned.
- **Security issues**: never in public issues. See `SECURITY.md`.

## Project structure map

```
apps/api/
  app/models/        # SQLAlchemy domain models (one module per phase domain)
  app/schemas/       # Pydantic request/response contracts
  app/services/      # The engines — deterministic business logic lives here
  app/api/v1/routes/ # HTTP surface (thin: auth, validation, delegation)
  app/worker/        # Background worker entrypoint
  alembic/versions/  # Migrations (linear, always upgradable)
  tests/             # ~2,000 tests, mirroring the service layout
apps/web/
  app/               # Next.js App Router pages (one folder per surface)
  lib/               # API client, presentation helpers, tests
infrastructure/
  e2e-smoke-phase*.sh  # Live gates (the real verification)
  load-soak.py         # Load harness
docs/                  # Architecture, phase reports, operations
```

## Licensing

By contributing, you agree that your contributions are licensed under the
MIT License that covers the project.
