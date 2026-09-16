import Link from 'next/link';
import { api, formatDate, type IngestionSourceHealth } from '@/lib/api';

/**
 * Ingestion Health (Phase 1 §45) — registered sources, 7-day volume,
 * dead-letter failures, and pipeline health.
 */

const STATUS_STYLES: Record<string, string> = {
  HEALTHY: 'bg-argus-success/15 text-argus-success',
  DEGRADED: 'bg-argus-warning/15 text-argus-warning',
  FAILING: 'bg-argus-error/15 text-argus-error',
  UNKNOWN: 'bg-slate-700/40 text-slate-400',
};

export const metadata = {
  title: 'Ingestion Health',
};

export default async function IngestionHealthPage() {
  const failing: string[] = [];

  const load = async () => {
    const sources = await api.listSources();
    const stats = await api.ingestionStats();
    const deadLetter = await api.deadLetter(50);
    const health = await api.sourcesHealth();
    return { sources: sources.items, stats, deadLetter, health };
  };

  let data;
  try {
    data = await load();
  } catch {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">
            Ingestion Health
          </h1>
          <p className="mt-1 text-sm text-slate-400">
            Registered sources and pipeline status
          </p>
        </div>
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load ingestion health
          </h2>
          <p className="mt-2 text-sm text-slate-400">
            Could not reach the ARGUS backend. Please ensure it is running on
            port 8000.
          </p>
        </div>
      </div>
    );
  }

  const { sources, stats, deadLetter, health } = data;
  const unhealthy = health.filter(
    (h) => h.status === 'FAILING' || h.status === 'DEGRADED'
  );
  if (unhealthy.length) {
    failing.push(`${unhealthy.length} source(s) not healthy`);
  }
  if (deadLetter.length) {
    failing.push(`${deadLetter.length} dead-lettered job(s) in last window`);
  }

  const statusOrder = ['HEALTHY', 'DEGRADED', 'FAILING', 'UNKNOWN'];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">
          Ingestion Health
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          The async queue drains into the pipeline; sources are tracked for
          liveness. {stats.events_ingested_7d.toLocaleString()} events ingested
          in the last 7 days.
        </p>
      </div>

      {failing.length > 0 ? (
        <div className="card border-argus-warning/40">
          <h2 className="font-medium text-argus-warning">Attention needed</h2>
          <ul className="mt-2 list-inside list-disc text-sm text-slate-400">
            {failing.map((f) => (
              <li key={f}>{f}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <div className="card">
          <p className="text-xs uppercase tracking-wider text-slate-500">
            Active sources
          </p>
          <p className="mt-1 font-mono text-2xl font-semibold text-slate-200">
            {stats.source_count}
          </p>
        </div>
        <div className="card">
          <p className="text-xs uppercase tracking-wider text-slate-500">
            Healthy / failing
          </p>
          <p className="mt-1 font-mono text-2xl font-semibold text-slate-200">
            {stats.healthy_sources}
            <span className="text-slate-500"> / {stats.failing_sources}</span>
          </p>
        </div>
        <div className="card">
          <p className="text-xs uppercase tracking-wider text-slate-500">
            Events (7d)
          </p>
          <p className="mt-1 font-mono text-2xl font-semibold text-slate-200">
            {stats.events_ingested_7d.toLocaleString()}
          </p>
        </div>
        <div className="card">
          <p className="text-xs uppercase tracking-wider text-slate-500">
            Dead-lettered
          </p>
          <p className="mt-1 font-mono text-2xl font-semibold text-slate-200">
            {stats.dead_letter_count}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section>
          <h2 className="mb-3 text-sm font-medium text-slate-300">
            Registered sources
          </h2>
          {sources.length === 0 ? (
            <div className="card py-10 text-center">
              <p className="text-sm text-slate-400">No sources registered.</p>
            </div>
          ) : (
            <div className="card overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-800 text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                    <th className="px-4 py-3">Source</th>
                    <th className="px-4 py-3">Type</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3">Events</th>
                    <th className="px-4 py-3">Last success</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800">
                  {sources.map((source) => (
                    <tr
                      key={source.id}
                      className="transition-colors hover:bg-slate-800/40"
                    >
                      <td className="px-4 py-3">
                        <p className="text-slate-200">{source.name}</p>
                        {source.last_error ? (
                          <p className="mt-0.5 max-w-[18rem] truncate text-[11px] text-argus-error">
                            {source.last_error}
                          </p>
                        ) : null}
                      </td>
                      <td className="px-4 py-3 font-mono text-xs text-slate-400">
                        {source.source_type}
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`badge ${
                            STATUS_STYLES[source.status] ?? STATUS_STYLES.UNKNOWN
                          }`}
                        >
                          {source.status}
                        </span>
                      </td>
                      <td className="px-4 py-3 font-mono text-slate-300">
                        {source.event_count}
                      </td>
                      <td className="px-4 py-3 text-xs text-slate-400">
                        {formatDate(source.last_success_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section>
          <h2 className="mb-3 text-sm font-medium text-slate-300">
            Source health (7d)
          </h2>
          {health.length === 0 ? (
            <div className="card py-10 text-center">
              <p className="text-sm text-slate-400">
                No source activity in the last 7 days.
              </p>
            </div>
          ) : (
            <div className="space-y-2">
              {health.map((h: IngestionSourceHealth) => (
                <div
                  key={h.id}
                  className="card flex items-center justify-between"
                >
                  <div>
                    <p className="text-sm font-medium text-slate-200">
                      {h.name}
                    </p>
                    <p className="text-xs text-slate-500">
                      {h.events_7d} events · {h.error_count} errors ·{' '}
                      {h.consecutive_errors} consecutive
                    </p>
                  </div>
                  <span
                    className={`badge ${
                      STATUS_STYLES[h.status] ?? STATUS_STYLES.UNKNOWN
                    }`}
                  >
                    {h.status}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>

      <section>
        <h2 className="mb-3 text-sm font-medium text-slate-300">
          Status breakdown
        </h2>
        <div className="card flex flex-wrap gap-3">
          {statusOrder.map((status) => {
            const count = stats.status_counts[status] ?? 0;
            if (count === 0) {
              return null;
            }
            return (
              <span key={status} className="badge pl-3">
                <span
                  className={`mr-2 inline-block h-2 w-2 rounded-full ${
                    status === 'HEALTHY'
                      ? 'bg-argus-success'
                      : status === 'DEGRADED'
                        ? 'bg-argus-warning'
                        : status === 'FAILING'
                          ? 'bg-argus-error'
                          : 'bg-slate-500'
                  }`}
                />
                {status}: {count}
              </span>
            );
          })}
          {Object.values(stats.status_counts).every((c) => c === 0) ? (
            <p className="text-sm text-slate-400">No sources registered.</p>
          ) : null}
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-medium text-slate-300">
          Dead-letter queue (exhausted retries)
        </h2>
        {deadLetter.length === 0 ? (
          <div className="card py-10 text-center">
            <p className="text-sm text-slate-400">
              No dead-lettered jobs — the pipeline is healthy.
            </p>
          </div>
        ) : (
          <div className="card overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-4 py-3">Failed at</th>
                  <th className="px-4 py-3">Source</th>
                  <th className="px-4 py-3">Error</th>
                  <th className="px-4 py-3">Retries</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {deadLetter.map((dl) => (
                  <tr key={dl.id} className="hover:bg-slate-800/40">
                    <td className="whitespace-nowrap px-4 py-3 font-mono text-xs text-slate-400">
                      {formatDate(dl.failed_at)}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-400">
                      {dl.source ?? '—'}
                    </td>
                    <td className="px-4 py-3 text-slate-300">
                      <p className="max-w-[24rem] truncate font-mono text-xs">
                        {dl.error_type}: {dl.error_message}
                      </p>
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-400">
                      {dl.retry_count}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="mt-3">
          <Link href="/observability" className="btn-ghost">
            ← Back to Observability
          </Link>
        </p>
      </section>
    </div>
  );
}