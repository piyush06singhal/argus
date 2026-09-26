'use client';

import Link from 'next/link';
import { useState } from 'react';

import { api, ApiError, type KnowledgeSearchAnswer } from '@/lib/api-client';

const SUGGESTIONS = [
  'Have we seen this before?',
  'Which remediation has worked for checkout errors?',
  'What usually precedes a latency spike?',
  'Which components have been chronically unreliable?',
];

/**
 * Grounded knowledge search (§46–§49, §90).
 *
 * The answer is rendered with its citations and its warnings, in that order, and
 * an answer with no evidence is rendered as exactly that. Nothing on this page
 * paraphrases the response into something more confident: if the backend says no
 * comparable case was found, that sentence is what the reader sees, because the
 * alternative is a plausible-sounding story assembled from nothing.
 */
export default function SearchPanel({ projectId }: { projectId: string }) {
  const [question, setQuestion] = useState('');
  const [answer, setAnswer] = useState<KnowledgeSearchAnswer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function ask(value: string) {
    if (!value.trim()) {
      return;
    }
    setBusy(true);
    setError(null);
    setAnswer(null);
    try {
      const response = await api.searchKnowledge({
        project_id: projectId,
        question: value,
      });
      setAnswer(response);
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
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          void ask(question);
        }}
      >
        <label className="flex flex-1 flex-col gap-1 text-xs text-slate-400">
          Question
          <input
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="what has ARGUS seen that resembles this"
            className="input"
          />
        </label>
        <button type="submit" className="btn" disabled={busy || !projectId}>
          {busy ? 'Searching…' : 'Search stored history'}
        </button>
      </form>

      <div className="flex flex-wrap gap-2">
        {SUGGESTIONS.map((suggestion) => (
          <button
            key={suggestion}
            type="button"
            className="badge bg-slate-800 text-slate-300 hover:text-argus-accent"
            onClick={() => {
              setQuestion(suggestion);
              void ask(suggestion);
            }}
          >
            {suggestion}
          </button>
        ))}
      </div>

      {error ? <p className="text-sm text-argus-error">Refused: {error}</p> : null}

      {answer ? (
        <div
          className={`rounded-md border p-4 ${
            answer.evidence_available
              ? 'border-slate-800 bg-slate-900/40'
              : 'border-argus-warning/40 bg-argus-warning/10'
          }`}
        >
          <p className="text-xs uppercase tracking-wider text-slate-500">
            {answer.intent.replace(/_/g, ' ').toLowerCase()}
          </p>
          <p className="mt-2 text-sm text-slate-200">{answer.answer}</p>

          {answer.citations.length > 0 ? (
            <div className="mt-3">
              <h3 className="text-xs uppercase tracking-wider text-slate-500">
                Cited evidence
              </h3>
              <ul className="mt-1 space-y-1 text-xs text-slate-400">
                {answer.citations.map((citation) => (
                  <li key={`${citation.type}-${citation.id}`}>
                    · <span className="font-mono">{citation.type}</span>{' '}
                    <span className="font-mono">{citation.id.slice(0, 8)}</span>
                    {citation.label ? ` — ${citation.label}` : ''}
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="mt-3 text-xs text-slate-500">
              No citation is attached. An answer without citations is an assertion,
              and is shown as one.
            </p>
          )}

          {answer.limitations.length > 0 ? (
            <div className="mt-3">
              <h3 className="text-xs uppercase tracking-wider text-slate-500">
                Limitations
              </h3>
              <ul className="mt-1 space-y-1 text-xs text-slate-400">
                {answer.limitations.map((limitation) => (
                  <li key={limitation}>· {limitation}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {answer.warnings.length > 0 ? (
            <div className="mt-3">
              <h3 className="text-xs uppercase tracking-wider text-argus-warning">
                Warnings
              </h3>
              <ul className="mt-1 space-y-1 text-xs text-argus-warning">
                {answer.warnings.map((warning) => (
                  <li key={warning}>· {warning}</li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : null}

      <p className="text-xs text-slate-500">
        Answers are assembled from stored knowledge, experiences and effectiveness
        records for this project only. ARGUS will not describe a case it cannot
        cite — see{' '}
        <Link href="/intelligence/patterns" className="text-argus-accent">
          the patterns behind them
        </Link>
        .
      </p>
    </div>
  );
}
