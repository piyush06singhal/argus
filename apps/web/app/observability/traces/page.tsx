import Link from 'next/link';
import { api } from '@/lib/api';
import TraceBrowser from './TraceBrowser';

interface TracesSearchParams {
  page?: string;
}

export const metadata = {
  title: 'Traces',
};

export default async function TracesPage({
  searchParams,
}: {
  searchParams: TracesSearchParams;
}) {
  const rawPage = Number(searchParams.page);
  const page = Number.isInteger(rawPage) && rawPage > 0 ? rawPage : 1;

  try {
    const data = await api.listTraces({ page, pageSize: 20 });

    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Traces</h1>
          <p className="mt-1 text-sm text-slate-400">
            {data.total} trace{data.total === 1 ? '' : 's'} — select a trace to
            inspect its spans
          </p>
        </div>

        <TraceBrowser traces={data.items} />

        {data.total_pages > 1 && (
          <nav className="flex items-center justify-between">
            <p className="text-sm text-slate-500">
              Page {page} of {data.total_pages}
            </p>
            <div className="flex gap-3">
              {page > 1 ? (
                <Link
                  href={`/observability/traces?page=${page - 1}`}
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
                  href={`/observability/traces?page=${page + 1}`}
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
          <h1 className="text-2xl font-semibold text-slate-100">Traces</h1>
          <p className="mt-1 text-sm text-slate-400">Distributed traces</p>
        </div>
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load traces
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