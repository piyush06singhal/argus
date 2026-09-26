'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError } from '@/lib/api-client';

/**
 * Index a repository at a revision (§8).
 *
 * Leaving the revision blank means "the deployment-derived revision" — what
 * the backend prefers for incident work. The run notes are kept and shown
 * because a partial index is a fact the engineer must see, not a footnote.
 */
export default function IndexRepositoryButton({
  projectId,
  repositoryId,
}: {
  projectId: string;
  repositoryId: string;
}) {
  const router = useRouter();
  const [reference, setReference] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState<string[]>([]);

  const run = async () => {
    setBusy(true);
    setError(null);
    setNotes([]);
    try {
      const result = await api.indexRepository(projectId, repositoryId, {
        reference: reference.trim() || undefined,
      });
      setNotes(result.notes);
      router.refresh();
    } catch (cause: unknown) {
      setError(
        cause instanceof ApiError ? cause.message : 'Indexing failed.'
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={reference}
          onChange={(event) => setReference(event.target.value)}
          placeholder="revision (blank = deployment-derived)"
          className="input w-64"
          aria-label="Revision to index"
        />
        <button
          type="button"
          onClick={run}
          disabled={busy}
          className="rounded-md bg-argus-accent/20 px-3 py-1.5 text-sm font-medium text-argus-accent hover:bg-argus-accent/30 disabled:opacity-50"
        >
          {busy ? 'Indexing…' : 'Index'}
        </button>
      </div>
      {notes.length > 0 ? (
        <ul className="text-xs text-slate-400">
          {notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : null}
      {error ? <p className="text-xs text-argus-error">{error}</p> : null}
    </div>
  );
}
