import { describe, expect, it } from 'vitest';

import type {
  CausalEvidence,
  CausalRelationship,
  RootCauseCandidate,
  ScoreBreakdown,
} from '../api';
import {
  candidateLabel,
  candidateOwnedEvidence,
  chainSummary,
  CONFIDENCE_NOTES,
  CONFIDENCE_STYLES,
  CANDIDATE_TYPE_LABELS,
  EVIDENCE_CATEGORY_LABELS,
  evidenceCategoryLabel,
  evidenceReconciles,
  formatAlignment,
  formatScore,
  headlineFor,
  isDirectional,
  isTemporalContradiction,
  layoutCausalGraph,
  rankCandidates,
  relationshipEvidence,
  relationshipLabel,
  relationshipStroke,
  scoreBreakdownRows,
  splitEvidence,
} from '../causal';

function candidate(overrides: Partial<RootCauseCandidate> = {}): RootCauseCandidate {
  return {
    id: 'c1',
    analysis_id: 'a1',
    component_id: 'comp-1',
    component_name: 'Inventory Database',
    event_id: null,
    event_kind: null,
    candidate_type: 'DATABASE',
    status: 'SUPPORTED',
    score: 0.58,
    confidence: 'HIGH',
    is_external: false,
    first_observed_at: null,
    supporting_evidence_count: 2,
    contradicting_evidence_count: 1,
    neutral_evidence_count: 0,
    explanation: null,
    score_breakdown: null,
    reasons: null,
    uncertainty: null,
    created_at: '2026-09-20T15:00:00Z',
    updated_at: '2026-09-20T15:00:00Z',
    ...overrides,
  };
}

function relationship(
  overrides: Partial<CausalRelationship> = {}
): CausalRelationship {
  return {
    id: 'r1',
    analysis_id: 'a1',
    source_candidate_id: 'c1',
    target_candidate_id: 'c2',
    relationship_type: 'LIKELY_CAUSE',
    confidence: 'HIGH',
    supporting_evidence_count: 3,
    contradicting_evidence_count: 0,
    temporal_alignment_seconds: 30,
    structural_support: 2,
    observational_support: 1,
    contradiction_notes: null,
    explanation: 'A failed before B on the same call path.',
    created_at: '2026-09-20T15:00:00Z',
    updated_at: '2026-09-20T15:00:00Z',
    ...overrides,
  };
}

function evidence(overrides: Partial<CausalEvidence> = {}): CausalEvidence {
  return {
    id: 'e1',
    analysis_id: 'a1',
    candidate_id: 'c1',
    relationship_id: null,
    category: 'TRACE',
    polarity: 'SUPPORTING',
    source_table: 'span_records',
    source_id: null,
    incident_evidence_id: null,
    quote: 'FAILED span inside parent span',
    explanation: 'the call this parent made failed',
    component_id: 'comp-1',
    observed_at: null,
    strength: 0.9,
    created_at: '2026-09-20T15:00:00Z',
    updated_at: '2026-09-20T15:00:00Z',
    ...overrides,
  };
}

describe('confidence presentation', () => {
  it('covers every backend confidence level with a style and a note', () => {
    for (const level of ['HIGH', 'MEDIUM', 'LOW', 'INSUFFICIENT'] as const) {
      expect(CONFIDENCE_STYLES[level]).toBeTruthy();
      expect(CONFIDENCE_NOTES[level]).toBeTruthy();
    }
  });

  it('never renders a confidence as a percentage', () => {
    for (const note of Object.values(CONFIDENCE_NOTES)) {
      expect(note).not.toMatch(/%/);
    }
  });

  it('covers every candidate type and evidence category', () => {
    const types = [
      'DEPLOYMENT',
      'CONFIGURATION_CHANGE',
      'APPLICATION_COMPONENT',
      'DATABASE',
      'EXTERNAL_DEPENDENCY',
      'INFRASTRUCTURE',
      'RESOURCE_EXHAUSTION',
      'DEPENDENCY_FAILURE',
      'DATA_ISSUE',
      'UNKNOWN',
    ] as const;
    for (const type of types) {
      expect(CANDIDATE_TYPE_LABELS[type]).toBeTruthy();
    }
    const categories = [
      'TEMPORAL',
      'TRACE',
      'DEPENDENCY',
      'CHANGE',
      'METRIC',
      'LOG',
      'HEALTH',
      'RESOURCE',
      'CONFIGURATION',
      'DEPLOYMENT',
      'RECOVERY',
      'CONTRADICTING',
    ] as const;
    for (const category of categories) {
      expect(EVIDENCE_CATEGORY_LABELS[category]).toBeTruthy();
      expect(evidenceCategoryLabel(category)).toBeTruthy();
    }
  });
});

describe('candidate labelling and ranking', () => {
  it('prefers the component name over the discovery sentence', () => {
    expect(
      candidateLabel(candidate({ component_name: 'PostgreSQL' }))
    ).toBe('PostgreSQL');
  });

  it('falls back to the explanation, then the type label', () => {
    expect(
      candidateLabel(
        candidate({
          component_name: null,
          explanation: 'Deployment deploy-1 occurred before onset',
        })
      )
    ).toBe('Deployment deploy-1 occurred before onset');
    expect(
      candidateLabel(candidate({ component_name: null, explanation: null }))
    ).toBe('Datastore');
  });

  it('ranks by score with a deterministic tie-break', () => {
    const ranked = rankCandidates([
      candidate({ id: 'b', score: 0.2 }),
      candidate({ id: 'c', score: 0.9 }),
      candidate({ id: 'a', score: 0.2 }),
    ]);
    expect(ranked.map((item) => item.id)).toEqual(['c', 'a', 'b']);
  });
});

describe('relationship semantics', () => {
  it('treats CORRELATES_WITH as non-directional, mirroring the backend', () => {
    expect(isDirectional('CORRELATES_WITH')).toBe(false);
    for (const type of [
      'POSSIBLE_CAUSE',
      'LIKELY_CAUSE',
      'CONTRIBUTES_TO',
      'TRIGGERS',
      'BLOCKS',
      'AMPLIFIES',
      'DOWNSTREAM_EFFECT',
    ] as const) {
      expect(isDirectional(type)).toBe(true);
    }
  });

  it('labels correlation as co-occurrence, never as a cause', () => {
    expect(relationshipLabel('CORRELATES_WITH')).toBe('correlates with');
    for (const type of ['LIKELY_CAUSE', 'POSSIBLE_CAUSE', 'TRIGGERS'] as const) {
      expect(relationshipLabel(type)).not.toMatch(/correlat/i);
    }
  });

  it('draws correlation differently from causation', () => {
    const correlation = relationshipStroke('CORRELATES_WITH', 'HIGH');
    expect(correlation.dash).not.toBe('');
    const likely = relationshipStroke('LIKELY_CAUSE', 'HIGH');
    expect(likely.dash).toBe('');
    expect(likely.width).toBeGreaterThan(correlation.width);
  });

  it('scales stroke width with confidence', () => {
    const high = relationshipStroke('LIKELY_CAUSE', 'HIGH').width;
    const low = relationshipStroke('LIKELY_CAUSE', 'LOW').width;
    expect(high).toBeGreaterThan(low);
  });
});

describe('evidence grouping', () => {
  it('splits by polarity without losing rows', () => {
    const rows = [
      evidence({ id: 'a', polarity: 'SUPPORTING' }),
      evidence({ id: 'b', polarity: 'CONTRADICTING' }),
      evidence({ id: 'c', polarity: 'NEUTRAL' }),
      evidence({ id: 'd', polarity: 'SUPPORTING' }),
    ];
    const split = splitEvidence(rows);
    expect(split.supporting).toHaveLength(2);
    expect(split.contradicting).toHaveLength(1);
    expect(split.neutral).toHaveLength(1);
    expect(
      split.supporting.length + split.contradicting.length + split.neutral.length
    ).toBe(rows.length);
  });

  it('excludes edge-bound rows from what a candidate owns', () => {
    const rows = [
      evidence({ id: 'owned' }),
      evidence({ id: 'edge-bound', relationship_id: 'r1' }),
    ];
    expect(candidateOwnedEvidence(rows).map((row) => row.id)).toEqual(['owned']);
    expect(relationshipEvidence(rows, 'r1').map((row) => row.id)).toEqual([
      'edge-bound',
    ]);
  });

  it('reports a mismatch between counters and rendered evidence', () => {
    const owning = candidate({
      supporting_evidence_count: 7,
      contradicting_evidence_count: 2,
      neutral_evidence_count: 0,
    });
    expect(evidenceReconciles(owning, new Array(9).fill(evidence()))).toBe(true);
    // The real bug this guards: 13 edge rows were being returned as the
    // candidate's own evidence while its counts said 7.
    expect(evidenceReconciles(owning, new Array(20).fill(evidence()))).toBe(false);
  });
});

describe('score breakdown', () => {
  const breakdown: ScoreBreakdown = {
    total: 0.58,
    trace: 0.3,
    change: 0,
    recovery: 0,
    resource: 0.02,
    temporal: 0.1,
    dependency: 0,
    propagation: 0,
    contradiction_penalty: 0.15,
    outgoing_edge_support: 0.16,
  };

  it('omits zero components and sorts the rest by weight', () => {
    const { positive } = scoreBreakdownRows(breakdown);
    expect(positive.map((row) => row.key)).toEqual([
      'trace',
      'outgoing_edge_support',
      'temporal',
      'resource',
    ]);
    expect(positive.every((row) => row.value !== 0)).toBe(true);
  });

  it('reports the penalty separately, because it subtracts', () => {
    const { penalty } = scoreBreakdownRows(breakdown);
    expect(penalty?.value).toBe(0.15);
    expect(penalty?.label).toMatch(/contradiction/i);
  });

  it('handles a missing breakdown without inventing components', () => {
    expect(scoreBreakdownRows(null)).toEqual({ positive: [], penalty: null });
  });

  it('never shows more than two decimals of a score', () => {
    expect(formatScore(0.5804123456)).toBe('0.58');
    expect(formatScore(null)).toBe('—');
    expect(formatScore(undefined)).toBe('—');
  });
});

describe('time alignment', () => {
  it('describes propagation delay in plain language', () => {
    expect(formatAlignment(45)).toBe('45 seconds after the source');
    expect(formatAlignment(120)).toBe('2 minutes after the source');
    expect(formatAlignment(0)).toBe('simultaneous with the source');
  });

  it('reports a negative alignment as a temporal contradiction', () => {
    expect(formatAlignment(-90)).toMatch(/temporal contradiction/);
    expect(isTemporalContradiction(-1)).toBe(true);
    expect(isTemporalContradiction(0)).toBe(false);
    expect(isTemporalContradiction(null)).toBe(false);
    expect(isTemporalContradiction(undefined)).toBe(false);
  });

  it('says so when no offset was measured, rather than implying zero', () => {
    expect(formatAlignment(null)).toBe('no measured offset');
    expect(formatAlignment(undefined)).toBe('no measured offset');
  });
});

describe('causal graph layout', () => {
  const candidates = [
    candidate({ id: 'db', component_name: 'Inventory Database', score: 0.58 }),
    candidate({ id: 'inv', component_name: 'Inventory Service', score: 0.27 }),
    candidate({ id: 'checkout', component_name: 'Checkout Service', score: 0.29 }),
  ];
  const edges = [
    relationship({ id: 'e1', source_candidate_id: 'db', target_candidate_id: 'inv' }),
    relationship({
      id: 'e2',
      source_candidate_id: 'inv',
      target_candidate_id: 'checkout',
    }),
  ];

  it('places a target one column further right than its source', () => {
    const layout = layoutCausalGraph(candidates, edges);
    const byId = new Map(layout.nodes.map((node) => [node.id, node]));
    expect(byId.get('db')!.x).toBeLessThan(byId.get('inv')!.x);
    expect(byId.get('inv')!.x).toBeLessThan(byId.get('checkout')!.x);
  });

  it('is deterministic for the same input', () => {
    const first = layoutCausalGraph(candidates, edges);
    const second = layoutCausalGraph(candidates, edges);
    expect(second.nodes.map((node) => [node.id, node.x, node.y])).toEqual(
      first.nodes.map((node) => [node.id, node.x, node.y])
    );
  });

  it('does not let a correlation pushed depth, only direction does', () => {
    const withCorrelation = layoutCausalGraph(candidates, [
      relationship({
        id: 'e3',
        source_candidate_id: 'checkout',
        target_candidate_id: 'db',
        relationship_type: 'CORRELATES_WITH',
      }),
    ]);
    const depths = new Map(withCorrelation.nodes.map((node) => [node.id, node.x]));
    expect(depths.get('db')).toBe(depths.get('checkout'));
  });

  it('ignores edges pointing at unknown candidates instead of crashing', () => {
    const layout = layoutCausalGraph(candidates, [
      relationship({ id: 'e4', source_candidate_id: 'ghost', target_candidate_id: 'db' }),
    ]);
    expect(layout.edges).toHaveLength(0);
    expect(layout.nodes).toHaveLength(3);
  });

  it('survives a cyclic payload without hanging', () => {
    const layout = layoutCausalGraph(candidates, [
      relationship({ id: 'x', source_candidate_id: 'db', target_candidate_id: 'inv' }),
      relationship({ id: 'y', source_candidate_id: 'inv', target_candidate_id: 'db' }),
    ]);
    expect(layout.nodes).toHaveLength(3);
    expect(layout.edges).toHaveLength(2);
  });

  it('leaves room for every node in the reported size', () => {
    const layout = layoutCausalGraph(candidates, edges);
    for (const node of layout.nodes) {
      expect(node.x + 190).toBeLessThanOrEqual(layout.width);
      expect(node.y + 62).toBeLessThanOrEqual(layout.height);
    }
  });
});

describe('chain summary and headline', () => {
  it('renders the chain with real labels in order', () => {
    const summary = chainSummary(
      ['db', 'inv', 'checkout'],
      [
        candidate({ id: 'db', component_name: 'Inventory Database' }),
        candidate({ id: 'inv', component_name: 'Inventory Service' }),
        candidate({ id: 'checkout', component_name: 'Checkout Service' }),
      ]
    );
    expect(summary).toBe(
      'Inventory Database → Inventory Service → Checkout Service'
    );
  });

  it('never invents a label for an unknown candidate id', () => {
    expect(chainSummary(['missing'], [])).toBe('unknown candidate');
  });

  it('uses the stored summary when there is one', () => {
    const summary = 'Most-supported explanation: PostgreSQL — 3 categories align.';
    expect(headlineFor(summary, candidate())).toBe(summary);
  });

  it('refuses to name a root cause when nothing was selected', () => {
    expect(headlineFor(null, null)).toMatch(/insufficient evidence/i);
    expect(headlineFor(null, candidate({ confidence: 'HIGH' }))).toMatch(
      /most-supported explanation/i
    );
  });
});
