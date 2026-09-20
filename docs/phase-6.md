# Phase 6 — AI Debugger

> **Locate · Hypothesize · Cite · Validate · Explain**
>
> Phase 6 does **not** fix, patch, deploy or claim certainty. It maps production
> evidence onto the indexed source, reasons only over what is stored, validates
> every code claim against the pinned snapshot, and shows its audit trail —
> including every claim it rejected.

Phases 1–5 let ARGUS say *"here is the most evidence-supported explanation, and
here is an experiment that tested it."* Phase 6 takes the investigation to the
code:

```
Incident
   ↓
Trace → code mapping (span names, routes, stack traces)
   ↓
Repository registration + snapshot pinning
   ↓
Incremental code index (files, symbols, call graph, routes, risk signals)
   ↓
Debug session (context building → analysis)
   ↓
Evidence-grounded analysis (deterministic or model-assisted)
   ↓
Validated code locations + hypotheses with citations
   ↓
Grounded follow-up questions (read-only tools, budgeted)
   ↓
Auditable timeline + honesty metrics
```

The rules that shape every design decision below:

> **A code claim that has not been validated against the pinned snapshot is not
> a finding.** It is stored with `displayable: false` and a reason — visible for
> audit, never rendered as a result.

> **A model's confidence never upgrades a hypothesis.** Validation status is
> decided by resolved stored evidence; the UI ranks by that first.

> **Everything external is data, never instructions.** Source files, commit
> messages and telemetry are quoted, never obeyed; instructions planted in any
> of them are reported, not followed.

---

## 1. Architecture

| Component | Module | Responsibility |
| :--- | :--- | :--- |
| Repository provider | `services/repository_provider.py` | Local checkouts and git remotes: read files, history, blame, diffs; capability advertisement is real, not claimed |
| Snapshot service | `services/code_snapshot_service.py` | Resolve a revision to an immutable snapshot row (commit SHA, message, author, version evidence) |
| Parser | `services/code_parser.py` | Python and TypeScript/JavaScript parsing: definitions, occurrences, routes, docstrings; versioned contract |
| Indexer | `services/code_index_service.py` | Content-hash incremental indexing; per-file commit attribution; relationships; risk signals; idempotent re-index |
| Graph queries | `services/code_graph.py`, `services/code_query_service.py` | Resolved call/callers/callees, related files, search over symbols/source/references |
| Trace mapper | `services/trace_code_mapper.py` | Spans, endpoints, operation names and stack frames → code locations, with unmapped reasons |
| Change intelligence | `services/change_history.py`, `services/change_relevance.py` | Per-file history/blame from the VCS; change classification (temporal relevance is not blame) |
| Debug context | `services/debug_context_builder.py` | Bounded, redacted context from stored evidence only; redaction report |
| Session manager | `services/debug_session_service.py` | Session lifecycle, message log, deterministic investigation (§43) |
| AI debugger | `services/ai_debugger.py` | Model-assisted analysis and grounded Q&A: tool loop, budget, citation validation |
| Code tools | `services/code_tools.py` | Read-only, bounded tool surface the model may call; every call recorded |
| Validator | `services/debug_reference_validator.py` | Every file/symbol/line claim resolved against the snapshot; rejections stored |
| Reaper | `services/code_sweep.py` | Closes sessions/runs/repositories abandoned by a dead process |
| Data | `models/code.py` | `code_repositories`, `repository_snapshots`, `code_files`, `code_symbols`, `code_references`, `code_relationships`, `code_risk_signals`, `trace_code_mappings`, `debug_sessions`, `debug_analysis_runs`, `debug_code_locations`, `debug_hypotheses`, `debug_evidence`, `debug_messages`, `debug_tool_calls` |
| API | `routes/code.py` | 26 paths: repositories, indexing, snapshots, files, symbols, search, mappings, sessions, analysis, messages, tools, timeline, metrics |
| Frontend | `apps/web/app/debugger/`, `lib/debugger.ts` | Debugger workspace, per-incident entry, session workspace; presentation rules mirror the backend's epistemics |

## 2. Code intelligence

**Registration is validated.** A repository is registered against a provider
(`local` path within configured allowed roots, or a `git` remote); the backend
reads it, records the capabilities that actually work, and refuses a bad path
with the provider's own reason.

**The snapshot pins the version.** Every analysis runs against an immutable
snapshot row carrying the commit SHA, message, author and the *version
evidence* — how ARGUS knows which revision produced the observed behaviour
(deployment mapping, or an explicit reference). `RESOLVED`, `UNRESOLVED` and
`UNKNOWN` are first-class: an unresolved version lowers the confidence of every
code claim made against that snapshot.

**Indexing is incremental by content hash.** A file is reused from the base
snapshot when its content hash matches — timestamps are never trusted. Moving a
file re-parses it; an unchanged file's symbols are copied with stable ids, so a
stored debug session's references keep resolving. Re-indexing an unchanged
revision is a true no-op that reports every file as reused. Parser-version
changes force a rebuild, and the run metadata records exactly what happened.

**Everything is queryable:** files (with last-commit attribution from the real
VCS), symbols with signatures and call counts, callers/callees with
relationship confidence, route metadata (file + HTTP method + path), and search
across symbol names, source text and name occurrences.

**Risk signals are labels, not scores.** Complexity hotspots, long functions,
god classes and similar are investigation *signals* with the reason they fired —
never a "bug score".

## 3. Trace → code mapping

Each failing span of an incident is mapped to code by four strategies, in
confidence order: exact span→symbol mapping, endpoint route match (HTTP method
+ normalized path template), operation-name heuristics, and service-file
heuristics. Stack frames from exceptions map to `file:line`. Every mapping row
records its kind, confidence and evidence; **unmapped spans are recorded with
an unmapped reason** rather than silently dropped.

## 4. The debug session

A session binds one incident to one snapshot and owns the conversation. The
lifecycle is `CREATED → CONTEXT_BUILDING → ANALYZING → WAITING_FOR_VALIDATION →
COMPLETED` (or `FAILED`/`CANCELLED`), with every transition stored.

**Context is built from stored evidence only:** the incident, its anomalies and
evidence, the failing spans, the causal analysis, the mappings, the indexed
symbols for the affected components, and classified recent changes. It is
bounded by configuration, redacted (secret detection with a stored redaction
report), and versioned (`context_version`) so an analysis can name the context
it reasoned over.

**Analysis has two providers.** The deterministic investigation (§43) needs no
model at all: it derives locations, hypotheses and recommended inspections from
mappings, causal candidates and the code graph, and validates everything
through the same validator as the model path. The model path (when an AI
provider is configured) runs a bounded tool loop over read-only code tools —
`search_code`, `get_file`, `get_symbol`, `get_history`, `get_related` — with a
per-run budget. A provider failure **degrades**: the run is stored as
`DEGRADED` with its reason, alongside the deterministic result, never as an
empty success.

**Validation before display.** Every claimed location is resolved against the
snapshot: `VALID`, `NOT_FOUND`, `OUT_OF_SNAPSHOT`, `LINE_OUT_OF_RANGE`,
`AMBIGUOUS` or `STALE`. Only `VALID` locations are findings; everything else is
stored with its reason and shown only as audit. `LIKELY_FAULT_LOCATION` is the
strongest label the system emits, and it means *verified to exist and cited by
the analysis* — never *proven to be the bug*.

**Hypotheses** carry category, confidence (`HIGH`/`MEDIUM`/`LOW`/
`INSUFFICIENT`), validation status (`UNVERIFIED` / `SUPPORTED` /
`PARTIALLY_SUPPORTED` / `WEAKENED` / `REFUTED` / `INVALID_REFERENCE`),
supporting and contradicting evidence with resolvable references, a test
approach when testable, and a recurrence count. Ranking is validation-first.

## 5. Grounded follow-up questions

Questions are answered from stored evidence only. The answering loop may call
the read-only tools within budget; every call is recorded with its arguments,
status, size and truncation flag. The answer carries:

* its citations — validated, resolvable references only;
* the references that failed validation (stored, never silently dropped);
* what evidence is missing;
* a degraded reason when the model was unavailable;
* the tool budget actually used.

Prompt-injection defence: source text, commit messages and telemetry are
delimited as data; instructions found inside them are reported to the user, not
followed; the tool surface is read-only so even a successful injection cannot
mutate anything.

## 6. Isolation and safety

Project scoping is enforced server-side on every route: another project's
session, repository or snapshot is 404, never data. Local providers are
confined to configured allowed roots. Secret scanning applies to responses and
context alike. The reaper closes abandoned work — a session stuck `ANALYZING`
or a repository stuck `INDEXING` after its process died is marked with the
honest terminal state, past a grace period tied to the analysis budget.

## 7. Metrics that measure honesty

`GET /debugger/metrics` reports the counts that show where the debugger is
*weak*: claimed vs. validated locations, rejected citations, degraded analyses,
refused tool calls, by-validation-status hypothesis counts — with stated
limitations. The frontend leads with the same ratios.

## 8. Frontend

`/debugger` — health metrics, repositories (register/index), incidents entry.
`/debugger/incident/{id}` — per-incident sessions and start controls.
`/debugger/{id}` — the session workspace: analysis audit (bounds, tool calls,
degraded reason), hypotheses ranked by validation, locations split into
findings and rejected claims, conversation with grounded answers, timeline.
Presentation helpers in `lib/debugger.ts` enforce the display rules: a
non-VALID location is never a finding, validation styles mirror the backend
enums, and unrecognised values render as themselves.

## 9. Configuration

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `CODE_ALLOWED_ROOTS` | working set | Roots a local repository provider may read |
| `CODE_MAX_FILES_PER_SNAPSHOT` | 5000 | Index bound |
| `CODE_MAX_FILE_BYTES` | 1 MiB | Per-file size bound |
| `DEBUG_MAX_ANALYSIS_SECONDS` | 120 | Analysis budget (also drives the reaper's grace) |
| `DEBUG_MAX_TOOL_CALLS` | 12 | Per-run tool budget |
| `CODE_SWEEP_ENABLED` / `CODE_SWEEP_INTERVAL_SECONDS` | true / 120 | Reaper cadence |

## 10. Limitations

* The debugger explains; it never edits code, opens PRs, or auto-applies
  anything.
* Languages without a dedicated parser are indexed heuristically and their rows
  are labelled as such (`files_heuristic`), with lower mapping confidence.
* Symbol resolution is best-effort across dynamic patterns; unresolved
  references are stored, not guessed.
* A model provider is optional; without one the deterministic investigation
  answers, clearly labelled as such.
* Line-level claims are exact only for the pinned snapshot; a newer commit makes
  them `STALE` until re-indexed.
