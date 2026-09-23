import Link from 'next/link';

import type { Project } from '@/lib/api';

/**
 * Shared building blocks for the Phase 11 platform surface.
 *
 * They exist as one module rather than a copy per page because the phase's own
 * rule (§133) is to remove duplication rather than add a dashboard-shaped layer
 * on top of the domain. Every page renders through these, so the honesty rules
 * (state needs a reason, empty is not healthy, a number carries its provenance)
 * are applied in one place.
 */

export function Card({
  title,
  subtitle,
  action,
  children,
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="card">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="font-medium text-slate-200">{title}</h2>
        <div className="flex items-center gap-3">
          {subtitle ? <p className="text-xs text-slate-500">{subtitle}</p> : null}
          {action}
        </div>
      </div>
      {children}
    </section>
  );
}

export function Tile({
  label,
  value,
  hint,
}: {
  label: string;
  value: string | number;
  hint?: string;
}) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/40 p-3">
      <div className="text-xs uppercase tracking-wider text-slate-500">{label}</div>
      <div className="mt-1 text-xl font-semibold text-slate-100">{value}</div>
      {hint ? <div className="mt-1 text-xs text-slate-500">{hint}</div> : null}
    </div>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-slate-400">{children}</p>;
}

export function Badge({
  children,
  className = 'bg-slate-800 text-slate-300',
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return <span className={`badge ${className}`}>{children}</span>;
}

export function Row({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-slate-800/60 py-2 last:border-0">
      <span className="text-xs uppercase tracking-wider text-slate-500">{label}</span>
      <span className="text-sm text-slate-200">{children}</span>
    </div>
  );
}

/**
 * §42. Project scope is explicit and visible: every read on this surface is
 * project-scoped, so the selected project is part of the page, not an implicit
 * server default.
 */
export function ProjectScope({
  projects,
  activeId,
  basePath,
  extraQuery = '',
}: {
  projects: Project[];
  activeId?: string;
  basePath: string;
  extraQuery?: string;
}) {
  if (projects.length === 0) {
    return (
      <Card title="No projects" subtitle="Every read is project-scoped">
        <Empty>
          No project could be loaded. ARGUS scopes every platform read to a
          project, so there is nothing to show without one.
        </Empty>
      </Card>
    );
  }
  return (
    <Card title="Project scope" subtitle="Every read and write is scoped to a project (§42)">
      <div className="flex flex-wrap gap-2">
        {projects.map((item) => (
          <Link
            key={item.id}
            href={`${basePath}?project_id=${encodeURIComponent(item.id)}${
              extraQuery ? `&${extraQuery}` : ''
            }`}
            className={`badge ${
              item.id === activeId
                ? 'bg-argus-accent/20 text-argus-accent'
                : 'bg-slate-800 text-slate-300'
            }`}
          >
            {item.name}
          </Link>
        ))}
      </div>
    </Card>
  );
}

/** A bounded, labelled list of limitations, rendered wherever a number appears. */
export function Limitations({ items }: { items?: string[] }) {
  if (!items || items.length === 0) {
    return null;
  }
  return (
    <ul className="mt-3 space-y-1 text-xs text-slate-500">
      {items.map((item) => (
        <li key={item}>· {item}</li>
      ))}
    </ul>
  );
}

export function readError(reason: unknown): string {
  if (reason instanceof Error) {
    return reason.message;
  }
  return String(reason);
}

/**
 * Renders a scalar-valued record as a definition list.
 *
 * Section payloads arrive as `Record<string, unknown>` because the backend
 * composes them from several owners. Rendering only the scalars (and saying how
 * many nested entries were not expanded) keeps the page honest: a nested object
 * is either expanded by a purpose-built component or explicitly named, never
 * silently stringified into `[object Object]`.
 */
export function Facts({
  data,
  empty = 'No facts recorded.',
}: {
  data?: Record<string, unknown> | null;
  empty?: string;
}) {
  if (!data) {
    return <Empty>{empty}</Empty>;
  }
  const scalars = Object.entries(data).filter(
    ([, value]) => value === null || typeof value !== 'object'
  );
  const nested = Object.entries(data).filter(
    ([, value]) => value !== null && typeof value === 'object'
  );
  if (scalars.length === 0 && nested.length === 0) {
    return <Empty>{empty}</Empty>;
  }
  return (
    <div>
      {scalars.map(([key, value]) => (
        <Row key={key} label={labelise(key)}>
          {value === null || value === undefined ? '—' : String(value)}
        </Row>
      ))}
      {nested.length > 0 ? (
        <p className="mt-2 text-xs text-slate-500">
          · {nested.length} nested section(s) not expanded here:{' '}
          {nested.map(([key]) => labelise(key)).join(', ')}.
        </p>
      ) : null}
    </div>
  );
}

/** `components_with_evidence` → `Components with evidence`. */
export function labelise(key: string): string {
  const spaced = key.replace(/[_-]+/g, ' ').trim();
  if (spaced.length === 0) {
    return key;
  }
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** A scalar pulled out of an untyped record, or `—` when absent. */
export function scalar(data: Record<string, unknown> | null | undefined, key: string): string {
  if (!data) {
    return '—';
  }
  const value = data[key];
  if (value === null || value === undefined) {
    return '—';
  }
  if (typeof value === 'object') {
    return '—';
  }
  return String(value);
}

/** A bounded table. The header is fixed so a row cannot silently shift columns. */
export function Table({
  headers,
  rows,
  empty = 'Nothing to show.',
}: {
  headers: string[];
  rows: React.ReactNode[][];
  empty?: string;
}) {
  if (rows.length === 0) {
    return <Empty>{empty}</Empty>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] text-left text-sm">
        <thead>
          <tr className="border-b border-slate-800 text-xs uppercase tracking-wider text-slate-500">
            {headers.map((header) => (
              <th key={header} className="py-2 pr-4 font-medium">
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index} className="border-b border-slate-800/60 align-top">
              {row.map((cell, cellIndex) => (
                <td key={cellIndex} className="py-2 pr-4 text-slate-300">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
