'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError } from '@/lib/api-client';

/**
 * Disable or re-enable one provisioned person.
 *
 * Disabling is offered as a single action because it *is* a single decision:
 * the API marks the identity disabled and revokes every session it holds in the
 * same transaction. Leaving the sessions alive would make "disabled" mean
 * "disabled whenever the shortest token happened to expire".
 *
 * The number of sessions is shown before the click, so the blast radius of the
 * action is visible rather than described.
 */
export default function IdentityControls({
  identityId,
  disabled,
  scope,
  activeSessions,
}: {
  identityId: string;
  disabled: boolean;
  scope: string;
  activeSessions: number;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  async function run(action: 'disable' | 'enable') {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const outcome =
        action === 'disable'
          ? await api.disableOidcIdentity(identityId)
          : await api.enableOidcIdentity(identityId);
      setResult(
        action === 'disable'
          ? `Disabled — ${outcome.revoked_sessions} session(s) revoked.`
          : 'Enabled — they may sign in again; old sessions stay revoked.'
      );
      router.refresh();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : 'The request could not be sent.'
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-2">
        <span className="text-xs text-slate-500">{scope}</span>
        <button
          type="button"
          disabled={busy}
          onClick={() => run(disabled ? 'enable' : 'disable')}
          className={`rounded border px-2 py-1 text-xs disabled:opacity-50 ${
            disabled
              ? 'border-emerald-800 text-emerald-300 hover:bg-emerald-950/40'
              : 'border-red-900 text-red-300 hover:bg-red-950/40'
          }`}
        >
          {busy ? 'Working…' : disabled ? 'Re-enable' : 'Disable'}
        </button>
      </div>
      {!disabled && activeSessions > 0 ? (
        <span className="text-xs text-slate-500">
          revokes {activeSessions} live session
          {activeSessions === 1 ? '' : 's'}
        </span>
      ) : null}
      {error ? <span className="text-xs text-amber-300">{error}</span> : null}
      {result ? (
        <span className="text-xs text-emerald-300">{result}</span>
      ) : null}
    </div>
  );
}
