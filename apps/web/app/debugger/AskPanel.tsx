'use client';

import { useState } from 'react';

import {
  api,
  ApiError,
  type DebugAssistantAnswer,
  type DebugMessage,
} from '@/lib/api';
import { isResolvableReference } from '@/lib/debugger';

/**
 * Ask a grounded follow-up question (§35, §36).
 *
 * The answer is rendered with its audit, not just its text: rejected
 * citations are listed, a degraded reason is shown when the model was not
 * available, and the tool budget is visible so the bounds are facts the
 * engineer can see.
 */
export default function AskPanel({
  sessionId,
  projectId,
}: {
  sessionId: string;
  projectId: string;
}) {
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [answer, setAnswer] = useState<DebugAssistantAnswer | null>(null);

  const submit = async () => {
    const text = question.trim();
    if (text.length < 3 || busy) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await api.askDebugSession(
        sessionId,
        { question: text, asked_by: 'ui' },
        projectId
      );
      setAnswer(result);
      setQuestion('');
    } catch (cause: unknown) {
      setError(
        cause instanceof ApiError ? cause.message : 'The question was refused.'
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              submit();
            }
          }}
          placeholder="Ask about this incident — answered only from stored evidence"
          className="input min-w-[20rem] flex-1"
          aria-label="Follow-up question"
        />
        <button
          type="button"
          onClick={submit}
          disabled={busy || question.trim().length < 3}
          className="rounded-md bg-argus-accent/20 px-3 py-1.5 text-sm font-medium text-argus-accent hover:bg-argus-accent/30 disabled:opacity-50"
        >
          {busy ? 'Answering…' : 'Ask'}
        </button>
      </div>

      {answer ? (
        <div className="rounded-md border border-slate-800 bg-slate-900/60 p-4 text-sm">
          <p className="whitespace-pre-wrap text-slate-200">{answer.answer}</p>
          {answer.evidence.length > 0 ? (
            <p className="mt-2 flex flex-wrap gap-1 text-xs text-slate-400">
              Cited:
              {answer.evidence.map((ref) => (
                <span
                  key={ref}
                  className={
                    isResolvableReference(ref)
                      ? 'badge bg-argus-accent/15 text-argus-accent'
                      : 'badge bg-slate-800 text-slate-400'
                  }
                >
                  {ref}
                </span>
              ))}
            </p>
          ) : null}
          {answer.invalid_references.length > 0 ? (
            <p className="mt-2 text-xs text-argus-error">
              {answer.invalid_references.length} cited reference(s) could not be
              verified and were removed.
            </p>
          ) : null}
          {answer.missing_evidence.length > 0 ? (
            <p className="mt-2 text-xs text-slate-500">
              Missing evidence: {answer.missing_evidence.join(' · ')}
            </p>
          ) : null}
          {answer.degraded_reason ? (
            <p className="mt-2 text-xs text-argus-warning">
              Degraded: {answer.degraded_reason}
            </p>
          ) : null}
          <p className="mt-2 text-xs text-slate-500">
            Confidence: {answer.confidence} · Tool budget:{' '}
            {String(answer.budget.calls_used ?? 0)}/
            {String(answer.budget.max_calls ?? '?')}
          </p>
        </div>
      ) : null}
      {error ? <p className="text-xs text-argus-error">{error}</p> : null}
    </div>
  );
}

/** Kept for the message renderer shared with the workspace page. */
export function messageTone(message: DebugMessage): string {
  if (message.role === 'ENGINEER') {
    return 'border-slate-700 bg-slate-800/40';
  }
  if (message.role === 'SYSTEM') {
    return 'border-slate-800 bg-slate-900/60 text-slate-400';
  }
  return 'border-argus-accent/30 bg-argus-accent/5';
}
