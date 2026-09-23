import Link from 'next/link';

import { api, formatDate, type ComponentLearningProfile } from '@/lib/api';
import {
  confidenceStyle,
  HISTORICAL_RELATIONSHIP_FALLBACK,
  knowledgeSampleLabel,
  knowledgeStatusLabel,
  knowledgeStatusStyle,
  knowledgeTypeLabel,
  mayDrawArrow,
  relationshipHeadline,
  relationshipKindLabel,
  relationshipsForComponent,
  scopeLabel,
} from '@/lib/intelligence';

export const metadata = {
  title: 'Component learning profile',
};

export const dynamic = 'force-dynamic';

type SearchParams = { project_id?: string; window_days?: string };

function Card({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="card">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="font-medium text-slate-200">{title}</h2>
        {subtitle ? <p className="text-xs text-slate-500">{subtitle}</p> : null}
      </div>
      {children}
    </section>
  );
}

/**
 * One component's learning profile (§58, §23).
 *
 * The historical facts are stated as *history*: how many incidents, anomalies,
 * remediations, rollbacks and regressions the component has accumulated in each
 * window, and whether the chronic thresholds flag it. The chronic signal is
 * presented as a reason to investigate — the page says so explicitly, because a
 * reader who believes ARGUS might act on it would be reading a capability the
 * system does not have.
 */
export default async function ComponentLearningPage({
  params,
  searchParams,
}: {
  params: { componentId: string };
  searchParams: SearchParams;
}) {
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';
  const windowDays = searchParams.window_days
    ? Number.parseInt(String(searchParams.window_days), 10)
    : undefined;

  if (!projectId) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-slate-100">
          Component learning profile
        </h1>
        <Card title="Project required" subtitle="Learning is project-scoped">
          <p className="text-sm text-slate-400">
            Open this page from the{' '}
            <Link href="/intelligence" className="text-argus-accent">
              Learning Center
            </Link>{' '}
            so the scope travels with the link.
          </p>
        </Card>
      </div>
    );
  }

  let profile: ComponentLearningProfile | null = null;
  let error: string | null = null;
  try {
    profile = await api.componentLearningProfile(params.componentId, projectId);
  } catch (caught) {
    error = caught instanceof Error ? caught.message : String(caught);
  }

  if (error || !profile) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-slate-100">
          Component learning profile
        </h1>
        <Card title="Could not load" subtitle="The component may belong to another project">
          <p className="text-sm text-argus-warning">{error ?? 'Not found.'}</p>
        </Card>
      </div>
    );
  }

  const relationships = profile.relationships ?? [];
  const grouped = relationshipsForComponent(relationships, profile.component.id);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">
          {profile.component.name}
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          What ARGUS has learned about this component from stored history — the
          patterns that mention it, and what has been observed happening around it.
        </p>
      </div>

      <Card
        title="Historical reliability facts"
        subtitle="§21, §58 — computed from stored rows, one row per window"
      >
        {profile.profiles.length === 0 ? (
          <p className="text-sm text-slate-400">
            No profile has been computed for this component yet. Profiles are
            recalculated by each learning run.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="py-2">Window</th>
                  <th className="py-2">Incidents</th>
                  <th className="py-2">Anomalies</th>
                  <th className="py-2">Remediations</th>
                  <th className="py-2">Rollbacks</th>
                  <th className="py-2">Regressions</th>
                  <th className="py-2">Mean recovery</th>
                  <th className="py-2">Forecast outcomes</th>
                  <th className="py-2">Chronic</th>
                </tr>
              </thead>
              <tbody>
                {profile.profiles
                  .filter((row) =>
                    windowDays === undefined ? true : row.window_days === windowDays
                  )
                  .map((row) => (
                    <tr key={row.id} className="border-t border-slate-800">
                      <td className="py-2 text-slate-300">{row.window_days}d</td>
                      <td className="py-2 text-slate-300">{row.incident_count}</td>
                      <td className="py-2 text-slate-300">{row.anomaly_count}</td>
                      <td className="py-2 text-slate-300">{row.remediation_count}</td>
                      <td className="py-2 text-slate-300">{row.rollback_count}</td>
                      <td className="py-2 text-slate-300">{row.regression_count}</td>
                      <td className="py-2 text-slate-300">
                        {row.mean_recovery_seconds === null ||
                        row.mean_recovery_seconds === undefined
                          ? 'unmeasured'
                          : `${Math.round(row.mean_recovery_seconds)}s`}
                      </td>
                      <td className="py-2 text-slate-300">
                        {row.forecast_true_positive_count}/{row.forecast_outcome_count}
                      </td>
                      <td className="py-2">
                        {row.chronic_signal ? (
                          <span className="badge bg-argus-warning/20 text-argus-warning">
                            Chronic
                          </span>
                        ) : (
                          <span className="text-slate-500">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        )}

        {profile.profiles.some((row) => row.chronic_signal) ? (
          <div className="mt-3 rounded-md border border-argus-warning/40 bg-argus-warning/10 p-3 text-sm text-argus-warning">
            The chronic thresholds are met.{' '}
            {profile.profiles
              .filter((row) => row.chronic_signal && row.chronic_reasons.length > 0)
              .flatMap((row) => row.chronic_reasons)
              .slice(0, 3)
              .join('; ')}
            . This is a signal to investigate. ARGUS does not disable, restart or
            reconfigure a component because of it.
          </div>
        ) : null}
      </Card>

      <Card
        title="Patterns about this component"
        subtitle="§53 — with the sample behind each one"
      >
        {profile.knowledge.length === 0 ? (
          <p className="text-sm text-slate-400">
            Nothing has been learned about this component yet.
          </p>
        ) : (
          <ul className="space-y-2">
            {profile.knowledge.map((item) => (
              <li key={item.id} className="rounded-md border border-slate-800 bg-slate-900/40 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Link
                    href={`/intelligence/patterns/${item.id}?project_id=${encodeURIComponent(projectId)}`}
                    className="font-medium text-slate-100 hover:text-argus-accent"
                  >
                    {item.title}
                  </Link>
                  <span className={`badge ${knowledgeStatusStyle(item.status)}`}>
                    {knowledgeStatusLabel(item.status)}
                  </span>
                  <span className="badge bg-slate-800 text-slate-400">
                    {knowledgeTypeLabel(item.knowledge_type)}
                  </span>
                  <span className={`badge ${confidenceStyle(item.confidence)}`}>
                    {item.confidence}
                  </span>
                </div>
                <p className="mt-1 text-xs text-slate-500">
                  {knowledgeSampleLabel(item)} · {scopeLabel(item.scope)} · last
                  confirmed {formatDate(item.last_confirmed_at)}
                </p>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card
        title="Learned relationships"
        subtitle={HISTORICAL_RELATIONSHIP_FALLBACK}
      >
        {relationships.length === 0 ? (
          <p className="text-sm text-slate-400">
            No relationship involving this component has been learned yet. These
            come from multi-component episodes, so a component that fails alone
            produces none.
          </p>
        ) : (
          <div className="space-y-4">
            {grouped.outbound.length > 0 ? (
              <div>
                <h3 className="text-xs uppercase tracking-wider text-slate-500">
                  Where failures at this component coincided with trouble downstream
                </h3>
                <ul className="mt-1 space-y-2 text-sm">
                  {grouped.outbound.map((item) => (
                    <li key={item.id}>
                      · {relationshipHeadline(item)}{' '}
                      <span className="text-xs text-slate-500">
                        ({relationshipKindLabel(item.kind)})
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            {grouped.inbound.length > 0 ? (
              <div>
                <h3 className="text-xs uppercase tracking-wider text-slate-500">
                  Upstream relationships pointing here
                </h3>
                <ul className="mt-1 space-y-2 text-sm">
                  {grouped.inbound.map((item) => (
                    <li key={item.id}>
                      · {relationshipHeadline(item)}{' '}
                      <span className="text-xs text-slate-500">
                        ({relationshipKindLabel(item.kind)})
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            {grouped.undirected.length > 0 ? (
              <div>
                <h3 className="text-xs uppercase tracking-wider text-slate-500">
                  Co-failure — no direction claimed
                </h3>
                <ul className="mt-1 space-y-2 text-sm">
                  {grouped.undirected.map((item) => (
                    <li key={item.id}>
                      · {relationshipHeadline(item)}
                      {mayDrawArrow(item) ? null : (
                        <span className="ml-2 text-xs text-slate-500">
                          (undirected: history does not say which end came first)
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        )}
      </Card>
    </div>
  );
}
