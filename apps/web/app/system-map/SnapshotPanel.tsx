'use client';

/**
 * Snapshot panel (§24, §49) — list snapshots, create a new one, and diff
 * two snapshots to see structural change over time (graph history view).
 */

import { useCallback, useEffect, useState } from 'react';
import type { Snapshot, SnapshotDiff } from '@/lib/graph';

interface Props {
  projectId: string;
}

export default function SnapshotPanel({ projectId }: Props) {
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [diff, setDiff] = useState<SnapshotDiff | null>(null);
  const [aId, setAId] = useState<string>('');
  const [bId, setBId] = useState<string>('');

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(
        `/api/v1/projects/${projectId}/graph/snapshots?page=1&page_size=50`
      );
      if (!res.ok) throw new Error(`Failed to load snapshots (${res.status})`);
      const body = (await res.json()) as { items: Snapshot[] };
      setSnapshots(body.items);
      if (body.items.length >= 2) {
        setAId(body.items[1].id); // older
        setBId(body.items[0].id); // newer
      } else if (body.items.length === 1) {
        setAId(body.items[0].id);
        setBId(body.items[0].id);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load snapshots');
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function createSnapshot() {
    setCreating(true);
    setError(null);
    try {
      const res = await fetch(`/api/v1/projects/${projectId}/graph/snapshots`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: 'MANUAL' }),
      });
      if (!res.ok) throw new Error(`Snapshot failed (${res.status})`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Snapshot failed');
    } finally {
      setCreating(false);
    }
  }

  async function runDiff() {
    if (!aId || !bId) return;
    setError(null);
    try {
      const res = await fetch(`/api/v1/graph/snapshots/${aId}/diff/${bId}`);
      if (!res.ok) throw new Error(`Diff failed (${res.status})`);
      setDiff((await res.json()) as SnapshotDiff);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Diff failed');
      setDiff(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <button
          onClick={createSnapshot}
          disabled={creating}
          className="rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
        >
          {creating ? 'Creating…' : 'Create snapshot'}
        </button>
        <button
          onClick={runDiff}
          disabled={!aId || !bId}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
        >
          Diff selected
        </button>
        <select
          value={aId}
          onChange={(e) => setAId(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1.5 text-xs"
        >
          {snapshots.map((s) => (
            <option key={s.id} value={s.id}>v{s.snapshot_version} (from)</option>
          ))}
        </select>
        <span className="text-xs text-slate-500">→</span>
        <select
          value={bId}
          onChange={(e) => setBId(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1.5 text-xs"
        >
          {snapshots.map((s) => (
            <option key={s.id} value={s.id}>v{s.snapshot_version} (to)</option>
          ))}
        </select>
      </div>

      {error && <p className="rounded-md bg-red-50 p-3 text-sm text-red-700">{error}</p>}
      {loading && <p className="text-sm text-slate-500">Loading snapshots…</p>}

      {!loading && snapshots.length === 0 && (
        <p className="rounded-md bg-slate-50 p-4 text-sm text-slate-500">
          No snapshots yet. Create one to capture the current structure.
        </p>
      )}

      {!loading && snapshots.length > 0 && (
        <table className="w-full rounded-md border border-slate-200 bg-white text-xs">
          <thead>
            <tr className="border-b border-slate-200 text-left text-slate-500">
              <th className="px-3 py-2 font-medium">Version</th>
              <th className="px-3 py-2 font-medium">Nodes</th>
              <th className="px-3 py-2 font-medium">Edges</th>
              <th className="px-3 py-2 font-medium">Source</th>
              <th className="px-3 py-2 font-medium">Caption</th>
              <th className="px-3 py-2 font-medium">Created</th>
            </tr>
          </thead>
          <tbody>
            {snapshots.map((s) => (
              <tr key={s.id} className="border-b border-slate-100">
                <td className="px-3 py-2 font-medium text-slate-800">v{s.snapshot_version}</td>
                <td className="px-3 py-2 text-slate-600">{s.node_count}</td>
                <td className="px-3 py-2 text-slate-600">{s.edge_count}</td>
                <td className="px-3 py-2 text-slate-600">{s.source}</td>
                <td className="px-3 py-2 text-slate-600">{s.caption ?? '—'}</td>
                <td className="px-3 py-2 text-slate-600">
                  {new Date(s.created_at).toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {diff && (
        <div className="grid gap-4 md:grid-cols-2">
          <div className="rounded-md border border-slate-200 bg-white p-4">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Nodes added
            </h4>
            <ul className="mt-1 space-y-0.5">
              {diff.added_node_names.length === 0 && (
                <li className="text-xs text-slate-400">None</li>
              )}
              {diff.added_node_names.map((n) => (
                <li key={n} className="text-xs text-emerald-700">+ {n}</li>
              ))}
            </ul>
          </div>
          <div className="rounded-md border border-slate-200 bg-white p-4">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Nodes removed
            </h4>
            <ul className="mt-1 space-y-0.5">
              {diff.removed_node_names.length === 0 && (
                <li className="text-xs text-slate-400">None</li>
              )}
              {diff.removed_node_names.map((n) => (
                <li key={n} className="text-xs text-red-700">− {n}</li>
              ))}
            </ul>
          </div>
          <div className="rounded-md border border-slate-200 bg-white p-4 md:col-span-2">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Edge changes
            </h4>
            <p className="mt-1 text-xs text-slate-600">
              {diff.added_edges.length} added · {diff.removed_edges.length} removed
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
