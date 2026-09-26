/**
 * Presentation helpers for Phase 4 root-cause & causal analysis.
 *
 * The rules that shape this module are the phase's own constraints:
 *
 * 1. **No fake precision.** The backend returns confidence *buckets*, never a
 *    probability, so nothing here turns a score into a percentage. `INSUFFICIENT`
 *    renders as its own outcome, not as "0%".
 * 2. **Correlation is never dressed as causation.** `CORRELATES_WITH` is
 *    labelled co-occurrence everywhere and is excluded from chain/highlight
 *    logic, exactly as `causal_explanation.DIRECTIONAL_TYPES` does server-side.
 * 3. **A score is only shown with its components.** The breakdown is the reason
 *    a number is defensible, so the UI always renders it alongside.
 * 4. **Every enum mirrors the backend.** An unrecognised value renders as
 *    itself rather than being coerced into a known one.
 */

import type {
  CausalEvidence,
  CausalEvidenceCategory,
  CausalRelationship,
  CausalRelationshipType,
  CandidateStatus,
  CandidateType,
  ConfidenceLevel,
  RootCauseCandidate,
  ScoreBreakdown,
} from './api-client';

// ---------------------------------------------------------------------------
// Confidence (§26, §27)
// ---------------------------------------------------------------------------

export const CONFIDENCE_STYLES: Record<ConfidenceLevel, string> = {
  HIGH: 'bg-argus-success/15 text-argus-success',
  MEDIUM: 'bg-argus-warning/15 text-argus-warning',
  LOW: 'bg-argus-info/15 text-argus-info',
  INSUFFICIENT: 'bg-slate-700/40 text-slate-400',
};

export const CONFIDENCE_NOTES: Record<ConfidenceLevel, string> = {
  HIGH: 'multiple independent evidence sources support the same chain',
  MEDIUM: 'evidence aligns, but direct causal evidence is incomplete',
  LOW: 'only temporal or structural evidence exists',
  INSUFFICIENT: 'evidence is contradictory or too sparse to decide',
};

export const CONFIDENCE_ORDER: ConfidenceLevel[] = [
  'HIGH',
  'MEDIUM',
  'LOW',
  'INSUFFICIENT',
];

export function confidenceStyle(value: ConfidenceLevel): string {
  return CONFIDENCE_STYLES[value] ?? CONFIDENCE_STYLES.INSUFFICIENT;
}

// ---------------------------------------------------------------------------
// Candidate types & statuses (§7)
// ---------------------------------------------------------------------------

export const CANDIDATE_TYPE_LABELS: Record<CandidateType, string> = {
  DEPLOYMENT: 'Deployment',
  CONFIGURATION_CHANGE: 'Configuration change',
  APPLICATION_COMPONENT: 'Application component',
  DATABASE: 'Datastore',
  EXTERNAL_DEPENDENCY: 'External dependency',
  INFRASTRUCTURE: 'Infrastructure',
  RESOURCE_EXHAUSTION: 'Resource exhaustion',
  DEPENDENCY_FAILURE: 'Dependency failure',
  DATA_ISSUE: 'Data issue',
  UNKNOWN: 'Unknown',
};

export const CANDIDATE_STATUS_LABELS: Record<CandidateStatus, string> = {
  SUPPORTED: 'Supported',
  WEAKENED: 'Weakened by contradictions',
  WITHDRAWN: 'Withdrawn — insufficient evidence',
  UNDER_EVALUATION: 'Under evaluation',
};

/** What a candidate is *called*: component name, else its discovery sentence. */
export function candidateLabel(candidate: RootCauseCandidate): string {
  return (
    candidate.component_name ??
    candidate.explanation ??
    CANDIDATE_TYPE_LABELS[candidate.candidate_type] ??
    candidate.candidate_type
  );
}

export function candidateTypeLabel(type: CandidateType): string {
  return CANDIDATE_TYPE_LABELS[type] ?? type;
}

/** Ranked candidates: score desc, ties broken by id for stable rendering. */
export function rankCandidates(
  candidates: RootCauseCandidate[]
): RootCauseCandidate[] {
  return [...candidates].sort(
    (a, b) => b.score - a.score || a.id.localeCompare(b.id)
  );
}

// ---------------------------------------------------------------------------
// Relationship semantics (§8, §40)
// ---------------------------------------------------------------------------

/**
 * Directional relationship types — mirrors `causal_explanation.DIRECTIONAL_TYPES`.
 * Anything not listed here is a co-occurrence and must not be drawn as a cause.
 */
export const DIRECTIONAL_TYPES: CausalRelationshipType[] = [
  'POSSIBLE_CAUSE',
  'LIKELY_CAUSE',
  'CONTRIBUTES_TO',
  'TRIGGERS',
  'BLOCKS',
  'AMPLIFIES',
  'DOWNSTREAM_EFFECT',
];

export function isDirectional(type: CausalRelationshipType): boolean {
  return DIRECTIONAL_TYPES.includes(type);
}

export const RELATIONSHIP_LABELS: Record<CausalRelationshipType, string> = {
  LIKELY_CAUSE: 'likely cause of',
  POSSIBLE_CAUSE: 'possible cause of',
  TRIGGERS: 'triggers',
  CONTRIBUTES_TO: 'contributes to',
  AMPLIFIES: 'amplifies',
  BLOCKS: 'blocks',
  DOWNSTREAM_EFFECT: 'downstream effect of',
  CORRELATES_WITH: 'correlates with',
};

export function relationshipLabel(type: CausalRelationshipType): string {
  return RELATIONSHIP_LABELS[type] ?? type;
}

/** Visual weight: a co-occurrence is deliberately drawn differently (§40). */
export function relationshipStroke(
  type: CausalRelationshipType,
  confidence: ConfidenceLevel
): { dash: string; width: number } {
  if (!isDirectional(type)) {
    return { dash: '4 4', width: 1.2 };
  }
  const width = confidence === 'HIGH' ? 2.6 : confidence === 'MEDIUM' ? 1.9 : 1.3;
  return { dash: type === 'LIKELY_CAUSE' ? '' : '7 3', width };
}

// ---------------------------------------------------------------------------
// Evidence (§23, §24)
// ---------------------------------------------------------------------------

export const EVIDENCE_CATEGORY_LABELS: Record<CausalEvidenceCategory, string> = {
  TEMPORAL: 'Temporal',
  TRACE: 'Trace',
  DEPENDENCY: 'Dependency',
  CHANGE: 'Change',
  METRIC: 'Metric',
  LOG: 'Log',
  HEALTH: 'Health',
  RESOURCE: 'Resource',
  CONFIGURATION: 'Configuration',
  DEPLOYMENT: 'Deployment',
  RECOVERY: 'Recovery',
  CONTRADICTING: 'Contradicting',
};

export const EVIDENCE_CATEGORY_STYLES: Record<CausalEvidenceCategory, string> = {
  TEMPORAL: 'bg-argus-info/15 text-argus-info',
  TRACE: 'bg-argus-accent/15 text-argus-accent',
  DEPENDENCY: 'bg-argus-accent/10 text-argus-accent',
  CHANGE: 'bg-argus-warning/15 text-argus-warning',
  METRIC: 'bg-argus-info/10 text-argus-info',
  LOG: 'bg-slate-700/40 text-slate-300',
  HEALTH: 'bg-argus-success/10 text-argus-success',
  RESOURCE: 'bg-argus-warning/10 text-argus-warning',
  CONFIGURATION: 'bg-argus-warning/15 text-argus-warning',
  DEPLOYMENT: 'bg-argus-warning/15 text-argus-warning',
  RECOVERY: 'bg-argus-success/15 text-argus-success',
  CONTRADICTING: 'bg-argus-error/15 text-argus-error',
};

export function evidenceCategoryLabel(category: CausalEvidenceCategory): string {
  return EVIDENCE_CATEGORY_LABELS[category] ?? category;
}

/**
 * Split evidence by polarity, the way every explainability view needs it.
 *
 * The API already returns the split, but the causal graph and the edge
 * inspector need the same grouping over raw rows.
 */
export function splitEvidence(evidence: CausalEvidence[]): {
  supporting: CausalEvidence[];
  contradicting: CausalEvidence[];
  neutral: CausalEvidence[];
} {
  const supporting: CausalEvidence[] = [];
  const contradicting: CausalEvidence[] = [];
  const neutral: CausalEvidence[] = [];
  for (const item of evidence) {
    if (item.polarity === 'SUPPORTING') {
      supporting.push(item);
    } else if (item.polarity === 'CONTRADICTING') {
      contradicting.push(item);
    } else {
      neutral.push(item);
    }
  }
  return { supporting, contradicting, neutral };
}

/**
 * The evidence a candidate *owns*.
 *
 * Rows bound to a relationship (`relationship_id`) justify an edge, not the
 * candidate; the backend excludes them from candidate reads and so must the UI,
 * or the displayed list would disagree with the counts beside it.
 */
export function candidateOwnedEvidence(evidence: CausalEvidence[]): CausalEvidence[] {
  return evidence.filter(
    (item) => item.relationship_id === null || item.relationship_id === undefined
  );
}

export function relationshipEvidence(
  evidence: CausalEvidence[],
  relationshipId: string
): CausalEvidence[] {
  return evidence.filter((item) => item.relationship_id === relationshipId);
}

/**
 * Does a candidate's rendered evidence agree with its own counters?
 *
 * Both must describe the same rows, or the panel is telling two stories; the
 * RCA view renders the counts and this check exists so a drift is visible
 * instead of silently trusted.
 */
export function evidenceReconciles(
  candidate: RootCauseCandidate,
  owned: CausalEvidence[]
): boolean {
  const counted =
    candidate.supporting_evidence_count +
    candidate.contradicting_evidence_count +
    candidate.neutral_evidence_count;
  return counted === owned.length;
}

// ---------------------------------------------------------------------------
// Score breakdown (§25)
// ---------------------------------------------------------------------------

const BREAKDOWN_LABELS: Record<keyof ScoreBreakdown, string> = {
  total: 'Total',
  temporal: 'Temporal alignment',
  trace: 'Trace support',
  dependency: 'Dependency support',
  propagation: 'Propagation (edges out)',
  change: 'Change relevance',
  recovery: 'Recovery evidence',
  resource: 'Resource/metric/log signal',
  contradiction_penalty: 'Contradiction penalty',
  outgoing_edge_support: 'Downstream explanation',
};

/**
 * Rows for the score breakdown, omitting zeros.
 *
 * The penalty is returned separately because it *subtracts*: rendering it in the
 * same list as the positive contributors would invite reading the numbers as a
 * sum that does not add up.
 */
export function scoreBreakdownRows(breakdown?: ScoreBreakdown | null): {
  positive: { key: string; label: string; value: number }[];
  penalty: { label: string; value: number } | null;
} {
  if (!breakdown) {
    return { positive: [], penalty: null };
  }
  const positive: { key: string; label: string; value: number }[] = [];
  let penalty: { label: string; value: number } | null = null;
  for (const [key, value] of Object.entries(breakdown)) {
    if (key === 'total' || key === 'contradiction_penalty') {
      continue;
    }
    if (typeof value !== 'number' || value === 0) {
      continue;
    }
    positive.push({
      key,
      label: BREAKDOWN_LABELS[key as keyof ScoreBreakdown] ?? key,
      value,
    });
  }
  positive.sort((a, b) => b.value - a.value);
  const rawPenalty = breakdown.contradiction_penalty;
  if (typeof rawPenalty === 'number' && rawPenalty !== 0) {
    penalty = { label: BREAKDOWN_LABELS.contradiction_penalty, value: rawPenalty };
  }
  return { positive, penalty };
}

/** Two-decimal score, never more — precision beyond that is not meaningful. */
export function formatScore(value?: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return '—';
  }
  return value.toFixed(2);
}

// ---------------------------------------------------------------------------
// Time alignment (§10, §12)
// ---------------------------------------------------------------------------

/**
 * How a target followed (or preceded) its source.
 *
 * A negative alignment is a temporal contradiction: the "effect" started first.
 * That is rendered as a contradiction, never smoothed into "0 seconds".
 */
export function formatAlignment(seconds?: number | null): string {
  if (seconds === null || seconds === undefined) {
    return 'no measured offset';
  }
  const magnitude = Math.abs(seconds);
  const amount =
    magnitude < 90
      ? `${magnitude} second${magnitude === 1 ? '' : 's'}`
      : magnitude < 5400
        ? `${Math.round(magnitude / 60)} minute${Math.round(magnitude / 60) === 1 ? '' : 's'}`
        : `${(magnitude / 3600).toFixed(1)} hours`;
  if (seconds < 0) {
    return `target degraded ${amount} before the source — temporal contradiction`;
  }
  if (seconds === 0) {
    return 'simultaneous with the source';
  }
  return `${amount} after the source`;
}

export function isTemporalContradiction(
  seconds?: number | null
): boolean {
  return seconds !== null && seconds !== undefined && seconds < 0;
}

// ---------------------------------------------------------------------------
// Graph building
// ---------------------------------------------------------------------------

export interface CausalGraphNodeLayout {
  id: string;
  candidate: RootCauseCandidate;
  x: number;
  y: number;
  degree: number;
}

export interface CausalGraphEdgeLayout {
  id: string;
  relationship: CausalRelationship;
  source: CausalGraphNodeLayout;
  target: CausalGraphNodeLayout;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  labelX: number;
  labelY: number;
}

export interface CausalGraphLayout {
  nodes: CausalGraphNodeLayout[];
  edges: CausalGraphEdgeLayout[];
  width: number;
  height: number;
}

const NODE_WIDTH = 190;
const NODE_HEIGHT = 62;
const COLUMN_GAP = 76;
const ROW_GAP = 42;
const PADDING = 28;

/**
 * Deterministic layered layout for the causal graph.
 *
 * Columns follow causal depth (a node sits one column right of whatever it
 * explains); rows are ordered by score so the most-supported candidate is
 * highest. Pure and deterministic, so the same analysis always draws the same
 * picture — and testable without a DOM.
 */
export function layoutCausalGraph(
  candidates: RootCauseCandidate[],
  relationships: CausalRelationship[]
): CausalGraphLayout {
  const ranked = rankCandidates(candidates);
  const byId = new Map(ranked.map((candidate) => [candidate.id, candidate]));
  const edges = relationships.filter(
    (edge) => byId.has(edge.source_candidate_id) && byId.has(edge.target_candidate_id)
  );

  // Longest-path depth over the *directional* subgraph only; a co-occurrence
  // must not push a node deeper, because it carries no direction.
  const depth = new Map<string, number>();
  for (const candidate of ranked) {
    depth.set(candidate.id, 0);
  }
  const directional = edges.filter((edge) => isDirectional(edge.relationship_type));
  // Bounded relaxation: the engine rejects cycles, but a malformed payload must
  // not hang the UI.
  const maxPasses = ranked.length + 1;
  for (let pass = 0; pass < maxPasses; pass += 1) {
    let changed = false;
    for (const edge of directional) {
      const sourceDepth = depth.get(edge.source_candidate_id) ?? 0;
      const targetDepth = depth.get(edge.target_candidate_id) ?? 0;
      if (targetDepth <= sourceDepth) {
        depth.set(edge.target_candidate_id, sourceDepth + 1);
        changed = true;
      }
    }
    if (!changed) {
      break;
    }
  }

  const degree = new Map<string, number>();
  for (const edge of edges) {
    degree.set(
      edge.source_candidate_id,
      (degree.get(edge.source_candidate_id) ?? 0) + 1
    );
    degree.set(
      edge.target_candidate_id,
      (degree.get(edge.target_candidate_id) ?? 0) + 1
    );
  }

  const columns = new Map<number, RootCauseCandidate[]>();
  for (const candidate of ranked) {
    const column = depth.get(candidate.id) ?? 0;
    const bucket = columns.get(column) ?? [];
    bucket.push(candidate);
    columns.set(column, bucket);
  }

  const orderedColumns = Array.from(columns.keys()).sort((a, b) => a - b);
  const lastColumn = orderedColumns.length ? Math.max(...orderedColumns) : 0;

  const nodes: CausalGraphNodeLayout[] = [];
  const nodeById = new Map<string, CausalGraphNodeLayout>();
  let maxRows = 0;
  for (const column of orderedColumns) {
    const bucket = columns.get(column) ?? [];
    maxRows = Math.max(maxRows, bucket.length);
    bucket.forEach((candidate, row) => {
      const node: CausalGraphNodeLayout = {
        id: candidate.id,
        candidate,
        x: PADDING + column * (NODE_WIDTH + COLUMN_GAP),
        y: PADDING + row * (NODE_HEIGHT + ROW_GAP),
        degree: degree.get(candidate.id) ?? 0,
      };
      nodes.push(node);
      nodeById.set(candidate.id, node);
    });
  }

  const edgeLayouts: CausalGraphEdgeLayout[] = [];
  for (const relationship of edges) {
    const source = nodeById.get(relationship.source_candidate_id);
    const target = nodeById.get(relationship.target_candidate_id);
    if (!source || !target) {
      continue;
    }
    const x1 = source.x + NODE_WIDTH;
    const y1 = source.y + NODE_HEIGHT / 2;
    const x2 = target.x;
    const y2 = target.y + NODE_HEIGHT / 2;
    edgeLayouts.push({
      id: relationship.id,
      relationship,
      source,
      target,
      x1,
      y1,
      x2,
      y2,
      labelX: (x1 + x2) / 2,
      labelY: (y1 + y2) / 2 - 6,
    });
  }

  const columnCount = lastColumn + 1;
  const rowCount = Math.max(maxRows, 1);
  return {
    nodes,
    edges: edgeLayouts,
    width: PADDING * 2 + columnCount * NODE_WIDTH + Math.max(0, columnCount - 1) * COLUMN_GAP,
    height: PADDING * 2 + rowCount * NODE_HEIGHT + Math.max(0, rowCount - 1) * ROW_GAP,
  };
}

export const CAUSAL_GRAPH_NODE_WIDTH = NODE_WIDTH;
export const CAUSAL_GRAPH_NODE_HEIGHT = NODE_HEIGHT;

/**
 * Render a chain as "A → B → C" using candidate labels.
 *
 * Only directional links participate; a chain threaded through a correlation
 * would imply a direction nobody observed.
 */
export function chainSummary(
  chainCandidateIds: string[],
  candidates: RootCauseCandidate[]
): string {
  const byId = new Map(candidates.map((candidate) => [candidate.id, candidate]));
  const labels = chainCandidateIds.map((id) => {
    const candidate = byId.get(id);
    return candidate ? candidateLabel(candidate) : 'unknown candidate';
  });
  return labels.join(' → ');
}

/** The single sentence an analyst reads first. */
export function headlineFor(
  summary?: string | null,
  primary?: RootCauseCandidate | null
): string {
  if (summary) {
    return summary;
  }
  if (primary) {
    return `Most-supported explanation: ${candidateLabel(primary)} (${candidateTypeLabel(
      primary.candidate_type
    )}) at ${primary.confidence} confidence.`;
  }
  return 'Insufficient evidence to determine a root cause.';
}

/** Rendered with the analysis — mirrors the backend's own disclaimer (§2). */
export const CAUSAL_DISCLAIMER =
  'Causal analysis produces evidence-supported hypotheses; it does not ' +
  'constitute mathematical proof of causation.';
