import Link from 'next/link';

import {
  api,
  formatDate,
  type Incident,
  type PaginatedResponse,
  type Project,
  type Repository,
} from '@/lib/api';
import {
  degradationRate,
  formatRate,
  verificationRate,
} from '@/lib/debugger';
import IndexRepositoryButton from './IndexRepositoryButton';
import RegisterRepositoryForm from './RegisterRepositoryForm';

export const metadata = {
  title: 'AI Debugger',
};

export const dynamic = 'force-dynamic';

const MAX_RECENT_INCIDENTS = 12;

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

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/60 p-3">
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="mt-1 font-mono text-sm text-slate-200">{value}</dd>
    </div>
  );
}

/**
 * The code-level investigation surface (§1, §63–§65).
 *
 * The page leads with the honesty metrics — the share of claimed locations
 * that survived snapshot validation, and the share of analyses that ran
 * degraded — because those decide whether the rest of the page can be
 * trusted. Repositories and the incidents-with-sessions list follow.
 */
export default async function DebuggerPage({
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
  const projectId = project?.id;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">AI Debugger</h1>
        <p className="mt-1 text-sm text-slate-400">
          Evidence-grounded code investigation: trace spans are mapped to the
          indexed source, analysis reasons only over what is stored, and every
          code claim is validated against the pinned snapshot before it is
          shown.
        </p>
      </div>

      {projects.length === 0 ? (
        <Card title="No projects" subtitle="The debugger is project-scoped">
          <p className="text-sm text-slate-400">
            No project could be loaded. Create a project first — repositories,
            snapshots and debug sessions are all scoped to one.
          </p>
        </Card>
      ) : (
        <Card title="Project scope" subtitle="All data on this page belongs to one project">
          <div className="flex flex-wrap gap-2">
            {projects.map((item) => (
              <Link
                key={item.id}
                href={`/debugger?project_id=${encodeURIComponent(item.id)}`}
                className={`badge ${
                  item.id === projectId
                    ? 'bg-argus-accent/20 text-argus-accent'
                    : 'bg-slate-800 text-slate-300'
                }`}
              >
                {item.name}
              </Link>
            ))}
          </div>
        </Card>
      )}

      {projectId ? (
        <>
          <MetricsSection projectId={projectId} />
          <RepositoriesSection projectId={projectId} />
          <IncidentsSection projectId={projectId} />
        </>
      ) : null}
    </div>
  );
}

async function MetricsSection({ projectId }: { projectId: string }) {
  let metrics = null;
  try {
    metrics = await api.debuggerMetrics(projectId);
  } catch {
    metrics = null;
  }

  if (!metrics) {
    return (
      <Card
        title="Debugger health"
        subtitle="§63–§65 — coverage and honesty tallies"
      >
        <p className="text-sm text-slate-400">
          Metrics are unavailable right now.
        </p>
      </Card>
    );
  }

  const indexSummary =
    Object.entries(metrics.index_status)
      .map(([key, value]) => `${key}: ${value}`)
      .join(' · ') || 'no repositories indexed';

  return (
    <Card
      title="Debugger health"
      subtitle={`§63–§65 — engine ${metrics.engine_version}`}
    >
      <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <Metric
          label="Location verification"
          value={formatRate(verificationRate(metrics))}
        />
        <Metric
          label="Degraded analyses"
          value={formatRate(degradationRate(metrics))}
        />
        <Metric
          label="Hypotheses"
          value={
            Object.entries(metrics.by_validation_status)
              .map(([key, value]) => `${key}: ${value}`)
              .join(' · ') || 'none'
          }
        />
        <Metric
          label="Tool calls"
          value={`${metrics.tool_calls} run · ${metrics.tool_calls_refused} refused`}
        />
        <Metric label="Sessions" value={`${metrics.sessions} · ${metrics.sessions_completed} completed`} />
        <Metric label="Code claims" value={`${metrics.locations_valid} valid / ${metrics.locations_claimed} claimed`} />
        <Metric label="Invalid citations" value={String(metrics.invalid_references)} />
        <Metric label="Index" value={indexSummary} />
      </dl>
      {metrics.limitations.length > 0 ? (
        <ul className="mt-3 space-y-1 text-xs text-slate-500">
          {metrics.limitations.map((note) => (
            <li key={note}>· {note}</li>
          ))}
        </ul>
      ) : null}
    </Card>
  );
}

async function RepositoriesSection({ projectId }: { projectId: string }) {
  let repositories: Repository[] = [];
  let error: string | null = null;
  try {
    const response = await api.listRepositories(projectId);
    repositories = response.items;
  } catch (cause: unknown) {
    error = cause instanceof Error ? cause.message : 'unknown error';
  }

  return (
    <Card
      title="Repositories"
      subtitle="§6–§8 — registered sources of code intelligence"
    >
      {repositories.length > 0 ? (
        <ul className="space-y-3">
          {repositories.map((repo) => (
            <li
              key={repo.id}
              className="rounded-md border border-slate-800 bg-slate-900/60 p-4"
            >
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <p className="font-mono text-sm text-slate-200">
                  {repo.repository_url}
                </p>
                <span className="badge bg-slate-800 text-slate-300">
                  {repo.provider}
                </span>
              </div>
              <dl className="mt-2 grid grid-cols-2 gap-2 text-xs text-slate-400 sm:grid-cols-4">
                <div>
                  <dt className="text-slate-500">Index status</dt>
                  <dd>{repo.index_status}</dd>
                </div>
                <div>
                  <dt className="text-slate-500">Last commit</dt>
                  <dd className="font-mono">
                    {repo.latest_commit_sha?.slice(0, 10) ?? '—'}
                  </dd>
                </div>
                <div>
                  <dt className="text-slate-500">Snapshots</dt>
                  <dd>{repo.snapshot_count}</dd>
                </div>
                <div>
                  <dt className="text-slate-500">Last indexed</dt>
                  <dd>{formatDate(repo.last_indexed_at)}</dd>
                </div>
              </dl>
              {repo.capabilities.length > 0 ? (
                <p className="mt-2 text-xs text-slate-500">
                  Capabilities: {repo.capabilities.join(' · ')}
                </p>
              ) : null}
              <div className="mt-3">
                <IndexRepositoryButton projectId={projectId} repositoryId={repo.id} />
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-slate-400">
          {error
            ? `Repositories could not be loaded: ${error}`
            : 'No repository registered for this project yet.'}
        </p>
      )}
      <div className="mt-4 border-t border-slate-800 pt-3">
        <RegisterRepositoryForm projectId={projectId} />
      </div>
    </Card>
  );
}

async function IncidentsSection({ projectId }: { projectId: string }) {
  let incidents: Incident[] = [];
  let error: string | null = null;
  try {
    const response = await api.listIncidents({ projectId, pageSize: 50 });
    incidents = response.items.slice(0, MAX_RECENT_INCIDENTS);
  } catch (cause: unknown) {
    error = cause instanceof Error ? cause.message : 'unknown error';
  }

  return (
    <Card
      title="Incidents"
      subtitle="Open a code-level debug session from an incident"
    >
      {incidents.length === 0 ? (
        <p className="text-sm text-slate-400">
          {error
            ? `Incidents could not be loaded: ${error}`
            : 'No incidents recorded for this project.'}
        </p>
      ) : (
        <ul className="divide-y divide-slate-800">
          {incidents.map((incident) => (
            <li
              key={incident.id}
              className="flex flex-wrap items-center justify-between gap-3 py-3"
            >
              <div className="min-w-0">
                <p className="truncate text-sm text-slate-200">
                  {incident.title}
                </p>
                <p className="text-xs text-slate-500">
                  {incident.severity} · {incident.status} · detected{' '}
                  {formatDate(incident.detected_at)}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Link
                  href={`/incidents/${incident.id}`}
                  className="badge bg-slate-800 text-slate-300 hover:bg-slate-700"
                >
                  Incident
                </Link>
                <Link
                  href={`/debugger/incident/${incident.id}?project_id=${encodeURIComponent(projectId)}`}
                  className="badge bg-argus-accent/20 text-argus-accent hover:bg-argus-accent/30"
                >
                  Debug →
                </Link>
              </div>
            </li>
          ))}
        </ul>
      )}
      <p className="mt-3 text-xs text-slate-500">
        Sessions are per-incident: open an incident to see its debug sessions,
        or start one directly from the incident page.
      </p>
    </Card>
  );
}
