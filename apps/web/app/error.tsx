'use client';

import Link from 'next/link';
import { useEffect } from 'react';

//: Imported from the browser-safe error module, not from `@/lib/api`: a page
//: boundary must not pull the API client (and its server-only token resolution)
//: into the browser bundle.
import { ApiError } from '@/lib/api-error';

import TokenForm from './components/TokenForm';

/**
 * Global error boundary (hardening W1, W7).
 *
 * Two failures dominate a fresh self-hosted install: the API is unreachable,
 * and the credential is missing or rejected. Both produce the same visible
 * symptom — a page that cannot render — so this boundary names the likely
 * cause and puts the fix (retry, connect) in the same place instead of showing
 * a stack trace and leaving the operator to guess.
 */
export default function AppError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Surface the real error in the browser console for operators debugging a
    // deployment; the UI below stays readable.
    console.error('[argus] page error', error);
  }, [error]);

  const unauthorised =
    (error instanceof ApiError && error.isUnauthenticated) ||
    /401|bearer|token|credential|authenticat/i.test(error.message);
  const forbidden = error instanceof ApiError && error.isForbidden;

  return (
    <div className="mx-auto max-w-2xl space-y-5">
      <div className="rounded border border-red-900 bg-red-950/30 p-4">
        <h1 className="text-lg font-semibold text-red-100">
          {unauthorised
            ? 'This page needs a valid ARGUS token'
            : forbidden
              ? 'Your token is not allowed to open this page'
              : 'Something went wrong rendering this page'}
        </h1>
        <p className="mt-2 text-sm text-red-200/90">{error.message}</p>
        {error.digest && (
          <p className="mt-1 font-mono text-xs text-red-300/70">
            digest {error.digest}
          </p>
        )}
        <div className="mt-4 flex flex-wrap gap-3">
          <button
            type="button"
            onClick={reset}
            className="rounded bg-argus-accent px-4 py-2 text-sm font-medium text-slate-950"
          >
            Try again
          </button>
          <Link
            href="/connect"
            className="rounded border border-slate-700 px-3 py-2 text-sm text-slate-200 hover:border-slate-500"
          >
            Open connection settings
          </Link>
        </div>
      </div>

      <div className="rounded border border-slate-800 bg-slate-900/40 p-4">
        <h2 className="text-sm font-medium text-slate-200">
          {unauthorised ? 'Connect this browser' : 'Still failing? Check your token'}
        </h2>
        <p className="mt-1 mb-3 text-xs text-slate-400">
          The token is sent with every request; a 401 means the backend did not
          accept it. Tokens are minted with{' '}
          <code className="font-mono">
            docker compose exec api python -m app.cli bootstrap-token
          </code>
          .
        </p>
        <TokenForm compact />
      </div>
    </div>
  );
}
