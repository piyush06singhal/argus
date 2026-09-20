/**
 * Presentation helpers for Phase 5 failure reproduction.
 *
 * The rules that shape this module are the phase's own constraints:
 *
 * 1. **A reproduction is an experiment, not a verdict.** `result` (what the
 *    sandbox observed) and `outcome` (what that means for the hypothesis) are
 *    rendered as separate, clearly-labelled facts — never merged into one
 *    "confidence" number.
 * 2. **No fake precision.** Similarity is a bucket plus explainable
 *    dimensions; nothing here turns a score into "97.2381%". A rate is only
 *    ever shown with the counts that produced it.
 * 3. **A null result is not a refutation.** `FAILED` / `NOT_SUPPORTED` render
 *    with the same visual weight as support, and always next to the
 *    failure classification and limitations that explain them.
 * 4. **Every enum mirrors the backend.** An unrecognised value renders as
 *    itself rather than being coerced into a known one.
 */

import type {
  ExperimentStatus,
  FailureClass,
  FaultStatus,
  FaultType,
  ObservationStatus,
  ReproductionComparison,
  ReproductionExperiment,
  ReproductionFault,
  ReproductionResult,
  ReproductionValidation,
  ReplayStatus,
  ValidationOutcome,
} from './api';

// ---------------------------------------------------------------------------
// Disclaimer
// ---------------------------------------------------------------------------

/** Always shown with a reproduction result — the phase's central caveat. */
export const REPRODUCTION_DISCLAIMER =
  'A reproduction is an experiment, not a proof. A failed reproduction does ' +
  'not by itself refute the hypothesis — the sandbox may simply differ from ' +
  'the original environment.';

// ---------------------------------------------------------------------------
// Experiment status (§6, §37, §48)
// ---------------------------------------------------------------------------

/** Happy path order; the trailing entries are terminal exits. */
export const EXPERIMENT_HAPPY_PATH: ExperimentStatus[] = [
  'PLANNED',
  'VALIDATING',
  'PROVISIONING',
  'READY',
  'REPLAYING',
  'RUNNING',
  'COLLECTING',
  'COMPARING',
  'COMPLETED',
];

export const TERMINAL_STATUSES: ExperimentStatus[] = [
  'COMPLETED',
  'FAILED',
  'CANCELLED',
  'TIMED_OUT',
];

export const EXPERIMENT_STATUS_STYLES: Record<ExperimentStatus, string> = {
  PLANNED: 'bg-slate-700/40 text-slate-300',
  VALIDATING: 'bg-argus-info/15 text-argus-info',
  PROVISIONING: 'bg-argus-info/15 text-argus-info',
  READY: 'bg-argus-info/15 text-argus-info',
  REPLAYING: 'bg-argus-accent/15 text-argus-accent',
  RUNNING: 'bg-argus-accent/15 text-argus-accent',
  COLLECTING: 'bg-argus-accent/15 text-argus-accent',
  COMPARING: 'bg-argus-accent/15 text-argus-accent',
  COMPLETED: 'bg-argus-success/15 text-argus-success',
  FAILED: 'bg-argus-error/15 text-argus-error',
  CANCELLED: 'bg-slate-700/40 text-slate-400',
  TIMED_OUT: 'bg-argus-warning/15 text-argus-warning',
};

export const EXPERIMENT_STATUS_LABELS: Record<ExperimentStatus, string> = {
  PLANNED: 'Planned — not executed',
  VALIDATING: 'Validating the plan',
  PROVISIONING: 'Provisioning the sandbox',
  READY: 'Sandbox ready',
  REPLAYING: 'Replaying inputs',
  RUNNING: 'Running',
  COLLECTING: 'Collecting telemetry',
  COMPARING: 'Comparing with the incident',
  COMPLETED: 'Completed',
  FAILED: 'Failed',
  CANCELLED: 'Cancelled',
  TIMED_OUT: 'Timed out',
};

export function experimentStatusStyle(status: ExperimentStatus): string {
  return EXPERIMENT_STATUS_STYLES[status] ?? EXPERIMENT_STATUS_STYLES.PLANNED;
}

export function isTerminalStatus(status: ExperimentStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

export function isRunningStatus(status: ExperimentStatus): boolean {
  return !isTerminalStatus(status) && status !== 'PLANNED';
}

/** 1-based position on the happy path; terminal exits report the last step. */
export function progressStep(status: ExperimentStatus): number {
  const index = EXPERIMENT_HAPPY_PATH.indexOf(status);
  if (index >= 0) {
    return index + 1;
  }
  return isTerminalStatus(status) ? EXPERIMENT_HAPPY_PATH.length : 0;
}

export function progressPercent(status: ExperimentStatus): number {
  return Math.round((progressStep(status) / EXPERIMENT_HAPPY_PATH.length) * 100);
}

// ---------------------------------------------------------------------------
// Result (§30) and outcome (§31)
// ---------------------------------------------------------------------------

export const RESULT_STYLES: Record<ReproductionResult, string> = {
  SUCCESSFUL: 'bg-argus-success/15 text-argus-success',
  PARTIAL: 'bg-argus-warning/15 text-argus-warning',
  FAILED: 'bg-slate-700/40 text-slate-300',
  INCONCLUSIVE: 'bg-argus-info/15 text-argus-info',
  NOT_RUN: 'bg-slate-700/40 text-slate-400',
};

export const RESULT_NOTES: Record<ReproductionResult, string> = {
  SUCCESSFUL:
    'the expected failure behaviour was observed in the sandbox with strong evidence',
  PARTIAL: 'some, but not all, of the expected behaviour was observed',
  FAILED: 'the expected failure did not appear in the sandbox',
  INCONCLUSIVE:
    'the environment or the evidence was insufficient to judge the attempt',
  NOT_RUN: 'this experiment has not produced an observation yet',
};

export const OUTCOME_STYLES: Record<ValidationOutcome, string> = {
  SUPPORTED: 'bg-argus-success/15 text-argus-success',
  PARTIALLY_SUPPORTED: 'bg-argus-warning/15 text-argus-warning',
  NOT_SUPPORTED: 'bg-argus-error/15 text-argus-error',
  INCONCLUSIVE: 'bg-argus-info/15 text-argus-info',
};

export const OUTCOME_NOTES: Record<ValidationOutcome, string> = {
  SUPPORTED:
    'the reproduction is consistent with the hypothesis across the compared dimensions',
  PARTIALLY_SUPPORTED:
    'the reproduction matched part of the expected behaviour only',
  NOT_SUPPORTED:
    'the expected behaviour did not reproduce under these conditions — this ' +
    'weakens, but does not by itself disprove, the hypothesis',
  INCONCLUSIVE:
    'the evidence cannot distinguish support from environment mismatch',
};

export function resultStyle(result: ReproductionResult): string {
  return RESULT_STYLES[result] ?? RESULT_STYLES.FAILED;
}

export function outcomeStyle(outcome: ValidationOutcome): string {
  return OUTCOME_STYLES[outcome] ?? OUTCOME_STYLES.INCONCLUSIVE;
}

// ---------------------------------------------------------------------------
// Failure classification (§36)
// ---------------------------------------------------------------------------

export const FAILURE_CLASS_LABELS: Record<FailureClass, string> = {
  ENVIRONMENT_ERROR: 'Environment error',
  INPUT_ERROR: 'Input error',
  TIMEOUT: 'Timed out',
  RESOURCE_LIMIT: 'Resource limit reached',
  DEPENDENCY_UNAVAILABLE: 'Dependency unavailable',
  SANDBOX_ERROR: 'Sandbox error',
  APPLICATION_FAILURE: 'Application failure in the sandbox',
  NO_FAILURE_OBSERVED: 'No failure observed',
  INSUFFICIENT_TELEMETRY: 'Insufficient telemetry',
  UNKNOWN: 'Unknown',
};

/**
 * Which classifications mean "the sandbox could not answer the question" —
 * a null result these classes explain must not read as a refutation.
 */
export const INCONCLUSIVE_CLASSES: FailureClass[] = [
  'ENVIRONMENT_ERROR',
  'INPUT_ERROR',
  'TIMEOUT',
  'RESOURCE_LIMIT',
  'DEPENDENCY_UNAVAILABLE',
  'SANDBOX_ERROR',
  'INSUFFICIENT_TELEMETRY',
  'UNKNOWN',
];

export function failureClassLabel(value: FailureClass): string {
  return FAILURE_CLASS_LABELS[value] ?? value;
}

export function isInconclusiveClass(value?: FailureClass | null): boolean {
  return value != null && INCONCLUSIVE_CLASSES.includes(value);
}

// ---------------------------------------------------------------------------
// Similarity (§28, §29)
// ---------------------------------------------------------------------------

/** Human labels for the comparator's independent dimensions (lower-cased keys). */
export const SIMILARITY_DIMENSION_LABELS: Record<string, string> = {
  temporal: 'Timing',
  component: 'Components',
  error: 'Errors',
  latency: 'Latency',
  trace_topology: 'Trace topology',
  log_pattern: 'Log patterns',
  failure_sequence: 'Failure sequence',
  recovery: 'Recovery',
};

export interface SimilarityRow {
  key: string;
  label: string;
  /** 0..1, or null when the dimension could not be scored. */
  score: number | null;
  style: string;
}

/** Dimension rows in a stable order, with an explicit "not measured" row. */
export function similarityDimensionRows(
  dimensions?: Record<string, number | null> | null
): SimilarityRow[] {
  if (!dimensions) {
    return [];
  }
  return Object.entries(dimensions).map(([rawKey, raw]) => {
    const score = typeof raw === 'number' && Number.isFinite(raw) ? raw : null;
    // The comparator keys dimensions by its own enum (`ERROR`, `FAILURE_SEQUENCE`),
    // so the lookup is case-insensitive: an exact-match map would silently render
    // raw enum names as labels instead of the human ones.
    const key = rawKey.toLowerCase();
    return {
      key,
      label: SIMILARITY_DIMENSION_LABELS[key] ?? rawKey,
      score,
      style: scoreStyle(score),
    };
  });
}

/**
 * Buckets a 0..1 dimension score. Deliberately coarse: the backend buckets
 * similarity for exactly this reason, and a UI that renders `0.9137` would
 * reintroduce the precision §29 forbids.
 */
export function scoreStyle(score: number | null): string {
  if (score === null) {
    return 'bg-slate-700/40 text-slate-400';
  }
  if (score >= 0.8) {
    return 'bg-argus-success/15 text-argus-success';
  }
  if (score >= 0.5) {
    return 'bg-argus-warning/15 text-argus-warning';
  }
  return 'bg-argus-error/15 text-argus-error';
}

export function scoreLabel(score: number | null): string {
  if (score === null) {
    return 'not measured';
  }
  if (score >= 0.8) {
    return 'high';
  }
  if (score >= 0.5) {
    return 'partial';
  }
  return 'low';
}

/** `4 / 5` for an overlap dict, or `—` when the counts are absent. */
export function formatOverlap(overlap?: Record<string, unknown> | null): string {
  if (!overlap) {
    return '—';
  }
  const matched = overlap.matched;
  const total = overlap.total ?? overlap.expected;
  if (typeof matched !== 'number' || typeof total !== 'number') {
    return '—';
  }
  return `${matched} / ${total}`;
}

/**
 * Compact similarity summary: bucket first, mean score as a supporting detail.
 * Returns `null` when there is nothing to summarise.
 */
export function similarityHeadline(
  comparison?: ReproductionComparison | null
): string | null {
  if (!comparison) {
    return null;
  }
  const parts: string[] = [`Similarity ${comparison.overall_similarity}`];
  if (typeof comparison.similarity_score === 'number') {
    parts.push(`mean ${comparison.similarity_score.toFixed(2)}`);
  }
  parts.push(`result ${comparison.result}`);
  return parts.join(' · ');
}

// ---------------------------------------------------------------------------
// Sequence / components
// ---------------------------------------------------------------------------

/** `db → inventory → checkout`, or `—` when nothing was captured. */
export function formatSequence(sequence?: string[] | null): string {
  if (!sequence || sequence.length === 0) {
    return '—';
  }
  return sequence.join(' → ');
}

export function formatComponents(names?: string[] | null): string {
  if (!names || names.length === 0) {
    return '—';
  }
  return names.join(', ');
}

// ---------------------------------------------------------------------------
// Faults (§21, §22)
// ---------------------------------------------------------------------------

export const FAULT_TYPE_LABELS: Record<FaultType, string> = {
  LATENCY: 'Latency',
  TIMEOUT: 'Timeout',
  HTTP_4XX: 'HTTP 4xx',
  HTTP_5XX: 'HTTP 5xx',
  CONNECTION_FAILURE: 'Connection failure',
  RESPONSE_CORRUPTION: 'Response corruption',
  RESOURCE_PRESSURE: 'Resource pressure',
  DEPENDENCY_UNAVAILABLE: 'Dependency unavailable',
};

export const FAULT_STATUS_STYLES: Record<FaultStatus, string> = {
  PLANNED: 'bg-slate-700/40 text-slate-300',
  ACTIVE: 'bg-argus-warning/15 text-argus-warning',
  COMPLETED: 'bg-argus-success/15 text-argus-success',
  FAILED: 'bg-argus-error/15 text-argus-error',
  SKIPPED: 'bg-slate-700/40 text-slate-400',
};

export function faultStatusStyle(status: FaultStatus): string {
  return FAULT_STATUS_STYLES[status] ?? FAULT_STATUS_STYLES.PLANNED;
}

/**
 * One line describing what a fault does — always including whether it was
 * actually injected, so "it reproduced naturally" and "we forced it" can never
 * be read as the same thing (§22).
 */
export function faultSummary(fault: ReproductionFault): string {
  const label = FAULT_TYPE_LABELS[fault.fault_type] ?? fault.fault_type;
  const bits: string[] = [`${label} → ${fault.target}`];
  if (typeof fault.duration_ms === 'number') {
    bits.push(`${fault.duration_ms} ms`);
  }
  if (typeof fault.intensity === 'number') {
    bits.push(`intensity ${fault.intensity}`);
  }
  if (fault.injected) {
    bits.push(
      fault.requests_affected > 0
        ? `injected · ${fault.requests_affected} request(s) affected`
        : 'injected'
    );
  } else {
    bits.push('not injected');
  }
  return bits.join(' · ');
}

// ---------------------------------------------------------------------------
// Replay inputs & observations
// ---------------------------------------------------------------------------

export const REPLAY_STATUS_STYLES: Record<ReplayStatus, string> = {
  PENDING: 'bg-slate-700/40 text-slate-300',
  SENT: 'bg-argus-accent/15 text-argus-accent',
  SUCCEEDED: 'bg-argus-success/15 text-argus-success',
  FAILED: 'bg-argus-error/15 text-argus-error',
  REJECTED: 'bg-argus-warning/15 text-argus-warning',
  SKIPPED: 'bg-slate-700/40 text-slate-400',
};

export function replayStatusStyle(status: ReplayStatus): string {
  return REPLAY_STATUS_STYLES[status] ?? REPLAY_STATUS_STYLES.PENDING;
}

export const OBSERVATION_STATUS_STYLES: Record<ObservationStatus, string> = {
  EXPECTED: 'bg-argus-success/15 text-argus-success',
  UNEXPECTED: 'bg-argus-warning/15 text-argus-warning',
  NEUTRAL: 'bg-slate-700/40 text-slate-300',
  MISSING: 'bg-argus-error/15 text-argus-error',
};

export function observationStatusStyle(status: ObservationStatus): string {
  return OBSERVATION_STATUS_STYLES[status] ?? OBSERVATION_STATUS_STYLES.NEUTRAL;
}

/** Telemetry capture coverage as an explicit ratio, never a bare percentage. */
export function captureCoverage(
  matched: number,
  expected: number
): { text: string; complete: boolean } {
  if (expected <= 0) {
    return { text: 'no expected signals were declared', complete: false };
  }
  return {
    text: `${matched} / ${expected} expected signal(s) captured`,
    complete: matched === expected,
  };
}

// ---------------------------------------------------------------------------
// Determinism (§34)
// ---------------------------------------------------------------------------

/**
 * Repeatability, framed as an experiment observation over the runs that
 * actually happened. Deliberately never a "probability that the hypothesis is
 * true" — the backend's own note says so, and this surfaces it verbatim.
 */
export function determinismNote(
  validation?: ReproductionValidation | null
): string | null {
  const determinism = validation?.determinism;
  if (!determinism || typeof determinism.runs !== 'number') {
    return null;
  }
  const classification = determinism.classification ?? 'UNKNOWN';
  if (determinism.runs === 0) {
    return `Behaviour ${classification}: no comparable run, so no rate exists.`;
  }
  const reproduced =
    (determinism.successful_runs ?? 0) + (determinism.partial_runs ?? 0);
  const rate =
    typeof determinism.reproduction_rate === 'number'
      ? `${Math.round(determinism.reproduction_rate * 100)}%`
      : 'unmeasured';
  return (
    `Behaviour ${classification}: ${reproduced} of ${determinism.runs} ` +
    `repetition(s) reproduced the failure (rate ${rate}). ` +
    (determinism.note ??
      'This is an observation about the sandbox, not a probability that the ' +
        'hypothesis is true.')
  );
}

// ---------------------------------------------------------------------------
// Durations
// ---------------------------------------------------------------------------

export function formatSeconds(value?: number | null): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return '—';
  }
  if (value < 60) {
    return `${value.toFixed(1)} s`;
  }
  const minutes = Math.floor(value / 60);
  const seconds = Math.round(value - minutes * 60);
  return `${minutes}m ${seconds}s`;
}

export function formatBytes(value?: number | null): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return '—';
  }
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / (1024 * 1024)).toFixed(2)} MB`;
}

// ---------------------------------------------------------------------------
// Experiment-level helpers
// ---------------------------------------------------------------------------

/** History rows are newest-version-first; this is the canonical ordering. */
export function sortHistory<T extends { experiment_version: number }>(
  items: T[]
): T[] {
  return [...items].sort((a, b) => b.experiment_version - a.experiment_version);
}

/**
 * What the experiment is *waiting for*, in plain language. Used by the live
 * view so a stalled experiment is legible without reading the raw status.
 */
export function statusNarrative(
  experiment: Pick<
    ReproductionExperiment,
    'status' | 'result' | 'failure_classification'
  >
): string {
  const base =
    EXPERIMENT_STATUS_LABELS[experiment.status] ?? experiment.status;
  if (experiment.failure_classification) {
    return `${base} — ${failureClassLabel(experiment.failure_classification)}`;
  }
  if (experiment.status === 'COMPLETED') {
    return `${base} — ${experiment.result}`;
  }
  return base;
}
