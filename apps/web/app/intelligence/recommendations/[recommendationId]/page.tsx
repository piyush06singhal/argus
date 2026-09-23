import Link from 'next/link';

import { api, formatDate, type RecommendationDetail } from '@/lib/api';
import {
  confidenceStyle,
  decisionAndOutcome,
  historicalSummary,
  isInvestigation,
  knowledgeSampleLabel,
  knowledgeStatusLabel,
  outcomeLabel,
  rankingRows,
  recommendationEvidence,
  recommendationStatusStyle,
  recommendationTypeLabel,
  requiresPolicyAttention,
  timelineStageLabel,
} from '@/lib/intelligence';

import DecisionPanel from './DecisionPanel';

export const metadata = {
  title: 'Recommendation',
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
 * One recommendation, with everything that qualifies it (§40–§42, §81).
 *
 * The page answers four questions in order, because they are the four a person
 * actually asks before acting on advice:
 *
 * 1. What is being suggested, and is it an investigation or an action?
 * 2. What is it based on — which patterns and which episodes, with their counts?
 * 3. What would it take to act, under Phase 9's policy?
 * 4. What happened the last time this was tried, if it ever was?
 */
export default async function RecommendationDetailPage({
  params,
  searchParams,
}: {
  params: { recommendationId: string };
  searchParams: { project_id?: string };
}) {
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  if (!projectId) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-slate-100">Recommendation</h1>
        <Card title="Project required" subtitle="Recommendations are project-scoped">
          <p className="text-sm text-slate-400">
            Open this page from the{' '}
            <Link href="/intelligence/recommendations" className="text-argus-accent">
              Recommendation Center
            </Link>{' '}
            so the scope travels with the link.
          </p>
        </Card>
      </div>
    );
  }

  let detail: RecommendationDetail | null = null;
  let error: string | null = null;
  try {
    detail = await api.getRecommendation(params.recommendationId, projectId);
  } catch (caught) {
    error = caught instanceof Error ? caught.message : String(caught);
  }

  if (error || !detail) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-slate-100">Recommendation</h1>
        <Card title="Could not load" subtitle="The row may belong to another project">
          <p className="text-sm text-argus-warning">{error ?? 'Not found.'}</p>
        </Card>
      </div>
    );
  }

  const { recommendation, outcomes, knowledge, experiences } = detail;
  const { decision, outcome } = decisionAndOutcome(recommendation);
  const policyNote = requiresPolicyAttention(recommendation);

  return (
    <div className="space-y-6">
      <div>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold text-slate-100">
            {recommendationTypeLabel(recommendation.recommendation_type)}
          </h1>
          <span className={`badge ${recommendationStatusStyle(recommendation.status)}`}>
            {recommendation.status}
          </span>
          <span className={`badge ${confidenceStyle(recommendation.confidence)}`}>
            {recommendation.confidence} confidence
          </span>
          {isInvestigation(recommendation.recommendation_type) ? (
            <span className="badge bg-slate-800 text-slate-400">
              Investigation, not an action
            </span>
          ) : null}
        </div>
        <p className="mt-2 text-sm text-slate-400">{recommendation.rationale}</p>
        <p className="mt-2 text-xs text-slate-500">
          This is advice grounded in stored history. Acting on it still requires
          Phase 9&#39;s policy, safety and authority checks; nothing here executes
          anything.
        </p>
      </div>

      <Card title="The advice" subtitle={`${recommendation.title}`}>
        <p className="text-sm text-slate-300">{recommendation.rationale}</p>
        <p className="mt-2 text-xs text-slate-500">
          {historicalSummary(recommendation.historical)}
        </p>
        {rankingRows(recommendation.ranking).length > 0 ? (
          <div className="mt-3">
            <h3 className="text-xs uppercase tracking-wider text-slate-500">
              Why it ranked here
            </h3>
            <ul className="mt-1 space-y-1 text-xs text-slate-400">
              {rankingRows(recommendation.ranking).map((row) => (
                <li key={row.key}>
                  · {row.key}: {row.value}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </Card>

      <Card
        title="Current evidence"
        subtitle="§41 — what ARGUS observed in the situation being advised on"
      >
        {recommendationEvidence(recommendation).length === 0 ? (
          <p className="text-sm text-slate-400">
            No current evidence is attached. Advice without evidence should not be
            acted on, and it is shown here rather than hidden.
          </p>
        ) : (
          <ul className="space-y-1 text-sm text-slate-400">
            {recommendationEvidence(recommendation).map((row) => (
              <li key={row.key}>
                · <span className="text-slate-300">{row.key}</span>: {row.value}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card
          title="Knowledge behind it"
          subtitle="§39 — the patterns this advice draws on"
        >
          {knowledge.length === 0 ? (
            <p className="text-sm text-slate-400">
              No stored pattern supports this advice; it came from the current
              evidence alone.
            </p>
          ) : (
            <ul className="space-y-2">
              {knowledge.map((item) => (
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

        <Card
          title="Comparable episodes"
          subtitle="§8 — the history the advice generalises from"
        >
          {experiences.length === 0 ? (
            <p className="text-sm text-slate-400">
              No comparable historical case was found. That is a finding: ARGUS has
              nothing to base a claim on, and says so instead of inventing history.
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
                    {formatDate(experience.start_time)} ·{' '}
                    {outcomeLabel(experience.outcome)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card
        title="Acting on this"
        subtitle="Phase 9 applies — advice never bypasses policy, safety or authority"
      >
        {policyNote && recommendation.policy_note ? (
          <p className="text-sm text-argus-warning">{recommendation.policy_note}</p>
        ) : (
          <p className="text-sm text-slate-400">
            {isInvestigation(recommendation.recommendation_type)
              ? 'This is an investigation: it asks a person to look at something, and has no execution path through Phase 9.'
              : 'No policy note is attached to this recommendation.'}
          </p>
        )}
        {recommendation.limitations.length > 0 ? (
          <ul className="mt-3 space-y-1 text-xs text-slate-500">
            {recommendation.limitations.map((limitation) => (
              <li key={limitation}>· {limitation}</li>
            ))}
          </ul>
        ) : null}
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card
          title="Decision and outcome"
          subtitle="§81 — two separate facts, never collapsed"
        >
          <dl className="space-y-2 text-sm">
            <div>
              <dt className="text-xs uppercase tracking-wider text-slate-500">
                Decision
              </dt>
              <dd className="text-slate-300">{decision}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wider text-slate-500">
                Outcome
              </dt>
              <dd className="text-slate-300">{outcome}</dd>
            </div>
          </dl>
          {outcomes.length > 0 ? (
            <ul className="mt-3 space-y-2 text-xs text-slate-400">
              {outcomes.map((item) => (
                <li key={item.id}>
                  · {outcomeLabel(item.verdict)} — recorded{' '}
                  {formatDate(item.recorded_at)}
                  {item.recorded_by ? ` by ${item.recorded_by}` : ''}
                </li>
              ))}
            </ul>
          ) : null}
          <p className="mt-3 text-xs text-slate-600">
            Generated {formatDate(recommendation.created_at)} · expires{' '}
            {formatDate(recommendation.expires_at)}
          </p>
        </Card>

        <Card
          title="Record a decision"
          subtitle="Recorded, never executed — Phase 9 still gates any action"
        >
          <DecisionPanel
            recommendationId={recommendation.id}
            projectId={projectId}
            status={recommendation.status}
          />
        </Card>
      </div>

      {experiences.length > 0 ? (
        <Card title="Pipeline context" subtitle="Which stage produced each fact">
          <ul className="space-y-1 text-sm text-slate-400">
            <li>· {timelineStageLabel('INCIDENT')}</li>
            <li>· {timelineStageLabel('CAUSAL_ANALYSIS')}</li>
            <li>· {timelineStageLabel('REMEDIATION')}</li>
            <li>· {timelineStageLabel('VERIFICATION')}</li>
          </ul>
        </Card>
      ) : null}
    </div>
  );
}
