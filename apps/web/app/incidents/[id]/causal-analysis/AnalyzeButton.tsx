'use client';

/**
 * Trigger the causal analysis for an incident (§34, §35).
 *
 * The backend is idempotent: without `force` an unchanged evidence set returns
 * the stored version, so the default action is "analyse", and re-running is an
 * explicit choice that appends a version rather than overwriting history.
 */

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, type AnalyzeResult } from '@/lib/api';

export default function AnalyzeButton({
  incidentId,
  force,
  label,
}: {
  incidentId: string;
  force?: boolean;
  label?: string;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AnalyzeResult | null>(null);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const outcome = await api.analyzeIncidentCausally(incidentId, {
        force: force ?? false,
        trigger: 'ui',
      });
      setResult(outcome);
      router.refresh();
    } catch (cause: unknown) {
      setError(
        cause instanceof Error ? cause.message : 'The analysis could not be run.'
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-3">
      <button
        type="button"
        onClick={run}
        disabled={busy}
        className="rounded-md bg-argus-accent/20 px-3 py-1.5 text-sm font-medium text-argus-accent hover:bg-argus-accent/30 disabled:opacity-50"
      >
        {busy ? 'Analysing…' : (label ?? (force ? 'Re-run analysis' : 'Run analysis'))}
      </button>
      {result ? (
        <span className="text-xs text-slate-500">
          {result.reused
            ? `Evidence unchanged — analysis v${result.analysis_version} reused.`
            : `Analysis v${result.analysis_version} created.`}
        </span>
      ) : null}
      {error ? <span className="text-xs text-argus-error">{error}</span> : null}
    </div>
  );
}
