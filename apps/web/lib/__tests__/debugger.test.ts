import { describe, expect, it } from 'vitest';

import type { DebugHypothesis } from '../api-client';
import {
  excerptLines,
  formatBytes,
  formatRate,
  hypothesisValidationStyle,
  HYPOTHESIS_VALIDATION_NOTES,
  isResolvableReference,
  locationValidationStyle,
  partitionLocations,
  rankHypotheses,
  referenceFilePath,
  splitMappings,
  verificationRate,
  degradationRate,
} from '../debugger';

function hypothesis(
  overrides: Partial<DebugHypothesis> = {}
): DebugHypothesis {
  return {
    id: 'h1',
    description: 'Retry amplification in inventory client',
    category: 'RETRY_LOGIC',
    confidence: 'MEDIUM',
    validation_status: 'UNVERIFIED',
    rationale: null,
    testable: false,
    test_approach: null,
    recurrence_count: 0,
    locations: [],
    supporting_evidence: [],
    contradicting_evidence: [],
    ...overrides,
  };
}

describe('location validation (§2)', () => {
  it('splits claims into findings and rejected', () => {
    const items = [
      { id: 'a', displayable: true },
      { id: 'b', displayable: false },
      { id: 'c', displayable: true },
    ];
    const { findings, rejected } = partitionLocations(items);
    expect(findings.map((i) => i.id)).toEqual(['a', 'c']);
    expect(rejected.map((i) => i.id)).toEqual(['b']);
  });

  it('renders every validation value, known or not', () => {
    expect(locationValidationStyle('VALID')).toContain('argus-success');
    expect(locationValidationStyle('NOT_FOUND')).toContain('argus-error');
    // An unknown value still gets a style (the stale/inert one), not a crash.
    expect(locationValidationStyle('SOMETHING_NEW' as never)).toBeDefined();
  });
});

describe('hypothesis ranking (§28)', () => {
  it('ranks by validation status before model confidence', () => {
    const ranked = rankHypotheses([
      hypothesis({ id: 'high-unverified', confidence: 'HIGH' }),
      hypothesis({
        id: 'low-supported',
        confidence: 'LOW',
        validation_status: 'SUPPORTED',
      }),
    ]);
    expect(ranked[0].id).toBe('low-supported');
  });

  it('breaks ties by recurrence then id for stable rendering', () => {
    const ranked = rankHypotheses([
      hypothesis({ id: 'b', recurrence_count: 2, validation_status: 'SUPPORTED' }),
      hypothesis({ id: 'a', recurrence_count: 3, validation_status: 'SUPPORTED' }),
      hypothesis({ id: 'c', recurrence_count: 3, validation_status: 'SUPPORTED' }),
    ]);
    expect(ranked.map((h) => h.id)).toEqual(['a', 'c', 'b']);
  });

  it('documents every validation state', () => {
    for (const status of [
      'SUPPORTED',
      'PARTIALLY_SUPPORTED',
      'WEAKENED',
      'REFUTED',
      'UNVERIFIED',
      'INVALID_REFERENCE',
    ] as const) {
      expect(HYPOTHESIS_VALIDATION_NOTES[status].length).toBeGreaterThan(0);
      expect(hypothesisValidationStyle(status)).toBeDefined();
    }
  });
});

describe('citations (§29)', () => {
  it('accepts canonical references as resolvable', () => {
    expect(isResolvableReference('FILE:services/checkout/service.py:20-28')).toBe(
      true
    );
    expect(isResolvableReference('TRACE:abc123')).toBe(true);
    expect(isResolvableReference('E3')).toBe(true);
    expect(
      isResolvableReference('3f9d6c2e-1a4b-4c5d-8e9f-0a1b2c3d4e5f')
    ).toBe(true);
  });

  it('treats prose as a plain citation, not a link', () => {
    expect(isResolvableReference('the checkout service timed out')).toBe(false);
    expect(isResolvableReference('')).toBe(false);
  });

  it('extracts the file path from a FILE reference', () => {
    expect(
      referenceFilePath('FILE:services/checkout/service.py:20-28')
    ).toBe('services/checkout/service.py');
    expect(referenceFilePath('FILE:README.md')).toBe('README.md');
    expect(referenceFilePath('TRACE:abc')).toBeNull();
  });
});

describe('trace mappings (§15)', () => {
  it('splits mapped from unmapped with reasons', () => {
    const { mapped, unmapped } = splitMappings([
      {
        id: 'm1',
        mapping_kind: 'EXACT_SPAN',
        confidence: 0.9,
        file_path: 'services/checkout/service.py',
      },
      {
        id: 'm2',
        mapping_kind: 'UNMAPPED',
        confidence: 0,
        file_path: null,
        unmapped_reason: 'no symbol matched the span operation',
      },
    ] as never);
    expect(mapped).toHaveLength(1);
    expect(unmapped).toHaveLength(1);
    expect(unmapped[0].unmapped_reason).toBeTruthy();
  });
});

describe('metrics (§64)', () => {
  it('reports verification and degradation as bounded ratios', () => {
    const metrics = {
      sessions: 10,
      sessions_completed: 8,
      analyses: 12,
      analyses_degraded: 3,
      hypotheses: 9,
      by_validation_status: { SUPPORTED: 4, REFUTED: 1 },
      locations_claimed: 20,
      locations_valid: 15,
      locations_rejected: 5,
      invalid_references: 2,
      tool_calls: 30,
      tool_calls_refused: 1,
      repositories: 2,
      snapshots: 3,
      index_status: {},
      engine_version: 'test',
      limitations: [],
    };
    expect(verificationRate(metrics)).toBe(0.75);
    expect(degradationRate(metrics)).toBeCloseTo(0.25);
    expect(formatRate(0.75)).toBe('75%');
  });

  it('returns null instead of a fake ratio when nothing was claimed', () => {
    expect(
      verificationRate({
        ...({} as Parameters<typeof verificationRate>[0]),
        locations_claimed: 0,
      })
    ).toBeNull();
    expect(formatRate(null)).toBe('—');
  });
});

describe('formatting', () => {
  it('formats byte sizes readably', () => {
    expect(formatBytes(null)).toBe('—');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2.0 KiB');
    expect(formatBytes(3 * 1024 * 1024)).toBe('3.0 MiB');
  });

  it('excerpts source with focused line ranges inside bounds', () => {
    const source = 'l1\nl2\nl3\nl4\nl5';
    const rows = excerptLines(source, 3, 4, 1);
    expect(rows.map((r) => r.line)).toEqual([2, 3, 4, 5]);
    expect(rows.filter((r) => r.focused).map((r) => r.text)).toEqual([
      'l3',
      'l4',
    ]);
  });

  it('clamps the excerpt at the file edges', () => {
    const rows = excerptLines('a\nb', 1, 1, 10);
    expect(rows).toHaveLength(2);
    expect(rows[0].line).toBe(1);
  });
});
