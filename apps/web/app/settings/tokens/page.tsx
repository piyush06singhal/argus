import { api, ApiError, type TokenSummary } from '@/lib/api';

import TokenAdmin from './TokenAdmin';

export const metadata = { title: 'Tokens' };
export const dynamic = 'force-dynamic';

/**
 * Token administration (hardening W1).
 *
 * Role-aware on purpose: a non-ADMIN caller is told exactly why this screen is
 * unavailable (their role) rather than being shown a form whose submissions
 * would 403. The token list is fetched with the caller's own credential, so the
 * page can never display credentials it should not see.
 */
export default async function TokensPage() {
  let me: Awaited<ReturnType<typeof api.whoami>> | null = null;
  let tokens: TokenSummary[] = [];
  let failure: string | null = null;

  try {
    me = await api.whoami();
    if (me.role === 'ADMIN') {
      tokens = (await api.listTokens()).items;
    }
  } catch (err) {
    failure =
      err instanceof ApiError
        ? `${err.message} (HTTP ${err.status})`
        : 'The ARGUS API could not be reached.';
  }

  if (!me) {
    return (
      <div className="card">
        <h1 className="font-medium text-slate-200">Tokens</h1>
        <p className="mt-2 text-sm text-amber-300">
          Could not identify this session: {failure}
        </p>
      </div>
    );
  }

  if (me.role !== 'ADMIN') {
    return (
      <div className="space-y-4">
        <h1 className="text-xl font-semibold text-slate-100">Tokens</h1>
        <div className="card">
          <p className="text-sm text-slate-300">
            Token management requires the <strong>ADMIN</strong> role. This
            session is <strong>{me.role}</strong> ({me.name}), so the list is not
            fetched and no create form is offered — the backend would refuse
            both, and a control that cannot succeed should not be rendered.
          </p>
          <p className="mt-2 text-sm text-slate-400">
            Ask an administrator to mint a credential for you, or to raise this
            token&apos;s role in the database.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">Tokens</h1>
        <p className="mt-1 text-sm text-slate-400">
          Credentials for people, collectors and CI. Roles decide what a caller
          may do; project grants decide what it may see. Every mint and revoke
          writes an audit row.
        </p>
      </div>
      <TokenAdmin tokens={tokens} />
    </div>
  );
}
