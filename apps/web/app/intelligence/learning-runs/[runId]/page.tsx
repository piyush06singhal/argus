import Link from 'next/link';

import { api, formatDate, type LearningRunDetail } from '@/lib/api';

export const metadata = {
  title: 'Learning run',
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

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className="text-sm text-slate-200">{value}</dd>
    </div>
  );
}

/**
 * One run, audited (§79).
 *
 * This is the page that answers "why does ARGUS believe that?" at the process
 * level: which window it read, which algorithm versions ran, which events it
 * consumed, and — importantly — which it could not process and why. The events
 * list is shown whole, including the ones that produced nothing, because a
 * consumer that silently drops input is indistinguishable from one that never
 * received it.
 */
export default async function LearningRunPage({
  params,
  searchParams,
}: {
  params: { runId: string };
  searchParams: { project_id?: string };
}) {
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  let detail: LearningRunDetail | null = null;
  let error: string | null = null;
  try {
    detail = await api.getLearningRun(params.runId, projectId || undefined);
  } catch (caught) {
    error = caught instanceof Error ? caught.message : String(caught);
  }

  if (error || !detail) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-slate-100">Learning run</h1>
        <Card title="Could not load" subtitle="The run may belong to another project">
          <p className="text-sm text-argus-warning">{error ?? 'Not found.'}</p>
        </Card>
      </div>
    );
  }

  const { run, events } = detail;
  const unprocessable = events.filter((event) => event.unprocessable_reason);

  return (
    <div className="space-y-6">
      <div>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold text-slate-100">
            Run <span className="font-mono">{run.id.slice(0, 8)}</span>
          </h1>
          <span
            className={`badge ${
              run.status === 'COMPLETED'
                ? 'bg-argus-success/15 text-argus-success'
                : run.status === 'FAILED'
                  ? 'bg-argus-error/20 text-argus-error'
                  : 'bg-slate-800 text-slate-300'
            }`}
          >
            {run.status}
          </span>
          <span className="badge bg-slate-800 text-slate-400">{run.trigger}</span>
        </div>
        <p className="mt-2 text-xs text-slate-500">
          Started {formatDate(run.started_at)} · completed{' '}
          {formatDate(run.completed_at)} · data cutoff{' '}
          {formatDate(run.data_cutoff)}
        </p>
        {run.error_summary ? (
          <p className="mt-2 text-sm text-argus-error">{run.error_summary}</p>
        ) : null}
      </div>

      <Card title="What it produced" subtitle="Counts come from the run row, not from a template">
        <dl className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Field label="Events consumed" value={run.events_processed} />
          <Field
            label="Experiences"
            value={`${run.experiences_created} created / ${run.experiences_updated} updated`}
          />
          <Field label="Records flagged" value={run.records_flagged} />
          <Field label="Patterns discovered" value={run.patterns_discovered} />
          <Field label="Patterns validated" value={run.patterns_validated} />
          <Field label="Patterns rejected" value={run.patterns_rejected} />
          <Field label="Knowledge activated" value={run.knowledge_activated} />
          <Field
            label="Relationships"
            value={`${run.relationships_created ?? 0} created / ${
              run.relationships_updated ?? 0
            } refreshed`}
          />
        </dl>
      </Card>

      <Card
        title="Algorithm versions"
        subtitle="§68, §79 — the exact producers of everything this run wrote"
      >
        {Object.keys(run.algorithm_versions).length === 0 ? (
          <p className="text-sm text-slate-400">No versions recorded.</p>
        ) : (
          <ul className="space-y-1 text-sm text-slate-400">
            {Object.entries(run.algorithm_versions).map(([name, value]) => (
              <li key={name}>
                · <span className="font-mono text-xs">{name}</span>{' '}
                {typeof value === 'string' ? value : JSON.stringify(value)}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card
        title={`${events.length} event${events.length === 1 ? '' : 's'} consumed`}
        subtitle="§64 — each event is processed once, and its fate is recorded"
      >
        {events.length === 0 ? (
          <p className="text-sm text-slate-400">
            This run consumed no events. That is a legitimate outcome: a run over
            unchanged history has nothing new to learn and says so instead of
            inventing activity.
          </p>
        ) : (
          <>
            {unprocessable.length > 0 ? (
              <p className="mb-3 text-sm text-argus-warning">
                {unprocessable.length} event
                {unprocessable.length === 1 ? '' : 's'} could not produce an
                experience. They are recorded rather than retried forever.
              </p>
            ) : null}
            <ul className="space-y-2 text-sm">
              {events.map((event) => (
                <li
                  key={event.id}
                  className="rounded-md border border-slate-800 bg-slate-900/40 p-3"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs text-slate-300">
                      {event.event_type}
                    </span>
                    <span className="badge bg-slate-800 text-slate-400">
                      {event.provenance}
                    </span>
                    {event.processed_at ? (
                      <span className="text-xs text-slate-500">
                        processed {formatDate(event.processed_at)}
                      </span>
                    ) : (
                      <span className="badge bg-argus-info/20 text-argus-info">
                        pending
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-xs text-slate-500">
                    subject{' '}
                    <span className="font-mono">{event.subject_id.slice(0, 8)}</span>{' '}
                    · occurred {formatDate(event.occurred_at)}
                    {event.unprocessable_reason
                      ? ` · not processable: ${event.unprocessable_reason}`
                      : ''}
                  </p>
                </li>
              ))}
            </ul>
          </>
        )}
      </Card>

      <Card title="Related" subtitle="Where this run's output is visible">
        <div className="flex flex-wrap gap-3 text-sm">
          <Link href="/intelligence/patterns" className="text-argus-accent">
            Patterns discovered by this run →
          </Link>
          <Link href="/intelligence/relationships" className="text-argus-accent">
            Relationships derived →
          </Link>
        </div>
      </Card>
    </div>
  );
}
