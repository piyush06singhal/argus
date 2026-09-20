# ARGUS — Phase 2 Implementation Report: Software Knowledge Graph

**Status: COMPLETE — all increments delivered, all gates green, live end-to-end validated.**

Final gate evidence (this run, live compose stack `DATABASE_PORT=5433`):

| Gate | Command | Result |
| --- | --- | --- |
| Phase 0/1 unchanged | `bash infrastructure/e2e-smoke-phase1.sh` | **46/46** |
| Phase 2 live smoke | `bash infrastructure/e2e-smoke-phase2.sh` | **28/28** |
| Backend suite | `.venv/bin/python -m pytest -q` | **356 passed** |
| Lint / format / types | `ruff check`, `ruff format --check`, `mypy app` | clean (65 source files) |
| Web | `npm run lint`, `npm run build`, `npx vitest run` | clean; **13/13** tests |
| Benchmark | `infrastructure/graph-benchmark.py` | runs at both scales, reports (below) |
| Migrations | `alembic upgrade head` on live DB | head `c8d9e0f1a2b3`, no-op re-run |

## 1. Architecture — graph as an overlay, no duplication

The Software Knowledge Graph is a **derived overlay** over existing Phase 0/1 tables.
`system_components` and `component_dependencies` remain the single source of truth;
graph rows are mirrors plus supplementary entities (owners, endpoints, snapshots,
discovery records). Nothing duplicates a component: the `ComponentRegistry` mirrors
existing `SystemComponent`s into typed nodes (`node_type_for_category` maps the
component category 1:1 onto node types — SERVICE→SERVICE, DATABASE→DATABASE,
CACHE→CACHE, EXTERNAL_API→EXTERNAL_API, FRONTEND→APPLICATION), and a structural
`CONTAINS` hierarchy anchors the topology: `PROJECT → ENVIRONMENT → COMPONENT`.

Key services (`apps/api/app/services/`): `graph_registry` (identity, aliases, typed
mirroring), `graph_reconciler` (run-recorded convergence + stale sweep), `graph_extractor`
(trace/endpoint/log/deployment/repository evidence → edges), `graph_query_service`
(capped traversals), `graph_impact` (dependents closure), `graph_snapshot_service`
(versioned diff), `graph_discovery` (evidence-driven suggestions, terminal
REGISTERED/IGNORED buckets), `graph_validator`, `graph_data_quality` (health roll-up).

## 2. Database

Nine new tables via Alembic `f6d7e8c9b4a2` (with indexes on project/environment/type/
name/status/edge_type/source): `graph_nodes`, `graph_edges`, `graph_snapshots`,
`graph_node_aliases`, `graph_discovery_records`, `service_endpoints`, `component_owners`,
`health_check_events`, `graph_reconciliation_runs`. Nodes carry `node_type`, name,
`external_identifier`, metadata, status; edges carry `edge_type`, `confidence`,
`source` (provenance enum), evidence metadata, `first_seen_at`/`last_seen_at`.

Two follow-up migrations fix latent Phase 0 delete-cascades so project deletion is
total (it previously failed with FK violations on any populated project):
`b7c8d9e0f1a2` (component-scoped references → `ON DELETE CASCADE`) and
`c8d9e0f1a2b3` (environment-scoped references → `ON DELETE CASCADE`).

## 3. Backend behavior highlights (verified by tests + live smoke)

- **Edge semantics are preserved.** `DEPENDS_ON`/`CALLS`/`READS_FROM`/`WRITES_TO` are
  distinct; trace-derived edges use the dependency-carrying types only. Dependency
  traversals, `find_path`, and impact never leak structural `CONTAINS` edges.
- **Impact = dependents closure** (per §32/§75: impact of X = everything downstream
  that depends on X), labeled "Dependency Impact".
- **Paths are project-scoped**: both endpoints are resolved from component/entity ids
  to graph nodes; traversal is bidirectional over dependency edges with
  `max_depth ≤ 25`, `max_nodes ≤ 2000` caps everywhere.
- **Provenance + confidence**: every edge carries `source`; trace-derived edges
  accumulate `metadata.sources` (e.g. `trace:<span-id>`); confidence is evidence
  strength (1.0 configured, 0.9 trace), never a causal probability.
- **Async ingestion hook (§38)**: accepted OTLP/webhook evidence enqueues a
  `graph_extract` job; the worker materializes first-seen `service.name` evidence as
  components (registry discovery role, §12–13), extracts CALLS/READS_FROM edges,
  reconciles, and runs the discovery scan — all idempotent.

Two latent Phase 1 worker bugs were found and fixed during live validation:
`IngestionQueue.pop(timeout=0)` used Redis `BLPOP` with zero timeout (blocks
forever), wedging the worker on any empty queue — now a true non-blocking `LPOP`;
jobs for permanently-missing projects retried forever, starving the queue — now a
`PermanentJobError` dead-letters immediately, and the dead-letter row never violates
its own FK when the referenced project is gone.

## 4. API surface

`/api/v1/graph/**` + `/api/v1/projects/{id}/graph/**` + `/api/v1/components/{id}/…`:
27 route operations across reconciliation, graph retrieval, dependents/dependencies,
paths, impact, environment comparison, snapshots (+ diff), search, endpoints,
owners, discovery records (list/register/ignore), health/data-quality. All are
project-scoped (§57) with cap enforcement.

## 5. Frontend

`/system-map` is a server-rendered shell (`page.tsx`) that preloads the demo
project's graph and emits the **SVG system map in the initial HTML** (deep links and
crawlers get a real render), hydrating `SystemMapClient.tsx` — the SVG explorer with
band layout (`lib/graph-layout.ts`), node/edge inspection, plus Impact,
Environments-compare, Snapshots, and Data-Quality panels. The typed data layer lives
in `lib/graph.ts`; deterministic layout helpers are unit-tested with vitest.

## 6. Performance (informational, synthetic data)

| Scenario | Result |
| --- | --- |
| 100 components / 500 deps — reconcile | 1.2 s |
| 100c graph retrieval / traversal / path / impact | 7.9 ms / 123 ms / 9.2 ms / 123 ms |
| 1000 components / 5000 deps — reconcile | 16.3 s |
| 1000c graph retrieval / traversal / path / impact | 123 ms / 1.85 s / 1.21 s / 4.5 s |

## 7. Demo scenario (§72–75) — proven live by the smoke

Seeded `argus-demo-commerce` (Web Frontend → API Gateway → Checkout → Inventory/
Payment → PostgreSQL/Redis, Staging without Payment, External Payment API via trace
evidence). The 28-check smoke asserts reconcile counts, dependents/dependencies
sets, Web→PostgreSQL path (3 hops), impact closure, Production-vs-Staging diff,
snapshot v2 + diff, search, endpoint templates, health with data-quality rows, edge
provenance, the async worker path (new OTLP pair → TRACE edge), and the web map.

## 8. Security & limits

Secrets are rejected at the API boundary and never enter the queue or the graph;
job payloads carry opaque ids only; graph `metadata_` passes the `RedactionEngine`
before persistence. All traversals enforce `max_depth ≤ 25` / `max_nodes ≤ 2000`;
all queries are project-scoped; late/orphan spans resolve within project scope only.

## 9. Scope discipline (§3)

No anomaly detection, root-cause analysis, causal inference, remediation, or
predictive modeling was implemented. Uncertainty is **displayed, never resolved**:
confidence/provenance are exposed in the API and UI; environment differences are
reported by `last_seen_at` versions, not assumed deployed-state truth. **Phase 3 was
not started.**

## 10. Known limitations

- Environment comparison reflects graph evidence recency, not live deployment state.
- Discovery suggestions require explicit registration (by design — no blind merges).
- Benchmark numbers are synthetic; they demonstrate cap-bounded traversal cost, not
  production-scale claims.
- Dead-letter rows for graph_extract jobs keep dangling project ids only in the
  redacted payload summary (the FK column is nulled), so full id recovery requires
  the summary.
