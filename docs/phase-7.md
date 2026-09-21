# Phase 7 — Automated Fix Generation & Verification

> **Hypothesize · Generate · Validate · Test · Reproduce · Compare · Verify · Review**
>
> Phase 7 does **not** merge, deploy, release or remediate. It produces the
> smallest defensible change for a failure ARGUS can already explain and
> reproduce, proves in a disposable workspace that the failure is gone and
> nothing else broke, stores the evidence hashed and immutable, and stops at a
> human decision.

Phases 3–6 answer *what happened, why, and where in the code*. Phase 7 is the
first phase that may **produce a change** — which is why the phase is built
around one distinction:

```text
Fix Suggestion ≠ Valid Patch ≠ Verified Fix ≠ Production-Safe Deployment
```

Every table, state and API in this phase exists to keep those four apart.

```
Incident
   ↓
Root-cause hypothesis (Phase 4)
   ↓
Reproduction (Phase 5)
   ↓
Code investigation (Phase 6)
   ↓
Fix hypothesis        ← planned from validated evidence only (§5, §10)
   ↓
Patch generation      ← deterministic or model-assisted, from stored bytes (§8, §21)
   ↓
Patch parsing         ← unified diff, counts validated (§11, §12)
   ↓
Safety validation     ← scope, sensitive files, deps, secrets, tampering (§13–§16, §55)
   ↓
Disposable workspace  ← git worktree on a temp branch, sealed from the original (§17, §48)
   ↓
Command registry      ← allowlisted argv, no shell, bounded, offline (§24, §50, §51)
   ↓
Build / static / tests
   ↓
Two-sided regression test (§29)
   ↓
Baseline vs patched reproduction (§30, §32, §33)
   ↓
Before/after comparison (§31, §36, §37)
   ↓
Verdict: VERIFIED / NOT_VERIFIED (§64)
   ↓
Human review → AWAITING_REVIEW → APPROVED | REJECTED | REGENERATED (§41, §71)
```

---

## 1. What the phase refuses to do

These are not aspirations; they are enforced in code and asserted by tests and
by the live gate.

| Never | Where it is enforced |
| :--- | :--- |
| Modify the user's repository | `PatchWorkspaceManager` copies the snapshot into a temp worktree; the original is never opened for writing (`§48`) |
| Run a command the model asked for | `CommandExecutor` executes only registry keys with fixed `argv`; unknown keys raise `CommandRefused` (`§50`) |
| Reach the network during verification | Registry entries declare `OFFLINE`; the executor never configures egress (`§51`) |
| Leak a secret into logs, artifacts or the UI | `SourceRedactor` runs over command output and model context; artifacts are scanned before storage (`§52`) |
| Store an unvalidated diff | `generate_patch_for_hypothesis` parses and safety-checks before the row is written (`§11`, `§13`) |
| Approve an unverified patch | `record_review_action` requires a stored `VERIFIED` run, not just a status (`§34`, `§41`) |
| Re-open a decided patch | assessment of terminal review states refuses further actions, matching the UI's allowlist (`§41`, `§71`) |
| Merge, deploy or release | No code path exists for it; the export report states it explicitly (`§74`) |

---

## 2. Domains and tables

Ten tables (`alembic/versions/c4d5e6f7a8b9_phase7_fixes.py`), all with native
PostgreSQL enums and UUID keys, matching the Phase 0–6 conventions.

| Table | What it stores | Notable fields |
| :--- | :--- | :--- |
| `fix_hypotheses` | the *idea*, bound to real evidence | `category`, `scope_files`, `excluded_paths`, `target_symbols`, `supporting_evidence`, `risk_level`, `confidence`, `status` |
| `patches` | the *artifact*: one unified diff | `base_commit_sha`, `patch_content`, `changed_files`, `lines_added/removed`, `symbols_modified`, `generated_by`, `generation_model`, `explanation`, `failure_reason` |
| `patch_workspaces` | one isolated experiment | `branch_name`, `base_commit_sha`, `root_path`, `status`, `created_at_workspace`, `destroyed_at` |
| `patch_verification_runs` | one attempt, one verdict | `status`, `level`, `confidence`, `confidence_reason`, `tampering_flag`, `verification_env_intact`, `baseline_failure_reproduced`, `patched_failure_reproduced`, `regression_detected`, `evidence` |
| `patch_test_runs` | allowlisted commands that ran | `kind`, `command_key`, `command_resolved`, `exit_code`, `timed_out`, `duration_ms`, `output_tail`, `selected_tests`, `selection_reason`, `unknown_configuration` |
| `patch_regression_tests` | the two-sided proof | `file_path`, `origin`, `content_hash`, `ran_on_base`, `failed_on_base`, `ran_on_patched`, `passed_on_patched`, `valid`, `invalid_reason` |
| `patch_comparisons` | BASE vs PATCHED numbers | `metrics`, `regressions`, `thresholds`, `summary`, `causal_chain_resolved`, `causal_chain_note` |
| `patch_risk_assessments` | deterministic risk + why | `risk_level`, `signals`, `explanation` |
| `patch_review_actions` | human decisions | `action`, `actor`, `reason`, `new_patch_id`, `audit_metadata` |
| `patch_artifacts` | hashed evidence files | `artifact_type`, `name`, `storage_path`, `size_bytes`, `content_hash`, `immutable` |

Every foreign key to a Phase 0–6 row is `ON DELETE CASCADE` or `SET NULL`: a fix
is derived evidence about a project's incident and must never outlive it.

---

## 3. Fix hypotheses (§5, §6, §10)

A hypothesis is planned from a debug session, and only from evidence that
already exists:

* at least one **validated** code location from the Phase 6 analysis, or a Phase 6
  hypothesis — with neither, planning refuses and says why;
* `scope_files` is seeded from those validated locations (an engineer may widen
  it explicitly through `scope_override`, and nothing widens it implicitly);
* `excluded_paths` takes the safety validator's sensitive-area defaults, so a
  hypothesis cannot opt into them by omission;
* `category` is chosen from keywords in the evidence text — when nothing
  supports one it stays `UNKNOWN` rather than guessing;
* `supporting_evidence` carries resolvable references (causal candidate rows,
  the analysis id, a reproduction experiment) so the patch can cite its origin.

## 4. Patch generation (§8, §9, §11, §12, §21, §22, §57)

Two generators, one contract: **the output must parse, must stay in scope, and
must be derived from stored bytes.**

**Deterministic** (`DeterministicPatchGenerator`) applies a small set of named
recipes to the *stored* content of the pinned snapshot — restore a database
timeout, bound a retry loop, cap a backoff. It composes a real unified diff with
`difflib`, so the result always parses and always applies to the base commit. If
no recipe matches the evidence, it refuses: *"ARGUS will not guess a patch"*.

**Model-assisted** (`AIFixGenerator`) sends the scoped files' redacted bytes, the
hypothesis and the evidence references to the configured provider and validates
the answer against a strict schema (`ModelPatchProposal`). It refuses:

* a diff for a file whose content was never provided — `hallucinated file (§57)`;
* a diff touching a file outside the requested scope — `outside the requested scope (§57)`;
* malformed output — `model output failed schema validation (§22)`;
* a provider that cannot answer — `AI provider failed (§57)`.

Either way, the patch is persisted only after parsing and safety validation; a
failure is stored as `PARSE_FAILED` / `VALIDATION_FAILED` / `GENERATION_FAILED`
with the reason and an **empty patch body**. Nothing is fabricated to fill the
gap.

Minimality is *measured*, not claimed: `measure()` reports files changed, lines
added/removed and symbols touched, and the API surfaces a size band
(Minimal → Focused → Broad → Very broad).

## 5. Patch safety (§13–§16, §52, §55, §56)

`PatchSafetyValidator` runs before storage and again after application:

* **Scope** — every changed path must be inside `scope_files`; the verification
  engine re-checks it independently, so a patch cannot verify out of scope even
  if the pre-apply gate were skipped.
* **Path traversal** — absolute paths, `..` segments and backslashes are refused
  by the parser and the schema alike.
* **Sensitive files** — CI/CD config, authentication/authorization, secret
  management, infrastructure, deployment config and migrations are *do not
  modify* by default; an explicit approval is recorded as such.
* **Dependencies & configuration** — `requirements.txt`, `pyproject.toml`,
  lockfiles, `Dockerfile`, CI files etc. are elevated-risk classes with their own
  findings.
* **Secrets** — introduced credentials are a hard refusal, and the changed files
  are scanned again post-apply.
* **Test tampering (§55)** — deleting a test, weakening an assertion, adding a
  skip, disabling lint/type-checking, or modifying CI or verification tooling is
  a disqualifying finding (`TEST_TAMPERING`, `CI_MODIFIED`,
  `VERIFICATION_MODIFIED`). A tampering patch never reaches a workspace.

## 6. The isolated workspace (§17, §48, §58)

`PatchWorkspaceManager` materialises the pinned snapshot into a temp directory
(`<tmp>/argus-reproduction/workspaces/argus-fix-<patch>-<candidate>`), `git init`s
it, commits the base tree, creates a branch under `argus/fix/…` and applies the
patch there.

* The branch namespace is enforced — `create_branch("main")` raises.
* A leftover directory from an interrupted run is **removed, not adopted**.
* `destroy()` is unconditional and runs on every path, including failures.
* Every verification attempt gets its own workspace; candidates never share
  mutable state.
* After a run the original repository has no uncommitted changes, no temp branch
  and no stray workspace — asserted by the live gate.

A reaper (`sweep_fix_workspaces_once`, running on the existing worker cadence)
destroys workspaces of runs that died mid-flight and reports what it did; row
timestamps are normalised so it works against both aware (PostgreSQL) and naive
(SQLite) values.

## 7. The command registry (§24, §25, §27, §50, §51)

Nothing is executed as a string. `CommandExecutor` takes a registry key and
appends only *our* arguments:

```text
python_syntax        {python} -m py_compile <changed .py files>
python_lint          {python} -m ruff check .
python_typecheck     {python} -m mypy .
python_tests         {python} -m pytest -x -q
python_tests_selected{python} -m pytest -q <selected test files>
node_build / node_tests / node_lint / make_check
```

Each entry has a fixed argv, working directory, timeout, environment passthrough
and network policy. Discovery is marker-based (`pyproject.toml`, `pytest.ini`,
`package.json`, `Makefile`), never assumed: a repository with no test runner gets
`BUILD_CONFIGURATION_UNKNOWN` rather than a fabricated green tick. Test selection
is deterministic — tests whose path mentions a changed module first, then
everything under `tests/`.

## 8. Verification engine (§23, §26–§37, §64)

`PatchVerificationEngine.verify()` walks the ladder and returns a verdict that
cites what it observed:

1. **Precondition** — the original failure must have been reproduced on the
   baseline (Phase 5). Without it, there is nothing to verify against.
2. **Apply** — the patch is applied inside the workspace.
3. **Post-apply inspection** — the diff that actually landed must match the paths
   the patch declared, and lie inside the scope.
4. **Secret re-scan** — the changed files, post-apply.
5. **Static gate** — byte-compile exactly the changed Python files (a bare
   `py_compile` exits non-zero and would fail every patch).
6. **Two-sided regression test (§29)** — a test is generated *from the patch
   itself*: the removed defect line must be absent and the added fix line
   present. It runs on the base commit (reverse-applied patch, must FAIL) and on
   the patched tree (must PASS). A test that cannot fail on base is recorded as
   `REGRESSION_TEST_INVALID` — never treated as evidence.
7. **Build & selected tests** — the repository's own commands, with their exit
   codes stored.
8. **Reproduction integration (§30, §32, §63)** — tests passing is not enough
   while the original failure still reproduces.
9. **Comparison (§31, §36, §37)** — every configured threshold
   (`latency_regression_fraction`, `error_rate_increase`,
   `memory_increase_fraction`) is compared; any breach is a regression and
   refuses verification.
10. **Verdict** — `VERIFIED` only when every rung passed, with the level
    (`STATIC_VALIDATED` → `FULLY_VERIFIED`) and a confidence whose reason names
    the evidence.

`NOT_VERIFIED` is a first-class result. So is `REGRESSION_DETECTED`,
`TEST_TAMPERING` and the distinction §63 demands: a patch that builds, passes
tests, and *still* leaves the failure reproducing is `NOT_VERIFIED` — never
"verified with caveats".

## 9. Artifacts and the export (§45, §72)

After the run, `store_verification_artifacts` writes and hashes:

```text
patch.diff
test_results.json
build_logs.json
regression_test.py          (when one was generated)
comparison_report.json
verification_report.json
```

Each row carries a SHA-256 of the exact bytes on disk, and terminal-run
artifacts are `immutable`. They live *outside* the workspace, because evidence
that dies with the sandbox is not evidence.

`GET /patches/{id}/report` exports everything a reviewer needs — patch,
hypothesis, verdict, the §64 checklist derived from the **stored rows**, the
artifact count, the diff, and a `boundary` block stating that nothing was merged,
deployed or released.

## 10. API surface

```text
POST   /api/v1/incidents/{id}/fixes          plan a fix hypothesis (§5)
GET    /api/v1/incidents/{id}/fixes          hypotheses for an incident
GET    /api/v1/fixes                         hypotheses (project-scoped)
GET    /api/v1/fixes/metrics                 dashboard tallies (§67)
GET    /api/v1/fixes/{id}                    one hypothesis
POST   /api/v1/fixes/{id}/generate           generate a patch (§8)
GET    /api/v1/fixes/{id}/patches            patches for a hypothesis

GET    /api/v1/patches                       patches (project-scoped)
GET    /api/v1/patches/{id}                  patch + diff + verification history + audit
GET    /api/v1/patches/{id}/diff             the raw unified diff (§43)
POST   /api/v1/patches/{id}/verify           run the verification ladder (§34)
GET    /api/v1/patches/{id}/verification     the latest run, with its evidence rows
GET    /api/v1/patches/{id}/comparison       BASE vs PATCHED (§31)
GET    /api/v1/patches/{id}/artifacts        hashed artifacts (§45)
GET    /api/v1/patches/{id}/report           the §72 export
POST   /api/v1/patches/{id}/approve          human decision (§41)
POST   /api/v1/patches/{id}/reject           human decision (§41)
POST   /api/v1/patches/{id}/regenerate       new candidate; the old one is SUPERSEDED
```

Scope rules follow the Phase 3–6 convention: mutating routes **require**
`project_id` and the row must belong to it; reads accept an optional
`project_id` and answer 404 — never a confirmation — for a foreign id. The review
endpoints determine their own action, so a client cannot send `REJECT` to
`/approve`.

## 11. Frontend

| Route | What it shows |
| :--- | :--- |
| `/fixes` | the §67 dashboard: engine tallies, hypothesis table (category, risk, status, scope), patch table (size band, status, review state) with status filters |
| `/fixes/{id}` | the workspace: hypothesis and its evidence links, candidate switcher, patch summary and minimality band, the **diff viewer** with line numbers and add/remove highlighting, the **verification timeline** (§44) with per-stage status, command output and regression-test hash, the §64 checklist, the §69 explanation block, the audit trail, and the review actions |

The UI states the phase's boundary everywhere it matters: the patch disclaimer
sits with every candidate, and no merge/deploy/release affordance exists
anywhere. The Approve button appears only when the stored run holds a `VERIFIED`
verdict — the same allowlist the backend enforces.

## 12. Operating it

```bash
# API + web + database
DATABASE_PORT=5433 docker compose up --build -d

# the live Phase 7 gate (90 checks; 91 with DDL_PROBE=1)
bash infrastructure/e2e-smoke-phase7.sh
DDL_PROBE=1 bash infrastructure/e2e-smoke-phase7.sh
```

The gate builds a real four-commit git history in the container from the demo
checkout, registers and indexes it, ingests the failing stack trace, opens a
Phase 6 debug session, plans a fix, generates the patch, verifies it against the
repository's own test suite in a disposable workspace, reads back the artifacts
and report, records the human decision, regenerates a candidate, and finally
proves the original checkout is byte-identical and no workspace survives.

## 13. Limitations

* **Verification is only as good as the repository's own tests.** ARGUS adds a
  two-sided regression test derived from the patch, but it cannot know what the
  project's tests do not cover.
* **The deterministic generator knows a handful of defect shapes.** Anything else
  is refused, or requires a configured model.
* **Model-assisted generation is untrusted by construction.** Its output is
  parsed, scope-checked, safety-checked and then verified like any other patch —
  a configured model improves the *supply* of candidates, never their standing.
* **Reproduction results are supplied to verification, not derived by it.** The
  engine takes `baseline_reproduced` / `patched_still_reproduces` from the Phase 5
  experiment; running that experiment automatically from here is future work.
* **No language-agnostic build detection.** Discovery is marker-based for
  Python, Node and Make; other stacks report `BUILD_CONFIGURATION_UNKNOWN`
  instead of pretending.
* **Risk assessment is deterministic and conservative.** It describes what
  changed; it does not predict the probability of a production incident.

## 14. What comes next

Phase 8 — **Predictive Reliability**: trend, capacity and reliability prediction
from historical behaviour, and pre-incident signal detection. Phase 7 is
`generate → validate → test → reproduce → verify → review`, and deliberately not
`merge → deploy → remediate`.

---

**Related documents:** [phase-7 implementation report](phase7-implementation-report.md) ·
[roadmap](roadmap.md) · [data model](data-model.md) · [development](development.md)
