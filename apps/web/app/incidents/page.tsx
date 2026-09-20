import Link from 'next/link';

import { api, formatDate, type Incident } from '@/lib/api';
import {
  INCIDENT_STATUS_ORDER,
  SEVERITY_ORDER,
  SEVERITY_STYLES,
  STATUS_STYLES,
} from '@/lib/incidents';

export const metadata = {
  title: 'Incidents',
};

interface IncidentsSearchParams {
  page?: string;
  severity?: string;
  status?: string;
  project_id?: string;
}

export default async function IncidentsPage({
  searchParams,
}: {
  searchParams: IncidentsSearchParams;
}) {
  const rawPage = Number(searchParams.page);
  const page = Number.isInteger(rawPage) && rawPage > 0 ? rawPage : 1;
  const severity =
    typeof searchParams.severity === 'string' ? searchParams.severity : '';
  const status =
    typeof searchParams.status === 'string' ? searchParams.status : '';
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  const buildHref = (overrides: {
    page?: number;
    severity?: string;
    status?: string;
  }) => {
    const params = new URLSearchParams();
    const nextSeverity = overrides.severity ?? severity;
    const nextStatus = overrides.status ?? status;
    if (overrides.page !== undefined) {
      params.set('page', String(overrides.page));
    }
    if (nextSeverity !== '') params.set('severity', nextSeverity);
    if (nextStatus !== '') params.set('status', nextStatus);
    if (projectId !== '') params.set('project_id', projectId);
    const qs = params.toString();
    return qs ? `/incidents?${qs}` : '/incidents';
  };

  try {
    const data = await api.listIncidents({
      page,
      pageSize: 20,
      severity,
      status,
      projectId: projectId || undefined,
    });

    return (
      <div className="space-y-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold text-slate-100">Incidents</h1>
            <p className="mt-1 text-sm text-slate-400">
              {data.total} incident{data.total === 1 ? '' : 's'} — correlated
              anomaly groups, not determined root causes.
            </p>
          </div>
          <div className="flex gap-3">
            <Link href="/incidents/dashboard" className="btn-ghost">
              Dashboard
            </Link>
            <Link href="/anomalies" className="btn-ghost">
              Anomaly center
            </Link>
          </div>
        </div>

        <form
          method="get"
          action="/incidents"
          className="flex flex-wrap items-end gap-3"
        >
          <div className="flex flex-col gap-1">
            <label
              htmlFor="severity"
              className="text-xs font-medium text-slate-400"
            >
              Severity
            </label>
            <select
              id="severity"
              name="severity"
              defaultValue={severity}
              className="input"
            >
              <option value="">All severities</option>
              {SEVERITY_ORDER.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </div>

          <div className="flex flex-col gap-1">
            <label
              htmlFor="status"
              className="text-xs font-medium text-slate-400"
            >
              Status
            </label>
            <select
              id="status"
              name="status"
              defaultValue={status}
              className="input"
            >
              <option value="">All statuses</option>
              {INCIDENT_STATUS_ORDER.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </div>

          <button type="submit" className="btn-primary">
            Apply filters
          </button>

          {(severity !== '' || status !== '' || projectId !== '') && (
            <Link href="/incidents" className="btn-ghost">
              Clear filters
            </Link>
          )}
        </form>

        {data.items.length === 0 ? (
          <div className="card py-16 text-center">
            <p className="text-sm text-slate-400">
              {severity !== '' || status !== ''
                ? 'No incidents match the selected filters.'
                : 'No incidents recorded yet. Incidents appear when anomalies are correlated.'}
            </p>
          </div>
        ) : (
          <div className="card overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-4 py-3">Title</th>
                  <th className="px-4 py-3">Severity</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Detected</th>
                  <th className="px-4 py-3">Fingerprint</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {data.items.map((incident: Incident) => (
                  <tr
                    key={incident.id}
                    className="transition-colors hover:bg-slate-800/40"
                  >
                    <td className="px-4 py-3">
                      <Link
                        href={`/incidents/${incident.id}`}
                        className="font-medium text-argus-accent hover:text-argus-accent-hover"
                      >
                        {incident.title}
                      </Link>
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`badge ${
                          SEVERITY_STYLES[incident.severity] ??
                          SEVERITY_STYLES.LOW
                        }`}
                      >
                        {incident.severity}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`badge ${
                          STATUS_STYLES[incident.status] ?? STATUS_STYLES.OPEN
                        }`}
                      >
                        {incident.status}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-slate-400">
                      {formatDate(incident.detected_at)}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-500">
                      {incident.fingerprint
                        ? `${incident.fingerprint.slice(0, 12)}…`
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {data.total_pages > 1 && (
          <nav className="flex items-center justify-between">
            <p className="text-sm text-slate-500">
              Page {page} of {data.total_pages}
            </p>
            <div className="flex gap-3">
              {page > 1 ? (
                <Link
                  href={buildHref({ page: page - 1 })}
                  className="btn-ghost"
                >
                  Previous
                </Link>
              ) : (
                <span className="btn-ghost cursor-not-allowed opacity-40">
                  Previous
                </span>
              )}
              {page < data.total_pages ? (
                <Link
                  href={buildHref({ page: page + 1 })}
                  className="btn-primary"
                >
                  Next
                </Link>
              ) : (
                <span className="btn-primary cursor-not-allowed opacity-40">
                  Next
                </span>
              )}
            </div>
          </nav>
        )}
      </div>
    );
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Unknown error occurred';
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Incidents</h1>
          <p className="mt-1 text-sm text-slate-400">All recorded incidents</p>
        </div>
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load incidents
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
