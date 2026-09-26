/**
 * Presentation helpers for Phase 7 — fix generation & verification.
 *
 * The rules that shape this module are the phase's own guarantees:
 *
 * 1. **A generated patch is not a fixed bug.** `GENERATED` and `VERIFIED` are
 *    rendered as fundamentally different states, and the disclaimer sits with
 *    every patch, not in a footnote.
 * 2. **The verification ladder is shown, not summarised.** Each rung reports
 *    its own status, so a patch that passed tests but never demonstrated the
 *    failure cannot look "mostly verified".
 * 3. **A refusal is a result.** Tampering findings, out-of-scope paths and
 *    regressions render with the same weight as successes — they are why the
 *    system exists.
 * 4. **Nothing here claims deployment.** There is no merge, release or promote
 *    affordance anywhere in Phase 7 (§74); approval only records a decision.
 */

import type {
  FixHypothesis,
  Patch,
  PatchDetail,
  PatchTestRun,
  PatchVerificationRun,
  ReviewAction,
  ReviewState,
} from './api-client';

// ---------------------------------------------------------------------------
// Disclaimer
// ---------------------------------------------------------------------------

/** Always shown with a patch — the phase's central caveat (§1). */
export const PATCH_DISCLAIMER =
  'A generated patch is a suggestion, not a fix. It becomes a verified fix only ' +
  'when the original failure was reproduced on the baseline, the regression test ' +
  'failed before the patch and passed after it, the patch passed the repository\'s ' +
  'own checks, and no unacceptable regression was detected.';

/** §71, §74 — the end of the line for this phase. */
export const REVIEW_BOUNDARY_NOTE =
  'Phase 7 stops at human review. Approving records a decision; ARGUS never ' +
  'merges, deploys or otherwise modifies a production system.';

// ---------------------------------------------------------------------------
// §66 — the flow this phase implements
// ---------------------------------------------------------------------------

export const FIX_FLOW = [
  'Incident',
  'RCA',
  'AI Debugger',
  'Fix Hypothesis',
  'Patch',
  'Safety Validation',
  'Build & Tests',
  'Regression Test',
  'Reproduction',
  'Verification',
  'Human Review',
] as const;

// ---------------------------------------------------------------------------
// Status vocabulary (§1, §7, §26, §64)
// ---------------------------------------------------------------------------

const NEUTRAL = 'bg-slate-800 text-slate-300';
const IN_FLIGHT = 'bg-argus-info/20 text-argus-info';
const GOOD = 'bg-argus-success/20 text-argus-success';
const BAD = 'bg-argus-error/20 text-argus-error';
const WARN = 'bg-argus-warning/20 text-argus-warning';

export function patchStatusStyle(status: Patch['status']): string {
  switch (status) {
    case 'VERIFIED':
      return GOOD;
    case 'GENERATED':
    case 'APPLIED':
      return IN_FLIGHT;
    case 'PARSE_FAILED':
    case 'VALIDATION_FAILED':
    case 'BUILD_FAILED':
    case 'TEST_FAILED':
    case 'REPRODUCTION_FAILED':
    case 'GENERATION_FAILED':
      return BAD;
    case 'REJECTED':
    case 'SUPERSEDED':
      return WARN;
    default:
      return NEUTRAL;
  }
}

export function verificationStatusStyle(
  status: PatchVerificationRun['status']
): string {
  switch (status) {
    case 'VERIFIED':
      return GOOD;
    case 'RUNNING':
    case 'PENDING':
      return IN_FLIGHT;
    case 'NOT_VERIFIED':
    case 'FAILED':
      return BAD;
    case 'CANCELLED':
      return WARN;
    default:
      return NEUTRAL;
  }
}

export function levelStyle(level: PatchVerificationRun['level']): string {
  return level === 'FULLY_VERIFIED' ? GOOD : NEUTRAL;
}

export function riskStyle(risk: string): string {
  switch (risk) {
    case 'LOW':
      return 'bg-argus-success/20 text-argus-success';
    case 'MEDIUM':
      return WARN;
    case 'HIGH':
    case 'CRITICAL':
      return BAD;
    default:
      return NEUTRAL;
  }
}

export function tamperingStyle(flag: PatchVerificationRun['tampering_flag']): string {
  return flag === 'NONE' ? NEUTRAL : BAD;
}

/** Human label for a tampering flag — never softened into "warning". */
export function tamperingLabel(flag: PatchVerificationRun['tampering_flag']): string {
  const labels: Record<string, string> = {
    NONE: 'No tampering detected',
    TEST_DELETED: 'A test was deleted',
    ASSERTION_WEAKENED: 'An assertion was weakened',
    TEST_SKIPPED: 'A test was skipped',
    EXPECTATION_CHANGED: 'Test expectations were changed',
    LINT_DISABLED: 'Linting was disabled',
    TYPECHECK_DISABLED: 'Type checking was disabled',
    CI_MODIFIED: 'CI configuration was modified',
    VERIFICATION_MODIFIED: 'Verification tooling was modified',
  };
  return labels[flag] ?? flag;
}

/** §57 — the failure classes a generator can end in, each with its meaning. */
export function generationFailureNote(patch: Patch): string | null {
  if (!patch.failure_reason) {
    return patch.status === 'GENERATION_FAILED'
      ? 'Generation failed and no patch was fabricated (§57).'
      : null;
  }
  return patch.failure_reason;
}

// ---------------------------------------------------------------------------
// §43 — the diff viewer
// ---------------------------------------------------------------------------

export type DiffLineKind = 'context' | 'addition' | 'deletion';

export interface DiffLine {
  kind: DiffLineKind;
  content: string;
  /** 1-based line number on the base side; absent for additions. */
  oldLine?: number;
  /** 1-based line number on the patched side; absent for deletions. */
  newLine?: number;
}

export interface DiffHunk {
  header: string;
  lines: DiffLine[];
}

export interface DiffFile {
  path: string;
  additions: number;
  deletions: number;
  hunks: DiffHunk[];
}

const HUNK_HEADER = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/**
 * Parse a unified diff for display.
 *
 * Deliberately tolerant: a patch that has not been through the backend parser
 * (or one stored from an older format) still renders as much as it can rather
 * than throwing inside a server component.
 */
export function parseUnifiedDiff(text: string): DiffFile[] {
  const files: DiffFile[] = [];
  let current: DiffFile | null = null;
  let hunk: DiffHunk | null = null;
  let oldLine = 0;
  let newLine = 0;

  for (const raw of (text ?? '').split('\n')) {
    if (raw.startsWith('--- ')) {
      continue; // the base-side header; the patched side names the file
    }
    if (raw.startsWith('+++ ')) {
      const path = raw.slice(4).replace(/^b\//, '').trim();
      current = { path, additions: 0, deletions: 0, hunks: [] };
      files.push(current);
      hunk = null;
      continue;
    }
    if (raw.startsWith('diff --git ')) {
      const parts = raw.split(' ');
      const path = (parts[3] ?? '').replace(/^b\//, '');
      current = { path, additions: 0, deletions: 0, hunks: [] };
      files.push(current);
      hunk = null;
      continue;
    }
    const header = HUNK_HEADER.exec(raw);
    if (header) {
      oldLine = Number(header[1]);
      newLine = Number(header[2]);
      if (current) {
        hunk = { header: raw, lines: [] };
        current.hunks.push(hunk);
      }
      continue;
    }
    if (!current || !hunk) {
      continue;
    }
    if (raw.startsWith('\\')) {
      continue; // "\ No newline at end of file"
    }
    const marker = raw.charAt(0);
    const content = raw.slice(1);
    if (marker === '+') {
      hunk.lines.push({ kind: 'addition', content, newLine });
      current.additions += 1;
      newLine += 1;
    } else if (marker === '-') {
      hunk.lines.push({ kind: 'deletion', content, oldLine });
      current.deletions += 1;
      oldLine += 1;
    } else {
      hunk.lines.push({ kind: 'context', content, oldLine, newLine });
      oldLine += 1;
      newLine += 1;
    }
  }
  return files;
}

/** Every path a diff touches, in file order. */
export function diffPaths(text: string): string[] {
  return parseUnifiedDiff(text).map((file) => file.path);
}

/** A one-line summary of a patch's size (§9 minimality). */
export function patchSizeSummary(patch: Patch): string {
  return (
    `${patch.changed_files} file${patch.changed_files === 1 ? '' : 's'} changed, ` +
    `+${patch.lines_added} −${patch.lines_removed}`
  );
}

/**
 * §9 — measurement bands. Large patches are *reported as large*, never hidden;
 * the phase prefers two files over twenty-five.
 */
export function sizeBand(patch: Patch): { label: string; style: string } {
  const touched = patch.changed_files;
  if (touched <= 1) {
    return { label: 'Minimal', style: GOOD };
  }
  if (touched <= 3) {
    return { label: 'Focused', style: NEUTRAL };
  }
  if (touched <= 10) {
    return { label: 'Broad', style: WARN };
  }
  return { label: 'Very broad — review carefully', style: BAD };
}

/** §10 — files changed outside the hypothesis's allowlist. Never silent. */
export function outOfScopePaths(
  patch: Patch,
  hypothesis: FixHypothesis | null
): string[] {
  if (!hypothesis || hypothesis.scope_files.length === 0) {
    return [];
  }
  const allowed = new Set(
    hypothesis.scope_files.map((item) => item.trim().replace(/\/$/, ''))
  );
  return patch.affected_paths.filter(
    (path) => !allowed.has(path.trim().replace(/\/$/, ''))
  );
}

// ---------------------------------------------------------------------------
// §44, §68 — the verification timeline / dashboard
// ---------------------------------------------------------------------------

export type StageStatus = 'PASS' | 'FAIL' | 'SKIPPED' | 'NOT_RUN' | 'UNKNOWN';

export interface VerificationStage {
  key: string;
  title: string;
  status: StageStatus;
  detail: string;
  /** The raw command/evedence rows behind this stage, for the detail view. */
  runs: PatchTestRun[];
}

export function stageStatusStyle(status: StageStatus): string {
  switch (status) {
    case 'PASS':
      return GOOD;
    case 'FAIL':
      return BAD;
    case 'SKIPPED':
    case 'NOT_RUN':
      return NEUTRAL;
    default:
      return WARN;
  }
}

function runStatus(run: PatchTestRun): StageStatus {
  if (run.timed_out) {
    return 'FAIL';
  }
  if (run.exit_code == null) {
    return run.unknown_configuration ? 'SKIPPED' : 'UNKNOWN';
  }
  if (run.exit_code === 0) {
    return 'PASS';
  }
  return 'FAIL';
}

function rollup(runs: PatchTestRun[]): StageStatus {
  if (runs.length === 0) {
    return 'NOT_RUN';
  }
  const statuses = runs.map(runStatus);
  if (statuses.includes('FAIL')) {
    return 'FAIL';
  }
  if (statuses.includes('PASS')) {
    return 'PASS';
  }
  if (statuses.includes('SKIPPED')) {
    return 'SKIPPED';
  }
  return 'UNKNOWN';
}

/**
 * Build the §44 timeline from the *stored* evidence — the ladder the engine
 * actually walked, not the one the UI hopes it walked.
 */
export function verificationTimeline(
  run: PatchVerificationRun | null
): VerificationStage[] {
  const evidence = (run?.evidence ?? {}) as Record<string, unknown>;
  const tests = (run?.test_runs ?? []).filter((item) => item.kind === 'STATIC');
  const build = (run?.test_runs ?? []).filter((item) => item.kind === 'BUILD');
  const unit = (run?.test_runs ?? []).filter((item) => item.kind === 'UNIT');
  const regression = (run?.test_runs ?? []).filter(
    (item) => item.kind === 'REGRESSION'
  );

  const applied = Boolean(evidence.applied);
  const regressionSummary = (evidence.regression ?? {}) as Record<string, unknown>;
  const reproduction = (evidence.reproduction ?? {}) as Record<string, unknown>;
  const comparison = (evidence.comparison ?? {}) as Record<string, unknown>;
  const regressions = (comparison.regressions ?? []) as string[];

  const baselineReproduced = reproduction.baseline_reproduced;
  const patchedReproduced = reproduction.patched_reproduced;

  const stages: VerificationStage[] = [
    {
      key: 'patch_generated',
      title: 'Patch generated',
      status: run ? 'PASS' : 'NOT_RUN',
      detail: run ? 'a patch exists and was stored' : 'no patch stored yet',
      runs: [],
    },
    {
      key: 'patch_applied',
      title: 'Patch applied',
      status: applied ? 'PASS' : run ? 'FAIL' : 'NOT_RUN',
      detail: applied
        ? `applied to ${(evidence.applied_files as string[] | undefined)?.length ?? 0} entr${
            (evidence.applied_files as string[] | undefined)?.length === 1 ? 'y' : 'ies'
          }`
        : 'the patch was not applied to a workspace',
      runs: [],
    },
    {
      key: 'static',
      title: 'Static checks',
      status: rollup(tests),
      detail:
        tests.length === 0
          ? 'no static command detected in this repository'
          : `${tests.length} static command(s)`,
      runs: tests,
    },
    {
      key: 'build',
      title: 'Build',
      status: run ? (build.length === 0 ? 'SKIPPED' : rollup(build)) : 'NOT_RUN',
      detail:
        build.length === 0
          ? 'the repository declares no build step'
          : `(${build[0]?.command_key}) build ran`,
      runs: build,
    },
    {
      key: 'tests',
      title: 'Existing tests',
      status: rollup(unit),
      detail:
        unit.length === 0
          ? 'no test command detected'
          : `${(unit[0]?.selected_tests ?? []).length} selected test file(s)`,
      runs: unit,
    },
    {
      key: 'regression',
      title: 'Regression test',
      status:
        regressionSummary.verdict === 'VALID'
          ? 'PASS'
          : regressionSummary.verdict
            ? 'FAIL'
            : 'NOT_RUN',
      detail:
        regressionSummary.verdict === 'VALID'
          ? 'failed on the base commit, passed on the patched commit (§29)'
          : ((regressionSummary.invalid_reason as string | undefined) ??
            'no regression test ran'),
      runs: regression,
    },
    {
      key: 'baseline_reproduction',
      title: 'Baseline reproduction',
      status: baselineReproduced === true ? 'PASS' : baselineReproduced === false ? 'FAIL' : 'NOT_RUN',
      detail:
        baselineReproduced === true
          ? 'the original failure was reproduced against the unpatched code'
          : 'the original failure was never reproduced — nothing to verify against (§33)',
      runs: [],
    },
    {
      key: 'patched_reproduction',
      title: 'Patched reproduction',
      status:
        patchedReproduced === false
          ? 'PASS'
          : patchedReproduced === true
            ? 'FAIL'
            : 'NOT_RUN',
      detail:
        patchedReproduced === true
          ? 'the original failure still reproduces after the patch (§63)'
          : patchedReproduced === false
            ? 'the original failure no longer reproduces'
            : 'not observed',
      runs: [],
    },
    {
      key: 'comparison',
      title: 'Regression detection',
      status:
        regressions.length === 0 && Object.keys(comparison).length > 0
          ? 'PASS'
          : regressions.length > 0
            ? 'FAIL'
            : 'NOT_RUN',
      detail:
        regressions.length > 0
          ? regressions.join('; ')
          : Object.keys(comparison).length > 0
            ? 'every compared dimension stays inside its threshold'
            : 'no before/after metrics were compared',
      runs: [],
    },
    {
      key: 'verification',
      title: 'Final verification',
      status:
        run?.status === 'VERIFIED' ? 'PASS' : run ? 'FAIL' : 'NOT_RUN',
      detail: run?.verdict_reason ?? 'verification has not run',
      runs: [],
    },
  ];
  return stages;
}

/**
 * §64 — the checklist, derived from the timeline so the two cannot disagree.
 *
 * Only stages that were genuinely evaluated appear: a repository with no build
 * step has no build line, rather than a tick for a step that never ran.
 */
export function verificationChecks(
  run: PatchVerificationRun | null
): Array<{ label: string; ok: boolean; detail: string }> {
  return verificationTimeline(run)
    .filter((stage) => stage.status === 'PASS' || stage.status === 'FAIL')
    .map((stage) => ({
      label: stage.title,
      ok: stage.status === 'PASS',
      detail: stage.detail,
    }));
}

// ---------------------------------------------------------------------------
// §69 — the fix explanation
// ---------------------------------------------------------------------------

export interface ExplanationRow {
  question: string;
  answer: string;
}

/**
 * The explanation block (§69). Every row answers a specific question; a row
 * with no stored answer says so rather than being dropped — a gap in the
 * reasoning is itself information.
 */
export function explanationRows(
  patch: Patch | null,
  hypothesis: FixHypothesis | null,
  verification: PatchVerificationRun | null
): ExplanationRow[] {
  const explanation = (patch?.explanation ?? {}) as Record<string, unknown>;
  const evidence = (explanation.evidence ?? []) as string[];
  const expected = (explanation.expected_behavior ?? []) as string[];
  const unchanged = (explanation.unchanged_behavior as string | undefined) ?? null;
  const regression = verification?.regression_tests?.[0] ?? null;
  const tests = (verification?.test_runs ?? []).filter(
    (item) => item.kind === 'UNIT'
  );

  return [
    {
      question: 'Why was this fix generated?',
      answer:
        (explanation.why_changed as string) ||
        hypothesis?.proposed_change ||
        'No reasoning was stored for this patch.',
    },
    {
      question: 'Which evidence supports it?',
      answer:
        evidence.length > 0
          ? evidence.join(' · ')
          : 'No evidence reference was recorded for this patch.',
    },
    {
      question: 'Which code path does it modify?',
      answer:
        (patch?.affected_paths ?? []).join(', ') ||
        'No file path was recorded.',
    },
    {
      question: 'What behaviour should change?',
      answer:
        expected.length > 0
          ? expected.join(' · ')
          : (explanation.what_changed as string) || 'Not stated.',
    },
    {
      question: 'What behaviour should remain unchanged?',
      answer: unchanged || 'Not stated.',
    },
    {
      question: 'What tests validate it?',
      answer:
        tests.length > 0
          ? tests
              .flatMap((item) => item.selected_tests)
              .filter(Boolean)
              .join(', ') || 'Test command ran without an explicit selection.'
          : 'No test run was recorded.',
    },
    {
      question: 'What reproduction validates it?',
      answer: regression
        ? `${regression.file_path} — ${
            regression.valid
              ? 'failed on base, passed on the patched tree'
              : (regression.invalid_reason ?? 'not a valid regression test')
          }`
        : 'No reproduction-derived regression test was stored.',
    },
  ];
}

// ---------------------------------------------------------------------------
// §71 — review legality
// ---------------------------------------------------------------------------

/** True only when the stored verification genuinely verified the patch. */
export function isApprovable(
  patch: Patch | null,
  verification: PatchVerificationRun | null
): boolean {
  if (!patch || !verification) {
    return false;
  }
  return patch.status === 'VERIFIED' && verification.status === 'VERIFIED';
}

/**
 * Which actions the UI may offer (§71). A patch that was never verified offers
 * no approval — the backend would refuse it, and offering the button anyway
 * would teach the engineer that the gate is decorative.
 */
export function allowedReviewActions(
  patch: Patch | null,
  verification: PatchVerificationRun | null,
  state: ReviewState | null
): ReviewAction[] {
  if (!patch) {
    return [];
  }
  const terminal = state === 'APPROVED' || state === 'REJECTED';
  if (terminal) {
    return [];
  }
  const actions: ReviewAction[] = [];
  if (isApprovable(patch, verification)) {
    actions.push('APPROVE');
  }
  //: Only actions the API actually exposes. A "request changes" button with
  //: no endpoint behind it would be a fake control.
  actions.push('REJECT', 'REGENERATE');
  return actions;
}

export function reviewActionStyle(action: ReviewAction): string {
  switch (action) {
    case 'APPROVE':
      return 'btn bg-argus-success/20 text-argus-success hover:bg-argus-success/30';
    case 'REJECT':
      return 'btn bg-argus-error/20 text-argus-error hover:bg-argus-error/30';
    case 'REGENERATE':
      return 'btn-ghost';
    default:
      return 'btn-ghost';
  }
}

export function reviewStateStyle(state: ReviewState | null): string {
  switch (state) {
    case 'APPROVED':
      return GOOD;
    case 'REJECTED':
      return BAD;
    case 'CHANGES_REQUESTED':
      return WARN;
    case 'AWAITING_REVIEW':
      return IN_FLIGHT;
    default:
      return NEUTRAL;
  }
}

/** §70 — the audit trail, oldest first (the order it happened in). */
export function auditTrail(patch: PatchDetail | null): Array<{
  at: string;
  actor: string;
  action: string;
  reason: string | null;
}> {
  return (patch?.review_actions ?? []).map((item) => ({
    at: item.created_at,
    actor: item.actor,
    action: item.action as string,
    reason: item.reason ?? null,
  }));
}

// ---------------------------------------------------------------------------
// §67 — dashboard filters
// ---------------------------------------------------------------------------

export const FIX_STATUS_FILTERS = [
  'HYPOTHESIZED',
  'PATCH_GENERATED',
  'GENERATION_FAILED',
  'REJECTED',
  'SUPERSEDED',
] as const;

export const PATCH_STATUS_FILTERS = [
  'GENERATED',
  'APPLIED',
  'VERIFIED',
  'VALIDATION_FAILED',
  'PARSE_FAILED',
  'BUILD_FAILED',
  'TEST_FAILED',
  'REPRODUCTION_FAILED',
] as const;

/** The category label, never expanded into an interpretation (§6). */
export function categoryLabel(category: FixHypothesis['category']): string {
  return category.replace(/_/g, ' ');
}
