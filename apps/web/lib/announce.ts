'use client';

import { useCallback, useSyncExternalStore } from 'react';

/**
 * Durable action announcements (hardening W7).
 *
 * **The defect this exists for.** Every mutating control in ARGUS re-renders the
 * page from the server after it succeeds (`router.refresh()`), and the obvious
 * implementation holds the confirmation in component state:
 *
 *     setNotice('Policy saved.');
 *     router.refresh();
 *
 * That message is then discarded — measured on the live sign-in form, the
 * confirmation was visible for **~40 ms**. The refresh re-renders the server
 * component whose output contains the client component, and state set in the
 * same tick does not survive it. An error message never had this problem (a
 * failure does not refresh), which is why the bug hid for so long: only the
 * *success* path lost its feedback, and only for the human, who sees the form
 * clear and nothing else.
 *
 * **The fix, and why it is a store rather than a context.** The message has to
 * outlive the React subtree that produced it, so it cannot live in that
 * subtree's state — and it cannot live in a provider above it either, because
 * the provider is re-rendered by the very same refresh. A store outside React is
 * unaffected by a remount. `useSyncExternalStore` is the supported way to read
 * one, and it behaves correctly under concurrent rendering (no tearing; the
 * server snapshot is `null` because a server render has no announcement).
 *
 * Messages are scoped by a short key (`'remediation-policy'`) so two panels on
 * one page cannot overwrite each other's confirmation.
 */

export type AnnouncementStore = {
  /** Record a confirmation for `scope`. */
  announce: (scope: string, message: string) => void;
  /** Retire `scope`'s confirmation. Safe to call when there is none. */
  clear: (scope: string) => void;
  /** The current message for `scope`, or `null`. */
  read: (scope: string) => string | null;
  /** Subscribe to changes for one scope; returns the unsubscribe function. */
  subscribe: (scope: string, listener: () => void) => () => void;
};

/**
 * Build an isolated store.
 *
 * A factory rather than module-level state alone so the behaviour can be tested
 * directly (two stores cannot leak into each other's tests) and so the singleton
 * below is one obvious instance rather than hidden globals.
 */
export function createAnnouncementStore(): AnnouncementStore {
  const messages = new Map<string, string>();
  const listeners = new Map<string, Set<() => void>>();

  function emit(scope: string): void {
    // `forEach` rather than `for…of`: the project's `tsconfig` targets below
    // ES2015 downlevel iteration, and a `Set` is not iterable without it.
    listeners.get(scope)?.forEach((listener) => listener());
  }

  return {
    announce(scope, message) {
      messages.set(scope, message);
      emit(scope);
    },
    clear(scope) {
      if (!messages.has(scope)) return; // no spurious re-render on a no-op
      messages.delete(scope);
      emit(scope);
    },
    read(scope) {
      return messages.get(scope) ?? null;
    },
    subscribe(scope, listener) {
      let scopeListeners = listeners.get(scope);
      if (!scopeListeners) {
        scopeListeners = new Set();
        listeners.set(scope, scopeListeners);
      }
      scopeListeners.add(listener);
      return () => {
        scopeListeners?.delete(listener);
      };
    },
  };
}

/** The application-wide store. One instance, shared by every panel. */
export const announcementStore = createAnnouncementStore();

/** Record a confirmation — see the module docstring for why not `useState`. */
export function announce(scope: string, message: string): void {
  announcementStore.announce(scope, message);
}

/** Retire a confirmation. */
export function clearAnnouncement(scope: string): void {
  announcementStore.clear(scope);
}

export type Announcement = {
  /** The current message, or `null` when there is nothing to say. */
  message: string | null;
  /** Dismiss this scope's message. */
  dismiss: () => void;
};

/**
 * Read the announcement for one scope.
 *
 * The server snapshot is `null`: an announcement is a client-side consequence of
 * an action this browser took, so it must never appear in server-rendered HTML.
 */
export function useAnnouncement(scope: string): Announcement {
  const message = useSyncExternalStore(
    (listener) => announcementStore.subscribe(scope, listener),
    () => announcementStore.read(scope),
    () => null
  );
  const dismiss = useCallback(() => announcementStore.clear(scope), [scope]);
  return { message, dismiss };
}
