import Link from 'next/link';

import {
  api,
  formatDate,
  type ComponentProfile,
  type Project,
} from '@/lib/api';
import {
  coverageLabel,
  dataQualityLabel,
  dataQualityStyle,
  HORIZON_LABELS,
  percentLabel,
  riskLevelStyle,
  riskPhrase,
  riskScoreLabel,
  UNKNOWN_RISK_NOTE,
} from '@/lib/reliability';

export const metadata = {
  title: 'Component reliability',
};

export const dynamic = 'force-dynamic';

/**
 * The component reliability profile (§52).
 *
 * Read-only by contract: the page renders stored forecasts, incidents, trends
 * and the composed score, and opening it changes nothing. The score is shown
 * with its missing dimensions, because a dimension with no data is not health.
 */
export default async function ComponentProfilePage({
  params,
  searchParams,
}: {
  params: Promise<{ componentId: string }>;
  searchParams: { project_id?: string };
}) {
  const { componentId } = await params;
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  let profile: ComponentProfile | null = null;
  let error: string | null = null;
  if (projectId) {
    try {
      profile = await api.getComponentProfile(componentId, projectId);
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    }
  } else {
    error = 'A project scope is required: open this page from a project.';
  }

  if (error !== null || !profile) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-slate-100">
          Component reliability
        </h1>
        <section className="card">
          <p className="text-sm text-argus-error">{error ?? 'Profile not found.'}</p>
        </section>
      </div>
    );
  }

  const score = profile.reliability_score as {
    score?: number | null;
    method?: string;
    dimensions?: Array<{ name: string; value: number | null; weight: number; missing: boolean }>;
    limitations?: string;
  };
  const signals = profile.signals as Record<string, unknown>;
  const currentRisk = profile.current_risk as Record<
    string,
    {
      risk_level?: string;
      risk_score?: number | null;
      headline?: string;
      data_quality?: string;
      generated_at?: string;
    }
  >;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">
          {profile.component_name ?? profile.component_id}
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          Reliability profile — {profile.component_type ?? 'component'}, generated{' '}
          {formatDate(profile.generated_at)}. This view is read-only: opening it
          changes nothing ARGUS believes.
        </p>
        <p className="mt-2 text-xs text-slate-500">{UNKNOWN_RISK_NOTE}</p>
      </div>

      <section className="card">
        <h2 className="font-medium text-slate-200">Current forecasts</h2>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          {Object.entries(currentRisk).length === 0 ? (
            <p className="text-sm text-slate-400">
              No current forecasts for this component.
            </p>
          ) : (
            Object.entries(currentRisk).map(([key, value]) => (
              <div
                key={key}
                className="rounded border border-slate-800 bg-slate-900/50 p-3"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    className={`badge ${riskLevelStyle((value.risk_level ?? 'UNKNOWN') as never)}`}
                  >
                    {value.risk_level ?? 'UNKNOWN'}
                  </span>
                  <span className="text-xs uppercase tracking-wider text-slate-500">
                    {key.replace(':', ' · ')}
                  </span>
                </div>
                <p className="mt-1 text-sm text-slate-300">{value.headline}</p>
                <p className="mt-1 text-xs text-slate-500">
                  score {riskScoreLabel(value.risk_score ?? null)} · data{' '}
                  {value.data_quality?.toLowerCase() ?? 'unknown'}
                </p>
              </div>
            ))
          )}
        </div>
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        <section className="card">
          <h2 className="font-medium text-slate-200">Reliability score</h2>
          <p className="mt-1 text-xs text-slate-500">{score.method}</p>
          <dl className="mt-3 grid grid-cols-2 gap-3">
            <div>
              <dt className="text-xs uppercase tracking-wider text-slate-500">
                Health (0–1)
              </dt>
              <dd className="text-slate-200">
                {score.score != null ? score.score.toFixed(2) : 'unknown'}
              </dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wider text-slate-500">
                Data quality
              </dt>
              <dd>
                <span className={`badge ${dataQualityStyle(profile.data_quality)}`}>
                  {dataQualityLabel(profile.data_quality)}
                </span>
              </dd>
            </div>
          </dl>
          <ul className="mt-3 space-y-1 text-xs text-slate-400">
            {(score.dimensions ?? []).map((dimension) => (
              <li key={dimension.name} className="flex items-center justify-between">
                <span>{dimension.name.replace(/_/g, ' ')}</span>
                <span className={dimension.missing ? 'text-slate-600' : 'text-slate-200'}>
                  {dimension.missing || dimension.value == null
                    ? 'no data'
                    : `${(dimension.value * 100).toFixed(0)} (weight ${dimension.weight})`}
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-slate-500">{score.limitations}</p>
          <p className="mt-1 text-xs text-slate-500">
            {coverageLabel(profile.data_coverage)}
          </p>
        </section>

        <section className="card">
          <h2 className="font-medium text-slate-200">Recent incidents</h2>
          {profile.recent_incidents.length === 0 ? (
            <p className="mt-2 text-sm text-slate-400">No incidents on record.</p>
          ) : (
            <ul className="mt-3 space-y-2 text-sm">
              {profile.recent_incidents.map((incident, index) => (
                <li
                  key={index}
                  className="rounded border border-slate-800 bg-slate-900/50 p-3"
                >
                  <span className="text-slate-200">{String(incident.title ?? '')}</span>
                  <span className="ml-2 text-xs text-slate-500">
                    {String(incident.severity ?? '')} ·{' '}
                    {String(incident.status ?? '')} ·{' '}
                    {incident.detected_at
                      ? formatDate(String(incident.detected_at))
                      : ''}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      <section className="card">
        <h2 className="font-medium text-slate-200">Signal summary</h2>
        <dl className="mt-3 grid grid-cols-2 gap-4 md:grid-cols-4">
          {Object.entries(signals).map(([name, value]) => (
            <div key={name}>
              <dt className="text-xs uppercase tracking-wider text-slate-500">
                {name.replace(/_/g, ' ')}
              </dt>
              <dd className="text-slate-200">
                {value == null ? 'no data' : String(value)}
              </dd>
            </div>
          ))}
        </dl>
      </section>

      {profile.limitations.length > 0 ? (
        <section className="card">
          <h2 className="font-medium text-slate-200">Limitations</h2>
          <ul className="mt-2 list-inside list-disc text-xs text-slate-500">
            {profile.limitations.map((note, index) => (
              <li key={index}>{note}</li>
            ))}
          </ul>
        </section>
      ) : null}

      <div>
        <Link
          href={`/reliability?project_id=${encodeURIComponent(projectId)}`}
          className="text-xs text-argus-accent hover:underline"
        >
          ← Reliability dashboard
        </Link>
      </div>
    </div>
  );
}
