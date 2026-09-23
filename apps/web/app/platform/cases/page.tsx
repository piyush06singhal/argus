import Link from 'next/link';

import { api, formatDate, type CaseListResponse, type Project } from '@/lib/api';
import {
  caseStatusLabel,
  caseStatusStyle,
  isCaseTerminal,
  PLATFORM_BOUNDARY,
} from '@/lib/platform';
import { Card, Empty, ProjectScope, Table, Tile, readError } from '../ui';

export const metadata = {
  title: 'Reliability Cases',
};

export const dynamic = 'force-dynamic';

type SearchParams = { project_id?: string; status?: string };

/**
 * The reliability case list (§14–§16).
 *
 * A case is the unit a human works, so the list is ordered and filtered by the
 * thing a human cares about — terminal or live — rather than by raw recency
 * alone. Terminal cases are shown, not hidden: a case that was cancelled carries
 * the same information as one that was resolved, and hiding it is how the
 * record starts to lie.
 */
export default async function CasesPage({ searchParams }: { searchParams: SearchParams }) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';
  const status = typeof searchParams.status === 'string' ? searchParams.status : '';

  let projects: Project[] = [];
  try {
    projects = (await api.listProjects(1, 50)).items;
  } catch {
    projects = [];
  }
  const project = projects.find((item) => item.id === requested) ?? projects[0];

  let listing: CaseListResponse | null = null;
  let error: string | null = null;
  if (project) {
    try {
      listing = await api.platformCases(project.id, {
        status: status || undefined,
        limit: 100,
      });
    } catch (reason) {
      error = readError(reason);
    }
  }

  const cases = listing?.cases ?? [];
  const live = cases.filter((item) => !isCaseTerminal(item.status));
  const terminal = cases.filter((item) => isCaseTerminal(item.status));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Reliability Cases</h1>
        <p className="mt-1 text-sm text-slate-400">
          The unified operational unit: one situation, followed from detection
          through diagnosis, remediation, verification and learning.
        </p>
        <p className="mt-2 text-xs text-slate-500">{PLATFORM_BOUNDARY}</p>
      </div>

      <ProjectScope projects={projects} activeId={project?.id} basePath="/platform/cases" />

      {error ? (
        <Card title="Cases unavailable">
          <Empty>{error}</Empty>
        </Card>
      ) : !project ? (
        <Card title="No projects">
          <Empty>Every case is scoped to a project, so there is nothing to list.</Empty>
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Tile label="Live cases" value={live.length} hint="Not yet closed" />
            <Tile label="Terminal cases" value={terminal.length} hint="Closed or cancelled" />
            <Tile label="Listed" value={cases.length} hint={`Limit ${listing?.limit ?? 0}`} />
            <Tile
              label="Total matching"
              value={listing?.total ?? 0}
              hint="May exceed the page size"
            />
          </div>

          <Card
            title="Live cases"
            subtitle="Ordered by the case engine — the ones still being worked"
          >
            {live.length === 0 ? (
              <Empty>No reliability case is currently live.</Empty>
            ) : (
              <Table
                headers={['Reference', 'Title', 'Status', 'Severity', 'Opened', 'Duration']}
                rows={live.map((item) => [
                  <Link
                    key="r"
                    href={`/platform/cases/${encodeURIComponent(
                      item.id
                    )}?project_id=${project.id}`}
                    className="font-mono text-xs text-argus-accent"
                  >
                    {item.reference}
                  </Link>,
                  item.title,
                  <span key="s" className={`badge ${caseStatusStyle(item.status)}`}>
                    {caseStatusLabel(item.status)}
                  </span>,
                  item.severity ?? '—',
                  formatDate(item.opened_at),
                  item.duration_seconds != null
                    ? `${Math.round(item.duration_seconds / 60)}m`
                    : '—',
                ])}
              />
            )}
          </Card>

          {terminal.length > 0 ? (
            <Card
              title="Terminal cases"
              subtitle="Closed and cancelled — kept visible so the record stays complete"
            >
              <Table
                headers={['Reference', 'Title', 'Status', 'Closed']}
                rows={terminal.map((item) => [
                  <Link
                    key="r"
                    href={`/platform/cases/${encodeURIComponent(
                      item.id
                    )}?project_id=${project.id}`}
                    className="font-mono text-xs text-argus-accent"
                  >
                    {item.reference}
                  </Link>,
                  item.title,
                  <span key="s" className={`badge ${caseStatusStyle(item.status)}`}>
                    {caseStatusLabel(item.status)}
                  </span>,
                  formatDate(item.closed_at),
                ])}
              />
            </Card>
          ) : null}
        </>
      )}
    </div>
  );
}
