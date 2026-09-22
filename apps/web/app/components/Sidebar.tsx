'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

interface NavItem {
  href: string;
  label: string;
}

const NAV_ITEMS: NavItem[] = [
  { href: '/', label: 'Dashboard' },
  { href: '/projects', label: 'Projects' },
  { href: '/system-map', label: 'System Map' },
  { href: '/incidents', label: 'Incidents' },
  { href: '/incidents/dashboard', label: 'Incident Dashboard' },
  { href: '/incidents/rca', label: 'Root Cause Analysis' },
  { href: '/reproductions', label: 'Failure Reproduction' },
  { href: '/debugger', label: 'AI Debugger' },
  { href: '/fixes', label: 'Fix & Verification' },
  { href: '/reliability', label: 'Reliability Intelligence' },
  { href: '/remediation', label: 'Safe Remediation' },
  { href: '/anomalies', label: 'Anomaly Center' },
  { href: '/observability', label: 'Observability' },
  { href: '/observability/logs', label: 'Logs' },
  { href: '/observability/metrics', label: 'Metrics' },
  { href: '/observability/traces', label: 'Traces' },
  { href: '/observability/events', label: 'Events' },
  { href: '/ingestion-health', label: 'Ingestion Health' },
  { href: '/deployments', label: 'Deployments' },
  { href: '/settings', label: 'Settings' },
];

export default function Sidebar() {
  const pathname = usePathname();

  const isActive = (href: string) => {
    if (href === '/') {
      return pathname === '/';
    }
    // Longest-prefix wins so `/incidents/dashboard` does not also light up
    // `/incidents`.
    const better = NAV_ITEMS.filter(
      (item) =>
        item.href !== '/' &&
        (pathname === item.href || pathname.startsWith(`${item.href}/`))
    ).sort((a, b) => b.href.length - a.href.length)[0];
    return better?.href === href;
  };

  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-slate-800 bg-slate-900/50">
      <div className="flex h-16 items-center gap-2 border-b border-slate-800 px-6">
        <div className="flex h-8 w-8 items-center justify-center rounded-md bg-argus-accent font-mono text-sm font-bold text-slate-950">
          A
        </div>
        <span className="text-lg font-semibold tracking-tight text-slate-100">
          ARGUS
        </span>
        <span className="mt-0.5 rounded-full bg-slate-800 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-slate-400">
          Intelligence
        </span>
      </div>

      <nav className="flex-1 space-y-1 overflow-y-auto px-3 py-4">
        {NAV_ITEMS.map((item) => {
          const active = isActive(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={
                active
                  ? 'block rounded-md bg-argus-accent/10 px-3 py-2 text-sm font-medium text-argus-accent transition-colors hover:bg-argus-accent/15'
                  : 'block rounded-md px-3 py-2 text-sm font-medium text-slate-400 transition-colors hover:bg-slate-800 hover:text-slate-200'
              }
              aria-current={active ? 'page' : undefined}
            >
              {item.label}
            </Link>
          );
        })}
      </nav>

      <div className="border-t border-slate-800 px-6 py-4 text-xs text-slate-500">
        ARGUS Intelligence
        <br />
        Observability &amp; Incident Response
      </div>
    </aside>
  );
}