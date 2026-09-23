import Link from 'next/link';

import { api, formatDate, type ExperienceDetail } from '@/lib/api';
import {
  dataQualityLabel,
  dataQualityStyle,
  isMineable,
  outcomeLabel,
  timelineStageLabel,
} from '@/lib/intelligence';

export const metadata = {
  title: 'Episode',
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
 * One episode, end to end (§8, §9, §10, §54).
 *
 * The signatures are rendered as structured features rather than prose, because
 * that is what they are: normalised features the similarity engine compares, not
 * a narrative. Showing them raw is also the honest choice — any summarising
 * sentence on this page would be written by the viewer, not by the data.
 */
export default async function ExperienceDetailPage({
  params,
  searchParams,
}: {
  params: { experienceId: string };
  searchParams: { project_id?: string };
}) {
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  if (!projectId) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-slate-100">Episode</h1>
        <Card title="Project required" subtitle="Experiences are project-scoped">
          <p className="text-sm text-slate-400">
            Open this page from{' '}
            <Link href="/intelligence/experiences" className="text-argus-accent">
              Reliability memory
            </Link>{' '}
            so the scope travels with the link.
          </p>
        </Card>
      </div>
    );
  }

  let detail: ExperienceDetail | null = null;
  let error: string | null = null;
  try {
    detail = await api.getExperience(params.experienceId, projectId);
  } catch (caught) {
    error = caught instanceof Error ? caught.message : String(caught);
  }

  if (error || !detail) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-slate-100">Episode</h1>
        <Card title="Could not load" subtitle="The row may belong to another project">
          <p className="text-sm text-argus-warning">{error ?? 'Not found.'}</p>
        </Card>
      </div>
    );
  }

  const { experience, incident, component, remediation, timeline } = detail;

  return (
    <div className="space-y-6">
      <div>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold text-slate-100">
            {experience.failure_label || 'Unclassified failure'}
          </h1>
          <span className={`badge ${dataQualityStyle(experience.data_quality)}`}>
            {dataQualityLabel(experience.data_quality)}
          </span>
          <span className="badge bg-slate-800 text-slate-400">
            {outcomeLabel(experience.outcome)}
          </span>
          {!isMineable(experience) ? (
            <span className="badge bg-argus-warning/15 text-argus-warning">
              Excluded from learning
            </span>
          ) : null}
        </div>
        <p className="mt-2 text-xs text-slate-500">
          {formatDate(experience.start_time)} → {formatDate(experience.end_time)} ·{' '}
          {experience.recovery_seconds === null ||
          experience.recovery_seconds === undefined
            ? 'recovery time unmeasured'
            : `${experience.recovery_seconds}s to recovery`}{' '}
          · assembled from stored rows, not new evidence
        </p>
        {!isMineable(experience) ? (
          <p className="mt-1 text-xs text-argus-warning">
            This episode&#39;s evidence is degraded, so the pre-learning quality
            gate excludes it from pattern mining. It is kept — the exclusion is a
            decision worth being able to inspect.
          </p>
        ) : null}
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Card title="What failed" subtitle="§9 — the failure signature">
          <pre className="overflow-x-auto rounded-md border border-slate-800 bg-slate-950/60 p-3 text-xs text-slate-400">
            {JSON.stringify(detail.failure_signature, null, 2)}
          </pre>
          <p className="mt-2 text-xs text-slate-500">
            Fingerprint{' '}
            <code className="font-mono">{experience.failure_fingerprint}</code>
          </p>
        </Card>

        <Card title="What was done" subtitle="§10 — the resolution signature">
          {detail.resolution_signature ? (
            <pre className="overflow-x-auto rounded-md border border-slate-800 bg-slate-950/60 p-3 text-xs text-slate-400">
              {JSON.stringify(detail.resolution_signature, null, 2)}
            </pre>
          ) : (
            <p className="text-sm text-slate-400">
              No resolution was recorded for this episode. An incident that
              recovered on its own is history too, and is stored as such rather
              than being given an inferred resolution.
            </p>
          )}
        </Card>
      </div>

      <Card title="Pipeline context" subtitle="§54 — the phase rows this was assembled from">
        <dl className="grid grid-cols-1 gap-3 text-sm md:grid-cols-2">
          <div>
            <dt className="text-xs uppercase tracking-wider text-slate-500">
              Incident
            </dt>
            <dd className="text-slate-300">
              {incident ? (
                <Link
                  href={`/incidents/${incident.id}`}
                  className="text-argus-accent"
                >
                  {incident.title}
                </Link>
              ) : (
                'Not linked'
              )}
            </dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wider text-slate-500">
              Primary component
            </dt>
            <dd className="text-slate-300">
              {component ? (
                <Link
                  href={`/intelligence/components/${component.id}?project_id=${encodeURIComponent(projectId)}`}
                  className="text-argus-accent"
                >
                  {component.name}
                </Link>
              ) : (
                'Not recorded'
              )}
            </dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wider text-slate-500">
              Remediation
            </dt>
            <dd className="text-slate-300">
              {remediation
                ? `${remediation.action_type} — ${remediation.status}${
                    remediation.outcome ? ` (${remediation.outcome})` : ''
                  }`
                : 'None recorded'}
            </dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wider text-slate-500">
              Components touched
            </dt>
            <dd className="text-slate-300">
              {experience.component_ids.length === 0
                ? 'None recorded'
                : `${experience.component_ids.length}`}
            </dd>
          </div>
        </dl>
      </Card>

      <Card title="Timeline" subtitle="What happened, in order — no causation implied">
        {timeline.length === 0 ? (
          <p className="text-sm text-slate-400">No timeline entries.</p>
        ) : (
          <ol className="space-y-2 text-sm">
            {timeline.map((entry, index) => (
              <li key={`${entry.stage}-${index}`} className="flex flex-wrap gap-3">
                <span className="w-40 text-xs uppercase tracking-wider text-slate-500">
                  {timelineStageLabel(entry.stage)}
                </span>
                <span className="text-slate-400">{formatDate(entry.at)}</span>
                <span className="text-slate-300">{entry.detail ?? ''}</span>
              </li>
            ))}
          </ol>
        )}
      </Card>
    </div>
  );
}
