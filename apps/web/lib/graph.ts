/**
 * Typed data layer for the ARGUS Software Knowledge Graph (Phase 2).
 *
 * Mirrors the backend contracts in `apps/api/app/schemas/graph.py`:
 * nodes mirror canonical entities (`entity_kind` + `entity_id`), edges carry
 * strict provenance (`source`) and confidence (evidence strength — never a
 * causality probability). Paths include the `/api/v1` prefix; see `lib/api.ts`
 * for the server/browser URL resolution.
 */

import { apiFetch, type PaginatedResponse } from './api';

// ---------------------------------------------------------------------------
// Enums (mirrors app/models/graph.py)
// ---------------------------------------------------------------------------

export const NODE_TYPES = [
  'PROJECT',
  'ENVIRONMENT',
  'COMPONENT',
  'APPLICATION',
  'SERVICE',
  'WORKER',
  'DATABASE',
  'CACHE',
  'QUEUE',
  'EXTERNAL_API',
  'REPOSITORY',
  'INFRASTRUCTURE',
  'ENDPOINT',
  'UNKNOWN',
] as const;
export type NodeType = (typeof NODE_TYPES)[number];

export const EDGE_TYPES = [
  'CONTAINS',
  'DEPENDS_ON',
  'DEPLOYS',
  'CALLS',
  'READS_FROM',
  'WRITES_TO',
  'PUBLISHES_TO',
  'CONSUMES_FROM',
  'DEPLOYED_AS',
  'IMPLEMENTS',
  'HOSTS',
  'RELATED_TO',
] as const;
export type EdgeType = (typeof EDGE_TYPES)[number];

export const EDGE_SOURCES = [
  'MANUAL',
  'CONFIGURATION',
  'TRACE',
  'LOG',
  'METRIC',
  'DEPLOYMENT',
  'REPOSITORY',
  'IMPORT',
  'INFERENCE',
  'MOCK',
] as const;
export type EdgeSource = (typeof EDGE_SOURCES)[number];

export type EdgeStatus = 'ACTIVE' | 'STALE' | 'DISABLED' | 'UNKNOWN';
export type NodeStatus = 'ACTIVE' | 'STALE' | 'DISABLED' | 'UNKNOWN';
export type Criticality = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL' | 'UNKNOWN';

// ---------------------------------------------------------------------------
// Response shapes (mirrors app/schemas/graph.py)
// ---------------------------------------------------------------------------

export interface GraphNode {
  id: string;
  project_id: string;
  node_type: NodeType;
  name: string;
  external_identifier?: string | null;
  description?: string | null;
  status: NodeStatus;
  criticality: Criticality;
  language?: string | null;
  framework?: string | null;
  runtime?: string | null;
  version?: string | null;
  repository_url?: string | null;
  documentation_url?: string | null;
  ownership_team?: string | null;
  environment_id?: string | null;
  entity_kind: string;
  entity_id?: string | null;
  metadata?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface GraphEdge {
  id: string;
  project_id: string;
  source_node_id: string;
  target_node_id: string;
  edge_type: EdgeType;
  dependency_type?: string | null;
  confidence: number;
  source: EdgeSource;
  status: EdgeStatus;
  environment_id?: string | null;
  metadata?: {
    sources?: string[];
    dependency_kind?: string;
    [key: string]: unknown;
  } | null;
  first_seen_at?: string | null;
  last_seen_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface SearchHit {
  kind: 'node' | 'endpoint' | 'repository' | 'alias';
  id: string;
  name: string;
  project_id: string;
  environment_id?: string | null;
  node_type?: NodeType | null;
  score: number;
}

export interface PathResult {
  found: boolean;
  path: GraphNode[];
  edges: GraphEdge[];
  total_hops: number;
}

export interface Snapshot {
  id: string;
  project_id: string;
  environment_id?: string | null;
  snapshot_version: number;
  node_count: number;
  edge_count: number;
  source: EdgeSource;
  caption?: string | null;
  previous_snapshot_id?: string | null;
  created_at: string;
}

export interface SnapshotDiff {
  a_id: string;
  b_id: string;
  added_nodes: string[];
  removed_nodes: string[];
  added_edges: string[];
  removed_edges: string[];
  added_node_names: string[];
  removed_node_names: string[];
}

export interface EnvSummary {
  environment_id: string | null;
  name: string;
  node_count: number;
  edge_count: number;
  component_count: number;
}

export interface EnvDiffItem {
  kind: 'node' | 'edge' | 'component' | 'endpoint' | 'version';
  category: { node_type: string | null; name: string; combined_key: string };
  key: string;
}

export interface EnvComparison {
  project_id: string;
  environment_a: EnvSummary;
  environment_b: EnvSummary;
  added: EnvDiffItem[];
  removed: EnvDiffItem[];
  changed: EnvDiffItem[];
  labels: Record<string, string>;
}

export interface ReconcileResult {
  reconciliation_run_id: string;
  project_id: string;
  environment_id?: string | null;
  input_source: EdgeSource;
  nodes_created: number;
  edges_created: number;
  edges_updated: number;
  edges_marked_stale: number;
  errors?: unknown[] | null;
  status: string;
  started_at: string;
  finished_at?: string | null;
}

export interface HealthRow {
  check_type: string;
  severity: 'INFO' | 'WARNING' | 'ERROR';
  count: number;
  latest_detected_at?: string | null;
}

export interface GraphHealth {
  project_id: string;
  node_count: number;
  edge_count: number;
  last_reconciled_at?: string | null;
  data_quality: HealthRow[];
  ok: boolean;
}

export interface DependencySets {
  component_id: string;
  direction: 'outgoing' | 'incoming';
  direct: GraphNode[];
  transitive: GraphNode[];
  direct_count: number;
  transitive_count: number;
}

export interface ImpactItem {
  node: GraphNode;
  path: string[];
  hops: number;
}

export interface ImpactResult {
  source_id: string;
  label: string;
  count: number;
  items: ImpactItem[];
  relation: 'downstream';
}

export interface ServiceEndpoint {
  id: string;
  project_id: string;
  environment_id?: string | null;
  component_id: string;
  method: string;
  path_template: string;
  original_paths?: string[] | null;
  is_external: boolean;
  metadata?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface ComponentOwner {
  id: string;
  project_id: string;
  component_id: string;
  team: string;
  owner_name?: string | null;
  contact_email?: string | null;
  repository_owner?: string | null;
  created_at: string;
  updated_at: string;
}

export interface DiscoveryRecord {
  id: string;
  project_id: string;
  environment_id?: string | null;
  discovered_name: string;
  suggested_node_type: NodeType;
  identity_hint?: Record<string, unknown> | null;
  evidence_count: number;
  evidence_sources?: string[] | null;
  confidence?: number | null;
  status: 'PENDING' | 'REGISTERED' | 'IGNORED';
  metadata?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// graphApi — typed client for the Phase 2 graph endpoints
// ---------------------------------------------------------------------------

export const graphApi = {
  getGraph: (projectId: string) =>
    apiFetch<GraphData>(`/api/v1/projects/${projectId}/graph`),

  listNodes: (projectId: string, page = 1, pageSize = 100) =>
    apiFetch<PaginatedResponse<GraphNode>>(
      `/api/v1/projects/${projectId}/graph/nodes?page=${page}&page_size=${pageSize}`
    ),

  listEdges: (projectId: string, page = 1, pageSize = 100) =>
    apiFetch<PaginatedResponse<GraphEdge>>(
      `/api/v1/projects/${projectId}/graph/edges?page=${page}&page_size=${pageSize}`
    ),

  search: (projectId: string, query: string) =>
    apiFetch<{ query: string; results: SearchHit[] }>(
      `/api/v1/projects/${projectId}/graph/search?q=${encodeURIComponent(query)}`
    ),

  findPath: (
    projectId: string,
    sourceId: string,
    targetId: string,
    maxDepth = 10
  ) =>
    apiFetch<PathResult>(
      `/api/v1/projects/${projectId}/graph/paths?source_id=${sourceId}&target_id=${targetId}&max_depth=${maxDepth}`
    ),

  listSnapshots: (projectId: string, page = 1, pageSize = 20) =>
    apiFetch<PaginatedResponse<Snapshot>>(
      `/api/v1/projects/${projectId}/graph/snapshots?page=${page}&page_size=${pageSize}`
    ),

  createSnapshot: (projectId: string) =>
    apiFetch<Snapshot>(`/api/v1/projects/${projectId}/graph/snapshots`, {
      method: 'POST',
      body: JSON.stringify({ source: 'MANUAL' }),
    }),

  getSnapshot: (snapshotId: string) =>
    apiFetch<Snapshot & { nodes: GraphNode[]; edges: GraphEdge[]; signature: string[] }>(
      `/api/v1/graph/snapshots/${snapshotId}`
    ),

  diffSnapshots: (aId: string, bId: string) =>
    apiFetch<SnapshotDiff>(`/api/v1/graph/snapshots/${aId}/diff/${bId}`),

  compareEnvironments: (projectId: string, aId: string, bId: string) =>
    apiFetch<EnvComparison>(
      `/api/v1/projects/${projectId}/graph/environments/compare?environment_a=${aId}&environment_b=${bId}`
    ),

  reconcile: (projectId: string, environmentId?: string) => {
    const qs = environmentId ? `?environment_id=${environmentId}` : '';
    return apiFetch<ReconcileResult>(
      `/api/v1/projects/${projectId}/graph/reconcile${qs}`,
      { method: 'POST' }
    );
  },

  health: (projectId: string) =>
    apiFetch<GraphHealth>(`/api/v1/projects/${projectId}/graph/health`),

  listDiscovery: (projectId: string, page = 1, pageSize = 50) =>
    apiFetch<PaginatedResponse<DiscoveryRecord>>(
      `/api/v1/projects/${projectId}/graph/discovery?page=${page}&page_size=${pageSize}`
    ),

  listDataQuality: (projectId: string, page = 1, pageSize = 50) =>
    apiFetch<PaginatedResponse<{ id: string; check_type: string; severity: string; detail: Record<string, unknown> | null; detected_at: string }>>(
      `/api/v1/projects/${projectId}/graph/data-quality?page=${page}&page_size=${pageSize}`
    ),

  listProjectEndpoints: (projectId: string) =>
    apiFetch<PaginatedResponse<ServiceEndpoint>>(
      `/api/v1/projects/${projectId}/graph/endpoints?page=1&page_size=100`
    ),

  listComponentEndpoints: (componentId: string) =>
    apiFetch<PaginatedResponse<ServiceEndpoint>>(
      `/api/v1/components/${componentId}/endpoints?page=1&page_size=100`
    ),

  dependencies: (componentId: string) =>
    apiFetch<DependencySets>(
      `/api/v1/components/${componentId}/graph/dependencies`
    ),

  dependents: (componentId: string) =>
    apiFetch<DependencySets>(
      `/api/v1/components/${componentId}/graph/dependents`
    ),

  neighbors: (componentId: string) =>
    apiFetch<DependencySets>(
      `/api/v1/components/${componentId}/graph/neighbors`
    ),

  impact: (componentId: string, maxDepth = 10) =>
    apiFetch<ImpactResult>(
      `/api/v1/components/${componentId}/graph/impact?max_depth=${maxDepth}`
    ),

  getOwner: (componentId: string) =>
    apiFetch<ComponentOwner>(`/api/v1/components/${componentId}/owner`),

  aliases: (componentId: string) =>
    apiFetch<PaginatedResponse<{ id: string; alias: string; source: EdgeSource; confidence: number | null }>>(
      `/api/v1/components/${componentId}/aliases`
    ),
};

// ---------------------------------------------------------------------------
// Provenance presentation (§52 — do not hide uncertainty)
// ---------------------------------------------------------------------------

/** Display label per edge provenance — uncertainty stays visible. */
export const SOURCE_LABELS: Record<EdgeSource, string> = {
  MANUAL: 'Configured',
  CONFIGURATION: 'Configured',
  TRACE: 'Observed',
  LOG: 'Observed',
  METRIC: 'Observed',
  DEPLOYMENT: 'Observed',
  REPOSITORY: 'Observed',
  IMPORT: 'Configured',
  INFERENCE: 'Inferred',
  MOCK: 'Inferred',
};

/** Confidence bands — evidence strength, not causality probability. */
export function confidenceBand(confidence: number | null | undefined): string {
  if (confidence == null) return 'unknown';
  if (confidence >= 0.9) return 'high';
  if (confidence >= 0.6) return 'medium';
  return 'low';
}

export function sourceLabel(source: EdgeSource | string): string {
  return SOURCE_LABELS[source as EdgeSource] ?? source;
}
