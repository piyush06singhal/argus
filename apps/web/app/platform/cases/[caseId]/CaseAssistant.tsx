'use client';

import { useState } from 'react';

import { api, ApiError, type AssistantAnswerResponse } from '@/lib/api';
import { searchKindLabel } from '@/lib/platform';

/**
 * The AI case assistant (§27, §28, §80).
 *
 * The assistant is grounded: every answer arrives with the stored rows it cited
 * and an explicit list of unknowns. This component renders all three — the
 * answer, its citations and its unknowns — because an answer shown without its
 * evidence is a confident sentence, not a finding. The confidence is displayed
 * as the reason the backend gave, never as a bare percentage.
 */
export default function CaseAssistant({
  caseId,
  projectId,
}: {
  caseId: string;
  projectId: string;
}) {
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [answer, setAnswer] = useState<AssistantAnswerResponse | null>(null);

  const ask = async () => {
    if (!question.trim()) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const response = await api.platformCaseAsk({
        caseId,
        projectId,
        question: question.trim(),
        includeEvidence: false,
      });
      setAnswer(response);
    } catch (cause: unknown) {
      setError(
        cause instanceof ApiError
          ? cause.message
          : 'The assistant could not answer.'
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="font-medium text-slate-200">Ask about this case</h2>
        <p className="text-xs text-slate-500">
          Answers cite stored rows and say when they do not know (§27, §28)
        </p>
      </div>

      <div className="flex flex-wrap gap-2">
        <input
          className="input flex-1"
          placeholder="e.g. what changed before the incident?"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              void ask();
            }
          }}
        />
        <button
          type="button"
          onClick={() => void ask()}
          disabled={busy || !question.trim()}
          className="rounded-md bg-argus-accent/20 px-3 py-1.5 text-sm font-medium text-argus-accent hover:bg-argus-accent/30 disabled:opacity-50"
        >
          {busy ? 'Asking…' : 'Ask'}
        </button>
      </div>

      {error ? <p className="mt-3 text-xs text-argus-error">{error}</p> : null}

      {answer ? (
        <div className="mt-4 space-y-3 border-t border-slate-800 pt-4">
          <p className="text-sm text-slate-200">{answer.answer}</p>
          <p className="text-xs text-slate-500">
            Confidence: {answer.confidence_reason}
            {answer.narrator ? ` · narrator: ${answer.narrator}` : ''}
          </p>

          <div>
            <h3 className="mb-1 text-xs uppercase tracking-wider text-slate-500">
              Citations
            </h3>
            {answer.citations.length === 0 ? (
              <p className="text-sm text-slate-400">
                No stored row was cited — treat this answer as unverified.
              </p>
            ) : (
              <ul className="space-y-1 text-sm text-slate-300">
                {answer.citations.map((citation) => (
                  <li key={`${citation.kind}:${citation.row_id}`}>
                    <span className="badge bg-slate-800 text-slate-300">
                      {searchKindLabel(citation.kind)}
                    </span>{' '}
                    {citation.label}
                    {citation.detail ? (
                      <span className="ml-2 text-xs text-slate-500">
                        {citation.detail}
                      </span>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </div>

          {answer.unknowns.length > 0 ? (
            <div>
              <h3 className="mb-1 text-xs uppercase tracking-wider text-slate-500">
                Unknowns
              </h3>
              <ul className="space-y-1 text-xs text-slate-500">
                {answer.unknowns.map((item) => (
                  <li key={item}>· {item}</li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
