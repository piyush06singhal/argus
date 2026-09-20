import Link from 'next/link';

import { api, formatDate, type Anomaly } from '@/lib/api';
import {
  ANOMALY_SEVERITY_STYLES,
  ANOMALY_STATUS_ORDER,
  ANOMALY_STATUS_STYLES,
  formatDeviation,
  formatValue,
  SEVERITY_ORDER,
} from '@/lib/incidents';

export const metadata = {
  title: 'Anomaly Center',
};

export const dynamic = 'force-dynamic';

const ANOMALY_TYPES = [
  'METRIC_THRESHOLD',
  'METRIC_BASELINE_DEVIATION',
  'ERROR_RATE_SPIKE',
  'LATENCY_SPIKE',
  'THROUGHPUT_DROP',
  'LOG_PATTERN_SPIKE',
  'TRACE_FAILURE_SPIKE',
  'HEALTH_DEGRADATION',
  'REQUEST_RATE_CHANGE',
  'RESOURCE_USAGE_SPIKE',
  'DEPLOYMENT_RELATED_CHANGE',
  'CONFIGURATION_RELATED_CHANGE',
] as const;

const SOURCES = [
  'METRIC',
  'LOG',
  'TRACE',
  'SPAN',
  'HEALTH_CHECK',
  'DEPLOYMENT',
  'CONFIGURATION',
  'GRAPH',
  'COMPOSITE',
  'UNKNOWN',
] as const;

interface AnomalySearchParams {
  page?: string;
  severity?: string;
  status?: string;
  anomaly_type?: string;
  source?: string;
  project_id?: string;
  component_id?: string;
  incident_id?: string;
}

export default async function AnomalyCenterPage({
  searchParams,
}: {
  searchParams: AnomalySearchParams;
}) {
  const pick = (key: keyof AnomalySearchParams): string =>
    typeof searchParams[key] === 'string' ? (searchParams[key] as string) : '';

  const rawPage = Number(searchParams.page);
  const page = Number.isInteger(rawPage) && rawPage > 0 ? rawPage : 1;
  const severity = pick('severity');
  const status = pick('status');
  const anomalyType = pick('anomaly_type');
  const source = pick('source');
  const projectId = pick('project_id');
  const componentId = pick('component_id');
  const incidentId = pick('incident_id');

  const hasFilters = [
    severity,
    status,
    anomalyType,
    source,
    projectId,
    componentId,
    incidentId,
  ].some((value) => value !== '');

  try {
    const data = await api.listAnomalies({
      page,
      pageSize: 25,
      severity: severity || undefined,
      status: status || undefined,
      anomalyType: anomalyType || undefined,
      source: source || undefined,
      projectId: projectId || undefined,
      componentId: componentId || undefined,
      incidentId: incidentId || undefined,
    });

    const buildHref = (nextPage: number) => {
      const params = new URLSearchParams();
      params.set('page', String(nextPage));
      if (severity) params.set('severity', severity);
      if (status) params.set('status', status);
      if (anomalyType) params.set('anomaly_type', anomalyType);
      if (source) params.set('source', source);
      if (projectId) params.set('project_id', projectId);
      if (componentId) params.set('component_id', componentId);
      if (incidentId) params.set('incident_id', incidentId);
      return `/anomalies?${params.toString()}`;
    };

    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">
            Anomaly Center
          </h1>
          <p className="mt-1 text-sm text-slate-400">
            {data.total} anomal{data.total === 1 ? 'y' : 'ies'} detected by
            deterministic rules. Each one records why it fired.
          </p>
        </div>

        <form
          method="get"
          action="/anomalies"
          className="card flex flex-wrap items-end gap-3"
        >
          <div className="flex flex-col gap-1">
            <label htmlFor="severity" className="text-xs font-medium text-slate-400">
              Severity
            </label>
            <select id="severity" name="severity" defaultValue={severity} className="input">
              <option value="">All</option>
              {SEVERITY_ORDER.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </div>

          <div className="flex flex-col gap-1">
            <label htmlFor="status" className="text-xs font-medium text-slate-400">
              Status
            </label>
            <select id="status" name="status" defaultValue={status} className="input">
              <option value="">All</option>
              {ANOMALY_STATUS_ORDER.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </div>

          <div className="flex flex-col gap-1">
            <label
              htmlFor="anomaly_type"
              className="text-xs font-medium text-slate-400"
            >
              Anomaly type
            </label>
            <select
              id="anomaly_type"
              name="anomaly_type"
              defaultValue={anomalyType}
              className="input"
            >
              <option value="">All</option>
              {ANOMALY_TYPES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </div>

          <div className="flex flex-col gap-1">
            <label htmlFor="source" className="text-xs font-medium text-slate-400">
              Source
            </label>
            <select id="source" name="source" defaultValue={source} className="input">
              <option value="">All</option>
              {SOURCES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </div>

          <div className="flex flex-col gap-1">
            <label
              htmlFor="component_id"
              className="text-xs font-medium text-slate-400"
            >
              Component ID
            </label>
            <input
              id="component_id"
              name="component_id"
              defaultValue={componentId}
              className="input"
              placeholder="uuid"
            />
          </div>

          <div className="flex flex-col gap-1">
            <label
              htmlFor="project_id"
              className="text-xs font-medium text-slate-400"
            >
              Project ID
            </label>
            <input
              id="project_id"
              name="project_id"
              defaultValue={projectId}
              className="input"
              placeholder="uuid"
            />
          </div>

          <button type="submit" className="btn-primary">
            Apply
          </button>
          {hasFilters ? (
            <Link href="/anomalies" className="btn-ghost">
              Clear
            </Link>
          ) : null}
        </form>

        {data.items.length === 0 ? (
          <div className="card py-16 text-center">
            <p className="text-sm text-slate-400">
              {hasFilters
                ? 'No anomalies match the selected filters.'
                : 'No anomalies detected yet. Detectors run on ingested telemetry.'}
            </p>
          </div>
        ) : (
          <div className="card overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-4 py-3">Detected</th>
                  <th className="px-4 py-3">Severity</th>
                  <th className="px-4 py-3">Type</th>
                  <th className="px-4 py-3">Observed</th>
                  <th className="px-4 py-3">Expected</th>
                  <th className="px-4 py-3">Deviation</th>
                  <th className="px-4 py-3">Obs.</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Incident</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {data.items.map((anomaly: Anomaly) => (
                  <tr key={anomaly.id} className="hover:bg-slate-800/40">
                    <td className="whitespace-nowrap px-4 py-3 text-slate-400">
                      {formatDate(anomaly.detected_at)}
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`badge ${
                          ANOMALY_SEVERITY_STYLES[anomaly.severity] ??
                          ANOMALY_SEVERITY_STYLES.LOW
                        }`}
                      >
                        {anomaly.severity}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <Link
                        href={`/anomalies/${anomaly.id}`}
                        className="text-argus-accent hover:text-argus-accent-hover"
                      >
                        {anomaly.anomaly_type}
                      </Link>
                      {anomaly.metric_name ? (
                        <p className="font-mono text-xs text-slate-500">
                          {anomaly.metric_name}
                        </p>
                      ) : null}
                      {anomaly.suppressed ? (
                        <p className="mt-1 text-xs text-slate-500">
                          suppressed — recorded, not hidden
                        </p>
                      ) : null}
                    </td>
                    <td className="px-4 py-3 text-slate-300">
                      {formatValue(anomaly.observed_value)}
                    </td>
                    <td className="px-4 py-3 text-slate-300">
                      {formatValue(anomaly.expected_value)}
                    </td>
                    <td className="px-4 py-3 text-slate-300">
                      {formatDeviation(anomaly.deviation)}
                    </td>
                    <td className="px-4 py-3 text-slate-400">
                      {anomaly.observation_count}
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`badge ${
                          ANOMALY_STATUS_STYLES[anomaly.status] ??
                          ANOMALY_STATUS_STYLES.DETECTED
                        }`}
                      >
                        {anomaly.status}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      {anomaly.incident_id ? (
                        <Link
                          href={`/incidents/${anomaly.incident_id}`}
                          className="font-mono text-xs text-argus-accent hover:text-argus-accent-hover"
                        >
                          {anomaly.incident_id.slice(0, 8)}…
                        </Link>
                      ) : (
                        <span className="text-xs text-slate-600">
                          not correlated
                        </span>
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
                <Link href={buildHref(page - 1)} className="btn-ghost">
                  Previous
                </Link>
              ) : (
                <span className="btn-ghost cursor-not-allowed opacity-40">
                  Previous
                </span>
              )}
              {page < data.total_pages ? (
                <Link href={buildHref(page + 1)} className="btn-primary">
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
        <h1 className="text-2xl font-semibold text-slate-100">Anomaly Center</h1>
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load anomalies
          </h2>
          <p className="mt-2 text-sm text-slate-400">
            Could not reach the ARGUS backend. ({message})
          </p>
        </div>
      </div>
    );
  }
}
