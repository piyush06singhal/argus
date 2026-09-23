'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError } from '@/lib/api';
import { caseStatusLabel, TERMINAL_CASE_STATUSES } from '@/lib/platform';

/**
 * Case status transitions (§16, §20).
 *
 * The buttons are built from `allowed_transitions`, which the backend state
 * machine computed. The UI never offers a transition the API would reject — the
 * alternative (a permissive dropdown plus server-side failure) teaches operators
 * to distrust the interface. A terminal case is read-only, and says so rather
 * than rendering disabled buttons with no explanation.
 */
export default function CaseControls({
  caseId,
  projectId,
  status,
  allowedTransitions,
  terminal,
}: {
  caseId: string;
  projectId: string;
  status: string;
  allowedTransitions: string[];
  terminal: boolean;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState('');

  const transition = async (next: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.platformCaseStatus({
        caseId,
        projectId,
        status: next,
        reason: reason.trim() || undefined,
        actor: 'ui',
      });
      router.refresh();
    } catch (cause: unknown) {
      setError(
        cause instanceof ApiError ? cause.message : 'The transition was rejected.'
      );
    } finally {
      setBusy(false);
    }
  };

  if (terminal) {
    return (
      <section className="card">
        <h2 className="font-medium text-slate-200">Case controls</h2>
        <p className="mt-2 text-sm text-slate-400">
          This case is <strong className="text-slate-200">{caseStatusLabel(status)}</strong>{' '}
          — a terminal state. No further transition is offered.
        </p>
      </section>
    );
  }

  return (
    <section className="card">
      <h2 className="font-medium text-slate-200">Case controls</h2>
      <p className="mt-1 text-xs text-slate-500">
        Only transitions the case state machine permits are offered. Phase 9
        guards any actual remediation regardless of case status.
      </p>
      {allowedTransitions.length === 0 ? (
        <p className="mt-3 text-sm text-slate-400">
          No transition is available from {caseStatusLabel(status)} right now.
        </p>
      ) : (
        <>
          <input
            className="input mt-3"
            placeholder="Reason (recorded on the transition)"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
          <div className="mt-3 flex flex-wrap gap-2">
            {(TERMINAL_CASE_STATUSES as readonly string[])
              .filter((item) => allowedTransitions.includes(item))
              .map((item) => (
                <button
                  key={item}
                  type="button"
                  onClick={() => transition(item)}
                  disabled={busy}
                  className="rounded-md border border-slate-700 px-3 py-1.5 text-sm font-medium text-slate-300 hover:bg-slate-800 disabled:opacity-50"
                  title="Terminal — no further transition"
                >
                  {caseStatusLabel(item)}
                </button>
              ))}
            {allowedTransitions
              .filter((item) => !(TERMINAL_CASE_STATUSES as readonly string[]).includes(item))
              .map((item) => (
                <button
                  key={item}
                  type="button"
                  onClick={() => transition(item)}
                  disabled={busy}
                  className="rounded-md bg-argus-accent/20 px-3 py-1.5 text-sm font-medium text-argus-accent hover:bg-argus-accent/30 disabled:opacity-50"
                >
                  {caseStatusLabel(item)}
                </button>
              ))}
          </div>
        </>
      )}
      {error ? <p className="mt-2 text-xs text-argus-error">{error}</p> : null}
    </section>
  );
}
