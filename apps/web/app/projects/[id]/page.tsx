import Link from 'next/link';
import {
  api,
  formatDate,
  type Component,
  type ComponentHealth,
  type Dependency,
  type DependencyStatus,
  type Deployment,
  type Environment,
  type Incident,
} from '@/lib/api';

const HEALTH_STYLES: Record<ComponentHealth, string> = {
  healthy: 'bg-argus-success/10 text-argus-success',
  degraded: 'bg-argus-warning/10 text-argus-warning',
  unhealthy: 'bg-argus-error/10 text-argus-error',
  unknown: 'bg-slate-700/40 text-slate-400',
};

const DEP_STATUS_STYLES: Record<DependencyStatus, string> = {
  operational: 'bg-argus-success/10 text-argus-success',
  partial: 'bg-argus-warning/10 text-argus-warning',
  outage: 'bg-argus-error/10 text-argus-error',
  unknown: 'bg-slate-700/40 text-slate-400',
};

export const metadata = {
  title: 'Project Detail',
};

export default async function ProjectDetailPage({
  params,
}: {
  params: { id: string };
}) {
  const { id } = params;

  try {
    const [project, environments, components, dependencies, incidents, deployments] =
      await Promise.all([
        api.getProject(id),
        api.listEnvironments(id),
        api.listComponents(id),
        api.listDependencies(id),
        api.listIncidents({ page: 1, pageSize: 100 }),
        api.listDeployments(1, 100),
      ]);

    const projectIncidents = incidents.items.filter(
      (i: Incident) => i.project_id === id
    );
    const projectDeployments = deployments.items.filter(
      (d: Deployment) => d.project_id === id
    );

    return (
      <div className="space-y-6">
        <div>
          <Link
            href="/projects"
            className="text-sm text-slate-400 hover:text-argus-accent"
          >
            ← Back to projects
          </Link>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-semibold text-slate-100">
              {project.name}
            </h1>
            <span className="badge bg-argus-accent/10 text-argus-accent">
              {project.status}
            </span>
          </div>
          <dl className="mt-3 grid max-w-2xl grid-cols-1 gap-x-8 gap-y-2 text-sm sm:grid-cols-2">
            <div className="flex gap-2">
              <dt className="text-slate-500">Slug</dt>
              <dd className="font-mono text-slate-300">{project.slug}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="text-slate-500">ID</dt>
              <dd className="font-mono text-xs leading-5 text-slate-300">
                {project.id}
              </dd>
            </div>
            <div className="flex gap-2">
              <dt className="text-slate-500">Created</dt>
              <dd className="text-slate-300">
                {formatDate(project.created_at)}
              </dd>
            </div>
            <div className="flex gap-2">
              <dt className="text-slate-500">Updated</dt>
              <dd className="text-slate-300">
                {formatDate(project.updated_at)}
              </dd>
            </div>
          </dl>
        </div>

        {project.description ? (
          <div className="card">
            <h2 className="mb-2 font-medium text-slate-200">Description</h2>
            <p className="text-sm text-slate-400">{project.description}</p>
          </div>
        ) : null}

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <SectionCard title="Environments" count={environments.total}>
            {environments.items.length === 0 ? (
              <EmptyNote text="No environments configured for this project." />
            ) : (
              <ul className="divide-y divide-slate-800">
                {environments.items.map((env: Environment) => (
                  <li key={env.id} className="py-3">
                    <p className="text-sm font-medium text-slate-200">
                      {env.name}
                    </p>
                    <p className="mt-0.5 text-xs text-slate-500">
                      {env.environment_type ?? 'environment'}
                      {env.tags && Object.keys(env.tags).length > 0
                        ? ` · ${Object.entries(env.tags)
                            .map(([k, v]) => `${k}=${v}`)
                            .join(', ')}`
                        : ''}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </SectionCard>

          <SectionCard title="Components" count={components.total}>
            {components.items.length === 0 ? (
              <EmptyNote text="No components registered for this project." />
            ) : (
              <ul className="divide-y divide-slate-800">
                {components.items.map((component: Component) => (
                  <li key={component.id} className="py-3">
                    <div className="flex items-center justify-between gap-3">
                      <p className="text-sm font-medium text-slate-200">
                        {component.name}
                      </p>
                      <span
                        className={`badge ${
                          HEALTH_STYLES[component.health ?? 'unknown'] ??
                          HEALTH_STYLES.unknown
                        }`}
                      >
                        {component.health ?? 'unknown'}
                      </span>
                    </div>
                    <p className="mt-0.5 text-xs text-slate-500">
                      {component.type ?? 'component'}
                      {component.version ? ` · v${component.version}` : ''}
                      {component.endpoint ? ` · ${component.endpoint}` : ''}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </SectionCard>

          <SectionCard title="Dependencies" count={dependencies.total}>
            {dependencies.items.length === 0 ? (
              <EmptyNote text="No dependencies discovered for this project." />
            ) : (
              <ul className="divide-y divide-slate-800">
                {dependencies.items.map((dep: Dependency) => (
                  <li key={dep.id} className="py-3">
                    <div className="flex items-center justify-between gap-3">
                      <p className="font-mono text-xs text-slate-300">
                        {dep.source_component_id}{' '}
                        <span className="text-slate-500">
                          → {dep.dependency_type} →
                        </span>{' '}
                        {dep.target_component_id}
                      </p>
                      <span
                        className={`badge ${
                          DEP_STATUS_STYLES[dep.status] ??
                          DEP_STATUS_STYLES.unknown
                        }`}
                      >
                        {dep.status}
                      </span>
                    </div>
                    <p className="mt-0.5 text-xs text-slate-500">
                      {dep.latency_ms != null
                        ? `${dep.latency_ms} ms latency · `
                        : ''}
                      {dep.error_rate != null
                        ? `${(dep.error_rate * 100).toFixed(2)}% error rate · `
                        : ''}
                      discovered {formatDate(dep.discovered_at)}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </SectionCard>

          <SectionCard title="Recent incidents">
            {projectIncidents.length === 0 ? (
              <EmptyNote text="No incidents associated with this project." />
            ) : (
              <ul className="divide-y divide-slate-800">
                {projectIncidents.slice(0, 5).map((incident: Incident) => (
                  <li key={incident.id} className="py-3">
                    <Link
                      href={`/incidents/${incident.id}`}
                      className="block hover:text-argus-accent"
                    >
                      <p className="text-sm font-medium text-slate-200">
                        {incident.title}
                      </p>
                      <p className="mt-0.5 text-xs text-slate-500">
                        {incident.severity} · {incident.status} ·{' '}
                        {formatDate(incident.detected_at)}
                      </p>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </SectionCard>
        </div>

        <div className="card">
          <h2 className="mb-3 font-medium text-slate-200">Recent deployments</h2>
          {projectDeployments.length === 0 ? (
            <EmptyNote text="No deployments associated with this project." />
          ) : (
            <ul className="divide-y divide-slate-800">
              {projectDeployments.slice(0, 5).map((deployment: Deployment) => (
                <li key={deployment.id} className="py-3">
                  <p className="text-sm font-medium text-slate-200">
                    {deployment.component_name ?? deployment.component_id}{' '}
                    <span className="text-slate-500">→</span>{' '}
                    {deployment.environment_name ?? deployment.environment_id}
                  </p>
                  <p className="mt-0.5 text-xs text-slate-500">
                    v{deployment.version}
                    {deployment.commit
                      ? ` · ${deployment.commit.slice(0, 7)}`
                      : ''}{' '}
                    · {deployment.status} · {formatDate(deployment.deployed_at)}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    );
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Unknown error occurred';
    return (
      <div className="space-y-6">
        <Link
          href="/projects"
          className="text-sm text-slate-400 hover:text-argus-accent"
        >
          ← Back to projects
        </Link>
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load project
          </h2>
          <p className="mt-2 text-sm text-slate-400">
            Could not load project {id}. ({message})
          </p>
        </div>
      </div>
    );
  }
}

function SectionCard({
  title,
  count,
  children,
}: {
  title: string;
  count?: number;
  children: React.ReactNode;
}) {
  return (
    <div className="card">
      <h2 className="mb-3 font-medium text-slate-200">
        {title}
        {count !== undefined ? (
          <span className="ml-2 text-xs font-normal text-slate-500">
            {count}
          </span>
        ) : null}
      </h2>
      {children}
    </div>
  );
}

function EmptyNote({ text }: { text: string }) {
  return <p className="text-sm text-slate-400">{text}</p>;
}