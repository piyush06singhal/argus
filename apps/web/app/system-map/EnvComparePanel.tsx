'use client';

/**
 * Environment Comparison panel (§48) — structural diff between two
 * environments. Shows missing/extra components, differing dependencies,
 * endpoints, and versions. A difference is displayed as structure only —
 * never as a problem statement (§74).
 */

import { useMemo, useState } from 'react';
import type { EnvComparison, EnvDiffItem } from '@/lib/graph';

interface Props {
  projectId: string;
  environments: { id: string; name: string }[];
}

export default function EnvComparePanel({ projectId, environments }: Props) {
  const [aId, setAId] = useState(environments[0]?.id ?? '');
  const [bId, setBId] = useState(environments[1]?.id ?? '');
  const [result, setResult] = useState<EnvComparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const canCompare = Boolean(aId && bId && aId !== bId);

  async function runCompare() {
    if (!canCompare) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(
        `/api/v1/projects/${projectId}/graph/environments/compare?environment_a=${aId}&environment_b=${bId}`
      );
      if (!res.ok) {
        throw new Error(`Comparison failed (${res.status})`);
      }
      setResult((await res.json()) as EnvComparison);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Comparison failed');
      setResult(null);
    } finally {
      setLoading(false);
    }
  }

  const byKind = useMemo(() => {
    const groups = new Map<string, EnvDiffItem[]>();
    for (const item of result?.added ?? []) {
      const list = groups.get(item.kind) ?? [];
      list.push(item);
      groups.set(item.kind, list);
    }
    return Array.from(groups.entries());
  }, [result]);

  const removedByKind = useMemo(() => {
    const groups = new Map<string, EnvDiffItem[]>();
    for (const item of result?.removed ?? []) {
      const list = groups.get(item.kind) ?? [];
      list.push(item);
      groups.set(item.kind, list);
    }
    return Array.from(groups.entries());
  }, [result]);

  if (environments.length < 2) {
    return (
      <p className="rounded-md bg-slate-50 p-6 text-sm text-slate-500">
        Environment comparison needs at least two environments.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <select
          value={aId}
          onChange={(e) => setAId(e.target.value)}
          aria-label="Compare from environment"
          className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
        >
          {environments.map((e) => (
            <option key={e.id} value={e.id}>{e.name}</option>
          ))}
        </select>
        <span className="text-xs text-slate-500">vs</span>
        <select
          value={bId}
          onChange={(e) => setBId(e.target.value)}
          aria-label="Compare to environment"
          className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
        >
          {environments.map((e) => (
            <option key={e.id} value={e.id}>{e.name}</option>
          ))}
        </select>
        <button
          onClick={runCompare}
          disabled={!canCompare || loading}
          className="rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
        >
          {loading ? 'Comparing…' : 'Compare'}
        </button>
      </div>

      {error && (
        <p className="rounded-md bg-red-50 p-3 text-sm text-red-700">{error}</p>
      )}

      {result && (
        <div className="grid gap-4 md:grid-cols-2">
          <div className="rounded-md border border-slate-200 bg-white p-4">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Only in {result.environment_b.name}
            </h4>
            {byKind.length === 0 ? (
              <p className="mt-2 text-xs text-slate-400">No differences.</p>
            ) : (
              byKind.map((entry) => (
                <div key={entry[0]} className="mt-2">
                  <p className="text-xs font-medium text-slate-700">{entry[0]}</p>
                  <ul className="mt-1 space-y-0.5">
                    {entry[1].map((i) => (
                      <li key={i.key} className="text-xs text-emerald-700">
                        + {i.category.name}
                        {i.category.node_type ? ` (${i.category.node_type})` : ''}
                      </li>
                    ))}
                  </ul>
                </div>
              ))
            )}
          </div>

          <div className="rounded-md border border-slate-200 bg-white p-4">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Only in {result.environment_a.name}
            </h4>
            {removedByKind.length === 0 ? (
              <p className="mt-2 text-xs text-slate-400">No differences.</p>
            ) : (
              removedByKind.map((entry) => (
                <div key={entry[0]} className="mt-2">
                  <p className="text-xs font-medium text-slate-700">{entry[0]}</p>
                  <ul className="mt-1 space-y-0.5">
                    {entry[1].map((i) => (
                      <li key={i.key} className="text-xs text-red-700">
                        − {i.category.name}
                        {i.category.node_type ? ` (${i.category.node_type})` : ''}
                      </li>
                    ))}
                  </ul>
                </div>
              ))
            )}
          </div>

          <div className="rounded-md border border-slate-200 bg-white p-4 md:col-span-2">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Totals
            </h4>
            <p className="mt-1 text-xs text-slate-600">
              {result.environment_a.name}: {result.environment_a.node_count} nodes ·{' '}
              {result.environment_a.edge_count} edges · {result.environment_a.component_count} components
              {' — '}
              {result.environment_b.name}: {result.environment_b.node_count} nodes ·{' '}
              {result.environment_b.edge_count} edges · {result.environment_b.component_count} components
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
