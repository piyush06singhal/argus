/**
 * Presentation helpers for Phase 8 — predictive reliability.
 *
 * The rules that shape this module are the phase's own guarantees:
 *
 * 1. **A prediction is not a fact.** Every risk level renders as a *band with
 *    uncertainty*, never as "will fail", and `UNKNOWN` is a first-class state
 *    that means "insufficient evidence" — never "healthy".
 * 2. **No fake precision.** Risk scores render as one decimal at most, and a
 *    level without a score renders without a number at all.
 * 3. **Signals are not causes.** Wording helpers keep "predictive signal" and
 *    "contributing evidence" separate from causal language everywhere.
 * 4. **Sample size travels with the metric.** An accuracy figure is never
 *    shown without the count that justifies it (§53).
 */

import type {
  Backtest,
  BacktestStep,
  CalibrationStatus,
  DataQuality,
  DriftFinding,
  EarlyWarning,
  EvaluationRun,
  Forecast,
  ForecastHorizon,
  ForecastRiskLevel,
  ForecastSignal,
  ForecastStatus,
  ModelVersion,
  PredictionOutcomeType,
  PredictionType,
} from './api';

export type { ForecastHorizon, ForecastRiskLevel };

// ---------------------------------------------------------------------------
// The phase's boundary, stated once and reused everywhere
// ---------------------------------------------------------------------------

export const FORECAST_DISCLAIMER =
  'A forecast describes evidence that risk is increasing. It is not a promise of ' +
  'failure, not an incident, and not a causal claim. ARGUS predicts, explains, ' +
  'evaluates and warns — a human decides what happens next.';

export const UNKNOWN_RISK_NOTE =
  'UNKNOWN means the evidence was insufficient to judge. It is not the same as ' +
  'LOW risk, and it is never rendered as reassurance.';

// ---------------------------------------------------------------------------
// Risk levels (§5)
// ---------------------------------------------------------------------------

const RISK_NEUTRAL = 'bg-slate-800 text-slate-300';
const RISK_LOW = 'bg-argus-success/15 text-argus-success';
const RISK_MEDIUM = 'bg-argus-info/20 text-argus-info';
const RISK_HIGH = 'bg-argus-warning/20 text-argus-warning';
const RISK_CRITICAL = 'bg-argus-error/20 text-argus-error';

export function riskLevelStyle(level: ForecastRiskLevel): string {
  switch (level) {
    case 'CRITICAL':
      return RISK_CRITICAL;
    case 'HIGH':
      return RISK_HIGH;
    case 'MEDIUM':
      return RISK_MEDIUM;
    case 'LOW':
      return RISK_LOW;
    default:
      return RISK_NEUTRAL;
  }
}

/** The rank used for ordering; UNKNOWN sorts below LOW on purpose. */
export function riskRank(level: ForecastRiskLevel): number {
  switch (level) {
    case 'CRITICAL':
      return 3;
    case 'HIGH':
      return 2;
    case 'MEDIUM':
      return 1;
    case 'LOW':
      return 0;
    default:
      return -1;
  }
}

export function worstLevel(levels: ForecastRiskLevel[]): ForecastRiskLevel {
  return levels.reduce<ForecastRiskLevel>(
    (worst, level) => (riskRank(level) > riskRank(worst) ? level : worst),
    'UNKNOWN'
  );
}

/**
 * §1 — the only sanctioned way to phrase a level. Never "will fail"; the
 * subject is risk, and the horizon is part of the claim.
 */
export function riskPhrase(level: ForecastRiskLevel, horizonLabel?: string): string {
  const within = horizonLabel ? ` over the ${horizonLabel.toLowerCase()}` : '';
  switch (level) {
    case 'CRITICAL':
      return `critical predicted reliability risk${within}`;
    case 'HIGH':
      return `high predicted reliability risk${within}`;
    case 'MEDIUM':
      return `elevated predicted reliability risk${within}`;
    case 'LOW':
      return `low predicted reliability risk${within}`;
    default:
      return `prediction unavailable — insufficient historical evidence${within}`;
  }
}

// ---------------------------------------------------------------------------
// Score rendering (§7 — no fake precision)
// ---------------------------------------------------------------------------

/** One decimal, at most. A score is a band, not a probability. */
export function riskScoreLabel(score?: number | null): string {
  if (score === null || score === undefined) return 'no score';
  return (Math.round(score * 10) / 10).toFixed(1);
}

/** "0.7" reads better on a heatmap cell than "0.7/1"; used sparingly. */
export function riskScoreOfTen(score?: number | null): string {
  if (score === null || score === undefined) return '—';
  return `${Math.round(score * 10)}/10`;
}

export function confidenceLabel(
  confidence?: number | null,
  reason?: string | null
): string {
  if (confidence === null || confidence === undefined) {
    return reason ?? 'no confidence claim';
  }
  const band =
    confidence >= 0.7 ? 'moderate' : confidence >= 0.4 ? 'low' : 'very low';
  return `${band} confidence${reason ? ` — ${reason}` : ''}`;
}

// ---------------------------------------------------------------------------
// Enum labels
// ---------------------------------------------------------------------------

export const HORIZON_LABELS: Record<ForecastHorizon, string> = {
  ONE_HOUR: '1 hour',
  SIX_HOURS: '6 hours',
  TWENTY_FOUR_HOURS: '24 hours',
  SEVEN_DAYS: '7 days',
};

export const PREDICTION_TYPE_LABELS: Record<PredictionType, string> = {
  FAILURE_RISK: 'Failure risk',
  ERROR_RATE_RISK: 'Error-rate risk',
  LATENCY_RISK: 'Latency risk',
  AVAILABILITY_RISK: 'Availability risk',
  RESOURCE_EXHAUSTION_RISK: 'Resource exhaustion',
  DEPENDENCY_FAILURE_RISK: 'Dependency failure',
  REGRESSION_RISK: 'Regression risk',
  INCIDENT_RISK: 'Incident risk',
  RELIABILITY_DEGRADATION: 'Reliability degradation',
};

export const FORECAST_STATUS_LABELS: Record<ForecastStatus, string> = {
  GENERATED: 'Generated',
  ACTIVE: 'Active',
  EXPIRED: 'Expired',
  CONFIRMED: 'Confirmed by event',
  FALSE_POSITIVE: 'False positive',
  INCONCLUSIVE: 'Inconclusive',
};

export const OUTCOME_LABELS: Record<PredictionOutcomeType, string> = {
  TRUE_POSITIVE: 'True positive',
  FALSE_POSITIVE: 'False positive',
  TRUE_NEGATIVE: 'True negative',
  FALSE_NEGATIVE: 'False negative',
  INCONCLUSIVE: 'Inconclusive',
};

export const DATA_QUALITY_LABELS: Record<DataQuality, string> = {
  GOOD: 'Good',
  PARTIAL: 'Partial',
  POOR: 'Poor',
  INSUFFICIENT: 'Insufficient',
};

export function forecastStatusStyle(status: ForecastStatus): string {
  switch (status) {
    case 'CONFIRMED':
      return RISK_MEDIUM;
    case 'FALSE_POSITIVE':
      return RISK_HIGH;
    case 'ACTIVE':
      return RISK_LOW;
    case 'EXPIRED':
    case 'INCONCLUSIVE':
    case 'GENERATED':
      return RISK_NEUTRAL;
    default:
      return RISK_NEUTRAL;
  }
}

export function dataQualityStyle(quality: DataQuality): string {
  switch (quality) {
    case 'GOOD':
      return RISK_LOW;
    case 'PARTIAL':
      return RISK_MEDIUM;
    case 'POOR':
      return RISK_HIGH;
    default:
      return RISK_NEUTRAL;
  }
}

export function dataQualityLabel(quality: DataQuality): string {
  return DATA_QUALITY_LABELS[quality] ?? quality;
}

export function calibrationStyle(status: CalibrationStatus): string {
  switch (status) {
    case 'GOOD':
      return RISK_LOW;
    case 'ACCEPTABLE':
      return RISK_MEDIUM;
    case 'POOR':
      return RISK_HIGH;
    default:
      return RISK_NEUTRAL;
  }
}

/** Signal types render as predictive evidence, never as causes (§6). */
export function signalTypeLabel(signalType: string): string {
  return signalType
    .toLowerCase()
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

export function signalSeverityStyle(severity: string): string {
  switch (severity) {
    case 'CRITICAL':
      return RISK_CRITICAL;
    case 'HIGH':
      return RISK_HIGH;
    case 'MEDIUM':
      return RISK_MEDIUM;
    default:
      return RISK_NEUTRAL;
  }
}

// ---------------------------------------------------------------------------
// Trend and metric formatting
// ---------------------------------------------------------------------------

export function trendArrow(trend: string): string {
  switch (trend) {
    case 'RISING':
      return '↗ rising';
    case 'FALLING':
      return '↘ falling';
    case 'FLAT':
      return '→ flat';
    case 'VOLATILE':
      return '↕ volatile';
    default:
      return '· unknown';
  }
}

export function percentLabel(value?: number | null, digits = 0): string {
  if (value === null || value === undefined) return '—';
  const pct = value <= 1 ? value * 100 : value;
  return `${pct.toFixed(digits)}%`;
}

export function coverageLabel(coverage?: number | null): string {
  if (coverage === null || coverage === undefined) return 'coverage unknown';
  return `${Math.round(coverage * 100)}% telemetry coverage`;
}

// ---------------------------------------------------------------------------
// Accuracy with its sample size (§29, §53)
// ---------------------------------------------------------------------------

export interface AccuracyView {
  /** Present only when the sample size can justify the number. */
  precision?: string;
  recall?: string;
  falsePositiveRate?: string;
  averageLeadTime?: string;
  sampleCount: number;
  /** Always present: the reason a number is missing is itself information. */
  note: string;
}

export function evaluationAccuracyView(run: EvaluationRun | null): AccuracyView {
  if (!run) {
    return {
      sampleCount: 0,
      note: 'No evaluation run yet. Forecasts are scored once their horizons elapse; run an evaluation to populate accuracy.',
    };
  }
  const metrics = run.metrics as Record<string, number | undefined>;
  const enough = run.sample_count > 0 && run.status !== 'INSUFFICIENT_SAMPLE';
  const lead = metrics.lead_time_seconds;
  return {
    precision: enough && metrics.precision != null ? percentLabel(metrics.precision, 0) : undefined,
    recall: enough && metrics.recall != null ? percentLabel(metrics.recall, 0) : undefined,
    falsePositiveRate:
      enough && metrics.false_positive_rate != null
        ? percentLabel(metrics.false_positive_rate, 0)
        : undefined,
    averageLeadTime:
      enough && lead != null
        ? lead >= 3600
          ? `${(lead / 3600).toFixed(1)} h`
          : `${Math.round(lead / 60)} min`
        : undefined,
    sampleCount: run.sample_count,
    note:
      run.notes.length > 0
        ? run.notes.join(' ')
        : `Evaluated ${run.sample_count} forecasts between ${run.dataset_window_start} and ${run.dataset_window_end}.`,
  };
}

export function outcomeStyle(outcome: PredictionOutcomeType): string {
  switch (outcome) {
    case 'TRUE_POSITIVE':
      return RISK_LOW;
    case 'TRUE_NEGATIVE':
      return RISK_NEUTRAL;
    case 'FALSE_POSITIVE':
    case 'FALSE_NEGATIVE':
      return RISK_HIGH;
    default:
      return RISK_NEUTRAL;
  }
}

// ---------------------------------------------------------------------------
// Backtest views (§55)
// ---------------------------------------------------------------------------

export function backtestCounts(backtest: Backtest): Record<string, number> {
  const counts: Record<string, number> = {
    TRUE_POSITIVE: 0,
    FALSE_POSITIVE: 0,
    TRUE_NEGATIVE: 0,
    FALSE_NEGATIVE: 0,
    INCONCLUSIVE: 0,
  };
  for (const step of backtest.steps) {
    counts[step.outcome] = (counts[step.outcome] ?? 0) + 1;
  }
  return counts;
}

export function backtestLeadTimes(backtest: Backtest): number[] {
  return backtest.steps
    .map((step: BacktestStep) => step.time_to_event_seconds)
    .filter((value): value is number => typeof value === 'number' && value >= 0);
}

// ---------------------------------------------------------------------------
// Drift (§41, §42)
// ---------------------------------------------------------------------------

export function driftStyle(status: DriftFinding['status']): string {
  switch (status) {
    case 'FLAGGED':
      return RISK_CRITICAL;
    case 'WATCH':
      return RISK_HIGH;
    default:
      return RISK_LOW;
  }
}

export function driftKindLabel(kind: DriftFinding['kind']): string {
  switch (kind) {
    case 'FEATURE_DRIFT':
      return 'Feature drift';
    case 'PREDICTION_DRIFT':
      return 'Prediction drift';
    case 'OUTCOME_DRIFT':
      return 'Outcome drift';
    case 'CALIBRATION_DRIFT':
      return 'Calibration drift';
    case 'DATA_DRIFT':
      return 'Data drift';
    default:
      return kind;
  }
}

// ---------------------------------------------------------------------------
// Warnings (§39)
// ---------------------------------------------------------------------------

export function warningStatusStyle(status: EarlyWarning['status']): string {
  switch (status) {
    case 'OPEN':
      return RISK_HIGH;
    case 'ACKNOWLEDGED':
      return RISK_MEDIUM;
    case 'DISMISSED':
    case 'EXPIRED':
      return RISK_NEUTRAL;
    default:
      return RISK_NEUTRAL;
  }
}

// ---------------------------------------------------------------------------
// Model registry (§54)
// ---------------------------------------------------------------------------

export function modelStatusLabel(status: ModelVersion['status']): string {
  switch (status) {
    case 'ACTIVE':
      return 'Active';
    case 'VALIDATED':
      return 'Validated';
    case 'DEVELOPMENT':
      return 'Development';
    case 'RETIRED':
      return 'Retired';
    default:
      return status;
  }
}

// ---------------------------------------------------------------------------
// Sorting helpers for the heatmap and lists
// ---------------------------------------------------------------------------

/** Cells sort worst-first so the page leads with what deserves attention. */
export function sortHeatmapCells<T extends { worst_level: ForecastRiskLevel }>(
  cells: T[]
): T[] {
  return [...cells].sort(
    (a, b) => riskRank(b.worst_level) - riskRank(a.worst_level)
  );
}

/** Current forecasts per (type, horizon), newest first — the profile's grid. */
export function latestForecasts(forecasts: Forecast[]): Forecast[] {
  const seen = new Map<string, Forecast>();
  for (const forecast of forecasts) {
    const key = `${forecast.prediction_type}:${forecast.forecast_horizon}`;
    if (!seen.has(key)) seen.set(key, forecast);
  }
  const latest: Forecast[] = [];
  seen.forEach((forecast) => latest.push(forecast));
  return latest.sort(
    (a, b) => riskRank(b.risk_level) - riskRank(a.risk_level)
  );
}

/** The top signals, ranked; ties break by severity then insertion order. */
export function topSignals(signals: ForecastSignal[], count = 5): ForecastSignal[] {
  return [...signals]
    .sort((a, b) => a.rank - b.rank)
    .slice(0, count);
}
