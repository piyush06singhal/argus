import Link from 'next/link';

import {
  api,
  formatDate,
  type DebugSession,
  type Incident,
} from '@/lib/api';
import { sessionStatusStyle, sessionTitle } from '@/lib/debugger';
import StartDebugSessionButton from '../../StartSessionButton';

export const metadata = {
  title: 'Debug Sessions',
};

export const dynamic = 'force-dynamic';

function Card({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="card">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="font-medium text-slate-200">{title}</h2>
        {subtitle ? <p className="text-xs text-slate-500">{subtitle}</p> : null}
      </div>
      {children}
    </section>
  );
}

/**
 * Per-incident debugger entry (§34): the incident's existing sessions and the
 * controls to start a new one.
 *
 * `hasSnapshot` is read from the incident's already-pinned snapshot when the
 * API reported one, so the "index code, then start" path is only offered when
 * there is genuinely nothing indexed yet.
 */
export default async function IncidentDebuggerPage({
  params,
  searchParams,
}: {
  params: { incidentId: string };
  searchParams: { project_id?: string };
}) {
  const { incidentId } = params;
  const projectId =
    typeof searchParams.project_id === 'string'
      ? searchParams.project_id
      : '';

  let incident: Incident | null = null;
  let sessions: DebugSession[] = [];
  let error: string | null = null;

  try {
    incident = await api.getIncident(incidentId);
  } catch (cause: unknown) {
    error = cause instanceof Error ? cause.message : 'unknown error';
  }

  if (incident !== null) {
    try {
      const response = await api.listDebugSessions(
        incidentId,
        projectId || undefined
      );
      sessions = response.items;
    } catch {
      sessions = [];
    }
  }

  if (incident === null) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-slate-100">
          Debug sessions
        </h1>
        <Card title="Incident unavailable">
          <p className="text-sm text-slate-400">
            {error
              ? `The incident could not be loaded: ${error}`
              : 'The incident could not be loaded.'}
          </p>
          <p className="mt-2 text-xs text-slate-500">
            <Link href="/incidents" className="text-argus-accent underline">
              ← Back to incidents
            </Link>
          </p>
        </Card>
      </div>
    );
  }

  const resolvedProject = projectId || incident.project_id;

  return (
    <div className="space-y-6">
      <div>
        <p className="text-xs uppercase tracking-wide text-slate-500">
          Incident{' '}
          <Link
            href={`/incidents/${incident.id}`}
            className="text-argus-accent underline"
          >
            {incident.id.slice(0, 8)}
          </Link>
        </p>
        <h1 className="mt-1 text-2xl font-semibold text-slate-100">
          Debug sessions — {incident.title}
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          {incident.severity} · {incident.status} · detected{' '}
          {formatDate(incident.detected_at)}
        </p>
      </div>

      <Card
        title="Start a debug session"
        subtitle="§34 — analysis runs against the incident's pinned code revision"
      >
        <StartDebugSessionButton
          incidentId={incident.id}
          projectId={resolvedProject}
          hasSnapshot={false}
        />
      </Card>

      <Card
        title="Existing sessions"
        subtitle={`${sessions.length} session(s) for this incident`}
      >
        {sessions.length === 0 ? (
          <p className="text-sm text-slate-400">
            No debug sessions yet. Start one above — ARGUS will map the
            incident&apos;s failing traces to the indexed source and analyse
            only from stored evidence.
          </p>
        ) : (
          <ul className="divide-y divide-slate-800">
            {sessions.map((session) => (
              <li
                key={session.id}
                className="flex flex-wrap items-center justify-between gap-3 py-3"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm text-slate-200">
                    {sessionTitle(session)}
                  </p>
                  <p className="text-xs text-slate-500">
                    {session.status} · started {formatDate(session.created_at)}
                    {session.snapshot_id
                      ? ` · snapshot ${session.snapshot_id.slice(0, 8)}`
                      : ' · no snapshot'}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <span
                    className={`badge ${sessionStatusStyle(session.status)}`}
                  >
                    {session.status}
                  </span>
                  <Link
                    href={`/debugger/${session.id}?project_id=${encodeURIComponent(resolvedProject)}`}
                    className="badge bg-argus-accent/20 text-argus-accent hover:bg-argus-accent/30"
                  >
                    Open →
                  </Link>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
