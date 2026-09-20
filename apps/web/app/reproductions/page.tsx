import Link from 'next/link';

import {
  api,
  formatDate,
  type PaginatedResponse,
  type Project,
  type ReproductionExperiment,
  type ReproductionMetrics,
} from '@/lib/api';
import {
  EXPERIMENT_HAPPY_PATH,
  experimentStatusStyle,
  formatSeconds,
  outcomeStyle,
  REPRODUCTION_DISCLAIMER,
  resultStyle,
} from '@/lib/reproduction';

export const metadata = {
  title: 'Failure Reproduction',
};

export const dynamic = 'force-dynamic';

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
 * Index of reproduction experiments for a project (§44, §54).
 *
 * Project scope is required by the backend on the list and metrics endpoints —
 * it is what proves ownership — so the page resolves one: the `project_id`
 * search param when present, otherwise the first project, with an explicit
 * picker.
 */
export default async function ReproductionsPage({
  searchParams,
}: {
  searchParams: { project_id?: string; status?: string; page?: string };
}) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';
  const statusFilter =
    typeof searchParams.status === 'string' ? searchParams.status : '';
  const rawPage = Number(searchParams.page);
  const page = Number.isInteger(rawPage) && rawPage > 0 ? rawPage : 1;

  let projects: Project[] = [];
  try {
    const response: PaginatedResponse<Project> = await api.listProjects(1, 50);
    projects = response.items;
  } catch {
    projects = [];
  }

  const project = projects.find((item) => item.id === requested) ?? projects[0];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">
          Failure Reproduction
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          ARGUS does not only analyse failures: it constructs an isolated
          environment representing the relevant system state, replays sanitized
          inputs, injects controlled faults, captures real telemetry and compares
          what happened against the original incident.
        </p>
        <p className="mt-2 text-xs text-slate-500">{REPRODUCTION_DISCLAIMER}</p>
      </div>

      {projects.length === 0 ? (
        <Card title="No projects" subtitle="Reproduction is project-scoped">
          <p className="text-sm text-slate-400">
            No project could be loaded, so there is nothing to scope experiments
            to. Project scope is how the API proves ownership of an experiment.
          </p>
        </Card>
      ) : (
        <ProjectPicker projects={projects} selectedId={project?.id ?? ''} />
      )}

      {project ? (
        <ExperimentsSection
          projectId={project.id}
          statusFilter={statusFilter}
          page={page}
        />
      ) : null}
    </div>
  );
}

function ProjectPicker({
  projects,
  selectedId,
}: {
  projects: Project[];
  selectedId: string;
}) {
  return (
    <Card title="Project scope" subtitle="Experiments are always project-scoped">
      <div className="flex flex-wrap gap-2">
        {projects.map((item) => (
          <Link
            key={item.id}
            href={`/reproductions?project_id=${encodeURIComponent(item.id)}`}
            className={`badge ${
              item.id === selectedId
                ? 'bg-argus-accent/20 text-argus-accent'
                : 'bg-slate-800 text-slate-300'
            }`}
          >
            {item.name}
          </Link>
        ))}
      </div>
    </Card>
  );
}

async function ExperimentsSection({
  projectId,
  statusFilter,
  page,
}: {
  projectId: string;
  statusFilter: string;
  page: number;
}) {
  let experiments: PaginatedResponse<ReproductionExperiment> = {
    items: [],
    total: 0,
    page: 1,
    page_size: 20,
    total_pages: 1,
  };
  let metrics: ReproductionMetrics | null = null;
  let error: string | null = null;

  try {
    experiments = await api.listReproductions({
      projectId,
      status: statusFilter || undefined,
      page,
      pageSize: 20,
    });
  } catch (cause: unknown) {
    error = cause instanceof Error ? cause.message : 'unknown error';
  }

  try {
    metrics = await api.reproductionMetrics(projectId);
  } catch {
    metrics = null;
  }

  return (
    <>
      {metrics ? (
        <Card
          title="Reproduction engine health"
          subtitle={`§54 — ARGUS monitoring its own experiments (backend ${metrics.backend}, network ${metrics.network_policy})`}
        >
          <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <Metric
              label="Experiments"
              value={Object.entries(metrics.experiments)
                .map(([key, value]) => `${key}: ${value}`)
                .join(' · ') || 'none'}
            />
            <Metric
              label="Results"
              value={
                Object.entries(metrics.results)
                  .map(([key, value]) => `${key}: ${value}`)
                  .join(' · ') || 'none'
              }
            />
            <Metric
              label="Live sandboxes"
              value={String(metrics.live_sandboxes)}
            />
            <Metric
              label="Orphaned sandboxes"
              value={String(metrics.orphaned_sandboxes)}
            />
            <Metric
              label="Sandboxes destroyed"
              value={`${metrics.sandboxes_destroyed} / ${metrics.sandboxes_total}`}
            />
            <Metric
              label="Cleanup failures"
              value={String(metrics.cleanup_failures)}
            />
            <Metric
              label="Avg experiment"
              value={
                metrics.durations_ms.experiment_avg != null
                  ? formatSeconds(metrics.durations_ms.experiment_avg / 1000)
                  : '—'
              }
            />
            <Metric
              label="Avg run"
              value={
                metrics.durations_ms.run_avg != null
                  ? formatSeconds(metrics.durations_ms.run_avg / 1000)
                  : '—'
              }
            />
          </dl>
          {metrics.orphaned_sandboxes > 0 || metrics.cleanup_failures > 0 ? (
            <p className="mt-3 text-xs text-argus-warning">
              Cleanup needs attention: a sandbox that failed to be destroyed is
              reported here rather than silently leaked.
            </p>
          ) : null}
        </Card>
      ) : null}

      <Card
        title="Experiments"
        subtitle={`${experiments.total} in this project · ${EXPERIMENT_HAPPY_PATH.join(' → ')}`}
      >
        <div className="mb-3 flex flex-wrap gap-2">
          {['', 'PLANNED', 'RUNNING', 'COMPLETED', 'FAILED'].map((value) => (
            <Link
              key={value || 'ALL'}
              href={`/reproductions?project_id=${encodeURIComponent(projectId)}${
                value ? `&status=${value}` : ''
              }`}
              className={`badge ${
                statusFilter === value
                  ? 'bg-argus-accent/20 text-argus-accent'
                  : 'bg-slate-800 text-slate-300'
              }`}
            >
              {value || 'All'}
            </Link>
          ))}
        </div>

        {error ? (
          <p className="text-sm text-argus-error">{error}</p>
        ) : experiments.items.length === 0 ? (
          <p className="text-sm text-slate-400">
            No experiment matches this filter. Experiments are planned from an
            incident&apos;s root-cause analysis, not from here.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-3 py-2">Version</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2">Result</th>
                  <th className="px-3 py-2">Reps</th>
                  <th className="px-3 py-2">Created</th>
                  <th className="px-3 py-2">Completed</th>
                  <th className="px-3 py-2">Summary</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {experiments.items.map((item) => (
                  <tr key={item.id} className="align-top">
                    <td className="px-3 py-2">
                      <Link href={`/reproductions/${item.id}`}>
                        v{item.experiment_version}
                      </Link>
                    </td>
                    <td className="px-3 py-2">
                      <span className={`badge ${experimentStatusStyle(item.status)}`}>
                        {item.status}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      <span className={`badge ${resultStyle(item.result)}`}>
                        {item.result}
                      </span>
                      <span
                        className={`badge ml-2 ${outcomeStyle('INCONCLUSIVE')}`}
                      >
                        {item.confidence}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-slate-400">
                      {item.completed_runs}/{item.repetitions}
                    </td>
                    <td className="px-3 py-2 text-slate-400">
                      {formatDate(item.created_at)}
                    </td>
                    <td className="px-3 py-2 text-slate-400">
                      {item.completed_at ? formatDate(item.completed_at) : '—'}
                    </td>
                    <td className="px-3 py-2 text-xs text-slate-400">
                      {item.summary ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {experiments.total_pages > 1 ? (
          <div className="mt-3 flex flex-wrap gap-2 text-xs">
            {Array.from({ length: experiments.total_pages }, (_, index) => index + 1)
              .slice(0, 12)
              .map((value) => (
                <Link
                  key={value}
                  href={`/reproductions?project_id=${encodeURIComponent(projectId)}${
                    statusFilter ? `&status=${statusFilter}` : ''
                  }&page=${value}`}
                  className={`badge ${
                    value === experiments.page
                      ? 'bg-argus-accent/20 text-argus-accent'
                      : 'bg-slate-800 text-slate-300'
                  }`}
                >
                  {value}
                </Link>
              ))}
          </div>
        ) : null}
      </Card>
    </>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className="text-slate-200">{value}</dd>
    </div>
  );
}
