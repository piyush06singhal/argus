import Link from 'next/link';

import { api, formatDate, type KnowledgeItem, type KnowledgeList } from '@/lib/api';
import {
  confidenceStyle,
  filterPatterns,
  isBelowValidationFloor,
  isRetired,
  KNOWLEDGE_TYPE_LABELS,
  knowledgeSampleLabel,
  knowledgeStatusLabel,
  knowledgeStatusStyle,
  knowledgeTypeLabel,
  LEARNING_BOUNDARY,
  OBSERVATIONAL_NOTE,
  scopeLabel,
  sortKnowledge,
  type KnowledgeTypeValue,
} from '@/lib/intelligence';

/**
 * Mirrors the deployment default for `INTELLIGENCE_MIN_SAMPLES_VALIDATION`.
 * Used only for the "small sample" badge: it labels a pattern as too thin to
 * promote, and never promotes or suppresses anything itself.
 */
const MIN_PROMOTABLE_SAMPLES = 5;

export const metadata = {
  title: 'Pattern Explorer',
};

export const dynamic = 'force-dynamic';

type SearchParams = {
  project_id?: string;
  knowledge_type?: string;
  search?: string;
  include_retired?: string;
  page?: string;
};

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
 * The Pattern Explorer (§56, §53).
 *
 * The rule this page is built around: **a pattern is never shown without the
 * facts that qualify it.** Every row carries its sample count, its scope, its
 * confidence bucket, its coverage window and its observed-pattern label, because
 * a list of titles is exactly how "restart works" ends up being read as a rule.
 *
 * Retired patterns are excluded by default and reachable explicitly: a
 * deprecated pattern displayed among live ones is how stale advice survives its
 * own expiry (§25).
 */
export default async function PatternsPage({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';
  const knowledgeType =
    typeof searchParams.knowledge_type === 'string' ? searchParams.knowledge_type : '';
  const search = typeof searchParams.search === 'string' ? searchParams.search : '';
  const includeRetired = searchParams.include_retired === 'true';
  const page = Number.parseInt(String(searchParams.page ?? '1'), 10) || 1;

  let listing: KnowledgeList | null = null;
  let error: string | null = null;
  if (projectId) {
    try {
      listing = await api.listPatterns({
        project_id: projectId,
        page,
        page_size: 20,
        ...(knowledgeType ? { knowledge_type: knowledgeType } : {}),
        ...(search ? { search } : {}),
      });
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    }
  }

  const items: KnowledgeItem[] = listing?.items ?? [];
  const visible = includeRetired
    ? (filterPatterns(items, { includeRetired: true }) as KnowledgeItem[])
    : items.filter((item) => !isRetired(item.status));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Pattern Explorer</h1>
        <p className="mt-1 text-sm text-slate-400">
          Everything ARGUS has concluded from this project&#39;s history, including
          the patterns nobody has validated yet.
        </p>
        <p className="mt-2 text-xs text-slate-500">{OBSERVATIONAL_NOTE}</p>
        <p className="mt-1 text-xs text-slate-500">{LEARNING_BOUNDARY}</p>
      </div>

      <Card title="Filters" subtitle="Filtering is server-side and scoped to the project">
        <form className="flex flex-wrap items-end gap-3" method="get">
          <input type="hidden" name="project_id" value={projectId} />
          <label className="flex flex-col gap-1 text-xs text-slate-400">
            Type
            <select
              name="knowledge_type"
              defaultValue={knowledgeType}
              className="input"
            >
              <option value="">All types</option>
              {(Object.keys(KNOWLEDGE_TYPE_LABELS) as KnowledgeTypeValue[]).map(
                (value) => (
                  <option key={value} value={value}>
                    {knowledgeTypeLabel(value)}
                  </option>
                )
              )}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-slate-400">
            Search
            <input
              type="search"
              name="search"
              defaultValue={search}
              placeholder="title or signature"
              className="input"
            />
          </label>
          <label className="flex items-center gap-2 text-xs text-slate-400">
            <input
              type="checkbox"
              name="include_retired"
              value="true"
              defaultChecked={includeRetired}
            />
            Include retired patterns
          </label>
          <button type="submit" className="btn">
            Apply
          </button>
        </form>
      </Card>

      {!projectId ? (
        <Card title="Pick a project" subtitle="Patterns are scoped to one project">
          <p className="text-sm text-slate-400">
            Add <code className="font-mono text-xs">?project_id=…</code> — or open
            this page from the{' '}
            <Link href="/intelligence" className="text-argus-accent">
              Learning Center
            </Link>
            , which carries the scope through.
          </p>
        </Card>
      ) : null}

      {error ? (
        <section className="rounded-md border border-argus-warning/40 bg-argus-warning/10 p-3 text-sm text-argus-warning">
          Patterns could not be loaded: {error}
        </section>
      ) : null}

      {projectId && !error ? (
        <Card
          title={`${listing?.total ?? 0} pattern${listing?.total === 1 ? '' : 's'}`}
          subtitle={
            includeRetired
              ? 'including retired patterns'
              : 'retired patterns are hidden; ask for them explicitly'
          }
        >
          {visible.length === 0 ? (
            <p className="text-sm text-slate-400">
              No pattern matches this filter. With no history there is nothing to
              learn from — that is a finding about the data, not a clean bill of
              health.
            </p>
          ) : (
            <ul className="space-y-3">
              {sortKnowledge(visible).map((item) => (
                <li
                  key={item.id}
                  className="rounded-md border border-slate-800 bg-slate-900/40 p-3"
                >
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
                    {isBelowValidationFloor(item.sample_count, MIN_PROMOTABLE_SAMPLES) ? (
                      <span className="badge bg-argus-warning/15 text-argus-warning">
                        Small sample
                      </span>
                    ) : null}
                  </div>
                  <p className="mt-2 text-sm text-slate-400">{item.description}</p>
                  <p className="mt-2 text-xs text-slate-500">
                    {knowledgeSampleLabel(item)} ·{' '}
                    {scopeLabel(item.scope)} · {item.algorithm}{' '}
                    {item.algorithm_version} · observed {formatDate(item.coverage_start)}{' '}
                    → {formatDate(item.coverage_end)}
                  </p>
                  {item.limitations.length > 0 ? (
                    <ul className="mt-2 space-y-1 text-xs text-slate-500">
                      {item.limitations.slice(0, 2).map((limitation) => (
                        <li key={limitation}>· {limitation}</li>
                      ))}
                    </ul>
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
                  href={buildHref(searchParams, listing.page - 1)}
                >
                  ← Previous
                </Link>
              ) : null}
              {listing.page < listing.total_pages ? (
                <Link
                  className="text-argus-accent"
                  href={buildHref(searchParams, listing.page + 1)}
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

function buildHref(searchParams: SearchParams, page: number): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(searchParams)) {
    if (key === 'page' || value === undefined) {
      continue;
    }
    params.set(key, String(value));
  }
  params.set('page', String(page));
  return `/intelligence/patterns?${params.toString()}`;
}
