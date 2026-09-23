'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError } from '@/lib/api';

//: The dispositions the API accepts. ``SUPPRESSED`` was offered here and is not a
//: status the backend has, so choosing it was rejected.
const STATUSES = ['OPEN', 'ACKNOWLEDGED', 'RESOLVED', 'IGNORED'];

/**
 * Change one data-quality issue's status (§90).
 *
 * The issue's *findings* are never mutated — only the operator's disposition of
 * them is recorded. `SUPPRESSED` is offered but not the default, because
 * suppressing a real orphan is how a data-quality center becomes decorative.
 */
export default function DataQualityIssueControls({
  issueId,
  projectId,
  status,
}: {
  issueId: string;
  projectId: string;
  status: string;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const setStatus = async (next: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.platformDataQualityStatus({
        issueId,
        projectId,
        status: next,
        actor: 'ui',
      });
      router.refresh();
    } catch (cause: unknown) {
      setError(cause instanceof ApiError ? cause.message : 'The change was rejected.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-slate-800/60 pt-3">
      <span className="text-xs uppercase tracking-wider text-slate-500">
        {status}
      </span>
      {STATUSES.filter((item) => item !== status).map((item) => (
        <button
          key={item}
          type="button"
          onClick={() => void setStatus(item)}
          disabled={busy}
          className="rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-50"
        >
          {item.toLowerCase()}
        </button>
      ))}
      {error ? <span className="text-xs text-argus-error">{error}</span> : null}
    </div>
  );
}
