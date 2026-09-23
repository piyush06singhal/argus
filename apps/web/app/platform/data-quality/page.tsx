import { api, formatDate, type DataQualityResponse, type Project } from '@/lib/api';
import {
  dataQualityKindLabel,
  PLATFORM_BOUNDARY,
  severityStyle,
} from '@/lib/platform';
import { Card, Empty, Facts, ProjectScope, Table, readError } from '../ui';
import DataQualityCheckButton from './DataQualityCheckButton';
import DataQualityIssueControls from './DataQualityIssueControls';

export const metadata = {
  title: 'Data Quality',
};

export const dynamic = 'force-dynamic';

type SearchParams = { project_id?: string; status?: string };

/**
 * The data-quality center (§87–§90).
 *
 * This is where ARGUS audits itself: orphaned records, incidents without a
 * component, predictions without a feature snapshot, knowledge without evidence.
 * The rule is that a detected issue is **shown with its suggestion and never
 * auto-repaired** — §90 forbids silently mutating historical data, so the only
 * actions offered are the ones a human must choose.
 */
export default async function DataQualityPage({ searchParams }: { searchParams: SearchParams }) {
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

  let payload: DataQualityResponse | null = null;
  let error: string | null = null;
  if (project) {
    try {
      payload = await api.platformDataQuality(project.id, {
        status: status || undefined,
        limit: 200,
      });
    } catch (reason) {
      error = readError(reason);
    }
  }

  const issues = payload?.issues ?? [];
  const descriptions = payload?.descriptions ?? {};

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Data Quality</h1>
        <p className="mt-1 text-sm text-slate-400">
          Consistency checks across observability, incidents, the graph,
          predictions, remediation and learning. Detected issues are reported and
          suggested — never silently repaired.
        </p>
        <p className="mt-2 text-xs text-slate-500">{PLATFORM_BOUNDARY}</p>
      </div>

      <ProjectScope
        projects={projects}
        activeId={project?.id}
        basePath="/platform/data-quality"
      />

      {error ? (
        <Card title="Data quality unavailable">
          <Empty>{error}</Empty>
        </Card>
      ) : !project ? (
        <Card title="No projects">
          <Empty>Data-quality checks are project-scoped.</Empty>
        </Card>
      ) : (
        <>
          <Card title="Summary" subtitle="§89 — what the checks found">
            <Facts data={payload?.summary} empty="No summary is available." />
            <div className="mt-3">
              <DataQualityCheckButton projectId={project.id} />
            </div>
          </Card>

          <Card
            title="Issues"
            subtitle={`${issues.length} issue(s)${status ? ` · ${status}` : ''}`}
          >
            {issues.length === 0 ? (
              <Empty>
                No data-quality issue is open. That is a statement about the
                checks that ran, not a guarantee about the data.
              </Empty>
            ) : (
              <div className="space-y-3">
                {issues.map((issue) => (
                  <div
                    key={issue.id}
                    className="rounded-md border border-slate-800 bg-slate-900/40 p-3"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`badge ${severityStyle(issue.severity)}`}>
                        {issue.severity}
                      </span>
                      <span className="badge bg-slate-800 text-slate-300">
                        {dataQualityKindLabel(issue.kind)}
                      </span>
                      <h3 className="text-sm font-medium text-slate-200">{issue.title}</h3>
                      <span className="text-xs text-slate-500">
                        seen {issue.occurrence_count}× · {formatDate(issue.last_seen_at)}
                      </span>
                    </div>
                    {issue.detail ? (
                      <p className="mt-2 text-sm text-slate-400">{issue.detail}</p>
                    ) : null}
                    {descriptions[issue.kind] ? (
                      <p className="mt-1 text-xs text-slate-500">
                        {descriptions[issue.kind]}
                      </p>
                    ) : null}
                    {issue.suggestion ? (
                      <p className="mt-2 text-sm text-argus-info">
                        Suggested: {issue.suggestion}
                      </p>
                    ) : null}
                    <DataQualityIssueControls
                      issueId={issue.id}
                      projectId={project.id}
                      status={issue.status}
                    />
                  </div>
                ))}
              </div>
            )}
          </Card>

          {Object.keys(descriptions).length > 0 ? (
            <Card title="Issue catalogue" subtitle="§88 — what each finding means">
              <Table
                headers={['Kind', 'Meaning']}
                rows={Object.entries(descriptions).map(([kind, meaning]) => [
                  dataQualityKindLabel(kind),
                  meaning,
                ])}
              />
            </Card>
          ) : null}
        </>
      )}
    </div>
  );
}
