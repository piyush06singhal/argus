import Link from 'next/link';

import { api, formatDate, type AnomalyObservation } from '@/lib/api';
import {
  ANOMALY_SEVERITY_STYLES,
  ANOMALY_STATUS_STYLES,
  formatDeviation,
  formatValue,
} from '@/lib/incidents';

export const metadata = {
  title: 'Anomaly Detail',
};

export const dynamic = 'force-dynamic';

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-xs font-medium uppercase tracking-wider text-slate-500">
        {label}
      </p>
      <p className="mt-1 text-sm text-slate-300">{value ?? '—'}</p>
    </div>
  );
}

export default async function AnomalyDetailPage({
  params,
}: {
  params: { id: string };
}) {
  const { id } = params;

  try {
    const anomaly = await api.getAnomaly(id);
    const explanation = anomaly.explanation;

    return (
      <div className="space-y-6">
        <div>
          <Link
            href="/anomalies"
            className="text-sm text-slate-400 hover:text-argus-accent"
          >
            ← Back to anomaly center
          </Link>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-semibold text-slate-100">
              {anomaly.anomaly_type}
            </h1>
            <span
              className={`badge ${
                ANOMALY_SEVERITY_STYLES[anomaly.severity] ??
                ANOMALY_SEVERITY_STYLES.LOW
              }`}
            >
              {anomaly.severity}
            </span>
            <span
              className={`badge ${
                ANOMALY_STATUS_STYLES[anomaly.status] ??
                ANOMALY_STATUS_STYLES.DETECTED
              }`}
            >
              {anomaly.status}
            </span>
            {anomaly.suppressed ? (
              <span className="badge bg-slate-700/40 text-slate-300">
                suppressed
              </span>
            ) : null}
          </div>
          <p className="mt-1 text-xs text-slate-500">
            Anomaly ID: {anomaly.id}
            {anomaly.incident_id ? (
              <>
                {' · '}
                <Link
                  href={`/incidents/${anomaly.incident_id}`}
                  className="text-argus-accent hover:text-argus-accent-hover"
                >
                  correlated into an incident
                </Link>
              </>
            ) : (
              ' · not correlated into an incident'
            )}
          </p>
        </div>

        <section className="card">
          <h2 className="mb-2 font-medium text-slate-200">
            Why was this detected?
          </h2>
          <p className="text-sm text-slate-300">
            {explanation?.why_detected ?? anomaly.description ?? '—'}
          </p>
          {explanation?.confidence_meaning ? (
            <p className="mt-3 text-xs text-slate-500">
              {explanation.confidence_meaning}
            </p>
          ) : null}
        </section>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div className="card">
            <Field
              label="Observed"
              value={formatValue(anomaly.observed_value)}
            />
          </div>
          <div className="card">
            <Field
              label="Expected"
              value={formatValue(anomaly.expected_value)}
            />
          </div>
          <div className="card">
            <Field label="Deviation" value={formatDeviation(anomaly.deviation)} />
          </div>
          <div className="card">
            <Field
              label="Z-score"
              value={formatValue(anomaly.z_score)}
            />
          </div>
        </div>

        <section className="card">
          <h2 className="mb-3 font-medium text-slate-200">
            Detection context
          </h2>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <Field
              label="Telemetry source"
              value={explanation?.telemetry_source ?? anomaly.source}
            />
            <Field
              label="Detector"
              value={explanation?.detector ?? 'rule_engine'}
            />
            <Field label="Rule condition" value={explanation?.condition} />
            <Field
              label="Baseline strategy"
              value={explanation?.baseline_strategy}
            />
            <Field
              label="Baseline samples"
              value={explanation?.baseline_sample_count}
            />
            <Field
              label="Baseline window"
              value={
                explanation?.baseline_window_seconds
                  ? `${explanation.baseline_window_seconds}s`
                  : null
              }
            />
            <Field label="Metric" value={anomaly.metric_name} />
            <Field label="Log pattern" value={anomaly.pattern_template} />
            <Field
              label="Component"
              value={
                anomaly.component_id ? (
                  <span className="font-mono text-xs">
                    {anomaly.component_id}
                  </span>
                ) : null
              }
            />
            <Field label="Detected at" value={formatDate(anomaly.detected_at)} />
            <Field label="Last seen" value={formatDate(anomaly.last_seen_at)} />
            <Field
              label="Observations"
              value={anomaly.observation_count}
            />
          </div>
          {explanation?.fingerprint_material ? (
            <p className="mt-4 text-xs text-slate-500">
              Deduplication fingerprint material:{' '}
              <span className="font-mono">{explanation.fingerprint_material}</span>
            </p>
          ) : null}
          {anomaly.suppression_reason ? (
            <p className="mt-3 text-xs text-slate-500">
              Suppressed by policy: {anomaly.suppression_reason} — the anomaly
              is still recorded and visible.
            </p>
          ) : null}
        </section>

        <section className="card">
          <div className="mb-3 flex items-baseline justify-between">
            <h2 className="font-medium text-slate-200">Observations</h2>
            <span className="text-xs text-slate-500">
              {anomaly.observations.length} shown
            </span>
          </div>
          {anomaly.observations.length === 0 ? (
            <p className="text-sm text-slate-400">
              No supporting observations have been recorded.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-800 text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                    <th className="px-3 py-2">Observed at</th>
                    <th className="px-3 py-2">Observed</th>
                    <th className="px-3 py-2">Expected</th>
                    <th className="px-3 py-2">Deviation</th>
                    <th className="px-3 py-2">Samples</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800">
                  {anomaly.observations.map(
                    (observation: AnomalyObservation) => (
                      <tr key={observation.id}>
                        <td className="whitespace-nowrap px-3 py-2 text-slate-400">
                          {formatDate(observation.observed_at)}
                        </td>
                        <td className="px-3 py-2 text-slate-300">
                          {formatValue(observation.observed_value)}
                        </td>
                        <td className="px-3 py-2 text-slate-300">
                          {formatValue(observation.expected_value)}
                        </td>
                        <td className="px-3 py-2 text-slate-300">
                          {formatDeviation(observation.deviation)}
                        </td>
                        <td className="px-3 py-2 text-slate-400">
                          {observation.sample_count ?? '—'}
                        </td>
                      </tr>
                    )
                  )}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    );
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Unknown error occurred';
    return (
      <div className="space-y-6">
        <Link
          href="/anomalies"
          className="text-sm text-slate-400 hover:text-argus-accent"
        >
          ← Back to anomaly center
        </Link>
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load anomaly
          </h2>
          <p className="mt-2 text-sm text-slate-400">
            Could not load anomaly {id}. ({message})
          </p>
        </div>
      </div>
    );
  }
}
