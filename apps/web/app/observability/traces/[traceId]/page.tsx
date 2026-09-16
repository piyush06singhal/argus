import Link from 'next/link';
import {
  apiFetch,
  formatDate,
  formatDuration,
  type TraceDetail,
  type TraceSpan,
} from '@/lib/api';

/**
 * Trace detail (Phase 1 §56) — one trace, its spans as a tree, and an orphan
 * check. Server component fetches the reconstruction endpoint directly.
 */

interface SpanNode {
  span: TraceSpan;
  children: SpanNode[];
}

function buildSpanTree(spans: TraceSpan[]): SpanNode[] {
  const nodes = new Map<string, SpanNode>();
  for (const span of spans) {
    nodes.set(span.id, { span, children: [] });
  }
  const roots: SpanNode[] = [];
  for (const span of spans) {
    const node = nodes.get(span.id)!;
    if (span.parent_span_id && nodes.has(span.parent_span_id)) {
      nodes.get(span.parent_span_id)!.children.push(node);
    } else {
      roots.push(node);
    }
  }
  const sortByStart = (list: SpanNode[]) => {
    list.sort(
      (a, b) =>
        new Date(a.span.start_time).getTime() -
        new Date(b.span.start_time).getTime()
    );
    list.forEach((n) => sortByStart(n.children));
  };
  sortByStart(roots);
  return roots;
}

function SpanRow({ node, depth }: { node: SpanNode; depth: number }) {
  const { span } = node;
  const hasError = span.status === 'error';
  return (
    <li>
      <div
        className="flex items-center gap-3 px-3 py-2 font-mono text-xs transition-colors hover:bg-slate-800/40"
        style={{ paddingLeft: `${depth * 1.5 + 1}rem` }}
      >
        {depth > 0 ? (
          <span className="select-none text-slate-600" aria-hidden="true">
            └─
          </span>
        ) : (
          <span className="select-none text-slate-600" aria-hidden="true">
            ●
          </span>
        )}
        <span className="flex-1 truncate text-slate-200">{span.name}</span>
        <span className="shrink-0 text-slate-500">{span.service}</span>
        <span className="shrink-0 text-slate-500">
          {formatDate(span.start_time)}
        </span>
        <span
          className={`shrink-0 font-medium ${
            hasError ? 'text-argus-error' : 'text-slate-300'
          }`}
        >
          {formatDuration(span.duration_ms)}
        </span>
      </div>
      {node.children.length > 0 ? (
        <ul>
          {node.children.map((child) => (
            <SpanRow key={child.span.id} node={child} depth={depth + 1} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export default async function TraceDetailPage({
  params,
}: {
  params: { traceId: string };
}) {
  const traceId = decodeURIComponent(params.traceId);

  try {
    const detail = await apiFetch<TraceDetail>(
      `/api/v1/observability/traces/${encodeURIComponent(traceId)}`
    );
    const tree = buildSpanTree(detail.spans);
    const orphanSpans = detail.spans.filter(
      (s) => s.parent_span_id && !detail.spans.some((p) => p.id === s.parent_span_id)
    );

    return (
      <div className="space-y-6">
        <div>
          <Link href="/observability/traces" className="btn-ghost">
            ← Back to traces
          </Link>
          <h1 className="mt-3 text-2xl font-semibold text-slate-100">
            Trace <span className="font-mono">{traceId}</span>
          </h1>
          <p className="mt-1 text-sm text-slate-400">
            {detail.name ?? detail.service} · {detail.spans.length} spans ·{' '}
            {formatDuration(detail.duration_ms)}
          </p>
        </div>

        {orphanSpans.length > 0 ? (
          <div className="card border-argus-warning/40">
            <h2 className="font-medium text-argus-warning">
              {orphanSpans.length} orphan span
              {orphanSpans.length === 1 ? '' : 's'}
            </h2>
            <p className="mt-1 text-sm text-slate-400">
              Span(s) whose parent reference cannot be resolved — the trace is
              partially reconstructed.
            </p>
          </div>
        ) : (
          <div className="card border-argus-success/40">
            <h2 className="font-medium text-argus-success">
              Trace is complete
            </h2>
            <p className="mt-1 text-sm text-slate-400">
              All spans link to a parent: no orphans detected.
            </p>
          </div>
        )}

        <div className="card">
          <h2 className="mb-3 font-medium text-slate-200">Span tree</h2>
          {tree.length === 0 ? (
            <p className="text-sm text-slate-400">
              No spans recorded for this trace.
            </p>
          ) : (
            <div className="rounded-md border border-slate-800 bg-slate-950">
              <ul>
                {tree.map((node) => (
                  <SpanRow key={node.span.id} node={node} depth={0} />
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    );
  } catch {
    return (
      <div className="space-y-6">
        <div>
          <Link href="/observability/traces" className="btn-ghost">
            ← Back to traces
          </Link>
          <h1 className="mt-3 text-2xl font-semibold text-slate-100">
            Trace not found
          </h1>
        </div>
        <div className="card border-argus-error/40">
          <p className="text-sm text-slate-400">
            No trace matches{' '}
            <span className="font-mono text-argus-accent">{traceId}</span>, or
            the backend is unreachable.
          </p>
        </div>
      </div>
    );
  }
}