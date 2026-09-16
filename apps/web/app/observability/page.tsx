import Link from 'next/link';
import { api } from '@/lib/api';

/**
 * Observability Center (Phase 1 §53) — entry point to the explore
 * surfaces: events, logs, metrics, traces, and ingestion health.
 */

interface ExplorerCard {
  title: string;
  description: string;
  href: string;
  metric?: { label: string; value: number };
  accentClass: string;
}

export const metadata = {
  title: 'Observability',
};

export default async function ObservabilityPage() {
  let counts: Record<string, number> = {
    events: 0,
    logs: 0,
    metrics: 0,
    traces: 0,
  };
  let statsError: string | null = null;

  try {
    const [events, logs, metrics, traces] = await Promise.all([
      api.listEvents({ page: 1, pageSize: 1 }),
      api.listLogs({ page: 1, pageSize: 1 }),
      api.listMetrics({ page: 1, pageSize: 1 }),
      api.listTraces({ page: 1, pageSize: 1 }),
    ]);
    counts = {
      events: events.total,
      logs: logs.total,
      metrics: metrics.total,
      traces: traces.total,
    };
  } catch {
    statsError = 'Observability data unavailable right now.';
  }

  const cards: ExplorerCard[] = [
    {
      title: 'Events',
      description: 'Normalized observability events across all sources.',
      href: '/observability/events',
      metric: { label: 'total events', value: counts.events },
      accentClass: 'border-t-argus-info',
    },
    {
      title: 'Logs',
      description: 'Structured log records, filterable by level and service.',
      href: '/observability/logs',
      metric: { label: 'log records', value: counts.logs },
      accentClass: 'border-t-argus-accent',
    },
    {
      title: 'Metrics',
      description: 'Time-series metric records with labels and units.',
      href: '/observability/metrics',
      metric: { label: 'metric records', value: counts.metrics },
      accentClass: 'border-t-argus-success',
    },
    {
      title: 'Traces',
      description: 'Distributed traces with span reconstruction.',
      href: '/observability/traces',
      metric: { label: 'traces', value: counts.traces },
      accentClass: 'border-t-argus-warning',
    },
    {
      title: 'Ingestion Health',
      description:
        'Registered sources, worker status, and dead-letter inspection.',
      href: '/ingestion-health',
      accentClass: 'border-t-argus-error',
    },
  ];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Observability</h1>
        <p className="mt-1 text-sm text-slate-400">
          Telemetry received, normalized, correlated, and stored by the ARGUS
          ingestion pipeline.
        </p>
      </div>

      {statsError ? (
        <div className="card border-argus-error/40">
          <p className="text-sm text-slate-400">{statsError}</p>
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5">
        {cards.map((card) => (
          <Link
            key={card.href}
            href={card.href}
            className={`card border-t-2 ${card.accentClass} transition-colors hover:bg-slate-800/40`}
          >
            <h2 className="text-base font-medium text-slate-100">
              {card.title}
            </h2>
            <p className="mt-1 text-xs leading-relaxed text-slate-400">
              {card.description}
            </p>
            {card.metric ? (
              <p className="mt-3 font-mono text-2xl font-semibold text-slate-200">
                {card.metric.value.toLocaleString()}
                <span className="ml-2 text-xs font-normal text-slate-500">
                  {card.metric.label}
                </span>
              </p>
            ) : null}
          </Link>
        ))}
      </div>
    </div>
  );
}