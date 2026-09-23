# Learning Governance

Phase 10 lets ARGUS form beliefs about its own history. This document is the
answer to the question that follows immediately: **what stops it from believing
something wrong, and what stops a wrong belief from doing damage?**

The short version: knowledge is derived, cited, checked, versioned, aged, and
gated. Nothing is deleted, nothing is auto-activated by default, and nothing
Phase 10 believes can execute anything.

---

## 1. Provenance: where a record came from

Every learning record carries a `DataProvenance` class, and the class decides
whether the record may be learned from at all:

| Provenance | Meaning | Learned from by default? |
| :--- | :--- | :--- |
| `OBSERVABILITY` | measured telemetry and derived rows | yes |
| `SYSTEM_GENERATED` | a deterministic ARGUS subsystem | yes |
| `HUMAN_ENTERED` | an operator's statement or decision | yes |
| `IMPORTED` | a verified external record | yes |
| `AI_GENERATED` | a model's hypothesis or prose | **no** |
| `MOCK` | synthetic data (demos, tests) | **no** |

The two exclusions are the data-poisoning defence (§76). An AI root-cause
hypothesis is not a confirmed root cause; a mock row is not history. A deployment
can consciously include them (`INTELLIGENCE_INCLUDE_AI_GENERATED`,
`INTELLIGENCE_INCLUDE_MOCK`), and the reason that switch exists is that some
deployments want the hypothesis in the corpus *labelled as a hypothesis* — the
default is to leave it out.

Different provenance for the same subject is a **different fact**, so it is a
different row. "A model thinks checkout failed" and "an operator confirmed
checkout failed" are not two sightings of one thing.

---

## 2. Lifecycle

```text
CANDIDATE ──► VALIDATING ──► VALIDATED ──► ACTIVE
    │              │              │            │
    │              │              │            └──► DEPRECATED ──► VALIDATED
    │              │              └──► CANDIDATE (a human asked for more evidence)
    │              └──► CANDIDATE (validation asked for more evidence)
    └──► REJECTED ──► SUPERSEDED                    ... ──► SUPERSEDED
```

The rules that make the diagram safe:

* **Belief only ever rises through validation.** `VALIDATED` is reached by the
  validation checks passing, never by a caller asserting it. `ACTIVE` is reached
  from `VALIDATED` only.
* **A human can always lower belief, never raise it past validation.** A review
  decision of `REQUEST_MORE_EVIDENCE` returns a row to `CANDIDATE`; it can never
  push a candidate to validated.
* **A rejected pattern stays rejected.** Only a human re-decides it. The one
  automatic exit is `SUPERSEDED` — retired to retired, which raises nothing.
* **Terminal means terminal.** `SUPERSEDED` has no outgoing transitions.
* **Only `VALIDATED` and `ACTIVE` knowledge may be cited by a recommendation.**
  A candidate may be *displayed* (a pattern explorer that hides weak evidence is
  lying by omission); it may not *advise*.

Every write of a knowledge status goes through the same state machine, so an
invalid state cannot be persisted by a caller that forgot the rules.

---

## 3. Validation

A mined pattern must survive seven checks against its **own** corpus — the
entries it was mined from, not the whole history:

| Check | What fails it |
| :--- | :--- |
| `sample_size` | too few observations for the confidence tier being claimed |
| `data_quality` | entries missing the fields the claim depends on |
| `temporal_consistency` | observations that do not hold together in time |
| `stability` | the pattern appears in one window and disappears in the adjacent one |
| `cross_component_consistency` | a component-scoped claim that other components contradict |
| `support_strength` | too small a fraction of compared cases actually support it |
| `false_discovery` | too many comparisons were made to find this one |

The verdict is stored **with its per-check detail**, so the UI can say *why* a
pattern is only a candidate rather than showing a bare "low confidence". A
failing pattern is not discarded: it becomes a candidate with a recorded reason.

Floors are configuration, not constants of nature:

```text
INTELLIGENCE_MIN_SAMPLES_CANDIDATE=3
INTELLIGENCE_MIN_SAMPLES_VALIDATION=5
INTELLIGENCE_MIN_SAMPLES_HIGH_CONFIDENCE=10
INTELLIGENCE_STABILITY_WINDOW_DAYS=30
INTELLIGENCE_MIN_SUPPORT_STRENGTH=0.5
```

Three observations is a policy choice. It is written down, and it is adjustable
per deployment, because pretending it is a statistical result would be worse
than admitting it is a threshold.

---

## 4. Human review

`POST /intelligence/knowledge/{id}/review` accepts one of four decisions:

| Decision | Effect | Allowed from |
| :--- | :--- | :--- |
| `APPROVE` | `VALIDATED → ACTIVE` | `VALIDATED` |
| `REJECT` | `→ REJECTED` (terminal for ARGUS) | `CANDIDATE`, `VALIDATING`, `VALIDATED` |
| `REQUEST_MORE_EVIDENCE` | `→ CANDIDATE` | `VALIDATING`, `VALIDATED` |
| `DEPRECATE` | `→ DEPRECATED` | `VALIDATING`, `VALIDATED`, `ACTIVE` |

Each decision writes a `KnowledgeReview` row with the actor, the reason and the
moment, and a `KnowledgeVersion` so the revision history survives. Rejection
records *why*, which is what makes a later "why did ARGUS stop believing this?"
answerable.

An `APPROVE` on a candidate is refused. The point of the review surface is to
decide about *validated* claims, not to hand a weak claim a promotion.

---

## 5. Autonomous activation

`INTELLIGENCE_AUTO_ACTIVATE_ENABLED` is **off by default**. When it is on, three
independent conditions must all hold before a row may reach `ACTIVE` without a
human:

1. the validation verdict says the knowledge is activatable;
2. the knowledge type is **not** high-impact —
   `REMEDIATION_PATTERN`, `RECOVERY_PATTERN` and `PREDICTIVE_PATTERN` always
   require a human, because they inform what ARGUS may be asked to execute and
   how it forecasts risk;
3. the confidence bucket is `HIGH`.

The activation is recorded with `reviewed_by = "ARGUS (policy-allowed autonomous
activation)"` and a reason string, so an autonomously activated row is
distinguishable from a human-approved one at a glance. There is no setting that
lets a low-confidence, high-impact pattern activate itself.

---

## 6. Ageing

Knowledge that new data stops confirming is **deprecated, not deleted**:

* if nothing confirms a `VALIDATED`/`ACTIVE` row for
  `INTELLIGENCE_KNOWLEDGE_STALE_AFTER_DAYS` (default 90), it becomes
  `DEPRECATED` with the reason and the date of its last confirmation;
* a deprecation can be reversed — `DEPRECATED → VALIDATED` — when the pattern is
  re-confirmed, so a seasonal or intermittent failure is not punished for being
  quiet;
* the row, its versions and its reviews all survive, which keeps "what did ARGUS
  believe, and when did it stop believing it?" answerable.

Modelling never happens here: no retraining, no weight updates, no threshold
tuning. Phase 10 learns *about* systems; it does not learn *a model* of them.

---

## 7. Versioning and supersession

Knowledge is versioned rather than overwritten. Re-deriving a pattern from a
larger corpus writes a new version and a note explaining what changed; a
materially different pattern supersedes the old row instead of editing it in
place, so a belief that was acted on six months ago can still be read as it was.

This matters most for the awkward case: the evidence changed, and the conclusion
reversed. A system that overwrites cannot show that; a ledger can.

---

## 8. Recommendations are advice, and outcomes are separate

A recommendation is an evidence-backed advisory, never an instruction:

* it names the knowledge and the evidence it came from, and cites nothing that
  is not at least `VALIDATED`;
* accepting or dismissing it is a human decision, recorded with the actor;
* **the outcome is a separate fact.** `ACCEPTED` means an engineer agreed;
  `EFFECTIVE` / `INEFFECTIVE` / `REGRESSION_CAUSING` mean the result was
  observed. The API never conflates the two, and the effectiveness view is where
  "were our recommendations worth following?" is actually answered.

Recommendations expire (`INTELLIGENCE_RECOMMENDATION_TTL_SECONDS`) rather than
lingering: an advisory about a state that no longer exists is noise.

---

## 9. Scope

Knowledge is **project-scoped**. A read requires `project_id`, and a project can
only see its own knowledge — cross-project leakage is impossible by
construction, not by discipline. `CROSS_PROJECT` exists as a scope value for a
future phase with a real multi-tenant story behind it; nothing today produces
one.

---

## 10. What this governance deliberately leaves out

* **No automatic policy change.** A learned pattern cannot edit a remediation
  policy, raise a risk ceiling, or enable a capability.
* **No cross-project pooling.** Even when two projects are the same software.
* **No statistical guarantees.** The floors and support thresholds are
  engineering policy; there is no false-discovery-rate control at the level of a
  journal, only a recorded count of comparisons.
* **No deletion.** Deprecated and superseded knowledge stays readable forever.

See [`docs/reliability-intelligence.md`](reliability-intelligence.md) for the
pipeline and [`docs/phase-10-report.md`](phase-10-report.md) for the delivery
report.
