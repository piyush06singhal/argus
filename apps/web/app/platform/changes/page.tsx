import { api, formatDate, type ChangeFailureRateResponse, type Project } from '@/lib/api';
import {
  changeRiskLabel,
  changeRiskStyle,
  CORRELATION_NOT_CAUSATION,
  formatRatio,
  PLATFORM_BOUNDARY,
} from '@/lib/platform';
import { Card, Empty, Facts, Limitations, ProjectScope, Table, Tile, readError } from '../ui';

export const metadata = {
  title: 'Changes',
};

export const dynamic = 'force-dynamic';

type SearchParams = { project_id?: string; days?: string };

/**
 * Change intelligence (§38–§41, §84).
 *
 * The whole page is written around one sentence: **correlation is not
 * causation.** A deployment that sits near an incident is labelled as
 * *correlated*, with the temporal relationship stated, never as the cause. The
 * change-failure rate is shown with its exact methodology so the number cannot
 * be read as something the definition does not support.
 */
export default async function ChangesPage({ searchParams }: { searchParams: SearchParams }) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';
  const days = Number.parseInt(String(searchParams.days ?? '30'), 10) || 30;

  let projects: Project[] = [];
  try {
    projects = (await api.listProjects(1, 50)).items;
  } catch {
    projects = [];
  }
  const project = projects.find((item) => item.id === requested) ?? projects[0];

  let changes: Array<Record<string, unknown>> = [];
  let note: string | null = null;
  let failureRate: ChangeFailureRateResponse | null = null;
  let error: string | null = null;
  if (project) {
    const [changesResult, rateResult] = await Promise.allSettled([
      api.platformChanges(project.id, { days, limit: 100 }),
      api.platformChangeFailureRate(project.id, days),
    ]);
    if (changesResult.status === 'fulfilled') {
      changes = changesResult.value.changes;
      note = changesResult.value.note;
    } else {
      error = readError(changesResult.reason);
    }
    if (rateResult.status === 'fulfilled') {
      failureRate = rateResult.value;
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Changes</h1>
        <p className="mt-1 text-sm text-slate-400">
          Deployments and configuration changes in the window, and the incidents
          they are temporally associated with.
        </p>
        <p className="mt-2 text-xs text-argus-warning">{CORRELATION_NOT_CAUSATION}</p>
        <p className="mt-1 text-xs text-slate-500">{PLATFORM_BOUNDARY}</p>
      </div>

      <ProjectScope
        projects={projects}
        activeId={project?.id}
        basePath="/platform/changes"
        extraQuery={`days=${days}`}
      />

      {error ? (
        <Card title="Changes unavailable">
          <Empty>{error}</Empty>
        </Card>
      ) : !project ? (
        <Card title="No projects">
          <Empty>Change intelligence is project-scoped.</Empty>
        </Card>
      ) : (
        <>
          {failureRate ? (
            <Card
              title="Change failure rate"
              subtitle={`Last ${failureRate.window_days} days · §84`}
            >
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
                <Tile label="Deployments" value={failureRate.deployments_total} />
                <Tile label="Succeeded" value={failureRate.deployments_succeeded} />
                <Tile label="Failed" value={failureRate.deployments_failed} />
                <Tile label="Rolled back" value={failureRate.deployments_rolled_back} />
                <Tile
                  label="Failure rate"
                  value={
                    failureRate.failure_rate != null
                      ? formatRatio(failureRate.failure_rate)
                      : '—'
                  }
                  hint={`${failureRate.deployments_with_incident} with an associated incident`}
                />
              </div>
              <div className="mt-3 border-t border-slate-800 pt-3">
                <h3 className="mb-1 text-xs uppercase tracking-wider text-slate-500">
                  Methodology
                </h3>
                <p className="text-sm text-slate-300">{failureRate.methodology}</p>
              </div>
              <Limitations items={failureRate.limitations} />
            </Card>
          ) : null}

          <Card title="Changes in the window" subtitle={note ?? undefined}>
            {changes.length === 0 ? (
              <Empty>No change was recorded for this project in the window.</Empty>
            ) : (
              <Table
                headers={['Change', 'Type', 'Risk', 'Correlated incidents', 'When']}
                rows={changes.map((item) => {
                  const riskBand = typeof item.risk_band === 'string' ? item.risk_band : null;
                  const incidents = Array.isArray(item.correlated_incidents)
                    ? (item.correlated_incidents as Array<Record<string, unknown>>)
                    : [];
                  return [
                    String(item.title ?? item.summary ?? item.id ?? '—'),
                    String(item.change_type ?? '—'),
                    riskBand ? (
                      <span key="r" className={`badge ${changeRiskStyle(riskBand)}`}>
                        {changeRiskLabel(riskBand)}
                      </span>
                    ) : (
                      '—'
                    ),
                    incidents.length === 0
                      ? '—'
                      : incidents
                          .map((incident) => String(incident.title ?? incident.id ?? '—'))
                          .join(', '),
                    formatDate(
                      typeof item.occurred_at === 'string' ? item.occurred_at : null
                    ),
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
