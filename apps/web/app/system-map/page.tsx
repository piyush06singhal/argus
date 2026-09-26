/**
 * System Map — server shell (§43–52).
 *
 * Preloads the first project's knowledge graph **on the server** so the
 * initial HTML already contains the SVG system map (crawlers and deep links
 * get a real render, not a loading spinner). All interactivity — tab
 * switching, project reload, impact/env/snapshot/quality panels — lives in
 * the client component, which rehydrates from this preloaded state.
 */

import type { GraphData } from '@/lib/graph';
import { apiFetch } from '@/lib/api';
import SystemMapClient from './SystemMapClient';

export const dynamic = 'force-dynamic';

interface ProjectListItem {
  items: { id: string; name: string; slug: string }[];
}

/**
 * Every backend route requires a bearer token (hardening W1), so the shell
 * fetches through ``apiFetch`` like every other server component. The raw
 * ``fetch`` this used to send carried no ``Authorization`` header — with auth
 * enforced each call came back ``401`` and the map rendered its loading
 * spinner forever: HTTP 200 with an empty page, which is the worst way to fail
 * because the status code says healthy while the user sees nothing.
 */
async function getJson<T>(path: string): Promise<T> {
  return apiFetch<T>(path);
}

/** The demo project if present, else the first project with a known graph. */
async function pickProject(
  projects: ProjectListItem['items']
): Promise<ProjectListItem['items'][number] | null> {
  if (projects.length === 0) return null;
  const demo = projects.find((p) => p.slug === 'argus-demo-commerce');
  if (demo) return demo;
  for (const p of projects.slice(0, 10)) {
    const graph = await getJson<GraphData>(`/api/v1/projects/${p.id}/graph`).catch(
      () => ({ nodes: [], edges: [] }) as GraphData
    );
    if (graph.nodes.length > 0) return p;
  }
  return projects[0];
}

export default async function SystemMapPage() {
  let initial: {
    projects: ProjectListItem['items'];
    project: ProjectListItem['items'][number] | null;
    graph: GraphData;
    environments: { id: string; name: string }[];
  } = { projects: [], project: null, graph: { nodes: [], edges: [] }, environments: [] };

  try {
    const projects = await getJson<ProjectListItem>('/api/v1/projects?page_size=100');
    const project = await pickProject(projects.items);
    if (project) {
      const [envs, graph] = await Promise.all([
        getJson<{ items: { id: string; name: string }[] }>(
          `/api/v1/projects/${project.id}/environments`
        ).catch(() => ({ items: [] })),
        getJson<GraphData>(`/api/v1/projects/${project.id}/graph`).catch(
          () => ({ nodes: [], edges: [] }) as GraphData
        ),
      ]);
      initial = {
        projects: projects.items,
        project,
        graph,
        environments: envs.items,
      };
    }
  } catch {
    // API unreachable — the client shows the error state and can retry.
  }

  return <SystemMapClient initial={initial} />;
}
