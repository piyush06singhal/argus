'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError } from '@/lib/api';

const DECISIONS = [
  {
    value: 'APPROVE',
    label: 'Approve',
    hint: 'The pattern may inform recommendations. The reviewer and reason are stored with the version.',
  },
  {
    value: 'REJECT',
    label: 'Reject',
    hint: 'The pattern stops influencing anything. It is kept, not deleted, so the decision remains auditable.',
  },
  {
    value: 'REQUEST_MORE_EVIDENCE',
    label: 'Request more evidence',
    hint: 'The pattern goes back to validation with a note about what is missing.',
  },
  {
    value: 'DEPRECATE',
    label: 'Deprecate',
    hint: 'Retire knowledge that new data no longer confirms.',
  },
] as const;

/**
 * A human review decision (§72).
 *
 * Three things this form refuses to hide:
 *
 * 1. **A reason is required.** An unexplained approval is indistinguishable from
 *    a rubber stamp when it is read a year later.
 * 2. **The lifecycle can refuse.** A decision the state machine forbids comes
 *    back as an error and is shown verbatim rather than being swallowed into a
 *    generic failure message.
 * 3. **The decision is not executed silently.** The resulting status is
 *    rendered, so "approved" is not mistaken for "active".
 */
export default function ReviewPanel({
  knowledgeId,
  projectId,
  currentStatus,
}: {
  knowledgeId: string;
  projectId: string;
  currentStatus: string;
}) {
  const router = useRouter();
  const [decision, setDecision] = useState<
    'APPROVE' | 'REJECT' | 'REQUEST_MORE_EVIDENCE' | 'DEPRECATE'
  >('APPROVE');
  const [reviewer, setReviewer] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  const selected = DECISIONS.find((item) => item.value === decision);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const updated = await api.reviewKnowledge(knowledgeId, projectId, {
        decision,
        reviewer,
        reason,
      });
      setResult(updated.knowledge.status);
      setReason('');
      router.refresh();
    } catch (caught) {
      //: The lifecycle's own message — "cannot move CANDIDATE to ACTIVE" is
      //: information; "something went wrong" is not.
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
    <form onSubmit={submit} className="space-y-3">
      <fieldset className="space-y-2">
        <legend className="text-xs uppercase tracking-wider text-slate-500">
          Decision
        </legend>
        {DECISIONS.map((item) => (
          <label key={item.value} className="flex items-start gap-2 text-sm">
            <input
              type="radio"
              name="decision"
              value={item.value}
              checked={decision === item.value}
              onChange={() => setDecision(item.value)}
              className="mt-1"
            />
            <span>
              <span className="text-slate-200">{item.label}</span>
              <span className="block text-xs text-slate-500">{item.hint}</span>
            </span>
          </label>
        ))}
      </fieldset>

      <label className="flex flex-col gap-1 text-xs text-slate-400">
        Reviewer
        <input
          required
          value={reviewer}
          onChange={(event) => setReviewer(event.target.value)}
          placeholder="who is making this call"
          className="input"
        />
      </label>

      <label className="flex flex-col gap-1 text-xs text-slate-400">
        Reason (required)
        <textarea
          required
          rows={3}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="what in the evidence supports this"
          className="input"
        />
      </label>

      {selected ? (
        <p className="text-xs text-slate-500">{selected.hint}</p>
      ) : null}

      <button type="submit" className="btn" disabled={busy || !reviewer || !reason}>
        {busy ? 'Recording…' : 'Record decision'}
      </button>

      <p className="text-xs text-slate-500">
        Current status: <span className="font-mono">{currentStatus}</span>
      </p>

      {result ? (
        <p className="text-sm text-argus-success">
          Recorded. The pattern is now <span className="font-mono">{result}</span>.
        </p>
      ) : null}
      {error ? (
        <p className="text-sm text-argus-error">Refused: {error}</p>
      ) : null}
    </form>
  );
}
