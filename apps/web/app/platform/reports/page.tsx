import Link from 'next/link';

import {
  api,
  formatDate,
  type ImprovementPlanResponse,
  type PlatformMetricsResponse,
  type PostmortemResponse,
  type Project,
  type ReportResponse,
} from '@/lib/api';
import {
  formatPercent,
  formatSeconds,
  mttrBreakdown,
  PLATFORM_BOUNDARY,
} from '@/lib/platform';
import { Card, Empty, Facts, Limitations, ProjectScope, Row, Table, readError } from '../ui';

export const metadata = {
  title: 'Reports',
};

export const dynamic = 'force-dynamic';

type SearchParams = { project_id?: string; days?: string; kind?: string; incident_id?: string };

const REPORT_KINDS = [
  'reliability',
  'incidents',
  'remediation',
  'learning',
  'executive',
  'change',
];

/**
 * Reports, postmortems and the reliability roadmap (§77–§85).
 *
 * Three rules hold this page together:
 *
 * 1. **A report carries its limitations.** Each section renders the limitations
 *    the backend attached, so a report cannot be read as more complete than the
 *    data behind it.
 * 2. **MTTR is broken down** (§74), never collapsed into one number.
 * 3. **A postmortem is evidence, or it says what is unknown** (§79, §80). The
 *    unknowns are rendered next to the narrative, not buried.
 */
export default async function ReportsPage({ searchParams }: { searchParams: SearchParams }) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';
  const days = Number.parseInt(String(searchParams.days ?? '30'), 10) || 30;
  const kind = typeof searchParams.kind === 'string' && searchParams.kind
    ? searchParams.kind
    : 'reliability';
  const incidentId =
    typeof searchParams.incident_id === 'string' ? searchParams.incident_id : '';

  let projects: Project[] = [];
  try {
    projects = (await api.listProjects(1, 50)).items;
  } catch {
    projects = [];
  }
  const project = projects.find((item) => item.id === requested) ?? projects[0];

  let report: ReportResponse | null = null;
  let metrics: PlatformMetricsResponse | null = null;
  let plan: ImprovementPlanResponse | null = null;
  let postmortem: PostmortemResponse | null = null;
  let error: string | null = null;

  if (project) {
    const [reportResult, metricsResult, planResult] = await Promise.allSettled([
      api.platformReport(project.id, { kind, days }),
      api.platformMetrics(project.id, days),
      api.platformImprovementPlan(project.id, days),
    ]);
    if (reportResult.status === 'fulfilled') {
      report = reportResult.value;
    } else {
      error = readError(reportResult.reason);
    }
    if (metricsResult.status === 'fulfilled') {
      metrics = metricsResult.value;
    }
    if (planResult.status === 'fulfilled') {
      plan = planResult.value;
    }
    if (incidentId) {
      try {
        postmortem = await api.platformPostmortem(incidentId, project.id, false);
      } catch {
        postmortem = null;
      }
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Reports &amp; Postmortems</h1>
        <p className="mt-1 text-sm text-slate-400">
          Generated reliability reports, engineering metrics and structured
          incident postmortems — every fact drawn from stored evidence.
        </p>
        <p className="mt-2 text-xs text-slate-500">{PLATFORM_BOUNDARY}</p>
      </div>

      <ProjectScope
        projects={projects}
        activeId={project?.id}
        basePath="/platform/reports"
        extraQuery={`days=${days}&kind=${kind}`}
      />

      {error ? (
        <Card title="Reports unavailable">
          <Empty>{error}</Empty>
        </Card>
      ) : !project ? (
        <Card title="No projects">
          <Empty>Reports are project-scoped.</Empty>
        </Card>
      ) : (
        <>
          <Card title="Report" subtitle={`${kind} · last ${days} days`}>
            <form method="get" className="mb-4 flex flex-wrap gap-2">
              <input type="hidden" name="project_id" value={project.id} />
              <select className="input" name="kind" defaultValue={kind}>
                {REPORT_KINDS.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
              <input className="input w-24" name="days" defaultValue={String(days)} />
              <button type="submit" className="btn btn-primary">
                Generate
              </button>
            </form>
            {!report ? (
              <Empty>The report could not be generated.</Empty>
            ) : (
              <>
                <p className="text-xs text-slate-500">
                  Generated {formatDate(report.generated_at)} · window {report.window_days} days
                </p>
                <div className="mt-4 space-y-4">
                  {Object.entries(report.sections).map(([section, content]) => (
                    <div key={section}>
                      <h3 className="mb-1 text-xs uppercase tracking-wider text-slate-500">
                        {section.replace(/_/g, ' ')}
                      </h3>
                      {content && typeof content === 'object' ? (
                        <Facts data={content as Record<string, unknown>} />
                      ) : (
                        <p className="text-sm text-slate-300">{String(content)}</p>
                      )}
                    </div>
                  ))}
                </div>
                <Limitations items={report.limitations} />
              </>
            )}
          </Card>

          {metrics ? (
            <Card title="Engineering reliability metrics" subtitle={`§73–§75, §85 · last ${days} days`}>
              <div className="grid gap-4 lg:grid-cols-2">
                <div>
                  <h3 className="mb-2 text-xs uppercase tracking-wider text-slate-500">
                    MTTR breakdown
                  </h3>
                  <div>
                    {mttrBreakdown(
                      (metrics.mttr ?? {}) as Record<string, unknown>
                    ).map((item) => (
                      <Row key={item.label} label={item.label}>
                        {formatSeconds(item.value)}
                      </Row>
                    ))}
                  </div>
                </div>
                <div>
                  <h3 className="mb-2 text-xs uppercase tracking-wider text-slate-500">
                    Metrics
                  </h3>
                  <Facts data={metrics as unknown as Record<string, unknown>} />
                </div>
              </div>
            </Card>
          ) : null}

          {plan ? (
            <Card title="Reliability improvement plan" subtitle={`§82 · last ${plan.window_days} days`}>
              <p className="mb-3 text-xs text-slate-500">{plan.criteria}</p>
              {plan.items.length === 0 ? (
                <Empty>
                  No recurring recommendation reaches the prioritisation criteria.
                </Empty>
              ) : (
                <Table
                  headers={['Recommendation', 'Priority', 'Occurrences', 'Rationale']}
                  rows={plan.items.map((item) => [
                    String(item.title ?? item.recommendation ?? '—'),
                    String(item.priority ?? '—'),
                    String(item.occurrence_count ?? item.count ?? '—'),
                    String(item.rationale ?? item.reason ?? '—'),
                  ])}
                />
              )}
              {plan.truncated ? (
                <p className="mt-3 text-xs text-slate-500">
                  The plan was truncated at the configured depth.
                </p>
              ) : null}
              <p className="mt-3 text-xs text-slate-500">{plan.note}</p>
            </Card>
          ) : null}

          <Card
            title="Incident postmortem"
            subtitle="§79, §80 — evidence, not invention"
          >
            <form method="get" className="mb-4 flex flex-wrap gap-2">
              <input type="hidden" name="project_id" value={project.id} />
              <input type="hidden" name="kind" value={kind} />
              <input type="hidden" name="days" value={String(days)} />
              <input
                className="input flex-1"
                name="incident_id"
                defaultValue={incidentId}
                placeholder="Incident id"
              />
              <button type="submit" className="btn btn-primary">
                Generate postmortem
              </button>
            </form>
            {!incidentId ? (
              <Empty>Enter an incident id to generate its postmortem.</Empty>
            ) : !postmortem ? (
              <Empty>The postmortem could not be generated for that incident.</Empty>
            ) : (
              <>
                <h3 className="text-sm font-medium text-slate-200">{postmortem.title}</h3>
                <p className="mt-1 text-xs text-slate-500">
                  Generated {formatDate(postmortem.generated_at)}
                </p>
                <div className="mt-4 space-y-4">
                  {Object.entries(postmortem.sections).map(([section, content]) => (
                    <div key={section}>
                      <h3 className="mb-1 text-xs uppercase tracking-wider text-slate-500">
                        {section.replace(/_/g, ' ')}
                      </h3>
                      {content && typeof content === 'object' ? (
                        <Facts data={content as Record<string, unknown>} />
                      ) : (
                        <p className="text-sm text-slate-300">{String(content)}</p>
                      )}
                    </div>
                  ))}
                </div>
                {postmortem.narrative ? (
                  <div className="mt-4 border-t border-slate-800 pt-3">
                    <h3 className="mb-1 text-xs uppercase tracking-wider text-slate-500">
                      Narrative {postmortem.narrative_provider ? `(${postmortem.narrative_provider})` : ''}
                    </h3>
                    <p className="whitespace-pre-line text-sm text-slate-300">
                      {postmortem.narrative}
                    </p>
                  </div>
                ) : postmortem.narrative_unavailable_reason ? (
                  <p className="mt-4 text-xs text-slate-500">
                    Narrative unavailable: {postmortem.narrative_unavailable_reason}
                  </p>
                ) : null}
                {postmortem.unknowns.length > 0 ? (
                  <div className="mt-4">
                    <h3 className="mb-1 text-xs uppercase tracking-wider text-slate-500">
                      Unknowns
                    </h3>
                    <ul className="space-y-1 text-xs text-slate-500">
                      {postmortem.unknowns.map((item) => (
                        <li key={item}>· {item}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}
                {postmortem.follow_up_actions.length > 0 ? (
                  <div className="mt-4">
                    <h3 className="mb-1 text-xs uppercase tracking-wider text-slate-500">
                      Follow-up actions
                    </h3>
                    <Table
                      headers={['Action', 'Kind', 'Priority']}
                      rows={postmortem.follow_up_actions.map((item) => [
                        String(item.title ?? '—'),
                        String(item.kind ?? '—'),
                        String(item.priority ?? '—'),
                      ])}
                    />
                  </div>
                ) : null}
                <p className="mt-4 text-xs text-slate-500">{postmortem.note}</p>
              </>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
