import Link from 'next/link';

import {
  api,
  formatDate,
  type KnowledgeDetail,
  type KnowledgeVersionItem,
} from '@/lib/api';
import {
  confidenceStyle,
  knowledgeSampleLabel,
  knowledgeStatusLabel,
  knowledgeStatusStyle,
  knowledgeTypeLabel,
  LEARNING_BOUNDARY,
  OBSERVATIONAL_NOTE,
  scopeLabel,
  supportLabel,
} from '@/lib/intelligence';

import ReviewPanel from './ReviewPanel';

export const metadata = {
  title: 'Pattern',
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
 * One pattern, in full (§53, §26, §72, §79).
 *
 * Everything that qualifies the claim is on this page: the sample, the coverage
 * window, the algorithm and feature-schema versions that produced it, the
 * validation verdict, the version ledger, the human review history, and the
 * episodes it was derived from. A reader should be able to decide whether to
 * trust it without leaving the page — and should be unable to mistake it for an
 * instruction, which is why `LEARNING_BOUNDARY` sits directly under the title.
 */
export default async function PatternDetailPage({
  params,
  searchParams,
}: {
  params: { knowledgeId: string };
  searchParams: { project_id?: string };
}) {
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  if (!projectId) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-slate-100">Pattern</h1>
        <Card title="Project required" subtitle="Knowledge is project-scoped">
          <p className="text-sm text-slate-400">
            Open this pattern from the{' '}
            <Link href="/intelligence" className="text-argus-accent">
              Learning Center
            </Link>{' '}
            so the project scope travels with the link. The API refuses to answer
            without one.
          </p>
        </Card>
      </div>
    );
  }

  let detail: KnowledgeDetail | null = null;
  let error: string | null = null;
  try {
    detail = await api.getKnowledge(params.knowledgeId, projectId);
  } catch (caught) {
    error = caught instanceof Error ? caught.message : String(caught);
  }

  if (error || !detail) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-slate-100">Pattern</h1>
        <Card title="Could not load" subtitle="The row may belong to another project">
          <p className="text-sm text-argus-warning">{error ?? 'Not found.'}</p>
          <p className="mt-2 text-xs text-slate-500">
            An out-of-scope id answers 404 rather than confirming that it exists.
          </p>
        </Card>
      </div>
    );
  }

  const { knowledge, versions, reviews, related, experiences } = detail;

  return (
    <div className="space-y-6">
      <div>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold text-slate-100">{knowledge.title}</h1>
          <span className={`badge ${knowledgeStatusStyle(knowledge.status)}`}>
            {knowledgeStatusLabel(knowledge.status)}
          </span>
          <span className={`badge ${confidenceStyle(knowledge.confidence)}`}>
            {knowledge.confidence} confidence
          </span>
          <span className="badge bg-slate-800 text-slate-400">
            {knowledgeTypeLabel(knowledge.knowledge_type)}
          </span>
        </div>
        <p className="mt-2 text-sm text-slate-400">{knowledge.description}</p>
        <p className="mt-2 text-xs text-slate-500">{OBSERVATIONAL_NOTE}</p>
        <p className="mt-1 text-xs text-slate-500">{LEARNING_BOUNDARY}</p>
      </div>

      <Card title="The claim and its support" subtitle="§7, §33, §34, §37">
        <dl className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Field label="Sample" value={knowledgeSampleLabel(knowledge)} />
          <Field
            label="Support"
            value={
              supportLabel(knowledge.support_strength, knowledge.sample_count) ??
              'No outcome ratio recorded.'
            }
          />
          <Field label="Scope" value={scopeLabel(knowledge.scope)} />
          <Field label="Coverage" value={`v${knowledge.version}`} />
          <Field label="Covered from" value={formatDate(knowledge.coverage_start)} />
          <Field label="Covered to" value={formatDate(knowledge.coverage_end)} />
          <Field
            label="Feature signature"
            value={<code className="font-mono text-xs">{knowledge.feature_signature}</code>}
          />
          <Field
            label="Produced by"
            value={`${knowledge.algorithm} ${knowledge.algorithm_version} (schema ${knowledge.feature_schema_version})`}
          />
          <Field
            label="Last confirmed"
            value={formatDate(knowledge.last_confirmed_at)}
          />
          <Field
            label="Reviewed"
            value={
              knowledge.reviewed_by
                ? `${knowledge.reviewed_by} · ${formatDate(knowledge.reviewed_at)}`
                : 'No human review recorded.'
            }
          />
        </dl>

        {knowledge.limitations.length > 0 ? (
          <div className="mt-4">
            <h3 className="text-xs uppercase tracking-wider text-slate-500">
              What this does not claim
            </h3>
            <ul className="mt-1 space-y-1 text-sm text-slate-400">
              {knowledge.limitations.map((limitation) => (
                <li key={limitation}>· {limitation}</li>
              ))}
            </ul>
          </div>
        ) : null}

        {knowledge.validation ? (
          <div className="mt-4">
            <h3 className="text-xs uppercase tracking-wider text-slate-500">
              Validation
            </h3>
            <pre className="mt-1 overflow-x-auto rounded-md border border-slate-800 bg-slate-950/60 p-3 text-xs text-slate-400">
              {JSON.stringify(knowledge.validation, null, 2)}
            </pre>
          </div>
        ) : null}
      </Card>

      <Card
        title="Provenance"
        subtitle="§5, §79 — a pattern with no sources is a guess, so the empty case is visible"
      >
        {knowledge.sources.length === 0 ? (
          <p className="text-sm text-argus-warning">
            No source is recorded on this row. That should be impossible; treat it
            as a data-integrity finding rather than as knowledge.
          </p>
        ) : (
          <ul className="space-y-1 text-sm text-slate-400">
            {knowledge.sources.map((source, index) => (
              <li key={`${index}-${String(source.id ?? '')}`}>
                · <span className="font-mono text-xs">{String(source.type ?? 'source')}</span>{' '}
                {String(source.id ?? '')}
              </li>
            ))}
          </ul>
        )}
        <p className="mt-3 text-xs text-slate-500">
          {knowledge.experience_ids.length} experience
          {knowledge.experience_ids.length === 1 ? '' : 's'} contributed to this
          pattern.
        </p>
      </Card>

      <Card title="Version ledger" subtitle="§26 — what ARGUS believed, and when">
        {versions.length === 0 ? (
          <p className="text-sm text-slate-400">No version record.</p>
        ) : (
          <ul className="space-y-2">
            {versions.map((version: KnowledgeVersionItem) => (
              <li
                key={version.id}
                className="rounded-md border border-slate-800 bg-slate-900/40 p-3 text-sm"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs text-slate-400">
                    v{version.version}
                  </span>
                  <span className={`badge ${knowledgeStatusStyle(version.status)}`}>
                    {knowledgeStatusLabel(version.status)}
                  </span>
                  <span className="text-xs text-slate-500">
                    {version.sample_count} sample
                    {version.sample_count === 1 ? '' : 's'} · {version.confidence} ·{' '}
                    {formatDate(version.created_at)}
                  </span>
                </div>
                {version.note ? (
                  <p className="mt-1 text-xs text-slate-500">{version.note}</p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Review history" subtitle="§72 — who decided what, and why">
        {reviews.length === 0 ? (
          <p className="text-sm text-slate-400">
            No human has reviewed this pattern yet.
          </p>
        ) : (
          <ul className="space-y-2 text-sm">
            {reviews.map((review) => (
              <li
                key={review.id}
                className="rounded-md border border-slate-800 bg-slate-900/40 p-3"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-slate-200">{review.decision}</span>
                  <span className="text-xs text-slate-500">
                    {review.reviewer} · {formatDate(review.created_at)} · v
                    {review.knowledge_version}
                  </span>
                </div>
                {review.reason ? (
                  <p className="mt-1 text-xs text-slate-400">{review.reason}</p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Review this pattern" subtitle="§71–§74, §82">
          <ReviewPanel
            knowledgeId={knowledge.id}
            projectId={projectId}
            currentStatus={knowledge.status}
          />
        </Card>

        <Card
          title="Related patterns"
          subtitle="Same component or type, computed by the server"
        >
          {related.length === 0 ? (
            <p className="text-sm text-slate-400">
              Nothing else has been learned about this area yet.
            </p>
          ) : (
            <ul className="space-y-2">
              {related.map((item) => (
                <li key={item.id} className="text-sm">
                  <Link
                    href={`/intelligence/patterns/${item.id}?project_id=${encodeURIComponent(projectId)}`}
                    className="text-slate-200 hover:text-argus-accent"
                  >
                    {item.title}
                  </Link>
                  <span className="ml-2 text-xs text-slate-500">
                    {knowledgeSampleLabel(item)} · {knowledgeStatusLabel(item.status)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card
        title="Episodes behind this pattern"
        subtitle="§8, §54 — the raw history the pattern was derived from"
      >
        {experiences.length === 0 ? (
          <p className="text-sm text-slate-400">
            No experience is linked to this pattern.
          </p>
        ) : (
          <ul className="space-y-2 text-sm">
            {experiences.map((experience) => (
              <li key={experience.id} className="flex flex-wrap items-center gap-2">
                <Link
                  href={`/intelligence/experiences/${experience.id}?project_id=${encodeURIComponent(projectId)}`}
                  className="font-mono text-xs text-argus-accent"
                >
                  {experience.id.slice(0, 8)}
                </Link>
                <span className="text-slate-300">{experience.failure_label}</span>
                <span className="text-xs text-slate-500">
                  {formatDate(experience.start_time)} · outcome{' '}
                  {experience.outcome}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
