import { describe, expect, it } from 'vitest';

import type { RemediationAction, RemediationPolicy } from '../api-client';
import {
  actionTypeLabel,
  attentionCounts,
  blastRadiusLabel,
  confidenceLabel,
  effectiveMode,
  failureReasonLabel,
  isInFlight,
  isTerminal,
  outcomeLabel,
  outcomeStyle,
  policyDecisionLabel,
  policyDecisionStyle,
  REMEDIATION_DISCLAIMER,
  requiresHumanApproval,
  reversibilityLabel,
  riskRank,
  riskStyle,
  safetyLabel,
  sortActionsForReview,
  statusLabel,
  statusStyle,
  verdictLabel,
  verdictStyle,
} from '../remediation';

function action(overrides: Partial<RemediationAction>): RemediationAction {
  return {
    id: 'a1',
    project_id: 'p1',
    action_type: 'PAUSE_BACKGROUND_JOB',
    status: 'PROPOSED',
    description: 'pause a sweep',
    headline: 'PAUSE_BACKGROUND_JOB: pause a sweep',
    risk_level: 'LOW',
    blast_radius: 'SINGLE_COMPONENT',
    affected_resource_count: 1,
    source_type: 'INCIDENT',
    execution_mode: 'OBSERVE_ONLY',
    adapter_kind: 'CONTROL_PLANE',
    rollback_strategy: 'INVERSE_ACTION',
    rollback_available: true,
    canary_required: false,
    canary_stage: 'NONE',
    attempt: 1,
    retry_count: 0,
    max_retries: 2,
    post_analysis_status: 'PENDING',
    fingerprint: 'fp',
    created_by: 'planner',
    ...overrides,
  } as RemediationAction;
}

// ---------------------------------------------------------------------------
// A proposal is not an authorization (§5, §8)
// ---------------------------------------------------------------------------

describe('status labelling', () => {
  it('says who acts next rather than only naming the state', () => {
    expect(statusLabel('PROPOSED')).toMatch(/no gate has run yet/i);
    expect(statusLabel('AWAITING_APPROVAL')).toMatch(/human/i);
    expect(statusLabel('AUTHORIZED')).toMatch(/pending/i);
    expect(statusLabel('VERIFYING')).toMatch(/not yet known/i);
  });

  it('never presents a failed verification as a success', () => {
    // VERIFIED and FAILED are the only terminal outcomes, and they read
    // differently — a green tone may not be reused for a failure.
    expect(statusStyle('VERIFIED')).toContain('argus-success');
    expect(statusStyle('FAILED')).toContain('argus-error');
    expect(statusStyle('FAILED')).not.toContain('success');
  });

  it('renders a refusal with the same weight as a success', () => {
    // A platform whose policy works looks mostly like refusals; toning them down
    // as "nothing happened" would hide the policy doing its job.
    expect(statusStyle('BLOCKED')).not.toBe(statusStyle('VERIFIED'));
    expect(statusStyle('BLOCKED').length).toBeGreaterThan(0);
    expect(statusLabel('BLOCKED')).toMatch(/refused it for now/i);
    expect(statusLabel('EXPIRED')).toMatch(/without being executed/i);
  });

  it('does not invent a label for an unknown status', () => {
    expect(statusLabel('SOMETHING_NEW')).toBe('something new');
    expect(statusStyle('SOMETHING_NEW')).toBe('bg-slate-800 text-slate-400');
  });

  it('knows which states are terminal and which are mid-action', () => {
    expect(isTerminal('VERIFIED')).toBe(true);
    expect(isTerminal('AWAITING_APPROVAL')).toBe(false);
    expect(isInFlight('EXECUTING')).toBe(true);
    expect(isInFlight('AUTHORIZED')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Risk, blast radius, confidence (§4, §7)
// ---------------------------------------------------------------------------

describe('risk and scope', () => {
  it('ranks critical above high above medium above low', () => {
    expect(riskRank('CRITICAL')).toBeGreaterThan(riskRank('HIGH'));
    expect(riskRank('HIGH')).toBeGreaterThan(riskRank('MEDIUM'));
    expect(riskRank('MEDIUM')).toBeGreaterThan(riskRank('LOW'));
  });

  it('renders blast radius in words, with the percentage when recorded', () => {
    expect(blastRadiusLabel('SINGLE_COMPONENT')).toBe('one component');
    expect(blastRadiusLabel('LIMITED_PERCENT', 10)).toBe('a limited percentage (10%)');
    expect(blastRadiusLabel('LIMITED_PERCENT', null)).toBe('a limited percentage');
  });

  it('describes confidence as a band, never a percentage', () => {
    expect(confidenceLabel(0.9)).toBe('strong');
    expect(confidenceLabel(0.6)).toBe('moderate');
    expect(confidenceLabel(0.3)).toBe('weak');
    expect(confidenceLabel(0.1)).toBe('speculative');
    expect(confidenceLabel(null)).toBe('no confidence recorded');
    expect(confidenceLabel(0.9)).not.toMatch(/%/);
  });
});

// ---------------------------------------------------------------------------
// Gate verdicts (§4, §21)
// ---------------------------------------------------------------------------

describe('gate verdicts', () => {
  it('states that a human must decide when the regime requires one', () => {
    expect(policyDecisionLabel('REQUIRE_APPROVAL')).toMatch(/human must approve/i);
    expect(policyDecisionStyle('REQUIRE_APPROVAL')).toContain('warning');
  });

  it('distinguishes an allow from an allow-with-canary', () => {
    expect(policyDecisionLabel('ALLOW')).not.toBe(
      policyDecisionLabel('ALLOW_WITH_CANARY')
    );
    expect(policyDecisionLabel('ALLOW_WITH_CANARY')).toMatch(/canary/i);
  });

  it('reports an unevaluated gate as unevaluated, not as denied', () => {
    expect(policyDecisionLabel(null)).toBe('not yet evaluated');
    expect(policyDecisionStyle(null)).toBe('bg-slate-800 text-slate-400');
  });

  it('labels a missing safety assessment honestly', () => {
    expect(safetyLabel(null)).toBe('not yet assessed');
    expect(safetyLabel('FAILED')).toMatch(/failed/i);
    expect(safetyLabel('PASSED_WITH_WARNINGS')).toMatch(/warnings/i);
  });
});

// ---------------------------------------------------------------------------
// Outcomes (§28–§31)
// ---------------------------------------------------------------------------

describe('outcomes and verification', () => {
  it('never rounds an inconclusive verdict up to a pass', () => {
    expect(verdictLabel('INCONCLUSIVE')).toMatch(/not enough observable evidence/i);
    expect(verdictStyle('INCONCLUSIVE')).not.toContain('success');
    expect(verdictStyle('VERIFIED')).toContain('success');
    expect(verdictLabel('NOT_EXECUTED')).toBe('Not executed');
  });

  it('describes a harmful outcome as harmful', () => {
    expect(outcomeLabel('HARMFUL')).toMatch(/worse/i);
    expect(outcomeStyle('HARMFUL')).toContain('error');
    expect(outcomeLabel(null)).toBe('no outcome recorded');
  });

  it('keeps the execution modes distinguishable', () => {
    expect(outcomeStyle('EFFECTIVE')).not.toBe(outcomeStyle('PARTIALLY_EFFECTIVE'));
    expect(outcomeStyle('INEFFECTIVE')).not.toContain('success');
  });
});

// ---------------------------------------------------------------------------
// Reversibility and authority (§4 of the principles)
// ---------------------------------------------------------------------------

describe('reversibility and authority', () => {
  it('does not call a manual undo a platform rollback', () => {
    const manual = reversibilityLabel({
      rollback_available: false,
      rollback_strategy: 'MANUAL',
    });
    expect(manual).toMatch(/only a human/i);
    expect(manual).not.toMatch(/ARGUS can/i);
  });

  it('says plainly when something cannot be reversed', () => {
    expect(
      reversibilityLabel({ rollback_available: false, rollback_strategy: 'NONE' })
    ).toMatch(/cannot be reversed/i);
  });

  it('names the platform rollback paths it actually has', () => {
    expect(
      reversibilityLabel({
        rollback_available: true,
        rollback_strategy: 'INVERSE_ACTION',
      })
    ).toMatch(/inverse action/i);
    expect(
      reversibilityLabel({
        rollback_available: true,
        rollback_strategy: 'RESTORE_PREVIOUS_STATE',
      })
    ).toMatch(/previous state/i);
  });

  it('states the authority requirement for each regime', () => {
    expect(
      requiresHumanApproval({
        rollback_available: true,
        execution_mode: 'HUMAN_APPROVAL',
      })
    ).toMatch(/named human/i);
    expect(
      requiresHumanApproval({
        rollback_available: false,
        execution_mode: 'AUTONOMOUS',
      })
    ).toMatch(/autonomous authorization/i);
    expect(
      requiresHumanApproval({
        rollback_available: false,
        execution_mode: 'AUTONOMOUS',
        authorized_by: 'policy-engine',
      })
    ).toBe('Authorized by policy-engine');
  });

  it('labels every registered action in words', () => {
    expect(actionTypeLabel('ROLLBACK_DEPLOYMENT')).toBe('Roll back deployment');
    expect(actionTypeLabel('SOMETHING_ELSE')).toBe('something else');
  });

  it('explains the real refusals and does not invent reasons', () => {
    expect(failureReasonLabel('STALE_ACTION')).toMatch(/nobody acted/i);
    expect(failureReasonLabel('EMERGENCY_STOP')).toMatch(/emergency stop/i);
    expect(failureReasonLabel('IRREVERSIBLE_RESTRICTED')).toMatch(
      /cannot be reversed/i
    );
    // An invented reason must fall through unchanged rather than be dressed up.
    expect(failureReasonLabel('SOMETHING_INVENTED')).toBe('something invented');
  });
});

// ---------------------------------------------------------------------------
// Console ordering and rollups (§44)
// ---------------------------------------------------------------------------

describe('console ordering', () => {
  it('puts what is waiting on a human first, not the newest action', () => {
    const sorted = sortActionsForReview([
      action({ id: 'expired', status: 'EXPIRED', created_at: '2026-03-01T00:00:00Z' }),
      action({
        id: 'newest',
        status: 'PROPOSED',
        created_at: '2026-03-02T00:00:00Z',
      }),
      action({
        id: 'waiting',
        status: 'AWAITING_APPROVAL',
        created_at: '2026-01-01T00:00:00Z',
      }),
      action({ id: 'running', status: 'EXECUTING', created_at: '2026-02-01T00:00:00Z' }),
    ]);
    expect(sorted[0].id).toBe('waiting');
    expect(sorted[1].id).toBe('running');
    expect(sorted[sorted.length - 1].id).toBe('expired');
  });

  it('breaks a tie on risk, then on recency', () => {
    const sorted = sortActionsForReview([
      action({ id: 'low', status: 'AWAITING_APPROVAL', risk_level: 'LOW' }),
      action({ id: 'high', status: 'AWAITING_APPROVAL', risk_level: 'HIGH' }),
    ]);
    expect(sorted[0].id).toBe('high');
  });

  it('counts what needs attention', () => {
    const counts = attentionCounts([
      action({ id: '1', status: 'AWAITING_APPROVAL' }),
      action({ id: '2', status: 'AWAITING_APPROVAL' }),
      action({ id: '3', status: 'BLOCKED' }),
      action({ id: '4', status: 'VERIFYING' }),
      action({ id: '5', status: 'FAILED' }),
      action({ id: '6', status: 'VERIFIED' }),
    ]);
    expect(counts).toEqual({
      awaitingApproval: 2,
      blocked: 1,
      inFlight: 1,
      failed: 1,
    });
  });
});

// ---------------------------------------------------------------------------
// Regime (§40)
// ---------------------------------------------------------------------------

describe('effective regime', () => {
  function policy(overrides: Partial<RemediationPolicy>): RemediationPolicy {
    return {
      source: 'stored',
      enabled: true,
      execution_mode: 'HUMAN_APPROVAL',
      autonomous_max_risk: 'LOW',
      max_actions_per_window: 5,
      action_window_seconds: 3600,
      cooldown_seconds: 60,
      max_concurrent_actions: 1,
      max_blast_radius_percent: 10,
      max_blast_radius_scope: 'SINGLE_COMPONENT',
      canary_enabled: true,
      canary_percent: 10,
      approval_ttl_seconds: 1800,
      verification_window_seconds: 300,
      execution_timeout_seconds: 120,
      action_expiry_seconds: 86400,
      emergency_stop_active: false,
      clamped: [],
      ...overrides,
    } as RemediationPolicy;
  }

  it('reports a missing policy as OBSERVE_ONLY, because that is what it does', () => {
    const mode = effectiveMode(null);
    expect(mode.mode).toBe('OBSERVE_ONLY');
    expect(mode.configured).toBe(false);
    expect(mode.explanation).toMatch(/authorizes nothing/i);
  });

  it('reports a fallback policy as unconfigured too', () => {
    const mode = effectiveMode(policy({ source: 'fallback' }));
    expect(mode.configured).toBe(false);
  });

  it('lets an emergency stop override the stored regime', () => {
    const mode = effectiveMode(
      policy({
        execution_mode: 'AUTONOMOUS',
        emergency_stop_active: true,
        emergency_stop_reason: 'database failover in progress',
      })
    );
    expect(mode.mode).toBe('EMERGENCY_STOP');
    expect(mode.explanation).toMatch(/database failover/i);
  });

  it('describes each regime in one honest sentence', () => {
    expect(effectiveMode(policy({ execution_mode: 'DRY_RUN' })).explanation).toMatch(
      /no effect/i
    );
    expect(
      effectiveMode(policy({ execution_mode: 'AUTONOMOUS' })).explanation
    ).toMatch(/non-production/i);
  });

  it('states the phase boundary once, unambiguously', () => {
    expect(REMEDIATION_DISCLAIMER).toMatch(/never an\s+authorization/i);
    expect(REMEDIATION_DISCLAIMER).toMatch(/roll back/i);
  });
});
