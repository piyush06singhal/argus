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

import { api, type AnalyzeResult } from '@/lib/api-client';
import { announce, clearAnnouncement } from '@/lib/announce';

import Announcement from '@/app/components/Announcement';

//: Announcement scope, so this panel's confirmation cannot collide with another's.
//: It is keyed per incident because an incident page can mount two of these
//: (analyse, re-run) and each should speak for itself.
const announceScope = (incidentId: string, force?: boolean) =>
  `causal-analysis:${incidentId}:${force ? 'forced' : 'default'}`;

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
  const scope = announceScope(incidentId, force);

  const run = async () => {
    setBusy(true);
    setError(null);
    clearAnnouncement(scope);
    try {
      const outcome = await api.analyzeIncidentCausally(incidentId, {
        force: force ?? false,
        trigger: 'ui',
      });
      // Recorded outside React: `router.refresh()` re-renders this component's
      // server parent, and a message held in component state did not survive it.
      announce(
        scope,
        outcome.reused
          ? `Evidence unchanged — analysis v${outcome.analysis_version} reused.`
          : `Analysis v${outcome.analysis_version} created.`
      );
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
      <Announcement scope={scope} tone="info" />
      {error ? <span className="text-xs text-argus-error">{error}</span> : null}
    </div>
  );
}
