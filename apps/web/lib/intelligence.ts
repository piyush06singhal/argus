/**
 * Presentation helpers for Phase 10 — reliability intelligence.
 *
 * The rules that shape this module are the phase's own guarantees, and each one
 * exists because the alternative is a UI that lies by omission:
 *
 * 1. **A learned pattern is not a rule** (§3). Every pattern renders with its
 *    sample count and its scope; "restart works" never appears without "over 34
 *    comparable cases in this project".
 * 2. **Small samples are labelled as small** (§7, §33). A pattern below the
 *    validation floor renders as a candidate with its count, never as a finding.
 * 3. **A recommendation is not an execution** (§39, §42). The policy note is
 *    rendered next to the advice so "approval required" is never discovered
 *    after someone tries to act.
 * 4. **Acceptance is not success** (§81). Decisions and outcomes render as
 *    separate facts, and an accepted recommendation with no outcome says so.
 * 5. **No comparable case is an answer** (§49). The empty state says exactly
 *    that, and never fills the space with a plausible-sounding explanation.
 * 6. **Retired knowledge is shown as retired** (§25). A deprecated pattern is
 *    never displayed among live ones.
 */

import type {
  EffectivenessBucket,
  ExperienceItem,
  KnowledgeConfidenceValue,
  KnowledgeItem,
  KnowledgeScopeValue,
  KnowledgeStatusValue,
  KnowledgeTypeValue,
  LearnedRelationshipItem,
  RelationshipKindValue,
  RecommendationItem,
  RecommendationTypeValue,
} from './api-client';

export type { KnowledgeStatusValue, KnowledgeTypeValue };

// ---------------------------------------------------------------------------
// The phase's boundary, stated once and reused everywhere (§3, §74)
// ---------------------------------------------------------------------------

export const LEARNING_BOUNDARY =
  'ARGUS learns from stored history and produces knowledge, evaluations and ' +
  'recommendations. It does not modify its own safety policy, authorization or ' +
  'verification logic, and a recommendation never executes anything by itself.';

export const OBSERVATIONAL_NOTE =
  'An association observed in this project\u2019s history. It is not a causal claim.';

export const NO_HISTORY_NOTE =
  'No comparable historical case was found. That is a finding, not a failure: it ' +
  'means ARGUS has nothing to base a claim on.';

// ---------------------------------------------------------------------------
// Knowledge status (§4)
// ---------------------------------------------------------------------------

/** §4. Only these two may influence a recommendation. */
export const LIVE_STATUSES: readonly KnowledgeStatusValue[] = ['VALIDATED', 'ACTIVE'];

/** §4, §25. Retired statuses are history and are labelled as such. */
export const RETIRED_STATUSES: readonly KnowledgeStatusValue[] = [
  'DEPRECATED',
  'REJECTED',
  'SUPERSEDED',
];

const STATUS_STYLES: Record<KnowledgeStatusValue, string> = {
  CANDIDATE: 'bg-slate-800 text-slate-300',
  VALIDATING: 'bg-argus-info/20 text-argus-info',
  VALIDATED: 'bg-argus-success/15 text-argus-success',
  ACTIVE: 'bg-argus-success/25 text-argus-success',
  DEPRECATED: 'bg-argus-warning/20 text-argus-warning',
  REJECTED: 'bg-argus-error/20 text-argus-error',
  SUPERSEDED: 'bg-slate-800 text-slate-400',
};

export function knowledgeStatusStyle(status: KnowledgeStatusValue): string {
  return STATUS_STYLES[status] ?? 'bg-slate-800 text-slate-300';
}

export function isLive(status: KnowledgeStatusValue): boolean {
  return LIVE_STATUSES.includes(status);
}

export function isRetired(status: KnowledgeStatusValue): boolean {
  return RETIRED_STATUSES.includes(status);
}

/**
 * What the status means for the reader.
 *
 * The distinction that matters: CANDIDATE is visible on purpose (§53) but it is
 * not believed, and the label has to say so or it reads as a finding.
 */
export function knowledgeStatusLabel(status: KnowledgeStatusValue): string {
  switch (status) {
    case 'CANDIDATE':
      return 'Candidate — not yet validated';
    case 'VALIDATING':
      return 'Under validation';
    case 'VALIDATED':
      return 'Validated — may inform recommendations';
    case 'ACTIVE':
      return 'Active — may inform recommendations';
    case 'DEPRECATED':
      return 'Deprecated — no longer confirmed by new data';
    case 'REJECTED':
      return 'Rejected by review';
    case 'SUPERSEDED':
      return 'Superseded — kept for history';
    default:
      return status;
  }
}

// ---------------------------------------------------------------------------
// Confidence and sample size (§7, §33, §34)
// ---------------------------------------------------------------------------

const CONFIDENCE_STYLES: Record<KnowledgeConfidenceValue, string> = {
  UNKNOWN: 'bg-slate-800 text-slate-400',
  LOW: 'bg-argus-warning/15 text-argus-warning',
  MEDIUM: 'bg-argus-info/20 text-argus-info',
  HIGH: 'bg-argus-success/15 text-argus-success',
};

export function confidenceStyle(confidence: KnowledgeConfidenceValue): string {
  return CONFIDENCE_STYLES[confidence] ?? CONFIDENCE_STYLES.UNKNOWN;
}

export function confidenceLabel(confidence: KnowledgeConfidenceValue): string {
  switch (confidence) {
    case 'UNKNOWN':
      return 'Confidence unknown — evidence was insufficient to judge';
    case 'HIGH':
      return 'High confidence';
    case 'MEDIUM':
      return 'Medium confidence';
    case 'LOW':
      return 'Low confidence';
    default:
      return String(confidence);
  }
}

/**
 * §15. "34 of 42 comparable cases were successful."
 *
 * A count over a stated sample, never a bare percentage and never a promise. A
 * missing or zero sample says so instead of printing 0%.
 */
export function sampleLabel(
  success: number | null | undefined,
  comparable: number | null | undefined
): string {
  const total = comparable ?? 0;
  if (total <= 0) {
    return 'No comparable cases recorded.';
  }
  if (success === null || success === undefined) {
    return `${total} comparable case${total === 1 ? '' : 's'}.`;
  }
  return `${success} of ${total} comparable case${total === 1 ? '' : 's'} were successful.`;
}

/** The support ratio as a share, with the sample it was taken over. */
export function supportLabel(
  ratio: number | null | undefined,
  sampleCount: number | null | undefined
): string | null {
  if (ratio === null || ratio === undefined) {
    return null;
  }
  const percent = Math.round(ratio * 100);
  const sample = sampleCount ?? 0;
  if (sample <= 0) {
    return `${percent}% of an unstated sample`;
  }
  return `${percent}% of ${sample} case${sample === 1 ? '' : 's'}`;
}

/**
 * §7, §33. One pattern's sample, described without implying a rate it lacks.
 *
 * Exists as a helper because ``sampleLabel`` takes ``(success, comparable)``
 * while a knowledge item stores the two in the opposite order — passing them
 * straight through inverts the sentence, which is the kind of mistake that reads
 * as a plausible claim rather than a bug.
 */
export function knowledgeSampleLabel(
  item: Pick<KnowledgeItem, 'sample_count' | 'success_count'>
): string {
  const total = item.sample_count ?? 0;
  if (total <= 0) {
    return 'No observations recorded.';
  }
  if (item.success_count === null || item.success_count === undefined) {
    return `Observed in ${total} episode${total === 1 ? '' : 's'}.`;
  }
  return `${item.success_count} of ${total} observed episode${
    total === 1 ? '' : 's'
  } showed the described outcome.`;
}

/** §33. Below the validation floor a pattern is a candidate, and says so. */
export function isBelowValidationFloor(
  sampleCount: number,
  minimumSamples: number
): boolean {
  return sampleCount < minimumSamples;
}

// ---------------------------------------------------------------------------
// Types and scopes (§3, §37)
// ---------------------------------------------------------------------------

export const KNOWLEDGE_TYPE_LABELS: Record<KnowledgeTypeValue, string> = {
  INCIDENT_PATTERN: 'Incident pattern',
  FAILURE_PATTERN: 'Failure pattern',
  ANOMALY_PATTERN: 'Anomaly pattern',
  REMEDIATION_PATTERN: 'Remediation pattern',
  REGRESSION_PATTERN: 'Regression pattern',
  DEPENDENCY_PATTERN: 'Dependency pattern',
  DEPLOYMENT_PATTERN: 'Deployment pattern',
  RESOURCE_PATTERN: 'Resource pattern',
  PREDICTIVE_PATTERN: 'Predictive pattern',
  RECOVERY_PATTERN: 'Recovery pattern',
  COMPONENT_RELIABILITY_PATTERN: 'Component reliability pattern',
};

export function knowledgeTypeLabel(type: KnowledgeTypeValue): string {
  return KNOWLEDGE_TYPE_LABELS[type] ?? String(type);
}

/**
 * §37. How far the claim is allowed to travel.
 *
 * A project-level claim from one component is exactly the generalisation the
 * section forbids, so the scope is always rendered next to the title.
 */
export function scopeLabel(scope: KnowledgeScopeValue): string {
  switch (scope) {
    case 'COMPONENT_SPECIFIC':
      return 'This component only';
    case 'SERVICE_CLASS':
      return 'This class of service';
    case 'PROJECT_LEVEL':
      return 'This project';
    case 'CROSS_PROJECT':
      return 'Across projects';
    default:
      return String(scope);
  }
}

// ---------------------------------------------------------------------------
// Recommendations (§39, §40, §41, §42, §81)
// ---------------------------------------------------------------------------

export const RECOMMENDATION_TYPE_LABELS: Record<RecommendationTypeValue, string> = {
  INVESTIGATE_COMPONENT: 'Investigate component',
  INVESTIGATE_DEPENDENCY: 'Investigate dependency',
  REVIEW_RECENT_CHANGE: 'Review recent change',
  REVIEW_REMEDIATION: 'Review remediation',
  RUN_REPRODUCTION: 'Run reproduction',
  CONSIDER_ROLLBACK: 'Consider rollback',
  CONSIDER_RESTART: 'Consider restart',
  CONSIDER_TRAFFIC_SHIFT: 'Consider traffic shift',
  REVIEW_CAPACITY: 'Review capacity',
  REVIEW_CONFIGURATION: 'Review configuration',
};

export function recommendationTypeLabel(type: RecommendationTypeValue): string {
  return RECOMMENDATION_TYPE_LABELS[type] ?? String(type);
}

/** §39. Types that are investigations rather than an implied action. */
const INVESTIGATION_TYPES: readonly RecommendationTypeValue[] = [
  'INVESTIGATE_COMPONENT',
  'INVESTIGATE_DEPENDENCY',
  'REVIEW_RECENT_CHANGE',
  'REVIEW_REMEDIATION',
  'RUN_REPRODUCTION',
];

export function isInvestigation(type: RecommendationTypeValue): boolean {
  return INVESTIGATION_TYPES.includes(type);
}

/** §42. Whether acting on this advice would need Phase 9 to allow it. */
export function requiresPolicyAttention(recommendation: RecommendationItem): boolean {
  return Boolean(recommendation.policy_note) && !isInvestigation(recommendation.recommendation_type);
}

export function recommendationStatusStyle(status: string): string {
  switch (status) {
    case 'OPEN':
      return 'bg-argus-info/20 text-argus-info';
    case 'ACCEPTED':
      return 'bg-argus-info/25 text-argus-info';
    case 'DISMISSED':
      return 'bg-slate-800 text-slate-400';
    case 'EFFECTIVE':
      return 'bg-argus-success/15 text-argus-success';
    case 'INEFFECTIVE':
      return 'bg-argus-warning/20 text-argus-warning';
    case 'REGRESSION_CAUSING':
      return 'bg-argus-error/20 text-argus-error';
    case 'EXPIRED':
      return 'bg-slate-800 text-slate-500';
    default:
      return 'bg-slate-800 text-slate-300';
  }
}

/**
 * §81. What was decided, and separately what then happened.
 *
 * Acceptance is not correctness: the two facts are returned independently so a
 * UI cannot present an accepted card as a success.
 */
export function decisionAndOutcome(recommendation: RecommendationItem): {
  decision: string;
  outcome: string;
} {
  const decidedBy =
    typeof recommendation.decision?.actor === 'string'
      ? recommendation.decision.actor
      : null;
  const verdict =
    typeof recommendation.outcome?.verdict === 'string'
      ? recommendation.outcome.verdict
      : null;

  let decision = 'Not yet decided.';
  if (recommendation.status === 'EXPIRED') {
    decision = 'Expired before anyone decided.';
  } else if (decidedBy) {
    decision = `Recorded by ${decidedBy}.`;
  }

  let outcome = 'No outcome has been recorded yet.';
  if (verdict) {
    outcome =
      verdict === 'EFFECTIVE'
        ? 'Recorded outcome: effective.'
        : verdict === 'REGRESSION_CAUSING'
          ? 'Recorded outcome: caused a regression.'
          : `Recorded outcome: ${verdict.toLowerCase().replace(/_/g, ' ')}.`;
  }
  return { decision, outcome };
}

/** §41. The ranking criteria, shown rather than hidden. */
export function rankingRows(
  ranking: Record<string, unknown> | null | undefined
): Array<{ key: string; value: string }> {
  if (!ranking) {
    return [];
  }
  const entries = Object.entries(ranking).filter(
    ([, value]) => typeof value === 'number' || typeof value === 'string'
  );
  return entries.map(([key, value]) => ({
    key: key.replace(/_/g, ' '),
    value: typeof value === 'number' ? String(value) : String(value),
  }));
}

// ---------------------------------------------------------------------------
// Experiences (§54)
// ---------------------------------------------------------------------------

const STAGE_LABELS: Record<string, string> = {
  detection: 'Detected',
  incident: 'Incident opened',
  anomaly: 'Anomaly recorded',
  root_cause: 'Root cause analysis',
  reproduction: 'Reproduction',
  fix: 'Fix generated',
  verification: 'Patch verified',
  remediation: 'Remediation',
  outcome: 'Outcome',
};

export function timelineStageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage.replace(/_/g, ' ');
}

/** §10. The outcome as a sentence, including the honest unfinished ones. */
export function outcomeLabel(outcome: string): string {
  switch (outcome) {
    case 'EFFECTIVE':
      return 'Resolved, and the fix was verified';
    case 'PATCH_VERIFIED':
      return 'Resolved by a verified patch';
    case 'SELF_RECOVERED':
      return 'Recovered without a recorded intervention';
    case 'RESOLVED_UNVERIFIED':
      return 'Closed, but the resolution was not verified';
    case 'INEFFECTIVE':
      return 'The attempted fix did not resolve it';
    case 'HARMFUL':
      return 'The attempted fix was rolled back';
    case 'UNRESOLVED':
      return 'Never resolved';
    default:
      return outcome.replace(/_/g, ' ').toLowerCase();
  }
}

export function dataQualityStyle(quality: string): string {
  switch (quality) {
    case 'POOR':
      return 'bg-argus-error/20 text-argus-error';
    case 'LIMITED':
      return 'bg-argus-warning/20 text-argus-warning';
    default:
      return 'bg-slate-800 text-slate-300';
  }
}

/** §30. Quality controls whether an episode may be mined, so it is always shown. */
export function dataQualityLabel(quality: string): string {
  switch (quality) {
    case 'POOR':
      return 'Poor data quality — excluded from pattern mining';
    case 'LIMITED':
      return 'Limited data quality — counts with a caveat';
    default:
      return 'Data quality acceptable';
  }
}

// ---------------------------------------------------------------------------
// Effectiveness (§15, §16, §45)
// ---------------------------------------------------------------------------

/**
 * §15. The bucket as a sentence a reader cannot misquote.
 *
 * The failure modes this avoids: a percentage with no denominator, and a
 * confident rate over three cases.
 */
export function effectivenessHeadline(bucket: EffectivenessBucket): string {
  if (bucket.comparable === 0) {
    return `No comparable historical cases for ${bucket.action_type}.`;
  }
  if (bucket.insufficient) {
    return (
      `Only ${bucket.comparable} comparable case${bucket.comparable === 1 ? '' : 's'} for ` +
      `${bucket.action_type} — below the minimum of ${bucket.minimum_samples} needed to ` +
      `describe a rate.`
    );
  }
  return (
    `${bucket.successful} of ${bucket.comparable} comparable historical cases for ` +
    `${bucket.action_type} were successful.`
  );
}

/**
 * §45. Whether a comparison between two actions is even defensible.
 *
 * A verdict of INSUFFICIENT_EVIDENCE renders as "not enough evidence", never as
 * a tie, because a tie would imply the two were measured against each other.
 */
export function comparisonVerdictLabel(verdict: string): string {
  if (verdict === 'INSUFFICIENT_EVIDENCE') {
    return 'Not enough comparable cases to compare these two actions.';
  }
  if (verdict === 'NO_MEANINGFUL_DIFFERENCE') {
    return 'The two actions performed similarly in these comparable cases.';
  }
  if (verdict.startsWith('FAVOURS_')) {
    const action = verdict.slice('FAVOURS_'.length).replace(/_/g, ' ');
    return `${action} succeeded more often in comparable cases — an observational difference, not a demonstrated effect.`;
  }
  return verdict;
}

// ---------------------------------------------------------------------------
// Sorting and filtering (§52, §53, §56)
// ---------------------------------------------------------------------------

const CONFIDENCE_RANK: Record<KnowledgeConfidenceValue, number> = {
  HIGH: 3,
  MEDIUM: 2,
  LOW: 1,
  UNKNOWN: 0,
};

/**
 * §53. Order by evidence strength, then recency.
 *
 * Confidence first, because a reader scanning a list should meet the
 * best-supported pattern first, and never a single-observation candidate before
 * a well-supported one.
 */
export function sortKnowledge(items: KnowledgeItem[]): KnowledgeItem[] {
  return [...items].sort((left, right) => {
    const confidence =
      (CONFIDENCE_RANK[right.confidence] ?? 0) - (CONFIDENCE_RANK[left.confidence] ?? 0);
    if (confidence !== 0) {
      return confidence;
    }
    const sample = (right.sample_count ?? 0) - (left.sample_count ?? 0);
    if (sample !== 0) {
      return sample;
    }
    return (right.updated_at ?? '').localeCompare(left.updated_at ?? '');
  });
}

/** §56. Live patterns first; retired ones only when asked for. */
export function filterPatterns(
  items: KnowledgeItem[],
  options: { includeRetired?: boolean; type?: KnowledgeTypeValue | 'ALL' } = {}
): KnowledgeItem[] {
  return items.filter((item) => {
    if (!options.includeRetired && isRetired(item.status)) {
      return false;
    }
    if (options.type && options.type !== 'ALL' && item.knowledge_type !== options.type) {
      return false;
    }
    return true;
  });
}

const RECOMMENDATION_RANK: Record<string, number> = {
  OPEN: 0,
  ACCEPTED: 1,
  EFFECTIVE: 2,
  INEFFECTIVE: 2,
  REGRESSION_CAUSING: 2,
  DISMISSED: 3,
  EXPIRED: 4,
};

/** §57. Open advice first — the things still waiting on a person. */
export function sortRecommendations(
  items: RecommendationItem[]
): RecommendationItem[] {
  return [...items].sort((left, right) => {
    const status =
      (RECOMMENDATION_RANK[left.status] ?? 5) - (RECOMMENDATION_RANK[right.status] ?? 5);
    if (status !== 0) {
      return status;
    }
    return (right.created_at ?? '').localeCompare(left.created_at ?? '');
  });
}

/** The evidence a recommendation stands on, as readable rows. */
export function recommendationEvidence(
  recommendation: RecommendationItem
): Array<{ key: string; value: string }> {
  const rows: Array<{ key: string; value: string }> = [];
  for (const [key, value] of Object.entries(recommendation.current_evidence ?? {})) {
    if (value === null || value === undefined) {
      continue;
    }
    const rendered = Array.isArray(value)
      ? value.join(', ')
      : typeof value === 'object'
        ? JSON.stringify(value)
        : String(value);
    if (!rendered) {
      continue;
    }
    rows.push({ key: key.replace(/_/g, ' '), value: rendered });
  }
  return rows;
}

/** §13, §40. What the recommendation looked at — counts, never a bare claim. */
export function historicalSummary(
  historical: Record<string, unknown> | null | undefined
): string | null {
  if (!historical) {
    return null;
  }
  const comparable = historical.comparable_episodes;
  const successful = historical.successful_resolutions;
  if (typeof comparable !== 'number' || comparable <= 0) {
    return NO_HISTORY_NOTE;
  }
  return (
    `${comparable} comparable episode${comparable === 1 ? '' : 's'}, ` +
    `${typeof successful === 'number' ? successful : 0} of which resolved successfully.`
  );
}

/** §54. One episode, as a sentence. */
export function experienceHeadline(experience: ExperienceItem): string {
  const when = experience.start_time?.slice(0, 16).replace('T', ' ') ?? 'unknown time';
  return `${experience.failure_label || 'Unclassified failure'} — ${when} UTC`;
}

/** §30. Whether a run may mine an episode at all. */
export function isMineable(experience: ExperienceItem): boolean {
  return experience.data_quality !== 'POOR';
}

/** §80. Health as a sentence, including the "not running" cases. */
export function intelligenceHealthLabel(health: {
  learning_enabled: boolean;
  sweep_enabled: boolean;
  auto_activation_enabled: boolean;
  pending_events: number;
}): string {
  if (!health.learning_enabled) {
    return 'Learning is disabled in this deployment; no new knowledge is being formed.';
  }
  const parts = [
    health.sweep_enabled ? 'Scheduled learning is on' : 'Scheduled learning is off',
    `${health.pending_events} event${health.pending_events === 1 ? '' : 's'} waiting to be processed`,
    health.auto_activation_enabled
      ? 'low-risk patterns may activate without review'
      : 'every activation requires human review',
  ];
  return `${parts.join('; ')}.`;
}

// ---------------------------------------------------------------------------
// Learned relationships (§23, §24)
// ---------------------------------------------------------------------------

export const RELATIONSHIP_KIND_LABELS: Record<RelationshipKindValue, string> = {
  FAILURE_PROPAGATION: 'Failure propagation',
  SHARED_FAILURE: 'Shared failure',
  DEPENDENCY_DEGRADATION: 'Dependency degradation',
  REMEDIATION_INFLUENCE: 'Remediation influence',
};

export function relationshipKindLabel(kind: RelationshipKindValue): string {
  return RELATIONSHIP_KIND_LABELS[kind] ?? String(kind);
}

/**
 * §24. How the edge must be described, direction included.
 *
 * An undirected co-failure is described as co-failure. Rendering it with an
 * arrow would turn "these two components failed together" into "failures travel
 * from one to the other", which is a different and stronger claim than the data.
 */
export function relationshipHeadline(
  relationship: Pick<
    LearnedRelationshipItem,
    'directed' | 'source_component_name' | 'target_component_name' | 'kind' | 'sample_count'
  >
): string {
  const count = relationship.sample_count;
  const episodes = `${count} episode${count === 1 ? '' : 's'}`;
  switch (relationship.kind) {
    case 'SHARED_FAILURE':
      return `${relationship.source_component_name} and ${relationship.target_component_name} failed in the same ${episodes}`;
    case 'FAILURE_PROPAGATION':
      return `Failures at ${relationship.source_component_name} coincided with failures at ${relationship.target_component_name} in ${episodes}`;
    case 'DEPENDENCY_DEGRADATION':
      return `${relationship.source_component_name} degraded while ${relationship.target_component_name}, which depends on it, was affected — ${episodes}`;
    case 'REMEDIATION_INFLUENCE':
      return `Acting on ${relationship.source_component_name} coincided with ${relationship.target_component_name} recovering in ${episodes}`;
    default:
      return relationship.directed
        ? `${relationship.source_component_name} → ${relationship.target_component_name} over ${episodes}`
        : `${relationship.source_component_name} and ${relationship.target_component_name} over ${episodes}`;
  }
}

/**
 * §24. Whether this may be drawn with an arrow.
 *
 * False for a co-occurrence and for anything the API did not mark directed — the
 * client never infers direction from ordering, naming or convention.
 */
export function mayDrawArrow(
  relationship: Pick<LearnedRelationshipItem, 'directed' | 'is_dependency'>
): boolean {
  return relationship.directed === true && relationship.is_dependency === false;
}

/** §24. The disclaimer every relationship view must carry. */
export function relationshipDisclaimer(
  relationship: Pick<LearnedRelationshipItem, 'disclaimer'>
): string {
  return relationship.disclaimer || HISTORICAL_RELATIONSHIP_FALLBACK;
}

export const HISTORICAL_RELATIONSHIP_FALLBACK =
  'HISTORICAL RELATIONSHIP — learned from past episodes, not a dependency ' +
  'declared in configuration';

/**
 * §23. Relationships grouped around one component, for the component view.
 *
 * Outbound and inbound are separated, and an undirected edge appears in both —
 * it genuinely concerns both ends, and dropping it from one side would hide half
 * of what history recorded.
 */
export function relationshipsForComponent(
  relationships: LearnedRelationshipItem[],
  componentId: string
): {
  outbound: LearnedRelationshipItem[];
  inbound: LearnedRelationshipItem[];
  undirected: LearnedRelationshipItem[];
} {
  const outbound: LearnedRelationshipItem[] = [];
  const inbound: LearnedRelationshipItem[] = [];
  const undirected: LearnedRelationshipItem[] = [];
  for (const relationship of relationships) {
    const isSource = relationship.source_component_id === componentId;
    const isTarget = relationship.target_component_id === componentId;
    if (isSource && isTarget) {
      continue;
    }
    if (!mayDrawArrow(relationship)) {
      undirected.push(relationship);
      continue;
    }
    if (isSource) {
      outbound.push(relationship);
    } else if (isTarget) {
      inbound.push(relationship);
    }
  }
  return { outbound, inbound, undirected };
}

/** §23. Highest-support first, then most recently confirmed. */
export function sortRelationships(
  relationships: LearnedRelationshipItem[]
): LearnedRelationshipItem[] {
  return [...relationships].sort((a, b) => {
    if (b.sample_count !== a.sample_count) {
      return b.sample_count - a.sample_count;
    }
    return (b.last_seen_at ?? '').localeCompare(a.last_seen_at ?? '');
  });
}

/** §80. The relationship panel's summary sentence, including the empty case. */
export function relationshipSummary(counts: {
  relationships_active?: number;
  relationships_stale?: number;
  relationships_undirected?: number;
}): string {
  const active = counts.relationships_active ?? 0;
  if (active === 0) {
    return 'No component relationship has been learned yet from this project\u2019s history.';
  }
  const undirected = counts.relationships_undirected ?? 0;
  const stale = counts.relationships_stale ?? 0;
  const parts = [
    `${active} active relationship${active === 1 ? '' : 's'}`,
    `${undirected} of them undirected (co-failure only)`,
  ];
  if (stale) {
    parts.push(`${stale} no longer confirmed by new data`);
  }
  return `${parts.join('; ')}.`;
}
