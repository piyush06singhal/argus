/**
 * Tests for the Phase 10 presentation helpers.
 *
 * These enforce the phase's honesty rules, not formatting:
 *
 * * a pattern never renders without its sample size and scope;
 * * a small sample is labelled as small rather than shown as a rate;
 * * a recommendation carries its policy requirement, its uncertainty and the
 *   criteria that ranked it;
 * * acceptance and outcome are reported as two separate facts;
 * * retired knowledge is never shown among live knowledge;
 * * nothing anywhere produces causal or confident-prediction language.
 */

import { describe, expect, it } from 'vitest';

import type {
  EffectivenessBucket,
  ExperienceItem,
  KnowledgeItem,
  LearnedRelationshipItem,
  RecommendationItem,
} from '../api';
import {
  comparisonVerdictLabel,
  confidenceLabel,
  HISTORICAL_RELATIONSHIP_FALLBACK,
  mayDrawArrow,
  relationshipDisclaimer,
  relationshipHeadline,
  relationshipsForComponent,
  relationshipSummary,
  sortRelationships,
  confidenceStyle,
  dataQualityLabel,
  dataQualityStyle,
  decisionAndOutcome,
  effectivenessHeadline,
  experienceHeadline,
  filterPatterns,
  historicalSummary,
  intelligenceHealthLabel,
  isBelowValidationFloor,
  isInvestigation,
  isLive,
  isMineable,
  isRetired,
  knowledgeStatusLabel,
  knowledgeStatusStyle,
  knowledgeTypeLabel,
  NO_HISTORY_NOTE,
  outcomeLabel,
  rankingRows,
  recommendationEvidence,
  recommendationStatusStyle,
  recommendationTypeLabel,
  requiresPolicyAttention,
  sampleLabel,
  scopeLabel,
  sortKnowledge,
  sortRecommendations,
  supportLabel,
  timelineStageLabel,
} from '../intelligence';

function knowledge(overrides: Partial<KnowledgeItem> = {}): KnowledgeItem {
  return {
    id: 'k1',
    knowledge_type: 'REMEDIATION_PATTERN',
    status: 'ACTIVE',
    scope: 'COMPONENT_SPECIFIC',
    component_id: 'c1',
    title: 'Restart resolved checkout error spikes',
    description: 'observed pattern',
    feature_signature: 'remediation:restart_service:checkout_error_spike',
    sample_count: 8,
    success_count: 7,
    support_strength: 0.875,
    coverage_start: '2026-08-01T00:00:00Z',
    coverage_end: '2026-09-01T00:00:00Z',
    confidence: 'MEDIUM',
    algorithm: 'remediation_pattern_v1',
    algorithm_version: '1.0',
    feature_schema_version: '1.0',
    limitations: ['observed pattern, not causal'],
    version: 1,
    sources: [{ type: 'experience', id: 'e1' }],
    experience_ids: ['e1'],
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    ...overrides,
  };
}

function recommendation(overrides: Partial<RecommendationItem> = {}): RecommendationItem {
  return {
    id: 'r1',
    recommendation_type: 'CONSIDER_RESTART',
    status: 'OPEN',
    title: 'Consider restarting checkout',
    rationale: 'historically resolved 2 of 3 comparable episodes',
    confidence: 'LOW',
    knowledge_ids: [],
    experience_ids: ['e1', 'e2'],
    current_evidence: { component_name: 'checkout', anomaly_types: ['error_rate_spike'] },
    limitations: ['historical outcomes do not guarantee this one'],
    ...overrides,
  };
}

function experience(overrides: Partial<ExperienceItem> = {}): ExperienceItem {
  return {
    id: 'e1',
    project_id: 'p1',
    start_time: '2026-09-01T10:00:00Z',
    end_time: '2026-09-01T10:30:00Z',
    outcome: 'EFFECTIVE',
    data_quality: 'OK',
    provenance: 'OBSERVABILITY',
    component_ids: ['c1'],
    failure_signature: {},
    failure_label: 'checkout_error_spike',
    failure_fingerprint: 'fp1',
    resolution_label: 'restart_service',
    ...overrides,
  };
}

function bucket(overrides: Partial<EffectivenessBucket> = {}): EffectivenessBucket {
  return {
    action_type: 'RESTART_SERVICE',
    dimension: 'action',
    dimension_value: null,
    comparable: 42,
    successful: 34,
    partially_successful: 4,
    failed: 4,
    rolled_back: 0,
    unresolved: 0,
    regression_count: 0,
    success_ratio: 34 / 42,
    insufficient: false,
    minimum_samples: 3,
    experience_ids: ['e1'],
    limitations: [],
    ...overrides,
  };
}

describe('knowledge status', () => {
  it('treats only VALIDATED and ACTIVE as believed', () => {
    expect(isLive('ACTIVE')).toBe(true);
    expect(isLive('VALIDATED')).toBe(true);
    expect(isLive('CANDIDATE')).toBe(false);
    expect(isLive('VALIDATING')).toBe(false);
    expect(isLive('DEPRECATED')).toBe(false);
    expect(isLive('REJECTED')).toBe(false);
    expect(isLive('SUPERSEDED')).toBe(false);
  });

  it('labels a candidate as not yet validated', () => {
    // The API returns candidates on purpose (§53); the label is what stops one
    // being read as a finding.
    expect(knowledgeStatusLabel('CANDIDATE')).toContain('not yet validated');
    expect(knowledgeStatusLabel('REJECTED')).toContain('Rejected');
    expect(knowledgeStatusLabel('DEPRECATED')).toContain('Deprecated');
  });

  it('never reuses the live colour for a retired status', () => {
    const live = knowledgeStatusStyle('ACTIVE');
    for (const retired of ['DEPRECATED', 'REJECTED', 'SUPERSEDED'] as const) {
      expect(isRetired(retired)).toBe(true);
      expect(knowledgeStatusStyle(retired)).not.toBe(live);
    }
  });

  it('keeps UNKNOWN visually distinct from a confident state', () => {
    expect(confidenceStyle('UNKNOWN')).not.toBe(confidenceStyle('HIGH'));
    expect(confidenceLabel('UNKNOWN')).toContain('insufficient');
  });
});

describe('sample size and confidence', () => {
  it('states counts over a sample, never a bare rate', () => {
    expect(sampleLabel(34, 42)).toBe('34 of 42 comparable cases were successful.');
    expect(sampleLabel(1, 1)).toBe('1 of 1 comparable case were successful.');
  });

  it('says so when there is no comparable case rather than printing 0%', () => {
    expect(sampleLabel(0, 0)).toBe('No comparable cases recorded.');
    expect(sampleLabel(null, 0)).toBe('No comparable cases recorded.');
  });

  it('carries the sample with a support ratio', () => {
    expect(supportLabel(0.875, 8)).toBe('88% of 8 cases');
    expect(supportLabel(null, 8)).toBeNull();
    // A ratio with no sample is not presented as a rate.
    expect(supportLabel(0.5, 0)).toBe('50% of an unstated sample');
  });

  it('treats a sample below the floor as a candidate', () => {
    expect(isBelowValidationFloor(2, 5)).toBe(true);
    expect(isBelowValidationFloor(5, 5)).toBe(false);
  });
});

describe('types and scopes', () => {
  it('names every knowledge type the API can return', () => {
    expect(knowledgeTypeLabel('REMEDIATION_PATTERN')).toBe('Remediation pattern');
    expect(knowledgeTypeLabel('COMPONENT_RELIABILITY_PATTERN')).toBe(
      'Component reliability pattern'
    );
  });

  it('renders scope so a claim cannot be read as broader than it is', () => {
    expect(scopeLabel('COMPONENT_SPECIFIC')).toBe('This component only');
    expect(scopeLabel('PROJECT_LEVEL')).toBe('This project');
    expect(scopeLabel('CROSS_PROJECT')).toBe('Across projects');
  });
});

describe('recommendations', () => {
  it('separates investigation from an implied action', () => {
    expect(isInvestigation('INVESTIGATE_COMPONENT')).toBe(true);
    expect(isInvestigation('CONSIDER_RESTART')).toBe(false);
  });

  it('flags advice that would need Phase 9 to allow it', () => {
    expect(
      requiresPolicyAttention(
        recommendation({ policy_note: 'RESTART_SERVICE requires human approval under Phase 9.' })
      )
    ).toBe(true);
    expect(
      requiresPolicyAttention(
        recommendation({
          recommendation_type: 'INVESTIGATE_COMPONENT',
          policy_note: 'No remediation action is implied.',
        })
      )
    ).toBe(false);
  });

  it('reports the decision and the outcome as two separate facts', () => {
    const open = decisionAndOutcome(recommendation());
    expect(open.decision).toContain('Not yet decided');
    expect(open.outcome).toContain('No outcome has been recorded');

    const accepted = decisionAndOutcome(
      recommendation({ status: 'ACCEPTED', decision: { actor: 'oncall@example.com' } })
    );
    // Accepting is not succeeding.
    expect(accepted.decision).toContain('oncall@example.com');
    expect(accepted.outcome).toContain('No outcome has been recorded');

    const closed = decisionAndOutcome(
      recommendation({
        status: 'REGRESSION_CAUSING',
        decision: { actor: 'oncall@example.com' },
        outcome: { verdict: 'REGRESSION_CAUSING' },
      })
    );
    expect(closed.outcome).toContain('caused a regression');
  });

  it('explains an expired card instead of leaving it looking pending', () => {
    const expired = decisionAndOutcome(recommendation({ status: 'EXPIRED' }));
    expect(expired.decision).toContain('Expired');
  });

  it('renders the ranking criteria it ordered by', () => {
    const rows = rankingRows({ evidence_strength: 0.3, sample_size: 4 });
    expect(rows).toHaveLength(2);
    expect(rows.map((row) => row.key)).toContain('evidence strength');
    expect(rankingRows(null)).toEqual([]);
  });

  it('renders the evidence it used as readable rows', () => {
    const rows = recommendationEvidence(
      recommendation({
        current_evidence: {
          component_name: 'checkout',
          anomaly_types: ['error_rate_spike', 'latency_spike'],
          nothing: null,
        },
      })
    );
    expect(rows.find((row) => row.key === 'anomaly types')?.value).toBe(
      'error_rate_spike, latency_spike'
    );
    expect(rows.find((row) => row.key === 'nothing')).toBeUndefined();
  });

  it('states the historical sample, and says so when there is none', () => {
    expect(
      historicalSummary({ comparable_episodes: 2, successful_resolutions: 2 })
    ).toBe('2 comparable episodes, 2 of which resolved successfully.');
    expect(historicalSummary({ comparable_episodes: 0 })).toBe(NO_HISTORY_NOTE);
    expect(historicalSummary(null)).toBeNull();
  });

  it('never colours a dismissed or expired card like an open one', () => {
    expect(recommendationStatusStyle('OPEN')).not.toBe(
      recommendationStatusStyle('DISMISSED')
    );
    expect(recommendationStatusStyle('REGRESSION_CAUSING')).not.toBe(
      recommendationStatusStyle('EFFECTIVE')
    );
  });

  it('names every recommendation type', () => {
    expect(recommendationTypeLabel('REVIEW_REMEDIATION')).toBe('Review remediation');
    expect(recommendationTypeLabel('RUN_REPRODUCTION')).toBe('Run reproduction');
  });
});

describe('experiences', () => {
  it('describes an episode with its failure shape and time', () => {
    expect(experienceHeadline(experience())).toContain('checkout_error_spike');
  });

  it('states outcomes honestly, including the unfinished ones', () => {
    expect(outcomeLabel('SELF_RECOVERED')).toContain('without a recorded intervention');
    expect(outcomeLabel('RESOLVED_UNVERIFIED')).toContain('not verified');
    expect(outcomeLabel('HARMFUL')).toContain('rolled back');
  });

  it('excludes poor-quality episodes from mining and says why', () => {
    expect(isMineable(experience())).toBe(true);
    expect(isMineable(experience({ data_quality: 'POOR' }))).toBe(false);
    expect(dataQualityLabel('POOR')).toContain('excluded');
    expect(dataQualityStyle('POOR')).not.toBe(dataQualityStyle('OK'));
  });

  it('labels every timeline stage the API can return', () => {
    expect(timelineStageLabel('detection')).toBe('Detected');
    expect(timelineStageLabel('outcome')).toBe('Outcome');
    expect(timelineStageLabel('something_new')).toBe('something new');
  });
});

describe('effectiveness', () => {
  it('states counts over the sample rather than a promise', () => {
    expect(effectivenessHeadline(bucket())).toBe(
      '34 of 42 comparable historical cases for RESTART_SERVICE were successful.'
    );
  });

  it('refuses to describe a rate from a thin sample', () => {
    const thin = bucket({ comparable: 2, successful: 2, insufficient: true, minimum_samples: 5 });
    const headline = effectivenessHeadline(thin);
    expect(headline).toContain('Only 2 comparable cases');
    expect(headline).toContain('below the minimum of 5');
    expect(headline).not.toContain('%');
  });

  it('says there is no comparable case instead of showing a zero rate', () => {
    expect(effectivenessHeadline(bucket({ comparable: 0, successful: 0 }))).toContain(
      'No comparable historical cases'
    );
  });

  it('renders a comparison as observational, never as a tie', () => {
    expect(comparisonVerdictLabel('INSUFFICIENT_EVIDENCE')).toContain('Not enough');
    expect(comparisonVerdictLabel('NO_MEANINGFUL_DIFFERENCE')).toContain('similarly');
    const favoured = comparisonVerdictLabel('FAVOURS_RESTART_SERVICE');
    expect(favoured).toContain('RESTART SERVICE');
    expect(favoured).toContain('not a demonstrated effect');
  });
});

describe('sorting and filtering', () => {
  it('orders knowledge by confidence then sample size', () => {
    const items = [
      knowledge({ id: 'low', confidence: 'LOW', sample_count: 50 }),
      knowledge({ id: 'high', confidence: 'HIGH', sample_count: 5 }),
      knowledge({ id: 'medium', confidence: 'MEDIUM', sample_count: 12 }),
    ];
    expect(sortKnowledge(items).map((item) => item.id)).toEqual([
      'high',
      'medium',
      'low',
    ]);
  });

  it('hides retired patterns unless they are asked for', () => {
    const items = [
      knowledge({ id: 'live', status: 'ACTIVE' }),
      knowledge({ id: 'dead', status: 'DEPRECATED' }),
    ];
    expect(filterPatterns(items).map((item) => item.id)).toEqual(['live']);
    expect(filterPatterns(items, { includeRetired: true })).toHaveLength(2);
  });

  it('filters by pattern type', () => {
    const items = [
      knowledge({ id: 'rem', knowledge_type: 'REMEDIATION_PATTERN' }),
      knowledge({ id: 'fail', knowledge_type: 'FAILURE_PATTERN' }),
    ];
    expect(
      filterPatterns(items, { type: 'FAILURE_PATTERN' }).map((item) => item.id)
    ).toEqual(['fail']);
    expect(filterPatterns(items, { type: 'ALL' })).toHaveLength(2);
  });

  it('puts open recommendations first, newest first among them', () => {
    const items = [
      recommendation({ id: 'old-open', status: 'OPEN', created_at: '2026-09-01T00:00:00Z' }),
      recommendation({ id: 'new-open', status: 'OPEN', created_at: '2026-09-02T00:00:00Z' }),
      recommendation({ id: 'closed', status: 'DISMISSED', created_at: '2026-09-03T00:00:00Z' }),
    ];
    expect(sortRecommendations(items).map((item) => item.id)).toEqual([
      'new-open',
      'old-open',
      'closed',
    ]);
  });
});

describe('health', () => {
  it('says when learning is off rather than showing an empty dashboard', () => {
    expect(
      intelligenceHealthLabel({
        learning_enabled: false,
        sweep_enabled: false,
        auto_activation_enabled: false,
        pending_events: 0,
      })
    ).toContain('disabled');
  });

  it('reports the activation policy in force', () => {
    const text = intelligenceHealthLabel({
      learning_enabled: true,
      sweep_enabled: true,
      auto_activation_enabled: false,
      pending_events: 3,
    });
    expect(text).toContain('3 events waiting');
    expect(text).toContain('human review');
  });
});

function relationship(
  overrides: Partial<LearnedRelationshipItem> = {}
): LearnedRelationshipItem {
  return {
    id: 'r1',
    project_id: 'p1',
    source_component_id: 'c1',
    source_component_name: 'checkout',
    target_component_id: 'c2',
    target_component_name: 'inventory',
    kind: 'SHARED_FAILURE',
    directed: false,
    status: 'ACTIVE',
    sample_count: 4,
    supporting_count: 4,
    support_strength: 1,
    confidence: 'LOW',
    evidence: [],
    limitations: [HISTORICAL_RELATIONSHIP_FALLBACK],
    provenance: 'OBSERVABILITY',
    algorithm: 'relationship-miner',
    algorithm_version: '1.0',
    feature_schema_version: '1.0',
    is_dependency: false,
    disclaimer: HISTORICAL_RELATIONSHIP_FALLBACK,
    ...overrides,
  };
}

describe('learned relationships', () => {
  it('never draws an arrow for a co-occurrence', () => {
    const shared = relationship();
    expect(mayDrawArrow(shared)).toBe(false);
    expect(relationshipHeadline(shared)).toContain('failed in the same 4 episodes');
    expect(relationshipHeadline(shared)).not.toContain('→');
  });

  it('never draws an arrow when the payload says it is a dependency', () => {
    //: The API always sends is_dependency false; if that ever changes, the
    //: viewer must stop treating the edge as history rather than silently draw it.
    const directed = relationship({ directed: true, is_dependency: true as never });
    expect(mayDrawArrow(directed)).toBe(false);
  });

  it('describes a directed edge with its direction source in the wording', () => {
    const propagation = relationship({
      kind: 'FAILURE_PROPAGATION',
      directed: true,
    });
    expect(mayDrawArrow(propagation)).toBe(true);
    expect(relationshipHeadline(propagation)).toContain('coincided with');
    expect(relationshipHeadline(propagation)).not.toContain('caused');
  });

  it('carries the historical disclaimer verbatim', () => {
    expect(relationshipDisclaimer(relationship())).toBe(
      HISTORICAL_RELATIONSHIP_FALLBACK
    );
    //: And falls back to it rather than rendering nothing.
    expect(
      relationshipDisclaimer(relationship({ disclaimer: '' }))
    ).toContain('not a dependency');
  });

  it('groups an undirected edge on both sides of a component', () => {
    const shared = relationship();
    const grouped = relationshipsForComponent([shared], 'c1');
    expect(grouped.undirected).toHaveLength(1);
    expect(grouped.outbound).toHaveLength(0);
    expect(grouped.inbound).toHaveLength(0);
  });

  it('sorts by support and then by recency', () => {
    const low = relationship({ id: 'a', sample_count: 2 });
    const high = relationship({ id: 'b', sample_count: 9 });
    expect(sortRelationships([low, high]).map((r) => r.id)).toEqual(['b', 'a']);
  });

  it('says nothing was learned rather than implying health', () => {
    expect(
      relationshipSummary({ relationships_active: 0, relationships_stale: 0 })
    ).toContain('No component relationship has been learned');
    expect(
      relationshipSummary({
        relationships_active: 3,
        relationships_undirected: 2,
        relationships_stale: 1,
      })
    ).toContain('2 of them undirected');
  });
});

describe('language rules', () => {
  it('never claims a cause or a prediction anywhere in the helpers', () => {
    const rendered = [
      knowledgeStatusLabel('ACTIVE'),
      confidenceLabel('HIGH'),
      scopeLabel('PROJECT_LEVEL'),
      outcomeLabel('EFFECTIVE'),
      effectivenessHeadline(bucket()),
      comparisonVerdictLabel('FAVOURS_RESTART_SERVICE'),
      ...rankingRows({ evidence_strength: 0.3 }).map((row) => row.value),
      decisionAndOutcome(recommendation()).outcome,
    ]
      .join(' ')
      .toLowerCase();

    expect(rendered).not.toContain('will fail');
    expect(rendered).not.toContain('caused by');
    expect(rendered).not.toContain('guarantee');
    expect(rendered).not.toContain('proves');
    expect(rendered).not.toContain('always');
  });

  it('states the no-history case as a finding rather than an error', () => {
    expect(NO_HISTORY_NOTE).toContain('That is a finding, not a failure');
  });
});
