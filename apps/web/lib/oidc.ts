/**
 * Presentation helpers for single sign-on (hardening W2).
 *
 * Three rules, and each one exists because the alternative is a bug:
 *
 * 1. **The browser never sees the verifier, the secret or the ID token.** The
 *    page handles exactly two values — the `code` and the `state` the provider
 *    put in the URL — and posts them to the API.
 * 2. **A redirect target is only ever a same-origin path.** The console
 *    navigates after a login; an absolute URL arriving from a query string is
 *    a phishing pivot, so it is refused rather than sanitized.
 * 3. **A refusal is translated, never swallowed.** The API returns a stable
 *    `error_code`; the user gets a sentence that says what to do about it, and
 *    an unrecognised code still renders as itself instead of "unknown error".
 */

/** Build the URL that starts a login, optionally returning somewhere after. */
export function loginUrl(loginPath: string, returnTo?: string | null): string {
  const base = loginPath && loginPath.startsWith('/') ? loginPath : '/api/v1/auth/oidc/login';
  const target = isSafeReturnPath(returnTo) ? `?return_to=${encodeURIComponent(returnTo as string)}` : '';
  return `${base}${target}`;
}

/**
 * Is this a relative, same-origin path?
 *
 * `//host` is protocol-relative and therefore absolute; `/\host` is treated as
 * absolute by several browsers; a control character can split a header. None
 * of them are paths, so none of them are accepted.
 */
export function isSafeReturnPath(value: string | null | undefined): boolean {
  if (!value) return false;
  if (!value.startsWith('/')) return false;
  if (value.startsWith('//') || value.startsWith('/\\')) return false;
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f]/.test(value)) return false;
  return true;
}

export interface CallbackParams {
  code?: string;
  state?: string;
  /** The provider (or the API) refused before a code was issued. */
  error?: string;
}

/**
 * Read the callback query string.
 *
 * Returns whatever is present and lets the caller decide; a missing `code`
 * with an `error` is a refusal, and a missing `code` *without* an error is a
 * malformed link. Both are answered, neither is guessed at.
 */
export function parseCallbackParams(search: string): CallbackParams {
  const params = new URLSearchParams(search.startsWith('?') ? search : `?${search}`);
  const out: CallbackParams = {};
  const code = params.get('code');
  const state = params.get('state');
  const error = params.get('error');
  if (code) out.code = code;
  if (state) out.state = state;
  if (error) out.error = error;
  return out;
}

//: Reason codes are the backend's contract (`app.services.oidc`), so they are
//: listed here rather than matched by substring. A new code that arrives
//: without an explanation renders as the code itself, which is recoverable; a
//: substring match would silently start explaining the wrong thing.
const EXPLANATIONS: Record<string, string> = {
  oidc_disabled:
    'Single sign-on is not enabled on this ARGUS deployment. Use a token instead.',
  state_unknown:
    'This sign-in link is not one ARGUS issued. Start the sign-in again from this page.',
  state_already_used:
    'This sign-in link has already been used. Start again — a link works once.',
  state_expired: 'This sign-in link expired. Start again.',
  token_exchange_failed:
    'The identity provider refused the exchange, or ARGUS could not reach it. Check the provider configuration and the API log.',
  id_token_invalid:
    'The identity provider’s token failed verification. This is a configuration problem, not a password problem.',
  nonce_mismatch:
    'The identity provider returned a token for a different sign-in attempt. Nothing was granted.',
  email_not_verified:
    'Your email address is not verified at the identity provider. Ask an administrator to verify it.',
  email_domain_not_allowed:
    'Your email domain may not sign in to this deployment.',
  identity_disabled:
    'This identity has been disabled in ARGUS. An administrator can re-enable it.',
  claims_invalid:
    'The identity provider did not send a stable user identifier, so ARGUS cannot tell who you are.',
};

/** Turn a backend reason code into something a person can act on. */
export function describeOidcError(reason: string | null | undefined): string {
  if (!reason) {
    return 'Sign-in did not complete. Start again from the connect page.';
  }
  const explanation = EXPLANATIONS[reason];
  if (explanation) return explanation;
  //: Unknown codes are shown verbatim: an operator can grep for them, and a
  //: mysterious failure is far worse than an ugly one.
  return `Sign-in was refused (${reason}). See the API log for the audit entry.`;
}

/** A one-line description of a provisioned identity, for the admin table. */
export function describeIdentityScope(
  identity: { unrestricted?: boolean; role: string; project_ids: string[] }
): string {
  if (identity.role === 'ADMIN') return 'every project (ADMIN)';
  const count = identity.project_ids.length;
  if (count === 0) return 'no project grants';
  return `${count} granted project${count === 1 ? '' : 's'}`;
}
