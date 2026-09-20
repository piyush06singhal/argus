'use client';

/**
 * System Map — the real Software Knowledge Graph explorer (§43–52).
 *
 * All data comes from the Phase 2 graph API; nothing is hardcoded. The
 * Explorer tab renders the SVG graph; the other tabs expose impact
 * analysis, environment comparison, snapshots/history, and data quality.
 */

import { useEffect, useMemo, useState } from 'react';
import type { Project, Environment } from '@/lib/api';
import type { GraphData, GraphNode } from '@/lib/graph';
import GraphExplorer from './GraphExplorer';
import ImpactPanel from './ImpactPanel';
import EnvComparePanel from './EnvComparePanel';
import SnapshotPanel from './SnapshotPanel';
import QualityPanel from './QualityPanel';

const TABS = ['Explorer', 'Impact', 'Environments', 'Snapshots', 'Quality'] as const;
type Tab = (typeof TABS)[number];

interface ProjectListItem {
  items: Project[];
}

interface SystemMapState {
  projects: Project[];
  project: Project | null;
  environments: Environment[];
  graph: GraphData;
  components: { id: string; name: string }[];
  error: string | null;
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`Request failed (${res.status})`);
  return (await res.json()) as T;
}

/** Preloaded by the server shell (`page.tsx`) so SSR HTML carries the SVG. */
interface PreloadedState {
  projects: { id: string; name: string }[];
  project: { id: string; name: string } | null;
  graph: GraphData;
  environments: { id: string; name: string }[];
}

export default function SystemMapClient({ initial }: { initial: PreloadedState }) {
  const [state, setState] = useState<SystemMapState>({
    projects: initial.projects as Project[],
    project: (initial.project as Project | null) ?? null,
    environments: initial.environments as Environment[],
    graph: initial.graph,
    components: [],
    error: null,
  });
  const [activeProjectId, setActiveProjectId] = useState<string>(initial.project?.id ?? '');
  const [tab, setTab] = useState<Tab>('Explorer');
  // A preloaded graph renders immediately — no loading screen on SSR.
  const [loading, setLoading] = useState(!initial.project);
  const [preselectedName, setPreselectedName] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      const params = new URLSearchParams(window.location.search);
      const selectedId = params.get('project') ?? '';
      const nodeParam = params.get('node');
      if (nodeParam && !cancelled) setPreselectedName(nodeParam);

      // Fast path: server shell already loaded this project's graph — only
      // the component list (for the Impact panel) is still missing.
      if (
        initial.project &&
        (!selectedId || selectedId === initial.project.id)
      ) {
        try {
          const components = await getJson<{
            items: { id: string; name: string }[];
          }>(`/api/v1/projects/${initial.project.id}/components?page_size=100`);
          if (!cancelled) {
            setState((s) => ({ ...s, components: components.items }));
            setActiveProjectId(initial.project!.id);
            setLoading(false);
          }
        } catch {
          if (!cancelled) setLoading(false);
        }
        return;
      }

      setLoading(true);
      try {
        const projects = await getJson<ProjectListItem>('/api/v1/projects?page_size=100');
        if (projects.items.length === 0) {
          if (!cancelled) {
            setState({
              projects: [],
              project: null,
              environments: [],
              graph: { nodes: [], edges: [] },
              components: [],
              error: null,
            });
            setLoading(false);
          }
          return;
        }
        const project =
          projects.items.find((p) => p.id === selectedId) ?? projects.items[0];

        const [envs, graph, components] = await Promise.all([
          getJson<{ items: Environment[] }>(
            `/api/v1/projects/${project.id}/environments`
          ),
          getJson<GraphData>(`/api/v1/projects/${project.id}/graph`).catch(
            () => ({ nodes: [], edges: [] })
          ),
          getJson<{ items: { id: string; name: string }[] }>(
            `/api/v1/projects/${project.id}/components?page_size=100`
          ),
        ]);

        if (!cancelled) {
          setState({
            projects: projects.items,
            project,
            environments: envs.items,
            graph,
            components: components.items,
            error: null,
          });
          setActiveProjectId(project.id);
          setLoading(false);
        }
      } catch (e) {
        if (!cancelled) {
          setState((s) => ({
            ...s,
            error: e instanceof Error ? e.message : 'Failed to load system map',
          }));
          setLoading(false);
        }
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  const environmentNames = useMemo(() => {
    const map: Record<string, string> = {};
    for (const e of state.environments) map[e.id] = e.name;
    return map;
  }, [state.environments]);

  function switchProject(id: string) {
    const url = new URL(window.location.href);
    url.searchParams.set('project', id);
    window.history.replaceState(null, '', url.toString());
    window.location.reload();
  }

  if (loading) {
    return (
      <div className="p-8">
        <h1 className="text-2xl font-bold text-slate-100">System Map</h1>
        <p className="mt-4 text-sm text-slate-400">Loading knowledge graph…</p>
      </div>
    );
  }

  if (state.error) {
    return (
      <div className="p-8">
        <h1 className="text-2xl font-bold text-slate-100">System Map</h1>
        <p className="mt-4 rounded-md bg-red-900/30 p-4 text-sm text-red-300">
          {state.error}
        </p>
      </div>
    );
  }

  if (!state.project) {
    return (
      <div className="p-8">
        <h1 className="text-2xl font-bold text-slate-100">System Map</h1>
        <p className="mt-4 text-slate-400">
          No projects found. Create a project first.
        </p>
      </div>
    );
  }

  return (
    <div className="p-8">
      <div className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">System Map</h1>
          <p className="mt-1 text-sm text-slate-400">
            Software Knowledge Graph — {state.graph.nodes.length} nodes ·{' '}
            {state.graph.edges.length} edges
          </p>
        </div>
        <select
          value={activeProjectId}
          onChange={(e) => switchProject(e.target.value)}
          className="rounded-md border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-200"
        >
          {state.projects.map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
      </div>

      <div className="mb-4 flex gap-1 border-b border-slate-800">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm font-medium transition-colors ${
              tab === t
                ? 'border-b-2 border-indigo-500 text-slate-100'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-6">
        {tab === 'Explorer' && (
          <GraphExplorer
            graph={state.graph}
            environmentNames={environmentNames}
            initialSelectedName={preselectedName}
          />
        )}
        {tab === 'Impact' && (
          <ImpactPanel components={state.components} />
        )}
        {tab === 'Environments' && (
          <EnvComparePanel
            projectId={state.project.id}
            environments={state.environments.map((e) => ({ id: e.id, name: e.name }))}
          />
        )}
        {tab === 'Snapshots' && <SnapshotPanel projectId={state.project.id} />}
        {tab === 'Quality' && <QualityPanel projectId={state.project.id} />}
      </div>
    </div>
  );
}
