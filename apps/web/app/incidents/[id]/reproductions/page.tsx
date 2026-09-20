import Link from 'next/link';

import {
  ApiError,
  api,
  formatDate,
  type CausalAnalysisDetail,
  type Incident,
  type ReproductionHistory,
} from '@/lib/api';
import { CAUSAL_DISCLAIMER, candidateLabel } from '@/lib/causal';
import {
  experimentStatusStyle,
  formatSeconds,
  isTerminalStatus,
  outcomeStyle,
  REPRODUCTION_DISCLAIMER,
  resultStyle,
  sortHistory,
} from '@/lib/reproduction';
import PlanReproductionButton, {
  type PlanCandidateOption,
} from './PlanReproductionButton';

export const metadata = {
  title: 'Failure Reproduction',
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
 * An incident's experiment history (§45, §51).
 *
 * Each row is a separate, immutable experiment: retrying a hypothesis creates a
 * new version rather than overwriting the first attempt, so both attempts stay
 * comparable. The page never summarises results into a single verdict — a
 * reproduction is an experiment, and the evidence belongs to each run.
 */
export default async function IncidentReproductionsPage({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: { project_id?: string };
}) {
  const { id } = params;
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  let incident: Incident;
  try {
    incident = await api.getIncident(id, projectId || undefined);
  } catch (error) {
    return (
      <div className="space-y-6">
        <BackLink incidentId={id} />
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

  // An incident with no analysis has no hypothesis, so there is nothing to
  // reproduce yet — a normal state that points at the Phase 4 step.
  let analysis: CausalAnalysisDetail | null = null;
  try {
    analysis = await api.getCausalAnalysis(id, projectId || undefined);
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 404)) {
      throw error;
    }
  }

  const history: ReproductionHistory = await api
    .listIncidentReproductions(id, projectId || undefined)
    .catch(() => ({ incident_id: id, items: [], total: 0 }));

  const candidates: PlanCandidateOption[] = (analysis?.candidates ?? [])
    .map((candidate) => ({
      id: candidate.id,
      label: candidateLabel(candidate),
      confidence: candidate.confidence,
      isPrimary: candidate.id === analysis?.primary_candidate_id,
    }))
    .sort((a, b) => Number(b.isPrimary) - Number(a.isPrimary));

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">
            Failure reproduction
          </h1>
          <p className="mt-1 text-sm text-slate-400">
            {incident.title} · detected {formatDate(incident.detected_at)} ·{' '}
            {incident.severity} / {incident.status}
          </p>
        </div>
        <BackLink incidentId={incident.id} />
      </div>

      <Card
        title="Reproduce a hypothesis"
        subtitle="Isolated sandbox · sanitized inputs · controlled faults"
      >
        {analysis === null ? (
          <>
            <p className="text-sm text-slate-400">
              This incident has no causal analysis yet, so ARGUS has no hypothesis
              to test. Reproducing a guess is not an experiment.
            </p>
            <div className="mt-3">
              <Link
                className="btn-ghost"
                href={`/incidents/${incident.id}/causal-analysis`}
              >
                Run root-cause analysis first
              </Link>
            </div>
          </>
        ) : candidates.length === 0 ? (
          <p className="text-sm text-slate-400">
            Analysis v{analysis.analysis_version} did not produce a candidate: the
            evidence does not single out one component, so there is nothing
            specific to reproduce.
          </p>
        ) : (
          <>
            <PlanReproductionButton
              incidentId={incident.id}
              projectId={projectId || undefined}
              candidates={candidates}
            />
            <p className="mt-3 text-xs text-slate-500">{REPRODUCTION_DISCLAIMER}</p>
            <p className="mt-1 text-xs text-slate-500">{CAUSAL_DISCLAIMER}</p>
          </>
        )}
      </Card>

      <Card
        title="Experiment history"
        subtitle={`${history.total} experiment(s), newest version first`}
      >
        {history.items.length === 0 ? (
          <p className="text-sm text-slate-400">
            No experiment has been planned for this incident yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-3 py-2">Version</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2">Result</th>
                  <th className="px-3 py-2">Hypothesis verdict</th>
                  <th className="px-3 py-2">Reps</th>
                  <th className="px-3 py-2">Duration</th>
                  <th className="px-3 py-2">Created</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {sortHistory(history.items).map((item) => (
                  <tr key={item.experiment_id} className="align-top">
                    <td className="px-3 py-2">
                      <Link href={`/reproductions/${item.experiment_id}`}>
                        v{item.experiment_version}
                      </Link>
                    </td>
                    <td className="px-3 py-2">
                      <span
                        className={`badge ${experimentStatusStyle(item.status)}`}
                      >
                        {item.status}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      <span className={`badge ${resultStyle(item.result)}`}>
                        {item.result}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      {item.outcome ? (
                        <span className={`badge ${outcomeStyle(item.outcome)}`}>
                          {item.outcome}
                        </span>
                      ) : (
                        <span className="text-xs text-slate-500">
                          {isTerminalStatus(item.status)
                            ? 'no verdict recorded'
                            : 'pending'}
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-slate-300">
                      {item.repetitions}
                    </td>
                    <td className="px-3 py-2 text-slate-400">
                      {item.duration_ms != null
                        ? formatSeconds(item.duration_ms / 1000)
                        : '—'}
                    </td>
                    <td className="px-3 py-2 text-slate-400">
                      {formatDate(item.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="mt-4 space-y-2">
              {sortHistory(history.items).map((item) => (
                <div
                  key={`summary-${item.experiment_id}`}
                  className="rounded-md border border-slate-800 bg-slate-950/40 p-3"
                >
                  <p className="text-xs uppercase tracking-wider text-slate-500">
                    v{item.experiment_version} · {item.status} · {item.result}
                    {item.outcome ? ` · ${item.outcome}` : ''}
                  </p>
                  <p className="mt-1 text-sm text-slate-300">
                    {item.hypothesis ?? 'No hypothesis recorded for this version.'}
                  </p>
                  {item.summary ? (
                    <p className="mt-1 text-xs text-slate-400">{item.summary}</p>
                  ) : null}
                </div>
              ))}
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}

function BackLink({ incidentId }: { incidentId: string }) {
  return (
    <div className="flex flex-wrap gap-3 text-sm">
      <Link href={`/incidents/${incidentId}`}>← Incident</Link>
      <Link href={`/incidents/${incidentId}/causal-analysis`}>
        Root-cause analysis
      </Link>
    </div>
  );
}
