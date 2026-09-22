import Link from 'next/link';

import {
  api,
  formatDate,
  type Project,
  type RemediationPolicy,
} from '@/lib/api';
import { blastRadiusLabel, MODE_EXPLANATIONS } from '@/lib/remediation';

import PolicyEditor from './PolicyEditor';

export const metadata = {
  title: 'Remediation policy',
};

export const dynamic = 'force-dynamic';

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className="text-slate-200">{value}</dd>
    </div>
  );
}

/**
 * The policy for one scope (§21, §40, §45).
 *
 * The page shows the *effective* policy even when there is no stored row, and it
 * says plainly when the process narrowed a stored value — because a policy that
 * silently differs from what is written here is worse than no policy page at all.
 */
export default async function RemediationPolicyPage({
  searchParams,
}: {
  searchParams: { project_id?: string };
}) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  let projects: Project[] = [];
  try {
    const response = await api.listProjects(1, 50);
    projects = response.items;
  } catch {
    projects = [];
  }
  const project = projects.find((item) => item.id === requested) ?? projects[0];

  let policy: RemediationPolicy | null = null;
  if (project) {
    try {
      policy = await api.getRemediationPolicy(project.id);
    } catch {
      policy = null;
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <Link
          href={
            project
              ? `/remediation?project_id=${encodeURIComponent(project.id)}`
              : '/remediation'
          }
          className="text-sm text-argus-accent hover:underline"
        >
          ← Remediation console
        </Link>
        <h1 className="mt-2 text-2xl font-semibold text-slate-100">
          Remediation policy
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          The policy decides whether an action ARGUS has already assessed and
          found safe is permitted to run here, under what budget, and whether a
          human must authorize it. Configuration only ever narrows: no value set
          here can raise the ceilings compiled into the platform.
        </p>
      </div>

      {projects.length > 1 ? (
        <section className="card">
          <h2 className="font-medium text-slate-200">Project scope</h2>
          <div className="mt-3 flex flex-wrap gap-2">
            {projects.map((item) => (
              <Link
                key={item.id}
                href={`/remediation/policy?project_id=${encodeURIComponent(item.id)}`}
                className={`badge ${
                  item.id === project?.id
                    ? 'bg-argus-accent/20 text-argus-accent'
                    : 'bg-slate-800 text-slate-300'
                }`}
              >
                {item.name}
              </Link>
            ))}
          </div>
        </section>
      ) : null}

      {!project ? (
        <section className="card">
          <p className="text-sm text-slate-400">
            No project could be loaded, so there is no scope to configure.
          </p>
        </section>
      ) : !policy ? (
        <section className="card">
          <p className="text-sm text-argus-warning">
            The policy for this scope could not be loaded. Nothing is assumed
            about it: with no readable policy the scope is on the restrictive
            default, and nothing is authorized.
          </p>
        </section>
      ) : (
        <>
          <section className="card">
            <h2 className="font-medium text-slate-200">Effective policy</h2>
            <p className="mt-1 text-xs text-slate-500">
              {policy.source === 'fallback'
                ? 'No row is stored for this scope, so the restrictive default is in force.'
                : `Stored policy, revision ${policy.revision ?? '—'}, last written by ${
                    policy.updated_by ?? 'unknown'
                  }.`}
            </p>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <span className="badge bg-slate-800 text-slate-200">
                {policy.execution_mode}
              </span>
              {policy.emergency_stop_active ? (
                <span className="badge bg-argus-error/20 text-argus-error">
                  emergency stop engaged by{' '}
                  {policy.emergency_stop_by ?? 'unknown'} at{' '}
                  {formatDate(policy.emergency_stop_at)}
                </span>
              ) : null}
              {policy.clamped.length > 0 ? (
                <span className="badge bg-argus-warning/20 text-argus-warning">
                  narrowed by configuration
                </span>
              ) : null}
            </div>
            <p className="mt-2 text-sm text-slate-300">
              {MODE_EXPLANATIONS[policy.execution_mode]}
            </p>
            {policy.emergency_stop_reason ? (
              <p className="mt-1 text-xs text-argus-error">
                {policy.emergency_stop_reason}
              </p>
            ) : null}

            <dl className="mt-4 grid gap-4 sm:grid-cols-3 lg:grid-cols-6">
              <Metric
                label="Autonomous ceiling"
                value={policy.autonomous_max_risk}
              />
              <Metric
                label="Actions per window"
                value={String(policy.max_actions_per_window)}
              />
              <Metric
                label="Window"
                value={`${policy.action_window_seconds}s`}
              />
              <Metric label="Cooldown" value={`${policy.cooldown_seconds}s`} />
              <Metric
                label="Concurrent"
                value={String(policy.max_concurrent_actions)}
              />
              <Metric
                label="Blast radius"
                value={blastRadiusLabel(
                  policy.max_blast_radius_scope,
                  policy.max_blast_radius_percent
                )}
              />
              <Metric
                label="Approval TTL"
                value={`${policy.approval_ttl_seconds}s`}
              />
              <Metric
                label="Verification window"
                value={`${policy.verification_window_seconds}s`}
              />
              <Metric
                label="Execution timeout"
                value={`${policy.execution_timeout_seconds}s`}
              />
              <Metric
                label="Action expiry"
                value={`${policy.action_expiry_seconds}s`}
              />
              <Metric
                label="Canary"
                value={
                  policy.canary_enabled
                    ? `yes, ${policy.canary_percent}% first`
                    : 'disabled'
                }
              />
              <Metric
                label="Action allow-list"
                value={
                  policy.allowed_action_types == null
                    ? 'all registered actions'
                    : `${policy.allowed_action_types.length} listed`
                }
              />
            </dl>

            {policy.clamped.length > 0 ? (
              <div className="mt-4">
                <h3 className="text-xs uppercase tracking-wider text-slate-500">
                  Values the platform narrowed
                </h3>
                <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-argus-warning">
                  {policy.clamped.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </section>

          <PolicyEditor projectId={project.id} policy={policy} />
        </>
      )}
    </div>
  );
}
