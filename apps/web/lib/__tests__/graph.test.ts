import { describe, expect, it } from 'vitest';

import {
  confidenceBand,
  sourceLabel,
  SOURCE_LABELS,
} from '../graph';

describe('provenance presentation (§52)', () => {
  it('labels configured sources as Configured', () => {
    expect(sourceLabel('CONFIGURATION')).toBe('Configured');
    expect(sourceLabel('MANUAL')).toBe('Configured');
    expect(sourceLabel('IMPORT')).toBe('Configured');
  });

  it('labels evidence sources as Observed', () => {
    expect(sourceLabel('TRACE')).toBe('Observed');
    expect(sourceLabel('LOG')).toBe('Observed');
    expect(sourceLabel('DEPLOYMENT')).toBe('Observed');
  });

  it('labels inference as Inferred — uncertainty stays visible', () => {
    expect(sourceLabel('INFERENCE')).toBe('Inferred');
    expect(sourceLabel('MOCK')).toBe('Inferred');
  });

  it('falls back to the raw value for unknown sources', () => {
    expect(sourceLabel('SOMETHING_NEW')).toBe('SOMETHING_NEW');
  });

  it('bands confidence as evidence strength', () => {
    expect(confidenceBand(1)).toBe('high');
    expect(confidenceBand(0.95)).toBe('high');
    expect(confidenceBand(0.75)).toBe('medium');
    expect(confidenceBand(0.3)).toBe('low');
    expect(confidenceBand(null)).toBe('unknown');
    expect(confidenceBand(undefined)).toBe('unknown');
  });

  it('covers every edge source', () => {
    // 10 provenance kinds from the data model.
    expect(Object.keys(SOURCE_LABELS)).toHaveLength(10);
  });
});
