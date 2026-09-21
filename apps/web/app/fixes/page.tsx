import Link from 'next/link';

import {
  api,
  formatDate,
  type FixHypothesis,
  type FixMetrics,
  type PaginatedResponse,
  type Patch,
  type Project,
} from '@/lib/api';
import {
  FIX_FLOW,
  FIX_STATUS_FILTERS,
  PATCH_DISCLAIMER,
  PATCH_STATUS_FILTERS,
  categoryLabel,
  patchSizeSummary,
  patchStatusStyle,
  REVIEW_BOUNDARY_NOTE,
  riskStyle,
} from '@/lib/fixes';

export const metadata = {
  title: 'Fix & Verification',
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

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className="text-slate-200">{value}</dd>
    </div>
  );
}

/**
 * The fix candidate dashboard (§66, §67).
 *
 * Deliberately shows *states*, not a score: a patch that reached
 * ``VALIDATION_FAILED`` is presented as a refusal that happened, not as a low
 * score. Project scope is required by the API — it is what proves ownership —
 * so the page resolves one explicitly.
 */
export default async function FixesPage({
  searchParams,
}: {
  searchParams: {
    project_id?: string;
    status?: string;
    patch_status?: string;
  };
}) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';
  const statusFilter =
    typeof searchParams.status === 'string' ? searchParams.status : '';
  const patchStatusFilter =
    typeof searchParams.patch_status === 'string' ? searchParams.patch_status : '';

  let projects: Project[] = [];
  try {
    const response: PaginatedResponse<Project> = await api.listProjects(1, 50);
    projects = response.items;
  } catch {
    projects = [];
  }

  const project = projects.find((item) => item.id === requested) ?? projects[0];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">
          Fix &amp; Verification
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          ARGUS generates the smallest defensible change, validates it, applies it
          inside a disposable workspace, runs the repository&apos;s own checks, proves
          the failure is gone with a two-sided regression test, and stops at human
          review.
        </p>
        <p className="mt-2 text-xs text-slate-500">{PATCH_DISCLAIMER}</p>
        <p className="mt-1 text-xs text-slate-500">
          {FIX_FLOW.join(' → ')}
        </p>
      </div>

      {projects.length === 0 ? (
        <Card title="No projects" subtitle="Fixes are project-scoped">
          <p className="text-sm text-slate-400">
            No project could be loaded, so there is nothing to scope fixes to.
            Project scope is how the API proves ownership.
          </p>
        </Card>
      ) : (
        <ProjectPicker projects={projects} selectedId={project?.id ?? ''} />
      )}

      {project ? (
        <FixSections
          projectId={project.id}
          statusFilter={statusFilter}
          patchStatusFilter={patchStatusFilter}
        />
      ) : null}
    </div>
  );
}

function ProjectPicker({
  projects,
  selectedId,
}: {
  projects: Project[];
  selectedId: string;
}) {
  return (
    <Card title="Project scope" subtitle="Fixes are always project-scoped">
      <div className="flex flex-wrap gap-2">
        {projects.map((item) => (
          <Link
            key={item.id}
            href={`/fixes?project_id=${encodeURIComponent(item.id)}`}
            className={`badge ${
              item.id === selectedId
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

async function FixSections({
  projectId,
  statusFilter,
  patchStatusFilter,
}: {
  projectId: string;
  statusFilter: string;
  patchStatusFilter: string;
}) {
  let hypotheses: FixHypothesis[] = [];
  let patches: Patch[] = [];
  let metrics: FixMetrics | null = null;
  let error: string | null = null;

  try {
    const response = await api.listFixes({
      projectId,
      status: statusFilter || undefined,
    });
    hypotheses = response.items;
  } catch (cause: unknown) {
    error = cause instanceof Error ? cause.message : 'unknown error';
  }

  try {
    const response = await api.listPatches({
      projectId,
      status: patchStatusFilter || undefined,
    });
    patches = response.items;
  } catch (cause: unknown) {
    error = error ?? (cause instanceof Error ? cause.message : 'unknown error');
  }

  try {
    metrics = await api.fixMetrics(projectId);
  } catch {
    metrics = null;
  }

  return (
    <>
      {metrics ? (
        <Card
          title="Fix engine health"
          subtitle="§67 — counts of what happened, never a confidence score"
        >
          <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <Metric label="Hypotheses" value={String(metrics.hypotheses_total)} />
            <Metric label="Patches" value={String(metrics.patches_total)} />
            <Metric label="Verified" value={String(metrics.patches_verified)} />
            <Metric
              label="Awaiting review"
              value={String(metrics.patches_awaiting_review)}
            />
            <Metric label="Rejected" value={String(metrics.patches_rejected)} />
            <Metric
              label="Verification runs"
              value={String(metrics.verification_runs_total)}
            />
            <Metric
              label="Tampering flags"
              value={String(metrics.tampering_flags_total)}
            />
            <Metric
              label="Regressions detected"
              value={String(metrics.regressions_detected_total)}
            />
          </dl>
          {metrics.tampering_flags_total > 0 ? (
            <p className="mt-3 text-xs text-argus-error">
              At least one candidate tried to tamper with its own verification.
              Those detections are why Phase 7 verifies patches in a disposable
              workspace instead of trusting them.
            </p>
          ) : null}
          <p className="mt-3 text-xs text-slate-500">{REVIEW_BOUNDARY_NOTE}</p>
        </Card>
      ) : null}

      {error ? <p className="text-sm text-argus-error">{error}</p> : null}

      <Card
        title="Fix candidates"
        subtitle={`${hypotheses.length} hypotheses`}
      >
        <div className="mb-3 flex flex-wrap gap-2">
          {['', ...FIX_STATUS_FILTERS].map((value) => (
            <Link
              key={value || 'ALL'}
              href={`/fixes?project_id=${encodeURIComponent(projectId)}${
                value ? `&status=${value}` : ''
              }`}
              className={`badge ${
                statusFilter === value
                  ? 'bg-argus-accent/20 text-argus-accent'
                  : 'bg-slate-800 text-slate-300'
              }`}
            >
              {value || 'All'}
            </Link>
          ))}
        </div>

        {hypotheses.length === 0 ? (
          <p className="text-sm text-slate-400">
            No fix hypothesis matches this filter. A hypothesis is planned from a
            debug session&apos;s validated code locations, never invented here.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-3 py-2">Hypothesis</th>
                  <th className="px-3 py-2">Category</th>
                  <th className="px-3 py-2">Risk</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2">Scope</th>
                  <th className="px-3 py-2">Created</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {hypotheses.map((item) => (
                  <tr key={item.id} className="align-top">
                    <td className="px-3 py-2">
                      <Link href={`/fixes/${item.id}`}>{item.title}</Link>
                      <p className="mt-1 max-w-md text-xs text-slate-500">
                        {item.proposed_change}
                      </p>
                    </td>
                    <td className="px-3 py-2 text-xs text-slate-400">
                      {categoryLabel(item.category)}
                    </td>
                    <td className="px-3 py-2">
                      <span className={`badge ${riskStyle(item.risk_level)}`}>
                        {item.risk_level}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      <span className="badge bg-slate-800 text-slate-300">
                        {item.status}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-xs text-slate-400">
                      {item.scope_files.join(', ') || '—'}
                    </td>
                    <td className="px-3 py-2 text-xs text-slate-400">
                      {formatDate(item.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Patches" subtitle={`${patches.length} candidates`}>
        <div className="mb-3 flex flex-wrap gap-2">
          {['', ...PATCH_STATUS_FILTERS].map((value) => (
            <Link
              key={value || 'ALL'}
              href={`/fixes?project_id=${encodeURIComponent(projectId)}${
                statusFilter ? `&status=${statusFilter}` : ''
              }${value ? `&patch_status=${value}` : ''}`}
              className={`badge ${
                patchStatusFilter === value
                  ? 'bg-argus-accent/20 text-argus-accent'
                  : 'bg-slate-800 text-slate-300'
              }`}
            >
              {value || 'All'}
            </Link>
          ))}
        </div>

        {patches.length === 0 ? (
          <p className="text-sm text-slate-400">
            No patch matches this filter.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-3 py-2">Patch</th>
                  <th className="px-3 py-2">Size</th>
                  <th className="px-3 py-2">Files</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2">Review</th>
                  <th className="px-3 py-2">Created</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {patches.map((item) => (
                  <tr key={item.id} className="align-top">
                    <td className="px-3 py-2">
                      <Link href={`/fixes/${item.fix_hypothesis_id}?patch=${item.id}`}>
                        {item.generated_by}
                      </Link>
                      {item.failure_reason ? (
                        <p className="mt-1 max-w-md text-xs text-argus-error">
                          {item.failure_reason}
                        </p>
                      ) : null}
                    </td>
                    <td className="px-3 py-2 text-xs text-slate-400">
                      {patchSizeSummary(item)}
                    </td>
                    <td className="px-3 py-2 text-xs text-slate-400">
                      {item.affected_paths.join(', ') || '—'}
                    </td>
                    <td className="px-3 py-2">
                      <span className={`badge ${patchStatusStyle(item.status)}`}>
                        {item.status}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-xs text-slate-400">
                      {item.review_state ?? '—'}
                    </td>
                    <td className="px-3 py-2 text-xs text-slate-400">
                      {formatDate(item.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
