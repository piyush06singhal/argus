/**
 * Presentation helpers for Phase 11 — the unified reliability platform.
 *
 * The rules that shape this module are the phase's own guarantees, and each one
 * exists because the alternative is a UI that lies by omission:
 *
 * 1. **A state is derived, and the reason travels with it** (§2–§5). A component
 *    never renders as `HEALTHY` or `DEGRADED` without the evidence that decided
 *    it, because a state is a conclusion and conclusions need provenance.
 * 2. **`UNKNOWN` is not healthy** (§4). Insufficient evidence renders as
 *    `UNKNOWN` with an explicit label, never as a clean bill of health.
 * 3. **A prediction is a band, not a fact** (§36). Risk renders with its
 *    uncertainty and its window.
 * 4. **Correlation is not causation** (§38, §39). A change near an incident is
 *    labelled as correlated, never as the cause.
 * 5. **An error budget is an allowance, not a target** (§34, §35). The budget is
 *    rendered as remaining headroom with its burn state.
 * 6. **Ownership that was never recorded is `UNKNOWN`** (§31). ARGUS never
 *    invents an owner.
 * 7. **This surface commands nothing.** The AI assistant cites evidence and the
 *    platform never executes remediation from here (§28, §129).
 */

// ---------------------------------------------------------------------------
// The boundary, stated once and reused everywhere (§28, §129)
// ---------------------------------------------------------------------------

export const PLATFORM_BOUNDARY =
  'This control plane consolidates what ARGUS observed, concluded and recorded. ' +
  'It never executes remediation, never modifies source, and never bypasses the ' +
  'Phase 9 authorization, blast-radius or verification controls.';

export const CORRELATION_NOT_CAUSATION =
  'A change observed near an incident is correlated with it, not proven to have caused it.';

export const UNKNOWN_OWNERSHIP = 'UNKNOWN';

// ---------------------------------------------------------------------------
// §2–§5 component state
// ---------------------------------------------------------------------------

export type PlatformState =
  | 'INCIDENT'
  | 'RECOVERING'
  | 'DEGRADED'
  | 'AT_RISK'
  | 'HEALTHY'
  | 'UNKNOWN';

/** §4. Worst-first, the exact order the state engine uses to resolve a component. */
export const STATE_PRECEDENCE: readonly PlatformState[] = [
  'INCIDENT',
  'RECOVERING',
  'DEGRADED',
  'AT_RISK',
  'HEALTHY',
  'UNKNOWN',
];

const STATE_LABELS: Record<PlatformState, string> = {
  INCIDENT: 'Incident',
  RECOVERING: 'Recovering',
  DEGRADED: 'Degraded',
  AT_RISK: 'At risk',
  HEALTHY: 'Healthy',
  UNKNOWN: 'Unknown',
};

const STATE_STYLES: Record<PlatformState, string> = {
  INCIDENT: 'bg-argus-error/20 text-argus-error',
  RECOVERING: 'bg-argus-info/20 text-argus-info',
  DEGRADED: 'bg-argus-warning/20 text-argus-warning',
  AT_RISK: 'bg-argus-warning/15 text-argus-warning',
  HEALTHY: 'bg-argus-success/15 text-argus-success',
  UNKNOWN: 'bg-slate-800 text-slate-400',
};

export function componentStateLabel(state?: string | null): string {
  if (!state) {
    return STATE_LABELS.UNKNOWN;
  }
  return STATE_LABELS[state as PlatformState] ?? state;
}

export function componentStateStyle(state?: string | null): string {
  if (!state) {
    return STATE_STYLES.UNKNOWN;
  }
  return STATE_STYLES[state as PlatformState] ?? 'bg-slate-800 text-slate-300';
}

/** §4. `UNKNOWN` means "insufficient evidence", and the UI says so. */
export function componentStateHint(state?: string | null): string {
  switch (state) {
    case 'HEALTHY':
      return 'No open incident, degradation, or elevated risk in the evidence window.';
    case 'AT_RISK':
      return 'A forecast or change signal raises risk without observed degradation yet.';
    case 'DEGRADED':
      return 'Observed degradation is present and no incident has been raised.';
    case 'RECOVERING':
      return 'A condition is recovering after a degradation or remediation.';
    case 'INCIDENT':
      return 'An open incident names this component.';
    default:
      return 'Insufficient evidence to decide a state — not a clean bill of health.';
  }
}

// ---------------------------------------------------------------------------
// §35 error-budget burn
// ---------------------------------------------------------------------------

export type BurnState = 'NORMAL' | 'ELEVATED' | 'FAST_BURN' | 'CRITICAL_BURN' | 'UNKNOWN';

const BURN_LABELS: Record<BurnState, string> = {
  NORMAL: 'Normal',
  ELEVATED: 'Elevated',
  FAST_BURN: 'Fast burn',
  CRITICAL_BURN: 'Critical burn',
  UNKNOWN: 'Unknown',
};

const BURN_STYLES: Record<BurnState, string> = {
  NORMAL: 'bg-argus-success/15 text-argus-success',
  ELEVATED: 'bg-argus-warning/15 text-argus-warning',
  FAST_BURN: 'bg-argus-warning/25 text-argus-warning',
  CRITICAL_BURN: 'bg-argus-error/20 text-argus-error',
  UNKNOWN: 'bg-slate-800 text-slate-400',
};

export function burnStateLabel(state?: string | null): string {
  if (!state) {
    return BURN_LABELS.UNKNOWN;
  }
  return BURN_LABELS[state as BurnState] ?? state;
}

export function burnStateStyle(state?: string | null): string {
  if (!state) {
    return BURN_STYLES.UNKNOWN;
  }
  return BURN_STYLES[state as BurnState] ?? 'bg-slate-800 text-slate-300';
}

// ---------------------------------------------------------------------------
// §32–§34 SLO status
// ---------------------------------------------------------------------------

export type SloStatus = 'MEETING' | 'AT_RISK' | 'BREACHED' | 'UNKNOWN';

const SLO_LABELS: Record<SloStatus, string> = {
  MEETING: 'Meeting',
  AT_RISK: 'At risk',
  BREACHED: 'Breached',
  UNKNOWN: 'Unknown',
};

const SLO_STYLES: Record<SloStatus, string> = {
  MEETING: 'bg-argus-success/15 text-argus-success',
  AT_RISK: 'bg-argus-warning/20 text-argus-warning',
  BREACHED: 'bg-argus-error/20 text-argus-error',
  UNKNOWN: 'bg-slate-800 text-slate-400',
};

export function sloStatusLabel(status?: string | null): string {
  if (!status) {
    return SLO_LABELS.UNKNOWN;
  }
  return SLO_LABELS[status as SloStatus] ?? status;
}

export function sloStatusStyle(status?: string | null): string {
  if (!status) {
    return SLO_STYLES.UNKNOWN;
  }
  return SLO_STYLES[status as SloStatus] ?? 'bg-slate-800 text-slate-300';
}

// ---------------------------------------------------------------------------
// §14–§16 case status
// ---------------------------------------------------------------------------

const CASE_STATUS_LABELS: Record<string, string> = {
  OPEN: 'Open',
  TRIAGED: 'Triaged',
  ANALYZING: 'Analyzing',
  DIAGNOSED: 'Diagnosed',
  REMEDIATION_READY: 'Remediation ready',
  AUTHORIZED: 'Authorized',
  EXECUTING: 'Executing',
  VERIFYING: 'Verifying',
  RESOLVED: 'Resolved',
  LEARNED: 'Learned',
  CLOSED: 'Closed',
  CANCELLED: 'Cancelled',
};

const CASE_STATUS_STYLES: Record<string, string> = {
  OPEN: 'bg-argus-error/20 text-argus-error',
  TRIAGED: 'bg-argus-info/20 text-argus-info',
  ANALYZING: 'bg-argus-info/20 text-argus-info',
  DIAGNOSED: 'bg-argus-info/15 text-argus-info',
  REMEDIATION_READY: 'bg-argus-warning/20 text-argus-warning',
  AUTHORIZED: 'bg-argus-warning/20 text-argus-warning',
  EXECUTING: 'bg-argus-warning/25 text-argus-warning',
  VERIFYING: 'bg-argus-info/20 text-argus-info',
  RESOLVED: 'bg-argus-success/15 text-argus-success',
  LEARNED: 'bg-argus-success/25 text-argus-success',
  CLOSED: 'bg-slate-800 text-slate-400',
  CANCELLED: 'bg-slate-800 text-slate-500',
};

/** §16. The same statuses the backend will accept as transitions. */
export const TERMINAL_CASE_STATUSES: readonly string[] = ['CLOSED', 'CANCELLED'];

export function caseStatusLabel(status?: string | null): string {
  if (!status) {
    return 'Unknown';
  }
  return CASE_STATUS_LABELS[status] ?? status;
}

export function caseStatusStyle(status?: string | null): string {
  if (!status) {
    return 'bg-slate-800 text-slate-300';
  }
  return CASE_STATUS_STYLES[status] ?? 'bg-slate-800 text-slate-300';
}

export function isCaseTerminal(status?: string | null): boolean {
  return status != null && TERMINAL_CASE_STATUSES.includes(status);
}

// ---------------------------------------------------------------------------
// §57–§60 subsystem health
// ---------------------------------------------------------------------------

const HEALTH_LABELS: Record<string, string> = {
  OK: 'OK',
  HEALTHY: 'Healthy',
  DEGRADED: 'Degraded',
  UNAVAILABLE: 'Unavailable',
  UNKNOWN: 'Unknown',
  DISABLED: 'Disabled',
};

const HEALTH_STYLES: Record<string, string> = {
  OK: 'bg-argus-success/15 text-argus-success',
  HEALTHY: 'bg-argus-success/15 text-argus-success',
  DEGRADED: 'bg-argus-warning/20 text-argus-warning',
  UNAVAILABLE: 'bg-argus-error/20 text-argus-error',
  UNKNOWN: 'bg-slate-800 text-slate-400',
  DISABLED: 'bg-slate-800 text-slate-500',
};

export function subsystemStatusLabel(status?: string | null): string {
  if (!status) {
    return HEALTH_LABELS.UNKNOWN;
  }
  return HEALTH_LABELS[status] ?? status;
}

export function subsystemStatusStyle(status?: string | null): string {
  if (!status) {
    return HEALTH_STYLES.UNKNOWN;
  }
  return HEALTH_STYLES[status] ?? 'bg-slate-800 text-slate-300';
}

/**
 * §60 — an optional subsystem being down degrades a capability; it must not be
 * rendered like a required subsystem being down. The badge says which it is.
 */
export function subsystemRequirementLabel(required: boolean): string {
  return required ? 'Required' : 'Optional';
}

// ---------------------------------------------------------------------------
// Severity / data quality
// ---------------------------------------------------------------------------

export function severityStyle(severity?: string | null): string {
  switch (severity) {
    case 'CRITICAL':
    case 'CRITICAL_BURN':
      return 'bg-argus-error/20 text-argus-error';
    case 'HIGH':
    case 'WARNING':
      return 'bg-argus-warning/20 text-argus-warning';
    case 'MEDIUM':
    case 'INFO':
      return 'bg-argus-info/20 text-argus-info';
    case 'LOW':
      return 'bg-slate-800 text-slate-400';
    default:
      return 'bg-slate-800 text-slate-300';
  }
}

/**
 * §89. The data-quality kinds are the §88 orphan catalogue. A UI that renders
 * them as generic "issues" throws away the only actionable part.
 */
const DATA_QUALITY_LABELS: Record<string, string> = {
  ORPHANED_RECORD: 'Orphaned record',
  INCIDENT_WITHOUT_COMPONENT: 'Incident without component',
  PREDICTION_WITHOUT_SNAPSHOT: 'Prediction without feature snapshot',
  REMEDIATION_WITHOUT_AUTHORIZATION: 'Remediation without authorization',
  KNOWLEDGE_WITHOUT_EVIDENCE: 'Knowledge without evidence',
  STALE_COMPONENT: 'Stale component',
  MISSING_TELEMETRY: 'Missing telemetry',
  BROKEN_RELATIONSHIP: 'Broken relationship',
  INVALID_EVIDENCE: 'Invalid evidence',
  LEARNING_RUN_WITHOUT_DATASET: 'Learning run without dataset',
};

export function dataQualityKindLabel(kind?: string | null): string {
  if (!kind) {
    return 'Issue';
  }
  return DATA_QUALITY_LABELS[kind] ?? kind;
}

// ---------------------------------------------------------------------------
// §38, §40 change intelligence
// ---------------------------------------------------------------------------

const CHANGE_RISK_LABELS: Record<string, string> = {
  LOW: 'Low',
  MEDIUM: 'Medium',
  HIGH: 'High',
  CRITICAL: 'Critical',
  UNKNOWN: 'Unknown',
};

export function changeRiskLabel(band?: string | null): string {
  if (!band) {
    return CHANGE_RISK_LABELS.UNKNOWN;
  }
  return CHANGE_RISK_LABELS[band] ?? band;
}

export function changeRiskStyle(band?: string | null): string {
  switch (band) {
    case 'CRITICAL':
      return 'bg-argus-error/20 text-argus-error';
    case 'HIGH':
      return 'bg-argus-warning/25 text-argus-warning';
    case 'MEDIUM':
      return 'bg-argus-warning/15 text-argus-warning';
    case 'LOW':
      return 'bg-argus-success/15 text-argus-success';
    default:
      return 'bg-slate-800 text-slate-400';
  }
}

// ---------------------------------------------------------------------------
// §18 search / §25 activity
// ---------------------------------------------------------------------------

const SEARCH_KIND_LABELS: Record<string, string> = {
  incident: 'Incident',
  case: 'Reliability case',
  anomaly: 'Anomaly',
  component: 'Component',
  service: 'Service',
  deployment: 'Deployment',
  remediation: 'Remediation',
  forecast: 'Forecast',
  knowledge: 'Learned pattern',
  report: 'Report',
  postmortem: 'Postmortem',
  data_quality: 'Data quality issue',
};

export function searchKindLabel(kind?: string | null): string {
  if (!kind) {
    return 'Result';
  }
  return SEARCH_KIND_LABELS[kind] ?? kind;
}

const ACTIVITY_LABELS: Record<string, string> = {
  COMPONENT_STATE_CHANGED: 'Component state changed',
  ANOMALY_DETECTED: 'Detected anomaly',
  INCIDENT_CREATED: 'Created incident',
  INCIDENT_UPDATED: 'Updated incident',
  RCA_COMPLETED: 'Completed RCA',
  REPRODUCTION_COMPLETED: 'Reproduced failure',
  PATCH_VERIFIED: 'Verified fix',
  FORECAST_GENERATED: 'Generated forecast',
  RISK_CHANGED: 'Risk changed',
  REMEDIATION_PROPOSED: 'Proposed remediation',
  REMEDIATION_STARTED: 'Started remediation',
  REMEDIATION_COMPLETED: 'Verified recovery',
  REMEDIATION_ROLLED_BACK: 'Rolled back remediation',
  LEARNING_COMPLETED: 'Learned pattern',
  DEPLOYMENT_RECORDED: 'Recorded deployment',
  SLO_STATUS_CHANGED: 'SLO status changed',
  ERROR_BUDGET_BURN: 'Error-budget burn',
  DATA_QUALITY_ISSUE: 'Data quality issue',
  CASE_OPENED: 'Opened reliability case',
  CASE_CLOSED: 'Closed reliability case',
  CASE_STATUS_CHANGED: 'Case status changed',
  CONFIGURATION_CHANGED: 'Configuration changed',
  NOTIFICATION_RAISED: 'Raised notification',
};

export function activityEventLabel(eventType?: string | null): string {
  if (!eventType) {
    return 'Activity';
  }
  return ACTIVITY_LABELS[eventType] ?? eventType;
}

// ---------------------------------------------------------------------------
// Formatting — no fake precision
// ---------------------------------------------------------------------------

/** §34. Renders a 0–1 ratio as a percentage, and never hides a missing value. */
export function formatRatio(value?: number | null, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return '—';
  }
  return `${(value * 100).toFixed(digits)}%`;
}

/** A percentage that already arrives as 0–100. */
export function formatPercent(value?: number | null, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return '—';
  }
  return `${value.toFixed(digits)}%`;
}

export function formatNumber(value?: number | null, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return '—';
  }
  return value.toFixed(digits);
}

export function formatMilliseconds(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return '—';
  }
  if (value < 1000) {
    return `${Math.round(value)} ms`;
  }
  return `${(value / 1000).toFixed(2)} s`;
}

/** §74. A duration rendered as a breakdown component (seconds). */
export function formatSeconds(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return '—';
  }
  const total = Math.max(0, Math.round(value));
  if (total < 60) {
    return `${total}s`;
  }
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes < 60) {
    return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
  }
  const hours = Math.floor(minutes / 60);
  const remMinutes = minutes % 60;
  return remMinutes ? `${hours}h ${remMinutes}m` : `${hours}h`;
}

/**
 * §74 — MTTR is broken down rather than collapsed. Given a metrics payload
 * shaped like `{ detection, triage, diagnosis, remediation, verification }` in
 * seconds, produces the ordered rows the UI renders.
 */
export const MTTR_DIMENSIONS: ReadonlyArray<{ key: string; label: string }> = [
  { key: 'detection', label: 'Detection' },
  { key: 'triage', label: 'Triage' },
  { key: 'diagnosis', label: 'Diagnosis' },
  { key: 'remediation', label: 'Fix / Remediation' },
  { key: 'verification', label: 'Verification' },
  { key: 'recovery', label: 'Recovery' },
];

export function mttrBreakdown(
  source?: Record<string, unknown> | null
): Array<{ label: string; value: number | null }> {
  if (!source) {
    return MTTR_DIMENSIONS.map((item) => ({ label: item.label, value: null }));
  }
  return MTTR_DIMENSIONS.map((item) => {
    const raw = source[item.key];
    const value = typeof raw === 'number' ? raw : null;
    return { label: item.label, value };
  });
}

// ---------------------------------------------------------------------------
// §42 multi-project
// ---------------------------------------------------------------------------

export function openCasesLabel(count?: number | null): string {
  if (!count) {
    return 'No open cases';
  }
  return count === 1 ? '1 open case' : `${count} open cases`;
}
