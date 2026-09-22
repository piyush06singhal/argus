import Link from 'next/link';
import { notFound } from 'next/navigation';

import {
  api,
  formatDate,
  type RemediationActionDetail,
  type RemediationAuditEvent,
  type RemediationExecutionRecord,
  type RemediationVerificationRecord,
} from '@/lib/api';
import {
  actionTypeLabel,
  blastRadiusLabel,
  confidenceLabel,
  failureReasonLabel,
  outcomeLabel,
  outcomeStyle,
  policyDecisionLabel,
  policyDecisionStyle,
  REMEDIATION_DISCLAIMER,
  requiresHumanApproval,
  reversibilityLabel,
  riskStyle,
  safetyLabel,
  safetyStyle,
  statusLabel,
  statusStyle,
  verdictLabel,
  verdictStyle,
} from '@/lib/remediation';

import RemediationActions from './RemediationActions';

export const metadata = {
  title: 'Remediation action',
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

/**
 * One remediation action, with its whole evidence chain (§12, §44).
 *
 * The page is deliberately organised as *gates in order* — proposal, safety,
 * policy, authority, execution, verification, rollback, audit — rather than as a
 * status summary, because the question an engineer asks is "which gate decided
 * this?" and a status alone cannot answer it. The audit trail is shown last and
 * in full, including whether its hash chain still verifies.
 */
export default async function RemediationActionPage({
  params,
  searchParams,
}: {
  params: { actionId: string };
  searchParams: { project_id?: string };
}) {
  const projectId =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  let detail: RemediationActionDetail | null = null;
  try {
    detail = await api.getRemediationAction(params.actionId, projectId || undefined);
  } catch {
    detail = null;
  }
  if (!detail) {
    notFound();
  }

  const action = detail.action;
  //: The newest of each record type is the one that decided the current state.
  const latestVerification = detail.verifications.at(-1) ?? null;
  const latestRollback = detail.rollbacks.at(-1) ?? null;

  return (
    <div className="space-y-6">
      <div>
        <Link
          href={`/remediation?project_id=${encodeURIComponent(action.project_id)}`}
          className="text-sm text-argus-accent hover:underline"
        >
          ← Remediation console
        </Link>
        <h1 className="mt-2 text-2xl font-semibold text-slate-100">
          {actionTypeLabel(action.action_type)}
        </h1>
        <p className="mt-1 text-sm text-slate-400">{action.headline}</p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className={`badge ${statusStyle(action.status)}`}>
            {action.status}
          </span>
          <span className="badge bg-slate-800 text-slate-300">
            {statusLabel(action.status)}
          </span>
          <span className={`badge ${riskStyle(action.risk_level)}`}>
            risk {action.risk_level}
          </span>
          <span className="badge bg-slate-800 text-slate-300">
            {blastRadiusLabel(action.blast_radius, action.blast_radius_percent)}
          </span>
          {action.outcome ? (
            <span className={`badge ${outcomeStyle(action.outcome)}`}>
              {outcomeLabel(action.outcome)}
            </span>
          ) : null}
          {action.canary_required ? (
            <span className="badge bg-argus-info/20 text-argus-info">
              canary {action.canary_stage}
            </span>
          ) : null}
        </div>
        <p className="mt-2 text-xs text-slate-500">{REMEDIATION_DISCLAIMER}</p>
      </div>

      {action.failure_reason ? (
        <Card
          title="Why this is not running"
          subtitle="A recorded refusal, not a silent no-op"
        >
          <p className="text-sm text-argus-warning">
            {failureReasonLabel(action.failure_reason)}
          </p>
          {action.failure_detail ? (
            <p className="mt-1 text-xs text-slate-400">{action.failure_detail}</p>
          ) : null}
        </Card>
      ) : null}

      <RemediationActions
        actionId={action.id}
        projectId={action.project_id}
        status={action.status}
        executionMode={action.execution_mode}
        rollbackStrategy={action.rollback_strategy}
        allowedTransitions={detail.allowed_transitions}
      />

      <Card title="The action" subtitle="What it targets, and under what rules">
        <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Metric label="Regime" value={action.execution_mode} />
          <Metric label="Adapter" value={action.adapter_kind} />
          <Metric label="Attempt" value={String(action.attempt)} />
          <Metric label="Proposed by" value={action.created_by} />
          <Metric label="Authorization" value={requiresHumanApproval(action)} />
          <Metric label="Reversibility" value={reversibilityLabel(action)} />
          <Metric label="Proposed at" value={formatDate(action.created_at)} />
          <Metric
            label="Expires"
            value={action.expires_at ? formatDate(action.expires_at) : 'never'}
          />
          <Metric
            label="Approved at"
            value={action.approved_at ? formatDate(action.approved_at) : '—'}
          />
          <Metric
            label="Authorized at"
            value={action.authorized_at ? formatDate(action.authorized_at) : '—'}
          />
          <Metric
            label="Started at"
            value={action.started_at ? formatDate(action.started_at) : '—'}
          />
          <Metric
            label="Completed at"
            value={action.completed_at ? formatDate(action.completed_at) : '—'}
          />
        </dl>

        {action.parameters && Object.keys(action.parameters).length > 0 ? (
          <div className="mt-4">
            <h3 className="text-xs uppercase tracking-wider text-slate-500">
              Parameters (validated against the registry)
            </h3>
            <ul className="mt-2 space-y-1 font-mono text-xs text-slate-300">
              {Object.entries(action.parameters).map(([key, value]) => (
                <li key={key}>
                  {key}: {JSON.stringify(value)}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {action.preconditions && action.preconditions.length > 0 ? (
          <div className="mt-4">
            <h3 className="text-xs uppercase tracking-wider text-slate-500">
              Preconditions checked before execution
            </h3>
            <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-slate-400">
              {action.preconditions.map((condition, index) => (
                <li key={index}>{JSON.stringify(condition)}</li>
              ))}
            </ul>
          </div>
        ) : null}

        {action.rollback_plan ? (
          <div className="mt-4">
            <h3 className="text-xs uppercase tracking-wider text-slate-500">
              Rollback plan (declared before execution, never discovered after)
            </h3>
            <pre className="mt-2 overflow-x-auto rounded-md bg-slate-900 p-3 text-xs text-slate-300">
              {JSON.stringify(action.rollback_plan, null, 2)}
            </pre>
          </div>
        ) : null}
      </Card>

      {detail.proposal ? (
        <Card
          title="Proposal"
          subtitle={`Provenance: ${detail.proposal.source_type}${
            detail.proposal.strategy ? ` · strategy ${detail.proposal.strategy}` : ''
          }`}
        >
          <div className="space-y-3 text-sm text-slate-300">
            <p>
              <span className="text-slate-500">Problem: </span>
              {detail.proposal.problem}
            </p>
            <p>
              <span className="text-slate-500">Recommended: </span>
              {detail.proposal.recommended_action}
            </p>
            <p>
              <span className="text-slate-500">Expected effect: </span>
              {detail.proposal.expected_effect}
            </p>
            {detail.proposal.rationale ? (
              <p>
                <span className="text-slate-500">Rationale: </span>
                {detail.proposal.rationale}
              </p>
            ) : null}
            <p className="text-xs text-slate-500">
              Confidence: {confidenceLabel(detail.proposal.confidence)}
              {detail.proposal.confidence_reason
                ? ` — ${detail.proposal.confidence_reason}`
                : ''}
            </p>
          </div>

          {detail.proposal.limitations && detail.proposal.limitations.length > 0 ? (
            <div className="mt-3">
              <h3 className="text-xs uppercase tracking-wider text-slate-500">
                Limitations the proposal itself declares
              </h3>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-slate-400">
                {detail.proposal.limitations.map((limitation) => (
                  <li key={limitation}>{limitation}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {detail.proposal.supporting_evidence &&
          detail.proposal.supporting_evidence.length > 0 ? (
            <div className="mt-3">
              <h3 className="text-xs uppercase tracking-wider text-slate-500">
                Evidence this came from
              </h3>
              <pre className="mt-2 overflow-x-auto rounded-md bg-slate-900 p-3 text-xs text-slate-300">
                {JSON.stringify(detail.proposal.supporting_evidence, null, 2)}
              </pre>
            </div>
          ) : null}
        </Card>
      ) : null}

      <Card
        title="Gates, in order"
        subtitle="Safety cannot override policy, and policy cannot override safety"
      >
        <div className="space-y-4">
          {detail.assessments.length === 0 ? (
            <p className="text-sm text-slate-400">No safety assessment recorded.</p>
          ) : (
            detail.assessments.map((assessment) => (
              <div key={assessment.id} className="rounded-md bg-slate-800/50 p-3">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className={`badge ${safetyStyle(assessment.status)}`}>
                    {safetyLabel(assessment.status)}
                  </span>
                  <span className="text-xs text-slate-500">
                    {formatDate(assessment.created_at)} · by {assessment.assessed_by}
                  </span>
                </div>
                <p className="mt-1 text-xs text-slate-400">
                  Reversible: {assessment.reversible ? 'yes' : 'no'} · blast radius{' '}
                  {blastRadiusLabel(
                    assessment.blast_radius,
                    assessment.blast_radius_percent
                  )}
                  {assessment.requires_human_approval
                    ? ' · requires a human'
                    : ''}
                </p>
                {assessment.blocking && assessment.blocking.length > 0 ? (
                  <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-argus-error">
                    {assessment.blocking.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                ) : null}
                {assessment.warnings && assessment.warnings.length > 0 ? (
                  <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-argus-warning">
                    {assessment.warnings.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                ) : null}
                {assessment.checks && assessment.checks.length > 0 ? (
                  <ul className="mt-2 space-y-1 text-xs text-slate-300">
                    {assessment.checks.map((check, index) => (
                      <li key={index}>
                        <span className="text-slate-500">
                          {String(check.name ?? check.check ?? 'check')}:{' '}
                        </span>
                        {String(
                          check.result ?? check.outcome ?? check.detail ?? ''
                        )}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ))
          )}
        </div>

        <div className="mt-4 space-y-4">
          {detail.policy_decisions.length === 0 ? (
            <p className="text-sm text-slate-400">No policy evaluation recorded.</p>
          ) : (
            detail.policy_decisions.map((decision) => (
              <div key={decision.id} className="rounded-md bg-slate-800/50 p-3">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className={`badge ${policyDecisionStyle(decision.decision)}`}>
                    {policyDecisionLabel(decision.decision)}
                  </span>
                  <span className="text-xs text-slate-500">
                    regime {decision.execution_mode} ·{' '}
                    {formatDate(decision.created_at)}
                  </span>
                </div>
                {decision.requires_canary ? (
                  <p className="mt-1 text-xs text-argus-info">
                    A canary step is required before the full effect.
                  </p>
                ) : null}
                {decision.reasons && decision.reasons.length > 0 ? (
                  <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-slate-300">
                    {decision.reasons.map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>
                ) : null}
                {decision.matched_rules && decision.matched_rules.length > 0 ? (
                  <ul className="mt-2 space-y-1 font-mono text-[11px] text-slate-500">
                    {decision.matched_rules.map((rule, index) => (
                      <li key={index}>
                        {String(rule.rule)} → {String(rule.outcome)}:{' '}
                        {String(rule.detail)}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ))
          )}
        </div>
      </Card>

      <Card
        title="Authorization records"
        subtitle="Append-only: an autonomous authorization is recorded as one, and a human approval names the person"
      >
        {detail.approvals.length === 0 ? (
          <p className="text-sm text-slate-400">Nothing has authorized this yet.</p>
        ) : (
          <ul className="space-y-2 text-sm">
            {detail.approvals.map((approval) => (
              <li key={approval.id} className="rounded-md bg-slate-800/50 p-3">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="text-slate-200">
                    {approval.status} · {approval.actor_type}
                  </span>
                  <span className="text-xs text-slate-500">
                    {approval.actor ?? 'unnamed'} ·{' '}
                    {formatDate(approval.decided_at ?? approval.created_at)}
                  </span>
                </div>
                {approval.reason ? (
                  <p className="mt-1 text-xs text-slate-400">{approval.reason}</p>
                ) : null}
                {approval.expires_at ? (
                  <p className="mt-1 text-xs text-slate-500">
                    Decision window closed {formatDate(approval.expires_at)}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card
        title="Execution attempts"
        subtitle="'The handler ran' and 'something changed' are different facts"
      >
        {detail.executions.length === 0 ? (
          <p className="text-sm text-slate-400">Nothing has been attempted.</p>
        ) : (
          <div className="space-y-3">
            {detail.executions.map((execution: RemediationExecutionRecord) => (
              <div key={execution.id} className="rounded-md bg-slate-800/50 p-3">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="text-slate-200">
                    Attempt {execution.attempt} · {execution.status}
                  </span>
                  <span className="text-xs text-slate-500">
                    {execution.mode} · {execution.adapter_kind}
                    {execution.duration_ms != null
                      ? ` · ${execution.duration_ms}ms`
                      : ''}
                  </span>
                </div>
                <p className="mt-1 text-xs text-slate-400">
                  {execution.effect_applied
                    ? 'A real effect was applied.'
                    : 'No effect was applied (a dry run or a refusal).'}
                  {execution.executed_by ? ` Executed by ${execution.executed_by}.` : ''}
                </p>
                {execution.output_summary ? (
                  <p className="mt-1 text-xs text-slate-300">
                    {execution.output_summary}
                  </p>
                ) : null}
                {execution.error ? (
                  <p className="mt-1 text-xs text-argus-error">{execution.error}</p>
                ) : null}
                {execution.steps && execution.steps.length > 0 ? (
                  <ul className="mt-2 space-y-1 text-xs text-slate-400">
                    {execution.steps.map((step, index) => (
                      <li key={index}>
                        <span className="text-slate-500">
                          {String(step.step)}:{' '}
                        </span>
                        {String(step.detail)}
                      </li>
                    ))}
                  </ul>
                ) : null}
                <p className="mt-2 font-mono text-[11px] text-slate-600">
                  idempotency key {execution.idempotency_key}
                </p>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card
        title="Verification"
        subtitle="An action is verified against telemetry, not by reading back its own exit code"
      >
        {latestVerification === null ? (
          <p className="text-sm text-slate-400">
            No verification has run. Until one does, this action has no verified
            effect.
          </p>
        ) : (
          <VerificationBlock verification={latestVerification} />
        )}
        {detail.verifications.length > 1 ? (
          <p className="mt-3 text-xs text-slate-500">
            {detail.verifications.length} verification passes recorded; the latest
            is shown.
          </p>
        ) : null}
      </Card>

      <Card
        title="Rollback"
        subtitle="A rollback is itself verified — an attempted reversal is not a completed one"
      >
        {latestRollback === null ? (
          <p className="text-sm text-slate-400">
            No rollback has been attempted for this action.
          </p>
        ) : (
          <div className="rounded-md bg-slate-800/50 p-3">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="text-slate-200">
                {latestRollback.status} · {latestRollback.strategy}
              </span>
              <span className="text-xs text-slate-500">
                trigger {latestRollback.trigger} ·{' '}
                {formatDate(latestRollback.completed_at)}
              </span>
            </div>
            <p className="mt-1 text-xs text-slate-400">
              Requested by {latestRollback.requested_by ?? 'unknown'}.
              {latestRollback.verification_verdict
                ? ` Reversal ${verdictLabel(latestRollback.verification_verdict)}.`
                : ' The reversal itself was not verified.'}
            </p>
            {latestRollback.error ? (
              <p className="mt-1 text-xs text-argus-error">{latestRollback.error}</p>
            ) : null}
            {latestRollback.controls_reverted &&
            latestRollback.controls_reverted.length > 0 ? (
              <p className="mt-1 text-xs text-slate-500">
                Controls restored: {latestRollback.controls_reverted.join(', ')}
              </p>
            ) : null}
          </div>
        )}
      </Card>

      {detail.post_analysis ? (
        <Card
          title="Post-remediation analysis"
          subtitle="What the evidence looked like after the action, and what it cannot tell us"
        >
          <pre className="overflow-x-auto rounded-md bg-slate-900 p-3 text-xs text-slate-300">
            {JSON.stringify(detail.post_analysis, null, 2)}
          </pre>
        </Card>
      ) : null}

      <Card
        title="Audit trail"
        subtitle="Hash-chained: each entry binds the previous one"
      >
        {detail.audit_chain ? (
          <p
            className={`text-sm ${
              detail.audit_chain.intact ? 'text-argus-success' : 'text-argus-error'
            }`}
          >
            {detail.audit_chain.intact
              ? `Chain intact across ${detail.audit_chain.events} events.`
              : `Chain broken at event ${detail.audit_chain.broken_at}: ${
                  detail.audit_chain.reason ?? 'unknown'
                }`}
          </p>
        ) : null}
        <ol className="mt-3 space-y-3">
          {detail.audit.map((event: RemediationAuditEvent) => (
            <li key={event.id} className="rounded-md bg-slate-800/50 p-3">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="font-mono text-xs text-slate-300">
                  #{event.sequence} {event.event_type}
                </span>
                <span className="text-xs text-slate-500">
                  {event.actor ?? event.actor_type} · {formatDate(event.occurred_at)}
                </span>
              </div>
              <p className="mt-1 text-sm text-slate-200">{event.summary}</p>
              {event.from_status || event.to_status ? (
                <p className="mt-1 text-xs text-slate-500">
                  {event.from_status ?? '—'} → {event.to_status ?? '—'}
                </p>
              ) : null}
              {event.detail ? (
                <pre className="mt-2 overflow-x-auto text-[11px] text-slate-500">
                  {JSON.stringify(event.detail, null, 2)}
                </pre>
              ) : null}
              <p className="mt-1 font-mono text-[10px] text-slate-600">
                {event.entry_hash}
              </p>
            </li>
          ))}
        </ol>
        {detail.audit.length === 0 ? (
          <p className="text-sm text-slate-400">
            No audit events were recorded for this action.
          </p>
        ) : null}
      </Card>
    </div>
  );
}

function VerificationBlock({
  verification,
}: {
  verification: RemediationVerificationRecord;
}) {
  return (
    <div className="rounded-md bg-slate-800/50 p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className={`badge ${verdictStyle(verification.verdict)}`}>
          {verdictLabel(verification.verdict)}
        </span>
        <span className="text-xs text-slate-500">
          {verification.observation_seconds
            ? `${verification.observation_seconds}s observed`
            : 'observation window not recorded'}{' '}
          · {formatDate(verification.created_at)}
        </span>
      </div>
      <p className="mt-1 text-xs text-slate-400">
        {verification.passed_count} passed, {verification.failed_count} failed,{' '}
        {verification.not_observable_count} not observable. A check that could not
        be observed never counts as a pass.
      </p>
      {verification.summary ? (
        <p className="mt-1 text-sm text-slate-300">{verification.summary}</p>
      ) : null}
      {verification.checks && verification.checks.length > 0 ? (
        <ul className="mt-2 space-y-1 text-xs text-slate-300">
          {verification.checks.map((check, index) => (
            <li key={index}>
              <span className="text-slate-500">
                {String(check.kind ?? check.name ?? 'check')}:{' '}
              </span>
              {String(check.result ?? '')}
              {check.detail ? ` — ${String(check.detail)}` : ''}
            </li>
          ))}
        </ul>
      ) : null}
      {verification.limitations && verification.limitations.length > 0 ? (
        <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-slate-400">
          {verification.limitations.map((limitation) => (
            <li key={limitation}>{limitation}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
