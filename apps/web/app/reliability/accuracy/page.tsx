import Link from 'next/link';

import {
  api,
  formatDate,
  type EvaluationRun,
  type PaginatedResponse,
  type Project,
} from '@/lib/api';
import {
  calibrationStyle,
  HORIZON_LABELS,
  OUTCOME_LABELS,
  percentLabel,
  UNKNOWN_RISK_NOTE,
} from '@/lib/reliability';

export const metadata = {
  title: 'Prediction accuracy',
};

export const dynamic = 'force-dynamic';

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className="text-slate-200">{value}</dd>
    </div>
  );
}

/**
 * The prediction accuracy dashboard (§53).
 *
 * The rule this page enforces is the one that keeps the numbers honest: a
 * metric and its sample size are inseparable, and a run that was
 * INSUFFICIENT_SAMPLE renders its notes instead of a percentage.
 */
export default async function AccuracyPage({
  searchParams,
}: {
  searchParams: { project_id?: string };
}) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  let projects: Project[] = [];
  try {
    const response: PaginatedResponse<Project> = await api.listProjects(1, 50);
    projects = response.items;
  } catch {
    projects = [];
  }
  const project = projects.find((item) => item.id === requested) ?? projects[0];

  let runs: EvaluationRun[] = [];
  let error: string | null = null;
  if (project) {
    try {
      const response = await api.listEvaluationRuns({ project_id: project.id, limit: 20 });
      runs = response.items;
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">
          Prediction accuracy
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          Every evaluation run is immutable history: the window, the filters, the
          confusion counts and the calibration that justified the numbers. {UNKNOWN_RISK_NOTE}
        </p>
      </div>

      <section className="card">
        <div className="flex flex-wrap gap-2">
          {projects.map((item) => (
            <Link
              key={item.id}
              href={`/reliability/accuracy?project_id=${encodeURIComponent(item.id)}`}
              className={`badge ${
                item.id === project?.id
                  ? 'bg-argus-accent/20 text-argus-accent'
                  : 'bg-slate-800 text-slate-300'
              }`}
            >
              {item.name}
            </Link>
          ))}
        </div>
      </section>

      {error ? (
        <section className="card">
          <p className="text-sm text-argus-error">{error}</p>
        </section>
      ) : null}

      {!error && runs.length === 0 ? (
        <section className="card">
          <p className="text-sm text-slate-400">
            No evaluation runs yet. Forecasts are scored once their horizons elapse;
            POST /api/v1/reliability/evaluate (or wait for the scheduled worker)
            to produce the first run.
          </p>
        </section>
      ) : null}

      {runs.map((run) => {
        const metrics = run.metrics as Record<string, number | undefined>;
        const enough =
          run.sample_count > 0 && run.status !== 'INSUFFICIENT_SAMPLE';
        return (
          <section
            key={run.id}
            className="card"
          >
            <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
              <h2 className="font-medium text-slate-200">
                Run {run.id.slice(0, 8)} · {formatDate(run.created_at)}
              </h2>
              <span className="text-xs text-slate-500">
                {run.prediction_type ?? 'all types'} ·{' '}
                {run.forecast_horizon
                  ? HORIZON_LABELS[run.forecast_horizon]
                  : 'all horizons'}{' '}
                · status {run.status}
              </span>
            </div>
            <dl className="grid grid-cols-2 gap-4 md:grid-cols-4">
              <Metric label="Sample size" value={String(run.sample_count)} />
              <Metric
                label="Positive / negative"
                value={`${run.positive_count} / ${run.negative_count}`}
              />
              <Metric
                label="Inconclusive"
                value={String(run.inconclusive_count)}
              />
              <Metric
                label="Calibration"
                value={run.calibration_status}
              />
              <Metric
                label="Precision"
                value={
                  enough && metrics.precision != null
                    ? percentLabel(metrics.precision)
                    : 'not reported'
                }
              />
              <Metric
                label="Recall"
                value={
                  enough && metrics.recall != null
                    ? percentLabel(metrics.recall)
                    : 'not reported'
                }
              />
              <Metric
                label="False-positive rate"
                value={
                  enough && metrics.false_positive_rate != null
                    ? percentLabel(metrics.false_positive_rate)
                    : 'not reported'
                }
              />
              <Metric
                label="Average lead time"
                value={
                  enough && metrics.lead_time_seconds != null
                    ? `${Math.round(metrics.lead_time_seconds / 60)} min`
                    : 'not reported'
                }
              />
            </dl>
            {run.reliability_bands.length > 0 ? (
              <div className="mt-3">
                <h3 className="text-xs uppercase tracking-wider text-slate-500">
                  Calibration bands
                </h3>
                <ul className="mt-1 space-y-1 text-xs text-slate-400">
                  {run.reliability_bands.map((band, index) => (
                    <li key={index}>
                      risk {String((band as Record<string, unknown>).band ?? '')}:{' '}
                      predicted{' '}
                      {percentLabel(Number((band as Record<string, unknown>).mean_predicted ?? 0))}
                      · observed{' '}
                      {percentLabel(Number((band as Record<string, unknown>).observed_rate ?? 0))}{' '}
                      of {String((band as Record<string, unknown>).count ?? 0)}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {run.notes.length > 0 ? (
              <ul className="mt-3 list-inside list-disc text-xs text-slate-500">
                {run.notes.map((note, index) => (
                  <li key={index}>{note}</li>
                ))}
              </ul>
            ) : null}
          </section>
        );
      })}

      <div>
        <Link
          href={`/reliability?project_id=${encodeURIComponent(project?.id ?? '')}`}
          className="text-xs text-argus-accent hover:underline"
        >
          ← Reliability dashboard
        </Link>
      </div>
    </div>
  );
}
