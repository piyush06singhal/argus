/**
 * Deterministic layout + presentation helpers for the SVG graph explorer.
 *
 * Pure functions only — unit-tested with vitest and shared between the
 * server-rendered page and the client explorer. Layout is fully
 * deterministic: nodes are banded by type family, ordered by name, and
 * assigned grid coordinates — the same graph always renders identically.
 */

import type { GraphEdge, GraphNode, NodeType } from './graph';

/** Vertical band per node family — anchor types frame the software tiers. */
const TYPE_BAND: Record<NodeType, number> = {
  PROJECT: 0,
  ENVIRONMENT: 1,
  APPLICATION: 2,
  SERVICE: 2,
  WORKER: 2,
  COMPONENT: 2,
  EXTERNAL_API: 2,
  DATABASE: 3,
  CACHE: 3,
  QUEUE: 3,
  INFRASTRUCTURE: 3,
  REPOSITORY: 4,
  ENDPOINT: 4,
  UNKNOWN: 4,
};

export const NODE_W = 148;
export const NODE_H = 40;
export const COL_GAP = 28;
export const BAND_GAP = 88;

export interface LayoutPosition {
  x: number;
  y: number;
}

export interface LayoutResult {
  positions: Map<string, LayoutPosition>;
  width: number;
  height: number;
}

/**
 * Grid layout: band rows by node family, columns ordered by name (then id
 * as a stable tiebreaker). Returns viewport size for the SVG viewBox.
 */
export function layoutGraph(nodes: GraphNode[]): LayoutResult {
  const byBand = new Map<number, GraphNode[]>();
  for (const node of nodes) {
    const band = TYPE_BAND[node.node_type] ?? TYPE_BAND.UNKNOWN;
    const bucket = byBand.get(band);
    if (bucket) {
      bucket.push(node);
    } else {
      byBand.set(band, [node]);
    }
  }

  const positions = new Map<string, LayoutPosition>();
  let maxCols = 0;
  let maxBand = 0;
  const bands = Array.from(byBand.entries());
  for (const entry of bands) {
    const band = entry[0];
    const members = entry[1];
    const sorted = members.slice().sort(
      (a: GraphNode, b: GraphNode) =>
        a.name.localeCompare(b.name) || a.id.localeCompare(b.id)
    );
    sorted.forEach((node: GraphNode, col: number) => {
      positions.set(node.id, {
        x: col * (NODE_W + COL_GAP),
        y: band * (NODE_H + BAND_GAP),
      });
    });
    maxCols = Math.max(maxCols, sorted.length);
    maxBand = Math.max(maxBand, band);
  }

  return {
    positions,
    width: Math.max(maxCols * (NODE_W + COL_GAP) - COL_GAP, NODE_W),
    height: maxBand * (NODE_H + BAND_GAP) + NODE_H,
  };
}

export interface EdgeGeometry {
  edge: GraphEdge;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

/** Line coordinates between positioned endpoints; drops dangling edges. */
export function edgeGeometry(
  edges: GraphEdge[],
  positions: Map<string, LayoutPosition>
): EdgeGeometry[] {
  const out: EdgeGeometry[] = [];
  for (const edge of edges) {
    const a = positions.get(edge.source_node_id);
    const b = positions.get(edge.target_node_id);
    if (!a || !b) continue;
    out.push({
      edge,
      x1: a.x + NODE_W / 2,
      y1: a.y + NODE_H / 2,
      x2: b.x + NODE_W / 2,
      y2: b.y + NODE_H / 2,
    });
  }
  return out;
}

const TYPE_COLORS: Record<string, string> = {
  PROJECT: '#6366f1',
  ENVIRONMENT: '#0ea5e9',
  APPLICATION: '#8b5cf6',
  SERVICE: '#10b981',
  WORKER: '#14b8a6',
  COMPONENT: '#64748b',
  DATABASE: '#f59e0b',
  CACHE: '#ef4444',
  QUEUE: '#f97316',
  EXTERNAL_API: '#ec4899',
  REPOSITORY: '#3b82f6',
  INFRASTRUCTURE: '#64748b',
  ENDPOINT: '#22c55e',
  UNKNOWN: '#94a3b8',
};

export function typeColor(type: string): string {
  return TYPE_COLORS[type] ?? TYPE_COLORS.UNKNOWN;
}

/** Truncate long node labels for SVG text rendering. */
export function shortLabel(name: string, max = 18): string {
  return name.length > max ? `${name.slice(0, max - 1)}…` : name;
}
