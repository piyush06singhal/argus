import Link from 'next/link';

import { api, ApiError } from '@/lib/api';
import { loginUrl } from '@/lib/oidc';

import TokenForm from '../components/TokenForm';

export const metadata = { title: 'Connect' };
export const dynamic = 'force-dynamic';

/**
 * Where a new operator connects the console to the platform.
 *
 * The instructions are the real ones — the token comes from the API's first
 * boot (printed to the container log) or from `app.cli`, and both commands are
 * given verbatim. A page that says "contact your administrator" is not an
 * onboarding path.
 */
export default async function ConnectPage() {
  let current: Awaited<ReturnType<typeof api.whoami>> | null = null;
  let currentError: string | null = null;
  try {
    current = await api.whoami();
  } catch (err) {
    currentError =
      err instanceof ApiError
        ? err.message
        : 'Could not reach the ARGUS API.';
  }

  //: Whether SSO is configured is a *public* API fact, so this is asked even
  //: when the caller has no valid token yet — which is exactly the situation
  //: the sign-in button exists for. A deployment with SSO off simply renders
  //: nothing here.
  let sso: Awaited<ReturnType<typeof api.oidcConfig>> | null = null;
  try {
    sso = await api.oidcConfig();
  } catch {
    sso = null;
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">Connect to ARGUS</h1>
        <p className="mt-1 text-sm text-slate-400">
          ARGUS requires a bearer token on every API request. This browser holds
          the token you enter here; server-rendered pages receive the same
          cookie, so what you see is always what your token is allowed to see.
        </p>
      </div>

      <section className="rounded border border-slate-800 bg-slate-900/40 p-4">
        <h2 className="text-sm font-medium text-slate-200">Current session</h2>
        {current ? (
          <dl className="mt-2 space-y-1 text-sm text-slate-300">
            <div className="flex gap-2">
              <dt className="text-slate-500">Identity</dt>
              <dd>{current.name}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="text-slate-500">Role</dt>
              <dd>{current.role}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="text-slate-500">Scope</dt>
              <dd>
                {current.unrestricted
                  ? 'all projects'
                  : `${current.project_ids.length} granted project(s)`}
              </dd>
            </div>
            <div className="flex gap-2">
              <dt className="text-slate-500">Enforcement</dt>
              <dd>{current.auth_enforced ? 'enabled' : 'disabled (dev bypass)'}</dd>
            </div>
          </dl>
        ) : (
          <p className="mt-2 text-sm text-amber-300">
            Not connected: {currentError}
          </p>
        )}
      </section>

      {sso?.enabled ? (
        <section className="rounded border border-slate-800 bg-slate-900/40 p-4">
          <h2 className="text-sm font-medium text-slate-200">
            Sign in with {sso.provider_name}
          </h2>
          <p className="mt-1 text-sm text-slate-400">
            Your role and project access come from the identity provider, and
            they are re-read on every sign-in — a change there takes effect the
            next time you sign in.
            {sso.requires_verified_email
              ? ' Your email address must be verified by the provider.'
              : ''}
            {sso.allowed_email_domains.length > 0
              ? ` Only these domains may sign in: ${sso.allowed_email_domains.join(', ')}.`
              : ''}
          </p>
          {/* A full-page navigation, not a fetch: the API answers with a
              redirect to the provider, and the provider needs the browser. */}
          <a
            href={loginUrl(sso.login_path)}
            className="mt-3 inline-block rounded bg-argus-accent px-3 py-1.5 text-sm font-medium text-slate-950 hover:opacity-90"
          >
            Continue with {sso.provider_name}
          </a>
          <p className="mt-2 text-xs text-slate-500">
            Callback registered at the provider:{' '}
            <code className="font-mono">{sso.redirect_uri || 'not set'}</code>
          </p>
        </section>
      ) : null}

      <section className="rounded border border-slate-800 bg-slate-900/40 p-4">
        <h2 className="text-sm font-medium text-slate-200">Enter a token</h2>
        <div className="mt-3">
          <TokenForm />
        </div>
      </section>

      <section className="rounded border border-slate-800 bg-slate-900/40 p-4 text-sm text-slate-300">
        <h2 className="text-sm font-medium text-slate-200">
          Where do I get a token?
        </h2>
        <p className="mt-2">
          On first boot ARGUS generates a root admin token and prints it once to
          the API log:
        </p>
        <pre className="mt-2 overflow-x-auto rounded bg-slate-950 p-3 font-mono text-xs text-slate-300">
{`docker compose logs api | grep -A2 "ARGUS ADMIN TOKEN"`}
        </pre>
        <p className="mt-3">
          Lost it? Mint a fresh admin token from inside the API container (this
          writes an audit row and never echoes an existing secret):
        </p>
        <pre className="mt-2 overflow-x-auto rounded bg-slate-950 p-3 font-mono text-xs text-slate-300">
{`docker compose exec api python -m app.cli bootstrap-token`}
        </pre>
        <p className="mt-3">
          Create narrower credentials (role + project grants) from{' '}
          <Link href="/settings/tokens" className="text-argus-accent underline">
            Settings → Tokens
          </Link>
          . See <code className="font-mono">SECURITY.md</code> for the full
          token model.
        </p>
      </section>
    </div>
  );
}
