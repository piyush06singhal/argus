import Link from 'next/link';

import {
  api,
  formatDate,
  type ErrorBudgetResponse,
  type Project,
  type SloEvaluationResponse,
} from '@/lib/api';
import {
  burnStateLabel,
  burnStateStyle,
  formatRatio,
  PLATFORM_BOUNDARY,
  sloStatusLabel,
  sloStatusStyle,
} from '@/lib/platform';
import { Card, Empty, Facts, Limitations, Table, Tile, readError } from '../../ui';

export const metadata = {
  title: 'Objective',
};

export const dynamic = 'force-dynamic';

/**
 * One objective and its error budget (§33–§35).
 *
 * The error budget is rendered as an allowance that is being spent, not as a
 * target to hit. The burn history is shown oldest-to-newest with each point's
 * state, so a burn that is accelerating is visible as a shape rather than an
 * averaged number that hides the acceleration.
 */
export default async function SloDetailPage({
  params,
  searchParams,
}: {
  params: { sloId: string };
  searchParams: { project_id?: string };
}) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

  let projects: Project[] = [];
  try {
    projects = (await api.listProjects(1, 50)).items;
  } catch {
    projects = [];
  }
  const project = projects.find((item) => item.id === requested) ?? projects[0];

  if (!project) {
    return (
      <Card title="No projects">
        <Empty>Objectives are project-scoped.</Empty>
      </Card>
    );
  }

  let slo: SloEvaluationResponse | null = null;
  let budget: ErrorBudgetResponse | null = null;
  let error: string | null = null;
  const [sloResult, budgetResult] = await Promise.allSettled([
    api.platformSloDetail(params.sloId, project.id),
    api.platformErrorBudget(params.sloId, project.id),
  ]);
  if (sloResult.status === 'fulfilled') {
    slo = sloResult.value;
  } else {
    error = readError(sloResult.reason);
  }
  if (budgetResult.status === 'fulfilled') {
    budget = budgetResult.value;
  }

  if (error || !slo) {
    return (
      <Card title="Objective unavailable">
        <Empty>{error ?? 'The objective could not be loaded.'}</Empty>
        <Link href={`/platform/slo?project_id=${project.id}`} className="mt-3 inline-block text-xs text-argus-accent">
          ← Back to objectives
        </Link>
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <Link href={`/platform/slo?project_id=${project.id}`} className="text-xs text-argus-accent">
          ← SLO &amp; Reliability
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold text-slate-100">{slo.name}</h1>
          <span className={`badge ${sloStatusStyle(slo.status)}`}>
            {sloStatusLabel(slo.status)}
          </span>
          <span className="badge bg-slate-800 text-slate-300">
            data: {slo.data_quality}
          </span>
        </div>
        <p className="mt-1 text-sm text-slate-400">
          {slo.indicator} · target {slo.comparison} {slo.target}
          {` · ${slo.sample_count} sample(s) in the window`}
        </p>
        <p className="mt-2 text-xs text-slate-500">{PLATFORM_BOUNDARY}</p>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Tile
          label="Reading"
          value={slo.reading != null ? formatRatio(slo.reading) : '—'}
          hint="Latest evaluation"
        />
        <Tile
          label="Remaining budget"
          value={slo.remaining_percent != null ? `${slo.remaining_percent.toFixed(1)}%` : '—'}
          hint="Allowance still available"
        />
        <Tile
          label="Burn rate"
          value={slo.burn_rate != null ? `${slo.burn_rate.toFixed(2)}×` : '—'}
          hint="Multiple of the sustainable rate"
        />
        <Tile
          label="Burn state"
          value={slo.burn_state ? burnStateLabel(slo.burn_state) : 'Unknown'}
          hint="Relative to the platform thresholds"
        />
      </div>

      <Card title="Evaluation evidence" subtitle="The stored rows that produced the reading">
        <Facts data={slo.evidence} empty="No evidence was attached to the evaluation." />
        {slo.compliance_percent != null ? (
          <p className="mt-3 text-sm text-slate-300">
            Compliance: {slo.compliance_percent.toFixed(2)}%
          </p>
        ) : null}
        <Limitations items={slo.limitations} />
      </Card>

      <Card title="Error budget" subtitle="§34, §35 — an allowance, not a target">
        {budget?.latest ? (
          <Facts data={budget.latest} empty="No evaluation has been stored." />
        ) : (
          <Empty>
            No error-budget evaluation has been stored. An objective with no
            evaluation is not evidence of compliance.
          </Empty>
        )}
        {budget?.definition ? (
          <p className="mt-3 text-xs text-slate-500">{budget.definition}</p>
        ) : null}
      </Card>

      <Card title="Burn history" subtitle="Oldest to newest, one row per evaluation">
        {!budget || budget.history.length === 0 ? (
          <Empty>No evaluation history is available.</Empty>
        ) : (
          <Table
            headers={['Computed', 'Reading', 'Burn rate', 'Burn state', 'Status']}
            rows={budget.history.map((item) => {
              const reading = typeof item.reading === 'number' ? item.reading : null;
              const burnRate = typeof item.burn_rate === 'number' ? item.burn_rate : null;
              const burnState = typeof item.burn_state === 'string' ? item.burn_state : null;
              const status = typeof item.status === 'string' ? item.status : null;
              return [
                formatDate(typeof item.computed_at === 'string' ? item.computed_at : null),
                reading != null ? formatRatio(reading) : '—',
                burnRate != null ? `${burnRate.toFixed(2)}×` : '—',
                burnState ? (
                  <span key="b" className={`badge ${burnStateStyle(burnState)}`}>
                    {burnStateLabel(burnState)}
                  </span>
                ) : (
                  '—'
                ),
                status ? (
                  <span key="s" className={`badge ${sloStatusStyle(status)}`}>
                    {sloStatusLabel(status)}
                  </span>
                ) : (
                  '—'
                ),
              ];
            })}
          />
        )}
      </Card>
    </div>
  );
}
