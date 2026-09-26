/**
 * Server-side token resolution (hardening W1).
 *
 * Separated from `api-client.ts` on purpose: this module imports
 * `next/headers`, which only exists in a server context, so importing it from
 * a client component fails the build. Server modules reach it through `api.ts`
 * (which registers the resolver); browser-boundary components must import
 * `api-client.ts` instead, which never touches this file.
 *
 * Returning the *caller's* token (rather than a shared service credential) is
 * the security property that matters: a server-rendered page shows a user
 * exactly what their own token may see, so a project-scoped VIEWER never gets
 * an admin's view of the platform just because the page was rendered on the
 * server.
 */

import { cookies } from 'next/headers';

import { TOKEN_COOKIE } from './argus-auth';

/** The token presented on the incoming request, or `null`. */
export async function getServerToken(): Promise<string | null> {
  const store = cookies();
  const value = store.get(TOKEN_COOKIE)?.value;
  return value && value.length > 0 ? value : null;
}
