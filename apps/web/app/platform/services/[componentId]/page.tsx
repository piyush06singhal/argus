import Link from 'next/link';

import { api, formatDate, type CatalogEntryResponse, type Project } from '@/lib/api';
import {
  componentStateHint,
  componentStateLabel,
  componentStateStyle,
  dataQualityKindLabel,
  formatRatio,
  PLATFORM_BOUNDARY,
  UNKNOWN_OWNERSHIP,
} from '@/lib/platform';
import { Card, Empty, Facts, Limitations, Table, readError } from '../../ui';
import OwnershipForm from './OwnershipForm';

export const metadata = {
  title: 'Service',
};

export const dynamic = 'force-dynamic';

/**
 * A single service's operational profile (§30, §31, §75).
 *
 * The rule: **the scorecard is shown dimension by dimension.** §75 forbids
 * collapsing reliability into an undocumented single number, so this page
 * renders the explicit dimensions the backend computed and names the ones that
 * could not be computed. An unavailable section is shown as unavailable, not
 * omitted — a service that looks complete because its telemetry is missing is
 * the exact failure a catalog exists to prevent.
 */
export default async function ServiceDetailPage({
  params,
  searchParams,
}: {
  params: { componentId: string };
  searchParams: { project_id?: string };
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

  if (!project) {
    return (
      <Card title="No projects">
        <Empty>The catalog is project-scoped.</Empty>
      </Card>
    );
  }

  let entry: CatalogEntryResponse | null = null;
  let error: string | null = null;
  try {
    entry = await api.platformService(params.componentId, project.id);
  } catch (reason) {
    error = readError(reason);
  }

  if (error || !entry) {
    return (
      <Card title="Service unavailable">
        <Empty>{error ?? 'The service could not be loaded.'}</Empty>
        <Link href={`/platform/services?project_id=${project.id}`} className="mt-3 inline-block text-xs text-argus-accent">
          ← Back to catalog
        </Link>
      </Card>
    );
  }

  const owner = (entry.owner ?? {}) as Record<string, unknown>;
  const unavailable = Object.entries(entry.unavailable ?? {});
  const scorecard = (entry.scorecard ?? {}) as Record<string, unknown>;

  return (
    <div className="space-y-6">
      <div>
        <Link
          href={`/platform/services?project_id=${project.id}`}
          className="text-xs text-argus-accent"
        >
          ← Service catalog
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold text-slate-100">{entry.name}</h1>
          <span className={`badge ${componentStateStyle(entry.state)}`}>
            {componentStateLabel(entry.state)}
          </span>
          <span className="badge bg-slate-800 text-slate-300">{entry.component_type}</span>
        </div>
        <p className="mt-1 text-xs text-slate-500">{componentStateHint(entry.state)}</p>
        {entry.state_reason ? (
          <p className="mt-1 text-sm text-slate-400">{entry.state_reason}</p>
        ) : null}
        <p className="mt-2 text-xs text-slate-500">{PLATFORM_BOUNDARY}</p>
      </div>

      {unavailable.length > 0 ? (
        <Card
          title="Sections that could not be computed"
          subtitle="Shown, not hidden — absence is information"
        >
          <ul className="space-y-1 text-sm text-slate-400">
            {unavailable.map(([section, reason]) => (
              <li key={section}>
                <span className="text-slate-300">{section}</span>: {reason}
              </li>
            ))}
          </ul>
        </Card>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Ownership" subtitle="§31 — never inferred">
          {Object.keys(owner).length === 0 ? (
            <Empty>
              Ownership has never been recorded for this service. ARGUS does not
              invent an owner from a naming convention.
            </Empty>
          ) : (
            <Facts data={owner} />
          )}
          <OwnershipForm
            componentId={entry.component_id}
            projectId={project.id}
            current={owner}
            unknownLabel={UNKNOWN_OWNERSHIP}
          />
        </Card>

        <Card title="Metrics" subtitle="From stored telemetry in the window">
          <Facts data={entry.metrics} empty="No metric is available for this service." />
        </Card>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Risk" subtitle="§36 — a band, not a fact">
          <Facts data={entry.risk} empty="No forecast covers this service." />
        </Card>

        <Card title="Reliability scorecard" subtitle="§75 — explicit dimensions, never one score">
          <Facts
            data={scorecard}
            empty="No scorecard could be computed from the available evidence."
          />
        </Card>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Dependencies" subtitle="What this service calls">
          {entry.dependencies.length === 0 ? (
            <Empty>No outgoing dependency is recorded.</Empty>
          ) : (
            <Table
              headers={['Target', 'Type']}
              rows={entry.dependencies.map((item) => [
                String(item.target_name ?? item.target_component_id ?? '—'),
                String(item.dependency_type ?? '—'),
              ])}
            />
          )}
        </Card>

        <Card title="Dependents" subtitle="What calls this service">
          {entry.dependents.length === 0 ? (
            <Empty>No incoming dependency is recorded.</Empty>
          ) : (
            <Table
              headers={['Source', 'Type']}
              rows={entry.dependents.map((item) => [
                String(item.source_name ?? item.source_component_id ?? '—'),
                String(item.dependency_type ?? '—'),
              ])}
            />
          )}
        </Card>
      </div>

      <Card title="Incident history" subtitle="Incidents naming this service">
        {entry.incident_history.length === 0 ? (
          <Empty>No incident has named this service.</Empty>
        ) : (
          <Table
            headers={['Incident', 'Status', 'Severity', 'Opened']}
            rows={entry.incident_history.map((item) => [
              String(item.title ?? item.id ?? '—'),
              String(item.status ?? '—'),
              String(item.severity ?? '—'),
              formatDate(typeof item.opened_at === 'string' ? item.opened_at : null),
            ])}
          />
        )}
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Deployment history">
          {entry.deployment_history.length === 0 ? (
            <Empty>No deployment is recorded for this service.</Empty>
          ) : (
            <Table
              headers={['Version', 'Environment', 'Deployed']}
              rows={entry.deployment_history.map((item) => [
                String(item.version ?? '—'),
                String(item.environment ?? '—'),
                formatDate(typeof item.deployed_at === 'string' ? item.deployed_at : null),
              ])}
            />
          )}
        </Card>

        <Card title="Remediation history" subtitle="§9 — recorded, policy-checked actions">
          {entry.remediation_history.length === 0 ? (
            <Empty>No remediation action has targeted this service.</Empty>
          ) : (
            <Table
              headers={['Action', 'State', 'Outcome', 'When']}
              rows={entry.remediation_history.map((item) => [
                String(item.action_type ?? '—'),
                String(item.state ?? '—'),
                String(item.outcome ?? '—'),
                formatDate(typeof item.occurred_at === 'string' ? item.occurred_at : null),
              ])}
            />
          )}
        </Card>
      </div>

      <Limitations items={entry.limitations} />
    </div>
  );
}
