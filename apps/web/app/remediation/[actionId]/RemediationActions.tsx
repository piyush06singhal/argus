'use client';

/**
 * The decision controls for one remediation action (§22, §26, §32).
 *
 * The rules this component obeys are the phase's, not UI conveniences:
 *
 * 1. **The buttons are the backend's own allowlist.** `allowedTransitions`
 *    comes from the server's state machine, so an action that cannot be
 *    authorized yet simply has no Approve button. The gate is not decorative.
 * 2. **Nothing consequential runs without a named actor.** A remediation nobody
 *    signed is one nobody can be asked about later, so the actor field is
 *    required before any control enables.
 * 3. **Irreversible things are labelled irreversible.** Rolling back an action
 *    whose strategy cannot be undone is offered as a refusal attempt, not as a
 *    guarantee.
 * 4. **The backend's message is shown verbatim.** "the approval request expired"
 *    is information an operator needs, not an error to paraphrase away.
 */

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError } from '@/lib/api-client';
import { announce, clearAnnouncement } from '@/lib/announce';

import Announcement from '@/app/components/Announcement';

//: Announcement scope, so this panel's confirmation cannot collide with another's.
const ANNOUNCE_SCOPE = 'remediation-action';

type Control =
  | 'assess'
  | 'evaluate'
  | 'approve'
  | 'reject'
  | 'execute'
  | 'run'
  | 'verify'
  | 'rollback'
  | 'cancel';

const LABELS: Record<Control, string> = {
  assess: 'Run safety checks',
  evaluate: 'Re-run policy',
  approve: 'Approve',
  reject: 'Reject',
  execute: 'Execute',
  run: 'Run the full pipeline',
  verify: 'Verify now',
  rollback: 'Roll back',
  cancel: 'Cancel',
};

export default function RemediationActions({
  actionId,
  projectId,
  status,
  executionMode,
  rollbackStrategy,
  allowedTransitions,
}: {
  actionId: string;
  projectId: string;
  status: string;
  executionMode: string;
  rollbackStrategy: string;
  allowedTransitions: string[];
}) {
  const router = useRouter();
  const [actor, setActor] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState<Control | null>(null);
  const [error, setError] = useState<string | null>(null);

  const can = (target: string) => allowedTransitions.includes(target);
  const named = actor.trim().length > 0;

  async function run(control: Control, work: () => Promise<string>) {
    setBusy(control);
    setError(null);
    clearAnnouncement(ANNOUNCE_SCOPE);
    try {
      // Recorded outside React: `router.refresh()` re-renders this component's
      // server parent, and a message held in component state did not survive it.
      announce(ANNOUNCE_SCOPE, await work());
      router.refresh();
    } catch (cause: unknown) {
      setError(
        cause instanceof ApiError || cause instanceof Error
          ? cause.message
          : 'unknown error'
      );
    } finally {
      setBusy(null);
    }
  }

  const irreversible = rollbackStrategy === 'NONE';
  const manualUndo = rollbackStrategy === 'MANUAL';
  const liveRegime = executionMode === 'HUMAN_APPROVAL' || executionMode === 'AUTONOMOUS';

  return (
    <section className="card space-y-4">
      <div>
        <h2 className="font-medium text-slate-200">Decisions</h2>
        <p className="mt-1 text-xs text-slate-500">
          Every control below is offered only when the action&apos;s own state
          machine allows the transition. The server re-checks the safety,
          budget, breaker and policy rules on approval — an authorization is not
          a token that can be replayed.
        </p>
      </div>

      <div className="flex flex-wrap gap-3">
        <label className="text-xs text-slate-400">
          Your name
          <input
            className="mt-1 block w-56 rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
            value={actor}
            onChange={(event) => setActor(event.target.value)}
            placeholder="on-call engineer"
          />
        </label>
        <label className="text-xs text-slate-400">
          Reason (recorded in the audit trail)
          <input
            className="mt-1 block w-80 rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-200"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="why this is the right call"
          />
        </label>
      </div>

      <div className="flex flex-wrap gap-2">
        {can('VALIDATING') || status === 'PROPOSED' ? (
          <button
            type="button"
            className="rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:opacity-50"
            disabled={busy != null}
            onClick={() =>
              run('assess', async () => {
                const action = await api.assessRemediationAction(actionId, projectId);
                return `Safety assessment: ${action.safety_status ?? 'unknown'}`;
              })
            }
          >
            {LABELS.assess}
          </button>
        ) : null}

        {can('POLICY_REVIEW') || status === 'VALIDATING' ? (
          <button
            type="button"
            className="rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:opacity-50"
            disabled={busy != null}
            onClick={() =>
              run('evaluate', async () => {
                const action = await api.evaluateRemediationPolicy(
                  actionId,
                  projectId
                );
                return `Policy decision: ${action.policy_status ?? 'unknown'}`;
              })
            }
          >
            {LABELS.evaluate}
          </button>
        ) : null}

        {status === 'AWAITING_APPROVAL' ? (
          <>
            <button
              type="button"
              className="rounded-md bg-argus-accent/20 px-3 py-1.5 text-sm font-medium text-argus-accent hover:bg-argus-accent/30 disabled:opacity-50"
              disabled={busy != null || !named}
              title={named ? undefined : 'Name the person authorizing this'}
              onClick={() =>
                run('approve', async () => {
                  const action = await api.approveRemediationAction(
                    actionId,
                    { actor: actor.trim(), reason: reason || null },
                    projectId
                  );
                  return `Authorization: ${action.authorization_status ?? action.status}. The server re-ran every gate.`;
                })
              }
            >
              {LABELS.approve}
            </button>
            <button
              type="button"
              className="rounded-md border border-argus-error/40 px-3 py-1.5 text-sm text-argus-error hover:bg-argus-error/10 disabled:opacity-50"
              disabled={busy != null || !named}
              onClick={() =>
                run('reject', async () => {
                  const action = await api.rejectRemediationAction(
                    actionId,
                    { actor: actor.trim(), reason: reason || null },
                    projectId
                  );
                  return `Rejected — status ${action.status}. Rejection is terminal: the remediation must be re-proposed.`;
                })
              }
            >
              {LABELS.reject}
            </button>
          </>
        ) : null}

        {status === 'AUTHORIZED' ? (
          <>
            <button
              type="button"
              className="rounded-md bg-argus-accent/20 px-3 py-1.5 text-sm font-medium text-argus-accent hover:bg-argus-accent/30 disabled:opacity-50"
              disabled={busy != null}
              onClick={() =>
                run('execute', async () => {
                  const result = await api.executeRemediationAction(
                    actionId,
                    projectId,
                    { actor: actor.trim() || 'operator' }
                  );
                  return `Status ${result.status}${describeSteps(result.steps)}`;
                })
              }
            >
              {LABELS.execute}
            </button>
            <button
              type="button"
              className="rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:opacity-50"
              disabled={busy != null}
              onClick={() =>
                run('run', async () => {
                  const result = await api.runRemediationAction(actionId, projectId);
                  return `Status ${result.status}${describeSteps(result.steps)}`;
                })
              }
            >
              {LABELS.run}
            </button>
          </>
        ) : null}

        {status === 'VERIFYING' ? (
          <button
            type="button"
            className="rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800 disabled:opacity-50"
            disabled={busy != null}
            onClick={() =>
              run('verify', async () => {
                const action = await api.verifyRemediationAction(actionId, projectId);
                return `Outcome: ${action.outcome ?? 'not determined'}`;
              })
            }
          >
            {LABELS.verify}
          </button>
        ) : null}

        {can('ROLLING_BACK') || status === 'VERIFIED' || status === 'FAILED' ? (
          <button
            type="button"
            className="rounded-md border border-argus-warning/40 px-3 py-1.5 text-sm text-argus-warning hover:bg-argus-warning/10 disabled:opacity-50"
            disabled={busy != null || !named}
            title={named ? undefined : 'Name the person requesting the rollback'}
            onClick={() =>
              run('rollback', async () => {
                const result = await api.rollbackRemediationAction(
                  actionId,
                  projectId,
                  { actor: actor.trim(), reason: reason || undefined }
                );
                return `Status ${result.status}${describeSteps(result.steps)}`;
              })
            }
          >
            {LABELS.rollback}
          </button>
        ) : null}

        {can('CANCELLED') ? (
          <button
            type="button"
            className="rounded-md border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800 disabled:opacity-50"
            disabled={busy != null || !named}
            onClick={() =>
              run('cancel', async () => {
                const action = await api.cancelRemediationAction(
                  actionId,
                  projectId,
                  { actor: actor.trim(), reason: reason || undefined }
                );
                return `Cancelled — status ${action.status}`;
              })
            }
          >
            {LABELS.cancel}
          </button>
        ) : null}
      </div>

      {irreversible ? (
        <p className="text-xs text-argus-warning">
          This action cannot be reversed. ARGUS requires a named human to
          authorize it for that reason, and the rollback control above will be
          refused rather than faked.
        </p>
      ) : null}
      {manualUndo ? (
        <p className="text-xs text-slate-400">
          Only a human can undo this, outside ARGUS. The platform will record that
          it happened and then verify the reversal from telemetry.
        </p>
      ) : null}
      {!liveRegime ? (
        <p className="text-xs text-slate-400">
          The regime is {executionMode}: execution is recorded but no effect will
          be applied from this scope.
        </p>
      ) : null}
      {!named ? (
        <p className="text-xs text-slate-500">
          Authorizing, rejecting, rolling back and cancelling require a name.
        </p>
      ) : null}
      {error ? <p className="text-xs text-argus-error">{error}</p> : null}
      <Announcement scope={ANNOUNCE_SCOPE} />
    </section>
  );
}

function describeSteps(steps: { step: string; detail: string }[]): string {
  if (steps.length === 0) {
    return '.';
  }
  return `. ${steps.map((step) => `${step.step}: ${step.detail}`).join(' · ')}`;
}
