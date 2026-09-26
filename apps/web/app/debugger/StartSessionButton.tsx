'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError } from '@/lib/api-client';

/**
 * Open a debugging session for an incident (§34).
 *
 * `index_snapshot` indexes the deployment-derived revision before analysing:
 * the slow, thorough path, so it is an explicit choice rather than the
 * default. A failed request is surfaced verbatim — "no readable repository"
 * is information, not noise.
 */
export default function StartDebugSessionButton({
  incidentId,
  projectId,
  hasSnapshot,
}: {
  incidentId: string;
  projectId: string;
  hasSnapshot: boolean;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (indexSnapshot: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const session = await api.createDebugSession(incidentId, projectId, {
        run_analysis: true,
        index_snapshot: indexSnapshot,
        created_by: 'ui',
      });
      router.push(
        `/debugger/${session.id}?project_id=${encodeURIComponent(projectId)}`
      );
    } catch (cause: unknown) {
      setError(
        cause instanceof ApiError
          ? cause.message
          : 'The debug session could not be started.'
      );
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => run(false)}
          disabled={busy}
          className="rounded-md bg-argus-accent/20 px-3 py-1.5 text-sm font-medium text-argus-accent hover:bg-argus-accent/30 disabled:opacity-50"
        >
          {busy ? 'Starting…' : 'Start debug session'}
        </button>
        {!hasSnapshot ? (
          <button
            type="button"
            onClick={() => run(true)}
            disabled={busy}
            className="rounded-md border border-slate-700 px-3 py-1.5 text-sm font-medium text-slate-200 hover:bg-slate-800 disabled:opacity-50"
            title="Index the deployment-derived revision first, then analyse"
          >
            Index code, then start
          </button>
        ) : null}
      </div>
      {error ? <p className="text-xs text-argus-error">{error}</p> : null}
    </div>
  );
}
