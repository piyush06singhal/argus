import Link from 'next/link';

import { api, formatDate, type Project, type SearchResponse } from '@/lib/api';
import { PLATFORM_BOUNDARY, searchKindLabel } from '@/lib/platform';
import { Card, Empty, ProjectScope, Table, Tile, readError } from '../ui';

export const metadata = {
  title: 'Search',
};

export const dynamic = 'force-dynamic';

type SearchParams = { project_id?: string; q?: string; kind?: string };

/**
 * Global reliability search (§17, §18).
 *
 * Search is bounded and says so: results are grouped by kind, the per-kind cap
 * is surfaced, and a query that matched nothing says it matched nothing. An
 * empty result is never rendered as "all clear".
 */
export default async function SearchPage({ searchParams }: { searchParams: SearchParams }) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';
  const query = typeof searchParams.q === 'string' ? searchParams.q.trim() : '';
  const kind = typeof searchParams.kind === 'string' ? searchParams.kind : '';

  let projects: Project[] = [];
  try {
    projects = (await api.listProjects(1, 50)).items;
  } catch {
    projects = [];
  }
  const project = projects.find((item) => item.id === requested) ?? projects[0];

  let response: SearchResponse | null = null;
  let error: string | null = null;
  if (project && query) {
    try {
      response = await api.platformSearch({
        projectId: project.id,
        query,
        kind: kind || undefined,
      });
    } catch (reason) {
      error = readError(reason);
    }
  }

  const groups = response
    ? Object.entries(response.results).filter(([, hits]) => hits.length > 0)
    : [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Global Search</h1>
        <p className="mt-1 text-sm text-slate-400">
          One query over incidents, cases, components, changes, forecasts,
          remediation and learned knowledge — scoped to a project.
        </p>
        <p className="mt-2 text-xs text-slate-500">{PLATFORM_BOUNDARY}</p>
      </div>

      <ProjectScope
        projects={projects}
        activeId={project?.id}
        basePath="/platform/search"
      />

      {!project ? (
        <Card title="No projects">
          <Empty>Search is project-scoped.</Empty>
        </Card>
      ) : (
        <>
          <Card title="Query" subtitle="Bounded per kind (§18)">
            <form method="get" className="flex flex-wrap gap-2">
              <input type="hidden" name="project_id" value={project.id} />
              <input
                className="input flex-1"
                name="q"
                defaultValue={query}
                placeholder="Search reliability records…"
              />
              <select className="input" name="kind" defaultValue={kind}>
                <option value="">All kinds</option>
                {[
                  'incident',
                  'case',
                  'anomaly',
                  'component',
                  'deployment',
                  'remediation',
                  'forecast',
                  'knowledge',
                  'data_quality',
                ].map((value) => (
                  <option key={value} value={value}>
                    {searchKindLabel(value)}
                  </option>
                ))}
              </select>
              <button type="submit" className="btn btn-primary">
                Search
              </button>
            </form>
            {response?.notes?.length ? (
              <ul className="mt-3 space-y-1 text-xs text-slate-500">
                {response.notes.map((item) => (
                  <li key={item}>· {item}</li>
                ))}
              </ul>
            ) : null}
          </Card>

          {!query ? (
            <Card title="Nothing searched yet">
              <Empty>Enter a query to search the stored records.</Empty>
            </Card>
          ) : error ? (
            <Card title="Search failed">
              <Empty>{error}</Empty>
            </Card>
          ) : response ? (
            <>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Tile label="Total matches" value={response.total} />
                {Object.entries(response.by_kind).map(([kindName, count]) => (
                  <Tile key={kindName} label={searchKindLabel(kindName)} value={count} />
                ))}
              </div>

              {groups.length === 0 ? (
                <Card title="No matches">
                  <Empty>
                    Nothing stored matches “{query}”. This is not a statement
                    about system health — only about the search.
                  </Empty>
                </Card>
              ) : (
                groups.map(([kindName, hits]) => (
                  <Card
                    key={kindName}
                    title={searchKindLabel(kindName)}
                    subtitle={`${hits.length} result(s)`}
                  >
                    <Table
                      headers={['Title', 'Status', 'When', 'Matched']}
                      rows={hits.map((hit) => [
                        hit.route ? (
                          <Link key="t" href={hit.route} className="text-argus-accent">
                            {hit.title}
                          </Link>
                        ) : (
                          hit.title
                        ),
                        hit.status ?? '—',
                        formatDate(hit.occurred_at),
                        hit.matched_field ?? '—',
                      ])}
                    />
                  </Card>
                ))
              )}
            </>
          ) : null}
        </>
      )}
    </div>
  );
}
