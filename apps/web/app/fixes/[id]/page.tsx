import Link from 'next/link';

import {
  api,
  formatDate,
  formatDuration,
  type FixHypothesis,
  type Patch,
  type PatchDetail,
  type PatchTestRun,
  type PatchVerificationRun,
} from '@/lib/api';
import {
  allowedReviewActions,
  auditTrail,
  diffPaths,
  explanationRows,
  FIX_FLOW,
  generationFailureNote,
  levelStyle,
  outOfScopePaths,
  parseUnifiedDiff,
  PATCH_DISCLAIMER,
  patchSizeSummary,
  patchStatusStyle,
  REVIEW_BOUNDARY_NOTE,
  reviewStateStyle,
  riskStyle,
  sizeBand,
  stageStatusStyle,
  tamperingLabel,
  tamperingStyle,
  verificationChecks,
  verificationTimeline,
  verificationStatusStyle,
} from '@/lib/fixes';
import FixActions from './FixActions';

export const metadata = {
  title: 'Fix Workspace',
};

export const dynamic = 'force-dynamic';

function Card({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="card">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="font-medium text-slate-200">{title}</h2>
        {subtitle ? <p className="text-xs text-slate-500">{subtitle}</p> : null}
      </div>
      {children}
    </section>
  );
}

/**
 * The fix & verification workspace (§42–§44, §66–§70).
 *
 * The page's job is to keep three things apart that are easy to blur:
 * a patch's *status*, the verification engine's *verdict*, and the engineer's
 * *decision*. Each has its own badge and its own section, and the diff is shown
 * exactly as the backend stored it.
 */
export default async function FixWorkspacePage({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: { project_id?: string; patch?: string };
}) {
  const projectScope =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : undefined;

  let hypothesis: FixHypothesis | null = null;
  let error: string | null = null;
  try {
    hypothesis = await api.getFixHypothesis(params.id, projectScope);
  } catch (cause: unknown) {
    error = cause instanceof Error ? cause.message : 'unknown error';
  }

  if (!hypothesis) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold text-slate-100">Fix Workspace</h1>
        <Card title="Fix hypothesis not available">
          <p className="text-sm text-argus-error">
            {error ?? 'The fix hypothesis could not be loaded.'}
          </p>
          <p className="mt-2 text-xs text-slate-500">
            A hypothesis is only readable inside the project that owns it.
          </p>
        </Card>
      </div>
    );
  }

  const projectId = hypothesis.project_id;
  let patches: Patch[] = [];
  try {
    patches = (await api.listHypothesisPatches(hypothesis.id, projectId)).items;
  } catch {
    patches = [];
  }

  const requested = typeof searchParams.patch === 'string' ? searchParams.patch : '';
  const selected = patches.find((item) => item.id === requested) ?? patches[0] ?? null;

  let patch: PatchDetail | null = null;
  if (selected) {
    try {
      patch = await api.getPatch(selected.id, projectId);
    } catch {
      patch = null;
    }
  }

  const verification = patch?.verification_runs?.[0] ?? null;
  const reviewState = patch?.review_state ?? null;
  const actions = allowedReviewActions(patch, verification, reviewState);
  const excluded = patch ? outOfScopePaths(patch, hypothesis) : [];

  return (
    <div className="space-y-6">
      <div>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold text-slate-100">
            {hypothesis.title}
          </h1>
          <span className={`badge ${riskStyle(hypothesis.risk_level)}`}>
            risk {hypothesis.risk_level}
          </span>
          <span className="badge bg-slate-800 text-slate-300">
            {hypothesis.status}
          </span>
          {reviewState ? (
            <span className={`badge ${reviewStateStyle(reviewState)}`}>
              {reviewState}
            </span>
          ) : null}
        </div>
        <p className="mt-1 text-sm text-slate-400">{hypothesis.proposed_change}</p>
        <p className="mt-2 text-xs text-slate-500">
          {FIX_FLOW.join(' → ')}
        </p>
        <p className="mt-1 text-xs text-slate-500">{PATCH_DISCLAIMER}</p>
      </div>

      <Card
        title="Fix hypothesis"
        subtitle={`§5 — planned from the debug session's validated locations`}
      >
        <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
          <Row label="Category" value={hypothesis.category.replace(/_/g, ' ')} />
          <Row label="Confidence" value={hypothesis.confidence} />
          <Row label="Failure description" value={hypothesis.description} />
          <Row
            label="Expected behaviour"
            value={hypothesis.expected_behavior ?? '—'}
          />
          <Row label="Scope (allowed files)" value={hypothesis.scope_files.join(', ') || '—'} />
          <Row
            label="Excluded paths"
            value={hypothesis.excluded_paths.join(', ') || '—'}
          />
          <Row
            label="Target symbols"
            value={hypothesis.target_symbols.join(', ') || '—'}
          />
          <Row
            label="Debug session"
            value={hypothesis.debug_session_id ?? '—'}
          />
        </dl>
        <div className="mt-3 flex flex-wrap gap-3 text-xs">
          <Link href={`/incidents/${hypothesis.incident_id}`}>Incident</Link>
          {hypothesis.debug_session_id ? (
            <Link href={`/debugger/${hypothesis.debug_session_id}`}>
              Debug session
            </Link>
          ) : null}
          {hypothesis.reproduction_experiment_id ? (
            <Link
              href={`/reproductions/${hypothesis.reproduction_experiment_id}`}
            >
              Reproduction experiment
            </Link>
          ) : null}
        </div>
      </Card>

      <Card
        title="Patch candidates"
        subtitle={`${patches.length} candidate${patches.length === 1 ? '' : 's'}`}
      >
        {patches.length === 0 ? (
          <p className="text-sm text-slate-400">
            No patch has been generated for this hypothesis yet. Generation is an
            explicit action — ARGUS never writes code on its own initiative.
          </p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {patches.map((item) => (
              <Link
                key={item.id}
                href={`/fixes/${hypothesis.id}?project_id=${encodeURIComponent(
                  projectId
                )}&patch=${item.id}`}
                className={`badge ${
                  item.id === selected?.id
                    ? 'bg-argus-accent/20 text-argus-accent'
                    : 'bg-slate-800 text-slate-300'
                }`}
              >
                {item.status} · {patchSizeSummary(item)}
              </Link>
            ))}
          </div>
        )}
      </Card>

      <FixActions
        hypothesisId={hypothesis.id}
        patchId={patch?.id ?? null}
        projectId={projectId}
        allowedActions={actions}
        patchStatus={patch?.status ?? null}
        baselineFailureSignature={hypothesis.description.slice(0, 160)}
        hasVerification={verification != null}
      />

      {patch ? (
        <>
          <Card
            title="Patch"
            subtitle={`§9 — minimality is measured, not claimed`}
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className={`badge ${patchStatusStyle(patch.status)}`}>
                {patch.status}
              </span>
              <span className={`badge ${sizeBand(patch).style}`}>
                {sizeBand(patch).label}
              </span>
              <span className="badge bg-slate-800 text-slate-300">
                {patchSizeSummary(patch)}
              </span>
              <span className="badge bg-slate-800 text-slate-300">
                generated by {patch.generated_by}
              </span>
              {patch.base_commit_sha ? (
                <span className="badge bg-slate-800 text-slate-300">
                  base {patch.base_commit_sha.slice(0, 8)}
                </span>
              ) : null}
            </div>

            <dl className="mt-3 grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
              <Row
                label="Files changed"
                value={patch.affected_paths.join(', ') || '—'}
              />
              <Row
                label="Symbols modified"
                value={patch.symbols_modified.join(', ') || '—'}
              />
            </dl>

            {generationFailureNote(patch) ? (
              <p className="mt-3 text-sm text-argus-error">
                {generationFailureNote(patch)}
              </p>
            ) : null}

            {excluded.length > 0 ? (
              <p className="mt-3 text-sm text-argus-error">
                This patch touches {excluded.join(', ')}, outside the hypothesis&apos;s
                scope (§10). An out-of-scope change cannot be verified.
              </p>
            ) : null}
          </Card>

          <Card
            title="Diff"
            subtitle={`§43 — ${diffPaths(patch.patch_content).length} file(s) in this candidate`}
          >
            <DiffViewer patchContent={patch.patch_content} />
          </Card>

          <Card
            title="Verification"
            subtitle="§44, §64 — every rung reports what it actually observed"
          >
            {verification ? (
              <VerificationPanel
                run={verification}
                patchId={patch.id}
                projectId={projectId}
              />
            ) : (
              <p className="text-sm text-slate-400">
                This patch has not been verified. An unverified patch is a
                suggestion — the approval gate stays closed until verification
                produces evidence.
              </p>
            )}
          </Card>

          <Card title="Why was this fix generated? (§69)">
            <dl className="space-y-3 text-sm">
              {explanationRows(patch, hypothesis, verification).map((row) => (
                <div key={row.question}>
                  <dt className="text-xs uppercase tracking-wider text-slate-500">
                    {row.question}
                  </dt>
                  <dd className="text-slate-200">{row.answer}</dd>
                </div>
              ))}
            </dl>
          </Card>

          <Card
            title="Audit trail"
            subtitle="§70 — every action that touched this patch"
          >
            {auditTrail(patch).length === 0 ? (
              <p className="text-sm text-slate-400">
                No review action has been recorded yet.
              </p>
            ) : (
              <ul className="space-y-2 text-sm">
                {auditTrail(patch).map((entry, index) => (
                  <li key={`${entry.at}-${index}`} className="flex flex-wrap gap-2">
                    <span className="text-slate-400">{formatDate(entry.at)}</span>
                    <span className="text-slate-200">{entry.actor}</span>
                    <span className="badge bg-slate-800 text-slate-300">
                      {entry.action}
                    </span>
                    {entry.reason ? (
                      <span className="text-slate-400">{entry.reason}</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-3 text-xs text-slate-500">{REVIEW_BOUNDARY_NOTE}</p>
          </Card>
        </>
      ) : null}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className="text-slate-200">{value}</dd>
    </div>
  );
}

function DiffViewer({ patchContent }: { patchContent: string }) {
  const files = parseUnifiedDiff(patchContent);
  if (files.length === 0) {
    return (
      <p className="text-sm text-slate-400">
        This patch carries no unified diff to display.
      </p>
    );
  }
  return (
    <div className="space-y-4">
      {files.map((file) => (
        <div key={file.path}>
          <p className="mb-1 font-mono text-xs text-slate-300">{file.path}</p>
          <div className="overflow-x-auto rounded-md border border-slate-800 bg-slate-950/60">
            <table className="w-full font-mono text-xs">
              <tbody>
                {file.hunks.map((hunk, hunkIndex) =>
                  hunk.lines.map((line, lineIndex) => (
                    <tr
                      key={`${hunkIndex}-${lineIndex}`}
                      className={
                        line.kind === 'addition'
                          ? 'bg-argus-success/10 text-argus-success'
                          : line.kind === 'deletion'
                            ? 'bg-argus-error/10 text-argus-error'
                            : 'text-slate-400'
                      }
                    >
                      <td className="w-10 select-none px-2 text-right text-slate-600">
                        {line.oldLine ?? ''}
                      </td>
                      <td className="w-10 select-none px-2 text-right text-slate-600">
                        {line.newLine ?? ''}
                      </td>
                      <td className="w-4 select-none px-1">
                        {line.kind === 'addition'
                          ? '+'
                          : line.kind === 'deletion'
                            ? '−'
                            : ' '}
                      </td>
                      <td className="whitespace-pre-wrap px-2">{line.content}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
          <p className="mt-1 text-xs text-slate-500">
            +{file.additions} −{file.deletions}
          </p>
        </div>
      ))}
    </div>
  );
}

function VerificationPanel({
  run,
  patchId,
  projectId,
}: {
  run: PatchVerificationRun;
  patchId: string;
  projectId: string;
}) {
  const timeline = verificationTimeline(run);
  const checks = verificationChecks(run);
  const scope = `project_id=${encodeURIComponent(projectId)}`;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`badge ${verificationStatusStyle(run.status)}`}>
          {run.status}
        </span>
        <span className={`badge ${levelStyle(run.level)}`}>{run.level}</span>
        <span className="badge bg-slate-800 text-slate-300">
          confidence {run.confidence}
        </span>
        <span className={`badge ${tamperingStyle(run.tampering_flag)}`}>
          {tamperingLabel(run.tampering_flag)}
        </span>
        {run.verification_env_intact ? (
          <span className="badge bg-argus-success/20 text-argus-success">
            verification environment intact
          </span>
        ) : (
          <span className="badge bg-argus-error/20 text-argus-error">
            verification environment was modified
          </span>
        )}
        {run.duration_ms != null ? (
          <span className="badge bg-slate-800 text-slate-300">
            {formatDuration(run.duration_ms)}
          </span>
        ) : null}
      </div>

      {run.verdict_reason ? (
        <p
          className={`text-sm ${
            run.status === 'VERIFIED' ? 'text-argus-success' : 'text-argus-error'
          }`}
        >
          {run.verdict_reason}
        </p>
      ) : null}

      <ol className="space-y-2 text-sm">
        {timeline.map((stage) => (
          <li key={stage.key} className="rounded-md border border-slate-800 p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`badge ${stageStatusStyle(stage.status)}`}>
                {stage.status}
              </span>
              <span className="text-slate-200">{stage.title}</span>
              <span className="text-xs text-slate-500">{stage.detail}</span>
            </div>
            {stage.runs.length > 0 ? (
              <details className="mt-2">
                <summary className="cursor-pointer text-xs text-slate-500">
                  {stage.runs.length} command run(s)
                </summary>
                <div className="mt-2 space-y-2">
                  {stage.runs.map((item) => (
                    <TestRunRow key={item.id} run={item} />
                  ))}
                </div>
              </details>
            ) : null}
          </li>
        ))}
      </ol>

      <div>
        <h3 className="mb-2 text-xs uppercase tracking-wider text-slate-500">
          §64 checklist
        </h3>
        <ul className="space-y-1 text-sm">
          {checks.map((check) => (
            <li key={check.label} className="flex flex-wrap gap-2">
              <span
                className={
                  check.ok ? 'text-argus-success' : 'text-slate-500'
                }
              >
                {check.ok ? '✓' : '·'}
              </span>
              <span className="text-slate-300">{check.label}</span>
              <span className="text-xs text-slate-500">{check.detail}</span>
            </li>
          ))}
        </ul>
      </div>

      {run.regression_tests.length > 0 ? (
        <div>
          <h3 className="mb-2 text-xs uppercase tracking-wider text-slate-500">
            Regression test (§28, §29)
          </h3>
          {run.regression_tests.map((test) => (
            <p key={test.id} className="text-sm text-slate-300">
              <span className="font-mono text-xs">{test.file_path}</span> —{' '}
              {test.valid
                ? 'failed on base, passed on patched'
                : (test.invalid_reason ?? 'not a valid regression test')}
              {test.content_hash ? (
                <span className="ml-2 text-xs text-slate-500">
                  sha256 {test.content_hash.slice(0, 12)}…
                </span>
              ) : null}
            </p>
          ))}
        </div>
      ) : null}

      <div className="flex flex-wrap gap-3 text-xs">
        <Link href={`/api/v1/patches/${patchId}/report?${scope}`}>
          Verification report (§72)
        </Link>
        <Link href={`/api/v1/patches/${patchId}/artifacts?${scope}`}>
          Hashed artifacts (§45)
        </Link>
        <Link href={`/api/v1/patches/${patchId}/comparison?${scope}`}>
          Before/after comparison (§31)
        </Link>
      </div>
    </div>
  );
}

function TestRunRow({ run }: { run: PatchTestRun }) {
  return (
    <div className="rounded border border-slate-800 bg-slate-950/40 p-2 text-xs">
      <div className="flex flex-wrap items-center gap-2 text-slate-400">
        <span className="badge bg-slate-800 text-slate-300">{run.kind}</span>
        <span className="font-mono">{run.command_key}</span>
        {run.exit_code != null ? (
          <span>exit {run.exit_code}</span>
        ) : (
          <span>{run.unknown_configuration ? 'not run' : 'no exit code'}</span>
        )}
        {run.timed_out ? <span className="text-argus-error">timed out</span> : null}
        {run.duration_ms != null ? <span>{formatDuration(run.duration_ms)}</span> : null}
      </div>
      {run.selection_reason ? (
        <p className="mt-1 text-slate-500">{run.selection_reason}</p>
      ) : null}
      {run.selected_tests.length > 0 ? (
        <p className="mt-1 font-mono text-slate-500">
          {run.selected_tests.join(' ')}
        </p>
      ) : null}
      {run.output_tail ? (
        <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap text-slate-400">
          {run.output_tail}
        </pre>
      ) : null}
    </div>
  );
}
