import Link from 'next/link';
import { api, formatDate, type Metric } from '@/lib/api';

interface MetricsSearchParams {
  page?: string;
  name?: string;
}

export const metadata = {
  title: 'Metrics',
};

export default async function MetricsPage({
  searchParams,
}: {
  searchParams: MetricsSearchParams;
}) {
  const rawPage = Number(searchParams.page);
  const page = Number.isInteger(rawPage) && rawPage > 0 ? rawPage : 1;
  const name = typeof searchParams.name === 'string' ? searchParams.name : '';

  try {
    const data = await api.listMetrics({ page, pageSize: 50, name });

    const buildListHref = (nextPage: number) => {
      const params = new URLSearchParams();
      params.set('page', String(nextPage));
      if (name !== '') {
        params.set('name', name);
      }
      return `/observability/metrics?${params.toString()}`;
    };

    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Metrics</h1>
          <p className="mt-1 text-sm text-slate-400">
            {data.total} metric record{data.total === 1 ? '' : 's'}
          </p>
        </div>

        <form
          method="get"
          action="/observability/metrics"
          className="flex flex-wrap items-end gap-3"
        >
          <div className="flex flex-col gap-1">
            <label
              htmlFor="name"
              className="text-xs font-medium text-slate-400"
            >
              Metric name
            </label>
            <input
              id="name"
              name="name"
              type="text"
              defaultValue={name}
              placeholder="e.g. http_request_duration"
              className="input w-64"
            />
          </div>
          <button type="submit" className="btn-primary">
            Apply filter
          </button>
          {name !== '' && (
            <Link href="/observability/metrics" className="btn-ghost">
              Clear filter
            </Link>
          )}
        </form>

        {data.items.length === 0 ? (
          <div className="card py-16 text-center">
            <p className="text-sm text-slate-400">
              {name !== ''
                ? `No metrics match "${name}".`
                : 'No metric records found.'}
            </p>
          </div>
        ) : (
          <div className="card overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-4 py-3">Timestamp</th>
                  <th className="px-4 py-3">Metric</th>
                  <th className="px-4 py-3">Type</th>
                  <th className="px-4 py-3 text-right">Value</th>
                  <th className="px-4 py-3">Unit</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {data.items.map((metric: Metric) => (
                  <tr
                    key={metric.id}
                    className="transition-colors hover:bg-slate-800/40"
                  >
                    <td className="whitespace-nowrap px-4 py-3 font-mono text-xs text-slate-400">
                      {formatDate(metric.timestamp)}
                    </td>
                    <td className="px-4 py-3">
                      <p className="font-mono text-xs text-slate-200">
                        {metric.metric_name}
                      </p>
                      {metric.labels && Object.keys(metric.labels).length > 0 ? (
                        <p className="mt-0.5 text-[11px] text-slate-500">
                          {Object.entries(metric.labels)
                            .map(([k, v]) => `${k}=${v}`)
                            .join(', ')}
                        </p>
                      ) : null}
                    </td>
                    <td className="px-4 py-3">
                      <span className="badge bg-slate-800 text-slate-300">
                        {metric.metric_type}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-sm text-slate-100">
                      {metric.value}
                    </td>
                    <td className="px-4 py-3 text-slate-400">
                      {metric.unit ?? '—'}
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
                <Link href={buildListHref(page - 1)} className="btn-ghost">
                  Previous
                </Link>
              ) : (
                <span className="btn-ghost cursor-not-allowed opacity-40">
                  Previous
                </span>
              )}
              {page < data.total_pages ? (
                <Link href={buildListHref(page + 1)} className="btn-primary">
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
          <h1 className="text-2xl font-semibold text-slate-100">Metrics</h1>
          <p className="mt-1 text-sm text-slate-400">Metric records</p>
        </div>
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load metrics
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