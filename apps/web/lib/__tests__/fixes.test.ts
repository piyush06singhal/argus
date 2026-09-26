import { describe, expect, it } from 'vitest';

import type {
  FixHypothesis,
  Patch,
  PatchDetail,
  PatchVerificationRun,
  ReviewState,
} from '../api-client';
import {
  allowedReviewActions,
  auditTrail,
  diffPaths,
  explanationRows,
  isApprovable,
  outOfScopePaths,
  parseUnifiedDiff,
  patchSizeSummary,
  sizeBand,
  tamperingLabel,
  verificationChecks,
  verificationTimeline,
} from '../fixes';

const DIFF = `--- a/services/inventory/repository.py
+++ b/services/inventory/repository.py
@@ -14,4 +14,4 @@ class InventoryRepository:
     """Reads inventory rows."""
 
-DB_TIMEOUT_SECONDS = 0.25
+DB_TIMEOUT_SECONDS = 2.0
`;

function hypothesis(overrides: Partial<FixHypothesis> = {}): FixHypothesis {
  return {
    id: 'h1',
    project_id: 'p1',
    incident_id: 'i1',
    title: 'Inventory timeout below query latency',
    description: 'the timeout is below the query latency',
    proposed_change: 'Restore the timeout budget',
    category: 'TIMEOUT_FIX',
    scope_files: ['services/inventory/repository.py'],
    excluded_paths: ['infrastructure/'],
    supporting_evidence: [],
    target_symbols: ['DB_TIMEOUT_SECONDS'],
    risk_level: 'MEDIUM',
    confidence: 'MEDIUM',
    status: 'PATCH_GENERATED',
    created_at: '2026-09-20T00:00:00Z',
    ...overrides,
  };
}

function patch(overrides: Partial<PatchDetail> = {}): PatchDetail {
  return {
    id: 'p1',
    project_id: 'p1',
    fix_hypothesis_id: 'h1',
    patch_format: 'UNIFIED_DIFF',
    changed_files: 1,
    lines_added: 1,
    lines_removed: 1,
    symbols_modified: ['DB_TIMEOUT_SECONDS'],
    affected_paths: ['services/inventory/repository.py'],
    generated_by: 'deterministic',
    status: 'VERIFIED',
    explanation: {
      what_changed: 'restore the timeout',
      why_changed: 'the configured timeout is below the query latency',
      evidence: ['TRACE:782'],
      expected_behavior: ['checkout completes'],
      unchanged_behavior: 'the public API is unchanged',
    },
    failure_reason: null,
    created_at: '2026-09-20T00:00:00Z',
    review_state: 'AWAITING_REVIEW',
    patch_content: DIFF,
    review_actions: [],
    verification_runs: [],
    workspaces: [],
    ...overrides,
  };
}

function run(overrides: Partial<PatchVerificationRun> = {}): PatchVerificationRun {
  return {
    id: 'v1',
    patch_id: 'p1',
    status: 'VERIFIED',
    level: 'FULLY_VERIFIED',
    confidence: 'HIGH',
    confidence_reason: null,
    tampering_flag: 'NONE',
    verification_env_intact: true,
    baseline_failure_reproduced: true,
    patched_failure_reproduced: false,
    regression_detected: false,
    started_at: '2026-09-20T00:00:00Z',
    completed_at: '2026-09-20T00:01:00Z',
    duration_ms: 60000,
    verdict_reason: 'FULLY_VERIFIED',
    evidence: {
      applied: true,
      applied_files: ['services/inventory/repository.py'],
      regression: {
        verdict: 'VALID',
        failed_on_base: true,
        passed_on_patched: true,
      },
      reproduction: { baseline_reproduced: true, patched_reproduced: false },
      comparison: { regressions: [], summary: 'within thresholds' },
    },
    test_runs: [
      {
        id: 't1',
        kind: 'STATIC',
        command_key: 'python_syntax',
        command_resolved: 'python -m py_compile',
        unknown_configuration: false,
        exit_code: 0,
        timed_out: false,
        duration_ms: 120,
        output_tail: null,
        selected_tests: [],
        selection_reason: null,
      },
      {
        id: 't2',
        kind: 'UNIT',
        command_key: 'python_tests_selected',
        command_resolved: 'pytest -q tests/test_checkout.py',
        unknown_configuration: false,
        exit_code: 0,
        timed_out: false,
        duration_ms: 900,
        output_tail: '1 passed',
        selected_tests: ['tests/test_checkout.py'],
        selection_reason: 'no test file references a changed module',
      },
    ],
    regression_tests: [
      {
        id: 'r1',
        name: 'regression:DB_TIMEOUT_SECONDS',
        file_path: 'tests/test_regression_DB_TIMEOUT_SECONDS.py',
        origin: 'generated',
        ran_on_base: true,
        failed_on_base: true,
        ran_on_patched: true,
        passed_on_patched: true,
        valid: true,
        invalid_reason: null,
        content_hash: 'abc123def456',
      },
    ],
    comparisons: [],
    ...overrides,
  };
}

describe('diff parsing (§43)', () => {
  it('reads paths, hunk lines and line numbers', () => {
    const files = parseUnifiedDiff(DIFF);
    expect(files).toHaveLength(1);
    expect(files[0].path).toBe('services/inventory/repository.py');
    expect(files[0].additions).toBe(1);
    expect(files[0].deletions).toBe(1);

    const removed = files[0].hunks[0].lines.find(
      (line) => line.kind === 'deletion'
    );
    const added = files[0].hunks[0].lines.find((line) => line.kind === 'addition');
    expect(removed?.content).toContain('0.25');
    expect(removed?.oldLine).toBeGreaterThan(0);
    expect(removed?.newLine).toBeUndefined();
    expect(added?.content).toContain('2.0');
    expect(added?.newLine).toBeGreaterThan(0);
    expect(added?.oldLine).toBeUndefined();
  });

  it('never throws on a patch it cannot parse', () => {
    expect(parseUnifiedDiff('not a diff at all')).toEqual([]);
    expect(diffPaths('')).toEqual([]);
  });

  it('tolerates several files in one patch', () => {
    const two = `${DIFF}--- a/b.py
+++ b/b.py
@@ -1,1 +1,1 @@
-x = 1
+x = 2
`;
    expect(diffPaths(two)).toEqual([
      'services/inventory/repository.py',
      'b.py',
    ]);
  });
});

describe('scope and size (§9, §10)', () => {
  it('reports an out-of-scope path rather than hiding it', () => {
    const item = patch({ affected_paths: ['services/other/thing.py'] });
    expect(outOfScopePaths(item, hypothesis())).toEqual([
      'services/other/thing.py',
    ]);
  });

  it('treats a scope-prefixed path as in scope', () => {
    const item = patch({ affected_paths: ['services/inventory/repository.py'] });
    expect(outOfScopePaths(item, hypothesis())).toEqual([]);
  });

  it('bands single-file patches as minimal', () => {
    expect(sizeBand(patch()).label).toBe('Minimal');
    expect(sizeBand(patch({ changed_files: 25 })).label).toMatch(/Very broad/);
  });

  it('summarises size from the stored counts', () => {
    expect(patchSizeSummary(patch())).toBe('1 file changed, +1 −1');
  });
});

describe('verification timeline (§44, §64)', () => {
  it('walks a fully verified run end to end', () => {
    const stages = verificationTimeline(run());
    const byKey = Object.fromEntries(stages.map((stage) => [stage.key, stage]));
    expect(byKey.patch_applied.status).toBe('PASS');
    expect(byKey.static.status).toBe('PASS');
    expect(byKey.build.status).toBe('SKIPPED');
    expect(byKey.tests.status).toBe('PASS');
    expect(byKey.regression.status).toBe('PASS');
    expect(byKey.baseline_reproduction.status).toBe('PASS');
    expect(byKey.patched_reproduction.status).toBe('PASS');
    expect(byKey.comparison.status).toBe('PASS');
    expect(byKey.verification.status).toBe('PASS');
    expect(verificationChecks(run()).every((check) => check.ok)).toBe(true);
  });

  it('shows a regression as a failed rung, not a partial pass', () => {
    const failing = run({
      status: 'NOT_VERIFIED',
      level: 'REPRODUCTION_VALIDATED',
      regression_detected: true,
      evidence: {
        applied: true,
        regression: {
          verdict: 'VALID',
          failed_on_base: true,
          passed_on_patched: true,
        },
        reproduction: { baseline_reproduced: true, patched_reproduced: false },
        comparison: { regressions: ['memory_mb increased by 100%'] },
      },
    });
    const comparison = verificationTimeline(failing).find(
      (stage) => stage.key === 'comparison'
    );
    expect(comparison?.status).toBe('FAIL');
    expect(comparison?.detail).toContain('memory_mb');
    expect(
      verificationChecks(failing).find((check) => check.label === 'Final verification')
        ?.ok
    ).toBe(false);
  });

  it('marks the patched reproduction as failed when the failure persists (§63)', () => {
    const persisting = run({
      status: 'NOT_VERIFIED',
      patched_failure_reproduced: true,
      evidence: {
        applied: true,
        reproduction: { baseline_reproduced: true, patched_reproduced: true },
      },
    });
    const stage = verificationTimeline(persisting).find(
      (item) => item.key === 'patched_reproduction'
    );
    expect(stage?.status).toBe('FAIL');
    expect(stage?.detail).toMatch(/still reproduces/);
  });

  it('keeps a never-verified patch out of the PASS column', () => {
    const stages = verificationTimeline(null);
    expect(stages.every((stage) => stage.status !== 'PASS')).toBe(true);
    // Nothing was evaluated, so the §64 checklist is empty — not all-green.
    expect(verificationChecks(null)).toEqual([]);
  });
});

describe('tampering and review (§55, §71)', () => {
  it('names each tampering flag plainly', () => {
    expect(tamperingLabel('TEST_DELETED')).toMatch(/deleted/);
    expect(tamperingLabel('CI_MODIFIED')).toMatch(/CI/);
    expect(tamperingLabel('NONE')).toMatch(/No tampering/);
  });

  it('offers approval only for a verified patch and verified run', () => {
    expect(isApprovable(patch(), run())).toBe(true);
    expect(
      isApprovable(patch({ status: 'GENERATED' }), run({ status: 'NOT_VERIFIED' }))
    ).toBe(false);
    const actions = allowedReviewActions(
      patch({ status: 'GENERATED' }),
      run({ status: 'NOT_VERIFIED' }),
      'AWAITING_REVIEW'
    );
    expect(actions).not.toContain('APPROVE');
    expect(actions).toContain('REJECT');
  });

  it('offers no actions once a decision is recorded', () => {
    const terminal: ReviewState[] = ['APPROVED', 'REJECTED'];
    for (const state of terminal) {
      expect(allowedReviewActions(patch(), run(), state)).toEqual([]);
    }
  });
});

describe('explanation and audit (§69, §70)', () => {
  it('answers every question, stating gaps explicitly', () => {
    const rows = explanationRows(patch(), hypothesis(), run());
    expect(rows.map((row) => row.question)).toContain(
      'What reproduction validates it?'
    );
    const evidence = rows.find((row) => row.question.includes('evidence'));
    expect(evidence?.answer).toContain('TRACE:782');
    const sparse = explanationRows(patch({ explanation: {} }), null, null);
    expect(sparse.every((row) => row.answer.length > 0)).toBe(true);
  });

  it('renders the audit trail from stored actions only', () => {
    const detail = patch({
      review_actions: [
        {
          id: 'a1',
          patch_id: 'p1',
          action: 'APPROVE',
          actor: 'engineer',
          reason: 'evidence complete',
          new_patch_id: null,
          audit_metadata: {},
          created_at: '2026-09-20T01:00:00Z',
        },
      ],
    });
    const trail = auditTrail(detail);
    expect(trail).toHaveLength(1);
    expect(trail[0].action).toBe('APPROVE');
    expect(auditTrail(patch({ review_actions: [] }))).toEqual([]);
  });
});
