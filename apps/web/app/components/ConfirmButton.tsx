'use client';

import { useEffect, useId, useRef, useState } from 'react';

import {
  type DestructiveActionKind,
  confirmationFor,
} from '@/lib/confirm';

/**
 * A destructive action that takes two deliberate steps (hardening W7).
 *
 * Property it is built around: **the confirmation is in the tab order, not in a
 * browser `confirm()` dialog.** A native dialog is unstyleable, untranslatable,
 * and blocks the whole page — and on the two screens that matter most here
 * (token revocation, emergency stop) the user needs to *read* what they are
 * about to do, next to the thing they are doing it to.
 *
 * What it guarantees:
 *
 * - the first click **arms** rather than acts — nothing is sent until the user
 *   answers the question, which names the object (`subject`);
 * - the consequence and the reversibility are both stated before the second
 *   click, so "are you sure?" never stands alone;
 * - `Escape` cancels, focus moves to the confirm control, and the state is
 *   announced through `aria-live` for a screen reader;
 * - arming resets if the user clicks away, so a stale armed state cannot be
 *   triggered by a later tab-through.
 */
export default function ConfirmButton({
  kind,
  subject,
  onConfirm,
  disabled = false,
  className = '',
  testId,
}: {
  kind: DestructiveActionKind;
  subject?: string | null;
  onConfirm: () => void | Promise<void>;
  disabled?: boolean;
  className?: string;
  testId?: string;
}) {
  const copy = confirmationFor(kind, subject);
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const warning = useId();

  useEffect(() => {
    if (!armed) {
      return;
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setArmed(false);
      }
    };
    const onPointerDown = (event: MouseEvent) => {
      if (
        container.current &&
        !container.current.contains(event.target as Node)
      ) {
        setArmed(false);
      }
    };
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('mousedown', onPointerDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('mousedown', onPointerDown);
    };
  }, [armed]);

  const confirm = async () => {
    setBusy(true);
    try {
      await onConfirm();
      setArmed(false);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div ref={container} className="inline-flex flex-col items-start gap-2">
      {!armed ? (
        <button
          type="button"
          disabled={disabled}
          onClick={() => setArmed(true)}
          aria-label={`${copy.action}${subject ? `: ${subject}` : ''}`}
          className={className}
          data-testid={testId}
        >
          {copy.action}
        </button>
      ) : (
        <div
          role="group"
          aria-describedby={warning}
          className="rounded border border-red-900 bg-red-950/40 p-3"
        >
          <p className="text-xs font-medium text-red-100">{copy.question}</p>
          <p id={warning} className="mt-1 max-w-sm text-xs text-red-200/80">
            {copy.consequence}
          </p>
          <p className="mt-1 max-w-sm text-xs text-red-200/60">
            {copy.reversibility}
          </p>
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              autoFocus
              disabled={busy}
              onClick={confirm}
              className="rounded bg-red-800 px-3 py-1 text-xs font-medium text-white disabled:opacity-50"
              data-testid={testId ? `${testId}-confirm` : undefined}
            >
              {busy ? 'Working…' : copy.confirmLabel}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setArmed(false)}
              className="rounded border border-slate-700 px-3 py-1 text-xs text-slate-200 disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      <span aria-live="polite" className="sr-only">
        {armed ? copy.question : ''}
      </span>
    </div>
  );
}
