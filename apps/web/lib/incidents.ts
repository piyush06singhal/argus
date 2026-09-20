/**
 * Presentation helpers for Phase 3 anomaly & incident intelligence.
 *
 * Two rules drive everything here:
 *
 * 1. **No invented enums.** Severity/status value sets mirror the backend
 *    exactly (`app/models/anomaly.py`, `app/models/incident.py`). The previous
 *    frontend used a divergent set (`critical|major|minor|low`) that could
 *    never match a real response.
 * 2. **No causality language.** Helper strings describe relevance and context,
 *    never a root cause; the lifecycle map is the shared source of truth so the
 *    UI cannot offer a transition the API will reject with 409.
 */

import type {
  AnomalySeverity,
  AnomalyStatus,
  IncidentSeverity,
  IncidentStatus,
} from './api';

// ---------------------------------------------------------------------------
// Badge styling
// ---------------------------------------------------------------------------

export const SEVERITY_STYLES: Record<IncidentSeverity, string> = {
  CRITICAL: 'bg-argus-error/15 text-argus-error',
  HIGH: 'bg-argus-warning/15 text-argus-warning',
  MEDIUM: 'bg-argus-info/15 text-argus-info',
  LOW: 'bg-argus-accent/15 text-argus-accent',
};

export const STATUS_STYLES: Record<IncidentStatus, string> = {
  OPEN: 'bg-argus-error/10 text-argus-error',
  ACKNOWLEDGED: 'bg-argus-warning/10 text-argus-warning',
  INVESTIGATING: 'bg-argus-info/10 text-argus-info',
  MITIGATED: 'bg-argus-info/10 text-argus-info',
  RESOLVED: 'bg-argus-success/10 text-argus-success',
  CLOSED: 'bg-slate-700/40 text-slate-400',
};

export const ANOMALY_SEVERITY_STYLES: Record<AnomalySeverity, string> = {
  CRITICAL: 'bg-argus-error/15 text-argus-error',
  HIGH: 'bg-argus-warning/15 text-argus-warning',
  MEDIUM: 'bg-argus-info/15 text-argus-info',
  LOW: 'bg-argus-accent/15 text-argus-accent',
};

export const ANOMALY_STATUS_STYLES: Record<AnomalyStatus, string> = {
  DETECTED: 'bg-argus-error/10 text-argus-error',
  ACKNOWLEDGED: 'bg-argus-warning/10 text-argus-warning',
  INVESTIGATING: 'bg-argus-info/10 text-argus-info',
  RESOLVED: 'bg-argus-success/10 text-argus-success',
  EXPIRED: 'bg-slate-700/40 text-slate-400',
};

/** Ordered severities (highest first) — used for filter dropdowns and charts. */
export const SEVERITY_ORDER: IncidentSeverity[] = [
  'CRITICAL',
  'HIGH',
  'MEDIUM',
  'LOW',
];

export const INCIDENT_STATUS_ORDER: IncidentStatus[] = [
  'OPEN',
  'ACKNOWLEDGED',
  'INVESTIGATING',
  'MITIGATED',
  'RESOLVED',
  'CLOSED',
];

export const ANOMALY_STATUS_ORDER: AnomalyStatus[] = [
  'DETECTED',
  'ACKNOWLEDGED',
  'INVESTIGATING',
  'RESOLVED',
  'EXPIRED',
];

// ---------------------------------------------------------------------------
// Lifecycle — mirrors app/services/incident_state.py
// ---------------------------------------------------------------------------

const INCIDENT_TRANSITIONS: Record<IncidentStatus, IncidentStatus[]> = {
  OPEN: ['ACKNOWLEDGED', 'INVESTIGATING', 'MITIGATED', 'RESOLVED', 'CLOSED'],
  ACKNOWLEDGED: ['INVESTIGATING', 'MITIGATED', 'RESOLVED', 'CLOSED'],
  INVESTIGATING: ['MITIGATED', 'RESOLVED', 'CLOSED'],
  MITIGATED: ['INVESTIGATING', 'RESOLVED', 'CLOSED'],
  RESOLVED: ['OPEN', 'CLOSED'],
  CLOSED: ['OPEN'],
};

/** Legal next statuses for an incident — the UI offers only these. */
export function allowedIncidentTransitions(
  status: IncidentStatus
): IncidentStatus[] {
  return INCIDENT_TRANSITIONS[status] ?? [];
}

/** Human label for a lifecycle action button. */
export function transitionLabel(target: IncidentStatus): string {
  switch (target) {
    case 'ACKNOWLEDGED':
      return 'Acknowledge';
    case 'INVESTIGATING':
      return 'Investigate';
    case 'MITIGATED':
      return 'Mark mitigated';
    case 'RESOLVED':
      return 'Resolve';
    case 'CLOSED':
      return 'Close';
    case 'OPEN':
      return 'Reopen';
    default:
      return target;
  }
}

// ---------------------------------------------------------------------------
// Affected-component classification (§30)
// ---------------------------------------------------------------------------

export const CLASSIFICATION_LABELS: Record<string, string> = {
  DIRECTLY_OBSERVED: 'Directly observed',
  UPSTREAM_CONTEXT: 'Upstream context',
  DOWNSTREAM_CONTEXT: 'Downstream context',
  DEPENDENCY_CONTEXT: 'Structural context',
};

export const CLASSIFICATION_STYLES: Record<string, string> = {
  DIRECTLY_OBSERVED: 'bg-argus-error/15 text-argus-error',
  UPSTREAM_CONTEXT: 'bg-argus-info/15 text-argus-info',
  DOWNSTREAM_CONTEXT: 'bg-argus-warning/15 text-argus-warning',
  DEPENDENCY_CONTEXT: 'bg-slate-700/40 text-slate-300',
};

export const CLASSIFICATION_NOTES: Record<string, string> = {
  DIRECTLY_OBSERVED: 'has a directly observed anomaly in this incident',
  UPSTREAM_CONTEXT: 'depends on an affected component — not observed failing',
  DOWNSTREAM_CONTEXT: 'is a dependency of an affected component',
  DEPENDENCY_CONTEXT: 'is structurally related — context only',
};

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

/** Compact duration from seconds (MTTA/MTTR display). */
export function formatSeconds(value?: number | null): string {
  if (value === null || value === undefined) {
    return '—';
  }
  if (value < 60) {
    return `${value.toFixed(0)}s`;
  }
  if (value < 3600) {
    return `${(value / 60).toFixed(1)}m`;
  }
  return `${(value / 3600).toFixed(1)}h`;
}

/** Signed relative deviation as a percentage (e.g. `+286%`). */
export function formatDeviation(value?: number | null): string {
  if (value === null || value === undefined) {
    return '—';
  }
  const percent = value * 100;
  const sign = percent > 0 ? '+' : '';
  return `${sign}${percent.toFixed(0)}%`;
}

/** Plain numeric formatting that never invents precision. */
export function formatValue(value?: number | null): string {
  if (value === null || value === undefined) {
    return '—';
  }
  if (Number.isInteger(value)) {
    return String(value);
  }
  return value.toFixed(2).replace(/\.?0+$/, '');
}

/**
 * Describe a contextual event relative to the first anomaly.
 *
 * Returns context language only — "occurred 2 minutes before the first
 * observed anomaly" — never a causal statement.
 */
export function formatRelativeToFirstAnomaly(
  seconds?: number | null
): string {
  if (seconds === null || seconds === undefined) {
    return 'near the first observed anomaly';
  }
  const magnitude = Math.abs(seconds);
  let amount: string;
  if (magnitude < 90) {
    amount = `${magnitude.toFixed(0)} seconds`;
  } else if (magnitude < 5400) {
    amount = `${(magnitude / 60).toFixed(0)} minutes`;
  } else {
    amount = `${(magnitude / 3600).toFixed(1)} hours`;
  }
  const direction = seconds >= 0 ? 'before' : 'after';
  return `${amount} ${direction} the first observed anomaly`;
}

/** Timeline/provenance badge tone for a timeline event type. */
export function timelineEventTone(
  eventType: string,
  isContextOnly: boolean
): string {
  if (isContextOnly) {
    return 'bg-slate-700/40 text-slate-300';
  }
  if (eventType.startsWith('INCIDENT_')) {
    return 'bg-argus-accent/15 text-argus-accent';
  }
  if (
    eventType === 'DEPLOYMENT_OCCURRED' ||
    eventType === 'CONFIGURATION_CHANGED'
  ) {
    return 'bg-argus-warning/15 text-argus-warning';
  }
  return 'bg-argus-info/15 text-argus-info';
}

export const NON_CAUSALITY_NOTE =
  'Temporal proximity is context, not causation. Phase 3 detects and ' +
  'correlates; it does not determine root cause.';
