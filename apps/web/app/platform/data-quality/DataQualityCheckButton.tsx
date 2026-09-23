'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError } from '@/lib/api';

/**
 * Run the data-quality checks now (§87, §88).
 *
 * The checks are read-only by default: they report what is orphaned without
 * changing anything. Persisting findings is what records them as issues, and
 * that is the choice this button makes explicit.
 */
export default function DataQualityCheckButton({ projectId }: { projectId: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  const run = async (persist: boolean) => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const response = await api.platformDataQualityCheck(projectId, persist);
      const count = response.findings;
      const detail = persist
        ? `opened ${response.opened}, updated ${response.updated}, resolved ${response.resolved}`
        : 'nothing was written';
      setResult(
        `Checked ${response.checked}; found ${count} issue(s) — ${detail}.`
      );
      if (persist) {
        router.refresh();
      }
    } catch (cause: unknown) {
      setError(cause instanceof ApiError ? cause.message : 'The checks could not run.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      <button
        type="button"
        onClick={() => void run(false)}
        disabled={busy}
        className="rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:opacity-50"
      >
        {busy ? 'Running…' : 'Run checks (read-only)'}
      </button>
      <button
        type="button"
        onClick={() => void run(true)}
        disabled={busy}
        className="rounded-md bg-argus-accent/20 px-3 py-1.5 text-sm font-medium text-argus-accent hover:bg-argus-accent/30 disabled:opacity-50"
      >
        Run and record findings
      </button>
      {result ? <p className="text-xs text-slate-400">{result}</p> : null}
      {error ? <p className="text-xs text-argus-error">{error}</p> : null}
    </div>
  );
}
