import Link from 'next/link';
import { api, formatDate, type Project, type ProjectStatus } from '@/lib/api';

const STATUS_STYLES: Record<ProjectStatus, string> = {
  active: 'bg-argus-success/10 text-argus-success',
  pending: 'bg-argus-warning/10 text-argus-warning',
  archived: 'bg-slate-700/40 text-slate-400',
};

export async function generateMetadata({
  searchParams,
}: {
  searchParams: { page?: string };
}) {
  const page = Number(searchParams.page) || 1;
  return { title: `Projects — Page ${page}` };
}

export default async function ProjectsPage({
  searchParams,
}: {
  searchParams: { page?: string };
}) {
  const requestedPage = Number(searchParams.page);
  const page = Number.isInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1;

  try {
    const data = await api.listProjects(page, 20);

    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Projects</h1>
          <p className="mt-1 text-sm text-slate-400">
            {data.total} project{data.total === 1 ? '' : 's'} tracked by ARGUS
          </p>
        </div>

        {data.items.length === 0 ? (
          <div className="card py-16 text-center">
            <p className="text-sm text-slate-400">No projects found.</p>
          </div>
        ) : (
          <div className="card overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-4 py-3">Name</th>
                  <th className="px-4 py-3">Slug</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Created</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {data.items.map((project: Project) => (
                  <tr
                    key={project.id}
                    className="transition-colors hover:bg-slate-800/40"
                  >
                    <td className="px-4 py-3">
                      <Link
                        href={`/projects/${project.id}`}
                        className="font-medium text-argus-accent hover:text-argus-accent-hover"
                      >
                        {project.name}
                      </Link>
                      {project.description ? (
                        <p className="mt-0.5 max-w-md truncate text-xs text-slate-500">
                          {project.description}
                        </p>
                      ) : null}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-400">
                      {project.slug}
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`badge ${
                          STATUS_STYLES[project.status] ?? STATUS_STYLES.active
                        }`}
                      >
                        {project.status}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-slate-400">
                      {formatDate(project.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <PaginationNav page={page} totalPages={data.total_pages} />
      </div>
    );
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Unknown error occurred';
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Projects</h1>
          <p className="mt-1 text-sm text-slate-400">
            Projects tracked by ARGUS
          </p>
        </div>
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load projects
          </h2>
          <p className="mt-2 text-sm text-slate-400">
            Could not reach the ARGUS backend. Please ensure it is running on
            port 8000. ({message})
          </p>
        </div>
      </div>
    );
  }
}

function PaginationNav({
  page,
  totalPages,
}: {
  page: number;
  totalPages: number;
}) {
  if (totalPages <= 1) {
    return null;
  }
  return (
    <nav className="flex items-center justify-between">
      <p className="text-sm text-slate-500">
        Page {page} of {totalPages}
      </p>
      <div className="flex gap-3">
        {page > 1 ? (
          <Link
            href={`/projects?page=${page - 1}`}
            className="btn-ghost"
          >
            Previous
          </Link>
        ) : (
          <span className="btn-ghost cursor-not-allowed opacity-40">
            Previous
          </span>
        )}
        {page < totalPages ? (
          <Link
            href={`/projects?page=${page + 1}`}
            className="btn-primary"
          >
            Next
          </Link>
        ) : (
          <span className="btn-primary cursor-not-allowed opacity-40">
            Next
          </span>
        )}
      </div>
    </nav>
  );
}