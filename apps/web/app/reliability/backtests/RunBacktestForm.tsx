'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError } from '@/lib/api';

/**
 * Run a backtest (§55).
 *
 * Every field is bounded: the horizon comes from the fixed set, the step is
 * clamped to the horizon, and `max_steps` caps the replay so a wide window
 * cannot turn into an unbounded scan. Failures surface verbatim — "training
 * window too short" is information, not noise.
 */
export default function RunBacktestForm({ projectId }: { projectId: string }) {
  const router = useRouter();
  const [days, setDays] = useState(7);
  const [horizon, setHorizon] = useState('ONE_HOUR');
  const [trainingHours, setTrainingHours] = useState(24);
  const [maxSteps, setMaxSteps] = useState(24);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setMessage(null);
    const end = new Date();
    const start = new Date(end.getTime() - days * 86_400_000);
    try {
      const result = await api.runBacktest(projectId, {
        start_time: start.toISOString(),
        end_time: end.toISOString(),
        training_window_seconds: trainingHours * 3600,
        forecast_horizon: horizon as never,
        prediction_type: 'FAILURE_RISK',
        step_seconds: 3600,
        max_steps: maxSteps,
        created_by: 'reliability-ui',
      });
      setMessage(
        `Ran ${result.total} backtest(s). ${result.note}`,
      );
      router.refresh();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card">
      <h2 className="font-medium text-slate-200">Run a backtest</h2>
      <form onSubmit={submit} className="mt-3 grid gap-3 md:grid-cols-4">
        <label className="text-xs text-slate-400">
          Lookback (days)
          <input
            type="number"
            min={1}
            max={30}
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
            className="mt-1 w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
          />
        </label>
        <label className="text-xs text-slate-400">
          Forecast horizon
          <select
            value={horizon}
            onChange={(event) => setHorizon(event.target.value)}
            className="mt-1 w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
          >
            <option value="ONE_HOUR">1 hour</option>
            <option value="SIX_HOURS">6 hours</option>
            <option value="TWENTY_FOUR_HOURS">24 hours</option>
          </select>
        </label>
        <label className="text-xs text-slate-400">
          Training window (hours)
          <input
            type="number"
            min={1}
            max={168}
            value={trainingHours}
            onChange={(event) => setTrainingHours(Number(event.target.value))}
            className="mt-1 w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
          />
        </label>
        <label className="text-xs text-slate-400">
          Max origins
          <input
            type="number"
            min={1}
            max={200}
            value={maxSteps}
            onChange={(event) => setMaxSteps(Number(event.target.value))}
            className="mt-1 w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
          />
        </label>
        <div className="md:col-span-4">
          <button
            type="submit"
            disabled={busy}
            className="rounded bg-argus-accent/20 px-4 py-2 text-sm text-argus-accent disabled:opacity-50"
          >
            {busy ? 'Replaying history…' : 'Run backtest'}
          </button>
          <span className="ml-3 text-xs text-slate-500">
            The label window is the horizon itself — scoring against a window the
            caller chose would make the metric mean whatever the caller wanted.
          </span>
        </div>
      </form>
      {message ? <p className="mt-3 text-sm text-argus-success">{message}</p> : null}
      {error ? <p className="mt-3 text-sm text-argus-error">{error}</p> : null}
    </section>
  );
}
