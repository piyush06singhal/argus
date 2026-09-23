import Link from 'next/link';

import { api, formatDate, type LearnedRelationshipList } from '@/lib/api';
import {
  confidenceStyle,
  HISTORICAL_RELATIONSHIP_FALLBACK,
  mayDrawArrow,
  relationshipHeadline,
  relationshipKindLabel,
  sortRelationships,
  supportLabel,
} from '@/lib/intelligence';

export const metadata = {
  title: 'Learned relationships',
};

export const dynamic = 'force-dynamic';

type SearchParams = { project_id?: string; kind?: string; status?: string };

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

const KINDS = [
  { value: '', label: 'All kinds' },
  { value: 'FAILURE_PROPAGATION', label: 'Failure propagation' },
  { value: 'SHARED_FAILURE', label: 'Shared failure (no direction)' },
  { value: 'DEPENDENCY_DEGRADATION', label: 'Dependency degradation' },
  { value: 'REMEDIATION_INFLUENCE', label: 'Remediation influence' },
];

/**
 * Learned relationships (§23, §24).
 *
 * This page exists to keep two different things apart. The structural graph says
 * how a system is wired; these rows say what history observed happening between
 * two components. Rendering them together — or with the same visual grammar —
 * would turn an observation into an architectural claim.
 *
 * So:
 *
 * * **Direction is stated, not implied.** An undirected edge is labelled
 *   "no direction claimed" and drawn without an arrow, whatever the ordering of
 *   the list suggests.
 * * **Support travels with every row.** Each relationship shows the episodes
 *   behind it, because "these two failed together" means something different at
 *   2 episodes and at 40.
 * * **The disclaimer is rendered verbatim** from the payload, so a change in the
 *   backend's wording reaches the reader.
 */
export default async function RelationshipsPage({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';
  const kind = typeof searchParams.kind === 'string' ? searchParams.kind : '';
  const status = typeof searchParams.status === 'string' ? searchParams.status : '';

  let listing: LearnedRelationshipList | null = null;
  let error: string | null = null;
  if (projectId) {
    try {
      listing = await api.intelligenceRelationships({
        project_id: projectId,
        page_size: 100,
        ...(kind ? { kind } : {}),
        ...(status ? { status } : {}),
      });
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught);
    }
  }

  const items = sortRelationships(listing?.items ?? []);
  const directed = items.filter((item) => mayDrawArrow(item));
  const undirected = items.filter((item) => !mayDrawArrow(item));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">
          Learned relationships
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          Relationships ARGUS derived from completed episodes. These are kept
          deliberately separate from the structural dependency graph: an
          observation is not an architecture.
        </p>
        <p className="mt-2 text-xs text-slate-500">
          {listing?.relationship_note ?? HISTORICAL_RELATIONSHIP_FALLBACK}
        </p>
        {listing?.limitations.length ? (
          <ul className="mt-1 space-y-1 text-xs text-slate-500">
            {listing.limitations.map((limitation) => (
              <li key={limitation}>· {limitation}</li>
            ))}
          </ul>
        ) : null}
      </div>

      <Card title="Filters" subtitle="Filtering is server-side and project-scoped">
        <form className="flex flex-wrap items-end gap-3" method="get">
          <input type="hidden" name="project_id" value={projectId} />
          <label className="flex flex-col gap-1 text-xs text-slate-400">
            Kind
            <select name="kind" defaultValue={kind} className="input">
              {KINDS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-slate-400">
            Status
            <select name="status" defaultValue={status} className="input">
              <option value="">Active only</option>
              <option value="STALE">No longer confirmed (stale)</option>
              <option value="SUPERSEDED">Superseded</option>
            </select>
          </label>
          <button type="submit" className="btn">
            Apply
          </button>
        </form>
        {status === 'STALE' ? (
          <p className="mt-2 text-xs text-slate-500">
            Stale relationships are history, not current belief. They are kept
            because an edge that stopped being confirmed is evidence that the
            topology changed.
          </p>
        ) : null}
      </Card>

      {!projectId ? (
        <Card title="Pick a project" subtitle="Relationships are project-scoped">
          <p className="text-sm text-slate-400">
            Open this page from the{' '}
            <Link href="/intelligence" className="text-argus-accent">
              Learning Center
            </Link>{' '}
            so the project scope travels with the link.
          </p>
        </Card>
      ) : null}

      {error ? (
        <section className="rounded-md border border-argus-warning/40 bg-argus-warning/10 p-3 text-sm text-argus-warning">
          Relationships could not be loaded: {error}
        </section>
      ) : null}

      {projectId && !error ? (
        <>
          <Card
            title={`${listing?.total ?? 0} relationship${
              listing?.total === 1 ? '' : 's'
            }`}
            subtitle={`${directed.length} with a direction, ${undirected.length} without`}
          >
            {items.length === 0 ? (
              <p className="text-sm text-slate-400">
                No component relationship has been learned from this project&#39;s
                history yet. With two-component episodes and a recurring pattern,
                they appear after the next learning run.
              </p>
            ) : (
              <ul className="space-y-3">
                {items.map((item) => (
                  <li
                    key={item.id}
                    className="rounded-md border border-slate-800 bg-slate-900/40 p-3"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="badge bg-slate-800 text-slate-300">
                        {relationshipKindLabel(item.kind)}
                      </span>
                      {mayDrawArrow(item) ? (
                        <span className="badge bg-argus-info/20 text-argus-info">
                          {item.source_component_name} → {item.target_component_name}
                        </span>
                      ) : (
                        <span className="badge bg-slate-800 text-slate-400">
                          {item.source_component_name} ↕ {item.target_component_name} —
                          no direction claimed
                        </span>
                      )}
                      <span className={`badge ${confidenceStyle(item.confidence)}`}>
                        {item.confidence}
                      </span>
                      {item.status !== 'ACTIVE' ? (
                        <span className="badge bg-argus-warning/20 text-argus-warning">
                          {item.status}
                        </span>
                      ) : null}
                    </div>
                    <p className="mt-2 text-sm text-slate-300">
                      {relationshipHeadline(item)}
                    </p>
                    <p className="mt-1 text-xs text-slate-500">
                      {supportLabel(item.support_strength, item.sample_count) ??
                        `${item.sample_count} episode${
                          item.sample_count === 1 ? '' : 's'
                        }`}{' '}
                      · observed {formatDate(item.first_seen_at)} →{' '}
                      {formatDate(item.last_seen_at)} · {item.algorithm}{' '}
                      {item.algorithm_version}
                    </p>
                    {item.limitations.length > 0 ? (
                      <ul className="mt-2 space-y-1 text-xs text-slate-500">
                        {item.limitations.map((limitation) => (
                          <li key={limitation}>· {limitation}</li>
                        ))}
                      </ul>
                    ) : null}
                    <div className="mt-2 flex flex-wrap gap-3 text-xs">
                      <Link
                        href={`/intelligence/components/${item.source_component_id}?project_id=${encodeURIComponent(projectId)}`}
                        className="text-argus-accent"
                      >
                        {item.source_component_name} profile →
                      </Link>
                      {item.target_component_id !== item.source_component_id ? (
                        <Link
                          href={`/intelligence/components/${item.target_component_id}?project_id=${encodeURIComponent(projectId)}`}
                          className="text-argus-accent"
                        >
                          {item.target_component_name} profile →
                        </Link>
                      ) : null}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          {undirected.length > 0 ? (
            <Card
              title="Undirected observations"
              subtitle="§24 — co-failure, with no claim about which end came first"
            >
              <p className="text-sm text-slate-400">
                {undirected.length} relationship
                {undirected.length === 1 ? '' : 's'} record that two components
                failed in the same episodes. Nothing established a direction, so
                none is shown. Treating these as propagation would be reading
                information into the data that the data does not contain.
              </p>
            </Card>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
