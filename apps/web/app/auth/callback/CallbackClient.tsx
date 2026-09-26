'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';

import { api, ApiError } from '@/lib/api-client';
import { writeTokenCookie } from '@/lib/argus-auth';
import {
  describeOidcError,
  isSafeReturnPath,
  parseCallbackParams,
} from '@/lib/oidc';

/**
 * Finish a single sign-on login.
 *
 * The provider sends the browser here with `code` and `state` in the query
 * string. This component posts both to the API in one request and stores the
 * session secret that comes back — then gets out of the way. Two properties
 * matter and both are visible in the code below:
 *
 * - **The exchange happens once.** React 18 runs effects twice in development
 *   and a user can reload the page; the code is single-use at the provider, so
 *   a second attempt would fail with a confusing error. A ref guards the
 *   request, and a completed login navigates away instead of sitting here.
 * - **Nothing is retried.** If the exchange fails, retrying the same code
 *   cannot succeed — the state has been consumed. The page says what happened
 *   and offers a fresh start.
 */
export default function CallbackClient({ search }: { search: string }) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<'working' | 'done' | 'failed'>('working');

  const complete = useCallback(
    async (code: string, state: string) => {
      try {
        const session = await api.oidcCallback({ code, state });
        writeTokenCookie(session.token);
        setStatus('done');
        const target = isSafeReturnPath(session.redirect_to)
          ? session.redirect_to
          : '/';
        router.replace(target);
      } catch (err) {
        setStatus('failed');
        if (err instanceof ApiError) {
          //: The API carries a stable reason code on every refusal; it maps to
          //: an explanation. The message is the fallback, never a raw dump.
          setError(err.code ? describeOidcError(err.code) : err.message);
          return;
        }
        setError(describeOidcError(null));
      }
    },
    [router]
  );

  useEffect(() => {
    const params = parseCallbackParams(search);
    if (params.error) {
      setStatus('failed');
      setError(describeOidcError(params.error));
      return;
    }
    if (!params.code || !params.state) {
      setStatus('failed');
      setError(
        'This page completes a sign-in, and the link that reached it carried no authorization code.'
      );
      return;
    }
    void complete(params.code, params.state);
    //: `search` is the whole input; re-running on anything else would re-spend
    //: a single-use code.
  }, [search, complete]);

  if (status === 'failed') {
    return (
      <div className="mx-auto max-w-xl space-y-4">
        <h1 className="text-xl font-semibold text-slate-100">
          Sign-in did not complete
        </h1>
        <p className="rounded border border-amber-900/60 bg-amber-950/30 p-3 text-sm text-amber-200">
          {error}
        </p>
        <div className="flex gap-3 text-sm">
          <Link
            href="/connect"
            className="rounded border border-slate-700 px-3 py-1.5 text-slate-200 hover:bg-slate-800"
          >
            Try again
          </Link>
        </div>
        <p className="text-xs text-slate-500">
          Every attempt — successful or refused — is written to the API&apos;s
          authentication audit trail, so an administrator can see what happened
          without asking you to reproduce it.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-xl space-y-3">
      <h1 className="text-xl font-semibold text-slate-100">
        Signing you in…
      </h1>
      <p className="text-sm text-slate-400">
        Verifying the identity provider&apos;s response and opening a session.
      </p>
    </div>
  );
}
