import Link from 'next/link';
import { api, formatDate, type Deployment, type DeploymentStatus } from '@/lib/api';

const STATUS_STYLES: Record<DeploymentStatus, string> = {
  in_progress: 'bg-argus-warning/15 text-argus-warning',
  successful: 'bg-argus-success/15 text-argus-success',
  failed: 'bg-argus-error/15 text-argus-error',
  canceled: 'bg-slate-700/40 text-slate-400',
};

interface DeploymentsSearchParams {
  page?: string;
}

export const metadata = {
  title: 'Deployments',
};

export default async function DeploymentsPage({
  searchParams,
}: {
  searchParams: DeploymentsSearchParams;
}) {
  const rawPage = Number(searchParams.page);
  const page = Number.isInteger(rawPage) && rawPage > 0 ? rawPage : 1;

  try {
    const data = await api.listDeployments(page, 20);

    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Deployments</h1>
          <p className="mt-1 text-sm text-slate-400">
            {data.total} deployment{data.total === 1 ? '' : 's'}
          </p>
        </div>

        {data.items.length === 0 ? (
          <div className="card py-16 text-center">
            <p className="text-sm text-slate-400">No deployments recorded.</p>
          </div>
        ) : (
          <div className="card overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-4 py-3">Component</th>
                  <th className="px-4 py-3">Environment</th>
                  <th className="px-4 py-3">Version</th>
                  <th className="px-4 py-3">Commit</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Deployed at</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {data.items.map((deployment: Deployment) => (
                  <tr
                    key={deployment.id}
                    className="transition-colors hover:bg-slate-800/40"
                  >
                    <td className="px-4 py-3">
                      <p className="font-medium text-slate-200">
                        {deployment.component_name ?? deployment.component_id}
                      </p>
                      {deployment.project_name ? (
                        <p className="mt-0.5 text-xs text-slate-500">
                          {deployment.project_name}
                        </p>
                      ) : null}
                    </td>
                    <td className="px-4 py-3">
                      <span className="badge bg-slate-800 text-slate-300">
                        {deployment.environment_name ?? deployment.environment_id}
                      </span>
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-300">
                      v{deployment.version}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-400">
                      {deployment.commit
                        ? deployment.commit.slice(0, 8)
                        : '—'}
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`badge ${
                          STATUS_STYLES[deployment.status] ??
                          STATUS_STYLES.in_progress
                        }`}
                      >
                        {deployment.status}
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 font-mono text-xs text-slate-400">
                      {formatDate(deployment.deployed_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {data.total_pages > 1 && (
          <nav className="flex items-center justify-between">
            <p className="text-sm text-slate-500">
              Page {page} of {data.total_pages}
            </p>
            <div className="flex gap-3">
              {page > 1 ? (
                <Link
                  href={`/deployments?page=${page - 1}`}
                  className="btn-ghost"
                >
                  Previous
                </Link>
              ) : (
                <span className="btn-ghost cursor-not-allowed opacity-40">
                  Previous
                </span>
              )}
              {page < data.total_pages ? (
                <Link
                  href={`/deployments?page=${page + 1}`}
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
        )}
      </div>
    );
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Unknown error occurred';
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Deployments</h1>
          <p className="mt-1 text-sm text-slate-400">Deployment history</p>
        </div>
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load deployments
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