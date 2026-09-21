import Link from 'next/link';

import {
  api,
  formatDate,
  type Backtest,
  type PaginatedResponse,
  type Project,
} from '@/lib/api';
import {
  backtestCounts,
  HORIZON_LABELS,
  OUTCOME_LABELS,
  percentLabel,
  UNKNOWN_RISK_NOTE,
} from '@/lib/reliability';
import RunBacktestForm from './RunBacktestForm';

export const metadata = {
  title: 'Backtests',
};

export const dynamic = 'force-dynamic';

/**
 * The backtest UI (§55).
 *
 * The form only ever asks for a *configuration*: the engine replays stored
 * history with time-based splits, so nothing a user types here can reach
 * production or change a forecast.
 */
export default async function BacktestsPage({
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

  let backtests: Backtest[] = [];
  let error: string | null = null;
  if (project) {
    try {
      const response = await api.listBacktests(project.id, 20);
      backtests = response.items;
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Backtests</h1>
        <p className="mt-1 text-sm text-slate-400">
          A backtest replays history: at each origin it may only see the past, and
          its verdict is scored against the stored future. No future information
          can leak into a historical prediction. {UNKNOWN_RISK_NOTE}
        </p>
      </div>

      <section className="card">
        <div className="flex flex-wrap gap-2">
          {projects.map((item) => (
            <Link
              key={item.id}
              href={`/reliability/backtests?project_id=${encodeURIComponent(item.id)}`}
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

      {project ? <RunBacktestForm projectId={project.id} /> : null}

      {error ? (
        <section className="card">
          <p className="text-sm text-argus-error">{error}</p>
        </section>
      ) : null}

      {backtests.map((backtest) => {
        const counts = backtestCounts(backtest);
        const metrics = backtest.metrics as Record<string, number | undefined>;
        return (
          <section key={backtest.id} className="card">
            <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
              <h2 className="font-medium text-slate-200">
                Backtest {backtest.id.slice(0, 8)} ·{' '}
                {HORIZON_LABELS[backtest.forecast_horizon]} ·{' '}
                {backtest.prediction_type}
              </h2>
              <span className="text-xs text-slate-500">
                {formatDate(backtest.created_at)} · status {backtest.status} ·{' '}
                {backtest.sample_count} step(s)
              </span>
            </div>
            <div className="flex flex-wrap gap-2 text-xs">
              {Object.entries(counts)
                .filter(([, count]) => count > 0)
                .map(([outcome, count]) => (
                  <span key={outcome} className="badge bg-slate-800 text-slate-300">
                    {OUTCOME_LABELS[outcome as keyof typeof OUTCOME_LABELS] ??
                      outcome}
                    : {count}
                  </span>
                ))}
            </div>
            <p className="mt-2 text-xs text-slate-500">
              precision:{' '}
              {metrics.precision != null ? percentLabel(metrics.precision) : 'not reported'}{' '}
              · recall:{' '}
              {metrics.recall != null ? percentLabel(metrics.recall) : 'not reported'}
            </p>
            {backtest.error ? (
              <p className="mt-2 text-xs text-argus-error">{backtest.error}</p>
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
