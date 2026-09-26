import { describe, expect, it } from 'vitest';

import type {
  ReproductionComparison,
  ReproductionFault,
  ReproductionValidation,
} from '../api-client';
import {
  captureCoverage,
  determinismNote,
  EXPERIMENT_HAPPY_PATH,
  experimentStatusStyle,
  failureClassLabel,
  faultSummary,
  formatBytes,
  formatComponents,
  formatOverlap,
  formatSeconds,
  formatSequence,
  isInconclusiveClass,
  isRunningStatus,
  isTerminalStatus,
  outcomeStyle,
  progressPercent,
  progressStep,
  REPRODUCTION_DISCLAIMER,
  replayStatusStyle,
  resultStyle,
  scoreLabel,
  scoreStyle,
  similarityDimensionRows,
  similarityHeadline,
  sortHistory,
  statusNarrative,
  TERMINAL_STATUSES,
} from '../reproduction';

function comparison(
  overrides: Partial<ReproductionComparison> = {}
): ReproductionComparison {
  return {
    id: 'c1',
    run_id: 'r1',
    overall_similarity: 'HIGH',
    similarity_score: 0.91,
    result: 'SUCCESSFUL',
    dimensions: {
      temporal: 0.9,
      component: 1,
      error: 0.91,
      latency: 0.84,
      trace_topology: 0.88,
      failure_sequence: 0.9,
      recovery: null,
    },
    formula_reference: 'docs/phase-5.md#similarity',
    sequence_original: ['db', 'inventory', 'checkout'],
    sequence_reproduced: ['db', 'inventory', 'checkout'],
    matched_components: ['db', 'inventory', 'checkout'],
    missing_components: [],
    extra_components: [],
    component_overlap: { matched: 3, total: 3 },
    created_at: '2026-09-20T00:00:00Z',
    ...overrides,
  } as ReproductionComparison;
}

describe('phase 5 · experiment status mapping', () => {
  it('orders the happy path exactly as the backend enum walks it', () => {
    expect(EXPERIMENT_HAPPY_PATH[0]).toBe('PLANNED');
    expect(EXPERIMENT_HAPPY_PATH.at(-1)).toBe('COMPLETED');
    expect(EXPERIMENT_HAPPY_PATH).toHaveLength(9);
  });

  it('treats only failure exits as terminal', () => {
    for (const status of TERMINAL_STATUSES) {
      expect(isTerminalStatus(status)).toBe(true);
    }
    expect(isTerminalStatus('PLANNED')).toBe(false);
    expect(isTerminalStatus('RUNNING')).toBe(false);
    expect(isTerminalStatus('COMPLETED')).toBe(true);
    // CANCELLED and TIMED_OUT are exits, not running states.
    expect(isRunningStatus('CANCELLED')).toBe(false);
    expect(isRunningStatus('TIMED_OUT')).toBe(false);
    // PLANNED is neither running nor terminal: it awaits confirmation.
    expect(isRunningStatus('PLANNED')).toBe(false);
    expect(isRunningStatus('REPLAYING')).toBe(true);
  });

  it('reports a 1-based step and a bounded percent', () => {
    expect(progressStep('PLANNED')).toBe(1);
    expect(progressStep('READY')).toBe(4);
    expect(progressPercent('PLANNED')).toBe(11);
    expect(progressPercent('COMPLETED')).toBe(100);
    // A failed experiment still shows how far it got, not 0%.
    expect(progressStep('FAILED')).toBe(EXPERIMENT_HAPPY_PATH.length);
  });

  it('falls back to a neutral style for an unknown status', () => {
    expect(experimentStatusStyle('COMPLETED')).toContain('argus-success');
    expect(
      experimentStatusStyle('NOT_A_STATUS' as unknown as 'COMPLETED')
    ).toBe(experimentStatusStyle('PLANNED'));
  });

  it('narrates a failure classification alongside the status', () => {
    expect(
      statusNarrative({
        status: 'FAILED',
        result: 'INCONCLUSIVE',
        failure_classification: 'TIMEOUT',
      })
    ).toBe('Failed — Timed out');
    expect(
      statusNarrative({
        status: 'COMPLETED',
        result: 'SUCCESSFUL',
        failure_classification: null,
      })
    ).toBe('Completed — SUCCESSFUL');
  });
});

describe('phase 5 · result and outcome are distinct', () => {
  it('gives a null result the same visual weight as a support', () => {
    expect(resultStyle('FAILED')).not.toContain('argus-error');
    expect(resultStyle('INCONCLUSIVE')).toContain('argus-info');
    expect(resultStyle('SUCCESSFUL')).toContain('argus-success');
    expect(outcomeStyle('NOT_SUPPORTED')).toContain('argus-error');
  });

  it('names the classifications that make a null result inconclusive', () => {
    expect(isInconclusiveClass('TIMEOUT')).toBe(true);
    expect(isInconclusiveClass('ENVIRONMENT_ERROR')).toBe(true);
    expect(isInconclusiveClass('NO_FAILURE_OBSERVED')).toBe(false);
    expect(isInconclusiveClass(null)).toBe(false);
    expect(failureClassLabel('DEPENDENCY_UNAVAILABLE')).toBe(
      'Dependency unavailable'
    );
  });

  it('always carries the experiment caveat', () => {
    expect(REPRODUCTION_DISCLAIMER).toContain('not a proof');
    expect(REPRODUCTION_DISCLAIMER).toContain('refute');
  });
});

describe('phase 5 · similarity is bucketed, never fake precision', () => {
  it('labels every dimension the comparator scores', () => {
    const rows = similarityDimensionRows(comparison().dimensions);
    expect(rows.map((row) => row.label)).toEqual([
      'Timing',
      'Components',
      'Errors',
      'Latency',
      'Trace topology',
      'Failure sequence',
      'Recovery',
    ]);
  });

  it('renders an unmeasurable dimension as not measured, not as zero', () => {
    const rows = similarityDimensionRows({ recovery: null });
    expect(rows[0].score).toBeNull();
    expect(scoreLabel(rows[0].score)).toBe('not measured');
    expect(rows[0].style).toContain('slate');
  });

  it('buckets a score coarsely', () => {
    expect(scoreLabel(0.84)).toBe('high');
    expect(scoreLabel(0.6)).toBe('partial');
    expect(scoreLabel(0.2)).toBe('low');
    expect(scoreStyle(0.84)).toContain('argus-success');
    expect(scoreStyle(0.6)).toContain('argus-warning');
    expect(scoreStyle(0.2)).toContain('argus-error');
  });

  it('labels dimensions the backend reports with upper-case enum keys', () => {
    // The stored dimensions are keyed by `ComparisonDimension` (`ERROR`, ...), so
    // a case-sensitive map would show raw enum names in the UI.
    const rows = similarityDimensionRows({
      FAILURE_SEQUENCE: 0.9,
      TRACE_TOPOLOGY: 0.88,
      RECOVERY: 0.5,
    });
    expect(rows.map((row) => row.label)).toEqual([
      'Failure sequence',
      'Trace topology',
      'Recovery',
    ]);
    expect(rows[0].score).toBeCloseTo(0.9);
  });

  it('returns no rows when the comparator produced nothing', () => {
    expect(similarityDimensionRows(null)).toEqual([]);
    expect(similarityDimensionRows(undefined)).toEqual([]);
  });

  it('skips a non-numeric dimension rather than printing NaN', () => {
    const rows = similarityDimensionRows({
      component: 'lots' as unknown as number,
    });
    expect(rows[0].score).toBeNull();
  });

  it('summarises the score with its bucket and result', () => {
    expect(similarityHeadline(comparison())).toBe(
      'Similarity HIGH · mean 0.91 · result SUCCESSFUL'
    );
    expect(similarityHeadline(null)).toBeNull();
  });

  it('formats an overlap as a ratio, and unknown counts as a dash', () => {
    expect(formatOverlap({ matched: 3, total: 5 })).toBe('3 / 5');
    expect(formatOverlap({ matched: 2, expected: 4 })).toBe('2 / 4');
    expect(formatOverlap(null)).toBe('—');
    expect(formatOverlap({ matched: 2 })).toBe('—');
  });

  it('formats the failure sequences and component lists', () => {
    expect(formatSequence(['db', 'inventory', 'checkout'])).toBe(
      'db → inventory → checkout'
    );
    expect(formatSequence([])).toBe('—');
    expect(formatComponents(['checkout', 'inventory'])).toBe(
      'checkout, inventory'
    );
    expect(formatComponents(null)).toBe('—');
  });
});

describe('phase 5 · defects cannot be read as natural failures', () => {
  const fault = (overrides: Partial<ReproductionFault> = {}) =>
    ({
      id: 'f1',
      fault_type: 'LATENCY',
      target: 'datastore',
      scope: 'sandbox',
      trigger: 'IMMEDIATE',
      parameters: null,
      duration_ms: 1500,
      intensity: null,
      status: 'COMPLETED',
      injected: true,
      started_at: null,
      ended_at: null,
      requests_affected: 3,
      result: null,
      created_at: '2026-09-20T00:00:00Z',
      ...overrides,
    }) as ReproductionFault;

  it('states that a fault was injected, and how much it touched', () => {
    expect(faultSummary(fault())).toBe(
      'Latency → datastore · 1500 ms · injected · 3 request(s) affected'
    );
  });

  it('states plainly when a fault was not injected', () => {
    const summary = faultSummary(
      fault({ injected: false, status: 'SKIPPED', requests_affected: 0 })
    );
    expect(summary).toContain('not injected');
    expect(summary).not.toContain('affected');
  });

  it('shows intensity when it was set', () => {
    expect(faultSummary(fault({ intensity: 0.5 }))).toContain('intensity 0.5');
  });

  it('styles fault and replay statuses distinctly', () => {
    expect(replayStatusStyle('REJECTED')).toContain('argus-warning');
    expect(replayStatusStyle('FAILED')).toContain('argus-error');
    expect(replayStatusStyle('PENDING')).toContain('slate');
  });
});

describe('phase 5 · telemetry, determinism and formatting', () => {
  it('reports capture coverage as an explicit ratio', () => {
    expect(captureCoverage(4, 5).text).toBe(
      '4 / 5 expected signal(s) captured'
    );
    expect(captureCoverage(4, 5).complete).toBe(false);
    expect(captureCoverage(5, 5).complete).toBe(true);
    expect(captureCoverage(0, 0).text).toContain('no expected signals');
  });

  it('frames repeatability as an experiment observation, not a probability', () => {
    const validation = {
      determinism: {
        runs: 4,
        successful_runs: 3,
        partial_runs: 0,
        reproduction_rate: 0.75,
        classification: 'INTERMITTENT',
        note: 'it is not a probability that the hypothesis is true.',
      },
    } as unknown as ReproductionValidation;
    const note = determinismNote(validation);
    expect(note).toContain('INTERMITTENT');
    expect(note).toContain('3 of 4');
    expect(note).toContain('75%');
    expect(note).toContain('not a probability');
  });

  it('counts partial reproductions towards the observed rate', () => {
    const validation = {
      determinism: {
        runs: 3,
        successful_runs: 1,
        partial_runs: 1,
        reproduction_rate: 0.6667,
        classification: 'INTERMITTENT',
      },
    } as unknown as ReproductionValidation;
    expect(determinismNote(validation)).toContain('2 of 3');
  });

  it('states plainly when no run was comparable', () => {
    const validation = {
      determinism: { runs: 0, reproduction_rate: null, classification: 'NOT_RUN' },
    } as unknown as ReproductionValidation;
    expect(determinismNote(validation)).toContain('NOT_RUN');
    expect(determinismNote(validation)).toContain('no rate');
  });

  it('returns no determinism note when the counts are absent', () => {
    expect(determinismNote(null)).toBeNull();
    expect(
      determinismNote({ determinism: {} } as unknown as ReproductionValidation)
    ).toBeNull();
  });

  it('formats durations and byte sizes', () => {
    expect(formatSeconds(9.54)).toBe('9.5 s');
    expect(formatSeconds(125)).toBe('2m 5s');
    expect(formatSeconds(null)).toBe('—');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2.0 KB');
    expect(formatBytes(3 * 1024 * 1024)).toBe('3.00 MB');
    expect(formatBytes(undefined)).toBe('—');
  });

  it('orders history newest version first without mutating the input', () => {
    const items = [
      { experiment_version: 1 },
      { experiment_version: 3 },
      { experiment_version: 2 },
    ];
    expect(sortHistory(items).map((item) => item.experiment_version)).toEqual([
      3, 2, 1,
    ]);
    expect(items[0].experiment_version).toBe(1);
  });
});
