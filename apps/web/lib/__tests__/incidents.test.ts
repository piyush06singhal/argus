import { describe, expect, it } from 'vitest';

import {
  allowedIncidentTransitions,
  ANOMALY_SEVERITY_STYLES,
  ANOMALY_STATUS_STYLES,
  CLASSIFICATION_LABELS,
  formatDeviation,
  formatRelativeToFirstAnomaly,
  formatSeconds,
  formatValue,
  INCIDENT_STATUS_ORDER,
  SEVERITY_ORDER,
  SEVERITY_STYLES,
  STATUS_STYLES,
  timelineEventTone,
  transitionLabel,
} from '../incidents';

describe('severity & status presentation', () => {
  it('covers every backend incident severity and status', () => {
    // Every value the API can return must have a style, or the UI would fall
    // back to an undefined class (the bug the divergent enum caused).
    for (const severity of ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'] as const) {
      expect(SEVERITY_STYLES[severity]).toBeTruthy();
    }
    for (const status of INCIDENT_STATUS_ORDER) {
      expect(STATUS_STYLES[status]).toBeTruthy();
    }
  });

  it('covers every anomaly severity and status', () => {
    for (const severity of SEVERITY_ORDER) {
      expect(ANOMALY_SEVERITY_STYLES[severity]).toBeTruthy();
    }
    for (const status of [
      'DETECTED',
      'ACKNOWLEDGED',
      'INVESTIGATING',
      'RESOLVED',
      'EXPIRED',
    ] as const) {
      expect(ANOMALY_STATUS_STYLES[status]).toBeTruthy();
    }
  });

  it('orders severities from highest to lowest', () => {
    expect(SEVERITY_ORDER[0]).toBe('CRITICAL');
    expect(SEVERITY_ORDER[SEVERITY_ORDER.length - 1]).toBe('LOW');
  });
});

describe('incident lifecycle map', () => {
  it('mirrors the backend transitions', () => {
    expect(allowedIncidentTransitions('OPEN')).toContain('RESOLVED');
    expect(allowedIncidentTransitions('ACKNOWLEDGED')).not.toContain('OPEN');
    expect(allowedIncidentTransitions('RESOLVED')).toEqual(['OPEN', 'CLOSED']);
    expect(allowedIncidentTransitions('CLOSED')).toEqual(['OPEN']);
  });

  it('never offers a status change to itself', () => {
    for (const status of INCIDENT_STATUS_ORDER) {
      expect(allowedIncidentTransitions(status)).not.toContain(status);
    }
  });

  it('labels lifecycle actions readably', () => {
    expect(transitionLabel('RESOLVED')).toBe('Resolve');
    expect(transitionLabel('OPEN')).toBe('Reopen');
  });
});

describe('formatting', () => {
  it('formats durations compactly', () => {
    expect(formatSeconds(null)).toBe('—');
    expect(formatSeconds(30)).toBe('30s');
    expect(formatSeconds(600)).toBe('10.0m');
    expect(formatSeconds(7200)).toBe('2.0h');
  });

  it('formats deviations with an explicit sign', () => {
    expect(formatDeviation(null)).toBe('—');
    expect(formatDeviation(2.86)).toBe('+286%');
    expect(formatDeviation(-0.5)).toBe('-50%');
  });

  it('formats values without inventing precision', () => {
    expect(formatValue(890)).toBe('890');
    expect(formatValue(2.5)).toBe('2.5');
    expect(formatValue(null)).toBe('—');
  });

  it('describes context, never causation', () => {
    const before = formatRelativeToFirstAnomaly(120);
    expect(before).toBe('2 minutes before the first observed anomaly');
    expect(before).not.toMatch(/caused|because/i);

    const after = formatRelativeToFirstAnomaly(-45);
    expect(after).toBe('45 seconds after the first observed anomaly');
  });
});

describe('classification labels', () => {
  it('explains every classification without claiming failure', () => {
    for (const key of Object.keys(CLASSIFICATION_LABELS)) {
      expect(CLASSIFICATION_LABELS[key]).toBeTruthy();
    }
    expect(CLASSIFICATION_LABELS.DIRECTLY_OBSERVED).toBe('Directly observed');
  });
});

describe('timeline tones', () => {
  it('marks context-only entries distinctly', () => {
    expect(timelineEventTone('DEPLOYMENT_OCCURRED', true)).toContain('slate');
    expect(timelineEventTone('DEPLOYMENT_OCCURRED', false)).toContain(
      'argus-warning'
    );
    expect(timelineEventTone('INCIDENT_CREATED', false)).toContain('argus-accent');
  });
});
