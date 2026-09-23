import Link from 'next/link';

import {
  api,
  formatDate,
  type OverviewResponse,
  type PlatformHealthResponse,
  type PlatformProjectCard,
  type Project,
} from '@/lib/api';
import {
  caseStatusLabel,
  caseStatusStyle,
  componentStateLabel,
  componentStateStyle,
  componentStateHint,
  openCasesLabel,
  PLATFORM_BOUNDARY,
} from '@/lib/platform';
import {
  Card,
  Empty,
  Facts,
  Limitations,
  ProjectScope,
  Table,
  Tile,
  readError,
  scalar,
} from './ui';

export const metadata = {
  title: 'Platform Overview',
};

export const dynamic = 'force-dynamic';

type SearchParams = { project_id?: string };

/**
 * The unified reliability dashboard (§19–§25, §52, §116).
 *
 * The rule that shapes this page: **ARGUS reports its own state with the same
 * honesty it asks of the services it watches.** The executive summary is built
 * by the backend (§23), so it cannot disagree with the page beneath it; the
 * platform's own health is rendered beside the observed system's, because a
 * dashboard that looks authoritative while its own learning subsystem is down
 * is worse than one that says so.
 */
export default async function PlatformOverviewPage({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  let projects: Project[] = [];
  try {
    projects = (await api.listProjects(1, 50)).items;
  } catch {
    projects = [];
  }
  const project = projects.find((item) => item.id === requested) ?? projects[0];

  let health: PlatformHealthResponse | null = null;
  let cards: PlatformProjectCard[] = [];
  const [healthResult, cardsResult] = await Promise.allSettled([
    api.platformHealth(),
    api.platformProjects(),
  ]);
  if (healthResult.status === 'fulfilled') {
    health = healthResult.value;
  }
  if (cardsResult.status === 'fulfilled') {
    cards = cardsResult.value;
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">
          Unified Reliability Platform
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          One control plane over what ARGUS observed, concluded and recorded
          across every phase. Nothing here executes remediation or modifies
          source.
        </p>
        <p className="mt-2 text-xs text-slate-500">{PLATFORM_BOUNDARY}</p>
      </div>

      {health ? (
        <Card
          title="ARGUS self-monitoring"
          subtitle={`As of ${formatDate(health.as_of)}`}
          action={
            <Link href="/platform/health" className="text-xs text-argus-accent">
              Platform health →
            </Link>
          }
        >
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Tile
              label="Platform"
              value={health.status}
              hint={health.ready ? 'Ready to serve' : 'Not ready — see readiness'}
            />
            <Tile
              label="Degraded capabilities"
              value={health.degraded_capabilities.length}
              hint={
                health.degraded_capabilities.length === 0
                  ? 'No capability is degraded'
                  : health.degraded_capabilities.join(', ')
              }
            />
            <Tile
              label="Subsystems"
              value={health.subsystems.length}
              hint="Required and optional together"
            />
            <Tile
              label="Ready"
              value={health.ready ? 'Yes' : 'No'}
              hint="Required subsystems only"
            />
          </div>
          <Limitations items={health.notes} />
        </Card>
      ) : null}

      {cards.length > 0 ? (
        <Card
          title="Projects"
          subtitle="Every read on this surface is project-scoped (§42)"
        >
          <Table
            headers={['Project', 'Status', 'Open cases', 'Data-quality issues']}
            rows={cards.map((card) => [
              <Link
                key={card.project_id}
                href={`/platform?project_id=${encodeURIComponent(card.project_id)}`}
                className="text-argus-accent"
              >
                {card.name}
              </Link>,
              card.status,
              openCasesLabel(card.open_cases),
              String(card.open_data_quality_issues),
            ])}
          />
        </Card>
      ) : null}

      <ProjectScope projects={projects} activeId={project?.id} basePath="/platform" />

      {project ? (
        <Dashboard projectId={project.id} projectName={project.name} />
      ) : (
        <Card title="No projects">
          <Empty>
            No project could be loaded. Every platform read is scoped to a
            project, so there is nothing to summarise without one.
          </Empty>
        </Card>
      )}
    </div>
  );
}

async function Dashboard({
  projectId,
  projectName,
}: {
  projectId: string;
  projectName: string;
}) {
  let overview: OverviewResponse | null = null;
  let error: string | null = null;
  try {
    overview = await api.platformOverview(projectId);
  } catch (reason) {
    error = readError(reason);
  }

  if (error || !overview) {
    return (
      <Card title="Dashboard unavailable" subtitle={projectName}>
        <Empty>{error ?? 'The overview could not be built.'}</Empty>
      </Card>
    );
  }

  const summary = overview.executive_summary;
  const headline = typeof summary.headline === 'string' ? summary.headline : null;
  const openCases = Array.isArray(overview.open_cases) ? overview.open_cases : [];
  const risks = Array.isArray(overview.predicted_risks) ? overview.predicted_risks : [];
  const changes = Array.isArray(overview.recent_changes) ? overview.recent_changes : [];
  const risky = Array.isArray(overview.top_risky_components)
    ? overview.top_risky_components
    : [];
  const recoveries = Array.isArray(overview.recent_recoveries)
    ? overview.recent_recoveries
    : [];
  const incidents = Array.isArray(overview.active_incidents)
    ? overview.active_incidents
    : [];
  const remediations = Array.isArray(overview.active_remediations)
    ? overview.active_remediations
    : [];

  const slo = overview.slo ?? {};
  const sloObjectives = typeof slo.objectives === 'number' ? slo.objectives : null;
  const sloByStatus =
    slo.by_status && typeof slo.by_status === 'object'
      ? (slo.by_status as Record<string, number>)
      : {};

  return (
    <>
      <Card title="Executive summary" subtitle={`${projectName} · ${formatDate(overview.as_of)}`}>
        {headline ? (
          <p className="text-sm text-slate-200">{headline}</p>
        ) : (
          <Empty>No summary was produced.</Empty>
        )}
        <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Tile
            label="Components monitored"
            value={scalarNumber(summary.components_monitored)}
            hint={`${scalarNumber(summary.components_with_evidence)} with evidence`}
          />
          <Tile
            label="Active incidents"
            value={scalarNumber(summary.active_incidents)}
            hint="Open right now"
          />
          <Tile
            label="Elevated predicted risk"
            value={scalarNumber(summary.components_at_elevated_risk)}
            hint="HIGH or CRITICAL forecasts"
          />
          <Tile
            label="Remediations executing"
            value={scalarNumber(summary.remediations_executing)}
            hint="In flight under Phase 9 policy"
          />
        </div>
        <div className="mt-4">
          <Facts data={overview.health} empty="No health summary." />
        </div>
        <Limitations items={overview.limitations} />
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card
          title="Open reliability cases"
          subtitle={openCasesLabel(openCases.length)}
          action={
            <Link href={`/platform/cases?project_id=${projectId}`} className="text-xs text-argus-accent">
              All cases →
            </Link>
          }
        >
          {openCases.length === 0 ? (
            <Empty>
              No reliability case is open. A case is opened by the backend when a
              situation warrants tracked investigation.
            </Empty>
          ) : (
            <Table
              headers={['Case', 'Status', 'Severity', 'Opened']}
              rows={openCases.map((item) => {
                const id = scalar(item, 'id');
                const status = scalar(item, 'status');
                return [
                  id === '—' ? (
                    scalar(item, 'title')
                  ) : (
                    <Link
                      key={id}
                      href={`/platform/cases/${encodeURIComponent(id)}?project_id=${projectId}`}
                      className="text-argus-accent"
                    >
                      {scalar(item, 'title') || id}
                    </Link>
                  ),
                  <span key="status" className={`badge ${caseStatusStyle(status)}`}>
                    {caseStatusLabel(status)}
                  </span>,
                  scalar(item, 'severity'),
                  formatDate(scalarOrNull(item, 'opened_at')),
                ];
              })}
            />
          )}
        </Card>

        <Card
          title="Components by state"
          subtitle="Worst-first precedence (§4) — UNKNOWN is not healthy"
        >
          <Table
            headers={['State', 'Count', 'Means']}
            rows={Object.entries(overview.state_counts)
              .sort((a, b) => b[1] - a[1])
              .map(([state, count]) => [
                <span key="s" className={`badge ${componentStateStyle(state)}`}>
                  {componentStateLabel(state)}
                </span>,
                String(count),
                <span key="h" className="text-xs text-slate-500">
                  {componentStateHint(state)}
                </span>,
              ])}
            empty="No component states were computed."
          />
        </Card>
      </div>

      <Card
        title="Top components needing attention"
        subtitle="Predicted risk first, then live state"
      >
        {risky.length === 0 ? (
          <Empty>
            No component is currently degraded, in incident, or at elevated
            predicted risk.
          </Empty>
        ) : (
          <Table
            headers={['Component', 'State', 'Predicted risk']}
            rows={risky.map((item) => {
              const state = scalar(item, 'state');
              return [
                <Link
                  key="c"
                  href={`/platform/services/${encodeURIComponent(
                    scalar(item, 'component_id')
                  )}?project_id=${projectId}`}
                  className="text-argus-accent"
                >
                  {scalar(item, 'name')}
                </Link>,
                <span key="s" className={`badge ${componentStateStyle(state)}`}>
                  {componentStateLabel(state)}
                </span>,
                scalar(item, 'predicted_risk') === '—' ? '—' : scalar(item, 'predicted_risk'),
              ];
            })}
          />
        )}
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Predicted risks" subtitle="§36 — a band, not a fact">
          {risks.length === 0 ? (
            <Empty>No forecast raises risk in the current window.</Empty>
          ) : (
            <Table
              headers={['Component', 'Risk', 'Window']}
              rows={risks.map((item) => [
                scalar(item, 'component_name') !== '—'
                  ? scalar(item, 'component_name')
                  : scalar(item, 'component_id'),
                scalar(item, 'risk_level'),
                scalar(item, 'window_hours') !== '—'
                  ? `${scalar(item, 'window_hours')}h`
                  : '—',
              ])}
            />
          )}
        </Card>

        <Card title="SLO &amp; error budget" subtitle="§32–§35">
          {sloObjectives === null ? (
            <Empty>No SLO summary was produced for this project.</Empty>
          ) : sloObjectives === 0 ? (
            <Empty>
              No service-level objective is defined. ARGUS does not invent an
              objective it was never given.
            </Empty>
          ) : (
            <>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Tile label="Objectives" value={sloObjectives} />
                {Object.entries(sloByStatus).map(([status, count]) => (
                  <Tile key={status} label={status} value={count} />
                ))}
              </div>
              <div className="mt-3">
                <Facts data={slo} />
              </div>
            </>
          )}
        </Card>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card
          title="Recent changes"
          subtitle="Correlated with incidents, never proven to cause them (§38, §39)"
        >
          {changes.length === 0 ? (
            <Empty>No change was recorded in the window.</Empty>
          ) : (
            <Table
              headers={['Change', 'Type', 'When']}
              rows={changes.slice(0, 10).map((item) => [
                scalar(item, 'title') !== '—'
                  ? scalar(item, 'title')
                  : scalar(item, 'summary'),
                scalar(item, 'change_type'),
                formatDate(scalarOrNull(item, 'occurred_at')),
              ])}
            />
          )}
        </Card>

        <Card title="Recent recoveries" subtitle="Resolved in the last 7 days">
          {recoveries.length === 0 ? (
            <Empty>No incident has resolved in the last seven days.</Empty>
          ) : (
            <Table
              headers={['Incident', 'Severity', 'Resolved']}
              rows={recoveries.map((item) => [
                scalar(item, 'title'),
                scalar(item, 'severity'),
                formatDate(scalarOrNull(item, 'resolved_at')),
              ])}
            />
          )}
        </Card>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Active incidents" subtitle="Open right now">
          {incidents.length === 0 ? (
            <Empty>No incident is open.</Empty>
          ) : (
            <Table
              headers={['Incident', 'Status', 'Severity']}
              rows={incidents.map((item) => [
                scalar(item, 'title'),
                scalar(item, 'status'),
                scalar(item, 'severity'),
              ])}
            />
          )}
        </Card>

        <Card title="Active remediations" subtitle="§9 — policy-controlled actions in flight">
          {remediations.length === 0 ? (
            <Empty>No remediation action is executing.</Empty>
          ) : (
            <Table
              headers={['Action', 'State', 'Blast radius']}
              rows={remediations.map((item) => [
                scalar(item, 'action_type'),
                scalar(item, 'state'),
                scalar(item, 'blast_radius') !== '—'
                  ? scalar(item, 'blast_radius')
                  : scalar(item, 'blast_radius_level'),
              ])}
            />
          )}
        </Card>
      </div>

      <Card title="Data quality &amp; learning" subtitle="§52, §87–§90">
        <div className="grid gap-4 lg:grid-cols-2">
          <div>
            <h3 className="mb-2 text-xs uppercase tracking-wider text-slate-500">
              Data quality
            </h3>
            <Facts data={overview.data_quality} empty="No data-quality summary." />
          </div>
          <div>
            <h3 className="mb-2 text-xs uppercase tracking-wider text-slate-500">
              Learning insights
            </h3>
            <Facts data={overview.learning_insights} empty="No learning summary." />
          </div>
        </div>
        <div className="mt-4">
          <h3 className="mb-2 text-xs uppercase tracking-wider text-slate-500">
            ARGUS&apos;s own health
          </h3>
          <Facts data={overview.argus_health} empty="No self-monitoring summary." />
        </div>
      </Card>
    </>
  );
}

function scalarNumber(value: unknown): number | string {
  if (typeof value === 'number') {
    return value;
  }
  if (typeof value === 'string') {
    return value;
  }
  return '—';
}

function scalarOrNull(data: Record<string, unknown> | null, key: string): string | null {
  if (!data) {
    return null;
  }
  const value = data[key];
  return typeof value === 'string' ? value : null;
}
