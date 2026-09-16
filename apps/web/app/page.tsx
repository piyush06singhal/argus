import Link from 'next/link';
import {
  api,
  type Deployment,
  type Incident,
  type ObservabilityEvent,
} from '@/lib/api';

interface StatCardProps {
  label: string;
  value: number;
  href: string;
  accent: string;
}

function StatCard({ label, value, href, accent }: StatCardProps) {
  return (
    <Link
      href={href}
      className="card block transition-colors hover:border-slate-700 hover:bg-slate-800/50"
    >
      <div className={`text-3xl font-semibold ${accent}`}>{value}</div>
      <div className="mt-1 text-sm text-slate-400">{label}</div>
    </Link>
  );
}

const ACTIVE_INCIDENT_STATUSES = ['detected', 'acknowledged', 'in_progress'];

export const metadata = {
  title: 'Dashboard',
};

export default async function DashboardPage() {
  try {
    const [projects, incidents, deployments, events] = await Promise.all([
      api.listProjects(1, 100),
      api.listIncidents({ page: 1, pageSize: 100 }),
      api.listDeployments(1, 100),
      api.listEvents({ page: 1, pageSize: 1 }),
    ]);

    const activeIncidents = incidents.items.filter((i: Incident) =>
      ACTIVE_INCIDENT_STATUSES.includes(i.status)
    ).length;

    const empty =
      projects.total === 0 &&
      incidents.total === 0 &&
      deployments.total === 0 &&
      events.total === 0;

    if (empty) {
      return (
        <div className="space-y-6">
          <div>
            <h1 className="text-2xl font-semibold text-slate-100">Dashboard</h1>
            <p className="mt-1 text-sm text-slate-400">
              ARGUS Intelligence overview
            </p>
          </div>
          <div className="card flex flex-col items-center justify-center py-16 text-center">
            <div className="text-4xl">📡</div>
            <h2 className="mt-4 text-lg font-medium text-slate-200">
              No data yet
            </h2>
            <p className="mt-2 max-w-md text-sm text-slate-400">
              There are no projects, incidents, deployments, or observability
              events in the system yet. Once the backend starts receiving data,
              it will appear here.
            </p>
          </div>
        </div>
      );
    }

    const recentIncidents = incidents.items.slice(0, 5);
    const recentDeployments = deployments.items.slice(0, 5);
    const recentEvents = events.items.slice(0, 5);

    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Dashboard</h1>
          <p className="mt-1 text-sm text-slate-400">
            ARGUS Intelligence overview
          </p>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <StatCard
            label="Projects"
            value={projects.total}
            href="/projects"
            accent="text-argus-accent"
          />
          <StatCard
            label="Active incidents"
            value={activeIncidents}
            href="/incidents"
            accent="text-argus-error"
          />
          <StatCard
            label="Recent deployments"
            value={deployments.total}
            href="/deployments"
            accent="text-argus-success"
          />
          <StatCard
            label="Observability events"
            value={events.total}
            href="/observability/logs"
            accent="text-argus-info"
          />
        </div>

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <section className="card">
            <div className="flex items-center justify-between">
              <h2 className="font-medium text-slate-200">Recent incidents</h2>
              <Link
                href="/incidents"
                className="text-sm text-argus-accent hover:text-argus-accent-hover"
              >
                View all
              </Link>
            </div>
            {recentIncidents.length === 0 ? (
              <p className="mt-4 text-sm text-slate-400">
                No incidents recorded.
              </p>
            ) : (
              <ul className="mt-4 divide-y divide-slate-800">
                {recentIncidents.map((incident: Incident) => (
                  <li key={incident.id} className="py-3">
                    <Link
                      href={`/incidents/${incident.id}`}
                      className="block hover:text-argus-accent"
                    >
                      <p className="text-sm font-medium text-slate-200">
                        {incident.title}
                      </p>
                      <p className="mt-0.5 text-xs text-slate-500">
                        {incident.severity} · {incident.status}
                      </p>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="card">
            <div className="flex items-center justify-between">
              <h2 className="font-medium text-slate-200">Recent deployments</h2>
              <Link
                href="/deployments"
                className="text-sm text-argus-accent hover:text-argus-accent-hover"
              >
                View all
              </Link>
            </div>
            {recentDeployments.length === 0 ? (
              <p className="mt-4 text-sm text-slate-400">
                No deployments recorded.
              </p>
            ) : (
              <ul className="mt-4 divide-y divide-slate-800">
                {recentDeployments.map((deployment: Deployment) => (
                  <li key={deployment.id} className="py-3">
                    <p className="text-sm font-medium text-slate-200">
                      {deployment.component_name ?? deployment.component_id}{' '}
                      <span className="text-slate-500">→</span>{' '}
                      {deployment.environment_name ?? deployment.environment_id}
                    </p>
                    <p className="mt-0.5 text-xs text-slate-500">
                      v{deployment.version}
                      {deployment.commit ? ` · ${deployment.commit.slice(0, 7)}` : ''}{' '}
                      · {deployment.status}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>

        <section className="card">
          <div className="flex items-center justify-between">
            <h2 className="font-medium text-slate-200">Latest activity</h2>
            <Link
              href="/observability/logs"
              className="text-sm text-argus-accent hover:text-argus-accent-hover"
            >
              Observability
            </Link>
          </div>
          {recentEvents.length === 0 ? (
            <p className="mt-4 text-sm text-slate-400">
              No observability events recorded.
            </p>
          ) : (
            <ul className="mt-4 divide-y divide-slate-800">
              {recentEvents.map((event: ObservabilityEvent) => (
                <li key={event.id} className="flex items-start gap-3 py-3">
                  <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-argus-accent" />
                  <div className="min-w-0">
                    <p className="truncate text-sm text-slate-200">
                      {typeof event.payload?.message === 'string'
                        ? event.payload.message
                        : event.payload
                          ? JSON.stringify(event.payload).slice(0, 120)
                          : '—'}
                    </p>
                    <p className="mt-0.5 text-xs text-slate-500">
                      {event.event_type} · {event.source} ·{' '}
                      {new Date(event.timestamp).toLocaleString()}
                    </p>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    );
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Unknown error occurred';
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Dashboard</h1>
          <p className="mt-1 text-sm text-slate-400">
            ARGUS Intelligence overview
          </p>
        </div>
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load dashboard data
          </h2>
          <p className="mt-2 text-sm text-slate-400">
            Could not reach the ARGUS backend. Please ensure it is running on
            port 8000. ({message})
          </p>
        </div>
      </div>
    );
  }
}