import { api, ApiError, formatDate } from '@/lib/api';
import { describeIdentityScope } from '@/lib/oidc';

import IdentityControls from './IdentityControls';

export const metadata = { title: 'Identities' };
export const dynamic = 'force-dynamic';

/**
 * People provisioned by the identity provider.
 *
 * This is the access surface, not a user directory: it shows who can currently
 * reach this deployment and as what, and it is where access is actually ended.
 * Disabling somebody here revokes every session they hold — the operation an
 * administrator needs when a person leaves, and the one that a deactivation at
 * the provider alone does not perform.
 *
 * ADMIN-only on the backend; a non-admin caller sees the refusal verbatim
 * rather than an empty table, because "no identities" and "you may not look"
 * are different facts.
 */
export default async function IdentitiesPage() {
  let data: Awaited<ReturnType<typeof api.listOidcIdentities>> | null = null;
  let error: string | null = null;
  try {
    data = await api.listOidcIdentities();
  } catch (err) {
    error = err instanceof ApiError ? err.message : 'Could not reach the ARGUS API.';
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">
          Single sign-on identities
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          A person appears here after their first successful sign-in. Role and
          project grants are re-read from the provider&apos;s claims on every
          login, so this table is a record of the last login rather than a place
          to configure access — change the claim at the provider and the next
          sign-in picks it up.
        </p>
      </div>

      {error ? (
        <p className="rounded border border-amber-900/60 bg-amber-950/30 p-3 text-sm text-amber-200">
          {error}
        </p>
      ) : null}

      {data && data.total === 0 ? (
        <p className="rounded border border-slate-800 bg-slate-900/40 p-4 text-sm text-slate-400">
          Nobody has signed in with an identity provider yet. If SSO is
          configured, the first login creates the first row here.
        </p>
      ) : null}

      {data && data.total > 0 ? (
        <div className="overflow-x-auto rounded border border-slate-800">
          <table className="w-full text-sm">
            <thead className="bg-slate-900/60 text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-3 py-2">Person</th>
                <th className="px-3 py-2">Role</th>
                <th className="px-3 py-2">Last sign-in</th>
                <th className="px-3 py-2">Logins</th>
                <th className="px-3 py-2 text-right">Access</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {data.items.map((identity) => (
                <tr
                  key={identity.id}
                  className={identity.disabled ? 'opacity-60' : undefined}
                >
                  <td className="px-3 py-2">
                    <div className="text-slate-200">
                      {identity.display_name || identity.email || identity.subject}
                    </div>
                    <div className="text-xs text-slate-500">
                      {identity.email || 'no email claim'} ·{' '}
                      <span className="font-mono">{identity.subject}</span>
                      {identity.email_verified ? '' : ' · email unverified'}
                      {identity.disabled
                        ? ` · disabled${
                            identity.disabled_reason
                              ? ` (${identity.disabled_reason})`
                              : ''
                          }`
                        : ''}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-slate-300">{identity.role}</td>
                  <td className="px-3 py-2 text-slate-400">
                    {formatDate(identity.last_login_at)}
                    {identity.last_login_ip ? (
                      <div className="text-xs text-slate-500">
                        from {identity.last_login_ip}
                      </div>
                    ) : null}
                  </td>
                  <td className="px-3 py-2 text-slate-400">
                    {identity.login_count}
                    {identity.active_sessions > 0 ? (
                      <div className="text-xs text-slate-500">
                        {identity.active_sessions} live
                      </div>
                    ) : null}
                  </td>
                  <td className="px-3 py-2">
                    <IdentityControls
                      identityId={identity.id}
                      disabled={identity.disabled}
                      scope={describeIdentityScope({
                        role: identity.role,
                        project_ids: identity.project_ids,
                      })}
                      activeSessions={identity.active_sessions}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      <section className="rounded border border-slate-800 bg-slate-900/40 p-4 text-sm text-slate-400">
        <h2 className="text-sm font-medium text-slate-200">What is enforced</h2>
        <ul className="mt-2 list-disc space-y-1 pl-5">
          <li>
            ID tokens are verified against the provider&apos;s published keys;
            unsigned tokens and tokens signed with a shared secret are refused.
          </li>
          <li>
            The provider&apos;s own issuer must match the configured one, so a
            redirected discovery document cannot move the trust anchor.
          </li>
          <li>
            Role and project grants are derived from claims and filtered against
            projects that exist — a claim naming another tenant grants nothing.
          </li>
          <li>
            Every login attempt, successful or refused, is written to the
            authentication audit trail.
          </li>
        </ul>
      </section>
    </div>
  );
}
