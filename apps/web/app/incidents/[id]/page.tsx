import Link from 'next/link';
import {
  api,
  formatDate,
  type Evidence,
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

export const metadata = {
  title: 'Incident Detail',
};

export default async function IncidentDetailPage({
  params,
}: {
  params: { id: string };
}) {
  const { id } = params;

  try {
    const [incident, evidence] = await Promise.all([
      api.getIncident(id),
      api.getIncidentEvidence(id),
    ]);

    return (
      <div className="space-y-6">
        <div>
          <Link
            href="/incidents"
            className="text-sm text-slate-400 hover:text-argus-accent"
          >
            ← Back to incidents
          </Link>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-semibold text-slate-100">
              {incident.title}
            </h1>
            <span
              className={`badge ${
                SEVERITY_STYLES[incident.severity] ?? SEVERITY_STYLES.low
              }`}
            >
              {incident.severity}
            </span>
            <span
              className={`badge ${
                STATUS_STYLES[incident.status] ?? STATUS_STYLES.detected
              }`}
            >
              {incident.status}
            </span>
          </div>
          <p className="mt-1 text-xs text-slate-500">Incident ID: {incident.id}</p>
        </div>

        {incident.description ? (
          <div className="card">
            <h2 className="mb-2 font-medium text-slate-200">Description</h2>
            <p className="text-sm whitespace-pre-line text-slate-400">
              {incident.description}
            </p>
          </div>
        ) : null}

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <TimelineItem
            label="Detected at"
            value={incident.detected_at}
            defaultText="Not detected"
          />
          <TimelineItem
            label="Started at"
            value={incident.started_at}
            defaultText="Not started"
          />
          <TimelineItem
            label="Resolved at"
            value={incident.resolved_at}
            defaultText="Not resolved"
          />
          <div className="card">
            <p className="text-xs font-medium uppercase tracking-wider text-slate-500">
              Assigned team
            </p>
            <p className="mt-2 text-sm text-slate-300">
              {incident.assigned_team ?? '—'}
            </p>
            <p className="mt-3 text-xs font-medium uppercase tracking-wider text-slate-500">
              Project
            </p>
            <p className="mt-2 text-sm text-slate-300">
              {incident.project_id ? (
                <Link
                  href={`/projects/${incident.project_id}`}
                  className="text-argus-accent hover:text-argus-accent-hover"
                >
                  {incident.project_id}
                </Link>
              ) : (
                '—'
              )}
            </p>
            <p className="mt-3 text-xs font-medium uppercase tracking-wider text-slate-500">
              Created at
            </p>
            <p className="mt-2 text-sm text-slate-300">
              {incident.created_at ? formatDate(incident.created_at) : '—'}
            </p>
            <p className="mt-3 text-xs font-medium uppercase tracking-wider text-slate-500">
              Last updated
            </p>
            <p className="mt-2 text-sm text-slate-300">
              {incident.updated_at ? formatDate(incident.updated_at) : '—'}
            </p>
          </div>
        </div>

        <div className="card">
          <h2 className="mb-2 font-medium text-slate-200">Root Cause Analysis</h2>
          <p className="text-sm text-slate-400">
            Not available in current phase.
          </p>
        </div>

        <div className="card">
          <h2 className="mb-3 font-medium text-slate-200">
            Evidence
            <span className="ml-2 text-xs font-normal text-slate-500">
              {evidence.total}
            </span>
          </h2>
          {evidence.items.length === 0 ? (
            <p className="text-sm text-slate-400">
              No evidence has been collected for this incident.
            </p>
          ) : (
            <ul className="divide-y divide-slate-800">
              {evidence.items.map((item: Evidence) => (
                <li key={item.id} className="py-3">
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-sm font-medium text-slate-200">
                      {item.evidence_type}
                    </p>
                    <span className="text-xs text-slate-500">
                      {formatDate(item.collected_at)}
                    </span>
                  </div>
                  {item.collection_method && (
                    <p className="mt-0.5 text-xs text-slate-500">
                      Collected via {item.collection_method}
                    </p>
                  )}
                  {item.data && Object.keys(item.data).length > 0 ? (
                    <pre className="mt-2 overflow-x-auto rounded-md bg-slate-950 p-3 font-mono text-xs text-slate-300">
                      {JSON.stringify(item.data, null, 2)}
                    </pre>
                  ) : null}
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
          href="/incidents"
          className="text-sm text-slate-400 hover:text-argus-accent"
        >
          ← Back to incidents
        </Link>
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load incident
          </h2>
          <p className="mt-2 text-sm text-slate-400">
            Could not load incident {id}. ({message})
          </p>
        </div>
      </div>
    );
  }
}

function TimelineItem({
  label,
  value,
  defaultText,
}: {
  label: string;
  value: string | null | undefined;
  defaultText: string;
}) {
  return (
    <div className="card">
      <p className="text-xs font-medium uppercase tracking-wider text-slate-500">
        {label}
      </p>
      <p className={`mt-2 text-sm ${value ? 'text-slate-300' : 'text-slate-600'}`}>
        {value ? formatDate(value) : defaultText}
      </p>
    </div>
  );
}