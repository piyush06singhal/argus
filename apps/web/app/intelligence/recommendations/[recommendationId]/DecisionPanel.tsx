'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError } from '@/lib/api-client';
import { announce, clearAnnouncement } from '@/lib/announce';

import Announcement from '@/app/components/Announcement';

//: Announcement scope, so this panel's confirmation cannot collide with another's.
const ANNOUNCE_SCOPE = 'recommendation-decision';

const OUTCOMES = [
  { value: 'EFFECTIVE', label: 'Effective — the situation improved' },
  { value: 'INEFFECTIVE', label: 'Ineffective — it did not help' },
  { value: 'REGRESSION_CAUSING', label: 'Regression-causing — it made things worse' },
] as const;

/**
 * Decide on a recommendation, and separately record what happened (§43, §81).
 *
 * The two actions are deliberately distinct controls. §81 is explicit that
 * acceptance is not correctness, so a UI that folded them into one button would
 * be unable to express "we did this and it did not work" — the fact the learning
 * pipeline most needs.
 *
 * Neither control executes anything. Accepting records a human decision; the
 * recommendation remains advice.
 */
export default function DecisionPanel({
  recommendationId,
  projectId,
  status,
}: {
  recommendationId: string;
  projectId: string;
  status: string;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actor, setActor] = useState('');
  const [outcome, setOutcome] = useState<(typeof OUTCOMES)[number]['value']>(
    'EFFECTIVE'
  );
  const [outcomeNote, setOutcomeNote] = useState('');

  const open = status === 'OPEN';
  const decided = ['ACCEPTED', 'DISMISSED'].includes(status);

  async function run(action: () => Promise<unknown>, message: string) {
    setBusy(true);
    setError(null);
    clearAnnouncement(ANNOUNCE_SCOPE);
    try {
      await action();
      // Recorded outside React: `router.refresh()` re-renders this component's
      // server parent, and a message held in component state did not survive it.
      announce(ANNOUNCE_SCOPE, message);
      router.refresh();
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? `${caught.message} (${caught.status})`
          : String(caught)
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <h3 className="text-xs uppercase tracking-wider text-slate-500">
          Decide
        </h3>
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          Who is deciding
          <input
            value={actor}
            onChange={(event) => setActor(event.target.value)}
            placeholder="reviewer"
            className="input"
          />
        </label>
        <div className="flex gap-2">
          <button
            type="button"
            className="btn"
            disabled={busy || !open || !actor}
            onClick={() =>
              run(
                () =>
                  api.decideRecommendation(recommendationId, projectId, {
                    decision: 'ACCEPTED',
                    actor,
                  }),
                'Recorded as accepted.'
              )
            }
          >
            Accept
          </button>
          <button
            type="button"
            className="btn"
            disabled={busy || !open || !actor}
            onClick={() =>
              run(
                () =>
                  api.decideRecommendation(recommendationId, projectId, {
                    decision: 'DISMISSED',
                    actor,
                  }),
                'Recorded as dismissed.'
              )
            }
          >
            Dismiss
          </button>
        </div>
        {!open ? (
          <p className="text-xs text-slate-500">
            This recommendation is no longer open, so it cannot be decided again.
          </p>
        ) : null}
      </div>

      <div className="space-y-2 border-t border-slate-800 pt-4">
        <h3 className="text-xs uppercase tracking-wider text-slate-500">
          What actually happened
        </h3>
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          Outcome
          <select
            value={outcome}
            onChange={(event) =>
              setOutcome(event.target.value as (typeof OUTCOMES)[number]['value'])
            }
            className="input"
          >
            {OUTCOMES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          Note
          <textarea
            rows={2}
            value={outcomeNote}
            onChange={(event) => setOutcomeNote(event.target.value)}
            placeholder="what was observed afterwards"
            className="input"
          />
        </label>
        <button
          type="button"
          className="btn"
          disabled={busy || !decided || !actor}
          onClick={() =>
            run(
              () =>
                api.recordRecommendationOutcome(recommendationId, projectId, {
                  verdict: outcome,
                  recorded_by: actor,
                  detail: outcomeNote ? { note: outcomeNote } : null,
                }),
              'Outcome recorded. It will be learned from on the next run.'
            )
          }
        >
          Record outcome
        </button>
        {!decided ? (
          <p className="text-xs text-slate-500">
            Outcomes are recorded against a decision, so accept or dismiss first.
            An unmeasured acceptance is not evidence, and the pipeline does not
            treat it as such.
          </p>
        ) : !actor ? (
          <p className="text-xs text-slate-500">
            An outcome needs a recorder: who observed it is part of the record,
            the same way a review decision needs a reviewer.
          </p>
        ) : null}
      </div>

      <Announcement scope={ANNOUNCE_SCOPE} />
      {error ? <p className="text-sm text-argus-error">Refused: {error}</p> : null}
    </div>
  );
}
