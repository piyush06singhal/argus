import Link from 'next/link';

import {
  api,
  formatDate,
  type Project,
  type RemediationAction,
  type RemediationActionTypeInfo,
  type RemediationBreaker,
  type RemediationControl,
  type RemediationMetrics,
  type RemediationPolicy,
} from '@/lib/api';
import {
  actionTypeLabel,
  attentionCounts,
  blastRadiusLabel,
  effectiveMode,
  failureReasonLabel,
  isInFlight,
  MODE_EXPLANATIONS,
  outcomeLabel,
  outcomeStyle,
  REMEDIATION_DISCLAIMER,
  requiresHumanApproval,
  riskStyle,
  sortActionsForReview,
  statusLabel,
  statusStyle,
} from '@/lib/remediation';

export const metadata = {
  title: 'Remediation',
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

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className="text-slate-200">{value}</dd>
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-slate-400">{children}</p>;
}

/**
 * The remediation console (§44, §47, §48).
 *
 * Three rules shape this page, and they are the phase's rules rather than design
 * preferences:
 *
 * 1. **The regime is stated before the queue.** A reader must know whether
 *    anything can run at all — an empty queue under `OBSERVE_ONLY` means
 *    something very different from an empty queue under `AUTONOMOUS`.
 * 2. **Refusals are counted as prominently as successes.** `BLOCKED`,
 *    `REJECTED` and `EXPIRED` are tiles, not silence: policy doing its job looks
 *    like a list of things ARGUS declined to do.
 * 3. **The queue is ordered by what waits on a human**, not by recency, because
 *    the newest action is rarely the one that is stuck.
 */
export default async function RemediationPage({
  searchParams,
}: {
  searchParams: { project_id?: string };
}) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';
  //: The requested project is honoured or clearly replaced, never silently
  //: substituted for a different scope than the reader asked for.

  let projects: Project[] = [];
  try {
    const response = await api.listProjects(1, 50);
    projects = response.items;
  } catch {
    projects = [];
  }
  const project = projects.find((item) => item.id === requested) ?? projects[0];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Safe Remediation</h1>
        <p className="mt-1 text-sm text-slate-400">
          ARGUS proposes a remediation from stored evidence, runs it through
          validation, safety, policy and authority, executes only what those gates
          allowed, verifies the effect against telemetry, and rolls back when the
          effect was harmful. Nothing here is authorized by a model.
        </p>
        <p className="mt-2 text-xs text-slate-500">{REMEDIATION_DISCLAIMER}</p>
      </div>

      {projects.length === 0 ? (
        <Card title="No projects" subtitle="Remediation is project-scoped">
          <Empty>
            No project could be loaded, so there is nothing to scope remediation
            to. Project scope is how the API proves ownership.
          </Empty>
        </Card>
      ) : (
        <ProjectPicker projects={projects} selectedId={project?.id ?? ''} />
      )}

      {project ? <ProjectConsole projectId={project.id} /> : null}
    </div>
  );
}

function ProjectPicker({
  projects,
  selectedId,
}: {
  projects: Project[];
  selectedId: string;
}) {
  return (
    <Card title="Project scope" subtitle="Every read and write is scoped to a project">
      <div className="flex flex-wrap gap-2">
        {projects.map((item) => (
          <Link
            key={item.id}
            href={`/remediation?project_id=${encodeURIComponent(item.id)}`}
            className={`badge ${
              item.id === selectedId
                ? 'bg-argus-accent/20 text-argus-accent'
                : 'bg-slate-800 text-slate-300'
            }`}
          >
            {item.name}
          </Link>
        ))}
      </div>
    </Card>
  );
}

async function ProjectConsole({ projectId }: { projectId: string }) {
  let metrics: RemediationMetrics | null = null;
  let policy: RemediationPolicy | null = null;
  let actions: RemediationAction[] = [];
  let controls: RemediationControl[] = [];
  let breakers: RemediationBreaker[] = [];
  let registry: RemediationActionTypeInfo[] = [];
  let executionEnabled = true;
  const failures: string[] = [];

  const [metricsResult, policyResult, actionsResult, controlsResult, breakersResult, registryResult] =
    await Promise.allSettled([
      api.remediationMetrics(projectId),
      api.getRemediationPolicy(projectId),
      api.listRemediationActions({ project_id: projectId, limit: 50 }),
      api.listRemediationControls(projectId),
      api.listRemediationBreakers(projectId),
      api.listRemediationActionTypes(),
    ]);

  if (metricsResult.status === 'fulfilled') metrics = metricsResult.value;
  else failures.push('metrics could not be loaded');

  if (policyResult.status === 'fulfilled') policy = policyResult.value;
  else failures.push('the policy could not be loaded');

  if (actionsResult.status === 'fulfilled') actions = actionsResult.value.actions;
  else failures.push('the action queue could not be loaded');

  if (controlsResult.status === 'fulfilled') controls = controlsResult.value.controls;
  else failures.push('the control plane could not be loaded');

  if (breakersResult.status === 'fulfilled') breakers = breakersResult.value.breakers;
  else failures.push('the circuit breakers could not be loaded');

  if (registryResult.status === 'fulfilled') {
    registry = registryResult.value.actions;
    executionEnabled = registryResult.value.execution_enabled;
  } else {
    failures.push('the action registry could not be loaded');
  }

  const regime = effectiveMode(policy);
  const sorted = sortActionsForReview(actions);
  const attention = attentionCounts(actions);
  const openBreakers = breakers.filter((breaker) => breaker.state !== 'CLOSED');
  const liveControls = controls.filter((control) => control.is_current);

  return (
    <>
      <RegimeCard
        projectId={projectId}
        regime={regime}
        policy={policy}
        executionEnabled={executionEnabled}
      />

      {failures.length > 0 ? (
        <Card title="Some sections could not be loaded">
          <ul className="list-disc space-y-1 pl-5 text-sm text-argus-warning">
            {failures.map((failure) => (
              <li key={failure}>{failure}</li>
            ))}
          </ul>
        </Card>
      ) : null}

      {metrics ? (
        <Card
          title="Activity"
          subtitle="Refusals are counted here deliberately: a policy doing its job looks like this"
        >
          <dl className="grid gap-4 sm:grid-cols-3 lg:grid-cols-6">
            <Metric label="Actions" value={String(metrics.total_actions)} />
            <Metric
              label="Awaiting a human"
              value={String(metrics.awaiting_approval)}
            />
            <Metric label="Executed" value={String(metrics.executions_attempted)} />
            <Metric
              label="Applied an effect"
              value={String(metrics.executions_with_effect)}
            />
            <Metric
              label="Verified"
              value={String(metrics.verifications_passed)}
            />
            <Metric
              label="Inconclusive"
              value={String(metrics.verifications_inconclusive)}
            />
            <Metric
              label="Failed verification"
              value={String(metrics.verifications_failed)}
            />
            <Metric
              label="Rolled back"
              value={String(metrics.rollbacks_succeeded)}
            />
            <Metric
              label="Rollbacks failed"
              value={String(metrics.rollbacks_failed)}
            />
            <Metric
              label="Autonomous authorizations"
              value={String(metrics.autonomous_authorizations)}
            />
            <Metric
              label="Human authorizations"
              value={String(metrics.human_authorizations)}
            />
            <Metric
              label="Controls in force"
              value={String(metrics.controls_in_force)}
            />
          </dl>

          {Object.keys(metrics.by_failure_reason).length > 0 ? (
            <div className="mt-4">
              <h3 className="text-xs uppercase tracking-wider text-slate-500">
                Why actions did not run
              </h3>
              <ul className="mt-2 space-y-1 text-sm text-slate-300">
                {Object.entries(metrics.by_failure_reason)
                  .sort(([, left], [, right]) => right - left)
                  .map(([reason, count]) => (
                    <li key={reason} className="flex justify-between gap-4">
                      <span>{failureReasonLabel(reason)}</span>
                      <span className="text-slate-500">{count}</span>
                    </li>
                  ))}
              </ul>
            </div>
          ) : null}
        </Card>
      ) : null}

      <Card
        title="Action queue"
        subtitle="Ordered by what is waiting on a person, not by recency"
      >
        {sorted.length === 0 ? (
          <Empty>
            No remediation has been proposed for this project.
            {regime.mode === 'OBSERVE_ONLY'
              ? ' No new proposals will be acted on while the regime is OBSERVE_ONLY.'
              : ' The planner produces a proposal only when stored evidence supports one.'}
          </Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase tracking-wider text-slate-500">
                <tr>
                  <th className="py-2 pr-4">Action</th>
                  <th className="py-2 pr-4">Status</th>
                  <th className="py-2 pr-4">Risk</th>
                  <th className="py-2 pr-4">Scope</th>
                  <th className="py-2 pr-4">Authority</th>
                  <th className="py-2 pr-4">Created</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {sorted.map((item) => (
                  <tr key={item.id} className="align-top">
                    <td className="py-3 pr-4">
                      <Link
                        href={`/remediation/${encodeURIComponent(
                          item.id
                        )}?project_id=${encodeURIComponent(projectId)}`}
                        className="font-medium text-argus-accent hover:underline"
                      >
                        {actionTypeLabel(item.action_type)}
                      </Link>
                      <p className="mt-1 max-w-md text-xs text-slate-400">
                        {item.headline}
                      </p>
                      {item.failure_reason ? (
                        <p className="mt-1 text-xs text-argus-warning">
                          {failureReasonLabel(item.failure_reason)}
                        </p>
                      ) : null}
                    </td>
                    <td className="py-3 pr-4">
                      <span className={`badge ${statusStyle(item.status)}`}>
                        {item.status}
                      </span>
                      <p className="mt-1 max-w-xs text-xs text-slate-500">
                        {statusLabel(item.status)}
                      </p>
                      {item.outcome ? (
                        <p className={`mt-1 badge ${outcomeStyle(item.outcome)}`}>
                          {outcomeLabel(item.outcome)}
                        </p>
                      ) : null}
                    </td>
                    <td className="py-3 pr-4">
                      <span className={`badge ${riskStyle(item.risk_level)}`}>
                        {item.risk_level}
                      </span>
                    </td>
                    <td className="py-3 pr-4 text-slate-300">
                      {blastRadiusLabel(item.blast_radius, item.blast_radius_percent)}
                    </td>
                    <td className="py-3 pr-4 max-w-xs text-xs text-slate-400">
                      {requiresHumanApproval(item)}
                      {item.canary_required ? (
                        <p className="mt-1 text-argus-info">
                          Canary first ({item.canary_stage})
                        </p>
                      ) : null}
                    </td>
                    <td className="py-3 pr-4 text-xs text-slate-500">
                      {formatDate(item.created_at)}
                      {item.execution_mode !== 'OBSERVE_ONLY' ? (
                        <p className="mt-1">{item.execution_mode}</p>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card
          title="Control plane"
          subtitle="ARGUS's own runtime is the only thing a native action can change"
        >
          {liveControls.length === 0 ? (
            <Empty>
              Nothing is paused, disabled or suppressed by remediation. A control
              that expires is retired rather than left labelled current.
            </Empty>
          ) : (
            <ul className="space-y-3">
              {liveControls.map((control) => (
                <li key={control.id} className="rounded-md bg-slate-800/50 p-3">
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="font-medium text-slate-200">
                      {control.scope_key}
                    </span>
                    <span className="badge bg-slate-800 text-slate-300">
                      {control.kind}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-slate-400">
                    {control.state}
                    {control.previous_state
                      ? ` (was ${control.previous_state})`
                      : ''}{' '}
                    · applied {formatDate(control.applied_at)}
                    {control.expires_at
                      ? ` · expires ${formatDate(control.expires_at)}`
                      : ' · no expiry'}
                  </p>
                  {!control.effective ? (
                    <p className="mt-1 text-xs text-argus-warning">
                      Past its deadline: the control plane already treats it as
                      reverted, and the sweep will retire the row.
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card
          title="Circuit breakers"
          subtitle="One action type failing repeatedly stops being attempted — without freezing everything else"
        >
          {breakers.length === 0 ? (
            <Empty>No action type has failed in this project yet.</Empty>
          ) : (
            <ul className="space-y-3">
              {breakers.map((breaker) => (
                <li
                  key={breaker.action_type}
                  className="rounded-md bg-slate-800/50 p-3"
                >
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="font-medium text-slate-200">
                      {actionTypeLabel(breaker.action_type)}
                    </span>
                    <span
                      className={`badge ${
                        breaker.state === 'CLOSED'
                          ? 'bg-argus-success/15 text-argus-success'
                          : breaker.state === 'HALF_OPEN'
                            ? 'bg-argus-info/20 text-argus-info'
                            : 'bg-argus-error/15 text-argus-error'
                      }`}
                    >
                      {breaker.state}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-slate-400">
                    {breaker.consecutive_failures} consecutive failures of{' '}
                    {breaker.threshold} before it opens · {breaker.total_successes}{' '}
                    succeeded, {breaker.total_failures} failed
                  </p>
                  {breaker.opened_until ? (
                    <p className="mt-1 text-xs text-slate-500">
                      A single probe is allowed after{' '}
                      {formatDate(breaker.opened_until)}
                    </p>
                  ) : null}
                  {breaker.last_trip_reason ? (
                    <p className="mt-1 text-xs text-argus-warning">
                      {breaker.last_trip_reason}
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      {attention.inFlight > 0 ? (
        <Card title="In flight right now">
          <ul className="space-y-2 text-sm text-slate-300">
            {actions
              .filter((item) => isInFlight(item.status))
              .map((item) => (
                <li key={item.id}>
                  <Link
                    href={`/remediation/${encodeURIComponent(
                      item.id
                    )}?project_id=${encodeURIComponent(projectId)}`}
                    className="text-argus-accent hover:underline"
                  >
                    {actionTypeLabel(item.action_type)}
                  </Link>{' '}
                  — {statusLabel(item.status)}
                </li>
              ))}
          </ul>
        </Card>
      ) : null}

      <Card
        title="Registered actions"
        subtitle="The closed set. There is no endpoint that accepts a command, a script or a URL"
      >
        {registry.length === 0 ? (
          <Empty>The registry could not be read.</Empty>
        ) : (
          <ul className="space-y-3">
            {registry.map((definition) => (
              <li
                key={definition.action_type}
                className="rounded-md bg-slate-800/50 p-3"
              >
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="font-medium text-slate-200">
                    {actionTypeLabel(definition.action_type)}
                  </span>
                  <span className="flex flex-wrap items-center gap-2">
                    <span className={`badge ${riskStyle(definition.risk_level)}`}>
                      {definition.risk_level}
                    </span>
                    <span className="badge bg-slate-800 text-slate-300">
                      {definition.maximum_blast_radius}
                    </span>
                    {definition.requires_human_approval ? (
                      <span className="badge bg-argus-warning/20 text-argus-warning">
                        human approval
                      </span>
                    ) : null}
                    {definition.supports_autonomous_execution ? (
                      <span className="badge bg-argus-info/20 text-argus-info">
                        autonomous eligible
                      </span>
                    ) : null}
                  </span>
                </div>
                <p className="mt-1 text-xs text-slate-400">
                  {definition.description}
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  {definition.reversible
                    ? `Rollback: ${definition.rollback_strategy}`
                    : 'No platform rollback — a human would have to undo it'}{' '}
                  · Verified with{' '}
                  {definition.verification_plan.join(', ') || 'no checks'}
                </p>
                {definition.unavailable_reason ? (
                  <p className="mt-1 text-xs text-argus-warning">
                    Not executable in this build: {definition.unavailable_reason}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}

function RegimeCard({
  projectId,
  regime,
  policy,
  executionEnabled,
}: {
  projectId: string;
  regime: ReturnType<typeof effectiveMode>;
  policy: RemediationPolicy | null;
  executionEnabled: boolean;
}) {
  const tone = regime.mode === 'EMERGENCY_STOP'
    ? 'bg-argus-error/15 text-argus-error'
    : regime.mode === 'OBSERVE_ONLY' || regime.mode === 'DRY_RUN'
      ? 'bg-slate-800 text-slate-300'
      : 'bg-argus-info/20 text-argus-info';

  return (
    <Card
      title="Regime"
      subtitle={
        regime.configured
          ? 'Configured for this scope'
          : 'No policy row — the restrictive default applies'
      }
    >
      <div className="flex flex-wrap items-center gap-3">
        <span className={`badge ${tone}`}>{regime.mode}</span>
        {policy?.emergency_stop_active ? (
          <span className="badge bg-argus-error/20 text-argus-error">
            emergency stop engaged
          </span>
        ) : null}
        {!executionEnabled ? (
          <span className="badge bg-argus-warning/20 text-argus-warning">
            execution disabled in this process
          </span>
        ) : null}
        <Link
          href={`/remediation/policy?project_id=${encodeURIComponent(projectId)}`}
          className="text-sm text-argus-accent hover:underline"
        >
          Review the policy
        </Link>
      </div>
      <p className="mt-2 text-sm text-slate-300">{regime.explanation}</p>
      {policy ? (
        <dl className="mt-4 grid gap-4 sm:grid-cols-3 lg:grid-cols-6">
          <Metric
            label="Autonomous ceiling"
            value={policy.autonomous_max_risk}
          />
          <Metric
            label="Actions per window"
            value={String(policy.max_actions_per_window)}
          />
          <Metric label="Cooldown" value={`${policy.cooldown_seconds}s`} />
          <Metric
            label="Concurrent actions"
            value={String(policy.max_concurrent_actions)}
          />
          <Metric
            label="Blast radius"
            value={blastRadiusLabel(
              policy.max_blast_radius_scope,
              policy.max_blast_radius_percent
            )}
          />
          <Metric
            label="Verification window"
            value={`${policy.verification_window_seconds}s`}
          />
        </dl>
      ) : null}
      {(policy?.clamped.length ?? 0) > 0 ? (
        <p className="mt-3 text-xs text-argus-warning">
          Operator ceilings were applied on top of the stored policy:{' '}
          {policy?.clamped.join(', ')}
        </p>
      ) : null}
      <p className="mt-3 text-xs text-slate-500">
        {MODE_EXPLANATIONS[regime.mode]}
      </p>
      {policy ? (
        <p className="mt-1 text-xs text-slate-500">
          Last updated by {policy.updated_by ?? 'unknown'} · revision{' '}
          {policy.revision ?? '—'} · source {policy.source}
        </p>
      ) : null}
    </Card>
  );
}
