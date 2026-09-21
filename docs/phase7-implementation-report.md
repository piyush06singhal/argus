# ARGUS Phase 7 — Completion Report

**Automated Fix Generation, Patch Validation & Verification**

Status: **complete** — implemented end to end, verified against the live stack,
and documented. The delivered behaviour is `generate → validate → test →
reproduce → verify → review`. Nothing merges, deploys or remediates.

---

## 1. Implemented

### Backend

| Area | Module |
| :--- | :--- |
| Data model (10 tables) | `app/models/fix.py` |
| Migration (native enums, UUID keys, cascades, reversible) | `alembic/versions/c4d5e6f7a8b9_phase7_fixes.py` |
| Fix planning from validated evidence | `app/services/fix_planner.py` |
| Deterministic + model-assisted generation | `app/services/patch_generator.py` |
| Unified-diff parsing and validation | `app/services/patch_parser.py` |
| Safety validation (scope, sensitive, deps, secrets, tampering) | `app/services/patch_safety.py` |
| Isolated git workspaces | `app/services/patch_workspace.py` |
| Command registry & executor | `app/services/patch_commands.py` |
| Verification ladder & two-sided regression test | `app/services/patch_verification.py` |
| Hashed, immutable artifacts | `app/services/patch_artifacts.py` |
| Workflow orchestration, review, export | `app/services/fix_service.py` |
| Workspace reaper | `app/services/fix_sweep.py` |
| API (18 routes) | `app/api/v1/routes/fix.py` |
| Request/response schemas | `app/schemas/fix.py` |
| Config knobs (`FIX_*`) and lifespan wiring | `app/core/config.py`, `app/main.py` |

### Frontend

`/fixes` (dashboard, §67) and `/fixes/{id}` (workspace: diff viewer §43,
verification timeline §44, §64 checklist, §69 explanation, §70 audit trail,
review actions), with presentation helpers in `apps/web/lib/fixes.ts` and typed
client methods in `apps/web/lib/api.ts`.

### Demo and gates

`demo/argus-commerce` carries the planted defect (retry amplification over a
sub-second database timeout) and a `pytest.ini` that makes its own suite
runnable — the fix workflow verifies against *that* suite, not a fixture.

---

## 2. Architecture

```
incident → RCA → reproduction → code investigation
   ↓
FixHypothesis            planned from validated Phase 6 locations only
   ↓
Patch (deterministic | model)   composed from stored snapshot bytes
   ↓
PatchParser → PatchSafetyValidator      parse, scope, secrets, tampering
   ↓
PatchWorkspaceManager   temp worktree + branch, original never written
   ↓
CommandExecutor         allowlisted argv, no shell, offline, bounded
   ↓
PatchVerificationEngine static → regression (two-sided) → tests → reproduction → comparison
   ↓
PatchArtifactStore      hashed, immutable evidence outside the workspace
   ↓
PatchReviewAction       the only path to APPROVED; stops there
```

The service layer owns state transitions and persistence; the pure services own
judgement. That split is why the verification decision can be re-read from stored
rows — and why the export report can never disagree with the run.

---

## 3. Patch generation

* **Fix hypotheses** come from a debug session's *validated* code locations; the
  scope allowlist is seeded from them, the excluded paths come from the safety
  defaults, and the category is chosen from evidence keywords (or stays
  `UNKNOWN`).
* **Deterministic generation** applies named recipes to stored bytes and renders
  a real unified diff; unmatched evidence is refused rather than guessed.
* **Model-assisted generation** receives only the scoped, redacted files plus the
  hypothesis and evidence references; its output is schema-validated and refused
  for hallucinated files, out-of-scope diffs or malformed output.
* **Constraints** are both prompted and enforced: no unrelated files, no new
  dependencies, no removed or weakened tests, no disabled lint/type checks, no CI
  edits, no secrets, no verification-tooling changes.
* **Scope** is checked three times — at generation, before application, and
  again inside the engine after application.

## 4. Safety

| Control | Implementation |
| :--- | :--- |
| Isolated workspace | temp directory per candidate, `git init` + branch under `argus/fix/`; `create_branch` refuses anything else |
| Original repository untouched | snapshot copied into the workspace; verified by the live gate (`git status --porcelain` empty, HEAD unchanged, no `argus/*` branches) |
| Command allowlist | registry keys → fixed argv; unknown key raises; **no shell** anywhere |
| Network restrictions | entries declare `OFFLINE`; no egress is configured |
| Secret handling | redaction over command output and model context; post-apply scan of changed files; artifacts/UI carry redacted text |
| Path validation | traversal, absolute paths and backslashes refused by parser and schema |
| Test tampering | deleted tests, weakened assertions, skips, disabled lint/typing, CI or verification edits → disqualifying finding, refused before any workspace exists |
| Command registry limits | per-command timeout, environment passthrough (`PATH`,`HOME`,`LANG`,`TMPDIR`), bounded output tail |

## 5. Verification

* **Static validation** — `py_compile` on exactly the changed Python files (a
  bare invocation would fail every patch, so changed files are passed
  explicitly).
* **Build** — the detected build command, or a recorded
  `BUILD_CONFIGURATION_UNKNOWN` when the repository declares none.
* **Tests** — deterministic selection (changed-module matches first), real exit
  codes stored, no claims without a row.
* **Regression test** — generated from the patch diff itself; must fail on the
  base commit and pass on the patched tree; anything else is
  `REGRESSION_TEST_INVALID` or `FAILS_ON_PATCHED`.
* **Reproduction** — the Phase 5 result is an input: without a baseline
  reproduction there is nothing to verify, and a patched tree that still
  reproduces is `NOT_VERIFIED` even when every test passes.
* **Comparison** — latency, error-rate and memory thresholds with the metric
  deltas stored; a breach is a regression and refuses verification.
* **Final verification** — `VERIFIED` / `FULLY_VERIFIED` / `HIGH` only when every
  rung passed; the confidence reason names the evidence.

## 6. Frontend

* **Fix dashboard** (`/fixes`): engine tallies, hypotheses (category, risk,
  status, scope), patches (size band, status, review state), status filters.
* **Diff viewer**: file navigation, line numbers on both sides, add/remove
  highlighting, per-file `+/-` counts.
* **Verification dashboard**: the §44 timeline with per-stage status, the command
  runs behind each stage (exit code, duration, selection reason, output tail),
  the regression test and its content hash, and the §64 checklist.
* **Review flow**: Generate / Verify / Approve / Reject / Regenerate, with the
  approval button appearing only for a stored `VERIFIED` run, a required
  confirmation before the first verification, and verbatim error surfacing.

## 7. Testing

| Gate | Command | Result |
| :--- | :--- | :--- |
| Backend suite | `cd apps/api && pytest -q` | **1093 passed** (85 Phase 7) |
| Lint | `ruff check app tests` | clean |
| Format | `ruff format --check app tests` | 206 files clean |
| Types | `mypy app` | clean (146 modules) |
| Frontend suite | `cd apps/web && npm test` | **113 passed** (16 Phase 7) |
| Frontend types | `npx tsc --noEmit` | clean |
| Frontend build | `npm run build` | succeeds, 26 routes |
| Phase 7 live gate | `bash infrastructure/e2e-smoke-phase7.sh` | **90/90** |
| Phase 7 live gate + DDL probe | `DDL_PROBE=1 bash infrastructure/e2e-smoke-phase7.sh` | **91/91** |
| Phase 6 live gate (regression) | `bash infrastructure/e2e-smoke-phase6.sh` | **159/159** |

Phase 7 test files: `test_phase7_parser.py` (13), `test_phase7_safety.py` (36),
`test_phase7_verification.py` (13), `test_phase7_commands.py` (16),
`test_phase7_fix_workflow.py` (14), `test_phase7_sweep.py` (2),
`test_phase7_demo.py` (4).

### Performance

Verification cost is dominated by the repository's own commands; the engine
records `duration_ms` per command and per run, and the live gate asserts they are
present. On the demo checkout (9 files, one pytest module) a full verification —
workspace creation, apply, static gate, two regression runs, unit run,
comparison, artifact storage and teardown — completes in a few seconds, with the
workspace destroyed on every path.

## 8. Demo

Four scenarios run against the real demo application
(`apps/api/tests/test_phase7_demo.py`), and the same pipeline is driven over HTTP
by the live gate:

| Scenario | Outcome |
| :--- | :--- |
| Successful fix (§60) | `VERIFIED` / `FULLY_VERIFIED`; regression test failed on base, passed on patch; the demo's own async test passed; `AWAITING_REVIEW`; approval recorded, nothing deployed |
| Bad patch (§61) | a patch that fixes the timeout but pushes memory past the threshold → `NOT_VERIFIED`, `regression detected`, and approval refused |
| Test tampering (§62) | a diff deleting the failing test → `VALIDATION_FAILED` with a tampering finding; it never reaches a workspace, and verification refuses it |
| Failed verification (§63) | builds and passes tests but the failure still reproduces → `NOT_VERIFIED` with *"still reproduces"* as the reason |

Also exercised live: scope and ownership refusals, foreign-project isolation,
second-approval refusal, decision-is-final for approved patches, regeneration
superseding the previous candidate, candidate isolation (no workspace or
verification carried over), AI generation failing loudly with no model
configured, and checkpoint checks that the original checkout is byte-identical
and no workspace survives.

## 9. Defects found and fixed during live verification

Running the pipeline against PostgreSQL rather than the SQLite test shim found
five real problems that unit tests could not:

1. **Migration used `VARCHAR(36)` ids.** PostgreSQL rejected the foreign keys
   (`incompatible types: character varying and uuid`); ids are now native UUIDs,
   matching Phase 0–6.
2. **Enum columns were `VARCHAR`.** The models use native enums, so queries
   failed with `operator does not exist: character varying = workspacestatus`;
   the migration now creates the types once and references them.
3. **Timestamps had no server default.** Inserts failed with
   `null value in column "created_at"`; all `created_at`/`updated_at` columns now
   carry `server_default=now()`, as earlier phases do.
4. **`GET /patches/{id}/comparison` 500'd** on a lazy relationship
   (`MissingGreenlet`); comparisons are queried explicitly.
5. **Verified runs returned empty evidence.** `POST /verify` omitted the stored
   test runs, regression test and comparison, which reads as "nothing was
   checked"; all three now travel with the verdict.

Two smaller ones: the AI generation path was implemented but unreachable through
the API (now wired, and its failure modes tested), and `/fixes/{id}/generate`
rejected `created_by` even though the service records it.

## 10. Limitations

Documented in [phase-7.md](phase-7.md#13-limitations): verification inherits the
repository's test coverage; the deterministic generator covers a handful of
defect shapes; model output is untrusted by construction; the reproduction result
is supplied to the verifier rather than produced by it; build detection is
marker-based (Python/Node/Make) and reports `BUILD_CONFIGURATION_UNKNOWN`
otherwise; risk assessment is deterministic and descriptive, not predictive.

## 11. Next phase

**Phase 8 — Predictive Reliability**: trend, capacity and reliability prediction
from historical behaviour, and pre-incident signal detection. Not started.
