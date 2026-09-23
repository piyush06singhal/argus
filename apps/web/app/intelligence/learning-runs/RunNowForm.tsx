'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError, type LearningRunSummary } from '@/lib/api';

/**
 * Trigger a learning run by hand (§63).
 *
 * The summary is rendered from the response, never from a hard-coded list of
 * fields-to-show: a run that processed nothing must be able to say so, and a run
 * that learned something must be able to say exactly what. That is §98's rule —
 * the demo reports real counts — applied to the interactive path.
 */
export default function RunNowForm({ projectId }: { projectId: string }) {
  const router = useRouter();
  const [lookback, setLookback] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<LearningRunSummary | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setSummary(null);
    try {
      const parsed = Number.parseInt(lookback, 10);
      const response = await api.triggerLearningRun({
        //: A manual run is always scoped: a run that silently covered every
        //: project would be a different — and much larger — action than the one
        //: the operator asked for.
        project_id: projectId,
        trigger: 'manual',
        ...(Number.isFinite(parsed) && parsed > 0 ? { lookback_days: parsed } : {}),
      });
      setSummary(response);
      router.refresh();
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? `${caught.message} (${caught.status})`
          : String(caught)
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-3">
      <label className="flex flex-col gap-1 text-xs text-slate-400">
        Lookback window in days (optional)
        <input
          value={lookback}
          onChange={(event) => setLookback(event.target.value)}
          placeholder="leave empty for the default window"
          className="input"
        />
      </label>
      <button type="submit" className="btn" disabled={busy || !projectId}>
        {busy ? 'Running…' : 'Run learning now'}
      </button>
      <p className="text-xs text-slate-500">
        The run reads only completed history. It never modifies source code,
        policies, authorization or verification logic.
      </p>
      {!projectId ? (
        <p className="text-xs text-argus-warning">
          A project scope is required: open this page with{' '}
          <code className="font-mono">?project_id=…</code> from the Learning
          Center.
        </p>
      ) : null}

      {error ? <p className="text-sm text-argus-error">Refused: {error}</p> : null}

      {summary ? (
        <div className="rounded-md border border-slate-800 bg-slate-950/60 p-3 text-xs text-slate-300">
          <p className="font-medium text-slate-200">
            Run {summary.status}
            {summary.run_id ? ` · ${summary.run_id.slice(0, 8)}` : ''}
          </p>
          <ul className="mt-2 space-y-1">
            <li>· {summary.events_processed} events processed</li>
            <li>
              · {summary.experiences_created} experiences created,{' '}
              {summary.experiences_updated} updated, {summary.experiences_flagged}{' '}
              flagged
            </li>
            <li>
              · {summary.patterns_discovered} patterns discovered ({' '}
              {summary.patterns_validated} validated, {summary.patterns_rejected}{' '}
              rejected)
            </li>
            <li>
              · {summary.knowledge_created} knowledge created,{' '}
              {summary.knowledge_updated} updated, {summary.knowledge_activated}{' '}
              activated, {summary.knowledge_deprecated} deprecated
            </li>
            <li>
              · {summary.relationships_created ?? 0} relationships created,{' '}
              {summary.relationships_updated ?? 0} refreshed,{' '}
              {summary.relationships_stale ?? 0} marked stale
            </li>
            <li>
              · {summary.recommendations_created} recommendations created,{' '}
              {summary.recommendations_expired} expired
            </li>
          </ul>
          {summary.skipped_reasons.length > 0 ? (
            <p className="mt-2 text-slate-400">
              Skipped: {summary.skipped_reasons.join(', ')}
            </p>
          ) : null}
          {Object.keys(summary.unprocessable).length > 0 ? (
            <div className="mt-2 text-slate-400">
              Not processable:
              <ul className="mt-1 space-y-1">
                {Object.entries(summary.unprocessable).map(([id, reason]) => (
                  <li key={id}>
                    · <span className="font-mono">{id.slice(0, 8)}</span> — {reason}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {summary.errors.length > 0 ? (
            <ul className="mt-2 space-y-1 text-argus-error">
              {summary.errors.map((message) => (
                <li key={message}>· {message}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </form>
  );
}
