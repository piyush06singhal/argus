/**
 * Presentation helpers for Phase 9 — safe autonomous remediation.
 *
 * The rules that shape this module are the phase's own guarantees, not styling
 * preferences:
 *
 * 1. **A proposal is not an authorization.** Nothing here renders a proposed or
 *    awaiting-approval action as though it had run, and every status label states
 *    who acts next.
 * 2. **A refusal is a result.** `BLOCKED`, `REJECTED` and `EXPIRED` are rendered
 *    with the same weight as `VERIFIED`, because a platform whose policy works
 *    looks mostly like a list of things it declined to do.
 * 3. **"The command finished" is not success.** A status alone never implies the
 *    remediation worked; the verdict, the outcome and the verification counts
 *    travel with it (`VERIFYING` and `NOT_EXECUTED` are separate states).
 * 4. **No irreversible vocabulary is softened.** An action that cannot be undone
 *    says so, and it says a human had to approve it.
 * 5. **An unknown value is never guessed.** An unrecognised enum renders as-is
 *    with a neutral tone rather than being mapped onto the nearest familiar one.
 */

import type {
  BlastRadiusScopeValue,
  RemediationAction,
  RemediationActionTypeValue,
  RemediationExecutionModeValue,
  RemediationOutcomeValue,
  RemediationPolicy,
  RemediationRiskLevelValue,
  RemediationStatusValue,
  PolicyDecisionValue,
  SafetyStatusValue,
} from './api-client';

export type { RemediationStatusValue, RemediationExecutionModeValue };

// ---------------------------------------------------------------------------
// The phase's boundary, stated once and reused everywhere
// ---------------------------------------------------------------------------

/** The line the whole phase exists to hold. Rendered on the console. */
export const REMEDIATION_DISCLAIMER =
  'ARGUS proposes and, where policy permits, executes a bounded registered ' +
  'action — then verifies it from telemetry. An AI recommendation is never an ' +
  'authorization: every action passes validation, safety, policy and authority, ' +
  'and a human can stop, reject or roll back anything.';

export const MODE_EXPLANATIONS: Record<RemediationExecutionModeValue, string> = {
  OBSERVE_ONLY:
    'OBSERVE_ONLY records proposals and authorizes nothing. This is the default ' +
    'when a scope has no policy row.',
  DRY_RUN: 'DRY_RUN validates the full pipeline but applies no effect.',
  SHADOW:
    'SHADOW performs the read-only half of an action and observes, which is how ' +
    'an operator sees the safety assessment of something live.',
  HUMAN_APPROVAL:
    'HUMAN_APPROVAL permits a remediation and waits for a named person before it ' +
    'runs. This is the default regime.',
  AUTONOMOUS:
    'AUTONOMOUS lets policy authorize a low-risk action in a non-production ' +
    'scope, inside every guard rail, if the action type supports it.',
  EMERGENCY_STOP:
    'EMERGENCY_STOP denies every action in this scope before any other rule runs.',
};

// ---------------------------------------------------------------------------
// Statuses (§5)
// ---------------------------------------------------------------------------

const STATUS_TONE: Partial<Record<RemediationStatusValue, string>> = {
  PROPOSED: 'bg-slate-800 text-slate-300',
  VALIDATING: 'bg-argus-info/15 text-argus-info',
  POLICY_REVIEW: 'bg-argus-info/15 text-argus-info',
  AWAITING_APPROVAL: 'bg-argus-warning/20 text-argus-warning',
  AUTHORIZED: 'bg-argus-info/20 text-argus-info',
  SCHEDULED: 'bg-argus-info/20 text-argus-info',
  EXECUTING: 'bg-argus-accent/20 text-argus-accent',
  VERIFYING: 'bg-argus-accent/20 text-argus-accent',
  VERIFIED: 'bg-argus-success/15 text-argus-success',
  FAILED: 'bg-argus-error/15 text-argus-error',
  ROLLING_BACK: 'bg-argus-warning/20 text-argus-warning',
  ROLLED_BACK: 'bg-slate-700 text-slate-200',
  REJECTED: 'bg-argus-error/15 text-argus-error',
  CANCELLED: 'bg-slate-800 text-slate-400',
  EXPIRED: 'bg-slate-800 text-slate-400',
  BLOCKED: 'bg-argus-warning/20 text-argus-warning',
};

/** Statuses that mean "this is over" — no further transition is expected. */
export const TERMINAL_STATUSES: RemediationStatusValue[] = [
  'VERIFIED',
  'REJECTED',
  'CANCELLED',
  'EXPIRED',
  'ROLLED_BACK',
];

/** Statuses where ARGUS is itself mid-action, so a human action is unwise. */
export const IN_FLIGHT_STATUSES: RemediationStatusValue[] = [
  'EXECUTING',
  'VERIFYING',
  'ROLLING_BACK',
];

export function statusStyle(status: RemediationStatusValue | string): string {
  return STATUS_TONE[status as RemediationStatusValue] ?? 'bg-slate-800 text-slate-400';
}

/**
 * A status label that says who acts next — because "PROPOSED" does not tell a
 * reviewer whether anything is running.
 */
export function statusLabel(status: RemediationStatusValue | string): string {
  switch (status) {
    case 'PROPOSED':
      return 'Proposed — no gate has run yet';
    case 'VALIDATING':
      return 'Validating — safety checks running';
    case 'POLICY_REVIEW':
      return 'Policy review — deciding whether it may run';
    case 'AWAITING_APPROVAL':
      return 'Awaiting a human decision';
    case 'AUTHORIZED':
      return 'Authorized — execution pending';
    case 'SCHEDULED':
      return 'Scheduled — a retry is queued';
    case 'EXECUTING':
      return 'Executing';
    case 'VERIFYING':
      return 'Verifying — outcome not yet known';
    case 'VERIFIED':
      return 'Verified against telemetry';
    case 'FAILED':
      return 'Failed';
    case 'ROLLING_BACK':
      return 'Rolling back';
    case 'ROLLED_BACK':
      return 'Rolled back';
    case 'REJECTED':
      return 'Rejected';
    case 'CANCELLED':
      return 'Cancelled';
    case 'EXPIRED':
      return 'Expired without being executed';
    case 'BLOCKED':
      return 'Blocked — a guard rail refused it for now';
    default:
      return String(status).replace(/_/g, ' ').toLowerCase();
  }
}

export function isTerminal(status: RemediationStatusValue | string): boolean {
  return TERMINAL_STATUSES.includes(status as RemediationStatusValue);
}

export function isInFlight(status: RemediationStatusValue | string): boolean {
  return IN_FLIGHT_STATUSES.includes(status as RemediationStatusValue);
}

// ---------------------------------------------------------------------------
// Risk, blast radius, confidence (§4, §7, §23)
// ---------------------------------------------------------------------------

const RISK_TONE: Partial<Record<RemediationRiskLevelValue, string>> = {
  LOW: 'bg-argus-success/15 text-argus-success',
  MEDIUM: 'bg-argus-info/20 text-argus-info',
  HIGH: 'bg-argus-warning/20 text-argus-warning',
  CRITICAL: 'bg-argus-error/20 text-argus-error',
};

export function riskStyle(risk: RemediationRiskLevelValue | string): string {
  return RISK_TONE[risk as RemediationRiskLevelValue] ?? 'bg-slate-800 text-slate-400';
}

export function riskRank(risk: RemediationRiskLevelValue | string): number {
  switch (risk) {
    case 'CRITICAL':
      return 3;
    case 'HIGH':
      return 2;
    case 'MEDIUM':
      return 1;
    default:
      return 0;
  }
}

const BLAST_RADIUS_LABELS: Record<BlastRadiusScopeValue, string> = {
  SINGLE_INSTANCE: 'one instance',
  SINGLE_COMPONENT: 'one component',
  SINGLE_ENVIRONMENT: 'one environment',
  LIMITED_PERCENT: 'a limited percentage',
  PROJECT_WIDE: 'the whole project',
};

export function blastRadiusLabel(
  scope: BlastRadiusScopeValue | string,
  percent?: number | null
): string {
  const base =
    BLAST_RADIUS_LABELS[scope as BlastRadiusScopeValue] ??
    String(scope).replace(/_/g, ' ').toLowerCase();
  if (percent === null || percent === undefined) {
    return base;
  }
  return `${base} (${percent}%)`;
}

/**
 * Confidence, as a phrase. Never a percentage: a remediation plan derived from
 * stored evidence does not have two significant figures of certainty.
 */
export function confidenceLabel(confidence?: number | null): string {
  if (confidence === null || confidence === undefined) {
    return 'no confidence recorded';
  }
  if (confidence >= 0.75) return 'strong';
  if (confidence >= 0.5) return 'moderate';
  if (confidence >= 0.25) return 'weak';
  return 'speculative';
}

export function percentLabel(value?: number | null, digits = 0): string {
  if (value === null || value === undefined) {
    return '—';
  }
  return `${value.toFixed(digits)}%`;
}

// ---------------------------------------------------------------------------
// Gate verdicts (§4, §21)
// ---------------------------------------------------------------------------

const DECISION_LABELS: Record<PolicyDecisionValue, string> = {
  ALLOW: 'Allowed',
  ALLOW_WITH_CANARY: 'Allowed, canary step first',
  REQUIRE_APPROVAL: 'A human must approve',
  DENY: 'Denied',
};

export function policyDecisionLabel(decision?: PolicyDecisionValue | null): string {
  if (!decision) {
    return 'not yet evaluated';
  }
  return DECISION_LABELS[decision] ?? String(decision);
}

export function policyDecisionStyle(decision?: PolicyDecisionValue | null): string {
  switch (decision) {
    case 'ALLOW':
    case 'ALLOW_WITH_CANARY':
      return 'bg-argus-success/15 text-argus-success';
    case 'REQUIRE_APPROVAL':
      return 'bg-argus-warning/20 text-argus-warning';
    case 'DENY':
      return 'bg-argus-error/15 text-argus-error';
    default:
      return 'bg-slate-800 text-slate-400';
  }
}

const SAFETY_LABELS: Record<SafetyStatusValue, string> = {
  PASSED: 'Safety checks passed',
  PASSED_WITH_WARNINGS: 'Safety checks passed with warnings',
  FAILED: 'Safety checks failed',
};

export function safetyLabel(status?: SafetyStatusValue | null): string {
  if (!status) {
    return 'not yet assessed';
  }
  return SAFETY_LABELS[status] ?? String(status);
}

export function safetyStyle(status?: SafetyStatusValue | null): string {
  switch (status) {
    case 'PASSED':
      return 'bg-argus-success/15 text-argus-success';
    case 'PASSED_WITH_WARNINGS':
      return 'bg-argus-warning/20 text-argus-warning';
    case 'FAILED':
      return 'bg-argus-error/15 text-argus-error';
    default:
      return 'bg-slate-800 text-slate-400';
  }
}

// ---------------------------------------------------------------------------
// Outcomes and verdicts (§28–§31)
// ---------------------------------------------------------------------------

const OUTCOME_LABELS: Record<RemediationOutcomeValue, string> = {
  EFFECTIVE: 'The expected improvement was observed',
  PARTIALLY_EFFECTIVE: 'Some checks improved, others did not',
  INEFFECTIVE: 'No improvement was observed',
  HARMFUL: 'The action made things worse',
  UNKNOWN: 'The outcome could not be determined',
};

export function outcomeLabel(
  outcome?: RemediationOutcomeValue | null
): string {
  if (!outcome) {
    return 'no outcome recorded';
  }
  return OUTCOME_LABELS[outcome] ?? String(outcome);
}

export function outcomeStyle(outcome?: RemediationOutcomeValue | null): string {
  switch (outcome) {
    case 'EFFECTIVE':
      return 'bg-argus-success/15 text-argus-success';
    case 'PARTIALLY_EFFECTIVE':
      return 'bg-argus-info/20 text-argus-info';
    case 'INEFFECTIVE':
      return 'bg-argus-warning/20 text-argus-warning';
    case 'HARMFUL':
      return 'bg-argus-error/15 text-argus-error';
    default:
      return 'bg-slate-800 text-slate-400';
  }
}

export function verdictLabel(verdict?: string | null): string {
  switch (verdict) {
    case 'VERIFIED':
      return 'Verified';
    case 'PARTIALLY_VERIFIED':
      return 'Partially verified';
    case 'FAILED':
      return 'Verification failed';
    case 'INCONCLUSIVE':
      return 'Inconclusive — not enough observable evidence';
    case 'NOT_EXECUTED':
      return 'Not executed';
    default:
      return verdict ? String(verdict).replace(/_/g, ' ').toLowerCase() : '—';
  }
}

export function verdictStyle(verdict?: string | null): string {
  switch (verdict) {
    case 'VERIFIED':
      return 'bg-argus-success/15 text-argus-success';
    case 'PARTIALLY_VERIFIED':
      return 'bg-argus-info/20 text-argus-info';
    case 'FAILED':
      return 'bg-argus-error/15 text-argus-error';
    case 'INCONCLUSIVE':
      return 'bg-argus-warning/20 text-argus-warning';
    default:
      return 'bg-slate-800 text-slate-400';
  }
}

// ---------------------------------------------------------------------------
// Action types and reversibility (§3, §4)
// ---------------------------------------------------------------------------

/** Human labels for the registered actions; the enum stays the identity. */
const ACTION_TYPE_LABELS: Partial<Record<RemediationActionTypeValue, string>> = {
  RESTART_SERVICE: 'Restart service',
  RESTART_INSTANCE: 'Restart instance',
  SCALE_SERVICE_WITHIN_LIMIT: 'Scale service (within limit)',
  DISABLE_FEATURE_FLAG: 'Disable feature flag',
  ENABLE_FEATURE_FLAG: 'Enable feature flag',
  PAUSE_BACKGROUND_JOB: 'Pause background job',
  RESUME_BACKGROUND_JOB: 'Resume background job',
  DISABLE_DEGRADED_DEPENDENCY: 'Disable degraded dependency',
  ROUTE_TRAFFIC_TO_HEALTHY_INSTANCE: 'Route traffic to a healthy instance',
  ROLLBACK_DEPLOYMENT: 'Roll back deployment',
  ROLLBACK_CONFIGURATION: 'Roll back configuration',
  APPLY_VERIFIED_PATCH: 'Apply a verified patch',
};

export function actionTypeLabel(actionType: string): string {
  return (
    ACTION_TYPE_LABELS[actionType as RemediationActionTypeValue] ??
    String(actionType).replace(/_/g, ' ').toLowerCase()
  );
}

/**
 * What "reversible" means for this action — stated, never implied.
 *
 * A `MANUAL` rollback is not the platform promising to undo anything: a person
 * does it outside ARGUS. Calling that "reversible" would be the kind of
 * softening this phase must not do.
 */
export function reversibilityLabel(action: {
  rollback_available: boolean;
  rollback_strategy: string;
}): string {
  if (action.rollback_strategy === 'INVERSE_ACTION') {
    return 'ARGUS can reverse this with the inverse action';
  }
  if (action.rollback_strategy === 'RESTORE_PREVIOUS_STATE') {
    return 'ARGUS can restore the previous state';
  }
  if (action.rollback_strategy === 'REVERT_WORKSPACE') {
    return 'ARGUS can revert the workspace';
  }
  if (action.rollback_strategy === 'MANUAL') {
    return 'Only a human can reverse this, outside ARGUS';
  }
  if (action.rollback_strategy === 'NONE') {
    return 'This cannot be reversed';
  }
  return action.rollback_available
    ? 'A rollback path is recorded'
    : 'No rollback path is recorded';
}

export function requiresHumanApproval(action: {
  rollback_available: boolean;
  execution_mode: RemediationExecutionModeValue;
  authorized_by?: string | null;
}): string {
  if (action.authorized_by) {
    return `Authorized by ${action.authorized_by}`;
  }
  if (action.execution_mode === 'AUTONOMOUS') {
    return 'Eligible for autonomous authorization if policy allows it';
  }
  return 'Requires a named human to authorize it';
}

// ---------------------------------------------------------------------------
// Console ordering and rollups (§44)
// ---------------------------------------------------------------------------

const STATUS_ORDER: Record<string, number> = {
  AWAITING_APPROVAL: 0,
  BLOCKED: 1,
  EXECUTING: 2,
  VERIFYING: 3,
  ROLLING_BACK: 4,
  AUTHORIZED: 5,
  SCHEDULED: 6,
  PROPOSED: 7,
  POLICY_REVIEW: 8,
  VALIDATING: 9,
  FAILED: 10,
  VERIFIED: 11,
  ROLLED_BACK: 12,
  REJECTED: 13,
  CANCELLED: 14,
  EXPIRED: 15,
};

/**
 * Order actions by what a human should look at first — the ones *waiting on
 * them*, not the newest. Newest-first is the wrong order for a queue that
 * contains things a person is blocking.
 */
export function sortActionsForReview(
  actions: RemediationAction[]
): RemediationAction[] {
  return [...actions].sort((left, right) => {
    const leftRank = STATUS_ORDER[left.status] ?? 50;
    const rightRank = STATUS_ORDER[right.status] ?? 50;
    if (leftRank !== rightRank) {
      return leftRank - rightRank;
    }
    const leftRisk = riskRank(left.risk_level);
    const rightRisk = riskRank(right.risk_level);
    if (leftRisk !== rightRisk) {
      return rightRisk - leftRisk;
    }
    return (right.created_at ?? '').localeCompare(left.created_at ?? '');
  });
}

/** Counts that belong in a headline, derived from the actions themselves. */
export function attentionCounts(actions: RemediationAction[]): {
  awaitingApproval: number;
  blocked: number;
  inFlight: number;
  failed: number;
} {
  return {
    awaitingApproval: actions.filter((a) => a.status === 'AWAITING_APPROVAL').length,
    blocked: actions.filter((a) => a.status === 'BLOCKED').length,
    inFlight: actions.filter((a) => isInFlight(a.status)).length,
    failed: actions.filter((a) => a.status === 'FAILED').length,
  };
}

/**
 * The mode a console banner should describe: a policy that is not configured is
 * reported as `OBSERVE_ONLY` because that is what it *does*, not as
 * "unconfigured" with no consequence.
 */
export function effectiveMode(policy?: RemediationPolicy | null): {
  mode: RemediationExecutionModeValue;
  configured: boolean;
  explanation: string;
} {
  if (!policy) {
    return {
      mode: 'OBSERVE_ONLY',
      configured: false,
      explanation: MODE_EXPLANATIONS.OBSERVE_ONLY,
    };
  }
  const configured = policy.source !== 'fallback';
  if (policy.emergency_stop_active) {
    return {
      mode: 'EMERGENCY_STOP',
      configured,
      explanation: policy.emergency_stop_reason
        ? `Emergency stop engaged: ${policy.emergency_stop_reason}`
        : MODE_EXPLANATIONS.EMERGENCY_STOP,
    };
  }
  return {
    mode: policy.execution_mode,
    configured,
    explanation:
      MODE_EXPLANATIONS[policy.execution_mode] ?? 'Unknown regime; nothing is assumed.',
  };
}

/**
 * Refusals are grouped so the console can show *why* nothing ran.
 *
 * The cases are exactly the backend's ``RemediationFailureReason`` members — no
 * invented ones. A reason this build does not recognise falls through to its raw
 * value rather than being mapped onto a familiar-sounding neighbour.
 */
export function failureReasonLabel(reason?: string | null): string {
  switch (reason) {
    case 'EXECUTION_DISABLED':
      return 'execution is disabled in this process';
    case 'ADAPTER_UNAVAILABLE':
      return 'this build cannot execute that action';
    case 'ENVIRONMENT_NOT_ALLOWED':
      return 'the environment or action type is not permitted here';
    case 'PRECONDITION_FAILED':
      return 'a precondition no longer holds';
    case 'IRREVERSIBLE_RESTRICTED':
      return 'it cannot be reversed, so it may not run here';
    case 'POLICY_DENIED':
      return 'policy denied it';
    case 'EMERGENCY_STOP':
      return 'an emergency stop is engaged';
    case 'APPROVAL_REQUIRED':
      return 'it needs a human approval';
    case 'APPROVAL_EXPIRED':
      return 'its approval window closed undecided';
    case 'CIRCUIT_OPEN':
      return 'the circuit breaker for this action type is open';
    case 'BUDGET_EXHAUSTED':
      return 'the action budget for this scope is spent';
    case 'CONCURRENCY_LIMIT':
      return 'too many actions are already in flight';
    case 'STALE_ACTION':
      return 'nobody acted before it expired';
    case 'NOT_ACTIONABLE':
      return 'its evidence is too weak to act on';
    case 'HANDLER_ERROR':
      return 'the adapter reported an error';
    case 'TIMEOUT':
      return 'it exceeded its execution timeout';
    case 'PARAMETER_INVALID':
      return 'its parameters are invalid';
    default:
      return reason ? String(reason).replace(/_/g, ' ').toLowerCase() : '';
  }
}
