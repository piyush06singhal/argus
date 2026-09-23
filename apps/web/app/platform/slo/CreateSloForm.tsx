'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError } from '@/lib/api';

/**
 * The indicator set the backend accepts, in its own vocabulary — the labels carry
 * the unit because the same number means a ratio for availability and
 * milliseconds for latency.
 */
const INDICATORS = [
  { value: 'AVAILABILITY', label: 'Availability (ratio 0–1)' },
  { value: 'ERROR_RATE', label: 'Error rate (ratio 0–1)' },
  { value: 'LATENCY', label: 'Latency (milliseconds)' },
  { value: 'SATURATION', label: 'Saturation (percent)' },
  { value: 'CUSTOM', label: 'Custom (names its own metric)' },
];

/**
 * Which side of the target is good, in the names the API uses. These were
 * ``GTE``/``LTE``, which the API does not accept, so every objective created here
 * was rejected with a 422 — the form was offering a comparison that could not be
 * stored.
 */
const COMPARISONS = [
  { value: 'AT_LEAST', label: 'At least (≥)' },
  { value: 'AT_MOST', label: 'At most (≤)' },
];

/**
 * Define a service-level objective (§32, §91).
 *
 * The target's unit depends on the indicator, and the form says which: the same
 * number means a ratio for availability and milliseconds for latency. An
 * objective must also name the metric it measures — that is what the evaluator
 * reads — so the field is required unless the indicator is ``CUSTOM``.
 */
export default function CreateSloForm({ projectId }: { projectId: string }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [name, setName] = useState('');
  const [indicator, setIndicator] = useState(INDICATORS[0].value);
  const [metricName, setMetricName] = useState('');
  const [target, setTarget] = useState('0.99');
  const [comparison, setComparison] = useState(COMPARISONS[0].value);

  const submit = async () => {
    const targetValue = Number.parseFloat(target);
    if (!name.trim() || Number.isNaN(targetValue)) {
      setError('A name and a numeric target are required.');
      return;
    }
    if (indicator !== 'CUSTOM' && !metricName.trim()) {
      setError('Name the metric this objective measures, or choose the custom indicator.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.platformCreateSlo(projectId, {
        name: name.trim(),
        indicator,
        target: targetValue,
        comparison,
        metric_name: metricName.trim() || undefined,
        actor: 'ui',
      });
      setOpen(false);
      setName('');
      router.refresh();
    } catch (cause: unknown) {
      setError(cause instanceof ApiError ? cause.message : 'The objective was rejected.');
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <section className="card">
        <div className="flex items-center justify-between">
          <h2 className="font-medium text-slate-200">Define an objective</h2>
          <button
            type="button"
            onClick={() => setOpen(true)}
            className="rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800"
          >
            New objective
          </button>
        </div>
      </section>
    );
  }

  return (
    <section className="card">
      <h2 className="font-medium text-slate-200">Define an objective</h2>
      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        <input className="input" placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} />
        <select className="input" value={indicator} onChange={(e) => setIndicator(e.target.value)}>
          {INDICATORS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
        <input
          className="input"
          placeholder={indicator === 'CUSTOM' ? 'Metric (optional)' : 'Metric name (e.g. http.checkout.error_rate)'}
          value={metricName}
          onChange={(e) => setMetricName(e.target.value)}
        />
        <input className="input" placeholder="Target" value={target} onChange={(e) => setTarget(e.target.value)} />
        <select className="input" value={comparison} onChange={(e) => setComparison(e.target.value)}>
          {COMPARISONS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      </div>
      <div className="mt-3 flex gap-2">
        <button
          type="button"
          onClick={() => void submit()}
          disabled={busy}
          className="rounded-md bg-argus-accent/20 px-3 py-1.5 text-sm font-medium text-argus-accent hover:bg-argus-accent/30 disabled:opacity-50"
        >
          {busy ? 'Saving…' : 'Create objective'}
        </button>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800"
        >
          Cancel
        </button>
      </div>
      {error ? <p className="mt-2 text-xs text-argus-error">{error}</p> : null}
    </section>
  );
}
