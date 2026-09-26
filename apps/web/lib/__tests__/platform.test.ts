/**
 * Tests for the Phase 11 presentation helpers.
 *
 * These enforce the phase's honesty rules, not formatting:
 *
 * * `UNKNOWN` is a first-class state that never renders as healthy;
 * * a state carries its reason, and a state's precedence is worst-first;
 * * an error budget renders as a burn state, never as a bare number;
 * * a change near an incident is labelled correlated, not causal;
 * * ownership that was never recorded renders as UNKNOWN;
 * * no formatter invents precision for a missing value.
 */

import { describe, expect, it } from 'vitest';

import {
  activityEventLabel,
  burnStateLabel,
  burnStateStyle,
  caseStatusLabel,
  caseStatusStyle,
  changeRiskLabel,
  componentStateHint,
  componentStateLabel,
  componentStateStyle,
  dataQualityKindLabel,
  formatMilliseconds,
  formatNumber,
  formatPercent,
  formatRatio,
  formatSeconds,
  isCaseTerminal,
  mttrBreakdown,
  openCasesLabel,
  PLATFORM_BOUNDARY,
  searchKindLabel,
  severityStyle,
  sloStatusLabel,
  sloStatusStyle,
  STATE_PRECEDENCE,
  subsystemRequirementLabel,
  subsystemStatusLabel,
  UNKNOWN_OWNERSHIP,
} from '../platform';

describe('platform boundary', () => {
  it('states that the surface commands nothing', () => {
    expect(PLATFORM_BOUNDARY).toMatch(/never executes remediation/);
    expect(PLATFORM_BOUNDARY).toMatch(/never bypasses/);
  });
});

describe('component state (§2–§5)', () => {
  it('does not render an unknown state as healthy', () => {
    expect(componentStateLabel(undefined)).toBe('Unknown');
    expect(componentStateLabel(null)).toBe('Unknown');
    expect(componentStateHint(undefined)).toMatch(/insufficient evidence/i);
    expect(componentStateHint(undefined)).not.toMatch(/healthy/i);
  });

  it('labels each state in plain language', () => {
    expect(componentStateLabel('INCIDENT')).toBe('Incident');
    expect(componentStateLabel('RECOVERING')).toBe('Recovering');
    expect(componentStateLabel('AT_RISK')).toBe('At risk');
  });

  it('orders precedence worst-first', () => {
    expect(STATE_PRECEDENCE[0]).toBe('INCIDENT');
    expect(STATE_PRECEDENCE[STATE_PRECEDENCE.length - 1]).toBe('UNKNOWN');
    expect(STATE_PRECEDENCE.indexOf('HEALTHY')).toBeLessThan(
      STATE_PRECEDENCE.indexOf('UNKNOWN')
    );
  });

  it('never invents a style for an unknown state', () => {
    expect(componentStateStyle(undefined)).toBe('bg-slate-800 text-slate-400');
    expect(componentStateStyle('NOT_A_STATE')).toContain('bg-slate-800');
  });
});

describe('error budget burn (§34, §35)', () => {
  it('renders a missing burn state as unknown, not normal', () => {
    expect(burnStateLabel(undefined)).toBe('Unknown');
    expect(burnStateStyle(undefined)).toContain('bg-slate-800');
    expect(burnStateStyle(undefined)).not.toContain('success');
  });

  it('distinguishes fast burn from critical burn', () => {
    expect(burnStateLabel('FAST_BURN')).toBe('Fast burn');
    expect(burnStateLabel('CRITICAL_BURN')).toBe('Critical burn');
    expect(burnStateStyle('CRITICAL_BURN')).toContain('argus-error');
  });
});

describe('SLO status (§32–§34)', () => {
  it('treats an uncomputed objective as unknown, not breached', () => {
    expect(sloStatusLabel(null)).toBe('Unknown');
    expect(sloStatusStyle(null)).toContain('bg-slate-800');
  });

  it('labels the three real states', () => {
    expect(sloStatusLabel('MEETING')).toBe('Meeting');
    expect(sloStatusLabel('AT_RISK')).toBe('At risk');
    expect(sloStatusLabel('BREACHED')).toBe('Breached');
  });
});

describe('case status (§14–§16)', () => {
  it('labels the lifecycle stages', () => {
    expect(caseStatusLabel('OPEN')).toBe('Open');
    expect(caseStatusLabel('VERIFYING')).toBe('Verifying');
    expect(caseStatusLabel('LEARNED')).toBe('Learned');
  });

  it('treats only closed and cancelled as terminal', () => {
    expect(isCaseTerminal('CLOSED')).toBe(true);
    expect(isCaseTerminal('CANCELLED')).toBe(true);
    expect(isCaseTerminal('RESOLVED')).toBe(false);
    expect(isCaseTerminal(undefined)).toBe(false);
  });

  it('never renders an unknown status as healthy', () => {
    expect(caseStatusStyle(undefined)).toContain('bg-slate-800');
  });
});

describe('subsystem health (§57–§60)', () => {
  it('labels an absent status as unknown', () => {
    expect(subsystemStatusLabel(undefined)).toBe('Unknown');
  });

  it('separates required from optional subsystems', () => {
    expect(subsystemRequirementLabel(true)).toBe('Required');
    expect(subsystemRequirementLabel(false)).toBe('Optional');
  });
});

describe('data quality (§88, §89)', () => {
  it('renders the orphan catalogue by name, not as a generic issue', () => {
    expect(dataQualityKindLabel('PREDICTION_WITHOUT_SNAPSHOT')).toBe(
      'Prediction without feature snapshot'
    );
    expect(dataQualityKindLabel('INCIDENT_WITHOUT_COMPONENT')).toBe(
      'Incident without component'
    );
  });

  it('labels every kind the backend can raise (including the hardening checks)', () => {
    // A kind with no label renders as a SCREAMING_SNAKE token in an operator's
    // queue, which is the same as showing them nothing.
    for (const kind of [
      'MISSING_TIMESTAMP',
      'MISSING_PROVENANCE',
      'IMPOSSIBLE_TRANSITION',
      'MISSING_AUDIT_EVENT',
      'CORRUPTED_ARTIFACT',
      'INCONSISTENT_STATE',
    ]) {
      expect(dataQualityKindLabel(kind)).not.toBe(kind);
    }
  });

  it('falls back to the raw kind rather than inventing one', () => {
    expect(dataQualityKindLabel('SOMETHING_NEW')).toBe('SOMETHING_NEW');
    expect(dataQualityKindLabel(undefined)).toBe('Issue');
  });
});

describe('change intelligence (§38–§41)', () => {
  it('treats an unscored change as unknown, not low risk', () => {
    expect(changeRiskLabel(undefined)).toBe('Unknown');
  });

  it('labels the bands', () => {
    expect(changeRiskLabel('HIGH')).toBe('High');
    expect(changeRiskLabel('CRITICAL')).toBe('Critical');
  });
});

describe('search and activity vocabulary (§18, §25)', () => {
  it('names search kinds the way an operator reads them', () => {
    expect(searchKindLabel('forecast')).toBe('Forecast');
    expect(searchKindLabel('knowledge')).toBe('Learned pattern');
  });

  it('names activity events as the phase describes them', () => {
    expect(activityEventLabel('ANOMALY_DETECTED')).toBe('Detected anomaly');
    expect(activityEventLabel('INCIDENT_CREATED')).toBe('Created incident');
    expect(activityEventLabel('REMEDIATION_COMPLETED')).toBe('Verified recovery');
    expect(activityEventLabel('LEARNING_COMPLETED')).toBe('Learned pattern');
  });
});

describe('ownership (§31)', () => {
  it('uses UNKNOWN rather than inventing an owner', () => {
    expect(UNKNOWN_OWNERSHIP).toBe('UNKNOWN');
  });
});

describe('formatting invents no precision', () => {
  it('renders a missing value as an em dash, never zero', () => {
    expect(formatRatio(undefined)).toBe('—');
    expect(formatRatio(null)).toBe('—');
    expect(formatPercent(undefined)).toBe('—');
    expect(formatNumber(undefined)).toBe('—');
    expect(formatMilliseconds(null)).toBe('—');
    expect(formatSeconds(undefined)).toBe('—');
  });

  it('renders a ratio as a percentage', () => {
    expect(formatRatio(0.995)).toBe('99.5%');
    expect(formatPercent(99.5)).toBe('99.5%');
  });

  it('scales milliseconds past a second', () => {
    expect(formatMilliseconds(250)).toBe('250 ms');
    expect(formatMilliseconds(2500)).toBe('2.50 s');
  });

  it('renders durations by magnitude', () => {
    expect(formatSeconds(45)).toBe('45s');
    expect(formatSeconds(185)).toBe('3m 5s');
    expect(formatSeconds(3700)).toBe('1h 1m');
  });

  it('breaks MTTR into its dimensions rather than a single number', () => {
    const rows = mttrBreakdown({ detection: 180, diagnosis: 300 });
    expect(rows.find((row) => row.label === 'Detection')?.value).toBe(180);
    expect(rows.find((row) => row.label === 'Fix / Remediation')?.value).toBeNull();
    expect(mttrBreakdown(null)).toHaveLength(6);
  });
});

describe('multi-project (§42)', () => {
  it('renders an empty open-case count as none, not zero', () => {
    expect(openCasesLabel(0)).toBe('No open cases');
    expect(openCasesLabel(1)).toBe('1 open case');
    expect(openCasesLabel(3)).toBe('3 open cases');
  });
});

describe('severity', () => {
  it('maps severities to distinct styles and defaults conservatively', () => {
    expect(severityStyle('CRITICAL')).toContain('argus-error');
    expect(severityStyle('HIGH')).toContain('argus-warning');
    expect(severityStyle(undefined)).toContain('bg-slate-800');
  });
});
