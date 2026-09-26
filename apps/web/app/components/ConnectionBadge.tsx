import Link from 'next/link';

import { api } from '@/lib/api';

/**
 * Header indicator of the credential in force (hardening W1).
 *
 * Shows the *effective* role rather than a green "connected" dot, because the
 * difference between an ADMIN token and a project-scoped VIEWER is the single
 * most common reason a screen looks emptier than expected. Failing to resolve
 * the identity is itself information: it links to the connect screen instead
 * of pretending the session is fine.
 */
export default async function ConnectionBadge() {
  let label: string;
  let scope: string;
  let connected = false;
  try {
    const me = await api.whoami();
    connected = true;
    label = `${me.name} · ${me.role.toLowerCase()}`;
    scope = me.unrestricted
      ? 'all projects'
      : `${me.project_ids.length} project(s)`;
  } catch {
    label = 'not connected';
    scope = 'paste a token';
  }

  return (
    <Link
      href="/connect"
      title={
        connected
          ? `Authenticated as ${label} — ${scope}. Click to change the token.`
          : 'No usable token in this browser. Click to connect.'
      }
      className="ml-auto flex items-center gap-2 rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:border-slate-500"
    >
      <span
        aria-hidden
        className={`h-2 w-2 rounded-full ${
          connected ? 'bg-emerald-400' : 'bg-amber-400'
        }`}
      />
      <span className="font-medium">{label}</span>
      <span className="hidden text-slate-500 sm:inline">· {scope}</span>
    </Link>
  );
}
