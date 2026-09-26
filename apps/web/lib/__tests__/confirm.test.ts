import { describe, expect, it } from 'vitest';

import {
  DESTRUCTIVE_ACTIONS,
  confirmationFor,
  requiresConfirmation,
} from '../confirm';

describe('destructive-action confirmation (W7)', () => {
  it('covers every destructive action the UI exposes', () => {
    // The catalogue is the contract: a new destructive control that is not in
    // here has no confirmation copy, and would therefore ship without one.
    expect(new Set(DESTRUCTIVE_ACTIONS)).toEqual(
      new Set([
        'revoke_token',
        'delete_project',
        'engage_emergency_stop',
        'release_emergency_stop',
        'cancel_experiment',
      ])
    );
    for (const kind of DESTRUCTIVE_ACTIONS) {
      expect(requiresConfirmation(kind)).toBe(true);
    }
  });

  it('states the consequence and the reversibility for every action', () => {
    // "Are you sure?" on its own is not a confirmation — it is a speed bump.
    for (const kind of DESTRUCTIVE_ACTIONS) {
      const copy = confirmationFor(kind);
      expect(copy.question.length).toBeGreaterThan(10);
      expect(copy.consequence.length).toBeGreaterThan(20);
      expect(copy.reversibility.length).toBeGreaterThan(20);
      expect(copy.confirmLabel.length).toBeGreaterThan(2);
      expect(copy.question.endsWith('?')).toBe(true);
    }
  });

  it('names the object when one is given', () => {
    const copy = confirmationFor('revoke_token', 'ci-runner');
    expect(copy.question).toContain('ci-runner');
    expect(copy.question.endsWith('?')).toBe(true);
  });

  it('never interpolates an empty subject', () => {
    // A prompt reading "Revoke this token — ?" is worse than the generic one.
    expect(confirmationFor('revoke_token', null).question).not.toContain('—');
    expect(confirmationFor('revoke_token', '').question).not.toContain('—');
  });

  it('does not claim a revoked token can be restored', () => {
    const copy = confirmationFor('revoke_token');
    expect(copy.reversibility).toMatch(/cannot|not be un-revoked/i);
    expect(copy.reversibility).not.toMatch(/can be undone/i);
  });

  it('says the emergency stop is reversible, because it is', () => {
    expect(confirmationFor('engage_emergency_stop').reversibility).toMatch(
      /reversible/i
    );
  });

  it('is a fresh object per call, so callers cannot mutate the catalogue', () => {
    const first = confirmationFor('revoke_token', 'a');
    first.question = 'changed';
    expect(confirmationFor('revoke_token', 'a').question).not.toBe('changed');
  });
});
