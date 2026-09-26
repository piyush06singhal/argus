/**
 * Tests for the Phase 8 presentation helpers.
 *
 * The assertions enforce the phase's honesty rules, not just formatting:
 * UNKNOWN never renders as a healthy colour, a score without a number renders
 * without a number, accuracy carries its sample size, and no helper anywhere
 * produces causal or "will fail" language.
 */

import { describe, expect, it } from 'vitest';

import type {
  Backtest,
  EvaluationRun,
  Forecast,
  ForecastRiskLevel,
  ForecastSignal,
} from '../api-client';
import {
  confidenceLabel,
  dataQualityStyle,
  driftStyle,
  evaluationAccuracyView,
  latestForecasts,
  outcomeStyle,
  percentLabel,
  riskLevelStyle,
  riskPhrase,
  riskRank,
  riskScoreLabel,
  riskScoreOfTen,
  sortHeatmapCells,
  topSignals,
  trendArrow,
  warningStatusStyle,
  worstLevel,
  FORECAST_DISCLAIMER,
  UNKNOWN_RISK_NOTE,
} from '../reliability';

function signal(overrides: Partial<ForecastSignal> = {}): ForecastSignal {
  return {
    id: 's1',
    forecast_id: 'f1',
    project_id: 'p1',
    signal_type: 'LATENCY_INCREASING',
    severity: 'HIGH',
    contribution: 0.4,
    rank: 0,
    description: 'p95 latency rising',
    metric_name: 'http.checkout.latency.p95',
    observed_value: 730,
    baseline_value: 420,
    change_rate: 0.42,
    trend: 'RISING',
    evidence_ids: {},
    similar_incident_count: 0,
    created_at: '2026-09-21T00:00:00Z',
    ...overrides,
  };
}

describe('risk levels', () => {
  it('ranks UNKNOWN below LOW so absence of evidence is not comfort', () => {
    expect(riskRank('UNKNOWN')).toBeLessThan(riskRank('LOW'));
    expect(riskRank('CRITICAL')).toBeGreaterThan(riskRank('HIGH'));
  });

  it('renders UNKNOWN in the neutral style, never green', () => {
    expect(riskLevelStyle('UNKNOWN')).not.toMatch(/success/);
    expect(riskLevelStyle('LOW')).toMatch(/success/);
    expect(riskLevelStyle('CRITICAL')).toMatch(/error/);
  });

  it('phrases risk as risk, never as a promise of failure', () => {
    const phrase = riskPhrase('HIGH', '6 hours');
    expect(phrase).toContain('risk');
    expect(phrase.toLowerCase()).not.toContain('will fail');
    expect(phrase.toLowerCase()).not.toContain('will crash');
    expect(riskPhrase('UNKNOWN')).toContain('insufficient');
  });

  it('picks the worst level for a set', () => {
    expect(worstLevel(['LOW', 'HIGH', 'UNKNOWN'])).toBe('HIGH');
    expect(worstLevel(['UNKNOWN', 'UNKNOWN'])).toBe('UNKNOWN');
  });
});

describe('score rendering (no fake precision)', () => {
  it('renders at most one decimal', () => {
    expect(riskScoreLabel(0.7463829)).toBe('0.7');
    expect(riskScoreLabel(1)).toBe('1.0');
  });

  it('renders a missing score without inventing one', () => {
    expect(riskScoreLabel(null)).toBe('no score');
    expect(riskScoreOfTen(undefined)).toBe('—');
  });

  it('phrases confidence as bands with the reason attached', () => {
    expect(confidenceLabel(0.8, '5 signals')).toContain('moderate');
    expect(confidenceLabel(null, 'no data')).toBe('no data');
    expect(confidenceLabel(null, null)).toBe('no confidence claim');
  });

  it('renders percents without fake digits', () => {
    expect(percentLabel(0.423)).toBe('42%');
    expect(percentLabel(null)).toBe('—');
  });

  it('names trends as observations, not predictions', () => {
    expect(trendArrow('RISING')).toContain('rising');
    expect(trendArrow('UNKNOWN')).toContain('unknown');
  });
});

describe('data quality and calibration', () => {
  it('renders INSUFFICIENT in the neutral style, not green', () => {
    expect(dataQualityStyle('INSUFFICIENT')).not.toMatch(/success/);
    expect(dataQualityStyle('GOOD')).toMatch(/success/);
  });

  it('styles a false positive as a warning state, not hidden', () => {
    expect(outcomeStyle('FALSE_POSITIVE')).toMatch(/warning/);
    expect(warningStatusStyle('OPEN')).toMatch(/warning/);
  });

  it('styles flagged drift loudly', () => {
    expect(driftStyle('FLAGGED')).toMatch(/error/);
    expect(driftStyle('STABLE')).toMatch(/success/);
  });
});

describe('accuracy views carry their sample size', () => {
  it('reports no metrics without a run', () => {
    const view = evaluationAccuracyView(null);
    expect(view.precision).toBeUndefined();
    expect(view.sampleCount).toBe(0);
    expect(view.note).toMatch(/evaluation/i);
  });

  it('hides metrics below the sample floor and says why', () => {
    const run = {
      id: 'r1',
      status: 'INSUFFICIENT_SAMPLE',
      sample_count: 4,
      dataset_window_start: '2026-08-21T00:00:00Z',
      dataset_window_end: '2026-09-21T00:00:00Z',
      metrics: { precision: 0.9, recall: 0.8 },
      notes: ['insufficient sample size'],
    } as unknown as EvaluationRun;
    const view = evaluationAccuracyView(run);
    expect(view.precision).toBeUndefined();
    expect(view.sampleCount).toBe(4);
    expect(view.note).toMatch(/insufficient/i);
  });

  it('shows metrics with their sample size when justified', () => {
    const run = {
      id: 'r2',
      status: 'COMPLETED',
      sample_count: 284,
      dataset_window_start: '2026-08-21T00:00:00Z',
      dataset_window_end: '2026-09-21T00:00:00Z',
      metrics: { precision: 0.71, recall: 0.55, lead_time_seconds: 5400 },
      notes: [],
    } as unknown as EvaluationRun;
    const view = evaluationAccuracyView(run);
    expect(view.precision).toBe('71%');
    expect(view.recall).toBe('55%');
    expect(view.averageLeadTime).toBe('1.5 h');
    expect(view.sampleCount).toBe(284);
  });
});

describe('sorting and selection', () => {
  it('sorts heatmap cells worst-first', () => {
    const cells = [
      { worst_level: 'LOW' as ForecastRiskLevel, name: 'a' },
      { worst_level: 'CRITICAL' as ForecastRiskLevel, name: 'b' },
      { worst_level: 'UNKNOWN' as ForecastRiskLevel, name: 'c' },
    ];
    expect(sortHeatmapCells(cells).map((cell) => cell.name)).toEqual([
      'b',
      'a',
      'c',
    ]);
  });

  it('keeps only the newest forecast per (type, horizon)', () => {
    const forecast = (overrides: Partial<Forecast>): Forecast =>
      ({
        id: 'f',
        prediction_type: 'FAILURE_RISK',
        forecast_horizon: 'ONE_HOUR',
        risk_level: 'LOW',
        ...overrides,
      }) as Forecast;
    const a = forecast({ id: 'a', risk_level: 'LOW' });
    const b = forecast({ id: 'b', risk_level: 'CRITICAL' });
    const c = forecast({ id: 'c', forecast_horizon: 'SIX_HOURS' });
    // The API returns newest-first, so the *first* row per key is current.
    const latest = latestForecasts([b, a, c]);
    expect(latest).toHaveLength(2);
    expect(latest[0].id).toBe('b');
    expect(latest.map((f) => f.id)).not.toContain('a');
  });

  it('returns the top-ranked signals in order', () => {
    const signals = [
      signal({ rank: 2, description: 'third' }),
      signal({ rank: 0, description: 'first' }),
      signal({ rank: 1, description: 'second' }),
    ];
    expect(topSignals(signals, 2).map((s) => s.description)).toEqual([
      'first',
      'second',
    ]);
  });
});

describe('the disclaimers', () => {
  it('state the phase boundary in user-facing language', () => {
    expect(FORECAST_DISCLAIMER).toMatch(/not a promise of failure/);
    expect(FORECAST_DISCLAIMER).toMatch(/human decides/);
    expect(UNKNOWN_RISK_NOTE).toMatch(/not the same as\s+LOW risk/i);
  });
});
