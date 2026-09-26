'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { api, ApiError, type TokenRole, type TokenSummary } from '@/lib/api-client';

import ConfirmButton from '../../components/ConfirmButton';

/**
 * Token administration (hardening W1).
 *
 * Two rules shape this screen:
 *
 * 1. **A secret is shown exactly once.** The created token appears in a
 *    one-time panel with a copy button; once dismissed it can never be
 *    retrieved, because only its hash is stored. Saying that plainly is kinder
 *    than letting an operator assume a "show" button exists somewhere.
 * 2. **Roles are explained where they are chosen.** A project-scoped VIEWER
 *    and a root ADMIN are both one click away, so the consequence of each
 *    choice is written next to it rather than in a doc nobody opens.
 */
const ROLES: Array<{ value: TokenRole; label: string; blurb: string }> = [
  {
    value: 'VIEWER',
    label: 'VIEWER',
    blurb: 'read-only; may see the projects granted to it',
  },
  {
    value: 'OPERATOR',
    label: 'OPERATOR',
    blurb: 'may ingest, run reproduction and fix workflows, approve remediation',
  },
  {
    value: 'ADMIN',
    label: 'ADMIN',
    blurb: 'everything, including policy changes and token management',
  },
];

export default function TokenAdmin({ tokens }: { tokens: TokenSummary[] }) {
  const router = useRouter();
  const [name, setName] = useState('');
  const [role, setRole] = useState<TokenRole>('OPERATOR');
  const [expiresInDays, setExpiresInDays] = useState('');
  const [projectIds, setProjectIds] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<{ token: string; name: string } | null>(
    null
  );

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const grants = projectIds
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean);
      const response = await api.createToken({
        name: name.trim(),
        role,
        expires_in_days: expiresInDays ? Number(expiresInDays) : undefined,
        project_ids: role === 'ADMIN' ? [] : grants,
        description: 'created from the web console',
      });
      setCreated({ token: response.token, name: response.name });
      setName('');
      setProjectIds('');
      setExpiresInDays('');
      router.refresh();
    } catch (cause: unknown) {
      setError(
        cause instanceof ApiError
          ? cause.message
          : 'The token could not be created.'
      );
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (tokenId: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.revokeToken(tokenId);
      router.refresh();
    } catch (cause: unknown) {
      setError(
        cause instanceof ApiError ? cause.message : 'Revocation failed.'
      );
    } finally {
      setBusy(false);
    }
  };

  const selectedRole = ROLES.find((r) => r.value === role)!;

  return (
    <div className="space-y-6">
      {created && (
        <section
          className="card border-emerald-800 bg-emerald-950/30"
          data-testid="token-created"
        >
          <h2 className="font-medium text-emerald-100">
            Token &ldquo;{created.name}&rdquo; created
          </h2>
          <p className="mt-1 text-sm text-emerald-200/90">
            Copy it now. ARGUS stores only a hash — this value cannot be
            displayed again, by anyone, including an administrator.
          </p>
          <pre className="mt-3 overflow-x-auto rounded bg-slate-950 p-3 font-mono text-xs text-slate-200">
            {created.token}
          </pre>
          <div className="mt-3 flex gap-3">
            <button
              type="button"
              onClick={() => navigator.clipboard?.writeText(created.token)}
              className="rounded border border-emerald-800 px-3 py-1.5 text-xs text-emerald-100"
            >
              Copy token
            </button>
            <button
              type="button"
              onClick={() => setCreated(null)}
              className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300"
            >
              I have stored it
            </button>
          </div>
        </section>
      )}

      <section className="card">
        <h2 className="font-medium text-slate-200">Create a token</h2>
        <form onSubmit={create} className="mt-3 grid gap-3 sm:grid-cols-2">
          <label className="text-sm">
            <span className="mb-1 block text-xs uppercase tracking-wide text-slate-400">
              Name
            </span>
            <input
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="ci-pipeline"
              className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            />
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-xs uppercase tracking-wide text-slate-400">
              Role
            </span>
            <select
              value={role}
              onChange={(e) => setRole(e.target.value as TokenRole)}
              className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            >
              {ROLES.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-xs uppercase tracking-wide text-slate-400">
              Expires in days (optional)
            </span>
            <input
              type="number"
              min={1}
              max={3650}
              value={expiresInDays}
              onChange={(e) => setExpiresInDays(e.target.value)}
              placeholder="no expiry"
              className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            />
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-xs uppercase tracking-wide text-slate-400">
              Project grants (comma-separated ids{role === 'ADMIN' ? ', ignored for ADMIN' : ''})
            </span>
            <input
              value={projectIds}
              onChange={(e) => setProjectIds(e.target.value)}
              placeholder="00000000-0000-0000-0000-000000000000"
              className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-xs text-slate-100"
            />
          </label>
          <p className="text-xs text-slate-400 sm:col-span-2">
            {selectedRole.label}: {selectedRole.blurb}.
            {role !== 'ADMIN'
              ? ' Without a project grant the token can read nothing — the same 404 as a project that does not exist.'
              : ' Grants are not needed: ADMIN passes every project check.'}
          </p>
          <div className="sm:col-span-2">
            <button
              type="submit"
              disabled={busy}
              className="rounded bg-argus-accent px-4 py-2 text-sm font-medium text-slate-950 disabled:opacity-50"
            >
              {busy ? 'Working…' : 'Create token'}
            </button>
          </div>
        </form>
        {error && (
          <p role="alert" className="mt-3 text-sm text-red-300">
            {error}
          </p>
        )}
      </section>

      <section className="card">
        <h2 className="font-medium text-slate-200">
          Tokens ({tokens.length})
        </h2>
        {tokens.length === 0 ? (
          <p className="mt-2 text-sm text-slate-400">No tokens yet.</p>
        ) : (
          <table className="mt-3 w-full text-left text-sm">
            <thead className="text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="py-1">Name</th>
                <th className="py-1">Role</th>
                <th className="py-1">Status</th>
                <th className="py-1">Scope</th>
                <th className="py-1">Last used</th>
                <th className="py-1" />
              </tr>
            </thead>
            <tbody className="text-slate-300">
              {tokens.map((token) => (
                <tr key={token.id} className="border-t border-slate-800">
                  <td className="py-2">{token.name}</td>
                  <td className="py-2">{token.role}</td>
                  <td className="py-2">
                    {token.status}
                    {token.expires_at ? ` · expires ${token.expires_at.slice(0, 10)}` : ''}
                  </td>
                  <td className="py-2 text-xs text-slate-400">
                    {token.role === 'ADMIN'
                      ? 'all projects'
                      : token.project_ids.length === 0
                        ? 'no project granted'
                        : `${token.project_ids.length} project(s)`}
                  </td>
                  <td className="py-2 text-xs text-slate-400">
                    {token.last_used_at
                      ? token.last_used_at.slice(0, 19).replace('T', ' ')
                      : 'never'}
                  </td>
                  <td className="py-2 text-right">
                    {token.status === 'ACTIVE' ? (
                      <ConfirmButton
                        kind="revoke_token"
                        subject={token.name}
                        disabled={busy}
                        onConfirm={() => revoke(token.id)}
                        className="rounded border border-red-900 px-3 py-1 text-xs text-red-200 disabled:opacity-50"
                        testId={`revoke-${token.id}`}
                      />
                    ) : (
                      <span className="text-xs text-slate-600">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
