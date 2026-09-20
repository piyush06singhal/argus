'use client';

/**
 * Dependency Impact panel (§32, §47, §75) — transitive downstream
 * dependents of a selected component, labeled "Dependency Impact"
 * (structure only — never failure prediction).
 */

import { useMemo, useState } from 'react';
import type { GraphNode, ImpactResult } from '@/lib/graph';

interface Props {
  components: { id: string; name: string }[];
}

export default function ImpactPanel({ components }: Props) {
  const [componentId, setComponentId] = useState(components[0]?.id ?? '');
  const [result, setResult] = useState<ImpactResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function runImpact() {
    if (!componentId) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(
        `/api/v1/components/${componentId}/graph/impact?max_depth=10`
      );
      if (!res.ok) {
        throw new Error(
          res.status === 404
            ? 'Component has no graph node yet — reconcile first.'
            : `Impact analysis failed (${res.status})`
        );
      }
      setResult((await res.json()) as ImpactResult);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Impact analysis failed');
      setResult(null);
    } finally {
      setLoading(false);
    }
  }

  const maxHops = useMemo(
    () => result?.items.reduce((m, i) => Math.max(m, i.hops), 0) ?? 0,
    [result]
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <select
          value={componentId}
          onChange={(e) => setComponentId(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
        >
          {components.map((c) => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </select>
        <button
          onClick={runImpact}
          disabled={!componentId || loading}
          className="rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
        >
          {loading ? 'Analyzing…' : 'Analyze impact'}
        </button>
      </div>

      {error && <p className="rounded-md bg-red-50 p-3 text-sm text-red-700">{error}</p>}

      {result && (
        <div className="rounded-md border border-slate-200 bg-white p-4">
          <div className="flex items-baseline justify-between">
            <h4 className="text-sm font-semibold text-slate-800">
              Dependency Impact — {result.label}
            </h4>
            <span className="text-xs text-slate-500">
              {result.count} downstream {result.count === 1 ? 'component' : 'components'}
            </span>
          </div>

          {result.items.length === 0 ? (
            <p className="mt-3 text-xs text-slate-400">
              Nothing depends on this component downstream.
            </p>
          ) : (
            <ul className="mt-3 space-y-2">
              {[...result.items]
                .sort((a, b) => a.hops - b.hops || a.node.name.localeCompare(b.node.name))
                .map((item) => (
                  <li key={item.node.id} className="flex items-center gap-2 text-xs">
                    <span
                      className="inline-flex h-6 items-center rounded-full bg-slate-100 px-2 font-mono text-[10px] text-slate-600"
                      title={`Hops: ${item.hops}`}
                    >
                      {item.hops} hop{item.hops === 1 ? '' : 's'}
                    </span>
                    <span className="font-medium text-slate-800">{item.node.name}</span>
                    <span className="text-slate-400">
                      ({item.node.node_type})
                    </span>
                    <span className="ml-auto text-[10px] text-slate-400">
                      via {item.path.length - 1 > 0 ? `${item.path.length - 1} edges` : 'direct'}
                    </span>
                  </li>
                ))}
            </ul>
          )}

          <p className="mt-3 border-t border-slate-100 pt-2 text-[10px] text-slate-400">
            Structural dependency analysis (max depth {maxHops || 0}+ hops shown).
            Dependency impact describes graph reachability — it is not a
            prediction of failures or outages.
          </p>
        </div>
      )}
    </div>
  );
}
