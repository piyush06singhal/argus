import Link from 'next/link';

import { api, formatDate, type IncidentDashboard } from '@/lib/api';
import {
  ANOMALY_SEVERITY_STYLES,
  formatSeconds,
  SEVERITY_ORDER,
  SEVERITY_STYLES,
} from '@/lib/incidents';
import BarChart from '@/app/components/BarChart';

export const metadata = {
  title: 'Incident Dashboard',
};

export const dynamic = 'force-dynamic';

interface ProjectList {
  items: { id: string; name: string; slug: string }[];
}

function MetricTile({
  label,
  value,
  hint,
}: {
  label: string;
  value: string | number;
  hint?: string;
}) {
  return (
    <div className="card">
      <p className="text-xs font-medium uppercase tracking-wider text-slate-500">
        {label}
      </p>
      <p className="mt-2 text-2xl font-semibold text-slate-100">{value}</p>
      {hint ? <p className="mt-1 text-xs text-slate-500">{hint}</p> : null}
    </div>
  );
}

function bucketLabel(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export default async function IncidentDashboardPage({
  searchParams,
}: {
  searchParams: { project_id?: string; window_seconds?: string };
}) {
  let projects: ProjectList = { items: [] };
  try {
    projects = await api.listProjects(1, 100);
  } catch {
    // Fall through to the empty state below.
  }

  const requestedProject =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';
  const project =
    projects.items.find((p) => p.id === requestedProject) ??
    projects.items[0] ??
    null;

  if (!project) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-slate-100">
          Incident Dashboard
        </h1>
        <div className="card py-16 text-center">
          <p className="text-sm text-slate-400">
            No projects are available. Create a project and ingest telemetry to
            see incident intelligence.
          </p>
        </div>
      </div>
    );
  }

  const rawWindow = Number(searchParams.window_seconds);
  const windowSeconds =
    Number.isInteger(rawWindow) && rawWindow >= 60 ? rawWindow : 86_400;

  let dashboard: IncidentDashboard | null = null;
  let error: string | null = null;
  try {
    dashboard = await api.incidentDashboard(project.id, windowSeconds);
  } catch (e) {
    error = e instanceof Error ? e.message : 'Unknown error occurred';
  }

  const severityDistribution = dashboard?.severity_distribution ?? {};
  const totalSeverity = Object.values(severityDistribution).reduce(
    (acc, value) => acc + value,
    0
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">
            Incident Dashboard
          </h1>
          <p className="mt-1 text-sm text-slate-400">
            {project.name} · last {Math.round(windowSeconds / 3600)}h
          </p>
        </div>
        <div className="flex gap-3">
          <Link href="/incidents" className="btn-ghost">
            Incident list
          </Link>
          <Link href="/anomalies" className="btn-ghost">
            Anomaly center
          </Link>
        </div>
      </div>

      {projects.items.length > 1 ? (
        <form method="get" action="/incidents/dashboard" className="flex items-end gap-3">
          <div className="flex flex-col gap-1">
            <label
              htmlFor="project_id"
              className="text-xs font-medium text-slate-400"
            >
              Project
            </label>
            <select
              id="project_id"
              name="project_id"
              defaultValue={project.id}
              className="input"
            >
              {projects.items.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <label
              htmlFor="window_seconds"
              className="text-xs font-medium text-slate-400"
            >
              Window
            </label>
            <select
              id="window_seconds"
              name="window_seconds"
              defaultValue={String(windowSeconds)}
              className="input"
            >
              <option value="3600">Last hour</option>
              <option value="21600">Last 6 hours</option>
              <option value="86400">Last 24 hours</option>
              <option value="604800">Last 7 days</option>
            </select>
          </div>
          <button type="submit" className="btn-primary">
            Apply
          </button>
        </form>
      ) : null}

      {error ? (
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load the dashboard
          </h2>
          <p className="mt-2 text-sm text-slate-400">{error}</p>
        </div>
      ) : null}

      {dashboard ? (
        <>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <MetricTile
              label="Open incidents"
              value={dashboard.metrics.incidents_open}
              hint="OPEN / ACKNOWLEDGED / INVESTIGATING"
            />
            <MetricTile
              label="Created in window"
              value={dashboard.metrics.incidents_created}
            />
            <MetricTile
              label="Active anomalies"
              value={dashboard.metrics.anomalies_open}
              hint={`${dashboard.metrics.anomalies_detected} detected in window`}
            />
            <MetricTile
              label="Resolved in window"
              value={dashboard.metrics.incidents_resolved}
            />
            <MetricTile
              label="MTTA"
              value={formatSeconds(dashboard.metrics.mtta_seconds)}
              hint={dashboard.metrics.mtta_definition}
            />
            <MetricTile
              label="MTTR"
              value={formatSeconds(dashboard.metrics.mttr_seconds)}
              hint={dashboard.metrics.mttr_definition}
            />
            <MetricTile
              label="Deduplicated anomalies"
              value={dashboard.metrics.anomalies_deduplicated}
              hint="Repeated detections collapsed by fingerprint"
            />
            <MetricTile
              label="Suppressed anomalies"
              value={dashboard.metrics.anomalies_suppressed}
              hint="Recorded, never silently dropped"
            />
          </div>

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <div className="card">
              <BarChart
                title="Anomalies over time"
                data={dashboard.anomalies_over_time.map((bucket) => ({
                  label: bucketLabel(bucket.bucket_start),
                  value: bucket.count,
                }))}
              />
            </div>
            <div className="card">
              <BarChart
                title="Incidents over time"
                data={dashboard.incidents_over_time.map((bucket) => ({
                  label: bucketLabel(bucket.bucket_start),
                  value: bucket.count,
                }))}
              />
            </div>
          </div>

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <section className="card">
              <h2 className="mb-3 font-medium text-slate-200">
                Severity distribution
              </h2>
              {totalSeverity === 0 ? (
                <p className="text-sm text-slate-500">
                  No anomalies in this window.
                </p>
              ) : (
                <ul className="space-y-2">
                  {SEVERITY_ORDER.map((severity) => {
                    const count = severityDistribution[severity] ?? 0;
                    return (
                      <li key={severity} className="flex items-center gap-3">
                        <span
                          className={`badge w-20 justify-center ${
                            SEVERITY_STYLES[severity] ?? SEVERITY_STYLES.LOW
                          }`}
                        >
                          {severity}
                        </span>
                        <div className="h-2 flex-1 rounded bg-slate-800">
                          <div
                            className="h-2 rounded bg-argus-accent/70"
                            style={{
                              width: `${(count / totalSeverity) * 100}%`,
                            }}
                          />
                        </div>
                        <span className="w-10 text-right text-sm text-slate-400">
                          {count}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>

            <section className="card">
              <h2 className="mb-3 font-medium text-slate-200">
                Top affected components
              </h2>
              {dashboard.top_affected_components.length === 0 ? (
                <p className="text-sm text-slate-500">
                  No component impact in this window.
                </p>
              ) : (
                <ul className="divide-y divide-slate-800">
                  {dashboard.top_affected_components.map((component) => (
                    <li
                      key={component.component_id}
                      className="flex items-center justify-between gap-3 py-2"
                    >
                      <span className="text-sm text-slate-200">
                        {component.name ?? component.component_id}
                      </span>
                      <span className="flex items-center gap-2">
                        <span
                          className={`badge ${
                            ANOMALY_SEVERITY_STYLES[
                              component.max_severity as keyof typeof ANOMALY_SEVERITY_STYLES
                            ] ?? ANOMALY_SEVERITY_STYLES.LOW
                          }`}
                        >
                          {component.max_severity}
                        </span>
                        <span className="text-sm text-slate-400">
                          {component.anomaly_count}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <section className="card">
              <h2 className="mb-3 font-medium text-slate-200">
                Anomaly categories
              </h2>
              {Object.keys(dashboard.anomaly_categories).length === 0 ? (
                <p className="text-sm text-slate-500">
                  No anomalies in this window.
                </p>
              ) : (
                <BarChart
                  title=""
                  data={Object.entries(dashboard.anomaly_categories)
                    .sort((a, b) => b[1] - a[1])
                    .map(([label, value]) => ({ label, value }))}
                />
              )}
              <ul className="mt-3 space-y-1">
                {Object.entries(dashboard.anomaly_categories)
                  .sort((a, b) => b[1] - a[1])
                  .map(([label, value]) => (
                    <li
                      key={label}
                      className="flex justify-between text-xs text-slate-400"
                    >
                      <span>{label}</span>
                      <span>{value}</span>
                    </li>
                  ))}
              </ul>
            </section>

            <section className="card">
              <h2 className="mb-3 font-medium text-slate-200">
                Recent incidents
              </h2>
              {dashboard.recent_incidents.length === 0 ? (
                <p className="text-sm text-slate-500">No incidents recorded.</p>
              ) : (
                <ul className="divide-y divide-slate-800">
                  {dashboard.recent_incidents.map((incident) => (
                    <li key={incident.id} className="py-2">
                      <div className="flex items-center justify-between gap-3">
                        <Link
                          href={`/incidents/${incident.id}`}
                          className="text-sm text-argus-accent hover:text-argus-accent-hover"
                        >
                          {incident.title}
                        </Link>
                        <span className="flex items-center gap-2">
                          <span
                            className={`badge ${
                              SEVERITY_STYLES[
                                incident.severity as keyof typeof SEVERITY_STYLES
                              ] ?? SEVERITY_STYLES.LOW
                            }`}
                          >
                            {incident.severity}
                          </span>
                          <span className="text-xs text-slate-500">
                            {formatDate(incident.detected_at)}
                          </span>
                        </span>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>

          <p className="text-xs text-slate-500">
            Metrics are deterministic aggregates of stored rows.
            Correlated ≠ causal: an incident groups anomalies that share
            evidence, and does not identify a root cause.
          </p>
        </>
      ) : null}
    </div>
  );
}
