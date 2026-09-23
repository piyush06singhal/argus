import Link from 'next/link';

import { api, formatDate, type LearningRunList } from '@/lib/api';
import { LEARNING_BOUNDARY } from '@/lib/intelligence';

import RunNowForm from './RunNowForm';

export const metadata = {
  title: 'Learning runs',
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
 * Learning runs (§59, §79, §98).
 *
 * A run is the unit of auditability: which cutoff it read, which algorithm
 * versions it used, what it consumed, what it produced, and what it refused.
 * Failed runs are listed with their error rather than disappearing, because a
 * pipeline that only reports its successes cannot be trusted with its output.
 */
export default async function LearningRunsPage({
  searchParams,
}: {
  searchParams: { project_id?: string; page?: string };
}) {
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';
  const page = Number.parseInt(String(searchParams.page ?? '1'), 10) || 1;

  let listing: LearningRunList | null = null;
  let error: string | null = null;
  try {
    listing = await api.listLearningRuns({
      page,
      page_size: 20,
      ...(projectId ? { project_id: projectId } : {}),
    });
  } catch (caught) {
    error = caught instanceof Error ? caught.message : String(caught);
  }

  const items = listing?.items ?? [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Learning runs</h1>
        <p className="mt-1 text-sm text-slate-400">
          Every execution of the learning pipeline, with the window it read, the
          algorithms it used, and what it decided. Re-running a completed run is
          safe: it finds nothing new and changes nothing.
        </p>
        <p className="mt-2 text-xs text-slate-500">{LEARNING_BOUNDARY}</p>
      </div>

      {error ? (
        <section className="rounded-md border border-argus-warning/40 bg-argus-warning/10 p-3 text-sm text-argus-warning">
          Runs could not be loaded: {error}
        </section>
      ) : null}

      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Run the pipeline now" subtitle="§63 — an explicit, auditable trigger">
          <RunNowForm projectId={projectId} />
        </Card>

        <Card
          title="What a run does"
          subtitle="the order matters: profiles before mining, relationships after"
        >
          <ol className="space-y-1 text-sm text-slate-400">
            <li>1. Consume unprocessed learning events, once each.</li>
            <li>2. Assemble or refresh an experience per resolved incident.</li>
            <li>3. Recompute component reliability profiles.</li>
            <li>4. Mine candidate patterns from the episode window.</li>
            <li>5. Validate each candidate and record it through the lifecycle.</li>
            <li>6. Derive component relationships from the same window.</li>
            <li>7. Emit recommendations for current open incidents.</li>
            <li>8. Mark knowledge that new data no longer confirms.</li>
          </ol>
        </Card>
      </div>

      <Card
        title={`${listing?.total ?? 0} run${listing?.total === 1 ? '' : 's'}`}
        subtitle={projectId ? 'scoped to this project' : 'all projects'}
      >
        {items.length === 0 ? (
          <p className="text-sm text-slate-400">
            No learning run has executed yet. Until one does, no knowledge exists —
            an empty learning layer is not evidence of a healthy system.
          </p>
        ) : (
          <ul className="space-y-3">
            {items.map((run) => (
              <li
                key={run.id}
                className="rounded-md border border-slate-800 bg-slate-900/40 p-3"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Link
                    href={`/intelligence/learning-runs/${run.id}${
                      projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''
                    }`}
                    className="font-mono text-xs text-argus-accent"
                  >
                    {run.id.slice(0, 8)}
                  </Link>
                  <span
                    className={`badge ${
                      run.status === 'COMPLETED'
                        ? 'bg-argus-success/15 text-argus-success'
                        : run.status === 'FAILED'
                          ? 'bg-argus-error/20 text-argus-error'
                          : 'bg-slate-800 text-slate-300'
                    }`}
                  >
                    {run.status}
                  </span>
                  <span className="badge bg-slate-800 text-slate-400">
                    {run.trigger}
                  </span>
                  <span className="text-xs text-slate-500">
                    {formatDate(run.started_at)}
                  </span>
                </div>
                <p className="mt-2 text-xs text-slate-500">
                  cutoff {formatDate(run.data_cutoff)} · {run.events_processed} event
                  {run.events_processed === 1 ? '' : 's'} consumed ·{' '}
                  {run.experiences_created + run.experiences_updated} experience
                  {run.experiences_created + run.experiences_updated === 1 ? '' : 's'} ·{' '}
                  {run.patterns_discovered} pattern
                  {run.patterns_discovered === 1 ? '' : 's'} found ({' '}
                  {run.patterns_validated} validated, {run.patterns_rejected} rejected)
                  · {run.relationships_created ?? 0} relationship
                  {(run.relationships_created ?? 0) === 1 ? '' : 's'} created
                </p>
                {run.error_summary ? (
                  <p className="mt-1 text-xs text-argus-error">{run.error_summary}</p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
        {listing && listing.total_pages > 1 ? (
          <div className="mt-4 flex items-center gap-3 text-xs text-slate-400">
            <span>
              Page {listing.page} of {listing.total_pages}
            </span>
            {listing.page > 1 ? (
              <Link
                className="text-argus-accent"
                href={`/intelligence/learning-runs?${projectId ? `project_id=${encodeURIComponent(projectId)}&` : ''}page=${listing.page - 1}`}
              >
                ← Previous
              </Link>
            ) : null}
            {listing.page < listing.total_pages ? (
              <Link
                className="text-argus-accent"
                href={`/intelligence/learning-runs?${projectId ? `project_id=${encodeURIComponent(projectId)}&` : ''}page=${listing.page + 1}`}
              >
                Next →
              </Link>
            ) : null}
          </div>
        ) : null}
      </Card>
    </div>
  );
}
