# Phase 6 — Completion Report

**AI Debugger — Code Intelligence & Evidence-Grounded Debugging**

Status: **complete** — implemented end to end, verified live against the compose
stack, all gates green.

---

## 1. Implemented

| Area | Delivered |
| :--- | :--- |
| Providers | `repository_provider.py` — local + git providers: read, history, blame, per-file commit attribution, diffs; capabilities are measured, not claimed; allowed-roots confinement |
| Snapshots | `code_snapshot_service.py` — revision resolution, commit metadata, version evidence (`RESOLVED`/`UNRESOLVED`/`UNKNOWN`), no-op re-index of an unchanged revision |
| Parsing | `code_parser.py` — Python (AST) + TS/JS (heuristic structural), versioned parser contract, routes, docstrings, complexity |
| Indexing | `code_index_service.py` — content-hash incremental reuse, stable symbol ids, per-file commit attribution, resolved relationships, risk signals, `PARSER_VERSION` invalidation |
| Queries | `code_graph.py`, `code_query_service.py` — callers/callees, related files, symbols, search (names, source, occurrences) |
| Trace mapping | `trace_code_mapper.py` — span/route/operation/frame strategies with confidence + unmapped reasons |
| Change intel | `change_history.py` (VCS-backed), `change_relevance.py` (classification: recent ≠ guilty) |
| Sessions | `debug_session_service.py` — lifecycle, message log, deterministic investigation |
| AI | `ai_debugger.py` — bounded tool loop, budget, citation validation, degradation, grounded Q&A |
| Safety | `debug_context_builder.py` (redaction report), `code_tools.py` (read-only tools), prompt-injection containment, `code_sweep.py` (reaper) |
| Data | `models/code.py` (15 tables), `schemas/code.py`, migration `phase6_code_intelligence` |
| API | `routes/code.py` — 26 paths incl. `/debug-sessions/{id}/{analyze,messages,tools,timeline}` and `/debugger/metrics` |
| Frontend | `lib/debugger.ts` + typed client methods, `/debugger` workspace, `/debugger/incident/{id}`, `/debugger/{id}` session workspace (analysis audit, hypotheses, rejected claims, conversation, timeline), sidebar + incident links, 14 vitest presentation tests |
| Docs | `docs/phase-6.md`, this report, README/roadmap/data-model updates |
| Live gate | `infrastructure/e2e-smoke-phase6.sh` |

## 2. Gates

| Gate | Command | Result |
| :--- | :--- | :--- |
| Backend test suite | `pytest -q` | **1008 passed** (142 Phase 6) |
| Lint / format / types | `ruff check`, `ruff format --check`, `mypy app` | clean (133 modules) |
| Frontend tests | `npm test` | **97 passed** (14 Phase 6) |
| Frontend type check / build | `tsc --noEmit`, `npm run build` | clean / succeeds |
| Phase 0–5 suites | unchanged | all green |
| Phase 6 live gate | `bash infrastructure/e2e-smoke-phase6.sh` | see report footer — run output recorded in the final validation |

## 3. Defects found and fixed during live validation

The live gate found real defects that unit tests had missed; each fix ships
with a regression test:

1. **Pinned revision recorded as `UNKNOWN`** — an explicitly requested revision
   was not persisted as version evidence; the snapshot now records `RESOLVED`
   with the reference.
2. **Silent empty diffs** — an unknown revision in the diff/history endpoints
   returned "no changes" instead of saying it could not resolve the revision.
3. **Symbol mangling accepted by the validator** — the deterministic path could
   emit `py:post_checkout`-shaped names; both the emitter and the (too
   permissive) validator were fixed.
4. **`files_reused` always 0** — the API read run metadata that never contained
   the key; incremental indexing now reports true reuse counts.
5. **Duplicate route declarations** — decorator *calls* were counted as route
   definitions as well as the decorated function.
6. **Stale-collection-time test clock** — the sweep test froze `NOW` at module
   import, so in a full-suite run the "just started" run was legitimately older
   than the reaper's grace. Fixed with a per-call clock (and documented).

## 4. Guarantees carried by this phase

* Only `VALID` (snapshot-verified) locations are findings; rejections are
  stored and auditable.
* Hypothesis ranking is validation-first; model confidence never upgrades a
  hypothesis.
* The tool surface is read-only, budgeted and fully recorded.
* External content is data, never instructions; planted instructions are
  reported, not followed; secrets are redacted with a stored report.
* Another project's data is 404, never visible.
* Degradation is explicit: no model, no problem — the deterministic
  investigation answers and the run is labelled `DEGRADED`, never faked.
