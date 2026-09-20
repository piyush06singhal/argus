import { describe, expect, it } from 'vitest';

import {
  edgeGeometry,
  layoutGraph,
  NODE_H,
  NODE_W,
  shortLabel,
  typeColor,
} from '../graph-layout';
import type { GraphEdge, GraphNode, NodeType } from '../graph';

function node(
  id: string,
  name: string,
  node_type: NodeType = 'SERVICE'
): GraphNode {
  return {
    id,
    project_id: 'p1',
    node_type,
    name,
    status: 'ACTIVE',
    criticality: 'UNKNOWN',
    entity_kind: 'system_component',
    entity_id: id,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  } as GraphNode;
}

function edge(
  id: string,
  source: string,
  target: string,
  edge_type: GraphEdge['edge_type'] = 'DEPENDS_ON'
): GraphEdge {
  return {
    id,
    project_id: 'p1',
    source_node_id: source,
    target_node_id: target,
    edge_type,
    confidence: 1,
    source: 'CONFIGURATION',
    status: 'ACTIVE',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  } as GraphEdge;
}

describe('layoutGraph', () => {
  it('is deterministic — same input, same positions', () => {
    const nodes = [
      node('a', 'Zeta'),
      node('b', 'Alpha'),
      node('c', 'Postgres', 'DATABASE'),
      node('d', 'prod', 'ENVIRONMENT'),
      node('e', 'demo', 'PROJECT'),
      node('f', 'repo', 'REPOSITORY'),
    ];
    const first = layoutGraph(nodes);
    const second = layoutGraph(nodes.slice().reverse());

    expect(first.positions.size).toBe(6);
    expect(second.positions.get('a')).toEqual(first.positions.get('a'));
    expect(second.positions.get('f')).toEqual(first.positions.get('f'));

    // Bands: project < environment < service < database < repository.
    const proj = first.positions.get('e')!;
    const env = first.positions.get('d')!;
    const svc = first.positions.get('b')!;
    const db = first.positions.get('c')!;
    const repo = first.positions.get('f')!;
    expect(proj.y).toBeLessThan(env.y);
    expect(env.y).toBeLessThan(svc.y);
    expect(svc.y).toBeLessThan(db.y);
    expect(db.y).toBeLessThan(repo.y);
  });

  it('orders same-band nodes by name', () => {
    const result = layoutGraph([node('a', 'Zeta'), node('b', 'Alpha')]);
    expect(result.positions.get('b')!.x).toBeLessThan(
      result.positions.get('a')!.x
    );
  });

  it('sizes the viewport to the content', () => {
    const result = layoutGraph([node('a', 'One'), node('b', 'Two')]);
    expect(result.width).toBeGreaterThanOrEqual(NODE_W * 2);
    expect(result.height).toBeGreaterThanOrEqual(NODE_H);
  });

  it('handles an empty graph', () => {
    const result = layoutGraph([]);
    expect(result.positions.size).toBe(0);
    expect(result.width).toBe(NODE_W);
  });
});

describe('edgeGeometry', () => {
  it('maps endpoints to node centers and drops dangling edges', () => {
    const nodes = [node('a', 'A'), node('b', 'B')];
    const layout = layoutGraph(nodes);
    const edges = [
      edge('e1', 'a', 'b'),
      edge('e2', 'a', 'missing'),
    ];
    const lines = edgeGeometry(edges, layout.positions);
    expect(lines).toHaveLength(1);
    expect(lines[0].x1).toBeCloseTo(layout.positions.get('a')!.x + NODE_W / 2);
    expect(lines[0].y2).toBeCloseTo(layout.positions.get('b')!.y + NODE_H / 2);
  });
});

describe('presentation helpers', () => {
  it('truncates long labels', () => {
    expect(shortLabel('short')).toBe('short');
    expect(shortLabel('a-very-long-component-name-here')).toContain('…');
    expect(shortLabel('a-very-long-component-name-here').length).toBeLessThanOrEqual(18);
  });

  it('colors known and unknown types', () => {
    expect(typeColor('SERVICE')).toMatch(/^#/);
    expect(typeColor('NOT_A_TYPE')).toBe(typeColor('UNKNOWN'));
  });
});
