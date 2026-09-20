import Link from 'next/link';

import {
  ApiError,
  api,
  formatDate,
  type ReproductionArtifact,
  type ReproductionComparisonList,
  type ReproductionEnvironmentSnapshot,
  type ReproductionExperimentDetail,
  type ReproductionFault,
  type ReproductionInput,
  type ReproductionManifest,
  type ReproductionSafetyPreview,
  type ReproductionTelemetry,
  type ReproductionValidation,
} from '@/lib/api';
import {
  captureCoverage,
  determinismNote,
  FAULT_TYPE_LABELS,
  failureClassLabel,
  faultStatusStyle,
  faultSummary,
  formatBytes,
  formatComponents,
  formatOverlap,
  formatSeconds,
  formatSequence,
  isInconclusiveClass,
  observationStatusStyle,
  outcomeStyle,
  OUTCOME_NOTES,
  replayStatusStyle,
  REPRODUCTION_DISCLAIMER,
  RESULT_NOTES,
  resultStyle,
  similarityDimensionRows,
  scoreLabel,
} from '@/lib/reproduction';
import ExperimentControl from './ExperimentControl';

export const metadata = {
  title: 'Reproduction Workspace',
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

function Pre({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return <span className="text-xs text-slate-500">—</span>;
  }
  return (
    <pre className="max-h-72 overflow-auto rounded-md border border-slate-800 bg-slate-950/60 p-3 text-xs text-slate-300">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

/**
 * The reproduction workspace (§45–§51).
 *
 * The sections mirror the phase's own flow — setup, safety, live experiment,
 * comparison, validation — and each keeps its own epistemic status visible: the
 * plan says what will run, `result` says what the sandbox did, `outcome` says
 * what that means for the hypothesis, and the limitations say why the answer
 * might be wrong.
 */
export default async function ReproductionWorkspacePage({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: { project_id?: string };
}) {
  const { id } = params;
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  let detail: ReproductionExperimentDetail;
  try {
    detail = await api.getReproduction(id, projectId || undefined);
  } catch (error) {
    return (
      <div className="space-y-6">
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load experiment
          </h2>
          <p className="mt-2 text-sm text-slate-400">
            Could not load reproduction {id}. (
            {error instanceof Error ? error.message : 'unknown error'})
          </p>
        </div>
        <Link href="/reproductions">← All experiments</Link>
      </div>
    );
  }

  const experiment = detail.experiment;
  const scope = projectId || experiment.project_id;

  // Every secondary panel is optional: a planned experiment has no comparison
  // yet, and a failed one may have no validation. A missing 404 panel is a
  // normal state, not an error page.
  const optional = async <T,>(loader: () => Promise<T>): Promise<T | null> => {
    try {
      return await loader();
    } catch (error) {
      if (error instanceof ApiError && (error.status === 404 || error.status === 409)) {
        return null;
      }
      throw error;
    }
  };

  const [
    safety,
    comparison,
    validation,
    inputs,
    faultsResponse,
    artifactsResponse,
    environment,
    manifest,
    telemetry,
  ] = await Promise.all([
    optional(() => api.getReproductionSafety(id, scope)),
    optional<ReproductionComparisonList>(() =>
      api.getReproductionComparison(id, scope)
    ),
    optional<ReproductionValidation>(() => api.getReproductionValidation(id, scope)),
    optional<ReproductionInput[]>(() => api.getReproductionInputs(id, scope)),
    optional(() => api.getReproductionFaults(id, scope)),
    optional(() => api.getReproductionArtifacts(id, scope)),
    optional<ReproductionEnvironmentSnapshot[]>(() =>
      api.getReproductionEnvironment(id, scope)
    ),
    optional<ReproductionManifest>(() => api.getReproductionManifest(id, scope)),
    optional<ReproductionTelemetry>(() => api.getReproductionTelemetry(id, scope)),
  ]);

  const plan = detail.plan ?? null;
  const hypothesis = detail.hypothesis ?? null;
  const originals = (environment ?? []).filter((row) => row.source === 'ORIGINAL');
  const sandboxes = (environment ?? []).filter((row) => row.source === 'SANDBOX');

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">
            Reproduction workspace
          </h1>
          <p className="mt-1 text-sm text-slate-400">
            Experiment v{experiment.experiment_version} ·{' '}
            {formatDate(experiment.created_at)} · triggered by{' '}
            {experiment.trigger ?? 'manual'}
            {experiment.requested_by ? ` (${experiment.requested_by})` : ''}
          </p>
        </div>
        <div className="flex flex-wrap gap-3 text-sm">
          <Link href={`/incidents/${experiment.incident_id}`}>← Incident</Link>
          <Link href={`/incidents/${experiment.incident_id}/causal-analysis`}>
            Root-cause analysis
          </Link>
          <Link href={`/incidents/${experiment.incident_id}/reproductions`}>
            Experiment history
          </Link>
          <Link href="/reproductions">All experiments</Link>
        </div>
      </div>

      <Card
        title="Verdict"
        subtitle="What the sandbox observed, and what that means for the hypothesis"
      >
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div>
            <p className="text-xs uppercase tracking-wider text-slate-500">
              Reproduction result
            </p>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <span className={`badge ${resultStyle(experiment.result)}`}>
                {experiment.result}
              </span>
              <span className="text-xs text-slate-500">
                {RESULT_NOTES[experiment.result]}
              </span>
            </div>
          </div>
          <div>
            <p className="text-xs uppercase tracking-wider text-slate-500">
              Hypothesis validation
            </p>
            {validation ? (
              <div className="mt-1 flex flex-wrap items-center gap-2">
                <span className={`badge ${outcomeStyle(validation.outcome)}`}>
                  {validation.outcome}
                </span>
                <span className="text-xs text-slate-500">
                  {OUTCOME_NOTES[validation.outcome]}
                </span>
              </div>
            ) : (
              <p className="mt-1 text-sm text-slate-400">
                No verdict has been written yet.
              </p>
            )}
          </div>
        </div>

        {experiment.summary ? (
          <p className="mt-3 whitespace-pre-line text-sm leading-relaxed text-slate-300">
            {experiment.summary}
          </p>
        ) : null}

        {experiment.failure_classification ? (
          <p className="mt-3 text-xs text-argus-warning">
            Classified as {failureClassLabel(experiment.failure_classification)}
            {isInconclusiveClass(experiment.failure_classification)
              ? ' — this class explains a null result, so it cannot be read as a refutation.'
              : ''}
          </p>
        ) : null}

        <p className="mt-3 text-xs text-slate-500">
          {detail.disclaimer || REPRODUCTION_DISCLAIMER}
        </p>
      </Card>

      {safety ? (
        <ExperimentControl detail={detail} safety={safety} projectId={scope} />
      ) : (
        <Card title="Execution" subtitle="Safety preview unavailable">
          <p className="text-sm text-slate-400">
            The safety preview for this experiment could not be loaded, so ARGUS
            will not offer to run it from here. This usually means the experiment
            has no plan server-side.
          </p>
        </Card>
      )}

      {hypothesis ? (
        <Card
          title="Hypothesis under test"
          subtitle={
            hypothesis.candidate_type
              ? `${hypothesis.candidate_type} candidate`
              : undefined
          }
        >
          <p className="text-sm leading-relaxed text-slate-200">
            {hypothesis.statement}
          </p>
          <dl className="mt-3 grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div>
              <dt className="text-xs uppercase tracking-wider text-slate-500">
                Expected failure
              </dt>
              <dd className="mt-1 text-sm text-slate-300">
                {hypothesis.expected_failure ?? '—'}
              </dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wider text-slate-500">
                Expected sequence
              </dt>
              <dd className="mt-1 text-sm text-slate-300">
                {formatSequence(hypothesis.expected_sequence)}
              </dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wider text-slate-500">
                Expected components
              </dt>
              <dd className="mt-1 text-sm text-slate-300">
                {formatComponents(hypothesis.expected_components)}
              </dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wider text-slate-500">
                Expected window
              </dt>
              <dd className="mt-1 text-sm text-slate-300">
                {hypothesis.expected_time_window_seconds != null
                  ? `${hypothesis.expected_time_window_seconds}s`
                  : '—'}
              </dd>
            </div>
          </dl>

          <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div>
              <p className="text-xs uppercase tracking-wider text-slate-500">
                Supporting evidence carried into the plan
              </p>
              <ul className="mt-1 space-y-1 text-sm text-slate-300">
                {(hypothesis.supporting_evidence ?? []).length === 0 ? (
                  <li className="text-slate-500">—</li>
                ) : (
                  (hypothesis.supporting_evidence ?? []).slice(0, 10).map(
                    (item, index) => {
                      const row = item as Record<string, unknown>;
                      return (
                        <li key={index} className="flex gap-2">
                          <span className="badge bg-slate-700/40 text-slate-300">
                            {String(row.category ?? 'EVIDENCE')}
                          </span>
                          <span className="text-slate-300">
                            {String(row.quote ?? '')}
                          </span>
                        </li>
                      );
                    }
                  )
                )}
              </ul>
            </div>
            <div>
              <p className="text-xs uppercase tracking-wider text-slate-500">
                Contradicted by
              </p>
              <ul className="mt-1 space-y-1 text-sm text-slate-300">
                {(hypothesis.contradicted_by ?? []).length === 0 ? (
                  <li className="text-slate-500">nothing recorded</li>
                ) : (
                  (hypothesis.contradicted_by ?? []).map((item, index) => (
                    <li key={index}>{item}</li>
                  ))
                )}
              </ul>
            </div>
          </div>
        </Card>
      ) : null}

      {plan ? (
        <Card title="Reproduction plan" subtitle="§46 — nothing hidden">
          <dl className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            <Field label="Strategy" value={plan.strategy} />
            <Field label="Target component" value={plan.target_component_name} />
            <Field label="Target version" value={plan.target_version ?? '—'} />
            <Field label="Network policy" value={plan.network_policy} />
            <Field label="Timeout" value={`${plan.timeout_seconds}s`} />
            <Field label="Repetitions" value={String(plan.repetitions)} />
            <Field
              label="Services"
              value={(plan.required_services ?? []).join(', ') || '—'}
            />
            <Field
              label="Dependencies"
              value={(plan.required_dependencies ?? []).join(', ') || '—'}
            />
            <Field
              label="Resource limits"
              value={Object.entries(plan.resource_limits ?? {})
                .map(([key, value]) => `${key}=${String(value)}`)
                .join(' · ')}
            />
          </dl>

          <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div>
              <p className="text-xs uppercase tracking-wider text-slate-500">
                Objectives
              </p>
              <Pre value={plan.objectives} />
            </div>
            <div>
              <p className="text-xs uppercase tracking-wider text-slate-500">
                Expected behaviour
              </p>
              <Pre value={plan.expected_behavior} />
            </div>
            <div>
              <p className="text-xs uppercase tracking-wider text-slate-500">
                Safety constraints
              </p>
              <Pre value={plan.safety_constraints} />
            </div>
            <div>
              <p className="text-xs uppercase tracking-wider text-slate-500">
                Derived from
              </p>
              <Pre value={plan.derived_from} />
            </div>
          </div>
        </Card>
      ) : null}

      <Card
        title="Replayed inputs"
        subtitle={`${detail.input_count} planned item(s), sanitized before replay`}
      >
        {(inputs ?? []).length === 0 ? (
          <p className="text-sm text-slate-400">
            No replay inputs have been recorded for this experiment yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-3 py-2">#</th>
                  <th className="px-3 py-2">Source</th>
                  <th className="px-3 py-2">Target</th>
                  <th className="px-3 py-2">Offset</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2">HTTP</th>
                  <th className="px-3 py-2">Redactions</th>
                  <th className="px-3 py-2">Reject reason</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {(inputs ?? []).map((item) => (
                  <tr key={item.id}>
                    <td className="px-3 py-2 text-slate-400">{item.plan_order}</td>
                    <td className="px-3 py-2 text-slate-300">{item.source}</td>
                    <td className="px-3 py-2 font-mono text-xs text-slate-300">
                      {item.method ? `${item.method} ` : ''}
                      {item.target_service}
                      {item.target_path}
                    </td>
                    <td className="px-3 py-2 text-slate-400">
                      {item.relative_offset_ms} ms
                    </td>
                    <td className="px-3 py-2">
                      <span className={`badge ${replayStatusStyle(item.status)}`}>
                        {item.status}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-slate-400">
                      {item.status_code ?? '—'}
                    </td>
                    <td className="px-3 py-2 text-xs text-slate-400">
                      {(item.redactions ?? []).length > 0
                        ? (item.redactions ?? []).join(', ')
                        : '—'}
                    </td>
                    <td className="px-3 py-2 text-xs text-argus-warning">
                      {item.reject_reason ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card
        title="Injected faults"
        subtitle="§22 — what ARGUS forced, and whether it actually activated"
      >
        {detail.faults.length === 0 ? (
          <p className="text-sm text-slate-400">
            This experiment injects no faults: it replays the recorded inputs
            unchanged, so any failure that appears is produced by the system
            itself.
          </p>
        ) : (
          <ul className="space-y-2">
            {detail.faults.map((fault: ReproductionFault) => (
              <li
                key={fault.id}
                className="rounded-md border border-slate-800 bg-slate-950/40 p-3"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`badge ${faultStatusStyle(fault.status)}`}>
                    {fault.status}
                  </span>
                  <span className="text-sm text-slate-200">
                    {FAULT_TYPE_LABELS[fault.fault_type] ?? fault.fault_type}
                  </span>
                  <span className="text-xs text-slate-500">
                    scope {fault.scope} · trigger {fault.trigger}
                  </span>
                </div>
                <p className="mt-1 text-sm text-slate-300">{faultSummary(fault)}</p>
                {fault.parameters && Object.keys(fault.parameters).length > 0 ? (
                  <p className="mt-1 font-mono text-xs text-slate-500">
                    {JSON.stringify(fault.parameters)}
                  </p>
                ) : null}
                {fault.result ? (
                  <p className="mt-1 text-xs text-slate-400">{fault.result}</p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card
        title="Repetitions"
        subtitle={`${detail.runs.length} run(s) of ${experiment.repetitions} planned`}
      >
        {detail.runs.length === 0 ? (
          <p className="text-sm text-slate-400">
            No repetition has run yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-3 py-2">Run</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2">Result</th>
                  <th className="px-3 py-2">Class</th>
                  <th className="px-3 py-2">Duration</th>
                  <th className="px-3 py-2">Replay ok/failed/rejected</th>
                  <th className="px-3 py-2">Observations</th>
                  <th className="px-3 py-2">Telemetry</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {detail.runs.map((run) => (
                  <tr key={run.id}>
                    <td className="px-3 py-2 text-slate-300">
                      #{run.run_index + 1}
                    </td>
                    <td className="px-3 py-2 text-slate-300">{run.status}</td>
                    <td className="px-3 py-2">
                      <span className={`badge ${resultStyle(run.result)}`}>
                        {run.result}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-xs text-slate-400">
                      {run.failure_classification
                        ? failureClassLabel(run.failure_classification)
                        : '—'}
                    </td>
                    <td className="px-3 py-2 text-slate-400">
                      {run.duration_ms != null
                        ? formatSeconds(run.duration_ms / 1000)
                        : '—'}
                    </td>
                    <td className="px-3 py-2 text-slate-400">
                      {run.replay_success_count}/{run.replay_failure_count}/
                      {run.replay_rejected_count}
                    </td>
                    <td className="px-3 py-2 text-slate-400">
                      {run.observation_count}
                    </td>
                    <td className="px-3 py-2 text-slate-400">
                      {formatBytes(run.telemetry_bytes)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {telemetry ? (
        <Card
          title="Captured telemetry"
          subtitle={`namespaced ${telemetry.namespace} — never mixed with production telemetry`}
        >
          <p className="text-sm text-slate-300">
            {captureCoverage(telemetry.matched_count, telemetry.expected_count).text}
            {telemetry.missing_count > 0
              ? ` · ${telemetry.missing_count} expected signal(s) never appeared`
              : ''}
          </p>
          {telemetry.items.length === 0 ? (
            <p className="mt-2 text-sm text-slate-400">
              No observations captured (yet).
            </p>
          ) : (
            <div className="mt-3 overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-800 text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                    <th className="px-3 py-2">Offset</th>
                    <th className="px-3 py-2">Signal</th>
                    <th className="px-3 py-2">Component</th>
                    <th className="px-3 py-2">Status</th>
                    <th className="px-3 py-2">Value</th>
                    <th className="px-3 py-2">Expected</th>
                    <th className="px-3 py-2">Detail</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800">
                  {telemetry.items.slice(0, 50).map((item) => (
                    <tr key={item.id}>
                      <td className="px-3 py-2 text-slate-400">
                        {item.relative_offset_ms} ms
                      </td>
                      <td className="px-3 py-2 text-slate-300">
                        {item.signal_type}
                        {item.error ? (
                          <span className="ml-2 text-xs text-argus-error">
                            error
                          </span>
                        ) : null}
                      </td>
                      <td className="px-3 py-2 text-slate-300">
                        {item.component_name ?? '—'}
                      </td>
                      <td className="px-3 py-2">
                        <span
                          className={`badge ${observationStatusStyle(item.status)}`}
                        >
                          {item.status}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-slate-300">
                        {item.value != null
                          ? `${item.value}${item.unit ? ` ${item.unit}` : ''}`
                          : item.duration_ms != null
                            ? `${item.duration_ms} ms`
                            : '—'}
                      </td>
                      <td className="px-3 py-2 text-slate-400">
                        {item.expected_value ?? '—'}
                      </td>
                      <td className="px-3 py-2 text-xs text-slate-400">
                        {item.message ?? item.operation ?? '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {telemetry.items.length > 50 ? (
                <p className="mt-2 text-xs text-slate-500">
                  Showing the first 50 of {telemetry.items.length} captured
                  observation(s).
                </p>
              ) : null}
            </div>
          )}
        </Card>
      ) : null}

      <Card
        title="Comparison with the original incident"
        subtitle="§28 — every dimension is explainable, none is a probability"
      >
        {!comparison || comparison.items.length === 0 ? (
          <p className="text-sm text-slate-400">
            No comparison has been produced yet. The comparator runs after a
            repetition has captured telemetry.
          </p>
        ) : (
          <div className="space-y-5">
            {comparison.aggregate ? (
              <p className="text-sm text-slate-300">
                {comparison.aggregate.runs} run(s) compared · mean similarity{' '}
                {comparison.aggregate.mean_similarity != null
                  ? comparison.aggregate.mean_similarity.toFixed(2)
                  : '—'}{' '}
                · buckets {comparison.aggregate.buckets.join(', ')}
              </p>
            ) : null}

            {comparison.items.map((row, index) => (
              <div
                key={row.id}
                className="rounded-md border border-slate-800 bg-slate-950/40 p-4"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm text-slate-200">
                    Run {index + 1}
                  </span>
                  <span className={`badge ${resultStyle(row.result)}`}>
                    {row.result}
                  </span>
                  <span className="text-xs text-slate-500">
                    overall similarity {row.overall_similarity}
                    {row.similarity_score != null
                      ? ` · mean ${row.similarity_score.toFixed(2)}`
                      : ''}
                  </span>
                </div>

                <div className="mt-3 grid grid-cols-1 gap-4 lg:grid-cols-2">
                  <div>
                    <p className="text-xs uppercase tracking-wider text-slate-500">
                      Failure sequence
                    </p>
                    <p className="mt-1 text-sm text-slate-300">
                      original: {formatSequence(row.sequence_original)}
                    </p>
                    <p className="text-sm text-slate-300">
                      reproduced: {formatSequence(row.sequence_reproduced)}
                    </p>
                    <p className="mt-1 text-xs text-slate-500">
                      order matched:{' '}
                      {row.sequence_match == null
                        ? 'not measured'
                        : row.sequence_match
                          ? 'yes'
                          : 'no'}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs uppercase tracking-wider text-slate-500">
                      Components
                    </p>
                    <p className="mt-1 text-sm text-slate-300">
                      overlap {formatOverlap(row.component_overlap)}
                    </p>
                    <p className="text-xs text-slate-400">
                      matched: {formatComponents(row.matched_components)}
                    </p>
                    <p className="text-xs text-slate-400">
                      missing: {formatComponents(row.missing_components)}
                    </p>
                    <p className="text-xs text-slate-400">
                      extra: {formatComponents(row.extra_components)}
                    </p>
                  </div>
                </div>

                {row.dimensions && Object.keys(row.dimensions).length > 0 ? (
                  <div className="mt-3">
                    <p className="text-xs uppercase tracking-wider text-slate-500">
                      Dimensions
                    </p>
                    <ul className="mt-1 grid grid-cols-1 gap-1 sm:grid-cols-2">
                      {similarityDimensionRows(row.dimensions).map((dimension) => (
                        <li
                          key={dimension.key}
                          className="flex items-center justify-between gap-2 text-sm"
                        >
                          <span className="text-slate-400">{dimension.label}</span>
                          <span className={`badge ${dimension.style}`}>
                            {scoreLabel(dimension.score)}
                            {dimension.score != null
                              ? ` (${dimension.score.toFixed(2)})`
                              : ''}
                          </span>
                        </li>
                      ))}
                    </ul>
                    {row.formula_reference ? (
                      <p className="mt-1 text-xs text-slate-500">
                        formulas: {row.formula_reference}
                      </p>
                    ) : null}
                  </div>
                ) : null}

                <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-3">
                  <div>
                    <p className="text-xs uppercase tracking-wider text-slate-500">
                      Metrics
                    </p>
                    <Pre value={row.metric_deltas} />
                  </div>
                  <div>
                    <p className="text-xs uppercase tracking-wider text-slate-500">
                      Errors
                    </p>
                    <Pre value={row.error_comparison} />
                  </div>
                  <div>
                    <p className="text-xs uppercase tracking-wider text-slate-500">
                      Trace topology
                    </p>
                    <Pre value={row.trace_topology} />
                  </div>
                </div>

                {row.explanation ? (
                  <p className="mt-3 text-sm text-slate-300">{row.explanation}</p>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card
        title="Hypothesis validation"
        subtitle="§31, §32 — the verdict, its evidence, and why it might be wrong"
      >
        {!validation ? (
          <p className="text-sm text-slate-400">
            No verdict has been recorded. A validation is written when the
            experiment reaches the comparing phase.
          </p>
        ) : (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`badge ${outcomeStyle(validation.outcome)}`}>
                {validation.outcome}
              </span>
              <span className="text-xs text-slate-500">
                confidence {validation.confidence}
              </span>
            </div>
            <p className="text-sm leading-relaxed text-slate-300">
              {validation.summary}
            </p>

            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              <div>
                <p className="text-xs uppercase tracking-wider text-slate-500">
                  Supporting observations
                </p>
                <ObservationList items={validation.supporting_observations} />
              </div>
              <div>
                <p className="text-xs uppercase tracking-wider text-slate-500">
                  Contradicting observations
                </p>
                <ObservationList items={validation.contradicting_observations} />
              </div>
            </div>

            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              <div>
                <p className="text-xs uppercase tracking-wider text-slate-500">
                  Environment differences
                </p>
                <ObservationList items={validation.environment_differences} />
              </div>
              <div>
                <p className="text-xs uppercase tracking-wider text-slate-500">
                  Missing inputs
                </p>
                {(validation.missing_inputs ?? []).length === 0 ? (
                  <p className="mt-1 text-sm text-slate-500">—</p>
                ) : (
                  <ul className="mt-1 space-y-1 text-sm text-slate-300">
                    {(validation.missing_inputs ?? []).map((item, index) => (
                      <li key={index}>{item}</li>
                    ))}
                  </ul>
                )}
              </div>
            </div>

            {validation.determinism ? (
              <div>
                <p className="text-xs uppercase tracking-wider text-slate-500">
                  Repeatability
                </p>
                <p className="mt-1 text-sm text-slate-300">
                  {determinismNote(validation) ?? 'not measured'}
                </p>
                <Pre value={validation.determinism} />
              </div>
            ) : null}

            {(validation.limitations ?? []).length > 0 ? (
              <div>
                <p className="text-xs uppercase tracking-wider text-slate-500">
                  Limitations
                </p>
                <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-slate-300">
                  {(validation.limitations ?? []).map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        )}
      </Card>

      <Card
        title="Environment difference"
        subtitle="§33 — original vs sandbox, and what could not be sanitized"
      >
        {originals.length === 0 && sandboxes.length === 0 ? (
          <p className="text-sm text-slate-400">
            No environment snapshots have been captured yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-3 py-2">Side</th>
                  <th className="px-3 py-2">Captured</th>
                  <th className="px-3 py-2">App version</th>
                  <th className="px-3 py-2">Schema</th>
                  <th className="px-3 py-2">Runtime</th>
                  <th className="px-3 py-2">Dependencies</th>
                  <th className="px-3 py-2">Sanitization</th>
                  <th className="px-3 py-2">Hash</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {[originals, sandboxes].flat().map((snapshot) => (
                  <tr key={snapshot.id}>
                    <td className="px-3 py-2 text-slate-300">
                      {snapshot.source}
                    </td>
                    <td className="px-3 py-2 text-slate-400">
                      {formatDate(snapshot.captured_at)}
                    </td>
                    <td className="px-3 py-2 text-slate-300">
                      {snapshot.application_version ?? '—'}
                    </td>
                    <td className="px-3 py-2 text-slate-300">
                      {snapshot.schema_version ?? '—'}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-slate-400">
                      {JSON.stringify(snapshot.runtime_versions ?? {})}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-slate-400">
                      {JSON.stringify(snapshot.dependency_versions ?? {})}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-slate-400">
                      {JSON.stringify(snapshot.sanitization ?? {})}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-slate-500">
                      {snapshot.content_hash
                        ? `${snapshot.content_hash.slice(0, 12)}…`
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card
        title="Artifacts"
        subtitle={`§41, §42 — immutable, content-hashed (${(artifactsResponse?.items ?? []).length})`}
      >
        {(artifactsResponse?.items ?? []).length === 0 ? (
          <p className="text-sm text-slate-400">
            No artifacts have been written yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-3 py-2">Type</th>
                  <th className="px-3 py-2">Name</th>
                  <th className="px-3 py-2">Size</th>
                  <th className="px-3 py-2">SHA-256</th>
                  <th className="px-3 py-2">Immutable</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {(artifactsResponse?.items ?? []).map(
                  (artifact: ReproductionArtifact) => (
                    <tr key={artifact.id}>
                      <td className="px-3 py-2 text-slate-300">
                        {artifact.artifact_type}
                      </td>
                      <td className="px-3 py-2 font-mono text-xs text-slate-300">
                        {artifact.name}
                      </td>
                      <td className="px-3 py-2 text-slate-400">
                        {formatBytes(artifact.size_bytes)}
                      </td>
                      <td className="px-3 py-2 font-mono text-xs text-slate-500">
                        {artifact.content_hash.slice(0, 16)}…
                      </td>
                      <td className="px-3 py-2 text-slate-400">
                        {artifact.immutable ? 'yes' : 'no'}
                      </td>
                    </tr>
                  )
                )}
              </tbody>
            </table>
          </div>
        )}

        {manifest ? (
          <details className="mt-4">
            <summary className="cursor-pointer text-sm text-slate-300">
              Reproduction manifest (§43)
            </summary>
            <div className="mt-2">
              <Pre value={manifest} />
            </div>
          </details>
        ) : null}
      </Card>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className="mt-1 text-sm text-slate-200">{value || '—'}</dd>
    </div>
  );
}

function ObservationList({ items }: { items?: unknown[] | null }) {
  const rows = items ?? [];
  if (rows.length === 0) {
    return <p className="mt-1 text-sm text-slate-500">—</p>;
  }
  return (
    <ul className="mt-1 space-y-1 text-sm text-slate-300">
      {rows.map((item, index) => (
        <li key={index}>
          {typeof item === 'string' ? item : JSON.stringify(item)}
        </li>
      ))}
    </ul>
  );
}
