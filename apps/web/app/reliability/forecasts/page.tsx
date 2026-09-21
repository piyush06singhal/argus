import Link from 'next/link';

import {
  api,
  formatDate,
  type Forecast,
  type PaginatedResponse,
  type Project,
} from '@/lib/api';
import {
  dataQualityStyle,
  forecastStatusStyle,
  FORECAST_STATUS_LABELS,
  HORIZON_LABELS,
  PREDICTION_TYPE_LABELS,
  riskLevelStyle,
  riskScoreLabel,
  UNKNOWN_RISK_NOTE,
} from '@/lib/reliability';

export const metadata = {
  title: 'Forecasts',
};

export const dynamic = 'force-dynamic';

const HORIZONS = ['ONE_HOUR', 'SIX_HOURS', 'TWENTY_FOUR_HOURS', 'SEVEN_DAYS'] as const;

function Badge({
  href,
  selected,
  children,
}: {
  href: string;
  selected: boolean;
  children: React.ReactNode;
}) {
  return (
    <Link
      href={href}
      className={`badge ${
        selected ? 'bg-argus-accent/20 text-argus-accent' : 'bg-slate-800 text-slate-300'
      }`}
    >
      {children}
    </Link>
  );
}

/** The forecast list (§46 "recent forecasts"), filterable like the heatmap. */
export default async function ForecastsPage({
  searchParams,
}: {
  searchParams: {
    project_id?: string;
    horizon?: string;
    risk_level?: string;
    active_only?: string;
  };
}) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';
  const horizon =
    typeof searchParams.horizon === 'string' ? searchParams.horizon : '';
  const riskLevel =
    typeof searchParams.risk_level === 'string' ? searchParams.risk_level : '';
  const activeOnly = searchParams.active_only === 'true';

  let projects: Project[] = [];
  try {
    const response: PaginatedResponse<Project> = await api.listProjects(1, 50);
    projects = response.items;
  } catch {
    projects = [];
  }
  const project = projects.find((item) => item.id === requested) ?? projects[0];

  let forecasts: Forecast[] = [];
  let error: string | null = null;
  if (project) {
    try {
      const response = await api.listForecasts({
        project_id: project.id,
        forecast_horizon: horizon || undefined,
        risk_level: riskLevel || undefined,
        active_only: activeOnly || undefined,
        limit: 50,
      });
      forecasts = response.items;
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    }
  }

  const base = `/reliability/forecasts?project_id=${encodeURIComponent(project?.id ?? '')}`;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Forecasts</h1>
        <p className="mt-1 text-sm text-slate-400">
          Every forecast names its model version, its data quality and its
          limitations. {UNKNOWN_RISK_NOTE}
        </p>
      </div>

      <section className="card">
        <div className="flex flex-wrap gap-2">
          {projects.map((item) => (
            <Badge
              key={item.id}
              href={`/reliability/forecasts?project_id=${encodeURIComponent(item.id)}`}
              selected={item.id === project?.id}
            >
              {item.name}
            </Badge>
          ))}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="text-xs uppercase tracking-wider text-slate-500">
            Horizon
          </span>
          <Badge href={base} selected={!horizon}>
            all
          </Badge>
          {HORIZONS.map((name) => (
            <Badge
              key={name}
              href={`${base}&horizon=${name}`}
              selected={horizon === name}
            >
              {HORIZON_LABELS[name]}
            </Badge>
          ))}
          <span className="ml-4 text-xs uppercase tracking-wider text-slate-500">
            Level
          </span>
          {['HIGH', 'CRITICAL', 'MEDIUM', 'LOW', 'UNKNOWN'].map((level) => (
            <Badge
              key={level}
              href={`${base}&risk_level=${level}`}
              selected={riskLevel === level}
            >
              {level}
            </Badge>
          ))}
          <Badge
            href={`${base}&active_only=true`}
            selected={activeOnly}
          >
            active only
          </Badge>
        </div>
      </section>

      {error ? (
        <section className="card">
          <p className="text-sm text-argus-error">{error}</p>
        </section>
      ) : null}

      {forecasts.length === 0 && !error ? (
        <section className="card">
          <p className="text-sm text-slate-400">
            No forecasts match this filter. Absence of forecasts means absence of
            evidence — it is not rendered as LOW risk.
          </p>
        </section>
      ) : null}

      <div className="space-y-3">
        {forecasts.map((forecast) => (
          <Link
            key={forecast.id}
            href={`/reliability/forecasts/${forecast.id}?project_id=${encodeURIComponent(
              project?.id || forecast.project_id
            )}`}
            className="card block hover:border-slate-700"
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className={`badge ${riskLevelStyle(forecast.risk_level)}`}>
                {forecast.risk_level}
              </span>
              <span className="text-sm text-slate-200">{forecast.headline}</span>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
              <span className={`badge ${forecastStatusStyle(forecast.status)}`}>
                {FORECAST_STATUS_LABELS[forecast.status]}
              </span>
              <span className={`badge ${dataQualityStyle(forecast.data_quality)}`}>
                data {forecast.data_quality.toLowerCase()}
              </span>
              <span>
                {PREDICTION_TYPE_LABELS[forecast.prediction_type]} ·{' '}
                {HORIZON_LABELS[forecast.forecast_horizon]} · score{' '}
                {riskScoreLabel(forecast.risk_score)} · {forecast.model_version_label}
              </span>
            </div>
            <p className="mt-1 text-xs text-slate-600">
              generated {formatDate(forecast.generated_at)} · valid until{' '}
              {formatDate(forecast.valid_until)}
            </p>
          </Link>
        ))}
      </div>
    </div>
  );
}
