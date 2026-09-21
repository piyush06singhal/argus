import Link from 'next/link';

import {
  api,
  formatDate,
  type EarlyWarning,
  type Forecast,
  type ModelVersionList,
  type PaginatedResponse,
  type PlatformHealth,
  type Project,
  type RiskHeatmap,
  type RiskHeatmapCell,
} from '@/lib/api';
import {
  confidenceLabel,
  coverageLabel,
  dataQualityStyle,
  dataQualityLabel,
  DATA_QUALITY_LABELS,
  FORECAST_DISCLAIMER,
  HORIZON_LABELS,
  percentLabel,
  riskLevelStyle,
  riskPhrase,
  riskScoreLabel,
  sortHeatmapCells,
  UNKNOWN_RISK_NOTE,
} from '@/lib/reliability';

export const metadata = {
  title: 'Reliability Intelligence',
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

const HEATMAP_TONE: Record<string, string> = {
  CRITICAL: 'bg-argus-error/40 text-argus-error',
  HIGH: 'bg-argus-warning/40 text-argus-warning',
  MEDIUM: 'bg-argus-info/30 text-argus-info',
  LOW: 'bg-argus-success/20 text-argus-success',
  UNKNOWN: 'bg-slate-800 text-slate-400',
};

/**
 * The predictive-reliability dashboard (§45, §46).
 *
 * Every number on this page either carries its own context (sample size,
 * coverage, thresholds) or says plainly that it has none. An empty heatmap
 * renders its `empty_reason` rather than an empty grid, and UNKNOWN is never
 * coloured as health.
 */
export default async function ReliabilityPage({
  searchParams,
}: {
  searchParams: { project_id?: string };
}) {
  const requested =
    typeof searchParams.project_id === 'string' ? searchParams.project_id : '';

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
          Reliability Intelligence
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          ARGUS transforms historical telemetry, incidents, anomalies, deployments
          and code changes into features, forecasts risk over four horizons with
          deterministic baseline models, scores every forecast against what actually
          happened, and raises deduplicated early warnings for human review.
        </p>
        <p className="mt-2 text-xs text-slate-500">{FORECAST_DISCLAIMER}</p>
        <p className="mt-1 text-xs text-slate-500">{UNKNOWN_RISK_NOTE}</p>
      </div>

      {projects.length === 0 ? (
        <Card title="No projects" subtitle="Forecasts are project-scoped">
          <p className="text-sm text-slate-400">
            No project could be loaded, so there is nothing to scope forecasts to.
          </p>
        </Card>
      ) : (
        <ProjectPicker projects={projects} selectedId={project?.id ?? ''} />
      )}

      {project ? <ReliabilitySections projectId={project.id} /> : null}
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
    <Card title="Project scope" subtitle="Forecasts are always project-scoped">
      <div className="flex flex-wrap gap-2">
        {projects.map((item) => (
          <Link
            key={item.id}
            href={`/reliability?project_id=${encodeURIComponent(item.id)}`}
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

async function ReliabilitySections({ projectId }: { projectId: string }) {
  let health: PlatformHealth | null = null;
  let heatmap: RiskHeatmap | null = null;
  let warnings: EarlyWarning[] = [];
  let models: ModelVersionList | null = null;
  let error: string | null = null;

  try {
    const [healthData, heatmapData, warningData, modelData] = await Promise.all([
      api.reliabilityHealth(projectId),
      api.getRiskHeatmap({ project_id: projectId }),
      api.listEarlyWarnings({ project_id: projectId, limit: 10 }),
      api.listModelVersions(20),
    ]);
    health = healthData;
    heatmap = heatmapData;
    warnings = warningData.items;
    models = modelData;
  } catch (cause) {
    error = cause instanceof Error ? cause.message : String(cause);
  }

  if (error !== null) {
    return (
      <Card title="Reliability platform" subtitle="Could not be loaded">
        <p className="text-sm text-argus-error">{error}</p>
      </Card>
    );
  }
  if (!health || !heatmap) {
    return null;
  }

  const accuracy = health.accuracy as {
    sample_count?: number;
    metrics?: Record<string, number>;
    notes?: string[];
    status?: string;
  };
  const drift = health.drift as {
    by_status?: Record<string, number>;
    requiring_review?: number;
    total?: number;
  };
  const warningSummary = health.warnings as { open?: number };
  const cells = sortHeatmapCells(heatmap.cells);

  return (
    <>
      <Card
        title="Overall state"
        subtitle={`generated ${formatDate(health.generated_at)}`}
      >
        <dl className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Metric label="Forecasts" value={String(health.forecast_count)} />
          <Metric label="Active" value={String(health.active_forecasts)} />
          <Metric
            label="High / critical risk"
            value={String(health.high_risk_forecasts)}
          />
          <Metric label="Unknown (no evidence)" value={String(health.unknown_forecasts)} />
          <Metric
            label="Open warnings"
            value={String(warningSummary.open ?? 0)}
          />
          <Metric label="Drift findings" value={String(drift.total ?? 0)} />
          <Metric
            label="Drift requiring review"
            value={String(drift.requiring_review ?? 0)}
          />
          <Metric label="Model versions" value={String(health.model_version_count)} />
        </dl>
        <div className="mt-4 flex flex-wrap gap-2">
          {Object.entries(health.data_quality_distribution).map(([quality, count]) => (
            <span
              key={quality}
              className={`badge ${dataQualityStyle(quality as never)}`}
            >
              {DATA_QUALITY_LABELS[quality as keyof typeof DATA_QUALITY_LABELS] ?? quality}: {count}
            </span>
          ))}
        </div>
        <p className="mt-3 text-xs text-slate-500">
          Risk bands: MEDIUM ≥ {health.thresholds.threshold_medium} · HIGH ≥{' '}
          {health.thresholds.threshold_high} · CRITICAL ≥{' '}
          {health.thresholds.threshold_critical} (configurable policy)
        </p>
      </Card>

      <Card
        title="Risk heatmap"
        subtitle="Newest forecast per component × horizon. A blank cell means no forecast exists — it is not LOW."
      >
        {cells.length === 0 ? (
          <p className="text-sm text-slate-400">
            {heatmap.empty_reason ?? 'No forecasts exist in this scope yet.'} Generate
            a forecast pass to populate the grid.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-3 py-2">Component</th>
                  <th className="px-3 py-2">Prediction</th>
                  {heatmap.horizons.map((horizon) => (
                    <th key={horizon} className="px-3 py-2">
                      {HORIZON_LABELS[horizon] ?? horizon}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {cells.map((cell: RiskHeatmapCell, index: number) => (
                  <tr key={`${cell.component_id}-${cell.prediction_type}-${index}`}>
                    <td className="px-3 py-2 text-slate-200">
                      {cell.component_name ?? cell.component_id ?? '—'}
                    </td>
                    <td className="px-3 py-2 text-slate-400">{cell.prediction_type}</td>
                    {heatmap.horizons.map((horizon) => {
                      const level = cell.by_horizon[horizon];
                      return (
                        <td key={horizon} className="px-3 py-2">
                          {level ? (
                            <span className={`badge ${HEATMAP_TONE[level]}`}>
                              {level}
                            </span>
                          ) : (
                            <span className="text-slate-600">—</span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card
          title="Prediction accuracy"
          subtitle="Never shown without its sample size"
        >
          {accuracy.status === 'NO_EVALUATION_RUN' ? (
            <p className="text-sm text-slate-400">
              {Array.isArray(accuracy.notes) ? accuracy.notes.join(' ') : null}
            </p>
          ) : (
            <dl className="grid grid-cols-2 gap-4">
              <Metric
                label="Precision"
                value={
                  accuracy.metrics?.precision != null
                    ? percentLabel(accuracy.metrics.precision)
                    : 'not reported'
                }
              />
              <Metric
                label="Recall"
                value={
                  accuracy.metrics?.recall != null
                    ? percentLabel(accuracy.metrics.recall)
                    : 'not reported'
                }
              />
              <Metric
                label="Sample size"
                value={String(accuracy.sample_count ?? 0)}
              />
              <Metric label="Run status" value={String(accuracy.status ?? '—')} />
            </dl>
          )}
          <div className="mt-3">
            <Link
              href={`/reliability/accuracy?project_id=${encodeURIComponent(projectId)}`}
              className="text-xs text-argus-accent hover:underline"
            >
              Accuracy dashboard →
            </Link>
          </div>
        </Card>

        <Card title="Early warnings" subtitle="Deduplicated; a human decides">
          {warnings.length === 0 ? (
            <p className="text-sm text-slate-400">No open early warnings.</p>
          ) : (
            <ul className="space-y-3">
              {warnings.map((warning) => (
                <li
                  key={warning.id}
                  className="rounded border border-slate-800 bg-slate-900/50 p-3"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={`badge ${riskLevelStyle(warning.severity)}`}>
                      {warning.severity}
                    </span>
                    <span className="text-sm text-slate-200">{warning.title}</span>
                  </div>
                  {warning.description ? (
                    <p className="mt-1 text-xs text-slate-500">{warning.description}</p>
                  ) : null}
                  <p className="mt-1 text-xs text-slate-600">
                    raised {formatDate(warning.first_raised_at)} · seen{' '}
                    {warning.occurrence_count}× · status {warning.status}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card
        title="Model registry"
        subtitle="Every forecast names the model version that produced it"
      >
        {!models || models.items.length === 0 ? (
          <p className="text-sm text-slate-400">
            No model versions registered yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-3 py-2">Model</th>
                  <th className="px-3 py-2">Version</th>
                  <th className="px-3 py-2">Type</th>
                  <th className="px-3 py-2">Status</th>
                  <th className="px-3 py-2">Calibration</th>
                  <th className="px-3 py-2">Samples</th>
                </tr>
              </thead>
              <tbody>
                {models.items.map((model) => (
                  <tr key={model.id}>
                    <td className="px-3 py-2 text-slate-200">{model.model_name}</td>
                    <td className="px-3 py-2 text-slate-400">{model.version}</td>
                    <td className="px-3 py-2 text-slate-400">{model.model_type}</td>
                    <td className="px-3 py-2 text-slate-400">{model.status}</td>
                    <td className="px-3 py-2 text-slate-400">
                      {model.calibration_status}
                    </td>
                    <td className="px-3 py-2 text-slate-400">{model.sample_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="mt-3 flex flex-wrap gap-4 text-xs">
          <Link
            href={`/reliability/backtests?project_id=${encodeURIComponent(projectId)}`}
            className="text-argus-accent hover:underline"
          >
            Backtests →
          </Link>
          <Link
            href={`/reliability/models?project_id=${encodeURIComponent(projectId)}`}
            className="text-argus-accent hover:underline"
          >
            Model registry →
          </Link>
          <Link
            href={`/reliability/forecasts?project_id=${encodeURIComponent(projectId)}`}
            className="text-argus-accent hover:underline"
          >
            All forecasts →
          </Link>
        </div>
      </Card>
    </>
  );
}
