'use client';

import { useAnnouncement } from '@/lib/announce';

/**
 * Renders the confirmation recorded by `announce(scope, …)`.
 *
 * `role="status"` with `aria-live="polite"` is the whole point: a confirmation
 * the user cannot see is indistinguishable from a failed action, and a screen
 * reader user gets no signal at all without a live region. The dismiss control
 * is explicit rather than a timer, because a message that vanishes on a clock is
 * how a user misses it while reading something else.
 */
export default function Announcement({
  scope,
  tone = 'success',
}: {
  /** The same key passed to `announce()` / `clearAnnouncement()`. */
  scope: string;
  tone?: 'success' | 'info';
}) {
  const { message, dismiss } = useAnnouncement(scope);
  if (!message) return null;

  const palette =
    tone === 'success'
      ? 'border-emerald-900 bg-emerald-950/40 text-emerald-200'
      : 'border-slate-700 bg-slate-900/60 text-slate-200';

  return (
    <p
      role="status"
      aria-live="polite"
      className={`flex items-start justify-between gap-3 rounded border px-3 py-2 text-sm ${palette}`}
    >
      <span>{message}</span>
      <button
        type="button"
        onClick={dismiss}
        aria-label="Dismiss confirmation"
        className="shrink-0 rounded px-1 text-xs text-slate-300 hover:text-slate-100"
      >
        Dismiss
      </button>
    </p>
  );
}
