# Software Knowledge Graph

Phase 2 gives ARGUS a queryable **Software Knowledge Graph**: a structural model of the software producing the observability evidence collected in Phase 1. ARGUS does not merely *see* telemetry anymore — it understands the *structure* of the system that emits it.

This document covers the architecture, data model, semantics, and limitations. It describes a **deterministic structural model only** — no anomaly detection, root-cause analysis, or causal reasoning is implemented. Those belong to Phases 3+.

---

## 1. Architecture: a materialized overlay

The graph is **not** a graph database. PostgreSQL remains the single source of truth. `graph_nodes` and `graph_edges` form a *materialized overlay* that mirrors canonical entities:

```text
system_components ──┐
environments ───────┤       graph_nodes  (entity_kind + entity_id linkage)
projects ───────────┤  ───▶ graph_edges  (typed, provenance-carrying)
code_repositories ──┘
component_dependencies ──▶ graph_edges (DEPENDS_ON, source=CONFIGURATION)
spans / trace events ────▶ graph_edges (CALLS / READS_FROM, source=TRACE)
deployment_events ───────▶ graph_edges (DEPLOYED_AS, source=DEPLOYMENT)
```

Design rules:

- **No duplication** — a graph node *is* a canonical entity (via `entity_kind`/`entity_id` unique constraint). Nothing is copied into a parallel representation.
- **Configured edges win** — explicit configuration (`DEPENDS_ON` from `component_dependencies`) is never overwritten by telemetry; evidence is appended into `metadata.sources[]`.
- **Reconciliation never deletes** — relationships that disappear from one telemetry sample are marked `STALE`, never removed (historical architecture is preserved).
- **Redaction before persistence** — all `metadata_` payloads pass through the Phase 1 `RedactionEngine`; no tokens, passwords, or API keys reach graph tables.

---

## 2. Node types

Nodes are typed (§7 of the Phase 2 contract). Canonical components project their category onto the graph:

| GraphNodeType | Comes from |
|---------------|------------|
| `PROJECT` | the software project anchor |
| `ENVIRONMENT` | each environment (prod/staging/…) |
| `APPLICATION` | components of category `APPLICATION` or `FRONTEND` |
| `SERVICE` / `WORKER` / `DATABASE` / `CACHE` / `QUEUE` / `EXTERNAL_API` / `INFRASTRUCTURE` | components of the matching category |
| `COMPONENT` | fallback for unknown categories |
| `REPOSITORY` | code repositories |
| `ENDPOINT` / `UNKNOWN` | reserved (endpoints currently modeled in `service_endpoints`) |

Each node carries: `id`, `project_id`, `environment_id` (where applicable), `node_type`, `name`, `external_identifier`, `description`, `status` (`ACTIVE`/`STALE`/`DISABLED`/`UNKNOWN`), `criticality` (`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`/`UNKNOWN`, explicit configuration only), language/framework/runtime/version/repository/documentation/ownership fields, `metadata`, timestamps.

---

## 3. Edge types and semantics

| EdgeType | Meaning |
|----------|---------|
| `CONTAINS` | structural containment (project → environment → components) |
| `DEPENDS_ON` | declared dependency (from canonical `component_dependencies`) |
| `CALLS` | observed or configured communication |
| `READS_FROM` / `WRITES_TO` | storage/cache access semantics |
| `PUBLISHES_TO` / `CONSUMES_FROM` | queue/topic semantics |
| `DEPLOYED_AS` | component ↔ deployment linkage |
| `DEPLOYS` | deployment event → version relation |
| `IMPLEMENTS` | service ↔ repository linkage |
| `HOSTS` | infrastructure hosting |
| `RELATED_TO` | untyped but known relation |

**Semantics are not causality.** `Checkout Service —CALLS→ Inventory Service` means *observed or configured communication*. It does **not** mean Inventory caused a Checkout failure. Nothing in Phase 2 claims causal direction.

**Provenance** (`GraphEdgeSource`): `MANUAL`, `CONFIGURATION`, `TRACE`, `LOG`, `METRIC`, `DEPLOYMENT`, `REPOSITORY`, `IMPORT`, `INFERENCE`, `MOCK`. Every edge records how we know it exists. The UI presents these as **Configured** / **Observed** / **Inferred** — uncertainty is never hidden.

**Confidence** is evidence strength (`0.0`–`1.0`), never a probability of causality: `1.0` for explicit configuration, `0.5` for a single weak trace signal, and so on. Values are assigned only at deterministic extraction points.

---

## 4. Component identity, aliases, ownership

- **Identity** — a component's identity is its canonical `entity_id`, not its display name. The registry resolves telemetry service names via explicit aliases and exact/normalized lookups; it never blindly merges similar names.
- **Aliases** (`graph_node_aliases`) — explicitly configured (`checkout-service`, `checkout_api`, `service-checkout` → *Checkout Service*) or confidently discovered. Conflicting aliases are surfaced by data-quality checks, not silently merged.
- **Ownership** (`component_owners`) — team, owner name, contact e-mail, repository owner. Set only through explicit configuration.

---

## 5. Service endpoints

`service_endpoints` model the API surface per component: HTTP method, **normalized path template** (`/api/checkout/{id}`, not raw `/api/checkout/12345`), original raw paths, internal/external flag, environment, first/last seen. Normalization is a deterministic, bounded strategy (UUID-like segments → `{id}` templates) — not a universal path parser. Unknown endpoints discovered through telemetry are recorded with `source=TRACE` evidence.

---

## 6. Discovery

Weak evidence (an unknown service name appearing in traces/logs) creates a `graph_discovery_records` row with status `PENDING` — a *suggestion*, not a component. Records carry evidence count, sources, first/last seen, confidence, and an identity hint. Conversion to a real node happens **only** via explicit registration (`POST .../graph/discovery/{id}/register`) or ignore.

---

## 7. Ingestion pipeline and reconciliation

The async ingestion path feeds the graph automatically:

```text
Telemetry → identity resolution → component discovery → relationship
discovery → graph reconciliation → validation → persistence
```

Concretely: every trace/span ingestion enqueues a Redis `graph_extract` job. The worker loads recent spans and trace-carrying events, extracts typed edges (`CALLS`, `READS_FROM`, …) via a deterministic parent/child walk, then runs reconciliation. The same services are callable directly (used by the seed and tests).

`GraphReconciler.reconcile()` mirrors canonical state (project/environments/components/dependencies/repositories), merges extractor output, and records every run in `graph_reconciliation_runs` (input source, counters, timing). A run records what changed; it never deletes.

**Stale policy** — edges whose `last_seen_at` ages past `GRAPH_STALE_AFTER_DAYS` (default 30) are marked `STALE`. `DISABLED` requires an explicit action.

---

## 8. Snapshots and temporal queries

`graph_snapshots` capture structure at a point in time: project, environment, version (per project), node/edge counts, source, caption, previous-snapshot link. Snapshots store a *signature* (node/edge identity strings) enabling set-level diffs (`GET /graph/snapshots/{a}/diff/{b}`) without full graph copies. Edges keep `first_seen_at`/`last_seen_at` and status, giving a foundation for time-travel analysis without full temporal playback (deferred).

---

## 9. Querying

`GraphQueryService` provides deterministic, bounded queries: node/edge lookup with filters, neighbors, direct/transitive dependencies and dependents, shortest path (BFS with `max_depth` ≤ 25 and `max_nodes` ≤ 2000 caps), and environment comparison (added/removed/changed components, endpoints, versions).

`DependencyImpactAnalyzer` answers *what is reachable downstream of X* — labeled **Dependency Impact**, not failure impact. Reachability is graph structure, not a prediction.

Dependency semantics note: dependency/dependent/impact traversals follow dependency-carrying edges only (`DEPENDS_ON`, `CALLS`, `READS_FROM`, `WRITES_TO`, `PUBLISHES_TO`, `CONSUMES_FROM`); structural edges (`CONTAINS`, `IMPLEMENTS`, `DEPLOYED_AS`, …) never create false transitive dependencies.

---

## 10. Data quality

`graph_data_quality_records` persist findings from deterministic checks: orphan nodes, orphan edges, duplicate components, unresolved dependencies, missing environments, stale relationships, conflicting identities. `GET /projects/{id}/graph/health` aggregates them into counts per check/severity plus an overall `ok` flag (`ok=false` only when ERROR-severity findings exist).

---

## 11. Security

- Every graph endpoint is **project-scoped server-side**; components resolve through their project, and path queries verify both endpoints share a project.
- Traversal bounds (`max_depth` ≤ 25, `max_nodes` ≤ 2000) are clamped server-side on every route — no unbounded exploration.
- Metadata is redacted before persistence; no secrets in graph tables.

---

## 12. Performance

`infrastructure/graph-benchmark.py` builds synthetic graphs at 100/500 and 1000/5000 scale and measures retrieval, dependency traversal, path queries, and impact closure. Indicative numbers (SQLite, single run):

| Operation | 100c/500e | 1000c/5000e |
|-----------|-----------|--------------|
| Synthesize + reconcile | 1.3 s | 12.2 s |
| Graph retrieval | 8 ms | 90 ms |
| Dependency traversal (transitive, depth 10) | 151 ms | 1.22 s |
| find_path (BFS, depth 25) | 11 ms | 297 ms |
| Impact closure (depth 25) | 125 ms | 1.26 s |

Informational only — not a production-scale performance claim.

---

## 13. Future extension points

Interfaces exist for later phases (`GraphReasoningEngine`, risk analyzers, causal analysis, incident graph building). Phase 2 deliberately provides **reliable structured data only**. Future AI systems will consume graph context such as *incident → affected component → dependencies → recent deployments → observability evidence → repository*, but no reasoning is implemented here.

## 14. Limitations

- Uncertainty (Inferred/Observed/Configured) is display-only; nothing acts on it yet.
- No causality, risk scoring, or failure prediction.
- Environment diffing compares current mirrored state (by last-seen versions), not full temporal history.
- Endpoint templates use a bounded normalization strategy, not a universal parser.
- Snapshot diffs are set-level (added/removed), not full graph timeline playback.
