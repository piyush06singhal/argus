'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import {
  apiFetch,
  formatDate,
  formatDuration,
  type Trace,
  type TraceDetail,
  type TraceSpan,
} from '@/lib/api-client';

type ViewState =
  | 'idle'
  | 'loading'
  | 'ready'
  | 'error';

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

function SpanRow({
  node,
  depth,
}: {
  node: SpanNode;
  depth: number;
}) {
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
        <span className="shrink-0 text-slate-500">{formatDate(span.start_time)}</span>
        <span
          className={`shrink-0 font-medium ${
            hasError ? 'text-argus-error' : 'text-slate-300'
          }`}
        >
          {formatDuration(span.duration_ms)}
        </span>
        <span className="shrink-0">
          {hasError ? (
            <span className="badge bg-argus-error/15 text-argus-error">error</span>
          ) : (
            <span className="badge bg-slate-800 text-slate-400">ok</span>
          )}
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

export default function TraceBrowser({ traces }: { traces: Trace[] }) {
  const [selectedTrace, setSelectedTrace] = useState<Trace | null>(null);
  const [view, setView] = useState<ViewState>('idle');
  const [detail, setDetail] = useState<TraceDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const selectTrace = useCallback(async (trace: Trace) => {
    setSelectedTrace(trace);
    setView('loading');
    setError(null);
    setDetail(null);
    try {
      const data = await apiFetch<TraceDetail>(
        `/api/v1/observability/traces/${encodeURIComponent(trace.trace_id)}`
      );
      setDetail(data);
      setView('ready');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load trace');
      setView('error');
    }
  }, []);

  // If the available trace list changes (e.g. pagination), drop stale selection.
  useEffect(() => {
    if (
      selectedTrace &&
      !traces.some((t) => t.trace_id === selectedTrace.trace_id)
    ) {
      setSelectedTrace(null);
      setDetail(null);
      setView('idle');
      setError(null);
    }
  }, [traces, selectedTrace]);

  const spanTree = useMemo(
    () => (detail ? buildSpanTree(detail.spans) : []),
    [detail]
  );

  return (
    <div className="space-y-4">
      {traces.length === 0 ? (
        <div className="card py-16 text-center">
          <p className="text-sm text-slate-400">No traces recorded.</p>
        </div>
      ) : (
        <div className="card overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-800 text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                <th className="px-4 py-3">Trace ID</th>
                <th className="px-4 py-3">Service</th>
                <th className="px-4 py-3">Started</th>
                <th className="px-4 py-3 text-right">Duration</th>
                <th className="px-4 py-3 text-right">Spans</th>
                <th className="px-4 py-3">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {traces.map((trace: Trace) => {
                const active = selectedTrace?.trace_id === trace.trace_id;
                const hasError = trace.has_errors || trace.status === 'error';
                return (
                  <tr
                    key={trace.trace_id}
                    onClick={() => selectTrace(trace)}
                    className={`cursor-pointer transition-colors ${
                      active ? 'bg-argus-accent/10' : 'hover:bg-slate-800/40'
                    }`}
                  >
                    <td className="px-4 py-3">
                      <Link
                        href={`/observability/traces/${encodeURIComponent(trace.trace_id)}`}
                        className="font-mono text-xs text-argus-accent hover:underline"
                        onClick={(e) => e.stopPropagation()}
                      >
                        {trace.trace_id} ↗
                      </Link>
                    </td>
                    <td className="px-4 py-3 text-slate-400">
                      {trace.name ?? trace.service}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 font-mono text-xs text-slate-400">
                      {formatDate(trace.started_at)}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-xs text-slate-300">
                      {formatDuration(trace.duration_ms)}
                    </td>
                    <td className="px-4 py-3 text-right text-slate-400">
                      {trace.span_count}
                    </td>
                    <td className="px-4 py-3">
                      {hasError ? (
                        <span className="badge bg-argus-error/15 text-argus-error">
                          error
                        </span>
                      ) : (
                        <span className="badge bg-argus-success/15 text-argus-success">
                          ok
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {view === 'loading' && (
        <div className="card border-slate-700 py-8 text-center text-sm text-slate-400">
          Loading...
        </div>
      )}

      {view === 'error' && (
        <div className="card border-argus-error/40">
          <h2 className="font-medium text-argus-error">
            Failed to load trace spans
          </h2>
          <p className="mt-2 text-sm text-slate-400">{error}</p>
        </div>
      )}

      {view === 'ready' && detail && (
        <div className="card">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h2 className="font-medium text-slate-200">
              Spans
              <span className="ml-2 text-xs font-normal text-slate-500">
                {detail.spans.length} span{detail.spans.length === 1 ? '' : 's'}
              </span>
            </h2>
            <p className="font-mono text-xs text-slate-500">{detail.trace_id}</p>
          </div>
          <div className="rounded-md border border-slate-800 bg-slate-950">
            {spanTree.length === 0 ? (
              <p className="p-4 text-sm text-slate-400">
                No span details available for this trace.
              </p>
            ) : (
              <ul>
                {spanTree.map((node) => (
                  <SpanRow key={node.span.id} node={node} depth={0} />
                ))}
              </ul>
            )}
          </div>
        </div>
      )}

      {view === 'idle' && (
        <div className="card border-dashed py-8 text-center text-sm text-slate-500">
          Click a trace row above to view its spans.
        </div>
      )}
    </div>
  );
}