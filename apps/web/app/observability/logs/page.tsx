import Link from 'next/link';
import { api, formatDate, type LogLevel, type LogRecord } from '@/lib/api';

const LEVEL_STYLES: Record<LogLevel, string> = {
  TRACE: 'bg-slate-700/40 text-slate-400',
  DEBUG: 'bg-argus-info/15 text-argus-info',
  INFO: 'bg-argus-accent/15 text-argus-accent',
  WARN: 'bg-argus-warning/15 text-argus-warning',
  ERROR: 'bg-argus-error/15 text-argus-error',
};

interface LogsSearchParams {
  page?: string;
  level?: string;
}

export const metadata = {
  title: 'Logs',
};

export default async function LogsPage({
  searchParams,
}: {
  searchParams: LogsSearchParams;
}) {
  const rawPage = Number(searchParams.page);
  const page = Number.isInteger(rawPage) && rawPage > 0 ? rawPage : 1;
  const level = typeof searchParams.level === 'string' ? searchParams.level : '';

  try {
    const data = await api.listLogs({ page, pageSize: 50, level });

    const buildListHref = (nextPage: number) => {
      const params = new URLSearchParams();
      params.set('page', String(nextPage));
      if (level !== '') {
        params.set('level', level);
      }
      return `/observability/logs?${params.toString()}`;
    };

    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Logs</h1>
          <p className="mt-1 text-sm text-slate-400">
            {data.total} log record{data.total === 1 ? '' : 's'}
          </p>
        </div>

        <form
          method="get"
          action="/observability/logs"
          className="flex flex-wrap items-end gap-3"
        >
          <div className="flex flex-col gap-1">
            <label
              htmlFor="level"
              className="text-xs font-medium text-slate-400"
            >
              Level
            </label>
            <select id="level" name="level" defaultValue={level} className="input">
              <option value="">All levels</option>
              <option value="INFO">INFO</option>
              <option value="WARN">WARN</option>
              <option value="ERROR">ERROR</option>
              <option value="DEBUG">DEBUG</option>
              <option value="TRACE">TRACE</option>
            </select>
          </div>
          <button type="submit" className="btn-primary">
            Apply filter
          </button>
          {level !== '' && (
            <Link href="/observability/logs" className="btn-ghost">
              Clear filter
            </Link>
          )}
        </form>

        {data.items.length === 0 ? (
          <div className="card py-16 text-center">
            <p className="text-sm text-slate-400">
              {level !== ''
                ? `No ${level} log records found.`
                : 'No log records found.'}
            </p>
          </div>
        ) : (
          <div className="card overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-4 py-3">Timestamp</th>
                  <th className="px-4 py-3">Level</th>
                  <th className="px-4 py-3">Message</th>
                  <th className="px-4 py-3">Service</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {data.items.map((log: LogRecord) => (
                  <tr
                    key={log.id}
                    className="align-top transition-colors hover:bg-slate-800/40"
                  >
                    <td className="whitespace-nowrap px-4 py-3 font-mono text-xs text-slate-400">
                      {formatDate(log.timestamp)}
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`badge font-mono ${
                          LEVEL_STYLES[log.level] ?? LEVEL_STYLES.INFO
                        }`}
                      >
                        {log.level}
                      </span>
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-200">
                      <p className="whitespace-pre-line">{log.message}</p>
                      {log.trace_id ? (
                        <p className="mt-1 text-[11px] text-slate-500">
                          trace: {log.trace_id}
                        </p>
                      ) : null}
                    </td>
                    <td className="px-4 py-3 text-slate-400">{log.service}</td>
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
          <h1 className="text-2xl font-semibold text-slate-100">Logs</h1>
          <p className="mt-1 text-sm text-slate-400">Log records</p>
        </div>
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load logs
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