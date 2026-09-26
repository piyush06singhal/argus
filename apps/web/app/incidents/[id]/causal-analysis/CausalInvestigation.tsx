'use client';

/**
 * The causal investigation workspace (§40–§42).
 *
 * Three panels share one selection so an engineer can move between them:
 *
 * * the **causal graph** — zoom/pan, node and edge selection;
 * * the **evidence inspector** — why ARGUS believes the selected node or edge;
 * * the **timeline** — selecting an event highlights the graph node it belongs
 *   to, and selecting a node or edge highlights the events behind it.
 *
 * Nothing here computes causality. Every sentence comes from the analysis the
 * backend stored; the edge inspector is fetched per selection because the
 * explanation is verbose and should not be shipped with the graph.
 */

import { useEffect, useMemo, useRef, useState } from 'react';

import {
  api,
  formatDate,
  type CausalEvidence,
  type CausalGraph,
  type CausalRelationship,
  type RelationshipExplanation,
  type TimelineEvent,
} from '@/lib/api-client';
import {
  CAUSAL_GRAPH_NODE_HEIGHT,
  CAUSAL_GRAPH_NODE_WIDTH,
  candidateLabel,
  candidateOwnedEvidence,
  candidateTypeLabel,
  confidenceStyle,
  EVIDENCE_CATEGORY_STYLES,
  evidenceCategoryLabel,
  formatAlignment,
  formatScore,
  isDirectional,
  layoutCausalGraph,
  relationshipLabel,
  scoreBreakdownRows,
} from '@/lib/causal';

interface Props {
  incidentId: string;
  graph: CausalGraph;
  evidence: CausalEvidence[];
  chainCandidateIds: string[];
  timeline: TimelineEvent[];
}

const MIN_ZOOM = 0.35;
const MAX_ZOOM = 2.5;

export default function CausalInvestigation({
  incidentId,
  graph,
  evidence,
  chainCandidateIds,
  timeline,
}: Props) {
  const layout = useMemo(
    () => layoutCausalGraph(graph.nodes, graph.edges),
    [graph.nodes, graph.edges]
  );

  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(
    graph.primary_candidate_id ?? graph.nodes[0]?.id ?? null
  );
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; panX: number; panY: number } | null>(
    null
  );

  const [edgeExplanation, setEdgeExplanation] =
    useState<RelationshipExplanation | null>(null);
  const [edgeError, setEdgeError] = useState<string | null>(null);
  const [edgeLoading, setEdgeLoading] = useState(false);

  useEffect(() => {
    if (!selectedEdgeId) {
      return undefined;
    }
    let cancelled = false;
    setEdgeLoading(true);
    setEdgeError(null);
    api
      .explainRelationship(incidentId, selectedEdgeId)
      .then((result) => {
        if (!cancelled) {
          setEdgeExplanation(result);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setEdgeExplanation(null);
          setEdgeError(errorMessage(error));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setEdgeLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [incidentId, selectedEdgeId]);

  const selectedNode = layout.nodes.find((node) => node.id === selectedNodeId);
  const selectedEdge = layout.edges.find((edge) => edge.id === selectedEdgeId);

  /** Component ids the current selection implicates, for timeline highlighting. */
  const highlightedComponents = useMemo(() => {
    const ids = new Set<string>();
    if (selectedNode?.candidate.component_id) {
      ids.add(selectedNode.candidate.component_id);
    }
    if (selectedEdge) {
      const source = selectedEdge.relationship.source_candidate_id;
      const target = selectedEdge.relationship.target_candidate_id;
      for (const candidate of graph.nodes) {
        if (candidate.id === source || candidate.id === target) {
          if (candidate.component_id) {
            ids.add(candidate.component_id);
          }
        }
      }
    }
    return ids;
  }, [graph.nodes, selectedEdge, selectedNode]);

  const chainSet = useMemo(() => new Set(chainCandidateIds), [chainCandidateIds]);

  const nodeEvidence = useMemo(() => {
    if (!selectedNode) {
      return [] as CausalEvidence[];
    }
    return candidateOwnedEvidence(evidence).filter(
      (item) => item.candidate_id === selectedNode.id
    );
  }, [evidence, selectedNode]);

  const edgeEvidence = useMemo(() => {
    if (!selectedEdgeId) {
      return [] as CausalEvidence[];
    }
    return evidence.filter((item) => item.relationship_id === selectedEdgeId);
  }, [evidence, selectedEdgeId]);

  const handleWheel = (event: React.WheelEvent<SVGSVGElement>) => {
    event.preventDefault();
    const next = event.deltaY > 0 ? zoom * 0.9 : zoom * 1.1;
    setZoom(Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, next)));
  };

  const handlePointerDown = (event: React.PointerEvent<SVGSVGElement>) => {
    drag.current = {
      x: event.clientX,
      y: event.clientY,
      panX: pan.x,
      panY: pan.y,
    };
    (event.target as Element).setPointerCapture?.(event.pointerId);
  };

  const handlePointerMove = (event: React.PointerEvent<SVGSVGElement>) => {
    const origin = drag.current;
    if (!origin) {
      return;
    }
    setPan({
      x: origin.panX + (event.clientX - origin.x),
      y: origin.panY + (event.clientY - origin.y),
    });
  };

  const handlePointerUp = () => {
    drag.current = null;
  };

  const viewWidth = layout.width / zoom;
  const viewHeight = layout.height / zoom;

  return (
    <section className="card">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="font-medium text-slate-200">Causal graph</h2>
        <div className="flex items-center gap-3 text-xs text-slate-500">
          <span className="flex items-center gap-1">
            <svg width="22" height="8" aria-hidden="true">
              <line x1="0" y1="4" x2="22" y2="4" stroke="#38bdf8" strokeWidth="2.5" />
            </svg>
            causal hypothesis
          </span>
          <span className="flex items-center gap-1">
            <svg width="22" height="8" aria-hidden="true">
              <line
                x1="0"
                y1="4"
                x2="22"
                y2="4"
                stroke="#94a3b8"
                strokeWidth="1.5"
                strokeDasharray="4 4"
              />
            </svg>
            correlation only
          </span>
          <button
            type="button"
            className="rounded border border-slate-700 px-2 py-0.5 text-slate-400 hover:text-slate-200"
            onClick={() => {
              setZoom(1);
              setPan({ x: 0, y: 0 });
            }}
          >
            Reset view
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <div className="overflow-hidden rounded-md border border-slate-800 bg-slate-950/60">
          <svg
            role="img"
            aria-label="Causal graph for this incident"
            className="h-[420px] w-full cursor-grab touch-none active:cursor-grabbing"
            viewBox={`${pan.x} ${pan.y} ${viewWidth} ${viewHeight}`}
            onWheel={handleWheel}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={handlePointerUp}
            onPointerLeave={handlePointerUp}
          >
            <defs>
              <marker
                id="causal-arrow"
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth="7"
                markerHeight="7"
                orient="auto-start-reverse"
              >
                <path d="M 0 0 L 10 5 L 0 10 z" fill="#38bdf8" />
              </marker>
              <marker
                id="correlation-arrow"
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth="7"
                markerHeight="7"
                orient="auto-start-reverse"
              >
                <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8" />
              </marker>
            </defs>

            {layout.edges.map((edge) => {
              const directional = isDirectional(edge.relationship.relationship_type);
              const selected = edge.id === selectedEdgeId;
              return (
                <g key={edge.id}>
                  <line
                    x1={edge.x1}
                    y1={edge.y1}
                    x2={edge.x2}
                    y2={edge.y2}
                    stroke={selected ? '#f8fafc' : directional ? '#38bdf8' : '#94a3b8'}
                    strokeWidth={selected ? 3 : directional ? 2 : 1.4}
                    strokeDasharray={directional ? undefined : '5 4'}
                    markerEnd={
                      directional
                        ? 'url(#causal-arrow)'
                        : 'url(#correlation-arrow)'
                    }
                  />
                  {/* A wide transparent hit area: a 2px line is not a target. */}
                  <line
                    x1={edge.x1}
                    y1={edge.y1}
                    x2={edge.x2}
                    y2={edge.y2}
                    stroke="transparent"
                    strokeWidth={16}
                    className="cursor-pointer"
                    onClick={() => {
                      setSelectedEdgeId(edge.id);
                      setSelectedNodeId(null);
                    }}
                  >
                    <title>
                      {`${relationshipLabel(edge.relationship.relationship_type)} · ` +
                        `${edge.relationship.supporting_evidence_count} supporting fact(s)`}
                    </title>
                  </line>
                  <text
                    x={edge.labelX}
                    y={edge.labelY}
                    textAnchor="middle"
                    className="pointer-events-none"
                    fill={directional ? '#7dd3fc' : '#94a3b8'}
                    fontSize="10"
                  >
                    {relationshipLabel(edge.relationship.relationship_type)}
                  </text>
                </g>
              );
            })}

            {layout.nodes.map((node) => {
              const selected = node.id === selectedNodeId;
              const onChain = chainSet.has(node.id);
              const inTimeline = highlightedComponents.has(
                node.candidate.component_id ?? ''
              );
              return (
                <g
                  key={node.id}
                  transform={`translate(${node.x}, ${node.y})`}
                  className="cursor-pointer"
                  onClick={() => {
                    setSelectedNodeId(node.id);
                    setSelectedEdgeId(null);
                  }}
                >
                  <rect
                    width={CAUSAL_GRAPH_NODE_WIDTH}
                    height={CAUSAL_GRAPH_NODE_HEIGHT}
                    rx="8"
                    fill={selected ? '#1e293b' : '#0f172a'}
                    stroke={
                      selected
                        ? '#f8fafc'
                        : inTimeline
                          ? '#facc15'
                          : onChain
                            ? '#38bdf8'
                            : '#334155'
                    }
                    strokeWidth={selected ? 2.5 : onChain || inTimeline ? 2 : 1}
                  />
                  <text x="10" y="20" fill="#e2e8f0" fontSize="11">
                    {truncate(candidateLabel(node.candidate), 26)}
                  </text>
                  <text x="10" y="36" fill="#94a3b8" fontSize="9.5">
                    {`${candidateTypeLabel(node.candidate.candidate_type)} · score ${formatScore(
                      node.candidate.score
                    )}`}
                  </text>
                  <text x="10" y="51" fill="#94a3b8" fontSize="9.5">
                    {`${node.candidate.confidence} · ${
                      node.candidate.supporting_evidence_count
                    } supporting${
                      node.candidate.contradicting_evidence_count
                        ? `, ${node.candidate.contradicting_evidence_count} against`
                        : ''
                    }`}
                  </text>
                  {graph.primary_candidate_id === node.id ? (
                    <text x={CAUSAL_GRAPH_NODE_WIDTH - 10} y="20" textAnchor="end" fill="#4ade80" fontSize="9.5">
                      PRIMARY
                    </text>
                  ) : null}
                </g>
              );
            })}
          </svg>
        </div>

        <div className="space-y-3">
          {selectedNode ? (
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="text-sm font-medium text-slate-200">
                  {candidateLabel(selectedNode.candidate)}
                </h3>
                <span className={`badge ${confidenceStyle(selectedNode.candidate.confidence)}`}>
                  {selectedNode.candidate.confidence}
                </span>
              </div>
              <p className="mt-1 text-xs text-slate-500">
                {`${candidateTypeLabel(selectedNode.candidate.candidate_type)} · score ${formatScore(
                  selectedNode.candidate.score
                )} · ${selectedNode.candidate.supporting_evidence_count} supporting / ${selectedNode.candidate.contradicting_evidence_count} contradicting`}
              </p>
              {selectedNode.candidate.explanation ? (
                <p className="mt-2 text-xs text-slate-400">
                  {selectedNode.candidate.explanation}
                </p>
              ) : null}
              <ScoreBreakdown breakdown={selectedNode.candidate.score_breakdown} />
              <EvidenceList
                evidence={nodeEvidence}
                emptyNote="No stored fact could be bound to this candidate."
              />
            </div>
          ) : null}

          {selectedEdge ? (
            <div>
              <h3 className="text-sm font-medium text-slate-200">
                Why this relationship exists
              </h3>
              <p className="mt-1 text-xs text-slate-500">
                {`${edgeLabelFor(graph, selectedEdge.relationship)} — ${relationshipLabel(
                  selectedEdge.relationship.relationship_type
                )}`}
              </p>
              <p className="mt-1 text-xs text-slate-500">
                {formatAlignment(selectedEdge.relationship.temporal_alignment_seconds)}
              </p>
              <p className="mt-2 text-xs text-slate-400">
                {selectedEdge.relationship.explanation}
              </p>
              {!isDirectional(selectedEdge.relationship.relationship_type) ? (
                <p className="mt-1 text-xs text-argus-info">
                  Recorded as co-occurrence: no direction was observed, so this is
                  not a causal claim.
                </p>
              ) : null}
              {edgeLoading ? (
                <p className="mt-2 text-xs text-slate-500">Loading evidence…</p>
              ) : null}
              {edgeError ? (
                <p className="mt-2 text-xs text-argus-error">{edgeError}</p>
              ) : null}
              {edgeExplanation ? (
                <>
                  {edgeExplanation.caveats.length > 0 ? (
                    <ul className="mt-2 space-y-1">
                      {edgeExplanation.caveats.map((caveat) => (
                        <li key={caveat} className="text-xs text-argus-warning">
                          • {caveat}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  <EvidenceList
                    evidence={edgeEvidence}
                    emptyNote="No stored fact justifies this edge."
                    quotes={edgeExplanation.evidence_quotes}
                  />
                </>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>

      <div className="mt-4">
        <h3 className="font-medium text-slate-200">Timeline</h3>
        <p className="mb-2 text-xs text-slate-500">
          Select an event to highlight the graph node it belongs to; select a node
          or edge to highlight the events behind it.
        </p>
        {timeline.length === 0 ? (
          <p className="text-sm text-slate-400">
            No timeline entries have been recorded for this incident.
          </p>
        ) : (
          <ol className="max-h-72 space-y-2 overflow-y-auto pr-1">
            {timeline.map((event) => {
              const highlighted =
                event.component_id !== null &&
                event.component_id !== undefined &&
                highlightedComponents.has(event.component_id);
              return (
                <li
                  key={event.id}
                  className={`flex items-start gap-3 rounded px-2 py-1.5 ${
                    highlighted ? 'bg-argus-accent/10' : ''
                  } ${
                    event.component_id
                      ? 'cursor-pointer hover:bg-slate-800/60'
                      : ''
                  }`}
                  onClick={() => {
                    if (!event.component_id) {
                      return;
                    }
                    const node = layout.nodes.find(
                      (item) => item.candidate.component_id === event.component_id
                    );
                    if (node) {
                      setSelectedNodeId(node.id);
                      setSelectedEdgeId(null);
                    }
                  }}
                >
                  <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-argus-accent" />
                  <div className="flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="badge bg-slate-800 text-slate-300">
                        {event.event_type}
                      </span>
                      <span className="text-xs text-slate-500">
                        {formatDate(event.occurred_at)}
                      </span>
                      {event.is_context_only ? (
                        <span className="text-xs text-slate-500">context only</span>
                      ) : null}
                    </div>
                    <p className="mt-0.5 text-sm text-slate-200">{event.title}</p>
                  </div>
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </section>
  );
}

function ScoreBreakdown({
  breakdown,
}: {
  breakdown: CausalGraph['nodes'][number]['score_breakdown'];
}) {
  const { positive, penalty } = scoreBreakdownRows(breakdown);
  if (positive.length === 0 && !penalty) {
    return null;
  }
  return (
    <div className="mt-2">
      <p className="text-xs uppercase tracking-wider text-slate-500">
        Score components
      </p>
      <ul className="mt-1 space-y-0.5">
        {positive.map((row) => (
          <li key={row.key} className="flex justify-between text-xs text-slate-400">
            <span>{row.label}</span>
            <span className="font-mono">+{row.value.toFixed(3)}</span>
          </li>
        ))}
        {penalty ? (
          <li className="flex justify-between text-xs text-argus-error">
            <span>{penalty.label}</span>
            <span className="font-mono">−{penalty.value.toFixed(3)}</span>
          </li>
        ) : null}
      </ul>
    </div>
  );
}

function EvidenceList({
  evidence,
  emptyNote,
  quotes,
}: {
  evidence: CausalEvidence[];
  emptyNote: string;
  quotes?: string[];
}) {
  const lines =
    evidence.length > 0 ? evidence.map((item) => item.quote) : (quotes ?? []);
  if (lines.length === 0) {
    return <p className="mt-2 text-xs text-slate-500">{emptyNote}</p>;
  }
  return (
    <ul className="mt-2 space-y-1">
      {evidence.map((item) => (
        <li key={item.id} className="text-xs text-slate-300">
          <span
            className={`badge ${
              EVIDENCE_CATEGORY_STYLES[item.category] ??
              'bg-slate-800 text-slate-300'
            }`}
          >
            {evidenceCategoryLabel(item.category)}
          </span>
          <span className="ml-2">{item.quote}</span>
          {item.observed_at ? (
            <span className="ml-1 text-slate-500">
              ({formatDate(item.observed_at)})
            </span>
          ) : null}
        </li>
      ))}
      {evidence.length === 0 && lines.length > 0
        ? lines.map((quote) => (
            <li key={quote} className="text-xs text-slate-300">
              {quote}
            </li>
          ))
        : null}
    </ul>
  );
}

function edgeLabelFor(graph: CausalGraph, relationship: CausalRelationship): string {
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const source = byId.get(relationship.source_candidate_id);
  const target = byId.get(relationship.target_candidate_id);
  return `${source ? candidateLabel(source) : 'unknown'} → ${
    target ? candidateLabel(target) : 'unknown'
  }`;
}

function errorMessage(error: unknown): string {
  return error instanceof Error
    ? error.message
    : 'Could not load the explanation for this relationship.';
}

function truncate(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}
