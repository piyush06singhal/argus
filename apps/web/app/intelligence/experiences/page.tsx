import Link from 'next/link';

import {
  api,
  formatDate,
  type ExperienceItem,
  type ExperienceList,
} from '@/lib/api';
import {
  dataQualityLabel,
  dataQualityStyle,
  experienceHeadline,
  isMineable,
  outcomeLabel,
} from '@/lib/intelligence';

export const metadata = {
  title: 'Reliability memory',
};

export const dynamic = 'force-dynamic';

type SearchParams = { project_id?: string; page?: string };

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
 * Reliability memory (§8, §54).
 *
 * The experiences are the whole basis of the learning layer, so this page shows
 * them as they are stored — including the ones excluded from learning. A row
 * marked `POOR` quality is visible with that verdict rather than filtered out:
 * the pipeline's refusal to learn from it is a decision worth being able to
 * inspect.
 */
export default async function ExperiencesPage({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';
  const page = Number.parseInt(String(searchParams.page ?? '1'), 10) || 1;

  let listing: ExperienceList | null = null;
  let error: string | null = null;
  if (projectId) {
    try {
      listing = await api.listExperiences({ project_id: projectId, page, page_size: 25 });
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    }
  }

  const items: ExperienceItem[] = listing?.items ?? [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Reliability memory</h1>
        <p className="mt-1 text-sm text-slate-400">
          Every completed reliability episode, normalised into what failed, what
          was observed, what was done and what the outcome was. This is the
          history the learning pipeline reasons over — nothing here is new
          evidence.
        </p>
      </div>

      {!projectId ? (
        <Card title="Pick a project" subtitle="Experiences are project-scoped">
          <p className="text-sm text-slate-400">
            Open this page from the{' '}
            <Link href="/intelligence" className="text-argus-accent">
              Learning Center
            </Link>{' '}
            so the scope travels with the link.
          </p>
        </Card>
      ) : null}

      {error ? (
        <section className="rounded-md border border-argus-warning/40 bg-argus-warning/10 p-3 text-sm text-argus-warning">
          Experiences could not be loaded: {error}
        </section>
      ) : null}

      {projectId && !error ? (
        <Card
          title={`${listing?.total ?? 0} episode${listing?.total === 1 ? '' : 's'}`}
          subtitle="Oldest facts are reused, never rewritten"
        >
          {items.length === 0 ? (
            <p className="text-sm text-slate-400">
              No episode has been assembled for this project yet. An experience
              appears once an incident resolves and a learning event for it is
              consumed.
            </p>
          ) : (
            <ul className="space-y-3">
              {items.map((item) => (
                <li
                  key={item.id}
                  className="rounded-md border border-slate-800 bg-slate-900/40 p-3"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <Link
                      href={`/intelligence/experiences/${item.id}?project_id=${encodeURIComponent(projectId)}`}
                      className="font-medium text-slate-100 hover:text-argus-accent"
                    >
                      {experienceHeadline(item)}
                    </Link>
                    <span className={`badge ${dataQualityStyle(item.data_quality)}`}>
                      {dataQualityLabel(item.data_quality)}
                    </span>
                    {!isMineable(item) ? (
                      <span className="badge bg-argus-warning/15 text-argus-warning">
                        Excluded from learning
                      </span>
                    ) : null}
                    <span className="badge bg-slate-800 text-slate-400">
                      {outcomeLabel(item.outcome)}
                    </span>
                  </div>
                  <p className="mt-2 text-xs text-slate-500">
                    {formatDate(item.start_time)} → {formatDate(item.end_time)} ·{' '}
                    {item.component_ids.length} component
                    {item.component_ids.length === 1 ? '' : 's'} ·{' '}
                    {item.recovery_seconds === null ||
                    item.recovery_seconds === undefined
                      ? 'recovery time unmeasured'
                      : `${item.recovery_seconds}s to recovery`}
                  </p>
                  <p className="mt-1 text-xs text-slate-500">
                    Failure signature: {item.failure_label} · resolution:{' '}
                    {item.resolution_label ?? 'unrecorded'}
                  </p>
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
                  href={`/intelligence/experiences?project_id=${encodeURIComponent(
                    projectId
                  )}&page=${listing.page - 1}`}
                >
                  ← Previous
                </Link>
              ) : null}
              {listing.page < listing.total_pages ? (
                <Link
                  className="text-argus-accent"
                  href={`/intelligence/experiences?project_id=${encodeURIComponent(
                    projectId
                  )}&page=${listing.page + 1}`}
                >
                  Next →
                </Link>
              ) : null}
            </div>
          ) : null}
        </Card>
      ) : null}
    </div>
  );
}
