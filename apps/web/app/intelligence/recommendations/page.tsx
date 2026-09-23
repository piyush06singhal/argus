import Link from 'next/link';

import {
  api,
  formatDate,
  type RecommendationItem,
  type RecommendationList,
} from '@/lib/api';
import {
  decisionAndOutcome,
  historicalSummary,
  isInvestigation,
  recommendationEvidence,
  recommendationStatusStyle,
  recommendationTypeLabel,
  requiresPolicyAttention,
  sortRecommendations,
} from '@/lib/intelligence';

export const metadata = {
  title: 'Recommendation Center',
};

export const dynamic = 'force-dynamic';

type SearchParams = {
  project_id?: string;
  status?: string;
  recommendation_type?: string;
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
 * The Recommendation Center (§57, §39–§42, §81).
 *
 * The rules this page is built around:
 *
 * 1. **Advice is not an action.** Every recommendation that would need Phase 9's
 *    approvals renders its policy note next to the advice, so nobody discovers
 *    the gate after acting.
 * 2. **Evidence is counted, not asserted.** Each card shows how many comparable
 *    episodes it was based on, and says "no comparable historical case was found"
 *    when that number is zero.
 * 3. **A decision is not an outcome.** Accepted-but-unmeasured and
 *    accepted-and-effective render differently, because acceptance is not
 *    correctness.
 */
export default async function RecommendationsPage({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';
  const status = typeof searchParams.status === 'string' ? searchParams.status : '';

  let listing: RecommendationList | null = null;
  let error: string | null = null;
  if (projectId) {
    try {
      listing = await api.listRecommendations({
        project_id: projectId,
        page_size: 50,
        ...(status ? { status } : {}),
      });
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    }
  }

  const items: RecommendationItem[] = sortRecommendations(listing?.items ?? []);
  const open = items.filter((item) => item.status === 'OPEN');

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">
          Recommendation Center
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          What ARGUS suggests for this project&#39;s incidents, based on what has
          actually happened before. Nothing here runs anything: acting on a
          recommendation still goes through Phase 9&#39;s policy, safety and
          authority checks.
        </p>
      </div>

      <Card title="Filters" subtitle="Server-side, project-scoped">
        <form className="flex flex-wrap items-end gap-3" method="get">
          <input type="hidden" name="project_id" value={projectId} />
          <label className="flex flex-col gap-1 text-xs text-slate-400">
            Status
            <select name="status" defaultValue={status} className="input">
              <option value="">All</option>
              <option value="OPEN">Open</option>
              <option value="ACCEPTED">Accepted</option>
              <option value="EFFECTIVE">Effective</option>
              <option value="INEFFECTIVE">Ineffective</option>
              <option value="REGRESSION_CAUSING">Regression-causing</option>
              <option value="DISMISSED">Dismissed</option>
              <option value="EXPIRED">Expired</option>
            </select>
          </label>
          <button type="submit" className="btn">
            Apply
          </button>
        </form>
      </Card>

      {!projectId ? (
        <Card title="Pick a project" subtitle="Recommendations are project-scoped">
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
          Recommendations could not be loaded: {error}
        </section>
      ) : null}

      {projectId && !error ? (
        <>
          <Card
            title={`${open.length} waiting on a person`}
            subtitle={`${items.length} in total`}
          >
            {items.length === 0 ? (
              <p className="text-sm text-slate-400">
                No recommendation has been produced for this project. Advice
                appears only when there is an open incident and enough stored
                history to ground it.
              </p>
            ) : (
              <ul className="space-y-3">
                {items.map((item) => {
                  const { decision, outcome } = decisionAndOutcome(item);
                  const policyNote = requiresPolicyAttention(item);
                  return (
                    <li
                      key={item.id}
                      className="rounded-md border border-slate-800 bg-slate-900/40 p-3"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <Link
                          href={`/intelligence/recommendations/${item.id}?project_id=${encodeURIComponent(projectId)}`}
                          className="font-medium text-slate-100 hover:text-argus-accent"
                        >
                          {recommendationTypeLabel(item.recommendation_type)}
                        </Link>
                        <span className={`badge ${recommendationStatusStyle(item.status)}`}>
                          {item.status}
                        </span>
                        {isInvestigation(item.recommendation_type) ? (
                          <span className="badge bg-slate-800 text-slate-400">
                            Investigation
                          </span>
                        ) : null}
                        {policyNote ? (
                          <span className="badge bg-argus-warning/15 text-argus-warning">
                            Subject to Phase 9 policy
                          </span>
                        ) : null}
                      </div>
                      <p className="mt-2 text-sm text-slate-300">{item.title}</p>
                      {item.rationale ? (
                        <p className="mt-1 text-sm text-slate-400">{item.rationale}</p>
                      ) : null}
                      <p className="mt-2 text-xs text-slate-500">
                        {historicalSummary(item.historical)}
                      </p>
                      <dl className="mt-2 grid grid-cols-1 gap-2 text-xs md:grid-cols-2">
                        <div>
                          <dt className="uppercase tracking-wider text-slate-500">
                            Decision
                          </dt>
                          <dd className="text-slate-300">{decision}</dd>
                        </div>
                        <div>
                          <dt className="uppercase tracking-wider text-slate-500">
                            Outcome
                          </dt>
                          <dd className="text-slate-300">{outcome}</dd>
                        </div>
                      </dl>
                      {item.policy_note ? (
                        <p className="mt-2 text-xs text-slate-500">
                          Policy: {item.policy_note}
                        </p>
                      ) : null}
                      {recommendationEvidence(item).length > 0 ? (
                        <ul className="mt-2 space-y-1 text-xs text-slate-500">
                          {recommendationEvidence(item).map((row) => (
                            <li key={row.key}>
                              · {row.key}: {row.value}
                            </li>
                          ))}
                        </ul>
                      ) : null}
                      <p className="mt-2 text-xs text-slate-600">
                        Generated {formatDate(item.created_at)} · expires{' '}
                        {formatDate(item.expires_at)}
                      </p>
                    </li>
                  );
                })}
              </ul>
            )}
          </Card>
        </>
      ) : null}
    </div>
  );
}
