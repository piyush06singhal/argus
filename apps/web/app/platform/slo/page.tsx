import Link from 'next/link';

import { api, formatDate, type Project, type SloOverviewResponse } from '@/lib/api';
import {
  burnStateLabel,
  burnStateStyle,
  formatRatio,
  PLATFORM_BOUNDARY,
  sloStatusLabel,
  sloStatusStyle,
} from '@/lib/platform';
import { Card, Empty, Limitations, ProjectScope, Table, Tile, readError } from '../ui';
import CreateSloForm from './CreateSloForm';

export const metadata = {
  title: 'SLO & Reliability',
};

export const dynamic = 'force-dynamic';

type SearchParams = { project_id?: string };

/**
 * Service-level objectives and error budgets (§32–§35).
 *
 * The rule this page is built around: **an objective that has never been
 * evaluated is not meeting.** A freshly-defined SLO with no data renders
 * `UNKNOWN` with an explicit "never evaluated" note, never a green badge — the
 * alternative is an SLO that looks compliant because nothing measured it.
 */
export default async function SloPage({ searchParams }: { searchParams: SearchParams }) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  let projects: Project[] = [];
  try {
    projects = (await api.listProjects(1, 50)).items;
  } catch {
    projects = [];
  }
  const project = projects.find((item) => item.id === requested) ?? projects[0];

  let overview: SloOverviewResponse | null = null;
  let error: string | null = null;
  if (project) {
    try {
      overview = await api.platformSlo(project.id);
    } catch (reason) {
      error = readError(reason);
    }
  }

  const objectives = overview?.objectives ?? [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">SLO &amp; Reliability</h1>
        <p className="mt-1 text-sm text-slate-400">
          Explicit reliability objectives and their remaining error budget. An
          objective ARGUS was never given is not invented.
        </p>
        <p className="mt-2 text-xs text-slate-500">{PLATFORM_BOUNDARY}</p>
      </div>

      <ProjectScope projects={projects} activeId={project?.id} basePath="/platform/slo" />

      {error ? (
        <Card title="SLOs unavailable">
          <Empty>{error}</Empty>
        </Card>
      ) : !project ? (
        <Card title="No projects">
          <Empty>Objectives are project-scoped.</Empty>
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
            <Tile label="Objectives" value={overview?.objectives_total ?? 0} />
            {['MEETING', 'AT_RISK', 'BREACHED', 'UNKNOWN'].map((status) => (
              <Tile
                key={status}
                label={sloStatusLabel(status)}
                value={overview?.by_status?.[status] ?? 0}
              />
            ))}
          </div>

          <CreateSloForm projectId={project.id} />

          <Card title="Objectives" subtitle="§32 — target, reading and burn state">
            {objectives.length === 0 ? (
              <Empty>
                No service-level objective is defined for this project. ARGUS does
                not create one it was never given.
              </Empty>
            ) : (
              <Table
                headers={['Objective', 'Indicator', 'Target', 'Reading', 'Status', 'Burn', 'Remaining']}
                rows={objectives.map((item) => [
                  <Link
                    key="n"
                    href={`/platform/slo/${encodeURIComponent(
                      item.slo_id
                    )}?project_id=${project.id}`}
                    className="text-argus-accent"
                  >
                    {item.name}
                  </Link>,
                  item.indicator,
                  `${item.comparison} ${item.target}${item.unit ? ` ${item.unit}` : ''}`,
                  item.reading != null ? formatRatio(item.reading) : '—',
                  <span key="s" className={`badge ${sloStatusStyle(item.status)}`}>
                    {sloStatusLabel(item.status)}
                    {item.never_evaluated ? ' · never evaluated' : ''}
                  </span>,
                  item.burn_state ? (
                    <span key="b" className={`badge ${burnStateStyle(item.burn_state)}`}>
                      {burnStateLabel(item.burn_state)}
                      {item.burn_rate != null ? ` ${item.burn_rate.toFixed(2)}×` : ''}
                    </span>
                  ) : (
                    '—'
                  ),
                  item.remaining_percent != null
                    ? `${item.remaining_percent.toFixed(1)}%`
                    : '—',
                ])}
              />
            )}
            <Limitations items={overview?.limitations} />
            {overview?.as_of ? (
              <p className="mt-3 text-xs text-slate-500">
                Evaluated as of {formatDate(overview.as_of)}
              </p>
            ) : null}
          </Card>
        </>
      )}
    </div>
  );
}
