'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError } from '@/lib/api-client';
import { announce, clearAnnouncement } from '@/lib/announce';
import {
  clearTokenCookie,
  describeToken,
  writeTokenCookie,
} from '@/lib/argus-auth';

import Announcement from './Announcement';

//: Announcement scope, so the confirmation cannot collide with another panel's.
const ANNOUNCE_SCOPE = 'connect-token';

/**
 * Connect this browser to ARGUS with a token (hardening W1).
 *
 * The token is validated against the backend **before** it is stored, so a
 * typo or a collector token is reported immediately with the backend's own
 * reason instead of turning into a wall of 401s across the app. Nothing is
 * hidden: the identity the token actually carries is shown, because the
 * quickest way to misunderstand ARGUS is to believe you have broader access
 * than your credential grants.
 */
export default function TokenForm({
  compact = false,
}: {
  /** `compact` is used by the error boundary, where the page failed already. */
  compact?: boolean;
}) {
  const router = useRouter();
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function connect(event: React.FormEvent) {
    event.preventDefault();
    const raw = value.trim();
    if (!raw) {
      setError('Paste a token first.');
      return;
    }
    setBusy(true);
    setError(null);
    // A previous confirmation is retired when a new attempt starts, so the
    // operator is never shown a stale "Connected as …" beside a fresh error.
    clearAnnouncement(ANNOUNCE_SCOPE);
    try {
      // An ingest token is single-purpose: say so instead of letting the user
      // conclude the platform is broken when the console refuses it.
      if (describeToken(raw) === 'ingestion token (collectors only)') {
        setError(
          'That is an ingestion token — it can only send telemetry. Use an API token (argus_…) for the console.'
        );
        return;
      }
      const me = await api.whoami(raw);
      writeTokenCookie(raw);
      // Recorded outside React: `router.refresh()` re-renders this component's
      // server parent, and a confirmation held in component state did not
      // survive it (measured on this very form: visible for ~40 ms).
      announce(
        ANNOUNCE_SCOPE,
        `Connected as ${me.name} (${me.role})${
          me.unrestricted ? ' — all projects' : ` — ${me.project_ids.length} project(s)`
        }`
      );
      setValue('');
      router.refresh();
    } catch (err) {
      const detail =
        err instanceof ApiError
          ? err.status === 401
            ? 'The backend rejected that token (unknown, expired or revoked).'
            : `${err.message} (HTTP ${err.status})`
          : 'Could not reach the ARGUS API. Is the backend running?';
      setError(detail);
    } finally {
      setBusy(false);
    }
  }

  function disconnect() {
    clearTokenCookie();
    announce(ANNOUNCE_SCOPE, 'Token cleared from this browser.');
    router.refresh();
  }

  return (
    <form
      onSubmit={connect}
      className={compact ? 'space-y-3' : 'space-y-4'}
      data-testid="token-form"
    >
      <div>
        <label
          htmlFor="argus-token"
          className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-400"
        >
          API token
        </label>
        <input
          id="argus-token"
          name="token"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="argus_…"
          className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm text-slate-100 placeholder:text-slate-600 focus:border-argus-accent focus:outline-none"
        />
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="submit"
          disabled={busy}
          className="rounded bg-argus-accent px-4 py-2 text-sm font-medium text-slate-950 disabled:opacity-50"
        >
          {busy ? 'Checking…' : 'Connect'}
        </button>
        <button
          type="button"
          onClick={disconnect}
          className="rounded border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:border-slate-500"
        >
          Sign out of this browser
        </button>
      </div>
      {error && (
        <p
          role="alert"
          className="rounded border border-red-900 bg-red-950/40 px-3 py-2 text-sm text-red-200"
        >
          {error}
        </p>
      )}
      <Announcement scope={ANNOUNCE_SCOPE} />
    </form>
  );
}
