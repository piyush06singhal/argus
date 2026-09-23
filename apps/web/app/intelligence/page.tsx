import Link from 'next/link';

import {
  api,
  formatDate,
  type IntelligenceDashboard,
  type IntelligenceHealth,
  type IntelligenceMetrics,
  type Project,
} from '@/lib/api';
import {
  confidenceStyle,
  intelligenceHealthLabel,
  knowledgeSampleLabel,
  knowledgeStatusLabel,
  knowledgeStatusStyle,
  knowledgeTypeLabel,
  LEARNING_BOUNDARY,
  relationshipSummary,
  scopeLabel,
} from '@/lib/intelligence';

export const metadata = {
  title: 'Learning Center',
};

export const dynamic = 'force-dynamic';

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

function Tile({
  label,
  value,
  hint,
}: {
  label: string;
  value: string | number;
  hint?: string;
}) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/40 p-3">
      <div className="text-xs uppercase tracking-wider text-slate-500">{label}</div>
      <div className="mt-1 text-xl font-semibold text-slate-100">{value}</div>
      {hint ? <div className="mt-1 text-xs text-slate-500">{hint}</div> : null}
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-slate-400">{children}</p>;
}

function percent(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return '—';
  }
  return `${(value * 100).toFixed(0)}%`;
}

const SECTIONS: Array<{ href: string; title: string; body: string }> = [
  {
    href: '/intelligence/patterns',
    title: 'Pattern Explorer',
    body: 'Every learned pattern with its sample count, scope, coverage window and limitations — including the candidates nobody has reviewed yet.',
  },
  {
    href: '/intelligence/recommendations',
    title: 'Recommendation Center',
    body: 'Evidence-backed advice for current incidents, what Phase 9 would require before acting, and what happened after each decision.',
  },
  {
    href: '/intelligence/experiences',
    title: 'Reliability memory',
    body: 'The normalised episodes the learning pipeline reasons over: what failed, what was done, and what the outcome was.',
  },
  {
    href: '/intelligence/relationships',
    title: 'Learned relationships',
    body: 'What history observed travelling between components — kept separate from the structural dependency graph.',
  },
  {
    href: '/intelligence/learning-runs',
    title: 'Learning runs',
    body: 'The auditable record of each pipeline execution: inputs, algorithm versions, what was learned and what was rejected.',
  },
  {
    href: '/intelligence/search',
    title: 'Knowledge search',
    body: 'Ask what ARGUS has seen before. Answers cite stored rows and say so when there is no comparable history.',
  },
];

/**
 * The Reliability Intelligence Center (§52, §60, §80, §82).
 *
 * Three rules shape this page:
 *
 * 1. **The boundary is stated before the numbers.** `LEARNING_BOUNDARY` is
 *    rendered above everything else, because "ARGUS learned this" is easy to
 *    misread as "ARGUS can act on this".
 * 2. **Empty is distinguished from healthy.** Every tile has an explicit
 *    zero-state sentence, and a project with no history says it has no history
 *    rather than showing zeroes that look like a clean bill of health.
 * 3. **Rejected and stale are shown next to validated.** A learning system whose
 *    only visible output is what it believes cannot be audited.
 */
export default async function IntelligencePage({
  searchParams,
}: {
  searchParams: { project_id?: string };
}) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  let projects: Project[] = [];
  try {
    const response = await api.listProjects(1, 50);
    projects = response.items;
  } catch {
    projects = [];
  }
  const project = projects.find((item) => item.id === requested) ?? projects[0];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Learning Center</h1>
        <p className="mt-1 text-sm text-slate-400">
          ARGUS turns the incidents, remediations, verifications and forecasts it
          has already recorded into knowledge it can retrieve later. Nothing on
          this page executes anything.
        </p>
        <p className="mt-2 text-xs text-slate-500">{LEARNING_BOUNDARY}</p>
      </div>

      {projects.length === 0 ? (
        <Card title="No projects" subtitle="Learning is project-scoped">
          <Empty>
            No project could be loaded. Learned knowledge is scoped to a project,
            so there is nothing to show without one.
          </Empty>
        </Card>
      ) : (
        <Card title="Project scope" subtitle="Every read and write is scoped to a project">
          <div className="flex flex-wrap gap-2">
            {projects.map((item) => (
              <Link
                key={item.id}
                href={`/intelligence?project_id=${encodeURIComponent(item.id)}`}
                className={`badge ${
                  item.id === project?.id
                    ? 'bg-argus-accent/20 text-argus-accent'
                    : 'bg-slate-800 text-slate-300'
                }`}
              >
                {item.name}
              </Link>
            ))}
          </div>
        </Card>
      )}

      {project ? <Center projectId={project.id} projectName={project.name} /> : null}
    </div>
  );
}

async function Center({
  projectId,
  projectName,
}: {
  projectId: string;
  projectName: string;
}) {
  let health: IntelligenceHealth | null = null;
  let dashboard: IntelligenceDashboard | null = null;
  let metrics: IntelligenceMetrics | null = null;
  const failures: string[] = [];

  const [healthResult, dashboardResult, metricsResult] = await Promise.allSettled([
    api.intelligenceHealth(projectId),
    api.intelligenceDashboard(projectId),
    api.intelligenceMetrics(projectId),
  ]);
  if (healthResult.status === 'fulfilled') {
    health = healthResult.value;
  } else {
    failures.push(`health: ${readError(healthResult.reason)}`);
  }
  if (dashboardResult.status === 'fulfilled') {
    dashboard = dashboardResult.value;
  } else {
    failures.push(`dashboard: ${readError(dashboardResult.reason)}`);
  }
  if (metricsResult.status === 'fulfilled') {
    metrics = metricsResult.value;
  } else {
    failures.push(`metrics: ${readError(metricsResult.reason)}`);
  }

  const run = dashboard?.last_run ?? null;

  return (
    <div className="space-y-6">
      {failures.length > 0 ? (
        <section className="rounded-md border border-argus-warning/40 bg-argus-warning/10 p-3 text-sm text-argus-warning">
          Some learning data could not be loaded:{' '}
          {failures.map((failure) => (
            <code key={failure} className="mx-1 font-mono text-xs">
              {failure}
            </code>
          ))}
        </section>
      ) : null}

      <Card
        title="Learning state"
        subtitle={health ? intelligenceHealthLabel(health) : 'unavailable'}
      >
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Tile
            label="Active knowledge"
            value={dashboard?.active_knowledge ?? 0}
            hint="Patterns that may inform a recommendation"
          />
          <Tile
            label="Candidates"
            value={dashboard?.candidate_patterns ?? 0}
            hint="Waiting for validation or review"
          />
          <Tile
            label="Rejected"
            value={dashboard?.rejected_patterns ?? 0}
            hint="Human decisions preserved, not deleted"
          />
          <Tile
            label="Stale"
            value={dashboard?.stale_knowledge ?? 0}
            hint="No longer confirmed by new data"
          />
        </div>
        {dashboard ? (
          <p className="mt-3 text-xs text-slate-500">
            {dashboard.validated_knowledge + dashboard.active_knowledge === 0
              ? 'No pattern has been validated yet in this project. Everything below is either a candidate or absent.'
              : `${dashboard.stale_knowledge} of ${
                  dashboard.validated_knowledge +
                  dashboard.active_knowledge +
                  dashboard.stale_knowledge
                } live patterns have stopped being confirmed by new data.`}
          </p>
        ) : null}
      </Card>

      <Card
        title="Learning quality"
        subtitle="§80, §82 — the pipeline audited by its own output"
      >
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Tile label="Experiences" value={metrics?.experiences ?? 0} />
          <Tile
            label="Excluded as poor quality"
            value={metrics?.experiences_poor_quality ?? 0}
            hint="Recorded, never learned from"
          />
          <Tile
            label="Pattern validation rate"
            value={percent(metrics?.pattern_validation_rate)}
            hint="Validated against validated + rejected"
          />
          <Tile
            label="Recommendation success"
            value={percent(metrics?.recommendation_success_rate)}
            hint="Outcomes recorded after a decision"
          />
          <Tile label="Learning runs" value={metrics?.learning_runs ?? 0} />
          <Tile
            label="Failed runs"
            value={metrics?.learning_failures ?? 0}
            hint="Recorded with their error, not swallowed"
          />
          <Tile
            label="Events pending"
            value={metrics?.events_pending ?? 0}
            hint="Waiting to be consumed once"
          />
          <Tile
            label="Events processed"
            value={metrics?.events_total ?? 0}
            hint="Append-only inbox"
          />
        </div>
        <div className="mt-3">
          <p className="text-sm text-slate-400">
            {relationshipSummary({
              relationships_active: metrics?.relationships_active,
              relationships_stale: metrics?.relationships_stale,
              relationships_undirected: metrics?.relationships_undirected,
            })}
          </p>
          <Link
            href={`/intelligence/relationships?project_id=${encodeURIComponent(projectId)}`}
            className="mt-1 inline-block text-xs text-argus-accent"
          >
            View learned relationships →
          </Link>
        </div>
      </Card>

      <Card
        title="Last learning run"
        subtitle={run ? formatDate(run.started_at) : 'no run recorded'}
      >
        {run ? (
          <dl className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <div>
              <dt className="text-xs uppercase tracking-wider text-slate-500">Status</dt>
              <dd className="text-slate-200">{run.status}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wider text-slate-500">Trigger</dt>
              <dd className="text-slate-200">{run.trigger}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wider text-slate-500">
                Events consumed
              </dt>
              <dd className="text-slate-200">{run.events_processed}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wider text-slate-500">
                Patterns found
              </dt>
              <dd className="text-slate-200">
                {run.patterns_discovered} ({run.patterns_validated} validated,{' '}
                {run.patterns_rejected} rejected)
              </dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wider text-slate-500">
                Relationships
              </dt>
              <dd className="text-slate-200">
                {run.relationships_created ?? 0} created,{' '}
                {run.relationships_updated ?? 0} refreshed
              </dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wider text-slate-500">
                Latency
              </dt>
              <dd className="text-slate-200">
                Data cutoff {formatDate(run.data_cutoff)}
              </dd>
            </div>
            {run.error_summary ? (
              <div className="col-span-2 md:col-span-4">
                <dt className="text-xs uppercase tracking-wider text-slate-500">
                  Error
                </dt>
                <dd className="text-argus-error">{run.error_summary}</dd>
              </div>
            ) : null}
          </dl>
        ) : (
          <Empty>
            No learning run has executed for {projectName}. Knowledge appears only
            after a run consumes completed outcomes — an empty center means nothing
            has happened yet, not that the system is healthy.
          </Empty>
        )}
      </Card>

      <Card
        title="Recently learned"
        subtitle="Newest patterns, with the evidence that qualifies them"
      >
        {dashboard && dashboard.recently_learned.length > 0 ? (
          <ul className="space-y-3">
            {dashboard.recently_learned.map((item) => (
              <li
                key={item.id}
                className="rounded-md border border-slate-800 bg-slate-900/40 p-3"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Link
                    href={`/intelligence/patterns/${item.id}?project_id=${encodeURIComponent(projectId)}`}
                    className="font-medium text-slate-100 hover:text-argus-accent"
                  >
                    {item.title}
                  </Link>
                  <span className={`badge ${knowledgeStatusStyle(item.status)}`}>
                    {knowledgeStatusLabel(item.status)}
                  </span>
                  <span className="badge bg-slate-800 text-slate-400">
                    {knowledgeTypeLabel(item.knowledge_type)}
                  </span>
                  <span className={`badge ${confidenceStyle(item.confidence)}`}>
                    {item.confidence}
                  </span>
                </div>
                <p className="mt-1 text-xs text-slate-500">
                  {knowledgeSampleLabel(item)} ·{' '}
                  {scopeLabel(item.scope)} · observed {formatDate(item.coverage_start)}{' '}
                  → {formatDate(item.coverage_end)}
                </p>
              </li>
            ))}
          </ul>
        ) : (
          <Empty>
            Nothing has been learned yet from {projectName}&#39;s history. That is a
            statement about the data, not about the software.
          </Empty>
        )}
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        {SECTIONS.map((section) => (
          <Link
            key={section.href}
            href={`${section.href}?project_id=${encodeURIComponent(projectId)}`}
            className="card transition-colors hover:border-argus-accent/40"
          >
            <h2 className="font-medium text-slate-200">{section.title}</h2>
            <p className="mt-1 text-sm text-slate-400">{section.body}</p>
          </Link>
        ))}
      </div>

      {dashboard && dashboard.chronic_components > 0 ? (
        <Card
          title="Chronic components"
          subtitle="Flagged for investigation, never modified automatically"
        >
          <p className="text-sm text-slate-400">
            {dashboard.chronic_components} component
            {dashboard.chronic_components === 1 ? '' : 's'} in this project meet the
            chronic-reliability thresholds. ARGUS raises a signal; it does not
            disable, restart or reconfigure anything on the strength of it.
          </p>
        </Card>
      ) : null}
    </div>
  );
}

function readError(reason: unknown): string {
  if (reason instanceof Error) {
    return reason.message;
  }
  return String(reason);
}
