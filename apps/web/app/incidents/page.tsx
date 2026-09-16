import Link from 'next/link';
import {
  api,
  formatDate,
  type Incident,
  type IncidentSeverity,
  type IncidentStatus,
} from '@/lib/api';

const SEVERITY_STYLES: Record<IncidentSeverity, string> = {
  critical: 'bg-argus-error/15 text-argus-error',
  major: 'bg-argus-warning/15 text-argus-warning',
  minor: 'bg-argus-info/15 text-argus-info',
  low: 'bg-argus-accent/15 text-argus-accent',
};

const STATUS_STYLES: Record<IncidentStatus, string> = {
  detected: 'bg-argus-error/10 text-argus-error',
  acknowledged: 'bg-argus-warning/10 text-argus-warning',
  in_progress: 'bg-argus-info/10 text-argus-info',
  resolved: 'bg-argus-success/10 text-argus-success',
  closed: 'bg-slate-700/40 text-slate-400',
};

interface IncidentsSearchParams {
  page?: string;
  severity?: string;
  status?: string;
}

export const metadata = {
  title: 'Incidents',
};

export default async function IncidentsPage({
  searchParams,
}: {
  searchParams: IncidentsSearchParams;
}) {
  const rawPage = Number(searchParams.page);
  const page = Number.isInteger(rawPage) && rawPage > 0 ? rawPage : 1;
  const severity = typeof searchParams.severity === 'string' ? searchParams.severity : '';
  const status = typeof searchParams.status === 'string' ? searchParams.status : '';

  try {
    const data = await api.listIncidents({ page, pageSize: 20, severity, status });

    const buildListHref = (overrides: { page?: number; severity?: string }) => {
      const params = new URLSearchParams();
      if (overrides.page !== undefined) {
        params.set('page', String(overrides.page));
      }
      if (overrides.severity !== undefined && overrides.severity !== '') {
        params.set('severity', overrides.severity);
      }
      if (status !== '') {
        params.set('status', status);
      }
      const qs = params.toString();
      return qs ? `/incidents?${qs}` : '/incidents';
    };

    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Incidents</h1>
          <p className="mt-1 text-sm text-slate-400">
            {data.total} incident{data.total === 1 ? '' : 's'}
          </p>
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
              <option value="critical">Critical</option>
              <option value="major">Major</option>
              <option value="minor">Minor</option>
              <option value="low">Low</option>
            </select>
          </div>

          <div className="flex flex-col gap-1">
            <label
              htmlFor="status"
              className="text-xs font-medium text-slate-400"
            >
              Status
            </label>
            <select id="status" name="status" defaultValue={status} className="input">
              <option value="">All statuses</option>
              <option value="detected">Detected</option>
              <option value="acknowledged">Acknowledged</option>
              <option value="in_progress">In progress</option>
              <option value="resolved">Resolved</option>
              <option value="closed">Closed</option>
            </select>
          </div>

          <button type="submit" className="btn-primary">
            Apply filters
          </button>

          {(severity !== '' || status !== '') && (
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
                : 'No incidents recorded.'}
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
                  <th className="px-4 py-3">Project</th>
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
                          SEVERITY_STYLES.low
                        }`}
                      >
                        {incident.severity}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`badge ${
                          STATUS_STYLES[incident.status] ??
                          STATUS_STYLES.detected
                        }`}
                      >
                        {incident.status}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-slate-400">
                      {formatDate(incident.detected_at)}
                    </td>
                    <td className="px-4 py-3 text-slate-500">
                      {incident.project_id ? (
                        <Link
                          href={`/projects/${incident.project_id}`}
                          className="font-mono text-xs text-slate-400 hover:text-argus-accent"
                        >
                          {incident.project_id}
                        </Link>
                      ) : (
                        '—'
                      )}
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
                <Link href={buildListHref({ page: page - 1 })} className="btn-ghost">
                  Previous
                </Link>
              ) : (
                <span className="btn-ghost cursor-not-allowed opacity-40">
                  Previous
                </span>
              )}
              {page < data.total_pages ? (
                <Link href={buildListHref({ page: page + 1 })} className="btn-primary">
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
          <p className="mt-1 text-sm text-slate-400">
            All recorded incidents
          </p>
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