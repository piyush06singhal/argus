/**
 * ARGUS token handling for the web app (hardening W1).
 *
 * The backend requires a bearer token on every API route. That token has to
 * reach the backend from **two** kinds of caller, and they need different
 * plumbing:
 *
 * - **Browser (client components)** — read the token cookie stored by the
 *   token-entry screen and attach it as `Authorization: Bearer …`.
 * - **Server (server components)** — read the *same* cookie off the incoming
 *   page request (`next/headers`) and attach the same header. This is the
 *   point of using a cookie instead of `localStorage`: the browser sends it
 *   to the Next.js server on every page request, so server-rendered data is
 *   scoped to the same token the user typed, never to a shared service
 *   credential that would silently widen what they can see.
 *
 * The cookie is deliberately **not** httpOnly: the client must be able to
 * attach the token as a header (the backend does not read cookies), and a
 * self-hosted deployment's own UI is the only place it is exposed. It is
 * `SameSite=Lax` and path-scoped to the app.
 */

export const TOKEN_COOKIE = 'argus_token';

/** The token the caller entered, or `null` when none is stored. */
export function readTokenCookie(): string | null {
  if (typeof document === 'undefined') return null;
  const match = document.cookie.match(
    new RegExp(`(?:^|;\\s*)${TOKEN_COOKIE}=([^;]*)`)
  );
  if (!match) return null;
  const raw = decodeURIComponent(match[1]);
  return raw ? raw : null;
}

/**
 * Store the token for subsequent requests.
 *
 * `Secure` is set when the page is served over HTTPS so the cookie is never
 * sent in the clear on a real deployment, while localhost development over
 * plain HTTP keeps working.
 */
export function writeTokenCookie(raw: string): void {
  if (typeof document === 'undefined') return;
  const secure = window.location.protocol === 'https:' ? '; Secure' : '';
  const maxAge = 60 * 60 * 24 * 30; // 30 days — a self-hosted operator's own token
  document.cookie = `${TOKEN_COOKIE}=${encodeURIComponent(
    raw
  )}; Path=/; SameSite=Lax; Max-Age=${maxAge}${secure}`;
}

/** Forget the token (logout / rejected credential). */
export function clearTokenCookie(): void {
  if (typeof document === 'undefined') return;
  document.cookie = `${TOKEN_COOKIE}=; Path=/; SameSite=Lax; Max-Age=0`;
}

/**
 * Is the token shape plausible? Used for immediate client-side feedback
 * (e.g. warning that an API key was pasted instead of a token). The backend
 * remains the only authority — this never gates a request by itself.
 */
export function looksLikeArgusToken(raw: string): boolean {
  const trimmed = raw.trim();
  return trimmed.startsWith('argus_') && trimmed.length >= 20;
}

/**
 * A one-line human description of the credential's kind, for the UI.
 *
 * Ingest tokens are shown as such so an operator who pastes one into the UI
 * gets told why it cannot open the console, instead of a bare 401.
 */
export function describeToken(raw: string): string {
  const trimmed = raw.trim();
  if (trimmed.startsWith('argus_ing_')) return 'ingestion token (collectors only)';
  if (trimmed.startsWith('argus_')) return 'API token';
  return 'unrecognised credential';
}
