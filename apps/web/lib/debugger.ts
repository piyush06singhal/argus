/**
 * Presentation helpers for Phase 6 code intelligence & the AI debugger.
 *
 * The rules that shape this module are the phase's own guarantees:
 *
 * 1. **A claim and its validation are rendered separately.** A location with
 *    `validation !== 'VALID'` is never displayed as a finding — the API sets
 *    `displayable: false`, and this module treats that as final.
 * 2. **No confirmed-bug vocabulary.** The strongest label is
 *    `SUSPICIOUS_CODE_PATH`; "likely fault" is always shown as a claim that
 *    was checked against the snapshot, never as a verdict.
 * 3. **A model's confidence never upgrades a hypothesis.** The validation
 *    status — decided by resolved evidence — is what the UI ranks by.
 * 4. **Every enum mirrors the backend.** An unrecognised value renders as
 *    itself rather than being coerced into a known one.
 */

import type {
  ConfidenceLevel,
  DebuggerMetrics,
  DebugAnalysisStatus,
  DebugEvidence,
  DebugHypothesis,
  DebugSession,
  DebugSessionStatus,
  HypothesisValidationStatus,
  LocationValidation,
  TraceCodeMapping,
} from './api';

// ---------------------------------------------------------------------------
// Location validation (§2, §25) — the core honesty rule
// ---------------------------------------------------------------------------

export const LOCATION_VALIDATION_STYLES: Record<LocationValidation, string> = {
  VALID: 'bg-argus-success/15 text-argus-success',
  NOT_FOUND: 'bg-argus-error/15 text-argus-error',
  OUT_OF_SNAPSHOT: 'bg-argus-warning/15 text-argus-warning',
  LINE_OUT_OF_RANGE: 'bg-argus-warning/15 text-argus-warning',
  AMBIGUOUS: 'bg-argus-info/15 text-argus-info',
  STALE: 'bg-slate-700/40 text-slate-400',
};

export const LOCATION_VALIDATION_NOTES: Record<LocationValidation, string> = {
  VALID: 'exists in the pinned snapshot at these lines',
  NOT_FOUND: 'no such file/symbol in the pinned snapshot',
  OUT_OF_SNAPSHOT: 'file exists but the snapshot cannot serve its content',
  LINE_OUT_OF_RANGE: 'file exists; the claimed lines do not',
  AMBIGUOUS: 'more than one symbol matches this name',
  STALE: 'claim checked against an older snapshot',
};

export function locationValidationStyle(value: LocationValidation): string {
  return LOCATION_VALIDATION_STYLES[value] ?? LOCATION_VALIDATION_STYLES.STALE;
}

/** Split a location list the way §2 requires: findings and rejected claims. */
export function partitionLocations<T extends { displayable: boolean }>(items: T[]): {
  findings: T[];
  rejected: T[];
} {
  return {
    findings: items.filter((item) => item.displayable),
    rejected: items.filter((item) => !item.displayable),
  };
}

/** The strongest thing ARGUS calls a verified location. */
export const LOCATION_LABEL_NOTE =
  'A verified location means the code exists at these lines — not that it is ' +
  'proven to be the fault.';

export function locationLabelNote(label: string): string {
  return label === 'LIKELY_FAULT_LOCATION'
    ? 'claimed likely fault location — verified against the snapshot, still a claim'
    : 'suspicious code path — verified against the snapshot, still a claim';
}

// ---------------------------------------------------------------------------
// Hypotheses (§27, §28)
// ---------------------------------------------------------------------------

export const HYPOTHESIS_VALIDATION_STYLES: Record<
  HypothesisValidationStatus,
  string
> = {
  SUPPORTED: 'bg-argus-success/15 text-argus-success',
  PARTIALLY_SUPPORTED: 'bg-argus-warning/15 text-argus-warning',
  WEAKENED: 'bg-argus-error/15 text-argus-error',
  REFUTED: 'bg-argus-error/15 text-argus-error',
  UNVERIFIED: 'bg-slate-700/40 text-slate-400',
  INVALID_REFERENCE: 'bg-argus-error/15 text-argus-error',
};

export const HYPOTHESIS_VALIDATION_NOTES: Record<
  HypothesisValidationStatus,
  string
> = {
  SUPPORTED: 'stored evidence is consistent and nothing contradicts it',
  PARTIALLY_SUPPORTED: 'some evidence supports it; some is missing or neutral',
  WEAKENED: 'contradicting evidence was resolved against it',
  REFUTED: 'resolved evidence contradicts it',
  UNVERIFIED: 'no evidence has been resolved for or against it yet',
  INVALID_REFERENCE: 'its citations pointed at things that do not exist',
};

export function hypothesisValidationStyle(
  value: HypothesisValidationStatus
): string {
  return HYPOTHESIS_VALIDATION_STYLES[value] ?? HYPOTHESIS_VALIDATION_STYLES.UNVERIFIED;
}

/**
 * Rank hypotheses the way §28 requires: validation status first (SUPPORTED >
 * PARTIALLY_SUPPORTED > UNVERIFIED > WEAKENED > REFUTED/INVALID), then
 * confidence, then recurrence — never the model's confidence alone.
 */
const VALIDATION_RANK: Record<HypothesisValidationStatus, number> = {
  SUPPORTED: 0,
  PARTIALLY_SUPPORTED: 1,
  UNVERIFIED: 2,
  WEAKENED: 3,
  REFUTED: 4,
  INVALID_REFERENCE: 5,
};

const CONFIDENCE_RANK: Record<ConfidenceLevel, number> = {
  HIGH: 0,
  MEDIUM: 1,
  LOW: 2,
  INSUFFICIENT: 3,
};

export function rankHypotheses(items: DebugHypothesis[]): DebugHypothesis[] {
  return [...items].sort(
    (a, b) =>
      (VALIDATION_RANK[a.validation_status] ?? 9) -
        (VALIDATION_RANK[b.validation_status] ?? 9) ||
      (CONFIDENCE_RANK[a.confidence] ?? 9) - (CONFIDENCE_RANK[b.confidence] ?? 9) ||
      b.recurrence_count - a.recurrence_count ||
      a.id.localeCompare(b.id)
  );
}

// ---------------------------------------------------------------------------
// Evidence (§29)
// ---------------------------------------------------------------------------

const EVIDENCE_POLARITY_STYLES: Record<string, string> = {
  SUPPORTING: 'bg-argus-success/15 text-argus-success',
  CONTRADICTING: 'bg-argus-error/15 text-argus-error',
  NEUTRAL: 'bg-slate-700/40 text-slate-400',
};

export function evidencePolarityStyle(polarity: string): string {
  return EVIDENCE_POLARITY_STYLES[polarity] ?? EVIDENCE_POLARITY_STYLES.NEUTRAL;
}

/**
 * A citation is clickable only when it is a canonical reference the UI can
 * resolve — `FILE:path:10-20`, a UUID-backed reference, or an `E<n>` id the
 * analysis defined. Anything else renders as plain text, never as a link.
 */
export function isResolvableReference(reference: string): boolean {
  return (
    /^FILE:.+/i.test(reference) ||
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
      reference
    ) ||
    /^(TRACE|SPAN|LOG|METRIC|DEPLOYMENT|CONFIGURATION_CHANGE|COMMIT|SYMBOL|REPRODUCTION|CAUSAL_ANALYSIS|CAUSAL_CANDIDATE|GRAPH_EDGE|RISK_SIGNAL):/i.test(
      reference
    ) ||
    /^E\d+$/i.test(reference)
  );
}

/** `FILE:services/checkout/service.py:20-28` → the path (for a viewer link). */
export function referenceFilePath(reference: string): string | null {
  const match = /^FILE:(.+?)(?::\d+(?:-\d+)?)?$/i.exec(reference);
  return match ? match[1] : null;
}

export function formatEvidence(evidence: DebugEvidence): string {
  const label = evidence.label ?? evidence.reference;
  return evidence.quote ? `${label} — "${evidence.quote}"` : label;
}

// ---------------------------------------------------------------------------
// Session & analysis status
// ---------------------------------------------------------------------------

export const SESSION_STATUS_STYLES: Record<DebugSessionStatus, string> = {
  CREATED: 'bg-slate-700/40 text-slate-300',
  CONTEXT_BUILDING: 'bg-argus-info/15 text-argus-info',
  ANALYZING: 'bg-argus-info/15 text-argus-info',
  WAITING_FOR_VALIDATION: 'bg-argus-warning/15 text-argus-warning',
  COMPLETED: 'bg-argus-success/15 text-argus-success',
  FAILED: 'bg-argus-error/15 text-argus-error',
  CANCELLED: 'bg-slate-700/40 text-slate-400',
};

export function sessionStatusStyle(value: DebugSessionStatus): string {
  return SESSION_STATUS_STYLES[value] ?? SESSION_STATUS_STYLES.FAILED;
}

export const ANALYSIS_STATUS_STYLES: Record<DebugAnalysisStatus, string> = {
  PENDING: 'bg-slate-700/40 text-slate-300',
  RUNNING: 'bg-argus-info/15 text-argus-info',
  COMPLETED: 'bg-argus-success/15 text-argus-success',
  DEGRADED: 'bg-argus-warning/15 text-argus-warning',
  FAILED: 'bg-argus-error/15 text-argus-error',
  CANCELLED: 'bg-slate-700/40 text-slate-400',
  LIMIT_REACHED: 'bg-argus-warning/15 text-argus-warning',
};

export function analysisStatusStyle(value: DebugAnalysisStatus): string {
  return ANALYSIS_STATUS_STYLES[value] ?? ANALYSIS_STATUS_STYLES.FAILED;
}

export const CONFIDENCE_STYLES: Record<ConfidenceLevel, string> = {
  HIGH: 'bg-argus-success/15 text-argus-success',
  MEDIUM: 'bg-argus-warning/15 text-argus-warning',
  LOW: 'bg-argus-info/15 text-argus-info',
  INSUFFICIENT: 'bg-slate-700/40 text-slate-400',
};

export function confidenceStyle(value: ConfidenceLevel): string {
  return CONFIDENCE_STYLES[value] ?? CONFIDENCE_STYLES.INSUFFICIENT;
}

// ---------------------------------------------------------------------------
// Trace → code mapping (§15–§17)
// ---------------------------------------------------------------------------

export const MAPPING_KIND_LABELS: Record<string, string> = {
  EXACT_SPAN: 'Exact span match',
  ENDPOINT_ROUTE: 'Endpoint route',
  OPERATION_NAME: 'Operation name',
  SERVICE_FILE: 'Service file heuristic',
  FRAME: 'Stack frame',
  UNMAPPED: 'Unmapped',
};

export function mappingKindLabel(kind: string): string {
  return MAPPING_KIND_LABELS[kind] ?? kind;
}

export function splitMappings(items: TraceCodeMapping[]): {
  mapped: TraceCodeMapping[];
  unmapped: TraceCodeMapping[];
} {
  return {
    mapped: items.filter((item) => item.file_path),
    unmapped: items.filter((item) => !item.file_path),
  };
}

// ---------------------------------------------------------------------------
// Metrics (§63–§65)
// ---------------------------------------------------------------------------

/** The honesty ratios the debugger page leads with. */
export function verificationRate(metrics: DebuggerMetrics): number | null {
  if (metrics.locations_claimed <= 0) {
    return null;
  }
  return metrics.locations_valid / metrics.locations_claimed;
}

export function degradationRate(metrics: DebuggerMetrics): number | null {
  if (metrics.analyses <= 0) {
    return null;
  }
  return metrics.analyses_degraded / metrics.analyses;
}

export function formatRate(rate: number | null): string {
  if (rate === null) {
    return '—';
  }
  return `${Math.round(rate * 100)}%`;
}

// ---------------------------------------------------------------------------
// Misc formatting
// ---------------------------------------------------------------------------

export function sessionTitle(session: DebugSession): string {
  return session.title ?? `Session ${session.id.slice(0, 8)}`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) {
    return '—';
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KiB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

/** Line-oriented source rendering: zero-padded, no interpretation. */
export function formatLineNumber(line: number): string {
  return String(line).padStart(4, ' ');
}

export function excerptLines(
  source: string,
  start: number,
  end: number,
  context = 3
): Array<{ line: number; text: string; focused: boolean }> {
  const all = source.split('\n');
  const from = Math.max(1, start - context);
  const to = Math.min(all.length, end + context);
  const rows: Array<{ line: number; text: string; focused: boolean }> = [];
  for (let line = from; line <= to; line += 1) {
    rows.push({ line, text: all[line - 1] ?? '', focused: line >= start && line <= end });
  }
  return rows;
}
