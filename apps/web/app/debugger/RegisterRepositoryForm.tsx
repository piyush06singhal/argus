'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError } from '@/lib/api-client';

/**
 * Register a repository for a project (§6).
 *
 * The backend validates by reading the repository — a bad path is rejected
 * with the provider's own reason, which this form shows verbatim rather than
 * paraphrasing into "invalid input".
 */
export default function RegisterRepositoryForm({
  projectId,
}: {
  projectId: string;
}) {
  const router = useRouter();
  const [url, setUrl] = useState('');
  const [provider, setProvider] = useState<'local' | 'git'>('local');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    const trimmed = url.trim();
    if (!trimmed || busy) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.registerRepository(projectId, {
        provider,
        repository_url: trimmed,
      });
      setUrl('');
      router.refresh();
    } catch (cause: unknown) {
      setError(
        cause instanceof ApiError ? cause.message : 'The repository was refused.'
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={provider}
          onChange={(event) =>
            setProvider(event.target.value as 'local' | 'git')
          }
          className="input w-auto"
          aria-label="Repository provider"
        >
          <option value="local">local path</option>
          <option value="git">git remote</option>
        </select>
        <input
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              submit();
            }
          }}
          placeholder={
            provider === 'local'
              ? '/path/to/checkout'
              : 'https://github.com/org/repo.git'
          }
          className="input min-w-[18rem] flex-1"
          aria-label="Repository location"
        />
        <button
          type="button"
          onClick={submit}
          disabled={busy || url.trim() === ''}
          className="rounded-md bg-argus-accent/20 px-3 py-1.5 text-sm font-medium text-argus-accent hover:bg-argus-accent/30 disabled:opacity-50"
        >
          {busy ? 'Checking…' : 'Register'}
        </button>
      </div>
      {error ? <p className="text-xs text-argus-error">{error}</p> : null}
    </div>
  );
}
