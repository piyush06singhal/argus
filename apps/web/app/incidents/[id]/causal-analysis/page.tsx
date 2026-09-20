import Link from 'next/link';

import {
  ApiError,
  api,
  formatDate,
  type AnalysisHistory,
  type CausalAnalysisDetail,
  type CausalChain,
  type Incident,
  type TimelineEvent,
} from '@/lib/api';
import {
  CAUSAL_DISCLAIMER,
  candidateLabel,
  candidateTypeLabel,
  chainSummary,
  confidenceStyle,
  CONFIDENCE_NOTES,
  formatAlignment,
  formatScore,
  isTemporalContradiction,
  relationshipLabel,
  scoreBreakdownRows,
} from '@/lib/causal';
import AnalyzeButton from './AnalyzeButton';
import CausalInvestigation from './CausalInvestigation';

export const metadata = {
  title: 'Root Cause Analysis',
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

export default async function CausalAnalysisPage({
  params,
}: {
  params: { id: string };
}) {
  const { id } = params;

  let incident: Incident;
  try {
    incident = await api.getIncident(id);
  } catch (error) {
    return (
      <div className="space-y-6">
        <BackLink />
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">Failed to load incident</h2>
          <p className="mt-2 text-sm text-slate-400">
            Could not load incident {id}. (
            {error instanceof Error ? error.message : 'unknown error'})
          </p>
        </div>
      </div>
    );
  }

  // No analysis yet is a normal state, not an error: the page offers to run one.
  let analysis: CausalAnalysisDetail | null = null;
  let analysisMissing = false;
  try {
    analysis = await api.getCausalAnalysis(id);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      analysisMissing = true;
    } else {
      throw error;
    }
  }

  const [timelineResult, history] = await Promise.all([
    api.getIncidentTimeline(id).catch(() => ({ items: [], total: 0 })),
    api
      .getAnalysisHistory(id)
      .catch((): AnalysisHistory => ({ items: [], total: 0 })),
  ]);
  const timeline: TimelineEvent[] = timelineResult.items;

  if (analysisMissing || !analysis) {
    return (
      <div className="space-y-6">
        <Header incident={incident} />
        <Card
          title="No analysis yet"
          subtitle="Root-cause analysis runs on stored evidence only"
        >
          <p className="text-sm text-slate-400">
            ARGUS has not analysed this incident yet. Running the analysis reasons
            over the anomalies, traces, dependency graph and change events already
            stored for it.
          </p>
          <div className="mt-3">
            <AnalyzeButton incidentId={incident.id} />
          </div>
          <p className="mt-3 text-xs text-slate-500">{CAUSAL_DISCLAIMER}</p>
        </Card>
      </div>
    );
  }

  const [graph, chain, hypotheses] = await Promise.all([
    api.getCausalGraph(id),
    api.getCausalChain(id),
    api.getHypotheses(id),
  ]);

  const primary = analysis.candidates.find(
    (candidate) => candidate.id === analysis.primary_candidate_id
  );
  const alternatives = hypotheses.items.filter(
    (item) => item.candidate.id !== analysis.primary_candidate_id
  );

  return (
    <div className="space-y-6">
      <Header incident={incident} />

      <Card
        title="Root cause analysis"
        subtitle={`Analysis v${analysis.analysis_version} · ${
          analysis.status
        } · ${analysis.trigger ?? 'manual'} trigger${
          analysis.requested_by ? ` by ${analysis.requested_by}` : ''
        }`}
      >
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-xs uppercase tracking-wider text-slate-500">
            Overall confidence
          </span>
          <span className={`badge ${confidenceStyle(analysis.overall_confidence)}`}>
            {analysis.overall_confidence}
          </span>
          <span className="text-xs text-slate-500">
            {CONFIDENCE_NOTES[analysis.overall_confidence]}
          </span>
        </div>

        <p className="mt-3 whitespace-pre-line text-sm leading-relaxed text-slate-300">
          {analysis.summary ?? summaryFallback(primary ? candidateLabel(primary) : null)}
        </p>

        {analysis.candidates.length > 0 ? (
          <p className="mt-3 text-sm text-slate-400">
            A candidate is a hypothesis, not a conclusion. To find out whether it
            holds, ARGUS can build an isolated environment and{' '}
            <Link
              href={`/incidents/${incident.id}/reproductions`}
              className="text-argus-accent hover:text-argus-accent-hover"
            >
              reproduce the hypothesis
            </Link>
            .
          </p>
        ) : null}

        <div className="mt-3 grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div>
            <p className="text-xs uppercase tracking-wider text-slate-500">
              Primary candidate
            </p>
            {primary ? (
              <>
                <div className="mt-1 flex flex-wrap items-center gap-2">
                  <span className="text-sm text-slate-200">
                    {candidateLabel(primary)}
                  </span>
                  <span className={`badge ${confidenceStyle(primary.confidence)}`}>
                    {primary.confidence}
                  </span>
                  <span className="text-xs text-slate-500">
                    {candidateTypeLabel(primary.candidate_type)}
                  </span>
                </div>
                <p className="mt-1 text-xs text-slate-500">
                  {`Score ${formatScore(primary.score)} · ${
                    primary.supporting_evidence_count
                  } supporting / ${primary.contradicting_evidence_count} contradicting fact(s)`}
                </p>
                <ScoreTable breakdown={primary.score_breakdown} />
              </>
            ) : (
              <p className="mt-1 text-sm text-slate-400">
                ARGUS did not select a primary hypothesis: the evidence does not
                single out one component. The ranked candidates below are still
                shown, with no winner implied.
              </p>
            )}
          </div>
          <div>
            <p className="text-xs uppercase tracking-wider text-slate-500">
              Missing evidence
            </p>
            {analysis.missing_evidence && analysis.missing_evidence.length > 0 ? (
              <ul className="mt-1 space-y-1">
                {analysis.missing_evidence.map((item) => (
                  <li key={item} className="text-xs text-slate-400">
                    • {item}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1 text-xs text-slate-400">
                Nothing the engine looked for was absent.
              </p>
            )}
          </div>
        </div>

        <div className="mt-4">
          <AnalyzeButton
            incidentId={incident.id}
            force
            label="Re-run analysis (new version)"
          />
        </div>
        <p className="mt-3 text-xs text-slate-500">{CAUSAL_DISCLAIMER}</p>
      </Card>

      <Card
        title="Causal chain"
        subtitle="Every link is backed by stored evidence"
      >
        <Chain chain={chain} candidates={analysis.candidates} />
      </Card>

      <CausalInvestigation
        incidentId={incident.id}
        graph={graph}
        evidence={analysis.evidence}
        chainCandidateIds={chain.candidate_ids}
        timeline={timeline}
      />

      <Card
        title="Alternative hypotheses"
        subtitle="Ranked, with the evidence that supports and weakens each"
      >
        {alternatives.length === 0 ? (
          <p className="text-sm text-slate-400">
            No alternative hypothesis was generated for this incident.
          </p>
        ) : (
          <ul className="space-y-4">
            {alternatives.map((item) => (
              <li key={item.candidate.id} className="border-t border-slate-800 pt-3 first:border-0 first:pt-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm text-slate-200">
                    {candidateLabel(item.candidate)}
                  </span>
                  <span className={`badge ${confidenceStyle(item.candidate.confidence)}`}>
                    {item.candidate.confidence}
                  </span>
                  <span className="text-xs text-slate-500">
                    {candidateTypeLabel(item.candidate.candidate_type)} · score{' '}
                    {formatScore(item.candidate.score)}
                  </span>
                </div>
                <p className="mt-1 text-xs text-slate-500">
                  {item.why_confidence_differs ?? 'No confidence rationale recorded.'}
                </p>
                <EvidenceColumn
                  label="Supporting"
                  items={item.supporting.map((row) => row.quote)}
                  tone="text-slate-300"
                />
                <EvidenceColumn
                  label="Contradicting"
                  items={item.contradicting.map((row) => row.quote)}
                  tone="text-argus-error"
                />
                {item.candidate.uncertainty?.missing?.length ? (
                  <p className="mt-1 text-xs text-slate-500">
                    Missing: {item.candidate.uncertainty.missing.join('; ')}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card
        title="Analysis history"
        subtitle="Previous versions are preserved, never overwritten"
      >
        <History history={history} />
      </Card>
    </div>
  );
}

function BackLink() {
  return (
    <Link href="/incidents" className="text-sm text-slate-400 hover:text-argus-accent">
      ← Back to incidents
    </Link>
  );
}

function Header({ incident }: { incident: Incident }) {
  return (
    <div>
      <BackLink />
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-semibold text-slate-100">
          {incident.title}
        </h1>
        <span className="badge bg-slate-800 text-slate-300">{incident.severity}</span>
      </div>
      <p className="mt-1 text-xs text-slate-500">
        Root cause analysis · incident {incident.id}
        {incident.project_id ? ` · project ${incident.project_id}` : ''}
      </p>
      <p className="mt-1 text-xs text-slate-500">
        <Link href={`/incidents/${incident.id}`} className="hover:text-argus-accent">
          Incident detail
        </Link>
        {' · '}
        <Link
          href={`/system-map?node=${incident.primary_component_id ?? ''}`}
          className="hover:text-argus-accent"
        >
          System map context
        </Link>
      </p>
    </div>
  );
}

function summaryFallback(label: string | null): string {
  if (label) {
    return `Most-supported explanation: ${label}. See the evidence below for what supports it.`;
  }
  return 'Insufficient evidence to determine a root cause. The candidates below are ranked, but none reached the documented threshold for a primary hypothesis.';
}

function ScoreTable({
  breakdown,
}: {
  breakdown: CausalAnalysisDetail['candidates'][number]['score_breakdown'];
}) {
  const { positive, penalty } = scoreBreakdownRows(breakdown);
  if (positive.length === 0 && !penalty) {
    return null;
  }
  return (
    <ul className="mt-2 space-y-0.5">
      {positive.map((row) => (
        <li key={row.key} className="flex max-w-xs justify-between text-xs text-slate-400">
          <span>{row.label}</span>
          <span className="font-mono">+{row.value.toFixed(3)}</span>
        </li>
      ))}
      {penalty ? (
        <li className="flex max-w-xs justify-between text-xs text-argus-error">
          <span>{penalty.label}</span>
          <span className="font-mono">−{penalty.value.toFixed(3)}</span>
        </li>
      ) : null}
    </ul>
  );
}

function EvidenceColumn({
  label,
  items,
  tone,
}: {
  label: string;
  items: string[];
  tone: string;
}) {
  if (items.length === 0) {
    return (
      <p className="mt-1 text-xs text-slate-500">
        {label}: none observed.
      </p>
    );
  }
  return (
    <div className="mt-1">
      <p className="text-xs uppercase tracking-wider text-slate-500">{label}</p>
      <ul className="mt-0.5 space-y-0.5">
        {items.map((item) => (
          <li key={item} className={`text-xs ${tone}`}>
            • {item}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Chain({
  chain,
  candidates,
}: {
  chain: CausalChain;
  candidates: CausalAnalysisDetail['candidates'];
}) {
  if (chain.chain.length === 0) {
    return (
      <div>
        <p className="text-sm text-slate-400">
          No causal chain is claimed for this incident.
        </p>
        {chain.validation_notes.length > 0 ? (
          <ul className="mt-2 space-y-1">
            {chain.validation_notes.map((note) => (
              <li key={note} className="text-xs text-slate-500">
                • {note}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    );
  }
  return (
    <div>
      <p className="text-sm text-slate-200">
        {chainSummary(chain.candidate_ids, candidates)}
      </p>
      <p className="mt-1 text-xs text-slate-500">
        {chain.valid
          ? 'Validated: no link contradicts its own timestamps.'
          : 'Not validated — see the notes below.'}
      </p>
      <ol className="mt-3 space-y-3">
        {chain.chain.map((link, index) => (
          <li key={`${link.source_candidate_id}-${link.target_candidate_id}-${index}`}>
            <div className="flex flex-wrap items-center gap-2">
              <span className="badge bg-slate-800 text-slate-300">
                {relationshipLabel(link.relationship_type)}
              </span>
              <span className={`badge ${confidenceStyle(link.confidence)}`}>
                {link.confidence}
              </span>
              <span className="text-xs text-slate-500">
                {`${link.evidence_count} fact(s) · ${formatAlignment(
                  link.temporal_alignment_seconds
                )}`}
              </span>
              {isTemporalContradiction(link.temporal_alignment_seconds) ? (
                <span className="text-xs text-argus-error">
                  temporal contradiction
                </span>
              ) : null}
            </div>
            <p className="mt-1 text-xs text-slate-400">{link.explanation}</p>
          </li>
        ))}
      </ol>
      {chain.validation_notes.length > 0 ? (
        <ul className="mt-3 space-y-1">
          {chain.validation_notes.map((note) => (
            <li key={note} className="text-xs text-slate-500">
              • {note}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function History({ history }: { history: AnalysisHistory }) {
  if (history.items.length === 0) {
    return (
      <p className="text-sm text-slate-400">
        No analysis versions have been recorded.
      </p>
    );
  }
  return (
    <table className="min-w-full divide-y divide-slate-800 text-sm">
      <thead>
        <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
          <th className="px-3 py-2">Version</th>
          <th className="px-3 py-2">Confidence</th>
          <th className="px-3 py-2">Primary</th>
          <th className="px-3 py-2">Candidates</th>
          <th className="px-3 py-2">Evidence</th>
          <th className="px-3 py-2">Ran</th>
          <th className="px-3 py-2">Changed</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-800">
        {history.items.map((item) => (
          <tr key={item.analysis_id}>
            <td className="px-3 py-2 text-slate-300">v{item.analysis_version}</td>
            <td className="px-3 py-2">
              <span className={`badge ${confidenceStyle(item.overall_confidence)}`}>
                {item.overall_confidence}
              </span>
            </td>
            <td className="px-3 py-2 text-xs text-slate-400">
              {item.primary_candidate_summary ?? '—'}
            </td>
            <td className="px-3 py-2 text-xs text-slate-400">
              {item.candidate_count}
            </td>
            <td className="px-3 py-2 text-xs text-slate-400">
              {`${item.supporting_evidence_count} supporting`}
              {item.contradicting_evidence_count
                ? ` / ${item.contradicting_evidence_count} contradicting`
                : ''}
            </td>
            <td className="px-3 py-2 text-xs text-slate-500">
              {formatDate(item.completed_at ?? item.started_at)}
              {item.trigger ? ` · ${item.trigger}` : ''}
            </td>
            <td className="px-3 py-2 text-xs text-slate-500">
              {item.diff
                ? [
                    item.diff.primary_changed ? 'primary changed' : null,
                    item.diff.confidence_changed
                      ? `confidence ${item.diff.previous_confidence} → ${
                          item.overall_confidence
                        }`
                      : null,
                    item.diff.new_contradictions
                      ? `${item.diff.new_contradictions} contradiction(s)`
                      : null,
                  ]
                    .filter(Boolean)
                    .join(', ') || 'no material change'
                : 'first version'}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
