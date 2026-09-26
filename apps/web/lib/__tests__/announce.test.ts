import { describe, expect, it, vi } from 'vitest';

import { createAnnouncementStore } from '../announce';

/**
 * The property under test is not "a message is stored" but **"the message
 * outlives the component that set it"** — the defect this store exists for. The
 * tests drive the store directly, with no React in the way, because that is
 * exactly the path a component takes after a `router.refresh()` remount:
 * `announce()` is called from the old instance and `read()` is called by the new
 * one, both against the same store.
 */

describe('announcement store', () => {
  it('remembers a message for the scope that set it', () => {
    const store = createAnnouncementStore();

    store.announce('policy', 'Policy saved.');

    // Read as a *fresh* consumer would after the original tree was replaced.
    expect(store.read('policy')).toBe('Policy saved.');
  });

  it('scopes messages so two panels cannot overwrite each other', () => {
    const store = createAnnouncementStore();

    store.announce('policy', 'Policy saved.');
    store.announce('tokens', 'Token revoked.');

    expect(store.read('policy')).toBe('Policy saved.');
    expect(store.read('tokens')).toBe('Token revoked.');
  });

  it('says nothing for a scope that has never announced', () => {
    expect(createAnnouncementStore().read('absent')).toBeNull();
  });

  it('retires a message when it is cleared', () => {
    const store = createAnnouncementStore();
    store.announce('policy', 'Policy saved.');

    store.clear('policy');

    expect(store.read('policy')).toBeNull();
  });

  it('replaces a message rather than queueing it', () => {
    const store = createAnnouncementStore();
    store.announce('policy', 'First.');
    store.announce('policy', 'Second.');

    expect(store.read('policy')).toBe('Second.');
  });

  it('notifies subscribers of a change', () => {
    const store = createAnnouncementStore();
    const listener = vi.fn();
    store.subscribe('policy', listener);

    store.announce('policy', 'Policy saved.');

    expect(listener).toHaveBeenCalledTimes(1);
  });

  it('stops notifying after unsubscribe', () => {
    const store = createAnnouncementStore();
    const listener = vi.fn();
    const unsubscribe = store.subscribe('policy', listener);

    unsubscribe();
    store.announce('policy', 'Policy saved.');

    expect(listener).not.toHaveBeenCalled();
  });

  it('does not notify when clearing a scope with nothing to clear', () => {
    const store = createAnnouncementStore();
    const listener = vi.fn();
    store.subscribe('policy', listener);

    store.clear('policy');

    // A no-op must not cause a re-render loop, which is what an unconditional
    // emit inside `clear` would produce on every mount.
    expect(listener).not.toHaveBeenCalled();
  });

  it('keeps stores isolated from one another', () => {
    const first = createAnnouncementStore();
    const second = createAnnouncementStore();

    first.announce('policy', 'Only in the first store.');

    expect(second.read('policy')).toBeNull();
  });
});
