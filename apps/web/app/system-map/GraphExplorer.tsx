'use client';

/**
 * Interactive SVG knowledge-graph explorer (§43–46, §51–52).
 *
 * Client component: filter/search/selection state stays local; all data is
 * fetched server-side and passed in. Edges carry provenance styling
 * (Configured solid / Observed dashed / Inferred dotted, STALE gray) so
 * uncertainty is never hidden (§52).
 */

import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import {
  type GraphData,
  type GraphNode,
  SOURCE_LABELS,
  sourceLabel,
  confidenceBand,
} from '@/lib/graph';
import {
  type EdgeGeometry,
  edgeGeometry,
  layoutGraph,
  NODE_H,
  NODE_W,
  shortLabel,
  typeColor,
} from '@/lib/graph-layout';
import {
  HORIZON_LABELS,
  riskRank,
  type ForecastHorizon,
  type ForecastRiskLevel,
} from '@/lib/reliability';

/** One component's predicted risk, for the §56 overlay. */
export interface RiskOverlayEntry {
  component_name: string;
  worst_level: ForecastRiskLevel;
  by_horizon: Partial<Record<ForecastHorizon, ForecastRiskLevel>>;
}

const NODE_TYPE_FILTERS = [
  'ALL',
  'SERVICE',
  'APPLICATION',
  'DATABASE',
  'CACHE',
  'QUEUE',
  'EXTERNAL_API',
  'REPOSITORY',
  'ENVIRONMENT',
  'PROJECT',
] as const;

const SOURCE_FILTERS = ['ALL', 'CONFIGURATION', 'TRACE', 'LOG', 'INFERENCE'] as const;

const RISK_STROKE: Record<ForecastRiskLevel, string> = {
  CRITICAL: '#f87171',
  HIGH: '#fbbf24',
  MEDIUM: '#38bdf8',
  LOW: '#4ade80',
  UNKNOWN: '#94a3b8',
};

interface Props {
  graph: GraphData;
  environmentNames: Record<string, string>;
  /** Optional deep-link: preselect the node with this exact name. */
  initialSelectedName?: string | null;
  /** §56 overlay: predicted risk per component name, when available. */
  riskOverlay?: Record<
    string,
    { worst_level: ForecastRiskLevel; by_horizon: Partial<Record<ForecastHorizon, ForecastRiskLevel>> }
  >;
}

export default function GraphExplorer({
  graph,
  environmentNames,
  initialSelectedName,
  riskOverlay,
}: Props) {
  const [query, setQuery] = useState('');
  const [typeFilter, setTypeFilter] = useState<string>('ALL');
  const [sourceFilter, setSourceFilter] = useState<string>('ALL');
  const [riskView, setRiskView] = useState(false);
  const [selected, setSelected] = useState<GraphNode | null>(() => {
    if (!initialSelectedName) return null;
    const wanted = initialSelectedName.toLowerCase();
    return graph.nodes.find((n) => n.name.toLowerCase() === wanted) ?? null;
  });

  const layout = useMemo(() => layoutGraph(graph.nodes), [graph.nodes]);

  const visibleNodeIds = useMemo(() => {
    const q = query.trim().toLowerCase();
    const ids = new Set<string>();
    for (const node of graph.nodes) {
      if (typeFilter !== 'ALL' && node.node_type !== typeFilter) continue;
      if (
        q &&
        !node.name.toLowerCase().includes(q) &&
        !(node.external_identifier ?? '').toLowerCase().includes(q)
      ) {
        continue;
      }
      ids.add(node.id);
    }
    return ids;
  }, [graph.nodes, query, typeFilter]);

  const visibleEdges = useMemo(
    () =>
      graph.edges.filter((e) => {
        if (sourceFilter !== 'ALL' && e.source !== sourceFilter) return false;
        return (
          visibleNodeIds.has(e.source_node_id) &&
          visibleNodeIds.has(e.target_node_id)
        );
      }),
    [graph.edges, sourceFilter, visibleNodeIds]
  );

  /** §56: overlay data keyed by node name, computed only when toggled on. */
  const overlayFor = useMemo(() => {
    if (!riskView || !riskOverlay) return () => null;
    return (node: GraphNode) => riskOverlay[node.name] ?? null;
  }, [riskView, riskOverlay]);

  const lines = useMemo(
    () => edgeGeometry(visibleEdges, layout.positions),
    [visibleEdges, layout.positions]
  );

  const nodeById = useMemo(() => {
    const map = new Map<string, GraphNode>();
    for (const n of graph.nodes) map.set(n.id, n);
    return map;
  }, [graph.nodes]);

  const outgoing = useMemo(() => {
    if (!selected) return [];
    return graph.edges
      .filter((e) => e.source_node_id === selected.id)
      .map((e) => nodeById.get(e.target_node_id))
      .filter((n): n is GraphNode => Boolean(n));
  }, [graph.edges, nodeById, selected]);

  const incoming = useMemo(() => {
    if (!selected) return [];
    return graph.edges
      .filter((e) => e.target_node_id === selected.id)
      .map((e) => nodeById.get(e.source_node_id))
      .filter((n): n is GraphNode => Boolean(n));
  }, [graph.edges, nodeById, selected]);

  if (graph.nodes.length === 0) {
    return (
      <p className="rounded-md bg-slate-50 p-6 text-sm text-slate-500">
        No graph data yet. Run a reconciliation or seed the demo project.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-3">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search nodes…"
          className="w-56 rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-indigo-500 focus:outline-none"
        />
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
        >
          {NODE_TYPE_FILTERS.map((t) => (
            <option key={t} value={t}>
              {t === 'ALL' ? 'All types' : t}
            </option>
          ))}
        </select>
        <select
          value={sourceFilter}
          onChange={(e) => setSourceFilter(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
        >
          {SOURCE_FILTERS.map((s) => (
            <option key={s} value={s}>
              {s === 'ALL' ? 'All sources' : sourceLabel(s)}
            </option>
          ))}
        </select>
        {riskOverlay && Object.keys(riskOverlay).length > 0 ? (
          <label className="flex items-center gap-1.5 text-xs text-slate-500">
            <input
              type="checkbox"
              checked={riskView}
              onChange={(e) => setRiskView(e.target.checked)}
            />
            Predicted risk overlay
          </label>
        ) : null}
        <span className="text-xs text-slate-500">
          {visibleNodeIds.size} nodes · {visibleEdges.length} edges
        </span>
      </div>

      <div className="flex gap-4">
        {/* SVG canvas */}
        <div className="flex-1 overflow-auto rounded-md border border-slate-200 bg-white">
          <svg
            viewBox={`0 0 ${layout.width} ${layout.height}`}
            width={layout.width}
            height={layout.height}
            role="img"
            aria-label="Software knowledge graph"
          >
            {lines.map(({ edge, x1, y1, x2, y2 }) => {
              const stroke =
                edge.status === 'STALE'
                  ? '#cbd5e1'
                  : edge.source === 'TRACE' || edge.source === 'LOG'
                    ? '#94a3b8'
                    : '#64748b';
              const dash =
                edge.source === 'TRACE' || edge.source === 'LOG'
                  ? '6 4'
                  : edge.source === 'INFERENCE'
                    ? '2 4'
                    : undefined;
              return (
                <line
                  key={edge.id}
                  x1={x1}
                  y1={y1}
                  x2={x2}
                  y2={y2}
                  stroke={stroke}
                  strokeWidth={1.5}
                  strokeDasharray={dash}
                >
                  <title>
                    {`${edge.edge_type} · ${sourceLabel(edge.source)} · confidence ${confidenceBand(edge.confidence)}`}
                  </title>
                </line>
              );
            })}
            {graph.nodes
              .filter((n) => visibleNodeIds.has(n.id))
              .map((node) => {
                const pos = layout.positions.get(node.id);
                if (!pos) return null;
                const overlay = overlayFor(node);
                const riskStroke =
                  overlay && riskRank(overlay.worst_level) >= riskRank('MEDIUM')
                    ? RISK_STROKE[overlay.worst_level]
                    : undefined;
                return (
                  <g
                    key={node.id}
                    transform={`translate(${pos.x}, ${pos.y})`}
                    onClick={() => setSelected(node)}
                    className="cursor-pointer"
                    data-node-name={node.name}
                  >
                    <rect
                      width={NODE_W}
                      height={NODE_H}
                      rx={8}
                      fill="#fff"
                      stroke={riskStroke ?? typeColor(node.node_type)}
                      strokeWidth={
                        selected?.id === node.id
                          ? 2.5
                          : riskStroke
                            ? 2.5
                            : 1.5
                      }
                    />
                    <rect width={6} height={NODE_H} rx={3} fill={typeColor(node.node_type)} />
                    <text x={14} y={17} fontSize={11} fontWeight={600} fill="#0f172a">
                      {shortLabel(node.name)}
                    </text>
                    <text x={14} y={31} fontSize={9} fill="#64748b">
                      {node.node_type}
                      {node.ownership_team ? ` · ${shortLabel(node.ownership_team, 10)}` : ''}
                    </text>
                    {overlay ? (
                      <>
                        <rect
                          x={NODE_W - 58}
                          y={4}
                          width={54}
                          height={14}
                          rx={4}
                          fill={RISK_STROKE[overlay.worst_level]}
                          opacity={0.9}
                        />
                        <text
                          x={NODE_W - 55}
                          y={14}
                          fontSize={8.5}
                          fontWeight={700}
                          fill="#0f172a"
                        >
                          {`RISK ${overlay.worst_level}`}
                        </text>
                      </>
                    ) : null}
                    <title>
                      {overlay
                        ? `${node.name} — predicted risk ${overlay.worst_level} (${Object.entries(
                            overlay.by_horizon
                          )
                            .map(
                              ([horizon, level]) =>
                                `${HORIZON_LABELS[horizon as ForecastHorizon] ?? horizon}: ${level}`
                            )
                            .join(', ')}) — predicted risk, not current status`
                        : `${node.name} — ${node.node_type}`}
                    </title>
                  </g>
                );
              })}
          </svg>
        </div>

        {/* Detail panel */}
        {selected && (
          <aside className="w-80 shrink-0 rounded-md border border-slate-200 bg-white p-4 text-sm">
            <div className="flex items-start justify-between gap-2">
              <div>
                <h3 className="font-semibold text-slate-900">{selected.name}</h3>
                <p className="text-xs text-slate-500">
                  {selected.node_type}
                  {selected.environment_id && environmentNames[selected.environment_id]
                    ? ` · ${environmentNames[selected.environment_id]}`
                    : ''}
                </p>
              </div>
              <button
                onClick={() => setSelected(null)}
                className="text-slate-400 hover:text-slate-600"
                aria-label="Close details"
              >
                ✕
              </button>
            </div>

            <dl className="mt-3 space-y-1.5 text-xs">
              {selected.ownership_team && (
                <div className="flex justify-between gap-2">
                  <dt className="text-slate-500">Team</dt>
                  <dd className="font-medium text-slate-800">{selected.ownership_team}</dd>
                </div>
              )}
              {selected.repository_url && (
                <div className="flex justify-between gap-2">
                  <dt className="text-slate-500">Repository</dt>
                  <dd className="truncate font-mono text-slate-800">{selected.repository_url}</dd>
                </div>
              )}
              {selected.version && (
                <div className="flex justify-between gap-2">
                  <dt className="text-slate-500">Version</dt>
                  <dd className="font-medium text-slate-800">{selected.version}</dd>
                </div>
              )}
              <div className="flex justify-between gap-2">
                <dt className="text-slate-500">Status</dt>
                <dd className="font-medium text-slate-800">{selected.status}</dd>
              </div>
            </dl>

            <div className="mt-4">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                Depends on
              </h4>
              <ul className="mt-1 space-y-1">
                {outgoing.length === 0 && <li className="text-xs text-slate-400">None</li>}
                {outgoing.map((n) => (
                  <li key={n.id} className="text-xs text-slate-700">
                    {n.name}
                  </li>
                ))}
              </ul>
            </div>

            <div className="mt-3">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                Dependents
              </h4>
              <ul className="mt-1 space-y-1">
                {incoming.length === 0 && <li className="text-xs text-slate-400">None</li>}
                {incoming.map((n) => (
                  <li key={n.id} className="text-xs text-slate-700">
                    {n.name}
                  </li>
                ))}
              </ul>
            </div>

            <Link
              href={`/system-map?node=${selected.entity_kind === 'system_component' && selected.entity_id ? selected.entity_id : selected.id}`}
              className="mt-4 block rounded-md bg-indigo-600 px-3 py-1.5 text-center text-xs font-medium text-white hover:bg-indigo-700"
            >
              Open full details
            </Link>
          </aside>
        )}
      </div>

      {/* Legend — provenance is never hidden (§52) */}
      <div className="flex flex-wrap items-center gap-4 text-xs text-slate-500">
        <span className="font-medium text-slate-600">Provenance:</span>
        <span className="flex items-center gap-1.5">
          <svg width={28} height={6}><line x1={0} y1={3} x2={28} y2={3} stroke="#64748b" strokeWidth={2} /></svg>
          Configured
        </span>
        <span className="flex items-center gap-1.5">
          <svg width={28} height={6}><line x1={0} y1={3} x2={28} y2={3} stroke="#94a3b8" strokeWidth={2} strokeDasharray="6 4" /></svg>
          Observed
        </span>
        <span className="flex items-center gap-1.5">
          <svg width={28} height={6}><line x1={0} y1={3} x2={28} y2={3} stroke="#94a3b8" strokeWidth={2} strokeDasharray="2 4" /></svg>
          Inferred
        </span>
        <span className="flex items-center gap-1.5">
          <svg width={28} height={6}><line x1={0} y1={3} x2={28} y2={3} stroke="#cbd5e1" strokeWidth={2} /></svg>
          Stale
        </span>
        <span className="ml-auto">
          {Object.entries(SOURCE_LABELS).length > 0 &&
            `Edge labels show type · source · confidence band`}
        </span>
      </div>
    </div>
  );
}
