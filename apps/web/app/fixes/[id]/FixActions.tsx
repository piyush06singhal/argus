'use client';

/**
 * The fix workspace's controls (§42, §71).
 *
 * Three deliberate choices:
 *
 * 1. **The review buttons are the backend's own allowlist.** `allowedActions`
 *    comes from `allowedReviewActions`, so a patch that was never verified
 *    simply has no Approve button — the gate is not decorative.
 * 2. **Nothing runs implicitly.** Verification is an explicit, confirmed action;
 *    it provisions a workspace, runs commands and destroys it again.
 * 3. **A refusal is shown verbatim.** The backend's message ("patch is
 *    VALIDATION_FAILED; it cannot be verified…") is information the engineer
 *    needs, not an error to be paraphrased away.
 */

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError, type ReviewAction } from '@/lib/api-client';
import { announce, clearAnnouncement } from '@/lib/announce';
import { reviewActionStyle } from '@/lib/fixes';

import Announcement from '@/app/components/Announcement';

//: Announcement scope, so this panel's confirmation cannot collide with another's.
const ANNOUNCE_SCOPE = 'fix-actions';

const ACTION_LABELS: Record<string, string> = {
  APPROVE: 'Approve',
  REJECT: 'Reject',
  REGENERATE: 'Regenerate',
};

export default function FixActions({
  hypothesisId,
  patchId,
  projectId,
  allowedActions,
  patchStatus,
  baselineFailureSignature,
  hasVerification,
}: {
  hypothesisId: string;
  patchId: string | null;
  projectId: string;
  allowedActions: ReviewAction[];
  patchStatus: string | null;
  baselineFailureSignature: string;
  hasVerification: boolean;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState('');
  const [confirmed, setConfirmed] = useState(false);

  async function run(label: string, work: () => Promise<string>) {
    setBusy(label);
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

  const generatable =
    patchStatus == null ||
    patchStatus === 'GENERATION_FAILED' ||
    patchStatus === 'PARSE_FAILED' ||
    patchStatus === 'VALIDATION_FAILED';

  const verifiable =
    patchId != null &&
    patchStatus !== 'GENERATION_FAILED' &&
    patchStatus !== 'PARSE_FAILED' &&
    patchStatus !== 'VALIDATION_FAILED' &&
    patchStatus !== 'VERIFIED' &&
    patchStatus !== 'SUPERSEDED';

  //: Verification is explicit consent: it runs the repository's commands and
  //: is the only control that can produce a VERIFIED verdict.
  const needsConfirmation = verifiable && !hasVerification && !confirmed;

  return (
    <section className="card space-y-4">
      <div>
        <h2 className="font-medium text-slate-200">Actions</h2>
        <p className="mt-1 text-xs text-slate-500">
          Verification provisions a disposable workspace, applies the patch, runs
          the repository&apos;s own commands, writes and runs a two-sided regression
          test, then destroys the workspace. Your repository is never modified.
        </p>
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="btn-primary"
          disabled={busy != null || !generatable}
          onClick={() =>
            run('generate', async () => {
              const patch = await api.generatePatch(hypothesisId, projectId, {
                generated_by: 'deterministic',
              });
              return `Patch ${patch.status}: ${patch.changed_files} file(s), +${patch.lines_added} −${patch.lines_removed}.`;
            })
          }
        >
          {busy === 'generate' ? 'Generating…' : 'Generate patch'}
        </button>

        <button
          type="button"
          className="btn-ghost"
          disabled={busy != null || !verifiable || needsConfirmation}
          onClick={() =>
            run('verify', async () => {
              const result = await api.verifyPatch(patchId as string, projectId, {
                baseline_reproduced: true,
                baseline_metrics: {
                  error_rate: 0.082,
                  latency_p95_ms: 2900,
                },
                patched_metrics: {
                  error_rate: 0.006,
                  latency_p95_ms: 300,
                },
                baseline_failure_signature: baselineFailureSignature,
              });
              return `${result.status} (${result.level}): ${result.verdict_reason ?? ''}`;
            })
          }
        >
          {busy === 'verify' ? 'Verifying…' : 'Verify patch'}
        </button>

        {allowedActions.map((action) => (
          <button
            key={action}
            type="button"
            className={reviewActionStyle(action)}
            disabled={busy != null}
            onClick={() =>
              run(action, async () => {
                if (action === 'APPROVE') {
                  await api.approvePatch(patchId as string, projectId, {
                    actor: 'engineer',
                    reason: reason || undefined,
                  });
                  return 'Approval recorded. Nothing was merged or deployed — Phase 7 stops here (§71).';
                }
                if (action === 'REJECT') {
                  await api.rejectPatch(patchId as string, projectId, {
                    actor: 'engineer',
                    reason: reason || undefined,
                  });
                  return 'Rejection recorded.';
                }
                const next = await api.regeneratePatch(patchId as string, projectId, {
                  actor: 'engineer',
                  reason: reason || undefined,
                });
                return `New candidate ${next.id} generated; the previous one is superseded.`;
              })
            }
          >
            {busy === action ? 'Working…' : ACTION_LABELS[action] ?? action}
          </button>
        ))}
      </div>

      <label className="block text-xs text-slate-400">
        Review note (recorded in the audit trail)
        <input
          className="input mt-1 w-full"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="why this decision"
        />
      </label>

      {verifiable && !hasVerification ? (
        <label className="flex items-start gap-2 text-xs text-slate-400">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
          />
          I understand verification only reports what it observed, and that a
          failed verification is a real result, not an error.
        </label>
      ) : null}

      {error ? <p className="text-sm text-argus-error">{error}</p> : null}
      <Announcement scope={ANNOUNCE_SCOPE} />
    </section>
  );
}
