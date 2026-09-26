# The demo dataset

ARGUS ships with a small, deterministic dataset so a fresh install is
*comprehensible* rather than empty. This page is honest about what it is, what
it is not, and how the automated gates use it.

**It is not a shortcut.** Nothing in the platform is hard-coded to the demo:
every row is produced by the same pipeline your own telemetry goes through. If
the detector regresses, the demo breaks — which is exactly why the gates use it.

---

## What gets seeded

| | |
| --- | --- |
| Project | `ARGUS Demo Commerce` (`argus-demo-commerce`) |
| Environments | Production, Staging, Development |
| Components | Web Frontend, API Gateway, Checkout Service, Inventory Service, Payment Service, PostgreSQL, Redis (with real dependency edges) |
| Telemetry | Metrics, logs, traces and events across the checkout path |
| Graph | Nodes, edges and an initial architecture snapshot |
| Anomaly rules | The rule set that produces the demo scenario |
| Incident | A checkout-latency incident built by the **real** detector and correlator from the telemetry above |

The incident's shape (this is what you will see on the dashboard):

```text
T-2m    deployment published
T-40s   checkout p95 latency      220 ms  →  890 ms
T-35s   inventory dependency      150 ms  →  620 ms
T-30s   checkout error rate       0.8 %   →  7.2 %
T-2m…   traces fail (ERROR/TIMEOUT)
T-25s   checkout health          HEALTHY  →  DEGRADED
T-90s   a configuration change is recorded
```

Timing is anchored to *relative* offsets from the detection instant, so the
scenario reproduces identically whenever it runs, and the ids are fixed so
fingerprints are stable across re-runs.

Note what the deployment is attached as: **temporal context**, with the explicit
wording that it does not establish cause. A deployment shortly before an
incident is a lead, not a verdict — the causal engine is where that distinction
is enforced.

---

## Turning it off (production)

```bash
SEED_DEMO=false        # never seed, in any environment
SEED_DEMO=             # unset: seeded outside production, skipped in production
SEED_DEMO=true         # seed everywhere (including production, deliberately)
```

The seeder runs from the container entrypoint, is idempotent (keyed on the
project slug, so a restart never duplicates), and skips with an explicit log
line when disabled — "the console is empty" should always be explainable.

If you already have the demo project and want it gone:

```bash
curl -sS -X DELETE "http://localhost:8000/api/v1/projects/<project_id>" \
  -H "Authorization: Bearer $TOKEN"
```

Deleting the project cascades its environments, components, telemetry,
anomalies, incidents, graph rows and learned knowledge. Nothing is left orphaned.

---

## The demo source tree

`demo/argus-commerce/` is a real (small) source tree, mounted read-only into the
API container at `/repos/demo-commerce`:

```text
demo/argus-commerce/
├── services/
│   ├── api/
│   ├── checkout/        ← the failing path
│   ├── inventory/       ← the planted defect
│   └── marketing/       ← the counterexample
└── tests/
```

It carries a **planted defect** and a **counterexample**, because investigating a
failure is only meaningful if the answer is discoverable from evidence rather
than from the fixture:

* the defect: `services/inventory/repository.py` guards queries with
  `DB_TIMEOUT_SECONDS`; a later commit halving it makes every query time out,
  and checkout's retry loop amplifies that into a `504`;
* the counterexample: `services/marketing/banners.py` is touched by a later,
  *unrelated* commit. ARGUS must classify it as temporally recent but
  unconnected — a `RELEVANT_CHANGE` ranking of `UNRELATED`/`WEAK` — and must not
  blame it.

`infrastructure/e2e-smoke-phase6.sh` copies the tree into a scratch volume and
builds a genuine two-commit history with real `git`, so code intelligence,
trace-to-code mapping, the debugger and change analysis are all exercised
against real files, real commits and real diffs.

---

## How the verification gates use it

Eleven live gates drive the running stack over HTTP — no mocks, no direct
database writes:

| Gate | What it proves |
| --- | --- |
| `e2e-smoke.sh` | The full lifecycle end to end |
| `e2e-smoke-phase1.sh` … `e2e-smoke-phase11.sh` | Each phase's guarantees, against real telemetry |
| `e2e-smoke-onboarding.sh` | A **new operator's first hour** on a *non-demo* project (see below) |

They run against the compose stack, so they verify the deployed artifact rather
than the test suite's own fixtures. Reproduce them locally:

```bash
DATABASE_PORT=5433 docker compose up --build -d
for gate in infrastructure/e2e-smoke*.sh; do bash "$gate"; done
```

The onboarding gate deliberately refuses to touch the demo project: it creates
its own project, environment, source and ingest token, sends its own telemetry,
and asserts that a stranger can get from zero to a detected anomaly using only
the documented steps. If the docs drift from the product, that gate fails.

---

## Using the demo as a learning surface

Good tours, in order, for a first look:

1. **Dashboard** — projects, active incidents, deployments, event counts.
2. **Incidents → the checkout incident** — evidence, timeline, severity, status.
3. **Root Cause Analysis** — candidates with their confidence buckets and
   *limitations*; notice that the deployment appears as context, not proof.
4. **Failure Reproduction** — the experiment plan, environment, faults, replay
   inputs, comparison and hypothesis verdict.
5. **AI Debugger** — code mappings, the failing path, and the hypotheses it
   forms from stored evidence.
6. **Fix & Verification** — a generated patch, its validation checks, and what
   was verified in isolation.
7. **Reliability** — forecasts with uncertainty bands and the model behind them.
8. **Safe Remediation** — the policy that decides what ARGUS may do at all.
9. **Learning Center** — what ARGUS concluded from the history it has seen.

Each screen states its own confidence and its own gaps; a screen that cannot
substantiate a claim says so instead of showing a confident number.
