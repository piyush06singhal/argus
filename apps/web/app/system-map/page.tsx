import { apiFetch } from '@/lib/api';
import type { Project, Component, Dependency } from '@/lib/api';
import type { Metadata } from 'next';

export const metadata: Metadata = {
  title: 'System Map — ARGUS Intelligence',
};

interface SystemMapData {
  project: Project;
  components: Component[];
  dependencies: Dependency[];
}

async function fetchSystemMap(projectId?: string): Promise<SystemMapData | null> {
  try {
    // Fetch project list to get the target project
    const projectsResp = await apiFetch<{ items: Project[]; total: number }>(
      '/api/v1/projects?page_size=100'
    );
    const projects = projectsResp.items;
    if (projects.length === 0) return null;

    const project = projectId
      ? projects.find((p) => p.id === projectId) ?? projects[0]
      : projects[0];

    const [componentsResp, depsResp] = await Promise.all([
      apiFetch<{ items: Component[]; total: number }>(
        `/api/v1/projects/${project.id}/components?page_size=100`
      ),
      apiFetch<{ items: Dependency[]; total: number }>(
        `/api/v1/projects/${project.id}/dependencies?page_size=100`
      ),
    ]);

    return {
      project,
      components: componentsResp.items,
      dependencies: depsResp.items,
    };
  } catch {
    return null;
  }
}

/** Build a tree of component ID → child IDs from the dependency list. */
function buildTree(
  components: Component[],
  dependencies: Dependency[],
): { rootIds: string[]; children: Record<string, string[]> } {
  const childMap: Record<string, string[]> = {};
  const parentIds = new Set<string>();
  for (const dep of dependencies) {
    if (!childMap[dep.source_component_id]) childMap[dep.source_component_id] = [];
    childMap[dep.source_component_id].push(dep.target_component_id);
    parentIds.add(dep.target_component_id);
  }
  const rootIds = components
    .map((c) => c.id)
    .filter((id) => !parentIds.has(id));
  // If every node is a child, fall back to first component as root
  if (rootIds.length === 0 && components.length > 0) rootIds.push(components[0].id);
  return { rootIds, children: childMap };
}

function renderTree(
  nodeId: string,
  componentMap: Record<string, Component>,
  children: Record<string, string[]>,
  prefix: string,
  isLast: boolean,
): React.ReactNode[] {
  const component = componentMap[nodeId];
  const label = component?.name ?? nodeId.slice(0, 8);
  const typeLabel = component?.type ? ` [${component.type}]` : '';
  const connector = prefix === '' ? '' : isLast ? '└── ' : '├── ';
  const childPrefix = prefix === '' ? '' : isLast ? '    ' : '│   ';
  const lines: React.ReactNode[] = [];

  lines.push(
    <div key={`line-${nodeId}`} className="flex items-center font-mono text-sm">
      <span className="mr-1 text-slate-600">{connector}</span>
      <span className="text-slate-100">{label}</span>
      <span className="ml-1 text-slate-500 text-xs">{typeLabel}</span>
    </div>,
  );

  const childIds = children[nodeId] ?? [];
  for (let i = 0; i < childIds.length; i++) {
    lines.push(
      <div key={`indent-${nodeId}-${i}`} className="font-mono text-sm text-slate-600">
        {prefix}{childPrefix}
      </div>,
    );
    lines.push(
      ...renderTree(childIds[i], componentMap, children, prefix + childPrefix, i === childIds.length - 1),
    );
  }
  return lines;
}

export default async function SystemMapPage({
  searchParams,
}: {
  searchParams: Promise<{ project?: string }>;
}) {
  const params = await searchParams;
  const data = await fetchSystemMap(params.project);

  if (!data) {
    return (
      <div className="p-8">
        <h1 className="text-2xl font-bold text-slate-100">System Map</h1>
        <p className="mt-4 text-slate-400">No projects found. Create a project first.</p>
      </div>
    );
  }

  const { project, components, dependencies } = data;
  const componentMap: Record<string, Component> = {};
  for (const c of components) componentMap[c.id] = c;
  const { rootIds, children } = buildTree(components, dependencies);

  return (
    <div className="p-8">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-slate-100">System Map</h1>
        <p className="mt-1 text-sm text-slate-400">
          Component dependency graph — built from live data
        </p>
      </div>

      <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-6">
        <h2 className="mb-1 font-semibold text-slate-100">{project.name}</h2>
        <p className="mb-4 text-xs text-slate-500">
          {components.length} component{components.length !== 1 && 's'} · {dependencies.length} dependenc{dependencies.length !== 1 ? 'ies' : 'y'}
        </p>
        {components.length === 0 ? (
          <p className="text-sm text-slate-400">No components registered for this project yet.</p>
        ) : (
          <div className="space-y-0.5 bg-slate-950 rounded-md px-4 py-3">
            {rootIds.map((rootId, idx) => (
              <div key={rootId} className="space-y-0.5">
                {idx > 0 && <div className="h-2" />}
                {renderTree(rootId, componentMap, children, '', true)}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Legend */}
      <div className="mt-6 flex flex-wrap gap-4 text-xs text-slate-500">
        {['SERVICE', 'DATABASE', 'CACHE', 'EXTERNAL_API', 'APPLICATION', 'WORKER', 'QUEUE'].map((t) => (
          <span key={t} className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full bg-slate-500" />
            {t}
          </span>
        ))}
      </div>
    </div>
  );
}
