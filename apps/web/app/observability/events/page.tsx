import Link from 'next/link';
import {
  apiFetch,
  formatDate,
  type ObservabilityEvent,
} from '@/lib/api';

/**
 * Events explorer (Phase 1 §37) — the normalized observability event stream.
 */

export const metadata = {
  title: 'Events',
};

interface EventsSearchParams {
  page?: string;
  event_type?: string;
  severity?: string;
}

const EVENT_TYPES = [
  'LOG',
  'METRIC',
  'TRACE',
  'DEPLOYMENT',
  'CONFIGURATION_CHANGE',
  'HEALTH_CHECK',
  'SYSTEM_EVENT',
];

const SEVERITIES = ['DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL'];

export default async function EventsPage({
  searchParams,
}: {
  searchParams: EventsSearchParams;
}) {
  const rawPage = Number(searchParams.page);
  const page = Number.isInteger(rawPage) && rawPage > 0 ? rawPage : 1;
  const eventType =
    typeof searchParams.event_type === 'string'
      ? searchParams.event_type
      : '';
  const severity =
    typeof searchParams.severity === 'string' ? searchParams.severity : '';

  try {
    const params = new URLSearchParams({
      page: String(page),
      page_size: String(50),
    });
    if (eventType !== '') {
      params.set('event_type', eventType);
    }
    if (severity !== '') {
      params.set('severity', severity);
    }
    const data = await apiFetchEvents(params);

    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Events</h1>
          <p className="mt-1 text-sm text-slate-400">
            {data.total} normalized observability event
            {data.total === 1 ? '' : 's'}
          </p>
        </div>

        <form method="get" action="/observability/events" className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1">
            <label htmlFor="event_type" className="text-xs font-medium text-slate-400">
              Event type
            </label>
            <select id="event_type" name="event_type" defaultValue={eventType} className="input">
              <option value="">All types</option>
              {EVENT_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="severity" className="text-xs font-medium text-slate-400">
              Severity
            </label>
            <select id="severity" name="severity" defaultValue={severity} className="input">
              <option value="">All severities</option>
              {SEVERITIES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <button type="submit" className="btn-primary">
            Apply filter
          </button>
          {(eventType !== '' || severity !== '') && (
            <Link href="/observability/events" className="btn-ghost">
              Clear
            </Link>
          )}
        </form>

        {data.items.length === 0 ? (
          <div className="card py-16 text-center">
            <p className="text-sm text-slate-400">No events found.</p>
          </div>
        ) : (
          <div className="card overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-4 py-3">Timestamp</th>
                  <th className="px-4 py-3">Type</th>
                  <th className="px-4 py-3">Severity</th>
                  <th className="px-4 py-3">Source</th>
                  <th className="px-4 py-3">Payload</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {data.items.map((event: ObservabilityEvent) => (
                  <tr key={event.id} className="hover:bg-slate-800/40">
                    <td className="whitespace-nowrap px-4 py-3 font-mono text-xs text-slate-400">
                      {formatDate(event.timestamp)}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-argus-accent">
                      {event.event_type}
                    </td>
                    <td className="px-4 py-3">
                      {event.severity ? (
                        <span className="badge">{event.severity}</span>
                      ) : (
                        <span className="text-slate-600">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-400">
                      {event.source}
                      {event.trace_id ? (
                        <p className="mt-0.5 text-[11px] text-slate-500">
                          trace: {event.trace_id}
                        </p>
                      ) : null}
                    </td>
                    <td className="px-4 py-3">
                      <p className="max-w-[28rem] truncate font-mono text-xs text-slate-300">
                        {event.payload
                          ? JSON.stringify(event.payload)
                          : '—'}
                      </p>
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
                <Link href={buildHref(page - 1, eventType, severity)} className="btn-ghost">
                  Previous
                </Link>
              ) : (
                <span className="btn-ghost cursor-not-allowed opacity-40">Previous</span>
              )}
              {page < data.total_pages ? (
                <Link href={buildHref(page + 1, eventType, severity)} className="btn-primary">
                  Next
                </Link>
              ) : (
                <span className="btn-primary cursor-not-allowed opacity-40">Next</span>
              )}
            </div>
          </nav>
        )}
      </div>
    );
  } catch {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Events</h1>
        </div>
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">Failed to load events</h2>
          <p className="mt-2 text-sm text-slate-400">
            Could not reach the ARGUS backend, or filtering by an event type/severity
            is not supported. Back to <Link href="/observability" className="text-argus-accent underline">Observability</Link>.
          </p>
        </div>
      </div>
    );
  }
}

function buildHref(page: number, eventType: string, severity: string) {
  const params = new URLSearchParams({ page: String(page) });
  if (eventType !== '') params.set('event_type', eventType);
  if (severity !== '') params.set('severity', severity);
  return `/observability/events?${params.toString()}`;
}

// The api helper exposes listEvents without filter params; call through the
// raw wrapper so event_type/severity filters reach the backend.
async function apiFetchEvents(params: URLSearchParams) {
  return apiFetch<{ items: ObservabilityEvent[]; total: number; page: number; page_size: number; total_pages: number }>(
    `/api/v1/observability/events?${params.toString()}`
  );
}