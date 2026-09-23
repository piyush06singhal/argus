import Link from 'next/link';

import { api, type CatalogEntryResponse, type Project } from '@/lib/api';
import {
  componentStateLabel,
  componentStateStyle,
  componentStateHint,
  PLATFORM_BOUNDARY,
  UNKNOWN_OWNERSHIP,
} from '@/lib/platform';
import { Card, Empty, ProjectScope, Table, Tile, readError } from '../ui';

export const metadata = {
  title: 'Service Catalog',
};

export const dynamic = 'force-dynamic';

type SearchParams = { project_id?: string };

/**
 * The service catalog (§30, §31).
 *
 * The catalog's job is to answer "what is this service, who owns it, and what
 * state is it in" from stored facts. Where a fact was never recorded, the page
 * says `UNKNOWN` — notably ownership, which ARGUS never infers from a naming
 * convention, because an invented owner is worse than an absent one.
 */
export default async function ServicesPage({ searchParams }: { searchParams: SearchParams }) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  let projects: Project[] = [];
  try {
    projects = (await api.listProjects(1, 50)).items;
  } catch {
    projects = [];
  }
  const project = projects.find((item) => item.id === requested) ?? projects[0];

  let services: CatalogEntryResponse[] = [];
  let total = 0;
  let error: string | null = null;
  if (project) {
    try {
      const response = await api.platformServices(project.id);
      services = response.services;
      total = response.total;
    } catch (reason) {
      error = readError(reason);
    }
  }

  const byState = services.reduce<Record<string, number>>((acc, item) => {
    acc[item.state] = (acc[item.state] ?? 0) + 1;
    return acc;
  }, {});
  const unowned = services.filter((item) => {
    const owner = item.owner ?? {};
    return !owner.team || owner.team === UNKNOWN_OWNERSHIP;
  }).length;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Service Catalog</h1>
        <p className="mt-1 text-sm text-slate-400">
          Every registered component with its owner, dependencies, operational
          state and reliability profile — assembled from stored rows.
        </p>
        <p className="mt-2 text-xs text-slate-500">{PLATFORM_BOUNDARY}</p>
      </div>

      <ProjectScope projects={projects} activeId={project?.id} basePath="/platform/services" />

      {error ? (
        <Card title="Catalog unavailable">
          <Empty>{error}</Empty>
        </Card>
      ) : !project ? (
        <Card title="No projects">
          <Empty>The catalog is project-scoped, so there is nothing to list.</Empty>
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Tile label="Services" value={total} hint="Registered components" />
            <Tile
              label="In incident"
              value={byState.INCIDENT ?? 0}
              hint="An open incident names them"
            />
            <Tile
              label="Degraded or at risk"
              value={(byState.DEGRADED ?? 0) + (byState.AT_RISK ?? 0)}
            />
            <Tile
              label="Unrecorded ownership"
              value={unowned}
              hint="No owner was ever recorded — not inferred"
            />
          </div>

          <Card title="Services" subtitle="Ordered by operational state — worst first">
            {services.length === 0 ? (
              <Empty>
                No component is registered for this project. A service appears
                here once it has been discovered or registered.
              </Empty>
            ) : (
              <Table
                headers={['Service', 'Type', 'State', 'Owner', 'Dependencies']}
                rows={services.map((item) => {
                  const owner = (item.owner ?? {}) as Record<string, unknown>;
                  const team = typeof owner.team === 'string' ? owner.team : null;
                  return [
                    <Link
                      key="n"
                      href={`/platform/services/${encodeURIComponent(
                        item.component_id
                      )}?project_id=${project.id}`}
                      className="text-argus-accent"
                    >
                      {item.name}
                    </Link>,
                    item.component_type,
                    <span title={componentStateHint(item.state)} key="s">
                      <span className={`badge ${componentStateStyle(item.state)}`}>
                        {componentStateLabel(item.state)}
                      </span>
                    </span>,
                    team ? (
                      team
                    ) : (
                      <span key="o" className="text-xs text-slate-500">
                        {UNKNOWN_OWNERSHIP} — no owner recorded
                      </span>
                    ),
                    String(item.dependencies.length),
                  ];
                })}
              />
            )}
          </Card>
        </>
      )}
    </div>
  );
}
