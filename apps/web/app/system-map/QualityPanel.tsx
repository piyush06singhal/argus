'use client';

/**
 * Graph Health / Data-Quality panel (§50) — aggregated data-quality rows
 * from `/graph/health` with an overall ok pill. Warnings (e.g. alias
 * conflicts) are surfaced, not hidden.
 */

import { useEffect, useState } from 'react';
import type { GraphHealth } from '@/lib/graph';

interface Props {
  projectId: string;
}

const SEVERITY_STYLES: Record<string, string> = {
  INFO: 'bg-slate-50 text-slate-600 border-slate-200',
  WARNING: 'bg-amber-50 text-amber-800 border-amber-200',
  ERROR: 'bg-red-50 text-red-800 border-red-200',
};

export default function QualityPanel({ projectId }: Props) {
  const [health, setHealth] = useState<GraphHealth | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await fetch(`/api/v1/projects/${projectId}/graph/health`);
        if (!res.ok) throw new Error(`Health check failed (${res.status})`);
        const body = (await res.json()) as GraphHealth;
        if (!cancelled) {
          setHealth(body);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : 'Health check failed');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  if (loading) {
    return <p className="text-sm text-slate-500">Checking graph health…</p>;
  }

  if (error) {
    return <p className="rounded-md bg-red-50 p-4 text-sm text-red-700">{error}</p>;
  }

  if (!health) {
    return <p className="text-sm text-slate-500">No health data.</p>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <span
          className={`inline-flex items-center rounded-full border px-3 py-1 text-xs font-medium ${
            health.ok
              ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
              : 'border-red-200 bg-red-50 text-red-800'
          }`}
        >
          {health.ok ? 'OK' : 'ERRORS PRESENT'}
        </span>
        <span className="text-xs text-slate-500">
          {health.node_count} nodes · {health.edge_count} edges
          {health.last_reconciled_at
            ? ` · last reconciled ${new Date(health.last_reconciled_at).toLocaleString()}`
            : ''}
        </span>
      </div>

      {health.data_quality.length === 0 ? (
        <p className="rounded-md bg-slate-50 p-4 text-sm text-slate-500">
          No data-quality findings. The graph passed all checks.
        </p>
      ) : (
        <table className="w-full rounded-md border border-slate-200 bg-white text-xs">
          <thead>
            <tr className="border-b border-slate-200 text-left text-slate-500">
              <th className="px-3 py-2 font-medium">Check</th>
              <th className="px-3 py-2 font-medium">Severity</th>
              <th className="px-3 py-2 font-medium">Count</th>
              <th className="px-3 py-2 font-medium">Last detected</th>
            </tr>
          </thead>
          <tbody>
            {health.data_quality.map((row) => (
              <tr key={row.check_type} className="border-b border-slate-100">
                <td className="px-3 py-2 font-mono text-slate-700">{row.check_type}</td>
                <td className="px-3 py-2">
                  <span
                    className={`inline-flex rounded-full border px-2 py-0.5 font-medium ${
                      SEVERITY_STYLES[row.severity] ?? SEVERITY_STYLES.INFO
                    }`}
                  >
                    {row.severity}
                  </span>
                </td>
                <td className="px-3 py-2 text-slate-600">{row.count}</td>
                <td className="px-3 py-2 text-slate-600">
                  {row.latest_detected_at
                    ? new Date(row.latest_detected_at).toLocaleString()
                    : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
