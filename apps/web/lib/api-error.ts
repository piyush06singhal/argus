/**
 * The API error type, browser-safe (hardening W7).
 *
 * Why it lives alone: `ApiError` is the one part of the API layer that *client*
 * components need on their own — every form and error boundary does
 * `cause instanceof ApiError` to decide whether to show the server's own reason
 * or a generic message. `lib/api.ts` also reaches a server-only module
 * (`next/headers`, through a dynamic import taken only when there is no
 * `window`), so importing the whole client from a page boundary pulls that
 * module into the browser bundle and fails the build.
 *
 * A module with no imports of its own is what lets a client component reason
 * about failures without dragging the request plumbing with it. `lib/api.ts`
 * re-exports this class, so existing imports keep working.
 */
export class ApiError extends Error {
  status: number;

  /**
   * The backend's stable machine-readable reason, when it supplies one.
   *
   * Two shapes exist across the API and both are handled by the client: a flat
   * `{"detail": "…", "error_code": "…"}` (the edge middlewares) and a
   * structured `{"detail": {"message": "…", "error_code": "…"}}` (routes that
   * need to attach facts to the refusal, such as the SSO callback). Without
   * this field the reason is only ever a human sentence, and a UI that wants to
   * explain a specific failure has to match on prose.
   */
  code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }

  /** 401 — no usable credential; the UI must ask for one. */
  get isUnauthenticated(): boolean {
    return this.status === 401;
  }

  /** 403 — authenticated, but not allowed to do this (role/scope). */
  get isForbidden(): boolean {
    return this.status === 403;
  }
}
