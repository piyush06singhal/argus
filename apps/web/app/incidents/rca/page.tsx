import Link from 'next/link';

import { api, formatDate, type Incident } from '@/lib/api';
import { CAUSAL_DISCLAIMER } from '@/lib/causal';
import { SEVERITY_STYLES, STATUS_STYLES } from '@/lib/incidents';

export const metadata = {
  title: 'Root Cause Analysis',
};

export const dynamic = 'force-dynamic';

/**
 * Index for Phase 4 root-cause analysis.
 *
 * Analysis runs per incident, so this page exists to make the feature
 * reachable: it lists recent incidents and links into each one's analysis. It
 * deliberately does not summarise confidences in bulk — a confidence bucket
 * without its evidence is exactly the kind of conclusion this phase refuses to
 * hand out.
 */
export default async function RootCauseIndexPage() {
  let incidents: Incident[] = [];
  let error: string | null = null;
  try {
    const response = await api.listIncidents({ page: 1, pageSize: 25 });
    incidents = response.items;
  } catch (cause: unknown) {
    error = cause instanceof Error ? cause.message : 'unknown error';
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">
          Root Cause Analysis
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          Select an incident to see the evidence-supported explanations ARGUS
          derived for it: candidates, the causal chain, supporting and
          contradicting evidence, and what is still missing.
        </p>
      </div>

      {error ? (
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load incidents
          </h2>
          <p className="mt-2 text-sm text-slate-400">{error}</p>
        </div>
      ) : incidents.length === 0 ? (
        <div className="card">
          <p className="text-sm text-slate-400">
            No incidents have been recorded yet. Analysis reasons over stored
            evidence, so there is nothing to analyse until an incident exists.
          </p>
        </div>
      ) : (
        <div className="card overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-800 text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                <th className="px-3 py-2">Detected</th>
                <th className="px-3 py-2">Incident</th>
                <th className="px-3 py-2">Severity</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2">Analysis</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {incidents.map((incident) => (
                <tr key={incident.id} className="hover:bg-slate-800/40">
                  <td className="whitespace-nowrap px-3 py-2 text-slate-400">
                    {formatDate(incident.detected_at)}
                  </td>
                  <td className="px-3 py-2">
                    <Link
                      href={`/incidents/${incident.id}`}
                      className="text-slate-200 hover:text-argus-accent"
                    >
                      {incident.title}
                    </Link>
                  </td>
                  <td className="px-3 py-2">
                    <span
                      className={`badge ${
                        SEVERITY_STYLES[incident.severity] ?? SEVERITY_STYLES.LOW
                      }`}
                    >
                      {incident.severity}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <span
                      className={`badge ${
                        STATUS_STYLES[incident.status] ?? STATUS_STYLES.OPEN
                      }`}
                    >
                      {incident.status}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <Link
                      href={`/incidents/${incident.id}/causal-analysis`}
                      className="text-argus-accent hover:text-argus-accent-hover"
                    >
                      Open analysis →
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-xs text-slate-500">{CAUSAL_DISCLAIMER}</p>
    </div>
  );
}
